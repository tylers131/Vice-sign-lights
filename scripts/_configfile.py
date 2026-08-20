"""Shared load/save for the installed config, used by the maintenance scripts.

The ownership handling is the reason this is one function rather than copied
into each script: /etc/vice-lights/config.json is written by a service running
as a normal user, and a root-owned replacement silently breaks saving from the
web UI until someone notices and chowns it back.
"""

from __future__ import annotations

import json
import os
import shutil
import stat


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_config(path: str, config: dict, backup: bool = True) -> str:
    """Write atomically, keeping a .bak and the original's mode and owner.

    Returns the backup path, or "" if there was nothing to back up.
    """
    backup_path = ""
    original = None
    if os.path.exists(path):
        original = os.stat(path)
        if backup:
            backup_path = path + ".bak"
            shutil.copy2(path, backup_path)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    if original is not None:
        try:
            os.chmod(tmp, stat.S_IMODE(original.st_mode))
        except OSError:
            pass
        # Only root can hand the file back to another user; when run unprivileged
        # against your own file the owner is already right.
        if os.geteuid() == 0:
            try:
                os.chown(tmp, original.st_uid, original.st_gid)
            except OSError:
                pass
    else:
        os.chmod(tmp, 0o644)

    os.replace(tmp, path)
    return backup_path
