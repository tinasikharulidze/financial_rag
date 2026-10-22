"""Stored LLM step outputs (docs/KG_V2_PLAN.md section 12).

``run_step`` is the ONLY way v2 code calls the LLM for a pipeline step:

    1. hash the exact inputs of the step (texts, ids, confirmed profile values, user edges ...);
    2. look for ``step_outputs(step, subject, input_hash, prompt_version)``; found -> reuse, no request at all;
    3. otherwise call the model (``LLM.call``), validate (JSON schema + the step's own checks, one retry with the
       error message), and store the validated JSON.

Everything downstream of a stored output (filters, guards, cause, ranking, tasks, diffs, UI) is plain code, so a
change of a weight or a template never needs a new request. To force a fresh answer: bump the prompt version in
the prompt file header, or pass ``force=True`` / ``retry_invalid=True``.

Prompt files start with a header line ``<!-- version: obl-v2.0 -->``; ``load_prompt`` returns (text, version).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kg.config import now_iso
from kg.llm import LLMRequestRejected, LLMValidationError, _excerpt

_VERSION = re.compile(r"^\s*<!--\s*version:\s*([\w.\-]+)\s*-->\s*\n?")


class StepRejected(RuntimeError):
    """Raised by a step validator when the whole answer is unusable (stored as invalid, not retried again)."""


@dataclass
class StepResult:
    output: Any                       # validated pydantic instance (None when invalid)
    output_id: int | None
    reused: bool
    valid: bool
    errors: list[str] = field(default_factory=list)
    model: str | None = None


def load_prompt(path: str | Path) -> tuple[str, str]:
    """-> (prompt text without the header, version string)."""
    raw = Path(path).read_text(encoding="utf-8")
    m = _VERSION.match(raw)
    return (raw[m.end():] if m else raw), (m.group(1) if m else "v0")


def input_hash(obj: Any) -> str:
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _row(store, step: str, subject: str, ihash: str, version: str) -> dict | None:
    return store.q1("SELECT * FROM step_outputs WHERE step=? AND subject=? AND input_hash=? AND prompt_version=?",
                    (step, subject, ihash, version))


def get_output(store, output_id: int | None) -> dict | None:
    if output_id is None:
        return None
    r = store.q1("SELECT * FROM step_outputs WHERE id=?", (output_id,))
    if r:
        r["output"] = json.loads(r["output"])
    return r


def _store(store, step, subject, ihash, version, model, output_json: str, valid: bool, errors: list[str],
           run_id: str | None) -> int:
    store.x("INSERT INTO step_outputs(step,subject,input_hash,prompt_version,model,output,valid,errors,run_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(step,subject,input_hash,prompt_version) DO UPDATE SET "
            "model=excluded.model, output=excluded.output, valid=excluded.valid, errors=excluded.errors, "
            "run_id=excluded.run_id, created_at=excluded.created_at",
            (step, subject, ihash, version, model, output_json, 1 if valid else 0,
             json.dumps(errors, ensure_ascii=False) if errors else None, run_id, now_iso()))
    return _row(store, step, subject, ihash, version)["id"]


def run_step(store, llm, step: str, subject: str, inputs: Any, messages: list[dict], schema, prompt_version: str,
             *, validate: Callable[[Any], list[str]] | None = None, force: bool = False,
             retry_invalid: bool = False, max_tokens: int | None = None, run=None) -> StepResult:
    """Run (or reuse) one LLM step. ``validate(parsed) -> list[str]`` may drop bad items in place and returns the
    problems it found; with problems the model gets ONE retry that shows them. Problems that remain are stored
    in ``errors`` (the cleaned output is still used). ``BudgetExhausted`` propagates to the caller."""
    ihash = input_hash(inputs)
    old = _row(store, step, subject, ihash, prompt_version)
    if old and not old["valid"] and "rejected:" in (old["errors"] or ""):
        old = None                              # a request the provider refused earlier: ask again
    if old and not force and (old["valid"] or not retry_invalid):
        if run:
            run.count(f"{step}_reused")
        if not old["valid"]:
            return StepResult(None, old["id"], True, False, json.loads(old["errors"] or "[]"), old["model"])
        try:
            parsed = schema.model_validate(json.loads(old["output"]))
            return StepResult(parsed, old["id"], True, True, json.loads(old["errors"] or "[]"), old["model"])
        except Exception:                       # schema changed since: fall through and recompute
            pass
    if run:
        run.count(f"{step}_calls")
    run_id = getattr(run, "id", None)
    try:
        res = llm.call(step, messages, schema, max_tokens=max_tokens, prompt_version=prompt_version)
    except LLMRequestRejected as exc:
        # normally fatal for the run; an unattended run (``llm.tolerate_rejected``) skips this one batch instead,
        # stores why, and goes on (the next run asks again)
        if not getattr(llm, "tolerate_rejected", False):
            raise
        oid = _store(store, step, subject, ihash, prompt_version, None, "{}", False, [f"rejected: {exc}"[:500]],
                     run_id)
        if run:
            run.log("step_rejected", step=step, subject=subject, error=str(exc)[:300])
        return StepResult(None, oid, False, False, [f"rejected: {exc}"[:500]])
    except LLMValidationError as exc:
        oid = _store(store, step, subject, ihash, prompt_version, None, "{}", False, [f"invalid_json: {exc}"[:500]],
                     run_id)
        if run:
            run.log("step_invalid", step=step, subject=subject, error=str(exc)[:300])
        return StepResult(None, oid, False, False, [str(exc)[:500]])
    parsed, model = res.parsed, res.model
    errors: list[str] = validate(parsed) if validate else []
    hard = [e for e in errors if not e.startswith("note:")]        # "note:" problems are logged, never retried
    if hard:
        feedback = messages + [
            {"role": "assistant", "content": _excerpt(res.text, 1500, 300)},
            {"role": "user", "content": "The answer has these problems:\n- " + "\n- ".join(e[:300] for e in hard[:20])
                                        + "\nReturn the COMPLETE corrected JSON only (same format)."}]
        try:
            res2 = llm.call(step, feedback, schema, max_tokens=max_tokens, prompt_version=prompt_version)
            parsed2 = res2.parsed
            errors2 = validate(parsed2) if validate else []
            parsed, model, errors = parsed2, res2.model, errors2
        except LLMValidationError:
            pass                                 # keep the first (already cleaned) answer and its errors
        except LLMRequestRejected as exc:        # e.g. the retry (answer + problems) is too big for tokens/minute
            if run:
                run.log("retry_skipped", step=step, subject=subject, reason=str(exc)[:300])
    try:
        out_json = parsed.model_dump_json()
    except Exception:  # noqa: BLE001
        out_json = json.dumps(parsed, ensure_ascii=False, default=str)
    oid = _store(store, step, subject, ihash, prompt_version, model, out_json, True, errors, run_id)
    if run and errors:
        run.log("step_problems", step=step, subject=subject, problems=errors[:10])
    if run:
        run.log("step_done", step=step, subject=subject, model=model, problems=len(errors))
    return StepResult(parsed, oid, False, True, errors, model)


def reuse_stats(store) -> dict:
    """Per step: stored outputs, invalid ones (for the ops evaluation)."""
    rows = store.q("SELECT step, COUNT(*) n, SUM(1-valid) bad FROM step_outputs GROUP BY step")
    return {r["step"]: {"outputs": r["n"], "invalid": r["bad"] or 0} for r in rows}
