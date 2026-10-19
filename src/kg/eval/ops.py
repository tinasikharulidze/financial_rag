"""Ops metrics from ``llm_calls`` and ``runs`` (latency, tokens, cache hits)."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    vals = sorted(vals)
    return vals[min(len(vals) - 1, max(0, math.ceil(q * len(vals)) - 1))]


def llm_metrics(store, run_id: str | None = None) -> dict:
    calls = store.llm_calls(run_id)
    by = defaultdict(list)
    for c in calls:
        by[c["purpose"] or "unknown"].append(c)
    out = {}
    for purpose, cs in by.items():
        live = [c for c in cs if not c["cache_hit"] and not c["error"]]
        lat = [c["latency_ms"] for c in live if c["latency_ms"] is not None]
        out[purpose] = {
            "calls": len(cs), "cache_hits": sum(1 for c in cs if c["cache_hit"]),
            "errors": sum(1 for c in cs if c["error"]),
            "cache_hit_rate": sum(1 for c in cs if c["cache_hit"]) / len(cs),
            "input_tokens": sum(c["input_tokens"] or 0 for c in cs),
            "output_tokens": sum(c["output_tokens"] or 0 for c in cs),
            "latency_p50_ms": _pct(lat, 0.5), "latency_p95_ms": _pct(lat, 0.95)}
    return out


def run_metrics(store, limit: int = 200) -> dict:
    by = defaultdict(list)
    for r in store.runs(limit=limit):
        if r["finished_at"]:
            dt = (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
            by[r["kind"]].append(dt)
    return {k: {"runs": len(v), "p50_s": _pct(v, .5), "p95_s": _pct(v, .95)} for k, v in by.items()}


def run_ops(cfg, store) -> dict:
    llm = llm_metrics(store)
    total_calls = sum(v["calls"] for v in llm.values())
    hits = sum(v["cache_hits"] for v in llm.values())
    metrics = {"llm_calls": total_calls, "cache_hit_rate": hits / total_calls if total_calls else None,
               "input_tokens": sum(v["input_tokens"] for v in llm.values()),
               "output_tokens": sum(v["output_tokens"] for v in llm.values()),
               "by_purpose": llm, "by_run_kind": run_metrics(store)}
    items = [{"purpose": k, **v} for k, v in llm.items()]
    return {"metrics": metrics, "items": items}
