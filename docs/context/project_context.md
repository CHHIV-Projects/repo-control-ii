# PROJECT_CONTEXT.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Current project/product context and working baseline  
**Project phase:** Git Control complete; M003.011 CLOSED; M003.012 single-user workflow compression implementation (formal milestone acceptance pending)
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`
**Current branch:** `main`
**Current repository state:** Active, version-controlled Repo Control II repository with Git Control complete and Workflow Orchestration implemented through a persistent Architect MCP tunnel with read-only evidence and bounded, Product Owner-gated dispatch proposals
**Current working emphasis:** Preserve the read-only Architect evidence contract while using typed dispatch proposals, explicit Product Owner approval, and local Coder CLI consumption without weakening safety or Product Owner control.

---

# 1. Overview

Repo Control II is a deterministic project-control and workflow-integration system for coordinating trusted repository state, Git operations, project documentation, AI architect workflows, and coding-agent execution while minimizing unnecessary context transfer and repeated model work.

The project began as a clean redefinition of the original Repo Control effort. The inherited implementation was treated as a candidate baseline rather than accepted architecture.

The clearest proven value to date is **Git management**. That capability is now implemented and operationally accepted through the completed R002 Git Control arc.

Repo Control II is not intended to become:

- a replacement IDE;
- a custom general-purpose AI coding environment;
- a replacement for Git;
- a replacement for GitHub;
- a replacement for specialized repository-analysis engines;
- a replacement for coding agents;
- a replacement for model gateways;
- an autonomous system that removes Product Owner control over important repository mutations.

The architectural direction remains:

```text
Repo Control owns workflow state and integration.

Specialized tools own specialized capabilities.

Deterministic systems establish facts.

AI interprets those facts and performs bounded work.
```

---

# 2. Product Owner Objectives

Repo Control II has three primary product objectives.

## 2.1 Git Management

Provide a practical graphical control surface for routine Git operations while preserving explicit Product Owner control.

The initial Git Control capability is now operationally accepted.

Current accepted Git scope includes:

- working-tree status;
- changed-file visibility;
- file-level diff inspection;
- staged versus unstaged visibility;
- selective staging;
- unstaging;
- restore of selected unstaged changes;
- restore selected paths to `HEAD`;
- staged-only commit;
- commit history and commit detail;
- file history;
- branch visualization;
- local branch creation;
- clean-tree branch switching;
- safe deletion of fully merged local branches;
- ahead/behind state;
- remote awareness;
- fetch;
- non-force push;
- first publish with upstream;
- fast-forward-only update;
- explicit prepared-action confirmation and revalidation;
- post-action verification;
- multi-repository selection from a server-controlled allowlist.

The initial Git Control arc intentionally does **not** provide:

- merge or merge-conflict resolution;
- rebase or interactive rebase;
- cherry-pick;
- reset;
- stash;
- reflog recovery;
- worktrees;
- submodules;
- force push or force-with-lease;
- remote branch deletion;
- tag management;
- arbitrary refs or refspecs;
- general pull;
- history rewriting;
- GitHub PR or CI control;
- browser editing of remote configuration.

Manual Git remains the fallback for advanced cases.

Future Git work should be driven by recurring live-use needs rather than expanding the feature surface speculatively.

## 2.2 Workflow Integration

Reduce the Product Owner's current role as the manual transport layer between:

```text
ChatGPT / Architect
↕
Product Owner
↕
VS Code / Coding Agent
↕
Repository / Tests / Git
```

The long-term target is:

```text
Product Owner
        │
        ├── directs / approves
        │
        ▼
ChatGPT / Architect
        │
        │ controlled project tools
        ▼
Repo Control II
        │
        ├── Git state
        ├── milestone state
        ├── project documents
        ├── repository intelligence
        ├── test evidence
        └── coder handoff artifacts
        │
        ▼
Coding Agent / VS Code
```

The Product Owner remains the decision maker.

Repo Control II should remove unnecessary copy/paste and state-transfer work without collapsing the distinction between Architect and Coder.

This is now the primary next product objective.

## 2.3 Token and Context Efficiency

Reduce unnecessary repeated AI context consumption.

Current project workflows often require:

- restating standing rules;
- repasting milestone prompts;
- transferring coder questions;
- transferring coder closeouts;
- rereading broad project documentation;
- rescanning repository structure that could be established deterministically.

Repo Control II should move toward:

```text
stable standing rules
+
structured milestone delta
+
deterministic repository facts
+
targeted AI reasoning
```

rather than repeatedly loading the entire project history.

---

# 3. Current Operating Model

The accepted operating environment is:

```text
Windows workstation
→ Product Owner workstation
→ ChatGPT browser
→ VS Code client / coding agents
→ browser access to Repo Control

Windows logon
→ per-user scheduled task
→ hidden SSH tunnel to Repo Control
→ local browser address http://127.0.0.1:8765

Ubuntu mini-server: henderson-server1
→ authoritative editable repositories
→ Repo Control II source
→ Repo Control II systemd service
→ Python runtime
→ local development tooling
→ Docker and other server infrastructure where required

GitHub
→ remote repository hosting
→ committed/pushed source history
→ remote branch state
```

The authoritative Repo Control II working repository is:

```text
/home/chuck/projects/repo-control-ii
```

The accepted GitHub remote is:

```text
git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git
```

The dedicated SSH alias isolates Repo Control II authentication from other project identities.

---

# 4. Accepted Runtime Architecture

Repo Control II now runs as an always-on Linux service.

The tracked systemd unit is:

```text
ops/systemd/repo-control.service
```

It runs as user `chuck` and starts Repo Control with the project virtual environment.

The accepted service endpoint is:

```text
127.0.0.1:8765
```

The application remains loopback-only. It is not directly exposed to the LAN or public network.

Windows access is provided through SSH port forwarding.

The accepted Windows launcher package is:

```text
ops/windows/RepoControlLauncher/
```

and provides:

- `RepoControl.cmd` for on-demand access;
- `open-repo-control.ps1` for tunnel creation/reuse;
- `-TunnelOnly` mode;
- per-user Task Scheduler installation/removal;
- hidden startup through a non-console wrapper;
- reuse of an already healthy tunnel;
- refusal to overwrite an unrelated listener on local port `8765`.

The Linux service starts automatically at boot.

The Windows tunnel can start automatically at user logon.

The Product Owner can therefore open Repo Control from the browser without manually starting the Linux application or opening an interactive SSH shell.

---

# 5. Multi-Repository Model

One Repo Control service process supports multiple explicitly configured repositories.

The registry is a server-controlled TOML allowlist:

```text
ops/repositories.toml
```

Current explicitly configured repositories include:

```text
Repo Control II
Vocab App
```

The browser may select only configured repository keys.

The browser does not supply arbitrary filesystem paths.

Repository selection is service-wide and held in memory.

A service restart returns the active selection to the configured default.

Changing registry entries requires a service restart.

Prepared Git actions are invalidated when repository selection changes.

---

# 6. GitHub Authentication Model

Repo Control remote Git operations are intentionally noninteractive.

The accepted remote-operation boundary preserves:

```text
GIT_TERMINAL_PROMPT=0
GIT_ASKPASS=/bin/false
SSH_ASKPASS=/bin/false
stdin=DEVNULL
SSH BatchMode=yes
```

Repo Control therefore does not depend on:

- VS Code credential helpers;
- interactive Linux shells;
- Windows PowerShell credential prompts;
- `GIT_ASKPASS`;
- SSH agents;
- username/password/token prompts.

Repo Control II uses a dedicated repository-scoped GitHub Deploy Key.

The private key is stored outside the repository under user `chuck`:

```text
~/.ssh/id_ed25519_repo_control_github
```

The dedicated SSH alias is:

```text
github-repo-control-ii
```

Conceptually:

```sshconfig
Host github-repo-control-ii
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_repo_control_github
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

The Deploy Key has write access only to:

```text
CHHIV-Projects/repo-control-ii
```

Other project SSH identities remain independent.

The passphrase-free private key is an acknowledged operational tradeoff required by the current unattended service model. Its repository-scoped permissions and filesystem protections limit its authority.

---

# 7. Product Architecture Direction

Repo Control II is organized around five major capability areas.

## 7.1 Git Control

**Status: initial arc complete and operationally accepted.**

Purpose:

```text
Give the Product Owner clear, graphical, trustworthy control of routine Git.
```

The R002 arc established the accepted Git baseline, browser control surface, guarded mutations, branch/remote operations, service runtime, multi-repository model, and noninteractive GitHub Push authentication.

Future Git work should be incremental and driven by actual use.

## 7.2 Workflow Orchestration

**Status: core capabilities through M003.010 are operationally accepted; M003.011 adds bounded Architect dispatch transport and remains in progress pending formal acceptance.**

Purpose:

```text
Coordinate trusted project state between Product Owner,
Architect, Coder, repository, tests, and Git.
```

This is Repo Control II's core coordination capability.

A milestone is a **bounded work container**, not a single prompt-response transaction.

The normal milestone lifecycle may contain multiple iterations of:

```text
coder work
↔ question / clarification
↔ stop condition
↔ escalation
↔ unexpected repository reality
↔ bug discovery
↔ test failure
↔ Architect decision
↔ Product Owner decision
↔ prompt addendum / lock-in
→ resumed implementation
```

These exchanges remain part of the same active milestone when they clarify or safely complete the approved objective.

A new or separate milestone should normally be created when a discovered issue materially changes:

- objective;
- scope;
- architecture;
- risk;
- authority;
- persistence;
- Git strategy;
- implementation direction;
- validation burden.

The accepted, implemented six-state lifecycle model is:

```text
PLANNING
→ READY
→ IN_PROGRESS
      ↔ WAITING
→ REVIEW
→ CLOSED
```

Key architectural foundations established in this arc include:

- recursive discovery of milestone prompt artifacts beneath configured repository `milestone_root`;
- external active milestone selection pointer (`.repo-control/selected-milestone.json`) outside repository content;
- human-readable chronological workflow sidecars (`*_workflow.md`) recording lifecycle transitions, binding decisions, and open conditions;
- on-demand Coder and Architect Review handoff packets;
- provider-neutral Architect JSON API on loopback (`/api/architect/v1/*`): read-only evidence routes plus fixed typed proposal-submission routes;
- persistent Architect Secure MCP Tunnel (`repoctl mcp-architect` via OpenAI `tunnel-client` daemon, systemd unit `repo-control-architect-tunnel.service`), retaining read-only evidence tools and adding bounded dispatch submission/readback.

## 7.3 Agent Procedure

Purpose:

```text
Encode stable coding-agent procedure once instead of repeating it in every prompt.
```

Primary concerns:

- repository preflight;
- milestone modes;
- scope discipline;
- implementation restraint;
- testing expectations;
- escalation;
- closeout structure;
- Git mutation prohibition for coding agents;
- environment identification;
- targeted context reading.

Current preferred direction:

- use the Agent Skills standard;
- create Repo Control II-specific skills;
- keep skills modular and progressively disclosed;
- avoid giant always-loaded instruction blocks.

Potential early skills include:

```text
repo-control-preflight
milestone-reconnaissance
milestone-implementation
milestone-validation
milestone-closeout
git-readonly-inspection
```

Ponytail may be evaluated as an optional coding-behavior skill focused on avoiding unnecessary abstractions and overengineering.

## 7.4 Repository Intelligence

Purpose:

```text
Provide deterministic structural understanding of the live repository
so AI does not need to repeatedly infer architecture from raw files.
```

Desired information includes:

- files;
- modules;
- classes;
- functions;
- imports;
- references;
- calls;
- inheritance;
- tests;
- dependency relationships;
- changed structural relationships;
- relevant neighborhood around a symbol or subsystem.

Current candidates include:

- Graphify;
- DevLens.

Evaluation remains preferred over immediate adoption.

A repository-intelligence engine should ideally support both:

```text
baseline committed structure
and
current working-tree structure
```

Repo Control II should sit in front of repository-intelligence engines rather than exposing raw internal data directly to every consumer.

## 7.5 Model Gateway

Purpose:

```text
Route AI tasks to appropriate local or cloud models when routing provides
a real benefit in cost, latency, privacy, specialization, or capability.
```

Current candidates include:

- LiteLLM;
- OmniRoute.

This remains a later arc.

The current bottleneck is primarily workflow/context movement, not model-provider selection.

---

# 8. AI Role Model

Repo Control II should preserve distinct AI roles.

## 8.1 Architect

Current preferred Architect:

```text
ChatGPT browser / ChatGPT Plus
```

Architect responsibilities include:

- product and system architecture;
- milestone decomposition;
- implementation strategy;
- safety and authority boundaries;
- acceptance criteria;
- coder prompt composition;
- coder-question resolution;
- closeout review;
- implementation acceptance guidance;
- next-milestone planning.

The accepted, operational integration architecture provides this capability through the Secure MCP Tunnel (M003.006):

```text
Product Owner asks Architect (ChatGPT)
        ↓
Architect invokes Repo Control MCP tools
(repo_status, repo_milestone, repo_prompt, repo_workflow, repo_git, etc.)
        ↓
Secure MCP Tunnel (tunnel-client daemon + repoctl mcp-architect)
        ↓
Repo Control loopback API (http://127.0.0.1:8765/api/architect/v1/*)
        ↓
Deterministic project/milestone/Git evidence returned to Architect
        ↓
Architect reasons over bounded evidence
```

The Architect remains in ChatGPT without requiring manual copy/paste of repository state, prompt files, or Git status. In offline or degraded tunnel situations, the generated Architect Review handoff packet serves as an accepted fallback.

Repo Control does not need to replace the ChatGPT browser experience.

## 8.2 Coder

The Coder operates in VS Code or another approved coding-agent environment.

Potential coding-agent platforms include:

- GitHub Copilot;
- Codex;
- Cline;
- OpenCode;
- Aider;
- Goose;
- OpenHands;
- other compatible coding agents.

Repo Control II should avoid unnecessary dependence on one specific coding agent.

The Coder should consume:

```text
active milestone
+
standing Agent Skills
+
targeted repository evidence
+
approved reconnaissance
```

and should produce:

```text
implementation
+
tests
+
structured closeout/report
+
final read-only Git status
```

## 8.3 Product Owner

The Product Owner remains responsible for:

- product direction;
- prioritization;
- architecture approval;
- milestone approval;
- resolving escalations;
- user testing;
- implementation acceptance;
- Git mutations;
- final workflow transitions with material consequences.

Repo Control II should reduce Product Owner clerical work without reducing Product Owner authority.

---

# 9. Deterministic Truth Before AI Interpretation

A central Repo Control II design principle is:

```text
Prefer deterministic state over AI inference whenever deterministic state exists.
```

Examples:

```text
Git status
→ Git

branch and commit identity
→ Git

remote state
→ Git / GitHub

repository relationships
→ repository-intelligence engine

test pass/fail
→ test runner

workflow state
→ Repo Control

milestone artifacts
→ tracked repository documents

AI
→ interpretation, architecture, planning, review, bounded implementation
```

AI should not be asked to reconstruct facts that reliable tools can provide directly.

This principle supports reliability, explainability, context efficiency, repeatability, reduced hallucinated repository state, and reduced unnecessary model usage.

---

# 10. Historical Manual Workflow

The baseline manual working pattern across earlier projects was:

```text
1. Product Owner discusses milestone with ChatGPT / Architect.

2. Architect composes milestone prompt.

3. Product Owner copies or saves prompt into repository.

4. Product Owner switches to VS Code.

5. Product Owner directs coding agent to prompt.

6. Coder inspects, implements, tests, and prepares closeout.

7. Product Owner transfers coder questions or closeout back to Architect.

8. Architect reviews implementation evidence.

9. Product Owner uses Repo Control for routine Git review, staging,
   commit, branch, fetch, update, and push operations.
```

While functional, this model placed the entire state-transfer and copy/paste burden onto the Product Owner.

The M003 Workflow Orchestration arc was created to replace manual copy/paste with controlled tool access while preserving deliberate Product Owner review gates.

---

# 11. Operational Workflow Model (M003 Baseline)

With M003.011 closed and M003.012 introducing compressed single-user browser actions, the operational workflow is:

```text
Product Owner & Architect (ChatGPT)
→ discuss milestone objectives
→ Architect queries Repo Control via read-only MCP evidence tools
→ Architect may submit a bounded proposal to Repo Control
→ Product Owner reviews and approves/rejects the proposal in Repo Control
→ approved guidance becomes available through local coder-context
→ Product Owner selects milestone in Repo Control UI (PLANNING phase)

Product Owner
→ reviews prompt and lock-ins in Repo Control UI
→ deliberately submits Approve & Start: PLANNING → READY → IN_PROGRESS
  (prompt_approved and implementation_started remain distinct ordered events)

Coder (VS Code / Coding Agent)
→ preflight inspection (read-only git status, diff, log)
→ reviews prompt, coding_agent_rules.md, and project architecture (or Coder Handoff Packet)
→ confirms IN_PROGRESS authorization before implementation
→ performs bounded implementation and tests (strictly no Git mutations)

    ↕ iterative milestone loop

    question / clarification
    stop condition
    escalation / unexpected repository reality
    bug discovery / test failure
    recorded in milestone workflow sidecar (*_workflow.md)
    optional WAITING phase during blocking questions/decisions
    prompt addendum / clarification / binding lock-in
    resumed implementation (IN_PROGRESS)

Coder
→ completes validation and executes test suite
→ writes structured closeout artifact (M<arc>.<seq>_<name>_closeout.md)
→ reports final read-only Git status
→ submits the closeout through coder-closeout: IN_PROGRESS → REVIEW

Architect (ChatGPT)
→ queries Repo Control via read-only evidence MCP tools:
  * repo_milestone / repo_workflow (lifecycle, events, binding lock-ins)
  * repo_closeout (implementation report)
  * repo_evidence (recorded test outcomes)
  * repo_git (cleanliness, modified/untracked files, upstream status)
  * repo_document (governing document alignment)
  (Fallback: Product Owner pastes Architect Review Packet if tunnel unavailable)
→ evaluates implementation against prompt, architecture, and rules
→ recommends acceptance, bounded correction, or follow-up milestone

Product Owner
→ reviews Architect recommendation
→ performs guarded Git staging, commit, and push via Repo Control UI (or manual Git)
→ deliberately submits Accept & Close: REVIEW → CLOSED
  (acceptance_recorded and milestone_closed remain distinct ordered events)
```

The iterative exchange between prompt and closeout is intentional.

In WAITING, Resolve & Resume records the active question answer or escalation resolution, an optional distinct binding lock-in, and implementation_resumed in one atomic sidecar update. Normal lifecycle browser actions use a fixed reported Product Owner context: no actor dropdown or extra confirmation checkbox. The deliberate action button is authorization, not authenticated identity. Polling, edited-form stale protection, and the separate Architect dispatch approval boundary remain intact.

The prompt begins the milestone, but the active milestone may evolve through controlled questions, answers, clarifications, escalation decisions, bug classifications, validation findings, and approved lock-ins.

Repo Control II should preserve those decisions as durable milestone evidence rather than relying on chat history alone.

The milestone should remain open while the approved objective can still be completed safely within its boundaries.

When a discovery materially changes the objective, architecture, authority, risk, or scope, the system should support stopping or suspending the milestone and creating a separately scoped follow-up rather than silently broadening the original work.

The exact mechanism should be developed incrementally.

Repo Control II should not attempt to automate the entire target flow before the individual components prove useful.

---

# 12. Structured Project Artifacts

Repo Control II should continue using human-readable Markdown as the normal project artifact format.

A future milestone prompt may use a small metadata header such as:

```yaml
---
repo_control_artifact: milestone_prompt
project: repo-control-ii
milestone: "R003"
status: ready_for_coder
version: 1
---
```

A coder closeout may similarly use small predictable metadata where it materially improves orchestration.

This concept is not yet a locked implementation requirement.

The governing principle remains:

```text
human-readable first
machine-readable where useful
machine-only only when necessary
```

---

# 13. External Tool Strategy

Repo Control II should not recreate mature specialized tools without a clear reason.

Preferred strategy:

```text
adopt open standards
+
integrate open-source engines
+
customize the workflow layer
+
fork only when configuration/integration cannot meet the requirement
```

Current investigation areas:

| Capability | Current Candidates |
|---|---|
| Agent procedure | Agent Skills |
| Coding restraint | Ponytail as optional skill |
| Repository intelligence | Graphify, DevLens |
| Model gateway | LiteLLM, OmniRoute |
| Coding agent | Copilot, Codex, Cline, OpenCode, Aider, Goose, OpenHands |

The initial Git Control decision is now to continue with Repo Control's browser-based Linux service rather than adopt a desktop Git client as the primary control surface.

Selection of future tools should be based on controlled evaluation, not marketing claims.

---

# 14. Local AI Position

Repo Control II is not primarily a local-AI project.

Local AI may become useful for bounded coding tasks, repository classification, repetitive summaries, low-risk transformations, private/offline processing, and cost reduction where quality remains acceptable.

The project should not assume that a local model can replace frontier-model architectural reasoning.

A future benchmark may compare:

```text
current cloud coding workflow
vs.
open coding agent + viable local model
vs.
open coding agent + strong cloud model
```

using completed historical milestones as controlled benchmark material.

This remains future evaluation work.

---

# 15. Repository Intelligence Position

Repository intelligence should be treated as a deterministic evidence layer, not as another AI assistant.

The desired model is:

```text
Repo Control
        │
        ├── Git
        ├── repository-intelligence engine
        ├── project documents
        ├── milestone state
        └── test results
```

The Architect and Coder should ideally consume the same underlying repository facts.

Potential future capabilities include querying symbol relationships, identifying affected tests, inspecting dependencies, comparing structural state before and after implementation, detecting coupling or renamed symbols, and comparing committed baseline with working-tree structure.

Large raw graph dumps should not be inserted into model context when targeted queries are sufficient.

---

# 16. Git Authority

Repo Control II retains the established Product Owner Git boundary.

Coding agents may use read-only Git commands for preflight, status, diffs, branch identification, commit inspection, and change reporting.

Coding agents do not normally perform:

```text
git add
git commit
git push
git tag
git merge
git rebase
git reset
git stash
git clean
git switch
git checkout
branch creation
branch deletion
remote deletion
```

The Product Owner performs Git mutations after review.

Repo Control II now provides the Product Owner with a guarded browser interface for the accepted routine Git subset.

That does not transfer authority away from the Product Owner.

---

# 17. Documentation Model

Repo Control II uses five global context documents:

```text
docs/context/project_context.md
docs/context/project_architecture.md
docs/context/project_workflow.md
docs/context/coding_agent_rules.md
docs/context/canonical_parking_lot.md
```

Their intended roles are:

```text
project_context.md
→ current project/product truth and near-term direction

project_architecture.md
→ durable system boundaries, ownership, interfaces, and invariants

project_workflow.md
→ collaboration process between Product Owner, Architect, Coder, Repo Control, and Git

coding_agent_rules.md
→ standing rules for implementation agents

canonical_parking_lot.md
→ future, deferred, conditional, and not-yet-prioritized work
```

Milestone prompts and closeouts preserve implementation-history truth.

Git preserves repository-history truth.

Global documents should describe current project truth rather than accumulate every historical event.

---

# 18. Roadmap

The current high-level roadmap is:

```text
R001 — Baseline / Inherited Code Reconnaissance              COMPLETE
R002 — Git Control                                           CLOSED
R003 / M003 — Workflow Orchestration                         ACTIVE
  ├─ M003.001: Architecture Reconnaissance & Contract Design  COMPLETE
  ├─ M003.002: Milestone Discovery & Lifecycle Engine         COMPLETE
  ├─ M003.003: Coder & Architect Handoff Packets              COMPLETE
  ├─ M003.004: Read-Only Architect API Contract               COMPLETE
  ├─ M003.005: Transport Evaluation & Security Hardening      COMPLETE
  ├─ M003.006: Secure MCP Tunnel & Persistent Runtime         ACCEPTED
  ├─ M003.007: MCP Integration & Governing Reconciliation     COMPLETE
  └─ M003.011: Bounded Architect Dispatch Transport           ACTIVE (acceptance pending)
R004 — Agent Procedure / Skills                              PLANNED
R005 — Repository Intelligence Evaluation / Integration     PLANNED
R006 — Deeper Architect / Coder Workflow Integration         LATER
R007 — Model Gateway Evaluation / Routing                   LATER
```

The preferred capability order is:

```text
1. Git Control                         COMPLETE
2. Workflow Orchestration              ACTIVE (M003.011 dispatch transport; formal acceptance pending)
3. Agent Procedure / Skills            PLANNED
4. Repository Intelligence             PLANNED
5. Deeper Architect / Coder Integration LATER
6. Model Gateway                       LATER
```

Rationale:

```text
Git Control
→ immediate proven value and now accepted operational foundation

Workflow Orchestration
→ reduces copy/paste and state transfer through read-only Architect evidence and Product Owner-gated dispatch transport

Agent Procedure
→ reduces repeated prompting and coding-agent drift

Repository Intelligence
→ adds deterministic structural evidence when the workflow can consume it

Architect / Coder Integration
→ connects proven workflow and evidence layers

Model Gateway
→ optimize model selection only after workflow/context problems are solved
```

---

# 19. Near-Term Work

The current implementation work is:

```text
M003.011 — Bounded Architect-to-Coder Dispatch Transport
```

Objectives:

- preserve existing read-only Architect evidence tools and add bounded PROMPT / INSTRUCTION / RESPONSE proposal submissions and status readback;
- require explicit Product Owner approval before proposals become Coder-authoritative;
- surface approved applicable guidance through the local Coder CLI without lifecycle or Git mutation;
- reconcile governing documents with the implemented authority and active workflow-sidecar rules;
- complete M003.011 validation and formal acceptance;
- prepare for R004 (Agent Procedure / Skills).

---

# 20. Resolved and Open Questions

## 20.1 Workflow State Model (Resolved)

The accepted six-state lifecycle model is:

```text
PLANNING
→ READY
→ IN_PROGRESS
      ↔ WAITING
→ REVIEW
→ CLOSED
```

Transitions are recorded in the per-milestone workflow sidecar. Unresolved blocking conditions gate transitions into READY and REVIEW. Product Owner approval remains required for major state changes.

## 20.2 Workflow State Persistence (Resolved)

Milestone workflow state is persisted in tracked chronological Markdown sidecars (`*_workflow.md`) beside each milestone prompt. The active milestone pointer is stored externally in `.repo-control/selected-milestone.json` outside repository source files.

## 20.3 ChatGPT / Architect Integration (Resolved)

Architect integration is operational via the Secure MCP Tunnel (M003.006 and bounded dispatch extension M003.011):

- OpenAI `tunnel-client` daemon running as persistent systemd service `repo-control-architect-tunnel.service`;
- custom stdio JSON-RPC 2.0 adapter `repoctl mcp-architect`;
- 10 existing read-only evidence tools remain mapped to Repo Control's loopback GET endpoints;
- bounded `PROMPT`, `INSTRUCTION`, and `RESPONSE` proposal tools use fixed POST endpoints; `repo_dispatches` provides bounded status/content readback;
- Product Owner approval is performed only in the Repo Control browser UI; pending content is not Coder-authoritative, and prompt approval creates only a canonical artifact under the configured milestone root;
- Repo Control strictly bound to `127.0.0.1:8765` with zero LAN or public exposure;
- runtime credentials stored securely outside the repository under `~/.config/repo-control/tunnel.env` (mode 0600);
- manual Architect Review Packets retained as an operational fallback.

## 20.4 Coder Integration (Active Direction)

Coding agents consume the active milestone prompt, `coding_agent_rules.md`, governing documents, and applicable Product Owner-approved Architect INSTRUCTION/RESPONSE dispatches through `repoctl coder-context`, directly or via generated Coder Handoff Packets. Coding agents remain strictly forbidden from executing Git mutations. Next arc (R004) will evaluate modular Agent Skills.

## 20.5 Repository Intelligence Engine (Future Evaluation)

Evaluating Graphify, DevLens, or other suitable tools remains planned for R005.

## 20.6 Agent Skills (Next Arc)

Designing reusable, progressively disclosed Agent Skills for coding agents is planned for R004.

## 20.7 Model Gateway (Deferred)

Model routing via LiteLLM or OmniRoute remains deferred until workflow and procedure layers are mature.

## 20.7 Model Gateway

Determine later whether LiteLLM, OmniRoute, or another solution provides enough benefit to justify integration.

Do not prioritize this ahead of current workflow and repository-context problems.

---

# 21. Project Principles

Repo Control II should follow these standing principles:

```text
Deterministic facts before AI inference.

Product Owner remains authority for important mutations.

Architect and Coder remain distinct roles.

A milestone is a bounded work container, not a single prompt-response transaction.

Questions, clarifications, stop issues, bug discoveries, and bounded corrections may iterate inside one milestone.

Material changes in objective, architecture, risk, authority, or scope require explicit re-scope or follow-up work.

Human-readable project artifacts remain primary.

Standing rules should not be repeated in every milestone.

Repository context should be queried, not dumped.

Use mature external tools when they solve the problem well.

Do not preserve legacy code merely because it exists.

Do not build speculative abstractions before a real requirement exists.

Reconnaissance should produce an implementation roadmap.

Implementation should be bounded by approved scope.

Validation should provide evidence.

AI should interpret trusted evidence rather than reconstructing basic state.

Token efficiency must not come at the expense of safety or correctness.

Operational convenience should not weaken authentication or mutation safeguards.
```

---

# 22. Definition of Success

Repo Control II will be successful if it materially improves the Product Owner's actual project workflow.

The intended experience is:

```text
I can see what changed.
I can understand repository and branch state.
I can manage routine Git without memorizing exact command syntax.
Repo Control is available whenever I need it without manually starting the server application.
The Architect can obtain current project evidence without me manually copying it.
The Coder can obtain the approved milestone and standing procedure without me manually carrying it.
The Architect can review implementation evidence without me reconstructing repository state by hand.
AI receives only the context it needs.
I remain in control of important repository and workflow decisions.
```

A feature that does not improve that workflow, reduce risk, improve understanding, or reduce repeated context should not be considered valuable merely because it is technically sophisticated.

---

# 23. Current Baseline Statement

Repo Control II is now beyond the initial workflow-foundation phase.

The project currently has:

```text
an authoritative Linux-hosted Git repository;
a working GitHub remote using dedicated SSH authentication;
accepted governing context and architecture documents;
an always-on systemd service (repo-control.service) bound to 127.0.0.1:8765;
loopback-only browser access through an automatic Windows SSH tunnel;
a portable Windows launcher package;
a server-controlled multi-repository allowlist;
an accepted browser-based routine Git control surface;
guarded local Git mutations and verified noninteractive GitHub Push;
Product Owner operational acceptance of the R002 Git Control arc;
recursive milestone prompt discovery and external selection pointer;
chronological tracked workflow sidecars with an accepted six-state lifecycle engine;
on-demand generation of Coder and Architect handoff packets;
a versioned Architect JSON API on loopback with read-only evidence and fixed bounded proposal-submission routes;
a persistent Architect Secure MCP Tunnel service (repo-control-architect-tunnel.service);
verified live read-only Architect evidence operations plus Product Owner-gated dispatch transport;
Product Owner operational acceptance of the M003.001–M003.006 milestones.
```

M003.011 adds bounded Architect dispatch proposals with explicit Product Owner approval and local Coder CLI consumption. Formal acceptance is pending; subsequent workflow enhancements must preserve the existing authority boundaries.
