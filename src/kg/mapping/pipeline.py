"""Mapping orchestration for one document version: filter -> triage -> locate -> judge (plan section 7).

Everything is resumable: each LLM step is stored in ``step_outputs`` (see ``kg.steps``), and a step only asks the
model about obligations that have no result row yet (``retriage`` / ``rejudge`` / ``force`` redo them). Rows decided
by the user (``source = user``) are never touched.
"""
from __future__ import annotations

from kg.company.profile import get_profile
from kg.mapping import filter as flt
from kg.mapping.docview import fits_whole_doc, load_doc
from kg.mapping.judging import judge_document
from kg.mapping.locate import locate_document, located_sections
from kg.mapping.triage import triage_document
from kg.services.accounts import get_company


class MappingError(RuntimeError):
    pass


def _context(cfg, store, version_id: str, require_confirmed: bool):
    view = load_doc(store, version_id)
    row = store.q1("SELECT d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                   "WHERE dv.id=?", (version_id,))
    if view is None or row is None:
        raise MappingError(f"unknown document version {version_id}")
    profile = get_profile(store, version_id)
    if profile is None:
        raise MappingError("the document has no profile yet (run the profile step first)")
    if require_confirmed and not profile["confirmed"]:
        raise MappingError("the profile is not confirmed yet")
    company = get_company(store, row["company_id"])
    return view, profile, company


def prune(store, version_id: str, keep: set[str]):
    """Remove results for obligations that are no longer in scope (not user decisions)."""
    for table in ("triage", "locations", "coverage"):
        extra = " AND source<>'user'" if table in ("triage", "coverage") else ""
        for r in store.q(f"SELECT obligation_id FROM {table} WHERE doc_version_id=?{extra}", (version_id,)):
            if r["obligation_id"] not in keep:
                store.x(f"DELETE FROM {table} WHERE doc_version_id=? AND obligation_id=?",
                        (version_id, r["obligation_id"]))
    gone = [e["id"] for e in store.edges("COVERED_BY") if e["props"].get("doc_version") == version_id
            and e["props"].get("source") == "llm" and e["src"] not in keep]
    store.delete_edge_ids(gone)


def plan_map(cfg, store, run, version_id: str, now: str | None = None, require_confirmed: bool = True) -> dict:
    """Upper bound of the requests a ``map_document`` call would need (no LLM)."""
    view, profile, company = _context(cfg, store, version_id, require_confirmed)
    f = flt.filter_obligations(cfg, store, run, view.props, profile, company["entity_type"], now)
    # rows written because the model gave no answer (a failed / skipped batch) are asked again
    have_t = {r["obligation_id"] for r in store.q("SELECT obligation_id FROM triage WHERE doc_version_id=? AND NOT "
                                                  "(source='rule' AND llm_output_id IS NULL)", (version_id,))}
    todo = [o for o in f["kept"] if o["id"] not in have_t]
    tb, jb, lb = (int(cfg.get_in(f"mapping.{k}", d)) for k, d in (("triage_batch", 60), ("judge_batch", 8),
                                                                  ("locate_batch", 40)))
    n_cont = len(todo)                                  # worst case: every new obligation is relevant
    req = {"triage": -(-len(todo) // tb), "locate": -(-n_cont // lb), "judge": -(-n_cont // jb)}
    return {"kept": len(f["kept"]), "new": len(todo), "dropped": {k: len(v) for k, v in f["dropped"].items()},
            "mode": "whole" if fits_whole_doc(cfg, view) else "candidates", "requests_upper_bound": req,
            "requests_total": sum(req.values()), "warnings": f["warnings"], "v_doc": f["v_doc"], "v_now": f["v_now"]}


def map_document(cfg, store, run, llm, version_id: str, *, now: str | None = None, retriage: bool = False,
                 rejudge: bool = False, force: bool = False, retry_invalid: bool = False,
                 require_confirmed: bool = True, use_locate: bool | None = None) -> dict:
    view, profile, company = _context(cfg, store, version_id, require_confirmed)
    use_locate = cfg.get_in("mapping.locate", True) if use_locate is None else use_locate
    f = flt.filter_obligations(cfg, store, run, view.props, profile, company["entity_type"], now)
    kept = f["kept"]
    prune(store, version_id, {o["id"] for o in kept})
    out = {"filter": {"kept": len(kept), "dropped": {k: len(v) for k, v in f["dropped"].items()},
                      "v_doc": f["v_doc"], "v_now": f["v_now"], "warnings": f["warnings"]}}
    # rows written because the model gave no answer (a failed / skipped batch) are asked again
    have_t = {r["obligation_id"] for r in store.q("SELECT obligation_id FROM triage WHERE doc_version_id=? AND NOT "
                                                  "(source='rule' AND llm_output_id IS NULL)", (version_id,))}
    to_triage = kept if (retriage or force) else [o for o in kept if o["id"] not in have_t]
    out["triage"] = triage_document(cfg, store, run, llm, version_id, view, profile, to_triage, force=force,
                                    retry_invalid=retry_invalid)
    rel = {r["obligation_id"]: r["relevance"] for r in store.q(
        "SELECT obligation_id, relevance FROM triage WHERE doc_version_id=?", (version_id,))}
    cont = [o for o in kept if rel.get(o["id"]) in ("relevant", "possibly")]
    for oid in [o["id"] for o in kept if rel.get(o["id"]) == "irrelevant"]:        # no result rows for irrelevant ones
        store.x("DELETE FROM locations WHERE doc_version_id=? AND obligation_id=?", (version_id, oid))
        store.x("DELETE FROM coverage WHERE doc_version_id=? AND obligation_id=? AND source<>'user'",
                (version_id, oid))
    have_c = {r["obligation_id"] for r in store.q("SELECT obligation_id FROM coverage WHERE doc_version_id=? AND NOT "
                                                  "(invalid=1 AND source<>'user')", (version_id,))}
    to_judge = cont if (rejudge or force) else [o for o in cont if o["id"] not in have_c]
    have_l = {r["obligation_id"] for r in store.q("SELECT obligation_id FROM locations WHERE doc_version_id=?",
                                                  (version_id,))}
    if use_locate:
        to_locate = to_judge if (rejudge or force) else [o for o in to_judge if o["id"] not in have_l]
        out["locate"] = locate_document(cfg, store, run, llm, version_id, view, profile, to_locate, force=force,
                                        retry_invalid=retry_invalid)
    else:
        out["locate"] = {"skipped": True}
    out["judge"] = judge_document(cfg, store, run, llm, version_id, view, profile, to_judge,
                                  located_sections(store, version_id), f["v_doc"], force=force,
                                  retry_invalid=retry_invalid)
    out["continuing"] = len(cont)
    run.log("map_document", doc=version_id, **{k: v for k, v in out.items()})
    return out


def recheck_obligation(cfg, store, run, llm, version_id: str, obligation_id: str, *, require_confirmed=True) -> dict:
    """Re-run the judge for ONE obligation (after the user changed its edges); the model sees the user's edges."""
    view, profile, company = _context(cfg, store, version_id, require_confirmed)
    ob = store.get_node(obligation_id)
    if ob is None:
        raise MappingError(f"unknown obligation {obligation_id}")
    f = flt.v_dates(cfg, view.props)
    return judge_document(cfg, store, run, llm, version_id, view, profile, [ob], located_sections(store, version_id),
                          f[0], force=True)
