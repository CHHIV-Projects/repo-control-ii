#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
REPOSITORY=/home/chuck/projects/repo-control-ii
SOURCE=${REPOSITORY}/ops/systemd
SYSTEMD=/etc/systemd/system
SERVICE_DROPIN=${SYSTEMD}/repo-control.service.d
TRANSACTIONS=/home/chuck/.local/share/.repoctl-procedure-transactions
BACKUP=/root/repo-control-m011001-before
PYTHON=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python

SOURCES=(
  repo-control-m011001-procedure-store.conf
  repo-control-procedure-update@.service
  repo-control-procedure-update@repo-control-ii.service.d/m011001-write-surface.conf
  repo-control-procedure-update@vocab-app.service.d/m011001-write-surface.conf
  repo-control-procedure-update@photo-organizer.service.d/m011001-write-surface.conf
)
TARGETS=(
  repo-control.service.d/m011001-procedure-store.conf
  repo-control-procedure-update@.service
  repo-control-procedure-update@repo-control-ii.service.d/m011001-write-surface.conf
  repo-control-procedure-update@vocab-app.service.d/m011001-write-surface.conf
  repo-control-procedure-update@photo-organizer.service.d/m011001-write-surface.conf
)

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this separately authorized operator procedure with sudo." >&2
  exit 2
fi

wait_ready() {
  local health=""
  for _ in {1..60}; do
    if health=$(curl --fail --silent http://127.0.0.1:8765/healthz 2>/dev/null); then
      break
    fi
    if systemctl is-failed --quiet repo-control.service; then
      systemctl status repo-control.service --no-pager -l >&2 || true
      echo "Repo Control failed before readiness; preserve evidence and use the reviewed undo only if directed." >&2
      return 1
    fi
    sleep 1
  done
  [[ -n ${health} ]] || {
    echo "Repo Control did not become ready within 60 seconds." >&2
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

verify_sources() {
  [[ $(stat -c '%U:%G:%a' "${TRANSACTIONS}") == chuck:chuck:700 ]] || {
    echo "Existing private transaction root is not chuck:chuck mode 0700: ${TRANSACTIONS}" >&2
    return 1
  }
  [[ ! -L ${TRANSACTIONS} ]] || {
    echo "Existing private transaction root may not be a symlink." >&2
    return 1
  }
  for index in "${!SOURCES[@]}"; do
    [[ -f ${SOURCE}/${SOURCES[$index]} && ! -L ${SOURCE}/${SOURCES[$index]} ]] || {
      echo "Missing or unsafe versioned systemd source: ${SOURCES[$index]}" >&2
      return 1
    }
  done
  systemd-analyze verify "${SOURCE}/repo-control-procedure-update@.service"
}

verify_installed_files() {
  for index in "${!TARGETS[@]}"; do
    cmp -s "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${TARGETS[$index]}" || {
      echo "Installed configuration differs: ${TARGETS[$index]}" >&2
      return 1
    }
    [[ $(stat -c '%U:%G:%a' "${SYSTEMD}/${TARGETS[$index]}") == root:root:644 ]] || {
      echo "Installed configuration has unsafe owner or mode: ${TARGETS[$index]}" >&2
      return 1
    }
  done
}

verify_runtime() {
  verify_sources
  verify_installed_files
  systemctl is-active --quiet repo-control.service
  wait_ready
  environment=$(systemctl show repo-control.service -p Environment --value)
  write_paths=$(systemctl show repo-control.service -p ReadWritePaths --value)
  [[ ${environment} == *"REPOCTL_PROCEDURE_TRANSACTION_ROOT=${TRANSACTIONS}"* ]]
  [[ ${write_paths} == *"${TRANSACTIONS}"* ]]
  systemctl is-enabled --quiet repo-control-backup-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-m010028-r8.timer
  systemctl is-enabled --quiet repo-control-backup-monitor-m010028-r8.timer
  systemctl is-active --quiet repo-control-backup-monitor-m010028-r8.timer
  ! systemctl is-active --quiet 'repo-control-procedure-update@*.service'
  ! systemctl is-enabled --quiet 'repo-control-procedure-update@*.service'
  page=$(curl --fail --silent http://127.0.0.1:8765/procedure-updates)
  for key in repo-control-ii vocab-app photo-organizer; do
    [[ ${page} == *"${key}"* ]] || {
      echo "Procedure Updates omitted ${key}." >&2
      return 1
    }
  done
  [[ ${page} != *"Transaction storage parent must be an owned"* ]]
  [[ ${page} != *"STORAGE_UNAVAILABLE"* ]]
  echo "PASS exact private journal binding, inactive bounded executor, three repository rows, managed R8 identity, and recovery timers"
}

capture_prior_state() {
  if [[ -e ${BACKUP} ]]; then
    [[ -f ${BACKUP}/capture-complete ]] || {
      echo "Prior-state capture is incomplete: ${BACKUP}" >&2
      return 1
    }
    return
  fi
  install -d -o root -g root -m 0700 "${BACKUP}" "${BACKUP}/files" "${BACKUP}/absent"
  systemctl cat repo-control.service >"${BACKUP}/repo-control.service.cat"
  systemctl show repo-control.service -p Environment -p ReadWritePaths -p ReadOnlyPaths \
    >"${BACKUP}/repo-control.service.effective"
  for target in "${TARGETS[@]}"; do
    if [[ -e ${SYSTEMD}/${target} ]]; then
      install -d -o root -g root -m 0700 "${BACKUP}/files/$(dirname "${target}")"
      cp -a "${SYSTEMD}/${target}" "${BACKUP}/files/${target}"
    else
      install -d -o root -g root -m 0700 "${BACKUP}/absent/$(dirname "${target}")"
      touch "${BACKUP}/absent/${target}"
    fi
  done
  touch "${BACKUP}/capture-complete"
}

install_configuration() {
  local exact=true
  for index in "${!TARGETS[@]}"; do
    if [[ -e ${SYSTEMD}/${TARGETS[$index]} ]] && \
       ! cmp -s "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${TARGETS[$index]}"; then
      echo "Refusing an unreviewed existing target: ${SYSTEMD}/${TARGETS[$index]}" >&2
      return 1
    fi
    cmp -s "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${TARGETS[$index]}" || exact=false
  done
  capture_prior_state
  for index in "${!TARGETS[@]}"; do
    install -d -o root -g root -m 0755 "$(dirname "${SYSTEMD}/${TARGETS[$index]}")"
    install -o root -g root -m 0644 \
      "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${TARGETS[$index]}"
  done
  systemctl daemon-reload
  if [[ ${exact} == true ]]; then
    systemctl is-active --quiet repo-control.service || {
      echo "Exact files are installed but Repo Control is inactive; do not retry blindly." >&2
      return 1
    }
    echo "PASS exact configuration already active; restart not repeated"
  else
    systemctl restart repo-control.service
  fi
  verify_runtime
}

undo_configuration() {
  [[ -f ${BACKUP}/capture-complete ]] || {
    echo "Missing complete prior-state capture: ${BACKUP}" >&2
    return 1
  }
  install -d -o root -g root -m 0700 "${BACKUP}/removed"
  for index in "${!TARGETS[@]}"; do
    target=${TARGETS[$index]}
    if [[ -e ${BACKUP}/absent/${target} ]]; then
      if [[ -e ${SYSTEMD}/${target} ]]; then
        cmp -s "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${target}" || {
          echo "Refusing to undo a configuration file changed since M011.001 install: ${target}" >&2
          return 1
        }
        install -d -o root -g root -m 0700 "${BACKUP}/removed/$(dirname "${target}")"
        mv "${SYSTEMD}/${target}" "${BACKUP}/removed/${target}.$(date -u +%Y%m%dT%H%M%SZ)"
      fi
    else
      install -d -o root -g root -m 0755 "$(dirname "${SYSTEMD}/${target}")"
      cp -a "${BACKUP}/files/${target}" "${SYSTEMD}/${target}"
    fi
  done
  rmdir "${SYSTEMD}/repo-control-procedure-update@repo-control-ii.service.d" 2>/dev/null || true
  rmdir "${SYSTEMD}/repo-control-procedure-update@vocab-app.service.d" 2>/dev/null || true
  rmdir "${SYSTEMD}/repo-control-procedure-update@photo-organizer.service.d" 2>/dev/null || true
  systemctl daemon-reload
  systemctl restart repo-control.service
  systemctl is-active --quiet repo-control.service
  wait_ready
  echo "UNDO VERIFIED: only the captured M011.001 systemd configuration was restored; removed files remain recoverable under ${BACKUP}/removed"
}

case ${ACTION} in
  preflight)
    verify_sources
    systemctl is-active --quiet repo-control.service
    wait_ready
    echo "PREFLIGHT PASS: no live configuration changed"
    ;;
  install)
    install_configuration
    ;;
  verify)
    verify_runtime
    ;;
  undo)
    undo_configuration
    ;;
  *)
    echo "Usage: sudo $0 {preflight|install|verify|undo}" >&2
    exit 2
    ;;
esac
