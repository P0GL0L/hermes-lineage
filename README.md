<p align="center">
  <img src="docs/banner.png" alt="lineage — where did this file come from?" width="800">
</p>

# lineage

**Provenance for everything your Hermes agent produces.**

Every file Hermes writes gets a receipt: what it was made from (files read, pages fetched, searches run), by which tool call, model and agent, with SHA-256 hashes — and a Desktop page draws the graph from the file back to its sources. Three kinds of link, always labelled, never blended:

| basis | meaning | drawn as |
|---|---|---|
| **● observed** | Hermes reported it: `write_file` wrote this path; this tool call came out of that model request; this child agent was spawned by that turn. | thick accent line |
| **◉ evidenced** | The artifact's own bytes name the source — a URL that was fetched this session, or the path of a file that was read. | solid line |
| **○ inferred** | The source was read earlier in the same turn (confidence 0.6) or in a recent turn of the session (0.35). | dashed line |

Hooks only — and observer hooks only: lineage does not register `pre_tool_call`, which Hermes treats as fail-closed (a slow callback would block the agent's tool call). No tools, no middleware, no LLM calls, no network. A provenance recorder that can interrupt the agent is worse than no recorder, so every callback swallows its own errors.

```
hermes plugins install lineage
hermes plugins enable lineage
```

Then ask Hermes to make something, and:

```
/lineage                      artifacts made in this session
/lineage q3-note.md           where it came from
/lineage receipt q3-note.md   write q3-note.md.receipt.json beside it
/lineage verify q3-note.md    check the file and its sources against the recorded hashes
```

Or open **Lineage** in the Desktop sidebar.

## What you get

### The graph

<p align="center"><img src="docs/graph.png" alt="Desktop page: sources → tool calls → artifacts" width="800"></p>

Sources on the left, tool calls in the middle (annotated with the model call that issued them), artifacts on the right. Click anything: a URL shows its content hash at fetch time and which tool read it; a tool call shows its redacted, bounded arguments and what it wrote; the artifact shows its hash, versions, and the actions below.

### The receipt

`report.pdf.receipt.json`, written beside the file on request (or automatically with `sidecar: true`):

```json
{
  "schema": "lineage.receipt.v1",
  "artifact": { "path": "/work/q3-note.md", "sha256": "4f1c…", "size": 182, "versions": [ … ], "cited_in_final_reply": true },
  "produced_by": { "tool": "write_file", "tool_call_id": "call_…", "at": "2026-11-05T01:12:09Z", "duration_ms": 12 },
  "agent": { "session_id": "…", "chain": [ { "role": "main", "model": "hermes-4-405b", "platform": "cli" } ] },
  "turn": { "user_prompt_preview": "Write a short Q3 revenue note…", "user_prompt_sha256": "…" },
  "models": [ { "model": "hermes-4-405b", "provider": "nous", "usage": { "prompt_tokens": 1200, "completion_tokens": 80 } } ],
  "sources": [
    { "kind": "url",  "locator": "https://example.com/benchmarks/2026", "sha256": "…", "sha256_of": "extracted page content at fetch time", "basis": "evidenced", "reason": "url appears in artifact" },
    { "kind": "file", "locator": "/work/revenue.csv", "sha256": "…", "basis": "evidenced", "reason": "path appears in artifact" },
    { "kind": "file", "locator": "/work/out/chart.png", "basis": "evidenced", "produced_by_agent": true }
  ],
  "transformations": [ { "tool": "web_search", … }, { "tool": "web_extract", … }, { "tool": "terminal", … }, { "tool": "write_file", "wrote_this": true } ],
  "basis_legend": { … },
  "integrity": { "receipt_sha256": "…", "signature": { "alg": "HMAC-SHA256", "key_id": "…" } }
}
```

Anyone with the file and the receipt can check the hash — no Hermes needed:

```
hermes lineage check q3-note.md.receipt.json      # or: python -c "import json,hashlib; …"
```

The signature is from a per-install random key (`plugin-data/lineage/install.key`, mode 0600). It lets *this install* recognise receipts it issued; it is not a third-party attestation and the receipt says so.

### Verify, and staleness

```
hermes lineage verify q3-note.md               # file + file sources against recorded hashes
hermes lineage verify q3-note.md --refetch     # also re-fetch URL sources (the only network lineage ever does; public hosts only)
```

Verdicts: **fresh** (nothing moved), **stale** (a source changed since the artifact was made), **modified** (the artifact itself changed), **missing**. The Desktop list marks each artifact with its last verdict.

URL change detection is honest about its limits: the hash recorded at fetch time is of the *extracted* text, which a raw re-fetch cannot reproduce, so the first `--refetch` sets a baseline fingerprint and later runs report *unchanged since* / *changed since* that date.

### Exports

```
hermes lineage graph q3-note.md                  # Mermaid (thick = observed, solid = evidenced, dashed = inferred)
hermes lineage graph q3-note.md --format dot
hermes lineage graph q3-note.md --format json
hermes lineage show  q3-note.md                  # plain-text account
hermes lineage show  q3-note.md --json           # the receipt
hermes lineage list | sessions | stats | prune --days 90 --yes
```

`/lineage mermaid <file>` in chat returns the Mermaid block, ready for a diagram tool.

## How it works

lineage registers ten observer hooks (`post_tool_call`, `pre/post_llm_call`, `post_api_request`, `api_request_error`, `post_auxiliary_call`, `subagent_start/stop`, `on_session_start/end`) and writes a graph to SQLite at `<HERMES_HOME>/plugin-data/lineage/lineage.db`.

* **Files the agent writes** — `write_file` and `patch` (including V4A multi-file patches) are observed directly. For `terminal` and `execute_code`, lineage walks the working tree once after the call (up to `snapshot_max_files`, bounded to `snapshot_max_seconds`) and attributes every regular file whose mtime falls inside the call's window, so `python report.py > out.csv` attributes `out.csv` to that command. At most `max_artifacts_per_call` (50) files per call; a bigger write is recorded as truncated. Any other tool whose result names a path that exists and was modified during the call (image generators, converters, MCP tools) gets an *inferred* `wrote` edge.
* **Sources** — `read_file`, `web_search` (the query, and the URLs it returned), `web_extract` (each URL, with a hash of the extracted content), `skill_view`, paths and URLs named in a shell command or code, and paths/URLs in the arguments of any other tool.
* **Evidence** — when an artifact is written, its text (bounded to `evidence_max_bytes`) is scanned for URLs and file paths that match sources of this session; matches become *evidenced* edges. Everything read earlier in the turn becomes an *inferred* edge at 0.6; the last `session_window_turns` turns at 0.35. An earlier artifact that feeds a later one shows up as a `parent`.
* **Model calls** — `post_tool_call` carries `api_request_id`, so the model request that issued each tool call is an *observed* `invoked` edge, with model, provider and usage from `post_api_request`.
* **Agents** — `subagent_start/stop` link child sessions to the parent session and turn; receipts carry the whole chain.
* **The reply** — when a turn ends, URLs and files named in the final answer get `cited_in_reply` edges, and the receipt says whether the artifact was cited.

Everything stored is bounded and redacted: tool arguments keep their shape, long values become `{"$preview", "$chars"}`, and anything credential-shaped (vendor key prefixes, `token=`, `Bearer …`, PEM blocks, `FOO_KEY=…` in commands) is masked before it reaches disk. URL nodes drop `user:pass@` and mask credential-shaped query values (`?token=`, `?key=`, `?sig=`…) in the id itself, so a secret-bearing URL never becomes a node. A blocked or failed tool call records nothing it would have read or written. `store_previews: false` keeps ids and hashes only.

## Configuration

`~/.hermes/config.yaml`:

```yaml
plugins:
  enabled: [lineage]
  entries:
    lineage:
      settings:
        sidecar: false            # write <file>.receipt.json beside every artifact when its turn ends
        store_previews: true      # short redacted previews of prompts, args and results (false = ids and hashes only)
        track_aux_calls: true     # record compression/title/vision model calls as nodes in the turn
        snapshot_max_files: 6000  # largest working tree scanned after a terminal/execute_code call
        session_window_turns: 3   # earlier turns that count as inferred sources (0 disables)
```

## Desktop

The unified package ships `desktop/plugin.js` (SDK only: `@hermes/plugin-sdk` and `react`) and `dashboard/plugin_api.py` (routes under `/api/plugins/lineage/`). Enable the Desktop half in **Capabilities → Plugins**; the Python half is gated separately by `plugins.enabled`, which is the security boundary. You get a sidebar page, a status-bar chip with the artifact count for the focused chat, and a ⌘K command. Live refresh comes from `broadcast_plugin_event` when a turn produces something, with polling as the fallback.

## Limits, stated plainly

* lineage sees what Hermes's hooks report. A file written by a background process that outlives the tool call, or by anything outside Hermes, is not attributed.
* *Inferred* means inferred. A dashed edge says "read earlier", not "used". The receipt's `basis` and `confidence` fields exist so a reader can discount them.
* Working trees larger than `snapshot_max_files` (or slower than `snapshot_max_seconds` to walk) are not scanned after terminal calls; `write_file`/`patch` attribution still works there. A file another process modifies during a terminal call's window can be attributed to that call.
* URL content hashes are of Hermes's extracted text; raw re-fetches establish their own baseline (see Verify).
* The signature proves which install issued a receipt, nothing more.
* Under `plugins.isolation: host`, Hermes runs user plugins in a separate host process: the Desktop backend is still served (responses are buffered) and `/lineage` still works, but `ctx.register_cli_command` is not wired there, so `hermes lineage …` is unavailable until the plugin runs in-process (the default).

## Disclosure

Hooks only (`post_tool_call`, `pre_llm_call`, `post_llm_call`, `post_api_request`, `api_request_error`, `post_auxiliary_call`, `subagent_start`, `subagent_stop`, `on_session_start`, `on_session_end`); no tools, no middleware, no LLM calls, no credentials. Runs no commands. Reads files the agent wrote (to hash them, up to 32 MB, and scan their text for sources, up to 512 KB) and stats the working tree once after `terminal`/`execute_code` calls (bounded by file count and time). Receipts and verify apply only to files the agent produced, never to files it only read. Writes only under `<HERMES_HOME>/plugin-data/lineage/` (SQLite store, `install.key`) and, on request or with `sidecar: true`, `<file>.receipt.json` beside artifacts. Stores redacted, bounded previews of prompts, tool arguments and results (`store_previews: false` turns previews off). Network only when you run `verify --refetch` or press *Verify + refetch URLs*, and then only `GET` to the recorded source URLs on public hosts (loopback, private and link-local addresses are refused, through Hermes's own `is_safe_url` when available). No telemetry.

## Validated

`hermes plugins validate` on Hermes Agent v0.22.7 (macOS): every check passes — manifest, requires_hermes, config schema, loadable, capability probe, declared tools/hooks/middleware, built-in collisions, security scan *safe*, no core override, desktop surface inside the SDK. A live task on that install produced a tracked artifact whose receipt verified with `hermes lineage check`.

## Development

```
python -m pytest -q tests          # 17 tests: tracker, receipts, redaction, CLI, slash command, API, review regressions
cd tests-js && npm install && npm test   # Desktop half under jsdom against Python-generated fixtures
hermes plugins validate .          # the catalog admission checks
```

To develop the Desktop half live: `ln -s $(pwd) ~/.hermes/plugins/lineage`, enable it, and every save of `desktop/plugin.js` hot-reloads.

## License

MIT — Charles Bonetti / ASV Labs. Built for the Hermes Agent Plugin Hackathon, November 2026.
