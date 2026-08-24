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
# Where the four colour bytes of a set-pixel command actually go. Four
# characters, one per byte: r, g, b, and a for one that does nothing visible.
#
# The published protocol says [R][G][B][A]. This sign's panel does not agree.
# Sent pure red, green and blue while still using [R][G][B][A] it showed blue,
# cyan and magenta, which solves to AGRB -- and AGRB was wrong too: with it in
# place the panel shows green for red. Both readings cannot be right, and the
# one to trust is the sign in front of you, so the layout is no longer derived
# from a remembered description of a photo. ``/api/matrix/colortest`` puts one
# block on the panel at a time and asks what colour it is; ``solve_layout``
# turns those three answers into the wiring. One block at a time on purpose:
# three bands at once and the answer depends on which end the reader started.
#
# This is a layout, not a channel order, and the difference matters: no
# permutation of the first three bytes can move a value into the fourth.
PIXEL_LAYOUTS = tuple("".join(order) for order in
                      __import__("itertools").permutations("rgba"))
DEFAULT_PIXEL_LAYOUT = "agrb"

# The colours the check sends, in order. Primaries only: a panel with two
# channels crossed still shows a primary, so the answer is a name someone can
# pick off a list rather than a judgement about a shade.
TEST_COLOURS = ("red", "green", "blue")

COLOUR_NAMES = {
    "off": (0, 0, 0),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "white": (255, 255, 255),
}


def pixel_bytes(rgb, layout: str = DEFAULT_PIXEL_LAYOUT, alpha: int = 0xFF):
    """The four colour bytes of a set-pixel command, in this panel's order."""
    if layout not in PIXEL_LAYOUTS:
        layout = DEFAULT_PIXEL_LAYOUT
    source = {"r": rgb[0], "g": rgb[1], "b": rgb[2], "a": alpha}
    return [source[channel] for channel in layout]


def perceived(rgb, sending_layout: str, panel_layout: str, alpha: int = 0xFF):
    """What a panel wired ``panel_layout`` lights up when we send ``rgb``.

    The alpha byte is part of this, not an aside: send with the wrong layout
    and a hard-coded 255 alpha lands in a colour channel, which is why the
    first attempt at this panel lit blue for every colour sent.
    """
    values = pixel_bytes(rgb, sending_layout, alpha)
    shown = {"r": 0, "g": 0, "b": 0, "a": 0}
    for index, channel in enumerate(panel_layout):
        shown[channel] = values[index]
    return (shown["r"], shown["g"], shown["b"])


def colour_name(rgb) -> str:
    """The nearest of the eight names a person can pick off a list."""
    best, distance = "off", None
    for name, value in COLOUR_NAMES.items():
        gap = sum((a - b) ** 2 for a, b in zip(rgb, value))
        if distance is None or gap < distance:
            best, distance = name, gap
    return best


def solve_layout(seen, sending_layout: str = None) -> list:
    """Which wirings would show ``seen`` for TEST_COLOURS, sent as we send now.

    Returns every layout that explains all three answers -- usually one. More
    than one means the answers do not pin it down; none means they contradict
    each other, which is worth saying rather than saving a guess.
    """
    sending_layout = sending_layout if sending_layout in PIXEL_LAYOUTS \
        else DEFAULT_PIXEL_LAYOUT
    answers = [str(name or "").strip().lower() for name in seen]
    if len(answers) != len(TEST_COLOURS):
        raise ValueError("expected %d answers, got %d"
                         % (len(TEST_COLOURS), len(answers)))
    for name in answers:
        if name not in COLOUR_NAMES:
            raise ValueError("%r is not one of: %s"
                             % (name, ", ".join(sorted(COLOUR_NAMES))))
    matches = []
    for candidate in PIXEL_LAYOUTS:
        if all(colour_name(perceived(COLOUR_NAMES[sent], sending_layout,
                                     candidate)) == answer
               for sent, answer in zip(TEST_COLOURS, answers)):
            matches.append(candidate)
    return matches


MODES = ("scroll", "static", "marquee", "flash", "fade")
DEFAULT_MODE = "static"

# Which of those a driver can actually deliver. Every one of them means moving
# or repainting pixels, and on a panel drawn a pixel at a time over BLE that is
# not a matter of writing the code: measured on this sign, one connect costs
# 1.5s and one disconnect 0.2s, so even the cheapest animation -- a flash done
# with the panel's own one-packet power command -- is ~1.8s of radio per step.
# At a two-second flash the panel holds the antenna nine tenths of the time,
# and it shares that antenna with the twelve controllers. Scrolling is worse
# again: a frame moves nearly every lit pixel, which is ~29 writes, 1.7 frames
# a second, forever.
#
# So this driver offers one mode, and the UI offers what the driver offers.
# A menu of five that all draw the same thing is worse than a menu of one.
STATIC_ONLY = ("static",)

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


def embolden(columns: list) -> list:
    """Widen every stroke by one column, in place.

    Each column is OR'd with the one before it, so a one-pixel stem becomes
    two. It does not close the holes in o, e or a -- those are two columns
    wide in this font, and only one of them fills.

    Needed because stretching to a 16-row panel makes horizontal strokes two
    or three LEDs tall while vertical ones stay a single LED wide, and that
    imbalance is what makes small text hard to read. Bolding evens it up. The
    strokes bleed one column right, so the caller widens the letter spacing to
    match or the letters touch.
    """
    return [column | (columns[index - 1] if index else 0)
            for index, column in enumerate(columns)]


def render_bitmap(text: str, height: int = FONT_HEIGHT, spacing: int = 1,
                  scale: int = 1, stretch: bool = False,
                  bold: bool = False) -> list:
    """Render to rows of 0/1, top row first, vertically centred in ``height``.

    ``scale`` repeats each pixel, so a 5x7 glyph at scale 2 is 10x14. Whole
    numbers, because one font pixel is one LED and a fractional scale would
    have to interpolate, turning a crisp letter into a smear.

    Which is why 2x on a 16-row panel leaves two rows dark: 7 does not divide
    16. ``stretch`` fills them by mapping the seven source rows across the full
    height instead, so some rows are drawn three times and some twice. The
    letters reach the edges; the cost is that a horizontal stroke can be three
    LEDs thick at the top of a glyph and two in the middle. Worth it on a
    sixteen-row panel, and a matter of taste, so it is a setting.
    """
    scale = max(1, int(scale))
    columns = render_columns(text, spacing)
    if bold:
        columns = embolden(columns)
    rows = [[(column >> y) & 1 for column in columns] for y in range(FONT_HEIGHT)]
    if scale > 1:
        rows = [[bit for bit in row for _ in range(scale)] for row in rows]
    blank = [0] * (len(rows[0]) if rows else 0)

    if stretch and height >= FONT_HEIGHT:
        # Nearest source row for each output row: fills the height exactly.
        return [list(rows[min(FONT_HEIGHT - 1, y * FONT_HEIGHT // height)])
                for y in range(height)]

    rows = [row for row in rows for _ in range(scale)]
    glyph_height = FONT_HEIGHT * scale
    top = max(0, (height - glyph_height) // 2)
    out = []
    for y in range(height):
        source = y - top
        out.append(list(rows[source]) if 0 <= source < glyph_height else list(blank))
    return out


def text_width(text: str, spacing: int = 1, scale: int = 1) -> int:
    if not text:
        return 0
    return (len(text) * FONT_WIDTH + (len(text) - 1) * spacing) * max(1, int(scale))


BOLD_SPACING = 2        # bolding bleeds a column right, so letters need room
PLAIN_SPACING = 1


def layout_for(config: dict, text: str) -> dict:
    """How this text will be drawn on this panel: scale, bold, spacing, width.

    One place decides all four because they are not independent. Bolding only
    helps at 1x -- at 2x the strokes are already two LEDs wide -- and bolding
    needs wider letter spacing, which changes what fits, which changes the
    scale. Working them out separately gets that circle wrong.
    """
    config = config or {}
    text = text or ""
    try:
        width = max(4, min(256, int(config.get("width") or 96)))
        height = max(4, min(256, int(config.get("height") or 16)))
    except (TypeError, ValueError):
        width, height = 96, 16

    want_bold = str(config.get("bold", "auto")).strip().lower()
    allowed = want_bold not in ("0", "false", "no", "off")
    forced = want_bold in ("1", "true", "yes", "on")

    def measure(scale):
        bold = allowed and (scale == 1 or forced)
        spacing = BOLD_SPACING if bold else PLAIN_SPACING
        return bold, spacing, text_width(text, spacing, scale)

    best = 1
    for scale in range(1, 9):
        if FONT_HEIGHT * scale > height:
            break
        if measure(scale)[2] > width:
            break
        best = scale

    setting = str(config.get("scale") or "auto").strip().lower()
    if setting != "auto":
        try:
            best = max(1, min(int(setting), best))
        except (TypeError, ValueError):
            pass

    bold, spacing, drawn = measure(best)
    return {"scale": best, "bold": bold, "spacing": spacing, "width": drawn,
            "height": height if config.get("stretch", True)
                      else min(height, FONT_HEIGHT * best),
            "fits": drawn <= width, "panel_width": width, "panel_height": height}


def scale_for(config: dict, text: str) -> int:
    """The scale a given panel config would draw this text at."""
    return layout_for(config, text)["scale"]


def _style(config: dict, scale: int):
    """(bold, spacing) at a given scale, by the same rule layout_for uses."""
    want = str((config or {}).get("bold", "auto")).strip().lower()
    allowed = want not in ("0", "false", "no", "off")
    forced = want in ("1", "true", "yes", "on")
    bold = allowed and (scale == 1 or forced)
    return bold, (BOLD_SPACING if bold else PLAIN_SPACING)


def _hard_split(word: str, width: int, spacing: int, scale: int) -> list:
    """Break a word that cannot fit however it is set. Last resort."""
    pieces, current = [], ""
    for char in word:
        trial = current + char
        if current and text_width(trial, spacing, scale) > width:
            pieces.append(current)
            current = char
        else:
            current = trial
    if current:
        pieces.append(current)
    return pieces or [word[:1]]


def paginate(config: dict, text: str, max_pages: int = 20) -> dict:
    """Split a long message into panel-width pages.

    True scrolling is not available here. Shifting a message one column moves
    nearly every lit pixel, so a frame costs about as many writes as the
    message has pixels -- under two frames a second on this panel -- and it
    never stops, so the panel would hold the radio the twelve controllers
    share. Paging costs one draw per page and then nothing.

    Pages are cut at word boundaries, and the scale is chosen for the message
    as a whole rather than per page: sized page by page, a two-word page would
    jump to double height and the next back down, which reads worse than small
    text held steady.
    """
    config = config or {}
    text = (text or "").strip()
    try:
        width = max(4, min(256, int(config.get("width") or 96)))
        height = max(4, min(256, int(config.get("height") or 16)))
    except (TypeError, ValueError):
        width, height = 96, 16
    if not text:
        return {"scale": 1, "bold": False, "spacing": PLAIN_SPACING, "pages": []}

    ceiling = 1
    for scale in range(1, 9):
        if FONT_HEIGHT * scale > height:
            break
        ceiling = scale
    setting = str(config.get("scale") or "auto").strip().lower()
    if setting != "auto":
        try:
            ceiling = max(1, min(int(setting), ceiling))
        except (TypeError, ValueError):
            pass

    # A message that fits is never paged, however small it had to go to fit.
    # Shrinking "ICED COFFEE" to one screenful reads better than flipping it
    # across two, and this is the same answer the panel gave before there was
    # any paging at all.
    whole = layout_for(config, text)
    if whole["fits"]:
        return {"scale": whole["scale"], "bold": whole["bold"],
                "spacing": whole["spacing"], "pages": [text]}

    words = text.split()
    best = None
    for scale in range(ceiling, 0, -1):
        bold, spacing = _style(config, scale)
        if any(text_width(word, spacing, scale) > width for word in words):
            continue                      # a single word does not fit; go smaller
        pages, current = [], ""
        for word in words:
            trial = (current + " " + word) if current else word
            if current and text_width(trial, spacing, scale) > width:
                pages.append(current)
                current = word
            else:
                current = trial
        if current:
            pages.append(current)
        if len(pages) > max_pages:
            continue
        # Fewest pages wins, and a tie goes to the bigger text: a page turn
        # costs a reader the thread of the sentence, so trading one for larger
        # letters is a bad trade -- but at equal pages, larger is free.
        if best is None or len(pages) < len(best["pages"]):
            best = {"scale": scale, "bold": bold, "spacing": spacing,
                    "pages": pages}
    if best is not None:
        return best

    # Even at 1x a word is wider than the panel: break it mid-word rather than
    # letting it run off the edge.
    bold, spacing = _style(config, 1)
    pages = []
    for word in words:
        if text_width(word, spacing, 1) > width:
            pages.extend(_hard_split(word, width, spacing, 1))
        elif pages and text_width(pages[-1] + " " + word, spacing, 1) <= width:
            pages[-1] += " " + word
        else:
            pages.append(word)
    return {"scale": 1, "bold": bold, "spacing": spacing,
            "pages": pages[:max_pages]}


def best_scale(text: str, width: int, height: int, spacing: int = 1,
               limit: int = 8) -> int:
    """The largest whole scale at which this message still fits.

    Bigger is better right up until it does not fit, and which of width or
    height binds depends on the message: four letters on a 96x16 panel are
    limited by the height, eleven letters by the width. Picking the larger of
    the two constraints is the whole job.
    """
    if not text:
        return 1
    best = 1
    for scale in range(1, max(1, limit) + 1):
        if FONT_HEIGHT * scale > height:
            break
        if text_width(text, spacing, scale) > width:
            break
        best = scale
    return best


def preview(text: str, on: str = "#", off: str = ".") -> str:
    """ASCII rendering of a message, for the probe tool and the log.

    Being able to see the bitmap without the panel in front of you is what
    makes a wrong glyph a five-second fix instead of a trip to the sign.
    """
    return "\n".join("".join(on if bit else off for bit in row)
                     for row in render_bitmap(text))


def text_pixels(text: str, width: int, height: int, colour, background,
                scale: int = 1, stretch: bool = False, spacing: int = 1,
                bold: bool = False):
    """The message as rows of RGB tuples, sized to the panel.

    Left-aligned and vertically centred, clipped rather than scaled: a panel
    pixel is a panel pixel, and squeezing a wide message into a narrow display
    turns readable letters into mush.
    """
    rows = render_bitmap(text, height=height, scale=scale, stretch=stretch,
                         spacing=spacing, bold=bold)
    out = []
    for y in range(height):
        row = rows[y] if y < len(rows) else []
        out.append([colour if (x < len(row) and row[x]) else background
                    for x in range(width)])
    return out


def png_bytes(pixels, width: int, height: int) -> bytes:
    """Encode rows of RGB tuples as a PNG, using nothing but the stdlib.

    Pillow is not installed on the sign and adding it for this would be a
    compiled dependency on a machine that has to be rebuildable in a tent. A
    PNG is a signature, three chunks and a zlib stream, so it is written here.
    """
    import struct
    import zlib

    raw = bytearray()
    for row in pixels:
        raw.append(0)                        # filter type 0: none
        for pixel in row:
            raw += bytes(pixel)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


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
    on, and it is per message rather than one global interval: a long line
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
    try:
        color_mode = max(0, min(MAX_COLOR_MODE, int(raw.get("color_mode", 0))))
    except (TypeError, ValueError):
        color_mode = TEXT_COLOR_MODES.get(
            str(raw.get("color_mode") or "").strip().lower(), 0)
    return {
        "id": raw.get("id") or new_id(),
        "text": text,
        # 0 is one solid colour; the panel's gradients are 2-4. Per message
        # rather than per panel: one line in a single colour and a rainbow
        # "GOOD MORNING" is the point of having it.
        "color_mode": color_mode,
        "color": format_color(parse_color(raw.get("color"), (255, 47, 110))),
        "background": format_color(parse_color(raw.get("background"), (0, 0, 0))),
        "mode": mode,
        "speed": max(0, min(100, speed)),
        # 0 means "hold until something else is sent" -- a single standing
        # message rather than a rotation.
        "dwell": max(0.0, min(3600.0, dwell)),
        "enabled": bool(raw.get("enabled", True)),
    }


# ---------------------------------------------- the panel's own text command
#
# The panel stores a message and animates it itself. That is not a small
# detail: drawing a scroll from here costs ~29 acknowledged writes a frame and
# never stops, while this is one transfer and then no radio at all. Everything
# the compose screen used to offer -- scroll, flash, fade -- is a byte in this
# packet, and so is the paging this code implements in software.
#
# Layout, from the community's reverse engineering of the iPixel Color app
# (DonKracho/ESPHome-component-iPixel-ble, yyewolf/go-ipxl):
#
#   [0:2]   packet length, little endian, counting itself
#   [2:4]   00 01
#   [4]     00, or 02 if another chunk follows
#   [5:9]   payload length
#   [9:13]  crc32 of the payload, little endian
#   [13]    unknown, 00
#   [14]    save slot -- 0 for "just show it", 1-100 to store it
#   -- payload starts here --
#   [15:17] character count
#   [17]    horizontal alignment
#   [18]    vertical alignment
#   [19]    animation
#   [20]    speed 0-100
#   [21]    text colour mode
#   [22:25] text colour
#   [25]    background colour mode
#   [26:29] background colour
#   then one block per character: [flag][r][g][b][bitmap]
#
# NOT VERIFIED ON HARDWARE. The panel answers 01 or 02, so whether it accepts
# the packet is knowable; whether the glyphs come out the right way up is not,
# because the bit order the app's font is stored in is not documented. So the
# bitmap goes out in one of four orders, and `matrix_probe.py text` sends an
# asymmetric letter and asks which one looked right -- the same way the colour
# order was settled.
CMD_TEXT = (0x00, 0x01)
CMD_PROGRAM_SET = (0x08, 0x80)     # which stored slots the panel cycles
CMD_PROGRAM_DEL = (0x02, 0x01)     # drop slots from that list

# text_color_mode: 0 uses the colour in each character block (what this sends,
# so a message is one colour end to end), 1 uses the single colour in the
# header, and the rest are the app's gradient effects -- documented only as
# "div. rainbow effects", with 2, 3 and 4 described elsewhere as yellow-to-red,
# light-blue-to-white and blue-to-yellow, top to bottom. Numbers past 4 are
# offered because the app's own range is 0-9; what they do is unknown, and a
# panel that does not like one answers 02 rather than doing something unsafe.
TEXT_COLOR_MODES = {"per-character": 0, "solid": 1, "sunset": 2,
                    "ice": 3, "ocean": 4}
MAX_COLOR_MODE = 9

# back_color_mode: 0 leaves the background off, 1 paints back_color. Sending 1
# with black is not the same as 0 on every panel, so it follows the message:
# a background someone actually chose gets painted, and the default does not.

TEXT_ANIMATIONS = {
    "pages": 0,          # a screenful at a time -- what paginate() does by hand
    "scroll": 1,         # right to left
    "marquee": 2,        # left to right
    "scroll_up": 3,
    "scroll_down": 4,
    "flash": 5,
    "fade": 6,
    "static": 0,         # one page, held: pages with nothing to page to
}

# A cell is 8 wide and 16 tall, one byte per row, sixteen bytes -- for BOTH
# flags. That is measured, not assumed: sending flag 1 with a 32-byte 16x16
# cell put the first character up correctly and turned everything after it
# into rubbish, which is what a block-size mismatch looks like from the front.
# The reference implementation's "width doubled" is the panel's own doing, and
# the flag is how it is asked for -- so flag 1 sends the same sixteen bytes
# and lets the panel stretch them.
#
# Flag 1 is UNCONFIRMED. Flag 0 is what this sign runs and what it is drawn
# with here; if "wide" turns out to be wrong too, it is wrong the same way and
# "narrow" is one word away.
TEXT_FONTS = {"narrow": 0, "wide": 1}
TEXT_CELLS = {0: (8, 16), 1: (8, 16)}

# A row of a 16-wide glyph is two bytes, and there are three independent ways
# to get it wrong: which end of a byte is the left of the picture, which of the
# two bytes comes first, and whether rows run top-down or bottom-up. Eight
# combinations.
#
# The first version of this offered four -- it varied bit order and row order
# and left byte order alone -- and every one of them came out backwards on the
# sign, which is exactly what that omission predicts: mirroring a 16-pixel row
# means reversing the bits AND swapping the bytes, so no combination of the
# four could produce a mirror. "lsb-swap" is that mirror.
BITMAP_ORDERS = ("msb", "lsb-swap", "lsb", "msb-swap",
                 "msb-flip", "lsb-swap-flip", "lsb-flip", "msb-swap-flip")


def glyph_cell(char: str, font_flag: int = 0, order: str = "msb",
               stretch: bool = True) -> bytes:
    """One character as the panel wants it: 16 or 32 bytes, top row first.

    The 5x7 font is placed into the cell rather than replaced: it is the font
    the preview draws and the one the pixel path is tested against, so a
    message looks the same whichever route it takes to the panel.
    """
    width, height = TEXT_CELLS.get(font_flag, TEXT_CELLS[0])
    columns = glyph(char)
    # Rows of the 5x7 glyph, top first.
    rows = [[(column >> y) & 1 for column in columns] for y in range(FONT_HEIGHT)]
    scale = max(1, width // (FONT_WIDTH + 1))
    if scale > 1:
        rows = [[bit for bit in row for _ in range(scale)] for row in rows]
    drawn = len(rows[0]) if rows else 0
    left = max(0, (width - drawn) // 2)

    out = []
    for y in range(height):
        if stretch:
            source = rows[min(FONT_HEIGHT - 1, y * FONT_HEIGHT // height)]
        else:
            top = (height - FONT_HEIGHT) // 2
            source = rows[y - top] if 0 <= y - top < FONT_HEIGHT else []
        bits = [0] * width
        for index, bit in enumerate(source):
            if left + index < width:
                bits[left + index] = bit
        out.append(bits)

    return pack_cell(out, width, order)


def pack_cell(rows, width: int, order: str = "msb") -> bytes:
    """Rows of 0/1, top first, packed the way one of the eight orders wants."""
    parts = str(order or "msb").split("-")
    high_first = parts[0] != "lsb"          # is bit 7 the left of the picture?
    swap = "swap" in parts                  # does the right-hand byte go first?
    rows = list(rows)
    if "flip" in parts:
        rows.reverse()

    data = bytearray()
    for bits in rows:
        row = []
        for byte_index in range(width // 8):
            chunk = list(bits[byte_index * 8:(byte_index + 1) * 8])
            chunk += [0] * (8 - len(chunk))
            value = 0
            for position, bit in enumerate(chunk):
                if bit:
                    value |= (0x80 >> position) if high_first else (1 << position)
            row.append(value)
        if swap:
            row.reverse()
        data += bytes(row)
    return bytes(data)


# Which corner the marker below sits in, per what the panel does to a glyph.
# One dot in one corner cannot be misread the way a letter can: "backwards"
# covers mirrored, upside down and back-to-front, and a letter looks the same
# for two of those.
CORNERS = {
    "top-left": "msb",              # nothing is being done to it
    "top-right": "lsb-swap",        # mirrored left to right
    "bottom-left": "msb-flip",      # upside down
    "bottom-right": "lsb-swap-flip",  # both, i.e. rotated 180
}


def marker_cell(font_flag: int = 1, order: str = "msb") -> bytes:
    """A bracket in the top-left corner: two edges, so it has a handedness.

    A solid square would survive every transform and tell you nothing; a
    letter needs reading. This is a corner, and naming a corner is not a
    judgement call.
    """
    width, height = TEXT_CELLS.get(font_flag, TEXT_CELLS[0])
    arm = max(2, width // 2)
    rows = []
    for y in range(height):
        if y < 2:
            rows.append([1 if x < arm else 0 for x in range(width)])
        elif y < max(3, height // 2):
            rows.append([1 if x < 2 else 0 for x in range(width)])
        else:
            rows.append([0] * width)
    return pack_cell(rows, width, order)


def describe_message(message: dict, mode: str = None) -> str:
    """One message, for a log line or a job label.

    ``mode`` overrides the saved one so the label says what the panel is
    actually doing: a line reading "'VICE' scroll" for text that sits still is
    a lie in the one place someone goes to find out what happened.
    """
    text = (message or {}).get("text") or ""
    short = text if len(text) <= 24 else text[:23] + "…"
    return "%r %s" % (short, mode or (message or {}).get("mode", DEFAULT_MODE))


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
            "modes": list(self.modes),
        }

    # How the message can be made to behave. Static unless a driver says
    # otherwise -- claiming an effect that lands as still text is worse than
    # not offering it.
    modes = STATIC_ONLY

    def mode_for(self, message: dict) -> str:
        """The mode this driver will actually use for a message.

        A message keeps whatever mode it was saved with -- panels get swapped,
        and a mode this one cannot do may mean something to the next one. What
        gets coerced is the mode used for *this* draw, and the label that goes
        with it, so the log does not claim a scroll that never scrolled.
        """
        mode = str((message or {}).get("mode") or DEFAULT_MODE).strip().lower()
        return mode if mode in self.modes else self.modes[0]

    def characteristic(self):
        return self.config.get("char_uuid") or self.write_uuid

    # -- encoders; a driver that cannot do one of these returns []
    def text_frames(self, message: dict, previous: dict = None) -> list:
        """Frames to show ``message``.

        ``previous`` is what the panel is showing now, for drivers that can
        erase only what changed. Part of the interface rather than one
        driver's extra: the caller passes it every time, and a driver that
        ignores it must still accept it.
        """
        raise NotImplementedError

    def power_frames(self, on: bool) -> list:
        return []

    def brightness_frames(self, percent: int) -> list:
        return []

    def clear_frames(self) -> list:
        return []

    def block_frames(self, rgb) -> list:
        """One block of solid colour, for the colour check. [] if unsupported."""
        return []

    # Does the panel move and page a message by itself? If not, this code has
    # to, which is what paginate() is for.
    animates = False

    # -- helper shared by every length-prefixed family
    @staticmethod
    def _chunked(payload: bytes, size: int) -> list:
        return [payload[at:at + size] for at in range(0, len(payload), size)] or [b""]


class IPixel(MatrixDriver):
    """iPixel Color panels -- the app is "iPixel Color" on the phone.

    Protocol from the community's reverse engineering (see README section 10
    for the sources), and corroborated on this sign's own panel: the power and
    brightness bytes below are byte-for-byte what it acknowledged.

    Every packet is::

        [len lo][len hi][cmd lo][cmd hi][data ...]

    where the length counts the whole packet including itself. The panel
    answers on its notify characteristic with a packet of the same shape --
    ``05 00 <cmd lo> <cmd hi> <status>`` -- echoing the command it is replying
    to. Status 01 came back for every command that is known-correct here, and
    02 for a payload it rejected, which makes the panel its own test harness.

    Text has two routes, because they trade certainty against speed:

    ``pixels`` (the default)
        Turn on DIY mode and set the lit pixels one at a time. Every byte is
        documented and nothing is guessed. It costs one small packet per lit
        pixel, so a short message is a second or two.

    ``png``
        One transfer of a PNG with a CRC32. Far fewer packets, but two header
        bytes are not documented, so it stays off by default until the panel
        answers 01 to one. ``matrix_probe.py png-sweep`` finds them.
    """

    key = "ipixel"
    label = "iPixel Color"
    # Confirmed on the sign's own panel: a full-width paint lit it end to end,
    # and text rendered legibly at 96x16 with the AGRB layout below.
    confirmed = True
    name_prefixes = ("ipixel", "led_ble", "ledble", "led-ble")
    write_uuid = "0000fa02-0000-1000-8000-00805f9b34fb"
    notify_uuid = "0000fa03-0000-1000-8000-00805f9b34fb"

    # Commands, as (low, high) exactly how they sit on the wire.
    CMD_POWER = (0x07, 0x01)
    CMD_BRIGHTNESS = (0x04, 0x80)
    CMD_DIY = (0x04, 0x01)
    CMD_PIXEL = (0x05, 0x01)
    CMD_SCREEN = (0x07, 0x80)
    CMD_PNG = (0x02, 0x00)
    CMD_GIF = (0x03, 0x00)

    @staticmethod
    def packet(cmd, data=b"") -> bytes:
        """One protocol packet, length included.

        The length counts itself and the command, which is why it is computed
        here rather than by every caller -- getting it wrong by two is the
        kind of mistake that produces a silent panel.
        """
        body = bytes(cmd) + bytes(data)
        total = len(body) + 2
        return bytes([total & 0xFF, (total >> 8) & 0xFF]) + body

    # -- capabilities

    @property
    def capabilities(self) -> dict:
        caps = super().capabilities
        caps["pixels"] = True
        caps["text_mode"] = self.text_mode
        return caps

    @property
    def text_mode(self) -> str:
        mode = str(self.config.get("text_mode") or "pixels").strip().lower()
        return mode if mode in ("pixels", "png", "native") else "pixels"

    @property
    def animates(self) -> bool:
        """Does the panel handle movement and paging on its own?"""
        return self.text_mode == "native"

    @property
    def modes(self):
        """What the panel can be asked to do with a message.

        Only in native text mode. Drawing the pixels ourselves gives one
        behaviour -- still text -- and offering more than that is how the
        compose screen came to have a menu of five that all did the same
        thing.
        """
        if self.text_mode != "native":
            return STATIC_ONLY
        return ("static", "scroll", "marquee", "scroll_up", "scroll_down",
                "flash", "fade", "pages")

    # -- control

    def power_frames(self, on: bool) -> list:
        return [self.packet(self.CMD_POWER, [0x01 if on else 0x00])]

    def brightness_frames(self, percent: int) -> list:
        return [self.packet(self.CMD_BRIGHTNESS, [max(1, min(100, int(percent)))])]

    def diy_frames(self, on: bool) -> list:
        return [self.packet(self.CMD_DIY, [0x01 if on else 0x00])]

    # Ten by eight, centred. Big enough to name the colour of across a camp,
    # small enough that the whole check is three writes of eighty pixels
    # instead of three full-panel repaints at half a minute each.
    TEST_BLOCK = (10, 8)

    def block_rect(self):
        width, height = self.size()
        block_w = max(1, min(width, self.TEST_BLOCK[0]))
        block_h = max(1, min(height, self.TEST_BLOCK[1]))
        return ((width - block_w) // 2, (height - block_h) // 2,
                block_w, block_h)

    def block_frames(self, rgb) -> list:
        """Fill the test block with one colour.

        Every step paints the same pixels, so there is nothing to erase
        between them and no way to lose track of which block is which.
        """
        left, top, block_w, block_h = self.block_rect()
        frames = self.diy_frames(True)
        for y in range(top, top + block_h):
            for x in range(left, left + block_w):
                frames.append(self.pixel_frame(x, y, tuple(rgb)))
        return frames

    def layout(self) -> str:
        order = str(self.config.get("pixel_layout")
                    or DEFAULT_PIXEL_LAYOUT).strip().lower()
        return order if order in PIXEL_LAYOUTS else DEFAULT_PIXEL_LAYOUT

    def pixel_frame(self, x: int, y: int, rgb) -> bytes:
        return self.packet(self.CMD_PIXEL,
                           pixel_bytes(tuple(rgb), self.layout())
                           + [x & 0xFF, y & 0xFF])

    def clear_frames(self) -> list:
        """Paint the whole panel in the background colour.

        Deliberately not a power cycle: DIY mode holds whatever was drawn, so
        turning the screen off and on again brings the old message back.
        """
        width, height = self.size()
        frames = self.diy_frames(True)
        for y in range(height):
            for x in range(width):
                frames.append(self.pixel_frame(x, y, (0, 0, 0)))
        return frames

    def size(self):
        try:
            width = max(4, min(256, int(self.config.get("width") or 96)))
            height = max(4, min(256, int(self.config.get("height") or 16)))
        except (TypeError, ValueError):
            width, height = 96, 16
        return width, height

    def screen_frames(self, number: int) -> list:
        """Show buffer 1-9.

        DIY drawing lands in a buffer, and a panel showing a different one will
        take every pixel without changing what is on the glass -- which reads
        as "the protocol does not work" when in fact it worked perfectly.
        """
        return [self.packet(self.CMD_SCREEN, [max(1, min(9, int(number)))])]

    # -- text

    def text_frames(self, message: dict, previous: dict = None) -> list:
        if self.text_mode == "native":
            return self.native_text_frames(message)
        if self.text_mode == "png":
            return self.png_frames(message)
        return self.pixel_frames(message, previous=previous)

    # -- the panel's own text command

    def text_font(self) -> int:
        want = str(self.config.get("text_font") or "narrow").strip().lower()
        return TEXT_FONTS.get(want, TEXT_FONTS["narrow"])

    def bitmap_order(self) -> str:
        order = str(self.config.get("bitmap_order") or "msb").strip().lower()
        return order if order in BITMAP_ORDERS else "msb"

    def _align(self, key: str) -> int:
        try:
            return max(0, min(2, int(self.config.get(key, 1))))
        except (TypeError, ValueError):
            return 1

    def color_mode(self, message: dict) -> int:
        """0-9: solid, or one of the panel's own gradients."""
        want = (message or {}).get("color_mode")
        if want is None:
            want = self.config.get("color_mode", 0)
        if isinstance(want, str):
            key = want.strip().lower()
            if key in TEXT_COLOR_MODES:
                return TEXT_COLOR_MODES[key]
            try:
                want = int(key)
            except (TypeError, ValueError):
                return 0
        try:
            return max(0, min(MAX_COLOR_MODE, int(want)))
        except (TypeError, ValueError):
            return 0

    def animation_for(self, message: dict) -> int:
        mode = str((message or {}).get("mode") or DEFAULT_MODE).strip().lower()
        return TEXT_ANIMATIONS.get(mode, TEXT_ANIMATIONS["static"])

    def native_text_frames(self, message: dict, slot: int = 0,
                           order: str = None, font_flag: int = None,
                           animation: int = None, **kwargs) -> list:
        """One packet: the message, and how the panel should animate it.

        After this the panel is on its own -- it scrolls, flashes or pages
        without another byte from us, which is the whole point. A slot of 1-100
        stores it as well, for the panel's own program list.
        """
        import binascii

        text = str((message or {}).get("text") or "")[:MAX_TEXT]
        if not text:
            return []
        # Some of these panels lay the character blocks out right to left. If
        # this one does, the letters arrive in the wrong order however right
        # each glyph is, and reversing here is the whole fix.
        if self.config.get("text_reversed"):
            text = text[::-1]
        order = order or self.bitmap_order()
        font_flag = self.text_font() if font_flag is None else int(font_flag)
        colour = parse_color((message or {}).get("color"))
        background = parse_color((message or {}).get("background"), (0, 0, 0))
        if animation is None:
            animation = self.animation_for(message)
        try:
            speed = max(0, min(100, int((message or {}).get("speed", 50))))
        except (TypeError, ValueError):
            speed = 50

        cells = kwargs.get("cells")
        count = len(cells) if cells else len(text)
        payload = bytearray()
        payload += count.to_bytes(2, "little")
        payload.append(self._align("h_align"))
        payload.append(self._align("v_align"))
        payload.append(int(animation) & 0xFF)
        payload.append(speed)
        payload.append(self.color_mode(message))
        payload += bytes(colour)
        # Paint the background only when one was chosen. The character blocks
        # carry the text colour themselves, so an unwanted background is the
        # one thing here that can hide a message completely.
        payload.append(0x01 if any(background) else 0x00)
        payload += bytes(background)
        for index in range(count):
            payload.append(font_flag & 0xFF)
            payload += bytes(colour)
            payload += (cells[index] if cells
                        else glyph_cell(text[index], font_flag, order,
                                        self.stretch()))

        header = bytearray()
        header.append(0x00)                       # no further chunk
        header += len(payload).to_bytes(4, "little")
        header += (binascii.crc32(bytes(payload)) & 0xFFFFFFFF).to_bytes(4, "little")
        header.append(0x00)                       # unknown, always zero
        header.append(max(0, min(100, int(slot))))
        packet = self.packet(CMD_TEXT, bytes(header) + bytes(payload))
        # Split, like the PNG path above and for the same reason: this is one
        # length-prefixed packet and it is far bigger than an ATT write. The
        # panel reads a stream and takes its length off the front, so the
        # pieces reassemble there; handed over whole it is a write the adapter
        # cannot make. Twenty characters is 749 bytes against an MTU of 247,
        # which is why short test words appeared and real messages did not.
        return self._chunked(packet, self.chunk)

    def program_frames(self, slots) -> list:
        """Tell the panel which stored slots to cycle, and in what order.

        This is the sign running its own playlist: once the slots are filled
        the Pi is not involved at all, which on a shared antenna is worth more
        than any of it.
        """
        return self._program(CMD_PROGRAM_SET, slots)

    def program_clear_frames(self, slots=None) -> list:
        """Drop slots from the panel's cycle. Everything, unless told otherwise."""
        return self._program(CMD_PROGRAM_DEL,
                             slots if slots else range(1, 101))

    def _program(self, cmd, slots) -> list:
        # Slots are 1-100. Zero and anything above are reserved, so a bad
        # number is dropped rather than clamped into someone else's message.
        numbers = []
        for slot in (slots or []):
            try:
                number = int(slot)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= 100 and number not in numbers:
                numbers.append(number)
        if not numbers:
            return []
        body = bytearray(len(numbers).to_bytes(2, "little"))
        body += bytes(numbers)
        return [self.packet(cmd, bytes(body))]

    def scale_for(self, message: dict) -> int:
        """How many LEDs per font pixel, for this message on this panel."""
        return layout_for(self.config, (message or {}).get("text") or "")["scale"]

    def plan(self, message: dict) -> dict:
        """How this message will be drawn: scale, bold, spacing, size.

        Not "layout" -- that name is taken by the pixel BYTE layout above, and
        having both meant every colour lookup called this instead and crashed.

        A message may carry its own ``plan``, and when it does that wins. That
        is how a paged message keeps one size across its pages: sized here,
        page by page, a two-word page would jump to double height and the next
        back down.
        """
        message = message or {}
        text = message.get("text") or ""
        plan = layout_for(self.config, text)
        override = message.get("plan")
        if isinstance(override, dict):
            for key in ("scale", "bold", "spacing"):
                if key in override and override[key] is not None:
                    plan[key] = override[key]
            plan["scale"] = max(1, int(plan["scale"]))
            plan["spacing"] = max(0, int(plan["spacing"]))
            plan["bold"] = bool(plan["bold"])
            plan["width"] = text_width(text, plan["spacing"], plan["scale"])
            plan["fits"] = plan["width"] <= plan["panel_width"]
            plan["height"] = (plan["panel_height"]
                              if self.config.get("stretch", True)
                              else min(plan["panel_height"],
                                       FONT_HEIGHT * plan["scale"]))
        return plan

    def stretch(self) -> bool:
        return bool(self.config.get("stretch", True))

    def lit_pixels(self, message) -> set:
        """Which pixels a message turns on.

        Takes a list as well as one message, for the case where more than one
        thing might be on the glass: a write that timed out may have got some
        of itself up before it died, so the next message has to erase against
        what we know landed *and* against what might have.
        """
        if isinstance(message, (list, tuple)):
            found = set()
            for entry in message:
                found |= self.lit_pixels(entry)
            return found
        width, height = self.size()
        plan = self.plan(message)
        rows = render_bitmap((message or {}).get("text") or "", height=height,
                             spacing=plan["spacing"], scale=plan["scale"],
                             stretch=self.stretch(), bold=plan["bold"])
        on = set()
        for y in range(height):
            row = rows[y] if y < len(rows) else []
            for x in range(min(width, len(row))):
                if row[x]:
                    on.add((x, y))
        return on

    def pixel_frames(self, message: dict, fill: bool = None,
                     previous: dict = None) -> list:
        """Draw the message a pixel at a time. Nothing here is guessed.

        Drawing only the lit pixels is cheap but leaves the last message
        underneath, so two messages end up superimposed. Painting the whole
        panel every time fixes that and costs width x height packets -- 1536 on
        this sign's 96x16, over thirty seconds of radio for one word.

        So the default is neither: erase exactly the pixels the previous
        message lit and this one does not. A message replaced by another of
        similar length costs about twice its own pixels, not the whole display.
        ``fill`` forces the expensive full repaint, for when what is on the
        panel is unknown.
        """
        width, height = self.size()
        colour = parse_color(message.get("color"))
        background = parse_color(message.get("background"), (0, 0, 0))
        if fill is None:
            fill = bool(self.config.get("fill_background", False))

        wanted = self.lit_pixels(message)
        frames = self.diy_frames(True)
        if fill:
            stale = {(x, y) for y in range(height) for x in range(width)}
        else:
            stale = self.lit_pixels(previous) if previous else set()
        for x, y in sorted(stale - wanted):
            frames.append(self.pixel_frame(x, y, background))
        for x, y in sorted(wanted):
            frames.append(self.pixel_frame(x, y, colour))
        return frames

    def png_frames(self, message: dict) -> list:
        """One PNG transfer.

        The extended-data header is::

            [len 2][type 2][opt 1][frame len 4][crc32 4][buffer 1][png ...]

        ``opt`` and ``buffer`` are not documented anywhere I could find, so
        they come from config and default to zero. The panel says 01 or 02, so
        finding them is a search with an answer rather than a guess -- see
        matrix_probe.py png-sweep.
        """
        width, height = self.size()
        plan = self.plan(message)
        colour = parse_color(message.get("color"))
        background = parse_color(message.get("background"), (0, 0, 0))
        blob = png_bytes(text_pixels(message.get("text") or "", width, height,
                                     colour, background,
                                     scale=plan["scale"],
                                     stretch=self.stretch(),
                                     spacing=plan["spacing"],
                                     bold=plan["bold"]),
                         width, height)
        opt = int(self.config.get("png_opt", 0)) & 0xFF
        buffer = int(self.config.get("png_buffer", 0)) & 0xFF
        import binascii
        crc = binascii.crc32(blob) & 0xFFFFFFFF
        body = bytearray()
        body += bytes(self.CMD_PNG)
        body += bytes([opt])
        body += len(blob).to_bytes(4, "little")
        body += crc.to_bytes(4, "little")
        body += bytes([buffer])
        body += blob
        total = len(body) + 2
        payload = bytes([total & 0xFF, (total >> 8) & 0xFF]) + bytes(body)
        return self._chunked(payload, self.chunk)


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

    def text_frames(self, message: dict, previous: dict = None) -> list:
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


FAMILIES = {driver.key: driver for driver in (IPixel, RawDriver)}
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
        # Unrecognised. A captured command set may still be present, so the raw
        # driver is the right fallback -- but only when there is something for
        # it to replay. With neither a family nor a capture, guessing at the
        # commonest family beats a driver that can do nothing at all, and the
        # UI shows which was chosen either way.
        if matrix.get("commands") or matrix.get("char_uuid"):
            cls = RawDriver
        else:
            log.debug("no family matched %r; assuming %s",
                      matrix.get("name"), IPixel.label)
            cls = IPixel
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
               ("matrix", "pixel", "badge", "screen", "display", "sign",
                # Seen on this sign's own panel: LED_BLE_4B3289C5, where the
                # tail is the device's own MAC suffix. The dash, underscore and
                # run-together spellings all occur across these brands.
                "led-", "led_", "ledble", "led ble"))
