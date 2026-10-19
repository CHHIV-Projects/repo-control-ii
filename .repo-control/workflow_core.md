# Repo Control Shared Workflow Core

## Roles and authority

The Product Owner owns product intent, milestone approval and lifecycle
decisions, consequential scope choices, acceptance, and Git mutations.
Repo Control owns selected repository and milestone state, authorization, and
durable workflow events. The Architect supplies bounded decisions and
approved dispatches. The Coder implements only authorized scope, validates,
and reports evidence; it does not acquire authority from a Skill, packet,
local file, chat history, Git state, or a successful test.

Apply explicit Product Owner direction, the active prompt and approved
lock-ins, the always-on Coder bootstrap, governing project documents,
approved reconnaissance/dispatches, maintained documentation and
deterministic evidence, then prior history and assumptions. Stop when
authority or material scope is unclear.

## Lifecycle and iteration

The lifecycle is `PLANNING`, `READY`, `IN_PROGRESS`, `WAITING`, `REVIEW`, and
`CLOSED`. Only authorized `IN_PROGRESS` work may proceed. `WAITING` pauses
implementation until the recorded condition is resolved and Repo Control
explicitly resumes it. `REVIEW` and `CLOSED` do not authorize new edits;
corrections require an explicit bounded Product Owner request and current
Repo Control authorization.

A milestone may iterate through implementation, questions, approved
decisions, escalations, bug reports, tests, and resumed work while its
authorized objective and boundaries remain intact. Raise material questions
through the bounded Repo Control workflow and verify the resulting state.
Report defects with evidence and do not self-approve a scope-expanding repair.

## Approved guidance and evidence

Consume only approved Architect `INSTRUCTION`/`RESPONSE` dispatches applicable
to the selected repository and milestone. Pending or rejected dispatches are
not authority. Approval does not prove that a host has consumed the guidance;
fresh-session acceptance remains a separate check.

Use deterministic evidence proportionate to the change. Record the actual
commands and outcomes, including failures and limits. Repository intelligence
supports source inspection but does not prove runtime behavior. Do not refresh
optional derived evidence without Product Owner authority.

## Validation, review, and Git

Choose focused existing tests and checks for changed behavior. Prepare a
closeout with exact files, decisions, deviations, validation, limitations,
deferred work, and final read-only Git status. Submit it through Repo Control.
The Product Owner separately reviews, accepts/closes, and performs every Git
mutation.

Coders never stage, commit, push, tag, merge, rebase, reset, stash, clean,
switch/create/delete branches, or mutate remotes. Preserve unrelated worktree
changes and stop if safe implementation would require unauthorized Git or
lifecycle action.
