#!/usr/bin/env bash
# Switch the Pi between hosting its own access point and joining a normal wifi
# network. The AP has no uplink, so you cannot git pull or apt install while it
# is up -- this is how you get back to a network that does.
#
#   sudo ./scripts/ap.sh status
#   sudo ./scripts/ap.sh off [SSID]     # rejoin a client network (needs internet)
#   sudo ./scripts/ap.sh on             # host ViceSign again (playa mode)
#
# Either switch drops the connection you are running over. Use --delay to let
# the command return first, then reconnect on the other network:
#
#   sudo ./scripts/ap.sh off --delay 5
set -euo pipefail

CON=vice-ap
IFACE="${IFACE:-wlan0}"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
command -v nmcli >/dev/null || {
  echo "nmcli not found -- this Pi uses the hostapd setup, not NetworkManager." >&2
  echo "Disable the AP with: sudo systemctl disable --now hostapd dnsmasq" >&2
  exit 1; }

ACTION="${1:-status}"; shift || true
TARGET_SSID=""
DELAY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --delay) DELAY="${2:-5}"; shift 2 ;;
    *) TARGET_SSID="$1"; shift ;;
  esac
done

# Re-run detached so the switch happens after this SSH session has been answered.
if [[ "$DELAY" != "0" && "${VICE_AP_DETACHED:-}" != "1" ]]; then
  echo "switching in ${DELAY}s -- this session will drop."
  case "$ACTION" in
    off) echo "  reconnect on your normal wifi, then ssh to the Pi's usual address" ;;
    on)  echo "  join ViceSign, then: ssh ${SUDO_USER:-pi}@$(ap_address)" ;;
  esac
  VICE_AP_DETACHED=1 systemd-run --quiet --on-active="$DELAY" \
    --setenv=VICE_AP_DETACHED=1 --unit="vice-ap-switch-$$" \
    "$(readlink -f "${BASH_SOURCE[0]}")" "$ACTION" ${TARGET_SSID:+"$TARGET_SSID"}
  exit 0
fi

ap_address() {
  # Whatever the AP is actually on. Never a literal: this address is chosen at
  # setup time, and printing a stale guess sends someone to the wrong machine.
  local found
  found="$(ip -4 -brief addr show "$IFACE" 2>/dev/null | awk '{print $3}')"
  found="${found%%/*}"
  if [[ -z "$found" ]]; then
    # dnsmasq's catch-all records the AP address as "address=/#/1.2.3.4".
    found="$(sed -n 's|^[[:space:]]*address=/#/||p' \
             /etc/dnsmasq.d/vice-ap.conf 2>/dev/null | head -1)"
  fi
  echo "${found:-the Pi}"
}

client_profiles() {
  nmcli -t -f NAME,TYPE connection show \
    | awk -F: -v ap="$CON" '$2 ~ /wireless/ && $1 != ap {print $1}'
}

case "$ACTION" in
  status)
    echo "== device"
    nmcli -f GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS device show "$IFACE" || true
    echo
    echo "== $CON"
    if nmcli -t -f NAME connection show | grep -qx "$CON"; then
      nmcli -t -f connection.autoconnect,802-11-wireless.mode,ipv4.addresses \
        connection show "$CON" | sed 's/^/    /'
      nmcli -t -f NAME connection show --active | grep -qx "$CON" \
        && echo "    ACTIVE -- web UI at http://$(ap_address)/" \
        || echo "    not active"
    else
      echo "    not configured (run setup_ap_networkmanager.sh)"
    fi
    echo
    echo "== other saved wifi networks"
    client_profiles | sed 's/^/    /' || echo "    none"
    ;;

  off)
    echo "==> stopping the access point"
    nmcli connection modify "$CON" connection.autoconnect no || true
    nmcli connection down "$CON" 2>/dev/null || true
    if [[ -z "$TARGET_SSID" ]]; then
      TARGET_SSID="$(client_profiles | head -1 || true)"
    fi
    [[ -n "$TARGET_SSID" ]] || {
      echo "No saved client wifi to fall back to. Connect one with:" >&2
      echo "  sudo nmcli device wifi connect 'YourSSID' password 'yourpassword'" >&2
      exit 1; }
    echo "==> joining '$TARGET_SSID'"
    nmcli connection up "$TARGET_SSID" 2>/dev/null \
      || nmcli device wifi connect "$TARGET_SSID"
    sleep 3
    nmcli -f GENERAL.CONNECTION,IP4.ADDRESS device show "$IFACE" || true
    echo
    echo "The AP will not come back on reboot until you run: sudo ./scripts/ap.sh on"
    ;;

  on)
    nmcli -t -f NAME connection show | grep -qx "$CON" || {
      echo "$CON does not exist -- run setup_ap_networkmanager.sh first" >&2; exit 1; }
    echo "==> starting the access point"
    nmcli connection modify "$CON" connection.autoconnect yes
    nmcli connection up "$CON"
    sleep 3
    nmcli -f GENERAL.CONNECTION,IP4.ADDRESS device show "$IFACE" || true
    echo
    echo "Join ViceSign, then the web UI is at http://$(ap_address)/"
    echo "There is no uplink in this mode: no git pull, no apt."
    ;;

  *)
    echo "usage: $0 {status|off [SSID]|on} [--delay SECONDS]" >&2
    exit 2 ;;
esac
