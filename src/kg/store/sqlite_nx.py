"""SQLite (source of truth) + NetworkX (traversal view) implementation of GraphStore.

Follows the conventions of ``src/chat_db.py``: stdlib ``sqlite3`` only, one module-level ``SCHEMA`` run with
``executescript`` in ``__init__``, a ``_tx()`` context manager per operation (no long-lived connections, so
Streamlit and CLI jobs can run side by side), light in-place migrations through ``PRAGMA table_info``,
parameterised queries only, JSON columns written with ``ensure_ascii=False``, Tbilisi-time ISO timestamps,
methods return plain dicts. No Streamlit imports, so it can be tested on its own.

Inspect the logs from a terminal, e.g.:
  sqlite3 -header -csv data/kg/kg.sqlite \
    "SELECT purpose, model, input_tokens, output_tokens, latency_ms, cache_hit, error, ts FROM llm_calls ORDER BY id" > llm_calls.csv
  sqlite3 -header -csv data/kg/kg.sqlite \
    "SELECT doc_id, version_basis, gap_class, priority, issue_chance, severity, action_type, rationale FROM findings"
  sqlite3 -header -csv data/kg/kg.sqlite "SELECT kind, message, acknowledged FROM alerts"
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import networkx as nx

from kg.config import now_iso
from kg.store.base import GraphStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    props       TEXT NOT NULL DEFAULT '{}',
    valid_from  TEXT,
    valid_to    TEXT
);
CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    type        TEXT NOT NULL,
    props       TEXT NOT NULL DEFAULT '{}',
    valid_from  TEXT,
    valid_to    TEXT
);
CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    doc_id          TEXT NOT NULL,
    clause_id       TEXT,
    obligation_id   TEXT NOT NULL,
    version_basis   TEXT,
    gap_class       TEXT,
    action_type     TEXT,
    coverage        TEXT,
    issue_chance    TEXT,
    severity        TEXT,
    priority        TEXT,
    rationale       TEXT,
    evidence_quote  TEXT,
    suggested_fix   TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    kind            TEXT NOT NULL,
    obligation_id   TEXT,
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    acknowledged    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    params      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT,
    purpose         TEXT,
    model           TEXT,
    prompt_hash     TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    latency_ms      INTEGER,
    cache_hit       INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    ts              TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_quota (
    model     TEXT NOT NULL,
    day       TEXT NOT NULL,
    requests  INTEGER NOT NULL DEFAULT 0,
    exhausted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (model, day)
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, type);
CREATE INDEX IF NOT EXISTS idx_findings_doc ON findings(doc_id);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);
"""

# columns added after the first release: table -> {column: DDL}
MIGRATIONS = {
    "findings": {"coverage": "TEXT"},
    "llm_quota": {"exhausted": "INTEGER NOT NULL DEFAULT 0"},
}


def _dumps(obj) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, default=str)


def _node(row) -> dict:
    d = dict(row)
    d["props"] = json.loads(d.get("props") or "{}")
    return d


def _valid(row_from: str | None, row_to: str | None, date: str | None) -> bool:
    if date is None:
        return True
    return (row_from is None or row_from <= date) and (row_to is None or date < row_to)


class SQLiteStore(GraphStore):
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as con:
            con.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
                for col, ddl in cols.items():
                    if col not in have:
                        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self._version = 0          # bumped on every write; invalidates the NetworkX cache
        self._nx_cache: tuple[int, nx.MultiDiGraph] | None = None

    @contextmanager
    def _tx(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------ nodes / edges
    def upsert_node(self, node_id, type, props=None, valid_from=None, valid_to=None):
        self.upsert_nodes([(node_id, type, props, valid_from, valid_to)])

    def upsert_nodes(self, rows: Iterable[tuple]):
        """rows: (id, type, props, valid_from, valid_to)."""
        with self._tx() as con:
            con.executemany(
                "INSERT INTO nodes(id,type,props,valid_from,valid_to) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET type=excluded.type, props=excluded.props, "
                "valid_from=excluded.valid_from, valid_to=excluded.valid_to",
                [(r[0], r[1], _dumps(r[2]), r[3], r[4]) for r in rows])
        self._version += 1

    def patch_node(self, node_id: str, props: dict | None = None, valid_to: str | None = ...):
        """Merge ``props`` into a node; optionally set valid_to (pass None explicitly to clear)."""
        n = self.get_node(node_id)
        if n is None:
            raise KeyError(node_id)
        merged = {**n["props"], **(props or {})}
        vt = n["valid_to"] if valid_to is ... else valid_to
        with self._tx() as con:
            con.execute("UPDATE nodes SET props=?, valid_to=? WHERE id=?", (_dumps(merged), vt, node_id))
        self._version += 1

    def upsert_edge(self, src, dst, type, props=None, valid_from=None, valid_to=None, edge_id=None):
        eid = edge_id or f"{src}|{type}|{dst}"
        with self._tx() as con:
            con.execute(
                "INSERT INTO edges(id,src,dst,type,props,valid_from,valid_to) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET props=excluded.props, valid_from=excluded.valid_from, "
                "valid_to=excluded.valid_to",
                (eid, src, dst, type, _dumps(props), valid_from, valid_to))
        self._version += 1
        return eid

    def upsert_edges(self, rows: Iterable[tuple]):
        """rows: (src, dst, type, props, valid_from, valid_to[, edge_id])."""
        prepared = []
        for r in rows:
            eid = r[6] if len(r) > 6 and r[6] else f"{r[0]}|{r[2]}|{r[1]}"
            prepared.append((eid, r[0], r[1], r[2], _dumps(r[3]), r[4], r[5]))
        with self._tx() as con:
            con.executemany(
                "INSERT INTO edges(id,src,dst,type,props,valid_from,valid_to) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET props=excluded.props, valid_from=excluded.valid_from, "
                "valid_to=excluded.valid_to", prepared)
        self._version += 1

    def delete_edges(self, type: str, src: str | None = None, dst: str | None = None,
                     dst_prefix: str | None = None, **prop_filters):
        """Delete edges of a type (used when a mapping run is recomputed)."""
        with self._tx() as con:
            rows = con.execute("SELECT id, src, dst, props FROM edges WHERE type=?", (type,)).fetchall()
            ids = []
            for r in rows:
                if src and r["src"] != src:
                    continue
                if dst and r["dst"] != dst:
                    continue
                if dst_prefix and not r["dst"].startswith(dst_prefix):
                    continue
                props = json.loads(r["props"] or "{}")
                if all(props.get(k) == v for k, v in prop_filters.items()):
                    ids.append((r["id"],))
            con.executemany("DELETE FROM edges WHERE id=?", ids)
        self._version += 1
        return len(ids)

    def edges(self, type: str | None = None) -> list[dict]:
        """All edges (optionally of one type) as dicts {src, dst, type, props, valid_from, valid_to}."""
        sql, args = "SELECT src, dst, type, props, valid_from, valid_to FROM edges", []
        if type:
            sql += " WHERE type=?"
            args.append(type)
        with self._tx() as con:
            return [{**dict(r), "props": json.loads(r["props"] or "{}")} for r in con.execute(sql, args)]

    def delete_nodes(self, ids: list[str]):
        """Delete nodes and every edge touching them."""
        with self._tx() as con:
            con.executemany("DELETE FROM edges WHERE src=? OR dst=?", [(i, i) for i in ids])
            con.executemany("DELETE FROM nodes WHERE id=?", [(i,) for i in ids])
        self._version += 1

    def purge_act(self, doc_id: str):
        """Remove the Provision / ProvisionVersion nodes of one act (used before a re-consolidation)."""
        ids = []
        for t in ("Provision", "ProvisionVersion"):
            ids += [n["id"] for n in self.nodes(t) if str(n["props"].get("act")) == str(doc_id)]
        self.delete_nodes(ids)
        return len(ids)

    def get_node(self, node_id):
        with self._tx() as con:
            row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return _node(row) if row else None

    def nodes(self, type=None, as_of=None, **filters: Any):
        sql, args = "SELECT * FROM nodes", []
        if type:
            sql += " WHERE type=?"
            args.append(type)
        with self._tx() as con:
            rows = [_node(r) for r in con.execute(sql, args)]
        out = []
        for n in rows:
            if not _valid(n["valid_from"], n["valid_to"], as_of):
                continue
            if all(n["props"].get(k, n.get(k)) == v for k, v in filters.items()):
                out.append(n)
        return out

    def neighbors(self, node_id, edge_type=None, direction="out", as_of=None):
        out = []
        with self._tx() as con:
            for d in (["out", "in"] if direction == "both" else [direction]):
                col, other = ("src", "dst") if d == "out" else ("dst", "src")
                sql = (f"SELECT e.id AS eid, e.src, e.dst, e.type AS etype, e.props AS eprops, "
                       f"e.valid_from AS efrom, e.valid_to AS eto, n.id AS nid, n.type AS ntype, n.props AS nprops, "
                       f"n.valid_from AS nfrom, n.valid_to AS nto FROM edges e JOIN nodes n ON n.id=e.{other} "
                       f"WHERE e.{col}=?")
                args = [node_id]
                if edge_type:
                    sql += " AND e.type=?"
                    args.append(edge_type)
                for r in con.execute(sql, args):
                    if not (_valid(r["efrom"], r["eto"], as_of) and _valid(r["nfrom"], r["nto"], as_of)):
                        continue
                    out.append({
                        "direction": d,
                        "edge": {"id": r["eid"], "src": r["src"], "dst": r["dst"], "type": r["etype"],
                                 "props": json.loads(r["eprops"] or "{}"), "valid_from": r["efrom"],
                                 "valid_to": r["eto"]},
                        "node": {"id": r["nid"], "type": r["ntype"], "props": json.loads(r["nprops"] or "{}"),
                                 "valid_from": r["nfrom"], "valid_to": r["nto"]}})
        return out

    # ------------------------------------------------------------------ NetworkX views
    def _full_graph(self) -> nx.MultiDiGraph:
        if self._nx_cache and self._nx_cache[0] == self._version:
            return self._nx_cache[1]
        g = nx.MultiDiGraph()
        with self._tx() as con:
            for r in con.execute("SELECT * FROM nodes"):
                n = _node(r)
                g.add_node(n["id"], type=n["type"], valid_from=n["valid_from"], valid_to=n["valid_to"], **n["props"])
            for r in con.execute("SELECT * FROM edges"):
                if r["src"] in g and r["dst"] in g:
                    g.add_edge(r["src"], r["dst"], key=r["id"], type=r["type"], valid_from=r["valid_from"],
                               valid_to=r["valid_to"], **json.loads(r["props"] or "{}"))
        self._nx_cache = (self._version, g)
        return g

    def as_of(self, date):
        g = self._full_graph()
        keep = [n for n, d in g.nodes(data=True) if _valid(d.get("valid_from"), d.get("valid_to"), date)]
        sub = g.subgraph(keep).copy()
        drop = [(u, v, k) for u, v, k, d in sub.edges(keys=True, data=True)
                if not _valid(d.get("valid_from"), d.get("valid_to"), date)]
        sub.remove_edges_from(drop)
        return sub

    def subgraph(self, ids):
        g = self._full_graph()
        return g.subgraph([i for i in ids if i in g]).copy()

    # ------------------------------------------------------------------ runs, llm usage, quota
    def start_run(self, run_id: str, kind: str, params: dict | None = None):
        with self._tx() as con:
            con.execute("INSERT OR REPLACE INTO runs(id,kind,started_at,params,status) VALUES(?,?,?,?, 'running')",
                        (run_id, kind, now_iso(), _dumps(params)))

    def finish_run(self, run_id: str, status: str = "ok"):
        with self._tx() as con:
            con.execute("UPDATE runs SET finished_at=?, status=? WHERE id=?", (now_iso(), status, run_id))

    def runs(self, kind: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM runs", []
        if kind:
            sql += " WHERE kind=?"
            args.append(kind)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self._tx() as con:
            rows = [dict(r) for r in con.execute(sql, args)]
        for r in rows:
            r["params"] = json.loads(r["params"] or "{}")
        return rows

    def log_llm_call(self, run_id, purpose, model, prompt_hash, input_tokens, output_tokens, latency_ms,
                     cache_hit=False, error=None):
        with self._tx() as con:
            con.execute(
                "INSERT INTO llm_calls(run_id,purpose,model,prompt_hash,input_tokens,output_tokens,latency_ms,"
                "cache_hit,error,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, purpose, model, prompt_hash, input_tokens, output_tokens, latency_ms,
                 1 if cache_hit else 0, error, now_iso()))

    def llm_calls(self, run_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM llm_calls", []
        if run_id:
            sql += " WHERE run_id=?"
            args.append(run_id)
        with self._tx() as con:
            return [dict(r) for r in con.execute(sql + " ORDER BY id", args)]

    def quota_used(self, model: str, day: str) -> dict:
        with self._tx() as con:
            r = con.execute("SELECT requests, exhausted FROM llm_quota WHERE model=? AND day=?", (model, day)).fetchone()
        return dict(r) if r else {"requests": 0, "exhausted": 0}

    def quota_inc(self, model: str, day: str):
        with self._tx() as con:
            con.execute("INSERT INTO llm_quota(model,day,requests) VALUES(?,?,1) "
                        "ON CONFLICT(model,day) DO UPDATE SET requests=requests+1", (model, day))

    def quota_dec(self, model: str, day: str):
        with self._tx() as con:
            con.execute("UPDATE llm_quota SET requests=MAX(0, requests-1) WHERE model=? AND day=?", (model, day))

    def quota_mark_exhausted(self, model: str, day: str):
        with self._tx() as con:
            con.execute("INSERT INTO llm_quota(model,day,requests,exhausted) VALUES(?,?,0,1) "
                        "ON CONFLICT(model,day) DO UPDATE SET exhausted=1", (model, day))

    # ------------------------------------------------------------------ findings and alerts
    def add_findings(self, rows: list[dict]):
        cols = ["run_id", "doc_id", "clause_id", "obligation_id", "version_basis", "gap_class", "action_type",
                "coverage", "issue_chance", "severity", "priority", "rationale", "evidence_quote", "suggested_fix"]
        with self._tx() as con:
            con.executemany(
                f"INSERT INTO findings({','.join(cols)},created_at) VALUES({','.join('?' * len(cols))},?)",
                [tuple(r.get(c) for c in cols) + (now_iso(),) for r in rows])

    def clear_findings(self, doc_id: str | None = None, obligation_ids: list[str] | None = None):
        with self._tx() as con:
            if doc_id and obligation_ids:
                con.executemany("DELETE FROM findings WHERE doc_id=? AND obligation_id=?",
                                [(doc_id, o) for o in obligation_ids])
            elif doc_id:
                con.execute("DELETE FROM findings WHERE doc_id=?", (doc_id,))
            else:
                con.execute("DELETE FROM findings")

    def findings(self, doc_id: str | None = None, run_id: str | None = None) -> list[dict]:
        sql, args, where = "SELECT * FROM findings", [], []
        if doc_id:
            where.append("doc_id=?")
            args.append(doc_id)
        if run_id:
            where.append("run_id=?")
            args.append(run_id)
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._tx() as con:
            return [dict(r) for r in con.execute(sql + " ORDER BY id", args)]

    def add_alert(self, run_id, kind, obligation_id, message) -> bool:
        """Insert unless an open alert with the same (kind, obligation_id) exists. Returns True if inserted."""
        with self._tx() as con:
            if con.execute("SELECT 1 FROM alerts WHERE kind=? AND obligation_id=?", (kind, obligation_id)).fetchone():
                return False
            con.execute("INSERT INTO alerts(run_id,kind,obligation_id,message,created_at) VALUES(?,?,?,?,?)",
                        (run_id, kind, obligation_id, message, now_iso()))
        return True

    def alerts(self, include_acknowledged: bool = False) -> list[dict]:
        sql = "SELECT * FROM alerts" + ("" if include_acknowledged else " WHERE acknowledged=0") + " ORDER BY id"
        with self._tx() as con:
            return [dict(r) for r in con.execute(sql)]

    def acknowledge_alert(self, alert_id: int):
        with self._tx() as con:
            con.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))

    def delete_alerts(self, kind: str):
        with self._tx() as con:
            con.execute("DELETE FROM alerts WHERE kind=? AND acknowledged=0", (kind,))
