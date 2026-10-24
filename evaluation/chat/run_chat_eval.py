#!/usr/bin/env python3
"""Evaluate the Goni chat assistant on scripted conversations (evaluation/chat/chat_cases.csv).

Every conversation is 2-3 user messages sent to the real agent (``kg.agent.run_turn``) against a COPY of your database,
so confirmed writes never touch your data. Each reply is checked automatically (tools used, ids cited, proposals made,
words that must / must not appear in a proposed wording, answer language, ...). No second model grades anything, so
the only requests that count against your quota are the chatbot's own.

Typical use (from the repository root, in a terminal you can leave alone):

    python evaluation/chat/run_chat_eval.py --models flash        # the Flash models of your agent_step stack
    python evaluation/chat/run_chat_eval.py --models gemma        # later: the Gemma models (same conversations, own results)
    python evaluation/chat/run_chat_eval.py --models flash --dry-run     # only show the plan

Heaviest conversations (redrafting, then challenging a flag, applicability, exploration) run first. After every
conversation the result is saved; when the Flash quota is used up the script stops by itself. Run the same command
again later and it continues with the conversations that are not finished yet. Failed conversations are kept (use
``--redo-failed`` to run them again, ``--force`` to start over).

Results (evaluation/chat/results/<tag>/): results.jsonl (everything), summary.csv (one row per conversation) and
transcripts.md (the conversations with a tick or a cross per check, easy to read).

The API key is read the same way the app reads it (FINRAG_ENV_FILE / ../.env); this script never prints or stores it.

CSV format: one row per conversation. Columns: id, use_case, token_tier (1 = most tokens, runs first), lang (the interface
language, ka or en), document, context (the JSON the chat panel would send), t1_message / t1_expect, t2_..., t3_...,
notes. ``tN_expect`` is a JSON object; a key with the prefix ``soft_`` only warns when it fails. Keys:

    kind / kind_in              "answer" | "ask"  (or a list)
    tools_any / tools_all / tools_none   tools called in this turn
    cites_any / cites_all       ids (prefixes) among the citations of the reply
    cites_min                   at least N grounded citations
    no_dropped                  the grounding check rejected nothing
    actions                     the confirmation cards created in this turn (exact set of kinds; [] = none)
    actions_any / actions_not / actions_none
    text_any / text_all / text_none     words in the answer text (case-insensitive)
    proposal_any / proposal_all / proposal_none     words in the proposed wording of the cards of this turn
    cursor_any                  prefixes the graph cursor is at / passed through
    confirm + task_status       click "confirm" on the first card, then check the flag's status in the database (a status or a list)
    either                      [expect, expect]: passes if one of them passes
Always checked: the reply is an answer or a question (not an error), it is in the interface language, it shows no raw ids
or tool names, and an answer that used tools has citations (warning only).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import time
import traceback
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = next(
    (p for p in (HERE, *HERE.parents) if (p / "src" / "kg").is_dir()), HERE.parents[1]
)
sys.path.insert(0, str(REPO / "src"))

from kg.config import Config, load_config

GEORGIAN = re.compile(r"[Ⴀ-ჿ]")
RAW_ID = re.compile(r"\b(?:obl|pv|prov|cl|amend|doc|ext):[\w/@.#+Ⴀ-ჿ-]{3,}")
STOP_AFTER_INFRA_ERRORS = 3


# ------------------------------------------------------------------------------------------------------ setup
def resolve_models(cfg, spec: str) -> tuple[str, list[str]]:
    stack = list(
        cfg.get_in("llm.step_models.agent_step") or cfg.get_in("llm.models") or []
    )
    if spec in ("flash", "gemma", "all"):
        pick = {
            "flash": [m for m in stack if "gemma" not in m.lower()],
            "gemma": [m for m in stack if "gemma" in m.lower()],
            "all": stack,
        }[spec]
        tag = spec
    else:
        pick = [m.strip() for m in spec.split(",") if m.strip()]
        tag = "custom-" + hashlib.sha1(",".join(pick).encode()).hexdigest()[:6]
    if not pick:
        sys.exit(f"No models for '{spec}'. agent_step in configs/kg.yaml is: {stack}")
    return tag, pick


def prepare_config(
    cfg,
    models: list[str],
    out_dir: Path,
    turn_timeout: float | None,
    request_timeout: float | None,
) -> Config:
    c = Config(deepcopy(dict(cfg)))
    if (
        request_timeout
    ):  # slow thinking models need more than the chat's 40 s per request
        c["llm"].setdefault("step_options", {}).setdefault("agent_step", {})[
            "timeout_s"
        ] = request_timeout
    c["llm"].setdefault("step_models", {})["agent_step"] = models
    c["llm"]["single_model"] = None
    c["paths"] = {
        **c["paths"],
        "runs": str(out_dir / "runs"),
        "llm_cache": str(out_dir / "llm_cache"),
    }
    if turn_timeout:
        c.setdefault("agent", {})["turn_timeout_s"] = turn_timeout
    return c


def backup(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dst)
    with d:
        s.backup(d)
    s.close()
    d.close()


def sync_quota(src: Path, dst: Path):
    """Copy the request counters (and 'used up today' marks) of the conversation's database back to the master copy."""
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        cols = [r[1] for r in s.execute("PRAGMA table_info(llm_quota)")]
        rows = s.execute(f"SELECT {','.join(cols)} FROM llm_quota").fetchall()
        with d:
            d.execute("DELETE FROM llm_quota")
            d.executemany(
                f"INSERT INTO llm_quota({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                rows,
            )
    finally:
        s.close()
        d.close()


# Options that win over agent_step's step_options for one model (substring of the model id). Gemini 3.8 Flash refuses
# thinking_level "minimal"; change or add entries here (or with --model-option) when another model complains.
MODEL_OPTIONS: dict[str, dict] = {"gemini-3.8-flash": {"thinking_level": "low"}}


def make_llm(cfg, store, run_id, cache_dir):
    """Overridable (the script's own test replaces it with a scripted fake model)."""
    from kg.llm import LLM

    class EvalLLM(LLM):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            # after a timeout the library re-sends the request with thinking "minimal", which Gemini 3.8 Flash rejects;
            # here a slow model simply fails over to the next one
            self.calm_on_timeout = False
            plain_sleep = self._sleep

            def sleep(
                seconds,
            ):  # say it when the library waits (rate-limit pacing, retries)
                if seconds >= 3:
                    print(
                        f"      … waiting {seconds:.0f}s (rate limit / retry)",
                        flush=True,
                    )
                plain_sleep(seconds)

            self._sleep = sleep

        def _request(self, *a, **kw):
            t0 = time.time()
            print("      → asking the model…", flush=True)
            res = super()._request(*a, **kw)
            print(
                f"      ← {res[0].split('/')[-1]} answered in {time.time() - t0:.0f}s",
                flush=True,
            )
            return res

        def options_for(self, model, purpose=None):
            opts = super().options_for(model, purpose)
            for pattern, o in MODEL_OPTIONS.items():
                if pattern in model:
                    opts.update(o)
            return opts

    return EvalLLM(cfg, store, run_id, cache_dir=cache_dir)


def load_cases(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            turns = []
            for i in (1, 2, 3):
                msg = (r.get(f"t{i}_message") or "").strip()
                if msg:
                    turns.append(
                        {
                            "message": msg,
                            "expect": json.loads(r.get(f"t{i}_expect") or "{}"),
                        }
                    )
            rows.append(
                {
                    **r,
                    "token_tier": int(r.get("token_tier") or 9),
                    "context": json.loads(r["context"] or "{}"),
                    "turns": turns,
                }
            )
    rows.sort(key=lambda r: r["token_tier"])  # stable: the CSV order inside a tier
    return rows


# ------------------------------------------------------------------------------------------------------ checks
def _has(text: str, words) -> bool:
    t = (text or "").casefold()
    return any(str(w).casefold() in t for w in words)


def _lang_ok(text: str, lang: str) -> tuple[bool, str]:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 25:
        return True, "too short to tell"
    geo = sum(1 for c in letters if GEORGIAN.match(c)) / len(letters)
    ok = geo >= 0.5 if lang == "ka" else geo <= 0.35
    return ok, f"{geo:.0%} Georgian letters"


def run_checks(exp: dict, turn: dict, lang: str, check_lang: bool = True) -> list[dict]:
    """turn: kind, text, tools, cites (ids), dropped, actions (rows), proposals (texts), cursor, grounded."""
    out: list[dict] = []

    def add(name, ok, detail="", soft=False):
        out.append(
            {
                "check": name,
                "status": "pass" if ok else ("warn" if soft else "fail"),
                "detail": detail,
            }
        )

    if check_lang:
        add("replied", turn["kind"] in ("answer", "ask"), f"kind={turn['kind']}")
        ok, d = _lang_ok(turn["text"], lang)
        add("language", ok, f"{lang}: {d}")
        raw = RAW_ID.findall(turn["text"]) + [
            t
            for t in set(turn["tools"])
            if re.search(rf"\b{re.escape(t)}\b", turn["text"])
        ]
        add("no_ids_or_tool_names_in_text", not raw, ", ".join(raw[:3]))
        if turn["kind"] == "answer" and turn["tools"]:
            add("answer_has_citations", bool(turn["cites"]), "no citations", soft=True)

    for key, val in exp.items():
        soft = key.startswith("soft_")
        k = key[5:] if soft else key
        tools, cites, kinds = (
            turn["tools"],
            turn["cites"],
            [a["kind"] for a in turn["actions"]],
        )
        if k == "kind":
            add(key, turn["kind"] == val, f"{turn['kind']} (wanted {val})", soft)
        elif k == "kind_in":
            add(key, turn["kind"] in val, f"{turn['kind']} (wanted one of {val})", soft)
        elif k == "tools_any":
            add(
                key,
                bool(set(val) & set(tools)),
                f"called {sorted(set(tools)) or 'nothing'}",
                soft,
            )
        elif k == "tools_all":
            add(
                key,
                set(val) <= set(tools),
                f"called {sorted(set(tools)) or 'nothing'}",
                soft,
            )
        elif k == "tools_none":
            add(
                key,
                not set(val) & set(tools),
                f"called {sorted(set(tools) & set(val))}",
                soft,
            )
        elif k == "cites_any":
            add(
                key,
                any(c.startswith(p) for c in cites for p in val),
                f"cited {cites[:6] or 'nothing'}",
                soft,
            )
        elif k == "cites_all":
            add(
                key,
                all(any(c.startswith(p) for c in cites) for p in val),
                f"cited {cites[:6] or 'nothing'}",
                soft,
            )
        elif k == "cites_min":
            add(key, len(cites) >= val, f"{len(cites)} citation(s)", soft)
        elif k == "no_dropped":
            add(
                key,
                not turn["dropped"] if val else True,
                f"{len(turn['dropped'])} rejected by the grounding check",
                soft,
            )
        elif k == "actions":
            add(
                key,
                sorted(kinds) == sorted(val),
                f"cards: {kinds or 'none'} (wanted {val or 'none'})",
                soft,
            )
        elif k == "actions_any":
            add(key, bool(set(val) & set(kinds)), f"cards: {kinds or 'none'}", soft)
        elif k == "actions_not":
            add(key, not set(val) & set(kinds), f"cards: {kinds or 'none'}", soft)
        elif k == "actions_none":
            add(key, not kinds if val else True, f"cards: {kinds or 'none'}", soft)
        elif k == "text_any":
            add(key, _has(turn["text"], val), f"looked for {val}", soft)
        elif k == "text_all":
            add(
                key,
                all(_has(turn["text"], [w]) for w in val),
                f"looked for {val}",
                soft,
            )
        elif k == "text_none":
            add(key, not _has(turn["text"], val), f"found one of {val}", soft)
        elif k in ("proposal_any", "proposal_all", "proposal_none"):
            props = turn["proposals"]
            if not props:
                add(key, k == "proposal_none", "no proposed wording in this turn", soft)
            elif k == "proposal_any":
                add(key, any(_has(p, val) for p in props), f"looked for {val}", soft)
            elif k == "proposal_all":
                add(
                    key,
                    all(all(_has(p, [w]) for w in val) for p in props),
                    f"looked for all of {val}",
                    soft,
                )
            else:
                add(
                    key,
                    not any(_has(p, val) for p in props),
                    "a forbidden word is in the proposal",
                    soft,
                )
        elif k == "cursor_any":
            at = [turn["cursor"].get("current"), *turn["cursor"].get("trail", [])]
            add(
                key,
                any(str(x or "").startswith(p) for x in at for p in val),
                f"cursor {turn['cursor'].get('current')}",
                soft,
            )
        elif k == "either":
            subs = [
                [c for c in run_checks(e, turn, lang, False) if c["status"] == "fail"]
                for e in val
            ]
            add(
                key,
                any(not s for s in subs),
                "none of the alternatives held: "
                + " | ".join(", ".join(c["check"] for c in s) for s in subs),
                soft,
            )
        elif k in ("confirm", "task_status"):
            pass  # handled by the runner
        else:
            add(key, False, "unknown check in the CSV", soft)
    return out


# ------------------------------------------------------------------------------------------------------ one conversation
def run_case(case: dict, cfg, master: Path, work: Path, out_dir: Path, args) -> dict:
    from kg.agent import Cursor, run_turn
    from kg.agent import actions as A
    from kg.llm import BudgetExhausted, LLMRequestRejected, LLMUnavailable
    from kg.runlog import Run
    from kg.services.accounts import get_user
    from kg.store.sqlite_nx import SQLiteStore

    conv_db = work / f"{case['id']}.sqlite"
    backup(master, conv_db)
    store = SQLiteStore(conv_db)
    user = get_user(store, int(args.user_id)) if args.user_id else None
    if not user:
        row = store.q1(
            "SELECT id FROM users WHERE user_type='entity' ORDER BY id LIMIT 1"
        )
        user = get_user(store, row["id"])
    history: list[dict] = []
    cursor: dict = {}
    result = {
        "id": case["id"],
        "use_case": case["use_case"],
        "lang": case["lang"],
        "document": case["document"],
        "notes": case["notes"],
        "turns": [],
        "status": "pass",
    }
    t_case = time.time()
    run_ids: list[str] = []
    try:
        for n, spec in enumerate(case["turns"], 1):
            t0 = time.time()
            with Run(
                store, cfg.path("runs"), "agent", {"eval": case["id"], "turn": n}
            ) as run:
                run_ids.append(run.id)
                llm = make_llm(cfg, store, run.id, cfg.path("llm_cache"))
                reply = run_turn(
                    cfg,
                    store,
                    llm,
                    user,
                    case["context"],
                    spec["message"],
                    list(history),
                    Cursor.from_dict(cursor),
                    lang=case["lang"],
                )
            rows = [r for r in (A.view(store, user, a) for a in reply.actions) if r]
            used = store.q(
                "SELECT model, COUNT(*) n, SUM(cache_hit) hits, SUM(COALESCE(input_tokens,0)) tin, "
                "SUM(COALESCE(output_tokens,0)) tout, SUM(error IS NOT NULL) errs FROM llm_calls "
                "WHERE run_id=? GROUP BY model",
                (run.id,),
            )
            turn = {
                "kind": reply.kind,
                "text": reply.text,
                "tools": [t["tool"] for t in reply.trace],
                "cites": [c["id"] for c in reply.citations],
                "dropped": reply.dropped,
                "actions": [
                    {
                        "id": r["id"],
                        "kind": r["kind"],
                        "status": r["status"],
                        "params": r["params"],
                        "reason": r.get("reason"),
                    }
                    for r in rows
                ],
                "proposals": [
                    str((r["params"] or {}).get("text") or "")
                    for r in rows
                    if r["kind"] == "suggest_wording"
                ],
                "cursor": reply.cursor,
            }
            checks = run_checks(spec["expect"], turn, case["lang"])
            if spec["expect"].get("confirm") and rows:
                try:
                    done = A.confirm(cfg, store, user, rows[0]["id"])
                    checks.append(
                        {
                            "check": "confirm_card",
                            "status": "pass",
                            "detail": f"status {done['status']}",
                        }
                    )
                    want = spec["expect"].get("task_status")
                    want = [want] if isinstance(want, str) else want
                    if want:
                        t = store.q1(
                            "SELECT status FROM tasks WHERE doc_version_id=? AND obligation_id=?",
                            (
                                case["context"].get("version_id"),
                                case["context"].get("obligation_id"),
                            ),
                        )
                        checks.append(
                            {
                                "check": "flag_status_after_confirm",
                                "status": "pass"
                                if t and t["status"] in want
                                else "fail",
                                "detail": f"{t and t['status']} (wanted {want})",
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    checks.append(
                        {
                            "check": "confirm_card",
                            "status": "fail",
                            "detail": f"{type(exc).__name__}: {exc}"[:200],
                        }
                    )
            elif spec["expect"].get("confirm"):
                checks.append(
                    {
                        "check": "confirm_card",
                        "status": "fail",
                        "detail": "there was no card to confirm",
                    }
                )
            result["turns"].append(
                {
                    "message": spec["message"],
                    "kind": reply.kind,
                    "text": reply.text,
                    "tools": turn["tools"],
                    "cites": turn["cites"],
                    "options": reply.options,
                    "steps": reply.steps,
                    "tool_errors": [
                        {"tool": t["tool"], "error": t["error"]}
                        for t in reply.trace
                        if t.get("error")
                    ],
                    "seconds": round(time.time() - t0, 1),
                    "cards": [
                        {
                            "kind": a["kind"],
                            "status": a["status"],
                            "text": (a["params"] or {}).get("text"),
                            "heading": (a["params"] or {}).get("heading"),
                            "fact": (a["params"] or {}).get("fact"),
                            "reason": a["reason"],
                        }
                        for a in turn["actions"]
                    ],
                    "models": [
                        {
                            "model": u["model"],
                            "calls": u["n"],
                            "cached": u["hits"] or 0,
                            "tokens_in": u["tin"],
                            "tokens_out": u["tout"],
                            "errors": u["errs"] or 0,
                        }
                        for u in used
                    ],
                    "checks": checks,
                }
            )
            history += [
                {"role": "user", "content": spec["message"]},
                {"role": "assistant", "content": reply.text},
            ]
            cursor = reply.cursor
            used_txt = (
                ", ".join("%s×%s" % (u["model"].split("/")[-1], u["n"]) for u in used)
                or "no calls"
            )
            print(
                f"    turn {n}: {reply.kind:6s} {sum(c['status'] == 'fail' for c in checks)} failed, "
                f"{sum(c['status'] == 'warn' for c in checks)} warnings  [{used_txt}]  {time.time() - t0:.0f}s",
                flush=True,
            )
    except BudgetExhausted:
        result["status"] = "quota"
    except (LLMUnavailable, LLMRequestRejected) as exc:
        result["status"] = "infra"
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
    except Exception as exc:  # noqa: BLE001 - a bug in the chat is a result too, not a reason to stop the whole run
        result["status"] = "crash"
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        result["traceback"] = traceback.format_exc()[-1500:]
    finally:
        if (
            result["status"] in ("infra", "quota", "crash") and run_ids
        ):  # keep the reason before the copy is deleted
            try:
                result["llm_errors"] = [
                    {
                        "model": r["model"],
                        "error": (r["error"] or "")[:300],
                        "seconds": round((r["latency_ms"] or 0) / 1000, 1),
                    }
                    for r in store.q(
                        "SELECT model, error, latency_ms FROM llm_calls WHERE run_id=? ORDER BY id",
                        (run_ids[-1],),
                    )
                ]
            except Exception:  # noqa: BLE001
                pass
        try:
            sync_quota(conv_db, master)
        except Exception:  # noqa: BLE001
            pass
        for p in (conv_db, Path(str(conv_db) + "-wal"), Path(str(conv_db) + "-shm")):
            p.unlink(missing_ok=True)
    result["seconds"] = round(time.time() - t_case, 1)
    if result["status"] == "pass":
        if any(c["status"] == "fail" for t in result["turns"] for c in t["checks"]):
            result["status"] = "fail"
    return result


# ------------------------------------------------------------------------------------------------------ reports
def read_results(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                r = json.loads(ln)
                out[r["id"]] = r  # the latest line of a conversation wins
    return out


def write_reports(
    out_dir: Path,
    cases: list[dict],
    results: dict[str, dict],
    tag: str,
    models: list[str],
):
    rows = []
    for c in cases:
        r = results.get(c["id"])
        if not r:
            continue
        fails = [
            f"t{i}:{k['check']}"
            for i, t in enumerate(r["turns"], 1)
            for k in t["checks"]
            if k["status"] == "fail"
        ]
        warns = [
            f"t{i}:{k['check']}"
            for i, t in enumerate(r["turns"], 1)
            for k in t["checks"]
            if k["status"] == "warn"
        ]
        used = Counter()
        for t in r["turns"]:
            for m in t["models"]:
                used[m["model"]] += m["calls"] - m["cached"]
        rows.append(
            {
                "id": r["id"],
                "use_case": r["use_case"],
                "status": r["status"],
                "turns_done": len(r["turns"]),
                "failed_checks": "; ".join(fails),
                "warnings": "; ".join(warns),
                "models": "; ".join(f"{m}×{n}" for m, n in used.items()),
                "seconds": r.get("seconds"),
                "error": r.get("error", ""),
            }
        )
    with open(out_dir / "summary.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(
            fh,
            [
                "id",
                "use_case",
                "status",
                "turns_done",
                "failed_checks",
                "warnings",
                "models",
                "seconds",
                "error",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    md = [f"# Goni chat evaluation — {tag}", "", f"Models: {', '.join(models)}", ""]
    by = defaultdict(lambda: Counter())
    for r in rows:
        by[r["use_case"]][r["status"]] += 1
    md += ["| use case | pass | fail | not finished |", "|---|---|---|---|"]
    for uc, cnt in by.items():
        md.append(
            f"| {uc} | {cnt['pass']} | {cnt['fail']} | {sum(v for k, v in cnt.items() if k not in ('pass', 'fail'))} |"
        )
    md.append("")
    for c in cases:
        r = results.get(c["id"])
        if not r:
            continue
        md += [
            f"## {r['id']} — {r['use_case']} — {r['status'].upper()}",
            "",
            f"*{r['notes']}*",
            "",
        ]
        for i, t in enumerate(r["turns"], 1):
            md += [
                f"**User {i}:** {t['message']}",
                "",
                f"**Goni ({t['kind']}):** {t['text']}",
                "",
            ]
            for card in t["cards"]:
                body = card.get("text") or card.get("fact") or card.get("reason") or ""
                md.append(f"> card `{card['kind']}`: {body}")
            if t["cards"]:
                md.append("")
            md.append(
                "tools: %s · cited: %d · %s"
                % (
                    ", ".join(t["tools"]) or "—",
                    len(t["cites"]),
                    ", ".join("%s×%s" % (m["model"], m["calls"]) for m in t["models"]),
                )
            )
            for e in t.get("tool_errors", []):
                md.append(f"tool error in `{e['tool']}`: {e['error']}")
            md.append("")
            for k in t["checks"]:
                if k["status"] != "pass" or True:
                    md.append(
                        f"- {icon[k['status']]} {k['check']}"
                        + (
                            f" — {k['detail']}"
                            if k["status"] != "pass" and k["detail"]
                            else ""
                        )
                    )
            md.append("")
        if r.get("error"):
            md += [f"**Stopped:** {r['error']}", ""]
    (out_dir / "transcripts.md").write_text("\n".join(md), encoding="utf-8")
    return rows


# ------------------------------------------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--models",
        default="flash",
        help="flash | gemma | all | comma-separated LiteLLM ids (default: flash)",
    )
    ap.add_argument(
        "--exclude",
        help="leave out models whose id contains one of these (comma-separated), e.g. 3.8-flash",
    )
    ap.add_argument("--cases", default=str(HERE / "chat_cases.csv"))
    ap.add_argument(
        "--only",
        help="comma-separated id prefixes or use cases, e.g. RD,CH or redrafting",
    )
    ap.add_argument("--ids", help="comma-separated conversation ids, e.g. RD-01,AP-03")
    ap.add_argument("--limit", type=int, help="run at most N conversations this time")
    ap.add_argument(
        "--tag", help="name of the results folder (default: the --models value)"
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="forget the cached model answers first (a re-run then asks the models again)",
    )
    ap.add_argument(
        "--redo-failed",
        action="store_true",
        help="run conversations that failed before again",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="forget earlier results of this tag and start over",
    )
    ap.add_argument(
        "--refresh-db",
        action="store_true",
        help="take a fresh copy of the database (default: reuse this tag's copy)",
    )
    ap.add_argument(
        "--turn-timeout",
        type=float,
        default=240,
        help="seconds per question (default 240; the app uses 75)",
    )
    ap.add_argument(
        "--request-timeout",
        type=float,
        default=90,
        help="seconds per model request (default 90; the app uses 40)",
    )
    ap.add_argument(
        "--user-id", help="user id to chat as (default: the first company user)"
    )
    ap.add_argument(
        "--model-option",
        action="append",
        default=[],
        metavar="MODEL=KEY:VALUE",
        help="override one option for a model, e.g. gemini-3.6-flash=thinking_level:low (repeatable)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    for item in args.model_option:
        pat, _, kv = item.partition("=")
        key, _, val = kv.partition(":")
        MODEL_OPTIONS.setdefault(pat.strip(), {})[key.strip()] = (
            None if val in ("", "null", "none") else val
        )
    cfg0 = load_config()
    tag, models = resolve_models(cfg0, args.models)
    if args.exclude:
        models = [
            m
            for m in models
            if not any(x.strip() in m for x in args.exclude.split(","))
        ]
        if not models:
            sys.exit("--exclude removed every model")
    tag = args.tag or tag
    out_dir = HERE / "results" / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = prepare_config(cfg0, models, out_dir, args.turn_timeout, args.request_timeout)
    cases = load_cases(Path(args.cases))
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    if args.only:
        pre = [x.strip().lower() for x in args.only.split(",")]
        cases = [
            c
            for c in cases
            if any(
                c["id"].lower().startswith(p) or c["use_case"].lower().startswith(p)
                for p in pre
            )
        ]

    res_path = out_dir / "results.jsonl"
    if args.no_cache:
        shutil.rmtree(out_dir / "llm_cache", ignore_errors=True)
    if args.force:  # start over: forget the results and the cached model answers
        res_path.unlink(missing_ok=True)
        shutil.rmtree(out_dir / "llm_cache", ignore_errors=True)
    results = read_results(res_path)
    todo = [
        c
        for c in cases
        if c["id"] not in results
        or results[c["id"]]["status"]
        in (
            ("quota", "infra", "crash", "fail")
            if args.redo_failed
            else ("quota", "infra", "crash")
        )
    ]
    if args.limit:
        todo = todo[: args.limit]

    n_turns = sum(len(c["turns"]) for c in todo)
    print(f"Tag: {tag}\nModels (in this order): {', '.join(models)}")
    print(
        f"{len(cases)} conversations selected, {len(todo)} to run ({n_turns} questions, roughly {n_turns * 3}-{n_turns * 4} model requests)."
    )
    for c in todo:
        print(
            f"  tier {c['token_tier']}  {c['id']:6s} {c['use_case']:15s} {len(c['turns'])} questions"
        )
    if args.dry_run or not todo:
        write_reports(out_dir, cases, results, tag, models)
        return 0

    db = cfg0.path("db")
    master = out_dir / "work" / "master.sqlite"
    if args.refresh_db or not master.exists():
        print(
            f"Copying the database ({db.stat().st_size / 1e6:.0f} MB) to a private working copy; your data is never written to."
        )
        backup(db, master)

    stopped = None
    infra_in_row = 0
    t_start = time.time()
    try:
        for i, case in enumerate(todo, 1):
            print(
                f"[{i}/{len(todo)}] {case['id']} ({case['use_case']}, {case['lang']})",
                flush=True,
            )
            r = run_case(case, cfg, master, out_dir / "work", out_dir, args)
            if r["status"] in ("pass", "fail"):
                infra_in_row = 0
            print(
                f"  -> {r['status'].upper()}  ({r['seconds']:.0f}s)"
                + (f"  {r.get('error', '')}" if r.get("error") else ""),
                flush=True,
            )
            for e in r.get("llm_errors", []):
                print(
                    f"       {e['model']}: {e['error'] or 'ok'} ({e['seconds']}s)",
                    flush=True,
                )
            if r["status"] == "crash":
                print(r.get("traceback", ""), flush=True)
            with open(res_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            results[case["id"]] = r
            write_reports(out_dir, cases, results, tag, models)
            if r["status"] == "quota":
                stopped = (
                    "All models in this list are used up for today (daily quota). Everything finished is saved; "
                    "run the same command later to continue."
                )
                break
            if r["status"] == "infra":
                infra_in_row += 1
                if infra_in_row >= STOP_AFTER_INFRA_ERRORS:
                    stopped = (
                        f"The model service failed {STOP_AFTER_INFRA_ERRORS} conversations in a row "
                        f"({r.get('error', '')}). Run the same command again later."
                    )
                    break
    except KeyboardInterrupt:
        stopped = "Interrupted. Finished conversations are saved; run the same command to continue."
    finally:
        rows = write_reports(out_dir, cases, results, tag, models)

    print("\n" + "=" * 70)
    cnt = Counter(r["status"] for r in rows)
    print(
        f"{cnt['pass']} passed, {cnt['fail']} failed, {sum(v for k, v in cnt.items() if k not in ('pass', 'fail'))} "
        f"not finished   ({(time.time() - t_start) / 60:.1f} min)"
    )
    for r in rows:
        if r["status"] != "pass":
            print(f"  {r['id']:6s} {r['status']:6s} {r['failed_checks'] or r['error']}")
    if stopped:
        print("\n" + stopped)
    print(
        f"\nResults: {out_dir / 'summary.csv'}\nReadable conversations: {out_dir / 'transcripts.md'}"
    )
    return 0 if not stopped else 2


if __name__ == "__main__":
    sys.exit(main())
