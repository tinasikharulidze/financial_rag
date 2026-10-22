"""The direction of a document (e.g. model risk management), chosen by a person when uploading.

Used only to organise the app (filters and groups on the documents page, the analysis page and the task board).
One direction per document, inherited by all its versions. It is independent of the profile's ``domains`` (which
the model proposes and which decide what the check looks at): nothing in the check reads this table.

The table is created on first use. At that moment every existing document gets its first profile domain as a
starting tag (source "initial"), so documents uploaded before this feature are not left without one.
"""
from __future__ import annotations

import json

from kg.config import now_iso
from kg.services.permissions import require

_TABLE = """CREATE TABLE IF NOT EXISTS document_directions (
    family_id TEXT PRIMARY KEY, direction TEXT NOT NULL, source TEXT NOT NULL, actor INTEGER, ts TEXT NOT NULL)"""


def _ensure(store):
    if getattr(store, "_doc_directions_ok", False):
        return
    new = store.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_directions'") is None
    store.x(_TABLE)
    if new:
        for f in store.q("SELECT id FROM documents"):
            p = store.q1("SELECT pr.domains FROM doc_profiles pr JOIN document_versions dv ON dv.id=pr.doc_version_id "
                         "WHERE dv.family_id=? AND pr.domains IS NOT NULL ORDER BY dv.id DESC LIMIT 1", (f["id"],))
            doms = json.loads(p["domains"] or "[]") if p else []
            if doms:
                store.x("INSERT OR IGNORE INTO document_directions(family_id,direction,source,actor,ts) "
                        "VALUES(?,?,?,?,?)", (f["id"], doms[0], "initial", None, now_iso()))
    store._doc_directions_ok = True


def get_directions(store, family_ids=None) -> dict[str, str]:
    """{family id: direction} (families without a tag are missing)."""
    _ensure(store)
    rows = store.q("SELECT family_id, direction FROM document_directions")
    out = {r["family_id"]: r["direction"] for r in rows}
    return out if family_ids is None else {k: v for k, v in out.items() if k in set(family_ids)}


def direction_of_version(store, version_id: str) -> str | None:
    _ensure(store)
    r = store.q1("SELECT dd.direction FROM document_versions dv JOIN document_directions dd ON dd.family_id=dv.family_id "
                 "WHERE dv.id=?", (version_id,))
    return r["direction"] if r else None


def set_direction(store, actor: dict, family_id: str, direction: str) -> None:
    """Tag a document (all its versions) with a direction; recorded as a ``set_direction`` decision."""
    from kg.services.decisions import record_decision
    f = store.q1("SELECT id, company_id FROM documents WHERE id=?", (family_id,))
    if f is None:
        raise KeyError(f"document not found: {family_id}")
    require(actor, "decision.make", f["company_id"])
    if not (direction or "").strip():
        raise ValueError("a direction is required")
    _ensure(store)
    old = get_directions(store, [family_id]).get(family_id)
    store.x("INSERT OR REPLACE INTO document_directions(family_id,direction,source,actor,ts) VALUES(?,?,?,?,?)",
            (family_id, direction, "user", actor["id"], now_iso()))
    if old != direction:
        record_decision(store, actor, "set_direction", data={"family": family_id, "from": old, "to": direction})
