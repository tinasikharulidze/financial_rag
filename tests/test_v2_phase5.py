"""Phase 5: proposals (kinds, targets, grounding, reuse) and the revised document (merge, docx, markdown)."""
import pytest
from fake_llm import FakeGemini

from kg.company import profile as prof
from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM
from kg.mapping.docview import load_doc
from kg.mapping.pipeline import map_document
from kg.mapping.proposals import get_proposals, numbers_in, plan_items, propose_document
from kg.runlog import Run
from kg.services import accounts
from kg.services.demo import seed_demo
from kg.services.permissions import PermissionDenied
from kg.services.revise import build_revised

NOW = "2025-01-01"


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    run = Run(store, cfg.path("runs"), "t-phase5")
    run.__enter__()
    llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
    extract_obligations_v2(cfg, store, run, llm)
    docs = seed_demo(cfg, store)["documents"]
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    reg = accounts.authenticate(store, "nbg@demo.ge", "demo1234")
    v = next(d for d in docs if "POL-RM-014" in d)
    prof.build_profile(cfg, store, run, llm, v)
    prof.confirm_profile(store, owner, v, domains=["model_risk"])
    map_document(cfg, store, run, llm, v, now=NOW)
    yield cfg, store, run, llm, fake, v, owner, reg
    run.__exit__(None, None, None)


def test_plan_follows_the_coverage(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    view = load_doc(store, v)
    plan = plan_items(cfg, store, v, view)
    non_full = store.q("SELECT * FROM coverage WHERE doc_version_id=? AND verdict IN ('partial','contradicts','none') "
                       "AND invalid=0", (v,))
    assert len(plan) == len(non_full) > 0
    for it in plan:
        verdict = it["coverage"]["verdict"]
        if it["kind"] == "adjust_clause":
            assert verdict in ("partial", "contradicts") and it["target"] in view.by_id
        elif it["kind"] == "add_section":
            assert verdict == "none" and it["target"] in view.by_id
    assert {it["kind"] for it in plan} >= {"adjust_clause"} or {it["kind"] for it in plan} >= {"add_section"}


def test_proposals_are_written_grounded_and_reused(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    view = load_doc(store, v)
    n_plan = len(plan_items(cfg, store, v, view))
    stats = propose_document(cfg, store, run, llm, v)
    assert stats["written"] == n_plan and stats["failed"] == 0
    assert fake.calls["propose"] == -(-n_plan // cfg["mapping"]["proposal_batch"])
    props = get_proposals(store, v)
    assert len(props) == n_plan and len({p["obligation_id"] for p in props}) == n_plan
    for p in props:
        assert p["proposed_text"] and p["explanation"]
        if p["kind"] == "adjust_clause":
            assert p["original_text"] == view.by_id[p["clause_id"]]["text"]       # copied by code
            assert p["proposed_text"] != p["original_text"]
        else:
            assert p["original_text"] is None and p["heading"]
        assert p["citations"] and p["citations"][0]["label"]
        assert "ungrounded_numbers" not in " ".join(p["flags"])
    ids = [p["id"] for p in props]
    again = propose_document(cfg, store, run, llm, v)
    assert again["batches"] == 0 and [p["id"] for p in get_proposals(store, v)] == ids
    # force: the stored answer is reused (no new request) and the ids stay
    n = fake.calls["propose"]
    propose_document(cfg, store, run, llm, v, force=False)
    assert fake.calls["propose"] == n


def test_ungrounded_numbers_are_flagged(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    fake.propose_extra = " დაუყოვნებლივ 777 დღეში."
    propose_document(cfg, store, run, llm, v)
    flagged = [p for p in get_proposals(store, v) if any(f.startswith("ungrounded_numbers") for f in p["flags"])]
    assert flagged and all("777" in " ".join(p["flags"]) for p in flagged)
    assert numbers_in("30 დღე და 1,5 წელი") == {"30", "1.5"}


def test_unchanged_text_is_rejected_after_one_retry(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    view = load_doc(store, v)
    plan = plan_items(cfg, store, v, view)
    adjust = [it for it in plan if it["kind"] == "adjust_clause"]
    assert adjust
    fake.propose_unchanged = True
    stats = propose_document(cfg, store, run, llm, v)
    assert stats["failed"] == len(adjust) and stats["written"] == len(plan) - len(adjust)
    assert fake.calls["propose"] >= 2                                   # the retry with the problems shown


def test_obsolete_proposals_are_pruned(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    propose_document(cfg, store, run, llm, v)
    victim = get_proposals(store, v)[0]["obligation_id"]
    store.x("UPDATE coverage SET verdict='full' WHERE doc_version_id=? AND obligation_id=?", (v, victim))
    propose_document(cfg, store, run, llm, v)
    assert victim not in {p["obligation_id"] for p in get_proposals(store, v)}


def test_new_document_when_nothing_fits(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    view = load_doc(store, v)
    plan = plan_items(cfg, store, v, view)
    target = next(it for it in plan if it["kind"] == "add_section")
    oid = target["obligation"]["id"]
    store.x("DELETE FROM locations WHERE doc_version_id=? AND obligation_id=?", (v, oid))
    store.x("UPDATE triage SET relevance='possibly' WHERE doc_version_id=? AND obligation_id=?", (v, oid))
    plan2 = {it["obligation"]["id"]: it for it in plan_items(cfg, store, v, view)}
    assert plan2[oid]["kind"] == "new_document"
    propose_document(cfg, store, run, llm, v)
    p = next(p for p in get_proposals(store, v) if p["obligation_id"] == oid)
    assert p["kind"] == "new_document" and p["heading"] and p["clause_id"] is None


def test_revised_document(env, tmp_path):
    cfg, store, run, llm, fake, v, owner, reg = env
    propose_document(cfg, store, run, llm, v)
    props = get_proposals(store, v)
    with pytest.raises(PermissionDenied):
        build_revised(cfg, store, run, llm, reg, v, [p["id"] for p in props])
    out = build_revised(cfg, store, run, llm, owner, v, [p["id"] for p in props])
    n_adjust = len({p["clause_id"] for p in props if p["kind"] == "adjust_clause"})
    assert out["changed_clauses"] == n_adjust
    assert out["added"] == sum(1 for p in props if p["kind"] == "add_section")
    docx_path, md_path = cfg.path("uploads") / out["docx"], cfg.path("uploads") / out["md"]
    assert docx_path.exists() and md_path.exists() and docx_path.parent.name == "revised"
    from docx import Document
    d = Document(str(docx_path))
    text = "\n".join(p.text for p in d.paragraphs)
    assert "ზოგადი დებულებები" in text and "გარდა ამისა" in text
    md = md_path.read_text(encoding="utf-8")
    assert "*(შეცვლილია)*" in md and ("*(ახალი)*" in md or out["added"] == 0)
    assert all(c["diff"] for c in out["changes"])
    assert store.q("SELECT kind FROM user_decisions ORDER BY id DESC LIMIT 1")[0]["kind"] == "revise"


def test_several_proposals_for_one_clause_are_merged(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    propose_document(cfg, store, run, llm, v)
    adj = [p for p in get_proposals(store, v) if p["kind"] == "adjust_clause"]
    assert adj
    first = adj[0]
    store.x("INSERT INTO proposals(doc_version_id,obligation_id,kind,clause_id,original_text,proposed_text,heading,"
            "explanation,citations,flags,llm_output_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (v, "obl:other", "adjust_clause", first["clause_id"], first["original_text"], first["original_text"] + " ორი.",
             None, "მეორე.", "[]", "[]", None, "x"))
    ids = [p["id"] for p in get_proposals(store, v) if p["clause_id"] == first["clause_id"]]
    assert len(ids) == 2
    fake.merge_conflict = True
    out = build_revised(cfg, store, run, llm, owner, v, ids)
    assert out["merge_requests"] == 1 and fake.calls["merge"] == 1
    assert first["clause_id"] in out["conflicts"]
    assert "[გაერთიანებული" in "".join(c["new"] for c in out["changes"])
    n = fake.calls["merge"]
    build_revised(cfg, store, run, llm, owner, v, ids)                      # stored answer reused
    assert fake.calls["merge"] == n


def test_added_sections_follow_their_anchor(env):
    cfg, store, run, llm, fake, v, owner, reg = env
    propose_document(cfg, store, run, llm, v)
    adds = [p for p in get_proposals(store, v) if p["kind"] == "add_section"]
    assert adds
    from kg.services.revise import build_blocks
    view = load_doc(store, v)
    built = build_blocks(store, cfg, run, llm, view, adds)
    for i, b in enumerate(built["blocks"]):
        if b["type"] == "added":
            assert built["blocks"][i - 1]["id"] in {p["clause_id"] for p in adds} or built["blocks"][i - 1]["type"] == "added"
