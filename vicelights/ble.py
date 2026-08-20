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
                 "finished", "state", "items", "result", "error", "payload")

    def __init__(self, kind, label, items=None, coalesce_key=None, payload=None):
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

    def _frames_for(self, state: dict) -> list:
        """Every path to the radio builds its frames here.

        Keeping the full-brightness override at this single point means the UI,
        a saved scene and a raw API call cannot disagree about it.
        """
        if self.store.setting("force_full_brightness", True):
            state = dict(state, brightness=100)
        return protocol.build_frames(state, self.store.setting("brightness_mode", "scale"))

    def submit_state(self, target: str, state: dict, label: str = None) -> Job:
        """Apply one light state to every device behind ``target``."""
        addresses = self.store.resolve_target(target)
        frames = self._frames_for(state)
        items = [self._item(address, frames) for address in addresses]
        label = label or ("%s -> %s" % (self.store.target_label(target), _describe(state)))
        job = Job("apply", label, items, coalesce_key="target:" + (target or "all"))
        log.info("queued %s: %d device(s), frames %s",
                 label, len(items), protocol.describe_frames(frames))
        return self._register(job)

    def submit_scene(self, scene: dict) -> Job:
        """Apply every step of a scene as one job.

        A device named by more than one step keeps only the last step that
        mentions it, so 'all -> dim red' followed by 'group:letters -> white'
        does what it reads like and never writes twice to one unit.
        """
        by_address = {}
        order = []
        for step in scene.get("steps") or []:
            frames = self._frames_for(step)
            for address in self.store.resolve_target(step.get("target", "all")):
                if address not in by_address:
                    order.append(address)
                by_address[address] = frames
        items = [self._item(address, by_address[address]) for address in order]
        job = Job("apply", "scene: %s" % scene.get("name", "?"), items,
                  coalesce_key="scene", payload={"scene": scene.get("name")})
        log.info("queued scene '%s': %d device(s)", scene.get("name"), len(items))
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

    def submit_scan(self, seconds: float = None) -> Job:
        seconds = float(seconds or self.store.setting("scan_seconds", 8.0))
        job = Job("scan", "scan %.0fs" % seconds, [], coalesce_key="scan",
                  payload={"seconds": seconds})
        return self._register(job)

    def _item(self, address, frames):
        device = self.store.device(address)
        return {
            "address": address,
            "name": device["name"] if device else address,
            "status": "pending",
            "detail": "",
            "frames": [f.hex() for f in frames],
        }

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

    async def _do_writes(self, job):
        settings = self.store.settings
        gap = float(settings.get("inter_device_delay", 0.35))
        for item in job.items:
            if job.state == "superseded":
                item["status"] = "skipped"
                continue
            item["status"] = "working"
            frames = [bytes.fromhex(h) for h in item["frames"]]
            started = time.time()
            ok, detail, char_uuid = await self._write_device(item["address"], frames, settings)
            elapsed = time.time() - started
            item["status"] = "ok" if ok else "failed"
            item["detail"] = detail
            item["ms"] = int(elapsed * 1000)
            self._note_device(item["address"], ok, detail, char_uuid, elapsed)
            if not ok:
                # One dead unit never blocks the rest: log it and move on.
                log.warning("%s (%s) unreachable: %s", item["name"], item["address"], detail)
            # Breathe between devices so the shared radio can serve the AP.
            await asyncio.sleep(gap)

    async def _write_device(self, address, frames, settings):
        attempts = int(settings.get("attempts", 3))
        backoff = float(settings.get("retry_backoff", 0.8))
        last_error = "no attempt made"
        for attempt in range(1, attempts + 1):
            try:
                char_uuid = await asyncio.wait_for(
                    self._connect_and_write(address, frames, settings),
                    timeout=float(settings.get("connect_timeout", 12.0)) + 15.0,
                )
                return True, "ok on attempt %d" % attempt, char_uuid
            except asyncio.TimeoutError:
                last_error = "timeout (attempt %d/%d)" % (attempt, attempts)
            except Exception as exc:
                last_error = "%s: %s" % (type(exc).__name__, exc)
            log.debug("write to %s failed: %s", address, last_error)
            if attempt < attempts:
                await asyncio.sleep(backoff * (2 ** (attempt - 1)))
        return False, last_error, None

    async def _connect_and_write(self, address, frames, settings):
        if not HAVE_BLEAK:
            return await self._fake_write(address, frames, settings)

        connect_timeout = float(settings.get("connect_timeout", 12.0))
        frame_delay = float(settings.get("inter_frame_delay", 0.06))
        write_timeout = float(settings.get("write_timeout", 6.0))
        device = self.store.device(address) or {}
        cached = device.get("char_uuid")

        client = BleakClient(address, timeout=connect_timeout)
        await asyncio.wait_for(client.connect(), timeout=connect_timeout)
        try:
            char_uuid, without_response = resolve_characteristic(
                await get_services(client), cached)
            if char_uuid is None:
                raise RuntimeError("no writable characteristic on %s" % address)
            if char_uuid != cached:
                self.store.remember_characteristic(address, char_uuid)
            for index, frame in enumerate(frames):
                await asyncio.wait_for(
                    client.write_gatt_char(char_uuid, frame, response=not without_response),
                    timeout=write_timeout,
                )
                if index + 1 < len(frames):
                    await asyncio.sleep(frame_delay)
            return char_uuid
        finally:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=8.0)
            except Exception:
                log.debug("disconnect from %s did not complete cleanly", address)

    async def _fake_write(self, address, frames, settings):
        """Simulation backend so the UI can be developed off-Pi."""
        await asyncio.sleep(0.4 + 0.05 * len(frames))
        if address.upper().endswith("FF:FF"):
            raise RuntimeError("simulated unreachable device")
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
        found.sort(key=lambda e: (not e["is_elk"], -(e.get("rssi") or -999)))
        self.last_scan = {"at": time.time(), "devices": found}
        job.result = found
        log.info("scan finished: %d device(s), %d look like ELK-BLEDOM",
                 len(found), sum(1 for e in found if e["is_elk"]))


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


def _describe(state: dict) -> str:
    if state.get("power") is False:
        return "off"
    if state.get("mode"):
        return "mode %s @ %s%%" % (protocol.MODES.get(state["mode"], hex(state["mode"])),
                                   state.get("brightness", 100))
    return "%s @ %s%%" % (state.get("color", "#ffffff"), state.get("brightness", 100))
