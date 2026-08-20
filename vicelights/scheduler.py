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

from .config import new_id

log = logging.getLogger("vicelights.scheduler")

TICK = 5.0
# How far past its minute a schedule may still fire.  Keeps a schedule from
# retroactively firing hours later when the clock is jumped forward.
GRACE_SECONDS = 90

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class Scheduler:
    def __init__(self, store, worker, timekeeper):
        self.store = store
        self.worker = worker
        self.timekeeper = timekeeper
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
        log.info("scheduler started (tick %.0fs)", TICK)

    def stop(self):
        self._stop.set()

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
