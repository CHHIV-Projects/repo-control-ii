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
