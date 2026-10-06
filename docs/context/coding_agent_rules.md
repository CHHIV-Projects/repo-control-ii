# CODING_AGENT_RULES.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Standing project-wide rules for coding agents  
**Project phase:** Clean restart and architecture definition  
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `https://github.com/CHHIV-Projects/repo-control-ii.git`  
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
- compatibility with future Agent Skills.

Milestone prompts should reference this document rather than repeat every standing rule.

This document does not replace:

- the active milestone prompt;
- approved prompt addenda;
- current Product Owner direction;
- current repository code;
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
2. active milestone prompt and approved addenda / lock-ins
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

Normal Git and repository commands run in:

```text
VS Code Remote SSH / Linux terminal
```

GitHub remote:

```text
https://github.com/CHHIV-Projects/repo-control-ii.git
```

Do not assume a Windows checkout is authoritative.

Do not treat GitHub remote state as equivalent to the local working tree.

---

# 4. Core Agent Workflow

For every milestone:

```text
1. Read this file.
2. Read the active milestone prompt and approved addenda.
3. Read the approved reconnaissance closeout when referenced.
4. Confirm the authoritative repository.
5. Perform read-only Git preflight.
6. Confirm current branch and working-tree state.
7. Confirm milestone mode.
8. Load only relevant broader context.
9. Inspect the targeted implementation paths.
10. Confirm the milestone boundary.
11. Ask only genuinely blocking questions.
12. Escalate when the approved roadmap is materially insufficient.
13. Implement only approved scope.
14. Run relevant validation.
15. Create one closeout.
16. Report final read-only Git status.
17. Stop before Product Owner Git actions.
```

The agent must not infer live repository state solely from documentation.

The agent must not infer project architecture solely from inherited code when governing documents explicitly define a new architecture.

---

# 5. Milestone Modes

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

# 6. Reconnaissance-Only Mode

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

# 7. Implementation-After-Reconnaissance Mode

When implementation follows approved reconnaissance:

```text
1. read this file;
2. read the implementation prompt;
3. read the approved reconnaissance closeout;
4. read approved addenda / lock-ins;
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

# 8. Direct Low-Risk Implementation Mode

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

# 9. Validation-Only Mode

Validation-only milestones establish evidence without changing implementation.

In validation-only mode:

- confirm the approved target repository/environment;
- run the approved checks;
- collect the required evidence;
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
recommend the smallest separate fix when needed
```

Validation-only work must not silently become repair work.

---

# 10. Documentation-Only Mode

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

# 11. Bug-Fix Follow-Up Mode

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

# 12. Integration / Tool Evaluation Mode

For tool evaluation:

- define the exact problem being solved;
- inspect candidate capabilities;
- define evaluation criteria;
- test on a realistic repository/workload;
- compare integration burden;
- compare licensing implications;
- compare operational complexity;
- recommend one direction;
- do not adopt or deeply integrate a tool unless the milestone explicitly authorizes it.

This applies to candidates such as:

```text
GitButler
Graphify
DevLens
Agent Skills
Ponytail
LiteLLM
OmniRoute
coding-agent alternatives
```

---

# 13. Iterative Milestone Behavior

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
```

Questions, clarifications, stops, and bounded corrections may remain inside one milestone.

Material changes in objective, architecture, authority, risk, persistence, or implementation strategy require explicit re-scope or follow-up work.

---

# 14. Question and Clarification Rules

Ask questions when:

- product behavior is ambiguous;
- two materially different implementation paths remain;
- repository evidence contradicts the prompt;
- a decision would change scope or architecture;
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

---

# 15. Stop Conditions

Stop when:

- wrong repository is active;
- wrong branch is active;
- unexpected dirty files threaten change isolation;
- required dependency or tool is missing;
- current code materially contradicts approved architecture;
- the task requires unauthorized Git mutation;
- the task requires material scope expansion;
- validation cannot be completed as required;
- a destructive operation appears necessary;
- project isolation cannot be guaranteed;
- the only apparent solution is speculative.

Do not work around a stop condition silently.

---

# 16. Escalation Protocol

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
- two or more materially different architectures remain unresolved;
- new persistence is required unexpectedly;
- an external tool would introduce major coupling;
- security boundaries would change;
- Product Owner authority would be bypassed;
- implementation would materially exceed milestone scope;
- the only solution appears speculative or significantly more complex than approved;
- required validation cannot establish confidence.

Stop at the escalation point.

---

# 17. Bug Discovery Rules

When a defect is discovered:

```text
1. record the defect;
2. determine whether it is current-regression or pre-existing;
3. classify whether it is in scope;
4. determine whether it blocks validation;
5. determine whether a fix changes architecture or risk;
6. either fix within approved scope or recommend follow-up.
```

Do not hide unrelated bugs inside cleanup.

Do not silently rewrite the milestone around a newly discovered defect.

---

# 18. Test Failure Rules

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

---

# 19. Resume After Stop or Escalation

After a stop/escalation, resume only when the decision is explicit enough to proceed safely.

The milestone record should preserve:

- what stopped work;
- who decided;
- what changed;
- what remains out of scope;
- what work is now authorized.

---

# 20. Simplicity and Restraint

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

# 21. Inherited Code Rules

The inherited Repo Control I code is implementation evidence, not architecture authority.

Do not assume an inherited subsystem should survive.

Classify major legacy components as:

```text
KEEP
REFACTOR
RETIRE
```

Keep when:

- it provides demonstrated value;
- it aligns with Repo Control II architecture;
- it is testable;
- it is understandable;
- retaining it is safer or cheaper than replacement.

Refactor when useful logic exists but current coupling conflicts with the new architecture.

Retire when the capability no longer serves the product, creates unnecessary complexity, or is better provided by an external tool.

Do not rewrite before reconnaissance establishes the correct path.

---

# 22. Controlled Reset / Escape From Repair Loop

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

# 23. Context Reading Rules

## 23.1 Always Read

- this file;
- the active milestone prompt;
- approved prompt addenda / lock-ins;
- approved reconnaissance closeout when the milestone depends on it.

## 23.2 Read As Needed

Use broader documents only when relevant:

```text
docs/context/project_context.md
docs/context/project_architecture.md
docs/context/project_workflow.md
docs/context/canonical_parking_lot.md
prior milestone prompts / closeouts
```

## 23.3 Targeted Implementation Reading

For implementation after reconnaissance:

```text
1. this file
2. active implementation prompt
3. approved reconnaissance closeout
4. approved addenda
5. named files
6. directly related tests
7. broader context only if required
```

Do not reread the entire repository without a reason.

---

# 24. Cost-Aware Investigation

Milestone prompts should be as long as necessary, but no longer.

Use these principles:

- standing rules belong here;
- Agent Skills should eventually carry reusable procedure;
- milestone prompts describe the current delta;
- reconnaissance carries architecture into implementation;
- implementation prompts should not repeat the full reconnaissance;
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

# 25. Deterministic Evidence Rules

Prefer deterministic facts over AI inference.

Use:

```text
Git
→ branch, status, diff, history, remote state

test runner
→ pass/fail evidence

repository-intelligence engine
→ code relationships

Repo Control workflow state
→ milestone status

tracked artifacts
→ prompt, addenda, closeout, decisions
```

AI may summarize or interpret this evidence.

AI must not replace it.

---

# 26. Repository Intelligence Rules

When repository intelligence is available:

- query only relevant symbols/relationships;
- prefer targeted neighborhoods;
- use structural delta when reviewing changes;
- do not dump the entire graph into context by default;
- do not treat graph output as project authority;
- verify critical findings against code when needed.

Repository intelligence is evidence that supports implementation and review.

---

# 27. Agent Skills Rules

Repo Control II intends to use Agent Skills as the reusable procedure layer.

Until skills are implemented, this document remains the standing rule source.

When skills are introduced:

- keep skills modular;
- use progressive disclosure;
- avoid copying entire project documents into skills;
- keep milestone-specific scope in the milestone prompt;
- keep durable project rules either here or in appropriately scoped skills;
- avoid conflicting duplicate instructions.

A skill does not override Product Owner direction or the active milestone.

---

# 28. Git Preflight

Before editing, from the Linux repository:

```bash
cd /home/chuck/projects/repo-control-ii
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
known working-tree state
active milestone identified
```

Unexpected dirty files must be reported.

---

# 29. Dirty-Tree Classification

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

# 30. Git Mutation Boundary

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

The Product Owner performs Git mutations separately after review.

---

# 31. Allowed Read-Only Git Operations

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

# 32. Exact-File Commit Guidance

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

The Product Owner then stages only the intended files.

Do not recommend committing unexplained files.

---

# 33. Prompt and Closeout Naming

The active milestone prompt is authoritative for:

- milestone ID;
- title;
- prompt filename;
- closeout filename;
- deliverables.

Recommended pattern:

```text
<milestone>_<exact_snake_case_name>_prompt.md
<milestone>_<exact_snake_case_name>_closeout.md
```

Prompt and closeout should use the same descriptive basename.

Do not invent an alternate closeout filename.

Do not create extra human-authored report files unless requested.

---

# 34. Prompt Addenda and Q&A

When questions or decisions occur, preserve them in the active milestone record.

Recommended headings:

```text
## Coder Questions / Answers Round 1
## Coder Questions / Answers Round 2
## Escalation / Stop Decision 1
## Bug Discovery / Classification 1
## Final Lock-ins
```

A clarification that preserves the original objective may remain inside the milestone.

A material scope or architecture change requires explicit re-scope.

---

# 35. Required Closeout Structure

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
- assumptions;
- inferences;
- failed tests;
- untested behavior;
- deferred work.

---

# 36. External Tool Boundaries

Do not integrate a major external tool without explicit milestone scope.

When evaluating:

```text
GitButler
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

# 37. Security and Secrets

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

# 38. Project Isolation

Repo Control II may eventually manage multiple repositories.

Never assume one project's paths, state, or workflow may be reused for another.

Confirm project identity before:

- code edits;
- test execution;
- repository intelligence queries;
- Git inspection;
- milestone updates.

Do not cross project boundaries silently.

---

# 39. UI and Framework Restraint

The inherited Flask implementation is not a locked framework choice.

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

---

# 40. Model Gateway Restraint

Repo Control II is not currently a model-routing project.

Do not introduce LiteLLM, OmniRoute, or local-model routing unless the active milestone explicitly requires it.

Workflow/context problems should not be disguised as model-selection problems.

---

# 41. Validation Rules

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

---

# 42. Final Agent Report

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

The closeout document remains the durable record.

The final chat response should be concise and point to the closeout rather than repeat the entire closeout unless requested.

---

# 43. Standing Product Owner Git Rule

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

Repo Control II may eventually provide graphical controls that make those Product Owner actions easier and safer.

That does not transfer Git authority to the coding agent.

---

# 44. Definition of Good Agent Behavior

A good Repo Control II coding agent:

```text
reads the right context
understands the milestone
checks repository state
inspects only what is needed
reuses existing authority
implements the smallest safe change
asks when genuinely blocked
stops at real boundaries
does not overengineer
runs the required tests
reports evidence honestly
creates one useful closeout
leaves Git mutations to the Product Owner
```

The objective is not maximum agent activity.

The objective is the smallest safe, understandable, validated change that satisfies the approved milestone.
