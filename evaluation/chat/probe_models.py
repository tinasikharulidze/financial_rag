"""Find out what a model accepts and how fast it answers, with a handful of tiny requests (each one counts against
the model's daily quota, so the default is 3 requests per model).

    uv run evaluation/chat/probe_models.py                                  # gemini/gemini-3.8-flash
    uv run evaluation/chat/probe_models.py --models gemini/gemini-3.6-flash,gemini/gemini-3.0-flash
    uv run evaluation/chat/probe_models.py --levels none,low,high --realistic   # also with the chat's real prompt size

For every model and thinking level it prints: ok / the provider's own error message, seconds, whether the answer was
valid JSON. The API key is read the way the app reads it (FINRAG_ENV_FILE); this script never prints it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from kg.llm import extract_json, litellm_backend, load_env  # noqa: E402

PING = [{"role": "user", "content": 'Reply with one JSON object only: {"action":"answer","answer":"ok","citations":[]}'}]


def realistic_prompt() -> list[dict]:
    from kg.agent.loop import SYSTEM

    tools = "\n".join(f"- tool_{i}(arg1, arg2?)   -> what this tool does, in one line of about a hundred characters" for i in range(27))
    system = SYSTEM.format(tools=tools, language="Georgian", acts="  4964423 = Model risk (No 1/04)", today="2026-10-24", steps=5)
    return [{"role": "system", "content": system},
            {"role": "user", "content": 'USER QUESTION: რას ეხება მოდელის რისკის მართვის დებულების მე-9 მუხლი?'}]


def probe(model: str, level: str | None, messages: list[dict], timeout: float) -> str:
    t0 = time.time()
    try:
        res = litellm_backend(model, messages, 2048, 0, True, None, level, timeout)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).replace("\n", " ")
        return f"ERROR after {time.time() - t0:.0f}s: {type(exc).__name__}: {msg[:220]}"
    try:
        extract_json(res["text"])
        valid = "valid JSON"
    except ValueError:
        valid = f"NOT JSON: {res['text'][:80]!r}"
    return f"ok in {time.time() - t0:.1f}s, {res.get('output_tokens')} tokens out, {valid}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="gemini/gemini-3.8-flash")
    ap.add_argument("--levels", default="none,minimal,low", help="thinking levels to try (none = not sent)")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--realistic", action="store_true", help="also send a prompt of the chat's real size once per model")
    args = ap.parse_args(argv)
    load_env()
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"\n{model}")
        for lv in [x.strip() for x in args.levels.split(",") if x.strip()]:
            print(f"  thinking {lv:8s} tiny prompt      -> {probe(model, None if lv == 'none' else lv, PING, args.timeout)}", flush=True)
        if args.realistic:
            lv = [x.strip() for x in args.levels.split(",") if x.strip()][-1]
            print(f"  thinking {lv:8s} real-size prompt -> {probe(model, None if lv == 'none' else lv, realistic_prompt(), args.timeout)}", flush=True)


if __name__ == "__main__":
    main()
