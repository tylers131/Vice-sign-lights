#!/usr/bin/env bash
# Enable USB gadget networking, so a laptop can reach the Pi over the USB cable
# even when wifi is broken.
#
#   sudo ./scripts/enable_usb_console.sh && sudo reboot
#
# Worth doing BEFORE you need it. A Zero W with no wifi and no ethernet is
# otherwise recoverable only by pulling the SD card, which on a Mac means the
# rootfs is unreadable and you are down to editing the boot partition blind.
# Afterwards: plug a cable into the Pi's USB port (not PWR) and
#   ssh <user>@<hostname>.local
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

BOOT=/boot/firmware
[[ -d "$BOOT" ]] || BOOT=/boot
CONFIG="$BOOT/config.txt"
CMDLINE="$BOOT/cmdline.txt"
[[ -f "$CONFIG" && -f "$CMDLINE" ]] || {
  echo "cannot find config.txt/cmdline.txt under $BOOT" >&2; exit 1; }

if grep -q '^dtoverlay=dwc2' "$CONFIG"; then
  echo "config.txt already has dtoverlay=dwc2"
else
  echo "==> adding dtoverlay=dwc2 to $CONFIG"
  cp "$CONFIG" "$CONFIG.bak-vicelights"
  printf '\n[all]\ndtoverlay=dwc2\n' >> "$CONFIG"
fi

if grep -q 'modules-load=dwc2,g_ether' "$CMDLINE"; then
  echo "cmdline.txt already loads the ethernet gadget"
else
  echo "==> adding modules-load=dwc2,g_ether to $CMDLINE"
  cp "$CMDLINE" "$CMDLINE.bak-vicelights"
  # cmdline.txt must stay a single line; insert after rootwait.
  sed -i 's/\brootwait\b/rootwait modules-load=dwc2,g_ether/' "$CMDLINE"
  grep -q 'modules-load=dwc2,g_ether' "$CMDLINE" || {
    echo "   no 'rootwait' to anchor to; appending instead"
    sed -i '1s/$/ modules-load=dwc2,g_ether/' "$CMDLINE"; }
fi

echo
echo "cmdline.txt is now (must be ONE line):"
sed 's/^/    /' "$CMDLINE"
echo
echo "Reboot, then plug a USB cable into the Pi's USB port (not PWR) and:"
echo "    ssh ${SUDO_USER:-pi}@$(hostname).local"
echo "Backups: $CONFIG.bak-vicelights, $CMDLINE.bak-vicelights"
