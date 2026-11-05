"""Backend for the lineage Desktop page. Mounted by Hermes at ``/api/plugins/lineage/``.

Read-mostly: the Desktop half lists artifacts, pulls a graph neighbourhood and a
receipt, and asks for a verify or a sidecar write on an explicit click. Node ids
contain ``:`` and ``/`` so every route takes them as a query parameter.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()

_CORE_NAME = "hermes_plugin_lineage_core_api"


def _core():
    """Load ../lineage_core as a package of our own name (plugin_api.py is imported standalone)."""
    mod = sys.modules.get(_CORE_NAME)
    if mod is not None:
        return mod
    root = Path(__file__).resolve().parent.parent / "lineage_core"
    spec = importlib.util.spec_from_file_location(_CORE_NAME, root / "__init__.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_NAME] = mod  # our own namespace, so the package's relative imports resolve
    spec.loader.exec_module(mod)
    return mod


def _store():
    _core()
    store_mod = importlib.import_module(f"{_CORE_NAME}.store")
    return store_mod.Store.shared()


def _receipt_mod():
    _core()
    return importlib.import_module(f"{_CORE_NAME}.receipt")


def _node_or_404(store, ref: str) -> dict:
    node = store.resolve(ref)
    if not node:
        raise HTTPException(status_code=404, detail=f"no provenance recorded for {ref!r}")
    return node


@router.get("/health")
async def health():
    core = _core()
    return {"ok": True, "plugin": core.PLUGIN_ID, "version": core.__version__}


@router.get("/stats")
async def stats():
    return _store().stats()


@router.get("/sessions")
async def sessions(limit: int = Query(50, ge=1, le=500)):
    return {"sessions": _store().list_sessions(limit=limit)}


@router.get("/artifacts")
async def artifacts(
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    since: Optional[float] = None,
    limit: int = Query(200, ge=1, le=1000),
):
    store = _store()
    return {"artifacts": store.list_artifacts(session_id=session_id, query=q, since=since, limit=limit)}


@router.get("/node")
async def node(id: str):
    store = _store()
    n = _node_or_404(store, id)
    out = dict(n)
    out["edges_out"] = store.edges_from(n["id"])
    out["edges_in"] = store.edges_to(n["id"])
    if n.get("kind") == "file":
        out["versions"] = store.versions(n["id"])
    if n.get("turn_id"):
        out["turn"] = store.get_turn(n["turn_id"])
    if n.get("session_id"):
        out["session"] = store.get_session(n["session_id"])
    return out


@router.get("/graph")
async def graph(id: str, depth: int = Query(6, ge=1, le=12), direction: str = Query("provenance", pattern="^(up|down|both|provenance)$")):
    store = _store()
    n = _node_or_404(store, id)
    g = store.neighborhood(n["id"], depth=depth, direction=direction)
    # The turn's user prompt and the agent chain travel with the graph so the page needs no second call.
    g["turn"] = store.get_turn(n["turn_id"]) if n.get("turn_id") else None
    g["session"] = store.get_session(n["session_id"]) if n.get("session_id") else None
    return g


@router.get("/receipt")
async def receipt(id: str):
    store = _store()
    n = _node_or_404(store, id)
    r = _receipt_mod().build_receipt(store, n["id"])
    if r is None:
        raise HTTPException(status_code=404, detail="not an artifact")
    return r


@router.get("/explain")
async def explain(id: str):
    store = _store()
    n = _node_or_404(store, id)
    return {"text": _receipt_mod().explain(store, n["id"]) or ""}


@router.get("/mermaid")
async def mermaid(id: str, depth: int = Query(6, ge=1, le=12)):
    store = _store()
    n = _node_or_404(store, id)
    return {"mermaid": _receipt_mod().to_mermaid(store.neighborhood(n["id"], depth=depth))}


class IdBody(BaseModel):
    id: str


class VerifyBody(BaseModel):
    id: str
    refetch: bool = False


@router.post("/receipt/write")
async def receipt_write(body: IdBody):
    store = _store()
    n = _node_or_404(store, body.id)
    path = _receipt_mod().write_sidecar(store, n["id"])
    if path is None:
        raise HTTPException(status_code=400, detail="could not write a receipt for this node")
    return {"ok": True, "path": str(path)}


@router.post("/verify")
async def verify(body: VerifyBody):
    store = _store()
    n = _node_or_404(store, body.id)
    res = _receipt_mod().verify(store, n["id"], refetch=body.refetch)
    if res is None:
        raise HTTPException(status_code=400, detail="not an artifact")
    return res


@router.get("/activity")
async def activity(since: Optional[float] = None, limit: int = Query(50, ge=1, le=500)):
    """Recently touched artifacts, for the status-bar chip and live refresh fallback."""
    store = _store()
    rows = store.list_artifacts(since=since, limit=limit)
    return {"artifacts": rows, "count": len(rows)}


__all__ = ["router"]
