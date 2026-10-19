"""LLM layer: one entry point ``LLM.call(purpose, messages, schema=None, ...)``.

* Wraps ``litellm.completion`` (same ``gemini/...`` ids as ``src/chatbot.py`` and ``src/generate.py``).
* Model rotation: the first model of ``llm.models`` whose daily request count is below ``daily_request_limit`` is
  used; on a 429 / quota error the model is marked exhausted for the day and the next one is tried. When every
  model is exhausted ``BudgetExhausted`` is raised, and the offline job can be resumed (cache).
* Cache: a JSON file per call, keyed by a SHA-1 of (purpose, messages, schema), NOT by model, so a rerun after
  rotation reuses earlier answers. Cache hits do not count toward the quota.
* Usage log: every call (also cache hits and failures) is written to the ``llm_calls`` table.
* Structured output: JSON is requested, validated with pydantic, retried once with the validation error.
* Secrets: ``python-dotenv`` loads the file named in ``FINRAG_ENV_FILE`` (default ``../.env`` next to the repo).
  Key values are never printed, logged or copied.
* ``mock/<name>`` models (and ``LLM(backend=...)``) are used by tests and dry demos; they never touch the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kg.config import REPO_ROOT, today_iso


class BudgetExhausted(RuntimeError):
    """Every configured model has reached its daily request limit."""


class LLMValidationError(RuntimeError):
    """The model answered, but not with JSON that satisfies the schema (after the retry)."""


@dataclass
class LLMResult:
    text: str
    parsed: Any = None
    model: str = ""
    cached: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int = 0


Backend = Callable[..., dict]


def load_env() -> None:
    """Load API keys from the .env named by FINRAG_ENV_FILE (default: ../.env relative to the repo)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    path = Path(os.getenv("FINRAG_ENV_FILE") or REPO_ROOT.parent / ".env")
    if path.exists():
        load_dotenv(path, override=False)


def is_quota_error(exc: BaseException) -> bool:
    code = getattr(exc, "status_code", None)
    msg = f"{type(exc).__name__} {exc}".lower()
    return code == 429 or any(s in msg for s in ("429", "rate limit", "ratelimit", "quota", "resource_exhausted",
                                                 "resource exhausted"))


def quota_day() -> str:
    """Google resets daily quotas at midnight Pacific time (about 11:00 in Tbilisi), not at local midnight."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _per_minute(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "perminute" in msg or "per minute" in msg or "retry in" in msg and "day" not in msg


def is_model_unavailable(exc: BaseException) -> bool:
    """404 / "no longer available": the model id itself is unusable, so rotate to the next one."""
    msg = f"{type(exc).__name__} {exc}".lower()
    return getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "NotFoundError" \
        or "no longer available" in msg or "is not found" in msg


def _strip_fences(text: str) -> str:
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    return m.group(1) if m else t


def extract_json(text: str):
    """Parse JSON from a model answer (tolerates code fences and leading/trailing prose)."""
    t = _strip_fences(text)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model output")


def litellm_backend(model: str, messages: list[dict], max_tokens: int, temperature: float, json_mode: bool,
                    reasoning_effort: str | None = None) -> dict:
    import litellm

    litellm.drop_params = True
    kwargs: dict[str, Any] = dict(model=model, messages=messages, max_tokens=max_tokens)
    if "gemini-3" not in model:        # Gemini 3+ deprecates sampling parameters (LiteLLM prints a warning)
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if reasoning_effort:
        # Gemini "thinking" tokens are counted against max_tokens; without a cap the answer gets cut off.
        kwargs["reasoning_effort"] = reasoning_effort
    resp = litellm.completion(**kwargs)
    usage = getattr(resp, "usage", None)
    return {
        "text": resp.choices[0].message.content or "",
        "finish_reason": getattr(resp.choices[0], "finish_reason", None),
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
    }


class LLM:
    def __init__(self, cfg, store, run_id: str | None = None, backend: Backend | None = None,
                 cache_dir: Path | str | None = None, sleep: Callable[[float], None] = time.sleep):
        llm_cfg = cfg["llm"]
        self.models: list[str] = list(llm_cfg["models"])
        self.limit: int = int(llm_cfg.get("daily_request_limit", 20))
        self.temperature: float = float(llm_cfg.get("temperature", 0))
        self.max_tokens: int = int(llm_cfg.get("max_output_tokens", 8192))
        self.json_retries: int = int(llm_cfg.get("retries_on_invalid_json", 1))
        self.reasoning_effort: str | None = llm_cfg.get("reasoning_effort")
        self.store = store
        self.run_id = run_id
        self._backend = backend
        self._sleep = sleep
        self.cache_dir = Path(cache_dir) if cache_dir else cfg.path("llm_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if backend is None:
            load_env()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def prompt_hash(purpose: str, messages: list[dict], schema) -> str:
        schema_repr = json.dumps(schema.model_json_schema(), sort_keys=True) if schema is not None else ""
        blob = json.dumps({"p": purpose, "m": messages, "s": schema_repr}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _pick_model(self, skip: set[str]) -> str:
        day = quota_day()
        for m in self.models:
            if m in skip:
                continue
            q = self.store.quota_used(m, day)
            if not q["exhausted"] and q["requests"] < self.limit:
                return m
        raise BudgetExhausted(f"no model left ({len(self.models)} configured): daily limit of {self.limit} requests "
                              "reached, model unavailable, or failing")

    def remaining_budget(self) -> int:
        day = quota_day()
        total = 0
        for m in self.models:
            q = self.store.quota_used(m, day)
            total += 0 if q["exhausted"] else max(0, self.limit - q["requests"])
        return total

    def _request(self, purpose: str, messages: list[dict], max_tokens: int, json_mode: bool, key: str):
        """One network request with rotation. Returns (model, backend_result, latency_ms)."""
        day = quota_day()
        skip: set[str] = set()
        fails: dict[str, int] = {}
        waits = 0
        while True:
            model = self._pick_model(skip)
            self.store.quota_inc(model, day)  # count the attempt (the provider counts it too)
            t0 = time.time()
            try:
                if self._backend is not None:
                    res = self._backend(model=model, messages=messages, max_tokens=max_tokens,
                                        temperature=self.temperature, json_mode=json_mode, purpose=purpose)
                elif model.startswith("mock/"):
                    raise RuntimeError("mock/ models need an explicit backend")
                else:
                    res = litellm_backend(model, messages, max_tokens, self.temperature, json_mode, self.reasoning_effort)
                return model, res, int((time.time() - t0) * 1000)
            except BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001 - provider errors vary
                lat = int((time.time() - t0) * 1000)
                if is_model_unavailable(exc) and not is_quota_error(exc):
                    self.store.quota_mark_exhausted(model, day)      # skip it for today, try the next model
                    skip.add(model)
                    self.store.log_llm_call(self.run_id, purpose, model, key, None, None, lat, False,
                                            f"model_unavailable: {str(exc)[:160]}")
                    continue
                if is_quota_error(exc):
                    note = f"quota: {type(exc).__name__}: {str(exc)[:300]}"
                    self.store.log_llm_call(self.run_id, purpose, model, key, None, None, lat, False, note)
                    if _per_minute(exc) and waits < 3:           # rate limit per minute, not the daily quota
                        waits += 1
                        self._sleep(30)
                        continue
                    self.store.quota_mark_exhausted(model, day)
                    skip.add(model)
                    continue
                # transient failure (503 overload, timeout ...): the provider does not charge it, so refund it
                self.store.quota_dec(model, day)
                fails[model] = fails.get(model, 0) + 1
                self.store.log_llm_call(self.run_id, purpose, model, key, None, None, lat, False,
                                        f"{type(exc).__name__}: {str(exc)[:200]}")
                if fails[model] >= 2:
                    skip.add(model)                             # try another model; it is not marked exhausted
                    if len(skip) >= len(self.models):
                        raise
                self._sleep(2 ** fails[model])

    # ------------------------------------------------------------------ public API
    def call(self, purpose: str, messages: list[dict], schema=None, max_tokens: int | None = None,
             use_cache: bool = True) -> LLMResult:
        """Run one logical call. ``schema`` is a pydantic model class; the result's ``parsed`` is an instance."""
        max_tokens = max_tokens or self.max_tokens
        key = self.prompt_hash(purpose, messages, schema)
        path = self._cache_path(key)
        if use_cache and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            parsed = self._validate(data["text"], schema) if schema is not None else None
            self.store.log_llm_call(self.run_id, purpose, data.get("model"), key, 0, 0, 0, True, None)
            return LLMResult(data["text"], parsed, data.get("model", ""), True, 0, 0, 0)

        msgs = list(messages)
        last_err = ""
        for attempt in range(self.json_retries + 1):
            model, res, lat = self._request(purpose, msgs, max_tokens, schema is not None, key)
            text = res["text"]
            try:
                parsed = self._validate(text, schema) if schema is not None else None
            except LLMValidationError as exc:
                last_err = str(exc)
                self.store.log_llm_call(self.run_id, purpose, model, key, res.get("input_tokens"),
                                        res.get("output_tokens"), lat, False, f"invalid_json: {last_err[:200]}")
                if res.get("finish_reason") == "length":       # truncated: a retry would be cut off the same way
                    last_err = "output truncated at max_tokens (" + last_err[:100] + ")"
                    break
                msgs = list(messages) + [
                    {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": "The previous answer was not valid for the required JSON schema: "
                                                f"{last_err[:600]}. Return ONLY corrected JSON."}]
                continue
            self.store.log_llm_call(self.run_id, purpose, model, key, res.get("input_tokens"),
                                    res.get("output_tokens"), lat, False, None)
            path.write_text(json.dumps({"text": text, "model": model, "purpose": purpose}, ensure_ascii=False),
                            encoding="utf-8")
            return LLMResult(text, parsed, model, False, res.get("input_tokens"), res.get("output_tokens"), lat)
        raise LLMValidationError(last_err)

    @staticmethod
    def _validate(text: str, schema):
        try:
            data = extract_json(text)
        except ValueError as exc:
            raise LLMValidationError(str(exc)) from exc
        if isinstance(data, list) and "results" in getattr(schema, "model_fields", {}):
            data = {"results": data}          # some models answer with the bare list
        try:
            return schema.model_validate(data)
        except Exception as exc:  # pydantic.ValidationError
            raise LLMValidationError(str(exc)) from exc


def system_user(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
