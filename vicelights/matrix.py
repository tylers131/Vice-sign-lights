"""BLE LED matrix panel: text messages on a pixel display.

This is a different device class from the ELK-BLEDOM controllers, and it is
kept in its own module on purpose.  The twelve controllers all speak one
9-byte frame format and the whole of ``protocol.py`` exists to build it; a
matrix panel speaks a length-prefixed, chunked protocol carrying a bitmap,
and which protocol it speaks depends on who made it.  Folding that into
``protocol.py`` would mean two unrelated wire formats sharing one namespace.

What is shared is the radio.  Panel writes go through the same serialized
``BleWorker`` as everything else -- see ``BleWorker.submit_matrix`` -- because
the Pi has one adapter and a panel write racing a scene sweep would cost both.

Drivers
-------
These panels are sold under many names and there is no common protocol, so
the encoder is chosen at runtime from a fingerprint (advertised name plus the
GATT characteristics the panel exposes).  ``matrix_probe.py`` prints that
fingerprint, and ``FAMILIES`` below maps it to a driver.

Until a panel has been fingerprinted on real hardware its driver is marked
``confirmed = False``.  Everything above the driver -- the queue, the API, the
UI -- is protocol independent, so confirming a panel is a change to one class.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("vicelights.matrix")

# ---------------------------------------------------------------- text modes

# What the panel should do with a message once it has it.  Not every panel
# supports every mode; a driver maps these onto whatever its firmware has and
# falls back to "scroll" rather than refusing.
MODES = ("scroll", "static", "marquee", "flash", "fade")
DEFAULT_MODE = "scroll"

MAX_TEXT = 240          # characters; longer messages get truncated, not dropped
MAX_MESSAGES = 40       # playlist ceiling, so a stuck client cannot fill the SD card

# -------------------------------------------------------------------- 5x7 font
#
# Five column bytes per glyph, bit 0 = top row, for printable ASCII 0x20-0x7E.
# The panel needs a bitmap and the Pi has no font stack worth depending on here
# (the kiosk's pygame fonts live in another process, and Pillow is not
# installed), so the font is data in this file.  Anything outside the range
# renders as a filled box rather than vanishing, so a bad character is visible
# on the panel instead of silently shortening the message.

FONT5X7 = (
    (0x00, 0x00, 0x00, 0x00, 0x00),  # space
    (0x00, 0x00, 0x5F, 0x00, 0x00),  # !
    (0x00, 0x07, 0x00, 0x07, 0x00),  # "
    (0x14, 0x7F, 0x14, 0x7F, 0x14),  # #
    (0x24, 0x2A, 0x7F, 0x2A, 0x12),  # $
    (0x23, 0x13, 0x08, 0x64, 0x62),  # %
    (0x36, 0x49, 0x55, 0x22, 0x50),  # &
    (0x00, 0x05, 0x03, 0x00, 0x00),  # '
    (0x00, 0x1C, 0x22, 0x41, 0x00),  # (
    (0x00, 0x41, 0x22, 0x1C, 0x00),  # )
    (0x14, 0x08, 0x3E, 0x08, 0x14),  # *
    (0x08, 0x08, 0x3E, 0x08, 0x08),  # +
    (0x00, 0x50, 0x30, 0x00, 0x00),  # ,
    (0x08, 0x08, 0x08, 0x08, 0x08),  # -
    (0x00, 0x60, 0x60, 0x00, 0x00),  # .
    (0x20, 0x10, 0x08, 0x04, 0x02),  # /
    (0x3E, 0x51, 0x49, 0x45, 0x3E),  # 0
    (0x00, 0x42, 0x7F, 0x40, 0x00),  # 1
    (0x42, 0x61, 0x51, 0x49, 0x46),  # 2
    (0x21, 0x41, 0x45, 0x4B, 0x31),  # 3
    (0x18, 0x14, 0x12, 0x7F, 0x10),  # 4
    (0x27, 0x45, 0x45, 0x45, 0x39),  # 5
    (0x3C, 0x4A, 0x49, 0x49, 0x30),  # 6
    (0x01, 0x71, 0x09, 0x05, 0x03),  # 7
    (0x36, 0x49, 0x49, 0x49, 0x36),  # 8
    (0x06, 0x49, 0x49, 0x29, 0x1E),  # 9
    (0x00, 0x36, 0x36, 0x00, 0x00),  # :
    (0x00, 0x56, 0x36, 0x00, 0x00),  # ;
    (0x00, 0x08, 0x14, 0x22, 0x41),  # <
    (0x14, 0x14, 0x14, 0x14, 0x14),  # =
    (0x41, 0x22, 0x14, 0x08, 0x00),  # >
    (0x02, 0x01, 0x51, 0x09, 0x06),  # ?
    (0x32, 0x49, 0x79, 0x41, 0x3E),  # @
    (0x7E, 0x11, 0x11, 0x11, 0x7E),  # A
    (0x7F, 0x49, 0x49, 0x49, 0x36),  # B
    (0x3E, 0x41, 0x41, 0x41, 0x22),  # C
    (0x7F, 0x41, 0x41, 0x22, 0x1C),  # D
    (0x7F, 0x49, 0x49, 0x49, 0x41),  # E
    (0x7F, 0x09, 0x09, 0x01, 0x01),  # F
    (0x3E, 0x41, 0x41, 0x51, 0x32),  # G
    (0x7F, 0x08, 0x08, 0x08, 0x7F),  # H
    (0x00, 0x41, 0x7F, 0x41, 0x00),  # I
    (0x20, 0x40, 0x41, 0x3F, 0x01),  # J
    (0x7F, 0x08, 0x14, 0x22, 0x41),  # K
    (0x7F, 0x40, 0x40, 0x40, 0x40),  # L
    (0x7F, 0x02, 0x04, 0x02, 0x7F),  # M
    (0x7F, 0x04, 0x08, 0x10, 0x7F),  # N
    (0x3E, 0x41, 0x41, 0x41, 0x3E),  # O
    (0x7F, 0x09, 0x09, 0x09, 0x06),  # P
    (0x3E, 0x41, 0x51, 0x21, 0x5E),  # Q
    (0x7F, 0x09, 0x19, 0x29, 0x46),  # R
    (0x46, 0x49, 0x49, 0x49, 0x31),  # S
    (0x01, 0x01, 0x7F, 0x01, 0x01),  # T
    (0x3F, 0x40, 0x40, 0x40, 0x3F),  # U
    (0x1F, 0x20, 0x40, 0x20, 0x1F),  # V
    (0x7F, 0x20, 0x18, 0x20, 0x7F),  # W
    (0x63, 0x14, 0x08, 0x14, 0x63),  # X
    (0x03, 0x04, 0x78, 0x04, 0x03),  # Y
    (0x61, 0x51, 0x49, 0x45, 0x43),  # Z
    (0x00, 0x00, 0x7F, 0x41, 0x41),  # [
    (0x02, 0x04, 0x08, 0x10, 0x20),  # backslash
    (0x41, 0x41, 0x7F, 0x00, 0x00),  # ]
    (0x04, 0x02, 0x01, 0x02, 0x04),  # ^
    (0x40, 0x40, 0x40, 0x40, 0x40),  # _
    (0x00, 0x01, 0x02, 0x04, 0x00),  # `
    (0x20, 0x54, 0x54, 0x54, 0x78),  # a
    (0x7F, 0x48, 0x44, 0x44, 0x38),  # b
    (0x38, 0x44, 0x44, 0x44, 0x20),  # c
    (0x38, 0x44, 0x44, 0x48, 0x7F),  # d
    (0x38, 0x54, 0x54, 0x54, 0x18),  # e
    (0x08, 0x7E, 0x09, 0x01, 0x02),  # f
    (0x08, 0x14, 0x54, 0x54, 0x3C),  # g
    (0x7F, 0x08, 0x04, 0x04, 0x78),  # h
    (0x00, 0x44, 0x7D, 0x40, 0x00),  # i
    (0x20, 0x40, 0x44, 0x3D, 0x00),  # j
    (0x00, 0x7F, 0x10, 0x28, 0x44),  # k
    (0x00, 0x41, 0x7F, 0x40, 0x00),  # l
    (0x7C, 0x04, 0x18, 0x04, 0x78),  # m
    (0x7C, 0x08, 0x04, 0x04, 0x78),  # n
    (0x38, 0x44, 0x44, 0x44, 0x38),  # o
    (0x7C, 0x14, 0x14, 0x14, 0x08),  # p
    (0x08, 0x14, 0x14, 0x18, 0x7C),  # q
    (0x7C, 0x08, 0x04, 0x04, 0x08),  # r
    (0x48, 0x54, 0x54, 0x54, 0x20),  # s
    (0x04, 0x3F, 0x44, 0x40, 0x20),  # t
    (0x3C, 0x40, 0x40, 0x20, 0x7C),  # u
    (0x1C, 0x20, 0x40, 0x20, 0x1C),  # v
    (0x3C, 0x40, 0x30, 0x40, 0x3C),  # w
    (0x44, 0x28, 0x10, 0x28, 0x44),  # x
    (0x0C, 0x50, 0x50, 0x50, 0x3C),  # y
    (0x44, 0x64, 0x54, 0x4C, 0x44),  # z
    (0x00, 0x08, 0x36, 0x41, 0x00),  # {
    (0x00, 0x00, 0x7F, 0x00, 0x00),  # |
    (0x00, 0x41, 0x36, 0x08, 0x00),  # }
    (0x08, 0x08, 0x2A, 0x1C, 0x08),  # ~
)

FONT_HEIGHT = 7
FONT_WIDTH = 5
MISSING_GLYPH = (0x7F, 0x41, 0x41, 0x41, 0x7F)   # hollow box: visible, not silent


def glyph(char: str):
    """Five column bytes for one character."""
    index = ord(char) - 0x20
    if 0 <= index < len(FONT5X7):
        return FONT5X7[index]
    return MISSING_GLYPH


def render_columns(text: str, spacing: int = 1) -> list:
    """Render text to a list of column bytes, bit 0 = top row.

    Columns rather than rows because that is the axis a scrolling panel
    consumes: shifting a message left is dropping one column off the front.
    """
    columns = []
    for index, char in enumerate(text):
        if index:
            columns.extend([0] * spacing)
        columns.extend(glyph(char))
    return columns


def render_bitmap(text: str, height: int = FONT_HEIGHT, spacing: int = 1) -> list:
    """Render to rows of 0/1, top row first, vertically centred in ``height``."""
    columns = render_columns(text, spacing)
    top = max(0, (height - FONT_HEIGHT) // 2)
    rows = []
    for y in range(height):
        source = y - top
        if 0 <= source < FONT_HEIGHT:
            rows.append([(column >> source) & 1 for column in columns])
        else:
            rows.append([0] * len(columns))
    return rows


def text_width(text: str, spacing: int = 1) -> int:
    if not text:
        return 0
    return len(text) * FONT_WIDTH + (len(text) - 1) * spacing


def preview(text: str, on: str = "#", off: str = ".") -> str:
    """ASCII rendering of a message, for the probe tool and the log.

    Being able to see the bitmap without the panel in front of you is what
    makes a wrong glyph a five-second fix instead of a trip to the sign.
    """
    return "\n".join("".join(on if bit else off for bit in row)
                     for row in render_bitmap(text))


# ------------------------------------------------------------------- messages

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_color(value, default=(255, 255, 255)) -> tuple:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(max(0, min(255, int(component))) for component in value)
    match = _HEX.match(str(value or "").strip())
    if not match:
        return default
    raw = match.group(1)
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def format_color(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb)


def normalize_message(raw: dict, default_dwell: float = 20.0) -> dict:
    """One message, cleaned up enough that no driver has to defend itself.

    Dwell is how long the panel holds this message before the playlist moves
    on, and it is per message rather than one global interval: "BAR IS OPEN"
    wants longer on screen than a three-letter shout.
    """
    from .config import new_id

    raw = raw or {}
    text = str(raw.get("text") or "").replace("\r", " ").replace("\n", " ")
    text = text[:MAX_TEXT]
    mode = str(raw.get("mode") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        mode = DEFAULT_MODE
    try:
        dwell = float(raw.get("dwell", default_dwell))
    except (TypeError, ValueError):
        dwell = default_dwell
    try:
        speed = int(raw.get("speed", 50))
    except (TypeError, ValueError):
        speed = 50
    return {
        "id": raw.get("id") or new_id(),
        "text": text,
        "color": format_color(parse_color(raw.get("color"), (255, 47, 110))),
        "background": format_color(parse_color(raw.get("background"), (0, 0, 0))),
        "mode": mode,
        "speed": max(0, min(100, speed)),
        # 0 means "hold until something else is sent" -- a single standing
        # message rather than a rotation.
        "dwell": max(0.0, min(3600.0, dwell)),
        "enabled": bool(raw.get("enabled", True)),
    }


def describe_message(message: dict) -> str:
    text = (message or {}).get("text") or ""
    short = text if len(text) <= 24 else text[:23] + "…"
    return "%r %s" % (short, (message or {}).get("mode", DEFAULT_MODE))


# -------------------------------------------------------------------- drivers

class MatrixDriver:
    """Turn an intention into BLE frames for one family of panel.

    Every method returns a list of ``bytes`` small enough to write one at a
    time; chunking for the MTU happens here, not in the BLE worker, because
    only the driver knows whether a payload may be split and where.
    """

    key = "generic"
    label = "Generic panel"
    # Set true only once the encoding has been checked against real hardware.
    confirmed = False
    # Advertised-name prefixes and the GATT characteristic to write to.
    name_prefixes = ()
    write_uuid = None
    notify_uuid = None
    chunk = 20              # payload bytes per write at the default 23-byte MTU

    def __init__(self, config: dict = None):
        self.config = dict(config or {})

    # -- capabilities the API advertises so the UI can hide what is missing
    @property
    def capabilities(self) -> dict:
        return {
            "text": True,
            "power": bool(self.power_frames(True)),
            "brightness": bool(self.brightness_frames(100)),
            "clear": bool(self.clear_frames()),
            "confirmed": self.confirmed,
        }

    def characteristic(self):
        return self.config.get("char_uuid") or self.write_uuid

    # -- encoders; a driver that cannot do one of these returns []
    def text_frames(self, message: dict) -> list:
        raise NotImplementedError

    def power_frames(self, on: bool) -> list:
        return []

    def brightness_frames(self, percent: int) -> list:
        return []

    def clear_frames(self) -> list:
        return []

    # -- helper shared by every length-prefixed family
    @staticmethod
    def _chunked(payload: bytes, size: int) -> list:
        return [payload[at:at + size] for at in range(0, len(payload), size)] or [b""]


class IDotMatrix(MatrixDriver):
    """iDotMatrix / "IDM-" pixel panels.

    The commonest BLE matrix sold for this job.  The short control commands
    below are length-prefixed five-byte packets and are the first thing to
    test with ``matrix_probe.py send``: if the screen blanks and comes back,
    the family is right and only the text encoder is in question.

    ``confirmed`` stays False until the text path has been seen to work on the
    panel in hand.  The queue does not care -- an unconfirmed driver still
    queues, still shows in the UI, and still reports what it sent -- so the
    only thing gated on confirmation is how loudly the status API hedges.
    """

    key = "idotmatrix"
    label = "iDotMatrix (IDM-*)"
    confirmed = False
    name_prefixes = ("idm-", "idotmatrix")
    write_uuid = "0000fa02-0000-1000-8000-00805f9b34fb"
    notify_uuid = "0000fa03-0000-1000-8000-00805f9b34fb"

    MODE_VALUES = {"scroll": 0, "static": 1, "marquee": 2, "flash": 3, "fade": 4}

    def power_frames(self, on: bool) -> list:
        return [bytes([0x05, 0x00, 0x07, 0x01, 0x01 if on else 0x00])]

    def brightness_frames(self, percent: int) -> list:
        level = max(5, min(100, int(percent)))
        return [bytes([0x05, 0x00, 0x04, 0x80, level])]

    def clear_frames(self) -> list:
        # Off then on is the portable way to blank one of these: it needs no
        # knowledge of the drawing commands and every unit in the family has it.
        return self.power_frames(False) + self.power_frames(True)

    def text_frames(self, message: dict) -> list:
        payload = self._text_payload(message)
        return self._chunked(payload, self.chunk)

    def _text_payload(self, message: dict) -> bytes:
        """Header describing how to show the message, then the bitmap.

        The header layout is the part still to be checked against hardware --
        see the module note.  The bitmap after it is our own font, so what the
        panel shows is under our control either way.
        """
        colour = parse_color(message.get("color"))
        background = parse_color(message.get("background"), (0, 0, 0))
        mode = self.MODE_VALUES.get(message.get("mode"), 0)
        speed = max(0, min(100, int(message.get("speed", 50))))
        text = message.get("text") or ""
        bitmap = self._bitmap(text)

        body = bytearray()
        body += bytes([0x00, 0x00])          # placeholder for total length
        body += bytes([0x03, 0x00, 0x00, 0x00, 0x00])
        body += bytes([mode, speed])
        body += bytes([0x01]) + bytes(colour)
        body += bytes([0x00]) + bytes(background)
        count = len(text)
        body += bytes([count & 0xFF, (count >> 8) & 0xFF])
        body += bitmap
        total = len(body)
        body[0] = total & 0xFF
        body[1] = (total >> 8) & 0xFF
        return bytes(body)

    def _bitmap(self, text: str) -> bytes:
        """One 16x16 cell per character, packed row-major, 2 bytes per row."""
        out = bytearray()
        for char in text:
            columns = glyph(char)
            for row in range(16):
                source = row - 4          # centre the 7-row font in 16
                bits = 0
                if 0 <= source < FONT_HEIGHT:
                    for index, column in enumerate(columns):
                        if (column >> source) & 1:
                            bits |= 1 << (index + 5)   # centre 5 cols in 16
                out += bytes([bits & 0xFF, (bits >> 8) & 0xFF])
        return bytes(out)


class RawDriver(MatrixDriver):
    """A panel whose protocol we captured rather than recognised.

    Commands come from config as hex strings, so a protocol lifted off an
    HCI capture (``matrix_probe.py btsnoop``) can be put into service by
    editing the config file -- no code change, no deploy, at the sign with a
    phone if it comes to that.

    ``{level}`` in a brightness template is replaced with one byte.
    """

    key = "raw"
    label = "Captured protocol"
    confirmed = True        # if it was captured off the wire, it is the truth

    def __init__(self, config=None):
        super().__init__(config)
        self.commands = dict((self.config.get("commands") or {}))
        self.chunk = int(self.config.get("chunk") or 20)

    def _sequence(self, name: str, level: int = None) -> list:
        raw = self.commands.get(name)
        if not raw:
            return []
        if isinstance(raw, str):
            raw = [raw]
        frames = []
        for entry in raw:
            cleaned = re.sub(r"[^0-9a-fA-F{}a-z:]", "", str(entry))
            if level is not None:
                cleaned = cleaned.replace("{level}", "%02x" % max(0, min(255, int(level))))
            cleaned = re.sub(r"[^0-9a-fA-F]", "", cleaned)
            if len(cleaned) % 2:
                log.warning("raw command %r has an odd number of hex digits; ignoring", name)
                continue
            frames.append(bytes.fromhex(cleaned))
        return frames

    @property
    def capabilities(self) -> dict:
        caps = super().capabilities
        caps["text"] = bool(self.commands.get("text_prefix") or self.commands.get("text"))
        return caps

    def power_frames(self, on: bool) -> list:
        return self._sequence("power_on" if on else "power_off")

    def brightness_frames(self, percent: int) -> list:
        return self._sequence("brightness", level=int(percent))

    def clear_frames(self) -> list:
        return self._sequence("clear") or (self.power_frames(False) + self.power_frames(True))

    def text_frames(self, message: dict) -> list:
        """Prefix and suffix from the capture, our bitmap in the middle.

        A capture gives the framing; it cannot give the message the user has
        not typed yet.  So the captured bytes either side are replayed as-is
        and the payload between them is rendered here.
        """
        literal = self._sequence("text")
        if literal:
            return self._chunked(b"".join(literal), self.chunk)
        prefix = b"".join(self._sequence("text_prefix"))
        suffix = b"".join(self._sequence("text_suffix"))
        rows = render_bitmap(message.get("text") or "",
                             height=int(self.config.get("height") or FONT_HEIGHT))
        body = bytearray()
        for row in rows:
            packed, bit = 0, 0
            for value in row:
                packed |= value << bit
                bit += 1
                if bit == 8:
                    body.append(packed)
                    packed, bit = 0, 0
            if bit:
                body.append(packed)
        return self._chunked(prefix + bytes(body) + suffix, self.chunk)


FAMILIES = {driver.key: driver for driver in (IDotMatrix, RawDriver)}
DEFAULT_FAMILY = "auto"


def family_names() -> list:
    return [{"key": key, "label": cls.label, "confirmed": cls.confirmed}
            for key, cls in FAMILIES.items()]


def identify(name: str = "", char_uuids=(), service_uuids=()) -> str:
    """Best guess at which family a panel belongs to.

    Name first because it is available from a scan without connecting, then
    the characteristic UUIDs, which need a connect but do not lie.
    """
    lowered = (name or "").strip().lower()
    for key, cls in FAMILIES.items():
        if cls.name_prefixes and any(lowered.startswith(p) for p in cls.name_prefixes):
            return key
    known = {str(u).lower() for u in list(char_uuids) + list(service_uuids)}
    for key, cls in FAMILIES.items():
        if cls.write_uuid and cls.write_uuid.lower() in known:
            return key
    return ""


def driver_for(matrix: dict) -> MatrixDriver:
    """The driver for a configured panel, resolving ``auto`` by fingerprint."""
    matrix = dict(matrix or {})
    family = (matrix.get("family") or DEFAULT_FAMILY).strip().lower()
    if family in ("", DEFAULT_FAMILY):
        family = identify(matrix.get("name"), [matrix.get("char_uuid") or ""]) or ""
    cls = FAMILIES.get(family)
    if cls is None:
        # Unrecognised, but a captured command set may still be present.
        cls = RawDriver
    return cls(matrix)


def looks_like_panel(name: str) -> bool:
    """Would this scan result be worth connecting to?

    Deliberately loose: it decides what the pairing screen offers, and a panel
    missing from that list is harder to explain than one extra row.
    """
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    if identify(lowered):
        return True
    return any(hint in lowered for hint in
               ("matrix", "pixel", "badge", "screen", "display", "led-", "sign"))
