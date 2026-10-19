# Repo Control II — Project Coder Overlay

<!-- repo-control-project-overlay:v1 -->

This file contains Repo Control II-specific context only. The generated
[host bootstrap](../../.github/copilot-instructions.md) carries the generic
Repo Control Coder procedure; the canonical human-readable sources are under
[`shared/procedure`](../../shared/procedure/). The generated generic workflow
core is at [`.repo-control/workflow_core.md`](../../.repo-control/workflow_core.md).

## Repository-specific rules

- Repo Control II is a Python 3.11+ application packaged from `src/`, with
  Flask as its web dependency and unittest coverage under `tests/`.
- `ops/repositories.toml` is central managed-repository configuration. Do not
  edit it unless an active prompt explicitly authorizes that scope.
- Preserve Repo Control II-specific lifecycle, service, web, and Git Control
  behavior described in the project architecture and workflow. Do not infer
  generic shared procedure from the comprehensive project workflow.

## Project documents

- [Project context](project_context.md) describes current Repo Control II
  state and direction.
- [Project architecture](project_architecture.md) describes system boundaries
  and invariants.
- [Project workflow](project_workflow.md) retains comprehensive Repo Control
  II operating and implementation guidance.
- [Canonical parking lot](canonical_parking_lot.md) contains unresolved
  Repo Control II follow-up work.

This overlay is not an authority source for Product Owner decisions, Repo
Control lifecycle state, authorization, approved-dispatch scope, or Git
mutations. It cannot override the generated generic bootstrap.
