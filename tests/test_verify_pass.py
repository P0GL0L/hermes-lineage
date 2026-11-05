"""Second-pass adversarial checks on the 0.1.5 fixes (the reviewer's verification pass).

Each test asserts the correct behaviour; all ten failed on the first 0.1.5 candidate (7e469db).
"""
import hashlib, json, os, time
from importlib import import_module


def _m(plugin, name):
    return import_module(f"{plugin.__name__}.lineage_core.{name}")


def _store(plugin):
    return plugin.get_tracker().store


def _srcs(R, st, p):
    return {(os.path.basename(x["locator"] or ""), x["basis"]) for x in R.build_receipt(st, str(p))["sources"]}


def test_V1_patch_in_a_later_session_keeps_sources_the_file_still_names(plugin, session_factory, workspace):
    R = _m(plugin, "receipt"); st = _store(plugin)
    (workspace / "revenue.csv").write_text("q,r\nQ3,15\n")
    s1 = session_factory(); s1.start_turn("write"); s1.model_call()
    s1.tool("read_file", {"path": "revenue.csv"}, "q,r")
    p = workspace / "note.md"; body = "Revenue 15 (revenue.csv).\nTypo: teh\n"
    s1.tool("write_file", {"path": "note.md", "content": body}, "ok", during=lambda: p.write_text(body))
    s1.end_turn("ok"); s1.end()
    assert ("revenue.csv", "evidenced") in _srcs(R, st, p)
    s2 = session_factory(); s2.start_turn("fix the typo"); s2.model_call()
    s2.tool("patch", {"path": "note.md", "old_string": "teh", "new_string": "the"}, "ok",
            during=lambda: p.write_text(body.replace("teh", "the")))
    assert ("revenue.csv", "evidenced") in _srcs(R, st, p)


def test_V2_chmod_does_not_make_a_user_file_an_artifact(plugin, session_factory, workspace):
    st = _store(plugin)
    f = workspace / "deploy.sh"; f.write_text("echo hi\n")
    time.sleep(0.3)
    s = session_factory(); s.start_turn("t"); s.model_call()
    s.tool("terminal", {"command": "chmod +x deploy.sh"}, '{"exit_code":0}', during=lambda: os.chmod(f, 0o755))
    n = st.get_node(f"file:{f}")
    assert not (n and n["produced"])


def test_V3_identical_common_content_is_not_moved_or_copied(plugin, session_factory, workspace):
    R = _m(plugin, "receipt"); st = _store(plugin)
    s = session_factory(); s.start_turn("scaffold"); s.model_call()
    for d in ("pkg", "pkg/sub"):
        (workspace / d).mkdir(parents=True, exist_ok=True)
        p = workspace / d / "__init__.py"
        s.tool("write_file", {"path": f"{d}/__init__.py", "content": "\n"}, "ok", during=lambda p=p: p.write_text("\n"))
    reasons = [x["reason"] for x in R.build_receipt(st, str(workspace / "pkg/sub/__init__.py"))["sources"]]
    assert not any("moved or copied" in (r or "") for r in reasons), reasons


def test_V4_distinct_urls_keep_distinct_node_ids(plugin):
    H = _m(plugin, "hashing")
    a, b = "https://news.example.com/list?author=alice", "https://news.example.com/list?author=bob"
    c, d = "https://shop.example.com/s?keyword=solar", "https://shop.example.com/s?keyword=wind"
    assert H.normalize_url(a) != H.normalize_url(b), H.normalize_url(a)
    assert H.normalize_url(c) != H.normalize_url(d), H.normalize_url(c)


def test_V5_files_named_in_matching(plugin):
    T = _m(plugin, "tracker").Tracker
    src = {"file:/w/b/config.json": {"kind": "file", "path": "/w/b/config.json"},
           "file:/w/sub/config.json": {"kind": "file", "path": "/w/sub/config.json"}}
    assert T._files_named_in("values from sub/config.json", src, "x") == ["file:/w/sub/config.json"]
    one = {"file:/w/data.csv": {"kind": "file", "path": "/w/data.csv"}}
    assert T._files_named_in("see metadata.csv", one, "x") == []
    short = {"file:/w/src/x.c": {"kind": "file", "path": "/w/src/x.c"}}
    assert T._files_named_in("built from /w/src/x.c", short, "x") == ["file:/w/src/x.c"]


def test_V6_stripped_signature_fails_on_the_issuing_install(plugin, session_factory, workspace):
    R = _m(plugin, "receipt"); st = _store(plugin)
    s = session_factory(); s.start_turn("w"); s.model_call()
    p = workspace / "a.md"
    s.tool("write_file", {"path": "a.md", "content": "genuine"}, "ok", during=lambda: p.write_text("genuine"))
    sc = R.write_sidecar(st, str(p))
    rc = json.loads(sc.read_text())
    rc["sources"] = [{"kind": "url", "locator": "https://forged.example", "basis": "observed"}]
    del rc["integrity"]["signature"]
    body = dict(rc); body.pop("integrity")
    rc["integrity"]["receipt_sha256"] = hashlib.sha256(R.canonical_json(body).encode()).hexdigest()
    sc.write_text(json.dumps(rc))
    res = R.verify_receipt_file(str(sc))
    assert res["ok"] is False, res


def test_V7_prune_does_not_reassign_an_artifact_to_a_reader(plugin, session_factory, workspace):
    R = _m(plugin, "receipt"); st = _store(plugin)
    old = session_factory(); old.start_turn("t"); old.model_call()
    p = workspace / "report.md"
    old.tool("write_file", {"path": "report.md", "content": "x"}, "ok", during=lambda: p.write_text("x")); old.end()
    st._conn.execute("update sessions set ended_at=?, started_at=? where id=?", (time.time() - 200 * 86400,) * 2 + (old.sid,)); st._conn.commit()
    new = session_factory(); new.start_turn("read it"); new.model_call()
    new.tool("read_file", {"path": "report.md"}, "x")
    st.prune(time.time() - 90 * 86400)
    rc = R.build_receipt(st, str(p))
    assert rc is None or rc["agent"]["session_id"] != new.sid, rc["agent"]
    assert all(a["id"] != f"file:{p}" for a in st.list_artifacts(session_id=new.sid))


def test_V8_empty_user_dsn_password(plugin):
    red = _m(plugin, "redact")
    assert "S3cretRedisPw" not in red.redact_text("redis-cli -u redis://:S3cretRedisPw@cache:6379/0 ping")


def test_V9_output_outside_cwd_is_not_an_input(plugin, session_factory, workspace, tmp_path):
    st = _store(plugin)
    out = tmp_path / "elsewhere" / "report.md"; out.parent.mkdir()
    s = session_factory(); s.start_turn("t"); s.model_call()
    tc = s.tool("terminal", {"command": f"python gen.py > {out}"}, '{"exit_code":0}', during=lambda: out.write_text("hi"))
    assert not [e for e in st.edges_from(f"tc:{tc}", "read") if e["dst"] == f"file:{out}"]


def test_V10_a_blocked_command_writes_nothing(plugin, session_factory, workspace):
    st = _store(plugin)
    s = session_factory(); s.start_turn("t"); s.model_call()
    f = workspace / "server.log"
    tc = s.tool("terminal", {"command": "rm -rf build"}, "BLOCKED: denied by user", status="blocked",
                during=lambda: f.write_text("appended by a dev server"))
    assert not st.edges_from(f"tc:{tc}", "wrote")
