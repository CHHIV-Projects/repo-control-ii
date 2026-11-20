# M010.030 reproducible R8 repair

This procedure repairs the existing managed R8 installation. It does not
remigrate data, select a milestone, alter SQLite directly, switch authority,
or return to legacy files.

Before the live repair, `production-shaped-fixture.sh start` creates a wholly
disposable repository and state tree, runs the atomic selection/SQLite/narrow
Git namespace probe, and starts a loopback browser on port 8876 under the same
systemd restrictions. The Product Owner stages `browser-proof.txt` and commits
it with the displayed exact message. `production-shaped-fixture.sh verify`
then inspects the index and commit read-only; `stop` stops only the fixture.

The versioned inputs are:

- `ops/repo-control-managed-cli-r8.json`: exact non-secret CLI authority and
  recovery binding;
- `ops/systemd/repo-control-m010028-r8-selection-pointer.conf`: the one
  selection-pointer directory;
- `ops/systemd/repo-control-m010028-r8-sqlite-runtime.conf`: the existing R8
  prepared database directory;
- `ops/systemd/repo-control-m010028-r8-git.conf`: only Repo Control II's
  `.git` directory, needed by the existing Product Owner Stage and Commit
  routes. The worktree and the rest of `/home` remain read-only.

After Product Owner authorization, run exactly one install command:

```bash
sudo /home/chuck/projects/repo-control-ii/ops/m010030/reproducible-r8-repair.sh install
```

It refuses a changed selection-directory baseline, captures the prior
drop-ins, binding, ACL and effective unit under
`/root/repo-control-m010030-before`, installs root-owned configuration, reloads
systemd, performs the one required restart, and verifies browser/ordinary CLI
identity plus both production coverage timers. Expected final lines are:

```text
READY sqlite_managed generation=63137936ea4a4f7db30c6b6b2ea4c59b database=5a41c08550c6de11fe777e94ed1ad29f19ed9e739c25b9ad1f2856d241018a17 selection=<current-selection-id>@<revision>
PASS R8 browser/CLI binding and coverage timers
```

Read-only re-verification uses `verify`. If only this configuration change
must be undone, use `undo`. Undo restores the captured drop-ins, binding and
ACL, performs one restart, and keeps the service on the same managed R8
database. It must not be replaced by the obsolete Gate A legacy-return path.
