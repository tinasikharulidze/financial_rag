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
            raise RateLimit("429 quota exceeded")
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
