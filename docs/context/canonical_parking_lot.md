# CANONICAL_PARKING_LOT.md — Repo Control II

## Document Status

**Project:** Repo Control II  
**Document role:** Forward-looking record of deferred, conditional, exploratory, or not-yet-prioritized work  
**Project phase:** Git Control complete; accepted and CLOSED through M009.010, including M009.008 context-verification audit, M009.009 Intelligence freshness/provenance, and M009.010 runtime compatibility and procedure-release safeguards; this document contains unresolved work only
**Authoritative repository:** `/home/chuck/projects/repo-control-ii` on `henderson-server1`  
**Remote repository:** `git@github-repo-control-ii:CHHIV-Projects/repo-control-ii.git`  
**Current status:** WORKFLOW-002, WORKFLOW-003, WORKFLOW-004, WORKFLOW-007, GIT-001, UI-001, INTEL-001, INTEL-003, and DOC-001 remain unresolved below. Managed-repository CLI resolution and fresh/resumed Coder-context convergence have been resolved; accepted current behavior and completed milestone history are documented in their owning project and milestone artifacts.

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

## WORKFLOW-002 — Handoff Packet Context Optimization

### Status

Deferred — fallback/export packet assembly only; live co-located Coder work uses the accepted local CLI, compact bootstrap, and Skills

### Summary

Optimize context size and avoid repeating standing documents in Coder Handoff Packets and fallback Architect Review Packets while preserving authoritative instructions, binding decisions, deterministic evidence, and explicit source references.

Live Architect review uses the accepted Secure MCP Tunnel and retrieves evidence on demand. Co-located Coder work normally uses `coder-context`, the compact always-loaded `coding_agent_rules.md` bootstrap, and the three accepted repository-local Agent Skills rather than a full handoff packet. Manual Architect Review Packets and Coder Handoff Packets remain available as fallback/export mechanisms, especially for disconnected or external use.

### Why It Is Parked

Native Agent Skills are implemented and accepted, and `coding_agent_rules.md` has been reduced to a compact authority/bootstrap layer. The remaining question is whether/how fallback packet assembly can use explicit references and progressive disclosure while preserving authoritative content, source provenance, and usability for disconnected or external Coders.

### Trigger for Reconsideration

Revisit when real fallback packet use or a concrete external/disconnected Coder need justifies evaluating:

- which prompt, bootstrap, Skill, and approved-decision content must be embedded versus referenced in Coder packets;
- whether fallback Architect Review Packets need a compact mode;
- how to make references and progressive disclosure usable when packet recipients cannot query the local CLI or load repository Skills;
- packet-size/context metrics when real use can be observed.

### Likely Scope

```text
Coder handoff packet assembly policy
fallback Architect packet inclusion rules
reference/provenance strategy for canonical prompts, bootstrap, Skills, and decisions
progressive disclosure compatible with fallback/export recipients
packet size/context metrics based on real use
optional compact/full packet modes if justified
```

### Dependencies

Useful evidence should come from real fallback packet usage and a concrete recipient/use case. The native Skill catalog and compact bootstrap are accepted current procedure, not pending work or authorization to optimize packet assembly.

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

## WORKFLOW-004 — Closeout Final-Status / Workflow-Sidecar Self-Invalidation

### Status

Deferred — not resolved by M008.000–M008.004

### Summary

Investigate and resolve the lifecycle timing problem where `coder-closeout` submission updates the tracked workflow sidecar after the closeout's final Git-status snapshot is prepared, which can make the closeout immediately stale upon entering REVIEW.

### Why It Is Parked

The issue concerns lifecycle/status representation and requires a separately scoped design that preserves an accurate audit trail. It is not implemented by M005.004.

### Trigger for Reconsideration

Revisit when Product Owner prioritizes eliminating correction loops caused solely by lifecycle-generated sidecar changes.

### Likely Scope

Preserve accurate reporting without requiring a correction loop merely to describe workflow-generated sidecar changes. Evaluate distinguishing pre-submission Git status from review-time authoritative status, or surfacing current Git/workflow status outside the static closeout.

### Dependencies / Notes

Keep closeout evidence auditable and distinguish repository status at preparation/submission from the subsequent workflow event. No fix is authorized by this entry.

---

## WORKFLOW-007 — Procedure Apply Parent-Directory Rename / Path-Confinement Hardening

### Status

Deferred — the accepted transaction documents this limitation; no mitigation is claimed.

### Summary

Study and validate safe path confinement for transactional shared-procedure Apply/Recover when a non-cooperating process renames or otherwise mutates target parent directories during file replacement.

### Why It Is Parked

The M008.004 fixture renamed a target parent after final path/identity validation but before replacement. The transaction wrote through an already-open parent directory descriptor into the moved directory, then detected the changed identity and blocked recovery because the transaction-created parent had disappeared. The tested rename stayed inside the fixture repository; no outside-root write was observed. A privileged or otherwise authorized external writer moving that open parent outside the managed worktree during the same narrow window presents a possible path escape that the current design does not exclude. The shared lock covers cooperating Repo Control operations and scans, not arbitrary external writers.

### Trigger for Reconsideration

Before extending Apply to environments with uncontrolled concurrent parent-directory mutation, multiuser writers, elevated privileges, or stronger path-containment guarantees; otherwise when the Product Owner separately prioritizes this security hardening.

### Likely Scope

Evaluate parent-path confinement across rename, mount, and path-swap races; descriptor and ancestor-identity validation; write ordering; safe recovery when transaction-created parents are missing or renamed; and feasibility on supported Linux filesystems. Require repeatable adversarial fixture tests and safe fail-closed behavior.

### Dependencies

Use the accepted M008.004 transaction/recovery behavior and its fixture finding as evidence. Any fix must retain the one-repository PO confirmation boundary, exact-image recovery, and no-silent-overwrite invariant; material architectural or privilege changes require separate approval.

### Notes

No code changes or mitigation claim are in M008.005. The outside-root escape is a possible, unobserved scenario—not an observed fixture result. Do not prescribe an unreviewed fix, exploit method, broader privilege, or silent auto-repair.

---

## GIT-001 — Browser Git Merge Support

### Status

Deferred

### Summary

Add a bounded Repo Control merge action for the common arc-closeout workflow, starting with safe fast-forward-only merge into the current branch.

### Why It Is Parked

Merge implementation is outside M005.000 reconnaissance and requires separately approved Git-control scope.

### Trigger for Reconsideration

Revisit when the Product Owner prioritizes browser support for the arc-closeout merge workflow.

### Likely Scope

Browser review and confirmation, Product Owner authorization, clean/divergence preflight checks, and a bounded fast-forward-only action into the current branch.

### Dependencies

Existing Git evidence, explicit mutation authorization, and separately approved implementation and validation.

### Notes

Preserve explicit review before mutation and the existing Coder no-Git-mutation boundary. No automatic conflict resolution. Consider non-fast-forward merge or conflict handling only as later expansion if justified.

---

## WORKFLOW-008 - Stale Host Bootstrap After Procedure Apply

### Status

Deferred

### Summary

Investigate why a fresh VS Code Copilot session received the 1.4.0 bootstrap after Repo Control II applied 1.5.0. The generated files on disk matched the published 1.5.0 manifest, and Repo Control context verification passed. Automatic instruction delivery failed. The source of the old text was not established.

### Why It Is Parked

The installation and context verification results are established. Keep the host-delivery investigation separate from the current database work. Do not treat manual recovery as proof of automatic delivery.

### Trigger for Reconsideration

Revisit before relying on automatic bootstrap for SQLite cutover, or sooner if another procedure update reproduces the mismatch.

### Likely Scope

Identify the instruction source used by a fresh Copilot session and compare the delivered text with the installed release. Add canonical guidance for a version mismatch: treat the host-supplied text as stale, follow the supported context refresh and verification procedure before consequential work, and record automatic delivery separately from coder-verify. A VERIFIED result does not prove the host loaded the current instructions.

### Notes

M010.013 evidence: automatic delivery failure event 8d3db7cee83e49ebae162f4629679742 and context verification event 0e6b632151f44771843b4e6c69318e47. Cache reuse and reading the committed Git copy remain hypotheses, not findings.

## UI-001 — Dashboard Refresh Status HTTP 500

### Status

Deferred — Repo Control II diagnosis required

### Summary

Diagnose the Vocab onboarding observation where Dashboard **Refresh Status** returned HTTP 500 / `unexpected_error`.

### Why It Is Parked

Git Review and Milestone views otherwise operated during the observation. The issue is a Repo Control II UI defect candidate and has not been diagnosed here; it is not a Vocab repository repair.

### Trigger for Reconsideration

Revisit when the Dashboard refresh failure is reproduced or prioritized for a focused Repo Control II investigation.

### Likely Scope

```text
reproduce Refresh Status failure with captured request/log evidence
identify the failing Dashboard status dependency
add focused regression coverage and report verified effects
```

### Dependencies

Requires a reproducible failure or sufficient service error evidence. Do not infer that the failed refresh changed Git or workflow state.

### Notes

Keep the defect classification and any repair within a separately authorized Repo Control II scope.

---

## INTEL-001 — Repository Intelligence Consumer-Rating Workflow

### Status

Deferred

### Summary

Make Repository Intelligence usage feedback reflect the experience of the Architect or Coder who consumed the evidence.

### Why It Is Parked

The system already supports optional per-use feedback, consumer identity, and the Coder `coder-intelligence-rate` command; M008.002 documented measurement and comparison but explicitly left a consumer-rating workflow redesign out of scope. The remaining question is whether further Coder/Architect-side feedback or consumer-centered usefulness reporting is justified by actual use. Historical unrated records remain unknown and must not be retroactively assigned.

### Trigger for Reconsideration

Revisit when usage measurement is used for an adoption or value decision and consumer feedback is still missing.

### Likely Scope

```text
Coder rating of materially relied-on uses before closeout
optional short consumer rationale
Architect-side rating when practical
consumer-centered usefulness metrics
```

### Dependencies

Must preserve optional feedback, recorded use identity, and the distinction between consumer experience and PO acceptance. Historical unrated uses remain unrated.

### Notes

Do not manufacture ratings for past uses. PO rating remains optional rather than expected when the PO did not consume the evidence.

---

## INTEL-003 — Evidence-Efficient Repository Navigation and Cross-Agent Method Evaluation

### Status

Awaiting Reconnaissance — deferred; no tool-selection policy, benchmark outcome, or new navigation architecture is approved by this entry.

### Summary

Determine the most accurate and context-efficient way for AI Coders to investigate managed repositories, independent of model/provider and without assuming Repo Control's structural graph is the preferred solution. The original rationale for Repository Intelligence was to avoid expensive broad source-file retrieval into model context, not to maximize graph-query counts. Establish evidence-based guidance so different Coders use effective tools consistently without ceremonial scans, unnecessary graph queries, or repeated Product Owner reminders.

Compare targeted textual/file search (including `rg` and bounded source windows); native agent/IDE and language-server symbol navigation; syntax/AST search; indexed or embedding-based semantic code search where actually available; Repo Control's current bounded graph for callers, imports, dependencies, impact, and tests; and useful hybrid sequences. Check actual availability and limitations in Codex/Copilot rather than assuming tool equivalence.

### Why It Is Parked

Existing implementation and adoption data do not yet demonstrate net savings. Lifecycle graph builds are maintenance receipts, not evidence of Coder use. The M008.002 fixed-reference controlled function-location comparison returned 310 `o200k_base` evidence tokens for source-first versus 670 for Intelligence-first (116.1% *more* evidence); both arms took two interactions and read one source window. This limited mechanical test does not measure whole agent sessions or complex impact investigations and cannot establish overall graph ROI. M009.009 recorded a materially used graph lookup; M009.010 classified Intelligence as NOT USED for a focused task. Current repositories may not yet supply enough complex structural questions. Product Owner wishes to preserve the capability while assessing alternative navigation strategies. Detailed governing-document review and future Photo Organizer onboarding take precedence; no graph replacement or new engine is justified yet.

### Trigger for Reconsideration

Promote when the Product Owner chooses an evidence-efficiency evaluation; when multiple nontrivial cross-module investigations are available; when Photo Organizer is appropriately onboarded and explicitly authorized as an additional benchmark subject; or when normal-use records show persistent missed opportunities or inadequate value. Do not manufacture tool use to satisfy an adoption metric.

### Likely Scope

- Inventory each actual Coder/provider's available navigation tools and evidence boundaries (Codex, Copilot, Repo Control), including tool discovery and coverage limitations.
- Define representative, correctly answerable questions across exact-symbol lookup, unknown-functionality discovery, callers/references, dependency and change impact, affected tests, and dynamic/runtime behavior. Stratify by investigation complexity and repository size; verify a ground-truth answer independently.
- Compare competent *source-first*, native-agent navigation, graph-first, and selected hybrid approaches under identical repository snapshots, task questions, correctness/completeness requirements, and stopping criteria. Include failures, follow-up source verification, and repeated tool outputs actually delivered to model context; never compare a large unbounded source dump to a tiny graph response as the only baseline.
- Reuse existing M008.002 isolated, fixed-reference `o200k_base` benchmark tooling. Report per-arm delivered evidence tokens/bytes, number of calls and source windows, elapsed tool time, correctness, missed relationships, and provenance. Treat evidence tokens separately from full model prompt/completion tokens, tool framing, cached/hidden context, and billing; report missing measurements as unknown rather than estimating them as exact.
- First run low-cost offline/mechanical comparisons, then consider only a small controlled number of comparable fresh AI Coder sessions when assessing genuine model-token or human-time savings. Record the evaluation's own token/time cost separately; do not run an A/B experiment in every routine milestone.
- Evaluate graph build/refresh/maintenance overhead, freshness failures and coverage gaps, not just response size. Distinguish repository file scanning performed locally by search/index programs from source text actually returned to an LLM.
- Propose an adaptive, progressive navigation standard: choose the least costly suitable method, return bounded locations/snippets/relations, expand only if needed, and verify authoritative source and tests. Determine when a graph-first query is justified versus when simple source search or native agent navigation wins.
- Evaluate whether measured guidance can be distributed via canonical workflow/Agent Skills to reduce provider-specific variability. Future policy must be Product Owner-approved, auditable where possible, and must not grant new Git/lifecycle authority or force ceremonial graph usage.
- Decide from evidence whether to retain the graph broadly, restrict it to proven specialized use cases, alter automatic build cadence, improve selected operations, or eventually retire/replace parts. No decision is predetermined.

### Dependencies

M004.002 accepted graph scope and limitations; M008.002 versioned benchmark/token measurement contract and negative single-task result; M009.009 freshness/provenance and usage-classification baseline; M009.010 operational compatibility safeguards; existing INTEL-001 consumer-rating idea (separate and not mandatory). Any Photo Organizer evaluation requires separate managed-repository onboarding and explicit authorization; do not switch repository selection or access/modify another worktree from this entry.

### Notes

Success is correct, complete repository understanding with lower *total model-context consumption* and reasonable time/maintenance cost—not the number of graph queries, graph builds, or subjective favorable ratings. Preserve graph-use telemetry, benchmark methodology/version, negative results, and honest unknowns. This parked item is a decision/research candidate, not implementation authority.

---

## DOC-001 — Controlled Governing Document Maintenance

### Status

Deferred

### Summary

Provide a lightweight controlled path for Product Owners and Architects to propose or maintain small factual/planning updates to the five canonical governing documents.

### Why It Is Parked

Without a reviewable maintenance path, governing documents may remain stale when updating them requires a full implementation milestone. Material policy, architecture, authority, or scope changes still require milestone-level review.

### Trigger for Reconsideration

Revisit when recurring small factual/status updates are delayed or handled inconsistently because the existing maintenance path is too cumbersome.

### Likely Scope

```text
controlled edits/proposals for project_context.md
project_architecture.md, project_workflow.md, coding_agent_rules.md,
and canonical_parking_lot.md
review, provenance, authority boundaries, and appropriate validation
separation of lightweight maintenance from material changes
```

### Dependencies

Requires a design preserving PO review, source provenance, no-Coder-Git-mutation rules, and milestone requirements for consequential changes.

### Notes

This is future capability only; it is not part of the current governing-document maintenance scope.

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
