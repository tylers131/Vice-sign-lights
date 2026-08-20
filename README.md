# Vice Sign Lights

Headless controller for the sign's 12 ELK-BLEDOM Bluetooth LE RGB controllers, driven from
a phone over the Raspberry Pi's own wifi network. Built for an off-grid art sign:
no internet, no cloud, no CDN, no NTP.

* Runs on a **Pi Zero W (ARMv6), Raspberry Pi OS Lite 32-bit**
* The Pi **broadcasts its own wifi AP**; the UI lives at **http://192.168.4.1/**
* All BLE is **serialized** through one worker: connect &rarr; write &rarr; disconnect,
  one device at a time. Never 12 open connections.
* The web UI **never blocks**: commands are queued and you watch progress.
* One dead controller is skipped and logged; the other 11 still get their colour.

---

## 1. What's in here

| Path | What it is |
| --- | --- |
| `vicelights/protocol.py` | Frame definitions. The only place command bytes live. |
| `vicelights/ble.py` | Serialized BLE worker: job queue, retries, reachability. |
| `vicelights/config.py` | JSON config store (atomic writes). |
| `vicelights/scheduler.py` | Pure-Python schedules + relative timers. |
| `vicelights/timekeeper.py` | Clock handling for a Pi with no RTC and no NTP. |
| `vicelights/web.py` | Flask JSON API. |
| `vicelights/templates/index.html` | Phone UI, all CSS/JS inline. |
| `vicelights/app.py` | Entry point (`python3 -m vicelights`). |
| `elk_scan.py` | CLI: scan / probe / flash / adopt. Shares `protocol.py`. |
| `systemd/vice-lights.service` | systemd unit. |
| `scripts/install.sh` | Installer for Pi OS Lite. |
| `scripts/setup_ap_networkmanager.sh` | Access point via NetworkManager (Bookworm). |
| `scripts/setup_ap_hostapd.sh` | Access point via hostapd + dnsmasq (Bullseye). |
| `config.example.json` | Starter config carrying the sign's 12 real BLE addresses. |

---

## 2. Command frames

Every frame is 9 bytes: `7e 00 <cmd> <a> <b> <c> <d> 00 ef`.

| Command | Bytes | Status |
| --- | --- | --- |
| Solid colour | `7e 00 05 03 RR GG BB 00 ef` | Accepted by the hardware; visual check pending |
| Power on | `7e 00 04 f0 00 01 ff 00 ef` | Accepted by the hardware; visual check pending |
| Power off | `7e 00 04 00 00 00 ff 00 ef` | Accepted by the hardware; visual check pending |
| Brightness | `7e 00 01 BB 00 00 00 00 ef` (`BB` = 0–100, **not** 0–255) | Common, not universal |
| Pattern / mode | `7e 00 03 MM 03 00 00 00 ef` (`MM` = `0x80`–`0x9d`) | Common, not universal |
| Pattern speed | `7e 00 02 SS 00 00 00 00 ef` (`SS` = 0–100) | Common, not universal |

`fff3` is **write-without-response**, so the controller acknowledges nothing: a
write that "succeeds" only proves the connection and characteristic are right,
never that the bytes meant anything. These frames are confirmed as far as the
transport goes — only watching a strand actually turn red confirms the rest.

Print them all, no radio needed:

```bash
./elk_scan.py frames
```

### Brightness policy

The `01` brightness frame is the one these clones most often ignore, so the
default `brightness_mode` is **`scale`**: brightness is applied by scaling RGB on
the Pi before the colour frame goes out. On an analog RGB controller that cannot
fail. If your units do honour `01`, set `"brightness_mode": "native"` (or
`"both"`) in the config — `both` will look darker, since the two dimmings stack.

Unknown commands are simply dropped by the controller, so an unsupported
brightness or pattern frame costs one write and nothing else. That's the whole
"degrade gracefully" story.

### Characteristic detection

On connect the code prefers `0000fff3-0000-1000-8000-00805f9b34fb`, then
`0000ffe1-...`, and otherwise takes the first characteristic with `write` or
`write-without-response`. Whichever it picks is cached back into `config.json`
per device (`char_uuid`), so later connects skip the guessing.

---

## 3. Install on Pi OS Lite

Do this at home, where the Pi still has internet. Nothing here needs a compiler:
`bleak`, `Flask` and `waitress` all come as wheels from
[piwheels](https://www.piwheels.org/), which Raspberry Pi OS already configures
in `/etc/pip.conf`.

```bash
sudo apt update && sudo apt install -y git python3-venv bluez
git clone <this repo> vice-sign-lights
cd vice-sign-lights
sudo ./scripts/install.sh
```

`install.sh` copies the app to `/opt/vice-sign-lights`, builds a venv, installs
the requirements from piwheels, seeds `/etc/vice-lights/config.json` from the
example, and enables the systemd service.

Manual equivalent, if you'd rather see each step:

```bash
python3 -m venv venv
SKIP_CYTHON=1 venv/bin/pip install --extra-index-url https://www.piwheels.org/simple -r requirements.txt
sudo mkdir -p /etc/vice-lights /var/lib/vice-lights
sudo cp config.example.json /etc/vice-lights/config.json
sudo cp systemd/vice-lights.service /etc/systemd/system/
sudo systemctl enable --now vice-lights
```

### Why `SKIP_CYTHON=1`

**Do not `pip install bleak` bare on a Zero W.** bleak depends on `dbus-fast`,
which ships Cython extensions. When piwheels has no ARMv6 wheel for the version
pip picks, it falls back to the source tarball and starts compiling — 30–60+
minutes on one ARMv6 core, frequently ending in an OOM kill on 512MB. It looks
like a hang; it isn't, it's just slow enough to be useless.

`SKIP_CYTHON=1` is honoured by dbus-fast's build script: it returns early and
installs the pure-Python implementation in seconds. That implementation is
slower at marshalling D-Bus messages, which does not matter here — with one BLE
connection open at a time, the radio dominates the cost by orders of magnitude.

If you already started a bare install and it's sitting on
`Building wheel for dbus-fast`, Ctrl-C it and re-run with the variable set. The
downloaded sdist is cached, so the retry is immediate.

**Budget 5–10 minutes** for the venv and the install even with `SKIP_CYTHON=1`.

### Find your controllers

Stop the service first — only one process may own the radio:

```bash
sudo systemctl stop vice-lights
sudo /opt/vice-sign-lights/venv/bin/python /opt/vice-sign-lights/elk_scan.py scan
sudo ... elk_scan.py probe BE:FF:11:22:33:44     # list characteristics
sudo ... elk_scan.py flash BE:FF:11:22:33:44     # red/green/blue/white blink
sudo ... elk_scan.py adopt --out /etc/vice-lights/config.json --force
sudo systemctl start vice-lights
```

`adopt` writes a ready config with every ELK unit it found, all in one group,
plus a few starter scenes. `config.example.json` already carries the sign's 12
addresses, so you only need `adopt` if a controller is replaced. Either way,
rename them in the UI afterwards — walk the sign and hit **Test** on each row to
see which physical light it is.

A controller at the edge of range advertises intermittently, so one pass
under-reports. `--repeat` unions several passes and flags the marginal units:

```bash
python elk_scan.py scan --seconds 10 --repeat 3 --elk-only
```

The `SEEN` column shows how many passes each unit appeared in. Anything below
3/3, or weaker than about -80 dBm, is worth moving the Pi or the controller for
before you rely on it.

### Mapping addresses to physical lights

A BLE address tells you nothing about which light it drives, and the
controllers are identical to look at. `identify` walks the fleet lighting one
controller at a time and writes the name you type straight into the config:

```bash
python elk_scan.py identify --config config.json
```

```
[1/12] BE:27:96:00:1C:AE  (currently 'Light 01')
  lit? name it, or Enter to skip: Left V
  saved as 'Left V'
[2/12] BE:27:49:00:06:95  (currently 'Light 02')
  lit? name it, or Enter to skip:
```

With fewer strands than controllers, plug a strand in, run the walk, and press
Enter past every address until that strand lights — that address is the
controller it's plugged into. Name it, then `q` to quit, move the strand, and
resume where you left off with `--start N`. Unreachable units are reported and
skipped rather than stopping the walk.

Useful flags: `--auto 3` holds each one lit for three seconds without
prompting (for when you're at the sign and not the keyboard), `--keep-on`
leaves them lit as it goes, `--all-off-first` blacks everything out before
starting, and `--color '#00ff00'` if red is hard to pick out.

If you end a walk with several lights on at once, the off frame isn't landing —
see below. Start over from a known state with:

```bash
python elk_scan.py all off --config config.json
```

### If lights won't turn off

`7e 00 04 00 00 00 ff 00 ef` is the documented off frame, but ELK-BLEDOM clones
use more than one encoding and an unrecognised frame is dropped in silence. Try
each in turn on a controller you can see, two seconds apart:

```bash
python elk_scan.py off BE:27:96:00:1C:AE --variant all
```

| Variant | Bytes | |
| --- | --- | --- |
| 0 | `7e 00 04 00 00 00 ff 00 ef` | the documented one |
| 1 | `7e 00 04 00 00 00 00 00 ef` | trailing byte differs |
| 2 | `7e 00 05 03 00 00 00 00 ef` | sets black, does not power off |

Whichever one blacks it out is what your units speak. Variant 2 always works
because it's just the colour frame with zeros — but it leaves the controller
powered, so it draws standby current and any later colour command takes effect
immediately. Fine for a scene, wrong for "off for the night".

### Two firmware families

The sign's 12 controllers are not identical:

| Addresses | Advertised name | Count |
| --- | --- | --- |
| `BE:27:xx:00:xx:xx` | `ELK-BLEDOM` | 8 |
| `BE:68:xx:xx:xx:xx` | `ELK-BLEDOM A4` / `B1` / `C1` / `DB` | 4 |

Different OUI blocks and naming schemes suggested different firmware, but
probing one of each found the same layout on both:

```
service 0000fff0-...  Vendor specific
   char 0000fff3-...  [read,write-without-response]      <- we write here
   char 0000fff4-...  [notify]
```

So the fleet is homogeneous where it counts, and the `fff3` preference is the
right default. The detected UUID is still cached per device in `config.json`, so
a replacement controller that differs is handled without any code change.

You can also do all of this from the UI: **Devices &rarr; Scan**, then **Add**.

---

## 4. Access point (the Pi makes its own wifi)

There is no wifi at the venue, so the Pi broadcasts its own. Pick one:

### Bookworm — NetworkManager (preferred)

```bash
sudo ./scripts/setup_ap_networkmanager.sh "ViceSign" "yourpassphrase" 6
```

Creates a saved connection profile named `vice-ap` with `802-11-wireless.mode ap`
and `ipv4.method shared` (NetworkManager runs its own dnsmasq for DHCP), pinned
to `192.168.4.1/24`, autoconnect on. It comes back on its own after a reboot.

### Bullseye, or if NM AP mode misbehaves — hostapd + dnsmasq

```bash
sudo ./scripts/setup_ap_hostapd.sh "ViceSign" "yourpassphrase" 6
sudo reboot
```

Writes `/etc/hostapd/hostapd.conf`, a dnsmasq DHCP range of
`192.168.4.10–60`, a static `192.168.4.1` on `wlan0`, and tells NetworkManager
to leave `wlan0` alone if it's installed. Its dnsmasq also resolves *every*
hostname to the Pi, so a phone browser lands on the UI whatever you type.

Either way:

* Join **ViceSign** from your phone, open **http://192.168.4.1/**
* The URL is logged on every boot: `journalctl -u vice-lights | grep "web UI"`
* Set a **wifi country** or the radio stays rfkill-blocked — both scripts do this
  (`COUNTRY=US` by default; override with `COUNTRY=XX sudo ./scripts/...`).
* Your phone will complain there's **no internet** and may drop to cellular.
  On iOS turn off "Auto-Join" prompts / tap "Keep Trying"; on Android disable
  "Switch to mobile data automatically" for this network. Nothing in the app
  needs internet, DNS or NTP.

---

## 5. Using it

The UI has five tabs.

**Control** — pick a target (All / a group / one controller), pick a colour and
brightness, hit **Apply**. *Live apply* sends as you drag; it debounces in the
browser and the server coalesces superseded jobs, so you can't build a backlog.
Built-in patterns are optional and are listed by name. The **Queue** card shows
the running job, per-device progress, and which units got skipped.

**Scenes** — one tap applies a saved scene. Save the Control tab's current
settings as a new scene, or **Add step to scene** to build a multi-group look
(letters pink, border cyan, base purple). If two steps name the same controller,
the last one wins and it is still written only once.

**Timing** — set the Pi's clock from your phone, start relative timers, and add
wall-clock schedules (scene + time + optional days).

**Devices** — per-controller status dot (green = last write succeeded, red =
unreachable, grey = not tried yet), plus last error, round-trip time, rename,
regroup, enable/disable, **Test** (blinks it green), scan, and add-by-address.
Disabling a unit parks it: it stays in the config and every scene skips it.

**System** — live log tail and a raw JSON config editor.

### Timing without a clock

The Zero W has no battery-backed clock and there's no NTP on an AP with no
uplink, so **after a cold boot the Pi does not know what time it is**. Three
things handle that:

1. The service writes the time to disk every minute and pulls the clock forward
   to that stamp on start, so time only ever moves forward.
2. **Timing &rarr; "Set Pi clock from this phone"** sets it in one tap from the
   phone already connected to the AP.
3. **Relative timers** ("All off in 90 minutes") run off a monotonic clock and
   don't care about the date at all. Use these if you never set the clock.

While the clock is obviously unset (before 2024), wall-clock schedules are
**paused** rather than firing against a garbage time, and the UI shows a red
banner. Timers keep working.

### Speed expectations

A connect/write/disconnect cycle is roughly 1.5–2.5s per controller on a Zero W,
so **a full 12-device change takes 20–30s**. That's the radio, not the code. The
UI stays responsive throughout; nothing waits on BLE.

---

## 6. Config file

`/etc/vice-lights/config.json` — hand-editable, and everything the UI writes
lands here. Written atomically (temp file + fsync + rename), so a power cut
can't truncate it.

```jsonc
{
  "settings": {
    "host": "0.0.0.0",
    "port": 80,
    "brightness_mode": "scale",   // scale | native | both
    "connect_timeout": 12.0,      // seconds per connect attempt
    "write_timeout": 6.0,
    "attempts": 3,                // 1 try + 2 retries
    "retry_backoff": 0.8,         // doubles each retry
    "inter_frame_delay": 0.06,
    "inter_device_delay": 0.35,   // gap between devices: lets the AP breathe
    "scan_seconds": 8.0,
    "log_level": "INFO",
    "apply_on_boot": "Warm on"    // scene applied at startup, "" to disable
  },
  "groups": ["letters", "border", "base"],
  "devices": [
    { "address": "BE:FF:00:00:00:01", "name": "Letters 1",
      "groups": ["letters"], "enabled": true, "char_uuid": null }
  ],
  "scenes": [
    { "id": "vice", "name": "Vice", "steps": [
      { "target": "group:letters", "power": true, "color": "#ff2d78", "brightness": 100 },
      { "target": "group:border",  "power": true, "color": "#22d3ee", "brightness": 90 }
    ]}
  ],
  "schedules": [
    { "id": "dusk", "name": "Dusk", "scene": "Vice", "time": "19:30",
      "days": [], "enabled": true }
  ]
}
```

* **`target`** is `"all"`, `"group:NAME"`, `"device:AA:BB:CC:DD:EE:FF"`, a bare
  address, or a controller's friendly name.
* **`days`** is `0`=Monday … `6`=Sunday. Empty means every day.
* A scene step with `"power": false` turns that target off and ignores colour.
* After hand-editing, `sudo systemctl restart vice-lights`, or POST
  `/api/config/reload`. Schedules are re-read from the store on every tick, so
  schedule edits made through the UI take effect immediately.

---

## 7. HTTP API

Everything the UI does, curl can do. All BLE endpoints return a job id at once.

| Method | Path | Body / notes |
| --- | --- | --- |
| GET | `/api/state` | Everything: devices, groups, scenes, schedules, timers, clock |
| GET | `/api/status` | Queue depth, live jobs, per-device reachability (poll this) |
| GET | `/api/job/<id>` | One job |
| POST | `/api/apply` | `{target, color, brightness, mode, speed, power}` |
| POST | `/api/power` | `{target, on}` |
| POST | `/api/scene/apply` | `{scene}` |
| POST | `/api/queue/clear` | Drop everything still queued |
| POST | `/api/devices` | Create/update `{address, name, groups, enabled}` |
| DELETE | `/api/devices/<addr>` | |
| POST | `/api/devices/<addr>/test` | Blink it green |
| POST | `/api/scan` | `{seconds}` — queued like any other BLE job |
| GET | `/api/scan/result` | Last scan |
| POST/DELETE | `/api/groups`, `/api/groups/<name>` | |
| POST/DELETE | `/api/scenes`, `/api/scenes/<id>` | |
| POST/DELETE | `/api/schedules`, `/api/schedules/<id>` | |
| POST/DELETE | `/api/timers`, `/api/timers/<id>` | `{scene, minutes}` |
| GET/POST | `/api/time` | `{iso: "2026-08-28T19:30:00"}` or `{epoch: ...}` |
| GET/PUT | `/api/config` | Whole config |
| GET | `/api/log?n=200` | Log tail |
| GET | `/healthz` | Liveness |

```bash
curl -X POST http://192.168.4.1/api/apply -H 'Content-Type: application/json' \
     -d '{"target":"group:letters","color":"#ff2d78","brightness":80}'
curl -X POST http://192.168.4.1/api/scene/apply -H 'Content-Type: application/json' \
     -d '{"scene":"All off"}'
```

There is **no authentication** — anyone on the AP can control the sign. That's
the intent for a private network; keep the WPA passphrase to yourself.

---

## 8. AP / BLE contention

The Zero W has **one radio** shared between the wifi AP and Bluetooth. They
coexist by time-slicing, so heavy BLE traffic makes the web UI feel sticky. The
app already: serializes all BLE, holds at most one connection, and waits
`inter_device_delay` between devices.

**Test for contention** — from your phone or a laptop on the AP, ping the Pi
while a full 12-device scene is applying:

```bash
ping -i 0.2 192.168.4.1        # in one terminal
curl -X POST http://192.168.4.1/api/scene/apply \
     -H 'Content-Type: application/json' -d '{"scene":"Vice"}'
```

Occasional 100–400ms spikes are normal. If you see multi-second stalls or
dropped packets:

* Raise `inter_device_delay` to `0.6`–`1.0` (slower sweeps, calmer AP).
* Lower `attempts` to `2` so a dead unit gives up sooner.
* Move the AP to a quieter channel: `sudo iwlist wlan0 scan | grep -i channel`,
  then re-run the setup script with a different channel (1, 6 and 11 don't overlap).
* Disable wifi power save: `sudo iw wlan0 set power_save off`.

Watch it live: `journalctl -u vice-lights -f` and
`sudo btmon` (contention shows up as connect timeouts, not errors).

---

## 9. Operating notes

```bash
sudo systemctl status vice-lights
sudo systemctl restart vice-lights
journalctl -u vice-lights -f
tail -f /var/log/vice-lights.log        # rotates at 2MB, 3 backups
```

The service restarts on failure (`Restart=always`, 5s) and starts on boot. It
runs as root because it binds port 80 and sets the system clock.

**Troubleshooting**

| Symptom | Try |
| --- | --- |
| Every device unreachable | Check `hciconfig hci0` says UP RUNNING and `rfkill list bluetooth` says `Soft blocked: no` — see the row below; also make sure `elk_scan.py` isn't running at the same time |
| `hci0` DOWN, `rfkill` shows `Soft blocked: yes`, bluetoothd logs `Failed to set mode: Failed (0x03)` | `sudo rfkill unblock bluetooth && sudo hciconfig hci0 up`. The service also does this at every start |
| One device unreachable | Power-cycle it; check range; **Test** it from the UI; it may just be too far — the log names it every attempt |
| Colours look wrong | Some clones swap red/green wiring. Send pure red with `elk_scan.py color <addr> '#ff0000'` and rewire, or swap channels at the controller |
| Brightness does nothing | You're on `"native"`; switch to `"scale"` |
| UI reachable but nothing happens | Check the Queue card and the log — jobs may be queued behind a slow retry |
| `pip install` hangs on "Building wheel for dbus-fast" | Ctrl-C, re-run with `SKIP_CYTHON=1` (see above) |
| `backend: simulated` badge | `bleak` failed to import — search the log for `bleak` and reinstall requirements |
| Can't reach 192.168.4.1 | `nmcli con show --active` (or `systemctl status hostapd`), `ip addr show wlan0` |
| Schedules never fire | Clock unset — see the red banner; use timers instead |

**Dust and power.** Cold boots lose the clock (see §5) and half-written files
are the other classic SD-card death, which is why config writes are atomic.
Shutting down cleanly (`sudo shutdown -h now`) is still worth the habit.

**Developing off-Pi.** Set `VICELIGHTS_FAKE_BLE=1` to run the whole thing with a
simulated radio — no hardware, no bleak, real timings:

```bash
VICELIGHTS_FAKE_BLE=1 python3 -m vicelights --config ./config.example.json \
    --log ./vice.log --state ./time.state --port 8080
```
