#!/usr/bin/env bash
# One command that answers "is this sign ready to leave the house?"
#
#   sudo ./scripts/preflight.sh
#
# Every check says PASS, WARN or FAIL and what to do about a FAIL. Exits
# non-zero if anything failed, so it can be trusted rather than skimmed.
#
# Deliberately checks the things that have bitten this build: a service that
# was enabled but not running, an access point on the band that starves
# Bluetooth, an AP address that collides with the house network, a config that
# would not reload, and a panel that is wired up on the bench but not paired
# in the config the service reads.
set -uo pipefail

BASE="${VICE_BASE:-http://127.0.0.1}"
PASS=0; WARN=0; FAIL=0

ok()   { printf '  \033[32mPASS\033[0m  %-34s %s\n' "$1" "${2:-}"; PASS=$((PASS+1)); }
warn() { printf '  \033[33mWARN\033[0m  %-34s %s\n' "$1" "${2:-}"; WARN=$((WARN+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %-34s %s\n' "$1" "${2:-}"; FAIL=$((FAIL+1));
         [[ -n "${3:-}" ]] && printf '        -> %s\n' "$3"; }
head_() { printf '\n%s\n' "$1"; }

api() { curl -sf --max-time 8 "$BASE$1" 2>/dev/null; }

echo "vice sign preflight -- $(date 2>/dev/null)"

# ---------------------------------------------------------------- services
head_ "services"
for unit in vice-lights vice-kiosk hostapd dnsmasq bluetooth; do
  active="$(systemctl is-active "$unit" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  if [[ "$active" == "active" && "$enabled" == "enabled" ]]; then
    ok "$unit" "running, starts at boot"
  elif [[ "$active" == "active" ]]; then
    bad "$unit" "running but NOT enabled" \
        "sudo systemctl enable $unit   # or it will not come back after a power cut"
  elif [[ "$enabled" == "enabled" ]]; then
    bad "$unit" "enabled but NOT running" "sudo systemctl start $unit; systemctl status $unit"
  else
    bad "$unit" "not running, not enabled" "sudo systemctl enable --now $unit"
  fi
done

# ------------------------------------------------------------------ radio
head_ "radio"
CHAN="$(iw dev wlan0 info 2>/dev/null | awk '/channel/ {print $2}')"
FREQ="$(iw dev wlan0 info 2>/dev/null | grep -o '([0-9]\{4\} MHz)' | tr -d '() MHz')"
if [[ -z "$FREQ" ]]; then
  warn "access point band" "could not read the channel"
elif [[ "$FREQ" -ge 5000 ]]; then
  ok "access point band" "channel $CHAN (${FREQ} MHz, 5GHz)"
else
  bad "access point band" "channel $CHAN (${FREQ} MHz, 2.4GHz)" \
      "This starves Bluetooth: measured 42s connect timeouts vs 4.7s. Fix with
           sudo ./scripts/setup_ap_hostapd.sh"
fi

AP_IP="$(ip -4 -brief addr show wlan0 2>/dev/null | awk '{print $3}')"
GW="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
if [[ -z "$AP_IP" ]]; then
  bad "access point address" "wlan0 holds no address" "sudo systemctl restart vice-ap-ip.service"
elif [[ -n "$GW" && "${AP_IP%%/*}" == "$GW" ]]; then
  bad "access point address" "$AP_IP is also the default gateway" \
      "sudo AP_IP=192.168.50.1 ./scripts/setup_ap_hostapd.sh"
else
  ok "access point address" "${AP_IP}"
fi

if rfkill list bluetooth 2>/dev/null | grep -q "yes"; then
  bad "bluetooth radio" "blocked" "sudo rfkill unblock bluetooth"
else
  ok "bluetooth radio" "not blocked"
fi

# ------------------------------------------------------------------ power
head_ "power and heat"
THROTTLED="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
TEMP="$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)"
case "$THROTTLED" in
  "")      warn "power" "vcgencmd unavailable" ;;
  0x0)     ok   "power" "no under-voltage, ever" ;;
  *)       bad  "power" "throttled=$THROTTLED" \
                "bit 0 = under-voltage now, bit 16 = it has happened. Check the supply
           and the cable before the playa: brownouts corrupt SD cards." ;;
esac
[[ -n "$TEMP" ]] && ok "temperature" "$TEMP"

# ------------------------------------------------------------------ the app
head_ "the sign"
if ! api /healthz >/dev/null; then
  bad "web UI" "not answering on $BASE" "systemctl status vice-lights; tail /var/log/vice-lights.log"
else
  ok "web UI" "answering"

  read -r DEVICES SCENES ROT BOOT <<<"$(api /api/state | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("0 0 off none"); raise SystemExit
print(len(d.get("devices") or []), len(d.get("scenes") or []),
      "on" if (d.get("rotation") or {}).get("enabled") else "off",
      (d.get("settings") or {}).get("apply_on_boot") or "none")
')"
  [[ "${DEVICES:-0}" -ge 12 ]] && ok "controllers configured" "$DEVICES" \
    || bad "controllers configured" "${DEVICES:-0}, expected 12" "check /etc/vice-lights/config.json"
  [[ "${SCENES:-0}" -gt 0 ]] && ok "scenes" "$SCENES" || bad "scenes" "none saved"
  [[ "$ROT" == "on" ]] && ok "rotation" "on" || warn "rotation" "off -- the sign will not change on its own"
  [[ "$BOOT" != "none" ]] && ok "boot scene" "$BOOT" \
    || warn "boot scene" "none -- the sign comes up dark after a power cut"

  # Reachability, which is the whole point of being at the sign to run this.
  read -r TOTAL DOWN NAMES <<<"$(api /api/status | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("0 0 -"); raise SystemExit
devices = d.get("devices") or {}
down = [a for a, s in devices.items() if s.get("reachable") is False]
print(len(devices), len(down), ",".join(down[:4]) or "-")
')"
  if [[ "${TOTAL:-0}" -eq 0 ]]; then
    warn "controllers reachable" "none tried yet -- apply a scene, then run this again"
  elif [[ "${DOWN:-0}" -eq 0 ]]; then
    ok "controllers reachable" "$TOTAL/$TOTAL answering"
  else
    bad "controllers reachable" "$DOWN of $TOTAL silent ($NAMES)" \
        "Run this at the sign, with it powered. If they are silent there:
           sudo ./scripts/diagnose.sh"
  fi

  # The text panel: paired in the config the SERVICE reads, not just on the CLI.
  read -r PANEL PNAME PQUEUE <<<"$(api /api/matrix | python3 -c '
import json, sys
try:
    m = json.load(sys.stdin)["matrix"]
except Exception:
    print("error - 0"); raise SystemExit
print("yes" if m.get("configured") else "no",
      (m.get("name") or m.get("address") or "-").replace(" ", "_"),
      m.get("queued", 0))
')"
  case "$PANEL" in
    yes) ok "text panel paired" "$PNAME, $PQUEUE message(s) queued"
         [[ "${PQUEUE:-0}" -eq 0 ]] && warn "panel messages" "none saved -- nothing to cycle" ;;
    no)  warn "text panel paired" "not paired in the service config" \
             ;;
    *)   warn "text panel" "could not read /api/matrix" ;;
  esac
  [[ "$PANEL" == "no" ]] && printf '        -> %s\n' \
     "The panel works from matrix_probe.py but the service cannot drive it until
           it is paired: phone UI -> Message -> Scan for it, or
           curl -X POST $BASE/api/matrix -H 'Content-Type: application/json' \\
             -d '{\"enabled\":true,\"address\":\"AC:36:4B:32:89:C5\",\"family\":\"ipixel\"}'"
fi

# ------------------------------------------------------------------ storage
head_ "storage and clock"
USE="$(df -P / 2>/dev/null | awk 'NR==2 {print $5}' | tr -d '%')"
if [[ -n "$USE" && "$USE" -lt 85 ]]; then ok "disk" "${USE}% used"
elif [[ -n "$USE" ]]; then bad "disk" "${USE}% used" "free space before the playa"; fi

if api /api/status | grep -q '"clock_ok": *true'; then
  ok "clock" "set"
else
  warn "clock" "not set -- wall-clock schedules are paused; timers still work"
fi

CFG=/etc/vice-lights/config.json
if [[ -f "$CFG" ]] && python3 -c "import json,sys; json.load(open('$CFG'))" 2>/dev/null; then
  ok "config parses" "$CFG"
else
  bad "config parses" "$CFG is missing or invalid" "the service keeps .lastgood next to it"
fi

# ------------------------------------------------------------------ verdict
printf '\n%s\n' "-------------------------------------------------------------"
printf '  %d passed, %d warning(s), %d failure(s)\n' "$PASS" "$WARN" "$FAIL"
if [[ $FAIL -gt 0 ]]; then
  printf '\n  \033[31mNOT READY.\033[0m Fix the failures above and run this again.\n\n'
  exit 1
fi
if [[ $WARN -gt 0 ]]; then
  printf '\n  \033[33mREADY, with warnings.\033[0m Nothing here stops the sign working;\n'
  printf '  read them and decide.\n\n'
  exit 0
fi
printf '\n  \033[32mREADY.\033[0m\n\n'
