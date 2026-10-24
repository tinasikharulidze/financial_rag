"""The chat panel in Streamlit's AppTest: a scripted fake model, a hand-built two-company graph (see test_agent.py)."""
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from test_agent import (CL_A, DOC_A, DOC_B, ENTITY_A, ENTITY_B, OBL, PV2, SECRET_B, Script, answer, make_llm, store,  # noqa: F401
                        tool)

from kg.agent import storage

ROOT = Path(__file__).resolve().parents[1]


def panel_app():
    import sys

    import streamlit as st
    for p in st.session_state["_paths"]:
        if p not in sys.path:
            sys.path.insert(0, p)
    import common
    import view_chat
    cfg, store = common.get_ctx()
    user = common.current_user(store)
    st.session_state["page_now"] = st.session_state.get("_page", "review")
    view_chat.render(cfg, store, user)


def make_app(store, tmp_path, script, user, **state):
    # the users table is used by common.current_user -> accounts.get_user
    for u in (ENTITY_A, ENTITY_B):
        if not store.q1("SELECT 1 FROM users WHERE id=?", (u["id"],)):
            store.x("INSERT INTO users(id,email,name,password_hash,user_type,company_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (u["id"], u["email"], u["name"], "x", u["user_type"], u["company_id"], "2026-01-01"))
    cfg, llm = make_llm(store, tmp_path, script)
    at = AppTest.from_function(panel_app, default_timeout=30)
    at.session_state["_paths"] = [str(ROOT / "src"), str(ROOT / "app")]
    at.session_state["_ctx"] = (cfg, store)
    at.session_state["_llm_factory"] = lambda c, s, run_id=None: llm
    at.session_state["user_id"] = user["id"]
    for k, v in state.items():
        at.session_state[k] = v
    return at, llm


def test_panel_answers_with_citations_and_saves_the_thread(store, tmp_path):
    script = Script(tool("provision_text", act="1111", path="art1", as_of="2023-01-01"),
                    answer("Quarterly now.", citations=[PV2]))
    at, _ = make_app(store, tmp_path, script, ENTITY_A, review_version=DOC_A)
    at.run()
    assert not at.exception
    at.chat_input[0].set_value("What does art 1 say?").run()
    assert not at.exception
    assert any("Quarterly now." in str(getattr(x, "value", "")) for x in at.markdown)      # the reply is on screen
    tid = at.session_state["chat_thread"]
    msgs = storage.messages(store, ENTITY_A, tid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["context"]["version_id"] == DOC_A                      # the context snapshot is stored
    assert msgs[1]["trace"]["citations"][0]["id"] == PV2 and msgs[1]["trace"]["tools"] == ["provision_text"]


def test_panel_context_has_ids_only_and_open_flag_becomes_focus(store, tmp_path):
    script = Script(answer("ok"))
    at, _ = make_app(store, tmp_path, script, ENTITY_A, review_version=DOC_A, prop_dlg=OBL)
    at.run()
    at.chat_input[0].set_value("hello").run()
    sent = script.prompts[0][1]["content"]
    assert OBL in sent and DOC_A in sent and "ჩვენი პოლიტიკა" not in sent       # ids yes, document text no


def test_panel_has_no_suggested_questions(store, tmp_path):
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_A, review_version=DOC_A, prop_dlg=OBL, lang="en")
    at.run()
    assert not at.exception and not any(b.key.startswith("chat_sugg") for b in at.button)


def test_panel_other_user_cannot_see_the_thread(store, tmp_path):
    tid = storage.new_thread(store, ENTITY_A, DOC_A)
    storage.add_message(store, tid, "user", "private question")
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_B, chat_thread=tid)
    at.run()
    assert not at.exception
    assert "chat_thread" not in at.session_state                  # B does not get A's thread (a fresh one starts on the first question)
    assert "private question" not in json.dumps([m.value for m in at.markdown])


def test_foreign_company_text_never_reaches_the_model_through_the_panel(store, tmp_path):
    script = Script(tool("get_node", id="doc:B/policy@1"), answer("no access"))
    at, _ = make_app(store, tmp_path, script, ENTITY_A, review_version="doc:B/policy@1")
    at.run()
    at.chat_input[0].set_value("show me everything").run()
    assert SECRET_B not in json.dumps(script.prompts) and "doc:B/policy@1" not in script.prompts[0][1]["content"]


def md(at):
    return " ".join(str(getattr(x, "value", "")) for x in at.markdown)


def test_context_chips_are_readable_not_raw_ids(store, tmp_path):
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_A, review_version=DOC_A, prop_dlg=OBL, lang="en")
    at.run()
    assert not at.exception
    text = md(at)
    assert "cp-chip" in text and "Obligation" in text
    assert "policy · v1" not in text                                  # the document is on screen: no chip for it
    assert "obl:" not in text and "doc:A" not in text                 # ids stay for the machine
    assert any("Flag: " in e.label for e in at.expander)             # the popup's verdict/reasoning is shown


def test_messages_are_bubbles_user_right_assistant_left(store, tmp_path):
    at, _ = make_app(store, tmp_path, Script(answer("the reply")), ENTITY_A, review_version=DOC_A, lang="en")
    at.run()
    at.chat_input[0].set_value("my question").run()
    assert not at.exception
    css = md(at)
    assert '[class*="st-key-cpu_"]' in css and '[class*="st-key-cpa_"]' in css      # right / left bubble rules
    assert "my question" in css and "the reply" in css


def test_pointing_to_a_clause_adds_a_chip_and_focus(store, tmp_path):
    script = Script(answer("ok"))
    at, _ = make_app(store, tmp_path, script, ENTITY_A, review_version=DOC_A, lang="en")
    at.run()
    at.selectbox(key="chat_clause_pick").select(CL_A).run()
    at.button(key="chat_clause_add").click().run()
    assert at.session_state["chat_clause"] == CL_A
    assert "policy · § 1.1" in md(at)
    at.chat_input[0].set_value("what does this clause need?").run()
    sent = script.prompts[0][1]["content"]
    assert CL_A in sent and '"clause_id"' in sent                     # the clause is the focus of the conversation


def test_new_chat_button_and_history_for_this_document(store, tmp_path):
    other = storage.new_thread(store, ENTITY_A, DOC_B)                # a conversation about ANOTHER document
    storage.add_message(store, other, "user", "about the other document")
    old = storage.new_thread(store, ENTITY_A, DOC_A, "what did article 1 say before?")
    storage.add_message(store, old, "user", "what did article 1 say before?")
    storage.add_message(store, old, "assistant", "It said annual.", None, {"kind": "answer"})
    at, _ = make_app(store, tmp_path, Script(answer("new answer")), ENTITY_A, review_version=DOC_A, lang="en")
    at.run()
    assert not at.exception
    assert "chat_thread" not in at.session_state                     # no empty thread is created just by opening the panel
    keys = [b.key for b in at.button]
    assert f"chat_h{old}" in keys and f"chat_h{other}" not in keys     # the history shows only this document
    at.button(key=f"chat_h{old}").click().run()
    assert at.session_state["chat_thread"] == old and "It said annual." in md(at)
    at.button(key="chat_new").click().run()
    assert "chat_thread" not in at.session_state and "It said annual." not in md(at)


def test_history_follows_the_document_not_the_version(store):
    store.x("INSERT INTO document_versions(id,family_id,version,status,file_path,sha1,uploaded_at) VALUES(?,?,?,?,?,?,?)",
            ("doc:A/policy@2", "fam-A", "2", "draft", "x", "x", "2026-02-01"))
    tid = storage.new_thread(store, ENTITY_A, DOC_A, "q")
    storage.add_message(store, tid, "user", "q")
    assert [t["id"] for t in storage.threads_for(store, ENTITY_A, "doc:A/policy@2")] == [tid]    # v2 sees v1's chats
    assert storage.threads_for(store, ENTITY_B, "doc:A/policy@2") == []                         # and nobody else does


def test_a_thread_of_another_document_is_not_continued(store, tmp_path):
    tid = storage.new_thread(store, ENTITY_A, DOC_B)
    storage.add_message(store, tid, "user", "about B")
    storage.add_message(store, tid, "assistant", "answer about B", None, {})
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_A, review_version=DOC_A, chat_thread=tid, lang="en")
    at.run()
    assert not at.exception and "answer about B" not in md(at) and "different document" not in md(at)


def test_sources_show_only_quotes_and_clauses(store, tmp_path):
    from kg.agent import storage as st_
    tid = st_.new_thread(store, ENTITY_A, DOC_A, "q")
    st_.add_message(store, tid, "user", "q")
    cites = [{"id": OBL, "type": "Obligation", "label": "Model risk · art 1", "sub": "", "quote": None, "quote_ok": None},
             {"id": PV2, "type": "Provision", "label": "Model risk · art 2", "sub": "", "quote": "quoted words", "quote_ok": True},
             {"id": CL_A, "type": "Clause", "label": "policy · § 1.1", "sub": "", "quote": None, "quote_ok": None}]
    st_.add_message(store, tid, "assistant", "plain answer", None, {"kind": "answer", "citations": cites, "tools": ["get_node"]})
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_A, review_version=DOC_A, chat_thread=tid, lang="en")
    at.run()
    text = md(at)
    assert "Model risk · art 2" in text and "policy · § 1.1" in text and "Model risk · art 1" not in text
    assert any(b.label == "Show in document" for b in at.button)


def test_point_to_a_place_sits_in_the_context_line(store, tmp_path):
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_A, review_version=DOC_A, lang="en")
    at.run()
    assert not at.exception and any(b.key == "chat_clause_add" for b in at.button)      # the "point to a place" window is there


def test_panel_keeps_a_quota_error_in_the_conversation(store, tmp_path):
    from kg.llm import BudgetExhausted
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_A, review_version=DOC_A, lang="en")

    def broke(c, s, run_id=None):
        raise BudgetExhausted("daily limit")
    at.session_state["_llm_factory"] = broke
    at.run()
    at.chat_input[0].set_value("Does it apply?").run()
    assert not at.exception
    msgs = storage.messages(store, ENTITY_A, at.session_state["chat_thread"])
    assert [m["role"] for m in msgs] == ["user", "assistant"] and "daily limit" in msgs[1]["content"]
    at.run()                                                              # still there after a rerun
    assert any("daily limit" in str(getattr(x, "value", "")) for x in at.markdown)


def test_panel_card_confirm_and_undo(store, tmp_path):
    store.x("INSERT INTO tasks(key,company_id,doc_version_id,obligation_id,title,status,score,priority,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", ("k", "A", DOC_A, OBL, "t", "open", 0.5, "high", "2026-01-01", "2026-01-01"))
    script = Script(tool("propose_dismiss_flag", doc_version_id=DOC_A, obligation_id=OBL,
                         reason="The obligation does not apply to us."),
                    answer("I propose closing the flag. Please confirm on the card."))
    at, _ = make_app(store, tmp_path, script, ENTITY_A, review_version=DOC_A, prop_dlg=OBL, lang="en")
    at.run()
    at.chat_input[0].set_value("it does not apply to us").run()
    assert not at.exception
    assert store.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "open"           # a proposal changes nothing
    ok = [b for b in at.button if b.label == "Confirm"]
    assert ok and any("Close the flag" in str(getattr(x, "value", "")) for x in at.markdown)
    ok[0].click().run()
    assert not at.exception and store.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "not_relevant"
    undo = [b for b in at.button if b.label == "Undo"]
    assert undo and not [b for b in at.button if b.label == "Confirm"]
    undo[0].click().run()
    assert store.q1("SELECT status FROM tasks WHERE key='k'")["status"] == "open"
    row = store.q1("SELECT thread_id, user_message_id, message_id FROM agent_actions")      # the audit link to the question
    assert row["thread_id"] and row["user_message_id"] and row["message_id"]


def test_point_to_a_place_lists_proposed_new_sections(store, tmp_path):
    store.x("INSERT INTO tasks(key,company_id,doc_version_id,obligation_id,title,status,score,priority,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)", ("k", "A", DOC_A, OBL, "t", "open", 0.5, "high", "2026-01-01", "2026-01-01"))
    pid = store.x("INSERT INTO proposals(doc_version_id,obligation_id,kind,clause_id,proposed_text,heading,explanation,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?)", (DOC_A, OBL, "add_section", CL_A, "ახალი მუხლის ტექსტი", "ახალი მუხლი", "why", "x"))
    at, _ = make_app(store, tmp_path, Script(answer("ok")), ENTITY_A, review_version=DOC_A, lang="en")
    at.run()
    assert not at.exception
    sel = next(x for x in at.selectbox if x.key == "chat_clause_pick")
    assert len(sel.options) == 2 and sel.options[1].startswith("＋ proposed: ახალი მუხლი")   # right after its clause
    sel.select(sel.options[-1])
    next(b for b in at.button if b.key == "chat_clause_add").click().run()
    assert not at.exception
    assert [c["id"] for c in at.session_state["chat_chips"]] == [f"prop:{pid}"]
    assert at.session_state.get("chat_clause") is None                          # it is a chip, not a clause focus
