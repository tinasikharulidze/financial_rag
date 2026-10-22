"""Background checks (შემოწმება) of a document version.

The web app must stay usable while a check runs (30-60 minutes with Gemma), so the check runs in its own process:

    launch(...)      -> starts ``python -m kg.cli check <version> --as <email>`` detached; a ``check_jobs`` row
                        (status ``queued``) is written at once, so the UI shows it immediately
    run_check(...)   -> what that process does: status ``running`` -> map -> propose -> sync tasks -> ``done``
                        (or ``budget`` / ``unavailable`` / ``rejected`` / ``failed`` with a message)
    get_job(...)     -> the row for the UI; a ``queued``/``running`` job whose process is gone becomes ``failed``
    progress(...)    -> what is already stored (triage / judge / proposals / tasks), for the progress line

Every step of the check stores its answers, so repeating a failed check continues where it stopped.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from kg.config import REPO_ROOT, now_iso
from kg.services.permissions import require

ACTIVE = ("queued", "running")
FINAL = ("done", "failed", "budget", "unavailable", "rejected")


_TABLE = """CREATE TABLE IF NOT EXISTS check_jobs (
    version_id TEXT PRIMARY KEY, status TEXT NOT NULL, pid INTEGER, actor INTEGER, run_id TEXT, log_path TEXT,
    message TEXT, started_at TEXT NOT NULL, finished_at TEXT)"""


def _ensure(store):
    """The table also exists for a store opened before this code was installed (e.g. a running web app)."""
    if not getattr(store, "_check_jobs_ok", False):
        store.x(_TABLE)
        store._check_jobs_ok = True


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:            # exists, owned by someone else
        return True
    except OSError:
        return False
    try:                               # a finished child that was never reaped is a zombie: not running
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return True


def _set(store, version_id: str, **fields):
    _ensure(store)
    cols = ", ".join(f"{k}=?" for k in fields)
    store.x(f"UPDATE check_jobs SET {cols} WHERE version_id=?", (*fields.values(), version_id))


def get_job(store, version_id: str) -> dict | None:
    _ensure(store)
    j = store.q1("SELECT * FROM check_jobs WHERE version_id=?", (version_id,))
    if j and j["status"] in ACTIVE and j["pid"] and not _alive(j["pid"]):
        msg = "the check process ended unexpectedly; start it again (finished steps are kept)"
        _set(store, version_id, status="failed", message=msg, finished_at=now_iso())
        j = {**j, "status": "failed", "message": msg}
    return j


def is_running(store, version_id: str) -> bool:
    j = get_job(store, version_id)
    return bool(j and j["status"] in ACTIVE)


def progress(store, version_id: str) -> dict:
    n = lambda t: store.q1(f"SELECT COUNT(*) n FROM {t} WHERE doc_version_id=?", (version_id,))["n"]  # noqa: E731
    return {"triage": n("triage"), "located": n("locations"), "judged": n("coverage"), "proposals": n("proposals"),
            "tasks": n("tasks")}


def launch(cfg, store, actor: dict, version_id: str, *, config_path: str | None = None, spawn=None) -> dict:
    """Start the check in the background (no-op when one is already running). ``spawn(cmd, log_path)`` is
    replaceable in tests; the default starts a detached process."""
    v = store.q1("SELECT dv.id, d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                 "WHERE dv.id=?", (version_id,))
    if v is None:
        raise KeyError(f"document version not found: {version_id}")
    require(actor, "decision.make", v["company_id"])
    _ensure(store)
    j = get_job(store, version_id)
    if j and j["status"] in ACTIVE:
        return j
    runs = cfg.path("runs")
    runs.mkdir(parents=True, exist_ok=True)
    safe = version_id.replace("/", "_").replace(":", "_").replace("@", "_")
    log_path = runs / f"check-{safe}.log"
    store.x("INSERT OR REPLACE INTO check_jobs(version_id,status,pid,actor,run_id,log_path,message,started_at,"
            "finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (version_id, "queued", None, actor["id"], None, str(log_path), None, now_iso(), None))
    cmd = [sys.executable, "-m", "kg.cli"] + (["--config", str(config_path)] if config_path else []) + \
          ["check", version_id, "--as", actor["email"]]
    pid = (spawn or _spawn)(cmd, log_path)
    if pid:
        _set(store, version_id, pid=int(pid))
    return get_job(store, version_id)


def _spawn(cmd: list[str], log_path: Path) -> int:
    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    log = open(log_path, "a", encoding="utf-8")                    # noqa: SIM115 - handed to the child
    p = subprocess.Popen(cmd, cwd=str(REPO_ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    return p.pid


def _merge(cfg, store, run, llm, version_id: str) -> dict:
    """The last, optional step: one wording for every clause that several proposals change. When the model fails
    here the check is still done (the page shows a word-level preview and offers to merge again)."""
    from kg.llm import LLMRequestRejected, LLMUnavailable
    from kg.services.revise import merge_pending
    try:
        return merge_pending(cfg, store, run, llm, version_id)
    except (LLMUnavailable, LLMRequestRejected) as exc:
        return {"error": str(exc)[:300]}


def run_check(cfg, store, run, llm, version_id: str, actor: dict) -> dict:
    """The check itself (called inside the background process): map -> propose -> sync tasks."""
    from kg.llm import BudgetExhausted, LLMRequestRejected, LLMUnavailable
    from kg.mapping.pipeline import map_document
    from kg.mapping.proposals import propose_document
    from kg.services.tasks import sync_tasks
    _ensure(store)
    if store.q1("SELECT 1 FROM check_jobs WHERE version_id=?", (version_id,)) is None:
        store.x("INSERT INTO check_jobs(version_id,status,actor,started_at) VALUES(?,?,?,?)",
                (version_id, "queued", actor["id"], now_iso()))
    _set(store, version_id, status="running", pid=os.getpid(), run_id=getattr(run, "id", None), message=None)
    try:
        out = {"map": map_document(cfg, store, run, llm, version_id),
               "propose": propose_document(cfg, store, run, llm, version_id),
               "tasks": sync_tasks(cfg, store, run, version_id, actor["id"])}
        out["merge"] = _merge(cfg, store, run, llm, version_id)
    except BudgetExhausted as exc:
        _set(store, version_id, status="budget", message=str(exc)[:500], finished_at=now_iso())
        raise
    except LLMUnavailable as exc:
        _set(store, version_id, status="unavailable", message=str(exc)[:500], finished_at=now_iso())
        raise
    except LLMRequestRejected as exc:
        _set(store, version_id, status="rejected", message=str(exc)[:500], finished_at=now_iso())
        raise
    except Exception as exc:  # noqa: BLE001 - recorded for the UI, then re-raised
        _set(store, version_id, status="failed", message=f"{type(exc).__name__}: {exc}"[:500], finished_at=now_iso())
        raise
    t = out["tasks"]
    _set(store, version_id, status="done", finished_at=now_iso(),
         message=f"tasks: {t.get('created', 0)} new, {t.get('updated', 0)} updated" if isinstance(t, dict) else None)
    return out


# ------------------------------------------------------------------------------------------- unattended: many documents
def _company_versions(store, company_id: str) -> list[str]:
    return [r["id"] for r in store.q("SELECT dv.id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                                     "WHERE d.company_id=? AND dv.status IN ('draft','published') ORDER BY dv.id",
                                     (company_id,))]


def _size(store, version_id: str) -> int:
    from kg.mapping.docview import load_doc
    v = load_doc(store, version_id)
    return sum(len(u["text"] or "") for u in v.units) if v else 0


def _summary(store, version_id: str) -> dict:
    n = lambda sql: store.q1(sql, (version_id,))["n"]  # noqa: E731
    verdicts = {r["verdict"]: r["n"] for r in store.q("SELECT verdict, COUNT(*) n FROM coverage WHERE doc_version_id=? "
                                                       "GROUP BY verdict", (version_id,))}
    rel = {r["relevance"]: r["n"] for r in store.q("SELECT relevance, COUNT(*) n FROM triage WHERE doc_version_id=? "
                                                    "GROUP BY relevance", (version_id,))}
    skipped = store.q("SELECT step, errors FROM step_outputs WHERE valid=0 AND subject LIKE ?", (version_id + ":%",))
    return {"relevance": rel, "verdicts": verdicts,
            "proposals": n("SELECT COUNT(*) n FROM proposals WHERE doc_version_id=?"),
            "tasks": n("SELECT COUNT(*) n FROM tasks WHERE doc_version_id=?"),
            "skipped_batches": [f"{r['step']}: {(r['errors'] or '')[:160]}" for r in skipped]}


def _report(path: Path, results: dict, started: str):
    lines = [f"# Overnight check — started {started}, last update {now_iso()}", ""]
    for v, r in results.items():
        s = r.get("summary") or {}
        lines += [f"## {v}", f"- status: **{r['status']}** (attempts: {r.get('attempts', 0)}, "
                             f"{r.get('minutes', 0)} min)"]
        if r.get("message"):
            lines.append(f"- last problem: {r['message']}")
        if s:
            lines += [f"- relevance: {s['relevance']}", f"- coverage: {s['verdicts']}",
                      f"- proposals: {s['proposals']} · tasks: {s['tasks']}"]
            if s["skipped_batches"]:
                lines.append(f"- skipped batches ({len(s['skipped_batches'])}, asked again by the next check):")
                lines += [f"  - {x}" for x in s["skipped_batches"][:20]]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def check_all(cfg, store, actor: dict, versions: list[str] | None = None, *, skip=(), make_drafts: bool = False,
              auto_confirm: bool = False, make_llm=None, sleep=None, log=print, waits: dict | None = None) -> dict:
    """Check many document versions one after the other, unattended (overnight).

    Nothing stops the whole run: every finished step is stored, so a document whose check was interrupted is simply
    started again and continues where it stopped.
      * model unavailable (5xx / overload / timeouts, after the client's own retries) -> wait, try again
      * request limit (daily flag or a 429 counted as daily) -> wait, clear the flag, try again
      * a request the provider refuses (400 / too large) -> that one batch is skipped and listed in the report
      * any other error -> logged, retried twice, then the next document; failed documents get a second pass
    A version whose check already finished only gets the merge step. ``runs/check-all-report.md`` is rewritten
    after every document."""
    import time
    import traceback

    from kg.company.profile import build_profile, confirm_profile, get_profile
    from kg.llm import LLM, BudgetExhausted, LLMRequestRejected, LLMUnavailable, quota_day
    from kg.runlog import Run
    from kg.services.revise import merge_pending
    sleep = sleep or time.sleep
    make_llm = make_llm or (lambda c, s, rid: LLM(c, s, rid))
    w = {"unavailable": 600, "unavailable_max": 12, "budget": 1800, "budget_max": 16, "other": 120, "other_max": 2}
    w.update(waits or {})
    _ensure(store)
    todo = [v for v in (versions or _company_versions(store, actor["company_id"])) if v not in set(skip)]
    todo.sort(key=lambda v: _size(store, v))                   # small documents first: results arrive early
    if make_drafts:
        for v in todo:
            store.x("UPDATE document_versions SET status='draft', published_at=NULL WHERE id=? AND status='published'",
                    (v,))
    report = cfg.path("runs") / "check-all-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    started = now_iso()
    results: dict[str, dict] = {v: {"status": "waiting"} for v in todo}
    log(f"check-all: {len(todo)} versions: {', '.join(todo)}")

    def one(v: str) -> dict:
        t0, tries = time.time(), {"unavailable": 0, "budget": 0, "other": 0}
        attempts, message = 0, None
        while True:
            attempts += 1
            try:
                with Run(store, cfg.path("runs"), "check", {"version": v, "check_all": True}) as run:
                    llm = make_llm(cfg, store, run.id)
                    llm.max_live = None                      # the per-process safety valve is for interactive use
                    llm.tolerate_rejected = True
                    done_before = (get_job(store, v) or {}).get("status") == "done"
                    if done_before:
                        log(f"[{v}] already checked: merge step only")
                        merge_pending(cfg, store, run, llm, v)
                    else:
                        prof = get_profile(store, v)
                        if prof is None or not prof["confirmed"]:
                            build_profile(cfg, store, run, llm, v)
                            if get_profile(store, v) is None:
                                raise RuntimeError("the model could not build the profile")
                            if not auto_confirm:
                                raise RuntimeError("the profile is not confirmed (use --auto-confirm)")
                            confirm_profile(store, actor, v)
                            log(f"[{v}] profile built and auto-confirmed: {get_profile(store, v)['doc_type']}")
                        log(f"[{v}] check: attempt {attempts}")
                        run_check(cfg, store, run, llm, v, actor)
                return {"status": "done", "attempts": attempts, "message": message,
                        "minutes": round((time.time() - t0) / 60)}
            except LLMUnavailable as exc:
                kind, wait, message = "unavailable", w["unavailable"], f"model unavailable: {str(exc)[:300]}"
            except BudgetExhausted as exc:
                kind, wait, message = "budget", w["budget"], f"request limit: {str(exc)[:300]}"
            except LLMRequestRejected as exc:
                kind, wait, message = "other", w["other"], f"request rejected: {str(exc)[:300]}"
            except Exception as exc:  # noqa: BLE001 - logged, retried, then the next document
                kind, wait, message = "other", w["other"], f"{type(exc).__name__}: {str(exc)[:300]}"
                log(traceback.format_exc()[-2000:])
            tries[kind] += 1
            log(f"[{v}] {message}")
            if tries[kind] > w[kind + "_max"]:
                return {"status": "stopped (" + kind + ")", "attempts": attempts, "message": message,
                        "minutes": round((time.time() - t0) / 60)}
            log(f"[{v}] waiting {wait} s, then continuing (try {tries[kind]} of {w[kind + '_max']})")
            sleep(wait)
            if kind == "budget":                             # a wrongly detected daily limit: clear the flag
                store.x("UPDATE llm_quota SET exhausted=0 WHERE day=?", (quota_day(),))

    for pass_no in (1, 2):
        for v in [v for v in todo if results[v]["status"] != "done"]:
            if pass_no == 2:
                log(f"[{v}] second pass")
            results[v] = {"status": "running"}
            _report(report, results, started)
            results[v] = one(v)
            try:
                results[v]["summary"] = _summary(store, v)
            except Exception:  # noqa: BLE001
                pass
            log(f"[{v}] {results[v]['status']} ({results[v].get('minutes')} min)")
            _report(report, results, started)
        if all(r["status"] == "done" for r in results.values()):
            break
    return {"report": str(report), "results": {v: r["status"] for v, r in results.items()}}
