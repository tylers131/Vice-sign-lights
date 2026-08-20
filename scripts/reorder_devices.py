#!/usr/bin/env python3
"""Set the order the sign is written in.

Devices are addressed strictly one at a time -- the Pi's single radio cannot
hold twelve BLE connections -- so at ~2.5s each a scene change is a visible
wipe across the sign rather than a snap. The order of the "devices" array is
the order of that wipe, for any step targeting "all".

    ./scripts/reorder_devices.py A_Cup A_V A_I A_C A_E A_Straw \
                                 B_Cup B_V B_I B_C B_E B_Straw

Names must cover the fleet exactly: every device once, none invented. Getting
that wrong by hand is how a device quietly disappears from the config, so it
is refused rather than half-applied. Addresses work in place of names.

Note this only governs steps that resolve several devices. A scene built from
group steps ("letters", then "cup", then "straw") addresses them group by
group regardless -- step order outranks device order.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _configfile import load_config, save_config  # noqa: E402

DEFAULT_CONFIG = "/etc/vice-lights/config.json"


def reorder(devices: list, wanted: list) -> list:
    """Return devices in the requested order, or raise if it is not a permutation."""
    by_key = {}
    for device in devices:
        by_key[device["name"].strip().lower()] = device
        by_key[device["address"].strip().upper()] = device

    ordered, claimed = [], set()
    missing, duplicated = [], []
    for token in wanted:
        key = token.strip()
        device = by_key.get(key.lower()) or by_key.get(key.upper())
        if device is None:
            missing.append(token)
            continue
        if id(device) in claimed:
            duplicated.append(token)
            continue
        claimed.add(id(device))
        ordered.append(device)

    problems = []
    if missing:
        problems.append("not in the config: %s" % ", ".join(missing))
    if duplicated:
        problems.append("listed twice: %s" % ", ".join(duplicated))
    left_out = [d["name"] for d in devices if id(d) not in claimed]
    if left_out:
        problems.append("left out: %s" % ", ".join(left_out))
    if problems:
        raise SystemExit("refusing to reorder -- " + "; ".join(problems))
    return ordered


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="+", help="device names or addresses, in the wanted order")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--apply", action="store_true", help="write the file; otherwise only report")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        sys.exit("no config at %s -- pass --config PATH" % args.config)
    config = load_config(args.config)
    devices = config.get("devices") or []
    if not devices:
        sys.exit("no devices in %s" % args.config)

    before = [d["name"] for d in devices]
    ordered = reorder(devices, args.names)
    after = [d["name"] for d in ordered]

    print("before:  " + " -> ".join(before))
    print("after:   " + " -> ".join(after))
    if before == after:
        print("\nAlready in that order; nothing to do.")
        return
    if not args.apply:
        print("\nDry run. Re-run with --apply to write %s" % args.config)
        return

    config["devices"] = ordered
    backup = save_config(args.config, config)
    print("\nWrote %s%s" % (args.config, " (previous saved as %s)" % backup if backup else ""))
    print("Reload it with:  curl -X POST http://localhost/api/config/reload")


if __name__ == "__main__":
    main()
