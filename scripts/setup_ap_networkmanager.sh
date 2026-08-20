#!/usr/bin/env bash
# Self-hosted access point via NetworkManager (Raspberry Pi OS Bookworm).
# The Pi becomes 192.168.4.1 on its own wifi network. No uplink, no internet.
#
#   sudo ./scripts/setup_ap_networkmanager.sh [SSID] [PASSPHRASE] [CHANNEL]
set -euo pipefail

SSID="${1:-ViceSign}"
PSK="${2:-burningman}"
CHANNEL="${3:-6}"
IFACE="${IFACE:-wlan0}"
CON=vice-ap
ADDR=192.168.4.1/24
COUNTRY="${COUNTRY:-US}"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
[[ ${#PSK} -ge 8 ]] || { echo "WPA passphrase must be >= 8 characters" >&2; exit 1; }
command -v nmcli >/dev/null || { echo "nmcli not found; use setup_ap_hostapd.sh" >&2; exit 1; }

echo "==> regulatory domain $COUNTRY (wifi stays blocked without it)"
raspi-config nonint do_wifi_country "$COUNTRY" 2>/dev/null || iw reg set "$COUNTRY" || true
rfkill unblock wifi || true

echo "==> creating NetworkManager AP profile '$CON'"
nmcli connection delete "$CON" >/dev/null 2>&1 || true
nmcli connection add type wifi ifname "$IFACE" con-name "$CON" autoconnect yes ssid "$SSID"
nmcli connection modify "$CON" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  802-11-wireless.channel "$CHANNEL" \
  802-11-wireless.powersave 2 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.psk "$PSK" \
  ipv4.method shared \
  ipv4.addresses "$ADDR" \
  ipv6.method disabled \
  connection.autoconnect-priority 100 \
  connection.autoconnect-retries 0

echo "==> bringing it up"
nmcli connection up "$CON"
sleep 3
nmcli -f GENERAL.STATE,IP4.ADDRESS device show "$IFACE" || true

cat <<MSG

Access point up.
  SSID:        $SSID
  Passphrase:  $PSK
  Channel:     $CHANNEL (2.4GHz)
  Pi address:  ${ADDR%/*}
  Web UI:      http://${ADDR%/*}/

'ipv4.method shared' runs NetworkManager's own dnsmasq for DHCP, so your phone
gets an address automatically. There is no uplink: your phone may warn about
"no internet" and try to fall back to cellular -- tell it to stay connected.
MSG
