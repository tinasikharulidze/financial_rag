"""``extract-obligations`` v2 (docs/KG_V2_PLAN.md sections 5.3 and 5.4).

Order of work, per act, date by date (the dates are the versions of its extraction units):

    first version of a unit  -> EXTRACT (context -> LLM -> ObligationV2 list)
    later version of a unit  -> DIFF    (old text + old obligations + new text -> unchanged | modified | new | removed)

The graph (Obligation / ExtractionUnit nodes, IMPOSES / APPLIES_TO / SUPERSEDES edges) is a pure function of the
stored step outputs: the whole act is replayed from ``step_outputs`` on every run, so a rerun costs no request
and an interrupted run (daily limit) resumes where it stopped.

    unchanged  the old obligation stays open (no new node); a new IMPOSES edge points to the new provision version
    modified   the old one is closed (valid_to = effective date), a new one is created, SUPERSEDES new -> old
    new        a new obligation, valid from the effective date
    removed    the old one is closed

Obligation ids keep the v1 format ``obl:<act>/<unit>#<n>@<date>``. ``props.schema == "v2"`` marks v2 nodes; v1
nodes of the same act are purged when a v2 run starts.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from kg.citations import quote_in_text, verify_citations
from kg.extract.context import ContextBuilder, compact_obligation, context_ids
from kg.extract.obligations import Unit, build_units
from kg.extract.schema import DiffBatch, ExtractBatch, ObligationV2
from kg.ids import act_id, display_path, path_sort_key
from kg.llm import BudgetExhausted, system_user
from kg.steps import load_prompt, run_step

PROMPT_EXTRACT = Path(__file__).parent / "prompts" / "obligations_v2.md"
PROMPT_DIFF = Path(__file__).parent / "prompts" / "amend_diff.md"
SCHEMA_TAG = "v2"


PROMPT_FIELDS = Path(__file__).parent / "prompts" / "obligation_fields.md"


def _system(path: Path, cfg) -> tuple[str, str]:
    """Prompt text with the shared field definitions and the entity types filled in. The version string covers
    both files, so editing the shared fragment re-asks both steps."""
    text, version = load_prompt(path)
    fields, fver = load_prompt(PROMPT_FIELDS)
    if "{obligation_fields}" in text:
        text, version = text.replace("{obligation_fields}", fields.strip()), f"{version}+{fver}"
    return text.replace("{entity_types}", ", ".join(cfg.get("entity_types") or [cfg.get("entity_type")])), version


def _chunks(items: list, size: int, max_chars: int | None = None) -> list[list]:
    """Batches of ``size`` items. With ``max_chars`` a batch whose units' text is longer than that is split further
    (a long unit, e.g. a list of 20 eligibility criteria, then goes alone: three such units in one request produced
    an answer so long that the request ran past its timeout every time). Batches under the limit stay as they were,
    so answers stored for them are reused."""
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    if not max_chars:
        return batches

    def chars(it) -> int:
        return len(getattr(it[0] if isinstance(it, tuple) else it, "text_for_llm", "") or "")
    out = []
    for b in batches:
        if sum(chars(it) for it in b) <= max_chars:
            out.append(b)
            continue
        cur, n = [], 0
        for it in b:
            if cur and n + chars(it) > max_chars:
                out.append(cur)
                cur, n = [], 0
            cur.append(it)
            n += chars(it)
        out.append(cur)
    return out


# ===================================================================================================== validation
NEGATION = re.compile(r"არ უნდა|დაუშვებელ|აკრძალ|არ შეიძლება|არ დაიშვება|არ შეუძლია|არ არის ნებადართული")
PERMISSION = re.compile(r"შეუძლია|უფლებამოსილ|შესაძლებელია|დასაშვებია|შეიძლება|უფლება აქვს")


def deontic_of(statement: str) -> str:
    """Deontic value from Georgian cue words: a prohibition when every "უნდა" of the sentence is negated."""
    s = statement or ""
    neg = len(NEGATION.findall(s))
    if neg and s.count("უნდა") <= len(re.findall(r"არ უნდა", s)):
        return "must_not"
    if PERMISSION.search(s) and "უნდა" not in s and not neg:
        return "may"
    return "must"


def _deontic(o: ObligationV2, notes: list[str]):
    cue = deontic_of(o.statement)
    if o.deontic is None:
        o.deontic = cue
        notes.append(f"note: {o.statement[:50]}: deontic missing -> '{cue}' (from the wording)")
    elif o.deontic == "must" and cue == "must_not":
        o.deontic = "must_not"
        notes.append(f"note: {o.statement[:50]}: negated wording -> deontic 'must_not'")


def _normalize(o: ObligationV2, ctx: dict, cfg, store, notes: list[str]) -> list[str]:
    """Fix what code can fix, verify citations; returns HARD problems (empty list = fine)."""
    hard: list[str] = []
    _deontic(o, notes)
    members = {p["path"] for p in ctx["unit"]["provisions"]}
    if o.source_path not in members:
        o.source_path = ctx["unit"]["path"] if ctx["unit"]["path"] in members else next(iter(members), None)
    vocab = set(cfg.get("entity_types") or [])
    o.applies_to = [a for a in dict.fromkeys(o.applies_to) if not vocab or a in vocab] \
        or list(ctx["act"].get("default_applies_to") or [])
    if not o.domain:
        o.domain = (ctx["act"].get("default_domains") or [None])[0]
    o.depends_on = [d for d in dict.fromkeys(o.depends_on) if isinstance(d, str)]
    allowed = context_ids(ctx)
    good, errs = verify_citations(store, o.citations, allowed_ids=allowed, soft_quotes=True)
    notes += [f"note: {o.statement[:50]}: {e.removeprefix('note: ')}" for e in errs]
    if not good:
        hard.append(f"obligation '{o.statement[:60]}' has no citation with a valid provision id "
                    f"(use ids from unit.provisions)")
    o.citations = good
    rd = o.required_doc
    if rd is not None:
        if rd.basis == "explicit" and not (rd.quote and quote_in_text(rd.quote, ctx["unit"]["text"])):
            notes.append(f"note: {o.statement[:50]}: required_doc 'explicit' without a verbatim quote -> 'inferred'")
            rd.basis, rd.quote = "inferred", None
        if not rd.doc_types:
            o.required_doc = None
    return hard


def make_extract_validator(cfg, store, ctx_by_id: dict[str, dict]):
    def validate(parsed: ExtractBatch) -> list[str]:
        problems: list[str] = []
        notes: list[str] = []
        parsed.results = [r for r in parsed.results if r.id in ctx_by_id]
        for r in parsed.results:
            keep = []
            for o in r.obligations:
                if not (o.statement or "").strip():
                    continue
                problems += _normalize(o, ctx_by_id[r.id], cfg, store, notes)
                keep.append(o)
            r.obligations = keep
        missing = sorted(set(ctx_by_id) - {r.id for r in parsed.results})
        if missing:
            problems.append("missing results for unit ids: " + ", ".join(missing))
        return problems + notes
    return validate


def make_diff_validator(cfg, store, ctx_by_id: dict[str, dict], old_ids_by_unit: dict[str, set[str]]):
    def validate(parsed: DiffBatch) -> list[str]:
        problems: list[str] = []
        notes: list[str] = []
        parsed.results = [r for r in parsed.results if r.id in ctx_by_id]
        for r in parsed.results:
            old_ids = old_ids_by_unit[r.id]
            ctx = ctx_by_id[r.id]
            seen, keep = set(), []
            for op in r.operations:
                if op.op in ("unchanged", "modified", "removed"):
                    if op.old_id not in old_ids:
                        problems.append(f"{r.id}: old_id '{op.old_id}' is not one of the old obligations")
                        continue
                    if op.old_id in seen:
                        notes.append(f"note: {r.id}: duplicate operation for {op.old_id} ignored")
                        continue
                    seen.add(op.old_id)
                if op.op in ("modified", "new"):
                    if op.new is None or not (op.new.statement or "").strip():
                        problems.append(f"{r.id}: operation '{op.op}' needs a complete `new` obligation")
                        continue
                    problems += _normalize(op.new, ctx, cfg, store, notes)
                else:
                    op.new = None
                keep.append(op)
            r.operations = keep
            for oid in sorted(old_ids - seen):
                problems.append(f"{r.id}: old obligation {oid} has no operation")
        missing = sorted(set(ctx_by_id) - {r.id for r in parsed.results})
        if missing:
            problems.append("missing results for unit ids: " + ", ".join(missing))
        return problems + notes
    return validate


# ===================================================================================================== graph writing
def amendment_for(store, act_doc: str, pv_ids: list[str], date: str) -> dict | None:
    """The amending act behind a unit version: via AMENDED_BY edges of its provision versions, else by date."""
    found: dict[str, dict] = {}
    for pv in pv_ids:
        for it in store.neighbors(pv, "AMENDED_BY", "out"):
            found[it["node"]["id"]] = it["node"]
    if not found:
        for n in store.nodes("AmendingAct"):
            if str(n["props"].get("base_act")) == str(act_doc) and n["props"].get("effective_from") == date:
                found[n["id"]] = n
    if not found:
        return None
    n = sorted(found.values(), key=lambda x: x["props"].get("seq", 0))[0]
    p = n["props"]
    return {"id": n["id"], "number": p.get("number"), "effective_from": p.get("effective_from"),
            "date": p.get("date")}


class _Graph:
    """In-memory result of one replay; written to the store in one go."""

    def __init__(self):
        self.obls: dict[str, dict] = {}                 # id -> {props, valid_from, valid_to}
        self.open: dict[str, list[str]] = defaultdict(list)   # unit path -> open obligation ids
        self.imposes: dict[str, dict] = {}              # obligation id -> current IMPOSES edge dict
        self.edges: list[dict] = []
        self.units: list[dict] = []

    def next_index(self, unit_path: str, date: str) -> int:
        n = sum(1 for oid in self.obls if oid.endswith(f"@{date}") and f"/{unit_path}#" in oid)
        return n + 1


def _obl_props(act_doc: str, unit: Unit, o: ObligationV2, output_id: int | None, prompt_version: str,
               amendment: dict | None, lineage: str | None) -> dict:
    p = o.model_dump()
    p["citations"] = [c for c in p["citations"]]
    p.update({"act": act_doc, "unit": unit.unit_path, "unit_date": unit.date, "schema": SCHEMA_TAG,
              "entity_types": list(o.applies_to), "output_id": output_id, "prompt_version": prompt_version,
              "amendment_id": amendment["id"] if amendment else None,
              "amendment_number": amendment["number"] if amendment else None,
              "effective_from": unit.date, "lineage": lineage})
    return p


def _add_obligation(g: _Graph, builder: ContextBuilder, act_doc: str, unit: Unit, o: ObligationV2, output_id,
                    prompt_version, amendment, lineage_of: str | None = None, supersedes: str | None = None,
                    what_changed: str | None = None) -> str:
    k = g.next_index(unit.unit_path, unit.date)
    oid = f"obl:{act_doc}/{unit.unit_path}#{k}@{unit.date}"
    lineage = g.obls[lineage_of]["props"]["lineage"] if lineage_of else oid
    g.obls[oid] = {"props": _obl_props(act_doc, unit, o, output_id, prompt_version, amendment, lineage),
                   "valid_from": unit.date, "valid_to": None}
    g.open[unit.unit_path].append(oid)
    pv = builder.pv_at(o.source_path, unit.date) if o.source_path else None
    if pv:
        e = {"src": pv["id"], "dst": oid, "type": "IMPOSES", "props": {}, "valid_from": unit.date, "valid_to": None}
        g.edges.append(e)
        g.imposes[oid] = e
    for et in o.applies_to:
        g.edges.append({"src": oid, "dst": f"ent:{et}", "type": "APPLIES_TO", "props": {}, "valid_from": unit.date,
                        "valid_to": None})
    if supersedes:
        g.edges.append({"src": oid, "dst": supersedes, "type": "SUPERSEDES",
                        "props": {"change": "modified", "amendment": amendment["id"] if amendment else None,
                                  "what_changed": what_changed}, "valid_from": unit.date, "valid_to": None})
    return oid


def _close(g: _Graph, oid: str, date: str, unit_path: str):
    g.obls[oid]["valid_to"] = date
    g.open[unit_path] = [x for x in g.open[unit_path] if x != oid]
    e = g.imposes.get(oid)
    if e and e["valid_to"] is None:
        e["valid_to"] = date
    for x in g.edges:                                   # APPLIES_TO edges end with the obligation
        if x["src"] == oid and x["type"] == "APPLIES_TO" and x["valid_to"] is None:
            x["valid_to"] = date


def _carry(g: _Graph, builder: ContextBuilder, oid: str, unit: Unit):
    """unchanged: keep the obligation open and let a new IMPOSES edge point to the new provision version."""
    src = g.obls[oid]["props"].get("source_path")
    pv = builder.pv_at(src, unit.date) if src else None
    cur = g.imposes.get(oid)
    if pv and (cur is None or cur["src"] != pv["id"]):
        if cur is not None and cur["valid_to"] is None:
            cur["valid_to"] = unit.date
        e = {"src": pv["id"], "dst": oid, "type": "IMPOSES", "props": {}, "valid_from": unit.date, "valid_to": None}
        g.edges.append(e)
        g.imposes[oid] = e


def _flush(cfg, store, act_doc: str, g: _Graph):
    nodes = [(oid, "Obligation", d["props"], d["valid_from"], d["valid_to"]) for oid, d in g.obls.items()]
    nodes += [(u["id"], "ExtractionUnit", u["props"], u["valid_from"], u["valid_to"]) for u in g.units]
    store.upsert_nodes(nodes)
    store.upsert_edges([(e["src"], e["dst"], e["type"], e["props"], e["valid_from"], e["valid_to"]) for e in g.edges])
    # obligations of this act that the replay no longer produces
    stale = [n["id"] for n in store.nodes("Obligation") if str(n["props"].get("act")) == str(act_doc)
             and n["id"] not in g.obls]
    stale += [n["id"] for n in store.nodes("ExtractionUnit") if str(n["props"].get("act")) == str(act_doc)
              and n["id"] not in {u["id"] for u in g.units}]
    if stale:
        store.delete_nodes(stale)
    return len(stale)


def purge_v1(store, act_doc: str) -> int:
    ids = [n["id"] for t in ("Obligation", "ExtractionUnit") for n in store.nodes(t)
           if str(n["props"].get("act")) == str(act_doc) and n["props"].get("schema") != SCHEMA_TAG]
    if ids:
        store.delete_nodes(ids)
    return len(ids)


# ===================================================================================================== main job
def plan(cfg, store, act_doc: str) -> dict:
    """Request estimate without any LLM call (``--dry-run``)."""
    units = build_units(store, act_doc)
    size, dsize = int(cfg.get_in("extraction.obligation_batch", 5)), int(cfg.get_in("extraction.diff_batch", 4))
    seen, fresh, diff = set(), defaultdict(int), defaultdict(int)
    for u in sorted(units, key=lambda x: (x.date, path_sort_key(x.unit_path))):
        if u.unit_path in seen:
            diff[u.date] += 1
        else:
            fresh[u.date] += 1
        seen.add(u.unit_path)
    req = sum(-(-n // size) for n in fresh.values()) + sum(-(-n // dsize) for n in diff.values())
    return {"units": len(units), "fresh_units": sum(fresh.values()), "diff_units": sum(diff.values()),
            "estimated_requests": req, "by_date": {d: {"fresh": fresh.get(d, 0), "diff": diff.get(d, 0)}
                                                   for d in sorted(set(fresh) | set(diff))}}


def extract_obligations_v2(cfg, store, run, llm, dry_run: bool = False, act_docs: list[str] | None = None,
                           force: bool = False, retry_invalid: bool = False) -> dict:
    summary = {"acts": {}, "requests_estimated": 0}
    for ent in cfg.get("entity_types") or [cfg.get("entity_type")]:
        store.upsert_node(f"ent:{ent}", "EntityType", {"name": ent})
    for act_doc in (act_docs or cfg["obligation_acts"]):
        act_doc = str(act_doc)
        pl = plan(cfg, store, act_doc)
        summary["requests_estimated"] += pl["estimated_requests"]
        run.log("obligation_plan_v2", act=act_doc, **pl)
        if dry_run:
            summary["acts"][act_doc] = pl
            continue
        summary["acts"][act_doc] = _run_act(cfg, store, run, llm, act_doc, force, retry_invalid)
    return summary


def _run_act(cfg, store, run, llm, act_doc: str, force: bool, retry_invalid: bool) -> dict:
    size, dsize = int(cfg.get_in("extraction.obligation_batch", 5)), int(cfg.get_in("extraction.diff_batch", 4))
    sys_x, ver_x = _system(PROMPT_EXTRACT, cfg)
    sys_d, ver_d = _system(PROMPT_DIFF, cfg)
    purged = purge_v1(store, act_doc)
    if purged:
        run.log("purged_v1_obligations", act=act_doc, nodes=purged)
    units = build_units(store, act_doc)
    builder = ContextBuilder(cfg, store, act_doc)
    g = _Graph()
    stats = defaultdict(int, {k: 0 for k in ("failed_units", "new", "modified", "unchanged", "removed",
                                              "extract_batches", "diff_batches", "unchanged_by_default",
                                              "non_obligation_units")})
    stats["purged_v1"] = purged
    by_path: dict[str, list[Unit]] = defaultdict(list)
    for u in sorted(units, key=lambda x: x.date):
        by_path[u.unit_path].append(u)

    def previous(u: Unit) -> Unit | None:
        vs = by_path[u.unit_path]
        i = vs.index(u)
        return vs[i - 1] if i > 0 else None

    try:
        for date in sorted({u.date for u in units}):
            todo = sorted((u for u in units if u.date == date), key=lambda u: path_sort_key(u.unit_path))
            fresh, changed = [], []
            for u in todo:
                prev = previous(u)
                old = [oid for oid in g.open.get(u.unit_path, [])]
                (changed if prev is not None and old else fresh).append((u, prev, old))
            amend_cache: dict[str, dict | None] = {}
            for batch in _chunks(fresh, size, cfg.get_in("extraction.batch_max_chars", None)):
                _do_extract(cfg, store, run, llm, act_doc, date, batch, builder, g, sys_x, ver_x, stats,
                            amend_cache, force, retry_invalid)
            for batch in _chunks(changed, dsize):
                _do_diff(cfg, store, run, llm, act_doc, date, batch, builder, g, sys_d, ver_d, stats, amend_cache,
                         force, retry_invalid)
    finally:
        stale = _flush(cfg, store, act_doc, g)         # also on BudgetExhausted: what is finished is visible
        stats["stale_removed"] = stale
        run.log("obligations_written", act=act_doc, obligations=len(g.obls), units=len(g.units), **dict(stats))
    stats["obligations"] = len(g.obls)
    stats["open_obligations"] = sum(1 for d in g.obls.values() if d["valid_to"] is None)
    return dict(stats)


def _unit_amendment(store, builder, act_doc, u: Unit, cache: dict):
    if u.date in cache:
        return cache[u.date]
    pvs = [builder.pv_at(p, u.date) for p, _ in u.members]
    base = min(n["valid_from"] for ns in builder.pvs.values() for n in ns)
    cache[u.date] = None if u.date == base else amendment_for(store, act_doc, [p["id"] for p in pvs if p], u.date)
    return cache[u.date]


def _unit_node(act_doc: str, u: Unit, status: str, n: int, mode: str, output_id, extra: dict | None = None) -> dict:
    return {"id": f"unit:{act_doc}/{u.unit_path}@{u.date}", "valid_from": u.date, "valid_to": u.end,
            "props": {"act": act_doc, "unit": u.unit_path, "status": status, "n_obligations": n,
                      "text_hash": u.text_hash, "mode": mode, "output_id": output_id, "schema": SCHEMA_TAG,
                      **(extra or {})}}


def _do_extract(cfg, store, run, llm, act_doc, date, batch, builder, g, system, version, stats, amend_cache,
                force, retry_invalid):
    ctxs = [builder.context(u) for u, _p, _o in batch]
    ctx_by_id = {c["id"]: c for c in ctxs}
    inputs = {"entity_types": cfg.get("entity_types"), "contexts": ctxs}
    messages = system_user(system, "INPUT:\n" + json.dumps(ctxs, ensure_ascii=False))
    subject = f"extract:{act_doc}@{date}:" + hashlib.sha1("|".join(c["id"] for c in ctxs).encode()).hexdigest()[:10]
    res = run_step(store, llm, "extract_obligations", subject, inputs, messages, ExtractBatch, version,
                   validate=make_extract_validator(cfg, store, ctx_by_id), force=force, retry_invalid=retry_invalid,
                   run=run)
    stats["extract_batches"] += 1
    by_id = {r.id: r for r in res.output.results} if res.output else {}
    for u, _prev, _old in batch:
        r = by_id.get(u.uid)
        if r is None:
            stats["failed_units"] += 1
            g.units.append(_unit_node(act_doc, u, "failed", 0, "extract", res.output_id))
            continue
        amendment = _unit_amendment(store, builder, act_doc, u, amend_cache)
        for o in r.obligations:
            _add_obligation(g, builder, act_doc, u, o, res.output_id, version, amendment)
            stats["new"] += 1
        g.units.append(_unit_node(act_doc, u, "extracted" if r.obligations else "non_obligation",
                                  len(r.obligations), "extract", res.output_id))
        if not r.obligations:
            stats["non_obligation_units"] += 1


def _too_big(llm, purpose: str, messages: list[dict], share: float = 0.85) -> bool:
    """Would the request (plus room for a retry) exceed the model's input tokens per minute?"""
    try:
        model = llm.models_for(purpose)[0]
        tpm = llm.options_for(model, purpose).get("tokens_per_minute")
    except Exception:  # noqa: BLE001 - a test double without these: never compact
        return False
    return bool(tpm) and llm.estimate_tokens(messages) > tpm * share


def _only_changed_members(ctx: dict, u: Unit, prev: Unit | None):
    """Keep only the changed sub-points of a unit (and of its previous wording) in a diff context."""
    if prev is None or "previous" not in ctx:
        return
    old, new = dict(prev.members), dict(u.members)
    changed = [m for m in dict.fromkeys(list(old) + list(new)) if old.get(m) != new.get(m)]

    def lines(members: dict) -> str:
        return "\n".join(t if m == u.unit_path else f"[{m}] {t}" for m, t in members.items()
                         if m in changed or m == u.unit_path)
    ctx["unit"]["text"] = lines(new)
    ctx["previous"]["text"] = lines(old)
    ctx["note"] = ("Only the sub-points that changed are shown (in both wordings); every other sub-point of this "
                   "unit is unchanged, so obligations based on them are 'unchanged'.")


def _do_diff(cfg, store, run, llm, act_doc, date, batch, builder, g, system, version, stats, amend_cache, force,
             retry_invalid):
    ctxs, old_ids = [], {}
    for u, prev, old in batch:
        amendment = _unit_amendment(store, builder, act_doc, u, amend_cache)
        ctx = builder.context(u, prev, [{"id": oid, "props": g.obls[oid]["props"]} for oid in old])
        ctx["amendment"] = {"number": (amendment or {}).get("number"), "effective_date": date}
        ctxs.append(ctx)
        old_ids[u.uid] = set(old)
    messages = system_user(system, "INPUT:\n" + json.dumps(ctxs, ensure_ascii=False))
    if _too_big(llm, "amend_diff", messages):
        # e.g. a definitions paragraph with 30 sub-points: old + new wording do not fit into one request. Only the
        # sub-points that changed are sent (the request would otherwise be refused and the old obligations kept)
        for (u, prev, _old), ctx in zip(batch, ctxs):
            _only_changed_members(ctx, u, prev)
        messages = system_user(system, "INPUT:\n" + json.dumps(ctxs, ensure_ascii=False))
        run.log("diff_compacted", act=act_doc, date=date, units=[c["id"] for c in ctxs])
    ctx_by_id = {c["id"]: c for c in ctxs}
    inputs = {"entity_types": cfg.get("entity_types"), "contexts": ctxs}
    subject = f"diff:{act_doc}@{date}:" + hashlib.sha1("|".join(c["id"] for c in ctxs).encode()).hexdigest()[:10]
    res = run_step(store, llm, "amend_diff", subject, inputs, messages, DiffBatch, version,
                   validate=make_diff_validator(cfg, store, ctx_by_id, old_ids), force=force,
                   retry_invalid=retry_invalid, run=run)
    stats["diff_batches"] += 1
    by_id = {r.id: r for r in res.output.results} if res.output else {}
    for u, prev, old in batch:
        r = by_id.get(u.uid)
        amendment = _unit_amendment(store, builder, act_doc, u, amend_cache)
        if r is None:                                       # the step failed: keep the old ones, flag the unit
            stats["failed_units"] += 1
            for oid in old:
                _carry(g, builder, oid, u)
            g.units.append(_unit_node(act_doc, u, "failed", len(old), "diff", res.output_id))
            continue
        handled = set()
        counts = defaultdict(int)
        for op in r.operations:
            if op.op == "unchanged":
                _carry(g, builder, op.old_id, u)
            elif op.op == "removed":
                _close(g, op.old_id, date, u.unit_path)
            elif op.op == "modified":
                _close(g, op.old_id, date, u.unit_path)
                _add_obligation(g, builder, act_doc, u, op.new, res.output_id, version, amendment,
                                lineage_of=op.old_id, supersedes=op.old_id, what_changed=op.what_changed)
            elif op.op == "new":
                _add_obligation(g, builder, act_doc, u, op.new, res.output_id, version, amendment)
            if op.old_id:
                handled.add(op.old_id)
            counts[op.op] += 1
            stats[op.op] += 1
        for oid in old:                                     # validator already complained; never lose an obligation
            if oid not in handled:
                _carry(g, builder, oid, u)
                counts["unchanged"] += 1
                stats["unchanged_by_default"] += 1
        n_open = sum(1 for oid in g.open.get(u.unit_path, []))
        g.units.append(_unit_node(act_doc, u, "diffed", n_open, "diff", res.output_id, {"ops": dict(counts)}))
