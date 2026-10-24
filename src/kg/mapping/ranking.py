"""Task ranking (plan section 9.2); the settings live in ``configs/kg.yaml`` (``ranking``).

Two modes (``ranking.mode``):

* ``signals`` (default): the priority follows rules over what the pipeline already knows about a gap:
    - critical: the verdict is in ``signals.critical`` (contradicts), whatever its relevance;
    - high:     every signal in ``signals.high_requires`` holds (relevant AND caused by an amendment);
    - medium:   at least one signal in ``signals.medium_any`` holds (relevant / amendment / document type);
    - low:      the rest.
  The signals: ``relevant`` (triage), ``amendment`` (coverage cause: the obligation comes from an amendment the
  document predates), ``doc_type`` (the obligation names the document type it must be written in, and it is the type
  of this document). The score orders the tasks: the base of the priority plus a small step per signal.
* ``formula``: ``score = severity x gap x relevance`` with weights and ``priority_thresholds`` (the earlier ranking).

Pure functions, no LLM: changing a setting and calling ``tasks.rescore`` re-ranks everything.
"""
from __future__ import annotations

DEFAULTS = {
    "severity": {"critical": 4, "high": 3, "medium": 2, "low": 1},
    "gap": {"contradicts": 1.0, "none": 1.0, "partial": 0.6},
    "relevance": {"relevant": 1.0, "possibly": 0.6},
    "priority_thresholds": {"critical": 3.5, "high": 2.4, "medium": 1.2},
}
SIGNAL_DEFAULTS = {"critical": ["contradicts"], "high_requires": ["relevant", "amendment"],
                   "medium_any": ["relevant", "amendment", "doc_type"]}
SIGNAL_NAMES = ("relevant", "amendment", "doc_type")
BASE = {"critical": 0.9, "high": 0.7, "medium": 0.4, "low": 0.1}     # signals mode: score = base + STEP x signals
STEP = 0.03
BUCKETS = ("critical", "high", "medium", "low")


def mode(cfg) -> str:
    return ((cfg.get("ranking") or {}).get("mode") or "signals").lower()


def weights(cfg) -> dict:
    r = cfg.get("ranking") or {}
    return {k: {**DEFAULTS[k], **(r.get(k) or {})} for k in DEFAULTS}


def signal_rules(cfg) -> dict:
    return {**SIGNAL_DEFAULTS, **((cfg.get("ranking") or {}).get("signals") or {})}


def doc_type_match(ob_props: dict | None, doc_type: str | None) -> bool:
    """The obligation lists the document types it belongs in (``required_doc.doc_types``) and this document's
    profile type is one of them (``policy`` matches ``model_risk_policy`` and the other way round)."""
    types = ((ob_props or {}).get("required_doc") or {}).get("doc_types") or []
    dt = (doc_type or "").strip().lower()
    return bool(dt) and any(t and (t.lower() in dt or dt in t.lower()) for t in types)


def signals(relevance: str | None, cause: str | None = None, doc_match: bool = False) -> dict:
    return {"relevant": relevance == "relevant", "amendment": cause == "amendment", "doc_type": bool(doc_match)}


def factors(cfg, severity: str | None, verdict: str | None, relevance: str | None) -> dict:
    w = weights(cfg)
    return {"severity": w["severity"].get(severity or "medium", 2),
            "gap": w["gap"].get(verdict or "none", 1.0),
            "relevance": w["relevance"].get(relevance or "possibly", 0.6)}


def bucket(cfg, verdict: str | None, sig: dict) -> str:
    """Signals mode: the priority of a gap from its verdict and signals."""
    rules = signal_rules(cfg)
    if verdict in rules["critical"]:
        return "critical"
    if rules["high_requires"] and all(sig.get(s) for s in rules["high_requires"]):
        return "high"
    if any(sig.get(s) for s in rules["medium_any"]):
        return "medium"
    return "low"


def score(cfg, severity: str | None, verdict: str | None, relevance: str | None,
          cause: str | None = None, doc_match: bool = False) -> float:
    if mode(cfg) == "formula":
        f = factors(cfg, severity, verdict, relevance)
        return round(f["severity"] * f["gap"] * f["relevance"], 4)
    sig = signals(relevance, cause, doc_match)
    return round(BASE[bucket(cfg, verdict, sig)] + STEP * sum(sig.values()), 4)


def priority(cfg, value: float) -> str:
    t = weights(cfg)["priority_thresholds"] if mode(cfg) == "formula" else BASE
    for b in ("critical", "high", "medium"):
        if value >= t[b] - 1e-9:
            return b
    return "low"


def explain(cfg, severity, verdict, relevance, cause: str | None = None, doc_match: bool = False) -> dict:
    s = score(cfg, severity, verdict, relevance, cause, doc_match)
    out = {"mode": mode(cfg), "inputs": {"severity": severity, "verdict": verdict, "relevance": relevance,
                                         "cause": cause, "doc_type_match": bool(doc_match)},
           "score": s, "priority": priority(cfg, s)}
    if mode(cfg) == "formula":
        sev_used = len(set(weights(cfg)["severity"].values())) > 1
        out.update(formula="severity x gap x relevance" if sev_used else "gap x relevance",
                   factors=factors(cfg, severity, verdict, relevance), thresholds=weights(cfg)["priority_thresholds"])
    else:
        out.update(formula="rules: critical = " + "/".join(signal_rules(cfg)["critical"])
                   + "; high = " + " and ".join(signal_rules(cfg)["high_requires"])
                   + "; medium = any of " + ", ".join(signal_rules(cfg)["medium_any"]),
                   signals=signals(relevance, cause, doc_match))
    return out
