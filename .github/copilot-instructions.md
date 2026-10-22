# Repo Control Coder Bootstrap

<!-- This template is rendered into a managed repository's host entrypoint. -->

This file is generated from Repo Control shared procedure release
`1.1.0`. Follow current Repo Control state and the active prompt; this
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
`docs/context/coding_agent_rules.md`. It may add project/runtime/data/test constraints;
it cannot override Product Owner authority, Repo Control lifecycle or
authorization, approved-dispatch scope, generic stop/escalation rules, or the
no-Git-mutation boundary.

The concise generic operating process is materialized at
`.repo-control/workflow_core.md`. It is distinct from the project's local
`project_workflow.md`, which describes project-specific workflow and
implementation context.

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
