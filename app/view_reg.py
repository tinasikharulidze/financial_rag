"""Regulation pages available to every user: Q&A, version comparison, graph, evaluation (from the v1 app)."""
from __future__ import annotations

import json

import streamlit as st
from common import act_choices, diff_html, html_frame, with_llm
from i18n_ka import T, tr


def page_qa(cfg, store):
    from kg.services import qa
    acts = act_choices(cfg)
    c1, c2 = st.columns(2)
    act_label = c1.selectbox(T["act"], [T["all_acts"]] + list(acts))
    as_of = c2.date_input(T["as_of"]).isoformat()
    q = st.text_area(T["question"])
    if st.button(T["ask"], type="primary") and q.strip():
        res = with_llm(cfg, store, "qa", lambda run, llm: qa.answer(cfg, store, llm, q, as_of, acts.get(act_label)),
                       {"question": q})
        if res:
            st.subheader(T["answer"])
            st.write(res["answer"])
            st.subheader(T["citations"])
            if not res["citations"]:
                st.caption(T["no_citations"])
            for c in res["citations"]:
                with st.expander(f"{c['act_title'][:70]} · {c['provision']} · {T['version_from']} {c['valid_from']}"):
                    st.write(c["text"])


def page_compare(cfg, store):
    import datetime
    from kg.config import today_iso
    from kg.services import compare
    acts = act_choices(cfg)
    versioned = {k: v for k, v in acts.items() if cfg["acts"][v]["mode"] == "versioned"}
    c1, c2, c3 = st.columns(3)
    act = versioned[c1.selectbox(T["act"], list(versioned))]
    d_a = c2.date_input(T["date_a"], value=datetime.date(2021, 6, 1)).isoformat()
    d_b = c3.date_input(T["date_b"], value=datetime.date.fromisoformat(today_iso())).isoformat()
    if st.button(T["compare"], type="primary"):
        st.session_state["cmp"] = compare.versions(store, act, d_a, d_b)
    res = st.session_state.get("cmp")
    if res:
        c = res["counts"]
        st.write(f"{T['added']}: **{c['added']}** · {T['removed']}: **{c['removed']}** · {T['modified']}: **{c['modified']}**")
        if st.button(T["summary_btn"]):
            s = with_llm(cfg, store, "compare", lambda run, llm: compare.summarize(llm, res))
            if s:
                st.info(s)
        if not res["changes"]:
            st.caption(T["no_changes"])
        for ch in res["changes"]:
            kinds = {"added": T["added"], "removed": T["removed"], "modified": T["modified"]}
            with st.expander(f"{ch['label']} · {tr(kinds, ch['kind'])}"):
                if ch["kind"] == "modified":
                    st.markdown(diff_html(ch["diff"]), unsafe_allow_html=True)
                else:
                    st.write(ch["text_b"] or ch["text_a"])


def page_graph(cfg, store):
    from kg.ids import display_path
    from pyvis.network import Network
    obls = sorted(store.nodes("Obligation"), key=lambda n: (n["props"].get("source_path", ""), n["valid_from"] or ""))
    if not obls:
        st.caption(T["graph_none"])
        return
    label = {o["id"]: f"{display_path(o['props'].get('source_path', ''))} · {o['valid_from']} · "
                      f"{(o['props'].get('statement') or '')[:50]}" for o in obls}
    oid = st.selectbox(T["graph_pick"], list(label), format_func=lambda x: label[x])
    net = Network(height="520px", width="100%", directed=True, cdn_resources="in_line")
    ob = store.get_node(oid)

    def add(n, text, color):
        net.add_node(n, label=text[:40], title=text[:300], color=color)

    add(oid, ob["props"].get("statement") or oid, "#e67e22")
    for e in store.neighbors(oid, "IMPOSES", "in"):
        pv = e["node"]
        add(pv["id"], f"{display_path(pv['props'].get('path', ''))} @ {pv['valid_from']}", "#2980b9")
        net.add_edge(pv["id"], oid, label="IMPOSES")
    for e in store.neighbors(oid, "SUPERSEDES", "both"):
        o2 = e["node"]
        add(o2["id"], f"{o2['valid_from']} {(o2['props'].get('statement') or '')}", "#8e44ad")
        net.add_edge(oid, o2["id"], label="SUPERSEDES")
    for e in store.neighbors(oid, "COVERED_BY", "out"):
        cl = e["node"]
        p = e["edge"]["props"]
        add(cl["id"], f"{cl['props'].get('section_no') or ''} {cl['props'].get('text', '')}", "#27ae60")
        net.add_edge(oid, cl["id"], label=f"{p.get('role', p.get('coverage', ''))}/{p.get('source', '')}")
    html_frame(net.generate_html(notebook=False), 540)


def page_eval(cfg, store):
    import csv
    idx = cfg.path("eval_results") / "index.csv"
    if not idx.exists():
        st.caption(T["eval_none"])
    else:
        latest = {}
        for r in csv.DictReader(open(idx, encoding="utf-8")):
            latest[r["suite"]] = r
        for suite, r in latest.items():
            st.subheader(f"{suite} · {r['timestamp']}")
            st.json(json.loads(r["metrics"]), expanded=False)
    try:
        from kg.eval.ops import llm_metrics
        m = llm_metrics(store)
    except Exception:  # noqa: BLE001
        m = None
    if m:
        import pandas as pd
        df = pd.DataFrame(m).T
        st.subheader(T["calls"])
        st.bar_chart(df[["calls", "cache_hits"]])
        st.subheader(T["latency"])
        st.bar_chart(df[["latency_p50_ms", "latency_p95_ms"]].fillna(0) / 1000)
        st.subheader(T["tokens"])
        st.bar_chart(df[["input_tokens", "output_tokens"]])
