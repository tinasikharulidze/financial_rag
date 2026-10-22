import json

import pytest
from pydantic import BaseModel

from kg.config import load_config, today_iso
from kg.llm import LLM, quota_day, BudgetExhausted, LLMValidationError, is_quota_error
from kg.store.sqlite_nx import SQLiteStore


@pytest.fixture()
def store(tmp_path):
    return SQLiteStore(tmp_path / "kg.sqlite")


def cfg_with(models, limit=2):
    c = load_config()
    c = type(c)(json.loads(json.dumps(c)))
    c["llm"]["models"] = models
    c["llm"]["daily_request_limit"] = limit
    return c


class Out(BaseModel):
    x: int


def test_store_nodes_edges_asof(store):
    store.upsert_node("a", "Act", {"t": 1})
    store.upsert_node("pv1", "ProvisionVersion", {"text": "old"}, "2020-01-01", "2022-01-01")
    store.upsert_node("pv2", "ProvisionVersion", {"text": "new"}, "2022-01-01", None)
    store.upsert_edge("pv2", "pv1", "UPDATES", {"change_type": "modified"}, "2022-01-01")
    assert store.get_node("pv2")["props"]["text"] == "new"
    assert [n["id"] for n in store.nodes("ProvisionVersion", as_of="2021-06-01")] == ["pv1"]
    g = store.as_of("2023-01-01")
    assert "pv2" in g and "pv1" not in g
    assert store.neighbors("pv2", "UPDATES")[0]["node"]["id"] == "pv1"
    assert store.neighbors("pv1", "UPDATES", "in")[0]["node"]["id"] == "pv2"
    assert store.subgraph(["pv1", "pv2"]).number_of_edges() == 1
    # georgian text survives a round trip
    store.upsert_node("g", "Provision", {"text": "მოდელის რისკი"})
    assert store.get_node("g")["props"]["text"] == "მოდელის რისკი"


def test_alert_dedup_and_findings(store):
    assert store.add_alert("r", "uncovered_obligation", "o1", "m")
    assert not store.add_alert("r", "uncovered_obligation", "o1", "m")
    store.add_findings([{"run_id": "r", "doc_id": "d", "obligation_id": "o1", "priority": "high"}])
    assert store.findings("d")[0]["priority"] == "high"


def test_llm_logs_cache_and_schema(store, tmp_path):
    calls = []

    def backend(model, messages, **kw):
        calls.append(model)
        return {"text": '```json\n{"x": 3}\n```', "input_tokens": 10, "output_tokens": 4}

    llm = LLM(cfg_with(["mock/a", "mock/b"]), store, "run1", backend, tmp_path / "cache")
    r = llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert r.parsed.x == 3 and not r.cached
    r2 = llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert r2.cached and len(calls) == 1
    rows = store.llm_calls("run1")
    assert rows[0]["model"] == "mock/a" and rows[0]["input_tokens"] == 10 and rows[0]["cache_hit"] == 0
    assert rows[1]["cache_hit"] == 1


def test_rotation_on_429_and_on_limit(store, tmp_path):
    class RateLimit(Exception):
        status_code = 429

    seen = []

    def backend(model, messages, **kw):
        seen.append(model)
        if model == "mock/a":
            raise RateLimit("429 Quota exceeded for quota metric GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        return {"text": '{"x": 1}', "input_tokens": 1, "output_tokens": 1}

    llm = LLM(cfg_with(["mock/a", "mock/b"], limit=2), store, "r", backend, tmp_path / "c")
    assert llm.call("p1", [{"role": "user", "content": "1"}], Out).model == "mock/b"
    assert store.quota_used("mock/a", quota_day())["exhausted"] == 1
    assert seen == ["mock/a", "mock/b"]
    llm.call("p2", [{"role": "user", "content": "2"}], Out)           # b: 2nd request
    with pytest.raises(BudgetExhausted):
        llm.call("p3", [{"role": "user", "content": "3"}], Out)      # a exhausted, b at limit


def test_invalid_json_retries_then_fails(store, tmp_path):
    n = {"c": 0}

    def backend(model, messages, **kw):
        n["c"] += 1
        return {"text": "not json", "input_tokens": 1, "output_tokens": 1}

    llm = LLM(cfg_with(["mock/a"], limit=10), store, "r", backend, tmp_path / "c")
    with pytest.raises(LLMValidationError):
        llm.call("p", [{"role": "user", "content": "x"}], Out)
    assert n["c"] == 2  # original + one retry


def test_quota_error_detection():
    class E(Exception):
        status_code = 429
    assert is_quota_error(E("x"))
    assert is_quota_error(RuntimeError("RESOURCE_EXHAUSTED"))
    assert not is_quota_error(RuntimeError("boom"))


def test_unavailable_model_is_skipped(store, tmp_path):
    class NotFoundError(Exception):
        pass

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        if model == "m1":
            raise NotFoundError("This model is no longer available to new users")
        return {"text": '{"x": 1}', "input_tokens": 1, "output_tokens": 1}

    llm = LLM(cfg_with(["m1", "m2"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c")
    res = llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert res.model == "m2" and res.parsed.x == 1
    assert store.quota_used("m1", quota_day())["exhausted"] == 1


def test_truncated_output_is_not_retried(store, tmp_path):
    calls = []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        calls.append(1)
        return {"text": '{"x": ', "finish_reason": "length", "input_tokens": 1, "output_tokens": 9}

    llm = LLM(cfg_with(["m1"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c")
    with pytest.raises(LLMValidationError):
        llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert len(calls) == 1


def test_transient_errors_rotate_and_are_refunded(store, tmp_path):
    class ServiceUnavailableError(Exception):
        pass

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        if model == "m1":
            raise ServiceUnavailableError("503 high demand")
        return {"text": '{"x": 2}', "input_tokens": 1, "output_tokens": 1}

    llm = LLM(cfg_with(["m1", "m2"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c",
              sleep=lambda s: None)
    res = llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert res.model == "m2"
    assert store.quota_used("m1", quota_day())["requests"] == 0       # refunded, not exhausted
    assert store.quota_used("m1", quota_day())["exhausted"] == 0


def test_bare_list_is_accepted_for_results_schema(store, tmp_path):
    from pydantic import BaseModel

    class Res(BaseModel):
        results: list[int]

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        return {"text": "[1, 2, 3]", "input_tokens": 1, "output_tokens": 1}

    llm = LLM(cfg_with(["m1"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c")
    assert llm.call("t", [{"role": "user", "content": "hi"}], Res).parsed.results == [1, 2, 3]


# ---------------------------------------------------------------------------------------------- Gemma-related behaviour
def test_one_element_list_is_unwrapped(store, tmp_path):
    from pydantic import BaseModel

    class Res(BaseModel):
        results: list[int]

    answers = iter(['[{"x": 5}]', '[{"results": [1, 2]}]'])

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        return {"text": next(answers), "input_tokens": 1, "output_tokens": 1}

    llm = LLM(cfg_with(["m1"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c")
    assert llm.call("a", [{"role": "user", "content": "1"}], Out).parsed.x == 5            # Gemma: [ {...} ]
    assert llm.call("b", [{"role": "user", "content": "2"}], Res).parsed.results == [1, 2]


class BadRequestError(Exception):
    status_code = 400


def test_bad_request_is_not_retried(store, tmp_path):
    from kg.llm import LLMRequestRejected
    calls, sleeps = [], []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        calls.append(model)
        raise BadRequestError('{"error": {"code": 400, "message": "Thinking budget is not supported for this model."}}')

    llm = LLM(cfg_with(["m1", "m2"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c",
              sleep=sleeps.append)
    with pytest.raises(LLMRequestRejected, match="Thinking budget is not supported"):
        llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert calls == ["m1"] and sleeps == []
    assert store.quota_used("m1", quota_day())["requests"] == 0          # refunded


def test_unknown_error_is_not_retried(store, tmp_path):
    from kg.llm import LLMRequestRejected
    calls = []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        calls.append(1)
        raise ValueError("something unexpected")

    llm = LLM(cfg_with(["m1"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c", sleep=lambda s: None)
    with pytest.raises(LLMRequestRejected):
        llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert len(calls) == 1


class InternalServerError(Exception):
    pass


def test_server_errors_are_retried_with_growing_waits(store, tmp_path):
    n, sleeps = {"c": 0}, []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        n["c"] += 1
        if n["c"] <= 3:
            raise InternalServerError('{"error": {"code": 500, "message": "Internal error encountered."}}')
        return {"text": '{"x": 3}', "input_tokens": 1, "output_tokens": 1}

    cfg = cfg_with(["m1"], limit=5)
    cfg["llm"]["server_error_backoff_s"] = [5, 15, 30, 60, 90]
    llm = LLM(cfg, store, "r", backend=backend, cache_dir=tmp_path / "c", sleep=sleeps.append)
    assert llm.call("t", [{"role": "user", "content": "hi"}], Out).parsed.x == 3
    assert n["c"] == 4 and len(sleeps) == 3
    for got, base in zip(sleeps, (5, 15, 30)):
        assert base * 0.85 <= got <= base * 1.15
    assert store.quota_used("m1", quota_day())["requests"] == 1          # only the answered request counts


def test_server_errors_end_in_llm_unavailable(store, tmp_path):
    from kg.llm import LLMUnavailable
    calls = []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        calls.append(model)
        raise InternalServerError('503 {"error": {"code": 503, "message": "This model is currently experiencing high '
                                  'demand."}}')

    cfg = cfg_with(["m1", "m2"], limit=50)
    cfg["llm"]["server_error_backoff_s"] = [1, 2]
    llm = LLM(cfg, store, "r", backend=backend, cache_dir=tmp_path / "c", sleep=lambda s: None)
    with pytest.raises(LLMUnavailable, match="high demand"):
        llm.call("t", [{"role": "user", "content": "hi"}], Out)
    assert calls == ["m1"] * 3 + ["m2"] * 3                              # 1 + 2 retries per model, then give up
    assert store.quota_used("m1", quota_day())["exhausted"] == 0         # not marked as used up for the day


def test_retry_hint():
    from kg.llm import retry_hint
    assert retry_hint(RuntimeError('"retryDelay": "31s"')) == 31
    assert retry_hint(RuntimeError("Please retry in 12.5s.")) == 12.5
    assert retry_hint(RuntimeError("boom")) is None


def test_options_resolution_and_litellm_kwargs(store, tmp_path, monkeypatch):
    import kg.llm as L
    cfg = cfg_with(["gemini/gemma-4-26b-a4b-it"], limit=5)
    cfg["llm"].update(model_overrides={"gemma": {"reasoning_effort": None, "thinking_level": "minimal"}},
                      step_options={"judge": {"thinking_level": "high"}}, reasoning_effort="low",
                      request_timeout_s=123)
    seen = []

    def fake_backend(model, messages, max_tokens, temperature, json_mode, reasoning_effort=None, thinking_level=None,
                     timeout=None):
        seen.append({"model": model, "reasoning_effort": reasoning_effort, "thinking_level": thinking_level,
                     "timeout": timeout, "json_mode": json_mode})
        return {"text": '{"x": 1}', "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(L, "litellm_backend", fake_backend)
    llm = LLM(cfg, store, "r", cache_dir=tmp_path / "c")
    assert llm.options_for("gemini/gemini-3.5-flash")["reasoning_effort"] == "low"     # override is per model
    llm.call("triage", [{"role": "user", "content": "a"}], Out)
    llm.call("judge", [{"role": "user", "content": "a"}], Out)
    assert seen[0] == {"model": "gemini/gemma-4-26b-a4b-it", "reasoning_effort": None, "thinking_level": "minimal",
                       "timeout": 123, "json_mode": True}
    assert seen[1]["thinking_level"] == "high"                                         # step option wins


def test_litellm_backend_sends_thinking_level(monkeypatch):
    import types

    import litellm

    import kg.llm as L
    got = {}

    def completion(**kw):
        got.update(kw)
        msg = types.SimpleNamespace(content='{"x": 1}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")],
                                     usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=4))

    monkeypatch.setattr(litellm, "completion", completion)
    out = L.litellm_backend("gemini/gemma-4-26b-a4b-it", [{"role": "user", "content": "x"}], 100, 0, True,
                            None, "minimal", 300)
    assert got["extra_body"] == {"generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}}}
    assert got["timeout"] == 300 and "reasoning_effort" not in got and got["response_format"]["type"] == "json_object"
    assert out["text"] == '{"x": 1}'
    got.clear()
    L.litellm_backend("gemini/gemini-3.5-flash", [{"role": "user", "content": "x"}], 100, 0, True, "low")
    assert "extra_body" not in got and got["reasoning_effort"] == "low"


def test_cache_key_includes_model_and_thinking(store, tmp_path):
    calls = []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        calls.append(model)
        return {"text": '{"x": 1}', "input_tokens": 1, "output_tokens": 1}

    def make(models, level):
        cfg = cfg_with(models, limit=50)
        cfg["llm"]["model_overrides"] = {"gemma": {"reasoning_effort": None, "thinking_level": level}}
        return LLM(cfg, store, "r", backend=backend, cache_dir=tmp_path / "c")

    msg = [{"role": "user", "content": "same prompt"}]
    make(["gemma-a"], "minimal").call("t", msg, Out)
    assert make(["gemma-a"], "minimal").call("t", msg, Out).cached                 # same setting: reused
    assert not make(["gemma-a"], "high").call("t", msg, Out).cached                # thinking changed: asked again
    assert not make(["gemma-b"], "minimal").call("t", msg, Out).cached             # other model: asked again
    assert make(["gemma-x", "gemma-b"], "minimal").call("t", msg, Out).cached      # any configured model's answer
    assert calls == ["gemma-a", "gemma-a", "gemma-b"]


def test_429_rate_limit_waits_and_does_not_use_up_the_day(store, tmp_path):
    from kg.llm import LLMUnavailable

    class RateLimitError(Exception):
        status_code = 429

    n, sleeps = {"c": 0}, []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        n["c"] += 1
        if n["c"] <= 2:
            raise RateLimitError('429 {"error": {"code": 429, "message": "You exceeded your current quota"}}')
        return {"text": '{"x": 7}', "input_tokens": 1, "output_tokens": 1}

    cfg = cfg_with(["m1"], limit=100)
    llm = LLM(cfg, store, "r", backend=backend, cache_dir=tmp_path / "c", sleep=sleeps.append)
    assert llm.call("t", [{"role": "user", "content": "hi"}], Out).parsed.x == 7
    assert sleeps == [65.0, 90.0]                                       # growing waits: the minute really empties
    q = store.quota_used("m1", quota_day())
    assert q["exhausted"] == 0 and q["requests"] == 1                   # refused attempts are not counted

    def always(model, messages, max_tokens, temperature, json_mode, purpose=None):
        raise RateLimitError('429 {"error": {"code": 429, "message": "You exceeded your current quota"}}')

    llm2 = LLM(cfg, store, "r", backend=always, cache_dir=tmp_path / "c2", sleep=lambda s: None)
    with pytest.raises(LLMUnavailable, match="NOT the daily quota"):
        llm2.call("t2", [{"role": "user", "content": "hi"}], Out)
    assert store.quota_used("m1", quota_day())["exhausted"] == 0


def test_pacing_by_tokens_per_minute(store, tmp_path):
    from kg.llm import LLMRequestRejected
    now, sleeps = {"t": 1000.0}, []

    def sleep(s):
        sleeps.append(s)
        now["t"] += s

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        now["t"] += 5                                                   # each answer takes 5 s
        return {"text": '{"x": 1}', "input_tokens": 1, "output_tokens": 1}

    cfg = cfg_with(["m1"], limit=100)
    cfg["llm"].update(model_overrides={"m1": {"tokens_per_minute": 1000}}, chars_per_token=1.0)
    llm = LLM(cfg, store, "r", backend=backend, cache_dir=tmp_path / "c", sleep=sleep, clock=lambda: now["t"])
    big = "x" * 600                                                     # ~600 tokens
    llm.call("a", [{"role": "user", "content": big}], Out)
    assert sleeps == []
    llm.call("b", [{"role": "user", "content": big + "y"}], Out)       # 600 + 600 > 1000 -> wait for the window
    assert len(sleeps) == 1 and 50 <= sleeps[0] <= 61
    with pytest.raises(LLMRequestRejected, match="tokens per minute"):
        llm.call("c", [{"role": "user", "content": "z" * 1500}], Out)  # can never fit


def test_retry_after_invalid_json_is_small(store, tmp_path):
    seen = []

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        seen.append(sum(len(m["content"]) for m in messages))
        return {"text": "x" * 20000 if len(seen) == 1 else '{"x": 1}', "input_tokens": 1, "output_tokens": 1}

    llm = LLM(cfg_with(["m1"], limit=5), store, "r", backend=backend, cache_dir=tmp_path / "c")
    assert llm.call("t", [{"role": "user", "content": "q" * 1000}], Out).parsed.x == 1
    assert seen[1] - seen[0] < 1500                                     # not the whole 20,000-character answer


def test_pacing_uses_the_providers_token_count(store, tmp_path):
    now, sleeps = {"t": 0.0}, []

    def sleep(s):
        sleeps.append(s)
        now["t"] += s

    def backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
        return {"text": '{"x": 1}', "input_tokens": 900, "output_tokens": 1}   # far more than our estimate

    cfg = cfg_with(["m1"], limit=100)
    cfg["llm"].update(model_overrides={"m1": {"tokens_per_minute": 1000}}, chars_per_token=1.0)
    llm = LLM(cfg, store, "r", backend=backend, cache_dir=tmp_path / "c", sleep=sleep, clock=lambda: now["t"])
    llm.call("a", [{"role": "user", "content": "x" * 100}], Out)
    llm.call("b", [{"role": "user", "content": "y" * 200}], Out)       # 900 (real) + 200 > 1000 -> waits
    assert len(sleeps) == 1
