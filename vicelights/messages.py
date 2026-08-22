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

    def __init__(self, store, worker):
        self.store = store
        self.worker = worker
        self._lock = threading.Lock()
        self._next_at = None          # monotonic
        self._current = None          # the message the panel is showing
        self._current_at = None       # wall clock, for the UI
        self._index = -1
        self._last_error = ""
        self._warned_unconfigured = False

    # ------------------------------------------------------------------ state

    def configured(self) -> bool:
        matrix = self.store.matrix()
        return bool(matrix.get("enabled") and matrix.get("address"))

    def status(self) -> dict:
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        queue = self.store.messages()
        with self._lock:
            current = dict(self._current) if self._current else None
            next_at = self._next_at
            error = self._last_error
            since = self._current_at
        remaining = None
        if next_at is not None and current and (current.get("dwell") or 0) > HOLD:
            remaining = max(0, int(next_at - time.monotonic()))
        return {
            "enabled": bool(matrix.get("enabled")),
            "configured": self.configured(),
            "address": matrix.get("address", ""),
            "name": matrix.get("name", ""),
            "family": driver.key,
            "family_label": driver.label,
            "char_uuid": driver.characteristic() or "",
            "capabilities": driver.capabilities,
            "brightness": matrix.get("brightness", 100),
            "playlist": bool(matrix.get("playlist")),
            "default_dwell": matrix.get("default_dwell", 20.0),
            "size": {"width": matrix.get("width", 32), "height": matrix.get("height", 16)},
            "queue": queue,
            "queued": len(queue),
            "current": current,
            "showing_since": since,
            "next_in": remaining,
            "last_error": error,
        }

    # ------------------------------------------------------------------ sends

    def _send(self, message: dict, label: str, hold: float) -> bool:
        """Encode and queue one message, and record it as what the panel shows.

        Recorded on submission rather than on completion, deliberately: the
        dwell is about how long a reader has the message in front of them, and
        starting that clock only once the write lands would let a slow radio
        turn a 20-second message into a 50-second one.
        """
        matrix = self.store.matrix()
        driver = matrix_module.driver_for(matrix)
        try:
            frames = driver.text_frames(message)
        except Exception as exc:
            with self._lock:
                self._last_error = "%s: %s" % (type(exc).__name__, exc)
            log.exception("could not encode %s", matrix_module.describe_message(message))
            return False
        job = self.worker.submit_matrix(
            frames, label, coalesce_key="matrix:text",
            payload={"message_id": message.get("id"), "text": message.get("text")})
        if job is None:
            with self._lock:
                self._last_error = "no panel configured"
            return False
        with self._lock:
            self._current = dict(message)
            self._current_at = time.time()
            self._next_at = time.monotonic() + max(0.0, hold)
            self._last_error = ""
        return True

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
        ok = self._send(message, "panel: %s" % matrix_module.describe_message(message), hold)
        with self._lock:
            error = self._last_error
        return {"sent": ok, "message": message, "error": error}

    def play_next(self, force: bool = False) -> dict:
        """Advance to the next enabled message in the queue."""
        queue = self.store.messages(enabled_only=True)
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
        ok = self._send(message, "panel: %s" % matrix_module.describe_message(message), dwell)
        if ok:
            log.info("panel -> %s (holding %.0fs, %d in queue)",
                     matrix_module.describe_message(message), dwell, len(queue))
        with self._lock:
            error = self._last_error
        return {"sent": ok, "message": message, "error": error}

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
        return ok

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
            self.store.update_matrix({"brightness": int(percent)})
        return ok

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
        job = self.worker.submit_matrix(frames, label,
                                        coalesce_key="matrix:" + kind)
        if job is None:
            with self._lock:
                self._last_error = "no panel configured"
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
        if not matrix.get("playlist"):
            return
        queue = self.store.messages(enabled_only=True)
        if not queue:
            if not self._warned_unconfigured:
                log.info("panel playlist is on but the queue is empty")
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
