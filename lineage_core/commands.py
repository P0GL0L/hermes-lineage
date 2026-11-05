"""Slash command (``/lineage``) and CLI (``hermes lineage``) front-ends.

Both are thin: parse, call the core, format text. Handlers return their text
(slash commands run in the gateway, where ``print`` goes nowhere useful).
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from .receipt import build_receipt, explain, to_dot, to_mermaid, verify, verify_receipt_file, write_sidecar
from .store import Store

HELP = """/lineage                 artifacts made in this session
/lineage <file>          where a file came from
/lineage receipt <file>  write <file>.receipt.json next to it
/lineage verify <file>   check the file and its sources against recorded hashes
/lineage mermaid <file>  the provenance graph as Mermaid
/lineage stats           what the store holds"""


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _resolve_ref(store: Store, ref: str, cwd: Optional[str]) -> Optional[dict]:
    node = store.resolve(ref)
    if node:
        return node
    if cwd and not os.path.isabs(ref):
        return store.resolve(os.path.normpath(os.path.join(cwd, ref)))
    return None


def list_artifacts_text(store: Store, session_id: Optional[str], limit: int = 15) -> str:
    rows = store.list_artifacts(session_id=session_id, limit=limit)
    if not rows and session_id:
        rows = store.list_artifacts(limit=limit)
        scope = "recent (none in this session yet)"
    else:
        scope = "this session" if session_id else "recent"
    if not rows:
        return "lineage: no artifacts recorded yet. Ask the agent to write a file and come back."
    lines = [f"lineage — artifacts, {scope}:"]
    for n in rows:
        meta = n.get("meta") or {}
        verdict = (meta.get("last_verify") or {}).get("verdict")
        tag = f"  [{verdict}]" if verdict else ""
        lines.append(f"  {_fmt_ts(n.get('updated_at'))}  {n.get('label'):40s}  {n.get('source_count', 0):>2} src  "
                     f"{(n.get('sha256') or '?')[:10]}  {meta.get('producer_tool') or ''}{tag}")
    lines.append("  /lineage <file> for the story behind one of them")
    return "\n".join(lines)


def slash_handler(store: Store, raw_args: str, *, session_id: Optional[str] = None, cwd: Optional[str] = None) -> str:
    args = (raw_args or "").strip().split()
    if not args:
        return list_artifacts_text(store, session_id)
    verb, rest = args[0].lower(), " ".join(args[1:]).strip()
    if verb in {"help", "-h", "--help"}:
        return HELP
    if verb == "stats":
        s = store.stats()
        c = s["counts"]
        return (f"lineage store: {c['artifacts']} artifacts, {c['file']} files, {c['url']} urls, {c['tool_call']} tool calls, "
                f"{c['model_call']} model calls, {c['edges']} edges ({s['edges_by_basis']}); {c['sessions']} sessions. "
                f"{(s['db_bytes'] or 0) // 1024} KB at {s['db_path']}")
    if verb in {"receipt", "verify", "mermaid", "dot", "show", "explain"}:
        if not rest:
            return f"usage: /lineage {verb} <file>"
        node = _resolve_ref(store, rest, cwd)
        if not node:
            return f"lineage: nothing recorded for {rest!r}. Only files the agent wrote through Hermes are tracked."
        if verb == "receipt":
            path = write_sidecar(store, node["id"])
            return f"receipt written: {path}" if path else "lineage: could not write the receipt"
        if verb == "verify":
            res = verify(store, node["id"])
            return _verify_text(res)
        if verb == "mermaid":
            return "```mermaid\n" + to_mermaid(store.neighborhood(node["id"])) + "```"
        if verb == "dot":
            return "```dot\n" + to_dot(store.neighborhood(node["id"])) + "```"
        return explain(store, node["id"]) or "lineage: nothing to explain"
    node = _resolve_ref(store, raw_args.strip(), cwd)
    if node:
        return explain(store, node["id"]) or "lineage: nothing to explain"
    return f"lineage: nothing recorded for {raw_args.strip()!r}.\n{HELP}"


def _verify_text(res: Optional[dict]) -> str:
    if not res:
        return "lineage: nothing to verify"
    a, s = res["artifact"], res["summary"]
    lines = [f"{os.path.basename(a['path'])}: {a['status']}  → verdict: {s['verdict']}"]
    if a["status"] == "modified":
        lines.append(f"  recorded {str(a['recorded_sha256'])[:12]}… now {str(a['current_sha256'])[:12]}…")
    sc = res.get("sidecar") or {}
    if sc.get("present"):
        lines.append(f"  receipt: {sc.get('path')}  signature {sc.get('signature')}  hash {'ok' if sc.get('receipt_sha256_matches') else 'MISMATCH'}")
    for src in res["sources"]:
        loc = src.get("locator") or src["id"]
        if src.get("kind") == "file":
            loc = os.path.basename(str(loc))
        lines.append(f"  {src.get('status', '?'):28s} {src.get('basis', ''):9s} {str(loc)[:70]}")
    if any(x.get("status") == "unchecked" for x in res["sources"]):
        lines.append("  URLs are unchecked; `hermes lineage verify <file> --refetch` reaches the network to check them.")
    return "\n".join(lines)


# ----------------------------------------------------------------------- CLI
def cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="lineage_cmd")
    p = sub.add_parser("list", help="artifacts the agent produced")
    p.add_argument("--session", help="limit to one session id")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="where a file came from")
    p.add_argument("ref", help="path, basename or node id")
    p.add_argument("--json", action="store_true", help="print the full receipt instead of the summary")
    p = sub.add_parser("receipt", help="build a receipt; --write puts <file>.receipt.json beside the file")
    p.add_argument("ref")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("verify", help="check a file and its sources against recorded hashes")
    p.add_argument("ref")
    p.add_argument("--refetch", action="store_true", help="also re-fetch URL sources (network)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("check", help="verify a .receipt.json against the file beside it (no database needed)")
    p.add_argument("receipt")
    p = sub.add_parser("graph", help="export the provenance graph")
    p.add_argument("ref")
    p.add_argument("--format", choices=["mermaid", "dot", "json"], default="mermaid")
    p.add_argument("--depth", type=int, default=6)
    p.add_argument("--direction", choices=["provenance", "up", "down", "both"], default="provenance")
    sub.add_parser("stats", help="what the store holds")
    sub.add_parser("sessions", help="sessions with artifact counts")
    p = sub.add_parser("prune", help="delete sessions older than N days")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--yes", action="store_true")


def cli_handler(args, store_factory=Store.shared) -> int:
    cmd = getattr(args, "lineage_cmd", None)
    if cmd == "check":
        res = verify_receipt_file(args.receipt)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    store = store_factory()
    if cmd in (None, "list"):
        rows = store.list_artifacts(session_id=getattr(args, "session", None), limit=getattr(args, "limit", 30))
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, default=str))
            return 0
        if not rows:
            print("no artifacts recorded yet")
            return 0
        for n in rows:
            print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(n.get('updated_at') or 0))}  {(n.get('sha256') or '?')[:10]}  "
                  f"{n.get('source_count', 0):>2} src  {n.get('path')}")
        return 0
    if cmd == "show":
        node = store.resolve(args.ref)
        if not node:
            print(f"nothing recorded for {args.ref!r}")
            return 1
        if args.json:
            print(json.dumps(build_receipt(store, node["id"]), indent=2, ensure_ascii=False))
        else:
            print(explain(store, node["id"]))
        return 0
    if cmd == "receipt":
        node = store.resolve(args.ref)
        if not node:
            print(f"nothing recorded for {args.ref!r}")
            return 1
        if args.write:
            path = write_sidecar(store, node["id"])
            print(f"wrote {path}")
        else:
            print(json.dumps(build_receipt(store, node["id"]), indent=2, ensure_ascii=False))
        return 0
    if cmd == "verify":
        node = store.resolve(args.ref)
        if not node:
            print(f"nothing recorded for {args.ref!r}")
            return 1
        res = verify(store, node["id"], refetch=args.refetch)
        print(json.dumps(res, indent=2) if args.json else _verify_text(res))
        return 0 if res and res["summary"]["verdict"] == "fresh" else 2
    if cmd == "graph":
        node = store.resolve(args.ref)
        if not node:
            print(f"nothing recorded for {args.ref!r}")
            return 1
        g = store.neighborhood(node["id"], depth=args.depth, direction=args.direction)
        print({"mermaid": to_mermaid, "dot": to_dot}.get(args.format, lambda x: json.dumps(x, indent=2, default=str))(g))
        return 0
    if cmd == "stats":
        print(json.dumps(store.stats(), indent=2))
        return 0
    if cmd == "sessions":
        for s in store.list_sessions():
            print(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(s.get('started_at') or 0))}  {s.get('role') or 'main':8s}  "
                  f"{s.get('artifact_count', 0):>3} artifacts  {s['id']}")
        return 0
    if cmd == "prune":
        cutoff = time.time() - args.days * 86400
        if not args.yes:
            print(f"would delete sessions older than {args.days} days; add --yes to do it")
            return 0
        print(json.dumps(store.prune(cutoff)))
        return 0
    print(HELP.replace("/lineage", "hermes lineage"))
    return 0


__all__ = ["slash_handler", "cli_setup", "cli_handler", "list_artifacts_text", "HELP"]
