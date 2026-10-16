# CODING_AGENT_RULES.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Standing project-wide rules for coding agents  
**Project phase:** Git Control complete; M003.011 CLOSED; M003.012 single-user workflow compression implementation (formal milestone acceptance pending)
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`  
**Current workflow baseline:** `docs/context/project_workflow.md`  
**Current architecture baseline:** `docs/context/project_architecture.md`  
**Current context baseline:** `docs/context/project_context.md`

---

# 1. Purpose

This document defines standing rules for coding agents working on Repo Control II.

Its purpose is to reduce repeated milestone-prompt boilerplate while preserving:

- milestone scope;
- Product Owner authority;
- architecture discipline;
- clean Git history;
- deterministic evidence;
- targeted context use;
- implementation restraint;
- honest validation;
- clear escalation;
- reusable closeout standards;
- portability across coding agents;
- compatibility with future Agent Skills;
- compatibility with Repo Control lifecycle and handoff-packet workflows.

Milestone prompts should reference this document rather than repeat every standing rule.

This document does not replace:

- the active milestone prompt;
- Product Owner-approved binding lock-ins;
- current Product Owner direction;
- current repository code and deterministic evidence;
- approved reconnaissance;
- validation evidence;
- milestone closeouts;
- applicable Agent Skills.

Use current repository evidence and active milestone artifacts as the source of truth.

Do not rely on chat memory alone when current project evidence is available.

---

# 2. Rule Priority

Apply instructions in this order:

```text
1. explicit current Product Owner direction
2. active milestone prompt and Product Owner-approved binding lock-ins
3. this coding-agent-rules document
4. current architecture, workflow, and context documents
5. approved reconnaissance closeout
6. relevant maintained project documentation
7. prior milestone prompts and closeouts
8. agent assumptions
```

When instructions conflict:

- follow the safer interpretation;
- stop and identify the conflict;
- ask for clarification before risky implementation;
- do not silently choose a new architecture;
- do not silently broaden scope;
- do not silently change Git authority or workflow authority.

The Product Owner Git-mutation boundary is a standing rule and is not a normal milestone-level override.

A non-binding workflow decision must not be treated as a Product Owner-approved instruction.

---

# 3. Authoritative Environment and Repository

The authoritative editable repository is:

```text
/home/chuck/projects/repo-control-ii
```

on:

```text
henderson-server1
```

Normal editing occurs through:

```text
VS Code Remote SSH from the Windows workstation
```

Normal repository and Git commands run in the Linux repository environment.

GitHub remote:

```text
git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git
```

Do not assume a Windows checkout is authoritative.

Do not treat GitHub remote state as equivalent to the local working tree.

Do not assume local tracking refs have been refreshed from the remote unless an authorized fetch occurred.

---

# 4. Repo Control Workflow Context

Repo Control II provides a workflow layer in addition to Git control.

For canonical lifecycle-managed milestones, Repo Control provides:

```text
authoritative selected milestone (external selection pointer)
current lifecycle phase and authorization check
authoritative milestone prompt
workflow sidecar with immutable event history
Product Owner-approved binding lock-ins
open questions, blockers, and bug records
closeout artifact discovery
Coder Local CLI interface (`repoctl coder-context`, `repoctl coder-status`)
Coder bounded event submission (`coder-question`, `coder-bug`, `coder-test`, `coder-closeout`)
Coder Handoff Packet (fallback manual transport)
Architect Review Packet (fallback manual transport)
current deterministic Git evidence
```

The Coder should use `repoctl coder-context` as the primary, authoritative mechanism for retrieving active milestone instructions and verifying implementation authorization. When Coder Handoff Packets are used, treat them as **derived transport output**, not as a new source of truth.

Authority remains with:

```text
Product Owner direction
+
active milestone prompt
+
approved binding lock-ins
+
governing project documents
+
current deterministic repository evidence
```

If packet or context content conflicts with an authoritative source, stop and report the discrepancy.

---

# 5. Core Agent Workflow

For every implementation milestone:

```text
1. Confirm the authoritative repository.
2. Run `repoctl coder-context` to retrieve active milestone selection, prompt, and authorization status.
3. Verify implementation authorization status: proceed only if authorized (`IN_PROGRESS` or `IN_PROGRESS (correction mode)`).
4. Review standing document references and the authoritative milestone prompt.
5. Review Product Owner-approved binding lock-ins, open conditions, and applicable approved Architect dispatches surfaced by `repoctl coder-context`.
6. Read approved reconnaissance when the milestone depends on it.
7. Perform read-only Git preflight (`git branch`, `git status`, `git log`).
8. Confirm current branch and working-tree state. The active milestone workflow sidecar is expected lifecycle state and is exempt from the clean-tree precondition by itself.
9. Load only relevant broader context.
10. Inspect targeted implementation paths.
11. Confirm the milestone boundary.
12. If blocked or clarification needed, submit via `repoctl coder-question` (or escalate if out of scope).
13. If bugs are discovered, submit via `repoctl coder-bug`.
14. Implement only approved scope.
15. Run relevant validation and record evidence via `repoctl coder-test`.
16. Create or update the required closeout artifact.
17. Submit closeout for Product Owner review via `repoctl coder-closeout`.
18. Report final read-only Git status.
19. Stop before Product Owner Git actions.
```

The agent must not infer live repository state solely from documentation.

The agent must not infer project architecture solely from inherited code when governing documents define a newer architecture.

The agent must not infer workflow authorization merely from the existence of a prompt or closeout. Always verify authorization with `repoctl coder-context` or `repoctl coder-status`.

---

# 6. Lifecycle Awareness

The implemented lifecycle is:

```text
PLANNING
READY
IN_PROGRESS
WAITING
REVIEW
CLOSED
```

Coding agents must respect lifecycle state.

The single-user Product Owner browser offers Approve & Start, Resolve & Resume, and Accept & Close. These deliberately authorized actions persist distinct ordered events together; they do not collapse lifecycle phases or grant Coder new authority. The lack of an actor dropdown or extra confirmation checkbox is UI simplification only. Coder must still verify IN_PROGRESS authorization and must never perform Product Owner approvals, resumes, acceptance, closure, or Git mutations.

## PLANNING

The milestone is not yet approved for implementation.

Do not begin implementation.

## READY

The prompt is approved for Coder handoff.

Implementation may begin when the workflow records `implementation_started`.

## IN_PROGRESS

Implementation or validation is active.

## WAITING

Implementation is paused.

Do not resume until the blocking condition has been validly resolved and the workflow is explicitly resumed.

## REVIEW

Implementation is under review.

Do not make further implementation changes unless a bounded correction is explicitly requested.

## CLOSED

The milestone is complete and terminal.

Do not resume implementation under the closed milestone.

Lifecycle state is reconstructed from workflow events and must not be inferred from Git state, closeout presence, or test results.

---

# 7. Milestone Modes

The active prompt should identify the mode.

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

# 8. Reconnaissance-Only Mode

Reconnaissance is used when current implementation reality must be mapped before coding.

Purpose:

- inspect relevant systems;
- identify current behavior;
- identify dependencies;
- identify authority boundaries;
- identify reuse candidates;
- identify retirement candidates;
- compare realistic implementation options;
- identify test strategy;
- select one recommended implementation direction;
- produce a practical implementation roadmap.

When the prompt says reconnaissance only:

- do not modify implementation files;
- do not begin coding;
- do not make unrelated cleanup changes;
- do not create speculative abstractions;
- do not perform Git mutations;
- create the required reconnaissance closeout;
- stop after the approved reconnaissance deliverables are complete.

Reconnaissance should become the roadmap for subsequent implementation.

---

# 9. Implementation-After-Reconnaissance Mode

When implementation follows approved reconnaissance:

```text
1. read this file / applicable standing rules;
2. read the implementation prompt;
3. read the approved reconnaissance closeout;
4. read Product Owner-approved lock-ins;
5. inspect named implementation files;
6. inspect directly related tests;
7. expand investigation only when evidence requires it.
```

Do not repeat broad repository exploration unless:

- repository state materially changed;
- reconnaissance omitted a required path;
- targeted code contradicts the roadmap;
- tests reveal an undocumented dependency;
- a safety, authority, or architecture boundary remains unresolved.

When escalating, identify the reconnaissance assumption that failed.

---

# 10. Direct Low-Risk Implementation Mode

A separate reconnaissance milestone is not required for clearly bounded work such as:

- copy changes;
- small labels or wording changes;
- narrow styling fixes;
- focused tests;
- documentation-only edits;
- minor non-destructive bugs with an obvious local cause;
- mechanical corrections.

Even low-risk work must:

- inspect directly relevant code;
- preserve scope;
- avoid unrelated cleanup;
- run relevant validation;
- create the required closeout.

Do not label work low-risk merely to reduce effort when architecture or authority is uncertain.

---

# 11. Validation-Only Mode

Validation-only milestones establish evidence without changing implementation.

In validation-only mode:

- confirm the approved target repository/environment;
- run approved checks;
- collect required evidence;
- document pass/fail results;
- do not modify implementation code;
- do not silently repair defects;
- do not broaden the test matrix without explanation;
- do not perform Git mutations.

When a defect is found:

```text
record evidence
classify severity
determine whether testing should continue
do not repair unless explicitly authorized
recommend the smallest bounded fix when needed
```

Validation-only work must not silently become repair work.

---

# 12. Documentation-Only Mode

Documentation-only work may update:

- Project Context;
- Project Architecture;
- Project Workflow;
- Coding Agent Rules;
- Canonical Parking Lot;
- milestone documentation;
- handoff documents.

In documentation-only mode:

- do not modify application code;
- distinguish current implementation from intended future direction;
- preserve historical milestone records;
- do not invent unimplemented behavior;
- create only requested documents;
- report files changed.

---

# 13. Bug-Fix Follow-Up Mode

A bug-fix follow-up must remain limited to the documented defect.

Rules:

- reproduce or confirm the defect when practical;
- identify the smallest safe repair;
- preserve current architecture;
- do not turn the repair into a broad refactor;
- add targeted regression coverage where appropriate;
- document whether the defect invalidates earlier validation;
- document retained limitations;
- create one closeout.

A bug may remain inside the active milestone only when the fix remains within the approved objective, architecture, authority, and risk boundary.

---

# 14. Integration / Tool Evaluation Mode

For tool evaluation:

- define the exact problem being solved;
- inspect candidate capabilities;
- define evaluation criteria;
- test on a realistic repository/workload;
- compare integration burden;
- compare licensing implications;
- compare operational complexity;
- recommend one direction;
- do not adopt or deeply integrate a tool unless explicitly authorized.

Potential candidates include:

```text
Graphify
DevLens
Agent Skills
Ponytail
LiteLLM
OmniRoute
coding-agent alternatives
```

Do not adopt a tool solely because it is available.

---

# 15. Iterative Milestone Behavior

A milestone is a bounded work container, not a single request-response exchange.

The agent should expect possible iterations such as:

```text
implementation
→ question
→ clarification
→ resume

implementation
→ stop condition
→ evidence
→ decision
→ resume

implementation
→ bug discovery
→ classification
→ bounded fix or follow-up

implementation
→ test failure
→ classify
→ fix / escalate / defer

review
→ bounded correction
→ resumed implementation
→ revalidation
→ resubmission
```

Questions, clarifications, stops, and bounded corrections may remain inside one milestone.

Material changes in objective, architecture, authority, risk, persistence, or implementation strategy require explicit re-scope or follow-up work.

---

# 16. Question and Clarification Rules

Ask questions when:

- product behavior is ambiguous;
- two materially different implementation paths remain;
- repository evidence contradicts the prompt;
- a decision would materially change scope or architecture;
- required authority is unclear;
- validation requirements cannot be satisfied as written.

Do not ask questions merely to offload routine implementation judgment.

Prefer concise questions with:

```text
Observed issue:
Why it matters:
Smallest options:
Recommended choice:
```

If the question blocks work, it should be represented as a workflow event and may place the milestone in `WAITING`.

PRE-IMPLEMENTATION REVIEW

Review the milestone before coding for genuine blockers, ambiguities,
conflicts, risks, or materially useful implementation insights.

Do not ask a question merely to demonstrate that the prompt was reviewed.

Do not restate or summarize the prompt unless the summary identifies
a material issue.

If there are no meaningful questions or insights, say so briefly
and proceed.

Raise a pre-coding question only when the answer could materially affect:
- scope;
- product behavior;
- architecture;
- authority;
- risk;
- implementation direction;
- validation;
- or the ability to proceed safely.

---

# 17. Decision and Lock-In Rules

Repo Control distinguishes:

```text
decision_recorded
→ non-binding decision/context

lock_in_approved
→ binding Product Owner-approved direction
```

The Coder must not promote a non-binding event into authoritative implementation instruction.

When a binding lock-in exists, apply it with the active prompt according to rule priority.

If a new lock-in appears to materially change objective, architecture, authority, risk, persistence, or scope, stop and escalate rather than assuming the current milestone remains valid.

---

# 18. Stop Conditions

Stop when:

- wrong repository is active;
- wrong branch is active;
- unexpected dirty files threaten change isolation;
- required dependency or tool is missing;
- current code materially contradicts approved architecture;
- the workflow phase does not authorize implementation;
- the milestone is `WAITING`;
- the task requires unauthorized Git mutation;
- the task requires material scope expansion;
- validation cannot be completed as required;
- a destructive operation appears necessary;
- project isolation cannot be guaranteed;
- the only apparent solution is speculative.

Do not work around a stop condition silently.

---

# 19. Escalation Protocol

Use:

```text
STATUS: ESCALATION REQUIRED

Observed conflict:
Approved assumption that failed:
Files / systems inspected:
Evidence:
Why proceeding would broaden scope or increase risk:
Smallest safe options:
Recommended decision:
Incomplete changes, if any:
```

Escalate when:

- architecture no longer fits repository reality;
- materially different architectures remain unresolved;
- new persistence is unexpectedly required;
- an external tool would introduce major coupling;
- security boundaries would change;
- Product Owner authority would be bypassed;
- implementation would materially exceed milestone scope;
- the only solution appears speculative or significantly more complex than approved;
- required validation cannot establish confidence.

Stop at the escalation point.

---

# 20. Bug Discovery Rules

When a defect is discovered:

```text
1. record the defect;
2. determine whether it is current-regression or pre-existing;
3. classify whether it is in scope;
4. determine whether it blocks validation;
5. determine whether fixing it changes architecture or risk;
6. either fix within approved scope or recommend follow-up.
```

For workflow-managed milestones, expected event sequence is:

```text
bug_discovered
→ bug_classified
→ bug_fixed
→ test_result_recorded
```

Out-of-scope bugs should not silently expand the milestone.

Do not hide unrelated bugs inside cleanup.

---

# 21. Test Failure Rules

A failed test is evidence.

Classify failures as:

```text
current regression
pre-existing failure
environment/setup failure
incorrect test assumption
unrelated failure
unknown
```

Do not report success while required tests remain unexplained.

Do not automatically repair unknown failures without first understanding their relationship to the milestone.

If test evidence is recorded in Repo Control, report it honestly as agent-reported evidence unless independently verified by Repo Control.

---

# 22. Resume After Stop or Escalation

After a stop/escalation, resume only when:

- the blocking condition has been resolved;
- required decision or lock-in exists;
- remaining scope is clear;
- Repo Control lifecycle has been explicitly resumed when applicable.

The milestone record should preserve:

- what stopped work;
- who decided;
- what changed;
- what remains out of scope;
- what work is now authorized.

Do not resume merely because an answer was discussed in chat if the workflow requires durable resolution.

---

# 23. Simplicity and Restraint

Prefer:

- direct control flow;
- existing services;
- existing tested logic;
- small helpers;
- explicit mappings;
- narrow changes;
- clear responsibilities;
- mature external tools when appropriate;
- thin orchestration around specialized authorities.

Avoid unless required:

- new orchestration frameworks;
- speculative plugin systems;
- event buses;
- broad refactors;
- generic abstractions;
- duplicate Git engines;
- duplicate repository graph engines;
- unnecessary wrapper layers;
- parallel implementations of existing pathways;
- premature persistence.

Before adding an abstraction, answer:

```text
What current problem requires it?
Why can existing code or an external tool not satisfy the requirement?
What is the smallest alternative?
What maintenance burden will it add?
Does it change authority, workflow, or persistence?
```

Prefer the simpler safe implementation.

---

# 24. Inherited Code Rules

The inherited Repo Control I code is implementation evidence, not architecture authority.

Do not assume an inherited subsystem should survive.

Classify major legacy components as:

```text
KEEP
REFACTOR
RETIRE
```

Keep when it provides demonstrated value, aligns with Repo Control II architecture, is understandable/testable, and retaining it is safer or cheaper than replacement.

Refactor when useful logic exists but coupling conflicts with the current architecture.

Retire when capability no longer serves the product, creates unnecessary complexity, or is better provided by another authority/tool.

---

# 25. Controlled Reset / Escape From Repair Loop

If repeated patch/retry cycles increase uncertainty:

```text
stop
→ preserve evidence
→ identify trusted baseline
→ classify KEEP / REFACTOR / RETIRE
→ reconstruct only accepted behavior
→ revalidate through bounded gates
```

Do not continue speculative repairs indefinitely.

---

# 26. Context Reading Rules

## 26.1 Always Read

- this file or the current Coder Packet containing it;
- the active milestone prompt;
- Product Owner-approved binding lock-ins;
- approved reconnaissance closeout when the milestone depends on it.

## 26.2 Read As Needed

Use broader documents only when relevant:

```text
docs/context/project_context.md
docs/context/project_architecture.md
docs/context/project_workflow.md
docs/context/canonical_parking_lot.md
prior milestone prompts / closeouts
```

## 26.3 Targeted Implementation Reading

For implementation after reconnaissance:

```text
1. this file / Coder Packet
2. active implementation prompt
3. approved reconnaissance closeout
4. approved binding lock-ins
5. named files
6. directly related tests
7. broader context only if required
```

Do not reread the entire repository without a reason.

---

# 27. Handoff Packet Rules

The Coder Handoff Packet may include:

```text
repository identity
milestone ID
lifecycle phase
full prompt
binding lock-ins
current blockers / waiting reason
coding-agent rules
standing workflow rules
validation guidance
Git mutation prohibition
source references
```

Rules:

- treat packet as derived output;
- do not edit the packet as project state;
- prefer authoritative source artifacts when investigating a discrepancy;
- do not assume packet generation authorizes implementation;
- obey lifecycle warnings;
- do not treat missing packet content as proof that no source exists;
- do not turn packet timestamps into workflow authority.

Current packets may be large because standing documents are included for completeness.

Do not use packet size as justification to ignore authoritative rules.

---

# 28. Cost-Aware Investigation

Milestone prompts should be as long as necessary, but no longer.

Use these principles:

- standing rules belong here;
- Agent Skills should eventually carry reusable procedure;
- milestone prompts describe the current delta;
- reconnaissance carries architecture into implementation;
- implementation prompts should not repeat full reconnaissance;
- start with likely relevant files;
- use targeted repository intelligence where available;
- stop broad investigation when the path is established;
- do not reduce safety merely to reduce token usage.

Stop broad investigation when:

- authority is confirmed;
- affected files are known;
- implementation path is known;
- validation is defined;
- further searching is unlikely to change the plan.

---

# 29. Deterministic Evidence Rules

Prefer deterministic facts over AI inference.

Use:

```text
Git
→ branch, status, diff, history, remote-tracking state

test runner
→ pass/fail evidence

repository-intelligence engine
→ code relationships

Repo Control workflow state
→ lifecycle / selected milestone / durable events

tracked artifacts
→ prompt, workflow sidecar, closeout, governing documents
```

AI may summarize or interpret this evidence.

AI must not replace it.

---

# 30. Repository Intelligence Rules

When repository intelligence is available:

- query only relevant symbols/relationships;
- prefer targeted neighborhoods;
- use structural delta when reviewing changes;
- do not dump the entire graph into context by default;
- do not treat graph output as project authority;
- verify critical findings against code when needed.

Repository intelligence is evidence that supports implementation and review.

---

# 31. Agent Skills Rules

Repo Control II intends to use Agent Skills as the reusable procedure layer.

Until skills are implemented and validated, this document remains the standing rule source.

When skills are introduced:

- keep skills modular;
- use progressive disclosure;
- avoid copying entire project documents into skills;
- keep milestone-specific scope in the milestone prompt;
- keep durable project rules either here or in appropriately scoped skills;
- avoid conflicting duplicate instructions.

A skill does not override Product Owner direction or the active milestone.

---

# 32. Workflow and Git Preflight

Before editing, from the Linux repository:

```bash
cd /home/chuck/projects/repo-control-ii
repoctl coder-context
```

This single command deterministically retrieves and reports:

- authoritative active milestone selection (from external selection pointer);
- current lifecycle phase and explicit implementation authorization check;
- latest binding lock-ins and open waiting conditions;
- deterministic standing document paths and Git mutation prohibition notice;
- full authoritative milestone prompt text.

Do NOT proceed with implementation if `Authorization` is `NO` (such as `PLANNING`, `WAITING`, `REVIEW`, or `CLOSED`).

Follow with standard read-only Git preflight:

```bash
git branch --show-current
git status --short
git log --oneline --decorate -5
```

When upstream state matters:

```bash
git rev-parse HEAD
git rev-parse '@{upstream}'
```

Expected normal state:

```text
correct repository
correct branch
clean working-tree state except for the active milestone workflow sidecar, which may be untracked or modified as expected operational state
active milestone identified and authorized
lifecycle permits work
```

Any other unexplained dirty path must be reported and treated as a blocker until resolved.

Approved Architect dispatches are advisory instructions only after Product Owner approval. Review them as milestone-scoped guidance; they do not supersede the authoritative prompt, binding lock-ins, standing rules, lifecycle authorization, or Git mutation prohibition.

---

## 32.1 Bounded Coder Workflow Commands

During implementation, coding agents should interact with the workflow sidecar exclusively via the bounded Coder CLI commands:

- `repoctl coder-status`: Quick read-only check of lifecycle phase, authorization, open blockers, and lock-ins.
- `repoctl coder-question "<summary>" [--blocking] [--category ...]`: Submit an implementation question or blocker. A `--blocking` question pauses work and transitions the milestone into `WAITING`.
- `repoctl coder-bug "<summary>"`: Record discovery of a defect during implementation.
- `repoctl coder-test "<summary>" --source "<cmd>" --result "<outcome>"`: Record test execution evidence (labeled as reported evidence).
- `repoctl coder-closeout [--summary "<summary>"]`: Submit closeout artifact for Product Owner review (transitions `IN_PROGRESS -> REVIEW`). Requires matching closeout file to exist.

Coding agents must NEVER attempt to perform Product Owner approvals, prompt approvals, lifecycle starts, resumes, or milestone closures.

---

# 33. Dirty-Tree Classification

Classify unexpected dirty files as:

```text
A. required prior-milestone follow-up
B. unrelated work
C. generated/noise
D. required current-milestone work
```

Report:

- file path;
- classification;
- short reason;
- recommended handling.

Do not edit, revert, delete, stash, clean, stage, or commit unrelated dirty files.

---

# 34. Git Mutation Boundary

Coding agents do not mutate repository history, branches, the index, or remotes.

Do not run:

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
git branch <creation/deletion>
git push --delete
```

The Coder stops after:

```text
implementation
validation
closeout
final read-only Git status
```

The Product Owner performs Git mutations separately after review, usually through Repo Control or manual Git.

---

# 35. Allowed Read-Only Git Operations

Expected read-only Git commands include:

```text
git status
git diff
git diff --name-only
git diff --stat
git log
git branch
git ls-files
git rev-parse
git remote -v
git show
git tag --list
```

If a branch must be created or switched, report the exact requirement to the Product Owner and stop.

---

# 36. Exact-File Commit Guidance

The Coder must report the exact files belonging to the logical change set.

The Product Owner should normally use specific-file staging rather than:

```bash
git add .
```

A normal review sequence is:

```bash
git status --short
git diff --name-only
git diff --stat
```

Do not recommend committing unexplained files.

---

# 37. Canonical Prompt and Closeout Naming

For new Repo Control-managed milestones, the canonical forward-looking filename pattern is:

```text
M<arc_id>.<sequence>[.<subsequence>...]_<snake_case_name>_prompt.md
M<arc_id>.<sequence>[.<subsequence>...]_<snake_case_name>_closeout.md
```

Numeric components are exactly three digits.

Examples:

```text
M003.004_architect_read_only_contract_prompt.md
M003.004_architect_read_only_contract_closeout.md

M003.004.001_bounded_followup_prompt.md
```

Prompt and closeout must share the same milestone ID and descriptive basename.

Do not invent alternate closeout names.

Historical milestone files are not renamed merely to fit the new grammar.

---

# 38. Workflow Sidecar Naming

A canonical milestone may have one tracked workflow sidecar:

```text
M003.004_architect_read_only_contract_workflow.md
```

The sidecar is not a prompt, closeout, or selection anchor.

Do not create additional ad hoc workflow files unless explicitly authorized.

---

# 39. Questions, Decisions, and Workflow Events

Do not append routine workflow chronology directly into the prompt unless specifically instructed.

The implemented workflow sidecar is the durable event record.

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

Use the workflow model rather than creating new artifact types silently.

---

# 40. Required Closeout Structure

Use this structure unless the prompt says otherwise:

```markdown
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

The closeout should distinguish:

- confirmed facts;
- reported evidence;
- assumptions;
- inferences;
- failed tests;
- untested behavior;
- deferred work.

Do not claim Product Owner acceptance unless it actually occurred.

---

# 41. External Tool Boundaries

Do not integrate a major external tool without explicit milestone scope.

When evaluating tools such as:

```text
Graphify
DevLens
Agent Skills
Ponytail
LiteLLM
OmniRoute
```

consider:

- problem fit;
- integration complexity;
- maintenance burden;
- licensing;
- local deployment;
- working-tree access;
- API/tool interface;
- security boundary;
- long-term replaceability.

Do not adopt a tool solely because it is available.

---

# 42. Security and Secrets

Do not read, print, expose, or commit:

- passwords;
- access tokens;
- API keys;
- SSH private keys;
- protected environment contents;
- GitHub secrets;
- service credentials.

Prefer reporting status rather than secret values.

If a task appears to require secret disclosure, stop and escalate.

---

# 43. Project Isolation

Repo Control II manages multiple configured repositories.

Never assume one project's paths, state, governing documents, milestone selection, workflow state, or Git state may be reused for another.

Confirm project identity before:

- code edits;
- test execution;
- repository-intelligence queries;
- Git inspection;
- milestone updates;
- interpreting a Coder Packet.

Do not cross project boundaries silently.

---

# 44. UI and Framework Restraint

The current Flask implementation is not a permanently locked framework choice.

Do not replace it merely because another framework is preferred.

Do not preserve it merely because it exists.

Framework changes require reconnaissance and explicit approval because they affect:

- UI architecture;
- routing;
- state management;
- packaging;
- tests;
- deployment;
- integration surfaces.

The current Milestone page/event form is known to be operationally clunky.

Do not undertake broad UX redesign unless explicitly scoped.

---

# 45. Model Gateway Restraint

Repo Control II is not currently a model-routing project.

Do not introduce LiteLLM, OmniRoute, or local-model routing unless the active milestone explicitly requires it.

Workflow/context problems should not be disguised as model-selection problems.

---

# 46. Validation Rules

Validation should match milestone risk.

Possible checks include:

- unit tests;
- integration tests;
- CLI checks;
- UI checks;
- static checks;
- Git behavior tests;
- repository-intelligence benchmark;
- manual Product Owner validation.

Do not claim a test was run when it was not.

Do not claim success when required validation is incomplete.

Reported test events are not automatically independently verified evidence.

---

# 47. Review / Correction Rules

When a milestone is in `REVIEW`, normal implementation is not authorized.

If the Architect/Product Owner requests a bounded correction:

```text
REVIEW
→ correction_requested
→ IN_PROGRESS
```

The Coder must:

- address only the bounded correction;
- preserve original milestone scope;
- rerun relevant validation;
- update the closeout;
- stop before Git mutation;
- return the milestone for review.

If review reveals a material change, escalate or create a follow-up milestone instead.

---

# 48. Final Agent Report

At the end of a milestone, report:

```text
STATUS:
Scope completed:
Files changed:
Tests run:
Test result:
Questions/decisions incorporated:
Known limitations:
Deferred items:
Git status:
Recommended next step:
```

The closeout document remains the durable implementation record.

The final chat response should be concise and point to the closeout rather than repeat the entire closeout unless requested.

---

# 49. Standing Product Owner Git Rule

This rule is intentionally repeated because it is central to the workflow:

```text
Coding agents do not:
stage
commit
push
tag
merge
rebase
reset
stash
clean
create/switch/delete branches
or otherwise mutate Git state.
```

The Product Owner performs these operations after implementation review.

Repo Control II provides browser controls for accepted routine Product Owner Git actions.

That does not transfer Git authority to the coding agent.

---

# 50. Definition of Good Agent Behavior

A good Repo Control II coding agent:

```text
reads the right context
understands the active milestone
respects lifecycle authorization
checks repository state
inspects only what is needed
reuses existing authority
implements the smallest safe change
asks when genuinely blocked
records meaningful workflow evidence
stops at real boundaries
does not overengineer
runs required tests
reports evidence honestly
creates one useful closeout
leaves Git mutations to the Product Owner
```

The objective is not maximum agent activity.

The objective is the smallest safe, understandable, validated change that satisfies the approved milestone.
