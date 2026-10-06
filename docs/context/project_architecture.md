# PROJECT_ARCHITECTURE.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Durable system architecture, authority boundaries, integration model, and architectural invariants  
**Project phase:** Clean restart and architecture definition  
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `https://github.com/CHHIV-Projects/repo-control-ii.git`  
**Current architecture baseline:** The inherited Repo Control I code is present only as unassessed implementation material. Repo Control II architecture is defined by the new governing documents, not by legacy code.  
**Current architectural emphasis:** establish a deterministic control plane for Git, agent procedure, repository intelligence, workflow orchestration, and later model routing while preserving Product Owner authority and minimizing unnecessary AI context transfer.

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
how repository intelligence is produced and consumed
how external tools are integrated
how AI receives bounded trusted context
what Repo Control should own
what Repo Control should not reimplement
```

`project_context.md` describes current project/product state and near-term direction.

This document describes the intended system structure and the architectural boundaries that should remain stable even as implementations change.

---

# 2. Architecture North Star

Repo Control II should become a deterministic project-control layer between the Product Owner, Architect, coding agents, repositories, Git, project documents, tests, and selected external tools.

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

## 3.2 Workflow Integration

Reduce manual transfer of prompts, questions, clarifications, coder output, test evidence, and repository state between the Architect and Coder.

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

milestone state
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

The system should not rebuild a full Git GUI, repository graph engine, coding agent, or model router merely because those functions are technically possible to recreate.

## 4.6 Local and Remote Repository State Are Different Authorities

Repo Control II must preserve the distinction between:

```text
GitHub
→ committed / pushed remote state

local Git repository
→ current branch / index / working tree

repository-intelligence engine
→ structural interpretation of the selected local or committed code state
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

---

# 5. System Boundary

Repo Control II is a **control plane**, not an IDE, coding model, or source-control replacement.

Repo Control II should own:

- project selection;
- repository registration;
- workflow state;
- milestone state;
- artifact location and status;
- Git-state presentation;
- controlled Git action preparation;
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

# 6. Major Architectural Layers

## 6.1 User / Product Owner Layer

Responsibilities:

- select project/repository;
- inspect Git state;
- review proposed operations;
- approve workflow transitions;
- approve architecture or scope changes;
- review escalations;
- accept implementation;
- perform or authorize Git mutations;
- initiate Architect or Coder work;
- review milestone state.

This layer expresses authority and intent.

It should not require the Product Owner to act as the primary transport mechanism between Architect and Coder.

## 6.2 Architect Integration Layer

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

The preferred first architecture is:

```text
ChatGPT
→ controlled Repo Control tools
→ local deterministic project evidence
```

rather than immediately replacing the ChatGPT interface with a custom API-backed chat client.

## 6.3 Coder Integration Layer

Coder environments may include:

- GitHub Copilot;
- Codex;
- Cline;
- OpenCode;
- Aider;
- Goose;
- OpenHands;
- other approved agent environments.

Repo Control should expose enough standardized state that the coding agent does not require product-specific custom integration where avoidable.

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

## 6.4 Workflow Orchestration Layer

This is the central custom layer.

Responsibilities:

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

This is conceptual, not yet a locked persistence schema.

## 6.5 Git Control Layer

Responsibilities:

- current repository identity;
- branch;
- HEAD;
- upstream;
- ahead/behind;
- working-tree state;
- staged/unstaged/untracked files;
- per-file diff;
- commit history;
- branch graph;
- remote information;
- tag information;
- safe action preparation;
- Product Owner confirmation;
- GitHub relationship.

Repo Control should use Git itself as the authority.

Potential external support:

- GitButler concepts or integration;
- Git libraries;
- Git CLI where appropriate.

The UI must not invent Git state independently.

## 6.6 Repository Intelligence Layer

Responsibilities:

- symbol structure;
- file/module relationships;
- imports;
- references;
- call relationships;
- inheritance;
- tests associated with code;
- local structural neighborhoods;
- architectural deltas;
- optional baseline/current comparison.

Candidate engines:

- Graphify;
- DevLens.

Repo Control should expose **query-oriented repository intelligence**, not blindly inject whole graphs into AI context.

## 6.7 Agent Procedure Layer

Primary mechanism:

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

Ponytail may be evaluated as an optional coding-restraint skill, not as a core subsystem.

## 6.8 Test and Evidence Layer

Responsibilities:

- test command definitions;
- test results;
- build results;
- static checks;
- validation evidence;
- optional screenshots/log references;
- evidence association with milestone state.

Repo Control should record test evidence but should not infer a passing result when the test runner did not produce one.

## 6.9 Model Gateway Layer

This is a later architecture layer.

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

This layer must not become a prerequisite for earlier workflow or Git functionality.

---

# 7. Project and Repository Model

Repo Control II should support multiple projects/repositories over time.

A project record should conceptually identify:

```text
project identity
display name
authoritative repository path
remote repository
default branch
documentation roots
milestone roots
tool configuration
repository-intelligence configuration
agent-skill configuration
optional test commands
```

For Repo Control II itself:

```text
Project:
Repo Control II

Repository:
/home/chuck/projects/repo-control-ii

Remote:
https://github.com/CHHIV-Projects/repo-control-ii.git

Default branch:
main

Context:
docs/context/

Milestones:
docs/milestones/
```

The exact storage format remains to be designed.

---

# 8. Milestone Artifact Architecture

## 8.1 Prompt

The milestone prompt is the initial approved contract.

It should remain human-readable.

Potential metadata:

```yaml
---
repo_control_artifact: milestone_prompt
project: repo-control-ii
milestone: "R001"
status: ready_for_coder
version: 1
---
```

The body should contain the actual milestone instructions.

## 8.2 Addenda and Decision Trail

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

Possible representation:

```text
Initial Prompt

Q&A Round 1

Q&A Round 2

Escalation 1

Bug Discovery 1

Final Lock-ins
```

The exact artifact/persistence strategy remains open.

## 8.3 Closeout

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

## 8.4 Artifact Authority

Milestone artifacts should be treated as durable project evidence.

They should not exist only inside chat history.

---

# 9. Workflow Event Architecture

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

This separation allows the system to retain chronology without requiring a unique state for every event.

The workflow engine should be simple and explicit.

Avoid introducing a generalized workflow framework unless a concrete requirement justifies it.

---

# 10. Git Architecture

## 10.1 Git Is the Authority

Repo Control should derive repository state from Git.

It should not maintain a competing model of:

- current branch;
- staged files;
- unstaged files;
- commit history;
- tags;
- remote-tracking state.

Cached presentation data is acceptable, but Git remains authoritative.

## 10.2 Read Versus Mutation

Read-only operations should be broadly available to Repo Control, Architect tools, and coding agents where appropriate.

Examples:

```text
status
diff
log
branch listing
remote listing
rev-parse
show
tag listing
ahead/behind
```

Mutation should remain explicitly gated.

Examples:

```text
stage
unstage
commit
push
branch create/delete
switch
merge
rebase
reset
tag create/delete
stash
clean
remote mutation
```

## 10.3 Product Owner Git Boundary

Coding agents do not perform normal Git mutations.

Repo Control may prepare a mutation plan such as:

```text
files selected
operation
commit message
target branch
remote impact
warnings
```

but execution requires Product Owner action.

## 10.4 Graphical Git Direction

Desired Git UI may include:

- file change list;
- staged/unstaged grouping;
- visual diff;
- branch graph;
- commit graph;
- upstream state;
- ahead/behind;
- local/remote distinction;
- tags;
- conflict indicators;
- safe action controls.

Before implementing advanced Git UI, evaluate whether GitButler or another mature open-source component can be integrated, adapted, or used as a reference.

---

# 11. GitHub Integration Architecture

GitHub provides committed/pushed repository state.

Potential uses:

- remote branch information;
- remote commit history;
- pull requests where relevant;
- issue/PR linkage where relevant;
- repository browsing by Architect;
- comparison with local branch state.

GitHub is not sufficient for:

```text
uncommitted local changes
staged changes
local-only branches
live working-tree state
local test results
```

Repo Control local integration is therefore required for complete current-state awareness.

---

# 12. Repository Intelligence Architecture

## 12.1 Purpose

Repository intelligence should make code structure queryable without repeated broad file reading.

## 12.2 Desired Evidence

Potential data includes:

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

## 12.3 Baseline and Working-Tree Views

A valuable future model is:

```text
Committed Baseline Graph
        vs.
Current Working-Tree Graph
```

This could support questions such as:

- what structural dependencies were added?
- what symbols disappeared?
- what tests now reference changed code?
- did the change introduce new cross-layer coupling?
- which modules are newly connected?
- which architectural relationships changed?

## 12.4 Query, Do Not Dump

AI should receive targeted results such as:

```text
neighbors of SourceProfile
callers of function X
tests referencing module Y
structural changes in files modified by current milestone
```

rather than an entire repository graph unless genuinely needed.

## 12.5 Refresh Strategy

Graph refresh should occur at meaningful lifecycle points rather than every keystroke.

Potential triggers:

```text
milestone baseline
coder start
implementation complete
post-test if files changed
architect review
```

Exact behavior depends on the chosen engine.

---

# 13. Agent Skills Architecture

Agent Skills should become the standing procedure layer for coding agents.

A skill should be:

- bounded;
- reusable;
- easy to inspect;
- loaded only when relevant;
- independent from one particular milestone;
- compatible with more than one agent where practical.

A milestone prompt should contain the **delta**, not restate every project rule.

Conceptual context stack:

```text
Agent Skill
→ standing procedure

Milestone Prompt
→ current objective and constraints

Approved Addenda
→ decisions made during the active milestone

Repository Intelligence
→ targeted implementation facts

Tests / Git
→ objective evidence
```

This architecture directly supports context efficiency.

---

# 14. Architect Tool Interface

The preferred long-term Architect interface is a small set of controlled project tools exposed through Repo Control.

Conceptual examples:

```text
get_project_state()
get_repo_status()
get_changed_files()
get_diff_summary()
get_branch_state()
get_active_milestone()
get_milestone_events()
get_closeout()
get_test_evidence()
query_code_graph(...)
get_structural_delta(...)
create_milestone(...)
append_milestone_decision(...)
mark_ready_for_review(...)
```

These names are illustrative, not final API commitments.

Tools should return compact structured evidence.

The Architect should not need unrestricted shell access to obtain ordinary project facts.

---

# 15. Coder Tool Interface

Coder integration should be lighter than Architect integration where possible.

The Coder should be able to determine:

- active project;
- active milestone;
- current prompt;
- standing skills;
- current lock-ins;
- repository path;
- relevant evidence;
- required tests;
- escalation path.

A coding agent should not need broad access to unrelated projects.

Repo Control should support agent portability rather than lock the architecture to one proprietary agent.

---

# 16. Security and Authority Boundaries

Repo Control II may eventually become a bridge between cloud AI and local repositories.

That boundary requires deliberate design.

## 16.1 Local Repository Access

Remote/cloud AI should access local repository evidence only through approved Repo Control tools.

Avoid exposing arbitrary filesystem access when a bounded query interface is sufficient.

## 16.2 Mutation Controls

Tool access should distinguish:

```text
read
prepare
propose
execute
```

For sensitive operations, these must not collapse into one implicit action.

## 16.3 Secrets

Repo Control must not expose:

- credentials;
- tokens;
- API keys;
- protected environment contents;
- SSH private keys;
- GitHub secrets;
- service credentials;

unless a future specifically approved feature has a justified secure mechanism.

## 16.4 Project Isolation

Multiple managed repositories must remain explicitly separated.

A tool request for one project must not silently inspect or mutate another project.

---

# 17. State Persistence Architecture

Repo Control II should avoid premature database complexity.

Potential state classes:

## 17.1 Durable Tracked State

Best represented in Git-tracked Markdown or configuration:

- project context;
- architecture;
- workflow;
- coding-agent rules;
- milestone prompts;
- closeouts;
- stable project configuration.

## 17.2 Operational Workflow State

May require lightweight structured storage:

- active milestone;
- current workflow state;
- transient waiting/escalation state;
- event timestamps;
- UI preferences;
- pending prepared operations;
- cached intelligence status.

Possible implementation options:

```text
small local JSON files
SQLite
lightweight relational store
```

The choice should be driven by actual requirements.

## 17.3 Derived State

Should be recomputable where practical:

- Git status;
- branch graph;
- diff summaries;
- repository graph;
- test summaries.

Derived state should not become competing canonical truth.

---

# 18. UI Architecture Direction

The current implementation is Flask-based.

That is an inherited implementation fact, not a locked architectural requirement.

The Repo Control II UI should eventually provide clear views for:

```text
Projects
Git
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
- practical daily use.

Framework choice should remain open until reconnaissance determines whether the inherited UI is worth retaining.

---

# 19. External Tool Evaluation Boundaries

## 19.1 GitButler

Evaluate for:

- branch/commit visualization;
- working-tree management concepts;
- Git UX patterns;
- integration feasibility;
- licensing;
- Linux support;
- whether Repo Control should integrate or only borrow design ideas.

## 19.2 Graphify

Evaluate for:

- local repo graph generation;
- live working-tree support;
- query interface;
- incremental refresh;
- MCP/tool exposure;
- language support;
- performance;
- licensing;
- usefulness to Architect and Coder.

## 19.3 DevLens

Evaluate against the same criteria.

Do not select Graphify or DevLens solely from feature claims.

## 19.4 Agent Skills

Treat as the preferred open procedure standard unless evaluation reveals a blocking mismatch.

## 19.5 Ponytail

Treat as optional behavior guidance.

It should not become a foundational dependency.

## 19.6 LiteLLM and OmniRoute

Evaluate later as model-gateway candidates.

Model routing should remain downstream of workflow and repository-context improvements.

---

# 20. Local AI Architecture Position

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

# 21. Inherited Code Architecture Boundary

The current inherited codebase must be treated as implementation evidence, not as architecture.

The first reconnaissance milestone should classify major subsystems:

```text
KEEP
REFACTOR
RETIRE
```

Likely candidates for review include:

- Git services;
- Git UI;
- repository scanner;
- relationship analysis;
- context generation;
- snapshots;
- comparison engine;
- local-AI analysis;
- milestone status logic;
- staging/commit planning;
- browser UI;
- CLI structure;
- tests.

A legacy subsystem should survive only if it supports the new architecture at reasonable maintenance cost.

---

# 22. Architectural Invariants

The following should be treated as durable rules unless explicitly revised by an approved architecture milestone.

## 22.1 Repo Control Is the Integration Layer

Repo Control coordinates state and tools.

It should not absorb specialized engines without need.

## 22.2 Git Remains Git Authority

Repo Control presents and controls Git.

It does not maintain a competing repository history model.

## 22.3 Product Owner Owns Important Mutations

Automation may prepare.

The Product Owner approves.

## 22.4 Coding Agents Do Not Own Git Lifecycle

Coder workflow ends at implementation, validation, closeout, and read-only status reporting.

## 22.5 Architect and Coder Remain Separate Roles

One may inform the other through Repo Control.

They do not silently collapse into one autonomous actor.

## 22.6 Milestone Decisions Must Be Durable

Important clarifications and lock-ins must not exist only in transient chat.

## 22.7 Iteration Inside a Milestone Is Normal

Questions, stops, bug discoveries, and corrections do not automatically create a new milestone.

## 22.8 Material Scope Change Must Be Explicit

A milestone must not silently expand into a different objective.

## 22.9 Repository Intelligence Is Evidence

Graph/parser output informs AI.

It is not itself project authority.

## 22.10 Test Results Come From Test Execution

AI summaries may explain results but must not fabricate or infer passing status.

## 22.11 Human-Readable Artifacts Are Primary

Metadata supports automation.

It does not replace understandable project documentation.

## 22.12 Context Should Be Targeted

Repo Control should provide the smallest sufficient evidence set.

## 22.13 External Tools Should Be Replaceable

Integrations should avoid unnecessary coupling to one vendor or engine where practical.

## 22.14 Local and Remote State Must Remain Distinct

GitHub remote state and local working-tree state must not be conflated.

## 22.15 Secrets Must Remain Outside Ordinary AI Context

Repo Control should expose status, not sensitive values, wherever possible.

---

# 23. Initial Architecture Sequence

The recommended architecture sequence is:

```text
1. Establish governing documents.

2. Reconnaissance of inherited Repo Control code.

3. Preserve or improve proven Git functionality.

4. Establish Agent Skills.

5. Evaluate repository-intelligence engines.

6. Integrate selected repository intelligence.

7. Implement milestone/workflow orchestration.

8. Expose controlled Architect tools.

9. Improve Coder handoff integration.

10. Evaluate model gateway only after context/workflow flow is working.
```

This order intentionally prioritizes proven workflow pain points over speculative AI infrastructure.

---

# 24. Architecture Decision Criteria

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
- can be validated incrementally.

Avoid choosing an option merely because it is more sophisticated.

---

# 25. First Reconnaissance Requirements

The first Repo Control II implementation-oriented milestone should inspect the inherited code and answer:

```text
What Git functionality already exists and works?

What UI components support the new control-plane direction?

What repository-scanning functionality overlaps with Graphify/DevLens?

What snapshot/comparison behavior is still useful?

What local-AI interpretation code should be retired?

What workflow/milestone code is reusable?

What coupling would make selective reuse expensive?

What tests prove current behavior?

What is the smallest viable Repo Control II foundation?
```

The closeout should classify major components:

```text
KEEP
REFACTOR
RETIRE
```

and should become the roadmap for the next implementation milestone.

---

# 26. Current Open Architecture Questions

The following remain intentionally unresolved.

## 26.1 Git Control Implementation

Should advanced Git UI be:

- implemented directly;
- integrated from an external component;
- delegated to GitButler while Repo Control handles workflow;
- hybrid?

## 26.2 Repository Intelligence Engine

Graphify versus DevLens remains unselected.

## 26.3 Workflow Persistence

Tracked Markdown versus lightweight operational database remains open.

## 26.4 Architect Integration Mechanism

The preferred direction is controlled tools from ChatGPT to Repo Control, but the exact mechanism remains open.

## 26.5 Coder Integration Mechanism

Agent Skills are preferred for procedure, but the active-milestone handoff mechanism remains to be designed.

## 26.6 UI Framework

Flask is inherited, not locked.

## 26.7 Model Gateway

LiteLLM versus OmniRoute is deferred.

---

# 27. Definition of Architectural Success

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

# 28. Current Architecture Baseline Statement

Repo Control II currently has:

```text
a fresh repository identity;
a copied but unassessed legacy codebase;
a clean documentation tree;
a defined project mission;
a defined five-arc capability model;
a defined Product Owner / Architect / Coder role separation;
a defined deterministic-evidence principle;
an explicit iterative milestone-loop requirement;
no accepted inherited application architecture yet.
```

The next architecture task is not to add more features.

It is to use this architecture as the standard against which the inherited code is evaluated.
