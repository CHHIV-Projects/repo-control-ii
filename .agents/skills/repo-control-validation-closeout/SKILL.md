---
name: repo-control-validation-closeout
description: 'Use when validating a Repo Control milestone, selecting proportionate checks, preparing its closeout, or submitting completed work for review. Not for starting implementation, approving work, or performing Git actions.'
---

# Repo Control Validation and Closeout

This instruction-only helper derives procedure from the [Coder bootstrap](../../../docs/context/coding_agent_rules.md), the active prompt, and [Project Workflow](../../../docs/context/project_workflow.md). Canonical policy and current Product Owner direction remain authoritative. This skill cannot authorize Git or lifecycle actions, replace mandatory context reads, or override milestone acceptance criteria.

1. Obtain the active prompt, approved guidance and authorization through
   `coder-context`, using the qualified Repo Control interpreter:

   ```bash
   /home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python -m repoctl.cli coder-context
   ```

   Use that interpreter for subsequent Repo Control CLI commands. `VERIFIED`
   permits ordinary milestone work within scope. `RELEASE_PREPARATION` permits
   only shared-procedure release operations explicitly authorized by the active
   prompt's valid release marker and live Repo Control result; it is not
   ordinary `VERIFIED` authorization. Stop for `BLOCKED`, `PARTIAL_UNVERIFIED`,
   identity mismatch, or unavailable/incoherent authorization. For a bounded
   correction, confirm explicit correction-mode authorization and address only
   the stated correction. Follow the current prompt's scope and acceptance
   criteria rather than inventing substitutes.
2. At required Coder NEW, HANDOFF, AUTHORITY TRANSITION, and RECOVERY triggers, follow the shared `coder-verify` procedure. Do not create duplicate warm-context records. In review evidence, distinguish observed snapshots from agent trigger/identity claims and provider delivery; missing records do not prove a session did not start.
3. Select the smallest existing checks that establish the required behavior. For documentation-only work, use relevant document/link/whitespace checks; do not run a large application suite without a reason. For code changes, cover changed behavior and directly affected regressions using repository-standard tests/builds.
4. Execute the chosen checks and preserve actual outcomes, including failures, untested behavior and environmental limitations. Do not repair unrelated defects or report required validation complete while failures remain unexplained. Distinguish observed command output from inference and agent-reported evidence.
5. Record meaningful test evidence with
   `/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python -m repoctl.cli coder-test "<summary>" --source "<actual command/check>" --result "<actual outcome>"`.
   Verification events are not test results. This records reported evidence,
   not independent verification. Do not manufacture production events when
   exercising a simulation or fixture.
6. Prepare the prompt's required closeout using the canonical paired filename (`M<arc>.<sequence>[.<subsequence>...]_<slug>_closeout.md`; numeric components are three digits) and this structure unless explicitly directed otherwise:

   ```markdown
   # Milestone <ID> — <Title>
   ## 1. Scope Completed
   ## 2. Resulting Behavior
   ## 3. Files Changed
   ## 4. Architecture / Integration Notes
   ## 5. Validation Performed
   ## 6. Questions / Decisions Incorporated
   ## 7. Deviations from Prompt
   ## 8. Known Limitations
   ## 9. Deferred / Follow-Up Work
   ## 10. Recommended Next Milestone
   ## 11. Git Status
   ```

   For every implementation milestone, include a concise Repository Intelligence disposition in the closeout:

   - `Repository Intelligence: USED — <structural question(s) and evidence materially used>.`
   - `Repository Intelligence: NOT USED — <why it was not materially useful or needed, regardless of graph status>.`
   - `Repository Intelligence: NOT AVAILABLE — <genuinely unsupported/inaccessible capability or evidence and observed cause>; proceeded with <other evidence>.`

   `STALE` alone does not mean `NOT AVAILABLE`. Report graph freshness/status, whether refresh was considered/attempted and its outcome, and the evidence-use classification separately. When USED, distinguish structural evidence from source/runtime proof. If the existing rating workflow is available, the consuming Coder rates materially relied-on uses according to current Repo Control procedure. Do not count refresh receipts as queries or manufacture uses or ratings. This disposition is not mandatory for documentation-only, lifecycle-only, or trivially localized work unless the active prompt requires it.

   Also report whether a graph refresh was considered/attempted, why it was or was not needed, the bounded outcome/limitations, and distinguish refresh receipts from structural query uses.

   Use at most one matching workflow sidecar, named `<same-id>_<slug>_workflow.md`; it is event history, not a prompt or closeout. For a bounded correction, update the existing closeout and rerun relevant validation. Include exact changed paths, validation, decisions, deviations, limitations, deferred work and recommended next step. Resolve naming conflicts through the bounded question path rather than bypassing artifact discovery.
7. Inspect final read-only Git status/diff and identify the exact logical change set. Do not stage, commit, push, tag, merge, rebase, reset, stash, clean, switch/create/delete branches, or mutate remotes. Product Owner review and all Git mutations remain separate.
8. When authorized work and required validation are complete, use the
   qualified Repo Control interpreter. Under file/ledger authority, retain the
   existing closeout command:

   ```bash
   /home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python -m repoctl.cli coder-closeout --summary "<bounded completion summary>"
   ```

   Under InformationStore authority, submit either the exact immutable
   closeout revision and hash:

   ```bash
   /home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python -m repoctl.cli coder-closeout \
     --document-id <document-id> \
     --revision-id <revision-id> \
     --content-sha256 <content-sha256> \
     --summary "<bounded completion summary>"
   ```

   Or explicitly import the selected canonical draft file using its exact
   content hash:

   ```bash
   /home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python -m repoctl.cli coder-closeout \
     --draft-file <selected-canonical-closeout-draft> \
     --content-sha256 <content-sha256> \
     --summary "<bounded completion summary>"
   ```

   Then run `coder-status` to verify REVIEW and report final read-only Git
   status. The final report should identify status, scope/files, tests and
   results, questions/decisions, limitations/deferred work, Git status, and
   recommended next step; point to the closeout instead of repeating it.
   Stop; REVIEW does not authorize further edits or imply Product Owner
   acceptance.

Use the [blocker/bug helper](../repo-control-blocker-bug-escalation/SKILL.md) only when a genuine question, defect or validation blocker needs escalation. Graphify evidence is optional: use only CURRENT evidence, refresh during authorized `IN_PROGRESS` work only through the bounded same-configuration command when materially useful, and do not substitute static relationships for actual acceptance checks.
