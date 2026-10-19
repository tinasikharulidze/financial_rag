"""LLM judge: does a document clause cover an obligation? (purpose ``judge_coverage``)."""
from __future__ import annotations

import json

from pydantic import BaseModel, field_validator

from kg.mapping.labels import norm_chance

COVERAGE = ("full", "partial", "contradicts", "none")
ACTIONS = ("ADJUST_SECTION", "ADD_SECTION", "NEW_DOCUMENT")

SYSTEM = """You are a bank compliance reviewer. You check whether an INTERNAL DOCUMENT of a commercial bank satisfies
regulatory OBLIGATIONS of the Georgian National Bank model-risk regulation. For each obligation you get the
obligation text, the regulation provision it comes from, and up to 5 CANDIDATE CLAUSES of the document.

For each obligation decide:
- coverage:
    full         a clause satisfies the whole obligation (including numbers, frequencies, deadlines and roles);
    partial      a clause addresses it but is weaker, incomplete or less strict than required;
    contradicts  a clause states something that conflicts with the obligation (e.g. a longer deadline, an
                 allowed practice that the regulation forbids, a missing independence);
    none         no candidate clause deals with it.
- best_clause_id: the id of the clause that matters most (null when coverage is none).
- issue_chance: how likely it is that the document has a real problem with this obligation:
    very_high | high | moderate | low.
- rationale: 1-3 sentences IN GEORGIAN explaining the decision with the specific mismatch.
- evidence_quote: a short quote copied VERBATIM from the best clause that shows the problem (null if none).
- suggested_fix: one concrete change IN GEORGIAN (null if coverage is full).
- action_type: ADJUST_SECTION (change an existing clause), ADD_SECTION (the document needs a new section) or
  NEW_DOCUMENT (the topic needs a separate document that the bank does not have).

Be strict about facts, but do not invent problems: when a clause clearly meets the requirement answer
coverage=full with issue_chance=low. Compare numbers, frequencies, deadlines and responsibilities literally.
Answer with JSON only: {"results": [{"obligation_id": "...", "coverage": "...", "best_clause_id": "...",
"issue_chance": "...", "rationale": "...", "evidence_quote": "...", "suggested_fix": "...", "action_type": "..."}]}
with exactly one result per obligation, in the same order."""


class JudgeItem(BaseModel):
    obligation_id: str
    coverage: str = "none"
    best_clause_id: str | None = None
    issue_chance: str = "moderate"
    rationale: str = ""
    evidence_quote: str | None = None
    suggested_fix: str | None = None
    action_type: str | None = None

    @field_validator("coverage")
    @classmethod
    def _cov(cls, v):
        v = (v or "none").strip().lower()
        return v if v in COVERAGE else "none"

    @field_validator("issue_chance")
    @classmethod
    def _chance(cls, v):
        return norm_chance(v)

    @field_validator("action_type")
    @classmethod
    def _act(cls, v):
        if v is None:
            return None
        v = v.strip().upper()
        return v if v in ACTIONS else None


class JudgeBatch(BaseModel):
    results: list[JudgeItem]


def build_user(items: list[dict]) -> str:
    """items: [{'obligation_id','statement','source_text','candidates':[{'id','section_no','heading','text'}]}]"""
    return "Obligations to check:\n" + json.dumps(items, ensure_ascii=False, indent=1)


def judge_batch(llm, items: list[dict]) -> dict[str, JudgeItem]:
    from kg.llm import system_user

    res = llm.call("judge_coverage", system_user(SYSTEM, build_user(items)), JudgeBatch)
    return {r.obligation_id: r for r in res.parsed.results}
