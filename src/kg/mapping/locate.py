"""Mapping step 3 (LLM): which sections of the document should contain the obligation? (plan section 7.3)"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, field_validator

from kg.llm import system_user
from kg.mapping.docview import DocView
from kg.steps import load_prompt, run_step
from kg.store.v2tables import jdump, jload

PROMPT = Path(__file__).parent / "prompts" / "locate.md"
STEP = "locate"
MAX_SECTIONS = 5


class LocSection(BaseModel):
    section_no: str
    reason: str = ""

    @field_validator("section_no", mode="before")
    @classmethod
    def _s(cls, v):
        return str(v).strip().rstrip(".")


class LocItem(BaseModel):
    obligation_id: str
    sections: list[LocSection] = []

    @field_validator("sections", mode="before")
    @classmethod
    def _l(cls, v):
        if v in (None, ""):
            return []
        return [x if isinstance(x, (dict, LocSection)) else {"section_no": x} for x in (v if isinstance(v, list) else [v])]


class LocBatch(BaseModel):
    results: list[LocItem] = []


def make_validator(view: DocView, ids: set[str]):
    valid = set(view.section_nos())

    def validate(out: LocBatch) -> list[str]:
        notes, seen, keep = [], set(), []
        for r in out.results:
            if r.obligation_id not in ids or r.obligation_id in seen:
                continue
            seen.add(r.obligation_id)
            good = [s for s in r.sections if s.section_no in valid]
            if len(good) != len(r.sections):
                notes.append(f"note: {r.obligation_id}: unknown section numbers dropped")
            r.sections = list({s.section_no: s for s in good}.values())[:MAX_SECTIONS]
            keep.append(r)
        out.results = keep
        if ids - seen:
            notes.append(f"note: {len(ids - seen)} obligations had no location answer")
        return notes
    return validate


def locate_document(cfg, store, run, llm, version_id: str, view: DocView, profile: dict, obligations: list[dict],
                    force: bool = False, retry_invalid: bool = False) -> dict:
    system, version = load_prompt(PROMPT)
    size = int(cfg.get_in("mapping.locate_batch", 40))
    secs = [{"section_no": s["section_no"], "title": s.get("title"), "summary": s.get("summary"), "goal": s.get("goal")}
            for s in profile["sections"]]
    counts = {"batches": 0, "located": 0, "empty": 0, "failed": 0}
    for i in range(0, len(obligations), size):
        batch = obligations[i:i + size]
        inputs = {"sections": secs, "obligations": [
            {"obligation_id": o["id"], "statement": o["props"].get("statement"),
             "location_hint": o["props"].get("location_hint"), "required_doc": o["props"].get("required_doc")}
            for o in batch]}
        subject = f"{version_id}:locate:" + hashlib.sha1("|".join(o["id"] for o in batch).encode()).hexdigest()[:10]
        messages = system_user(system, "INPUT:\n" + json.dumps(inputs, ensure_ascii=False))
        res = run_step(store, llm, STEP, subject, inputs, messages, LocBatch, version,
                       validate=make_validator(view, {o["id"] for o in batch}), force=force,
                       retry_invalid=retry_invalid, run=run)
        counts["batches"] += 1
        if res.output is None:
            counts["failed"] += 1
        by_id = {r.obligation_id: r for r in res.output.results} if res.output else {}
        for o in batch:
            r = by_id.get(o["id"])
            sections = [s.model_dump() for s in r.sections] if r else []
            counts["located" if sections else "empty"] += 1
            store.x("INSERT OR REPLACE INTO locations(doc_version_id,obligation_id,sections,llm_output_id) "
                    "VALUES(?,?,?,?)", (version_id, o["id"], jdump(sections), res.output_id))
    return counts


def located_sections(store, version_id: str) -> dict[str, list[str]]:
    return {r["obligation_id"]: [s["section_no"] for s in jload(r["sections"], [])]
            for r in store.q("SELECT obligation_id, sections FROM locations WHERE doc_version_id=?", (version_id,))}
