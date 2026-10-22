"""Login and registration (plan section 3.3)."""
from __future__ import annotations

import streamlit as st
from common import login_as
from i18n_ka import ENTITY_TYPE, T, tr


def render(cfg, store):
    from kg.services import accounts
    tab_in, tab_up = st.tabs([T["login"], T["register"]])
    with tab_in:
        email = st.text_input(T["email"], key="li_email")
        pw = st.text_input(T["password"], type="password", key="li_pw")
        if st.button(T["login"], type="primary", key="li_btn"):
            user = accounts.authenticate(store, email, pw)
            if user:
                login_as(user)
                st.rerun()
            else:
                st.error(T["bad_login"])
    with tab_up:
        kind = st.radio(T["user_type"], ["entity", "regulator"], key="rg_kind", horizontal=True,
                        format_func=lambda k: T["type_entity"] if k == "entity" else T["type_regulator"])
        name = st.text_input(T["name"], key="rg_name")
        email = st.text_input(T["email"], key="rg_email")
        pw = st.text_input(T["password"], type="password", key="rg_pw")
        code_needed = cfg.get_in("auth.regulator_code")
        code = st.text_input(T["regulator_code"], type="password", key="rg_code") if (
            kind == "regulator" and code_needed) else None
        company, join = None, None
        if kind == "entity":
            existing = [c["name"] for c in store.q("SELECT name FROM companies ORDER BY name")]
            mode = st.radio("company", [T["create_company"], T["join_company"]] if existing else [T["create_company"]],
                            key="rg_mode", horizontal=True, label_visibility="collapsed")
            if mode == T["create_company"]:
                company = {"name": st.text_input(T["company_name"], key="rg_cname"),
                           "entity_type": st.selectbox(T["entity_type"], cfg["entity_types"], key="rg_etype",
                                                       format_func=lambda x: tr(ENTITY_TYPE, x)),
                           "reg_number": st.text_input(T["reg_number"], key="rg_regno") or None}
            else:
                join = st.selectbox(T["company"], existing, key="rg_join")
        if st.button(T["register"], type="primary", key="rg_btn"):
            if kind == "regulator" and code_needed and code != code_needed:
                st.error(T["wrong_code"])
                return
            try:
                user = accounts.register(store, email=email, name=name, password=pw, user_type=kind, company=company,
                                         join_company=join, entity_types=cfg["entity_types"])
            except accounts.AccountError as exc:
                st.error(str(exc))
                return
            login_as(user)
            st.rerun()
