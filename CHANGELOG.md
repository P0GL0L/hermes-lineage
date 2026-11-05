# Changelog

## 0.1.0 — 2026-11-05

First release, built for the Hermes Agent Plugin Hackathon.

- Observer plugin: eleven hooks turn tool calls, model calls, subagents and turns into a provenance graph (SQLite under `plugin-data/lineage/`).
- Artifact attribution for `write_file`/`patch` (observed), `terminal`/`execute_code` via working-tree snapshot diff (observed), and any tool whose result names a file it just made (inferred).
- Sources: `read_file`, `web_search` queries and the URLs they return, `web_extract` pages with content hashes, `skill_view`, paths and URLs named in commands/code or in any tool's arguments.
- Three labelled bases for every link: observed, evidenced (the artifact's bytes name the source), inferred (read earlier this turn / recent turns), with confidence.
- Receipts (`lineage.receipt.v1`) with hashes, agent chain, models, sources, transformations, parents; HMAC-signed per install; `<file>.receipt.json` sidecars on demand or with `sidecar: true`.
- Verify: artifact and file-source hashes; optional URL re-fetch with a baseline fingerprint; verdicts fresh / stale / modified / missing.
- `/lineage` slash command and `hermes lineage` CLI (list, show, receipt, verify, check, graph as Mermaid/DOT/JSON, stats, sessions, prune).
- Desktop page (sources → tool calls → intermediate → artifact), inspector with Verify / Write receipt / Copy Mermaid / Reveal, status-bar chip, ⌘K command, live refresh via `broadcast_plugin_event`.
- Redaction and bounding of everything stored; no tools, no middleware, no network at runtime.
