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
# Override if this collides with the network you use at home -- typing
# 192.168.4.1 and reaching your router instead of the sign is a bad five
# minutes. e.g. AP_ADDR=192.168.50.1/24 sudo ./scripts/setup_ap_networkmanager.sh
ADDR="${AP_ADDR:-192.168.50.1/24}"
COUNTRY="${COUNTRY:-US}"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
[[ ${#PSK} -ge 8 ]] || { echo "WPA passphrase must be >= 8 characters" >&2; exit 1; }
command -v nmcli >/dev/null || { echo "nmcli not found; use setup_ap_hostapd.sh" >&2; exit 1; }

cat <<WARN
This will move $IFACE from your wifi network to hosting its own.
If you are connected over wifi, THIS SESSION WILL DROP -- rejoin "$SSID"
and ssh to ${ADDR%/*}. Ctrl-C now if that is not what you want.
WARN
sleep 5

echo "==> checking the adapter can host an access point"
if command -v iw >/dev/null; then
  if ! iw list 2>/dev/null | grep -A 10 "Supported interface modes" | grep -q '\* AP$'; then
    echo "   WARNING: this adapter does not advertise AP mode. NetworkManager" >&2
    echo "   will fail to bring the profile up. Try scripts/setup_ap_hostapd.sh." >&2
  else
    echo "   AP mode supported"
  fi
fi

# ipv4.method shared makes NetworkManager run its own dnsmasq for DHCP. Without
# the binary the profile activates and then fails, which looks like a hang.
if ! dpkg -s dnsmasq-base >/dev/null 2>&1; then
  echo "==> installing dnsmasq-base (needed for ipv4.method shared)"
  apt-get install -y dnsmasq-base || {
    echo "   could not install dnsmasq-base -- DHCP will not work on the AP" >&2; }
fi

echo "==> regulatory domain $COUNTRY (wifi stays blocked without it)"
raspi-config nonint do_wifi_country "$COUNTRY" 2>/dev/null || iw reg set "$COUNTRY" || true
rfkill unblock wifi || true

echo "==> creating NetworkManager AP profile '$CON'"
nmcli connection delete "$CON" >/dev/null 2>&1 || true
# autoconnect stays OFF until the profile proves it can actually come up. A
# broken AP profile with autoconnect on and priority 100 locks the Pi out of
# every network it knows, on every boot, with no way in short of the SD card.
nmcli connection add type wifi ifname "$IFACE" con-name "$CON" autoconnect no ssid "$SSID"
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
  connection.autoconnect-retries 3

echo "==> bringing it up (30s limit -- it should not take that long)"
if ! nmcli --wait 30 connection up "$CON"; then
  echo
  echo "FAILED to activate '$CON'. NetworkManager said:" >&2
  journalctl -u NetworkManager -n 25 --no-pager | sed 's/^/    /' >&2
  cat <<HINT >&2

Common causes:
  * dnsmasq-base missing            -> ipv4.method shared cannot serve DHCP
  * adapter refuses AP mode         -> check: iw list | grep -A8 "interface modes"
  * wifi still soft-blocked         -> sudo rfkill unblock wifi
  * regulatory domain unset         -> sudo raspi-config nonint do_wifi_country $COUNTRY

The profile is left in place, NOT active, and NOT set to autoconnect -- so a
reboot returns the Pi to your normal wifi rather than locking you out. Undo it
entirely with:
  sudo nmcli connection delete $CON
Or fall back to the hostapd path:
  sudo ./scripts/setup_ap_hostapd.sh "$SSID" "<passphrase>" $CHANNEL
HINT
  exit 1
fi
sleep 3

# It came up. Only now is it safe to have it start on its own at boot.
echo "==> activation succeeded; enabling autoconnect at boot"
nmcli connection modify "$CON" connection.autoconnect yes
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
