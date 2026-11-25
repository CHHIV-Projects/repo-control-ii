# Codex Repo Control Session Bootstrap

**Scope:** Repo Control II only

**Contract:** `repo-control.continuity.v1`

**Status:** Historical LIVE PASS for the specifically tested VS Code Codex
Remote SSH host. The M011.004 runtime-binding update below requires fresh
host trust review and cold-session acceptance; it does not inherit that PASS.

## Verified host and result

M009.004 verified repository-local automatic startup in the Product Owner's
Codex experience in the VS Code Windows client connected by Remote SSH to
`repo-control-ii [SSH: henderson-server1]` at
`/home/chuck/projects/repo-control-ii`.

The tested remote installation was:

- extension `openai.chatgpt` version `26.1021.22027`;
- bundled Linux runtime `codex-cli 0.164.0-alpha.11.1`;
- project trust for the exact canonical worktree;
- normal Codex UI per-definition review/trust, with no manual trust-hash edit
  or bypass.

The host discovered the repository definition under **From Projects**, showed
the exact `SessionStart` command, required a new review after each command
change, executed the trusted hook synchronously, and inserted its output into
the thread as a developer-context item before model response generation.

An instrumented cold start correlated a private invocation receipt, a unique
marker in the model-facing context, and the same marker in the first assistant
response. After removing instrumentation and re-trusting the static
definition, a separate production cold start persisted a model-facing
`repo-control.continuity.v1` item at `2026-10-29T03:52:00.381Z`. It contained
the exact repository identity, M009.004, `IN_PROGRESS`, current authorization,
`source=startup`, and approved prompt hash before the exact `Prompt ready.`
user item and before the first assistant response.

This is not a claim about another Codex version, host, workspace, repository,
VS Code tab, local session, or managed policy. The exact Windows VS Code build
and effective Windows-side policy were not independently available.

## Active repository registration

The shipped production definition is `.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/chuck/.venvs/repo-control-python-3.12.16-sqlite-3.51.3/bin/python -m repoctl.codex_bootstrap",
            "timeout": 20,
            "async": false
          }
        ]
      }
    ]
  }
}
```

The definition deliberately has no matcher, so the adapter can validate all
documented Codex `SessionStart` sources: `startup`, `resume`, `clear`,
`compact`, and `fork`. Only real `startup` delivery was required and verified
by M009.004. Fixture coverage of other sources is not live host acceptance.

The command uses the qualified Coder environment's fixed absolute interpreter
and queries fresh canonical Repo Control state on every invocation. Both
adapters use their launching interpreter for the inner canonical CLI and
returned follow-up commands, preserving the virtualenv path rather than
selecting the checkout's `.venv` or resolving to the base Python executable.
The repository root still comes from the central adapter source and expected
identity arguments; choosing an interpreter does not change repository
authority. No milestone, prompt, authorization, marker, receipt path, or
current decision is embedded in the production hook.

M011.004 B3 corrects the former mismatch: the checkout's `.venv` links SQLite
3.45.1, below the 3.51.3 production floor, while the qualified environment
links SQLite 3.51.3. Launching an adapter with an unsupported interpreter must
still return invalid/no-authority context; no minimum-version check is
disabled. These registrations are repository-owned project definitions, not
artifacts of the shared-procedure release. This update does not alter system
Python, service units, databases, host trust stores, or published procedures.

M011.004 B4 updates the shared adapter for SQLite-managed authority. It requires
`information_store_authority: "sqlite_managed"`, an explicit
`prompt.authority_verified: true`, a well-formed current selection, UTF-8
content whose byte count matches `prompt.size_bytes`, and a trailing SHA-256
that matches the content. The document/revision portion of `prompt.identity`
is opaque; the adapter does not split or reinterpret IDs that may contain
colons. Approval and provenance remain the canonical producer's
responsibility. The current context projection does not expose
`authority_generation`, so the adapter does not invent or infer that value.
SQLite-managed authority may omit a materialized prompt file only after these
checks; file/ledger authority continues to require the existing bare hash and
on-disk verification. Neither adapter has a passing managed-authority native
cold-session acceptance result; provider delivery remains unverified.

## Adapter contract and authority boundary

`src/repoctl/codex_bootstrap.py` validates the Codex event, exact canonical
worktree, repository key and repository ID. It then invokes fresh
`repoctl coder-context` and emits:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "{\"schema\":\"repo-control.continuity.v1\", ...}"
  }
}
```

The bounded inner envelope includes repository/worktree identity, selected
milestone and prompt identity, lifecycle and authorization, open-condition
references, approved-scope references, and event provenance. It excludes raw
prompt bodies, transcript paths/bodies, dispatch bodies, credentials, and
trust tokens.

Canonical CLI failure, malformed output, identity or prompt mismatch, an
unsupported event, wrong worktree, or incoherent lifecycle/authorization
returns `INVALID_NO_AUTHORITY` or `NO_AUTHORITY` with a stop instruction. It
does not fall back to successful authorization. Context is bounded to 8,192
UTF-8 bytes and host output to 20,000 bytes.

The hook supplies point-in-time context; it grants no filesystem, shell, Git,
model, or lifecycle authority. Codex permission and sandbox enforcement remain
separate. If a host does not discover, trust, invoke, or deliver a hook, this
adapter cannot create a fail-closed tool boundary.

## Trust and safe changes

Project trust and hook-definition trust are distinct. Codex stores a
host-managed trusted hash for the exact project definition. Any change to the
command or hook shape must be reviewed again through **Codex Settings >
Hooks** in the correct SSH workspace. Never write `hooks.state` or
`trusted_hash` manually, copy a hash, bypass trust, or broaden this to a user
or global hook.

M009.004 temporarily appended `--acceptance-receipt` for one approved test.
That changed the definition hash and required separate UI trust. The adapter
generated a random marker and wrote bounded metadata to an owner-only Linux
state path. The test argument, receipt, and receipt directory were removed
before production verification. They are not active in the shipped command.

## Operation and verification

For the tested host, a normal fresh-session verification is:

1. Confirm Repo Control has the intended selected repository and milestone.
2. Confirm `.codex/hooks.json` shows the exact static command under **From
   Projects** and is trusted/enabled in the same Remote SSH workspace.
3. Open a genuinely new Codex session in that workspace.
4. Send exactly `Prompt ready.` with no attachment.
5. Confirm the response is based on the current repository, milestone,
   lifecycle, and authorization without manually copying context.

A model statement by itself is weaker evidence than a host thread record. For
diagnosis, inspect only minimal hook-event/context metadata and ordering; do
not export complete transcripts or private configuration. A successful direct
CLI smoke is adapter validation, not host acceptance.

## Disable and rollback

To disable automatic Codex startup for this repository, the Product Owner may
remove or disable `.codex/hooks.json` through normal repository review and use
the supported Codex UI if revoking the stored definition trust is desired. Do
not edit Linux user trust configuration directly. Removing the registration
does not require removing the adapter or its deterministic tests.

The existing Copilot hook remains separate and unchanged. Genuine Codex
resume/compact delivery, genuine Copilot resume, cross-agent switching, and
multi-repository portability remain separately unverified unless tested in
their own approved scopes.
