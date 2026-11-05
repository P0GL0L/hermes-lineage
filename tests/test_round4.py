"""Final-pass checks. Each asserts the correct behaviour and FAILS on HEAD (cffccdc)."""
import json, time
from importlib import import_module


def _m(plugin, name):
    return import_module(f"{plugin.__name__}.lineage_core.{name}")


def _store(plugin):
    return plugin.get_tracker().store


def test_F1_rejected_commands_did_not_run(plugin):
    T = _m(plugin, "tracker").Tracker
    guard = json.dumps({"output": "", "exit_code": -1, "status": "error",
                        "error": "Terminal pre-execution guard did not finish within 5s (process-identity probe wedged); the command was not run. Retry the call."})
    fg = json.dumps({"output": "", "exit_code": -1, "status": "error",
                     "error": "This looks like a long-running server; run it with background=true."})
    bad_arg = json.dumps({"error": "terminal received a 'code' parameter, but it requires a shell command in 'command'."})
    for r in (guard, fg, bad_arg):
        assert T._command_ran(r, "error") is False, r


def test_F2_combined_short_flags_name_an_output(plugin, session_factory, workspace, tmp_path):
    st = _store(plugin)
    out = tmp_path / "elsewhere" / "page.html"; out.parent.mkdir()
    s = session_factory(); s.start_turn("t"); s.model_call()
    tc = s.tool("terminal", {"command": f"curl -fsSLo {out} https://example.com/"}, '{"exit_code":0}',
                during=lambda: out.write_text("<html>"))
    n = st.get_node(f"file:{out}")
    assert n and n["produced"], n
    assert not [e for e in st.edges_from(f"tc:{tc}", "read") if e["dst"] == f"file:{out}"]


def test_F3_prune_keeps_sources_first_seen_by_an_older_session(plugin, session_factory, workspace):
    R = _m(plugin, "receipt"); st = _store(plugin)
    (workspace / "revenue.csv").write_text("q,r\nQ3,15\n"); time.sleep(1.2)
    s0 = session_factory(); s0.start_turn("t"); s0.model_call()
    s0.tool("read_file", {"path": "revenue.csv"}, "q,r"); s0.end()
    s1 = session_factory(); s1.start_turn("t"); s1.model_call()
    s1.tool("read_file", {"path": "revenue.csv"}, "q,r")
    p = workspace / "report.md"; body = "Q3 was 15 per revenue.csv\n"
    s1.tool("write_file", {"path": "report.md", "content": body}, "ok", during=lambda: p.write_text(body)); s1.end()
    time.sleep(1.2)
    old = time.time() - 200 * 86400
    for sid in (s0.sid, s1.sid):
        st._conn.execute("update sessions set ended_at=?, started_at=? where id=?", (old, old, sid))
    st._conn.commit()
    s2 = session_factory(); s2.start_turn("read"); s2.model_call()
    s2.tool("read_file", {"path": "report.md"}, body)
    assert R.build_receipt(st, str(p))["sources"]
    st.prune(time.time() - 90 * 86400)
    assert R.build_receipt(st, str(p))["sources"], "kept artifact lost a source an older pruned session saw first"


def test_F4_page_tokens_keep_pages_apart(plugin):
    H = _m(plugin, "hashing")
    a = H.normalize_url("https://www.googleapis.com/youtube/v3/search?q=x&pageToken=CAUQAA")
    b = H.normalize_url("https://www.googleapis.com/youtube/v3/search?q=x&pageToken=CAoQAA")
    assert a != b, a
