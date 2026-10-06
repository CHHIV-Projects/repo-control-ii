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

Current pages and capabilities:

- Dashboard (deterministic workflow status)
- Context (deterministic context generation and rendering)
- Snapshots (list/create immutable snapshots)
- Comparisons (list/create structural comparisons)
- AI Review (run/read local GPT-OSS comparison analysis)
- Workflow (read-only stage/commit plan and execution visibility)

Milestone 009 explicit boundary:

- Browser routes do not stage, commit, push, fetch, or pull.
- M007/M008 mutation execution remains CLI-only in this milestone.

Windows access from a remote workstation can use SSH port forwarding, for example:

```bash
ssh -N -L 8765:127.0.0.1:8765 chuck@192.168.1.173
```

Then open:

```text
http://127.0.0.1:8765
```

Stop the server with Ctrl+C in the terminal running `repoctl web`.

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
