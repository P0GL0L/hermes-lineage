"""Receipts, verification and exports.

A receipt is the part of lineage that leaves Hermes: a JSON sidecar
(``report.pdf.receipt.json``) that says what the file is (hash), who made it
(agent, model, tool call), what it was made from (sources with their own hashes
and an explicit basis) and when. Anyone with the file and the receipt can check
the hash without Hermes installed; the signature lets this install recognise its
own receipts later.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from . import PLUGIN_ID, RECEIPT_SCHEMA, __version__
from .hashing import sha256_bytes, sha256_file, sha256_text
from .store import Store, default_data_dir

BASIS_LEGEND = {
    "observed": "Hermes reported this relationship directly (the tool wrote the file, the model issued the call).",
    "evidenced": "The artifact's own content names the source (a fetched URL or a read file path appears in it).",
    "inferred": "The source was read earlier in the same turn (0.6) or in a recent turn of the session (0.35).",
}

SIDECAR_SUFFIX = ".receipt.json"


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


# --------------------------------------------------------------------- signing
def _key_path(data_dir: Optional[Path] = None) -> Path:
    return (data_dir or default_data_dir()) / "install.key"


def install_key(data_dir: Optional[Path] = None, create: bool = True) -> Optional[bytes]:
    """Per-install random key for receipt signatures. Created on first use, mode 0600."""
    path = _key_path(data_dir)
    try:
        if path.is_file():
            return bytes.fromhex(path.read_text().strip())
        if not create:
            return None
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(key.hex())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return key
    except (OSError, ValueError):
        return None


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:12]


def sign(payload: dict, data_dir: Optional[Path] = None) -> Optional[dict]:
    key = install_key(data_dir)
    if key is None:
        return None
    body = canonical_json(payload).encode("utf-8")
    return {"alg": "HMAC-SHA256", "key_id": key_id(key), "value": hmac.new(key, body, hashlib.sha256).hexdigest(),
            "note": "Proves this receipt was issued by the Hermes install whose lineage key has this id; it is not a third-party attestation."}


def check_signature(receipt: dict, data_dir: Optional[Path] = None) -> Optional[bool]:
    """True/False when this install can check the signature, None when it cannot (other install, no key)."""
    integrity = receipt.get("integrity") or {}
    sig = integrity.get("signature") or {}
    key = install_key(data_dir, create=False)
    if not sig or key is None or sig.get("key_id") != key_id(key):
        return None
    body = dict(receipt)
    body.pop("integrity", None)
    expected = hmac.new(key, canonical_json(body).encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(sig.get("value") or ""))


# -------------------------------------------------------------------- building
def _linked_state(edge: dict, node: dict) -> tuple[Optional[str], Optional[int]]:
    """The source's hash and size when the artifact was linked to it (falls back to the node for old edges)."""
    m = edge.get("meta") or {}
    if m.get("src_sha256"):
        return m["src_sha256"], m.get("src_size")
    return node.get("sha256"), node.get("size")


def _rewritten_after(store: Store, edge: dict, node: dict) -> bool:
    """True when a write to the source is recorded after the artifact was linked to it."""
    t = Store.edge_time(edge)
    return any((v.get("created_at") or 0) > t + 1.0 for v in store.versions(node["id"]))


def _source_entry(store: Store, edge: dict, node: dict) -> dict:
    kind = node.get("kind")
    locator = node.get("url") or node.get("path") or (node.get("meta") or {}).get("query") or node.get("label")
    sha, size = _linked_state(edge, node)
    entry = {
        "id": node["id"],
        "kind": kind,
        "locator": locator,
        "sha256": sha,
        "size": size,
        "basis": edge.get("basis"),
        "confidence": edge.get("confidence"),
        "reason": (edge.get("meta") or {}).get("reason"),
        "first_seen": _iso(node.get("created_at")),
    }
    if kind == "url":
        entry["sha256_of"] = "extracted page content at fetch time" if sha else None
        entry["fetched_at"] = _iso((node.get("meta") or {}).get("fetched_at"))
    if kind == "file":
        entry["sha256_of"] = "file bytes when this artifact was built" if sha else None
        entry["produced_by_agent"] = bool(node.get("produced"))
    # Which tool call read it (the first one in this session).
    readers = [e for e in store.edges_to(node["id"], "read")]
    if readers:
        tc = store.get_node(readers[0]["src"])
        if tc:
            entry["read_by"] = {"tool_call_id": tc.get("tool_call_id"), "tool": (tc.get("meta") or {}).get("tool"),
                                "at": _iso((tc.get("meta") or {}).get("started_at"))}
    return entry


def _agent_chain(store: Store, session_id: Optional[str]) -> list[dict]:
    chain: list[dict] = []
    seen: set[str] = set()
    sid = session_id
    while sid and sid not in seen and len(chain) < 8:
        seen.add(sid)
        sess = store.get_session(sid) or {}
        agent = store.get_node(f"agent:{sid}") or {}
        chain.append({
            "session_id": sid,
            "role": sess.get("role") or (agent.get("meta") or {}).get("role") or "main",
            "platform": sess.get("platform"),
            "model": sess.get("model") or (agent.get("meta") or {}).get("model"),
            "goal": (sess.get("meta") or {}).get("goal"),
        })
        sid = sess.get("parent_session_id")
    return chain


def build_receipt(store: Store, ref: str, *, include_signature: bool = True, data_dir: Optional[Path] = None) -> Optional[dict]:
    """Assemble the receipt for an artifact (node id, absolute path, or basename)."""
    node = store.resolve(ref)
    if not node or node.get("kind") != "file" or not node.get("produced"):
        return None  # receipts are for files the agent made, never for files it merely read
    nid = node["id"]
    path = node.get("path") or ""

    # Producer: the observed 'wrote' edge, newest first.
    writers = sorted(store.edges_to(nid, "wrote"), key=lambda e: e.get("created_at") or 0, reverse=True)
    producer = None
    tc_nodes: dict[str, dict] = {}
    for w in writers:
        tc = store.get_node(w["src"])
        if tc:
            tc_nodes[tc["id"]] = tc
            if producer is None:
                m = tc.get("meta") or {}
                producer = {"tool": m.get("tool"), "tool_call_id": tc.get("tool_call_id"), "basis": w.get("basis"),
                            "at": _iso(m.get("started_at")), "duration_ms": m.get("duration_ms"), "args": m.get("args"),
                            "status": m.get("status")}

    # Models that issued the writing tool calls.
    models: list[dict] = []
    seen_models: set[str] = set()
    for tc_id in tc_nodes:
        for e in store.edges_to(tc_id, "invoked"):
            mc = store.get_node(e["src"])
            if mc and mc["id"] not in seen_models:
                seen_models.add(mc["id"])
                m = mc.get("meta") or {}
                models.append({"api_request_id": mc["id"].split(":", 1)[-1], "model": m.get("response_model") or m.get("model"),
                               "provider": m.get("provider"), "usage": m.get("usage"), "at": _iso(m.get("started_at"))})

    # Sources, by basis strength.
    sources: list[dict] = []
    parents: list[dict] = []
    for e in store.edges_from(nid, "derived_from"):
        src = store.get_node(e["dst"])
        if not src:
            continue
        entry = _source_entry(store, e, src)
        sources.append(entry)
        if src.get("kind") == "file" and src.get("produced"):
            parents.append({"id": src["id"], "path": src.get("path"), "sha256": entry.get("sha256"), "basis": e.get("basis")})
    rank = {"observed": 0, "evidenced": 1, "inferred": 2}
    sources.sort(key=lambda s: (rank.get(s.get("basis") or "", 9), -(s.get("confidence") or 0), s.get("locator") or ""))

    # Transformations: every tool call in the producing turn, in order.
    turn_id = node.get("turn_id")
    transformations: list[dict] = []
    if turn_id:
        for tc in store.nodes_in_turn(turn_id, kinds=["tool_call"]):
            m = tc.get("meta") or {}
            transformations.append({"tool_call_id": tc.get("tool_call_id"), "tool": m.get("tool"), "label": tc.get("label"),
                                    "status": m.get("status"), "at": _iso(m.get("started_at")), "duration_ms": m.get("duration_ms"),
                                    "wrote_this": tc["id"] in tc_nodes})
    turn = store.get_turn(turn_id) if turn_id else None
    cited = any(e["src"] == f"turn:{turn_id}" for e in store.edges_to(nid, "cited_in_reply")) if turn_id else False

    versions = store.versions(nid)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "generated_at": _iso(_now()),
        "generator": {"plugin": PLUGIN_ID, "version": __version__},
        "artifact": {
            "path": path,
            "name": os.path.basename(path),
            "sha256": node.get("sha256"),
            "size": node.get("size"),
            "created_at": _iso(node.get("created_at")),
            "last_written_at": _iso(versions[-1].get("created_at")) if versions else _iso(node.get("created_at")),
            "versions": [{"sha256": v.get("sha256"), "size": v.get("size"), "at": _iso(v.get("created_at")),
                          "tool_call_id": v.get("tool_call_id")} for v in versions],
            "cited_in_final_reply": cited,
        },
        "produced_by": producer,
        "agent": {"session_id": node.get("session_id"), "chain": _agent_chain(store, node.get("session_id"))},
        "turn": {
            "turn_id": turn_id,
            "user_prompt_preview": (turn or {}).get("user_preview"),
            "user_prompt_sha256": (turn or {}).get("user_sha"),
            "started_at": _iso((turn or {}).get("started_at")),
            "ended_at": _iso((turn or {}).get("ended_at")),
        },
        "models": models,
        "sources": sources,
        "parents": parents,
        "transformations": transformations,
        "basis_legend": BASIS_LEGEND,
        "counts": {
            "sources": len(sources),
            "observed": sum(1 for s in sources if s.get("basis") == "observed"),
            "evidenced": sum(1 for s in sources if s.get("basis") == "evidenced"),
            "inferred": sum(1 for s in sources if s.get("basis") == "inferred"),
        },
    }
    body = canonical_json(receipt).encode("utf-8")
    integrity: dict[str, Any] = {"receipt_sha256": sha256_bytes(body)}
    if include_signature:
        sig = sign(receipt, data_dir)
        if sig:
            integrity["signature"] = sig
    receipt["integrity"] = integrity
    return receipt


def _now() -> float:
    import time

    return time.time()


def sidecar_path(artifact_path: str) -> Path:
    return Path(artifact_path + SIDECAR_SUFFIX)


def write_sidecar(store: Store, ref: str, *, data_dir: Optional[Path] = None) -> Optional[Path]:
    receipt = build_receipt(store, ref, data_dir=data_dir)
    if not receipt or not receipt["artifact"].get("path"):
        return None
    path = sidecar_path(receipt["artifact"]["path"])
    if path.is_symlink():
        raise OSError(f"refusing to write a receipt through a symlink: {path}")
    data = (json.dumps(receipt, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    # O_EXCL + O_NOFOLLOW: never write through a link planted in a cloned repo
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_sidecar(artifact_path: str) -> Optional[dict]:
    p = sidecar_path(artifact_path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- verification
def _url_is_safe(url: str) -> bool:
    """Public hosts only. Uses Hermes's own SSRF check when available; otherwise refuses loopback,
    private, link-local (cloud metadata) and unresolvable hosts itself."""
    try:
        from tools.url_safety import is_safe_url  # documented public helper

        return bool(is_safe_url(url))
    except Exception:
        pass
    import ipaddress
    import socket

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        # is_global also excludes shared address space (100.64.0.0/10: CGNAT, Tailscale) and
        # documentation ranges, which is_private alone lets through
        if not ip.is_global or ip.is_multicast:
            return False
    return True


MAX_REDIRECTS = 5


def _fetch_url_fingerprint(url: str, timeout: float = 8.0) -> dict:
    """Reachability and a content fingerprint for *url* (bounded to 1 MiB). Network only on request,
    and only to public hosts — every redirect hop is checked again, so a public page cannot bounce the
    request to loopback, a private network or a cloud metadata address."""
    import urllib.error
    import urllib.request

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return {"status": "skipped", "reason": "not http(s)"}
    if not _url_is_safe(url):
        return {"status": "refused", "reason": "not a public host"}

    class _RefusedRedirect(Exception):
        def __init__(self, target: str, reason: str):
            super().__init__(reason)
            self.target, self.reason = target, reason

    class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
        max_redirections = MAX_REDIRECTS

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urlsplit(newurl).scheme not in ("http", "https"):
                raise _RefusedRedirect(newurl, "redirect to a non-http(s) URL")
            if not _url_is_safe(newurl):
                raise _RefusedRedirect(newurl, "redirect to a non-public host")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(_CheckedRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": f"hermes-lineage/{__version__} (+verify)"})
    try:
        with opener.open(req, timeout=timeout) as resp:  # noqa: S310 - scheme and every hop checked
            data = resp.read(1024 * 1024)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            out = {"status": "reachable", "http_status": getattr(resp, "status", None), "sha256": sha256_bytes(data),
                   "bytes": len(data), "etag": headers.get("etag"), "last_modified": headers.get("last-modified")}
            final = resp.geturl()
            if final and final != url:
                out["final_url"] = final
            return out
    except _RefusedRedirect as exc:
        return {"status": "refused", "reason": exc.reason, "redirect_to": exc.target}
    except urllib.error.HTTPError as exc:
        return {"status": "unreachable", "http_status": exc.code}
    except Exception as exc:  # network errors of every kind
        return {"status": "unreachable", "error": exc.__class__.__name__}


def verify(store: Store, ref: str, *, refetch: bool = False, data_dir: Optional[Path] = None) -> Optional[dict]:
    """Check an artifact against its recorded hashes (and sidecar, if present).

    * artifact: ``unchanged`` / ``modified`` / ``missing``
    * file sources: ``unchanged`` / ``changed`` / ``missing``
    * url sources: ``unchecked`` unless ``refetch``; then ``reachable``/``unreachable`` and, once a
      baseline fingerprint exists from an earlier verify, ``unchanged since``/``changed since``.
      (The hash recorded at fetch time is of the *extracted* text, which a raw fetch cannot reproduce,
      so URL change detection starts from the first verify, and says so.)
    """
    node = store.resolve(ref)
    if not node or node.get("kind") != "file" or not node.get("produced"):
        return None
    path = node.get("path") or ""
    now_sha, now_size = sha256_file(path)
    if now_sha is None and now_size is None:
        artifact_status = "missing"
    elif now_sha == node.get("sha256"):
        artifact_status = "unchanged"
    else:
        artifact_status = "modified"
    result: dict[str, Any] = {
        "artifact": {"path": path, "status": artifact_status, "recorded_sha256": node.get("sha256"), "current_sha256": now_sha,
                     "recorded_size": node.get("size"), "current_size": now_size},
        "sources": [],
        "sidecar": None,
        "checked_at": _iso(_now()),
    }
    sidecar = read_sidecar(path)
    if sidecar:
        sig = check_signature(sidecar, data_dir)
        result["sidecar"] = {"present": True, "path": str(sidecar_path(path)), "receipt_sha256_matches": _receipt_hash_ok(sidecar),
                             "signature": {True: "valid", False: "INVALID", None: "not checkable from this install"}[sig],
                             "artifact_sha256_matches_receipt": (sidecar.get("artifact") or {}).get("sha256") == now_sha}
    else:
        result["sidecar"] = {"present": False}
    changed = 0
    for e in store.edges_from(node["id"], "derived_from"):
        src = store.get_node(e["dst"])
        if not src:
            continue
        entry: dict[str, Any] = {"id": src["id"], "kind": src.get("kind"), "locator": src.get("url") or src.get("path") or src.get("label"),
                                 "basis": e.get("basis")}
        if src.get("kind") == "file" and src.get("path"):
            sha, _size = sha256_file(src["path"])
            linked_sha, _linked_size = _linked_state(e, src)
            entry["linked_sha256"], entry["current_sha256"] = linked_sha, sha
            if sha is None and _size is None:
                entry["status"] = "missing"
                changed += 1  # a source that is gone no longer backs the artifact
            elif (e.get("meta") or {}).get("src_sha256"):
                entry["status"] = "unchanged" if sha == linked_sha else "changed"
            elif _rewritten_after(store, e, src):
                # Edge from before 0.1.4 (no snapshot): a recorded write after the link is still a change.
                entry["status"] = "changed"
                entry["reason"] = "rewritten after this artifact was built"
            else:
                entry["status"] = "unchanged" if sha == src.get("sha256") else "changed"
            if entry["status"] == "changed":
                changed += 1
        elif src.get("kind") == "url" and src.get("url"):
            if not refetch:
                entry["status"] = "unchecked"
            else:
                fp = _fetch_url_fingerprint(src["url"])
                entry.update(fp)
                baseline = (src.get("meta") or {}).get("verify_baseline")
                if fp.get("status") == "reachable":
                    if baseline and baseline.get("sha256"):
                        if baseline["sha256"] == fp.get("sha256"):
                            entry["status"] = "unchanged since " + (baseline.get("at") or "baseline")
                        else:
                            entry["status"] = "changed since " + (baseline.get("at") or "baseline")
                            changed += 1
                    else:
                        entry["status"] = "reachable (baseline set)"
                        store.upsert_node(src["id"], "url", touch=False,
                                          meta={"verify_baseline": {"sha256": fp.get("sha256"), "etag": fp.get("etag"),
                                                                    "last_modified": fp.get("last_modified"), "at": _iso(_now())}})
                else:
                    entry["status"] = fp.get("status", "unreachable")
        else:
            entry["status"] = "n/a"
        result["sources"].append(entry)
    result["summary"] = {
        "artifact": artifact_status,
        "sources_total": len(result["sources"]),
        "sources_changed": changed,
        "verdict": artifact_status if artifact_status != "unchanged" else ("stale" if changed else "fresh"),
    }
    store.upsert_node(node["id"], "file", touch=False, meta={"last_verify": {"at": result["checked_at"], "verdict": result["summary"]["verdict"],
                                                            "sources_changed": changed}})
    return result


def _receipt_hash_ok(receipt: dict) -> Optional[bool]:
    integrity = receipt.get("integrity") or {}
    recorded = integrity.get("receipt_sha256")
    if not recorded:
        return None
    body = dict(receipt)
    body.pop("integrity", None)
    return sha256_bytes(canonical_json(body).encode("utf-8")) == recorded


def verify_receipt_file(receipt_path: str) -> dict:
    """Stand-alone check of a sidecar against the file *beside it*. Needs no database.

    The receipt names the path it was issued for, but a copy travels: what is checked is the file next
    to the receipt (``x.md`` for ``x.md.receipt.json``), never the original path, which may be somewhere
    else entirely. ``ok`` needs the bytes to match, the receipt body to hash to its recorded digest,
    and the signature to be valid or not checkable here — an INVALID signature always fails."""
    p = Path(receipt_path)
    try:
        receipt = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"cannot read receipt: {exc}"}
    art = receipt.get("artifact") or {}
    if str(p).endswith(SIDECAR_SUFFIX):
        target = str(p)[: -len(SIDECAR_SUFFIX)]
    else:
        target = str(p.with_name(art.get("name") or os.path.basename(art.get("path") or "")))
    sha, size = sha256_file(target) if os.path.isfile(target) else (None, None)
    body_ok = _receipt_hash_ok(receipt)
    sig = check_signature(receipt)
    out = {
        "ok": bool(sha) and sha == art.get("sha256") and body_ok is True and sig is not False,
        "receipt": str(p),
        "artifact": target,
        "issued_for": art.get("path"),
        "artifact_sha256_matches": sha == art.get("sha256") if sha else False,
        "receipt_sha256_matches": body_ok,
        "signature": {True: "valid", False: "INVALID", None: "not checkable from this install"}[sig],
        "recorded_sha256": art.get("sha256"),
        "current_sha256": sha,
        "sources": len(receipt.get("sources") or []),
        "schema": receipt.get("schema"),
    }
    if sha is None:
        out["error"] = "no file beside the receipt"
    return out


# -------------------------------------------------------------------- exports
def _mm_id(node_id: str) -> str:
    return "n" + sha256_text(node_id)[:10]


def _mm_label(node: dict) -> str:
    label = (node.get("label") or node.get("id") or "").replace('"', "'").replace("\n", " ")
    kind = node.get("kind")
    if kind == "file":
        label = os.path.basename(node.get("path") or label) or label
    return label[:60]


def to_mermaid(graph: dict) -> str:
    """Mermaid ``flowchart LR`` for a neighbourhood: thick arrows observed, solid evidenced, dashed inferred."""
    lines = ["flowchart LR"]
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    shapes = {"file": ('[["{}"]]', ""), "url": ('(("{}"))', ""), "query": ('>"{}"]', ""), "tool_call": ('["{}"]', ""),
              "model_call": ('{{"{}"}}', ""), "aux_call": ('{{"{}"}}', ""), "agent": ('(["{}"])', ""), "turn": ('[/"{}"/]', ""),
              "skill": ('["{}"]', ""), "memory": ('["{}"]', "")}
    for nid, n in nodes.items():
        shape = shapes.get(n.get("kind"), ('["{}"]', ""))[0]
        lines.append(f"    {_mm_id(nid)}{shape.format(_mm_label(n))}")
    for e in graph.get("edges", []):
        if e["src"] not in nodes or e["dst"] not in nodes:
            continue
        arrow = {"observed": "==>", "evidenced": "-->", "inferred": "-.->"}.get(e.get("basis"), "-->")
        lines.append(f"    {_mm_id(e['src'])} {arrow}|{e['rel']}| {_mm_id(e['dst'])}")
    lines.append("    classDef artifact stroke-width:3px;")
    for nid, n in nodes.items():
        if n.get("kind") == "file" and n.get("produced"):
            lines.append(f"    class {_mm_id(nid)} artifact;")
    return "\n".join(lines) + "\n"


def to_dot(graph: dict) -> str:
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    lines = ["digraph lineage {", "  rankdir=LR;", '  node [fontname="Helvetica", shape=box];']
    for nid, n in nodes.items():
        shape = {"file": "note", "url": "ellipse", "query": "cds", "tool_call": "box", "model_call": "hexagon",
                 "aux_call": "hexagon", "agent": "component", "turn": "parallelogram"}.get(n.get("kind"), "box")
        label = _mm_label(n).replace('"', '\\"')
        lines.append(f'  "{nid}" [label="{label}", shape={shape}];')
    for e in graph.get("edges", []):
        if e["src"] in nodes and e["dst"] in nodes:
            style = {"observed": "bold", "evidenced": "solid", "inferred": "dashed"}.get(e.get("basis"), "solid")
            lines.append(f'  "{e["src"]}" -> "{e["dst"]}" [label="{e["rel"]}", style={style}];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def explain(store: Store, ref: str, *, max_sources: int = 12) -> Optional[str]:
    """A short, human-readable account of where an artifact came from."""
    receipt = build_receipt(store, ref, include_signature=False)
    if not receipt:
        return None
    a = receipt["artifact"]
    size = a.get("size")
    lines = [f"{a['name']}  ({(a.get('sha256') or '?')[:12]}…, {'?' if size is None else size} bytes)"]
    p = receipt.get("produced_by") or {}
    if p:
        lines.append(f"  written by {p.get('tool')} at {p.get('at')}")
    chain = (receipt.get("agent") or {}).get("chain") or []
    if chain:
        agents = " ← ".join(f"{c.get('role') or 'agent'}" + (f" ({c.get('model')})" if c.get("model") else "") for c in chain)
        lines.append(f"  by {agents}")
    if receipt.get("models"):
        m = receipt["models"][0]
        via = f" via {m['provider']}" if m.get("provider") else ""
        lines.append(f"  model call {m.get('model') or '?'}{via}")
    t = receipt.get("turn") or {}
    if t.get("user_prompt_preview"):
        lines.append(f"  asked: “{t['user_prompt_preview'][:120]}”")
    srcs = receipt.get("sources") or []
    if srcs:
        lines.append(f"  from {len(srcs)} source(s):")
        for s in srcs[:max_sources]:
            mark = {"observed": "●", "evidenced": "◉", "inferred": "○"}.get(s.get("basis"), "○")
            loc = s.get("locator") or s.get("id")
            if s.get("kind") == "file":
                loc = os.path.basename(str(loc))
            extra = f"  {s['reason']}" if s.get("reason") else ""
            lines.append(f"    {mark} {s.get('basis'):9s} {s.get('kind'):5s} {str(loc)[:80]}{extra}")
        if len(srcs) > max_sources:
            lines.append(f"    … and {len(srcs) - max_sources} more")
    else:
        lines.append("  no sources recorded (made from the prompt alone, or read outside Hermes)")
    lines.append("  ● observed  ◉ evidenced  ○ inferred")
    return "\n".join(lines)


__all__ = [
    "BASIS_LEGEND", "SIDECAR_SUFFIX", "build_receipt", "write_sidecar", "read_sidecar", "sidecar_path", "verify",
    "verify_receipt_file", "check_signature", "install_key", "to_mermaid", "to_dot", "explain", "canonical_json",
]
