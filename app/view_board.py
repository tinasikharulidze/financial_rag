"""Task board (Jira-like): filters on top, then direction -> document -> table of tasks (each group collapsible,
columns sortable per document). A click on a task opens its window in the middle of the page."""
from __future__ import annotations

import json

import streamlit as st
from common import prio_badge, verdict_badge, version_label
from i18n_ka import CAUSE, DOMAIN, KIND, PRIORITY, RELEVANCE, T, TASK_STATUS, VERDICT, tr

def _component():
    """Declared on every run (cheap): a component cached across runs is unknown to a new Streamlit runtime."""
    from board_lib import BOARD_CSS, BOARD_JS
    return st.components.v2.component("goni_task_board", css=BOARD_CSS, js=BOARD_JS)


def doc_info(store, version_ids) -> dict[str, dict]:
    """{version id: {"label", "title", "direction"}}; the direction is the tag a person chose for the document."""
    from kg.services.directions import get_directions
    tags = get_directions(store)
    out = {}
    for v in version_ids:
        d = store.q1("SELECT d.id, d.title FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                     "WHERE dv.id=?", (v,)) or {}
        out[v] = {"label": version_label(store, v), "title": d.get("title") or "",
                  "direction": tags.get(d.get("id")) or "other"}
    return out


def _labels() -> dict:
    return {"columns": [T["col_priority"], T["col_summary"], T["col_coverage"], T["col_relevance"], T["cause"],
                        T["status"], T["assignee"]],
            "docs": T["docs_n"], "tasks": T["tasks_n"], "new": T["new_badge"], "open_analysis": T["open_analysis"],
            "obligation": T["obligation"], "source": T["col_source"], "none_found": T["none_found"],
            "irr_everywhere": T["irr_everywhere"], "no_tasks_direction": T["no_tasks_direction"],
            "prio": dict(PRIORITY.items()), "cov": dict(VERDICT.items()) | {"none": T["grp_missing"]},
            "rel": dict(RELEVANCE.items()), "status": dict(TASK_STATUS.items()), "cause": dict(CAUSE.items())}


def _close():
    st.session_state.pop("task_key", None)


def open_analysis(version_id: str, focus: dict | None = None, state=None):
    """Go to the analysis page of a document version; with ``focus`` the page scrolls to that change and marks it
    (the document filters are widened so the change is visible)."""
    state = st.session_state if state is None else state
    for k in [k for k in list(state.keys()) if str(k).startswith("rf_")]:    # the version picker's own filters
        state.pop(k, None)
    state["rf_status"] = []                   # all statuses, so the chosen version is never filtered out
    state["review_version"] = version_id
    state.pop("prop_dlg", None)
    if focus:
        state["review_focus"] = {**focus, "version_id": version_id}
        roles = list(state.get("dv_roles") or ["contradicts", "anchor"])
        for r in focus.get("roles", []):
            if r not in roles:
                roles.append(r)
        state["dv_roles"] = roles
        rels = list(state.get("dv_rel") or ["relevant"])
        if focus.get("relevance") in ("relevant", "possibly") and focus["relevance"] not in rels:
            rels.append(focus["relevance"])
        state["dv_rel"] = rels
    else:
        state.pop("review_focus", None)
    state.pop("task_key", None)
    state["goto"] = "review"


def task_focus(store, t: dict) -> dict:
    """Where a task shows in the analysis page: the slot of its proposal (right) and its clause (left)."""
    from kg.services.edits import slot_of
    pr = store.q1("SELECT * FROM proposals WHERE id=?", (t["proposal_id"],)) if t.get("proposal_id") else None
    clause = pr["clause_id"] if pr else None
    if clause is None:
        e = next((e for e in store.edges("COVERED_BY") if e["src"] == t["obligation_id"]
                  and e["props"].get("doc_version") == t["doc_version_id"]), None)
        clause = e["dst"] if e else None
    from review_lib import ITEM_ROLE
    role = ITEM_ROLE.get(t["verdict"], "partial")
    return {"slot": slot_of(pr) if pr else None, "clause_id": clause, "oid": t["obligation_id"],
            "title": t["title"], "kind": pr["kind"] if pr else None, "relevance": t["relevance"],
            "roles": sorted({role, "anchor" if pr and pr["kind"] != "adjust_clause" else role})}


def _detail(cfg, store, user, key, can_edit):
    """The task window: flags, what is wrong and why, the regulation, the proposal, people, history."""
    from kg.services import tasks as TK
    ex = TK.explain_task(cfg, store, user, key)
    t = ex["task"]
    cov = ex["coverage"] or {}
    tri = ex["triage"] or {}
    st.markdown(f"#### {t['title']}")
    if st.button("📍 " + T["show_in_doc"], key=f"goto{key}", type="primary"):
        open_analysis(t["doc_version_id"], task_focus(store, t))
        st.rerun()
    lab = _labels()
    st.markdown(" ".join([prio_badge(t["priority"]), verdict_badge(t["verdict"]) if t["verdict"] != "none" else
                          f"<span style='background:#2980b9;color:#fff;padding:1px 8px;border-radius:3px'>"
                          f"{T['grp_missing']}</span>",
                          f"<span style='border:1px solid #2c3e50;padding:0 7px;border-radius:3px'>"
                          f"{lab['rel'].get(t['relevance'], t['relevance'] or '—')}</span>",
                          f"<span style='color:#42526e'>· {tr(CAUSE, t['cause'])}"
                          + (f" {t['amendment_number']}" if t.get("amendment_number") else "") + "</span>",
                          f"<span style='color:#42526e'>· {tr(TASK_STATUS, t['status'])}</span>"]),
                unsafe_allow_html=True)
    st.caption(version_label(store, t["doc_version_id"]))
    ob = store.get_node(t["obligation_id"]) or {"props": {}}
    st.markdown(f"**{T['obligation']}:** {ob['props'].get('statement') or t['title']}")
    if cov.get("missing"):
        st.markdown(f"**{T['what_missing']}:** {cov['missing']}")
    if cov.get("rationale"):
        st.markdown(f"**{T['explanation']}:** {cov['rationale']}")
    if tri.get("reason"):
        st.markdown(f"**{T['col_relevance']}:** {tri['reason']}")
    for c in [x for x in (ob["props"].get("citations") or []) if x.get("kind") == "provision"][:3]:
        src = f"[{c.get('label') or c.get('id')}]({c['url']})" if c.get("url") else (c.get("label") or c.get("id"))
        st.markdown(f"**{T['regulation']}:** {src}" + (f" — „{c['quote']}“" if c.get("quote") else ""))
    pr = store.q1("SELECT * FROM proposals WHERE id=?", (t["proposal_id"],)) if t.get("proposal_id") else None
    if pr:
        st.markdown(f"**{T['proposed']}** ({tr(KIND, pr['kind'])}):")
        st.info(((pr["heading"] + ". ") if pr.get("heading") else "") + pr["proposed_text"])
    if can_edit:
        from kg.services import accounts
        members = accounts.members(store, user, t["company_id"])
        c1, c2 = st.columns(2)
        who = c1.selectbox(T["assignee"], [None] + [m["id"] for m in members], key=f"who{key}",
                           index=([None] + [m["id"] for m in members]).index(t["assignee_user"])
                           if t.get("assignee_user") in [m["id"] for m in members] else 0,
                           format_func=lambda i: "—" if i is None else next(m["name"] for m in members if m["id"] == i))
        due = c2.text_input(T["due"], t.get("due_date") or "", key=f"due{key}")
        if st.button(T["assign"], key=f"asg{key}"):
            TK.assign(store, user, key, user_id=who, due_date=due or None)
            st.rerun()
        text = st.text_input(T["comment"], key=f"cm{key}")
        if st.button(T["send"], key=f"cms{key}") and text.strip():
            TK.comment(store, user, key, text)
            st.rerun()
    with st.expander(T["history"]):
        for d in ex["decisions"]:
            st.write(f"{d['ts'][:16]} · {d['actor_name']} · {d['kind']}")
        for e in ex["events"]:
            if e["event"] == "rescored":
                continue
            st.caption(f"{e['ts'][:16]} · {e.get('actor_name') or '—'} · {e['event']}")


def _irrelevant(store, info: dict) -> dict[str, list[dict]]:
    """Per direction: obligations judged irrelevant for every checked document of that direction."""
    from kg.services.tasks import irrelevant_everywhere
    by: dict[str, list[str]] = {}
    for v, i in info.items():
        by.setdefault(i["direction"], []).append(v)
    out = {}
    for d, vers in by.items():
        items = []
        for oid in irrelevant_everywhere(store, vers):
            ob = store.get_node(oid) or {"props": {}}
            c = next((x for x in (ob["props"].get("citations") or []) if x.get("kind") == "provision"), {})
            items.append({"statement": ob["props"].get("statement") or oid, "source": c.get("label") or "",
                          "url": c.get("url")})
        out[d] = items
    return out


def _filters(cfg, rows, info):
    """Direction (all directions of the configured acts, as on the documents page), document, priority, cause."""
    from common import direction_label, directions
    c = st.columns([3, 3, 2, 2])
    opts = directions(cfg) + (["other"] if any(i["direction"] == "other" for i in info.values()) else [])
    f_dir = c[0].multiselect(T["filter_direction"], opts, key="bf_dirs", format_func=lambda x: direction_label(cfg, x))
    docs = sorted([v for v, i in info.items() if not f_dir or i["direction"] in f_dir],
                  key=lambda v: info[v]["label"])
    f_doc = c[1].selectbox(T["filter_doc"], [None] + docs, key="bf_doc",
                           format_func=lambda x: T["all"] if x is None else
                           f"{info[x]['label']} — {info[x]['title'][:40]}")
    f_prio = c[2].multiselect(T["filter_prio"], list(PRIORITY), key="bf_prio", format_func=lambda x: tr(PRIORITY, x))
    f_cause = c[3].selectbox(T["filter_cause"], [None, "pre_existing", "amendment"], key="bf_cause",
                             format_func=lambda x: T["all"] if x is None else tr(CAUSE, x))
    sel = [r for r in rows if (not f_dir or info[r["doc_version_id"]]["direction"] in f_dir)
           and (f_doc is None or r["doc_version_id"] == f_doc) and (not f_prio or r["priority"] in f_prio)
           and (f_cause is None or r["cause"] == f_cause)]
    shown_dirs = [d for d in opts if not f_dir or d in f_dir]
    if f_doc is not None:                       # one document chosen: only its direction
        shown_dirs = [info[f_doc]["direction"]]
    return sel, shown_dirs


def render(cfg, store, user, company_id: str):
    from board_lib import board_html, group
    from kg.services import tasks as TK
    try:                                     # the ranking weights may have changed (configs/kg.yaml): re-rank
        TK.rescore(cfg, store, company_id)
    except Exception:  # noqa: BLE001
        pass
    can_edit = user["user_type"] == "entity"
    rows = TK.list_tasks(cfg, store, user, company_id)
    if not rows:
        st.caption(T["no_tasks"])
        return
    info = doc_info(store, {r["doc_version_id"] for r in rows})
    sel, shown_dirs = _filters(cfg, rows, info)
    st.caption(f"{len(sel)} / {len(rows)} {T['tasks_n']}")
    key = st.session_state.get("task_key")
    if key and any(r["key"] == key for r in rows):
        st.dialog(T["task_title"], width="large", on_dismiss=_close)(_detail)(cfg, store, user, key, can_edit)
    try:
        from kg.services import accounts
        users = {m["id"]: m["name"] for m in accounts.members(store, user, company_id)}
    except Exception:  # noqa: BLE001 - a regulator may not list members
        users = {}
    from common import direction_label
    groups = {g["direction"]: g for g in group(sel, info)}
    # every direction gets its frame (in the order of the filter), also the ones without documents yet
    ordered = [groups.get(d) or {"direction": d, "docs": []} for d in shown_dirs]
    labels = {d: direction_label(cfg, d) for d in shown_dirs}
    html = board_html(ordered, _labels(), labels, users, _irrelevant(store, info))
    res = _component()(data=html, key="task_board", on_open_change=lambda: None, on_goto_change=lambda: None)
    go = getattr(res, "goto", None)
    if isinstance(go, dict) and go.get("vid") and go.get("t") != st.session_state.get("_doc_click"):
        st.session_state["_doc_click"] = go.get("t")
        open_analysis(go["vid"])
        st.rerun()
    hit = getattr(res, "open", None)
    if isinstance(hit, dict) and hit.get("key") and hit.get("t") != st.session_state.get("_task_click"):
        st.session_state["task_key"] = hit["key"]
        st.session_state["_task_click"] = hit.get("t")            # one click opens the window once
        st.rerun()
