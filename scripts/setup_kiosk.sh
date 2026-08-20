#!/usr/bin/env bash
# Boot straight into the touch UI on a DSI panel.
#
#   sudo ./scripts/setup_kiosk.sh [USER]
#
# Pi OS Lite has no desktop, so there is nothing for a browser to draw into.
# cage is a Wayland compositor that runs exactly one fullscreen application and
# nothing else -- no panel, no launcher, no window chrome to escape into. That
# is the whole requirement for a panel bolted to a sign.
set -euo pipefail

KIOSK_USER="${1:-${SUDO_USER:-vicesign}}"
URL="${KIOSK_URL:-http://127.0.0.1/kiosk}"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
id "$KIOSK_USER" >/dev/null 2>&1 || { echo "no such user: $KIOSK_USER" >&2; exit 1; }
UID_N="$(id -u "$KIOSK_USER")"

echo "==> checking the panel is detected"
# The kernel enumerates a DSI connector once the ribbon is seated. If this finds
# nothing, no amount of browser configuration will help -- fix the cable first.
DSI="$(find /sys/class/drm -maxdepth 1 -name '*DSI*' 2>/dev/null | head -1 || true)"
if [[ -z "$DSI" ]]; then
  echo "    WARNING: no DSI connector in /sys/class/drm." >&2
  echo "    Connectors present:" >&2
  find /sys/class/drm -maxdepth 1 -name 'card*-*' -printf '      %f\n' 2>/dev/null >&2 || true
  echo "    Check the ribbon is seated the right way round in the DISPLAY port" >&2
  echo "    (contacts toward the board) and that the Pi was powered off when it" >&2
  echo "    was seated. Some third-party panels also need a dtoverlay line in" >&2
  echo "    /boot/firmware/config.txt -- check the panel's own instructions." >&2
  echo "    Continuing anyway; the service will start once the panel appears."
else
  STATUS="$(cat "$DSI/status" 2>/dev/null || echo unknown)"
  echo "    found $(basename "$DSI") (status: $STATUS)"
fi

echo "==> installing cage and a browser"
apt-get update || echo "    (apt update had trouble; continuing with cached indexes)"

# --no-install-recommends deliberately. Pulling cage in with recommends drags
# xwayland, mesa-vulkan-drivers and llvm along -- 486MB and 123 packages, none
# of which a Wayland kiosk uses. Fonts are the one recommend that actually
# matters (without them Chromium renders blank boxes), so ask for them by name.
APT_INSTALL=(apt-get install -y --no-install-recommends)
"${APT_INSTALL[@]}" cage fonts-liberation fonts-dejavu-core

BROWSER=""
for candidate in chromium chromium-browser; do
  if "${APT_INSTALL[@]}" "$candidate" >/dev/null 2>&1 || command -v "$candidate" >/dev/null; then
    BROWSER="$(command -v "$candidate" || true)"
    [[ -n "$BROWSER" ]] && break
  fi
done
[[ -n "$BROWSER" ]] || { echo "could not install chromium or chromium-browser" >&2; exit 1; }
echo "    browser: $BROWSER"

echo "==> kiosk service"
PROFILE="/var/lib/vice-kiosk"
mkdir -p "$PROFILE"
chown "$KIOSK_USER":"$KIOSK_USER" "$PROFILE"

cat > /etc/systemd/system/vice-kiosk.service <<CONF
[Unit]
Description=Vice sign touch panel
# Only the local web UI matters; if it is not up yet the browser retries.
After=vice-lights.service systemd-user-sessions.service
Wants=vice-lights.service

[Service]
Type=simple
User=$KIOSK_USER
# A Wayland compositor needs a seat, which means a real login session on a VT.
PAMName=login
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
StandardInput=tty-force
Environment=XDG_RUNTIME_DIR=/run/user/$UID_N
# The profile is deleted on every start. The sign is powered off by pulling a
# plug, and a Chromium profile that was not closed cleanly shows a "Restore
# pages?" bubble over the UI that nobody can dismiss without a keyboard. There
# is no state worth keeping -- the page is served from this machine.
ExecStartPre=/bin/rm -rf $PROFILE/profile
ExecStart=/usr/bin/cage -s -- $BROWSER \\
    --kiosk --app=$URL \\
    --user-data-dir=$PROFILE/profile \\
    --ozone-platform=wayland \\
    --noerrdialogs --disable-infobars --disable-session-crashed-bubble \\
    --disable-features=Translate,TranslateUI \\
    --no-first-run \\
    --disable-pinch --overscroll-history-navigation=0 \\
    --password-store=basic \\
    --check-for-update-interval=31536000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
CONF

echo "==> disabling console blanking on tty1"
# Under the compositor this is belt and braces, but a blanked VT underneath is
# one more thing that can leave the panel dark for no visible reason.
if [[ -f /boot/firmware/cmdline.txt ]] && ! grep -q consoleblank /boot/firmware/cmdline.txt; then
  sed -i 's/$/ consoleblank=0/' /boot/firmware/cmdline.txt
  echo "    added consoleblank=0 (takes effect on reboot)"
fi

systemctl daemon-reload
systemctl enable vice-kiosk.service
systemctl restart vice-kiosk.service
sleep 6

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
  Showing:  $URL
  Service:  vice-kiosk   (systemctl restart vice-kiosk to reload the page)

If the panel is dark but the service is running, the display is the problem,
not the browser -- check /sys/class/backlight and the ribbon seating.
To turn the panel off again:  sudo systemctl disable --now vice-kiosk
MSG
