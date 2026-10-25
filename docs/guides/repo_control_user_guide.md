# Repo Control User Guide

This guide is for Product Owners and operators using the Repo Control browser
and the normal Architect/Coder workflow. Repo Control supplies deterministic
repository evidence and explicit workflow controls; AI advice does not replace
the authoritative repository, Git, or milestone records.

## Quick start

1. Open the Repo Control URL provided by your operator.
2. Check the **Active Repository** name and path in the header before acting.
   If it is wrong, choose a configured repository and select **Use repository**.
3. Use **Milestone** for prompt selection and workflow, **Git Review** for Git
   inspection/actions, **Architect Dispatches** for pending proposals, and
   **Intelligence Value** for repository-intelligence status and feedback.
4. At the start of every fresh or resumed Coder session, retrieve current
   `coder-context` and verify repository, selected milestone, lifecycle,
   authoritative prompt, applicable approved dispatches, and authorization.
   Stop if the browser, Architect, and Coder disagree about any of these.
   From a managed worktree, the canonical command is
   `/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.cli coder-context`.

### Fresh sessions and host boundaries

Handoff phrases have stable meanings, but none authorizes work:

| Phrase | Meaning |
|---|---|
| `Prompt ready.` | A prompt is available for current Repo Control review. It is not approval or a start instruction. |
| `Instruction ready.` | An instruction is available for review; check whether it is approved and applies to this repository and milestone. |
| `Correction ready.` | A proposed bounded correction is available; confirm the current lifecycle and explicit authorization before acting. |

At the start of a fresh session, identify the role (Architect or Coder), the
host's actual Repo Control tool/CLI capability, the intended repository key
and path, the selected milestone, and its current lifecycle/authorization.
Fail closed if a tool or identity is missing, the intended repository differs
from the service-selected repository, or the phase is ambiguous. Do not change
the service selection just to satisfy a request. A useful operator handoff is:

> Use Repo Control to read current context for **[named managed repository]**; **[handoff phrase]**.

For VS Code/GitHub Copilot Coder in a managed worktree, run the canonical
local command from that worktree:

```bash
/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.cli coder-context
```

Confirm the returned repository key/path, selected milestone, current phase,
prompt, applicable approved dispatches, and authorization. Repo-resident
Copilot instructions only help when the host actually loads them.

#### ChatGPT Architect project instruction (copy-ready proposal)

The Product Owner must install project/chat instructions and connect the
Repo Control Architect tools in the host. This repository cannot edit host
account settings or guarantee that a new conversation consumed them.

```text
You are the Repo Control Architect, not the Coder. At the start of every
conversation, use the connected read-only Repo Control tools repo_status,
repo_workflow, and repo_prompt to identify the currently selected managed
repository, path, selected milestone, and lifecycle. State those identities
before advising. If tools are unavailable, identity is ambiguous, or the
selected repository differs from the user's named target, stop and ask the
Product Owner/operator to resolve it in Repo Control; never switch global
repository selection or infer state from chat history. "Prompt ready.",
"Instruction ready.", and "Correction ready." are handoff notices, not
approval or authorization. Do not impersonate the Coder, run a local
coder-context command from ChatGPT, or begin implementation based on a
handoff phrase. Only current Repo Control lifecycle and approved decisions
establish authority.
```

For other hosts, use only instructions and tools actually supported and
connected there. Do not assume automatic discovery, local shell access, or
that a repository file changed settings in another product. The quick
diagnostic is: **instructions loaded? Repo Control tool available? intended
repository selected? milestone selected? phase and role authorized?** Stop and
contact the operator when any answer is unknown.

## Core concepts

| Term | Meaning |
|---|---|
| **Repository** | A configured Git worktree that Repo Control is allowed to serve. The browser can select only configured repositories, not arbitrary paths. |
| **Milestone** | A bounded unit of approved work represented by a prompt, a workflow sidecar, and usually a closeout. |
| **Active milestone** | The prompt explicitly selected for the active repository. Selection does not approve or start the work. |
| **Governing documents** | The current project context, architecture, workflow, Coder rules, and canonical parking lot. These describe accepted present-day truth and authority. |
| **Product Owner (PO)** | Owns product decisions, consequential workflow approvals, milestone acceptance, and every Git mutation. Repo Control records reported role attribution; it does not authenticate a human identity. |
| **Architect** | Plans work, advises on scope and design, answers questions, and reviews evidence. Architect proposals are not authoritative until the PO approves them. |
| **Coder** | Implements approved scope, validates it, records meaningful questions/evidence, and submits a closeout. The Coder does not mutate Git. |
| **Architect Dispatch** | A typed proposal from the Architect: a **PROMPT**, **INSTRUCTION**, or **RESPONSE**. It begins pending and is scoped to its target milestone. |
| **Workflow sidecar** | The tracked `*_workflow.md` file beside a milestone prompt; it records lifecycle events and durable decisions. It is not the prompt or closeout. |
| **Closeout** | The Coder's report of completed scope, evidence, deviations, limitations, and Git status. It is submitted for review; its presence alone does not accept the work. |
| **Repository Intelligence** | Optional, bounded static structural evidence about code. It is not runtime truth, project authority, or proof that an absent relationship does not exist. |
| **Skill** | A reusable procedure for an agent. Skills do not grant authority or replace the prompt, governing documents, or Repo Control state. |

## Milestone lifecycle

| State | What it means | PO action / expectation |
|---|---|---|
| `PLANNING` | Work is being planned; Coder implementation is not authorized. | Review the prompt and any applicable approved decisions. |
| `READY` | The prompt is approved for handoff, but implementation has not started. | Start explicitly when ready. |
| `IN_PROGRESS` | Authorized implementation or validation is active. | Monitor meaningful evidence and answer escalations. |
| `WAITING` | Work is paused for a recorded question, blocker, or decision. | Review the reason and resolve it before work resumes. |
| `REVIEW` | Closeout and evidence are submitted for review. | Review the result; accept, reject, or request a bounded correction. |
| `CLOSED` | The PO accepted and closed the milestone; Coder authority ends. | The PO may explicitly reopen to `REVIEW` or `IN_PROGRESS`; Git completion or deferral remains separate. |

The normal implementation loop is distinct from waiting, correction, and
re-scoping:

- **Normal implementation:** after `IN_PROGRESS` authorization, the Coder
  works only within the prompt and applicable approved lock-ins.
- **WAITING/question:** the Coder stops when blocked. The PO records the
  appropriate resolution and explicitly resumes; a chat answer alone does not
  resume a paused milestone.
- **Bounded correction:** from `REVIEW`, the PO may authorize a narrow,
  reviewable correction. The Coder changes only that boundary and resubmits the
  closeout.
- **Material re-scope:** a changed objective, architecture, risk, authority, or
  materially different validation need requires an explicit re-scope or new
  milestone. Do not hide it in a correction.

## Run a milestone

1. On **Milestone**, confirm the repository and configured milestone root.
   Select the intended discovered prompt; Repo Control does not guess a
   milestone from filenames or Git history.
2. Read the prompt and relevant governing documents. Confirm scope, exclusions,
   acceptance evidence, and any open conditions.
3. From `PLANNING`, the PO uses **Approve & Start** when the work is approved.
   Repo Control records the prompt-approval and implementation-start events
   distinctly and moves to `IN_PROGRESS`.
4. At every fresh or resumed session, the Coder retrieves current authoritative
   context rather than relying on previous chat history or searching documents
   to reconstruct state. Confirm repository, selected milestone, phase,
   prompt, approved applicable dispatches, and authorization. If any identity
   or status cannot be resolved, stop and contact the operator. If there is a
   blocking question, review its recorded reason and use
   **Resolve & Resume** only after the decision is clear.
5. In `REVIEW`, inspect the closeout, changed files, tests/evidence, questions,
   deviations, and known limitations. Use **Request bounded correction** only
   for a small correction inside the approved objective.
6. Use **Accept & Close** only after review is complete. Decide separately
   whether to complete Git actions or defer them.
   If approved applicable Architect INSTRUCTION/RESPONSE guidance is newer
   than the latest closeout, submit an amended closeout or review the
   dispatches and provide a reason in the explicit second confirmation.
7. To recover an accidentally or prematurely closed milestone, the PO may use
   **Reopen milestone** and provide a reason and target phase. `REVIEW` grants
   no edit authority; `IN_PROGRESS` authorizes only the same bounded objective.
   Reopening appends history, preserves prior acceptance/closure, and does not
   modify Git or rewrite Git history.

The available action and prerequisites are shown in the Milestone page. Do not
try to bypass a missing action; check the lifecycle, open conditions, repository
identity, and prompt selection first.

## Meaningful workflow events

Use the Milestone page's **Record a meaningful workflow event** controls to
preserve decisions and evidence that affect the work. Record the smallest
accurate summary; routine conversation does not need an event.

| Control | What it records / when to use it | When not to use it | Effect |
|---|---|---|---|
| **Record a question** | A specific clarification needed to continue or make a decision. Add a waiting category and reason when work must pause. | Routine discussion, or a material blocker better raised as an escalation. | Evidence only unless a waiting reason is supplied; then moves `IN_PROGRESS` to `WAITING`. |
| **Record a non-binding decision** | A useful decision or direction that should be preserved but is not a binding scope or authority lock-in. | When the PO intends the decision to bind; use the lock-in control instead. | Evidence only; does not change lifecycle. |
| **Approve a binding lock-in (Product Owner)** | The PO explicitly approves a decision that must bind the milestone's work. | Ordinary discussion, advice, or a decision not approved by the PO. | Records a binding PO lock-in; does not change lifecycle. |
| **Open an escalation and wait** | A material blocker, risk, or conflict that requires work to stop for PO attention. Include the reason. | A routine question that can be answered without pausing. | Records the escalation and moves `IN_PROGRESS` to `WAITING`. |
| **Record bug discovered** | An observed defect that should be tracked for classification. | A suspected issue without evidence, or as a substitute for recording a fix. | Discovery evidence only; does not classify or fix the bug or change lifecycle. |
| **Record test result** | The evidence source and outcome of a test actually run, including failures. | A test that was not run, or as a substitute for PO acceptance. | Reported evidence only; does not verify the run, accept work, or change lifecycle. |
| **Record Git disposition (no Git action)** | The PO's choice to complete Git work later or defer it, recorded for the milestone. | As a command to stage, commit, push, or otherwise change Git. | Records disposition only; performs no Git action and does not change lifecycle. |

A **question** may be non-blocking or may pause work when its waiting reason
is recorded. An **escalation** is specifically for a matter that needs PO
attention before work continues and always enters `WAITING`. A **non-binding
decision** preserves guidance; a **binding lock-in** is explicitly approved by
the PO and carries binding authority. A **bug-discovered** event records
discovery; fixing it is a separate action available only for a classified,
in-scope bug. A **test-result** event records reported evidence; acceptance is
a separate PO decision. A **Git disposition** records a choice; it never
performs a Git mutation.

Answer, classify, and fix controls are context-specific: they appear only when
an eligible related question or bug exists. Answer an open question, classify
an unclassified discovered bug, and record a fix only for an eligible
classified in-scope bug. Do not create events just to capture routine
conversation.

## Architect Dispatches

Open **Architect Dispatches** to review each proposal's complete content,
target milestone, and status.

- **PENDING** proposals are not Coder instructions.
- A **PROMPT** approved with **Approve & Save Prompt** materializes the
  canonical prompt; that approval does not select or start the milestone.
- Approved **INSTRUCTION** and **RESPONSE** proposals apply only to their
  approved milestone and remain below the prompt, governing rules, and PO
  authority.
- After the normal concise handoff, confirm an approved applicable instruction
  appears in current `coder-context` and matches the intended repository and
  milestone. M007.006 accepted fresh/resumed Coder-context convergence across
  Repo Control II and Vocab, including approved applicable instruction
  visibility; pending or rejected proposals remain excluded.
- Architect dispatch records live in the external per-repository state ledger
  and are preserved during repository scans. If a ledger is missing,
  malformed, or a scanner backup is present, stop and preserve the state for
  Product Owner/operator review. Never reconstruct records or approvals from
  prompts, workflow sidecars, chat history, or a stale backup.

## Review Git state and actions

Use **Dashboard** for a status overview. **Git Review → Changes** shows
working-tree and index states and file diffs; selecting files is read-only.
**History** inspects local commits and file history. **Branches** shows local
branches, branch-vs-`main` relationships, configured upstreams, and locally
known ahead/behind counts. These upstream counts use local remote-tracking refs
and may be stale until Fetch.

Every offered Git mutation has a separate review/confirmation step and
revalidates relevant state before execution. If the state changes or a selected
item is unsupported, the action may be disabled or refused.

Distinguish:

- **Untracked:** not yet tracked by Git.
- **Unstaged:** working-tree changes not in the index.
- **Staged:** changes already selected in the index for the next commit.
- **Staged and unstaged:** both a staged version and additional working-tree
  changes exist; review both.

### Changes and review

| Control | What happens |
|---|---|
| **Stage selected** | Adds only selected supported paths to the index. Review the exact paths and preview; staging a tracked path uses its current worktree content. |
| **Unstage selected** | Removes only selected supported changes from the index; worktree content is not replaced. |
| **Restore unstaged** | Discards selected unstaged worktree content and restores from the index. |
| **Restore selected paths to HEAD** | Replaces selected staged and unstaged content with the version at `HEAD`; this can discard both layers of work. |
| **Review commit of staged files** | Commits the complete staged set only. Inspect the full staged diff and file list; other unstaged or untracked changes are not implicitly staged. |

Stage, unstage, restore, and commit actions each require review/confirmation
and revalidate current state before acting. Restore actions are destructive:
check every selected path and the preview. A batch action is available only
when every selected path supports it; conflicts, unsupported states, incomplete
preview evidence, or changed state can disable or refuse an action.

### Local branches and history

Use **Branches** to inspect local branch tips, their relationship to local
`main`, upstreams, and ahead/behind state. Use **History** for read-only
inspection of recent commits, commit diffs, and file history. Neither view
fetches automatically.

| Control | Supported behavior and safety condition |
|---|---|
| **Create local branch** | Creates a validated new branch from current `HEAD`; it does not switch to it or configure an upstream. Review and confirm. |
| **Review switch** | Switches to an existing local branch only when the current branch is attached, the worktree/index is completely clean, and no in-progress Git operation prevents it. Review and confirm; no auto-stash or discard occurs. |
| **Review local branch deletion** | Available only for an eligible non-current, non-`main` branch proven fully merged into local `main`. Uses normal safe-delete behavior. This deletes the local branch only; it does not delete its remote branch. |

### Remote operations

Remote controls require review/confirmation. Repo Control prefers an eligible
`origin`; without an `origin`, a sole configured remote can be selected by
default. When multiple remotes require a choice, select the intended one
explicitly. Re-check branch, upstream, ahead/behind, and working-tree state
after remote operations; local tracking information may still be stale or an
operation may report incomplete verification.

| Control | What happens and when it is available |
|---|---|
| **Fetch** | Contacts the selected configured remote and updates local remote-tracking refs. It does **not** move the current branch, `HEAD`, index, or worktree, and does not merge, rebase, or prune. Fetch before relying on possibly stale upstream counts. |
| **Push** | Publishes committed history from the current branch to its configured upstream; it is non-force. Uncommitted changes remain local. It is unavailable without a usable upstream or when there is nothing to push. |
| **First push / set upstream (Publish)** | For a current branch without an upstream, publishes the same-name branch to the selected configured remote and sets that remote branch as its upstream. The same-name remote branch must be absent; only committed history is pushed. |
| **Fast-forward update** | Advances the current local branch to its known upstream tip without creating a merge commit and without fetching. It is available only for a clean, behind-only branch with a usable upstream; dirty, ahead, diverged, or missing-upstream cases are refused. Fetch and inspect first if the tracking ref may be stale. |

Repo Control does not support merge or conflict resolution, rebase,
cherry-pick, reset, stash, force push or force-with-lease, remote branch
deletion, tags, arbitrary refs/refspecs, general pull, history rewriting,
GitHub PR/CI control, or remote-configuration editing. These remain outside
the browser workflow. Manual Git is a controlled PO/operator escape hatch for
such cases, not Coder permission: the PO or authorized repository operator
must verify the repository, exact operation, and current state. Never ask the
Coder to stage, commit, push, or otherwise mutate Git.

## Repository Intelligence

On **Intelligence Value**, check the selected repository and graph state:

| State | Meaning |
|---|---|
| `MISSING` | No usable graph has been built. |
| `BUILDING` | A deliberate build is in progress. |
| `CURRENT` | The graph matches the recorded source identity and options. |
| `STALE` | Source identity or options changed since the build. |
| `FAILED` | A build or graph check failed; review the explicit error. |
| `INCOMPATIBLE` | The stored graph does not match the supported schema/producer. |

Only `CURRENT` evidence is served; queries do not refresh it. Product Owner
**Approve & Start** automatically establishes a CURRENT baseline for supported
repositories before implementation begins. A required build failure leaves the
milestone unstarted; repositories with no supported code source report
`NOT_AVAILABLE`. The PO's deliberate **Build / Refresh Graph** button remains
available on this page without a separate approval checkbox.

During authorized `IN_PROGRESS` work, a Coder may run
`repoctl coder-intelligence-refresh` only when the graph is non-CURRENT and
fresh structural evidence would materially help. The command takes no
repository, provider, path, or build options; it resolves the managed Coder
workspace and active milestone, requires the service-selected repository to
match, and changes only the existing external derived graph. It refuses other
lifecycle/waiting states and does not change Git, lifecycle, repository
selection, or graph configuration. `CURRENT` is a no-op; unsupported source
scope is reported as `NOT_AVAILABLE`; failures are explicit and do not fall
back to older evidence.

Static relationships can help target source inspection but do not establish
runtime behavior, and missing graph edges do not prove that behavior is absent.
Refresh receipts and counts are shown separately from query-use ratings and
comparative measurements; they contain bounded provenance, not source or query
bodies.

The **Intelligence Value** page includes an interpretation disclosure and
serves the [Reading Intelligence Value tutorial](./intelligence_value_tutorial.md)
at `/intelligence/guide`. The guide explains the rated-use denominator,
negative benchmark reductions, the one-task scope of the latest result, and
why older unmatched byte-only measurements remain historical rather than
comparable.

Each new query's byte count is the UTF-8 length of the compact normalized JSON
response after evidence pruning and count metadata updates. Measurements are
stored separately from the response, preserving the existing `response_bytes`
meaning, response shape, and evidence budget. The measured text includes returned
outcome, warnings, errors, and truncation/omission metadata; it excludes the CLI
pretty-print whitespace/newline, MCP result wrapper/protocol framing, and HTTP
headers. This is the canonical compact service-JSON boundary shared by Coder and
Architect before transport formatting. Character count is the Python Unicode
code-point count of that serialized JSON text (non-ASCII content is JSON-escaped
by the serializer). The estimate is `ceil(characters / 4)`, method
`char4-ceil-v1` version `1`; it is a rough evidence-text estimate, not exact tokens
for any model (`~1 token = 4 English characters ≈ 0.75 English words` is only an
approximation). Languages, JSON, whitespace, paths, and identifiers can differ
substantially. The dashboard displays code-point counts and estimated tokens for
each measured use from separate analytics. Existing SQLite rows keep
their original bytes and have NULL character/token provenance, shown as “Not
measured”; they are not backfilled.

Normal query paths remain network-free and use only the explicitly versioned
char4 estimate. The separate deliberate benchmark runner uses one fixed
cross-agent reference: `o200k_base` from `tiktoken==0.11.0`, method
`o200k-base-reference-v1` version `1`. This reference count is exact only for
the counted text and encoding; it is not a model-native, Copilot, or billed
token count. It is computed with `encode_ordinary`, so strings resembling
special tokens are treated as ordinary text.

The benchmark-only Python 3.12 environment and vocabulary cache are user-owned
and outside the application environment. The hash-locked dependency manifest is
[`tools/intelligence-benchmark-requirements.txt`](../../tools/intelligence-benchmark-requirements.txt).
For a one-time online provisioning, download only the locked wheels, install
from that wheelhouse, then fetch and verify the official encoding asset:

```sh
root="$HOME/.local/share/repoctl/intelligence-benchmark"
venv="$HOME/.local/share/repoctl/intelligence-benchmark-venv"
cache="$HOME/.cache/repoctl/intelligence-benchmark/tiktoken"
mkdir -p "$root/wheelhouse" "$cache"
python3.12 -m venv "$venv"
"$venv/bin/python" -m pip download --require-hashes --only-binary=:all: \
  -r tools/intelligence-benchmark-requirements.txt -d "$root/wheelhouse"
"$venv/bin/python" -m pip install --no-index --find-links "$root/wheelhouse" \
  --require-hashes -r tools/intelligence-benchmark-requirements.txt
curl --fail --location \
  https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken \
  --output "$cache/fb374d419588a4632f3f557e76b4b70aebbca790"
printf '%s  %s\n' \
  446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d \
  "$cache/fb374d419588a4632f3f557e76b4b70aebbca790" | sha256sum --check
```

The `tiktoken` wheel SHA-256 is pinned in the manifest and was checked against
the exact release-file entry in `https://pypi.org/pypi/tiktoken/0.11.0/json`;
the asset SHA-256 above matches the hash embedded by that pinned package.
Subsequent benchmark execution is offline:
the runner verifies the local asset and package version before importing
`tiktoken` or requesting the encoding, and refuses if either does not match.
It accepts bounded JSON from standard input; each `tool_evidence` item must be
the exact compact normalized JSON text delivered, while
`source_windows_read` items are the exact source text actually read. Items are
counted separately, so delivered duplicates count twice. Only bounded counts
and provenance may be persisted; raw input is not stored. Omit
`--database-root` to print the bounded record without persisting it.
After provisioning, use the isolated interpreter and local asset cache, for
example:

```sh
printf '%s' '<bounded controlled-evidence-v1 JSON>' |
  "$venv/bin/python" tools/intelligence_benchmark.py --cache-dir "$cache"
```

The fixed `tiktoken==0.11.0` wheel SHA-256 is
`7f929255c705efec7a28bf515e29dc74220b2f07544a8c81b8d69e8efc4578bd`;
the official `o200k_base` vocabulary SHA-256 is
`446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`.

A controlled comparison is a deliberate local experiment, separate from automatic
query and refresh records. Fix a repository snapshot/HEAD/source identity, question,
protocol version, and identical completion/stopping criteria before either arm.
Run source-first (for example, bounded search and source reads) and
Intelligence-first (a CURRENT graph query followed by source validation); record
which source windows were read versus only located, files inspected, interactions,
and monotonic elapsed milliseconds. Count UTF-8 bytes and Unicode code points on
the actual delivered tool text and actually read source text, then apply the same
`char4-ceil-v1` rule and fixed reference tokenizer to both. The controlled
protocol counts each delivered occurrence, including a source excerpt delivered
twice if actually returned twice.
Count source windows actually read separately from windows only located/scanned.
Do not retain the literal question, source, or evidence text in the benchmark
record; retain a stable question/task ID instead. Store protocol/snapshot/timestamp/method provenance and both arms' separate source,
tool, and total units through the trusted-local
`intelligence.measurements.add_benchmark` interface. Mark stopping rules matched
only when equivalent completion criteria were actually followed. Char4 and byte
reductions require matched stop rules and counting provenance; fixed-reference
reductions additionally require matched completion criteria and the same
tokenizer method, package, encoding, vocabulary asset and counted surface. Zero
control cannot produce a percentage, and a negative reduction means
more evidence in the Intelligence-first arm. If equivalence was not established,
retain descriptive measurements but do not present a savings comparison.

The controlled record schema is versioned: `provenance` includes
`recorded_at`, `protocol_version`, `corpus_id`, `repository_id`, `source_head`,
`source_identity`, `generation`, `question_id`, both arm strategies,
`stopping_rule`, `stopping_rules_matched`, `completion_criteria_matched`,
`counting_boundary`, and `overlap_policy`. Each `control` and `graphify` arm
retains the legacy `files`, `source_bytes`, `evidence_bytes`, `interactions`, and
`elapsed_ms` fields, plus `files_inspected`, `source_evidence_bytes`,
`tool_evidence_bytes`, `total_evidence_bytes`, source/tool/total character counts,
`estimated_evidence_tokens`, `evidence_metric_method`,
`evidence_metric_version`, `evidence_metric_surface`,
`evidence_metric_quality`, `source_windows_read`, and
`source_windows_located_only`. Fixed-reference records additionally contain
nullable per-arm `reference_tokenizer_evidence_tokens`, method and version,
package version, encoding, asset SHA-256, exact surface, and measurement
quality. Char4-only and historical byte-only records remain unchanged and
their reference counts display as “Not measured.” The aliases remain internally consistent:
`source_bytes` equals `source_evidence_bytes`, and `evidence_bytes` equals the
source-plus-tool total. `controlled-evidence-v1` fixes the counted surface and
the count-each-delivered-occurrence overlap policy; char4 and fixed
`o200k_base` reference figures remain separately identified.

The comparative table is empty until an explicit controlled record is added;
ordinary queries populate Recent uses instead. Historical M004.002 rows remain
byte-only with their original non-blind/unequal-stopping-rule caveats. Their byte
figures are not converted into token counts. None of these measurements captures
hidden host context, model prompts, reasoning, output tokens, provider billing, or
human reasoning time.

The consuming Coder should rate each materially relied-on use and add a concise
rationale where useful. Ratings are evidence of the consumer's experience;
the PO is not expected to judge technical usefulness for evidence they did not
consume. Do not invent ratings for older unrated uses.

## Governing documents and Skills

The five canonical documents serve different purposes:

- [Project context](../context/project_context.md): current product goals and
  project baseline.
- [Project architecture](../context/project_architecture.md): system design,
  integrations, and architectural invariants.
- [Project workflow](../context/project_workflow.md): roles, lifecycle, review,
  and Git authority.
- [Coding Agent Rules](../context/coding_agent_rules.md): repository-specific
  Coder overlay; the generated host bootstrap carries generic Repo Control
  safe-start procedure.
- [Canonical parking lot](../context/canonical_parking_lot.md): unresolved
  future/deferred work, not current implementation scope.

Current approved documents and Repo Control state govern. Milestone prompts
and closeouts are authoritative for their own work or history, not a reason to
override current accepted rules. The three accepted repository-local Skills
are [implementation](../../.agents/skills/repo-control-implementation/SKILL.md),
[blocker/bug escalation](../../.agents/skills/repo-control-blocker-bug-escalation/SKILL.md),
and [validation/closeout](../../.agents/skills/repo-control-validation-closeout/SKILL.md).
Explicitly load the relevant Skill; it supplies procedure, never permission.

### Architect reads of current repository documents

`repo_document` remains the fixed-role reader for the five registered
governing documents. For other current documentation, the Architect can use
`repo_read_document` with one explicit repository-relative path and the
expected repository key and registry generation. The read is from the
selected repository's working tree, so modified and untracked documents are
available for review.

The path must name a Markdown, `.txt`, or `.rst` file under `docs/`,
`.github/`, or `.repo-control/`; the root `README.md`; or a Skill
`.agents/skills/<skill>/SKILL.md`. Traversal, symlinks, source-code paths,
secret/configuration paths, directories, and unsupported types are rejected.
At most 128,000 UTF-8 bytes are returned with explicit original size and
truncation metadata; source files above 5,000,000 bytes are rejected. Git
tracking status is not collected. This is not directory browsing or search,
and paths under historical roots (`history`, `milestones`, `archive`,
`archives`, or `historical`) are excluded from current-document reads; use the
separate bounded historical evidence tools for prior canonical milestone
artifacts.

### Architect reads of historical project evidence

Historical planning evidence uses a separate two-step interface; it does not
extend the current-document reader:

- MCP `repo_history_search` / API `GET /api/architect/v1/history/search`
  returns bounded metadata for prior canonical milestone prompts, workflow
  sidecars, and closeouts under the selected repository's configured
  milestone root, plus supported text artifacts under `docs/history/`.
- MCP `repo_history_document` / API
  `GET /api/architect/v1/history/document` reads one explicitly selected
  historical path returned by discovery.

Both tools require `expected_repository_key` and `expected_generation`.
Search supports an optional lexical `query` (all terms must match),
`milestone_id`, `artifact_type`, `category` (`milestone` or `archive`), and
`limit` (1–20). Without a query, request a narrow inventory using a milestone
ID or both category and artifact type. The fixed search policy considers at most
2,000 candidate files and directories, reads at most 300 text files and
16,000,000 bytes, returns at most 20 results, and caps each response at
128,000 bytes. Results never include document content;
they include the exact path, type, size, category, milestone ID when
derivable, match counts, and explicit returned/total/omitted and bounded-scan
metadata. Ordering is deterministic: exact path match, exact milestone-ID
match, descending lexical occurrence count, then path.

The current selected milestone's artifacts are not returned as historical
results. A search is permitted even when no milestone is selected. Historical
reads use the current working tree, allow only repository-contained regular
Markdown/text/reStructuredText files in the configured milestone root or the
fixed `docs/history/` convention, reject symlinks and unsafe paths, and return
at most 128,000 UTF-8 bytes. Source files above 5,000,000 bytes are rejected.
Returned content includes original size, returned byte/character counts, and
truncation metadata. The tools label all results as `historical_evidence` and
not current authority. Current Product Owner decisions/approved dispatches,
active lifecycle/prompt, and current governing documents remain authoritative.
They do not inspect Git history, search source code, browse arbitrary
directories, or access external/network resources.

Generic bootstrap, workflow core, and Skill sources are maintained in the
versioned [`shared/procedure`](../../shared/procedure/) bundle. Managed
repositories contain a generated `.github/copilot-instructions.md`, physical
`.repo-control/workflow_core.md`, `.agents/skills/` copies, and
`.repo-control/shared-procedure.lock.toml`.
Project-specific constraints remain in the local Coder overlay and are never
overwritten by generic materialization.

Use these read-only commands to inspect a registered repository:

```bash
/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.cli coder-procedure-status
/home/chuck/projects/repo-control-ii/.venv/bin/python -m repoctl.cli coder-procedure-preview --repository /path/to/managed/worktree
```

Status reports release/hash/overlay drift. Preview shows proposed diffs and
blocked conflicts; it does not write or delete files. The browser's
**Procedure Updates** page shows status for configured repositories and a
deterministic **Review Update** preview without changing the global active
repository selection.

### Procedure release update: start to finish

1. The PO reviews and approves a semantic release to the generic sources in
   Repo Control II `shared/procedure/`; publishing a release does not update
   any managed worktree.
2. In **Procedure Updates**, compare each registered repository's installed
   release, status, lock hash, canonical manifest/source hashes, and overlay.
   `STALE_VERSION` is expected release drift, not by itself a defect.
3. Select one configured repository's **Review Update** action. Review the
   deterministic read-only diff, generated target paths, hashes, blockers,
   and overlay state. Page views and preview do not write files or change the
   active browser repository.
4. If the PO chooses to update this repository, use **Review Apply** and
   inspect the separate confirmation page: it names the repository, stable
   ID, full path, release, exact generated targets, and before/after hashes.
   Only the explicit, CSRF-protected **Confirm Apply** POST materializes the
   fixed five generated procedure files and release lock. Review and
   confirmation are separate actions; an unblocked preview alone is never
   authorization. The workflow covers one selected registered repository at
   a time and does not change the active Repo Control selection.
5. Apply reports success only after an independent full-set check returns
   `CURRENT`. A no-op is reported without rewriting files. Then use ordinary
   PO Git Review for the generated working-tree changes; procedure
   materialization does not stage, commit, or push. This milestone's tests use
   disposable fixtures only; do not infer that Vocab or another live
   repository was materialized.
6. If Apply is interrupted or cannot verify the full set, the durable
   transaction record blocks another Apply. Status, preview, page load, and
   startup do not repair files. When the record is safely recoverable, use
   **Review Recovery** and the distinct **Confirm Recover** POST to restore
   only the exact recorded original images. If any target differs from both
   known images, recovery is blocked: preserve the evidence and escalate for
   manual investigation rather than overwriting the external edit.

Transaction journals and recovery images are stored under
`<state-root parent>/.<state-root-name>-procedure-transactions/<repository_id>/`,
in private service-user directories beside, not inside, scanner-replaced
repository state or a managed worktree. The journal serializes Repo Control scans and transactions, not
arbitrary external filesystem writers. Individual replacements are atomic,
but readers can observe a mixed set during Apply; the workflow does not claim
simultaneous six-file visibility or immunity to a non-cooperating writer in
the final check-to-replace interval. A loopback browser role label and CSRF
token are not human identity authentication. Apply/Recover are not Coder
commands and grant no Coder Git authority.

The five repository-owned `docs/context` documents—including the local Coder
rules overlay—are never generic procedure targets. Their independent
maintenance path is a PO-approved, narrowly scoped project documentation
milestone or reviewed factual correction in the owning repository. Review the
proposed content and authority impact, validate relevant claims, and use
ordinary Git Review. Do not use shared-procedure materialization to update
them. A future lightweight proposal UX remains deferred as DOC-001.

There is no operator-facing Coder materialization command. The guarded
internal materializer remains separate from the browser transaction workflow
and must not be treated as PO authorization for a real managed repository.
M007.006 accepted fresh/resumed Coder-context resolution and approved
instruction visibility across Repo Control II and Vocab. A `CURRENT` file
status remains distinct from that host-consumption acceptance.

Each managed repository is pinned to the shared-procedure release in its local
lock. `CURRENT` means its installed release and hashes match the canonical
target; `STALE_VERSION` means the installed release is older and needs
deliberate per-repository reconciliation. This is expected drift, not by
itself a defect. Publishing a canonical release does
not change managed repositories. The operator flow is status → Review Update
→ explicit PO confirmation → independently verified `CURRENT` → ordinary
Git Review. Page views and preview remain read-only; the separate Apply and
Recover POST confirmations are the only browser transaction actions.

Status values are `CURRENT`, `MISSING`, `STALE_VERSION`, `HASH_MISMATCH`,
`OVERLAY_INVALID`, and `INCOMPATIBLE`. Overlay checks validate its marker and
required section, not the meaning of its rules; reviewers must ensure that
local content does not conflict with generic authority.

### Procedure update questions

- **Does `STALE_VERSION` mean the repository is broken?** No. It is expected
  drift when the canonical release advances; inspect the lock, target hashes,
  preview, and overlay for separate conflicts.
- **Does preview or page load write files?** No. Apply and Recover each require
  their own CSRF-protected review and explicit confirmation POST.
- **Does Apply update local `docs/context` files, stage changes, or update all
  repositories?** No. Only the fixed generated procedure files and lock in one
  selected registered repository are in scope. Git review remains separate.
- **What if recovery reports a conflict?** Do not edit, delete, or replace the
  journal or recovery images. Preserve them and escalate for operator
  investigation; an unknown target image is never overwritten automatically.

## Common recovery

| Situation | What to do |
|---|---|
| Wrong repository selected | Stop before acting. Select the intended configured repository and re-check its displayed name/path. If the browser, Architect, or Coder still disagree, contact the operator. |
| Dashboard **Refresh Status** returns HTTP 500 / `unexpected_error` | Do not infer a Git or lifecycle change from the failed refresh. Verify the repository on **Git Review** and **Milestone**, preserve the error details, and report it; this known UI defect candidate is tracked in the parking lot. |
| Intelligence is stale, missing, failed, or incompatible | Do not use it as current evidence. During authorized `IN_PROGRESS` work, a Coder may run `repoctl coder-intelligence-refresh` when fresh structural evidence would materially help; it refreshes only the existing configured graph. Otherwise use targeted source evidence if the task allows it. The PO's deliberate **Build / Refresh Graph** action remains available. |
| Coder asks a blocking question / workflow is `WAITING` | Read the recorded question and reason. Resolve the decision through the PO workflow and resume explicitly. Do not ask the Coder to continue while waiting. |
| Closeout needs a small correction | From `REVIEW`, authorize only the bounded correction. If it changes scope or direction materially, plan a separate milestone. |
| Unexpected dirty file appears | Identify the exact path and owner. Do not discard, stage, or commit it. Stop if it may be unrelated to the active milestone. |
| Browser action is unavailable | Check repository/milestone identity, lifecycle prerequisites, selection, and safety checks. Use manual Git only for supported-by-policy corner cases and only under PO/operator control. |
| Fresh/resumed Coder session shows old or conflicting context | Stop implementation. Retrieve current `coder-context`/`coder-status` and compare repository identity, selected milestone, lifecycle, prompt, approved applicable dispatches, and authorization against the browser. Do not rely on old chat history or broadly search documents to guess the answer. If identity or authorization remains unresolved, report the failure and contact the operator. |
| Architect and Coder disagree | Stop implementation and compare current `coder-context`, the prompt, approved dispatches, and Repo Control status. Escalate unresolved authority conflicts; do not rely on chat memory. |
