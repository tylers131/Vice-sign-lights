"""Wall-clock handling for a Pi with no RTC and no NTP.

The Zero W forgets the time on every cold boot and the playa AP has no uplink,
so we cannot assume the clock is right.  Strategy:

1. Persist the current time to disk every minute (a mini fake-hwclock).
2. On start, if the system clock is older than the stored stamp, push it
   forward.  Time then only ever moves forward, and after a reboot you are at
   worst off by the downtime.
3. Expose ``set_time`` so the web UI can sync the clock from the phone that is
   already connected -- one tap, no NTP.
4. Report ``clock_ok`` so the scheduler can refuse to run wall-clock schedules
   against a clock that is obviously wrong (and the UI can shout about it).

Relative timers do not need any of this: they run off ``time.monotonic``.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("vicelights.time")

# Anything before this means "the clock was never set on this boot".
SANE_AFTER = dt.datetime(2024, 1, 1)


class TimeKeeper:
    def __init__(self, state_path: str, persist_interval: float = 60.0):
        self.state_path = os.path.abspath(state_path)
        self.persist_interval = persist_interval
        self._thread = None
        self._stop = threading.Event()
        self.last_set_source = ""

    # ------------------------------------------------------------------ query

    @staticmethod
    def now() -> dt.datetime:
        return dt.datetime.now()

    @staticmethod
    def clock_ok() -> bool:
        return dt.datetime.now() >= SANE_AFTER

    def info(self) -> dict:
        now = dt.datetime.now()
        return {
            "now": now.isoformat(timespec="seconds"),
            "epoch": time.time(),
            "clock_ok": self.clock_ok(),
            "weekday": now.weekday(),
            "tz": time.tzname[0] if time.tzname else "",
            "source": self.last_set_source,
            "can_set": os.geteuid() == 0,
        }

    # ---------------------------------------------------------------- persist

    def _read_stamp(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                return float(handle.read().strip())
        except Exception:
            return None

    def _write_stamp(self):
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write("%.0f" % time.time())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        except Exception:
            log.debug("could not persist clock stamp", exc_info=True)

    def restore(self):
        """Pull the clock forward to the last known good time, if needed."""
        stamp = self._read_stamp()
        if stamp is None:
            log.info("no saved clock stamp; time is unknown until you set it")
            return False
        if time.time() >= stamp:
            log.info("system clock (%s) is at or ahead of saved stamp",
                     dt.datetime.now().isoformat(timespec="seconds"))
            return False
        restored = dt.datetime.fromtimestamp(stamp)
        if self._apply_system_time(restored):
            self.last_set_source = "restored from disk"
            log.warning("clock was behind; restored to %s", restored.isoformat(timespec="seconds"))
            return True
        return False

    # -------------------------------------------------------------------- set

    def set_time(self, value, source: str = "web ui") -> dict:
        """Set the system clock.  ``value`` is an ISO string or epoch seconds."""
        if isinstance(value, (int, float)):
            target = dt.datetime.fromtimestamp(float(value))
        else:
            text = str(value).strip().replace("Z", "")
            target = dt.datetime.fromisoformat(text)
        if not self._apply_system_time(target):
            raise RuntimeError("could not set system clock (need root)")
        self.last_set_source = source
        self._write_stamp()
        log.warning("system clock set to %s (%s)", target.isoformat(timespec="seconds"), source)
        return self.info()

    @staticmethod
    def _apply_system_time(target: dt.datetime) -> bool:
        stamp = target.strftime("%Y-%m-%d %H:%M:%S")
        try:
            subprocess.run(["date", "-s", stamp], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
            return True
        except Exception as exc:
            log.error("date -s failed: %s", exc)
            return False

    # ------------------------------------------------------------------ thread

    def start(self):
        if self._thread:
            return
        self.restore()
        self._thread = threading.Thread(target=self._run, name="timekeeper", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._write_stamp()

    def _run(self):
        while not self._stop.wait(self.persist_interval):
            if self.clock_ok():
                self._write_stamp()
