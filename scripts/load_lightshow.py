#!/usr/bin/env python3
"""Install the around-the-clock light show into the live config.

The show -- the scene palette, the time-of-day day-parts, the coffee attract
look and the 24/7 rotation -- lives in ``vicelights/lightshow.py``. This script
writes it into the installed config in one shot, so the sign runs itself:

* lit 24/7, always changing, re-waking any controller whose Bluetooth dropped;
* a mood that follows the clock -- chill at dawn, bold enough to read in desert
  sun, full-on party after dark, hypnotic in the small hours;
* a warm come-here look whenever a coffee service is on, at the same moment the
  panel is shouting about iced coffee;
* and no scheduled blackout -- the old "Dawn off" that was leaving the sign
  dark every morning is removed.

It is safe to re-run: it replaces the scenes and rotation each time and leaves
everything else -- your twelve devices, their groups, the panel, saved
messages, the temperature block, the learned mode names -- exactly as it found
them.

    # see what would change, touch nothing:
    sudo ./scripts/load_lightshow.py

    # write it:
    sudo ./scripts/load_lightshow.py --apply

Run it as the same user the service writes the config as (root is fine -- the
write carries the original owner across), then the sign picks it up on the next
reload or restart.
"""

from __future__ import annotations

import argparse
import os
import sys

# Import the package whether this is run from the repo root or from scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vicelights import lightshow                       # noqa: E402
from vicelights.config import ConfigStore              # noqa: E402

DEFAULT_CONFIG = os.environ.get("VICELIGHTS_CONFIG",
                                "/etc/vice-lights/config.json")


def _dropped_blackout(schedules) -> list:
    return [s for s in (schedules or [])
            if (s.get("scene") or "").strip().lower() == "all off"]


def _summary(store) -> None:
    """Print what is there now versus what the show would install."""
    snap = store.snapshot()
    rotation = snap.get("rotation") or {}
    settings = snap.get("settings") or {}

    print("Now:")
    print("  scenes:      %d" % len(snap.get("scenes") or []))
    print("  rotation:    %s" % ("on" if rotation.get("enabled") else "off"))
    print("  day-parts:   %d" % len(rotation.get("dayparts") or []))
    print("  boot scene:  %s" % (settings.get("apply_on_boot") or "(none)"))
    print("  schedules:   %d" % len(snap.get("schedules") or []))

    dropped = _dropped_blackout(snap.get("schedules"))
    parts = ", ".join("%s @%s" % (d["name"], d["start"]) for d in lightshow.DAYPARTS)

    print("\nWould install:")
    print("  scenes:      %d  (%s ... All off)" %
          (len(lightshow.SCENES), ", ".join(s["name"] for s in lightshow.SCENES[:4])))
    print("  rotation:    on, 24/7")
    print("  day-parts:   %d  (%s)" % (len(lightshow.DAYPARTS), parts))
    print("  attract:     %s  (while coffee is on)" % ", ".join(lightshow.ATTRACT))
    print("  boot scene:  %s" % lightshow.BOOT_SCENE)
    if dropped:
        print("  removing:    %d scheduled blackout(s): %s" %
              (len(dropped), ", ".join(s.get("name") or "All off" for s in dropped)))
    else:
        print("  removing:    no scheduled blackout found (nothing to remove)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="path to config.json (default %(default)s)")
    parser.add_argument("--apply", action="store_true",
                        help="write the file; otherwise only report")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        sys.exit("no config at %s -- pass --config PATH" % args.config)

    # ConfigStore reads and normalises the live config; loading it does not
    # rewrite the main file (only a .lastgood snapshot), so the summary is
    # safe to show before deciding to write.
    store = ConfigStore(args.config)
    _summary(store)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write %s" % args.config)
        return

    lightshow.apply(store)
    print("\nWrote %s" % args.config)
    print("The sign picks it up on the next reload or restart:")
    print("  curl -X POST http://localhost/api/config/reload")
    print("  # or:  sudo systemctl restart vice-lights")


if __name__ == "__main__":
    main()
