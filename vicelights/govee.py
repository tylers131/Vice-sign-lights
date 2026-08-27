"""Read a Govee Bluetooth temperature/humidity sensor.

A Govee sensor is not a device you connect to -- it is a beacon. Several times a
minute it broadcasts a BLE advertisement whose manufacturer-specific data holds
the current temperature, humidity and battery. So there is no pairing, no
connection and no write: you listen for the advertisement and decode it. That
also means the sensor never has to be "claimed" by this Pi -- the phone app can
keep working at the same time.

The catch is that Govee has never published the format, and it differs across
models. This module decodes the one family whose format is well established and
verified across the community's implementations:

* **H5075 / H5072 / H5074** (and other models that broadcast the same way): a
  three-byte value under manufacturer id ``0xEC88`` packing temperature and
  humidity together.

Any other model is still *found* by the scan -- by its Govee name -- and its
raw advertisement bytes are printed, but no number is invented for it. That is
deliberate: this sign would rather show nothing than a plausible-looking wrong
temperature (the same rule the DHT path follows). A model that does not decode
is added by capturing its real bytes with the scan tool and working out the
layout from those, not by guessing.

A reading is accepted only when it is physically plausible (a real temperature
and a 0-100% humidity), so a stray advertisement cannot put a nonsense number
on the sign.

Nothing here connects to the shared radio for more than a short passive scan,
and the reading is returned as the same :class:`~vicelights.thermometer.Reading`
the DHT path returns, so the schedule cannot tell which sensor it came from.

    python3 -m vicelights.govee scan          # find your sensor, see a reading
    python3 -m vicelights.govee read AA:BB:.. # one reading from that sensor
"""

from __future__ import annotations

import logging

from .thermometer import Reading

log = logging.getLogger("vicelights.govee")

# Manufacturer id (the key of the advertisement's manufacturer_data dict) for
# the packed temp/humidity format this module decodes.
GOVEE_TEMP_HUM = 0xEC88     # H5075/H5072/H5074 and kin

# Names Govee gives its sensors, so a passive scan can tell one from the dozens
# of other beacons on the playa. Matched case-insensitively as a prefix. This is
# also how a model this module cannot yet decode still shows up in the scan.
GOVEE_NAME_HINTS = ("GVH", "GV_H", "GOVEE", "IHOMENT", "B5178")

# A reading outside these is not a temperature/humidity, it is a mis-decode of
# some other beacon -- dropped rather than shown.
MIN_C, MAX_C = -40.0, 85.0
MIN_RH, MAX_RH = 0.0, 100.0


class GoveeReading:
    """What one advertisement said: temperature, humidity, battery."""

    def __init__(self, celsius, humidity, battery=None, model=None):
        self.celsius = celsius
        self.humidity = humidity
        self.battery = battery
        self.model = model

    def plausible(self) -> bool:
        return (self.celsius is not None
                and MIN_C <= self.celsius <= MAX_C
                and (self.humidity is None
                     or MIN_RH <= self.humidity <= MAX_RH))

    def __repr__(self):
        hum = "" if self.humidity is None else " %.1f%%RH" % self.humidity
        batt = "" if self.battery is None else " %d%%batt" % self.battery
        return "<GoveeReading %.1fC%s%s%s>" % (
            self.celsius, hum, batt, " %s" % self.model if self.model else "")


def looks_like_govee(name: str, mfr_data: dict) -> bool:
    """A name or a known manufacturer id that marks this as a Govee sensor."""
    upper = (name or "").upper()
    if any(upper.startswith(hint) for hint in GOVEE_NAME_HINTS):
        return True
    return bool(mfr_data) and GOVEE_TEMP_HUM in mfr_data


def _packed_temp_hum(value: int):
    """Split Govee's packed 3-byte temp+humidity value.

    The low three decimal digits are the humidity in tenths of a percent; the
    rest is the temperature in ten-thousandths of a degree. The top bit is a
    sign flag, so a freezing playa dawn reads negative rather than as a huge
    positive number.
    """
    negative = bool(value & 0x800000)
    magnitude = value & 0x7FFFFF
    celsius = magnitude / 10000.0
    if negative:
        celsius = -celsius
    humidity = (magnitude % 1000) / 10.0
    return round(celsius, 2), round(humidity, 1)


def _decode_temp_hum(data: bytes):
    """H5075/H5072/H5074 and the H51xx minis (manufacturer id 0xEC88).

    Layout ``[flag][t/h hi][t/h mid][t/h lo][battery]``: a leading byte, the
    packed value big-endian, then battery percent.
    """
    if len(data) < 5:
        return None
    value = (data[1] << 16) | (data[2] << 8) | data[3]
    celsius, humidity = _packed_temp_hum(value)
    battery = data[4] if data[4] <= 100 else None
    return GoveeReading(celsius, humidity, battery)


def decode(name: str, mfr_data: dict):
    """A plausible :class:`GoveeReading` from one advertisement, or ``None``.

    ``mfr_data`` is bleak's ``manufacturer_data``: ``{company_id: bytes}``. The
    reading is returned only if it is physically sensible -- so a beacon that
    happens to carry id 0xEC88 with unrelated bytes is rejected rather than
    shown as a wild temperature. An unrecognised model returns ``None`` and the
    caller keeps the raw bytes.
    """
    mfr_data = mfr_data or {}
    if GOVEE_TEMP_HUM in mfr_data:
        reading = _decode_temp_hum(mfr_data[GOVEE_TEMP_HUM])
        if reading is not None and reading.plausible():
            reading.model = _model_from_name(name)
            return reading
    return None


def _model_from_name(name: str):
    """"GVH5075_1A2B" -> "H5075", for the status line. Best effort."""
    upper = (name or "").upper()
    start = upper.find("H5")
    if start == -1:
        return None
    model = upper[start:start + 5]
    return model if model[2:].isdigit() else None


def describe_manufacturer(mfr_data: dict) -> str:
    """"0xEC88=000341..., 0x004C=..." -- the raw bytes, for the scan tool.

    When a sensor does not decode, this is what to send back so its format can
    be added: every manufacturer block, id and bytes, exactly as broadcast.
    """
    if not mfr_data:
        return "(no manufacturer data)"
    parts = []
    for mid in sorted(mfr_data):
        parts.append("0x%04X=%s" % (mid, bytes(mfr_data[mid]).hex()))
    return ", ".join(parts)


class GoveeThermometer:
    """Reads a Govee sensor by listening for its advertisement.

    ``scan`` is any callable ``scan(seconds) -> list`` of observations, each a
    dict with ``address``, ``name``, ``mfr_data`` and optionally ``rssi`` --
    the contract :func:`scan_observations` fulfils with real hardware and a
    test fills in with no radio at all. Leaving it out builds the real scanner
    lazily, so this module imports on a machine with no Bluetooth.

    ``address`` locks onto one sensor (there may be several Govee beacons in a
    camp); without it the first plausible Govee reading wins.
    """

    def __init__(self, address: str = None, seconds: float = 8.0,
                 stale_after: float = None, scan=None):
        self.address = (address or "").upper() or None
        self.seconds = max(3.0, float(seconds))
        self._scan = scan
        self.last = None
        self._complaint = None
        from .thermometer import DEFAULT_STALE_AFTER
        self.stale_after = (DEFAULT_STALE_AFTER if stale_after is None
                            else float(stale_after))

    def read(self):
        """One reading, or ``None`` if no matching sensor answered in time."""
        try:
            observations = (self._scan or _default_scan)(self.seconds)
        except Exception as exc:                     # never take the app down
            self._complain("Govee scan failed: %s" % exc)
            return None

        best = None
        for obs in observations or []:
            address = (obs.get("address") or "").upper()
            if self.address and address != self.address:
                continue
            reading = decode(obs.get("name"), obs.get("mfr_data"))
            if reading is None:
                continue
            # Locked to one sensor: take it. Otherwise the strongest signal, so
            # the sensor in this camp wins over one bleeding in from next door.
            if self.address:
                best = (reading, obs)
                break
            if best is None or _rssi(obs) > _rssi(best[1]):
                best = (reading, obs)

        if best is None:
            where = " %s" % self.address if self.address else ""
            self._complain("no Govee reading%s in %.0fs" % (where, self.seconds))
            return None

        self._complaint = None
        reading, _obs = best
        out = Reading(celsius=reading.celsius, humidity=reading.humidity)
        out.battery = reading.battery
        out.model = reading.model
        self.last = out
        return out

    def close(self):
        """Nothing to release: a passive scan holds no handle."""
        return

    def _complain(self, message: str):
        if message == self._complaint:
            log.debug("%s", message)
            return
        self._complaint = message
        log.warning("%s", message)


def _rssi(obs) -> int:
    value = obs.get("rssi")
    return -999 if value is None else int(value)


# ------------------------------------------------------------------- hardware

def _default_scan(seconds: float):
    """Passive BLE scan for Govee advertisements, via bleak.

    Built lazily and run on its own event loop so it is callable from the
    plain, threaded sampler with no async plumbing leaking outward.
    """
    import asyncio
    return asyncio.run(scan_observations(seconds))


async def scan_observations(seconds: float = 8.0) -> list:
    """Every Govee advertisement seen in ``seconds``, decoded-ready.

    Returns ``[{address, name, mfr_data, rssi}]`` for the Govee beacons only --
    manufacturer data kept so the caller can decode or dump it.
    """
    try:
        from bleak import BleakScanner
    except Exception as exc:                         # pragma: no cover
        raise RuntimeError("bleak not installed: %s" % exc)

    seen = {}

    def _on(device, adv):
        mfr = dict(getattr(adv, "manufacturer_data", {}) or {})
        name = getattr(adv, "local_name", None) or getattr(device, "name", None) or ""
        if not looks_like_govee(name, mfr):
            return
        address = getattr(device, "address", "") or ""
        seen[address.upper()] = {
            "address": address.upper(),
            "name": name.strip(),
            "mfr_data": mfr,
            "rssi": getattr(adv, "rssi", None),
        }

    scanner = BleakScanner(detection_callback=_on)
    await scanner.start()
    try:
        import asyncio
        await asyncio.sleep(max(3.0, float(seconds)))
    finally:
        await scanner.stop()
    return list(seen.values())


# ------------------------------------------------------------------------ CLI

def _cli(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Find and read a Govee Bluetooth temperature sensor.")
    sub = parser.add_subparsers(dest="cmd")
    scan = sub.add_parser("scan", help="list Govee sensors and their readings")
    scan.add_argument("--seconds", type=float, default=10.0)
    read = sub.add_parser("read", help="one reading from a specific sensor")
    read.add_argument("address")
    read.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "read":
        therm = GoveeThermometer(address=args.address, seconds=args.seconds)
        reading = therm.read()
        if reading is None:
            print("No reading from %s. Is it in range and switched on?"
                  % args.address)
            return 1
        _print_reading(args.address, reading)
        return 0

    # Default: scan.
    import asyncio
    observations = asyncio.run(scan_observations(
        getattr(args, "seconds", 10.0)))
    if not observations:
        print("No Govee sensors seen. Make sure it is on and within a few "
              "metres, and that Bluetooth is up (hciconfig hci0 up).")
        return 1
    print("Found %d Govee sensor(s):\n" % len(observations))
    for obs in sorted(observations, key=_rssi, reverse=True):
        reading = decode(obs.get("name"), obs.get("mfr_data"))
        print("  %s  %s  (rssi %s)" % (
            obs["address"], obs["name"] or "(unnamed)", obs.get("rssi")))
        if reading is not None:
            print("      -> %.1fF / %.1fC%s%s" % (
                reading.celsius * 9 / 5 + 32, reading.celsius,
                "" if reading.humidity is None else "  %.0f%%RH" % reading.humidity,
                "" if reading.battery is None else "  battery %d%%" % reading.battery))
            print("      use:  set this as the sensor with address %s"
                  % obs["address"])
        else:
            print("      -> could not decode this model yet. Send these raw "
                  "bytes to have it added:")
            print("         %s" % describe_manufacturer(obs.get("mfr_data")))
        print()
    return 0


def _print_reading(address, reading):
    extra = []
    if getattr(reading, "humidity", None) is not None:
        extra.append("%.0f%%RH" % reading.humidity)
    if getattr(reading, "battery", None) is not None:
        extra.append("battery %d%%" % reading.battery)
    print("%s: %.1fF / %.1fC%s" % (
        address, reading.fahrenheit, reading.celsius,
        ("  " + "  ".join(extra)) if extra else ""))


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
