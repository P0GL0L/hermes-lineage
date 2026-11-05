"""Round-three checks. Each asserts the correct behaviour and FAILS on f74d86c."""
import hashlib, json, os, time
from importlib import import_module


def _m(plugin, name):
    return import_module(f"{plugin.__name__}.lineage_core.{name}")


def _store(plugin):
    return plugin.get_tracker().store


def test_R1_fake_key_id_is_not_accepted(plugin, session_factory, workspace):
    R = _m(plugin, "receipt"); st = _store(plugin)
    s = session_factory(); s.start_turn("w"); s.model_call()
    p = workspace / "a.md"
    s.tool("write_file", {"path": "a.md", "content": "genuine"}, "ok", during=lambda: p.write_text("genuine"))
    sc = R.write_sidecar(st, str(p))
    rc = json.loads(sc.read_text())
    rc["sources"] = [{"kind": "url", "locator": "https://forged.example", "basis": "observed"}]
    rc["integrity"]["signature"] = {"alg": "HMAC-SHA256", "key_id": "000000000000", "value": "00"}
    body = dict(rc); body.pop("integrity")
    rc["integrity"]["receipt_sha256"] = hashlib.sha256(R.canonical_json(body).encode()).hexdigest()
    sc.write_text(json.dumps(rc))
    res = R.verify_receipt_file(str(sc))
    assert res["ok"] is False, (res["ok"], res["signature"])


def test_R2_prune_keeps_artifact_but_guts_its_receipt(plugin, session_factory, workspace):
    R = _m(plugin, "receipt"); st = _store(plugin)
    (workspace / "revenue.csv").write_text("q,r\nQ3,15\n")
    time.sleep(1.2)
    old = session_factory(); old.start_turn("t"); old.model_call()
    old.tool("read_file", {"path": "revenue.csv"}, "q,r")
    p = workspace / "report.md"; body = "Q3 was 15 per revenue.csv\n"
    old.tool("write_file", {"path": "report.md", "content": body}, "ok", during=lambda: p.write_text(body)); old.end()
    time.sleep(1.2)
    before = R.build_receipt(st, str(p))
    assert before["produced_by"] and before["sources"]
    st._conn.execute("update sessions set ended_at=?, started_at=? where id=?", (time.time() - 200 * 86400,) * 2 + (old.sid,)); st._conn.commit()
    new = session_factory(); new.start_turn("read"); new.model_call()
    new.tool("read_file", {"path": "report.md"}, body)
    st.prune(time.time() - 90 * 86400)
    after = R.build_receipt(st, str(p))
    assert after["produced_by"] is not None, "kept artifact lost its producing tool call"
    assert after["sources"], "kept artifact lost its sources"


def test_R3_secret_param_names(plugin):
    H = _m(plugin, "hashing"); red = _m(plugin, "redact")
    for name in ("authtoken", "accesstoken", "secretkey", "apiKey2"):  # all masked by round one
        assert red.is_secret_param_name(name), name
    a = H.normalize_url("https://api.example.com/rates?country_code=us")
    b = H.normalize_url("https://api.example.com/rates?country_code=gb")
    assert a != b, a  # two different pages, one node id


def test_R4_unzip_overwrite_flag_is_not_an_output(plugin, session_factory, workspace):
    st = _store(plugin)
    z = workspace / "data.zip"; z.write_bytes(b"PK\x03\x04 not really")
    time.sleep(0.2)
    s = session_factory(); s.start_turn("t"); s.model_call()
    tc = s.tool("terminal", {"command": "unzip -o data.zip"}, '{"exit_code":0,"output":""}')
    assert any(e["dst"] == f"file:{z}" for e in st.edges_from(f"tc:{tc}", "read"))  # passed on 7e469db


def test_R5_timed_out_code_still_wrote_its_files(plugin, session_factory, workspace):
    st = _store(plugin)
    s = session_factory(); s.start_turn("t"); s.model_call()
    out = workspace / "partial.csv"
    res = {"status": "timeout", "output": "...", "exit_code": -1, "error": "Cell timed out after 300s"}
    s.tool("execute_code", {"code": "slow()"}, res, status="error", during=lambda: out.write_text("rows"))
    n = st.get_node(f"file:{out}")
    assert n and n["produced"]  # docs: "Recorded even when the command exits non-zero"

