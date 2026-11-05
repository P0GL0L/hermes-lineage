"""The dashboard backend, mounted the way Hermes mounts it (plugin_api.py imported standalone by path)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_tracker import research_task

PLUGIN_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(plugin, ctx, workspace, session_factory, hermes_home, monkeypatch):
    # Make the API's Store.shared() resolve to the same database the tracker wrote.
    s = session_factory()
    research_task(s, workspace)
    name = "hermes_dashboard_plugin_lineage"
    sys.modules.pop(name, None)
    sys.modules.pop("hermes_plugin_lineage_core_api", None)
    for k in [k for k in sys.modules if k.startswith("hermes_plugin_lineage_core_api.")]:
        sys.modules.pop(k)
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/lineage")
    return TestClient(app), s, workspace


def test_routes(client):
    c, s, ws = client
    base = "/api/plugins/lineage"
    assert c.get(f"{base}/health").json()["ok"] is True
    stats = c.get(f"{base}/stats").json()
    assert stats["counts"]["artifacts"] == 3  # the report, the chart, and analysis.py the command created
    arts = c.get(f"{base}/artifacts", params={"session_id": s.sid}).json()["artifacts"]
    names = {a["label"] for a in arts}
    assert {"q3-note.md", "chart.png", "analysis.py"} <= names
    report = next(a for a in arts if a["label"] == "q3-note.md")
    assert report["source_count"] >= 4
    g = c.get(f"{base}/graph", params={"id": report["id"]}).json()
    kinds = {n["kind"] for n in g["nodes"]}
    assert {"file", "url", "tool_call", "model_call", "agent", "turn"} <= kinds
    assert g["turn"]["user_preview"].startswith("Write a short")
    assert any(e["basis"] == "evidenced" for e in g["edges"]) and any(e["basis"] == "observed" for e in g["edges"])
    r = c.get(f"{base}/receipt", params={"id": report["id"]}).json()
    assert r["schema"] == "lineage.receipt.v1" and r["integrity"]["receipt_sha256"]
    assert "written by write_file" in c.get(f"{base}/explain", params={"id": "q3-note.md"}).json()["text"]
    assert c.get(f"{base}/mermaid", params={"id": report["id"]}).json()["mermaid"].startswith("flowchart LR")
    n = c.get(f"{base}/node", params={"id": report["id"]}).json()
    assert n["versions"] and n["edges_out"]
    w = c.post(f"{base}/receipt/write", json={"id": report["id"]}).json()
    assert w["ok"] and w["path"].endswith("q3-note.md.receipt.json")
    v = c.post(f"{base}/verify", json={"id": report["id"], "refetch": False}).json()
    assert v["summary"]["verdict"] == "fresh" and v["sidecar"]["present"]
    assert c.get(f"{base}/activity").json()["count"] == 3
    assert c.get(f"{base}/sessions").json()["sessions"][0]["artifact_count"] == 3
    assert c.get(f"{base}/graph", params={"id": "nope"}).status_code == 404
