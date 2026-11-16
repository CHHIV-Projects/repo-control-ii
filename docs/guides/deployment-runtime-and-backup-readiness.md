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
