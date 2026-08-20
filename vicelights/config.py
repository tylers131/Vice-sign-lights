"""JSON config store: devices, groups, scenes, schedules, settings.

Hand-editable and UI-editable.  Writes are atomic (tmp file + fsync + rename)
so a power cut mid-save can never leave a truncated config on the SD card --
which matters when the whole thing lives on playa power.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid

log = logging.getLogger("vicelights.config")

ADDR_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

DEFAULT_SETTINGS = {
    "host": "0.0.0.0",
    "port": 80,
    "brightness_mode": "scale",      # scale | native | both
    # The sign runs flat out. With this true, every brightness value anywhere
    # -- UI slider, scene step, API call -- is overridden to 100, so nothing can
    # quietly dim the sign. Set false to re-enable dimming end to end.
    "force_full_brightness": True,
    # How to leave a built-in pattern when a solid colour is next requested.
    # none | static_mode | power_cycle. Measured on this sign with
    # `elk_scan.py unstick`: a plain colour frame is enough, so nothing extra is
    # sent. Raise it only if a replacement controller behaves differently.
    "exit_pattern": "none",
    "connect_timeout": 12.0,         # seconds per connect attempt
    "write_timeout": 6.0,            # seconds per characteristic write
    "attempts": 3,                   # 1 try + 2 retries
    "retry_backoff": 0.8,            # seconds, doubled each retry
    "inter_frame_delay": 0.06,       # gap between frames on one device
    "inter_device_delay": 0.35,      # gap between devices: lets wifi breathe
    # Measured on this sign: a clean BlueZ disconnect takes ~2.4s, 40% of the
    # time per controller, and nothing depends on its completing. Wait this long
    # then move on, letting it finish in the background. 0 = never wait; raise it
    # to restore the old blocking behaviour if a radio gets upset.
    "disconnect_wait": 0.5,
    # A controller that is off, out of range or dead costs attempts x
    # connect_timeout on EVERY sweep -- around 60s for one unit at the defaults.
    # After this many consecutive failures it is skipped outright for
    # failure_cooldown seconds, then probed once. It comes straight back the
    # moment it answers.
    "cooldown_after": 2,
    "failure_cooldown": 180.0,
    "scan_seconds": 8.0,
    "log_level": "INFO",
    "apply_on_boot": "",             # scene name to apply at startup, "" = none
}

# Cycle scenes all night on a monotonic timer. Deliberately not wall-clock: the
# Zero W has no RTC, so a cold boot on the playa knows nothing about the time,
# and rotation has to work anyway.
DEFAULT_ROTATION = {
    "enabled": False,
    "playlist": [],                  # scene names; empty = every scene not excluded
    "exclude": ["All off"],          # never rotate into these
    "interval_minutes": 8.0,
    "order": "shuffle",              # shuffle | sequential
    "avoid_repeat": True,            # never play the same scene twice running
    # Touching the controls should win. Any manual command pauses rotation for
    # this long, so the sign does not fight you while you are looking at it.
    "hold_after_manual_minutes": 15.0,
}

# A sweep takes ~50s; anything near that leaves the radio permanently busy and
# the UI permanently sluggish.
MIN_ROTATION_MINUTES = 2.0

DEFAULT_CONFIG = {
    "settings": dict(DEFAULT_SETTINGS),
    "rotation": dict(DEFAULT_ROTATION),
    # What each built-in pattern actually does on THIS hardware, keyed "0x89".
    # The documented names do not describe these controllers, so this is filled
    # in by watching: `elk_scan.py modes ADDR`.
    "mode_names": {},
    "devices": [],
    "groups": [],
    "scenes": [],
    "schedules": [],
    "timers": [],
}


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def normalize_address(addr: str) -> str:
    addr = (addr or "").strip().upper().replace("-", ":")
    if not ADDR_RE.match(addr):
        raise ValueError("bad BLE address: %r (want AA:BB:CC:DD:EE:FF)" % addr)
    return addr


def _channels(value) -> str:
    """Validate a per-device channel order, falling back to plain rgb."""
    from .protocol import CHANNEL_ORDERS
    order = str(value or "rgb").strip().lower()
    if order not in CHANNEL_ORDERS:
        if value:
            log.warning("ignoring unknown channel order %r", value)
        return "rgb"
    return order


def _rotation(raw) -> dict:
    """Validate the rotation block, clamping the interval to something sane."""
    value = dict(DEFAULT_ROTATION)
    value.update(raw or {})
    value["enabled"] = bool(value.get("enabled"))
    value["playlist"] = [str(n).strip() for n in (value.get("playlist") or []) if str(n).strip()]
    value["exclude"] = [str(n).strip() for n in (value.get("exclude") or []) if str(n).strip()]
    value["order"] = "sequential" if value.get("order") == "sequential" else "shuffle"
    value["avoid_repeat"] = bool(value.get("avoid_repeat", True))
    try:
        minutes = float(value.get("interval_minutes", 8.0))
    except (TypeError, ValueError):
        minutes = 8.0
    if minutes < MIN_ROTATION_MINUTES:
        log.warning("rotation interval %.1f min is below the %.1f min floor; using the floor",
                    minutes, MIN_ROTATION_MINUTES)
        minutes = MIN_ROTATION_MINUTES
    value["interval_minutes"] = minutes
    try:
        value["hold_after_manual_minutes"] = max(
            0.0, float(value.get("hold_after_manual_minutes", 15.0)))
    except (TypeError, ValueError):
        value["hold_after_manual_minutes"] = 15.0
    return value


def _mode_names(raw) -> dict:
    """Normalise observed mode labels to '0xNN' keys, dropping junk."""
    from .protocol import MODE_MIN, MODE_MAX, mode_key
    names = {}
    for key, label in (raw or {}).items():
        try:
            value = int(str(key), 0)
        except (TypeError, ValueError):
            log.warning("ignoring mode name with bad key %r", key)
            continue
        if not MODE_MIN <= value <= MODE_MAX:
            log.warning("ignoring mode name outside 0x%02x-0x%02x: %r",
                        MODE_MIN, MODE_MAX, key)
            continue
        label = str(label or "").strip()
        if label:
            names[mode_key(value)] = label[:60]
    return names


class ConfigError(Exception):
    pass


LAST_GOOD_SUFFIX = ".lastgood"


def _read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


class ConfigStore:
    """Thread-safe accessor around the on-disk JSON config."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._data = dict(DEFAULT_CONFIG)
        self.load()

    # ---------------------------------------------------------------- io

    def load(self):
        with self._lock:
            if not os.path.exists(self.path):
                log.warning("no config at %s, starting from defaults", self.path)
                self._data = json.loads(json.dumps(DEFAULT_CONFIG))
                self.save()
                return self._data
            try:
                raw = _read_json(self.path)
            except Exception as exc:
                return self._recover(exc)
            self._data = self._normalize(raw)
            self._keep_last_good()
            log.info(
                "loaded config: %d devices, %d groups, %d scenes, %d schedules",
                len(self._data["devices"]), len(self._data["groups"]),
                len(self._data["scenes"]), len(self._data["schedules"]),
            )
            return self._data

    def _keep_last_good(self):
        """Snapshot a config that parsed, to fall back to if this one stops.

        Cheap: load happens at startup and on an explicit reload, not per sweep.
        """
        try:
            shutil.copy2(self.path, self.path + LAST_GOOD_SUFFIX)
        except OSError as exc:
            log.debug("could not snapshot last-good config: %s", exc)

    def _recover(self, exc):
        """Come up anyway when the config will not parse.

        A sign serving its web UI with the wrong scenes can be fixed from a
        phone in thirty seconds. One that raises at startup crash-loops under
        Restart=always, never binds port 80, and needs SSH and a laptop --
        which, in a desert at night, means it stays dark. So a config that has
        been corrupted (an SD card surviving repeated unclean shutdowns is not
        a given) must never be able to stop the service from starting.
        """
        log.error("config at %s will not parse: %s", self.path, exc)
        preserved = self._preserve_unreadable()

        for label, candidate in (("last-good", self.path + LAST_GOOD_SUFFIX),
                                 ("backup", self.path + ".bak")):
            if not os.path.exists(candidate):
                continue
            try:
                raw = _read_json(candidate)
            except Exception as inner:
                log.error("the %s copy (%s) will not parse either: %s",
                          label, candidate, inner)
                continue
            self._data = self._normalize(raw)
            log.warning("recovered the config from the %s copy (%s)", label, candidate)
            if preserved:
                log.warning("the unreadable file was kept at %s", preserved)
            self.save()
            return self._data

        log.error("no usable copy of the config; starting from defaults. "
                  "Devices, scenes and mode names are gone until you restore "
                  "one%s", " -- unreadable original kept at %s" % preserved
                  if preserved else "")
        self._data = json.loads(json.dumps(DEFAULT_CONFIG))
        self.save()
        return self._data

    def _preserve_unreadable(self) -> str:
        """Copy the unreadable config aside so it can still be salvaged.

        Copied rather than moved: save() carries ownership across only when the
        original is still there, and losing the owner is how the config stops
        being writable from the web UI.
        """
        target = "%s.unreadable-%d" % (self.path, int(time.time()))
        try:
            shutil.copy2(self.path, target)
            return target
        except OSError as exc:
            log.error("could not preserve the unreadable config: %s", exc)
            return ""

    def save(self):
        with self._lock:
            payload = json.dumps(self._data, indent=2, sort_keys=False)
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                # An atomic replace makes a NEW inode owned by whoever is
                # writing. The service runs as root, so without carrying the
                # original ownership across it quietly takes the config away
                # from the user editing it with the CLI -- and every `chown`
                # gets undone by the next save.
                self._inherit_ownership(tmp)
                os.replace(tmp, self.path)
                # fsync the directory so the rename itself is durable.
                dir_fd = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    def _inherit_ownership(self, tmp: str):
        """Give the replacement file the original's mode and owner."""
        try:
            original = os.stat(self.path)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
            return
        try:
            os.chmod(tmp, stat.S_IMODE(original.st_mode))
        except OSError as exc:
            log.debug("could not carry mode across: %s", exc)
        if os.geteuid() != 0 and original.st_uid != os.geteuid():
            return                      # only root can hand a file to someone else
        try:
            os.chown(tmp, original.st_uid, original.st_gid)
        except OSError as exc:
            log.debug("could not carry ownership across: %s", exc)

    # ------------------------------------------------------------ shape

    def _normalize(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be an object")

        data = json.loads(json.dumps(DEFAULT_CONFIG))
        settings = dict(DEFAULT_SETTINGS)
        settings.update(raw.get("settings") or {})
        data["settings"] = settings
        data["rotation"] = _rotation(raw.get("rotation"))
        data["mode_names"] = _mode_names(raw.get("mode_names"))

        seen = set()
        for entry in raw.get("devices") or []:
            try:
                address = normalize_address(entry.get("address"))
            except ValueError as exc:
                log.error("dropping device: %s", exc)
                continue
            if address in seen:
                log.warning("dropping duplicate device %s", address)
                continue
            seen.add(address)
            data["devices"].append({
                "address": address,
                "name": (entry.get("name") or address).strip(),
                "groups": [str(g).strip() for g in (entry.get("groups") or []) if str(g).strip()],
                "enabled": bool(entry.get("enabled", True)),
                "char_uuid": entry.get("char_uuid") or None,
                "channels": _channels(entry.get("channels")),
                "notes": entry.get("notes") or "",
            })

        groups = [str(g).strip() for g in (raw.get("groups") or []) if str(g).strip()]
        for device in data["devices"]:
            for group in device["groups"]:
                if group not in groups:
                    groups.append(group)
        data["groups"] = groups

        for scene in raw.get("scenes") or []:
            steps = []
            for step in scene.get("steps") or []:
                steps.append({
                    "target": step.get("target") or "all",
                    "power": step.get("power", True),
                    "color": step.get("color", "#ffffff"),
                    "brightness": step.get("brightness", 100),
                    "mode": step.get("mode"),
                    "speed": step.get("speed"),
                })
            data["scenes"].append({
                "id": scene.get("id") or new_id(),
                "name": (scene.get("name") or "unnamed").strip(),
                "steps": steps,
            })

        for schedule in raw.get("schedules") or []:
            data["schedules"].append({
                "id": schedule.get("id") or new_id(),
                "name": (schedule.get("name") or "").strip(),
                "scene": schedule.get("scene") or "",
                "time": schedule.get("time") or "00:00",
                "days": sorted({int(d) for d in (schedule.get("days") or []) if 0 <= int(d) <= 6}),
                "enabled": bool(schedule.get("enabled", True)),
                "last_fired": schedule.get("last_fired") or "",
            })

        return data

    # ------------------------------------------------------------ reads

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    @property
    def settings(self) -> dict:
        with self._lock:
            return dict(self._data["settings"])

    def setting(self, key, default=None):
        with self._lock:
            return self._data["settings"].get(key, DEFAULT_SETTINGS.get(key, default))

    def devices(self, enabled_only: bool = False) -> list:
        with self._lock:
            devices = json.loads(json.dumps(self._data["devices"]))
        if enabled_only:
            devices = [d for d in devices if d.get("enabled", True)]
        return devices

    def device(self, address: str):
        address = normalize_address(address)
        for device in self.devices():
            if device["address"] == address:
                return device
        return None

    def group_names(self) -> list:
        with self._lock:
            return list(self._data["groups"])

    def scenes(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._data["scenes"]))

    def scene(self, name_or_id: str):
        key = (name_or_id or "").strip().lower()
        for scene in self.scenes():
            if scene["id"] == name_or_id or scene["name"].strip().lower() == key:
                return scene
        return None

    def schedules(self) -> list:
        with self._lock:
            return json.loads(json.dumps(self._data["schedules"]))

    # ---------------------------------------------------------- targets

    def resolve_target(self, target: str) -> list:
        """'all' | 'group:NAME' | 'device:AA:BB:..' | bare address -> addresses.

        Disabled devices are never returned: disabling is how you park a unit
        that has died in the dust without editing every scene.
        """
        target = (target or "all").strip()
        devices = self.devices(enabled_only=True)

        if target.lower() in ("all", "*"):
            return [d["address"] for d in devices]

        if target.lower().startswith("group:"):
            name = target.split(":", 1)[1].strip().lower()
            return [d["address"] for d in devices
                    if any(g.strip().lower() == name for g in d["groups"])]

        if target.lower().startswith("device:"):
            target = target.split(":", 1)[1].strip()

        try:
            address = normalize_address(target)
        except ValueError:
            # Fall back to a friendly-name lookup so scenes can name a device.
            key = target.strip().lower()
            return [d["address"] for d in devices if d["name"].strip().lower() == key]
        return [d["address"] for d in devices if d["address"] == address]

    def target_label(self, target: str) -> str:
        target = (target or "all").strip()
        if target.lower() in ("all", "*"):
            return "all devices"
        if target.lower().startswith("group:"):
            return "group %s" % target.split(":", 1)[1]
        addresses = self.resolve_target(target)
        if len(addresses) == 1:
            device = self.device(addresses[0])
            if device:
                return device["name"]
        return target

    # --------------------------------------------------------- mutation

    def mutate(self, fn):
        """Apply ``fn(data)`` under the lock, then persist and return the result."""
        with self._lock:
            result = fn(self._data)
            self._sync_groups()
            self.save()
        return result

    def _sync_groups(self):
        groups = list(self._data["groups"])
        for device in self._data["devices"]:
            for group in device["groups"]:
                if group not in groups:
                    groups.append(group)
        self._data["groups"] = groups

    def replace_all(self, raw: dict):
        with self._lock:
            self._data = self._normalize(raw)
            self.save()
        return self.snapshot()

    def upsert_device(self, entry: dict) -> dict:
        address = normalize_address(entry.get("address"))

        def apply(data):
            for device in data["devices"]:
                if device["address"] == address:
                    device["name"] = (entry.get("name") or device["name"]).strip()
                    if "groups" in entry:
                        device["groups"] = [str(g).strip() for g in entry["groups"] if str(g).strip()]
                    if "enabled" in entry:
                        device["enabled"] = bool(entry["enabled"])
                    if "char_uuid" in entry:
                        device["char_uuid"] = entry["char_uuid"] or None
                    if "channels" in entry:
                        device["channels"] = _channels(entry["channels"])
                    if "notes" in entry:
                        device["notes"] = entry["notes"]
                    return device
            device = {
                "address": address,
                "name": (entry.get("name") or address).strip(),
                "groups": [str(g).strip() for g in (entry.get("groups") or []) if str(g).strip()],
                "enabled": bool(entry.get("enabled", True)),
                "char_uuid": entry.get("char_uuid") or None,
                "channels": _channels(entry.get("channels")),
                "notes": entry.get("notes") or "",
            }
            data["devices"].append(device)
            return device

        return self.mutate(apply)

    def delete_device(self, address: str) -> bool:
        address = normalize_address(address)

        def apply(data):
            before = len(data["devices"])
            data["devices"] = [d for d in data["devices"] if d["address"] != address]
            return len(data["devices"]) != before

        return self.mutate(apply)

    def remember_characteristic(self, address: str, char_uuid: str):
        """Cache the detected characteristic so later connects skip discovery."""
        def apply(data):
            for device in data["devices"]:
                if device["address"] == address and device.get("char_uuid") != char_uuid:
                    device["char_uuid"] = char_uuid
                    return True
            return False

        try:
            return self.mutate(apply)
        except Exception:
            log.exception("could not cache characteristic for %s", address)
            return False

    def add_group(self, name: str):
        name = (name or "").strip()
        if not name:
            raise ValueError("group name required")

        def apply(data):
            if name not in data["groups"]:
                data["groups"].append(name)
            return data["groups"]

        return self.mutate(apply)

    def delete_group(self, name: str):
        def apply(data):
            data["groups"] = [g for g in data["groups"] if g != name]
            for device in data["devices"]:
                device["groups"] = [g for g in device["groups"] if g != name]
            return data["groups"]

        result = self.mutate(apply)
        # _sync_groups would resurrect it from membership; membership is cleared
        # above, so this is safe.
        return result

    def upsert_scene(self, scene: dict) -> dict:
        name = (scene.get("name") or "").strip()
        if not name:
            raise ValueError("scene name required")
        steps = []
        for step in scene.get("steps") or []:
            steps.append({
                "target": step.get("target") or "all",
                "power": bool(step.get("power", True)),
                "color": step.get("color", "#ffffff"),
                "brightness": int(step.get("brightness", 100)),
                "mode": step.get("mode"),
                "speed": step.get("speed"),
            })
        if not steps:
            raise ValueError("scene needs at least one step")
        scene_id = scene.get("id")

        def apply(data):
            for existing in data["scenes"]:
                if (scene_id and existing["id"] == scene_id) or existing["name"] == name:
                    existing["name"] = name
                    existing["steps"] = steps
                    return existing
            created = {"id": scene_id or new_id(), "name": name, "steps": steps}
            data["scenes"].append(created)
            return created

        return self.mutate(apply)

    def delete_scene(self, scene_id: str) -> bool:
        def apply(data):
            before = len(data["scenes"])
            data["scenes"] = [s for s in data["scenes"]
                              if s["id"] != scene_id and s["name"] != scene_id]
            return len(data["scenes"]) != before

        return self.mutate(apply)

    def upsert_schedule(self, schedule: dict) -> dict:
        time_str = (schedule.get("time") or "").strip()
        if not re.match(r"^\d{1,2}:\d{2}$", time_str):
            raise ValueError("time must be HH:MM")
        hour, minute = (int(part) for part in time_str.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("time out of range")
        time_str = "%02d:%02d" % (hour, minute)
        scene_name = (schedule.get("scene") or "").strip()
        if not self.scene(scene_name):
            raise ValueError("unknown scene: %s" % scene_name)
        days = sorted({int(d) for d in (schedule.get("days") or []) if 0 <= int(d) <= 6})
        schedule_id = schedule.get("id")

        def apply(data):
            for existing in data["schedules"]:
                if schedule_id and existing["id"] == schedule_id:
                    existing.update({
                        "name": (schedule.get("name") or existing["name"]).strip(),
                        "scene": scene_name,
                        "time": time_str,
                        "days": days,
                        "enabled": bool(schedule.get("enabled", True)),
                    })
                    return existing
            created = {
                "id": schedule_id or new_id(),
                "name": (schedule.get("name") or "").strip(),
                "scene": scene_name,
                "time": time_str,
                "days": days,
                "enabled": bool(schedule.get("enabled", True)),
                "last_fired": "",
            }
            data["schedules"].append(created)
            return created

        return self.mutate(apply)

    def delete_schedule(self, schedule_id: str) -> bool:
        def apply(data):
            before = len(data["schedules"])
            data["schedules"] = [s for s in data["schedules"] if s["id"] != schedule_id]
            return len(data["schedules"]) != before

        return self.mutate(apply)

    def mode_names(self) -> dict:
        with self._lock:
            return dict(self._data.get("mode_names") or {})

    def set_mode_name(self, value: int, label: str) -> dict:
        """Record what a pattern really looks like on this hardware."""
        from .protocol import mode_key

        def apply(data):
            names = dict(data.get("mode_names") or {})
            key = mode_key(value)
            label_clean = str(label or "").strip()[:60]
            if label_clean:
                names[key] = label_clean
            else:
                names.pop(key, None)
            data["mode_names"] = _mode_names(names)
            return data["mode_names"]

        return self.mutate(apply)

    def rotation(self) -> dict:
        with self._lock:
            return dict(self._data.get("rotation") or DEFAULT_ROTATION)

    def update_rotation(self, changes: dict) -> dict:
        def apply(data):
            merged = dict(data.get("rotation") or DEFAULT_ROTATION)
            merged.update(changes or {})
            data["rotation"] = _rotation(merged)
            return data["rotation"]

        return self.mutate(apply)

    def rotation_scenes(self) -> list:
        """The scenes rotation may play, in config order, honouring exclusions."""
        rotation = self.rotation()
        excluded = {n.strip().lower() for n in rotation["exclude"]}
        chosen = [n.strip().lower() for n in rotation["playlist"]]
        names = []
        for scene in self.scenes():
            key = scene["name"].strip().lower()
            if key in excluded:
                continue
            if chosen and key not in chosen:
                continue
            names.append(scene["name"])
        return names

    def mark_schedule_fired(self, schedule_id: str, stamp: str):
        def apply(data):
            for schedule in data["schedules"]:
                if schedule["id"] == schedule_id:
                    schedule["last_fired"] = stamp
            return True

        return self.mutate(apply)
