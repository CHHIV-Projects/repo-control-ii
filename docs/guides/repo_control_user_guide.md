# Repo Control User Guide

This guide is for Product Owners and operators using the Repo Control browser
and the normal Architect/Coder workflow. Repo Control supplies deterministic
repository evidence and explicit workflow controls; AI advice does not replace
the authoritative repository, Git, or milestone records.

## Quick start

1. Open the Repo Control URL provided by your operator.
2. Check the **Active Repository** name and path in the header before acting.
   If it is wrong, choose a configured repository and select **Use repository**.
3. Use **Milestone** for prompt selection and workflow, **Git Review** for Git
   inspection/actions, **Architect Dispatches** for pending proposals, and
   **Intelligence Value** for repository-intelligence status and feedback.
4. At the start of every fresh or resumed Coder session, retrieve current
   `coder-context` and verify repository, selected milestone, lifecycle,
   authoritative prompt, applicable approved dispatches, and authorization.
   Stop if the browser, Architect, and Coder disagree about any of these.

## Core concepts

| Term | Meaning |
|---|---|
| **Repository** | A configured Git worktree that Repo Control is allowed to serve. The browser can select only configured repositories, not arbitrary paths. |
| **Milestone** | A bounded unit of approved work represented by a prompt, a workflow sidecar, and usually a closeout. |
| **Active milestone** | The prompt explicitly selected for the active repository. Selection does not approve or start the work. |
| **Governing documents** | The current project context, architecture, workflow, Coder rules, and canonical parking lot. These describe accepted present-day truth and authority. |
| **Product Owner (PO)** | Owns product decisions, consequential workflow approvals, milestone acceptance, and every Git mutation. Repo Control records reported role attribution; it does not authenticate a human identity. |
| **Architect** | Plans work, advises on scope and design, answers questions, and reviews evidence. Architect proposals are not authoritative until the PO approves them. |
| **Coder** | Implements approved scope, validates it, records meaningful questions/evidence, and submits a closeout. The Coder does not mutate Git. |
| **Architect Dispatch** | A typed proposal from the Architect: a **PROMPT**, **INSTRUCTION**, or **RESPONSE**. It begins pending and is scoped to its target milestone. |
| **Workflow sidecar** | The tracked `*_workflow.md` file beside a milestone prompt; it records lifecycle events and durable decisions. It is not the prompt or closeout. |
| **Closeout** | The Coder's report of completed scope, evidence, deviations, limitations, and Git status. It is submitted for review; its presence alone does not accept the work. |
| **Repository Intelligence** | Optional, bounded static structural evidence about code. It is not runtime truth, project authority, or proof that an absent relationship does not exist. |
| **Skill** | A reusable procedure for an agent. Skills do not grant authority or replace the prompt, governing documents, or Repo Control state. |

## Milestone lifecycle

| State | What it means | PO action / expectation |
|---|---|---|
| `PLANNING` | Work is being planned; Coder implementation is not authorized. | Review the prompt and any applicable approved decisions. |
| `READY` | The prompt is approved for handoff, but implementation has not started. | Start explicitly when ready. |
| `IN_PROGRESS` | Authorized implementation or validation is active. | Monitor meaningful evidence and answer escalations. |
| `WAITING` | Work is paused for a recorded question, blocker, or decision. | Review the reason and resolve it before work resumes. |
| `REVIEW` | Closeout and evidence are submitted for review. | Review the result; accept, reject, or request a bounded correction. |
| `CLOSED` | The PO accepted and closed the milestone. This state is terminal. | Git completion or deferral remains a separate PO decision. |

The normal implementation loop is distinct from waiting, correction, and
re-scoping:

- **Normal implementation:** after `IN_PROGRESS` authorization, the Coder
  works only within the prompt and applicable approved lock-ins.
- **WAITING/question:** the Coder stops when blocked. The PO records the
  appropriate resolution and explicitly resumes; a chat answer alone does not
  resume a paused milestone.
- **Bounded correction:** from `REVIEW`, the PO may authorize a narrow,
  reviewable correction. The Coder changes only that boundary and resubmits the
  closeout.
- **Material re-scope:** a changed objective, architecture, risk, authority, or
  materially different validation need requires an explicit re-scope or new
  milestone. Do not hide it in a correction.

## Run a milestone

1. On **Milestone**, confirm the repository and configured milestone root.
   Select the intended discovered prompt; Repo Control does not guess a
   milestone from filenames or Git history.
2. Read the prompt and relevant governing documents. Confirm scope, exclusions,
   acceptance evidence, and any open conditions.
3. From `PLANNING`, the PO uses **Approve & Start** when the work is approved.
   Repo Control records the prompt-approval and implementation-start events
   distinctly and moves to `IN_PROGRESS`.
4. At every fresh or resumed session, the Coder retrieves current authoritative
   context rather than relying on previous chat history or searching documents
   to reconstruct state. Confirm repository, selected milestone, phase,
   prompt, approved applicable dispatches, and authorization. If any identity
   or status cannot be resolved, stop and contact the operator. If there is a
   blocking question, review its recorded reason and use
   **Resolve & Resume** only after the decision is clear.
5. In `REVIEW`, inspect the closeout, changed files, tests/evidence, questions,
   deviations, and known limitations. Use **Request bounded correction** only
   for a small correction inside the approved objective.
6. Use **Accept & Close** only after review is complete. Decide separately
   whether to complete Git actions or defer them.

The available action and prerequisites are shown in the Milestone page. Do not
try to bypass a missing action; check the lifecycle, open conditions, repository
identity, and prompt selection first.

## Meaningful workflow events

Use the Milestone page's **Record a meaningful workflow event** controls to
preserve decisions and evidence that affect the work. Record the smallest
accurate summary; routine conversation does not need an event.

| Control | What it records / when to use it | When not to use it | Effect |
|---|---|---|---|
| **Record a question** | A specific clarification needed to continue or make a decision. Add a waiting category and reason when work must pause. | Routine discussion, or a material blocker better raised as an escalation. | Evidence only unless a waiting reason is supplied; then moves `IN_PROGRESS` to `WAITING`. |
| **Record a non-binding decision** | A useful decision or direction that should be preserved but is not a binding scope or authority lock-in. | When the PO intends the decision to bind; use the lock-in control instead. | Evidence only; does not change lifecycle. |
| **Approve a binding lock-in (Product Owner)** | The PO explicitly approves a decision that must bind the milestone's work. | Ordinary discussion, advice, or a decision not approved by the PO. | Records a binding PO lock-in; does not change lifecycle. |
| **Open an escalation and wait** | A material blocker, risk, or conflict that requires work to stop for PO attention. Include the reason. | A routine question that can be answered without pausing. | Records the escalation and moves `IN_PROGRESS` to `WAITING`. |
| **Record bug discovered** | An observed defect that should be tracked for classification. | A suspected issue without evidence, or as a substitute for recording a fix. | Discovery evidence only; does not classify or fix the bug or change lifecycle. |
| **Record test result** | The evidence source and outcome of a test actually run, including failures. | A test that was not run, or as a substitute for PO acceptance. | Reported evidence only; does not verify the run, accept work, or change lifecycle. |
| **Record Git disposition (no Git action)** | The PO's choice to complete Git work later or defer it, recorded for the milestone. | As a command to stage, commit, push, or otherwise change Git. | Records disposition only; performs no Git action and does not change lifecycle. |

A **question** may be non-blocking or may pause work when its waiting reason
is recorded. An **escalation** is specifically for a matter that needs PO
attention before work continues and always enters `WAITING`. A **non-binding
decision** preserves guidance; a **binding lock-in** is explicitly approved by
the PO and carries binding authority. A **bug-discovered** event records
discovery; fixing it is a separate action available only for a classified,
in-scope bug. A **test-result** event records reported evidence; acceptance is
a separate PO decision. A **Git disposition** records a choice; it never
performs a Git mutation.

Answer, classify, and fix controls are context-specific: they appear only when
an eligible related question or bug exists. Answer an open question, classify
an unclassified discovered bug, and record a fix only for an eligible
classified in-scope bug. Do not create events just to capture routine
conversation.

## Architect Dispatches

Open **Architect Dispatches** to review each proposal's complete content,
target milestone, and status.

- **PENDING** proposals are not Coder instructions.
- A **PROMPT** approved with **Approve & Save Prompt** materializes the
  canonical prompt; that approval does not select or start the milestone.
- Approved **INSTRUCTION** and **RESPONSE** proposals apply only to their
  approved milestone and remain below the prompt, governing rules, and PO
  authority.
- Approval does not mean the Coder has read or acted on the proposal. After an
  approved instruction or correction, explicitly confirm it appears in current
  `coder-context`.

There is a known instruction-visibility workflow gap: a previously approved
instruction was not immediately surfaced by the Coder until it was explicitly
asked to check. See the related item in the
[canonical parking lot](../context/canonical_parking_lot.md).

## Review Git state and actions

Use **Dashboard** for a status overview. **Git Review → Changes** shows
working-tree and index states and file diffs; selecting files is read-only.
**History** inspects local commits and file history. **Branches** shows local
branches, branch-vs-`main` relationships, configured upstreams, and locally
known ahead/behind counts. These upstream counts use local remote-tracking refs
and may be stale until Fetch.

Every offered Git mutation has a separate review/confirmation step and
revalidates relevant state before execution. If the state changes or a selected
item is unsupported, the action may be disabled or refused.

Distinguish:

- **Untracked:** not yet tracked by Git.
- **Unstaged:** working-tree changes not in the index.
- **Staged:** changes already selected in the index for the next commit.
- **Staged and unstaged:** both a staged version and additional working-tree
  changes exist; review both.

### Changes and review

| Control | What happens |
|---|---|
| **Stage selected** | Adds only selected supported paths to the index. Review the exact paths and preview; staging a tracked path uses its current worktree content. |
| **Unstage selected** | Removes only selected supported changes from the index; worktree content is not replaced. |
| **Restore unstaged** | Discards selected unstaged worktree content and restores from the index. |
| **Restore selected paths to HEAD** | Replaces selected staged and unstaged content with the version at `HEAD`; this can discard both layers of work. |
| **Review commit of staged files** | Commits the complete staged set only. Inspect the full staged diff and file list; other unstaged or untracked changes are not implicitly staged. |

Stage, unstage, restore, and commit actions each require review/confirmation
and revalidate current state before acting. Restore actions are destructive:
check every selected path and the preview. A batch action is available only
when every selected path supports it; conflicts, unsupported states, incomplete
preview evidence, or changed state can disable or refuse an action.

### Local branches and history

Use **Branches** to inspect local branch tips, their relationship to local
`main`, upstreams, and ahead/behind state. Use **History** for read-only
inspection of recent commits, commit diffs, and file history. Neither view
fetches automatically.

| Control | Supported behavior and safety condition |
|---|---|
| **Create local branch** | Creates a validated new branch from current `HEAD`; it does not switch to it or configure an upstream. Review and confirm. |
| **Review switch** | Switches to an existing local branch only when the current branch is attached, the worktree/index is completely clean, and no in-progress Git operation prevents it. Review and confirm; no auto-stash or discard occurs. |
| **Review local branch deletion** | Available only for an eligible non-current, non-`main` branch proven fully merged into local `main`. Uses normal safe-delete behavior. This deletes the local branch only; it does not delete its remote branch. |

### Remote operations

Remote controls require review/confirmation. Repo Control prefers an eligible
`origin`; without an `origin`, a sole configured remote can be selected by
default. When multiple remotes require a choice, select the intended one
explicitly. Re-check branch, upstream, ahead/behind, and working-tree state
after remote operations; local tracking information may still be stale or an
operation may report incomplete verification.

| Control | What happens and when it is available |
|---|---|
| **Fetch** | Contacts the selected configured remote and updates local remote-tracking refs. It does **not** move the current branch, `HEAD`, index, or worktree, and does not merge, rebase, or prune. Fetch before relying on possibly stale upstream counts. |
| **Push** | Publishes committed history from the current branch to its configured upstream; it is non-force. Uncommitted changes remain local. It is unavailable without a usable upstream or when there is nothing to push. |
| **First push / set upstream (Publish)** | For a current branch without an upstream, publishes the same-name branch to the selected configured remote and sets that remote branch as its upstream. The same-name remote branch must be absent; only committed history is pushed. |
| **Fast-forward update** | Advances the current local branch to its known upstream tip without creating a merge commit and without fetching. It is available only for a clean, behind-only branch with a usable upstream; dirty, ahead, diverged, or missing-upstream cases are refused. Fetch and inspect first if the tracking ref may be stale. |

Repo Control does not support merge or conflict resolution, rebase,
cherry-pick, reset, stash, force push or force-with-lease, remote branch
deletion, tags, arbitrary refs/refspecs, general pull, history rewriting,
GitHub PR/CI control, or remote-configuration editing. These remain outside
the browser workflow. Manual Git is a controlled PO/operator escape hatch for
such cases, not Coder permission: the PO or authorized repository operator
must verify the repository, exact operation, and current state. Never ask the
Coder to stage, commit, push, or otherwise mutate Git.

## Repository Intelligence

On **Intelligence Value**, check the selected repository and graph state:

| State | Meaning |
|---|---|
| `MISSING` | No usable graph has been built. |
| `BUILDING` | A deliberate build is in progress. |
| `CURRENT` | The graph matches the recorded source identity and options. |
| `STALE` | Source identity or options changed since the build. |
| `FAILED` | A build or graph check failed; review the explicit error. |
| `INCOMPATIBLE` | The stored graph does not match the supported schema/producer. |

Only `CURRENT` evidence is served; queries do not refresh it. The PO explicitly
approves **Build / Refresh Graph** when structural evidence is useful. Static
relationships can help target source inspection but do not establish runtime
behavior, and missing graph edges do not prove that behavior is absent.

The consuming Coder should rate each materially relied-on use and add a concise
rationale where useful. Ratings are evidence of the consumer's experience;
the PO is not expected to judge technical usefulness for evidence they did not
consume. Do not invent ratings for older unrated uses.

## Governing documents and Skills

The five canonical documents serve different purposes:

- [Project context](../context/project_context.md): current product goals and
  project baseline.
- [Project architecture](../context/project_architecture.md): system design,
  integrations, and architectural invariants.
- [Project workflow](../context/project_workflow.md): roles, lifecycle, review,
  and Git authority.
- [Coding Agent Rules](../context/coding_agent_rules.md): always-loaded Coder
  authority and safe-start requirements.
- [Canonical parking lot](../context/canonical_parking_lot.md): unresolved
  future/deferred work, not current implementation scope.

Current approved documents and Repo Control state govern. Milestone prompts
and closeouts are authoritative for their own work or history, not a reason to
override current accepted rules. The three accepted repository-local Skills
are [implementation](../../.agents/skills/repo-control-implementation/SKILL.md),
[blocker/bug escalation](../../.agents/skills/repo-control-blocker-bug-escalation/SKILL.md),
and [validation/closeout](../../.agents/skills/repo-control-validation-closeout/SKILL.md).
Explicitly load the relevant Skill; it supplies procedure, never permission.

## Common recovery

| Situation | What to do |
|---|---|
| Wrong repository selected | Stop before acting. Select the intended configured repository and re-check its displayed name/path. If the browser, Architect, or Coder still disagree, contact the operator. |
| Dashboard **Refresh Status** returns HTTP 500 / `unexpected_error` | Do not infer a Git or lifecycle change from the failed refresh. Verify the repository on **Git Review** and **Milestone**, preserve the error details, and report it; this known UI defect candidate is tracked in the parking lot. |
| Intelligence is stale, missing, failed, or incompatible | Do not use it as current evidence. Ask the PO to review/approve a refresh when useful, or proceed with targeted source evidence if the task allows it. |
| Coder asks a blocking question / workflow is `WAITING` | Read the recorded question and reason. Resolve the decision through the PO workflow and resume explicitly. Do not ask the Coder to continue while waiting. |
| Closeout needs a small correction | From `REVIEW`, authorize only the bounded correction. If it changes scope or direction materially, plan a separate milestone. |
| Unexpected dirty file appears | Identify the exact path and owner. Do not discard, stage, or commit it. Stop if it may be unrelated to the active milestone. |
| Browser action is unavailable | Check repository/milestone identity, lifecycle prerequisites, selection, and safety checks. Use manual Git only for supported-by-policy corner cases and only under PO/operator control. |
| Fresh/resumed Coder session shows old or conflicting context | Stop implementation. Retrieve current `coder-context`/`coder-status` and compare repository identity, selected milestone, lifecycle, prompt, approved applicable dispatches, and authorization against the browser. Do not rely on old chat history or broadly search documents to guess the answer. If identity or authorization remains unresolved, report the failure and contact the operator. |
| Architect and Coder disagree | Stop implementation and compare current `coder-context`, the prompt, approved dispatches, and Repo Control status. Escalate unresolved authority conflicts; do not rely on chat memory. |
