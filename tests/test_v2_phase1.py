"""Phase 1 on the real corpus: titles, exact references, context builder, extraction v2 and the amendment diff
(with the fake model of ``fake_llm.py``)."""
import json
from collections import Counter

import pytest
from fake_llm import FakeGemini

from kg.citations import verify_citations
from kg.extract.context import ContextBuilder, context_ids
from kg.extract.obligations import build_units
from kg.extract.obligations_v2 import extract_obligations_v2, plan
from kg.llm import LLM, BudgetExhausted
from kg.runlog import Run

ACT = "4964423"


# --------------------------------------------------------------------------------------------- titles
def test_every_provision_has_title_display_and_index_text(built):
    _, store = built
    pvs = store.nodes("ProvisionVersion")
    assert pvs and all(n["props"].get("title") is not None and n["props"].get("display") for n in pvs)
    assert all("index_text" in n["props"] and n["props"]["act_short"] is not None or n["props"]["act"] for n in pvs)
    provs = store.nodes("Provision")
    assert all(p["props"].get("display") and "title" in p["props"] for p in provs)
    # articles of the model-risk act all have a title; order articles and annexes get one from their text
    arts = [n for n in pvs if "/" not in n["props"]["path"] and n["props"]["status"] == "active"]
    assert arts and all(n["props"]["article_title"] for n in arts), \
        [n["id"] for n in arts if not n["props"]["article_title"]]


def test_index_text_carries_the_article_title(built):
    _, store = built
    n = store.get_node("pv:4964423/art6/p4.1@2022-05-03")
    t = n["props"]["index_text"]
    assert t.startswith("მოდელის რისკის მართვა › მუხლი 6. მოდელის შიდა ვალიდაცია › პუნქტი 4¹")
    assert "რისკზე დაფუძნებული" in t
    art = store.get_node("pv:4964423/art6@2021-01-01")
    assert art["props"]["title"] == "მოდელის შიდა ვალიდაცია"


def test_references_are_exact_and_placeholders_are_visible(built):
    _, store = built
    refs = store.edges("REFERS_TO")
    nodes = store.get_nodes([r["dst"] for r in refs])
    assert all(r["dst"] in nodes for r in refs), "every REFERS_TO target exists as a node"
    ph = [n for n in store.nodes("Provision") if n["props"].get("placeholder")]
    assert ph, "at least one reference points to a provision that is not in the corpus"
    for p in ph:
        assert not store.neighbors(p["id"], "VERSION_OF", "in")           # no text
        e = [r for r in refs if r["dst"] == p["id"]]
        assert e and all(x["props"].get("resolved_to") for x in e)       # the nearest existing ancestor is recorded
        assert "მიუწვდომელია" in p["props"]["title"]
    ext = store.nodes("ExternalProvision")
    assert ext and store.edges("PART_OF")


# --------------------------------------------------------------------------------------------- context
def test_context_builder_on_a_real_unit(kg):
    cfg, store = kg
    units = build_units(store, ACT)
    u = next(x for x in units if x.unit_path == "art6/p5" and x.date == "2021-01-01")
    b = ContextBuilder(cfg, store, ACT)
    ctx = b.context(u)
    assert ctx["article"]["title"] == "მოდელის შიდა ვალიდაცია" and ctx["article"]["display"] == "მუხლი 6"
    assert ctx["act"]["default_domains"] == ["model_risk"] and ctx["act"]["default_applies_to"] == ["commercial_bank"]
    assert ctx["unit"]["provisions"][0]["pv_id"].startswith("pv:4964423/art6/p5@")
    assert ctx["definitions"], "the definitions article provides the terms used in the unit"
    ids = context_ids(ctx)
    assert ids and all(store.get_node(i) for i in ids)
    # a unit with a reference to another provision of the same act shows its text
    refs = [r for x in units for r in b.context(x)["references"]]
    assert any(r["available"] and r["text"] for r in refs)
    # context JSON must stay small enough for a 5-unit batch
    sizes = [len(json.dumps(b.context(x), ensure_ascii=False)) for x in units]
    assert max(sizes) < 30000, max(sizes)


def test_plan_request_estimate(kg):
    cfg, store = kg
    pl = plan(cfg, store, ACT)
    assert pl["units"] == pl["fresh_units"] + pl["diff_units"] > 0
    assert 10 <= pl["estimated_requests"] <= 40, pl


# --------------------------------------------------------------------------------------------- extraction
@pytest.fixture
def extracted(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    with Run(store, cfg.path("runs"), "t-extract") as run:
        llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
        summary = extract_obligations_v2(cfg, store, run, llm)
    return cfg, store, fake, summary


def test_extract_v2_end_to_end(extracted):
    cfg, store, fake, summary = extracted
    s = summary["acts"][ACT]
    assert s["obligations"] > 40 and s["failed_units"] == 0
    assert fake.calls["extract_obligations"] >= 10 and fake.calls["amend_diff"] >= 3
    obls = store.nodes("Obligation")
    assert all(n["props"]["schema"] == "v2" for n in obls)
    assert all(n["props"]["citations"] and n["props"]["applies_to"] == ["commercial_bank"] for n in obls)
    assert all(n["props"]["domain"] == "model_risk" for n in obls)
    # every obligation is imposed by an existing provision version, and applies to an entity type
    for n in obls[:60]:
        assert store.neighbors(n["id"], "IMPOSES", "in"), n["id"]
        assert store.neighbors(n["id"], "APPLIES_TO", "out")
    # citations were verified against the real provision text
    for n in obls[:30]:
        from kg.citations import Citation
        good, errs = verify_citations(store, [Citation(**c) for c in n["props"]["citations"]])
        assert good and not errs, (n["id"], errs)


def test_diff_semantics(extracted):
    cfg, store, fake, summary = extracted
    s = summary["acts"][ACT]
    assert s["modified"] > 0 and s["unchanged"] > 0
    sup = store.edges("SUPERSEDES")
    assert len(sup) == s["modified"]
    for e in sup:
        new, old = store.get_node(e["src"]), store.get_node(e["dst"])
        assert old["valid_to"] == new["valid_from"], (old["id"], new["id"])
        assert new["props"]["lineage"] == old["props"]["lineage"]
        assert new["props"]["amendment_id"] and new["props"]["amendment_number"]
        assert e["props"]["what_changed"]
    # an unchanged obligation is carried: still open, no duplicate node for the later unit versions
    open_at_end = store.nodes("Obligation", as_of="2026-01-01")
    assert open_at_end
    keys = Counter((n["props"]["unit"], n["props"]["source_path"], n["props"]["statement"]) for n in open_at_end)
    assert max(keys.values()) == 1, [k for k, v in keys.items() if v > 1][:3]
    # removed / replaced obligations are closed
    closed = [n for n in store.nodes("Obligation") if n["valid_to"]]
    assert closed
    # obligations that exist at the first version and at the last
    v0, v2 = store.nodes("Obligation", as_of="2021-06-01"), store.nodes("Obligation", as_of="2023-08-01")
    assert v0 and v2 and {n["id"] for n in v0} != {n["id"] for n in v2}


def test_unchanged_text_does_not_create_new_obligations(extracted):
    """Units whose only change is elsewhere in the unit must not produce a 'changed' obligation (v1 problem)."""
    cfg, store, fake, summary = extracted
    mod = {e["src"] for e in store.edges("SUPERSEDES")}
    units = store.nodes("ExtractionUnit")
    diffed = [u for u in units if u["props"]["mode"] == "diff"]
    assert diffed
    total_ops = Counter()
    for u in diffed:
        total_ops.update(u["props"].get("ops", {}))
    assert total_ops["unchanged"] > 0
    assert len(mod) == total_ops["modified"]


def test_second_run_is_free_and_identical(extracted, tmp_path):
    cfg, store, fake, _ = extracted
    before_calls = sum(fake.calls.values())
    ids_before = sorted(n["id"] for n in store.nodes("Obligation"))
    props_before = {n["id"]: n["props"]["statement"] for n in store.nodes("Obligation")}
    with Run(store, cfg.path("runs"), "t-extract2") as run:
        llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "other-cache")   # no file cache either
        extract_obligations_v2(cfg, store, run, llm)
    assert sum(fake.calls.values()) == before_calls
    assert sorted(n["id"] for n in store.nodes("Obligation")) == ids_before
    assert {n["id"]: n["props"]["statement"] for n in store.nodes("Obligation")} == props_before


def test_budget_exhaustion_is_resumable(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    cfg["llm"]["daily_request_limit"] = 4
    cfg["llm"]["models"] = ["m/a"]
    with pytest.raises(BudgetExhausted):
        with Run(store, cfg.path("runs"), "t-part") as run:
            llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
            extract_obligations_v2(cfg, store, run, llm)
    assert fake.calls["extract_obligations"] == 4
    partial = len(store.nodes("Obligation"))
    assert partial > 0                                                # finished batches are visible
    cfg["llm"]["daily_request_limit"] = None
    with Run(store, cfg.path("runs"), "t-resume") as run:
        llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
        s = extract_obligations_v2(cfg, store, run, llm)["acts"][ACT]
    # the 4 finished batches were reused: the total number of requests equals the number of batches, not +4
    assert fake.calls["extract_obligations"] + fake.calls["amend_diff"] == s["extract_batches"] + s["diff_batches"]
    assert len(store.nodes("Obligation")) > partial


def test_garbage_answers_do_not_write_obligations(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    fake.fail = {"extract_obligations", "amend_diff"}
    with Run(store, cfg.path("runs"), "t-bad") as run:
        llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
        s = extract_obligations_v2(cfg, store, run, llm)
    assert s["acts"][ACT]["failed_units"] > 0 and not store.nodes("Obligation")
    bad = store.q("SELECT COUNT(*) n FROM step_outputs WHERE valid=0")[0]["n"]
    assert bad > 0
    calls = sum(fake.calls.values())
    with Run(store, cfg.path("runs"), "t-bad2") as run:                 # invalid outputs are not retried every run
        llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
        extract_obligations_v2(cfg, store, run, llm)
    assert sum(fake.calls.values()) == calls


# --------------------------------------------------------------------------------------------- deontic and schema
def test_deontic_from_georgian_cue_words():
    from kg.extract.obligations_v2 import deontic_of
    assert deontic_of("ჩამნაცვლებელი ცვლადი არ უნდა გამოიყენებოდეს, თუ იგი კაუზალურად არ არის დაკავშირებული") == "must_not"
    assert deontic_of("მოდელის განხილვის საერთო ვადა არ უნდა აღემატებოდეს 90 სამუშაო დღეს") == "must_not"
    assert deontic_of("მოდელის გამოყენება დაუშვებელია ვალიდაციამდე") == "must_not"
    assert deontic_of("მოდელის გამომყენებელ სუბიექტს შეუძლია განახორციელოს აუთსორსინგი") == "may"
    assert deontic_of("ბანკმა უნდა ჩაატაროს ვალიდაცია ყოველწლიურად") == "must"
    # a duty with a negated condition stays a duty
    assert deontic_of("თუ მონაცემები არ უნდა იყოს გამოყენებული, ბანკმა უნდა აცნობოს კომიტეტს") == "must"


def test_schema_is_lenient_and_code_decides_a_missing_deontic():
    from kg.extract.obligations_v2 import _deontic
    from kg.extract.schema import DiffBatch, ObligationV2
    o = ObligationV2.model_validate({"statement": "ბანკმა არ უნდა გამოიყენოს მოდელი", "category": None,
                                     "subject": None, "action": None})
    assert o.category == "policy" and o.subject == "" and o.deontic is None
    notes = []
    _deontic(o, notes)
    assert o.deontic == "must_not" and "deontic missing" in notes[0]
    o2 = ObligationV2.model_validate({"statement": "ბანკმა არ უნდა გამოიყენოს მოდელი", "deontic": "must"})
    _deontic(o2, notes)
    assert o2.deontic == "must_not"                                   # negated wording wins over "must"
    assert ObligationV2.model_validate({"statement": "x", "deontic": "must not"}).deontic == "must_not"
    batch = DiffBatch.model_validate({"results": [{"id": "u", "operations": [
        {"op": "new", "new": {"statement": "ბანკმა უნდა ...", "category": None}}]}]})
    assert batch.results[0].operations[0].new.category == "policy"   # was a validation error (a lost request)


def test_both_prompts_carry_the_shared_field_definitions(kg):
    from kg.extract.obligations_v2 import PROMPT_DIFF, PROMPT_EXTRACT, _system
    cfg, _ = kg
    (x, vx), (d, vd) = _system(PROMPT_EXTRACT, cfg), _system(PROMPT_DIFF, cfg)
    for text in (x, d):
        assert "{obligation_fields}" not in text and "{entity_types}" not in text
        assert "DEONTIC" in text and "must_not" in text and "microbank" in text and "Severity rubric" in text
    assert "+fields-v" in vx and "+fields-v" in vd
