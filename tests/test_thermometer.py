"""Tests for the DHT read.

Every one of these runs with no sensor attached and no sleeping: the
thermometer takes an injectable reader and an injectable sleep, so the retry
paths that would take ten seconds on hardware take none here.

    python3 -m unittest discover -s tests -v
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vicelights.thermometer import (            # noqa: E402
    DEFAULT_STALE_AFTER, MODELS, Reading, Thermometer, _hint_for)

logging.disable(logging.CRITICAL)               # the tests speak for themselves


class Reader:
    """A scripted stand-in for adafruit_dht.

    Each item is either a ``(celsius, humidity)`` pair to return, an exception
    to raise, or None -- the three things the real driver does. The script
    repeats once exhausted, so "always fails" is a one-item script.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def __call__(self):
        item = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    """Records what we were asked to sleep instead of sleeping."""

    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)

    @property
    def total(self):
        return sum(self.slept)


def probe(*script, **kwargs):
    kwargs.setdefault("sleep", Clock())
    return Thermometer(reader=Reader(*script), **kwargs)


class GoodReads(unittest.TestCase):
    def test_every_read_succeeds(self):
        t = probe((21.0, 40.0))
        reading = t.read()
        self.assertEqual(reading.celsius, 21.0)
        self.assertEqual(reading.humidity, 40.0)

    def test_stops_early_once_it_has_enough(self):
        # No reason to spend the full five attempts when three agree.
        t = probe((21.0, 40.0), enough=3, attempts=5)
        t.read()
        self.assertEqual(t._reader.calls, 3)

    def test_a_good_run_does_not_sleep_between_every_attempt(self):
        clock = Clock()
        t = Thermometer(reader=Reader((21.0, 40.0)), sleep=clock,
                        enough=3, retry_delay=2.0)
        t.read()
        # Two gaps for three reads, not three: no trailing wait.
        self.assertEqual(clock.slept, [2.0, 2.0])

    def test_fahrenheit(self):
        self.assertEqual(Reading(0.0).fahrenheit, 32.0)
        self.assertEqual(Reading(100.0).fahrenheit, 212.0)
        self.assertEqual(Reading(21.0).fahrenheit, 69.8)


class Median(unittest.TestCase):
    def test_the_median_is_actually_taken(self):
        # The middle value, not the first and not the mean.
        t = probe((20.0, 40.0), (26.0, 40.0), (21.0, 40.0), enough=3)
        self.assertEqual(t.read().celsius, 21.0)

    def test_dither_between_two_integers_is_flattened(self):
        # What a DHT11 actually does: whole degrees, wobbling.
        t = probe((21.0, 40.0), (22.0, 41.0), (21.0, 40.0), enough=3)
        reading = t.read()
        self.assertEqual(reading.celsius, 21.0)
        self.assertEqual(reading.humidity, 40.0)

    def test_one_bad_frame_is_outvoted(self):
        # A corrupt frame that passed its checksum by luck.
        t = probe((21.0, 40.0), (85.0, 40.0), (21.0, 40.0), enough=3)
        self.assertEqual(t.read().celsius, 21.0)

    def test_median_of_two_averages_them(self):
        t = probe((20.0, 40.0), (21.0, 42.0), RuntimeError("checksum"),
                  attempts=3, enough=3)
        reading = t.read()
        self.assertEqual(reading.celsius, 20.5)
        self.assertEqual(reading.samples, 2)

    def test_humidity_is_medianed_independently_of_temperature(self):
        # A frame can carry a temperature and no humidity.
        t = probe((21.0, None), (22.0, 50.0), (23.0, 60.0), enough=3)
        reading = t.read()
        self.assertEqual(reading.celsius, 22.0)
        self.assertEqual(reading.humidity, 55.0)

    def test_samples_reports_how_many_reads_survived(self):
        t = probe((21.0, 40.0), enough=3)
        self.assertEqual(t.read().samples, 3)


class BadReads(unittest.TestCase):
    """The two shapes of failure, and the mix that is an ordinary day."""

    def test_every_read_raises(self):
        t = probe(RuntimeError("Checksum did not validate"), attempts=5)
        self.assertIsNone(t.read())
        self.assertEqual(t._reader.calls, 5)

    def test_every_read_returns_none(self):
        # adafruit_dht returns None rather than raising on some bad frames.
        t = probe(None, attempts=5)
        self.assertIsNone(t.read())
        self.assertEqual(t._reader.calls, 5)

    def test_a_none_temperature_inside_a_pair(self):
        t = probe((None, 40.0), attempts=4)
        self.assertIsNone(t.read())
        self.assertEqual(t._reader.calls, 4)

    def test_a_mix_of_failures_and_successes(self):
        t = probe(RuntimeError("checksum"), (21.0, 40.0), None,
                  (22.0, 41.0), (21.0, 40.0), attempts=5, enough=3)
        reading = t.read()
        self.assertEqual(reading.celsius, 21.0)
        self.assertEqual(reading.samples, 3)

    def test_one_good_read_is_still_a_reading(self):
        t = probe(RuntimeError("x"), RuntimeError("x"), (19.0, 30.0),
                  RuntimeError("x"), RuntimeError("x"), attempts=5)
        reading = t.read()
        self.assertEqual(reading.celsius, 19.0)
        # Flagged as a single sample: nothing outvoted a bad frame here.
        self.assertEqual(reading.samples, 1)

    def test_retries_wait_between_attempts(self):
        clock = Clock()
        t = Thermometer(reader=Reader(RuntimeError("x")), sleep=clock,
                        attempts=5, retry_delay=2.0)
        t.read()
        # Four gaps for five attempts, ~8s on hardware and none here.
        self.assertEqual(clock.slept, [2.0, 2.0, 2.0, 2.0])

    def test_a_failed_read_does_not_disturb_the_previous_one(self):
        t = probe((21.0, 40.0), enough=1)
        good = t.read()
        t._reader = Reader(RuntimeError("checksum"))
        self.assertIsNone(t.read())
        # Still available to the display, still marked as the earlier read.
        self.assertIs(t.last, good)


class NeverStale(unittest.TestCase):
    def test_read_never_returns_the_previous_value(self):
        t = probe((21.0, 40.0), enough=1)
        t.read()
        t._reader = Reader(None)
        self.assertIsNone(t.read(), "a failed read must not pass off the last "
                                    "value as current")

    def test_last_is_none_before_any_read(self):
        self.assertIsNone(probe((21.0, 40.0)).last)

    def test_age_and_staleness(self):
        import time
        fresh = Reading(21.0, at=time.monotonic())
        self.assertLess(fresh.age(), 1.0)
        self.assertFalse(fresh.stale())

        old = Reading(21.0, at=time.monotonic() - (DEFAULT_STALE_AFTER + 1))
        self.assertGreater(old.age(), DEFAULT_STALE_AFTER)
        self.assertTrue(old.stale())

    def test_age_is_monotonic_so_setting_the_clock_cannot_break_it(self):
        # The wall clock on this sign gets set by hand at arbitrary moments;
        # an age computed from it could come out negative or hours wrong.
        reading = Reading(21.0, taken=0.0)
        self.assertLess(reading.age(), 1.0)


class NeverCrashes(unittest.TestCase):
    def test_a_reader_that_explodes_returns_none(self):
        def boom():
            raise OSError("the pin is not wired to anything")
        t = Thermometer(reader=boom, sleep=Clock(), attempts=2)
        self.assertIsNone(t.read())

    def test_a_reader_returning_nonsense_returns_none(self):
        t = probe("hello", attempts=2)
        self.assertIsNone(t.read())

    def test_no_driver_installed_returns_none(self):
        # No reader injected and no adafruit_dht importable: the real path on
        # any machine that is not the Pi, including this one.
        t = Thermometer(sleep=Clock())
        self.assertIsNone(t.read())

    def test_close_is_safe_before_any_read(self):
        probe((21.0, 40.0)).close()          # must not raise

    def test_close_survives_a_device_that_objects(self):
        class Grumpy:
            def exit(self):
                raise RuntimeError("already released")
        t = probe((21.0, 40.0))
        t._device = Grumpy()
        t.close()
        self.assertIsNone(t._device)


class Complaints(unittest.TestCase):
    """A dead sensor says so once, not twice an hour until the burn ends."""

    def setUp(self):
        self.said = []
        logging.disable(logging.NOTSET)
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.said.append(
            (record.levelno, record.getMessage()))
        self.log = logging.getLogger("vicelights.temp")
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.DEBUG)

    def tearDown(self):
        self.log.removeHandler(self.handler)
        logging.disable(logging.CRITICAL)

    def warnings(self):
        return [m for level, m in self.said if level >= logging.WARNING]

    def test_the_same_fault_warns_once(self):
        t = probe(RuntimeError("checksum"), attempts=1)
        for _ in range(5):
            t.read()
        self.assertEqual(len(self.warnings()), 1)

    def test_recovering_then_failing_again_is_news(self):
        t = probe(RuntimeError("checksum"), attempts=1)
        t.read()
        t._reader = Reader((21.0, 40.0))
        t.read()
        t._reader = Reader(RuntimeError("checksum"))
        t.read()
        self.assertEqual(len(self.warnings()), 2)

    def test_the_rpi_gpio_trap_gets_its_fix_attached(self):
        t = probe(RuntimeError("Cannot determine SOC peripheral base address"),
                  attempts=1)
        t.read()
        self.assertIn("rpi-lgpio", self.warnings()[0])


class Configuration(unittest.TestCase):
    def test_the_pin_is_configurable(self):
        self.assertEqual(Thermometer().pin, 13)          # BCM 13, physical 33
        self.assertEqual(Thermometer(pin=4).pin, 4)

    def test_am2302_is_a_dht22(self):
        self.assertEqual(MODELS["AM2302"], "DHT22")
        self.assertEqual(MODELS["DHT22"], "DHT22")
        self.assertEqual(MODELS["DHT11"], "DHT11")

    def test_model_is_case_insensitive(self):
        self.assertEqual(Thermometer(model="dht22").model, "DHT22")

    def test_enough_cannot_exceed_attempts(self):
        # Otherwise the early-stop never fires and every call is worst case.
        t = Thermometer(attempts=2, enough=5)
        self.assertEqual(t.enough, 2)

    def test_at_least_one_attempt(self):
        self.assertEqual(Thermometer(attempts=0).attempts, 1)

    def test_hint_lookup_returns_none_for_unknown_faults(self):
        self.assertIsNone(_hint_for("something nobody has seen before"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
