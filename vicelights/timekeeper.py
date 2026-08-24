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

import calendar
import datetime as dt
import fcntl
import logging
import os
import struct
import subprocess
import threading
import time

log = logging.getLogger("vicelights.time")

# Anything before this means "the clock was never set on this boot".
SANE_AFTER = dt.datetime(2024, 1, 1)

# A battery-backed clock, if scripts/setup_rtc.sh has been run on a module
# wired to the I2C pins. Everything here still works without one -- the RTC
# just makes the stamp-restore dance unnecessary instead of load-bearing.
RTC_DEVICE = "/dev/rtc0"

# Talk to it through the kernel directly rather than through hwclock(8).
# Debian Trixie moved that binary into util-linux-extra, which is not
# installed on this sign -- and the discovery that it was missing came from a
# playa-bound Pi that could not have apt-getted it anyway. These are the
# kernel's own RTC ioctls, derived from _IOR('p', 0x09, struct rtc_time) and
# _IOW('p', 0x0a, ...); struct rtc_time is nine ints, C conventions
# throughout (month 0-11, year since 1900, Sunday=0).
RTC_RD_TIME = 0x80247009
RTC_SET_TIME = 0x4024700A
_RTC_STRUCT = "9i"

# The kernel reads the RTC as UTC when it sets the system clock at boot, and
# hwclock writes UTC by default, so anything we write has to be UTC too --
# a local-time RTC would come back hours out on the next cold boot.


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

    @staticmethod
    def rtc_present() -> bool:
        return os.path.exists(RTC_DEVICE)

    @staticmethod
    def read_rtc():
        """What the battery-backed clock thinks the time is, or None."""
        if not os.path.exists(RTC_DEVICE):
            return None
        try:
            handle = os.open(RTC_DEVICE, os.O_RDONLY)
        except OSError as exc:
            log.debug("cannot open %s: %s", RTC_DEVICE, exc)
            return None
        try:
            buffer = bytearray(struct.calcsize(_RTC_STRUCT))
            fcntl.ioctl(handle, RTC_RD_TIME, buffer, True)
        except OSError as exc:
            log.debug("RTC_RD_TIME failed: %s", exc)
            return None
        finally:
            os.close(handle)
        sec, minute, hour, mday, mon, year = struct.unpack(
            _RTC_STRUCT, bytes(buffer))[:6]
        try:
            # timegm, not mktime: the RTC holds UTC.
            return calendar.timegm((year + 1900, mon + 1, mday,
                                    hour, minute, sec, 0, 1, -1))
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _tz_name() -> str:
        """The abbreviation that matches the offset actually in force.

        time.tzname is a pair -- ("PST", "PDT") -- and index 0 is the standard
        one whatever the date. Reporting "PST" through the summer put a label
        an hour off the clock beside it, which is the sort of small lie that
        wastes an hour of someone's night when a schedule fires late.
        """
        if not time.tzname:
            return ""
        local = time.localtime()
        if local.tm_isdst > 0 and len(time.tzname) > 1 and time.tzname[1]:
            return time.tzname[1]
        return time.tzname[0]

    def info(self) -> dict:
        now = dt.datetime.now()
        return {
            "now": now.isoformat(timespec="seconds"),
            "epoch": time.time(),
            "clock_ok": self.clock_ok(),
            "weekday": now.weekday(),
            "tz": self._tz_name(),
            "source": self.last_set_source,
            "rtc": self.rtc_present(),
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
        """Pull the clock forward to the last known good time, if needed.

        The hardware clock goes first when there is one: it is the only source
        here that survives a power cut knowing the real time, where the stamp
        on disk only knows when the sign was last alive. Both are forward-only
        and the later one wins, so a stale RTC cannot drag the clock back.
        """
        self._restore_from_rtc()
        stamp = self._read_stamp()
        if stamp is None:
            log.info("no saved clock stamp; time is unknown until you set it")
            return False
        if time.time() >= stamp:
            if self.rtc_present() and self.clock_ok():
                # The kernel (and the service's own hwclock -s at start) set
                # the system clock from the hardware one; say so rather than
                # leaving the source blank as if nobody knew the time.
                self.last_set_source = self.last_set_source or "hardware clock"
            log.info("system clock (%s) is at or ahead of saved stamp",
                     dt.datetime.now().isoformat(timespec="seconds"))
            return False
        restored = dt.datetime.fromtimestamp(stamp)
        if self._apply_system_time(restored):
            self.last_set_source = "restored from disk"
            log.warning("clock was behind; restored to %s", restored.isoformat(timespec="seconds"))
            return True
        return False

    def sync_from_rtc(self) -> bool:
        """Set the system clock from the hardware one. Public for setup_rtc.sh."""
        return self._restore_from_rtc()

    def _restore_from_rtc(self) -> bool:
        rtc = self.read_rtc()
        if rtc is None:
            return False
        moment = dt.datetime.fromtimestamp(rtc)
        if moment < SANE_AFTER:
            log.info("hardware clock reads %s -- never set; ignoring it",
                     moment.isoformat(timespec="seconds"))
            return False
        # A minute of slack: the kernel usually sets the system clock from the
        # RTC at boot already, and re-applying a near-identical time every
        # start would be noise in the log for nothing.
        if time.time() >= rtc - 60:
            self.last_set_source = self.last_set_source or "hardware clock"
            return False
        if self._apply_system_time(moment):
            self.last_set_source = "hardware clock"
            log.warning("clock set from the hardware clock: %s",
                        moment.isoformat(timespec="seconds"))
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
        # A time someone bothered to set belongs in the hardware clock, or it
        # dies with the next power cut and the RTC keeps serving whatever it
        # held before. Nothing auto-syncs system -> RTC on a box with no NTP.
        self.write_rtc()
        log.warning("system clock set to %s (%s)", target.isoformat(timespec="seconds"), source)
        return self.info()

    def write_rtc(self, epoch: float = None) -> bool:
        """Push the system time into the battery-backed clock."""
        if not self.rtc_present():
            return False
        moment = time.gmtime(time.time() if epoch is None else epoch)
        payload = struct.pack(
            _RTC_STRUCT, moment.tm_sec, moment.tm_min, moment.tm_hour,
            moment.tm_mday, moment.tm_mon - 1, moment.tm_year - 1900,
            # C counts weekdays from Sunday and days-of-year from zero;
            # Python does neither.
            (moment.tm_wday + 1) % 7, moment.tm_yday - 1, 0)
        try:
            handle = os.open(RTC_DEVICE, os.O_RDONLY)
        except OSError as exc:
            log.warning("could not open %s to set it: %s", RTC_DEVICE, exc)
            return False
        try:
            fcntl.ioctl(handle, RTC_SET_TIME, payload)
        except OSError as exc:
            log.warning("could not set the hardware clock: %s", exc)
            return False
        finally:
            os.close(handle)
        log.info("hardware clock set to %s UTC",
                 time.strftime("%Y-%m-%d %H:%M:%S", moment))
        return True

    # Kept under the old name for callers that predate the ioctl path.
    _write_rtc = write_rtc

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
