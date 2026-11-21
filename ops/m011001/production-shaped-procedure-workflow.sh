#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
ROOT=/var/lib/repo-control-staging/m011.001-procedure-fixture
STATE=${ROOT}/state
TRANSACTIONS=${ROOT}/procedure-transactions
SCANNER=${ROOT}/scanner
REGISTRY=${ROOT}/repositories.toml
SOURCE=/home/chuck/projects/repo-control-ii
PYTHON=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
DRIVER=${SOURCE}/ops/m011001/production_fixture_driver.py
UNIT=repo-control-m011001-procedure-fixture.service
PORT=8878
KEYS=(fixture-one fixture-two fixture-three)
EXECUTION_COUNTER=0

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this disposable production-shaped fixture with sudo." >&2
  exit 2
fi

repository_path() { printf '%s/repositories/%s' "${ROOT}" "$1"; }

stop_browser() {
  systemctl stop "${UNIT}" >/dev/null 2>&1 || true
  systemctl reset-failed "${UNIT}" >/dev/null 2>&1 || true
}

wait_ready() {
  for _ in {1..60}; do
    if curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
      return
    fi
    sleep 0.25
  done
  curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null
}

browser_action() {
  runuser -u chuck -- "${PYTHON}" "${DRIVER}" browser \
    --base "http://127.0.0.1:${PORT}" --key "$1" --action "$2"
}

transaction_status() {
  runuser -u chuck -- "${PYTHON}" "${DRIVER}" status \
    --registry "${REGISTRY}" --key "$1" --state "${STATE}" \
    --transaction-root "${TRANSACTIONS}" --expect "$2"
}

verify_repository() {
  runuser -u chuck -- "${PYTHON}" "${DRIVER}" verify \
    --root "${ROOT}" --key "$1" --expect "$2"
}

executor() {
  local key=$1
  local mode=$2
  local repository
  local unit
  repository=$(repository_path "${key}")
  EXECUTION_COUNTER=$((EXECUTION_COUNTER + 1))
  unit="repo-control-m011001-${key}-${mode}-${EXECUTION_COUNTER}"
  common=(
    --quiet --wait --collect --pipe --unit="${unit}"
    --uid=chuck --gid=chuck
    --property=Type=oneshot
    --property="WorkingDirectory=${SOURCE}"
    --property=NoNewPrivileges=true
    --property=PrivateTmp=true
    --property=ProtectSystem=strict
    --property=ProtectHome=read-only
    --property=UMask=0077
    --property="ReadWritePaths=${TRANSACTIONS}"
    --property="ReadWritePaths=${STATE}/.locks"
    --property="ReadWritePaths=${repository}"
    --property="ReadOnlyPaths=${repository}/.git"
    --property="ReadOnlyPaths=${repository}/docs/context"
    --property="ReadOnlyPaths=${repository}/docs/milestones"
    --property="BindReadOnlyPaths=${REGISTRY}:${SOURCE}/ops/repositories.toml"
  )
  if [[ ${mode} == normal ]]; then
    systemd-run "${common[@]}" "${PYTHON}" -m repoctl.cli procedure-execute-request \
      --repository-key "${key}" --repository-registry "${REGISTRY}" \
      --state-root "${STATE}" --transaction-root "${TRANSACTIONS}" \
      --runtime-health-url "http://127.0.0.1:${PORT}/healthz"
  else
    systemd-run "${common[@]}" "${PYTHON}" "${DRIVER}" crash \
      --registry "${REGISTRY}" --key "${key}" --state "${STATE}" \
      --transaction-root "${TRANSACTIONS}" --point "${mode}" \
      --runtime-health-url "http://127.0.0.1:${PORT}/healthz"
  fi
}

run_fixture() {
  stop_browser
  if [[ -e ${ROOT} ]]; then
    preserved="${ROOT}.preserved-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "${ROOT}" "${preserved}"
    echo "PRESERVED prior disposable evidence at ${preserved}"
  fi
  install -d -o root -g chuck -m 0750 "${ROOT}"
  install -d -o chuck -g chuck -m 0750 \
    "${ROOT}/repositories" "${ROOT}/legacy-bundle" "${SCANNER}"
  install -d -o root -g chuck -m 0750 "${STATE}"
  install -d -o chuck -g chuck -m 0700 "${STATE}/.locks" "${TRANSACTIONS}"
  install -o chuck -g chuck -m 0600 /dev/null "${REGISTRY}"
  install -o chuck -g chuck -m 0600 /dev/null "${ROOT}/snapshots.json"
  runuser -u chuck -- "${PYTHON}" "${DRIVER}" initialize --root "${ROOT}" --source "${SOURCE}"
  chown root:chuck "${REGISTRY}" "${ROOT}/snapshots.json"
  chmod 0640 "${REGISTRY}" "${ROOT}/snapshots.json"

  fallback=$(runuser -u chuck -- "${PYTHON}" "${DRIVER}" status \
    --registry "${REGISTRY}" --key fixture-one --state "${STATE}" --expect STORAGE_UNAVAILABLE)
  echo "${fallback}"
  echo "PASS root-owned managed parent is classified STORAGE_UNAVAILABLE, not journal corruption"

  readonly_paths=()
  for key in "${KEYS[@]}"; do
    readonly_paths+=(--property="ReadOnlyPaths=$(repository_path "${key}")")
  done
  systemd-run --quiet --unit="${UNIT}" --uid=chuck --gid=chuck \
    --property=Type=simple \
    --property="WorkingDirectory=${SOURCE}" \
    --property=NoNewPrivileges=true \
    --property=PrivateTmp=true \
    --property=ProtectSystem=strict \
    --property=ProtectHome=read-only \
    --property=UMask=0077 \
    --property="ReadWritePaths=${TRANSACTIONS}" \
    --property="ReadWritePaths=${STATE}/.locks" \
    --property="ReadWritePaths=${SCANNER}" \
    --property="BindReadOnlyPaths=${REGISTRY}:${SOURCE}/ops/repositories.toml" \
    --setenv="REPOCTL_PROCEDURE_TRANSACTION_ROOT=${TRANSACTIONS}" \
    "${readonly_paths[@]}" \
    "${PYTHON}" -m repoctl.cli web --repository-registry "${REGISTRY}" \
    --host 127.0.0.1 --port "${PORT}" --state-root "${STATE}" \
    --scanner-cache-root "${SCANNER}"
  trap stop_browser EXIT
  wait_ready

  if systemd-run --quiet --wait --collect --pipe --unit=repo-control-m011001-readonly-probe \
    --uid=chuck --gid=chuck --property=ProtectSystem=strict --property=ProtectHome=read-only \
    --property="ReadOnlyPaths=$(repository_path fixture-one)" \
    /usr/bin/touch "$(repository_path fixture-one)/sandbox-write-must-fail"; then
    echo "Browser-shaped sandbox unexpectedly wrote the worktree." >&2
    exit 3
  fi
  [[ ! -e $(repository_path fixture-one)/sandbox-write-must-fail ]]
  systemd-run --quiet --wait --collect --pipe --unit=repo-control-m011001-store-probe \
    --uid=chuck --gid=chuck --property=ProtectSystem=strict --property=ProtectHome=read-only \
    --property="ReadWritePaths=${TRANSACTIONS}" \
    /usr/bin/env FIXTURE_STORE="${TRANSACTIONS}" "${PYTHON}" -c \
    'import os; from pathlib import Path; p=Path(os.environ["FIXTURE_STORE"])/".probe"; p.write_text("ok"); p.unlink()'
  echo "PASS effective browser sandbox keeps worktrees read-only and permits only the private journal store"

  browser_action fixture-one apply
  transaction_status fixture-one EXECUTION_PENDING
  verify_repository fixture-one legacy
  runuser -u chuck -- "${PYTHON}" "${DRIVER}" switch \
    --base "http://127.0.0.1:${PORT}" --key fixture-two
  if executor fixture-one normal; then
    echo "Executor accepted a stale browser repository selection." >&2
    exit 3
  fi
  transaction_status fixture-one NONE
  verify_repository fixture-one legacy
  runuser -u chuck -- "${PYTHON}" "${DRIVER}" switch \
    --base "http://127.0.0.1:${PORT}" --key fixture-one
  browser_action fixture-one apply
  executor fixture-one normal
  transaction_status fixture-one VERIFIED
  verify_repository fixture-one current

  browser_action fixture-two apply
  transaction_status fixture-two EXECUTION_PENDING
  if executor fixture-two prepared; then
    echo "Prepared-state crash injection did not stop the executor." >&2
    exit 3
  fi
  transaction_status fixture-two PREPARED
  browser_action fixture-two recover
  transaction_status fixture-two EXECUTION_PENDING
  executor fixture-two normal
  transaction_status fixture-two RECOVERED
  verify_repository fixture-two legacy
  browser_action fixture-two apply
  executor fixture-two normal
  verify_repository fixture-two current

  browser_action fixture-three apply
  transaction_status fixture-three EXECUTION_PENDING
  if executor fixture-three after-first; then
    echo "Post-first-target crash injection did not stop the executor." >&2
    exit 3
  fi
  transaction_status fixture-three APPLYING
  browser_action fixture-three recover
  transaction_status fixture-three EXECUTION_PENDING
  executor fixture-three normal
  transaction_status fixture-three RECOVERED
  verify_repository fixture-three legacy
  browser_action fixture-three apply
  executor fixture-three normal
  verify_repository fixture-three current

  for key in "${KEYS[@]}"; do
    verify_repository "${key}" current
  done
  systemctl show "${UNIT}" -p User -p Group -p ProtectSystem -p ProtectHome \
    -p ReadWritePaths -p ReadOnlyPaths -p BindReadOnlyPaths >"${ROOT}/effective-browser-sandbox.txt"
  chown root:chuck "${ROOT}/effective-browser-sandbox.txt"
  chmod 0640 "${ROOT}/effective-browser-sandbox.txt"
  stop_browser
  trap - EXIT
  touch "${ROOT}/.complete"
  echo "PASS complete three-repository browser Review/Confirm, sealed one-shot Apply, two interruption/recovery points, independent CURRENT verification, isolation, protected paths, and Git HEAD/index boundaries"
  echo "EVIDENCE preserved at ${ROOT}"
}

case ${ACTION} in
  run)
    run_fixture
    ;;
  stop)
    stop_browser
    echo "STOPPED disposable M011.001 browser service; fixture evidence was preserved"
    ;;
  *)
    echo "Usage: sudo $0 {run|stop}" >&2
    exit 2
    ;;
esac
