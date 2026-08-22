#!/usr/bin/env bash
# Capture everything needed to work out why the sign is misbehaving, in one
# pass, into one file you can send on later.
#
#   ./scripts/diagnose.sh              # report to stdout and /tmp/vice-diag.txt
#   ./scripts/diagnose.sh --no-scan    # skip the BLE scan (it takes ~30s)
#
# Written for a garage with no network: it asks nothing and needs nothing.
set -uo pipefail          # not -e: a missing tool must not stop the report

OUT="${VICE_DIAG_OUT:-/tmp/vice-diag.txt}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCAN=1
[[ "${1:-}" == "--no-scan" ]] && SCAN=0

exec > >(tee "$OUT") 2>&1

section() { printf '\n===== %s =====\n' "$1"; }
try()     { echo "\$ $*"; "$@" 2>&1 | sed 's/^/    /' || echo "    (failed or not present)"; }

echo "vice sign diagnostics -- $(date 2>/dev/null)"
echo "host $(hostname 2>/dev/null)  uptime $(cut -d' ' -f1 /proc/uptime 2>/dev/null)s"

section "what is installed"
try cat /opt/vice-sign-lights/INSTALLED_FROM
try git -C "$SRC_DIR" log --oneline -1

section "services"
for unit in vice-lights vice-kiosk bluetooth hostapd dnsmasq; do
  printf '    %-12s %-10s %s\n' "$unit" \
    "$(systemctl is-active "$unit" 2>/dev/null)" \
    "$(systemctl is-enabled "$unit" 2>/dev/null)"
done

section "power and heat"
# Sticky bits: they latch until reboot, so a 3am brownout still shows here.
try vcgencmd get_throttled
try vcgencmd measure_temp
echo "    0x0 = healthy. bit 0 under-voltage now, bit 16 under-voltage has happened."

section "bluetooth adapter"
try rfkill list bluetooth
try hciconfig -a
try systemctl status bluetooth --no-pager --lines=5

section "radio neighbourhood"
# The AP shares one chip with BLE, so who is on it matters.
try iw dev
try iwconfig
echo "    AP clients:"
iw dev wlan0 station dump 2>/dev/null | grep -c Station | sed 's/^/    /' \
  || echo "    (none, or not an AP)"

section "network"
try ip -brief addr
try ip route

section "storage"
try df -h /
try dmesg -T --level=err,warn

section "BLE scan (RSSI against the recorded baseline)"
if [[ $SCAN -eq 1 ]]; then
  # The one measurement that separates "radio is worse" from "software is
  # broken". config.json notes B_C at -81 dBm and B_E at -83 dBm from the Pi's
  # original test position; 10-15 dB worse than that across the board means
  # something is interfering, not failing.
  echo "    scanning ~30s ..."
  ( cd "$SRC_DIR" && ./elk_scan.py scan --repeat 3 --elk-only 2>&1 ) | sed 's/^/    /'
  # An empty table above is ambiguous on its own, and the ambiguity matters: it
  # is the difference between "the sign is unplugged / you are not near it" and
  # "the radio has failed". Say how many OTHER devices were heard, because a
  # radio that hears forty strangers and none of the twelve is not a broken
  # radio.
  echo
  echo "    how many devices were audible at all (ELK or not):"
  ( cd "$SRC_DIR" && ./elk_scan.py scan --repeat 1 2>&1 ) \
    | grep -cE '^[0-9A-F]{2}:' | sed 's/^/      /' || echo "      (scan failed)"
  echo "      If that number is healthy but no controller is listed, the"
  echo "      controllers are out of range, unpowered, or already connected to"
  echo "      something -- not a fault on this Pi."
else
  echo "    skipped (--no-scan)"
fi

section "recent sweeps"
# The file log survives reboots; the journal here does not.
try tail -n 60 /var/log/vice-lights.log

section "sweep timings, oldest last"
grep -hE "phase averages|failing device|-> done" /var/log/vice-lights.log 2>/dev/null \
  | tail -n 20 | sed 's/^/    /' || echo "    (no log yet)"

printf '\n===== end =====\n'
echo "Saved to $OUT -- send that file."
