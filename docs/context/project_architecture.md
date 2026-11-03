# PROJECT_ARCHITECTURE.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Durable system architecture, authority boundaries, integration model, and architectural invariants  
**Project phase:** Git Control complete; accepted and CLOSED through M009.010, including M009.008 context-verification audit, M009.009 Intelligence freshness/provenance, and M009.010 runtime compatibility and procedure-release safeguards; see milestone artifacts and Repo Control for individual lifecycle authority
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`
**Current architecture baseline:** Repo Control II is a Linux-hosted, loopback-only browser Git Control service with guarded Product Owner Git actions, a managed multi-repository registry, tracked milestone sidecars and a six-state lifecycle, read-only Architect MCP evidence with separately PO-approved dispatch transport, and local Coder context/Skills with strict role and Git boundaries. It also provides bounded Repository Intelligence and a versioned shared procedure; accepted M008 capabilities add scanner-surviving dispatch-ledger durability and PO-confirmed, single-repository transactional procedure Apply/Recover. M009.008–M009.010 add bounded context-verification audit semantics, semantic graph freshness/provenance, and a narrow runtime compatibility check plus authorized release preparation.
**Current architectural emphasis:** Preserve read-only Architect evidence semantics while routing typed proposal submissions through pending Repo Control dispatches, explicit Product Owner approval, and local Coder CLI consumption; preserve deterministic Git authority, strict role separation, exact-image recovery boundaries, and candid disclosure of non-cooperating-writer limits.

---

# 1. Architecture Purpose

This document defines the durable architecture of Repo Control II.

It focuses on:

```text
what owns truth
what may mutate state
what may only inspect state
what the Product Owner controls
how Architect and Coder roles remain distinct
how milestone state is represented
how iterative milestone exchanges are preserved
how Git state is surfaced and controlled
how repositories are selected safely
how external tools are integrated
how AI receives bounded trusted context
what Repo Control should own
what Repo Control should not reimplement
```

`project_context.md` describes current project/product state and near-term direction.

This document describes the intended system structure and architectural boundaries that should remain stable even as implementations change.

---

# 2. Architecture North Star

Repo Control II is a deterministic project-control layer between the Product Owner, Architect, coding agents, repositories, Git, project documents, tests, and selected external tools.

Conceptually:

```text
                         Product Owner
                              │
                      direction / approval
                              │
                 ┌────────────┴────────────┐
                 │                         │
                 ▼                         ▼
        ChatGPT / Architect          VS Code / Coder
                 │                         │
                 │ controlled tools       │ active milestone,
                 │ and bounded evidence   │ skills, repo evidence
                 │                         │
                 └────────────┬────────────┘
                              ▼
                       Repo Control II
                              │
          ┌───────────────────┼────────────────────┐
          │                   │                    │
          ▼                   ▼                    ▼
        Git            Workflow State       Project Artifacts
          │                   │                    │
          ├──────────┐        │                    │
          │          │        │                    │
          ▼          ▼        ▼                    ▼
      GitHub     Local Tree   Tests       Repository Intelligence
                                             │
                                             ▼
                                      Replaceable engine
```

The architectural principle is:

```text
Repo Control owns workflow state and integration.

Specialized tools own specialized capabilities.

Deterministic systems establish facts.

AI interprets trusted facts and performs bounded reasoning or implementation.
```

---

# 3. Architectural Goals

Repo Control II architecture should optimize for three primary outcomes.

## 3.1 Git Control

Provide the Product Owner with understandable, graphical, trustworthy Git control without requiring memorization of exact command syntax.

The initial Git Control arc is complete and operationally accepted.

## 3.2 Workflow Integration

Reduce manual transfer of prompts, questions, clarifications, coder output, test evidence, and repository state between the Architect and Coder.

The accepted M003 baseline implements bounded Architect evidence/dispatch transport and local Coder workflow access, while retaining Product Owner authority.

## 3.3 Context Efficiency

Ensure AI systems receive:

```text
the right project rules
+
the current milestone delta
+
the current repository facts
+
only the evidence needed for the task
```

rather than repeatedly ingesting broad project history.

---

# 4. Core Architecture Principles

## 4.1 Deterministic Facts Before AI Interpretation

When a deterministic source of truth exists, Repo Control II should use it directly.

Examples:

```text
working-tree state
→ Git

branch identity
→ Git

commit identity
→ Git

remote state
→ Git / GitHub

file relationships
→ repository-intelligence engine

test outcome
→ test runner

workflow state
→ Repo Control workflow state

project instructions
→ tracked Markdown / Agent Skills

AI
→ interpretation, planning, review, explanation, bounded implementation
```

AI must not become the source of truth for repository state.

## 4.2 Product Owner Authority Is Preserved

Automation should reduce clerical work, not remove Product Owner authority.

The Product Owner remains the approval boundary for:

- significant Git mutations;
- milestone acceptance;
- architecture decisions;
- scope changes;
- escalation resolution;
- risky environment changes;
- destructive operations;
- final workflow transitions with material consequences.

## 4.3 Architect and Coder Remain Distinct Roles

Repo Control II should improve communication between Architect and Coder without merging them into one undifferentiated autonomous agent.

The Architect should reason about:

- product intent;
- architecture;
- milestone scope;
- constraints;
- acceptance criteria;
- implementation review;
- escalation;
- follow-up design.

The Coder should:

- inspect actual code;
- implement bounded scope;
- run tests;
- report evidence;
- create or update the required closeout artifact;
- stop at unresolved authority or architecture boundaries.

### Shared procedure governance

Generic Coder bootstrap, workflow-core, and Skill sources live in the
versioned [`shared/procedure`](../../shared/procedure/) bundle. The bundle
renders the host entrypoint `.github/copilot-instructions.md`, repository-local
copy of the concise workflow core, physical Skill copies, and a small SHA-256
lock. M009.009 prepared canonical release `1.3.0`; M009.010 accepted the
runtime/release-preparation safeguards. M009.015 extended manifest validation,
rendering, preview, status, transactional Apply, and Recover for the optional
root `AGENTS.md`, rendered from the same bootstrap source as the Copilot host
entrypoint. M009.014 published canonical release `1.4.0` with six generated
artifacts plus the release lock. The published `1.3.0` manifest and its
five-artifact installations remain unchanged; older installations stay
pinned until separately reviewed and explicitly applied by the Product Owner.
Existing root `AGENTS.md` files without matching procedure-lock provenance
are protected collisions, including when their bytes happen to match the
generated output. Each managed repository retains its independently recorded
installed release, so version and integrity must be inspected rather than
inferred. The registry-mapped
`docs/context/coding_agent_rules.md` is the Repo Control II-specific overlay,
not the generic bootstrap; generic materialization never overwrites that
overlay.

`coder-procedure-status` reports deterministic version, hash, overlay, and
path drift. `coder-procedure-preview` produces proposed file diffs without
writing. The internal materializer remains distinct from the browser workflow
and is not an operator-facing Coder command. M008.004 added the guarded
Procedure Updates operator path: status → Review Update → separate
CSRF-protected PO Confirm Apply → independent full-set `CURRENT` verification
→ ordinary PO Git Review. It updates one centrally registered repository per
confirmation, never switches global selection, and excludes local project
overlays and the five `docs/context` authorities. Exact existing Skill copies
may be adopted into the lock without rewriting their bytes; conflicts are
blocked. M007.006 accepted fresh/resumed host consumption and approved
instruction visibility across Repo Control II and Vocab.

The M008.004 transaction records before/after images in a private sibling of
scanner state, outside the scanner-replaced per-repository state directory and
managed worktree. It uses the existing cross-process scanner/transaction lock,
records `PREPARING`, `PREPARED`, `APPLYING`, `VERIFIED`,
`RECOVERY_REQUIRED`, and `RECOVERED`, replaces every generated artifact
before the release lock, and permits a separate PO-confirmed recovery only
when targets match recorded before/after images. Existing five-target journals
remain recoverable; new future-release journals may include the allowlisted
root `AGENTS.md`. Corrupt images, unknown external edits, and unsafe/missing
parents fail closed. Individual rename replacements are atomic; the
multi-file set is not simultaneously visible.
Repo Control locking excludes its own cooperating scans and transactions, not
arbitrary external writers. A fixture demonstrated a parent-directory rename
after final validation where an already-open descriptor wrote into the moved
in-repository directory before detection; an outside-root escape was not
observed, but the corresponding external-writer race remains a deferred
path-confinement risk. PO UI acceptance is distinct from live materialization;
no live repository update or recovery is implied.

Overlay validation checks its declared marker and required section
structurally; human review remains responsible for semantic conflicts between
local rules and generic authority.

M009.010 added explicit release preparation for a separately approved
milestone: an active prompt marker binds a higher candidate version and
allowlisted canonical sources to the current manifest digest. Read-only
preview validates the complete candidate using the ordinary strict loader;
publish rechecks authorization and integrity before atomically replacing only
the manifest. This is not a hash-mismatch bypass, generated-file Apply, or
service deployment. Product Owner-controlled rollout remains separate.
M009.010's accepted recovery checks remain separately available when canonical
comparison cannot be established: recovery uses verified transaction images
and explicit Product Owner confirmation, while Apply remains blocked.

## 4.4 Human-Readable Artifacts Remain Primary

Normal project artifacts should remain understandable without specialized tooling.

Preferred format:

```text
Markdown
+
small structured metadata where useful
```

Repo Control II should not require machine-only workflow documents for ordinary project work.

## 4.5 External Engines Should Be Integrated, Not Rebuilt Without Need

Repo Control II should prefer:

```text
adopt open standards
integrate mature open-source engines
customize the workflow layer
fork only when required
```

The system should not rebuild a repository graph engine, coding agent, or model router merely because those functions are technically possible to recreate.

## 4.6 Local and Remote Repository State Are Different Authorities

Repo Control II must preserve the distinction between:

```text
GitHub
→ committed / pushed remote state

local Git repository
→ current branch / index / working tree

repository-intelligence engine
→ structural interpretation of selected code state
```

GitHub alone cannot represent uncommitted local changes.

## 4.7 Milestones Are Bounded Work Containers

A milestone is not a single request-response exchange.

A milestone may contain:

```text
initial prompt
→ coder work
↔ questions
↔ clarifications
↔ stop conditions
↔ escalations
↔ bug discoveries
↔ failed tests
↔ Architect decisions
↔ Product Owner decisions
↔ prompt addenda / lock-ins
→ resumed implementation
→ validation
→ closeout
```

Repo Control II must preserve this iterative structure.

## 4.8 Scope Change Must Be Explicit

Clarifications that preserve the approved objective may remain inside the active milestone.

Material changes to:

- objective;
- architecture;
- authority;
- risk;
- persistence;
- implementation strategy;
- validation burden;
- product behavior;

should trigger explicit re-scope, suspension, or a follow-up milestone.

## 4.9 Operational Convenience Must Not Weaken Security Boundaries

Always-on operation, browser access, startup automation, and remote authentication must not weaken:

- mutation confirmation;
- repository isolation;
- SSH host-key verification;
- credential separation;
- Product Owner authority.

---

# 5. System Boundary

Repo Control II is a **control plane**, not an IDE, coding model, or source-control replacement.

Its durable value is Product Owner authority, deterministic repository/project state, explicit lifecycle, bounded evidence routing, and safe integration of interchangeable specialized tools. It is not an AI project manager or a bespoke general-purpose agent framework.

Repo Control II should own:

- project/repository selection;
- repository registration;
- workflow state;
- milestone state;
- artifact location and status;
- Git-state presentation;
- controlled Git action preparation;
- accepted routine Git execution after Product Owner confirmation;
- integration with Git/GitHub;
- integration with repository-intelligence tools;
- integration with Agent Skills;
- integration with test evidence;
- tool-access boundary for Architect and Coder;
- bounded context assembly;
- review-state transitions;
- Product Owner approval points.

Repo Control II should not own by default:

- source-code editing experience;
- generalized terminal emulation;
- autonomous project management;
- source-control internals;
- repository parser/compiler implementation;
- model hosting;
- model training;
- broad coding-agent implementation;
- replacement of GitHub;
- replacement of VS Code.

---

# 6. Runtime Architecture

## 6.1 Linux Service

Repo Control II runs as an always-on systemd service on `henderson-server1`.

The tracked unit is:

```text
ops/systemd/repo-control.service
```

The service runs as:

```text
User=chuck
```

and starts Repo Control from the project virtual environment.

The accepted network endpoint is:

```text
127.0.0.1:8765
```

The service starts automatically at Linux boot and restarts after failure.

Repo Control is therefore not dependent on an interactive terminal, VS Code session, or manually started shell process.

## 6.2 Loopback-Only Exposure

Repo Control remains bound to Linux loopback only.

It is not directly exposed to:

- the home LAN;
- arbitrary network clients;
- the public Internet.

This keeps the browser control plane behind the server's SSH access boundary.

## 6.3 Windows Access

Windows reaches Repo Control through an SSH local port forward.

The accepted portable launcher package is:

```text
ops/windows/RepoControlLauncher/
```

The Windows operating model is:

```text
Windows login
→ per-user scheduled task
→ hidden SSH tunnel
→ Windows 127.0.0.1:8765
→ server 127.0.0.1:8765
→ Repo Control
```

The Product Owner may then open:

```text
http://127.0.0.1:8765
```

directly in the browser.

The launcher remains available as a fallback/on-demand entry point.

## 6.4 Tunnel Safety

The Windows helper must:

- reuse an existing healthy Repo Control tunnel;
- refuse an unrelated listener on local port `8765`;
- avoid embedding credentials;
- preserve normal SSH host-key verification;
- avoid visible console windows during automatic logon startup;
- not become a Windows service.

---

# 7. Project and Repository Model

Repo Control II supports multiple explicitly registered repositories.

The current server-controlled registry is:

```text
ops/repositories.toml
```

The browser selects repositories by configured key.

The browser must not supply arbitrary filesystem paths.

Current configured repositories include:

```text
Repo Control II
Vocab App
```

For Repo Control II:

```text
Project:
Repo Control II

Repository:
/home/chuck/projects/repo-control-ii

Remote:
git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git

Default branch:
main

Context:
docs/context/

Milestones:
docs/milestones/
```

The browser must never silently switch to or inspect an unregistered repository.

---

# 8. Multi-Repository Isolation

Repository selection is a control-plane concern.

The selected repository is service-wide operational state.

A service restart returns selection to the configured default.

Repository registry changes require service restart.

Important invariants:

- selection must not mutate Git state;
- prepared Git actions must be invalidated when selection changes;
- an action prepared for one repository must never execute against another repository;
- browser clients must not bypass the allowlist;
- repository identity must remain visible in the UI.

M009.017 adds a remap-specific, Product Owner-operated CLI for coordinating
the five registered governing-document roles with the six-artifact 1.4.0
procedure overlay and lock. It is not a general cross-repository transaction
engine and does not change ordinary Procedure Apply's incompatibility guard.
The read-only `coder-procedure-remap-preview` binds the repository identity,
old/new maps, registry image, mapped-document hashes, current generated
artifacts, direct Markdown references between mapped documents, Git state,
selected milestone, live service selection, operator prerequisites, restart
implications, rollback steps, and procedure manifest. A separate
`coder-procedure-remap-stage --confirm`
rechecks that plan, stores exact before/after images outside managed
worktrees, stages the generated entrypoints and lock first, and writes the
central registry last.

These are individually atomic file replacements, not one atomic transaction
across repository roots or service restart. During staging, procedure status
may be incompatible and the running service's stale-registry guard disables
mutations. The Product Owner must restart the service, select the target
repository, then separately run `coder-procedure-remap-activate --confirm`;
activation verifies the new runtime instance, role map, milestone, mapped
document hashes, and procedure `CURRENT`. The out-of-band
`coder-procedure-remap-recover --confirm` restores exact before-images when
all targets match either recorded image, after which restart and activation
verify the rollback. Corrupt journals or unknown images remain blocked for
operator intervention. Ordinary Coder sessions cannot perform the live
remap, restart, recovery, or managed-repository migration. Synthetic fixture
coverage does not constitute live operational acceptance or deployment.

---

# 9. Git Control Architecture

## 9.1 Git Is the Authority

Repo Control derives repository state from Git.

It does not maintain a competing model of:

- current branch;
- staged files;
- unstaged files;
- untracked files;
- commit history;
- remote-tracking state.

Cached presentation data is acceptable, but Git remains authoritative.

## 9.2 Accepted Initial Git Scope

The accepted routine Git lifecycle includes:

```text
status / change review
diff review
stage selected paths
unstage selected paths
restore selected unstaged changes
restore selected paths to HEAD
commit explicitly staged content
history / commit detail / file history
local branch create
clean-tree branch switch
safe fully-merged local branch delete
fetch
non-force push
first publish with upstream
fast-forward-only update
```

The initial Git Control arc intentionally excludes:

```text
merge / merge-conflict resolution
rebase / interactive rebase
cherry-pick
reset
stash
reflog recovery
worktrees
submodules
force push / force-with-lease
remote branch deletion
tag management
arbitrary refs or refspecs
general pull
history rewriting
GitHub PR or CI control
browser remote-configuration editing
```

Manual Git remains the fallback for advanced cases.

## 9.3 Read Versus Mutation

Read-only operations may be broadly available where appropriate.

Mutation remains explicitly gated.

Repo Control may prepare and explain a mutation, but Product Owner confirmation is required for execution.

## 9.4 Prepared-Action Boundary

Mutation operations use prepared-action semantics.

A prepared action should be:

- short-lived;
- single-use;
- repository-bound;
- state-bound;
- operation-bound;
- revalidated before execution.

Prepared actions must be invalidated by relevant state changes, including repository selection changes and service restart.

## 9.5 Post-Action Verification

Git command success alone is not sufficient for important remote operations.

Where practical, Repo Control should verify the resulting authoritative Git state after execution.

For Push, this includes verifying the actual remote tip relationship rather than trusting process exit alone.

---

# 10. GitHub Authentication Architecture

Repo Control remote Git actions are intentionally noninteractive.

The remote subprocess boundary preserves controls equivalent to:

```text
GIT_TERMINAL_PROMPT=0
GIT_ASKPASS=/bin/false
SSH_ASKPASS=/bin/false
stdin=DEVNULL
SSH BatchMode=yes
```

Repo Control II uses a dedicated repository-scoped GitHub Deploy Key.

The private key is stored outside the repository:

```text
~/.ssh/id_ed25519_repo_control_github
```

The dedicated SSH alias is:

```text
github-repo-control-ii
```

The accepted remote is:

```text
git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git
```

Architectural requirements:

- `StrictHostKeyChecking` remains enabled;
- the deploy key is scoped only to Repo Control II;
- other project SSH identities remain independent;
- no private key enters repository content;
- no token/password prompt is required;
- no SSH agent is required for the accepted service path;
- service operation must remain deterministic after boot.

The passphrase-free private key is an accepted operational tradeoff of unattended systemd Push and must remain protected by filesystem permissions and repository-scoped authorization.

---

# 11. Workflow Orchestration Layer

The Workflow Orchestration layer coordinates milestone lifecycle, governing documents, decision trails, and role handoffs across Repo Control II.

Accepted responsibilities include:

- recursive discovery of milestone prompt artifacts beneath configured repository `milestone_root`;
- external repository-scoped active milestone selection pointer (`<state_root>/<repository_id>/milestones/selection.json`, default state root `~/.local/share/repoctl`);
- chronological workflow sidecars (`*_workflow.md`) beside each milestone prompt;
- reconstruction of milestone lifecycle phase from sidecar event history;
- open condition tracking and blocking condition enforcement;
- on-demand generation of Coder and Architect handoff packets;
- provider-neutral Architect JSON API on loopback with read-only evidence routes and fixed typed proposal-submission routes;
- persistent Architect Secure MCP Tunnel (`repoctl mcp-architect` via OpenAI `tunnel-client`), exposing bounded dispatch proposals without granting approval or general write authority.

The accepted, implemented six-state lifecycle model is:

```text
PLANNING
→ READY
→ IN_PROGRESS
      ↔ WAITING
→ REVIEW
→ CLOSED
  ↘ Product Owner append-only reopen to REVIEW or IN_PROGRESS
```

Lifecycle state meanings:

```text
PLANNING
→ Initial phase upon milestone selection. Prompt and lock-ins are reviewed.
  Implementation is not authorized.

READY
→ Product Owner has reviewed and approved the prompt. Unresolved conditions
  are cleared. Coder may preflight, but implementation requires the explicit
  implementation_started event and IN_PROGRESS authorization.

IN_PROGRESS
→ Implementation is actively underway by the coding agent. Normal tests,
  refinements, and intermediate validation occur here.

WAITING
→ Execution is paused awaiting a blocker, clarification, Architect answer,
  or Product Owner decision. Resume transitions back to IN_PROGRESS.

REVIEW
→ Closeout/evidence has been explicitly submitted for Architect and Product Owner
  review. The phase itself does not independently verify passing tests.

CLOSED
→ Product Owner has accepted and explicitly closed the milestone. Coder
  authority ends. A guarded Product Owner reopen may append a transition to
  REVIEW (no edit authority) or IN_PROGRESS (bounded work under the same
  objective); prior acceptance and closure events remain historical.
```

Transitions are explicit and recorded in the per-milestone workflow sidecar. Transition into `READY` and `REVIEW` is gated on zero open blocking conditions.

Tests, Git cleanliness, commits, and closeout presence do not automatically advance lifecycle.

---

# 12. Workflow Event Architecture

Repo Control II separates **durable workflow state** from **workflow event chronology**.

Milestone lifecycle state is reconstructed deterministically from the chronological workflow sidecar beside the prompt:

```text
docs/milestones/M003.004_architect_read_only_contract_prompt.md
docs/milestones/M003.004_architect_read_only_contract_workflow.md
```

Supported event types include:

```text
prompt_approved
implementation_started
question_submitted
answer_recorded
decision_recorded
lock_in_approved
escalation_opened
escalation_resolved
bug_discovered
bug_classified
bug_fixed
test_result_recorded
context_verification_recorded
implementation_resumed
closeout_submitted
review_requested
correction_requested
acceptance_recorded
milestone_closed
milestone_reopened
git_disposition_recorded
```

Each event records an ISO-8601 timestamp, event type, summary, optional actor, and optional structured metadata (such as blocking status or condition keys).

This event model preserves a full audit trail of questions, answers, bug classifications, test outcomes, and binding decisions without requiring an opaque database or losing transparency.

M009.010's runtime compatibility gate is registered for the newer
`context_verification_recorded` payload (verification-record schema version 2)
and checks the running service's advertised capabilities before that record is
appended. Existing version-1 records remain readable. This is not a blanket
runtime gate for every event type: unchanged established event contracts were
not newly gated. Future event or payload-schema changes require explicit
reader-capability requirements; unknown historical events are not discarded,
coerced, or rewritten.

## Single-User Browser Action Compression

Approve & Start records prompt_approved followed by implementation_started; Resolve & Resume records the active condition's answer/resolution, an optional distinct lock_in_approved, then implementation_resumed; Accept & Close records acceptance_recorded followed by milestone_closed. A valid immediately preceding acceptance may be reused by closure, preserving existing ledger semantics. Reopen appends `milestone_reopened` with the required reason and target phase; it never edits prior events or mutates Git.

Before closure, approved applicable Architect INSTRUCTION/RESPONSE dispatches
are compared deterministically with the most recent `closeout_submitted`
timestamp. Newer guidance requires a later amended closeout or a deliberate
second confirmation that identifies the outstanding dispatches and records
the reason and IDs on the closure event. Pending, rejected, PROMPT, or
non-applicable dispatches do not trigger the guard.

The authoritative lifecycle primitives validate the complete sequence in memory before one bounded, atomic sidecar replacement. Failure of any constituent event, event/byte limit, or persistence leaves the prior sidecar intact. Repository/selection, phase, and event-count preconditions reject stale browser submissions. READY remains a real intermediate event phase even when Approve & Start passes through it in one deliberate action.

Normal lifecycle browser actions hardcode reported Product Owner attribution and approval; the submit button authorizes the action without an actor selector or additional confirmation checkbox. Reopen requires a separate explicit confirmation and reason; overriding the newer-guidance closure guard requires a second confirmation and durable reason/dispatch evidence. These are single-user UI controls, not authentication or delegated Architect/Coder authority. Architect proposal approval/save remains separate and cannot start or resume lifecycle work. Server-authoritative polling and edited-form stale protection remain unchanged.

---

# 13. Milestone Artifact Architecture

## 13.1 Prompt

The milestone prompt is the initial approved contract.

Canonical pattern:

```text
M<arc>.<sequence>[.<subsequence>]_<name>_prompt.md
```

Prompts remain human-readable Markdown. They define the objective, scope, boundaries, tests, and expected closeout structure.

## 13.2 Workflow Sidecar

The workflow sidecar is the tracked event ledger:

```text
M<arc>.<sequence>[.<subsequence>]_<name>_workflow.md
```

It resides in the same directory as the prompt and shares the exact milestone ID and descriptive basename. It is not an active selection anchor or a prompt; it is the durable record of transitions, decisions, and conditions.

It is intended for Git tracking, but the active sidecar may be modified/untracked as expected operational state. Only that sidecar is exempt by itself from clean-tree preflight; a newly materialized prompt or any other unexplained dirty path is not.

## 13.3 Closeout

The closeout is the final implementation and validation evidence package:

```text
M<arc>.<sequence>[.<subsequence>]_<name>_closeout.md
```

It documents completed scope, resulting behavior, files changed, validation performed, questions/decisions incorporated, deviations, known limitations, deferred work, next milestone recommendations, and read-only Git status.

Presence of a closeout is required before transitioning from `IN_PROGRESS` to `REVIEW`.

## 13.4 Handoff Packets

Handoff packets (Coder Handoff Packet, Architect Review Packet) are on-demand, read-only derived views assembled from:

```text
milestone identity & prompt
+
governing documents (context, architecture, workflow, rules, parking lot)
+
workflow event chronology & open conditions
+
deterministic local Git status & diff summary
+
recorded test results
```

Packets are generated dynamically. They are not persisted as project files. Generating a packet does not mutate repository state, milestone selection, lifecycle phase, or Git state.

---

# 14. Architect Integration Layer

The Architect role is fulfilled by ChatGPT (browser / ChatGPT Plus).

Responsibilities:

- inspect current repository state and active milestone;
- compose and refine milestone prompts;
- resolve coder questions and clarify boundaries;
- review closeout reports, test evidence, and Git status;
- recommend acceptance, bounded fixes, or follow-up milestones;
- advise the Product Owner without performing unauthorized mutations.

The accepted operational architecture uses the **Secure MCP Tunnel** (M003.006):

```text
ChatGPT (Architect)
      │
      │ MCP JSON-RPC 2.0 (Streamable HTTP / WebSocket)
      ▼
OpenAI Control Plane
      │
      │ outbound persistent tunnel session
      ▼
tunnel-client daemon (henderson-server1)
      │
      │ stdio JSON-RPC 2.0
      ▼
repoctl mcp-architect adapter
      │
      ├── HTTP GET (read-only evidence and bounded dispatch readback)
      └── fixed typed HTTP POST (pending proposal submissions only)
             ▼
Repo Control Service (127.0.0.1:8765/api/architect/v1/*)
      │
      ├── Git inspection
      ├── milestone discovery & sidecar parsing
      ├── governing document retrieval
      └── recorded evidence summaries
```

### 14.1 Read-Only MCP Tool Inventory

The adapter exposes 14 focused evidence tools (intelligence queries also persist bounded operational use metrics):

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
| `repo_read_document` | `GET /api/architect/v1/repository-document` | Read one explicit current working-tree document under the allowlisted documentation roots/types |
| `repo_history_search` | `GET /api/architect/v1/history/search` | Bounded lexical discovery over prior milestone artifacts and the `docs/history/` archive |
| `repo_history_document` | `GET /api/architect/v1/history/document` | Read one selected prior artifact with historical/non-authoritative provenance |
| `repo_review` | `GET /api/architect/v1/review` | Bounded review summary combining status, git, and lifecycle |
| `repo_intelligence` | `GET /api/architect/v1/intelligence` | Bounded structural evidence with explicit repository/generation preconditions; no build authority |

`repo_read_document` is separate from the fixed five-role `repo_document`
contract. It requires an explicit repository-relative path plus expected
repository key and registry generation. Allowed files are Markdown, text, or
reStructuredText under `docs/`, `.github/`, and `.repo-control/`; one
repository-root `README.md`; or a Skill `SKILL.md` below
`.agents/skills/<skill>/`. Symlinks, traversal, secrets/configuration paths,
source code, arbitrary root files, and non-regular files are rejected. Reads
use the current working tree, including modified and untracked documentation.
At most 128,000 UTF-8 bytes are returned; content beyond that limit is
deterministically truncated with original size, returned byte/character
counts, and a `truncated` flag. Files above 5,000,000 bytes are rejected.
Git tracking state is not collected. This does not provide browsing, search,
historical roots (`history`, `milestones`, `archive`, `archives`, or
`historical` path components), arbitrary file access, or remote access.

Historical access uses the separate `repo_history_search` then
`repo_history_document` workflow. Discovery is limited to canonical prior
prompt/workflow/closeout artifacts under each managed repository's configured
milestone root and supported text files under the fixed `docs/history/`
convention. No additional registry root field is introduced; the configured
milestone root plus this repository-relative archive convention is the
historical-root policy. The selected milestone's artifacts are excluded from historical results.
Discovery remains bounded at 2,000 candidate files, 2,000 directories, and
50,000 directory entries. If discovery reaches one of those bounds, the API
returns `historical_discovery_incomplete` (503) rather than presenting a
partial candidate set as a complete search.

Search is repository-local and lexical. Each query page reads at most 300
files and 16,000,000 bytes; a file may contribute up to its 5,000,000-byte
source limit to lexical matching. The public document read remains capped at
128,000 returned UTF-8 bytes and rejects source files above 5,000,000 bytes.
Broad inventory without a milestone ID or paired category/type filters is
rejected. Results contain paths, types, sizes, milestone/category metadata and
match counts, not full content or snippets. The default result page is 10 and
the maximum is 20. Both query and inventory results are ordered by
repository-relative path ascending. Responses include `has_more` and
`next_cursor`; repeat the same query, filters, and page limit with that cursor
to continue. Query-search `total_count` is the cumulative number of matches
found so far until the final page sets `total_count_complete=true`. Inventory
counts are complete when discovery succeeds. `omitted_count` is zero when
continuation is available because later results have not been omitted from
the traversal.

The stateless, signed cursor binds the repository identity/root, selected
milestone, query, filters, page limit, and a fingerprint of candidate paths
and file metadata. A changed candidate inventory returns
`stale_history_cursor` (409); a malformed cursor, invalid signature, or one
used with different query parameters returns `invalid_history_cursor` (400).
The service's in-memory signing key means outstanding cursors are invalidated
by a service restart. Each page rediscovers the bounded candidate set and
scans from its continuation offset. Oversized, invalid-encoding, or
unsafe-content documents cause an explicit error rather than a false
complete no-match. One selected result can be read with the same no-follow
file protections and UTF-8/truncation bounds as current-document reads.
Results and retrieved content identify themselves as `historical_evidence`,
sourced from the current working tree, and explicitly not current authority.
Current PO decisions and approved dispatches, lifecycle and active prompt,
and current governing documents remain above historical evidence in
authority. This capability does not browse Git history, source code,
arbitrary roots, or network resources.

### 14.2 Bounded Dispatch Proposal Tools

The original 10 evidence tools retain their existing read-only behavior.
`repo_intelligence`, `repo_read_document`, and the two historical tools bring
the evidence inventory to 14; with the four dispatch tools below, the fixed
inventory is 18 tools:

| Tool | Endpoint | Scope |
|---|---|---|
| `repo_dispatches` | `GET /api/architect/v1/dispatches` | Bounded status/content readback; at most five list results, or one explicit dispatch ID |
| `architect_submit_prompt_draft` | `POST /api/architect/v1/dispatches/prompt` | Create a pending prompt proposal; no file or lifecycle change |
| `architect_submit_instruction_draft` | `POST /api/architect/v1/dispatches/instruction` | Create a pending instruction for the selected non-closed milestone |
| `architect_submit_response_draft` | `POST /api/architect/v1/dispatches/response` | Create a pending response, optionally related to an event in the active milestone |

Dispatch state is repository-scoped operational JSON outside the repository, atomically replaced and bounded to 500 retained records, a 40 MiB serialized-ledger ceiling, and a 64 KiB body limit. List readback remains bounded to at most five results, or one explicit dispatch ID; increasing retention does not increase default response size. Architect submission cannot approve, reject, write repository files, invoke Git, or record lifecycle events. Only the Product Owner-facing browser route can approve or reject.

PROMPT approval atomically creates only the derived canonical prompt path under the configured milestone root. It does not select the milestone, approve its lifecycle prompt, start work, or mutate Git. Approved INSTRUCTION/RESPONSE content is surfaced in `repoctl coder-context` only for its approved active milestone.

This replaces manual prompt copy/paste where applicable: Architect draft -> Repo Control -> Product Owner review -> canonical prompt. Dispatch **Approve & Save Prompt** is distinct from lifecycle **Approve & Start**. The Product Owner should normally review/commit a materialized prompt before implementation; creation is not a dirty-tree exception.

Pending and rejected proposals are not authorized Coder guidance. Dispatch approval itself records no lifecycle event. The approved section in `coder-context` is milestone-scoped guidance, not standing lifecycle authority.

Live M003.011 discovery showed that tool inventory changes may require an explicit ChatGPT custom-app refresh beyond restarting Linux services. Local `tools/list` evidence must be distinguished from remote catalog visibility.

### 14.3 Authority and Security Invariants

- **Authority Boundary:** The MCP tunnel is a transport projection, NOT project authority. Authority remains with Repo Control on `127.0.0.1:8765`, local Git, and tracked markdown artifacts.
- **Bounded Capability:** Existing evidence tools remain GET-only. The only Architect POST operations are fixed, typed proposal submissions that persist pending records; they cannot approve dispatches or mutate lifecycle, repository files, or Git.
- **Product Owner Gate:** Only deliberate approval on the Product Owner-facing browser route authorizes applicable guidance; rejection never does.
- **Coder Consumption:** Only approved applicable guidance is surfaced by the local Coder CLI; Coder remains Git-prohibited.
- **Loopback Isolation:** Repo Control binds strictly to `127.0.0.1:8765`. Zero LAN or public ports are opened.
- **Credential Protection:** Runtime tunnel credentials (`CONTROL_PLANE_API_KEY`, `CONTROL_PLANE_TUNNEL_ID`) reside outside the repository in `~/.config/repo-control/tunnel.env` (permissions `0600`).
- **Persistent Service:** The tunnel runs as a dedicated systemd service (`repo-control-architect-tunnel.service`) under user `chuck`.
- **Operational Fallback:** When the tunnel is unavailable, the Product Owner can generate and provide an Architect Review Packet as an accepted fallback.
- **Minimum Evidence:** Natural Architect requests authorize only the minimum bounded read-only evidence required through fixed tools, never arbitrary repository, filesystem, shell, Git, lifecycle, or Product Owner authority.

---

# 15. Coder Integration Layer

The accepted Coder integration is local, in-process CLI execution, not Coder MCP:

- `repoctl coder-context` / `coder-status`: selected milestone, authorization, standing references, open conditions, and applicable approved dispatches (context only).
- `coder-question`, `coder-bug`, `coder-test`, `coder-closeout`: fixed bounded event submissions using `actor_role="coder"` and `po_approved=False`.

There is no generic event submission or Product Owner approval/start/resume/acceptance/closure command. The CLI reuses the lifecycle engine; it does not own Git mutations. Handoff packets remain a read-only fallback.

Coder environments may include:

- GitHub Copilot;
- Codex;
- Cline;
- OpenCode;
- Aider;
- Goose;
- OpenHands;
- other approved agent environments.

Repo Control should expose enough standardized state that coding agents do not require product-specific custom integration where avoidable.

Coder input should converge toward:

```text
active milestone
+
standing Agent Skills
+
approved addenda / lock-ins
+
targeted repository evidence
+
relevant tests
```

Coder output should converge toward:

```text
code changes
+
test evidence
+
questions / escalation events as needed
+
one closeout
+
final read-only Git status
```

Coding agents do not own the Git lifecycle.

---

# 16. Agent Procedure Layer

Primary preferred mechanism:

```text
Agent Skills
```

Responsibilities:

- reusable agent procedure;
- explicit invocation for accepted procedures;
- milestone-mode behavior;
- targeted context reading;
- escalation behavior;
- implementation restraint;
- validation expectations;
- closeout requirements;
- reminders about Git mutation boundaries, whose authority remains in governing rules and deterministic Repo Control controls.

M005.002 and M005.003 were accepted and closed with three repository-local explicit SOP helpers: `repo-control-validation-closeout`, `repo-control-blocker-bug-escalation`, and `repo-control-implementation`. The versioned shared procedure renders the generic Coder bootstrap at the host entrypoint; `docs/context/coding_agent_rules.md` is the repository-specific overlay. Explicit Skill invocation/routing is normal; implicit activation is advisory only. Skills derive procedure and cannot grant scope, Git, lifecycle or Product Owner authority. The bootstrap, overlay, active prompt, and Repo Control authority have distinct roles. Native repository-local Agent Skills are in use; no custom skill runtime, loader, manager, catalog, or permission framework is integrated.

`repo-control-preflight` is deferred: mandatory `coder-context`/authorization and read-only Git preflight are already explicitly required, and a skill must not become their sole carrier.

No additional Skill families are part of the accepted catalog or current direction. Optional coding-restraint guidance remains outside the adopted procedure set and is not scheduled.

---

# 17. Repository Intelligence Layer

Repository intelligence should make code structure queryable without repeated broad file reading.

Desired evidence may include:

```text
file
module
class
function
method
import
call
reference
inheritance
test relationship
dependency relationship
```

A valuable future model remains:

```text
Committed Baseline Graph
        vs.
Current Working-Tree Graph
```

Repo Control should expose query-oriented repository intelligence rather than dumping entire graphs into AI context.

M004.001 and M004.002 are accepted and CLOSED. The M004.001 Graphify POC recovered 19/20 supported checks and 27/27 bounded audited assertions, but only 1/5 cross-layer checks. M004.002's bounded integration is accepted within its documented scope; this is not an unconditional engine-adoption or correctness claim.

`intelligence.service` owns source identity, graph generations, bounds and provenance; a replaceable provider owns engine access. The Graphify provider runs a short-lived, fixed-argument bridge in a dedicated external pinned environment. It uses tested native library lookup/traversal/impact helpers rather than parsing MCP node/edge text or recreating a graph engine. Generic paths retain bounded native text with explicit endpoint-only structured counts; they are not call-chain evidence.

Both `repo_intelligence` and local `repoctl coder-intelligence` use the same service-selected repository and normalized envelope. Operations include stats/freshness, lookup/disambiguation, filtered neighbors, callers/callees, imports/references, likely tests/reverse impact, lexical context and generic paths. Communities, viewer, semantic processing and PR/network tools are excluded.

Semantic source identity hashes supported nonignored tracked/untracked code content together with the versioned source scope/options, Graphify producer, and graph schema. The recorded HEAD and branch remain descriptive build provenance and do not invalidate structurally identical inputs by themselves. Status reports semantic freshness reasons separately from provenance-only changes. Older manifests lacking the semantic identity are INCOMPATIBLE and require a verified rebuild; provider, extractor/options, and schema mismatches are likewise explicit. Builds copy the bounded scope externally, check semantic identity again after extraction, and publish a manifest/hash. States are MISSING, BUILDING, CURRENT, STALE, FAILED, INCOMPATIBLE, and NOT_AVAILABLE; only CURRENT graphs supply evidence. Queries never rebuild. Product Owner Approve & Start establishes a CURRENT baseline for supported repositories and does not advance lifecycle if a required build fails; unsupported repositories report NOT_AVAILABLE. The deliberate CSRF-protected Product Owner Build / Refresh button remains available without a separate approval checkbox. During authorized IN_PROGRESS work, a Coder may invoke `repoctl coder-intelligence-refresh` when non-current evidence would materially help; the command is fixed-configuration, bound to the centrally registered workspace and active milestone, and cannot alter Git or lifecycle.

The fixed external runtime is `~/.local/share/repoctl-tools/graphify-0.9.91/venv`. Application dependencies remain unchanged. Runtime networking is denied through inherited Linux seccomp rules; configuration/home/environment are isolated and credentials are not inherited. This is not a general filesystem sandbox. State lives under `<state_root>/intelligence/<repository_id>`, outside scanner-republished artifacts; at most current and previous graph generations are retained after builds.

Independent defaults are 48 KiB, 60 nodes, 120 relationships, depth 2 and a 5-second request budget; hard maxima are 64 KiB, 120 nodes, 240 relationships, depth 4 and 10 seconds. Native pipes, graph bytes, source scope and process resources have additional bounds. Failures, ambiguity, stale evidence and omissions are explicit; native `isError=false` is never used as a success criterion.

Bounded SQLite measurements retain use metadata, not query text/source bodies. Refresh receipts are separate from structural query uses and comparative benchmark records. M009.009 dispositions distinguish materially relied-on queried structure (`USED`), evidence not materially useful or needed (`NOT USED`), and genuinely unsupported or inaccessible evidence (`NOT AVAILABLE`); `STALE` alone does not mean `NOT AVAILABLE`. Optional ratings and source-inspection feedback, sticky MISLEADING flags, latency/size statistics, operation breakdowns, refresh counts/history and comparative task metrics appear at `/intelligence`. Mechanical benchmark replay is not proof of human/model productivity. See [operator instructions](../../README.md#repository-intelligence-m004002-provisional) for installation, access, measurement and limitations.

---

# 18. Test and Evidence Layer

Responsibilities:

- test command definitions;
- test results;
- build results;
- static checks;
- validation evidence;
- optional screenshots/log references;
- evidence association with milestone state.

Repo Control should record test evidence but must not infer a passing result when the test runner did not produce one.

Test results are evidence, not AI opinion.

---

# 19. Model Gateway Layer

This remains a later architecture layer.

Potential responsibilities:

- local/cloud provider abstraction;
- model routing;
- quota/cost routing;
- health/fallback;
- latency selection;
- privacy/offline selection;
- task-type routing.

Candidate engines:

- LiteLLM;
- OmniRoute.

This layer must not become a prerequisite for Workflow Orchestration, Agent Skills, or Repository Intelligence.

---

# 20. State Persistence Architecture

Repo Control II should avoid premature database complexity.

## 20.1 Durable Tracked State

Best represented in Git-tracked Markdown or configuration:

- project context;
- architecture;
- workflow;
- coding-agent rules;
- milestone prompts;
- closeouts;
- stable project configuration;
- repository allowlist.

## 20.2 Operational and Workflow State

The accepted M003 persistence choices are:

- external JSON selection pointer at `<state_root>/<repository_id>/milestones/selection.json`, default state root `~/.local/share/repoctl`;
- chronological Markdown workflow sidecar beside the prompt, including events, current phase reconstruction, waiting reasons, and open conditions;
- separate external repository-scoped JSON Architect dispatch ledger;
- validated dispatch-ledger preservation across scanner state-directory replacement, serialized with scanner/dispatch operations and fail-closed handling of invalid ledgers or pre-existing backups (M008.003.001);
- in-memory service-wide repository selection and short-lived prepared Git actions.

Workflow events are durable milestone evidence, not an alternate database model. Cached intelligence/UI state remains future or derived state; no database was introduced by the M003 workflow or dispatch implementation.

## 20.3 Derived State

Should be recomputable where practical:

- Git status;
- branch graph;
- diff summaries;
- repository graph;
- test summaries.

Derived state must not become competing canonical truth.

---

# 21. UI Architecture Direction

The current implementation is Flask-based and has proven sufficient for the accepted Git Control baseline.

Flask remains the current implementation framework, but it is not an immutable architectural requirement.

The UI should provide clear views for:

```text
Repository / Project Selection
Git Review
History
Branches
Milestones
Current Workflow
Repository Intelligence
Tests / Evidence
Settings / Integrations
```

The UI should emphasize:

- state visibility;
- explicit actions;
- clear authority;
- minimal ambiguity;
- safe confirmation;
- practical daily use;
- wide desktop usability;
- browser-first operation from Windows.

Framework replacement should require a demonstrated product or maintenance benefit.

---

# 22. Security and Authority Boundaries

Repo Control II may become a bridge between cloud AI and local repositories.

That boundary requires deliberate design.

## 22.1 Local Repository Access

Remote/cloud AI should access local repository evidence only through approved Repo Control tools.

Avoid exposing arbitrary filesystem access when a bounded query interface is sufficient.

## 22.2 Mutation Controls

Tool access should distinguish:

```text
read
prepare
propose
execute
```

For sensitive operations, these must not collapse into one implicit action.

## 22.3 Secrets

Repo Control must not expose:

- credentials;
- tokens;
- API keys;
- protected environment contents;
- SSH private keys;
- GitHub secrets;
- service credentials;

unless a future specifically approved feature has a justified secure mechanism.

## 22.4 Project Isolation

Multiple managed repositories must remain explicitly separated.

A tool request for one project must not silently inspect or mutate another project.

## 22.5 Browser Mutation Boundary

Browser-based Git mutation is allowed only through the accepted bounded Repo Control action model.

The browser must not expose a general shell or arbitrary Git command facility.

---

# 23. External Tool Strategy

Repo Control II should prefer mature external tools where they solve a specialized problem well.

Current positions:

## 23.1 Git Control

The initial decision is to keep Git Control inside the browser-based Linux Repo Control service rather than adopt a desktop Git client as the primary architecture.

GitButler and other Git tools may remain references for UX concepts or future advanced needs.

## 23.2 Repository Intelligence

M004.001 and M004.002 are accepted and CLOSED. The M004.002 provider-backed integration supplies bounded evidence and value/usefulness measurements within documented limits; acceptance does not establish unconditional engine adoption or runtime correctness.

## 23.3 Agent Skills

The accepted Agent Procedure baseline consists of the generated generic host bootstrap, the Repo Control II-specific `docs/context/coding_agent_rules.md` overlay, and three explicit repository-local SOP helpers: `repo-control-implementation`, `repo-control-blocker-bug-escalation`, and `repo-control-validation-closeout`. The Product Owner may use a minimal handoff notification; the Coder retrieves authoritative context and loads applicable procedures. Skills do not confer authority.

## 23.4 Ponytail

Optional coding-restraint guidance only.

## 23.5 LiteLLM / OmniRoute

Deferred model-gateway candidates.

---

# 24. Local AI Architecture Position

Local AI is an optional execution resource, not architectural truth.

Possible uses:

- bounded coding;
- repetitive summaries;
- low-risk classification;
- private/local transformations;
- semantic assistance;
- cost reduction.

Local AI must not be trusted to establish:

- Git state;
- test results;
- branch identity;
- repository structure when a deterministic parser exists;
- workflow state.

If local models are introduced, their quality should be benchmarked against actual completed milestones.

---

# 25. Architectural Invariants

The following are durable rules unless explicitly revised by an approved architecture milestone.

## 25.1 Repo Control Is the Integration Layer

Repo Control coordinates state and tools.

It should not absorb specialized engines without need.

## 25.2 Git Remains Git Authority

Repo Control presents and controls Git.

It does not maintain a competing repository history model.

## 25.3 Product Owner Owns Important Mutations

Automation may prepare and execute only through explicit Product Owner-confirmed controls.

## 25.4 Coding Agents Do Not Own Git Lifecycle

Coder workflow ends at implementation, validation, closeout, and read-only status reporting.

## 25.5 Architect and Coder Remain Separate Roles

They inform each other through controlled artifacts and Repo Control evidence.

They do not silently collapse into one autonomous actor.

## 25.6 Milestone Decisions Must Be Durable

Important clarifications and lock-ins must not exist only in transient chat.

## 25.7 Iteration Inside a Milestone Is Normal

Questions, stops, bug discoveries, and corrections do not automatically create a new milestone.

## 25.8 Material Scope Change Must Be Explicit

A milestone must not silently expand into a different objective.

## 25.9 Repository Intelligence Is Evidence

Graph/parser output informs AI.

It is not itself project authority.

## 25.10 Test Results Come From Test Execution

AI summaries may explain results but must not fabricate or infer passing status.

## 25.11 Human-Readable Artifacts Are Primary

Metadata supports automation.

It does not replace understandable project documentation.

## 25.12 Context Should Be Targeted

Repo Control should provide the smallest sufficient evidence set.

## 25.13 External Tools Should Be Replaceable

Integrations should avoid unnecessary coupling to one vendor or engine where practical.

## 25.14 Local and Remote State Must Remain Distinct

GitHub remote state and local working-tree state must not be conflated.

## 25.15 Secrets Must Remain Outside Ordinary AI Context

Repo Control should expose status, not sensitive values, wherever possible.

## 25.16 Repository Selection Must Be Explicitly Bounded

Only server-configured repositories may be selected.

Browser clients may not supply arbitrary paths.

## 25.17 Prepared Actions Must Be Repository- and State-Bound

A prepared action may not survive relevant repository/state changes.

## 25.18 Always-On Operation Must Remain Noninteractive

Service startup, remote authentication, and browser access must not depend on transient interactive sessions.

---

# 26. Architecture Sequence

The completed architectural baseline and remaining capability areas are:

```text
1. Governing documents                              COMPLETE

2. Inherited-code reconnaissance                    COMPLETE

3. Git Control                                      COMPLETE / CLOSED

4. Workflow Orchestration architecture              ACCEPTED

5. Workflow Orchestration through M003.013           ACCEPTED / CLOSED

6. Agent Procedure / Skills                         M005.000–M005.004 ACCEPTED / CLOSED
7. Repository Intelligence                          M004.001–M004.002 ACCEPTED / CLOSED (bounded integration; limits/monitoring retained)
8. Managed context / history / Coder convergence    M006.000, M007.000–M007.007 ACCEPTED / CLOSED
9. M008 lifecycle/dispatch/Intelligence/procedure controls M008.000–M008.004 ACCEPTED / CLOSED
10. M009.008–M009.010 context-verification, Intelligence freshness, and runtime/release safeguards ACCEPTED / CLOSED
11. Deeper Architect / Coder integration            DEFERRED
12. Model Gateway                                  LATER
```

This is not a prescribed future implementation sequence. Any extension requires separately approved scope and must preserve the completed transport/authority boundaries.

---

# 27. Architecture Decision Criteria

When choosing between implementation options, prefer the option that:

- preserves clear authority;
- reduces manual Product Owner transport work;
- increases deterministic evidence;
- reduces repeated AI context;
- uses an existing mature tool where practical;
- minimizes new persistence;
- minimizes custom infrastructure;
- remains inspectable;
- remains recoverable;
- supports more than one coding agent;
- does not unnecessarily bind the project to one AI provider;
- can be validated incrementally;
- preserves the accepted always-on service/security model.

Avoid choosing an option merely because it is more sophisticated.

---

# 28. Workflow Orchestration Architectural Foundations (M003 Baseline)

The M003 Workflow Orchestration arc resolved the fundamental architectural questions:

```text
Minimum state model:
→ Six explicit states: PLANNING, READY, IN_PROGRESS, WAITING, REVIEW, CLOSED.

Active milestone identity:
→ External repository-scoped selection pointer
  (<state_root>/<repository_id>/milestones/selection.json) outside tracked repo content.

Artifact and state relationship:
→ Prompts define the initial contract.
→ Workflow sidecars (*_workflow.md) store chronological event history and conditions.
→ Closeouts store final validation and evidence.
→ Handoff packets are generated dynamically as on-demand read-only derived views.

Product Owner control:
→ Prompt approval (PLANNING → READY), implementation resume, review acceptance,
  milestone closure (REVIEW → CLOSED), and Git mutations require explicit Product Owner action.

Architect integration:
→ Persistent Secure MCP Tunnel (tunnel-client daemon + repoctl mcp-architect)
  preserving 10 original read-only evidence tools, adding accepted bounded intelligence/use metrics, and fixed typed dispatch submission/readback endpoints.
  Manual Architect Review Packets serve as operational fallback.

Coder integration:
→ Coder consumes prompt, approved applicable Architect dispatches, coding_agent_rules.md,
  and governing docs through the local CLI or Coder Handoff Packets. Coder remains
  strictly forbidden from executing Git mutations.
```

---

# 29. Architectural Decisions and Open Topics

## 29.1 Workflow Persistence (Decided)

Markdown workflow sidecars beside each prompt store durable event history and are intended for Git tracking; the active sidecar may be modified/untracked. The active selection pointer is externalized in `<state_root>/<repository_id>/milestones/selection.json` (default state root `~/.local/share/repoctl`).

## 29.2 Active Milestone Identity (Decided)

External selection pointer referencing the relative prompt path within the repository's configured `milestone_root`.

## 29.3 Architect Integration Mechanism (Decided & Operational)

Persistent Secure MCP Tunnel (`repo-control-architect-tunnel.service`) running OpenAI `tunnel-client` and `repoctl mcp-architect` stdio adapter. Existing evidence tools remain read-only GETs; separate fixed POST endpoints create only pending typed proposals. `repo_dispatches` provides bounded readback. Manual review packet retained as fallback.

## 29.4 Coder Integration Mechanism (Implemented)

Local `repoctl coder-context` and `repoctl coder-status`, plus four bounded Coder event commands under strict read-only Git rules. Applicable approved Architect guidance is surfaced separately through `coder-context`; no Coder MCP is implemented.

## 29.5 Repository Intelligence Engine (Accepted Bounded Integration)

Graphify 0.9.91 behind a replaceable provider and a shared bounded Architect/Coder contract. M004.001 and M004.002 are accepted and CLOSED; usefulness monitoring may continue within the documented acceptance limits.

## 29.6 Agent Skills Packaging (Explicit SOP Helpers)

M005.002 and M005.003 were accepted and closed with three minimal instruction-only repository-local Skills: `.agents/skills/repo-control-validation-closeout/SKILL.md` for validation/closeout, `.agents/skills/repo-control-blocker-bug-escalation/SKILL.md` for genuine blockers/bugs, and `.agents/skills/repo-control-implementation/SKILL.md` for reusable milestone procedure. The generic Coder bootstrap is rendered at the host entrypoint from the canonical shared procedure; `docs/context/coding_agent_rules.md` is the Repo Control II-specific overlay. Explicit invocation/routing is normal and implicit activation advisory only.

Governing documents, active prompts, approved binding lock-ins, Repo Control lifecycle and Product Owner authority remain canonical. Skills cannot authorize scope, Git, lifecycle or Product Owner actions. Native repository-local Agent Skills are in use; no custom runtime, loader, manager, catalog or permission framework is integrated. The bootstrap retains context retrieval, lifecycle authorization, repository identity, read-only preflight and no-Git authority; `repo-control-preflight` remains deferred because it would overlap mandatory startup procedure.

## 29.7 UI Framework (Stable Baseline)

Flask remains the operational framework; working adequately for current needs.

## 29.8 Model Gateway (Deferred)

LiteLLM and OmniRoute evaluation remain deferred until workflow and procedure layers are mature.

---

# 30. Definition of Architectural Success

The architecture is successful when the normal project flow becomes:

```text
Product Owner expresses intent.

Architect inspects trusted current project state via read-only MCP evidence tools.

Architect submits a bounded proposal to Repo Control.

Product Owner reviews and approves or rejects it in Repo Control.

Repo Control discovers and manages the milestone lifecycle.

Coder consumes the active milestone, standing rules, and approved applicable guidance.

Coder and Architect iterate through questions, clarifications, stops, bugs,
and decisions without the Product Owner manually transporting every artifact.

Repo Control preserves the decision trail in the workflow sidecar.

Coder validates and closes out.

Architect reviews current repository/test evidence via read-only MCP tools.

Product Owner approves.

Repo Control makes Git state and next actions clear.

Product Owner performs the Git mutation through Repo Control.

The milestone closes with durable project evidence.
```

The system makes this workflow easier, safer, and less repetitive without removing the Product Owner from consequential decisions.

---

# 31. Current Architecture Baseline Statement

Repo Control II now has an accepted operational architecture across both Git Control and Workflow Orchestration.

The current baseline includes:

```text
authoritative Linux-hosted repository
always-on systemd service (repo-control.service)
loopback-only browser service on 127.0.0.1:8765
automatic Windows SSH tunnel access
portable Windows launcher
server-controlled multi-repository allowlist
browser-based routine Git control
prepared-action safeguards and post-action verification
safe local branch and remote operations
noninteractive GitHub SSH authentication with repository-scoped Deploy Key
verified Repo Control Push
external repository-scoped milestone selection pointer (<state_root>/<repository_id>/milestones/selection.json)
recursive milestone prompt discovery beneath configured milestone_root
chronological tracked workflow sidecars (*_workflow.md)
accepted six-state lifecycle engine (PLANNING, READY, IN_PROGRESS, WAITING, REVIEW, CLOSED)
open condition tracking and blocking condition enforcement
on-demand Coder and Architect handoff packet generation
versioned Architect JSON API on loopback (read-only evidence plus fixed bounded proposal submissions)
persistent Secure MCP Tunnel service (repo-control-architect-tunnel.service)
14 bounded evidence MCP tools (intelligence also records use metrics) plus four dispatch tools; 18 total
Product Owner-gated PROMPT / INSTRUCTION / RESPONSE dispatch transport
approved applicable dispatches surfaced through local coder-context
local Coder context/status and bounded question/bug/test/closeout commands; no Coder MCP
phase-aware workflow controls, aliases/defaults, and five-second server-authoritative polling
edited-form preservation and stale-submit blocking; no WebSocket/event bus
compressed Product Owner actions with distinct ordered events and atomic sidecar persistence
strict Product Owner / Architect / Coder role separation
deterministic-evidence principle and human-readable milestone artifacts
explicit iterative milestone-loop requirement
bounded context-verification audit with agent-reported trigger/identity and unverified provider-delivery limitations (M009.008)
semantic Repository Intelligence freshness separated from Git provenance, with explicit use classification and separately reported refresh activity (M009.009)
running-service capability check before the newer context-verification record is written, plus explicitly authorized canonical release preparation with strict candidate validation and PO-controlled publication/rollout boundaries (M009.010)
```

M003 capabilities through M003.013, M004.001–M004.002, M005.000–M005.004, M006.000, M007.000–M007.007, M008.000–M008.004, and M009.008–M009.010 are accepted and CLOSED. M003.012 acceptance explicitly permits limited live coverage; it does not establish every live polling/stale-form/start scenario. M004.002's bounded Graphify integration is accepted with documented source-scope, output, and template/browser/runtime limitations; usefulness monitoring may continue. M005's accepted Agent Procedure baseline comprises the canonical generated host bootstrap, the Repo Control II-specific `coding_agent_rules.md` overlay, and three explicit repository-local Skills. M007.006 accepted fresh/resumed Coder-context convergence across Repo Control II and Vocab, including visibility of approved applicable instructions after the normal handoff. M008 added lifecycle recovery/closure guards, separately measured Intelligence estimates and fixed-reference comparison, durable dispatch-ledger preservation, and the guarded transactional procedure update path; each retains its milestone-specific validation and limits. M009.008–M009.010 added bounded context-verification audit semantics, semantic graph freshness/provenance and usage classification, and the narrow runtime capability/release-preparation safeguards; M009.010's local test/fixture validation does not itself claim live deployment/restart or managed-repository Apply acceptance. No custom Agent Skills runtime, loader, manager, catalog, or permission framework is integrated. The active milestone and lifecycle are authoritative in Repo Control, not inferred from this architecture document. Documentation maintenance does not by itself change this architecture or authorize product behavior changes.

The accepted M008.004 transaction is single-repository and exact-image recoverable, not a simultaneous six-file transaction and not a guarantee against arbitrary external filesystem mutation. Its final-check-to-rename parent-directory race is explicitly deferred under WORKFLOW-007; no mitigation is claimed here.
