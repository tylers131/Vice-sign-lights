"""Serialized BLE worker.

Everything BLE goes through one thread running one asyncio loop, processing one
job at a time, touching one device at a time: connect -> write -> disconnect.
The Pi Zero W has a single radio shared between the wifi AP and BLE, and the
ELK-BLEDOM controllers do not tolerate a swarm of parallel connections, so
serialization is not a simplification -- it is the design.

Web requests never block on BLE.  They enqueue a job, get an id back
immediately, and poll ``status()`` for progress.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from . import matrix as matrix_module
from . import protocol
from .config import new_id

log = logging.getLogger("vicelights.ble")

FAKE = os.environ.get("VICELIGHTS_FAKE_BLE", "").strip() not in ("", "0", "false")

try:  # pragma: no cover - import shape depends on host
    if FAKE:
        raise ImportError("fake BLE requested")
    from bleak import BleakClient, BleakScanner
    HAVE_BLEAK = True
except Exception as exc:  # pragma: no cover
    BleakClient = None
    BleakScanner = None
    HAVE_BLEAK = False
    if not FAKE:
        log.warning("bleak unavailable (%s); running in simulation mode", exc)


class Job:
    """One unit of queued BLE work."""

    __slots__ = ("id", "kind", "label", "coalesce_key", "created", "started",
                 "finished", "state", "items", "result", "error", "payload",
                 "stagger", "manual")

    def __init__(self, kind, label, items=None, coalesce_key=None, payload=None,
                 stagger=0.0, manual=False):
        self.id = new_id()
        self.kind = kind                  # apply | scan | test
        self.label = label
        self.coalesce_key = coalesce_key
        self.created = time.time()
        self.started = None
        self.finished = None
        self.state = "queued"             # queued|running|done|failed|superseded
        self.items = items or []          # [{address,name,status,detail}]
        self.result = None
        self.error = None
        self.payload = payload or {}
        # Extra seconds to wait between devices, on top of the usual gap.
        # Writes are serialised anyway, so a built-in pattern started one unit
        # at a time already rolls across the sign; this makes the roll a chosen
        # length rather than however long a connect happened to take.
        self.stagger = max(0.0, float(stagger or 0.0))
        # Did a person ask for this, or did a timer? Only used for cooldown:
        # someone standing at the sign watching for their message deserves an
        # attempt and an error, where a playlist tick deserves to be quiet.
        self.manual = bool(manual)

    @property
    def total(self):
        return len(self.items)

    @property
    def done(self):
        return sum(1 for item in self.items if item["status"] in ("ok", "failed", "skipped"))

    @property
    def ok(self):
        return sum(1 for item in self.items if item["status"] == "ok")

    @property
    def failed(self):
        return sum(1 for item in self.items if item["status"] == "failed")

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "total": self.total,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "items": list(self.items),
            "error": self.error,
            "result": self.result,
        }


class BleWorker:
    def __init__(self, store, history=25):
        self.store = store
        self._history_len = history
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._pending_by_key = {}
        self._current = None
        self._loop = None
        self._queue = None
        self._ready = threading.Event()
        self._thread = None
        self._stop = False
        self.device_state = {}
        self.last_scan = {"at": None, "devices": []}
        # Devices whose last queued command put them into a built-in pattern.
        # A solid colour aimed at one of these needs help getting out first.
        self._in_pattern = set()
        # Disconnects we stopped waiting on. Held so they are not garbage
        # collected mid-flight, discarded as they finish.
        self._pending_disconnects = set()
        # The write happening right now, so it can be cancelled. Without this,
        # stopping meant waiting out attempts x connect_timeout -- up to 36s per
        # unreachable device -- which is exactly when you most want to stop.
        self._inflight = None
        # When someone last drove the sign by hand. Rotation backs off after it
        # so the sign is not changing under you while you are looking at it.
        # Monotonic, not wall clock -- see note_manual. None, not 0.0: on the
        # monotonic clock 0.0 is boot, so a zero here would read as "touched
        # at startup" and hold rotation for the first quarter hour of every
        # power-on.
        self.last_manual_at = None

    # ------------------------------------------------------------ lifecycle

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="ble-worker", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            log.error("BLE worker failed to start within 10s")
        log.info("BLE worker started (backend=%s)", "bleak" if HAVE_BLEAK else "simulated")

    def stop(self):
        self._stop = True
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._queue = asyncio.Queue()
        self._ready.set()
        try:
            loop.run_until_complete(self._main())
        except Exception:
            log.exception("BLE worker loop crashed")
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _main(self):
        while not self._stop:
            job = await self._queue.get()
            if job is None:
                break
            with self._lock:
                if self._pending_by_key.get(job.coalesce_key) is job:
                    self._pending_by_key.pop(job.coalesce_key, None)
                if job.state == "superseded":
                    continue
                job.state = "running"
                job.started = time.time()
                self._current = job
            try:
                if job.kind == "scan":
                    await self._do_scan(job)
                else:
                    await self._do_writes(job)
                job.state = "failed" if (job.error or (job.total and job.ok == 0)) else "done"
            except Exception as exc:
                log.exception("job %s (%s) blew up", job.id, job.label)
                job.error = str(exc)
                job.state = "failed"
            finally:
                job.finished = time.time()
                with self._lock:
                    self._current = None
                log.info("job %s '%s' -> %s (%d ok / %d failed in %.1fs)",
                         job.id, job.label, job.state, job.ok, job.failed,
                         (job.finished or 0) - (job.started or 0))

    # ------------------------------------------------------------ submission

    def _register(self, job) -> Job:
        with self._lock:
            if job.coalesce_key:
                previous = self._pending_by_key.get(job.coalesce_key)
                if previous is not None and previous.state == "queued":
                    previous.state = "superseded"
                    log.debug("coalesced job %s into %s", previous.id, job.id)
                self._pending_by_key[job.coalesce_key] = job
            self._jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > self._history_len:
                stale = self._order.pop(0)
                stale_job = self._jobs.get(stale)
                if stale_job is not None and stale_job.state in ("queued", "running"):
                    self._order.append(stale)  # never evict live work
                    break
                self._jobs.pop(stale, None)
        if self._loop is None:
            raise RuntimeError("BLE worker not started")
        self._loop.call_soon_threadsafe(self._queue.put_nowait, job)
        return job

    def _frames_for(self, state: dict, address: str = None) -> list:
        """Every path to the radio builds its frames here.

        Keeping the full-brightness override at this single point means the UI,
        a saved scene and a raw API call cannot disagree about it. Channel order
        is per device, so frames are built per device rather than once per job.
        """
        if self.store.setting("force_full_brightness", True):
            state = dict(state, brightness=100)
        if address and state.get("color") is not None:
            device = self.store.device(address) or {}
            order = device.get("channels", "rgb")
            if order != "rgb":
                rgb = protocol.apply_channel_order(
                    protocol.parse_color(state["color"]), order)
                state = dict(state, color=protocol.format_color(rgb))
        frames = protocol.build_frames(state, self.store.setting("brightness_mode", "scale"))
        if address:
            escape = self._track_pattern(address, state)
            if escape:
                # The power-cycle escape already ends powered on, so don't send
                # the power-on frame build_frames put at the front as well.
                if frames and escape[-1] == frames[0] == protocol.power_frame(True):
                    frames = frames[1:]
                frames = escape + frames
        return frames

    def _track_pattern(self, address: str, state: dict) -> list:
        """Remember whether this device is animating, and help it stop.

        A unit running a built-in pattern often ignores a solid-colour frame and
        keeps animating, which reads as "the colour did not apply". Prepend the
        configured escape only on the transition out of a pattern, so a plain
        colour change stays one write.
        """
        wants_mode = state.get("mode") not in (None, "", "none")
        if wants_mode:
            self._in_pattern.add(address)
            return []
        if state.get("power") is False:
            self._in_pattern.discard(address)
            return []
        if address in self._in_pattern:
            self._in_pattern.discard(address)
            strategy = self.store.setting("exit_pattern", "none")
            escape = protocol.exit_pattern_frames(strategy)
            if escape:
                log.info("%s was running a pattern; prepending '%s' escape",
                         address, strategy)
            return escape
        return []

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None or any(
                job.state == "queued" for job in self._jobs.values())

    def note_manual(self):
        """Stamp the last hands-on command, on the monotonic clock.

        Wall clock would be wrong here: this Pi has no RTC and the time gets
        set from a phone at the sign, so time.time() can jump hours in either
        direction. A backward jump would extend the rotation hold by the size
        of the jump and stop the sign rotating for the rest of the night.
        """
        self.last_manual_at = time.monotonic()

    def submit_state(self, target: str, state: dict, label: str = None,
                     addresses=None, stagger=0.0) -> Job:
        """Apply one light state to every device behind ``target``.

        ``addresses`` overrides the lookup, so a caller that has already worked
        out a set of devices -- picking zones off the panel's sign preview, say
        -- gets one job for the lot rather than one job per light.
        """
        if addresses is None:
            addresses = self.store.resolve_target(target)
        items = [self._item(address, self._frames_for(state, address), state)
                 for address in addresses]
        label = label or ("%s -> %s" % (self.store.target_label(target), describe_state(state)))
        job = Job("apply", label, items, coalesce_key="target:" + (target or "all"),
                  stagger=stagger)
        log.info("queued %s: %d device(s), frames %s",
                 label, len(items), _describe_items(items))
        if stagger:
            log.info("  rolling: %.1fs between devices", stagger)
        return self._register(job)

    def submit_scene(self, scene: dict, stagger=None) -> Job:
        """Apply every step of a scene as one job.

        A device named by more than one step keeps only the last step that
        mentions it, so 'all -> dim red' followed by 'group:letters -> white'
        does what it reads like and never writes twice to one unit.

        Devices are then visited in config order, not in the order the steps
        happen to name them. Writes are serialised at ~2.5s each, so a scene
        change is a visible wipe across the sign, and which way that wipe
        travels is a fact about where the units are mounted -- not about
        whether someone wrote the 'letters' step before the 'cup' one. Sorting
        here cannot change what any device displays: the step that wins for a
        given device is settled in by_address above, independently of order.
        """
        by_address, wanted = {}, {}
        for step in scene.get("steps") or []:
            for address in self.store.resolve_target(step.get("target", "all")):
                by_address[address] = self._frames_for(step, address)
                wanted[address] = step
        order = [a for a in self.store.resolve_target("all") if a in by_address]
        # Anything a step reached that 'all' does not list (a device disabled
        # between the two calls) still gets written rather than dropped.
        order += [a for a in by_address if a not in set(order)]
        items = [self._item(address, by_address[address], wanted.get(address))
                 for address in order]
        if stagger is None:
            stagger = scene.get("stagger", 0.0)
        job = Job("apply", "scene: %s" % scene.get("name", "?"), items,
                  coalesce_key="scene", payload={"scene": scene.get("name")},
                  stagger=stagger)
        log.info("queued scene '%s': %d device(s), frames %s",
                 scene.get("name"), len(items), _describe_items(items))
        return self._register(job)

    def submit_test(self, address: str) -> Job:
        """Reachability probe: connect, blink green, restore off-ish white."""
        device = self.store.device(address)
        name = device["name"] if device else address
        frames = [
            protocol.power_frame(True),
            protocol.color_frame(0, 255, 0),
            protocol.color_frame(0, 0, 0),
            protocol.color_frame(0, 255, 0),
        ]
        job = Job("test", "test %s" % name,
                  [self._item(address, frames)], coalesce_key="test:" + address)
        return self._register(job)

    def submit_matrix(self, frames, label: str, coalesce_key: str = None,
                      payload: dict = None, manual: bool = False) -> Job:
        """Write already-encoded frames to the text panel.

        The panel is not an ELK-BLEDOM controller and nothing above knows how
        to build its frames -- ``matrix.py`` does that -- but it is on the same
        radio, so it queues here with everything else. A panel write landing in
        the middle of a twelve-device sweep would cost both.

        Returns None rather than raising when no panel is configured: the
        callers are a scheduler tick and an HTTP handler, and neither should
        have to guard every call.
        """
        matrix = self.store.matrix()
        address = matrix.get("address")
        if not address:
            log.debug("no matrix panel configured; dropping %s", label)
            return None
        driver = matrix_module.driver_for(matrix)
        char_uuid = driver.characteristic()
        if not char_uuid:
            log.warning("matrix panel %s has no write characteristic; "
                        "run matrix_probe.py to fingerprint it", address)
            return None
        frames = [bytes(f) for f in frames if f]
        if not frames:
            log.debug("nothing to send to the panel for %s", label)
            return None
        item = self._item(address, frames,
                          name=matrix.get("name") or "panel",
                          char_uuid=char_uuid,
                          frame_delay=matrix.get("frame_delay", 0.02),
                          # Every pixel matters here: one dropped write is one
                          # dark LED in the middle of a letter.
                          response=matrix.get("write_response", True),
                          batch=matrix.get("batch_writes", True))
        job = Job("matrix", label, [item],
                  coalesce_key=coalesce_key or "matrix",
                  payload=dict(payload or {}), manual=manual)
        log.info("queued %s: %d frame(s), %d bytes to %s via %s",
                 label, len(frames), sum(len(f) for f in frames), address, char_uuid)
        return self._register(job)

    def submit_scan(self, seconds: float = None) -> Job:
        seconds = float(seconds or self.store.setting("scan_seconds", 8.0))
        job = Job("scan", "scan %.0fs" % seconds, [], coalesce_key="scan",
                  payload={"seconds": seconds})
        return self._register(job)

    def _item(self, address, frames, state=None, name=None, char_uuid=None,
              frame_delay=None, response=None, batch=False):
        """One device's worth of work.

        ``name`` and ``char_uuid`` exist for devices that are not in the
        ELK-BLEDOM device list -- the text panel, which has its own protocol
        and a characteristic that must not be guessed at.
        """
        device = self.store.device(address)
        item = {
            "address": address,
            "name": name or (device["name"] if device else address),
            "status": "pending",
            "detail": "",
            "frames": [f.hex() for f in frames],
            # What this write is meant to make the unit show. Recorded on
            # success as the device's current appearance, so the panel can draw
            # the sign as it actually is rather than as a generic diagram.
            "want": _showing(state),
        }
        if char_uuid:
            item["char_uuid"] = char_uuid
        if frame_delay is not None:
            item["frame_delay"] = float(frame_delay)
        if response is not None:
            item["response"] = bool(response)
        if batch:
            item["batch"] = True
        return item

    # ---------------------------------------------------------------- status

    def status(self) -> dict:
        with self._lock:
            jobs = [self._jobs[jid].to_dict() for jid in self._order if jid in self._jobs]
            current = self._current.id if self._current else None
        queued = [job for job in jobs if job["state"] == "queued"]
        active = [job for job in jobs if job["state"] == "running"]
        return {
            "backend": "bleak" if HAVE_BLEAK else "simulated",
            "current": current,
            "queued": len(queued),
            "busy": bool(active),
            "jobs": list(reversed(jobs))[:self._history_len],
            "devices": {addr: dict(state) for addr, state in self.device_state.items()},
            "last_scan": self.last_scan,
        }

    def job(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def clear_queue(self) -> int:
        cleared = 0
        with self._lock:
            for job in self._jobs.values():
                if job.state == "queued":
                    job.state = "superseded"
                    cleared += 1
            self._pending_by_key.clear()
        log.info("cleared %d queued job(s)", cleared)
        return cleared

    def abort(self) -> dict:
        """Stop everything now: the running job and anything behind it.

        Marking the running job superseded makes _do_writes skip its remaining
        devices, but the device being written to at this instant would still
        run out its retries -- 36s at the defaults, and the whole reason for
        wanting to stop is usually that a device is timing out. So the in-flight
        write is cancelled too.
        """
        cleared = self.clear_queue()
        stopped = None
        with self._lock:
            current = self._current
            if current is not None and current.state == "running":
                current.state = "superseded"
                stopped = current.label
            task = self._inflight
        if task is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(_cancel, task)
        log.warning("aborted: %s%s",
                    ("running job %r" % stopped) if stopped else "nothing running",
                    (", %d queued" % cleared) if cleared else "")
        return {"stopped": stopped, "cleared": cleared}

    def _note_device(self, address, ok, detail="", char_uuid=None, elapsed=None):
        state = self.device_state.setdefault(address, {
            "reachable": None, "last_ok": None, "last_error": "",
            "last_attempt": None, "consecutive_failures": 0, "char_uuid": None,
            "last_ms": None,
        })
        state["last_attempt"] = time.time()
        if elapsed is not None:
            state["last_ms"] = int(elapsed * 1000)
        if ok:
            state["reachable"] = True
            state["last_ok"] = time.time()
            state["consecutive_failures"] = 0
            state["last_error"] = ""
            if char_uuid:
                state["char_uuid"] = char_uuid
        else:
            state["reachable"] = False
            state["consecutive_failures"] += 1
            state["last_error"] = detail

    # ------------------------------------------------------------- execution

    def _cooldown_remaining(self, address, settings) -> float:
        """Seconds left before a repeatedly-failing device is worth trying again.

        Without this, one unreachable controller costs attempts x connect_timeout
        on every single sweep -- measured at ~60s for one unit, roughly half the
        wall clock of a twelve-device scene. Skipping it outright keeps the other
        eleven fast, and it rejoins the moment a probe succeeds.
        """
        threshold = int(settings.get("cooldown_after", 2))
        if threshold <= 0:
            return 0.0
        state = self.device_state.get(address)
        if not state or state.get("consecutive_failures", 0) < threshold:
            return 0.0
        last = state.get("last_attempt") or 0
        return max(0.0, float(settings.get("failure_cooldown", 180.0)) - (time.time() - last))

    async def _do_writes(self, job):
        settings = self.store.settings
        gap = float(settings.get("inter_device_delay", 0.35))
        for item in job.items:
            if job.state == "superseded":
                item["status"] = "skipped"
                continue
            remaining = self._cooldown_remaining(item["address"], settings)
            # Cooling off is for sweeps: skipping one dead controller is what
            # keeps the other eleven fast. A job with one device in it has
            # nothing to keep fast, so if a person asked for it, it goes out
            # and comes back with an error -- otherwise a message someone just
            # typed vanishes with no reason given, for three minutes, on one
            # timeout. _is_known_bad still cuts it to a single attempt, so a
            # dead panel costs one connect rather than the full retry budget.
            # A timer's own retries stay skipped: a dead panel asked for a
            # message every twenty seconds would hold the radio the twelve
            # controllers need.
            if remaining > 0 and not (job.manual and len(job.items) == 1):
                fails = self.device_state[item["address"]]["consecutive_failures"]
                item["status"] = "skipped"
                item["detail"] = ("unreachable %dx, skipping for another %ds"
                                  % (fails, int(remaining)))
                continue
            item["status"] = "working"
            frames = [bytes.fromhex(h) for h in item["frames"]]
            started = time.time()
            ok, detail, char_uuid, phases = await self._write_device(
                item["address"], frames, settings,
                probing=self._is_known_bad(item["address"], settings),
                char_override=item.get("char_uuid"),
                frame_delay=item.get("frame_delay"),
                response=item.get("response"),
                batch=item.get("batch", False))
            elapsed = time.time() - started
            item["status"] = "ok" if ok else "failed"
            item["detail"] = detail
            item["ms"] = int(elapsed * 1000)
            item["phases"] = phases
            self._note_device(item["address"], ok, detail, char_uuid, elapsed)
            if ok and item.get("want"):
                # Only on success: a unit that did not answer is still showing
                # whatever it was showing before.
                self.device_state.setdefault(item["address"], {})["showing"] = \
                    dict(item["want"])
            if not ok:
                # One dead unit never blocks the rest: log it and move on.
                log.warning("%s (%s) unreachable: %s", item["name"], item["address"], detail)
            # Breathe between devices so the shared radio can serve the AP,
            # and hold longer still when the job is rolling on purpose.
            await asyncio.sleep(max(gap, job.stagger))
        _log_phase_summary(job, gap)
        if job.stagger:
            log.info("rolled across %d device(s) with a %.1fs stagger",
                     len(job.items), job.stagger)
        skipped = [item for item in job.items if item["status"] == "skipped"]
        if skipped:
            log.info("skipped %d device(s) still in cooldown: %s",
                     len(skipped), ", ".join(item["name"] for item in skipped))
            # A job where everything was skipped ends as "failed" with nothing
            # failed in it, which reads as the sign ignoring you for no reason.
            # Give the job the reason so the UI can show it.
            if not job.error and len(skipped) == len(job.items):
                job.error = skipped[0]["detail"] or "every device is in cooldown"

    def _is_known_bad(self, address, settings) -> bool:
        state = self.device_state.get(address)
        threshold = int(settings.get("cooldown_after", 2))
        return bool(state and threshold > 0
                    and state.get("consecutive_failures", 0) >= threshold)

    async def _write_device(self, address, frames, settings, probing=False,
                            char_override=None, frame_delay=None, response=None,
                            batch=False):
        # A device that just came off cooldown gets one cheap probe rather than
        # the full retry budget: if it is still dead, that is 12s wasted, not 60.
        attempts = 1 if probing else int(settings.get("attempts", 3))
        backoff = float(settings.get("retry_backoff", 0.8))
        last_error = "no attempt made"
        for attempt in range(1, attempts + 1):
            task = None
            try:
                phases = {}
                # Held on the worker so abort() can cancel it. A bare await
                # cannot be reached from another thread.
                task = asyncio.ensure_future(
                    self._connect_and_write(address, frames, settings, phases,
                                            char_override=char_override,
                                            frame_delay=frame_delay,
                                            response=response, batch=batch))
                self._inflight = task
                char_uuid = await asyncio.wait_for(
                    task, timeout=float(settings.get("connect_timeout", 12.0)) + 15.0)
                phases["attempts"] = attempt
                return True, "ok on attempt %d" % attempt, char_uuid, phases
            except asyncio.CancelledError:
                # Cancelled on purpose. Not an error, and not something to
                # retry -- CancelledError is a BaseException, so without this
                # it would escape and take the worker loop with it.
                return False, "stopped", None, {}
            except asyncio.TimeoutError:
                last_error = "timeout (attempt %d/%d)" % (attempt, attempts)
            except Exception as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
            finally:
                self._inflight = None
            log.debug("write to %s failed: %s", address, last_error)
            if attempt < attempts:
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))
        return False, last_error, None, {}

    async def _connect_and_write(self, address, frames, settings, phases=None,
                                 char_override=None, frame_delay=None,
                                 response=None, batch=False):
        """Connect, write, disconnect -- timing each phase into ``phases``.

        The split matters: connect and discover are ATT round trips gated by the
        BLE connection interval, so a faster CPU barely touches them, while the
        D-Bus marshalling around them is pure Python and scales with the core.
        Without the breakdown, "would better hardware help?" is unanswerable.
        """
        phases = {} if phases is None else phases
        if not HAVE_BLEAK:
            return await self._fake_write(address, frames, settings, phases)

        connect_timeout = float(settings.get("connect_timeout", 12.0))
        if frame_delay is None:
            frame_delay = settings.get("inter_frame_delay", 0.06)
        frame_delay = float(frame_delay)
        write_timeout = float(settings.get("write_timeout", 6.0))
        device = self.store.device(address) or {}
        cached = char_override or device.get("char_uuid")

        client = BleakClient(address, timeout=connect_timeout)
        mark = time.monotonic()
        await asyncio.wait_for(client.connect(), timeout=connect_timeout)
        phases["connect"] = time.monotonic() - mark
        try:
            mark = time.monotonic()
            services = await get_services(client)
            if char_override:
                # The panel's characteristic is a fact about its protocol, not
                # a guess to be re-derived: pick_characteristic scores UUIDs for
                # ELK-BLEDOM controllers and would happily choose a different
                # writable one here.
                char_uuid, without_response = require_characteristic(
                    services, char_override, address)
            else:
                char_uuid, without_response = resolve_characteristic(services, cached)
            phases["discover"] = time.monotonic() - mark
            if char_uuid is None:
                raise RuntimeError("no writable characteristic on %s" % address)
            if not char_override and char_uuid != cached:
                self.store.remember_characteristic(address, char_uuid)
            # Unacknowledged writes are fire and forget: when the sender
            # outruns the device, packets are dropped with no error anywhere.
            # On the text panel that showed up as a few LEDs missing from each
            # message, different ones every time. An acknowledged write is
            # flow-controlled and cannot do that, so a caller that needs every
            # byte to land asks for one.
            acknowledged = (not without_response) if response is None else bool(response)
            if batch:
                frames = pack_frames(frames, attribute_mtu(client))
            mark = time.monotonic()
            for index, frame in enumerate(frames):
                await asyncio.wait_for(
                    client.write_gatt_char(char_uuid, frame, response=acknowledged),
                    timeout=write_timeout,
                )
                if index + 1 < len(frames):
                    await asyncio.sleep(frame_delay)
            phases["write"] = time.monotonic() - mark
            return char_uuid
        finally:
            mark = time.monotonic()
            await self._release(client, address, settings)
            phases["disconnect"] = time.monotonic() - mark

    async def _release(self, client, address, settings):
        """Disconnect, but don't sit through the whole teardown.

        A clean BlueZ disconnect measures ~2.4s on this hardware -- 40% of the
        time spent per controller -- and nothing downstream depends on it having
        finished. Start it, give it a moment, then move on and let it complete in
        the background while the next device is being connected.
        """
        task = asyncio.ensure_future(client.disconnect())
        self._pending_disconnects.add(task)

        def _finished(done_task):
            self._pending_disconnects.discard(done_task)
            if not done_task.cancelled() and done_task.exception() is not None:
                log.debug("disconnect from %s ended with %s",
                          address, done_task.exception())

        task.add_done_callback(_finished)
        wait = float(settings.get("disconnect_wait", 0.5))
        if wait <= 0:
            return
        done, _pending = await asyncio.wait({task}, timeout=wait)
        if not done:
            log.debug("%s still disconnecting after %.1fs; carrying on",
                      address, wait)

    async def _fake_write(self, address, frames, settings, phases=None):
        """Simulation backend so the UI can be developed off-Pi."""
        if phases is not None:
            phases.update({"connect": 0.25, "discover": 0.1,
                           "write": 0.05 * len(frames), "disconnect": 0.05})
        await asyncio.sleep(0.4 + 0.05 * len(frames))
        if address.upper().endswith("FF:FF"):
            raise RuntimeError("simulated unreachable device")
        # A panel text payload is dozens of chunks; log the shape, not the wall.
        if len(frames) > 6:
            log.info("[sim] %s <- %d frames, %d bytes (%s ...)", address, len(frames),
                     sum(len(f) for f in frames), frames[0].hex(" "))
        else:
            log.info("[sim] %s <- %s", address, protocol.describe_frames(frames))
        return protocol.PREFERRED_CHAR_UUIDS[0]

    async def _do_scan(self, job):
        seconds = float(job.payload.get("seconds", 8.0))
        found = []
        if not HAVE_BLEAK:
            await asyncio.sleep(min(seconds, 2.0))
            found = [{"address": "AA:BB:CC:00:00:%02X" % i, "name": "ELK-BLEDOM ", "rssi": -60 - i}
                     for i in range(3)]
        else:
            found = await scan_devices(seconds)
        known = {device["address"] for device in self.store.devices()}
        for entry in found:
            entry["known"] = entry["address"] in known
            entry["is_elk"] = protocol.looks_like_elk(entry.get("name"))
            # Worth offering as the text panel? Deliberately loose -- an extra
            # row in the pairing list is cheaper than a panel that never shows up.
            entry["is_panel"] = (not entry["is_elk"]
                                 and matrix_module.looks_like_panel(entry.get("name")))
            entry["family"] = matrix_module.identify(entry.get("name")) or ""
        found.sort(key=lambda e: (not e["is_elk"], not e.get("is_panel"),
                                  -(e.get("rssi") or -999)))
        self.last_scan = {"at": time.time(), "devices": found}
        job.result = found
        log.info("scan finished: %d device(s), %d look like ELK-BLEDOM",
                 len(found), sum(1 for e in found if e["is_elk"]))


def attribute_mtu(client, fallback=23) -> int:
    """The negotiated ATT MTU, or a safe assumption.

    bleak has exposed this under two names across versions, and on some
    backends not at all. Guessing high would silently truncate every write, so
    an unknown MTU means the 23-byte minimum every device must support.
    """
    for name in ("mtu_size", "mtu"):
        value = getattr(client, name, None)
        if isinstance(value, int) and value >= 23:
            return value
    return fallback


def pack_frames(frames, mtu: int) -> list:
    """Combine whole frames into as few writes as the MTU allows.

    Only ever whole frames: this protocol is length-prefixed, so a device
    reading a stream can take several packets from one write -- but a packet
    split across two writes would be read as garbage. Three bytes of every MTU
    go to the ATT header.

    Worth doing because an acknowledged write costs a round trip regardless of
    how full it is. Drawing text a pixel at a time is hundreds of ten-byte
    packets, and at the 23-byte minimum that is already two per write; a device
    that negotiates a larger MTU gets proportionally faster for free.
    """
    room = max(20, int(mtu) - 3)
    packed, current = [], bytearray()
    for frame in frames:
        if len(frame) > room:
            if current:
                packed.append(bytes(current))
                current = bytearray()
            packed.append(bytes(frame))       # too big to combine; send alone
            continue
        if len(current) + len(frame) > room:
            packed.append(bytes(current))
            current = bytearray()
        current += frame
    if current:
        packed.append(bytes(current))
    return packed


def _cancel(task):
    if not task.done():
        task.cancel()


async def get_services(client):
    """Service collection for a connected client, across bleak versions.

    bleak >= 0.21 exposes a ``services`` property; older releases only have the
    ``get_services()`` coroutine.  Returns a plain list of services.
    """
    services = getattr(client, "services", None)
    if services is None:
        getter = getattr(client, "get_services", None)
        if getter is None:
            raise RuntimeError("this bleak version exposes no service collection")
        services = await getter()
    return list(services)


def require_characteristic(collection, wanted, address=""):
    """Use exactly this characteristic, or say why it cannot be used.

    Falling back to "some other writable characteristic" for a device whose
    protocol we know would write a text payload into whatever the panel
    happens to expose, which is worse than failing.
    """
    wanted = str(wanted).lower()
    for service in collection:
        for char in service.characteristics:
            if char.uuid.lower() == wanted:
                props = set(char.properties or ())
                if not ("write" in props or "write-without-response" in props):
                    raise RuntimeError("%s on %s is not writable (%s)"
                                       % (wanted, address or "device",
                                          ", ".join(sorted(props)) or "no properties"))
                return char.uuid.lower(), "write-without-response" in props
    raise RuntimeError("%s does not expose %s" % (address or "device", wanted))


def resolve_characteristic(collection, cached=None):
    """Pick the write characteristic, honouring a cached UUID when still valid.

    ``collection`` is the list returned by ``get_services``.
    """
    if cached:
        cached = cached.lower()
        for service in collection:
            for char in service.characteristics:
                if char.uuid.lower() == cached:
                    props = set(char.properties or ())
                    if "write" in props or "write-without-response" in props:
                        return char.uuid.lower(), "write-without-response" in props
    return protocol.pick_characteristic(collection)


async def scan_devices(seconds: float = 8.0) -> list:
    """BLE discovery.  Returns [{address, name, rssi}] for everything seen.

    ``BleakScanner.discover`` has returned three different shapes across bleak
    versions (list of devices, list of pairs, dict of address -> pair) and
    ``return_adv`` does not exist on the oldest or, possibly, the newest.  Ask
    for advertisement data, fall back if the keyword is rejected, and normalise
    whatever comes back.
    """
    if not HAVE_BLEAK:
        raise RuntimeError("bleak not installed")
    try:
        discovered = await BleakScanner.discover(timeout=seconds, return_adv=True)
    except TypeError:
        discovered = await BleakScanner.discover(timeout=seconds)
    return normalize_scan(discovered)


def normalize_scan(discovered) -> list:
    entries = discovered.values() if isinstance(discovered, dict) else discovered
    results = []
    for entry in entries:
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            device, adv = entry
        else:
            device, adv = entry, None
        address = getattr(device, "address", None)
        if not address:
            continue
        name = getattr(adv, "local_name", None) or getattr(device, "name", None) or ""
        rssi = getattr(adv, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)
        results.append({"address": address.upper(), "name": name.strip(), "rssi": rssi})
    return results


def _showing(state):
    """The visible part of a light state: what a person would see."""
    if not state:
        return None
    if state.get("power") is False:
        return {"power": False}
    return {"power": True,
            "color": state.get("color"),
            "mode": state.get("mode"),
            "speed": state.get("speed")}


PHASE_ORDER = ("connect", "discover", "write", "disconnect")


def _log_phase_summary(job, gap: float):
    """Where the wall clock went, averaged over the devices that succeeded.

    Reports measured-vs-actual, because the phases do not cover everything: a
    failed device's retries, and the Python and D-Bus work between phases, land
    in the gap between the two numbers. Quoting only the accounted figure would
    understate what a sweep really costs.
    """
    elapsed = time.time() - (job.started or time.time())

    # A device that fails burns attempts x connect_timeout and drags the whole
    # sweep with it. Say so in seconds: otherwise the cost only surfaces as
    # inflated "unmeasured" time spread over every device, which reads like a
    # measurement problem rather than one broken controller. This runs before
    # the phase averages bail out, because a sweep where nothing succeeded is
    # exactly when you most need to be told which units ate the clock.
    failures = [item for item in job.items if item.get("status") == "failed"]
    if failures:
        wasted = sum(item.get("ms", 0) for item in failures) / 1000.0
        log.warning("%d failing device(s) cost %.0fs of this %.0fs sweep: %s",
                    len(failures), wasted, elapsed,
                    ", ".join("%s %.0fs" % (item.get("name") or item.get("address", "?"),
                                            item.get("ms", 0) / 1000.0)
                              for item in failures))

    samples = [item.get("phases") or {} for item in job.items
               if item.get("status") == "ok" and item.get("phases")]
    if not samples:
        return
    totals = {name: sum(s.get(name, 0.0) for s in samples) / len(samples)
              for name in PHASE_ORDER}
    accounted = sum(totals.values()) + gap
    attempted = [item for item in job.items if item.get("status") in ("ok", "failed")]
    actual = elapsed / len(attempted) if attempted else 0.0
    log.info("phase averages over %d device(s): %s | inter-device gap %.2fs "
             "| %.2fs/device accounted, %.2fs actual (%.2fs unmeasured)",
             len(samples),
             "  ".join("%s %.2fs" % (name, totals[name]) for name in PHASE_ORDER),
             gap, accounted, actual, max(0.0, actual - accounted))

    # Only the attempt that worked contributes phase timings, so a device that
    # failed once and succeeded on the retry looks fast while having cost a
    # whole connect timeout. Without naming them, "12 ok" hides the problem.
    retried = [(item["name"], (item.get("phases") or {}).get("attempts", 1))
               for item in job.items
               if (item.get("phases") or {}).get("attempts", 1) > 1]
    if retried:
        log.warning("%d device(s) needed a retry (most of the unmeasured time): %s",
                    len(retried),
                    ", ".join("%s on attempt %d" % pair for pair in retried))


def _spaced(frames) -> str:
    """Byte-spaced hex, so a log line can be compared against the frame table."""
    return " | ".join(bytes.fromhex(f).hex(" ") for f in frames)


def _describe_items(items) -> str:
    """Frames for the job, saying so when channel order makes them differ."""
    if not items:
        return "(none)"
    unique = {_spaced(item["frames"]) for item in items}
    if len(unique) == 1:
        return unique.pop()
    # A scene with several steps, or a device with its own channel order, both
    # land here -- so name the fact, not a guess at the cause.
    return "%s (+%d other frame set(s) in this job)" % (
        _spaced(items[0]["frames"]), len(unique) - 1)


def describe_state(state: dict) -> str:
    if state.get("power") is False:
        return "off"
    if state.get("mode"):
        return "mode %s @ %s%%" % (protocol.MODES.get(state["mode"], hex(state["mode"])),
                                   state.get("brightness", 100))
    return "%s @ %s%%" % (state.get("color", "#ffffff"), state.get("brightness", 100))
