#!/usr/bin/env bash
# Boot straight into the touch panel on a DSI screen.
#
#   sudo ./scripts/setup_kiosk.sh [USER]
#
# pygame drawing on KMSDRM: no browser, no compositor, no desktop. Rendering a
# grid of buttons does not need Chromium and 486MB of dependencies on a machine
# whose actual job is writing nine-byte frames over Bluetooth.
set -euo pipefail

KIOSK_USER="${1:-${SUDO_USER:-vicesign}}"
APP_DIR=/opt/vice-sign-lights
URL="${VICE_KIOSK_URL:-http://127.0.0.1}"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
id "$KIOSK_USER" >/dev/null 2>&1 || { echo "no such user: $KIOSK_USER" >&2; exit 1; }
[[ -f "$APP_DIR/vice_kiosk.py" ]] || {
  echo "$APP_DIR/vice_kiosk.py missing -- run scripts/update.sh first" >&2; exit 1; }

echo "==> checking the panel is detected"
# The kernel enumerates a DSI connector once the ribbon is seated. Nothing else
# here helps if this is missing -- fix the cable first.
DSI="$(find /sys/class/drm -maxdepth 1 -name '*DSI*' 2>/dev/null | head -1 || true)"
if [[ -z "$DSI" ]]; then
  echo "    WARNING: no DSI connector in /sys/class/drm. Connectors present:" >&2
  find /sys/class/drm -maxdepth 1 -name 'card*-*' -printf '      %f\n' 2>/dev/null >&2 || true
  echo "    The ribbon goes in the DISPLAY port (not CAMERA), contacts toward" >&2
  echo "    the board, with the Pi powered off while seating it." >&2
else
  echo "    found $(basename "$DSI") (status: $(cat "$DSI/status" 2>/dev/null || echo unknown))"
fi

# SDL's KMSDRM backend opens /dev/dri/card0 unless told otherwise, and on a
# Pi 4 that is the render node with no connectors on it -- the display lives on
# card1. Pick the card that actually has something plugged into it.
CARD_INDEX=""
for connector in /sys/class/drm/card*-*/status; do
  [[ "$(cat "$connector" 2>/dev/null)" == "connected" ]] || continue
  name="$(basename "$(dirname "$connector")")"       # e.g. card1-DSI-1
  CARD_INDEX="${name#card}"; CARD_INDEX="${CARD_INDEX%%-*}"
  echo "    display is on /dev/dri/card$CARD_INDEX ($name)"
  break
done

echo "==> installing pygame"
apt-get update || echo "    (apt update had trouble; continuing with cached indexes)"
apt-get install -y --no-install-recommends python3-pygame

echo "==> group access for the framebuffer and the touchscreen"
# KMSDRM needs the GPU nodes; libinput needs the event devices. Without input
# the panel draws perfectly and ignores every touch, which is a confusing way
# to fail.
for group in video render input; do
  getent group "$group" >/dev/null || continue
  usermod -aG "$group" "$KIOSK_USER"
  echo "    $KIOSK_USER in $group"
done

echo "==> panel service"
cat > /etc/systemd/system/vice-kiosk.service <<CONF
[Unit]
Description=Vice sign touch panel
# The panel only reads the local HTTP API, so it can start whenever; if the
# service is not up yet it shows "cannot reach the sign" and recovers by itself.
After=vice-lights.service systemd-user-sessions.service
Wants=vice-lights.service

[Service]
Type=simple
User=$KIOSK_USER
# A real login session on a VT, so SDL can take DRM master on the display.
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
StandardInput=tty-force
Environment=SDL_VIDEODRIVER=kmsdrm
${CARD_INDEX:+Environment=SDL_KMSDRM_DEVICE_INDEX=$CARD_INDEX}
Environment=VICE_KIOSK_URL=$URL
Environment=PYTHONUNBUFFERED=1
# Explicit, because StandardInput=tty-force otherwise sends output to the TTY:
# a traceback would scroll past on the panel instead of landing in the journal,
# which is precisely when you need to read it.
StandardOutput=journal
StandardError=journal
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/vice_kiosk.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
CONF

echo "==> disabling console blanking on tty1"
if [[ -f /boot/firmware/cmdline.txt ]] && ! grep -q consoleblank /boot/firmware/cmdline.txt; then
  sed -i 's/$/ consoleblank=0/' /boot/firmware/cmdline.txt
  echo "    added consoleblank=0 (takes effect on reboot)"
fi

systemctl daemon-reload
systemctl enable vice-kiosk.service
systemctl restart vice-kiosk.service
sleep 5

echo
if systemctl is-active --quiet vice-kiosk; then
  echo "    OK      vice-kiosk is running"
else
  echo "    FAILED  vice-kiosk did not start:"
  systemctl --no-pager --lines=20 status vice-kiosk 2>&1 | sed 's/^/            /'
  exit 1
fi

cat <<MSG

Touch panel configured.
  Talking to: $URL
  Service:    vice-kiosk    (systemctl restart vice-kiosk to reload it)

The group memberships above only take effect on a fresh login, so if the panel
is blank or ignores touches, reboot once before investigating further.

Turn the panel off again:  sudo systemctl disable --now vice-kiosk
MSG
