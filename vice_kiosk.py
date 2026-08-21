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

# Same palette as the web UI, so the panel and a phone look like one product.
BG      = (11, 13, 18)
PANEL   = (22, 26, 35)
PANEL2  = (30, 36, 49)
LINE    = (43, 51, 66)
INK     = (232, 236, 244)
MUTED   = (139, 150, 173)
ACCENT  = (255, 45, 120)
ACCENT2 = (34, 211, 238)
OK      = (37, 192, 106)
WARN    = (245, 165, 36)
BAD     = (239, 68, 68)
OFF_BG  = (42, 21, 32)
OFF_INK = (255, 180, 198)

TABS = (("scenes", "SCENES"), ("colour", "COLOUR"), ("lights", "LIGHTS"))

# One tap each. Deliberately short: a wall of swatches is harder to use in the
# dark than a dozen that are clearly different from each other.
SWATCHES = (
    ("#ff2d78", "Vice"),   ("#ff0033", "Red"),    ("#ff6a00", "Orange"),
    ("#ffb400", "Amber"),  ("#ffd9a0", "Warm"),   ("#ffffff", "White"),
    ("#22d3ee", "Cyan"),   ("#0066ff", "Blue"),   ("#8000ff", "Violet"),
    ("#ff00ff", "Magenta"),("#00ff66", "Green"),  ("#00ffcc", "Mint"),
)

# Group names that read better than the config's own keys.
GROUP_LABELS = {
    "letters": "All letters", "drink": "Whole drink", "cup": "Cups",
    "straw": "Straws", "side-a": "Side A", "side-b": "Side B",
    "border": "Border",
}
PATTERN_SPEED = 70


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
        self.queued = 0
        self.job = None
        self.down = 0
        self.total = 0
        self.online = False
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

    def snapshot(self):
        with self.lock:
            return {
                "scenes": list(self.scenes), "playing": self.playing,
                "rotation": dict(self.rotation), "busy": self.busy,
                "queued": self.queued, "job": self.job,
                "done": self.down, "total": self.total, "online": self.online,
                "pending": self.pending,
                "devices_total": self.devices_total, "devices_bad": self.devices_bad,
                "groups": list(self.groups), "devices": list(self.devices),
                "patterns": list(self.patterns),
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
        scenes = []
        for scene in state.get("scenes", []):
            steps = scene.get("steps") or []
            animated = any(s.get("mode") and s["mode"] in moving for s in steps)
            scenes.append({"name": scene.get("name", "?"), "animated": animated})
        # Movement first, matching how the web UI groups them.
        scenes.sort(key=lambda s: (not s["animated"],))

        # Patterns worth a button: the ones that move through several colours.
        # A single-colour fade needs speed 85+ to be visible at all and reads
        # as a solid colour otherwise, which is a poor thing to offer as a
        # one-tap choice.
        patterns = [m for m in modes if m.get("animates")
                    and ("7 colour" in m["name"].lower() or "rgb" in m["name"].lower())]

        devices = [{"name": d.get("name", "?"), "address": d.get("address", ""),
                    "groups": d.get("groups", []), "reachable": d.get("reachable")}
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
        job, done, total = None, 0, 0
        for candidate in status.get("jobs") or []:
            if candidate.get("state") in ("running", "queued"):
                job = candidate
                items = candidate.get("items") or []
                total = len(items)
                done = sum(1 for i in items if i.get("status") and i["status"] != "pending")
                break
        with self.lock:
            self.online = True
            self.rotation = rotation
            self.playing = rotation.get("current") or self.playing
            self.busy = bool(status.get("busy"))
            self.queued = int(status.get("queued") or 0)
            self.job, self.down, self.total = job, done, total
            devices = status.get("devices") or {}
            self.devices_total = len(devices)
            self.devices_bad = sum(1 for d in devices.values() if d.get("reachable") is False)
            for device in self.devices:
                runtime = devices.get(device["address"])
                if runtime:
                    device["reachable"] = runtime.get("reachable")
            # Drop the tapped highlight once the sign confirms, or after 45s so
            # a scene that never lands cannot leave the button stuck lit.
            if self.pending and (self.playing == self.pending
                                 or time.monotonic() - self.pending_since > 45):
                self.pending = None

    def _run(self):
        while not self._stop.is_set():
            try:
                with self.lock:
                    need_scenes = not self.scenes
                if need_scenes:
                    self._load_scenes()
                self._poll_status()
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
FONT_DIRS = (
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


def _sdl_touch_devices():
    """How many touchscreens SDL found. Zero here with a working picture means
    the input side is the problem, not the display -- worth stating in the log
    rather than leaving someone to infer it from taps doing nothing."""
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
        pygame.mouse.set_visible(False)
        self.fb = None
        self.touch = None
        self.screen = self._open_output(size, fullscreen, backend)
        self.w, self.h = self.screen.get_size()

        scale = max(0.75, min(1.4, self.w / 800.0))
        self.f_small = load_font(int(13 * scale))
        self.f_body  = load_font(int(17 * scale), bold=True)
        self.f_head  = load_font(int(12 * scale), bold=True)
        self.f_brand = load_font(int(16 * scale), bold=True)

        self.bar_h = int(40 * scale)
        self.act_h = int(72 * scale)
        self.prog_h = int(38 * scale)
        self.pad = int(9 * scale)
        self.btn_h = int(62 * scale)

        self.tab_h = int(42 * scale)
        self.chip_h = int(38 * scale)
        self.swatch_h = int(52 * scale)

        self.sign = Sign()
        self.scroll = 0.0
        self.scroll_max = 0.0
        self.buttons = []
        self.actions = []
        self.tabs = []
        self.drag_from = None
        self.dragging = False
        self.last_point = (0, 0)
        self.content_h = 0
        self.tab = "scenes"
        # What the colour and pattern buttons act on. Scenes carry their own
        # targets, so this is ignored there.
        self.target = "all"
        self.target_name = "Everything"

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

    def content_area(self, showing_progress):
        top = self.bar_h + self.tab_h + (self.prog_h if showing_progress else 0)
        return pygame.Rect(0, top, self.w, self.h - top - self.act_h)

    def layout_tabs(self):
        self.tabs = []
        width = self.w // len(TABS)
        for i, (key, label) in enumerate(TABS):
            self.tabs.append(Button((i * width, self.bar_h, width, self.tab_h),
                                    label, "tab", key))

    def _row(self, area, y, items, height, gap=None, min_w=0):
        """Lay `items` out left to right, wrapping. Returns the new y."""
        gap = self.pad if gap is None else gap
        x = self.pad
        for make in items:
            width = max(min_w, make[0])
            if x + width > area.w - self.pad:
                x, y = self.pad, y + height + gap
            make[1](pygame.Rect(x, y, width, height))
            x += width + gap
        return y + height + gap

    def head(self, text, y, area):
        self.buttons.append(Button((self.pad, y, area.w - self.pad * 2,
                                    int(self.f_head.get_height() * 1.4)), text, "head"))
        return y + int(self.f_head.get_height() * 1.4)

    def layout_scenes(self, state, area):
        columns = max(2, (area.w - self.pad) // int(150 * max(0.75, self.w / 800.0)))
        col_w = (area.w - self.pad * (columns + 1)) // columns
        y = area.y + self.pad - int(self.scroll)
        index, last_group = 0, None
        for scene in state["scenes"]:
            group = "Animated" if scene["animated"] else "Solid"
            if group != last_group:
                if index % columns:
                    y += self.btn_h + self.pad
                index, last_group = 0, group
                y = self.head(group, y, area)
            column = index % columns
            x = self.pad + column * (col_w + self.pad)
            self.buttons.append(Button((x, y, col_w, self.btn_h), scene["name"],
                                       "scene", scene["name"]))
            index += 1
            if column == columns - 1:
                y += self.btn_h + self.pad
        if index % columns:
            y += self.btn_h + self.pad
        return y

    def target_options(self, state):
        """Everything, then whatever groups the config defines, in a useful order."""
        preferred = ["letters", "drink", "cup", "straw", "border", "side-a", "side-b"]
        groups = list(state["groups"])
        ordered = [g for g in preferred if g in groups]
        ordered += [g for g in groups if g not in ordered]
        options = [("all", "Everything")]
        options += [("group:" + g, GROUP_LABELS.get(g, g.replace("-", " ").title()))
                    for g in ordered]
        return options

    def layout_colour(self, state, area):
        y = area.y + self.pad - int(self.scroll)
        y = self.head("APPLY TO", y, area)
        items = []
        for target, label in self.target_options(state):
            width = self.f_body.size(label)[0] + int(26 * max(0.75, self.w / 800.0))
            items.append((width, (lambda rect, t=target, l=label:
                                  self.buttons.append(Button(rect, l, "target", (t, l))))))
        y = self._row(area, y, items, self.chip_h)

        y = self.head("COLOUR", y, area)
        columns = max(4, (area.w - self.pad) // int(120 * max(0.75, self.w / 800.0)))
        col_w = (area.w - self.pad * (columns + 1)) // columns
        for i, (hexcode, name) in enumerate(SWATCHES):
            x = self.pad + (i % columns) * (col_w + self.pad)
            row = i // columns
            self.buttons.append(Button((x, y + row * (self.swatch_h + self.pad),
                                        col_w, self.swatch_h), name, "swatch", hexcode))
        y += ((len(SWATCHES) + columns - 1) // columns) * (self.swatch_h + self.pad)

        if state["patterns"]:
            y = self.head("PATTERN", y, area)
            items = []
            for pattern in state["patterns"]:
                label = pattern["name"]
                width = self.f_small.size(label)[0] + int(28 * max(0.75, self.w / 800.0))
                items.append((width, (lambda rect, p=pattern:
                                      self.buttons.append(Button(rect, p["name"],
                                                                 "pattern", p["value"])))))
            y = self._row(area, y, items, self.chip_h)
        return y

    def layout_lights(self, state, area):
        y = area.y + self.pad - int(self.scroll)
        y = self.head("ONE LIGHT AT A TIME", y, area)
        columns = max(2, (area.w - self.pad) // int(150 * max(0.75, self.w / 800.0)))
        col_w = (area.w - self.pad * (columns + 1)) // columns
        for i, device in enumerate(state["devices"]):
            x = self.pad + (i % columns) * (col_w + self.pad)
            row = i // columns
            self.buttons.append(Button((x, y + row * (self.btn_h + self.pad),
                                        col_w, self.btn_h),
                                       device["name"], "device", device))
        rows = (len(state["devices"]) + columns - 1) // columns
        return y + rows * (self.btn_h + self.pad)

    def layout_content(self, state, area):
        self.buttons = []
        if self.tab == "scenes":
            end = self.layout_scenes(state, area)
        elif self.tab == "colour":
            end = self.layout_colour(state, area)
        else:
            end = self.layout_lights(state, area)
        self.content_h = (end + int(self.scroll)) - area.y
        self.scroll_max = max(0.0, self.content_h - area.h + self.pad)

    def layout_actions(self):
        self.actions = []
        y = self.h - self.act_h + self.pad // 2
        height = self.act_h - self.pad
        width = (self.w - self.pad * 4) // 3
        for i, (label, kind) in enumerate((("OFF", "off"), ("NEXT", "next"),
                                           ("ROTATE", "rotate"))):
            x = self.pad + i * (width + self.pad)
            self.actions.append(Button((x, y, width, height), label, kind))

    # -- drawing --------------------------------------------------------------

    def text(self, surface, font, message, color, center=None, topleft=None):
        image = font.render(message, True, color)
        rect = image.get_rect()
        if center:
            rect.center = center
        else:
            rect.topleft = topleft
        surface.blit(image, rect)
        return rect

    def rounded(self, surface, rect, fill, border=None, radius=10, width=1):
        pygame.draw.rect(surface, fill, rect, border_radius=radius)
        if border:
            pygame.draw.rect(surface, border, rect, width=width, border_radius=radius)

    def draw_bar(self, state):
        rect = pygame.Rect(0, 0, self.w, self.bar_h)
        pygame.draw.rect(self.screen, (20, 24, 36), rect)
        pygame.draw.line(self.screen, LINE, (0, self.bar_h - 1), (self.w, self.bar_h - 1))
        x = self.pad
        for glyph, color in (("VI", INK), ("C", ACCENT), ("E", INK)):
            r = self.text(self.screen, self.f_brand, glyph, color,
                          topleft=(x, self.bar_h // 2 - self.f_brand.get_height() // 2))
            x = r.right
        mid = self.bar_h // 2 - self.f_small.get_height() // 2

        if not state["online"]:
            self.text(self.screen, self.f_small, "no answer from the sign", WARN,
                      topleft=(x + self.pad, mid))
            return
        if self.tab == "scenes":
            label = ("Now playing  " + state["playing"]) if state["playing"] else "Ready"
            color = INK
        else:
            # On the acting tabs, what you are about to change matters more than
            # what is currently playing.
            label = "Target:  " + self.target_name
            color = ACCENT2
        self.text(self.screen, self.f_small, label, color, topleft=(x + self.pad, mid))

        bad, total = state["devices_bad"], state["devices_total"]
        health = "%d lit" % total if not bad else "%d down" % bad
        color = OK if not bad else (WARN if bad <= 2 else BAD)
        image = self.f_small.render(health, True, MUTED)
        self.screen.blit(image, (self.w - image.get_width() - self.pad, mid))
        pygame.draw.circle(self.screen, color,
                           (self.w - image.get_width() - self.pad - 10, self.bar_h // 2), 4)

    def draw_tabs(self):
        rect = pygame.Rect(0, self.bar_h, self.w, self.tab_h)
        pygame.draw.rect(self.screen, PANEL, rect)
        pygame.draw.line(self.screen, LINE, (0, rect.bottom - 1), (self.w, rect.bottom - 1))
        for button in self.tabs:
            on = button.payload == self.tab
            if on:
                pygame.draw.rect(self.screen, PANEL2, button.rect)
                pygame.draw.rect(self.screen, ACCENT,
                                 (button.rect.x, button.rect.bottom - 3, button.rect.w, 3))
            self.text(self.screen, self.f_head, button.label, INK if on else MUTED,
                      center=button.rect.center)

    def draw_progress(self, state):
        """A sweep is ~30s. Without this the panel looks frozen and people jab it."""
        rect = pygame.Rect(0, self.bar_h + self.tab_h, self.w, self.prog_h)
        pygame.draw.rect(self.screen, PANEL, rect)
        pygame.draw.line(self.screen, LINE, (0, rect.bottom - 1), (self.w, rect.bottom - 1))
        job = state["job"] or {}
        label = (job.get("label") or "Working").replace("scene: ", "Applying ")
        self.text(self.screen, self.f_small, label[:48], MUTED, topleft=(self.pad, rect.y + 5))
        total = max(1, state["total"])
        count = "%d of %d" % (state["done"], total)
        if state["queued"]:
            count += "   (+%d queued)" % state["queued"]
        image = self.f_small.render(count, True, MUTED)
        self.screen.blit(image, (self.w - image.get_width() - self.pad, rect.y + 5))
        track = pygame.Rect(self.pad, rect.bottom - 12, self.w - self.pad * 2, 6)
        self.rounded(self.screen, track, PANEL2, radius=3)
        filled = int(track.w * state["done"] / total)
        if filled > 0:
            self.rounded(self.screen, pygame.Rect(track.x, track.y, filled, track.h),
                         ACCENT, radius=3)

    def draw_content(self, state, area):
        self.screen.set_clip(area)
        for button in self.buttons:
            if button.rect.bottom < area.y or button.rect.y > area.bottom:
                continue
            kind = button.kind
            if kind == "head":
                self.text(self.screen, self.f_head, button.label.upper(), MUTED,
                          topleft=(button.rect.x, button.rect.y))
            elif kind == "scene":
                playing = (button.payload == state["playing"]
                           and button.payload != state["pending"])
                pending = button.payload == state["pending"]
                fill = (36, 23, 38) if playing else ((21, 36, 48) if pending else PANEL)
                border = ACCENT if playing else (ACCENT2 if pending else LINE)
                self.rounded(self.screen, button.rect, fill, border, radius=11,
                             width=2 if (playing or pending) else 1)
                self.text(self.screen, self.f_body, button.label,
                          ACCENT2 if pending else INK, center=button.rect.center)
            elif kind == "target":
                on = button.payload[0] == self.target
                self.rounded(self.screen, button.rect, PANEL2 if on else PANEL,
                             ACCENT2 if on else LINE, radius=self.chip_h // 2,
                             width=2 if on else 1)
                self.text(self.screen, self.f_body, button.label, INK if on else MUTED,
                          center=button.rect.center)
            elif kind == "swatch":
                colour = pygame.Color(button.payload)
                self.rounded(self.screen, button.rect, (colour.r, colour.g, colour.b),
                             LINE, radius=11)
                # Dark text on pale swatches, so the name stays readable.
                luma = 0.299 * colour.r + 0.587 * colour.g + 0.114 * colour.b
                self.text(self.screen, self.f_small, button.label,
                          (10, 12, 16) if luma > 150 else (255, 255, 255),
                          center=button.rect.center)
            elif kind == "pattern":
                self.rounded(self.screen, button.rect, PANEL, LINE,
                             radius=self.chip_h // 2)
                self.text(self.screen, self.f_small, button.label, INK,
                          center=button.rect.center)
            elif kind == "device":
                device = button.payload
                on = self.target == "device:" + device["address"]
                self.rounded(self.screen, button.rect, PANEL2 if on else PANEL,
                             ACCENT2 if on else LINE, radius=11, width=2 if on else 1)
                self.text(self.screen, self.f_body, device["name"], INK,
                          center=(button.rect.centerx, button.rect.centery - 6))
                reach = device.get("reachable")
                colour = MUTED if reach is None else (OK if reach else BAD)
                word = "unknown" if reach is None else ("ok" if reach else "not answering")
                self.text(self.screen, self.f_small, word, colour,
                          center=(button.rect.centerx, button.rect.centery + 13))
        self.screen.set_clip(None)

    def draw_actions(self, state):
        rect = pygame.Rect(0, self.h - self.act_h, self.w, self.act_h)
        pygame.draw.rect(self.screen, (20, 24, 36), rect)
        pygame.draw.line(self.screen, LINE, (0, rect.y), (self.w, rect.y))
        rotation = state["rotation"] or {}
        for button in self.actions:
            fill, border, ink = PANEL, LINE, INK
            sub, sub_ink = "", MUTED
            if button.kind == "off":
                fill, border, ink = OFF_BG, (74, 34, 48), OFF_INK
                # Off acts on whatever is targeted, so it can turn one letter
                # off without touching the rest of the sign.
                sub = "everything" if self.target == "all" else self.target_name.lower()
            elif button.kind == "next":
                sub = "new scene"
            else:
                on = bool(rotation.get("enabled"))
                if on:
                    border, sub_ink = (40, 80, 60), OK
                if not on:
                    sub = "off"
                elif rotation.get("holding"):
                    sub = "held %dm" % max(1, round(rotation.get("hold_remaining_seconds", 0) / 60))
                elif rotation.get("next_in_seconds") is None:
                    sub = "on"
                else:
                    seconds = max(0, int(rotation["next_in_seconds"]))
                    sub = ("next %dm" % max(1, round(seconds / 60))) if seconds >= 60 \
                        else ("next %ds" % seconds)
            self.rounded(self.screen, button.rect, fill, border, radius=11)
            centre = button.rect.centery - (6 if sub else 0)
            self.text(self.screen, self.f_body, button.label, ink,
                      center=(button.rect.centerx, centre))
            if sub:
                self.text(self.screen, self.f_small, sub[:18], sub_ink,
                          center=(button.rect.centerx, centre + self.f_body.get_height() - 2))

    def draw_toast(self, message):
        if not message:
            return
        image = self.f_small.render(message, True, INK)
        box = image.get_rect()
        box.inflate_ip(28, 18)
        box.centerx = self.w // 2
        box.bottom = self.h - self.act_h - self.pad
        self.rounded(self.screen, box, (8, 10, 14), LINE, radius=9)
        self.screen.blit(image, image.get_rect(center=box.center))

    # -- input ----------------------------------------------------------------

    def tap(self, position):
        for button in self.tabs:
            if button.rect.collidepoint(position):
                if self.tab != button.payload:
                    self.tab, self.scroll = button.payload, 0.0
                return
        for button in self.actions:
            if button.rect.collidepoint(position):
                self.act(button.kind)
                return
        area = self.content_area(self.showing_progress)
        if not area.collidepoint(position):
            return
        for button in self.buttons:
            if button.kind == "head" or not button.rect.collidepoint(position):
                continue
            if button.kind == "scene":
                self.apply_scene(button.payload)
            elif button.kind == "target":
                self.target, self.target_name = button.payload
            elif button.kind == "device":
                device = button.payload
                self.target = "device:" + device["address"]
                self.target_name = device["name"]
                self.tab, self.scroll = "colour", 0.0
                self.sign.say("Now setting %s only" % device["name"])
            elif button.kind == "swatch":
                self.apply_state({"color": button.payload, "brightness": 100,
                                  "power": True},
                                 "%s on %s" % (button.label, self.target_name.lower()))
            elif button.kind == "pattern":
                self.apply_state({"mode": button.payload, "speed": PATTERN_SPEED,
                                  "power": True},
                                 "%s on %s" % (button.label, self.target_name.lower()))
            return

    def apply_state(self, changes, said):
        payload = dict(changes, target=self.target)
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
            result = _post("/api/scene/apply", {"scene": name})
            if not result.get("ok"):
                self.sign.clear_pending()
                self.sign.say(result.get("error") or "could not apply")
        except Exception:
            self.sign.clear_pending()
            self.sign.say("no answer from the sign")

    def act(self, kind):
        threading.Thread(target=self._act, args=(kind,), daemon=True).start()

    def _act(self, kind):
        try:
            if kind == "off":
                self.sign.clear_pending()
                _post("/api/power", {"target": self.target, "on": False})
                self.sign.say("Turning off " + ("everything" if self.target == "all"
                                                else self.target_name.lower()))
            elif kind == "next":
                result = _post("/api/rotation/next", {})
                self.sign.say(("Now playing " + result["scene"]) if result.get("ok")
                              else (result.get("error") or "nothing to play"))
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
        self.layout_actions()
        self.layout_tabs()
        clock = pygame.time.Clock()
        self.showing_progress = False
        drawn = 0
        running = True
        while running:
            for kind, x, y in self.gestures():
                if kind == "quit":
                    running = False
                elif kind == "down":
                    self.drag_from = (x, y)
                    self.dragging = False
                    self.last_point = (x, y)
                elif kind == "move" and self.drag_from:
                    if abs(y - self.drag_from[1]) > 8:
                        self.dragging = True
                    if self.dragging:
                        self.scroll = max(0.0, min(self.scroll_max,
                                                   self.scroll - (y - self.last_point[1])))
                    self.last_point = (x, y)
                elif kind == "up":
                    # A drag scrolls; only a still finger counts as a press.
                    if self.drag_from and not self.dragging:
                        self.tap((x, y))
                    self.drag_from, self.dragging = None, False

            state = self.sign.snapshot()
            self.showing_progress = bool(state["busy"] or state["queued"] or state["job"])
            area = self.content_area(self.showing_progress)
            self.layout_content(state, area)
            self.scroll = max(0.0, min(self.scroll_max, self.scroll))

            self.screen.fill(BG)
            if state["scenes"] or state["devices"]:
                self.draw_content(state, area)
            else:
                self.text(self.screen, self.f_small,
                          "Waiting for the sign…" if state["online"]
                          else "Cannot reach the sign", MUTED,
                          center=(self.w // 2, area.centery))
            self.draw_bar(state)
            self.draw_tabs()
            if self.showing_progress:
                self.draw_progress(state)
            self.draw_actions(state)
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
