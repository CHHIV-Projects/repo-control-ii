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
TEST_FIXTURE_ROOT=${REPOCTL_PROCEDURE_INSTALLER_FIXTURE_ROOT:-}
TEST_MODE=false
INSTALL_OWNER=root
INSTALL_GROUP=root
EXPECTED_AUTHORITY=sqlite_managed
EXPECTED_AUTHORITY_STATE_ROOT=/var/lib/repo-control-staging/m010.028-r8/state
EXPECTED_AUTHORITY_GENERATION=63137936ea4a4f7db30c6b6b2ea4c59b
EXPECTED_DATABASE_IDENTITY=5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17

SOURCES=(
  repo-control-m011001-procedure-store.conf
  repo-control-procedure-update@.service
  repo-control-procedure-update@repo-control-ii.service.d/m011001-write-surface.conf
  repo-control-procedure-update@vocab-app.service.d/m011001-write-surface.conf
  repo-control-procedure-update@photo-organizer.service.d/m011001-write-surface.conf
)
EXPECTED_SOURCE_SHA256=(
  9a3aac799e60eef1f1ac0f02d4b2d8829f5ffe2649b6347c6d5cd99a416d9749
  cb62365733deb4292c979cb5ee7cc9df115ede2b4c1d4ee86ea7f762b04a6899
  64eca87275ac0cbb9fc3ee9327bef987d31268df74b46a0d78fcfe93e8a5d24b
  0ea9352ca071bb83cfb85e2ffaf6bc79af59be2c0c6a5d13d593f700fb861b51
  61ad4d491dc6a384f7a926b41888773109b69ddb550d8a90095555512798071f
)
TARGETS=(
  repo-control.service.d/m011001-procedure-store.conf
  repo-control-procedure-update@.service
  repo-control-procedure-update@repo-control-ii.service.d/m011001-write-surface.conf
  repo-control-procedure-update@vocab-app.service.d/m011001-write-surface.conf
  repo-control-procedure-update@photo-organizer.service.d/m011001-write-surface.conf
)
EXACT_CONFIGURATION=false

if [[ -n ${TEST_FIXTURE_ROOT} ]]; then
  case ${TEST_FIXTURE_ROOT} in
    /tmp/repoctl-procedure-installer.*) ;;
    *)
      echo "Installer fixture root must be a dedicated /tmp/repoctl-procedure-installer.* directory." >&2
      exit 2
      ;;
  esac
  [[ -d ${TEST_FIXTURE_ROOT} && ! -L ${TEST_FIXTURE_ROOT} ]] || {
    echo "Installer fixture root must be an existing real directory." >&2
    exit 2
  }
  PATH=${TEST_FIXTURE_ROOT}/bin
  [[ $(stat -c '%u:%a' "${TEST_FIXTURE_ROOT}") == "${EUID}:700" ]] || {
    echo "Installer fixture root must be owned by the caller with mode 0700." >&2
    exit 2
  }
  TEST_MODE=true
  SOURCE=${TEST_FIXTURE_ROOT}/source
  SYSTEMD=${TEST_FIXTURE_ROOT}/systemd
  SERVICE_DROPIN=${SYSTEMD}/repo-control.service.d
  TRANSACTIONS=${TEST_FIXTURE_ROOT}/transactions
  BACKUP=${TEST_FIXTURE_ROOT}/backup
  INSTALL_OWNER=$(id -un)
  INSTALL_GROUP=$(id -gn)
  EXPECTED_AUTHORITY_STATE_ROOT=${TEST_FIXTURE_ROOT}/state
  EXPECTED_AUTHORITY_GENERATION=00000000000000000000000000000000
  EXPECTED_DATABASE_IDENTITY=0000000000000000000000000000000000000000000000000000000000000000
else
  PATH=/usr/sbin:/usr/bin:/sbin:/bin
fi
export PATH

if [[ ${TEST_MODE} != true && ${EUID} -ne 0 ]]; then
  echo "Run this separately authorized operator procedure with sudo." >&2
  exit 2
fi

require_tools() {
  local tool
  for tool in sha256sum systemd-analyze systemctl stat cmp install cp touch mkdir rmdir mv dirname curl date id sleep; do
    command -v "${tool}" >/dev/null 2>&1 || {
      echo "Required installer tool is unavailable: ${tool}" >&2
      return 1
    }
  done
  [[ -x ${PYTHON} ]] || {
    echo "Required Python interpreter is unavailable: ${PYTHON}" >&2
    return 1
  }
}

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
  HEALTH_JSON=${health} \
    EXPECTED_AUTHORITY=${EXPECTED_AUTHORITY} \
    EXPECTED_AUTHORITY_STATE_ROOT=${EXPECTED_AUTHORITY_STATE_ROOT} \
    EXPECTED_AUTHORITY_GENERATION=${EXPECTED_AUTHORITY_GENERATION} \
    EXPECTED_DATABASE_IDENTITY=${EXPECTED_DATABASE_IDENTITY} \
    "${PYTHON}" - <<'PY'
import json
import os

health = json.loads(os.environ["HEALTH_JSON"])
binding = health.get("information_store_binding", {})
expected = {
    "authority": os.environ["EXPECTED_AUTHORITY"],
    "authority_state_root": os.environ["EXPECTED_AUTHORITY_STATE_ROOT"],
    "authority_generation": os.environ["EXPECTED_AUTHORITY_GENERATION"],
    "database_identity": os.environ["EXPECTED_DATABASE_IDENTITY"],
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
  [[ -d ${SOURCE} && ! -L ${SOURCE} ]] || {
    echo "Procedure executor source root is missing or unsafe: ${SOURCE}" >&2
    return 1
  }
  [[ ${#SOURCES[@]} -eq ${#TARGETS[@]} && ${#SOURCES[@]} -eq ${#EXPECTED_SOURCE_SHA256[@]} ]] || {
    echo "Installer source, hash, and target allowlists are inconsistent." >&2
    return 1
  }
  for index in "${!SOURCES[@]}"; do
    [[ -f ${SOURCE}/${SOURCES[$index]} && ! -L ${SOURCE}/${SOURCES[$index]} ]] || {
      echo "Missing or unsafe versioned systemd source: ${SOURCES[$index]}" >&2
      return 1
    }
    digest=$(sha256sum -- "${SOURCE}/${SOURCES[$index]}") || {
      echo "Could not hash versioned systemd source: ${SOURCES[$index]}" >&2
      return 1
    }
    [[ ${digest%% *} == "${EXPECTED_SOURCE_SHA256[$index]}" ]] || {
      echo "Versioned systemd source hash mismatch: ${SOURCES[$index]}" >&2
      return 1
    }
  done
  systemd-analyze verify "${SOURCE}/repo-control-procedure-update@.service"
}

verify_target_scope() {
  [[ ! -L ${SYSTEMD} && ( ! -e ${SYSTEMD} || -d ${SYSTEMD} ) ]] || {
    echo "Systemd target root is not a real directory: ${SYSTEMD}" >&2
    return 1
  }
  for target in "${TARGETS[@]}"; do
    [[ ${target} != /* && ${target} != *".."* && ${target} != *$'\n'* ]] || {
      echo "Unsafe systemd target path: ${target}" >&2
      return 1
    }
    target_path=${SYSTEMD}/${target}
    [[ ! -L ${target_path} && ( ! -e ${target_path} || -f ${target_path} ) ]] || {
      echo "Systemd target is not a regular file: ${target}" >&2
      return 1
    }
    parent=${target_path%/*}
    while [[ ${parent} == "${SYSTEMD}/"* ]]; do
      [[ ! -L ${parent} && ( ! -e ${parent} || -d ${parent} ) ]] || {
        echo "Unsafe systemd target parent directory: ${parent}" >&2
        return 1
      }
      [[ ${parent} != */* ]] && break
      parent=${parent%/*}
    done
  done
}

verify_existing_targets() {
  EXACT_CONFIGURATION=true
  for index in "${!TARGETS[@]}"; do
    target=${SYSTEMD}/${TARGETS[$index]}
    if [[ -e ${target} || -L ${target} ]]; then
      [[ -f ${target} && ! -L ${target} ]] || {
        echo "Refusing an unsafe existing target: ${target}" >&2
        return 1
      }
      cmp -s "${SOURCE}/${SOURCES[$index]}" "${target}" || {
        echo "Refusing an unreviewed existing target: ${target}" >&2
        return 1
      }
    else
      EXACT_CONFIGURATION=false
    fi
  done
}

verify_backup_destination() {
  [[ ! -L ${BACKUP} && ( ! -e ${BACKUP} || -d ${BACKUP} ) ]] || {
    echo "Prior-state backup path is unsafe: ${BACKUP}" >&2
    return 1
  }
  if [[ -e ${BACKUP} ]]; then
    [[ $(stat -c '%U:%G:%a' "${BACKUP}") == "${INSTALL_OWNER}:${INSTALL_GROUP}:700" ]] || {
      echo "Prior-state backup directory has unsafe ownership or mode: ${BACKUP}" >&2
      return 1
    }
    [[ -f ${BACKUP}/capture-complete && ! -L ${BACKUP}/capture-complete ]] || {
      echo "Prior-state capture is incomplete: ${BACKUP}" >&2
      return 1
    }
  fi
}

preflight_install() {
  require_tools
  verify_target_scope
  verify_sources
  verify_existing_targets
  verify_backup_destination
  systemctl is-active --quiet repo-control.service
  wait_ready
}

verify_installed_files() {
  for index in "${!TARGETS[@]}"; do
    cmp -s "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${TARGETS[$index]}" || {
      echo "Installed configuration differs: ${TARGETS[$index]}" >&2
      return 1
    }
    [[ $(stat -c '%U:%G:%a' "${SYSTEMD}/${TARGETS[$index]}") == "${INSTALL_OWNER}:${INSTALL_GROUP}:644" ]] || {
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
  install -d -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0700 "${BACKUP}" "${BACKUP}/files" "${BACKUP}/absent"
  systemctl cat repo-control.service >"${BACKUP}/repo-control.service.cat"
  systemctl show repo-control.service -p Environment -p ReadWritePaths -p ReadOnlyPaths \
    >"${BACKUP}/repo-control.service.effective"
  for target in "${TARGETS[@]}"; do
    if [[ -e ${SYSTEMD}/${target} ]]; then
      install -d -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0700 "${BACKUP}/files/$(dirname "${target}")"
      cp -a "${SYSTEMD}/${target}" "${BACKUP}/files/${target}"
    else
      install -d -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0700 "${BACKUP}/absent/$(dirname "${target}")"
      touch "${BACKUP}/absent/${target}"
    fi
  done
  touch "${BACKUP}/capture-complete"
}

install_configuration() {
  preflight_install
  capture_prior_state
  for index in "${!TARGETS[@]}"; do
    install -d -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0755 "$(dirname "${SYSTEMD}/${TARGETS[$index]}")"
    install -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0644 \
      "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${TARGETS[$index]}"
  done
  systemctl daemon-reload
  if [[ ${EXACT_CONFIGURATION} == true ]]; then
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
  install -d -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0700 "${BACKUP}/removed"
  for index in "${!TARGETS[@]}"; do
    target=${TARGETS[$index]}
    if [[ -e ${BACKUP}/absent/${target} ]]; then
      if [[ -e ${SYSTEMD}/${target} ]]; then
        cmp -s "${SOURCE}/${SOURCES[$index]}" "${SYSTEMD}/${target}" || {
          echo "Refusing to undo a configuration file changed since M011.001 install: ${target}" >&2
          return 1
        }
        install -d -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0700 "${BACKUP}/removed/$(dirname "${target}")"
        mv "${SYSTEMD}/${target}" "${BACKUP}/removed/${target}.$(date -u +%Y%m%dT%H%M%SZ)"
      fi
    else
      install -d -o "${INSTALL_OWNER}" -g "${INSTALL_GROUP}" -m 0755 "$(dirname "${SYSTEMD}/${target}")"
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
    preflight_install
    echo "PREFLIGHT PASS: no live configuration changed"
    ;;
  install)
    install_configuration
    ;;
  verify)
    require_tools
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
