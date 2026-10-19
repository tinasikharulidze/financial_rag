"""UC4: active obligations that no internal document covers (no full/partial COVERED_BY edge)."""
from __future__ import annotations

from kg.config import today_iso
from kg.ids import display_path
from kg.mapping.coverage import active_obligations, category_map

KIND = "uncovered_obligation"


def coverage_alerts(cfg, store, run, now: str | None = None) -> dict:
    now = now or cfg.get_in("mapping.now") or today_iso()
    cmap = category_map(cfg)
    cats = set().union(*cmap.values()) if cmap else set()
    uploads = {n["id"] for n in store.nodes("InternalDoc") if n["props"].get("source") == "upload"}
    covered = {e["src"] for e in store.edges("COVERED_BY")
               if e["props"].get("coverage") in ("full", "partial") and e["props"].get("doc") not in uploads}
    new, existing, new_items = 0, 0, []
    active = active_obligations(store, now, cats)
    for o in active:
        if o["id"] in covered:
            continue
        p = o["props"]
        msg = f"{display_path(p['source_path'])}: {p['statement']}"
        if store.add_alert(run.id, KIND, o["id"], msg):
            new += 1
            new_items.append({'obligation_id': o['id'], 'message': msg})
        else:
            existing += 1
    # alerts of obligations that became covered or are no longer active are acknowledged-by-removal
    still = {o["id"] for o in active if o["id"] not in covered}
    stale = [a for a in store.alerts() if a["kind"] == KIND and a["obligation_id"] not in still]
    for a in stale:
        store.acknowledge_alert(a["id"])
    run.log("coverage_alerts", active=len(active), new=new, existing=existing, closed=len(stale))
    return {"active": len(active), "new": new, "existing": existing, "closed": len(stale), "new_alerts": new_items}
