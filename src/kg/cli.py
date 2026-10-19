"""Command line for every offline job and the evaluation suites.

    python -m kg.cli <command> [options]        (run from the repo root; ``src/`` is put on the path)

Pipeline order:  parse-acts -> parse-amendments -> consolidate -> extract-refs -> extract-obligations
                 -> ingest-company-docs -> map-coverage -> coverage-alerts -> replay / eval
Use ``--dry-run`` on the LLM jobs to see how many requests they would need (20 per model and day).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kg.config import load_config  # noqa: E402
from kg.llm import LLM, BudgetExhausted  # noqa: E402
from kg.runlog import Run  # noqa: E402
from kg.store.sqlite_nx import SQLiteStore  # noqa: E402


def _ctx(args):
    cfg = load_config(args.config)
    cfg.path("data_dir").mkdir(parents=True, exist_ok=True)
    return cfg, SQLiteStore(cfg.path("db"))


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _job(args, kind: str, fn, needs_llm: bool = False, params: dict | None = None):
    cfg, store = _ctx(args)
    try:
        with Run(store, cfg.path("runs"), kind, params or {}) as run:
            llm = LLM(cfg, store, run.id) if needs_llm else None
            result = fn(cfg, store, run, llm)
            result_out = result if not isinstance(result, dict) else {k: v for k, v in result.items() if k != "new_alerts"}
            _print({"run_id": run.id, "result": result_out, "log": str(run.log_path)})
        return 0
    except BudgetExhausted as exc:
        print(f"LLM budget exhausted: {exc}\nRe-run tomorrow; completed work is cached.", file=sys.stderr)
        return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kg")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("parse-acts", "consolidate", "extract-refs", "coverage-alerts", "status"):
        sub.add_parser(name)
    p = sub.add_parser("parse-amendments"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--llm-fallback", action="store_true", help="use the LLM for unresolved instructions")
    p = sub.add_parser("extract-obligations"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--act", action="append")
    p = sub.add_parser("ingest-company-docs"); p.add_argument("--file", action="append")
    p.add_argument("--llm-fallback", action="store_true")
    p = sub.add_parser("map-coverage"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--doc", action="append", help="doc id, e.g. doc:POL-RM-014@3.0")
    p = sub.add_parser("replay"); p.add_argument("--act", required=True); p.add_argument("--amendment", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("eval"); p.add_argument("--suite", required=True,
                                                choices=["compliance", "consolidation", "ops", "qa"])
    p.add_argument("--refresh", action="store_true", help="compliance: re-run map-coverage first (cache, no new calls)")
    p = sub.add_parser("qa"); p.add_argument("question"); p.add_argument("--as-of"); p.add_argument("--act")
    p = sub.add_parser("compare"); p.add_argument("--act", required=True)
    p.add_argument("--date-a", required=True); p.add_argument("--date-b", required=True)
    p.add_argument("--summary", action="store_true")
    p = sub.add_parser("check-doc"); p.add_argument("doc_id")
    p = sub.add_parser("check-upload"); p.add_argument("pdf")
    sub.add_parser("alerts")
    args = ap.parse_args(argv)
    c = args.cmd

    if c == "parse-acts":
        from kg.ingest.jobs import parse_acts
        return _job(args, c, lambda cfg, s, run, llm: parse_acts(cfg, s, run))
    if c == "parse-amendments":
        from kg.ingest.jobs import parse_amendments
        return _job(args, c, lambda cfg, s, run, llm: parse_amendments(cfg, s, run, llm, args.dry_run),
                    needs_llm=args.llm_fallback and not args.dry_run)
    if c == "consolidate":
        from kg.ingest.jobs import consolidate
        return _job(args, c, lambda cfg, s, run, llm: consolidate(cfg, s, run))
    if c == "extract-refs":
        from kg.ingest.jobs import extract_refs
        return _job(args, c, lambda cfg, s, run, llm: extract_refs(cfg, s, run))
    if c == "extract-obligations":
        from kg.extract.obligations import extract_obligations
        return _job(args, c, lambda cfg, s, run, llm: extract_obligations(cfg, s, run, llm, args.dry_run, args.act),
                    needs_llm=not args.dry_run)
    if c == "ingest-company-docs":
        from kg.company.ingest import ingest_company_docs
        files = [Path(f) for f in args.file] if args.file else None
        return _job(args, c, lambda cfg, s, run, llm: ingest_company_docs(cfg, s, run, llm, files),
                    needs_llm=args.llm_fallback)
    if c == "map-coverage":
        from kg.mapping.coverage import map_coverage
        return _job(args, c, lambda cfg, s, run, llm: map_coverage(cfg, s, run, llm, args.doc, args.dry_run),
                    needs_llm=not args.dry_run)
    if c == "coverage-alerts":
        from kg.mapping.alerts import coverage_alerts
        return _job(args, c, lambda cfg, s, run, llm: coverage_alerts(cfg, s, run))
    if c == "replay":
        from kg.services.replay import replay
        return _job(args, c, lambda cfg, s, run, llm: replay(cfg, s, run, llm, args.act, args.amendment, args.dry_run),
                    needs_llm=not args.dry_run, params={"act": args.act, "amendment": args.amendment})
    if c == "eval":
        from kg.eval.runner import run_suite

        def go(cfg, s, run, llm):
            if args.suite == "compliance" and args.refresh:
                from kg.mapping.coverage import map_coverage
                map_coverage(cfg, s, run, LLM(cfg, s, run.id))
            res = run_suite(cfg, s, run, args.suite)
            return {"dir": res["dir"], "metrics": res["metrics"]}
        return _job(args, c, go, params={"suite": args.suite})
    if c == "qa":
        from kg.services.qa import answer
        return _job(args, c, lambda cfg, s, run, llm: answer(cfg, s, llm, args.question, args.as_of, args.act),
                    needs_llm=True)
    if c == "compare":
        from kg.services import compare as cmp

        def go(cfg, s, run, llm):
            r = cmp.versions(s, args.act, args.date_a, args.date_b)
            if args.summary:
                r["summary"] = cmp.summarize(llm, r)
            r["changes"] = [{k: v for k, v in ch.items() if k not in ("diff", "text_a", "text_b")} for ch in r["changes"]]
            return r
        return _job(args, c, go, needs_llm=args.summary)
    if c == "check-doc":
        from kg.services.doccheck import check_existing
        cfg, store = _ctx(args)
        _print([{k: r[k] for k in ("priority", "gap_class", "version_basis", "action_type", "issue_chance", "severity",
                                   "section_no", "source", "rationale", "suggested_fix")}
                for r in check_existing(store, args.doc_id)])
        return 0
    if c == "check-upload":
        from kg.services.doccheck import check_upload
        return _job(args, c, lambda cfg, s, run, llm: check_upload(
            cfg, s, llm, run, args.pdf, progress=lambda st, d, t: print(f"  {st}: {d}/{t}", file=sys.stderr)),
            needs_llm=True)
    if c == "alerts":
        cfg, store = _ctx(args)
        _print(store.alerts())
        return 0
    if c == "status":
        from collections import Counter
        cfg, store = _ctx(args)
        _print({"nodes": dict(Counter(n["type"] for n in store.nodes())),
                "findings": len(store.findings()), "alerts_open": len(store.alerts()),
                "runs": [{k: r[k] for k in ("id", "kind", "status")} for r in store.runs(limit=8)],
                "llm_budget_remaining": LLM(cfg, store).remaining_budget()})
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
