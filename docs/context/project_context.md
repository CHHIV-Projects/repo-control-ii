# PROJECT_CONTEXT.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Current project/product context and working baseline  
**Project phase:** Clean restart and architecture definition  
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `https://github.com/CHHIV-Projects/repo-control-ii.git`  
**Current branch:** `main`  
**Current repository state:** New Git repository initialized locally; initial commit and first push intentionally deferred until the governing context documents are complete and reviewed  
**Current working emphasis:** define Repo Control II from a clean product perspective, preserve useful inherited Git functionality, classify inherited code before extending it, and build toward a deterministic project-control system that reduces manual handoff between Product Owner, Architect, coding agents, repository state, and Git.

---

# 1. Overview

Repo Control II is a clean redefinition of the original Repo Control project.

The original Repo Control effort explored several ideas, including:

- deterministic repository scanning;
- static relationship analysis;
- context-pack generation;
- repository snapshots;
- local-AI interpretation;
- Git workflow intelligence;
- guarded staging and commit workflows;
- a local browser UI.

In practice, the portion that has demonstrated the clearest value to the Product Owner is **Git management**.

Repo Control II therefore does not assume that every capability from Repo Control I deserves to survive.

The project starts from a new product definition:

```text
Repo Control II is a deterministic project-control and workflow-integration system
for coordinating trusted repository state, Git operations, project documentation,
AI architect workflows, and coding-agent execution while minimizing unnecessary
context transfer and repeated model work.
```

The project is not intended to become:

- a replacement IDE;
- a custom general-purpose AI coding environment;
- a replacement for Git;
- a replacement for GitHub;
- a replacement for specialized repository-analysis engines;
- a replacement for coding agents;
- a replacement for model gateways;
- an autonomous system that removes Product Owner control over important repository mutations.

The architectural direction is instead:

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

Provide a practical graphical control surface for Git operations that reduces dependence on memorized command syntax while preserving explicit Product Owner control.

Desired Git capabilities include:

- working-tree status;
- changed-file visibility;
- file-level diff inspection;
- staged versus unstaged visibility;
- selective staging;
- commit preparation;
- commit execution by the Product Owner;
- commit history;
- branch visualization;
- branch creation and switching;
- ahead/behind state;
- remote awareness;
- tags;
- conflict visibility;
- merge/rebase/reset guidance where appropriate;
- GitHub repository awareness;
- clear indication of local versus remote state.

The project should evaluate mature open-source Git tooling before rebuilding advanced Git behavior unnecessarily.

GitButler is a current evaluation candidate for Git UI concepts and potentially reusable functionality.

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

The Product Owner should remain the decision maker.

Repo Control II should remove unnecessary copy/paste and state-transfer work without collapsing the distinction between Architect and Coder.

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

The current development environment is:

```text
Windows workstation
→ Product Owner workstation
→ ChatGPT browser
→ VS Code client
→ VS Code Remote SSH
→ browser access to local tools where appropriate

Ubuntu mini-server: henderson-server1
→ authoritative editable repositories
→ Repo Control II source
→ Python runtime
→ local development tooling
→ Docker and other server infrastructure where required

GitHub
→ remote repository hosting
→ committed/pushed source history
→ remote branch and collaboration state
```

The authoritative Repo Control II working repository is:

```text
/home/chuck/projects/repo-control-ii
```

The GitHub remote is:

```text
https://github.com/CHHIV-Projects/repo-control-ii.git
```

The local repository was created as a fresh Git repository rather than preserving the old Repo Control `.git` directory.

---

# 4. Repo Control II Starting Baseline

Repo Control II was created by selectively copying the existing Repo Control application code into a new directory.

Copied into Repo Control II:

```text
src/
tests/
README.md
pyproject.toml
.gitignore
```

Not copied:

```text
.git/
.venv/
docs/
```

New directories were created:

```text
docs/context/
docs/milestones/
```

A fresh Python virtual environment was created locally.

Current package baseline:

```text
Python 3.12
package name: repoctl
editable local install
Flask-based current implementation
```

The inherited `README.md`, package description, source code, and tests remain **legacy baseline material** until reviewed.

They must not be treated as the governing definition of Repo Control II merely because they were copied into the new repository.

---

# 5. Inherited Code Status

The inherited Repo Control I implementation is intentionally preserved as a starting point, but it is not automatically accepted architecture.

The first implementation-oriented work should classify inherited components as:

```text
KEEP
→ already useful and compatible with Repo Control II direction

REFACTOR
→ valuable capability exists but is coupled to obsolete architecture

RETIRE
→ capability no longer justifies continued maintenance or conflicts with the new direction
```

The working expectation is that:

- existing Git functionality is the strongest KEEP candidate;
- useful project/repository configuration may also survive;
- deterministic repository-scanning code may be reusable depending on external-tool evaluation;
- snapshot machinery requires justification before reuse;
- local-AI interpretation machinery has not yet earned a place in Repo Control II;
- existing browser UI components should be evaluated based on whether they support the new control-plane design.

No implementation component should be preserved solely because it already exists.

The guiding question is:

```text
What parts of Repo Control I have earned the right to be part of Repo Control II?
```

---

# 6. Product Architecture Direction

Repo Control II is organized around five major capability arcs.

## 6.1 Git Control

Purpose:

```text
Give the Product Owner clear, graphical, trustworthy control of Git.
```

Primary concerns:

- repository state;
- working-tree changes;
- staging;
- commits;
- branch state;
- history;
- remote state;
- GitHub awareness;
- conflict visibility;
- safe Product Owner execution of repository mutations.

Important rule:

```text
AI may inspect, explain, recommend, and prepare.

The Product Owner authorizes and performs important Git mutations.
```

External tooling should be evaluated before duplicating mature Git functionality.

Current candidate:

- GitButler.

## 6.2 Agent Procedure

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

Ponytail is not currently considered a major architectural subsystem.

## 6.3 Repository Intelligence

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

Current candidates:

- Graphify;
- DevLens.

The preferred strategy is evaluation rather than immediate adoption.

A repository-intelligence engine should ideally support both:

```text
baseline committed structure
and
current working-tree structure
```

so Repo Control can eventually identify meaningful structural deltas.

Repo Control II should sit in front of repository-intelligence engines rather than exposing their raw internal data directly to every consumer.

## 6.4 Workflow Orchestration

Purpose:

```text
Coordinate trusted project state between Product Owner,
Architect, Coder, repository, tests, and Git.
```

This is expected to become Repo Control II's most distinctive custom capability.

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

A new or separate milestone should normally be created when the discovered issue materially changes:

- objective;
- scope;
- architecture;
- risk;
- authority;
- persistence;
- Git strategy;
- implementation direction;
- validation burden.

Candidate high-level workflow states:

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

The exact state model remains subject to design and testing.

The orchestration layer should preserve a chronological decision trail inside the active milestone, potentially including:

```text
Initial Prompt

Q&A Round 1
- Coder question
- Architect answer
- Product Owner decision where required

Q&A Round 2
- new evidence
- clarification
- decision / lock-in

Stop / Escalation Event
- observed conflict
- evidence
- decision
- authorization to resume or remain blocked

Bug Discovery
- defect
- in-scope / out-of-scope classification
- handling decision

Final Lock-ins
- authoritative decisions reached during implementation
```

Repo Control should eventually be able to manage structured project artifacts such as:

- milestone prompts;
- prompt addenda;
- coder questions;
- Architect answers;
- Product Owner decisions;
- stop and escalation records;
- bug-discovery records;
- coder reports;
- closeouts;
- test evidence;
- Git status;
- implementation state;
- architect-review state.

Human-readable Markdown remains the preferred artifact format.

Machine-readable metadata may be added in small predictable headers where it materially improves orchestration.

Repo Control II should not require machine-only JSON documents for ordinary project work.

## 6.5 Model Gateway

Purpose:

```text
Route AI tasks to appropriate local or cloud models when routing provides
a real benefit in cost, latency, privacy, specialization, or capability.
```

Current candidates:

- LiteLLM;
- OmniRoute.

This is intentionally a later arc.

The current bottleneck is primarily workflow/context movement, not model-provider selection.

Repo Control II should not prematurely become a model-routing project.

---

# 7. AI Role Model

Repo Control II should preserve distinct AI roles.

## 7.1 Architect

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

The current preferred integration direction is to keep the existing ChatGPT Architect experience while allowing ChatGPT to query controlled Repo Control tools.

Conceptually:

```text
Product Owner asks Architect
        ↓
Architect invokes Repo Control tools
        ↓
Repo Control returns deterministic project evidence
        ↓
Architect reasons over bounded evidence
```

Repo Control does not need to replace the ChatGPT browser experience in the first architecture.

## 7.2 Coder

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

## 7.3 Product Owner

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

# 8. Deterministic Truth Before AI Interpretation

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

This principle is important for:

- reliability;
- explainability;
- context efficiency;
- repeatability;
- reducing hallucinated repository state;
- reducing unnecessary model usage.

---

# 9. Current Manual Workflow

The current working pattern across projects is approximately:

```text
1. Product Owner discusses milestone with ChatGPT / Architect.

2. Architect composes milestone prompt.

3. Product Owner copies or saves prompt into repository.

4. Product Owner switches to VS Code.

5. Product Owner directs coding agent to prompt.

6. Coder inspects, implements, tests, and prepares closeout.

7. Product Owner transfers coder questions or closeout back to Architect.

8. Architect reviews implementation evidence.

9. Product Owner performs Git staging / commit / push / branch actions.
```

This workflow works, but the Product Owner currently acts as the transport mechanism between tools.

Repo Control II should preserve the deliberate review gates while reducing the manual transport.

---

# 10. Target Workflow Direction

The intended future pattern is approximately:

```text
Product Owner
→ defines or discusses milestone with Architect

Architect
→ queries Repo Control for current repository/project facts
→ creates approved structured milestone artifact

Repo Control
→ stores milestone in correct project location
→ exposes active milestone state to Coder

Coder
→ consumes milestone + standing skills
→ performs preflight
→ begins bounded implementation

    ↕ iterative milestone loop

    question / clarification
    stop condition
    escalation
    unexpected repository reality
    bug discovery
    test failure
    Architect decision
    Product Owner decision where required
    prompt addendum / clarification / final lock-in

→ resumes implementation when authorized
→ validates
→ writes structured closeout/report

Repo Control
→ preserves the milestone decision trail
→ records ready-for-review state
→ exposes implementation/test/Git evidence

Architect
→ reviews bounded evidence through Repo Control tools
→ recommends acceptance, bounded correction, or separate follow-up

Product Owner
→ approves
→ performs Git mutation
→ closes milestone
```

The iterative exchange between prompt and closeout is intentional.

The prompt begins the milestone, but the active milestone may evolve through controlled questions, answers, clarifications, escalation decisions, bug classifications, validation findings, and approved lock-ins.

Repo Control II should preserve those decisions as durable milestone evidence rather than relying on chat history alone.

The milestone should remain open while the approved objective can still be completed safely within its boundaries.

When a discovery materially changes the objective, architecture, authority, risk, or scope, the system should support stopping or suspending the milestone and creating a separately scoped follow-up rather than silently broadening the original work.

The exact mechanism will be developed incrementally.

Repo Control II should not attempt to automate the entire target flow before the individual components prove useful.

---

# 11. Structured Project Artifacts

Repo Control II should continue using human-readable Markdown as the normal project artifact format.

A future milestone prompt may use a small metadata header such as:

```yaml
---
repo_control_artifact: milestone_prompt
project: repo-control-ii
milestone: "R001"
status: ready_for_coder
version: 1
---
```

followed by ordinary Markdown:

```text
# Milestone

## Goal
## Background
## Scope
## Out of Scope
## Architecture Constraints
## Implementation Requirements
## Testing Requirements
## Stop / Escalation Conditions
## Deliverables
## Completion Criteria
```

Similarly, a coder closeout may contain metadata such as:

```yaml
---
repo_control_artifact: coder_closeout
project: repo-control-ii
milestone: "R001"
status: ready_for_architect_review
---
```

This concept is not yet a locked implementation requirement.

The governing principle is:

```text
human-readable first
machine-readable where useful
machine-only only when necessary
```

---

# 12. External Tool Strategy

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
| Git control / GUI concepts | GitButler |
| Agent procedure | Agent Skills |
| Coding restraint | Ponytail as optional skill |
| Repository intelligence | Graphify, DevLens |
| Model gateway | LiteLLM, OmniRoute |
| Coding agent | Copilot, Codex, Cline, OpenCode, Aider, Goose, OpenHands |

Selection should be based on controlled evaluation, not marketing claims.

---

# 13. Local AI Position

Repo Control II is not primarily a local-AI project.

Local AI may become useful for:

- bounded coding tasks;
- repository classification;
- repetitive summaries;
- low-risk transformations;
- private/offline processing;
- cost reduction where quality remains acceptable.

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

Possible scoring dimensions include:

- requirements satisfied;
- tests passed;
- unauthorized changes;
- architecture violations;
- lines changed;
- number of corrections;
- completion time;
- model/API cost.

This remains future evaluation work.

---

# 14. Repository Intelligence Position

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

Potential future capabilities include:

- query symbol relationships;
- identify affected tests;
- inspect dependencies;
- compare structural state before and after implementation;
- identify new coupling;
- detect removed or renamed symbols;
- inspect neighborhoods around changed files;
- compare committed baseline with working-tree structure.

Large raw graph dumps should not be inserted into model context when targeted queries are sufficient.

---

# 15. Git Authority

Repo Control II retains the established Product Owner Git boundary.

Coding agents may use read-only Git commands for:

- preflight;
- status;
- diffs;
- branch identification;
- commit inspection;
- change reporting.

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

Repo Control II may make those operations easier and safer through graphical controls, plans, previews, and confirmation.

That does not transfer authority away from the Product Owner.

---

# 16. Documentation Model

Repo Control II begins with five global context documents:

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

# 17. Initial Roadmap

The current conceptual roadmap is:

```text
R001 — Repo Control II Baseline / Inherited Code Reconnaissance
R002 — Git Control Expansion
R003 — Agent Skills Foundation
R004 — Repository Intelligence Evaluation
R005 — Repository Intelligence Integration
R006 — Workflow Handoff Prototype
R007 — Architect / Coder Workflow Integration
R008 — Model Gateway Evaluation
R009 — Model Routing Integration
```

These identifiers and names are provisional until the project workflow and architecture documents formalize milestone conventions.

The preferred capability order is:

```text
1. Git Control
2. Agent Procedure
3. Repository Intelligence
4. Workflow Orchestration
5. Model Gateway
```

Rationale:

```text
Git Control
→ immediate proven value

Agent Procedure
→ low-cost reduction in repeated prompting and agent drift

Repository Intelligence
→ establish trusted deterministic code understanding

Workflow Orchestration
→ integrate proven pieces into the control plane

Model Gateway
→ optimize model selection only after workflow/context problems are solved
```

---

# 18. Near-Term Work

Before substantive implementation, Repo Control II should complete its governing documentation.

Current sequence:

```text
1. project_context.md
2. project_architecture.md
3. project_workflow.md
4. coding_agent_rules.md
5. canonical_parking_lot.md
6. review inherited README / package metadata
7. run and record inherited baseline tests
8. create initial Git commit
9. push initial Repo Control II baseline to GitHub
10. begin first reconnaissance milestone
```

The first implementation-oriented milestone should not begin by changing code.

It should first establish what the inherited implementation actually contains and classify it against the new architecture.

---

# 19. Current Open Questions

The following remain intentionally unresolved.

## 19.1 Git UI Strategy

Determine whether Repo Control II should:

- implement additional Git UI directly;
- integrate with an existing Git GUI;
- reuse libraries or concepts from GitButler;
- combine internal workflow state with external Git visualization.

## 19.2 Repository Intelligence Engine

Benchmark Graphify and DevLens on a real repository.

Determine:

- installation complexity;
- language coverage;
- graph fidelity;
- working-tree support;
- queryability;
- incremental refresh behavior;
- MCP/tool interface quality;
- performance;
- licensing implications;
- usefulness to both Architect and Coder.

## 19.3 ChatGPT Integration

Preferred initial direction:

```text
keep ChatGPT browser as Architect
+
expose controlled Repo Control tools to ChatGPT
```

Open questions include:

- integration mechanism;
- authentication;
- local-server accessibility;
- tool security boundary;
- project selection;
- whether GitHub plus Repo Control local state should be combined;
- how local working-tree information is exposed safely.

A separate Repo Control-hosted Architect UI using the OpenAI API remains a possible later architecture, not the current default.

## 19.4 Coder Integration

Determine how coding agents should receive:

- active milestone;
- standing skills;
- repository context;
- current workflow state.

The system should ideally support more than one coding agent.

## 19.5 Workflow State Persistence

Determine where milestone/workflow state should live:

- tracked Markdown;
- lightweight structured local state;
- database;
- combination of tracked artifacts and operational state.

The architecture should prefer the simplest design that preserves recoverability and transparency.

## 19.6 Model Gateway

Determine later whether LiteLLM, OmniRoute, or another solution provides enough benefit to justify integration.

Do not prioritize this ahead of current workflow and repository-context problems.

---

# 20. Project Principles

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
```

---

# 21. Definition of Success

Repo Control II will be successful if it materially improves the Product Owner's actual project workflow.

The intended experience is:

```text
I can see what changed.
I can understand repository and branch state.
I can manage Git without memorizing exact command syntax.
The Architect can obtain current project evidence without me manually copying it.
The Coder can obtain the approved milestone and standing procedure without me manually carrying it.
The Architect can review implementation evidence without me reconstructing the repository state by hand.
AI receives only the context it needs.
I remain in control of important repository and workflow decisions.
```

A feature that does not improve that workflow, reduce risk, improve understanding, or reduce repeated context should not be considered valuable merely because it is technically sophisticated.

---

# 22. Current Baseline Statement

Repo Control II is currently in its clean-foundation phase.

The project has:

```text
a fresh local Git repository;
a new GitHub remote;
a clean documentation tree;
a recreated Python environment;
the inherited Repo Control source and tests;
no accepted Repo Control II application architecture yet;
no initial Repo Control II commit yet.
```

The immediate objective is to define the new architecture and workflow clearly enough that the inherited code can be evaluated against them rather than allowing the inherited implementation to define the new product by default.
