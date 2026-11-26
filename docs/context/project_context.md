# Repo Control II project context

**Baseline:** As of accepted M011.004 and the M011.005 documentation reconciliation. M011.004 is CLOSED for a functional MVP architecture evaluation. Consult live Repo Control for current selection, authorization and operating status.

[Architecture and tradeoffs](project_architecture.md) · [Operating workflow](project_workflow.md) · [Decision and deficiency register](canonical_parking_lot.md) · [Coder overlay](coding_agent_rules.md)

## What Repo Control II is for

Repo Control II is a single-user control plane for a hobby Linux server. It gives the Product Owner a browser for routine Git review and explicit actions, a way to give an Architect and Coder the right repository and milestone, and an inspectable record of approvals, tests and acceptance. The Product Owner chooses outcomes and owns consequential actions. The Architect plans and reviews against bounded evidence. The Coder works locally under approved scope. Git owns source, history and index state.

The operating model uses registered local repositories on `henderson-server1` and Windows browser access over SSH. Value comes from fewer manual transfers, less repeated context, correct repository selection and durable recovery evidence. Compare these benefits with actual operator time, repeated validation, backup load and maintenance. Repo Control integrates Git, agents and specialized tools. Multi-tenant hosting, a general-purpose agent framework and model hosting are outside the product baseline. [Runtime and security](project_architecture.md#runtime-and-security)

## Five functional pillars

**Implemented** refers to source behavior. **Exercised** identifies fixture, native-host or live evidence. **Deployed/verified** applies to the installed service at the cited observation. **Deferred/unverified** marks outstanding obligations.

| Pillar | Evidence supporting the M011.004 evaluation | Forward installation gap |
|---|---|---|
| Registered repository selection and inspection | M010.031 exercised live browser selection in Repo Control II, Vocab App and Photo Organizer. M011.004 exercised repository/diagnostic isolation in fixtures. The old service rejected a nonselected Photo Organizer read with HTTP 409. | Paired live evidence under the two correct selections on the forward release. |
| Prompt, decision, lifecycle and closeout | M010.028 exercised the live R8 store and canary. M011.004 fixture regressions verified exact approved prompts, lifecycle and handoff behavior and imported-CLOSED non-authorization (`3a50c42f985e4bfaa4cc00c1babb6b80`). | Live acceptance of changed browser, API and MCP projections against real Photo Organizer migrated state. |
| Current and historical documents and authority | The old live service returned 200 for exact M011.002 Git-worktree and SQLite revisions and 409 on an ambiguous path. M011.004 exercised exact public selectors, scoped warnings and selected-CLOSED archive opt-in in fixtures. | The old service returned 400 on the new opt-in. Photo Organizer M001.003 paired reads and M001.004 current/history semantics await correct-selection verification. |
| Native Codex and Copilot correct-context onboarding | Both native SessionStart paths passed on Repo Control II with Python 3.12.16, SQLite 3.51.3 and the exact approved prompt (`3a4952d15368491cae7407b2bb012b8d`). The PO reviewed the exact Codex hook. Repo Control II procedure 1.5.0 reported CURRENT. | Auxiliary preview remains deferred. Photo Organizer procedure artifacts, canonical paths and cold-session evidence require separate verification. |
| Managed SQLite integrity and recoverability | Manifest, receipt and payload checks, an operator-reported isolated NAS restore and an isolated frozen forward/rollback rehearsal support recovery. | Scheduled freshness, production source isolation and live application rollback remain unverified. The isolated restore proves recovery for the tested point. |

Evidence register and limitations appear in the [accepted M011.004 closeout](../milestones/M011.004_cross_repository_authority_historical_evidence_and_onboarding_reliability_reconciliation_closeout.md). The [operator preflight](../milestones/M011.004_operator_acceptance_preflight.md) preserves earlier snapshots, diagnosis, installation planning and the intended smoke matrix. Its lifecycle and pre-rehearsal status statements describe their recorded times.

## What the Product Owner accepted

Response `93b26316de3a1f9dfa0f91f8baeb8c84` scoped M011.004 to the **functional MVP architecture evaluation**. Acceptance `8ea28f8b43514c5595b0dcb965250a1d` and closure `04cd1a43a8644c50ab4183046dcec44a` bind the closeout SHA-256 `4a18146689bde138901a68a2c3462d63f56cb81c29da3e46ec0165327ba44098`. The five pillars support an architecture decision. **G5 production isolation/readiness and G6 installed-service acceptance remain deferred/unverified. Production cutover of the forward release remains unauthorized.**

## How the product reached this baseline

| Period | What to carry forward |
|---|---|
| R002 and M003 | Loopback browser Git operations, distinct remote/local facts, six-state milestone workflow, Architect read/proposal transport and bounded local Coder commands made the manual workflow inspectable. M003 used Markdown prompts/closeouts, workflow sidecars and external selection/dispatch files. Its live polling/form coverage was limited. |
| M004–M009 | Bounded Graphify, three explicit procedure Skills, registered context and historical reads, provider onboarding, append-only reopening, late-guidance guards, dispatch durability and exact-image procedure recovery. M009 added auditable context verification, semantic graph freshness and scoped runtime/release safeguards. [Earlier accepted capabilities](project_architecture.md#earlier-accepted-capabilities) |
| M010 | R8 replaced file-first authority for managed workflow with SQLite, exact prompt approval, versioned documents, transaction and recovery guards. Git still owns source and reviewed exports. Legacy artifacts remain available as history or in an explicit legacy authority mode. R8 retains a managed selection compatibility file and associated operational constraints. [Persistence and cutover](project_architecture.md#persistence-and-cutover) |
| M011.001–M011.004 | Bounded the procedure executor, audited store/runtime/history seams, exercised production-shaped fixtures, repaired native startup and reconciled provenance and exact read contracts. [Document and history contracts](project_architecture.md#document-and-history-contracts) |

## Operating baseline and costs

- The last inspected service used qualified Python 3.12.16 / SQLite 3.51.3 and imported from the editable checkout. Its in-memory source revision is unproven. Scheduled backup, monitor and on-demand procedure units also resolved to editable code. SQLite 3.45.1 is unsupported for managed authority. [Runtime and security](project_architecture.md#runtime-and-security)
- The operator rehearsed sealed forward `8b4c4e43cfcc9cb1bb4527e2b9d50ea6b5225430` and rollback `6aae3905e8bf1dd907963bf17e7709245823ffa9` under `/opt/repo-control/releases/<commit>`. The older release reopened the same disposable store. The `db-workflow-persistence-architecture` branch is candidate provenance. Check live Git and running import paths separately. [Recovery and operational assurance](project_architecture.md#recovery-and-operational-assurance)
- Monitoring read about 55 GiB across 493 manifests per cycle in about 4m30. The roughly 52 GiB cgroup peak was largely filesystem cache. Python RSS was about 36 MiB. Independently, systemd intermittently failed before Python with `226/NAMESPACE` when SQLite SHM was absent. Scheduled recovery remains a release gate. [Backup priorities](canonical_parking_lot.md#release-and-recovery)
- Historical event `fda86c229ed84a54bf35d5ebc292b2b0` reports 864 total, 863 passed, one failed, zero errors and two skipped. The sole failure was the PO-deferred `coder-bootstrap-preview` case. The accepted record supplies the counts. Both native SessionStart paths passed independently. [Validation evidence](../milestones/M011.004_cross_repository_authority_historical_evidence_and_onboarding_reliability_reconciliation_closeout.md#5-validation-performed)
- Photo Organizer requires live correct-selection checks on Repo Control II and independent procedure acceptance. Its outdated path/governance text has a separate Photo Organizer approval boundary. [Photo Organizer follow-up](canonical_parking_lot.md#ops-004--photo-organizer-governance-and-install-evidence)

## The next decision belongs to the Product Owner

Repeated defect loops and governance and backup complexity have consumed disproportionate effort for a local tool. Stop open-ended stabilization. Reuse prior evidence and request new proof only when it changes the decision or satisfies a separately approved release gate.

The options remain **retain and repair**, **simplify/refactor**, **rebuild**, and **defer**. Preserve correct repository isolation, explicit PO approval/Git ownership, truthful current/history provenance and recoverable state. Compare total effort, including migration, rollback and operator burden. Count manual interactions per handoff, operator steps per release/recovery, acceptable recovery freshness and resource cost, and the validation needed for the highest-risk authority boundaries.

The remaining unknowns are the cost of fixing scheduled launch within the existing sandbox, the value of full-catalog hashing at current scale, the smallest reliable packaging boundary, the work of transferring exact approvals/history to a replacement, and the benefit of quicker daily handoffs. Use the [alternatives and evaluation criteria](project_architecture.md#architecture-decision-baseline) and [PO decision gate](canonical_parking_lot.md#arch-001--choose-the-next-architecture-direction) before defining any new scope.

## Start here

1. Confirm live identity, selected repository/milestone and authority using the [workflow startup](project_workflow.md#cold-start-handoff-and-warm-continuation). Repo Control II's registered root is `/home/chuck/projects/repo-control-ii`.
2. Read [M010.005 InformationStore](../milestones/M010.005_m010_005_central_sqlite_information_store_foundation_and_contract_tests_closeout.md), [M010.028 R8 cutover](../milestones/M010.028_live_sqlite_cutover_and_canary_acceptance_closeout.md) and [M010.031 parity](../milestones/M010.031_multi_repository_workflow_parity_and_milestone_ui_finish_closeout.md) for persistence and operating boundaries.
3. Read the [M011.004 accepted closeout](../milestones/M011.004_cross_repository_authority_historical_evidence_and_onboarding_reliability_reconciliation_closeout.md) and [preflight ledger](../milestones/M011.004_operator_acceptance_preflight.md). Use [Architecture](project_architecture.md) for tradeoffs, [Workflow](project_workflow.md) for approvals and the [Parking Lot](canonical_parking_lot.md) for the decision and deferred work. A new work scope requires separate PO approval.
