"""End-to-end: a scripted research task produces a graph, a receipt and a verifiable sidecar."""

from __future__ import annotations

import json
import os
import time
from importlib import import_module


def _mod(plugin, name):
    return import_module(f"{plugin.__name__}.lineage_core.{name}")


def research_task(session, ws, *, run_terminal=True):
    """search -> extract two pages -> read a CSV -> run a script that writes a chart -> write the report."""
    csv = ws / "revenue.csv"
    csv.write_text("quarter,revenue\nQ1,10\nQ2,12\nQ3,15\n")
    time.sleep(0.15)  # the CSV predates the turn; keep it outside the terminal call's mtime window
    session.start_turn("Write a short Q3 revenue note with a chart, grounded in two public sources.")
    rid = session.model_call()
    session.tool("web_search", {"query": "Q3 2026 SaaS revenue growth benchmarks"},
                 {"success": True, "data": {"web": [
                     {"title": "Benchmarks", "url": "https://example.com/benchmarks/2026", "description": "x", "position": 1},
                     {"title": "Other", "url": "https://news.example.org/saas-q3", "description": "y", "position": 2},
                 ]}}, api_request_id=rid)
    rid = session.model_call()
    session.tool("web_extract", {"urls": ["https://example.com/benchmarks/2026", "https://news.example.org/saas-q3"], "format": "markdown"},
                 {"results": [
                     {"url": "https://example.com/benchmarks/2026", "title": "Benchmarks", "content": "Median growth was 18%."},
                     {"url": "https://news.example.org/saas-q3", "title": "News", "content": "Q3 was strong."},
                 ]}, api_request_id=rid)
    rid = session.model_call()
    session.tool("read_file", {"path": "revenue.csv"}, csv.read_text(), api_request_id=rid)
    chart = ws / "out" / "chart.png"
    if run_terminal:
        rid = session.model_call()

        def make_chart():
            chart.parent.mkdir(exist_ok=True)
            chart.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fakepng" * 10)
            (ws / "analysis.py").write_text("import csv\nrows=list(csv.reader(open('revenue.csv')))\n")

        session.tool("terminal", {"command": "python analysis.py revenue.csv && ls out/"}, "out/chart.png\n", during=make_chart,
                     api_request_id=rid)
    rid = session.model_call()
    report = ws / "q3-note.md"
    content = ("# Q3 revenue note\n\nRevenue grew to 15 (see revenue.csv).\n\n![chart](out/chart.png)\n\n"
               "Sources: https://example.com/benchmarks/2026 and https://news.example.org/saas-q3\n")

    def write_report():
        report.write_text(content)

    session.tool("write_file", {"path": "q3-note.md", "content": content}, "Wrote 180 bytes to q3-note.md", during=write_report,
                 api_request_id=rid)
    session.end_turn("Done. The note is in q3-note.md with the chart at out/chart.png, grounded in https://example.com/benchmarks/2026.")
    return report, chart, csv


def test_register_matches_manifest(plugin, ctx):
    import yaml  # PyYAML is only a test dependency

    manifest = yaml.safe_load((os.path.join(os.path.dirname(plugin.__file__), "plugin.yaml") and open(
        os.path.join(os.path.dirname(plugin.__file__), "plugin.yaml")).read()))
    assert set(ctx.recorded["hooks"]) == set(manifest["provides_hooks"])
    assert ctx.recorded["tools"] == [] and manifest["provides_tools"] == []
    assert ctx.recorded["middleware"] == [] and manifest["provides_middleware"] == []
    assert ctx.recorded["commands"] == ["lineage"]
    assert ctx.recorded["cli_commands"] == ["lineage"]


def test_register_survives_probe_context(plugin, hermes_home, monkeypatch):
    """The validator's probe passes a stub whose unknown attributes raise; register() must not touch any."""
    from conftest import RecordingContext

    monkeypatch.setattr(plugin, "_tracker", None)
    plugin.register(RecordingContext(context_methods=()))


def test_graph_receipt_and_sidecar(plugin, ctx, workspace, session_factory):
    store_mod = _mod(plugin, "store")
    receipt_mod = _mod(plugin, "receipt")
    s = session_factory()
    report, chart, csv = research_task(s, workspace)
    store = plugin.get_tracker().store

    node = store.find_file_node(str(report))
    assert node and node["produced"] == 1 and node["sha256"]
    assert node["meta"]["producer_tool"] == "write_file"

    rels = {(e["dst"], e["rel"], e["basis"]) for e in store.edges_from(node["id"])}
    # evidenced: the report names both URLs and the CSV and the chart
    assert (f"url:https://example.com/benchmarks/2026", "derived_from", "evidenced") in rels
    assert (f"url:https://news.example.org/saas-q3", "derived_from", "evidenced") in rels
    assert (f"file:{csv}", "derived_from", "evidenced") in rels
    assert (f"file:{chart}", "derived_from", "evidenced") in rels
    # observed: produced by this agent, in this turn
    assert (f"agent:{s.sid}", "produced_by", "observed") in rels
    # the chart was produced by the terminal call (snapshot diff) — observed
    chart_node = store.find_file_node(str(chart))
    assert chart_node and chart_node["produced"] == 1
    writers = store.edges_to(chart_node["id"], "wrote")
    assert writers and writers[0]["basis"] == "observed"
    tc = store.get_node(writers[0]["src"])
    assert tc["meta"]["tool"] == "terminal"
    # the terminal call read revenue.csv (named in the command) -> chart derived from csv (inferred this turn at least)
    chart_rels = {(e["dst"], e["rel"]) for e in store.edges_from(chart_node["id"])}
    assert (f"file:{csv}", "derived_from") in chart_rels
    # model call -> tool call is observed via api_request_id
    invoked = store.edges_to(tc["id"], "invoked")
    assert invoked and invoked[0]["basis"] == "observed" and invoked[0]["src"].startswith("mc:")
    # the final reply cited the report and a URL
    turn_edges = {(e["dst"], e["rel"]) for e in store.edges_from(f"turn:{s.turn_id}")}
    assert (node["id"], "cited_in_reply") in turn_edges
    assert ("url:https://example.com/benchmarks/2026", "cited_in_reply") in turn_edges

    receipt = receipt_mod.build_receipt(store, str(report))
    assert receipt["schema"] == "lineage.receipt.v1"
    assert receipt["artifact"]["sha256"] == node["sha256"]
    assert receipt["produced_by"]["tool"] == "write_file"
    assert receipt["models"] and receipt["models"][0]["model"] == "hermes-4-405b"
    assert receipt["agent"]["chain"][0]["session_id"] == s.sid
    assert receipt["turn"]["user_prompt_preview"].startswith("Write a short Q3")
    kinds = {(x["kind"], x["basis"]) for x in receipt["sources"]}
    assert ("url", "evidenced") in kinds and ("file", "evidenced") in kinds
    assert receipt["counts"]["sources"] == len(receipt["sources"]) >= 4
    assert receipt["artifact"]["cited_in_final_reply"] is True
    assert receipt["integrity"]["receipt_sha256"] and receipt["integrity"]["signature"]["alg"] == "HMAC-SHA256"
    # the url source carries the content hash recorded at extraction time
    url_src = next(x for x in receipt["sources"] if x["locator"] == "https://example.com/benchmarks/2026")
    assert url_src["sha256"] and url_src["sha256_of"].startswith("extracted")

    # sidecar round-trips and verifies without the database
    path = receipt_mod.write_sidecar(store, node["id"])
    assert path.name == "q3-note.md.receipt.json"
    check = receipt_mod.verify_receipt_file(str(path))
    assert check["ok"] and check["artifact_sha256_matches"] and check["receipt_sha256_matches"] and check["signature"] == "valid"

    # verify: fresh, then a source changes, then the artifact changes
    v = receipt_mod.verify(store, node["id"])
    assert v["summary"]["verdict"] == "fresh" and v["sidecar"]["present"]
    csv.write_text("quarter,revenue\nQ1,10\nQ2,12\nQ3,99\n")
    v = receipt_mod.verify(store, node["id"])
    assert v["summary"]["verdict"] == "stale"
    assert any(x["locator"] == str(csv) and x["status"] == "changed" for x in v["sources"])
    report.write_text("tampered")
    v = receipt_mod.verify(store, node["id"])
    assert v["artifact"]["status"] == "modified"
    check = receipt_mod.verify_receipt_file(str(path))
    assert not check["ok"] and check["artifact_sha256_matches"] is False

    # exports
    mm = receipt_mod.to_mermaid(store.neighborhood(node["id"]))
    assert mm.startswith("flowchart LR") and "==>|wrote|" in mm and "-.->|derived_from|" in mm or "-->|derived_from|" in mm
    dot = receipt_mod.to_dot(store.neighborhood(node["id"]))
    assert dot.startswith("digraph lineage")
    text = receipt_mod.explain(store, node["id"])
    assert "q3-note.md" in text and "written by write_file" in text and "evidenced" in text


def test_slash_and_cli(plugin, ctx, workspace, session_factory, capsys):
    s = session_factory()
    report, chart, csv = research_task(s, workspace, run_terminal=False)
    out = ctx.slash("", session_id=s.sid)
    assert "q3-note.md" in out and "artifacts" in out
    out = ctx.slash("q3-note.md", session_id=s.sid)
    assert "written by write_file" in out
    out = ctx.slash("receipt q3-note.md", session_id=s.sid)
    assert out.startswith("receipt written:") and os.path.exists(str(report) + ".receipt.json")
    out = ctx.slash("verify q3-note.md", session_id=s.sid)
    assert "verdict: fresh" in out
    out = ctx.slash("mermaid q3-note.md", session_id=s.sid)
    assert "```mermaid" in out
    out = ctx.slash("stats", session_id=s.sid)
    assert "artifacts" in out
    out = ctx.slash("nope-not-a-file.txt", session_id=s.sid)
    assert "nothing recorded" in out

    import argparse

    setup_fn, handler_fn = ctx.cli
    parser = argparse.ArgumentParser(prog="hermes lineage")
    setup_fn(parser)
    rc = handler_fn(parser.parse_args(["list"]))
    assert rc == 0 and "q3-note.md" in capsys.readouterr().out
    rc = handler_fn(parser.parse_args(["show", "q3-note.md", "--json"]))
    assert rc == 0 and json.loads(capsys.readouterr().out)["schema"] == "lineage.receipt.v1"
    rc = handler_fn(parser.parse_args(["verify", "q3-note.md"]))
    assert rc == 0 and "verdict: fresh" in capsys.readouterr().out
    rc = handler_fn(parser.parse_args(["check", str(report) + ".receipt.json"]))
    assert rc == 0 and json.loads(capsys.readouterr().out)["ok"] is True
    rc = handler_fn(parser.parse_args(["graph", "q3-note.md", "--format", "dot"]))
    assert rc == 0 and "digraph" in capsys.readouterr().out
    rc = handler_fn(parser.parse_args(["stats"]))
    assert rc == 0
    rc = handler_fn(parser.parse_args(["sessions"]))
    assert rc == 0 and s.sid in capsys.readouterr().out


def test_subagent_chain_and_session_window(plugin, ctx, workspace, session_factory):
    parent = session_factory()
    parent.start_turn("Delegate the research, then summarise.")
    parent.model_call()
    ctx.fire("subagent_start", parent_session_id=parent.sid, parent_turn_id=parent.turn_id, parent_subagent_id=None,
             child_session_id="child-1", child_subagent_id="sub-1", child_role="researcher", child_goal="find two sources")
    child = session_factory(session_id="child-1", parent_session_id=parent.sid)
    child.start_turn("find two sources")
    child.model_call()
    child.tool("web_extract", {"urls": ["https://example.com/a"]}, {"results": [{"url": "https://example.com/a", "content": "A"}]})
    notes = workspace / "notes.md"
    child.tool("write_file", {"path": "notes.md", "content": "see https://example.com/a"}, "ok", during=lambda: notes.write_text("see https://example.com/a"))
    child.end_turn("notes.md written")
    ctx.fire("subagent_stop", parent_session_id=parent.sid, child_session_id="child-1", child_role="researcher", status="ok",
             child_summary="done", duration_ms=1500, tool_call_history=[])
    # second parent turn: read the notes, write the summary — notes read earlier *this* turn, URL from the window
    parent.start_turn("Summarise notes.md")
    parent.model_call()
    parent.tool("read_file", {"path": "notes.md"}, notes.read_text())
    summary = workspace / "summary.md"
    parent.tool("write_file", {"path": "summary.md", "content": "Summary of the notes."}, "ok", during=lambda: summary.write_text("Summary of the notes."))
    parent.end_turn("summary.md written")

    store = plugin.get_tracker().store
    receipt = _mod(plugin, "receipt").build_receipt(store, str(summary))
    locators = {x["locator"]: x for x in receipt["sources"]}
    assert str(notes) in locators and locators[str(notes)]["basis"] == "inferred"  # read earlier this turn, not named in content
    assert locators[str(notes)]["produced_by_agent"] is True
    assert receipt["parents"] and receipt["parents"][0]["path"] == str(notes)
    child_receipt = _mod(plugin, "receipt").build_receipt(store, str(notes))
    chain = child_receipt["agent"]["chain"]
    assert [c["role"] for c in chain] == ["researcher", "main"]
    assert chain[0]["session_id"] == "child-1" and chain[1]["session_id"] == parent.sid
    edges = {(e["dst"], e["rel"]) for e in store.edges_from("agent:child-1")}
    assert (f"agent:{parent.sid}", "delegated_by") in edges


def test_failed_tool_and_blocked_tool_record_no_artifact(plugin, ctx, workspace, session_factory):
    s = session_factory()
    s.start_turn("try")
    s.model_call()
    s.tool("write_file", {"path": "never.md", "content": "x"}, "permission denied", status="error")
    s.tool("terminal", {"command": "rm -rf /"}, "blocked", status="blocked")
    s.end_turn("could not")
    store = plugin.get_tracker().store
    assert store.find_file_node(str(workspace / "never.md")) is None
    tcs = [n for n in store.nodes_in_turn(s.turn_id, kinds=["tool_call"])]
    assert {n["meta"]["status"] for n in tcs} == {"error", "blocked"}


def test_hooks_never_raise(plugin, ctx):
    """Garbage payloads must be absorbed; the agent loop never sees an exception."""
    for name in plugin.HOOKS:
        ctx.fire(name)
        ctx.fire(name, session_id=object(), turn_id=123, args="not-a-dict", result=b"bytes", tool_name=None)
        ctx.fire(name, session_id="s", turn_id="t", tool_name="write_file", args={"path": "\0bad", "content": 3}, status="ok", result=None)


def test_prune_and_stats(plugin, ctx, workspace, session_factory):
    s = session_factory()
    research_task(s, workspace, run_terminal=False)
    s.end()
    store = plugin.get_tracker().store
    stats = store.stats()
    assert stats["counts"]["artifacts"] == 1 and stats["counts"]["edges"] > 5
    removed = store.prune(time.time() + 1)
    assert removed["sessions"] == 1 and store.stats()["counts"]["artifacts"] == 0


def test_review_fixes(plugin, ctx, workspace, session_factory):
    """Findings from the independent review: URL secrets, V4A patches, produced-only receipts, safe refetch."""
    hashing = _mod(plugin, "hashing")
    receipt_mod = _mod(plugin, "receipt")
    # credentials never reach a URL node id
    norm = hashing.normalize_url("https://user:p4ss@Example.com/a/?token=abcdef123456&page=2#frag")
    assert "p4ss" not in norm and "user" not in norm and "abcdef123456" not in norm and "page=2" in norm
    assert norm.startswith("https://example.com/a?")
    # refetch refuses non-public hosts even without Hermes's helper
    for bad in ("http://127.0.0.1:8642/api", "http://169.254.169.254/latest/meta-data", "http://localhost/x", "ftp://example.com/x"):
        assert receipt_mod._url_is_safe(bad) is False
    assert receipt_mod._fetch_url_fingerprint("http://169.254.169.254/latest")["status"] == "refused"

    s = session_factory()
    s.start_turn("patch two files, V4A style")
    s.model_call()
    a, b = workspace / "a.py", workspace / "b.py"
    patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-x\n+y\n*** Add File: b.py\n+print(1)\n*** End Patch\n"
    s.tool("patch", {"mode": "patch", "patch": patch}, "ok", during=lambda: (a.write_text("y\n"), b.write_text("print(1)\n")))
    # a blocked call records nothing it would have read
    s.tool("read_file", {"path": "a.py"}, "blocked", status="blocked")
    s.end_turn("done")
    store = plugin.get_tracker().store
    assert store.find_file_node(str(a))["produced"] == 1 and store.find_file_node(str(b))["produced"] == 1
    assert not store.edges_to(f"file:{a}", "read")
    # a file that was only read gets no receipt
    s.start_turn("read only")
    s.model_call()
    src = workspace / "input.txt"
    src.write_text("hello")
    s.tool("read_file", {"path": "input.txt"}, "hello")
    s.end_turn("read it")
    assert receipt_mod.build_receipt(store, str(src)) is None
    assert receipt_mod.write_sidecar(store, str(src)) is None
    assert not (workspace / "input.txt.receipt.json").exists()
    assert "pre_tool_call" not in plugin.HOOKS


def test_find_urls_drops_escapes_and_xml_namespaces(plugin):
    find_urls = _mod(plugin, "hashing").find_urls
    cmd = 'python3 -c "print(\\"<svg xmlns=\\\\\\"http://www.w3.org/2000/svg\\\\\\">\\")" && curl \\"https://example.com/a\\"'
    assert find_urls(cmd) == ["https://example.com/a"]
    assert find_urls('<svg xmlns="http://www.w3.org/2000/svg"><a xlink:href="https://x.org/p"/></svg>') == ["https://x.org/p"]


def test_sources_seen_in_an_earlier_session_still_count(plugin, workspace, session_factory):
    """A page first fetched in one session is evidence in a later session that fetches it again."""
    url = "https://example.com/benchmarks/2026"
    first = session_factory()
    first.start_turn("look something up")
    first.tool("web_extract", {"urls": [url]}, {"results": [{"url": url, "title": "B", "content": "Median growth was 18%."}]})
    first.end_turn("done")

    second = session_factory()
    second.start_turn("write the note")
    second.tool("web_extract", {"urls": [url]}, {"results": [{"url": url, "title": "B", "content": "Median growth was 18%."}]})
    note = workspace / "note.md"
    note.write_text(f"Growth was 18% ({url}).\\n")
    second.tool("write_file", {"path": str(note), "content": note.read_text()}, {"success": True})
    second.end_turn("wrote note.md")

    store = plugin.get_tracker().store
    node = store.find_file_node(str(note))
    edges = [e for e in store.edges_from(node["id"]) if e["dst"].startswith("url:")]
    assert [e["basis"] for e in edges] == ["evidenced"], edges
