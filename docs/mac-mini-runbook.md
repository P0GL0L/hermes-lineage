# First live run (Mac mini / MacBook)

The plugin was built and tested against the Hermes source contracts in a container that could
not run Hermes itself. This is the checklist for the first run on a real install. Do it in order;
each step has a visible pass signal.

```bash
# 0. Hermes ≥ 0.21.5 (plugin.yaml declares the floor)
hermes --version

# 1. Install from the checkout (symlink keeps Desktop hot-reload working while editing)
git clone https://github.com/P0GL0L/hermes-lineage ~/src/hermes-lineage
ln -s ~/src/hermes-lineage ~/.hermes/plugins/lineage
hermes plugins validate ~/src/hermes-lineage      # PASS on every check; read any 'caution' line
hermes plugins enable lineage                     # answers the consent prompt (no capabilities declared)
hermes plugins list | grep lineage                # enabled, 10 hooks, 1 command

# 2. Restart the gateway so dashboard/plugin_api.py mounts
hermes gateway restart   # or quit + reopen Hermes Desktop
tail -n 50 ~/.hermes/logs/errors.log | grep -i lineage   # must be empty
curl -s http://127.0.0.1:8642/api/plugins/lineage/health  # {"ok": true, ...} (port/auth per your setup)

# 3. One real task
hermes chat --query 'Create notes.md that summarises https://hermes-agent.nousresearch.com/docs/ in three bullets, then write a second file summary.txt from notes.md.'
hermes lineage list                 # both files, with source counts
hermes lineage show summary.txt     # 'written by write_file', parent notes.md, the URL as a source
hermes lineage verify summary.txt   # verdict: fresh
hermes lineage receipt summary.txt --write && hermes lineage check summary.txt.receipt.json   # "ok": true

# 4. Terminal attribution (the snapshot path)
hermes chat --query 'Run: python3 -c "open(\"out.txt\",\"w\").write(\"hi\")" in the current directory, then tell me the file exists.'
hermes lineage show out.txt         # written by terminal (observed)

# 5. Desktop
#   Capabilities → Plugins → Rescan → Lineage on. Sidebar shows "Lineage". Status bar shows "N artifacts".
#   Page: list → graph → inspector. Verify / Write receipt / Copy Mermaid / Reveal all respond.
#   ⌘K → "Lineage: where did this file come from?"

# 6. Subagent chain
hermes chat --query 'Delegate to a subagent: write facts.md with two facts about Mercury. Then write planet.md from facts.md.'
hermes lineage show planet.md       # parent facts.md; chain shows the subagent, then main
```

## What to look at if something is off

| symptom | look at |
|---|---|
| `validate` fails on *declared hooks* | the hook list in `plugin.yaml` must equal `HOOKS` in `__init__.py` (the tests assert this) |
| `/api/plugins/lineage/*` → 404 | plugin not in `plugins.enabled`, or the gateway was not restarted after enabling |
| a `terminal` write shows no artifact | the working tree is larger than `snapshot_max_files` (6000) or slower than 0.5 s to walk — raise the setting or run from a smaller directory |
| `written by terminal` missing for a file a script made | the file sits in an ignored directory (`.git`, `node_modules`, `dist`, `build`, …) or has an ignored suffix (`.pyc`, `.lock`, `.tmp`) |
| `api_request_id` absent on tool calls (no "via model" annotation) | the provider adapter did not supply it on `post_tool_call`; the tool call still links to its turn |
| Desktop half says *unavailable (remote backend)* | the agent half is on a remote host; install the Desktop half from Git with the Desktop target checked |

## Hook payload fields lineage reads

If a field is renamed upstream the plugin degrades, never breaks: every callback takes `**kwargs`
and reads with `.get()`. The fields that matter most: `session_id`, `turn_id`, `tool_call_id`,
`tool_name`, `args`, `result`, `status`, `api_request_id`, `model`, `provider`, `usage`,
`user_message`, `assistant_response`, `parent_session_id`, `child_session_id`, `child_role`,
`child_goal`, `cwd` (on `on_session_start`), `aux_task`.
