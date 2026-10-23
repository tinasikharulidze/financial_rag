"""The whole process for new regulation acts and new documents, unattended (overnight):

    1. extract the obligations of the acts      (``extract_obligations_v2``, one act at a time)
    2. upload the documents with their direction (skipped when the same file was already uploaded)
    3. check the documents                      (``jobs.check_all``: profile -> map -> propose -> tasks -> merge)

Model errors never stop the run (the same rules as ``check_all``): unavailable -> wait 10 min and continue, a request
limit -> wait 30 min, any other error -> logged, retried twice, then the next act / document. Every finished step is
stored, so a repeated attempt continues where the previous one stopped. ``runs/run-all-report.md`` is rewritten
after every act and document.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path

from kg.config import now_iso

WAITS = {"unavailable": 600, "unavailable_max": 12, "budget": 1800, "budget_max": 16, "other": 120, "other_max": 2}


def acts_without_obligations(cfg, store) -> list[str]:
    """Obligation acts that have no extracted obligation yet (the model risk act is already done)."""
    have = {str(r["a"]) for r in store.q("SELECT DISTINCT json_extract(props,'$.act') a FROM nodes "
                                          "WHERE type='Obligation'")}
    return [str(a) for a in cfg["obligation_acts"] if str(a) not in have]


def _retry(store, label: str, fn, *, sleep, log, waits: dict) -> dict:
    """Run ``fn(attempt)`` until it succeeds or one kind of error happened too often."""
    from kg.llm import BudgetExhausted, LLMRequestRejected, LLMUnavailable, quota_day
    t0, tries, attempt, message = time.time(), {"unavailable": 0, "budget": 0, "other": 0}, 0, None
    while True:
        attempt += 1
        try:
            out = fn(attempt)
            return {"status": "done", "attempts": attempt, "message": message, "result": out,
                    "minutes": round((time.time() - t0) / 60)}
        except LLMUnavailable as exc:
            kind, message = "unavailable", f"model unavailable: {str(exc)[:300]}"
        except BudgetExhausted as exc:
            kind, message = "budget", f"request limit: {str(exc)[:300]}"
        except LLMRequestRejected as exc:
            kind, message = "other", f"request rejected: {str(exc)[:300]}"
        except Exception as exc:  # noqa: BLE001 - logged, retried, then the next item
            kind, message = "other", f"{type(exc).__name__}: {str(exc)[:300]}"
            log(traceback.format_exc()[-2000:])
        tries[kind] += 1
        log(f"[{label}] {message}")
        if tries[kind] > waits[kind + "_max"]:
            return {"status": f"stopped ({kind})", "attempts": attempt, "message": message,
                    "minutes": round((time.time() - t0) / 60)}
        log(f"[{label}] waiting {waits[kind]} s, then continuing (try {tries[kind]} of {waits[kind + '_max']})")
        sleep(waits[kind])
        if kind == "budget":                             # a wrongly detected daily limit: clear the flag
            store.x("UPDATE llm_quota SET exhausted=0 WHERE day=?", (quota_day(),))


def _act_summary(store, act: str) -> dict:
    n = store.q1("SELECT COUNT(*) n FROM nodes WHERE type='Obligation' AND json_extract(props,'$.act')=?", (act,))["n"]
    bad = store.q("SELECT errors FROM step_outputs WHERE valid=0 AND step IN ('extract_obligations','amend_diff') "
                  "AND subject LIKE ?", (f"%:{act}@%",))
    return {"obligations": n, "failed_batches": len(bad)}


def _report(path: Path, started: str, acts: dict, uploads: list, check: dict | None):
    lines = [f"# Run-all — started {started}, last update {now_iso()}", "", "## 1. Obligations of the acts", ""]
    for a, r in acts.items():
        s = r.get("summary") or {}
        lines.append(f"- **{a}**: {r['status']} (attempts {r.get('attempts', 0)}, {r.get('minutes', 0)} min)"
                     + (f" · obligations: {s.get('obligations')}" if s else "")
                     + (f" · failed batches: {s.get('failed_batches')}" if s.get("failed_batches") else ""))
        if r.get("message"):
            lines.append(f"  - last problem: {r['message']}")
    lines += ["", "## 2. Uploaded documents", ""]
    lines += [f"- {u['file']}: {u['status']} → {u.get('version_id', '')} ({u.get('direction') or '—'})"
              for u in uploads] or ["- none"]
    lines += ["", "## 3. Checks of the documents", "",
              ("see check-all-report.md: " + ", ".join(f"{k}: {v}" for k, v in check["results"].items()))
              if check else "- not started yet", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_all(cfg, store, actor: dict, *, acts: list[str] | None = None, uploads: list[tuple[str, str]] = (),
            check: bool = True, skip=(), make_llm=None, sleep=None, log=print, waits: dict | None = None,
            force_map: bool = False) -> dict:
    """``uploads`` = [(pdf path, direction)]. ``acts`` None = the obligation acts without obligations yet."""
    from kg.extract.obligations_v2 import extract_obligations_v2
    from kg.llm import LLM
    from kg.runlog import Run
    from kg.services import documents as D
    from kg.services.directions import set_direction
    from kg.services.jobs import check_all
    sleep = sleep or time.sleep
    make_llm = make_llm or (lambda c, s, rid: LLM(c, s, rid))
    w = {**WAITS, **(waits or {})}
    report = cfg.path("runs") / "run-all-report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    started = now_iso()
    acts = [str(a) for a in (acts if acts is not None else acts_without_obligations(cfg, store))]
    res_acts: dict[str, dict] = {a: {"status": "waiting"} for a in acts}
    res_up: list[dict] = []
    log(f"run-all: acts {acts or '—'}; documents {[Path(p).name for p, _ in uploads] or '—'}")

    # ---- 1. obligations, one act at a time
    for a in acts:
        res_acts[a] = {"status": "running"}
        _report(report, started, res_acts, res_up, None)

        def extract(attempt, a=a):
            with Run(store, cfg.path("runs"), "extract-obligations", {"act": a, "run_all": True}) as run:
                llm = make_llm(cfg, store, run.id)
                llm.max_live = None
                llm.tolerate_rejected = True
                log(f"[act {a}] extraction: attempt {attempt}")
                # from the 2nd attempt on, batches whose answer was not usable are asked again
                return extract_obligations_v2(cfg, store, run, llm, act_docs=[a], retry_invalid=attempt > 1)
        r = _retry(store, f"act {a}", extract, sleep=sleep, log=log, waits=w)
        r.pop("result", None)
        r["summary"] = _act_summary(store, a)
        res_acts[a] = r
        log(f"[act {a}] {r['status']} · {r['summary']}")
        _report(report, started, res_acts, res_up, None)

    # ---- 2. upload the documents
    versions = []
    for path, direction in uploads:
        p = Path(path)
        try:
            out = D.upload(cfg, store, actor, p.read_bytes(), p.name, mode="new_document")
            vid = out.get("version_id")
            if vid:
                fam = store.q1("SELECT family_id FROM document_versions WHERE id=?", (vid,))
                if direction:
                    set_direction(store, actor, fam["family_id"], direction)
                versions.append(vid)
            res_up.append({"file": p.name, "status": out.get("status"), "version_id": vid, "direction": direction})
        except Exception as exc:  # noqa: BLE001 - reported, the others go on
            res_up.append({"file": p.name, "status": f"failed: {exc}"[:200], "direction": direction})
        log(f"[upload] {res_up[-1]}")
    _report(report, started, res_acts, res_up, None)

    # ---- 3. check the documents (auto-confirmed profiles, drafts)
    out_check = None
    if check and versions:
        out_check = check_all(cfg, store, actor, versions, skip=skip, make_drafts=True, auto_confirm=True,
                              make_llm=make_llm, sleep=sleep, log=log, waits=waits, force_map=force_map)
    _report(report, started, res_acts, res_up, out_check)
    return {"report": str(report), "acts": {a: r["status"] for a, r in res_acts.items()},
            "uploads": res_up, "check": (out_check or {}).get("results")}
