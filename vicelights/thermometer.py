"""A DHT11/DHT22 temperature read, on demand.

Deliberately small: one blocking call that returns the current temperature or
``None``.  No thread, no history, no logging of values -- the caller asks a
few times an hour and puts the number on the panel.

The whole design is shaped by one fact: **failed reads are routine**.  The DHT
protocol is bit-banged microsecond timing on a single wire, and on a
non-realtime kernel any scheduler hiccup mid-frame corrupts the checksum.
Losing a fifth of reads is an ordinary day.

That is a footnote at one sample every five seconds and the entire problem at
two samples an hour: a single unlucky read means a blank panel for half an
hour.  So one call to :meth:`Thermometer.read` retries internally, and returns
the *median* of the reads that worked rather than the first:

* DHT11 resolution is whole degrees Celsius, so consecutive reads dither
  between two integers.  A median of three flattens that.
* It also throws out a single bad frame that passed its checksum by luck.

Retries are ~2s apart because the DHT11's own sampling period is 1-2s -- ask
faster and you get the same stale frame back, or another failure.

What this module will not do is hand you a stale number dressed up as a fresh
one.  A failed read returns ``None`` and the display decides what to show: the
previous value marked as old, or dashes.  :attr:`Thermometer.last` and
:meth:`Reading.age` exist for exactly that, and are the only way to get a
reading this call did not take.
"""

from __future__ import annotations

import logging
import statistics
import time

log = logging.getLogger("vicelights.temp")

# BCM numbering. 13 is physical pin 33.
DEFAULT_PIN = 13

# DHT11 is the cheap blue one, whole degrees. DHT22/AM2302 is the white one,
# one decimal place and a wider range; same three wires and the same driver.
# AM2302 is a DHT22 in a nicer housing -- adafruit_dht has no separate class.
MODELS = {"DHT11": "DHT11", "DHT22": "DHT22", "AM2302": "DHT22"}

# Five tries about two seconds apart: ~10s worst case, which is fine for a
# caller that asks twice an hour. Stop early at three good reads -- that is
# enough for a median, and it takes the common case down to ~4s.
DEFAULT_ATTEMPTS = 5
DEFAULT_RETRY_DELAY = 2.0
DEFAULT_ENOUGH = 3

# When a displayed reading stops being worth showing. Long enough to ride out
# one missed sample at a twice-an-hour cadence, short enough that a sensor
# that died stops showing a plausible number within the hour.
DEFAULT_STALE_AFTER = 45 * 60.0

# Failures worth explaining rather than just repeating. The first is the one
# everybody hits: Blinka falls back to RPi.GPIO, which pokes /dev/mem and does
# not work on current kernels.
HINTS = (
    ("SOC peripheral base address",
     "Blinka fell back to RPi.GPIO, which does not work on this kernel. Fix: "
     "pip uninstall -y RPi.GPIO && pip install rpi-lgpio"),
    ("pulseio",
     "the pulseio path needs /dev/pulseio, which stock Pi OS does not have -- "
     "the device must be built with use_pulseio=False"),
    ("No module named",
     "the driver is not installed: pip install adafruit-circuitpython-dht"),
)


def _hint_for(message: str):
    for needle, hint in HINTS:
        if needle in message:
            return hint
    return None


class Reading:
    """One temperature, and enough context for a display to judge it."""

    def __init__(self, celsius, humidity=None, samples=1, at=None, taken=None):
        self.celsius = celsius
        self.humidity = humidity
        # How many good reads went into the median. One means no dither was
        # averaged out and no bad frame could have been outvoted.
        self.samples = samples
        # Monotonic, so age survives the clock being set from the phone --
        # which on this sign happens at arbitrary moments.
        self.at = time.monotonic() if at is None else at
        # Wall clock, purely for display. None when the clock is not set.
        self.taken = time.time() if taken is None else taken

    @property
    def fahrenheit(self):
        return round(self.celsius * 9.0 / 5.0 + 32.0, 1)

    def age(self) -> float:
        """Seconds since this was measured."""
        return max(0.0, time.monotonic() - self.at)

    def stale(self, after: float = DEFAULT_STALE_AFTER) -> bool:
        return self.age() >= after

    def __repr__(self):
        return "<Reading %.1fC (%d sample%s, %.0fs old)>" % (
            self.celsius, self.samples, "" if self.samples == 1 else "s",
            self.age())


class Thermometer:
    """Reads a DHT11/DHT22 on demand.

    ``reader`` is any callable returning ``(celsius, humidity)`` or raising --
    the same contract adafruit_dht offers.  Passing one makes every path here
    testable with no hardware attached; leaving it out builds the real device
    lazily, on the first read, so this module imports fine on a machine with
    no GPIO.
    """

    def __init__(self, pin: int = DEFAULT_PIN, model: str = "DHT11",
                 attempts: int = DEFAULT_ATTEMPTS,
                 retry_delay: float = DEFAULT_RETRY_DELAY,
                 enough: int = DEFAULT_ENOUGH,
                 stale_after: float = DEFAULT_STALE_AFTER,
                 reader=None, sleep=None):
        self.pin = int(pin)
        self.model = str(model).upper()
        self.attempts = max(1, int(attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.enough = max(1, min(int(enough), self.attempts))
        self.stale_after = float(stale_after)
        self.last = None

        self._reader = reader
        self._sleep = sleep or time.sleep
        self._device = None
        # The last thing we complained about. A sensor that is unwired fails
        # every time forever; saying so once is information, saying so twice an
        # hour until the burn is over is noise in the log someone has to read
        # past when something else breaks.
        self._complaint = None

    # ------------------------------------------------------------------ read

    def read(self):
        """The current temperature, or None if the sensor would not answer.

        Never returns a previous reading. If this call could not measure
        anything, the answer is None and the display decides what to put up.
        """
        try:
            return self._read()
        except Exception as exc:                    # never take the app down
            self._complain("temperature read failed: %s" % exc)
            return None

    def _read(self):
        reader = self._reader or self._hardware_reader()
        if reader is None:
            return None

        temperatures, humidities = [], []
        last_error = None
        for attempt in range(self.attempts):
            # Sleep between attempts, not after the last one: a trailing wait
            # would add 2s to every call for nothing.
            if attempt:
                self._sleep(self.retry_delay)
            try:
                sample = reader()
            except Exception as exc:
                # adafruit_dht raises on some bad frames...
                last_error = exc
                log.debug("DHT attempt %d raised: %s", attempt + 1, exc)
                continue
            # ...and returns None on others. Both are ordinary.
            celsius, humidity = self._unpack(sample)
            if celsius is None:
                log.debug("DHT attempt %d returned no temperature", attempt + 1)
                continue
            temperatures.append(celsius)
            if humidity is not None:
                humidities.append(humidity)
            if len(temperatures) >= self.enough:
                break

        if not temperatures:
            self._complain("no good read from the %s on BCM %d in %d attempts%s"
                           % (self.model, self.pin, self.attempts,
                              ": %s" % last_error if last_error else ""))
            return None

        self._complaint = None      # it is talking again; next fault is news
        reading = Reading(
            celsius=round(statistics.median(temperatures), 1),
            humidity=round(statistics.median(humidities), 1) if humidities else None,
            samples=len(temperatures))
        self.last = reading
        return reading

    @staticmethod
    def _unpack(sample):
        """(celsius, humidity) out of whatever the reader handed back."""
        if sample is None:
            return None, None
        if isinstance(sample, (int, float)):        # a bare temperature
            return float(sample), None
        try:
            celsius, humidity = sample
        except (TypeError, ValueError):
            raise TypeError("reader returned %r, wanted (celsius, humidity)"
                            % (sample,))
        return (None if celsius is None else float(celsius),
                None if humidity is None else float(humidity))

    # -------------------------------------------------------------- hardware

    def _hardware_reader(self):
        """Build the real device, once, on first use."""
        if self._device is not None:
            return self._device_reader
        try:
            # Imported here rather than at module scope so this package
            # installs, imports and tests on a machine with no GPIO at all.
            import adafruit_dht
            import board
        except Exception as exc:
            self._complain("no DHT driver: %s" % exc)
            return None

        name = MODELS.get(self.model)
        if name is None:
            self._complain("unknown sensor model %r; expected one of %s"
                           % (self.model, ", ".join(sorted(MODELS))))
            return None
        try:
            pin = getattr(board, "D%d" % self.pin)
            # use_pulseio=False is not a preference. The pulseio path wants
            # /dev/pulseio, absent on stock Bookworm/Trixie, and its absence
            # blows up at construction rather than failing a read. This flag
            # also means no libgpiod package is needed.
            self._device = getattr(adafruit_dht, name)(pin, use_pulseio=False)
        except Exception as exc:
            self._complain("could not open the %s on BCM %d: %s"
                           % (self.model, self.pin, exc))
            self._device = None
            return None
        log.info("%s on BCM %d ready", self.model, self.pin)
        return self._device_reader

    def _device_reader(self):
        # adafruit_dht measures on property access and caches the frame, so
        # these two come from one reading of the wire, not two.
        device = self._device
        return device.temperature, device.humidity

    def close(self):
        """Release the GPIO. Safe to call twice, or having never read."""
        device, self._device = self._device, None
        if device is None:
            return
        try:
            device.exit()
        except Exception as exc:
            log.debug("releasing the DHT pin: %s", exc)

    # ----------------------------------------------------------------- noise

    def _complain(self, message: str):
        """WARN the first time, DEBUG while it keeps saying the same thing."""
        if message == self._complaint:
            log.debug("%s", message)
            return
        self._complaint = message
        hint = _hint_for(message)
        log.warning("%s%s", message, " -- %s" % hint if hint else "")
