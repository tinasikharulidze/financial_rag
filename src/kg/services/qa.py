"""UC1: question -> cited answer from the provisions in force at ``as_of``."""
from __future__ import annotations

import json

from pydantic import BaseModel

from kg.config import today_iso
from kg.ids import act_id, display_path, prov_id
from kg.llm import system_user
from kg.retrieval.index import HybridIndex, build_index, maybe_rerank

SYSTEM = """You answer questions about Georgian banking regulation. Use ONLY the numbered passages you are given.
Each passage has an id, the act, the provision and the text valid on the requested date. Answer in the language of
the question (Georgian if the question is Georgian), concisely and precisely. Every statement must be supported by
a passage; if the passages do not contain the answer, say so plainly and leave citations empty.
Answer with JSON only: {"answer": "...", "citations": ["<passage id>", ...]} where citations lists only ids
of passages you actually relied on."""

_CACHE: dict = {}


class QAOut(BaseModel):
    answer: str
    citations: list[str] = []


def _items(store, as_of: str, act: str | None) -> list[dict]:
    out = []
    for n in store.nodes("ProvisionVersion", as_of=as_of):
        p = n["props"]
        if p.get("status") != "active" or not (p.get("text") or "").strip():
            continue
        if act and str(p["act"]) != str(act):
            continue
        # index_text = act short name › article number and title › path, then label and text (KG v2 plan 5.1)
        out.append({"id": n["id"], "text": p.get("index_text") or f"{p.get('heading') or ''} {p['text']}".strip(),
                    "body": p["text"],
                    "act": str(p["act"]), "path": p["path"], "heading": p.get("heading"),
                    "valid_from": n["valid_from"]})
    return out


def get_index(cfg, store, as_of: str, act: str | None) -> HybridIndex:
    items = _items(store, as_of, act)
    key = (str(store.path) if hasattr(store, "path") else id(store), as_of, act, len(items),
           getattr(store, "_version", 0))
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = build_index(cfg, items, f"pv_{as_of}_{act or 'all'}")
    return _CACHE[key]


def _active_pv_at(store, act: str, path: str, as_of: str):
    for it in store.neighbors(prov_id(act, path), "VERSION_OF", "in"):
        n = it["node"]
        if n["valid_from"] <= as_of and (n["valid_to"] is None or as_of < n["valid_to"]) \
                and n["props"].get("status") == "active":
            return n
    return None


def _act_title(store, act: str) -> str:
    n = store.get_node(act_id(act))
    return (n["props"].get("title") or act) if n else act


def expand(store, hits: list[dict], as_of: str, limit: int = 12) -> list[dict]:
    """hits + parent article + 1-hop in-corpus REFERS_TO targets, de-duplicated."""
    ctx: dict[str, dict] = {h["id"]: dict(h, why="hit") for h in hits}

    def add(n, why):
        if n and n["id"] not in ctx and len(ctx) < limit:
            p = n["props"]
            ctx[n["id"]] = {"id": n["id"], "act": str(p["act"]), "path": p["path"], "body": p["text"],
                            "heading": p.get("heading"), "valid_from": n["valid_from"], "why": why}

    for h in hits:
        top = h["path"].split("/")[0]
        if top != h["path"]:
            add(_active_pv_at(store, h["act"], top, as_of), "parent")
        for e in store.neighbors(prov_id(h["act"], h["path"]), "REFERS_TO", "out"):
            node = e["node"]
            if node["type"] == "Provision":
                add(_active_pv_at(store, str(node["props"]["act"]), node["props"]["path"], as_of), "reference")
    return list(ctx.values())


def answer(cfg, store, llm, question: str, as_of: str | None = None, act: str | None = None) -> dict:
    as_of = as_of or today_iso()
    rc = cfg["retrieval"]
    idx = get_index(cfg, store, as_of, act)
    hits = [dict(idx.get(i), score=s) for i, s in idx.search(question, rc.get("top_k", 30))]
    hits = maybe_rerank(cfg, question, hits, rc.get("keep", 6))
    ctx = expand(store, hits, as_of)
    passages = [{"id": c["id"], "act": _act_title(store, c["act"]), "provision": display_path(c["path"]),
                 "text": (c["body"] or "")[:1800]} for c in ctx]
    user = f"Date: {as_of}\nQuestion: {question}\n\nPassages:\n" + json.dumps(passages, ensure_ascii=False, indent=1)
    res = llm.call("qa_answer", system_user(SYSTEM, user), QAOut, max_tokens=2048)
    allowed = {c["id"] for c in ctx}
    cites = []
    for cid in res.parsed.citations:
        if cid in allowed and cid not in [c["id"] for c in cites]:
            c = next(x for x in ctx if x["id"] == cid)
            cites.append({"id": cid, "act": c["act"], "act_title": _act_title(store, c["act"]),
                          "path": c["path"], "provision": display_path(c["path"]), "valid_from": c["valid_from"],
                          "text": c["body"]})
    dropped = [c for c in res.parsed.citations if c not in allowed]
    return {"question": question, "as_of": as_of, "answer": res.parsed.answer, "citations": cites,
            "dropped_citations": dropped, "context": [c["id"] for c in ctx], "model": res.model,
            "cached": res.cached}
