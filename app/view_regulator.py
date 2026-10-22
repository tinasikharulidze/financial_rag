"""Regulator: company search and the published documents / tasks of a company."""
from __future__ import annotations

import streamlit as st
from common import version_label
from i18n_ka import ENTITY_TYPE, T, tr


def render(cfg, store, user):
    from kg.services import accounts
    from kg.services import documents as D
    q = st.text_input(T["search_company"], key="co_q")
    rows = accounts.list_companies(store, user, q)
    st.subheader(f"{T['companies']} ({len(rows)})")
    for c in rows:
        a, b, d = st.columns([5, 3, 2])
        a.write(f"**{c['name']}**")
        b.write(f"{tr(ENTITY_TYPE, c['entity_type'])} · {c['reg_number'] or '—'}")
        if d.button(T["open_company"], key=f"oc{c['id']}"):
            st.session_state["company_id"] = c["id"]
    cid = st.session_state.get("company_id")
    if cid:
        co = accounts.get_company(store, cid)
        st.divider()
        st.subheader(f"{co['name']} — {T['published_docs']}")
        fams = D.list_families(store, user, cid)
        if not fams:
            st.caption(T["no_published"])
        for f in fams:
            for v in f["versions"]:
                x, y = st.columns([6, 2])
                x.write(version_label(store, v["id"]))
                if y.button(T["review"], key=f"rr{v['id']}"):
                    st.session_state["review_version"] = v["id"]
                    st.session_state["goto"] = "review"
                    st.rerun()
