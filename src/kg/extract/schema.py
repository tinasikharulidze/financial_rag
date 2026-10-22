"""Pydantic schema of obligation extraction v2 (docs/KG_V2_PLAN.md section 5.3.2) and of the amendment diff (5.4).

The validators are deliberately lenient about the SHAPE (models answer ``null`` for lists, a string where a list is
expected, "shall" instead of "must" ...) so that a small formatting slip does not cost a request; the CONTENT
checks (ids exist, quotes verbatim, every old obligation handled) are done by the step validators in
``obligations_v2.py``.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

from kg.citations import Citation

SEVERITIES = ("critical", "high", "medium", "low")
DEONTIC = ("must", "must_not", "may")
_DEONTIC_MAP = {"shall": "must", "required": "must", "obligation": "must", "mandatory": "must",
                "must not": "must_not", "shall not": "must_not", "prohibited": "must_not", "forbidden": "must_not",
                "prohibition": "must_not", "permitted": "may", "allowed": "may", "permission": "may", "can": "may"}


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return list(v)


class RequiredDoc(BaseModel):
    doc_types: list[str] = []
    basis: Literal["explicit", "inferred"] = "inferred"
    quote: str | None = None

    @field_validator("doc_types", mode="before")
    @classmethod
    def _types(cls, v):
        return [str(x).strip().lower().replace(" ", "_") for x in _as_list(v) if str(x).strip()]

    @field_validator("basis", mode="before")
    @classmethod
    def _basis(cls, v):
        return "explicit" if str(v or "").strip().lower() == "explicit" else "inferred"


class ObligationV2(BaseModel):
    statement: str
    deontic: str | None = None           # None = not given / unknown: decided from the statement by code
    subject: str = ""
    applies_to: list[str] = []
    action: str = ""
    condition: str | None = None
    exceptions: str | None = None
    deadline_or_frequency: str | None = None
    severity: str = "medium"
    category: str = "policy"
    domain: str | None = None
    required_doc: RequiredDoc | None = None
    evidence_type: str | None = None
    location_hint: str | None = None
    depends_on: list[str] = []
    source_path: str | None = None
    citations: list[Citation] = []

    @field_validator("deontic", mode="before")
    @classmethod
    def _deontic(cls, v):
        v = str(v or "").strip().lower().replace("-", "_")
        v = _DEONTIC_MAP.get(v, _DEONTIC_MAP.get(v.replace("_", " "), v))
        return v if v in DEONTIC else None

    @field_validator("subject", "action", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else str(v)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        v = "" if v is None else str(v).strip()
        return v if v and v.lower() not in ("null", "none", "n/a", "-") else "policy"

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        v = str(v or "medium").strip().lower()
        return v if v in SEVERITIES else "medium"

    @field_validator("applies_to", "depends_on", "citations", mode="before")
    @classmethod
    def _lists(cls, v):
        return _as_list(v)

    @field_validator("required_doc", mode="before")
    @classmethod
    def _rd(cls, v):
        if v in (None, "", [], {}):
            return None
        if isinstance(v, str):
            return {"doc_types": [v], "basis": "inferred"}
        if isinstance(v, list):
            return {"doc_types": v, "basis": "inferred"}
        return v

    @field_validator("condition", "exceptions", "deadline_or_frequency", "evidence_type", "location_hint", "domain",
                     mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        return None if v.lower() in ("", "null", "none", "n/a", "-") else v


class UnitResult(BaseModel):
    id: str
    obligations: list[ObligationV2] = []

    @field_validator("obligations", mode="before")
    @classmethod
    def _obl(cls, v):
        return _as_list(v)


class ExtractBatch(BaseModel):
    results: list[UnitResult]


# ----------------------------------------------------------------------------------------------- amendment diff
class DiffOp(BaseModel):
    op: Literal["unchanged", "modified", "new", "removed"]
    old_id: str | None = None
    new: ObligationV2 | None = None
    what_changed: str | None = None
    reason: str | None = None

    @field_validator("op", mode="before")
    @classmethod
    def _op(cls, v):
        v = str(v or "").strip().lower()
        return {"changed": "modified", "updated": "modified", "added": "new", "deleted": "removed",
                "repealed": "removed", "same": "unchanged", "no_change": "unchanged"}.get(v, v)


class DiffUnit(BaseModel):
    id: str
    operations: list[DiffOp] = []

    @field_validator("operations", mode="before")
    @classmethod
    def _ops(cls, v):
        return _as_list(v)


class DiffBatch(BaseModel):
    results: list[DiffUnit]
