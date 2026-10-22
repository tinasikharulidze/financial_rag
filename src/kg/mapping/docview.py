"""A read-only view of one company document version: units, sections, text and size estimates.

Used by the profile step, locate, judge and proposals. Sections are the parser's heading units; every unit belongs
to the section of the last heading before it (by ``order``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kg.company.ingest import MAPPED_TYPES


@dataclass
class DocView:
    doc_id: str
    props: dict
    units: list[dict]                       # all units in reading order: {id, section_no, heading, unit_type, text, page, order, section}
    sections: list[dict] = field(default_factory=list)   # {section_no, title, id (heading unit), clause_ids}
    by_id: dict = field(default_factory=dict)

    @property
    def mapped(self) -> list[dict]:
        return [u for u in self.units if u["unit_type"] in MAPPED_TYPES]

    def section_nos(self) -> list[str]:
        return [s["section_no"] for s in self.sections]

    def section(self, section_no: str) -> dict | None:
        return next((s for s in self.sections if s["section_no"] == section_no), None)

    def clauses_of(self, section_nos: list[str]) -> list[dict]:
        """Mapped units of the given sections, including their sub-sections ("5" also returns "5.1", "5.2.1" ...)."""
        want = [str(x) for x in section_nos]
        return [u for u in self.mapped if u["section"] and any(
            u["section"] == w or u["section"].startswith(w + ".") for w in want)]


def load_doc(store, doc_id: str) -> DocView | None:
    node = store.get_node(doc_id)
    if node is None:
        return None
    units = []
    for it in store.neighbors(doc_id, "HAS_CLAUSE"):
        n = it["node"]
        p = n["props"]
        units.append({"id": n["id"], "section_no": p.get("section_no"), "heading": p.get("heading"),
                      "unit_type": p.get("unit_type"), "text": p.get("text") or "", "page": p.get("page"),
                      "order": p.get("order", 0), "bbox": p.get("bbox"), "section": None})
    units.sort(key=lambda u: u["order"])
    sections: list[dict] = []
    cur = None
    for u in units:
        if u["unit_type"] == "heading" and u["section_no"]:
            cur = {"section_no": u["section_no"], "title": u["text"], "id": u["id"], "clause_ids": []}
            sections.append(cur)
        u["section"] = cur["section_no"] if cur else None
        if cur is not None and u["unit_type"] != "heading":
            cur["clause_ids"].append(u["id"])
    return DocView(doc_id, node["props"], units, sections, {u["id"]: u for u in units})


def render_unit(u: dict) -> str:
    sec = f"{u['section_no']} " if u.get("section_no") and u["unit_type"] != "heading" else ""
    tag = "#" if u["unit_type"] == "heading" else ""
    return f"[{u['id']}] {tag}{sec}{u['text']}".strip()


def doc_text(view: DocView, mapped_only: bool = False, cap: int | None = None) -> str:
    units = [u for u in view.units if u["unit_type"] not in ("signature",)]
    if mapped_only:
        units = [u for u in units if u["unit_type"] in MAPPED_TYPES or u["unit_type"] == "heading"]
    lines = [render_unit(u) if cap is None else render_unit(u)[:cap] for u in units]
    return "\n".join(lines)


def estimate_tokens(cfg, text: str) -> int:
    return int(len(text) / float(cfg.get_in("mapping.chars_per_token", 2.2))) + 1


def fits_whole_doc(cfg, view: DocView) -> bool:
    """The judge's whole-document mode: the text it really receives (``mapped_only``), estimated with the stricter of
    ``mapping.chars_per_token`` and ``llm.chars_per_token`` (the pacing estimate)."""
    cpt = min(float(cfg.get_in("mapping.chars_per_token", 2.2)), float(cfg.get_in("llm.chars_per_token", 2.2)))
    tokens = int(len(doc_text(view, mapped_only=True)) / cpt) + 1
    return tokens <= int(cfg.get_in("mapping.whole_doc_max_tokens", 30000))


def section_digest(view: DocView, chars_per_section: int = 600) -> str:
    """The first characters of every section (profile input for documents that exceed the whole-document limit)."""
    out = []
    for s in view.sections:
        body = " ".join(view.by_id[c]["text"] for c in s["clause_ids"])[:chars_per_section]
        out.append(f"## {s['section_no']} {s['title']}\n{body}")
    return "\n".join(out)
