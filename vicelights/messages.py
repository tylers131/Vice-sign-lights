"""The message queue for the text panel.

A queued message is not a queued BLE job.  The BLE worker's queue is about
getting bytes onto the radio and drains as fast as the adapter allows; this
queue is about what the panel *says*, and it drains at reading speed.  So the
two are separate: the runner holds a message on the panel for its dwell, then
hands the next one to the worker.

Timed on ``time.monotonic`` for the same reason rotation is -- the Pi has no
RTC and comes up on the playa knowing nothing about the date, and the panel
still has to cycle.
"""

from __future__ import annotations

import logging
import threading
import time

from . import matrix as matrix_module

log = logging.getLogger("vicelights.messages")

# A dwell of 0 means "hold this until something replaces it".  In a cycling
# playlist that would stall forever, so the playlist substitutes the panel's
# default dwell; a message sent by hand keeps the literal meaning.
HOLD = 0.0


class MatrixRunner:
    """Cycle queued messages across the panel, one at a time."""

    def __init__(self, store, worker, schedule=None):
        self.store = store
        self.worker = worker
        # When schedule mode is on (matrix.schedule), the queue is built by
        # this instead of read from the saved messages. None on a runner
        # created without one -- then schedule mode simply has no effect.
        self.schedule = schedule
        self._lock = threading.Lock()
        self._next_at = None          # monotonic
        self._current = None          # the message the panel is showing
        self._current_at = None       # wall clock, for the UI
        self._index = -1
        self._last_error = ""
        self._pages = []              # a long message, cut into panel-width pages
        self._page = 0
        self._page_at = None          # monotonic; when to turn the page
        self._source = None           # the whole message the pages came from
        self._job = None              # the write we are waiting on
        self._job_page = None         # what that write was meant to put up
        self._landed = None           # the last write that actually completed
        self._unsure = []             # writes that failed and may be half up
        self._warned_unconfigured = False
        self._applied_brightness = None   # last brightness we commanded (auto-dim)

    # ------------------------------------------------------------------ state

    def configured(self) -> bool:
        matrix = self.store.matrix()
        return bool(matrix.get("enabled") and matrix.get("address"))

    def _scheduled(self) -> bool:
        """Is the panel driven by the event calendar right now?

        Preview counts: it drives the panel from the calendar too, just walking
        the week's days on a timer instead of reading the real date -- so before
        the event, when "today" is empty, you can still see how every day looks.
        """
        matrix = self.store.matrix()
        return bool(self.schedule
                    and (matrix.get("schedule") or matrix.get("schedule_preview")))

    def _queue(self):
        """What to rotate: the calendar's messages, or the saved queue.

        Both are lists of the same message dicts, so the rest of the runner
        cannot tell which it got. The schedule rebuilds its list every call so
        it stays current with the clock and the temperature; the slot ids are
        stable, so advancing by id still works across rebuilds.
        """
        if self._scheduled():
            try:
                return self.schedule.messages(
                    preview=bool(self.store.matrix().get("schedule_preview")))
            except Exception:
                log.exception("building the schedule failed; panel idle")
                return []
        return self.store.messages(enabled_only=True)

    def unusable(self) -> str:
        """Why the panel cannot be driven, or "" if it can.

        "No panel configured" is the wrong answer for a panel that is paired
        but whose family we do not recognise -- it sends someone to re-pair a
        device that is already paired. Name the real problem.
        """
        matrix = self.store.matrix()
        if not matrix.get("enabled"):
            return "the panel is switched off in settings"
        if not matrix.get("address"):
            return "no panel is paired"
        driver = matrix_module.driver_for(matrix)
        if not driver.characteristic():
            return ("%s is paired but its type is not recognised, so nothing "
                    "knows which characteristic to write to. Run "
                    "matrix_probe.py info %s on the Pi."
                    % (matrix.get("name") or matrix.get("address"),
                       matrix.get("address")))
        return ""

    def status(self) -> dict:
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        # The queue as it will actually play: the calendar's when scheduled,
        # the saved messages otherwise, so the UI's count and preview match
        # what the panel is doing.
        queue = self._queue() if self._scheduled() else self.store.messages()
        with self._lock:
            # The whole message, not the page fragment on the glass. Someone
            # reading "now showing" wants to know which of their messages is
            # up; a partial page like "ICED CO" is not one of them.
            current = dict(self._source or self._current or {}) or None
            next_at = self._next_at
            error = self._last_error
            since = self._current_at
            pages = len(self._pages)
            page = self._page
        remaining = None
        if next_at is not None and current and (current.get("dwell") or 0) > HOLD:
            remaining = max(0, int(next_at - time.monotonic()))
        # Every panel and how it is faring, so the UI can show two signs and
        # flag one that has stopped answering. The worker keys its per-device
        # health by address, panels included.
        states = self.worker.device_state
        panels = []
        for entry in matrix.get("panels") or []:
            health = states.get(entry["address"], {})
            panels.append({
                "address": entry["address"],
                "name": entry.get("name") or entry["address"],
                "enabled": bool(entry.get("enabled", True)),
                "last_error": health.get("last_error", ""),
                "failures": int(health.get("consecutive_failures", 0)),
            })
        return {
            "enabled": bool(matrix.get("enabled")),
            "configured": self.configured(),
            "address": matrix.get("address", ""),
            "name": matrix.get("name", ""),
            "panels": panels,
            # Two different things: which driver is in use, and what the
            # config asked for. A panel set to "auto" that fell back to a guess
            # must not look like a panel someone chose that driver for.
            "family": driver.key,
            "family_label": driver.label,
            "family_setting": matrix.get("family", "auto"),
            "scale": matrix.get("scale", "auto"),
            "stretch": bool(matrix.get("stretch", True)),
            "pixel_layout": matrix.get("pixel_layout",
                                       matrix_module.DEFAULT_PIXEL_LAYOUT),
            # Which route text takes to the panel, and the two settings that
            # only matter on the native one. Reported so "did that actually
            # save" is a question the API answers.
            "text_mode": matrix.get("text_mode", "pixels"),
            "text_animation": matrix.get("text_animation", "static"),
            "scrolling": (matrix.get("text_mode") == "native"
                          and matrix.get("text_animation", "static") != "static"),
            "bitmap_order": matrix.get("bitmap_order", "msb"),
            "text_reversed": bool(matrix.get("text_reversed")),
            "char_uuid": driver.characteristic() or "",
            "capabilities": driver.capabilities,
            "modes": list(driver.modes),
            "brightness": matrix.get("brightness", 100),
            "night_dim_enabled": bool(matrix.get("night_dim_enabled")),
            "night_dim_start": matrix.get("night_dim_start", "23:00"),
            "night_dim_end": matrix.get("night_dim_end", "06:00"),
            "night_brightness": matrix.get("night_brightness", 15),
            "playlist": bool(matrix.get("playlist")),
            "schedule": self._scheduled(),
            "schedule_preview": bool(matrix.get("schedule_preview")),
            "default_dwell": matrix.get("default_dwell", 20.0),
            "size": {"width": matrix.get("width", 32), "height": matrix.get("height", 16)},
            "queue": queue,
            "queued": len(queue),
            "current": current,
            "showing_since": since,
            "pages": pages,
            "page": (page + 1) if pages else 0,
            "paging": bool(matrix.get("paging", True)),
            "page_seconds": self._page_seconds(),
            "next_in": remaining,
            "last_error": error or self.unusable(),
        }

    # ------------------------------------------------------------------ sends

    def _paint(self, page: dict, label: str, manual: bool = False) -> bool:
        """Put one page on the panel and record it as what is showing.

        Recorded on submission rather than on completion, deliberately: the
        dwell is about how long a reader has the message in front of them, and
        starting that clock only once the write lands would let a slow radio
        turn a 20-second message into a 50-second one.
        """
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        # What is on the panel now, so the new page can erase exactly what the
        # old one lit rather than repainting every pixel or leaving the two
        # superimposed.
        self._reconcile()
        with self._lock:
            # What might be on the glass: what we know landed, plus anything
            # whose write died partway. Erasing against only the first leaves
            # half a message underneath the next one.
            # Not _current: that is set when a write is *queued*, so after a
            # failure it names a message the panel never got, and erasing
            # against it would leave the real one lit underneath.
            previous = [dict(entry) for entry in
                        ([self._landed] if self._landed else []) + self._unsure]
        try:
            frames = driver.text_frames(page, previous=previous or None)
        except Exception as exc:
            with self._lock:
                self._last_error = "%s: %s" % (type(exc).__name__, exc)
            log.exception("could not encode %s", matrix_module.describe_message(page))
            return False
        job = self.worker.submit_matrix(
            frames, label, coalesce_key="matrix:text", manual=manual,
            payload={"message_id": page.get("id"), "text": page.get("text")})
        if job is None:
            with self._lock:
                self._last_error = self.unusable() or "the panel did not take it"
            return False
        with self._lock:
            self._current = dict(page)
            self._current_at = time.time()
            self._last_error = ""
            self._job = job
            self._job_page = dict(page)
        return True

    def _reconcile(self):
        """Find out whether the last write actually landed.

        A message is recorded as showing the moment it is queued, so the dwell
        counts reading time rather than radio time. That is right for timing
        and wrong for erasing: a write that timed out did not put anything up,
        or put half of it up, and the next message erasing against it leaves
        the real one underneath. So the job is checked once it finishes, and a
        write that did not succeed joins the list of things that might still
        be lit.
        """
        with self._lock:
            job, page = self._job, self._job_page
        if job is None or getattr(job, "state", "") in ("queued", "running"):
            return
        landed = getattr(job, "state", "") == "done" and getattr(job, "ok", 0)
        with self._lock:
            if self._job is not job:
                return
            self._job = None
            self._job_page = None
            if landed:
                self._landed = page
                self._unsure = []
            elif page:
                # Three is enough to cover a bad patch of radio without
                # growing the erase into a full-panel repaint.
                self._unsure = ([page] + self._unsure)[:3]

    def _page_seconds(self) -> float:
        """How long one page of a long message stays up.

        Floored at the scheduler's tick: pages turn from that thread, so
        asking for two seconds on a five-second tick would not give two
        seconds, it would give five and a status line that lies about it.
        """
        matrix = self.store.matrix()
        try:
            seconds = float(matrix.get("page_seconds", 5.0))
        except (TypeError, ValueError):
            seconds = 5.0
        return max(5.0, min(120.0, seconds))

    def _split(self, message: dict) -> tuple:
        """(pages, style) for a message too wide for the panel, else ([], None).

        This is what the panel does instead of scrolling.  Shifting a message
        one column moves nearly every lit pixel, so a scroll frame costs about
        as many writes as the message has pixels -- under two frames a second
        here -- and it never stops, so the panel would hold the radio the
        twelve controllers share for as long as the message is up.  Paging
        costs one draw per page and then nothing.
        """
        matrix = self.store.matrix()
        if not matrix.get("paging", True):
            return [], None
        # A panel that pages itself does not need this done for it -- and doing
        # it anyway would cut the message into pieces the panel would then
        # scroll one piece at a time.
        if matrix_module.driver_for(matrix).animates:
            return [], None
        plan = matrix_module.paginate(matrix, message.get("text") or "")
        pages = plan.get("pages") or []
        if len(pages) < 2:
            return [], None
        style = {"scale": plan["scale"], "bold": plan["bold"],
                 "spacing": plan["spacing"]}
        return [dict(message, text=page, plan=style) for page in pages], style

    def _send(self, message: dict, label: str, hold: float,
              manual: bool = False) -> bool:
        """Show a message, in pages if it is too wide to fit at once."""
        pages, _style = self._split(message)
        first = pages[0] if pages else dict(message)
        if pages:
            label = "%s (1/%d)" % (label, len(pages))
        if not self._paint(first, label, manual=manual):
            return False
        seconds = self._page_seconds()
        now = time.monotonic()
        with self._lock:
            self._source = dict(message)
            self._pages = pages
            self._page = 0
            # Hold a paged message at least long enough to read all of it --
            # otherwise a 20-second dwell on a four-page message would show
            # page one, page two, and then move on mid-sentence.
            span = len(pages) * seconds if pages else 0.0
            self._page_at = (now + seconds) if pages else None
            self._next_at = now + max(max(0.0, hold), span)
        if pages:
            log.info("panel: %d pages at %.0fs each", len(pages), seconds)
        return True

    def _reset_pages(self):
        """Called with the lock held."""
        self._pages = []
        self._page = 0
        self._page_at = None
        self._source = None
        self._job = None
        self._job_page = None
        self._landed = None

    def _turn_page(self) -> bool:
        """Show the next page of the current message, wrapping at the end.

        Wrapping matters for a standing message: a hand-sent message holds
        until something replaces it, and stopping on the last page would mean
        the sign spent the night showing the tail of a sentence.
        """
        seconds = self._page_seconds()
        with self._lock:
            pages = list(self._pages)
            index = (self._page + 1) % len(pages) if pages else 0
        if len(pages) < 2:
            with self._lock:
                self._reset_pages()
            return False
        # A page turn is the timer's work, not a person's.
        ok = self._paint(pages[index],
                         "panel: page %d/%d" % (index + 1, len(pages)))
        with self._lock:
            if ok:
                self._page = index
                self._page_at = time.monotonic() + seconds
            else:
                # Do not spin on a panel that is not answering; the message
                # itself will be retried by the playlist.
                self._page_at = time.monotonic() + seconds
        return ok

    def _page_due(self) -> bool:
        with self._lock:
            at = self._page_at
            more = len(self._pages) > 1
        return more and at is not None and time.monotonic() >= at

    def send(self, raw: dict, hold: float = None) -> dict:
        """Show a message right now, ahead of whatever the playlist had queued.

        The message need not be in the queue: this is the "type it and send it"
        path.  Its dwell decides how long the playlist stays out of the way, so
        a hand-sent message is not overwritten two seconds later.
        """
        matrix = self.store.matrix()
        message = matrix_module.normalize_message(raw, matrix.get("default_dwell", 20.0))
        if not message["text"]:
            raise ValueError("a message needs some text")
        if hold is None:
            hold = message["dwell"] or self._forever()
        # Typed by a person, so it is tried even if the panel has been
        # failing: an error on the screen beats a message that quietly never
        # went anywhere.
        driver = matrix_module.driver_for(matrix)
        ok = self._send(message,
                        "panel: %s" % matrix_module.describe_message(
                            message, driver.mode_for(message)),
                        hold, manual=True)
        with self._lock:
            error = self._last_error
        return {"sent": ok, "message": message, "error": error}

    def play_next(self, force: bool = False) -> dict:
        """Advance to the next enabled message in the queue."""
        queue = self._queue()
        if not queue:
            with self._lock:
                self._next_at = None
            return {"sent": False, "message": None, "error": "the queue is empty"}
        matrix = self.store.matrix()
        with self._lock:
            current_id = (self._current or {}).get("id")
        start = 0
        if current_id:
            for index, message in enumerate(queue):
                if message["id"] == current_id:
                    start = index + 1
                    break
        message = queue[start % len(queue)]
        dwell = message["dwell"] or matrix.get("default_dwell", 20.0)
        driver = matrix_module.driver_for(matrix)
        described = matrix_module.describe_message(message, driver.mode_for(message))
        ok = self._send(message, "panel: %s" % described, dwell, manual=force)
        if ok:
            log.info("panel -> %s (holding %.0fs, %d in queue)",
                     described, dwell, len(queue))
        with self._lock:
            error = self._last_error
        return {"sent": ok, "message": message, "error": error}

    def forget_current(self):
        """Stop assuming we know what is on the panel.

        After anything that changes the display outside this code -- a blank, a
        buffer switch, someone using the vendor app -- the remembered message
        is no longer what is up there, and erasing against it would leave
        fragments behind.
        """
        with self._lock:
            self._current = None
            self._reset_pages()

    def clear(self) -> bool:
        """Blank the panel, and mean it.

        Turning the playlist off is not a side effect, it is the request: with
        it left running the next tick would put a message straight back up, so
        "clear" would blank the panel for at most five seconds. Off is also
        visible -- the toggle moves in the UI -- rather than a hidden pause
        that expires on its own.
        """
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        ok = self._send_control(driver.clear_frames(), "panel: clear", "clear")
        if matrix.get("playlist"):
            self.store.update_matrix({"playlist": False})
            log.info("panel cleared; playlist stopped")
        with self._lock:
            self._current = None
            self._current_at = None
            self._next_at = None
            self._unsure = []
            self._landed = None
            self._reset_pages()
        return ok

    def program(self) -> dict:
        """Store the queue in the panel and let the panel cycle it.

        The point is not convenience, it is the radio. Cycling from here costs
        a connection and a write every time a message changes, on the one
        antenna the twelve controllers also need. Stored in the panel's own
        slots, the whole playlist costs one connection now and nothing at all
        afterwards -- the sign keeps running with the Pi switched off.

        Only for a panel that animates: drawing pixel by pixel has no slots to
        put anything in.
        """
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        if not driver.animates:
            return {"ok": False, "error": "this panel draws from here; there is "
                                          "nothing on it to store a message in",
                    "stored": 0}
        queue = self.store.messages(enabled_only=True)[:100]
        if not queue:
            return {"ok": False, "error": "the queue is empty", "stored": 0}
        frames = []
        for slot, message in enumerate(queue, 1):
            frames.extend(driver.native_text_frames(message, slot=slot))
        frames.extend(driver.program_frames(range(1, len(queue) + 1)))
        job = self.worker.submit_matrix(
            frames, "panel: store %d message(s)" % len(queue),
            coalesce_key="matrix:program", manual=True)
        if job is None:
            return {"ok": False, "stored": 0,
                    "error": self.unusable() or "the panel did not take it"}
        # Two things cycling the same panel would fight each other, and the one
        # that is not using the radio should win.
        if matrix.get("playlist"):
            self.store.update_matrix({"playlist": False})
        with self._lock:
            self._last_error = ""
            self._reset_pages()
            self._current = None
        log.info("panel: stored %d message(s) in its own slots", len(queue))
        return {"ok": True, "stored": len(queue)}

    def program_clear(self) -> dict:
        """Stop the panel cycling its own slots."""
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        frames = driver.program_clear_frames()
        if not frames:
            return {"ok": False, "error": "this panel has no stored playlist"}
        ok = self._send_control(frames, "panel: clear its stored playlist",
                                "program")
        return {"ok": ok, "error": "" if ok else self._last_error}

    def power(self, on: bool) -> bool:
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        return self._send_control(driver.power_frames(on),
                                  "panel: %s" % ("on" if on else "off"), "power")

    def brightness(self, percent: int) -> bool:
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        ok = self._send_control(driver.brightness_frames(percent),
                                "panel: brightness %d%%" % int(percent), "brightness")
        if ok:
            # The slider sets the daytime level. Record it as applied so the
            # auto-dim tick does not re-send the same value a moment later; if
            # a night window is active it will still override on the next tick.
            self.store.update_matrix({"brightness": int(percent)})
            self._applied_brightness = int(percent)
        return ok

    # ----------------------------------------------------------- night dimming

    def _clock_now(self):
        """The wall clock, or None if unset/unavailable -- via the schedule."""
        if self.schedule is None:
            return None
        try:
            if not self.schedule.clock.clock_ok():
                return None
            return self.schedule.clock.now()
        except Exception:
            return None

    @staticmethod
    def _in_window(now, start: str, end: str) -> bool:
        """Is ``now`` inside the [start, end) wall-clock window (wraps midnight)?"""
        def mins(hhmm):
            hour, _, minute = str(hhmm).partition(":")
            return int(hour) * 60 + int(minute)
        try:
            lo, hi = mins(start), mins(end)
        except (ValueError, TypeError):
            return False
        if lo == hi:
            return False
        cur = now.hour * 60 + now.minute
        return lo <= cur < hi if lo < hi else (cur >= lo or cur < hi)

    def _target_brightness(self, matrix) -> int:
        """The panel brightness the clock calls for: night level or day level."""
        day = int(matrix.get("brightness", 100))
        if not matrix.get("night_dim_enabled"):
            return day
        now = self._clock_now()
        if now is None:
            return day          # no clock -> cannot know it is night; stay bright
        if self._in_window(now, matrix.get("night_dim_start", "23:00"),
                           matrix.get("night_dim_end", "06:00")):
            return int(matrix.get("night_brightness", 15))
        return day

    def _apply_night_dim(self, matrix):
        """Send the panel its clock-appropriate brightness when it changes.

        Only the panel: the sign's LED strips are never dimmed. The command is
        sent once when the target changes (a window boundary, or the day level
        being edited), not every tick, and it does not persist -- the daytime
        level the slider set is left intact so leaving the window restores it.
        """
        target = self._target_brightness(matrix)
        if target == self._applied_brightness:
            return
        driver = matrix_module.driver_for(matrix)
        frames = driver.brightness_frames(target)
        if not frames:
            # This panel has no brightness command; nothing to dim with. Record
            # the target anyway so we do not retry it every tick.
            self._applied_brightness = target
            return
        if self._send_control(frames, "panel: auto-brightness %d%%" % target,
                              "brightness"):
            self._applied_brightness = target

    def _send_control(self, frames, label: str, kind: str) -> bool:
        """Queue one control command.

        The coalesce key is per command, not one key for all of them. Sharing
        a key would make "turn the panel on" and "set brightness" supersede
        each other, so half of a two-tap sequence would silently never
        happen -- while a key per command still collapses the twenty
        brightness values a slider drag produces into the last one.
        """
        if not frames:
            with self._lock:
                self._last_error = "this panel has no command for that"
            return False
        job = self.worker.submit_matrix(frames, label, manual=True,
                                        coalesce_key="matrix:" + kind)
        if job is None:
            with self._lock:
                self._last_error = self.unusable() or "the panel did not take it"
            return False
        with self._lock:
            self._last_error = ""
        return True

    # ------------------------------------------------------------------- tick

    @staticmethod
    def _forever() -> float:
        # Far enough out that the playlist will not step on a standing message,
        # near enough that it is a number and not an infinity in the status API.
        return 24 * 3600.0

    def tick(self):
        """Called from the scheduler thread every few seconds."""
        matrix = self.store.matrix()
        if not (matrix.get("enabled") and matrix.get("address")):
            return
        # Dim the panel at night before anything else -- it applies whether or
        # not messages are cycling, and it is cheap (a no-op unless the target
        # just changed).
        self._apply_night_dim(matrix)
        self._reconcile()
        # Pages turn whether or not the playlist is running: a message sent by
        # hand is still too long to fit, and its later pages are as much of the
        # message as the first one.  But the playlist wins when the dwell is
        # up, or a paged message would wrap forever and nothing else would ever
        # get a turn.
        # Schedule mode cycles the panel whether or not the saved-message
        # playlist is on -- it is its own reason to be playing.
        playing = bool(matrix.get("playlist")) or self._scheduled()
        with self._lock:
            dwell_up = self._next_at is not None and time.monotonic() >= self._next_at
        if self._page_due() and not (playing and dwell_up):
            if self.worker.busy:
                # A page cannot turn while the radio is busy elsewhere, and a
                # page nobody saw is not a page: restart its clock rather than
                # turning the instant a scene sweep ends.
                with self._lock:
                    self._page_at = time.monotonic() + self._page_seconds()
            else:
                self._turn_page()
            return
        if not playing:
            return
        queue = self._queue()
        if not queue:
            if not self._warned_unconfigured:
                log.info("panel is set to play but has nothing to show")
                self._warned_unconfigured = True
            return
        self._warned_unconfigured = False
        with self._lock:
            if self._next_at is None:
                self._next_at = time.monotonic()
            due = time.monotonic() >= self._next_at
        if not due:
            return
        if self.worker.busy:
            # Do not stack a panel write behind a full sweep and then another
            # behind that. Look again on the next tick.
            with self._lock:
                self._next_at = time.monotonic() + 5.0
            return
        self.play_next()
