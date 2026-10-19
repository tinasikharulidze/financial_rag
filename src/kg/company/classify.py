"""Document classification (doc_type): regex on the title first, LLM fallback."""
from __future__ import annotations

import re

from pydantic import BaseModel

DOC_TYPES = ["policy", "inventory", "validation_report", "monitoring_report", "ethics_guideline", "audit_report",
             "supervisory_application", "other"]

# order matters: the first match wins
TITLE_RULES = [
    ("audit_report", r"აუდიტის\s+ანგარიშ|audit report"),
    ("validation_report", r"ვალიდაციის\s+ანგარიშ|validation report"),
    ("monitoring_report", r"მონიტორინგის[\s\S]{0,30}ანგარიშ|monitoring report"),
    ("supervisory_application", r"თანხმობის\s+მიღების|განაცხადი\s+საზედამხედველო|consent application"),
    ("inventory", r"რეესტრ|ინვენტარ|inventory|register"),
    ("ethics_guideline", r"ეთიკურ|ეთიკის\s+(?:პრინციპ|სახელმძღვანელო)|ethic"),
    ("policy", r"პოლიტიკა|policy|პროცედურა"),
]


class ClassifyResult(BaseModel):
    doc_type: str = "other"
    reason: str = ""


def classify_by_title(title: str) -> str | None:
    t = (title or "").lower()
    for dt, pat in TITLE_RULES:
        if re.search(pat, t, re.I):
            return dt
    return None


def classify(title: str, text_head: str = "", llm=None) -> tuple[str, str]:
    """-> (doc_type, method)."""
    dt = classify_by_title(title)
    if dt:
        return dt, "regex"
    if llm is not None:
        from kg.llm import LLMValidationError, system_user
        system = ("Classify an internal bank document about model risk. Allowed doc_type values: "
                  + ", ".join(DOC_TYPES) + '. Return JSON {"doc_type": "...", "reason": "..."}.')
        try:
            res = llm.call("classify_doc", system_user(system, f"Title: {title}\n\nStart of the text:\n{text_head[:1500]}"),
                           ClassifyResult, max_tokens=300)
            dt = res.parsed.doc_type if res.parsed.doc_type in DOC_TYPES else "other"
            return dt, "llm"
        except LLMValidationError:
            pass
    return "other", "default"
