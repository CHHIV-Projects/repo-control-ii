---
name: repo-control-implementation
description: 'Use after authoritative preflight for reusable Repo Control milestone procedures including reconnaissance, implementation, documentation, and tool evaluation. Not for authorization, lifecycle decisions, Git actions, blocker escalation, or validation/closeout.'
---

# Repo Control Implementation and Milestone Work

This instruction-only helper derives procedure from [Coding Agent Rules](../../../docs/context/coding_agent_rules.md), [Project Workflow](../../../docs/context/project_workflow.md), [Project Architecture](../../../docs/context/project_architecture.md), and the active milestone prompt. Product Owner direction, approved lock-ins, Repo Control state, and deterministic evidence remain authoritative. This skill cannot authorize scope, lifecycle, Product Owner, or Git actions.

Invoke after `repoctl coder-context` and read-only repository preflight have confirmed the active prompt, mode, repository, branch, and authorization. The prompt owns the current objective and acceptance criteria; select the procedure below from its stated mode, not from assumptions.

## Choose the mode

- **Reconnaissance only:** inspect relevant systems and dependencies; map behavior, authority boundaries, reuse/retirement candidates, options, and validation; recommend one direction and a practical roadmap. Do not edit implementation files or start coding.
- **Implementation after reconnaissance:** read the approved reconnaissance and lock-ins, inspect named files and directly related tests, then expand only when evidence requires it or an assumption fails.
- **Direct low-risk implementation:** use for clearly bounded wording, documentation, styling, focused tests, or an obvious local bug. Inspect the relevant paths, keep the change narrow, and validate it.
- **Validation only:** use the validation/closeout helper. Make no implementation changes or repairs; record actual pass/fail evidence and report defects instead of silently fixing them.
- **Documentation only:** edit only requested documentation; distinguish current behavior from intended direction, preserve history, and do not invent implemented behavior.
- **Bug-fix follow-up:** reproduce when practical, make the smallest repair within documented scope, add focused regression coverage, and record whether previous validation is affected. For a discovered or out-of-scope defect, use the blocker/bug helper; reporting does not authorize classification or repair.
- **Integration/tool evaluation:** define the problem and criteria, test realistic candidates, compare burden, licensing, operations, and security boundaries, then recommend. Do not adopt or deeply integrate a tool unless explicitly in scope.

Do not silently change mode. A milestone may iterate through implementation, question, resolution, validation, and bounded correction; material changes to scope, architecture, authority, risk, persistence, or strategy require explicit re-scope.

## Investigation and implementation discipline

- Read only context likely to affect the decision: the active prompt and approved lock-ins are mandatory; use approved reconnaissance and governing workflow/architecture/context as relevant. Handoff packets are derived transport, not authority; missing packet content does not prove the source is absent.
- Classify unexpected dirty paths as required prior-milestone work, unrelated work, generated/noise, or required current-milestone work. Report each path, its classification and reason, and recommended handling. Never edit, revert, delete, stash, clean, stage, or commit unrelated changes.
- Use only read-only Git inspection (for example `status`, `diff`, `log`, `branch`, `ls-files`, `rev-parse`, `show`, and `tag --list`). Do not update refs/remotes or alter the index; if branch creation/switching is required, stop and report it.
- Stop broad investigation when authority, affected paths, implementation direction, and validation are established. Prefer targeted code, tests, and current deterministic repository intelligence over repeated broad reading.
- For implementation milestones, explicitly decide whether Repository Intelligence may materially help with structural questions such as unfamiliar subsystems, cross-module impact, callers/callees, imports/references, affected tests, or blast radius. Inspect its freshness/status before relying on it. If CURRENT and useful, use targeted queries; source/runtime inspection remains necessary where graph coverage is insufficient.
- If Repository Intelligence is CURRENT but not materially useful, consciously record `NOT USED` and why. If it is MISSING, STALE, FAILED, INCOMPATIBLE, or otherwise unavailable, do not rely on it as current evidence; record `NOT AVAILABLE` and the observed state/reason. Missing graph evidence does not prove that a runtime relationship is absent.
- Before relying on Repository Intelligence, inspect freshness and decide whether structural evidence is materially useful. Product Owner Approve & Start establishes a CURRENT baseline automatically where supported; do not add a ceremonial build.
- During authorized `IN_PROGRESS` work, if source edits made the graph non-CURRENT and refreshed structural evidence would materially help, run the bounded `repoctl coder-intelligence-refresh` command. It takes no repository/provider/path/scope options, resolves the central managed Coder workspace and active milestone, and changes only the existing external derived graph. It grants no Git, lifecycle, repository-selection, provider/configuration, schema, or source-scope authority. A CURRENT graph is a no-op; unsupported scope and failures are explicit.
- If refresh is not materially useful, do not run it; use direct source evidence and record the decision. Refresh is not mandatory ceremony for documentation-only, lifecycle-only, or trivially localized work. Static relationships support but never replace source/runtime verification or acceptance evidence.
- Prefer direct control flow, existing tested logic, small helpers, and narrow changes. Before adding an abstraction, identify the current problem, simpler alternatives, and added maintenance/authority/persistence burden.
- Treat inherited code as evidence, not architecture authority. Classify major legacy components KEEP, REFACTOR, or RETIRE based on value, fit, testability, and replacement cost.
- If repeated patch/retry cycles increase uncertainty, stop, preserve evidence, identify a trusted baseline, classify the affected behavior, and reconstruct only accepted behavior before revalidation.
- Do not integrate major external tools, replace the UI framework, or introduce model routing outside explicit scope. Consult the governing architecture/context for project-specific constraints.

If scope, authority, or safe continuation is unclear, stop and use the blocker/bug helper. When validation or closeout begins, explicitly load the validation/closeout helper. If a helper cannot load, follow the canonical prompt and governing documents directly; do not infer permission from a skill or its absence.
