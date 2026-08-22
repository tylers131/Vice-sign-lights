#!/usr/bin/env python3
"""Work out what the LED matrix panel is, and how to talk to it.

These panels are sold under a dozen brands with no common protocol, so the
driver in ``vicelights/matrix.py`` has to be chosen from evidence rather than
assumed.  This tool gathers the evidence:

    ./matrix_probe.py scan                  what is advertising nearby
    ./matrix_probe.py info AA:BB:..         connect, dump the GATT tree
    ./matrix_probe.py send AA:BB:.. -t HI   try a family's text encoding
    ./matrix_probe.py raw AA:BB:.. -c UUID -x 05000701 01
    ./matrix_probe.py render "BAR IS OPEN"  what our font makes of it
    ./matrix_probe.py btsnoop capture.log   read the vendor app's protocol

The last one is the way out of a dead end.  If no driver fits, run the panel's
own phone app once with Android's "Bluetooth HCI snoop log" developer option
on, pull the capture, and this decodes exactly which bytes the app wrote to
which characteristic -- which is the protocol, not a guess at it.  Feed the
result back with --emit-config and the "raw" driver runs it with no code
change at all.

Everything except scan/info/send/raw works with no radio and no bleak, so a
capture can be decoded on a laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vicelights import matrix as M   # noqa: E402

try:
    from bleak import BleakClient, BleakScanner
    HAVE_BLEAK = True
except Exception:                    # pragma: no cover - laptop use
    BleakClient = BleakScanner = None
    HAVE_BLEAK = False

# The same problem elk_scan.py already solved: bleak lives in the service's
# virtualenv, not in /usr/bin/python3, so the shebang picks an interpreter that
# cannot see it. Reuse that machinery rather than re-deriving it -- and reuse
# its message too, which points at the interpreter instead of suggesting an
# install that has already happened.
try:
    from elk_scan import reexec_with_venv, require_bleak as _require_bleak
except Exception:                    # pragma: no cover - decoding on a laptop
    reexec_with_venv = None
    _require_bleak = None


# Where the previous scan is kept, so a second scan can say what changed. A
# crowded band means 40+ rows a pass, and picking the one new line out by eye is
# the step most likely to go wrong -- the panel hides in plain sight.
SCAN_CACHE = "/var/lib/vice-lights/last-scan.json"


def _scan_cache_path():
    for candidate in (SCAN_CACHE,
                      os.path.expanduser("~/.cache/vice-lights-last-scan.json"),
                      "/tmp/vice-lights-last-scan.json"):
        directory = os.path.dirname(candidate)
        if os.path.isdir(directory) and os.access(directory, os.W_OK):
            return candidate
        try:
            os.makedirs(directory, exist_ok=True)
            return candidate
        except OSError:
            continue
    return None


def _load_last_scan():
    path = _scan_cache_path()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_last_scan(rows):
    path = _scan_cache_path()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"addresses": {r["address"]: r["name"] for r in rows}}, handle)
    except Exception:
        pass


def need_bleak():
    if HAVE_BLEAK:
        return
    if _require_bleak is not None:
        _require_bleak()
    sys.exit("this needs bleak: pip3 install bleak (or run it on the Pi)")


# ------------------------------------------------------------------- scanning

async def do_scan(args):
    need_bleak()
    print("scanning %.0fs ..." % args.seconds)
    try:
        found = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
    except TypeError:
        found = await BleakScanner.discover(timeout=args.seconds)
    entries = found.values() if isinstance(found, dict) else found

    rows = []
    for entry in entries:
        device, adv = entry if isinstance(entry, (tuple, list)) else (entry, None)
        name = (getattr(adv, "local_name", None) or getattr(device, "name", "") or "").strip()
        rssi = getattr(adv, "rssi", None)
        if rssi is None:
            rssi = getattr(device, "rssi", None)
        services = list(getattr(adv, "service_uuids", None) or [])
        family = M.identify(name, service_uuids=services)
        rows.append({
            "address": device.address.upper(),
            "name": name,
            "rssi": rssi,
            "family": family,
            "panel": bool(family) or M.looks_like_panel(name),
            "services": services,
        })

    from vicelights.protocol import looks_like_elk

    previous = None if args.forget else _load_last_scan()
    seen_before = set((previous or {}).get("addresses") or {})
    for row in rows:
        row["new"] = bool(seen_before) and row["address"] not in seen_before
    _save_last_scan(rows)

    rows.sort(key=lambda r: (not r["new"], -(r["rssi"] or -999)))
    print("\n%-18s %-26s %5s  %-4s %s"
          % ("ADDRESS", "NAME", "RSSI", "NEW", "VERDICT"))
    print("-" * 84)
    hidden = 0
    for row in rows:
        if looks_like_elk(row["name"]):
            verdict = "ELK-BLEDOM controller"
        elif row["family"]:
            verdict = "PANEL -> %s" % row["family"]
        elif row["panel"]:
            verdict = "panel? (unknown family)"
        elif args.all:
            verdict = ""
        else:
            hidden += 1
            continue
        if row["new"] and not verdict:
            verdict = "appeared since the last scan"
        print("%-18s %-26s %5s  %-4s %s"
              % (row["address"], row["name"][:26] or "(no name)",
                 row["rssi"] if row["rssi"] is not None else "?",
                 "NEW" if row["new"] else "", verdict))
    candidates = [r for r in rows if r["panel"]]
    fresh = [r for r in rows if r["new"]]
    print("\n%d device(s) seen, %d look like a text panel." % (len(rows), len(candidates)))
    if seen_before:
        print("%d of them were not in the previous scan." % len(fresh))
    else:
        print("No previous scan to compare against -- this one is the baseline "
              "now.\nPower the panel on and run this again to see what appears.")
    # Plenty of these panels advertise as a bare MAC, a hex blob, or a generic
    # word, and the name filter cannot help with those -- so never let the
    # default view imply the panel was not there.
    if hidden:
        print("%d device(s) hidden by the name filter. If the panel is not "
              "listed above,\nre-run with --all -- it may be advertising under "
              "something meaningless." % hidden)
    if candidates:
        print("Next: ./matrix_probe.py info %s" % candidates[0]["address"])
    elif fresh:
        print("\nNothing matched by name, but these appeared since the last scan:")
        for row in fresh[:5]:
            print("  ./matrix_probe.py info %-18s # %s, rssi %s"
                  % (row["address"], row["name"] or "(no name)", row["rssi"]))
    else:
        print("\nNothing obvious. To find a panel advertising under a "
              "meaningless name:\n"
              "  1. power the panel OFF, then: ./matrix_probe.py scan --all\n"
              "  2. power it ON, wait 10s, then run the same command again\n"
              "  3. the second run marks anything new with NEW\n"
              "Then: ./matrix_probe.py info <address>")


# ------------------------------------------------------------------ inspection

async def do_info(args):
    need_bleak()
    print("connecting to %s ..." % args.address)
    async with BleakClient(args.address, timeout=args.timeout) as client:
        services = getattr(client, "services", None)
        if services is None:
            services = await client.get_services()
        char_uuids, writable = [], []
        print("\nGATT tree  (handle is what an HCI capture reports)")
        print("-" * 76)
        for service in services:
            print("service %s  %s" % (service.uuid, service.description or ""))
            for char in service.characteristics:
                props = ",".join(sorted(char.properties or ()))
                print("    char %s  handle 0x%04x  [%s]" % (char.uuid, char.handle, props))
                char_uuids.append(char.uuid)
                if "write" in (char.properties or ()) or \
                        "write-without-response" in (char.properties or ()):
                    writable.append((char.uuid, char.handle, props))

        name = args.name or ""
        if not name:
            try:
                found = await BleakScanner.find_device_by_address(args.address, timeout=5.0)
                name = getattr(found, "name", "") or ""
            except Exception:
                pass

        family = M.identify(name, char_uuids=char_uuids,
                            service_uuids=[s.uuid for s in services])
        print("\nfingerprint")
        print("-" * 76)
        print("  advertised name : %s" % (name or "(unknown)"))
        print("  writable chars  : %s" % (", ".join(u for u, _h, _p in writable) or "none"))
        print("  matched family  : %s" % (family or "none of the known families"))
        if family:
            driver = M.FAMILIES[family]({})
            print("  driver          : %s (%s)"
                  % (driver.label, "confirmed on hardware" if driver.confirmed
                     else "encoding NOT yet confirmed -- test it below"))
            print("\nPair it:")
            print('  curl -s -X POST http://127.0.0.1/api/matrix -H "Content-Type: application/json" \\')
            print('    -d \'{"enabled":true,"address":"%s","name":"%s","family":"%s"}\''
                  % (args.address, name, family))
            print("\nTest it before trusting it:")
            print("  ./matrix_probe.py send %s --family %s --text VICE" % (args.address, family))
        else:
            print("\nNo driver matches. Capture the vendor app instead:")
            print("  1. Android: Developer options -> Enable Bluetooth HCI snoop log")
            print("  2. Send one message from the panel's own app")
            print("  3. Pull the log (adb bugreport, or Settings -> ... -> capture)")
            print("  4. ./matrix_probe.py btsnoop <file> --emit-config")
            if writable:
                print("\n  Writes in that capture will name a handle. From the tree above:")
                for uuid, handle, _props in writable:
                    print("    handle 0x%04x = %s" % (handle, uuid))


# -------------------------------------------------------------------- sending

async def write_frames(address, char_uuid, frames, timeout, delay, response=False):
    need_bleak()
    total = sum(len(f) for f in frames)
    print("writing %d frame(s), %d bytes to %s on %s" % (len(frames), total, address, char_uuid))
    async with BleakClient(address, timeout=timeout) as client:
        for index, frame in enumerate(frames, 1):
            print("  %2d/%d  %s" % (index, len(frames), frame.hex(" ")))
            await client.write_gatt_char(char_uuid, frame, response=response)
            if index < len(frames):
                await asyncio.sleep(delay)
    print("done -- look at the panel.")


def driver_from_args(args):
    config = {"family": args.family or "auto", "name": args.name or "",
              "char_uuid": args.char or "", "chunk": args.chunk}
    if args.family == "raw" and args.commands:
        config["commands"] = json.loads(args.commands)
    driver = M.driver_for(config)
    if not driver.characteristic():
        sys.exit("no characteristic known for this panel; pass --char, "
                 "or run 'info' first to find one")
    return driver


async def do_send(args):
    driver = driver_from_args(args)
    message = M.normalize_message({"text": args.text, "color": args.color,
                                   "mode": args.mode, "speed": args.speed})
    print(M.preview(message["text"]))
    print()
    frames = driver.text_frames(message)
    await write_frames(args.address, driver.characteristic(), frames,
                       args.timeout, args.delay)


async def do_control(args):
    driver = driver_from_args(args)
    if args.what == "on":
        frames = driver.power_frames(True)
    elif args.what == "off":
        frames = driver.power_frames(False)
    elif args.what == "clear":
        frames = driver.clear_frames()
    else:
        frames = driver.brightness_frames(int(args.what))
    if not frames:
        sys.exit("this driver has no command for %r" % args.what)
    await write_frames(args.address, driver.characteristic(), frames,
                       args.timeout, args.delay)


async def do_raw(args):
    if not args.char:
        sys.exit("--char is required: run 'info' to see the writable characteristics")
    cleaned = "".join(args.hex).replace(":", "").replace(" ", "")
    if len(cleaned) % 2:
        sys.exit("odd number of hex digits")
    payload = bytes.fromhex(cleaned)
    frames = [payload[at:at + args.chunk] for at in range(0, len(payload), args.chunk)]
    await write_frames(args.address, args.char, frames, args.timeout, args.delay,
                       response=args.response)


def do_render(args):
    text = args.text
    print(M.preview(text))
    print("\n%d characters, %d columns wide, %d rows tall"
          % (len(text), M.text_width(text), M.FONT_HEIGHT))
    if args.width:
        fits = M.text_width(text) <= args.width
        print("panel is %d wide: %s" % (args.width,
                                        "fits" if fits else "will need to scroll"))


def do_families(_args):
    print("%-14s %-26s %s" % ("KEY", "PANEL", "ENCODING"))
    print("-" * 68)
    for entry in M.family_names():
        print("%-14s %-26s %s" % (entry["key"], entry["label"],
                                  "confirmed" if entry["confirmed"] else "not yet confirmed"))


# ------------------------------------------------------------- btsnoop decoding
#
# btsnoop is a documented format and Android writes it verbatim, so this is a
# decode rather than a guess: 16-byte big-endian record headers, then an H4
# packet whose first byte is the HCI packet type. We care about ACL data
# (0x02) carrying L2CAP CID 0x0004, which is ATT -- and within ATT, the writes.

BTSNOOP_MAGIC = b"btsnoop\x00"

ATT_WRITE_REQUEST = 0x12
ATT_WRITE_COMMAND = 0x52
ATT_PREPARE_WRITE = 0x16
ATT_NOTIFY = 0x1B
ATT_INDICATE = 0x1D
ATT_READ_BY_TYPE_RSP = 0x09
ATT_MTU_REQUEST = 0x02
ATT_MTU_RESPONSE = 0x03

UUID_CHARACTERISTIC = 0x2803


def _uuid_str(raw: bytes) -> str:
    """A 2- or 16-byte ATT UUID as the long form bleak reports."""
    if len(raw) == 2:
        return "0000%04x-0000-1000-8000-00805f9b34fb" % struct.unpack("<H", raw)[0]
    if len(raw) == 16:
        hexed = raw[::-1].hex()
        return "%s-%s-%s-%s-%s" % (hexed[0:8], hexed[8:12], hexed[12:16],
                                   hexed[16:20], hexed[20:32])
    return raw.hex()


def read_btsnoop(path: str):
    """Yield (direction, payload) for every record. direction: 'tx' or 'rx'."""
    with open(path, "rb") as handle:
        header = handle.read(16)
        if len(header) < 16 or not header.startswith(BTSNOOP_MAGIC):
            raise SystemExit("%s is not a btsnoop capture (no 'btsnoop' magic)" % path)
        version, datalink = struct.unpack(">II", header[8:16])
        if version != 1:
            print("warning: btsnoop version %d, expected 1" % version, file=sys.stderr)
        if datalink not in (1001, 1002, 1003, 1004):
            print("warning: unexpected datalink type %d" % datalink, file=sys.stderr)
        while True:
            record = handle.read(24)
            if len(record) < 24:
                return
            _original, included, flags, _drops, _stamp = struct.unpack(">IIIIq", record)
            payload = handle.read(included)
            if len(payload) < included:
                return
            # Flags bit 0: 0 = host to controller (what the app sent), 1 = back.
            yield ("rx" if flags & 0x01 else "tx"), payload


def decode_att(path: str):
    """Pull ATT operations out of a capture, reassembling fragmented ACL.

    Returns (operations, handle_to_uuid). Operations are dicts with direction,
    opcode, handle and value.
    """
    pending = {}          # acl handle -> [expected_l2cap_len, buffer]
    operations = []
    handle_uuid = {}

    for direction, packet in read_btsnoop(path):
        if not packet:
            continue
        # H4: a leading packet-type byte. Some writers omit it; if the first
        # byte is not a known type, assume the record is already an ACL frame.
        if packet[0] in (0x01, 0x02, 0x03, 0x04, 0x05):
            kind, body = packet[0], packet[1:]
        else:
            kind, body = 0x02, packet
        if kind != 0x02 or len(body) < 4:
            continue
        handle_flags, acl_len = struct.unpack("<HH", body[:4])
        acl_handle = handle_flags & 0x0FFF
        pb = (handle_flags >> 12) & 0x03
        data = body[4:4 + acl_len]

        if pb == 0x01 and acl_handle in pending:        # continuation
            pending[acl_handle][1] += data
        else:
            if len(data) < 4:
                continue
            l2cap_len, cid = struct.unpack("<HH", data[:4])
            if cid != 0x0004:
                pending.pop(acl_handle, None)
                continue
            pending[acl_handle] = [l2cap_len, bytearray(data[4:])]

        expected, buffer = pending.get(acl_handle, (None, None))
        if buffer is None or len(buffer) < expected:
            continue
        att = bytes(buffer[:expected])
        pending.pop(acl_handle, None)
        if not att:
            continue

        opcode = att[0]
        if opcode in (ATT_WRITE_REQUEST, ATT_WRITE_COMMAND, ATT_NOTIFY, ATT_INDICATE):
            if len(att) < 3:
                continue
            value_handle = struct.unpack("<H", att[1:3])[0]
            operations.append({"direction": direction, "opcode": opcode,
                               "handle": value_handle, "value": att[3:]})
        elif opcode == ATT_PREPARE_WRITE and len(att) >= 5:
            value_handle, offset = struct.unpack("<HH", att[1:5])
            operations.append({"direction": direction, "opcode": opcode,
                               "handle": value_handle, "offset": offset,
                               "value": att[5:]})
        elif opcode == ATT_READ_BY_TYPE_RSP and len(att) >= 2:
            # Characteristic declarations, which is how a capture can name its
            # own handles: [props(1)][value handle(2)][uuid(2 or 16)].
            size = att[1]
            for at in range(2, len(att) - size + 1, size):
                pair = att[at:at + size]
                if len(pair) < 6:
                    continue
                value_handle = struct.unpack("<H", pair[3:5])[0]
                handle_uuid[value_handle] = _uuid_str(pair[5:])
        elif opcode in (ATT_MTU_REQUEST, ATT_MTU_RESPONSE) and len(att) >= 3:
            operations.append({"direction": direction, "opcode": opcode,
                               "handle": None,
                               "mtu": struct.unpack("<H", att[1:3])[0]})
    return operations, handle_uuid


OPCODE_NAMES = {
    ATT_WRITE_REQUEST: "write-request", ATT_WRITE_COMMAND: "write-command",
    ATT_PREPARE_WRITE: "prepare-write", ATT_NOTIFY: "notification",
    ATT_INDICATE: "indication", ATT_MTU_REQUEST: "mtu-request",
    ATT_MTU_RESPONSE: "mtu-response",
}


def do_btsnoop(args):
    operations, handle_uuid = decode_att(args.file)
    writes = [op for op in operations
              if op["opcode"] in (ATT_WRITE_REQUEST, ATT_WRITE_COMMAND, ATT_PREPARE_WRITE)
              and op["direction"] == "tx"]
    if args.handle is not None:
        writes = [op for op in writes if op["handle"] == args.handle]

    mtus = [op["mtu"] for op in operations if op.get("mtu")]
    if mtus:
        print("negotiated ATT MTU: %s  (payload per write is MTU-3)"
              % ", ".join(str(m) for m in sorted(set(mtus))))

    by_handle = {}
    for op in writes:
        by_handle.setdefault(op["handle"], []).append(op)

    if not writes:
        print("No outbound ATT writes in %s.\n"
              "Either the capture predates the app's session, or the app talked\n"
              "to the panel over something other than GATT." % args.file)
        return

    print("\n%-8s %-42s %6s %8s" % ("HANDLE", "CHARACTERISTIC", "WRITES", "BYTES"))
    print("-" * 70)
    for handle, ops in sorted(by_handle.items(), key=lambda kv: -len(kv[1])):
        print("0x%04x   %-42s %6d %8d"
              % (handle, handle_uuid.get(handle, "(not in this capture)"),
                 len(ops), sum(len(op["value"]) for op in ops)))

    target = args.handle
    if target is None:
        target = max(by_handle, key=lambda h: sum(len(op["value"]) for op in by_handle[h]))
        print("\nBusiest handle is 0x%04x -- showing that one. "
              "Use --handle to pick another." % target)

    chosen = by_handle.get(target, [])
    print("\n%d write(s) to handle 0x%04x:" % (len(chosen), target))
    print("-" * 70)
    for index, op in enumerate(chosen, 1):
        value = op["value"]
        if args.limit and index > args.limit:
            print("  ... %d more (raise --limit to see them)" % (len(chosen) - args.limit))
            break
        shown = value if len(value) <= 32 or args.full else value[:32]
        suffix = "" if len(shown) == len(value) else "  (+%d bytes)" % (len(value) - len(shown))
        print("  %3d  %-14s %s%s" % (index, OPCODE_NAMES.get(op["opcode"], "?"),
                                     shown.hex(" "), suffix))

    if args.emit_config:
        uuid = handle_uuid.get(target) or args.char or "PUT-THE-UUID-HERE"
        joined = b"".join(op["value"] for op in chosen)
        block = {
            "family": "raw",
            "char_uuid": uuid,
            "chunk": (max(len(op["value"]) for op in chosen) if chosen else 20),
            "commands": {"text": [joined.hex()]},
        }
        print("\nPaste into the panel's config (or POST it to /api/matrix):")
        print(json.dumps(block, indent=2))
        if block["chunk"] > 20:
            print("\nNote: chunk %d only works if BlueZ negotiates an MTU of at least %d.\n"
                  "If the writes fail on the Pi, set chunk to 20 and try again -- most\n"
                  "of these panels accept a payload split across several writes."
                  % (block["chunk"], block["chunk"] + 3))
        print("\nThat replays the captured message exactly -- proof the wiring works.\n"
              "Then capture two DIFFERENT messages: the bytes that differ are the\n"
              "text payload, the bytes that do not are the framing, and those go\n"
              "into 'text_prefix' / 'text_suffix' so any message can be built.")


def do_diff(args):
    """Compare two captures to separate framing from payload."""
    first, _ = decode_att(args.first)
    second, _ = decode_att(args.second)

    def blob(ops, handle):
        chosen = [op for op in ops
                  if op["direction"] == "tx"
                  and op["opcode"] in (ATT_WRITE_REQUEST, ATT_WRITE_COMMAND)
                  and (handle is None or op["handle"] == handle)]
        return b"".join(op["value"] for op in chosen)

    a, b = blob(first, args.handle), blob(second, args.handle)
    if not a or not b:
        sys.exit("one of the captures has no writes to compare")

    head = 0
    while head < min(len(a), len(b)) and a[head] == b[head]:
        head += 1
    tail = 0
    while (tail < min(len(a), len(b)) - head and a[-1 - tail] == b[-1 - tail]):
        tail += 1

    print("capture A: %d bytes\ncapture B: %d bytes" % (len(a), len(b)))
    print("\ncommon prefix : %d bytes  %s" % (head, a[:head].hex(" ") or "(none)"))
    print("common suffix : %d bytes  %s" % (tail, (a[len(a) - tail:].hex(" ") if tail else "(none)")))
    print("\nA payload : %s" % a[head:len(a) - tail].hex(" "))
    print("B payload : %s" % b[head:len(b) - tail].hex(" "))
    print("\nIf the prefix contains a byte that tracks total length, it will differ\n"
          "between the two -- look just before where the prefix ends.")
    if args.emit_config:
        print("\n" + json.dumps({
            "family": "raw",
            "commands": {"text_prefix": [a[:head].hex()],
                         "text_suffix": [a[len(a) - tail:].hex()] if tail else []},
        }, indent=2))


# ----------------------------------------------------------------------- main

def build_parser():
    parser = argparse.ArgumentParser(
        prog="matrix_probe.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def ble_args(p):
        p.add_argument("address")
        p.add_argument("--timeout", type=float, default=15.0)
        p.add_argument("--delay", type=float, default=0.02,
                       help="seconds between frames (default 0.02)")
        p.add_argument("--char", help="characteristic UUID to write to")
        p.add_argument("--family", choices=sorted(M.FAMILIES) + ["auto"], default="auto")
        p.add_argument("--name", default="", help="advertised name, to help auto-detect")
        p.add_argument("--chunk", type=int, default=20)

    p = sub.add_parser("scan", help="list nearby BLE devices and guess which is the panel")
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--all", action="store_true", help="show every device, not just candidates")
    p.add_argument("--forget", action="store_true",
                   help="ignore the remembered scan and start a fresh baseline")
    p.set_defaults(run=lambda a: asyncio.run(do_scan(a)))

    p = sub.add_parser("info", help="connect and dump the GATT tree")
    p.add_argument("address")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--name", default="")
    p.set_defaults(run=lambda a: asyncio.run(do_info(a)))

    p = sub.add_parser("send", help="send a test message")
    ble_args(p)
    p.add_argument("-t", "--text", required=True)
    p.add_argument("--color", default="#ff2f6e")
    p.add_argument("--mode", choices=M.MODES, default="scroll")
    p.add_argument("--speed", type=int, default=50)
    p.add_argument("--commands", help="JSON command table, for --family raw")
    p.set_defaults(run=lambda a: asyncio.run(do_send(a)))

    p = sub.add_parser("control", help="on | off | clear | a brightness percentage")
    ble_args(p)
    p.add_argument("what")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_control(a)))

    p = sub.add_parser("raw", help="write arbitrary bytes to a characteristic")
    ble_args(p)
    p.add_argument("-x", "--hex", nargs="+", required=True)
    p.add_argument("--response", action="store_true", help="use write-with-response")
    p.set_defaults(run=lambda a: asyncio.run(do_raw(a)))

    p = sub.add_parser("render", help="show what our font makes of a message")
    p.add_argument("text")
    p.add_argument("--width", type=int, help="panel width in pixels, to check it fits")
    p.set_defaults(run=do_render)

    p = sub.add_parser("families", help="list the drivers and whether they are confirmed")
    p.set_defaults(run=do_families)

    p = sub.add_parser("btsnoop", help="decode ATT writes from an Android HCI capture")
    p.add_argument("file")
    p.add_argument("--handle", type=lambda v: int(v, 0),
                   help="only this ATT handle (default: the busiest)")
    p.add_argument("--char", help="the UUID for that handle, if the capture lacks discovery")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--full", action="store_true", help="do not truncate long writes")
    p.add_argument("--emit-config", action="store_true",
                   help="print a config block for the raw driver")
    p.set_defaults(run=do_btsnoop)

    p = sub.add_parser("diff", help="compare two captures to find the framing")
    p.add_argument("first")
    p.add_argument("second")
    p.add_argument("--handle", type=lambda v: int(v, 0))
    p.add_argument("--emit-config", action="store_true")
    p.set_defaults(run=do_diff)

    return parser


def main(argv=None):
    # Before parsing, so --help and the offline subcommands still work on a
    # laptop, and so a re-exec carries the whole command line across.
    if reexec_with_venv is not None:
        reexec_with_venv()
    args = build_parser().parse_args(argv)
    try:
        args.run(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
