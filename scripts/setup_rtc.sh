#!/usr/bin/env bash
# Put a battery-backed clock under a sign that has never had one.
#
#   sudo ./scripts/setup_rtc.sh            # DS3231, the usual module
#   sudo ./scripts/setup_rtc.sh pcf8523    # Adafruit PiRTC and some HATs
#   sudo ./scripts/setup_rtc.sh ds1307     # older 5V-era boards
#
# Why this matters here: the Pi has no clock of its own and the playa AP has
# no uplink, so until now the time survived only as a stamp written to disk
# every minute -- right after a clean boot, wrong by the whole downtime after
# a power cut, and wall-clock schedules pause whenever it is suspect. A real
# RTC ends that: the kernel reads it at boot and the service writes it back
# whenever someone sets the time from the phone.
#
# Everything here is idempotent: run it again and it changes nothing that is
# already right.
set -uo pipefail

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
CHIP="${1:-ds3231}"
case "$CHIP" in
  ds3231|ds1307|pcf8523|pcf8563|mcp7940x|m41t62) ;;
  *) echo "unknown RTC chip '$CHIP' -- one of: ds3231 ds1307 pcf8523 pcf8563" >&2
     exit 2 ;;
esac

CONFIG=/boot/firmware/config.txt
[[ -f "$CONFIG" ]] || CONFIG=/boot/config.txt
[[ -f "$CONFIG" ]] || { echo "no config.txt at /boot/firmware or /boot" >&2; exit 1; }

PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; [[ -n "${2:-}" ]] && echo "        -> $2"; FAIL=$((FAIL+1)); }
note() { echo "  ....  $1"; }

echo "== I2C bus"
if grep -Eq '^\s*dtparam=i2c_arm=on' "$CONFIG"; then
  ok "i2c enabled in $CONFIG"
else
  echo "dtparam=i2c_arm=on" >> "$CONFIG"
  ok "i2c enabled in $CONFIG (added)"
fi
modprobe i2c-dev 2>/dev/null || true
# Live-load so this works without a reboot; harmless if already up.
command -v dtparam >/dev/null && dtparam i2c_arm=on 2>/dev/null || true

if ! command -v i2cdetect >/dev/null; then
  if apt-get install -y i2c-tools >/dev/null 2>&1; then
    ok "installed i2c-tools"
  else
    note "no i2c-tools and no network to install it; skipping the bus scan"
  fi
fi

echo "== RTC overlay ($CHIP)"
WANT="dtoverlay=i2c-rtc,$CHIP"
if grep -Eq "^\s*$WANT(\s|,|$)" "$CONFIG"; then
  ok "overlay already in $CONFIG"
elif grep -Eq '^\s*dtoverlay=i2c-rtc' "$CONFIG"; then
  # A different chip is configured. Replace rather than stack -- two RTC
  # overlays fight over the same address and neither wins.
  sed -i -E "s|^\s*dtoverlay=i2c-rtc.*|$WANT|" "$CONFIG"
  ok "overlay changed to $CHIP in $CONFIG"
else
  echo "$WANT" >> "$CONFIG"
  ok "overlay added to $CONFIG"
fi
# Live-load too. If the device is wired and the chip name is right, /dev/rtc0
# appears in a moment and no reboot is needed.
command -v dtoverlay >/dev/null && dtoverlay i2c-rtc "$CHIP" 2>/dev/null || true

for _ in 1 2 3 4 5 6; do [[ -e /dev/rtc0 ]] && break; sleep 0.5; done
if [[ -e /dev/rtc0 ]]; then
  ok "/dev/rtc0 is up"
else
  DETECT="$(command -v i2cdetect >/dev/null && i2cdetect -y 1 2>/dev/null | grep -Eo '\b(68|UU)\b' | head -1 || true)"
  case "$DETECT" in
    UU) bad "/dev/rtc0 missing but the chip is claimed" "reboot, then re-run this to finish" ;;
    68) bad "a chip answers at 0x68 but the $CHIP driver did not bind" \
            "wrong chip name -- try: sudo $0 pcf8523   (or ds1307)" ;;
    *)  bad "nothing answers on the I2C bus" \
            "check the wiring (SDA=pin3, SCL=pin5, 3V3, GND), then re-run" ;;
  esac
fi

echo "== fake-hwclock (the software stand-in)"
if dpkg -s fake-hwclock >/dev/null 2>&1; then
  if apt-get remove -y fake-hwclock >/dev/null 2>&1; then
    ok "fake-hwclock removed -- the real one owns the job now"
  else
    bad "could not remove fake-hwclock" "apt-get remove -y fake-hwclock"
  fi
else
  ok "fake-hwclock not installed"
fi
# The classic udev script refuses to run under systemd; with a real RTC we
# want it to. Editing it is the standard fix, and the service also does its
# own hwclock -s at start, so this is belt as well as braces.
HWSET=/lib/udev/hwclock-set
if [[ -f "$HWSET" ]] && grep -q '^if \[ -e /run/systemd/system \]' "$HWSET"; then
  sed -i 's|^if \[ -e /run/systemd/system \] ; then|if false ; then|' "$HWSET"
  ok "udev hwclock-set unblocked for systemd"
fi

if [[ -e /dev/rtc0 ]]; then
  echo "== syncing the two clocks"
  RTC_YEAR="$(hwclock -r 2>/dev/null | grep -Eo '^[0-9]{4}' || echo 0)"
  SYS_YEAR="$(date +%Y)"
  if [[ "$SYS_YEAR" -ge 2024 && "$RTC_YEAR" -lt 2024 ]]; then
    hwclock -w && ok "RTC set from the system clock ($(date '+%F %T'))"
  elif [[ "$RTC_YEAR" -ge 2024 && "$SYS_YEAR" -lt 2024 ]]; then
    hwclock -s && ok "system clock set from the RTC ($(date '+%F %T'))"
  elif [[ "$RTC_YEAR" -ge 2024 ]]; then
    ok "both clocks look sane (RTC: $(hwclock -r 2>/dev/null | cut -c1-19))"
  else
    bad "neither clock knows the time yet" \
        "set it from the phone (Timing tab) -- the service now writes it to the RTC"
  fi
fi

echo
echo "-------------------------------------------------------------"
echo "  $PASS passed, $FAIL failed"
if [[ $FAIL -eq 0 && -e /dev/rtc0 ]]; then
  echo "  The sign keeps real time across power cuts now."
  echo "  Wall-clock schedules stop pausing once the time is set."
else
  echo "  Fix the failures above and run this again."
fi
exit $((FAIL > 0))
