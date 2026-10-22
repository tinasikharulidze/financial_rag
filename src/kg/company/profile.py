"""Document profile (docs/KG_V2_PLAN.md section 6.3): purpose, type, domains and a summary of every section.

``build_profile`` asks the model once per document version (one request, reused from ``step_outputs`` afterwards)
and stores the result in ``doc_profiles``. The user then confirms or edits it (``confirm_profile``); mapping uses
the confirmed values. When the model fails, a profile built from the parser alone is stored so the user can still
fill it in by hand.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, field_validator

from kg.citations import Citation, dump, verify_citations
from kg.config import now_iso
from kg.llm import LLMValidationError, system_user
from kg.mapping.docview import DocView, doc_text, fits_whole_doc, load_doc, section_digest
from kg.services.decisions import record_decision
from kg.services.permissions import require
from kg.steps import load_prompt, run_step
from kg.store.v2tables import jdump, jload

PROMPT = Path(__file__).parent.parent / "mapping" / "prompts" / "doc_profile.md"
STEP = "doc_profile"


class ProfileSection(BaseModel):
    section_no: str
    title: str = ""
    summary: str = ""
    goal: str = ""

    @field_validator("section_no", mode="before")
    @classmethod
    def _s(cls, v):
        return str(v).strip().rstrip(".")


class ProfileOut(BaseModel):
    purpose: str = ""
    doc_type: str = ""
    domains: list[str] = []
    sections: list[ProfileSection] = []
    citations: list[Citation] = []

    @field_validator("domains", "sections", "citations", mode="before")
    @classmethod
    def _l(cls, v):
        return [] if v in (None, "") else (v if isinstance(v, list) else [v])


def norm_type(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").strip().lower()).strip("_")


def type_matches(doc_types: list[str], required: list[str]) -> bool:
    """Does one of the document's type labels satisfy one of the required types? (exact, or one is a '_'-suffix of the
    other: ``model_risk_policy`` satisfies ``policy``, ``inventory`` satisfies ``model_inventory``)."""
    for d in {norm_type(x) for x in doc_types if x}:
        for r in {norm_type(x) for x in required if x}:
            if d == r or d.endswith("_" + r) or r.endswith("_" + d):
                return True
    return False


# ----------------------------------------------------------------------------------------------- vocabularies
def domains_available(cfg, store) -> list[str]:
    ds = {n["props"].get("domain") for n in store.nodes("Obligation") if n["props"].get("schema") == "v2"}
    for a in (cfg.get("acts") or {}).values():
        ds.update(a.get("domains") or [])
    return sorted(d for d in ds if d)


def doc_type_vocab(store) -> list[str]:
    out = set()
    for n in store.nodes("Obligation"):
        rd = n["props"].get("required_doc") or {}
        out.update(norm_type(t) for t in (rd.get("doc_types") or []))
    return sorted(t for t in out if t)


# ----------------------------------------------------------------------------------------------- build
def _inputs(cfg, store, view: DocView) -> dict:
    p = view.props
    meta = [u["text"] for u in view.units if u["unit_type"] == "metadata"]
    whole = fits_whole_doc(cfg, view)
    return {"title": p.get("title"), "parser_doc_type": p.get("doc_type"),
            "dates": {"doc_date": p.get("doc_date"), "effective_date": p.get("effective_date")},
            "cover": "\n".join(meta)[:1500],
            "sections": [{"section_no": s["section_no"], "title": s["title"]} for s in view.sections],
            "text": doc_text(view) if whole else section_digest(view),
            "domain_options": domains_available(cfg, store),
            "doc_type_examples": doc_type_vocab(store)}


def make_validator(store, view: DocView, domain_options: list[str]):
    clause_ids = {u["id"] for u in view.units}

    def validate(out: ProfileOut) -> list[str]:
        notes: list[str] = []
        have = {s.section_no: s for s in out.sections if view.section(s.section_no)}
        dropped = [s.section_no for s in out.sections if s.section_no not in have]
        if dropped:
            notes.append(f"note: sections not in the document were dropped: {dropped}")
        fixed = []
        for s in view.sections:                          # every parser section appears exactly once, in order
            m = have.get(s["section_no"])
            if m is None:
                notes.append(f"note: section {s['section_no']} had no summary")
                m = ProfileSection(section_no=s["section_no"])
            m.title = s["title"]
            fixed.append(m)
        out.sections = fixed
        out.doc_type = norm_type(out.doc_type)
        if domain_options:
            keep = [d for d in dict.fromkeys(out.domains) if d in domain_options]
            if keep != out.domains:
                notes.append(f"note: unknown domains dropped: {[d for d in out.domains if d not in keep]}")
            out.domains = keep
        good, errs = verify_citations(store, out.citations, allowed_ids=clause_ids, soft_quotes=True)
        out.citations = good
        problems = []
        if not out.purpose.strip():
            problems.append("`purpose` is empty")
        if not out.doc_type:
            problems.append("`doc_type` is empty")
        return problems + notes + errs
    return validate


def parser_profile(view: DocView) -> dict:
    """Profile built from the parser alone (used when the model fails; the user completes it)."""
    return {"doc_type": norm_type(view.props.get("doc_type")), "domains": [], "purpose": view.props.get("title") or "",
            "sections": [{"section_no": s["section_no"], "title": s["title"], "summary": "", "goal": ""}
                         for s in view.sections], "citations": []}


def build_profile(cfg, store, run, llm, version_id: str, force: bool = False, retry_invalid: bool = False) -> dict:
    """Run (or reuse) the profile step; returns ``get_profile``. A confirmed profile is never overwritten."""
    view = load_doc(store, version_id)
    if view is None:
        raise KeyError(f"document version not found: {version_id}")
    cur = store.q1("SELECT * FROM doc_profiles WHERE doc_version_id=?", (version_id,))
    if cur and cur["confirmed_at"] and not force:
        return get_profile(store, version_id)
    system, version = load_prompt(PROMPT)
    inputs = _inputs(cfg, store, view)
    messages = system_user(system, "INPUT:\n" + json.dumps(inputs, ensure_ascii=False))
    res = run_step(store, llm, STEP, version_id, inputs, messages, ProfileOut, version,
                   validate=make_validator(store, view, inputs["domain_options"]), force=force,
                   retry_invalid=retry_invalid, run=run)
    if res.output is not None:
        o = res.output
        row = {"doc_type": o.doc_type, "domains": o.domains, "purpose": o.purpose,
               "sections": [s.model_dump() for s in o.sections], "citations": dump(o.citations)}
    else:
        row = parser_profile(view)
    store.x("INSERT OR REPLACE INTO doc_profiles(doc_version_id,llm_output_id,doc_type,domains,purpose,sections,"
            "citations,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?,?,NULL,NULL)",
            (version_id, res.output_id if res.output is not None else None, row["doc_type"], jdump(row["domains"]),
             row["purpose"], jdump(row["sections"]), jdump(row["citations"])))
    return get_profile(store, version_id)


# ----------------------------------------------------------------------------------------------- read / confirm
def get_profile(store, version_id: str) -> dict | None:
    r = store.q1("SELECT * FROM doc_profiles WHERE doc_version_id=?", (version_id,))
    if r is None:
        return None
    return {"doc_version_id": version_id, "doc_type": r["doc_type"], "domains": jload(r["domains"], []),
            "purpose": r["purpose"], "sections": jload(r["sections"], []), "citations": jload(r["citations"], []),
            "confirmed": bool(r["confirmed_at"]), "confirmed_by": r["confirmed_by"], "confirmed_at": r["confirmed_at"],
            "from_model": r["llm_output_id"] is not None}


def confirm_profile(store, actor: dict, version_id: str, *, doc_type: str | None = None,
                    domains: list[str] | None = None, purpose: str | None = None,
                    sections: list[dict] | None = None) -> dict:
    """The user confirms the profile, optionally editing it. Edits are recorded as a ``profile_edit`` decision."""
    v = store.q1("SELECT dv.id, d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                 "WHERE dv.id=?", (version_id,))
    if v is None:
        raise KeyError(f"document version not found: {version_id}")
    require(actor, "decision.make", v["company_id"])
    before = get_profile(store, version_id)
    if before is None:
        raise KeyError("no profile yet: run build_profile first")
    after = dict(before)
    if doc_type is not None:
        after["doc_type"] = norm_type(doc_type)
    if domains is not None:
        after["domains"] = list(dict.fromkeys(domains))
    if purpose is not None:
        after["purpose"] = purpose.strip()
    if sections is not None:
        known = {s["section_no"]: s for s in before["sections"]}
        merged = []
        for s in sections:
            base = known.get(str(s.get("section_no")))
            if base is None:
                continue
            merged.append({**base, **{k: s[k] for k in ("summary", "goal") if k in s}})
        after["sections"] = merged or before["sections"]
    changed = {k: [before[k], after[k]] for k in ("doc_type", "domains", "purpose", "sections")
               if before[k] != after[k]}
    store.x("UPDATE doc_profiles SET doc_type=?, domains=?, purpose=?, sections=?, confirmed_by=?, confirmed_at=? "
            "WHERE doc_version_id=?",
            (after["doc_type"], jdump(after["domains"]), after["purpose"], jdump(after["sections"]), actor["id"],
             now_iso(), version_id))
    record_decision(store, actor, "profile_edit" if changed else "profile_confirm", doc_version_id=version_id,
                    data={"changed": sorted(changed), "before": {k: v[0] for k, v in changed.items()}})
    return get_profile(store, version_id)
