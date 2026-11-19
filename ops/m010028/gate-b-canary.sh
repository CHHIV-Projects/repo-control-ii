#!/usr/bin/env bash
# Exact Gate B release plus one canary. Run only with the approval record
# produced from gate_b.py review. Never source and never rerun after release.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "gate-b-canary.sh must be run with bash, not sourced." >&2
  return 2
fi
set -euo pipefail

PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
GATE="$HERE/gate_b.py"

refuse() {
  echo "GATE B REFUSED: $*" >&2
  echo "No hold release or canary was attempted." >&2
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
  refuse "usage: gate-b-canary.sh --plan P --approval-record R --approval-sha256 H"
APPROVAL_ARGS=(--plan "$PLAN" --approval-record "$RECORD" --approval-sha256 "$RECORD_SHA")

echo "== 1. Verify exact Gate B approval and held live target"
"$PY" "$GATE" verify-approval "${APPROVAL_ARGS[@]}" || refuse "approval did not verify"
"$PY" "$GATE" preflight "${APPROVAL_ARGS[@]}" || refuse "preflight failed"

echo "== 2. Authenticate and repeat approval/preflight before release"
sudo -v || refuse "sudo authentication failed"
"$PY" "$GATE" verify-approval "${APPROVAL_ARGS[@]}" || refuse "post-authentication approval failed"
"$PY" "$GATE" preflight "${APPROVAL_ARGS[@]}" || refuse "post-authentication preflight failed"

echo "== 3. Release only the exact inode-bound hold targets"
sudo -n "$PY" "$GATE" release "${APPROVAL_ARGS[@]}" || {
  echo "GATE B RELEASE FAILED: do not invoke the canary; inspect exact target metadata." >&2
  exit 3
}

echo "== 4. Invoke exactly one approved canary and verify its event read-only"
CANARY_RC=0
"$PY" "$GATE" run-canary "${APPROVAL_ARGS[@]}" || CANARY_RC=$?
if [[ $CANARY_RC -ne 0 ]]; then
  echo "GATE B CANARY STOP: commit status is uncertain. Do not retry and do not return to legacy." >&2
  echo "Keep writers paused and resolve the nonce/event read-only using forward recovery." >&2
  exit 4
fi

echo "== 5. Run the exact backup once, blocking on systemctl exit status"
BACKUP_SERVICE=$("$PY" "$GATE" backup-service "${APPROVAL_ARGS[@]}") || {
  echo "POST-WRITE STOP: canary committed but backup service binding is unavailable." >&2
  exit 5
}
sudo -n systemctl start "$BACKUP_SERVICE" || {
  echo "POST-WRITE STOP: canary committed but the backup service failed. Do not rerun the canary." >&2
  exit 5
}
"$PY" "$GATE" post-canary-backup "${APPROVAL_ARGS[@]}" || {
  echo "POST-WRITE STOP: canary committed but fresh bound coverage is unverified. Do not rerun the canary." >&2
  exit 5
}
"$PY" "$GATE" restore-canary-backup "${APPROVAL_ARGS[@]}" || {
  echo "POST-WRITE STOP: canary committed but its separate restore is unverified. Do not rerun the canary." >&2
  exit 5
}

echo "GATE B VERIFIED: exact hold released, one canary committed and read back, fresh backup and separate restore verified."
