# Vice Sign Lights

Headless controller for the sign's 12 ELK-BLEDOM Bluetooth LE RGB controllers, driven from
a phone over the Raspberry Pi's own wifi network. Built for an off-grid art sign:
no internet, no cloud, no CDN, no NTP.

* Runs on a **Pi Zero W (ARMv6), Raspberry Pi OS Lite 32-bit**
* The Pi **broadcasts its own wifi AP**; the UI lives at **http://192.168.50.1/**
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
| `vicelights/thermometer.py` | DHT11/DHT22 read, on demand. Retries and medians. |
| `vicelights/schedule.py` | Calendar-driven panel messages: today, tomorrow, temp. |
| `vicelights/qr.py` | Self-contained QR encoder for the Wi-Fi join code. |
| `vicelights/matrix.py` | BLE text panel: drivers, fingerprints, 5x7 font. |
| `vicelights/messages.py` | The message queue and its dwell timer. |
| `vicelights/web.py` | Flask JSON API. |
| `vicelights/templates/index.html` | Phone UI, all CSS/JS inline. |
| `vicelights/app.py` | Entry point (`python3 -m vicelights`). |
| `elk_scan.py` | CLI: scan / probe / flash / adopt. Shares `protocol.py`. |
| `matrix_probe.py` | CLI: identify the text panel, or lift its protocol off a capture. |
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
| Solid colour | `7e 00 05 03 RR GG BB 00 ef` | **Confirmed on the sign** — all 12 lit and were named |
| Power on | `7e 00 04 f0 00 01 ff 00 ef` | **Confirmed on the sign** |
| Power off | `7e 00 04 00 00 00 ff 00 ef` | **Confirmed on the sign** — 12/12 went dark |
| Brightness | `7e 00 01 BB 00 00 00 00 ef` (`BB` = 0–100, **not** 0–255) | Common, not universal |
| Pattern / mode | `7e 00 03 MM 03 00 00 00 ef` (`MM` = `0x80`–`0x9d`) | Common, not universal |
| Pattern speed | `7e 00 02 SS 00 00 00 00 ef` (`SS` = 0–100) | Common, not universal |

`fff3` is **write-without-response**, so the controller acknowledges nothing: a
write that "succeeds" only proves the connection and characteristic are right,
never that the bytes meant anything. Everything marked confirmed above was
confirmed by watching lights, not by a return code.

Print them all, no radio needed:

```bash
./elk_scan.py frames
```

### Brightness is locked at 100%

The sign runs flat out. `"force_full_brightness": true` (the default) overrides
every brightness value on its way to the radio — UI slider, saved scene, raw API
call, all of them — at a single point in the BLE worker, so no path can quietly
dim the sign. The UI hides its brightness slider when the setting is on and says
why.

Set it to `false` in `config.json` to unlock dimming end to end; the slider
reappears on the next page load and scene brightness values start mattering
again. Nothing else needs changing, which is why there is no dim scene shipped:
add one if you ever want it.

### Brightness policy (only if you unlock dimming)

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
sudo cp config.example.json /etc/vice-lights/config.json
sudo systemctl restart vice-lights
```

`install.sh` chooses the `dbus-fast` build by architecture — pure Python on
armv6 where no wheel exists, compiled everywhere else. You do not need to think
about `SKIP_CYTHON` unless you are installing by hand on a Zero W.

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

### Why `SKIP_CYTHON=1` (Pi Zero / Zero W only)

`install.sh` handles this for you; it matters when installing by hand.

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

## 7. Touch panel on the sign

A 5" DSI panel driven straight from the Pi, so the sign can be controlled with
no phone at all. `vice_kiosk.py` draws it with pygame on KMSDRM: scene buttons
grouped movement-first, OFF, NEXT and rotation, and nothing that can summon a
keyboard or change the fleet. Scales from 480x320 up.

    cd ~/vice-sign-lights && git pull && sudo ./scripts/update.sh
    sudo ./scripts/setup_kiosk.sh

### Why pygame and not a browser

The first attempt ran Chromium under the `cage` Wayland compositor. That is the
conventional answer and it wanted 123 packages and 486MB -- Chromium, xwayland,
LLVM, Vulkan drivers -- to draw a grid of buttons on a machine whose actual job
is writing nine-byte frames over Bluetooth. pygame needs 53 packages and no
browser at all.

It also removed a whole class of failure. A browser kiosk has to be told not to
offer devtools, not to zoom, not to navigate, and above all not to show the
"Restore pages?" bubble after the plug is pulled -- which on a screen with no
keyboard cannot be dismissed at all. None of that exists here.

### What is on it

Built from the Claude Design handoff (`VICE Panel.dc.html`, boards 2a-2c), at
the 800x480 the boards are drawn at. Everything is spaced from that, scaled by
one factor, so another panel size carries over.

A **fixed shell**: the sign preview, the control row and the tab row are
identical on every tab, and only the middle third changes. Nothing scrolls
vertically -- no control is ever below a fold, with one deliberate exception,
the Devices tab, whose whole point is a list too long to fit (see below).

- **The preview is the sign, and you tap it.** Every letter, cup and straw is
  a target: tap to add it to the selection, tap again to drop it. Selected
  zones get a ring, so the sign itself says what the next colour will land on,
  and "colour just the C" needs no chip. Picking a chip clears the shapes and
  vice versa -- they are two views of one choice.
- **The queue is in the shell**, in the control row under the sign, so every
  tab shows what the sign is doing: which job, how far through, and how many
  are waiting. A sweep is ~30s; without it a tap looks like nothing happened.
- **The preview is the sign, not a diagram.** Each letter, cup and straw is
  drawn in the colour that controller is actually showing. That needed a
  backend change: the worker records what it successfully wrote to each device
  and `/api/status` reports it as `showing`. A letter stands for both sides and
  dims if either is silent.
- **SCENES** is a horizontal shelf -- animated cards with the scene's own
  colours as a ramp, then solid pills. Swipe or tap `>` to page.
- **COLOUR** is target chips, twelve swatches, four named patterns and a speed
  track, all in one view.
- **LIGHTS** is a six-across grid; a controller that is not answering gets a
  dashed outline as well as red text, so it reads without relying on hue.
- The control row under the sign is OFF (scoped to the target), SURPRISE ME
  and ROTATE -- or, mid-sweep, the job and a **STOP**.

### The Status tab

Alerts, diagnostics, and the way to switch the Pi off cleanly. Nine rows,
judged on the server rather than in the panel -- "is this healthy" is a
statement about the sign, not about pixels, so the phone can show the same
answers. Each row says the consequence, not just the fact: "SIMULATED --
nothing is being sent to the lights" beats "simulated".

    Controllers   how many are answering, and which are not
    Bluetooth     the real radio, or the simulator
    Clock         set or never set -- an unset clock means schedules do not run
    Network       the addresses the phone UI is on
    Pi power      the firmware's sticky under-voltage bits
    Storage / Uptime / Version / Rotation

**Pi power is the one worth understanding.** Those bits latch until reboot, so
a brownout at 3am is still readable at breakfast, where the live bits would
have cleared long before anyone walked over. A marginal supply corrupts data
silently and announces itself no other way -- it is what turned an apt install
into a wall of hash mismatches during the build.

The diagnostics row is the verdict; the **System page keeps the log**. A
sampler on the scheduler thread reads the bits every 20s and records the
edges -- when under-voltage begins, when it clears -- each with a timestamp
(the wall clock if it is set, else uptime). `GET /api/pi-power` returns that
timeline; the phone's System tab and the touchscreen's System tab both show
it, green when the supply has never dipped, amber once it has, red while it
is dipping now.

### Shutting down

**SHUT DOWN PI**, behind a prompt that names the consequence rather than asking
"are you sure": the panel goes dark and does not come back, and the only way to
start it again is unplugging the Pi and plugging it in. Do it before pulling
power -- the config is written continuously, and a mid-write power cut is how
SD cards corrupt, which on this machine would take the mode audit and the
device mapping with it.

**REBOOT PI** is the gentler one and says so: the controllers keep running
whatever they were showing, because they do not need the Pi, and the sign is
back in about a minute.

While a prompt is up nothing else on the screen is live -- a stray finger on
the tab row cannot dismiss it by navigating away.

Both go through `POST /api/system`, which refuses anything other than
`shutdown` or `reboot` and refuses outright when not running as root. **Anyone
on the access point can call it**, the same as every other control here; that
is the deliberate no-auth design, and the passphrase on the AP is what stands
in front of it.

### Rolling starts

Writes are serialised one controller at a time, so a built-in pattern started
across twelve units already sweeps rather than snapping. **ROLL** on the Colour
tab makes that deliberate -- off, 0.5s, 1.0s, 2.0s of extra wait between
devices -- and the roll travels the `devices` array's order, which is the same
cup-first wipe every scene uses.

It works because the controllers' own timing is not tight. A perfectly
synchronised stagger would leave every unit a fixed step out of phase and read
as a mechanical chase; the drift between twelve cheap controllers scatters them
instead, and the pattern arrives looking staggered rather than sequenced.

Four scenes ship with a roll: Drift 1.0s, Jump cut 0.8s, Two faces 1.2s,
Carousel 1.5s. A scene stores its own `stagger`, and `/api/apply` and
`/api/scene/apply` both take one.

**It costs wall clock.** At the measured 2.5s a device, twelve units take 30s
with no roll and 48s at 1.5s. That is radio time on the chip also serving the
access point, so at a 5-minute rotation a 1.5s roll is about 16% busy rather
than 10%. Worth it on a few scenes, not on all of them.

### One job, however many zones

Selecting several zones sends `targets` -- a list -- rather than one `target`
string, and the API resolves the union into a single job. Posting once per
device would have worked, but it would put one tap in the queue as five
entries, and the queue is the thing that tells you a sweep is running.

### Fonts

The design specifies Caprasimo for display and Figtree for body. Neither ships
with Pi OS Lite. Drop `Caprasimo-Regular.ttf` and `Figtree-SemiBold.ttf` into
`/opt/vice-sign-lights/fonts/` to get the intended look; without them a bold
DejaVu stands in, which keeps the weight contrast the layout depends on but not
the character.

### Not built yet

Board 2d is a playlist builder -- a spin wheel, a drag-reorderable queue, and a
**dwell time per step**. Rotation currently has one interval for every scene, so
that one needs a backend change before the screen is worth drawing.

Two more from the boards need concepts that do not exist here: scenes
attributed to a campmate ("Sam's one"), and SAVE THIS, which needs a name and
so a keyboard -- the panel says to use the phone for it rather than pretending.

### What is on it (previous layout)

Three tabs, and a target that persists across them.

- **SCENES** -- every saved scene, movement first, the playing one outlined.
- **COLOUR** -- pick what to act on, then a colour or a pattern. Targets are
  Everything, then every group in the config, so creating a group from the
  phone makes it appear here with no code change.
- **LIGHTS** -- all twelve controllers with live reachability. Tapping one
  makes it the target and jumps to COLOUR, which is how you set a single
  letter without touching the rest of the sign.

`OFF` acts on the current target, not always the whole sign, so it can black
out one straw and leave everything else running. `NEXT` and `ROTATE` are
whole-sign by nature and ignore the target.

Only the multi-colour patterns get buttons. A single-colour fade needs speed
85+ to be visible at all (see **Fade speed**) and reads as a solid colour
otherwise, which is a poor thing to offer as a one-tap choice.

### It talks to the same API a phone does

The panel is a separate process from the service, speaking HTTP to
`127.0.0.1`. It cannot wedge the sign: if it crashes, the lights carry on and
the web UI still works, and systemd restarts it. It imports nothing from the
`vicelights` package, so it runs on the system interpreter with pygame from
apt, outside the venv.

When the API is unreachable it says "cannot reach the sign" and keeps trying,
so the panel surviving a service restart needs no coordination between them.

### The panel opens and then hangs before drawing

`pygame.font.SysFont` shells out to `fc-list`, which Pi OS Lite does not ship.
Rather than failing it stalls, so the log shows the display opening
successfully and then nothing at all -- which reads as a graphics problem and
is not one:

    display: native fullscreen -> 800x480
    UserWarning: 'fc-list' is missing, system fonts cannot be loaded

The panel opens font files by path now and never calls `SysFont`. Installing
`fontconfig` would also work, but a touch panel should not need a font
database to draw twelve buttons.

### "EGL not initialized"

SDL's KMSDRM backend dlopens libEGL at runtime instead of declaring it as a
dependency, so `python3-pygame` installs and imports perfectly happily on a
machine with no EGL driver at all, then fails at `set_mode`. The message reads
like a configuration problem and is really a missing package. What makes it
easy to miss is that `libgbm1` **is** a hard dependency and gets pulled in,
so a quick look suggests the graphics stack is present.

    dpkg -l libegl1 libegl-mesa0 libgles2 libgbm1 | grep ^ii

All four should be there. If only `libgbm1` is:

    sudo apt-get install -y --no-install-recommends libegl1 libegl-mesa0 libgles2

`setup_kiosk.sh` names them explicitly now, so this only bites an install that
predates that, or one where apt failed partway through.

Two things that look like this but are not: the panel showing a console login
prompt means `getty` holds the display (see below), and a `set_mode` failure at
an explicit resolution can mean the size does not match a mode the connector
advertises -- the panel asks for the native mode first for that reason.

### The picture works but taps do nothing

Three causes, cheapest first.

**SDL delivers finger events, not mouse events.** Its touch-to-mouse
translation is off under KMSDRM, so a panel that only handles MOUSEBUTTONDOWN
draws perfectly and ignores every touch. Both are handled now; this is only
history if you are reading old code.

**SDL cannot see the touchscreen.** The log says so on startup:

    panel: SDL sees 0 touch device(s)

Zero, with a working picture, means the input side is the problem. Check the
kernel found it at all:

    cat /proc/bus/input/devices | grep -iA4 touch

If the kernel has it and SDL does not, take the input from the kernel directly
and leave SDL to draw:

    VICE_KIOSK_INPUT=evdev

**The overlay.** On the generator build, a DSI panel whose picture works while
touch does not is fixed by swapping `dtoverlay=vc4-kms-v3d` for
`dtoverlay=vc4-fkms-v3d` in `/boot/firmware/config.txt`. Try this last: it
changes how the display comes up as well, so verify the picture again after.

### The layout, second edition: built for the two jobs

The touchscreen is the interface when the phone is not around, and its two
jobs are *change what the lights are doing* and *see what the system is doing
and make it stop*. The second edition reshapes the screen around exactly
those, after a design review that scored three competing layouts against the
failures that actually happened at this sign.

**Five tabs** -- Scenes, Colour, Devices, LED Text Display, System. The old
per-device *Lights* grid is still gone as a colour picker -- the sign preview
in the header *is* that (tap a letter, a cup, a straw). What came back, by
request, is **Devices** as a troubleshooting list: every controller, whether
it is answering, what colour it is showing, its address and group and last
round-trip time, and a **TEST** that pokes one on its own so you can watch for
the blink. It is the one tab that scrolls -- twelve controllers do not fit a
no-scroll screen, and this is exactly the list you run a finger down when
something is dark. Drag to scroll (a real drag, distinct from a tap on TEST);
reading it needs no unlock, but TEST writes to a light, so it waits for one
like any other action. TEST ALL pokes the whole set.

**The clock tile** sits in that top-right corner (it once held the battery
budget, before a hardware low-voltage disconnect took that job over). It shows
the time the sign keeps and, in amber, `CLOCK NOT SET` -- which is exactly when
a wall-clock schedule and the event calendar quietly do nothing, surfaced up
here rather than only on the System tab.

**The ACTIVITY strip** replaces the passive progress readout beside the tabs.
While anything is running or queued it shows the job, its progress, and a
pink **STOP** that kills the in-flight write and drops the queue -- one tap,
from any tab. STOP takes no touch slop: a near miss aimed at the readout
beside it lands nowhere rather than cancelling the queue. Idle, the strip
shows the most urgent fact rather than a hint, in strict order: controllers
down > rotation hold > the next-scene countdown. Tapping the strip opens
System.

**System** is the troubleshooting-and-connect tab. Its left is the **Pi power
log** -- steady/brownout headline plus the timeline of under-voltage events
(§9) and any controllers not answering. Its right is **Wi-Fi**: the SSID and a
join QR, tap to enlarge to a full-screen code (§12). One row of levers runs
along the bottom: STOP EVERYTHING, CLEAR QUEUE (drops queued jobs, lets the
running write finish), RETRY DOWN, and POWER… (a reboot/shutdown chooser --
one entry point instead of two buttons). The running job and its progress live
in the control row on every tab, so the detailed queue list no longer needs a
home here; the running item's own words -- "unreachable 4x, skipping for
another 112s" -- ride along there.

**Colour** became four fixed rows -- targets, swatches, patterns, speed --
that sum to exactly the middle's 228 pixels. The round V/I/C/E chips and Side
A/B are gone (the preview letters are the picker; per-side stays on the
phone), which is what buys a single un-wrapped 48-pixel target row. The speed
slider now says "pick a pattern first" and dims when it would do nothing,
instead of silently storing a value.

Dropped entirely: SAVE THIS and PUT IT BACK (phone jobs), the Lights tab, the
idle hint strings, and a handful of dead code. Scenes' shelf arrows now only
appear when there is somewhere to page to, swiping the solid row pages the
solid row, and stale page offsets clamp when scenes are deleted.

### Tap targets on a small panel

The layout was drawn for a 5-inch panel and now runs on a 4.3-inch one. Same
800x480, so nothing moved -- but 4.3 inches puts 8.6 pixels in a millimetre
against the 5-inch panel's 7.3, and a fingertip covers eight to ten
millimetres. Measured against that, every control on the screen was under 9mm
across its short side and the tab pills -- the most-tapped thing on it -- were
3.0mm.

Redrawing everything finger-sized does not fit: the middle of the layout is
about 230 pixels tall once the sign band, the tab row and the bottom bar have
taken theirs. So the hit area is separated from the picture. A tap that lands
outside every control goes to the **nearest** one within 18 pixels, which
makes a 35-pixel pill behave like a 71-pixel one without looking like one.

Nearest-centre rather than padded rectangles, deliberately: padding two
neighbours until they overlap makes the winner depend on which was drawn
first, so a tap between two swatches would pick the left one every time rather
than the one it was closer to. A direct hit always wins, so a confident tap is
unchanged.

Controls grew as well, because slop only helps where there is empty screen
next to one:

| Row | Was | Now | Where the pixels came from |
| --- | --- | --- | --- |
| Tab row | 46px, 26px pills | 72px full-width pills, 19pt | see "third edition" below |
| Pattern row (*Colour*) | 34px | 46px | the tab's own spare room |
| Target chips (*Colour*) | 34px | 42px | as above; they are the only controls that wrap |
| Status actions | 40px | 48px | the tab's own spare room |
| Panel controls | 40px | 48px | as above |
| Keyboard keys | 38px | 58px | the tab row, which the keyboard now covers |

The sign band went from 122 to 110 pixels, which still leaves 88 pixels of
card around 70-pixel cups. The middle lost 6, measured against the tightest
tab -- *Panel* with a full queue of eight messages -- which had 24 to give and
now has 10.

The keyboard covers the tab row now rather than starting below it. Nothing
outside the keyboard answers a tap while it is up, so that row was 64 pixels
of screen a keyboard could be using, and key size is the whole experience
there. Keys went from 4.4mm to 6.8mm.

What a finger gets, measured per control with its neighbours allowed for:

| Control | Reachable |
| --- | --- |
| speed slider | 6.3mm |
| ROLL | 7.0mm |
| keyboard backspace | 7.1mm |
| compose buttons | 7.3mm |
| target chips | 8.4mm |
| STOP, System actions | 8.4mm |
| keyboard keys | 9.0mm |
| colour swatches | 9.0mm |
| tabs | 12.6mm |

Nothing is below 6mm. The keyboard used to set that floor at 5.6mm and stopped
once it was given the tab row's height.

Pinning the *Status* action row inside the middle fixed a bug found while
measuring: the unreachable-device strip only appears when something is down,
and with it on screen that row ran twelve pixels off the bottom -- which is
precisely when someone is reaching for **RETRY DOWN**.

`touchtest.py` asserts all of this: that a direct hit still wins, that a miss
up to 18 pixels out lands on the intended control, that a tap 120 pixels away
still hits nothing, that the nearer of two neighbours wins on both sides of
the seam, and that no control falls below 5.5mm.

### The layout, third edition: navigation at the bottom, and a lock

Two more things came out of using it at the sign.

**The tabs and the action row traded places.** The tabs are now the bottom
row and OFF / SURPRISE ME / ROTATE sit directly under the sign preview. A
thumb reaches the bottom of a panel this size without the hand covering the
preview, and every phone in every pocket already puts navigation at the
bottom, so nobody has to be told which end to look at. The action row also
ends up next to the thing it talks about.

| | Was | Now |
| --- | --- | --- |
| Tab row | top, 64px, 26-48px pills, 13pt | bottom, 72px full-width pills, 19pt |
| Action row | bottom, 52px | top, 56px |
| Tab reach | 9.5mm | 12.6mm |

Pills are sized to their own text plus an equal share of the leftover, rather
than four equal cells. `LED Text Display` is three times the width of
`Colour`; equal cells would be sized for the longest label and leave that one
nearly touching its own edges while the other three sat in acres of nothing.

The middle keeps its 228 pixels **to the pixel** -- the Colour tab is four
fixed rows that come to exactly that -- so the eight pixels the action row
gives up and the eight the tab row takes come from each other, not from
content. The last two came from the margin under the tab row, which is now
2px: a thumb aimed at the bottom edge of a panel wants something there, and
spending that margin here keeps a full 12 pixels of clearance between the
tabs and the row above, which is what stops the speed slider's touch target
from being crowded.

**The control row has three states**, because each wants all 800 pixels:

- *Idle*: OFF, SURPRISE ME, ROTATE.
- *Mid-sweep*: the job, its progress, and a 150x48 **STOP** -- 56% more
  button than the old 96x48. The three actions stand down while this is up,
  deliberately: all three queue more radio work, and mid-sweep that is the
  wrong answer to every question. STOP sits at the right, where ROTATE was
  rather than where OFF was, so a finger already travelling toward OFF when a
  sweep starts lands on dead label instead of throwing the queue away.
- *Locked*: a single **TOUCH TO UNLOCK** bar, carrying the running job too.

**`Panel` became `LED Text Display`.** On a sign whose other twelve devices
are panels of a sort, `Panel` named the thing by what the code calls it
instead of by what you would point at.

### The lock

The screen is bolted to a sign in a crowd at the height of a passing hand,
and every control on it writes to the lights. It now starts locked and
relocks after **90 seconds** untouched.

Locked means *no control writes to the sign*. It does not mean the screen
goes away, and that distinction is the whole design: the sign preview, the
clock tile, the health chips and **all four tabs stay live**, because the
failure this panel exists to catch is noticing at 3am that something is
wrong, and a screen that must be unlocked before it will tell you anything is
a screen nobody looks at. Moving between tabs changes nothing about the sign,
so looking is free; only acting costs a tap.

A tap on an inert control says `Locked — touch UNLOCK first` rather than
doing nothing, because a dead tap reads as a dead panel. Relocking clears a
half-typed message and any open prompt: unlocking into someone else's
unfinished business means the first tap lands somewhere it was never aimed.

While a sweep is running, a locked screen still shows the job and its
progress -- seeing costs nothing. Stopping it costs one unlock first, which
is the trade.

`locktest.py` covers it: that no control fires while locked, that all four
tabs still open, that one tap unlocks and the actions come back, that it
relocks on the timer but not before, that interacting pushes the deadline
out, and that relocking drops the keyboard and any prompt.

### A live button under an overlay

Found while re-rendering the keyboard, and older than this layout: the
control row was drawn *underneath* the compose box, and a control drawn under
an overlay is still live. With a sweep running, STOP sat exactly under the
text field -- so tapping where you were typing dropped the queue, and a tap
slightly off hit the activity strip and jumped to System, losing the message.
The row is no longer drawn at all while the keyboard is up.

### If the panel is dark, or ignores touches

Check whether the service is running first: `systemctl status vice-kiosk`. A
running service with a dark panel is a display problem, not an app one.

`setup_kiosk.sh` adds the user to `video`, `render` and `input`. Group changes
only apply to a new login, so **reboot once** after setup before investigating
anything else. Missing `input` in particular draws the UI perfectly and ignores
every touch, which is a confusing way to fail.

    ls /sys/class/drm | grep -i dsi        # the kernel should enumerate a DSI connector
    cat /sys/class/backlight/*/brightness  # and the backlight should be non-zero

If no DSI connector appears, the ribbon is the usual cause: it must be seated
contacts-toward-the-board in the DISPLAY port (not CAMERA), with the Pi powered
off while seating it. Some third-party panels also want a `dtoverlay` line in
`/boot/firmware/config.txt`; check the panel's own instructions.

### Rotation

Not wired up, because it depends on how the panel physically mounts and that is
not known until it is on the sign. If it comes up upside down, say so rather
than guessing at `config.txt` values.

### Updating

```bash
cd ~/vice-sign-lights && git pull && sudo ./scripts/update.sh
```

That copies the code into `/opt/vice-sign-lights` and restarts both services --
`vice-lights` for the web UI and the sign, and `vice-kiosk` for the
touchscreen, which runs its own copy of `vice_kiosk.py` in its own process. It
used to restart only the first, so a change to the touchscreen looked like an
update that did nothing.

`/etc/vice-lights/config.json` is never touched: scenes, devices and panel
settings all survive an update.

### Diagnostics without a laptop

At the sign there is a phone, a touchscreen and nothing else -- so a check
that needs SSH is a check that does not get run. The *System* tab has a
**Checks** card that runs them on the Pi and shows the output on the phone:

| Button | What it runs |
| --- | --- |
| Go / no-go | `scripts/preflight.sh` -- services, AP band, radio, controllers, panel, disk, clock |
| Bluetooth and system | `scripts/diagnose.sh` -- adapter state, throttling, temperature, what is advertising |
| Service journal | the last 300 lines systemd holds, including whatever killed the service before it could log |

The exit code is reported alongside the output, because preflight's whole
purpose is its verdict and a wall of ticks with one failure buried in it reads
as a pass.

**Fixed commands, chosen by name from a table in `web.py`.** Nothing from the
request reaches a command line. The service runs as root on a network whose
passphrase is in this repository, so "run what you are told" would be a remote
root shell; adding a check means adding an entry to that table.

Scanning is already a button on the *Devices* tab, and it queues through the
BLE worker like any other job rather than shelling out -- two things scanning
the same radio at once is how you get a scan that finds nothing.

Not offered as a button, deliberately: `scripts/ble_connect_test.sh`. It takes
the access point down as part of the measurement, which would disconnect the
phone you are reading the result on.

### Turning the panel off

    sudo systemctl disable --now vice-kiosk

The web UI on a phone keeps working regardless; the panel is an addition to it,
never the only way in.

### A corrupt config can no longer stop the sign from starting

`load()` used to call `json.load()` with nothing catching it, so an unparseable
`/etc/vice-lights/config.json` raised at startup, crash-looped forever under
`Restart=always`, and never bound port 80. That is the same symptom as the
disabled-unit failure below -- no web UI at all -- but recoverable only over
SSH, which in a desert at night means the sign stays dark.

Saves are atomic and fsync the directory, so a power cut mid-write leaves the
old file intact. Card corruption from repeated unclean shutdowns is the real
risk, and it is not a remote one on a Pi that gets switched off by pulling a
plug for a week.

Now a config that will not parse is copied aside as
`config.json.unreadable-<epoch>` and the service recovers, in order, from:

1. `config.json.lastgood` -- written every time a config parses cleanly
2. `config.json.bak` -- left by `retune_fades.py` and `reorder_devices.py`
3. the built-in defaults, as a last resort

Only the third loses your devices and scenes, and even then the UI comes up so
you can see what happened from a phone. Tested against a truncated file, binary
garbage, a zero-length file, and garbage with only a `.bak` surviving; the
first three recover the full fleet.

### The service must be enabled, not just started

`systemctl restart` works on a unit that is not enabled for boot, so a sign
deployed with `update.sh` alone runs perfectly until the first power cut and
then never comes back. This was found the hard way: the web UI was serving all
day, `wlan0` came up as an access point after a reboot, a phone associated and
got a DHCP lease, and nothing loaded -- because the unit had never been armed.

Check it, and not just whether it is running:

    systemctl is-enabled vice-lights    # must say: enabled
    systemctl is-active  vice-lights    # must say: active

    sudo systemctl enable --now vice-lights

`install.sh` enables it; `update.sh` now does too, and both fail loudly rather
than reporting success when the service is not up afterwards. Worth confirming
before you leave, along with the reboot test -- a sign that cannot survive
being switched off is not finished.

Note the journal here is volatile, so `journalctl -u vice-lights` shows nothing
from before the last boot. The service also writes `/var/log/vice-lights.log`,
which does persist; look there for what happened before a restart.

### Updating

**The service runs from `/opt/vice-sign-lights`, not from your checkout.**
`git pull` alone changes nothing about what is running — it updates the CLI in
your working copy only. To ship code changes to the service:

```bash
cd ~/vice-sign-lights && git pull && sudo ./scripts/update.sh
```

`update.sh` copies the code across, reinstalls the unit file if it changed,
restarts, and prints the tail of the log. It skips apt and pip, so it takes
seconds; use `install.sh` when dependencies change.

Every startup logs which tree it is running and the revision that was
installed, so this is never a guess:

```
running from /opt/vice-sign-lights/vicelights (installed from 8c41948 Log the frames a job actually sends)
```

Running an old service against a new config is worth avoiding for a second
reason: the config store drops fields it does not recognise, and it rewrites the
file after the first successful write to each device. A `channels` order saved
by a newer CLI would be silently erased by an older service.

### Who owns the config

`/etc/vice-lights/config.json` is read and written by two things: the service,
running as root, and the CLI, running as you. `install.sh` hands the directory
to the installing user so the CLI works without `sudo`.

That ownership used to evaporate. Saves are atomic — write a temp file, rename
it over the original — which creates a *new* inode owned by whoever wrote it. So
the first time the service cached a characteristic UUID, root took the file back
and every `chown` you had done was undone. The save now carries the original
file's owner and mode across, so it stays where you put it.

If an older install already lost it:

```bash
sudo chown -R $USER /etc/vice-lights /var/lib/vice-lights
```

### The CLI finds its own interpreter

`elk_scan.py` needs `bleak`, which lives in a virtualenv. Rather than requiring
you to remember which shell has it activated, the script re-runs itself under
one that does — checking the installed `/opt/vice-sign-lights/venv` first, since
the service runs from it:

```
$ python elk_scan.py scan
bleak is not in /usr/bin/python3; re-running with /opt/vice-sign-lights/venv/bin/python
scanning 8s ...
```

So any of these work, from any directory:

```bash
python elk_scan.py scan
python3 elk_scan.py scan
/opt/vice-sign-lights/venv/bin/python elk_scan.py scan
```

If no virtualenv can be found, it names the interpreter that failed and how to
install into it, rather than suggesting an install that already happened.

### Find your controllers

Stop the service first — only one process may own the radio. `install.sh` hands
`/etc/vice-lights` to the user who ran it, so the CLI can edit the config
without `sudo`; if you hit `Permission denied` on an older install, either run
`sudo chown -R $USER /etc/vice-lights` once, or invoke the CLI as root with the
venv's interpreter (`sudo ~/venv/bin/python elk_scan.py ...` — plain
`sudo python` has no bleak):

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
python elk_scan.py identify
```

It writes to `config.json`, which is gitignored. If only the tracked
`config.example.json` exists it forks a copy first, so your names never end up
in a file that blocks the next `git pull`.

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

### If a colour comes out wrong

These controllers are cheap and the RGB pads are not always wired the way the
firmware assumes, so asking for red can produce green. Find out which:

```bash
python elk_scan.py channels BE:68:F1:BB:DB:04 --save
```

It sends pure red, then green, then blue, and asks what you actually saw. If the
answers are a permutation of r/g/b it saves that device's `channels` order and
every frame for that controller is permuted from then on — ask for red, get red.
The setting is **per device**, so one miswired unit does not affect the other
eleven.

The command also separates the cases that are *not* wiring:

| What you see | Meaning |
| --- | --- |
| A clean r/g/b permutation | Swapped pads. Saved and corrected automatically. |
| Nothing changes | Likely stuck in a built-in pattern — power off, then set a colour |
| White for every primary | More than one channel driven at once: shorted wiring, or the strand isn't analog RGB |
| Anything else | Not a simple swap — capture what you saw for each probe |

### If a colour won't take after using a pattern

Many ELK-BLEDOM firmwares keep animating after a solid-colour frame: the colour
frame lands, the unit ignores it, and the strand carries on strobing. From the
UI that reads as "colours aren't setting" — the bytes on the wire are correct
and the light disagrees.

**Measured on this sign: not needed.** `unstick` reports that a plain colour
frame exits a pattern on all three strategies, so `exit_pattern` defaults to
`none` and nothing extra is sent. The machinery stays for a replacement
controller that behaves differently. To re-check:

```bash
python elk_scan.py unstick BE:68:F1:BB:DB:04
```

It starts an obvious animation, tries each escape, and asks whether the strand
went steady green.

| `exit_pattern` | Frames sent before the colour |
| --- | --- |
| `none` | nothing — a colour frame alone is enough |
| `static_mode` | `7e 00 03 86 03 00 00 00 ef` (static white) |
| `power_cycle` | off, then on |

Set the cheapest one that works in `config.json` under `settings`. The escape
is prepended **only** on a pattern→colour transition, so an ordinary colour
change is a single write either way. `power_cycle` causes a brief blink on that
transition; prefer `static_mode` or `none` where they work.

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

The command lights the controller white before each attempt — an off frame is
invisible on an already-dark light, so without that every variant looks like it
worked. Watch for white → dark.

**On this sign, variant 0 is correct**: `all off` blacked out all 12. The
alternates are here for a replacement controller that behaves differently.
Variant 2 always appears to work because it's just the colour frame with zeros,
but it leaves the controller powered — fine for a scene, wrong for "off for the
night".

### The sign

Twelve controllers: **VICE** spelled once per face, plus a cup and a straw per
face. `config.example.json` carries the map, so the groups below already exist:

| Group | Members |
| --- | --- |
| `letters` | A_V A_I A_C A_E B_V B_I B_C B_E |
| `drink` | A_Cup A_Straw B_Cup B_Straw |
| `cup` / `straw` | the two of each |
| `side-a` / `side-b` | everything on one face |
| `V` `I` `C` `E` | both faces of one letter — for chases across the word |

A controller belongs to as many groups as you like, so `A_Cup` is in `drink`,
`cup` and `side-a` at once. Delete any you don't want from the Devices tab.

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
to `192.168.50.1/24`, autoconnect on. It comes back on its own after a reboot.

The script warns before switching (five seconds to Ctrl-C), checks the adapter
advertises AP mode, installs `dnsmasq-base` if missing, and gives up after 30
seconds rather than hanging — printing the NetworkManager log and the likely
cause if activation fails.

`autoconnect` is deliberately left **off** until the profile activates
successfully at least once. A broken AP profile that starts at boot takes the Pi
off every network it knows, on every boot, and the only way back in is the SD
card — so the script only makes it persistent after it has proven it works.

**If it fails to come up**, the usual reasons in order:

| Symptom | Cause |
| --- | --- |
| Activation hangs, then fails | `dnsmasq-base` missing — `ipv4.method shared` cannot serve DHCP |
| `iw list` shows no `* AP` | The adapter will not host an AP; use the hostapd path |
| No network appears at all | wifi soft-blocked (`sudo rfkill unblock wifi`) or no regulatory domain set |

The profile is left saved but inactive on failure, so nothing is half-applied.
Remove it with `sudo nmcli connection delete vice-ap`, or fall back to
`setup_ap_hostapd.sh`.

### Bullseye, or if NM AP mode misbehaves — hostapd + dnsmasq

```bash
sudo ./scripts/setup_ap_hostapd.sh "ViceSign" "yourpassphrase" 6
sudo reboot
```

Writes `/etc/hostapd/hostapd.conf`, a dnsmasq DHCP range of
`192.168.50.10–60`, a static `192.168.50.1` on `wlan0`, and tells NetworkManager
to leave `wlan0` alone if it's installed. Its dnsmasq also resolves *every*
hostname to the Pi, so a phone browser lands on the UI whatever you type.

### Pick a subnet that does not collide with home

The AP defaults to `192.168.50.1/24`, chosen because almost nothing ships
on it. The setup script **refuses to run** if that subnet overlaps anything
this machine already uses, and says what to pass instead.

That guard exists because getting it wrong is not a small mistake. This sign
was briefly configured with the AP on `192.168.4.1` -- the same address as
the house router. `wlan0` took the gateway's address, the Pi began resolving
DNS against itself, and dnsmasq's catch-all answered every name with the Pi.
It lost apt, git and its own uplink in one step. At the sign, recovering that
needs a keyboard.

**Check what your home network uses anyway:**

```bash
ip addr show wlan0 | grep 'inet '
```

This Pi sits on a home LAN of `192.168.4.0/22`, which spans 192.168.4.0 to
192.168.7.255 — so anything in that range is taken, and `192.168.4.1` is the
router itself. The default of `192.168.50.1` is outside it, which is the point.

If your own LAN happens to use `192.168.50.x`, pick something else:

```bash
AP_ADDR=10.42.0.1/24 sudo ./scripts/setup_ap_networkmanager.sh "ViceSign" "passphrase"
AP_IP=10.42.0.1      sudo ./scripts/setup_ap_hostapd.sh        "ViceSign" "passphrase"
```

The hostapd script derives its DHCP range from whatever address you give it, and
refuses anything that overlaps a network this machine already uses. Substitute
your chosen address wherever this README says `192.168.50.1`.

### Switching between the AP and normal wifi

The AP has **no uplink**, so while it is up you cannot `git pull`, `apt install`
or reach anything off the Pi. `scripts/ap.sh` moves between the two:

```bash
sudo ./scripts/ap.sh status          # which mode, which address, what else is saved
sudo ./scripts/ap.sh off             # rejoin your normal wifi (internet, iteration)
sudo ./scripts/ap.sh off 'MyWifi'    # ...or a specific saved network
sudo ./scripts/ap.sh on              # host ViceSign again (playa mode)
```

`off` also clears the AP profile's autoconnect, so it will not reappear on the
next reboot until you run `on`.

Either switch drops the connection you are running over. `--delay` re-runs the
switch as a detached `systemd-run` timer so your command returns first:

```bash
sudo ./scripts/ap.sh off --delay 5
```

Then reconnect on the other network. Coming **from** the AP, the Pi returns to
whatever address your router gives it; going **to** the AP, it is always
`192.168.50.1`.

If you are locked out with the AP up: join **ViceSign** from a laptop and
`ssh <user>@192.168.50.1`.

### Locked out with no network at all

If the AP failed *and* the Pi will not rejoin your wifi — NetworkManager
retrying a broken `vice-ap` instead of falling back — recover in this order:

1. **Power cycle**, wait three minutes, then check your router's client list and
   for the `ViceSign` network. NetworkManager often falls back after enough
   failed attempts.
2. **SD card in a Linux machine** (WSL counts). Delete the profile from the
   rootfs partition and boot the Pi again:
   ```bash
   sudo rm /mnt/rootfs/etc/NetworkManager/system-connections/vice-ap.nmconnection
   ```
3. **On a Mac (or any machine without ext4)** — the rootfs is unreadable, but
   the boot partition is FAT32 and mounts anywhere. Turn on USB gadget
   networking from there and get a shell over the USB cable:

   ```bash
   printf '\n[all]\ndtoverlay=dwc2\n' | sudo tee -a /Volumes/bootfs/config.txt
   sudo sed -i '' 's/rootwait/rootwait modules-load=dwc2,g_ether/' /Volumes/bootfs/cmdline.txt
   cat /Volumes/bootfs/cmdline.txt        # must still be ONE line
   diskutil eject /Volumes/bootfs
   ```

   Edit these in Terminal, never in TextEdit — smart quotes break the boot.
   Then put the card back, connect a USB cable to the Pi's **USB** port (not
   PWR — it powers the Pi too), wait ~90s for the gadget interface to appear in
   System Settings → Network, and `ssh <user>@<hostname>.local`.

### Moving the card to a Pi Zero 2 W

The SD card is portable: Raspberry Pi OS ships kernels for every model and the
bootloader picks the right one, so the card boots unchanged. Hostname, SSH host
keys, wifi profile, service and config all carry over.

Three things do change.

**The IP address.** Different board, different MAC, so the router hands out a
new lease. Use `http://<hostname>.local`, which the service prints at startup,
or check the log:

```bash
journalctl -u vice-lights -n 40 --no-pager | grep "web UI"
```

**`SKIP_CYTHON` stops being necessary — and leaving it in place wastes the
upgrade.** The Zero W is armv6, where piwheels has no compiled `dbus-fast`, so
the install used the pure-Python fallback. A Zero 2 W is armv7, where the
compiled wheel exists. Reinstall to pick it up, or the D-Bus layer stays exactly
as slow as it was:

```bash
sudo /opt/vice-sign-lights/venv/bin/pip install --force-reinstall --no-cache-dir dbus-fast
sudo systemctl restart vice-lights
```

**Power draw goes up.** A Zero 2 W under load wants a supply that can hold 5V at
2A or better. An adequate-for-a-Zero-W supply may brown out under a BLE sweep,
which looks like random controller failures rather than a power problem.

Then re-measure, and compare against the Zero W baseline of 4.13s per device:

```bash
journalctl -u vice-lights --no-pager | grep "phase averages" | tail -2
```

Keep the old Zero W. It works, and a spare board that has already been proven
against these controllers is worth more in the desert than on a shelf.

### Rebuilding from scratch

Nothing on the Pi is irreplaceable. `config.example.json` carries all twelve
addresses, their physical names, the groups, the scenes and every setting that
was measured rather than guessed — so a fresh card is about twenty minutes and
the identify walk does not need repeating.

1. Flash Raspberry Pi OS Lite with Raspberry Pi Imager. In its settings, set the
   hostname, enable SSH, and give it your wifi — that alone avoids most of the
   ways this gets painful.
2. Then:

```bash
sudo apt update && sudo apt install -y git
git clone -b claude/elk-bledom-pi-controller-bfabcl   https://github.com/tylers131/Vice-sign-lights.git vice-sign-lights
cd vice-sign-lights
sudo ./scripts/install.sh
sudo cp config.example.json /etc/vice-lights/config.json
sudo systemctl restart vice-lights
sudo ./scripts/enable_usb_console.sh      # before anything can strand you again
```

Leave the access point until last, as before.

### Set up the USB console before you need it

A Zero W with broken wifi has no ethernet and no console. Enabling the gadget
interface in advance turns a card-pulling exercise into plugging in a cable:

```bash
sudo ./scripts/enable_usb_console.sh && sudo reboot
```

It is idempotent, backs up both files it touches, and keeps `cmdline.txt` on one
line. Worth doing before the sign ships — a USB cable is a lot easier to find in
the dust than a card reader and a laptop that can mount ext4.

Current versions of the setup script avoid this by not enabling autoconnect
until the AP has come up once, but a profile made by an older version can still
strand you.

Either way:

* Join **ViceSign** from your phone, open **http://192.168.50.1/**
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

> **Built-in pattern names start as guesses.** The mode list comes from general
> ELK-BLEDOM documentation and does not describe this hardware — 0x9a,
> documented as "Flash 7 colour", flashes a single colour. Audition them and
> record what they really do (below); the picker then shows your names, with
> ● marking the ones someone has actually watched.

**Control** — pick a target (All / a group / one controller), pick a colour and
brightness, hit **Apply**. *Live apply* sends as you drag; it debounces in the
browser and the server coalesces superseded jobs, so you can't build a backlog.
Built-in patterns are optional and are listed by name. The **Queue** card shows
the running job, per-device progress, and which units got skipped.

**Scenes** — one tap applies a saved scene. Save the Control tab's current
settings as a new scene, or **Add step to scene** to build a multi-group look
(letters pink, border cyan, base purple). If two steps name the same controller,
the last one wins and it is still written only once.

**Timing** — scene rotation (below), setting the Pi's clock from your phone,
relative timers, and wall-clock schedules (scene + time + optional days).

**Devices** — per-controller status dot (green = last write succeeded, red =
unreachable, grey = not tried yet), plus last error, round-trip time, rename,
regroup, enable/disable, **Test** (blinks it green), scan, and add-by-address.
Disabling a unit parks it: it stays in the config and every scene skips it.

**System** — live log tail and a raw JSON config editor.

### Scene rotation

The sign is on all night, so it cycles scenes on its own:

```jsonc
"rotation": {
  "enabled": true,
  "playlist": [],                    // empty = every scene not excluded
  "exclude": ["All off"],            // never rotate into these
  "interval_minutes": 8,             // floor of 2, see below
  "order": "shuffle",                // shuffle | sequential
  "avoid_repeat": true,              // never the same scene twice running
  "hold_after_manual_minutes": 15    // back off after someone uses the controls
}
```

All of it is on the **Timing** tab: toggle, interval, order, hold, a tick-list of
which scenes take part, and a **Skip to next scene** button. The card shows what
is playing and how long until the next change.

Four decisions worth knowing about:

**It runs on elapsed time, not the clock.** The Zero W forgets the time across a
cold boot and there is no NTP on the playa, so anything keyed to wall-clock time
would simply never run. Rotation works from the moment the Pi powers on, knowing
nothing about what day it is.

**The interval floor is 2 minutes.** A full twelve-device sweep takes ~50s. Any
faster and the radio never rests, which makes the web UI sluggish — the AP and
BLE share one antenna. Eight minutes is the default; under about three gets busy.

**Touching the controls wins.** Any manual colour, scene or power command pauses
rotation for `hold_after_manual_minutes`, so the sign is not changing under you
while you are looking at it. The card shows how long the pause has left.

**Rotation never stacks.** If a sweep is still running when a change comes due,
it waits rather than queueing another 50s of work behind it.

The boot scene counts as the first pick, so a reboot does not run two full
sweeps back to back.

### The shipped scenes

Sixteen, mostly saturated on purpose — analog RGB strips render pale blends as
dirty white, so washed-out palettes look worse on the sign than on a phone.

| | Letters | Cup | Straw |
| --- | --- | --- | --- |
| Vice | hot pink | cyan | white |
| Neon | violet | hot pink | cyan |
| Miami | pink | teal | yellow |
| Sunset | orange | magenta | amber |
| Cyberpunk | magenta | yellow | cyan |
| Ice | ice blue | white | deep blue |
| Acid | lime | yellow | spring green |
| Ultraviolet | violet | magenta | deep blue |
| Candy | pale pink | white | hot pink |
| Fire | red | orange | amber |
| Mint | mint | white | teal |
| Gold | amber | cream | orange |

Plus **Split** (one face pink, the other cyan — only interesting because you
cannot see both at once), **Warm**, **Letters only** (drink goes dark), and
**All off**, which is excluded from rotation.

### Scenes that move

Twelve run the controllers' own patterns, so they animate continuously with no
BLE traffic at all. **These are what rotation plays by default** — a static
palette held for eight minutes is what makes a sign look switched off.

| | What it does |
| --- | --- |
| **Drift** | whole sign fading through 7 colours, slowly |
| **Slow burn** | letters fading RGB, drink steady cyan |
| **Carousel** | letters and cup fading, straw steady white |
| **Pulse** | letters magenta, cup cyan, straw white — each breathing in its own colour |
| **Heartbeat** | whole sign on a slow red pulse |
| **Neon drift** | letters magenta, drink cyan, both fading |
| **Ocean** | letters blue, cup cyan, straw white, all at different rates |
| **Ember** | letters red, cup yellow, slow |
| **Counterpoint** | letters fading slowly while the drink runs four times faster |
| **Two faces** | each side of the sign doing something different |
| **Jump cut** | whole sign stepping hard through 7 colours |
| **Rave** | flash 7 colours, fast — excluded from rotation on purpose |

Two ideas do most of the work. The **single-colour fades** (`0x8b`–`0x91`) give
movement while keeping a colour identity, so the sign can breathe without
turning into a rainbow. And running groups at **different speeds** stops
everything pulsing in lockstep, which is what makes cheap light installations
look mechanical.

The catch is that a single-colour fade only ramps *brightness*. See
**Fade speed** below: below about speed 50 it is invisible, and the five scenes
built on them shipped at 15–35, which made them look like solid colours.

`Rave` is left out of rotation deliberately: flash-7 at speed 70 is a party
trick, not something to leave running for six hours beside people trying to
sleep. Tick it back in from the Timing tab if you disagree.

**Speeds are a starting point, not a result.** Watch them after dark and adjust:
Control → pick the pattern → speed → Apply, then save over the scene.

#### Fade speed: higher is faster, and single-colour fades need most of the range

Measured on the sign, on `A_Straw`:

| Frame | What it did |
| --- | --- |
| `0x88` jump 7 colours @ 50 | hard cuts through colours, unmistakable |
| `0x91` fade white @ 25 | **looked like solid white** |
| `0x91` fade white @ 100 | visibly breathes |
| `0x9c` strobe white @ 60 | flashes clearly — about 2s off, 2s on |

A strobe at 60 running a ~4-second cycle sets the scale: **60 is about the
slowest worth using for anything**, and at 25 a cycle is long enough that a
brightness ramp simply cannot be seen. A fade needs more speed than a strobe
does, because on/off is far easier to notice than a gradual ramp.

Higher is faster, then — but the two kinds of fade are not comparable:

- **Multi-colour** fades (`fade 7 colours`, `fade RGB`) shift *hue*, so they read
  as movement even at speed 20. Drift, Slow burn, Carousel, Counterpoint and
  Two faces run at 20–40 and are fine.
- **Single-colour** fades (`fade red`, `fade white`, …) only ramp *brightness* on
  one hue. Under ~50 the change is too gradual to perceive and the unit looks
  like a solid colour. These want 85–100.

Pulse, Heartbeat, Neon drift, Ocean and Ember were all built from single-colour
fades at 15–35, so all five read as static — five of the eleven scenes in the
rotation. They are now at 100, the value confirmed visible above. To retune a
config that predates that fix, or after changing speeds by hand:

    ./scripts/retune_fades.py                        # what would change
    sudo ./scripts/retune_fades.py --apply           # write it
    curl -X POST http://localhost/api/config/reload  # pick it up, no restart

It only touches single-colour fades below `--floor` (default 60), leaves the
multi-colour ones alone, keeps a `.bak`, and preserves the file's owner. Run it
against `/etc/vice-lights/config.json` — that is the file the service reads, and
`update.sh` deliberately never overwrites it, so pulling new code does not
change your scenes.

### What the patterns actually are on this hardware

Audited on the sign, not taken from documentation. The firmware has a second
7-colour fade at `0x8a` that the documented table does not, which displaces
everything after it — so the published mode list is not merely mislabelled, it
is a different list.

| Values | Behaviour |
| --- | --- |
| `0x80`–`0x86` | static colours — the colour picker does these better |
| `0x87`, `0x88` | flash / jump through 7 colours |
| `0x89`, `0x8a` | **fade RGB, fade 7 colours** — the useful ones |
| `0x8b`–`0x91` | fade a single colour: red, green, blue, yellow, cyan, magenta, white — needs speed 85+ to be visible |
| `0x92`–`0x94` | further fade RGB variants |
| `0x95`–`0x9c` | strobes and flashes, same colour order (`0x9a` is a flash) |
| `0x9d` | solid white |

For something left running all night, `0x89` and `0x8a` are the material: they
move continuously without strobing. The `0x95`+ range is exhausting to sit
beside for hours and is best left out of rotation.

Edit any of them from the Scenes tab, or add your own — new scenes join the
rotation automatically unless you tick a specific playlist.

### Built-in patterns: motion for free

At ~4s per controller the Pi cannot animate the sign. The controllers can: a
built-in pattern runs in their own firmware, so once set it animates
continuously with **no further BLE traffic at all**. One 50s sweep buys motion
that lasts until you change it — which makes a pattern scene the cheapest thing
rotation can play.

The catch is that the documented mode names are wrong for this hardware, so find
out what the 30 values actually do:

```bash
sudo systemctl stop vice-lights
python elk_scan.py modes BE:68:F1:BB:DB:04 --config /etc/vice-lights/config.json
sudo systemctl start vice-lights
```

It sets each pattern in turn, waits `--dwell` seconds, and asks what you saw.
Type a description to record it, Enter to skip, `r` to watch it again, `q` to
stop. Useful flags: `--only 0x89,0x91` for specific values, `--new-only` to skip
what is already recorded, `--speed` and `--start`.

**Only describe what you see after `>>> NOW SHOWING`.** Writing a mode takes
around four seconds — connect, write, disconnect — and the *previous* pattern
keeps running for all of it. Answering during that window records the pattern
before the one you are being asked about, and once you settle into a rhythm the
whole run shifts by one. The tool prints the switch and the watch phase as
separate lines for that reason, and discards anything typed before the prompt
appears, so a fast answer cannot land on the wrong pattern.

Or do it from the phone while standing at the sign, which is easier in the dark:
**Control → pick a pattern → Apply pattern → Name this pattern.** Same store,
same effect.

Recorded names replace the documented ones everywhere, and the picker marks
observed patterns with ● and unverified ones with ○.

**Turn a good one into a scene:** Control tab → choose the pattern and speed →
Apply → Scenes → save. Target a group and the rest of the sign keeps its solid
colour, so "letters fading through the spectrum, cup and straw steady cyan" is
one scene. Those scenes rotate like any other, and cost nothing to leave running.

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

Measured on the sign: **a full 12-device scene takes about 70 seconds**, or
~5.9s per controller. That is connect, GATT service discovery, two writes and
disconnect, on a 1GHz ARMv6 core running BlueZ — the radio and the stack, not
the code. The UI stays responsive throughout; nothing waits on BLE.

That number is why targeting matters. A group is proportionally faster, and the
per-letter groups are the fastest useful unit:

| Target | Devices | Roughly |
| --- | --- | --- |
| `all` / a full scene | 12 | ~70s |
| `letters` | 8 | ~47s |
| `side-a` / `side-b` | 6 | ~35s |
| `drink` | 4 | ~24s |
| `cup`, `straw`, `V`, `I`, `C`, `E` | 2 | ~12s |
| one controller | 1 | ~6s |

So drive a group while you're playing with colours and save `all` for scenes you
apply and walk away from. Live apply on a two-device group feels immediate;
live apply on all 12 does not.

### The battery

The sign runs off a battery on a solar charge controller. The controllers do
not switch themselves off, and neither does anyone at four in the morning:
left lit overnight, this sign once took its battery below the voltage the
controller will begin charging at -- worse than a dark sign, because it is a
sign that cannot come back the next day without mains power, and on the playa
there is none.

**The safeguard is a hardware low-voltage disconnect.** The lights (and the
Pi) run through a module that cuts them at a set voltage, in hardware, with
nothing running and nothing to go wrong. That is the right place for this job:
the Pi is on the same battery, so any software cutoff is browning out at
exactly the moment it would need to act. Nothing in this repository tries to do
it any more.

There used to be a software runtime budget here -- N minutes of light, then
everything off -- as a second line for the ordinary case of nobody
remembering. It was removed once the hardware disconnect was wired in: two
mechanisms guarding one battery, one of them the wrong layer for the job, is
one more than earns its keep. The config, the `/api/battery` routes, the
Control-tab card and the preflight check are all gone with it.

### One dead controller used to cost a minute

A controller that is powered down, out of range or simply dead costs
`attempts` × `connect_timeout` **every sweep** — measured at roughly 60 seconds
for a single unit, about half the wall clock of a twelve-device scene. It never
blocked the others, but it taxed every one of them.

After `cooldown_after` consecutive failures (default 2) a device is skipped
outright for `failure_cooldown` seconds (default 180), then probed once with a
single attempt instead of the full retry budget. A successful probe clears the
state immediately and it rejoins the fleet. The UI shows it as `skipped` with
the reason and the time remaining, and the log names anyone sitting out:

```
skipped 1 device(s) still in cooldown: B_E
```

Set `cooldown_after` to 0 to disable the mechanism and always retry everything.

**A cooldown never swallows something a person just asked for.** Skipping is
what keeps a twelve-device sweep fast; a job with one device in it has nothing
to keep fast, so if someone typed the message or pressed the button it goes out
and comes back with an error rather than vanishing. It still gets the single
probe attempt, so a dead panel costs one connect and not the full budget. A
*timer's* single-device work — the message playlist, a page turn — stays
skipped: a dead panel asked for a message every twenty seconds would hold the
radio the twelve controllers need.

This was worth fixing because of how it read from the phone. One panel timeout
put the panel in cooldown, and every message after it came back as
`failed (0 ok / 0 failed in 0.0s)` — a failure with nothing failed in it, for
three minutes, with no reason shown anywhere. The queue card now prints the
job's reason, and tells `no answer from` (a device that did not respond) apart
from `skipped` (one the sign chose not to try), which were previously both
printed as "skipped".

**Investigate a device that keeps cooling down** rather than living with it —
check the Devices tab for its last error and signal, and see the range note in
the troubleshooting table.

### Where the time goes

Every job logs a phase breakdown, because the split decides whether anything can
be done about it:

```
phase averages over 12 device(s): connect 4.01s  discover 0.00s  write 0.09s
  disconnect 0.50s | inter-device gap 0.35s | 4.95s/device accounted,
  5.47s actual (0.52s unmeasured)
```

Measured on the sign, after both fixes below. The **actual** figure is the one
that matters: the phases do not cover the Python and D-Bus work between them, or
a failed device's retries, so quoting only the accounted number understates a
sweep. The history so far:

| | 12-device sweep |
| --- | --- |
| one unreachable unit, blocking disconnect | 131.3s |
| after skipping dead units and capping disconnect | 65.6s |
| with all 12 reachable | **49.6s** |

At 49.6s the phases account for everything: `4.13s/device accounted, 4.13s
actual`. The half-second per device that used to go unmeasured was the failing
unit's retry time.

Two useful things fall out of the breakdown.

**`discover` is 0.00s** because BlueZ resolves services as part of connecting —
bleak's `connect()` waits for `ServicesResolved`. The ATT round trips are real,
they are just billed to `connect`. Do not read a zero here as "discovery is
free"; a GATT cache experiment would show up as a shorter `connect`, not a
shorter `discover`.

**`disconnect` was 2.43s** — 40% of every controller's time, spent waiting for a
teardown nothing downstream depends on. The worker now starts the disconnect,
waits `disconnect_wait` (default 0.5s), and moves on while it finishes in the
background. Measured effect: 2.43s → 0.50s. Set `disconnect_wait` to 0 to never
wait, or raise it if a radio misbehaves.

Note that `connect` rose from 3.34s to 4.01s across the same change. That may be
the lingering disconnect competing with the next connection — or simply that the
earlier average excluded the one device that would not connect, and a flaky unit
connects slowly. The two are not separable from a fleet average; per-device
phases in `/api/status` would settle it.

**`connect` is now ~80% of the remaining time.** If you want to chase it, BlueZ's
connection interval is the next knob — in `/etc/bluetooth/main.conf`:

```ini
[LE]
MinConnectionInterval=6
MaxConnectionInterval=12
ConnectionLatency=0
```

Those are 1.25ms units, so 7.5–15ms against a default nearer 30–50ms. Service
resolution is a sequence of round trips gated by that interval, so a shorter one
can cut `connect` substantially. **Test AP responsiveness afterwards** — more
frequent BLE events mean more airtime on the one antenna the wifi AP shares, so
this trades UI latency for sweep speed. Measure both before keeping it.

A job that reports `12 ok / 0 failed` but takes twice as long as it should is
usually retries. Only the attempt that succeeded contributes phase timings, so a
device that timed out once and connected on the second try looks fast while
having cost a whole `connect_timeout`. That shows up as unmeasured time, and the
log now names the devices responsible:

```
2 device(s) needed a retry (most of the unmeasured time): A_C on attempt 2, B_V on attempt 2
```

Repeat offenders are a range or placement problem, not a software one.

Read your own numbers before believing anything about how to speed this up:

```bash
journalctl -u vice-lights --no-pager | grep "phase averages"
```

**If `connect` dominates** (it does here), try making BlueZ keep its GATT cache
for unbonded devices — add to `/etc/bluetooth/main.conf`, then `sudo systemctl
restart bluetooth`:

```ini
[GATT]
Cache = always
```

A reconnect can then skip rediscovering services it already knows, which is part
of what `connect` is paying for. Measure before and after.

### Board comparison, measured

Same twelve controllers, same code, same room:

| | Pi Zero W | Pi 4 (2GB, 64-bit) |
| --- | --- | --- |
| connect | 3.18s | **1.79s** |
| write | 0.09s | 0.07s |
| per device (accounted) | 4.13s | **2.73s** |
| 12-device sweep | 49.6s | **32.8s** |

A 34% cut, all of it in `connect`. The estimate below said 0.5–1s per device;
the real figure is 1.4s. More of `connect` was CPU and D-Bus than that reasoning
credited — the irreducible radio floor is at most 1.79s, not the 1.5–2.5s
guessed. The Pi 4 also has a newer BLE chip, so some of the gain is the radio
rather than the processor; the two cannot be separated from these numbers alone.

**Measure with only one host running.** A Zero W still powered and running the
service will fight the Pi 4 for the same twelve controllers, and the symptom is
specific: the phase totals stay low while `unmeasured` climbs, because the
timings only record the attempt that succeeded and retries land outside them. A
first Pi 4 reading showed `2.59s accounted, 7.02s actual (4.43s unmeasured)`
with the Zero W still on; killing it gave `2.71 / 2.73 / 0.02`.

**Tuning for a faster board.** Two defaults are sized for the Zero W's single
core and shared antenna, and can be tightened where there is headroom:

```jsonc
"inter_device_delay": 0.1,   // was 0.35 -- the pause that let the AP breathe
"disconnect_wait": 0.2       // was 0.5  -- how long to wait on a teardown
```

That is roughly another 6s off a sweep. Re-check AP responsiveness afterwards
(§ AP / BLE contention) — the point of those delays was leaving the radio time
to serve the web UI, and a Pi 4 has more CPU headroom but still one antenna.

### Would a Pi Zero 2 W help? Mostly no — this was asked and measured

Final per-device breakdown at 49.6s for twelve controllers:

| Phase | Time | Share | What it is |
| --- | --- | --- | --- |
| connect | 3.18s | 77% | catching an advertisement, link setup, service resolution |
| disconnect | 0.50s | 12% | capped; the real teardown finishes in the background |
| inter-device gap | 0.35s | 8% | our own deliberate pause, to let the AP breathe |
| write | 0.09s | 2% | the actual frames |

The telling number is that accounted and actual agree to **0.01s per device**.
There is essentially no Python time outside the awaited BLE calls, so the
interpreter is not the bottleneck. What is left inside `connect` is a mix of ATT
round trips gated by the connection interval — which no CPU can speed up — and
BlueZ/D-Bus marshalling, which one can. Best guess at the split puts 1.5–2.5s of
that 3.18s in the radio.

So a Zero 2 W plausibly saves **0.5–1s per device, ~6–12s off a sweep**: call it
50s → 40s. Real, but not transformative, and irrelevant at an 8-minute rotation
interval where nobody is watching the transition.

The genuine reasons to switch are not speed:

* **Four cores.** The wifi AP stops competing with the service for the one core,
  which is the AP/BLE contention this project was warned about from the start.
* **armv7**, so piwheels serves a compiled `dbus-fast` and `SKIP_CYTHON` stops
  being necessary.

Against: slightly higher idle draw, which matters on battery, and a reinstall.

**What actually cost time was never the CPU** — one flaky controller burning
~60s of retries every sweep, and a 2.4s teardown nobody needed to wait for.
Both were fixed in software, for nothing, and together took 131.3s → 49.6s.
Measure before buying.

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
| POST | `/api/stop` | Stop now: drops the queue *and* cancels the write in flight |
| POST | `/api/devices` | Create/update `{address, name, groups, enabled}` |
| DELETE | `/api/devices/<addr>` | |
| POST | `/api/devices/<addr>/test` | Blink it green |
| POST | `/api/scan` | `{seconds}` — queued like any other BLE job |
| GET | `/api/scan/result` | Last scan |
| POST/DELETE | `/api/groups`, `/api/groups/<name>` | |
| POST/DELETE | `/api/scenes`, `/api/scenes/<id>` | |
| POST/DELETE | `/api/schedules`, `/api/schedules/<id>` | |
| POST/DELETE | `/api/timers`, `/api/timers/<id>` | `{scene, minutes}` |
| GET/POST | `/api/modes` | `{value, name}` — record what a pattern really does |
| GET/POST | `/api/rotation` | `{enabled, interval_minutes, order, playlist, exclude, avoid_repeat, hold_after_manual_minutes}` |
| POST | `/api/rotation/next` | Skip to the next scene now |
| GET/POST | `/api/rotation` | `{enabled, interval_minutes, order, playlist, exclude, avoid_repeat, hold_after_manual_minutes}` |
| POST | `/api/rotation/next` | Skip to the next scene now |
| GET/POST | `/api/matrix` | Panel settings: `{enabled, address, name, family, char_uuid, playlist, width, height, default_dwell, paging, page_seconds, chunk, frame_delay, commands}` |
| POST | `/api/matrix/send` | Say it now: `{text, color, mode, speed, dwell}` |
| POST | `/api/matrix/next` | Skip to the next queued message |
| POST | `/api/matrix/clear` | Blank the panel (also stops the cycle) |
| POST | `/api/matrix/power` | `{on}` |
| POST | `/api/matrix/brightness` | `{percent}` |
| GET | `/api/pi-power` | The Pi's under-voltage / throttling log, with timestamps |
| GET | `/api/wifi` | AP name, passphrase, and a join-QR module matrix |
| GET/POST | `/api/matrix/messages` | The queue; POST creates or edits one |
| DELETE | `/api/matrix/messages/<id>` | |
| POST | `/api/matrix/messages/<id>/send` | Put that one up now |
| POST | `/api/matrix/messages/order` | `{ids: [...]}` — the list order is the play order |
| GET | `/api/matrix/preview?text=` | The bitmap the panel will get, whether it fits, and its pages if it does not |
| POST/DELETE | `/api/matrix/program` | Store the queue in the panel's own slots and cycle it there / stop |
| POST | `/api/matrix/colortest` | `{step}` — put one known colour on the panel |
| POST | `/api/matrix/colorfix` | `{seen: [...], layout}` — solve the byte order from what was seen |
| GET/POST | `/api/settings` | Server-wide settings, e.g. `{apply_on_boot}` |
| GET/POST | `/api/time` | `{iso: "2026-08-28T19:30:00"}` or `{epoch: ...}` |
| GET/PUT | `/api/config` | Whole config |
| GET | `/api/log?n=200` | Log tail |
| GET | `/api/checks` | The diagnostics that can be run from the phone |
| GET/POST | `/api/checks/<name>` | Run one, or read its output |
| GET | `/healthz` | Liveness |

```bash
curl -X POST http://192.168.50.1/api/apply -H 'Content-Type: application/json' \
     -d '{"target":"group:letters","color":"#ff2d78","brightness":80}'
curl -X POST http://192.168.50.1/api/scene/apply -H 'Content-Type: application/json' \
     -d '{"scene":"All off"}'
```

There is **no authentication** — anyone on the AP can control the sign. That's
the intent for a private network; keep the WPA passphrase to yourself.

---

## 8. AP / BLE contention

**Measured on this sign, and it is not a matter of degree.** With the access
point on 2.4GHz, *every* BLE connection failed:

| Trial | Result |
| --- | --- |
| as found (AP on channel 6) | FAILED, 42.6s |
| after clearing BlueZ's cached entry | FAILED, 42.6s |
| after restarting bluetooth | FAILED, 42.8s |
| **with the access point stopped** | **ok, 4.7s** |
| access point stopped + bluetooth restarted | ok, 4.8s |

`bluetoothctl` showed why: the link established and was then torn down by this
end.

```
hci0 AC:36:4B:32:89:C5 type LE Public connected eir_len 21
[CHG] Device AC:36:4B:32:89:C5 Connected: yes
Failed to connect: org.bluez.Error.Failed le-connection-abort-by-local
```

The Pi has **one antenna** shared between wifi and Bluetooth. Advertising uses
three channels at 2402, 2426 and 2480 MHz -- the edges of the band -- so
scanning kept working perfectly and showed the device at -56 dBm. A *connection*
hops across 37 data channels through the middle of the band, which is exactly
where a 2.4GHz AP sits. That is why "the scan sees it fine" and "every connect
times out" are not a contradiction; they are the fingerprint of this fault.

### The fix

Put the AP on 5GHz. The antenna diplexer passes 2.4 and 5 at the same time, so
they stop competing:

```bash
sudo ./scripts/setup_ap_hostapd.sh          # defaults to 5GHz, channel 36
```

Then **prove it on the hardware** rather than trusting that the AP came up:

```bash
sudo ./scripts/ble_connect_test.sh AC:36:4B:32:89:C5
```

Every trial should pass, including the ones with the AP running.

The cost is range -- 5GHz carries less far and through less. For a sign you walk
up to that is a fair trade. If it is not, the alternative is a **USB Bluetooth
dongle** with its own antenna, which lets the AP stay on 2.4GHz for reach.

`BAND=2.4 sudo ./scripts/setup_ap_hostapd.sh` goes back, knowingly.

### Diagnosing it again

`scripts/ble_connect_test.sh` runs the same connection under five conditions and
maps each outcome to a different cause: the device refusing connections, a stale
BlueZ address-type cache, a wedged stack, or this. It restores every service it
touches, and refuses to stop hostapd if you are connected over the sign's own
wifi.

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
| Can't reach 192.168.50.1 | `nmcli con show --active` (or `systemctl status hostapd`), `ip addr show wlan0` |
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

---

## 9b. A real clock, at last

Everything time-shaped in this project was built around the Pi having no RTC:
the timekeeper persists a stamp to disk every minute and pulls the clock
forward on boot, wall-clock schedules pause whenever the clock is suspect,
and scene rotation counts monotonic minutes because "off at 3am" cannot be
trusted. All of that still works -- but with a battery-backed RTC module on
the I2C pins, it stops being load-bearing.

Wire the module to SDA (pin 3), SCL (pin 5), 3V3 and GND, then:

```bash
sudo ./scripts/setup_rtc.sh            # DS3231, the usual module
sudo ./scripts/setup_rtc.sh pcf8523    # Adafruit PiRTC and some HATs
```

The script is idempotent and does the whole job: enables I2C, adds the
overlay to config.txt *and* live-loads it so no reboot is needed, removes
fake-hwclock, unblocks the udev hwclock script, and syncs whichever clock
knows the time into the one that does not. If the chip name is wrong it says
so from the bus scan rather than leaving a silent dead overlay.

**The RTC is read and written through the kernel directly, not `hwclock`.**
`timekeeper.py` opens `/dev/rtc0` and issues the `RTC_RD_TIME` /
`RTC_SET_TIME` ioctls itself. That is not purity -- Debian Trixie moved
`hwclock` out of `util-linux` into `util-linux-extra`, which is not installed
on this sign, and the discovery that it was missing came from a Pi that is
bound for a week in the desert with no uplink to `apt install` it from. The
ioctl interface is in the kernel, so it is there whether or not any package
is. The RTC holds UTC, the same convention `hwclock` uses.

Two integrations make it stick:

* **The service pulls the RTC forward at start.** `restore()` reads the
  hardware clock before it looks at the on-disk stamp, and credits the source
  as "hardware clock". A Pi with no RTC takes exactly the old path -- every
  RTC call is a no-op that returns `None` when the device is absent. If the
  RTC ever holds garbage (anything before 2024), it is ignored and the
  forward-only stamp restore still corrects it.
* **Setting the time from the phone now writes the RTC too.** Nothing
  auto-syncs system time into an RTC on a box with no NTP, so without this
  the module would keep serving whatever time it shipped with, and a set time
  would die with the next power cut.

`/api/time` reports `rtc: true` when the device is present, the Timing tab's
clock source reads "hardware clock" after a boot that used it, and
`preflight.sh` warns when no RTC is fitted.

---

## 9c. Temperature

`vicelights/thermometer.py` reads a DHT11 (or DHT22/AM2302 -- same three
wires, better resolution) on demand, for putting the temperature on the panel.
One blocking call, no thread, no history.

```python
from vicelights.thermometer import Thermometer

probe = Thermometer(pin=13)          # BCM 13 = physical pin 33
reading = probe.read()               # blocks up to ~10s
if reading:
    text = "%.0fF" % reading.fahrenheit
```

**Wire VCC to 3.3V, not 5V.** The data line idles at whatever VCC is through
its pull-up, so a 5V-powered sensor puts 5V on a 3.3V-only GPIO. That is the
ordinary way people kill a Pi header.

```bash
sudo /opt/vice-sign-lights/venv/bin/pip install adafruit-circuitpython-dht
```

Not in `requirements.txt`: it pulls in Blinka, which is Pi-only and heavy, for
a sensor the sign works fine without. The driver is imported inside the read,
so everything here installs and tests on a machine with no GPIO.

### Why one read is five reads

The DHT protocol is bit-banged microsecond timing on a single wire. On a
non-realtime kernel any scheduler hiccup mid-frame corrupts the checksum, and
**losing a fifth of reads is an ordinary day.** That is a footnote when you
sample every five seconds. It is the whole problem at two samples an hour: one
unlucky read is a blank panel for thirty minutes.

So a single `read()` tries up to five times, ~2s apart -- the DHT11's own
sampling period is 1-2s, so retrying faster just fails again -- and stops early
at three good reads. It returns the **median**, not the first: DHT11 resolution
is whole degrees Celsius, so consecutive reads dither between two integers, and
a median of three flattens that as well as outvoting a bad frame that passed
its checksum by luck. `reading.samples` says how many survived.

### Stale readings

A failed read returns `None`. It never quietly hands back the last value as if
it were current -- a stale number that looks live is worse than dashes. If the
display wants to keep showing the previous one, `probe.last` is there with
`.age()` and `.stale()` so it can be marked as old and blanked once it stops
being worth showing.

Nothing here raises. A missing driver, an unwired pin or a dead sensor logs
once -- not twice an hour until the burn ends -- and returns `None`. Nothing
driving an LED display should die on a thermometer.

### If reads fail on hardware

"Cannot determine SOC peripheral base address" means Blinka fell back to
RPi.GPIO, which pokes `/dev/mem` and does not work on current kernels. The
warning says so, with the fix:

```bash
pip uninstall -y RPi.GPIO && pip install rpi-lgpio
```

Same import name, same API. (The device is built with `use_pulseio=False`,
which sidesteps the other trap: the pulseio path wants `/dev/pulseio`, absent
on stock Bookworm/Trixie, and its absence explodes at construction rather than
failing a read. That flag also means no libgpiod package is needed.)

### Tests

```bash
python3 -m unittest discover -s tests -v
```

35 tests, no hardware, no sleeping: `Thermometer` takes an injectable reader
and an injectable sleep, so the retry paths that take ten seconds on a Pi take
milliseconds here.

## 10. The text panel

A BLE LED matrix that shows text. It is a **different device class** from
the twelve controllers -- a length-prefixed, chunked protocol carrying a bitmap
rather than a 9-byte frame -- so it lives in `vicelights/matrix.py` and has its
own config block. What it shares is the radio: panel writes queue through the
same serialized `BleWorker` as everything else, because a panel write racing a
scene sweep would cost both.

### Two queues, and why

The BLE worker's queue drains as fast as the adapter allows. The **message**
queue drains at reading speed: each message carries its own dwell, and the
runner in `messages.py` holds it on the panel for that long before handing the
next one over. Timed on `time.monotonic`, like scene rotation, because the Pi
has no RTC. It backs off while a twelve-device sweep is in flight rather than
stacking writes behind it.

### This sign's panel

Advertises as `LED_BLE_4B3289C5` and is driven by the phone app **iPixel
Color**. Write to `0000fa02-0000-1000-8000-00805f9b34fb`, replies arrive on
`0000fa03`.

Every packet is:

```
[len lo][len hi][cmd lo][cmd hi][data ...]
```

where the length counts the whole packet including itself, and the command is
**two bytes**. The panel answers with a packet of the same shape --
`05 00 <cmd lo> <cmd hi> <status>` -- echoing the command it is replying to.
Status `01` means it took the packet, `02` that it did not, which makes the
panel its own test harness.

| Command | Bytes |
| --- | --- |
| Power on / off | `05 00 07 01 01` / `05 00 07 01 00` |
| Brightness (1-100) | `05 00 04 80 <level>` |
| DIY mode on / off | `05 00 04 01 01` / `05 00 04 01 00` |
| Set one pixel | `0A 00 05 01 FF <g> <r> <b> <x> <y>` — see below |
| Select screen buffer | `05 00 07 80 <1-9>` |
| PNG image | type `02 00`, extended header, CRC32 |
| GIF | type `03 00`, same shape |

**Writes must be acknowledged.** `0000fa02` advertises both write and
write-without-response, and without-response is faster — but it has no flow
control, so outrunning the panel drops packets with no error at either end.
That showed as a few LEDs missing from every message, different ones each time,
which looks like a rendering bug and is not one. `write_response` defaults to
true; `frame_delay` can then be 0, because the acknowledgement paces the radio.

**The set-pixel byte order is not what the protocol writeups say**, and it is
not reliably guessable either. They give `[R][G][B][A]`. Sending that, this
panel showed blue, cyan and magenta for red, green and blue, which solves to
`[A][G][R][B]` — and `agrb` was wrong too: with it in place the panel showed
green for red. Two readings of the same panel, one of them wrong, and no way
to tell which from a description after the fact.

So the layout is not derived from a remembered photo any more. **Message tab →
Panel → Colour check** puts one block on the panel, asks what colour it is,
three times, and solves for the wiring:

```
POST /api/matrix/colortest  {"step": 0}   -> {"sent": "red", "layout": "agrb"}
POST /api/matrix/colorfix   {"seen": ["green", "red", "blue"], "layout": "agrb"}
                                          -> {"saved": "argb"}
```

One block at a time on purpose. Three bands at once and the answer depends on
which end the reader started from — which is how this was got wrong the first
time. The check costs 81 packets, about three seconds.

`solve_layout` searches all 24 permutations of `r`,`g`,`b`,`a` and returns
every one that explains all three answers. The alpha byte is part of the
search, not an aside: send with the wrong layout and a hard-coded 255 alpha
lands in a colour channel, which is exactly why blue lit for *every* colour on
the first attempt. That also means no reordering of the first three bytes can
express the fix, which is why this is a layout and not a channel order.

Answers that fit nothing are refused rather than saved, and the two-answer
ambiguity is real: "red looks green" alone leaves four candidates (`argb`,
`abgr`, `grab`, `gbar`). The green block is what separates them.

Text has two routes, and the config picks between them with `text_mode`:

* **`pixels`** (default) -- DIY mode on, then one small packet per lit pixel.
  Every byte of it is documented, so it is the one that is certain. "VICE" is
  55 pixels, about a second.
* **`png`** -- the whole image in one transfer with a CRC32. Far fewer packets,
  but the extended header carries an `opt` byte and a `buffer` byte that no
  public write-up specifies. `./matrix_probe.py png-sweep <address>` tries the
  plausible values and keeps whichever the panel answers `01` to.

The panel also exposes a second service, `0000ae00` with a writable `ae01`.
Do not use it: it answers a plaintext write with a type byte and sixteen
high-entropy bytes -- one AES block, different every time. Whatever that
channel is, it is encrypted, and nothing here needs it.

Protocol details from the community's reverse engineering of iPixel Color:
[cagcoach/ha-ipixel-color](https://github.com/cagcoach/ha-ipixel-color),
[lucagoc/iPixel-CLI](https://github.com/lucagoc/iPixel-CLI),
[DonKracho/ESPHome-component-iPixel-ble](https://github.com/DonKracho/ESPHome-component-iPixel-ble).
Corroborated against this panel: the power and brightness bytes above are what
it acknowledged with status `01`.

### What the panel is showing, and what it might be showing

Drawing costs one packet per pixel, so a message is drawn by erasing exactly
the pixels the last one lit and this one does not — 182 packets instead of
1537 for a full repaint.

That needs an honest answer to "what is on the glass". A message is recorded as
showing the moment it is *queued*, so its dwell counts reading time rather than
radio time — right for timing, wrong for erasing: a write that timed out put up
nothing, or half of something, and erasing against it leaves the real message
lit underneath the next one. So the runner watches the job it queued: what
completed is what it erases against, and what failed joins a short list of
things that *might* still be lit and gets erased against as well. The list
clears on the next successful write and is capped at three, so a bad patch of
radio cannot grow the erase into a full-panel repaint.

### The panel can animate a message itself

There are two ways to get text onto this panel and they are not variations of
each other.

**Drawing it (`text_mode: "pixels"`, the default and the verified one.)** We
work out every lit LED and send one packet each. Certain, because every byte
is documented -- and static, because making it move means sending the whole
thing again for every frame.

**Handing it over (`text_mode: "native"`.)** One packet carrying the message,
an animation number and a speed. The panel scrolls, flashes or fades it on its
own, with no further radio at all, and can store it in one of a hundred slots
and cycle those by itself.

The difference for "WELCOME TO CAMP VICE, ICED COFFEE ALL DAY":

| | packets | bytes | after that |
| --- | --- | --- | --- |
| `pixels` | 524, in 4 software pages | 5235 | nothing moves |
| `native` | **1** (split across 4 writes) | 1037 | the panel scrolls it, forever, on its own |

The animations are the panel's, numbered 0-6: pages, scroll left, scroll
right, scroll up, scroll down, flash, brightness fade. Which is to say the
paging this code implements by hand is animation 0, and the effects the
compose screen used to offer and could not deliver are 1, 5 and 6.

```
[0:2]   packet length, little endian, counting itself
[2:4]   00 01
[4]     00, or 02 if another chunk follows
[5:9]   payload length
[9:13]  crc32 of the payload, little endian
[13]    unknown, 00
[14]    save slot -- 0 for "just show it", 1-100 to store it
[15:17] character count          <- payload starts here
[17]    horizontal alignment
[18]    vertical alignment
[19]    animation 0-6
[20]    speed 0-100
[21]    text colour mode
[22:25] text colour
[25]    background colour mode
[26:29] background colour
        then one block per character: [flag][r][g][b][bitmap]
```

**The packet is bigger than a write.** Twenty characters at 16x16 is 749
bytes against an MTU of 247, so it goes out in `chunk`-sized pieces like the
PNG transfer above: the panel reads a stream and takes the length off the
front, so the pieces reassemble there. Missing this cost an evening and looked
like an encoding fault -- every short test word appeared (`F` is 65 bytes,
`FL` is 101, the corner test 101) and every real message did not, because
short ones fitted a single write and were the only thing being tested.
`pack_frames` now splits an oversized frame as a backstop rather than handing
the adapter a write it cannot make.

**A cell is 8 wide and 16 tall -- sixteen bytes, one per row -- for both
flags.** The reference implementation describes flag 1 as "width doubled" and
that reads as a 32-byte 16x16 cell; it is not. Sending 32-byte cells put the
first character up correctly and turned everything after it into rubbish,
which is the signature of a block-size mismatch: the panel took sixteen bytes,
started reading the next character's flag halfway through the first one's
bitmap, and never recovered. The doubling is the panel's own, and the flag is
how it is asked for. Flag 0 is what this sign runs; flag 1 sends the same
sixteen bytes and is unconfirmed.

That also collapses the bit-order question. A row is one byte, so swapping the
bytes of a row does nothing, and the eight orders give four distinct pictures:
`lsb` and `lsb-swap` are the same bytes at this width. The eight exist because
a 16-wide cell has three independent axes -- which end of a byte is the left of
the picture, which byte comes first, whether rows run top-down -- and mirroring
a 16-wide row means reversing the bits *and* swapping the bytes. The first
version offered only bit order and row order, so every one of its four choices
came out backwards on the sign: none of them could produce a mirror. Six characters fit
across 96 pixels at 16x16 -- which stops mattering once the panel is scrolling
rather than holding still.

**This is not verified on hardware.** The layout is from the community's work
on the iPixel Color app; the panel answers `01` or `02`, so whether it accepts
the packet is knowable, but the bit order the app stores its own font in is
not documented, so whether the glyphs come out the right way up is a question
for the panel:

```bash
sudo ./matrix_probe.py text AA:BB:CC:DD:EE:FF --sweep
```

That sends `FL` in each of the eight orders, pausing between. But letters are
the wrong question, and two rounds of "it still looks backwards" proved it:
"backwards" covers mirrored, upside down and back-to-front, and a letter looks
the same under two of those. Ask something with one answer instead:

```bash
sudo ./matrix_probe.py text AA:BB:CC:DD:EE:FF --corner
```

That puts up a corner bracket and a solid square, sent as *bracket in the
top-left, square to its right*. Which corner the bracket comes out in names
the transform, because a corner is not a judgement call; which side of the
square it lands on says whether the panel lays characters left to right or
right to left. One look, two answers:

```bash
./matrix_probe.py text AA:BB:CC:DD:EE:FF --saw top-right,left
```

which prints the exact setting. And if the panel shows neither a bracket nor a
square -- stripes, scattered pixels -- that is not an orientation at all: the
bitmap is not laid out in rows and none of the eight orders will help.

Whichever the test names:

```bash
curl -X POST http://localhost/api/matrix -H 'content-type: application/json' \
  -d '{"text_mode":"native","bitmap_order":"lsb-swap"}'
```

The compose screen then offers the panel's animations, because the menu is
built from what the driver says it can deliver. If the panel answers `02` the
packet layout is wrong rather than the bit order, and the pixel path is
untouched and still works.

### The panel can run the playlist too

`POST /api/matrix/program` stores every enabled message in one of the panel's
own slots (1-100) and sets it cycling there; `DELETE` stops it. That is the
arrangement that costs the least radio of anything here: one connection now,
and then nothing at all -- the sign keeps cycling with the Pi switched off,
and the twelve controllers have the antenna to themselves.

```
09 00 08 80 03 00 01 02 03      cycle slots 1, 2 and 3
08 00 02 01 02 00 01 02         drop slots 1 and 2
```

A stored message is an ordinary text packet with a slot number in byte 14
instead of zero. Turning it on switches the Pi's own playlist off, because two
things cycling one panel would fight and the one not using the radio should
win. Refused on the pixel path, which has no slots to store anything in.

Also from the same reading, and also unconfirmed on hardware: `color_mode` on
a message (0 solid, 2-4 the panel's own gradients -- yellow-to-red,
light-blue-to-white, blue-to-yellow, top to bottom), and `h_align`/`v_align`
in the panel config. The background is now painted only when one was chosen,
since `back_color_mode` 1 with black is not the same as 0 on every panel and an
unwanted background is the one thing here that can hide a message completely.

One discrepancy worth recording rather than acting on: this documentation
gives `04 01` as **power on/off**, where this code has it as "DIY mode" and
uses `07 01` for power. Both are acknowledged by the panel and the pixel path
works as it stands, so nothing has been changed on the strength of a document
that has already been wrong once about this panel (see the cell size above).

Protocol details from
[DonKracho/ESPHome-component-iPixel-ble](https://github.com/DonKracho/ESPHome-component-iPixel-ble)
(`docs/IPIXEL_COMMANDS_DEMYSTIFIED.md`) and
[yyewolf/go-ipxl](https://github.com/yyewolf/go-ipxl).

### The text holds still, and the menu says so

This applies to the `pixels` path above -- the one that is verified, and the
one running until the native path is confirmed at the sign.

A message carries a `mode` -- `scroll`, `static`, `marquee`, `flash`, `fade` --
and the compose screen used to offer all five. Nothing in the pixel path read
it. Five choices, one behaviour, and no way to tell from the screen which one
you were getting: the menu was decoration.

Movement *we* drive is not affordable. Measured on this sign, a connect costs
1.5s and a disconnect 0.2s, and every write is its own connection. Even the
cheapest animation we could drive -- a flash using the panel's one-packet
power command -- is about 1.8s of radio per step, so a two-second flash holds
the antenna nine tenths of the time. That antenna is shared with the twelve
controllers. Scrolling is worse again, at ~29 writes a frame and 1.7 frames a
second, forever.

None of which applies to movement the *panel* drives, which costs one packet
and then nothing -- and is why the section above exists. The mistake worth not
repeating: "we cannot afford to animate this" was measured carefully and
answered the wrong question, because it never asked whether the animating had
to be ours.

So a driver now declares the modes it can actually deliver (`modes`, in its
capabilities), the compose screen offers those and hides the control when there
is only one, and the queue lists what this panel will really do rather than
what a message was saved with. Saved messages keep their own `mode` -- panels
get swapped, and it may mean something to the next one -- but the draw and the
log line use the mode that happened:

```
queued panel: 'HI' static: 135 frame(s)
```

### Long messages: pages, not scrolling

A message wider than 96 pixels is shown a page at a time, not scrolled. That
is a measurement, not a preference:

* Shifting a message one column moves nearly every lit pixel, so a scroll frame
  costs about as many packets as the message has pixels. Batched at MTU 247
  that is ~29 acknowledged writes a frame, ~20ms each: **1.7 frames a second**.
* Scrolling never stops. The panel and the twelve controllers share one
  antenna, so a permanently scrolling panel means the lights cannot be driven
  at all -- the same contention that made the panel unreachable while the
  access point was on 2.4 GHz.

Paging costs one draw per page and then nothing, and it uses the same
erase-diff as any other message change, so a page turn writes only the pixels
that differ between the two pages.

Pages are cut at word boundaries. **A message that fits is never paged**,
however small it had to be set to fit: shrinking "ICED COFFEE" onto one screen
reads better than flipping it across two. When it will not fit at any size the
scale is chosen for the message as a whole -- fewest pages first, ties to the
larger text -- and every page is drawn at that one size, so the panel does not
jump between heights mid-sentence. A word too wide even at 1x is broken
mid-word rather than run off the edge.

The composer says so before the message goes up (`4 pages, one at a time`), and
both UIs show `page 2/4` while it cycles. Pages do not turn while the radio is
busy with a scene sweep, and the timer restarts when it frees up -- a page
nobody saw is not a page. `paging: false` sends the whole message in one draw
instead.

### Stopping something mid-flight

A twelve-device sweep to controllers that are out of range takes about seven
minutes to time out, and until this there was no way to interrupt it: clearing
the queue dropped what had not started but left the current job running.

**Stop everything** on the *Control* tab (`POST /api/stop`) drops the queue and
cancels the BLE write in flight, so a sweep ends in the time it takes to abort
one connection rather than in minutes. Use it when a scene is in the way of
troubleshooting -- and note that a scene that keeps coming back is rotation:
turn that off on the *Timing* tab, or the next tick will start it again.

### Pairing it

These panels are sold under a dozen brands with no common protocol, so the
driver is chosen from evidence, not assumed:

```bash
sudo ./matrix_probe.py scan               # what is advertising; flags candidates
sudo ./matrix_probe.py info AA:BB:CC:DD:EE:FF   # GATT tree + fingerprint
```

**A panel setting the server does not recognise is refused, not replaced.**
`_matrix()` normalises anything unusable back to a default, which is right for
a config file read off disk and wrong for a value someone just worked out from
the panel: an early `bitmap_order` that the running code predated was silently
swapped for the default, the API answered 200, and the setting was gone
without a word -- so the same wrong picture came back and the evidence pointed
at the wrong thing. `POST /api/matrix` now checks the enumerated fields and
says what the choices are:

```
{"ok": false, "error": "bitmap_order must be one of: msb, lsb-swap, ... (not 'lsb-twist')"}
```

`info` prints the family it matched and the exact `curl` to pair it. Then test
before trusting it:

```bash
sudo ./matrix_probe.py send AA:BB:CC:DD:EE:FF --text VICE
```

`confirm` walks a panel from "does it hear us" up to "does it show text", in
order of how much each step assumes, and prints whatever the panel says back.
`trace` sends a payload one chunk at a time so a failure lands on a chunk
instead of on "the driver".

A driver is marked **confirmed** only once its encoding has been checked
against real hardware; until then the UI says `encoding unconfirmed` rather
than pretending. Everything above the driver -- queue, API, both UIs -- is
protocol independent, so confirming a panel is a change to one class.

### When no driver fits

Lift the protocol off the panel's own phone app:

1. Android → Developer options → **Enable Bluetooth HCI snoop log**
2. Send one message from the vendor app
3. Pull the capture (`adb bugreport`, or the phone's own bug-report export)
4. Decode it:

```bash
./matrix_probe.py btsnoop capture.log --emit-config
```

That reassembles fragmented ACL, maps ATT handles to characteristic UUIDs using
the capture's own discovery traffic, and prints a config block the `raw` driver
runs with **no code change**. Capture a second, different message and:

```bash
./matrix_probe.py diff first.log second.log --emit-config
```

separates the framing (identical in both) from the payload (different), which
is what turns "replay this one message" into "send any message". Both work on a
laptop with no radio and no bleak.

### Using it

* **Phone** — the *Message* tab: compose with a live pixel preview, reorder the
  queue, start the cycle, pair the panel off the same scan the controllers use.
  The preview is rendered by the Pi, not the browser: the panel draws our own
  5x7 bitmap font, so a preview in a browser typeface would be a picture of a
  different thing.
* **Touchscreen** — the *LED Text Display* tab: what is up now, the queue as one-tap chips,
  and `WRITE` for an on-screen keyboard so a message can be typed at the sign
  with no phone. Upper case only; sign messages are shouty anyway and dropping
  the shift key buys a row of width on an 800-pixel screen.

### Config

```json
"matrix": {
  "enabled": true,
  "address": "AA:BB:CC:DD:EE:FF",
  "family": "auto",          // auto | idotmatrix | raw
  "char_uuid": "",           // forced; never guessed at, unlike the controllers
  "width": 96, "height": 16, // THIS PANEL'S REAL PIXEL COUNT, not a preference
  "playlist": false,         // cycle the saved messages
  "default_dwell": 20.0,
  "paging": true,            // show a too-wide message a page at a time
  "page_seconds": 5.0,       // how long each page stays up (floor: the 5s tick)
  "chunk": 20,               // payload bytes per write at the default 23-byte MTU
  "commands": {}             // hex, for family "raw"
},
"messages": [
  {"text": "ICED COFFEE HERE", "color": "#22d3ee", "mode": "static", "dwell": 30}
]
```

A `dwell` of `0` means *hold until something replaces it*. In a cycling
playlist that would stall forever, so the playlist substitutes `default_dwell`;
a message sent by hand keeps the literal meaning.

---

## 11. The event schedule

The panel can drive itself from the week's calendar instead of a hand-typed
queue. Turn it on -- **LED Text Display &rarr; SCHEDULE** on the touchscreen,
or **Event schedule &rarr; Start** on the phone -- and it rotates:

* **VICE**
* **today's offerings** -- "TODAY 830A BLOODY MARYS / 1P BEARD SPA / 2P COFFEE
  / 2P TAROT / NAIL SPA 24/7"
* **tomorrow's** -- same shape, next day
* **the temperature** -- "NOW 75F / 22C", when a sensor is fitted (§9c)

and while an event is actually happening it adds its shouts -- the coffee's
two by name ("NOW SERVING VIETNAMESE ICED COFFEE!", "GET YOUR GAY ICED COFFEE
HERE!") and a "NOW: ..." line naming everything on at once. A quiet hour is
calm; a live event is loud. It updates on its own as the week goes on, off the
clock -- **so the clock has to be set** (§9b). The RTC handles that now.

It lives in `vicelights/schedule.py`. Nothing there talks to the panel: it
only decides the *text*, and hands back the same message dicts the runner
already cycles, so a schedule message and a hand-typed one travel the identical
road -- paging, colour, rotation, all unchanged. Each rotation slot has a
**stable id** (`sched-today` is always `sched-today`), so the list can be
rebuilt every few seconds to stay current with the clock and the thermometer
while the rotation still advances cleanly.

**The calendar is data, in `EVENTS`.** It is this camp's Burning Man 2026 week,
transcribed from the schedule sheet and keyed by date (Aug 30 – Sep 6). Outside
those dates there is no "today", so the panel shows only VICE and the
temperature -- correct, not broken. To fix a time, add an event or change how
long one runs, edit that table; the end times in it are assumptions (the sheet
gave only start times) and only decide when an event stops shouting "NOW".

**No bar anywhere.** The camp has no bar, so nothing here mentions one -- a test
walks every hour of every day and asserts it, and the old "BAR IS OPEN"
placeholders elsewhere are gone.

**What it never does** is show a stale number as if it were live. A missing or
old temperature reading drops the temperature line rather than showing the last
one; an unset clock drops today and tomorrow rather than guessing. `°` is
avoided on purpose -- the panel font is ASCII only, so a degree sign would come
out a hollow box.

`GET /api/temperature` reports the sensor config and the current reading (null
when there is none or it has gone stale). `POST /api/matrix {"schedule": true}`
turns the mode on. `tests/test_schedule.py` covers the calendar, the event
windows, the temperature line and the stable ids -- 25 checks, no clock and no
hardware.

---

## 12. Joining the Wi-Fi, and the QR encoder

The sign runs its own access point (§4), and until now joining it meant being
told the name and typing the passphrase. The **System page now shows a QR** --
point a phone camera at it and the phone joins. It is on both screens: the
phone's System tab (handy for showing a friend) and, more to the point, the
**touchscreen's System tab**, so someone standing at the sign with no
credentials can scan the sign itself. Tapping it there opens a full-screen
version -- a bigger code reads more reliably across a dark camp, and the
passphrase is spelled out underneath for a camera that will not focus.

The name and passphrase are read straight from `/etc/hostapd/hostapd.conf` --
the file the AP is actually serving, so the code can never drift from the real
network the way a second copy in our own config would. `GET /api/wifi` returns
the SSID, the passphrase, and the QR as a **module matrix** (rows of 0/1, quiet
zone included); the touchscreen draws it as pygame rectangles and the phone as
one SVG, both from that same matrix, so neither re-implements the encoder.

### The encoder

`vicelights/qr.py` is a from-scratch QR encoder -- byte mode, versions 1-10,
all four error-correction levels, automatic version and mask selection. It is
pure Python with no dependency, because the playa has no pip and a Wi-Fi join
code that needs a wheel installed is a join code that does not work. That is
the same reason the box has its own 5x7 font and its own RTC ioctls.

Writing a QR encoder is a good way to ship something that looks right and does
not scan, so it is **verified against `segno`** (a spec-compliant library) and
**decoded with OpenCV** in the tests. The proof that it is not merely
"approximately a QR code": across hundreds of real Wi-Fi payloads rendered and
fed back through a decoder, ours and segno's fail at the *identical* rate
(only where OpenCV's own detector gives up -- a real phone camera does better).
The core tests -- the Reed-Solomon against the published vector, the payload
grammar, the fixed matrix structure -- need neither library and run on the Pi;
the two cross-checks skip cleanly where the libraries are absent, which on the
sign they are.

Two bugs worth remembering, both caught by the decoder and neither visible to
the eye: pad codewords must **alternate** `0xEC`/`0x11`, not repeat; and an
alignment pattern centred on the timing row (versions 7+) must be drawn even
though the timing modules are already there -- an "already set, skip" guard
silently dropped them and broke every version from 7 up.
