"""ELK-BLEDOM frame definitions.

Single source of truth for the wire protocol.  Both the web service and the
standalone ``elk_scan.py`` CLI import from here so there is exactly one place
where the command bytes live.

Frame layout is always 9 bytes::

    7e 00 <cmd> <a> <b> <c> <d> 00 ef

Confirmed on the sign's hardware (lights observed changing)
----------------------------------------------------------
power on       7e 00 04 f0 00 01 ff 00 ef
power off      7e 00 04 00 00 00 ff 00 ef      variant 0, all 12 went dark
solid colour   7e 00 05 03 RR GG BB 00 ef      all 12 lit one at a time
                                               during the identify walk

0000fff3 is write-without-response, so nothing is acknowledged and an encoding
a unit does not recognise is dropped in silence. That is why these are marked
by what was seen, not by whether the write returned.

Widely reported for ELK-BLEDOM, but *not* honoured by every clone
-----------------------------------------------------------------
brightness     7e 00 01 BB 00 00 00 00 ef      BB = 0..100
mode speed     7e 00 02 SS 00 00 00 00 ef      SS = 0..100
built-in mode  7e 00 03 MM 03 00 00 00 ef      MM = 0x80..0x9d

Units that ignore an unknown command simply drop the frame, so sending one of
the uncertain frames is harmless -- that is the "degrade gracefully" strategy.
For brightness the default policy is ``scale`` (dim by scaling RGB host-side),
which cannot fail on an analog controller.  See ``build_frames``.
"""

from __future__ import annotations

HEADER = 0x7E
TAIL = 0xEF

# Characteristics we prefer, in order, before falling back to "first writable".
PREFERRED_CHAR_UUIDS = (
    "0000fff3-0000-1000-8000-00805f9b34fb",
    "0000ffe1-0000-1000-8000-00805f9b34fb",
)

# Name prefixes used by these controllers (case-insensitive match).
NAME_PREFIXES = ("elk-bledom", "elk-ble", "elkbledom", "melk", "ledble")

BRIGHTNESS_MODES = ("scale", "native", "both")

# Per-device channel order. These controllers are cheap and the RGB pads are not
# always wired the way the firmware assumes, so asking for red can produce green.
# The string names which physical colour each frame byte actually drives, in
# byte order: "grb" means byte 0 drives green, byte 1 red, byte 2 blue. Feeding
# our (r, g, b) through it puts each value in the byte that lights it.
CHANNEL_ORDERS = ("rgb", "rbg", "grb", "gbr", "brg", "bgr")


def apply_channel_order(rgb, order: str = "rgb"):
    order = (order or "rgb").strip().lower()
    if order == "rgb" or order not in CHANNEL_ORDERS:
        return tuple(rgb)
    value = {"r": rgb[0], "g": rgb[1], "b": rgb[2]}
    return tuple(value[channel] for channel in order)

# Built-in animation modes.  Value -> friendly name.
MODES = {
    0x80: "Static red",
    0x81: "Static blue",
    0x82: "Static green",
    0x83: "Static cyan",
    0x84: "Static yellow",
    0x85: "Static magenta",
    0x86: "Static white",
    0x87: "Jump RGB",
    0x88: "Jump 7 colour",
    0x89: "Fade RGB",
    0x8A: "Fade red",
    0x8B: "Fade green",
    0x8C: "Fade blue",
    0x8D: "Fade yellow",
    0x8E: "Fade cyan",
    0x8F: "Fade magenta",
    0x90: "Fade white",
    0x91: "Fade 7 colour",
    0x92: "Strobe 7 colour",
    0x93: "Strobe red",
    0x94: "Strobe green",
    0x95: "Strobe blue",
    0x96: "Strobe yellow",
    0x97: "Strobe cyan",
    0x98: "Strobe magenta",
    0x99: "Strobe white",
    0x9A: "Flash 7 colour",
    0x9B: "Flash red",
    0x9C: "Flash green",
    0x9D: "Flash blue",
}


def _frame(cmd: int, a: int = 0, b: int = 0, c: int = 0, d: int = 0) -> bytes:
    return bytes([HEADER, 0x00, cmd, a & 0xFF, b & 0xFF, c & 0xFF, d & 0xFF, 0x00, TAIL])


def color_frame(r: int, g: int, b: int) -> bytes:
    """7e 00 05 03 RR GG BB 00 ef"""
    return _frame(0x05, 0x03, r, g, b)


# Reported off frames, in the order we try them. Variant 0 is the documented
# one; the others turn up in other ELK-BLEDOM implementations. A unit that does
# not understand a frame drops it silently, so trying alternates costs nothing
# but a write. See `elk_scan.py off --variant N`.
POWER_OFF_VARIANTS = (
    bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0xFF, 0x00, 0xEF]),
    bytes([0x7E, 0x00, 0x04, 0x00, 0x00, 0x00, 0x00, 0x00, 0xEF]),
    bytes([0x7E, 0x00, 0x05, 0x03, 0x00, 0x00, 0x00, 0x00, 0xEF]),  # black, not off
)

POWER_ON_VARIANTS = (
    bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0xFF, 0x00, 0xEF]),
    bytes([0x7E, 0x00, 0x04, 0xF0, 0x00, 0x01, 0x00, 0x00, 0xEF]),
)


def power_frame(on: bool, variant: int = 0) -> bytes:
    """7e 00 04 f0 00 01 ff 00 ef / 7e 00 04 00 00 00 ff 00 ef

    ``variant`` selects an alternate encoding for units that ignore the
    documented one; 0 is the default everywhere in the service.
    """
    table = POWER_ON_VARIANTS if on else POWER_OFF_VARIANTS
    return table[clamp(variant, 0, len(table) - 1)]


def brightness_frame(percent: int) -> bytes:
    """7e 00 01 BB 00 00 00 00 ef -- BB is 0..100, not 0..255."""
    return _frame(0x01, clamp(percent, 0, 100))


def speed_frame(percent: int) -> bytes:
    """7e 00 02 SS 00 00 00 00 ef -- animation speed, 0..100."""
    return _frame(0x02, clamp(percent, 0, 100))


def mode_frame(mode: int) -> bytes:
    """7e 00 03 MM 03 00 00 00 ef -- MM is one of MODES."""
    return _frame(0x03, mode & 0xFF, 0x03)


def clamp(value, low, high):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = low
    return max(low, min(high, value))


def scale_rgb(rgb, percent: int):
    """Dim a colour host-side.  Always works, even on units that ignore 0x01."""
    percent = clamp(percent, 0, 100)
    r, g, b = (clamp(c, 0, 255) for c in rgb)
    f = percent / 100.0
    return (int(round(r * f)), int(round(g * f)), int(round(b * f)))


def parse_color(value) -> tuple:
    """Accept '#rrggbb', 'rrggbb', [r,g,b] or (r,g,b)."""
    if value is None:
        return (255, 255, 255)
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        if len(s) != 6:
            raise ValueError("colour must be #rrggbb")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(clamp(c, 0, 255) for c in value)
    raise ValueError("colour must be #rrggbb or [r,g,b]")


def format_color(rgb) -> str:
    r, g, b = rgb
    return "#%02x%02x%02x" % (clamp(r, 0, 255), clamp(g, 0, 255), clamp(b, 0, 255))


def build_frames(state: dict, brightness_mode: str = "scale") -> list:
    """Turn a light state dict into the ordered list of frames to write.

    ``state`` keys (all optional):
        power       bool
        color       '#rrggbb' or [r,g,b]
        brightness  0..100
        mode        int 0x80..0x9d (built-in animation; overrides colour)
        speed       0..100 (only meaningful alongside a mode)

    Ordering matters: power first (a unit that is off ignores colour), then the
    dimming/colour payload.  Frames the unit does not understand are ignored by
    the unit, so an unsupported brightness/mode frame costs one write and
    nothing else.
    """
    if brightness_mode not in BRIGHTNESS_MODES:
        brightness_mode = "scale"

    frames = []
    power = state.get("power")

    if power is False:
        return [power_frame(False)]
    if power is True or power is None:
        frames.append(power_frame(True))

    brightness = state.get("brightness")
    if brightness is None:
        brightness = 100
    brightness = clamp(brightness, 0, 100)

    mode = state.get("mode")
    if mode not in (None, "", "none"):
        mode = clamp(mode, 0x80, 0x9D)
        if brightness_mode in ("native", "both"):
            frames.append(brightness_frame(brightness))
        frames.append(mode_frame(mode))
        speed = state.get("speed")
        if speed is not None:
            frames.append(speed_frame(clamp(speed, 0, 100)))
        return frames

    rgb = parse_color(state.get("color", "#ffffff"))
    if brightness_mode in ("native", "both"):
        frames.append(brightness_frame(brightness))
    if brightness_mode in ("scale", "both"):
        rgb = scale_rgb(rgb, brightness)
    frames.append(color_frame(*rgb))
    return frames


def describe_frames(frames) -> str:
    return " | ".join(f.hex(" ") for f in frames)


def pick_characteristic(services):
    """Choose the characteristic to write to.

    Prefers 0000fff3-... then 0000ffe1-..., else the first writable
    characteristic found.  ``services`` is a bleak ``BleakGATTServiceCollection``
    (or anything iterable of services with ``.characteristics``).

    Returns ``(uuid, write_without_response)`` or ``(None, False)``.
    """
    writable = []
    for service in services:
        for char in service.characteristics:
            props = set(char.properties or ())
            if "write" in props or "write-without-response" in props:
                writable.append((char.uuid.lower(), "write-without-response" in props))

    for preferred in PREFERRED_CHAR_UUIDS:
        for uuid, wwr in writable:
            if uuid == preferred:
                return uuid, wwr
    # Short-form match (some stacks report 16-bit UUIDs).
    for preferred in PREFERRED_CHAR_UUIDS:
        short = preferred[4:8]
        for uuid, wwr in writable:
            if uuid.startswith("0000" + short) or uuid == short:
                return uuid, wwr

    if writable:
        return writable[0]
    return None, False


def looks_like_elk(name: str) -> bool:
    if not name:
        return False
    lowered = name.strip().lower()
    return any(lowered.startswith(prefix) for prefix in NAME_PREFIXES)
