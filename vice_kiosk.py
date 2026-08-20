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

import json
import os
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
        moving = {m["value"] for m in state.get("modes", []) if m.get("animates")}
        scenes = []
        for scene in state.get("scenes", []):
            steps = scene.get("steps") or []
            animated = any(s.get("mode") and s["mode"] in moving for s in steps)
            scenes.append({"name": scene.get("name", "?"), "animated": animated})
        # Movement first, matching how the web UI groups them.
        scenes.sort(key=lambda s: (not s["animated"],))
        with self.lock:
            self.scenes = scenes
            self.moving_modes = moving

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

def load_font(size, bold=False):
    for name in ("dejavusans", "liberationsans", "freesans", "notosans"):
        try:
            return pygame.font.SysFont(name, size, bold=bold)
        except Exception:
            continue
    return pygame.font.Font(None, size)


class Button:
    __slots__ = ("rect", "label", "sub", "kind", "payload")

    def __init__(self, rect, label, kind, payload=None, sub=""):
        self.rect, self.label, self.kind, self.payload, self.sub = \
            pygame.Rect(rect), label, kind, payload, sub


class Panel:
    def __init__(self, size=None, fullscreen=True):
        pygame.display.init()
        pygame.font.init()
        pygame.mouse.set_visible(False)
        flags = pygame.FULLSCREEN if fullscreen else 0
        self.screen = pygame.display.set_mode(size or (800, 480), flags)
        pygame.display.set_caption("Vice Sign")
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

        self.sign = Sign()
        self.scroll = 0.0
        self.scroll_max = 0.0
        self.buttons = []
        self.actions = []
        self.drag_from = None
        self.dragging = False
        self.content_h = 0

    # -- layout ---------------------------------------------------------------

    def scene_area(self, showing_progress):
        top = self.bar_h + (self.prog_h if showing_progress else 0)
        return pygame.Rect(0, top, self.w, self.h - top - self.act_h)

    def layout_scenes(self, scenes, area):
        """Grid of scene buttons, grouped with movement first."""
        self.buttons = []
        columns = max(2, (self.w - self.pad) // int(150 * max(0.75, self.w / 800.0)))
        col_w = (area.w - self.pad * (columns + 1)) // columns
        y = area.y + self.pad - int(self.scroll)
        index, last_group = 0, None

        for scene in scenes:
            group = "Animated" if scene["animated"] else "Solid"
            if group != last_group:
                if index % columns:                     # finish the current row
                    y += self.btn_h + self.pad
                index = 0
                self.buttons.append(Button((self.pad, y, area.w - self.pad * 2,
                                            int(self.f_head.get_height() * 1.5)),
                                           group, "head"))
                y += int(self.f_head.get_height() * 1.5)
                last_group = group
            column = index % columns
            x = self.pad + column * (col_w + self.pad)
            self.buttons.append(Button((x, y, col_w, self.btn_h), scene["name"], "scene",
                                       scene["name"]))
            index += 1
            if column == columns - 1:
                y += self.btn_h + self.pad
        if index % columns:
            y += self.btn_h + self.pad
        self.content_h = (y + int(self.scroll)) - area.y
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

        if not state["online"]:
            self.text(self.screen, self.f_small, "no answer from the sign", WARN,
                      topleft=(x + self.pad, self.bar_h // 2 - self.f_small.get_height() // 2))
            return
        label = ("Now playing  " + state["playing"]) if state["playing"] else "Ready"
        self.text(self.screen, self.f_small, label, INK,
                  topleft=(x + self.pad, self.bar_h // 2 - self.f_small.get_height() // 2))

        bad, total = state["devices_bad"], state["devices_total"]
        health = "%d lit" % total if not bad else "%d down" % bad
        color = OK if not bad else (WARN if bad <= 2 else BAD)
        image = self.f_small.render(health, True, MUTED)
        self.screen.blit(image, (self.w - image.get_width() - self.pad,
                                 self.bar_h // 2 - image.get_height() // 2))
        pygame.draw.circle(self.screen, color,
                           (self.w - image.get_width() - self.pad - 10, self.bar_h // 2), 4)

    def draw_progress(self, state):
        """A sweep is ~30s. Without this the panel looks frozen and people jab it."""
        rect = pygame.Rect(0, self.bar_h, self.w, self.prog_h)
        pygame.draw.rect(self.screen, PANEL, rect)
        pygame.draw.line(self.screen, LINE, (0, rect.bottom - 1), (self.w, rect.bottom - 1))
        job = state["job"] or {}
        label = (job.get("label") or "Working").replace("scene: ", "Applying ")
        self.text(self.screen, self.f_small, label, MUTED,
                  topleft=(self.pad, rect.y + 5))
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

    def draw_scenes(self, state, area):
        self.screen.set_clip(area)
        for button in self.buttons:
            if button.rect.bottom < area.y or button.rect.y > area.bottom:
                continue                                  # off-screen, skip drawing
            if button.kind == "head":
                self.text(self.screen, self.f_head, button.label.upper(), MUTED,
                          topleft=(button.rect.x, button.rect.y))
                continue
            playing = button.payload == state["playing"] and button.payload != state["pending"]
            pending = button.payload == state["pending"]
            fill = (36, 23, 38) if playing else ((21, 36, 48) if pending else PANEL)
            border = ACCENT if playing else (ACCENT2 if pending else LINE)
            self.rounded(self.screen, button.rect, fill, border, radius=11,
                         width=2 if (playing or pending) else 1)
            self.text(self.screen, self.f_body, button.label,
                      INK if not pending else ACCENT2, center=button.rect.center)
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
                sub = "all lights"
            elif button.kind == "next":
                sub = "new scene"
            else:
                on = bool(rotation.get("enabled"))
                if on:
                    border = (40, 80, 60)
                    sub_ink = OK
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
                self.text(self.screen, self.f_small, sub, sub_ink,
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
        for button in self.actions:
            if button.rect.collidepoint(position):
                self.act(button.kind)
                return
        area = self.scene_area(self.showing_progress)
        if not area.collidepoint(position):
            return
        for button in self.buttons:
            if button.kind == "scene" and button.rect.collidepoint(position):
                self.apply_scene(button.payload)
                return

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
                _post("/api/power", {"target": "all", "on": False})
                self.sign.say("Turning everything off")
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

    # -- main loop ------------------------------------------------------------

    def run(self, frames=None, on_frame=None):
        self.sign.start()
        self.layout_actions()
        clock = pygame.time.Clock()
        self.showing_progress = False
        drawn = 0
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self.drag_from = event.pos
                    self.dragging = False
                elif event.type == pygame.MOUSEMOTION and self.drag_from:
                    if abs(event.pos[1] - self.drag_from[1]) > 8:
                        self.dragging = True
                    if self.dragging:
                        self.scroll = max(0.0, min(self.scroll_max,
                                                   self.scroll - event.rel[1]))
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                    # A drag scrolls; only a still finger counts as a press.
                    if self.drag_from and not self.dragging:
                        self.tap(event.pos)
                    self.drag_from, self.dragging = None, False

            state = self.sign.snapshot()
            self.showing_progress = bool(state["busy"] or state["queued"] or state["job"])
            area = self.scene_area(self.showing_progress)
            self.layout_scenes(state["scenes"], area)

            self.screen.fill(BG)
            if state["scenes"]:
                self.draw_scenes(state, area)
            else:
                self.text(self.screen, self.f_small,
                          "Waiting for the sign…" if state["online"]
                          else "Cannot reach the sign", MUTED,
                          center=(self.w // 2, area.centery))
            self.draw_bar(state)
            if self.showing_progress:
                self.draw_progress(state)
            self.draw_actions(state)
            self.draw_toast(state["toast"])
            pygame.display.flip()

            drawn += 1
            if on_frame:
                on_frame(self, drawn)
            if frames and drawn >= frames:
                running = False
            clock.tick(FPS)

        self.sign.stop()
        pygame.quit()


def main():
    windowed = "--windowed" in sys.argv
    size = None
    for argument in sys.argv[1:]:
        if argument.startswith("--size="):
            width, _, height = argument[7:].partition("x")
            size = (int(width), int(height))
    Panel(size=size, fullscreen=not windowed).run()


if __name__ == "__main__":
    main()
