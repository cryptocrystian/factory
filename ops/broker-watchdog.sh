#!/usr/bin/env bash
# Auth-broker health watchdog.
#
# Turns a silent credential failure into a proactive Buzz alert, so re-auth is a rare, *notified*
# event — never something discovered mid-run. systemd already guarantees the process stays up
# (Restart=always); what it CANNOT see is a credential that refreshed-failed and got disabled while
# the process keeps running. That state does not self-heal — it needs a human re-login — so we must
# detect and announce it.
#
# Three states:
#   DOWN      — the service is not active (systemd is restarting it, or it's masked/failed).
#   DEGRADED  — service is up but a credential was disabled since the current run started.
#   HEALTHY   — service up, no disabled credential this run.
#
# Alerts fire on any transition into a bad state, on recovery, and as a periodic reminder while a
# bad state persists (default every 6h). State is de-duped via a small state file so the timer can
# run often without spamming the channel. Buzz posting is best-effort: a relay hiccup never fails
# the check (it still logs to the journal).

set -uo pipefail

UNIT="${BROKER_UNIT:-omp-auth-broker.service}"
STATE_FILE="${BROKER_WATCHDOG_STATE:-/root/.omp/broker-watchdog.state}"
FACTORY_DIR="${FACTORY_DIR:-/root/factory}"
BUZZ_CHANNEL="${BUZZ_ESCALATION_CHANNEL:-cee4c3ba-d3e4-480c-90d8-1d4f883cf4b0}"  # factory-escalations
REMINDER_SECS="${BROKER_WATCHDOG_REMINDER_SECS:-21600}"                          # 6h
HOSTNAME_S="$(hostname -s 2>/dev/null || echo vps)"

log() { echo "[broker-watchdog] $*"; }

# --- classify current state -------------------------------------------------
detail=""
if ! systemctl is-active --quiet "$UNIT"; then
  state="DOWN"
  detail="systemd reports $UNIT not active (is-active=$(systemctl is-active "$UNIT" 2>&1))."
else
  # Scan the journal only since THIS run began — a disabled credential persists until restart+reauth,
  # so a fixed time window would miss an old-but-still-broken credential.
  since="$(systemctl show -p ActiveEnterTimestamp --value "$UNIT" 2>/dev/null)"
  disabled="$(journalctl -u "$UNIT" ${since:+--since "$since"} --no-pager 2>/dev/null \
              | grep -c 'credential disabled')"
  if [ "${disabled:-0}" -gt 0 ]; then
    state="DEGRADED"
    prov="$(journalctl -u "$UNIT" ${since:+--since "$since"} --no-pager 2>/dev/null \
            | grep 'credential disabled' | grep -oE '"provider":"[^"]*"' | tail -1)"
    detail="A credential was disabled this run (${prov:-provider unknown}). OAuth refresh is failing; a re-login is required. Workers cannot borrow this family until fixed."
  else
    state="HEALTHY"
    detail="service active; no disabled credential since $since."
  fi
fi

now="$(date -u +%s)"

# --- read prior state -------------------------------------------------------
prev_state=""; prev_ts=0
if [ -f "$STATE_FILE" ]; then
  prev_state="$(sed -n '1p' "$STATE_FILE" 2>/dev/null)"
  prev_ts="$(sed -n '2p' "$STATE_FILE" 2>/dev/null)"; prev_ts="${prev_ts:-0}"
fi

# --- decide whether to alert ------------------------------------------------
should_alert=0; kind=""
if [ "$state" != "$prev_state" ]; then
  should_alert=1
  if [ "$state" = "HEALTHY" ]; then kind="RECOVERED"; else kind="ALERT"; fi
elif [ "$state" != "HEALTHY" ]; then
  # same bad state — remind periodically
  if [ $(( now - prev_ts )) -ge "$REMINDER_SECS" ]; then should_alert=1; kind="REMINDER"; fi
fi

# --- persist state (advance reminder clock only when we actually alert) ------
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
if [ "$should_alert" = "1" ] || [ "$state" != "$prev_state" ]; then
  printf '%s\n%s\n' "$state" "$now" > "$STATE_FILE"
else
  printf '%s\n%s\n' "$state" "$prev_ts" > "$STATE_FILE"   # keep original reminder clock
fi

log "state=$state kind=${kind:-none} alert=$should_alert :: $detail"
[ "$should_alert" = "1" ] || exit 0

# --- emit the alert to Buzz (best-effort) -----------------------------------
if [ "$state" = "HEALTHY" ]; then
  emoji="✅"; head="auth-broker recovered on ${HOSTNAME_S}"
else
  emoji="🔴"; head="auth-broker ${state} on ${HOSTNAME_S}"
fi
msg="${emoji} [${kind}] ${head}
${detail}
Runbook: factory/ops/AUTH-BROKER-RUNBOOK.md → 'Re-auth'."

python3 - "$FACTORY_DIR" "$BUZZ_CHANNEL" "$msg" <<'PY' || log "buzz post failed (relay unreachable?) — state still logged above"
import sys
sys.path.insert(0, f"{sys.argv[1]}/control-plane")
from buzz import BuzzClient
BuzzClient.from_env().send_message(sys.argv[2], sys.argv[3])
print("[broker-watchdog] alert posted to Buzz")
PY
exit 0
