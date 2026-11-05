## lineage — provenance receipts and a graph for everything the agent produces

Adds `plugin-catalog/lineage.yaml` (community tier, `tools` category), pinned to `9a5c2ba` = tag `v0.1.1` of https://github.com/P0GL0L/hermes-lineage.

**What it does.** Every file Hermes writes gets a receipt — what it was made from (files read, pages fetched, searches run), by which tool call, model and agent, with SHA-256 hashes — and a Desktop page draws the graph from the file back to its sources. Links are labelled **observed** (Hermes reported it), **evidenced** (the artifact's own bytes name the source) or **inferred** (read earlier this turn / session, with confidence), never blended. `hermes lineage verify` reports fresh / stale / modified / missing; `hermes lineage check <file>.receipt.json` verifies a receipt with no database.

**Disclosure.** Hooks only — observer hooks only, deliberately no `pre_tool_call` (`post_tool_call`, `pre_llm_call`, `post_llm_call`, `post_api_request`, `api_request_error`, `post_auxiliary_call`, `subagent_start`, `subagent_stop`, `on_session_start`, `on_session_end`); no tools, no middleware, no LLM calls, no credentials; runs no commands. Reads files the agent wrote (hash up to 32 MB, text scan up to 512 KB) and stats the working tree once after `terminal`/`execute_code` calls (bounded by file count and time). Writes only under `<HERMES_HOME>/plugin-data/lineage/` (SQLite, `install.key`) and, on request or with `sidecar: true`, `<file>.receipt.json` beside files the agent produced. Stores redacted, bounded previews of prompts, tool arguments and results (`store_previews: false` keeps ids and hashes only); URL ids drop userinfo and mask credential-shaped query values. Network only on an explicit `verify --refetch` / *Verify + refetch URLs*: `GET` to the recorded source URLs, public hosts only (Hermes's `is_safe_url` when available). No telemetry.

**Validation.** `hermes plugins validate` passes every check on Hermes Agent v0.22.7 (macOS) — security scan *safe*, no core override, desktop surface inside the SDK. 17 pytest + Desktop (jsdom) tests in the repo; CI runs them and `hermes plugins validate` against Hermes main on every push.

**Desktop half.** `desktop/plugin.js` uses only `@hermes/plugin-sdk` and `react`; `dashboard/plugin_api.py` mounts under `/api/plugins/lineage/`. Under `plugins.isolation: host` the CLI subcommand is not wired (noted in the README); the slash command and Desktop backend still work.

Maintainer: P0GL0L (repository owner). Built for the Hermes Agent Plugin Hackathon, November 2026.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01F1DXuWjUkUGYSqHhtAutEe
