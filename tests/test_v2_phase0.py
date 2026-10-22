"""Phase 0: tables / migration, stored step outputs, citation checks, per-step model switching."""
import json
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

from kg.citations import Citation, norm_text, quote_in_text, verify_citations
from kg.config import REPO_ROOT, Config
from kg.llm import LLM, BudgetExhausted
from kg.steps import StepRejected, input_hash, load_prompt, run_step
from kg.store.sqlite_nx import SQLiteStore


class Out(BaseModel):
    items: list[str] = []


def _cfg(tmp_path, **llm_over):
    base = {"llm": {"models": ["m/a", "m/b"], "daily_request_limit": 20, "temperature": 0,
                    "max_output_tokens": 1000, "retries_on_invalid_json": 1},
            "paths": {"llm_cache": str(tmp_path / "cache")}}
    base["llm"].update(llm_over)
    return Config(base)


class Counter:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, model, messages, max_tokens, temperature, json_mode, purpose=None):
        self.calls.append((model, purpose, json_mode))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {"text": json.dumps(a), "input_tokens": 1, "output_tokens": 1}


def test_all_v2_tables_exist(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")
    have = {r["name"] for r in store.q("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("companies", "users", "company_roles", "user_roles", "documents", "document_versions", "doc_profiles",
              "triage", "locations", "coverage", "proposals", "tasks", "task_events", "user_decisions",
              "step_outputs", "nodes", "edges", "findings"):
        assert t in have, t


def test_migrates_the_current_database_in_place(tmp_path):
    src = REPO_ROOT / "data" / "kg" / "kg.sqlite"
    if not src.exists():
        pytest.skip("no local database")
    dst = tmp_path / "copy.sqlite"
    shutil.copy(src, dst)
    first = SQLiteStore(dst)
    before = first.nodes("Obligation")
    n_steps = first.q("SELECT COUNT(*) n FROM step_outputs")[0]["n"]
    store = SQLiteStore(dst)                      # opening again must be idempotent
    assert len(store.nodes("Obligation")) == len(before)
    assert store.q("SELECT COUNT(*) n FROM step_outputs")[0]["n"] == n_steps


def test_second_identical_step_makes_no_call(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")
    be = Counter([{"items": ["x"]}])
    llm = LLM(_cfg(tmp_path), store, backend=be, cache_dir=tmp_path / "c")
    msgs = [{"role": "user", "content": "hi"}]
    r1 = run_step(store, llm, "triage", "doc1", {"a": 1}, msgs, Out, "t-v1")
    r2 = run_step(store, llm, "triage", "doc1", {"a": 1}, msgs, Out, "t-v1")
    assert len(be.calls) == 1 and not r1.reused and r2.reused
    assert r2.output.items == ["x"] and r1.output_id == r2.output_id
    # other input / other prompt version -> new call
    run_step(store, llm, "triage", "doc1", {"a": 2}, [{"role": "user", "content": "other"}], Out, "t-v1")
    run_step(store, llm, "triage", "doc1", {"a": 1}, msgs, Out, "t-v2")
    assert len(be.calls) == 3
    # a fresh LLM object (no file cache hit possible with another cache dir) still reuses the stored output
    llm2 = LLM(_cfg(tmp_path), store, backend=be, cache_dir=tmp_path / "c2")
    r3 = run_step(store, llm2, "triage", "doc1", {"a": 1}, msgs, Out, "t-v1")
    assert r3.reused and len(be.calls) == 3


def test_validator_retry_and_cleaning(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")
    be = Counter([{"items": ["bad", "ok"]}, {"items": ["ok"]}])
    llm = LLM(_cfg(tmp_path), store, backend=be, cache_dir=tmp_path / "c")

    def validate(p):
        errs = [f"{i} is bad" for i in p.items if i == "bad"]
        p.items = [i for i in p.items if i != "bad"]
        return errs

    r = run_step(store, llm, "judge", "s", {"q": 1}, [{"role": "user", "content": "q"}], Out, "j1", validate=validate)
    assert len(be.calls) == 2 and r.output.items == ["ok"] and r.errors == []
    row = store.q1("SELECT * FROM step_outputs WHERE id=?", (r.output_id,))
    assert row["valid"] == 1 and json.loads(row["output"]) == {"items": ["ok"]}


def test_invalid_json_is_stored_and_not_retried_every_run(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")

    def bad(model, messages, max_tokens, temperature, json_mode, purpose=None):
        return {"text": "not json", "input_tokens": 1, "output_tokens": 1}

    llm = LLM(_cfg(tmp_path), store, backend=bad, cache_dir=tmp_path / "c")
    m = [{"role": "user", "content": "q"}]
    r = run_step(store, llm, "judge", "s", {"q": 1}, m, Out, "j1")
    assert not r.valid and r.output is None
    n = len(store.llm_calls())
    r2 = run_step(store, llm, "judge", "s", {"q": 1}, m, Out, "j1")
    assert r2.reused and not r2.valid and len(store.llm_calls()) == n
    r3 = run_step(store, llm, "judge", "s", {"q": 1}, m, Out, "j1", retry_invalid=True)
    assert len(store.llm_calls()) > n


def test_prompt_header_and_version(tmp_path):
    p = tmp_path / "p.md"
    p.write_text("<!-- version: demo-v3 -->\nHello", encoding="utf-8")
    assert load_prompt(p) == ("Hello", "demo-v3")
    p.write_text("no header", encoding="utf-8")
    assert load_prompt(p)[1] == "v0"


def test_prompt_version_changes_cache_key(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")
    be = Counter([{"items": []}])
    llm = LLM(_cfg(tmp_path), store, backend=be, cache_dir=tmp_path / "c")
    m = [{"role": "user", "content": "q"}]
    llm.call("x", m, Out, prompt_version="a")
    llm.call("x", m, Out, prompt_version="a")
    llm.call("x", m, Out, prompt_version="b")
    assert len(be.calls) == 2


def test_single_model_step_models_and_no_limit(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")
    be = Counter([{"items": []}])
    cfg = _cfg(tmp_path, single_model="m/only", step_models={"judge": ["m/x"]}, daily_request_limit=None)
    llm = LLM(cfg, store, backend=be, cache_dir=tmp_path / "c")
    assert llm.models == ["m/only"] and llm.step_models == {} and llm.remaining_budget() is None
    llm.call("judge", [{"role": "user", "content": "1"}], Out)
    assert be.calls[0][0] == "m/only"
    # per-step lists (no single model)
    be2 = Counter([{"items": []}])
    llm2 = LLM(_cfg(tmp_path, step_models={"judge": ["m/x", "m/y"]}), store, backend=be2, cache_dir=tmp_path / "c3")
    llm2.call("judge", [{"role": "user", "content": "2"}], Out)
    llm2.call("triage", [{"role": "user", "content": "3"}], Out)
    assert [c[0] for c in be2.calls] == ["m/x", "m/a"]


def test_model_overrides_and_request_cap(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")
    be = Counter([{"items": []}])
    cfg = _cfg(tmp_path, single_model="vertex_ai/gemma-4", model_overrides={"gemma": {"json_mode": False}},
               max_requests_per_run=2)
    llm = LLM(cfg, store, backend=be, cache_dir=tmp_path / "c")
    llm.call("judge", [{"role": "user", "content": "1"}], Out)
    assert be.calls[0][2] is False                       # json_mode off for gemma, on for others
    llm.call("judge", [{"role": "user", "content": "2"}], Out)
    with pytest.raises(BudgetExhausted):
        llm.call("judge", [{"role": "user", "content": "3"}], Out)


def test_daily_limit_still_rotates(tmp_path):
    store = SQLiteStore(tmp_path / "k.sqlite")
    be = Counter([{"items": []}])
    llm = LLM(_cfg(tmp_path, daily_request_limit=1), store, backend=be, cache_dir=tmp_path / "c")
    for i in range(2):
        llm.call("x", [{"role": "user", "content": str(i)}], Out)
    assert [c[0] for c in be.calls] == ["m/a", "m/b"]
    with pytest.raises(BudgetExhausted):
        llm.call("x", [{"role": "user", "content": "z"}], Out)


# ---------------------------------------------------------------------------------------------- citations
def test_quote_normalisation():
    assert quote_in_text("ვალიდაცია  უნდა\nჩატარდეს", "ყოველწლიურად ვალიდაცია უნდა ჩატარდეს.")
    assert quote_in_text("„ვალიდაცია“", 'ვალიდაცია')
    assert quote_in_text("ერთი ... სამი", "ერთი ორი სამი")
    assert not quote_in_text("სამი ... ერთი", "ერთი ორი სამი")
    assert not quote_in_text("", "abc")
    assert norm_text("4¹ პუნქტი") == "4^1 პუნქტი"


def test_verify_citations_on_real_provisions(built):
    cfg, store = built
    pv = next(n for n in store.nodes("ProvisionVersion", act="4964423", path="art8/p1"))
    quote = " ".join(pv["props"]["text"].split()[:6])
    cites = [Citation(kind="provision", id=pv["id"], quote=quote),
             Citation(kind="provision", id=pv["id"], quote="ეს ტექსტი არ არსებობს ნორმაში"),
             Citation(kind="provision", id="pv:4964423/art99@2021-01-01", quote="x"),
             Citation(kind="clause", id=pv["id"], quote=None)]
    good, errs = verify_citations(store, cites)
    assert len(good) == 1 and len(errs) == 3
    assert good[0].url and good[0].url.startswith("https://matsne") and "›" in good[0].label
    assert any("unknown id" in e for e in errs) and any("does not match" in e for e in errs)
    # Provision node (no version): any version may be quoted
    good2, _ = verify_citations(store, [Citation(kind="provision", id="prov:4964423/art8/p1", quote=quote)])
    assert len(good2) == 1
    # allowed_ids guard: ids that were not part of the input are rejected
    _, errs3 = verify_citations(store, cites[:1], allowed_ids={"other"})
    assert errs3


def test_input_hash_is_stable():
    assert input_hash({"b": 1, "a": [1, 2]}) == input_hash({"a": [1, 2], "b": 1})
    assert input_hash({"a": 1}) != input_hash({"a": 2})
