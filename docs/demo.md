# The 90-second demo

One take, screen-recorded in Hermes Desktop with the Lineage page open in a split beside the chat.
Narration is in *italics*; keep it under the recording, no music.

| t | on screen | say |
|---|---|---|
| 0–8 s | Chat. Type: *"Write me a one-page Q3 revenue note with a chart. Use revenue.csv and two public sources on SaaS growth benchmarks."* Hit enter. | *Hermes is about to search the web, read a CSV, run Python, and write a report. Where will the numbers in that report actually come from?* |
| 8–30 s | The agent works: `web_search`, `web_extract` on two pages, `read_file revenue.csv`, a `terminal` call that runs `analysis.py` and writes `chart.png`, then `write_file q3-note.md`. The Lineage status-bar chip ticks to **3 artifacts**. | *While it works, lineage is listening to Hermes's own hooks — no tools, no middleware, nothing in the agent's way.* |
| 30–45 s | Click the chip. The Lineage page opens on `q3-note.md`: sources → tool calls → intermediate → artifact. | *Here's the note. Two pages it fetched, the CSV it read, the chart and the script the terminal call produced, and the model call that issued each tool call.* |
| 45–58 s | Click `chart.png`, then `revenue.csv`. Point at the badges. | *Every link is labelled. **Observed**: Hermes said so. **Evidenced**: the file itself names the source. **Inferred**: read earlier — and it says so, with a confidence, instead of pretending.* |
| 58–70 s | Back on the artifact. Click **Verify** → *fresh*. Switch to the terminal, append a row to `revenue.csv`, click **Verify** again → *stale: 1/5 sources changed*. | *Sources move. Lineage tells you when a report is older than the data under it.* |
| 70–82 s | Click **Write receipt**. Finder reveals `q3-note.md.receipt.json` beside the note. Open it: hashes, sources, models, agent chain, signature. In a terminal: `hermes lineage check q3-note.md.receipt.json` → `"ok": true`. | *The receipt travels with the file. Anyone with the file can check the hash — no Hermes required.* |
| 82–90 s | Click **Copy Mermaid**, paste into a Mermaid viewer, the same graph renders. Cut to the banner. | *Lineage. Where did this file come from — answered, every time.* |

## Before recording

- Fresh profile: `HERMES_HOME=~/.hermes-demo hermes plugins install /path/to/hermes-lineage && hermes plugins enable lineage`.
- Desktop: **Capabilities → Plugins → Rescan**, toggle Lineage on, confirm the sidebar entry.
- Put `revenue.csv` in the workspace with 3 rows; pick two real public pages the agent can fetch.
- `sidecar: false` (so the "Write receipt" click is the moment the file appears).
- Run the task once off-camera so the local model is warm and the pages are reachable.
- Window: 1600×900, dark theme, chat left / Lineage right.

## Posting

- Tweet: ≤90 s video, tag **@NousResearch**, one line: *"lineage — provenance receipts for everything your Hermes agent produces. Every file gets a receipt: sources, hashes, model, tool call, agent chain. Observed / evidenced / inferred, labelled. Hooks only; no network unless you ask for a refetch. `hermes plugins install lineage`"* + repo link.
- Catalog PR: copy `catalog/lineage.yaml` into `hermes-agent/plugin-catalog/`, set `sha` to the release commit (40 hex), paste the Disclosure line into the PR description.
- Submission form: catalog PR link + tweet link. Deadline **Nov 11, 2026, 5:00 PM EST** (confirm on the announcement post).
