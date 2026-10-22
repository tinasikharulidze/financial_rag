"""Phase 3: document view, profile (LLM + user confirmation), filter, triage - on the real corpus with the fake model."""
import pytest
from fake_llm import FakeGemini

from kg.company import profile as prof
from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM
from kg.mapping import filter as flt
from kg.mapping import triage as tri
from kg.mapping.docview import doc_text, estimate_tokens, fits_whole_doc, load_doc
from kg.runlog import Run
from kg.services import accounts
from kg.services.demo import seed_demo
from kg.services.permissions import PermissionDenied


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    run = Run(store, cfg.path("runs"), "t-phase3")
    run.__enter__()
    llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
    extract_obligations_v2(cfg, store, run, llm)
    out = seed_demo(cfg, store)
    ids = {v.split("/")[1].split("@")[0].split("-")[0]: v for v in out["documents"]}
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    reg = accounts.authenticate(store, "nbg@demo.ge", "demo1234")
    yield cfg, store, run, llm, fake, ids, owner, reg
    run.__exit__(None, None, None)


def vid(ids, key):
    return next(v for k, v in ids.items() if key in v)


def test_docview(env):
    cfg, store, run, llm, fake, ids, *_ = env
    v = load_doc(store, vid(ids, "POL-RM-014"))
    assert len(v.sections) >= 20 and v.sections[0]["title"].startswith("1.")
    assert all(u["section"] for u in v.units if u["unit_type"] in ("clause", "heading"))
    assert v.clauses_of([v.sections[0]["section_no"]])
    assert fits_whole_doc(cfg, v) and estimate_tokens(cfg, doc_text(v)) < 12000
    d2 = load_doc(store, vid(ids, "REG-RM-003"))
    assert [s["section_no"] for s in d2.sections] == ["1", "2", "3", "4"] and d2.mapped


def test_profile_flow(env):
    cfg, store, run, llm, fake, ids, owner, reg = env
    v = vid(ids, "POL-RM-014")
    p = prof.build_profile(cfg, store, run, llm, v)
    assert p["from_model"] and not p["confirmed"] and p["doc_type"] == "model_risk_policy"
    assert p["domains"] == ["model_risk"] and p["purpose"]
    view = load_doc(store, v)
    assert [s["section_no"] for s in p["sections"]] == view.section_nos()
    assert p["citations"] and p["citations"][0]["label"]
    assert fake.calls["doc_profile"] == 1
    prof.build_profile(cfg, store, run, llm, v)                         # reuse: no new request
    assert fake.calls["doc_profile"] == 1
    # the regulator cannot confirm; the owner can, with edits
    with pytest.raises(PermissionDenied):
        prof.confirm_profile(store, reg, v)
    c = prof.confirm_profile(store, owner, v, doc_type="Model Risk Policy", domains=["model_risk"],
                             sections=[{"section_no": view.section_nos()[0], "summary": "ახალი შეჯამება"}])
    assert c["confirmed"] and c["doc_type"] == "model_risk_policy" and c["sections"][0]["summary"] == "ახალი შეჯამება"
    dec = store.q("SELECT kind, data FROM user_decisions WHERE doc_version_id=?", (v,))
    assert dec[-1]["kind"] == "profile_edit" and "sections" in dec[-1]["data"]
    # a confirmed profile is not overwritten by a rebuild
    again = prof.build_profile(cfg, store, run, llm, v)
    assert again["confirmed"] and again["sections"][0]["summary"] == "ახალი შეჯამება"
    # confirming without changes is a plain confirmation
    prof.confirm_profile(store, owner, v)
    assert store.q("SELECT kind FROM user_decisions ORDER BY id DESC LIMIT 1")[0]["kind"] == "profile_confirm"


def test_profile_validator_repairs_model_answers(env):
    cfg, store, run, llm, fake, ids, *_ = env
    v = vid(ids, "REG-RM-003")
    orig = fake.profile

    def sloppy(inp):
        o = orig(inp)
        o["sections"] = o["sections"][1:] + [{"section_no": "99", "title": "x", "summary": "y", "goal": "z"}]
        o["domains"] = ["model_risk", "nonsense"]
        o["doc_type"] = "Model Inventory!"
        o["citations"].append({"kind": "clause", "id": "cl:does-not-exist", "quote": "q"})
        return o
    fake.handlers["doc_profile"] = sloppy
    p = prof.build_profile(cfg, store, run, llm, v)
    assert [s["section_no"] for s in p["sections"]] == ["1", "2", "3", "4"]      # the missing one is added, 99 dropped
    assert p["sections"][0]["summary"] == "" and p["domains"] == ["model_risk"]
    assert p["doc_type"] == "model_inventory"
    assert all(c["id"] != "cl:does-not-exist" for c in p["citations"])


def test_profile_falls_back_to_the_parser(env):
    cfg, store, run, llm, fake, ids, owner, _ = env
    fake.fail = {"doc_profile"}
    v = vid(ids, "REG-RM-003")
    p = prof.build_profile(cfg, store, run, llm, v)
    assert not p["from_model"] and p["doc_type"] == "inventory" and len(p["sections"]) == 4
    c = prof.confirm_profile(store, owner, v, doc_type="model_inventory", domains=["model_risk"], purpose="რეესტრი")
    assert c["confirmed"] and c["purpose"] == "რეესტრი"


def confirmed(env, key):
    cfg, store, run, llm, fake, ids, owner, _ = env
    v = vid(ids, key)
    prof.build_profile(cfg, store, run, llm, v)
    return v, prof.confirm_profile(store, owner, v, domains=["model_risk"])


def test_filter(env):
    cfg, store, run, llm, fake, ids, owner, _ = env
    v, p = confirmed(env, "POL-RM-014")
    node = store.get_node(v)
    f = flt.filter_obligations(cfg, store, run, node["props"], p, "commercial_bank", now="2025-01-01")
    assert f["kept"] and f["v_now"] == "2025-01-01" and f["v_doc"] == "2021-04-01"
    assert all(o["props"]["schema"] == "v2" for o in f["kept"])
    assert {"not_v2"} >= set(f["dropped"]) or f["dropped"]
    # other domain / entity type
    p2 = {**p, "domains": ["other_domain"]}
    f2 = flt.filter_obligations(cfg, store, run, node["props"], p2, "commercial_bank", now="2025-01-01")
    assert len(f2["kept"]) < len(f["kept"]) and f2["dropped"]["domain"]
    f3 = flt.filter_obligations(cfg, store, run, node["props"], p, "payment_service_provider", now="2025-01-01")
    assert len(f3["kept"]) == 0 and f3["dropped"]["entity_type"]
    # a date before the regulation existed keeps nothing
    f4 = flt.filter_obligations(cfg, store, run, node["props"], p, "commercial_bank", now="2000-01-01")
    assert f4["kept"] == []
    # cause
    o = f["kept"][0]
    assert flt.cause_of(o, "2030-01-01") == "pre_existing"
    assert flt.cause_of({"valid_from": "2023-01-01", "props": {}}, "2022-01-01") == "amendment"
    assert flt.cause_of({"valid_from": "2023-01-01", "props": {}}, None) == "pre_existing"


def test_triage_and_guard(env):
    cfg, store, run, llm, fake, ids, owner, _ = env
    v, p = confirmed(env, "POL-RM-014")
    view = load_doc(store, v)
    f = flt.filter_obligations(cfg, store, run, store.get_node(v)["props"], p, "commercial_bank", now="2025-01-01")
    obls = f["kept"]
    c = tri.triage_document(cfg, store, run, llm, v, view, p, obls)
    n = len(obls)
    assert c["relevant"] + c["possibly"] + c["irrelevant"] == n and c["relevant"] > 0 and c["irrelevant"] > 0
    assert fake.calls["triage"] == -(-n // cfg["mapping"]["triage_batch"])
    rows = tri.triage_rows(store, v)
    assert len(rows) == n and all(r["reason"] for r in rows)
    assert any(r["citations"] and r["citations"][0]["label"] for r in rows)
    assert len(tri.continuing(store, v)) == c["relevant"] + c["possibly"]
    # second run: no requests
    before = fake.calls["triage"]
    tri.triage_document(cfg, store, run, llm, v, view, p, obls)
    assert fake.calls["triage"] == before
    # inventory document: policy-only obligations are irrelevant there
    v2, p2 = confirmed(env, "REG-RM-003")
    c2 = tri.triage_document(cfg, store, run, llm, v2, load_doc(store, v2), p2, obls)
    assert c2["relevant"] < c["relevant"]


def test_explicit_guard_overrides_the_model(env):
    cfg, store, run, llm, fake, ids, owner, _ = env
    v, p = confirmed(env, "POL-RM-014")
    view = load_doc(store, v)
    obls = flt.filter_obligations(cfg, store, run, store.get_node(v)["props"], p, "commercial_bank",
                                  now="2025-01-01")["kept"]
    fake.triage_all = "irrelevant"
    c = tri.triage_document(cfg, store, run, llm, v, view, p, obls)
    explicit = [o for o in obls if tri.explicit_match(o, [p["doc_type"], "policy"])]
    assert explicit and c["guard"] == len(explicit) and c["relevant"] == len(explicit)
    rules = [r for r in tri.triage_rows(store, v) if r["source"] == "rule"]
    assert len(rules) == len(explicit) and all(r["relevance"] == "relevant" for r in rules)


def test_triage_missing_answers_and_user_rows(env):
    cfg, store, run, llm, fake, ids, owner, _ = env
    v, p = confirmed(env, "POL-RM-014")
    view = load_doc(store, v)
    obls = flt.filter_obligations(cfg, store, run, store.get_node(v)["props"], p, "commercial_bank",
                                  now="2025-01-01")["kept"][:6]
    orig = fake.triage
    fake.handlers["triage"] = lambda inp: {"results": orig(inp)["results"][:3]}
    c = tri.triage_document(cfg, store, run, llm, v, view, p, obls)
    assert c["missing"] == 3
    rows = {r["obligation_id"]: r for r in tri.triage_rows(store, v)}
    assert sum(1 for r in rows.values() if r["source"] == "rule") >= 3
    # a row decided by the user is never overwritten
    target = obls[0]["id"]
    store.x("UPDATE triage SET relevance='irrelevant', source='user' WHERE doc_version_id=? AND obligation_id=?",
            (v, target))
    fake.handlers["triage"] = orig
    tri.triage_document(cfg, store, run, llm, v, view, p, obls, force=True)
    row = [r for r in tri.triage_rows(store, v) if r["obligation_id"] == target][0]
    assert row["source"] == "user" and row["relevance"] == "irrelevant"


def test_type_matching():
    assert prof.type_matches(["model_risk_policy"], ["policy"])
    assert prof.type_matches(["inventory"], ["model_inventory"])
    assert prof.type_matches(["validation_report"], ["validation_report"])
    assert not prof.type_matches(["monitoring_report"], ["validation_report", "policy"])
    assert not prof.type_matches([None, ""], ["policy"])
