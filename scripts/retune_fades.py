#!/usr/bin/env python3
"""Raise the speed of single-colour fade patterns so they actually move.

A "fade <colour>" mode only ramps brightness on one hue. On this hardware that
is imperceptible when slow -- 0x91 "fade white" at speed 25 sits looking like
solid white. The multi-colour fades ("fade 7 colours", "fade RGB") shift hue
instead, so they read as movement even slowly and are left alone.

Speed 100 is the default because it is the value confirmed visible on a fade.
For scale: a strobe at 60 runs about 2s off / 2s on, so 60 is roughly the
slowest worth using at all, and a fade needs more than a strobe does -- on/off
is easier to see than a brightness ramp. Pass --speed to choose your own.

    ./scripts/retune_fades.py                        # show what would change
    sudo ./scripts/retune_fades.py --apply           # write it

Defaults to the installed config; pass --config for a different file.
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from vicelights import protocol   # noqa: E402
from _configfile import load_config, save_config  # noqa: E402

DEFAULT_CONFIG = "/etc/vice-lights/config.json"


def is_single_colour_fade(mode, learned) -> bool:
    """A fade over one hue: brightness-only, and invisible when slow."""
    if not mode:
        return False
    label = protocol.mode_label(mode, learned).strip().lower()
    if not label.startswith("fade"):
        return False
    # "fade 7 colours" and "fade RGB" move through hues; those are fine slow.
    return "7 colour" not in label and "rgb" not in label


def retune(config: dict, speed: int, floor: int) -> list:
    learned = config.get("mode_names") or {}
    changes = []
    for scene in config.get("scenes", []):
        for index, step in enumerate(scene.get("steps", [])):
            mode = step.get("mode")
            if not is_single_colour_fade(mode, learned):
                continue
            was = step.get("speed")
            if was is not None and was >= floor:
                continue          # already fast enough to see; leave it alone
            step["speed"] = speed
            changes.append((scene.get("name", scene.get("id", "?")), index,
                            step.get("target", "?"), mode,
                            protocol.mode_label(mode, learned), was, speed))
    return changes


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--speed", type=int, default=100,
                        help="speed to set (default 100, the confirmed-visible value)")
    parser.add_argument("--floor", type=int, default=60,
                        help="leave steps already at or above this speed alone "
                             "(default 60: below that a pattern cycles too slowly to read)")
    parser.add_argument("--apply", action="store_true",
                        help="write the file; without this, only report")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        sys.exit("no config at %s -- pass --config PATH" % args.config)
    config = load_config(args.config)

    changes = retune(config, args.speed, args.floor)
    if not changes:
        print("Nothing to change: no single-colour fade below speed %d." % args.floor)
        return

    scenes = []
    for name, _i, target, mode, label, was, now in changes:
        if name not in scenes:
            scenes.append(name)
        print("  %-13s %-14s 0x%02x %-16s speed %s -> %d"
              % (name, target, mode, label, was, now))
    print("\n%d step(s) across %d scene(s): %s"
          % (len(changes), len(scenes), ", ".join(scenes)))

    if not args.apply:
        print("\nDry run. Re-run with --apply to write %s" % args.config)
        return
    backup = save_config(args.config, config)
    print("\nWrote %s%s" % (args.config,
                            " (previous saved as %s)" % backup if backup else ""))
    print("Reload it with:  curl -X POST http://localhost/api/config/reload")


if __name__ == "__main__":
    main()
