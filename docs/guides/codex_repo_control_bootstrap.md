# Codex Repo Control Session Bootstrap

**Scope:** Repo Control II only
**Contract:** `repo-control.continuity.v1`
**Status:** experimental adapter and deterministic checks are present; no
Codex host hook is installed. Discovery, trust approval, invocation and
first-response delivery in the selected VS Code Codex host remain
**NOT VERIFIED**. Automatic startup for that host is **NOT PASS**.

## Gate A evidence and selected host

The Product Owner confirmed through the UI that the intended target is the
Codex coding experience in the VS Code Windows client connected to this Linux
Remote SSH worktree. This is operator-reported evidence; no screenshot or
Windows-client build was available to the Coder.

Read-only inspection of the remote extension installation found:

- `openai.chatgpt` version `26.1021.22027`, installed under
  `/home/chuck/.vscode-server/extensions/`.
- The extension bundles a Linux runtime at
  `bin/linux-x86_64/codex`, reporting
  `codex-cli 0.164.0-alpha.11.1`. No standalone `codex` executable is on
  `PATH`; the bundled runtime is not being substituted as a separate host.
- The project venv imports `repoctl` outside the worktree and is the fixed
  interpreter configured below.
- Before this change, the repository had no `.codex/` directory. The private
  user `~/.codex/config.toml` existed; a metadata-only parse found no user
  `[hooks]` table and no `features.hooks` override. No user `hooks.json` was
  present. Its other contents were not disclosed or copied.

Official Codex hook documentation describes project-local
`<repo>/.codex/hooks.json` / `.codex/config.toml`, `SessionStart`, project
trust, per-hook hash review, event input on stdin and context injection via
`hookSpecificOutput.additionalContext`. The official VS Code hooks guide
distinguishes Codex from the Copilot and Local hook implementations. Those
documents establish a documented candidate, not runtime support in this
installed VS Code Codex session.

The Product Owner reports that the selected VS Code Codex Hooks settings page
for this SSH worktree still displayed “No hooks found” after refresh. A bounded
read-only check of the remote extension and available logs found no hook
discovery/trust/invocation evidence. Effective Windows-client policy and trust
state remain unknown. The VS Code issue linked below discusses project-hook
discovery/trust problems in the Codex Agents window; its discussion is not
proof that a fix is present in this installed host.

References:

- [OpenAI Codex hooks](https://developers.openai.com/codex/hooks/)
- [VS Code agent hooks](https://code.visualstudio.com/docs/agent-customization/hooks)
- [OpenAI Codex IDE extension](https://developers.openai.com/codex/ide/)
- [VS Code issue #331733](https://github.com/microsoft/vscode/issues/331733)

## Candidate mechanism decision

| Candidate | Decision | Evidence boundary |
|---|---|---|
| Codex native `SessionStart` command hook in `.codex/hooks.json` | Documented candidate; not installed | The selected VS Code Codex UI reported no hooks. No supported repo-only trust/review path or host invocation was established. |
| `AGENTS.md` or discovered Skills | Not an automatic bootstrap | Instructions can guide the model but do not independently prove a live command ran or context was consumed before the first response. No conflicting `AGENTS.md` was added. |
| Existing Copilot `.github/hooks` registration | Rejected for Codex | It is a Copilot shared-harness hook and is unchanged. |
| Standalone Codex CLI | Not the selected host | No standalone CLI is installed/on `PATH`; invoking the extension-bundled executable for `--version` is inventory only, not host acceptance. |
| User/global or enterprise hooks | Not selected | Would broaden scope and could affect other repositories. No such configuration was created or changed. |

## Repo-local implementation

No `.codex/hooks.json` or other project Codex hook registration is shipped.
The experimental adapter is not host-integrated. The following is illustrative
only; do not install or activate it in this repository or another project
without a future separately approved milestone that verifies the selected
host's discovery and normal per-hook trust flow:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.codex_bootstrap",
            "timeout": 20,
            "async": false
          }
        ]
      }
    ]
  }
}
```

This example is not an active runtime configuration. Do not copy it to
`.codex/hooks.json` as a workaround for the selected UI's empty hook list.
No global hooks, trust policy, sandbox, permission, account or model settings
were changed.

`src/repoctl/codex_bootstrap.py` parses the Codex event, validates the exact
managed repository key, ID and canonical path, then reuses the established
Copilot adapter's fresh canonical `coder-context` lookup and validation. It
maps validated fields into a distinct Codex output:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "{\"schema\":\"repo-control.continuity.v1\", ...}"
  }
}
```

The inner envelope includes repository/worktree identity, selected
milestone/prompt path and hash, live lifecycle/authorization, open-condition
references, approved-scope references, and event provenance. It does not
include raw prompts, transcript paths/bodies, dispatch bodies, credentials or
shell-provided identity. It handles documented Codex SessionStart sources
`startup`, `resume`, `clear`, `compact`, and `fork`; this source mapping is
fixture-tested, not evidence that the selected VS Code target delivers each
event.

The context has an 8,192-byte bound and fails to a compact
`INVALID_NO_AUTHORITY` instruction rather than truncating required data. The
current live CLI/adapter smoke output measured 2,338 UTF-8 bytes and 585
estimated tokens using `ceil(UTF-8 bytes / 4)`. This is a byte heuristic, not
model-token telemetry. Codex's documented default per-handler context
threshold is approximately 2,500 tokens; the hook leaves
`additionalContextLimit` unset and produces a compact payload.

On canonical CLI error, malformed output, identity/prompt mismatch or
incoherent lifecycle/authorization, the adapter emits `INVALID_NO_AUTHORITY`
or `NO_AUTHORITY` with a stop instruction and no successful authorization.
If the host does not execute the hook, refuses its trust, or fails before
context reaches the model, this adapter cannot enforce a stop. Codex's
permission/sandbox policy is separate; no tool/file enforcement is claimed.

## Host acceptance status

Codex documents project hooks as active only for a trusted project and requires
review/trust of each non-managed hook definition hash; a changed definition
must be reviewed again. Do not bypass hook trust or modify trust policy. The
The selected VS Code host did not show a project hook after refresh, and no
normal repository-scoped review/trust path was verified. Therefore no real
host acceptance was attempted. The adapter's fixture and CLI results do not
prove discovery, execution or model delivery. Do not enable a receipt/marker
probe unless a future approved milestone first establishes visible
repo-scoped discovery and normal trust in the exact selected host.

Automatic startup for the selected VS Code Codex host is **NOT PASS**. Whether
this host/runtime supports the documented project hook path is **UNKNOWN**;
do not report it as universally unsupported. If some other supported host
later runs a hook, that is a separate host acceptance and cannot be
substituted for this target. Hook context is not fail-closed tool
authorization.

## Rollback and boundaries

The unverified `.codex/hooks.json` created during this milestone was removed
before closeout. The production Copilot hook and adapter are preserved. No
other repository, shared-procedure release, Repo Control selection/lifecycle,
Windows setting, host trust, permission, sandbox, or account configuration
was changed. No acceptance receipt or marker is active or was created. To
disable the experimental adapter entirely, its module/tests and this guide
may be removed only through normal Product Owner review and Git ownership.
No Git operation is authorized for the Coder.

Copilot genuine-resume behavior remains a separate **NOT VERIFIED**
dependency. A later cross-agent switch test should keep the same managed repo
and selected milestone while switching Copilot ↔ Codex; that is separate from
this fresh Codex startup acceptance. Any multi-repository deployment requires
new Product Owner approval.
