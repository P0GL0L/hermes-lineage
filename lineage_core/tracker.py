"""Turns Hermes observer-hook events into provenance.

The Tracker owns no behaviour: every method is a hook callback that records what
Hermes reports, decides which nodes it implies, and links them with an explicit
basis. The three bases are the whole product:

* ``observed``  — Hermes said so. ``write_file`` wrote this path; this tool call
  came out of that model request (``api_request_id``); this child agent was
  spawned by that turn.
* ``evidenced`` — the artifact's own bytes name the source: a URL that was
  fetched this session, or the path of a file that was read.
* ``inferred``  — the source was read earlier in the same turn (confidence 0.6)
  or in one of the last few turns of the session (0.35). Useful, and labelled.

Every public method swallows its own exceptions. A provenance recorder that can
interrupt the agent is worse than no recorder.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import PLUGIN_ID
from .hashing import (
    existing_paths,
    find_path_candidates,
    find_urls,
    is_probably_text,
    normalize_url,
    resolve_path,
    sha256_file,
    sha256_text,
    short_id,
)
from .redact import bound_args, preview
from .store import Store

log = logging.getLogger("hermes.plugins.lineage")

WRITE_TOOLS = {"write_file", "patch"}
EXEC_TOOLS = {"terminal", "execute_code"}
READ_TOOLS = {"read_file"}
IGNORED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".cache", "dist", "build", ".next", ".turbo", "target", ".hermes",
    ".idea", ".vscode", ".DS_Store",
}
# Files lineage never treats as artifacts even when a command touched them.
IGNORED_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".tmp", ".lock", ".receipt.json")
IGNORED_FILE_NAMES = {".DS_Store", "Thumbs.db"}
# Credential locations: a file merely *named* in a shell command is not opened when it looks like one
# of these (``chmod 600 ~/.ssh/id_rsa`` must not make lineage read a private key).
_CREDENTIAL_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store"}
_CREDENTIAL_NAME_RE = re.compile(
    r"(?i)^(?:\.env(?:\..*)?|\.netrc|\.npmrc|\.pypirc|\.pgpass|\.git-credentials|credentials(?:\..*)?|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|.*\.(?:pem|key|p12|pfx|jks|keystore|kdbx|keychain-db))$"
)
_V4A_FILE_HEADER = re.compile(r"^\*\*\*\s*(Update|Add|Delete)\s+File:\s*(.+?)\s*$")
_V4A_MOVE_HEADER = re.compile(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+?)\s*$")


def looks_like_credential(path: str) -> bool:
    parts = Path(path).parts
    if any(part in _CREDENTIAL_DIRS for part in parts):
        return True
    if any(a == ".config" and b in {"gcloud", "gh"} for a, b in zip(parts, parts[1:])):
        return True
    return bool(_CREDENTIAL_NAME_RE.match(os.path.basename(path)))


DEFAULTS: dict[str, Any] = {
    "snapshot_max_files": 6000,
    "snapshot_max_seconds": 0.5,
    "max_artifacts_per_call": 50,
    "evidence_max_bytes": 2 * 1024 * 1024,
    "evidence_scan_bytes": 512 * 1024,
    "session_window_turns": 3,
    "session_window_max_sources": 25,
    "turn_max_inferred_sources": 25,
    "sidecar": False,
    "store_previews": True,
    "track_aux_calls": True,
}


def _now() -> float:
    return time.time()


def _s(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(value)
    except Exception:
        return None


def _safe(fn: Callable) -> Callable:
    """Wrap a hook so that nothing it does can reach the agent loop."""

    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # pragma: no cover - defensive by design
            log.debug("lineage %s failed: %s", fn.__name__, exc, exc_info=True)
            return None

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class _TurnState:
    __slots__ = ("session_id", "started", "consumed", "produced")

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.started = _now()
        self.consumed: list[str] = []  # node ids read in this turn, in order
        self.produced: list[str] = []  # artifact node ids written in this turn


class Tracker:
    def __init__(
        self,
        store: Store,
        config: Optional[dict] = None,
        *,
        emit: Optional[Callable[[str, dict], None]] = None,
        cwd_resolver: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.store = store
        self.config = dict(DEFAULTS)
        self.config.update({k: v for k, v in (config or {}).items() if v is not None})
        self._emit = emit
        self._cwd_resolver = cwd_resolver
        self._lock = threading.RLock()
        self._turns: dict[str, _TurnState] = {}
        self._session_cwd: dict[str, str] = {}
        self._session_turn: dict[str, str] = {}  # session -> current turn id

    # ----------------------------------------------------------------- helpers
    def emit(self, event: str, payload: dict) -> None:
        if self._emit is None:
            return
        try:
            self._emit(event, payload)
        except Exception:
            pass

    def _cwd(self, session_id: Optional[str]) -> Optional[str]:
        if session_id and session_id in self._session_cwd:
            return self._session_cwd[session_id]
        if self._cwd_resolver is not None:
            try:
                resolved = self._cwd_resolver()
                if resolved:
                    return str(resolved)
            except Exception:
                pass
        try:
            return os.getcwd()
        except OSError:
            return None

    def _turn(self, turn_id: Optional[str], session_id: Optional[str]) -> Optional[_TurnState]:
        if not turn_id:
            if session_id and session_id in self._session_turn:
                turn_id = self._session_turn[session_id]
            else:
                return None
        with self._lock:
            st = self._turns.get(turn_id)
            if st is None and session_id:
                st = _TurnState(session_id)
                self._turns[turn_id] = st
                self._session_turn[session_id] = turn_id
                self.store.open_turn(turn_id, session_id)
                self._ensure_turn_node(turn_id, session_id, None)
            return st

    def _ensure_agent(self, session_id: Optional[str], role: Optional[str] = None, **fields) -> Optional[str]:
        if not session_id:
            return None
        nid = f"agent:{session_id}"
        existing = self.store.get_node(nid)
        label = fields.pop("label", None)
        meta = {k: v for k, v in fields.items() if v is not None}
        if role and (existing is None or (existing.get("meta") or {}).get("role") in (None, "main", "child") or role not in ("main", "child")):
            if not (existing and (existing.get("meta") or {}).get("role") not in (None, "main", "child") and role in ("main", "child")):
                meta["role"] = role
        if existing is None and "role" not in meta:
            meta["role"] = "main"
        if label is None and existing is None:
            label = f"{meta.get('role', 'agent')} agent" if meta.get("role") not in (None, "main") else "agent"
        self.store.upsert_node(nid, "agent", label=label, session_id=session_id, meta=meta)
        return nid

    def _ensure_turn_node(self, turn_id: str, session_id: Optional[str], user_message: Optional[str]) -> str:
        nid = f"turn:{turn_id}"
        label = preview(user_message, 80) if user_message else None
        self.store.upsert_node(nid, "turn", label=label or "turn", session_id=session_id, turn_id=turn_id)
        return nid

    def _file_node(
        self, abs_path: str, *, session_id: Optional[str], turn_id: Optional[str], tool_call_id: Optional[str],
        produced: bool = False, consumed: bool = False, hash_now: bool = True, meta: Optional[dict] = None,
    ) -> dict:
        nid = f"file:{abs_path}"
        sha, size = (None, None)
        if hash_now:
            sha, size = sha256_file(abs_path)
        if not produced:
            existing = self.store.get_node(nid)
            if existing and existing.get("produced"):
                # A file the agent made keeps the hash it *wrote* (verify and receipts check against it).
                # What a later read saw is kept beside it, so tampering stays visible.
                return self.store.upsert_node(
                    nid, "file", consumed=consumed, touch=False,
                    meta={**(meta or {}), "last_read_sha256": sha, "last_read_size": size, "last_read_at": _now()},
                )
        node = self.store.upsert_node(
            nid, "file", label=os.path.basename(abs_path) or abs_path, path=abs_path, sha256=sha, size=size,
            session_id=session_id, turn_id=turn_id, tool_call_id=tool_call_id, produced=produced,
            consumed=consumed, meta=meta, overwrite_ids=bool(produced),
        )
        return node

    def _url_node(self, url: str, *, session_id: Optional[str], turn_id: Optional[str], tool_call_id: Optional[str],
                  consumed: bool, content_sha: Optional[str] = None, size: Optional[int] = None,
                  meta: Optional[dict] = None) -> dict:
        norm = normalize_url(url)
        nid = f"url:{norm}"
        label = norm.split("://", 1)[-1]
        if len(label) > 72:
            label = label[:69] + "…"
        return self.store.upsert_node(
            nid, "url", label=label, url=norm, sha256=content_sha, size=size, session_id=session_id,
            turn_id=turn_id, tool_call_id=tool_call_id, consumed=consumed, meta=meta,
        )

    # ---------------------------------------------------------------- sessions
    @_safe
    def on_session_start(self, **kw) -> None:
        sid = _s(kw.get("session_id"))
        if not sid:
            return
        cwd = _s(kw.get("cwd"))
        if cwd:
            self._session_cwd[sid] = cwd
        parent = _s(kw.get("parent_session_id"))
        # Hermes's on_session_start carries no parent id; a child announced by subagent_start first
        # already has its role, which "main" must not overwrite.
        known = self.store.get_session(sid)
        is_root = not parent and not (known and (known.get("role") or known.get("parent_session_id")))
        self.store.upsert_session(
            sid, parent_session_id=parent, platform=_s(kw.get("platform")),
            model=_s(kw.get("model")), cwd=cwd, role="main" if is_root else None,
            meta={"profile": _s(kw.get("profile_name")), "provider": _s(kw.get("provider"))},
        )
        self._ensure_agent(sid, "main" if is_root else None, model=_s(kw.get("model")), platform=_s(kw.get("platform")))

    @_safe
    def on_session_end(self, **kw) -> None:
        sid = _s(kw.get("session_id"))
        if sid:
            self.store.end_session(sid)

    # ------------------------------------------------------------------- turns
    @_safe
    def pre_llm_call(self, **kw) -> None:
        sid, tid = _s(kw.get("session_id")), _s(kw.get("turn_id"))
        if not sid:
            return
        if not tid:
            tid = f"turn-{short_id(sid + str(_now()))}"
        user_message = kw.get("user_message")
        um_text = user_message if isinstance(user_message, str) else json.dumps(user_message, default=str)
        with self._lock:
            self._turns[tid] = _TurnState(sid)
            self._session_turn[sid] = tid
        self.store.upsert_session(sid, parent_session_id=_s(kw.get("parent_session_id")),
                                  platform=_s(kw.get("platform")), model=_s(kw.get("model")))
        self._ensure_agent(sid, None)
        self.store.open_turn(
            tid, sid, model=_s(kw.get("model")), platform=_s(kw.get("platform")),
            user_preview=preview(um_text, 200) if self.config["store_previews"] else "",
            user_sha=sha256_text(um_text or ""),
        )
        self._ensure_turn_node(tid, sid, um_text if self.config["store_previews"] else None)

    @_safe
    def post_llm_call(self, **kw) -> None:
        sid, tid = _s(kw.get("session_id")), _s(kw.get("turn_id"))
        st = self._turn(tid, sid)
        tid = tid or (self._session_turn.get(sid) if sid else None)
        if not tid:
            return
        response = kw.get("assistant_response")
        text = response if isinstance(response, str) else json.dumps(response, default=str)
        self.store.close_turn(
            tid, assistant_preview=preview(text, 200) if self.config["store_previews"] else "",
            assistant_sha=sha256_text(text or ""), status="done",
        )
        # The reply itself is evidence: a file or URL the agent names in its answer is cited.
        turn_node = f"turn:{tid}"
        cited = 0
        for url in find_urls(text or ""):
            nid = f"url:{normalize_url(url)}"
            if self.store.get_node(nid):
                self.store.add_edge(turn_node, nid, "cited_in_reply", basis="evidenced", turn_id=tid)
                cited += 1
        cwd = self._cwd(sid)
        for cand in find_path_candidates(text or ""):
            abs_path = resolve_path(cand, cwd)
            if self.store.get_node(f"file:{abs_path}"):
                self.store.add_edge(turn_node, f"file:{abs_path}", "cited_in_reply", basis="evidenced", turn_id=tid)
                cited += 1
            else:
                node = self.store.resolve(os.path.basename(cand)) if "/" not in cand else None
                if node and node.get("session_id") == sid:
                    self.store.add_edge(turn_node, node["id"], "cited_in_reply", basis="evidenced", turn_id=tid)
                    cited += 1
        produced = list(st.produced) if st else []
        if produced and self.config.get("sidecar"):
            from .receipt import write_sidecar  # local import keeps hook startup cheap

            for nid in produced:
                try:
                    write_sidecar(self.store, nid)
                except Exception as exc:  # pragma: no cover
                    log.debug("sidecar failed for %s: %s", nid, exc)
        self.emit("turn.closed", {"session_id": sid, "turn_id": tid, "artifacts": produced, "cited": cited})
        with self._lock:
            # Keep recent turn state around for late post_tool_call events; trim the oldest beyond 64.
            if len(self._turns) > 64:
                live = set(self._session_turn.values())
                stale = [k for k in sorted(self._turns, key=lambda k: self._turns[k].started) if k not in live]
                for old_id in stale[:32]:
                    self._turns.pop(old_id, None)


    # ------------------------------------------------------------ model calls
    @_safe
    def post_api_request(self, **kw) -> None:
        rid = _s(kw.get("api_request_id"))
        if not rid:
            return
        usage = kw.get("usage")
        if usage is not None and not isinstance(usage, dict):
            try:
                usage = dict(usage)
            except Exception:
                usage = {"repr": preview(usage, 120)}
        self.store.upsert_node(
            f"mc:{rid}", "model_call", label=_s(kw.get("response_model") or kw.get("model")) or "model",
            session_id=_s(kw.get("session_id")), turn_id=_s(kw.get("turn_id")),
            meta={
                "status": "ok", "model": _s(kw.get("model")), "provider": _s(kw.get("provider")), "base_url": _s(kw.get("base_url")),
                "api_mode": _s(kw.get("api_mode")), "api_call_count": kw.get("api_call_count"),
                "response_model": _s(kw.get("response_model")), "finish_reason": _s(kw.get("finish_reason")),
                "api_duration": kw.get("api_duration"), "started_at": kw.get("started_at"),
                "ended_at": kw.get("ended_at") or _now(), "usage": usage, "tool_calls": kw.get("assistant_tool_call_count"),
            },
            overwrite_ids=True,
        )

    @_safe
    def api_request_error(self, **kw) -> None:
        rid = _s(kw.get("api_request_id"))
        if not rid:
            return
        err = kw.get("error")
        self.store.upsert_node(
            f"mc:{rid}", "model_call", session_id=_s(kw.get("session_id")), turn_id=_s(kw.get("turn_id")),
            meta={"status": "error", "status_code": kw.get("status_code"), "retryable": kw.get("retryable"),
                  "error": preview(err, 200) if err is not None else None},
        )

    @_safe
    def post_auxiliary_call(self, **kw) -> None:
        if not self.config.get("track_aux_calls"):
            return
        sid, tid, rid = _s(kw.get("session_id")), _s(kw.get("turn_id")), _s(kw.get("api_request_id"))
        task = _s(kw.get("aux_task")) or "auxiliary"
        nid = f"aux:{rid or short_id(task + str(_now()))}"
        self.store.upsert_node(
            nid, "aux_call", label=f"{task}: {_s(kw.get('model')) or 'model'}", session_id=sid, turn_id=tid,
            meta={"aux_task": task, "model": _s(kw.get("model")), "provider": _s(kw.get("provider")),
                  "api_duration": kw.get("api_duration"), "error": preview(kw.get("error"), 160) if kw.get("error") else None,
                  "ended_at": kw.get("ended_at") or _now()},
        )
        if tid:
            self.store.add_edge(nid, f"turn:{tid}", "in_turn", basis="observed", turn_id=tid)

    # -------------------------------------------------------------- subagents
    @_safe
    def subagent_start(self, **kw) -> None:
        parent, child = _s(kw.get("parent_session_id")), _s(kw.get("child_session_id"))
        if not child:
            return
        role = _s(kw.get("child_role")) or "subagent"
        self.store.upsert_session(child, parent_session_id=parent, role=role,
                                  meta={"goal": preview(kw.get("child_goal"), 200), "child_subagent_id": _s(kw.get("child_subagent_id"))})
        self._ensure_agent(child, role, label=f"{role}", goal=preview(kw.get("child_goal"), 200))
        if parent:
            self._ensure_agent(parent)
            self.store.add_edge(f"agent:{child}", f"agent:{parent}", "delegated_by", basis="observed")
            if child in self._session_cwd or parent in self._session_cwd:
                self._session_cwd.setdefault(child, self._session_cwd.get(parent, ""))
        ptid = _s(kw.get("parent_turn_id"))
        if ptid:
            self.store.add_edge(f"agent:{child}", f"turn:{ptid}", "spawned_in", basis="observed", turn_id=ptid)

    @_safe
    def subagent_stop(self, **kw) -> None:
        child = _s(kw.get("child_session_id"))
        if not child:
            return
        self.store.upsert_node(
            f"agent:{child}", "agent", session_id=child,
            meta={"status": _s(kw.get("status") or kw.get("child_status")), "duration_ms": kw.get("duration_ms"),
                  "summary": preview(kw.get("child_summary"), 240)},
        )
        self.store.end_session(child)

    # -------------------------------------------------------------- tool calls
    # lineage deliberately registers no pre_tool_call: in Hermes that hook is fail-closed (a slow or
    # stuck callback blocks the agent's tool call). Everything is reconstructed after the fact from
    # post_tool_call, which carries duration_ms and api_request_id.
    @_safe
    def post_tool_call(self, **kw) -> None:
        tool = _s(kw.get("tool_name")) or "tool"
        sid, tid = _s(kw.get("session_id")), _s(kw.get("turn_id"))
        args = kw.get("args") if isinstance(kw.get("args"), dict) else {}
        tcid = _s(kw.get("tool_call_id")) or f"anon-{uuid.uuid4().hex[:12]}"
        duration = kw.get("duration_ms")
        ended = _now()
        try:
            started = ended - (float(duration) / 1000.0 if duration is not None else 0.0)
        except (TypeError, ValueError):
            started = ended
        rec = {"tool": tool, "session_id": sid, "turn_id": tid, "api_request_id": _s(kw.get("api_request_id")),
               "started": started, "args": args}
        st = self._turn(tid, sid)
        status = _s(kw.get("status")) or "ok"
        result = kw.get("result")
        result_text = result if isinstance(result, str) else (json.dumps(result, default=str) if result is not None else "")
        if duration is None:
            duration = 0
        tc_node = f"tc:{tcid}"
        label = self._tool_label(tool, args)
        meta = {
            "tool": tool, "status": status, "duration_ms": duration, "started_at": rec["started"], "ended_at": _now(),
            "args": bound_args(args) if self.config["store_previews"] else {"$omitted": True},
            "result_chars": len(result_text), "result_sha256": sha256_text(result_text) if result_text else None,
            "result_preview": preview(result_text, 200) if self.config["store_previews"] else "",
            "error_type": _s(kw.get("error_type")), "error_message": preview(kw.get("error_message"), 200),
        }
        self.store.upsert_node(tc_node, "tool_call", label=label, session_id=sid, turn_id=tid, tool_call_id=tcid, meta=meta)
        if tid:
            self.store.add_edge(tc_node, f"turn:{tid}", "in_turn", basis="observed", turn_id=tid, tool_call_id=tcid)
        rid = rec.get("api_request_id") or _s(kw.get("api_request_id"))
        if rid:
            self.store.upsert_node(f"mc:{rid}", "model_call", session_id=sid, turn_id=tid)
            self.store.add_edge(f"mc:{rid}", tc_node, "invoked", basis="observed", turn_id=tid, tool_call_id=tcid)
        agent = self._ensure_agent(sid)
        if agent:
            self.store.add_edge(tc_node, agent, "run_by", basis="observed", turn_id=tid, tool_call_id=tcid)

        cwd = self._cwd(sid)
        ctx = dict(session_id=sid, turn_id=tid, tool_call_id=tcid)

        # A failed write or read touched nothing. A shell command is different: `pytest --junitxml=r.xml`
        # or a build that writes its output and then exits 1 is reported as an error, yet the files it
        # wrote are real, so terminal/execute_code are always scanned for changed files.
        if status != "ok" and not (tool in EXEC_TOOLS and self._command_ran(result_text, status)):
            return

        with self.store.batch():
            self._post_tool_call_body(tool, args, status, result_text, rec, st, cwd, tc_node, ctx)

    def _post_tool_call_body(self, tool: str, args: dict, status: str, result_text: str, rec: dict,
                             st: Optional[_TurnState], cwd: Optional[str], tc_node: str, ctx: dict) -> None:
        started = rec["started"]
        # 1. What did this call produce? (first, so a command's outputs are not mistaken for its inputs)
        produced: list[tuple[str, Optional[str], str, Optional[str]]] = []  # (abs_path, evidence, basis, reason)
        if tool == "write_file" and args.get("path"):
            content = args.get("content") if isinstance(args.get("content"), str) else None
            produced.append((resolve_path(str(args["path"]), cwd), content, "observed", None))
        elif tool == "patch":
            if args.get("path"):
                # evidence is the whole file after the edit, not the replacement snippet
                produced.append((resolve_path(str(args["path"]), cwd), None, "observed", None))
            elif isinstance(args.get("patch"), str):  # V4A multi-file patch (OpenAI-family models)
                for rel in self._v4a_written_paths(args["patch"]):
                    produced.append((resolve_path(rel, cwd), None, "observed", None))
        elif tool in EXEC_TOOLS:
            workdir = _s(args.get("workdir")) or cwd
            command = _s(args.get("command") or args.get("code")) or ""
            seen_paths: set[str] = set()
            if workdir:
                for changed, ctime_only in self._modified_since(workdir, started):
                    # Hermes says the command ran; *which* files it wrote is lineage's inference from the
                    # working tree (another process writing there at the same moment would look the same).
                    if ctime_only:
                        # chmod, ln, xattrs and renames all bump ctime; only a rename of an artifact counts
                        if not self._moved_artifact(changed):
                            continue
                        produced.append((changed, None, "inferred", "moved here during this call"))
                    else:
                        produced.append((changed, None, "inferred", "changed in the working tree during this call"))
                    seen_paths.add(changed)
            # Redirect / tee / -o targets anywhere on disk (`python gen.py > /tmp/out/report.md`).
            for target in self._command_outputs(command, workdir):
                if target in seen_paths or not os.path.isfile(target) or looks_like_credential(target):
                    continue
                try:
                    if os.stat(target).st_mtime >= started - 0.1:
                        produced.append((target, None, "inferred", "the command's output (redirect or -o)"))
                        seen_paths.add(target)
                except OSError:
                    continue
        produced_paths = {p for p, *_ in produced}
        if tool in EXEC_TOOLS:
            # an output named in the command is never one of its inputs, even if it already existed
            produced_paths |= set(self._command_outputs(_s(args.get("command") or args.get("code")) or "",
                                                        _s(args.get("workdir")) or cwd))

        # 2. What did this call read?
        if status == "ok":
            consumed_ids = self._record_consumed(tool, args, result_text, status, cwd, tc_node, ctx, exclude=produced_paths)
            if st is not None:
                for nid in consumed_ids:
                    if nid not in st.consumed:
                        st.consumed.append(nid)

        # Any tool may announce a file it just made (image_generate, converters, MCP tools): a path in the
        # result that exists and was modified during the call counts, labelled as inferred.
        inferred_outputs = []
        if status == "ok":
            inferred_outputs = [p for p in self._paths_created_during(result_text, cwd, started) if p not in produced_paths]

        cap = int(self.config.get("max_artifacts_per_call") or 50)
        extra = {"call_status": status} if status != "ok" else {}
        for abs_path, evidence, basis, reason in produced[:cap]:
            self._record_artifact(abs_path, tool, tc_node, ctx, st, evidence, basis=basis, reason=reason, extra=extra)
        for abs_path in inferred_outputs[: max(0, cap - len(produced))]:
            self._record_artifact(abs_path, tool, tc_node, ctx, st, None, basis="inferred",
                                  reason="path named in tool result, modified during the call")
        if len(produced) > cap:
            self.store.upsert_node(tc_node, "tool_call", meta={"artifacts_truncated": len(produced) - cap})

    _REDIRECT_RE = re.compile(r"(?:^|[\s;&|(])(?:\d?>>?|&>>?)\s*([^\s;&|<>()]+)")
    _TEE_RE = re.compile(r"\btee\s+(?:-[a-z]+\s+)*([^\s;&|<>()]+)")
    _OUTPUT_LONG_RE = re.compile(r"\s--(?:output|out|output-file|outfile)(?:\s+|=)([^\s;&|<>()]+)")
    # -o FILE, -oFILE, -o=FILE, and a flag cluster ending in o: curl -fsSLo FILE
    _OUTPUT_SHORT_RE = re.compile(r"\s-(?:[A-Za-z]*[oO](?:\s+|=)|[oO])([^\s;&|<>()-][^\s;&|<>()]*)")
    _WRAPPERS = frozenset({"sudo", "env", "time", "nice", "nohup", "command", "exec", "xargs", "timeout", "doas"})
    # Programs where -o / -O names the output file. Elsewhere it means something else
    # (unzip -o overwrite, grep -o only-matching, ssh -o option, ls -o long listing).
    _OUTPUT_SHORT_PROGRAMS = frozenset({
        "curl", "wget", "gcc", "g++", "cc", "c++", "clang", "clang++", "rustc", "nvcc", "go", "pandoc",
        "dot", "mmdc", "yt-dlp", "youtube-dl", "sort", "pdflatex", "xelatex", "tsc", "esbuild", "zig",
    })

    def _command_outputs(self, command: str, cwd: Optional[str]) -> list[str]:
        """Files a shell command names as its output: ``> f``, ``>> f``, ``2> f``, ``tee f``, ``--output f``,
        and ``-o f`` for programs where -o means output (curl, gcc, go build, pandoc, …)."""
        out: list[str] = []
        matches = []
        for rx in (self._REDIRECT_RE, self._TEE_RE, self._OUTPUT_LONG_RE):
            matches.extend(m.group(1) for m in rx.finditer(command or ""))
        for segment in re.split(r"[;&|]+", command or ""):
            words = segment.split()
            # skip wrappers and VAR=value prefixes: sudo curl, env X=1 curl, timeout 30 curl
            while words and (os.path.basename(words[0]) in self._WRAPPERS or "=" in words[0]
                             or words[0].startswith("-") or words[0].isdigit()):
                words = words[1:]
            if words and os.path.basename(words[0]) in self._OUTPUT_SHORT_PROGRAMS:
                matches.extend(m.group(1) for m in self._OUTPUT_SHORT_RE.finditer(" " + segment))
        for raw in matches:
            t = raw.strip("'\"")
            if not t or t.startswith(("&", "/dev/")) or "$" in t:
                continue
            p = resolve_path(t, cwd)
            if p not in out:
                out.append(p)
        return out[:20]

    def _moved_artifact(self, abs_path: str) -> bool:
        """True when *abs_path* has the bytes of an artifact whose own path is gone (it was moved here)."""
        sha, size = sha256_file(abs_path)
        if not sha or not size:
            return False
        return any(t.get("produced") and t.get("path") and not os.path.exists(t["path"])
                   for t in self.store.nodes_with_sha(sha, exclude=f"file:{abs_path}"))

    _NOT_RUN = frozenset({"blocked", "pending_approval", "disabled", "rejected", "denied"})

    @classmethod
    def _command_ran(cls, result_text: str, status: str = "error") -> bool:
        """Whether a shell call that reported an error actually ran (and so may have written files).

        Needs positive evidence, from the shape of Hermes's own result: an exit code from the process
        (anything but -1, Hermes's "nothing ran" code), or a cell that timed out or was interrupted.
        A rejection (blocked, pending approval, the pre-exec guard, argument errors) ran nothing."""
        if status in cls._NOT_RUN:
            return False
        try:
            data = json.loads(result_text) if result_text else None
        except Exception:
            return False
        if not isinstance(data, dict) or data.get("user_summary"):
            return False
        if str(data.get("status") or "").lower() in ("timeout", "interrupted"):
            return True
        code = data.get("exit_code")
        return isinstance(code, int) and not isinstance(code, bool) and code != -1

    # ------------------------------------------------------- consumed inputs
    def _record_consumed(self, tool: str, args: dict, result_text: str, status: str, cwd: Optional[str],
                         tc_node: str, ctx: dict, exclude: Optional[set] = None) -> list[str]:
        ids: list[str] = []
        exclude = exclude or set()

        def read_file_node(p: str, basis: str = "observed", reason: Optional[str] = None) -> None:
            abs_path = resolve_path(p, cwd)
            if abs_path in exclude or not os.path.isfile(abs_path):
                return  # an output of this very call is not one of its inputs
            node = self._file_node(abs_path, consumed=True, **ctx)
            self.store.add_edge(tc_node, node["id"], "read", basis=basis, meta={"reason": reason} if reason else None, **ctx)
            if node["id"] not in ids:
                ids.append(node["id"])

        def read_url_node(u: str, basis: str = "observed", **extra) -> None:
            node = self._url_node(u, consumed=True, **ctx, **extra)
            self.store.add_edge(tc_node, node["id"], "read", basis=basis, **ctx)
            if node["id"] not in ids:
                ids.append(node["id"])

        if tool in READ_TOOLS and args.get("path"):
            read_file_node(str(args["path"]))
        elif tool == "patch" and args.get("path"):
            # a patch reads the file it edits; the write is recorded separately
            abs_path = resolve_path(str(args["path"]), cwd)
            if os.path.isfile(abs_path):
                ids.append(f"file:{abs_path}")
        elif tool == "web_search":
            q = _s(args.get("query"))
            if q:
                qid = f"query:{short_id(q)}"
                self.store.upsert_node(qid, "query", label=preview(q, 80), consumed=True, meta={"query": preview(q, 300), "tool": tool}, **ctx)
                self.store.add_edge(tc_node, qid, "read", basis="observed", **ctx)
                ids.append(qid)
                for url in self._urls_from_search_result(result_text):
                    node = self._url_node(url, consumed=False, **ctx, meta={"found_by": preview(q, 120)})
                    self.store.add_edge(qid, node["id"], "returned", basis="observed", **ctx)
        elif tool in {"web_extract", "web_fetch", "fetch_url"}:
            urls = args.get("urls") or args.get("url") or []
            if isinstance(urls, str):
                urls = [urls]
            hashes = self._content_hashes_from_extract(result_text)
            for u in urls:
                if isinstance(u, str) and u.startswith(("http://", "https://")):
                    sha, size = hashes.get(normalize_url(u), (None, None))
                    read_url_node(u, content_sha=sha, size=size, meta={"fetched_at": _now()})
        elif tool == "skill_view":
            name = _s(args.get("name") or args.get("skill"))
            if name:
                nid = f"skill:{name}"
                self.store.upsert_node(nid, "skill", label=name, consumed=True, **ctx)
                self.store.add_edge(tc_node, nid, "read", basis="observed", **ctx)
                ids.append(nid)
        elif tool in EXEC_TOOLS:
            text = _s(args.get("command") or args.get("code")) or ""
            for abs_path in existing_paths(find_path_candidates(text), args.get("workdir") or cwd):
                if looks_like_credential(abs_path):
                    continue  # named is not read: never open a key or .env on a guess
                read_file_node(abs_path, basis="inferred", reason="named in the command")
            for url in find_urls(text):
                read_url_node(url, basis="inferred")  # named in the command; Hermes didn't say it was fetched

        # Generic: URLs and existing files anywhere in the arguments of any other tool
        # (browser tools, MCP tools, converters). Observed: the argument names them.
        if tool not in {"write_file", "web_search"} | READ_TOOLS | EXEC_TOOLS:
            flat = json.dumps(args, default=str) if args else ""
            for url in find_urls(flat)[:20]:
                read_url_node(url)
            for key in ("path", "file", "file_path", "filepath", "input", "source", "src", "paths", "files"):
                val = args.get(key)
                vals = val if isinstance(val, list) else [val]
                for v in vals:
                    if isinstance(v, str) and len(v) < 1024:
                        read_file_node(v)
        return ids

    # ---------------------------------------------------------- produced files
    def _record_artifact(self, abs_path: str, tool: str, tc_node: str, ctx: dict, st: Optional[_TurnState],
                         evidence_text: Optional[str], *, basis: str, reason: Optional[str] = None,
                         extra: Optional[dict] = None) -> None:
        base = os.path.basename(abs_path)
        if base in IGNORED_FILE_NAMES or base.endswith(IGNORED_FILE_SUFFIXES):
            return
        if any(part in IGNORED_DIRS for part in Path(abs_path).parts):
            return
        if not os.path.isfile(abs_path):
            return  # deleted or moved away by this call: nothing to give a receipt to
        node = self._file_node(abs_path, produced=True, **ctx, meta={"producer_tool": tool})
        nid = node["id"]
        new_version = self.store.add_version(nid, sha256=node.get("sha256"), size=node.get("size"),
                                             tool_call_id=ctx.get("tool_call_id"), turn_id=ctx.get("turn_id"))
        previous: dict[str, dict] = {}
        if new_version:
            # Sources describe the bytes on disk now. A new version re-derives them: a source the text
            # still names is evidenced again (whichever session read it), the rest are dropped.
            for e in self.store.edges_from(nid, "derived_from"):
                n = self.store.get_node(e["dst"])
                if n:
                    previous[n["id"]] = n
            self.store.clear_edges(nid, "derived_from")
        edge_meta = dict(extra or {})
        if reason:
            edge_meta["reason"] = reason
        self.store.add_edge(tc_node, nid, "wrote", basis=basis, confidence=0.8 if basis == "inferred" else None,
                            meta=edge_meta or None, **ctx)
        agent = f"agent:{ctx['session_id']}" if ctx.get("session_id") else None
        if agent:
            self.store.add_edge(nid, agent, "produced_by", basis="observed", **ctx)
        if ctx.get("turn_id"):
            self.store.add_edge(nid, f"turn:{ctx['turn_id']}", "produced_in", basis="observed", **ctx)

        # Evidence: the artifact's bytes name a source.
        text = evidence_text
        if text is None:
            text = self._read_text_bounded(abs_path)
        if text and len(text) > int(self.config["evidence_scan_bytes"]):
            text = text[: int(self.config["evidence_scan_bytes"])]
        session_sources = self._session_source_index(ctx.get("session_id"))
        for pid, pn in previous.items():
            session_sources.setdefault(pid, pn)
        evidenced: set[str] = set()
        if text:
            for url in find_urls(text):
                sid_ = f"url:{normalize_url(url)}"
                if sid_ in session_sources and sid_ != nid:
                    self.store.add_edge(nid, sid_, "derived_from", basis="evidenced", meta={"reason": "url appears in artifact"}, **ctx)
                    evidenced.add(sid_)
            for src_id in self._files_named_in(text, session_sources, nid):
                self.store.add_edge(nid, src_id, "derived_from", basis="evidenced", meta={"reason": "path appears in artifact"}, **ctx)
                evidenced.add(src_id)
            # Same bytes as another artifact: the file was moved or copied (``mv draft.md final.md``).
            # Only for a move (the twin's path is gone) or a copy within this session, and never for
            # trivial content: two "\n" __init__.py files are not one file.
            if new_version and node.get("sha256") and (node.get("size") or 0) >= 32:
                for twin in self.store.nodes_with_sha(node["sha256"], exclude=nid):
                    if not twin.get("produced") or twin["id"] in evidenced:
                        continue
                    moved = bool(twin.get("path")) and not os.path.exists(twin["path"])
                    if moved or twin.get("session_id") == ctx.get("session_id"):
                        verb = "moved" if moved else "copied"
                        self.store.add_edge(nid, twin["id"], "derived_from", basis="inferred", confidence=0.9,
                                            meta={"reason": f"same bytes as {twin.get('label') or 'another artifact'} ({verb})"}, **ctx)
                        evidenced.add(twin["id"])

        # Inferred: read earlier in this turn, then in the recent session window.
        if st is not None:
            # newest reads first, bounded: a turn that read 400 files must not cost 400 edges per artifact
            turn_cap = int(self.config.get("turn_max_inferred_sources") or 25)
            recent = [x for x in reversed(st.consumed) if x != nid and x not in evidenced][:turn_cap]
            for src_id in recent:
                self.store.add_edge(nid, src_id, "derived_from", basis="inferred", confidence=0.6,
                                    meta={"reason": "read earlier in this turn"}, **ctx)
            window = self._session_window_sources(ctx.get("session_id"), ctx.get("turn_id"))
            for src_id in window:
                if src_id == nid or src_id in evidenced or src_id in recent or src_id in st.consumed:
                    continue
                self.store.add_edge(nid, src_id, "derived_from", basis="inferred", confidence=0.35,
                                    meta={"reason": "read earlier in this session"}, **ctx)
            if nid not in st.produced:
                st.produced.append(nid)
        self.emit("artifact.created" if new_version else "artifact.touched",
                  {"node_id": nid, "path": abs_path, "sha256": node.get("sha256"), "size": node.get("size"),
                   "session_id": ctx.get("session_id"), "turn_id": ctx.get("turn_id"), "tool": tool})

    def _session_source_index(self, session_id: Optional[str]) -> dict[str, dict]:
        """Everything this session read, plus the files it produced (an artifact can feed a later one)."""
        if not session_id:
            return {}
        index = {n["id"]: n for n in self.store.consumed_in_session(session_id, limit=400)}
        for n in self.store.produced_in_session(session_id, limit=200):
            index.setdefault(n["id"], n)
        return index

    def _session_window_sources(self, session_id: Optional[str], turn_id: Optional[str]) -> list[str]:
        if not session_id:
            return []
        n = int(self.config.get("session_window_turns") or 0)
        if n <= 0:
            return []
        turns = [t for t in self.store.recent_turns(session_id, limit=n + 1) if t["id"] != turn_id][:n]
        if not turns:
            return []
        since = min(t["started_at"] or 0 for t in turns)
        out: list[str] = []
        for node in self.store.consumed_in_session(session_id, since=since, limit=int(self.config["session_window_max_sources"]) * 2):
            if node.get("turn_id") in {t["id"] for t in turns}:
                out.append(node["id"])
            if len(out) >= int(self.config["session_window_max_sources"]):
                break
        return out

    # ------------------------------------------------------- modified files
    def _modified_since(self, workdir: str, started: float) -> list[tuple[str, bool]]:
        """Regular files under *workdir* whose mtime falls inside the call window, bounded by
        ``snapshot_max_files`` and ``snapshot_max_seconds``; a tree too big or too slow yields []."""
        root = Path(workdir)
        if not root.is_dir():
            return []
        limit = int(self.config["snapshot_max_files"])
        deadline = _now() + float(self.config["snapshot_max_seconds"])
        threshold = started - 0.1  # a little slack for filesystem clock granularity
        out: list[str] = []
        seen = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
                for fn in filenames:
                    seen += 1
                    if seen > limit or _now() > deadline:
                        return []
                    if fn in IGNORED_FILE_NAMES:
                        continue
                    p = os.path.join(dirpath, fn)
                    if looks_like_credential(p):
                        continue  # never hash a key or .env just because it changed nearby
                    try:
                        stt = os.stat(p, follow_symlinks=False)
                    except OSError:
                        continue
                    if not os.path.isfile(p) or os.path.islink(p):
                        continue
                    # mtime for writes; ctime too, because a rename (``mv a b``) keeps the old mtime —
                    # the caller decides whether a ctime-only change was a move or just a chmod
                    if stt.st_mtime >= threshold:
                        out.append((p, False))
                    elif stt.st_ctime >= threshold:
                        out.append((p, True))
        except OSError:
            return []
        out.sort()
        return out[:200]

    @staticmethod
    def _v4a_written_paths(patch_text: str) -> list[str]:
        """Files that exist after a V4A patch: Update/Add targets and Move destinations (Hermes's own
        header grammar, ``tools/patch_parser.py``). Deleted files and move sources are not artifacts."""
        out: list[str] = []
        gone: set[str] = set()
        for line in patch_text.splitlines():
            m = _V4A_FILE_HEADER.match(line)
            if m:
                op, path = m.group(1).lower(), m.group(2).strip()
                if op == "delete":
                    gone.add(path)
                elif path not in out:
                    out.append(path)
                continue
            m = _V4A_MOVE_HEADER.match(line)
            if m:
                src, dst = m.group(1).strip(), m.group(2).strip()
                gone.add(src)
                if dst not in out:
                    out.append(dst)
        return [p for p in out if p not in gone]

    @staticmethod
    def _mentions(text: str, needle: str) -> bool:
        """*needle* appears in *text* as a whole path or name, not inside a longer one
        (``data.csv`` is not in ``metadata.csv``; ``b/config.json`` is not in ``sub/config.json``)."""
        if not needle or needle not in text:  # cheap test first: most sources are not named at all
            return False
        return re.search(r"(?<![A-Za-z0-9_.\-])" + re.escape(needle) + r"(?![A-Za-z0-9_])", text) is not None

    @classmethod
    def _files_named_in(cls, text: str, sources: dict[str, dict], self_id: str) -> list[str]:
        """Source files the artifact's text names: by full path, by a path suffix (``b/config.json``),
        or by bare file name (longer than 3 characters) only when no other source shares that name."""
        files = [(sid, n["path"]) for sid, n in sources.items()
                 if n.get("kind") == "file" and sid != self_id and n.get("path")]
        by_name: dict[str, list[tuple[str, str]]] = {}
        for sid, p in files:
            by_name.setdefault(os.path.basename(p), []).append((sid, p))
        out: list[str] = []
        for name, group in by_name.items():
            exact = [sid for sid, p in group if cls._mentions(text, p)]
            if exact:
                out.extend(exact)
                continue
            if not name or len(name) <= 3 or not cls._mentions(text, name):
                continue
            # the longest path suffix that the text names decides; ties mean ambiguous
            hits = []
            for sid, p in group:
                parts = Path(p).parts
                best = max((k for k in range(2, min(len(parts), 4) + 1) if cls._mentions(text, os.path.join(*parts[-k:]))), default=1)
                hits.append((best, sid))
            top = max(b for b, _ in hits)
            winners = [sid for b, sid in hits if b == top]
            if len(group) == 1 or (top > 1 and len(winners) == 1):
                out.extend(winners)
        return out

    def _paths_created_during(self, text: str, cwd: Optional[str], started: float) -> list[str]:
        out: list[str] = []
        if not text:
            return out
        for abs_path in existing_paths(find_path_candidates(text, limit=100), cwd):
            try:
                if os.stat(abs_path).st_mtime >= started - 1.0:
                    out.append(abs_path)
            except OSError:
                continue
        return out

    # ---------------------------------------------------------------- parsing
    @staticmethod
    def _urls_from_search_result(text: str) -> list[str]:
        if not text:
            return []
        try:
            data = json.loads(text)
        except Exception:
            return find_urls(text, limit=20)
        urls: list[str] = []
        web = (data.get("data") or {}).get("web") if isinstance(data, dict) else None
        items = web if isinstance(web, list) else (data.get("results") if isinstance(data, dict) else None)
        for item in items or []:
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"].startswith(("http://", "https://")):
                urls.append(item["url"])
        return urls[:25] or find_urls(text, limit=20)

    @staticmethod
    def _content_hashes_from_extract(text: str) -> dict[str, tuple[Optional[str], Optional[int]]]:
        out: dict[str, tuple[Optional[str], Optional[int]]] = {}
        if not text:
            return out
        try:
            data = json.loads(text)
        except Exception:
            return out
        results = data.get("results") if isinstance(data, dict) else None
        for item in results or []:
            if not isinstance(item, dict):
                continue
            url, content = item.get("url"), item.get("content") or item.get("raw_content") or ""
            if isinstance(url, str) and isinstance(content, str) and content:
                out[normalize_url(url)] = (sha256_text(content), len(content))
        return out

    def _read_text_bounded(self, abs_path: str) -> Optional[str]:
        try:
            size = os.path.getsize(abs_path)
            if size > int(self.config["evidence_max_bytes"]):
                return None
            with open(abs_path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        if not is_probably_text(data[:4096]):
            return None
        return data.decode("utf-8", "replace")

    @staticmethod
    def _tool_label(tool: str, args: dict) -> str:
        target = None
        for key in ("path", "command", "query", "urls", "url", "goal", "name", "code"):
            if key in args and args[key]:
                target = args[key]
                break
        if isinstance(target, list):
            target = target[0] if target else None
        if target is None:
            return tool
        t = preview(target, 60)
        if tool in {"write_file", "read_file", "patch"} and isinstance(target, str):
            t = os.path.basename(target) or t
        return f"{tool}: {t}"


__all__ = ["Tracker", "DEFAULTS", "IGNORED_DIRS"]
