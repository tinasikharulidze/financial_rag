"""The Streamlit app (Streamlit's AppTest, fake model) and the pure review model."""
from pathlib import Path

import sys

import pytest
from fake_llm import FakeGemini

from kg.company import profile as prof
from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM
from kg.mapping.pipeline import map_document
from kg.mapping.proposals import propose_document
from kg.runlog import Run
from kg.services import accounts, tasks as T
from kg.services.demo import seed_demo

st_testing = pytest.importorskip("streamlit.testing.v1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
APP = str(Path(__file__).resolve().parents[1] / "app" / "kg_app.py")
NOW = "2025-01-01"


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    run = Run(store, cfg.path("runs"), "t-ui")
    run.__enter__()
    llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
    extract_obligations_v2(cfg, store, run, llm)
    docs = seed_demo(cfg, store)["documents"]
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    v = next(d for d in docs if "POL-RM-014" in d)
    prof.build_profile(cfg, store, run, llm, v)
    prof.confirm_profile(store, owner, v, domains=["model_risk"])
    map_document(cfg, store, run, llm, v, now=NOW)
    propose_document(cfg, store, run, llm, v)
    T.sync_tasks(cfg, store, run, v)
    yield cfg, store, v, owner, fake
    run.__exit__(None, None, None)


def app(env, email=None, page=None, draft=False, all_status=True):
    cfg, store, v, owner, fake = env
    if draft:                                  # the demo seeds published documents; uploads start as drafts
        store.x("UPDATE document_versions SET status='draft' WHERE id=?", (v,))
    at = st_testing.AppTest.from_file(APP, default_timeout=60)
    at.session_state["_test_page"] = "docs"             # the current page between runs (see kg_app.main)
    at.session_state["_ctx"] = (cfg, store)
    at.session_state["_llm_factory"] = lambda c, s, rid: LLM(c, s, rid, backend=fake)
    if email:
        at.session_state["user_id"] = store.q1("SELECT id FROM users WHERE email=?", (email,))["id"]
    if page:
        at.session_state["_test_page"] = page
        at.session_state["review_version"] = v
        at.session_state["company_id"] = "bank-a"
        if page == "review" and all_status:     # the demo seeds published versions; the page starts on drafts
            at.session_state["rf_status"] = []
    return at.run()


def test_review_model(env):
    from review_lib import build_review, render_html
    cfg, store, v, owner, fake = env
    m = build_review(cfg, store, v)
    assert m["cards"] and m["proposals"] and sum(m["counts"].values()) > 0
    flagged = [u for u in m["units"] if u["cards"]]
    assert flagged
    h = render_html(m)
    assert "cds" in h and h.count("class='cl") >= len(flagged)
    assert all(c["verdict"] in ("partial", "contradicts", "none", "full") for u in flagged for c in u["cards"])


def test_login_screen_and_bad_password(env):
    at = app(env)
    assert not at.exception and len(at.tabs) == 2
    at.text_input(key="li_email").input("bank@demo.ge")
    at.text_input(key="li_pw").input("wrong")
    at.button(key="li_btn").click().run()
    assert at.error and not at.exception
    at.text_input(key="li_pw").input("demo1234")
    at.button(key="li_btn").click().run()
    assert not at.exception and at.session_state["user_id"]
    assert at.session_state["page_now"] == "docs" and not at.sidebar.radio      # menu = navigation bar on top


@pytest.mark.parametrize("email,page", [("bank@demo.ge", "docs"), ("bank@demo.ge", "review"),
                                        ("bank@demo.ge", "board"),
                                        ("nbg@demo.ge", "companies"), ("nbg@demo.ge", "board"),
                                        ("nbg@demo.ge", "review")])
def test_pages_render(env, email, page):
    at = app(env, email, page)
    assert not at.exception, [e.value for e in at.exception]


def test_accept_and_reject_flow(env):
    sys.path.insert(0, APP.rsplit("/", 1)[0])
    from review_lib import build_review
    from view_review import handle_action
    cfg, store, v, owner, fake = env
    store.x("UPDATE document_versions SET status='draft' WHERE id=?", (v,))
    m = build_review(cfg, store, v)
    first, second = [oid for oid, t in m["tasks"].items() if t["status"] == "open" and t["proposal_id"]][:2]
    state = {}
    assert handle_action(cfg, store, owner, m, v, {"action": "accept", "oid": first}, state) == "accept"
    assert store.q1("SELECT status FROM tasks WHERE obligation_id=? AND doc_version_id=?", (first, v))["status"] == \
        "accepted"
    m = build_review(cfg, store, v)
    assert handle_action(cfg, store, owner, m, v, {"action": "undo", "oid": first}, state) == "undo"
    assert store.q1("SELECT status FROM tasks WHERE obligation_id=? AND doc_version_id=?", (first, v))["status"] == "open"
    assert handle_action(cfg, store, owner, m, v, {"action": "reject", "oid": second}, state) == "reject"
    assert state == {"prop_dlg": second, "prop_mode": "reject"}
    # the reject window: a reason is required, then the change leaves the proposed version
    at = app(env, "bank@demo.ge", "review", draft=True)
    at.session_state["prop_dlg"], at.session_state["prop_mode"] = second, "reject"
    at.run()
    at.text_input(key="rs" + second).input("not for us")
    at.button(key="nr" + second).click().run()
    assert not at.exception and "prop_dlg" not in at.session_state
    assert store.q1("SELECT status FROM tasks WHERE obligation_id=? AND doc_version_id=?", (second, v))["status"] == \
        "not_relevant"
    from review_lib import revised_view
    assert second not in {o for x in revised_view(build_review(cfg, store, v))["units"]
                          for sl in ([x["change"]] if x["change"] else []) + x["inserts"] for o in sl["oids"]}


LABELS = {k: k for k in ("accept", "reject", "details", "undo", "accepted", "obligation", "new_docs", "edit",
                         "save_accept", "cancel", "preview_merge", "overlap_warn", "user_edited", "model_merged")}
LABELS["merged_from"] = "{n} obligations"


def test_revised_html_buttons(env):
    from review_lib import build_review, revised_html
    cfg, store, v, owner, fake = env
    m = build_review(cfg, store, v)
    h = revised_html(m, LABELS, can_edit=True)
    assert "data-action='accept'" in h and "data-action='reject'" in h and "class='act'" in h
    assert "data-action='edit'" in h and "data-action='save'" in h and "data-plain=" in h
    ro = revised_html(m, LABELS, can_edit=False)
    assert "data-action='accept'" not in ro and "data-action='details'" in ro          # regulator: explanation only
    assert "data-action='edit'" not in ro
    at = app(env, "bank@demo.ge", "review", draft=True)
    assert not at.exception


def test_revised_view_model(env):
    from review_lib import build_review, revised_view
    cfg, store, v, owner, fake = env
    m = build_review(cfg, store, v)
    rv = revised_view(m)
    rows = store.q("SELECT kind, clause_id FROM proposals WHERE doc_version_id=?", (v,))
    slots = len({r["clause_id"] for r in rows if r["kind"] == "adjust_clause"}) + \
        sum(1 for r in rows if r["kind"] != "adjust_clause")
    assert rv["count"] == slots and rv["accepted"] == 0 and len(rv["units"]) == len(m["units"])
    assert all(x["change"]["kind"] == "adjust_clause" for x in rv["units"] if x["change"])


def _two_on_one_clause(store, v):
    """Make two proposals change the same clause (the demo has one per clause) -> (clause_id, [oid, oid])."""
    rows = store.q("SELECT p.id, p.obligation_id, p.clause_id, p.original_text FROM proposals p JOIN tasks t ON "
                   "t.doc_version_id=p.doc_version_id AND t.obligation_id=p.obligation_id WHERE p.doc_version_id=? "
                   "AND p.kind='adjust_clause' AND t.status='open' ORDER BY p.id", (v,))
    a, b = rows[0], rows[1]
    store.x("UPDATE proposals SET clause_id=?, original_text=?, proposed_text=? WHERE id=?",
            (a["clause_id"], a["original_text"], (a["original_text"] or "") + " მეორე დამატება.", b["id"]))
    return a["clause_id"], [a["obligation_id"], b["obligation_id"]]


def test_merge_words():
    from kg.services.edits import merge_words
    m = merge_words("a b c d e", [("1", "a B c d e"), ("2", "a b c d e f"), ("3", "a b X d e")])
    assert m["text"] == "a B X d e f" and not m["overlap"]
    m = merge_words("a b c", [("1", "a b c z"), ("2", "a b c y")])
    assert m["text"] == "a b c z y" and m["overlap"]
    assert merge_words("a b", [("1", "a b c"), ("2", "a b c")])["text"] == "a b c"      # the same change once


def test_several_proposals_are_one_block(env):
    from review_lib import build_review, revised_html, revised_view
    from view_review import handle_action
    from kg.services.edits import get_edits
    from kg.services.revise import build_revised
    cfg, store, v, owner, fake = env
    store.x("UPDATE document_versions SET status='draft' WHERE id=?", (v,))
    cid, oids = _two_on_one_clause(store, v)
    m = build_review(cfg, store, v)
    sl = next(x["change"] for x in revised_view(m)["units"] if x["unit"]["id"] == cid)
    assert sorted(sl["oids"]) == sorted(oids) and sl["source"] == "preview" and "მეორე დამატება." in sl["text"]
    h = revised_html(m, LABELS, can_edit=True)
    assert h.count(f"data-slot='clause:{cid}'") == 1 and "2 obligations" in h
    # accepting the block accepts both and keeps the text that was shown
    assert handle_action(cfg, store, owner, m, v, {"action": "accept", "oids": "|".join(oids),
                                                   "slot": f"clause:{cid}"}, {}) == "accept"
    assert {r["status"] for r in store.q("SELECT status FROM tasks WHERE doc_version_id=? AND obligation_id IN (?,?)",
                                         (v, *oids))} == {"accepted"}
    assert get_edits(store, v)[f"clause:{cid}"]["source"] == "preview"
    ids = [t["proposal_id"] for t in build_review(cfg, store, v)["tasks"].values() if t["status"] == "accepted"]
    n = fake.calls.get("merge", 0)
    with_run(env, lambda run, llm: build_revised(cfg, store, run, llm, owner, v, ids))
    assert fake.calls.get("merge", 0) == n                     # the stored wording is used, no merge request


def test_merge_pending_with_model(env):
    from review_lib import build_review, revised_view
    from kg.services.revise import merge_pending, pending_merges
    cfg, store, v, owner, fake = env
    cid, oids = _two_on_one_clause(store, v)
    pending = pending_merges(store, v)
    assert cid in pending
    out = with_run(env, lambda run, llm: merge_pending(cfg, store, run, llm, v))
    assert out["merged"] == len(pending) and not pending_merges(store, v)
    sl = next(x["change"] for x in revised_view(build_review(cfg, store, v))["units"] if x["unit"]["id"] == cid)
    assert sl["source"] == "merge" and "[გაერთიანებული" in sl["text"] and len(sl["notes"]) == len(sl["oids"])


def test_user_edits_the_text_then_accepts(env):
    from review_lib import build_review, revised_view
    from view_review import handle_action
    from kg.services.revise import build_revised
    cfg, store, v, owner, fake = env
    store.x("UPDATE document_versions SET status='draft' WHERE id=?", (v,))
    m = build_review(cfg, store, v)
    sl = next(x["change"] for x in revised_view(m)["units"] if x["change"])
    text = "ახალი ტექსტი, რომელიც მომხმარებელმა შეასწორა."
    assert handle_action(cfg, store, owner, m, v, {"action": "save", "oids": "|".join(sl["oids"]),
                                                   "slot": sl["slot"], "text": text}, {}) == "save"
    assert store.q1("SELECT status FROM tasks WHERE doc_version_id=? AND obligation_id=?",
                    (v, sl["oids"][0]))["status"] == "accepted"
    m = build_review(cfg, store, v)
    new = next(x["change"] for x in revised_view(m)["units"] if x["change"] and x["change"]["slot"] == sl["slot"])
    assert new["text"] == text and new["source"] == "user" and new["status"] == "accepted"
    assert store.q1("SELECT kind FROM user_decisions WHERE kind='edit_proposal'")
    ids = [t["proposal_id"] for t in m["tasks"].values() if t["status"] == "accepted"]
    out = with_run(env, lambda run, llm: build_revised(cfg, store, run, llm, owner, v, ids))
    assert any(c["new"] == text for c in out["changes"])
    # undo keeps the user's text (the block goes back to "open" with it)
    handle_action(cfg, store, owner, m, v, {"action": "undo", "oids": "|".join(sl["oids"])}, {})
    again = next(x["change"] for x in revised_view(build_review(cfg, store, v))["units"]
                 if x["change"] and x["change"]["slot"] == sl["slot"])
    assert again["text"] == text and again["status"] == "open"


def with_run(env, fn):
    cfg, store, v, owner, fake = env
    with Run(store, cfg.path("runs"), "t-ui2") as run:
        return fn(run, LLM(cfg, store, run.id, backend=fake))


def test_regulator_is_read_only(env):
    at = app(env, "nbg@demo.ge", "review")
    assert not at.exception
    assert not [b for b in at.button if b.key and b.key[:2] in ("ac", "nr", "aa")]
    assert not [b for b in at.button if b.key in ("pub", "disc", "br")]


def test_publish_blocked_by_open_tasks(env):
    cfg, store, v, owner, fake = env
    at = app(env, "bank@demo.ge", "review", draft=True)
    assert any("pubforce" == c.key for c in at.checkbox)
    at.button(key="pub").click().run()
    assert store.q1("SELECT status FROM document_versions WHERE id=?", (v,))["status"] == "draft"
    at.checkbox(key="pubforce").check().run()
    at.button(key="pub").click().run()
    assert store.q1("SELECT status FROM document_versions WHERE id=?", (v,))["status"] == "published"


def test_register_new_company(env):
    cfg, store, v, owner, fake = env
    at = app(env)
    at.text_input(key="rg_name").input("Test User")
    at.text_input(key="rg_email").input("t@x.ge")
    at.text_input(key="rg_pw").input("secret12")
    at.text_input(key="rg_cname").input("Beta Bank")
    at.button(key="rg_btn").click().run()
    assert not at.exception
    assert store.q1("SELECT company_id FROM users WHERE email='t@x.ge'")["company_id"] == "beta-bank"


# ----------------------------------------------------------------------------------------- documents page (tiles)
def _tiles(at):
    return [b for b in at.button if b.key and b.key.startswith("tile_")]


def test_document_tiles_search_and_filters(env):
    at = app(env, "bank@demo.ge", "docs")
    assert not at.exception and len(_tiles(at)) == 7
    at.text_input(key="doc_q").input("EG-01").run()
    assert len(_tiles(at)) == 1
    at.text_input(key="doc_q").input("").run()
    at.multiselect(key="doc_status").select("draft").run()
    assert len(_tiles(at)) == 0 and not at.exception                    # every demo document is published
    at.multiselect(key="doc_status").unselect("draft").select("published").run()
    assert len(_tiles(at)) == 7
    at.selectbox(key="doc_sort").select("tasks").run()
    assert not at.exception


def test_open_tile_switches_to_review(env):
    cfg, store, v, owner, fake = env
    at = app(env, "bank@demo.ge", "docs")
    next(b for b in _tiles(at) if "POL-RM-014" in b.key).click().run()
    assert not at.exception and at.session_state["page_now"] == "review" and at.session_state["review_version"] == v


def test_upload_window_opens_with_the_upload_step(env):
    at = app(env, "bank@demo.ge", "docs")
    at.button(key="open_upload").click().run()
    assert not at.exception and at.session_state["dlg"] == {"step": "upload"}
    assert any(r.key == "up_mode" for r in at.radio)
    at.button(key="up_cancel").click().run()
    assert "dlg" not in at.session_state


def test_upload_window_profile_step_confirms_and_closes(env):
    cfg, store, v, owner, fake = env
    eg = "doc:bank-a/EG-01@1.0"
    store.x("UPDATE document_versions SET status='draft' WHERE id=?", (eg,))
    at = st_testing.AppTest.from_file(APP, default_timeout=60)
    at.session_state["_test_page"] = "docs"             # the current page between runs (see kg_app.main)
    at.session_state["_ctx"] = (cfg, store)
    at.session_state["_llm_factory"] = lambda c, s, rid: LLM(c, s, rid, backend=fake)
    at.session_state["user_id"] = owner["id"]
    at.session_state["_test_page"] = "docs"
    at.session_state["dlg"] = {"step": "profile", "version": eg, "info": "ok"}
    n0 = fake.calls["doc_profile"]
    at.run()
    assert not at.exception and fake.calls["doc_profile"] == n0 + 1       # the profile was built in the window
    at.button(key="pf_confirm").click().run()
    assert not at.exception and "dlg" not in at.session_state
    from kg.company import profile as P
    assert P.get_profile(store, eg)["confirmed"]
    assert any("EG-01" in s.value for s in at.success)


def test_confirm_in_the_window_starts_the_background_check(env):
    cfg, store, v, owner, fake = env
    eg = "doc:bank-a/EG-01@1.0"
    started = []
    at = st_testing.AppTest.from_file(APP, default_timeout=60)
    at.session_state["_test_page"] = "docs"             # the current page between runs (see kg_app.main)
    at.session_state["_ctx"] = (cfg, store)
    at.session_state["_llm_factory"] = lambda c, s, rid: LLM(c, s, rid, backend=fake)
    at.session_state["_job_launcher"] = lambda c, s, u, vid: started.append(vid)
    at.session_state["user_id"] = owner["id"]
    at.session_state["_test_page"] = "docs"
    at.session_state["dlg"] = {"step": "profile", "version": eg}
    at.run()
    at.button(key="pf_confirm").click().run()
    assert not at.exception and started == [eg] and "dlg" not in at.session_state


def test_tile_shows_a_running_check(env):
    import os
    cfg, store, v, owner, fake = env
    store.x("INSERT INTO check_jobs(version_id,status,pid,actor,started_at) VALUES(?,?,?,?,?)",
            (v, "running", os.getpid(), owner["id"], "2026-10-21T12:00:00"))
    at = app(env, "bank@demo.ge", "docs")
    assert not at.exception
    assert any("⏳" in m.value for m in at.markdown)
    rv = app(env, "bank@demo.ge", "review")
    assert not rv.exception and any("ნაწილობრივია" in i.value for i in rv.info)


def test_account_menu_company_profile_and_hidden_pages(env):
    at = app(env, "bank@demo.ge", "docs")
    assert not at.exception and not at.sidebar.radio and not [b for b in at.sidebar.button]
    at.button(key="menu_company").click().run()
    assert not at.exception and at.session_state["page_now"] == "company"
    assert any(f.label for f in at.text_input)                                  # the company form is shown
    at.session_state["goto"] = "board"
    at.run()
    assert not at.exception and at.session_state["page_now"] == "board"
    at.button(key="menu_logout").click().run()
    assert "user_id" not in at.session_state


def test_regulator_menu(env):
    at = app(env, "nbg@demo.ge", "companies")
    assert not at.exception and at.session_state["page_now"] == "companies"
    assert not [b for b in at.button if b.key == "menu_company"]


def test_review_page_filters(env):
    cfg, store, v, owner, fake = env
    at = app(env, "bank@demo.ge", "review")
    assert not at.exception and len(at.selectbox(key="rf_pick").options) == 7
    at.text_input(key="rf_q").input("EG-01").run()
    assert not at.exception and len(at.selectbox(key="rf_pick").options) == 1
    assert at.session_state["review_version"] == "doc:bank-a/EG-01@1.0"
    at.text_input(key="rf_q").input("").run()
    at.multiselect(key="rf_status").select("draft").run()
    assert not at.exception and any(i.value for i in at.info)            # nothing matches: a message, no error


def test_review_obligation_table_and_no_sort(env):
    from review_lib import build_review, table_html
    cfg, store, v, owner, fake = env
    m = build_review(cfg, store, v)
    n_tri = store.q1("SELECT COUNT(*) n FROM triage WHERE doc_version_id=?", (v,))["n"]
    assert len(m["table"]) == n_tri and all(r["source"] for r in m["table"])
    assert any(r["url"] and r["url"].startswith("https://matsne.gov.ge") for r in m["table"])
    assert all(r["verdict"] is None for r in m["table"] if r["relevance"] == "irrelevant")
    labels = {"n": "#", "a": "A", "b": "B", "c": "C", "d": "D", "e": "E"}
    h = table_html(m["table"], labels, {"relevant": "r", "possibly": "p", "irrelevant": "i"},
                   {k: k or "-" for k in ("contradicts", "none", "partial", "full", "undecided", None)}, {}, {})
    assert h.count("<tr>") == n_tri + 1 and "<td class='n' data-k='1'>1</td>" in h and "white-space:normal" in h
    assert "matsne.gov.ge" in h and "addEventListener('click'" in h                  # links + sortable headers
    at = app(env, "bank@demo.ge", "review")
    assert not at.exception and not [s for s in at.selectbox if s.key == "rf_sort"]
    assert any(c.value == f"{n_tri} / {n_tri}" for c in at.caption)
    assert [b for b in at.get("download_button")]
    at.multiselect(key="ot_rel").select("irrelevant").run()
    n_irr = store.q1("SELECT COUNT(*) n FROM triage WHERE doc_version_id=? AND relevance='irrelevant'", (v,))["n"]
    assert not at.exception and any(c.value == f"{n_irr} / {n_tri}" for c in at.caption)


def test_contradicts_verdict_with_partial_clause_is_shown_red(env):
    from review_lib import build_review, render_html
    cfg, store, v, owner, fake = env
    row = store.q1("SELECT obligation_id FROM coverage WHERE doc_version_id=? AND verdict='partial'", (v,))
    oid = row["obligation_id"]
    store.x("UPDATE coverage SET verdict='contradicts' WHERE doc_version_id=? AND obligation_id=?", (v, oid))
    m = build_review(cfg, store, v)
    roles = {c["role"] for u in m["units"] for c in u["cards"] if c["oid"] == oid}
    assert "contradicts" in roles and "partial" not in roles
    assert " bad" in render_html(m, roles=["contradicts"], relevances=["relevant", "possibly"])


def test_document_view_filters(env):
    from review_lib import build_review, render_html
    cfg, store, v, owner, fake = env
    m = build_review(cfg, store, v)
    every = render_html(m)
    none_shown = render_html(m, roles=[], relevances=["relevant"])
    assert "class='cds'" in every and "class='cds'" not in none_shown          # nothing selected: plain text
    only_full = render_html(m, roles=["full"], relevances=["relevant", "possibly"])
    assert " bad" not in only_full and " part" not in only_full
    at = app(env, "bank@demo.ge", "review")
    assert not at.exception
    assert at.session_state["dv_roles"] == ["contradicts", "anchor"] and at.session_state["dv_rel"] == ["relevant"]


def test_language_switch(env):
    import i18n_en
    at = app(env, "bank@demo.ge", "docs")
    assert not at.exception and at.session_state["page_now"] == "docs"
    assert at.button(key="open_upload").label.endswith("დოკუმენტის ატვირთვა")
    at.session_state["lang"] = "en"
    at.run()
    assert not at.exception and at.button(key="open_upload").label.endswith(i18n_en.T["upload_new"])
    assert any(i18n_en.T["status_published"] in m.value for m in at.markdown)          # tiles in English too
    from i18n_ka import T, tr, VERDICT
    assert T["title"] == "გონი" and tr(VERDICT, "full") == "სრულად"                     # outside a session: Georgian


def test_colours_filters_and_new_documents_tab(env):
    from review_lib import build_review, revised_html, revised_view
    cfg, store, v, owner, fake = env
    m = build_review(cfg, store, v)
    rv = revised_view(m)
    doc = revised_html(m, LABELS, True)
    assert "class='chg bad" in doc or "class='chg part" in doc or "class='chg anchor" in doc     # verdict colours
    only_red = revised_html(m, LABELS, True, roles=["contradicts"])
    assert "class='chg part" not in only_red and "class='chg anchor" not in only_red
    assert "class='chg" not in revised_html(m, LABELS, True, relevances=["irrelevant"])
    new = revised_html(m, LABELS, True, part="new")
    assert new.count("data-slot='new:") == len(rv["new_documents"])
    assert "data-slot='new:" not in doc


def test_board_groups_table_and_window(env):
    sys.path.insert(0, APP.rsplit("/", 1)[0])
    from board_lib import board_html, group
    from view_board import _labels, doc_info
    from kg.services import tasks as TK
    cfg, store, v, owner, fake = env
    rows = TK.list_tasks(cfg, store, owner, "bank-a")
    info = doc_info(store, {r["doc_version_id"] for r in rows})
    assert info[v]["direction"] == "model_risk"
    g = group(rows, info)
    h = board_html(g, _labels(), {"model_risk": "MRM"}, {})
    assert "<details class='dir'" in h and "<details class='doc'" in h and h.count("<tr data-key=") == len(rows)
    assert "score" not in h.lower()
    at = app(env, "bank@demo.ge", "board")
    assert not at.exception and at.multiselect(key="bf_dirs") and at.selectbox(key="bf_doc")
    at.session_state["task_key"] = rows[0]["key"]
    at.run()
    assert not at.exception


def test_board_links_open_the_analysis_at_the_task(env):
    sys.path.insert(0, APP.rsplit("/", 1)[0])
    from board_lib import board_html, group
    from review_lib import build_review, render_html, revised_html
    from view_board import _labels, doc_info, open_analysis, task_focus
    from kg.services import tasks as TK
    cfg, store, v, owner, fake = env
    rows = TK.list_tasks(cfg, store, owner, "bank-a")
    h = board_html(group(rows, doc_info(store, {r["doc_version_id"] for r in rows})), _labels(), {}, {})
    assert f"class='lnk' data-vid='{v}'" in h
    t = next(r for r in rows if r["kind"] == "adjust_clause")
    f = task_focus(store, t)
    assert f["slot"] == f"clause:{f['clause_id']}" and f["oid"] == t["obligation_id"]
    state = {"rf_q": "x", "rf_pick": "other", "dv_roles": ["contradicts"]}
    open_analysis(v, f, state)
    assert state["goto"] == "review" and state["review_version"] == v and "rf_q" not in state
    assert state["rf_status"] == []
    assert state["review_focus"]["slot"] == f["slot"] and set(f["roles"]) <= set(state["dv_roles"])
    m = build_review(cfg, store, v)
    assert "classList.add('focus')" in render_html(m, focus=f["clause_id"])
    assert f"data-focus='{f['slot']}'" in revised_html(m, LABELS, True, focus=f["slot"])
    at = app(env, "bank@demo.ge", "review", draft=True)
    at.session_state["review_focus"] = state["review_focus"]
    at.run()
    assert not at.exception and any(t["title"][:30] in i.value for i in at.info)


def test_duplicate_unplaced_additions_are_hidden(env):
    sys.path.insert(0, APP.rsplit("/", 1)[0])
    from review_lib import build_review, revised_view
    from view_board import _irrelevant, doc_info
    from kg.services import documents as D, tasks as TK
    cfg, store, v, owner, fake = env
    t = next(r for r in TK.list_tasks(cfg, store, owner, "bank-a") if r["kind"] == "adjust_clause")
    n_open = D.open_task_count(store, v)
    # pretend the model found no place for it: the obligation is still covered / placed in a document -> duplicate
    store.x("UPDATE proposals SET kind='new_document', clause_id=NULL WHERE id=?", (t["proposal_id"],))
    assert (v, t["obligation_id"]) in TK.duplicate_new_documents(store, "bank-a") or \
        store.q1("SELECT 1 FROM coverage WHERE obligation_id=? AND verdict IN ('full','partial','contradicts')",
                 (t["obligation_id"],)) is None
    hidden = (v, t["obligation_id"]) in TK.duplicate_new_documents(store, "bank-a")
    if hidden:
        assert t["key"] not in {r["key"] for r in TK.list_tasks(cfg, store, owner, "bank-a")}
        assert t["key"] in {r["key"] for r in TK.list_tasks(cfg, store, owner, "bank-a", include_duplicates=True)}
        assert t["obligation_id"] not in {o for s in revised_view(build_review(cfg, store, v))["new_documents"]
                                          for o in s["oids"]}
        assert D.open_task_count(store, v) == n_open - (1 if t["status"] == "open" else 0)
    # obligations irrelevant in every document of the direction get their own group
    oid = store.q1("SELECT obligation_id FROM triage WHERE doc_version_id=?", (v,))["obligation_id"]
    store.x("UPDATE triage SET relevance='irrelevant' WHERE obligation_id=?", (oid,))
    info = doc_info(store, {v})
    irr = _irrelevant(store, info)
    assert any(x["statement"] for x in irr["model_risk"])
    at = app(env, "bank@demo.ge", "board")
    assert not at.exception


def test_tables_are_shown_as_tables():
    from review_lib import is_row_unit, parse_table, table_html_of
    text = "№ | რისკი | ვადა\n№: F1 | რისკი: მაღალი | ვადა: 30.06.2022\n№: F2 | ვადა: 30.04.2022"
    h, rows = parse_table(text)
    assert h == ["№", "რისკი", "ვადა"] and rows == [["F1", "მაღალი", "30.06.2022"], ["F2", "", "30.04.2022"]]
    out = table_html_of(text)
    assert out.count("<tr>") == 3 and "<th>რისკი</th>" in out
    assert is_row_unit("კოდი: M-001 | სახელი: სკორინგი") and not is_row_unit(text)
    assert parse_table("ჩვეულებრივი ტექსტი") is None
    new = text.replace("მაღალი", "საშუალო") + "\n№: F3 | რისკი: დაბალი | ვადა: 2023"
    d = table_html_of(new, text)
    assert "class='cc'" in d and "<ins>F3</ins>" in d


def test_direction_filter_and_groups(env):
    cfg, store, v, owner, fake = env
    from common import directions
    assert directions(cfg)[0] == "model_risk" and len(directions(cfg)) == len(cfg["acts"])
    at = app(env, "bank@demo.ge", "docs")
    assert not at.exception and any(m.key == "doc_dirs" for m in at.multiselect)
    assert len([e for e in at.expander if "(" in e.label]) >= len(directions(cfg))     # + "not set yet"
    at.multiselect(key="doc_dirs").select("capital_adequacy").run()
    assert not at.exception and len([e for e in at.expander if "(" in e.label]) == 1
    rv = app(env, "bank@demo.ge", "review")
    assert not rv.exception and any(m.key == "rf_dirs" for m in rv.multiselect)


def test_board_direction_frames_and_review_draft_default(env):
    cfg, store, v, owner, fake = env
    from common import directions
    at = app(env, "bank@demo.ge", "board")
    assert not at.exception and any(m.key == "bf_dirs" for m in at.multiselect)
    rv = app(env, "bank@demo.ge", "review", all_status=False)
    assert rv.multiselect(key="rf_status").value == ["draft"]
    sys.path.insert(0, APP.rsplit("/", 1)[0])
    from board_lib import board_html
    from view_board import _labels
    h = board_html([{"direction": d, "docs": []} for d in directions(cfg)], _labels(), {}, {}, {})
    assert h.count("<details class='dir'") == len(directions(cfg)) and "class='none'" in h


def test_direction_is_a_human_tag_per_document(env):
    from kg.services import directions as DR
    cfg, store, v, owner, fake = env
    fam = store.q1("SELECT family_id FROM document_versions WHERE id=?", (v,))["family_id"]
    # created on first use: documents with a profile get its first domain as a starting tag
    assert DR.get_directions(store).get(fam) == "model_risk"
    DR.set_direction(store, owner, fam, "capital_adequacy")
    assert DR.direction_of_version(store, v) == "capital_adequacy"             # every version inherits it
    assert store.q1("SELECT 1 FROM user_decisions WHERE kind='set_direction'")
    prof = store.q1("SELECT domains FROM doc_profiles WHERE doc_version_id=?", (v,))["domains"]
    assert "model_risk" in prof                                                # the profile is not touched
    sys.path.insert(0, APP.rsplit("/", 1)[0])
    from view_board import doc_info
    assert doc_info(store, {v})[v]["direction"] == "capital_adequacy"
    at = app(env, "bank@demo.ge", "docs")
    assert not at.exception and at.selectbox(key=f"dir{fam}").value == "capital_adequacy"
    at.selectbox(key=f"dir{fam}").select("model_risk").run()
    at.button(key=f"dirs{fam}").click().run()
    assert not at.exception and DR.get_directions(store)[fam] == "model_risk"
    # the upload window asks for the direction before the file can be uploaded
    at.session_state["dlg"] = {"step": "upload"}
    at.run()
    assert not at.exception and at.selectbox(key="up_dir").value is None and at.button(key="up_btn").disabled
