#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
ROOT=/var/lib/repo-control-staging/m010.030-browser-fixture
FIXTURE_REPOSITORY=${ROOT}/repository
FIXTURE_STATE=${ROOT}/state
FIXTURE_SCANNER=${ROOT}/scanner
FIXTURE_PREPARED=${ROOT}/prepared
FIXTURE_SELECTION=${FIXTURE_STATE}/selection-probe
REGISTRY=${ROOT}/repositories.toml
UNIT=repo-control-m010030-fixture.service
SOURCE_REPOSITORY=/home/chuck/projects/repo-control-ii
PYTHON=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
PORT=8876

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this disposable validation fixture with sudo." >&2
  exit 2
fi

probe() {
  systemd-run --quiet --wait --collect --pipe --unit=repo-control-m010030-namespace-probe \
    --uid=chuck --gid=chuck \
    --property=NoNewPrivileges=true \
    --property=PrivateTmp=true \
    --property=ProtectSystem=strict \
    --property=ProtectHome=read-only \
    --property="ReadOnlyPaths=${FIXTURE_REPOSITORY}" \
    --property="ReadWritePaths=${FIXTURE_SELECTION} ${FIXTURE_PREPARED} ${FIXTURE_REPOSITORY}/.git" \
    "${PYTHON}" - "${FIXTURE_SELECTION}" "${FIXTURE_PREPARED}" "${FIXTURE_REPOSITORY}" <<'PY'
import os
from pathlib import Path
import sqlite3
import sys

selection, prepared, repository = map(Path, sys.argv[1:])
scratch = selection / ".selection.json.m010030-scratch"
published = selection / ".selection.json.m010030-published"
with scratch.open("xb") as stream:
    stream.write(b'{"probe":true}\n')
    stream.flush()
    os.fsync(stream.fileno())
scratch.replace(published)
directory = os.open(selection, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
published.unlink()

database = prepared / "probe.sqlite3"
with sqlite3.connect(database) as connection:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE probe(value TEXT NOT NULL)")
    connection.execute("INSERT INTO probe VALUES ('verified')")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("SQLite integrity probe failed")
git_scratch = repository / ".git" / "m010030-write-probe"
git_scratch.write_text("verified\n", encoding="utf-8")
git_scratch.unlink()
try:
    (repository / "unexpected-write").write_text("must fail\n", encoding="utf-8")
except OSError:
    pass
else:
    raise SystemExit("worktree unexpectedly writable")
print("PASS atomic selection, SQLite WAL, narrow Git write, and read-only worktree boundaries")
PY
}

case "${ACTION}" in
  start)
    ! systemctl is-active --quiet "${UNIT}" || { echo "Fixture service is already active." >&2; exit 2; }
    [[ ! -e ${ROOT} ]] || { echo "Disposable fixture already exists: ${ROOT}" >&2; exit 2; }
    install -d -o chuck -g chuck -m 0750 \
      "${ROOT}" "${FIXTURE_REPOSITORY}" "${FIXTURE_STATE}" "${FIXTURE_SCANNER}" \
      "${FIXTURE_PREPARED}" "${FIXTURE_SELECTION}"
    sudo -u chuck git -C "${FIXTURE_REPOSITORY}" init --quiet
    sudo -u chuck git -C "${FIXTURE_REPOSITORY}" config user.name "M010.030 Disposable Product Owner"
    sudo -u chuck git -C "${FIXTURE_REPOSITORY}" config user.email "m010030-fixture@localhost"
    printf 'baseline\n' >"${FIXTURE_REPOSITORY}/browser-proof.txt"
    chown chuck:chuck "${FIXTURE_REPOSITORY}/browser-proof.txt"
    sudo -u chuck git -C "${FIXTURE_REPOSITORY}" add browser-proof.txt
    sudo -u chuck git -C "${FIXTURE_REPOSITORY}" commit --quiet -m "Disposable baseline"
    printf 'browser stage and commit proof\n' >"${FIXTURE_REPOSITORY}/browser-proof.txt"
    chown chuck:chuck "${FIXTURE_REPOSITORY}/browser-proof.txt"
    cat >"${REGISTRY}" <<EOF
default = "fixture"

[repositories.fixture]
name = "M010.030 Disposable Fixture"
path = "${FIXTURE_REPOSITORY}"
milestone_root = "docs/milestones"
EOF
    chown chuck:chuck "${REGISTRY}"
    chmod 0644 "${REGISTRY}"
    probe
    systemd-run --quiet --unit="${UNIT}" --uid=chuck --gid=chuck \
      --property=Type=simple \
      --property="WorkingDirectory=${SOURCE_REPOSITORY}" \
      --property=NoNewPrivileges=true \
      --property=PrivateTmp=true \
      --property=ProtectSystem=strict \
      --property=ProtectHome=read-only \
      --property=UMask=0077 \
      --property="ReadOnlyPaths=${FIXTURE_REPOSITORY}" \
      --property="ReadWritePaths=${FIXTURE_STATE} ${FIXTURE_SCANNER} ${FIXTURE_PREPARED} ${FIXTURE_REPOSITORY}/.git" \
      "${PYTHON}" -m repoctl.cli web --repository-registry "${REGISTRY}" \
      --host 127.0.0.1 --port "${PORT}" --state-root "${FIXTURE_STATE}" \
      --scanner-cache-root "${FIXTURE_SCANNER}"
    for _ in {1..20}; do
      curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null && break
      sleep 0.25
    done
    curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null
    echo "READY disposable browser fixture at http://127.0.0.1:${PORT}/git-review"
    echo "Stage browser-proof.txt, then commit with message: M010.030 disposable browser commit"
    ;;
  verify)
    systemctl is-active --quiet "${UNIT}"
    [[ $(sudo -u chuck git -C "${FIXTURE_REPOSITORY}" rev-list --count HEAD) == 2 ]]
    [[ $(sudo -u chuck git -C "${FIXTURE_REPOSITORY}" log -1 --format=%s) == "M010.030 disposable browser commit" ]]
    [[ -z $(sudo -u chuck git -C "${FIXTURE_REPOSITORY}" status --porcelain=v1) ]]
    [[ $(sudo -u chuck git -C "${FIXTURE_REPOSITORY}" show HEAD:browser-proof.txt) == "browser stage and commit proof" ]]
    echo "PASS disposable browser Stage and Commit; index clean and one local commit verified"
    ;;
  stop)
    systemctl stop "${UNIT}" || true
    systemctl reset-failed "${UNIT}" || true
    echo "STOPPED disposable fixture; evidence remains at ${ROOT}"
    ;;
  *)
    echo "Usage: sudo $0 {start|verify|stop}" >&2
    exit 2
    ;;
esac
