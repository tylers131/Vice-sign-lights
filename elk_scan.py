#!/usr/bin/env python3
"""elk_scan -- discover ELK-BLEDOM controllers and confirm the command bytes.

Standalone CLI. It shares ``vicelights/protocol.py`` with the service, so the
frames you verify here are byte-for-byte the frames the service sends.

    ./elk_scan.py scan                    # list BLE devices, ELK units first
    ./elk_scan.py probe BE:FF:00:11:22:33 # list services/characteristics, pick one
    ./elk_scan.py flash BE:FF:00:11:22:33 # red/green/blue confirmation blink
    ./elk_scan.py color BE:FF:.. '#ff2d78' --brightness 60
    ./elk_scan.py off   BE:FF:..
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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vicelights import protocol  # noqa: E402

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover
    BleakClient = BleakScanner = None


def require_bleak():
    if BleakScanner is None:
        sys.exit("bleak is not installed. pip install -r requirements.txt")


async def cmd_scan(args):
    require_bleak()
    print("scanning %.0fs ..." % args.seconds)
    seen = []
    try:
        discovered = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
        for device, adv in discovered.values():
            seen.append((device.address.upper(), (adv.local_name or device.name or "").strip(),
                         adv.rssi))
    except TypeError:
        for device in await BleakScanner.discover(timeout=args.seconds):
            seen.append((device.address.upper(), (device.name or "").strip(),
                         getattr(device, "rssi", None)))

    seen.sort(key=lambda row: (not protocol.looks_like_elk(row[1]), -(row[2] or -999)))
    print("%-20s %-6s %-28s %s" % ("ADDRESS", "RSSI", "NAME", ""))
    for address, name, rssi in seen:
        mark = "<- ELK-BLEDOM" if protocol.looks_like_elk(name) else ""
        print("%-20s %-6s %-28s %s" % (address, rssi if rssi is not None else "?",
                                       name or "(no name)", mark))
    elk = [row for row in seen if protocol.looks_like_elk(row[1])]
    print("\n%d device(s), %d look like ELK-BLEDOM" % (len(seen), len(elk)))
    return seen


async def cmd_probe(args):
    require_bleak()
    print("connecting to %s ..." % args.address)
    async with BleakClient(args.address, timeout=args.timeout) as client:
        services = getattr(client, "services", None) or client.get_services()
        collection = list(services)
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
        services = getattr(client, "services", None) or client.get_services()
        uuid, without_response = protocol.pick_characteristic(list(services))
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
    await write_frames(args.address, [protocol.power_frame(False)], args.timeout)


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
    p.set_defaults(fn=cmd_off, is_async=True)

    p = sub.add_parser("adopt", help="scan and write a starter config.json")
    p.add_argument("--seconds", type=float, default=10.0)
    p.add_argument("--out", default="config.json")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_adopt, is_async=True)

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
