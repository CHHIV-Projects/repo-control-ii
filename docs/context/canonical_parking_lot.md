# CANONICAL_PARKING_LOT.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Forward-looking record of deferred, conditional, exploratory, or not-yet-prioritized work  
**Project phase:** Clean restart and architecture definition  
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `https://github.com/CHHIV-Projects/repo-control-ii.git`  
**Current status:** Framework established; no active Parking Lot entries yet

---

# 1. Purpose

The Canonical Parking Lot records work that is:

- future;
- deferred;
- conditional;
- exploratory;
- intentionally not yet implemented;
- awaiting reconnaissance;
- awaiting prioritization;
- blocked by another milestone or architecture decision;
- useful enough to preserve but not appropriate for the current active scope.

Its purpose is to:

```text
keep active milestones focused
preserve useful ideas without expanding current scope
separate future possibilities from accepted architecture
capture follow-up work discovered during implementation
provide candidates for future milestone arcs
prevent chat-only ideas from being lost
```

The Parking Lot is a **forward-looking planning document**.

It is not a historical record.

---

# 2. What Does Not Belong Here

The Parking Lot is not:

- an active milestone prompt;
- a milestone closeout;
- a bug tracker for defects that require immediate action;
- a replacement for project architecture;
- a replacement for project context;
- a record of completed milestones;
- a changelog;
- a Git history;
- a list of every idea mentioned in conversation;
- a commitment that every item will eventually be implemented.

Completed work should not remain in this document merely for historical preservation.

History belongs in:

```text
milestone prompts
milestone closeouts
Git history
current governing documents
other maintained historical records when explicitly required
```

---

# 3. Maintenance Rule

When a Parking Lot item is completed, rejected, superseded, or otherwise resolved:

```text
remove it from the active Parking Lot
```

Preserve the relevant historical evidence elsewhere.

The Parking Lot should contain only work that remains genuinely unresolved, deferred, conditional, exploratory, or future-facing.

Do not let it become an archive of old ideas.

---

# 4. Entry Criteria

Add an item when all of the following are true:

1. the item is relevant enough to Repo Control II to preserve;
2. it is not appropriate to implement in the current active milestone;
3. it has a recognizable future decision or action;
4. losing the item would likely cause useful work or reasoning to be repeated later.

Do not add an item when it is:

- merely speculative;
- too vague to describe;
- already fully covered by an active milestone;
- a duplicate of an existing Parking Lot item;
- already completed;
- clearly rejected with no expected reconsideration.

---

# 5. Sources of Parking Lot Items

Parking Lot items may originate from:

- Product Owner ideas;
- Architect recommendations;
- Coder discoveries;
- reconnaissance findings;
- implementation limitations;
- validation gaps;
- external-tool evaluations;
- deferred UX improvements;
- performance observations;
- future integration opportunities;
- architecture questions intentionally postponed;
- model/tool experiments not yet justified;
- follow-up work discovered during milestone review.

A Parking Lot entry should state where useful why the work was deferred.

---

# 6. Relationship to Active Milestones

The active milestone remains authoritative for current work.

When new work is discovered during implementation:

```text
Does it fit safely inside current approved scope?
    │
    ├── Yes
    │    → handle inside milestone
    │
    └── No
         ↓
Does it require immediate separate work?
    │
    ├── Yes
    │    → create follow-up milestone
    │
    └── No
         ↓
      Parking Lot
```

The Parking Lot must not be used to avoid creating a necessary follow-up milestone.

Likewise, a milestone must not silently expand merely to avoid parking a future item.

---

# 7. Relationship to Project Context

`project_context.md` describes:

```text
current project truth
current baseline
current product direction
current near-term priorities
```

The Parking Lot describes:

```text
not-current work
deferred possibilities
conditional future work
unresolved options
```

An item should move from Parking Lot into Context only when it becomes part of the accepted current direction or baseline.

---

# 8. Relationship to Project Architecture

`project_architecture.md` defines accepted architecture.

The Parking Lot may contain:

- architecture alternatives not yet selected;
- possible future extensions;
- replacement candidates;
- integration ideas;
- unresolved architecture questions.

A Parking Lot item must not be written as though it is already accepted architecture.

Use language such as:

```text
candidate
possible
evaluate
consider
conditional
future
```

when the architecture is not yet decided.

---

# 9. Relationship to Workflow

`project_workflow.md` defines current working procedure.

The Parking Lot may contain future workflow improvements, but those improvements are not active workflow rules until formally adopted.

For example:

```text
future automation of prompt storage
future Architect connector
future Coder handoff automation
future workflow-state notifications
```

remain Parking Lot items until promoted into an approved milestone and then into governing workflow if accepted.

---

# 10. Relationship to Coding Agent Rules

`coding_agent_rules.md` defines current standing coding-agent behavior.

Potential future changes to agent procedure belong here only when they are not yet approved.

Once adopted, they should be removed from the Parking Lot and reflected in:

```text
coding_agent_rules.md
Agent Skills
project_workflow.md
or another appropriate maintained source
```

---

# 11. Entry Format

Each Parking Lot entry should use a compact standard format.

Recommended template:

```markdown
## <ID> — <Short Title>

### Status

Deferred | Conditional | Exploratory | Awaiting Reconnaissance | Awaiting Prioritization | Blocked

### Summary

Brief description of the future work.

### Why It Is Parked

Why this is not part of the current active milestone or roadmap position.

### Trigger for Reconsideration

What would make this item worth promoting into active work.

### Likely Scope

High-level areas that may be affected.

### Dependencies

Dependencies or prerequisite milestones, if any.

### Notes

Optional design thoughts, constraints, candidate tools, or prior conclusions.
```

Not every entry needs every subsection.

The entry should remain concise enough to scan.

---

# 12. Suggested Status Values

Use one of these when practical:

```text
Deferred
→ intentionally postponed

Conditional
→ pursue only if a future condition becomes true

Exploratory
→ idea worth preserving but not yet justified

Awaiting Reconnaissance
→ likely useful, but implementation reality is unknown

Awaiting Prioritization
→ understood enough to act on, but sequence not chosen

Blocked
→ cannot proceed until another decision or milestone completes
```

Avoid creating a large status taxonomy.

---

# 13. Suggested Priority Guidance

Parking Lot priority is optional.

When useful, use:

```text
Near-Term Candidate
Medium-Term Candidate
Long-Term Candidate
Conditional
```

Do not assign fake precision.

The active roadmap should remain the primary sequencing authority.

---

# 14. Suggested Item Identifiers

Until a more formal planning system exists, use stable category-prefixed IDs.

Possible categories:

```text
GIT-
AGENT-
INTEL-
WORKFLOW-
MODEL-
UI-
INTEGRATION-
SEC-
PERF-
OPS-
DOC-
```

Examples:

```text
GIT-001
AGENT-001
INTEL-001
WORKFLOW-001
MODEL-001
```

IDs should be stable once assigned.

Do not renumber merely to make the document look tidy.

---

# 15. Promotion to Active Work

When a Parking Lot item is selected for implementation:

```text
Parking Lot item
→ Architect reviews current relevance
→ reconnaissance if needed
→ milestone created
→ item removed from active Parking Lot when formally absorbed
→ milestone prompt becomes active authority
```

The Parking Lot item itself should not become the implementation prompt.

If useful, the milestone prompt may reference the former Parking Lot ID.

---

# 16. Deferred Tool and Integration Ideas

Repo Control II is expected to evaluate several external tools over time.

Potential items may eventually appear here for:

```text
GitButler
Graphify
DevLens
Agent Skills
Ponytail
LiteLLM
OmniRoute
Cline
OpenCode
Aider
Goose
OpenHands
other future integrations
```

A tool should not be placed here merely because it exists.

There must be a Repo Control II use case worth preserving.

---

# 17. Deferred Architecture Ideas

Possible future Parking Lot categories may include:

- alternate workflow persistence;
- custom Architect UI;
- richer Git graph visualization;
- multi-repository orchestration;
- local/cloud model routing;
- structural-delta review;
- automated milestone-state transitions;
- richer notification/automation;
- additional coding-agent integrations;
- project templates;
- release workflows;
- backup/restore of Repo Control operational state.

These examples are illustrative only.

They are not current Parking Lot entries.

---

# 18. Deferred UX Ideas

Possible future UX items may include:

- project dashboard refinements;
- richer milestone timeline;
- visual workflow-state graph;
- side-by-side Architect/Coder evidence views;
- Git operation preview;
- repository-intelligence browser;
- diff-focused review workspace;
- configurable project panels.

Again, these are examples of appropriate categories, not accepted requirements.

---

# 19. Deferred Model / Local AI Ideas

Potential future topics may include:

- local coding-model benchmarking;
- task-specific model routing;
- offline mode;
- local summarization;
- model-cost reporting;
- semantic routing;
- fallback strategies;
- model-quality scoring.

These should remain secondary to proven workflow needs.

Repo Control II must not become a model experimentation project by default.

---

# 20. Review Cadence

Review the Parking Lot:

- when beginning a new milestone arc;
- after major reconnaissance;
- after major architecture changes;
- when the active roadmap becomes short;
- when Product Owner priorities change;
- when an external dependency or capability changes materially.

Do not review or rewrite it mechanically after every small milestone.

---

# 21. Cleanup Rules

During review:

```text
remove completed items
remove rejected items with no realistic reconsideration
merge duplicates
rewrite unclear items
promote active candidates into milestones
retain unresolved useful items
```

The document should become shorter when work is completed.

Growth alone is not a sign of good planning.

---

# 22. Initial Repo Control II Parking Lot

No canonical Parking Lot entries are currently established.

Current state:

```text
No active entries.
```

This is intentional.

The project is still defining its baseline architecture and workflow.

The first likely entries should emerge from:

```text
inherited-code reconnaissance
Git-control evaluation
Agent Skills implementation
repository-intelligence evaluation
workflow-orchestration design
later model-gateway evaluation
```

Only genuinely deferred work should be added.

---

# 23. Entry Template

Copy this section when creating a new Parking Lot item:

```markdown
## <ID> — <Short Title>

### Status

<Deferred | Conditional | Exploratory | Awaiting Reconnaissance | Awaiting Prioritization | Blocked>

### Summary

<What future work or decision should be preserved?>

### Why It Is Parked

<Why is this not part of the current active work?>

### Trigger for Reconsideration

<What event, milestone, need, failure, or priority change should cause this item to return?>

### Likely Scope

<What parts of Repo Control II may be affected?>

### Dependencies

<Prerequisites or related work, if any.>

### Notes

<Optional constraints, candidate tools, prior conclusions, or useful references.>
```

---

# 24. Governing Principle

The Canonical Parking Lot exists to preserve future value without allowing future possibilities to dilute current work.

```text
Active milestone
→ do the work now

Follow-up milestone
→ important enough to do next

Parking Lot
→ preserve for later

Rejected
→ remove
```

The document should help Repo Control II remain focused, not make the project larger.
