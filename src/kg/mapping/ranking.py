"""Task ranking (plan section 9.2): ``score = severity x gap x relevance``; weights live in ``configs/kg.yaml``.

Pure functions, no LLM: changing a weight and calling ``tasks.rescore`` re-ranks everything. The amendment cause is
NOT part of the score (the board filters and badges it), so a regulator compares like with like.
"""
from __future__ import annotations

DEFAULTS = {
    "severity": {"critical": 4, "high": 3, "medium": 2, "low": 1},
    "gap": {"contradicts": 1.0, "none": 1.0, "partial": 0.6},
    "relevance": {"relevant": 1.0, "possibly": 0.6},
    "priority_thresholds": {"critical": 3.5, "high": 2.4, "medium": 1.2},
}
BUCKETS = ("critical", "high", "medium", "low")


def weights(cfg) -> dict:
    r = cfg.get("ranking") or {}
    return {k: {**DEFAULTS[k], **(r.get(k) or {})} for k in DEFAULTS}


def factors(cfg, severity: str | None, verdict: str | None, relevance: str | None) -> dict:
    w = weights(cfg)
    return {"severity": w["severity"].get(severity or "medium", 2),
            "gap": w["gap"].get(verdict or "none", 1.0),
            "relevance": w["relevance"].get(relevance or "possibly", 0.6)}


def score(cfg, severity: str | None, verdict: str | None, relevance: str | None) -> float:
    f = factors(cfg, severity, verdict, relevance)
    return round(f["severity"] * f["gap"] * f["relevance"], 4)


def priority(cfg, value: float) -> str:
    t = weights(cfg)["priority_thresholds"]
    for b in ("critical", "high", "medium"):
        if value >= t[b]:
            return b
    return "low"


def explain(cfg, severity, verdict, relevance) -> dict:
    f = factors(cfg, severity, verdict, relevance)
    s = score(cfg, severity, verdict, relevance)
    sev_used = len(set(weights(cfg)["severity"].values())) > 1
    return {"formula": "severity x gap x relevance" if sev_used else "gap x relevance", "inputs": {"severity": severity, "verdict": verdict,
                                                                "relevance": relevance},
            "factors": f, "score": s, "priority": priority(cfg, s),
            "thresholds": weights(cfg)["priority_thresholds"]}
