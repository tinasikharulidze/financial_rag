import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------------------------------- shared fixtures
import json as _json

import pytest as _pytest


def mock_backend(model, messages, max_tokens, temperature, json_mode, purpose=None):
    """Deterministic stand-in for Gemini used by the pipeline tests."""
    user = messages[-1]["content"]

    def out(o):
        return {"text": _json.dumps(o, ensure_ascii=False), "input_tokens": 10, "output_tokens": 5}

    if purpose == "extract_obligations":
        items = _json.loads(user.split("\n", 1)[1])
        return out({"results": [{"id": i["id"], "category": "validation", "obligations": [{
            "statement": i["text"][:200], "subject": "bank", "action": "validate", "severity": "high",
            "category": "validation"}]} for i in items]})
    if purpose == "judge_coverage":
        items = _json.loads(user.split("\n", 1)[1])
        return out({"results": [{"obligation_id": i["obligation_id"], "coverage": ["full", "partial", "none"][k % 3],
                                 "best_clause_id": i["candidates"][0]["id"] if i["candidates"] else None,
                                 "issue_chance": "high", "rationale": "mock", "evidence_quote": None,
                                 "suggested_fix": "mock fix", "action_type": None} for k, i in enumerate(items)]})
    if purpose == "qa_answer":
        ps = _json.loads(user.split("Passages:\n", 1)[1])
        return out({"answer": "mock answer", "citations": [ps[0]["id"], "bogus-id"]})
    if purpose == "compare_summary":
        return out({"summary": "mock summary"})
    raise RuntimeError(f"unexpected purpose {purpose}")


@_pytest.fixture(scope="session")
def built(tmp_path_factory):
    """Phases 0-3 built from the real corpus into a temporary directory (no LLM)."""
    from kg.config import Config, load_config
    from kg.company.ingest import ingest_company_docs
    from kg.ingest import jobs
    from kg.runlog import Run
    from kg.store.sqlite_nx import SQLiteStore

    tmp = tmp_path_factory.mktemp("kg")
    base = load_config()
    cfg = Config(_json.loads(_json.dumps(base)))
    for k in ("raw_html", "amendment_docs", "amendment_edges", "company_docs", "gold_labels"):
        cfg["paths"][k] = str(base.path(k))
    cfg["paths"].update(data_dir=str(tmp / "data"), db=str(tmp / "data" / "kg.sqlite"), runs=str(tmp / "data" / "runs"),
                        reports=str(tmp / "data" / "reports"), indexes=str(tmp / "data" / "indexes"),
                        llm_cache=str(tmp / "data" / "cache"), eval_results=str(tmp / "eval"),
                        cross_references=str(tmp / "cross.csv"))
    cfg.path("data_dir").mkdir(parents=True)
    cfg["mapping"]["doc_type_table"] = str(base.path("data_dir").parents[1] / "configs" / "doc_type_obligations.yaml")
    store = SQLiteStore(cfg.path("db"))
    with Run(store, cfg.path("runs"), "build") as run:
        jobs.parse_acts(cfg, store, run)
        jobs.parse_amendments(cfg, store, run)
        jobs.consolidate(cfg, store, run)
        jobs.extract_refs(cfg, store, run)
        ingest_company_docs(cfg, store, run)
    return cfg, store
