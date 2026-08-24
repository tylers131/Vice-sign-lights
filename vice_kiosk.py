#!/usr/bin/env python3
"""Touch panel for the Vice sign, drawn with pygame.

Runs beside the service rather than inside it, talking to the same local HTTP
API a phone would use. That keeps the panel from being able to wedge the sign:
if this process dies, the lights carry on and the web UI still works.

Deliberately not a browser. Rendering twelve buttons does not need Chromium, a
compositor, or 486MB of dependencies on a machine whose whole job is writing
nine-byte frames over Bluetooth.

    python3 vice_kiosk.py                  # windowed, for a desktop
    VICE_KIOSK_URL=http://127.0.0.1 python3 vice_kiosk.py

Only stdlib plus pygame -- nothing from the vicelights package, so it runs on
the system interpreter with no venv.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import math
import mmap
import os
import select
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

import pygame

BASE = os.environ.get("VICE_KIOSK_URL", "http://127.0.0.1").rstrip("/")
FPS = 30

# Straight from the artboards. The canvas chrome around them is the light
# "Organic" design system; the panel itself is explicitly dark, so these are the
# inline values the boards use rather than the DS tokens.
BG       = (0x12, 0x10, 0x0e)
CARD     = (0x1a, 0x16, 0x13)
CARD_ALT = (0x17, 0x14, 0x12)
INK      = (0xf3, 0xec, 0xe2)
PINK     = (0xff, 0x2f, 0x6e)
PINK_SOFT= (0xff, 0x8f, 0xb4)
CYAN     = (0x22, 0xd3, 0xee)
CYAN_SOFT= (0x8b, 0xea, 0xf7)
OLIVE    = (0xa8, 0xbd, 0x80)
OLIVE_SOFT=(0xc3, 0xd6, 0xa0)
ORANGE   = (0xe5, 0x8b, 0x4d)
ORANGE_INK=(0x24, 0x15, 0x05)


def over(colour, alpha, base=BG):
    """Flatten an rgba onto a background, the way the CSS does."""
    return tuple(int(round(c * alpha + b * (1 - alpha)))
                 for c, b in zip(colour, base))


WHITE = (255, 255, 255)
LINE     = over(WHITE, 0.07)          # rgba(255,255,255,.07) hairlines
LINE_SOFT= over(WHITE, 0.12)
MUTED    = over(INK, 0.45)
FAINT    = over(INK, 0.40)
DIM      = over(INK, 0.30)
CHIP_BG  = over(INK, 0.06)
# Four, down from five. Lights is gone -- the sign preview IS the per-device
# picker, and device health lives on System with the queue -- and Status grew
# into System, the troubleshooting tab. Fewer pills also means wider ones.
#
# "LED Text Display" rather than "Panel": on a sign whose other twelve devices
# are panels of a sort too, "Panel" named the thing by what the code calls it
# instead of by what you would point at.
TABS = (("scenes", "Scenes"), ("colour", "Colour"),
        ("panel", "LED Text Display"), ("system", "System"))

# How long the screen stays unlocked once untouched. It is bolted to a sign in
# a crowd at the height of a passing hand, and every control on it writes to
# the lights. Long enough to think between taps, short enough that walking
# away arms it.
LOCK_AFTER = 90.0

# The on-screen keyboard. Upper case only, and that is a decision rather than a
# shortcut: sign messages are shouty anyway, the panel's own font has one case,
# and dropping the shift key buys a whole row of width on an 800-pixel screen.
KEY_ROWS = ("1234567890", "QWERTYUIOP", "ASDFGHJKL\u232b", "ZXCVBNM!?.")
MAX_COMPOSE = 60          # what fits the field; the API allows far more

# The twelve circles from 2b, in the board's order.
SWATCHES = (
    ("#ff2f6e", "Vice"),   ("#f01e3c", "Red"),    ("#f2721b", "Orange"),
    ("#f0b429", "Amber"),  ("#f6d8a8", "Warm"),   ("#f3ece2", "White"),
    ("#22d3ee", "Cyan"),   ("#2563eb", "Blue"),   ("#8b5cf6", "Violet"),
    ("#ff2fd0", "Magenta"),("#22c55e", "Green"),  ("#2fe3b0", "Mint"),
)

# Chip labels from 2b -- shorter than the config's own keys.
GROUP_LABELS = {
    "letters": "Letters", "drink": "Drink", "cup": "Cups", "straw": "Straws",
    "border": "Border", "side-a": "Side A", "side-b": "Side B",
}
# 2b names four patterns rather than listing every mode.
PATTERN_LABELS = (("flash", "Flash"), ("jump", "Jump"),
                  ("rgb", "Fade RGB"), ("7 colour", "Fade 7"))
PATTERN_SPEED = 70
# Chosen so the whole sign takes roughly 1s, 6s, 12s and 24s to
# come round at twelve devices -- the last is slow enough to watch
# travel, the first is just the sweep's own pace.
ROLL_STEPS = (0.0, 0.5, 1.0, 2.0)


# --------------------------------------------------------------------- client

def _get(path, timeout=4.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(path, payload=None, timeout=6.0):
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(BASE + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class Sign:
    """Everything the panel knows, kept current by one background thread.

    The UI thread never makes a request. A sweep takes ~30s and the API can
    block for seconds; doing this inline would freeze the panel mid-touch.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.scenes = []
        self.groups = []
        self.devices = []
        self.patterns = []
        self.moving_modes = set()
        self.playing = ""
        self.rotation = {}
        self.busy = False
        self.jobs = []                # recent jobs, live and finished
        self.battery = {}             # the runtime budget, from /api/status
        self.queued = 0
        self.job = None
        self.down = 0
        self.total = 0
        self.online = False
        self.diagnostics = []
        # The text panel: its settings and queue from /api/matrix on the slow
        # beat, its "showing right now" merged from /api/status on every poll.
        self.panel = {}
        self.messages = []
        # Not "down": Sign.down is already the count of finished items in the
        # running job, and shadowing it silently broke the progress strip.
        self.unreachable = []
        self._diag_at = 0.0
        self.devices_total = 0
        self.devices_bad = 0
        self.pending = None
        self.pending_since = 0.0
        self.toast = ""
        self.toast_until = 0.0
        self._stop = threading.Event()

    # -- helpers used by the UI thread; all cheap and lock-guarded ------------

    def say(self, message, seconds=2.6):
        with self.lock:
            self.toast = message
            self.toast_until = time.monotonic() + seconds

    def mark_pending(self, name):
        with self.lock:
            self.pending = name
            self.pending_since = time.monotonic()

    def clear_pending(self):
        with self.lock:
            self.pending = None

    def refresh_panel(self):
        """Re-read the panel on the next poll rather than on the slow beat.

        The queue is polled every ten seconds because it only changes when
        someone edits it -- but having just edited it yourself, ten seconds of
        stale chips reads as the tap not working.
        """
        with self.lock:
            self._diag_at = 0.0

    def snapshot(self):
        with self.lock:
            return {
                "scenes": list(self.scenes), "playing": self.playing,
                "rotation": dict(self.rotation), "busy": self.busy,
                "queued": self.queued, "job": self.job,
                "jobs": list(self.jobs), "battery": dict(self.battery),
                "done": self.down, "total": self.total, "online": self.online,
                "pending": self.pending,
                "devices_total": self.devices_total, "devices_bad": self.devices_bad,
                "groups": list(self.groups), "devices": list(self.devices),
                "patterns": list(self.patterns),
                "diagnostics": list(self.diagnostics),
                "unreachable": list(self.unreachable),
                "panel": dict(self.panel), "messages": list(self.messages),
                "toast": self.toast if time.monotonic() < self.toast_until else "",
            }

    # -- the polling thread ---------------------------------------------------

    def start(self):
        threading.Thread(target=self._run, name="sign-poll", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _load_scenes(self):
        state = _get("/api/state")
        if not state.get("ok"):
            return
        modes = state.get("modes", [])
        moving = {m["value"] for m in modes if m.get("animates")}
        labels = {m["value"]: m["name"] for m in modes}
        scenes = []
        for scene in state.get("scenes", []):
            steps = scene.get("steps") or []
            animated = any(s.get("mode") and s["mode"] in moving for s in steps)
            # The shelf card shows the scene's own colours as a ramp. A step
            # running a pattern has no colour of its own, so it contributes the
            # rainbow those modes actually play.
            ramp = []
            for step in steps:
                if step.get("mode") and step["mode"] in moving:
                    # "fade blue" is blue; only the multi-colour modes are a
                    # rainbow. Without this every animated card looks alike.
                    ramp += _mode_ramp(labels.get(step["mode"], ""))
                elif step.get("power") is False:
                    ramp.append("#2a2622")
                elif step.get("color"):
                    ramp.append(step["color"])
            scenes.append({"name": scene.get("name", "?"), "animated": animated,
                           "ramp": ramp or ["#2a2622"]})
        # Movement first, matching how the web UI groups them.
        scenes.sort(key=lambda s: (not s["animated"],))

        # Patterns worth a button: the ones that move through several colours.
        # A single-colour fade needs speed 85+ to be visible at all and reads
        # as a solid colour otherwise, which is a poor thing to offer as a
        # one-tap choice.
        patterns = [m for m in modes if m.get("animates")
                    and ("7 colour" in m["name"].lower() or "rgb" in m["name"].lower())]

        devices = [{"name": d.get("name", "?"), "address": d.get("address", ""),
                    "groups": d.get("groups", []), "reachable": d.get("reachable"),
                    "showing": d.get("showing")}
                   for d in state.get("devices", []) if d.get("enabled", True)]

        with self.lock:
            self.scenes = scenes
            self.moving_modes = moving
            self.patterns = patterns
            self.devices = devices
            # Straight from the config, so a group added from the phone shows
            # up here with no code change.
            self.groups = list(state.get("groups", []))

    def _poll_status(self):
        status = _get("/api/status")
        if not status.get("ok"):
            return
        rotation = status.get("rotation") or {}
        # Everything recent, not just the first live one: the System tab lists
        # the queue, and finished jobs answer "did my tap earlier actually
        # run", which a live-only list cannot.
        jobs = list(status.get("jobs") or [])
        live = [j for j in jobs if j.get("state") in ("running", "queued")]
        job, done, total = None, 0, 0
        if live:
            job = live[0]
            items = job.get("items") or []
            total = len(items)
            done = sum(1 for i in items if i.get("status") and i["status"] != "pending")
        with self.lock:
            self.online = True
            self.rotation = rotation
            self.playing = rotation.get("current") or self.playing
            self.busy = bool(status.get("busy"))
            self.queued = int(status.get("queued") or 0)
            self.job, self.down, self.total = job, done, total
            self.jobs = jobs
            self.battery = status.get("battery") or {}
            devices = status.get("devices") or {}
            self.devices_total = len(devices)
            self.devices_bad = sum(1 for d in devices.values() if d.get("reachable") is False)
            for device in self.devices:
                runtime = devices.get(device["address"])
                if runtime:
                    device["reachable"] = runtime.get("reachable")
                    device["showing"] = runtime.get("showing")
            # Drop the tapped highlight once the sign confirms, or after 45s so
            # a scene that never lands cannot leave the button stuck lit.
            if self.pending and (self.playing == self.pending
                                 or time.monotonic() - self.pending_since > 45):
                self.pending = None
            live = status.get("panel")
            if live and self.panel:
                self.panel.update(live)
            elif live:
                self.panel = dict(live)

    def _poll_panel(self):
        """The text panel's settings and queue.

        On the slow beat with diagnostics: the queue only changes when someone
        edits it, and what changes second to second -- which message is up, how
        long is left -- rides along on /api/status instead.
        """
        try:
            data = _get("/api/matrix", timeout=6.0)
        except Exception:
            return
        if not data.get("ok"):
            return
        panel = data.get("matrix") or {}
        with self.lock:
            self.messages = panel.pop("queue", [])
            self.panel = panel

    def _poll_diagnostics(self):
        try:
            data = _get("/api/diagnostics", timeout=8.0)
        except Exception:
            return
        if not data.get("ok"):
            return
        with self.lock:
            self.diagnostics = data.get("rows") or []
            self.unreachable = data.get("down") or []

    def _run(self):
        while not self._stop.is_set():
            try:
                with self.lock:
                    need_scenes = not self.scenes
                if need_scenes:
                    self._load_scenes()
                self._poll_status()
                # Diagnostics change slowly and cost a few subprocesses, so
                # they run on their own beat rather than every status poll.
                if time.monotonic() - self._diag_at > 10:
                    self._diag_at = time.monotonic()
                    self._poll_diagnostics()
                    self._poll_panel()
                with self.lock:
                    quick = self.busy or self.queued
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                with self.lock:
                    self.online = False
                quick = False
            self._stop.wait(0.7 if quick else 2.5)


# ----------------------------------------------------------------------- view

# --------------------------------------------------------------- output ---
#
# SDL's KMSDRM backend sets the mode and scans out its cursor plane on this Pi
# but never presents the primary plane, so the panel stays on whatever was
# drawn last while the app runs happily at 30fps. DOUBLEBUF does not change it.
#
# /dev/fb0 is the same display through the DRM driver's fbdev emulation, and it
# demonstrably works here: it is what put the console -- and a stale Python
# traceback -- on this screen. Writing pixels into it needs no EGL, no GBM, no
# DRM master and no planes, which removes every layer that has failed so far.

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


def _ioc(direction, kind, number, size):
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | number


class FrameBuffer:
    """The panel as a block of memory."""

    def __init__(self, path="/dev/fb0"):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)
        try:
            var = bytearray(160)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var, True)
            (self.width, self.height, _vw, _vh, _xo, _yo,
             self.bpp, _gray) = struct.unpack_from("<8I", var, 0)
            # fb_bitfield red/green/blue/transp, each offset/length/msb_right.
            self.red, self.green, self.blue, self.transp = [
                struct.unpack_from("<3I", var, 32 + i * 12) for i in range(4)]

            fix = bytearray(80)
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fix, True)
            self.stride = struct.unpack_from("<I", fix, 48)[0]
            if not self.stride:
                self.stride = self.width * (self.bpp // 8)
        except Exception:
            os.close(self.fd)
            raise

        if self.bpp not in (16, 32):
            os.close(self.fd)
            raise SystemExit("%s is %d bits per pixel; only 16 and 32 are handled"
                             % (path, self.bpp))
        self.map = mmap.mmap(self.fd, self.stride * self.height,
                             mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        # 32bpp on this hardware is XRGB8888, which in memory is B,G,R,X --
        # exactly pygame's "BGRA" byte order, so no per-pixel work is needed.
        self.packed = self.width * (self.bpp // 8) == self.stride

    def describe(self):
        return "%s %dx%d %dbpp stride %d (r@%d g@%d b@%d)" % (
            self.path, self.width, self.height, self.bpp, self.stride,
            self.red[0], self.green[0], self.blue[0])

    def _pixels(self, surface):
        if self.bpp == 32:
            return pygame.image.tostring(surface, "BGRA")
        # RGB565, the other format these panels come up in.
        import numpy
        view = pygame.surfarray.array3d(surface).swapaxes(0, 1).astype(numpy.uint16)
        packed = (((view[:, :, 0] >> 3) << 11)
                  | ((view[:, :, 1] >> 2) << 5)
                  | (view[:, :, 2] >> 3))
        return packed.tobytes()

    def blit(self, surface):
        data = self._pixels(surface)
        if self.packed:
            self.map.seek(0)
            self.map.write(data)
            return
        # Padded scanlines: copy a row at a time so the padding is left alone.
        row = self.width * (self.bpp // 8)
        for y in range(self.height):
            self.map.seek(y * self.stride)
            self.map.write(data[y * row:(y + 1) * row])

    def close(self):
        try:
            self.map.close()
        finally:
            os.close(self.fd)


class Touch:
    """Touches from the kernel, without SDL in the way.

    Reads evdev directly: a touchscreen reports absolute coordinates in its own
    units, so the ranges come from the driver rather than being assumed.
    """

    EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
    BTN_TOUCH = 0x14A
    ABS_X, ABS_Y = 0x00, 0x01
    ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
    EVENT_SIZE = struct.calcsize("llHHi")

    def __init__(self, width, height):
        self.width, self.height = width, height
        self.devices = []
        self.x = self.y = None
        self.down = False
        self._pending = None
        for path in sorted(self._candidates()):
            info = self._open(path)
            if info:
                self.devices.append(info)

    @staticmethod
    def _candidates():
        try:
            return ["/dev/input/" + name for name in os.listdir("/dev/input")
                    if name.startswith("event")]
        except OSError:
            return []

    def _absinfo(self, fd, axis):
        """EVIOCGABS: value, min, max, fuzz, flat, resolution."""
        buffer = bytearray(24)
        fcntl.ioctl(fd, _ioc(2, "E", 0x40 + axis, 24), buffer, True)
        return struct.unpack("<6i", buffer)

    def _open(self, path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None
        for axis_x, axis_y in ((self.ABS_MT_POSITION_X, self.ABS_MT_POSITION_Y),
                               (self.ABS_X, self.ABS_Y)):
            try:
                x_info = self._absinfo(fd, axis_x)
                y_info = self._absinfo(fd, axis_y)
            except OSError:
                continue
            if x_info[2] > x_info[1] and y_info[2] > y_info[1]:
                return {"fd": fd, "path": path,
                        "x": (axis_x, x_info[1], x_info[2]),
                        "y": (axis_y, y_info[1], y_info[2])}
        os.close(fd)
        return None

    def describe(self):
        if not self.devices:
            return "no touchscreen found in /dev/input"
        return ", ".join("%s x:%d-%d y:%d-%d" % (d["path"], d["x"][1], d["x"][2],
                                                 d["y"][1], d["y"][2])
                         for d in self.devices)

    def _scale(self, value, low, high, span):
        if high <= low:
            return 0
        return int((value - low) / float(high - low) * (span - 1))

    def poll(self):
        """Return (kind, x, y) tuples: 'down', 'move', 'up'."""
        if not self.devices:
            return []
        readable, _, _ = select.select([d["fd"] for d in self.devices], [], [], 0)
        out = []
        for device in self.devices:
            if device["fd"] not in readable:
                continue
            try:
                data = os.read(device["fd"], self.EVENT_SIZE * 64)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise
            for offset in range(0, len(data) - self.EVENT_SIZE + 1, self.EVENT_SIZE):
                _s, _us, kind, code, value = struct.unpack_from("llHHi", data, offset)
                if kind == self.EV_ABS:
                    if code == device["x"][0]:
                        self.x = self._scale(value, device["x"][1], device["x"][2],
                                             self.width)
                    elif code == device["y"][0]:
                        self.y = self._scale(value, device["y"][1], device["y"][2],
                                             self.height)
                elif kind == self.EV_KEY and code == self.BTN_TOUCH:
                    self._pending = "down" if value else "up"
                elif kind == self.EV_SYN:
                    if self.x is None or self.y is None:
                        continue
                    if self._pending == "down":
                        self.down = True
                        out.append(("down", self.x, self.y))
                    elif self._pending == "up":
                        self.down = False
                        out.append(("up", self.x, self.y))
                    elif self.down:
                        out.append(("move", self.x, self.y))
                    self._pending = None
        return out

    def close(self):
        for device in self.devices:
            try:
                os.close(device["fd"])
            except OSError:
                pass


# Font files, opened by path. Never pygame.font.SysFont: that shells out to
# fc-list, which Pi OS Lite does not ship, and rather than failing it stalls --
# the panel opens the display successfully and then hangs before drawing
# anything, which looks like a graphics problem and is not one.
# The design calls for Caprasimo (display) and Figtree (body). Neither ships
# with Pi OS Lite, so drop the .ttf files into /opt/vice-sign-lights/fonts to
# get the intended look; otherwise a bold DejaVu stands in, which keeps the
# weight contrast the layout depends on even though the character is different.
FONT_DIRS = (
    ("/opt/vice-sign-lights/fonts", "Figtree-SemiBold.ttf", "Caprasimo-Regular.ttf"),
    ("fonts", "Figtree-SemiBold.ttf", "Caprasimo-Regular.ttf"),
    ("/usr/share/fonts/truetype/dejavu", "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation",
     "LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/freefont", "FreeSans.ttf", "FreeSansBold.ttf"),
)


def _font_file(bold=False):
    for directory, regular, heavy in FONT_DIRS:
        candidate = os.path.join(directory, heavy if bold else regular)
        if os.path.exists(candidate):
            return candidate
    return None


MODE_HUES = {"red": "#f01e3c", "green": "#22c55e", "blue": "#2563eb",
             "yellow": "#f0b429", "cyan": "#22d3ee", "magenta": "#ff2fd0",
             "white": "#f3ece2"}


def _mode_ramp(label):
    """The colours a built-in pattern actually plays, from its audited name."""
    label = (label or "").lower()
    if "7 colour" in label or "rgb" in label:
        return ["#ff2f6e", "#f0b429", "#22c55e", "#22d3ee", "#8b5cf6"]
    for word, hexcode in MODE_HUES.items():
        if word in label:
            # A fade breathes one hue; show it dark-to-bright, not flat.
            return ["#1a1613", hexcode]
    return ["#ff2f6e", "#8b1a8b", "#22d3ee"]


def _sdl_touch_devices():
    """How many touchscreens SDL found. Zero here with a working picture means
    the input side is the problem, not the display."""
    try:
        return pygame.get_num_touch_devices()
    except Exception:
        return -1


def load_font(size, bold=False):
    path = _font_file(bold)
    if path:
        try:
            return pygame.font.Font(path, size)
        except Exception:
            pass
    # pygame ships a font of its own; ugly, but it always works.
    return pygame.font.Font(None, size)


class Button:
    __slots__ = ("rect", "label", "sub", "kind", "payload")

    def __init__(self, rect, label, kind, payload=None, sub=""):
        self.rect, self.label, self.kind, self.payload, self.sub = \
            pygame.Rect(rect), label, kind, payload, sub


class Panel:
    def __init__(self, size=None, fullscreen=True, backend=None):
        backend = backend or os.environ.get("VICE_KIOSK_BACKEND", "auto")
        if backend == "auto":
            # SDL/KMSDRM, which is proven on this hardware in the generator
            # build. VICE_KIOSK_BACKEND=fb switches to writing /dev/fb0
            # directly, which needs no EGL at all.
            backend = "sdl"
        self.backend = backend
        if backend == "fb":
            # Nothing is drawn by SDL, but pygame still wants a video system
            # initialised before it will make surfaces or load fonts.
            os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        try:
            pygame.display.init()
        except pygame.error as exc:
            raise SystemExit(
                "could not start the video driver (SDL_VIDEODRIVER=%s): %s\n"
                "On a Pi panel this wants kmsdrm, and SDL_KMSDRM_DEVICE_INDEX "
                "must name the card the display is on (see /sys/class/drm)."
                % (os.environ.get("SDL_VIDEODRIVER", "<unset>"), exc))
        pygame.font.init()
        self.fb = None
        self.touch = None
        self.screen = self._open_output(size, fullscreen, backend)
        self._hide_cursor()
        self.w, self.h = self.screen.get_size()

        # Everything is spaced from the artboards' 800x480, so one scale
        # factor carries the whole layout to another panel size.
        self.k = scale = self.w / 800.0
        self.f_sign  = load_font(int(56 * scale), bold=True)
        self.f_head2 = load_font(int(17 * scale), bold=True)
        # The tabs are the most-tapped thing on the screen and were set in
        # the same 13 points as a chip label. At arm's length on a 4.3-inch
        # panel, in dust, that is not a label so much as a rumour.
        self.f_tab   = load_font(int(19 * scale), bold=True)
        self.f_body  = load_font(int(13 * scale), bold=True)
        self.f_body2 = load_font(int(14 * scale), bold=True)
        self.f_small = load_font(int(12 * scale))
        self.f_tiny  = load_font(int(10 * scale), bold=True)

        self.sign = Sign()
        self.buttons = []
        self.actions = []
        self.tabs = []
        self.drag_from = None
        self.dragging = False
        self.last_point = (0, 0)
        self.tab = "scenes"
        # None, or the message being typed on the on-screen keyboard.
        self.compose = None
        # What the colour and pattern buttons act on. Scenes carry their own
        # targets, so this is ignored there.
        self.target = "all"
        self.target_name = "Everything"
        # 2b keeps the chosen colour and pattern lit, so a tap has a visible
        # result even while the sweep is still running.
        # Zones picked off the sign preview. Empty means the whole sign; the
        # chips and this are two views of the same choice, so setting either
        # clears the other.
        self.zones = []
        self.chosen_colour = None
        self.chosen_pattern = None
        self.speed = PATTERN_SPEED
        # Seconds between devices. Writes are serialised anyway, so a pattern
        # started one unit at a time already sweeps across the sign; this makes
        # the roll deliberate. Off means "as fast as the radio manages".
        self.roll = 0.0
        # A pending prompt, drawn over the middle third. Nothing that changes
        # the machine happens without one.
        self.confirm = None
        # Locked at boot, and again after LOCK_AFTER untouched. Locked means
        # no control writes to the sign. It does NOT mean the screen goes
        # away: reading it without hunting for a phone is the whole reason it
        # is out there, so everything stays drawn, live and legible.
        self.locked = True
        self.touched = time.monotonic()
        # The shelves scroll by the page, via the "›" card at the end.
        self.shelf = {"scenes": 0, "solid": 0}

    @staticmethod
    def _hide_cursor():
        """Get rid of the pointer, after the display exists.

        Under KMSDRM the cursor lives on its own hardware plane, created by
        set_mode -- so hiding it beforehand is asking a window that does not
        exist yet, and the plane comes up visible regardless. The probe also
        re-inits the display for each card it tries, which would undo an early
        call anyway. A blank cursor as well as set_visible(False), because on
        some drivers only one of the two takes.
        """
        try:
            pygame.mouse.set_visible(False)
        except Exception:
            pass
        try:
            blank = pygame.cursors.Cursor((8, 8), (0, 0),
                                          (0,) * 8, (0,) * 8)
            pygame.mouse.set_cursor(blank)
        except Exception:
            pass

    def _open_output(self, size, fullscreen, backend):
        if backend != "fb":
            if fullscreen and os.environ.get("SDL_VIDEODRIVER") == "kmsdrm" \
                    and not os.environ.get("SDL_KMSDRM_DEVICE_INDEX"):
                screen = self._probe_kmsdrm(size, fullscreen)
            else:
                screen = self._open_display(size, fullscreen)
            pygame.display.set_caption("Vice Sign")
            # The panel must never blank: it is the only control at the sign.
            try:
                pygame.display.set_allow_screensaver(False)
            except Exception:
                pass
            # SDL draws, the kernel supplies the touches. Worth having when SDL
            # renders correctly but reports no input, which is a common way for
            # a DSI panel to half-work.
            if os.environ.get("VICE_KIOSK_INPUT") == "evdev":
                width, height = screen.get_size()
                self.touch = Touch(width, height)
                print("touch (evdev): " + self.touch.describe(), flush=True)
            return screen
        self.fb = FrameBuffer(os.environ.get("VICE_KIOSK_FB", "/dev/fb0"))
        print("framebuffer: " + self.fb.describe(), flush=True)
        self.touch = Touch(self.fb.width, self.fb.height)
        print("touch: " + self.touch.describe(), flush=True)
        # A plain off-screen surface; it reaches the panel through fb.blit.
        return pygame.Surface((self.fb.width, self.fb.height))

    @staticmethod
    def _drm_indices():
        try:
            return sorted(int(name[4:]) for name in os.listdir("/dev/dri")
                          if name.startswith("card") and name[4:].isdigit())
        except OSError:
            return []

    @classmethod
    def _probe_kmsdrm(cls, size, fullscreen):
        """Find the DRM device that is actually a display.

        Not every /dev/dri/cardN is one: on a Pi 4 the render-only v3d node
        takes card0 and the vc4 display controller takes card1. Pinning the
        wrong one fails in ways that send you hunting through /dev/input for a
        problem that is really the wrong card. Probing and reporting the choice
        beats asserting it.
        """
        errors = []
        for index in cls._drm_indices():
            os.environ["SDL_KMSDRM_DEVICE_INDEX"] = str(index)
            try:
                pygame.display.quit()
                pygame.display.init()
                screen = cls._open_display(size, fullscreen)
                print("panel: display on DRM device %d (KMSDRM)" % index,
                      flush=True)
                print("panel: SDL sees %d touch device(s)" % _sdl_touch_devices(),
                      flush=True)
                return screen
            except (pygame.error, SystemExit) as exc:
                errors.append("card%d: %s" % (index, exc))
        os.environ.pop("SDL_KMSDRM_DEVICE_INDEX", None)
        raise SystemExit("no DRM device would open a display.\n  "
                         + "\n  ".join(errors or ["/dev/dri is empty"]))

    @staticmethod
    def _open_display(size, fullscreen):
        """Open the panel, trying the least presumptuous mode first.

        On KMSDRM an explicit size has to match a mode the connector actually
        advertises; asking for 800x480 when the panel reports slightly
        different timings fails inside EGL, which surfaces as the unhelpful
        "EGL not initialized". (0, 0) means "whatever this display already is",
        which is what a panel bolted to a sign should use anyway.
        """
        attempts = []
        if fullscreen:
            attempts.append(((0, 0), pygame.FULLSCREEN, "native fullscreen"))
            if size:
                attempts.append((size, pygame.FULLSCREEN, "%dx%d fullscreen" % size))
            attempts.append(((0, 0), 0, "native, windowed"))
        else:
            attempts.append((size or (800, 480), 0, "windowed"))

        errors = []
        for wanted, flags, described in attempts:
            try:
                screen = pygame.display.set_mode(wanted, flags)
                print("display: %s -> %dx%d" % (described, *screen.get_size()),
                      flush=True)
                return screen
            except pygame.error as exc:
                errors.append("%s: %s" % (described, exc))
        raise SystemExit("could not open the display.\n  " + "\n  ".join(errors))

    # -- layout ---------------------------------------------------------------
    #
    # Fixed shell from the artboards: preview band, pill tabs, bottom bar. Only
    # the middle third changes between tabs, and nothing scrolls -- the design's
    # point is that no control ever hides below a fold.

    def measure(self):
        """Where the four bands live. Nothing here scrolls, by design.

        The tabs and the action row traded places. Two reasons, and the second
        is the one that mattered: a thumb reaches the bottom of a panel this
        size without the hand covering the sign preview, and every phone in
        every pocket already puts its navigation there, so nobody has to be
        told which end to look at.

        The middle keeps its 228 pixels to the pixel. The Colour tab is built
        from four fixed rows that come to exactly that, so the eight pixels
        the action row gives up and the eight the tab row takes have to come
        from each other rather than from the content.
        """
        s = self.k
        self.band = pygame.Rect(0, 0, self.w, int(110 * s))
        # Was the bottom bar, at 52. Now directly under the sign, so what it
        # says about the sign sits next to the sign.
        self.controlrow = pygame.Rect(0, self.band.bottom, self.w, int(56 * s))
        # Hard against the bottom edge, near enough. A margin under the tab
        # row buys nothing -- a thumb aimed at the edge of a panel wants
        # something there -- and spending it here instead keeps a full 12
        # pixels between the tabs and the last row of the tab above, which is
        # what stops the Colour tab's speed slider from having the tab row
        # crowd its touch target.
        tab_h = int(72 * s)
        self.tabrow = pygame.Rect(0, self.h - tab_h - int(2 * s), self.w, tab_h)
        self.middle = pygame.Rect(int(16 * s), self.controlrow.bottom,
                                  self.w - int(32 * s),
                                  self.tabrow.y - int(12 * s)
                                  - self.controlrow.bottom)

    # -- the sign itself ------------------------------------------------------

    LETTERS = ("V", "I", "C", "E")

    def zone_colours(self, state):
        """What each zone of the sign is showing, and whether it is answering.

        The preview is the centrepiece of the design, so it is drawn from the
        controllers' actual reported state rather than being a fixed diagram.
        A letter stands for both sides, so it dims if either side is silent.
        """
        by_name = {d["name"]: d for d in state["devices"]}

        def look(*names):
            colour, ok = None, True
            for name in names:
                device = by_name.get(name)
                if not device:
                    continue
                if device.get("reachable") is False:
                    ok = False
                showing = device.get("showing") or {}
                if colour is None and showing.get("power") is not False:
                    if showing.get("mode"):
                        colour = "pattern"
                    elif showing.get("color"):
                        colour = showing["color"]
            return colour, ok

        zones = {}
        for letter in self.LETTERS:
            zones[letter] = look("A_" + letter, "B_" + letter)
        zones["A_Cup"] = look("A_Cup")
        zones["B_Cup"] = look("B_Cup")
        zones["A_Straw"] = look("A_Straw")
        zones["B_Straw"] = look("B_Straw")
        return zones

    @staticmethod
    def _lit(value, fallback=(90, 84, 78)):
        """A zone's drawing colour. 'pattern' means it is animating."""
        if value == "pattern":
            return None                      # drawn as a gradient instead
        if not value:
            return fallback
        try:
            colour = pygame.Color(value)
            return (colour.r, colour.g, colour.b)
        except ValueError:
            return fallback

    def glow_text(self, font, glyph, colour, spot, strength=1.0):
        """Draw a glyph with the boards' halo behind it.

        The halo is the glyph itself, scaled up and added at low alpha, so it
        follows the letterform. A plain circle -- which is what this was first
        -- swamps the very letter it is meant to be lighting.
        """
        base = font.render(glyph, True, colour)
        if strength > 0:
            # Plain alpha blending, not additive: font.render fills the whole
            # rect with the colour and varies only alpha, so an additive blit
            # adds full colour across the glyph's bounding box and paints a
            # solid block where a halo was wanted.
            for scale, alpha in ((1.34, 34), (1.18, 46), (1.07, 58)):
                size = (max(1, int(base.get_width() * scale)),
                        max(1, int(base.get_height() * scale)))
                halo = pygame.transform.smoothscale(base, size)
                halo.set_alpha(int(alpha * strength))
                self.screen.blit(halo, halo.get_rect(center=spot.center))
        self.screen.blit(base, spot)

    def glow_rect(self, rect, colour, strength=1.0):
        """The same idea for the straws, which are shapes rather than glyphs."""
        pad = int(9 * self.k)
        layer = pygame.Surface((rect.w + pad * 2, rect.h + pad * 2), pygame.SRCALPHA)
        for step, alpha in ((pad, 26), (pad // 2, 40)):
            spread = pygame.Rect(pad - step, pad - step,
                                 rect.w + step * 2, rect.h + step * 2)
            pygame.draw.rect(layer, tuple(colour) + (int(alpha * strength),), spread,
                             border_radius=max(1, spread.w // 2))
        self.screen.blit(layer, (rect.x - pad, rect.y - pad))
        # The crisp shape goes back on top of its own halo.
        self.rounded(self.screen, rect, colour, radius=max(1, rect.w // 2))

    def draw_preview(self, state):
        s = self.k
        pad = int(16 * s)
        rect = pygame.Rect(pad, int(12 * s), self.w - pad * 2 - int(208 * s),
                           self.band.h - int(22 * s))
        self.rounded(self.screen, rect, CARD, LINE, radius=int(16 * s))
        zones = self.zone_colours(state)
        by_address = {d["name"]: d["address"] for d in state["devices"]}

        # Wordmark. One glyph per letter, both sides behind it, and each a
        # target you can tap -- so "colour just the C" needs no chip at all.
        x = rect.x + int(16 * s)
        baseline = rect.centery
        for letter in self.LETTERS:
            value, ok = zones[letter]
            colour = self._lit(value, (120, 112, 104))
            if colour is None:
                colour = CYAN
            if not ok:
                colour = over(INK, 0.14, CARD)
            spot = self.f_sign.render(letter, True, colour).get_rect(
                midleft=(x, baseline))
            hit = spot.inflate(int(10 * s), int(18 * s))
            self.zone_chrome(hit, "group:" + letter)
            self.glow_text(self.f_sign, letter, colour, spot,
                           1.0 if (ok and value) else 0.0)
            self.buttons.append(Button(hit, letter, "zone", "group:" + letter))
            x = hit.right + int(2 * s)

        # The drink: cup outline, straw, cup outline, straw -- each its own
        # target, so a single straw can be picked without going to Lights.
        x += int(12 * s)
        for name, kind in (("A_Cup", "cup"), ("A_Straw", "straw"),
                           ("B_Cup", "cup"), ("B_Straw", "straw")):
            value, ok = zones[name]
            colour = self._lit(value, (70, 65, 60))
            if colour is None:
                colour = CYAN
            if not ok:
                colour = over(INK, 0.14, CARD)
            device = by_address.get(name)
            # A shape with no configured device draws but does not tap: the
            # "name:" token it used to send was never a target the server
            # validates, so the tap silently did nothing anyway.
            token = ("device:" + device) if device else None
            if kind == "cup":
                shape = pygame.Rect(x, baseline - int(35 * s),
                                    int(54 * s), int(70 * s))
                hit = shape.inflate(int(10 * s), int(14 * s))
                self.zone_chrome(hit, token)
                pygame.draw.rect(self.screen, colour, shape,
                                 width=max(2, int(4 * s)),
                                 border_top_left_radius=int(7 * s),
                                 border_top_right_radius=int(7 * s),
                                 border_bottom_left_radius=int(18 * s),
                                 border_bottom_right_radius=int(18 * s))
            else:
                shape = pygame.Rect(x + int(6 * s), baseline - int(43 * s),
                                    int(10 * s), int(86 * s))
                hit = shape.inflate(int(22 * s), int(10 * s))
                self.zone_chrome(hit, token)
                self.rounded(self.screen, shape, colour, radius=int(5 * s))
                if ok and value:
                    self.glow_rect(shape, colour)
            if token:
                self.buttons.append(Button(hit, name, "zone", token))
            x = hit.right + int(4 * s)

        # Now playing, and the health chips.
        right = rect.right - int(18 * s)
        y = rect.y + int(14 * s)
        self.text_right(self.f_tiny, "NOW PLAYING", MUTED, right, y)
        y += int(16 * s)
        self.text_right(self.f_head2, state["playing"] or "--", INK, right, y)
        y += int(28 * s)
        chips = []
        total, bad = state["devices_total"], state["devices_bad"]
        chips.append(("%d lit" % max(0, total - bad), OLIVE, over(OLIVE, 0.16, CARD)))
        if bad:
            chips.append(("%d down" % bad, PINK_SOFT, over(PINK, 0.14, CARD)))
        cx = right
        for label, ink, fill in reversed(chips):
            width = self.f_tiny.size(label)[0] + int(18 * s)
            chip = pygame.Rect(cx - width, y, width, int(18 * s))
            self.rounded(self.screen, chip, fill, radius=int(9 * s))
            self.text(self.screen, self.f_tiny, label, ink, center=chip.center)
            cx = chip.x - int(6 * s)

        # The battery tile, where SAVE THIS used to apologise. Saving a scene
        # needs a name and names need the phone; the battery flattening is the
        # failure that nearly ended the sign, so its clock gets the corner.
        self.draw_battery(state, pygame.Rect(rect.right + int(14 * s), rect.y,
                                             self.w - pad - rect.right - int(14 * s),
                                             rect.h))

    def draw_battery(self, state, tile):
        s = self.k
        battery = state.get("battery") or {}
        tripped = battery.get("tripped_at")
        warning = battery.get("warning")
        seconds = battery.get("seconds_left")

        if tripped:
            fill, edge, ink = over(PINK, 0.18), PINK, PINK_SOFT
        elif warning:
            fill, edge, ink = over(ORANGE, 0.14), over(ORANGE, 0.6), ORANGE
        else:
            fill, edge, ink = CARD, LINE, INK
        self.rounded(self.screen, tile, fill, edge, radius=int(16 * s))

        if not battery.get("enabled"):
            big, note, ink = "no limit", "set one on the phone", MUTED
        elif tripped:
            big, note = "TRIPPED", "tap to re-arm"
        elif seconds is None:
            big, note, ink = "dark", "budget paused", MUTED
        else:
            minutes = max(0, int(seconds)) // 60
            big = "%dh %02dm" % (minutes // 60, minutes % 60) if minutes >= 60 \
                else "%dm" % minutes
            note = "left · WARNING" if warning else "left on the battery"
        self.text(self.screen, self.f_tiny, "BATTERY", FAINT,
                  center=(tile.centerx, tile.y + int(16 * s)))
        self.text(self.screen, self.f_head2, big, ink,
                  center=(tile.centerx, tile.centery + int(2 * s)))
        self.text(self.screen, self.f_tiny, note,
                  ink if (tripped or warning) else MUTED,
                  center=(tile.centerx, tile.bottom - int(15 * s)))
        self.buttons.append(Button(tile, "BATTERY", "battery"))

    def zone_chrome(self, rect, token):
        """Mark a preview shape as picked. Drawn behind the shape itself."""
        if token in self.zones:
            self.rounded(self.screen, rect, over(CYAN, 0.16, CARD), CYAN,
                         radius=int(12 * self.k), width=max(1, int(2 * self.k)))

    # -- shell ----------------------------------------------------------------

    def draw_tabs(self, state):
        """The four tabs, along the bottom.

        They were pills at the top set in 13 points: a 26-pixel target for the
        most-tapped control on the screen. Now they are 72 tall in 19 points,
        and they share the full width proportionally rather than hugging their
        own text -- which turns 240 pixels of leftover background into target.

        Proportional rather than four equal cells on purpose: "LED Text
        Display" is three times the width of "Colour", and equal cells would
        have to be sized for the longest label, leaving that one nearly
        touching its own edges while the other three sat in acres of nothing.

        Tabs stay live when the screen is locked. Moving between them changes
        nothing about the sign, and being able to read the System tab without
        unlocking is most of what this panel is for.
        """
        s = self.k
        row, pad, gap = self.tabrow, int(16 * s), int(8 * s)
        bite = int(28 * s)
        texts = [self.f_tab.size(label)[0] for _key, label in TABS]
        room = row.w - pad * 2 - gap * (len(TABS) - 1)
        share = max(0, (room - sum(texts) - bite * len(TABS))) // len(TABS)

        x = pad
        self.tabs = []
        for (key, label), text_w in zip(TABS, texts):
            pill = pygame.Rect(x, row.y, text_w + bite + share, row.h)
            live = key == self.tab
            self.rounded(self.screen, pill, INK if live else CARD,
                         None if live else LINE_SOFT, radius=int(16 * s))
            self.text(self.screen, self.f_tab, label,
                      BG if live else over(INK, 0.72), center=pill.center)
            self.tabs.append(Button(pill, label, "tab", key))
            x = pill.right + gap

    # -- the control row ------------------------------------------------------

    def draw_control(self, state):
        """The row under the sign: what you can do, or what is being done.

        Three states, and only ever one at a time, because each of them wants
        the whole 800 pixels rather than a third of it.
        """
        s = self.k
        self.actions = []
        bar = pygame.Rect(int(16 * s), self.controlrow.y + int(2 * s),
                          self.w - int(32 * s), self.controlrow.h - int(8 * s))
        if self.locked:
            self.draw_unlock(state, bar)
        elif state["busy"] or state["queued"] or state["job"]:
            self.draw_working(state, bar)
        else:
            self.draw_actions(state, bar)

    def draw_actions(self, state, bar):
        """Idle: the three things worth doing without opening a tab."""
        s = self.k
        rotation = state["rotation"] or {}
        off = pygame.Rect(bar.x, bar.y, int(190 * s), bar.h)
        self.rounded(self.screen, off, over(PINK, 0.10), over(PINK, 0.35),
                     radius=off.h // 2)
        self.stack(off, "OFF", PINK_SOFT, self.where_name(), over(PINK_SOFT, 0.65))
        self.actions.append(Button(off, "OFF", "off"))

        rot_w = int(210 * s)
        rot = pygame.Rect(bar.right - rot_w, bar.y, rot_w, bar.h)
        on = bool(rotation.get("enabled"))
        self.rounded(self.screen, rot,
                     over(OLIVE, 0.12) if on else CARD,
                     over(OLIVE, 0.40) if on else over(WHITE, 0.08),
                     radius=rot.h // 2)
        self.stack(rot, "ROTATE ON" if on else "ROTATE OFF",
                   OLIVE_SOFT if on else INK, self.rotation_note(rotation),
                   over(OLIVE_SOFT, 0.7) if on else MUTED)
        self.actions.append(Button(rot, "ROTATE", "rotate"))

        mid = pygame.Rect(off.right + int(10 * s), bar.y,
                          rot.x - off.right - int(20 * s), bar.h)
        self.rounded(self.screen, mid, CARD, over(WHITE, 0.08), radius=mid.h // 2)
        self.stack(mid, "SURPRISE ME", INK, "new scene", MUTED)
        self.actions.append(Button(mid, "SURPRISE ME", "next"))

    def draw_working(self, state, bar):
        """A sweep is running, so the row becomes the thing that watches it.

        The three actions are gone while this is up, deliberately. All three
        queue more radio work, and mid-sweep that is the wrong answer to every
        question; the right one is seeing how far along it is and being able
        to kill it. A sweep takes ~30s across twelve controllers, which is
        long enough that "is it stuck?" is the question actually being asked.

        STOP sits at the right, where ROTATE was rather than where OFF was. A
        finger already travelling towards OFF when a sweep starts then lands
        on dead label instead of throwing the queue away -- and STOP takes no
        touch slop either (see NEVER_SLOP), so only a direct hit counts.
        """
        s = self.k
        self.rounded(self.screen, bar, CARD_ALT, over(CYAN, 0.22),
                     radius=int(16 * s))
        # Full bar height, not inset: the control row is 56 pixels and cannot
        # grow without taking them off the Colour tab, so 48 is the ceiling
        # here and STOP gets all of it. 150x48 against the old strip's 96x48.
        stop = pygame.Rect(bar.right - int(154 * s), bar.y,
                           int(150 * s), bar.h)
        self.rounded(self.screen, stop, over(PINK, 0.18), PINK,
                     radius=stop.h // 2)
        self.text(self.screen, self.f_head2, "STOP", PINK_SOFT,
                  center=stop.center)
        self.buttons.append(Button(stop, "STOP", "stop-all"))

        job = state["job"] or {}
        label = (job.get("label") or "working").replace("scene: ", "")
        total = max(1, state["total"])
        count = "%d/%d" % (state["done"], total)
        if state["queued"]:
            count += "  +%d" % state["queued"]
        info = pygame.Rect(bar.x + int(14 * s), bar.y,
                           stop.x - int(12 * s) - bar.x - int(14 * s), bar.h)

        counted = self.f_body.render(count, True, CYAN_SOFT)
        track_w = int(120 * s)
        track = pygame.Rect(info.right - counted.get_width() - int(10 * s)
                            - track_w, info.centery - int(3 * s),
                            track_w, int(7 * s))
        self.rounded(self.screen, track, over(WHITE, 0.08), radius=int(3 * s))
        filled = int(track.w * state["done"] / float(total))
        if filled:
            self.rounded(self.screen,
                         pygame.Rect(track.x, track.y, filled, track.h),
                         CYAN, radius=int(3 * s))
        self.screen.blit(counted, (info.right - counted.get_width(),
                                   info.centery - counted.get_height() // 2))
        room = track.x - int(12 * s) - info.x
        if room > int(50 * s):
            while label and self.f_body2.size(label)[0] > room:
                label = label[:-1]
            self.text(self.screen, self.f_body2, label, INK,
                      topleft=(info.x,
                               info.centery - self.f_body2.get_height() // 2))
        # Appended last so a tap on STOP finds STOP: _hit returns the first
        # rectangle it lands in, and this one contains that one.
        self.buttons.append(Button(info, "activity", "activity"))

    def draw_unlock(self, state, bar):
        """Locked. One tap here and the sign can be changed again.

        Only the writing controls go away. The sign preview, the battery
        clock, the health chips and all four tabs stay live, because the
        failure this panel exists to catch is noticing at 3am that something
        is wrong -- and a screen that has to be unlocked before it will tell
        you anything is a screen nobody looks at.

        When a sweep is running the job shows here too. Seeing it costs
        nothing; stopping it costs one tap first, which is the trade.
        """
        s = self.k
        self.rounded(self.screen, bar, over(CYAN, 0.10), over(CYAN, 0.40),
                     radius=int(16 * s))
        busy = state["busy"] or state["queued"] or state["job"]

        said = "TOUCH TO UNLOCK"
        width = self.f_head2.size(said)[0]
        lock_h = int(22 * s)
        # Centred as a unit, padlock and words together, unless a job is
        # showing on the right -- then it sits left so the two do not collide.
        block = width + int(14 * s) + int(lock_h * 0.8)
        left = bar.x + int(18 * s) if busy else bar.centerx - block // 2
        self.padlock((left + int(lock_h * 0.4), bar.centery), lock_h, CYAN)
        self.text(self.screen, self.f_head2, said, CYAN_SOFT,
                  topleft=(left + int(lock_h * 0.8) + int(14 * s),
                           bar.centery - self.f_head2.get_height() // 2))

        if busy:
            job = state["job"] or {}
            label = (job.get("label") or "working").replace("scene: ", "")
            total = max(1, state["total"])
            note = "%s  %d/%d" % (label, state["done"], total)
            room = bar.right - int(18 * s) - (left + block + int(20 * s))
            while note and self.f_body2.size(note)[0] > room:
                note = note[:-1]
            image = self.f_body2.render(note, True, MUTED)
            self.screen.blit(image, (bar.right - int(18 * s) - image.get_width(),
                                     bar.centery - image.get_height() // 2))
        self.actions.append(Button(bar, "UNLOCK", "unlock"))

    def padlock(self, centre, height, colour):
        """A padlock, drawn rather than typed.

        The fallback DejaVu has no lock glyph, and a tofu box on the one
        control that explains why nothing else is responding would be the
        worst possible place for a missing character.
        """
        s = self.k
        thick = max(2, int(3 * s))
        body = pygame.Rect(0, 0, int(height * 0.80), int(height * 0.56))
        body.midbottom = (centre[0], centre[1] + height // 2)
        self.rounded(self.screen, body, colour, radius=max(2, int(3 * s)))
        arc = pygame.Rect(0, 0, int(body.w * 0.62), int(body.w * 0.62))
        arc.midtop = (centre[0], centre[1] - height // 2)
        pygame.draw.arc(self.screen, colour, arc, 0, math.pi, thick)
        for side in (arc.left, arc.right - thick):
            pygame.draw.rect(self.screen, colour,
                             pygame.Rect(side, arc.centery, thick,
                                         max(1, body.top - arc.centery)))

    @staticmethod
    def rotation_note(rotation):
        if not rotation.get("enabled"):
            return "tap to start"
        if rotation.get("holding"):
            return "held %dm \u00b7 edit" % max(
                1, round(rotation.get("hold_remaining_seconds", 0) / 60))
        seconds = rotation.get("next_in_seconds")
        if seconds is None:
            return "edit"
        seconds = max(0, int(seconds))
        when = "%dm" % max(1, round(seconds / 60)) if seconds >= 60 else "%ds" % seconds
        return "changes in %s \u00b7 edit" % when

    def stack(self, rect, title, ink, sub, sub_ink):
        s = self.k
        self.text(self.screen, self.f_head2, title, ink,
                  center=(rect.centerx, rect.centery - int(8 * s)))
        self.text(self.screen, self.f_tiny, sub, sub_ink,
                  center=(rect.centerx, rect.centery + int(11 * s)))

    # -- tabs -----------------------------------------------------------------

    def divider(self, label, y, width, x=None):
        s = self.k
        x = self.middle.x if x is None else x
        image = self.f_tiny.render(label.upper(), True, FAINT)
        self.screen.blit(image, (x, y))
        line_x = x + image.get_width() + int(10 * s)
        pygame.draw.line(self.screen, LINE, (line_x, y + image.get_height() // 2),
                         (x + width, y + image.get_height() // 2))
        return y + image.get_height() + int(10 * s)

    def draw_scenes(self, state):
        """One horizontal shelf per group -- animated cards, then solid pills."""
        s = self.k
        animated = [x for x in state["scenes"] if x["animated"]]
        solid = [x for x in state["scenes"] if not x["animated"]]
        # A stale page offset -- scenes deleted on the phone mid-session --
        # must not strand the shelf on an empty page.
        for name, total in (("scenes", len(animated)), ("solid", len(solid))):
            if self.shelf[name] >= total:
                self.shelf[name] = 0
        y = self.middle.y
        card_w, card_h = int(126 * s), int(100 * s)

        y = self.divider("Animated", y, self.middle.w)
        x = self.middle.x
        shown = 0
        for scene in animated[self.shelf["scenes"]:]:
            if x + card_w > self.middle.right - int(80 * s):
                break
            self.scene_card(pygame.Rect(x, y, card_w, card_h), scene, state)
            x += card_w + int(10 * s)
            shown += 1
        # The paging card only when there is somewhere to page to: a "more"
        # arrow beside a shelf that already fits is a button that lies.
        if self.shelf["scenes"] or shown < len(animated):
            self.more_card(pygame.Rect(x, y, int(74 * s), card_h), "scenes",
                           len(animated))
        y += card_h + int(14 * s)

        y = self.divider("Solid", y, self.middle.w)
        x = self.middle.x
        pill_h = int(48 * s)
        shown = 0
        for scene in solid[self.shelf["solid"]:]:
            if x + card_w > self.middle.right - int(80 * s):
                break
            self.solid_pill(pygame.Rect(x, y, card_w, pill_h), scene, state)
            x += card_w + int(10 * s)
            shown += 1
        if self.shelf["solid"] or shown < len(solid):
            self.more_card(pygame.Rect(x, y, int(74 * s), pill_h), "solid",
                           len(solid))

    def scene_card(self, rect, scene, state):
        s = self.k
        name = scene["name"]
        playing = name == state["playing"] and name != state["pending"]
        pending = name == state["pending"]
        if pending:
            self.rounded(self.screen, rect, over(CYAN, 0.14), CYAN,
                         radius=int(16 * s), width=max(1, int(1.5 * s)))
        elif playing:
            self.rounded(self.screen, rect, CARD, PINK, radius=int(16 * s),
                         width=max(1, int(1.5 * s)))
        else:
            self.rounded(self.screen, rect, CARD, LINE, radius=int(16 * s))
        swatch = pygame.Rect(rect.x + int(10 * s), rect.y + int(10 * s),
                             rect.w - int(20 * s), int(26 * s))
        self.gradient(swatch, scene["ramp"], int(8 * s))
        self.text(self.screen, self.f_head2, name, INK,
                  topleft=(rect.x + int(10 * s), swatch.bottom + int(8 * s)))
        note, ink = ("sending\u2026", CYAN_SOFT) if pending else \
                    (("playing", PINK) if playing else ("built in", FAINT))
        self.text(self.screen, self.f_tiny, note, ink,
                  topleft=(rect.x + int(10 * s), rect.bottom - int(20 * s)))
        self.buttons.append(Button(rect, name, "scene", name))

    def solid_pill(self, rect, scene, state):
        s = self.k
        name = scene["name"]
        playing = name == state["playing"] and name != state["pending"]
        pending = name == state["pending"]
        self.rounded(self.screen, rect, over(CYAN, 0.14) if pending else CARD,
                     CYAN if pending else (PINK if playing else LINE),
                     radius=int(14 * s), width=max(1, int(1.5 * s)) if
                     (pending or playing) else 1)
        dot = (rect.x + int(21 * s), rect.centery)
        colour = self._lit(scene["ramp"][0], (120, 112, 104))
        pygame.draw.circle(self.screen, colour, dot, int(7 * s))
        self.text(self.screen, self.f_body2, name, INK,
                  topleft=(dot[0] + int(16 * s),
                           rect.centery - self.f_body2.get_height() // 2))
        self.buttons.append(Button(rect, name, "scene", name))

    def more_card(self, rect, shelf, total):
        s = self.k
        self.rounded(self.screen, rect, over(WHITE, 0.03), over(WHITE, 0.06),
                     radius=int(16 * s))
        self.text(self.screen, self.f_head2, "\u203a", DIM, center=rect.center)
        self.buttons.append(Button(rect, "more", "shelf", (shelf, total)))

    def gradient(self, rect, ramp, radius):
        """The scene's colours as a left-to-right ramp, like the boards."""
        strip = pygame.Surface((max(1, rect.w), max(1, rect.h)))
        stops = [self._lit(c, (90, 84, 78)) or CYAN for c in ramp] or [CARD]
        if len(stops) == 1:
            stops = stops * 2
        span = rect.w / float(len(stops) - 1)
        for x in range(rect.w):
            i = min(len(stops) - 2, int(x / span))
            t = (x - i * span) / span
            colour = tuple(int(stops[i][c] + (stops[i + 1][c] - stops[i][c]) * t)
                           for c in range(3))
            pygame.draw.line(strip, colour, (x, 0), (x, rect.h))
        mask = pygame.Surface(strip.get_size(), pygame.SRCALPHA)
        pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(),
                         border_radius=radius)
        strip = strip.convert_alpha()
        strip.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        self.screen.blit(strip, rect.topleft)

    def draw_colour(self, state):
        """Four fixed rows: targets, swatches, patterns, speed.

        Fixed on purpose. The old version flowed and wrapped, which meant its
        height depended on the data and it quietly overflowed the middle once
        the chips grew to finger size. Budget: 48 + 12 + 54 + 12 + 48 + 12
        + 42 = 228 -- exactly the middle, ending flush at its bottom edge.

        The round V/I/C/E chips and the Side A/B chips are gone: the sign
        preview above is the per-letter picker (tap the letters themselves),
        and per-side targeting has never once been wanted from the kiosk.
        Dropping them is what buys a single un-wrapped 48-pixel target row.
        """
        s = self.k
        x, y = self.middle.x, self.middle.y

        chip_h = int(48 * s)
        for target, label in self.target_options(state):
            width = self.f_small.size(label)[0] + int(30 * s)
            if x + width > self.middle.right:
                break                 # one row; the rest stay on the phone
            chip = pygame.Rect(x, y, width, chip_h)
            on = target == self.target
            self.rounded(self.screen, chip, over(CYAN, 0.16) if on else CHIP_BG,
                         over(CYAN, 0.6) if on else None, radius=chip_h // 2)
            self.text(self.screen, self.f_small, label,
                      CYAN_SOFT if on else over(INK, 0.62), center=chip.center)
            self.buttons.append(Button(chip, label, "target", (target, label)))
            x = chip.right + int(8 * s)
        y += chip_h + int(12 * s)

        size = int(54 * s)
        step = (self.middle.w - size) / float(len(SWATCHES) - 1)
        for index, (hexcode, name) in enumerate(SWATCHES):
            spot = pygame.Rect(int(self.middle.x + index * step), y, size, size)
            colour = pygame.Color(hexcode)
            pygame.draw.circle(self.screen, (colour.r, colour.g, colour.b),
                               spot.center, size // 2)
            if hexcode == self.chosen_colour:
                pygame.draw.circle(self.screen, BG, spot.center,
                                   size // 2 + int(3 * s), width=int(3 * s))
                pygame.draw.circle(self.screen, INK, spot.center,
                                   size // 2 + int(5 * s), width=int(2 * s))
            self.buttons.append(Button(spot, name, "swatch", hexcode))
        y += size + int(12 * s)

        x = self.middle.x
        pattern_h = int(48 * s)
        for pattern in self.pattern_choices(state):
            width = self.f_small.size(pattern["label"])[0] + int(34 * s)
            pill = pygame.Rect(x, y, width, pattern_h)
            on = pattern["value"] == self.chosen_pattern
            self.rounded(self.screen, pill, over(CYAN, 0.14) if on else CARD,
                         CYAN if on else over(WHITE, 0.08), radius=pattern_h // 2)
            self.text(self.screen, self.f_small, pattern["label"],
                      CYAN_SOFT if on else INK, center=pill.center)
            self.buttons.append(Button(pill, pattern["label"], "pattern",
                                       pattern["value"]))
            x = pill.right + int(8 * s)

        roll = pygame.Rect(self.middle.right - int(96 * s), y, int(96 * s),
                           pattern_h)
        on = self.roll > 0
        self.rounded(self.screen, roll, over(ORANGE, 0.14) if on else CARD,
                     ORANGE if on else over(WHITE, 0.08), radius=pattern_h // 2)
        self.text(self.screen, self.f_small,
                  "ROLL %.1fs" % self.roll if on else "ROLL off",
                  ORANGE if on else over(INK, 0.6), center=roll.center)
        self.buttons.append(Button(roll, "roll", "roll"))
        y += pattern_h + int(12 * s)

        # Speed, its own full-width row -- and honest about when it applies.
        # It only means anything while a pattern is chosen; the old version
        # silently stored the value, which read as a broken slider.
        active = self.chosen_pattern is not None
        label_text = "SPEED" if active else "SPEED  (pick a pattern first)"
        label = self.f_tiny.render(label_text, True,
                                   FAINT if active else over(INK, 0.25))
        self.screen.blit(label, (self.middle.x,
                                 y + int(21 * s) - label.get_height() // 2))
        track_x = self.middle.x + label.get_width() + int(14 * s)
        track = pygame.Rect(track_x, y + int(17 * s),
                            self.middle.right - track_x, int(8 * s))
        self.rounded(self.screen, track, over(WHITE, 0.08 if active else 0.04),
                     radius=int(4 * s))
        filled = pygame.Rect(track.x, track.y,
                             int(track.w * self.speed / 100.0), track.h)
        self.rounded(self.screen, filled,
                     ORANGE if active else over(ORANGE, 0.25),
                     radius=int(4 * s))
        self.buttons.append(Button(track.inflate(0, int(34 * s)), "speed",
                                   "speed"))

    def draw_system(self, state):
        """The troubleshooting tab: the queue, what is down, and the big levers.

        This is where the ACTIVITY strip lands when tapped. Left: the job
        queue, with the running item's own words -- "unreachable 4x, skipping
        for another 112s" was always in the API and never on a screen. Right:
        whatever is wrong (down devices) or, when nothing is, the vital signs.
        Bottom: stop, clear, retry, power -- every recovery action, one row.
        """
        s = self.k
        left = pygame.Rect(self.middle.x, self.middle.y, int(470 * s),
                           self.middle.h)
        right = pygame.Rect(int(500 * s), self.middle.y,
                            self.middle.right - int(500 * s), self.middle.h)

        # -- the queue
        jobs = state["jobs"]
        title = "Queue" if len(jobs) <= 3 else "Queue \u00b7 +%d more" % (len(jobs) - 3)
        y = self.divider(title, left.y, left.w)
        order = {"running": 0, "queued": 1}
        jobs = sorted(jobs, key=lambda j: (order.get(j.get("state"), 2),
                                           j.get("age", 0)))
        row_h, gap = int(46 * s), int(6 * s)
        shown = jobs[:3]
        for job in shown:
            row = pygame.Rect(left.x, y, left.w, row_h)
            state_name = job.get("state", "?")
            dot = {"running": CYAN, "queued": over(INK, 0.4), "done": OLIVE,
                   "superseded": over(INK, 0.3)}.get(state_name, PINK)
            self.rounded(self.screen, row,
                         over(CYAN, 0.07) if state_name == "running" else CARD,
                         over(CYAN, 0.4) if state_name == "running" else LINE_SOFT,
                         radius=int(12 * s))
            pygame.draw.circle(self.screen, dot,
                               (row.x + int(16 * s), row.centery), int(5 * s))
            label = (job.get("label") or "?").replace("scene: ", "")
            while label and self.f_body2.size(label)[0] > row.w - int(170 * s):
                label = label[:-1]
            # The running row carries the current item's own words underneath;
            # everything else centres one line.
            detail = ""
            if state_name == "running":
                for item in job.get("items") or []:
                    if item.get("status") == "working" and item.get("detail"):
                        detail = item["detail"]
                        break
                    if item.get("status") in ("failed", "skipped") and item.get("detail"):
                        detail = "%s: %s" % (item.get("name", "?"), item["detail"])
            if detail:
                self.text(self.screen, self.f_body2, label, INK,
                          topleft=(row.x + int(30 * s), row.y + int(6 * s)))
                while detail and self.f_tiny.size(detail)[0] > row.w - int(44 * s):
                    detail = detail[:-1]
                self.text(self.screen, self.f_tiny, detail, MUTED,
                          topleft=(row.x + int(30 * s), row.y + int(25 * s)))
            else:
                self.text(self.screen, self.f_body2, label, INK,
                          topleft=(row.x + int(30 * s),
                                   row.centery - self.f_body2.get_height() // 2))
            done = job.get("done", 0)
            total = job.get("total", 0)
            age = int(job.get("age", 0))
            when = "%dm" % (age // 60) if age >= 60 else "%ds" % age
            note = "%d/%d \u00b7 %s" % (done, total, when) if total else when
            failed = job.get("failed", 0)
            image = self.f_tiny.render(note, True,
                                       PINK_SOFT if failed else MUTED)
            self.screen.blit(image, (row.right - int(12 * s) - image.get_width(),
                                     row.centery - image.get_height() // 2))
            y = row.bottom + gap
        if not shown:
            self.text(self.screen, self.f_small,
                      "Nothing has run lately.", MUTED,
                      topleft=(left.x, y + int(6 * s)))

        # -- what is wrong, or the vitals when nothing is
        down = state["unreachable"]
        y = self.divider("Not answering" if down else "Vitals", right.y,
                         right.w, x=right.x)
        if down:
            for device in down[:2]:
                row = pygame.Rect(right.x, y, right.w, int(40 * s))
                self.rounded(self.screen, row, over(PINK, 0.06),
                             over(PINK, 0.35), radius=int(10 * s))
                name = device.get("name", "?").replace("_", " ")
                self.text(self.screen, self.f_body2, name, PINK_SOFT,
                          topleft=(row.x + int(12 * s), row.y + int(4 * s)))
                why = (device.get("error") or "no answer")[:34]
                self.text(self.screen, self.f_tiny, why, over(PINK_SOFT, 0.7),
                          topleft=(row.x + int(12 * s), row.y + int(22 * s)))
                y = row.bottom + int(6 * s)
            if len(down) > 2:
                self.text(self.screen, self.f_tiny, "+%d more" % (len(down) - 2),
                          FAINT, topleft=(right.x + int(4 * s), y))
                y += int(16 * s)
            test = pygame.Rect(right.x, y + int(2 * s), right.w, int(48 * s))
            self.rounded(self.screen, test, over(INK, 0.08), LINE,
                         radius=test.h // 2)
            self.text(self.screen, self.f_body, "TEST DOWN UNITS", INK,
                      center=test.center)
            self.buttons.append(Button(test, "TEST DOWN UNITS", "retry-down"))
        else:
            wanted = ("Bluetooth", "Clock", "Network", "Pi power", "Storage")
            rows = [r for name in wanted for r in state["diagnostics"]
                    if r.get("name") == name][:4] or state["diagnostics"][:4]
            for entry in rows:
                ok = entry.get("ok")
                pygame.draw.circle(self.screen, OLIVE if ok else PINK,
                                   (right.x + int(5 * s), y + int(15 * s)),
                                   int(4 * s))
                self.text(self.screen, self.f_small, entry["name"], INK,
                          topleft=(right.x + int(16 * s), y + int(6 * s)))
                value = entry.get("value", "")[:22]
                image = self.f_small.render(value, True,
                                            INK if ok else PINK_SOFT)
                self.screen.blit(image, (right.right - image.get_width(),
                                         y + int(6 * s)))
                y += int(30 * s)

        # -- the levers, pinned to the bottom of the middle
        height = int(48 * s)
        y = self.middle.bottom - height
        gap = int(10 * s)
        stop = pygame.Rect(self.middle.x, y, int(268 * s), height)
        self.rounded(self.screen, stop, over(ORANGE, 0.12), over(ORANGE, 0.5),
                     radius=height // 2)
        self.text(self.screen, self.f_body, "STOP EVERYTHING", ORANGE,
                  center=stop.center)
        self.buttons.append(Button(stop, "STOP EVERYTHING", "stop-all"))

        clear = pygame.Rect(stop.right + gap, y, int(160 * s), height)
        self.rounded(self.screen, clear, CARD, LINE, radius=height // 2)
        self.text(self.screen, self.f_body, "CLEAR QUEUE", INK,
                  center=clear.center)
        self.buttons.append(Button(clear, "CLEAR QUEUE", "clear-queue"))

        retry = pygame.Rect(clear.right + gap, y, int(160 * s), height)
        self.rounded(self.screen, retry, CARD, LINE, radius=height // 2)
        self.text(self.screen, self.f_body, "RETRY DOWN", INK,
                  center=retry.center)
        self.buttons.append(Button(retry, "RETRY DOWN", "retry-down"))

        power = pygame.Rect(retry.right + gap, y,
                            self.middle.right - retry.right - gap, height)
        self.rounded(self.screen, power, CARD, over(ORANGE, 0.45),
                     radius=height // 2)
        self.text(self.screen, self.f_body, "POWER\u2026", ORANGE,
                  center=power.center)
        self.buttons.append(Button(power, "POWER", "ask-power"))

    def draw_panel(self, state):
        """The text panel: what it is saying, and what else it could say.

        The phone composes; this exists so the sign can be talked to with the
        phone in someone's pocket. Tapping a saved message puts it up now, and
        WRITE opens a keyboard for something that is not saved yet.
        """
        s = self.k
        panel = state["panel"] or {}
        if not panel.get("configured"):
            box = pygame.Rect(self.middle.x, self.middle.y,
                              self.middle.w, int(120 * s))
            self.rounded(self.screen, box, CARD, LINE, radius=int(14 * s))
            self.text(self.screen, self.f_head2, "No text panel paired", INK,
                      topleft=(box.x + int(18 * s), box.y + int(16 * s)))
            for index, line in enumerate((
                    "Power the panel on, then pair it from the phone UI's",
                    "Message tab -- it needs a Bluetooth scan and a name,",
                    "and both are easier with a keyboard in your hand.")):
                self.text(self.screen, self.f_small, line, MUTED,
                          topleft=(box.x + int(18 * s),
                                   box.y + int(48 * s) + index * int(20 * s)))
            return

        # -- who it is, and whether we trust the encoding
        bits = [panel.get("name") or panel.get("address", ""),
                panel.get("family_label") or panel.get("family") or "unknown type"]
        size = panel.get("size") or {}
        if size:
            bits.append("%sx%s" % (size.get("width"), size.get("height")))
        self.text(self.screen, self.f_tiny, "  \u00b7  ".join(str(b) for b in bits),
                  MUTED, topleft=(self.middle.x, self.middle.y))
        caps = panel.get("capabilities") or {}
        if not caps.get("confirmed"):
            note = "encoding unconfirmed"
            image = self.f_tiny.render(note, True, ORANGE)
            self.screen.blit(image, (self.middle.right - image.get_width(),
                                     self.middle.y))
        y = self.middle.y + int(22 * s)

        # -- what is on the panel right now
        current = panel.get("current") or {}
        card = pygame.Rect(self.middle.x, y, self.middle.w, int(54 * s))
        colour = self._lit(current.get("color"), (90, 84, 78))
        self.rounded(self.screen, card, over(colour, 0.10), over(colour, 0.45),
                     radius=int(14 * s))
        if current.get("text"):
            # Glowing, like the sign preview above it: the panel is part of the
            # sign, and reading it here should feel like reading it out there.
            label = current["text"]
            while label and self.f_head2.size(label)[0] > card.w - int(150 * s):
                label = label[:-1]
            spot = self.f_head2.render(label, True, colour).get_rect(
                topleft=(card.x + int(16 * s),
                         card.centery - self.f_head2.get_height() // 2))
            self.glow_text(self.f_head2, label, colour, spot)
        else:
            self.text(self.screen, self.f_head2, "Panel is blank", DIM,
                      topleft=(card.x + int(16 * s),
                               card.centery - self.f_head2.get_height() // 2))
        right = []
        if panel.get("playlist"):
            right.append("cycling")
            if panel.get("next_in") is not None:
                right.append("next in %ds" % panel["next_in"])
        elif current.get("text"):
            right.append("holding")
        # A message too wide for the panel is shown a page at a time. Without
        # this the sign looks like it is repainting the same message over and
        # over for no reason.
        if panel.get("pages", 0) > 1:
            right.append("page %d/%d" % (panel.get("page") or 1, panel["pages"]))
        if panel.get("last_error"):
            right = [panel["last_error"][:40]]
        if right:
            image = self.f_tiny.render("  \u00b7  ".join(right), True,
                                       PINK_SOFT if panel.get("last_error") else MUTED)
            self.screen.blit(image, (card.right - int(16 * s) - image.get_width(),
                                     card.centery - image.get_height() // 2))
        y = card.bottom + int(10 * s)

        # -- the queue, as chips you can put up with one tap
        messages = state["messages"]
        chip_h = int(44 * s)
        gap = int(8 * s)
        per_row = 4
        chip_w = (self.middle.w - gap * (per_row - 1)) // per_row
        showing = current.get("id")
        if not messages:
            self.text(self.screen, self.f_small,
                      "Nothing queued yet -- tap WRITE, or add some from the phone.",
                      MUTED, topleft=(self.middle.x, y + int(6 * s)))
        for index, message in enumerate(messages[:per_row * 2]):
            col, row = index % per_row, index // per_row
            chip = pygame.Rect(self.middle.x + col * (chip_w + gap),
                               y + row * (chip_h + gap), chip_w, chip_h)
            live = message["id"] == showing
            tint = self._lit(message.get("color"), INK)
            self.rounded(self.screen, chip,
                         over(tint, 0.22 if live else 0.08),
                         over(tint, 0.8 if live else 0.28),
                         radius=int(10 * s))
            label = message.get("text", "")
            while label and self.f_small.size(label)[0] > chip.w - int(20 * s):
                label = label[:-1]
            self.text(self.screen, self.f_small, label,
                      INK if message.get("enabled", True) else DIM,
                      center=chip.center)
            self.buttons.append(Button(chip, message.get("text", ""),
                                       "msg-show", message["id"]))
        y += (chip_h + gap) * min(2, max(1, (len(messages) + per_row - 1) // per_row))

        # -- the controls
        height = int(48 * s)
        y = max(y, self.middle.bottom - height)
        wide = int(140 * s)
        write = pygame.Rect(self.middle.x, y, wide, height)
        self.rounded(self.screen, write, over(CYAN, 0.14), CYAN, radius=height // 2)
        self.text(self.screen, self.f_small, "WRITE", CYAN, center=write.center)
        self.buttons.append(Button(write, "WRITE", "compose"))

        cycling = panel.get("playlist")
        cycle = pygame.Rect(write.right + gap, y, wide, height)
        self.rounded(self.screen, cycle,
                     over(OLIVE, 0.16) if cycling else CARD,
                     OLIVE if cycling else LINE, radius=height // 2)
        self.text(self.screen, self.f_small, "STOP CYCLE" if cycling else "CYCLE",
                  OLIVE if cycling else INK, center=cycle.center)
        self.buttons.append(Button(cycle, "CYCLE", "panel-cycle"))

        nxt = pygame.Rect(cycle.right + gap, y, wide, height)
        self.rounded(self.screen, nxt, CARD, LINE, radius=height // 2)
        self.text(self.screen, self.f_small, "NEXT", INK, center=nxt.center)
        self.buttons.append(Button(nxt, "NEXT", "panel-next"))

        blank = pygame.Rect(nxt.right + gap, y, wide, height)
        self.rounded(self.screen, blank, CARD, over(PINK, 0.4), radius=height // 2)
        self.text(self.screen, self.f_small, "BLANK", PINK_SOFT, center=blank.center)
        self.buttons.append(Button(blank, "BLANK", "panel-blank"))

    def draw_compose(self):
        """Type a message on the panel itself.

        Covers the tab body and the bottom bar, because a keyboard needs the
        room and because nothing else should be tappable while one is up. Upper
        case only -- see KEY_ROWS.
        """
        s = self.k
        gap = int(5 * s)
        # Over the tab row as well as the tab body. Nothing outside the
        # keyboard answers a tap while it is up -- the handler says so -- so
        # the row was sixty-four pixels of screen a keyboard could be using,
        # and it is the one part of this UI where key size is the whole
        # experience.
        top = self.controlrow.y + int(4 * s)
        box = pygame.Rect(self.middle.x, top, self.middle.w,
                          self.h - int(14 * s) - top)
        self.rounded(self.screen, box, CARD_ALT, over(CYAN, 0.3), radius=int(16 * s))

        text = self.compose["text"]
        field = pygame.Rect(box.x + int(12 * s), box.y + int(10 * s),
                            box.w - int(24 * s), int(36 * s))
        self.rounded(self.screen, field, BG, LINE, radius=int(10 * s))
        shown = text or "type a message"
        self.text(self.screen, self.f_head2, shown[-34:],
                  INK if text else DIM,
                  topleft=(field.x + int(12 * s),
                           field.centery - self.f_head2.get_height() // 2))
        count = self.f_tiny.render("%d/%d" % (len(text), MAX_COMPOSE), True, MUTED)
        self.screen.blit(count, (field.right - int(12 * s) - count.get_width(),
                                 field.centery - count.get_height() // 2))

        # Fill the box rather than taking a fixed 38 pixels and leaving the
        # rest empty: on the 4.3-inch panel every pixel of key height is worth
        # having, and deriving it means another screen size gets it too.
        top = field.bottom + int(8 * s)
        action_h = int(42 * s)
        rows = len(KEY_ROWS)
        room = box.bottom - int(10 * s) - top - action_h - gap
        key_h = max(int(30 * s), (room - gap * (rows - 1)) // rows)
        columns = max(len(row) for row in KEY_ROWS)
        key_w = (box.w - int(24 * s) - gap * (columns - 1)) // columns
        for r, row in enumerate(KEY_ROWS):
            y = top + r * (key_h + gap)
            for c, char in enumerate(row):
                key = pygame.Rect(box.x + int(12 * s) + c * (key_w + gap), y,
                                  key_w, key_h)
                back = char == "\u232b"
                self.rounded(self.screen, key, CARD,
                             over(PINK, 0.35) if back else LINE_SOFT,
                             radius=int(8 * s))
                self.text(self.screen, self.f_small, char,
                          PINK_SOFT if back else INK, center=key.center)
                self.buttons.append(
                    Button(key, char, "key-back" if back else "key", char))

        y = top + len(KEY_ROWS) * (key_h + gap)
        action_h = min(action_h, box.bottom - int(10 * s) - y)
        space = pygame.Rect(box.x + int(12 * s), y, int(250 * s), action_h)
        self.rounded(self.screen, space, CARD, LINE_SOFT, radius=int(10 * s))
        self.text(self.screen, self.f_small, "SPACE", MUTED, center=space.center)
        self.buttons.append(Button(space, " ", "key", " "))

        wide = (box.right - int(12 * s) - space.right - gap * 3) // 3
        cancel = pygame.Rect(space.right + gap, y, wide, action_h)
        self.rounded(self.screen, cancel, CARD, LINE, radius=action_h // 2)
        self.text(self.screen, self.f_small, "CANCEL", MUTED, center=cancel.center)
        self.buttons.append(Button(cancel, "CANCEL", "compose-cancel"))

        queue = pygame.Rect(cancel.right + gap, y, wide, action_h)
        self.rounded(self.screen, queue, CARD, LINE_SOFT, radius=action_h // 2)
        self.text(self.screen, self.f_small, "QUEUE", INK, center=queue.center)
        self.buttons.append(Button(queue, "QUEUE", "compose-queue"))

        send = pygame.Rect(queue.right + gap, y, wide, action_h)
        self.rounded(self.screen, send, over(CYAN, 0.18), CYAN, radius=action_h // 2)
        self.text(self.screen, self.f_small, "SEND", CYAN, center=send.center)
        self.buttons.append(Button(send, "SEND", "compose-send"))

    def draw_confirm(self):
        """A full-width prompt over the middle third.

        It names the consequence rather than asking "are you sure": after a
        shutdown the sign is dark and this panel is dead, and the only way back
        is unplugging the Pi and plugging it in again.

        ``options`` is a list of {label, action, danger?}; CANCEL is added
        here so no caller can forget it. Actions dispatch through the same
        act()/system() rails as any button, on their worker threads.
        """
        s = self.k
        ask = self.confirm
        panel = pygame.Rect(self.middle.x, self.middle.y - int(4 * s),
                            self.middle.w, self.middle.h)
        self.rounded(self.screen, panel, CARD_ALT, over(PINK, 0.35),
                     radius=int(16 * s))
        self.text(self.screen, self.f_head2, ask["title"], INK,
                  topleft=(panel.x + int(18 * s), panel.y + int(16 * s)))
        y = panel.y + int(46 * s)
        for line in ask["body"]:
            self.text(self.screen, self.f_small, line, MUTED,
                      topleft=(panel.x + int(18 * s), y))
            y += int(20 * s)

        height = int(48 * s)
        options = ask["options"]
        wide = min(int(200 * s),
                   (panel.w - int(36 * s) - int(10 * s) * len(options))
                   // (len(options) + 1))
        x = panel.right - int(18 * s) - wide
        row_y = panel.bottom - int(18 * s) - height
        for option in reversed(options):
            spot = pygame.Rect(x, row_y, wide, height)
            danger = option.get("danger")
            self.rounded(self.screen, spot,
                         over(PINK, 0.16) if danger else over(CYAN, 0.12),
                         PINK if danger else over(CYAN, 0.6),
                         radius=height // 2)
            self.text(self.screen, self.f_body, option["label"],
                      PINK_SOFT if danger else CYAN_SOFT, center=spot.center)
            self.buttons.append(Button(spot, option["label"], "confirm-opt",
                                       option["action"]))
            x -= wide + int(10 * s)
        cancel = pygame.Rect(x, row_y, wide, height)
        self.rounded(self.screen, cancel, CARD, LINE, radius=height // 2)
        self.text(self.screen, self.f_body, "CANCEL", INK, center=cancel.center)
        self.buttons.append(Button(cancel, "CANCEL", "confirm-no"))

    def target_options(self, state):
        """The named groups, one finger-sized row's worth.

        The single-letter groups and the per-side pair are deliberately not
        here: the preview letters above ARE the per-letter picker, and
        per-side targeting stays on the phone. That is what lets this be one
        48-pixel row instead of two cramped ones.
        """
        preferred = ["letters", "drink", "cup", "straw", "border"]
        groups = list(state["groups"])
        ordered = [g for g in preferred if g in groups]
        ordered += [g for g in groups
                    if g not in ordered and len(g) > 1
                    and not g.startswith("side-")]
        options = [("all", "Everything")]
        options += [("group:" + g, GROUP_LABELS.get(g, g.replace("-", " ").title()))
                    for g in ordered]
        return options

    @staticmethod
    def pattern_choices(state):
        """Four named patterns rather than the whole mode table, per 2b.

        Matched by what the audit called them, so a renamed mode still lands in
        the right slot and a missing one simply does not appear.
        """
        out = []
        for needle, label in PATTERN_LABELS:
            for mode in state["patterns"]:
                if needle in mode["name"].lower():
                    out.append({"label": label, "value": mode["value"]})
                    break
        return out


    # -- drawing helpers ------------------------------------------------------

    def text(self, surface, font, message, color, center=None, topleft=None):
        image = font.render(message, True, color)
        rect = image.get_rect()
        if center:
            rect.center = center
        else:
            rect.topleft = topleft
        surface.blit(image, rect)
        return rect

    def text_right(self, font, message, colour, right, top):
        image = font.render(message, True, colour)
        self.screen.blit(image, (right - image.get_width(), top))

    def rounded(self, surface, rect, fill, border=None, radius=10, width=1,
                dashed=False):
        if fill:
            pygame.draw.rect(surface, fill, rect, border_radius=radius)
        if not border:
            return
        if not dashed:
            pygame.draw.rect(surface, border, rect, width=width, border_radius=radius)
            return
        # A dashed outline, for a zone that is not answering: the design marks
        # those by shape as well as colour so it reads without relying on hue.
        for x in range(rect.x + radius, rect.right - radius, 10):
            pygame.draw.line(surface, border, (x, rect.y), (min(x + 5, rect.right), rect.y))
            pygame.draw.line(surface, border, (x, rect.bottom - 1),
                             (min(x + 5, rect.right), rect.bottom - 1))
        for y in range(rect.y + radius, rect.bottom - radius, 10):
            pygame.draw.line(surface, border, (rect.x, y), (rect.x, min(y + 5, rect.bottom)))
            pygame.draw.line(surface, border, (rect.right - 1, y),
                             (rect.right - 1, min(y + 5, rect.bottom)))

    def draw_toast(self, message):
        if not message:
            return
        s = self.k
        image = self.f_small.render(message, True, INK)
        box = image.get_rect()
        box.inflate_ip(int(28 * s), int(18 * s))
        box.centerx = self.w // 2
        box.bottom = self.tabrow.y - int(8 * s)
        self.rounded(self.screen, box, CARD_ALT, LINE_SOFT, radius=int(10 * s))
        self.screen.blit(image, image.get_rect(center=box.center))

    # -- input ----------------------------------------------------------------

    # A 4.3-inch panel at 800x480 puts about 8.6 pixels in a millimetre, and a
    # fingertip covers eight to ten of those millimetres. Measured against
    # that, every control on this screen was under 9mm across its short side
    # and the tab pills were 3mm -- which is why they were hard to hit. The
    # tabs are now 72 pixels of bottom row, about 8.4mm, so they no longer
    # depend on any of this; the swatches and chips still do.
    #
    # Redrawing everything finger-sized would not fit: the middle of the layout
    # is only ~230 pixels tall once the sign band, the tab row and the bottom
    # bar have taken theirs. So the hit area is separated from the picture. A
    # near miss lands on the nearest control instead of on nothing, which makes
    # a 26-pixel pill behave like a 62-pixel one without looking like one.
    #
    # Nearest-centre rather than padded rectangles on purpose: padding two
    # neighbours until they overlap makes the winner depend on which was drawn
    # first, and a tap between two swatches would pick the left one every time
    # rather than the one it was closer to.
    TOUCH_SLOP = 18          # pixels at 800x480, about 2.1mm on this panel

    # Controls a near miss must never land on. STOP throws away the queue; a
    # finger aimed at the progress readout beside it has to miss harmlessly.
    NEVER_SLOP = ("stop-all",)

    @staticmethod
    def _gap(rect, position) -> float:
        """How far a point is from a rectangle. Zero when it is inside."""
        x, y = position
        dx = max(rect.left - x, 0, x - rect.right)
        dy = max(rect.top - y, 0, y - rect.bottom)
        return (dx * dx + dy * dy) ** 0.5

    def _hit(self, position, buttons, kinds=None):
        """The control the finger meant, or None if it meant nothing.

        A direct hit always wins, so nothing changes for a confident tap.
        """
        candidates = [b for b in buttons
                      if kinds is None or b.kind in kinds]
        for button in candidates:
            if button.rect.collidepoint(position):
                return button
        slop = self.TOUCH_SLOP * self.k
        best, best_gap = None, None
        for button in candidates:
            if button.kind in self.NEVER_SLOP:
                continue              # direct hits only, handled above
            gap = self._gap(button.rect, position)
            if gap <= slop and (best_gap is None or gap < best_gap):
                best, best_gap = button, gap
        return best

    def relock(self):
        """Lock, and drop anything half-finished on the way.

        A half-typed message or an open prompt left behind a lock is a trap:
        the next person unlocks into someone else\'s unfinished business and
        the first tap lands somewhere they never chose.
        """
        self.locked = True
        self.compose = None
        self.confirm = None

    def tap(self, position):
        if self.locked:
            # Nothing that writes to the sign answers while locked. Two things
            # still do: the unlock bar, and the tabs -- looking is not
            # changing, and a locked screen you cannot read is useless.
            button = self._hit(position, self.actions, kinds=("unlock",))
            if button is not None:
                self.locked = False
                self.touched = time.monotonic()
                self.sign.say("Unlocked \u00b7 locks itself again in %ds"
                              % int(LOCK_AFTER))
                return
            button = self._hit(position, self.tabs)
            if button is not None:
                self.tab = button.payload
                return
            # Say why, rather than letting a dead tap read as a dead panel.
            self.sign.say("Locked \u2014 touch UNLOCK first")
            return
        self.touched = time.monotonic()
        if self.confirm:
            # While a prompt is up nothing else is live, so a stray finger on
            # the tab row cannot dismiss it by navigating away.
            button = self._hit(position, self.buttons,
                               kinds=("confirm-opt", "confirm-no"))
            if button is not None:
                self.confirm = None
                if button.kind == "confirm-opt":
                    action = button.payload
                    if action in ("reboot", "shutdown"):
                        self.system(action)
                    else:
                        self.act(action)
            return
        if self.compose is not None:
            # Same reasoning as the confirm prompt above: while the keyboard is
            # up it is the only thing that answers, so a stray finger on the
            # tab row cannot navigate away mid-word.
            button = self._hit(position, self.buttons)
            if button is not None:
                if button.kind == "key":
                    if len(self.compose["text"]) < MAX_COMPOSE:
                        self.compose["text"] += button.payload
                elif button.kind == "key-back":
                    self.compose["text"] = self.compose["text"][:-1]
                elif button.kind == "compose-cancel":
                    self.compose = None
                elif button.kind in ("compose-send", "compose-queue"):
                    text = self.compose["text"].strip()
                    if not text:
                        self.sign.say("Type something first")
                    else:
                        self.compose = None
                        self.send_message(text, queue=button.kind == "compose-queue")
                return
            return
        button = self._hit(position, self.tabs)
        if button is not None:
            self.tab = button.payload
            return
        button = self._hit(position, self.actions)
        if button is not None:
            self.act(button.kind)
            return
        button = self._hit(position, self.buttons)
        if button is not None:
            kind = button.kind
            if kind == "scene":
                self.apply_scene(button.payload)
            elif kind == "zone":
                token = button.payload
                if token in self.zones:
                    self.zones.remove(token)
                else:
                    self.zones.append(token)
                # A shape pick and a chip are two views of one choice.
                self.target, self.target_name = "all", "Everything"
            elif kind == "target":
                self.zones = []
                self.target, self.target_name = button.payload
            elif kind == "compose":
                self.compose = {"text": ""}
            elif kind == "msg-show":
                self.show_message(button.payload, button.label)
            elif kind in ("panel-cycle", "panel-next", "panel-blank"):
                self.act(kind)
            elif kind == "device":
                device = button.payload
                self.zones = []
                self.target = "device:" + device["address"]
                self.target_name = device["name"].replace("_", " ")
                self.tab = "colour"
                self.sign.say("Now colouring %s only" % self.target_name)
            elif kind == "swatch":
                self.chosen_colour, self.chosen_pattern = button.payload, None
                self.apply_state({"color": button.payload, "brightness": 100,
                                  "power": True},
                                 "%s on %s" % (button.label, self.where_name()))
            elif kind == "pattern":
                self.chosen_pattern, self.chosen_colour = button.payload, None
                self.apply_state({"mode": button.payload, "speed": self.speed,
                                  "power": True},
                                 "%s on %s" % (button.label, self.where_name()))
            elif kind == "roll":
                # A cycling control has to say where it is, so the label
                # carries the value rather than only the state.
                nxt = (ROLL_STEPS.index(self.roll) + 1) % len(ROLL_STEPS) \
                    if self.roll in ROLL_STEPS else 1
                self.roll = ROLL_STEPS[nxt]
                self.sign.say("Roll off" if not self.roll else
                              "Rolling %.1fs between lights" % self.roll)
            elif kind == "speed":
                fraction = (position[0] - button.rect.x) / float(max(1, button.rect.w))
                self.speed = max(10, min(100, int(round(fraction * 100))))
                self.sign.say("Speed %d" % self.speed)
                if self.chosen_pattern:
                    self.apply_state({"mode": self.chosen_pattern,
                                      "speed": self.speed, "power": True},
                                     "Speed %d" % self.speed)
            elif kind == "shelf":
                name, total = button.payload
                page = 5
                self.shelf[name] = 0 if self.shelf[name] + page >= total \
                    else self.shelf[name] + page
            elif kind == "retry":
                self.retry(button.payload)
            elif kind == "stop-all":
                self.stop_everything()
            elif kind == "retry-down":
                down = self.sign.snapshot()["unreachable"]
                if down:
                    self.retry([{"name": d["name"], "address": d["address"]}
                                for d in down])
                else:
                    self.sign.say("Nothing is down")
            elif kind == "ask-power":
                self.confirm = {
                    "title": "Reboot or shut down?",
                    "body": ["Reboot: the lights hold what they are showing and",
                             "the sign comes back on its own in about a minute.",
                             "",
                             "Shut down: this panel goes dark and does not come",
                             "back until the Pi is unplugged and plugged in.",
                             "Do it this way before pulling power -- a mid-write",
                             "power cut is how SD cards corrupt."],
                    "options": [{"label": "REBOOT", "action": "reboot"},
                                {"label": "SHUT DOWN", "action": "shutdown",
                                 "danger": True}]}
            elif kind == "battery":
                snap = self.sign.snapshot()
                if (snap.get("battery") or {}).get("tripped_at"):
                    self.confirm = {
                        "title": "The battery guard cut the lights",
                        "body": ["The runtime budget ran out, so everything was",
                                 "switched off -- rotation too, or it would have",
                                 "relit the sign minutes later.",
                                 "",
                                 "Re-arm starts a fresh budget. With rotation, the",
                                 "sign also starts changing scenes again."],
                        "options": [
                            {"label": "RE-ARM", "action": "rearm"},
                            {"label": "+ ROTATE", "action": "rearm-rotate"}]}
                else:
                    self.tab = "system"
            elif kind == "activity":
                self.tab = "system"
            elif kind == "clear-queue":
                self.act("clear-queue")
            return

    def system(self, action):
        self.sign.say("Rebooting\u2026" if action == "reboot"
                      else "Shutting down\u2026 wait for the green light to stop")
        threading.Thread(target=self._system, args=(action,), daemon=True).start()

    @staticmethod
    def _system(action):
        try:
            _post("/api/system", {"action": action}, timeout=10.0)
        except Exception:
            pass

    def stop_everything(self):
        """Drop the queue and cancel the write in flight.

        On a thread, like every other request from this screen: the UI thread
        making a call that can block for seconds is how a touch panel comes to
        feel broken -- and this is the button people press *because* something
        is already stuck.
        """
        self.sign.say("Stopping\u2026")
        threading.Thread(target=self._stop_everything, daemon=True).start()

    def _stop_everything(self):
        """Says what will start something again, because otherwise a scene that
        rotation puts straight back looks like the button doing nothing.
        """
        try:
            reply = _post("/api/stop", {})
        except Exception:
            self.sign.say("Could not reach the sign")
            return
        said = reply.get("stopped") or ("cleared %d queued" % reply["cleared"]
                                        if reply.get("cleared") else "")
        said = ("Stopped " + said) if said else "Nothing was running"
        if reply.get("rotation"):
            said += " -- rotation is still on"
        elif reply.get("playlist"):
            said += " -- the message cycle is still on"
        self.sign.say(said, seconds=4.0)

    def retry(self, devices):
        """Re-send to whatever is silent, rather than waiting for the next sweep."""
        self.sign.say("Retrying " + ", ".join(
            d["name"].replace("_", " ") for d in devices))
        for device in devices:
            threading.Thread(target=self._retry_one, args=(device,),
                             daemon=True).start()

    @staticmethod
    def _retry_one(device):
        try:
            _post("/api/devices/%s/test" % device["address"], {})
        except Exception:
            pass

    def selection(self):
        """What a colour or pattern would land on, and what to call it."""
        if not self.zones:
            return {"target": self.target}, self.target_name
        if len(self.zones) == 1:
            name = self.zone_name(self.zones[0])
        else:
            name = "%d zones" % len(self.zones)
        # targets, not target: the API resolves the union into a single job.
        return {"targets": list(self.zones)}, name

    def where_name(self):
        return self.selection()[1].lower()

    def zone_name(self, token):
        if token.startswith("group:"):
            return token.split(":", 1)[1]
        if token.startswith("device:"):
            for device in self.sign.snapshot()["devices"]:
                if device["address"] == token.split(":", 1)[1]:
                    return device["name"].replace("_", " ")
        return token.split(":", 1)[-1].replace("_", " ")

    def apply_state(self, changes, said):
        where, _name = self.selection()
        payload = dict(changes, **where)
        if self.roll:
            payload["stagger"] = self.roll
        threading.Thread(target=self._apply_state, args=(payload, said),
                         daemon=True).start()

    def _apply_state(self, payload, said):
        try:
            result = _post("/api/apply", payload)
            self.sign.say(said if result.get("ok") else
                          (result.get("error") or "could not apply"))
        except Exception:
            self.sign.say("no answer from the sign")

    def apply_scene(self, name):
        self.sign.mark_pending(name)
        threading.Thread(target=self._apply_scene, args=(name,), daemon=True).start()

    def _apply_scene(self, name):
        try:
            body = {"scene": name}
            if self.roll:
                body["stagger"] = self.roll
            result = _post("/api/scene/apply", body)
            if not result.get("ok"):
                self.sign.clear_pending()
                self.sign.say(result.get("error") or "could not apply")
        except Exception:
            self.sign.clear_pending()
            self.sign.say("no answer from the sign")

    def send_message(self, text, queue=False):
        """Put a typed message up now, or save it to the queue."""
        threading.Thread(target=self._send_message, args=(text, queue),
                         daemon=True).start()

    def _send_message(self, text, queue):
        # No mode: the server fills in what the panel can actually do, rather
        # than this screen naming an effect the panel will not perform.
        body = {"text": text, "color": "#ff2f6e"}
        try:
            if queue:
                result = _post("/api/matrix/messages", body)
                self.sign.say(("Queued \u201c%s\u201d" % text) if result.get("ok")
                              else (result.get("error") or "could not queue it"))
            else:
                result = _post("/api/matrix/send", body)
                self.sign.say(("Panel: " + text) if result.get("ok")
                              else (result.get("error") or "the panel did not take it"))
            self.sign.refresh_panel()
        except Exception:
            self.sign.say("no answer from the sign")

    def show_message(self, message_id, label):
        threading.Thread(target=self._show_message, args=(message_id, label),
                         daemon=True).start()

    def _show_message(self, message_id, label):
        try:
            result = _post("/api/matrix/messages/%s/send" % message_id, {})
            self.sign.say(("Panel: " + label) if result.get("ok")
                          else (result.get("error") or "the panel did not take it"))
            self.sign.refresh_panel()
        except Exception:
            self.sign.say("no answer from the sign")

    def act(self, kind):
        threading.Thread(target=self._act, args=(kind,), daemon=True).start()

    def _act(self, kind):
        try:
            if kind == "off":
                self.sign.clear_pending()
                where, name = self.selection()
                _post("/api/power", dict(where, on=False))
                self.sign.say("Turning off " + name.lower())
            elif kind == "panel-cycle":
                with self.sign.lock:
                    cycling = bool((self.sign.panel or {}).get("playlist"))
                _post("/api/matrix", {"playlist": not cycling})
                if cycling:
                    self.sign.say("Panel stopped cycling")
                else:
                    _post("/api/matrix/next", {})
                    self.sign.say("Panel cycling the queue")
                self.sign.refresh_panel()
            elif kind == "panel-next":
                result = _post("/api/matrix/next", {})
                self.sign.say(("Panel: " + result["message"]["text"])
                              if result.get("ok")
                              else (result.get("error") or "nothing queued"))
            elif kind == "panel-blank":
                result = _post("/api/matrix/clear", {})
                # Blanking also stops the cycle server-side, or the next tick
                # would refill the panel a few seconds later.
                self.sign.say("Panel blanked" if result.get("ok")
                              else (result.get("error") or "could not blank it"))
                self.sign.refresh_panel()
            elif kind == "next":
                result = _post("/api/rotation/next", {})
                self.sign.say(("Now playing " + result["scene"]) if result.get("ok")
                              else (result.get("error") or "nothing to play"))
            elif kind == "rearm":
                _post("/api/battery/rearm", {})
                self.sign.say("Battery budget restarted")
            elif kind == "rearm-rotate":
                _post("/api/battery/rearm", {})
                _post("/api/rotation", {"enabled": True})
                self.sign.say("Re-armed \u00b7 rotation back on")
            elif kind == "clear-queue":
                result = _post("/api/queue/clear", {})
                cleared = result.get("cleared", 0)
                self.sign.say("Cleared %d queued job%s"
                              % (cleared, "" if cleared == 1 else "s")
                              if result.get("ok") else "could not clear it")
            else:
                with self.sign.lock:
                    want = not bool(self.sign.rotation.get("enabled"))
                _post("/api/rotation", {"enabled": want})
                self.sign.say("Rotation on" if want else "Rotation off")
        except Exception:
            self.sign.say("no answer from the sign")

    def present(self):
        if self.fb:
            self.fb.blit(self.screen)
        else:
            pygame.display.flip()

    def gestures(self):
        """Pointer events, from the touchscreen or from SDL.

        Normalised to (kind, x, y) so the main loop does not care which backend
        is running -- the panel behaves the same on a desktop as on the sign.
        """
        if self.touch is not None:
            events = self.touch.poll()
            # Without a display SDL delivers nothing, so a quit has to come
            # from elsewhere; systemd stops this service, and Ctrl-C raises.
            return events
        out = []
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                out.append(("quit", 0, 0))
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE,
                                                                pygame.K_q):
                out.append(("quit", 0, 0))
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                out.append(("down",) + event.pos)
            elif event.type == pygame.MOUSEMOTION and event.buttons[0]:
                out.append(("move",) + event.pos)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                out.append(("up",) + event.pos)
            # Touchscreens report fingers, and SDL only turns those into mouse
            # events when its touch-mouse hint is on -- which it is not under
            # KMSDRM. Handling both is why the panel draws and ignores taps.
            # Finger coordinates are fractions of the window, not pixels.
            elif event.type == pygame.FINGERDOWN:
                out.append(("down", int(event.x * self.w), int(event.y * self.h)))
            elif event.type == pygame.FINGERMOTION:
                out.append(("move", int(event.x * self.w), int(event.y * self.h)))
            elif event.type == pygame.FINGERUP:
                out.append(("up", int(event.x * self.w), int(event.y * self.h)))
        return out

    # -- main loop ------------------------------------------------------------

    def run(self, frames=None, on_frame=None):
        self.sign.start()
        self.measure()
        clock = pygame.time.Clock()
        drawn, running = 0, True
        while running:
            for kind, x, y in self.gestures():
                if kind == "quit":
                    running = False
                elif kind == "down":
                    self.drag_from, self.last_point = (x, y), (x, y)
                    self.dragging = False
                elif kind == "move" and self.drag_from:
                    if abs(x - self.drag_from[0]) > 24:
                        self.dragging = True      # the shelf is swiped sideways
                    self.last_point = (x, y)
                elif kind == "up":
                    if self.drag_from and self.dragging:
                        self.swipe(self.last_point[0] - self.drag_from[0],
                                   at_y=self.drag_from[1])
                    elif self.drag_from:
                        self.tap((x, y))
                    self.drag_from, self.dragging = None, False

            # Relock on a timer rather than only on a tap: the risk this
            # guards against is the panel nobody is standing at.
            if not self.locked and time.monotonic() - self.touched > LOCK_AFTER:
                self.relock()

            state = self.sign.snapshot()
            self.buttons = []
            self.screen.fill(BG)
            if state["online"] and (state["scenes"] or state["devices"]):
                self.draw_preview(state)
                # Not while the keyboard is up. The compose box covers this
                # row, and a control drawn under an overlay is still live:
                # with a sweep running, STOP sat exactly under the text field,
                # so tapping where you were typing threw away the queue.
                if self.compose is None:
                    self.draw_control(state)
                else:
                    self.actions = []
                if self.confirm:
                    self.draw_confirm()
                elif self.compose is not None:
                    self.draw_compose()
                elif self.tab == "scenes":
                    self.draw_scenes(state)
                elif self.tab == "colour":
                    self.draw_colour(state)
                elif self.tab == "panel":
                    self.draw_panel(state)
                else:
                    self.draw_system(state)
                # The keyboard covers the tab row; drawing it underneath
                # would put live buttons behind an overlay.
                if self.compose is None:
                    self.draw_tabs(state)
            else:
                self.text(self.screen, self.f_head2,
                          "Waiting for the sign\u2026" if state["online"]
                          else "Cannot reach the sign", MUTED,
                          center=(self.w // 2, self.h // 2))
            self.draw_toast(state["toast"])
            self.present()

            drawn += 1
            if on_frame:
                on_frame(self, drawn)
            if frames and drawn >= frames:
                running = False
            clock.tick(FPS)

        self.sign.stop()
        if self.touch:
            self.touch.close()
        if self.fb:
            self.fb.close()
        pygame.quit()

    def swipe(self, dx, at_y=None):
        """Page a shelf. Only Scenes has them; the other tabs ignore this.

        Pages the shelf the drag started over, not always the animated one --
        swiping the solid row used to page the row above it, which reads as a
        broken gesture rather than a wrong target.
        """
        if self.tab != "scenes":
            return
        if not self.locked:
            self.touched = time.monotonic()
        boundary = self.middle.y + int(136 * self.k)   # divider+cards end
        which = "solid" if (at_y is not None and at_y > boundary) else "scenes"
        page = 5
        with self.sign.lock:
            total = sum(1 for s in self.sign.scenes
                        if s["animated"] == (which == "scenes"))
        if dx < 0:
            self.shelf[which] = 0 if self.shelf[which] + page >= total \
                else self.shelf[which] + page
        else:
            self.shelf[which] = max(0, self.shelf[which] - page)

def main():
    windowed = "--windowed" in sys.argv
    size, backend = None, None
    for argument in sys.argv[1:]:
        if argument.startswith("--size="):
            width, _, height = argument[7:].partition("x")
            size = (int(width), int(height))
        elif argument.startswith("--backend="):
            backend = argument.split("=", 1)[1]
    Panel(size=size, fullscreen=not windowed, backend=backend).run()


if __name__ == "__main__":
    main()
