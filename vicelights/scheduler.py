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
    """Cycle scenes all day so the sign keeps changing.

    Timed on ``time.monotonic``, never the wall clock: the Zero W forgets the
    time across a cold boot and there is no NTP on the playa, so the *pace* of
    rotation has to work from the moment the Pi powers on, knowing nothing.

    But *which* scenes it draws from can follow the time of day when the clock
    is set. Three sources, in priority order (see ``_resolve``):

    * **Coffee attract** -- while a service is on (``service_active``), the
      warm come-here scenes, so the lights pull people in at the same moment
      the panel is shouting about iced coffee.
    * **Day-part** -- the mood for this hour (chill at dawn, bold in the sun,
      party after dark), from the rotation config's ``dayparts``.
    * **Fallback** -- the plain ``playlist``, used whenever the clock is unset
      or no day-parts are configured. The sign always has a good look.

    Re-sending the current mood on every interval is also what re-wakes a
    controller whose Bluetooth dropped, so the sign heals itself as it runs.
    """

    def __init__(self, store, worker, timekeeper=None, service_active=None):
        self.store = store
        self.worker = worker
        self.timekeeper = timekeeper
        # A zero-arg callable -> bool: is a coffee service on right now? None
        # (or one that raises) simply means the attract look never triggers.
        self._service_active = service_active
        self._lock = threading.Lock()
        self._bag = []
        self._last_played = None
        self._next_at = None          # monotonic
        self._current = None
        self._active_key = None       # which mood is running (see _resolve)
        self._warned_empty = False

    # --------------------------------------------------------- time of day

    def _clock_now(self):
        """The wall clock, or None if unset or unavailable."""
        if self.timekeeper is None:
            return None
        try:
            if not self.timekeeper.clock_ok():
                return None
            return self.timekeeper.now()
        except Exception:
            return None

    def _coffee_on(self) -> bool:
        if self._service_active is None:
            return False
        try:
            return bool(self._service_active())
        except Exception:
            return False

    @staticmethod
    def _active_daypart(dayparts, now):
        """The day-part in force at ``now`` -- the latest whose start has passed.

        Day-parts arrive sorted by start time. Before the first one's start we
        have wrapped past midnight, so the last part of the day is still on
        (e.g. a Party that begins at 20:00 owns 03:00 too).
        """
        now_min = now.hour * 60 + now.minute
        chosen = None
        for part in dayparts:
            hour, _, minute = part["start"].partition(":")
            if int(hour) * 60 + int(minute) <= now_min:
                chosen = part
        return chosen or dayparts[-1]

    @staticmethod
    def _hhmm_min(hhmm) -> int:
        hour, _, minute = str(hhmm).partition(":")
        return int(hour) * 60 + int(minute)

    def _quiet_now(self, rotation) -> bool:
        """Is the sign in its nightly auto-off downtime right now?

        A wall-clock window, so it needs a set clock; with none there is no way
        to know it is night, and the sign stays lit rather than guess. The
        window may wrap midnight (off 23:00, on 06:00). Only meaningful while
        rotation is on -- rotation is what darkens the sign here and what wakes
        it -- which the caller (tick) already guarantees.
        """
        if not rotation.get("auto_off_enabled"):
            return False
        now = self._clock_now()
        if now is None:
            return False
        try:
            off = self._hhmm_min(rotation.get("auto_off_at", "00:00"))
            on = self._hhmm_min(rotation.get("auto_on_at", "06:00"))
        except (ValueError, TypeError):
            return False
        if off == on:
            return False          # a zero-length window is "never off"
        cur = now.hour * 60 + now.minute
        if off < on:
            return off <= cur < on
        return cur >= off or cur < on     # wraps past midnight

    def _resolve(self, rotation):
        """Pick the mood right now: (key, playlist names, interval minutes).

        ``key`` is a stable label for the mood -- rotation switches promptly
        when it changes (auto-off starting/ending, a day-part boundary, or
        coffee starting/ending) rather than finishing the old interval first.
        """
        # Auto-off wins over everything: a dark sign is the whole point of it.
        if self._quiet_now(rotation):
            return ("quiet", ["All off"], float(rotation["interval_minutes"]))
        attract = rotation.get("attract") or []
        if attract and self._coffee_on():
            return ("attract", attract,
                    float(rotation.get("attract_interval_minutes", 4.0)))
        dayparts = rotation.get("dayparts") or []
        if dayparts:
            now = self._clock_now()
            if now is not None:
                part = self._active_daypart(dayparts, now)
                return ("daypart:" + part["name"], part["playlist"],
                        float(part["interval_minutes"]))
        return ("base", rotation.get("playlist") or [],
                float(rotation["interval_minutes"]))

    def _resolve_scenes(self, rotation):
        """Resolve to (key, playable scene names, interval) -- never freeze.

        The mood's playlist is filtered against the scenes that exist and the
        exclude list. If that leaves nothing -- a day-part naming scenes that
        were renamed, or an exclude that swallowed the whole mood -- fall back
        to the base playlist rather than sitting on a frozen sign with no
        rotation and no self-heal. The key stays the mood's, so the boundary
        detection and the on-screen label are unaffected.
        """
        key, playlist, minutes = self._resolve(rotation)
        if key == "quiet":
            # Force the blackout scene, past the exclude that normally hides it.
            names = ["All off"] if self.store.scene("All off") else []
            return key, names, minutes
        names = self.store.rotation_scenes(playlist)
        if not names and key != "base":
            names = self.store.rotation_scenes(rotation.get("playlist") or [])
        return key, names, minutes

    @staticmethod
    def _mood_label(key):
        """Human name for a resolved key, for the UI. None for the plain list."""
        if key == "quiet":
            return "Auto off"
        if key == "attract":
            return "Coffee attract"
        if key.startswith("daypart:"):
            return key.split(":", 1)[1]
        return None

    # ------------------------------------------------------------------ state

    def reschedule(self, delay: float = None):
        """Push the next change out, e.g. after a config edit or a manual skip."""
        rotation = self.store.rotation()
        _, _, minutes = self._resolve(rotation)
        with self._lock:
            self._next_at = time.monotonic() + (
                minutes * 60.0 if delay is None else delay)

    def status(self) -> dict:
        rotation = self.store.rotation()
        key, names, minutes = self._resolve_scenes(rotation)
        with self._lock:
            next_at, current = self._next_at, self._current
        remaining = None
        if rotation["enabled"] and next_at is not None:
            remaining = max(0, int(next_at - time.monotonic()))
        return {
            "enabled": rotation["enabled"],
            "order": rotation["order"],
            # The base pace, so the editor round-trips it. The mood in force may
            # move faster or slower -- that is active_interval_minutes.
            "interval_minutes": rotation["interval_minutes"],
            "active_interval_minutes": minutes,
            "hold_after_manual_minutes": rotation["hold_after_manual_minutes"],
            "playlist": rotation["playlist"],
            "exclude": rotation["exclude"],
            "avoid_repeat": rotation["avoid_repeat"],
            # The scenes actually in play right now (the active mood's set).
            "scenes": names,
            "current": current,
            "next_in_seconds": remaining,
            "holding": self._hold_remaining(rotation) > 0,
            "hold_remaining_seconds": int(self._hold_remaining(rotation)),
            # Which mood is driving the sign: "Party", "Coffee attract",
            # "Auto off", ... or null when it is just the plain playlist.
            "daypart": self._mood_label(key),
            # The nightly auto-off downtime, and whether it is dark right now.
            "auto_off_enabled": rotation["auto_off_enabled"],
            "auto_off_at": rotation["auto_off_at"],
            "auto_on_at": rotation["auto_on_at"],
            "sleeping": key == "quiet",
            # The time-of-day moods, so the phone can retune their start and pace.
            "dayparts": rotation["dayparts"],
            "attract_interval_minutes": rotation["attract_interval_minutes"],
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
        pick rather than immediately replacing it with another 50s sweep. The
        mood key is recorded too, so the first tick does not read a boundary
        crossing that never happened.
        """
        rotation = self.store.rotation()
        key, _, minutes = self._resolve(rotation)
        with self._lock:
            self._last_played = name
            self._current = name
            self._next_at = time.monotonic() + minutes * 60.0
            self._active_key = key

    def play_next(self, force: bool = False) -> str:
        """Play the next scene now (re-sending a lone one). Returns it, or None.

        A single-scene mood is still *sent* every interval, not skipped: that
        re-send is the self-heal that re-wakes a controller whose Bluetooth
        dropped, so the sign staying lit does not depend on there being two
        scenes to alternate between.
        """
        rotation = self.store.rotation()
        key, names, minutes = self._resolve_scenes(rotation)
        if not names:
            if not self._warned_empty:
                log.warning("rotation has no scenes to play (check playlist/exclude)")
                self._warned_empty = True
            return None
        self._warned_empty = False

        with self._lock:
            # A mood change reached here (a manual "next" landing right on a
            # day-part boundary) must draw from the new mood, not the old bag.
            if self._active_key is not None and key != self._active_key:
                self._bag = []
            name = self._next_name(names, rotation)
            self._last_played = name
            self._current = name
            self._next_at = time.monotonic() + minutes * 60.0
            self._active_key = key

        scene = self.store.scene(name)
        if not scene:
            log.error("rotation picked missing scene '%s'", name)
            return None
        log.info("rotation -> '%s' (%s, next in %.0f min)", name,
                 self._mood_label(key) or "playlist", minutes)
        self.worker.submit_scene(scene)
        return name

    # ------------------------------------------------------------------- tick

    def tick(self):
        rotation = self.store.rotation()
        if not rotation["enabled"]:
            with self._lock:
                self._next_at = None
                self._active_key = None
            return

        key, _, _ = self._resolve(rotation)
        with self._lock:
            if self._next_at is None:
                # Freshly enabled: change on the next tick rather than making
                # someone wait a full interval to see that it works.
                self._next_at = time.monotonic()
            elif self._active_key is not None and key != self._active_key:
                # The mood changed under us -- a day-part boundary passed, or a
                # coffee service started or ended. Switch now, drawing fresh
                # from the new set, rather than finishing the old interval.
                self._next_at = time.monotonic()
                self._bag = []
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

    def __init__(self, store, worker=None):
        self.store = store
        # The BLE worker, so a Govee scan runs on the one radio thread instead
        # of a concurrent scan of its own. None in tests / standalone use.
        self.worker = worker
        self._thread = None
        self._stop = threading.Event()
        # Poked to cut the between-reads sleep short, so a sensor change from
        # the UI is read within seconds instead of up to a whole interval later.
        self._wake = threading.Event()
        self._probe = None
        self._probe_sig = None
        self._reading = None
        self._lock = threading.Lock()

    def _config(self):
        return self.store.temperature()

    @staticmethod
    def _probe_signature(config: dict) -> tuple:
        """What, if changed, means a different sensor to build."""
        return (config.get("source", "dht"), config.get("model", "DHT11"),
                config.get("pin", 13), (config.get("address") or "").upper())

    def _build_probe(self, config: dict):
        """The reader the config asks for: the Govee beacon or the wired DHT."""
        if config.get("source") == "govee":
            from .govee import GoveeThermometer
            return GoveeThermometer(address=config.get("address") or None,
                                    scan=self._govee_scan)
        return Thermometer(pin=config.get("pin", 13),
                           model=config.get("model", "DHT11"))

    def _govee_scan(self, seconds):
        """A Govee scan run on the worker's radio thread, not a rival one.

        Submitting the scan to the worker means it is serialised with every
        light and panel write on the one adapter -- two BLE scans at once on
        BlueZ can wedge it mid-write, which is exactly the failure a direct
        scan from this thread caused. Blocks this (temperature) thread until the
        job finishes, which is what it is for.
        """
        worker = self.worker
        if worker is None:                      # standalone / tests: direct scan
            from .govee import _default_scan
            return _default_scan(seconds)
        job = worker.submit_govee_scan(seconds)
        if job is None:
            return []
        deadline = time.monotonic() + max(20.0, float(seconds) + 15.0)
        while time.monotonic() < deadline:
            if job.state in ("done", "failed", "superseded"):
                break
            if self._stop.wait(0.5):            # shutting down: give up the wait
                return []
        return list(getattr(worker, "last_govee_raw", None) or [])

    def _close_probe(self):
        probe, self._probe = self._probe, None
        if probe is None:
            return
        try:
            probe.close()
        except Exception as exc:
            log.debug("closing the temperature probe: %s", exc)

    def start(self):
        if not self._config().get("enabled"):
            return
        # A settings change reaches a sampler that is already running: wake it so
        # it re-reads with the new sensor now, not at the end of its sleep.
        self._wake.set()
        # is_alive, not just "is there a thread": a sampler turned off from the
        # UI exits its thread but leaves the (now dead) handle set, and toggling
        # the sensor back on has to be able to start a fresh one.
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="temperature",
                                        daemon=True)
        self._thread.start()
        log.info("temperature sampler started")

    def stop(self):
        self._stop.set()
        self._wake.set()            # cut any sleep short so the thread exits now

    def current(self):
        """The last reading, or None if there is none or it has gone stale."""
        with self._lock:
            reading = self._reading
        if reading is None or reading.stale():
            return None
        return reading

    def _run(self):
        try:
            self._loop()
        finally:
            # Whichever way the loop ends, leave no dead handle behind: clearing
            # it is what lets start() spin a fresh thread when the sensor is
            # switched back on, and dropping the probe frees the sensor.
            self._thread = None
            self._close_probe()
            self._probe_sig = None

    def _loop(self):
        while not self._stop.is_set():
            config = self._config()
            if not config.get("enabled"):
                # Turned off from the UI while running: drop the sensor and
                # idle. Toggling it back on starts a fresh thread.
                return
            signature = self._probe_signature(config)
            if self._probe is None or signature != self._probe_sig:
                # First read, or the sensor was changed from the UI (DHT to
                # Govee, a different address): drop the old probe and build the
                # one the config now asks for, without a restart.
                self._close_probe()
                self._probe = self._build_probe(config)
                self._probe_sig = signature
            reading = self._probe.read()
            if reading is not None:
                with self._lock:
                    self._reading = reading
            interval = max(60.0, float(config.get("interval_minutes", 20.0)) * 60.0)
            # Sleep until the interval is up OR a settings change pokes us awake,
            # whichever comes first; clear the poke so the next sleep is real.
            self._wake.wait(interval)
            self._wake.clear()


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
        # The temperature the schedule shows, sampled on its own thread so a
        # ten-second sensor read never stalls the radio pacing here. It is given
        # the worker so a Govee scan takes its turn on the one radio rather than
        # opening a second, concurrent scan that wedges the adapter mid-write.
        self.temperature = TemperatureSampler(store, worker)
        # What the panel should say, built from the clock, the calendar and
        # that sampler. It only decides text; the runner still does the
        # sending and cycling. Built before rotation because rotation borrows
        # its calendar to know when a coffee service is on.
        self.schedule = Schedule(timekeeper, temperature=self.temperature.current,
                                 coffee_overrides=store.coffee_overrides)
        # Cycle scenes all day. Time-of-day aware: it reads the clock for the
        # hour's mood and the schedule's calendar for the coffee attract look.
        self.rotation = Rotation(store, worker, timekeeper=timekeeper,
                                 service_active=self.schedule.attract_now)
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
