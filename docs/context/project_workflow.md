# PROJECT_WORKFLOW.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Standing project workflow and collaboration model  
**Project phase:** Git Control complete; Workflow Orchestration implemented through M003.011 (bounded Architect dispatch implementation in progress; formal milestone acceptance pending)
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`
**Current workflow baseline:** Product Owner-directed, milestone-based development with ChatGPT as Architect/Planner querying Repo Control via the Secure MCP Tunnel, coding agents as implementers adhering strictly to read-only Git boundaries, Repo Control II as the workflow/Git control plane, and Product Owner ownership of Git mutations and consequential workflow approvals
**Current workflow emphasis:** Use read-only Architect MCP evidence tools plus a separate bounded proposal channel requiring Product Owner approval before Coder consumption; maintain strict six-state lifecycle discipline and reconcile governing documents with the implemented system.

---

# 1. Purpose

This document defines how Repo Control II is planned, implemented, reviewed, validated, accepted, committed, and advanced.

It defines the working relationship among:

- **Product Owner**
- **ChatGPT / Architect and Planner**
- **Coder / Implementation Agent**
- **Repo Control II**
- **Git / GitHub**
- **governing project documents**
- **milestone artifacts**
- **test and validation tooling**
- **future repository-intelligence and Agent Skills layers**

The workflow exists to keep development milestone-driven, bounded, understandable, evidence-based, recoverable, resistant to scope drift and mixed commits, explicit about authority, efficient in AI context usage, portable across coding agents, durable across chat/tool transitions, and suitable for incremental automation.

Repo Control II should reduce clerical handoff work without removing Product Owner control from consequential decisions.

---

# 2. Core Workflow Principle

The project workflow is not:

```text
prompt
→ implementation
→ closeout
```

It is:

```text
discussion / planning
→ approved prompt
→ explicit milestone selection
→ Coder preflight
↔ questions
↔ clarifications
↔ stop conditions
↔ escalations
↔ bug discoveries
↔ test failures
↔ Architect decisions
↔ Product Owner decisions
↔ binding lock-ins
→ resumed implementation
→ validation
→ closeout
→ Architect review
→ Product Owner acceptance
→ Product Owner Git action
→ milestone closure
```

A milestone is a **bounded work container**, not a single prompt-response transaction.

Multiple exchanges may occur inside one milestone so long as the approved objective, architecture, authority, and risk boundary remain intact.

When a discovery materially changes those boundaries, the milestone must stop, suspend, be re-scoped, or spawn a follow-up milestone rather than silently expanding.

---

# 3. Standing Workflow Principles

Repo Control II uses these standing principles:

```text
Product intent comes first.

Architecture precedes implementation when uncertainty is material.

Reconnaissance precedes implementation when repository reality is unclear.

Implementation follows approved scope.

Questions and clarification loops are normal.

Stop conditions are useful signals, not failures.

Validation establishes evidence.

Closeout records actual outcome.

Architect review interprets implementation evidence.

Product Owner retains authority over consequential decisions.

Git history preserves logical work units.

Deterministic state should be consulted before asking AI to infer it.

Tracked Markdown remains the durable project evidence layer.

Lifecycle state and event chronology are distinct.

Standing instructions should not be repeated unnecessarily in every milestone.

Milestone prompts should describe the current delta.

Context should be targeted rather than repeatedly reloaded.

Scope must not silently broaden.

Automation should remove clerical transport, not authority.

AI efficiency must not weaken safety or correctness.
```

---

# Part I — Roles and Responsibilities

# 4. Product Owner

The Product Owner:

- defines product goals and priorities;
- decides intended user behavior;
- approves architecture and milestone sequencing;
- reviews and approves milestone prompts;
- explicitly selects the active milestone in Repo Control;
- approves binding lock-ins where required;
- resolves product-level ambiguity;
- approves material scope changes;
- resolves or approves escalation outcomes;
- performs user testing where appropriate;
- accepts or rejects milestone completion;
- owns Git mutations;
- approves destructive or risky operations;
- decides which external tools are adopted;
- decides when to suspend, split, or close a milestone.

The Product Owner should progressively stop serving as the manual transport mechanism among Architect, Coder, repository state, tests, and Git.

Repo Control should reduce that clerical burden while leaving decision authority intact.

---

# 5. ChatGPT / Architect and Planner

The Architect:

- helps define product behavior;
- designs system architecture;
- identifies invariants and authority boundaries;
- decomposes work into milestones;
- chooses milestone mode;
- determines when reconnaissance is required;
- writes implementation-ready milestone prompts;
- defines scope and out-of-scope boundaries;
- defines stop and escalation conditions;
- defines expected validation;
- answers Coder questions;
- distinguishes clarification from scope change;
- interprets unexpected repository evidence;
- reviews closeouts;
- reviews test and Git evidence;
- recommends acceptance, correction, escalation, or follow-up;
- proposes documentation changes;
- recommends next milestones.

The Architect should use current deterministic project evidence where available.

Standard live transport path:

```text
Architect (ChatGPT)
→ queries Repo Control directly through read-only MCP tools
  (repo_status, repo_milestone, repo_prompt, repo_workflow, repo_git, etc.)
→ receives bounded current evidence deterministically
→ reasons over live evidence
```

Operational fallback path:

```text
Product Owner
→ generates Architect Review Packet in Repo Control
→ provides bounded packet to Architect
→ Architect reviews milestone evidence from packet
```

The Architect should not rely on chat memory when current project documents, milestone artifacts, Git facts, or test evidence can be queried directly.

---

# 6. Coder / Implementation Agent

The Coder:

- reads the active milestone and verifies implementation authorization directly via `repoctl coder-context` or `repoctl coder-status`;
- reads the current Coder Packet when provided as an export/fallback;
- follows standing coding-agent rules;
- performs read-only Git/repository preflight;
- confirms repository and branch;
- performs reconnaissance when requested;
- inspects targeted code;
- implements approved scope;
- runs required validation and records test results via `repoctl coder-test`;
- asks focused questions or records blockers via `repoctl coder-question`;
- stops when a stop condition is reached;
- escalates architecture, authority, safety, or scope conflicts;
- records bug discoveries via `repoctl coder-bug`;
- resumes only when sufficient clarification or approval is available and the workflow is resumed;
- prepares one closeout and submits it for review via `repoctl coder-closeout`;
- reports final read-only Git state.

Standard live transport path (co-located Coder):

```text
Coder
→ queries Repo Control directly for active milestone, prompt, and authorization via `repoctl coder-context`
→ checks status via `repoctl coder-status`
→ submits bounded events via `coder-question`, `coder-bug`, `coder-test`, and `coder-closeout`
```

Operational fallback path (disconnected or external Coder):

```text
Product Owner
→ generates Coder Handoff Packet in Repo Control
→ provides bounded packet to Coder
→ Coder works from packet instructions
```

The Coder does not:

- redefine product behavior;
- silently broaden scope;
- silently change architecture;
- create speculative abstractions;
- perform Product Owner Git mutations;
- ignore stop conditions;
- conceal failed tests;
- treat incomplete validation as success.

---

# 7. Repo Control II

Repo Control II is the shared deterministic control plane.

It currently owns or exposes:

- configured project/repository identity;
- explicit repository selection;
- explicit active-milestone selection;
- milestone artifact discovery;
- lifecycle phase reconstruction;
- durable workflow event history;
- Product Owner-approved lock-ins;
- waiting reasons;
- bug/event chronology;
- closeout discovery;
- Coder handoff packet generation;
- Architect review packet generation;
- current Git evidence;
- routine Product Owner Git controls.

Repo Control should not make product or architecture decisions by itself.

Its job is to preserve, validate, assemble, and expose trusted state so the appropriate role can decide.

---

# Part II — Governing Documents and Project State

# 8. Governing Documents

Repo Control II uses these project-level authorities:

```text
docs/context/project_context.md
docs/context/project_architecture.md
docs/context/project_workflow.md
docs/context/coding_agent_rules.md
docs/context/canonical_parking_lot.md
```

Their roles are:

```text
project_context.md
→ current project/product truth and near-term direction

project_architecture.md
→ durable architecture, authority boundaries, interfaces, and invariants

project_workflow.md
→ collaboration process among Product Owner, Architect, Coder, Repo Control, and Git

coding_agent_rules.md
→ standing implementation-agent procedure and constraints

canonical_parking_lot.md
→ future, deferred, conditional, or not-yet-prioritized work
```

These are **standing project context**, not milestone artifacts.

Repo Control may resolve them through server-controlled repository configuration.

---

# 9. Documentation Authority

When sources conflict, use this general priority:

```text
1. explicit current Product Owner direction
2. active milestone prompt and Product Owner-approved binding lock-ins
3. current architecture / workflow / coding-agent rules
4. current deterministic repository evidence
5. approved reconnaissance closeout
6. prior milestone prompts and closeouts
7. old chat recollection
8. agent assumptions
```

If a conflict is material, stop and surface it.

Do not silently choose one interpretation.

---

# 10. Milestone Root and Canonical Naming

For new Repo Control-managed work, the preferred milestone root is:

```text
docs/milestones/
```

A repository may configure another server-controlled milestone root where needed for existing projects.

Repo Control recursively discovers milestone artifacts beneath that configured root.

Subfolders are human organization only.

Folder names do not define machine identity.

The canonical forward-looking milestone filename grammar is:

```text
M<arc_id>.<sequence>[.<subsequence>...]_<human_readable_name>_<artifact_type>.md
```

Rules:

```text
M
→ process type = Milestone

arc_id
→ exactly three digits

sequence
→ exactly three digits

optional subsequence components
→ each exactly three digits

human-readable name
→ lowercase snake_case

artifact_type
→ prompt or closeout
```

Examples:

```text
M003.001_workflow_orchestration_reconnaissance_prompt.md
M003.001_workflow_orchestration_reconnaissance_closeout.md

M003.002_minimal_lifecycle_and_durable_decisions_prompt.md
M003.002_minimal_lifecycle_and_durable_decisions_closeout.md

M003.010.001_bounded_followup_prompt.md
```

Historical artifacts are not renamed merely to conform to this standard.

Narrow legacy compatibility may remain where useful, but the `M...` convention is the forward-looking SOP.

---

# 11. Workflow Sidecar

Canonical milestones may have one milestone-scoped workflow sidecar:

```text
<canonical_milestone_basename>_workflow.md
```

Example:

```text
M003.002_minimal_lifecycle_and_durable_decisions_workflow.md
```

The workflow sidecar is:

- tracked;
- human-readable;
- chronological;
- milestone-scoped;
- durable;
- not the prompt;
- not the closeout;
- not the active-milestone selection anchor.

It preserves meaningful workflow events and machine-readable lifecycle history.

Repo Control reconstructs lifecycle from this durable event history.

---

# Part III — Milestone Modes

# 12. Supported Milestone Modes

A milestone should identify its operating mode.

Supported modes include:

```text
reconnaissance-only
implementation-after-reconnaissance
direct low-risk implementation
validation-only
documentation-only
bug-fix follow-up
integration/tool evaluation
workflow/operational validation
```

Do not silently change milestone mode.

---

# 13. Reconnaissance-Only Mode

Use reconnaissance when current implementation is not sufficiently understood, architecture may depend on repository reality, multiple implementation paths remain plausible, persistence implications are unclear, external-tool fit is uncertain, or security/authority boundaries remain unresolved.

Reconnaissance should produce a bounded implementation roadmap.

It is not implementation.

---

# 14. Implementation-After-Reconnaissance Mode

When implementation follows approved reconnaissance, Coder reading should normally be targeted:

```text
1. coding-agent rules / applicable skill
2. active implementation prompt
3. approved reconnaissance closeout
4. approved lock-ins
5. named implementation files
6. directly related tests
7. broader context only when needed
```

Do not repeat broad repository exploration without evidence that the roadmap is stale or incomplete.

---

# 15. Direct Low-Risk Implementation Mode

Use for bounded changes such as narrow UI corrections, text/copy changes, focused tests, documentation-only edits, isolated mechanical changes, and obvious bounded bugs.

Even low-risk work must remain scoped, validate appropriately, preserve Git boundaries, and create the required closeout.

---

# 16. Validation-Only Mode

Use when the goal is evidence rather than implementation.

When validation finds a defect:

```text
record evidence
→ classify impact
→ continue or stop according to prompt
→ do not silently repair
→ recommend bounded repair if needed
```

Validation-only work must not silently become implementation work.

---

# 17. Documentation-Only Mode

Use for context, architecture, workflow, coding-agent rules, parking-lot, and roadmap/handoff updates.

Documentation must describe current truth and approved future direction without inventing implementation that does not exist.

---

# 18. Bug-Fix Follow-Up Mode

A bug may remain inside the active milestone when it is directly related to current work, the fix is bounded, architecture/authority/risk/scope remain intact, and the bug, classification, fix, and validation are recorded.

Create a separate follow-up when the defect materially changes architecture, objective, risk, persistence, tool choice, workflow contract, or validation burden.

---

# 19. Integration / Tool Evaluation Mode

Use when evaluating external capabilities such as Agent Skills, Graphify, DevLens, Ponytail, LiteLLM, OmniRoute, or coding-agent alternatives.

Evaluation should define the problem, realistic workload, acceptance criteria, integration burden, licensing, operational complexity, security implications, and recommendation.

Do not adopt tools merely because they are available.

---

# Part IV — Prompt, Decisions, and Closeout

# 20. Prompt Purpose

A milestone prompt is the initial approved contract.

It should define, as appropriate:

- milestone identity;
- mode;
- goal;
- background;
- repository;
- target environment;
- required context;
- scope;
- out of scope;
- architecture boundaries;
- authority boundaries;
- implementation expectations;
- validation;
- stop conditions;
- escalation conditions;
- deliverables;
- definition of done;
- closeout expectations.

The prompt should describe the current delta rather than restate the whole project.

---

# 21. Prompt Handoff

Current implemented transport:

```text
Architect creates prompt
→ Product Owner saves/commits prompt
→ Product Owner selects milestone in Repo Control
→ Repo Control can generate a Coder Packet
→ Product Owner can copy packet to Coder
```

The packet is derived output, not project authority.

The prompt remains authoritative.

Future direction:

```text
Coder
→ queries Repo Control directly for active milestone and current instructions
```

Direct Coder tool access is not yet implemented.

---

# 22. Routine Clarification vs Binding Decision

The workflow distinguishes three levels.

## 22.1 Routine Conversation

A transient explanation or small wording choice that does not affect future implementation does not require formal durable project evidence.

## 22.2 Durable Decision/Event

Record a meaningful question, answer, decision, blocker, bug, or test event when it changes Coder behavior, resolves a blocker, establishes an assumption, matters to future review, or must survive chat loss.

## 22.3 Product Owner-Approved Lock-In

A binding lock-in is required when guidance supersedes part of the prompt, materially refines an ambiguous requirement, or establishes binding implementation direction while preserving the same milestone objective.

Binding lock-ins require Product Owner approval.

A non-binding decision must not be promoted into authoritative Coder instruction.

---

# 23. Material Scope Change

Stop and escalate when a discovery materially changes:

- objective;
- scope;
- architecture;
- authority;
- security;
- risk;
- persistence;
- implementation strategy;
- validation burden;
- product behavior.

Do not hide material change inside casual Q&A.

Re-scope or create a separate milestone.

---

# 24. Closeout Standard

Each milestone normally produces one human-authored closeout.

Preferred structure:

```text
# Milestone <ID> — <Title>

## 1. Scope Completed
## 2. Resulting Behavior
## 3. Files Changed
## 4. Architecture / Integration Notes
## 5. Validation Performed
## 6. Questions / Decisions Incorporated
## 7. Deviations from Prompt
## 8. Known Limitations
## 9. Deferred / Follow-Up Work
## 10. Recommended Next Milestone
## 11. Git Status
```

A closeout does not become accepted merely because it exists.

---

# Part V — Implemented Lifecycle

# 25. Current Lifecycle

The implemented Repo Control lifecycle is:

```text
PLANNING
READY
IN_PROGRESS
WAITING
REVIEW
CLOSED
```

This six-state model supersedes earlier conceptual state names such as `READY_FOR_CODER`, `CODING`, `READY_FOR_ARCHITECT_REVIEW`, `APPROVED`, and `READY_FOR_GIT`.

---

# 26. Lifecycle Meanings

## PLANNING
The selected milestone is not yet approved for Coder implementation.

## READY
The prompt has been explicitly approved for Coder handoff.

## IN_PROGRESS
Implementation or validation is actively underway.

## WAITING
Work is paused for a recorded clarification, decision, blocker, escalation, or other valid reason.

## REVIEW
Closeout/evidence is available for Architect/Product Owner review.

## CLOSED
The milestone has been explicitly accepted and closed by Product Owner action.

`CLOSED` is terminal.

---

# 27. Lifecycle Transition Rules

Normal transitions are explicit:

```text
PLANNING → READY
→ Product Owner-approved prompt approval event

READY → IN_PROGRESS
→ explicit implementation-start event

IN_PROGRESS → WAITING
→ blocking question / escalation / out-of-scope bug / valid waiting reason

WAITING → IN_PROGRESS
→ resolution recorded
→ explicit implementation-resumed event

IN_PROGRESS → REVIEW
→ closeout exists and is explicitly submitted

REVIEW → IN_PROGRESS
→ bounded correction requested

REVIEW → CLOSED
→ explicit Product Owner acceptance
→ explicit closure
```

Do not infer transitions from prompt existence, closeout existence, Git cleanliness, commits, pushes, or passing tests.

---

# 28. Workflow Event Model

The implemented event vocabulary includes:

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

Events preserve chronology and are not automatically lifecycle states.

---

# 29. Waiting and Resume

`WAITING` requires a reason.

A valid resolution event must occur before a separate `implementation_resumed` event returns the milestone to `IN_PROGRESS`.

The milestone record should preserve what stopped work, what resolution occurred, what remains out of scope, and what implementation is now authorized.

---

# 30. Bug Handling

Supported bounded flow:

```text
bug_discovered
→ bug_classified
→ bug_fixed
→ test_result_recorded
```

Classification distinguishes `in_scope` from `needs_escalation`.

An out-of-scope bug can place the milestone into `WAITING`.

The system must not silently expand the milestone.

---

# 31. Product Owner Approval Boundaries

Explicit Product Owner action is required for at least:

- selecting the active milestone;
- approving the prompt into `READY`;
- approving a binding lock-in;
- approving material re-scope;
- accepting implementation;
- closing the milestone;
- staging;
- committing;
- pushing;
- other consequential Git mutations.

Repo Control currently records actor role as reported context. It does not claim cryptographic role authentication.

---

# Part VI — Coder Interface and Handoff Packets

# 32. Coder Interface and Packet Options

Repo Control II provides two complementary mechanisms for delivering instructions and workflow state to Coder agents:

1. **Coder Local CLI Interface (Primary for Co-Located Agents):**
   Established in M003.009, local coding agents running on `henderson-server1` (via VS Code Remote SSH) interact directly with Repo Control via the local CLI (`repoctl coder-context`, `repoctl coder-status`). This in-process execution avoids monolithic document bundling, provides deterministic standing document references, checks implementation authorization, and provides bounded event submission with zero network overhead.

2. **Coder Handoff Packet (Fallback / Export):**
   Repo Control can generate a derived, read-only monolithic Coder Packet containing:

```text
repository / milestone identity
current lifecycle phase
authoritative prompt
Product Owner-approved binding lock-ins
current blockers / waiting reason
coding-agent rules
standing workflow rules
repository guidance
explicit Git mutation prohibition
source references
```

The packet does not replace the prompt or governing documents.

Packet generation does not change lifecycle or Git state.

---

## 32.1 Coder Local CLI Workflow and Bounded Event Submission

Co-located coding agents execute under strict boundary constraints (Model B write boundary):

- **Context Retrieval:** `repoctl coder-context` retrieves the authoritative milestone prompt and checks lifecycle authorization before coding begins.
- **Status Check:** `repoctl coder-status` provides lightweight inspection of lifecycle phase, blockers, lock-ins, and open questions without re-reading the prompt.
- **Question Submission:** `repoctl coder-question "<summary>" [--blocking] [--category ...]` records implementation questions. If marked `--blocking`, the milestone transitions from `IN_PROGRESS` to `WAITING` until answered and resumed by the Product Owner.
- **Defect Discovery:** `repoctl coder-bug "<summary>"` records defects discovered during work without granting classification authority.
- **Validation Evidence:** `repoctl coder-test "<summary>" --source "<cmd>" --result "<outcome>"` records test execution outcomes, labeled as reported evidence.
- **Closeout Submission:** `repoctl coder-closeout [--summary "<summary>"]` verifies the matching closeout artifact exists and transitions the milestone from `IN_PROGRESS` to `REVIEW`.

**Enforced Invariants:**
- All Coder CLI commands hardcode `actor_role="coder"` and `po_approved=False`.
- The Coder CLI provides no generic `coder-event` command.
- The Coder CLI cannot execute Product Owner actions (`prompt_approved`, `lock_in_approved`, `implementation_resumed`, `acceptance_recorded`, `milestone_closed`).
- Absolute prohibition on Git mutations remains strictly enforced.

---

# 33. Architect Review Packet

Repo Control can generate a derived, read-only Architect Review Packet containing, as appropriate:

```text
repository / milestone identity
current lifecycle phase
authoritative prompt/objective
binding lock-ins
relevant workflow events
open blockers / escalations / bugs
closeout
deterministic local Git state
reported test evidence
configured context / architecture / workflow documents
source references
```

Reported test-result events are labeled as reported unless independently verified.

While the persistent Secure MCP Tunnel (`repo_review`, `repo_milestone`, `repo_closeout`, `repo_git`, etc.) is now the standard live evidence retrieval path for ChatGPT Architect sessions, the Architect Review Packet remains an accepted and maintained fallback for offline review, portable export, or degraded network conditions.

---

# 34. Packet Authority and Freshness

Before generation Repo Control revalidates repository identity, repository generation, selected milestone, prompt, workflow sidecar, closeout if used, configured governing documents, and current local Git state.

Stale repository or milestone requests are rejected.

Packet generation must not append workflow events, alter lifecycle, alter selection state, modify milestone artifacts, or mutate Git.

---

# 35. Packet Context Discipline

The current packet implementation intentionally favors completeness over aggressive summarization.

The long-term goal is:

```text
smallest sufficient context
+
current milestone delta
+
binding decisions
+
only relevant standing rules
+
current deterministic evidence
```

Future packet refinement should be driven by real usage rather than speculative trimming.

---

# Part VII — Standard Development Cycle

# 36. Step 1 — Discuss and Define Milestone

Product Owner and Architect define objective, milestone mode, acceptance criteria, scope, out of scope, and expected evidence.

Architect drafts the milestone prompt.

Product Owner reviews and approves it.

---

# 37. Step 2 — Save and Commit Prompt

The prompt is saved under the configured milestone root using canonical naming for new work.

Product Owner commits the prompt through Repo Control or manual Git.

Coder does not perform this Git mutation.

---

# 38. Step 3 — Select Active Milestone

Product Owner explicitly selects the milestone in Repo Control.

Repo Control does not infer active status from newest filename, modification time, highest milestone number, missing closeout, or Git history recency.

---

# 39. Step 4 — Approve Handoff

For lifecycle-managed milestones:

```text
PLANNING
→ Product Owner approves prompt
→ READY
```

Repo Control records the approval event in the milestone workflow sidecar.

---

# 40. Step 5 — Coder Handoff and Interface

Primary standard path (co-located Coder):

```text
Coder runs `repoctl coder-context` directly
→ verifies implementation authorization
→ receives authoritative prompt, standing references, and binding lock-ins
```

Secondary / fallback path (disconnected or external Coder):

```text
Repo Control assembles Coder Packet
→ Product Owner copies packet to Coder
```

Direct Coder local CLI access is implemented in M003.009 for co-located agents on `henderson-server1`:
- local co-located Coders use `repoctl coder-context` and `repoctl coder-status` for direct context and status retrieval;
- bounded Coder workflow submission commands are implemented (`coder-question`, `coder-bug`, `coder-test`, `coder-closeout`);
- Coder Handoff Packets remain supported as a fallback/export mechanism for disconnected or external use;
- Architect continues using read-only MCP via the Secure MCP Tunnel;
- Architect may submit bounded PROMPT, INSTRUCTION, and RESPONSE drafts through separate typed MCP tools; dispatches remain pending until Product Owner approval;
- only approved INSTRUCTION/RESPONSE dispatches for the active milestone are surfaced by the local Coder CLI; prompt approval creates only a canonical prompt artifact;
- Product Owner retains sole authority for prompt approvals, lock-in approvals, workflow resume, acceptance, milestone closure, and all Git mutations.

---

# 41. Step 6 — Coder Preflight

Coder confirms correct repository, branch, working-tree state, active milestone, milestone mode, required standing rules, required reconnaissance, relevant files, and validation strategy.

The active milestone's `*_workflow.md` sidecar is expected Repo Control lifecycle state and may be untracked or modified during an active milestone. It does not by itself violate the clean-working-tree precondition. Any other unexplained dirty path remains a blocker and must be reported.

Do not clean, stash, reset, revert, or discard them.

---

# 42. Step 7 — Start Implementation

```text
READY
→ implementation_started
→ IN_PROGRESS
```

Coder performs the smallest safe change satisfying approved scope.

---

# 43. Step 8 — Iterative Question / Stop / Bug Loop

During implementation the Coder may encounter a question, clarification, stop condition, escalation, bug discovery, test failure, or unexpected repository evidence.

If blocking:

```text
IN_PROGRESS
→ WAITING
```

Architect and Product Owner respond as appropriate.

Meaningful decisions are recorded durably.

After valid resolution:

```text
WAITING
→ implementation_resumed
→ IN_PROGRESS
```

This loop may repeat.

---

# 44. Step 9 — Validate

Validation may include unit, integration, CLI, browser/UI, Git behavior, static, Product Owner manual, external-tool, repository-intelligence, and Git status/diff checks.

Validation should match milestone risk.

---

# 45. Step 10 — Closeout

Coder creates one closeout reflecting actual implementation, actual validation, incorporated decisions, deviations, limitations, and final read-only Git state.

---

# 46. Step 11 — Submit for Review

```text
IN_PROGRESS
→ closeout_submitted
→ REVIEW
```

Closeout presence alone does not trigger review automatically.

---

# 47. Step 12 — Architect Review

Standard path:

```text
Architect (ChatGPT) invokes read-only evidence MCP tools:
- repo_milestone / repo_workflow (phase, events, binding lock-ins)
- repo_closeout (review implementation report)
- repo_git (inspect working tree cleanliness, modified/untracked files, HEAD)
- repo_evidence (inspect reported test outcomes)
- repo_document (verify governing document compliance)
- repo_review (compact aggregate summary)
```

Fallback path:

```text
Product Owner generates Architect Review Packet in Repo Control UI
and pastes it into ChatGPT.
```

Architect reviews prompt, binding lock-ins, relevant events, closeout, Git evidence, test evidence, and governing context where useful.

Architect-to-Coder proposals use separate typed MCP submission tools. Repo Control stores each proposal as PENDING; the Product Owner reviews the complete content in the Architect Dispatches page and explicitly approves or rejects it. Approval does not trigger lifecycle events. Approved applicable guidance is delivered by `repoctl coder-context`; PROMPT approval only creates a canonical milestone prompt under the configured milestone root.

Possible recommendations:

```text
ACCEPT
BOUNDED CORRECTION
ESCALATE
FOLLOW-UP MILESTONE
REJECT / REWORK
```

---

# 48. Step 13 — Bounded Correction Loop

If review identifies bounded work:

```text
REVIEW
→ correction_requested
→ IN_PROGRESS
```

Coder performs only the approved bounded correction.

Then the closeout is resubmitted and the milestone returns to `REVIEW`.

A material change requires escalation or a new milestone.

---

# 49. Step 14 — Product Owner Acceptance

Product Owner reviews the Architect recommendation, performs user testing if needed, accepts or rejects implementation, decides follow-up timing, and authorizes Git completion.

Acceptance is explicit.

---

# 50. Step 15 — Git Completion

Git mutation remains Product Owner-owned.

Typical sequence:

```text
review exact files
→ stage intended files
→ inspect staged state
→ commit
→ push
```

Coder does not own these actions.

---

# 51. Step 16 — Milestone Closure

Normal closure requires approved scope complete, required validation complete, closeout present, Architect review complete, Product Owner acceptance, Git action complete or explicitly deferred, and follow-up work captured.

Repo Control then records explicit acceptance/closure events:

```text
REVIEW
→ acceptance_recorded
→ milestone_closed
→ CLOSED
```

---

# Part VIII — Git Workflow Rules

# 52. Product Owner Git Ownership

Coding agents do not perform normal Git mutations.

Prohibited for coding agents:

```text
git add
git commit
git push
git reset
git rebase
git merge
git tag
git checkout
git switch
git stash
git clean
branch creation/deletion
remote mutation
```

Read-only Git inspection is expected.

---

# 53. Exact-File Staging

Product Owner should prefer specific-file staging over broad commands such as `git add .`.

Normal review includes:

```bash
git status --short
git diff --name-only
git diff --stat
```

Repo Control should make exact-file review and staging understandable and explicit.

---

# 54. Local vs Remote State

Repo Control should clearly distinguish:

```text
local working tree
local index
local branch
local HEAD
upstream tracking ref
remote repository
GitHub state
```

When local ahead/behind is based on tracking refs without remote refresh, that fact should be explicit.

---

# Part IX — Context and AI Efficiency

# 55. Context Loading Principle

AI should receive the smallest sufficient context.

Preferred Coder context:

```text
active milestone prompt
+
approved binding lock-ins
+
standing coding-agent procedure
+
current workflow state
+
targeted repository evidence
+
relevant validation expectations
```

Preferred Architect review context:

```text
milestone objective
+
approved decisions
+
relevant event history
+
closeout
+
Git state
+
test evidence
+
targeted governing context
```

---

# 56. Governing Context vs Current Work Context

Distinguish:

```text
STANDING PROJECT CONTEXT

project_context.md
project_architecture.md
project_workflow.md
coding_agent_rules.md
canonical_parking_lot.md
```

from:

```text
CURRENT WORK CONTEXT

active milestone prompt
workflow sidecar
binding lock-ins
closeout
current Git/test evidence
```

Repo Control should assemble context according to task need rather than always sending everything.

---

# 57. Current Packet Limitation

The current M003.003 packet implementation favors correctness/completeness and may include large standing documents in full.

That is acceptable as an initial transport proof.

Future optimization should reduce repeated standing content, preserve authoritative wording where needed, avoid silent summarization of binding instructions, use source references, and be driven by real usage evidence.

---

# Part X — External Tool Workflow

# 58. Agent Skills

Agent Skills remain the intended reusable procedure layer.

Until Agent Skills are implemented and validated, `coding_agent_rules.md` remains the standing Coder procedure source.

---

# 59. Repository Intelligence

Repository intelligence should provide deterministic structural evidence for targeted questions such as what calls a function, what tests cover a module, what depends on a class, and what changed structurally.

Candidate engines remain Graphify and DevLens.

---

# 60. Model Gateway

Model routing remains a later concern.

Do not redesign workflow around LiteLLM, OmniRoute, or local models before a concrete routing need exists.

---

# Part XI — Stop, Escalation, and Reset

# 61. Stop Conditions

Stop when the wrong repository or branch is active, unexpected dirty files threaten isolation, required dependencies are missing, repository reality materially contradicts the prompt, unauthorized Git mutation would be required, material scope expansion appears necessary, validation cannot be completed, a destructive operation appears necessary, project isolation cannot be guaranteed, or only speculative solutions remain.

Do not silently work around stop conditions.

---

# 62. Escalation Protocol

Use a structure such as:

```text
STATUS: ESCALATION REQUIRED

Observed conflict:
Approved assumption that failed:
Evidence:
Files / systems involved:
Why proceeding would broaden scope or increase risk:
Smallest safe options:
Recommended decision:
Incomplete changes, if any:
```

Stop at the escalation point.

---

# 63. Controlled Reset Principle

If repeated patch/retry cycles increase uncertainty:

```text
stop
→ preserve evidence
→ identify trusted baseline
→ classify what is worth keeping
→ reconstruct only accepted behavior
→ revalidate through bounded gates
```

---

# Part XII — Current Workflow Baseline

# 64. Implemented Workflow Capabilities

Repo Control II now has:

```text
canonical milestone root configuration
recursive milestone discovery
canonical Mxxx.xxx[.xxx...] naming
explicit active-milestone selection
repository-scoped milestone pointer
six-state lifecycle
durable workflow sidecar
question / answer / decision events
binding Product Owner lock-ins
bug and test events
waiting / resume behavior
closeout submission
review / correction loop
explicit acceptance and closure
Coder handoff packet
Architect review packet
configured governing-document roles
current local Git evidence in review packet
bounded Architect PROMPT / INSTRUCTION / RESPONSE dispatch proposals
Product Owner browser approval and bounded status readback
approved applicable Architect dispatches in local Coder context
```

---

# 65. Operational Transport Model

The operational transport architecture now distinguishes Architect from Coder workflows:

- **Architect (ChatGPT):** Uses the persistent Secure MCP Tunnel for 10 unchanged read-only evidence tools, bounded `repo_dispatches` readback, and separate typed PROMPT / INSTRUCTION / RESPONSE submissions that create only pending proposals. Product Owner approval remains in Repo Control's browser UI; Architect Review Packets remain an on-demand fallback.
- **Coder (VS Code):** Co-located Coders use the local CLI for authoritative prompt, standing references, authorization, and approved applicable Architect dispatches via `repoctl coder-context`; status via `repoctl coder-status`; and bounded workflow events via existing `coder-*` commands. Coder Handoff Packets remain supported as a fallback/export mechanism for disconnected or external use. The Coder remains bound to read-only Git commands.
- **Product Owner:** Remains in full control of milestone selection, prompt approval (PLANNING → READY), resume decisions, review acceptance (REVIEW → CLOSED), and all Git mutations (staging, commit, push).

---

# 66. Near-Term Direction

With read-only Architect evidence tools, bounded dispatch transport, and local Coder CLI workflow interfaces operational, the next workflow enhancements focus on:

1. **Procedure:** Establishing reusable Agent Skills (R004) to standardize coding-agent behavior without prompt repetition.
2. **Context & Packet Optimization:** Evaluating whether Coder Handoff Packets can be trimmed or made modular without losing authoritative instruction.
3. **Phase-Aware Milestone UI:** Hardening the Milestone page controls based on operational usage (WORKFLOW-001).

Bounded local Coder write access is implemented in M003.009; future Coder MCP / remote Coder transport remains deferred as WORKFLOW-003, and multi-agent coordination remains deferred.

---

# 67. UX Direction

The current Milestone page proves the workflow model but is not yet the desired final UX.

The universal event-entry form is functionally complete but operationally clunky.

Do not optimize it prematurely.

After several real milestone runs, use observed behavior to redesign toward phase-aware controls, for example:

```text
PLANNING
→ Approve prompt

READY
→ Start implementation

IN_PROGRESS
→ Ask question
→ Record bug
→ Record test result
→ Submit closeout

WAITING
→ Record answer / decision
→ Resume

REVIEW
→ Request correction
→ Accept milestone
```

UX hardening should preserve the backend event model rather than redesign workflow semantics merely for appearance.

---

# 68. Workflow Success Criteria

The workflow is improving when:

- the Product Owner copies fewer artifacts manually;
- the Architect receives current bounded evidence;
- the Coder receives current authoritative instructions;
- standing rules are repeated less often;
- questions and decisions are durably preserved;
- lifecycle status is visible rather than remembered;
- implementation reviews require less reconstruction;
- Git state is deterministic and understandable;
- AI context becomes more targeted;
- Product Owner authority remains explicit;
- no automation silently broadens scope or mutates Git.

---

# 69. Current Baseline Statement

Repo Control II has completed the foundational Workflow Orchestration arc.

The current operational baseline includes:

```text
Product Owner-directed milestone workflow
ChatGPT as Architect querying Repo Control via persistent Secure MCP Tunnel
VS Code coding agents as Coder under strict read-only Git discipline
Linux authoritative repositories on henderson-server1
GitHub remote repositories with dedicated Deploy Key authentication
Product Owner-owned Git mutations via guarded Repo Control UI
browser-based Git Control (stage, unstage, restore, commit, branch, push)
explicit multi-repository selection allowlist
explicit active-milestone selection pointer (.repo-control/selected-milestone.json)
recursive milestone prompt discovery beneath configured milestone_root
canonical milestone naming (Mxxx.xxx[_name]_prompt.md)
durable workflow lifecycle and event ledger (*_workflow.md)
accepted six-state lifecycle (PLANNING, READY, IN_PROGRESS, WAITING, REVIEW, CLOSED)
on-demand Coder handoff packets and Architect review packets
Architect JSON API on loopback with read-only evidence and fixed pending-proposal endpoints
persistent Secure MCP Tunnel daemon (repo-control-architect-tunnel.service)
10 read-only evidence MCP tools plus bounded dispatch submission and readback
Product Owner-gated dispatch approval and local Coder consumption
manual review packets retained as operational fallback
Agent Skills not yet implemented (planned for R004)
repository intelligence not yet integrated (planned for R005)
local Coder CLI workflow interface and bounded event submission implemented (M003.009)
remote Coder MCP transport deferred (WORKFLOW-003)
```

The next milestones focus on encoding reusable Agent Skills (R004) and evaluating repository intelligence (R005) while preserving the Product Owner as the sole authority for consequential decisions.
