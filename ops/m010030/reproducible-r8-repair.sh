#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
REPOSITORY=/home/chuck/projects/repo-control-ii
SOURCE_SYSTEMD=${REPOSITORY}/ops/systemd
SERVICE_DROPIN=/etc/systemd/system/repo-control.service.d
BINDING_DIRECTORY=/etc/repo-control/managed-authority.d
BINDING_TARGET=${BINDING_DIRECTORY}/r8.json
SELECTION_DIRECTORY=/var/lib/repo-control-staging/m010.028-r8/state/repo-control-ii--945e7f88eafb/milestones
BACKUP=/root/repo-control-m010030-before
PYTHON=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python

DROPINS=(r8-selection-pointer.conf r8-sqlite-runtime.conf r8-git.conf)
SOURCES=(
  repo-control-m010028-r8-selection-pointer.conf
  repo-control-m010028-r8-sqlite-runtime.conf
  repo-control-m010028-r8-git.conf
)

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this reviewed operator procedure with sudo." >&2
  exit 2
fi

verify() {
  systemctl is-active --quiet repo-control.service
  local health cli
  health=""
  for _ in {1..60}; do
    if health=$(curl --fail --silent http://127.0.0.1:8765/healthz 2>/dev/null); then
      break
    fi
    systemctl is-failed --quiet repo-control.service && {
      systemctl status repo-control.service --no-pager -l >&2 || true
      echo "Repo Control failed before readiness; do not retry this installer." >&2
      return 1
    }
    sleep 1
  done
  [[ -n ${health} ]] || {
    echo "Repo Control did not become ready within 60 seconds; do not retry this installer." >&2
    return 1
  }
  cli=$(sudo -u chuck env -u REPOCTL_BACKUP_COVERAGE_PATH \
    "${PYTHON}" -m repoctl.cli coder-status \
    --repository "${REPOSITORY}" --format json)
  HEALTH_JSON=${health} CLI_JSON=${cli} "${PYTHON}" - <<'PY'
import json
import os

expected = {
    "authority": "sqlite_managed",
    "authority_state_root": "/var/lib/repo-control-staging/m010.028-r8/state",
    "authority_configuration_sha256": "266f722946a7a85a417bbf79752562514598aa0c11696ea530c7b5cc9bc6676b",
    "authority_generation": "63137936ea4a4f7db30c6b6b2ea4c59b",
    "database_identity": "5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17",
}
health = json.loads(os.environ["HEALTH_JSON"])
cli = json.loads(os.environ["CLI_JSON"])
browser = health["information_store_binding"]
for key, value in expected.items():
    if browser.get(key) != value:
        raise SystemExit(f"browser binding mismatch for {key}")
if cli.get("information_store_authority") != "sqlite_managed":
    raise SystemExit("ordinary CLI did not resolve sqlite_managed")
selection = cli["selection"]
for key in ("milestone_id", "selection_id", "selection_revision"):
    if browser.get(key) != selection.get(key):
        raise SystemExit(f"browser/CLI selection mismatch for {key}")
if cli["repository"]["repository_id"] != browser["repository_id"]:
    raise SystemExit("browser/CLI repository mismatch")
expected_prompt_suffix = f":{browser['prompt_revision_id']}:{browser['prompt_sha256']}"
if not cli["prompt"]["identity"].endswith(expected_prompt_suffix):
    raise SystemExit("browser/CLI prompt revision mismatch")
print(
    "READY sqlite_managed "
    f"generation={browser['authority_generation']} database={browser['database_identity']} "
    f"selection={browser['selection_id']}@{browser['selection_revision']}"
)
PY
  systemctl is-enabled --quiet repo-control-backup-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-m010028-r8.timer
  systemctl is-enabled --quiet repo-control-backup-monitor-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-monitor-m010028-r8.timer
  echo "PASS R8 browser/CLI binding and coverage timers"
}

case "${ACTION}" in
  install)
    [[ $(stat -c '%U:%G:%a' "${SELECTION_DIRECTORY}") == root:chuck:770 ]] || {
      echo "Selection directory ownership or mode differs from the reviewed baseline." >&2
      exit 2
    }
    getfacl -cp "${SELECTION_DIRECTORY}" | grep -qx 'user:chuck:rwx' || {
      echo "Selection directory lacks the reviewed chuck:rwx ACL." >&2
      exit 2
    }
    if [[ ! -e ${BACKUP} ]]; then
      install -d -o root -g root -m 0700 "${BACKUP}"
      systemctl cat repo-control.service >"${BACKUP}/repo-control.service.cat"
      getfacl -p "${SELECTION_DIRECTORY}" >"${BACKUP}/selection-directory.acl"
      for target in "${DROPINS[@]}"; do
        if [[ -e ${SERVICE_DROPIN}/${target} ]]; then
          cp -a "${SERVICE_DROPIN}/${target}" "${BACKUP}/${target}"
        else
          touch "${BACKUP}/${target}.absent"
        fi
      done
      if [[ -e ${BINDING_TARGET} ]]; then
        cp -a "${BINDING_TARGET}" "${BACKUP}/r8.json"
      else
        touch "${BACKUP}/r8.json.absent"
      fi
      [[ -d ${BINDING_DIRECTORY} ]] || touch "${BACKUP}/managed-authority-directory.absent"
    else
      [[ -f ${BACKUP}/repo-control.service.cat && -f ${BACKUP}/selection-directory.acl ]] || {
        echo "Prior-state capture is incomplete: ${BACKUP}" >&2
        exit 2
      }
    fi

    configuration_was_exact=true
    for index in 0 1 2; do
      cmp -s "${SOURCE_SYSTEMD}/${SOURCES[$index]}" "${SERVICE_DROPIN}/${DROPINS[$index]}" \
        || configuration_was_exact=false
    done
    cmp -s "${REPOSITORY}/ops/repo-control-managed-cli-r8.json" "${BINDING_TARGET}" \
      || configuration_was_exact=false

    install -d -o root -g root -m 0755 "${SERVICE_DROPIN}" "${BINDING_DIRECTORY}"
    for index in 0 1 2; do
      install -o root -g root -m 0644 \
        "${SOURCE_SYSTEMD}/${SOURCES[$index]}" "${SERVICE_DROPIN}/${DROPINS[$index]}"
    done
    install -o root -g root -m 0644 \
      "${REPOSITORY}/ops/repo-control-managed-cli-r8.json" "${BINDING_TARGET}"
    setfacl -m u:chuck:rwx "${SELECTION_DIRECTORY}"
    systemctl daemon-reload
    if [[ ${configuration_was_exact} == true ]]; then
      if ! systemctl is-active --quiet repo-control.service; then
        echo "Exact configuration is installed but the service is inactive; do not retry the installer." >&2
        echo "Use the reviewed bounded recovery action for the preserved managed R8 service." >&2
        exit 3
      fi
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
    [[ -d ${BACKUP} ]] || { echo "Missing prior-state capture: ${BACKUP}" >&2; exit 2; }
    for target in "${DROPINS[@]}"; do
      if [[ -e ${BACKUP}/${target}.absent ]]; then
        rm -f "${SERVICE_DROPIN}/${target}"
      else
        cp -a "${BACKUP}/${target}" "${SERVICE_DROPIN}/${target}"
      fi
    done
    if [[ -e ${BACKUP}/r8.json.absent ]]; then
      rm -f "${BINDING_TARGET}"
    else
      cp -a "${BACKUP}/r8.json" "${BINDING_TARGET}"
    fi
    setfacl --restore="${BACKUP}/selection-directory.acl"
    if [[ -e ${BACKUP}/managed-authority-directory.absent ]]; then
      rmdir "${BINDING_DIRECTORY}"
    fi
    systemctl daemon-reload
    systemctl restart repo-control.service
    systemctl is-active --quiet repo-control.service
    echo "UNDO VERIFIED: prior configuration restored; managed R8 service remains active"
    ;;
  *)
    echo "Usage: sudo $0 {install|verify|undo}" >&2
    exit 2
    ;;
esac
