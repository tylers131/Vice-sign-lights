"""Flask app: JSON API + the phone UI.

Every endpoint that touches BLE returns immediately with a job id.  The browser
polls ``/api/status`` (1 Hz) for queue depth and per-device progress, so a
40-second sweep across 16 controllers never blocks a request or freezes the UI.
"""

from __future__ import annotations

import logging
import socket
import time

from flask import Flask, jsonify, render_template, request

from . import protocol
from .ble import describe_state
from .config import normalize_address

log = logging.getLogger("vicelights.web")


def _json_error(message, code=400):
    return jsonify({"ok": False, "error": str(message)}), code


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _targets_from(store, body: dict):
    """Work out which devices a request means, and what to call them.

    ``target`` is the usual single string. ``targets`` is a list, so the panel
    can send an arbitrary selection of zones and still get one job -- which
    matters because the queue reads per job, and one tap should be one entry.
    """
    listed = body.get("targets")
    if not listed:
        target = body.get("target", "all")
        return target, store.target_label(target), store.resolve_target(target)
    if isinstance(listed, str):
        listed = [listed]
    seen, addresses = set(), []
    for one in listed:
        for address in store.resolve_target(one):
            if address not in seen:
                seen.add(address)
                addresses.append(address)
    names = [store.target_label(one) for one in listed]
    label = ", ".join(names) if len(names) <= 3 else "%d zones" % len(names)
    return ("+".join(str(x) for x in listed), label, addresses)


def _state_from(body: dict) -> dict:
    state = {}
    if "power" in body:
        state["power"] = bool(body["power"])
    if body.get("color"):
        state["color"] = protocol.format_color(protocol.parse_color(body["color"]))
    if body.get("brightness") is not None:
        state["brightness"] = protocol.clamp(body["brightness"], 0, 100)
    if body.get("mode") not in (None, "", "none"):
        state["mode"] = protocol.clamp(body["mode"], 0x80, 0x9D)
    if body.get("speed") is not None:
        state["speed"] = protocol.clamp(body["speed"], 0, 100)
    return state


def local_addresses() -> list:
    """Best-effort list of IPv4 addresses, so we can print the UI URL on boot."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in found:
                found.append(address)
    except Exception:
        pass
    for candidate in _addresses_from_ip_command():
        if candidate not in found:
            found.append(candidate)
    return [a for a in found if not a.startswith("127.")] or found


def _addresses_from_ip_command() -> list:
    import subprocess
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return []
    addresses = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            value = parts[parts.index("inet") + 1].split("/")[0]
            if not value.startswith("127."):
                addresses.append(value)
    return addresses


def create_app(store, worker, scheduler, timekeeper, log_buffer, log_path):
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    # ------------------------------------------------------------------ pages

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            modes=sorted(protocol.MODES.items()),
            version=app.config.get("VERSION", "1.0"),
        )

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "backend": worker.status()["backend"]})

    # ------------------------------------------------------------------ state

    @app.route("/api/state")
    def api_state():
        snapshot = store.snapshot()
        status = worker.status()
        devices = []
        for device in snapshot["devices"]:
            runtime = status["devices"].get(device["address"], {})
            devices.append(dict(device, **{
                "reachable": runtime.get("reachable"),
                "last_ok": runtime.get("last_ok"),
                "last_error": runtime.get("last_error", ""),
                "last_attempt": runtime.get("last_attempt"),
                "consecutive_failures": runtime.get("consecutive_failures", 0),
                "last_ms": runtime.get("last_ms"),
            }))
        return jsonify({
            "ok": True,
            "devices": devices,
            "groups": snapshot["groups"],
            "scenes": snapshot["scenes"],
            "schedules": snapshot["schedules"],
            "settings": snapshot["settings"],
            "timers": scheduler.timers(),
            "next_runs": scheduler.next_runs(),
            "rotation": scheduler.rotation.status(),
            "time": timekeeper.info(),
            "modes": protocol.mode_catalog(store.mode_names()),
            "queue": {"queued": status["queued"], "busy": status["busy"],
                      "current": status["current"]},
            "jobs": [_slim_job(job) for job in status["jobs"][:6]],
            "backend": status["backend"],
        })

    @app.route("/api/status")
    def api_status():
        status = worker.status()
        return jsonify({
            "ok": True,
            "backend": status["backend"],
            "queued": status["queued"],
            "busy": status["busy"],
            "current": status["current"],
            "jobs": [_slim_job(job) for job in status["jobs"][:8]],
            "devices": status["devices"],
            "clock_ok": timekeeper.clock_ok(),
            "now": timekeeper.info()["now"],
            "timers": scheduler.timers(),
            "rotation": scheduler.rotation.status(),
        })

    @app.route("/api/job/<job_id>")
    def api_job(job_id):
        job = worker.job(job_id)
        if not job:
            return _json_error("no such job", 404)
        return jsonify({"ok": True, "job": _slim_job(job)})

    # ------------------------------------------------------------------ apply

    @app.route("/api/apply", methods=["POST"])
    def api_apply():
        body = _body()
        target = body.get("target", "all")
        try:
            state = _state_from(body)
        except ValueError as exc:
            return _json_error(exc)
        if not state:
            return _json_error("nothing to apply")
        target, label, addresses = _targets_from(store, body)
        if not addresses:
            return _json_error("target '%s' matches no enabled device" % target)
        worker.note_manual()
        job = worker.submit_state(target, state,
                                  label="%s -> %s" % (label, describe_state(state)),
                                  addresses=addresses)
        return jsonify({"ok": True, "job": _slim_job(job.to_dict())})

    @app.route("/api/power", methods=["POST"])
    def api_power():
        body = _body()
        target, label, addresses = _targets_from(store, body)
        if not addresses:
            return _json_error("target '%s' matches no enabled device" % target)
        worker.note_manual()
        on = bool(body.get("on", True))
        job = worker.submit_state(target, {"power": on},
                                  label="%s -> %s" % (label, "on" if on else "off"),
                                  addresses=addresses)
        return jsonify({"ok": True, "job": _slim_job(job.to_dict())})

    @app.route("/api/scene/apply", methods=["POST"])
    def api_scene_apply():
        body = _body()
        scene = store.scene(body.get("scene") or body.get("name") or "")
        if not scene:
            return _json_error("unknown scene", 404)
        worker.note_manual()
        job = worker.submit_scene(scene)
        return jsonify({"ok": True, "job": _slim_job(job.to_dict())})

    @app.route("/api/queue/clear", methods=["POST"])
    def api_queue_clear():
        return jsonify({"ok": True, "cleared": worker.clear_queue()})

    # ---------------------------------------------------------------- devices

    @app.route("/api/devices", methods=["POST"])
    def api_device_upsert():
        try:
            device = store.upsert_device(_body())
        except ValueError as exc:
            return _json_error(exc)
        return jsonify({"ok": True, "device": device})

    @app.route("/api/devices/<address>", methods=["DELETE"])
    def api_device_delete(address):
        try:
            removed = store.delete_device(address)
        except ValueError as exc:
            return _json_error(exc)
        return jsonify({"ok": True, "removed": removed})

    @app.route("/api/devices/<address>/test", methods=["POST"])
    def api_device_test(address):
        try:
            address = normalize_address(address)
        except ValueError as exc:
            return _json_error(exc)
        job = worker.submit_test(address)
        return jsonify({"ok": True, "job": _slim_job(job.to_dict())})

    @app.route("/api/scan", methods=["POST"])
    def api_scan():
        job = worker.submit_scan(_body().get("seconds"))
        return jsonify({"ok": True, "job": _slim_job(job.to_dict())})

    @app.route("/api/scan/result")
    def api_scan_result():
        return jsonify({"ok": True, "scan": worker.last_scan})

    # ----------------------------------------------------------------- groups

    @app.route("/api/groups", methods=["POST"])
    def api_group_add():
        try:
            groups = store.add_group(_body().get("name"))
        except ValueError as exc:
            return _json_error(exc)
        return jsonify({"ok": True, "groups": groups})

    @app.route("/api/groups/<name>", methods=["DELETE"])
    def api_group_delete(name):
        return jsonify({"ok": True, "groups": store.delete_group(name)})

    # ----------------------------------------------------------------- scenes

    @app.route("/api/scenes", methods=["POST"])
    def api_scene_upsert():
        try:
            scene = store.upsert_scene(_body())
        except ValueError as exc:
            return _json_error(exc)
        return jsonify({"ok": True, "scene": scene})

    @app.route("/api/scenes/<scene_id>", methods=["DELETE"])
    def api_scene_delete(scene_id):
        return jsonify({"ok": True, "removed": store.delete_scene(scene_id)})

    # -------------------------------------------------------------- schedules

    @app.route("/api/schedules", methods=["POST"])
    def api_schedule_upsert():
        try:
            schedule = store.upsert_schedule(_body())
        except ValueError as exc:
            return _json_error(exc)
        return jsonify({"ok": True, "schedule": schedule})

    @app.route("/api/schedules/<schedule_id>", methods=["DELETE"])
    def api_schedule_delete(schedule_id):
        return jsonify({"ok": True, "removed": store.delete_schedule(schedule_id)})

    @app.route("/api/modes", methods=["GET", "POST"])
    def api_modes():
        """Record what a built-in pattern actually does on this hardware.

        The documented names describe some other firmware, so the useful ones
        come from someone standing in front of the sign watching it.
        """
        if request.method == "POST":
            body = _body()
            try:
                value = int(str(body.get("value")), 0)
            except (TypeError, ValueError):
                return _json_error("mode value required, e.g. 137 or '0x89'")
            if not protocol.MODE_MIN <= value <= protocol.MODE_MAX:
                return _json_error("mode must be between 0x%02x and 0x%02x"
                                   % (protocol.MODE_MIN, protocol.MODE_MAX))
            store.set_mode_name(value, body.get("name", ""))
        return jsonify({"ok": True, "modes": protocol.mode_catalog(store.mode_names())})

    @app.route("/api/rotation", methods=["GET", "POST"])
    def api_rotation():
        if request.method == "POST":
            body = _body()
            allowed = ("enabled", "playlist", "exclude", "interval_minutes",
                       "order", "avoid_repeat", "hold_after_manual_minutes")
            changes = {k: body[k] for k in allowed if k in body}
            was = store.rotation()["enabled"]
            try:
                store.update_rotation(changes)
            except Exception as exc:
                return _json_error(exc)
            # An interval change should take effect from now, not from whenever
            # the old one happened to be due.
            if "interval_minutes" in changes and store.rotation()["enabled"] and was:
                scheduler.rotation.reschedule()
        return jsonify({"ok": True, "rotation": scheduler.rotation.status()})

    @app.route("/api/rotation/next", methods=["POST"])
    def api_rotation_next():
        name = scheduler.rotation.play_next(force=True)
        if not name:
            return _json_error("nothing to play -- check the playlist")
        return jsonify({"ok": True, "scene": name,
                        "rotation": scheduler.rotation.status()})

    @app.route("/api/timers", methods=["POST"])
    def api_timer_add():
        body = _body()
        try:
            timer = scheduler.add_timer(body.get("scene"), body.get("minutes"))
        except (ValueError, TypeError) as exc:
            return _json_error(exc)
        return jsonify({"ok": True, "timer": timer})

    @app.route("/api/timers/<timer_id>", methods=["DELETE"])
    def api_timer_delete(timer_id):
        return jsonify({"ok": True, "removed": scheduler.cancel_timer(timer_id)})

    # ------------------------------------------------------------------- time

    @app.route("/api/time", methods=["GET", "POST"])
    def api_time():
        if request.method == "GET":
            return jsonify({"ok": True, "time": timekeeper.info()})
        body = _body()
        value = body.get("iso") or body.get("epoch")
        if value is None:
            return _json_error("send {iso: '2026-08-28T19:30:00'} or {epoch: 1234567890}")
        try:
            info = timekeeper.set_time(value, body.get("source", "web ui"))
        except Exception as exc:
            return _json_error(exc, 500)
        return jsonify({"ok": True, "time": info})

    # ----------------------------------------------------------- config / log

    @app.route("/api/config", methods=["GET", "PUT"])
    def api_config():
        if request.method == "GET":
            return jsonify({"ok": True, "config": store.snapshot()})
        body = _body()
        try:
            config = store.replace_all(body.get("config") if "config" in body else body)
        except Exception as exc:
            return _json_error(exc)
        return jsonify({"ok": True, "config": config})

    @app.route("/api/config/reload", methods=["POST"])
    def api_config_reload():
        store.load()
        return jsonify({"ok": True, "config": store.snapshot()})

    @app.route("/api/log")
    def api_log():
        try:
            count = int(request.args.get("n", 200))
        except ValueError:
            count = 200
        lines = list(log_buffer)[-max(1, min(count, 1000)):]
        return jsonify({"ok": True, "path": log_path, "lines": lines})

    return app


def _slim_job(job: dict) -> dict:
    """Strip the frame hex from job items -- the UI does not need it."""
    items = [{k: v for k, v in item.items() if k != "frames"} for item in job.get("items", [])]
    slim = dict(job)
    slim["items"] = items
    if slim.get("kind") == "scan" and slim.get("result"):
        slim["result"] = slim["result"][:40]
    elif slim.get("kind") != "scan":
        slim.pop("result", None)
    slim["age"] = round(time.time() - job.get("created", time.time()), 1)
    return slim
