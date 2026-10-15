# PROJECT_ARCHITECTURE.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Durable system architecture, authority boundaries, integration model, and architectural invariants  
**Project phase:** Git Control complete; Workflow Orchestration implemented through M003.011 (bounded Architect dispatch implementation in progress; formal milestone acceptance pending)
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`
**Current architecture baseline:** Repo Control II has an accepted browser-based Git Control layer, always-on Linux service runtime, multi-repository allowlist, Windows SSH access model, noninteractive GitHub authentication, external milestone selection pointer, chronological workflow sidecars, six-state lifecycle engine, read-only Architect evidence API, and persistent Architect Secure MCP Tunnel with a separate bounded dispatch proposal channel.
**Current architectural emphasis:** Preserve read-only Architect evidence semantics while routing typed proposal submissions through pending Repo Control dispatches, explicit Product Owner approval, and local Coder CLI consumption; preserve deterministic Git authority and strict role separation.

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
                                      Graphify / DevLens
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

This is the next major architectural focus.

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
- external active milestone selection pointer (`.repo-control/selected-milestone.json`);
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
```

Lifecycle state meanings:

```text
PLANNING
→ Initial phase upon milestone selection. Prompt and lock-ins are reviewed.
  Implementation is not authorized.

READY
→ Product Owner has reviewed and approved the prompt. Unresolved conditions
  are cleared. Coder is authorized to begin preflight and implementation.

IN_PROGRESS
→ Implementation is actively underway by the coding agent. Normal tests,
  refinements, and intermediate validation occur here.

WAITING
→ Execution is paused awaiting a blocker, clarification, Architect answer,
  or Product Owner decision. Resume transitions back to IN_PROGRESS.

REVIEW
→ Implementation is complete, tests have passed, closeout is present, and
  read-only Git status is reported. Ready for Architect and Product Owner review.

CLOSED
→ Product Owner has accepted the implementation, performed necessary Git
  mutations (staging, commit, push), and closed the milestone.
```

Transitions are explicit and recorded in the per-milestone workflow sidecar. Transition into `READY` and `REVIEW` is gated on zero open blocking conditions.

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
implementation_resumed
closeout_submitted
review_requested
correction_requested
acceptance_recorded
milestone_closed
git_disposition_recorded
```

Each event records an ISO-8601 timestamp, event type, summary, optional actor, and optional structured metadata (such as blocking status or condition keys).

This event model preserves a full audit trail of questions, answers, bug classifications, test outcomes, and binding decisions without requiring an opaque database or losing transparency.

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

The adapter exposes 10 focused read-only tools:

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

### 14.2 Bounded Dispatch Proposal Tools

The 10 evidence tools above retain their existing read-only behavior. Four separate tools implement the bounded dispatch channel:

| Tool | Endpoint | Scope |
|---|---|---|
| `repo_dispatches` | `GET /api/architect/v1/dispatches` | Bounded status/content readback; at most five list results, or one explicit dispatch ID |
| `architect_submit_prompt_draft` | `POST /api/architect/v1/dispatches/prompt` | Create a pending prompt proposal; no file or lifecycle change |
| `architect_submit_instruction_draft` | `POST /api/architect/v1/dispatches/instruction` | Create a pending instruction for the selected non-closed milestone |
| `architect_submit_response_draft` | `POST /api/architect/v1/dispatches/response` | Create a pending response, optionally related to an event in the active milestone |

Dispatch state is repository-scoped operational JSON outside the repository, atomically replaced and bounded to 50 retained records with a 64 KiB body limit. Architect submission cannot approve, reject, write repository files, invoke Git, or record lifecycle events. Only the Product Owner-facing browser route can approve or reject.

PROMPT approval atomically creates only the derived canonical prompt path under the configured milestone root. It does not select the milestone, approve its lifecycle prompt, start work, or mutate Git. Approved INSTRUCTION/RESPONSE content is surfaced in `repoctl coder-context` only for its approved active milestone.

### 14.3 Authority and Security Invariants

- **Authority Boundary:** The MCP tunnel is a transport projection, NOT project authority. Authority remains with Repo Control on `127.0.0.1:8765`, local Git, and tracked markdown artifacts.
- **Bounded Capability:** Existing evidence tools remain GET-only. The only Architect POST operations are fixed, typed proposal submissions that persist pending records; they cannot approve dispatches or mutate lifecycle, repository files, or Git.
- **Product Owner Gate:** A proposal becomes Coder-authoritative only after deliberate approval or rejection on the Product Owner-facing browser route.
- **Coder Consumption:** Only approved applicable guidance is surfaced by the local Coder CLI; Coder remains Git-prohibited.
- **Loopback Isolation:** Repo Control binds strictly to `127.0.0.1:8765`. Zero LAN or public ports are opened.
- **Credential Protection:** Runtime tunnel credentials (`CONTROL_PLANE_API_KEY`, `CONTROL_PLANE_TUNNEL_ID`) reside outside the repository in `~/.config/repo-control/tunnel.env` (permissions `0600`).
- **Persistent Service:** The tunnel runs as a dedicated systemd service (`repo-control-architect-tunnel.service`) under user `chuck`.
- **Operational Fallback:** When the tunnel is unavailable, the Product Owner can generate and provide an Architect Review Packet as an accepted fallback.

---

# 15. Coder Integration Layer

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
- preflight rules;
- milestone-mode behavior;
- targeted context reading;
- escalation behavior;
- implementation restraint;
- validation expectations;
- closeout requirements;
- Git mutation boundaries.

Skills should use progressive disclosure.

Potential skill families:

```text
repo-control-preflight
milestone-reconnaissance
milestone-implementation
milestone-validation
milestone-closeout
git-readonly-inspection
```

Ponytail may be evaluated as optional coding-restraint guidance, not as a core subsystem.

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

Candidate engines remain:

- Graphify;
- DevLens.

The engine remains unselected.

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

## 20.2 Operational Workflow State

May require lightweight structured storage:

- active milestone;
- current workflow state;
- transient waiting/escalation state;
- event timestamps;
- UI preferences;
- pending prepared operations;
- cached intelligence status;
- active repository selection.

Possible implementation options remain:

```text
small local JSON files
SQLite
lightweight relational store
```

The choice should be driven by actual R003 requirements.

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

Graphify and DevLens remain evaluation candidates.

## 23.3 Agent Skills

Agent Skills remain the preferred direction for reusable coding-agent procedure.

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

The current preferred architecture sequence is:

```text
1. Governing documents                              COMPLETE

2. Inherited-code reconnaissance                    COMPLETE

3. Git Control                                      COMPLETE / CLOSED

4. Workflow Orchestration architecture              NEXT

5. Workflow Orchestration implementation            AFTER RECONNAISSANCE

6. Agent Skills / standing procedure                PLANNED

7. Repository Intelligence evaluation/integration   PLANNED

8. Controlled Architect / Coder integration         LATER

9. Model Gateway                                    LATER
```

This sequence prioritizes the current proven workflow pain point: manual state and artifact transport.

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
→ External selection pointer (.repo-control/selected-milestone.json) outside tracked repo content.

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
  preserving 10 read-only evidence tools and adding fixed typed dispatch submission/readback endpoints.
  Manual Architect Review Packets serve as operational fallback.

Coder integration:
→ Coder consumes prompt, approved applicable Architect dispatches, coding_agent_rules.md,
  and governing docs through the local CLI or Coder Handoff Packets. Coder remains
  strictly forbidden from executing Git mutations.
```

---

# 29. Architectural Decisions and Open Topics

## 29.1 Workflow Persistence (Decided)

Tracked Markdown workflow sidecars beside each prompt store the durable event history. Active selection pointer is externalized in `.repo-control/selected-milestone.json`.

## 29.2 Active Milestone Identity (Decided)

External selection pointer referencing the relative prompt path within the repository's configured `milestone_root`.

## 29.3 Architect Integration Mechanism (Decided & Operational)

Persistent Secure MCP Tunnel (`repo-control-architect-tunnel.service`) running OpenAI `tunnel-client` and `repoctl mcp-architect` stdio adapter. Existing evidence tools remain read-only GETs; separate fixed POST endpoints create only pending typed proposals. `repo_dispatches` provides bounded readback. Manual review packet retained as fallback.

## 29.4 Coder Integration Mechanism (Active Direction)

Local `repoctl coder-context` and `repoctl coder-status` under strict read-only Git rules; applicable approved Architect guidance is surfaced through `coder-context`. Reusable Agent Skills will be evaluated in R004.

## 29.5 Repository Intelligence Engine (Future Evaluation)

Graphify versus DevLens evaluation remains planned for R005.

## 29.6 Agent Skills Packaging (Next Arc)

Designing modular, progressively disclosed Agent Skills for coding agents is planned for R004.

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
external active milestone selection pointer (.repo-control/selected-milestone.json)
recursive milestone prompt discovery beneath configured milestone_root
chronological tracked workflow sidecars (*_workflow.md)
accepted six-state lifecycle engine (PLANNING, READY, IN_PROGRESS, WAITING, REVIEW, CLOSED)
open condition tracking and blocking condition enforcement
on-demand Coder and Architect handoff packet generation
versioned Architect JSON API on loopback (read-only evidence plus fixed bounded proposal submissions)
persistent Secure MCP Tunnel service (repo-control-architect-tunnel.service)
10 read-only evidence MCP tools plus bounded dispatch submission and readback
Product Owner-gated PROMPT / INSTRUCTION / RESPONSE dispatch transport
approved applicable dispatches surfaced through local coder-context
strict Product Owner / Architect / Coder role separation
deterministic-evidence principle and human-readable milestone artifacts
explicit iterative milestone-loop requirement
```

The next architecture focus is Agent Procedure / Skills (R004) to encode reusable coding-agent behaviors.
