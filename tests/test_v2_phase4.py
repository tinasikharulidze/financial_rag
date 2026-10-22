"""Phase 4: locate, judge (whole-document and candidate mode), multi-edge coverage, causes, user precedence,
the pipeline and hybrid search - with the fake model on the real corpus."""
import pytest
from fake_llm import FakeGemini

from kg.company import profile as prof
from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM, BudgetExhausted
from kg.mapping import judging, locate
from kg.mapping.docview import load_doc
from kg.mapping.pipeline import MappingError, map_document, plan_map, recheck_obligation
from kg.mapping.search import candidates_for, doc_index, obligation_queries
from kg.runlog import Run
from kg.services import accounts
from kg.services.demo import seed_demo

NOW = "2025-01-01"


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    run = Run(store, cfg.path("runs"), "t-phase4")
    run.__enter__()
    llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
    extract_obligations_v2(cfg, store, run, llm)
    out = seed_demo(cfg, store)
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    yield cfg, store, run, llm, fake, out["documents"], owner
    run.__exit__(None, None, None)


def prepared(env, key="POL-RM-014"):
    cfg, store, run, llm, fake, docs, owner = env
    v = next(d for d in docs if key in d)
    prof.build_profile(cfg, store, run, llm, v)
    prof.confirm_profile(store, owner, v, domains=["model_risk"])
    return v


def test_map_requires_a_confirmed_profile(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = next(d for d in docs if "POL-RM-014" in d)
    with pytest.raises(MappingError):
        map_document(cfg, store, run, llm, v, now=NOW)
    prof.build_profile(cfg, store, run, llm, v)
    with pytest.raises(MappingError):
        map_document(cfg, store, run, llm, v, now=NOW)


def test_full_mapping_whole_document_mode(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    plan = plan_map(cfg, store, run, v, now=NOW)
    assert plan["mode"] == "whole" and plan["requests_total"] >= 3
    out = map_document(cfg, store, run, llm, v, now=NOW)
    assert out["judge"]["mode"] == "whole" and out["continuing"] > 0
    used = fake.calls["triage"] + fake.calls["locate"] + fake.calls["judge"]
    assert used <= plan["requests_total"]
    rows = store.q("SELECT * FROM coverage WHERE doc_version_id=?", (v,))
    assert len(rows) == out["continuing"]
    verdicts = {r["verdict"] for r in rows}
    assert verdicts <= {"full", "partial", "contradicts", "none", "undecided"} and len(verdicts) >= 2
    assert all(r["cause"] in ("pre_existing", "amendment") for r in rows)
    # edges: role, quote verified against the clause, link to the stored output
    es = [e for e in store.edges("COVERED_BY") if e["props"].get("doc_version") == v]
    assert es
    view = load_doc(store, v)
    for e in es:
        assert e["dst"] in view.by_id and e["props"]["role"] in ("full", "partial", "contradicts")
        assert e["props"]["source"] == "llm" and e["props"]["output_id"]
    assert any(len([x for x in es if x["src"] == oid]) > 1 for oid in {e["src"] for e in es}), "several edges"
    # every full verdict has evidence; citations were verified by code
    for r in rows:
        if r["verdict"] == "full":
            assert any(e["src"] == r["obligation_id"] for e in es)
    cited = [r for r in rows if r["citations"] != "[]"]
    assert cited and "label" in cited[0]["citations"]


def test_second_run_makes_no_requests(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    map_document(cfg, store, run, llm, v, now=NOW)
    n = sum(fake.calls.values())
    out = map_document(cfg, store, run, llm, v, now=NOW)
    assert sum(fake.calls.values()) == n and out["judge"]["batches"] == 0
    # forcing a re-judge reuses the stored answers (same inputs, same prompt): still no request
    map_document(cfg, store, run, llm, v, now=NOW, rejudge=True)
    assert sum(fake.calls.values()) == n


def test_candidate_mode_and_search(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    cfg["mapping"]["whole_doc_max_tokens"] = 100          # force candidate mode
    out = map_document(cfg, store, run, llm, v, now=NOW)
    assert out["judge"]["mode"] == "candidates"
    rows = store.q("SELECT * FROM coverage WHERE doc_version_id=?", (v,))
    assert rows and {r["verdict"] for r in rows} != {"undecided"}
    # the prompt contains candidates, not the whole document
    assert "DOCUMENT:" not in fake.last_user["judge"] and "candidates" in fake.last_user["judge"]
    view = load_doc(store, v)
    idx = doc_index(cfg, view)
    ob = next(o for o in store.nodes("Obligation") if o["props"].get("schema") == "v2" and "ვალიდაც" in o["props"]["statement"])
    qs = obligation_queries(ob["props"], "ვალიდაცია")
    assert len(qs) == 2
    c = candidates_for(cfg, idx, view, ob["props"], "ვალიდაცია", [view.sections[3]["section_no"]])
    assert 0 < len(c) <= 20 and len({x["id"] for x in c}) == len(c)
    assert all(x["id"] in view.by_id for x in c)


def test_locate_validates_sections(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    orig = fake.locate
    fake.handlers["locate"] = lambda inp: {"results": [
        {**r, "sections": r["sections"] + [{"section_no": "999", "reason": "x"}] * 2} for r in orig(inp)["results"]]}
    map_document(cfg, store, run, llm, v, now=NOW)
    view = load_doc(store, v)
    locs = locate.located_sections(store, v)
    assert locs and all(set(s) <= set(view.section_nos()) for s in locs.values())
    assert all(len(s) <= 5 for s in locs.values())


def test_judge_rules_are_enforced_by_code(env):
    cfg, store, run, llm, fake, docs, owner = env
    cfg["mapping"]["judge_batch"] = 8                               # the four obligations in ONE request
    v = prepared(env)
    view = load_doc(store, v)
    cl = [u["id"] for u in view.mapped][:3]
    obls = [o for o in store.nodes("Obligation", as_of=NOW) if o["props"].get("schema") == "v2"][:4]
    a, b, c, d = [o["id"] for o in obls]
    fake.judge_force = {
        a: {"verdict": "full", "edges": [(cl[0], "partial"), (cl[1], "contradicts")]},       # contradicts wins
        b: {"verdict": "none", "edges": [(cl[0], "partial")]},                               # none + edges -> partial
        c: {"verdict": "full", "edges": []},                                                 # full without evidence
        d: {"verdict": "partial", "edges": [(cl[2], "full"), ("cl:invented", "full")]},      # unknown clause id
    }
    res = judging.judge_document(cfg, store, run, llm, v, view, {}, obls, {}, "2021-04-01")
    cov = {r["obligation_id"]: r for r in store.q("SELECT * FROM coverage WHERE doc_version_id=?", (v,))}
    assert cov[a]["verdict"] == "contradicts" and cov[b]["verdict"] == "partial"
    assert cov[c]["verdict"] == "undecided" and cov[c]["invalid"] == 1
    assert cov[d]["verdict"] == "undecided"
    assert fake.calls["judge"] == 2 and res["undecided"] == 2          # one retry with the problems shown to the model


def test_user_edges_and_rows_win(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    view = load_doc(store, v)
    ob = [o for o in store.nodes("Obligation", as_of=NOW) if o["props"].get("schema") == "v2"][0]
    oid = ob["id"]
    clause = [u["id"] for u in view.mapped][5]
    fake.judge_force = {oid: {"verdict": "none", "edges": []}}
    store.upsert_edges([(oid, clause, "COVERED_BY", {"role": "full", "coverage": "full", "source": "user",
                                                     "doc_version": v}, None, None, f"{oid}|COVERED_BY|{clause}|user")])
    judging.judge_document(cfg, store, run, llm, v, view, {}, [ob], {}, "2021-04-01")
    row = store.q1("SELECT * FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v, oid))
    assert row["verdict"] == "full"                                # user full edge beats the model's 'none'
    assert "fixed_edges" in fake.last_user["judge"] and clause in fake.last_user["judge"]
    # the user's edge survives a re-judge, and a user-decided coverage row is not re-judged at all
    judging.judge_document(cfg, store, run, llm, v, view, {}, [ob], {}, "2021-04-01", force=True)
    assert any(e["props"]["source"] == "user" and e["dst"] == clause for e in store.edges("COVERED_BY"))
    store.x("UPDATE coverage SET source='user', verdict='full' WHERE doc_version_id=? AND obligation_id=?", (v, oid))
    n = fake.calls["judge"]
    st = judging.judge_document(cfg, store, run, llm, v, view, {}, [ob], {}, "2021-04-01", force=True)
    assert fake.calls["judge"] == n and st["skipped_user"] == 1


def test_cause_is_amendment_for_obligations_after_the_document_date(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    map_document(cfg, store, run, llm, v, now=NOW)
    rows = store.q("SELECT c.*, n.valid_from FROM coverage c JOIN nodes n ON n.id=c.obligation_id "
                   "WHERE c.doc_version_id=?", (v,))
    v_doc = "2021-04-01"
    for r in rows:
        assert r["cause"] == ("amendment" if r["valid_from"] > v_doc else "pre_existing")
        if r["cause"] == "amendment":
            assert r["amendment_id"] is None or r["amendment_id"].startswith("amend")
    assert {"pre_existing"} <= {r["cause"] for r in rows}


def test_other_documents_and_pruning(env):
    cfg, store, run, llm, fake, docs, owner = env
    v2 = prepared(env, "REG-RM-003")
    out = map_document(cfg, store, run, llm, v2, now=NOW)
    assert out["continuing"] >= 0 and out["triage"]["irrelevant"] > 0
    # irrelevant obligations have a triage row but no coverage row
    irr = [r["obligation_id"] for r in store.q("SELECT obligation_id FROM triage WHERE doc_version_id=? "
                                               "AND relevance='irrelevant'", (v2,))]
    assert irr and not store.q("SELECT 1 FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v2, irr[0]))
    # a later 'now' before the regulation drops everything
    map_document(cfg, store, run, llm, v2, now="2000-01-01")
    assert not store.q("SELECT 1 FROM coverage WHERE doc_version_id=?", (v2,))
    assert not [e for e in store.edges("COVERED_BY") if e["props"].get("doc_version") == v2]


def test_budget_exhaustion_midway_is_resumable(env, tmp_path):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    cfg["llm"]["daily_request_limit"] = 2
    cfg["llm"]["models"] = ["m/x"]
    llm2 = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c2")
    base = sum(fake.calls.values())
    with pytest.raises(BudgetExhausted):
        map_document(cfg, store, run, llm2, v, now=NOW)
    assert sum(fake.calls.values()) - base == 2
    cfg["llm"]["daily_request_limit"] = None
    llm3 = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c3")
    out = map_document(cfg, store, run, llm3, v, now=NOW)
    assert store.q1("SELECT COUNT(*) n FROM coverage WHERE doc_version_id=?", (v,))["n"] == out["continuing"]


def test_recheck_one_obligation(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    map_document(cfg, store, run, llm, v, now=NOW)
    oid = store.q1("SELECT obligation_id FROM coverage WHERE doc_version_id=?", (v,))["obligation_id"]
    n = fake.calls["judge"]
    fake.judge_force = {oid: {"verdict": "none", "edges": []}}
    recheck_obligation(cfg, store, run, llm, v, oid)
    assert fake.calls["judge"] == n + 1
    assert store.q1("SELECT verdict FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v, oid))["verdict"] == "none"


def test_contradicts_verdict_turns_its_partial_clauses_into_contradicts(env):
    cfg, store, run, llm, fake, docs, owner = env
    v = prepared(env)
    view = load_doc(store, v)
    cl = [u["id"] for u in view.mapped][:2]
    ob = [o for o in store.nodes("Obligation", as_of=NOW) if o["props"].get("schema") == "v2"][0]
    fake.judge_force = {ob["id"]: {"verdict": "contradicts", "edges": [(cl[0], "partial")]}}
    judging.judge_document(cfg, store, run, llm, v, view, {}, [ob], {}, "2021-04-01")
    row = store.q1("SELECT verdict FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v, ob["id"]))
    roles = [e["props"]["role"] for e in store.edges("COVERED_BY")
             if e["src"] == ob["id"] and e["props"].get("doc_version") == v]
    assert row["verdict"] == "contradicts" and roles == ["contradicts"]
