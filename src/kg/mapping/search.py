"""Candidate clauses for an obligation inside one document version (plan section 7.5).

Candidates = clauses of the sections the locate step pointed to + the hybrid-search top hits (BM25 on character
n-grams, optional dense vectors, RRF over the obligation text and its source provision).
"""
from __future__ import annotations

import re

from kg.mapping.docview import DocView
from kg.retrieval.index import HybridIndex, build_index


def doc_index(cfg, view: DocView) -> HybridIndex:
    """Clauses are indexed with ``section title + clause heading + clause text``."""
    titles = {s["section_no"]: s["title"] for s in view.sections}
    items = []
    for u in view.mapped:
        text = " ".join(x for x in (titles.get(u["section"]), u.get("heading"), u["text"]) if x)
        items.append({"id": u["id"], "text": text, "body": u["text"], "section_no": u["section_no"],
                      "heading": u.get("heading"), "order": u["order"]})
    return build_index(cfg, items, "doc_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", view.doc_id))


def obligation_queries(props: dict, source_text: str | None) -> list[str]:
    main = " ".join(str(x) for x in (props.get("statement"), props.get("location_hint"),
                                     props.get("deadline_or_frequency"), props.get("condition")) if x)
    return [main] + ([source_text] if source_text else [])


def candidates_for(cfg, index: HybridIndex, view: DocView, props: dict, source_text: str | None,
                   located: list[str], extra_ids: list[str] | None = None) -> list[dict]:
    """-> [{id, section_no, heading, text}] without duplicates: user-fixed clauses, located sections, search hits."""
    k = int(cfg.get_in("mapping.top_k_clauses", 8))
    max_loc = int(cfg.get_in("mapping.max_located_clauses", 12))
    cap = int(cfg.get_in("mapping.clause_text_cap", 1500))
    order: list[str] = list(extra_ids or [])
    order += [u["id"] for u in view.clauses_of(located)][:max_loc]
    order += [cid for cid, _ in index.search_multi(obligation_queries(props, source_text), k)]
    seen, out = set(), []
    for cid in order:
        u = view.by_id.get(cid)
        if u is None or cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "section_no": u["section_no"], "heading": u.get("heading"), "text": u["text"][:cap]})
    return out
