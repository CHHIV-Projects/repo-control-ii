#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
REPOSITORY=/home/chuck/projects/repo-control-ii
PYTHON=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
BACKUP=/root/repo-control-m011001-authority-correction-before
BASELINE_HEAD=dd9e2c18e37b5f9194fe265c3e180edca36f7332

declare -A EXPECTED=(
  [src/repoctl/web/app.py]=3fdb27714021d85e3c85b0a3571c65c91f860305ddd55d0f51dd6a4cc3dc5ffd
  [src/repoctl/web/closeout_publication.py]=7dfb034640745df4fbbe175c223d200cd00106ae5e47a57af5b831a6b813cf94
  [src/repoctl/web/templates/architect_dispatches.html]=4d52cefd5be8f4800ea7a92ee18a2ad6cb3a0ea6402fa985c1b8282868691a78
  [src/repoctl/web/templates/closeout_publication.html]=f9fec4471b2eebe97c214ae424e6ab76d28878e7553eabb41a7f9fe24513a486
  [src/repoctl/web/templates/milestone_workflow.html]=cfb2b65ffa233cd5ed5106a9ae8c609df667184aaf8c76fe0c23f37f8dd460ea
  [src/repoctl/web/templates/procedure_updates.html]=73d7e89e648046de4f8bc7182c4fa39435d62921aeea139b429cbc975c2400b3
)

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this separately authorized correction rollout with sudo." >&2
  exit 2
fi

verify_sources() {
  local relative observed
  for relative in "${!EXPECTED[@]}"; do
    [[ -f ${REPOSITORY}/${relative} && ! -L ${REPOSITORY}/${relative} ]] || {
      echo "Missing or unsafe correction source: ${relative}" >&2
      return 1
    }
    observed=$(sha256sum "${REPOSITORY}/${relative}" | awk '{print $1}')
    [[ ${observed} == "${EXPECTED[${relative}]}" ]] || {
      echo "Correction source hash mismatch: ${relative}" >&2
      return 1
    }
  done
  TEMPLATE_ROOT="${REPOSITORY}/src/repoctl/web/templates" "${PYTHON}" - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["TEMPLATE_ROOT"])
forbidden = (
    "I am the Product Owner",
    "Confirm this decision as Product Owner",
    'name="confirm_publish"',
)
for path in root.glob("*.html"):
    text = path.read_text(encoding="utf-8")
    for phrase in forbidden:
        if phrase in text:
            raise SystemExit(f"redundant Product Owner identity control remains: {path}: {phrase}")
PY
  echo "PASS exact six-file correction source and generic identity-checkbox audit"
}

wait_ready() {
  local health=""
  for _ in {1..60}; do
    if health=$(curl --fail --silent http://127.0.0.1:8765/healthz 2>/dev/null); then
      break
    fi
    if systemctl is-failed --quiet repo-control.service; then
      systemctl status repo-control.service --no-pager -l >&2 || true
      echo "Repo Control failed before readiness; preserve evidence and do not retry." >&2
      return 1
    fi
    sleep 1
  done
  [[ -n ${health} ]] || {
    echo "Repo Control did not become ready within 60 seconds; preserve evidence and do not retry." >&2
    return 1
  }
  HEALTH_JSON=${health} "${PYTHON}" - <<'PY'
import json
import os

health = json.loads(os.environ["HEALTH_JSON"])
binding = health.get("information_store_binding", {})
expected = {
    "authority": "sqlite_managed",
    "authority_state_root": "/var/lib/repo-control-staging/m010.028-r8/state",
    "authority_generation": "63137936ea4a4f7db30c6b6b2ea4c59b",
    "database_identity": "5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17",
}
for key, value in expected.items():
    if binding.get(key) != value:
        raise SystemExit(f"live R8 binding mismatch for {key}")
print(
    "READY sqlite_managed "
    f"generation={binding['authority_generation']} database={binding['database_identity']} "
    f"selection={binding.get('selection_id')}@{binding.get('selection_revision')}"
)
PY
}

verify_runtime() {
  verify_sources
  systemctl is-active --quiet repo-control.service
  wait_ready
  systemctl is-enabled --quiet repo-control-backup-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-m010028-r8.timer
  systemctl is-enabled --quiet repo-control-backup-monitor-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-monitor-m010028-r8.timer
  page=$(curl --fail --silent http://127.0.0.1:8765/procedure-updates)
  [[ ${page} == *"repo-control-ii"* && ${page} == *"vocab-app"* && ${page} == *"photo-organizer"* ]]
  echo "PASS corrected source is live; exact managed R8 authority and recovery timers remain active"
}

capture_prior_source() {
  if [[ -e ${BACKUP} ]]; then
    [[ -f ${BACKUP}/capture-complete ]] || {
      echo "Prior-source capture is incomplete: ${BACKUP}" >&2
      return 1
    }
    return
  fi
  observed_head=$(git -C "${REPOSITORY}" rev-parse HEAD)
  [[ ${observed_head} == "${BASELINE_HEAD}" ]] || {
    echo "Refusing to capture rollback bytes from unexpected HEAD: ${observed_head}" >&2
    return 1
  }
  install -d -o root -g root -m 0700 "${BACKUP}/files"
  local relative
  for relative in "${!EXPECTED[@]}"; do
    install -d -o root -g root -m 0700 "${BACKUP}/files/$(dirname "${relative}")"
    git -C "${REPOSITORY}" show "HEAD:${relative}" >"${BACKUP}/files/${relative}"
    chmod 0600 "${BACKUP}/files/${relative}"
  done
  git -C "${REPOSITORY}" rev-parse HEAD >"${BACKUP}/head"
  systemctl show repo-control.service -p ExecStart -p WorkingDirectory -p Environment -p ReadWritePaths \
    >"${BACKUP}/repo-control.service.effective"
  touch "${BACKUP}/capture-complete"
}

rollout() {
  verify_sources
  systemctl is-active --quiet repo-control.service
  wait_ready
  capture_prior_source
  systemctl restart repo-control.service
  verify_runtime
}

undo() {
  [[ -f ${BACKUP}/capture-complete ]] || {
    echo "Missing complete prior-source capture: ${BACKUP}" >&2
    return 1
  }
  verify_sources
  local relative
  for relative in "${!EXPECTED[@]}"; do
    [[ -f ${BACKUP}/files/${relative} && ! -L ${BACKUP}/files/${relative} ]] || {
      echo "Missing captured prior source: ${relative}" >&2
      return 1
    }
  done
  for relative in "${!EXPECTED[@]}"; do
    install -o chuck -g chuck -m 0644 "${BACKUP}/files/${relative}" "${REPOSITORY}/${relative}"
  done
  systemctl restart repo-control.service
  systemctl is-active --quiet repo-control.service
  wait_ready
  echo "UNDO VERIFIED: only the six captured correction sources were restored; managed R8 authority is unchanged"
}

case ${ACTION} in
  preflight)
    verify_sources
    systemctl is-active --quiet repo-control.service
    wait_ready
    echo "PREFLIGHT PASS: no live state changed"
    ;;
  rollout)
    rollout
    ;;
  verify)
    verify_runtime
    ;;
  undo)
    undo
    ;;
  *)
    echo "Usage: sudo $0 {preflight|rollout|verify|undo}" >&2
    exit 2
    ;;
esac
