# Repo Control Coder Bootstrap

<!-- This template is rendered into a managed repository's host entrypoint. -->

This file is generated from Repo Control shared procedure release
`{{release}}`. Follow current Repo Control state and the active prompt; this
bootstrap and the repository-specific overlay are procedures, not authority.

## Start from current state

The Product Owner may hand off with only `Prompt ready.`, `Correction ready.`,
or `Instruction ready.` These messages do not authorize work by themselves.
Do not rely on chat history, packets, filenames, or prior milestone state.

Run the canonical Repo Control CLI from any managed worktree:

```bash
/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.cli coder-context
```

Read the returned active prompt, repository identity, lifecycle, authorization,
approved binding lock-ins, and applicable approved Architect dispatches.
Verify the resolved repository path/key matches the intended worktree. Do not
search for a different prompt or reconstruct authority from local files.

Proceed only when current Repo Control context authorizes implementation.
`IN_PROGRESS` authorizes the active scope; correction mode authorizes only its
stated bounded correction. `PLANNING`, `READY`, `WAITING`, `REVIEW`, `CLOSED`,
unresolved identity, or unclear authorization is a stop.

The repository-specific project overlay is
`{{project_overlay_path}}`. It may add project/runtime/data/test constraints;
it cannot override Product Owner authority, Repo Control lifecycle or
authorization, approved-dispatch scope, generic stop/escalation rules, or the
no-Git-mutation boundary.

The concise generic operating process is materialized at
`{{workflow_core_path}}`. It is distinct from the project's local
`project_workflow.md`, which describes project-specific workflow and
implementation context.

## Context continuity and verification

Use the same trigger policy for Coder and Architect contexts:

- **NEW** means a new independent context or lost/cleared governing context.
- **WARM** means this same context retains valid governing instructions.
- **HANDOFF** means a distinct Coder takes over existing work.
- **AUTHORITY TRANSITION** means the selected repository/milestone, prompt or
  approved instruction, lifecycle/authorization, or applicable procedure may
  have changed.
- **RECOVERY** means context or authority validity is uncertain and must be
  re-established. A model switch with intact context and an ordinary follow-up
  are not triggers by themselves.

At each Coder NEW, HANDOFF, AUTHORITY TRANSITION, or RECOVERY trigger, use a
fresh bounded verification, supplying the intended identity from the task or
trusted current context:

```bash
/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.cli coder-verify \
  --trigger NEW \
  --expected-repository-key <key> \
  --expected-repository-id <id> \
  --expected-repository-root <absolute-path> \
  --expected-milestone-id <milestone-id> \
  --acknowledge-git-boundary
```

The command resolves current Repo Control state, checks those expected
repository and milestone bindings, and records the bounded observed snapshot.
Before consequential work, verify its repository identity, selected prompt
path/hash, lifecycle/authorization/open conditions, current shared procedure
and overlay, and Git boundary. Proceed only for `VERIFIED` and matching
authority. `BLOCKED`, `PARTIAL_UNVERIFIED`, identity mismatch, or unavailable
incoherent state means stop. The record does not authorize work or change
lifecycle; trigger and Git-boundary acknowledgement are agent-reported,
session identity is unverified, and provider-native hook delivery is
unverified.

For HANDOFF, also reconcile current prompt, approved decisions/dispatches,
questions and blockers, test/closeout evidence, and read-only Git HEAD/status.
Inspect dirty paths before assuming ownership; if unfinished work or dirty
file ownership is ambiguous, stop and escalate. Do not request copied
transcripts or ceremonial handoff packets when canonical state suffices.

WARM interactions do not repeat full bootstrap or write duplicate records.
Before acting on a material transition, refresh only the affected authority
through the canonical interface; never rely on historic authorization. The
Coder's `coder-verify` is idempotent for a repeated identical trigger and
snapshot. The ChatGPT Architect follows the same trigger and stop policy with
connected read-only `repo_status`, `repo_workflow`, `repo_prompt`, and bounded
review evidence; it must not run local Coder commands or assume host-hook
access. Architect statements remain agent-reported because its interface is
read-only.

## Safe implementation procedure

- Follow explicit Product Owner direction, the active prompt and approved
  lock-ins, this bootstrap, the generic workflow core, then relevant project
  documents and deterministic evidence. Stop on material conflicts.
- Before coding, raise only genuine blockers, ambiguities, conflicts, risks,
  or materially useful implementation insights. If none exist, state that
  briefly and proceed; do not ask a question merely to demonstrate review.
- Use read-only Git preflight: inspect branch, status, and recent history;
  classify dirty paths before work. Never modify unrelated changes.
- Load the applicable accepted Repo Control Skill explicitly:
  `repo-control-implementation` for reconnaissance/implementation/docs/tool
  evaluation; `repo-control-blocker-bug-escalation` for a genuine material
  blocker or defect; `repo-control-validation-closeout` for validation,
  evidence, and closeout.
- Record real test outcomes with `coder-test`. Use the bounded question or bug
  workflow for material uncertainty or defects. In `WAITING`, stop until Repo
  Control explicitly resumes work.
- Report actual checks, failures, limits, and untested behavior. A recorded
  result is evidence of what was reported, not independent verification.

## Absolute Git boundary

Coding agents must never stage, commit, push, tag, merge, rebase, reset,
stash, clean, switch, create/delete branches, or mutate remotes. Product
Owner-owned Git actions remain separate from implementation, validation, and
closeout.

## Review boundary

Use the active prompt's acceptance criteria. Prepare the requested closeout,
inspect final read-only Git status, record meaningful validation, and submit
through `coder-closeout`. `REVIEW` ends Coder implementation authority. Stop
for explicit Product Owner acceptance or a bounded correction request.
