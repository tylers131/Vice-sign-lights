"""Pure-Python scheduler thread.

Two kinds of trigger:

* **Schedules** -- wall-clock, "apply scene X at 19:30 on these days".  They
  reload from config on every tick, so editing the config file (or the UI)
  takes effect without a restart, and they survive a restart because they live
  in the config.  They are skipped entirely while the clock is unset, since
  firing "19:30" against a 1970 clock would be worse than doing nothing.

* **Timers** -- one-shot relative countdowns ("this scene in 45 minutes"),
  measured on ``time.monotonic``.  These work fine with no RTC and no NTP, and
  are the recommended trigger when you never bothered to set the clock.  They
  are in-memory: a service restart clears them.
"""

from __future__ import annotations

import logging
import threading
import time

import random

from .config import new_id
from .messages import MatrixRunner
from .schedule import Schedule
from .thermometer import Thermometer

log = logging.getLogger("vicelights.scheduler")

TICK = 5.0
# How far past its minute a schedule may still fire.  Keeps a schedule from
# retroactively firing hours later when the clock is jumped forward.
GRACE_SECONDS = 90

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Rotation:
    """Cycle scenes all night so the sign keeps changing.

    Timed on ``time.monotonic``, never the wall clock: the Zero W forgets the
    time across a cold boot and there is no NTP on the playa, so anything
    depending on knowing the date would simply not run. Rotation has to work
    from the moment the Pi powers on, knowing nothing.
    """

    def __init__(self, store, worker):
        self.store = store
        self.worker = worker
        self._lock = threading.Lock()
        self._bag = []
        self._last_played = None
        self._next_at = None          # monotonic
        self._current = None
        self._warned_empty = False

    # ------------------------------------------------------------------ state

    def _interval(self, rotation) -> float:
        return float(rotation["interval_minutes"]) * 60.0

    def reschedule(self, delay: float = None):
        """Push the next change out, e.g. after a config edit or a manual skip."""
        rotation = self.store.rotation()
        with self._lock:
            self._next_at = time.monotonic() + (
                self._interval(rotation) if delay is None else delay)

    def status(self) -> dict:
        rotation = self.store.rotation()
        names = self.store.rotation_scenes()
        with self._lock:
            next_at, current = self._next_at, self._current
        remaining = None
        if rotation["enabled"] and next_at is not None:
            remaining = max(0, int(next_at - time.monotonic()))
        return {
            "enabled": rotation["enabled"],
            "order": rotation["order"],
            "interval_minutes": rotation["interval_minutes"],
            "hold_after_manual_minutes": rotation["hold_after_manual_minutes"],
            "playlist": rotation["playlist"],
            "exclude": rotation["exclude"],
            "avoid_repeat": rotation["avoid_repeat"],
            "scenes": names,
            "current": current,
            "next_in_seconds": remaining,
            "holding": self._hold_remaining(rotation) > 0,
            "hold_remaining_seconds": int(self._hold_remaining(rotation)),
        }

    def _hold_remaining(self, rotation) -> float:
        hold = float(rotation.get("hold_after_manual_minutes", 0)) * 60.0
        if hold <= 0:
            return 0.0
        last = self.worker.last_manual_at
        if last is None:            # nothing touched since boot
            return 0.0
        since = time.monotonic() - last
        return max(0.0, hold - since)

    # ------------------------------------------------------------------- play

    def _next_name(self, names, rotation):
        if rotation["order"] == "sequential":
            if self._last_played in names:
                index = (names.index(self._last_played) + 1) % len(names)
            else:
                index = 0
            return names[index]

        if not self._bag:
            self._bag = list(names)
            random.shuffle(self._bag)
            # A fresh bag whose first pick repeats the last one is the only way
            # shuffle can stutter; swap it away when there is an alternative.
            if (rotation["avoid_repeat"] and len(self._bag) > 1
                    and self._bag[0] == self._last_played):
                self._bag[0], self._bag[1] = self._bag[1], self._bag[0]
        return self._bag.pop(0)

    def note_played(self, name: str):
        """Record a scene as the current one without playing it.

        Used for the boot scene: rotation should treat it as this interval's
        pick rather than immediately replacing it with another 50s sweep.
        """
        with self._lock:
            self._last_played = name
            self._current = name
            self._next_at = time.monotonic() + self._interval(self.store.rotation())

    def play_next(self, force: bool = False) -> str:
        """Advance to the next scene now. Returns the name, or None."""
        rotation = self.store.rotation()
        names = self.store.rotation_scenes()
        if not names:
            if not self._warned_empty:
                log.warning("rotation has no scenes to play (check playlist/exclude)")
                self._warned_empty = True
            return None
        self._warned_empty = False
        if len(names) == 1 and not force:
            return None

        with self._lock:
            name = self._next_name(names, rotation)
            self._last_played = name
            self._current = name
            self._next_at = time.monotonic() + self._interval(rotation)

        scene = self.store.scene(name)
        if not scene:
            log.error("rotation picked missing scene '%s'", name)
            return None
        log.info("rotation -> '%s' (next in %.0f min)", name, rotation["interval_minutes"])
        self.worker.submit_scene(scene)
        return name

    # ------------------------------------------------------------------- tick

    def tick(self):
        rotation = self.store.rotation()
        if not rotation["enabled"]:
            with self._lock:
                self._next_at = None
            return

        with self._lock:
            if self._next_at is None:
                # Freshly enabled: change on the next tick rather than making
                # someone wait a full interval to see that it works.
                self._next_at = time.monotonic()
            due = time.monotonic() >= self._next_at

        if not due:
            return

        hold = self._hold_remaining(rotation)
        if hold > 0:
            with self._lock:
                self._next_at = time.monotonic() + hold
            return

        if self.worker.busy:
            # A sweep takes ~50s. Never stack rotation on top of live work.
            with self._lock:
                self._next_at = time.monotonic() + TICK
            return

        self.play_next()


# How long to leave a cut alone before sending it again. A cut that did not
# take -- controllers out of range, a write that failed -- must not be left at
# one attempt, because the battery does not care that we tried. Slow enough not
# to hammer the shared radio, fast enough to matter.
RECUT_SECONDS = 300.0


class BatteryGuard:
    """Turn the sign off before it flattens the battery.

    The lights do not switch themselves off, and neither does anyone at four in
    the morning. Left lit overnight they took this sign's battery below the
    voltage its solar controller will begin charging at, which is worse than a
    dark sign: it is a sign that cannot come back the next day without mains
    power, and on the playa there is none.

    So the sign gets a runtime budget. Counted on ``time.monotonic`` because
    the Pi has no RTC and comes up on the playa knowing nothing about the date
    -- a wall-clock "off at 3am" cannot be relied on there, and this can.

    What it cannot do is save a battery once the Pi is browning out on that
    same battery. The real cutoff is the charge controller's own load output,
    which disconnects in hardware with nothing running. This is the layer above
    that: it stops the ordinary case of nobody remembering.
    """

    def __init__(self, store, worker, panel=None):
        self.store = store
        self.worker = worker
        # The text panel is on the same battery, so "off" has to include it.
        self.panel = panel
        self._lock = threading.Lock()
        self._lit_since = None        # monotonic, or None when the sign is dark
        self._tripped_at = None       # wall clock, for the UI
        self._cut_at = None           # monotonic: when the last cut was sent
        # The trip, remembered PAST the moment the sign goes dark. _tripped_at
        # has to clear once the sign is seen dark -- a morning relight would
        # otherwise be re-cut five minutes later -- but the person walking up
        # the next day still deserves "the guard cut this" on the screen, not
        # "sign is dark". Cleared when the lights come back on, or on re-arm.
        self._last_trip = None
        self._warned = False

    def lit(self) -> bool:
        """Is anything believed to be on?

        Read from what the worker recorded after each successful write rather
        than from what was asked for: a device that never answered is not
        drawing anything, and counting it would spend the budget on lights that
        are not lit.
        """
        for state in self.worker.device_state.values():
            if (state.get("showing") or {}).get("power"):
                return True
        return False

    def status(self) -> dict:
        battery = self.store.battery()
        with self._lock:
            lit_since, warned = self._lit_since, self._warned
            tripped = self._tripped_at or self._last_trip
        remaining = None
        if lit_since is not None:
            spent = time.monotonic() - lit_since
            remaining = max(0, int(battery["run_minutes"] * 60 - spent))
        return {
            "enabled": bool(battery["enabled"]),
            "run_minutes": battery["run_minutes"],
            "warn_minutes": battery["warn_minutes"],
            "include_panel": bool(battery["include_panel"]),
            "lit": lit_since is not None,
            "seconds_left": remaining,
            "warning": bool(warned and remaining),
            "tripped_at": tripped,
        }

    def rearm(self) -> dict:
        """Start the budget again, for someone who wants the rest of the night."""
        with self._lock:
            self._lit_since = time.monotonic() if self.lit() else None
            self._tripped_at = None
            self._last_trip = None
            self._cut_at = None
            self._warned = False
        log.info("battery guard re-armed")
        return self.status()

    def tick(self):
        battery = self.store.battery()
        if not battery["enabled"]:
            with self._lock:
                self._lit_since = None
                self._warned = False
            return
        lit = self.lit()
        now = time.monotonic()
        cut = False
        with self._lock:
            if not lit:
                # Dark: nothing is being spent, and the re-cut machinery can
                # stand down -- but the memory of the trip stays (see above).
                self._lit_since = None
                self._warned = False
                self._tripped_at = None
                self._cut_at = None
            elif self._tripped_at is not None:
                # Already cut, and the sign still reads as lit. Usually that is
                # just the off still being written -- seen in a live run as a
                # second "counting" line five seconds after the first cut, which
                # would have started a whole fresh budget. So the clock does not
                # restart until the sign is actually seen dark.
                #
                # But a cut that did not take is not a cut, and the battery does
                # not care that we tried, so it goes again on a slow beat.
                if now - (self._cut_at or 0) >= RECUT_SECONDS:
                    self._cut_at = now
                    cut = True
            elif self._lit_since is None:
                self._lit_since = now
                # Someone lit the sign on purpose; the old trip is history.
                self._last_trip = None
                log.info("battery guard: counting %.0f minutes of light",
                         battery["run_minutes"])
            else:
                left = battery["run_minutes"] * 60.0 - (now - self._lit_since)
                warn_at = battery["warn_minutes"] * 60.0
                if left > 0:
                    if warn_at and left <= warn_at and not self._warned:
                        self._warned = True
                        log.warning("battery guard: lights out in %.0f minutes",
                                    left / 60.0)
                else:
                    self._lit_since = None
                    self._tripped_at = time.time()
                    self._last_trip = self._tripped_at
                    self._cut_at = now
                    self._warned = False
                    cut = True
        if cut:
            self._cut(battery)

    def _cut(self, battery: dict):
        """Everything off, and nothing left running that would turn it back on."""
        log.warning("battery guard: %.0f minutes spent -- turning the sign off",
                    battery["run_minutes"])
        try:
            if self.store.rotation().get("enabled"):
                # Otherwise the next rotation tick lights it all again a few
                # minutes later and the budget buys nothing.
                self.store.update_rotation({"enabled": False})
                log.info("battery guard: rotation stopped")
            self.worker.submit_state("all", {"power": False},
                                     label="battery guard: lights out")
            if battery.get("include_panel") and self.panel is not None:
                self.panel.clear()
                self.panel.power(False)
                log.info("battery guard: panel off as well")
        except Exception:
            log.exception("battery guard could not turn the lights off")


class TemperatureSampler:
    """Reads the DHT on its own slow thread and hands out the last reading.

    The sensor read blocks for up to ten seconds with retries, which is fine
    for a thermometer and fatal for the scheduler thread that paces the radio
    -- so it gets a thread of its own. Everything else asks ``current()``,
    which returns the last reading or None, never blocks, and never hands back
    a value old enough to mislead (``Reading.stale``).

    Off unless the config turns it on: no sensor, no thread, no log noise.
    """

    def __init__(self, store):
        self.store = store
        self._thread = None
        self._stop = threading.Event()
        self._probe = None
        self._reading = None
        self._lock = threading.Lock()

    def _config(self):
        return self.store.temperature()

    def start(self):
        if self._thread or not self._config().get("enabled"):
            return
        self._thread = threading.Thread(target=self._run, name="temperature",
                                        daemon=True)
        self._thread.start()
        log.info("temperature sampler started")

    def stop(self):
        self._stop.set()

    def current(self):
        """The last reading, or None if there is none or it has gone stale."""
        with self._lock:
            reading = self._reading
        if reading is None or reading.stale():
            return None
        return reading

    def _run(self):
        while not self._stop.is_set():
            config = self._config()
            if not config.get("enabled"):
                # Turned off from the UI while running: drop the sensor and
                # idle. A later on-tick starts a fresh thread.
                return
            if self._probe is None:
                self._probe = Thermometer(pin=config.get("pin", 13),
                                          model=config.get("model", "DHT11"))
            reading = self._probe.read()
            if reading is not None:
                with self._lock:
                    self._reading = reading
            interval = max(60.0, float(config.get("interval_minutes", 20.0)) * 60.0)
            self._stop.wait(interval)


class Scheduler:
    def __init__(self, store, worker, timekeeper):
        self.store = store
        self.worker = worker
        self.timekeeper = timekeeper
        self.rotation = Rotation(store, worker)
        # The temperature the schedule shows, sampled on its own thread so a
        # ten-second sensor read never stalls the radio pacing here.
        self.temperature = TemperatureSampler(store)
        # What the panel should say, built from the clock, the calendar and
        # that sampler. It only decides text; the runner still does the
        # sending and cycling.
        self.schedule = Schedule(timekeeper, temperature=self.temperature.current)
        # The text panel cycles on this same thread. It is a different device
        # on a different protocol, but it is the same "wait, then send one
        # thing" shape, and giving it its own thread would only add a second
        # writer racing for the one radio.
        self.panel = MatrixRunner(store, worker, schedule=self.schedule)
        # Same thread again, and for the same reason: one writer for one radio.
        self.battery = BatteryGuard(store, worker, panel=self.panel)
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._timers = []
        self.last_tick = None

    # ---------------------------------------------------------------- control

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()
        self.temperature.start()
        log.info("scheduler started (tick %.0fs)", TICK)

    def stop(self):
        self._stop.set()
        self.temperature.stop()

    def _run(self):
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")

    # ------------------------------------------------------------------- tick

    def tick(self):
        self.last_tick = time.time()
        self._fire_timers()
        try:
            self.rotation.tick()
        except Exception:
            log.exception("rotation tick failed")
        try:
            self.panel.tick()
        except Exception:
            log.exception("panel tick failed")
        try:
            self.battery.tick()
        except Exception:
            log.exception("battery guard tick failed")
        if not self.timekeeper.clock_ok():
            return
        self._fire_schedules()

    def _fire_schedules(self):
        now = self.timekeeper.now()
        key = now.strftime("%Y-%m-%d %H:%M")
        for schedule in self.store.schedules():
            if not schedule.get("enabled", True):
                continue
            days = schedule.get("days") or []
            if days and now.weekday() not in days:
                continue
            hour, minute = (int(part) for part in schedule["time"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (now - due).total_seconds()
            if not (0 <= delta <= GRACE_SECONDS):
                continue
            if schedule.get("last_fired") == key:
                continue
            scene = self.store.scene(schedule.get("scene", ""))
            self.store.mark_schedule_fired(schedule["id"], key)
            if not scene:
                log.error("schedule %s points at missing scene '%s'",
                          schedule["id"], schedule.get("scene"))
                continue
            log.info("schedule '%s' firing scene '%s'",
                     schedule.get("name") or schedule["id"], scene["name"])
            self.worker.submit_scene(scene)

    def _fire_timers(self):
        now = time.monotonic()
        due = []
        with self._lock:
            remaining = []
            for timer in self._timers:
                if timer["deadline"] <= now:
                    due.append(timer)
                else:
                    remaining.append(timer)
            self._timers = remaining
        for timer in due:
            scene = self.store.scene(timer["scene"])
            if not scene:
                log.error("timer %s points at missing scene '%s'", timer["id"], timer["scene"])
                continue
            log.info("timer '%s' firing scene '%s'", timer["id"], scene["name"])
            self.worker.submit_scene(scene)

    # ----------------------------------------------------------------- timers

    def add_timer(self, scene_name: str, minutes: float) -> dict:
        scene = self.store.scene(scene_name)
        if not scene:
            raise ValueError("unknown scene: %s" % scene_name)
        minutes = float(minutes)
        if minutes <= 0:
            raise ValueError("minutes must be > 0")
        timer = {
            "id": new_id(),
            "scene": scene["name"],
            "minutes": minutes,
            "deadline": time.monotonic() + minutes * 60.0,
            "created_epoch": time.time(),
        }
        with self._lock:
            self._timers.append(timer)
        log.info("timer %s: scene '%s' in %.1f min", timer["id"], scene["name"], minutes)
        return self._public_timer(timer)

    def cancel_timer(self, timer_id: str) -> bool:
        with self._lock:
            before = len(self._timers)
            self._timers = [t for t in self._timers if t["id"] != timer_id]
            return len(self._timers) != before

    def timers(self) -> list:
        now = time.monotonic()
        with self._lock:
            return [self._public_timer(t, now) for t in sorted(self._timers,
                                                               key=lambda t: t["deadline"])]

    @staticmethod
    def _public_timer(timer, now=None) -> dict:
        now = time.monotonic() if now is None else now
        return {
            "id": timer["id"],
            "scene": timer["scene"],
            "minutes": timer["minutes"],
            "remaining_seconds": max(0, int(timer["deadline"] - now)),
        }

    # ------------------------------------------------------------------- info

    def next_runs(self, limit: int = 5) -> list:
        """Human-readable 'what fires next', for the UI."""
        if not self.timekeeper.clock_ok():
            return []
        now = self.timekeeper.now()
        upcoming = []
        for schedule in self.store.schedules():
            if not schedule.get("enabled", True):
                continue
            hour, minute = (int(part) for part in schedule["time"].split(":"))
            days = schedule.get("days") or list(range(7))
            for ahead in range(0, 8):
                candidate = (now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                             + _days(ahead))
                if candidate <= now or candidate.weekday() not in days:
                    continue
                upcoming.append({
                    "id": schedule["id"],
                    "name": schedule.get("name") or schedule["scene"],
                    "scene": schedule["scene"],
                    "when": candidate.isoformat(timespec="minutes"),
                    "in_seconds": int((candidate - now).total_seconds()),
                })
                break
        upcoming.sort(key=lambda entry: entry["in_seconds"])
        return upcoming[:limit]


def _days(count):
    import datetime as dt
    return dt.timedelta(days=count)
