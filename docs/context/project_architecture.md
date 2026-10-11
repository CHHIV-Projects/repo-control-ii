# PROJECT_ARCHITECTURE.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Durable system architecture, authority boundaries, integration model, and architectural invariants  
**Project phase:** Git Control operationally accepted; Workflow Orchestration next  
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`  
**Current architecture baseline:** Repo Control II now has an accepted browser-based Git Control layer, always-on Linux service runtime, multi-repository allowlist, Windows SSH access model, and noninteractive GitHub authentication. The next architecture focus is Workflow Orchestration.  
**Current architectural emphasis:** preserve deterministic Git authority and Product Owner control while reducing manual transfer of milestone, review, test, and repository state between Product Owner, Architect, and Coder.

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

This is the next central custom layer.

Responsibilities include:

- active project state;
- active milestone identity;
- milestone phase;
- prompt location;
- prompt addenda;
- coder questions;
- Architect answers;
- Product Owner decisions;
- escalation state;
- bug-discovery records;
- validation state;
- closeout location;
- review state;
- acceptance state;
- Git-ready state;
- closure state.

Candidate high-level states:

```text
PLANNING
→ READY_FOR_CODER
→ CODING
      ↔ WAITING_FOR_CLARIFICATION
      ↔ ESCALATION_REQUIRED
      ↔ BLOCKED
      ↔ VALIDATING
→ READY_FOR_ARCHITECT_REVIEW
→ APPROVED
→ READY_FOR_GIT
→ CLOSED
```

This remains conceptual.

R003 should determine the smallest useful state model rather than prematurely implementing a generalized workflow engine.

---

# 12. Workflow Event Architecture

Repo Control II should distinguish **workflow state** from **workflow events**.

Example:

```text
state:
CODING

events:
- prompt approved
- coder question submitted
- architect answer recorded
- Product Owner decision recorded
- implementation resumed
- test failed
- bug classified
- validation resumed
```

This separation preserves chronology without requiring a unique state for every event.

The workflow engine should remain simple and explicit.

Avoid a generalized workflow framework unless a concrete requirement justifies it.

---

# 13. Milestone Artifact Architecture

## 13.1 Prompt

The milestone prompt is the initial approved contract.

It should remain human-readable.

Small structured metadata may be added when useful.

## 13.2 Addenda and Decision Trail

The initial prompt is not necessarily the final authoritative state by itself.

The milestone may accumulate:

- coder questions;
- Architect answers;
- Product Owner decisions;
- clarifications;
- stop/resume decisions;
- scope lock-ins;
- bug classification;
- validation clarifications;
- final implementation lock-ins.

Repo Control should preserve those events in chronological order.

## 13.3 Closeout

The closeout is the final implementation/validation evidence package for the milestone.

It should contain:

- scope completed;
- operational behavior;
- files changed;
- tests and validation;
- deviations;
- known limitations;
- unresolved items;
- final Git status;
- recommended next action.

A closeout does not erase the intermediate decision trail.

## 13.4 Artifact Authority

Milestone artifacts are durable project evidence.

They should not exist only inside chat history.

---

# 14. Architect Integration Layer

Current preferred Architect:

```text
ChatGPT browser / ChatGPT Plus
```

Responsibilities:

- retrieve bounded project facts;
- retrieve active milestone;
- inspect Git/repository evidence;
- compose milestone artifacts;
- answer coder questions;
- resolve or frame escalations;
- review closeouts;
- recommend acceptance or follow-up;
- propose Git actions without silently executing them.

The preferred architecture remains:

```text
ChatGPT
→ controlled Repo Control tools
→ local deterministic project evidence
```

rather than immediately replacing ChatGPT with a custom hosted chat client.

The Architect should not require unrestricted shell access to obtain ordinary project facts.

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

# 28. R003 Workflow Orchestration Reconnaissance Requirements

The next architecture milestone should answer:

```text
What is the minimum useful workflow state model?

How should Repo Control identify the active milestone?

What state can be inferred directly from tracked artifacts and Git?

What state must be stored operationally?

How should prompt, addenda, questions, decisions, and closeout be related?

Can tracked Markdown remain the primary authority?

What transitions must require Product Owner action?

How should READY_FOR_CODER, READY_FOR_ARCHITECT_REVIEW,
READY_FOR_GIT, and CLOSED be represented?

How should coder questions and escalation events be preserved?

How should Repo Control surface milestone state without becoming
an autonomous project manager?

How can the Product Owner stop acting as the manual transport layer
without collapsing Architect and Coder roles?

What is the smallest useful implementation slice after reconnaissance?
```

The milestone should begin with reconnaissance, not workflow-engine implementation.

---

# 29. Current Open Architecture Questions

## 29.1 Workflow Persistence

Tracked Markdown versus lightweight operational state remains open.

## 29.2 Active Milestone Identity

The mechanism for identifying the active milestone remains to be designed.

## 29.3 Architect Integration Mechanism

The preferred direction is controlled tools from ChatGPT to Repo Control, but the exact mechanism remains open.

## 29.4 Coder Integration Mechanism

Agent Skills are preferred for procedure, but the active-milestone handoff mechanism remains open.

## 29.5 Repository Intelligence Engine

Graphify versus DevLens remains unselected.

## 29.6 Agent Skills Packaging

The relationship between Repo Control workflow state and reusable Agent Skills remains to be designed.

## 29.7 UI Framework

Flask is the accepted current implementation framework, but replacement remains possible if a later requirement demonstrates material benefit.

## 29.8 Model Gateway

LiteLLM versus OmniRoute remains deferred.

---

# 30. Definition of Architectural Success

The architecture is successful when the normal project flow becomes:

```text
Product Owner expresses intent.

Architect can inspect trusted current project state.

Architect creates a bounded milestone.

Repo Control stores and exposes it.

Coder consumes the milestone and standing skills.

Coder and Architect can iterate through questions,
clarifications, stops, bugs, and decisions without
the Product Owner manually transporting every artifact.

Repo Control preserves the decision trail.

Coder validates and closes out.

Architect reviews current repository/test evidence.

Product Owner approves.

Repo Control makes Git state and next actions clear.

Product Owner performs the Git mutation.

The milestone closes with durable project evidence.
```

The system should make this workflow easier, safer, and less repetitive without removing the Product Owner from consequential decisions.

---

# 31. Current Architecture Baseline Statement

Repo Control II now has an accepted operational architecture for its first major capability.

The current baseline includes:

```text
authoritative Linux-hosted repository
always-on systemd service
loopback-only browser service
automatic Windows SSH tunnel access
portable Windows launcher
server-controlled multi-repository allowlist
browser-based routine Git control
prepared-action safeguards
post-action verification
safe local branch and remote operations
noninteractive GitHub SSH authentication
repository-scoped Deploy Key
verified Repo Control Push
Product Owner / Architect / Coder role separation
deterministic-evidence principle
human-readable milestone artifacts
explicit iterative milestone-loop requirement
```

The next architecture task is not to add more Git features.

It is to define the smallest useful Workflow Orchestration model that can sit on top of this accepted foundation and reduce manual handoff without introducing unnecessary persistence, agent coupling, or autonomous behavior.
