#!/usr/bin/env bash
# Self-hosted access point via hostapd + dnsmasq.
# Use this on Bullseye, or on Bookworm if NetworkManager AP mode misbehaves.
#
#   sudo ./scripts/setup_ap_hostapd.sh [SSID] [PASSPHRASE] [CHANNEL]
#   BAND=2.4 sudo ./scripts/setup_ap_hostapd.sh          # force the old band
#
# BAND defaults to 5 GHz, and that default is the whole point.
#
# The Pi 4 has one antenna shared between wifi and Bluetooth. With the AP on
# 2.4GHz it sits directly on top of the BLE data channels, and the measured
# result on this sign was not "slower" -- it was total: every BLE connection
# established at the link layer and was then aborted by the local host
# (le-connection-abort-by-local), while scanning kept working perfectly,
# because advertising uses three channels at the edges of the band and a
# connection hops through the middle of it. Connects went from 4.7s to a 42s
# timeout, 100% of the time, purely on whether hostapd was running.
#
# On 5GHz the antenna diplexer passes 2.4 (Bluetooth) and 5 (wifi) at once, so
# they stop fighting. Verify it on the hardware afterwards:
#
#   sudo ./scripts/ble_connect_test.sh <a BLE address>
#
# The cost is range: 5GHz carries less far and through less. For a sign you
# walk up to that is a fair trade, and if it is not, the other way out is a USB
# Bluetooth dongle with its own antenna, which lets the AP stay on 2.4GHz.
set -euo pipefail

SSID="${1:-ViceSign}"
PSK="${2:-burningman}"
BAND="${BAND:-5}"
if [[ "$BAND" == "5" ]]; then
  # UNII-1. Non-DFS, so there is no radar-detection wait before the AP comes
  # up -- which matters on a machine that has to work from cold with nobody
  # watching it.
  CHANNEL="${3:-36}"
else
  CHANNEL="${3:-6}"
fi
IFACE="${IFACE:-wlan0}"
COUNTRY="${COUNTRY:-US}"
# 192.168.4.1 is the single most common home-router address, and standing on
# it is not a small mistake: wlan0 takes the gateway's address, the Pi starts
# resolving DNS against itself, and dnsmasq's catch-all answers every name with
# the Pi -- so the machine loses its own uplink, apt, and git in one step.
# 192.168.50.0/24 is picked because almost nothing ships on it.
IP="${AP_IP:-192.168.50.1}"
NET="${IP%.*}"

ip_to_int() {
  local IFS=. a b c d; read -r a b c d <<<"$1"
  echo $(( (a << 24) | (b << 16) | (c << 8) | d ))
}

# Refuse to stand on a network this machine already uses. Getting this wrong
# costs the Pi its uplink, and at the sign the only way back is a keyboard.
AP_INT="$(ip_to_int "$IP")"
CLASH=""
while read -r iface addr; do
  [[ "$iface" == "$IFACE" || -z "$addr" ]] && continue
  other="${addr%%/*}"; bits="${addr##*/}"
  [[ "$other" == "$addr" ]] && bits=32
  mask=$(( bits == 0 ? 0 : (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  other_int="$(ip_to_int "$other")"
  # Overlap either way: their network containing our address, or ours (a /24)
  # containing theirs.
  if (( (other_int & mask) == (AP_INT & mask) ))      || (( (other_int & 0xFFFFFF00) == (AP_INT & 0xFFFFFF00) )); then
    CLASH="$iface already holds $addr"
    break
  fi
done < <(ip -4 -brief addr 2>/dev/null | awk '{for (i = 3; i <= NF; i++) print $1, $i}')

GW="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
if [[ -z "$CLASH" && -n "$GW" ]]; then
  gw_int="$(ip_to_int "$GW")"
  (( (gw_int & 0xFFFFFF00) == (AP_INT & 0xFFFFFF00) ))     && CLASH="the default gateway is $GW"
fi

if [[ -n "$CLASH" ]]; then
  cat >&2 <<CLASHMSG
Refusing to put the access point on $IP: $CLASH.

Standing on a network this machine already uses breaks its own routing and
DNS -- and because dnsmasq answers every name with the AP address, the Pi
then cannot reach apt, git, or anything else. Recovering that needs a
keyboard on the sign.

Pick a subnet nothing else here uses:

    sudo AP_IP=192.168.50.1 $0 ${1:+\'$1\'} ${2:+\'$2\'}
CLASHMSG
  exit 1
fi

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

if [[ "$BAND" == "5" ]]; then
  echo "==> checking $IFACE can do 5GHz"
  if ! iw phy 2>/dev/null | grep -qE '\* 5[0-9]{3}(\.[0-9]+)? MHz'; then
    echo "    this adapter reports no 5GHz channels; falling back to 2.4GHz" >&2
    echo "    (BLE will contend with the AP -- see the note at the top of this" >&2
    echo "     file, and consider a USB Bluetooth dongle)" >&2
    BAND=2.4
    CHANNEL=6
  fi
fi

echo "==> hostapd config (${BAND}GHz, channel $CHANNEL)"
if [[ "$BAND" == "5" ]]; then
  BAND_CONF="hw_mode=a
ieee80211n=1
ieee80211ac=1
# Regulatory rules come from the beacons of whoever else is around, and on the
# playa there is nobody else -- so the country code has to be authoritative.
ieee80211d=1
# No DFS channels above, so radar detection is not needed and the AP does not
# have to wait a minute before it starts serving.
ieee80211h=0"
else
  BAND_CONF="hw_mode=g
ieee80211n=1"
fi
cat > /etc/hostapd/hostapd.conf <<CONF
interface=$IFACE
driver=nl80211
ssid=$SSID
country_code=$COUNTRY
channel=$CHANNEL
$BAND_CONF
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
  if [[ "$BAND" == "5" ]]; then
    echo
    echo "On 5GHz the usual cause is the regulatory domain. Check:"
    echo "    iw reg get                 # should show country $COUNTRY, not 00"
    echo "    sudo raspi-config          # Localisation -> WLAN Country"
    echo "Then re-run. To fall back to the old band, losing the BLE fix:"
    echo "    BAND=2.4 sudo $0 '$SSID' '$PSK'"
  fi
  exit 1
fi

# Which band actually came up. hostapd will silently land somewhere else if the
# requested channel is not permitted, and the whole point of this change is
# which band the AP is on -- so read it back rather than assume.
LIVE="$(iw dev "$IFACE" info 2>/dev/null | awk '/channel/ {print $0}')"
echo "    $IFACE: ${LIVE:-(could not read the channel)}"
if [[ "$BAND" == "5" ]] && ! grep -qE '\(5[0-9]{3}' <<<"$LIVE"; then
  echo "    WARNING: asked for 5GHz but the interface is not on a 5GHz channel."
  echo "             BLE will still contend with the AP." >&2
fi

cat <<MSG

Access point configured. Reboot to be sure it comes up cleanly: sudo reboot
  SSID:       $SSID
  Passphrase: $PSK
  Band:       ${BAND}GHz, channel $CHANNEL
  Web UI:     http://$IP/
  DHCP range: $NET.10 - $NET.60

Now prove Bluetooth got its band back -- this is the measurement that
matters, not the fact that the AP is up:

  sudo ./scripts/ble_connect_test.sh <a BLE address from ./matrix_probe.py scan>

Every trial should pass. If the ones with the AP running still fail, the
contention is not fixed and the fallback is a USB Bluetooth dongle with its
own antenna.
MSG
