---
name: repo-control-blocker-bug-escalation
description: 'Use when Repo Control work encounters a genuine blocking question, conflicting instructions, failed required validation, or a discovered bug requiring bounded recording and scope classification. Not for routine implementation choices or Product Owner approvals.'
---

# Repo Control Blocker and Bug Escalation

This instruction-only helper derives procedure from the [Coder bootstrap](../../../docs/context/coding_agent_rules.md), [Project Workflow](../../../docs/context/project_workflow.md), and the active prompt. Canonical policy and current Product Owner direction remain authoritative. This skill cannot authorize Git or lifecycle actions, change scope, replace mandatory context reads, or grant Product Owner authority.

1. Read current `repoctl coder-context` / `repoctl coder-status` and relevant evidence. Use `.venv/bin/python -m repoctl.cli` from the repository root if `repoctl` is unavailable. Check scope, approved guidance and authorization; stop implementation when authorization is NO.
2. Distinguish a routine choice from a genuine ambiguity, conflict, required validation failure or blocker. Ask only when the answer materially affects scope, behavior, architecture, authority, risk, implementation direction or required validation. Prefer the simplest supported in-scope choice when no such question exists.
3. For a genuine question, report the observation, evidence, why it matters, smallest options and recommendation via `repoctl coder-question "<summary>"`. Add `--blocking --category blocker` when work cannot safely continue; use an appropriate supported category otherwise. Verify the returned state with `repoctl coder-status`; a blocking question pauses implementation in WAITING. Do not use chat alone as the durable blocker record or create a generic event mechanism.
4. For a discovered defect, record concrete evidence with `repoctl coder-bug "<summary>"`. Identify whether it is a current regression or pre-existing, whether it blocks validation, and whether a fix changes scope/architecture/risk. Reporting discovery does not approve classification or repair. Follow the canonical discovery/classification/fix sequence and Product Owner scope decisions; never self-approve classification through a lower-level event API.
5. Fix only defects clearly within already approved scope and required authorization, preserving required workflow classification. Stop/escalate material or unclear scope changes; do not opportunistically repair unrelated bugs. Record real validation outcomes rather than hiding failures as cleanup.
6. In WAITING, stop implementation. Resume only after the condition/decision is resolved and Product Owner lifecycle authorization is restored; re-read normal Coder context. A chat answer, skill invocation or recorded answer alone is not a lifecycle resume.
7. Never stage, commit, push, tag, merge, rebase, reset, stash, clean, switch/create/delete branches, or mutate remotes. If requested work requires Git mutation or unauthorized lifecycle/PO actions, stop and explain the boundary through the applicable bounded escalation path.

Missing or STALE Graphify evidence is not automatically a blocker: report its status, avoid stale structural claims and use targeted source inspection if sufficient. If CURRENT graph evidence is required to satisfy the prompt, raise the unmet requirement; never refresh without Product Owner authority.

For fixtures, dry runs and non-authoritative simulations, describe expected submissions and stop behavior without issuing production commands or inventing real incidents.
