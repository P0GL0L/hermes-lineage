"""SQLite provenance store.

One database per Hermes profile, at ``<HERMES_HOME>/plugin-data/lineage/lineage.db``.
Everything is a node or an edge:

* nodes — ``file``, ``url``, ``query``, ``tool_call``, ``model_call``, ``aux_call``,
  ``agent`` (a session, main or delegated), ``turn``, ``memory``, ``skill``.
* edges — ``src -> dst`` with a relation and a *basis*:
  ``observed`` (Hermes told us: this tool wrote that file), ``evidenced`` (the
  artifact's own bytes name the source), or ``inferred`` (read earlier in the
  same turn / session). The basis is stored, shown and exported; it is never
  collapsed into one "derived from".

Writes are short transactions behind one lock so hook callbacks stay cheap and
the Desktop reader can poll while the agent works.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from . import PLUGIN_ID

BASIS_RANK = {"observed": 3, "evidenced": 2, "inferred": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    platform TEXT,
    model TEXT,
    cwd TEXT,
    role TEXT,
    started_at REAL,
    ended_at REAL,
    meta TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    started_at REAL,
    ended_at REAL,
    model TEXT,
    platform TEXT,
    user_preview TEXT,
    user_sha TEXT,
    assistant_preview TEXT,
    assistant_sha TEXT,
    status TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id, started_at);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    label TEXT,
    path TEXT,
    url TEXT,
    sha256 TEXT,
    size INTEGER,
    session_id TEXT,
    turn_id TEXT,
    tool_call_id TEXT,
    produced INTEGER DEFAULT 0,
    consumed INTEGER DEFAULT 0,
    created_at REAL,
    updated_at REAL,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind, updated_at);
CREATE INDEX IF NOT EXISTS nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS nodes_session ON nodes(session_id, updated_at);
CREATE INDEX IF NOT EXISTS nodes_turn ON nodes(turn_id);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    rel TEXT NOT NULL,
    basis TEXT NOT NULL,
    confidence REAL,
    turn_id TEXT,
    tool_call_id TEXT,
    created_at REAL,
    meta TEXT,
    UNIQUE(src, dst, rel)
);
CREATE INDEX IF NOT EXISTS edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    node_id TEXT NOT NULL,
    sha256 TEXT,
    size INTEGER,
    tool_call_id TEXT,
    turn_id TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS versions_node ON versions(node_id, created_at);
"""


def _j(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(str(value))


def _loads(text: Optional[str]) -> Any:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def default_data_dir() -> Path:
    """``<HERMES_HOME>/plugin-data/lineage``, through Hermes's own helper when available."""
    try:  # inside Hermes: the documented per-plugin storage helper
        from plugins.plugin_storage import plugin_data_dir  # type: ignore

        return Path(plugin_data_dir(PLUGIN_ID))
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        root = Path(get_hermes_home())
    except Exception:
        root = Path(os.getenv("HERMES_HOME") or (Path.home() / ".hermes"))
    path = root / "plugin-data" / PLUGIN_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


class Store:
    """Thread-safe provenance store. One instance per database path."""

    _instances: dict[str, "Store"] = {}
    _instances_lock = threading.Lock()

    @classmethod
    def shared(cls, path: Optional[Path] = None) -> "Store":
        p = Path(path) if path else default_data_dir() / "lineage.db"
        key = str(p)
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls(p)
                cls._instances[key] = inst
            return inst

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._batch_depth = 0
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (str(time.time()),)
            )

    # ------------------------------------------------------------------ basics
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    @contextmanager
    def _tx(self):
        """One write transaction — or none of its own inside :meth:`batch`."""
        with self._lock:
            if self._batch_depth:
                yield
            else:
                with self._conn:
                    yield

    @contextmanager
    def batch(self):
        """Group many writes into one transaction (one fsync instead of one per edge). Holds the lock."""
        with self._lock:
            self._batch_depth += 1
            try:
                if self._batch_depth == 1:
                    with self._conn:
                        yield self
                else:
                    yield self
            finally:
                self._batch_depth -= 1

    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ---------------------------------------------------------------- sessions
    def upsert_session(
        self,
        session_id: str,
        *,
        parent_session_id: Optional[str] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[str] = None,
        role: Optional[str] = None,
        started_at: Optional[float] = None,
        meta: Optional[dict] = None,
    ) -> None:
        if not session_id:
            return
        now = time.time()
        with self._tx():
            row = self._conn.execute("SELECT meta FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO sessions(id, parent_session_id, platform, model, cwd, role, started_at, meta)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, parent_session_id, platform, model, cwd, role, started_at or now, _j(meta or {})),
                )
            else:
                merged = _loads(row["meta"])
                merged.update(meta or {})
                self._conn.execute(
                    "UPDATE sessions SET parent_session_id=COALESCE(?, parent_session_id),"
                    " platform=COALESCE(?, platform), model=COALESCE(?, model), cwd=COALESCE(?, cwd),"
                    " role=COALESCE(?, role), meta=? WHERE id=?",
                    (parent_session_id, platform, model, cwd, role, _j(merged), session_id),
                )

    def end_session(self, session_id: str, ended_at: Optional[float] = None) -> None:
        with self._tx():
            self._conn.execute(
                "UPDATE sessions SET ended_at=? WHERE id=?", (ended_at or time.time(), session_id)
            )

    def get_session(self, session_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return self._row(row)

    def list_sessions(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM nodes n WHERE n.session_id=s.id AND n.kind='file' AND n.produced=1)"
                " AS artifact_count FROM sessions s ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row(r) for r in rows]

    # ------------------------------------------------------------------- turns
    def open_turn(
        self,
        turn_id: str,
        session_id: str,
        *,
        model: Optional[str] = None,
        platform: Optional[str] = None,
        user_preview: str = "",
        user_sha: Optional[str] = None,
        started_at: Optional[float] = None,
    ) -> None:
        if not turn_id:
            return
        with self._tx():
            self._conn.execute(
                "INSERT INTO turns(id, session_id, started_at, model, platform, user_preview, user_sha, status, meta)"
                " VALUES(?,?,?,?,?,?,?,'open','{}')"
                " ON CONFLICT(id) DO UPDATE SET session_id=COALESCE(excluded.session_id, turns.session_id),"
                " model=COALESCE(excluded.model, turns.model), platform=COALESCE(excluded.platform, turns.platform),"
                " user_preview=CASE WHEN turns.user_preview='' OR turns.user_preview IS NULL THEN excluded.user_preview ELSE turns.user_preview END,"
                " user_sha=COALESCE(excluded.user_sha, turns.user_sha)",
                (turn_id, session_id, started_at or time.time(), model, platform, user_preview, user_sha),
            )

    def close_turn(
        self,
        turn_id: str,
        *,
        assistant_preview: str = "",
        assistant_sha: Optional[str] = None,
        status: str = "done",
        ended_at: Optional[float] = None,
    ) -> None:
        if not turn_id:
            return
        with self._tx():
            self._conn.execute(
                "UPDATE turns SET ended_at=?, assistant_preview=?, assistant_sha=?, status=? WHERE id=?",
                (ended_at or time.time(), assistant_preview, assistant_sha, status, turn_id),
            )

    def get_turn(self, turn_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return self._row(row)

    def recent_turns(self, session_id: str, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY started_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    # ------------------------------------------------------------------- nodes
    def upsert_node(
        self,
        node_id: str,
        kind: str,
        *,
        label: Optional[str] = None,
        path: Optional[str] = None,
        url: Optional[str] = None,
        sha256: Optional[str] = None,
        size: Optional[int] = None,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        produced: Optional[bool] = None,
        consumed: Optional[bool] = None,
        meta: Optional[dict] = None,
        created_at: Optional[float] = None,
        overwrite_ids: bool = False,
        touch: bool = True,
    ) -> dict:
        """Create or merge a node. ``session_id``/``turn_id``/``tool_call_id`` on an existing node are kept
        unless ``overwrite_ids`` is set (a write re-attributes a file; a read never does). ``touch=False``
        merges bookkeeping (a verify result) without moving ``updated_at``, which the UI shows as
        "written … ago"."""
        now = time.time()
        with self._tx():
            row = self._conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO nodes(id, kind, label, path, url, sha256, size, session_id, turn_id, tool_call_id,"
                    " produced, consumed, created_at, updated_at, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node_id, kind, label, path, url, sha256, size, session_id, turn_id, tool_call_id,
                        1 if produced else 0, 1 if consumed else 0, created_at or now, now, _j(meta or {}),
                    ),
                )
            else:
                merged = _loads(row["meta"])
                merged.update(meta or {})
                if overwrite_ids:
                    ids_sql = "session_id=COALESCE(?, session_id), turn_id=COALESCE(?, turn_id), tool_call_id=COALESCE(?, tool_call_id),"
                else:
                    ids_sql = "session_id=COALESCE(session_id, ?), turn_id=COALESCE(turn_id, ?), tool_call_id=COALESCE(tool_call_id, ?),"
                self._conn.execute(
                    "UPDATE nodes SET label=COALESCE(?, label), path=COALESCE(?, path), url=COALESCE(?, url),"
                    " sha256=COALESCE(?, sha256), size=COALESCE(?, size), " + ids_sql +
                    " produced=MAX(produced, ?), consumed=MAX(consumed, ?), updated_at=?, meta=? WHERE id=?",
                    (
                        label, path, url, sha256, size, session_id, turn_id, tool_call_id,
                        1 if produced else 0, 1 if consumed else 0, now if touch else row["updated_at"], _j(merged), node_id,
                    ),
                )
            return self._row(self._conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())

    def get_node(self, node_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._row(row)

    def find_file_node(self, path: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE kind='file' AND path=? ORDER BY updated_at DESC LIMIT 1", (path,)
            ).fetchone()
        return self._row(row)

    def resolve(self, ref: str) -> Optional[dict]:
        """Find a node by id, absolute path, basename suffix, or URL."""
        node = self.get_node(ref)
        if node:
            return node
        node = self.find_file_node(ref)
        if node:
            return node
        with self._lock:
            rel = ref[2:] if ref.startswith("./") else ref
            esc = rel.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            row = self._conn.execute(
                "SELECT * FROM nodes WHERE kind='file' AND (path LIKE ? ESCAPE '\\' OR label=?)"
                " ORDER BY produced DESC, updated_at DESC LIMIT 1",
                ("%/" + esc, rel),
            ).fetchone()
            if row is None:
                row = self._conn.execute(
                    "SELECT * FROM nodes WHERE kind='url' AND url=? LIMIT 1", (ref,)
                ).fetchone()
        return self._row(row)

    def list_artifacts(
        self,
        *,
        session_id: Optional[str] = None,
        query: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM nodes WHERE kind='file' AND produced=1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id=?"
            params.append(session_id)
        if since:
            sql += " AND updated_at>=?"
            params.append(since)
        if query:
            sql += " AND (path LIKE ? OR label LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%"])
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [self._row(r) for r in rows]
        for node in out:
            node["source_count"] = self.count_edges(src=node["id"], rel="derived_from")
            node["version_count"] = self.count_versions(node["id"])
        return out

    def produced_in_session(self, session_id: str, limit: int = 200) -> list[dict]:
        """Artifacts of one session without the per-row counts (cheap enough for a hook)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM nodes WHERE kind='file' AND produced=1 AND session_id=? ORDER BY updated_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def nodes_in_turn(self, turn_id: str, kinds: Optional[Iterable[str]] = None) -> list[dict]:
        sql = "SELECT * FROM nodes WHERE turn_id=?"
        params: list[Any] = [turn_id]
        if kinds:
            ks = list(kinds)
            sql += " AND kind IN (%s)" % ",".join("?" for _ in ks)
            params.extend(ks)
        sql += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def consumed_in_session(self, session_id: str, *, since: Optional[float] = None, limit: int = 500) -> list[dict]:
        """Sources this session read, newest read first, each with the turn that read it.

        A node keeps the ids of the session that first saw it, so a page or file read again in a later
        session is found through that session's own ``read`` edges, not through the node's columns."""
        kinds = "('file','url','query','memory','skill')"
        own = ("SELECT n.*, n.turn_id AS _read_turn, n.updated_at AS _read_at FROM nodes n"
               " WHERE n.session_id=? AND n.consumed=1 AND n.kind IN " + kinds)
        via = ("SELECT n.*, e.turn_id AS _read_turn, e.created_at AS _read_at FROM edges e"
               " JOIN nodes t ON t.id=e.src AND t.kind='tool_call' AND t.session_id=?"
               " JOIN nodes n ON n.id=e.dst"
               " WHERE e.rel='read' AND n.kind IN " + kinds)
        params: list[Any] = [session_id]
        if since:
            own += " AND n.updated_at>=?"
            via += " AND e.created_at>=?"
            params = [session_id, since, session_id, since]
        else:
            params = [session_id, session_id]
        with self._lock:
            rows = self._conn.execute(own + " UNION ALL " + via, params).fetchall()
        newest: dict[str, dict] = {}
        for r in rows:
            d = self._row(r)
            read_turn, read_at = d.pop("_read_turn"), d.pop("_read_at") or 0
            prev = newest.get(d["id"])
            if prev is not None and prev["_at"] >= read_at:
                continue
            if read_turn:
                d["turn_id"] = read_turn
            d["_at"] = read_at
            newest[d["id"]] = d
        out = sorted(newest.values(), key=lambda d: d["_at"], reverse=True)[:limit]
        for d in out:
            d.pop("_at", None)
        return out

    # ------------------------------------------------------------------- edges
    def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        *,
        basis: str = "observed",
        confidence: Optional[float] = None,
        turn_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        meta: Optional[dict] = None,
        session_id: Optional[str] = None,  # accepted for call-site symmetry; edges are keyed by nodes
    ) -> None:
        del session_id
        if not src or not dst or src == dst:
            return
        if basis not in BASIS_RANK:
            basis = "inferred"
        if confidence is None:
            confidence = {"observed": 1.0, "evidenced": 0.9, "inferred": 0.5}[basis]
        now = time.time()
        meta = dict(meta or {})
        with self._tx():
            if rel == "derived_from":
                # Snapshot the source as it is *now*, when the artifact is linked to it. The source node
                # keeps moving (a later session may rewrite the file); verify and the graph compare
                # against this snapshot, not against the node's latest state.
                dst_row = self._conn.execute("SELECT sha256, size, updated_at, meta FROM nodes WHERE id=?", (dst,)).fetchone()
                meta["linked_at"] = now
                if dst_row is not None:
                    sha, size = dst_row["sha256"], dst_row["size"]
                    # A produced file keeps its last *written* hash; if the agent read it since and saw
                    # other bytes (someone edited it), the artifact was built from what it read.
                    dm = _loads(dst_row["meta"])
                    if dm.get("last_read_sha256") and (dm.get("last_read_at") or 0) > (dst_row["updated_at"] or 0):
                        sha, size = dm["last_read_sha256"], dm.get("last_read_size", size)
                    if sha:
                        meta["src_sha256"], meta["src_size"] = sha, size
            row = self._conn.execute(
                "SELECT id, basis, confidence, meta FROM edges WHERE src=? AND dst=? AND rel=?", (src, dst, rel)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO edges(src, dst, rel, basis, confidence, turn_id, tool_call_id, created_at, meta)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (src, dst, rel, basis, confidence, turn_id, tool_call_id, now, _j(meta)),
                )
            elif BASIS_RANK[basis] > BASIS_RANK.get(row["basis"], 0) or (
                basis == row["basis"] and (confidence or 0) > (row["confidence"] or 0)
            ):
                merged = _loads(row["meta"])
                merged.update(meta)
                self._conn.execute(
                    "UPDATE edges SET basis=?, confidence=?, turn_id=COALESCE(?, turn_id),"
                    " tool_call_id=COALESCE(?, tool_call_id), meta=? WHERE id=?",
                    (basis, confidence, turn_id, tool_call_id, _j(merged), row["id"]),
                )
            elif rel == "derived_from":
                # Same link made again (the artifact was rewritten): refresh the snapshot only.
                merged = _loads(row["meta"])
                merged.update({k: v for k, v in meta.items() if k in ("linked_at", "src_sha256", "src_size")})
                self._conn.execute("UPDATE edges SET meta=? WHERE id=?", (_j(merged), row["id"]))

    def edges_from(self, src: str, rel: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM edges WHERE src=?", [src]
        if rel:
            sql += " AND rel=?"
            params.append(rel)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def edges_to(self, dst: str, rel: Optional[str] = None) -> list[dict]:
        sql, params = "SELECT * FROM edges WHERE dst=?", [dst]
        if rel:
            sql += " AND rel=?"
            params.append(rel)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row(r) for r in rows]

    def count_edges(self, *, src: Optional[str] = None, dst: Optional[str] = None, rel: Optional[str] = None) -> int:
        sql, params, clauses = "SELECT COUNT(*) FROM edges", [], []
        if src:
            clauses.append("src=?")
            params.append(src)
        if dst:
            clauses.append("dst=?")
            params.append(dst)
        if rel:
            clauses.append("rel=?")
            params.append(rel)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            return int(self._conn.execute(sql, params).fetchone()[0])

    def clear_edges(self, src: str, rel: str) -> int:
        """Drop every *rel* edge out of *src* (an artifact's sources are re-derived for each new version)."""
        with self._tx():
            cur = self._conn.execute("DELETE FROM edges WHERE src=? AND rel=?", (src, rel))
            return cur.rowcount or 0

    def nodes_with_sha(self, sha256: str, *, exclude: str, kind: str = "file", limit: int = 3) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM nodes WHERE kind=? AND sha256=? AND id<>? ORDER BY updated_at DESC LIMIT ?",
                (kind, sha256, exclude, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    # ---------------------------------------------------------------- versions
    def add_version(
        self,
        node_id: str,
        *,
        sha256: Optional[str],
        size: Optional[int],
        tool_call_id: Optional[str],
        turn_id: Optional[str],
        created_at: Optional[float] = None,
    ) -> bool:
        """Record a new content version; returns False when the hash is unchanged from the last one."""
        with self._tx():
            last = self._conn.execute(
                "SELECT sha256 FROM versions WHERE node_id=? ORDER BY created_at DESC, id DESC LIMIT 1", (node_id,)
            ).fetchone()
            if last is not None and last["sha256"] == sha256 and sha256 is not None:
                return False
            self._conn.execute(
                "INSERT INTO versions(node_id, sha256, size, tool_call_id, turn_id, created_at) VALUES(?,?,?,?,?,?)",
                (node_id, sha256, size, tool_call_id, turn_id, created_at or time.time()),
            )
            return True

    def versions(self, node_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM versions WHERE node_id=? ORDER BY created_at ASC, id ASC", (node_id,)
            ).fetchall()
        return [self._row(r) for r in rows]

    def count_versions(self, node_id: str) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM versions WHERE node_id=?", (node_id,)).fetchone()[0])

    # ------------------------------------------------------------------- graph
    PRODUCER_RELS = frozenset({"wrote", "invoked", "returned", "delegated_by", "spawned_in"})

    @staticmethod
    def edge_time(edge: dict) -> float:
        """When an edge last held: ``linked_at`` for a re-made derived_from link, else ``created_at``."""
        return float((edge.get("meta") or {}).get("linked_at") or edge.get("created_at") or 0)

    def neighborhood(self, node_id: str, *, depth: int = 6, direction: str = "provenance", limit: int = 400) -> dict:
        """Nodes and edges reachable from *node_id*.

        * ``'provenance'`` (default): everything that explains the node — its out-edges (sources, agent,
          turn) plus the in-edges that *made* it (the tool call that wrote it, the model call that
          invoked that tool call, the query that returned a URL), recursively. The walk is *as of* the
          moment each source was linked: a source rewritten after the artifact was built shows as it
          was then (its later writer is not pulled in, and it is not re-labelled an artifact).
        * ``'up'`` follows out-edges only; ``'down'`` follows in-edges only (what was built from this);
          ``'both'`` follows everything.
        """
        slack = 1.0
        as_of: dict[str, float] = {}  # node -> latest edge time that can explain it
        seen: set[str] = {node_id}
        frontier = [node_id]
        edges: dict[int, dict] = {}
        snapshot: dict[str, dict] = {}  # source node -> derived_from edge meta from the root
        for _ in range(max(depth, 0)):
            if not frontier or len(seen) >= limit:
                break
            nxt: list[str] = []
            for nid in frontier:
                cutoff = as_of.get(nid)
                rows: list[dict] = []
                if direction in ("up", "both", "provenance"):
                    rows.extend(self.edges_from(nid))
                if direction in ("down", "both"):
                    rows.extend(self.edges_to(nid))
                if direction == "provenance":
                    rows.extend(e for e in self.edges_to(nid) if e["rel"] in self.PRODUCER_RELS)
                for e in rows:
                    if direction == "provenance" and cutoff is not None and (e.get("created_at") or 0) > cutoff + slack:
                        continue  # happened after this node fed the artifact
                    edges[e["id"]] = e
                    other = e["dst"] if e["src"] == nid else e["src"]
                    if direction == "provenance":
                        t = self.edge_time(e) if (e["src"] == nid and e["rel"] == "derived_from") else cutoff
                        if t is not None:
                            as_of[other] = min(as_of.get(other, t), t)
                        if nid == node_id and e["rel"] == "derived_from":
                            snapshot[other] = e.get("meta") or {}
                    if other not in seen:
                        seen.add(other)
                        nxt.append(other)
            frontier = nxt
        nodes = []
        for nid in seen:
            n = self.get_node(nid)
            if not n:
                continue
            if direction == "provenance" and nid != node_id and n.get("kind") == "file" and n.get("produced"):
                cutoff = as_of.get(nid)
                writers = self.edges_to(nid, "wrote")
                if cutoff is not None and writers and all((w.get("created_at") or 0) > cutoff + slack for w in writers):
                    n = dict(n, produced=0, meta={k: v for k, v in (n.get("meta") or {}).items() if k != "producer_tool"})
            snap = snapshot.get(nid)
            if snap and snap.get("src_sha256") and snap["src_sha256"] != n.get("sha256"):
                n = dict(n, sha256=snap["src_sha256"], size=snap.get("src_size", n.get("size")), as_of=snap.get("linked_at"))
            nodes.append(n)
        return {"root": node_id, "nodes": nodes, "edges": list(edges.values())}

    def stats(self) -> dict:
        with self._lock:
            c = self._conn
            counts = {
                k: int(c.execute(f"SELECT COUNT(*) FROM nodes WHERE kind='{k}'").fetchone()[0])
                for k in ("file", "url", "query", "tool_call", "model_call", "aux_call", "agent")
            }
            counts["artifacts"] = int(c.execute("SELECT COUNT(*) FROM nodes WHERE kind='file' AND produced=1").fetchone()[0])
            counts["edges"] = int(c.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
            counts["sessions"] = int(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            counts["turns"] = int(c.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
            basis = {
                r["basis"]: int(r["n"]) for r in c.execute("SELECT basis, COUNT(*) AS n FROM edges GROUP BY basis")
            }
            last = c.execute("SELECT MAX(updated_at) FROM nodes").fetchone()[0]
        try:
            db_bytes = self.path.stat().st_size
        except OSError:
            db_bytes = None
        return {"counts": counts, "edges_by_basis": basis, "last_activity": last, "db_bytes": db_bytes, "db_path": str(self.path)}

    def prune(self, before: float) -> dict:
        """Delete sessions (and what only they used) that ended before *before*.

        A node belongs to the session that first saw it, but a later session may still read it or cite
        it: a node linked to anything outside the pruned sessions is handed to the newest such session
        and kept, with its versions."""
        with self._tx():
            sess = [r["id"] for r in self._conn.execute(
                "SELECT id FROM sessions WHERE COALESCE(ended_at, started_at) < ?", (before,)
            )]
            pruned = set(sess)
            removed_nodes = kept = 0
            for sid in sess:
                ids = [r["id"] for r in self._conn.execute("SELECT id FROM nodes WHERE session_id=?", (sid,))]
                for nid in ids:
                    heir = self._conn.execute(
                        "SELECT o.session_id AS sid FROM edges e JOIN nodes o ON o.id = CASE WHEN e.src=? THEN e.dst ELSE e.src END"
                        " WHERE (e.src=? OR e.dst=?) AND o.session_id IS NOT NULL AND o.session_id NOT IN (%s)"
                        " ORDER BY o.updated_at DESC LIMIT 1" % ",".join("?" for _ in pruned),
                        (nid, nid, nid, *pruned),
                    ).fetchone()
                    if heir is not None:
                        self._conn.execute("UPDATE nodes SET session_id=? WHERE id=?", (heir["sid"], nid))
                        kept += 1
                        continue
                    self._conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (nid, nid))
                    self._conn.execute("DELETE FROM versions WHERE node_id=?", (nid,))
                    self._conn.execute("DELETE FROM nodes WHERE id=?", (nid,))
                    removed_nodes += 1
                self._conn.execute("DELETE FROM turns WHERE session_id=?", (sid,))
                self._conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        try:
            with self._lock:
                self._conn.execute("VACUUM")
        except sqlite3.DatabaseError:
            pass
        return {"sessions": len(sess), "nodes": removed_nodes, "kept_shared": kept}

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        if "meta" in d:
            d["meta"] = _loads(d.get("meta"))
        return d


__all__ = ["Store", "default_data_dir", "BASIS_RANK"]
