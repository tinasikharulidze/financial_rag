"""Context builder for obligation extraction (docs/KG_V2_PLAN.md section 5.3.1) - no LLM.

For one extraction unit it collects what a reader needs to understand the unit:

    act title / short name / default domain and entity types      <- Act node + config
    article number, title and lead text                           <- article ProvisionVersion at the unit date
    the unit text with the ids of its provisions                  <- as before, plus pv ids (citations point to them)
    definitions used                                              <- definitions article, matched by the defined term
    referenced provisions (one hop of REFERS_TO)                  <- text capped; placeholders say "not available"
    previous version of the unit and its obligations              <- only for the amendment diff

The model is told that ONLY the unit is a source of obligations; everything else is context.
``context_ids(ctx)`` lists every node id that may be cited, so citations can be checked against it.
"""
from __future__ import annotations

import re
from collections import defaultdict

from kg.extract.obligations import Unit
from kg.ids import act_id, display_path, prov_id
from kg.ingest.titles import article_display, article_of

_DEF_SPLIT = re.compile(r"^(?P<term>[^–—\n]{2,90}?)\s+[–—]\s+(?P<body>.+)$", re.S)


def _stems(term: str) -> list[str]:
    """Georgian words change their endings, so a term is matched through word stems (the last two letters dropped)."""
    out = []
    for w in re.findall(r"[ა-ჰA-Za-z0-9]+", term.lower()):
        out.append(w[:max(3, len(w) - 2)])
    return out


def term_in_text(term: str, text_lower: str) -> bool:
    stems = _stems(term)
    return bool(stems) and all(s in text_lower for s in stems)


class ContextBuilder:
    def __init__(self, cfg, store, act_doc: str):
        self.cfg, self.store, self.act = cfg, store, str(act_doc)
        self.cap = int(cfg.get_in("extraction.context_chars", 800))
        self.pvs: dict[str, list[dict]] = defaultdict(list)
        for n in store.nodes("ProvisionVersion", act=self.act):
            self.pvs[n["props"]["path"]].append(n)
        for v in self.pvs.values():
            v.sort(key=lambda n: n["valid_from"])
        self.refs: dict[str, list[dict]] = defaultdict(list)
        for e in store.edges("REFERS_TO"):
            if e["src"].startswith(f"prov:{self.act}/"):
                self.refs[e["src"]].append(e)
        self.act_node = store.get_node(act_id(self.act)) or {"props": {}}
        ac = cfg["acts"].get(self.act, {})
        self.domains = list(ac.get("domains") or [])
        self.applies_to = list(ac.get("applies_to") or [cfg.get("entity_type", "commercial_bank")])
        self._defs: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------ lookups
    def pv_at(self, path: str, date: str, act: str | None = None) -> dict | None:
        vs = self.pvs.get(path, []) if act in (None, self.act) else [
            n for n in self.store.nodes("ProvisionVersion", act=act, path=path)]
        best = None
        for n in sorted(vs, key=lambda n: n["valid_from"]):
            if n["valid_from"] <= date and (n["valid_to"] is None or date < n["valid_to"]) \
                    and n["props"].get("status") == "active":
                best = n
        return best

    def definitions(self, date: str) -> list[dict]:
        """[{'id': pv id, 'term', 'text'}] of the definitions article valid on ``date``."""
        if date in self._defs:
            return self._defs[date]
        out = []
        for path in sorted(self.pvs):
            if "/" in path:
                continue
            head = self.pv_at(path, date)
            if head and "განმარტ" in (head["props"].get("heading") or ""):
                for sp in sorted(p for p in self.pvs if p.startswith(path + "/")):
                    n = self.pv_at(sp, date)
                    if not n:
                        continue
                    m = _DEF_SPLIT.match((n["props"].get("text") or "").strip())
                    if m:
                        out.append({"id": n["id"], "term": m.group("term").strip(), "text": n["props"]["text"].strip()})
        self._defs[date] = out
        return out

    # ------------------------------------------------------------------ pieces
    def _references(self, unit: Unit) -> list[dict]:
        own = {p for p, _ in unit.members}
        seen, out = set(), []
        for path, _ in unit.members:
            for e in self.refs.get(prov_id(self.act, path), []):
                if not ((e["valid_from"] is None or e["valid_from"] <= unit.date)
                        and (e["valid_to"] is None or unit.date < e["valid_to"])):
                    continue
                dst = e["dst"]
                if dst in seen:
                    continue
                seen.add(dst)
                node = self.store.get_node(dst)
                if node is None:
                    continue
                t = node["type"]
                if t == "Provision":
                    p = node["props"]
                    if p["path"] in own:
                        continue
                    pv = self.pv_at(p["path"], unit.date, str(p["act"]))
                    if pv is not None:
                        out.append({"id": pv["id"], "display": p.get("display") or display_path(p["path"]),
                                    "text": (pv["props"].get("text") or "")[: self.cap], "available": True,
                                    "from": path})
                    else:
                        res = (e["props"] or {}).get("resolved_to")
                        parent = self.store.get_node(res) if res else None
                        ppv = self.pv_at(parent["props"]["path"], unit.date, str(parent["props"]["act"])) if parent else None
                        out.append({"id": ppv["id"] if ppv else dst,
                                    "display": p.get("display") or display_path(p["path"]),
                                    "text": "(ტექსტი მიუწვდომელია)" if not ppv else
                                            "(exact provision not in the corpus; its parent says) "
                                            + (ppv["props"].get("text") or "")[: self.cap],
                                    "available": False, "from": path})
                elif t in ("ExternalAct", "ExternalProvision"):
                    out.append({"id": dst, "display": node["props"].get("title") or dst, "text": "",
                                "available": False, "external": True, "from": path})
                elif t == "Act":
                    out.append({"id": dst, "display": node["props"].get("title") or dst, "text": "",
                                "available": False, "from": path})
        return out[:8]

    def context(self, unit: Unit, previous: Unit | None = None, prev_obligations: list[dict] | None = None) -> dict:
        art = article_of(unit.unit_path)
        art_pv = self.pv_at(art, unit.date)
        art_heading = ((art_pv or {}).get("props", {}).get("article_title")
                       or (art_pv or {}).get("props", {}).get("heading") or "")
        provisions = []
        for path, _txt in unit.members:
            pv = self.pv_at(path, unit.date)
            provisions.append({"path": path, "display": display_path(path), "pv_id": pv["id"] if pv else None})
        ltext = unit.text_for_llm.lower()
        defs = [d for d in self.definitions(unit.date) if term_in_text(d["term"], ltext)]
        defs.sort(key=lambda d: -len(d["term"]))
        lead = ""
        if art_pv and art != unit.unit_path:
            lead = (art_pv["props"].get("text") or "")[: self.cap]
        ctx = {
            "id": unit.uid,
            "act": {"title": self.act_node["props"].get("title"), "short": self.act_node["props"].get("short"),
                    "default_domains": self.domains, "default_applies_to": self.applies_to},
            "article": {"display": article_display(unit.unit_path), "title": art_heading, "lead": lead},
            "unit": {"path": unit.unit_path, "display": display_path(unit.unit_path), "date": unit.date,
                     "text": unit.text_for_llm, "provisions": provisions},
            "definitions": [{"id": d["id"], "term": d["term"], "text": d["text"][:300]} for d in defs[:5]],
            "references": self._references(unit),
        }
        if previous is not None:
            ctx["previous"] = {"date": previous.date, "text": previous.text_for_llm,
                               "obligations": [compact_obligation(o) for o in (prev_obligations or [])]}
        return ctx


def compact_obligation(o: dict) -> dict:
    p = o["props"]
    keys = ("statement", "deontic", "subject", "action", "condition", "exceptions", "deadline_or_frequency",
            "severity", "source_path", "required_doc")
    return {"id": o["id"], **{k: p.get(k) for k in keys if p.get(k) not in (None, "", [])}}


def context_ids(ctx: dict) -> set[str]:
    """Every node id the model may cite for this unit."""
    ids = {p["pv_id"] for p in ctx["unit"]["provisions"] if p["pv_id"]}
    ids |= {d["id"] for d in ctx["definitions"]}
    ids |= {r["id"] for r in ctx["references"]}
    return ids
