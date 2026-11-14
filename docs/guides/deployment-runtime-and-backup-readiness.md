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

The live database must stay on local storage. Proposed values in
`ops/backup.env.example` are not confirmed settings:

| Role | Proposed path / identity |
|---|---|
| Database | `/home/chuck/.local/share/repoctl-sqlite/repo-control-ii/information-store.sqlite3` |
| NAS mount | `/mnt/nas/repo-control` |
| NAS source | `//192.168.1.171/RepoControl` |
| NAS destination | `/mnt/nas/repo-control/repo-control-ii` |
| Local staging | `/home/chuck/.local/state/repo-control-backup/staging` |
| Separate local restore | `/home/chuck/.local/state/repo-control-backup/restore` |
| Job identity | `chuck` (`uid=1000` on the observed host) |

No dedicated Repo Control share or writable test target was mounted during
M010.014. The existing mounted shares are for other purposes; the Photo
Organizer share and every other existing share must remain untouched. The
smallest operator setup is to provision a dedicated Repo Control share and a
separate explicitly disposable test directory, confirm the exact share name
and SMB source, grant a dedicated share credential access only to that share,
and mount the production share at `/mnt/nas/repo-control`. Keep the mount
credential in a root-owned file with mode `0600`, outside Git and logs; the
service runs as `chuck` and receives only the mounted directory permissions.
Create the staging, restore, and destination directories with restrictive
permissions (`0700` directories, `0600` backup files), confirm the service
identity can access them, and confirm the restore path is local and separate
from both the live DB and NAS mount. Do not grant the service credential
access to Photo Organizer data.

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
unavailable it reports the mount mismatch and attempts the configured
independent alert hook. The hook receives the approved route identifier as
its first argument and alert text on stdin. The hook and destination must be
selected and tested by the operator; no approved route is currently
configured. A local journal entry or simulated hook is not proof that the
intended operator received an alert.

When a 20-minute alert fires, the operator should immediately verify the NAS
mount identity, restore backup job operation, and confirm a new verified
manifest. If coverage is not restored, follow the approved service procedure
to pause new writes before the newest verified copy reaches 60 minutes old;
keep writes paused until a fresh verified copy exists. The proposed monitor
checks every five minutes, but that schedule and response window do not prove
the one-hour loss limit. Restore duration, alert delivery, and actual NAS
behavior must be measured before relying on that objective.

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

Before any later cutover decision, the operator must first provide the
dedicated test target and approved alert route, then complete a real NAS
backup and restore rehearsal with integrity, foreign-key, selection,
representative workflow, and exact document-hash verification. Take a fresh
verified backup immediately before any separately authorized cutover.
