"""Georgian Streamlit UI for the regulatory knowledge graph.   Run:  streamlit run app/kg_app.py"""
from __future__ import annotations

import html
import json
import sys
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))

from i18n_ka import (ACTION, BASIS, CHANCE, COVERAGE, DOC_TYPE, GAP, PRIORITY, PRIORITY_COLOR, T, tr)  # noqa: E402
from kg.config import load_config, today_iso  # noqa: E402
from kg.ids import display_path  # noqa: E402
from kg.llm import LLM, BudgetExhausted  # noqa: E402
from kg.runlog import Run  # noqa: E402
from kg.store.sqlite_nx import SQLiteStore  # noqa: E402

st.set_page_config(page_title=T["title"], layout="wide")


@st.cache_resource
def get_ctx():
    cfg = load_config()
    db = cfg.path("db")
    return cfg, (SQLiteStore(db) if db.exists() else None)


def badge(text: str, color: str) -> str:
    return (f"<span style='background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:0.8em'>"
            f"{html.escape(text)}</span>")


def prio_badge(p: str) -> str:
    return badge(tr(PRIORITY, p), PRIORITY_COLOR.get(p, "#555"))


def act_choices(cfg) -> dict[str, str]:
    return {f"{a} – {c.get('short', '')}": a for a, c in cfg["acts"].items()}


def with_llm(cfg, store, kind: str, fn, params=None):
    """Run ``fn(run, llm)`` inside a logged Run; shows a Georgian message when the quota is spent."""
    try:
        with Run(store, cfg.path("runs"), kind, params or {}) as run:
            return fn(run, LLM(cfg, store, run.id))
    except BudgetExhausted:
        st.error(T["budget"])
        return None


# ------------------------------------------------------------------------------------------------ pages
def page_qa(cfg, store):
    from kg.services import qa
    acts = act_choices(cfg)
    c1, c2 = st.columns(2)
    act_label = c1.selectbox(T["act"], [T["all_acts"]] + list(acts))
    as_of = c2.date_input(T["as_of"]).isoformat()
    q = st.text_area(T["question"])
    if st.button(T["ask"], type="primary") and q.strip():
        res = with_llm(cfg, store, "qa", lambda run, llm: qa.answer(
            cfg, store, llm, q, as_of, acts.get(act_label)), {"question": q})
        if res:
            st.subheader(T["answer"])
            st.write(res["answer"])
            st.subheader(T["citations"])
            if not res["citations"]:
                st.caption(T["no_citations"])
            for c in res["citations"]:
                with st.expander(f"{c['act_title'][:70]} · {c['provision']} · {T['version_from']} {c['valid_from']}"):
                    st.write(c["text"])


def diff_html(diff: list[dict]) -> str:
    out = []
    for d in diff:
        a, b = html.escape(d["a"]), html.escape(d["b"])
        if d["op"] == "equal":
            out.append(a)
        else:
            if a:
                out.append(f"<del style='background:#f8d7da'>{a}</del>")
            if b:
                out.append(f"<ins style='background:#d4edda;text-decoration:none'>{b}</ins>")
    return "".join(out)


def page_compare(cfg, store):
    from kg.services import compare
    acts = act_choices(cfg)
    versioned = {k: v for k, v in acts.items() if cfg["acts"][v]["mode"] == "versioned"}
    c1, c2, c3 = st.columns(3)
    act = versioned[c1.selectbox(T["act"], list(versioned))]
    d_a = c2.date_input(T["date_a"], value=__import__("datetime").date(2021, 6, 1)).isoformat()
    d_b = c3.date_input(T["date_b"], value=__import__("datetime").date.fromisoformat(today_iso())).isoformat()
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
            with st.expander(f"{ch['label']} · {tr({'added': T['added'], 'removed': T['removed'], 'modified': T['modified']}, ch['kind'])}"):
                if ch["kind"] == "modified":
                    st.markdown(diff_html(ch["diff"]), unsafe_allow_html=True)
                else:
                    st.write(ch["text_b"] or ch["text_a"])


def findings_table(rows: list[dict], key: str):
    if not rows:
        st.caption(T["no_findings"])
        return
    with st.expander(T["filters"], expanded=True):
        c = st.columns(4)
        f_prio = c[0].multiselect(T["priority"], sorted({r["priority"] for r in rows}), key=f"{key}p", format_func=lambda x: tr(PRIORITY, x))
        f_ch = c[1].multiselect(T["chance"], sorted({r["issue_chance"] for r in rows}), key=f"{key}c", format_func=lambda x: tr(CHANCE, x))
        f_b = c[2].multiselect(T["basis"], sorted({r["version_basis"] for r in rows}), key=f"{key}b", format_func=lambda x: tr(BASIS, x))
        f_a = c[3].multiselect(T["action"], sorted({r["action_type"] for r in rows}), key=f"{key}a", format_func=lambda x: tr(ACTION, x))
    sel = [r for r in rows if (not f_prio or r["priority"] in f_prio) and (not f_ch or r["issue_chance"] in f_ch)
           and (not f_b or r["version_basis"] in f_b) and (not f_a or r["action_type"] in f_a)]
    st.dataframe([{T["priority"]: tr(PRIORITY, r["priority"]), T["clause"]: r.get("section_no") or "—",
                   T["obligation"]: (r.get("source") or ""), T["chance"]: tr(CHANCE, r["issue_chance"]),
                   T["severity"]: tr(PRIORITY, r["severity"]), T["basis"]: tr(BASIS, r["version_basis"]),
                   T["gap"]: tr(GAP, r["gap_class"]), T["action"]: tr(ACTION, r["action_type"])} for r in sel],
                 use_container_width=True)
    for r in sel:
        title = f"{r.get('section_no') or '—'} · {r.get('source') or ''}"
        with st.expander(title):
            st.markdown(prio_badge(r["priority"]) + " " + badge(tr(GAP, r["gap_class"]), "#34495e"), unsafe_allow_html=True)
            st.markdown(f"**{T['obligation']}:** {r.get('obligation') or ''}")
            st.markdown(f"**{T['rationale']}:** {r.get('rationale') or ''}")
            if r.get("clause_text"):
                text = html.escape(r["clause_text"])
                q = r.get("evidence_quote")
                if q and html.escape(q) in text:
                    text = text.replace(html.escape(q), f"<mark>{html.escape(q)}</mark>")
                st.markdown(f"**{T['clause']}:**<br>{text}", unsafe_allow_html=True)
            if r.get("suggested_fix"):
                st.markdown(f"**{T['fix']}:** {r['suggested_fix']}")


def page_doccheck(cfg, store):
    from kg.services import doccheck
    mode = st.radio("mode", [T["existing_doc"], T["upload_pdf"]], horizontal=True, label_visibility="collapsed")
    if mode == T["existing_doc"]:
        docs = doccheck.list_documents(store)
        label = {d["id"]: f"{d['title'] or d['id']} · {tr(DOC_TYPE, d['doc_type'])} · {d['doc_date']}" for d in docs}
        did = st.selectbox(T["doc"], list(label), format_func=lambda x: label[x])
        if did:
            findings_table(doccheck.check_existing(store, did), "ex")
        comp = doccheck.company_findings(store)
        if comp:
            st.subheader(T["company_level"])
            findings_table(comp, "co")
    else:
        up = st.file_uploader("PDF", type="pdf")
        if up and st.button(T["check"], type="primary"):
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                fh.write(up.getvalue())
            bar = st.progress(0.0)

            def progress(stage, done, total):
                bar.progress(min(1.0, done / max(total, 1)), text=T.get(f"progress_{stage}", stage))

            res = with_llm(cfg, store, "check-upload", lambda run, llm: doccheck.check_upload(
                cfg, store, llm, run, fh.name, progress), {"file": up.name})
            if res:
                st.success(f"{res['doc_id']} · {tr(DOC_TYPE, res['doc_type'])}")
                findings_table(res["findings"], "up")


def page_replay(cfg, store):
    from kg.services import replay, reports
    ams = sorted((n for n in store.nodes("AmendingAct") if str(n["props"]["base_act"]) in cfg["obligation_acts"]),
                 key=lambda n: n["props"]["seq"])
    label = {n["props"]["doc_id"]: f"{n['props']['number']} · {n['props']['effective_from']}" for n in ams}
    if not label:
        st.caption(T["no_report"])
        return
    am = st.selectbox(T["amendment"], list(label), format_func=lambda x: label[x])
    base = next(n["props"]["base_act"] for n in ams if n["props"]["doc_id"] == am)
    existing = [r for r in reports.list_reports(cfg) if r["amendment"] == am]
    if st.button(T["run_replay"]):
        rep = with_llm(cfg, store, "replay", lambda run, llm: replay.replay(cfg, store, run, llm, base, am),
                       {"amendment": am})
        if rep:
            st.session_state["rep"] = rep
    rep = st.session_state.get("rep")
    if not rep or rep.get("amendment") != am:
        rep = reports.get(cfg, existing[0]["run_id"]) if existing else None
    if not rep:
        st.info(T["no_report"])
        return
    st.subheader(f"{T['change_summary']} · {rep['effective_date']}")
    st.write(f"{len(rep['changes'])} · {T['new_obl']}: {len(rep['new_obligations'])} · "
             f"{T['changed_obl']}: {len(rep['changed_obligations'])} · {T['removed_obl']}: {len(rep['removed_obligations'])}")
    for title, key in ((T["new_obl"], "new_obligations"), (T["changed_obl"], "changed_obligations"),
                       (T["removed_obl"], "removed_obligations")):
        with st.expander(f"{title} ({len(rep[key])})"):
            for o in rep[key]:
                st.markdown(f"- **{display_path(o['source_path'])}** [{tr(PRIORITY, o['severity'])}] {o['statement']}")
    st.subheader(T["affected"])
    for f in sorted(rep["affected"], key=lambda f: ["low", "medium", "high", "critical"].index(f["priority"]), reverse=True):
        with st.expander(f"{f['doc_id']} · {tr(ACTION, f['action_type'])}"):
            st.markdown(prio_badge(f["priority"]) + f" {tr(CHANCE, f['issue_chance'])}", unsafe_allow_html=True)
            st.write(f.get("obligation"))
            st.write(f.get("rationale"))
            if f.get("suggested_fix"):
                st.markdown(f"**{T['fix']}:** {f['suggested_fix']}")
    st.subheader(T["new_alerts"])
    for a in rep["new_alerts"]:
        st.warning(a["message"])


def page_alerts(cfg, store):
    alerts = store.alerts()
    if not alerts:
        st.caption(T["alerts_none"])
    for a in alerts:
        c1, c2 = st.columns([8, 1])
        c1.warning(f"{T['uncovered']}: {a['message']}")
        if c2.button(T["ack"], key=f"ack{a['id']}"):
            store.acknowledge_alert(a["id"])
            st.rerun()


def page_graph(cfg, store):
    import streamlit.components.v1 as components
    from pyvis.network import Network
    obls = sorted(store.nodes("Obligation"), key=lambda n: (n["props"]["source_path"], n["valid_from"]))
    if not obls:
        st.caption(T["graph_none"])
        return
    label = {o["id"]: f"{display_path(o['props']['source_path'])} · {o['valid_from']} · {o['props']['statement'][:50]}"
             for o in obls}
    oid = st.selectbox(T["graph_pick"], list(label), format_func=lambda x: label[x])
    net = Network(height="520px", width="100%", directed=True, cdn_resources="in_line")
    ob = store.get_node(oid)

    def add(n, text, color):
        net.add_node(n, label=text[:40], title=text[:300], color=color)

    add(oid, ob["props"]["statement"], "#e67e22")
    for e in store.neighbors(oid, "IMPOSES", "in"):
        pv = e["node"]
        add(pv["id"], f"{display_path(pv['props']['path'])} @ {pv['valid_from']}", "#2980b9")
        net.add_edge(pv["id"], oid, label="IMPOSES")
    for e in store.neighbors(oid, "COVERED_BY", "out"):
        cl = e["node"]
        add(cl["id"], f"{cl['props'].get('section_no') or ''} {cl['props']['text']}", "#27ae60")
        net.add_edge(oid, cl["id"], label=str(e["edge"]["props"].get("coverage", "COVERED_BY")))
    components.html(net.generate_html(notebook=False), height=540)


def page_eval(cfg, store):
    import csv
    idx = cfg.path("eval_results") / "index.csv"
    if not idx.exists():
        st.caption(T["eval_none"])
    else:
        rows = list(csv.DictReader(open(idx, encoding="utf-8")))
        latest = {}
        for r in rows:
            latest[r["suite"]] = r
        for suite, r in latest.items():
            st.subheader(f"{suite} · {r['timestamp']}")
            st.json(json.loads(r["metrics"]), expanded=False)
    from kg.eval.ops import llm_metrics
    m = llm_metrics(store)
    if m:
        import pandas as pd
        df = pd.DataFrame(m).T
        st.subheader(T["calls"])
        st.bar_chart(df[["calls", "cache_hits"]])
        st.subheader(T["latency"])
        st.bar_chart(df[["latency_p50_ms", "latency_p95_ms"]].fillna(0) / 1000)
        st.subheader(T["tokens"])
        st.bar_chart(df[["input_tokens", "output_tokens"]])


PAGES = [page_qa, page_compare, page_replay, page_doccheck, page_alerts, page_graph, page_eval]


def main():
    cfg, store = get_ctx()
    st.title(T["title"])
    page = st.sidebar.radio("menu", T["pages"], key="page", label_visibility="collapsed")
    if store is None:
        st.error(T["no_db"])
        return
    PAGES[T["pages"].index(page)](cfg, store)


main()
