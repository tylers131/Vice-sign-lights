"""Entry point: wire the pieces together and serve.

    python3 -m vicelights --config /etc/vice-lights/config.json
"""

from __future__ import annotations

import argparse
import collections
import logging
import logging.handlers
import os
import signal
import sys

from .ble import BleWorker
from .config import ConfigStore
from .scheduler import Scheduler
from .timekeeper import TimeKeeper
from .web import create_app, local_addresses

VERSION = "1.0"

DEFAULT_CONFIG = os.environ.get("VICELIGHTS_CONFIG", "/etc/vice-lights/config.json")
DEFAULT_LOG = os.environ.get("VICELIGHTS_LOG", "/var/log/vice-lights.log")
DEFAULT_STATE = os.environ.get("VICELIGHTS_STATE", "/var/lib/vice-lights/lastknown-time")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-20s %(message)s"


class RingBufferHandler(logging.Handler):
    """Keeps the tail of the log in memory so the UI can show it."""

    def __init__(self, capacity=800):
        super().__init__()
        self.buffer = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass


def setup_logging(path, level="INFO"):
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    ring = RingBufferHandler()
    ring.setFormatter(formatter)
    root.addHandler(ring)

    resolved = path
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except Exception as exc:
        resolved = "(file logging disabled: %s)" % exc
        root.warning("could not open log file %s: %s", path, exc)

    # bleak/dbus are chatty at DEBUG and this is a 1GHz single core.
    logging.getLogger("bleak").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return ring.buffer, resolved


def _installed_from() -> str:
    """The revision stamped by install.sh / update.sh, if there is one."""
    stamp = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "INSTALLED_FROM")
    try:
        with open(stamp, "r", encoding="utf-8") as handle:
            text = handle.read().strip()
        return " (installed from %s)" % text if text else ""
    except Exception:
        return ""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="vicelights",
                                     description="ELK-BLEDOM sign controller")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    parser.add_argument("--log", default=DEFAULT_LOG, help="path to log file")
    parser.add_argument("--state", default=DEFAULT_STATE, help="path to clock state file")
    parser.add_argument("--host", default=None, help="bind address (default from config)")
    parser.add_argument("--port", type=int, default=None, help="port (default from config)")
    parser.add_argument("--no-scheduler", action="store_true", help="do not run schedules")
    parser.add_argument("--version", action="version", version="vicelights " + VERSION)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    store = ConfigStore(args.config)
    log_buffer, log_path = setup_logging(args.log, store.setting("log_level", "INFO"))
    log = logging.getLogger("vicelights")
    log.info("vice-sign-lights %s starting (config %s)", VERSION, store.path)
    # Which tree is actually running? The service runs from its installed copy,
    # so a `git pull` in a checkout elsewhere changes nothing here -- say so
    # plainly rather than leaving it to be inferred from missing log lines.
    log.info("running from %s%s", os.path.dirname(os.path.abspath(__file__)),
             _installed_from())

    timekeeper = TimeKeeper(args.state)
    timekeeper.start()
    if not timekeeper.clock_ok():
        log.warning("CLOCK NOT SET -- wall-clock schedules are paused. "
                    "Set the time from the web UI, or use relative timers.")

    worker = BleWorker(store)
    worker.start()

    scheduler = Scheduler(store, worker, timekeeper)
    if not args.no_scheduler:
        scheduler.start()

    boot_scene_name = store.setting("apply_on_boot", "")
    if boot_scene_name:
        scene = store.scene(boot_scene_name)
        if scene:
            log.info("applying boot scene '%s'", scene["name"])
            worker.submit_scene(scene)
            # Count it as this interval's rotation pick, or the Pi does two full
            # sweeps back to back every time it boots.
            scheduler.rotation.note_played(scene["name"])
        else:
            log.error("apply_on_boot names a missing scene: %s", boot_scene_name)

    app = create_app(store, worker, scheduler, timekeeper, log_buffer, log_path)
    app.config["VERSION"] = VERSION

    host = args.host or store.setting("host", "0.0.0.0")
    port = int(args.port or store.setting("port", 80))

    for address in local_addresses():
        log.info("web UI: http://%s%s", address, "" if port == 80 else ":%d" % port)
    log.info("%d device(s), %d group(s), %d scene(s), %d schedule(s) loaded",
             len(store.devices()), len(store.group_names()),
             len(store.scenes()), len(store.schedules()))
    rotation = store.rotation()
    if rotation["enabled"]:
        log.info("rotation on: %d scene(s), %s, every %.0f min",
                 len(store.rotation_scenes()), rotation["order"],
                 rotation["interval_minutes"])

    def shutdown(signum, _frame):
        log.info("signal %s: shutting down", signum)
        scheduler.stop()
        timekeeper.stop()
        worker.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    serve(app, host, port, log)
    return 0


def serve(app, host, port, log):
    """Prefer waitress (pure Python, steady under a slow single core)."""
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        log.warning("waitress not installed; using the Flask dev server")
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
        return
    log.info("serving with waitress on %s:%d", host, port)
    # Few threads on purpose: the Zero W has one core and the real work is
    # serialized in the BLE thread anyway.
    waitress_serve(app, host=host, port=port, threads=4, connection_limit=40,
                   channel_timeout=60, ident="vice-sign-lights")


if __name__ == "__main__":
    raise SystemExit(main())
