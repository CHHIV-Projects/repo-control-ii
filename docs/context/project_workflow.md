# PROJECT_WORKFLOW.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Standing project workflow and collaboration model  
**Project phase:** Clean restart and architecture definition  
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `https://github.com/CHHIV-Projects/repo-control-ii.git`  
**Current workflow baseline:** Product Owner-directed, milestone-based development with ChatGPT as Architect/Planner, coding agents as implementers, and Product Owner ownership of Git mutations  
**Current workflow emphasis:** preserve deliberate review and authority gates while reducing manual transport of prompts, questions, clarifications, closeouts, test evidence, and repository state between Architect and Coder.

---

# 1. Purpose

This document defines how Repo Control II is designed, implemented, reviewed, validated, and advanced.

It defines the working relationship among:

- **Product Owner**
- **ChatGPT / Architect and Planner**
- **Coder / Implementation Agent**
- **Repo Control II**
- **Git / GitHub**
- **Repository-intelligence tools**
- **Agent Skills**
- **test and validation tooling**

The workflow exists to keep development milestone-driven, bounded, understandable, evidence-based, recoverable, resistant to scope drift and mixed commits, explicit about authority, efficient in AI context usage, portable across coding agents, durable across chat/tool transitions, and suitable for incremental automation.

Repo Control II should reduce clerical handoff work without removing Product Owner control from consequential decisions.

---

# 2. Core Workflow Principle

The core development loop is not:

```text
prompt
→ implementation
→ closeout
```

It is:

```text
initial prompt
→ coder work
↔ questions
↔ clarifications
↔ stop conditions
↔ escalations
↔ bug discoveries
↔ test failures
↔ Architect decisions
↔ Product Owner decisions
↔ prompt addenda / lock-ins
→ resumed implementation
→ validation
→ closeout
→ Architect review
→ Product Owner acceptance
→ Git action
→ milestone closure
```

A milestone is a **bounded work container**, not a single prompt-response transaction.

Multiple exchanges may occur inside one milestone as long as the approved objective, architecture, authority, and risk boundary remain intact.

When a discovery materially changes those boundaries, the milestone should stop, suspend, or be re-scoped rather than silently expanding.

---

# 3. Working Principles

Repo Control II uses the following standing workflow principles:

```text
Product intent comes first.
Architecture precedes implementation when uncertainty is material.
Reconnaissance precedes implementation when code reality is unclear.
Implementation follows approved scope.
Questions and clarification loops are normal.
Stop conditions are useful signals, not failures.
Validation establishes evidence.
Closeout records actual outcome.
Architect review determines whether evidence satisfies the milestone.
Product Owner retains authority over consequential decisions.
Git history preserves logical work units.
Deterministic state should be consulted before asking AI to infer it.
Standing procedure belongs in reusable rules/skills.
Milestone prompts should describe the current delta.
Context should be targeted rather than repeatedly reloaded.
Scope must not silently broaden.
AI efficiency must not weaken safety or correctness.
```

---

# Part I — Roles and Responsibilities

# 4. Product Owner

The Product Owner:

- defines product goals and priorities;
- decides intended user behavior;
- approves architecture and milestone sequencing;
- reviews milestone prompts;
- resolves product-level ambiguity;
- approves material scope changes;
- approves important workflow transitions;
- reviews escalation decisions;
- tests completed behavior where appropriate;
- accepts or rejects milestone completion;
- owns Git mutations;
- approves destructive or risky operations;
- decides which external tools are adopted;
- decides whether inherited subsystems are kept, refactored, or retired;
- decides when to suspend, split, or close a milestone.

The Product Owner should not be required to manually reconstruct repository state for the Architect, repeat standing rules to the Coder, carry every prompt or closeout between tools, translate product intent into low-level implementation instructions alone, or memorize Git command syntax for ordinary project operation.

Repo Control II should progressively reduce these clerical burdens.

---

# 5. ChatGPT / Architect and Planner

The Architect:

- helps define product behavior;
- designs system architecture;
- identifies architectural invariants;
- decomposes work into milestones;
- chooses milestone mode;
- determines when reconnaissance is required;
- writes implementation-ready milestone prompts;
- defines scope and out-of-scope boundaries;
- defines stopping and escalation conditions;
- defines expected validation;
- answers coder questions;
- interprets unexpected repository evidence;
- distinguishes clarification from scope change;
- interprets coder closeouts;
- reviews tests and Git evidence;
- recommends acceptance or follow-up;
- proposes documentation changes;
- recommends Git actions;
- helps maintain project continuity across chats and tools.

The Architect should use deterministic project evidence where available:

```text
Product Owner asks Architect
→ Architect queries Repo Control
→ Repo Control returns project/repository evidence
→ Architect reasons over bounded evidence
```

The Architect should not rely on chat memory when current project documents, Git state, test evidence, milestone artifacts, or repository-intelligence results are available.

---

# 6. Coder / Implementation Agent

The Coder:

- reads the active milestone;
- loads applicable Agent Skills;
- performs read-only Git/repository preflight;
- confirms repository and branch;
- performs reconnaissance when requested;
- inspects targeted code;
- implements approved scope;
- runs required validation;
- asks focused questions when genuinely blocked;
- stops when a stop condition is reached;
- escalates architecture, authority, safety, or scope conflicts;
- reports bug discoveries;
- resumes only when sufficient clarification or approval is provided;
- prepares one closeout;
- reports final read-only Git state.

The Coder should not:

- redefine product behavior;
- silently broaden scope;
- silently change architecture;
- create speculative abstractions;
- preserve legacy implementation merely because it exists;
- create parallel systems where a current authority can be reused;
- perform Product Owner Git mutations;
- ignore stop conditions;
- conceal failed tests;
- treat incomplete validation as success.

---

# 7. Repo Control II

Repo Control II should eventually act as the shared control plane.

Responsibilities may include:

- project identity;
- repository location;
- active milestone;
- milestone state;
- prompt location;
- prompt addenda;
- Q&A trail;
- escalation records;
- Product Owner decisions;
- coder closeout location;
- test evidence;
- Git state;
- repository-intelligence access;
- Architect review state;
- Git-ready state;
- milestone closure.

Repo Control should not make product or architecture decisions by itself. It should preserve and expose state so the appropriate human or AI role can make the decision.

---

# Part II — Documentation and Project State

# 8. Governing Documents

Repo Control II begins with:

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
→ durable architecture, authority, boundaries, and invariants

project_workflow.md
→ how project work moves between Product Owner, Architect, Coder, tools, and Git

coding_agent_rules.md
→ standing implementation-agent procedure and constraints

canonical_parking_lot.md
→ future, deferred, conditional, or not-yet-prioritized work
```

Milestone prompts and closeouts preserve milestone-history truth. Git preserves repository-history truth.

---

# 9. Documentation Authority

When sources conflict, use this general priority:

```text
1. explicit current Product Owner direction
2. active milestone prompt and approved addenda
3. current architecture/workflow/coding-agent rules
4. current repository code and deterministic evidence
5. approved reconnaissance closeout
6. prior milestone prompts and closeouts
7. old chat recollection
8. agent assumptions
```

If a conflict is material, do not silently choose one interpretation. Stop and surface the conflict.

---

# 10. Milestone Documentation Location

Milestone artifacts belong under:

```text
docs/milestones/
```

The exact folder structure may evolve, but prompt and closeout files for the same milestone should remain easy to locate together.

A future structure may use:

```text
docs/milestones/R001/
docs/milestones/R002/
```

or a more descriptive variation.

The structure should optimize for discoverability, chronological use, machine parsing, human readability, and low naming ambiguity.

---

# Part III — Milestone Model

# 11. Milestone Modes

A milestone should identify its operating mode.

Supported modes should include at least:

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

Do not silently change modes.

---

# 12. Reconnaissance-Only Mode

Use when inherited code is not understood, architecture may depend on implementation reality, multiple approaches appear plausible, external-tool fit is uncertain, persistence implications are unclear, integration boundaries are unclear, implementation could duplicate existing capability, or security/authority boundaries are unresolved.

Reconnaissance should produce:

```text
current implementation map
relevant files and functions
current behavior
dependencies
authority boundaries
candidate reuse
candidate retirement
integration options
risks
tests
recommended implementation shape
exact likely files to change
stop conditions
```

Reconnaissance is not implementation. The expected outcome is a usable implementation roadmap.

---

# 13. Implementation-After-Reconnaissance Mode

Use after an approved reconnaissance result.

Coder reading order should normally be:

```text
1. applicable Agent Skills / coding-agent rules
2. active implementation prompt
3. approved reconnaissance closeout
4. approved addenda / lock-ins
5. named implementation files
6. directly related tests
7. broader context only when needed
```

The Coder should not repeat broad repository exploration without evidence that the reconnaissance is stale or incomplete.

---

# 14. Direct Low-Risk Implementation Mode

Use for bounded work such as text/copy changes, narrow UI corrections, isolated tests, simple mechanical updates, obvious local bug fixes, and documentation changes.

Even small work must remain scoped, avoid unrelated cleanup, run relevant validation, preserve Git boundaries, and create the required closeout.

---

# 15. Validation-Only Mode

Use when the goal is to establish evidence without changing implementation.

Examples:

- inherited baseline testing;
- Git behavior validation;
- repository-intelligence benchmarking;
- tool integration evaluation;
- performance benchmarking;
- workflow-state validation.

When validation finds a defect:

```text
record evidence
classify impact
continue or stop according to prompt
do not silently repair
recommend separate repair if needed
```

Validation-only work must not silently become implementation work.

---

# 16. Documentation-Only Mode

Use for context updates, architecture updates, workflow updates, coding-agent rule updates, parking-lot updates, and roadmap/handoff documents.

Documentation should describe actual decisions and current truth. It should not invent implementation that has not occurred.

---

# 17. Bug-Fix Follow-Up Mode

A bug may remain inside the active milestone when it is directly caused by the current implementation, fixing it does not materially broaden scope, architecture and authority remain unchanged, and risk remains bounded.

Create a separate follow-up when the fix materially changes architecture, objective, risk, persistence, tool choice, workflow contract, or validation burden.

---

# 18. Integration / Tool Evaluation Mode

Use when comparing external capabilities such as GitButler, Graphify, DevLens, Agent Skills, Ponytail, LiteLLM, OmniRoute, or coding agents.

Evaluation should define:

```text
question being answered
candidate tools
test repository or workload
acceptance criteria
integration burden
licensing
performance
fit with architecture
risk
recommendation
```

Do not adopt a tool solely because it appears feature-rich.

---

# Part IV — Prompt and Closeout Standards

# 19. Prompt Purpose

A milestone prompt is the initial approved contract for work.

It should define:

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

The prompt should be complete enough to execute without restating the whole project.

---

# 20. Preferred Prompt Structure

```text
# Milestone <ID> — <Title>

## Required Files
## Milestone Mode
## Goal
## Background
## Authoritative Repository
## Required Reading
## Scope
## Out of Scope
## Architecture / Authority Boundaries
## Implementation Requirements
## Testing / Validation
## Stop Conditions
## Escalation Conditions
## Deliverables
## Definition of Done
## Required Closeout Structure
```

Not every milestone requires every heading. Risk-sensitive work should be more explicit.

---

# 21. Prompt Handoff Format

Prompts intended for coding agents should be self-contained, easy to copy or retrieve, preferably one coherent Markdown artifact, free from unnecessary conversational commentary, explicit about command environment, explicit about Git restrictions, and explicit about stop/escalation behavior.

As Repo Control II matures, direct retrieval should replace manual copy/paste.

---

# 22. Active Prompt Evolution

The initial prompt may evolve during implementation.

Normal additions include:

```text
## Coder Questions / Answers Round 1
## Coder Questions / Answers Round 2
## Escalation / Stop Decision 1
## Bug Discovery / Classification 1
## Final Lock-ins
```

A clarification may remain inside the milestone when it explains how to satisfy the original objective.

A material change should not be hidden in a casual Q&A answer.

Repo Control II should preserve these additions durably.

---

# 23. Material Prompt Change

A prompt update is material when it changes:

- objective;
- scope;
- architecture;
- authority;
- Git boundary;
- persistence;
- destructive behavior;
- security;
- tool selection;
- workflow model;
- implementation direction;
- closeout expectation;
- validation burden.

Material changes require explicit Product Owner awareness and should be preserved as milestone evidence.

---

# 24. Closeout Standard

Each milestone should normally produce one human-authored closeout.

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

The closeout must distinguish confirmed facts, assumptions, inferences, untested behavior, deferred work, failed validation, and Product Owner decisions.

A closeout should not claim completion merely because code was written.

---

# Part V — Iterative Milestone Loop

# 25. Normal Iteration Inside a Milestone

```text
Coder begins work
        ↓
New issue or question?
        │
        ├── No → continue
        │
        └── Yes
             ↓
        classify event
             ↓
        question / clarification / stop / escalation / bug / test failure
             ↓
        Architect review
             ↓
        Product Owner decision if required
             ↓
        durable answer / addendum / lock-in
             ↓
        resume or remain blocked
```

This loop may repeat several times. That is expected behavior.

---

# 26. Question / Clarification Event

Use when requirement wording is ambiguous, the Coder needs a bounded implementation choice, product behavior needs clarification, or repository reality exposes a small unanswered detail.

The Architect should answer directly when the decision is technical and already within approved Product Owner intent.

The Product Owner should be involved when the answer changes product behavior, priority, risk, or authority.

---

# 27. Stop Condition Event

A stop condition is a deliberate guardrail.

Examples:

- wrong branch;
- unexpected dirty files;
- missing required dependency;
- repository state contradicts prompt;
- test environment unavailable;
- external integration not installed;
- required evidence cannot be established.

The Coder should stop and report rather than work around the condition silently.

---

# 28. Escalation Event

Use when architecture no longer fits reality, two materially different designs remain, required work is significantly broader than prompt, data/persistence model must change, security boundary must change, tool adoption creates major coupling, required Git/runtime mutation is not authorized, validation cannot establish required safety, or the only solution appears speculative.

Recommended format:

```text
STATUS: ESCALATION REQUIRED

Observed conflict:
Approved assumption that failed:
Evidence:
Files / systems involved:
Why proceeding would broaden or increase risk:
Smallest safe options:
Recommended decision:
Incomplete changes, if any:
```

Stop at the escalation point.

---

# 29. Bug Discovery Event

When a defect is found during implementation:

1. record the defect;
2. determine whether it was introduced by current work or pre-existing;
3. classify whether it is in scope;
4. determine whether it blocks validation;
5. determine whether fixing it changes architecture or risk;
6. either fix within scope or defer to a follow-up.

Do not hide defects inside unrelated cleanup.

---

# 30. Test Failure Event

A failed test is evidence.

Classify it as:

```text
current regression
pre-existing failure
environment/setup failure
incorrect test assumption
unrelated failure
unknown
```

Unknown failures should not automatically be ignored or repaired. Material uncertainty may require escalation.

---

# 31. Resume Decision

After a stop or escalation, implementation resumes only when the necessary decision is sufficiently explicit.

The milestone record should preserve:

- what stopped work;
- who decided;
- what changed;
- what remains prohibited;
- what the Coder may now do.

---

# 32. When to Split the Milestone

Split or suspend when the objective changed, work now spans unrelated systems, architecture must be redesigned, a major external tool decision is required, implementation risk increased materially, Product Owner testing should precede further work, validation and repair should remain separate, a new persistence model is required, or the discovered issue deserves independent acceptance.

Do not split mechanically when a bounded clarification is enough.

---

# Part VI — Standard Development Cycle

# 33. Step 1 — Define Milestone

The Product Owner and Architect define objective, reason for work, milestone mode, acceptance criteria, scope, out of scope, and expected evidence.

Architect drafts the milestone prompt. Product Owner reviews and approves.

---

# 34. Step 2 — Establish Git / Repository Baseline

The Product Owner ensures the intended branch exists and is selected.

The Coder performs read-only preflight:

```bash
cd /home/chuck/projects/repo-control-ii
git branch --show-current
git status --short
git log --oneline --decorate -5
git rev-parse HEAD
```

When upstream exists and matters:

```bash
git rev-parse '@{upstream}'
```

The Coder does not create or switch branches.

---

# 35. Step 3 — Save Milestone Prompt

Current/manual state:

```text
Architect creates prompt
→ Product Owner saves it into repository
```

Target state:

```text
Architect creates approved prompt
→ Repo Control stores it in the correct project/milestone location
```

Repo Control II should eventually remove the manual transport step.

---

# 36. Step 4 — Coder Handoff

Current/manual state:

```text
Product Owner tells Coder where prompt is
```

Target state:

```text
Coder asks Repo Control for active milestone
→ receives prompt + relevant Agent Skills + lock-ins
```

The transition should be incremental.

---

# 37. Step 5 — Coder Preflight

Before editing, the Coder confirms correct repository, correct branch, expected working-tree state, milestone mode, required Agent Skills, required reconnaissance, relevant files, and test strategy.

Unexpected dirty files must be reported.

Do not clean, stash, reset, or discard them.

---

# 38. Step 6 — Reconnaissance or Targeted Inspection

If milestone is reconnaissance, inspect enough to establish the implementation roadmap.

If milestone follows reconnaissance, verify the roadmap, inspect named paths, and expand only when evidence requires it.

Stop broad investigation when relevant authority is known, affected files are known, implementation shape is known, validation is defined, and further searching is unlikely to change the plan.

---

# 39. Step 7 — Implement

The Coder makes the smallest safe change satisfying the milestone.

Prefer existing code, existing patterns, explicit control flow, small helpers, narrow changes, targeted tests, and mature external tools where architecture calls for them.

Avoid speculative frameworks, unrelated cleanup, broad refactors, unnecessary wrappers, duplicate capability, and premature generalization.

---

# 40. Step 8 — Iterative Questions / Stops / Escalations

At any point, the Coder may stop and raise a question, clarification, bug discovery, test failure, architecture conflict, scope issue, or tool/integration problem.

The Architect and Product Owner respond.

The decision is preserved.

The Coder resumes only when the path is clear.

This step may repeat multiple times.

---

# 41. Step 9 — Validate

Validation may include unit tests, integration tests, CLI tests, browser/UI tests, Git behavior checks, static checks, manual Product Owner validation, external-tool benchmark, repository-intelligence comparison, and Git status/diff review.

Validation should match the milestone risk.

---

# 42. Step 10 — Closeout

Coder produces one closeout reflecting actual implementation, actual test outcome, actual deviations, actual limitations, and actual Git state.

It should include decisions made during the milestone where they affected implementation.

---

# 43. Step 11 — Architect Review

The Architect reviews:

```text
milestone prompt
+ addenda / lock-ins
+ closeout
+ relevant Git diff
+ tests
+ repository intelligence where useful
```

Possible outcomes:

```text
ACCEPT
BOUNDED CORRECTION
ESCALATE
FOLLOW-UP MILESTONE
REJECT / REWORK
```

---

# 44. Step 12 — Product Owner Acceptance

The Product Owner reviews the Architect recommendation, performs user testing if needed, accepts or rejects completion, approves Git action, and decides whether follow-up work is immediate or parked.

---

# 45. Step 13 — Git Completion

Git mutation remains Product Owner-owned.

Typical lifecycle:

```text
review exact files
→ stage intended files
→ inspect staged diff/stat
→ commit
→ push
→ tag/merge/branch action if appropriate
```

Repo Control II should progressively provide graphical support for this workflow.

---

# 46. Step 14 — Milestone Closure

A milestone is closed when approved scope is complete, required validation is complete, closeout exists, Architect review is complete, Product Owner accepts, required Git action is complete or explicitly deferred, and follow-up work is captured.

Repo Control should record the final state.

---

# Part VII — Git Workflow Rules

# 47. Product Owner Git Ownership

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
remote deletion
```

Read-only Git is expected.

---

# 48. Exact-File Staging

Product Owner should prefer specific-file staging over:

```bash
git add .
```

Typical review:

```bash
git status --short
git diff --name-only
git diff --stat
```

Repo Control II should make exact-file selection easier through graphical controls.

---

# 49. Branch Lifecycle

Substantial work should normally occur on an approved branch once branch strategy is in use.

Conceptually:

```text
main
→ feature / milestone arc
→ implementation
→ validation
→ documentation
→ merge
→ post-merge validation
```

Do not introduce complex branching merely because Git supports it.

---

# 50. Remote State

Repo Control should clearly distinguish:

```text
local working tree
local branch
local HEAD
upstream branch
remote repository
GitHub state
```

A user should not need to infer these distinctions from raw Git output.

---

# Part VIII — Context and Token Efficiency

# 51. Context Loading Principle

AI should receive the smallest sufficient context.

Preferred stack:

```text
standing Agent Skill / coding-agent rules
+
active milestone
+
approved addenda / lock-ins
+
approved reconnaissance
+
targeted repository evidence
+
relevant tests / Git state
```

Avoid reloading every global document for every implementation task.

---

# 52. Always-Needed Coder Context

At minimum:

- active milestone;
- applicable standing coding-agent procedure;
- current repository path;
- current branch;
- approved lock-ins.

---

# 53. Context Loaded As Needed

Load architecture/context when work touches architecture, workflow state, Git authority, repository-intelligence integration, external tool integration, security, persistence, model gateway, cross-agent behavior, or project-wide UI structure.

---

# 54. Repository Intelligence Use

Use repository intelligence to answer targeted questions:

```text
what calls this function?
what tests cover this module?
what depends on this class?
what changed structurally?
what neighboring code should the Coder inspect?
```

Do not dump the entire graph into every prompt.

---

# 55. Stop Broad Investigation

Stop repository-wide analysis when implementation path is stable, affected files are known, test strategy is known, authority boundaries are clear, and further searching is unlikely to change the plan.

Longer investigation is not inherently better.

---

# Part IX — External Tool Workflow

# 56. Tool Adoption Process

Before adopting a major external tool:

```text
identify current problem
→ define evaluation criteria
→ test candidate
→ compare alternatives
→ review licensing / deployment
→ review architectural fit
→ Product Owner decides
→ integrate incrementally
```

Do not adopt a tool before its intended problem is clear.

---

# 57. Repository Intelligence Evaluation

Graphify and DevLens should be benchmarked against a real repo.

Evaluation should consider installation, indexing, working-tree support, graph accuracy, query quality, refresh, speed, MCP/tool usability, licensing, integration effort, usefulness to Architect, and usefulness to Coder.

---

# 58. Agent Skills Workflow

Agent Skills should carry reusable procedure.

The milestone prompt should contain current scope, not repeated standing instructions.

Skills should be versionable and inspectable.

When a skill changes materially, affected workflows should be revalidated.

---

# 59. Model Gateway Workflow

Model gateway evaluation comes later.

Do not redesign workflow around LiteLLM, OmniRoute, or local models before a concrete routing need exists.

Model choice should follow task requirements rather than drive architecture.

---

# Part X — Inherited Code Workflow

# 60. Legacy Code Is Evidence, Not Authority

The copied Repo Control I implementation is not automatically accepted.

The first reconnaissance should classify major subsystems:

```text
KEEP
REFACTOR
RETIRE
```

The Coder should not start rewriting before this classification is approved.

---

# 61. Salvage Decision Rule

Keep an inherited subsystem when it already provides real value, aligns with new architecture, is understandable, is testable, and retaining it is cheaper and safer than replacement.

Refactor when valuable logic exists but current coupling conflicts with new architecture.

Retire when the capability is no longer needed, an external tool replaces it better, it creates unnecessary complexity, or it exists only because of the old product direction.

---

# 62. Controlled Reset Principle

If implementation enters a repeated patch/retry loop and uncertainty increases:

```text
stop
→ preserve evidence
→ identify trusted baseline
→ classify what is worth keeping
→ reconstruct bounded accepted behavior
→ revalidate
```

Do not continue speculative repairs indefinitely.

---

# Part XI — Reasoning Guidance

# 63. Higher-Reasoning Work

Use deeper architectural reasoning for architecture, inherited-code reconnaissance, Git-control architecture, workflow orchestration, repository-intelligence selection, security boundaries, external tool adoption, persistence design, model gateway architecture, and complex escalations.

The result should be a concrete recommendation.

---

# 64. Medium-Reasoning Work

Appropriate for implementation after reconnaissance, bounded Git UI work, targeted integration, tests, bug fixes with known architecture, closeout preparation, and implementation debugging.

---

# 65. Lower-Complexity Work

Appropriate for documentation formatting, copy changes, small mechanical fixes, simple tests, and narrowly scoped cleanup.

Do not lower rigor when risk is unclear.

---

# Part XII — Workflow State Model

# 66. Candidate High-Level States

Current conceptual states:

```text
PLANNING
→ READY_FOR_CODER
→ CODING
→ READY_FOR_ARCHITECT_REVIEW
→ APPROVED
→ READY_FOR_GIT
→ CLOSED
```

Intermediate substates/events may include:

```text
WAITING_FOR_CLARIFICATION
ESCALATION_REQUIRED
BLOCKED
VALIDATING
```

These are not yet a locked database schema.

---

# 67. State vs Event

Repo Control should distinguish state from chronology.

Example:

```text
current state:
CODING

event history:
prompt approved
coder started
question submitted
architect answered
Product Owner confirmed
implementation resumed
test failed
bug classified
fix completed
validation passed
```

This prevents the workflow model from becoming overly complex.

---

# 68. Workflow Transition Authority

Not every role may perform every transition.

Conceptually:

```text
Architect
→ PLANNING to READY_FOR_CODER recommendation

Coder
→ CODING / WAITING / VALIDATING status updates

Architect
→ READY_FOR_ARCHITECT_REVIEW assessment

Product Owner
→ APPROVED

Product Owner / Repo Control UI
→ READY_FOR_GIT to CLOSED after Git completion
```

Exact permissions remain to be designed.

---

# Part XIII — Near-Term Workflow Transition

# 69. Current Manual Workflow

Today:

```text
Architect creates prompt
→ Product Owner saves/copies it
→ Product Owner directs Coder
→ Coder works
→ Product Owner carries questions back
→ Architect answers
→ Product Owner carries answer to Coder
→ Coder closes out
→ Product Owner brings closeout to Architect
→ Architect reviews
→ Product Owner performs Git
```

This is the baseline that Repo Control II should improve.

---

# 70. Transitional Workflow

Near-term improvements may happen in stages:

```text
Stage 1
standardized files and naming

Stage 2
Agent Skills reduce repeated instructions

Stage 3
Repo Control tracks active milestone

Stage 4
Coder reads active milestone directly

Stage 5
Repo Control stores Q&A / decisions

Stage 6
Architect queries Repo Control directly

Stage 7
Repo Control assembles review evidence

Stage 8
Git UI completes Product Owner action
```

Do not require the final architecture before gaining value from earlier stages.

---

# 71. Target Workflow

```text
Product Owner discusses work with Architect

Architect
→ reads current project state through Repo Control
→ creates milestone

Repo Control
→ stores milestone
→ sets READY_FOR_CODER

Coder
→ reads milestone + skills
→ implements

Coder / Architect / Product Owner
↔ iterate through questions, stops, bugs, decisions

Coder
→ validates
→ creates closeout

Repo Control
→ assembles review evidence

Architect
→ reviews
→ recommends acceptance

Product Owner
→ accepts
→ uses Repo Control Git UI
→ commits/pushes as appropriate

Repo Control
→ closes milestone
```

---

# 72. Workflow Success Criteria

The workflow is improving when:

- the Product Owner copies fewer artifacts manually;
- the Architect has better current repository evidence;
- the Coder reads less irrelevant context;
- standing rules are repeated less often;
- fewer implementation assumptions are rediscovered;
- questions and decisions are durably preserved;
- Git actions are easier to understand;
- milestone boundaries remain clear;
- implementation reviews require less manual reconstruction;
- Product Owner authority remains explicit.

---

# 73. Initial Workflow Baseline

Repo Control II currently begins with:

```text
Product Owner-directed milestone workflow;
ChatGPT as Architect;
VS Code coding agents as Coder;
Linux authoritative repository;
GitHub remote repository;
Product Owner-owned Git mutations;
manual prompt/coder transport still present;
new governing documents being created;
Agent Skills not yet implemented;
repository intelligence not yet selected;
workflow orchestration not yet implemented.
```

The purpose of the next milestones is to improve this workflow incrementally while preserving the controls that already work.
