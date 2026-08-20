#!/usr/bin/env bash
# Self-hosted access point via hostapd + dnsmasq.
# Use this on Bullseye, or on Bookworm if NetworkManager AP mode misbehaves.
#
#   sudo ./scripts/setup_ap_hostapd.sh [SSID] [PASSPHRASE] [CHANNEL]
set -euo pipefail

SSID="${1:-ViceSign}"
PSK="${2:-burningman}"
CHANNEL="${3:-6}"
IFACE="${IFACE:-wlan0}"
COUNTRY="${COUNTRY:-US}"
IP="${AP_IP:-192.168.4.1}"
NET="${IP%.*}"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
[[ ${#PSK} -ge 8 ]] || { echo "WPA passphrase must be >= 8 characters" >&2; exit 1; }

echo "==> installing hostapd + dnsmasq"
apt-get update
apt-get install -y hostapd dnsmasq

systemctl stop hostapd dnsmasq || true
# If NetworkManager is present it will fight hostapd for wlan0.
if systemctl is-enabled NetworkManager >/dev/null 2>&1; then
  echo "==> telling NetworkManager to leave $IFACE alone"
  mkdir -p /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/99-vice-ap.conf <<CONF
[keyfile]
unmanaged-devices=interface-name:$IFACE
CONF
  systemctl restart NetworkManager || true
fi

echo "==> static IP on $IFACE"
# Which daemon owns the address is decided by what is RUNNING, not by whether
# /etc/dhcpcd.conf exists. Pi OS Trixie ships that file with NetworkManager
# active and dhcpcd not running at all; trusting the file there writes a stanza
# nothing reads, leaves wlan0 down with no address, and dnsmasq then dies with
# "unknown interface wlan0".
USE_DHCPCD=""
if systemctl is-active --quiet dhcpcd 2>/dev/null; then
  USE_DHCPCD=1
fi

# Either way, drop any stanza a previous run left in dhcpcd.conf.
if [[ -f /etc/dhcpcd.conf ]]; then
  sed -i '/# vice-sign-lights AP/,/# end vice-sign-lights AP/d' /etc/dhcpcd.conf
fi

if [[ -n "$USE_DHCPCD" ]]; then
  echo "    dhcpcd is running; pinning the address there"
  # Clear a drop-in a previous run may have left pointing at a unit this branch
  # does not create -- dnsmasq would Require a unit that no longer exists.
  rm -f /etc/systemd/system/dnsmasq.service.d/vice-ap.conf
  systemctl disable vice-ap-ip.service 2>/dev/null || true
  rm -f /etc/systemd/system/vice-ap-ip.service
  systemctl daemon-reload
  cat >> /etc/dhcpcd.conf <<CONF
# vice-sign-lights AP
interface $IFACE
    static ip_address=$IP/24
    nohook wpa_supplicant
# end vice-sign-lights AP
CONF
else
  echo "    dhcpcd is not running; pinning the address with a systemd unit"
  cat > /etc/systemd/system/vice-ap-ip.service <<CONF
[Unit]
Description=Static IP for the Vice sign access point
# dnsmasq is listed as well as hostapd: it binds to this address specifically
# (bind-interfaces), so starting it before the address exists leaves an AP that
# is visible but hands out no DHCP leases -- a phone joins and gets nothing.
Before=hostapd.service dnsmasq.service
Wants=network.target
After=network.target
[Service]
Type=oneshot
ExecStart=/sbin/ip addr flush dev $IFACE
ExecStart=/sbin/ip addr add $IP/24 dev $IFACE
ExecStart=/sbin/ip link set $IFACE up
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
CONF
  systemctl enable vice-ap-ip.service

  # Belt and braces: make dnsmasq itself refuse to start before the address is up.
  mkdir -p /etc/systemd/system/dnsmasq.service.d
  cat > /etc/systemd/system/dnsmasq.service.d/vice-ap.conf <<CONF
[Unit]
After=vice-ap-ip.service
Requires=vice-ap-ip.service
CONF
  systemctl daemon-reload
fi

echo "==> hostapd config"
cat > /etc/hostapd/hostapd.conf <<CONF
interface=$IFACE
driver=nl80211
ssid=$SSID
country_code=$COUNTRY
hw_mode=g
channel=$CHANNEL
ieee80211n=1
wmm_enabled=1
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$PSK
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
CONF
sed -i 's|^#\?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

echo "==> dnsmasq config (DHCP only; there is no uplink to resolve against)"
[[ -f /etc/dnsmasq.conf && ! -f /etc/dnsmasq.conf.orig ]] && cp /etc/dnsmasq.conf /etc/dnsmasq.conf.orig
cat > /etc/dnsmasq.d/vice-ap.conf <<CONF
interface=$IFACE
bind-interfaces
dhcp-range=$NET.10,$NET.60,255.255.255.0,24h
dhcp-option=option:router,$IP
dhcp-option=option:dns-server,$IP
# Answer every name with the Pi so a phone browser lands on the UI.
address=/#/$IP
CONF

rfkill unblock wifi || true
systemctl unmask hostapd
systemctl enable hostapd dnsmasq

echo "==> bringing up $IFACE with $IP"
if [[ -n "$USE_DHCPCD" ]]; then
  systemctl restart dhcpcd
else
  systemctl restart vice-ap-ip.service
fi
sleep 2

# dnsmasq binds to this address specifically, so a missing address here is a
# hard stop with a clear cause rather than "unknown interface" three steps on.
if ! ip addr show "$IFACE" | grep -q "inet ${IP}/"; then
  echo
  echo "FAILED: $IFACE did not come up holding $IP." >&2
  ip addr show "$IFACE" | sed 's/^/    /' >&2
  echo "Nothing is serving yet. Check that $IFACE exists (iw dev) and is not" >&2
  echo "rfkill-blocked (rfkill list), then re-run. ./scripts/ap.sh off reverts." >&2
  exit 1
fi
echo "    $IFACE holds $IP"

systemctl restart hostapd
sleep 2
systemctl restart dnsmasq
sleep 2

echo
echo "==> checking what actually came up"
FAILED=0
for unit in hostapd dnsmasq; do
  if systemctl is-active --quiet "$unit"; then
    echo "    OK      $unit is running"
  else
    echo "    FAILED  $unit is NOT running:"
    systemctl --no-pager --lines=12 status "$unit" 2>&1 | sed 's/^/            /'
    FAILED=1
  fi
done
if ip addr show "$IFACE" | grep -q "inet $IP/"; then
  echo "    OK      $IFACE holds $IP"
else
  echo "    FAILED  $IFACE does not hold $IP:"
  ip addr show "$IFACE" | sed 's/^/            /'
  FAILED=1
fi
if [[ $FAILED -ne 0 ]]; then
  echo
  echo "The access point did NOT come up cleanly. Nothing above is permanent yet"
  echo "beyond the enabled units -- fix the error, or run ./scripts/ap.sh off."
  exit 1
fi

cat <<MSG

Access point configured. Reboot to be sure it comes up cleanly: sudo reboot
  SSID:       $SSID
  Passphrase: $PSK
  Web UI:     http://$IP/
  DHCP range: $NET.10 - $NET.60
MSG
