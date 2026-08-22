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
import time
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


async def _stream(client, char, frames, args, label="writing"):
    """Send many packets as few writes as the MTU allows, and report the rate.

    An acknowledged write costs a round trip whether it carries one ten-byte
    packet or twenty, so combining them is most of the difference between a
    message appearing in five seconds and in one. Whole packets only -- the
    protocol is length-prefixed, so a device can take several from one write,
    but a packet split across two is garbage.
    """
    from vicelights.ble import attribute_mtu, pack_frames

    ack = _ack(args)
    mtu = attribute_mtu(client)
    writes = frames if getattr(args, "no_batch", False) else pack_frames(frames, mtu)
    print("%s: %d packet(s) in %d write(s), MTU %d%s"
          % (label, len(frames), len(writes), mtu,
             "" if len(writes) == len(frames)
             else "  (%.1fx fewer round trips)" % (len(frames) / len(writes))))
    started = time.monotonic()
    for index, chunk in enumerate(writes, 1):
        await client.write_gatt_char(char, chunk, response=ack)
        if index % 50 == 0:
            print("  %d/%d" % (index, len(writes)))
        if args.delay:
            await asyncio.sleep(args.delay)
    elapsed = time.monotonic() - started
    print("  %d write(s) in %.1fs (%.0f/s)"
          % (len(writes), elapsed, len(writes) / elapsed if elapsed else 0))


def _ack(args) -> bool:
    """Should writes be acknowledged?

    Yes, by default, and it is not a small preference. A write without a
    response has no flow control: outrun the device and packets vanish with no
    error at either end. On this panel that appeared as a few LEDs missing from
    each message, different ones every time -- which looks like a rendering bug
    and is not one. --no-ack restores the fast, lossy behaviour for cases where
    losing a packet does not matter.
    """
    return not getattr(args, "no_ack", False)


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
    ack = _ack(args)
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
    ]
    # Each message erases what the one before it lit. Without this the two
    # texts end up superimposed on the panel, which reads as a colour fault.
    # Ends on the large message on purpose. A long message drops to 1x to fit,
    # so finishing with one leaves the panel showing the smallest thing this
    # can draw -- which reads as "scaling does nothing" when it is the opposite.
    long_message = M.normalize_message({"text": "BAR IS OPEN", "mode": "scroll",
                                        "color": "#22d3ee"})
    big = M.normalize_message({"text": "VICE", "mode": "static",
                               "color": "#ff2f6e"})
    steps += [
        ("long text", "BAR IS OPEN, small -- 11 letters only fit at 1x",
         driver.text_frames(long_message), 7.0),
        ("short text", "VICE, twice the height -- it fits, so it goes bigger",
         driver.text_frames(big, previous=long_message), 8.0),
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
                    await client.write_gatt_char(char, frame, response=ack)
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


def _short_uuid(uuid: str) -> str:
    """The 16-bit part of a Bluetooth base UUID, which is all that varies."""
    text = str(uuid).lower()
    if text.endswith("-0000-1000-8000-00805f9b34fb"):
        return text[4:8]
    return text[:8]


def _looks_random(block: bytes) -> bool:
    """Does this block look like ciphertext rather than fields?

    Crude on purpose, and only ever used to label output -- but the difference
    between "a reply we cannot parse" and "this channel is encrypted" is the
    difference between keep probing and stop, so it is worth getting roughly
    right rather than not saying it.

    Three tests, all of which structured data tends to fail:
      * nearly every byte distinct -- a status or an id repeats values
      * a real spread above 0x7f -- counters, ASCII and small integers do not
      * not an arithmetic run -- 01 02 03 04 has distinct bytes and is not
        random at all
    """
    if len(block) < 8:
        return False
    if len(set(block)) < len(block) * 0.8:
        return False
    if block.count(0) > 1:
        return False
    if sum(1 for byte in block if byte >= 0x80) < len(block) * 0.2:
        return False
    steps = {block[i + 1] - block[i] for i in range(len(block) - 1)}
    if len(steps) <= 2:                      # a ramp, not a cipher
        return False
    return True


def _decode_reply(data: bytes) -> str:
    """Read one of this panel's replies.

    Two shapes have been seen on this sign's panel, on two different notify
    characteristics:

    * ``05 00 <b2> <b3> <status>`` on fa03, echoing the two bytes that followed
      the length in the packet being answered. Control commands came back 01
      and the text command came back 02, so 01 reads as accepted and 02 as
      something else. That is an inference from a handful of samples, not a
      specification, and is printed with a question mark for that reason.

    * ``01`` plus sixteen high-entropy bytes on ae02 -- one AES block, a
      different one every time. That is not a status code, and a channel that
      answers a plaintext write with a cipher-sized block of noise is not one
      we can drive without its key.
    """
    if len(data) == 5 and data[0] == 0x05:
        status = data[4]
        meaning = {0x01: "accepted?", 0x02: "rejected / not understood?"}.get(
            status, "status 0x%02x, meaning unknown" % status)
        return "echoes cmd %02x %02x, %s" % (data[2], data[3], meaning)
    if len(data) == 17 and _looks_random(data[1:]):
        return "type %02x + a 16-byte high-entropy block -- looks encrypted" % data[0]
    if _looks_random(data):
        return "%d high-entropy bytes -- looks encrypted" % len(data)
    return "%d bytes, shape not recognised" % len(data)



async def _trace_one(client, char, chunks, replies, args) -> int:
    """Write one payload to one characteristic, narrating the replies.

    ``replies`` is the shared list the notification handler appends to; it is
    cleared before each chunk so a reply is attributed to the write that
    provoked it rather than to whatever came before.
    """
    ack = _ack(args)
    answered = 0
    encrypted_looking = False
    silent_since = None
    for index, chunk in enumerate(chunks, 1):
        replies.clear()
        try:
            await client.write_gatt_char(char, chunk, response=ack)
        except Exception as exc:
            print("%3d/%d  WRITE FAILED: %s: %s"
                  % (index, len(chunks), type(exc).__name__, exc))
            return answered, encrypted_looking
        await asyncio.sleep(args.gap)
        head = "%3d/%d  %-3d bytes  %s" % (index, len(chunks), len(chunk),
                                           chunk[:12].hex(" "))
        if replies:
            answered += len(replies)
            if silent_since is not None:
                print("        (silent from chunk %d to %d)"
                      % (silent_since, index - 1))
                silent_since = None
            print(head)
            for uuid, reply in replies:
                note = _decode_reply(reply)
                encrypted_looking = encrypted_looking or "encrypted" in note
                print("        <- %s  %s   %s"
                      % (_short_uuid(uuid), reply.hex(" "), note))
        else:
            if silent_since is None:
                silent_since = index
                print("%s   (no reply)" % head)
    if silent_since is not None and silent_since < len(chunks):
        print("        (silent from chunk %d to %d)" % (silent_since, len(chunks)))

    # Some panels only answer once the declared length has arrived.
    if args.settle > 0:
        print("waiting %.0fs for anything late ..." % args.settle)
        replies.clear()
        await asyncio.sleep(args.settle)
        if replies:
            answered += len(replies)
            for uuid, reply in replies:
                note = _decode_reply(reply)
                encrypted_looking = encrypted_looking or "encrypted" in note
                print("  late <- %s  %s   %s"
                      % (_short_uuid(uuid), reply.hex(" "), note))
        else:
            print("  nothing further")
    return answered, encrypted_looking


async def do_trace(args):
    """Send a payload one chunk at a time, reporting replies per chunk.

    The panel answers, which is worth more than watching it: a payload that
    fails somewhere can be localised instead of guessed at. Does it answer the
    first chunk and ignore the rest? Every chunk? Only the last? Each of those
    means something different about how it reassembles a long write, and none
    of them is visible from the screen.
    """
    ack = _ack(args)
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

    # (characteristic uuid, bytes). Which notify channel answers is itself
    # evidence: a panel with a control characteristic and a data characteristic
    # usually pairs each with its own notify, so the pairing says which write
    # channel a reply belongs to.
    replies = []

    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        services = getattr(client, "services", None)
        if services is None:
            services = await client.get_services()
        listening = []
        for service in services:
            for characteristic in service.characteristics:
                if "notify" not in (characteristic.properties or ()):
                    continue
                uuid = characteristic.uuid

                def on_notify(_sender, data, _uuid=uuid):
                    replies.append((_uuid, bytes(data)))

                try:
                    await client.start_notify(uuid, on_notify)
                    listening.append(uuid)
                except Exception:
                    pass
        if listening:
            print("listening on %s\n" % ", ".join(listening))

        targets = [char]
        if args.sweep_chars:
            # A panel with more than one writable characteristic usually has a
            # control channel and a bulk data channel, and the GATT tree does
            # not say which is which. This sign's panel exposes two; the driver
            # picked one by UUID preference and the other has never been tried.
            targets = []
            for service in services:
                for characteristic in service.characteristics:
                    if set(characteristic.properties or ()) \
                            & {"write", "write-without-response"}:
                        targets.append(characteristic.uuid)
            print("sweeping %d writable characteristic(s)\n" % len(targets))

        summary = []
        encrypted_looking = False
        for target in targets:
            if len(targets) > 1:
                print("=" * 70)
                print("writing to %s" % target)
                print("=" * 70)
            answered, sus = await _trace_one(client, target, chunks, replies, args)
            encrypted_looking = encrypted_looking or sus
            summary.append((target, answered))
            if len(targets) > 1 and target != targets[-1]:
                # Let the panel settle before the next characteristic, so a
                # reply cannot be attributed to the wrong one.
                await asyncio.sleep(1.5)
                print()

        if len(summary) > 1:
            print("\n===== per characteristic =====")
            for target, answered in summary:
                print("  %-42s %s" % (target,
                                      "%d repl%s" % (answered, "y" if answered == 1 else "ies")
                                      if answered else "silent"))
        if encrypted_looking:
            print("""
One channel answered with cipher-sized blocks of noise, different every
time. Writing to it without the key is not something more probing will
solve -- and a capture of the vendor app using that channel would record
ciphertext, which is no more usable than what we have.

If the app instead drives the plaintext channel, the capture gives us the
payload exactly. That is the thing worth finding out, and it is one
capture either way.""")

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


async def do_png_sweep(args):
    """Find the two undocumented bytes in the PNG header, by asking the panel.

    The extended-data header carries an ``opt`` byte and a ``buffer`` byte that
    no public write-up specifies. Guessing them blind would be poor, but the
    panel answers 01 or 02 to every packet, so this is a search with an oracle
    rather than a guess: send the same valid PNG with each combination and keep
    whichever comes back 01.

    Small on purpose. If nothing in the default set answers 01, the answer is
    not one byte further along and the pixel path is already working.
    """
    ack = _ack(args)
    from vicelights.matrix import IPixel

    opts = [int(v, 0) for v in args.opts.split(",")] if args.opts else [0, 1, 2, 3]
    buffers = ([int(v, 0) for v in args.buffers.split(",")]
               if args.buffers else [0, 1, 2])
    if args.sizes:
        sizes = []
        for spec in args.sizes.split(","):
            width, _, height = spec.lower().partition("x")
            sizes.append((int(width), int(height)))
    else:
        sizes = [(args.width, args.height)]
    message = M.normalize_message({"text": args.text, "color": args.color})
    print("trying %d combination(s) on %r"
          % (len(sizes) * len(opts) * len(buffers), message["text"]))
    print("a status of 01 is the answer; 02 means it did not like the packet")
    if len(sizes) > 1:
        # A panel that validates an image against its own geometry turns this
        # into a way of asking how big it is, which is otherwise a guess.
        print("sweeping sizes too: if only one is accepted, that is the panel's")
    print()

    replies = []

    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        services = getattr(client, "services", None)
        if services is None:
            services = await client.get_services()
        listening = []
        for service in services:
            for characteristic in service.characteristics:
                if "notify" not in (characteristic.properties or ()):
                    continue
                uuid = characteristic.uuid

                def on_notify(_sender, data, _uuid=uuid):
                    replies.append((_uuid, bytes(data)))

                try:
                    await client.start_notify(uuid, on_notify)
                    listening.append(uuid)
                except Exception:
                    pass

        winners = []
        for width, height in sizes:
            for opt in opts:
                for buffer in buffers:
                    driver = IPixel({"width": width, "height": height,
                                     "chunk": args.chunk, "text_mode": "png",
                                     "png_opt": opt, "png_buffer": buffer})
                    frames = driver.png_frames(message)
                    replies.clear()
                    for position, frame in enumerate(frames):
                        await client.write_gatt_char(driver.characteristic(), frame,
                                                     response=ack)
                        if position + 1 < len(frames):
                            await asyncio.sleep(args.delay)
                    await asyncio.sleep(args.gap)
                    verdict = "no reply"
                    for _uuid, reply in replies:
                        if len(reply) == 5 and reply[0] == 0x05:
                            # Control commands acknowledge with 01, but this one
                            # answers 00 and 03, so 01 is not the only shape a
                            # success can take. Collect anything that is not an
                            # outright rejection and say which is which.
                            verdict = "status %02x" % reply[4]
                            if reply[4] in (0x00, 0x01):
                                winners.append((width, height, opt, buffer,
                                                reply[4]))
                            break
                    print("  %-7s opt %3d  buffer %3d  ->  %s"
                          % ("%dx%d" % (width, height), opt, buffer, verdict))

        for characteristic in listening:
            try:
                await client.stop_notify(characteristic)
            except Exception:
                pass

    print()
    if winners:
        width, height, opt, buffer, status = winners[0]
        accepted_sizes = sorted({(w, h) for w, h, _o, _b, _s in winners})
        print("(status %02x -- control commands acknowledge with 01, so treat "
              "this as\n promising rather than proven until the panel shows "
              "something.)" % status)
        print("Accepted with %dx%d, opt=%d, buffer=%d. Put it in the config:"
              % (width, height, opt, buffer))
        print('  curl -s -X POST http://127.0.0.1/api/matrix '
              '-H "Content-Type: application/json" \\')
        print('    -d \'{"text_mode":"png","width":%d,"height":%d,'
              '"png_opt":%d,"png_buffer":%d}\''
              % (width, height, opt, buffer))
        if len(accepted_sizes) == 1 and len(sizes) > 1:
            print("\nOnly %dx%d was accepted, so that is very likely the panel's"
                  " real size." % (width, height))
        elif len(accepted_sizes) > 1:
            print("\n%d sizes were accepted (%s), so the panel is not checking"
                  "\ngeometry -- this says nothing about how big it is."
                  % (len(accepted_sizes),
                     ", ".join("%dx%d" % s for s in accepted_sizes)))
    else:
        print("Nothing was accepted. The PNG route needs something this does not\n"
              "cover, and it is an optimisation rather than a requirement --\n"
              "text_mode=pixels is fully documented and does not need it.")


async def _listen_all(client, replies):
    """Subscribe to every notify characteristic; return the uuids taken."""
    services = getattr(client, "services", None)
    if services is None:
        services = await client.get_services()
    listening = []
    for service in services:
        for characteristic in service.characteristics:
            if "notify" not in (characteristic.properties or ()):
                continue
            uuid = characteristic.uuid

            def on_notify(_sender, data, _uuid=uuid):
                replies.append((_uuid, bytes(data)))

            try:
                await client.start_notify(uuid, on_notify)
                listening.append(uuid)
            except Exception:
                pass
    return listening


async def do_hello(args):
    """Ask the panel about itself, rather than assuming.

    Everything drawn on this display depends on how many pixels it has, and so
    far that has been a guess in a config file. The documented device-info
    query is the time-set command; the panel answers with a packet that is
    reported to carry the screen dimensions. Rather than claim to know which
    bytes those are, this prints the reply in full with every plausible
    reading, so the right one can be recognised on sight.
    """
    ack = _ack(args)
    from vicelights.matrix import IPixel

    # The info query is also the set-time command, so its reply mixes the clock
    # in with whatever else it reports. Send it several times with deliberately
    # different times: the bytes that follow what we sent are the clock, and
    # the bytes that stay put whatever we send are the device describing
    # itself. That separates them without guessing at an offset.
    stamps = [(1, 2, 3), (11, 22, 33), (20, 44, 55)][:max(1, args.repeat)]
    probes = [("device info, clock %02d:%02d:%02d" % stamp,
               IPixel.packet((0x01, 0x80), [stamp[0], stamp[1], stamp[2], 0]),
               stamp)
              for stamp in stamps]
    if args.extra:
        for spec in args.extra.split(","):
            cleaned = re.sub(r"[^0-9a-fA-F]", "", spec)
            if len(cleaned) % 2 == 0 and cleaned:
                probes.append(("custom %s" % cleaned, bytes.fromhex(cleaned), None))

    replies = []
    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        listening = await _listen_all(client, replies)
        print("listening on %s\n" % ", ".join(listening))
        collected = []
        for label, packet, stamp in probes:
            replies.clear()
            print("%-28s -> %s" % (label, packet.hex(" ")))
            await client.write_gatt_char(IPixel.write_uuid, packet, response=ack)
            await asyncio.sleep(args.gap)
            if not replies:
                print("    (no reply)\n")
                continue
            for uuid, reply in replies:
                print("    <- %s  %s" % (_short_uuid(uuid), reply.hex(" ")))
                collected.append((reply, stamp, packet))
            print()
        _report_static(collected)
        for characteristic in listening:
            try:
                await client.stop_notify(characteristic)
            except Exception:
                pass


def _report_static(collected):
    """Split a reply into the part that tracked what we sent and the part that did not."""
    replies = [reply for reply, _stamp, _sent in collected if reply]
    if len(replies) < 2:
        if replies:
            _guess_dimensions(replies[0])
        return
    shortest = min(len(reply) for reply in replies)
    if any(len(reply) != shortest for reply in replies):
        print("replies differ in length; not comparing them byte by byte")
        return

    echoed, static, varying = [], [], []
    for index in range(shortest):
        values = {reply[index] for reply in replies}
        sent = {packet[index] if index < len(packet) else None
                for _r, _s, packet in collected}
        if len(values) == 1:
            static.append(index)
        elif values == sent:
            echoed.append(index)
        else:
            varying.append(index)

    def show(name, indexes):
        if not indexes:
            return
        print("  %-28s %s" % (name, ", ".join(
            "byte %d = %s" % (i, " / ".join("%02x" % reply[i] for reply in replies))
            for i in indexes)))

    print("comparing %d replies:" % len(replies))
    show("same whatever we send", static)
    show("echoes what we sent", echoed)
    show("changes, but not an echo", varying)
    # Bytes 0-3 are the length and the echoed command; they are framing, not
    # device information, and listing them as candidates only adds noise.
    payload_static = [index for index in static if index >= 4]
    if payload_static:
        print("\n  The bytes that never move are the panel describing itself.")
        print("  A size would be among them. Candidates, as decimal:")
        for index in payload_static:
            value = replies[0][index]
            notes = []
            if value in (8, 16, 32, 64, 96, 128):
                notes.append("a common panel dimension")
            if value + 1 in (8, 16, 32, 64, 128):
                notes.append("= %d - 1, so possibly a maximum coordinate" % (value + 1))
            print("      byte %-2d = %3d%s"
                  % (index, value, ("   (" + "; ".join(notes) + ")") if notes else ""))
        print("\n  Counting the LEDs on the panel settles it in five seconds,")
        print("  and beats any amount of staring at these bytes.")


def _guess_dimensions(reply: bytes):
    """Point at bytes in a reply that could be a screen size.

    Deliberately shows every candidate rather than picking one: the field
    offset is not documented, and a confident wrong answer here would send the
    whole drawing path off the edge of the display.
    """
    if len(reply) < 6:
        return
    plausible = (8, 16, 32, 64, 96, 128)
    found = []
    for index in range(4, len(reply) - 1):
        a, b = reply[index], reply[index + 1]
        if a in plausible and b in plausible:
            found.append("bytes %d,%d = %dx%d" % (index, index + 1, a, b))
    if found:
        print("       possible size: %s" % ";  ".join(found))
    else:
        singles = ["byte %d = %d" % (i, v) for i, v in enumerate(reply)
                   if v in plausible and i >= 4]
        if singles:
            print("       panel-sized values in it: %s" % ", ".join(singles))


async def do_fill(args):
    """Paint the panel. A test you cannot misread.

    Three colours by default, one per third of the width, because a partial
    result is the informative one: if only the left third lights, the panel is
    wider than we think; if the bands are stacked rather than side by side, the
    axes are swapped. A single flat colour tells you far less.
    """
    ack = _ack(args)
    from vicelights.matrix import IPixel

    driver = IPixel({"width": args.width, "height": args.height})
    if args.color:
        bands = [M.parse_color(args.color, (255, 0, 0))]
        plan = "solid %s" % args.color
    else:
        bands = [(255, 0, 0), (0, 255, 0), (0, 80, 255)]
        plan = "red | green | blue, left to right"

    frames = []
    if args.screen:
        frames += driver.screen_frames(args.screen)
    frames += driver.diy_frames(True)
    for y in range(args.height):
        for x in range(args.width):
            colour = bands[min(len(bands) - 1, x * len(bands) // args.width)]
            frames.append(driver.pixel_frame(x, y, colour))
    print("painting %dx%d: %s" % (args.width, args.height, plan))
    print("%d packets at %.0fms, about %.0fs\n"
          % (len(frames), args.delay * 1000, len(frames) * args.delay))

    replies = []
    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        listening = await _listen_all(client, replies)
        await _stream(client, IPixel.write_uuid, frames, args, "painting")
        await asyncio.sleep(1.5)
        for characteristic in listening:
            try:
                await client.stop_notify(characteristic)
            except Exception:
                pass

    statuses = [reply for _u, reply in replies if len(reply) == 5]
    if statuses:
        print("\npanel said: %s"
              % "  ".join(sorted({r.hex(" ") for r in statuses})))
    else:
        print("\npanel said nothing (pixel writes are not acknowledged)")
    print("""
What is on it now?
  the whole panel coloured   -> drawing works; size and axes are right
  only part of it            -> drawing works, the geometry is wrong; say
                                which part and I can work out the real one
  bands stacked, not side by side -> width and height are swapped
  nothing at all             -> the pixels are going somewhere that is not on
                                screen. Try the buffers:
                                  ./matrix_probe.py screens %s""" % args.address)


async def do_say(args):
    """Put one message on the panel and leave it there.

    The command this whole exercise was for. Everything else here is
    diagnosis; this is the thing you actually want at the sign.
    """
    ack = _ack(args)
    from vicelights.matrix import IPixel, layout_for

    config = {"width": args.width, "height": args.height}
    if args.scale:
        config["scale"] = str(args.scale)
    driver = IPixel(config)
    message = M.normalize_message({"text": args.text, "color": args.color})
    plan = layout_for(config, message["text"])
    scale, drawn = plan["scale"], plan["width"]

    print("%r at %dx%s -- %d x %d on a %d x %d panel%s"
          % (message["text"], scale, ", bold" if plan["bold"] else "",
             drawn, plan["height"], args.width, args.height,
             " (stretched to fill the height)"
             if driver.stretch() and plan["height"] != 7 * scale else ""))
    if drawn > args.width:
        print("  too long: %d columns of %d. It will be cut off."
              % (drawn, args.width))
    elif scale == 1 and 14 <= args.height:
        fits_bigger = text_width(message["text"], scale=2) <= args.width
        if not fits_bigger:
            print("  at 2x this would be %d columns, past the %d available, so"
                  " it stays at 1x."
                  % (text_width(message["text"], scale=2), args.width))
    print()
    print(M.preview(message["text"]))
    print()

    previous = None
    if args.replaces:
        previous = M.normalize_message({"text": args.replaces})
    frames = driver.text_frames(message, previous=previous)
    if args.clear:
        frames = driver.pixel_frames(message, fill=True)
        print("repainting the whole panel (--clear): %d packets, about %.0fs"
              % (len(frames), len(frames) * args.delay))
    else:
        print("%d packets, about %.0fs" % (len(frames), len(frames) * args.delay))

    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        await _stream(client, IPixel.write_uuid, frames, args, "drawing")
    print("done -- it stays up until something replaces it.")


async def do_text(args):
    """Send the panel's OWN text command, the one it animates by itself.

    Everything else in this tool draws pixels: we compute every lit LED and
    send one packet each, and if the message is to move we have to send it all
    again for every frame. This is the other thing entirely -- one packet
    carrying the message, an animation number and a speed, after which the
    panel scrolls it on its own with no further radio at all. It can also store
    it in one of a hundred slots and cycle those by itself.

    Unverified when written. The packet layout comes from the community's work
    on the iPixel Color app, and the panel answers 01 or 02 so whether it takes
    the packet is knowable -- but whether the glyphs come out the right way up
    is not, because the bit order of the app's own font is not documented. So
    --order picks one of four, and --sweep sends the same letter in all four
    with a pause between. Watch the panel and say which one was right; that is
    the same way the colour order got settled, and for the same reason.
    """
    if args.corner or args.saw:
        return await do_text_corner(args)
    if args.bisect:
        return await do_text_bisect(args)
    ack = _ack(args)
    from vicelights.matrix import IPixel, BITMAP_ORDERS, TEXT_ANIMATIONS

    config = {"width": args.width, "height": args.height,
              "text_mode": "native", "text_font": args.font,
              "pixel_layout": args.layout}
    driver = IPixel(config)
    text = args.text[::-1] if args.reverse else args.text
    message = M.normalize_message({"text": text, "color": args.color,
                                   "mode": args.mode, "speed": args.speed})
    animation = TEXT_ANIMATIONS.get(args.mode, 0)
    orders = list(BITMAP_ORDERS) if args.sweep else [args.order]

    print("%r as the panel's own text command" % message["text"])
    print("  animation %d (%s), speed %d, font %s, slot %d"
          % (animation, args.mode, args.speed, args.font, args.slot))
    frames = driver.native_text_frames(message, slot=args.slot,
                                       order=orders[0], animation=animation)
    if not frames:
        print("nothing to send")
        return 2
    print("  one packet, %d bytes -- against %d packets to draw it a pixel at "
          "a time" % (len(frames[0]),
                      len(driver.pixel_frames(message))))
    print()

    replies = []
    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        listening = await _listen_all(client, replies)
        if listening:
            print("listening on %s" % ", ".join(_short_uuid(u) for u in listening))
        for order in orders:
            frames = driver.native_text_frames(message, slot=args.slot,
                                               order=order, animation=animation)
            print("-- bit order %s" % order)
            replies.clear()
            await _stream(client, IPixel.write_uuid, frames, args,
                          "text (%s)" % order)
            await asyncio.sleep(args.gap)
            if not replies:
                print("   no reply -- the panel did not answer this one")
            for uuid, reply in replies:
                decoded = _decode_reply(reply)
                print("   <- %s  %s%s" % (_short_uuid(uuid), reply.hex(" "),
                                          ("   %s" % decoded) if decoded else ""))
            if len(orders) > 1:
                print("   watch the panel for %.0fs" % args.hold)
                await asyncio.sleep(args.hold)
    print()
    print("Reading the panel: %r should read left to right, each letter the "
          "right way round." % args.text)
    print("  mirrored letters      -> a different bit order below")
    print("  letters in the wrong order -> add --reverse")
    print("  nothing at all, and a reply ending 02 -> the panel rejected it,")
    print("     which is the packet layout rather than the bit order")
    print()
    if args.sweep:
        print("Which one was right? Set it and it stays set:")
        print("  curl -X POST http://localhost/api/matrix \\")
        print("    -H 'content-type: application/json' \\")
        print("    -d '{\"text_mode\":\"native\",\"bitmap_order\":\"lsb-swap\"}'")
        print("(swap lsb-swap for whichever of these looked right:")
        print("   %s)" % ", ".join(BITMAP_ORDERS))
    else:
        print("If that looked wrong, --sweep sends all %d orders in turn."
              % len(BITMAP_ORDERS))
    return 0


async def do_text_bisect(args):
    """Find the longest message this panel will actually show.

    One packet carries the whole message, so a long message is a long packet:
    twenty characters at 16x16 is 749 bytes against an MTU of about 247. Every
    short test that ever appeared on this panel fitted a single write, and
    every real message did not -- so "the glyphs look wrong" and "nothing
    happens" were being read off two different situations without noticing.

    This sends the same word repeated to a series of lengths, printing the
    packet size and the number of writes each one takes, with a pause to look.
    The longest one that appears is the answer:

      * everything appears -> the panel reassembles across writes, and
        whatever is left is an encoding question
      * they stop appearing right where the packet passes the MTU -> it does
        not reassemble, and no amount of bit-order fiddling will help. The
        message has to be split into several length-prefixed packets using the
        continuation byte instead.
    """
    from vicelights.matrix import IPixel
    from vicelights.ble import attribute_mtu, pack_frames

    config = {"width": args.width, "height": args.height,
              "text_mode": "native", "text_font": args.font,
              "pixel_layout": args.layout, "chunk": args.chunk}
    driver = IPixel(config)
    lengths = [int(n) for n in args.bisect.split(",")] if args.bisect != "auto" \
        else [1, 2, 4, 6, 8, 12, 16, 24]

    print("Sending the same message at %d lengths, %.0fs apart."
          % (len(lengths), args.hold))
    print("Watch the panel and note the LONGEST one that appears.")
    print()

    replies = []
    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        listening = await _listen_all(client, replies)
        mtu = attribute_mtu(client)
        room = max(20, mtu - 3)
        print("negotiated MTU %d, so %d bytes of payload per write" % (mtu, room))
        if listening:
            print("listening on %s" % ", ".join(_short_uuid(u) for u in listening))
        print()
        for count in lengths:
            text = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 4)[:count]
            frames = driver.native_text_frames(
                {"text": text, "color": args.color}, order=args.order,
                animation=0)
            size = sum(len(f) for f in frames)
            writes = pack_frames(frames, mtu)
            print("%2d char%s  %4d bytes  %2d write(s)  %s"
                  % (count, " " if count == 1 else "s", size, len(writes),
                     "one write" if len(writes) == 1 else "SPLIT"))
            replies.clear()
            for chunk in writes:
                await client.write_gatt_char(IPixel.write_uuid, chunk,
                                             response=_ack(args))
            await asyncio.sleep(args.gap)
            for uuid, reply in replies:
                decoded = _decode_reply(reply)
                print("            <- %s%s" % (reply.hex(" "),
                                               ("   %s" % decoded) if decoded else ""))
            if not replies:
                print("            (no reply)")
            await asyncio.sleep(args.hold)
    print()
    print("The longest one that APPEARED on the panel is the answer.")
    print("  all of them          -> the panel reassembles across writes")
    print("  they stop at the point marked SPLIT -> it does not, and the")
    print("     message needs splitting into several packets instead")
    return 0


async def do_text_corner(args):
    """Ask the panel what it does to a glyph, in a way that has one answer.

    Two rounds of "it looks backwards" is two rounds of me guessing, and the
    fault is the question: a letter looks the same mirrored as it does with
    the characters in reverse order, and "backwards" covers both plus upside
    down. So this sends no letters.

    It sends two blocks: a bracket occupying one corner, and a solid square.
    Which corner the bracket ends up in names the transform -- a corner is not
    a judgement call -- and which side of the square it lands on says whether
    the panel lays characters out left to right or right to left. One look,
    two answers, nothing to interpret.
    """
    from vicelights.matrix import (IPixel, CORNERS, marker_cell, pack_cell,
                                   TEXT_CELLS)

    if args.saw:
        parts = [p.strip().lower() for p in args.saw.replace(",", " ").split()]
        corner = next((p for p in parts if p in CORNERS), None)
        if corner is None:
            print("say which corner the bracket was in: %s"
                  % ", ".join(sorted(CORNERS)), file=sys.stderr)
            return 2
        side = next((p for p in parts if p in ("left", "right")), "left")
        order = CORNERS[corner]
        reversed_text = side == "right"
        print("bracket in the %s, %s of the square" % (corner, side))
        print()
        print("  the panel %s"
              % {"msb": "draws a glyph exactly as sent",
                 "lsb-swap": "mirrors a glyph left to right",
                 "msb-flip": "turns a glyph upside down",
                 "lsb-swap-flip": "rotates a glyph 180 degrees"}[order])
        if reversed_text:
            print("  and lays its characters out right to left")
        print()
        print("Set it:")
        print("  curl -X POST http://localhost/api/matrix \\")
        print("    -H 'content-type: application/json' \\")
        print("    -d '{\"text_mode\":\"native\",\"bitmap_order\":\"%s\""
              "%s}'" % (order, ",\"text_reversed\":true" if reversed_text else ""))
        return 0

    config = {"width": args.width, "height": args.height,
              "text_mode": "native", "text_font": args.font,
              "pixel_layout": args.layout}
    driver = IPixel(config)
    font_flag = M.TEXT_FONTS.get(args.font, 1)
    width, height = TEXT_CELLS.get(font_flag, TEXT_CELLS[1])
    # Sent as msb -- the reference. Whatever the panel does to it is what the
    # answer describes.
    bracket = marker_cell(font_flag, "msb")
    square = pack_cell([[1] * width for _ in range(height)], width, "msb")

    print("Putting two blocks on the panel: a corner bracket, then a solid square.")
    print("Sent as: bracket in the TOP-LEFT, square to its RIGHT.")
    print()
    frames = driver.native_text_frames(
        {"text": "  ", "color": args.color}, slot=0, order="msb",
        animation=0, cells=[bracket, square])

    replies = []
    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        listening = await _listen_all(client, replies)
        if listening:
            print("listening on %s" % ", ".join(_short_uuid(u) for u in listening))
        await _stream(client, IPixel.write_uuid, frames, args, "corner test")
        await asyncio.sleep(args.gap)
        for uuid, reply in replies:
            decoded = _decode_reply(reply)
            print("   <- %s  %s%s" % (_short_uuid(uuid), reply.hex(" "),
                                      ("   %s" % decoded) if decoded else ""))
        if not replies:
            print("   no reply")
    print()
    print("Look at the panel, then tell it what you saw:")
    print("  ./matrix_probe.py text %s --saw <corner>,<side>" % args.address)
    print()
    print("  <corner>  which corner the BRACKET is in:")
    print("            top-left, top-right, bottom-left, bottom-right")
    print("  <side>    which side of the SQUARE the bracket is on: left, right")
    print()
    print("  e.g.  --saw top-right,left      (mirrored, characters in order)")
    print()
    print("If you see neither a bracket nor a square -- stripes, scattered")
    print("pixels, nothing at all -- say so instead: that is not an")
    print("orientation, it means the bitmap is not laid out in rows and none")
    print("of the eight orders will fix it.")
    return 0


async def do_colortest(args):
    """Show pure red, green and blue, and work out where the colour bytes go.

    Not a channel order: this sign's panel showed blue for all three, plus
    green on the second and red on the third, and the only byte set to 255
    every time was the alpha. Blue therefore comes from the fourth byte, which
    no reordering of the first three could ever produce. So this solves for the
    layout -- which of the four bytes drives which channel -- and treats the
    documented rgba as one possibility rather than the truth.
    """
    ack = _ack(args)
    from vicelights.matrix import IPixel

    driver = IPixel({"width": args.width, "height": args.height})
    # One byte at a time, so each band's result names one byte's destination.
    probes = [("byte 4", [0xFF, 0x00, 0x00, 0x00]),
              ("byte 5", [0x00, 0xFF, 0x00, 0x00]),
              ("byte 6", [0x00, 0x00, 0xFF, 0x00]),
              ("byte 7", [0x00, 0x00, 0x00, 0xFF])]

    frames = driver.diy_frames(True)
    for y in range(args.height):
        for x in range(args.width):
            _name, colour = probes[min(len(probes) - 1,
                                       x * len(probes) // args.width)]
            frames.append(driver.packet(driver.CMD_PIXEL,
                                        colour + [x & 0xFF, y & 0xFF]))

    print("painting %d bands along the long axis. Each sets ONE byte to 255:"
          % len(probes))
    for index, (name, colour) in enumerate(probes, 1):
        print("   band %d  %s  ->  %s"
              % (index, name, " ".join("%02x" % v for v in colour)))
    print("\n%d packets, about %.0fs\n" % (len(frames), len(frames) * args.delay))

    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        await _stream(client, IPixel.write_uuid, frames, args, "painting")

    if not args.saw:
        print("""
Look at the panel and name the %d bands, in order. For example:

    ./matrix_probe.py colortest %s --saw off,green,red,blue

Any of: red green blue cyan magenta yellow white off""" % (len(probes), args.address))
        return

    seen = [name.strip().lower() for name in args.saw.split(",")]
    if len(seen) != len(probes):
        sys.exit("--saw needs %d colours, one per band" % len(probes))
    named = {"red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
             "cyan": (0, 255, 255), "magenta": (255, 0, 255),
             "yellow": (255, 255, 0), "white": (255, 255, 255),
             "off": (0, 0, 0), "black": (0, 0, 0), "none": (0, 0, 0)}
    unknown = [name for name in seen if name not in named]
    if unknown:
        sys.exit("do not know the colour %s; use one of: %s"
                 % (", ".join(unknown), ", ".join(sorted(named))))

    print()
    layout = []
    for index, name in enumerate(seen):
        lit = [channel for channel, value in zip("rgb", named[name])
               if value == 255]
        print("  %s alone lit: %s"
              % (probes[index][0], ", ".join(lit) if lit else "nothing"))
        layout.append(lit)

    flat = [lit[0] if len(lit) == 1 else None for lit in layout]
    driven = [c for c in flat if c]
    if any(len(lit) > 1 for lit in layout):
        print("""
At least one byte lit more than one channel. Setting a single byte cannot do
that, so either two bands were read together or the panel is mixing channels.
Look again with the bands well separated.""")
        return
    if len(set(driven)) != len(driven):
        print("\nTwo bytes drove the same channel, which should not happen. "
              "Check the\nband order and try again.")
        return

    order = "".join(channel or "a" for channel in flat)
    missing = [c for c in "rgb" if c not in driven]
    if missing:
        print("\nNothing drove %s, so one band was misread. Try again."
              % ", ".join(missing))
        return

    print("\nThe layout is %s -- byte 4 is %s, byte 5 %s, byte 6 %s, byte 7 %s."
          % ((order.upper(),) + tuple(
              "unused" if c == "a" else c for c in order)))
    from vicelights.matrix import PIXEL_LAYOUTS, DEFAULT_PIXEL_LAYOUT
    if order == DEFAULT_PIXEL_LAYOUT:
        print("That is already the default, so nothing to change.")
        return
    if order not in PIXEL_LAYOUTS:
        print("\nThat layout is not one the driver knows yet. Tell me and I "
              "will add it.")
        return
    print("Apply it:")
    print('  curl -s -X POST http://127.0.0.1/api/matrix '
          '-H "Content-Type: application/json" \\')
    print('    -d \'{"pixel_layout":"%s"}\'' % order)


async def do_screens(args):
    """Step through the display buffers, one per pause.

    One packet each, so it costs nothing, and it separates two failures that
    look identical: pixels not arriving, versus pixels arriving into a buffer
    the panel is not currently showing.
    """
    ack = _ack(args)
    from vicelights.matrix import IPixel

    replies = []
    holder = connected(args.address, args.timeout, args.retries)
    async with holder as client:
        listening = await _listen_all(client, replies)
        print("watch the panel; %ds on each\n" % args.hold)
        for number in range(args.first, args.last + 1):
            replies.clear()
            packet = driver_screen_packet(IPixel, number)
            print("  screen %d   -> %s" % (number, packet.hex(" ")))
            await client.write_gatt_char(IPixel.write_uuid, packet, response=ack)
            await asyncio.sleep(args.hold)
            for _uuid, reply in replies:
                print("             <- %s   %s" % (reply.hex(" "), _decode_reply(reply)))
        for characteristic in listening:
            try:
                await client.stop_notify(characteristic)
            except Exception:
                pass
    print("""
Did the display change on any of them, and which? If one of these shows
something the others do not, that is the buffer the panel is displaying, and
drawing has to go there.""")


def driver_screen_packet(cls, number):
    return cls.packet(cls.CMD_SCREEN, [max(1, min(9, int(number)))])


# -------------------------------------------------------------------- sending

async def write_frames(address, char_uuid, frames, timeout, delay, response=True,
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
                       response=_ack(args), retries=args.retries)


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
        p.add_argument("--no-ack", action="store_true",
                       help="unacknowledged writes: faster, and drops packets")
        p.add_argument("--no-batch", action="store_true",
                       help="one packet per write, for a panel that needs it")
        p.add_argument("--delay", type=float, default=0.0,
                       help="extra seconds between writes; acknowledged writes "
                            "already pace themselves, so 0")
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
    p.add_argument("--sweep-chars", action="store_true",
                   help="repeat the payload on every writable characteristic")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_trace(a)))

    p = sub.add_parser("png-sweep",
                       help="find the undocumented PNG header bytes by trying them")
    ble_args(p)
    p.add_argument("-t", "--text", default="VICE")
    p.add_argument("--color", default="#ff2f6e")
    p.add_argument("--width", type=int, default=32)
    p.add_argument("--height", type=int, default=16)
    p.add_argument("--opts", default="", help="comma-separated, default 0,1,2,3")
    p.add_argument("--buffers", default="", help="comma-separated, default 0,1,2")
    p.add_argument("--sizes", default="",
                   help="also sweep sizes, e.g. 16x16,32x8,32x16,64x32")
    p.add_argument("--gap", type=float, default=1.5,
                   help="seconds to wait for a verdict after each attempt")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_png_sweep(a)))

    p = sub.add_parser("hello", help="ask the panel what it is, including its size")
    ble_args(p)
    p.add_argument("--gap", type=float, default=2.0)
    p.add_argument("--repeat", type=int, default=3,
                   help="how many times to ask, with different clocks (default 3)")
    p.add_argument("--extra", default="",
                   help="comma-separated extra hex packets to try")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_hello(a)))

    p = sub.add_parser("fill", help="paint the panel; partial results are informative")
    ble_args(p)
    p.add_argument("--color", default="",
                   help="one solid colour instead of three bands")
    p.add_argument("--width", type=int, default=96)
    p.add_argument("--height", type=int, default=16)
    p.add_argument("--screen", type=int, default=0,
                   help="select this display buffer first (1-9)")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_fill(a)))

    p = sub.add_parser("say", help="put one message on the panel and leave it")
    ble_args(p)
    p.add_argument("-t", "--text", required=True)
    p.add_argument("--color", default="#ff2f6e")
    p.add_argument("--width", type=int, default=96)
    p.add_argument("--height", type=int, default=16)
    p.add_argument("--scale", type=int, default=0,
                   help="force a size; default picks the largest that fits")
    p.add_argument("--replaces", default="",
                   help="the message currently up, so only its pixels are erased")
    p.add_argument("--clear", action="store_true",
                   help="repaint the whole panel first (slow, but certain)")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_say(a)))

    p = sub.add_parser("text",
                       help="send the panel's OWN text command -- it animates itself")
    ble_args(p)
    p.add_argument("-t", "--text", default="FL",
                   help="two asymmetric letters: mirrored glyphs and reversed "
                        "character order look different, and one letter "
                        "cannot tell them apart")
    p.add_argument("--color", default="#ff2f6e")
    p.add_argument("--mode", choices=sorted(M.TEXT_ANIMATIONS), default="scroll",
                   help="what the PANEL does with it, not what we do")
    p.add_argument("--speed", type=int, default=50)
    p.add_argument("--font", choices=sorted(M.TEXT_FONTS), default="narrow")
    p.add_argument("--order", choices=M.BITMAP_ORDERS, default="msb")
    p.add_argument("--sweep", action="store_true",
                   help="send every bit order in turn so you can pick one")
    p.add_argument("--corner", action="store_true",
                   help="send a corner bracket and a square: one look names "
                        "both the glyph transform and the character order")
    p.add_argument("--saw", default="",
                   help="what the corner test showed, e.g. top-right,left")
    p.add_argument("--bisect", nargs="?", const="auto", default="",
                   help="send the message at a series of lengths to find the "
                        "longest the panel will show; optionally a list, "
                        "e.g. --bisect 4,8,16")
    p.add_argument("--reverse", action="store_true",
                   help="send the characters back to front, if the panel lays "
                        "them out right to left")
    p.add_argument("--slot", type=int, default=0,
                   help="0 shows it; 1-100 also stores it on the panel")
    p.add_argument("--layout", default="argb", help="colour byte order")
    p.add_argument("--width", type=int, default=96)
    p.add_argument("--height", type=int, default=16)
    p.add_argument("--gap", type=float, default=0.6)
    p.add_argument("--hold", type=float, default=8.0,
                   help="seconds to watch each one during --sweep")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_text(a)))

    p = sub.add_parser("colortest",
                       help="find the panel's colour byte order from what it shows")
    ble_args(p)
    p.add_argument("--width", type=int, default=96)
    p.add_argument("--height", type=int, default=16)
    p.add_argument("--saw", default="",
                   help="the three colours you saw, e.g. blue,magenta,cyan")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_colortest(a)))

    p = sub.add_parser("screens", help="step through the display buffers")
    ble_args(p)
    p.add_argument("--first", type=int, default=1)
    p.add_argument("--last", type=int, default=9)
    p.add_argument("--hold", type=float, default=3.0)
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_screens(a)))

    p = sub.add_parser("control", help="on | off | clear | a brightness percentage")
    ble_args(p)
    p.add_argument("what")
    p.add_argument("--commands")
    p.set_defaults(run=lambda a: asyncio.run(do_control(a)))

    p = sub.add_parser("raw", help="write arbitrary bytes to a characteristic")
    ble_args(p)
    p.add_argument("-x", "--hex", nargs="+", required=True)

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
