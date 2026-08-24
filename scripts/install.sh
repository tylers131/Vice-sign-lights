#!/usr/bin/env bash
# Install vice-sign-lights on Raspberry Pi OS Lite.
# Run from a checkout:  sudo ./scripts/install.sh
set -euo pipefail

APP_DIR=/opt/vice-sign-lights
CONF_DIR=/etc/vice-lights
STATE_DIR=/var/lib/vice-lights
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }

echo "==> apt dependencies"
apt-get update
apt-get install -y python3 python3-venv python3-pip bluez

echo "==> copying $SRC_DIR -> $APP_DIR"
mkdir -p "$APP_DIR" "$CONF_DIR" "$STATE_DIR"
for item in vicelights elk_scan.py matrix_probe.py requirements.txt config.example.json README.md systemd scripts tests; do
  [[ -e "$SRC_DIR/$item" ]] && cp -r "$SRC_DIR/$item" "$APP_DIR/"
done

echo "==> python venv (piwheels supplies the ARMv6 wheels)"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip wheel

# dbus-fast ships Cython extensions. On armv6 no prebuilt wheel exists anywhere,
# so pip compiles for the better part of an hour on a 1GHz core and may OOM --
# SKIP_CYTHON=1 takes the pure-Python implementation instead. On armv7 (piwheels)
# and arm64 (PyPI manylinux) a compiled wheel exists, and skipping it would throw
# away the faster implementation for nothing.
ARCH="$(uname -m)"
PIP_ENV=()
case "$ARCH" in
  armv6l)
    echo "==> $ARCH: no compiled dbus-fast exists; installing the pure-Python build"
    PIP_ENV=(SKIP_CYTHON=1)
    ;;
  *)
    echo "==> $ARCH: using compiled wheels where they exist"
    ;;
esac

env "${PIP_ENV[@]}" "$APP_DIR/venv/bin/pip" install \
  --extra-index-url https://www.piwheels.org/simple \
  -r "$APP_DIR/requirements.txt"

if [[ ! -f "$CONF_DIR/config.json" ]]; then
  echo "==> seeding $CONF_DIR/config.json from the example"
  cp "$APP_DIR/config.example.json" "$CONF_DIR/config.json"
  echo "    It carries the sign's 12 known BLE addresses; rename them from the UI."
  echo "    To re-detect instead: sudo $APP_DIR/venv/bin/python $APP_DIR/elk_scan.py adopt --out $CONF_DIR/config.json --force"
fi

# The service runs as root, but the CLI tools run as you. Without this the
# config is root-only and every elk_scan invocation needs sudo.
if [[ -n "${SUDO_USER:-}" ]]; then
  echo "==> giving $SUDO_USER ownership of $CONF_DIR so the CLI works without sudo"
  chown -R "$SUDO_USER" "$CONF_DIR" "$STATE_DIR"
fi
chmod 0755 "$CONF_DIR"
[[ -f "$CONF_DIR/config.json" ]] && chmod 0644 "$CONF_DIR/config.json"

REVISION="$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf '%s %s\n' "$REVISION" "$(git -C "$SRC_DIR" log -1 --format=%s 2>/dev/null || true)" \
  > "$APP_DIR/INSTALLED_FROM"

echo "==> systemd unit"
install -m 0644 "$SRC_DIR/systemd/vice-lights.service" /etc/systemd/system/vice-lights.service
systemctl daemon-reload
systemctl enable vice-lights.service
systemctl restart vice-lights.service

sleep 3
systemctl --no-pager --lines=15 status vice-lights.service || true
echo
echo "Done. Web UI: http://192.168.4.1/ once the access point is up."
echo
echo "The service runs from $APP_DIR, not from your checkout. To ship code changes:"
echo "    cd $SRC_DIR && git pull && sudo ./scripts/update.sh"
echo "Set up the AP with:  sudo ./scripts/setup_ap_networkmanager.sh   (Bookworm)"
echo "                or:  sudo ./scripts/setup_ap_hostapd.sh          (Bullseye)"
