"""Human-readable names for graph ids. The ids stay the machine's business (tools, citations, audit); the people see
``node_label(...)``: what it is, a short title, and a one-line description."""
from __future__ import annotations

from typing import Any

from kg.ids import display_path


def _cut(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def act_short(acts: dict | None, act: Any) -> str:
    """The short name of an act from ``configs/kg.yaml`` (keys may be ints or strings there)."""
    by_str = {str(k): v for k, v in (acts or {}).items()}
    return str((by_str.get(str(act)) or {}).get("short") or act or "")


def _doc_row(store, version_id: str) -> dict | None:
    return store.q1("SELECT dv.version, dv.status, d.code, d.title FROM document_versions dv "
                    "JOIN documents d ON d.id=dv.family_id WHERE dv.id=?", (version_id,))


def node_label(store, node_id: str, acts: dict | None = None) -> dict:
    """{'type': Document | Obligation | Provision | Clause | Act | Amendment | Other, 'title': str, 'sub': str}"""
    if str(node_id).startswith("prop:") and str(node_id)[5:].isdigit():
        pr = store.q1("SELECT p.heading, p.proposed_text, p.kind, d.code FROM proposals p JOIN document_versions dv "
                      "ON dv.id=p.doc_version_id JOIN documents d ON d.id=dv.family_id WHERE p.id=?",
                      (int(str(node_id)[5:]),))
        if pr:
            what = pr["heading"] or _cut(pr["proposed_text"], 50)
            return {"type": "Proposal", "title": f"{pr['code']} · {what}", "sub": _cut(pr["proposed_text"], 110)}
    doc = _doc_row(store, node_id)
    if doc:
        return {"type": "Document", "title": f"{doc['code']} · v{doc['version']}", "sub": _cut(doc["title"], 100)}
    n = store.get_node(node_id)
    if not n:
        return {"type": "Other", "title": str(node_id), "sub": ""}
    p, t = n.get("props") or {}, n["type"]
    if t == "Obligation":
        where = display_path(p["source_path"]) if p.get("source_path") else ""
        title = " · ".join(x for x in (act_short(acts, p.get("act")), where) if x) or "Obligation"
        return {"type": "Obligation", "title": title, "sub": _cut(p.get("statement"), 120)}
    if t in ("ProvisionVersion", "Provision"):
        where = p.get("display") or (display_path(p["path"]) if p.get("path") else "")
        title = " · ".join(x for x in (act_short(acts, p.get("act")), where) if x) or "Provision"
        sub = _cut(p.get("heading") or p.get("title") or p.get("text"), 110)
        if t == "ProvisionVersion" and n.get("valid_from"):
            sub = (sub + " " if sub else "") + f"({n['valid_from']} →)"
        return {"type": "Provision", "title": title, "sub": sub}
    if t == "Clause":
        d = _doc_row(store, p.get("doc") or "")
        code = d["code"] if d else ""
        title = " · ".join(x for x in (code, f"§ {p['section_no']}" if p.get("section_no") else "") if x) or "Clause"
        return {"type": "Clause", "title": title, "sub": _cut(p.get("text"), 110)}
    if t == "Act":
        return {"type": "Act", "title": _cut(p.get("title") or act_short(acts, p.get("doc_id")), 80), "sub": ""}
    if t == "AmendingAct":
        return {"type": "Amendment", "title": f"№ {p.get('number')}" if p.get("number") else n["id"],
                "sub": _cut(p.get("title"), 100)}
    return {"type": "Other", "title": str(p.get("title") or p.get("name") or n["id"])[:80], "sub": ""}


def flag_card(store, scope, version_id: str, obligation_id: str, acts: dict | None = None) -> dict | None:
    """What the flag popup already shows, for the top of the chat: the obligation, the stored verdict and reasoning.
    Only for a document version the actor may see."""
    if not scope.doc_version_ok(version_id):
        return None
    ob = store.get_node(obligation_id)
    if not ob or ob["type"] != "Obligation":
        return None
    lab = node_label(store, obligation_id, acts)
    cov = store.q1("SELECT verdict, missing, rationale FROM coverage WHERE doc_version_id=? AND obligation_id=?",
                   (version_id, obligation_id))
    return {"obligation": lab, "document": node_label(store, version_id, acts),
            "verdict": cov["verdict"] if cov else None, "missing": _cut(cov and cov["missing"], 300) if cov else "",
            "rationale": _cut(cov and cov["rationale"], 500) if cov else ""}
