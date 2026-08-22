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
import re
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


# ------------------------------------------------------------------ connecting
#
# Always find the device before connecting to it, and hand bleak the object it
# found rather than the address string. On BlueZ a bare address resolves through
# bluetoothd's device cache, which is discarded once a device stops being seen,
# so `scan` then `info` a few minutes later connects to a stale entry and hangs
# until the timeout -- the failure looks like a radio problem and is not one.
# Finding it first also turns "not advertising" into its own answer, which is a
# different problem with a different fix.


async def find_device(address, seconds=8.0):
    """The advertising device at this address, or None."""
    try:
        return await BleakScanner.find_device_by_address(address, timeout=seconds)
    except Exception as exc:
        print("scan failed while looking for %s: %s" % (address, exc), file=sys.stderr)
        return None


def _connect_failed(address, exc, device_found):
    """Say what a failed connect means, instead of a traceback."""
    kind = type(exc).__name__
    print("\ncould not connect to %s (%s)" % (address, kind), file=sys.stderr)
    print("", file=sys.stderr)
    if not device_found:
        print("It is not advertising right now. That usually means one of:",
              file=sys.stderr)
        print("  * it is powered off, asleep, or out of range", file=sys.stderr)
        print("  * something else is already connected to it -- these panels", file=sys.stderr)
        print("    stop advertising and refuse a second central. Close the", file=sys.stderr)
        print("    vendor app on your phone, or power-cycle the panel.", file=sys.stderr)
        print("", file=sys.stderr)
        print("  ./matrix_probe.py scan --all      # confirm it is on the air",
              file=sys.stderr)
    else:
        print("It IS advertising, so the radio can hear it -- the connection",
              file=sys.stderr)
        print("itself is what failed. Usually:", file=sys.stderr)
        print("  * something else holds a connection to it (close the app)",
              file=sys.stderr)
        print("  * the access point is on 2.4GHz and is starving Bluetooth.",
              file=sys.stderr)
        print("    This is measured, not theoretical -- see README section 8:",
              file=sys.stderr)
        print("      sudo systemctl stop hostapd     # to test", file=sys.stderr)
        print("      sudo ./scripts/setup_ap_hostapd.sh   # to fix (5GHz)",
              file=sys.stderr)
        print("  * the sign's own service is mid-sweep and owns the adapter:",
              file=sys.stderr)
        print("      sudo systemctl stop vice-lights", file=sys.stderr)
        print("  * BlueZ is wedged:  sudo systemctl restart bluetooth",
              file=sys.stderr)
        print("", file=sys.stderr)
        print("  Then retry with a longer window:", file=sys.stderr)
        print("      ./matrix_probe.py info %s --timeout 25 --retries 3" % address,
              file=sys.stderr)
    return 1


class connected:
    """`async with connected(args) as client` -- found first, retried, explained.

    A context manager rather than a helper function because every caller needs
    the client for the length of a block, and the failure message has to be the
    same wherever the connection is made.
    """

    def __init__(self, address, timeout=15.0, retries=1, quiet=False):
        self.address = address
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.quiet = quiet
        self.client = None
        self.device = None

    async def __aenter__(self):
        need_bleak()
        last = None
        for attempt in range(1, self.retries + 1):
            if not self.quiet:
                print("looking for %s ..." % self.address)
            self.device = await find_device(self.address, seconds=min(self.timeout, 10.0))
            target = self.device or self.address
            if self.device is None and not self.quiet:
                print("  not seen in that window; trying the address anyway")
            if not self.quiet:
                print("connecting%s ..."
                      % ("" if self.device is None else
                         " to %s" % (getattr(self.device, "name", "") or self.address)))
            client = BleakClient(target, timeout=self.timeout)
            try:
                await client.connect()
                self.client = client
                return client
            except Exception as exc:
                last = exc
                try:
                    await client.disconnect()
                except Exception:
                    pass
                if attempt < self.retries:
                    print("  attempt %d/%d failed (%s); retrying"
                          % (attempt, self.retries, type(exc).__name__))
                    await asyncio.sleep(1.5)
        raise SystemExit(_connect_failed(self.address, last, self.device is not None))

    async def __aexit__(self, *_exc):
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
        return False


# ------------------------------------------------------------------ inspection

async def do_info(args):
    need_bleak()
    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
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

        # The connect already found the device; reuse that name rather than
        # scanning again while holding a connection open.
        name = args.name or getattr(holder.device, "name", "") or ""

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



async def do_confirm(args):
    """Walk a panel from "does it hear us at all" up to "does it show text".

    One connection for the whole sequence, in order of how much each step
    assumes. The control commands are short and their framing is well
    established; the text encoder is the part that was written from
    documentation rather than from this hardware. Running them together in one
    go means a failure lands on a specific step instead of on "the driver".

    Anything the panel sends back on its notify characteristic is printed
    against the step that provoked it. A device that answers a good command and
    stays silent on a bad one tells us more than watching the screen does.
    """
    driver = driver_from_args(args)
    char = driver.characteristic()
    print("panel   %s" % args.address)
    print("driver  %s  (%s)"
          % (driver.label, "confirmed" if driver.confirmed else "UNCONFIRMED"))
    print("writing to %s\n" % char)

    steps = [
        ("screen off", "the panel goes dark", driver.power_frames(False), 2.5),
        ("screen on", "the panel lights up again", driver.power_frames(True), 2.5),
        ("brightness 20%", "it dims", driver.brightness_frames(20), 2.5),
        ("brightness 100%", "it goes back to full", driver.brightness_frames(100), 2.5),
        ("text VICE", "VICE appears, in pink",
         driver.text_frames(M.normalize_message({"text": "VICE", "mode": "static",
                                                 "color": "#ff2f6e"})), 6.0),
        ("text scrolling", "BAR IS OPEN scrolls past, in cyan",
         driver.text_frames(M.normalize_message({"text": "BAR IS OPEN",
                                                 "mode": "scroll",
                                                 "color": "#22d3ee"})), 8.0),
    ]
    if args.steps:
        wanted = {int(n) for n in args.steps.split(",") if n.strip().isdigit()}
        steps = [s for i, s in enumerate(steps, 1) if i in wanted]

    replies = []

    def on_notify(_sender, data):
        replies.append(bytes(data))

    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        # Listen on every notify characteristic the panel has. Which one it
        # answers on is itself information.
        services = getattr(client, "services", None)
        if services is None:
            services = await client.get_services()
        listening = []
        for service in services:
            for characteristic in service.characteristics:
                if "notify" in (characteristic.properties or ()):
                    try:
                        await client.start_notify(characteristic.uuid, on_notify)
                        listening.append(characteristic.uuid)
                    except Exception as exc:
                        print("  (could not subscribe to %s: %s)"
                              % (characteristic.uuid, exc))
        if listening:
            print("listening on %s\n" % ", ".join(listening))

        print("Watch the panel. Each step says what it should do.\n")
        results = []
        for index, (name, expect, frames, hold) in enumerate(steps, 1):
            if not frames:
                print("%d. %-16s SKIPPED -- this driver has no such command"
                      % (index, name))
                results.append((name, expect, "no command"))
                continue
            replies.clear()
            print("%d. %-16s -> %s" % (index, name, expect))
            print("      %d frame(s), %d bytes" % (len(frames), sum(len(f) for f in frames)))
            try:
                for position, frame in enumerate(frames):
                    await client.write_gatt_char(char, frame, response=False)
                    if position + 1 < len(frames):
                        await asyncio.sleep(args.delay)
            except Exception as exc:
                print("      WRITE FAILED: %s: %s" % (type(exc).__name__, exc))
                results.append((name, expect, "write failed"))
                continue
            await asyncio.sleep(hold)
            if replies:
                print("      panel replied: %s"
                      % "  ".join(r.hex(" ") for r in replies[:4]))
            # "sent" is not evidence -- a write-without-response always
            # "succeeds". A reply is evidence, so record which happened.
            results.append((name, expect,
                            "answered" if replies else "no reply"))

        for characteristic in listening:
            try:
                await client.stop_notify(characteristic)
            except Exception:
                pass

    print("\n===== what to report back =====")
    for index, (name, expect, outcome) in enumerate(results, 1):
        print("  %d. %-16s %-34s [%s]" % (index, name, expect, outcome))
    answered = [n for n, _e, o in results if o == "answered"]
    if answered and len(answered) < len(results):
        print("\n  The panel answered %d of %d commands. It is not ignoring us,"
              % (len(answered), len(results)))
        print("  so the characteristic is right and the silent ones are the")
        print("  commands it did not understand.")
    elif not answered:
        print("\n  The panel answered nothing. That is not proof of failure --"
              "\n  plenty of these never reply -- so go by what the screen did.")
    print("""
Say which numbers actually happened on the panel. The split matters:

  * 1-4 work, 5-6 do not  -> the family and the framing are right and only
    the text encoder is wrong. That is the good outcome: capture the vendor
    app once and the exact bytes come out of it.
        ./matrix_probe.py btsnoop <capture> --emit-config

  * nothing at all        -> right characteristic, wrong protocol. Same fix,
    and the capture becomes the only route.

  * everything works      -> say so and the driver gets marked confirmed.""")


def _decode_reply(data: bytes) -> str:
    """Read one of this panel's status replies.

    Observed on the sign's own panel: a reply is 05 00 <b2> <b3> <status>,
    echoing the two bytes that followed the length in the packet it is
    answering. Control commands that visibly worked came back with status 01;
    the text command came back 02. So 01 reads as accepted and 02 as
    something else -- which is an inference from four samples, not a
    specification, and is labelled as such wherever it is printed.
    """
    if len(data) != 5 or data[0] != 0x05:
        return "unrecognised shape"
    status = data[4]
    meaning = {0x01: "accepted?", 0x02: "rejected / not understood?"}.get(
        status, "status 0x%02x, meaning unknown" % status)
    return "echoes cmd %02x %02x, %s" % (data[2], data[3], meaning)


async def do_trace(args):
    """Send a payload one chunk at a time, reporting replies per chunk.

    The panel answers, which is worth more than watching it: a payload that
    fails somewhere can be localised instead of guessed at. Does it answer the
    first chunk and ignore the rest? Every chunk? Only the last? Each of those
    means something different about how it reassembles a long write, and none
    of them is visible from the screen.
    """
    driver = driver_from_args(args)
    char = driver.characteristic()

    if args.hex:
        cleaned = re.sub(r"[^0-9a-fA-F]", "", "".join(args.hex))
        if len(cleaned) % 2:
            sys.exit("odd number of hex digits")
        payload = bytes.fromhex(cleaned)
        print("payload from --hex: %d bytes" % len(payload))
    else:
        message = M.normalize_message({"text": args.text, "color": args.color,
                                       "mode": args.mode, "speed": args.speed})
        frames = driver.text_frames(message)
        payload = b"".join(frames)
        print("payload for %r: %d bytes" % (message["text"], len(payload)))
    if args.cmd is not None and len(payload) >= 3:
        payload = payload[:2] + bytes([args.cmd]) + payload[3:]
        print("command byte overridden to 0x%02x" % args.cmd)

    if len(payload) >= 2:
        declared = payload[0] | (payload[1] << 8)
        note = "matches" if declared == len(payload) else "DOES NOT MATCH"
        print("header says %d bytes, payload is %d -- %s"
              % (declared, len(payload), note))

    chunks = [payload[at:at + args.chunk]
              for at in range(0, len(payload), args.chunk)]
    print("%d chunk(s) of up to %d bytes, %.1fs between them\n"
          % (len(chunks), args.chunk, args.gap))

    replies = []

    def on_notify(_sender, data):
        replies.append(bytes(data))

    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        services = getattr(client, "services", None)
        if services is None:
            services = await client.get_services()
        listening = []
        for service in services:
            for characteristic in service.characteristics:
                if "notify" in (characteristic.properties or ()):
                    try:
                        await client.start_notify(characteristic.uuid, on_notify)
                        listening.append(characteristic.uuid)
                    except Exception:
                        pass

        silent_since = None
        for index, chunk in enumerate(chunks, 1):
            replies.clear()
            await client.write_gatt_char(char, chunk, response=False)
            await asyncio.sleep(args.gap)
            head = "%3d/%d  %-3d bytes  %s" % (index, len(chunks), len(chunk),
                                               chunk[:12].hex(" "))
            if replies:
                if silent_since is not None:
                    print("        (silent from chunk %d to %d)"
                          % (silent_since, index - 1))
                    silent_since = None
                for reply in replies:
                    print("%s\n        <- %s   %s"
                          % (head, reply.hex(" "), _decode_reply(reply)))
            else:
                if silent_since is None:
                    silent_since = index
                    print("%s   (no reply)" % head)
        if silent_since is not None and silent_since < len(chunks):
            print("        (silent from chunk %d to %d)"
                  % (silent_since, len(chunks)))

        # Some panels only answer once the declared length has arrived.
        print("\nwaiting %.0fs for anything late ..." % args.settle)
        replies.clear()
        await asyncio.sleep(args.settle)
        if replies:
            for reply in replies:
                print("  late <- %s   %s" % (reply.hex(" "), _decode_reply(reply)))
        else:
            print("  nothing further")

        for characteristic in listening:
            try:
                await client.stop_notify(characteristic)
            except Exception:
                pass

    print("""
Reading it:
  * answers chunk 1, then silent  -> it took the header and lost the rest.
    The chunks after the first probably need their own framing rather than
    being raw continuation.
  * answers every chunk           -> each chunk is a packet in its own right.
  * silent until the last chunk   -> it reassembles by the declared length,
    and the final status is the verdict on the whole payload.
  * no reply at all               -> the command byte is not one it knows.""")

# -------------------------------------------------------------------- sending

async def write_frames(address, char_uuid, frames, timeout, delay, response=False,
                       retries=1):
    need_bleak()
    total = sum(len(f) for f in frames)
    print("writing %d frame(s), %d bytes to %s on %s" % (len(frames), total, address, char_uuid))
    async with connected(address, timeout, retries) as client:
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
                       args.timeout, args.delay, retries=args.retries)


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
                       args.timeout, args.delay, retries=args.retries)


async def do_raw(args):
    if not args.char:
        sys.exit("--char is required: run 'info' to see the writable characteristics")
    cleaned = "".join(args.hex).replace(":", "").replace(" ", "")
    if len(cleaned) % 2:
        sys.exit("odd number of hex digits")
    payload = bytes.fromhex(cleaned)
    frames = [payload[at:at + args.chunk] for at in range(0, len(payload), args.chunk)]
    await write_frames(args.address, args.char, frames, args.timeout, args.delay,
                       response=args.response, retries=args.retries)


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
        p.add_argument("--retries", type=int, default=2,
                       help="connection attempts before giving up (default 2)")
        p.add_argument("--delay", type=float, default=0.02,
                       help="seconds between frames (default 0.02)")
        p.add_argument("-c", "--char", help="characteristic UUID to write to")
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
    p.add_argument("--retries", type=int, default=2,
                   help="connection attempts before giving up (default 2)")
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

    p = sub.add_parser("confirm",
                       help="run a panel through every command, in order of risk")
    ble_args(p)
    p.add_argument("--steps", default="",
                   help="only these step numbers, e.g. 1,2,5")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_confirm(a)))

    p = sub.add_parser("trace",
                       help="send a payload chunk by chunk, reporting each reply")
    ble_args(p)
    p.add_argument("-t", "--text", default="VICE")
    p.add_argument("--color", default="#ff2f6e")
    p.add_argument("--mode", choices=M.MODES, default="static")
    p.add_argument("--speed", type=int, default=50)
    p.add_argument("-x", "--hex", nargs="+",
                   help="send these bytes instead of an encoded message")
    p.add_argument("--cmd", type=lambda v: int(v, 0),
                   help="override the command byte at offset 2")
    p.add_argument("--gap", type=float, default=0.35,
                   help="seconds to wait after each chunk (default 0.35)")
    p.add_argument("--settle", type=float, default=4.0,
                   help="seconds to wait for a late reply (default 4)")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_trace(a)))

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
    except BrokenPipeError:
        # `btsnoop ... | head` is the normal way to read a long capture, and a
        # traceback on a closed pipe reads like the decode failed.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
