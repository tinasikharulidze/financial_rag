"""The final wording of one place of the proposed document.

A *slot* is one place that proposals change:

    clause:<clause id>      a clause changed by one or more ``adjust_clause`` proposals
    add:<proposal id>       a section added by an ``add_section`` proposal
    new:<proposal id>       a ``new_document`` proposal

The proposals keep the model's wording. When several proposals change the same clause they are merged into ONE
wording (the ``merge`` step, or a word-level preview), and the user may correct any wording by hand. That text is
stored here with the set of proposals it covers; it is used only while that set is still the active one (a
proposal rejected later makes the stored text stale, and the page falls back to the proposals again).

    source: "merge"   written by the merge step (model)
            "preview" the word-level preview the user accepted as it was shown
            "user"    typed by the user
"""
from __future__ import annotations

import difflib
import json

from kg.config import now_iso

_TABLE = """CREATE TABLE IF NOT EXISTS clause_edits (
    doc_version_id TEXT NOT NULL, slot TEXT NOT NULL, text TEXT NOT NULL, proposal_ids TEXT NOT NULL DEFAULT '[]',
    notes TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL, actor INTEGER, ts TEXT NOT NULL,
    PRIMARY KEY (doc_version_id, slot))"""


def _ensure(store):
    if not getattr(store, "_clause_edits_ok", False):
        store.x(_TABLE)
        store._clause_edits_ok = True


def slot_of(p: dict) -> str:
    """The slot a proposals row belongs to."""
    if p["kind"] == "adjust_clause" and p.get("clause_id"):
        return f"clause:{p['clause_id']}"
    return ("add:" if p["kind"] == "add_section" else "new:") + str(p["id"])


def get_edits(store, version_id: str) -> dict[str, dict]:
    """{slot: {"text", "proposal_ids": [...], "notes": {...}, "source", "actor", "ts"}}"""
    _ensure(store)
    out = {}
    for r in store.q("SELECT * FROM clause_edits WHERE doc_version_id=?", (version_id,)):
        out[r["slot"]] = {**r, "proposal_ids": sorted(json.loads(r["proposal_ids"] or "[]")),
                          "notes": json.loads(r["notes"] or "{}")}
    return out


def valid_edit(edits: dict, slot: str, proposal_ids) -> dict | None:
    """The stored text of ``slot`` if it was made for exactly these proposals, else None (stale or missing)."""
    e = edits.get(slot)
    return e if e and e["proposal_ids"] == sorted(int(i) for i in proposal_ids) else None


def save_edit(store, actor: dict | None, version_id: str, slot: str, text: str, proposal_ids, source: str,
              notes: dict | None = None) -> None:
    _ensure(store)
    store.x("INSERT OR REPLACE INTO clause_edits(doc_version_id,slot,text,proposal_ids,notes,source,actor,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (version_id, slot, text.strip(), json.dumps(sorted(int(i) for i in proposal_ids)),
             json.dumps(notes or {}, ensure_ascii=False), source, (actor or {}).get("id"), now_iso()))


def drop_edit(store, version_id: str, slot: str) -> None:
    _ensure(store)
    store.x("DELETE FROM clause_edits WHERE doc_version_id=? AND slot=?", (version_id, slot))


# ------------------------------------------------------------------------------------------- word-level preview merge
def merge_words(original: str, proposals: list[tuple[str, str]]) -> dict:
    """Apply several rewrites of the same text at once, word by word (no model; used until the merge step ran).

    ``proposals`` = [(key, new text)]. Changes at different places are all applied; changes at the SAME place are
    combined (a change contained in another is dropped, otherwise both are kept one after the other) and reported
    in ``overlap``. -> {"text", "segments": [(kind, text, [keys])], "overlap": bool}; kind = equal | ins | del."""
    x = (original or "").split()
    edits = []
    for key, new in proposals:
        y = (new or "").split()
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
            if op != "equal":
                edits.append({"i1": i1, "i2": i2, "repl": y[j1:j2], "keys": [key]})
    edits.sort(key=lambda e: (e["i1"], e["i2"]))
    groups, overlap = [], False
    for e in edits:
        g = groups[-1] if groups else None
        same_point = g is not None and g["i1"] == g["i2"] == e["i1"] == e["i2"]
        if g is not None and (e["i1"] < g["i2"] or same_point):
            a, b = " ".join(g["repl"]), " ".join(e["repl"])
            if b and b not in a:
                if a and a in b:
                    g["repl"] = list(e["repl"])
                else:
                    g["repl"] = g["repl"] + e["repl"]
                    overlap = overlap or bool(a)
            g["i2"] = max(g["i2"], e["i2"])
            g["keys"] = g["keys"] + [k for k in e["keys"] if k not in g["keys"]]
        else:
            groups.append({**e, "repl": list(e["repl"]), "keys": list(e["keys"])})
    segs, pos = [], 0
    for g in groups:
        if g["i1"] > pos:
            segs.append(("equal", " ".join(x[pos:g["i1"]]), []))
        if g["i2"] > g["i1"]:
            segs.append(("del", " ".join(x[g["i1"]:g["i2"]]), g["keys"]))
        if g["repl"]:
            segs.append(("ins", " ".join(g["repl"]), g["keys"]))
        pos = max(pos, g["i2"])
    if pos < len(x):
        segs.append(("equal", " ".join(x[pos:]), []))
    text = " ".join(t for k, t, _ in segs if k != "del")
    return {"text": text, "segments": segs, "overlap": overlap}


def diff_segments(original: str, new: str) -> list[tuple[str, str, list]]:
    """Word diff of one text against the original, in the same shape as ``merge_words`` segments."""
    x, y = (original or "").split(), (new or "").split()
    segs = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
        if op == "equal":
            segs.append(("equal", " ".join(y[j1:j2]), []))
            continue
        if i2 > i1:
            segs.append(("del", " ".join(x[i1:i2]), []))
        if j2 > j1:
            segs.append(("ins", " ".join(y[j1:j2]), []))
    return segs
