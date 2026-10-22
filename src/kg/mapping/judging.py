"""Mapping step 4 (LLM): does the document fulfil the obligation, and which clauses are the evidence? (plan 7.4)

* Two modes, chosen per document by size: ``whole`` (the full document is in the prompt) or ``candidates`` (located
  sections + hybrid-search hits per obligation).
* Output per obligation: several edges (clause, role, verbatim quote), a verdict and a Georgian explanation.
* Code enforces: known clause ids; verbatim quotes (an unverifiable quote is dropped, the edge stays); a
  ``contradicts`` edge makes the verdict ``contradicts``; ``full`` needs evidence; user edges beat the model.
* Results: ``coverage`` rows and ``COVERED_BY`` edges (``source = llm``); rows and edges of the user are never touched.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, field_validator

from kg.citations import Citation, dump, quote_in_text, verify_citations
from kg.llm import system_user
from kg.mapping.docview import DocView, doc_text, fits_whole_doc
from kg.mapping.filter import cause_of
from kg.mapping.search import candidates_for, doc_index
from kg.steps import load_prompt, run_step
from kg.store.v2tables import jdump

PROMPT = Path(__file__).parent / "prompts" / "judge.md"
STEP = "judge"
ROLES = ("full", "partial", "contradicts")
VERDICTS = ("full", "partial", "contradicts", "none")
STRENGTH = {"contradicts": 3, "full": 2, "partial": 1}


class JEdge(BaseModel):
    clause_id: str
    role: str = "partial"
    quote: str | None = None
    why: str = ""
    quote_verified: bool = False

    @field_validator("role", mode="before")
    @classmethod
    def _r(cls, v):
        v = str(v or "").strip().lower()
        return v if v in ROLES else "partial"


class JItem(BaseModel):
    obligation_id: str
    edges: list[JEdge] = []
    verdict: str = "none"
    missing: str | None = None
    rationale: str = ""
    citations: list[Citation] = []
    invalid: bool = False
    problems: list[str] = []

    @field_validator("verdict", mode="before")
    @classmethod
    def _v(cls, v):
        v = str(v or "").strip().lower()
        return v if v in VERDICTS else "none"

    @field_validator("edges", "citations", mode="before")
    @classmethod
    def _l(cls, v):
        return [] if v in (None, "") else (v if isinstance(v, list) else [v])

    @field_validator("missing", mode="before")
    @classmethod
    def _m(cls, v):
        return None if v in (None, "", "null") else str(v)


class JudgeBatch(BaseModel):
    results: list[JItem] = []


# ------------------------------------------------------------------------------------------------ validation
def make_validator(store, view: DocView, allowed: dict[str, set[str]], src_pvs: dict[str, set[str]],
                   rejected: dict[str, set[str]] | None = None):
    def validate(out: JudgeBatch) -> list[str]:
        hard, notes, seen, keep = [], [], set(), []
        for r in out.results:
            oid = r.obligation_id
            if oid not in allowed or oid in seen:
                continue
            seen.add(oid)
            probs: list[str] = []
            best: dict[str, JEdge] = {}
            for e in r.edges:
                if e.clause_id in (rejected or {}).get(oid, ()):
                    continue                                   # the user said this clause does not cover it
                if e.clause_id not in allowed[oid]:
                    probs.append(f"{oid}: clause id '{e.clause_id}' was not offered for this obligation")
                    continue
                if e.quote:
                    u = view.by_id[e.clause_id]
                    e.quote_verified = quote_in_text(e.quote, f"{u.get('heading') or ''} {u['text']}")
                    if not e.quote_verified:
                        notes.append(f"note: {oid}: quote not verbatim in {e.clause_id}, dropped")
                        e.quote = None
                cur = best.get(e.clause_id)
                if cur is None or STRENGTH[e.role] > STRENGTH[cur.role]:
                    best[e.clause_id] = e
            r.edges = list(best.values())
            roles = {e.role for e in r.edges}
            if "contradicts" in roles and r.verdict != "contradicts":
                notes.append(f"note: {oid}: verdict set to contradicts (a clause contradicts)")
                r.verdict = "contradicts"
            if r.verdict == "full" and not roles & {"full", "partial"}:
                probs.append(f"{oid}: verdict 'full' needs at least one full or partial edge")
            if r.verdict == "none" and roles:
                notes.append(f"note: {oid}: verdict none with edges, set to partial")
                r.verdict = "partial"
            if r.verdict == "full":
                r.missing = None
            elif not (r.missing or "").strip() and r.verdict != "contradicts":
                notes.append(f"note: {oid}: no `missing` text")
            good, errs = verify_citations(store, r.citations, allowed_ids=src_pvs.get(oid), soft_quotes=True)
            r.citations = good
            notes += [x if x.startswith("note:") else "note: " + x for x in errs]
            r.invalid, r.problems = bool(probs), probs
            hard += probs
            keep.append(r)
        out.results = keep
        miss = sorted(set(allowed) - seen)
        if miss:
            hard.append("missing results for obligation ids: " + ", ".join(miss))
        return hard + notes
    return validate


# ------------------------------------------------------------------------------------------------ rules
def final_verdict(llm_verdict: str, edges: list[dict]) -> str:
    """Precedence user > rule > llm (plan section 10). ``edges``: dicts with role and source."""
    user = [e for e in edges if e.get("source") == "user"]
    if any(e["role"] == "contradicts" for e in user):
        return "contradicts"
    if any(e["role"] == "full" for e in user):
        return "full"
    if any(e["role"] == "contradicts" for e in edges):
        return "contradicts"
    return llm_verdict


def user_edges(store, version_id: str, obligation_ids: list[str] | None = None) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in store.edges("COVERED_BY"):
        p = e["props"]
        if p.get("source") == "user" and p.get("doc_version") == version_id and (
                obligation_ids is None or e["src"] in obligation_ids):
            out.setdefault(e["src"], []).append({"clause_id": e["dst"], "role": p.get("role"), "source": "user"})
    return out


def source_of(store, ob: dict) -> dict | None:
    """The provision (version) that imposes the obligation: the one whose IMPOSES edge is still open, else the last."""
    its = store.neighbors(ob["id"], "IMPOSES", "in")
    if not its:
        return None
    its.sort(key=lambda it: (it["edge"]["valid_to"] is None, it["edge"]["valid_from"] or ""))
    p = its[-1]["node"]["props"]
    return {"pv_id": its[-1]["node"]["id"], "display": p.get("display"), "text": (p.get("text") or "")[:900]}


def amendment_of(store, ob: dict) -> str | None:
    p = ob["props"]
    if p.get("amendment_id"):
        return p["amendment_id"]
    for it in store.neighbors(ob["id"], "SUPERSEDES", "out"):
        if it["edge"]["props"].get("amendment"):
            return it["edge"]["props"]["amendment"]
    return None


# ------------------------------------------------------------------------------------------------ writing
def write_results(store, run, version_id: str, v_doc: str | None, batch: list[dict], items: dict[str, JItem | None],
                  user: dict[str, list[dict]], output_id: int | None):
    old = [e["id"] for e in store.edges("COVERED_BY")
           if e["src"] in {o["id"] for o in batch} and e["props"].get("doc_version") == version_id
           and e["props"].get("source") == "llm"]
    store.delete_edge_ids(old)
    new_edges = []
    for o in batch:
        oid = o["id"]
        it = items.get(oid)
        cause = cause_of(o, v_doc)
        amend = amendment_of(store, o) if cause == "amendment" else None
        ue = user.get(oid, [])
        if it is None or it.invalid:
            verdict, missing, rationale, cites, invalid = "undecided", None, "; ".join(it.problems) if it else "", [], 1
            edges = []
        else:
            if it.verdict == "contradicts" and it.edges and not any(e.role == "contradicts" for e in it.edges):
                # "contradicts" with only "partial" clauses: the cited partial clauses are where it contradicts
                for e in it.edges:
                    if e.role == "partial":
                        e.role = "contradicts"
            edges = [{"clause_id": e.clause_id, "role": e.role, "source": "llm"} for e in it.edges]
            verdict = final_verdict(it.verdict, edges + ue)
            missing, rationale, cites, invalid = it.missing, it.rationale, dump(it.citations), 0
            if verdict == "full" and any(x["role"] == "full" for x in ue) and it.verdict != "full":
                missing = None
            for e in it.edges:
                new_edges.append((oid, e.clause_id, "COVERED_BY",
                                  {"role": e.role, "coverage": e.role, "source": "llm", "doc_version": version_id,
                                   "run": run.id, "output_id": output_id, "quote": e.quote, "why": e.why}, None, None,
                                  f"{oid}|COVERED_BY|{e.clause_id}|llm"))
        store.x("INSERT OR REPLACE INTO coverage(doc_version_id,obligation_id,verdict,missing,rationale,citations,"
                "cause,amendment_id,llm_output_id,source,invalid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, oid, verdict, missing, rationale, jdump(cites), cause, amend, output_id, "llm", invalid))
    store.upsert_edges(new_edges)


# ------------------------------------------------------------------------------------------------ the step
def judge_document(cfg, store, run, llm, version_id: str, view: DocView, profile: dict, obligations: list[dict],
                   located: dict[str, list[str]], v_doc: str | None, force: bool = False,
                   retry_invalid: bool = False) -> dict:
    system, version = load_prompt(PROMPT)
    size = int(cfg.get_in("mapping.judge_batch", 8))
    whole = fits_whole_doc(cfg, view)
    mode = "whole" if whole else "candidates"
    index = None if whole else doc_index(cfg, view)
    all_ids = {u["id"] for u in view.mapped}
    document = doc_text(view, mapped_only=True) if whole else None
    ues = user_edges(store, version_id, [o["id"] for o in obligations])
    locked = {r["obligation_id"] for r in store.q("SELECT obligation_id FROM coverage WHERE doc_version_id=? "
                                                  "AND source='user'", (version_id,))}
    todo = [o for o in obligations if o["id"] not in locked]
    stats = {"mode": mode, "batches": 0, "full": 0, "partial": 0, "contradicts": 0, "none": 0, "undecided": 0,
             "skipped_user": len(obligations) - len(todo)}
    for i in range(0, len(todo), size):
        batch = todo[i:i + size]
        items, allowed, src_pvs, rej = [], {}, {}, {}
        for o in batch:
            oid, p = o["id"], o["props"]
            src = source_of(store, o)
            src_pvs[oid] = {src["pv_id"]} if src else set()
            fixed = ues.get(oid, [])
            rej[oid] = {e["clause_id"] for e in fixed if e["role"] == "rejected"}
            d = {"obligation_id": oid, "statement": p.get("statement"),
                 "requirement": {k: p.get(k) for k in ("subject", "action", "condition", "exceptions",
                                                       "deadline_or_frequency")},
                 "source": src, "located_sections": located.get(oid, []),
                 "fixed_edges": [{"clause_id": e["clause_id"], "role": e["role"]} for e in fixed]}
            if whole:
                allowed[oid] = all_ids - rej[oid]
            else:
                cands = candidates_for(cfg, index, view, p, (src or {}).get("text"), located.get(oid, []),
                                       [e["clause_id"] for e in fixed])
                cands = [c for c in cands if c["id"] not in rej[oid]]
                d["candidates"] = cands
                allowed[oid] = {c["id"] for c in cands}
            items.append(d)
        inputs = {"mode": mode, "obligations": items, "document": hashlib.sha1((document or "").encode()).hexdigest()}
        body = "INPUT:\n" + json.dumps({"mode": mode, "obligations": items}, ensure_ascii=False)
        user_msg = (f"DOCUMENT:\n{document}\n\n" + body) if whole else body
        subject = f"{version_id}:judge:" + hashlib.sha1("|".join(o["id"] for o in batch).encode()).hexdigest()[:10]
        res = run_step(store, llm, STEP, subject, inputs, system_user(system, user_msg), JudgeBatch, version,
                       validate=make_validator(store, view, allowed, src_pvs, rej), force=force,
                       retry_invalid=retry_invalid, run=run)
        stats["batches"] += 1
        by_id = {r.obligation_id: r for r in res.output.results} if res.output else {}
        write_results(store, run, version_id, v_doc, batch, by_id, ues, res.output_id)
        for o in batch:
            row = store.q1("SELECT verdict FROM coverage WHERE doc_version_id=? AND obligation_id=?",
                           (version_id, o["id"]))
            stats[row["verdict"]] = stats.get(row["verdict"], 0) + 1
    return stats
