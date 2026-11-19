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

The live database must stay on local storage. `ops/backup.env.example` now
contains non-secret proposal values for the approved NAS source/destination
and shared recovery-point path. It is not the protected host configuration;
the Product Owner reports `/etc/repo-control/backup.env` is provisioned and
matches the example. Approval of the NAS destination does not authorize live
service changes, backup writes, or cutover:

| Role | Path / identity and status |
|---|---|
| Database | `/home/chuck/.local/share/repoctl-sqlite/repo-control-ii/information-store.sqlite3` (absent; parent prepared `0700 chuck:chuck`) |
| NAS mount | `/mnt/nas/repo-control` (current CIFS mount) |
| NAS source | `//192.168.1.171/ProjectBackups` (approved by Product Owner in correction event `d0fb8f95a73b4f1e8f262efb2b1a90ea`) |
| NAS destination | `/mnt/nas/repo-control/RepoControl/repo-control-ii` (directory exists; authorized sentinel test passed) |
| Local staging | `/home/chuck/.local/state/repo-control-backup/staging` (`0700 chuck:chuck`, prepared locally) |
| Separate local restore | `/home/chuck/.local/state/repo-control-backup/restore` (`0700 chuck:chuck`, prepared locally) |
| Recovery-point record | `/home/chuck/.local/state/repo-control-backup/staging/.repoctl-verified-recovery-point.json` (configured path; record absent) |
| Host backup environment | `/etc/repo-control/backup.env` (`root:root` mode `0600`; Product Owner reports byte-for-byte match to the tracked example) |
| Job identity | `chuck` (`uid=1000` on the observed host) |

M010.014 found no dedicated Repo Control share. M010.019 now records the
Product Owner's approval to use the existing `ProjectBackups` share with the
destination `/mnt/nas/repo-control/RepoControl/repo-control-ii`. Keep
`/mnt/nas/repo-control/RepoControl/M010.015-test` separate; it is not the
production destination.

The tracked example now matches the approved source and destination and
explicitly sets `REPOCTL_BACKUP_COVERAGE_PATH`. Product Owner correction event
`bf23be35f93f41678e3319b5b06e8f3d` reports that the protected host file
matches this example byte for byte. A read-only metadata check confirms the
file is `root:root` mode `0600`; its contents were not read or displayed.
Neither that host-file verification nor the destination's existence proves
that access is isolated from other projects under `ProjectBackups`.

The initial M010.020 inspection found the approved destination absent.
Following the Product Owner correction, a read-only `stat` confirms that
`/mnt/nas/repo-control/RepoControl/repo-control-ii` now exists as a directory
on the mounted CIFS share. It is presented client-side as `0700
chuck:chuck`, but the mount reports `nounix`, `uid=1000`, `gid=1000`,
`dir_mode=0700`, and `file_mode=0600`; these synthetic client modes do not
establish the NAS server's effective ACL. The earlier share-root listing as
`chuck` was denied. No write probe was attempted during that initial
inspection.

Product Owner correction event `3ed3f92eb172453f8934026b5eba9e6f` approves
`ProjectBackups/RepoControl` and its subfolders as the write boundary for
`repoctl-backup`, superseding the earlier destination-only requirement. After
verifying the mount source and SMB username, the Coder created, read, and
removed one unique harmless sentinel in the exact approved destination as
`chuck:chuck` (Repo Control test event
`16909daa18e04eeb92b3278f0129d822`). No existing file, `M010.015-test`, or
path outside `RepoControl` was touched.

Isolation from other projects under `ProjectBackups` remains NOT TESTED. The
mount's `username=repoctl-backup` and client-presented CIFS modes do not prove
server-side rights outside `RepoControl`; the earlier share-root listing denial
also does not establish write isolation. No NAS-side effective-access/ACL
evidence for other projects was available, and no write was attempted outside
`RepoControl`. Report this isolation separately; do not test it by writing to
other projects. Keep `M010.015-test` and all existing files untouched.

Keep any mount credential in a separate root-owned file with mode `0600`,
outside Git and logs; do not put it in `backup.env`. The M010.020 preparation
created the local database parent, staging directory, and separate restore
directory as `0700 chuck:chuck` on local ext4. No database or recovery-point
record was created. Access to the approved NAS destination was verified by the
authorized sentinel test; isolation from other projects under `ProjectBackups`
remains unverified. Do not write outside `RepoControl`.

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

This table preserves the M010.019 pre-provisioning assessment; the M010.020
status below records subsequent local preparation, operator provisioning, and
proposal updates. The read-only host assessment found the following. Proposed
units and directories remained uninstalled or unprovisioned at that
assessment; no service, mount, credential, database, or production destination
was changed then.

| Gate | Result | Evidence | Owner and exact next action |
|---|---|---|---|
| Current service and authority | PASS | `repo-control.service` remains active as `chuck` with file-backed (`legacy_files`) authority. | No action for this preflight; preserve the service until a separate deployment is approved. |
| Candidate runtime | PASS | Both proposed backup service `ExecStart` entries use `/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3-m010014-clean/bin/python`; it exists and reports CPython 3.12.16 / SQLite 3.51.3. | Deployment operator: use the approved pinned runtime for any separately authorized deployment. |
| Live runtime and loaded source revision | FAIL | The active process uses Python 3.12.3 / SQLite 3.45.1. It exposes no source revision or build hash, so its loaded source revision is unverified. | Deployment operator: deploy the approved pinned runtime only under separate authorization, and record independently verifiable source/build identity before relying on it. |
| Full-suite regression attribution | PASS | Baseline `320a2a4616a5875413c44ce789c72be4d9e2bfed`: 735 tests; accepted `f011f4be5481c6f574c5e770c19c9324f77d7e2f`: 738 tests. Both had 68 failures, 0 errors, and 1 skip with identical failure IDs; no accepted-only failure. | No deployment action. A separate onboarding review should reconcile the one legacy-history `milestone_id` expectation mismatch before changing that behavior. |
| Proposed unit syntax and timer calendars | PASS | All four proposed unit files passed read-only syntax verification and both calendars parsed. Verification exited 0 with an unrelated legacy `/var/run` PIDFile warning from an installed Synology unit. | No action for syntax; this is not evidence that the proposed controls are installed or firing. |
| Installed backup and monitor controls | NOT TESTED | The proposed services and 15-minute backup / independent five-minute monitor timers are not installed. | Deployment operator: after separate approval and installation, verify both timers with `systemctl list-timers --all repo-control-backup.timer repo-control-backup-monitor.timer` and inspect both units' local journal output, including warning and recovery. |
| NAS source/destination decision | PASS | Product Owner correction event `d0fb8f95a73b4f1e8f262efb2b1a90ea` approves `//192.168.1.171/ProjectBackups` and `/mnt/nas/repo-control/RepoControl/repo-control-ii`; `M010.015-test` remains separate. That M010.019 correction did not authorize provisioning at the time. | No further share-selection action; do not alter the mount or configuration under this assessment. |
| Production destination provisioning | FAIL | The approved `/mnt/nas/repo-control/RepoControl/repo-control-ii` destination is absent. | Host operator: provision only under separate authorization, preserving `M010.015-test` as a distinct disposable target. |
| Backup-account sibling-project isolation | NOT TESTED | The share root cannot be listed by `chuck`; `RepoControl` and `M010.015-test` appear locally as `0700 chuck:chuck`, the parent is locally writable/searchable, and the approved destination is absent. The CIFS mount uses `nounix`, so these client modes do not prove the server ACL; no write probe was attempted. | NAS operator: before deployment, verify server-side access lets the backup account write the exact approved destination but denies create/write access to sibling project folders; preserve evidence and do not test by writing to production siblings. |
| Database, staging, restore paths, and backup environment | FAIL | The proposed local database, staging, separate restore path, and `/etc/repo-control/backup.env` are absent. No credentials or environment-file contents were read. | Host operator: provision approved paths for `chuck:chuck` with `0700` directories and `0600` database/backup files; create `/etc/repo-control/backup.env` as `root:root` mode `0600`; then verify service access without displaying credentials. |
| Recovery-point wiring to every writer | NOT TESTED | No managed SQLite writer is active and the environment file is absent. The example omits `REPOCTL_BACKUP_COVERAGE_PATH`; its code default is `/home/chuck/.local/state/repo-control-backup/staging/.repoctl-verified-recovery-point.json`. The backup units read the environment file, but the active `repo-control.service` has no `EnvironmentFile`, so effective publisher/writer path parity is unverified. | Deployment operator: explicitly configure the same recovery-point path for the backup publisher and each enabled managed writer, then prove they publish/read the same database-, authority-, repository-, and milestone-bound record before permitting writes. |
| Production backup, restore, and one-hour objective | NOT TESTED | M010.018's disposable CIFS rehearsal passed, but no production backup or separate-path restore was run. Production cadence, outage behavior, and restore duration are unmeasured. | Deployment operator: during a separately authorized deployment, create a fresh verified production backup, restore it to a separate local path, verify integrity/foreign keys and identity/content evidence, and measure restore duration. Do not treat the one-hour loss objective as a restore-time guarantee. |

At M010.019 closeout, the approved share and destination were specified, but
the production destination was absent, sibling-project isolation was
unverified, other required paths were absent, and the live runtime did not
meet the pinned target. Evidence supported planning a bounded preparation
step; it did not support production activation or live SQLite cutover. The
existing 45-minute rehearsal validates the guard design on disposable data
only; it does not establish production backup coverage or rollback safety.

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

## M010.020 production backup preparation

M010.020 prepares local paths and reviewable configuration only. It leaves the
live file-backed service, CIFS mount, systemd activation, production database,
NAS backup, and recovery-point record unchanged.

| Gate | Result | Evidence | Owner and exact next action |
|---|---|---|---|
| Current mount and live authority | PASS | `/mnt/nas/repo-control` is CIFS from `//192.168.1.171/ProjectBackups`; the service remains active as `chuck`, and `/healthz` reports `legacy_files` with a null InformationStore binding. | Preserve the running file-backed service; do not install the proposed unit. |
| Approved destination exists | PASS (existence only) | Product Owner correction event `bf23be35f93f41678e3319b5b06e8f3d` reports creation; read-only `stat` confirms the exact approved path is a directory on the CIFS mount. Client-presented mode is synthetic and is not ACL evidence. | Do not treat existence or client metadata as access proof. |
| Approved destination sentinel test | PASS | With the mount verified as `//192.168.1.171/ProjectBackups` (`cifs`, SMB username `repoctl-backup`) and process identity `chuck:chuck`, one unique sentinel was created, read back, and removed in `/mnt/nas/repo-control/RepoControl/repo-control-ii` (event `16909daa18e04eeb92b3278f0129d822`). No payload or other path was written. | The Product Owner-approved write boundary is `ProjectBackups/RepoControl` and its subfolders. Do not write to `M010.015-test`, existing files, or paths outside `RepoControl`. |
| Isolation from other projects under `ProjectBackups` | NOT TESTED | No NAS-side effective-access/ACL evidence for other projects was available. CIFS client modes and the earlier denied share-root listing do not prove write isolation. No write was attempted outside `RepoControl`. | NAS request event `d449d294ebba4c62920e18e3bd2e8401` asks for sanitized, read-only effective-rights evidence for `repoctl-backup`, including share/folder ACLs, inherited permissions, and group grants. Do not test by writing to other projects. |
| Local database parent, staging, and restore paths | PASS | The three paths are distinct, on local ext4, owned by `chuck:chuck`, mode `0700`, and accessible to the job identity. The database and recovery-point record do not exist. | No further local-path action; do not initialize the database or create a recovery-point record in this milestone. |
| Protected backup environment | PASS | Product Owner correction event `bf23be35f93f41678e3319b5b06e8f3d` reports a byte-for-byte match to `ops/backup.env.example`; read-only `stat` confirms a regular file owned by `root:root`, mode `0600`. Contents were not read or displayed. | No further host-file action. Keep CIFS credentials separate and do not print or copy the protected file. |
| Candidate runtime and source | PASS | The intended interpreter is `/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3-m010014-clean/bin/python`; `ops/verify_sqlite_runtime.py` reports Python 3.12.16 / SQLite 3.51.3 and production eligibility, and `pip check` passes. Its editable `repoctl` source is this checkout at HEAD `3282168bde4221a987e201dd2f80b7d9b178434c`. | Use this interpreter and reviewed source revision for the staged deployment. It lacks optional `_ssl`, `_ctypes`, `_bz2`, `_lzma`, `readline`, and `_curses`; `hashlib` imports successfully. Rebuild before any feature that requires those modules. |
| Proposed systemd syntax and calendars | PASS (proposals only) | `systemd-analyze verify` passed for the browser service, backup service/timer, and monitor service/timer. Both the 15-minute backup and five-minute monitor calendars parsed. One unrelated legacy `/var/run` warning came from the installed Synology Active Backup unit. | No action until a separate deployment authorization; none of these units was installed or activated. |
| Recovery-point environment wiring | PASS for proposals; NOT TESTED at runtime | Backup and monitor proposals read `/etc/repo-control/backup.env`; the proposed `repo-control.service` now uses that same file and the pinned interpreter. The active service still has no `EnvironmentFile`. `InformationStore` and backup code read `REPOCTL_BACKUP_COVERAGE_PATH` from each process environment. | For an independent Coder CLI process, explicitly set `REPOCTL_BACKUP_COVERAGE_PATH=/home/chuck/.local/state/repo-control-backup/staging/.repoctl-verified-recovery-point.json` in that process's environment; do not assume it inherits systemd settings. Verify actual parity before enabling any managed writer. |
| Candidate authority preparation and activation | NOT TESTED | No production database or managed authority exists. Source inspection confirms the publisher requires an active exact selection/authority to create the bound recovery-point record. | In the next separately authorized milestone, prepare and activate the reviewed candidate with writers quiesced and explicit Product Owner/source-parity confirmation. No managed application write is required before the first verified backup. |
| Production backup, restore, timers, and forward recovery | NOT TESTED | No production NAS payload, recovery record, installed timer, or managed SQLite writer exists. Post-first-managed-write recovery has not been rehearsed. | After authority activation, publish and validate its first verified NAS backup, perform a separate-path restore, then verify controls before Product Owner GO. Before the first managed SQLite-only application write, keep writers paused and use only the explicit pre-first-write abort path if abandoning the candidate. After a managed write, preserve the database and applicable WAL/SHM plus snapshots; rehearse a failure after that write, separate-path restore, and deterministic reconciliation of every post-snapshot write. Do not switch to files or claim recovery readiness until that rehearsal passes. |

The ordering has no requirement to allow an application write before the first
verified backup: authority and exact selection must be prepared and activated
first so the backup can bind to them, while managed writers remain quiesced.
The successful backup can then publish the recovery-point record that permits
writes. This milestone does not perform that authority activation. An
independently launched Coder CLI receives no systemd environment implicitly;
its explicit coverage-path setting must be verified before managed CLI writes
are allowed.

Activation writes authority metadata to the prepared candidate database, but
the managed-write boundary remains the first application write. Before that
boundary, the reviewed explicit abort path is available only with Product
Owner confirmation and writers quiesced. After it, preserve SQLite authority
and rehearse forward recovery; do not revert to file-backed authority.

Use the same explicit process setting for each independent Coder CLI write
invocation; do not source the root-only backup environment or assume that a
VS Code-launched process inherits a systemd service environment:

```sh
REPOCTL_BACKUP_COVERAGE_PATH=/home/chuck/.local/state/repo-control-backup/staging/.repoctl-verified-recovery-point.json \
  /home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3-m010014-clean/bin/python \
  -m repoctl.cli <exact-Coder-CLI-write-command>
```

The current disposition is **NO-GO for live SQLite cutover**. The approved
destination sentinel test passed; the remaining NAS finding is isolation from
other projects under `ProjectBackups`, which remains NOT TESTED. The protected
host environment file is present. No backup, timer, service, production
database, or managed writer was activated in M010.020.

## M010.022 safe SQLite staging controls

M010.022 adds a persistent freeze for cooperating file-backed application
writers and keeps prepared SQLite authority in a distinct candidate state
root. The freeze marker survives process restart. Acquiring the shared state
lock drains admitted writers; a new mutation checks the marker while holding
that lock and fails closed. A bounded lock timeout leaves no freeze marker.
Normal release requires the exact freeze ID and matching registered-source
and registry fingerprints. Abort release also requires the exact ID and a
non-empty reason. The marker has no expiry or automatic restart cleanup.

The freeze governs Repo Control browser, Coder CLI, workflow, selection,
dispatch, closeout, procedure, and related cooperating file-backed writes.
It does not stop external editors, Git commands, or noncooperating processes.
During a separately approved maintenance window, the operator must keep those
writers quiescent and review registered-source drift. The importer and normal
release recheck registered inventory and registry fingerprints; on drift,
do not activate the candidate and use the explicit abort release. The
`--writers-quiesced` option is an assertion, not a freeze or independent proof.

| Control | Result | Evidence boundary |
|---|---|---|
| Cooperating writer freeze, drain, source parity, normal release, and abort | PASS on disposable roots | Pinned-runtime tests prove an admitted writer drains, new process/browser/independent Coder CLI mutations are rejected, the marker persists across processes, lock timeout creates no marker, source drift blocks import/release, and exact-token normal/abort release. This is not a production freeze or a control over external editors/Git. |
| Separate candidate state-root binding | PASS on disposable data | A separate loopback browser, independent Coder CLI process, and verified backup publisher resolve the same candidate authority, selection, workflow count, and recovery-point binding. The source selection/workflow bytes remain unchanged. Missing candidate selection does not use the source selection; invalid authority and missing/shared-default coverage paths fail closed. |
| Independent pre-GO candidate writer hold | NOT TESTED | The disposable app-level freeze is not an operator-owned access boundary. The observed service identity is `chuck` (UID 1000); noninteractive `sudo` is unavailable. No root-owned or separate-operator control was installed or exercised, and no same-owner permission change is acceptable evidence. Obtain operator-controlled identity/access evidence and prove that `chuck` cannot release the hold through browser, CLI, default settings, restart, or writable ownership before enabling candidate writes. |
| Production cutover and recovery operations | NOT TESTED / NO-GO | No live freeze, service change/restart, candidate activation, production database initialization, NAS backup, timer action, or Git mutation was performed. The production backup/restore, installed timer, and operator-owned writer-hold gates remain separate requirements. |

To close the writer-hold gate, retain the exact control path and owner,
effective access evidence for both the operator and `chuck`, and before/after
hashes for the candidate database, SQLite sidecars, source selection/workflow,
and recovery-point record. With fresh valid candidate-bound coverage, prove
browser and independent CLI writes are denied while reads and backup/restore
remain viable; repeat the denial after an ordinary process restart; then show
that only the operator identity can release the hold. Re-run the inclusive
45-minute stale-coverage test after deliberate release. These observations
must come from the actual identity boundary, not a same-owner file mode.

### Later separately approved staging procedure

The following is a reviewable procedure, not authorization to run it. Do not
start the candidate browser or enable managed writes until the independent
writer-hold gate above has passed. In a later approved window, first verify
the active service's actual `--state-root`; the value below is the code
default and is correct only when the running service has no override. The
candidate directories must be new, local, private, outside all repositories,
and distinct from the source state root, production database, and live
recovery-point path.

```sh
PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
REPO=/home/chuck/projects/repo-control-ii
REGISTRY="$REPO/ops/repositories.toml"
SOURCE_STATE_ROOT=/home/chuck/.local/share/repoctl
CANDIDATE_STATE_ROOT=/home/chuck/.local/share/repoctl-m010.022-candidate
IMPORT_DIR=/home/chuck/.local/share/repoctl-sqlite-candidate/m010.022
IMPORT_REPORT="$IMPORT_DIR/import-report.json"
FREEZE_REPORT="$IMPORT_DIR/freeze-report.json"
PLAN_REPORT="$IMPORT_DIR/activation-plan.json"
CANDIDATE_COVERAGE_PATH="$IMPORT_DIR/.repoctl-candidate-recovery-point.json"

# Run only after separately approved operator preflight; never reuse these paths.
test ! -e "$CANDIDATE_STATE_ROOT"
test ! -e "$IMPORT_DIR"
mkdir -m 0700 "$CANDIDATE_STATE_ROOT"
mkdir -m 0700 "$IMPORT_DIR"
```

Confirm the live file-backed service still reports `legacy_files`, the source
root is the one currently used by that service, the registry is the exact
canonical `ops/repositories.toml`, and the host has not changed since review.
Keep the original service and its state-root configuration unchanged. Quiesce
external editors/Git writers through the approved maintenance procedure, then
establish and retain the cooperating-writer freeze:

```sh
"$PY" -m repoctl.cli store-authority freeze-writers \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" > "$FREEZE_REPORT"
FREEZE_ID=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["freeze_id"])' "$FREEZE_REPORT")
```

The returned record includes the freeze ID and source/registry hashes. A
freeze failure or lock timeout is an abort: do not import. Import only into
the new candidate directory and copy each selection into the new candidate
state root; never bind or mutate the source-root selection:

```sh
"$PY" -m repoctl.cli store-authority prepare \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" \
  --candidate-state-root "$CANDIDATE_STATE_ROOT" \
  --destination "$IMPORT_DIR" \
  --writers-quiesced > "$IMPORT_REPORT"
DATABASE=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_database_path"])' "$IMPORT_REPORT")

for INDEX in $("$PY" -c 'import json,sys; print(*range(len(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"])))' "$IMPORT_REPORT"); do
  BINDING_JSON=$("$PY" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"][int(sys.argv[2])], separators=(",", ":")))' "$IMPORT_REPORT" "$INDEX")
  "$PY" -m repoctl.cli store-authority bind-selection \
    --registry "$REGISTRY" \
    --state-root "$CANDIDATE_STATE_ROOT" \
    --database "$DATABASE" \
    --binding-json "$BINDING_JSON" \
    --po-confirm
done
```

Review the exact import report and each selection binding. Preview and activate
with the candidate state root as the authority root and the frozen original
root as the source-parity root. Keep the same generation and plan hash for
both commands:

```sh
GENERATION=$("$PY" -c 'import secrets; print(secrets.token_hex(16))')
"$PY" -m repoctl.cli store-authority preview \
  --registry "$REGISTRY" \
  --state-root "$CANDIDATE_STATE_ROOT" \
  --source-state-root "$SOURCE_STATE_ROOT" \
  --database "$DATABASE" \
  --generation "$GENERATION" > "$PLAN_REPORT"
PLAN_SHA256=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["plan_sha256"])' "$PLAN_REPORT")

# Do not run until the independent candidate writer hold has been proved.
"$PY" -m repoctl.cli store-authority activate \
  --registry "$REGISTRY" \
  --state-root "$CANDIDATE_STATE_ROOT" \
  --source-state-root "$SOURCE_STATE_ROOT" \
  --database "$DATABASE" \
  --generation "$GENERATION" \
  --expected-plan-sha256 "$PLAN_SHA256" \
  --po-confirm \
  --writers-quiesced \
  --source-parity-confirmed
```

After successful import and parity review, release the source freeze normally
with the exact ID. This rechecks the registered-source and registry hashes:

```sh
"$PY" -m repoctl.cli store-authority release-writers \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" \
  --freeze-id "$FREEZE_ID"
```

Activation's confirmation flags do not substitute for the operator-owned
writer hold. Before any candidate service is started, choose a private
candidate-only coverage path under the candidate staging directory, and pass
that exact absolute path to the candidate browser, every independent Coder
CLI process, and the backup publisher. A non-default managed state root
rejects a missing coverage path and rejects the live default recovery-point
path. Do not source or print the root-only backup environment file in a Coder
shell.

After the writer hold is independently verified, a candidate browser may be
started on a separate loopback port using the same canonical registry and
candidate root:

```sh
REPOCTL_BACKUP_COVERAGE_PATH="$CANDIDATE_COVERAGE_PATH" \
  "$PY" -m repoctl.cli web \
    --repository-registry "$REGISTRY" \
    --host 127.0.0.1 \
    --port 18766 \
    --state-root "$CANDIDATE_STATE_ROOT"
```

In another terminal, inspect `/healthz` and independently query the Coder
CLI. The reported `authority_state_root`, database identity, repository,
milestone, prompt, and selection ID/revision must identify the candidate:

```sh
curl -fsS http://127.0.0.1:18766/healthz
REPOCTL_BACKUP_COVERAGE_PATH="$CANDIDATE_COVERAGE_PATH" \
  "$PY" -m repoctl.cli coder-status \
    --repository "$REPO" \
    --state-root "$CANDIDATE_STATE_ROOT" \
    --format json
```

The disposable mixed-client rehearsal also ran the actual verified-backup
publisher function against temporary local staging and destination
directories. It compared the browser and independent CLI selection/workflow
with the backup snapshot and authority-bound recovery-point record. It did
not invoke the host backup command or write to the NAS. A later production
backup requires a separate approval, a fresh verified candidate, the
approved mount/source/destination, and a distinct candidate coverage file;
never point the candidate publisher at the live coverage record. The
publisher environment must name the exact candidate database, state root,
coverage path, repository ID/key/root, staging, destination and restore root.
Do not treat one repository's selection-bound recovery point as coverage for
another selection. The following publisher/restore and timer checks are
later production gates, not commands run for M010.022:

```sh
BACKUP_STAGING=/home/chuck/.local/state/repo-control-backup/candidate-staging
BACKUP_DESTINATION=/mnt/nas/repo-control/RepoControl/repo-control-ii
BACKUP_RESTORE_ROOT=/home/chuck/.local/state/repo-control-backup/candidate-restore
REPO_ID=repo-control-ii--945e7f88eafb
REPO_KEY=repo-control-ii
REPO_ROOT=/home/chuck/projects/repo-control-ii

env \
  REPOCTL_BACKUP_SOURCE="$DATABASE" \
  REPOCTL_BACKUP_STAGING="$BACKUP_STAGING" \
  REPOCTL_BACKUP_DESTINATION="$BACKUP_DESTINATION" \
  REPOCTL_BACKUP_RESTORE_ROOT="$BACKUP_RESTORE_ROOT" \
  REPOCTL_BACKUP_MOUNTPOINT=/mnt/nas/repo-control \
  REPOCTL_BACKUP_MOUNT_SOURCE=//192.168.1.171/ProjectBackups \
  REPOCTL_BACKUP_REPOSITORY_ID="$REPO_ID" \
  REPOCTL_BACKUP_REPOSITORY_KEY="$REPO_KEY" \
  REPOCTL_BACKUP_REPOSITORY_ROOT="$REPO_ROOT" \
  REPOCTL_BACKUP_AUTHORITY_STATE_ROOT="$CANDIDATE_STATE_ROOT" \
  REPOCTL_BACKUP_COVERAGE_PATH="$CANDIDATE_COVERAGE_PATH" \
  "$PY" -m repoctl.backup backup

# After a fresh manifest exists, restore it to a new separate local path.
env \
  REPOCTL_BACKUP_SOURCE="$DATABASE" \
  REPOCTL_BACKUP_STAGING="$BACKUP_STAGING" \
  REPOCTL_BACKUP_DESTINATION="$BACKUP_DESTINATION" \
  REPOCTL_BACKUP_RESTORE_ROOT="$BACKUP_RESTORE_ROOT" \
  REPOCTL_BACKUP_MOUNTPOINT=/mnt/nas/repo-control \
  REPOCTL_BACKUP_MOUNT_SOURCE=//192.168.1.171/ProjectBackups \
  REPOCTL_BACKUP_REPOSITORY_ID="$REPO_ID" \
  REPOCTL_BACKUP_REPOSITORY_KEY="$REPO_KEY" \
  REPOCTL_BACKUP_REPOSITORY_ROOT="$REPO_ROOT" \
  REPOCTL_BACKUP_AUTHORITY_STATE_ROOT="$CANDIDATE_STATE_ROOT" \
  REPOCTL_BACKUP_COVERAGE_PATH="$CANDIDATE_COVERAGE_PATH" \
  "$PY" -m repoctl.backup restore --manifest <exact-verified-manifest> \
  --restore-name m010022-separate-restore.sqlite3

# Only after separately authorized unit installation:
systemctl list-timers --all repo-control-backup.timer repo-control-backup-monitor.timer
```

The backup command writes to the approved NAS destination; do not run it
without separate operator authorization. Before using these commands, create
the candidate staging/restore directories with the approved private ownership
and verify the repository ID/key/root against the exact registry entry. The
restore must pass integrity, foreign-key, authority, selection, workflow, and
document-hash checks. Timer listing alone does not prove a successful backup
or monitor run; inspect the corresponding service journals and verify warning
and recovery output.

Abort before activation if source parity changes, an import is incomplete, a
candidate identity differs, or any candidate setting is absent/mismatched.
Leave the prepared database and candidate files isolated for review; do not
switch the original service to them. Release the original freeze only by its
exact ID and an explicit abort reason:

```sh
"$PY" -m repoctl.cli store-authority release-writers \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" \
  --freeze-id "$FREEZE_ID" \
  --abort-reason "Candidate staging aborted before activation; source remains authoritative."
```

If a later approved rehearsal activates a candidate but has not made a
managed application write, stop the candidate server, prove writers are
quiesced, and use the exact reviewed plan for `store-authority abort` with
`--state-root "$CANDIDATE_STATE_ROOT"` and
`--source-state-root "$SOURCE_STATE_ROOT"`, `--po-confirm`, and
`--writers-quiesced`. Then release any still-active source freeze with an
explicit abort reason. The original file-backed service must remain pointed
at the original state root; confirm `/healthz` reports `legacy_files` and a
null InformationStore binding. If any managed SQLite write occurred, abort
is refused: preserve the database and applicable WAL/SHM plus verified
backups, keep writes paused, and use a separately approved forward-recovery
plan instead of switching back to files.

M010.022 ends at **REVIEW / NO-GO**. A production backup and separate restore,
installed backup/monitor timer validation, the independent operator-owned
candidate writer hold, and a separately approved production activation remain
later gates. The active file-backed service, global selection, production
database, NAS payloads, timers, and Git state remain outside this milestone.

## M010.023 disposable operator-controlled SQLite write hold

M010.023 exercised the operator-owned hold on a private disposable candidate
only. This evidence does not authorize or demonstrate a production hold,
service change, NAS write, timer installation, or SQLite cutover. The candidate
used `/tmp/repoctl-m010023.1qwFGEt5TR-hold/candidate-state`, database
`/tmp/repoctl-m010023.1qwFGEt5TR-hold/prepared/managed-prepared.sqlite3`,
selection `6e71658b96fa44108e7d21764fc8db60` for `M010.023`, and a separate
local coverage, backup destination, staging, and restore path. Browser and
independent CLI processes ran as `chuck` (UID/EUID 1000), without privilege
escalation.

### Fixture hold and release procedure

The operator used a one-off root-only helper that checked the exact candidate
device/inodes, file types, owners, groups, modes, non-symlink paths, and absence
of open protected-file holders before changing anything. It covered 17 exact
paths; it did not use recursive permission changes. The helper was kept in
session state and is not a production tool.

| Candidate path class | Held owner and mode | Purpose |
|---|---|---|
| Candidate, repository, and milestones directories | `root:chuck 0750` | Prevent `chuck` from replacing protected children. |
| Authority guard/configuration, selections, database, and WAL | `root:chuck 0440` | Preserve read access while denying writes and replacement. |
| SQLite SHM | `root:chuck 0660` | Product Owner-approved exception required for SQLite online backup; it does not make the database or WAL writable. |
| `.locks` directory and lock file | `chuck:chuck 0700` / `0600` | Preserve the application's candidate lock path. |

Before the hold, the protected fixture paths were owned by `chuck:chuck`;
directories were `0700`, authority/selection files `0600`, and SQLite
DB/WAL/SHM files `0644`. Release restored those original owners and modes.
The operator-reported release output was
`RELEASE PASS: 17 exact fixture paths verified.` A subsequent unprivileged
inspection returned `INSPECT PASS: 17 exact fixture paths; no protected-file
holders.` The tested trust boundary was that the app and CLI ran as `chuck`
without privilege escalation, while the Product Owner ran the helper as root.
No claim was made that `chuck` is a separate OS account from the operator or
that sudo policy was changed.
An unprivileged `chmod` of the root-owned SHM and creation of a file under a
protected milestones directory both failed with permission errors.

For a repeat disposable rehearsal, first verify candidate identity and fresh
local backup coverage, stop all fixture writers, then have the operator run
the exact-path hold helper and inspect its output. Start the candidate browser
and independent CLI as `chuck`, verify both bind to the same candidate root,
database, selection, and coverage, then test reads, backup/restore, and
representative writes. To release, stop the browser and CLI, verify no
protected-file holders, and have the operator run the helper's exact-path
release action. Do not substitute same-owner `chmod`, a freshness flag, or
recursive `chown`/`chmod`. A production deployment requires a separately
reviewed, operator-owned control; the session-local fixture helper is not
suitable for installation.

### M010.023 fixture evidence and limitations

With the hold active and coverage fresh, candidate reads remained available.
The actual verified local backup
`repo-control-20261116T161838149196Z-12e09cf29d62.manifest.json` published
108,204,032 bytes with integrity `ok` and zero foreign-key violations. Its
recovery-point timestamp was `2026-11-16T16:18:38.915414+00:00`; restoring to
the distinct local fixture path succeeded and matched the backup hash.
Independent candidate `coder-status` remained bound to the selected
`M010.023` candidate and reported four workflow events.

After restarting the candidate browser as `chuck`, a fresh CLI write and an
actual browser workflow POST were denied by SQLite with
`attempt to write a readonly database`. The browser returned HTTP 500
`unexpected_error`; an instrumented fixture call captured the underlying
`sqlite3.OperationalError`. No event was added. Candidate DB/WAL/SHM,
authority guard/configuration, all selection files, and the selected prompt
matched their pre-attempt hashes. The HTTP error is a limitation: the write is
blocked, but the UI does not present a clear hold-denial response.

The normal `repoctl web` startup could not create scanner temporary files under
the held candidate root. For the browser test only, the fixture redirected
scanner cache output to a separate writable local cache while keeping both the
InformationStore root and lock root on the candidate. The actual browser then
served candidate reads and exercised the standard workflow route. M010.024
adds the explicit `--scanner-cache-root` option and routes scanner-derived
status/context artifacts there while keeping authority, lock, snapshot, and
workflow records on the candidate state root. A focused test verifies startup
with a read-only state root and a separate writable cache. This resolves the
startup limitation in code, but does not replace the required production-shaped
held-candidate browser test.

The operator then released the hold. With the injected clock set exactly to
`2026-11-16T17:03:38.915414+00:00` (the inclusive 45-minute age of the prior
coverage), an independent candidate CLI write was rejected with
`writes are paused` and `45.0 minutes old`; event count remained four and the
database hash did not change. A new local verified backup was then published
at `2026-11-16T16:47:30.899360+00:00`, with integrity `ok` and zero foreign-key
violations. A fresh independent CLI write succeeded, advancing the candidate
workflow from four to five events. This verifies the stale-coverage guard
separately from the filesystem hold.

| Acceptance area | Result | Evidence boundary |
|---|---|---|
| Independent CLI and browser write denial | PASS, with generic browser HTTP 500 limitation | Fresh coverage; CLI readonly-database error; browser POST's underlying readonly-database exception; no candidate event or protected-file hash change. |
| Reads, verified backup, and separate restore under hold | PASS | Candidate reads, local verified backup, and hash-matched restore; no NAS access. |
| Restart persistence | PASS with scanner-cache workaround | Browser restarted as `chuck` against the same candidate authority and lock root; the normal CLI startup path could not create scanner temp files under the hold. |
| Operator-only release | PASS | Operator-reported exact-path helper release plus independent inspection of all 17 restored paths and no holders. |
| Freshness guard after release | PASS | Injected clock at the inclusive 45-minute cutoff rejected the write; fresh verified coverage then allowed one candidate-only CLI write. |
| Production hold, backup/restore, timers, and cutover | NOT TESTED / NO-GO | No live service, production database/recovery record, NAS payload, mount/ACL, timer, or other repository was changed or tested. |

After all fixture writers exited, the exact run-owned roots
`/tmp/repoctl-m010023.1qwFGEt5TR-hold` and
`/tmp/repoctl-m010023.1qwFGEt5TR` were removed; no fixture roots remained.
Before any production SQLite write, the separately authorized gates remain:
source freeze and import parity, staged authority binding, a verified
production NAS backup and separate-path restore, installed timer/monitor
operation with journal evidence, and explicit Product Owner GO. Keep live
cutover **NO-GO** until those gates are independently authorized and passed.

## M010.024 production staging preparation — not yet executed

This section records the prepared staging plan, not completed production
evidence. The candidate tree, staging/restore directories, staging environment,
and staging systemd units were absent at the latest read-only check. No live
service was stopped or restarted; no source freeze, import, candidate binding,
hold, NAS write, restore, or timer installation/firing was performed. The
maintenance-window actions below require explicit operator confirmation.

Implementation checks to date: 82 focused tests passed across web, workflow
status/commit, and managed-authority suites; all 10 backup tests passed. The
staging environment example parsed and its candidate paths/bindings matched
the reviewed configuration. The web CLI exposes `--scanner-cache-root`,
`git diff --check` passed, and `systemd-analyze verify` plus both timer
calendar checks succeeded. `systemd-analyze verify` emitted only the existing
unrelated Synology `/var/run` PIDFile warning. These checks do not establish
production import, hold, NAS, restore, or timer-operation acceptance.

### Starting authority and exact inputs

The pinned interpreter reports CPython 3.12.16 and SQLite 3.51.3. The current
`repo-control.service` is active as PID 1022213, started 2026-11-13, with
`/home/chuck/projects/repo-control-ii/.venv/bin/repoctl web` and no explicit
state-root override. Its default source authority is therefore
`/home/chuck/.local/share/repoctl`. The running service predates the
freeze-aware changes; recheck it in the agreed window and have the operator
stop it before freezing if it remains non-cooperating. Resume the unchanged
file-backed service promptly after parity/isolated activation or on abort.

The approved registry contains Repo Control II, Vocab App, and Photo
Organizer. Their source selections currently resolve to M010.024, M002.007,
and M001.004 respectively. Recheck all hashes immediately before import:

| Input | SHA-256 |
|---|---|
| `ops/repositories.toml` | `8a3899debf3b494ff5840dbdf145d20c2a847bd18b45762e22a1589e0919119e` |
| Repo Control II selection | `cf99fc6d899c10948fb755bcb749095452adcc0a5b55208409441802839c3182` |
| Vocab App selection | `2ca05370af4e14b1328f212271e54974feb624a9e83b81830618137650002c87` |
| Photo Organizer selection | `13baaf0d2e55ac67fa7f0219a426c882d1a07e369f019c7884da1ef6a858b76e` |
| Repo Control II dispatch ledger | `2240a0871825a90ad37cb67f3b0d4786e27fef75e4ddac2fa4947a8bfa8b2f48` |
| Vocab App dispatch ledger | `357ceae34931de5cf2058f01205c419c779c2ee3789f9fd77dd0af0b972aaac6` |
| Photo Organizer dispatch ledger | `1a51e59c0d374e557f2fbfdba4005dfde0dfe06db6672c69bda64e936e0491b5` |

Repo Control II has no approved Architect dispatches at this snapshot. The
dispatch ledgers were fingerprinted, not opened or disclosed. Any changed
registry, selection, dispatch, repository inventory, service binding, or source
fingerprint at window start is a stop-and-review condition, not an instruction
to proceed with stale evidence.

The NAS mount currently resolves to
`//192.168.1.171/ProjectBackups` at `/mnt/nas/repo-control` as CIFS, with the
previously accepted `repoctl-backup` principal and destination subtree
`/mnt/nas/repo-control/RepoControl/repo-control-ii`. No concrete mount/layout
change was observed; reuse the accepted ACL evidence and do not repeat the
sentinel or inspect protected configuration absent evidence of a change.

### Isolated staging paths and definitions

Use only these distinct paths:

| Purpose | Path |
|---|---|
| Candidate base | `/var/lib/repo-control-staging/m010.024` |
| Prepared database | `/var/lib/repo-control-staging/m010.024/prepared/managed-prepared.sqlite3` |
| Candidate authority/state root | `/var/lib/repo-control-staging/m010.024/state` |
| Browser scanner/status/context cache | `/home/chuck/.cache/repoctl/m010.024/scanner` |
| Freeze/import/plan reports | `/home/chuck/.local/state/repo-control-staging/m010.024` |
| Backup staging and recovery-point record | `/home/chuck/.local/state/repo-control-backup/staging/m010.024` |
| Candidate recovery-point file | `/home/chuck/.local/state/repo-control-backup/staging/m010.024/.repoctl-verified-recovery-point.json` |
| Separate restore root | `/home/chuck/.local/state/repo-control-backup/restore/m010.024` |
| NAS destination | `/mnt/nas/repo-control/RepoControl/repo-control-ii` |
| Installed staging environment | `/etc/repo-control/backup-m010024-staging.env` |

The staging environment example contains no credentials and explicitly binds
the backup source, candidate authority root, coverage path, restore root, and
approved NAS subtree. The four reviewed units are
`repo-control-backup-m010024-staging.service/.timer` and
`repo-control-backup-monitor-m010024-staging.service/.timer`; they run as
`chuck`, target only the candidate, and do not reference or control the live
browser service. The backup interval is 15 minutes; the independent monitor
interval is 5 minutes with a 1200-second stale-coverage threshold.

### Window import and activation sequence

The following exact-path command sequence is only for the operator-confirmed
window. Recheck the listed source fingerprints, repository set, active service
binding, and absence of all candidate/report paths before creating anything.
Have the operator freeze external writers and stop the old browser service if
it remains non-cooperating. Create the root-controlled staging parent as the
operator; then create the candidate base and private report directory as
`chuck`:

```sh
set -eu
test ! -e /var/lib/repo-control-staging
test ! -e /home/chuck/.local/state/repo-control-staging/m010.024
sudo install -d -o root -g chuck -m 0750 /var/lib/repo-control-staging
sudo install -d -o chuck -g chuck -m 0700 /var/lib/repo-control-staging/m010.024
install -d -m 0700 /home/chuck/.local/state/repo-control-staging/m010.024
```

Capture the freeze and import reports outside the candidate tree. Do not pass
`--writers-quiesced` until every cooperating writer is frozen and the older
browser has actually stopped:

```sh
set -eu
PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
REPO=/home/chuck/projects/repo-control-ii
REGISTRY="$REPO/ops/repositories.toml"
SOURCE_STATE_ROOT=/home/chuck/.local/share/repoctl
BASE=/var/lib/repo-control-staging/m010.024
STATE="$BASE/state"
IMPORT_DIR="$BASE/prepared"
REPORTS=/home/chuck/.local/state/repo-control-staging/m010.024
FREEZE_REPORT="$REPORTS/freeze-report.json"
IMPORT_REPORT="$REPORTS/import-report.json"
PLAN_REPORT="$REPORTS/activation-plan.json"

test ! -e "$STATE"
test ! -e "$IMPORT_DIR"
install -d -m 0700 "$STATE"
install -d -m 0700 "$IMPORT_DIR"
"$PY" -m repoctl.cli store-authority freeze-writers \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" > "$FREEZE_REPORT"
FREEZE_ID=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["freeze_id"])' "$FREEZE_REPORT")

"$PY" -m repoctl.cli store-authority prepare \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" \
  --candidate-state-root "$STATE" \
  --destination "$IMPORT_DIR" \
  --writers-quiesced > "$IMPORT_REPORT"
DATABASE=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_database_path"])' "$IMPORT_REPORT")
test "$DATABASE" = "$IMPORT_DIR/managed-prepared.sqlite3"
```

Review the import counts, refusal/mismatch totals, source fingerprints, exact
selection bindings, and representative hashes. The prepared bindings must
match only Repo Control II M010.024, Vocab App M002.007, and Photo Organizer
M001.004. Do not bind the live source root or alter global selection. Only
after the Product Owner confirms these exact entries, bind each reported
selection:

```sh
set -eu
BINDING_COUNT=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"]))' "$IMPORT_REPORT")
test "$BINDING_COUNT" = 3
for INDEX in $("$PY" -c 'import json,sys; print(*range(len(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"])))' "$IMPORT_REPORT"); do
  BINDING_JSON=$("$PY" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"][int(sys.argv[2])], separators=(",", ":")))' "$IMPORT_REPORT" "$INDEX")
  "$PY" -m repoctl.cli store-authority bind-selection \
    --registry "$REGISTRY" \
    --state-root "$STATE" \
    --database "$DATABASE" \
    --binding-json "$BINDING_JSON" \
    --po-confirm
done

# After every binding succeeds, generate one unique generation and preview.
GENERATION=$("$PY" -c 'import secrets; print(secrets.token_hex(16))')
"$PY" -m repoctl.cli store-authority preview \
  --registry "$REGISTRY" \
  --state-root "$STATE" \
  --source-state-root "$SOURCE_STATE_ROOT" \
  --database "$DATABASE" \
  --generation "$GENERATION" > "$PLAN_REPORT"
PLAN_SHA256=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["plan_sha256"])' "$PLAN_REPORT")
```

Activate only after independent parity review and the explicit Product Owner
confirmation required by the command. Do not start candidate application
writers; the operator hold must be applied and verified first:

```sh
set -eu
"$PY" -m repoctl.cli store-authority activate \
  --registry "$REGISTRY" \
  --state-root "$STATE" \
  --source-state-root "$SOURCE_STATE_ROOT" \
  --database "$DATABASE" \
  --generation "$GENERATION" \
  --expected-plan-sha256 "$PLAN_SHA256" \
  --po-confirm \
  --writers-quiesced \
  --source-parity-confirmed

"$PY" -m repoctl.cli store-authority release-writers \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" \
  --freeze-id "$FREEZE_ID"
```

Verify the source freeze is released and promptly resume the unchanged
file-backed service if it was stopped. Confirm its health still reports
`legacy_files` before proceeding to the operator hold. Do not keep source
writers frozen for the backup, restore, or timer observations.

### Operator-controlled hold procedure

Do not create the candidate or apply this hold before the operator confirms
the maintenance window. The parent `/var/lib/repo-control-staging` was absent
at the pre-window check; during the confirmed window, the operator created it
as `root:chuck 0750`, allowing `chuck` to traverse but not replace entries.
The operator must also create the candidate base as `chuck:chuck 0700` with
`sudo install -d -o chuck -g chuck -m 0700
/var/lib/repo-control-staging/m010.024`. The Coder creates only the `prepared`
and `state` children beneath that base so import and activation can create
their exact files. The operator changes the candidate base to `root:chuck
0750` only when applying the hold. Keep scanner cache,
`.locks`, backup coverage/staging, and restore paths outside the protected
candidate tree.

After import, initialize and verify the candidate `.locks` directory and
`state-artifacts.lock` as `chuck:chuck 0700/0600` before applying the hold.
Inspect the database journal mode and exact DB/WAL/SHM sidecars. If SQLite
requires a SHM file, let a normal pre-hold SQLite open create/initialize it;
never fabricate an empty sidecar. Stop all candidate processes and verify no
protected-file holders before changing ownership or modes.

Managed import creates one empty per-repository scratch directory under
`prepared`, named from the first 24 hex characters of the repository ID's
SHA-256. Verify these three directories are empty and include them in the
protected inventory; do not apply recursive permission changes.

Set these exact paths from the verified post-import inventory:

```sh
set -eu
BASE=/var/lib/repo-control-staging/m010.024
STATE="$BASE/state"
RC_IMPORT_SCRATCH="$BASE/prepared/61047c89b60f61a85d61ba3a"
VOCAB_IMPORT_SCRATCH="$BASE/prepared/c07984ec043b5029f6d87480"
PHOTO_IMPORT_SCRATCH="$BASE/prepared/1b12de3f56fa83f1657f41f5"
DB="$BASE/prepared/managed-prepared.sqlite3"
GUARD="$BASE/.state.information-store-authority.guard"
CONFIG="$STATE/information-store-authority.json"
RC_SELECTION="$STATE/repo-control-ii--945e7f88eafb/milestones/selection.json"
VOCAB_SELECTION="$STATE/vocab-app--0d81efec80ef/milestones/selection.json"
PHOTO_SELECTION="$STATE/photo-organizer-dev--84c64acdea9f/milestones/selection.json"
LOCK_DIR="$STATE/.locks"
LOCK_FILE="$LOCK_DIR/state-artifacts.lock"
```

Before any privileged change, the operator runs the exact path and inventory
checks below and reviews every output:

```sh
set -eu
namei -l "$BASE"
namei -l "$STATE"
namei -l "$DB"
namei -l "$GUARD"
namei -l "$CONFIG"
find "$BASE" -xdev -type l -print
find "$BASE" -xdev -printf '%p|%y|%u:%g|%m|%d:%i\n' | sort
stat -c '%n|%F|%U:%G|%a|%d:%i' \
  "$BASE" "$BASE/prepared" "$RC_IMPORT_SCRATCH" \
  "$VOCAB_IMPORT_SCRATCH" "$PHOTO_IMPORT_SCRATCH" \
  "$STATE" "$DB" "$DB-shm" \
  "$GUARD" "$CONFIG" "$RC_SELECTION" "$VOCAB_SELECTION" \
  "$PHOTO_SELECTION" "$LOCK_DIR" "$LOCK_FILE"
sha256sum "$DB" "$DB-wal" "$GUARD" "$CONFIG" \
  "$RC_SELECTION" "$VOCAB_SELECTION" "$PHOTO_SELECTION"
```

If `$DB-wal` exists, include it in `stat`, the before/after hash record, and
the holder check; omit it from the commands only when the reviewed inventory
confirms it is absent. Check each existing protected DB/sidecar, guard,
configuration, selection, and lock path for open holders:

```sh
fuser_status=0
sudo fuser -v "$DB" "$DB-wal" "$DB-shm" "$GUARD" "$CONFIG" \
  "$RC_SELECTION" "$VOCAB_SELECTION" "$PHOTO_SELECTION" "$LOCK_FILE" \
  || fuser_status=$?
if test "$fuser_status" -eq 0; then
  echo "STOP: a protected candidate path has an open holder" >&2
  exit 1
fi
if test "$fuser_status" -ne 1; then
  echo "STOP: fuser failed with status $fuser_status" >&2
  exit "$fuser_status"
fi
```

Status 1 with no output means no listed users; any PID or diagnostic is a
stop. Include `$DB-wal` here only when it exists, as confirmed by the reviewed
inventory.
Stop if a path is missing or unexpected, a symlink exists, the parent is not
root-controlled, any holder remains, or the inventory differs from the
reviewed import. Do not use recursive ownership or mode changes.

Apply only to the exact reviewed paths:

| Paths | Held owner/mode |
|---|---|
| Candidate base, `prepared`, the three empty per-repository scratch directories, `state`, each repository directory and each `milestones` directory | `root:chuck 0750` |
| Guard, authority configuration, all three selection files, database, and existing WAL | `root:chuck 0440` |
| Existing SQLite SHM only | `root:chuck 0660` |
| `.locks` and `state-artifacts.lock` | `chuck:chuck 0700` and `chuck:chuck 0600` |

After confirming the exact inventory and no open holders, the operator uses
these explicit changes (omit the WAL commands only when the reviewed inventory
confirms that no WAL exists; SHM must exist for the reviewed held deployment):

```sh
set -eu
sudo chown root:chuck -- \
  "$BASE" "$BASE/prepared" "$RC_IMPORT_SCRATCH" \
  "$VOCAB_IMPORT_SCRATCH" "$PHOTO_IMPORT_SCRATCH" "$STATE" \
  "$STATE/repo-control-ii--945e7f88eafb" \
  "$STATE/repo-control-ii--945e7f88eafb/milestones" \
  "$STATE/vocab-app--0d81efec80ef" \
  "$STATE/vocab-app--0d81efec80ef/milestones" \
  "$STATE/photo-organizer-dev--84c64acdea9f" \
  "$STATE/photo-organizer-dev--84c64acdea9f/milestones"
sudo chmod 0750 -- \
  "$BASE" "$BASE/prepared" "$RC_IMPORT_SCRATCH" \
  "$VOCAB_IMPORT_SCRATCH" "$PHOTO_IMPORT_SCRATCH" "$STATE" \
  "$STATE/repo-control-ii--945e7f88eafb" \
  "$STATE/repo-control-ii--945e7f88eafb/milestones" \
  "$STATE/vocab-app--0d81efec80ef" \
  "$STATE/vocab-app--0d81efec80ef/milestones" \
  "$STATE/photo-organizer-dev--84c64acdea9f" \
  "$STATE/photo-organizer-dev--84c64acdea9f/milestones"
sudo chown root:chuck -- \
  "$GUARD" "$CONFIG" "$RC_SELECTION" "$VOCAB_SELECTION" \
  "$PHOTO_SELECTION" "$DB"
sudo chmod 0440 -- \
  "$GUARD" "$CONFIG" "$RC_SELECTION" "$VOCAB_SELECTION" \
  "$PHOTO_SELECTION" "$DB"
sudo chown root:chuck -- "$DB-shm"
sudo chmod 0660 -- "$DB-shm"
```

If `$DB-wal` exists, apply `sudo chown root:chuck -- "$DB-wal"` and
`sudo chmod 0440 -- "$DB-wal"` as two exact-path operations. Do not apply any
recursive operation or same-owner `chmod` workaround. Recheck the full
inventory after these commands and verify the `.locks` owner/modes were
preserved.

Leave scanner cache, coverage/staging, restore, and NAS destination ownership
and modes independently writable by the intended `chuck` processes. Verify
the resulting exact inventory and, as `chuck`, confirm protected directories,
DB/WAL, guard/configuration, and selection pointers are not writable; SHM and
`.locks` are writable:

```sh
set -eu
sudo -u chuck -- test ! -w "$BASE"
sudo -u chuck -- test ! -w "$STATE"
sudo -u chuck -- test ! -w "$DB"
sudo -u chuck -- test ! -w "$GUARD"
sudo -u chuck -- test ! -w "$CONFIG"
sudo -u chuck -- test ! -w "$RC_SELECTION"
sudo -u chuck -- test ! -w "$VOCAB_SELECTION"
sudo -u chuck -- test ! -w "$PHOTO_SELECTION"
sudo -u chuck -- test -w "$DB-shm"
sudo -u chuck -- test -w "$LOCK_DIR"
sudo -u chuck -- test -w "$LOCK_FILE"
```

Also verify the unchanged hashes for DB/WAL, guard/configuration, and
selections after the browser restart. No candidate process starts until the
initial permissions and hashes pass.
Then run the candidate browser with the production-shaped command and verify
`/healthz`, candidate identity, and an independent read-only Coder CLI query:

```sh
set -eu
PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
REPO=/home/chuck/projects/repo-control-ii
REGISTRY="$REPO/ops/repositories.toml"
SCANNER_CACHE_ROOT=/home/chuck/.cache/repoctl/m010.024/scanner
COVERAGE_PATH=/home/chuck/.local/state/repo-control-backup/staging/m010.024/.repoctl-verified-recovery-point.json
REPOCTL_BACKUP_COVERAGE_PATH="$COVERAGE_PATH" \
  "$PY" -m repoctl.cli web \
    --repository-registry "$REGISTRY" \
    --host 127.0.0.1 \
    --port 18766 \
    --state-root "$STATE" \
    --scanner-cache-root "$SCANNER_CACHE_ROOT"

# In a second terminal, while the browser is running:
curl -fsS http://127.0.0.1:18766/healthz
REPOCTL_BACKUP_COVERAGE_PATH="$COVERAGE_PATH" \
  "$PY" -m repoctl.cli coder-status \
    --repository "$REPO" \
    --state-root "$STATE" \
    --format json
```

Stop and restart the browser once under the same held paths, then repeat the
checks and permission/hash inspection. The known browser write-denial response
remains a generic HTTP 500; do not attempt a production-candidate write.

If any privileged command partially fails, do not start candidate services,
backup, or timers; do not recursively repair or release the hold. Preserve the
candidate and exact before/after inventory, keep the candidate offline, and
have the operator resolve the specific path discrepancy. Independently
release the source freeze by its exact ID with an explicit abort reason when
source import/parity work has stopped:

```sh
set -eu
"$PY" -m repoctl.cli store-authority release-writers \
  --registry "$REGISTRY" \
  --state-root /home/chuck/.local/share/repoctl \
  --freeze-id "$FREEZE_ID" \
  --abort-reason "M010.024 staging aborted; file-backed authority remains active."
```

Then resume and verify the unchanged file-backed service. If a candidate was
activated but the operator hold has not yet been applied and no managed write
occurred, stop and verify all candidate writers, then use the exact reviewed
plan with explicit confirmation:

```sh
set -eu
"$PY" -m repoctl.cli store-authority abort \
  --registry "$REGISTRY" \
  --state-root "$STATE" \
  --source-state-root /home/chuck/.local/share/repoctl \
  --database "$DB" \
  --generation "$GENERATION" \
  --expected-plan-sha256 "$PLAN_SHA256" \
  --po-confirm \
  --writers-quiesced
```

This abort is not permitted after the production hold has been applied; the
hold remains in place for this milestone. If any managed write occurred, do
not abort or switch back to files; preserve DB/WAL/SHM and verified backups
and use separately approved forward recovery.

### Window sequence and remaining evidence

The operator handoff must confirm the window, freeze external editors and
non-cooperating writers, and stop `repo-control.service` if it is still the
older non-cooperating process. The Coder then freezes the source, captures the
fingerprinted inventory, imports all three registered sources, verifies
counts/hashes/drift and the exact plan hash, binds/activates only the separate
candidate root, and releases the source freeze promptly. The original service
must be restarted unchanged and verified `legacy_files` before any candidate
backup/timer observation; do not keep the live source frozen for those checks.
Do not rely on `--writers-quiesced` without independent evidence that every
writer is stopped or cooperating.

Create the private scanner-cache directory before starting the candidate
browser. Create the separate backup-staging and restore directories before
the first backup, only when absent; if any already exist, inspect their exact
owner, mode, and contents rather than overwriting or removing them. The
candidate browser/CLI identity checks must pass before installing staging
backup units. Confirm the destination environment and unit files do not
already exist. If any are present, stop rather than overwrite them. Install
only the staging environment and four reviewed M010.024 units; do not
overwrite `backup.env` or any live unit:

```sh
set -eu
test ! -e /home/chuck/.local/state/repo-control-backup/staging/m010.024
test ! -e /home/chuck/.local/state/repo-control-backup/restore/m010.024
test ! -e /etc/repo-control/backup-m010024-staging.env
test ! -e /etc/systemd/system/repo-control-backup-m010024-staging.service
test ! -e /etc/systemd/system/repo-control-backup-m010024-staging.timer
test ! -e /etc/systemd/system/repo-control-backup-monitor-m010024-staging.service
test ! -e /etc/systemd/system/repo-control-backup-monitor-m010024-staging.timer
install -d -m 0700 /home/chuck/.local/state/repo-control-backup/staging/m010.024
install -d -m 0700 /home/chuck/.local/state/repo-control-backup/restore/m010.024
sudo install -o root -g chuck -m 0640 \
  ops/backup.m010024-staging.env.example \
  /etc/repo-control/backup-m010024-staging.env
sudo install -o root -g root -m 0644 \
  ops/systemd/repo-control-backup-m010024-staging.service \
  /etc/systemd/system/repo-control-backup-m010024-staging.service
sudo install -o root -g root -m 0644 \
  ops/systemd/repo-control-backup-m010024-staging.timer \
  /etc/systemd/system/repo-control-backup-m010024-staging.timer
sudo install -o root -g root -m 0644 \
  ops/systemd/repo-control-backup-monitor-m010024-staging.service \
  /etc/systemd/system/repo-control-backup-monitor-m010024-staging.service
sudo install -o root -g root -m 0644 \
  ops/systemd/repo-control-backup-monitor-m010024-staging.timer \
  /etc/systemd/system/repo-control-backup-monitor-m010024-staging.timer
sudo systemctl daemon-reload
sudo systemctl start repo-control-backup-m010024-staging.service
```

Verify the first candidate-bound NAS backup and restore its exact manifest to
the distinct local restore root before enabling timers. The staging environment
contains paths and identity only and is group-readable by `chuck`; use it for
the restore command, never the protected production `backup.env`:

```sh
set -eu
set -a
. /etc/repo-control/backup-m010024-staging.env
set +a
PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
printf 'Enter the exact verified M010.024 manifest path: '
read -r MANIFEST
test -f "$MANIFEST"
"$PY" -m repoctl.backup restore \
  --manifest "$MANIFEST" \
  --restore-name m010024-separate-restore.sqlite3
```

After restore validation, enable the timers and capture actual firings and
journals:

```sh
set -eu
sudo systemctl enable --now \
  repo-control-backup-m010024-staging.timer \
  repo-control-backup-monitor-m010024-staging.timer
systemctl list-timers --all \
  repo-control-backup-m010024-staging.timer \
  repo-control-backup-monitor-m010024-staging.timer
printf 'Enter the UTC maintenance-window start time for journal review: '
read -r WINDOW_START
journalctl -u repo-control-backup-m010024-staging.service \
  -u repo-control-backup-monitor-m010024-staging.service --since "$WINDOW_START"
```

Then prove actual 15-minute backup and independent 5-minute monitor outcomes,
including the bounded 20-minute warning/recovery check; calendar syntax alone
is not evidence. Record timer state, journals, coverage binding, restore
duration, and the inclusive 45-minute freshness guard. Stop the staging-only
timers after validation:

```sh
set -eu
sudo systemctl disable --now \
  repo-control-backup-m010024-staging.timer \
  repo-control-backup-monitor-m010024-staging.timer
sudo systemctl stop \
  repo-control-backup-m010024-staging.service \
  repo-control-backup-monitor-m010024-staging.service
systemctl list-timers --all \
  repo-control-backup-m010024-staging.timer \
  repo-control-backup-monitor-m010024-staging.timer
```

If the run aborts, stop the candidate browser in its owning terminal, disable
and stop only these staging timers/services, retain the candidate hold, release
only the exact source freeze ID with an explicit abort reason, and verify the
original file-backed service is healthy. If the original service was stopped,
the operator resumes the unchanged unit with
`sudo systemctl start repo-control.service` and verifies its health and
`legacy_files` binding. Never touch `repo-control.service` through the staging
unit files.

#### M010.024 window result (2026-11-16)

- **PASS — source freeze/import/parity and isolated activation.** The active
  freeze ID was `4b36cde0b6b66a8db55696eb8779907f`. Import recorded 962
  documents, 118 milestones, 1,591 events, 452 imported sources, and 161
  dispatches with zero mismatches/refusals. Source inventory hash:
  `2f22474513008fb2ca7faf9b68ea6fc80b52aed38e12124844346d1b57379846`.
  Activation plan hash:
  `fd1ea05211d86cd782e39c880829dd749e62277d98b585162f13a4bab6714b5e`.
  Candidate generation: `863b2270fa4149b084746dd845fa7bf0`.
- **PASS — live recovery after the staging snapshot.** The exact source freeze
  was released after activation. The unchanged `repo-control.service` was
  resumed and `/healthz` reported `legacy_files` with no InformationStore
  binding.
- **PASS — candidate binding and operator hold.** The candidate uses only
  `/var/lib/repo-control-staging/m010.024/state` and
  `/var/lib/repo-control-staging/m010.024/prepared/managed-prepared.sqlite3`.
  The operator's exact-path hold passed independent ownership, mode,
  read-only-hash, SQLite integrity, and foreign-key checks. The candidate
  browser returned HTTP 200 and reported the expected `sqlite_managed`
  generation and state root.
- **FAIL — independent candidate Coder CLI.** The read-only
  `coder-status --state-root /var/lib/repo-control-staging/m010.024/state`
  failed with `InformationStore has no approved selected prompt revision`.
  The imported selected prompt has a draft revision but no approved revision,
  and the candidate has no publication or revision-decision rows. This blocks
  the required CLI identity gate. Bug `13536637375043a7a4042023493b5a90` was
  classified out of scope for M010.024; the approval/import fix is deferred
  to a focused follow-up milestone and is required before cutover. Do not
  manually approve the candidate prompt or bypass the CLI check. The candidate
  browser was stopped after this failure. Its hold remains applied; no
  candidate application write occurred.
- **NOT TESTED — NAS backup, separate restore, installed backup/monitor
  timers, and warning/recovery.** These steps were not started after the
  candidate CLI gate failed. The staging environment and units remain
  uninstalled.

The candidate was imported before the later file-backed bug/test events, so
it is not a final cutover copy. Before any separately authorized cutover,
freeze writers again, refresh/re-import as required, verify exact final
source/selection/dispatch parity, and create a fresh candidate-bound backup.
Keep live-cutover status **NO-GO** until the candidate CLI blocker is
classified and resolved and all remaining backup/restore/timer evidence
passes. Do not release the candidate hold or make a live SQLite application
write under M010.024.

#### M010.025 prompt reconciliation for a future fresh candidate

The M010.024 result above is historical and unchanged. Its candidate remains
held and stopped; do not confirm, repair, rebuild, or restart it. For a
separately authorized fresh candidate, the old `prompt_approved` lifecycle
event does not identify the approved revision, and a later Coder hash does not
prove which bytes the Product Owner approved. Keep the imported prompt
unapproved until this explicit reconciliation:

1. Before the initial candidate backup, verify exact source parity, repository
   identity, candidate state-root binding, selected milestone/path, and the
   imported prompt revision ID and SHA-256 against its source bytes. In the
   candidate-bound browser, review the exact prompt shown in the unverified
   prompt panel. If any identity or byte check is missing or mismatched, stop.
2. Create a verified candidate-bound recovery point while the prompt remains
   unapproved. The explicit confirmation is a managed write and requires valid
   recovery-point evidence. It must not be performed against a read-only
   operator-held candidate; only a separately authorized fresh staging
   procedure may permit this candidate-local confirmation.
3. The Product Owner explicitly confirms that exact revision in the browser's
   **Confirm exact imported prompt revision** action. This creates a new,
   candidate-local Product Owner revision decision bound to the repository,
   milestone, document, revision, and imported source bytes. It does not claim
   to recover the historical approval time and does not add or rewrite a
   workflow lifecycle event. Missing, stale, changed, or mismatched identity
   must stop the run; do not bypass the confirmation.
4. Launch a fresh Coder CLI process against only the candidate root and verify
   its JSON reports `sqlite_managed`, the expected repository/milestone/
   selection, and the same prompt revision and SHA-256. A failure is NO-GO;
   do not fall back to the source selection or manually alter approval rows.
5. Before enabling candidate writes or cutover, publish a fresh verified
   backup that includes the confirmation decision, restore that copy to a
   separate path and verify integrity/foreign keys, and complete the
   separately authorized backup/monitor timer checks. These remain independent
   gates and are not established by prompt confirmation.

M010.025 validates this explicit one-time reconciliation on disposable data;
it does not authorize another production staging attempt or maintenance
window. Approval-time revision/hash capture may be proposed as a separate
follow-up, with no bulk historical backfill.

#### M010.026 exact-prompt approval dependency for a future fresh candidate

M010.026 adds optional `prompt_approval` evidence to newly written
file-backed `prompt_approved` events. The evidence binds repository key/ID,
milestone, canonical prompt path, and the SHA-256 of the exact raw prompt bytes
revalidated at the lifecycle write boundary. The file-backed workflow has no
separate immutable document revision ID; this digest identifies the reviewed
bytes. The managed import creates its own immutable revision and binds the
source event to that exact revision. The evidence remains a reported
Product Owner action, not actor authentication.

The shortest next staging sequence is:

1. **Obtain separate deployment and staging authorization.** Before the
   Product Owner reviews or approves a future staging prompt, deploy the
   approved code containing [the file-backed approval writer](../../src/repoctl/workflow/lifecycle.py),
   [both browser approval routes](../../src/repoctl/web/app.py), and the
   [managed importer/parity checks](../../src/repoctl/information_store.py)
   and [source-parity verifier](../../src/repoctl/migration_rehearsal.py).
   Use the pinned CPython 3.12.16 / SQLite 3.51.3 runtime for the service and
   importer. Confirm `/healthz` reports `legacy_files` before candidate
   preparation and advertises
   `runtime_capabilities.workflow.prompt_approval_evidence_schema_version`
   as `1`. A missing capability means the running service cannot establish
   approval-time byte evidence; do not approve the staging prompt or infer
   support from a local checkout. M010.026 does not authorize deployment or
   restarting the active service.
2. **Prove the source approval before any downtime is scheduled.** Inspect the
   new sidecar `prompt_approved` event and confirm its single `prompt_approval`
   object matches the selected repository key/ID, milestone, canonical prompt
   path, and `sha256sum` of the exact prompt file. Do not substitute a later
   Coder hash, timestamp, filename, lifecycle phase, or manually entered
   digest. Any missing, malformed, duplicate, stale, or mismatched evidence
   stays lifecycle-only and must not be treated as an imported approval.
3. **Run the separately authorized fresh-candidate rehearsal and proof gates.**
   Before production scheduling, use the real importer on disposable data,
   verify exact source parity, and preview/activate a separate candidate root.
   The prepared approval must have one
   `v1_imported_prompt_approval` decision and exact event-to-revision link;
   the document and milestone pointers must name the same imported revision.
   Verify original workflow chronology and source bytes are unchanged,
   `PRAGMA integrity_check` is `ok`, `PRAGMA foreign_key_check` is empty, and
   two fresh independent `coder-status` processes report the exact repository,
   milestone, selection, revision, and digest. An exact-source retry must not
   duplicate the approval decision or link.

If the source history predates this evidence, including the M010.024 B1 event,
or if its bytes/scope do not match, the importer must leave the prompt
unapproved. Only on a separately authorized fresh candidate, follow the
M010.025 one-time exact-revision confirmation: verify the candidate-bound
revision and source parity, create its valid recovery point while still
unapproved, then explicitly confirm that revision. This confirmation is a new
candidate-local decision, not recovered historical approval. M010.026's own
earlier approval and all other old events remain unchanged and do not gain
digests retroactively.

The held `/var/lib/repo-control-staging/m010.024` candidate remains untouched.
Production backup/restore, installed timer verification, candidate writer
release, cutover, and any maintenance window remain separate NO-GO gates.

## M010.027 fresh SQLite staging and GO/NO-GO review

This section records the renewed M010.027 staging window and current hold
gate. After the Product Owner answered the Vocab App source question, the
operator confirmed a new start window, renewed the external-writer pause, and
reconfirmed the 30-minute interruption and 60-minute post-thaw bounds. The
live unit remained unchanged. A fresh source freeze was captured, imported,
bound, and activated only in the isolated M010.027 candidate; the exact freeze
was then released. The original service was never stopped or restarted and
remains active on `legacy_files`.

The candidate is activated but not held. No candidate browser or other
candidate application process has been started, and no M010.027 NAS backup,
restore, or timer operation has occurred. Pre-hold inspection found the
database in WAL mode with neither a WAL nor SHM sidecar. The documented
normal SQLite open and a read transaction both left those sidecars absent.
After the Product Owner's conditional approval and the
disposable proof below, the one-time metadata-only sidecar initialization was
run on the exact candidate. The helper reported final `user_version` 3, the
main-file hash remained unchanged, and WAL/SHM were created. After the first
read-only postflight checker failed on its missing SQL binding, the corrected
read-only check passed: application data and exact approval binding matched
the baseline, integrity was `ok`, and FK violations were zero. Per the
Product Owner's answer, no further candidate operation was run; the candidate
remains offline and unheld pending a separate hold decision. The existing
`/var/lib/repo-control-staging/m010.024` candidate remains outside this
procedure and must remain held, stopped, and unchanged.

### Disposable SQLite WAL-sidecar and backup/restore test

On a disposable copy, the pinned SQLite 3.51.3 C API probe enabled
`SQLITE_FCNTL_PERSIST_WAL` and
`SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`, then round-tripped `user_version`. After
close and read-only reopen, the main-file SHA-256 remained
`90c6f41981d60586d343c3d4b64f764be5fd76a40d91137c20bffb26be535e94`;
the nonempty WAL (4,152 bytes) and SHM (32,768 bytes) persisted. The
`user_version` (3), `schema_version` (31), all 15 non-internal table row
counts, and integrity/FK checks were unchanged (`ok`, zero violations).

The disposable backup copy exercised the local prepared-store backup path.
An initial offline attempt against an activated copy correctly failed closed:
activated stores cannot be opened as offline prepared stores. On the disposable
copy only, its four `authority_*` rows were removed from `store_metadata` to
make it an offline prepared-store fixture; application-table counts and
schema/user versions did not change. Under a same-owner mode simulation
(parent `0550`, DB/WAL `0440`, SHM `0660`), read-only open and integrity checks
passed, write-open of DB/WAL was denied, and SHM remained writable. This does
not prove the root-owned operator boundary.

The actual local `repoctl.backup.create_verified_backup` and
`restore_verified_backup` functions then produced and restored a separate
115,965,952-byte verified snapshot. The manifest database SHA-256 is
`4fec510fce5cb8f810b3c6f6e5ac7b6cfabee62f8a6687daa4a3ca2fa4f78e8c` and the
snapshot SHA-256 is `24a9c5d32ee9d5e662a46a1f3f2107e647890ff2a6a6c149fd83b5ee941f576f`.
The manifest and restored copy both passed integrity (`ok`) and FK (zero)
checks and retained the exact M010.027 selection r104, prompt revision
`bb1fe9fe9df5a6373c6a8b880dad8108`, and identical table row counts. The
disposable source DB and WAL hashes remained unchanged through backup/restore;
the 12,392-byte WAL and 32,768-byte SHM remained present.

The Product Owner also required proof that the `user_version` 3-to-4-to-3
round-trip is one transaction and that interruption between the changes
recovers to version 3. On two fresh disposable copies, the pinned C helper
confirmed autocommit stayed off through both changes and the commit copy
finished at version 3. A second process flushed version 4 inside the open
transaction and was interrupted before the 4-to-3 change; read-only recovery
returned version 3. Both copies retained the original main-file hash,
schema version, all 15 table digests, and exact M010.027 approval event,
revision, and selection; integrity was `ok` and FK violations were zero. The
committed copy retained a 4,152-byte WAL and 32,768-byte SHM. After recovery,
the interrupted copy had a zero-byte WAL and 32,768-byte SHM.

These disposable tests did not use NAS, active-authority coverage/recovery-point
publication, the staged candidate for backup/restore, a candidate process, or
timers. They do not clear the M010.027 hold gate.

### Approved candidate sidecar initialization and read-only postflight

The exact staged database is
`/var/lib/repo-control-staging/m010.027/prepared/managed-prepared.sqlite3`;
its pre-operation main-file SHA-256 was
`90c6f41981d60586d343c3d4b64f764be5fd76a40d91137c20bffb26be535e94`, with no
WAL or SHM sidecar. Immediately before the approved operation, the candidate
hash and absent sidecars were rechecked, `fuser` reported no DB holders, the
original service remained active on `legacy_files` (PID `408204`), and
candidate port `18767` was idle. The pinned SQLite 3.51.3 helper enabled
`SQLITE_FCNTL_PERSIST_WAL` and `SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE`, performed
the single-transaction 3-to-4-to-3 round-trip, and returned
`final_version=3`. The observed main-file hash remained
`90c6f41981d60586d343c3d4b64f764be5fd76a40d91137c20bffb26be535e94`; WAL
(4,152 bytes) and SHM (32,768 bytes) were created.

The first read-only checker stopped with
`sqlite3.ProgrammingError: Incorrect number of bindings supplied` because its
`workflow_document_link` query omitted a parameter. Following the blocker
answer, only the corrected read-only postflight was rerun. Against the
preserved disposable baseline it confirmed the unchanged main-file hash,
nonempty persistent WAL and SHM, `user_version` 3, `schema_version` 31,
identical row counts and table digests across all 15 non-internal tables, and
the exact M010.027 approval event `134022835ceb45d5a38ca9c383a7ade0`, revision
`bb1fe9fe9df5a6373c6a8b880dad8108`, and selection r104. Integrity was `ok`
and FK violations were zero.

No hold change, candidate application process, backup, NAS, restore, timer,
or cutover action occurred. The candidate remains offline and unheld. Stop
here as directed; a separate decision is required before applying the hold.

### Current gates

| Gate | Result | Evidence / next action |
|---|---|---|
| Exact M010.027 source approval | PASS | The exact-byte approval event is `134022835ceb45d5a38ca9c383a7ade0`; the approved prompt SHA-256 is `b6cb039a00b43aaa864ca8a18e3fdc7b3a7b03b287d4ab941b9f23bf682f1fe0`. The canonical prompt bytes were not changed. |
| Real importer and independent CLI, isolated | PASS | A disposable pinned-runtime import linked that event to exact revision `bb1fe9fe9df5a6373c6a8b880dad8108`, with one `v1_imported_prompt_approval`, zero mismatches/refusals, source parity, integrity/FK checks, exact retry, and two fresh independent `coder-status` successes. The disposable snapshot hash `1ca1461ce1d099826668a88e9737828f2674335f41942ba87f8373286dd3059d` is **not** a production freeze fingerprint and must not be reused. |
| Fresh registered-source freeze/import and parity | PASS | Freeze `834eb6870d81ab90bb08a2eda69c7b49` captured at `2026-11-17T05:29:31Z`; inventory SHA-256 `dcff46f803a14888ee1c3cffe642d5b788cd04bec42e30c9eb84b466e37f265a`, registry binding SHA-256 `84336c8139eba6031dae2b0f0ffebffeeef48d782216bffd99ad6590b07d1df2`. The fresh import contains 971 documents, 121 milestones, 1,641 events, 464 sources, and 164 dispatches, with zero mismatches/refusals. Source parity and exact M010.027 approval/revision linkage passed; SQLite integrity was `ok`, foreign-key violations were zero, and all registered worktrees matched the captured baseline after import. |
| Candidate selection binding and isolated activation | PASS | Only the three reported selections were bound: Repo Control II M010.027 r104, Vocab App M002.007 r15, and Photo Organizer M001.004 r5. Generation `2e329ee9e60c16591a1b4afd1d1bafba` was re-previewed under the active freeze with the Product Owner-confirmed plan SHA-256 `b248e981490a3e7385debc74cef7b2cf8fef3967d295009735e9464c2c6e0ad5`; candidate database identity is `2bff56f7c5d61d0c3680f55bdc7f02586ae98389f154f7229d3a2f184ead1033`. Activation and exact freeze release both succeeded. |
| Original service after source thaw | PASS | `repo-control.service` remained active with PID `408204`; `/healthz` reported `legacy_files`, null InformationStore binding, and prompt-approval evidence schema 1. Candidate browser port `18767` had no listener. |
| Negative and compatibility cases | PASS | The accepted [M010.026 closeout](../milestones/M010.026_capture_exact_prompt_approval_at_approve_start_closeout.md) records 17 targeted pinned-runtime tests covering missing, malformed, conflicting, wrong-scope, stale/changed-byte, invented-approval, retry, and legacy-confirmation cases. Not rerun: M010.027 changed documentation and staging proposals, not application behavior. |
| Candidate/import/backup runtime | PASS | [ops/verify_sqlite_runtime.py](../../ops/verify_sqlite_runtime.py) reports production eligible: CPython 3.12.16 / SQLite 3.51.3. |
| Running service authority and capability | PASS | The active service is file-backed: `/healthz` reports `legacy_files`, no InformationStore binding, and `prompt_approval_evidence_schema_version: 1`. The process imports the repository source tree at the reviewed starting HEAD, but the health response has no build hash. |
| Effective service runtime vs. reviewed unit | ATTENTION | The running service uses the repository `.venv` (Python 3.12.3 / SQLite 3.45.1) and has no `EnvironmentFile`; tracked [ops/systemd/repo-control.service](../../ops/systemd/repo-control.service) uses the pinned Python and `/etc/repo-control/backup.env`. The operator approved leaving the current unit unchanged for this file-backed staging; candidate/import/backup work uses the pinned runtime. Do not change or restart the live unit independently. Before any separately authorized managed-service write, reconcile and verify the installed unit/runtime and coverage environment in a separate deployment step. |
| Post-snapshot forward recovery | PASS in disposable fixture; production NOT TESTED | [M010.021 closeout](../milestones/M010.021_staged_sqlite_deployment_validation_and_cutover_go_review_closeout.md) records one committed post-snapshot event preserved with failed-state DB/WAL/SHM, omitted from a separate earlier restore, then replayed through the guarded transaction API after fresh coverage; final events, projection, and payload hash matched. This does not cover production/NAS recovery, loss of both DB and WAL, or a zero-loss/RTO guarantee. |
| Operator-controlled candidate hold and candidate browser/CLI identity | NOT TESTED | The sidecar initialization and corrected read-only postflight passed, but the candidate remains offline and unheld. No root-owned hold or candidate browser/CLI identity has been exercised. The Product Owner's answer directed stopping before the hold; await a separate decision before proceeding. |
| Production NAS backup and separate restore | NOT TESTED | The local disposable verified-backup/restore functions passed, but no candidate-specific active-authority coverage, NAS backup, recovery-point record, or candidate restore was run. |
| Installed timer/monitor firing and warning/recovery | NOT TESTED | No M010.027 staging units or timers are installed or running. Syntax/calendar checks alone will not satisfy this gate. |

The disposable proof fingerprint is stale by design and must not be reused.
The production freeze fingerprint above is the exact source snapshot used by
this candidate. After thaw, the successful `RECOVERY` verification event
`9546c9a2b3e641cea9b4ca8fcbca9c13` and this guide update were recorded, so the
candidate is known to predate those source changes. Any later cutover review
must perform a fresh final drift/parity check and re-import or restage if the
source moved or the candidate binding changes. The selections listed above
are the actual imported and bound selections, not substitutes for that later
check.

Preflight Git snapshot: branch `db-workflow-persistence-architecture`, HEAD
`28e9a7366bf9ff5ca5a4734a210b44164f246de9`; the tracked worktree was clean,
with only the M010.027 prompt and workflow sidecar untracked. The live service
was PID `408204`, instance `0fda4763438cce37ccf4daebcf234db6`, user `chuck`,
working in `/home/chuck/projects/repo-control-ii`, using the default source
state root `/home/chuck/.local/share/repoctl`. Its imported module resolves
under `src/`; the process started after the reviewed HEAD, but no build hash is
embedded in health. The registered source paths are Repo Control II
`/home/chuck/projects/repo-control-ii`, Vocab App
`/home/chuck/projects/vocab-app`, and Photo Organizer
`/home/chuck/projects/photo-organizer-dev`. These are the expected paths to
revalidate against the live registry at window start.

The Product Owner resolved the Vocab App worktree question: its six modified
shared-procedure 1.4.0 files and untracked `AGENTS.md` are expected, may remain
uncommitted, and must remain untouched. In the next window, capture them as
they stand and stop if any registered-source input changes during capture or
import. Vocab App was on `main` at
`e41683bcda167e0acdc1f7da57726c267f0d63b4` with those six modified paths and
the untracked `AGENTS.md`; Photo Organizer was clean on `main` at
`48b9393dc0980ebb55f8b5285d3779e0534deee6`. These are observed reference
states, not import fingerprints. Do not edit, stage, commit, stash, or revert
anything in Vocab App.

Initial Repo Control context verification was `VERIFIED` for
`AUTHORITY_TRANSITION` under supported procedure 1.5.0, snapshot
`3f0cd127fb1b5b7177dc30dd9c6211a52700bf726810f7c4fb262927dacb3a44`.
The trigger and Git-boundary acknowledgement are agent-reported; session
identity and provider-native hook delivery are unverified.
After the blocker answer and lifecycle resume, another
`AUTHORITY_TRANSITION` verification returned `VERIFIED` under current
procedure 1.5.0, snapshot
`0d6fb154d4302c1d932c5915b04eecacd5b1ee5a72f58c0f64b0c778a7a57c6c`.
That record does not renew or extend the expired operator window.
After candidate activation and source-freeze release, the `RECOVERY`
verification returned `VERIFIED` under procedure 1.5.0, snapshot
`e8649ce2e7b68d81050ae18dbdf33194c73b6733f14d37dd7174dc9465840ed8`.
The trigger and Git-boundary acknowledgement are agent-reported; session
identity and provider-native hook delivery remain unverified.

The previous M010.024 system journal shows the original service stopped at
17:58:06 UTC and started at 18:11:08 UTC, a measured service interruption of
13 minutes 2 seconds. For planning only, reserve a 30-minute source
quiescence/import window and target resuming the original service within 15
minutes. Reserve a separate bounded observation of up to 60 minutes after
source thaw for backup recurrence and the 20-minute warning/recovery check;
the original service remains available during that observation. The
source-freeze interval itself was not separately measured. Abort staging and
restore file-backed availability if the service is not healthy within the
operator-approved 30-minute limit; these are planning bounds, not duration
guarantees.

### Fresh M010.027 paths and proposals

| Purpose | M010.027 path |
|---|---|
| Candidate base | `/var/lib/repo-control-staging/m010.027` |
| Prepared database | `/var/lib/repo-control-staging/m010.027/prepared/managed-prepared.sqlite3` |
| Candidate state root | `/var/lib/repo-control-staging/m010.027/state` |
| Scanner/status/context cache | `/home/chuck/.cache/repoctl/m010.027/scanner` |
| Freeze/import/plan and validation reports | `/home/chuck/.local/state/repo-control-staging/m010.027` |
| Backup staging and candidate coverage | `/home/chuck/.local/state/repo-control-backup/staging/m010.027` |
| Candidate coverage record | `/home/chuck/.local/state/repo-control-backup/staging/m010.027/.repoctl-verified-recovery-point.json` |
| Separate restore root | `/home/chuck/.local/state/repo-control-backup/restore/m010.027` |
| Approved NAS destination | `/mnt/nas/repo-control/RepoControl/repo-control-ii` |
| Candidate browser | `127.0.0.1:18767` |
| Staging environment, installed only by operator | `/etc/repo-control/backup-m010027-staging.env` |

The tracked environment example is
[ops/backup.m010027-staging.env.example](../../ops/backup.m010027-staging.env.example).
The four staging-only proposals are
[repo-control-backup-m010027-staging.service](../../ops/systemd/repo-control-backup-m010027-staging.service),
[repo-control-backup-m010027-staging.timer](../../ops/systemd/repo-control-backup-m010027-staging.timer),
[repo-control-backup-monitor-m010027-staging.service](../../ops/systemd/repo-control-backup-monitor-m010027-staging.service), and
[repo-control-backup-monitor-m010027-staging.timer](../../ops/systemd/repo-control-backup-monitor-m010027-staging.timer).
They use the pinned interpreter, target only the M010.027 candidate, and do
not reference or control `repo-control.service`. The backup timer is every 15
minutes; the independent monitor timer is every 5 minutes with a 1200-second
freshness threshold. These are uninstalled proposals; timer syntax is not
evidence of installation or firing. The NAS destination stays within the accepted
RepoControl subtree; do not repeat the sentinel, inspect credentials, change
the mount/ACL, write to `M010.015-test`, or touch existing backups.

### Completed M010.027 source-freeze window

The operator reconfirmed the external-writer pause, the 30-minute interruption
limit, the effective-unit handling above, and the 60-minute post-thaw bound.
The original service was healthy before and after the freeze; it was not
stopped. The fresh freeze was created at `2026-11-17T05:29:31Z`, imported to
the candidate database, and released immediately after activation. The
registry, source set, selections, approval, and source hashes matched during
the import and pre-activation preview.

The following variables and source-freeze/import sequence document the
executed procedure. The candidate is now populated and active, so do not
rerun its empty-directory preconditions, initialization, freeze, import,
binding, or activation. `store-authority prepare` and all candidate work use
the pinned interpreter. The home-directory report/cache/backup/restore paths
remain separate from the protected candidate:

```sh
set -eu
PY=/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python
REPO=/home/chuck/projects/repo-control-ii
REGISTRY="$REPO/ops/repositories.toml"
SOURCE_STATE_ROOT=/home/chuck/.local/share/repoctl
BASE=/var/lib/repo-control-staging/m010.027
STATE="$BASE/state"
IMPORT_DIR="$BASE/prepared"
DATABASE="$IMPORT_DIR/managed-prepared.sqlite3"
REPORTS=/home/chuck/.local/state/repo-control-staging/m010.027
SCANNER_CACHE=/home/chuck/.cache/repoctl/m010.027/scanner
BACKUP_STAGING=/home/chuck/.local/state/repo-control-backup/staging/m010.027
RESTORE_ROOT=/home/chuck/.local/state/repo-control-backup/restore/m010.027
COVERAGE_PATH="$BACKUP_STAGING/.repoctl-verified-recovery-point.json"
FREEZE_REPORT="$REPORTS/freeze-report.json"
IMPORT_REPORT="$REPORTS/import-report.json"
PLAN_REPORT="$REPORTS/activation-plan.json"

test -d "$BASE"
test "$(stat -c '%U:%G:%a' "$BASE")" = chuck:chuck:700
test -z "$(find "$BASE" -mindepth 1 -maxdepth 1 -print)"
test ! -e "$STATE"
test ! -e "$IMPORT_DIR"
for path in "$REPORTS" "$SCANNER_CACHE" "$BACKUP_STAGING" "$RESTORE_ROOT"; do
  test -d "$path"
  test "$(stat -c '%U:%G:%a' "$path")" = chuck:chuck:700
  test -z "$(find "$path" -mindepth 1 -maxdepth 1 -print)"
done
install -d -m 0700 "$STATE" "$IMPORT_DIR"
```

The operator first pauses external/non-cooperating writes across all registered
sources. Stop the original service only if it cannot be shown to cooperate
with the source freeze; use operator confirmation and the installed unit
unchanged. If stopped, `systemctl is-active repo-control.service` must report
`inactive`. Then freeze/drain cooperating writers and prepare only the fresh
candidate:

```sh
"$PY" -m repoctl.cli store-authority freeze-writers \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" > "$FREEZE_REPORT"
FREEZE_ID=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["freeze_id"])' "$FREEZE_REPORT")

"$PY" -m repoctl.cli store-authority prepare \
  --registry "$REGISTRY" \
  --state-root "$SOURCE_STATE_ROOT" \
  --candidate-state-root "$STATE" \
  --destination "$IMPORT_DIR" \
  --writers-quiesced > "$IMPORT_REPORT"
test "$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_database_path"])' "$IMPORT_REPORT")" = "$DATABASE"
```

Review the report before binding: exact three-repository source set and
current selections; source/registry fingerprints; counts and representative
hashes; zero unexplained mismatches/refusals; the single imported
M010.027 approval event linked to the exact prompt revision; and source parity.
Any discrepancy is an abort. Bind only the three reported selection objects,
then preview and activate using one reviewed generation and plan hash:

```sh
BINDING_COUNT=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"]))' "$IMPORT_REPORT")
test "$BINDING_COUNT" = 3
for INDEX in $("$PY" -c 'import json,sys; print(*range(len(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"])))' "$IMPORT_REPORT"); do
  BINDING_JSON=$("$PY" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))["prepared_selection_bindings"][int(sys.argv[2])], separators=(",", ":")))' "$IMPORT_REPORT" "$INDEX")
  "$PY" -m repoctl.cli store-authority bind-selection \
    --registry "$REGISTRY" --state-root "$STATE" \
    --database "$DATABASE" --binding-json "$BINDING_JSON" --po-confirm
done
GENERATION=$("$PY" -c 'import secrets; print(secrets.token_hex(16))')
"$PY" -m repoctl.cli store-authority preview \
  --registry "$REGISTRY" --state-root "$STATE" \
  --source-state-root "$SOURCE_STATE_ROOT" --database "$DATABASE" \
  --generation "$GENERATION" > "$PLAN_REPORT"
PLAN_SHA256=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["plan_sha256"])' "$PLAN_REPORT")
```

Do not activate until independent parity review and the operator/Product Owner
confirm the exact report and plan. After activation, release the source freeze
immediately and restore the original service if it was stopped; do not keep
source writers frozen while applying the candidate hold.

```sh
"$PY" -m repoctl.cli store-authority activate \
  --registry "$REGISTRY" --state-root "$STATE" \
  --source-state-root "$SOURCE_STATE_ROOT" --database "$DATABASE" \
  --generation "$GENERATION" --expected-plan-sha256 "$PLAN_SHA256" \
  --po-confirm --writers-quiesced --source-parity-confirmed
"$PY" -m repoctl.cli store-authority release-writers \
  --registry "$REGISTRY" --state-root "$SOURCE_STATE_ROOT" \
  --freeze-id "$FREEZE_ID"
```

If the original service was stopped, the operator starts the same installed
unit without editing/reloading it and verifies `systemctl is-active` reports
`active`; `curl -fsS http://127.0.0.1:8765/healthz` must report
`legacy_files` with a null InformationStore binding. Resume original
file-backed availability before candidate hold, NAS, restore, or timer work.

With the source thawed and original service healthy, apply and verify an
operator-controlled hold to the fresh candidate before starting its browser:
protected candidate parents/directories and DB/WAL/guard/config/selection files
must be root-controlled and not writable by `chuck`; preserve the approved SHM
exception and `chuck`-writable lock paths. Derive the exact selection and
import-scratch inventory from this candidate, check for symlinks/open holders,
and record before/after hashes. Do not copy M010.024 paths, hashes, scratch
inventory, or permissions recursively. The scanner cache, reports, coverage,
backup staging, and restore paths remain outside the held tree. If hold setup
fails, leave the candidate offline; do not refreeze the live source to
troubleshoot it.

For the currently registered IDs, verify these leaf names against the fresh
candidate inventory before any privileged command. The scratch directory
names are the first 24 hex characters of each repository ID's SHA-256; stop
and recalculate if a repository ID differs. Continue in the same shell so the
path variables from the preceding window commands remain bound:

```sh
set -euo pipefail
RC_ID=repo-control-ii--945e7f88eafb
VOCAB_ID=vocab-app--0d81efec80ef
PHOTO_ID=photo-organizer-dev--84c64acdea9f
RC_SCRATCH="$IMPORT_DIR/61047c89b60f61a85d61ba3a"
VOCAB_SCRATCH="$IMPORT_DIR/c07984ec043b5029f6d87480"
PHOTO_SCRATCH="$IMPORT_DIR/1b12de3f56fa83f1657f41f5"
GUARD="$BASE/.state.information-store-authority.guard"
CONFIG="$STATE/information-store-authority.json"
LOCK_DIR="$STATE/.locks"
LOCK_FILE="$LOCK_DIR/state-artifacts.lock"
RC_SELECTION="$STATE/$RC_ID/milestones/selection.json"
VOCAB_SELECTION="$STATE/$VOCAB_ID/milestones/selection.json"
PHOTO_SELECTION="$STATE/$PHOTO_ID/milestones/selection.json"

for path in "$BASE" "$IMPORT_DIR" "$STATE" "$DATABASE" \
  "$GUARD" "$CONFIG" "$RC_SCRATCH" "$VOCAB_SCRATCH" "$PHOTO_SCRATCH" \
  "$RC_SELECTION" "$VOCAB_SELECTION" "$PHOTO_SELECTION" "$LOCK_DIR" "$LOCK_FILE"; do
  test -e "$path"
done
if ! test -e "$DATABASE-shm"; then
  "$PY" -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("PRAGMA journal_mode").fetchone()[0]); c.close()' "$DATABASE"
fi
test -e "$DATABASE-shm"
namei -l "$BASE" "$DATABASE" "$GUARD" "$CONFIG" \
  "$RC_SELECTION" "$VOCAB_SELECTION" "$PHOTO_SELECTION"
find "$BASE" -xdev -type l -print > "$REPORTS/candidate-symlinks.txt"
test ! -s "$REPORTS/candidate-symlinks.txt"
find "$BASE" -xdev -printf '%p|%y|%u:%g|%m|%d:%i\n' | sort \
  > "$REPORTS/candidate-before-hold.txt"
sha256sum "$DATABASE" "$DATABASE-shm" "$GUARD" "$CONFIG" \
  "$RC_SELECTION" "$VOCAB_SELECTION" "$PHOTO_SELECTION" \
  > "$REPORTS/candidate-before-hold.sha256"
if test -e "$DATABASE-wal"; then
  sha256sum "$DATABASE-wal" >> "$REPORTS/candidate-before-hold.sha256"
fi
find "$RC_SCRATCH" "$VOCAB_SCRATCH" "$PHOTO_SCRATCH" -mindepth 1 -print \
  > "$REPORTS/candidate-scratch-contents.txt"
test ! -s "$REPORTS/candidate-scratch-contents.txt"
```

The inventory includes any WAL present; do not create an empty WAL/SHM
sidecar. SHM must exist for this held deployment. The ordinary pinned-runtime
SQLite open above is only to initialize its normal sidecars when needed; close
it and record the resulting inventory before continuing. Check for open
holders before the hold; status 1 with no output is the expected `fuser`
result:

For the current M010.027 database, the documented open returned
`journal_mode: wal` but created neither sidecar. A subsequent pinned-runtime
read transaction (`SELECT COUNT(*) FROM sqlite_master`) also left both absent.
The required SHM path therefore cannot yet be inventoried or held. Stop here:
do not fabricate an empty sidecar, make a candidate database write, start the
candidate browser/CLI, or proceed to backup/timer work. Resolve this blocker
through Repo Control before continuing.

```sh
fuser_status=0
sudo fuser -v "$DATABASE" "$DATABASE-shm" "$GUARD" "$CONFIG" \
  "$RC_SELECTION" "$VOCAB_SELECTION" "$PHOTO_SELECTION" "$LOCK_FILE" \
  || fuser_status=$?
test "$fuser_status" -eq 1
if test -e "$DATABASE-wal"; then
  fuser_status=0
  sudo fuser -v "$DATABASE-wal" || fuser_status=$?
  test "$fuser_status" -eq 1
fi
```

After reviewing the exact inventory and confirming no holders, the operator
applies only these exact paths. Do not use recursive ownership/mode changes.
The optional WAL commands apply only if the reviewed inventory contains it:

```sh
set -euo pipefail
sudo chown root:chuck -- "$BASE" "$IMPORT_DIR" "$RC_SCRATCH" \
  "$VOCAB_SCRATCH" "$PHOTO_SCRATCH" "$STATE" \
  "$STATE/$RC_ID" "$STATE/$RC_ID/milestones" \
  "$STATE/$VOCAB_ID" "$STATE/$VOCAB_ID/milestones" \
  "$STATE/$PHOTO_ID" "$STATE/$PHOTO_ID/milestones"
sudo chmod 0750 -- "$BASE" "$IMPORT_DIR" "$RC_SCRATCH" \
  "$VOCAB_SCRATCH" "$PHOTO_SCRATCH" "$STATE" \
  "$STATE/$RC_ID" "$STATE/$RC_ID/milestones" \
  "$STATE/$VOCAB_ID" "$STATE/$VOCAB_ID/milestones" \
  "$STATE/$PHOTO_ID" "$STATE/$PHOTO_ID/milestones"
sudo chown root:chuck -- "$GUARD" "$CONFIG" "$RC_SELECTION" \
  "$VOCAB_SELECTION" "$PHOTO_SELECTION" "$DATABASE"
sudo chmod 0440 -- "$GUARD" "$CONFIG" "$RC_SELECTION" \
  "$VOCAB_SELECTION" "$PHOTO_SELECTION" "$DATABASE"
sudo chown root:chuck -- "$DATABASE-shm"
sudo chmod 0660 -- "$DATABASE-shm"
if test -e "$DATABASE-wal"; then
  sudo chown root:chuck -- "$DATABASE-wal"
  sudo chmod 0440 -- "$DATABASE-wal"
fi

sudo -u chuck -- test ! -w "$BASE"
sudo -u chuck -- test ! -w "$STATE"
sudo -u chuck -- test ! -w "$DATABASE"
sudo -u chuck -- test ! -w "$GUARD"
sudo -u chuck -- test ! -w "$CONFIG"
sudo -u chuck -- test ! -w "$RC_SELECTION"
sudo -u chuck -- test ! -w "$VOCAB_SELECTION"
sudo -u chuck -- test ! -w "$PHOTO_SELECTION"
sudo -u chuck -- test -w "$DATABASE-shm"
sudo -u chuck -- test -w "$LOCK_DIR"
sudo -u chuck -- test -w "$LOCK_FILE"
find "$BASE" -xdev -printf '%p|%y|%u:%g|%m|%d:%i\n' | sort \
  > "$REPORTS/candidate-held-inventory.txt"
sha256sum "$DATABASE" "$DATABASE-shm" "$GUARD" "$CONFIG" \
  "$RC_SELECTION" "$VOCAB_SELECTION" "$PHOTO_SELECTION" \
  > "$REPORTS/candidate-held.sha256"
if test -e "$DATABASE-wal"; then
  sha256sum "$DATABASE-wal" >> "$REPORTS/candidate-held.sha256"
fi
```

Compare both inventories and hash records; only ownership/mode changes
approved above may differ. If any expected path is missing, a symlink/holder
exists, a hash changes, or a permission probe disagrees, do not start a
candidate process or any backup/timer job.

### Held candidate, backup, restore, and timer checks

With the candidate held, start its read-only browser on the separate port and
check an independent Coder process against the imported approval. The coverage
path is candidate-specific and must never be replaced by the live coverage
record:

```sh
REPOCTL_BACKUP_COVERAGE_PATH="$COVERAGE_PATH" \
  "$PY" -m repoctl.cli web --repository-registry "$REGISTRY" \
    --host 127.0.0.1 --port 18767 \
    --state-root "$STATE" --scanner-cache-root "$SCANNER_CACHE"

# In a second terminal:
curl -fsS http://127.0.0.1:18767/healthz
REPOCTL_BACKUP_COVERAGE_PATH="$COVERAGE_PATH" \
  "$PY" -m repoctl.cli coder-status \
    --repository "$REPO" --state-root "$STATE" --format json
```

Expected: browser health binds to this candidate and reports
`sqlite_managed`; independent `coder-status` reports M010.027, the exact
selection/revision/digest, and no second confirmation. Repeat after one
candidate-browser restart, then stop the browser and verify the hold and
protected hashes remain unchanged. No candidate application write is allowed.

After confirming the existing CIFS mount source/identity and exact
RepoControl destination, install only the M010.027 environment and four
reviewed staging units. Never overwrite the live `backup.env` or install a
service for the live browser. With the root-only environment file in place,
run the pinned publisher once and capture its JSON report; the report supplies
the exact manifest path for a separate restore:

```sh
test ! -e /etc/repo-control/backup-m010027-staging.env
sudo install -o root -g root -m 0600 \
  ops/backup.m010027-staging.env.example \
  /etc/repo-control/backup-m010027-staging.env
test ! -e /etc/systemd/system/repo-control-backup-m010027-staging.service
test ! -e /etc/systemd/system/repo-control-backup-m010027-staging.timer
test ! -e /etc/systemd/system/repo-control-backup-monitor-m010027-staging.service
test ! -e /etc/systemd/system/repo-control-backup-monitor-m010027-staging.timer
sudo install -o root -g root -m 0644 \
  ops/systemd/repo-control-backup-m010027-staging.service \
  ops/systemd/repo-control-backup-m010027-staging.timer \
  ops/systemd/repo-control-backup-monitor-m010027-staging.service \
  ops/systemd/repo-control-backup-monitor-m010027-staging.timer \
  /etc/systemd/system/
sudo systemctl daemon-reload
env \
  REPOCTL_BACKUP_SOURCE="$DATABASE" \
  REPOCTL_BACKUP_STAGING="$BACKUP_STAGING" \
  REPOCTL_BACKUP_DESTINATION=/mnt/nas/repo-control/RepoControl/repo-control-ii \
  REPOCTL_BACKUP_RESTORE_ROOT="$RESTORE_ROOT" \
  REPOCTL_BACKUP_MOUNTPOINT=/mnt/nas/repo-control \
  REPOCTL_BACKUP_MOUNT_SOURCE=//192.168.1.171/ProjectBackups \
  REPOCTL_BACKUP_COVERAGE_PATH="$COVERAGE_PATH" \
  REPOCTL_BACKUP_AUTHORITY_STATE_ROOT="$STATE" \
  REPOCTL_BACKUP_REPOSITORY_ID=repo-control-ii--945e7f88eafb \
  REPOCTL_BACKUP_REPOSITORY_KEY=repo-control-ii \
  REPOCTL_BACKUP_REPOSITORY_ROOT="$REPO" \
  "$PY" -m repoctl.backup backup > "$REPORTS/backup-report.json"
MANIFEST=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["manifest_path"])' "$REPORTS/backup-report.json")
test -f "$MANIFEST"
env \
  REPOCTL_BACKUP_SOURCE="$DATABASE" \
  REPOCTL_BACKUP_STAGING="$BACKUP_STAGING" \
  REPOCTL_BACKUP_DESTINATION=/mnt/nas/repo-control/RepoControl/repo-control-ii \
  REPOCTL_BACKUP_RESTORE_ROOT="$RESTORE_ROOT" \
  REPOCTL_BACKUP_MOUNTPOINT=/mnt/nas/repo-control \
  REPOCTL_BACKUP_MOUNT_SOURCE=//192.168.1.171/ProjectBackups \
  REPOCTL_BACKUP_COVERAGE_PATH="$COVERAGE_PATH" \
  REPOCTL_BACKUP_AUTHORITY_STATE_ROOT="$STATE" \
  REPOCTL_BACKUP_REPOSITORY_ID=repo-control-ii--945e7f88eafb \
  REPOCTL_BACKUP_REPOSITORY_KEY=repo-control-ii \
  REPOCTL_BACKUP_REPOSITORY_ROOT="$REPO" \
  "$PY" -m repoctl.backup restore --manifest "$MANIFEST" \
    --restore-name m010027-separate-restore.sqlite3 \
    > "$REPORTS/restore-report.txt"
```

Verify the backup manifest/payload, repository/authority/selection/database
identity, and fresh coverage record bind to this candidate. Verify the distinct
restore path, integrity, foreign keys, selected prompt approval/revision/digest,
representative workflow state and document hashes; record actual restore
duration. Browser, independent CLI, and publisher must agree on candidate root
and recovery-point binding. Do not overwrite any existing backup or restore.

Only after those checks pass, start the two candidate-specific timers and
observe actual journal evidence, not just calendars or `list-timers` output:

```sh
sudo systemctl start repo-control-backup-m010027-staging.timer \
  repo-control-backup-monitor-m010027-staging.timer
systemctl list-timers --all repo-control-backup-m010027-staging.timer \
  repo-control-backup-monitor-m010027-staging.timer
journalctl -u repo-control-backup-m010027-staging.service \
  -u repo-control-backup-monitor-m010027-staging.service --no-pager
```

Observe successful recurring backup firings 15 minutes apart and monitor
firings at five-minute intervals. For the 20-minute warning/recovery check,
use only a bounded staging-only fault/check: stop the M010.027 backup timer
after a verified backup, let its monitor report stale coverage locally, then
run a fresh candidate backup and verify a recovery result. Do not disturb the
live service, NAS mount, or known-good backups; do not add external alerting.
Keep this observation bounded and preserve all report/journal timestamps.
Before closing, stop both M010.027 timers and the candidate browser; leave the
candidate held and offline, the original service active/file-backed, and no
staging-only job running unless separately approved.

If any step fails, stop candidate/staging-only processes and timers, preserve
the exact reports/inventory, leave the candidate held/offline, release only
the exact source freeze ID with an abort reason, and restore the original
file-backed service promptly. If candidate activation completed but no
managed application write occurred, a candidate abort requires the exact
reviewed plan and separately approved `store-authority abort`; do not infer
permission from this handoff. If any managed SQLite-only application write
occurred, do not abort or switch back to files: preserve SQLite and applicable
WAL/SHM plus verified snapshots, pause writes, and use separately authorized
forward recovery.

### Separate cutover checklist (not authorized by M010.027)

Even a GO recommendation is not cutover permission. Before any later
separately approved canary:

1. Recheck live service identity/authority, source registry, all registered
   source inventories, selection, dispatch and approval evidence. If any
   source moved, re-freeze and re-import/restage; if candidate binding or
   generation changes, take and verify a new candidate-bound backup and
   separate restore. Never reuse this review's disposable or stale
   fingerprints.
2. Verify fresh recoverable coverage for the exact final candidate, exact
   restore checks, operator-controlled hold, independent browser/CLI identity,
   effective pinned service/runtime/coverage configuration, and healthy
   original service. Obtain fresh operator confirmation and explicit
   authorization to release only the candidate hold.
3. Perform read/health checks before a separately authorized canary write.
   Document and rehearse the pre-first-write return path: pause/quiesce
   writers, use the exact approved candidate abort plan, retain file-backed
   state as authoritative, release the exact freeze, and verify the original
   service is healthy with `legacy_files`.
4. After the first SQLite-only application write, preserve the managed
   database and applicable WAL/SHM and all verified snapshots. Never switch
   back to files or use candidate abort; pause writes and use forward recovery
   with a separate restore and deterministic reconciliation of post-snapshot
   writes.

The one-hour maximum-loss objective depends on every managed writer using the
effective guard and recoverable verified coverage. It is not a restore-time
or zero-loss guarantee; the isolated M010.021 forward-recovery proof does not
establish production recovery time or loss behavior.

**Current recommendation: GO for a separately authorized cutover review, not
for cutover execution.** The exact M010.027 approval/import and source parity,
original file-backed service restoration, operator-controlled candidate hold,
read-only candidate browser/CLI identity (including restart), verified NAS
backup and separate restore all passed. The candidate remains bound to
generation `2e329ee9e60c16591a1b4afd1d1bafba`, config
`9531d8b9346afbadd00d19edc19dc5dcb57c57db9107b3f5128a9b7173508a77`, DB
identity `2bff56f7c5d61d0c3680f55bdc7f02586ae98389f154f7229d3a2f184ead1033`,
and M010.027 selection r104.

The production source capture used freeze ID
`834eb6870d81ab90bb08a2eda69c7b49`, source-inventory SHA-256
`dcff46f803a14888ee1c3cffe642d5b788cd04bec42e30c9eb84b466e37f265a`, and
activation-plan SHA-256
`b248e981490a3e7385debc74cef7b2cf8fef3967d295009735e9464c2c6e0ad5`.
The 464 import sources matched with zero mismatches/refusals and source parity
passed. The exact M010.027 approval was imported once and bound to revision
`bb1fe9fe9df5a6373c6a8b880dad8108`.

The metadata-only `user_version` 3→4→3 initialization preserved main-file
SHA-256 `90c6f41981d60586d343c3d4b64f764be5fd76a40d91137c20bffb26be535e94`.
The corrected read-only postflight verified WAL (4,152 B) and SHM (32,768 B)
persistence, `user_version=3`, `schema_version=31`, all 15 application-table
digests, unchanged approval binding, integrity `ok`, and zero FK violations.

The timer preflight required two in-scope fixes: mount-root validation now
requires only read/traverse access, while the approved destination retains its
write-access check; mountinfo validation also permits a nested systemd
destination mount only when its filesystem, CIFS source, and mounted child
root prove the exact approved destination. Wrong shares, wrong child paths,
unexpected nested mounts, and ambiguous mount entries are rejected. The
focused backup suite passes (12 tests). The initial 06:45 and 16:06 timer
failures occurred before any NAS write and are resolved by these fixes.

The installed systemd backup service passed in its real sandbox at 16:15 UTC.
Calendar backup firings at 16:30:06 and 16:45:07 succeeded 15 minutes apart;
both manifests were verified with integrity `ok`, zero FK violations, and
coverage bound to the exact candidate. The monitor succeeded on its five-minute
cadence. Timer backup manifests were
`repo-control-20261117T163004093777Z-ca586c2f7c9a` and
`repo-control-20261117T164504088195Z-932598b4af21`. For the controlled
warning/recovery check, the backup timer was
stopped after the 16:45 backup while the monitor continued. It reported
healthy coverage at 20 minutes, then the expected warning at 25 minutes
(17:10:06). A verified one-shot recovery backup completed at 17:12:34, and the
17:15:07 monitor reported healthy coverage at 2.5 minutes. Its manifest was
`repo-control-20261117T171233197682Z-f49fd0db1300`.

At close, both M010.027 timers are stopped and not enabled; no candidate
browser or staging job is running. The candidate remains held and offline,
with no hold release, application write, live authority switch, or cutover
performed. The original service is active on `legacy_files`. Before any later
cutover, recheck source drift/parity and the final candidate binding, obtain
fresh verified coverage and operator confirmation, and follow the separate
cutover/abort checklist above. The one-hour maximum-loss objective depends on
the effective guard and recoverable coverage; it is not restore time or a
zero-loss guarantee. The Vocab App dirty shared-procedure files remain
accepted as-is and untouched; the live Repo Control service was not changed.

## M010.028 preflight — proposed final target, not installed

M010.028 is authorized for preflight and preparation only. The exact approved
prompt hash is
`f540e5100054bee731af5f286eff1c2853ce9b0789cd541dc6cbd35f3e05a9b6`;
M010.027 is CLOSED. Repo Control reports the installed shared procedure
`CURRENT` at release `1.5.0`; Coder context verification schema 2 is
supported. The current service remains active on `legacy_files`.
Its installed unit is `/etc/systemd/system/repo-control.service`, has no
drop-ins or `EnvironmentFile`, and was observed with SHA-256
`76b2e8ed878ba7952bb495388c49dc022aa4707fd2015e1cd336ce2ef73e5ce3`.
It starts the repository-local `.venv` on CPython 3.12.3 / SQLite 3.45.1
(SQLite source ID `2024-01-30 16:01:20 e876e51a0ed5c5b3126f52e532044363a014bc594cfefa87ffb5b82257ccalt1`),
not the reviewed CPython 3.12.16 / SQLite 3.51.3 runtime. The live health
endpoint advertises prompt-approval evidence schema 1 and reports
`legacy_files` with no managed binding. The installed unit differs from the
tracked `ops/systemd/repo-control.service`; preserve the exact installed file
and recheck its hash and drop-ins immediately before any authorized switch.

The registered-source drift check found Vocab App HEAD and its six modified
shared-procedure files plus untracked `AGENTS.md` match the captured M010.027
pre-import state. Photo Organizer HEAD matches its captured HEAD and has no
worktree changes (its current `main` is one commit ahead of the recorded
`origin/main`). Repo Control II remains on
`db-workflow-persistence-architecture` at
`794a57ae61ad5845b2862c5fffaa4cb2ce9b456a`. M010.028's prompt/workflow and
the current preflight proposals are new source records; the test-result event
events were added to its workflow sidecar. All require a fresh import after
preparation is complete. The current NAS mount remains
`//192.168.1.171/ProjectBackups` (CIFS) at
`/mnt/nas/repo-control`; the accepted destination and parent paths remain
owned by `chuck:chuck` mode `0700`. M010.024 and M010.027 candidate DB/WAL
files remain root-controlled mode `0440`, SHM mode `0660`, and their staging
timers are inactive. No production backup or monitor timer is currently
loaded or active.

After later workflow events made the first M010.028 candidate stale, it was
preserved held as evidence. Preparation of `m010.028-r2` was aborted before
binding because a precheck incorrectly expected old selection IDs; the
importer generates new IDs for each candidate. The following replacement paths
identify a fresh candidate proposal; they are not evidence of an import,
activation, hold, or backup:

| Purpose | M010.028 path |
|---|---|
| Candidate base | `/var/lib/repo-control-staging/m010.028-r3` |
| Prepared database | `/var/lib/repo-control-staging/m010.028-r3/prepared/managed-prepared.sqlite3` |
| Candidate state root | `/var/lib/repo-control-staging/m010.028-r3/state` |
| Scanner cache | `/home/chuck/.cache/repoctl/m010.028-r3/scanner` |
| Reports and evidence | `/home/chuck/.local/state/repo-control-staging/m010.028-r3` |
| Backup staging / coverage | `/home/chuck/.local/state/repo-control-backup/staging/m010.028-r3` |
| Separate restore root | `/home/chuck/.local/state/repo-control-backup/restore/m010.028-r3` |
| Candidate browser during checks | `127.0.0.1:18768` |
| Final browser | `127.0.0.1:8765` |
| Browser environment target | `/etc/repo-control/repo-control-m010028.env` |
| Backup environment target | `/etc/repo-control/backup-m010028.env` |

The proposed browser unit pins the reviewed runtime, explicitly names the
candidate state root, scanner cache and coverage file, and grants sandbox
write access only to the candidate DB/WAL/SHM, state lock and scanner cache.
The filesystem hold remains the independent write barrier until Gate B.
Candidate-specific backup and monitor units point only at M010.028 and the
accepted NAS subtree, with 15-minute and five-minute timers respectively.
The proposals are
[repo-control-m010028-managed.service](../../ops/systemd/repo-control-m010028-managed.service),
[repo-control-m010028.env.example](../../ops/repo-control-m010028.env.example),
[backup.m010028.env.example](../../ops/backup.m010028.env.example),
[repo-control-backup-m010028.service](../../ops/systemd/repo-control-backup-m010028.service),
[repo-control-backup-m010028.timer](../../ops/systemd/repo-control-backup-m010028.timer),
[repo-control-backup-monitor-m010028.service](../../ops/systemd/repo-control-backup-monitor-m010028.service), and
[repo-control-backup-monitor-m010028.timer](../../ops/systemd/repo-control-backup-monitor-m010028.timer).
They are uninstalled proposals; timer syntax is not evidence of installation
or firing. Before the first candidate was prepared, `systemd-analyze verify`
returned success for the original proposed units, with only an unrelated
pre-existing Synology unit warning about `/var/run` versus `/run`. The
replacement paths require their own verification. Neither syntax check proves
an installed sandbox, live write guard, or timer firing.

### R4 proposals prepared before capture

The R3 candidate is retired and remains preserved. The following separate R4
files now use only the planned R4 candidate paths; they do not overwrite or
modify the R3 proposals:

- [repo-control-m010028-r4-managed.service](../../ops/systemd/repo-control-m010028-r4-managed.service)
- [repo-control-m010028-r4.env.example](../../ops/repo-control-m010028-r4.env.example)
- [backup.m010028-r4.env.example](../../ops/backup.m010028-r4.env.example)
- [repo-control-backup-m010028-r4.service](../../ops/systemd/repo-control-backup-m010028-r4.service)
- [repo-control-backup-m010028-r4.timer](../../ops/systemd/repo-control-backup-m010028-r4.timer)
- [repo-control-backup-monitor-m010028-r4.service](../../ops/systemd/repo-control-backup-monitor-m010028-r4.service)
- [repo-control-backup-monitor-m010028-r4.timer](../../ops/systemd/repo-control-backup-monitor-m010028-r4.timer)

They are uninstalled proposals. The backup example contains exactly the 12
approved non-secret path, identity, mount-source and age bindings, with no
credential fields. After the candidate is prepared, the operator-installed
file is exclusively `/etc/repo-control/backup-m010028-r4.env`, owned by
`root:chuck` mode `0640`; the publisher check requires every parent directory
to be root-owned and writable only by root. Existing backup settings and NAS
credentials remain unchanged. The systemd syntax check passed with the same
unrelated existing Synology `/var/run` warning noted above. This does not
establish that R4 exists, is held, has a bound publisher, or has a backup or
restore. Those checks follow the fresh import and exact R4 binding.

### R6 replacement — held, source-stale evidence

R5 remains held as evidence, but legacy workflow records after its capture
made it source-stale. Its verified recovery point covers R5, not R6.
Before import, these five roots must be empty, real `chuck:chuck 0700`
directories with importer read, write and traversal access:

- `/var/lib/repo-control-staging/m010.028-r6`
- `/home/chuck/.local/state/repo-control-staging/m010.028-r6`
- `/home/chuck/.cache/repoctl/m010.028-r6`
- `/home/chuck/.local/state/repo-control-backup/staging/m010.028-r6`
- `/home/chuck/.local/state/repo-control-backup/restore/m010.028-r6`

The importer needs to write the candidate root. A separately approved hold
later protects the candidate's database and WAL. The R6-only proposals are:

- [repo-control-m010028-r6-managed.service](../../ops/systemd/repo-control-m010028-r6-managed.service)
- [repo-control-m010028-r6.env.example](../../ops/repo-control-m010028-r6.env.example)
- [backup.m010028-r6.env.example](../../ops/backup.m010028-r6.env.example)
- [repo-control-backup-m010028-r6.service](../../ops/systemd/repo-control-backup-m010028-r6.service)
- [repo-control-backup-m010028-r6.timer](../../ops/systemd/repo-control-backup-m010028-r6.timer)
- [repo-control-backup-monitor-m010028-r6.service](../../ops/systemd/repo-control-backup-monitor-m010028-r6.service)
- [repo-control-backup-monitor-m010028-r6.timer](../../ops/systemd/repo-control-backup-monitor-m010028-r6.timer)

The R6 candidate was imported, bound, activated in isolation, and held. The
Product Owner approved `root:chuck 0640` for only the new non-secret
`/etc/repo-control/backup-m010028-r6.env`. The operator installed the R6
publisher files; binding passed before one backup-service firing. A separate
restore passed integrity, foreign-key, binding, approval and full source
parity checks in 3.351 seconds. The backup wrapper reported STOP; independent
journal and recovery-point checks verified one successful firing, but the
exact wrapper failure is unproven. It was not retried. R6 publisher services
are inactive and timers are inactive and disabled.

The required B5 bug, test and classification records changed the legacy
workflow after capture. R6 is therefore source-stale and must not be used for
Gate A. Preserve its hold, backup, restore and evidence; a replacement
candidate needs a fresh capture and its own binding and recovery proof.
Legacy remains live, and the Product Owner stood down the external-writer
pause. The R6 recovery point's 20-minute freshness deadline was
2026-11-18 2:15:16 p.m. Pacific.

### R7 replacement proposal — preparation only

Before import, all five R7 roots must be empty, real `chuck:chuck 0700`
directories with importer read, write and traversal access:

- `/var/lib/repo-control-staging/m010.028-r7`
- `/home/chuck/.local/state/repo-control-staging/m010.028-r7`
- `/home/chuck/.cache/repoctl/m010.028-r7`
- `/home/chuck/.local/state/repo-control-backup/staging/m010.028-r7`
- `/home/chuck/.local/state/repo-control-backup/restore/m010.028-r7`

The four home directories were created by the importer and the first root by
the operator. All five then passed independent importer metadata, ACL, access
and emptiness checks; repeat those checks before capture. The R7-only
proposals are:

- [repo-control-m010028-r7-managed.service](../../ops/systemd/repo-control-m010028-r7-managed.service)
- [repo-control-m010028-r7.env.example](../../ops/repo-control-m010028-r7.env.example)
- [backup.m010028-r7.env.example](../../ops/backup.m010028-r7.env.example)
- [repo-control-backup-m010028-r7.service](../../ops/systemd/repo-control-backup-m010028-r7.service)
- [repo-control-backup-m010028-r7.timer](../../ops/systemd/repo-control-backup-m010028-r7.timer)
- [repo-control-backup-monitor-m010028-r7.service](../../ops/systemd/repo-control-backup-monitor-m010028-r7.service)
- [repo-control-backup-monitor-m010028-r7.timer](../../ops/systemd/repo-control-backup-monitor-m010028-r7.timer)

The units passed syntax verification with the same unrelated Synology
`/var/run` warning. The backup proposal contains only the 12 permitted
non-secret bindings, and the prospective settings directory's ancestors are
root-controlled. The Product Owner approved `root:chuck 0640` in chat for
the new `/etc/repo-control/backup-m010028-r7.env` only; installation still
requires separate approval. No R7 import, hold, installed publisher, backup,
restore or service switch has occurred.

On 2026-11-18, the importer rechecked all five empty roots and access; the
legacy service was healthy and unfrozen, its saved unit matched, the NAS
destination was accessible, and prior staging publishers were inactive with
timers disabled. The held R6 inventory and protected hashes remained
unchanged. A conditional 2026-11-19 capture window was superseded by the
Product Owner's request to start sooner. On 2026-11-18, the proposed same-day
window and external-writer pause were not confirmed. Gates A and B remain
unapproved. Reverify current authority and all volatile checks in a confirmed
window; record any required context verification before the final source
capture, not afterward.

The B4 recovery-check fix uses the reviewed candidate coverage path regardless
of an inherited shell value and restores that value on success or error.
The legacy workflow records the fixture result as
`12415c4844754ea4bc9350ca88697897` and the fix as
`10b412da4169402fbea3d8ad0963509f`; both precede any R6 capture.

Before any service-file replacement in the confirmed window, compare the live
unit hash with the recorded value, confirm `DropInPaths` is empty, and copy
`/etc/systemd/system/repo-control.service` into the run-owned reports
directory. If the hash, fragment path, or drop-in state differs, stop and
reconcile the rollback image. Install the proposed browser unit as
`repo-control.service` only after Gate A. The pre-first-write return is:
stop the managed service, restore the captured exact unit with root ownership
and mode `0644`, run `systemctl daemon-reload`, restart the original service,
and verify active health reports `legacy_files` with a null managed binding.
Do not use this path after a managed application commit or when commit status
is uncertain. In those cases preserve DB/WAL/SHM and verified snapshots, pause
writers, and use reviewed forward recovery.

The R6 freeze probe against the same running legacy instance returned 423 for
the tested workflow submission, versus 400 without the freeze, and the
workflow hash did not change. This proves only that tested path, not every
writer. In the confirmed window, verify behavior again; if the process does
not honor the freeze, use only the explicitly authorized stop and unchanged
restart procedure. External and non-cooperating writers require a separately
confirmed pause. Do not freeze, stop, restart, install units, or switch
authority during preflight.

Isolated candidate activation uses
`store-authority activate --expected-plan-sha256 ... --po-confirm` only after
its separate exact-plan approval; it is not the Gate A live switch. For Gate A,
retain the data-only switch plan and its approval outside frozen source records
and use the operator switch helper only after exact plan/window approval.
Gate B is separate: show the exact target and coverage, then include its
explicit approval and candidate identity in the one planned M010.028
`coder-test` event payload. The first managed application write then records
that decision without an extra approval write. If the event cannot carry the
evidence or its commit result is ambiguous, stop and follow the prompt's
forward-recovery boundary; do not retry or use a sidecar/SQL edit.

A local `repoctl mcp-architect` process was also observed configured against
the loopback service. Although most adapter tools are read-only, it exposes
typed PROMPT, INSTRUCTION, and RESPONSE proposal submissions that write the
dispatch ledger. Treat clients able to invoke those submissions as writers
during source freeze: pause them or explicitly stop the adapter for the
confirmed window, then recheck dispatch/source inventory. This preflight did
not test whether that long-running adapter honors a source freeze.

### M010.028 incident: unapproved switch to the held R3 candidate

On 2026-11-17 the copyable Gate A commands in a stand-down handoff (marked
"do not run now") were executed without Gate A approval. Live authority was
the held R3 candidate from 22:06:27 to 22:12:38 UTC and served only GET
requests. The guarded pre-write return restored the saved legacy unit. The
return verified `legacy_files` health with a null managed binding, an inactive
freeze, an unchanged hold, and inactive, disabled timers. No managed
application commit occurred: the R3 DB and WAL stayed `root:chuck 0440` under
root `0750` directories while the service ran as `chuck` with
`NoNewPrivileges`, and the 22-path inventory, inodes, and hashes were
unchanged. The cause of the operator's SSH disconnect is **unproven**. The
suspected mechanism is a bare `set -euo pipefail` left active in the
long-lived operator shell. The incident is recorded as a Repo Control bug, and
its evidence is sealed (0400, `SHA256SUMS`) under
`~/.local/state/repo-control-staging/m010.028-r3/incident-20261117-unapproved-switch/`.
R3 is retired as a Gate A target and preserved; it remains held.

### M010.028 Gate A handoff procedure (replaces pasted command blocks)

[gate_a.py](../../ops/m010028/gate_a.py),
[gate-a-switch.sh](../../ops/m010028/gate-a-switch.sh), and
[gate-a-return.sh](../../ops/m010028/gate-a-return.sh) replace copyable
privileged command blocks:

1. **Review is data only.** `gate_a.py review --plan P` validates a strict
   plan, which allows only exact keys and pattern-checked values, a window of
   at most four hours, and a 5–120 s readiness bound. It prints the plan hash,
   the window, and the exact approval phrase
   `GATE A APPROVED plan_sha256=<sha> window=<start>/<end>`. It executes no
   programs and cannot reach systemd, which is enforced by an strace test
   with negative controls. A review never contains executable switch
   commands. This is the authorized switch-start window, not necessarily
   the full maintenance window. Its end must be early enough to complete
   readiness and a safe pre-write return before the agreed safe-stop; a
   maintenance-window end later than that safe-stop is not a switch deadline.
2. **Execution requires exact Product Owner approval.** Run
   `gate_a.py record-approval` only with the Product Owner's verbatim phrase.
   It writes an O_EXCL `0400` record bound to the plan hash, the window, and
   the switch and return script hashes. Missing, altered or mismatched
   approval, an expired window, or changed scripts cause refusal before
   stopping the service.
3. **Preflight immediately before switching.** The preflight checks:
   - the window;
   - the saved legacy unit (root:root 0644, hash);
   - that the installed unit is legacy with no drop-ins;
   - the managed unit and env source hashes;
   - the managed unit's executable contract and every systemd namespace path:
     pinned interpreter, CLI module, registry, loopback port, state root,
     candidate-specific scanner cache, exact non-optional `ReadWritePaths`,
     hardening directives, scanner ownership/mode, and a one-attempt start
     limit; every required file or directory must already exist;
   - the held-path inventory and hashes;
   - source and registry parity;
   - that the recovery point covers the expected generation, database
     identity, and selection;
   - publisher binding.
   The recovery check uses the reviewed candidate coverage path even if the
   operator's shell has no coverage variable or a different inherited value.
   It restores the caller's environment on both success and failure.
   The script checks before authentication, repeats the full preflight and
   exact approval after `sudo -v`, installs the reviewed environment using
   `sudo -n`, and repeats the preflight and approval immediately before the
   service stop. Each repeated preflight uses one verified plan and verifies
   that approval again afterward. Source parity and recovery freshness are
   checked after the longer publisher checks, and the window is checked again
   after preflight. Later privileged commands use `sudo -n` so an
   authentication prompt cannot silently reopen the gap. This does not
   replace confirmed writer quiescence or guarantee hard real-time execution.
4. **Operator shell discipline.** Never paste `set -e` or `set -euo pipefail`
   into an interactive shell. Do not source the scripts; they refuse when
   sourced. Run exactly:
   `if bash ops/m010028/gate-a-switch.sh --plan P --approval-record R --approval-sha256 H; then echo "GATE A: PASS"; else echo "GATE A: STOPPED (exit $?)"; fi`.
   Exit 2 means the service was not stopped; the message states whether
   managed-environment installation was attempted and may need inspection.
   Exit 3 means the service may have been stopped; the script prints the
   return command.
5. **Bounded readiness.** After start, readiness uses one monotonic plan
   deadline, reserving 0.1 s for timeout cleanup. The remaining budget bounds
   `systemctl show`, loopback connection and response reads, and retry sleeps.
   The health endpoint must respond directly with a successful HTTP status;
   redirects are not readiness. Operating-system scheduling and local unit-file
   reads are not hard real-time guarantees. It fails fast on:
   - a wrong unit path, hash, or drop-in;
   - a failed state;
   - any authority or binding field that differs from the plan.

   A unit reported `active` with no listener is not success.
6. **Return from a fresh SSH session.** `bash ops/m010028/gate-a-return.sh
   --plan P` works from a new session without inherited shell state. It
   verifies the saved legacy unit and the held paths before changing
   anything. After restoring the unit, it requires legacy health, a hold
   that is still unchanged or shows only expected activity, and an inactive
   freeze.
   `gate_a.py held-check` reports one of three outcomes:
   - `UNCHANGED`: every held hash and the full inventory match.
   - `EXPECTED_ACTIVITY`: only the WAL-index (`-shm`) content differs, which
     ordinary readers rewrite. Lock-file `flock`/mtime activity is not
     compared. The database, WAL, other held files, and every path's type,
     owner, mode, and inode are unchanged. The return proceeds.
   - `COMMIT UNCERTAIN` (exit 4): the database or WAL content changed, any
     path appeared or disappeared (for example a rollback journal), any
     owner, mode, or inode changed (including a recreated `-shm` or lock
     file), or a held path could not be read. A WAL-mode commit must change
     the WAL or database, so only this outcome can hide a commit. The return
     refuses before any change. If this happens after a switch, the switch
     exits 4 and does not offer the return. Pause writers and use reviewed
     forward recovery.
7. **Publisher binding before the first R4 backup.**
   `gate_a.py publisher-binding --plan P` must pass before any R4 backup timer
   or manual backup. It checks:
   - that the installed backup env binds only to the R4 candidate and its
     coverage path;
   - the hashes of the backup and monitor units and their timers;
   - that the units have no drop-ins;
   - that `ReadWritePaths` stays within the allowed roots;
   - that no other `repo-control-backup*` unit is active or enabled.

   Product Owner decision (2026-11-18): the new R4 backup env only is
   installed as `root:chuck 0640` at the candidate-specific path
   `/etc/repo-control/backup-m010028-r4.env`. The check enforces that
   decision's conditions:
   - every ancestor directory is root-owned and writable only by root;
   - the file holds exactly the 12 approved non-secret path, identity,
     mount-source, and age bindings, with no credentials or unknown keys.

   Existing backup env files and the NAS credentials are unchanged and stay
   root `0600`.

The fixture suite is
[test_m010028_gate_a.py](../../tests/test_m010028_gate_a.py). It stubs sudo,
install, and systemctl, fakes root ownership, and uses a temporary loopback
sshd. It does not exercise the live privileged path.

### M010.028 R7 failed-start investigation and corrective design

Gate A on 2026-11-18 did not reach Repo Control application startup. The
reviewed R7 unit named
`/home/chuck/.cache/repoctl/m010.028-r7/scanner` in both
`--scanner-cache-root` and `ReadWritePaths`, but preparation created and
validated only its parent. systemd therefore failed mount-namespace setup
with `226/NAMESPACE`. `Restart=on-failure` caused fourteen attempts before
bounded readiness stopped. The guarded return restored the saved legacy unit;
legacy health reported `legacy_files` with a null managed binding, and the R7
database, WAL, and held inventory remained unchanged. R7 is source-stale
evidence and must not be repaired or retried.

The replacement proposal is R8. Its browser environment, backup environment,
managed unit, backup/monitor services, and 15-/five-minute timers are new
R8-only files under `ops/` and `ops/systemd/`. They are not installed, and no
R8 root, candidate, binding, hold, backup, restore, plan, or approval exists.
The R8 managed unit includes `StartLimitBurst=1` and the exact R8 scanner
child in both ExecStart and `ReadWritePaths`; the child must be created and
verified as `chuck:chuck 0700` before capture. `systemd-analyze verify` passed
for all five R8 units, with only the unrelated pre-existing Synology
`/var/run` warning. Syntax success is not runtime proof; the static contract
and transient namespace probe remain mandatory before capture.

This was not an SQLite, NAS, backup, or data-parity failure. It had two causes:

1. **Direct implementation error:** the R7 preparation checklist lost a known
   prerequisite. M010.027 had explicitly created the scanner child, but the
   copied R7 root list named only the parent. The unit and preparation steps
   were maintained separately, so they drifted.
2. **Validation-design error:** `systemd-analyze verify` proved unit syntax,
   not runtime path existence. The Gate A fixtures made their `systemctl`
   stub report `active` and served health from an independent stub; they never
   attempted systemd mount namespacing. The isolated candidate process also
   bypassed the production unit sandbox. Consequently, all tests could pass
   while the exact production unit could not start.

The durable B6 correction is in `ops/m010028/gate_a.py`. Every preflight now
parses the hashed managed unit and binds its `ExecStart` arguments to the
approved plan. It requires the exact database, WAL, SHM, lock, and scanner
paths in non-optional `ReadWritePaths`; verifies all exist with the right
file/directory type; requires the candidate scanner directory to be owned by
the service user/group at mode `0700`; checks the pinned runtime, repository,
registry, loopback endpoint, state root, environment, and sandbox hardening;
and requires `StartLimitBurst=1`. This runs before `sudo -v`, after
authentication, and immediately before service stop. A missing scanner path
now refuses while legacy is still live, and an early namespace defect cannot
produce a restart loop.

The regression suite contains negative cases for the exact R7 omission,
optional or mismatched `ReadWritePaths`, incorrect scanner permissions, and
an unbounded restart policy. `gate_a.py sandbox-probe --plan P` supplies the
production-shaped namespace rehearsal: when separately run by the operator as
root, it uses a collected transient oneshot with the reviewed User, Group,
WorkingDirectory, `NoNewPrivileges`, `PrivateTmp`, `ProtectHome`,
`ProtectSystem`, and exact `ReadWritePaths`, but executes only `/usr/bin/true`
with `Restart=no`. It therefore exercises systemd's real mount-namespace
setup without starting Repo Control or accessing the application endpoint.
Run it on the replacement candidate before final capture; a
`226/NAMESPACE` or any other failure is a stop. The final candidate is a data
refresh, not the first integration test.

Two operator-process findings also apply to the next attempt:

- The R6 and R7 backup wrapper inferred oneshot completion from volatile
  `ExecMain*TimestampMonotonic` properties. A completed/garbage-collected
  oneshot can expose zeros, so the wrapper reported STOP even though the
  journal and recovery-point verification proved the backup succeeded. Use
  blocking `systemctl start <exact-backup.service>` and judge success from its
  exit status plus the bound recovery-point verification; do not poll those
  timestamps and do not repeat a verified backup or restore.
- Gate B must be fully reviewed before the next final capture: exact approval
  binding, the exact root-only hold-release operation, one named canary,
  read-only resolution of ambiguous commit status, post-canary backup/restore,
  and the forward-recovery boundary. Do not treat a successful Gate A as
  permission or sufficient tooling for Gate B.

No further live attempt is ready until a replacement unit containing the
one-attempt limit has passed the production-shaped sandbox rehearsal and the
Gate B operator path above is concrete and tested. The 20-minute configured
backup limit is intentionally stricter than the prompt's 45-minute Gate B
maximum; it is not a conflicting weaker guard.

### M010.028 Gate B exact-plan procedure

[gate_b.py](../../ops/m010028/gate_b.py) and
[gate-b-canary.sh](../../ops/m010028/gate-b-canary.sh) supply the previously
missing production Gate B boundary. They are preparation artifacts only until
an exact replacement candidate has passed Gate A; their existence is not Gate
B approval and they have not released a live hold or made a managed write.

After a successful read-only Gate A, `gate_b.py build-plan` reads the exact
Gate A plan and refuses unless live health reports that managed binding, the
held inventory is unchanged, source parity is current, and publisher/recovery
coverage is valid. It binds:

- the exact Gate A plan and both Gate B helper/runner hashes;
- a Gate B window of at most four hours;
- exactly four root-owned, service-group targets by path, device, inode,
  owner, group, type, held mode, and released mode: `prepared`, DB, WAL, and
  SHM;
- one unique nonce-bearing `coder-test` payload; and
- one O_EXCL canary evidence path.

`gate_b.py review` is data only. It prints every permission transition, the
one canary payload, the significance of crossing the first-write boundary,
and the exact phrase
`GATE B APPROVED plan_sha256=<sha> window=<start>/<end> canary_nonce=<nonce>`.
Approval recording uses an O_EXCL mode-0400 record. The runner verifies that
approval and the full held/source/coverage/publisher/live binding before and
after authentication. The root action re-verifies the approval and exact
metadata, then applies only the three required mode changes; it performs no
recursive operation and refuses a symlink or replaced inode.

Immediately before invoking the canary, the unprivileged path again verifies
source parity, recovery freshness, publisher binding, managed health, the
window, and all released metadata. It calls `repoctl coder-test` exactly once
against the explicit candidate state root and coverage path, captures the
returned event ID, and reads that exact ID and payload back through
`coder-status`. Any failure after invocation is `COMMIT UNCERTAIN` (exit 4):
never rerun the canary or use the legacy return; resolve the unique nonce and
event read-only under forward recovery.

After a confirmed canary, the runner obtains the exact backup service from
the approval-bound Gate A plan and uses blocking `systemctl start`. It does
not inspect volatile oneshot `ExecMain*TimestampMonotonic` properties. The
new recovery point must be candidate-bound and no older than the committed
event. The helper then restores that exact manifest to a new separate target
and opens it read-only to require the exact canary event ID, type, summary,
source, and result. A post-write backup or restore failure stops under forward
recovery and never repeats the canary.

The fixture suite is
[test_m010028_gate_b.py](../../tests/test_m010028_gate_b.py). It proves exact
approval binding, root-only and inode-bound release, one invocation, exact
event readback, commit-uncertain no-retry behavior, blocking backup startup,
newer recovery coverage, and exact restored payload. sudo, systemd, the
canary CLI, and the restored database are isolated fixture substitutes; the
next candidate still requires the prompt's separately approved live gates.

### M010.028 R8 live cutover acceptance (2026-11-19)

R8 is the accepted production authority. The final source inventory SHA-256
is `60c72880060c6afdb9a9e5b30f3fa500751429254f765943fa19e6655e4c0e7a`;
the isolated activation used plan
`7b81828e3b4cbca0136a15c1974463b1108c73aab77728bc9cd701a52c57ffd2`,
generation `63137936ea4a4f7db30c6b6b2ea4c59b`, and database identity
`5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17`.
Import parity, SQLite integrity, foreign keys, the selected M010.028 prompt,
and all 72 captured workflow events passed. The exact production-shaped
transient namespace probe passed before the hold; it started no Repo Control
application process. The final hold covered 22 unchanged paths with protected
DB/WAL and only the reviewed SHM/lock exceptions.

The first R8 NAS backup completed successfully at
`2026-11-19T01:02:28.721841+00:00`; a distinct restore passed integrity,
foreign keys, binding, approval, selection, and 72-event chronology in 3.3703
seconds. The one-time wrapper nevertheless printed STOP because it had
accidentally copied the obsolete R7 `ExecMain*TimestampMonotonic` poll even
though this guide and the tested Gate B runner already required blocking
`systemctl start`. systemd garbage-collected the completed static oneshot and
returned zero timestamps. Read-only journal, service-result, recovery-record,
manifest, and invocation-ID evidence proved exactly one successful firing, so
the backup was not repeated. The missing restore was completed against that
exact recovery point, and the local R8 wrapper was corrected to blocking
`systemctl start`. This was a runbook-generation error, not an SQLite, NAS,
publisher, or production-service failure.

Gate A used plan
`dbad28f71582671a8997ab7acdf2c8c50093ef9aba42ddf84803ae010233db0d`.
The exact Product Owner approval passed every pre-stop revalidation. Live
health and an independent CLI with the explicit candidate coverage binding
both reported `sqlite_managed`, the exact R8 generation/database, selection
`4b21850796c04bd8953f07ca2e753ef6@105`, and the approved prompt revision.
The hold remained unchanged and Gate A made no application write.

Gate B used plan
`9a9e9605536fc340a7881da7806deb0db4e1e88127e01d87f6563fd62b371f16`
and nonce `64e3afdeb9ee03fe5df9120fc6ebc97a`. It released only the four
device/inode-bound R8 targets and committed exactly one nonbinding
`test_result_recorded` event,
`91a0e61c43854d2bb8b0586d7b42fd8f`. Read-only live status matched its exact
summary, source, and result. A blocking post-canary backup verified at
`2026-11-19T01:21:56.329278+00:00`, and a separate restore contained the exact
event with integrity `ok` and zero foreign-key violations. No legacy source
artifact or Git state changed during either gate.

Only the R8 production timers are enabled. The five-minute monitor fired at
`01:25:03Z` and completed successfully; it fired successfully again at
`01:30:03Z`. The 15-minute backup fired at `01:30:03Z` and completed with
integrity `ok`, zero foreign-key violations, and fresh coverage at
`2026-11-19T01:30:06.453200+00:00`. Its manifest is
`repo-control-20261119T013004095578Z-d8af08d2df23.manifest.json`; its database
SHA-256 is `75290d6962cea09f2a030ee67ac1c7b3aed1dc5b5e21b977f06741181f9ab669`.
Direct immutable inspection of that verified NAS payload found the exact
canary as workflow event 73. The next scheduled boundaries were monitor
`01:35Z` and backup `01:45Z`. The existing Architect tunnel returned active
and connected to the managed service.

The first-write boundary has been crossed. The saved legacy unit and earlier
snapshots remain recovery evidence, but the pre-write legacy return is no
longer valid; any incident now uses reviewed forward recovery from the managed
database/WAL and verified snapshots. Normal managed writers may resume. Do
not re-enable an older publisher, import file-backed workflow state over this
authority, or use the Gate A return procedure.
