"""JSON config store: devices, groups, scenes, schedules, settings.

Hand-editable and UI-editable.  Writes are atomic (tmp file + fsync + rename)
so a power cut mid-save can never leave a truncated config on the SD card --
which matters when the whole thing lives on playa power.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid

log = logging.getLogger("vicelights.config")

ADDR_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

DEFAULT_SETTINGS = {
    "host": "0.0.0.0",
    "port": 80,
    "brightness_mode": "scale",      # scale | native | both
    # The sign runs flat out. With this true, every brightness value anywhere
    # -- UI slider, scene step, API call -- is overridden to 100, so nothing can
    # quietly dim the sign. Set false to re-enable dimming end to end.
    "force_full_brightness": True,
    # How to leave a built-in pattern when a solid colour is next requested.
    # none | static_mode | power_cycle. Measured on this sign with
    # `elk_scan.py unstick`: a plain colour frame is enough, so nothing extra is
    # sent. Raise it only if a replacement controller behaves differently.
    "exit_pattern": "none",
    "connect_timeout": 12.0,         # seconds per connect attempt
    "write_timeout": 6.0,            # seconds per characteristic write
    "attempts": 3,                   # 1 try + 2 retries
    "retry_backoff": 0.8,            # seconds, doubled each retry
    "inter_frame_delay": 0.06,       # gap between frames on one device
    "inter_device_delay": 0.35,      # gap between devices: lets wifi breathe
    # Measured on this sign: a clean BlueZ disconnect takes ~2.4s, 40% of the
    # time per controller, and nothing depends on its completing. Wait this long
    # then move on, letting it finish in the background. 0 = never wait; raise it
    # to restore the old blocking behaviour if a radio gets upset.
    "disconnect_wait": 0.5,
    # A controller that is off, out of range or dead costs attempts x
    # connect_timeout on EVERY sweep -- around 60s for one unit at the defaults.
    # After this many consecutive failures it is skipped outright for
    # failure_cooldown seconds, then probed once. It comes straight back the
    # moment it answers.
    "cooldown_after": 2,
    "failure_cooldown": 180.0,
    "scan_seconds": 8.0,
    "log_level": "INFO",
    "apply_on_boot": "",             # scene name to apply at startup, "" = none
}

# Cycle scenes all night on a monotonic timer. Deliberately not wall-clock: the
# Zero W has no RTC, so a cold boot on the playa knows nothing about the time,
# and rotation has to work anyway.
DEFAULT_ROTATION = {
    "enabled": False,
    "playlist": [],                  # scene names; empty = every scene not excluded
    "exclude": ["All off"],          # never rotate into these
    "interval_minutes": 8.0,
    "order": "shuffle",              # shuffle | sequential
    "avoid_repeat": True,            # never play the same scene twice running
    # Touching the controls should win. Any manual command pauses rotation for
    # this long, so the sign does not fight you while you are looking at it.
    "hold_after_manual_minutes": 15.0,
    # Time-of-day scheduling. Each day-part: {name, start "HH:MM",
    # interval_minutes, playlist:[scene names]}. Empty = one playlist all day.
    # The active part is the latest whose start has passed, wrapping midnight.
    "dayparts": [],
    # Scenes played while a coffee service is on (from the event calendar),
    # whatever the hour. Empty = the feature is off.
    "attract": [],
    "attract_interval_minutes": 4.0,
    # Nightly downtime to save battery, set from the phone or tablet mid-week.
    # While on, and the clock is set, the sign is dark between these two
    # wall-clock times (wrapping midnight) and wakes itself back into the show.
    # Enforced only while rotation is on -- rotation is what turns it dark and
    # what brings it back, so there is never a way to get stuck off.
    "auto_off_enabled": False,
    "auto_off_at": "00:00",
    "auto_on_at": "06:00",
}

# A sweep takes ~50s; anything near that leaves the radio permanently busy and
# the UI permanently sluggish.
MIN_ROTATION_MINUTES = 2.0

# The BLE text panel. One panel, not twelve: it is a single named device with
# its own protocol, so it lives here rather than in "devices" where every
# entry is assumed to speak ELK-BLEDOM and to be targetable by a group.
DEFAULT_MATRIX = {
    "enabled": False,
    "address": "",
    "name": "",
    # auto = pick a driver from the advertised name and characteristics.
    # See vicelights/matrix.py for the families and matrix_probe.py for how
    # to fingerprint a panel that none of them match.
    "family": "auto",
    "char_uuid": "",
    # THE PANEL'S REAL PIXEL COUNT. Not a preference: everything drawn is
    # placed against these, so a wrong value puts the message off the edge of
    # the display -- a failure that looks exactly like a broken protocol. This
    # sign's panel is 96 wide by 16 tall, from the product listing.
    "width": 96,
    "height": 16,
    "brightness": 100,
    # Cycle the saved messages, one at a time, each for its own dwell.
    "playlist": False,
    "default_dwell": 20.0,
    # A message wider than the panel is shown a page at a time rather than
    # scrolled. Scrolling moves nearly every lit pixel every frame, which on
    # this panel is under two frames a second and never stops -- and the panel
    # shares its radio with the twelve sign controllers.
    "paging": True,
    "page_seconds": 5.0,
    # "pixels" draws every LED from here; "native" hands the message to the
    # panel's own text command and lets it animate on its own. Native is not
    # the default because the glyph bit order it wants is undocumented -- run
    # matrix_probe.py text --sweep at the sign and set what looked right.
    "text_font": "narrow",      # 8x16 cells: the one this panel is proven on
    "bitmap_order": "msb",
    "text_reversed": False,     # for a panel that lays characters right to left
    "color_mode": 0,            # 0 solid; 2-4 are the panel's own gradients
    "h_align": 1,
    "v_align": 1,
    # Payload bytes per write. 20 is what fits the default 23-byte MTU, and
    # nothing here negotiates a larger one, so raising it needs evidence.
    "chunk": 20,
    # Acknowledged writes. Without this the panel silently drops packets when
    # it cannot keep up, which shows as a few LEDs missing from each message,
    # different ones every time. Acknowledged writes are flow-controlled and
    # cannot drop; they cost a round trip each, so the panel draws a little
    # slower and completely.
    "write_response": True,
    # Combine whole packets into one BLE write when they fit. An acknowledged
    # write costs a round trip whether it carries ten bytes or two hundred, and
    # drawing text a pixel at a time is hundreds of ten-byte packets -- so this
    # is most of the difference between a message appearing in five seconds and
    # in one. Turn it off if a panel will not read more than one packet per
    # write.
    "batch_writes": True,
    # With acknowledged writes the radio already paces itself, so this can be
    # 0. It is the gap BETWEEN packets, on top of the acknowledgement.
    "frame_delay": 0.0,
    # How an iPixel panel is given text. "pixels" sets one pixel at a time and
    # every byte of it is documented; "png" sends the whole image in one
    # transfer but has two header bytes nobody has written down. See
    # matrix_probe.py png-sweep.
    "text_mode": "pixels",
    # How many LEDs per font pixel. "auto" picks the largest whole scale the
    # message still fits at, which is what you want almost always: four letters
    # on this panel go up to 2x and fill its height, eleven letters stay at 1x
    # because 2x would run off the end.
    "scale": "auto",
    # Fill the panel's full height rather than leaving a margin. 7 does not
    # divide 16, so a doubled 5x7 glyph is 14 tall and two rows stay dark;
    # stretching maps the seven rows across all sixteen instead. Strokes end up
    # slightly uneven -- three LEDs thick in places, two in others -- in
    # exchange for letters that reach the edges.
    "stretch": True,
    # Widen every stroke by one column. "auto" does it only at 1x, where
    # stretching to a 16-row panel otherwise leaves horizontal strokes two or
    # three LEDs tall and vertical ones a single LED wide -- and that imbalance
    # is what makes small text hard to read at a distance. At 2x the strokes
    # are already two wide, so it is skipped. true forces it, false never.
    "bold": "auto",
    # Where the four colour bytes of a set-pixel command go. The published
    # protocol says rgba; this sign's panel is agrb. matrix_probe.py colortest
    # works it out from what the panel shows.
    "pixel_layout": "agrb",
    # Paint the dark pixels too, so a new message erases the last one. Costs
    # width x height packets, so it is off by default.
    "fill_background": False,
    "png_opt": 0,
    "png_buffer": 0,
    # A protocol lifted off an HCI capture, for family "raw". Hex strings,
    # so a panel nobody has written a driver for can still be driven from
    # the config file alone.
    "commands": {},
}

# The DHT temperature sensor, shown on the panel by the event schedule. Off
# by default: it needs a sensor wired to the GPIO and a driver that is not a
# base dependency, so a sign without one should see nothing and log nothing.
# BCM 13 is physical pin 33; VCC must be 3.3V, never 5V (see thermometer.py).
DEFAULT_TEMPERATURE = {
    "enabled": False,
    "pin": 13,
    "model": "DHT11",           # or DHT22 / AM2302 -- same three wires
    # A few times an hour is plenty for a number on a sign, and reading the
    # DHT blocks for up to ten seconds, so this is not tight.
    "interval_minutes": 20.0,
}

DEFAULT_CONFIG = {
    "settings": dict(DEFAULT_SETTINGS),
    "rotation": dict(DEFAULT_ROTATION),
    # What each built-in pattern actually does on THIS hardware, keyed "0x89".
    # The documented names do not describe these controllers, so this is filled
    # in by watching: `elk_scan.py modes ADDR`.
    "mode_names": {},
    "devices": [],
    "groups": [],
    "scenes": [],
    "schedules": [],
    "timers": [],
    "matrix": dict(DEFAULT_MATRIX),
    "messages": [],
    "temperature": dict(DEFAULT_TEMPERATURE),
}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def normalize_address(addr: str) -> str:
    addr = (addr or "").strip().upper().replace("-", ":")
    if not ADDR_RE.match(addr):
        raise ValueError("bad BLE address: %r (want AA:BB:CC:DD:EE:FF)" % addr)
    return addr


def _channels(value) -> str:
    """Validate a per-device channel order, falling back to plain rgb."""
    from .protocol import CHANNEL_ORDERS
    order = str(value or "rgb").strip().lower()
    if order not in CHANNEL_ORDERS:
        if value:
            log.warning("ignoring unknown channel order %r", value)
        return "rgb"
    return order


def _as_list(value) -> list:
    """A list, or [] for anything that is not one.

    So a scalar left in a hand-edited config (``"attract": true`` to "turn it
    on", or a fat-fingered ``"dayparts": 8``) reads as the feature being off,
    rather than raising out of _normalize and crash-looping the service before
    the web UI can come up to fix it.
    """
    return list(value) if isinstance(value, (list, tuple)) else []


def _stagger(raw) -> float:
    """Seconds to wait between devices when a scene is applied.

    Writes are serialised, so a built-in pattern started one unit at a time
    already sweeps across the sign. A stagger makes that deliberate: the roll
    becomes a length you chose rather than however long a connect took.
    """
    try:
        return max(0.0, min(10.0, float(raw or 0)))
    except (TypeError, ValueError):
        return 0.0


def _rotation(raw) -> dict:
    """Validate the rotation block, clamping the interval to something sane."""
    value = dict(DEFAULT_ROTATION)
    if isinstance(raw, dict):
        value.update(raw)
    value["enabled"] = bool(value.get("enabled"))
    value["playlist"] = [str(n).strip() for n in _as_list(value.get("playlist")) if str(n).strip()]
    value["exclude"] = [str(n).strip() for n in _as_list(value.get("exclude")) if str(n).strip()]
    value["order"] = "sequential" if value.get("order") == "sequential" else "shuffle"
    value["avoid_repeat"] = bool(value.get("avoid_repeat", True))
    try:
        minutes = float(value.get("interval_minutes", 8.0))
    except (TypeError, ValueError):
        minutes = 8.0
    if minutes < MIN_ROTATION_MINUTES:
        log.warning("rotation interval %.1f min is below the %.1f min floor; using the floor",
                    minutes, MIN_ROTATION_MINUTES)
        minutes = MIN_ROTATION_MINUTES
    value["interval_minutes"] = minutes
    try:
        value["hold_after_manual_minutes"] = max(
            0.0, float(value.get("hold_after_manual_minutes", 15.0)))
    except (TypeError, ValueError):
        value["hold_after_manual_minutes"] = 15.0
    value["dayparts"] = _dayparts(value.get("dayparts"))
    value["attract"] = [str(n).strip() for n in _as_list(value.get("attract"))
                        if str(n).strip()]
    try:
        value["attract_interval_minutes"] = max(
            MIN_ROTATION_MINUTES, float(value.get("attract_interval_minutes", 4.0)))
    except (TypeError, ValueError):
        value["attract_interval_minutes"] = 4.0
    value["auto_off_enabled"] = bool(value.get("auto_off_enabled"))
    value["auto_off_at"] = _hhmm(value.get("auto_off_at"), "00:00")
    value["auto_on_at"] = _hhmm(value.get("auto_on_at"), "06:00")
    return value


def _hhmm(value, default: str) -> str:
    """A validated "HH:MM" 24-hour string, or ``default`` for anything bad."""
    try:
        hour, minute = (int(x) for x in str(value).strip().split(":"))
        if 0 <= hour < 24 and 0 <= minute < 60:
            return "%02d:%02d" % (hour, minute)
    except (ValueError, TypeError):
        pass
    return default


def _dayparts(raw) -> list:
    """Validate the day-part list: named windows with a start and a playlist."""
    out = []
    for part in _as_list(raw):
        if not isinstance(part, dict):
            continue
        start = str(part.get("start") or "").strip()
        try:
            hour, minute = (int(x) for x in start.split(":"))
            if not (0 <= hour < 24 and 0 <= minute < 60):
                raise ValueError
        except (ValueError, TypeError):
            log.warning("dropping day-part with a bad start time: %r", start)
            continue
        try:
            interval = max(MIN_ROTATION_MINUTES,
                           float(part.get("interval_minutes", 8.0)))
        except (TypeError, ValueError):
            interval = 8.0
        out.append({
            "name": str(part.get("name") or start).strip(),
            "start": "%02d:%02d" % (hour, minute),
            "interval_minutes": interval,
            "playlist": [str(n).strip() for n in _as_list(part.get("playlist"))
                         if str(n).strip()],
        })
    out.sort(key=lambda d: d["start"])
    return out


def _temperature(raw) -> dict:
    """Validate the temperature block."""
    value = dict(DEFAULT_TEMPERATURE)
    if isinstance(raw, dict):
        value.update(raw)
    value["enabled"] = bool(value.get("enabled"))
    value["model"] = str(value.get("model") or "DHT11").strip().upper()
    try:
        value["pin"] = max(0, min(40, int(value.get("pin", 13))))
    except (TypeError, ValueError):
        value["pin"] = 13
    try:
        value["interval_minutes"] = max(1.0, min(180.0,
                                                 float(value.get("interval_minutes", 20.0))))
    except (TypeError, ValueError):
        value["interval_minutes"] = 20.0
    return value


def _matrix(raw) -> dict:
    """Validate the panel block, clamping anything a bad client could send."""
    from .matrix import FAMILIES, DEFAULT_FAMILY
    value = dict(DEFAULT_MATRIX)
    if isinstance(raw, dict):
        value.update(raw)
    value["enabled"] = bool(value.get("enabled"))
    value["playlist"] = bool(value.get("playlist"))
    # Cycle the calendar-driven messages (schedule.py) instead of the
    # hand-typed queue. Independent of "playlist": schedule on means the
    # panel plays the schedule whether or not the saved queue is used.
    value["schedule"] = bool(value.get("schedule"))
    address = str(value.get("address") or "").strip()
    if address:
        try:
            address = normalize_address(address)
        except ValueError as exc:
            log.error("dropping matrix address: %s", exc)
            address = ""
    value["address"] = address
    value["name"] = str(value.get("name") or "").strip()[:60]
    family = str(value.get("family") or DEFAULT_FAMILY).strip().lower()
    if family not in FAMILIES and family != DEFAULT_FAMILY:
        log.warning("unknown matrix family %r; falling back to auto-detect", family)
        family = DEFAULT_FAMILY
    value["family"] = family
    value["char_uuid"] = str(value.get("char_uuid") or "").strip().lower()
    mode = str(value.get("text_mode") or "pixels").strip().lower()
    value["text_mode"] = mode if mode in ("pixels", "png", "native") else "pixels"
    from .matrix import TEXT_FONTS, BITMAP_ORDERS
    font = str(value.get("text_font") or "narrow").strip().lower()
    value["text_font"] = font if font in TEXT_FONTS else "narrow"
    order = str(value.get("bitmap_order") or "msb").strip().lower()
    value["bitmap_order"] = order if order in BITMAP_ORDERS else "msb"
    value["text_reversed"] = bool(value.get("text_reversed"))
    from .matrix import MAX_COLOR_MODE
    for key, low, high, default in (("color_mode", 0, MAX_COLOR_MODE, 0),
                                    ("h_align", 0, 2, 1), ("v_align", 0, 2, 1)):
        try:
            value[key] = max(low, min(high, int(value.get(key, default))))
        except (TypeError, ValueError):
            value[key] = default
    scale = str(value.get("scale") or "auto").strip().lower()
    if scale != "auto":
        try:
            scale = str(max(1, min(8, int(scale))))
        except (TypeError, ValueError):
            log.warning("ignoring unusable matrix scale %r", value.get("scale"))
            scale = "auto"
    value["scale"] = scale
    from .matrix import PIXEL_LAYOUTS, DEFAULT_PIXEL_LAYOUT
    layout = str(value.get("pixel_layout") or DEFAULT_PIXEL_LAYOUT).strip().lower()
    if layout not in PIXEL_LAYOUTS:
        log.warning("ignoring unknown matrix pixel layout %r", value.get("pixel_layout"))
        layout = DEFAULT_PIXEL_LAYOUT
    value["pixel_layout"] = layout
    value["fill_background"] = bool(value.get("fill_background"))
    value["write_response"] = bool(value.get("write_response", True))
    value["batch_writes"] = bool(value.get("batch_writes", True))
    value["stretch"] = bool(value.get("stretch", True))
    bold = str(value.get("bold", "auto")).strip().lower()
    value["bold"] = bold if bold in ("auto", "true", "false") else "auto"
    for key, low, high, default in (("width", 4, 256, 32), ("height", 4, 256, 16),
                                    ("brightness", 5, 100, 100), ("chunk", 8, 512, 20),
                                    ("png_opt", 0, 255, 0), ("png_buffer", 0, 255, 0)):
        try:
            value[key] = max(low, min(high, int(value.get(key, default))))
        except (TypeError, ValueError):
            value[key] = default
    try:
        value["default_dwell"] = max(0.0, min(3600.0, float(value.get("default_dwell", 20.0))))
    except (TypeError, ValueError):
        value["default_dwell"] = 20.0
    value["paging"] = bool(value.get("paging", True))
    try:
        # Floored at the scheduler tick: pages turn from that thread, so a
        # smaller number would not be honoured, it would just be a lie.
        value["page_seconds"] = max(5.0, min(120.0, float(value.get("page_seconds", 5.0))))
    except (TypeError, ValueError):
        value["page_seconds"] = 5.0
    try:
        value["frame_delay"] = max(0.0, min(1.0, float(value.get("frame_delay", 0.02))))
    except (TypeError, ValueError):
        value["frame_delay"] = 0.02
    commands = {}
    for name, entry in (value.get("commands") or {}).items():
        if isinstance(entry, str):
            entry = [entry]
        if not isinstance(entry, (list, tuple)):
            log.warning("ignoring matrix command %r: not hex or a list of hex", name)
            continue
        commands[str(name)] = [str(part) for part in entry]
    value["commands"] = commands
    return value


def _mode_names(raw) -> dict:
    """Normalise observed mode labels to '0xNN' keys, dropping junk."""
    from .protocol import MODE_MIN, MODE_MAX, mode_key
    names = {}
    for key, label in (raw if isinstance(raw, dict) else {}).items():
        try:
            value = int(str(key), 0)
        except (TypeError, ValueError):
            log.warning("ignoring mode name with bad key %r", key)
            continue
        if not MODE_MIN <= value <= MODE_MAX:
            log.warning("ignoring mode name outside 0x%02x-0x%02x: %r",
                        MODE_MIN, MODE_MAX, key)
            continue
        label = str(label or "").strip()
        if label:
            names[mode_key(value)] = label[:60]
    return names


class ConfigError(Exception):
    pass


LAST_GOOD_SUFFIX = ".lastgood"


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class ConfigStore:
    """Thread-safe accessor around the on-disk JSON config."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._data = dict(DEFAULT_CONFIG)
        self.load()

    # ---------------------------------------------------------------- io

    def load(self):
        with self._lock:
            if not os.path.exists(self.path):
                log.warning("no config at %s, starting from defaults", self.path)
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
                self.save()
                return self._data
            try:
                raw = _read_json(self.path)
            except Exception as exc:
                return self._recover(exc)
            # A config that parses as JSON can still be structurally wrong (a
            # scalar where a list belongs). Normalising must never be able to
            # crash startup either -- that would crash-loop the service under
            # Restart=always, exactly what _recover exists to prevent -- so a
            # normalise failure falls back to the last-good copy just as a parse
            # failure does. The field normalisers coerce the common typos; this
            # catches anything they miss.
            try:
                self._data = self._normalize(raw)
            except Exception as exc:
                return self._recover(exc)
            self._keep_last_good()
            log.info(
                "loaded config: %d devices, %d groups, %d scenes, %d schedules",
                len(self._data["devices"]), len(self._data["groups"]),
                len(self._data["scenes"]), len(self._data["schedules"]),
            )
            return self._data

    def _keep_last_good(self):
        """Snapshot a config that parsed, to fall back to if this one stops.

        Cheap: load happens at startup and on an explicit reload, not per sweep.
        """
        try:
            shutil.copy2(self.path, self.path + LAST_GOOD_SUFFIX)
        except OSError as exc:
            log.debug("could not snapshot last-good config: %s", exc)

    def _recover(self, exc):
        """Come up anyway when the config will not parse.

        A sign serving its web UI with the wrong scenes can be fixed from a
        phone in thirty seconds. One that raises at startup crash-loops under
        Restart=always, never binds port 80, and needs SSH and a laptop --
        which, in a desert at night, means it stays dark. So a config that has
        been corrupted (an SD card surviving repeated unclean shutdowns is not
        a given) must never be able to stop the service from starting.
        """
        log.error("config at %s will not parse: %s", self.path, exc)
        preserved = self._preserve_unreadable()

        for label, candidate in (("last-good", self.path + LAST_GOOD_SUFFIX),
                                 ("backup", self.path + ".bak")):
            if not os.path.exists(candidate):
                continue
            try:
                raw = _read_json(candidate)
            except Exception as inner:
                log.error("the %s copy (%s) will not parse either: %s",
                          label, candidate, inner)
                continue
            self._data = self._normalize(raw)
            log.warning("recovered the config from the %s copy (%s)", label, candidate)
            if preserved:
                log.warning("the unreadable file was kept at %s", preserved)
            self.save()
            return self._data

        log.error("no usable copy of the config; starting from defaults. "
                  "Devices, scenes and mode names are gone until you restore "
                  "one%s", " -- unreadable original kept at %s" % preserved
                  if preserved else "")
        self._data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.save()
        return self._data

    def _preserve_unreadable(self) -> str:
        """Copy the unreadable config aside so it can still be salvaged.

        Copied rather than moved: save() carries ownership across only when the
        original is still there, and losing the owner is how the config stops
        being writable from the web UI.
        """
        target = "%s.unreadable-%d" % (self.path, int(time.time()))
        try:
            shutil.copy2(self.path, target)
            return target
        except OSError as exc:
            log.error("could not preserve the unreadable config: %s", exc)
            return ""

    def save(self):
        with self._lock:
            payload = json.dumps(self._data, indent=2, sort_keys=False)
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                # An atomic replace makes a NEW inode owned by whoever is
                # writing. The service runs as root, so without carrying the
                # original ownership across it quietly takes the config away
                # from the user editing it with the CLI -- and every `chown`
                # gets undone by the next save.
                self._inherit_ownership(tmp)
                os.replace(tmp, self.path)
                # fsync the directory so the rename itself is durable.
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    def _inherit_ownership(self, tmp: str):
        """Give the replacement file the original's mode and owner."""
        try:
            original = os.stat(self.path)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
            return
        try:
            os.chmod(tmp, stat.S_IMODE(original.st_mode))
        except OSError as exc:
            log.debug("could not carry mode across: %s", exc)
        if os.geteuid() != 0 and original.st_uid != os.geteuid():
            return                      # only root can hand a file to someone else
        try:
            os.chown(tmp, original.st_uid, original.st_gid)
        except OSError as exc:
            log.debug("could not carry ownership across: %s", exc)

    # ------------------------------------------------------------ shape

    def _normalize(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be an object")

        data = json.loads(json.dumps(DEFAULT_CONFIG))
        settings = dict(DEFAULT_SETTINGS)
        raw_settings = raw.get("settings")
        if isinstance(raw_settings, dict):
            settings.update(raw_settings)
        data["settings"] = settings
        data["rotation"] = _rotation(raw.get("rotation"))
        data["mode_names"] = _mode_names(raw.get("mode_names"))
        data["matrix"] = _matrix(raw.get("matrix"))
        data["temperature"] = _temperature(raw.get("temperature"))

        from .matrix import normalize_message, MAX_MESSAGES
        dwell = data["matrix"]["default_dwell"]
        seen_messages = set()
        for entry in _as_list(raw.get("messages"))[:MAX_MESSAGES]:
            if not isinstance(entry, dict):
                continue
            message = normalize_message(entry, dwell)
            if not message["text"]:
                log.warning("dropping message with no text")
                continue
            if message["id"] in seen_messages:
                log.warning("dropping duplicate message id %s", message["id"])
                continue
            seen_messages.add(message["id"])
            data["messages"].append(message)

        seen = set()
        for entry in _as_list(raw.get("devices")):
            if not isinstance(entry, dict):
                log.warning("dropping non-object device entry")
                continue
            try:
                address = normalize_address(entry.get("address"))
            except ValueError as exc:
                log.error("dropping device: %s", exc)
                continue
            if address in seen:
                log.warning("dropping duplicate device %s", address)
                continue
            seen.add(address)
            data["devices"].append({
                "address": address,
                "name": (entry.get("name") or address).strip(),
                "groups": [str(g).strip() for g in _as_list(entry.get("groups")) if str(g).strip()],
                "enabled": bool(entry.get("enabled", True)),
                "char_uuid": entry.get("char_uuid") or None,
                "channels": _channels(entry.get("channels")),
                "notes": entry.get("notes") or "",
            })

        groups = [str(g).strip() for g in _as_list(raw.get("groups")) if str(g).strip()]
        for device in data["devices"]:
            for group in device["groups"]:
                if group not in groups:
                    groups.append(group)
        data["groups"] = groups

        for scene in _as_list(raw.get("scenes")):
            if not isinstance(scene, dict):
                continue
            steps = []
            for step in _as_list(scene.get("steps")):
                if not isinstance(step, dict):
                    continue
                steps.append({
                    "target": step.get("target") or "all",
                    "power": step.get("power", True),
                    "color": step.get("color", "#ffffff"),
                    "brightness": step.get("brightness", 100),
                    "mode": step.get("mode"),
                    "speed": step.get("speed"),
                })
            data["scenes"].append({
                "id": scene.get("id") or new_id(),
                "name": (scene.get("name") or "unnamed").strip(),
                "steps": steps,
                "stagger": _stagger(scene.get("stagger")),
            })

        for schedule in _as_list(raw.get("schedules")):
            if not isinstance(schedule, dict):
                continue
            data["schedules"].append({
                "id": schedule.get("id") or new_id(),
                "name": (schedule.get("name") or "").strip(),
                "scene": schedule.get("scene") or "",
                "time": schedule.get("time") or "00:00",
                "days": sorted({int(d) for d in _as_list(schedule.get("days")) if 0 <= int(d) <= 6}),
                "enabled": bool(schedule.get("enabled", True)),
                "last_fired": schedule.get("last_fired") or "",
            })

        return data

    # ------------------------------------------------------------ reads

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    @property
    def settings(self) -> dict:
        with self._lock:
            return dict(self._data["settings"])

    def setting(self, key, default=None):
        with self._lock:
            return self._data["settings"].get(key, DEFAULT_SETTINGS.get(key, default))

    def devices(self, enabled_only: bool = False) -> list:
        with self._lock:
            devices = json.loads(json.dumps(self._data["devices"]))
        if enabled_only:
            devices = [d for d in devices if d.get("enabled", True)]
        return devices

    def device(self, address: str):
        address = normalize_address(address)
        for device in self.devices():
            if device["address"] == address:
                return device
        return None

    def group_names(self) -> list:
        with self._lock:
            return list(self._data["groups"])

    def scenes(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._data["scenes"]))

    def scene(self, name_or_id: str):
        key = (name_or_id or "").strip().lower()
        for scene in self.scenes():
            if scene["id"] == name_or_id or scene["name"].strip().lower() == key:
                return scene
        return None

    def schedules(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._data["schedules"]))

    # ---------------------------------------------------------- targets

    def resolve_target(self, target: str) -> list:
        """'all' | 'group:NAME' | 'device:AA:BB:..' | bare address -> addresses.

        Disabled devices are never returned: disabling is how you park a unit
        that has died in the dust without editing every scene.
        """
        target = (target or "all").strip()
        devices = self.devices(enabled_only=True)

        if target.lower() in ("all", "*"):
            return [d["address"] for d in devices]

        if target.lower().startswith("group:"):
            name = target.split(":", 1)[1].strip().lower()
            return [d["address"] for d in devices
                    if any(g.strip().lower() == name for g in d["groups"])]

        if target.lower().startswith("device:"):
            target = target.split(":", 1)[1].strip()

        try:
            address = normalize_address(target)
        except ValueError:
            # Fall back to a friendly-name lookup so scenes can name a device.
            key = target.strip().lower()
            return [d["address"] for d in devices if d["name"].strip().lower() == key]
        return [d["address"] for d in devices if d["address"] == address]

    def target_label(self, target: str) -> str:
        target = (target or "all").strip()
        if target.lower() in ("all", "*"):
            return "all devices"
        if target.lower().startswith("group:"):
            return "group %s" % target.split(":", 1)[1]
        addresses = self.resolve_target(target)
        if len(addresses) == 1:
            device = self.device(addresses[0])
            if device:
                return device["name"]
        return target

    # --------------------------------------------------------- mutation

    def mutate(self, fn):
        """Apply ``fn(data)`` under the lock, then persist and return the result."""
        with self._lock:
            result = fn(self._data)
            self._sync_groups()
            self.save()
        return result

    def _sync_groups(self):
        groups = list(self._data["groups"])
        for device in self._data["devices"]:
            for group in device["groups"]:
                if group not in groups:
                    groups.append(group)
        self._data["groups"] = groups

    def replace_all(self, raw: dict):
        with self._lock:
            self._data = self._normalize(raw)
            self.save()
        return self.snapshot()

    def upsert_device(self, entry: dict) -> dict:
        address = normalize_address(entry.get("address"))

        def apply(data):
            for device in data["devices"]:
                if device["address"] == address:
                    device["name"] = (entry.get("name") or device["name"]).strip()
                    if "groups" in entry:
                        device["groups"] = [str(g).strip() for g in entry["groups"] if str(g).strip()]
                    if "enabled" in entry:
                        device["enabled"] = bool(entry["enabled"])
                    if "char_uuid" in entry:
                        device["char_uuid"] = entry["char_uuid"] or None
                    if "channels" in entry:
                        device["channels"] = _channels(entry["channels"])
                    if "notes" in entry:
                        device["notes"] = entry["notes"]
                    return device
            device = {
                "address": address,
                "name": (entry.get("name") or address).strip(),
                "groups": [str(g).strip() for g in (entry.get("groups") or []) if str(g).strip()],
                "enabled": bool(entry.get("enabled", True)),
                "char_uuid": entry.get("char_uuid") or None,
                "channels": _channels(entry.get("channels")),
                "notes": entry.get("notes") or "",
            }
            data["devices"].append(device)
            return device

        return self.mutate(apply)

    def delete_device(self, address: str) -> bool:
        address = normalize_address(address)

        def apply(data):
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d["address"] != address]
            return len(data["devices"]) != before

        return self.mutate(apply)

    def remember_characteristic(self, address: str, char_uuid: str):
        """Cache the detected characteristic so later connects skip discovery."""
        def apply(data):
            for device in data["devices"]:
                if device["address"] == address and device.get("char_uuid") != char_uuid:
                    device["char_uuid"] = char_uuid
                    return True
            return False

        try:
            return self.mutate(apply)
        except Exception:
            log.exception("could not cache characteristic for %s", address)
            return False

    def add_group(self, name: str):
        name = (name or "").strip()
        if not name:
            raise ValueError("group name required")

        def apply(data):
            if name not in data["groups"]:
                data["groups"].append(name)
            return data["groups"]

        return self.mutate(apply)

    def delete_group(self, name: str):
        def apply(data):
            data["groups"] = [g for g in data["groups"] if g != name]
            for device in data["devices"]:
                device["groups"] = [g for g in device["groups"] if g != name]
            return data["groups"]

        result = self.mutate(apply)
        # _sync_groups would resurrect it from membership; membership is cleared
        # above, so this is safe.
        return result

    def upsert_scene(self, scene: dict) -> dict:
        name = (scene.get("name") or "").strip()
        if not name:
            raise ValueError("scene name required")
        steps = []
        for step in scene.get("steps") or []:
            steps.append({
                "target": step.get("target") or "all",
                "power": bool(step.get("power", True)),
                "color": step.get("color", "#ffffff"),
                "brightness": int(step.get("brightness", 100)),
                "mode": step.get("mode"),
                "speed": step.get("speed"),
            })
        if not steps:
            raise ValueError("scene needs at least one step")
        scene_id = scene.get("id")

        def apply(data):
            for existing in data["scenes"]:
                if (scene_id and existing["id"] == scene_id) or existing["name"] == name:
                    existing["name"] = name
                    existing["steps"] = steps
                    if "stagger" in scene:
                        existing["stagger"] = _stagger(scene.get("stagger"))
                    return existing
            created = {"id": scene_id or new_id(), "name": name, "steps": steps,
                       "stagger": _stagger(scene.get("stagger"))}
            data["scenes"].append(created)
            return created

        return self.mutate(apply)

    def delete_scene(self, scene_id: str) -> bool:
        def apply(data):
            before = len(data["scenes"])
            data["scenes"] = [s for s in data["scenes"]
                              if s["id"] != scene_id and s["name"] != scene_id]
            return len(data["scenes"]) != before

        return self.mutate(apply)

    def upsert_schedule(self, schedule: dict) -> dict:
        time_str = (schedule.get("time") or "").strip()
        if not re.match(r"^\d{1,2}:\d{2}$", time_str):
            raise ValueError("time must be HH:MM")
        hour, minute = (int(part) for part in time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time out of range")
        time_str = "%02d:%02d" % (hour, minute)
        scene_name = (schedule.get("scene") or "").strip()
        if not self.scene(scene_name):
            raise ValueError("unknown scene: %s" % scene_name)
        days = sorted({int(d) for d in (schedule.get("days") or []) if 0 <= int(d) <= 6})
        schedule_id = schedule.get("id")

        def apply(data):
            for existing in data["schedules"]:
                if schedule_id and existing["id"] == schedule_id:
                    existing.update({
                        "name": (schedule.get("name") or existing["name"]).strip(),
                        "scene": scene_name,
                        "time": time_str,
                        "days": days,
                        "enabled": bool(schedule.get("enabled", True)),
                    })
                    return existing
            created = {
                "id": schedule_id or new_id(),
                "name": (schedule.get("name") or "").strip(),
                "scene": scene_name,
                "time": time_str,
                "days": days,
                "enabled": bool(schedule.get("enabled", True)),
                "last_fired": "",
            }
            data["schedules"].append(created)
            return created

        return self.mutate(apply)

    def delete_schedule(self, schedule_id: str) -> bool:
        def apply(data):
            before = len(data["schedules"])
            data["schedules"] = [s for s in data["schedules"] if s["id"] != schedule_id]
            return len(data["schedules"]) != before

        return self.mutate(apply)

    def mode_names(self) -> dict:
        with self._lock:
            return dict(self._data.get("mode_names") or {})

    def set_mode_name(self, value: int, label: str) -> dict:
        """Record what a pattern really looks like on this hardware."""
        from .protocol import mode_key

        def apply(data):
            names = dict(data.get("mode_names") or {})
            key = mode_key(value)
            label_clean = str(label or "").strip()[:60]
            if label_clean:
                names[key] = label_clean
            else:
                names.pop(key, None)
            data["mode_names"] = _mode_names(names)
            return data["mode_names"]

        return self.mutate(apply)

    def update_settings(self, changes: dict) -> dict:
        """Change settings at runtime, validated by the usual normaliser.

        Only for the handful worth reaching without an editor -- the boot
        scene above all, because it fires a full twelve-device sweep on every
        service restart, and during troubleshooting that is a restart every
        few minutes.
        """
        def apply(data):
            merged = dict(data.get("settings") or DEFAULT_SETTINGS)
            merged.update(changes or {})
            cleaned = dict(DEFAULT_SETTINGS)
            cleaned.update(merged)
            data["settings"] = cleaned
            return cleaned

        return self.mutate(apply)

    def temperature(self) -> dict:
        with self._lock:
            return dict(self._data.get("temperature") or DEFAULT_TEMPERATURE)

    def update_temperature(self, changes: dict) -> dict:
        def apply(data):
            merged = dict(data.get("temperature") or DEFAULT_TEMPERATURE)
            merged.update(changes or {})
            data["temperature"] = _temperature(merged)
            return data["temperature"]

        return self.mutate(apply)

    def rotation(self) -> dict:
        with self._lock:
            return dict(self._data.get("rotation") or DEFAULT_ROTATION)

    def update_rotation(self, changes: dict) -> dict:
        def apply(data):
            merged = dict(data.get("rotation") or DEFAULT_ROTATION)
            merged.update(changes or {})
            data["rotation"] = _rotation(merged)
            return data["rotation"]

        return self.mutate(apply)

    def rotation_scenes(self, playlist=None) -> list:
        """The scenes rotation may play, in config order, honouring exclusions.

        ``playlist`` overrides the configured one -- the rotation passes the
        active day-part's list so the same filtering (exclude, existence) runs
        against it.
        """
        rotation = self.rotation()
        excluded = {n.strip().lower() for n in rotation["exclude"]}
        source = rotation["playlist"] if playlist is None else playlist
        chosen = [str(n).strip().lower() for n in source]
        names = []
        for scene in self.scenes():
            key = scene["name"].strip().lower()
            if key in excluded:
                continue
            if chosen and key not in chosen:
                continue
            names.append(scene["name"])
        return names

    # ------------------------------------------------------------ matrix panel

    def matrix(self) -> dict:
        with self._lock:
            return dict(self._data.get("matrix") or DEFAULT_MATRIX)

    def update_matrix(self, changes: dict) -> dict:
        def apply(data):
            merged = dict(data.get("matrix") or DEFAULT_MATRIX)
            merged.update(changes or {})
            data["matrix"] = _matrix(merged)
            return data["matrix"]

        return self.mutate(apply)

    def messages(self, enabled_only: bool = False) -> list:
        with self._lock:
            messages = [dict(m) for m in self._data.get("messages") or []]
        if enabled_only:
            messages = [m for m in messages if m.get("enabled", True) and m.get("text")]
        return messages

    def message(self, message_id: str):
        for message in self.messages():
            if message["id"] == message_id:
                return message
        return None

    def upsert_message(self, entry: dict) -> dict:
        """Add or edit one queued message.

        New messages go on the end of the list. The list order IS the play
        order -- there is no separate sort key -- so reordering is a single
        call and what you see in the UI is what the panel will do.
        """
        from .matrix import normalize_message, MAX_MESSAGES

        def apply(data):
            dwell = (data.get("matrix") or DEFAULT_MATRIX).get("default_dwell", 20.0)
            message = normalize_message(entry, dwell)
            if not message["text"]:
                raise ConfigError("a message needs some text")
            existing = data.setdefault("messages", [])
            for index, current in enumerate(existing):
                if current["id"] == message["id"]:
                    existing[index] = message
                    return message
            if len(existing) >= MAX_MESSAGES:
                raise ConfigError("the queue holds %d messages; delete one first"
                                  % MAX_MESSAGES)
            existing.append(message)
            return message

        return self.mutate(apply)

    def delete_message(self, message_id: str) -> bool:
        def apply(data):
            existing = data.setdefault("messages", [])
            remaining = [m for m in existing if m["id"] != message_id]
            if len(remaining) == len(existing):
                return False
            data["messages"] = remaining
            return True

        return self.mutate(apply)

    def reorder_messages(self, ids: list) -> list:
        """Put the queue in the given order.

        Anything the caller left out keeps its relative position at the end
        rather than being deleted: a reorder request built from a stale copy
        of the list should shuffle the queue, never silently empty it.
        """
        wanted = [str(i) for i in (ids or [])]

        def apply(data):
            existing = data.setdefault("messages", [])
            by_id = {m["id"]: m for m in existing}
            ordered = [by_id.pop(i) for i in wanted if i in by_id]
            ordered += [m for m in existing if m["id"] in by_id]
            data["messages"] = ordered
            return [m["id"] for m in ordered]

        return self.mutate(apply)

    def mark_schedule_fired(self, schedule_id: str, stamp: str):
        def apply(data):
            for schedule in data["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule["last_fired"] = stamp
            return True

        return self.mutate(apply)
