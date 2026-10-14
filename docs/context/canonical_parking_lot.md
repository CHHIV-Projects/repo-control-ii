# CANONICAL_PARKING_LOT.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Forward-looking record of deferred, conditional, exploratory, or not-yet-prioritized work  
**Project phase:** Git Control complete; Workflow Orchestration substantially implemented through M003.006 (Persistent Read-Only Architect MCP Tunnel Active; Governing Document Reconciliation in M003.007)
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`  
**Current status:** Active deferred workflow/UX and packet-optimization items refined for post-M003 baseline

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

Examples include:

```text
future lifecycle UX hardening
future packet-context optimization
future Architect connector
future Coder handoff automation
future workflow-state notifications
```

These remain Parking Lot items until promoted into an approved milestone and, if accepted, reflected in governing workflow.

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

They are not current Parking Lot entries unless explicitly listed in the active-entry section below.

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

# 22. Active Repo Control II Parking Lot

## WORKFLOW-001 — Milestone Lifecycle UX Hardening

### Status

Deferred — Near-Term Candidate

### Summary

Redesign the Milestone-page lifecycle controls after several real workflow runs so the user is presented with phase-appropriate actions instead of the current dense, universal **Record a meaningful workflow event** form.

The intended direction is context-sensitive actions such as:

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

### Why It Is Parked

The M003.002 implementation intentionally prioritized a complete, explicit event model over polished interaction design.

The current form is functional but clunky.

Redesigning it immediately would risk optimizing around assumptions rather than observed usage.

### Trigger for Reconsideration

Revisit after several real milestones have been operated through the lifecycle and the Product Owner can identify:

- which actions are actually used;
- which fields are routinely unnecessary;
- which events need shortcuts;
- where mistakes or confusion occur;
- whether phase-aware actions materially improve usability.

### Likely Scope

```text
Milestone page UX
phase-aware action rendering
event form simplification
validation/error presentation
workflow action labels
possibly event-history presentation
```

### Dependencies

The existing lifecycle/event backend should remain stable enough to serve as the semantic foundation.

### Notes

Prefer improving the UI around the existing event model rather than changing lifecycle semantics solely for presentation convenience.

---

## WORKFLOW-002 — Handoff Packet Context Optimization

### Status

Deferred — Near-Term Candidate (Partially Superseded for Live Architect by MCP Tunnel)

### Summary

Optimize context size and avoid repeating standing documents in Coder Handoff Packets and fallback Architect Review Packets while preserving authoritative instructions, binding decisions, deterministic evidence, and explicit source references.

Note: With the operational acceptance of M003.006 (Secure MCP Tunnel), live Architect review sessions no longer rely on large monolithic review packets; ChatGPT queries individual endpoints on demand. However, manual Architect Review Packets remain supported as an operational fallback, and Coder Handoff Packets still bundle full prompts and standing rules.

### Why It Is Parked

The initial implementation deliberately favored correctness and completeness over aggressive context reduction.

With Architect live evidence retrieval now handled primarily via the MCP tunnel, the context burden is shifted primarily to Coder handoffs. Context optimization for Coder packets is best evaluated alongside the upcoming R004 Agent Skills work, which may provide a cleaner progressive disclosure model for standing procedure.

### Trigger for Reconsideration

Revisit during or following R004 (Agent Skills) to evaluate:

- whether Agent Skills replace standing document embedding in Coder packets;
- which prompt sections and rules are essential in Coder packets versus referenceable;
- whether fallback Architect Review Packets need a compact mode;
- token-efficiency metrics and context window limits for coding agents.

### Likely Scope

```text
Coder handoff packet assembly policy
fallback Architect packet inclusion rules
progressive disclosure via Agent Skills
source-reference strategy
packet size and token-efficiency metrics
optional compact/full packet modes if justified
```

### Dependencies

Useful evidence should come from R004 Agent Skills design and real Coder execution experience.

### Notes

Do not silently summarize or truncate binding instructions.

Any optimization should preserve provenance and make omitted/referenced sources explicit.

---

## WORKFLOW-003 — Future Coder MCP Integration

### Status

Deferred

### Summary

Evaluate and implement an MCP (Model Context Protocol) transport interface for Coder agents (e.g., Claude Desktop, Cline, or external autonomous agents) if non-local or containerized coding agents are supported in the future.

### Why It Is Parked

The M003.008 architectural reconnaissance confirmed that the primary Coder operates co-located with the repository on `henderson-server1` via VS Code Remote SSH. For co-located coding agents, direct in-process Python CLI execution (`repoctl coder-context`, `repoctl coder-status`, and bounded event submission commands) meets all workflow requirements with zero network attack surface, zero daemon dependencies, zero authentication overhead, and zero protocol translation complexity. Introducing an MCP server for the Coder would add unnecessary operational surface without current functional benefit.

### Trigger for Reconsideration

Revisit only if:

- a non-local or containerized coding agent without local shell/Python execution access becomes a primary project tool;
- an external coding assistant platform requires standardized MCP server discovery for repository context and event submission;
- the Product Owner explicitly decides to support remote coding agents across network boundaries.

### Likely Scope

```text
src/repoctl/mcp/coder_adapter.py (or unified adapter)
Coder MCP tool definitions (context, status, question, bug, test, closeout)
transport security and authentication for remote Coder clients
ops/systemd service configuration if networked
```

### Dependencies

Requires a clear use case for non-local Coder execution that cannot be served by local CLI or VS Code Remote SSH.

### Notes

If ever implemented, Coder MCP must strictly adhere to the same Model B bounded write boundary established in M003.009: hardcoded `actor_role="coder"`, `po_approved=False`, strictly non-authoritative event types, zero Git mutation authority, and zero caller-selectable generic event submission.

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
