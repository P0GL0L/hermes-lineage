# Changelog

## 0.1.5 — 2026-11-05

From the catalog review (thanks, Teknium) and a second independent review of the code. Every item has a test that fails on the code before it.

**Security and honesty**
- `verify --refetch` checks every redirect hop against the same public-host rule as the first URL, at most 5 hops. A public page can no longer bounce it to loopback, a private network or a metadata address. The fallback check now refuses everything that is not globally routable, including 100.64.0.0/10 (CGNAT, Tailscale).
- `hermes lineage check` hashes the file *beside* the receipt, not the path the receipt was issued for, so a tampered copy no longer passes by pointing at the untouched original. An `INVALID` signature now fails the check.
- Redaction covers tokens in URL paths (Telegram `/bot<id>:<secret>/`), any query parameter whose name says credential (`X-Amz-Signature`, `X-Amz-Security-Token`, `client_secret`, …), `user:password@` in any scheme (`postgres://`, `redis://`), and camelCase argument names (`accessToken`).
- Files merely *named* in a shell command are no longer opened when they look like credentials (`~/.ssh/*`, `~/.aws`, `.env*`, `*.pem`, `*.key`, `id_rsa`); the working-tree scan skips them too. The disclosure says files named in commands are hashed.
- Which files a shell command wrote, and which named files it read, is now labelled **inferred** (it comes from the working tree and the command text, not from Hermes). **Observed** means Hermes reported it, as documented.
- Receipt sidecars are written with `O_EXCL | O_NOFOLLOW` and never through a symlink.
- Desktop opens only `http(s)` links; search results with other schemes are not recorded.

**Correctness**
- Reading an artifact no longer replaces its recorded hash: if someone edits the file and the agent reads it, Verify says *modified* instead of *fresh*.
- A rewritten artifact's sources are re-derived: version 2 no longer inherits version 1's evidence. Earlier hashes stay in `versions`.
- Hermes's `on_session_start` (which carries no parent id) no longer overwrites a subagent's role with `main`.
- A command's outputs are not also recorded as its inputs (`python make.py > summary.txt`). Renames are caught (`mv` updates ctime) and linked to the earlier artifact with the same bytes.
- V4A `*** Move File: a -> b` and `*** Delete File:` are understood with Hermes's own header grammar; evidence comes from the file after the patch, not from removed lines.
- Files written by a command that exits non-zero (`pytest --junitxml=…` with failures) are recorded, marked `call_status: error`.
- A deleted source makes the artifact *stale*.
- Two sources with the same file name in different folders are told apart by the path in the text.
- `prune` keeps nodes that a newer session still reads or cites.
- `resolve` handles `./name`, dotfiles and `_`/`%` in names.

**Second pass.** The reviewer then attacked the fixes themselves and found ten more problems (six introduced by them); all fixed, with `tests/test_verify_pass.py`:
- A later edit no longer drops sources the file still names (re-derivation considers the previous version's sources, whichever session read them).
- `chmod`/`ln` no longer make a file an artifact: a ctime-only change counts only when it is a move of an artifact.
- "Same bytes" links only for moves or same-session copies of 32+ bytes, labelled *inferred* — two `"\n"` files are not one file.
- Credential query names are matched word by word, so `?author=`, `?keyword=` and `?zipcode=` keep distinct URLs.
- File-name matching respects path boundaries (`data.csv` is not in `metadata.csv`) and full paths of short names match again.
- `check` fails a receipt whose signature was removed (`absent`); a key this install doesn't hold reads `other install`.
- `prune` never re-credits an artifact to a session that only read it.
- `redis://:password@` (empty user) is masked; a command's `> /elsewhere/out.md` output is not an input; a blocked command records nothing.

**Third pass.** Five more, all fixed, with `tests/test_round3.py`:
- `check` accepts only a *valid* signature for `ok`: a made-up signature naming some other key no longer passes. On another install, `file_matches_receipt` reports what can be known there.
- `prune` keeps the whole record of a session that made an artifact still in use, so its receipt keeps producer and sources.
- Credential parameter names: `authtoken`, `secretkey`, `apiKey2` are masked again; `country_code` and `promo_code` are not (only the OAuth `code`).
- `-o` counts as an output only for programs where it means one (curl, gcc, go, pandoc, …), not `unzip -o` or `grep -o`.
- Timed-out and interrupted commands (`exit_code` -1) ran and may have written files, so they are scanned; only what Hermes marks blocked, pending approval or disabled is skipped.

**Fourth pass.** Four more (none high), fixed, with `tests/test_round4.py`:
- A shell call counts as having run only on positive evidence — an exit code other than -1, or a timed-out/interrupted cell — so Hermes's guard and argument rejections record nothing.
- `curl -fsSLo FILE` and `sudo`/`env`-wrapped commands are recognised as naming an output.
- `prune` is a fixed point: a kept session's sources shared with an even older session stay.
- `page_token`, `nextPageToken` and other pagination cursors are not masked, so paginated pages stay distinct URLs.

**Performance**
- Each tool call's writes go into one SQLite transaction; inferred links per artifact are capped (`turn_max_inferred_sources`, 25 newest). A turn with 400 reads and a command writing 50 files went from 21,753 edges and 8.7 s inside the hook to 1,403 edges and about 0.4 s (measured in this container).
- Dashboard API routes are plain functions, so FastAPI runs them in its threadpool; *Verify + refetch URLs* no longer blocks the Hermes web server's event loop.

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
