# Runtime Compatibility and Procedure Release Operations

This guide covers Repo Control II's runtime capability handshake and the
bounded shared-procedure release-preparation workflow. It does not authorize
service operations, Git operations, procedure Apply/Recover, or work outside an
approved milestone.

## Workflow event compatibility

The browser service reports its effective process capabilities through
`GET /healthz` and Architect `repo_status`. The response includes a
process-scoped `instance_id`, a capability-contract version, workflow ledger
and event schema versions, supported event types, and the context-verification
record schema version. The identity changes when the app process is recreated;
it is not a host identity or an authenticated claim.

The local Coder CLI reports its producer contract and schema versions through
`coder-status` / `coder-context`. Before writing the newer
`context_verification_recorded` event, `coder-verify` compares its registered
requirements with the running service's response. It records the service
instance and producer/reader schema details in the event. An unsupported event
contract fails before the workflow sidecar is replaced. No event is discarded,
rewritten, or coerced. Existing version-1 event shapes remain readable and
unchanged.

Configure a non-default loopback service port with
`REPO_CONTROL_RUNTIME_URL=http://127.0.0.1:<port>` or
`coder-verify --runtime-url http://127.0.0.1:<port>`. The URL must be plain
HTTP on `127.0.0.1` or `localhost`; credentials, paths, query strings, and
remote hosts are rejected. The request has a bounded response and short
timeout.

`runtime_upgrade_required` means a service responded but lacks a required
capability. `runtime_unavailable` means no usable process answered.
`runtime_capability_unknown` means the response did not establish the contract.
In each case, no context-verification event was written. Read-only
`coder-status`, `coder-context`, Git inspection, and the workflow reader remain
available when their own local inputs are valid. Do not repair a malformed
sidecar by deleting or rewriting history.

Only event types with a registered `EVENT_CAPABILITY_REQUIREMENTS` entry use
this new-event gate. When a future writer adds a new event type or payload
schema, it must register its reader requirements; an unregistered gated event
is rejected before network access or sidecar mutation. Existing question,
bug, test-result, and closeout event shapes remain the established version-1
contract; their schema has not changed here.

### Coder command behavior

- `coder-context` and `coder-status` are read-only and report local schemas and
  any active procedure-release scope.
- `coder-verify` is the only existing Coder writer changed to require the
  runtime handshake, because it writes the newer verification record schema.
  An idempotent retry of the latest identical verification event does not
  append and does not need another handshake.
- `coder-question`, `coder-test`, and `coder-closeout` retain their existing
  event contracts. Closeout is refused while an explicitly authorized shared
  procedure release remains in preparation.
- Product Owner workflow transitions continue to use their existing lifecycle
  checks. The Coder compatibility check grants no lifecycle authority.

## Authorized shared-procedure release preparation

A release can be prepared only when the active, Product Owner-approved
milestone prompt contains exactly one explicit marker, the milestone is live
and authorized `IN_PROGRESS`, the base manifest still matches the approved
digest, and every changed canonical source is on the prompt's allowlist.
Version 1 authorizes source changes only. Its bounded JSON marker is:

```markdown
<!-- repoctl-procedure-release-preparation:v1
{
  "schema_version": 1,
  "base_manifest_sha256": "<64 lowercase hex characters>",
  "candidate_release": "1.4.0",
  "authorized_sources": ["workflow_core.md"]
}
-->
```

Paths are relative to `shared/procedure`. Candidate versions must be a higher
numeric `major.minor.patch` version. The manifest digest binds this task to the
published base; when that manifest changes, the old marker no longer authorizes
publication.

Version 2 additionally authorizes one fixed artifact-map addition, and no
other manifest changes:

```markdown
<!-- repoctl-procedure-release-preparation:v2
{
  "schema_version": 2,
  "base_manifest_sha256": "<64 lowercase hex characters>",
  "candidate_release": "1.4.0",
  "authorized_sources": [
    "bootstrap.md",
    "skills/repo-control-blocker-bug-escalation/SKILL.md",
    "skills/repo-control-implementation/SKILL.md",
    "skills/repo-control-validation-closeout/SKILL.md",
    "workflow_core.md"
  ],
  "authorized_artifact_additions": [
    {
      "source": "bootstrap.md",
      "target": "AGENTS.md",
      "renderer": "bootstrap"
    }
  ],
  "repository_key": "<active managed repository key>",
  "repository_id": "<active managed repository ID>",
  "repository_root": "<absolute active managed repository path>",
  "milestone_id": "<active milestone ID>"
}
-->
```

The v2 marker is valid only for the exact published five-artifact 1.3.0
mapping, a numerically higher candidate, and the bound active repository and
milestone. `authorized_sources` still controls which canonical source bytes
may change; it must include `bootstrap.md` for the fixed Codex artifact
addition. V1 markers retain source-only meaning and can never add artifacts.
Unknown marker fields, alternate targets/renderers/sources, identity
mismatches, stale manifest digests, and unauthorized source changes fail
closed.

1. Before editing canonical sources, generate a marker from a valid current
   bundle, for example
   `repoctl coder-procedure-release-marker --candidate-release 1.4.0 --source workflow_core.md`.
   Repeat `--source` for every approved canonical path. The CLI supplies the
   exact base manifest digest and rejects unknown paths or a non-increasing
   version. Insert the rendered marker verbatim into the Product Owner-approved
   prompt; do not hand-copy or invent a digest.
   To authorize the fixed Codex artifact addition, run the same command with
   every approved source and `--add-codex-entrypoint`:

   ```bash
   repoctl coder-procedure-release-marker \
     --candidate-release 1.4.0 \
     --source bootstrap.md \
     --source workflow_core.md \
     --source skills/repo-control-blocker-bug-escalation/SKILL.md \
     --source skills/repo-control-implementation/SKILL.md \
     --source skills/repo-control-validation-closeout/SKILL.md \
     --add-codex-entrypoint
   ```

   The command uses the selected active repository and milestone identity and
   emits the current live manifest digest. It does not edit the prompt or
   authorize release work. The Product Owner must review the exact output and
   incorporate it into the controlled, approved milestone prompt. If the
   repository, milestone, manifest, candidate version, or source allowlist
   changes, regenerate and reapprove the marker.
2. Verify active repository, milestone, lifecycle, authorization, marker, and
   release status with `coder-context` / `coder-status`. A
   `RELEASE_PREPARATION` verification result confirms only the bounded
   release-preparation scope and live authority; it is not ordinary instruction
   verification or permission for generic milestone work. Edit only the
   allowlisted canonical sources. Do not treat generated instructions as
   authority.
3. Run `coder-procedure-release-preview`. It recomputes every source hash,
   constructs the candidate manifest (including `AGENTS.md` only when
   explicitly authorized by v2), validates the complete candidate bundle
   using the ordinary strict loader, and creates a read-only per-repository
   reconciliation preview of every generated artifact. Confirm the preview
   contains only the authorized changes. No managed repository files are
   changed.
4. Only after a successful candidate validation and preview, run
   `coder-procedure-release-publish`. It rechecks active authorization, the
   bound repository/milestone identity where applicable, original manifest
   digest, source allowlist, exact artifact addition, complete bundle
   integrity, and candidate manifest immediately before atomically replacing
   the manifest. Publication changes only the canonical manifest. It does not
   Apply generated files, switch repositories, restart services, or perform
   Git operations. Managed-repository rollout remains a separate,
   individually confirmed Product Owner Apply.
5. Confirm `coder-procedure-status` reports a manifest-matching canonical
   release. Any managed-repository Apply or Recover remains a separate
   Product Owner-controlled operation.

`INTEGRITY_UNAVAILABLE` means the marker is absent/invalid for the observed
integrity failure, the base manifest changed, a source changed outside the
allowlist, or the candidate bundle is incomplete/corrupt. Stop release actions
and preserve the evidence; there is no hash-mismatch bypass. An installed
repository reporting `STALE_VERSION` is ordinary release drift and is distinct
from canonical bundle integrity failure.

While canonical integrity is unavailable, Procedure Updates retains the
configured repository list and displays the actual error, but comparisons are
unavailable and Apply is blocked. Independently verified transaction recovery
remains available when its journal, recovery images, targets, and explicit
confirmation pass the established safety checks; recovery does not use or
publish the canonical bundle. Existing journals and recovery images remain
visible where safely readable; do not conceal, delete, or reconstruct them.

During authorized canonical release preparation, Apply remains blocked, but
safe transaction recovery remains available for the same independent reason.
Recovery restores only the verified original target set and does not authorize
generic work or Apply.

## Product Owner-controlled runtime reconciliation

The Coder never restarts, reloads, or manages the service. After reviewing an
accepted code update, the Product Owner controls deployment and runtime
reconciliation:

1. Review the approved source diff, focused/full relevant tests, final
   read-only Git status, and any authorized canonical release candidate.
   Confirm the active service's current `/healthz` instance and capabilities.
2. Deliberately deploy/restart using the Product Owner's normal service
   procedure. Do not start the service from unreviewed or unfinished source.
3. Re-read `/healthz` and confirm the process instance changed as expected and
   advertises the required schema/event capabilities.
4. Verify the previously active milestone is readable through both browser
   workflow views and Architect `repo_status` / `repo_workflow`. Existing
   events remain in place and must round-trip without modification.
5. Only then authorize new event writes or further lifecycle transitions.

No background auto-upgrade or restart is performed. A failed runtime check is
an explicit stop for Product Owner inspection; it is not permission to change
repository selection, install a release, or alter workflow history.
