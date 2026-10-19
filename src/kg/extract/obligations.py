"""``extract-obligations``: provisions of the obligation acts (4964423) -> Obligation nodes (LLM, batched).

Extraction units
    * a paragraph together with its sub-points (so "must cover: a) b) c)" stays in one piece),
    * an article that has no paragraphs (definitions, scope ...) together with its direct sub-points,
    * article lead text that stands alone.
A unit version starts whenever any member provision changes. V0 units are all extracted; later versions only the
units that changed (taken from the version history). Unchanged units need no copy: an Obligation is valid over the
version span of its unit, so it is carried forward implicitly (docs/KG_ARCHITECTURE.md section 4.5).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

from kg.ids import act_id, display_path, path_sort_key, prov_id, pv_id
from kg.ingest.consolidate import is_descendant
from kg.llm import BudgetExhausted, LLMValidationError

PROMPT_PATH = Path(__file__).parent / "prompts" / "obligations.md"
SEVERITIES = ("critical", "high", "medium", "low")


class OblItem(BaseModel):
    statement: str
    subject: str = ""
    action: str = ""
    condition: str | None = None
    deadline_or_frequency: str | None = None
    severity: str = "medium"
    category: str = "policy"
    source_path: str | None = None

    @field_validator("severity")
    @classmethod
    def _sev(cls, v):
        v = (v or "medium").strip().lower()
        return v if v in SEVERITIES else "medium"


class UnitResult(BaseModel):
    id: str
    category: str = "policy"
    obligations: list[OblItem] = []


class BatchResult(BaseModel):
    results: list[UnitResult]


@dataclass
class Unit:
    unit_path: str
    date: str
    end: str | None
    members: list[tuple[str, str]]          # (path, text) in order, active at `date`
    text_for_llm: str = ""
    uid: str = ""

    @property
    def text_hash(self) -> str:
        return hashlib.sha1(self.text_for_llm.encode("utf-8")).hexdigest()[:10]


def _unit_defs(paths: list[str]) -> dict[str, list[str]]:
    """unit path -> member paths (ordered)."""
    paths = sorted(set(paths), key=path_sort_key)
    arts = [p for p in paths if "/" not in p and p.startswith("art")]
    units: dict[str, list[str]] = {}
    for a in arts:
        paras = [p for p in paths if p.startswith(a + "/p") and p.count("/") == 1]
        if paras:
            lead = a
            units[lead] = [a]                       # lead text of the article (usually empty)
            for p in paras:
                units[p] = [p] + [q for q in paths if is_descendant(q, p)]
            # sub-points directly under the article (rare) belong to the article lead unit
            units[lead] += [q for q in paths if q.startswith(a + "/s")]
        else:
            units[a] = [a] + [q for q in paths if q.startswith(a + "/s")]
    return units


def build_units(store, act_doc: str) -> list[Unit]:
    """All unit versions of an act, with the text at each version date."""
    pvs: dict[str, list[dict]] = {}
    for n in store.nodes("ProvisionVersion"):
        if str(n["props"].get("act")) == str(act_doc):
            pvs.setdefault(n["props"]["path"], []).append(n)
    for v in pvs.values():
        v.sort(key=lambda n: n["valid_from"])
    defs = _unit_defs([p for p in pvs if p.startswith("art")])
    out: list[Unit] = []
    for upath, members in sorted(defs.items(), key=lambda kv: path_sort_key(kv[0])):
        dates = sorted({n["valid_from"] for m in members for n in pvs[m]})
        for i, d in enumerate(dates):
            end = dates[i + 1] if i + 1 < len(dates) else None
            parts = []
            for m in members:
                act = [n for n in pvs[m] if n["valid_from"] <= d and (n["valid_to"] is None or d < n["valid_to"])
                       and n["props"]["status"] == "active"]
                if act:
                    p = act[0]["props"]
                    txt = f"{p.get('label','')} {p.get('text','')}".strip()
                    if m == upath and p.get("heading"):
                        txt = f"{p['heading']}\n{txt}".strip()
                    if txt:
                        parts.append((m, txt))
            if not parts or all(m == upath and txt == (pvs[m][0]["props"].get("heading") or "") for m, txt in parts):
                continue          # nothing but an article heading
            u = Unit(upath, d, end, parts)
            head = display_path(upath)
            lines = []
            for m, t in parts:
                lines.append(t if m == upath else f"[{m}] {t}")
            u.text_for_llm = "\n".join(lines)
            u.uid = f"{upath}@{d}"
            out.append(u)
    return out


def units_to_extract(units: list[Unit], base_date: str, only_changed: bool = True) -> list[Unit]:
    # V0 = all units at the first date; later dates = changed units only (each date row is a change by construction)
    return [u for u in units if (u.date == base_date) or only_changed]


def batches(items: list[Unit], size: int) -> list[list[Unit]]:
    items = sorted(items, key=lambda u: (u.date, path_sort_key(u.unit_path)))
    return [items[i:i + size] for i in range(0, len(items), size)]


def _load_system() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def extract_obligations(cfg, store, run, llm, dry_run: bool = False, act_docs: list[str] | None = None) -> dict:
    from kg.llm import system_user

    size = int(cfg.get_in("extraction.obligation_batch", 10))
    summary = {"units": 0, "batches": 0, "obligations": 0, "non_obligation_units": 0, "failed_units": 0,
               "estimated_requests": 0}
    ent = f"ent:{cfg['entity_type']}"
    store.upsert_node(ent, "EntityType", {"name": cfg["entity_type"]})
    for act_doc in (act_docs or cfg["obligation_acts"]):
        act_doc = str(act_doc)
        units = build_units(store, act_doc)
        if not units:
            continue
        base_date = min(u.date for u in units)
        todo = units_to_extract(units, base_date)
        summary["units"] += len(todo)
        bs = batches(todo, size)
        summary["batches"] += len(bs)
        summary["estimated_requests"] += len(bs)
        run.log("obligation_plan", act=act_doc, units=len(todo), batches=len(bs), base_date=base_date)
        if dry_run:
            continue
        system = _load_system()
        for bi, batch in enumerate(bs):
            payload = [{"id": u.uid, "path": display_path(u.unit_path), "text": u.text_for_llm} for u in batch]
            user = ("Provisions:\n" + json.dumps(payload, ensure_ascii=False, indent=1))
            try:
                res = llm.call("extract_obligations", system_user(system, user), BatchResult)
            except LLMValidationError as exc:
                run.log("batch_failed", batch=bi, error=str(exc)[:300])
                summary["failed_units"] += len(batch)
                continue
            by_id = {r.id: r for r in res.parsed.results}
            nodes, edges = [], []
            for u in batch:
                r = by_id.get(u.uid)
                if r is None:
                    summary["failed_units"] += 1
                    run.log("unit_missing", unit=u.uid)
                    continue
                n = 0
                members = dict(u.members)
                for k, o in enumerate(r.obligations, 1):
                    oid = f"obl:{act_doc}/{u.unit_path}#{k}@{u.date}"
                    src = o.source_path if o.source_path in members else u.unit_path
                    if src not in members:
                        src = next(iter(members))
                    nodes.append((oid, "Obligation", {
                        "act": act_doc, "unit": u.unit_path, "statement": o.statement, "subject": o.subject,
                        "action": o.action, "condition": o.condition,
                        "deadline_or_frequency": o.deadline_or_frequency, "severity": o.severity,
                        "category": o.category, "source_path": src, "entity_types": [cfg["entity_type"]],
                        "unit_date": u.date}, u.date, u.end))
                    # IMPOSES from the version of the source provision that is active at the unit-version date
                    pv_node = _active_pv(store, act_doc, src, u.date)
                    if pv_node:
                        edges.append((pv_node["id"], oid, "IMPOSES", {}, u.date, u.end))
                    edges.append((oid, ent, "APPLIES_TO", {}, u.date, u.end))
                    n += 1
                status = "non_obligation" if (n == 0) else "extracted"
                nodes.append((f"unit:{act_doc}/{u.unit_path}@{u.date}", "ExtractionUnit", {
                    "act": act_doc, "unit": u.unit_path, "status": status, "category": r.category,
                    "n_obligations": n, "text_hash": u.text_hash}, u.date, u.end))
                summary["obligations"] += n
                summary["non_obligation_units"] += 1 if n == 0 else 0
            store.upsert_nodes(nodes)
            store.upsert_edges(edges)
            run.log("batch_done", batch=bi, units=len(batch), obligations=sum(1 for n in nodes if n[1] == "Obligation"))
            run.count("batches")
    return summary


def _active_pv(store, act_doc: str, path: str, date: str):
    # ids are deterministic: pv:{act}/{path}@{valid_from}; find the one valid at `date`
    prov = prov_id(act_doc, path)
    best = None
    for item in store.neighbors(prov, "VERSION_OF", "in"):
        n = item["node"]
        if n["valid_from"] <= date and (n["valid_to"] is None or date < n["valid_to"]):
            best = n
    return best
