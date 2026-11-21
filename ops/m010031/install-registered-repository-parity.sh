#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
REPOSITORY=/home/chuck/projects/repo-control-ii
SOURCE=${REPOSITORY}/ops/systemd/repo-control-m010031-registered-repositories.conf
DROPIN_DIRECTORY=/etc/systemd/system/repo-control.service.d
TARGET=${DROPIN_DIRECTORY}/m010031-registered-repositories.conf
BACKUP=/root/repo-control-m010031-before
ACL_BACKUP=${BACKUP}/selection-directories.acl
PYTHON=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python

GIT_DIRECTORIES=(
  /home/chuck/projects/repo-control-ii/.git
  /home/chuck/projects/vocab-app/.git
  /home/chuck/projects/photo-organizer-dev/.git
)
SELECTION_DIRECTORIES=(
  /var/lib/repo-control-staging/m010.028-r8/state/repo-control-ii--945e7f88eafb/milestones
  /var/lib/repo-control-staging/m010.028-r8/state/vocab-app--0d81efec80ef/milestones
  /var/lib/repo-control-staging/m010.028-r8/state/photo-organizer-dev--84c64acdea9f/milestones
)

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this reviewed operator procedure with sudo." >&2
  exit 2
fi

verify_registered_inputs() {
  local resolved index
  for index in 0 1 2; do
    resolved=$(git -C "${GIT_DIRECTORIES[$index]%/.git}" rev-parse --absolute-git-dir)
    [[ ${resolved} == "${GIT_DIRECTORIES[$index]}" ]] || {
      echo "Registered Git metadata path changed: ${GIT_DIRECTORIES[$index]}" >&2
      return 1
    }
    [[ -d ${SELECTION_DIRECTORIES[$index]} && ! -L ${SELECTION_DIRECTORIES[$index]} ]] || {
      echo "Managed selection directory is missing or unsafe: ${SELECTION_DIRECTORIES[$index]}" >&2
      return 1
    }
  done
}

verify() {
  verify_registered_inputs
  cmp -s "${SOURCE}" "${TARGET}" || {
    echo "Installed parity drop-in differs from the versioned source." >&2
    return 1
  }
  local effective path health
  effective=$(systemctl show repo-control.service -p ReadWritePaths --value)
  for path in "${GIT_DIRECTORIES[@]}" "${SELECTION_DIRECTORIES[@]}"; do
    [[ " ${effective} " == *" ${path} "* ]] || {
      echo "Effective sandbox lacks exact write surface: ${path}" >&2
      return 1
    }
  done
  for path in "${SELECTION_DIRECTORIES[@]}"; do
    getfacl -cp "${path}" | grep -qx 'user:chuck:rwx' || {
      echo "Service account ACL is missing on ${path}" >&2
      return 1
    }
  done
  systemctl is-active --quiet repo-control.service
  health=""
  for _ in {1..60}; do
    if health=$(curl --fail --silent http://127.0.0.1:8765/healthz 2>/dev/null); then
      break
    fi
    systemctl is-failed --quiet repo-control.service && {
      systemctl status repo-control.service --no-pager -l >&2 || true
      echo "Repo Control failed before readiness; use the configuration-only undo." >&2
      return 1
    }
    sleep 1
  done
  [[ -n ${health} ]] || {
    echo "Repo Control did not become ready within 60 seconds; use the configuration-only undo." >&2
    return 1
  }
  HEALTH_JSON=${health} "${PYTHON}" - <<'PY'
import json
import os

expected = {
    "information_store_authority": "sqlite_managed",
    "database_identity": "5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17",
    "authority_generation": "63137936ea4a4f7db30c6b6b2ea4c59b",
    "authority_configuration_sha256": "266f722946a7a85a417bbf79752562514598aa0c11696ea530c7b5cc9bc6676b",
}
health = json.loads(os.environ["HEALTH_JSON"])
binding = health.get("information_store_binding") or {}
coverage = health.get("managed_write_coverage") or {}
if health.get("information_store_authority") != expected["information_store_authority"]:
    raise SystemExit("health authority is not sqlite_managed")
for key in ("database_identity", "authority_generation", "authority_configuration_sha256"):
    if binding.get(key) != expected[key]:
        raise SystemExit(f"R8 binding mismatch for {key}")
if coverage.get("status") != "ready" or coverage.get("write_eligible") is not True:
    raise SystemExit("managed write coverage is not ready")
print(
    "READY sqlite_managed "
    f"generation={binding['authority_generation']} database={binding['database_identity']} "
    f"selection={binding['selection_id']}@{binding['selection_revision']}"
)
PY
  systemctl is-enabled --quiet repo-control-backup-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-m010028-r8.timer
  systemctl is-enabled --quiet repo-control-backup-monitor-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-monitor-m010028-r8.timer
  echo "PARITY VERIFIED: exact three-repository surfaces; managed R8 authority and recovery timers remain active"
}

case ${ACTION} in
  install)
    verify_registered_inputs
    if [[ ! -e ${BACKUP} ]]; then
      install -d -o root -g root -m 0700 "${BACKUP}"
      getfacl -p "${SELECTION_DIRECTORIES[@]}" >"${ACL_BACKUP}"
      if [[ -e ${TARGET} ]]; then
        cp -a "${TARGET}" "${BACKUP}/m010031-registered-repositories.conf"
      else
        touch "${BACKUP}/m010031-registered-repositories.conf.absent"
      fi
    fi
    [[ -f ${ACL_BACKUP} ]] || {
      echo "Prior-state ACL capture is incomplete: ${ACL_BACKUP}" >&2
      exit 2
    }
    already_exact=true
    cmp -s "${SOURCE}" "${TARGET}" || already_exact=false
    for path in "${SELECTION_DIRECTORIES[@]}"; do
      getfacl -cp "${path}" | grep -qx 'user:chuck:rwx' || already_exact=false
    done
    install -d -o root -g root -m 0755 "${DROPIN_DIRECTORY}"
    install -o root -g root -m 0644 "${SOURCE}" "${TARGET}"
    setfacl -m u:chuck:rwx "${SELECTION_DIRECTORIES[@]}"
    systemctl daemon-reload
    if [[ ${already_exact} == true ]]; then
      echo "PASS exact configuration already installed; restart not repeated"
    else
      systemctl restart repo-control.service
    fi
    verify
    ;;
  verify)
    verify
    ;;
  undo)
    [[ -f ${ACL_BACKUP} ]] || { echo "Missing prior-state ACL capture: ${ACL_BACKUP}" >&2; exit 2; }
    if [[ -e ${BACKUP}/m010031-registered-repositories.conf.absent ]]; then
      rm -f "${TARGET}"
    else
      cp -a "${BACKUP}/m010031-registered-repositories.conf" "${TARGET}"
    fi
    setfacl --restore="${ACL_BACKUP}"
    systemctl daemon-reload
    systemctl restart repo-control.service
    systemctl is-active --quiet repo-control.service
    echo "UNDO VERIFIED: only the M010.031 drop-in and selection ACLs were restored"
    ;;
  *)
    echo "Usage: sudo $0 {install|verify|undo}" >&2
    exit 2
    ;;
esac
