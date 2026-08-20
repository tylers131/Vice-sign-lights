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
IP=192.168.4.1

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
if [[ -f /etc/dhcpcd.conf ]]; then
  sed -i '/# vice-sign-lights AP/,/# end vice-sign-lights AP/d' /etc/dhcpcd.conf
  cat >> /etc/dhcpcd.conf <<CONF
# vice-sign-lights AP
interface $IFACE
    static ip_address=$IP/24
    nohook wpa_supplicant
# end vice-sign-lights AP
CONF
else
  # No dhcpcd (Bookworm): pin the address with a tiny oneshot unit.
  cat > /etc/systemd/system/vice-ap-ip.service <<CONF
[Unit]
Description=Static IP for the Vice sign access point
Before=hostapd.service
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
dhcp-range=192.168.4.10,192.168.4.60,255.255.255.0,24h
dhcp-option=option:router,$IP
dhcp-option=option:dns-server,$IP
# Answer every name with the Pi so a phone browser lands on the UI.
address=/#/$IP
CONF

rfkill unblock wifi || true
systemctl unmask hostapd
systemctl enable hostapd dnsmasq
systemctl restart dnsmasq
systemctl restart hostapd
sleep 2
systemctl --no-pager --lines=8 status hostapd || true

cat <<MSG

Access point configured. Reboot to be sure it comes up cleanly: sudo reboot
  SSID:       $SSID
  Passphrase: $PSK
  Web UI:     http://$IP/
MSG
