"""Phases 1 and 3 on the real corpus (no LLM) plus the mapping/replay/eval flow with a mock LLM."""
from conftest import mock_backend
from kg.eval import consolidation
from kg.llm import LLM
from kg.runlog import Run


def test_consolidation_assertions_pass(built):
    cfg, store = built
    failed = [a for a in consolidation.assertions(cfg, store) if not a["passed"]]
    assert not failed, failed


def test_amendment_parse_stats(built):
    cfg, _ = built
    st = consolidation.parse_stats(cfg)
    assert st["instructions"] >= 100 and st["share_unresolved"] < 0.10


def test_versions_and_texts(built):
    cfg, store = built
    vs = sorted(store.nodes("ProvisionVersion", act="4964423", path="art4/p3"), key=lambda n: n["valid_from"])
    assert len(vs) == 3 and len({v["props"]["text"] for v in vs}) == 3
    a91 = store.nodes("ProvisionVersion", act="4964423", path="art9_1")
    assert a91 and all(v["props"]["status"] == "active" for v in a91)
    assert store.nodes("InternalDoc") and len(store.nodes("InternalDoc")) == 7
    assert any(e["dst"].startswith("prov:") for e in store.edges("REFERS_TO"))


def test_company_docs(built):
    _, store = built
    docs = {n["props"]["doc_code"]: n["props"] for n in store.nodes("InternalDoc")}
    assert docs["POL-RM-014"]["doc_type"] == "policy" and docs["POL-RM-014"]["doc_date"] == "2021-03-15"
    assert docs["REG-RM-003"]["doc_type"] == "inventory"
    clauses = [n for n in store.nodes("Clause") if n["props"]["doc"].startswith("doc:POL-RM-014")]
    assert any(c["props"].get("section_no") == "5.2.1" for c in clauses)
    tables = [n for n in store.nodes("Clause") if n["props"]["doc"].startswith("doc:REG-RM-003")
              and n["props"]["unit_type"] == "table"]
    assert tables


def test_mock_llm_flow(built, tmp_path):
    from kg.eval.runner import run_suite
    from kg.extract.obligations import extract_obligations
    from kg.mapping.alerts import coverage_alerts
    from kg.mapping.coverage import map_coverage
    from kg.services import compare, doccheck, qa, replay

    cfg, store = built
    cache = tmp_path / "cache"
    with Run(store, cfg.path("runs"), "t-flow") as run:
        llm = LLM(cfg, store, run.id, backend=mock_backend, cache_dir=cache)
        s = extract_obligations(cfg, store, run, llm)
        assert s["obligations"] > 50 and s["failed_units"] == 0
        assert store.nodes("Obligation", as_of="2021-06-01") and store.nodes("Obligation", as_of="2023-08-01")
        # dry run costs nothing and reports the estimate
        before = len(store.llm_calls())
        est = map_coverage(cfg, store, run, llm, dry_run=True)
        assert est["estimated_requests"] > 0 and len(store.llm_calls()) == before
        res = map_coverage(cfg, store, run, llm)
        assert res["docs"] == 7 and res["A"] > 0 and res["B"] > 0
        f = store.findings()
        assert {x["gap_class"] for x in f} <= {"A", "B"}
        assert all(x["priority"] and x["issue_chance"] and x["action_type"] for x in f)
        assert store.edges("COVERED_BY")
        # second run reads the cache only
        calls_before = sum(1 for c in store.llm_calls() if not c["cache_hit"])
        map_coverage(cfg, store, run, llm)
        assert sum(1 for c in store.llm_calls() if not c["cache_hit"]) == calls_before
        al = coverage_alerts(cfg, store, run)
        assert al["active"] > 0
        assert coverage_alerts(cfg, store, run)["new"] == 0          # deduplicated
    with Run(store, cfg.path("runs"), "t-replay") as run:
        llm = LLM(cfg, store, run.id, backend=mock_backend, cache_dir=cache)
        rep = replay.replay(cfg, store, run, llm, "4964423", "5451815")
        assert rep["new_obligations"] and rep["changes"] and rep["report_files"]
        assert any(o["source_path"] == "art9_1/p2" for o in rep["new_obligations"] + rep["changed_obligations"])
    with Run(store, cfg.path("runs"), "t-svc") as run:
        llm = LLM(cfg, store, run.id, backend=mock_backend, cache_dir=cache)
        a = qa.answer(cfg, store, llm, "რა ვადაში უნდა ჩატარდეს ვალიდაცია?")
        assert a["citations"] and a["dropped_citations"] == ["bogus-id"]
        c = compare.versions(store, "4964423", "2021-06-01", "2023-08-01")
        assert c["counts"]["added"] > 0 and c["counts"]["modified"] > 0
        assert any(ch["diff"] for ch in c["changes"] if ch["kind"] == "modified")
        assert compare.summarize(llm, c) == "mock summary"
        docs = doccheck.list_documents(store)
        assert doccheck.check_existing(store, docs[0]["id"]) is not None
        for suite in ("compliance", "consolidation", "ops", "qa"):
            r = run_suite(cfg, store, run, suite)
            assert (cfg.path("eval_results") / run.id / "metrics.json").exists()
        assert (cfg.path("eval_results") / "index.csv").exists()
