"""LLM layer: one entry point ``LLM.call(purpose, messages, schema=None, ...)``.

* Wraps ``litellm.completion`` (same ``gemini/...`` ids as ``src/chatbot.py`` and ``src/generate.py``).
* Model rotation: the first model of ``llm.models`` whose daily request count is below ``daily_request_limit`` is
  used; on a 429 / quota error the model is marked exhausted for the day and the next one is tried. When every
  model is exhausted ``BudgetExhausted`` is raised, and the offline job can be resumed (cache).
* Cache: a JSON file per call, keyed by a SHA-1 of (purpose, messages, schema), NOT by model, so a rerun after
  rotation reuses earlier answers. Cache hits do not count toward the quota.
* Usage log: every call (also cache hits and failures) is written to the ``llm_calls`` table.
* Structured output: JSON is requested, validated with pydantic, retried once with the validation error.
* Generation options per model (``llm.model_overrides``, substring of the model id) and per step
  (``llm.step_options``): ``json_mode``, ``reasoning_effort``, ``thinking_level`` (Gemma 4 / Gemini 3:
  ``minimal`` = off, ``high`` = on; sent as ``generationConfig.thinkingConfig.thinkingLevel``), ``timeout_s``.
* Pacing: ``tokens_per_minute`` (per model, e.g. Gemma 4 free tier: 16,000) -> before each request the input tokens
  sent to that model in the last 60 s are summed (estimate: characters / ``chars_per_token``) and the call waits
  until the next request fits; a request larger than the whole limit is refused at once (``LLMRequestRejected``).
* Errors: 429 -> per-day quota: model used up for today; any other 429 (per minute, tokens): wait >= 65 s, retry,
  then ``LLMUnavailable`` (the model is NOT marked used up); 5xx / timeouts -> retried with growing waits (``server_error_backoff_s``), then the
  next model, then ``LLMUnavailable``; 400 (the request itself is wrong) -> ``LLMRequestRejected`` at once.
* Secrets: ``python-dotenv`` loads the file named in ``FINRAG_ENV_FILE`` (default ``../.env`` next to the repo).
  Key values are never printed, logged or copied.
* ``mock/<name>`` models (and ``LLM(backend=...)``) are used by tests and dry demos; they never touch the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
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


class LLMUnavailable(RuntimeError):
    """The provider kept failing (5xx / timeouts) for every configured model; the job can be repeated later."""


class LLMRequestRejected(RuntimeError):
    """The provider rejected the request itself (HTTP 400), e.g. an option the model does not support."""


class litellm_timeout_for_test(Exception):  # noqa: N801 - a timeout-like error for tests (no litellm needed)
    pass


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


def _per_day(exc: BaseException) -> bool:
    """Does a 429 name a DAILY quota (quotaId ``...PerDay...``)? Only then is the model used up for today."""
    msg = str(exc).lower()
    return "perday" in msg or "per day" in msg or "requests_per_day" in msg or "daily" in msg


def is_model_unavailable(exc: BaseException) -> bool:
    """404 / "no longer available": the model id itself is unusable, so rotate to the next one."""
    msg = f"{type(exc).__name__} {exc}".lower()
    return getattr(exc, "status_code", None) == 404 or type(exc).__name__ == "NotFoundError" \
        or "no longer available" in msg or "is not found" in msg


def is_bad_request(exc: BaseException) -> bool:
    """HTTP 400 / INVALID_ARGUMENT: repeating the same request cannot help."""
    msg = f"{type(exc).__name__} {exc}".lower()
    return getattr(exc, "status_code", None) == 400 or type(exc).__name__ in ("BadRequestError", "UnsupportedParamsError") \
        or "invalid_argument" in msg


_SERVER = ("internalservererror", "serviceunavailable", "timeout", "apiconnectionerror", "badgatewayerror",
           "gatewaytimeout", "internal error", "unavailable", "high demand", "overloaded", "deadline exceeded",
           "connection reset", "connection aborted", "remotedisconnected")


def is_server_error(exc: BaseException) -> bool:
    """5xx, overload, timeouts, dropped connections: worth repeating after a pause."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and 500 <= code < 600:
        return True
    msg = f"{type(exc).__name__} {exc}".lower()
    return any(s in msg for s in _SERVER) or any(f" {c}" in msg or f"'{c}" in msg or f'"code": {c}' in msg
                                                 for c in ("500", "502", "503", "504"))


def retry_hint(exc: BaseException) -> float | None:
    """Seconds the provider asks us to wait ("retryDelay": "30s" / "retry in 12.5s"), if it says so."""
    m = re.search(r'retry(?:delay)?["\s:]*(?:in\s*)?"?(\d+(?:\.\d+)?)\s*s', str(exc), re.I)
    return float(m.group(1)) if m else None


def _provider_message(exc: BaseException) -> str:
    """The provider's own message ("message": "...") or the start of the error text."""
    m = re.search(r'"message":\s*"([^"]+)"', str(exc))
    return m.group(1) if m else str(exc)[:200]


def _excerpt(text: str, head: int = 600, tail: int = 200) -> str:
    """The start and end of a failed answer for the retry: enough to show the mistake, small enough that the retry
    is not much bigger than the original request (tokens per minute)."""
    t = text or ""
    return t if len(t) <= head + tail + 20 else f"{t[:head]}\n[... {len(t) - head - tail} characters left out ...]\n{t[-tail:]}"


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
                    reasoning_effort: str | None = None, thinking_level: str | None = None,
                    timeout: float | None = None) -> dict:
    import litellm

    litellm.drop_params = True
    kwargs: dict[str, Any] = dict(model=model, messages=messages, max_tokens=max_tokens)
    if timeout:
        kwargs["timeout"] = timeout
    if thinking_level:
        # LiteLLM turns reasoning_effort into a thinking BUDGET for every model except Gemini 3, and Gemma 4 rejects a
        # budget (400). The thinking LEVEL is therefore sent as-is; LiteLLM merges extra_body into the request body.
        kwargs["extra_body"] = {"generationConfig": {"thinkingConfig": {"thinkingLevel": thinking_level}}}
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
                 cache_dir: Path | str | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        llm_cfg = cfg["llm"]
        # ``single_model`` switches the whole product to ONE model (all steps, no rotation); otherwise ``models``
        # is the default rotation list and ``step_models`` may give a different list for a single step.
        self.single_model: str | None = llm_cfg.get("single_model") or None
        self.models: list[str] = [self.single_model] if self.single_model else list(llm_cfg["models"])
        self.step_models: dict[str, list[str]] = {} if self.single_model else {
            k: list(v) for k, v in (llm_cfg.get("step_models") or {}).items() if v}
        lim = llm_cfg.get("daily_request_limit", 20)
        self.limit: int | None = None if lim is None else int(lim)       # None = no daily limit (billed models)
        self.json_mode_default: bool = bool(llm_cfg.get("json_mode", True))
        self.model_overrides: dict[str, dict] = dict(llm_cfg.get("model_overrides") or {})
        self.max_live: int | None = llm_cfg.get("max_requests_per_run")
        self.calm_on_timeout: bool = bool(llm_cfg.get("thinking_off_after_timeout", True))
        self.live_requests = 0                      # network attempts made by this object (safety valve)
        self.deadline: float | None = None          # time.time() after which a waiting request gives up (the chat sets it)
        self.temperature: float = float(llm_cfg.get("temperature", 0))
        self.max_tokens: int = int(llm_cfg.get("max_output_tokens", 8192))
        self.json_retries: int = int(llm_cfg.get("retries_on_invalid_json", 1))
        self.reasoning_effort: str | None = llm_cfg.get("reasoning_effort")
        self.thinking_level: str | None = llm_cfg.get("thinking_level")
        self.step_options: dict[str, dict] = dict(llm_cfg.get("step_options") or {})
        self.timeout_s: float | None = llm_cfg.get("request_timeout_s", 300)
        self.backoff: list[float] = [float(x) for x in (llm_cfg.get("server_error_backoff_s") or [5, 15, 30, 60, 90])]
        self.tokens_per_minute: int | None = llm_cfg.get("tokens_per_minute")
        self.chars_per_token: float = float(llm_cfg.get("chars_per_token", 2.0))
        # waits after a 429 that is not the daily quota: growing, since refused attempts seem to count as well
        rl = llm_cfg.get("rate_limit_backoff_s")
        self.rate_limit_backoff: list[float] = [float(x) for x in rl] if rl else \
            [float(llm_cfg.get("rate_limit_wait_s", 65))] * int(llm_cfg.get("rate_limit_retries", 5))
        self._sent: dict[str, list[tuple[float, int]]] = {}   # model -> [(time, input tokens)] of the last minute
        self._clock: Callable[[], float] = clock
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
    def prompt_hash(purpose: str, messages: list[dict], schema, prompt_version: str = "",
                    variant: dict | None = None) -> str:
        schema_repr = json.dumps(schema.model_json_schema(), sort_keys=True) if schema is not None else ""
        blob = {"p": purpose, "m": messages, "s": schema_repr}
        if prompt_version:                       # added only when given, so cache keys of v1 prompts do not change
            blob["v"] = prompt_version
        if variant:                              # model + generation options: a different setting = a new answer
            blob["g"] = variant
        return hashlib.sha1(json.dumps(blob, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def models_for(self, purpose: str | None) -> list[str]:
        """Models tried (in order) for a step: ``llm.step_models[purpose]`` or the default list."""
        return self.step_models.get(purpose or "", self.models)

    def options_for(self, model: str, purpose: str | None = None) -> dict:
        """Generation options: defaults < ``llm.model_overrides`` (substring of the model id) < ``llm.step_options``."""
        opts = {"json_mode": self.json_mode_default, "reasoning_effort": self.reasoning_effort,
                "thinking_level": self.thinking_level, "timeout_s": self.timeout_s,
                "tokens_per_minute": self.tokens_per_minute, "server_error_backoff_s": None}
        for pattern, o in self.model_overrides.items():
            if pattern in model:
                opts.update({k: v for k, v in (o or {}).items() if k != "by_step"})
        opts.update(self.step_options.get(purpose or "") or {})
        for pattern, o in self.model_overrides.items():          # a model's own setting for ONE step beats the step's
            if pattern in model:
                opts.update(((o or {}).get("by_step") or {}).get(purpose or "") or {})
        return opts

    def variant(self, model: str, purpose: str | None = None) -> dict:
        """What decides the answer besides the prompt: part of the cache key."""
        o = self.options_for(model, purpose)
        return {"model": model, **{k: o[k] for k in ("json_mode", "reasoning_effort", "thinking_level")}}

    def estimate_tokens(self, messages: list[dict]) -> int:
        return int(sum(len(m.get("content") or "") for m in messages) / max(self.chars_per_token, 0.5)) + 1

    def _nap(self, seconds: float):
        """Sleep before a retry, unless the caller's deadline (an interactive chat turn) would pass meanwhile."""
        if self.deadline is not None and time.time() + seconds > self.deadline:
            raise LLMUnavailable("the model is not answering in time (waiting for a retry would take too long)")
        self._sleep(seconds)

    def _pace(self, model: str, tokens: int, tpm: int | None):
        """Wait until ``tokens`` more input tokens fit into the model's tokens-per-minute window."""
        if not tpm:
            return
        if tokens > tpm:
            raise LLMRequestRejected(
                f"the request is about {tokens} input tokens, more than {model}'s limit of {tpm} tokens per minute: "
                "it can never be accepted. Lower the batch size of this step in configs/kg.yaml "
                "(e.g. extraction.diff_batch, extraction.obligation_batch, mapping.*_batch).")
        while True:
            now = self._clock()
            window = [(t, n) for t, n in self._sent.get(model, []) if now - t < 60]
            self._sent[model] = window
            used = sum(n for _, n in window)
            if used + tokens <= tpm or not window:
                window.append((now, tokens))
                return
            wait = 60 - (now - window[0][0]) + 1                # until the oldest request leaves the window
            self._sleep(max(1.0, wait))

    def _pick_model(self, skip: set[str], purpose: str | None = None) -> str:
        day = quota_day()
        models = self.models_for(purpose)
        for m in models:
            if m in skip:
                continue
            q = self.store.quota_used(m, day)
            if not q["exhausted"] and (self.limit is None or q["requests"] < self.limit):
                return m
        raise BudgetExhausted(f"no model left ({len(models)} configured for '{purpose}'): daily limit of "
                              f"{self.limit} requests reached, model unavailable, or failing")

    def remaining_budget(self, purpose: str | None = None) -> int | None:
        """Requests left today over the models of a step (None = unlimited)."""
        if self.limit is None:
            return None
        day = quota_day()
        total = 0
        for m in self.models_for(purpose):
            q = self.store.quota_used(m, day)
            total += 0 if q["exhausted"] else max(0, self.limit - q["requests"])
        return total

    def _request(self, purpose: str, messages: list[dict], max_tokens: int, json_mode: bool, key: str):
        """One network request with rotation. Returns (model, backend_result, latency_ms)."""
        day = quota_day()
        skip: set[str] = set()
        fails: dict[str, int] = {}
        waits: dict[str, int] = {}
        tokens = self.estimate_tokens(messages)
        calm = False                     # thinking switched off for this request after it hung / kept failing
        while True:
            if self.max_live is not None and self.live_requests >= int(self.max_live):
                raise BudgetExhausted(f"max_requests_per_run ({self.max_live}) reached")
            model = self._pick_model(skip, purpose)
            opts = self.options_for(model, purpose)
            if calm:
                opts = {**opts, "thinking_level": "minimal"}
            try:
                self._pace(model, tokens, opts.get("tokens_per_minute"))  # refused attempts count in the window too
            except LLMRequestRejected as exc:
                self.store.log_llm_call(self.run_id, purpose, model, key, tokens, None, 0, False,
                                        f"too_large: {str(exc)[:200]}")
                raise
            self.store.quota_inc(model, day)  # count the attempt (the provider counts it too)
            self.live_requests += 1
            use_json = json_mode and bool(opts["json_mode"])
            t0 = time.time()
            try:
                if self._backend is not None:
                    res = self._backend(model=model, messages=messages, max_tokens=max_tokens,
                                        temperature=self.temperature, json_mode=use_json, purpose=purpose)
                elif model.startswith("mock/"):
                    raise RuntimeError("mock/ models need an explicit backend")
                else:
                    res = litellm_backend(model, messages, max_tokens, self.temperature, use_json,
                                          opts["reasoning_effort"], opts["thinking_level"], opts["timeout_s"])
                real = res.get("input_tokens") if isinstance(res, dict) else None
                if real and self._sent.get(model):              # the provider's own count, if larger than ours
                    t_sent, est = self._sent[model][-1]
                    self._sent[model][-1] = (t_sent, max(est, int(real)))
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
                    note = f"quota: {type(exc).__name__}: {str(exc)[:1500]}"
                    self.store.log_llm_call(self.run_id, purpose, model, key, None, None, lat, False, note)
                    used = self.store.quota_used(model, day)["requests"]
                    daily = _per_day(exc) or (self.limit is not None and used >= self.limit)
                    if not daily:
                        # per-minute requests or tokens: the minute must really empty before the next attempt
                        self.store.quota_dec(model, day)
                        n = waits.get(model, 0)
                        if n < len(self.rate_limit_backoff):
                            waits[model] = n + 1
                            self._nap(max(self.rate_limit_backoff[n], min(retry_hint(exc) or 0, 300.0)))
                            continue
                        skip.add(model)
                        if all(m in skip for m in self.models_for(purpose)):
                            raise LLMUnavailable(
                                f"{model} kept refusing with a rate limit (429) after {n + 1} attempts "
                                f"({_provider_message(exc)}); it is NOT the daily quota ({used} requests counted "
                                "today). Every finished step is stored: repeat the same command later, or lower "
                                "llm.tokens_per_minute / the batch sizes.") from exc
                        continue
                    self.store.quota_mark_exhausted(model, day)
                    skip.add(model)
                    continue
                self.store.quota_dec(model, day)                 # not answered: the provider does not charge it
                self.store.log_llm_call(self.run_id, purpose, model, key, None, None, lat, False,
                                        f"{type(exc).__name__}: {str(exc)[:200]}")
                if purpose == "agent_step" and is_bad_request(exc) and "not supported" in str(exc).lower() \
                        and any(m not in skip and m != model for m in self.models_for(purpose)):
                    skip.add(model)          # the chat must not die because one model refuses an option: try the next
                    continue
                if is_bad_request(exc) or not is_server_error(exc):
                    # the request itself is wrong (or an unknown error): repeating it cannot help
                    raise LLMRequestRejected(
                        f"{model} rejected the request ({type(exc).__name__}): {_provider_message(exc)}. "
                        "If an option is not supported by this model, change llm.model_overrides in "
                        "configs/kg.yaml.") from exc
                n = fails.get(model, 0)
                fails[model] = n + 1
                # with thinking ON the same request (temperature 0) tends to think just as long again: after a
                # timeout, or a second server error, ask once more WITHOUT thinking instead of hanging all night
                thinking = opts.get("thinking_level") not in (None, "minimal")
                if thinking and not calm and self.calm_on_timeout and \
                        ("timeout" in f"{type(exc).__name__} {exc}".lower() or n >= 1):
                    calm = True
                    self.store.log_llm_call(self.run_id, purpose, model, key, None, None, 0, False,
                                            "thinking_off: the next attempt is sent without thinking")
                    continue
                backoff = [float(x) for x in opts["server_error_backoff_s"]] if opts.get("server_error_backoff_s") is not None \
                    else self.backoff
                if n >= len(backoff):                            # this model keeps failing: try the next one
                    skip.add(model)
                    if all(m in skip for m in self.models_for(purpose)):
                        raise LLMUnavailable(
                            f"the provider kept failing for {', '.join(self.models_for(purpose))} "
                            f"({n + 1} attempts on {model}; last: {_provider_message(exc)}). Every finished step is "
                            "stored: repeat the same command later.") from exc
                    continue
                wait = max(backoff[n], min(retry_hint(exc) or 0, 180.0))
                self._nap(wait * random.uniform(0.85, 1.15))

    # ------------------------------------------------------------------ public API
    def call(self, purpose: str, messages: list[dict], schema=None, max_tokens: int | None = None,
             use_cache: bool = True, prompt_version: str = "") -> LLMResult:
        """Run one logical call. ``schema`` is a pydantic model class; the result's ``parsed`` is an instance.
        ``prompt_version`` is part of the cache key, so an old cached answer is never reused for a new prompt."""
        max_tokens = max_tokens or self.max_tokens
        models = self.models_for(purpose)
        keys = {m: self.prompt_hash(purpose, messages, schema, prompt_version, self.variant(m, purpose)) for m in models}
        key = keys[models[0]] if models else self.prompt_hash(purpose, messages, schema, prompt_version)
        if use_cache:              # an answer of ANY configured model with the same options is reused (rotation)
            for m in models:
                path = self._cache_path(keys[m])
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    parsed = self._validate(data["text"], schema) if schema is not None else None
                    self.store.log_llm_call(self.run_id, purpose, data.get("model"), keys[m], 0, 0, 0, True, None)
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
                    {"role": "assistant", "content": _excerpt(text)},
                    {"role": "user", "content": "The previous answer was not valid for the required JSON schema: "
                                                f"{last_err[:600]}. Return ONLY corrected JSON."}]
                continue
            self.store.log_llm_call(self.run_id, purpose, model, key, res.get("input_tokens"),
                                    res.get("output_tokens"), lat, False, None)
            path = self._cache_path(keys.get(model) or self.prompt_hash(purpose, messages, schema, prompt_version,
                                                                        self.variant(model, purpose)))
            path.write_text(json.dumps({"text": text, "model": model, "purpose": purpose}, ensure_ascii=False),
                            encoding="utf-8")
            return LLMResult(text, parsed, model, False, res.get("input_tokens"), res.get("output_tokens"), lat)
        err = LLMValidationError(last_err)
        err.text = text                          # what the model actually wrote (a caller may still use plain prose)
        raise err

    @staticmethod
    def _validate(text: str, schema):
        try:
            data = extract_json(text)
        except ValueError as exc:
            raise LLMValidationError(str(exc)) from exc
        fields = getattr(schema, "model_fields", {})
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and (
                "results" not in fields or "results" in data[0]):
            data = data[0]                    # Gemma 4 (thinking off) wraps the object in a list: [ {...} ]
        elif isinstance(data, list) and "results" in fields:
            data = {"results": data}          # some models answer with the bare list
        try:
            return schema.model_validate(data)
        except Exception as exc:  # pydantic.ValidationError
            raise LLMValidationError(str(exc)) from exc


def system_user(system: str, user: str) -> list[dict]:
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
