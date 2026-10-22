"""Mapping step 1 (no LLM): which obligations are worth looking at for one document version (plan section 7.1)."""
from __future__ import annotations

from kg.config import today_iso


def v_dates(cfg, doc_props: dict, now: str | None = None) -> tuple[str | None, str]:
    """(V_doc, V_now): the document's regulatory date (effective or document date; None when it has none) and today."""
    now = now or cfg.get_in("mapping.now") or today_iso()
    return (doc_props.get("effective_date") or doc_props.get("doc_date")), now


def cause_of(obl: dict, v_doc: str | None) -> str:
    """``amendment`` when the obligation became valid after the document's regulatory date, else ``pre_existing``
    (also for a document without a date: plan section 7.6)."""
    vf = obl.get("valid_from") or obl["props"].get("effective_from")
    return "amendment" if (v_doc and vf and vf > v_doc) else "pre_existing"


def filter_obligations(cfg, store, run, doc_props: dict, profile: dict, entity_type: str,
                       now: str | None = None) -> dict:
    """-> {'kept': [obligation nodes], 'dropped': {reason: [ids]}, 'v_doc', 'v_now', 'warnings'}"""
    v_doc, v_now = v_dates(cfg, doc_props, now)
    dropped: dict[str, list[str]] = {}
    kept, warnings = [], []
    domains = set(profile.get("domains") or [])
    if not domains:
        warnings.append("no_domains")
    if v_doc is None:
        warnings.append("no_document_date")

    def drop(reason, o):
        dropped.setdefault(reason, []).append(o["id"])

    for o in sorted(store.nodes("Obligation", as_of=v_now), key=lambda n: n["id"]):
        p = o["props"]
        if p.get("schema") != "v2":
            drop("not_v2", o)
        elif p.get("deontic") == "may":
            drop("permission_only", o)
        elif p.get("domain") and p["domain"] not in domains:
            drop("domain", o)
        elif p.get("applies_to") and entity_type not in p["applies_to"]:
            drop("entity_type", o)
        else:
            kept.append(o)
    stats = {k: len(v) for k, v in dropped.items()}
    run.log("filter", doc=doc_props.get("doc_code"), kept=len(kept), dropped=stats, v_doc=v_doc, v_now=v_now,
            warnings=warnings)
    return {"kept": kept, "dropped": dropped, "v_doc": v_doc, "v_now": v_now, "warnings": warnings}
