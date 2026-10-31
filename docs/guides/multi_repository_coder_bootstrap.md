# Multi-Repository Coder Bootstrap Preparation

## Status and design choice

This guide describes a read-only preparation path. A generated registration is
a preview, not an installed or trusted hook, and it does not demonstrate
multi-repository host delivery.

The selected design is a thin per-repository registration that invokes the
trusted Repo Control II virtualenv and reuses the central Copilot or Codex
adapter. A versioned repository-local adapter bundle was considered and
rejected: it would duplicate authority-sensitive validation and require each
target to track source updates. The central adapter invokes the canonical
`repoctl.cli coder-context --repository <event cwd>` command on every hook
event. It confines the event worktree to the expected repository root and
compares the canonical result's repository key, ID, and root with the
registration's expected identity. The CLI remains responsible for resolving
the managed repository and its independent selection, prompt, and lifecycle.

Relevant implementation locations:

- `src/repoctl/copilot_bootstrap.py` and `src/repoctl/codex_bootstrap.py` —
  provider event handling and identity checks.
- `src/repoctl/bootstrap_preview.py` and
  `repoctl coder-bootstrap-preview` — deterministic provider registration
  preview; no target files are written.
- `.github/hooks/repo_control_context.json` and `.codex/hooks.json` —
  existing Repo Control II production registrations. This preparation does
  not rewrite them.

The preview binds a registration to an expected repository key, repository ID,
and absolute root. These values constrain the hook but grant no authority. The
adapter retrieves current canonical context each time; it does not cache a
milestone, prompt hash, lifecycle, decision, or approval. A target selection
that does not resolve to the expected managed identity fails closed.

## Generate a preview

Run the command from the Repo Control II checkout using its trusted Python
environment. Supply an identity tuple from the applicable managed-repository
record or from that target's separately authorized canonical Repo Control
context; do not guess paths or identities.

```bash
.venv/bin/python -m repoctl.cli coder-bootstrap-preview \
  --repository-key fixture-repo \
  --repository-id fixture-repo--0123456789ab \
  --repository-root /absolute/path/to/fixture-repo
```

The example values are placeholders. The command emits JSON for the Copilot
`.github/hooks/repo_control_context.json` and Codex `.codex/hooks.json`
registration shapes. The output uses shell-escaped arguments for paths with
spaces and reports the trusted interpreter and source root. It does not read
or write the target worktree, install files, merge existing hooks, approve a
repository, or satisfy Codex project trust. Review the output before any
separately authorized target operation.

The generated command must run with a Python environment that can import the
trusted central `repoctl` source. The default is this checkout's
`.venv/bin/python`. If that checkout or environment moves, regenerate the
preview and re-review the new executable and source paths. Do not copy the
adapter source into a target repository or substitute an unverified `repoctl`
installation.

## Per-repository onboarding checklist

Only the Product Owner can start the target-repository work. For a live pilot:

1. In Repo Control, the Product Owner explicitly selects the target repository
   and verifies its exact key, ID, canonical path, selected milestone, prompt
   hash, lifecycle, authorization, and open conditions. The target must have
   its own approved milestone. A Repo Control II approval does not authorize
   target-repository changes.
2. The Product Owner approves any target registration or procedure changes.
   Record and preserve the target's existing hook configuration before
   proposing a merge. Review the expected identity tuple and trusted
   central Python/source paths; never replace other hooks wholesale.
3. Install or merge only under that target milestone's authority. Use normal
   host project discovery and trust review. For Codex, accept only its normal
   project trust prompt after checking the exact target and reviewed files.
   Never edit trust hashes, bypass trust, install a user/global hook, or
   silently copy files.
4. Start independent fresh Copilot and Codex sessions in the exact target SSH
   worktree. Each must retrieve fresh canonical context and show the target's
   own key, ID, path, selected prompt hash, lifecycle, and authorization.
   Verify explicit no-authority behavior for a wrong/unmanaged worktree and
   WAITING, REVIEW, or CLOSED conditions. A CLI-only smoke test does not prove
   host discovery or first-response delivery.
5. Record provider-specific host evidence. Claim delivery only when correlated
   host-native evidence demonstrates the hook ran and the expected context was
   delivered. Model statements, a generated preview, a trusted config file,
   or a successful standalone CLI invocation are not host-delivery proof.

The preview proves only that stable provider-shaped JSON can be rendered with
the supplied identity tuple and correctly escaped invocation arguments. It
does not prove the tuple matches an active registry, that the interpreter is
available in another host, that the project hook is discovered or trusted, or
that a fresh session receives the result.

## Failure classification and rollback

- `worktree_mismatch` or `repository_identity_mismatch`: stop and review the
  selected worktree and registration identity. Do not relax the binding.
- `INVALID_NO_AUTHORITY` from an unavailable, failed, malformed, or timed-out
  canonical CLI: stop; repair the trusted runtime only under its own approved
  scope. Do not fall back to cached context or a different repository.
- `NO_AUTHORITY`: follow the live Repo Control lifecycle and resolve its
  condition through the target repository's normal authority workflow.
- No host event/receipt: treat discovery or delivery as unverified. Do not
  infer that the adapter ran or broaden trust.

For rollback, the Product Owner reviews and restores the exact pre-change
target registration (or removes only the newly added entry), preserving all
unrelated existing hooks. Revoke project trust only through the host's normal
user-facing workflow if appropriate. Recheck that the target hooks match the
reviewed baseline. The Coder performs no Git mutations; target selection,
approval, rollback, and all Git actions remain Product Owner-owned.

## Authority boundary

This preparation has no Vocab App installation or live-pilot authority. Any
future pilot requires a separately selected Vocab App repository, its own
approved milestone, target-specific Product Owner approval, normal trust
review, and independently recorded host acceptance. Until then, report
portability preparation and live second-repository acceptance separately.
