#!/usr/bin/env python3
"""elk_scan -- discover ELK-BLEDOM controllers and confirm the command bytes.

Standalone CLI. It shares ``vicelights/protocol.py`` with the service, so the
frames you verify here are byte-for-byte the frames the service sends.

    ./elk_scan.py scan                    # list BLE devices, ELK units first
    ./elk_scan.py probe BE:FF:00:11:22:33 # list services/characteristics, pick one
    ./elk_scan.py flash BE:FF:00:11:22:33 # red/green/blue confirmation blink
    ./elk_scan.py color BE:FF:.. '#ff2d78' --brightness 60
    ./elk_scan.py off   BE:FF:.. --variant all   # try each off encoding
    ./elk_scan.py all off                 # black out every controller in the config
    ./elk_scan.py identify                # light each controller in turn and name it
    ./elk_scan.py channels BE:FF:.. --save # detect swapped RGB wiring
    ./elk_scan.py frames                  # print every frame, no radio needed
    ./elk_scan.py adopt --out config.json # scan and write a starter config

Run it before the service, or with the service stopped: only one process should
own the radio at a time (``sudo systemctl stop vice-lights``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vicelights import protocol  # noqa: E402
from vicelights.config import ConfigStore, normalize_address  # noqa: E402
from vicelights.ble import get_services, normalize_scan  # noqa: E402

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    BleakClient = BleakScanner = None


def require_bleak():
    if BleakScanner is None:
        sys.exit("bleak is not installed. pip install -r requirements.txt")


def merge_scan(merged: dict, rows: list) -> dict:
    """Fold one scan pass into the running union, keyed by address.

    A controller at the edge of range advertises intermittently, so a single
    pass under-reports.  Keep the best RSSI seen, the first real name seen (a
    pass can catch the advertisement without the scan response, which is where
    the name lives), and count how many passes each unit appeared in -- that
    count is the useful signal for "which of these is marginal".
    """
    for row in rows:
        entry = merged.setdefault(row["address"], {
            "address": row["address"], "name": "", "rssi": None, "seen": 0})
        entry["seen"] += 1
        if row.get("name") and not entry["name"]:
            entry["name"] = row["name"]
        rssi = row.get("rssi")
        if rssi is not None and (entry["rssi"] is None or rssi > entry["rssi"]):
            entry["rssi"] = rssi
    return merged


async def cmd_scan(args):
    require_bleak()
    passes = max(1, int(getattr(args, "repeat", 1) or 1))
    merged = {}
    for index in range(passes):
        label = ("pass %d/%d: " % (index + 1, passes)) if passes > 1 else ""
        print("%sscanning %.0fs ..." % (label, args.seconds))
        try:
            discovered = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
        except TypeError:
            discovered = await BleakScanner.discover(timeout=args.seconds)
        merge_scan(merged, normalize_scan(discovered))
        if passes > 1:
            elk_so_far = sum(1 for e in merged.values() if protocol.looks_like_elk(e["name"]))
            print("  %d device(s) so far, %d look like ELK-BLEDOM" % (len(merged), elk_so_far))

    rows = list(merged.values())
    if getattr(args, "elk_only", False):
        rows = [e for e in rows if protocol.looks_like_elk(e["name"])]
    rows.sort(key=lambda e: (not protocol.looks_like_elk(e["name"]), -(e["rssi"] or -999)))

    seen_col = "SEEN" if passes > 1 else ""
    print("\n%-20s %-6s %-6s %-28s" % ("ADDRESS", "RSSI", seen_col, "NAME"))
    for entry in rows:
        mark = "<- ELK-BLEDOM" if protocol.looks_like_elk(entry["name"]) else ""
        seen = ("%d/%d" % (entry["seen"], passes)) if passes > 1 else ""
        print("%-20s %-6s %-6s %-28s %s" % (
            entry["address"], entry["rssi"] if entry["rssi"] is not None else "?",
            seen, entry["name"] or "(no name)", mark))

    elk = [e for e in rows if protocol.looks_like_elk(e["name"])]
    print("\n%d device(s), %d look like ELK-BLEDOM" % (len(rows), len(elk)))
    if passes > 1 and elk:
        flaky = [e for e in elk if e["seen"] < passes]
        if flaky:
            print("marginal (missed at least one pass): %s"
                  % ", ".join("%s %d/%d" % (e["address"], e["seen"], passes) for e in flaky))
    return [(e["address"], e["name"], e["rssi"]) for e in rows]


async def cmd_probe(args):
    require_bleak()
    print("connecting to %s ..." % args.address)
    async with BleakClient(args.address, timeout=args.timeout) as client:
        collection = await get_services(client)
        for service in collection:
            print("service %s  %s" % (service.uuid, service.description or ""))
            for char in service.characteristics:
                props = ",".join(char.properties or [])
                print("   char %s  [%s]" % (char.uuid, props))
        uuid, without_response = protocol.pick_characteristic(collection)
        if uuid:
            print("\nwould write to %s (%s)"
                  % (uuid, "write-without-response" if without_response else "write"))
        else:
            print("\nno writable characteristic found!")


async def write_frames(address, frames, timeout=12.0, gap=0.06, verbose=True):
    require_bleak()
    async with BleakClient(address, timeout=timeout) as client:
        uuid, without_response = protocol.pick_characteristic(await get_services(client))
        if uuid is None:
            raise SystemExit("no writable characteristic on %s" % address)
        if verbose:
            print("writing to %s on %s" % (uuid, address))
        for frame in frames:
            if verbose:
                print("  -> %s" % frame.hex(" "))
            await client.write_gatt_char(uuid, frame, response=not without_response)
            await asyncio.sleep(gap)
    return uuid


async def cmd_flash(args):
    frames = [
        protocol.power_frame(True),
        protocol.color_frame(255, 0, 0),
    ]
    await write_frames(args.address, frames, args.timeout)
    for rgb in [(0, 255, 0), (0, 0, 255), (255, 255, 255)]:
        await asyncio.sleep(0.8)
        await write_frames(args.address, [protocol.color_frame(*rgb)], args.timeout)
    print("\nIf you saw red, green, blue, white then the frames are confirmed.")


async def cmd_color(args):
    state = {"power": True, "color": args.color, "brightness": args.brightness}
    frames = protocol.build_frames(state, args.brightness_mode)
    await write_frames(args.address, frames, args.timeout)


async def cmd_off(args):
    variant = getattr(args, "variant", 0)
    if variant == "all":
        # An off frame is only observable on a lit controller, so light it
        # white before each attempt. Otherwise every variant "works".
        for index in range(len(protocol.POWER_OFF_VARIANTS)):
            print("\nvariant %d -- lighting it white first ..." % index)
            await write_frames(args.address,
                               [protocol.power_frame(True),
                                protocol.color_frame(255, 255, 255)],
                               args.timeout, verbose=False)
            await asyncio.sleep(1.5)
            frame = protocol.power_frame(False, index)
            print("  sending off: %s" % frame.hex(" "))
            await write_frames(args.address, [frame], args.timeout, verbose=False)
            await asyncio.sleep(2.5)
        print("\nWhichever variant blacked it out is the one these units speak.")
        print("(Watch for white -> dark. If it stayed white, that variant is wrong.)")
        return
    await write_frames(args.address, [protocol.power_frame(False, int(variant))],
                       args.timeout)


async def cmd_all(args):
    """Drive every controller in the config at once -- the panic button."""
    require_bleak()
    if args.addresses:
        addresses = [normalize_address(a) for a in args.addresses]
    else:
        store = _load_store(args.config)
        if store is None:
            sys.exit("no config found -- pass --addresses AA:.. or --config PATH")
        addresses = [d["address"] for d in store.devices()]

    if args.state == "off":
        frames = [protocol.power_frame(False, args.variant)]
    else:
        rgb = protocol.scale_rgb(protocol.parse_color(args.color), args.brightness)
        frames = [protocol.power_frame(True), protocol.color_frame(*rgb)]

    print("%s %d controller(s): %s"
          % (args.state, len(addresses), protocol.describe_frames(frames)))
    ok = failed = 0
    for index, address in enumerate(addresses, 1):
        try:
            await write_frames(address, frames, args.timeout, verbose=False)
            print("  [%d/%d] %s ok" % (index, len(addresses), address))
            ok += 1
        except Exception as exc:
            print("  [%d/%d] %s FAILED: %s" % (index, len(addresses), address, exc))
            failed += 1
    print("\n%d ok, %d failed" % (ok, failed))


async def cmd_mode(args):
    frames = [protocol.power_frame(True), protocol.mode_frame(args.mode),
              protocol.speed_frame(args.speed)]
    await write_frames(args.address, frames, args.timeout)


async def cmd_adopt(args):
    seen = await cmd_scan(args)
    elk = [row for row in seen if protocol.looks_like_elk(row[1])]
    if not elk:
        sys.exit("no ELK-BLEDOM units found; nothing written")
    config = {
        "settings": {"host": "0.0.0.0", "port": 80, "brightness_mode": "scale"},
        "groups": ["all-letters"],
        "devices": [
            {"address": address, "name": "Light %02d" % (index + 1),
             "groups": ["all-letters"], "enabled": True, "char_uuid": None}
            for index, (address, _name, _rssi) in enumerate(elk)
        ],
        "scenes": [
            {"name": "Warm on", "steps": [
                {"target": "all", "power": True, "color": "#ff8c2a", "brightness": 80}]},
            {"name": "All off", "steps": [{"target": "all", "power": False}]},
        ],
        "schedules": [],
    }
    if os.path.exists(args.out) and not args.force:
        sys.exit("%s exists; pass --force to overwrite" % args.out)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    print("\nwrote %s with %d device(s)" % (args.out, len(elk)))


DEFAULT_CONFIG_PATHS = ("/etc/vice-lights/config.json", "config.json",
                        "config.example.json")


def _load_store(path=None, for_writing=False):
    """Open the first config that exists, so identify works before install.

    config.example.json is tracked in git. Writing names into it means the next
    `git pull` refuses to fast-forward over your own work, so when a command is
    going to write, fork the example into config.json (gitignored) first and
    work there instead.
    """
    for candidate in ([path] if path else list(DEFAULT_CONFIG_PATHS)):
        if not candidate or not os.path.exists(candidate):
            continue
        if for_writing and os.path.basename(candidate) == "config.example.json":
            target = os.path.join(os.path.dirname(candidate) or ".", "config.json")
            if not os.path.exists(target):
                shutil.copyfile(candidate, target)
                print("copied %s -> %s (leaving the tracked example alone)"
                      % (candidate, target))
            else:
                print("%s is tracked in git; using %s instead" % (candidate, target))
            candidate = target
        print("using config %s" % candidate)
        try:
            return ConfigStore(candidate)
        except PermissionError:
            sys.exit(
                "cannot %s %s -- it belongs to root.\n"
                "Either re-run under the service's interpreter:\n"
                "  sudo %s %s ...\n"
                "or hand the config to your user once:\n"
                "  sudo chown -R $USER %s"
                % ("write" if for_writing else "read", candidate,
                   sys.executable, os.path.abspath(sys.argv[0]),
                   os.path.dirname(candidate) or candidate))
    return None


async def cmd_identify(args):
    """Light one controller at a time so you can map addresses to strands.

    You cannot tell a BE:27:96:00:1C:AE from a BE:27:49:00:06:95 by looking at
    it. Plug a strand into one controller, run this, and the address that lights
    it up is that controller's. Type its physical name at the prompt and it is
    written straight into the config.
    """
    require_bleak()
    store = None
    names = {}
    if args.addresses:
        addresses = [normalize_address(a) for a in args.addresses]
    else:
        store = _load_store(args.config, for_writing=True)
        if store is None:
            sys.exit("no config found -- pass --addresses AA:.. or --config PATH")
        addresses = [d["address"] for d in store.devices()]
        names = {d["address"]: d["name"] for d in store.devices()}
    if not addresses:
        sys.exit("no addresses to walk")

    start = max(1, args.start) - 1
    addresses = addresses[start:]
    rgb = protocol.parse_color(args.color)

    if args.all_off_first:
        print("turning all %d controller(s) off first ..." % len(addresses))
        for address in addresses:
            try:
                await write_frames(address, [protocol.power_frame(False)],
                                   args.timeout, verbose=False)
            except Exception as exc:
                print("  %s: %s" % (address, exc))

    print("\nWalking %d controller(s). Only one is lit at a time." % len(addresses))
    if not args.auto:
        print("At each prompt: type a name to save it, Enter to skip, q to quit.\n")

    for index, address in enumerate(addresses, start + 1):
        known = names.get(address, "")
        print("[%d/%d] %s%s" % (index, start + len(addresses), address,
                                ("  (currently '%s')" % known) if known else ""))
        try:
            await write_frames(address, [protocol.power_frame(True),
                                         protocol.color_frame(*rgb)],
                               args.timeout, verbose=False)
        except Exception as exc:
            print("  unreachable: %s" % exc)
            continue

        if args.auto:
            await asyncio.sleep(args.auto)
        else:
            try:
                answer = input("  lit? name it, or Enter to skip: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if answer.lower() in ("q", "quit"):
                if not args.keep_on:
                    try:
                        await write_frames(address, [protocol.power_frame(False)],
                                           args.timeout, verbose=False)
                    except Exception as exc:
                        print("  could not turn it back off: %s" % exc)
                break
            if answer and store is not None:
                store.upsert_device({"address": address, "name": answer})
                print("  saved as '%s'" % answer)
            elif answer:
                print("  (no config open, so '%s' was not saved)" % answer)

        if not args.keep_on:
            try:
                await write_frames(address, [protocol.power_frame(False)],
                                   args.timeout, verbose=False)
            except Exception as exc:
                # Never silent: a failed off is why you end up with several lit
                # at once and no idea which address you are looking at.
                print("  WARNING: could not turn it back off: %s" % exc)

    print("\ndone.")
    if store is not None:
        print("config: %s" % store.path)


PROBES = [("red", (255, 0, 0), "r"), ("green", (0, 255, 0), "g"),
          ("blue", (0, 0, 255), "b")]


async def cmd_channels(args):
    """Work out whether a controller's RGB pads are wired the way we assume.

    Sends pure red, then green, then blue, and asks what actually appeared. If
    the answers are a permutation of r/g/b, that permutation is the device's
    channel order and can be saved straight into the config.
    """
    require_bleak()
    address = normalize_address(args.address)
    store = _load_store(args.config, for_writing=True) if args.save else None

    print("Watch the strand. Answer with what you SEE, not what was asked for.")
    observed = []
    for name, rgb, _expected in PROBES:
        try:
            await write_frames(address,
                               [protocol.power_frame(True), protocol.color_frame(*rgb)],
                               args.timeout, verbose=False)
        except Exception as exc:
            sys.exit("could not write to %s: %s" % (address, exc))
        try:
            answer = input("  sent %-5s -> what do you see? "
                           "[r]ed [g]reen [b]lue [w]hite [n]othing [o]ther: "
                           % name).strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return
        observed.append(answer)

    print("\nsent red, green, blue -> saw %s" % ", ".join(o or "?" for o in observed))

    if sorted(observed) == ["b", "g", "r"]:
        order = "".join(observed)
        if order == "rgb":
            print("Channels are wired correctly: this controller shows the colour"
                  " it is asked for.")
            print("\nSo a colour problem in the web app is in the app's path, not")
            print("the wiring. Set a colour from the UI, then compare what it put")
            print("on the wire with what this command just sent:")
            print("  journalctl -u vice-lights -n 20 --no-pager | grep queued")
            print("A solid colour should log 7e 00 05 03 RR GG BB 00 ef with your")
            print("RR GG BB in it. If it logs 7e 00 03 .. instead, a built-in")
            print("pattern is selected in the UI and it overrides solid colour.")
            print("\nIf the bytes are right and the colour still looks wrong, that")
            print("is the strip, not the software: analog RGB blends never match a")
            print("phone screen exactly, and pale colours drift worst.")
        else:
            print("Channel order for this controller is '%s'." % order)
            print("Frames will be permuted so asking for red gives red.")
            if store is not None:
                store.upsert_device({"address": address, "channels": order})
                print("saved to %s" % store.path)
            else:
                print("Re-run with --save, or set \"channels\": \"%s\" on this"
                      " device in config.json." % order)
        return

    if "n" in observed:
        print("\nAt least one colour changed nothing. That is not a wiring")
        print("problem -- the controller may be running a built-in pattern that")
        print("overrides solid colour. Try:")
        print("  elk_scan.py off %s" % address)
        print("  elk_scan.py color %s '#ff0000'" % address)
    elif "w" in observed:
        print("\nWhite where a primary was asked for means more than one channel")
        print("is driven at once -- usually a wiring short, or a strand wired for")
        print("a different controller type (check it is analog RGB, not addressable).")
    else:
        print("\nNot a clean permutation, so this is not a simple channel swap.")
        print("Tell me exactly what you saw for each and I'll work from that.")


async def cmd_unstick(args):
    """Find out how this firmware leaves a built-in pattern.

    Puts the controller into an obvious animation, then tries each escape
    strategy, asking whether the strand became a steady colour.
    """
    require_bleak()
    address = normalize_address(args.address)
    green = protocol.color_frame(0, 255, 0)
    results = {}

    for strategy in protocol.EXIT_PATTERN_STRATEGIES:
        print("\n--- strategy '%s'" % strategy)
        print("  starting a Flash 7 colour pattern ...")
        await write_frames(address, [protocol.power_frame(True),
                                     protocol.mode_frame(0x9A)],
                           args.timeout, verbose=False)
        await asyncio.sleep(3.0)
        escape = protocol.exit_pattern_frames(strategy)
        for frame in escape:
            print("  escape: %s" % frame.hex(" "))
        print("  colour: %s  (asking for steady green)" % green.hex(" "))
        await write_frames(address, escape + [green], args.timeout, verbose=False)
        await asyncio.sleep(2.0)
        try:
            answer = input("  steady green now? [y/n]: ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            print()
            return
        results[strategy] = (answer == "y")

    print("\n%-14s %s" % ("STRATEGY", "WORKED"))
    for strategy, worked in results.items():
        print("%-14s %s" % (strategy, "yes" if worked else "no"))

    winners = [s for s, worked in results.items() if worked]
    if not winners:
        print("\nNone worked. The pattern survives everything we can send, so the")
        print("only reliable escape is cutting power to the controller. Tell me and")
        print("I will look for another frame.")
        return
    best = winners[0]                      # listed cheapest-first
    print("\nCheapest that works: '%s'." % best)
    print("Set \"exit_pattern\": \"%s\" in config.json settings"
          " (current default is power_cycle)." % best)
    if best == "none":
        print("A plain colour frame is enough, so patterns were never the problem.")


def cmd_frames(_args):
    print("solid #ff2d78 :", protocol.color_frame(0xFF, 0x2D, 0x78).hex(" "))
    print("power on      :", protocol.power_frame(True).hex(" "))
    print("power off     :", protocol.power_frame(False).hex(" "))
    print("brightness 60 :", protocol.brightness_frame(60).hex(" "))
    print("speed 50      :", protocol.speed_frame(50).hex(" "))
    print("mode 0x89     :", protocol.mode_frame(0x89).hex(" "))
    print()
    print("build_frames(scale) :",
          protocol.describe_frames(protocol.build_frames(
              {"power": True, "color": "#ff2d78", "brightness": 50}, "scale")))
    print("build_frames(native):",
          protocol.describe_frames(protocol.build_frames(
              {"power": True, "color": "#ff2d78", "brightness": 50}, "native")))
    print()
    print("modes:")
    for value, name in sorted(protocol.MODES.items()):
        print("  0x%02x %s" % (value, name))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=float, default=12.0)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="discover BLE devices")
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--repeat", type=int, default=1,
                   help="run N passes and union the results (finds marginal units)")
    p.add_argument("--elk-only", action="store_true", help="hide non-ELK devices")
    p.set_defaults(fn=cmd_scan, is_async=True)

    p = sub.add_parser("probe", help="list characteristics on one device")
    p.add_argument("address")
    p.set_defaults(fn=cmd_probe, is_async=True)

    p = sub.add_parser("flash", help="red/green/blue/white confirmation blink")
    p.add_argument("address")
    p.set_defaults(fn=cmd_flash, is_async=True)

    p = sub.add_parser("color", help="set a solid colour")
    p.add_argument("address")
    p.add_argument("color")
    p.add_argument("--brightness", type=int, default=100)
    p.add_argument("--brightness-mode", default="scale",
                   choices=list(protocol.BRIGHTNESS_MODES))
    p.set_defaults(fn=cmd_color, is_async=True)

    p = sub.add_parser("mode", help="run a built-in pattern")
    p.add_argument("address")
    p.add_argument("mode", type=lambda v: int(v, 0))
    p.add_argument("--speed", type=int, default=50)
    p.set_defaults(fn=cmd_mode, is_async=True)

    p = sub.add_parser("off", help="power a unit off")
    p.add_argument("address")
    p.add_argument("--variant", default=0,
                   help="off-frame encoding: 0, 1, 2, or 'all' to try each in turn")
    p.set_defaults(fn=cmd_off, is_async=True)

    p = sub.add_parser("all", help="drive every controller in the config at once")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--config", help="config.json to read addresses from")
    p.add_argument("--addresses", nargs="+", help="use these addresses instead")
    p.add_argument("--color", default="#ffffff", help="colour for 'on'")
    p.add_argument("--brightness", type=int, default=100)
    p.add_argument("--variant", type=int, default=0, help="off-frame encoding")
    p.set_defaults(fn=cmd_all, is_async=True)

    p = sub.add_parser("adopt", help="scan and write a starter config.json")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--repeat", type=int, default=3,
                   help="scan passes to union before adopting (default 3)")
    p.add_argument("--elk-only", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--out", default="config.json")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_adopt, is_async=True)

    p = sub.add_parser("identify",
                       help="light each controller in turn so you can name it")
    p.add_argument("--config", help="config.json to read addresses from and write names to")
    p.add_argument("--addresses", nargs="+", help="walk these addresses instead of a config")
    p.add_argument("--color", default="#ff0000", help="colour to light with (default red)")
    p.add_argument("--auto", type=float, default=0.0,
                   help="do not prompt; hold each one lit for N seconds")
    p.add_argument("--start", type=int, default=1, help="resume from the Nth controller")
    p.add_argument("--keep-on", action="store_true", help="leave each one lit when moving on")
    p.add_argument("--all-off-first", action="store_true",
                   help="turn every controller off before starting")
    p.set_defaults(fn=cmd_identify, is_async=True)

    p = sub.add_parser("channels",
                       help="find out if a controller's RGB pads are swapped")
    p.add_argument("address")
    p.add_argument("--config", help="config.json to save the result into")
    p.add_argument("--save", action="store_true", help="write the result to the config")
    p.set_defaults(fn=cmd_channels, is_async=True)

    p = sub.add_parser("unstick",
                       help="find how this firmware leaves a built-in pattern")
    p.add_argument("address")
    p.set_defaults(fn=cmd_unstick, is_async=True)

    p = sub.add_parser("frames", help="print every frame (no radio needed)")
    p.set_defaults(fn=cmd_frames, is_async=False)

    args = parser.parse_args(argv)
    if args.is_async:
        return asyncio.run(args.fn(args))
    return args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
