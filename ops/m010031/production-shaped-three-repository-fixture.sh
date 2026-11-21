#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-}
ROOT=/var/lib/repo-control-staging/m010.031-browser-fixture
STATE=${ROOT}/state
SCANNER=${ROOT}/scanner
REGISTRY=${ROOT}/repositories.toml
UNIT=repo-control-m010031-fixture.service
SOURCE_REPOSITORY=/home/chuck/projects/repo-control-ii
PYTHON=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
PORT=8877
KEYS=(fixture-one fixture-two fixture-three)
MILESTONES=(M901.001 M902.001 M903.001)
MESSAGES=(
  "M010.031 fixture-one browser commit"
  "M010.031 fixture-two browser commit"
  "M010.031 fixture-three browser commit"
)

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this disposable validation fixture with sudo." >&2
  exit 2
fi

repository_path() { printf '%s/repositories/%s' "${ROOT}" "$1"; }
repository_id() {
  "${PYTHON}" -c 'from pathlib import Path; import sys; from repoctl.scanner.util import make_repository_id; print(make_repository_id(Path(sys.argv[1])))' "$1"
}

wait_ready() {
  for _ in {1..40}; do
    curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null && break
    sleep 0.25
  done
  curl --fail --silent "http://127.0.0.1:${PORT}/healthz" >/dev/null
  curl --fail --silent "http://127.0.0.1:${PORT}/milestones" >/dev/null
}

browser_probe() {
  FIXTURE_PORT=${PORT} "${PYTHON}" - <<'PY'
from http.cookiejar import CookieJar
import os
import re
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

fixture = f"http://127.0.0.1:{os.environ['FIXTURE_PORT']}"
live = "http://127.0.0.1:8765"
browser = build_opener(HTTPCookieProcessor(CookieJar()))


def get(url: str) -> str:
    with browser.open(url, timeout=10) as response:
        return response.read().decode("utf-8")


def post(path: str, fields: dict[str, str]) -> str:
    request = Request(
        fixture + path,
        data=urlencode(fields).encode("ascii"),
        method="POST",
    )
    with browser.open(request, timeout=10) as response:
        return response.read().decode("utf-8")


def field(markup: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', markup)
    if match is None:
        raise SystemExit(f"Fixture page omitted required field: {name}")
    return match.group(1)


def snapshot() -> tuple[str, str, str]:
    markup = get(fixture + "/milestones")
    return markup, field(markup, "csrf_token"), field(markup, "repository_generation")


def switch_repository(repository_key: str) -> None:
    _, token, generation = snapshot()
    markup = post(
        "/repositories/select",
        {
            "csrf_token": token,
            "repository_key": repository_key,
            "repository_generation": generation,
        },
    )
    if f">{repository_key}<" not in markup:
        raise SystemExit(f"Fixture did not switch to {repository_key}")


def select_milestone(repository_key: str, milestone_id: str) -> None:
    _, token, generation = snapshot()
    markup = post(
        "/milestones/select",
        {
            "csrf_token": token,
            "repository_key": repository_key,
            "repository_generation": generation,
            "milestone_id": milestone_id,
        },
    )
    if f"Selected milestone: {milestone_id}" not in markup:
        raise SystemExit(f"Fixture did not select {milestone_id} for {repository_key}")


# Retain the fixture form/token, visit the live service with the same browser
# cookie jar, then submit the retained fixture form. This reproduces the real
# shared-host, different-port collision that isolated Flask test clients miss.
_, retained_token, retained_generation = snapshot()
get(live + "/milestones")
markup = post(
    "/milestones/select",
    {
        "csrf_token": retained_token,
        "repository_key": "fixture-one",
        "repository_generation": retained_generation,
        "milestone_id": "M901.001",
    },
)
if "Selected milestone: M901.001" not in markup:
    raise SystemExit("Cross-service cookie probe did not preserve fixture-one selection")

switch_repository("fixture-two")
select_milestone("fixture-two", "M902.001")
switch_repository("fixture-three")
select_milestone("fixture-three", "M903.001")
switch_repository("fixture-one")
print("PASS shared-browser cookie isolation and all three sandboxed milestone selections")
PY
}

case ${ACTION} in
  start)
    ! systemctl is-active --quiet "${UNIT}" || { echo "Fixture service is already active." >&2; exit 2; }
    if [[ -e ${ROOT} ]]; then
      [[ ! -e ${ROOT}/.setup-complete ]] || {
        echo "Completed disposable fixture evidence already exists: ${ROOT}" >&2
        exit 2
      }
      failed_root="${ROOT}.failed-$(date -u +%Y%m%dT%H%M%SZ)"
      mv "${ROOT}" "${failed_root}"
      echo "PRESERVED incomplete fixture at ${failed_root}"
    fi
    install -d -o chuck -g chuck -m 0750 "${ROOT}" "${ROOT}/repositories" "${STATE}" "${SCANNER}"
    install -d -o chuck -g chuck -m 0700 "${STATE}/.locks"
    read_write_paths="${STATE}/.locks ${SCANNER}"
    read_only_paths=""
    for index in 0 1 2; do
      key=${KEYS[$index]}
      milestone=${MILESTONES[$index]}
      repository=$(repository_path "${key}")
      install -d -o chuck -g chuck -m 0750 \
        "${repository}" "${repository}/docs" "${repository}/docs/milestones"
      sudo -u chuck git -C "${repository}" init --quiet
      sudo -u chuck git -C "${repository}" config user.name "M010.031 Disposable Product Owner"
      sudo -u chuck git -C "${repository}" config user.email "m010031-fixture@localhost"
      printf '# %s fixture prompt\n' "${key}" >"${repository}/docs/milestones/${milestone}_${key//-/_}_prompt.md"
      printf 'baseline\n' >"${repository}/browser-proof.txt"
      chown -R chuck:chuck "${repository}"
      sudo -u chuck git -C "${repository}" add "docs/milestones/${milestone}_${key//-/_}_prompt.md" browser-proof.txt
      sudo -u chuck git -C "${repository}" commit --quiet -m "Disposable baseline"
      printf '%s browser stage and commit proof\n' "${key}" >"${repository}/browser-proof.txt"
      chown chuck:chuck "${repository}/browser-proof.txt"
      id=$(repository_id "${repository}")
      selection=${STATE}/${id}/milestones
      install -d -o chuck -g chuck -m 0700 "${selection}"
      read_write_paths+=" ${repository}/.git ${selection}"
      read_only_paths+=" ${repository}"
    done
    {
      echo 'default = "fixture-one"'
      for index in 0 1 2; do
        key=${KEYS[$index]}
        echo
        echo "[repositories.${key}]"
        echo "name = \"${key}\""
        echo "path = \"$(repository_path "${key}")\""
        echo 'milestone_root = "docs/milestones"'
      done
    } >"${REGISTRY}"
    chown chuck:chuck "${REGISTRY}"
    chmod 0644 "${REGISTRY}"
    systemd-run --quiet --unit="${UNIT}" --uid=chuck --gid=chuck \
      --property=Type=simple \
      --property="WorkingDirectory=${SOURCE_REPOSITORY}" \
      --property=NoNewPrivileges=true \
      --property=PrivateTmp=true \
      --property=ProtectSystem=strict \
      --property=ProtectHome=read-only \
      --property=UMask=0077 \
      --property="ReadOnlyPaths=${read_only_paths}" \
      --property="ReadWritePaths=${read_write_paths}" \
      "${PYTHON}" -m repoctl.cli web --repository-registry "${REGISTRY}" \
      --host 127.0.0.1 --port "${PORT}" --state-root "${STATE}" \
      --scanner-cache-root "${SCANNER}"
    wait_ready
    browser_probe
    touch "${ROOT}/.setup-complete"
    echo "READY three-repository browser fixture at http://127.0.0.1:${PORT}/milestones"
    for index in 0 1 2; do
      echo "${KEYS[$index]}: select ${MILESTONES[$index]}, then Stage browser-proof.txt and Commit as: ${MESSAGES[$index]}"
    done
    ;;
  verify)
    systemctl is-active --quiet "${UNIT}"
    [[ $(stat -c '%U:%G:%a' "${STATE}/.locks") == chuck:chuck:700 ]]
    curl --fail --silent "http://127.0.0.1:${PORT}/milestones" >/dev/null
    for index in 0 1 2; do
      key=${KEYS[$index]}
      repository=$(repository_path "${key}")
      id=$(repository_id "${repository}")
      selection=${STATE}/${id}/milestones/selection.json
      [[ -f ${selection} ]]
      [[ $("${PYTHON}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["milestone_id"])' "${selection}") == "${MILESTONES[$index]}" ]]
      [[ $(sudo -u chuck git -C "${repository}" rev-list --count HEAD) == 2 ]]
      [[ $(sudo -u chuck git -C "${repository}" log -1 --format=%s) == "${MESSAGES[$index]}" ]]
      [[ -z $(sudo -u chuck git -C "${repository}" status --porcelain=v1) ]]
    done
    echo "PASS all three disposable repository selections and isolated browser Stage/Commit results"
    ;;
  repair-lock)
    systemctl is-active --quiet "${UNIT}"
    [[ $(stat -c '%U:%G' "${STATE}/.locks") == chuck:chuck ]] || {
      echo "Refusing changed fixture lock-directory ownership." >&2
      exit 2
    }
    chmod 0700 "${STATE}/.locks"
    wait_ready
    touch "${ROOT}/.setup-complete"
    echo "PASS disposable fixture lock directory is chuck:chuck 0700; milestone page is ready"
    ;;
  refresh-service)
    systemctl is-active --quiet "${UNIT}"
    systemctl restart "${UNIT}"
    wait_ready
    browser_probe
    echo "PASS disposable fixture restarted on current code and passed browser preflight"
    ;;
  stop)
    systemctl stop "${UNIT}" || true
    systemctl reset-failed "${UNIT}" || true
    echo "STOPPED disposable fixture; evidence remains at ${ROOT}"
    ;;
  *)
    echo "Usage: sudo $0 {start|repair-lock|refresh-service|verify|stop}" >&2
    exit 2
    ;;
esac
