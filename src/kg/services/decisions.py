"""User decisions (plan section 10). Each one is a row in ``user_decisions`` AND takes effect immediately in code.

    accept            task -> accepted (the proposal goes into the revised text)
    not_relevant      task -> not_relevant, triage row = irrelevant (source user); a reason is required
    already_addressed task -> already_addressed, the chosen clauses become user COVERED_BY edges (role full),
                      coverage verdict = full (source user); clauses are required
    edge_add/change/remove   user COVERED_BY edges; the verdict is recomputed by a RULE (no LLM); "re-check" is a
                      separate call of the judge (``pipeline.recheck_obligation``)
Precedence everywhere: user > rule > llm. The pipeline never writes over user rows.
"""
from __future__ import annotations

import json

from kg.config import now_iso
from kg.mapping.judging import final_verdict
from kg.services import tasks as T
from kg.services.permissions import PermissionDenied, require
from kg.store.v2tables import jdump

USER_ROLES = ("full", "partial", "contradicts")


class DecisionError(ValueError):
    pass


def record_decision(store, actor: dict, kind: str, *, doc_version_id: str | None = None,
                    obligation_id: str | None = None, proposal_id: int | None = None, clause_id: str | None = None,
                    data: dict | None = None) -> int:
    """Append a row to ``user_decisions`` (who, what, on what, when). Returns its id."""
    return store.x("INSERT INTO user_decisions(actor,kind,doc_version_id,obligation_id,proposal_id,clause_id,data,ts) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (actor["id"], kind, doc_version_id, obligation_id, proposal_id, clause_id,
                    json.dumps(data or {}, ensure_ascii=False, default=str), now_iso()))


def _own_task(store, actor: dict, key: str) -> dict:
    t = T.get_task(store, actor, key)
    require(actor, "decision.make", t["company_id"])
    return t


def _set_status(store, actor: dict, t: dict, status: str, data: dict | None = None):
    store.x("UPDATE tasks SET status=?, updated_at=? WHERE key=?", (status, now_iso(), t["key"]))
    T.event(store, t["key"], actor["id"], "status_change", {"from": t["status"], "to": status, **(data or {})})


def _clause_ok(store, version_id: str, clause_id: str):
    n = store.get_node(clause_id)
    if n is None or n["type"] != "Clause" or n["props"].get("doc") != version_id:
        raise DecisionError(f"{clause_id} is not a clause of this document version")


# ------------------------------------------------------------------------------------------------ task decisions
def accept(store, actor: dict, key: str) -> dict:
    t = _own_task(store, actor, key)
    if t["status"] != "open":
        raise DecisionError("only an open task can be accepted")
    if not t["proposal_id"]:
        raise DecisionError("the task has no proposal")
    _set_status(store, actor, t, "accepted")
    record_decision(store, actor, "accept", doc_version_id=t["doc_version_id"], obligation_id=t["obligation_id"],
                    proposal_id=t["proposal_id"])
    return T.get_task(store, actor, key)


def not_relevant(store, actor: dict, key: str, reason: str) -> dict:
    t = _own_task(store, actor, key)
    if not (reason or "").strip():
        raise DecisionError("a reason is required")
    if t["status"] not in T.OPEN:
        raise DecisionError("only an open or accepted task can be dismissed")
    store.x("INSERT OR REPLACE INTO triage(doc_version_id,obligation_id,relevance,reason,citations,llm_output_id,"
            "source) VALUES(?,?,?,?,?,?,?)",
            (t["doc_version_id"], t["obligation_id"], "irrelevant", reason.strip(), "[]", None, "user"))
    _set_status(store, actor, t, "not_relevant", {"reason": reason.strip()})
    record_decision(store, actor, "not_relevant", doc_version_id=t["doc_version_id"],
                    obligation_id=t["obligation_id"], proposal_id=t["proposal_id"], data={"reason": reason.strip()})
    return T.get_task(store, actor, key)


def already_addressed(store, actor: dict, key: str, clause_ids: list[str]) -> dict:
    t = _own_task(store, actor, key)
    if not clause_ids:
        raise DecisionError("choose the clauses that address the obligation")
    if t["status"] not in T.OPEN:
        raise DecisionError("only an open or accepted task can be closed this way")
    for c in clause_ids:
        _clause_ok(store, t["doc_version_id"], c)
    v, oid = t["doc_version_id"], t["obligation_id"]
    for c in clause_ids:
        _put_user_edge(store, v, oid, c, "full")
    store.x("INSERT OR REPLACE INTO coverage(doc_version_id,obligation_id,verdict,missing,rationale,citations,cause,"
            "amendment_id,llm_output_id,source,invalid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (v, oid, "full", None, "მომხმარებელმა დაადასტურა, რომ მოთხოვნა უკვე ასახულია.", "[]", t["cause"], None,
             None, "user", 0))
    _set_status(store, actor, t, "already_addressed", {"clauses": clause_ids})
    record_decision(store, actor, "already_addressed", doc_version_id=v, obligation_id=oid,
                    proposal_id=t["proposal_id"], data={"clauses": clause_ids})
    return T.get_task(store, actor, key)


def reopen(store, actor: dict, key: str) -> dict:
    """Undo accept / not_relevant / already_addressed. The next mapping run re-checks the obligation."""
    t = _own_task(store, actor, key)
    if t["status"] in ("open", "replaced"):
        raise DecisionError("nothing to reopen")
    v, oid = t["doc_version_id"], t["obligation_id"]
    if t["status"] == "not_relevant":
        store.x("DELETE FROM triage WHERE doc_version_id=? AND obligation_id=? AND source='user'", (v, oid))
    elif t["status"] == "already_addressed":
        last = store.q1("SELECT data FROM user_decisions WHERE doc_version_id=? AND obligation_id=? "
                        "AND kind='already_addressed' ORDER BY id DESC LIMIT 1", (v, oid))
        for c in (json.loads(last["data"]).get("clauses", []) if last else []):
            store.delete_edge_ids([_edge_id(oid, c, "user")])
        store.x("DELETE FROM coverage WHERE doc_version_id=? AND obligation_id=? AND source='user'", (v, oid))
    _set_status(store, actor, t, "open", {"reopened": True})
    record_decision(store, actor, "reopen", doc_version_id=v, obligation_id=oid, proposal_id=t["proposal_id"])
    return T.get_task(store, actor, key)


# ------------------------------------------------------------------------------------------------ edges
def _edge_id(oid: str, clause_id: str, source: str) -> str:
    return f"{oid}|COVERED_BY|{clause_id}|{source}"


def _put_user_edge(store, version_id: str, oid: str, clause_id: str, role: str):
    store.delete_edge_ids([_edge_id(oid, clause_id, "llm")])         # the user's word replaces the model's edge
    store.upsert_edges([(oid, clause_id, "COVERED_BY",
                         {"role": role, "coverage": role, "source": "user", "doc_version": version_id}, None, None,
                         _edge_id(oid, clause_id, "user"))])


def edges_of(store, version_id: str, oid: str) -> list[dict]:
    return [{"clause_id": e["dst"], **e["props"]} for e in store.edges("COVERED_BY")
            if e["src"] == oid and e["props"].get("doc_version") == version_id]


def recompute_verdict(store, version_id: str, oid: str) -> str | None:
    """Rule-based verdict after an edge edit (no LLM): see the module docstring of ``judging.final_verdict``."""
    row = store.q1("SELECT * FROM coverage WHERE doc_version_id=? AND obligation_id=?", (version_id, oid))
    if row is None:
        return None
    es = [e for e in edges_of(store, version_id, oid) if e["role"] in USER_ROLES]
    if not es:
        verdict = "none"
    else:
        base = row["verdict"] if row["verdict"] in ("full", "partial") else "partial"
        verdict = final_verdict(base, es)
    missing = None if verdict == "full" else (row["missing"] or "ზოგიერთი ელემენტი ჯერ არ არის ასახული.")
    store.x("UPDATE coverage SET verdict=?, missing=?, invalid=0 WHERE doc_version_id=? AND obligation_id=?",
            (verdict, missing, version_id, oid))
    return verdict


def _edit_edge(cfg, store, run, actor: dict, version_id: str, oid: str, clause_id: str, role: str | None, kind: str):
    v = store.q1("SELECT d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                 "WHERE dv.id=?", (version_id,))
    if v is None:
        raise DecisionError("unknown document version")
    require(actor, "decision.make", v["company_id"])
    _clause_ok(store, version_id, clause_id)
    if store.get_node(oid) is None or not store.q1("SELECT 1 FROM coverage WHERE doc_version_id=? AND obligation_id=?",
                                                    (version_id, oid)):
        raise DecisionError("the obligation has no coverage row in this document version")
    if role is None:                                                  # remove
        store.delete_edge_ids([_edge_id(oid, clause_id, "llm"), _edge_id(oid, clause_id, "user")])
        store.upsert_edges([(oid, clause_id, "COVERED_BY", {"role": "rejected", "coverage": "rejected",
                             "source": "user", "doc_version": version_id}, None, None, _edge_id(oid, clause_id, "user"))])
    else:
        if role not in USER_ROLES:
            raise DecisionError(f"role must be one of {USER_ROLES}")
        _put_user_edge(store, version_id, oid, clause_id, role)
    verdict = recompute_verdict(store, version_id, oid)
    record_decision(store, actor, kind, doc_version_id=version_id, obligation_id=oid, clause_id=clause_id,
                    data={"role": role, "verdict": verdict})
    T.sync_tasks(cfg, store, run, version_id, actor["id"], only={oid})
    return verdict


def edge_add(cfg, store, run, actor, version_id, oid, clause_id, role):
    return _edit_edge(cfg, store, run, actor, version_id, oid, clause_id, role, "edge_add")


def edge_change(cfg, store, run, actor, version_id, oid, clause_id, role):
    return _edit_edge(cfg, store, run, actor, version_id, oid, clause_id, role, "edge_change")


def edge_remove(cfg, store, run, actor, version_id, oid, clause_id):
    return _edit_edge(cfg, store, run, actor, version_id, oid, clause_id, None, "edge_remove")


def decisions_of(store, actor: dict, version_id: str, obligation_id: str | None = None) -> list[dict]:
    """What the regulator (and the company) sees on a task: who decided what, when, why."""
    v = store.q1("SELECT d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                 "WHERE dv.id=?", (version_id,))
    if v is None:
        raise DecisionError("unknown document version")
    require(actor, "task.view", v["company_id"])
    q, args = ("SELECT d.*, u.name AS actor_name FROM user_decisions d LEFT JOIN users u ON u.id=d.actor "
               "WHERE d.doc_version_id=?"), [version_id]
    if obligation_id:
        q += " AND d.obligation_id=?"
        args.append(obligation_id)
    rows = store.q(q + " ORDER BY d.id", args)
    for r in rows:
        r["data"] = json.loads(r["data"] or "{}")
    return rows
