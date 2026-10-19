"""Candidate clauses for an obligation: hybrid retrieval inside one document."""
from __future__ import annotations

from kg.company.ingest import MAPPED_TYPES
from kg.retrieval.index import HybridIndex, build_index


def doc_index(cfg, store, doc_id: str) -> HybridIndex:
    items = [{"id": n["id"], "text": (f"{n['props'].get('heading') or ''} {n['props']['text']}").strip(),
              "body": n["props"]["text"], "section_no": n["props"].get("section_no"),
              "heading": n["props"].get("heading"), "unit_type": n["props"].get("unit_type"),
              "order": n["props"].get("order", 0)}
             for n in store.nodes("Clause") if n["props"].get("doc") == doc_id
             and n["props"].get("unit_type") in MAPPED_TYPES]
    return build_index(cfg, items, f"doc_{doc_id.split(':')[1].split('@')[0]}")


def candidates_for(index: HybridIndex, obligation: dict, k: int = 5, text_cap: int = 900) -> list[dict]:
    p = obligation["props"]
    query = " ".join(x for x in (p.get("statement"), p.get("action"), p.get("subject")) if x)
    out = []
    for cid, _score in index.search(query, k):
        it = index.get(cid)
        out.append({"id": cid, "section_no": it.get("section_no"), "heading": it.get("heading"),
                    "text": (it["body"] or "")[:text_cap]})
    return out
