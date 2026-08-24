#!/usr/bin/env bash
# Push the current checkout into the installed service and restart it.
#
# The service runs from /opt/vice-sign-lights, so `git pull` alone changes
# nothing about what is running. This copies the code across and restarts,
# without re-running apt or pip -- use install.sh when dependencies change.
#
#   cd ~/vice-sign-lights && git pull && sudo ./scripts/update.sh
set -euo pipefail

APP_DIR=/opt/vice-sign-lights
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "run me with sudo" >&2; exit 1; }
[[ -d "$APP_DIR/venv" ]] || { echo "$APP_DIR/venv missing -- run install.sh first" >&2; exit 1; }

echo "==> $SRC_DIR -> $APP_DIR"
for item in vicelights elk_scan.py matrix_probe.py vice_kiosk.py requirements.txt config.example.json README.md scripts; do
  [[ -e "$SRC_DIR/$item" ]] || continue
  rm -rf "${APP_DIR:?}/$item"
  cp -r "$SRC_DIR/$item" "$APP_DIR/"
done
find "$APP_DIR/vicelights" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# Record what was installed, so the service can say so at startup.
REVISION="$(git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DESCRIBED="$(git -C "$SRC_DIR" log -1 --format=%s 2>/dev/null || echo '')"
printf '%s %s\n' "$REVISION" "$DESCRIBED" > "$APP_DIR/INSTALLED_FROM"

if ! cmp -s "$SRC_DIR/systemd/vice-lights.service" /etc/systemd/system/vice-lights.service; then
  echo "==> unit file changed, reinstalling it"
  install -m 0644 "$SRC_DIR/systemd/vice-lights.service" /etc/systemd/system/vice-lights.service
  systemctl daemon-reload
fi

# A restart works perfectly well on a disabled unit, so a sign deployed with
# this script alone runs all day and then never comes back from a power cut.
# Enabling here is idempotent and makes that impossible to get wrong.
if ! systemctl is-enabled --quiet vice-lights 2>/dev/null; then
  echo "==> service was not enabled for boot; enabling it"
  systemctl enable vice-lights.service
fi

echo "==> restarting"
systemctl restart vice-lights

# The touchscreen is its own service running its own copy of vice_kiosk.py,
# which this script has just replaced -- so without this it keeps running the
# old code and an update looks like it did nothing. Only when it is installed:
# a sign with no panel attached should not grow an error message.
if systemctl list-unit-files vice-kiosk.service >/dev/null 2>&1 \
   && systemctl is-enabled --quiet vice-kiosk 2>/dev/null; then
  echo "==> restarting the touchscreen too"
  systemctl restart vice-kiosk || echo "    (vice-kiosk did not come back; systemctl status vice-kiosk)"
fi
sleep 4
journalctl -u vice-lights -n 12 --no-pager | sed 's/^/    /'
echo

if systemctl is-active --quiet vice-lights; then
  echo "Now running $REVISION (enabled for boot)"
else
  echo "FAILED: vice-lights is not running after the restart." >&2
  systemctl --no-pager --lines=15 status vice-lights >&2 || true
  exit 1
fi
