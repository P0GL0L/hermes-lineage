"""Regression tests for the second independent review (2026-11-05).

Each test is the reviewer's reproduction turned around: it asserts the correct behaviour, and it
fails on 0.1.5-pre code. Numbers match the review report.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from importlib import import_module


def _m(plugin, name):
    return import_module(f"{plugin.__name__}.lineage_core.{name}")


def _store(plugin):
    return plugin.get_tracker().store


def test_1_a_read_does_not_launder_an_edited_artifact(plugin, session_factory, workspace):
    s = session_factory()
    R = _m(plugin, "receipt")
    s.start_turn("write")
    s.model_call()
    p = workspace / "report.md"
    s.tool("write_file", {"path": "report.md", "content": "v1"}, "ok", during=lambda: p.write_text("v1"))
    s.end_turn("done")
    written = hashlib.sha256(b"v1").hexdigest()
    p.write_text("TAMPERED by someone else")
    s.start_turn("look at it")
    s.model_call()
    s.tool("read_file", {"path": "report.md"}, "TAMPERED")
    assert R.verify(_store(plugin), str(p))["summary"]["verdict"] == "modified"
    rc = R.build_receipt(_store(plugin), str(p))
    assert rc["artifact"]["sha256"] == written  # the receipt is for the bytes write_file wrote
    node = _store(plugin).find_file_node(str(p))
    assert node["meta"]["last_read_sha256"] == hashlib.sha256(b"TAMPERED by someone else").hexdigest()


def test_2_a_rewrite_drops_sources_the_new_version_does_not_use(plugin, session_factory, workspace):
    s = session_factory()
    R = _m(plugin, "receipt")
    st = _store(plugin)
    csv = workspace / "revenue.csv"
    csv.write_text("a,b\n1,2\n")
    s.start_turn("t1")
    s.model_call()
    s.tool("read_file", {"path": "revenue.csv"}, "a,b")
    p = workspace / "note.md"
    s.tool("write_file", {"path": "note.md", "content": "from revenue.csv"}, "ok", during=lambda: p.write_text("from revenue.csv"))
    s.end_turn("ok")
    for i in range(4):
        s.start_turn(f"pad{i}")
        s.model_call()
        s.end_turn("x")
    s.start_turn("rewrite from scratch")
    s.model_call()
    s.tool("write_file", {"path": "note.md", "content": "hello world"}, "ok", during=lambda: p.write_text("hello world"))
    s.end_turn("ok")
    srcs = [x["locator"] for x in R.build_receipt(st, str(p))["sources"]]
    assert not any(l.endswith("revenue.csv") for l in srcs), srcs
    csv.write_text("changed")
    assert R.verify(st, str(p))["summary"]["verdict"] == "fresh"
    assert len(R.build_receipt(st, str(p))["artifact"]["versions"]) == 2  # history is kept


def test_3_check_uses_the_file_beside_the_receipt_and_rejects_bad_signatures(plugin, session_factory, workspace, tmp_path):
    s = session_factory()
    R = _m(plugin, "receipt")
    st = _store(plugin)
    s.start_turn("w")
    s.model_call()
    p = workspace / "a.md"
    s.tool("write_file", {"path": "a.md", "content": "genuine"}, "ok", during=lambda: p.write_text("genuine"))
    s.end_turn("ok")
    sc = R.write_sidecar(st, str(p))
    other = tmp_path / "shared"
    other.mkdir()
    shutil.copy(sc, other / "a.md.receipt.json")
    (other / "a.md").write_text("FORGED CONTENT")
    res = R.verify_receipt_file(str(other / "a.md.receipt.json"))
    assert res["ok"] is False and res["artifact"] == str(other / "a.md") and res["issued_for"] == str(p)
    shutil.copy(p, other / "a.md")  # an honest copy travels fine
    assert R.verify_receipt_file(str(other / "a.md.receipt.json"))["ok"] is True
    # forged sources with a recomputed (unkeyed) body hash: signature INVALID -> not ok
    rc = json.loads(sc.read_text())
    rc["sources"] = [{"kind": "url", "locator": "https://forged.example", "basis": "observed"}]
    body = dict(rc)
    body.pop("integrity")
    rc["integrity"]["receipt_sha256"] = hashlib.sha256(R.canonical_json(body).encode()).hexdigest()
    sc.write_text(json.dumps(rc))
    res = R.verify_receipt_file(str(sc))
    assert res["signature"] == "INVALID" and res["ok"] is False


def test_4_secrets_in_paths_queries_dsns_and_arg_names_are_redacted(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("t")
    s.model_call()
    tok = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
    s.tool("terminal", {"command": f"curl -s https://api.telegram.org/bot{tok}/getUpdates && psql postgres://admin:Hunter2Secret@db.internal/prod -c 'select 1'"}, '{"exit_code":0,"output":""}')
    s.tool("web_extract", {"urls": ["https://bucket.s3.amazonaws.com/f.csv?X-Amz-Credential=AKIDEXAMPLE&X-Amz-Signature=deadbeefcafebabe1234&X-Amz-Security-Token=FQoGZXIvYXdzEJr"]}, "{}")
    s.tool("mcp_github_create_issue", {"accessToken": "q9w8e7r6t5y4u3i2o1p0", "client_secret": "zzzzzzzzzzzzzzzz1"}, "{}")
    dump = "\n".join(str(dict(r)) for r in st._conn.execute("select * from nodes")) + \
        "\n".join(str(dict(r)) for r in st._conn.execute("select * from edges"))
    for secret in (tok, "Hunter2Secret", "deadbeefcafebabe1234", "FQoGZXIvYXdzEJr", "q9w8e7r6t5y4u3i2o1p0", "zzzzzzzzzzzzzzzz1"):
        assert secret not in dump, secret


def test_5_credential_files_named_in_a_command_are_not_opened(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    ssh = workspace / ".ssh"
    ssh.mkdir()
    key = ssh / "id_rsa"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    env = workspace / ".env"
    env.write_text("TOKEN=x")
    s.start_turn("t")
    s.model_call()
    s.tool("terminal", {"command": f"chmod 600 {key} .env"}, '{"exit_code":0}')
    assert st.get_node(f"file:{key}") is None and st.get_node(f"file:{env}") is None


def test_5b_working_tree_writes_are_labelled_inferred(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("t")
    s.model_call()
    out = workspace / "out.txt"
    tc = s.tool("terminal", {"command": "python gen.py"}, '{"exit_code":0}', during=lambda: out.write_text("hi"))
    wrote = [e for e in st.edges_to(f"file:{out}", "wrote") if e["src"] == f"tc:{tc}"]
    assert wrote and wrote[0]["basis"] == "inferred" and "working tree" in wrote[0]["meta"]["reason"]


def test_6_hermes_session_start_does_not_overwrite_a_subagent_role(plugin, ctx, session_factory, workspace):
    R = _m(plugin, "receipt")
    st = _store(plugin)
    parent = session_factory()
    parent.start_turn("delegate")
    ctx.fire("subagent_start", parent_session_id=parent.sid, parent_turn_id=parent.turn_id, parent_subagent_id=None,
             child_session_id="child-9", child_subagent_id="sa1", child_role="researcher", child_goal="research")
    ctx.fire("on_session_start", session_id="child-9", model="m", platform="cli")  # as Hermes sends it
    child = type(parent)(ctx, workspace, session_id="child-9", parent_session_id=parent.sid)
    child.start_turn("research")
    child.model_call()
    p = workspace / "c.md"
    child.tool("write_file", {"path": "c.md", "content": "x"}, "ok", during=lambda: p.write_text("x"))
    chain = R.build_receipt(st, str(p))["agent"]["chain"]
    assert chain[0]["role"] == "researcher" and chain[1]["session_id"] == parent.sid


def test_7_a_command_output_is_not_also_an_input(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("t")
    s.model_call()
    out = workspace / "summary.txt"
    tc = s.tool("terminal", {"command": "python make.py > summary.txt"}, '{"exit_code":0}', during=lambda: out.write_text("hi"))
    rels = {e["rel"] for e in st.edges_from(f"tc:{tc}") if e["dst"] == f"file:{out}"}
    assert rels == {"wrote"}


def test_7b_a_rename_keeps_lineage(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    R = _m(plugin, "receipt")
    s.start_turn("t")
    s.model_call()
    a = workspace / "draft.md"
    body = "a draft long enough to be told apart"
    s.tool("write_file", {"path": "draft.md", "content": body}, "ok", during=lambda: a.write_text(body))
    time.sleep(0.3)
    s.tool("terminal", {"command": "mv draft.md final.md"}, '{"exit_code":0}', during=lambda: a.rename(workspace / "final.md"))
    final = workspace / "final.md"
    rc = R.build_receipt(st, str(final))
    assert rc is not None and rc["artifact"]["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert any(x["locator"] == str(a) and "same bytes as draft.md (moved)" == x["reason"] and x["basis"] == "inferred"
               for x in rc["sources"]), rc["sources"]


def test_8_v4a_move_and_delete_and_evidence_from_the_final_file(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("t")
    s.model_call()
    s.tool("web_extract", {"urls": ["https://old.example.com/a"]}, "{}")
    (workspace / "b.md").write_text("see https://old.example.com/a\n")
    (workspace / "gone.md").write_text("bye\n")
    patch = ("*** Begin Patch\n*** Update File: b.md\n@@\n-see https://old.example.com/a\n+no citation now\n"
             "*** Move File: b.md -> docs/b2.md\n*** Delete File: gone.md\n*** End Patch\n")

    def apply():
        (workspace / "docs").mkdir()
        (workspace / "b.md").write_text("no citation now\n")
        (workspace / "b.md").rename(workspace / "docs" / "b2.md")
        (workspace / "gone.md").unlink()

    s.tool("patch", {"mode": "patch", "patch": patch}, "ok", during=apply)
    assert st.get_node(f"file:{workspace / 'b.md'}") is None
    assert st.get_node(f"file:{workspace / 'gone.md'}") is None
    b2 = st.get_node(f"file:{workspace / 'docs' / 'b2.md'}")
    assert b2 and b2["produced"] == 1 and b2["sha256"]
    ev = [(e["dst"], e["basis"]) for e in st.edges_from(b2["id"], "derived_from")]
    assert ("url:https://old.example.com/a", "evidenced") not in ev  # a removed line is not evidence


def test_9_prune_keeps_nodes_a_recent_session_still_uses(plugin, session_factory, workspace):
    R = _m(plugin, "receipt")
    st = _store(plugin)
    old = session_factory()
    old.start_turn("t")
    old.model_call()
    old.tool("web_extract", {"urls": ["https://example.com/x"]}, "{}")
    old.end()
    st._conn.execute("update sessions set ended_at=?, started_at=? where id=?", (time.time() - 200 * 86400,) * 2 + (old.sid,))
    st._conn.commit()
    new = session_factory()
    new.start_turn("t")
    new.model_call()
    new.tool("web_extract", {"urls": ["https://example.com/x"]}, "{}")
    p = workspace / "n.md"
    new.tool("write_file", {"path": "n.md", "content": "per https://example.com/x"}, "ok", during=lambda: p.write_text("per https://example.com/x"))
    out = st.prune(time.time() - 90 * 86400)
    assert out["sessions"] == 1 and out["kept_shared"] >= 1
    assert [x["locator"] for x in R.build_receipt(st, str(p))["sources"]] == ["https://example.com/x"]


def test_10_hook_cost_is_bounded_for_big_turns(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("big")
    s.model_call()
    for i in range(400):
        f = workspace / f"in{i}.txt"
        f.write_text(str(i))
        s.tool("read_file", {"path": f.name}, str(i))
    before = st.count_edges()

    def write_many():
        for i in range(50):
            (workspace / f"out{i}.txt").write_text(f"out {i}")

    t0 = time.time()
    s.tool("terminal", {"command": "python fan_out.py"}, '{"exit_code":0}', during=write_many)
    elapsed = time.time() - t0
    added = st.count_edges() - before
    assert added < 50 * 60, added  # was ~21,700
    assert elapsed < 3.0, elapsed  # was ~8.7 s inside the hook


def test_12_files_written_by_a_failing_command_are_recorded(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("t")
    s.model_call()
    report = workspace / "r.xml"
    tc = s.tool("terminal", {"command": "pytest --junitxml=r.xml"}, '{"exit_code":1,"output":"1 failed"}',
                during=lambda: report.write_text("<testsuite failures='1'/>"), status="error")
    wrote = [e for e in st.edges_to(f"file:{report}", "wrote") if e["src"] == f"tc:{tc}"]
    assert wrote and wrote[0]["meta"]["call_status"] == "error"
    # a failed write_file still records nothing
    s.tool("write_file", {"path": "x.md", "content": "x"}, '{"error":"denied"}', status="error")
    assert st.get_node(f"file:{workspace / 'x.md'}") is None


def test_13_a_deleted_source_makes_the_artifact_stale(plugin, session_factory, workspace):
    s = session_factory()
    R = _m(plugin, "receipt")
    st = _store(plugin)
    src = workspace / "data.csv"
    src.write_text("1,2")
    s.start_turn("t")
    s.model_call()
    s.tool("read_file", {"path": "data.csv"}, "1,2")
    p = workspace / "r.md"
    s.tool("write_file", {"path": "r.md", "content": "based on data.csv"}, "ok", during=lambda: p.write_text("based on data.csv"))
    src.unlink()
    v = R.verify(st, str(p))
    assert v["summary"]["verdict"] == "stale" and [x["status"] for x in v["sources"]] == ["missing"]


def test_14_same_name_in_two_folders_is_told_apart(plugin, session_factory, workspace):
    s = session_factory()
    R = _m(plugin, "receipt")
    st = _store(plugin)
    for d in ("a", "b"):
        (workspace / d).mkdir()
        (workspace / d / "config.json").write_text(d)
    s.start_turn("t")
    s.model_call()
    s.tool("read_file", {"path": "a/config.json"}, "a")
    s.tool("read_file", {"path": "b/config.json"}, "b")
    s.end_turn("x")
    for _ in range(4):
        s.start_turn("p")
        s.model_call()
        s.end_turn("x")
    s.start_turn("t2")
    s.model_call()
    p = workspace / "x.md"
    s.tool("write_file", {"path": "x.md", "content": "values from b/config.json"}, "ok", during=lambda: p.write_text("values from b/config.json"))
    srcs = {(x["locator"], x["basis"]) for x in R.build_receipt(st, str(p))["sources"]}
    assert (str(workspace / "b" / "config.json"), "evidenced") in srcs
    assert not any(l == str(workspace / "a" / "config.json") for l, _ in srcs)


def test_15_a_read_does_not_move_the_written_time(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("t")
    s.model_call()
    p = workspace / "a.md"
    s.tool("write_file", {"path": "a.md", "content": "x"}, "ok", during=lambda: p.write_text("x"))
    written_at = st.find_file_node(str(p))["updated_at"]
    time.sleep(0.05)
    s.tool("read_file", {"path": "a.md"}, "x")
    assert st.find_file_node(str(p))["updated_at"] == written_at


def test_16_search_results_only_yield_http_urls(plugin, session_factory, workspace):
    s = session_factory()
    st = _store(plugin)
    s.start_turn("t")
    s.model_call()
    s.tool("web_search", {"query": "q"}, {"success": True, "data": {"web": [
        {"url": "file:///Applications/Calculator.app"}, {"url": "javascript:alert(1)"}, {"url": "https://ok.example.com/"}]}})
    urls = [r["url"] for r in st._conn.execute("select url from nodes where kind='url'")]
    assert urls == ["https://ok.example.com/"]


def test_refetch_refuses_shared_address_space(plugin, monkeypatch):
    R = _m(plugin, "receipt")
    import socket

    monkeypatch.setitem(__import__("sys").modules, "tools.url_safety", None)  # force the local fallback
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("100.100.1.2", 80))])
    assert R._url_is_safe("http://my-tailnet-box/") is False


def test_sidecar_is_not_written_through_a_symlink(plugin, session_factory, workspace, tmp_path):
    R = _m(plugin, "receipt")
    st = _store(plugin)
    s = session_factory()
    s.start_turn("t")
    s.model_call()
    p = workspace / "a.md"
    s.tool("write_file", {"path": "a.md", "content": "x"}, "ok", during=lambda: p.write_text("x"))
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (workspace / "a.md.receipt.json").symlink_to(victim)
    try:
        R.write_sidecar(st, str(p))
    except OSError:
        pass
    assert victim.read_text() == "keep me"


def test_resolve_handles_dotfiles_and_like_wildcards(plugin, session_factory, workspace):
    st = _store(plugin)
    s = session_factory()
    s.start_turn("t")
    s.model_call()
    for name in (".env.example", "a_b.md", "axb.md"):
        f = workspace / name
        s.tool("write_file", {"path": name, "content": name}, "ok", during=lambda f=f, name=name: f.write_text(name))
    assert st.resolve(".env.example")["label"] == ".env.example"
    assert st.resolve("./a_b.md")["label"] == "a_b.md"
    assert st.resolve("a_b.md")["label"] == "a_b.md"
