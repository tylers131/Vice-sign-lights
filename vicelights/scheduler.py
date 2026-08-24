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
import subprocess
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


class PowerMonitor:
    """A timeline of the Pi's brownouts, kept where a screen can show it.

    The firmware latches under-voltage and throttling bits until reboot, and
    `/api/diagnostics` already reads them for a one-line "Pi power" verdict.
    That answers *whether* it happened; it cannot say *when*, or how often. A
    marginal supply on the playa browns out in gusts -- a dip when the
    compressor kicks in, a sag at 3am -- and each one risks silent corruption.

    So this samples the bits every SAMPLE_SECONDS and records the edges: the
    moment under-voltage begins, the moment it clears. The log is what the
    System page shows. It lives in memory -- the latched bits reset on reboot
    anyway, so there is nothing older worth persisting -- capped at MAX_EVENTS
    so a bad night cannot grow it without bound.
    """

    SAMPLE_SECONDS = 20.0
    MAX_EVENTS = 40

    # The sticky bits, from `vcgencmd get_throttled`.
    UNDERVOLT_NOW = 0x1
    THROTTLED_NOW = 0x4
    UNDERVOLT_EVER = 0x10000
    THROTTLED_EVER = 0x40000

    def __init__(self, timekeeper):
        self.timekeeper = timekeeper
        self._lock = threading.Lock()
        self._events = []            # newest last; {kind, at, active}
        self._active = {}            # kind -> event dict still open
        self._last_at = 0.0          # monotonic, for the sample interval
        self._bits = None            # last raw reading, None until first read
        self._available = None       # is vcgencmd even here?

    @staticmethod
    def _read_bits():
        """The throttled bitmask, or None if this is not a Pi."""
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return None
        if "=" not in out:
            return None
        try:
            return int(out.split("=", 1)[1].strip(), 0)
        except ValueError:
            return None

    def _stamp(self):
        """A human 'when' for an event: the wall clock if set, else uptime."""
        try:
            if self.timekeeper.clock_ok():
                return self.timekeeper.now().strftime("%b %-d %H:%M")
        except Exception:
            pass
        try:
            with open("/proc/uptime") as handle:
                secs = int(float(handle.read().split()[0]))
            h, m = secs // 3600, (secs % 3600) // 60
            return ("up %dh %02dm" % (h, m)) if h else ("up %dm" % m)
        except Exception:
            return "unknown"

    def _log(self, kind):
        event = {"kind": kind, "at": self._stamp(), "active": True}
        with self._lock:
            self._events.append(event)
            del self._events[:-self.MAX_EVENTS]
        return event

    def poll(self):
        """Sample on the scheduler beat; the interval gate keeps it cheap."""
        now = time.monotonic()
        if self._bits is not None and now - self._last_at < self.SAMPLE_SECONDS:
            return
        self._last_at = now
        bits = self._read_bits()
        self._available = bits is not None
        if bits is None:
            return
        for kind, mask in (("under-voltage", self.UNDERVOLT_NOW),
                           ("throttling", self.THROTTLED_NOW)):
            on = bool(bits & mask)
            open_event = self._active.get(kind)
            if on and open_event is None:
                self._active[kind] = self._log("%s began" % kind)
            elif not on and open_event is not None:
                open_event["active"] = False
                self._active.pop(kind, None)
                self._log("%s cleared" % kind)
        self._bits = bits

    def status(self) -> dict:
        bits = self._bits or 0
        with self._lock:
            events = list(reversed(self._events))   # newest first for a screen
        return {
            "available": bool(self._available),
            "undervoltage_now": bool(bits & self.UNDERVOLT_NOW),
            "throttled_now": bool(bits & self.THROTTLED_NOW),
            "undervoltage_ever": bool(bits & self.UNDERVOLT_EVER),
            "throttled_ever": bool(bits & self.THROTTLED_EVER),
            "events": events,
        }


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
        # Watches the firmware's under-voltage bits and keeps a timeline of
        # brownouts for the System page. No thread of its own -- one cheap
        # sample on the scheduler beat, gated to every 20s.
        self.power = PowerMonitor(timekeeper)
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
            self.power.poll()
        except Exception:
            log.exception("power monitor poll failed")
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
