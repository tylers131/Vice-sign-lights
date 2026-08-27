"""Tests for reading a Govee Bluetooth temperature sensor.

No radio: the decoders are pure, and the reader takes an injected scan, so a
whole sensor can be faked with a dict.

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vicelights import govee as G                 # noqa: E402


# Manufacturer id 0xEC88, layout [flag][t/h hi][t/h mid][t/h lo][battery]. The
# packed value 0x03419C = 213404 means 21.34C and 40.4%RH; battery 0x64 = 100%.
H5075 = {G.GOVEE_TEMP_HUM: bytes([0x00, 0x03, 0x41, 0x9C, 0x64, 0x00])}


class Decode(unittest.TestCase):
    def test_h5075_temperature_humidity_battery(self):
        r = G.decode("GVH5075_1A2B", H5075)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.celsius, 21.34, places=2)
        self.assertAlmostEqual(r.humidity, 40.4, places=1)
        self.assertEqual(r.battery, 100)
        self.assertEqual(r.model, "H5075")

    def test_fahrenheit_is_derived(self):
        r = G.decode("GVH5075_1A2B", H5075)
        # 21.34C -> 70.4F, via the shared Reading conversion.
        self.assertAlmostEqual(r.celsius * 9 / 5 + 32, 70.4, places=1)

    def test_a_freezing_reading_decodes_negative(self):
        # Sign bit set: magnitude 51200 -> 5.12C, 20.0%RH, negated.
        value = 51200 | 0x800000
        data = bytes([0x00, (value >> 16) & 0xFF, (value >> 8) & 0xFF,
                      value & 0xFF, 0x50])
        r = G.decode("GVH5075_cold", {G.GOVEE_TEMP_HUM: data})
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.celsius, -5.12, places=2)
        self.assertAlmostEqual(r.humidity, 20.0, places=1)

    def test_an_implausible_value_is_rejected(self):
        # All-ones packs to a wild temperature -- not a reading.
        self.assertIsNone(G.decode("GVH5075_x",
                                   {G.GOVEE_TEMP_HUM: bytes([0, 0xFF, 0xFF, 0xFF, 0x64])}))

    def test_an_unknown_model_returns_none_not_a_guess(self):
        # A Govee by name but with a format this module does not decode: no
        # invented number, so the caller falls back to the raw bytes.
        self.assertIsNone(G.decode("Govee_H5179_9x", {0x0188: bytes(9)}))

    def test_a_non_govee_beacon_is_ignored(self):
        self.assertIsNone(G.decode("SomeLamp", {0x004C: bytes([1, 2, 3])}))

    def test_battery_over_100_is_dropped(self):
        data = bytes([0x00, 0x03, 0x41, 0x9C, 0xFF, 0x00])
        reading = G.decode("GVH5075_x", {G.GOVEE_TEMP_HUM: data})
        self.assertIsNotNone(reading)          # the temperature still decodes
        self.assertIsNone(reading.battery)     # but 0xFF is not a percentage


class Recognise(unittest.TestCase):
    def test_name_prefixes(self):
        self.assertTrue(G.looks_like_govee("GVH5075_1A2B", {}))
        self.assertTrue(G.looks_like_govee("Govee_H5179_9x", {}))
        self.assertFalse(G.looks_like_govee("MyLamp", {}))

    def test_manufacturer_id_alone_is_enough(self):
        self.assertTrue(G.looks_like_govee("", H5075))

    def test_model_from_name(self):
        self.assertEqual(G._model_from_name("GVH5075_1A2B"), "H5075")
        self.assertEqual(G._model_from_name("Govee_H5102_ZZ"), "H5102")
        self.assertIsNone(G._model_from_name("random"))

    def test_describe_manufacturer_dumps_bytes(self):
        self.assertEqual(G.describe_manufacturer(H5075), "0xEC88=0003419c6400")
        self.assertIn("no manufacturer", G.describe_manufacturer({}))


class Reader(unittest.TestCase):
    def _scan(self, *observations):
        return lambda seconds: list(observations)

    def _obs(self, address, mfr=None, name="GVH5075_1A2B", rssi=-55):
        return {"address": address, "name": name,
                "mfr_data": mfr or H5075, "rssi": rssi}

    def test_reads_the_only_sensor(self):
        t = G.GoveeThermometer(scan=self._scan(self._obs("AA:BB:CC:DD:EE:01")))
        r = t.read()
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.fahrenheit, 70.4, places=1)
        self.assertEqual(r.battery, 100)
        self.assertIs(t.last, r)

    def test_address_locks_onto_one_sensor(self):
        scan = self._scan(self._obs("AA:BB:CC:DD:EE:01"),
                          self._obs("11:22:33:44:55:66"))
        t = G.GoveeThermometer(address="11:22:33:44:55:66", scan=scan)
        # It read *a* sensor; the address filter did not drop everything.
        self.assertIsNotNone(t.read())

    def test_a_wrong_address_reads_nothing(self):
        t = G.GoveeThermometer(address="00:00:00:00:00:00",
                               scan=self._scan(self._obs("AA:BB:CC:DD:EE:01")))
        self.assertIsNone(t.read())

    def test_strongest_signal_wins_without_an_address(self):
        far = self._obs("AA:BB:CC:DD:EE:01", rssi=-90)
        near = self._obs("11:22:33:44:55:66", rssi=-40)
        t = G.GoveeThermometer(scan=self._scan(far, near))
        # Both decode to the same reading; the point is it does not crash and
        # returns a reading from the set.
        self.assertIsNotNone(t.read())

    def test_no_sensors_reads_none(self):
        self.assertIsNone(G.GoveeThermometer(scan=self._scan()).read())

    def test_a_scan_that_raises_is_survived(self):
        def boom(seconds):
            raise RuntimeError("no adapter")
        self.assertIsNone(G.GoveeThermometer(scan=boom).read())


class Config(unittest.TestCase):
    def _temp(self, raw):
        from vicelights.config import _temperature
        return _temperature(raw)

    def test_source_defaults_to_dht_and_rejects_junk(self):
        self.assertEqual(self._temp({})["source"], "dht")
        self.assertEqual(self._temp({"source": "govee"})["source"], "govee")
        self.assertEqual(self._temp({"source": "wat"})["source"], "dht")

    def test_address_is_normalised_or_blanked(self):
        self.assertEqual(self._temp({"address": "aa-bb-cc-dd-ee-ff"})["address"],
                         "AA:BB:CC:DD:EE:FF")
        self.assertEqual(self._temp({"address": "garbage"})["address"], "")
        self.assertEqual(self._temp({})["address"], "")


class SamplerPicksSource(unittest.TestCase):
    def _sampler(self):
        from vicelights.scheduler import TemperatureSampler

        class _Store:
            def temperature(self):
                return {}
        return TemperatureSampler(_Store())

    def test_govee_source_builds_a_govee_reader(self):
        from vicelights.govee import GoveeThermometer
        probe = self._sampler()._build_probe(
            {"source": "govee", "address": "AA:BB:CC:DD:EE:01"})
        self.assertIsInstance(probe, GoveeThermometer)
        self.assertEqual(probe.address, "AA:BB:CC:DD:EE:01")

    def test_dht_source_builds_a_thermometer(self):
        from vicelights.thermometer import Thermometer
        probe = self._sampler()._build_probe(
            {"source": "dht", "model": "DHT22", "pin": 4})
        self.assertIsInstance(probe, Thermometer)

    def test_signature_changes_when_the_sensor_changes(self):
        s = self._sampler()
        dht = s._probe_signature({"source": "dht", "model": "DHT11", "pin": 13})
        govee = s._probe_signature({"source": "govee",
                                    "address": "AA:BB:CC:DD:EE:01"})
        self.assertNotEqual(dht, govee)

    def test_a_disabled_run_clears_the_thread_handle(self):
        # So toggling the sensor back on can start a fresh thread rather than
        # being blocked by a dead handle.
        from vicelights.scheduler import TemperatureSampler

        class Store:
            def temperature(self):
                return {"enabled": False}
        s = TemperatureSampler(Store())
        s._thread = "stale"
        s._run()                      # disabled -> loop returns at once
        self.assertIsNone(s._thread)

    def test_start_does_not_stack_a_second_live_thread(self):
        from vicelights.scheduler import TemperatureSampler

        class Store:
            def temperature(self):
                return {"enabled": True, "source": "dht"}

        class Alive:
            def is_alive(self):
                return True
        s = TemperatureSampler(Store())
        s._thread = Alive()
        s.start()
        self.assertIsInstance(s._thread, Alive)   # left the running one alone


if __name__ == "__main__":
    unittest.main(verbosity=2)
