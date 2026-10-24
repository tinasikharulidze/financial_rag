"""Georgian Streamlit UI (v2): login gate + router.   Run:  streamlit run app/kg_app.py"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "app"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import common  # noqa: E402
import view_auth, view_board, view_chat, view_company, view_docs, view_reg, view_regulator, view_review  # noqa: E402,E401
from i18n_ka import T  # noqa: E402

st.set_page_config(page_title=T["title"], layout="wide")

ENTITY_PAGES = ["docs", "review", "board"]
REGULATOR_PAGES = ["companies", "review", "board"]
EXTRA_PAGES = ["qa", "compare", "graph", "eval"]      # hidden for now; configs/kg.yaml -> ui.extra_pages: true
LABEL = {"docs": "pg_docs", "review": "pg_review", "board": "pg_board", "company": "pg_company",
         "companies": "pg_companies", "qa": "pg_qa", "compare": "pg_compare", "graph": "pg_graph", "eval": "pg_eval"}


CSS = """
<style>
.block-container {padding-top: 3.2rem; padding-left: 2rem; padding-right: 2rem; max-width: 100%;}
h1 {font-size: 1.6rem !important; margin: 0 !important; padding: 0.2rem 0 !important;}
/* top navigation: a thin line under the bar, a soft hover, the current page marked */
[data-testid="stHeader"] {border-bottom: 1px solid rgba(49, 51, 63, 0.10); background: rgba(255, 255, 255, 0.96);
                          backdrop-filter: blur(6px);}
[data-testid="stTopNavLink"] {border-radius: 8px; padding: 0.3rem 0.85rem; transition: background 0.15s, color 0.15s;}
[data-testid="stTopNavLink"]:hover {background: rgba(31, 111, 178, 0.08); color: #1f6fb2;}
[data-testid="stTopNavLink"][aria-current="page"] {background: rgba(31, 111, 178, 0.12); color: #1f6fb2;
                                                   font-weight: 600; box-shadow: inset 0 -2px 0 #1f6fb2;}
</style>
"""


def _render_page(key: str, cfg, store, user):
    st.session_state["page_now"] = key
    cid = user["company_id"] or st.session_state.get("company_id")
    if key == "docs":
        view_docs.render(cfg, store, user)
    elif key == "review":
        view_review.render(cfg, store, user)
    elif key == "board":
        if cid:
            view_board.render(cfg, store, user, cid)
        else:
            st.info(T["search_company"])
    elif key == "company":
        view_company.render(cfg, store, user)
    elif key == "companies":
        view_regulator.render(cfg, store, user)
    else:
        getattr(view_reg, "page_" + key)(cfg, store)


def _page(key: str, cfg, store, user, *, default=False, hidden=False):
    def run():
        _render_page(key, cfg, store, user)
    run.__name__ = f"page_{key}"
    return st.Page(run, title=T[LABEL[key]], url_path=key, default=default,
                   visibility="hidden" if hidden else "visible")


def _lang_page():
    """The last menu entry: switches Georgian <-> English and goes back to the page the user was on."""
    from i18n_lang import current
    st.session_state["lang"] = "en" if current() == "ka" else "ka"
    st.session_state["goto"] = st.session_state.get("page_now") or "docs"
    st.rerun()


def _account_menu(store, user, pages: dict):
    """Top right, apart from the pages: who is logged in, the company profile, log out."""
    co = store.q1("SELECT name FROM companies WHERE id=?", (user["company_id"],)) if user["company_id"] else None
    _, right = st.columns([5, 1.4], vertical_alignment="center")       # no big page title: the menu is enough
    with right.popover(f"👤 {user['name']}", use_container_width=True):
        st.markdown(f"**{user['name']}**  \n{user['email']}" + (f"  \n{co['name']}" if co else
                                                                 f"  \n{T['type_regulator']}"))
        if "company" in pages and st.button(T["pg_company"], key="menu_company", use_container_width=True):
            st.session_state["goto"] = "company"
            st.rerun()
        st.divider()
        if st.button(T["logout"], key="menu_logout", use_container_width=True):
            common.logout()
            st.rerun()


def main():
    cfg, store = common.get_ctx()
    if store is None:
        st.title(T["title"])
        st.error(T["no_db"])
        return
    user = common.current_user(store)
    if user is None:
        st.title(T["title"])
        view_auth.render(cfg, store)
        return
    keys = (ENTITY_PAGES if user["user_type"] == "entity" else REGULATOR_PAGES) + \
        (EXTRA_PAGES if cfg.get_in("ui.extra_pages", False) else [])
    # tests only: Streamlit's AppTest does not keep the current page between runs (a browser keeps it in the URL),
    # so a test names it in ``_test_page`` and it becomes the default page
    goto = st.session_state.pop("goto", None)           # a page asked to switch (a tile's review button, the menu)
    if goto and "_test_page" in st.session_state:
        st.session_state["_test_page"], goto = goto, None
    hidden = ["company"] if user["user_type"] == "entity" else []      # reached from the account menu only
    first = st.session_state.get("_test_page") if st.session_state.get("_test_page") in keys + hidden else keys[0]
    pages = {k: _page(k, cfg, store, user, default=(k == first), hidden=k in hidden) for k in keys + hidden}
    pages["lang"] = st.Page(_lang_page, title=T["lang_switch"], url_path="language")      # the last menu entry
    # the pages as a navigation bar at the top (hover + click), the whole width below it for the content
    nav = st.navigation(list(pages.values()), position="top")
    st.markdown(CSS, unsafe_allow_html=True)
    _account_menu(store, user, pages)
    if "_test_page" in st.session_state and first in pages and nav != pages[first]:
        goto = first                                    # tests: a rerun keeps the previous page; switch explicitly
    if goto in pages and nav != pages[goto]:
        st.switch_page(pages[goto])
    if not cfg.get_in("agent.enabled", True):
        nav.run()
    elif st.session_state.get("chat_open"):
        # the chat panel is a column on the LEFT that pushes the layout (it does not overlay the page)
        chat_col, main_col = st.columns([0.92, 3], gap="medium")      # the panel: ~23% of the width
        with chat_col:
            view_chat.render(cfg, store, user)
        with main_col:
            nav.run()
    else:
        # collapsed: only a slim bar on the left; one click expands it
        rail_col, main_col = st.columns([1, 40], gap="small")
        with rail_col:
            view_chat.rail()
        with main_col:
            nav.run()


main()
