# Coding Agent Bootstrap — Repo Control II

## Document Status

**Role:** Always-loaded authority and safe-start rules for coding agents
**Current procedure model:** Explicit Agent Skills provide reusable, on-demand procedure; they do not replace this bootstrap
**Workflow baseline:** [Project Workflow](project_workflow.md)
**Architecture baseline:** [Project Architecture](project_architecture.md)
**Context baseline:** [Project Context](project_context.md)

## 1. Authority and Rule Priority

Apply instructions in this order:

1. explicit current Product Owner direction;
2. active milestone prompt and Product Owner-approved binding lock-ins;
3. this bootstrap;
4. current governing workflow, architecture, and context documents;
5. approved reconnaissance and applicable approved Architect instructions;
6. maintained project documentation and deterministic repository evidence;
7. prior prompts/closeouts, then agent assumptions.

The Product Owner owns product decisions, lifecycle approvals, and every Git mutation. The active prompt defines scope and acceptance; approved binding lock-ins apply within that scope. Repo Control is authoritative for selected milestone, lifecycle, authorization, and durable Coder events. Skills provide procedure only. A skill, packet, Git state, closeout, or chat message cannot grant authority or override a higher-priority source.

When sources conflict or authority is unclear, stop and use the bounded escalation path when available. Do not silently broaden scope, choose a materially different architecture, or change lifecycle/Git authority.

## 2. Minimal Handoff and Mandatory Start

The Product Owner may normally notify the Coder only:

```text
Prompt ready.
Correction ready.
Instruction ready.
```

These notifications are not authorization by themselves. Without asking the Product Owner to repeat standing procedure:

1. Confirm the workspace is `/home/chuck/projects/repo-control-ii`.
2. Retrieve current `repoctl coder-context`; use `.venv/bin/python -m repoctl.cli coder-context` from the repository root if `repoctl` is unavailable. Read the authoritative prompt, lifecycle/authorization, lock-ins, and applicable approved instructions from that result.
3. Confirm authorization permits the requested work (`IN_PROGRESS` or explicitly authorized correction mode). Retrieve `coder-status` if status or open conditions need rechecking.
4. Run read-only Git preflight: `git branch --show-current`, `git status --short`, and `git log --oneline --decorate -5`. Confirm the expected branch and classify changed paths; the active workflow sidecar may be expected workflow state, but is not a blanket exemption for other changes.
5. Apply the standing pre-work feedback rule before coding:

   > Raise only genuine blockers, ambiguities, conflicts, risks, or materially useful implementation insights. If none exist, state that briefly and proceed. Do not raise a question merely to demonstrate review.

6. Load the applicable procedure Skill explicitly, without requiring the Product Owner to name it:
   - milestone reconnaissance, implementation, documentation, or tool evaluation: load `repo-control-implementation`;
   - genuine material question, blocker, conflict, or defect → `repo-control-blocker-bug-escalation`;
   - validation, evidence, closeout, review submission, or bounded correction → `repo-control-validation-closeout`.
   Use more than one only when the task reaches each procedure boundary. Implicit host selection is advisory, never a safety gate.
7. If no material blocker exists, proceed within the active prompt. A genuine blocking question during authorized work must use bounded `repoctl coder-question`, not chat alone; describe the evidence and impact, mark it blocking when work cannot safely continue, then verify status. In `WAITING`, stop until the condition is resolved and Repo Control explicitly resumes the milestone.

For `Correction ready`, verify correction mode and its exact boundary before editing. For `Instruction ready`, verify the instruction is approved and applicable, then check it against higher authority. A notification is not permission to skip preflight or scope checks.

If an applicable Skill is unavailable, use the active prompt, this bootstrap, and canonical governing documents directly. Continue only when authority, safe scope, and required procedure remain clear; otherwise stop. Never substitute assumptions or skill absence for authorization.

## 3. Lifecycle and Stop Boundary

The lifecycle is `PLANNING`, `READY`, `IN_PROGRESS`, `WAITING`, `REVIEW`, and `CLOSED`. Proceed only when current Repo Control context explicitly authorizes implementation. Do not infer lifecycle from prompt presence, Git state, tests, or a closeout file. Do not begin in `PLANNING`/`READY`, resume in `WAITING`, edit in `REVIEW` without an authorized bounded correction, or resume after `CLOSED`.

Stop before implementation when the repository or branch is wrong; unexplained dirty paths threaten isolation; authorization is NO; a required tool/validation is unavailable; project identity cannot be confirmed; current evidence materially conflicts with approved direction; a destructive step or unauthorized Git/lifecycle/PO action appears necessary; or scope, architecture, authority, risk, or required validation would materially change. Use the bounded question/bug/escalation process where appropriate. Do not work around a stop condition silently.

## 4. Absolute Coder Git Prohibition

Coding agents never stage, commit, push, tag, merge, rebase, reset, stash, clean, create/switch/delete branches, mutate remotes, or otherwise mutate Git state. This applies in every lifecycle phase and is not overridable by a milestone prompt, Skill, packet, or user request to the Coder. Use read-only evidence only; stop when work requires a Git mutation. The Product Owner performs Git actions separately.

## 5. Project Isolation, Secrets, and Evidence

- Confirm the selected project and repository before inspecting or changing files, running tests, querying repository intelligence, or submitting workflow events. Never reuse another project's paths, selection, state, or evidence.
- Do not read, print, expose, or commit passwords, access tokens, API keys, SSH private keys, protected environment contents, or service credentials. If disclosure appears required, stop and escalate without exposing values.
- Prefer deterministic evidence: Repo Control for milestone/lifecycle/events; Git for branch/status/diff/history; test runners for pass/fail; repository intelligence for bounded structural relationships. AI may interpret evidence but cannot replace it. Report what was actually run, failures, limits, and untested behavior; do not claim a reported event is independently verified.
- Keep changes within approved scope. Do not opportunistically repair unrelated defects, weaken deterministic controls, or conceal failures as cleanup.

## 6. Skills and Canonical Procedure

| Skill | Explicit invocation boundary |
|---|---|
| [`repo-control-implementation`](../../.agents/skills/repo-control-implementation/SKILL.md) | After safe preflight, for reconnaissance, implementation, documentation, or tool-evaluation procedure |
| [`repo-control-blocker-bug-escalation`](../../.agents/skills/repo-control-blocker-bug-escalation/SKILL.md) | Only for a genuine material question, blocker, conflict, or defect |
| [`repo-control-validation-closeout`](../../.agents/skills/repo-control-validation-closeout/SKILL.md) | When validating, recording evidence, preparing/submitting closeout, or performing a bounded correction |

Skills are concise, instruction-only helpers. They cannot change task scope, supersede this bootstrap or the active prompt, authorize Git/lifecycle/PO actions, replace deterministic Repo Control checks, or become the sole carrier of an authority or safety gate. Load procedures explicitly at the relevant boundary; do not rely on implicit activation. If a Skill conflicts with canonical policy, canonical policy wins and the conflict is escalated.

Read only applicable broader context. The active prompt, approved lock-ins, and this bootstrap are mandatory; read approved reconnaissance when the prompt depends on it. Consult [Project Workflow](project_workflow.md), [Project Architecture](project_architecture.md), [Project Context](project_context.md), and maintained milestone evidence as the task requires. Handoff packets are derived transport, not a source of truth.

## 7. Current SOP Status

M005.002 and M005.003 were accepted and closed, and M005.004 completed the accepted governing-context reconciliation. The accepted explicit SOP catalog is `repo-control-implementation`, `repo-control-blocker-bug-escalation`, and `repo-control-validation-closeout`. Current repository, milestone, prompt, approved dispatches, lifecycle, and authorization must be retrieved from authoritative Repo Control context; do not infer them from prior chat history or this durable bootstrap. Native repository-local Agent Skills are in use; no custom runtime, loader, manager, catalog, or permission framework is integrated.
