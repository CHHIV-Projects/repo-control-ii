# Coder Session Continuity and Live Authority Refresh

- **Contract:** `repo-control.continuity.v1`
- **Authority:** current Repo Control state, read through `repoctl coder-context`
- **Status:** provider-neutral Coder procedure; Copilot host observations are scoped separately below

## Purpose and authority boundary

A chat transcript, checkpoint, prior `AUTHORIZED` response, startup hook, or
model assertion is not current work authorization. Before implementation,
validation that changes repository state, or another consequential action,
the Coder obtains canonical context in the exact current managed worktree.
That observation is point-in-time evidence only; it is not persistent
permission.

Repo Control remains the sole authority for managed repository identity,
milestone selection, prompt identity, workflow phase, open conditions, and
approved scope. This contract does not create a registry, shadow lifecycle,
session authorization cache, or tool permission boundary. Host tool/file
permissions remain a separate control.

## Versioned observation shape

`repo-control.continuity.v1` is the minimum agent-neutral interpretation of
the existing read-only `coder-context` result. It is a contract, not a new CLI
wire format or independently persisted authorization record.

| Contract field | Canonical evidence |
|---|---|
| `repository.key`, `repository.repository_id`, `repository.path` | `repository.key`, `repository.repository_id`, `repository.path` |
| `worktree.cwd` | The current absolute worktree passed to `coder-context --repository`; resolve and compare with the canonical path. |
| `selection.milestone_id`, `selection.prompt_path` | `selection.milestone_id`, `selection.prompt_path` |
| `milestone.phase` | `milestone.id`, `milestone.phase` |
| `prompt.path`, `prompt.sha256` | `prompt.path`, `prompt.identity`; retain the canonical hash, do not hash an old transcript. |
| `authorization.authorized`, `authorization.status` | `authorization.authorized`, `authorization.status`, `authorization.message` |
| `conditions.open_questions`, `conditions.open_bugs` | `workflow.open_questions`, `workflow.open_bugs`, including identifiers and omitted counts in compact host output. |
| `scope.prompt`, `scope.approved_dispatches`, `scope.binding_lock_in` | Active prompt identity plus only applicable, approved dispatch references and the latest approved binding lock-in. Read the full prompt/guidance when needed; do not treat references as substitutes for it. |
| `observation.source`, `observation.observed_at`, `observation.id` | Host adapters may supply event provenance and a fresh observation ID/time. The current CLI response does not itself supply a universal freshness token; a caller timestamp is provenance, not Repo Control authority. |

Do not infer missing identity, selection, hash, lifecycle, authorization, or
scope. A host envelope may omit prompt and dispatch bodies, but must preserve
the required identity and stop condition fields or return an explicit invalid
result. The existing Copilot adapter's `repo-control.bootstrap.v1` maps these
canonical fields and is not a second authority.

## Refresh and stop procedure

1. In the exact worktree, run
   `.venv/bin/python -m repoctl.cli coder-context --format json --repository <absolute-worktree>`.
2. Require a successful command and valid context. Compare canonical repository
   key, ID and path, selected milestone and prompt path/hash, lifecycle,
   authorization and applicable approved scope against the active task.
3. Proceed only when the live context authorizes the active scope. `IN_PROGRESS`
   authorizes only the active prompt and binding approvals; correction mode is
   limited to the explicit correction.
4. On CLI failure/timeout, malformed or oversized output, invalid identity or
   selection, mismatched prompt hash, unsupported lifecycle, or incoherent
   authorization, stop. A transport or validation failure is
   `INVALID_NO_AUTHORITY`, never the last successful result.
5. On `WAITING`, `REVIEW`, `CLOSED`, or any other non-authorizing result, stop
   implementation and follow Repo Control's resolution/lifecycle procedure.
   `READY` is preflight-only. An old `AUTHORIZED` observation must never
   override current state.
6. Repeat the query after a resumed/reopened chat, compaction, subagent
   handoff, model/harness change, worktree change, or any interruption that
   could leave the agent relying on stale history, and before later
   consequential work. This is Coder procedure, not guaranteed runtime
   enforcement.

## Copilot event and refresh matrix

“Documented” describes published host/SDK capability, not proof that this
particular event ran. “Observed” requires an independent invocation receipt
and, for delivery claims, correlation with model-facing context. A UI banner,
chat action, or model report alone is insufficient.

| Trigger | Copilot shared-harness hook / evidence | Stale-response risk and refresh boundary | Authorization and fallback | M009.002 validation |
|---|---|---|---|---|
| Fresh Copilot session | `sessionStart` is the shared-harness start event. M009.001 has fresh-session host evidence for its then-installed target/version. A repo-local `SessionStart` adapter performs a fresh canonical CLI lookup on invocation. | A startup result becomes stale as workflow changes. Refresh before consequential work. | Emit current authorized context only after identity and lifecycle checks; otherwise explicit no-authority/invalid result. | Previous fresh-session acceptance is historical baseline; retest only where host access supports it. |
| Genuine Copilot resume | Shared Copilot SDK documents `onSessionStart` for new or resumed sessions and a `source=resume` value. This documents capability, not VS Code UI route or delivery. | High if resumed conversation retains old authorization. Refresh on an independently observed resume and again before consequential work. | New CLI lookup; never reuse cached authorization. If no callback/receipt, procedural refresh and do not claim automatic delivery. | **NOT VERIFIED** in this milestone: the same-chat reopen attempt produced no protected receipt or marker; UI target and event source were not independently established. |
| Checkpoint restore without callback | “Checkpoint Restored” or restored transcript is a UI event, not proof of SDK `source=resume`. M009.001's checkpoint attempt had no receipt. | High; transcript may contain old authority. Re-query before work. | Require live CLI context; stop on failure or non-authorizing phase. | **NOT VERIFIED** automatically; procedural fallback only. |
| Reopen the same chat / return to a session | VS Code can reopen/continue sessions; no independent `SessionStart(source=resume)` receipt was obtained for the Product Owner's same-chat return. | High; reopening may restore messages without a new context callback. Re-query before work. | Same live checks; do not infer callback from chat continuity. | Attempt recorded `NOT VERIFIED`; see M009.002 workflow evidence. |
| Compaction | VS Code documents conversation compaction; shared Copilot hook references include `preCompact`. This repo hook registers only session start, and pre-compaction is not proof of fresh post-compaction context delivery. | Compaction may summarize or lose details, but does not refresh Repo Control. Re-query after compaction before work. | Live query; stop if invalid/no-authority. | Not directly host-tested. |
| Later user turn / repeated “prompt ready” | Shared hooks expose prompt-submit observation; command output is not the session-start `additionalContext` channel. No second-start callback is implied by a later turn. | High if old authority persists across turns. Re-query before consequential work, regardless of phrase. | A model's voluntary query is procedural, not guaranteed. Stop on failure. | Not independently tested as automatic delivery. |
| Subagent start | Shared SDK/hooks document subagent lifecycle events, but the current repo hook is session-start only and does not inject authority into subagents. | High if a subagent acts on copied/stale context. Do not delegate authority; require each acting agent to verify canonical context before consequential work. | If the subagent cannot query the exact managed worktree/current state, it must not implement. | Not directly tested. |
| Model switch in an existing session | VS Code documentation says model choice does not change harness; a model switch alone is not proof of a new session or callback. | High if new model inherits stale conversation context. Re-query before consequential work. | Same canonical lookup; no implicit permission increase. | No independent model-switch callback test. |
| No new host callback / unavailable host | No callback means no new host-delivered context. A local `coder-context` query remains available to the Coder. | Highest stale-context risk until the query is completed. | Do not act on prior status; stop if live lookup cannot be completed or does not authorize. | Procedural behavior is specified; not host-enforced. |

## Authorization and result semantics

- **`AUTHORIZED`**: current canonical result says authorized for the exact
  selected repository/milestone and active scope. Point-in-time only.
- **`NO_AUTHORITY`**: a valid canonical result explicitly does not authorize
  implementation (including `WAITING`, `REVIEW`, or `CLOSED`). Stop.
- **`INVALID_NO_AUTHORITY`**: identity, selection, prompt hash, lifecycle,
  authorization coherence, event, or CLI result cannot be trusted. Stop.
- **Timeout, missing CLI, nonzero exit, malformed output, or excessive output**:
  no authorization result was established. Never fall back to a cached
  authorized result.
- **`NOT_VERIFIED`**: the trigger/delivery was not independently observed.
  This is not a Repo Control phase and grants no permission.

These are context and procedure classifications. They do not claim that a
tool/file permission system blocks work on hook errors or that every host,
harness, model, UI route, or timeout fails closed.

## Host-backed acceptance procedure

For any future genuine-resume claim:

1. Confirm the selected host/harness and workspace from its UI and installed
   runtime; record versions and the exact operation. Do not conflate shared
   Copilot with Local, Codex, or cloud hooks.
2. Use a documented, non-destructive way to return to the same inactive
   session. Do not substitute a new chat or checkpoint banner.
3. Temporarily enable the existing opt-in protected receipt and unique
   non-sensitive delivery marker. Do not log prompts, transcript bodies,
   secrets, or full context.
4. Require a new receipt with actual hook event/source, invocation and CLI
   exit, exact repository identity/worktree, current lifecycle, emitted
   context and marker; independently correlate that marker with the receiving
   session's first substantive response.
5. Restore the exact production hook configuration and remove the private
   test receipt immediately after recording the bounded result. If no receipt
   or correlation exists, classify resume **NOT VERIFIED**; seek explicit
   Product Owner scope/acceptance resolution. Never claim automatic continuity
   from documentation or model testimony.

## Guarantees and non-goals

The canonical CLI resolves current managed repository, selected prompt,
workflow lifecycle, open conditions and applicable approved Architect
dispatches. The Copilot adapter validates a bounded subset and performs a new
CLI invocation per observed hook call. Neither it nor this contract creates
universal pre-tool/file enforcement, refreshes chats with no callback,
provides a freshness lease, implements Codex/Local adapters, changes workspace
trust or permissions, or grants Git/lifecycle authority. Product Owner approval
and Repo Control lifecycle remain separate from host context delivery.
