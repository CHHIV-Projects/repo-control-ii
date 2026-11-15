# Deployment Runtime and Backup Readiness

This guide describes M010.014's proposed deployment artifacts and their
validation boundary. Nothing here installs or enables systemd units, changes
the active service, configures a NAS mount, creates a live database, or
authorizes a cutover.

## Reproducible candidate runtime

The target baseline is the hash-verified CPython 3.12.16 build linked to
SQLite 3.51.3. Source hashes are:

| Artifact | Digest |
|---|---|
| CPython `Python-3.12.16.tar.xz` | `b5796915dc45ff745cfb4e990a7e7c93d6f546328a102ce31448d69ebc37575c` |
| SQLite `sqlite-autoconf-3510300.tar.gz` | `81f5be397049b0cae1b167f2225af7646fc0f82e4a9b3c48c9ea3a533e21d77a` |
| Extracted SQLite `sqlite3.c` (SHA3-256) | `32d5424f97e0a7fc5ed2f6335afbb58be4e0298bd7117a34e39d345ff13d859e` |

`ops/runtime-dependencies.lock` pins every runtime and build-tool wheel by
version and SHA-256 for CPython 3.12 on Linux x86_64. The pip bootstrap is
bundled with the verified Python build: version 25.0.1, wheel SHA-256
`c46efd13b6aa8279f33f2864459c8ce587ea6a1a59ee20de055868d8f7688f7f`.
Create a clean sibling environment and install the application editable with
the locked build tools, without build isolation or network access:

```sh
BASE=/home/chuck/.local/opt/python-3.12.16-sqlite-3.51.3/bin/python3.12
ENV=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3-m010014-clean
WHEELS=/home/chuck/.cache/repoctl-sqlite-3.51.3/wheels

# Use a new sibling environment for each clean reproduction.
test ! -e "$ENV"
"$BASE" -m venv "$ENV"
"$ENV/bin/python" -m pip install --no-index --find-links "$WHEELS" \
  --require-hashes -r ops/runtime-dependencies.lock
"$ENV/bin/python" -m pip install --no-index --no-deps --no-build-isolation -e .
"$ENV/bin/python" -m pip check
"$ENV/bin/python" ops/verify_sqlite_runtime.py
```

The wheelhouse is a local offline input, not a source of implicit build tools;
all runtime and build-tool requirements consumed by pip are pinned in the lock.
The clean environment guard prevents accidental reuse. An isolated wheel build
also succeeded (SHA-256
`55b2552e85bb5837e89f72c870425fa027d7cf505fcc142b2659f91a87872fa9`), but
installing that wheel does not support Coder CLI context: the current package
resolves `ops/repositories.toml` relative to its source tree. The verified
candidate therefore uses the offline editable install, which resolves that
repository data from the checked-out source. Rebuild from the approved source
revision when repeating the install. Do not replace the live `.venv` or change
the service unit.

The app's managed database path requires `_sqlite3`, and the tested candidate
loads SQLite 3.51.3 from the pinned user-local library. The service and adapter
clients use loopback HTTP; no application-owned outbound HTTPS path was found
in the inspected service/CLI code. Offline wheel installation does not require
`_ssl`. `hashlib` remains usable through its built-in fallback. The Graphify
bridge runs in its separately pinned external environment; its `_ctypes` and
libseccomp requirements are not provided by this application venv. The
candidate lacks `_ssl`, `_ctypes`, `_bz2`, `_lzma`, `readline`, `_curses`, and
`_hashlib`; any future TLS-dependent operation or in-process Graphify runtime
requires an appropriately rebuilt runtime. No system packages are installed
by these steps.

## Backup architecture and operator provisioning

The live database must stay on local storage. The values in
`ops/backup.env.example` remain proposals except for the M010.019 Product
Owner's recorded NAS source and destination decision. That decision does not
authorize provisioning or configuration changes:

| Role | Path / identity and status |
|---|---|
| Database | `/home/chuck/.local/share/repoctl-sqlite/repo-control-ii/information-store.sqlite3` (proposed; absent) |
| NAS mount | `/mnt/nas/repo-control` (current CIFS mount) |
| NAS source | `//192.168.1.171/ProjectBackups` (approved by Product Owner in correction event `d0fb8f95a73b4f1e8f262efb2b1a90ea`) |
| NAS destination | `/mnt/nas/repo-control/RepoControl/repo-control-ii` (approved path; absent) |
| Local staging | `/home/chuck/.local/state/repo-control-backup/staging` (proposed; absent) |
| Separate local restore | `/home/chuck/.local/state/repo-control-backup/restore` (proposed; absent) |
| Job identity | `chuck` (`uid=1000` on the observed host) |

M010.014 found no dedicated Repo Control share. M010.019 now records the
Product Owner's approval to use the existing `ProjectBackups` share with the
destination `/mnt/nas/repo-control/RepoControl/repo-control-ii`. Keep
`/mnt/nas/repo-control/RepoControl/M010.015-test` separate; it is not the
production destination. The example file still proposes the old
`//192.168.1.171/RepoControl` source and
`/mnt/nas/repo-control/repo-control-ii` destination, so it must not be treated
as the approved configuration.

The approved destination is absent. Read-only inspection as `chuck` found the
`RepoControl` parent and disposable test subtree presented as mode `0700`,
owned by `chuck:chuck`; the parent is locally writable/searchable, while the
approved destination does not exist. Listing the share root as `chuck` was
denied. The CIFS mount reports `nounix`, `uid=1000`, `gid=1000`,
`dir_mode=0700`, and `file_mode=0600`; these client-side modes do not establish
the NAS server's effective ACL. No production write probe was attempted.
Therefore, the requirement that the backup account cannot write to sibling
project folders remains NOT TESTED. Before any separately approved deployment,
the NAS operator must verify that the effective backup account can write to
the approved production destination but cannot create or write sibling project
folders. Do not change the mount or permissions under this assessment.

Keep any mount credential in a root-owned file with mode `0600`, outside Git
and logs; the service runs as `chuck`. When provisioning is separately
authorized, create staging, restore, and destination directories with
restrictive permissions (`0700` directories, `0600` backup files), verify
service access, and confirm the restore path is local and separate from both
the live DB and NAS mount. Do not grant the backup account access to unrelated
project data.

`repoctl.backup backup` takes a consistent local snapshot through SQLite's
online backup API, verifies the Repo Control schema, selection, workflow
projection/event count, document content hashes, `integrity_check`, and
`foreign_key_check`, transfers to a unique partial NAS filename, compares the
transferred bytes and verified database snapshot, then publishes a manifest.
Only a completed manifest represents a verified copy. It never replaces or
deletes a prior good backup. `repoctl.backup restore` verifies that manifest
and creates a new file under the separate restore directory; it refuses to
overwrite an existing restore path.

The four `ops/systemd/repo-control-backup*` files are review proposals, not
installed units. The backup timer runs at minutes 00, 15, 30, and 45. A
separate monitor timer runs every five minutes and checks the newest
verified-manifest age against 20 minutes, validating the recorded backup hash.
The monitor does not require the NAS mount to start; when the share is
unavailable it reports the mount mismatch. M010.018 selects repeated local
terminal/journal warnings; the monitor does not invoke the optional alert-hook
configuration. These warnings are not unattended notifications and do not
prove that an operator saw them. The monitor proposal remains uninstalled, and
journal retention and inspection must be verified during the operational
rehearsal.

When a 20-minute warning fires, the operator should immediately verify the NAS
mount identity, restore backup job operation, and confirm a new verified
manifest. For managed SQLite writers, the guard automatically refuses new
writes at an inclusive 45-minute recovery-point age; there is no fallback write
to file-backed authority. Keep managed writes paused until a fresh verified
backup publishes valid recovery-point evidence for the same database and
authority. The five-minute monitor cadence and 45-minute guard do not prove
the one-hour loss limit. Restore duration, alert visibility, and actual NAS
behavior must be measured before relying on that objective. The guard remains
disabled in the live file-backed service.

## M010.018 managed-write guard

For an active managed InformationStore only, a successful verified NAS backup
publishes a private local recovery-point record bound to the exact database,
authority configuration, repository and selected milestone. Managed writes
validate that evidence before starting a transaction and again before commit;
missing, malformed, stale, future-dated, or mismatched evidence pauses writes
at an inclusive 45-minute age. The verified backup path refreshes the record.
Reads, backup creation, and restore to a separate disposable path remain
available while managed writes are paused. The guard does not change current
file-backed service behavior and has not been enabled in production. There is
no fallback write to file-backed authority; managed writes resume only after a
valid verified recovery point for the same database and authority is
published.

The monitor's 20-minute warning threshold is independent of the 45-minute
write cutoff. While a check is unhealthy, it prints an unresolved local
warning; after a fresh verified backup, a healthy run reports current
coverage. The implementation uses monitor output rather than a separate
status file, so actual scheduling, journal persistence, warning/recovery
cadence, and operator acknowledgement still require the M010.018 rehearsal.

## Rehearsal boundary

M010.014 tests create only temporary local SQLite stores and directories. They
exercise online snapshot creation, complete transfer comparison, restore to a
new path, identity/selection/workflow/document-hash checks, monitor age and
corruption behavior, mount identity rejection, and injected transfer failure
while proving an earlier good copy remains unchanged. This establishes
application logic on disposable local fixtures only. It does not test a
Synology share, actual network transfer, filesystem ACLs, systemd scheduling,
NAS unavailability, or delivery to an operator.

## M010.014 validation status

| Acceptance area | Result | Evidence boundary |
|---|---|---|
| Clean pinned runtime and isolated app/CLI launch | PASS | Hash-locked offline dependencies, Python 3.12.16 / SQLite 3.51.3 eligibility probe, `pip check`, CLI help, and browser/Coder CLI integration against an injected disposable store. The wheel install did not resolve repository data; the verified runtime uses the editable source install. |
| Disposable backup and restore logic | PASS | Local fixtures verify snapshot/restore, identity and exact document hashes, stale/corrupt copies, mount rejection, failed transfer preservation, and simulated monitor alert dispatch. |
| Actual NAS transfer and restore | NOT TESTED | No dedicated Repo Control share or approved writable NAS test target was mounted. |
| Timer activation and firing | NOT TESTED | Both proposed calendar expressions and units passed read-only syntax verification; no unit was installed or enabled. |
| Failure alert delivery | NOT TESTED | Monitor dispatch was tested with an injected stub only; no approved route or operator delivery was configured. |
| One-hour loss target | NOT TESTED | No real backup cadence, restore duration, alert response, or NAS outage was measured. |

This table records the M010.014 status. M010.018 later completed a 45-minute
disposable CIFS backup/monitor/restore rehearsal using
`/mnt/nas/repo-control/RepoControl/M010.015-test`; it did not establish
production destination provisioning, server-side sibling-folder isolation,
or production backup/restore. No external alert route was added or is required
by the accepted design. Keep `M010.015-test` separate. Before any separately
authorized production cutover, verify the approved destination and its
server-side access boundary, then complete a production backup and separate-
path restore with integrity, foreign-key, selection, representative workflow,
and exact document-hash verification. Take a fresh verified backup
immediately before cutover.

## M010.019 production host readiness

The read-only host assessment found the following. Proposed units and
directories remain uninstalled or unprovisioned; no service, mount, credential,
database, or production destination was changed.

| Gate | Result | Evidence | Owner and exact next action |
|---|---|---|---|
| Current service and authority | PASS | `repo-control.service` remains active as `chuck` with file-backed (`legacy_files`) authority. | No action for this preflight; preserve the service until a separate deployment is approved. |
| Candidate runtime | PASS | Both proposed backup service `ExecStart` entries use `/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3-m010014-clean/bin/python`; it exists and reports CPython 3.12.16 / SQLite 3.51.3. | Deployment operator: use the approved pinned runtime for any separately authorized deployment. |
| Live runtime and loaded source revision | FAIL | The active process uses Python 3.12.3 / SQLite 3.45.1. It exposes no source revision or build hash, so its loaded source revision is unverified. | Deployment operator: deploy the approved pinned runtime only under separate authorization, and record independently verifiable source/build identity before relying on it. |
| Full-suite regression attribution | PASS | Baseline `320a2a4616a5875413c44ce789c72be4d9e2bfed`: 735 tests; accepted `f011f4be5481c6f574c5e770c19c9324f77d7e2f`: 738 tests. Both had 68 failures, 0 errors, and 1 skip with identical failure IDs; no accepted-only failure. | No deployment action. A separate onboarding review should reconcile the one legacy-history `milestone_id` expectation mismatch before changing that behavior. |
| Proposed unit syntax and timer calendars | PASS | All four proposed unit files passed read-only syntax verification and both calendars parsed. Verification exited 0 with an unrelated legacy `/var/run` PIDFile warning from an installed Synology unit. | No action for syntax; this is not evidence that the proposed controls are installed or firing. |
| Installed backup and monitor controls | NOT TESTED | The proposed services and 15-minute backup / independent five-minute monitor timers are not installed. | Deployment operator: after separate approval and installation, verify both timers with `systemctl list-timers --all repo-control-backup.timer repo-control-backup-monitor.timer` and inspect both units' local journal output, including warning and recovery. |
| NAS source/destination decision | PASS | Product Owner correction event `d0fb8f95a73b4f1e8f262efb2b1a90ea` approves `//192.168.1.171/ProjectBackups` and `/mnt/nas/repo-control/RepoControl/repo-control-ii`; `M010.015-test` remains separate. The correction does not authorize provisioning. | No further share-selection action; do not alter the mount or configuration under this assessment. |
| Production destination provisioning | FAIL | The approved `/mnt/nas/repo-control/RepoControl/repo-control-ii` destination is absent. | Host operator: provision only under separate authorization, preserving `M010.015-test` as a distinct disposable target. |
| Backup-account sibling-project isolation | NOT TESTED | The share root cannot be listed by `chuck`; `RepoControl` and `M010.015-test` appear locally as `0700 chuck:chuck`, the parent is locally writable/searchable, and the approved destination is absent. The CIFS mount uses `nounix`, so these client modes do not prove the server ACL; no write probe was attempted. | NAS operator: before deployment, verify server-side access lets the backup account write the exact approved destination but denies create/write access to sibling project folders; preserve evidence and do not test by writing to production siblings. |
| Database, staging, restore paths, and backup environment | FAIL | The proposed local database, staging, separate restore path, and `/etc/repo-control/backup.env` are absent. No credentials or environment-file contents were read. | Host operator: provision approved paths for `chuck:chuck` with `0700` directories and `0600` database/backup files; create `/etc/repo-control/backup.env` as `root:root` mode `0600`; then verify service access without displaying credentials. |
| Recovery-point wiring to every writer | NOT TESTED | No managed SQLite writer is active and the environment file is absent. The example omits `REPOCTL_BACKUP_COVERAGE_PATH`; its code default is `/home/chuck/.local/state/repo-control-backup/staging/.repoctl-verified-recovery-point.json`. The backup units read the environment file, but the active `repo-control.service` has no `EnvironmentFile`, so effective publisher/writer path parity is unverified. | Deployment operator: explicitly configure the same recovery-point path for the backup publisher and each enabled managed writer, then prove they publish/read the same database-, authority-, repository-, and milestone-bound record before permitting writes. |
| Production backup, restore, and one-hour objective | NOT TESTED | M010.018's disposable CIFS rehearsal passed, but no production backup or separate-path restore was run. Production cadence, outage behavior, and restore duration are unmeasured. | Deployment operator: during a separately authorized deployment, create a fresh verified production backup, restore it to a separate local path, verify integrity/foreign keys and identity/content evidence, and measure restore duration. Do not treat the one-hour loss objective as a restore-time guarantee. |

The approved share and destination are now specified, but the production
destination is absent, sibling-project isolation is unverified, other required
paths are absent, and the live runtime does not meet the pinned target.
Evidence supports planning a bounded preparation step to verify server-side
access and provision the approved destination only after separate
authorization; it does not support production activation or live SQLite
cutover. The existing 45-minute rehearsal validates the guard design on
disposable data only; it does not establish production backup coverage or
rollback safety.

### Ordered checklist for a separately approved deployment

1. **Use the approved share and verify isolation before provisioning.** Use
   `//192.168.1.171/ProjectBackups` with production destination
   `/mnt/nas/repo-control/RepoControl/repo-control-ii`; keep
   `/mnt/nas/repo-control/RepoControl/M010.015-test` separate. Before
   deployment, obtain NAS-side evidence that the backup account can write the
   approved destination but cannot create or write sibling project folders.
   The current `nounix` CIFS mode display is not proof of server ACL scope.
   Provision the destination and local paths only under separate authorization.
   Privately validate `/etc/repo-control/backup.env` ownership/mode and service
   access without printing its contents.
2. **Verify the runtime and authority before changing service behavior.**
   Confirm the candidate reports Python 3.12.16 / SQLite 3.51.3, record the
   deployed source/build identity, and verify the service's current authority.
   If any value or source identity is uncertain, abort before cutover and keep
   the file-backed service unchanged.
3. **Wire and verify the guard record before enabling writers.** Explicitly set
   one `REPOCTL_BACKUP_COVERAGE_PATH` in the backup publisher and every managed
   writer's effective environment. Keep managed writes paused while the first
   fresh verified production NAS backup publishes the record. Verify that the
   record binds the exact active database, authority, repository, and selected
   milestone, and that each writer reads that same record. Do not infer this
   from matching defaults.
4. **Install and verify the controls only under the deployment authorization.**
   Install the proposed units, then confirm the backup timer fires every
   15 minutes and the independent monitor every five minutes with
   `systemctl list-timers --all repo-control-backup.timer repo-control-backup-monitor.timer`.
   Inspect `repo-control-backup.service` and
   `repo-control-backup-monitor.service` journal output to verify the local
   20-minute warning and recovery behavior. These local journal messages are
   not an unattended alert route.
5. **Verify a separate-path restore before permitting managed writes.** Restore
   the fresh manifest to the configured separate local restore directory; do
   not overwrite the live database. Verify database integrity, foreign keys,
   repository/selection/authority binding, representative workflow state, and
   exact document hashes. Keep writes paused unless backup, restore, and
   recovery-point checks all pass.
6. **Use a conservative abort boundary.** Before the first SQLite-only write,
   abort by leaving managed writes paused and retaining file-backed authority.
   After any SQLite-only write, preserve the SQLite database and verified NAS
   copies; do not switch back to file-backed authority without a separately
   approved reconciliation or forward-recovery plan. Safe rollback after that
   point is unproven.
7. **Measure the objectives separately.** The 45-minute guard cutoff leaves a
   15-minute margin against the one-hour loss target only when every managed
   writer uses the verified guard. Measure actual backup cadence and outage
   response separately from restore duration; a successful restore does not
   prove the one-hour loss objective or a bounded recovery time.
