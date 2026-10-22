"""A deterministic stand-in for Gemini used by the v2 tests.

It reads the JSON the prompts embed after ``INPUT:`` and answers like a careful (but simple) model would, using
keyword rules on the real regulation / document texts. It is NOT a quality benchmark: it exists to run every step
of the pipeline end to end on the real data (parsing, validation, storing, graph writing, tasks, UI) without the
network. Real answer quality is judged by the evaluation suites with the real model.

``FakeGemini.calls`` counts requests per purpose; ``.fail`` lets a test make a purpose return garbage.
"""
from __future__ import annotations

import json
import re
from collections import Counter

TRIGGERS = ("უნდა", "ვალდებულია", "აკრძალულია", "შეუძლია", "ვალდებულნი")


def _payload(user: str):
    return json.loads(user.split("INPUT:\n", 1)[1])


def _out(o):
    return {"text": json.dumps(o, ensure_ascii=False), "input_tokens": 100, "output_tokens": 50}


def unit_lines(text: str) -> list[tuple[str | None, str]]:
    """[(path or None, line)] from an extraction-unit text ('[path] text' lines)."""
    out = []
    for ln in text.split("\n"):
        m = re.match(r"^\[([^\]]+)\]\s*(.*)$", ln)
        out.append((m.group(1), m.group(2)) if m else (None, ln))
    return out


def _stems(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[ა-ჰA-Za-z0-9]{4,}", s or "")}


def _words(s: str, n: int = 6) -> str:
    return " ".join(s.split()[:n])


def fake_obligation(path: str, line: str, pv_id: str | None) -> dict | None:
    if not any(t in line for t in TRIGGERS):
        return None
    sev = "critical" if "თანხმობ" in line else ("high" if "ვალიდაც" in line else "medium")
    rd = None
    if "პოლიტიკ" in line:
        i = line.index("პოლიტიკ")
        rd = {"doc_types": ["policy"], "basis": "explicit", "quote": line[max(0, i - 10): i + 25]}
    elif "რეესტრ" in line or "ინვენტარ" in line:
        rd = {"doc_types": ["model_inventory"], "basis": "inferred"}
    elif "ანგარიშ" in line:
        rd = {"doc_types": ["validation_report"], "basis": "inferred"}
    o = {"statement": line[:240], "deontic": "must_not" if "არ უნდა" in line else "must",
         "subject": "კომერციული ბანკი", "applies_to": ["commercial_bank"], "action": line[:80],
         "condition": None, "exceptions": None, "deadline_or_frequency": "ყოველწლიურად" if "ყოველწლ" in line else None,
         "severity": sev, "category": "validation" if "ვალიდაც" in line else "governance", "domain": "model_risk",
         "required_doc": rd, "evidence_type": "clause in the policy",
         "location_hint": "მოდელის მართვის პოლიტიკის შესაბამისი თავი", "depends_on": [], "source_path": path,
         "citations": [{"kind": "provision", "id": pv_id, "quote": _words(line)}] if pv_id else []}
    return o


class FakeGemini:
    def __init__(self):
        self.calls = Counter()
        self.fail: set[str] = set()
        self.handlers = {
            "extract_obligations": self.extract, "amend_diff": self.diff,
            "doc_profile": self.profile, "triage": self.triage, "locate": self.locate, "judge": self.judge,
            "propose": self.propose, "merge": self.merge,
        }
        self.propose_extra = ""                    # appended to every proposal (e.g. an invented number)
        self.merge_conflict = False
        self.propose_unchanged = False
        self.judge_force: dict[str, dict] = {}     # obligation id -> {"verdict":..., "edges":[(clause_id, role)]}
        self.triage_all = None            # e.g. "irrelevant": every obligation gets this relevance
        self.last_user: dict[str, str] = {}

    # -------------------------------------------------------------------- backend protocol
    def __call__(self, model, messages, max_tokens, temperature, json_mode, purpose=None):
        self.calls[purpose] += 1
        self.last_user[purpose] = messages[-1]["content"]
        if purpose in self.fail:
            return {"text": "this is not json", "input_tokens": 1, "output_tokens": 1}
        if purpose == "smoke":
            return _out({"ok": True})
        h = self.handlers.get(purpose)
        if h is None:
            raise RuntimeError(f"FakeGemini has no handler for purpose '{purpose}'")
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user" and "INPUT:" in m["content"])
        return _out(h(_payload(user)))

    # -------------------------------------------------------------------- steps
    def extract(self, ctxs):
        results = []
        for c in ctxs:
            pv = {p["path"]: p["pv_id"] for p in c["unit"]["provisions"]}
            obls = []
            for path, line in unit_lines(c["unit"]["text"]):
                p = path or c["unit"]["path"]
                o = fake_obligation(p, line, pv.get(p))
                if o:
                    obls.append(o)
            results.append({"id": c["id"], "obligations": obls})
        return {"results": results}

    def diff(self, ctxs):
        results = []
        for c in ctxs:
            pv = {p["path"]: p["pv_id"] for p in c["unit"]["provisions"]}
            new_lines = {(p or c["unit"]["path"]): ln for p, ln in unit_lines(c["unit"]["text"])}
            ops, covered = [], set()
            for old in c["previous"]["obligations"]:
                sp = old.get("source_path")
                line = new_lines.get(sp)
                if line is None:
                    ops.append({"op": "removed", "old_id": old["id"], "reason": "ნორმა ამოღებულია"})
                elif old["statement"][:60] in line:
                    ops.append({"op": "unchanged", "old_id": old["id"]})
                    covered.add(sp)
                else:
                    o = fake_obligation(sp, line, pv.get(sp))
                    if o is None:
                        ops.append({"op": "removed", "old_id": old["id"], "reason": "მოთხოვნა აღარ არსებობს"})
                    else:
                        ops.append({"op": "modified", "old_id": old["id"], "new": o,
                                    "what_changed": "ტექსტი შეიცვალა"})
                    covered.add(sp)
            old_paths = {o.get("source_path") for o in c["previous"]["obligations"]}
            for sp, line in new_lines.items():
                if sp in old_paths or sp in covered:
                    continue
                o = fake_obligation(sp, line, pv.get(sp))
                if o:
                    ops.append({"op": "new", "new": o})
            results.append({"id": c["id"], "operations": ops})
        return {"results": results}

    # -------------------------------------------------------------------- document side
    def profile(self, inp):
        pt = (inp.get("parser_doc_type") or "other")
        doc_type = {"policy": "model_risk_policy", "inventory": "model_inventory"}.get(pt, pt)
        units = [re.match(r"^\[([^\]]+)\]\s*(.*)$", ln) for ln in inp["text"].split("\n")]
        units = [m for m in units if m and not m.group(2).startswith("#")]
        cites = []
        if units:
            uid, txt = units[0].group(1), units[0].group(2)
            cites = [{"kind": "clause", "id": uid, "quote": _words(txt, 4)}]
        title = inp.get("title") or ""
        doms = [] if pt == "ethics_guideline" or "model_risk" not in inp["domain_options"] else ["model_risk"]
        return {"purpose": f"ეს დოკუმენტი არის: {title}. იგი განსაზღვრავს მოდელის რისკთან დაკავშირებულ საკითხებს.",
                "doc_type": doc_type, "domains": doms,
                "sections": [{"section_no": s["section_no"], "title": s["title"],
                              "summary": f"განყოფილება: {s['title']}", "goal": "მიზანი"} for s in inp["sections"]],
                "citations": cites}

    def triage(self, inp):
        prof = inp["profile"]
        sec = next((s for s in prof["sections"] if s.get("id")), None)
        cites = [{"kind": "clause", "id": sec["id"], "quote": sec["title"]}] if sec else []
        out = []
        for o in inp["obligations"]:
            rd = o.get("required_doc") or {}
            types = [t.lower() for t in (rd.get("doc_types") or [])]
            dt = prof["doc_type"]
            if self.triage_all:
                rel = self.triage_all
            elif types and any(dt == t or dt.endswith("_" + t) or t.endswith("_" + dt) for t in types):
                rel = "relevant"
            elif types:
                rel = "irrelevant"
            else:
                rel = "possibly"
            out.append({"obligation_id": o["obligation_id"], "relevance": rel, "reason": "მიზეზი.",
                        "citations": cites})
        return {"results": out}

    def locate(self, inp):
        out = []
        for o in inp["obligations"]:
            st = _stems(o["statement"] + " " + (o.get("location_hint") or ""))
            scored = sorted(((len(st & _stems(s["title"] + " " + (s.get("summary") or ""))), s["section_no"])
                             for s in inp["sections"]), reverse=True)
            out.append({"obligation_id": o["obligation_id"],
                        "sections": [{"section_no": n, "reason": "ახლოსაა."} for sc, n in scored[:3] if sc > 0]})
        return {"results": out}

    def judge(self, inp):
        user = self.last_user["judge"]
        clauses = {}
        if "DOCUMENT:\n" in user:
            doc = user.split("DOCUMENT:\n", 1)[1].split("\n\nINPUT:\n", 1)[0]
            for ln in doc.split("\n"):
                m = re.match(r"^\[([^\]]+)\]\s*(#)?(?:(\d+(?:\.\d+)*)\s+)?(.*)$", ln)
                if m and not m.group(2):
                    clauses[m.group(1)] = m.group(4)
        out = []
        for o in inp["obligations"]:
            oid = o["obligation_id"]
            pool = ({c["id"]: c["text"] for c in o["candidates"]} if "candidates" in o else clauses)
            src = o.get("source") or {}
            cites = [{"kind": "provision", "id": src["pv_id"], "quote": _words(src.get("text", ""), 4)}] if src else []
            f = self.judge_force.get(oid)
            if f:
                edges = [{"clause_id": c, "role": r, "quote": _words(pool.get(c, ""), 4), "why": "იძულებითი."}
                         for c, r in f["edges"]]
                out.append({"obligation_id": oid, "edges": edges, "verdict": f["verdict"],
                            "missing": None if f["verdict"] == "full" else "აკლია.", "rationale": "იძულებითი.",
                            "citations": cites})
                continue
            st = _stems(o["statement"])
            scored = sorted(((len(st & _stems(t)) / max(1, len(st)), cid) for cid, t in pool.items()), reverse=True)
            edges, verdict = [], "none"
            for sc, cid in scored[:2]:
                if sc >= 0.5:
                    edges.append({"clause_id": cid, "role": "full", "quote": _words(pool[cid], 5), "why": "შეესაბამება."})
                elif sc >= 0.25:
                    edges.append({"clause_id": cid, "role": "partial", "quote": _words(pool[cid], 5), "why": "ნაწილობრივ."})
            for e in o.get("fixed_edges", []):
                if e["role"] == "rejected":
                    edges = [x for x in edges if x["clause_id"] != e["clause_id"]]
                    continue
                if e["clause_id"] not in {x["clause_id"] for x in edges}:
                    edges.append({"clause_id": e["clause_id"], "role": e["role"], "quote": None, "why": "მომხმარებლის."})
            roles = [e["role"] for e in edges]
            if "contradicts" in roles:
                verdict = "contradicts"
            elif "full" in roles:
                verdict = "full"
            elif roles:
                verdict = "partial"
            out.append({"obligation_id": oid, "edges": edges, "verdict": verdict,
                        "missing": None if verdict == "full" else "აკლია კონკრეტული დეტალები.",
                        "rationale": "ავტომატური შეფასება.", "citations": cites})
        return {"results": out}

    def propose(self, inp):
        out = []
        for it in inp["items"]:
            src = it.get("source") or {}
            cites = [{"kind": "provision", "id": src["pv_id"], "quote": _words(src.get("text", ""), 4)}] if src else []
            heading = None
            if it["kind"] == "adjust_clause":
                t = it["target"]
                text = t["text"] if self.propose_unchanged else (t["text"] + " გარდა ამისა: " + it["statement"][:150])
                cites.append({"kind": "clause", "id": t["id"], "quote": _words(t["text"], 4)})
            elif it["kind"] == "add_section":
                heading = "ახალი დებულება"
                text = it["statement"][:200]
                cites.append({"kind": "clause", "id": it["anchor"]["id"], "quote": _words(it["anchor"]["text"], 4)})
            else:
                heading = "ახალი დოკუმენტი"
                text = "1. მიზანი\n2. " + it["statement"][:100]
            out.append({"obligation_id": it["obligation_id"], "heading": heading,
                        "proposed_text": text + self.propose_extra, "explanation": "ცვლილება საჭიროა.",
                        "citations": cites})
        return {"results": out}

    def merge(self, inp):
        return {"results": [{"clause_id": it["clause_id"],
                             "merged_text": it["original"] + " [გაერთიანებული: " + " | ".join(
                                 p["text"][-40:] for p in it["proposals"]) + "]",
                             "conflicts": ["ეწინააღმდეგება."] if self.merge_conflict else [],
                             "contributions": [{"proposal_id": p["proposal_id"], "change": "დაემატა: " + p["text"][-20:]}
                                               for p in it["proposals"]]}
                            for it in inp["items"]]}
