"""Phase 8: evaluation suites on the real corpus with the fake model."""
import csv

import pytest
from fake_llm import FakeGemini

from kg.company import profile as prof
from kg.eval import extraction as X
from kg.eval.gold_v2 import OBL_COLS, gold_paths, load_csv, make_templates
from kg.eval.runner import run_suite
from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM
from kg.mapping.pipeline import map_document
from kg.mapping.proposals import propose_document
from kg.runlog import Run
from kg.services import accounts, tasks as T
from kg.services.demo import seed_demo


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    run = Run(store, cfg.path("runs"), "t-p8")
    run.__enter__()
    llm = LLM(cfg, store, run.id, backend=FakeGemini(), cache_dir=tmp_path / "c")
    extract_obligations_v2(cfg, store, run, llm)
    docs = seed_demo(cfg, store)["documents"]
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    for v in docs:
        prof.build_profile(cfg, store, run, llm, v)
        prof.confirm_profile(store, owner, v)
        map_document(cfg, store, run, llm, v, now="2025-01-01")
        propose_document(cfg, store, run, llm, v)
        T.sync_tasks(cfg, store, run, v)
    yield cfg, store, run, docs
    run.__exit__(None, None, None)


def test_templates_prefilled_and_not_overwritten(env):
    cfg, store, run, docs = env
    out = make_templates(cfg, store)
    obl, diff = gold_paths(cfg)
    rows = load_csv(obl)
    assert set(out["written"]) == {"obligations_v2", "amend_diff_v1"} and 15 <= len(rows) <= 21
    assert list(rows[0]) == OBL_COLS and all(r["unit_text"] and not r["statement"] for r in rows)
    assert len({(r["unit_path"], r["unit_date"]) for r in rows}) == len(rows)
    assert 1 <= len(load_csv(diff)) <= 6
    obl.write_text(obl.read_text(encoding="utf-8-sig") + "mine\n", encoding="utf-8")
    assert make_templates(cfg, store)["written"] == []                       # never overwritten


def test_extraction_suite_with_labelled_rows(env):
    cfg, store, run, docs = env
    res = run_suite(cfg, store, run, "extraction")
    assert res["metrics"]["obligations"]["status"] == "no gold labels yet"      # no gold yet: reported, not an error
    ob = next(n for n in store.nodes("Obligation") if n["valid_to"] is None)
    p = ob["props"]
    make_templates(cfg, store)
    obl, _ = gold_paths(cfg)
    rows = load_csv(obl)
    rows.append({c: "" for c in OBL_COLS} | {"unit_path": p["unit"], "statement": p["statement"],
                                              "deontic": p["deontic"], "severity": p["severity"]})
    with open(obl, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OBL_COLS)
        w.writeheader()
        w.writerows(rows)
    m = run_suite(cfg, store, run, "extraction")["metrics"]["obligations"]
    assert m["labelled_units"] == 1 and m["recall"] == 1.0 and m["attribute_accuracy"]["deontic"] == 1.0
    assert 0 < m["precision"] <= 1.0


def test_pair_up_is_one_to_one():
    pairs = X.pair_up(["ბანკმა უნდა გაატაროს ვალიდაცია ყოველწლიურად", "სხვა რამ"],
                      ["ბანკმა უნდა გაატაროს ვალიდაცია ყოველწლიურად მოდელებზე"])
    assert [(i, j) for i, j, _ in pairs] == [(0, 0)]


def test_compliance_v2_and_triage_and_proposals(env):
    cfg, store, run, docs = env
    c = run_suite(cfg, store, run, "compliance")
    m = c["metrics"]
    assert m["source"] == "v2 coverage/edges" and m["findings_total"] > 0 and m["gold_items"]["A"] > 0
    assert m["recall_A"] is not None and 0 <= m["recall_A"] <= 1
    one = run_suite(cfg, store, run, "compliance", version=docs[0])["metrics"]
    assert one["findings_total"] <= m["findings_total"]
    t = run_suite(cfg, store, run, "triage")["metrics"]
    assert t["triage_recall_A"] is not None and sum(t["relevance_counts"].values()) > 0
    p = run_suite(cfg, store, run, "proposals")["metrics"]
    assert p["sample"] > 0 and (cfg.path("eval_gold") / "proposals_review.csv").exists() and p["reviewed"] == 0
    assert run_suite(cfg, store, run, "ops")["metrics"]["llm_calls"] > 0
    with pytest.raises(ValueError):
        run_suite(cfg, store, run, "nope")
