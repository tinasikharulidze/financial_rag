"""Confirmed writes: the agent only proposes, the person's click commits through the normal services."""
import json

import pytest
from test_agent import CL_A, DOC_A, DOC_B, ENTITY_A, ENTITY_B, OBL, REGULATOR, Script, answer, make_llm, tool
from test_agent_tools import rich  # noqa: F401

from kg.agent import actions as A
from kg.agent import run_turn
from kg.agent.tools import ToolBox
from kg.config import Config

CL_A2 = "cl:doc:A/1#2"
OBL_GAP2 = "obl:x/gap"


@pytest.fixture
def env(rich, tmp_path):
    s = rich
    s.upsert_node(CL_A2, "Clause", {"doc": DOC_A, "section_no": "1.2", "unit_type": "clause", "text": "ჩვენ ვაფასებთ რისკს ყოველკვარტალურად.",
                                    "order": 2})
    s.upsert_edge(DOC_A, CL_A2, "HAS_CLAUSE")
    cfg = Config({"paths": {"runs": str(tmp_path / "runs")}})
    return s, cfg


def box(store, actor=ENTITY_A, read=(CL_A, CL_A2)):
    t = ToolBox(store, actor)
    t.call("clause_coverage", {"doc_version_id": DOC_A, "obligation_id": OBL})
    for c in read:
        t.call("read_text", {"id": c})
    return t


def pending_row(store, aid):
    return A.get(store, ENTITY_A, aid)


def test_propose_changes_nothing_until_confirmed(env):
    s, cfg = env
    r = box(s).call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL,
                                             "reason": "The obligation does not apply: we use no scoring models."})
    assert r["status"] == "waiting_for_the_user_to_confirm"
    assert s.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "open"          # untouched
    assert s.q1("SELECT COUNT(*) n FROM user_decisions")["n"] == 0
    row = pending_row(s, 1)
    assert row["status"] == "pending" and row["before"]["status"] == "open" and row["after"]["status"] == "not_relevant"


def test_confirm_dismisses_the_flag_with_the_reason_and_an_audit_row(env):
    s, cfg = env
    t = box(s)
    t.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL,
                                    "reason": "The obligation does not apply: we use no scoring models."})
    row = A.confirm(cfg, s, ENTITY_A, t.proposed[0], "Edited by the person: we use no scoring models at all.")
    assert row["status"] == "confirmed"
    task = s.q1("SELECT status FROM tasks WHERE key='k'")
    assert task["status"] == "not_relevant"
    kinds = [d["kind"] for d in s.q("SELECT kind FROM user_decisions ORDER BY id")]
    assert kinds == ["not_relevant", "chat_action"]
    audit = json.loads(s.q1("SELECT data FROM user_decisions WHERE kind='chat_action'")["data"])
    assert audit["reason"].startswith("Edited by the person") and audit["before"]["status"] == "open"


def test_undo_reopens_and_a_second_confirm_is_refused(env):
    s, cfg = env
    t = box(s)
    t.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to a bank like us"})
    aid = t.proposed[0]
    A.confirm(cfg, s, ENTITY_A, aid)
    with pytest.raises(A.ActionError):
        A.confirm(cfg, s, ENTITY_A, aid)
    assert A.undo(cfg, s, ENTITY_A, aid)["status"] == "undone"
    assert s.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "open"


def test_commit_is_refused_when_the_flag_changed_after_the_proposal(env):
    s, cfg = env
    t = box(s)
    t.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to a bank like us"})
    s.x("UPDATE tasks SET status='accepted' WHERE key='k'")                       # somebody else acted in the meantime
    assert A.view(s, ENTITY_A, t.proposed[0])["status"] == "stale"
    with pytest.raises(A.ActionError, match="changed"):
        A.confirm(cfg, s, ENTITY_A, t.proposed[0])
    assert pending_row(s, t.proposed[0])["status"] == "stale"


def test_proposals_expire(env):
    s, cfg = env
    t = box(s)
    t.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to a bank like us"})
    s.x("UPDATE agent_actions SET expires_at='2000-01-01T00:00:00'")
    assert A.view(s, ENTITY_A, t.proposed[0])["status"] == "expired"
    with pytest.raises(A.ActionError):
        A.confirm(cfg, s, ENTITY_A, t.proposed[0])


def test_the_model_must_have_read_what_it_names(env):
    s, _ = env
    t = ToolBox(s, ENTITY_A)                                                       # nothing read in this turn
    r = t.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to us"})
    assert "error" in r and not t.proposed
    t2 = box(s, read=())                                                           # flag read, clause not
    r = t2.call("propose_mark_covered", {"doc_version_id": DOC_A, "obligation_id": OBL, "clause_ids": [CL_A2],
                                        "reason": "the clause covers it fully"})
    assert "error" in r and not t2.proposed


def test_a_reason_is_required(env):
    s, _ = env
    assert "reason" in box(s).call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL,
                                                            "reason": ""})["error"]


def test_mark_covered_closes_the_flag_and_undo_restores_it(env):
    s, cfg = env
    t = box(s)
    t.call("propose_mark_covered", {"doc_version_id": DOC_A, "obligation_id": OBL, "clause_ids": [CL_A2],
                                    "reason": "clause 1.2 requires a quarterly assessment"})
    A.confirm(cfg, s, ENTITY_A, t.proposed[0])
    assert s.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "already_addressed"
    assert s.q1("SELECT verdict FROM coverage WHERE doc_version_id=? AND obligation_id=?", (DOC_A, OBL))["verdict"] == "full"
    A.undo(cfg, s, ENTITY_A, t.proposed[0])
    assert s.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "open"


def test_mapping_change_shows_every_item_and_undo_restores_the_old_role(env):
    s, cfg = env
    t = box(s)
    r = t.call("propose_mapping_change", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "1.2 is the real cover",
                                          "changes": [{"clause_id": CL_A2, "op": "add", "role": "full"},
                                                      {"clause_id": CL_A, "op": "change", "role": "contradicts"}]})
    assert r["after"]["needs_recheck"] and r["after"]["clauses"] == {CL_A2: "full", CL_A: "contradicts"}
    aid = t.proposed[0]
    A.confirm(cfg, s, ENTITY_A, aid)
    from kg.services import decisions as D
    roles = {e["clause_id"]: e["role"] for e in D.edges_of(s, DOC_A, OBL) if e["role"] != "rejected"}
    assert roles == {CL_A2: "full", CL_A: "contradicts"}
    A.undo(cfg, s, ENTITY_A, aid)
    roles = {e["clause_id"]: e["role"] for e in D.edges_of(s, DOC_A, OBL) if e["role"] not in ("rejected",)}
    assert roles.get(CL_A) == "partial" and CL_A2 not in roles


def test_mapping_ops_are_validated(env):
    s, _ = env
    t = box(s)
    bad = t.call("propose_mapping_change", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "because of this",
                                            "changes": [{"clause_id": CL_A, "op": "add", "role": "full"}]})
    assert "already mapped" in bad["error"]
    bad = t.call("propose_mapping_change", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "because of this",
                                            "changes": [{"clause_id": CL_A2, "op": "remove"}]})
    assert "not mapped" in bad["error"]


def test_profile_fact_is_saved_and_undone(env):
    s, cfg = env
    t = ToolBox(s, ENTITY_A)
    t.call("company_profile", {})
    r = t.call("propose_profile_fact", {"fact": "We do not offer consumer credit scoring.", "reason": "stated by the user"})
    assert r["after"]["fact"].startswith("We do not")
    A.confirm(cfg, s, ENTITY_A, t.proposed[0])
    profile = ToolBox(s, ENTITY_A).call("company_profile", {})
    assert [f["text"] for f in profile["known_facts"]] == ["We do not offer consumer credit scoring."]
    A.undo(cfg, s, ENTITY_A, t.proposed[0])
    assert ToolBox(s, ENTITY_A).call("company_profile", {})["known_facts"] == []
    again = t.call("propose_profile_fact", {"fact": "We do not offer consumer credit scoring."})
    assert "error" not in again


def test_extraction_report_goes_to_the_queue(env):
    s, cfg = env
    t = box(s)
    t.call("propose_extraction_report", {"obligation_id": OBL, "note": "The obligation merges two separate duties."})
    A.confirm(cfg, s, ENTITY_A, t.proposed[0])
    assert s.q1("SELECT status, company_id FROM extraction_reports")["company_id"] == "A"
    A.undo(cfg, s, ENTITY_A, t.proposed[0])
    assert s.q1("SELECT status FROM extraction_reports")["status"] == "withdrawn"


def test_tenant_and_role_isolation(env):
    s, cfg = env
    t = box(s)
    t.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to a bank like us"})
    aid = t.proposed[0]
    assert A.get(s, ENTITY_B, aid) is None                                         # B cannot even see it
    with pytest.raises(A.ActionError):
        A.confirm(cfg, s, ENTITY_B, aid)
    tb_b = ToolBox(s, ENTITY_B)
    tb_b.call("clause_coverage", {"doc_version_id": DOC_B, "obligation_id": OBL})
    r = tb_b.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to a bank like us"})
    assert "error" in r                                                            # another company's document
    reg = ToolBox(s, REGULATOR)
    assert "propose_dismiss_flag" not in reg.tools                                 # a regulator gets no write tools
    assert "error" in reg.call("propose_dismiss_flag", {})


def test_a_published_version_cannot_be_changed(env):
    s, _ = env
    s.x("UPDATE document_versions SET status='published' WHERE id=?", (DOC_A,))
    r = box(s).call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to us"})
    assert "published" in r["error"]


def test_a_new_proposal_replaces_the_pending_one_and_the_cap_holds(env):
    s, _ = env
    t = box(s)
    for _ in range(2):
        t.call("propose_dismiss_flag", {"doc_version_id": DOC_A, "obligation_id": OBL, "reason": "does not apply to a bank like us"})
    assert [r["status"] for r in s.q("SELECT status FROM agent_actions ORDER BY id")] == ["cancelled", "pending"]
    t2 = box(s)
    for i in range(5):
        t2.call("propose_profile_fact", {"fact": f"A different fact number {i}"})
    assert len(t2.proposed) == A.MAX_PER_TURN


def test_agent_turn_returns_the_cards_it_created(env):
    s, cfg = env
    script = Script(tool("propose_dismiss_flag", doc_version_id=DOC_A, obligation_id=OBL,
                         reason="The obligation does not apply to us."),
                    answer("I propose dismissing the flag. Please confirm on the card."))
    lcfg, llm = make_llm(s, cfg.path("runs").parent, script)
    r = run_turn(lcfg, s, llm, ENTITY_A, {"view": "analysis", "version_id": DOC_A, "obligation_id": OBL}, "it does not apply")
    assert len(r.actions) == 1 and s.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "open"


# ----------------------------------------------------------------------------------------------- redrafting (step 4)
def proposed_text(s):
    return s.q1("SELECT proposed_text, flags FROM proposals WHERE obligation_id=?", (OBL,))


def test_wording_is_proposed_then_lands_in_the_suggestion_and_undo_restores_it(env):
    s, cfg = env
    t = box(s)
    r = t.call("propose_wording", {"doc_version_id": DOC_A, "obligation_id": OBL, "explanation": "keeps our terms; quarterly",
                                   "text": "ჩვენი პოლიტიკა: რისკის შეფასება ყოველკვარტალურად."})
    assert r["status"] == "waiting_for_the_user_to_confirm" and proposed_text(s)["proposed_text"] == "new text"
    aid = t.proposed[0]
    A.confirm(cfg, s, ENTITY_A, aid)
    row = proposed_text(s)
    assert row["proposed_text"].endswith("ყოველკვარტალურად.") and "chat_redraft" in row["flags"]
    assert s.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "open"        # still the user's to accept
    A.undo(cfg, s, ENTITY_A, aid)
    assert proposed_text(s)["proposed_text"] == "new text"


def test_a_hand_edit_is_replaced_and_comes_back_on_undo(env):
    s, cfg = env
    from kg.services import edits as E
    pid = s.q1("SELECT id FROM proposals")["id"]
    E.save_edit(s, ENTITY_A, DOC_A, f"clause:{CL_A}", "my own wording", [pid], "user")
    t = box(s)
    t.call("propose_wording", {"doc_version_id": DOC_A, "obligation_id": OBL, "explanation": "keeps our terms; quarterly",
                               "text": "ჩვენი პოლიტიკა: რისკის შეფასება ყოველკვარტალურად."})
    assert pending_row(s, t.proposed[0])["before"]["replaces_edit"] == "my own wording"
    A.confirm(cfg, s, ENTITY_A, t.proposed[0])
    assert f"clause:{CL_A}" not in E.get_edits(s, DOC_A)
    A.undo(cfg, s, ENTITY_A, t.proposed[0])
    assert E.get_edits(s, DOC_A)[f"clause:{CL_A}"]["text"] == "my own wording"


def test_wording_rules(env):
    s, _ = env
    t = box(s)
    base = {"doc_version_id": DOC_A, "obligation_id": OBL, "explanation": "keeps our terms; quarterly"}
    s.x("UPDATE proposals SET proposed_text=?", ("ჩვენი პოლიტიკა: რისკის შეფასება ყოველკვარტალურად.",))
    assert "same wording" in t.call("propose_wording", {**base, "text": "ჩვენი  პოლიტიკა: რისკის შეფასება ყოველკვარტალურად."})["error"]
    assert "complete" in t.call("propose_wording", {**base, "text": "short"})["error"]
    r = t.call("propose_wording", {**base, "text": "ჩვენ ვაფასებთ რისკს ყოველკვარტალურად, 24 საათში."})
    assert pending_row(s, t.proposed[0])["after"]["ungrounded_numbers"] == ["24"]       # warned, not blocked
    t2 = box(s)
    t2.user_text = "do not commit us to 24 hours"
    t2.call("propose_wording", {**base, "text": "ჩვენ ვაფასებთ რისკს ყოველკვარტალურად, 24 საათში."})
    assert pending_row(s, t2.proposed[0])["after"]["ungrounded_numbers"] == []          # the user said it
    s.x("UPDATE tasks SET status='accepted' WHERE key='k'")
    assert "accepted" in box(s).call("propose_wording", {**base, "text": "ჩვენ ვაფასებთ რისკს ყოველთვიურად."})["error"]


def test_a_new_hand_edit_makes_the_wording_proposal_stale(env):
    s, cfg = env
    from kg.services import edits as E
    t = box(s)
    t.call("propose_wording", {"doc_version_id": DOC_A, "obligation_id": OBL, "explanation": "keeps our terms; quarterly",
                               "text": "ჩვენი პოლიტიკა: რისკის შეფასება ყოველკვარტალურად."})
    pid = s.q1("SELECT id FROM proposals")["id"]
    E.save_edit(s, ENTITY_A, DOC_A, f"clause:{CL_A}", "typed meanwhile", [pid], "user")
    with pytest.raises(A.ActionError, match="changed"):
        A.confirm(cfg, s, ENTITY_A, t.proposed[0])


def test_a_proposed_section_can_be_read_pointed_at_and_stays_in_its_tenant(env):
    s, _ = env
    pid = s.x("INSERT INTO proposals(doc_version_id,obligation_id,kind,clause_id,proposed_text,heading,explanation,created_at)"
              " VALUES(?,?,?,?,?,?,?,?)", (DOC_A, OBL_GAP2, "add_section", CL_A, "ახალი მუხლის ტექსტი", "ახალი მუხლი", "why",
                                           "2026-01-01"))
    t = ToolBox(s, ENTITY_A)
    r = t.call("read_text", {"id": f"prop:{pid}"})
    assert "ახალი მუხლის ტექსტი" in r["text"] and f"prop:{pid}" in t.seen
    assert "error" in ToolBox(s, ENTITY_B).call("read_text", {"id": f"prop:{pid}"})
    from kg.agent.context import from_payload, validate
    from kg.agent.tools import Scope
    ctx, notes = validate(Scope(s, ENTITY_A), from_payload({"view": "analysis", "version_id": DOC_A,
                                                            "chips": [{"type": "proposal", "id": f"prop:{pid}"}]}))
    assert [c.id for c in ctx.chips] == [f"prop:{pid}"] and not notes
    ctx, notes = validate(Scope(s, ENTITY_B), from_payload({"chips": [{"type": "proposal", "id": f"prop:{pid}"}]}))
    assert ctx.chips == [] and notes
