#!/usr/bin/env bash
# M010.028 pre-first-write return to the saved legacy unit. Safe from a fresh
# SSH session: it depends only on its arguments and reviewed files. Run it as a
# child process; never source it:
#   if bash gate-a-return.sh --plan P; then echo "RETURN: PASS"; else echo "RETURN: FAILED (exit $?)"; fi
# Exit 2: refused before any change. If the DB/WAL or held inventory changed or
#   cannot be verified, commit status is uncertain: use forward recovery.
# Exit 3: the return started but did not verify; keep writers paused and escalate.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  echo "gate-a-return.sh must be run with bash, not sourced." >&2
  return 2
fi
set -euo pipefail

PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
GATE="$HERE/gate_a.py"

refuse() {
  echo "RETURN REFUSED: $*" >&2
  echo "No change was made. Keep writers paused." >&2
  exit 2
}
failed() {
  echo "RETURN FAILED: $*" >&2
  echo "Keep writers paused; do not retry the switch or release the hold." >&2
  exit 3
}

[[ $# -eq 2 && "$1" == "--plan" ]] || refuse "usage: gate-a-return.sh --plan P"
PLAN=$2

echo "== 1. Verify pre-write return prerequisites"
"$PY" "$GATE" validate --plan "$PLAN" || refuse "plan is invalid"
FIELDS=$("$PY" "$GATE" field --plan "$PLAN" unit installed_unit_path legacy_saved_unit_path) ||
  refuse "could not read plan fields"
read -r UNIT INSTALLED SAVED <<<"$FIELDS"
[[ -n "${SAVED:-}" ]] || refuse "incomplete plan fields"
"$PY" "$GATE" saved-legacy --plan "$PLAN" || refuse "saved legacy unit is not the verified copy"
# held-check exit 0 covers UNCHANGED and EXPECTED_ACTIVITY (-shm content only);
# any other result means a commit cannot be excluded.
HELD_RC=0
"$PY" "$GATE" held-check --plan "$PLAN" || HELD_RC=$?
if [[ $HELD_RC -eq 4 ]]; then
  refuse "COMMIT UNCERTAIN: a protected held path changed; use reviewed forward recovery, not this return"
elif [[ $HELD_RC -ne 0 ]]; then
  refuse "held state could not be verified; commit status is uncertain; use reviewed forward recovery"
fi

echo "== 2. Restore the saved legacy unit"
sudo -v || refuse "sudo authentication failed"
sudo systemctl stop "$UNIT" || failed "stop failed"
sudo install -o root -g root -m 0644 "$SAVED" "$INSTALLED" || failed "legacy unit install failed"
sudo systemctl daemon-reload || failed "daemon-reload failed"
sudo systemctl start "$UNIT" || failed "start failed"

echo "== 3. Bounded readiness: legacy_files with null binding"
"$PY" "$GATE" readiness --plan "$PLAN" --expect legacy || failed "legacy readiness failed"

echo "== 4. Verify preserved hold and source state"
"$PY" "$GATE" held-check --plan "$PLAN" || failed "held DB/WAL or inventory changed or unverifiable: commit uncertain"
"$PY" "$GATE" freeze-inactive --plan "$PLAN" || failed "source freeze is active"

echo "RETURN VERIFIED: legacy authority restored; candidate held and unchanged."
