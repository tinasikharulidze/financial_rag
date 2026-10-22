"""Company profile, roles and members (regulated entity)."""
from __future__ import annotations

import streamlit as st
from i18n_ka import ENTITY_TYPE, T, tr


def render(cfg, store, user):
    from kg.services import accounts
    cid = user["company_id"]
    c = accounts.get_company(store, cid)
    st.subheader(c["name"])
    with st.form("company_form"):
        name = st.text_input(T["company_name"], c["name"])
        types = cfg["entity_types"]
        etype = st.selectbox(T["entity_type"], types, index=types.index(c["entity_type"]) if c["entity_type"] in types else 0,
                             format_func=lambda x: tr(ENTITY_TYPE, x))
        reg = st.text_input(T["reg_number"], c["reg_number"] or "")
        address = st.text_input(T["address"], c["props"].get("address", ""))
        phone = st.text_input(T["phone"], c["props"].get("phone", ""))
        if st.form_submit_button(T["save"]):
            accounts.update_company(store, user, cid, name=name, entity_type=etype, reg_number=reg,
                                    props={"address": address, "phone": phone}, entity_types=types)
            st.success(T["saved"])
    st.subheader(T["roles"])
    roles = accounts.list_roles(store, cid)
    for r in roles:
        c1, c2 = st.columns([6, 1])
        c1.write(r["name"])
        if c2.button("✕", key=f"rmrole{r['id']}"):
            accounts.remove_role(store, user, cid, r["id"])
            st.rerun()
    new = st.text_input(T["role_name"], key="newrole")
    if st.button(T["add_role"]) and new.strip():
        accounts.add_role(store, user, cid, new)
        st.rerun()
    st.subheader(T["members"])
    for m in accounts.members(store, user, cid):
        c1, c2, c3 = st.columns([3, 3, 3])
        c1.write(f"**{m['name']}** · {m['email']}")
        c2.write(", ".join(m["roles"]) or "—")
        if roles:
            pick = c3.selectbox(T["assign_role"], [r["id"] for r in roles], key=f"as{m['id']}",
                                format_func=lambda i: next(r["name"] for r in roles if r["id"] == i))
            if c3.button(T["assign_role"], key=f"asb{m['id']}"):
                accounts.assign_role(store, user, cid, m["id"], pick)
                st.rerun()
