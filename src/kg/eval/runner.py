"""Runs an eval suite and writes evaluation/results/<run_id>/{metrics.json,items.csv,report.md} + index.csv row."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from kg.config import now_iso


def _flat(d, prefix=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, f"{prefix}{k}.")
        else:
            yield f"{prefix}{k}", v


def report_md(suite: str, run_id: str, result: dict) -> str:
    L = [f"# Evaluation: {suite}", f"Run `{run_id}` at {now_iso()}", "", "## Metrics", "", "| metric | value |", "|---|---|"]
    for k, v in _flat(result["metrics"]):
        if isinstance(v, float):
            v = f"{v:.3f}"
        L.append(f"| {k} | {v} |")
    items = result.get("items") or []
    if items:
        L += ["", f"## Items ({len(items)})", ""]
        cols = list(items[0].keys())
        L += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for it in items[:200]:
            L.append("| " + " | ".join(str(it.get(c, ""))[:80].replace("|", "/").replace("\n", " ") for c in cols) + " |")
    for extra in ("replay_check", "per_amendment"):
        if result.get(extra):
            L += ["", f"## {extra}", "", "```json", json.dumps(result[extra], ensure_ascii=False, indent=1), "```"]
    return "\n".join(L) + "\n"


def write_results(cfg, suite: str, run_id: str, result: dict) -> Path:
    base = cfg.path("eval_results")
    out = base / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(result["metrics"], ensure_ascii=False, indent=2), encoding="utf-8")
    items = result.get("items") or []
    with open(out / "items.csv", "w", encoding="utf-8-sig", newline="") as fh:
        if items:
            w = csv.DictWriter(fh, fieldnames=list(items[0].keys()))
            w.writeheader()
            w.writerows(items)
    (out / "report.md").write_text(report_md(suite, run_id, result), encoding="utf-8")
    idx = base / "index.csv"
    new = not idx.exists()
    with open(idx, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["run_id", "suite", "timestamp", "metrics"])
        w.writerow([run_id, suite, now_iso(), json.dumps(result["metrics"], ensure_ascii=False)])
    return out


def run_suite(cfg, store, run, suite: str) -> dict:
    if suite == "compliance":
        from kg.eval import compliance
        gold = compliance.load_gold(cfg)
        res = compliance.evaluate(gold, compliance.finding_views(store))
        from kg.services import reports
        reps = []
        for r in reports.list_reports(cfg):
            full = reports.get(cfg, r["run_id"])
            if full:
                docs = {n["id"]: n["props"] for n in store.nodes("InternalDoc")}
                for f in full.get("affected", []):
                    cl = store.get_node(f["clause_id"]) if f.get("clause_id") else None
                    ob = store.get_node(f["obligation_id"])
                    f["doc_file"] = compliance.Path_name(docs.get(f["doc_id"], {}).get("file"))
                    f["section_no"] = (cl or {}).get("props", {}).get("section_no")
                    f["source_path"] = (ob or {}).get("props", {}).get("source_path")
                reps.append(full)
        res["replay_check"] = compliance.replay_check(cfg, gold, reps)
    elif suite == "consolidation":
        from kg.eval import consolidation
        res = consolidation.run_consolidation(cfg, store)
    elif suite == "ops":
        from kg.eval import ops
        res = ops.run_ops(cfg, store)
    elif suite == "qa":
        res = {"metrics": {"status": "deferred: no gold set yet"}, "items": []}
    else:
        raise ValueError(f"unknown suite {suite}")
    out = write_results(cfg, suite, run.id, res)
    run.log("eval_done", suite=suite, dir=str(out), metrics=res["metrics"])
    res["dir"] = str(out)
    return res
