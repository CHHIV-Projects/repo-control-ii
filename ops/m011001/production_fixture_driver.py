#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import subprocess
from unittest import mock
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from repoctl.procedure_governance import (
    _render_artifacts,
    _render_lock,
    inspect_procedure,
    load_bundle,
)
from repoctl.procedure_transactions import (
    TRANSACTION_ROOT_ENV,
    execute_sealed_request,
    inspect_transaction,
)
from repoctl.scanner.util import make_repository_id
from repoctl.web.repository_registry import RepositoryEntry, RepositoryRegistry


KEYS = ("fixture-one", "fixture-two", "fixture-three")
LEGACY_REVISION = "f8c0480^"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(*arguments: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(arguments, cwd=cwd, text=True).strip()


def entry_for(root: Path, key: str) -> RepositoryEntry:
    path = (root / "repositories" / key).resolve()
    return RepositoryEntry(
        key=key,
        name=key,
        path=path,
        repository_id=make_repository_id(path),
        milestone_root="docs/milestones",
        documents={"coding_agent_rules": "docs/context/coding_agent_rules.md"},
    )


def write_file(root: Path, relative: str, content: bytes) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def initialize(root: Path, source: Path) -> None:
    bundle_root = root / "legacy-bundle"
    revision = run("git", "rev-parse", LEGACY_REVISION, cwd=source)
    names = run(
        "git", "ls-tree", "-r", "--name-only", revision, "shared/procedure", cwd=source
    ).splitlines()
    for name in names:
        relative = name.removeprefix("shared/procedure/")
        content = subprocess.check_output(["git", "show", f"{revision}:{name}"], cwd=source)
        write_file(bundle_root, relative, content)
    legacy = load_bundle(bundle_root)
    if legacy.release != "1.4.0":
        raise SystemExit(f"Expected exact 1.4.0 fixture base, found {legacy.release}")

    registry_lines = ['default = "fixture-one"']
    snapshots: dict[str, object] = {"legacy_revision": revision, "repositories": {}}
    for key in KEYS:
        (root / "repositories" / key).mkdir(parents=True)
        entry = entry_for(root, key)
        overlay = entry.path / "docs/context/coding_agent_rules.md"
        overlay.parent.mkdir(parents=True)
        overlay.write_text(
            "<!-- repo-control-project-overlay:v1 -->\n\n"
            "## Repository-specific rules\n\n"
            f"- Disposable {key} rule.\n",
            encoding="utf-8",
        )
        prompt = entry.path / f"docs/milestones/M911.00{KEYS.index(key) + 1}_{key}_prompt.md"
        prompt.parent.mkdir(parents=True)
        prompt.write_text(f"# Disposable prompt for {key}\n", encoding="utf-8")
        artifacts = _render_artifacts(legacy, entry)
        for relative, content in artifacts:
            write_file(entry.path, relative, content)
        write_file(entry.path, legacy.lock_path, _render_lock(legacy, entry, artifacts))
        run("git", "init", "--quiet", cwd=entry.path)
        run("git", "config", "user.name", "M011.001 Disposable Product Owner", cwd=entry.path)
        run("git", "config", "user.email", "m011001-fixture@localhost", cwd=entry.path)
        run("git", "add", ".", cwd=entry.path)
        run("git", "commit", "--quiet", "-m", "Disposable 1.4.0 baseline", cwd=entry.path)
        with mock.patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=entry):
            status = inspect_procedure(entry, load_bundle())
        if status["status"] != "STALE_VERSION" or status["current_release"] != "1.4.0":
            raise SystemExit(f"{key} did not begin as exact 1.4.0 drift: {status}")
        protected = [
            "docs/context/coding_agent_rules.md",
            prompt.relative_to(entry.path).as_posix(),
        ]
        generated = [relative for relative, _ in artifacts] + [legacy.lock_path]
        snapshots["repositories"][key] = {
            "head": run("git", "rev-parse", "HEAD", cwd=entry.path),
            "index_sha256": digest(entry.path / ".git/index"),
            "protected": {name: digest(entry.path / name) for name in protected},
            "legacy": {name: digest(entry.path / name) for name in generated},
        }
        registry_lines.extend(
            [
                "",
                f"[repositories.{key}]",
                f'name = "{key}"',
                f'path = "{entry.path}"',
                'milestone_root = "docs/milestones"',
                "",
                f"[repositories.{key}.documents]",
                'coding_agent_rules = "docs/context/coding_agent_rules.md"',
            ]
        )
    (root / "repositories.toml").write_text("\n".join(registry_lines) + "\n", encoding="utf-8")
    (root / "snapshots.json").write_text(
        json.dumps(snapshots, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print("READY exact three-repository 1.4.0 fixture")


def open_browser() -> object:
    return build_opener(HTTPCookieProcessor(CookieJar()))


def browser_get(browser: object, url: str) -> str:
    with browser.open(url, timeout=15) as response:
        return response.read().decode("utf-8")


def form_fields(markup: str, action: str) -> dict[str, str]:
    match = re.search(
        rf'<form[^>]*method="post"[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>',
        markup,
        re.DOTALL,
    )
    if match is None:
        raise SystemExit(f"Browser page omitted required form {action}")
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', match.group(1)))


def browser_post(browser: object, base: str, action: str, fields: dict[str, str]) -> str:
    request = Request(
        base + action,
        data=urlencode(fields).encode("ascii"),
        method="POST",
    )
    with browser.open(request, timeout=15) as response:
        return response.read().decode("utf-8")


def browser_action(base: str, key: str, action: str) -> None:
    browser = open_browser()
    page = browser_get(browser, f"{base}/procedure-updates?repository={key}")
    if action == "apply":
        review_path = "/procedure-updates/apply-review"
        confirm_path = "/procedure-updates/apply"
    else:
        review_path = "/procedure-updates/recovery-review"
        confirm_path = "/procedure-updates/recover"
    reviewed = browser_post(browser, base, review_path, form_fields(page, review_path))
    confirmed = browser_post(browser, base, confirm_path, form_fields(reviewed, confirm_path))
    if "is sealed" not in confirmed or "no worktree file was changed" not in confirmed:
        raise SystemExit(f"Browser did not seal {action} safely for {key}")
    print(f"PASS browser reviewed and sealed {action} for {key} without worktree mutation")


def browser_switch(base: str, key: str) -> None:
    browser = open_browser()
    page = browser_get(browser, f"{base}/procedure-updates")
    switched = browser_post(
        browser,
        base,
        "/repositories/select",
        {
            **form_fields(page, "/repositories/select"),
            "repository_key": key,
        },
    )
    if (
        f"Active repository changed to {key}." not in switched
        and f"{key} is already the active repository." not in switched
    ):
        raise SystemExit(f"Browser did not switch active repository to {key}")
    print(f"PASS browser active repository switched to {key}")


def fixture_entry(registry_path: Path, key: str) -> RepositoryEntry:
    registry = RepositoryRegistry.from_toml(registry_path)
    return next(entry for entry in registry.entries if entry.key == key)


def transaction_status(registry: Path, key: str, state: Path, transaction_root: Path | None) -> dict:
    if transaction_root is None:
        os.environ.pop(TRANSACTION_ROOT_ENV, None)
    else:
        os.environ[TRANSACTION_ROOT_ENV] = str(transaction_root)
    result = inspect_transaction(fixture_entry(registry, key), state)
    return result or {"state": "NONE"}


def crash_execute(
    registry: Path,
    key: str,
    state: Path,
    transaction_root: Path,
    point: str,
    runtime_health_url: str,
) -> None:
    os.environ[TRANSACTION_ROOT_ENV] = str(transaction_root)
    entry = fixture_entry(registry, key)
    with open_browser().open(runtime_health_url, timeout=5) as response:
        health = json.loads(response.read().decode("utf-8"))
    active_key = health["active_repository"]
    registry_generation = health["active_repository_generation"]
    from repoctl import procedure_transactions

    if point == "prepared":
        original = procedure_transactions._write_journal

        def injected(directory, journal):
            original(directory, journal)
            if journal.get("state") == "PREPARED":
                os._exit(76)

        patcher = mock.patch("repoctl.procedure_transactions._write_journal", side_effect=injected)
    else:
        original = procedure_transactions._write_target
        calls = 0

        def injected(*args, **kwargs):
            nonlocal calls
            original(*args, **kwargs)
            calls += 1
            if calls == 1:
                os._exit(77)

        patcher = mock.patch("repoctl.procedure_transactions._write_target", side_effect=injected)
    with patcher:
        execute_sealed_request(
            entry,
            state,
            active_key=active_key,
            registry_generation=registry_generation,
        )
    raise SystemExit("Crash injection did not fire")


def verify(root: Path, key: str, expected: str) -> None:
    snapshots = json.loads((root / "snapshots.json").read_text(encoding="utf-8"))
    snapshot = snapshots["repositories"][key]
    entry = fixture_entry(root / "repositories.toml", key)
    if run("git", "rev-parse", "HEAD", cwd=entry.path) != snapshot["head"]:
        raise SystemExit(f"{key} HEAD changed")
    if digest(entry.path / ".git/index") != snapshot["index_sha256"]:
        raise SystemExit(f"{key} Git index changed")
    for relative, expected_digest in snapshot["protected"].items():
        if digest(entry.path / relative) != expected_digest:
            raise SystemExit(f"{key} protected path changed: {relative}")
    if expected == "legacy":
        for relative, expected_digest in snapshot["legacy"].items():
            if not (entry.path / relative).is_file() or digest(entry.path / relative) != expected_digest:
                raise SystemExit(f"{key} recovery did not restore exact legacy image: {relative}")
    else:
        with mock.patch("repoctl.workflow.coder_interface.resolve_repository_entry", return_value=entry):
            status = inspect_procedure(entry, load_bundle())
        if status["status"] != "CURRENT" or status["current_release"] != "1.5.0":
            raise SystemExit(f"{key} did not independently verify CURRENT: {status}")
    print(f"PASS {key} {expected}; protected paths and Git HEAD/index unchanged")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize_parser = subparsers.add_parser("initialize")
    initialize_parser.add_argument("--root", type=Path, required=True)
    initialize_parser.add_argument("--source", type=Path, required=True)
    browser_parser = subparsers.add_parser("browser")
    browser_parser.add_argument("--base", required=True)
    browser_parser.add_argument("--key", required=True)
    browser_parser.add_argument("--action", choices=("apply", "recover"), required=True)
    switch_parser = subparsers.add_parser("switch")
    switch_parser.add_argument("--base", required=True)
    switch_parser.add_argument("--key", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--registry", type=Path, required=True)
    status_parser.add_argument("--key", required=True)
    status_parser.add_argument("--state", type=Path, required=True)
    status_parser.add_argument("--transaction-root", type=Path)
    status_parser.add_argument("--expect")
    crash_parser = subparsers.add_parser("crash")
    crash_parser.add_argument("--registry", type=Path, required=True)
    crash_parser.add_argument("--key", required=True)
    crash_parser.add_argument("--state", type=Path, required=True)
    crash_parser.add_argument("--transaction-root", type=Path, required=True)
    crash_parser.add_argument("--point", choices=("prepared", "after-first"), required=True)
    crash_parser.add_argument("--runtime-health-url", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--root", type=Path, required=True)
    verify_parser.add_argument("--key", required=True)
    verify_parser.add_argument("--expect", choices=("legacy", "current"), required=True)
    args = parser.parse_args()
    if args.command == "initialize":
        initialize(args.root.resolve(), args.source.resolve())
    elif args.command == "browser":
        browser_action(args.base.rstrip("/"), args.key, args.action)
    elif args.command == "switch":
        browser_switch(args.base.rstrip("/"), args.key)
    elif args.command == "status":
        result = transaction_status(args.registry, args.key, args.state, args.transaction_root)
        if args.expect and result["state"] != args.expect:
            raise SystemExit(f"Expected {args.expect}, found {result}")
        print(json.dumps(result, sort_keys=True))
    elif args.command == "crash":
        crash_execute(
            args.registry,
            args.key,
            args.state,
            args.transaction_root,
            args.point,
            args.runtime_health_url,
        )
    else:
        verify(args.root, args.key, args.expect)


if __name__ == "__main__":
    main()
