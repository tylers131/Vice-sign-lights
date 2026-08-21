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
# libegl-mesa0 is named explicitly, and this is not optional. SDL's KMSDRM
# backend dlopens libEGL at runtime rather than declaring it, so pygame
# installs and imports perfectly happily without any EGL driver present -- and
# then fails at set_mode with "EGL not initialized", which reads like a
# configuration problem rather than a missing package. libgbm1 arrives as a
# hard dependency while its EGL counterpart does not, which is what makes this
# so easy to miss.
apt-get install -y --no-install-recommends \
    python3-pygame libegl1 libegl-mesa0 libgles2 libgbm1 libgl1-mesa-dri

echo "==> group access for the framebuffer and the touchscreen"
# The service runs as root and does not need these, but they let you reproduce
# the panel by hand as $KIOSK_USER when something needs debugging.
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
# getty owns tty1 and holds DRM master on the display it is drawing the console
# on. Two processes cannot both be master, so SDL fails with "EGL not
# initialized" while the login prompt sits there looking fine. Conflicts= stops
# getty when the panel starts and brings it back if the panel is ever disabled.
Conflicts=getty@tty1.service
After=getty@tty1.service

[Service]
Type=simple
# Root, because taking DRM master on the panel needs CAP_SYS_ADMIN unless the
# process is the session leader on the active VT -- which a systemd service is
# not, whatever groups its user is in. Running as $KIOSK_USER fails with
# "EGL not initialized" while the very same command works under sudo. The
# vice-lights service is already root for port 80 and the clock, so this adds
# no privilege that is not already on the box, and the panel only ever talks to
# 127.0.0.1.
User=root
# A real login session on a VT, so SDL can take the display."
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

echo "==> handing tty1 to the panel"
# Disabled rather than only stopped: otherwise it comes back on the next boot
# and races the panel for the display. Console login moves to SSH, which is how
# this machine is administered anyway.
systemctl disable --now getty@tty1.service 2>/dev/null || true
echo "    getty@tty1 disabled (console login is over SSH)"

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

If the panel is blank or ignores touches, reboot once before investigating
further -- the group memberships above only apply to a fresh login.

Turn the panel off again:  sudo systemctl disable --now vice-kiosk
MSG
