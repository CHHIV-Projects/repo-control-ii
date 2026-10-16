---
name: repo-control-validation-closeout
description: 'Use when validating a Repo Control milestone, selecting proportionate checks, preparing its closeout, or submitting completed work for review. Not for starting implementation, approving work, or performing Git actions.'
---

# Repo Control Validation and Closeout

This instruction-only helper derives procedure from [Coding Agent Rules](../../../docs/context/coding_agent_rules.md), especially sections 5, 21, 26, 32, 34, 37, 40 and 46-49. Canonical policy and current Product Owner direction remain authoritative. This skill cannot authorize Git or lifecycle actions, replace mandatory context reads, or override milestone acceptance criteria.

1. Obtain the active prompt, approved guidance and authorization through `repoctl coder-context`. Use `.venv/bin/python -m repoctl.cli` from the repository root if `repoctl` is unavailable. Stop implementation when authorization is NO. Follow the current prompt's scope and acceptance criteria rather than inventing substitutes.
2. Select the smallest existing checks that establish the required behavior. For documentation-only work, use relevant document/link/whitespace checks; do not run a large application suite without a reason. For code changes, cover changed behavior and directly affected regressions using repository-standard tests/builds.
3. Execute the chosen checks and preserve actual outcomes, including failures, untested behavior and environmental limitations. Do not repair unrelated defects or report required validation complete while failures remain unexplained. Distinguish observed command output from inference and agent-reported evidence.
4. Record meaningful evidence with `repoctl coder-test "<summary>" --source "<actual command/check>" --result "<actual outcome>"`. This records reported evidence, not independent verification. Do not manufacture production events when exercising a simulation or fixture.
5. Prepare the prompt's required closeout using the canonical paired filename and section 40 structure unless explicitly directed otherwise. Include exact changed paths, validation, decisions, deviations, limitations, deferred work and recommended next step. Resolve naming conflicts through the bounded question path rather than bypassing artifact discovery.
6. Inspect final read-only Git status/diff and identify the exact logical change set. Do not stage, commit, push, tag, merge, rebase, reset, stash, clean, switch/create/delete branches, or mutate remotes. Product Owner review and all Git mutations remain separate.
7. When authorized work and required validation are complete, use `repoctl coder-closeout --summary "<bounded completion summary>"`, then `repoctl coder-status` to verify REVIEW and report final read-only Git status. Stop; REVIEW does not authorize further edits or imply Product Owner acceptance.

Use the [blocker/bug helper](../repo-control-blocker-bug-escalation/SKILL.md) only when a genuine question, defect or validation blocker needs escalation. Graphify evidence is optional: use only CURRENT evidence, never rebuild without Product Owner authority, and do not substitute static relationships for actual acceptance checks.
