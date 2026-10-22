"""Background checks: the job row, the launcher, the check process (fake model) and the CLI command."""
import os

import pytest
from fake_llm import FakeGemini

from kg.company import profile as prof
from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM, BudgetExhausted
from kg.runlog import Run
from kg.services import accounts, jobs
from kg.services.demo import seed_demo
from kg.services.permissions import PermissionDenied


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    run = Run(store, cfg.path("runs"), "t-jobs")
    run.__enter__()
    llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
    extract_obligations_v2(cfg, store, run, llm)
    docs = seed_demo(cfg, store)["documents"]
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    v = next(d for d in docs if "POL-RM-014" in d)
    prof.build_profile(cfg, store, run, llm, v)
    prof.confirm_profile(store, owner, v, domains=["model_risk"])
    yield cfg, store, run, llm, fake, v, owner
    run.__exit__(None, None, None)


def test_launch_writes_a_queued_job_and_is_idempotent(env):
    cfg, store, run, llm, fake, v, owner = env
    calls = []

    def spawn(cmd, log_path):
        calls.append(cmd)
        return os.getpid()                         # a live process: the job counts as running

    j = jobs.launch(cfg, store, owner, v, spawn=spawn)
    assert j["status"] == "queued" and j["pid"] == os.getpid() and jobs.is_running(store, v)
    assert calls[0][-4:] == ["check", v, "--as", "bank@demo.ge"] and calls[0][1:3] == ["-m", "kg.cli"]
    jobs.launch(cfg, store, owner, v, spawn=spawn)
    assert len(calls) == 1                         # already running: not started twice
    reg = accounts.authenticate(store, "nbg@demo.ge", "demo1234")
    with pytest.raises(PermissionDenied):
        jobs.launch(cfg, store, reg, v, spawn=spawn)


def test_a_vanished_process_marks_the_job_failed(env):
    cfg, store, run, llm, fake, v, owner = env
    jobs.launch(cfg, store, owner, v, spawn=lambda cmd, log: 999_999_9)
    j = jobs.get_job(store, v)
    assert j["status"] == "failed" and "ended unexpectedly" in j["message"] and not jobs.is_running(store, v)


def test_run_check_end_to_end_and_progress(env):
    cfg, store, run, llm, fake, v, owner = env
    out = jobs.run_check(cfg, store, run, llm, v, owner)
    j = jobs.get_job(store, v)
    assert j["status"] == "done" and j["finished_at"] and out["tasks"]["created"] > 0
    p = jobs.progress(store, v)
    assert p["triage"] > 0 and p["judged"] > 0 and p["tasks"] == out["tasks"]["created"]


def test_run_check_records_a_stop(env, tmp_path):
    cfg, store, run, llm, fake, v, owner = env

    def broke(*a, **k):
        raise BudgetExhausted("no model left")

    llm2 = LLM(cfg, store, run.id, backend=broke, cache_dir=tmp_path / "c2")
    with pytest.raises(BudgetExhausted):
        jobs.run_check(cfg, store, run, llm2, v, owner)
    j = jobs.get_job(store, v)
    assert j["status"] == "budget" and "no model left" in j["message"]
    jobs.run_check(cfg, store, run, llm, v, owner)            # repeating continues and finishes
    assert jobs.get_job(store, v)["status"] == "done"
