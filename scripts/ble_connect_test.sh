#!/usr/bin/env bash
# Why can this Pi HEAR a device but not CONNECT to it?
#
#   sudo ./scripts/ble_connect_test.sh AC:36:4B:32:89:C5
#
# Scanning and connecting use the radio differently. Advertising lives on three
# fixed channels (2402, 2426, 2480 MHz) that sit either side of the 2.4GHz wifi
# band; a connection hops across 37 data channels spread through the middle of
# it. So "the scan sees it at -56 dBm" and "the connection times out" are not a
# contradiction -- they are the fingerprint of something occupying the middle of
# the band. On a Pi 4 the obvious candidate is this machine's own access point,
# which shares one antenna with Bluetooth.
#
# This runs the same connection under four conditions and prints a table. It
# restores every service it touched, including on Ctrl-C.
set -uo pipefail

ADDR="${1:-}"
if [[ -z "$ADDR" ]]; then
  echo "usage: $0 AA:BB:CC:DD:EE:FF [--force]" >&2
  echo "  find an address with: ./matrix_probe.py scan" >&2
  exit 2
fi
FORCE=0
[[ "${2:-}" == "--force" ]] && FORCE=1

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${VICE_TEST_OUT:-/tmp/vice-connect-test.txt}"
exec > >(tee "$OUT") 2>&1

if [[ $EUID -ne 0 ]]; then
  echo "run this with sudo: it stops and starts services" >&2
  exit 2
fi

# Refuse to cut the branch we are sitting on. Stopping hostapd drops anyone
# connected over the sign's own wifi, and that includes this shell.
VIA_AP=0
if [[ -n "${SSH_CONNECTION:-}" ]]; then
  server_ip="$(awk '{print $3}' <<<"$SSH_CONNECTION")"
  [[ "$server_ip" == "192.168.50."* ]] && VIA_AP=1
fi
if [[ $VIA_AP -eq 1 && $FORCE -eq 0 ]]; then
  echo "You are connected over the sign's own wifi ($server_ip)."
  echo "Two of the four trials stop hostapd, which would drop this session."
  echo
  echo "Connect over ethernet or the house wifi and run this again, or pass"
  echo "--force to run only the trials that leave hostapd alone."
  exit 2
fi

was_active() { systemctl is-active --quiet "$1"; }

LIGHTS_WAS_UP=0; HOSTAPD_WAS_UP=0; KIOSK_WAS_UP=0
was_active vice-lights && LIGHTS_WAS_UP=1
was_active hostapd     && HOSTAPD_WAS_UP=1
was_active vice-kiosk  && KIOSK_WAS_UP=1

restore() {
  echo
  echo "restoring services ..."
  [[ $HOSTAPD_WAS_UP -eq 1 ]] && systemctl start hostapd  2>/dev/null
  [[ $LIGHTS_WAS_UP  -eq 1 ]] && systemctl start vice-lights 2>/dev/null
  printf '    hostapd %s   vice-lights %s\n' \
    "$(systemctl is-active hostapd 2>/dev/null)" \
    "$(systemctl is-active vice-lights 2>/dev/null)"
  echo "Saved to $OUT"
}
trap restore EXIT INT TERM

echo "BLE connect test -- $(date 2>/dev/null)"
echo "target $ADDR"
echo "at start: vice-lights=$(systemctl is-active vice-lights) "\
"hostapd=$(systemctl is-active hostapd) vice-kiosk=$(systemctl is-active vice-kiosk)"

# The sign's own service owns the adapter for minutes at a time. Every trial
# needs it out of the way, or the first trial measures the queue, not the radio.
echo
echo "stopping vice-lights for the duration ..."
systemctl stop vice-lights 2>/dev/null
sleep 2

ATTEMPTS=2
TIMEOUT=20

attempt() {
  # Prints "ok 3.1s" or "FAILED 40.2s". Uses the probe's exit code, which is 0
  # only on a completed GATT read. Milliseconds via the shell rather than bc,
  # which Pi OS Lite does not ship.
  local started ended elapsed
  started=$(date +%s%3N)
  if ( cd "$SRC_DIR" && ./matrix_probe.py info "$ADDR" \
        --timeout "$TIMEOUT" --retries "$ATTEMPTS" ) >/dev/null 2>&1; then
    ended=$(date +%s%3N)
    elapsed=$((ended - started))
    printf 'ok      %3d.%01ds' $((elapsed / 1000)) $(((elapsed % 1000) / 100))
    return 0
  fi
  ended=$(date +%s%3N)
  elapsed=$((ended - started))
  printf 'FAILED  %3d.%01ds' $((elapsed / 1000)) $(((elapsed % 1000) / 100))
  return 1
}

declare -a NAMES RESULTS
trial() {
  local label="$1"; shift
  echo
  echo "--- $label"
  "$@"
  local line
  line="$(attempt)"
  echo "    $line"
  NAMES+=("$label")
  RESULTS+=("$line")
}

noop() { :; }
forget_device() {
  # BlueZ caches each device with the address type it was first seen as. If
  # that entry is stale or was recorded wrong, every connect goes out with the
  # wrong type and times out -- while scanning, which does not use the cache at
  # all, keeps working perfectly. Dropping the entry forces a fresh discovery.
  echo "    bluetoothctl remove $ADDR   (drop BlueZ's cached entry)"
  bluetoothctl remove "$ADDR" 2>&1 | sed 's/^/      /'
  sleep 2
}
restart_bt() {
  echo "    systemctl restart bluetooth"
  systemctl restart bluetooth
  sleep 3
}
stop_ap() {
  echo "    systemctl stop hostapd"
  systemctl stop hostapd
  sleep 2
}

trial "as found" noop
trial "after clearing BlueZ's cached entry" forget_device
trial "after restarting bluetooth" restart_bt

if [[ $VIA_AP -eq 1 ]]; then
  echo
  echo "--- skipping the two hostapd trials (--force over the sign's own wifi)"
else
  trial "with the access point OFF" stop_ap
  trial "access point OFF + bluetooth restarted" restart_bt
fi

# An independent check: if bluetoothctl cannot connect either, the fault is not
# in bleak or in Python.
echo
echo "--- cross-check with bluetoothctl (nothing to do with Python)"
# Scan first: bluetoothctl will not connect to a device the adapter has not
# discovered in this session, and "Device not available" would look like a
# connection failure when it is not one.
{ echo "scan on"; sleep 10; echo "scan off"; echo "connect $ADDR"; sleep 12;
  echo "quit"; } | timeout 45 bluetoothctl 2>&1 \
  | grep -iE "connect|fail|error|not available|Connected:" | tail -6 | sed 's/^/    /'

echo
echo "===== results ====="
for i in "${!NAMES[@]}"; do
  printf '  %-42s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}"
done

echo
echo "How to read it:"
echo "  * fails everywhere            -> the device is refusing connections."
echo "    Power-cycle the panel and run this again within a minute, and close"
echo "    any phone app that talks to it: these controllers accept exactly one"
echo "    central at a time, and keep advertising while they hold it."
echo "  * works after clearing the cache -> BlueZ had the wrong address type"
echo "    stored. Nothing is wrong with the radio, the panel or the AP."
echo "  * works only with the AP off  -> wifi and Bluetooth are fighting over"
echo "    the one antenna. The sign can still have an access point, but it"
echo "    needs to stop transmitting while BLE writes are in flight, or move"
echo "    to a channel further from the BLE data channels."
echo "  * works after restarting bluetooth -> BlueZ had wedged; nothing is"
echo "    wrong with the radio or the panel."
