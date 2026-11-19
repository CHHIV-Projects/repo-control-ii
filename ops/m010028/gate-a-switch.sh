#!/usr/bin/env bash
# M010.028 Gate A operator execution. Deliver only after exact Product Owner
# approval of this plan and window. Run it as a child process; never source it:
#   if bash gate-a-switch.sh --plan P --approval-record R --approval-sha256 H; then echo "GATE A: PASS"; else echo "GATE A: STOPPED (exit $?)"; fi
# Exit 2: refused before the service stop; the managed env may have been installed.
# Exit 3: legacy may be stopped; run gate-a-return.sh.
# Exit 4: commit status uncertain after the switch; use forward recovery, not the return.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "gate-a-switch.sh must be run with bash, not sourced." >&2
  return 2
fi
set -euo pipefail

PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
SELF="$HERE/$(basename "${BASH_SOURCE[0]}")"
GATE="$HERE/gate_a.py"
RETURN_SCRIPT="$HERE/gate-a-return.sh"
ENV_ATTEMPTED=0

refuse() {
  echo "GATE A REFUSED: $*" >&2
  if (( ENV_ATTEMPTED )); then
    echo "The service was not stopped; the managed environment install was attempted and may have changed $ENV_PATH. Inspect it before retrying." >&2
  else
    echo "No privileged step ran. Legacy authority is unchanged." >&2
  fi
  exit 2
}

PLAN="" RECORD="" RECORD_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan) PLAN=${2:-}; shift 2 || refuse "missing value for --plan" ;;
    --approval-record) RECORD=${2:-}; shift 2 || refuse "missing value for --approval-record" ;;
    --approval-sha256) RECORD_SHA=${2:-}; shift 2 || refuse "missing value for --approval-sha256" ;;
    *) refuse "unknown argument: $1" ;;
  esac
done
[[ -n "$PLAN" && -n "$RECORD" && -n "$RECORD_SHA" ]] ||
  refuse "usage: gate-a-switch.sh --plan P --approval-record R --approval-sha256 H"
APPROVAL_ARGS=(--plan "$PLAN" --approval-record "$RECORD" --approval-sha256 "$RECORD_SHA"
  --switch-script "$SELF" --return-script "$RETURN_SCRIPT")
FIELD_NAMES=(unit installed_unit_path managed_unit_source managed_env_source managed_env_path)

echo "== 1. Verify exact Product Owner approval (no privileged step yet)"
"$PY" "$GATE" verify-approval "${APPROVAL_ARGS[@]}" ||
  refuse "approval did not verify"

echo "== 2. Preflight: legacy unit, hold, source parity, recovery point, publishers"
"$PY" "$GATE" preflight --plan "$PLAN" || refuse "preflight failed"

echo "== 3. Authenticate and revalidate before installing the managed environment"
sudo -v || refuse "sudo authentication failed"
FIELDS=$("$PY" "$GATE" verify-approval "${APPROVAL_ARGS[@]}" --preflight --fields "${FIELD_NAMES[@]}") ||
  refuse "post-authentication approval or preflight did not verify"
echo "PASS post-authentication approval, window, hold, source, recovery and publishers"
read -r UNIT INSTALLED MANAGED_SRC ENV_SRC ENV_PATH <<<"$FIELDS"
[[ -n "${ENV_PATH:-}" ]] || refuse "incomplete plan fields"

ENV_ATTEMPTED=1
sudo -n install -o root -g root -m 0600 "$ENV_SRC" "$ENV_PATH" || refuse "managed environment install failed"

echo "== 4. Final pre-stop revalidation"
FINAL_FIELDS=$("$PY" "$GATE" verify-approval "${APPROVAL_ARGS[@]}" --preflight --fields "${FIELD_NAMES[@]}") ||
  refuse "final pre-stop approval or preflight did not verify"
[[ "$FINAL_FIELDS" == "$FIELDS" ]] || refuse "approved plan fields changed"
echo "PASS final approval, window, hold, source, recovery and publishers"

stopped() {
  echo "GATE A FAILED at or after the legacy service stop attempt: $*" >&2
  echo "Do not retry. Return now, from this shell or a fresh SSH session:" >&2
  echo "  bash $RETURN_SCRIPT --plan $PLAN" >&2
  exit 3
}

echo "== 5. Switch $UNIT to the held managed candidate"
sudo -n systemctl stop "$UNIT" || stopped "stop failed"
sudo -n install -o root -g root -m 0644 "$MANAGED_SRC" "$INSTALLED" || stopped "unit install failed"
sudo -n systemctl daemon-reload || stopped "daemon-reload failed"
sudo -n systemctl start "$UNIT" || stopped "start failed"

echo "== 6. Bounded readiness: exact managed authority and binding"
"$PY" "$GATE" readiness --plan "$PLAN" --expect managed || stopped "readiness failed"
HELD_RC=0
"$PY" "$GATE" held-check --plan "$PLAN" || HELD_RC=$?
if [[ $HELD_RC -ne 0 ]]; then
  echo "GATE A FAILED: held DB/WAL or inventory changed or unverifiable; commit status is uncertain." >&2
  echo "Do not return to legacy and do not retry. Keep writers paused; use reviewed forward recovery." >&2
  exit 4
fi

echo "GATE A SWITCH VERIFIED: live authority is the exact held candidate; hold unchanged; no write made."
