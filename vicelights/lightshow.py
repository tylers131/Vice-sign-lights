"""The sign's around-the-clock look: scenes, and which play when.

The brief was: keep it lit 24/7, always doing *something*, and change the mood
with the time of day -- chill at dawn, bold enough to read in desert sun,
full-on party after dark, and a warm come-hither during coffee service. This
module is the whole answer in one place: the scene palette, and the day-parts
that schedule it. ``apply(store)`` writes it into a config.

Design rules the whole thing obeys:

* **Every scene lights every zone.** The complaint was finding zones dark, so
  there is no scene here that leaves the cups, the straws, or a letter unlit.
  Rotation re-sends the current scene on its interval, which also re-wakes any
  controller whose Bluetooth had dropped -- so the sign heals itself.
* **One identity, many moods.** It is a VICE sign: hot pink and teal are the
  spine, sunset orange and violet the accents. Every day-part is a variation
  on that, never a random palette, so it reads as *the* sign all week.
* **Motion without churn.** These are analogue RGB controllers -- one colour
  or one built-in animation per zone, no per-pixel. Where a scene should
  breathe on its own between rotations, it uses a single-colour *fade* mode
  (a slow swell in one hue) so the sign is alive even when nothing is
  changing it. Party scenes use the 7-colour jump/flash for real energy.
* **Daylight is the hard case.** LED strips wash out in direct sun, so the
  daytime scenes drop white and pastels and lean on the few colours that
  still punch through: saturated pink, red, deep blue, magenta.

The mode numbers are this hardware's *measured* ones (see ``mode_names`` in the
config, learned with ``elk_scan.py modes``), not the datasheet's -- they
disagree, and the sign obeys the hardware.
"""

from __future__ import annotations

# -- the measured animation modes we actually use ------------------------
#
# Single-colour fades ("breathe" one hue in and out) and the multi-colour
# party modes. Named for what they do on this sign.
BREATHE_RED = 0x8B
BREATHE_GREEN = 0x8C
BREATHE_BLUE = 0x8D
BREATHE_YELLOW = 0x8E     # warm, reads as amber
BREATHE_CYAN = 0x8F
BREATHE_PINK = 0x90       # "fade magenta" -- the closest to VICE pink
BREATHE_WHITE = 0x91
FADE_SEVEN = 0x8A         # smooth drift through seven colours
JUMP_SEVEN = 0x88         # hard cuts through seven colours
FLASH_SEVEN = 0x87        # flashing seven colours

# Speeds, 0-100. Slow enough to feel like breathing, fast enough to party.
SLOW, EASY, MED, FAST = 22, 35, 55, 72


def _solid(target, color):
    return {"target": target, "color": color, "power": True}


def _breathe(target, mode, speed):
    return {"target": target, "mode": mode, "speed": speed, "power": True}


def _scene(name, letters, cup, straw, stagger=0.0):
    """A scene from three zone steps -- letters, cup, straw cover all twelve."""
    return {"name": name, "stagger": stagger,
            "steps": [letters, cup, straw]}


# Handy colours, so the intent reads in the scene list.
PINK = "#ff2d78"       # the VICE pink
HOTPINK = "#ff0a5a"    # saturated, for daylight
TEAL = "#22d3ee"
DEEPTEAL = "#00b4d8"   # holds up in sun better than pale cyan
BLUE = "#0060ff"
VIOLET = "#8000ff"
MAGENTA = "#c800ff"
ORANGE = "#ff6a00"
SUNSET = "#ff5a00"
GOLD = "#ffb000"
AMBER = "#ff8c2a"
CREAM = "#ffe0a0"
RED = "#ff0030"
YELLOW = "#ffd000"
MINT = "#00e5c0"
CYAN2 = "#00d0ff"


# ------------------------------------------------------------------ scenes

SCENES = [
    # -- Sunrise / chill (05:00-09:00): warm, slow, breathing.
    _scene("Sunrise", _solid("group:letters", AMBER),
           _solid("group:cup", "#ff80c0"), _solid("group:straw", CREAM)),
    _scene("Dawn Breathe", _breathe("group:letters", BREATHE_PINK, SLOW),
           _breathe("group:cup", BREATHE_CYAN, SLOW),
           _breathe("group:straw", BREATHE_WHITE, SLOW)),
    _scene("Ember Glow", _solid("group:letters", "#e0407a"),
           _solid("group:cup", GOLD), _solid("group:straw", ORANGE)),

    # -- Daytime / bold (09:00-17:00): saturated solids that fight the sun.
    _scene("Vice", _solid("group:letters", HOTPINK),
           _solid("group:cup", DEEPTEAL), _solid("group:straw", HOTPINK)),
    _scene("Sunblast", _solid("group:letters", RED),
           _solid("group:cup", ORANGE), _solid("group:straw", YELLOW)),
    _scene("Electric Blue", _solid("group:letters", BLUE),
           _solid("group:cup", MINT), _solid("group:straw", PINK)),
    _scene("Magenta Pop", _solid("group:letters", MAGENTA),
           _solid("group:cup", PINK), _solid("group:straw", VIOLET)),

    # -- Golden hour (17:00-20:00): sunset palette, a little motion.
    _scene("Sunset", _solid("group:letters", SUNSET),
           _solid("group:cup", "#ff0080"), _solid("group:straw", "#ffc000")),
    _scene("Magic Hour", _breathe("group:letters", BREATHE_PINK, EASY),
           _solid("group:cup", ORANGE), _solid("group:straw", GOLD)),
    _scene("Afterglow", _solid("group:letters", PINK),
           _solid("group:cup", VIOLET), _solid("group:straw", SUNSET)),
    _scene("Neon Dusk", _solid("group:letters", "#ff00d0"),
           _solid("group:cup", MINT), _solid("group:straw", YELLOW)),

    # -- Party / neon night (20:00-00:00): vibrant, varied, moving.
    _scene("Miami", _solid("group:letters", "#ff40a0"),
           _solid("group:cup", MINT), _solid("group:straw", YELLOW)),
    _scene("Neon Nights", _solid("group:letters", VIOLET),
           _solid("group:cup", PINK), _solid("group:straw", TEAL)),
    _scene("Cyberpunk", _solid("group:letters", "#ff00d0"),
           _solid("group:cup", "#ffe000"), _solid("group:straw", "#00fff0")),
    # The showpiece: each letter its own neon. Cup white, straws pink.
    {"name": "Rainbow VICE", "stagger": 0.0, "steps": [
        _solid("group:V", PINK), _solid("group:I", "#ff8c00"),
        _solid("group:C", MINT), _solid("group:E", VIOLET),
        _solid("group:cup", "#ffffff"), _solid("group:straw", PINK)]},
    _scene("Vice Breathe", _breathe("group:letters", BREATHE_PINK, MED),
           _breathe("group:cup", BREATHE_CYAN, MED),
           _breathe("group:straw", BREATHE_PINK, MED)),
    # Whole sign cutting colour together -- big and loud.
    _scene("Jump Party", _breathe("group:letters", JUMP_SEVEN, MED),
           _breathe("group:cup", JUMP_SEVEN, MED),
           _breathe("group:straw", JUMP_SEVEN, MED)),

    # -- Late night / deep (00:00-05:00): hypnotic, slow, violet-blue.
    _scene("Ultraviolet", _solid("group:letters", "#6000ff"),
           _solid("group:cup", "#ff00ff"), _solid("group:straw", "#2000c0")),
    _scene("After Hours", _solid("group:letters", "#4000ff"),
           _solid("group:cup", "#ff0080"), _solid("group:straw", CYAN2)),
    _scene("Deep Fade", _breathe("group:letters", FADE_SEVEN, SLOW),
           _breathe("group:cup", FADE_SEVEN, SLOW),
           _breathe("group:straw", FADE_SEVEN, SLOW)),
    _scene("Hypnotic", _breathe("group:letters", BREATHE_BLUE, SLOW),
           _breathe("group:cup", BREATHE_PINK, SLOW),
           _breathe("group:straw", BREATHE_CYAN, SLOW)),

    # -- Coffee attract: warm, inviting, come-on-over.
    _scene("Coffee Call", _solid("group:letters", PINK),
           _solid("group:cup", AMBER), _solid("group:straw", "#ffd070")),
    _scene("Fresh Brew", _breathe("group:letters", BREATHE_PINK, EASY),
           _solid("group:cup", ORANGE), _solid("group:straw", "#ffcc66")),
    _scene("Wake Up", _solid("group:letters", GOLD),
           _solid("group:cup", PINK), _solid("group:straw", ORANGE)),

    # Kept for a manual blackout; never rotated into.
    {"name": "All off", "stagger": 0.0,
     "steps": [{"target": "all", "color": "#000000", "power": False}]},
]


# --------------------------------------------------------------- schedule

# Each day-part: when it starts (local, 24h), how often it changes scene, and
# which scenes it draws from. Starts sorted; the active one is the latest whose
# start has passed, wrapping midnight. Intervals set the pace of the mood --
# chill drifts slowly, the party moves.
DAYPARTS = [
    {"name": "Late night", "start": "00:00", "interval_minutes": 8.0,
     "playlist": ["Ultraviolet", "After Hours", "Deep Fade", "Hypnotic"]},
    {"name": "Sunrise chill", "start": "05:00", "interval_minutes": 12.0,
     "playlist": ["Sunrise", "Dawn Breathe", "Ember Glow"]},
    {"name": "Daytime", "start": "09:00", "interval_minutes": 10.0,
     "playlist": ["Vice", "Sunblast", "Electric Blue", "Magenta Pop"]},
    {"name": "Golden hour", "start": "17:00", "interval_minutes": 7.0,
     "playlist": ["Sunset", "Magic Hour", "Afterglow", "Neon Dusk"]},
    {"name": "Party", "start": "20:00", "interval_minutes": 5.0,
     "playlist": ["Miami", "Neon Nights", "Cyberpunk", "Rainbow VICE",
                  "Vice Breathe", "Jump Party"]},
]

# Played while a coffee service is on (from the event calendar, same source as
# the panel's promos), whatever the hour -- so the lights pull people in at the
# same moment the panel is shouting about iced coffee.
ATTRACT = ["Coffee Call", "Fresh Brew", "Wake Up"]
ATTRACT_INTERVAL_MINUTES = 4.0

# When the clock is unset (no day-part can be chosen) the sign still needs a
# good look. A best-of mix across the moods.
FALLBACK_PLAYLIST = ["Vice", "Miami", "Neon Nights", "Sunset", "Magenta Pop",
                     "Afterglow", "Vice Breathe"]

BOOT_SCENE = "Vice"


def rotation_config() -> dict:
    """The rotation block that drives the whole show."""
    return {
        "enabled": True,
        "playlist": [n for n in FALLBACK_PLAYLIST],
        "exclude": ["All off"],
        "order": "shuffle",
        "avoid_repeat": True,
        "interval_minutes": 8.0,
        # Touch the controls and the sign holds still for a bit, then goes back
        # to running itself -- the manual override the brief asked for.
        "hold_after_manual_minutes": 20.0,
        "dayparts": [dict(d) for d in DAYPARTS],
        "attract": list(ATTRACT),
        "attract_interval_minutes": ATTRACT_INTERVAL_MINUTES,
    }


def build(raw: dict) -> dict:
    """Return ``raw`` with the whole light show written in, preserving the rest.

    Pure: it does not touch disk, so the loader can preview the result and the
    tests can check it. ``apply`` is this plus a save. Everything not about the
    show -- devices, groups, the panel, saved messages, the temperature block,
    the learned mode names -- is carried across untouched.
    """
    raw = dict(raw)
    raw["scenes"] = [dict(s) for s in SCENES]
    raw["rotation"] = rotation_config()
    # Drop schedules that turn the sign off; keep any the user added that do
    # not. The blackout was the "Dawn off"/"All off" one specifically -- the
    # thing that was leaving the sign dark every morning.
    raw["schedules"] = [s for s in (raw.get("schedules") or [])
                        if (s.get("scene") or "").strip().lower() != "all off"]
    settings = dict(raw.get("settings") or {})
    settings["apply_on_boot"] = BOOT_SCENE
    raw["settings"] = settings
    return raw


def apply(store) -> dict:
    """Install the whole light show into ``store``, preserving everything else.

    Replaces the scenes and the rotation, points the boot scene at a signature
    look, and clears any scheduled blackout. Devices, groups, the panel, saved
    messages and the temperature block are left untouched. Returns the new
    normalised config.
    """
    return store.replace_all(build(store.snapshot()))
