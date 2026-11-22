# M011.001 shared-procedure transaction compatibility

M011.001 separates durable procedure journals from the root-managed R8 state
parent and keeps the continuously running browser service unable to modify any
worktree. It does not change SQLite, the repository registry, milestone
selection, backup policy, Git permissions, or the published procedure bundle.

## Reviewed design

The explicit transaction root is the existing private directory
`/home/chuck/.local/share/.repoctl-procedure-transactions`, owned by
`chuck:chuck` with mode `0700`. Existing terminal journals remain in place.
An absent, unsafe, inaccessible, or symlinked configured root reports
`STORAGE_UNAVAILABLE`; only malformed or integrity-invalid journal/request
content reports `CORRUPT`.

Browser Preview and Review are read-only. The second Product Owner confirmation
stores a private, exact request bound to repository key/ID/path, active
selection generation, release plan or recovery journal, target hashes, and the
short-lived confirmation token. It does not write the worktree. The page then
shows the one operator command for that repository:

```text
sudo systemctl start repo-control-procedure-update@<repository-key>.service
```

The inactive template runs as the same `chuck` UID, revalidates the sealed
request, and exits after one Apply, overlay initialization, or Recover. There
is no daemon and no privilege transition.

| Component | Persistent writable paths | Temporarily writable paths | Explicitly protected |
|---|---|---|---|
| `repo-control.service` | private transaction root plus its pre-existing R8 paths | none | all registered worktrees remain under `ProtectSystem=strict` / `ProtectHome=read-only` |
| selected one-shot instance | private transaction root and R8 lock directory | only the selected registered repository root, for atomic parent-directory replacement | selected `.git`, `docs/context`, and `docs/milestones`; every other repository remains read-only |

The selected root exception is temporal because root `AGENTS.md` replacement
requires rename permission on the repository directory. Systemd cannot grant
atomic replacement of that filename without granting its parent directory.
The request validator and generated-target allowlist constrain what the
one-shot actually writes. Apply and Recover never stage, commit, push, or alter
Git HEAD/index.

If execution dies before a journal is created, its sealed request can be
started again. If it dies after a recoverable journal exists, the UI shows that
journal rather than claiming the original request is merely pending. A newly
reviewed Recover may supersede only the one matching interrupted `EXECUTING`
request; unrelated, multiple, changed, or tampered requests fail closed.

## Disposable production-shaped proof

`production-shaped-procedure-workflow.sh run` is a single automated fixture.
It creates three disposable registered repositories at exact procedure 1.4.0,
uses a root-owned managed-state parent, starts a browser under the effective
read-only policy, and exercises real browser Review/Confirm requests. It then
runs bounded transient same-UID executors for:

- an uninterrupted 1.4.0 to 1.5.0 Apply;
- a death immediately after durable `PREPARED`, explicit Recover, exact legacy
  verification, then a fresh successful Apply;
- a death after the first target replacement, explicit Recover, exact legacy
  verification, then a fresh successful Apply.

Each repository is independently verified `CURRENT`. The fixture compares
protected document hashes and Git HEAD/index snapshots throughout. Its browser
namespace proves a worktree scratch create fails while a private-store scratch
create/sync/remove succeeds. It bind-mounts the disposable registry over the
canonical registry path only inside fixture namespaces; the host registry and
live service are untouched.

Run only the disposable proof with:

```bash
sudo /home/chuck/projects/repo-control-ii/ops/m011001/production-shaped-procedure-workflow.sh run
```

Expected final output begins with individual PASS lines and ends with:

```text
PASS complete three-repository browser Review/Confirm, sealed one-shot Apply, two interruption/recovery points, independent CURRENT verification, isolation, protected paths, and Git HEAD/index boundaries
EVIDENCE preserved at /var/lib/repo-control-staging/m011.001-procedure-fixture
```

`stop` stops only the disposable browser service and preserves evidence.

## Deliberate live rollout

The fixture does not authorize deployment. After separate Product Owner
authorization, the reviewed operator sequence is:

```bash
sudo /home/chuck/projects/repo-control-ii/ops/m011001/install-procedure-executor.sh preflight
sudo /home/chuck/projects/repo-control-ii/ops/m011001/install-procedure-executor.sh install
```

`preflight` is read-only. `install` captures the five target files and current
effective service properties under `/root/repo-control-m011001-before`, installs
only the versioned main-service drop-in, one-shot template, and three exact
instance drop-ins, reloads systemd, and performs exactly one
`repo-control.service` restart when configuration changed. It does not start an
executor instance or Apply to a real repository.

Post-restart verification requires the exact managed R8 generation/database,
both recovery timers active and enabled, the private store binding in the
effective main-service sandbox, no active/enabled executor, and truthful rows
for Repo Control II, Vocab App, and Photo Organizer. Vocab and Photo remain at
1.4.0 until separately confirmed by the Product Owner.

Read-only re-verification uses `verify`. If only this configuration must be
undone, `undo` restores the captured files, moves newly introduced files into
the private backup rather than deleting them, reloads systemd, performs one
restart, and rechecks the unchanged managed R8 service. It does not restore a
legacy authority, edit SQLite, or alter the private journal history.

## Reopened-milestone authority correction

The bounded correction separates two previously coupled facts:

- `UNVERIFIED_IMPORTED_HISTORY` continues to deny milestone implementation and
  new procedure-release preparation, but does not invalidate an independently
  hash-verified published procedure release.
- `RELEASE_INTEGRITY_UNAVAILABLE`, `RELEASE_AUTHORITY_UNAVAILABLE`, canonical
  bundle corruption, or an actively authorized release preparation still block
  Review Apply.

The existing repository/selection generation, exact plan digest, overlay,
short-lived confirmation token, sealed request, one-shot executor, and recovery
checks are unchanged. No historical prompt or guidance is approved by this
correction.

The UI audit removed only duplicate identity/self-attestation fields. The
deliberate named action remains the Product Owner-reported decision; Repo
Control still does not authenticate identity.

| Surface | Before | After | Deliberately retained |
|---|---|---|---|
| Closed milestone reopen | Named Reopen action plus generic “I am the Product Owner” checkbox | Named Reopen action | exact repository/milestone/phase/event-count binding and required reason/target phase |
| Pending Architect dispatch approve/reject | Named action plus generic Product Owner checkbox | Named action | exact dispatch fingerprint, repository generation, and route action |
| Accepted closeout export | reviewed preview plus generic Product Owner checkbox plus named Export action | reviewed preview plus named Export action | preview fingerprint, accepted revision/hash, Git drift checks, and server-side managed/fixture boundary |
| Imported prompt/guidance reconciliation | content-specific new-decision checkbox | unchanged | exact imported bytes/fingerprint acknowledgement; historical approval remains unknown |
| Close despite newer guidance | content-specific override checkbox | unchanged | listed dispatch IDs and required override reason |

The correction rollout changes no configuration or data. It verifies the exact
six runtime source hashes, captures the prior committed bytes, performs exactly
one service restart, and rechecks the managed R8 identity and recovery timers:

```bash
sudo /home/chuck/projects/repo-control-ii/ops/m011001/rollout-authority-correction.sh preflight
sudo /home/chuck/projects/repo-control-ii/ops/m011001/rollout-authority-correction.sh rollout
```

`preflight` is read-only. Do not run `rollout` without separate explicit
Product Owner authorization. If and only if rollback is later directed, `undo`
restores the six captured pre-correction source files and performs one restart;
it does not edit SQLite, the recovery record, registry, selection, ACLs,
systemd configuration, procedure bundle, or Git metadata.
