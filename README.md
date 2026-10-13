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
- Milestone (recursively discover prompt artifacts beneath each repository's server-configured milestone root; explicitly select a prompt; record lifecycle transitions and meaningful decisions in a tracked, human-readable per-milestone workflow sidecar; and generate bounded Coder and Architect Review handoff packets from configured project documents, milestone sources, workflow history, and current local Git evidence)
- Git Review provides explicitly selected batch stage, unstage, and restore actions (up to 256 paths) plus staged-set local commit, through preview, confirmation, fresh-state revalidation, and post-action verification.
- The Branches view provides confirmed local branch create/switch/safe-delete, configured-remote fetch/push, first-push upstream setup, and fast-forward-only update. Remote operations are bounded, non-interactive, and use configured remotes only; Push is non-force and verifies the actual remote branch tip after success.
- Workflow remains available as a read-only historical artifact view, but is no longer in primary navigation; its legacy snapshot/stage/commit POST actions are disabled.

In repository-registry mode, each `[repositories.<key>]` entry may set a
repository-relative `milestone_root` (for example, `docs/milestones` or
`milestones`). If omitted, it defaults to `docs/milestones`. The browser cannot
override this path. Discovery recurses only beneath that root.

Repositories may also configure repository-relative governing document roles
under `[repositories.<key>.documents]`. Supported roles are `context`,
`architecture`, `workflow`, `coding_agent_rules`, and `parking_lot`. Paths are
validated at registry load and rechecked when used; the browser cannot supply
document paths. Coder packets require configured workflow and coding-agent
rules. Architect packets include configured context, architecture, and
workflow documents where available. Missing required or unsafe sources are
reported rather than substituted or silently truncated.

Milestone lifecycle phases are reconstructed from the chronological Markdown
workflow sidecar beside the selected prompt. The sidecar is distinct from the
prompt, closeout, and external active-selection pointer. Selection, artifact
presence, Git cleanliness, and test results never automatically approve or
close a milestone. Product Owner approvals require explicit UI confirmation;
Repo Control does not authenticate the actor.

Handoff packets are generated on demand as read-only derived views. They
include source references, lifecycle/authorization context, and bounded local
Git evidence where appropriate; test outcomes remain labeled as reported
unless independently verified. Packets are not saved as project artifacts and
generation does not change milestone lifecycle, selection, or Git state.

## Architect read-only API (M003.004)

Repo Control exposes a versioned, provider-neutral, GET-only JSON contract for
an Architect client to retrieve current repository evidence without copying a
large handoff packet. The active repository and selected milestone come only
from the server registry and external milestone-selection pointer; callers
cannot supply repository or filesystem paths.

The API is available beneath `/api/architect/v1`:

| Endpoint | Result |
|---|---|
| `GET /status` | Active repository, registry generation, current selection, and lifecycle summary when selected. |
| `GET /milestone` | Selected milestone identity, phase, waiting state, closeout/workflow status, binding lock-in, open conditions, and next authorized action. |
| `GET /prompt` | Full selected authoritative prompt with source metadata. |
| `GET /workflow` | Bounded recent event history, binding lock-ins, and unresolved conditions. |
| `GET /closeout` | Closeout status and full bounded content when present. A missing closeout required by REVIEW is returned as an explicit contract state. |
| `GET /git` | Deterministic local branch, HEAD, upstream, and bounded working-tree summary. |
| `GET /evidence` | Bounded recorded test-result events, labeled as reported rather than independently verified. |
| `GET /documents` | Configured governing-document roles and repository-relative paths with presence, safety, and size status. |
| `GET /documents/{role}` | Full bounded content for one configured role; roles are allowlisted by the registry. |
| `GET /review` | Bounded aggregate of milestone, workflow, Git, evidence, and configured-document metadata; large source content is not embedded. |

Example local queries:

```bash
curl -fsS http://127.0.0.1:8765/api/architect/v1/status
curl -fsS 'http://127.0.0.1:8765/api/architect/v1/prompt?expected_repository_key=repo-control-ii&expected_generation=0&expected_milestone_id=M003.004'
curl -fsS http://127.0.0.1:8765/api/architect/v1/documents/architecture
curl -fsS http://127.0.0.1:8765/api/architect/v1/review
```

Responses use `application/json`, include `contract_version`, server-resolved
repository and selection identity, source labels, and explicit null/status
values. Follow-up requests may provide `expected_repository_key`,
`expected_generation`, and `expected_milestone_id`; mismatches return HTTP 409
with a stable JSON error object. The registry generation is process-local and
increments when the active repository changes; it is not a durable registry
revision. Other errors use JSON `{ "error": { "code": "...", "message": "..." } }`
with an appropriate 4xx/5xx status. Responses are not cached.

Each Markdown source is limited to 128 KB, workflow sidecars to 5 MB,
event summaries to 30 events, Git paths to 40 per category, and each JSON
response to 512 KB. Oversized authoritative content is rejected explicitly,
never silently truncated. The aggregate omits prompt and governing-document
contents by default. Git state uses local tracking refs only; API queries do
not fetch, contact remotes, or run tests.

## Architect Secure MCP Tunnel (M003.006)

Repo Control provides a read-only Model Context Protocol (MCP) stdio adapter that integrates with OpenAI's `tunnel-client` daemon, enabling ChatGPT (as Architect) to query Repo Control securely without public ingress or port forwarding.

Repo Control remains strictly bound to `127.0.0.1:8765`. The adapter runs locally over stdio and maps MCP tool calls strictly to loopback HTTP GET endpoints:

| Tool | Mapped Endpoint | Purpose |
|---|---|---|
| `repo_status` | `GET /api/architect/v1/status` | Read-only overview of active repository and milestone status |
| `repo_milestone` | `GET /api/architect/v1/milestone` | Milestone lifecycle, phase, prompt, and closeout status |
| `repo_prompt` | `GET /api/architect/v1/prompt` | Active milestone prompt text and metadata |
| `repo_workflow` | `GET /api/architect/v1/workflow` | Chronological workflow lifecycle events and open conditions |
| `repo_closeout` | `GET /api/architect/v1/closeout` | Milestone closeout report content or status |
| `repo_git` | `GET /api/architect/v1/git` | Read-only Git branch, HEAD, and working-tree status |
| `repo_evidence` | `GET /api/architect/v1/evidence` | Recorded test results and binding verification items |
| `repo_documents` | `GET /api/architect/v1/documents` | List registered governing documents |
| `repo_document` | `GET /api/architect/v1/documents/{role}` | Read a specific governing document (`context`, `architecture`, `workflow`, `coding_agent_rules`, `parking_lot`) |
| `repo_review` | `GET /api/architect/v1/review` | Bounded review summary combining status, git, and lifecycle |

### Running the Adapter

```bash
repoctl mcp-architect [--repo-control-url http://127.0.0.1:8765]
```

### Systemd Service Setup

A systemd unit is provided at `ops/systemd/repo-control-architect-tunnel.service`.
To configure and enable it:

1. Copy the example environment file outside the repository:
   ```bash
   mkdir -p ~/.config/repo-control
   cp ops/systemd/architect-tunnel.env.example ~/.config/repo-control/tunnel.env
   chmod 600 ~/.config/repo-control/tunnel.env
   ```
2. Populate `CONTROL_PLANE_API_KEY` (restricted runtime key with Tunnels Read+Use permissions) and `CONTROL_PLANE_TUNNEL_ID` in `~/.config/repo-control/tunnel.env`.
3. Install and start the service:
   ```bash
   sudo cp ops/systemd/repo-control-architect-tunnel.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now repo-control-architect-tunnel.service
   ```

### Operator Guidance and Health Verification

The persistent tunnel enables ChatGPT to retrieve authoritative repository and milestone evidence directly without manual copy/paste.

**Health Verification Commands:**

```bash
# Verify Repo Control service is active on loopback
systemctl status repo-control.service
curl -fsS http://127.0.0.1:8765/api/architect/v1/status

# Verify Secure MCP Tunnel service is active
systemctl status repo-control-architect-tunnel.service
curl -fsS http://127.0.0.1:8080/healthz

# Inspect tunnel client service logs
journalctl -u repo-control-architect-tunnel.service -n 50 --no-pager
```

**Standard Workflow Pattern:**
1. **Planning & Prompt Review:** Architect invokes `repo_status`, `repo_prompt`, and `repo_document` to inspect active state and governing context.
2. **Implementation Monitoring:** Product Owner marks milestone `READY` then `IN_PROGRESS` in Repo Control UI. Coder works under strict read-only Git discipline.
3. **Closeout Review:** When implementation finishes and closeout is written, Architect invokes `repo_review`, `repo_milestone`, `repo_closeout`, and `repo_git` to assess completion and Git cleanliness.
4. **Acceptance & Git Action:** Architect advises the Product Owner. Product Owner reviews, performs Git staging/commit/push via Repo Control UI, and closes the milestone (`REVIEW` → `CLOSED`).

**Fallback Procedure:**
If the MCP tunnel is interrupted, degraded, or offline:
1. Open Repo Control UI at `http://127.0.0.1:8765`.
2. Navigate to the **Milestone** page for the active milestone.
3. Click **Generate Architect Review Packet**.
4. Copy and paste the resulting bounded text packet directly into ChatGPT.
5. All milestone data and Git evidence remain fully available for review.


Every Architect route is GET-only and read-only. GET access requires no CSRF
token because these routes do not mutate state; cross-origin browser reads
remain subject to the browser same-origin policy, and the API does not enable
CORS. This is not independent caller authentication: the service trusts its
existing loopback boundary and the accepted SSH local-tunnel path. Keep the
systemd service bound to `127.0.0.1:8765`; do not expose it to a LAN or public
network. Local processes/users able to reach that loopback service or
authorized tunnel can read the selected project's exposed evidence. No
authenticated role identity is asserted.

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

The explicitly selected milestone prompt pointer is stored separately under:

~/.local/share/repoctl/<repository_id>/milestones/selection.json

It is an operational pointer only; the tracked prompt and related milestone
artifacts remain authoritative. Milestone selection is repository-scoped and
is preserved when scanner artifacts are refreshed.

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
