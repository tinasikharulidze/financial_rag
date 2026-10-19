"""UC3 / §4.9 ``replay``: analyse one amendment -> changed obligations -> impact on documents -> report.

The consolidated graph already contains every amendment (``consolidate``). Replaying an amendment therefore
*re-enacts* it on top of the state before it: the provisions it created, the obligations that began or ended on its
effective date, the clauses covering the replaced obligations, and the alerts that result. Cached LLM prompts are
re-used, so a replay after ``map-coverage`` costs few or no new requests.
"""
from __future__ import annotations

from kg.extract.obligations import extract_obligations
from kg.ids import amend_id, display_path
from kg.mapping.alerts import coverage_alerts
from kg.mapping.coverage import map_coverage, predecessor
from kg.services import reports


class ReplayError(RuntimeError):
    pass


def amendment_changes(store, amendment: str) -> tuple[dict, list[dict]]:
    am = store.get_node(amend_id(amendment))
    if not am:
        raise ReplayError(f"amendment {amendment} not found; run parse-amendments and consolidate first")
    changes = []
    for e in store.edges("AMENDED_BY"):
        if e["dst"] != amend_id(amendment):
            continue
        pv = store.get_node(e["src"])
        if not pv:
            continue
        p = pv["props"]
        prev = None
        for u in store.edges("UPDATES"):
            if u["src"] == pv["id"]:
                prev = store.get_node(u["dst"])
        changes.append({"path": p["path"], "act": p["act"], "kind": p.get("change_type") or p.get("status"),
                        "status": p.get("status"), "valid_from": pv["valid_from"], "version": pv["id"],
                        "text_after": p.get("text"), "text_before": (prev or {}).get("props", {}).get("text")})
    return am, changes


def obligation_delta(store, act: str, date: str) -> dict:
    all_o = [o for o in store.nodes("Obligation") if str(o["props"]["act"]) == str(act)]
    starting = [o for o in all_o if o["valid_from"] == date]
    ending = [o for o in all_o if o["valid_to"] == date]
    new, changed, links = [], [], {}
    matched_pred = set()
    for s in starting:
        pred = predecessor(s, ending)
        if pred is None:
            new.append(s)
        else:
            changed.append(s)
            links[s["id"]] = pred["id"]
            matched_pred.add(pred["id"])
    removed = [p for p in ending if p["id"] not in matched_pred]
    return {"new": new, "changed": changed, "removed": removed, "links": links, "ending": ending}


def _brief(o):
    p = o["props"]
    return {"id": o["id"], "statement": p["statement"], "severity": p["severity"], "category": p["category"],
            "source_path": p["source_path"], "valid_from": o["valid_from"]}


def replay(cfg, store, run, llm, act: str, amendment: str, dry_run: bool = False) -> dict:
    act, amendment = str(act), str(amendment)
    if not dry_run and not store.edges("COVERED_BY"):
        raise ReplayError("no coverage mapping yet: run `map-coverage` to completion before `replay`")
    am, changes = amendment_changes(store, amendment)
    date = am["props"]["effective_from"]
    run.log("replay_start", act=act, amendment=amendment, effective_date=date, changed_provisions=len(changes))
    obl_act = act in [str(a) for a in cfg["obligation_acts"]]
    delta = obligation_delta(store, act, date) if obl_act else {"new": [], "changed": [], "removed": [], "links": {},
                                                                 "ending": []}
    if obl_act and changes and not (delta["new"] or delta["changed"] or delta["removed"]):
        run.log("replay_extract", reason="no obligations start on the effective date yet")
        if not dry_run:
            extract_obligations(cfg, store, run, llm, act_docs=[act])
            delta = obligation_delta(store, act, date)
    focus = {o["id"] for o in delta["new"] + delta["changed"]} | {o["id"] for o in delta["ending"]}
    map_summary, alert_info = {}, {"new_alerts": []}
    if focus:
        map_summary = map_coverage(cfg, store, run, llm, dry_run=dry_run, only_obligations=focus, now=date,
                                   links=delta["links"])
    if dry_run:
        return {"dry_run": True, "changes": len(changes), "focus_obligations": len(focus), **map_summary}
    alert_info = coverage_alerts(cfg, store, run, now=date)
    rows = []
    for f in store.findings(run_id=run.id):
        ob = store.get_node(f["obligation_id"])
        cl = store.get_node(f["clause_id"]) if f.get("clause_id") else None
        rows.append({**f, "obligation": ob["props"]["statement"] if ob else None,
                     "clause_text": (cl or {}).get("props", {}).get("text")})
    rep = {"run_id": run.id, "act": act, "amendment": amendment, "amendment_number": am["props"].get("number"),
           "effective_date": date, "transitional_note": am["props"].get("transitional_note"),
           "changes": [{k: c[k] for k in ("path", "kind", "status", "valid_from", "text_before", "text_after")}
                       for c in changes],
           "new_obligations": [_brief(o) for o in delta["new"]],
           "changed_obligations": [_brief(o) for o in delta["changed"]],
           "removed_obligations": [_brief(o) for o in delta["removed"]],
           "affected": rows, "new_alerts": alert_info["new_alerts"], "map_summary": map_summary}
    j, m = reports.save(cfg, rep)
    run.log("replay_done", report=str(m), findings=len(rows), new_alerts=len(alert_info["new_alerts"]))
    rep["report_files"] = [str(j), str(m)]
    return rep
