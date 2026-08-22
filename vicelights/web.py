"""Flask app: JSON API + the phone UI.

Every endpoint that touches BLE returns immediately with a job id.  The browser
polls ``/api/status`` (1 Hz) for queue depth and per-device progress, so a
40-second sweep across 16 controllers never blocks a request or freezes the UI.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import threading
import time

from flask import Flask, jsonify, render_template, request

from . import matrix
from . import protocol
from .ble import describe_state
from .config import ConfigError, normalize_address

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


def _installed_from() -> str:
    try:
        with open("/opt/vice-sign-lights/INSTALLED_FROM", "r", encoding="utf-8") as h:
            return h.read().strip()[:60]
    except Exception:
        return ""


def _probe(command, timeout=4.0) -> str:
    """Run a small system command, or return "" if it is not on this box."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return done.stdout.strip()
    except Exception:
        return ""


def _uptime() -> str:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            seconds = float(handle.read().split()[0])
    except Exception:
        return "unknown"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


def _throttled():
    """The firmware's sticky under-voltage and throttling bits.

    They latch until reboot, so a brownout at 3am is still readable at
    breakfast -- the live bits would have cleared long before anyone walked
    over. Silent data corruption from a marginal supply is the failure this is
    here to catch, and it does not announce itself any other way.
    """
    raw = _probe(["vcgencmd", "get_throttled"])
    if "=" not in raw:
        return None, "not a Pi, or vcgencmd missing"
    try:
        bits = int(raw.split("=", 1)[1], 0)
    except ValueError:
        return None, raw
    if bits == 0:
        return True, "no under-voltage since boot"
    seen = []
    if bits & 0x1:
        seen.append("under-voltage NOW")
    if bits & 0x4:
        seen.append("throttled NOW")
    if bits & 0x10000:
        seen.append("under-voltage has happened")
    if bits & 0x40000:
        seen.append("throttling has happened")
    return False, ", ".join(seen) or ("bits %s" % raw)


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

    @app.route("/api/diagnostics")
    def api_diagnostics():
        """One screen's worth of "is this thing healthy", judged here.

        The verdict is decided server-side rather than in the panel, because
        "is this healthy" is a statement about the sign, not about pixels, and
        the phone should be able to show the same answers. Every row says the
        consequence, not just the fact.
        """
        status = worker.status()
        devices = status["devices"]
        down = [addr for addr, state in devices.items()
                if state.get("reachable") is False]
        rows = []

        def row(name, value, ok, note=""):
            rows.append({"name": name, "value": value, "ok": ok, "note": note})

        total = len(store.devices(enabled_only=True))
        row("Controllers",
            "%d of %d answering" % (total - len(down), total),
            not down,
            ", ".join(store.device(a)["name"] for a in down if store.device(a))
            or "all answering")

        backend = status["backend"]
        row("Bluetooth", backend, backend == "bleak",
            "real radio" if backend == "bleak"
            else "SIMULATED -- nothing is being sent to the lights")

        clock = timekeeper.info()
        row("Clock", clock["now"].replace("T", " "), clock["clock_ok"],
            "set from %s" % (clock["source"] or "unknown")
            if clock["clock_ok"] else "never set -- schedules will not run")

        addresses = local_addresses()
        row("Network", ", ".join(addresses) or "none", bool(addresses),
            "the panel talks to 127.0.0.1 regardless")

        healthy, note = _throttled()
        row("Pi power", "OK" if healthy else ("CHECK" if healthy is False else "n/a"),
            healthy is not False, note)

        try:
            usage = shutil.disk_usage("/")
            free = usage.free / 1e9
            row("Storage", "%.1f GB free" % free, free > 0.5,
                "logs are capped at 8 MB")
        except Exception as exc:
            row("Storage", "unknown", False, str(exc))

        row("Uptime", _uptime(), True, "since the last power-up")
        row("Version", app.config.get("VERSION", "?"), True, _installed_from())

        rotation = scheduler.rotation.status()
        row("Rotation", "on" if rotation["enabled"] else "off", True,
            "%d scenes, every %g min" % (len(rotation["scenes"]),
                                         rotation["interval_minutes"]))
        return jsonify({"ok": True, "rows": rows,
                        "down": [{"address": a,
                                  "name": (store.device(a) or {}).get("name", a),
                                  "error": devices[a].get("last_error", ""),
                                  "since": devices[a].get("last_attempt")}
                                 for a in down]})

    @app.route("/api/system", methods=["POST"])
    def api_system():
        """Shut down or reboot the Pi.

        Deliberate: the config is written continuously and pulling power
        mid-write is the usual way to corrupt an SD card -- which on this
        machine would take the mode audit and the device mapping with it.
        """
        action = (_body().get("action") or "").strip().lower()
        if action not in ("shutdown", "reboot"):
            return _json_error("action must be 'shutdown' or 'reboot'")
        if os.geteuid() != 0:
            return _json_error("not running as root; cannot %s" % action, 500)
        command = ["/sbin/shutdown", "-h" if action == "shutdown" else "-r", "now"]
        log.warning("%s requested from the UI", action)

        def go():
            # After a beat, so this request gets its reply out first -- without
            # it the caller sees a dropped connection and cannot tell whether
            # the machine is going down or the request simply failed.
            time.sleep(1.0)
            subprocess.run(command, capture_output=True)

        threading.Thread(target=go, daemon=True).start()
        return jsonify({"ok": True, "action": action,
                        "message": "shutting down" if action == "shutdown"
                        else "rebooting"})

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
            "panel": scheduler.panel.status(),
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
            "panel": _slim_panel(scheduler.panel.status()),
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
        roll = protocol.clamp(body.get("stagger") or 0, 0, 10)
        job = worker.submit_state(target, state,
                                  label="%s -> %s" % (label, describe_state(state)),
                                  addresses=addresses, stagger=roll)
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
        # A scene can carry its own roll; a caller may override it.
        override = body.get("stagger")
        job = worker.submit_scene(
            scene, stagger=protocol.clamp(override, 0, 10)
            if override is not None else None)
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

    # ------------------------------------------------------------ text panel

    @app.route("/api/matrix", methods=["GET", "POST"])
    def api_matrix():
        """Read or change the panel's settings.

        Everything the UI needs to draw the panel page comes back from GET,
        including which commands this panel's driver can actually build, so a
        family without a brightness command does not get a dead slider.
        """
        if request.method == "POST":
            body = _body()
            allowed = ("enabled", "address", "name", "family", "char_uuid",
                       "playlist", "width", "height", "default_dwell",
                       "chunk", "frame_delay", "commands",
                       "text_mode", "fill_background", "png_opt", "png_buffer",
                       "channels")
            changes = {k: body[k] for k in allowed if k in body}
            if not changes:
                return _json_error("nothing to change")
            # The store drops an unparseable address and carries on, which is
            # right when it is loading a config file it did not write -- but
            # here it would answer 200 to "pair this panel" and leave the panel
            # unpaired. Reject it where the caller can still be told.
            if changes.get("address"):
                try:
                    changes["address"] = normalize_address(changes["address"])
                except ValueError as exc:
                    return _json_error(exc)
            try:
                store.update_matrix(changes)
            except ConfigError as exc:
                return _json_error(exc)
            log.info("panel settings changed: %s", ", ".join(sorted(changes)))
        return jsonify({"ok": True, "matrix": scheduler.panel.status(),
                        "families": matrix.family_names()})

    @app.route("/api/matrix/send", methods=["POST"])
    def api_matrix_send():
        """Put a message on the panel now, whether or not it is in the queue."""
        body = _body()
        try:
            result = scheduler.panel.send(body, hold=body.get("hold"))
        except ValueError as exc:
            return _json_error(exc)
        if not result["sent"]:
            return _json_error(result["error"] or "the panel did not take it", 503)
        worker.note_manual()
        return jsonify({"ok": True, "message": result["message"]})

    @app.route("/api/matrix/next", methods=["POST"])
    def api_matrix_next():
        result = scheduler.panel.play_next(force=True)
        if not result["sent"]:
            return _json_error(result["error"] or "nothing to play", 409)
        return jsonify({"ok": True, "message": result["message"]})

    @app.route("/api/matrix/clear", methods=["POST"])
    def api_matrix_clear():
        if not scheduler.panel.clear():
            return _json_error("this panel has no clear command", 503)
        return jsonify({"ok": True})

    @app.route("/api/matrix/power", methods=["POST"])
    def api_matrix_power():
        on = bool(_body().get("on", True))
        if not scheduler.panel.power(on):
            return _json_error("this panel has no power command", 503)
        return jsonify({"ok": True, "on": on})

    @app.route("/api/matrix/brightness", methods=["POST"])
    def api_matrix_brightness():
        try:
            percent = int(_body().get("percent", 100))
        except (TypeError, ValueError):
            return _json_error("brightness must be a number")
        if not 0 <= percent <= 100:
            return _json_error("brightness must be 0-100")
        if not scheduler.panel.brightness(percent):
            return _json_error("this panel has no brightness command", 503)
        return jsonify({"ok": True, "percent": percent})

    # -- the queue itself

    @app.route("/api/matrix/messages", methods=["GET", "POST"])
    def api_matrix_messages():
        if request.method == "POST":
            try:
                message = store.upsert_message(_body())
            except (ConfigError, ValueError) as exc:
                return _json_error(exc)
            return jsonify({"ok": True, "message": message,
                            "messages": store.messages()})
        return jsonify({"ok": True, "messages": store.messages()})

    @app.route("/api/matrix/messages/<message_id>", methods=["DELETE"])
    def api_matrix_message_delete(message_id):
        if not store.delete_message(message_id):
            return _json_error("no such message", 404)
        return jsonify({"ok": True, "messages": store.messages()})

    @app.route("/api/matrix/messages/<message_id>/send", methods=["POST"])
    def api_matrix_message_send(message_id):
        message = store.message(message_id)
        if not message:
            return _json_error("no such message", 404)
        result = scheduler.panel.send(message)
        if not result["sent"]:
            return _json_error(result["error"] or "the panel did not take it", 503)
        worker.note_manual()
        return jsonify({"ok": True, "message": result["message"]})

    @app.route("/api/matrix/messages/order", methods=["POST"])
    def api_matrix_message_order():
        ids = _body().get("ids")
        if not isinstance(ids, list):
            return _json_error("send an 'ids' list in the order you want")
        return jsonify({"ok": True, "ids": store.reorder_messages(ids),
                        "messages": store.messages()})

    @app.route("/api/matrix/preview")
    def api_matrix_preview():
        """What a message will look like, as pixels, without the panel.

        The font lives on the Pi, so this is the only honest preview -- and it
        means a message can be checked from the phone before it goes up on a
        sign several people are looking at.
        """
        text = request.args.get("text", "")[:matrix.MAX_TEXT]
        height = store.matrix().get("height", 16)
        return jsonify({
            "ok": True,
            "text": text,
            "width": matrix.text_width(text),
            "height": matrix.FONT_HEIGHT,
            "fits": matrix.text_width(text) <= store.matrix().get("width", 32),
            "rows": matrix.render_bitmap(text, height=matrix.FONT_HEIGHT),
            "ascii": matrix.preview(text),
            "panel": {"width": store.matrix().get("width", 32), "height": height},
        })

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


def _slim_panel(panel: dict) -> dict:
    """The panel status without the whole queue.

    /api/status is polled every couple of seconds by both the phone and the
    touch panel; shipping forty messages each time to show one line of "now
    showing" is forty times the bytes for none of the information.
    """
    keep = ("enabled", "configured", "name", "address", "playlist", "queued",
            "current", "next_in", "last_error", "brightness")
    return {key: panel.get(key) for key in keep}


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
