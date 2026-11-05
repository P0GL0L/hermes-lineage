# Changelog

## 0.1.5 — 2026-11-05

From the catalog review (thanks, Teknium): the disclosure now says *redirects are followed* on `verify --refetch`, and they were — without a second check.

- `verify --refetch` checks every redirect hop against the same public-host rule as the first URL (Hermes's `is_safe_url` when available), at most 5 hops. A public page can no longer bounce the request to loopback, a private network or a cloud metadata address; the source reports `refused` with the redirect target instead. Reachable results record `final_url` when a redirect was followed.
- README and catalog disclosure match the reviewed wording.

## 0.1.4 — 2026-11-05

Found while recording the demo: a later session appended a row to `revenue.csv`, and Verify on the note built from it still said *fresh*.

- Each `derived_from` link now snapshots the source's hash and size at the moment the artifact is linked to it. Verify compares the file on disk with that snapshot, not with the source node's latest hash, which a later read or write in another session moves.
- Links recorded by 0.1.3 and earlier have no snapshot; Verify still calls the source *changed* when a write to it is recorded after the link.
- The provenance graph is drawn as of each link: a source rewritten after the artifact was built shows as the source it was then, without the later tool call and without being re-labelled an intermediate artifact. Receipts list the source hash as of the build.
- Verify no longer moves the artifact's `updated_at`, so the list order and "written … ago" stay true after a check.

## 0.1.3 — 2026-11-05

- A page or file first seen in an earlier session now counts as this session's source when this session reads it again. Before, evidence matching and the session window only saw nodes created in the current session, so a re-fetched URL that the artifact cites stayed *inferred* instead of *evidenced*.

## 0.1.2 — 2026-11-05

From the first Desktop session on Hermes Agent v0.22.7 (macOS).

- URLs end at a backslash, so an escaped quote in a shell command (`\"https://…\"`) no longer leaves a `\` on the source node.
- XML namespace names (`http://www.w3.org/2000/svg`, xhtml, xlink, MathML) are no longer recorded as fetched pages; every SVG the agent writes names one.

## 0.1.1 — 2026-11-05

Fixes from the first live run on Hermes Agent v0.22.7 (macOS).

- Model-call nodes keep `model`, `provider`, `base_url` and `started_at` from `post_api_request`.
- Plain-text account shows `0 bytes` for empty files instead of `?`, and omits an unknown provider.
- Receipts take `last_written_at` from the newest recorded version, not the last metadata update.
- README: validation result; `plugins.isolation: host` note (CLI subcommand not wired there).

## 0.1.0 — 2026-11-05

First release, built for the Hermes Agent Plugin Hackathon.

- Observer plugin: ten observer hooks (deliberately no `pre_tool_call`, which is fail-closed in Hermes) turn tool calls, model calls, subagents and turns into a provenance graph (SQLite under `plugin-data/lineage/`).
- Artifact attribution for `write_file`/`patch` (observed), `terminal`/`execute_code` via a bounded mtime scan of the working tree after the call (observed), and any tool whose result names a file it just made (inferred).
- Sources: `read_file`, `web_search` queries and the URLs they return, `web_extract` pages with content hashes, `skill_view`, paths and URLs named in commands/code or in any tool's arguments.
- Three labelled bases for every link: observed, evidenced (the artifact's bytes name the source), inferred (read earlier this turn / recent turns), with confidence.
- Receipts (`lineage.receipt.v1`) with hashes, agent chain, models, sources, transformations, parents; HMAC-signed per install; `<file>.receipt.json` sidecars on demand or with `sidecar: true`.
- Verify: artifact and file-source hashes; optional URL re-fetch with a baseline fingerprint; verdicts fresh / stale / modified / missing.
- `/lineage` slash command and `hermes lineage` CLI (list, show, receipt, verify, check, graph as Mermaid/DOT/JSON, stats, sessions, prune).
- Desktop page (sources → tool calls → intermediate → artifact), inspector with Verify / Write receipt / Copy Mermaid / Reveal, status-bar chip, ⌘K command, live refresh via `broadcast_plugin_event`.
- Redaction and bounding of everything stored, including URL ids (no userinfo, masked credential-shaped query values); `verify --refetch` refuses non-public hosts; no tools, no middleware, no network at runtime.
