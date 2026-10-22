"""The unattended overnight check of many documents (``check-all``): model errors never stop the run."""
import pytest
from fake_llm import FakeGemini

from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM
from kg.runlog import Run
from kg.services import accounts
from kg.services.demo import seed_demo
from kg.services.jobs import check_all


class Flaky:
    """The fake model, failing on purpose: ``plan`` = {purpose: [exception, ...]} raised by the first calls."""

    def __init__(self, fake, plan):
        self.fake, self.plan = fake, {k: list(v) for k, v in plan.items()}

    def __call__(self, **kw):
        todo = self.plan.get(kw.get("purpose"))
        if todo:
            raise todo.pop(0)
        return self.fake(**kw)


class HTTP(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.status_code = code


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    with Run(store, cfg.path("runs"), "t-all") as run:
        extract_obligations_v2(cfg, store, run, LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c"))
    docs = seed_demo(cfg, store)["documents"]
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    return cfg, store, docs, owner, fake


def make(backend):
    def f(cfg, store, rid):
        llm = LLM(cfg, store, rid, backend=backend)
        llm._sleep = lambda s: None
        return llm
    return f


def test_check_all_survives_errors(env):
    cfg, store, docs, owner, fake = env
    pol = next(d for d in docs if "POL-RM-014" in d)
    others = [d for d in docs if d != pol][:2]
    flaky = Flaky(fake, {"judge": [HTTP(503, "503 UNAVAILABLE: high demand")] * 6,     # -> unavailable, wait, again
                         "triage": [HTTP(400, "400 INVALID_ARGUMENT: bad")] * 1000})  # -> batches skipped
    slept = []
    out = check_all(cfg, store, owner, others, make_drafts=True, auto_confirm=True, make_llm=make(flaky),
                    sleep=slept.append, log=lambda m: None)
    assert set(out["results"].values()) == {"done"}
    assert 600 in slept                                                    # it waited instead of stopping
    for v in others:
        assert store.q1("SELECT status FROM document_versions WHERE id=?", (v,))["status"] == "draft"
        assert store.q1("SELECT confirmed_at FROM doc_profiles WHERE doc_version_id=?", (v,))["confirmed_at"]
        assert store.q1("SELECT status FROM check_jobs WHERE version_id=?", (v,))["status"] == "done"
    text = (cfg.path("runs") / "check-all-report.md").read_text(encoding="utf-8")
    assert "skipped batches" in text and all(v in text for v in others)
    # the next run asks the skipped batch again (and everything else is reused)
    n = store.q1("SELECT COUNT(*) n FROM step_outputs WHERE valid=0 AND errors LIKE '%rejected:%'")["n"]
    assert n >= 1
    assert store.q1("SELECT COUNT(*) n FROM triage WHERE source='rule' AND llm_output_id IS NULL")["n"] > 0
    store.x("UPDATE check_jobs SET status='failed'")
    check_all(cfg, store, owner, others, make_llm=make(fake), sleep=slept.append, log=lambda m: None)
    assert store.q1("SELECT COUNT(*) n FROM step_outputs WHERE valid=0 AND errors LIKE '%rejected:%'")["n"] == 0
    assert store.q1("SELECT COUNT(*) n FROM triage WHERE source='rule' AND llm_output_id IS NULL")["n"] == 0


def test_check_all_gives_up_on_one_document_and_goes_on(env):
    cfg, store, docs, owner, fake = env
    a, b = [d for d in docs if "POL-RM-014" not in d][:2]
    # the profile of the first (smaller) document never works; the second must still be checked
    first = min((a, b), key=lambda v: __import__("kg.services.jobs", fromlist=["_size"])._size(store, v))
    flaky = Flaky(fake, {"doc_profile": [HTTP(503, "503 UNAVAILABLE")] * 500})
    out = check_all(cfg, store, owner, [a, b], auto_confirm=True, make_llm=make(flaky), sleep=lambda s: None,
                    log=lambda m: None, waits={"unavailable_max": 1})
    assert out["results"][first].startswith("stopped")
    assert sum(1 for s in out["results"].values() if s == "done") == 0 or out["results"][({a, b} - {first}).pop()]


def test_profile_needs_auto_confirm(env):
    cfg, store, docs, owner, fake = env
    v = next(d for d in docs if "POL-RM-014" not in d)
    out = check_all(cfg, store, owner, [v], make_llm=make(fake), sleep=lambda s: None, log=lambda m: None)
    assert out["results"][v].startswith("stopped")


def test_thinking_request_that_hangs_is_asked_without_thinking(kg, monkeypatch):
    import kg.llm as L
    cfg, store = kg
    seen = []

    def fake_litellm(model, messages, max_tokens, temperature, json_mode, reasoning_effort=None,
                     thinking_level=None, timeout_s=None):
        seen.append(thinking_level)
        if len(seen) == 1:
            raise L.litellm_timeout_for_test("litellm.Timeout: Connection timed out after None seconds.")
        return {"text": '{"ok": 1}', "input_tokens": 5, "output_tokens": 2}

    monkeypatch.setattr(L, "litellm_backend", fake_litellm)
    with Run(store, cfg.path("runs"), "t-calm") as run:
        llm = LLM(cfg, store, run.id)
        llm._sleep = lambda s: None
        llm.models_for = lambda purpose: ["gemini/gemma-4-26b-a4b-it"]
        llm.step_options = {"triage": {"thinking_level": "high"}}
        llm._request("triage", [{"role": "user", "content": "x"}], 100, True, "k")
    assert seen == ["high", "minimal"]
    assert store.q1("SELECT 1 FROM llm_calls WHERE error LIKE 'thinking_off%'")
