"""Proposals (plan section 8.1): for every non-full coverage row, one concrete wording that closes the gap.

Code decides WHAT kind of change and WHERE (from the judge's edges, the locate step and triage); the model writes the
text. ``original_text`` is always copied from the clause by code. Numbers in the proposal that appear nowhere in the
obligation, its source or the original text are flagged (``ungrounded_numbers``), not silently accepted.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, field_validator

from kg.citations import Citation, dump, norm_text, verify_citations
from kg.config import now_iso
from kg.llm import system_user
from kg.mapping.docview import DocView, load_doc
from kg.mapping.judging import source_of
from kg.mapping.locate import located_sections
from kg.steps import load_prompt, run_step
from kg.store.v2tables import jdump, jload

PROMPT = Path(__file__).parent / "prompts" / "propose.md"
STEP = "propose"
KINDS = ("adjust_clause", "add_section", "new_document")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


class PropItem(BaseModel):
    obligation_id: str
    heading: str | None = None
    proposed_text: str = ""
    explanation: str = ""
    citations: list[Citation] = []

    @field_validator("citations", mode="before")
    @classmethod
    def _c(cls, v):
        return [] if v in (None, "") else (v if isinstance(v, list) else [v])

    @field_validator("heading", mode="before")
    @classmethod
    def _h(cls, v):
        return None if v in (None, "", "null") else str(v)


class PropBatch(BaseModel):
    results: list[PropItem] = []


def numbers_in(text: str | None) -> set[str]:
    return {n.replace(",", ".") for n in _NUM.findall(text or "")}


def section_numbers(view: DocView) -> set[str]:
    out = set()
    for u in view.units:
        for s in (u.get("section_no"),):
            if s:
                out.add(s)
                out.update(s.split("."))
    return out


# ------------------------------------------------------------------------------------------------ planning
def plan_items(cfg, store, version_id: str, view: DocView) -> list[dict]:
    """Deterministic list of what to propose, from the coverage / triage / locate rows (no LLM)."""
    cov = [r for r in store.q("SELECT * FROM coverage WHERE doc_version_id=? ORDER BY obligation_id", (version_id,))
           if r["verdict"] in ("partial", "contradicts", "none") and not r["invalid"]]
    rel = {r["obligation_id"]: r["relevance"] for r in store.q(
        "SELECT obligation_id, relevance FROM triage WHERE doc_version_id=?", (version_id,))}
    located = located_sections(store, version_id)
    edges: dict[str, list[dict]] = {}
    for e in store.edges("COVERED_BY"):
        if e["props"].get("doc_version") == version_id:
            edges.setdefault(e["src"], []).append(e)
    items = []
    for r in cov:
        oid = r["obligation_id"]
        ob = store.get_node(oid)
        if ob is None:
            continue
        item = {"obligation": ob, "coverage": r, "kind": None, "target": None}
        es = sorted(edges.get(oid, []), key=lambda e: (e["props"].get("source") != "user", e["dst"]))
        want = "contradicts" if r["verdict"] == "contradicts" else "partial"
        tgt = next((e["dst"] for e in es if e["props"].get("role") == want and e["dst"] in view.by_id), None)
        if tgt is None and r["verdict"] != "none":
            tgt = next((e["dst"] for e in es if e["dst"] in view.by_id), None)
        if tgt is not None:
            item.update(kind="adjust_clause", target=tgt)
        else:
            secs = [s for s in located.get(oid, []) if view.clauses_of([s])]
            if secs:
                anchor = view.clauses_of([secs[0]])[-1]["id"]
            elif rel.get(oid) == "relevant" and view.mapped:
                anchor = view.mapped[-1]["id"]
            else:
                anchor = None
            if anchor is not None:
                item.update(kind="add_section", target=anchor)
            else:
                item.update(kind="new_document")
        items.append(item)
    return items


def _clause_view(view: DocView, cid: str, cap: int = 1500) -> dict:
    u = view.by_id[cid]
    return {"id": cid, "section_no": u["section_no"], "heading": u.get("heading"), "text": u["text"][:cap]}


def build_input(store, view: DocView, item: dict) -> dict:
    ob, cov = item["obligation"], item["coverage"]
    p = ob["props"]
    d = {"obligation_id": ob["id"], "kind": item["kind"], "statement": p.get("statement"),
         "requirement": {k: p.get(k) for k in ("subject", "action", "condition", "exceptions",
                                               "deadline_or_frequency")},
         "source": source_of(store, ob),
         "coverage": {"verdict": cov["verdict"], "missing": cov["missing"], "rationale": cov["rationale"]}}
    if item["kind"] == "adjust_clause":
        d["target"] = _clause_view(view, item["target"])
    elif item["kind"] == "add_section":
        a = view.by_id[item["target"]]
        sec = view.section(a["section"]) if a["section"] else None
        d["anchor"] = _clause_view(view, item["target"])
        d["section_title"] = sec["title"] if sec else None
        d["section_clauses"] = [{"section_no": c["section_no"], "text": c["text"][:400]}
                                for c in view.clauses_of([a["section"]])[:4]] if a["section"] else []
    else:
        d["doc_type_hint"] = (p.get("required_doc") or {}).get("doc_types")
    return d


def make_validator(store, view: DocView, items: dict[str, dict], inputs: dict[str, dict]):
    def validate(out: PropBatch) -> list[str]:
        hard, notes, seen, keep = [], [], set(), []
        for r in out.results:
            oid = r.obligation_id
            if oid not in items or oid in seen:
                continue
            seen.add(oid)
            it, inp = items[oid], inputs[oid]
            if not r.proposed_text.strip():
                hard.append(f"{oid}: `proposed_text` is empty")
                continue
            if it["kind"] == "adjust_clause":
                orig = view.by_id[it["target"]]["text"]
                if norm_text(orig) == norm_text(r.proposed_text):
                    hard.append(f"{oid}: the proposed text is identical to the existing clause")
                    continue
            if it["kind"] != "adjust_clause" and not (r.heading or "").strip():
                notes.append(f"note: {oid}: no heading")
            allowed = {(inp.get("source") or {}).get("pv_id"), it["target"]} - {None}
            good, errs = verify_citations(store, r.citations, allowed_ids=allowed or None, soft_quotes=True)
            r.citations = good
            notes += [e if e.startswith("note:") else "note: " + e for e in errs]
            keep.append(r)
        out.results = keep
        miss = sorted(set(items) - seen)
        if miss:
            hard.append("missing results for obligation ids: " + ", ".join(miss))
        return hard + notes
    return validate


def flags_for(view: DocView, item: dict, inp: dict, r: PropItem) -> list[str]:
    allowed = numbers_in(inp.get("statement")) | section_numbers(view)
    for v in (inp.get("requirement") or {}).values():
        allowed |= numbers_in(str(v or ""))
    allowed |= numbers_in((inp.get("source") or {}).get("text"))
    for k in ("target", "anchor"):
        if inp.get(k):
            allowed |= numbers_in(inp[k]["text"])
    bad = sorted(numbers_in(r.proposed_text) - allowed)
    flags = []
    if bad:
        flags.append("ungrounded_numbers:" + ",".join(bad))
    if not r.citations:
        flags.append("no_verified_citations")
    return flags


# ------------------------------------------------------------------------------------------------ the step
def propose_document(cfg, store, run, llm, version_id: str, force: bool = False, retry_invalid: bool = False) -> dict:
    view = load_doc(store, version_id)
    system, version = load_prompt(PROMPT)
    size = int(cfg.get_in("mapping.proposal_batch", 5))
    plan = plan_items(cfg, store, version_id, view)
    have = {r["obligation_id"] for r in store.q("SELECT obligation_id FROM proposals WHERE doc_version_id=?",
                                                (version_id,))}
    todo = plan if force else [it for it in plan if it["obligation"]["id"] not in have]
    stats = {"planned": len(plan), "batches": 0, "written": 0, "failed": 0, "flagged": 0,
             "adjust_clause": 0, "add_section": 0, "new_document": 0}
    prof = store.q1("SELECT doc_type FROM doc_profiles WHERE doc_version_id=?", (version_id,)) or {}
    doc = {"title": view.props.get("title"), "doc_type": prof.get("doc_type")}
    for i in range(0, len(todo), size):
        batch = todo[i:i + size]
        items = {it["obligation"]["id"]: it for it in batch}
        inputs = {oid: build_input(store, view, it) for oid, it in items.items()}
        payload = {"document": doc, "items": list(inputs.values())}
        subject = f"{version_id}:propose:" + hashlib.sha1("|".join(items).encode()).hexdigest()[:10]
        res = run_step(store, llm, STEP, subject, payload,
                       system_user(system, "INPUT:\n" + json.dumps(payload, ensure_ascii=False)), PropBatch, version,
                       validate=make_validator(store, view, items, inputs), force=force,
                       retry_invalid=retry_invalid, run=run)
        stats["batches"] += 1
        by_id = {r.obligation_id: r for r in res.output.results} if res.output else {}
        for oid, it in items.items():
            r = by_id.get(oid)
            if r is None:
                stats["failed"] += 1
                continue
            flags = flags_for(view, it, inputs[oid], r)
            stats["flagged"] += bool(flags)
            stats[it["kind"]] += 1
            original = view.by_id[it["target"]]["text"] if it["kind"] == "adjust_clause" else None
            store.x("INSERT INTO proposals(doc_version_id,obligation_id,kind,clause_id,original_text,proposed_text,"
                    "heading,explanation,citations,flags,llm_output_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(doc_version_id,obligation_id) DO UPDATE SET kind=excluded.kind, "
                    "clause_id=excluded.clause_id, original_text=excluded.original_text, "
                    "proposed_text=excluded.proposed_text, heading=excluded.heading, "
                    "explanation=excluded.explanation, citations=excluded.citations, flags=excluded.flags, "
                    "llm_output_id=excluded.llm_output_id, created_at=excluded.created_at",
                    (version_id, oid, it["kind"], it["target"], original, r.proposed_text.strip(), r.heading,
                     r.explanation, jdump(dump(r.citations)), jdump(flags), res.output_id, now_iso()))
            stats["written"] += 1
    prune_proposals(store, version_id, {it["obligation"]["id"] for it in plan})
    return stats


def prune_proposals(store, version_id: str, keep: set[str]):
    """Proposals whose obligation is now covered (or out of scope) are obsolete."""
    closed = {t["obligation_id"] for t in store.q(
        "SELECT obligation_id FROM tasks WHERE doc_version_id=? AND status IN ('not_relevant','already_addressed')",
        (version_id,))}                                   # kept for the audit trail of the user's decision
    for r in store.q("SELECT id, obligation_id FROM proposals WHERE doc_version_id=?", (version_id,)):
        if r["obligation_id"] not in keep and r["obligation_id"] not in closed:
            store.x("DELETE FROM proposals WHERE id=?", (r["id"],))


def get_proposals(store, version_id: str) -> list[dict]:
    rows = store.q("SELECT * FROM proposals WHERE doc_version_id=? ORDER BY id", (version_id,))
    for r in rows:
        r["citations"] = jload(r["citations"], [])
        r["flags"] = jload(r["flags"], [])
    return rows
