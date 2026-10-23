"""Other directions: 44/04 replayed from its original text, one-act rebuilds, the D12-D14 gold keys."""
import json

from kg.ingest import jobs
from kg.runlog import Run


def _pv(store, act):
    return sorted((n["id"], n["valid_from"], n["valid_to"]) for n in store.nodes("ProvisionVersion")
                  if str(n["props"].get("act")) == act)


def test_retail_lending_act_is_versioned_from_the_original(kg):
    cfg, store = kg
    assert cfg["acts"]["4822603"]["mode"] == "versioned"
    pvs = _pv(store, "4822603")
    assert len(pvs) > len({p[0].rsplit("@", 1)[0] for p in pvs})          # provisions with several versions
    grace = [p for p in pvs if "/art2/p1/sო@" in p[0]]
    assert [g[1] for g in grace][:2] == ["2020-04-15", "2022-04-01"]          # original, then order 31/04


def test_rebuilding_one_act_leaves_the_others_untouched(kg):
    cfg, store = kg
    before = _pv(store, "4964423")
    with Run(store, cfg.path("runs"), "t-one-act") as run:
        jobs.parse_acts(cfg, store, run, only=["4822603"])
        jobs.parse_amendments(cfg, store, run, None, False, only=["4822603"])
        out = jobs.consolidate(cfg, store, run, only=["4822603"])
    assert list(out) == ["4822603"] and _pv(store, "4964423") == before


def test_gold_keys_of_other_directions_are_read(kg):
    from kg.eval.compliance import load_gold
    cfg, store = kg
    rows = load_gold(cfg)
    d12 = [r for r in rows if r["id"].startswith("D12-")]
    assert d12 and all(r["document_file"].startswith("D12_") for r in d12)
    r = next(r for r in d12 if r["id"] == "D12-07")
    assert r["paths"] == ["art5/p8"]
    assert any(r["id"].startswith("D1-") for r in rows)                   # the model risk key is still there


def test_fill_labels_in_place_keeps_obligation_links(kg):
    from kg.extract.obligations_v2 import extract_obligations_v2
    from kg.llm import LLM
    from fake_llm import FakeGemini
    from kg.mapping.judging import source_of
    cfg, store = kg
    with Run(store, cfg.path("runs"), "t-fl") as run:
        extract_obligations_v2(cfg, store, run, LLM(cfg, store, run.id, backend=FakeGemini()))
    imposes = len(store.edges("IMPOSES"))
    # simulate an act written before KG v2: no labels
    for n in store.nodes("ProvisionVersion") + store.nodes("Provision"):
        if str(n["props"].get("act")) == "4964423":
            p = {k: v for k, v in n["props"].items()
                 if k not in ("display", "title", "article_title", "act_short", "index_text")}
            store.x("UPDATE nodes SET props=? WHERE id=?", (json.dumps(p, ensure_ascii=False), n["id"]))
    with Run(store, cfg.path("runs"), "t-fl2") as run:
        out = jobs.fill_labels(cfg, store, run, only=["4964423"])
    assert out["4964423"]["versions_updated"] > 0 and out["4964423"]["provisions_updated"] > 0
    assert len(store.edges("IMPOSES")) == imposes                            # nothing deleted
    pv = next(n for n in store.nodes("ProvisionVersion")
              if n["props"].get("act") == "4964423" and n["props"]["path"] == "art6/p4.1")
    assert pv["props"]["display"].startswith("მუხლი 6") and pv["props"]["index_text"]
    ob = store.nodes("Obligation")[0]
    src = source_of(store, ob)
    assert src["display"] and src["article"].startswith("მუხლი ") and src["text"]
    with Run(store, cfg.path("runs"), "t-fl3") as run:                      # running again changes nothing
        assert jobs.fill_labels(cfg, store, run, only=["4964423"])["4964423"] == {"provisions_updated": 0,
                                                                                    "versions_updated": 0}


def test_fill_labels_matches_what_a_rebuild_writes(kg):
    cfg, store = kg                                       # the test graph was written by the current code
    with Run(store, cfg.path("runs"), "t-fl4") as run:
        out = jobs.fill_labels(cfg, store, run)
    assert all(v == {"provisions_updated": 0, "versions_updated": 0} for v in out.values()), out


def test_oversized_diff_sends_only_the_changed_sub_points(kg):
    from kg.extract.context import ContextBuilder
    from kg.extract.obligations import build_units
    from kg.extract.obligations_v2 import _only_changed_members, _too_big
    cfg, store = kg
    units = build_units(store, "4822603")
    vs = sorted([u for u in units if u.unit_path == "art2/p1"], key=lambda u: u.date)
    ctx = ContextBuilder(cfg, store, "4822603").context(vs[1], vs[0], [])
    before = len(json.dumps(ctx, ensure_ascii=False))
    _only_changed_members(ctx, vs[1], vs[0])
    assert len(json.dumps(ctx, ensure_ascii=False)) < before / 2 and "[art2/p1/sო]" in ctx["unit"]["text"]
    assert "[art2/p1/sა]" not in ctx["unit"]["text"] and ctx["note"]

    class L:                                                  # 14K tokens per minute, 2 chars per token
        def models_for(self, p): return ["m"]
        def options_for(self, m, p): return {"tokens_per_minute": 14000}
        def estimate_tokens(self, msgs): return sum(len(m["content"]) for m in msgs) // 2
    assert _too_big(L(), "amend_diff", [{"content": "x" * 30000}]) and not _too_big(L(), "amend_diff",
                                                                                     [{"content": "x" * 1000}])


def test_documents_with_body_size_headings_are_parsed():
    """D12-D14 set their section headings bold in the body font size: they must not end up as one cover block."""
    from pathlib import Path
    from kg.company.pdf_parser import parse_pdf
    root = Path(__file__).resolve().parents[1] / "other_directions_company_docs"
    for f in sorted(root.glob("D1[234]_*.pdf")):
        pd = parse_pdf(f, llm=None, classify_llm=False)
        kinds = [u.unit_type for u in pd.units]
        assert kinds.count("heading") >= 4 and kinds.count("clause") >= 10, (f.name, kinds)
        assert "heuristics_weak" not in pd.warnings


def test_clause_ids_written_without_prefix_are_resolved():
    from kg.mapping.judging import resolve_clause_id
    allowed = {"cl:bank-a/X@1.0/2.2", "cl:bank-a/X@1.0/2.1", "cl:bank-a/X@1.0/t27", "cl:bank-a/Y@1.0/2.2"}
    assert resolve_clause_id("bank-a/X@1.0/2.2", allowed) == "cl:bank-a/X@1.0/2.2"
    assert resolve_clause_id("cl:bank-a/X@1.0/2.1", allowed) == "cl:bank-a/X@1.0/2.1"
    assert resolve_clause_id("X@1.0/t27", allowed) == "cl:bank-a/X@1.0/t27"
    assert resolve_clause_id("2.2", allowed) is None                         # ambiguous: two documents
    assert resolve_clause_id("9.9", allowed) is None


def test_long_units_are_split_into_smaller_batches():
    from types import SimpleNamespace as N
    from kg.extract.obligations_v2 import _chunks
    items = [(N(text_for_llm="x" * n, uid=str(i)), None, []) for i, n in enumerate([100, 200, 300, 3000, 3900, 2400])]
    out = _chunks(items, 3, 5000)
    assert [[x[0].uid for x in b] for b in out] == [["0", "1", "2"], ["3"], ["4"], ["5"]]
    assert _chunks(items, 3) == [items[:3], items[3:]]                      # without a limit: as before
