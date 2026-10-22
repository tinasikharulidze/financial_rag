"""Mapping step 2 (LLM): is this document a place where the obligation belongs? (plan section 7.2)

relevant / possibly continue to locate and judge; irrelevant ones are stored (with the reason) and produce no task.
Guard in code: an obligation whose regulation EXPLICITLY names this kind of document can never be irrelevant.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, field_validator

from kg.citations import Citation, dump, verify_citations
from kg.company.profile import norm_type, type_matches
from kg.llm import system_user
from kg.mapping.docview import DocView
from kg.steps import load_prompt, run_step
from kg.store.v2tables import jdump, jload

PROMPT = Path(__file__).parent / "prompts" / "triage.md"
STEP = "triage"
LEVELS = ("relevant", "possibly", "irrelevant")


class TriageItem(BaseModel):
    obligation_id: str
    relevance: str = "possibly"
    reason: str = ""
    citations: list[Citation] = []

    @field_validator("relevance", mode="before")
    @classmethod
    def _r(cls, v):
        v = str(v or "").strip().lower()
        return v if v in LEVELS else "possibly"

    @field_validator("citations", mode="before")
    @classmethod
    def _c(cls, v):
        return [] if v in (None, "") else (v if isinstance(v, list) else [v])


class TriageBatch(BaseModel):
    results: list[TriageItem] = []


def compact(o: dict) -> dict:
    p = o["props"]
    return {"obligation_id": o["id"], "statement": p.get("statement"), "required_doc": p.get("required_doc"),
            "evidence_type": p.get("evidence_type"), "location_hint": p.get("location_hint"),
            "severity": p.get("severity")}


def profile_input(profile: dict, view: DocView) -> dict:
    heads = {s["section_no"]: s["id"] for s in view.sections}
    return {"doc_type": profile["doc_type"], "purpose": profile["purpose"],
            "sections": [{"id": heads.get(s["section_no"]), "section_no": s["section_no"], "title": s.get("title"),
                          "summary": s.get("summary"), "goal": s.get("goal")} for s in profile["sections"]]}


def explicit_match(o: dict, doc_types: list[str]) -> bool:
    rd = o["props"].get("required_doc") or {}
    return rd.get("basis") == "explicit" and type_matches(doc_types, rd.get("doc_types") or [])


def make_validator(store, ids: set[str], section_ids: set[str]):
    def validate(out: TriageBatch) -> list[str]:
        notes: list[str] = []
        seen, keep = set(), []
        for r in out.results:
            if r.obligation_id not in ids:
                notes.append(f"note: unknown obligation id ignored: {r.obligation_id}")
                continue
            if r.obligation_id in seen:
                continue
            seen.add(r.obligation_id)
            good, errs = verify_citations(store, r.citations, allowed_ids=section_ids, soft_quotes=True)
            r.citations = good
            notes += [e if e.startswith("note:") else "note: " + e for e in errs]
            keep.append(r)
        out.results = keep
        miss = sorted(ids - seen)
        if miss:
            notes.append(f"note: {len(miss)} obligations had no answer and are treated as 'possibly'")
        return notes
    return validate


def triage_document(cfg, store, run, llm, version_id: str, view: DocView, profile: dict, obligations: list[dict],
                    force: bool = False, retry_invalid: bool = False) -> dict:
    """Triage the obligations of one document version; rows go to ``triage``. -> counts per relevance."""
    system, version = load_prompt(PROMPT)
    size = int(cfg.get_in("mapping.triage_batch", 60))
    prof = profile_input(profile, view)
    section_ids = {s["id"] for s in prof["sections"] if s["id"]}
    doc_types = [profile["doc_type"], view.props.get("doc_type")]
    user_rows = {r["obligation_id"] for r in store.q("SELECT obligation_id FROM triage WHERE doc_version_id=? "
                                                     "AND source='user'", (version_id,))}
    todo = [o for o in obligations if o["id"] not in user_rows]
    counts = {"relevant": 0, "possibly": 0, "irrelevant": 0, "guard": 0, "missing": 0, "batches": 0, "failed": 0}
    for i in range(0, len(todo), size):
        batch = todo[i:i + size]
        inputs = {"profile": prof, "obligations": [compact(o) for o in batch]}
        subject = f"{version_id}:triage:" + hashlib.sha1("|".join(o["id"] for o in batch).encode()).hexdigest()[:10]
        messages = system_user(system, "INPUT:\n" + json.dumps(inputs, ensure_ascii=False))
        res = run_step(store, llm, STEP, subject, inputs, messages, TriageBatch, version,
                       validate=make_validator(store, {o["id"] for o in batch}, section_ids), force=force,
                       retry_invalid=retry_invalid, run=run)
        counts["batches"] += 1
        by_id = {r.obligation_id: r for r in res.output.results} if res.output else {}
        if res.output is None:
            counts["failed"] += 1
        for o in batch:
            r = by_id.get(o["id"])
            source, rel, reason, cites = "llm", "possibly", "", []
            if r is None:
                counts["missing"] += 1
                source, reason = "rule", "მოდელმა პასუხი არ დააბრუნა; დარჩა შესამოწმებლად."
            else:
                rel, reason, cites = r.relevance, r.reason, dump(r.citations)
            if rel == "irrelevant" and explicit_match(o, doc_types):
                rel, source = "relevant", "rule"
                reason = "რეგულაცია პირდაპირ ითხოვს ამ ტიპის დოკუმენტში ასახვას. " + reason
                counts["guard"] += 1
                run.log("triage_guard", doc=version_id, obligation=o["id"])
            counts[rel] += 1
            store.x("INSERT OR REPLACE INTO triage(doc_version_id,obligation_id,relevance,reason,citations,"
                    "llm_output_id,source) VALUES(?,?,?,?,?,?,?)",
                    (version_id, o["id"], rel, reason, jdump(cites), res.output_id if r is not None else None, source))
    return counts


def triage_rows(store, version_id: str) -> list[dict]:
    rows = store.q("SELECT * FROM triage WHERE doc_version_id=? ORDER BY obligation_id", (version_id,))
    for r in rows:
        r["citations"] = jload(r["citations"], [])
    return rows


def continuing(store, version_id: str) -> list[str]:
    return [r["obligation_id"] for r in triage_rows(store, version_id) if r["relevance"] in ("relevant", "possibly")]
