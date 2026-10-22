"""Diagnostic probe for Gemma 4 on the Gemini API (does NOT change the pipeline or the database).

    uv run python scripts/probe_gemma.py                 # all variants, ~6 requests
    uv run python scripts/probe_gemma.py --only A B      # some variants
    uv run python scripts/probe_gemma.py --model gemma-4-26b-a4b-it --repeat 2

Each variant sends the same small Georgian JSON task and prints: ok/error, latency, tokens, whether the visible
text is pure JSON, and the first characters of the answer. The API key is loaded from the .env named in
FINRAG_ENV_FILE (default ../.env) and is never printed.

Variants
  A  LiteLLM, no thinking parameter (what the pipeline does now)
  B  LiteLLM, thinking_level="minimal" passed as a parameter
  C  LiteLLM, extra_body with generationConfig.thinkingConfig.thinkingLevel="minimal"
  D  LiteLLM, reasoning_effort="minimal"     (expected to fail: LiteLLM turns it into a thinking budget)
  E  direct REST call, thinkingConfig.thinkingLevel="minimal"   (what the API itself accepts)
  F  direct REST call, thinkingConfig.thinkingLevel="high"
  G  LiteLLM, extra_body thinkingLevel="high"   (what the pipeline sends for triage / judge)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.llm import extract_json, load_env  # noqa: E402

MODEL = "gemma-4-31b-it"                            # changed by --model
SYSTEM = "You are a compliance analyst. Answer ONLY with a JSON object, no explanation."
USER = ("ტექსტი: „ბანკი ვალდებულია მოდელის ვალიდაცია ჩაატაროს არანაკლებ წელიწადში ერთხელ.“\n"
        "Return JSON: {\"deontic\": \"must|must_not|may\", \"subject\": \"...\", \"frequency\": \"...\", "
        "\"quote\": \"<exact words copied from the text>\"}")
MESSAGES = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]


def _key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")


def via_litellm(**extra) -> dict:
    import litellm
    litellm.drop_params = False                      # we WANT to see if a parameter is rejected
    r = litellm.completion(model=f"gemini/{MODEL}", messages=MESSAGES, max_tokens=2048, temperature=0,
                           response_format={"type": "json_object"}, **extra)
    u = getattr(r, "usage", None)
    msg = r.choices[0].message
    return {"text": msg.content or "", "reasoning_chars": len(getattr(msg, "reasoning_content", None) or ""),
            "in": getattr(u, "prompt_tokens", None), "out": getattr(u, "completion_tokens", None),
            "finish": r.choices[0].finish_reason}


def via_rest(level: str) -> dict:
    import httpx
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": USER}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 2048, "responseMimeType": "application/json",
                                 "thinkingConfig": {"thinkingLevel": level}}}
    r = httpx.post(url, json=body, headers={"x-goog-api-key": _key() or ""}, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    d = r.json()
    parts = d["candidates"][0]["content"].get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    thought = "".join(p.get("text", "") for p in parts if p.get("thought"))
    um = d.get("usageMetadata", {})
    return {"text": text, "reasoning_chars": len(thought), "in": um.get("promptTokenCount"),
            "out": um.get("candidatesTokenCount"), "thoughts_tokens": um.get("thoughtsTokenCount"),
            "finish": d["candidates"][0].get("finishReason")}


VARIANTS = {
    "A": ("litellm, no thinking param", lambda: via_litellm()),
    "B": ("litellm, thinking_level=minimal", lambda: via_litellm(thinking_level="minimal")),
    "C": ("litellm, extra_body thinkingLevel=minimal", lambda: via_litellm(
        extra_body={"generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}}})),
    "G": ("litellm, extra_body thinkingLevel=high (what triage / judge use)", lambda: via_litellm(
        extra_body={"generationConfig": {"thinkingConfig": {"thinkingLevel": "high"}}})),
    "D": ("litellm, reasoning_effort=minimal", lambda: via_litellm(reasoning_effort="minimal")),
    "E": ("REST, thinkingLevel=minimal", lambda: via_rest("minimal")),
    "F": ("REST, thinkingLevel=high", lambda: via_rest("high")),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=list(VARIANTS))
    ap.add_argument("--model", default=MODEL, help="Gemini API model id without the gemini/ prefix")
    ap.add_argument("--repeat", type=int, default=1, help="run the chosen variants this many times")
    ap.add_argument("--pause", type=float, default=20, help="seconds between repeats")
    args = ap.parse_args()
    globals()["MODEL"] = args.model.removeprefix("gemini/")
    load_env()
    if not _key():
        print("No GEMINI_API_KEY / GOOGLE_API_KEY after loading the .env (FINRAG_ENV_FILE).")
        return 1
    rows = []
    for rnd in range(args.repeat):
        if rnd:
            time.sleep(args.pause)
        rows += run_round(args.only, rnd + 1)
    ok = sum(1 for r in rows if r["ok"])
    print(json.dumps({"model": MODEL, "requests": len(rows), "ok": ok, "failed": len(rows) - ok,
                      "errors": sorted({r["error"].split(":")[0] for r in rows if not r["ok"]})}))
    return 0


def run_round(variants: list[str], rnd: int) -> list[dict]:
    rows = []
    for k in variants:
        name, fn = VARIANTS[k]
        t0 = time.time()
        try:
            r = fn()
            text = r["text"].strip()
            try:
                extract_json(text)
                parsed = True
            except ValueError:
                parsed = False
            pure = text.startswith("{") and text.endswith("}")
            row = {"round": rnd, "variant": k, "what": name, "ok": True, "sec": round(time.time() - t0, 1),
                   **{x: r.get(x) for x in ("in", "out", "thoughts_tokens", "reasoning_chars", "finish")},
                   "json_parses": parsed, "pure_json": pure, "text_head": text[:160]}
        except Exception as exc:  # noqa: BLE001
            row = {"round": rnd, "variant": k, "what": name, "ok": False, "sec": round(time.time() - t0, 1),
                   "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False))
    return rows


if __name__ == "__main__":
    sys.exit(main())
