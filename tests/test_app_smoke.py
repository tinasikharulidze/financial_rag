import json
from pathlib import Path
from streamlit.testing.v1 import AppTest
from test_agent import ENTITY_A, Script, answer, make_llm, store  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def _run(store, tmp_path, script, **state):
    store.x("INSERT OR IGNORE INTO users(id,email,name,password_hash,user_type,company_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (1, "a@x", "a", "x", "entity", "A", "2026-01-01"))
    cfg, llm = make_llm(store, tmp_path, script)
    cfg["ui"] = {"extra_pages": False}
    cfg["entity_types"] = ["commercial_bank"]
    at = AppTest.from_file(str(ROOT / "app" / "kg_app.py"), default_timeout=60)
    at.session_state["_ctx"] = (cfg, store)
    at.session_state["_llm_factory"] = lambda c, s, run_id=None: llm
    at.session_state["user_id"] = 1
    at.session_state["_test_page"] = "docs"
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


def test_chat_is_collapsed_by_default_and_opens_with_the_toggle(store, tmp_path):
    at = _run(store, tmp_path, Script(answer("hi")))
    assert not at.exception, at.exception
    assert not at.chat_input                                   # collapsed: only the slim bar (its button)
    assert [b for b in at.button if b.key == "chat_toggle"]
    at.button(key="chat_toggle").click().run()
    assert not at.exception, at.exception
    assert at.chat_input and at.session_state["chat_open"]     # open: the panel is a column
    at.button(key="chat_close").click().run()
    assert not at.chat_input


def test_open_panel_answers_inside_the_real_app(store, tmp_path):
    at = _run(store, tmp_path, Script(answer("Hello from the copilot")), chat_open=True)
    assert not at.exception, at.exception
    at.chat_input[0].set_value("hi there").run()
    assert not at.exception, at.exception
    assert any("Hello from the copilot" in str(getattr(x, "value", "")) for x in at.markdown)
