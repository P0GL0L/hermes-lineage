# How lineage works

The README tells you what you get. This page is for the reader who wants to know exactly how the
graph is built, what is stored, and where the edges come from.

## Hooks

lineage registers ten **observer** hooks and nothing else:

| hook | what lineage takes from it |
|---|---|
| `on_session_start` / `on_session_end` | session identity, platform, model, parent session |
| `pre_llm_call` | opens a turn: prompt preview (redacted), prompt hash |
| `post_llm_call` | closes a turn: reply hash; URLs and files named in the reply become `cited_in_reply` edges; optional sidecar receipts |
| `post_api_request` / `api_request_error` | one `model_call` node per provider attempt: model, provider, usage, duration, outcome |
| `post_auxiliary_call` | compression, titling, vision and other auxiliary model calls as `aux_call` nodes in the turn |
| `post_tool_call` | the heart of it — see below |
| `subagent_start` / `subagent_stop` | child sessions linked to the parent session and turn |

It deliberately does **not** register `pre_tool_call`. Hermes treats that hook as fail-closed: a
callback that times out blocks the agent's tool call. An observer must never be able to do that, so
everything is reconstructed from `post_tool_call`, which carries `duration_ms` and `api_request_id`.

Every callback swallows its own exceptions. Hermes already fail-opens hook errors, but lineage
guards itself twice: a provenance recorder that can interrupt the agent is worse than none.

## Nodes

| kind | id | created when |
|---|---|---|
| `file` | `file:<absolute path>` | read by a tool, or written by one (`produced=1`) |
| `url` | `url:<normalized url>` | fetched, searched up, or named in a command / argument |
| `query` | `query:<hash>` | a `web_search` runs |
| `tool_call` | `tc:<tool_call_id>` | every `post_tool_call` |
| `model_call` | `mc:<api_request_id>` | every provider attempt |
| `aux_call` | `aux:<api_request_id>` | every auxiliary attempt |
| `agent` | `agent:<session_id>` | a session starts or is delegated |
| `turn` | `turn:<turn_id>` | a user turn starts |
| `skill` | `skill:<name>` | `skill_view` |

URL ids are normalized: scheme and host lowercased, `www.` and fragments dropped, userinfo removed,
credential-shaped query values (`token=`, `key=`, `sig=`…) masked. A secret-bearing URL never becomes
a node id.

## Edges and the three bases

Every edge carries a `basis` and a `confidence`. The basis is stored, drawn, exported and written
into receipts. It is never collapsed into a single "derived from".

| basis | confidence | how it arises |
|---|---|---|
| **observed** | 1.0 | Hermes reported it directly: `wrote` (the tool wrote this path), `read`, `invoked` (model call → tool call, via `api_request_id`), `produced_by`, `produced_in`, `delegated_by`, `spawned_in`, `returned` (search → URL) |
| **evidenced** | 0.9 | the artifact's own bytes name the source: a fetched URL or a read file's path appears in the written text; also `cited_in_reply` when the final answer names an artifact or URL |
| **inferred** | 0.6 / 0.35 | the source was read earlier in the same turn (0.6) or in one of the last `session_window_turns` turns (0.35) |

When the same pair is linked more than once, the stronger basis wins and the weaker is dropped.

## What counts as "the agent wrote this"

| tool | how the artifact is found | basis |
|---|---|---|
| `write_file` | `args.path` | observed |
| `patch` (replace mode) | `args.path` | observed |
| `patch` (V4A multi-file, OpenAI-family models) | `*** Update File:` / `*** Add File:` lines parsed from `args.patch` | observed |
| `terminal`, `execute_code` | one bounded walk of the working directory after the call; every regular file whose mtime falls inside the call's window | observed |
| any other tool | a path named in the tool's result that exists and was modified during the call (image generators, converters, MCP tools) | inferred |

The terminal walk is bounded by `snapshot_max_files` (6000) and `snapshot_max_seconds` (0.5); a tree
that is too big or too slow yields nothing rather than a slow hook. At most `max_artifacts_per_call`
(50) files are recorded per call; the overflow is counted on the tool call node. `.git`,
`node_modules`, build output and the like are skipped; `.pyc`, `.lock`, `.tmp` and `.receipt.json`
are never artifacts.

A blocked or failed tool call records nothing it would have read or written.

## What counts as a source

| tool | sources recorded |
|---|---|
| `read_file` | the file (hashed when read) |
| `web_search` | the query node, plus every URL it returned (`returned` edges) |
| `web_extract` | each URL, with a SHA-256 of the extracted content and the fetch time |
| `skill_view` | the skill |
| `terminal`, `execute_code` | existing files and URLs named in the command or code |
| anything else | URLs anywhere in the arguments; files named by `path`, `file`, `input`, `source`, `paths`, `files` |

## Evidence scan

When an artifact is recorded, its text (`write_file` content or `patch` text when available,
otherwise the file read back, up to `evidence_max_bytes` = 2 MB and scanned up to 512 KB) is checked
against every source this session has read **and every artifact it has produced**. A match on a
URL or on a file path / basename becomes an `evidenced` `derived_from` edge. An earlier artifact that
feeds a later one shows up in the receipt as a `parent`.

Then the turn's consumed sources become `inferred` (0.6) and the recent-turn window's become
`inferred` (0.35), skipping anything already evidenced.

## Receipts

`build_receipt` assembles, for one artifact: hashes and versions, the producing tool call with its
redacted arguments, the model calls that issued it (with usage), the agent chain (child → parent),
the turn (prompt preview and hash), sources sorted strongest-basis-first, parents, every tool call
of the producing turn in order, and the basis legend. The body is hashed (`receipt_sha256`) and
signed with HMAC-SHA256 under a per-install random key (`plugin-data/lineage/install.key`, 0600).
The signature means "this install issued this receipt" and nothing more; the receipt says so.

`verify_receipt_file` checks a sidecar against the file beside it with no database and no Hermes:
artifact hash, receipt hash, and the signature if the key is local.

## Verify

`verify` re-hashes the artifact and every file source and compares. URL sources are `unchecked`
unless `--refetch`; then they are fetched (public hosts only, every redirect hop included — Hermes's `is_safe_url` when available,
otherwise a local resolver check that refuses loopback, private and link-local addresses), bounded to
1 MiB. Because the recorded hash is of Hermes's *extracted* text, a raw fetch cannot reproduce it;
the first refetch stores a baseline fingerprint (hash, ETag, Last-Modified) and later runs report
`unchanged since` / `changed since` that date.

Verdicts: `fresh`, `stale` (a source changed), `modified` (the artifact changed), `missing`.

## What is stored, and what is not

* Tool arguments keep their shape; strings over 400 chars become `{"$preview", "$chars"}`; keys named
  like credentials (`api_key`, `token`, `password`, `headers`, `cookie`…) are masked outright.
* Previews of prompts, replies and results are single-line, ≤ 200–240 chars, redacted.
* Redaction masks vendor key prefixes, `key=value` and `key: value` credential pairs, `Bearer …`,
  PEM blocks, `FOO_TOKEN=…` in shell text, `user:pass@` in URLs and credential-shaped query values.
* `store_previews: false` keeps ids, hashes, sizes and timestamps only.
* The database is `<HERMES_HOME>/plugin-data/lineage/lineage.db` (SQLite, WAL). `hermes lineage prune
  --days N` deletes old sessions and everything hanging off them.

## Desktop half

`desktop/plugin.js` is plain ESM that imports only `@hermes/plugin-sdk` and `react`. It contributes a
route (`/lineage`), a sidebar entry, a status-bar chip, and a ⌘K command, and talks to
`dashboard/plugin_api.py` through `ctx.rest` (`/api/plugins/lineage/…`). The graph is laid out in
four semantic columns — sources, tool calls, intermediate artifacts, the artifact — with
column-spanning edges routed along a bus above or below the rows. `broadcast_plugin_event` pushes
`artifact.created` and `turn.closed` so the page refreshes as the agent works; polling is the
fallback.
