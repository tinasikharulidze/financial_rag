"""``map-coverage``: obligations x internal documents -> findings (docs/KG_ARCHITECTURE.md section 6).

For every document the obligations that apply to its doc_type are judged once (distinct obligation nodes active at
the document's regulatory version ``V_doc`` or today's version ``V_now``). Findings are then classified:

    class B   the obligation already applied when the document was written and the document does not meet it
              (basis V_doc);
    class A   the document met the earlier wording, but the regulation changed and it no longer does, or the
              obligation is new (basis V_now);
    nothing   the document meets the obligation (no row; only the COVERED_BY edge is kept).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from kg.config import today_iso
from kg.extract.obligations import _active_pv
from kg.ids import display_path
from kg.llm import BudgetExhausted, LLMValidationError
from kg.mapping.candidates import candidates_for, doc_index
from kg.mapping.judge import JudgeItem, judge_batch
from kg.mapping.labels import priority

COMPANY = "__company__"
_TOK = re.compile(r"\w+", re.U)


def category_map(cfg) -> dict[str, set[str]]:
    from kg.config import REPO_ROOT
    rel = cfg.get_in("mapping.doc_type_table", "configs/doc_type_obligations.yaml")
    p = Path(rel) if Path(rel).is_absolute() else REPO_ROOT / rel
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return {k: set(v) for k, v in data["doc_types"].items()}


def active_obligations(store, date: str, cats: set[str]) -> list[dict]:
    return [n for n in store.nodes("Obligation", as_of=date)
            if n["props"].get("category") in cats and n["props"].get("category") != "non_obligation"]


def _jaccard(a: str, b: str) -> float:
    x, y = set(_TOK.findall(a.lower())), set(_TOK.findall(b.lower()))
    return len(x & y) / len(x | y) if x and y else 0.0


def predecessor(succ: dict, pool: list[dict]) -> dict | None:
    """Best match of a successor obligation among the obligations of the same unit at the earlier version."""
    same = [p for p in pool if p["props"]["act"] == succ["props"]["act"] and p["props"]["unit"] == succ["props"]["unit"]]
    if not same:
        return None
    for p in same:
        if p["id"] == succ["id"]:
            return p
    best = max(same, key=lambda p: _jaccard(p["props"]["statement"], succ["props"]["statement"]))
    return best if _jaccard(best["props"]["statement"], succ["props"]["statement"]) >= 0.25 else None


def has_issue(j: JudgeItem) -> bool:
    return j.coverage != "full"


def _source_text(store, ob: dict) -> str:
    p = ob["props"]
    pv = _active_pv(store, p["act"], p["source_path"], ob["valid_from"])
    return ((pv or {}).get("props", {}).get("text") or "")[:600]


def judge_all(cfg, store, run, llm, index, obligations: list[dict], k: int | None = None,
              progress=None, extra_clauses: dict[str, list[str]] | None = None) -> dict[str, JudgeItem]:
    """Judge obligations against one document index in batches; returns {obligation_id: JudgeItem}."""
    k = k or int(cfg.get_in("mapping.top_k_clauses", 5))
    size = int(cfg.get_in("mapping.judge_batch", 8))
    out: dict[str, JudgeItem] = {}
    for i in range(0, len(obligations), size):
        chunk = obligations[i:i + size]
        items = [{"obligation_id": o["id"], "statement": o["props"]["statement"],
                  "requirement": {kk: o["props"].get(kk) for kk in ("subject", "action", "condition",
                                                                    "deadline_or_frequency")},
                  "source": f"{display_path(o['props']['source_path'])}: {_source_text(store, o)}",
                  "candidates": _with_extra(index, o, k, (extra_clauses or {}).get(o["id"]))} for o in chunk]
        valid = {c["id"] for it in items for c in it["candidates"]}
        try:
            res = judge_batch(llm, items)
        except LLMValidationError as exc:
            run.log("judge_failed", error=str(exc)[:300], obligations=[o["id"] for o in chunk])
            run.count("judge_failed", len(chunk))
            continue
        for o in chunk:
            j = res.get(o["id"])
            if j is None:
                run.log("judge_missing", obligation=o["id"])
                continue
            if j.best_clause_id not in valid:
                j.best_clause_id = None
            out[o["id"]] = j
        run.count("judge_batches")
        if progress:
            progress("judge", min(i + size, len(obligations)), len(obligations))
    return out


def _with_extra(index, ob: dict, k: int, extra: list[str] | None) -> list[dict]:
    """Top-k clauses, plus clauses that were linked to the obligation this one replaces (replay)."""
    cands = candidates_for(index, ob, k)
    have = {c["id"] for c in cands}
    for cid in extra or []:
        it = index.get(cid)
        if it and cid not in have:
            cands.append({"id": cid, "section_no": it.get("section_no"), "heading": it.get("heading"),
                          "text": (it["body"] or "")[:900]})
    return cands


def _finding(run_id, doc_id, ob, j: JudgeItem, basis: str, gap_class: str) -> dict:
    sev = ob["props"]["severity"]
    action = j.action_type or ("ADD_SECTION" if j.coverage == "none" else "ADJUST_SECTION")
    return {"run_id": run_id, "doc_id": COMPANY if action == "NEW_DOCUMENT" else doc_id,
            "clause_id": j.best_clause_id, "obligation_id": ob["id"], "version_basis": basis,
            "gap_class": gap_class, "action_type": action, "coverage": j.coverage,
            "issue_chance": j.issue_chance, "severity": sev, "priority": priority(sev, j.issue_chance),
            "rationale": j.rationale, "evidence_quote": j.evidence_quote, "suggested_fix": j.suggested_fix}


def classify(doc_id, v_doc_obls: list[dict], v_now_obls: list[dict], judged: dict[str, JudgeItem],
             run_id: str | None) -> tuple[list[dict], dict]:
    """Findings from judged V_doc / V_now obligations (pure function, unit-tested)."""
    rows, stats = [], {"B": 0, "A": 0, "ok": 0, "unjudged": 0, "repealed_skipped": 0}
    now_ids = {o["id"] for o in v_now_obls}
    for p in v_doc_obls:                                        # class B: broken at the time of writing
        j = judged.get(p["id"])
        if j is None:
            stats["unjudged"] += 1
        elif has_issue(j):
            if p["id"] in now_ids or any(predecessor(s, [p]) for s in v_now_obls):
                rows.append(_finding(run_id, doc_id, p, j, "V_doc", "B"))
                stats["B"] += 1
            else:
                stats["repealed_skipped"] += 1             # obligation no longer exists: nothing to act on
    b_units = {(r["obligation_id"]) for r in rows}
    for s in v_now_obls:                                        # class A: changed or new since V_doc
        if s["id"] in {p["id"] for p in v_doc_obls}:
            if judged.get(s["id"]) is not None and not has_issue(judged[s["id"]]):
                stats["ok"] += 1
            continue                                            # unchanged and already handled as B
        j = judged.get(s["id"])
        if j is None:
            stats["unjudged"] += 1
            continue
        pred = predecessor(s, v_doc_obls)
        if pred is not None and pred["id"] in b_units:
            continue                                            # already non-compliant before the change
        if has_issue(j):
            rows.append(_finding(run_id, doc_id, s, j, "V_now", "A"))
            stats["A"] += 1
        else:
            stats["ok"] += 1
    return rows, stats


def write_covered_by(store, doc_id: str, judged: dict[str, JudgeItem], run_id: str | None, partial: bool = False):
    """Recompute the COVERED_BY edges of one document (``partial``: only for the judged obligations)."""
    prefix = doc_id.replace("doc:", "cl:") + "/"
    if partial:
        for oid in judged:
            store.delete_edges("COVERED_BY", src=oid, dst_prefix=prefix)
    else:
        store.delete_edges("COVERED_BY", dst_prefix=prefix)
    edges = []
    for oid, j in judged.items():
        if j.best_clause_id and j.coverage in ("full", "partial", "contradicts"):
            edges.append((oid, j.best_clause_id, "COVERED_BY",
                          {"coverage": j.coverage, "doc": doc_id, "run": run_id}, None, None))
    store.upsert_edges(edges)
    return len(edges)


def doc_versions(store, doc: dict, now: str) -> tuple[str, str]:
    all_from = [n["valid_from"] for n in store.nodes("Obligation") if n["valid_from"]]
    earliest = min(all_from) if all_from else now
    d = doc["props"].get("effective_date") or doc["props"].get("doc_date") or doc["valid_from"] or now
    return max(d, earliest), now


def map_coverage(cfg, store, run, llm, docs: list[str] | None = None, dry_run: bool = False,
                 only_obligations: set[str] | None = None, now: str | None = None, progress=None,
                 links: dict[str, str] | None = None) -> dict:
    """``links``: successor obligation id -> predecessor id (replay); clauses covering the predecessor become extra
    candidates for the successor."""
    now = now or cfg.get_in("mapping.now") or today_iso()
    cmap = category_map(cfg)
    summary = {"docs": 0, "obligations_judged": 0, "findings": 0, "A": 0, "B": 0, "estimated_requests": 0,
               "skipped_docs": []}
    size = int(cfg.get_in("mapping.judge_batch", 8))
    doc_nodes = [d for d in store.nodes("InternalDoc") if not docs or d["id"] in docs]
    company_rows: list[dict] = []
    for d in sorted(doc_nodes, key=lambda n: n["id"]):
        cats = cmap.get(d["props"].get("doc_type") or "other", set())
        if not cats:
            summary["skipped_docs"].append(d["id"])
            continue
        v_doc, v_now = doc_versions(store, d, now)
        o_doc = active_obligations(store, v_doc, cats)
        o_now = active_obligations(store, v_now, cats)
        if only_obligations is not None:
            o_doc = [o for o in o_doc if o["id"] in only_obligations]
            o_now = [o for o in o_now if o["id"] in only_obligations]
        distinct = {o["id"]: o for o in o_doc + o_now}
        n_req = -(-len(distinct) // size)
        summary["estimated_requests"] += n_req
        run.log("doc_plan", doc=d["id"], doc_type=d["props"].get("doc_type"), v_doc=v_doc, v_now=v_now,
                obligations=len(distinct), requests=n_req)
        if dry_run:
            continue
        index = doc_index(cfg, store, d["id"])
        extra = {}
        for succ, pred in (links or {}).items():
            extra[succ] = [e["dst"] for e in store.edges("COVERED_BY") if e["src"] == pred
                           and e["dst"].startswith(d["id"].replace("doc:", "cl:") + "/")]
        if progress:
            progress("index", 0, len(distinct))
        judged = judge_all(cfg, store, run, llm, index, list(distinct.values()), progress=progress,
                           extra_clauses=extra)
        rows, stats = classify(d["id"], o_doc, o_now, judged, run.id)
        decided = [o["id"] for o in distinct.values()]
        if only_obligations is None:
            store.clear_findings(d["id"])
        else:
            store.clear_findings(d["id"], decided)
        direct = [r for r in rows if r["doc_id"] != COMPANY]
        company_rows += [r for r in rows if r["doc_id"] == COMPANY]
        store.add_findings(direct)
        n_edges = write_covered_by(store, d["id"], judged, run.id, partial=only_obligations is not None)
        run.log("doc_done", doc=d["id"], judged=len(judged), edges=n_edges, **stats)
        run.count("docs")
        summary["docs"] += 1
        summary["obligations_judged"] += len(judged)
        summary["A"] += stats["A"]
        summary["B"] += stats["B"]
        summary["findings"] += len(rows)
    if docs is None and only_obligations is None and not dry_run:
        company_rows += new_document_findings(store, cmap, doc_nodes, now, run.id)
    if company_rows:
        seen: set[str] = set()
        uniq = []
        for r in company_rows:
            if r["obligation_id"] not in seen:
                seen.add(r["obligation_id"])
                uniq.append(r)
        store.clear_findings(COMPANY, [r["obligation_id"] for r in uniq])
        store.add_findings(uniq)
    return summary


def new_document_findings(store, cmap, doc_nodes, now: str, run_id: str | None) -> list[dict]:
    """§4.7 step 6: obligations of a category that no ingested document type is expected to address."""
    have = set()
    for d in doc_nodes:
        have |= cmap.get(d["props"].get("doc_type") or "other", set())
    rows = []
    for o in store.nodes("Obligation", as_of=now):
        cat = o["props"].get("category")
        if cat and cat != "non_obligation" and cat not in have:
            sev = o["props"]["severity"]
            rows.append({"run_id": run_id, "doc_id": COMPANY, "clause_id": None, "obligation_id": o["id"],
                         "version_basis": "V_now", "gap_class": "A", "action_type": "NEW_DOCUMENT",
                         "coverage": "none", "issue_chance": "high", "severity": sev,
                         "priority": priority(sev, "high"),
                         "rationale": f"კატეგორია „{cat}“ არცერთ არსებულ დოკუმენტში არ არის გათვალისწინებული.",
                         "evidence_quote": None, "suggested_fix": "მომზადდეს ცალკე დოკუმენტი ამ მოთხოვნისთვის."})
    return rows
