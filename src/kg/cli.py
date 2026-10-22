"""Command line for every offline job, the v2 services and the evaluation suites.

    python -m kg.cli <command> [options]        (run from the repo root; ``src/`` is put on the path)

Regulation side (once, then after every new amendment):
    rebuild-graph            parse-acts + parse-amendments + consolidate + extract-refs (no LLM)
    extract-obligations      v2 extraction + amendment diff (LLM; --dry-run shows the number of requests)
Company side (per document version, all steps resumable and free to repeat):
    seed-demo | create-user | upload | profile | map | propose | sync-tasks      (or ``pipeline`` for all of them)
    tasks | explain-task | recompute | revise | publish
Checks:
    migrate | smoke-llm | count-tokens | status | eval-templates | eval --suite extraction|triage|compliance|proposals|ops
Model switching is a config change only: see ``llm:`` in configs/kg.yaml (single_model, models, step_models,
daily_request_limit, model_overrides). Legacy v1 commands (map-coverage, check-doc ...) are kept until v2 is validated.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kg.config import load_config  # noqa: E402
from kg.llm import LLM, BudgetExhausted, LLMRequestRejected, LLMUnavailable  # noqa: E402
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
        print(f"LLM budget exhausted: {exc}\nRe-run later; every finished step is stored and is not asked again.",
              file=sys.stderr)
        return 2
    except LLMUnavailable as exc:
        print(f"LLM unavailable: {exc}", file=sys.stderr)
        return 3
    except LLMRequestRejected as exc:
        print(f"LLM request rejected: {exc}", file=sys.stderr)
        return 1


def _actor(store, email: str | None) -> dict:
    from kg.services.accounts import AccountError
    row = store.q1("SELECT id FROM users WHERE email=?", ((email or "").strip().lower(),))
    if row is None:
        raise AccountError("no_user", f"unknown user '{email}' (see create-user / seed-demo)")
    from kg.services.accounts import get_user
    return get_user(store, row["id"])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="kg")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("parse-acts", "consolidate", "extract-refs", "coverage-alerts", "status", "migrate", "alerts"):
        sub.add_parser(name)
    p = sub.add_parser("rebuild-graph", help="parse-acts + parse-amendments + consolidate + extract-refs (no LLM)")
    p = sub.add_parser("parse-amendments"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--llm-fallback", action="store_true", help="use the LLM for unresolved instructions")
    p = sub.add_parser("extract-obligations"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--act", action="append"); p.add_argument("--force", action="store_true")
    p.add_argument("--retry-invalid", action="store_true"); p.add_argument("--legacy", action="store_true",
                                                                          help="the v1 extraction")
    p = sub.add_parser("reset-quota", help="clear today's 'used up' flag of a model (after a wrongly detected limit)")
    p.add_argument("--model", help="LiteLLM id; default: every model")
    p = sub.add_parser("smoke-llm", help="one tiny request: does the configured model id / key work?")
    p.add_argument("--step", default="smoke")
    p = sub.add_parser("count-tokens", help="size of the corpus documents for the whole-document threshold")
    p.add_argument("--model", default=None)
    # --- company side
    p = sub.add_parser("seed-demo"); p.add_argument("--no-docs", action="store_true")
    p = sub.add_parser("create-user")
    p.add_argument("--email", required=True); p.add_argument("--name", required=True)
    p.add_argument("--password", required=True); p.add_argument("--type", required=True, choices=["entity", "regulator"])
    p.add_argument("--company-name"); p.add_argument("--entity-type"); p.add_argument("--join")
    p = sub.add_parser("upload"); p.add_argument("pdf"); p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--new-version-of", help="family id, e.g. bank-a/POL-RM-014"); p.add_argument("--replace", action="store_true")
    p = sub.add_parser("profile"); p.add_argument("version"); p.add_argument("--as", dest="as_user")
    p.add_argument("--confirm", action="store_true", help="confirm the profile (needs --as)")
    p.add_argument("--domain", action="append"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("map"); p.add_argument("version"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--now"); p.add_argument("--retriage", action="store_true"); p.add_argument("--rejudge", action="store_true")
    p.add_argument("--force", action="store_true"); p.add_argument("--retry-invalid", action="store_true")
    p = sub.add_parser("propose"); p.add_argument("version"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("sync-tasks"); p.add_argument("version")
    p = sub.add_parser("check", help="the check of one version (map -> propose -> sync tasks); the web app runs it "
                                     "in the background and shows its status")
    p.add_argument("version"); p.add_argument("--as", dest="as_user", required=True)
    p = sub.add_parser("check-all", help="unattended check of many document versions (overnight): waits and "
                                         "continues on model errors, writes runs/check-all-report.md")
    p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--version", action="append", help="only these versions (default: all of the user's company)")
    p.add_argument("--skip", action="append", default=[], help="leave this version out (repeatable)")
    p.add_argument("--make-drafts", action="store_true", help="published versions become drafts (editable review)")
    p.add_argument("--auto-confirm", action="store_true", help="confirm the model's profile without review")
    p = sub.add_parser("merge-proposals", help="merge (with the model) the clauses that several proposals change, "
                                               "so the analysis page shows one wording per clause")
    p.add_argument("version"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("pipeline", help="profile (+confirm) -> map -> propose -> sync-tasks for one version")
    p.add_argument("version"); p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--confirm-profile", action="store_true"); p.add_argument("--now")
    p.add_argument("--domain", action="append"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("tasks"); p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--company"); p.add_argument("--status"); p.add_argument("--priority"); p.add_argument("--cause")
    p = sub.add_parser("explain-task"); p.add_argument("key"); p.add_argument("--as", dest="as_user", required=True)
    p = sub.add_parser("recompute", help="re-score tasks and re-sync them from stored results (no LLM)")
    p.add_argument("--company")
    p = sub.add_parser("revise"); p.add_argument("version"); p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--all-proposals", action="store_true", help="include proposals of tasks that are not accepted")
    p = sub.add_parser("publish"); p.add_argument("version"); p.add_argument("--as", dest="as_user", required=True)
    p.add_argument("--force", action="store_true")
    # --- legacy v1
    p = sub.add_parser("ingest-company-docs"); p.add_argument("--file", action="append")
    p.add_argument("--llm-fallback", action="store_true")
    p = sub.add_parser("map-coverage"); p.add_argument("--dry-run", action="store_true")
    p.add_argument("--doc", action="append", help="doc id, e.g. doc:POL-RM-014@3.0")
    p = sub.add_parser("replay"); p.add_argument("--act", required=True); p.add_argument("--amendment", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("eval"); p.add_argument("--suite", required=True)
    p.add_argument("--version", help="document version for the v2 suites"); p.add_argument("--refresh", action="store_true")
    p = sub.add_parser("eval-templates", help="write the gold-label templates (units pre-filled) if missing")
    p.add_argument("--act")
    p = sub.add_parser("qa"); p.add_argument("question"); p.add_argument("--as-of"); p.add_argument("--act")
    p = sub.add_parser("compare"); p.add_argument("--act", required=True)
    p.add_argument("--date-a", required=True); p.add_argument("--date-b", required=True)
    p.add_argument("--summary", action="store_true")
    p = sub.add_parser("check-doc"); p.add_argument("doc_id")
    p = sub.add_parser("check-upload"); p.add_argument("pdf")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - expected, user-facing errors of the services are printed, not raised
        from kg.mapping.pipeline import MappingError
        from kg.services.accounts import AccountError
        from kg.services.decisions import DecisionError
        from kg.services.documents import DocumentError
        from kg.services.permissions import PermissionDenied
        from kg.services.tasks import TaskError
        if isinstance(exc, (MappingError, AccountError, DecisionError, DocumentError, PermissionDenied, TaskError,
                            KeyError)):
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
            print(f"error: {msg}", file=sys.stderr)
            return 1
        raise


def _run(args) -> int:
    c = args.cmd

    # ------------------------------------------------------------------ regulation side
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
    if c == "rebuild-graph":
        from kg.ingest.jobs import consolidate, extract_refs, parse_acts, parse_amendments

        def go(cfg, s, run, llm):
            return {"parse_acts": parse_acts(cfg, s, run), "parse_amendments": parse_amendments(cfg, s, run, None, False),
                    "consolidate": consolidate(cfg, s, run), "extract_refs": extract_refs(cfg, s, run)}
        return _job(args, c, go)
    if c == "extract-obligations":
        if args.legacy:
            from kg.extract.obligations import extract_obligations
            return _job(args, c, lambda cfg, s, run, llm: extract_obligations(cfg, s, run, llm, args.dry_run, args.act),
                        needs_llm=not args.dry_run)
        from kg.extract.obligations_v2 import extract_obligations_v2
        return _job(args, c, lambda cfg, s, run, llm: extract_obligations_v2(
            cfg, s, run, llm, args.dry_run, args.act, args.force, args.retry_invalid), needs_llm=not args.dry_run)

    # ------------------------------------------------------------------ checks
    if c == "migrate":
        cfg, store = _ctx(args)
        tables = [r["name"] for r in store.q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        _print({"db": str(cfg.path("db")), "tables": {t: store.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in tables}})
        return 0
    if c == "smoke-llm":
        from kg.llm import system_user

        def go(cfg, s, run, llm):
            r = llm.call(args.step, system_user("Reply with the JSON object {\"ok\": true} and nothing else.", "ping"),
                         use_cache=False)
            return {"model": r.model, "text": r.text[:200], "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens, "models_configured": llm.models_for(args.step),
                    "daily_limit": llm.limit}
        return _job(args, c, go, needs_llm=True)
    if c == "reset-quota":
        from kg.llm import quota_day
        cfg, store = _ctx(args)
        day = quota_day()
        q, a = "UPDATE llm_quota SET exhausted=0 WHERE day=?", [day]
        if args.model:
            q += " AND model=?"
            a.append(args.model)
        store.x(q, a)
        _print({"day": day, "reset": [r for r in store.q("SELECT model, requests, exhausted FROM llm_quota "
                                                        "WHERE day=?", (day,))]})
        return 0
    if c == "count-tokens":
        return _count_tokens(args)
    if c == "status":
        return _status(args)

    # ------------------------------------------------------------------ company side
    if c == "seed-demo":
        from kg.services.demo import seed_demo
        return _job(args, c, lambda cfg, s, run, llm: seed_demo(cfg, s, with_docs=not args.no_docs))
    if c == "create-user":
        from kg.services.accounts import register

        def go(cfg, s, run, llm):
            company = ({"name": args.company_name, "entity_type": args.entity_type or cfg.get("entity_type")}
                       if args.company_name else None)
            return register(s, email=args.email, name=args.name, password=args.password, user_type=args.type,
                            company=company, join_company=args.join, entity_types=cfg.get("entity_types"))
        return _job(args, c, go)
    if c == "upload":
        from kg.services import documents

        def go(cfg, s, run, llm):
            actor = _actor(s, args.as_user)
            data = Path(args.pdf).read_bytes()
            return documents.upload(cfg, s, actor, data, Path(args.pdf).name,
                                    mode="new_version" if args.new_version_of else "new_document",
                                    family_id=args.new_version_of, version_policy="replace" if args.replace else "keep")
        return _job(args, c, go)
    if c == "profile":
        from kg.company import profile as P

        def go(cfg, s, run, llm):
            p = P.build_profile(cfg, s, run, llm, args.version, force=args.force)
            if args.confirm or args.domain:
                p = P.confirm_profile(s, _actor(s, args.as_user), args.version, domains=args.domain)
            return p
        return _job(args, c, go, needs_llm=True)
    if c == "map":
        from kg.mapping.pipeline import map_document, plan_map
        if args.dry_run:
            return _job(args, c, lambda cfg, s, run, llm: plan_map(cfg, s, run, args.version, args.now,
                                                                   require_confirmed=False))
        return _job(args, c, lambda cfg, s, run, llm: map_document(
            cfg, s, run, llm, args.version, now=args.now, retriage=args.retriage, rejudge=args.rejudge,
            force=args.force, retry_invalid=args.retry_invalid), needs_llm=True)
    if c == "propose":
        from kg.mapping.proposals import propose_document
        return _job(args, c, lambda cfg, s, run, llm: propose_document(cfg, s, run, llm, args.version, args.force),
                    needs_llm=True)
    if c == "sync-tasks":
        from kg.services.tasks import sync_tasks
        return _job(args, c, lambda cfg, s, run, llm: sync_tasks(cfg, s, run, args.version))
    if c == "check":
        from kg.services.jobs import run_check

        def go(cfg, s, run, llm):
            out = run_check(cfg, s, run, llm, args.version, _actor(s, args.as_user))
            return {k: (v if not isinstance(v, dict) else {kk: vv for kk, vv in v.items()
                                                           if not isinstance(vv, (list, dict))}) for k, v in out.items()}
        return _job(args, c, go, needs_llm=True, params={"version": args.version})
    if c == "check-all":
        from kg.services.jobs import check_all
        cfg, store = _ctx(args)
        out = check_all(cfg, store, _actor(store, args.as_user), args.version, skip=args.skip,
                        make_drafts=args.make_drafts, auto_confirm=args.auto_confirm,
                        log=lambda m: print(f"{__import__('kg.config', fromlist=['now_iso']).now_iso()} {m}",
                                            flush=True))
        _print(out)
        return 0 if all(v == "done" for v in out["results"].values()) else 1
    if c == "merge-proposals":
        from kg.services.revise import merge_pending
        return _job(args, c, lambda cfg, s, run, llm: merge_pending(cfg, s, run, llm, args.version, force=args.force),
                    needs_llm=True, params={"version": args.version})
    if c == "pipeline":
        return _job(args, c, lambda cfg, s, run, llm: _pipeline(cfg, s, run, llm, args), needs_llm=True)
    if c == "tasks":
        from kg.services import tasks as T
        cfg, store = _ctx(args)
        actor = _actor(store, args.as_user)
        company = args.company or actor.get("company_id")
        rows = T.list_tasks(cfg, store, actor, company, status=args.status, priority=args.priority, cause=args.cause)
        _print([{k: r[k] for k in ("key", "title", "status", "priority", "score", "severity", "verdict", "cause",
                                   "amendment_number", "is_new")} for r in rows])
        return 0
    if c == "explain-task":
        from kg.services import tasks as T
        cfg, store = _ctx(args)
        _print(T.explain_task(cfg, store, _actor(store, args.as_user), args.key))
        return 0
    if c == "recompute":
        return _job(args, c, lambda cfg, s, run, llm: _recompute(cfg, s, run, args.company))
    if c == "revise":
        from kg.services.revise import build_revised

        def go(cfg, s, run, llm):
            actor = _actor(s, args.as_user)
            if args.all_proposals:
                ids = [r["id"] for r in s.q("SELECT id FROM proposals WHERE doc_version_id=?", (args.version,))]
            else:
                ids = [r["proposal_id"] for r in s.q("SELECT proposal_id FROM tasks WHERE doc_version_id=? "
                                                     "AND status='accepted'", (args.version,)) if r["proposal_id"]]
            out = build_revised(cfg, s, run, llm, actor, args.version, ids)
            out["changes"] = len(out["changes"])
            return out
        return _job(args, c, go, needs_llm=True)
    if c == "publish":
        from kg.services import documents
        return _job(args, c, lambda cfg, s, run, llm: documents.publish(s, _actor(s, args.as_user), args.version,
                                                                        args.force))

    # ------------------------------------------------------------------ legacy v1 + services
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
    if c == "eval-templates":
        from kg.eval.gold_v2 import make_templates
        return _job(args, c, lambda cfg, s, run, llm: make_templates(cfg, s, args.act))
    if c == "eval":
        from kg.eval.runner import run_suite

        def go(cfg, s, run, llm):
            if args.suite == "compliance" and args.refresh:
                from kg.mapping.coverage import map_coverage
                map_coverage(cfg, s, run, LLM(cfg, s, run.id))
            res = run_suite(cfg, s, run, args.suite, **({"version": args.version} if args.version else {}))
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
    return 1


# ====================================================================================================== helpers
def _pipeline(cfg, store, run, llm, args) -> dict:
    """profile (optionally confirmed) -> map -> propose -> sync tasks. Every step reuses stored answers."""
    from kg.company import profile as P
    from kg.mapping.pipeline import map_document
    from kg.mapping.proposals import propose_document
    from kg.services.tasks import sync_tasks
    actor = _actor(store, args.as_user)
    out = {}
    prof = P.build_profile(cfg, store, run, llm, args.version, force=args.force)
    if args.confirm_profile or args.domain:
        prof = P.confirm_profile(store, actor, args.version, domains=args.domain)
    out["profile"] = {k: prof[k] for k in ("doc_type", "domains", "confirmed", "from_model")}
    if not prof["confirmed"]:
        out["stopped"] = "the profile is not confirmed: check it, then repeat with --confirm-profile"
        return out
    out["map"] = map_document(cfg, store, run, llm, args.version, now=args.now, force=args.force)
    out["propose"] = propose_document(cfg, store, run, llm, args.version, force=args.force)
    out["tasks"] = sync_tasks(cfg, store, run, args.version, actor["id"])
    return out


def _recompute(cfg, store, run, company: str | None) -> dict:
    from kg.services.tasks import rescore, sync_tasks
    n = rescore(cfg, store, company)
    synced = {}
    rows = store.q("SELECT DISTINCT doc_version_id FROM proposals")
    for r in rows:
        v = r["doc_version_id"]
        if company and not v.startswith(f"doc:{company}/"):
            continue
        synced[v] = sync_tasks(cfg, store, run, v)
    return {"rescored": n, "synced": synced}


def _count_tokens(args) -> int:
    from kg.company.pdf_parser import parse_pdf
    cfg = load_config(args.config)
    model = args.model or (cfg["llm"].get("single_model") or cfg["llm"]["models"][0])
    cpt = float(cfg.get_in("mapping.chars_per_token", 2.2))
    limit = int(cfg.get_in("mapping.whole_doc_max_tokens", 30000))
    counter, method = None, f"estimate ({cpt} chars/token)"
    try:
        import litellm
        counter = lambda t: litellm.token_counter(model=model, text=t)  # noqa: E731
        counter("test")
        method = f"litellm.token_counter({model})"
    except Exception:  # noqa: BLE001
        counter = None
    rows = []
    for f in sorted(cfg.path("company_docs").glob("D*.pdf")):
        pd = parse_pdf(f, classify_llm=False)
        text = "\n".join(u.text for u in pd.units)
        try:
            tokens = counter(text) if counter else int(len(text) / cpt) + 1
        except Exception:  # noqa: BLE001
            tokens = int(len(text) / cpt) + 1
        rows.append({"file": f.name, "chars": len(text), "tokens": tokens, "whole_document_mode": tokens <= limit})
    _print({"method": method, "whole_doc_max_tokens": limit, "documents": rows})
    return 0


def _status(args) -> int:
    from collections import Counter
    cfg, store = _ctx(args)
    from kg.steps import reuse_stats
    llm = LLM(cfg, store)
    _print({"nodes": dict(Counter(n["type"] for n in store.nodes())),
            "obligations_v2": sum(1 for n in store.nodes("Obligation") if n["props"].get("schema") == "v2"),
            "companies": store.q1("SELECT COUNT(*) n FROM companies")["n"],
            "users": store.q1("SELECT COUNT(*) n FROM users")["n"],
            "document_versions": {r["status"]: r["n"] for r in store.q(
                "SELECT status, COUNT(*) n FROM document_versions GROUP BY status")},
            "tasks": {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")},
            "step_outputs": reuse_stats(store),
            "legacy": {"findings": len(store.findings()), "alerts_open": len(store.alerts())},
            "runs": [{k: r[k] for k in ("id", "kind", "status")} for r in store.runs(limit=8)],
            "llm": {"single_model": llm.single_model, "models": llm.models, "step_models": llm.step_models,
                    "daily_request_limit": llm.limit, "budget_remaining": llm.remaining_budget()}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
