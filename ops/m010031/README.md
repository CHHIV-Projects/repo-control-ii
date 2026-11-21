# M010.031 registered-repository parity

This bundle adds no repository and grants no worktree-wide write access. It
materializes the six exact write surfaces already reviewed for the three
entries in `ops/repositories.toml`: each repository's resolved Git metadata
directory and its managed R8 milestone-selection directory. The service stays
bound to the existing managed R8 database, loopback listener, backup publisher,
and monitor.

The repeated write pause had one authority-boundary cause at two mutable
levels: recovery coverage compared both milestone selection and repository
request context as though each were the stable whole-database authority.
M010.031 keeps both objects as strict, checksummed audit evidence but binds
write eligibility only to the stable authority configuration and database
identity. Freshness, record checksum, exact recovery manifest, database,
generation, configuration, registry and state-root changes still fail closed.
Repository authorization remains a separate exact trusted-registry check.

The correction was reproduced before editing with one real verified recovery
record: repository one completed a selection and workflow write, while
repositories two and three failed with the live binding error. The resulting
regression performs milestone selection, prompt confirmation and a workflow
write across all three repositories without another backup or restart. It also
proves that health, monitor and write eligibility report the same fresh/stale
result, that an unregistered target is rejected, and that wrong or tampered
stable recovery evidence remains rejected.

`/healthz` now reports `managed_write_coverage` separately from the immutable
runtime binding. `ready` means the same local recovery evaluator used by writes
accepts the current whole-database authority. `blocked` preserves read-only
service readiness while making write ineligibility and its reason explicit.
This bounded correction has not been deployed live.

## Completed parity-configuration handoff

The Product Owner must explicitly authorize the live change before running it.
The install changes one versioned systemd drop-in and adds `chuck:rwx` only to
the three milestone-selection directories. It performs one service restart when
the installed configuration or ACLs differ. Expected browser interruption is
the duration of that restart and the bounded readiness check (normally a few
seconds, at most 60 seconds).

```bash
sudo /home/chuck/projects/repo-control-ii/ops/m010031/install-registered-repository-parity.sh install
```

Expected final output includes:

```text
READY sqlite_managed generation=63137936ea4a4f7db30c6b6b2ea4c59b database=5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17 selection=<id>@<revision>
PARITY VERIFIED: exact three-repository surfaces; managed R8 authority and recovery timers remain active
```

Configuration-only undo restores the captured prior drop-in state and the
three prior ACLs, then performs one restart:

```bash
sudo /home/chuck/projects/repo-control-ii/ops/m010031/install-registered-repository-parity.sh undo
```

For a newly approved registry entry, resolve its metadata with
`git -C <reviewed-root> rev-parse --absolute-git-dir`, derive the managed
selection directory from the trusted repository ID, review both canonical
paths, and add only those two paths to a new versioned drop-in. Add a named
service-account ACL only to that selection directory. This is a one-time
registration operation; repository switching must never rewrite service
configuration or broaden the worktree surface.

## Recovery-authority correction rollout

The parity drop-in and selection ACLs described above are already installed
and match the versioned files. The running service predates the final
recovery-authority correction, so this rollout changes no systemd
configuration, ACL, database, recovery record, registry, or selection. It
loads only these four corrected source files from the existing worktree:

- `src/repoctl/backup_guard.py` — bind recovery eligibility to the stable
  managed authority and database identity while retaining strict checksummed
  repository and selection audit data;
- `src/repoctl/information_store.py` — use one recovery evaluator for writes
  and explicit health status;
- `src/repoctl/workflow/information_store_fixture.py` — require an exact
  trusted-registry match for request-scoped repository clients and expose the
  shared coverage status; and
- `src/repoctl/web/app.py` — publish `managed_write_coverage` from that same
  evaluator on `/healthz`.

Before any restart, the corrected code was evaluated read-only against the
current verified R8 recovery record. It accepted all three registered
repository request contexts with one unchanged record:

```text
record_sha256=4f82ed444c5305e75558183abd6ba0477eb9686fcc03e9e52fdb0b6cd28c99d2
verified_at_utc=2026-11-20T17:30:03.217276+00:00
status=ready write_eligible=true
generation=63137936ea4a4f7db30c6b6b2ea4c59b
database=5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17
```

Do not run the following until the Product Owner gives the exact rollout
approval. The code is already present, so deployment is exactly one restart:

```bash
/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python \
  /home/chuck/projects/repo-control-ii/ops/m010031/recovery-authority-code-toggle.py verify
sudo systemctl restart repo-control.service
```

Expected interruption is one normal service restart, usually a few seconds.
Afterward, `systemctl is-active repo-control.service` must report `active` and
`/healthz` must retain `sqlite_managed` with the exact generation/database
above and add:

```json
"managed_write_coverage": {
  "required": true,
  "status": "ready",
  "write_eligible": true,
  "verified_at_utc": "2026-11-20T17:30:03.217276+00:00",
  "record_sha256": "4f82ed444c5305e75558183abd6ba0477eb9686fcc03e9e52fdb0b6cd28c99d2"
}
```

The post-restart acceptance sequence is: verify backup and monitor coverage;
select Repo Control II, Vocab App, and Photo Organizer through the ordinary
browser path and compare each result with the ordinary CLI; restore M010.031
under Product Owner control; verify the normal approval UI; and perform one
exact eligible accepted-closeout export. Any failed authority, recovery,
write-protection, or required operator check stops the sequence.

To undo only this correction, run the hash-guarded rollback as `chuck` (not
with `sudo`) and restart once. It restores the exact four pre-correction source
hashes and does not touch SQLite, the recovery record, registry, selection,
ACLs, or systemd configuration:

```bash
/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python \
  /home/chuck/projects/repo-control-ii/ops/m010031/recovery-authority-code-toggle.py rollback
sudo systemctl restart repo-control.service
```

The same helper's `reapply` action restores the exact corrected source hashes
if a later authorized retry is needed.

## Historical imported-selection status correction

The live three-repository acceptance exposed a separate historical import
condition: Vocab M002.006 and Photo Organizer M001.004 contain Product Owner
approval events, but those old events do not carry the event-time prompt hash
needed to bind the decision to the imported revision. No approval or accepted
closeout link may be inferred from that history.

`src/repoctl/workflow/coder_interface.py` therefore permits only the ordinary
read-only `coder-status` command to report an unambiguous selected CLOSED
milestone as `UNVERIFIED_PROMPT`. It explicitly denies authorization.
`coder-context` continues to fail closed, active imported milestones still
require exact Product Owner confirmation, and historical closeouts without an
exact acceptance link remain ineligible for export. This change is loaded by
each fresh CLI process and requires no service restart, database migration, or
configuration change.

The code-only rollback is hash guarded and does not touch SQLite or the running
service:

```bash
/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python \
  /home/chuck/projects/repo-control-ii/ops/m010031/historical-status-code-rollback.py verify

# Only if rollback is explicitly authorized:
/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python \
  /home/chuck/projects/repo-control-ii/ops/m010031/historical-status-code-rollback.py rollback
```

## Production-shaped browser proof

`production-shaped-three-repository-fixture.sh start` creates three disposable
repositories and runs one browser under `ProtectSystem=strict`,
`ProtectHome=read-only`, and only the same per-repository Git/selection write
surfaces. The Product Owner selects and performs the displayed Stage/Commit in
each repository. `verify` then checks all three selection pointers, target
commits, and clean indexes read-only; `stop` stops only the disposable service.
No real project Git state is changed by this fixture.

If setup stops before readiness, a repeated `start` preserves the incomplete
tree beside the fixture root with a UTC `.failed-<timestamp>` suffix and starts
cleanly. A completed fixture is never replaced automatically.

The fixture requires its service-owned state lock directory to be private
(`chuck:chuck 0700`), matching the application lock contract. `start` verifies
the actual milestone page before marking setup complete. `repair-lock` repairs
only that exact directory on an already-running disposable fixture and verifies
the page without a restart.

The browser preflight uses one real HTTP cookie jar across live port 8765 and
fixture port 8877, submits the fixture form retained before visiting the live
service, and exercises all three milestone selections through the sandboxed
service. This catches host-scoped session-cookie collisions that isolated test
clients cannot. `refresh-service` reloads current code into an existing
disposable unit and reruns that preflight without recreating repositories or
performing Git actions.
