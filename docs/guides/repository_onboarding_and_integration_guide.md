# Repository Onboarding and Integration Guide

This guide describes two repeatable paths for bringing a repository under Repo
Control: a **new repository** and an **established repository**. Onboarding is
complete only when the browser, Architect, and Coder resolve the same
repository, milestone root, governing documents, and authorization state.

## Roles

- **Product Owner (PO) / service operator:** chooses the repository identity,
  owns the central registry and service configuration, approves lifecycle and
  Git actions, and decides when onboarding is complete.
- **Architect:** defines the product context, architecture, workflow, prompt
  boundaries, and required evidence; reviews whether the repository's existing
  truth has been preserved.
- **Coder:** performs only authorized repository changes, verifies its host and
  local Repo Control context, and reports evidence. It never mutates Git or
  central Repo Control configuration.

The PO may also be the service operator. Keep these responsibilities explicit
even if one person performs several roles.

## A. New repository

Use this path when there is no existing project corpus that must be migrated.

| Step | Owner | Work and completion check |
|---|---|---|
| 1. Establish identity | PO | Choose the repository name/key and absolute worktree path on the Repo Control service host. Confirm who owns the worktree and how it will be maintained. |
| 2. Initialize Git and hosting | PO | Initialize the repository and configure its intended GitHub/remote relationship as appropriate. Verify the expected branch and remote without assuming a default name. |
| 3. Establish noninteractive authentication | PO | Configure the service account's normal SSH/Git authentication and verified host key so service operations do not depend on an interactive login. Do not put credentials in the repository, registry, prompts, or guide. |
| 4. Create canonical structure | PO + Architect | Agree on the active milestone root `docs/milestones/` and governing-document content before product milestones begin. |
| 5. Create governing documents | Architect + Coder; PO approves | Create `docs/context/project_context.md`, `docs/context/project_architecture.md`, `docs/context/project_workflow.md`, `docs/context/coding_agent_rules.md`, and `docs/context/canonical_parking_lot.md`. Keep each role distinct; do not fill them with unverified template claims. |
| 6. Configure central registry | PO / service operator | Add a stable repository key to the service's central registry. Set its absolute worktree path, `milestone_root = "docs/milestones"`, and repository-relative mappings for the five governing-document roles. Follow the deployed service's configuration and restart/reload procedure. |
| 7. Install accepted Skills | Coder; Architect verifies; PO approves | Copy the accepted instruction-only Repo Control Skills under `.agents/skills/`: `repo-control-implementation`, `repo-control-blocker-bug-escalation`, and `repo-control-validation-closeout`. Verify the intended Coder host can discover and explicitly load each applicable Skill. Skills provide procedure, not authority. |
| 8. Verify repository identity end to end | PO + Architect + Coder | In a fresh or resumed Coder session, retrieve current context/status and verify repository path, selected prompt, lifecycle, applicable approved dispatches, and authorization without relying on prior chat history. Also verify that browser and Architect resolve the intended repository key/path and milestone root. |
| 9. Build Repository Intelligence when useful | PO; Coder consumes | When code exists and structural evidence would help, deliberately approve a build/refresh. Confirm `CURRENT` before relying on it. The Coder uses targeted queries and rates material uses; graph evidence is not runtime proof. |
| 10. Verify operations | PO + Architect + Coder | Review a real or isolated onboarding milestone through selection, authorized start, question/waiting behavior if exercised, closeout review, and Git Review. Keep test events in an appropriate test repository/milestone; do not fabricate product history. |
| 11. Start product work | PO + Architect | Begin ordinary product milestones only after identity, documents, Skills, Git authentication, lifecycle, and required intelligence checks pass. |

The service registry is a controlled operator configuration, not a browser
form and not Coder-owned state. The browser may switch among allowlisted
repositories; it cannot submit an arbitrary filesystem path.

### New-repository completion checklist

- [ ] Repository key and absolute path identify the intended worktree.
- [ ] Git remote and noninteractive service-account authentication work.
- [ ] `docs/milestones/` is the configured active prompt root.
- [ ] All five canonical documents exist and their registry mappings are correct.
- [ ] The three accepted Skills are installed and host-visible.
- [ ] Browser, Architect, and Coder agree on repository identity and selection.
- [ ] Fresh and resumed Coder sessions confirm repository, selected milestone,
  lifecycle, prompt, applicable approved dispatches, and authorization using
  current Repo Control context, not chat history or filename inference.
- [ ] Repository Intelligence is `CURRENT` if the onboarding plan relies on it.
- [ ] Git Review and required milestone lifecycle actions operate as expected.
- [ ] The PO explicitly closes onboarding before product implementation begins.

## B. Established repository

Use this path when adopting an existing codebase, documentation set, or
milestone corpus. Reconnaissance comes before normalization or file movement.

| Step | Owner | Work and completion check |
|---|---|---|
| 1. Reconnoiter first | Architect; PO authorizes; Coder supplies evidence | Establish repository purpose, active work, architecture, risks, and intended Repo Control use before editing. Record uncertainties that materially affect authority, history, or migration strategy. |
| 2. Inventory the repository | Coder; Architect reviews | Inventory current context/architecture/workflow/Coder rules; prompts, closeouts, workflow sidecars, and milestone roots; Git status/branches/remotes; source/runtime structure; agent instructions/Skills; and historical references. Do not infer active work from filenames alone. |
| 3. Classify existing material | Architect; PO decides conflicts | Mark each relevant source as current authoritative, stale but useful, duplicate/mislabeled, or historical only. Identify the actual Repo Control selection and lifecycle state. Resolve conflicting authority before migration. |
| 4. Reconstruct canonical context | Architect; Coder writes approved scope | Derive missing context and architecture from current source, runtime, and accepted evidence. Do not copy stale prose as fact. Preserve important limitations and label proposals or future direction clearly. |
| 5. Normalize the five governing documents | Architect + Coder; PO approves | Establish the five canonical roles under `docs/context/`. Reconcile only current authority; do not convert historical milestone narratives into current rules. |
| 6. Preserve milestone history | Coder under authorized scope; Architect reviews | Keep original prompts, closeouts, and workflow sidecars intact where practical. Separate legacy/historical corpus outside recursive active discovery (for example, `docs/history/milestones/`) so it cannot be mistaken for current prompts. Do not rewrite historical evidence just to normalize names. |
| 7. Prepare a milestone-root cutover | Architect plans; PO operates | Prepare and inspect the new-root prompt/closeout/sidecar copies first. Keep old-root recovery copies while the new root is selected and verified. Avoid simultaneously exposing legacy artifacts to active discovery. |
| 8. Install and verify Skills | Coder; Architect verifies | Install the accepted three Skills in `.agents/skills/` and explicitly test host discovery/loading. Remove no legacy instructions until their authority and use have been reconciled. |
| 9. Configure the central registry | PO / service operator | Set the repository key, absolute worktree path, `docs/milestones/` (or an explicitly approved canonical root), and repository-relative mappings for the five documents. The Coder does not edit central Repo Control configuration. |
| 10. Perform the controlled cutover | PO / service operator | Change the registry's active milestone root, reload/restart through the normal operator procedure, then reselect the repository. Verify the browser discovers the intended new-root prompts and no legacy candidates pollute discovery. |
| 11. Verify identity in all clients | PO + Architect + Coder | Confirm browser, Architect, and Coder agree on key, path, prompt/root, governing documents, and lifecycle. In a fresh or resumed Coder session, verify current `coder-context` and `coder-status`, including applicable approved `INSTRUCTION`/`RESPONSE` dispatches and authorization. Packets and prior chat history are fallback transport, not current authority. |
| 12. Verify Git and intelligence | PO + Coder | Verify noninteractive remote operations under the service account. If structural intelligence is required, PO approves a build and the Coder confirms `CURRENT` before use/rating. |
| 13. Exercise lifecycle and review | PO + Architect + Coder | Validate selection, prompt approval/start, waiting/resume, closeout/review, and Git Review at the appropriate risk level. Use a dedicated test milestone/repository for scenarios that would otherwise create misleading product history. |
| 14. Retire recovery copies last | PO; Architect confirms | Only after successful new-root selection and operation may the PO decide how to archive or retire old-root copies. Preserve history and a clear rollback path until then. |
| 15. Close onboarding | PO | Record what was migrated, retained, deferred, and validated. Begin regular product work only after the PO accepts the integration. |

### Established-repository inventory

Before normalizing, inspect:

- existing context, architecture, workflow, coding rules, and parking/backlog;
- milestone prompts, closeouts, workflow sidecars, naming conventions, and
  recursive discovery boundaries;
- current repository selection, lifecycle, branches, remotes, and dirty paths;
- source code, runtime/deployment configuration, external services, and tests;
- existing agent instructions, Skills, host-specific behavior, and direct CLI
  resolution;
- links and historical references that could be broken or misinterpreted by a
  move.

Do not delete or rename records as a cleanup shortcut. Classify first, preserve
evidence, and obtain PO direction for material conflicts or irreversible
cutover decisions.

## Current integration checks

Use these checks as an onboarding sign-off, selecting only what is applicable:

- **Registry:** the key, path, milestone root, and document-role mappings are
  correct and validated by the service.
- **Repository identity:** browser header, Architect repository selection,
  and fresh-session Coder context resolve to the same intended worktree.
- **Milestones:** prompts are discovered only below the configured canonical
  root; selection is explicit; workflow sidecars are beside their prompts.
- **Documents:** all five canonical documents are present, scoped to their
  intended role, and not replaced by stale or historical copies.
- **Skills:** all three accepted procedures load in the actual Coder host;
  they do not grant permission or replace current preflight.
- **Git:** correct remote and branch are visible; fetch/push authentication is
  noninteractive for the service account; routine Git Review actions work with
  explicit review and confirmation.
- **Lifecycle:** PO-owned approvals, waiting/resume, review, and closure are
  exercised or otherwise validated with deterministic evidence.
- **Intelligence:** when used, the graph is `CURRENT`, the consumer can query
  it, and materially relied-on uses are rated by that consumer.
- **Authority:** the Coder can inspect but cannot stage, commit, push, or change
  central registry/service configuration.
- **Fresh/resumed-session convergence:** a new or resumed Coder session resolves the current
  repository, selected milestone, lifecycle, prompt, approved applicable
  dispatches, and authorization without prior chat history; unresolved identity
  fails clearly instead of being reconstructed by broad document searches.
- **Limitations:** unsupported actions, required manual operator steps, and
  unresolved defects are recorded rather than silently worked around.

### Get the maximum Repo Control advantage

- [ ] Central registry entry and absolute repository path are correct.
- [ ] Canonical milestone root is configured and isolated from history.
- [ ] All five governing documents are present and mapped to the right roles.
- [ ] Historical material is preserved but cannot become active authority.
- [ ] Shared Skills are installed and explicitly loadable in the Coder host.
- [ ] Git remote/authentication works noninteractively.
- [ ] Browser repository selection is correct.
- [ ] Architect and Coder resolve the same repository, path, and milestone.
- [ ] Fresh Coder sessions resolve current repository, milestone, phase,
  prompt, approved dispatches, and authorization through Repo Control context.
- [ ] Unresolvable repository or milestone identity produces a clear stop,
  not a guess based on previous chat or document searching.
- [ ] Lifecycle actions work and durable questions/evidence remain reviewable.
- [ ] Applicable Repository Intelligence is `CURRENT` and used selectively.
- [ ] Coder rates materially relied-on intelligence uses.
- [ ] Git Review works for routine changes and staged-set commits.
- [ ] Unsupported Git cases have a deliberate PO/operator path.
- [ ] Approved dispatches and corrections are visible to the Coder.
- [ ] Known limitations are recorded in the canonical parking lot.
- [ ] Coding agents have no Git mutation authority.

## Stop and rollback rules

- Do not remove old milestone-root recovery copies before new-root selection and
  lifecycle operation are verified.
- Do not rewrite historical prompts, closeouts, or workflow evidence merely to
  normalize names or paths.
- Do not let historical artifacts become current governing authority.
- Do not let legacy milestone files pollute recursive active discovery.
- Do not let a Coder change the central Repo Control registry or service
  configuration unless a specifically designed, separately authorized
  capability exists.
- Stop if the browser, Architect, and Coder disagree about repository identity,
  active prompt, milestone root, or lifecycle state.
- Stop when Git authentication, registry validation, or a required host/Skill
  check fails; surface the evidence and restore the last known-good selection
  or configuration under PO/operator control.
- Treat rollback as a controlled PO/operator decision. Preserve source/history
  and do not use destructive Git or filesystem cleanup to make validation pass.

## Known cross-repository integration risks

Direct Coder CLI resolution is not yet guaranteed to follow the same
authoritative managed-repository configuration as the browser and Architect.
The CLI may fall back to repository key `default`, explicit repository or
milestone-root options may be needed, and multiple legacy `repoctl`
installations may resolve to the wrong executable. During onboarding, verify
the executable, repository, root, and selected milestone explicitly; do not
assume shell location or a default key proves correct targeting. This gap is
tracked in the [canonical parking lot](../context/canonical_parking_lot.md).

The **Dashboard → Refresh Status** HTTP 500 / `unexpected_error` observation
from Vocab onboarding is a Repo Control II issue candidate, not a Vocab repair
task. Record the error and use other verified views without assuming a failed
refresh changed Git or workflow state. See the
[canonical parking lot](../context/canonical_parking_lot.md).
