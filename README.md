# Repo Control Plane (Milestone 001)

Repo Control Plane provides a deterministic, read-only repository scanner:

repoctl scan <repository>

Milestone 002 extends scan results with deterministic static relationship analysis for internal Python module dependencies, imported symbols, conservative call relationships, and test-to-symbol static references.

Milestone 003 adds deterministic context-pack generation:

repoctl context "<query>" [--repository <path>]

Milestone 004 adds immutable snapshots and deterministic structural comparison:

repoctl snapshot [--repository <path>]
repoctl compare <before_snapshot_id> <after_snapshot_id> [--repository <path>]

Milestone 005 adds bounded local AI interpretation over immutable comparison evidence:

repoctl analyze <comparison_id> [--repository <path>]

Milestone 006 adds deterministic read-only Git workflow intelligence:

repoctl milestone status [--repository <path>]

Milestone 007 adds a guarded local commit foundation with immutable planning and explicit approval:

repoctl milestone prepare-commit --message "<commit message>" [--repository <path>]
repoctl milestone commit <plan_id> --approve [--repository <path>]

Milestone 008 adds guarded staging preparation with immutable exact path plans and explicit approval:

repoctl milestone prepare-stage --all [--repository <path>]
repoctl milestone stage <plan_id> --approve [--repository <path>]

Milestone 009 adds a local browser UI foundation over existing core services:

repoctl web --repository <path> [--host 127.0.0.1] [--port 8765]

## Development install

```bash
python -m pip install -e .
```

## Usage

```bash
repoctl scan /path/to/git/repo
```

```bash
repoctl context "synonym handling"
repoctl context "get_sheet" --repository /path/to/git/repo
repoctl snapshot --repository /path/to/git/repo
repoctl compare snap--before snap--after --repository /path/to/git/repo
repoctl analyze cmp--abcdef0123456789 --repository /path/to/git/repo
repoctl milestone status --repository /path/to/git/repo
repoctl milestone prepare-commit --repository /path/to/git/repo --message "Complete Milestone 007"
repoctl milestone commit commit-plan--abcdef0123456789 --approve --repository /path/to/git/repo
repoctl milestone prepare-stage --repository /path/to/git/repo --all
repoctl milestone stage stage-plan--abcdef0123456789 --approve --repository /path/to/git/repo
repoctl web --repository /path/to/git/repo
```

## Browser UI (Milestone 009)

Start the local server:

```bash
repoctl web --repository /path/to/git/repo
```

Default bind behavior is loopback-only (`127.0.0.1`) in Milestone 009.
Non-loopback host binding is blocked.

Default browser URL:

```text
http://127.0.0.1:8765
```

Current primary pages and capabilities:

- Dashboard (deterministic workflow status)
- Git Review (current changes, diagnosis/recovery previews, history, and branches)
- Git Review provides explicitly selected batch stage, unstage, and restore actions (up to 256 paths) plus staged-set local commit, through preview, confirmation, fresh-state revalidation, and post-action verification.
- The Branches view provides confirmed local branch create/switch/safe-delete, configured-remote fetch/push, first-push upstream setup, and fast-forward-only update. Remote operations are bounded, non-interactive, and use configured remotes only; Push is non-force and verifies the actual remote branch tip after success.
- Workflow remains available as a read-only historical artifact view, but is no longer in primary navigation; its legacy snapshot/stage/commit POST actions are disabled.

Browser Git mutation boundary:

- GET requests are read-only. Mutations require a CSRF-protected POST, a short-lived single-use confirmation token, and fresh Git-state validation.
- Batch stage/unstage/restore use only the explicit selected path set; an action is unavailable unless every selected path supports it. Commit includes the complete reviewed staged set and never stages implicitly.
- Branch and remote actions are explicitly prepared and confirmed, then revalidated. Switch and fast-forward require a clean worktree and no in-progress Git operation; local deletion requires a non-current, non-main branch proven merged into local `main`. Fetch does not move the current branch. Push publishes committed history only, never uses force, and distinguishes command success from remote-tip verification.
- Fetch/publish remote selection prefers `origin`; without `origin`, a sole configured remote is selected automatically, while multiple remotes require explicit selection from the server-observed inventory. Remote URLs are sanitized for display.
- Merge workflows and conflict resolution, rebase/interactive rebase, cherry-pick, reset, stash, reflog recovery, worktrees, submodules, force push/force-with-lease, remote branch deletion, tag management, arbitrary refs/refspecs, general pull, history rewriting, GitHub PR/CI control, and remote configuration editing remain out of scope; use manual Git for these cases.

Windows access from a remote workstation can use SSH port forwarding, for example:

```bash
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 chuck@henderson-server1
```

Then open:

```text
http://127.0.0.1:8765
```

Stop the server with Ctrl+C in the terminal running `repoctl web`.

## Always-on service and repository selection

The tracked systemd unit is `ops/systemd/repo-control.service`. It runs the
project virtual-environment executable as user `chuck` and binds only to
`127.0.0.1:8765`. The unit starts the web app in server-side registry mode:

```bash
repoctl web --repository-registry /home/chuck/projects/repo-control-ii/ops/repositories.toml
```

`ops/repositories.toml` is the server-controlled allowlist. Each
`[repositories.<key>]` entry has a human-readable `name` and an absolute Git
worktree `path`; `default` names the repository selected at service startup.
Add repositories by editing this file and restarting the service. All entries
are validated at startup; there is no filesystem discovery, and the browser can
select only configured keys, never submit an arbitrary path. A repository
selection change is service-wide, performs no Git mutation, and invalidates all
pending confirmations. The configured `default` is selected again after a
service restart.

The service definition can be installed and started by the Product Owner with:

```bash
sudo install -m 0644 /home/chuck/projects/repo-control-ii/ops/systemd/repo-control.service /etc/systemd/system/repo-control.service
sudo systemctl daemon-reload
sudo systemctl enable --now repo-control.service
systemctl status repo-control.service
journalctl -u repo-control.service --no-pager -n 100
```

From Windows, copy `ops/windows/RepoControlLauncher/` anywhere and double-click
`RepoControl.cmd`. The launcher checks the local service health endpoint,
reuses a working Repo Control tunnel, or starts an SSH loopback forward using
the normal `chuck@henderson-server1` SSH configuration. It opens
`http://127.0.0.1:8765/` and does not store credentials or disable host-key
checking. If local port 8765 belongs to another service, it fails instead of
creating a duplicate listener. The package README describes the optional
per-user Task Scheduler logon task for tunnel-only startup.
SSH runs without a visible console window; the logon task uses a small
Windows Script Host wrapper to create PowerShell hidden from the outset.
Optional logon startup requires Windows Script Host/VBScript enabled;
on-demand use does not. Hidden startup requires normal non-interactive SSH authentication
and an already verified host key. Failures return a nonzero exit code and
write local diagnostics under `%LOCALAPPDATA%\RepoControlLauncher`; no
authentication or host-key checks are disabled. Re-run the task installer
after updating the package to apply the console-free entry point.

For service operation:

```bash
sudo systemctl restart repo-control.service
sudo systemctl stop repo-control.service
sudo systemctl disable repo-control.service
```

To remove the installed unit during rollback:

```bash
sudo systemctl stop repo-control.service
sudo systemctl disable repo-control.service
sudo rm /etc/systemd/system/repo-control.service
sudo systemctl daemon-reload
```

Restarting the service clears in-memory prepared actions. Avoid simultaneous
Git mutations through Repo Control and another Git client or terminal.

## External state location

Default root:

~/.local/share/repoctl/

Outputs are written into a deterministic repository-specific directory and include:

- repository.json
- files.json
- symbols.json
- tests.json
- dependencies.json
- summary.md

Context outputs are written under:

~/.local/share/repoctl/<repository_id>/contexts/<context_id>/

with:

- context.json
- context.md

Snapshots are written under:

~/.local/share/repoctl/<repository_id>/snapshots/<snapshot_id>/

Comparisons are written under:

~/.local/share/repoctl/<repository_id>/comparisons/<comparison_id>/

Analyses are written under:

~/.local/share/repoctl/<repository_id>/analyses/<comparison_id>/<analysis_id>/

with:

- analysis_input.json
- analysis.json
- analysis.md

Snapshots are content-derived and immutable. Running `repoctl snapshot` twice against identical deterministic scan evidence reuses the same snapshot ID.
Comparisons are directional and operate on named snapshots, not the repository's current working state.
Snapshot structural scope remains tracked files; if untracked worktree entries exist, completeness is explicitly reported as partial rather than implying full worktree structural analysis.
Analysis operates only on an existing immutable comparison, sends bounded structural metadata to local Ollama (`gpt-oss:20b`), and keeps deterministic comparison evidence authoritative.
AI output is advisory, immutable, external to target repositories, and does not perform Git writes.
There is no cloud fallback.

Workflow status output is written under:

~/.local/share/repoctl/<repository_id>/workflow/

with:

- status.json
- status.md

Workflow status artifacts are current-state projections and may be replaced by later status runs.
Milestone 006 performs no Git mutation, no AI analysis, and no snapshot/comparison creation.

Milestone 007 commit plans are written under:

~/.local/share/repoctl/<repository_id>/workflow/commit_plans/<plan_id>/

with:

- plan.json
- plan.md

Milestone 007 commit execution evidence is written under:

~/.local/share/repoctl/<repository_id>/workflow/commit_executions/<execution_id>/

with:

- execution.json
- execution.md

Milestone 008 stage plans are written under:

~/.local/share/repoctl/<repository_id>/workflow/stage_plans/<plan_id>/

with:

- plan.json
- plan.md

Milestone 008 stage execution evidence is written under:

~/.local/share/repoctl/<repository_id>/workflow/stage_executions/<execution_id>/

with:

- execution.json
- execution.md

Guarded write constraints in Milestone 007:

- `prepare-commit` is read-only toward the target repository.
- `commit` requires an immutable `plan_id` plus explicit `--approve`.
- Staged fingerprint and repository state are revalidated immediately before mutation.
- No fetch, pull, push, merge, rebase, amend, or auto-staging occurs.
- No AI component authorizes or rewrites commit decisions/messages.
- Custom hooks environments or executable commit hooks are blocked (`unsupported_git_hooks`).

Guarded staging constraints in Milestone 008:

- `prepare-stage --all` is read-only toward the target repository.
- Starting index must be clean; existing staged changes block planning.
- Candidate enumeration is deterministic and Git-derived; ignored files are excluded.
- Custom Git filter drivers are blocked (`unsupported_git_filters`).
- Execution stages only the exact reviewed path set from the immutable plan.
- No uncontrolled recomputed `git add .` or `git add -A` is used.
- No commit, fetch, pull, or push occurs in the staging path.

Milestone 006 `workflow_state` enum values are:

- clean
- staged_only
- unstaged_only
- staged_and_unstaged
- conflicted

Milestone 006 distinguishes staged, unstaged, untracked, and unmerged collections with deterministic path ordering.
Detached HEAD is represented explicitly as `branch.state = detached` with `branch.name = null`.
Active Git operations are reported as deterministic names only: merge, rebase, cherry_pick, revert, bisect.

Upstream divergence is local-ref based only. No Git fetch is performed.
Ahead/behind values describe locally available refs and may be stale versus the remote server.

Context packs are lexical and deterministic (no AI, embeddings, or semantic search), use a fixed seed-plus-one-hop selection strategy, and enforce fixed bounds for seeds, files, symbols, relationships, and test references.
They are navigation evidence, not source-code authority.

## Read-only target guarantee

Milestone 001 only performs read-only filesystem and Git inspection of the target repository.
It does not write, stage, commit, switch branches, or otherwise mutate the target repository.

## Current limitations

- No call graph or test-to-symbol mapping.
- No dependency resolution.
- No Git write operations.
- No architectural or risk scoring.

Repo Control Plane remains read-only toward target repositories in these milestones. It does not stage, commit, push, switch branches, or otherwise perform Git writes against the target.
