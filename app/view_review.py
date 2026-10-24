"""Review of one document version: coloured text (hover cards) on the left, proposals on the right."""
from __future__ import annotations

import streamlit as st
from common import html_frame, diff_html, prio_badge, verdict_badge, version_label, with_llm, word_diff
from i18n_ka import VERDICT_COLOR, KIND, ROLE, T, TASK_STATUS, tr
from review_lib import build_review, render_html, revised_html, revised_view, table_html


def _pick_version(cfg, store, user):
    """Choose the version to review; the same search / status / type / sort filters as on the documents page."""
    from kg.services import documents as D
    from view_docs import _tile_data, apply_filters, filter_bar
    cid = user["company_id"] if user["user_type"] == "entity" else st.session_state.get("company_id")
    if not cid:
        st.caption(T["no_docs"])
        return None
    # one entry per VERSION (a family reduced to that version, so the tile helpers apply as they are)
    entries = [_tile_data(store, {**f, "versions": [v]}) for f in D.list_families(store, user, cid)
               for v in f["versions"]]
    if not entries:
        st.caption(T["no_docs"])
        return None
    shown = apply_filters(entries, *filter_bar("rf", entries, sort=False, status_default=["draft"]))
    st.caption(f"{len(shown)} / {len(entries)}")
    if not shown:
        st.info(T["no_match"])
        return None
    rows = [e["latest"]["id"] for e in shown]
    titles = {e["latest"]["id"]: e["family"]["title"] or "" for e in shown}
    cur = st.session_state.get("review_version")
    idx = rows.index(cur) if cur in rows else 0
    vid = st.selectbox(T["pick_version"], rows, index=idx, key="rf_pick",
                       format_func=lambda i: f"{version_label(store, i)} — {titles.get(i, '')[:60]}")
    st.session_state["review_version"] = vid
    return vid


def _close_proposal():
    st.session_state.pop("prop_dlg", None)
    st.session_state.pop("prop_mode", None)


def _done():
    """After a decision: close the window and redraw the page."""
    _close_proposal()
    st.rerun()


def _task_panel(cfg, store, user, model, vid, oid, can_edit):
    """Everything about one proposal (shown in the window that opens from the proposed document)."""
    from kg.services import decisions as DC
    card = model["cards"][oid]
    pr = model["proposals"].get(oid)
    t = model["tasks"].get(oid)
    key = t["key"] if t else oid
    bits = verdict_badge(card["verdict"])
    if t:
        bits += " " + prio_badge(t["priority"]) + f" {tr(TASK_STATUS, t['status'])}"
    st.markdown(bits, unsafe_allow_html=True)
    st.markdown(f"**{T['obligation']}:** {card['statement']}")
    if card.get("missing"):
        st.caption(card["missing"])
    if pr:
        st.markdown(f"**{T['explanation']}:** {pr['explanation']}")
        for r in card.get("regulation", []):
            src = f"[{r.get('label') or r['id']}]({r['url']})" if r.get("url") else (r.get("label") or r["id"])
            st.markdown(f"**{T['regulation']}:** {src}" + (f" — „{r['quote']}“" if r.get("quote") else ""))
        if pr["kind"] == "adjust_clause" and pr["original_text"]:
            st.markdown(f"**{T['changes_in_text']}:**")
            st.markdown(diff_html(word_diff(pr['original_text'], pr['proposed_text'])), unsafe_allow_html=True)
            o, n = st.columns(2)
            o.markdown(f"**{T['original']}**")
            o.write(pr["original_text"])
            n.markdown(f"**{T['proposed']}**")
            n.write(pr["proposed_text"])
        else:
            st.markdown(f"**{T['proposed']}:** " + (f"**{pr['heading']}** " if pr.get("heading") else "")
                        + pr["proposed_text"])
        fl = pr["flags"] if isinstance(pr["flags"], list) else []
        if "ungrounded_numbers" in str(fl):
            st.warning(T["flag_numbers"])
    if not (can_edit and t):
        return
    c = st.columns(4)
    if t["status"] == "open" and t["proposal_id"] and c[0].button("✓ " + T["accept"], key=f"ac{oid}",
                                                                  type="primary"):
        DC.accept(store, user, key)
        _done()
    if t["status"] in ("accepted", "not_relevant", "already_addressed") and c[3].button(T["reopen"], key=f"ro{oid}"):
        DC.reopen(store, user, key)
        _done()
    if t["status"] in ("open", "accepted"):
        reason = st.text_input(T["reason_reject"], key=f"rs{oid}")
        if c[1].button("✕ " + T["reject"], key=f"nr{oid}") and reason.strip():
            DC.not_relevant(store, user, key, reason)
            _done()
        clauses = [u["id"] for u in model["units"] if u["unit_type"] == "clause"]
        pick = st.multiselect(T["pick_clauses"], clauses, key=f"pk{oid}",
                              format_func=lambda i: next((f"{u['section_no'] or ''} {u['text'][:50]}"
                                                          for u in model["units"] if u["id"] == i), i))
        if c[2].button(T["already_addressed"], key=f"aa{oid}") and pick:
            DC.already_addressed(store, user, key, pick)
            _done()
    with st.expander(T["edit_edges"]):
        for e in DC.edges_of(store, vid, oid):
            st.write(f"{e['clause_id'].split(':')[-1]} · {tr(ROLE, e['role'])} · {e.get('source')}")
        clauses = [u["id"] for u in model["units"] if u["unit_type"] == "clause"]
        cl = st.selectbox(T["clause"], clauses, key=f"ec{oid}",
                          format_func=lambda i: next((f"{u['section_no'] or ''} {u['text'][:50]}"
                                                      for u in model["units"] if u["id"] == i), i))
        role = st.selectbox(T["role"], ["full", "partial", "contradicts"], key=f"er{oid}",
                            format_func=lambda r: tr(ROLE, r))
        e1, e2, e3 = st.columns(3)

        def edit(fn, *a):
            with_llm(cfg, store, "edge", lambda run, llm: fn(cfg, store, run, user, vid, oid, *a))
            st.rerun()

        if e1.button(T["add_edge"], key=f"ea{oid}"):
            edit(DC.edge_add, cl, role)
        if e2.button(T["change_edge"], key=f"ech{oid}"):
            edit(DC.edge_change, cl, role)
        if e3.button(T["remove_edge"], key=f"erm{oid}"):
            edit(DC.edge_remove, cl)
        if st.button(T["recheck"], key=f"rc{oid}"):
            from kg.mapping.pipeline import recheck_obligation
            from kg.mapping.proposals import propose_document
            from kg.services.tasks import sync_tasks

            def job(run, llm):
                recheck_obligation(cfg, store, run, llm, vid, oid)
                propose_document(cfg, store, run, llm, vid)
                return sync_tasks(cfg, store, run, vid, user["id"], only={oid})

            with_llm(cfg, store, "recheck", job)
            st.rerun()


ROLE_LABEL = {"contradicts": "lg_contradicts", "partial": "lg_partial", "anchor": "grp_missing", "full": "lg_ok"}
ROLE_COLOR = {"contradicts": "🟥", "anchor": "🟦", "partial": "🟧", "full": "🟩"}


def _choose(label: str, options: list, default: list, key: str, fmt):
    return list(st.multiselect(label, options, default=default, key=key, format_func=fmt) or [])


def _doc_filters():
    """What BOTH documents show: by default only contradictions and missing parts of RELEVANT obligations."""
    c1, c2 = st.columns([3, 2])
    with c1:
        roles = _choose(T["dv_show"], list(ROLE_LABEL), ["contradicts", "anchor"], "dv_roles",
                        lambda r: f"{ROLE_COLOR[r]} {T[ROLE_LABEL[r]]}")
    with c2:
        rels = _choose(T["col_relevance"], ["relevant", "possibly"], ["relevant"], "dv_rel",
                       lambda r: T[REL_LABEL[r]])
    return roles, rels


REL_COLOR = {"relevant": "#2c3e50", "possibly": "#7f8c8d", "irrelevant": "#bdc3c7"}
REL_LABEL = {"irrelevant": "grp_irrelevant", "possibly": "grp_possibly", "relevant": "grp_relevant"}
COV_LABEL = {"contradicts": "grp_contradicts", "none": "grp_missing", "partial": "grp_partial", "full": "grp_full",
             "undecided": "grp_undecided", None: "grp_not_judged"}


def _obligation_table(model):
    """Every obligation of the check in ONE table (obligation, relevance, coverage, reasoning, source + link),
    with search and filters on top."""
    import pandas as pd
    rows = model["table"]
    c = st.columns([4, 2, 2])
    q = c[0].text_input(T["search_docs"], key="ot_q", placeholder=T["obl_search_hint"])
    rel = c[1].multiselect(T["col_relevance"], list(REL_LABEL), key="ot_rel", format_func=lambda k: T[REL_LABEL[k]])
    covs = [k for k in COV_LABEL if any(r["verdict"] == k for r in rows)]
    cov = c[2].multiselect(T["col_coverage"], covs, key="ot_cov", format_func=lambda k: T[COV_LABEL[k]])
    words = (q or "").lower().split()
    shown = [r for r in rows if (not rel or r["relevance"] in rel) and (not cov or r["verdict"] in cov)
             and all(w in f"{r['statement']} {r['reason']} {r['source']}".lower() for w in words)]
    st.caption(f"{len(shown)} / {len(rows)}")
    df = pd.DataFrame([{"#": i, T["col_obligation"]: r["statement"], T["col_relevance"]: T[REL_LABEL[r["relevance"]]],
                        T["col_coverage"]: T[COV_LABEL[r["verdict"]]] if r["relevance"] != "irrelevant" else "—",
                        T["col_reasoning"]: r["reason"], T["col_source"]: r["source"], T["col_link"]: r["url"]}
                       for i, r in enumerate(shown, 1)])
    labels = {"n": "#", **{k: T[k] for k in ("col_obligation", "col_relevance", "col_coverage", "col_reasoning",
                                               "col_source")}}
    html_frame(table_html(shown, labels, {k: T[v] for k, v in REL_LABEL.items()},
                          {k: T[v] for k, v in COV_LABEL.items()}, REL_COLOR, VERDICT_COLOR),
               min(90 + 70 * max(len(shown), 1), 620))
    st.download_button(T["download_csv"], df.to_csv(index=False).encode("utf-8-sig"), file_name="obligations.csv",
                       mime="text/csv", key="ot_csv")


def _revised_component():
    """The proposed version: an HTML document whose buttons (shown on hover) send the click back to Python.
    Declared on every run (cheap): a component cached across runs is unknown to a new Streamlit runtime."""
    from review_lib import REVISED_CSS, REVISED_JS
    return st.components.v2.component("goni_revised_document", css=REVISED_CSS, js=REVISED_JS)


def _oids(action: dict) -> list[str]:
    raw = action.get("oids") or action.get("oid") or ""
    return [o for o in (raw if isinstance(raw, list) else str(raw).split("|")) if o]


def handle_action(cfg, store, user, model, vid, action: dict | None, state=None) -> str | None:
    """A click in the proposed version. One block = one place of the document (one or several proposals):
    accept / undo / save (an edited text, then accept) act at once on all of them; reject and details open the
    proposal window."""
    state = st.session_state if state is None else state
    if not action or not _oids(action):
        return None
    from kg.services import decisions as DC
    from kg.services.edits import save_edit
    oids, kind = [o for o in _oids(action) if o in model["tasks"]], action.get("action")
    tasks = [model["tasks"][o] for o in oids]
    if not tasks:
        return None
    if kind == "save":
        text = (action.get("text") or "").strip()
        props = [model["proposals"][o] for o in oids if o in model["proposals"]]
        if not text or not props:
            return None
        slot = action.get("slot") or ""
        old = (model.get("edits") or {}).get(slot) or {}
        save_edit(store, user, vid, slot, text, [p["id"] for p in props], "user", notes=old.get("notes"))
        DC.record_decision(store, user, "edit_proposal", doc_version_id=vid, obligation_id=oids[0],
                           proposal_id=props[0]["id"], data={"slot": slot, "text": text, "obligations": oids})
        for t in tasks:
            if t["status"] == "open":
                DC.accept(store, user, t["key"])
        return kind
    if kind == "accept":
        _keep_preview(store, user, model, vid, action.get("slot"), oids)
        for t in tasks:
            if t["status"] == "open":
                DC.accept(store, user, t["key"])
        return kind
    if kind == "undo":
        for t in tasks:
            if t["status"] == "accepted":
                DC.reopen(store, user, t["key"])
        return kind
    if kind in ("reject", "details"):
        state["prop_dlg"] = "|".join(oids)
        state["prop_mode"] = kind
        return kind
    return None


def _keep_preview(store, user, model, vid, slot, oids):
    """Accepting a merged block that only has the word-level preview: keep exactly the text the user saw."""
    from review_lib import revised_view
    from kg.services.edits import save_edit
    for x in revised_view(model)["units"]:
        s = x["change"]
        if s and s["slot"] == slot and s["source"] == "preview":
            save_edit(store, user, vid, slot, s["text"], s["pids"], "preview")


def _revised_document(cfg, store, user, model, vid, can_edit, roles=None, rels=None, part="doc", focus=None,
                      focus_clause=None):
    """The proposed version (``part="doc"``, right half) or the proposals for new documents (``part="new"``, own
    tab): hovering a change shows why, and accept / reject / edit."""
    labels = {k: T[k] for k in ("accept", "reject", "details", "undo", "obligation", "new_docs", "edit", "save_accept",
                                "cancel", "merged_from", "preview_merge", "overlap_warn", "user_edited",
                                "model_merged")}
    labels["accepted"] = T["accepted_mark"]
    html = revised_html(model, labels, can_edit, roles, rels, part, focus, focus_clause if part == "doc" else None)
    if not html:
        st.caption(T["no_match"])
        return
    try:
        res = _revised_component()(data=html, key=f"revised_{part}_{vid}", on_action_change=lambda: None)
        action = getattr(res, "action", None)
    except Exception:  # noqa: BLE001 - an older Streamlit without components v2: show the text without buttons
        from review_lib import DOC_HEIGHT, REVISED_CSS
        html_frame(f"<style>{REVISED_CSS}html,body{{margin:0;overflow:hidden}}</style>{html}", DOC_HEIGHT + 6)
        action = None
    done = handle_action(cfg, store, user, model, vid, action if isinstance(action, dict) else None)
    if done:
        st.rerun()


def _shown_slots(rv: dict, roles, rels) -> list[dict]:
    """The changes of the proposed version that the filters show (the clause changes and the added sections)."""
    from review_lib import slot_shown
    slots = [x["change"] for x in rv["units"] if x["change"]] + [y for x in rv["units"] for y in x["inserts"]]
    return [s for s in slots if slot_shown(s, roles, rels)]


def _accept_all(store, user, model, vid, slots: list[dict]) -> int:
    from kg.services import decisions as DC
    n = 0
    for s in slots:
        oids = [o for o in s["oids"] if (model["tasks"].get(o) or {}).get("status") == "open"]
        if not oids:
            continue
        _keep_preview(store, user, model, vid, s["slot"], oids)
        for o in oids:
            DC.accept(store, user, model["tasks"][o]["key"])
            n += 1
    return n


def _reject_all_window(store, user, model, slots: list[dict]):
    from kg.services import decisions as DC
    keys = [model["tasks"][o]["key"] for s in slots for o in s["oids"]
            if (model["tasks"].get(o) or {}).get("status") in ("open", "accepted")]
    st.markdown(T["reject_all_n"].format(n=len(keys)))
    reason = st.text_input(T["reason_reject"], key="rv_rej_reason")
    if st.button("✕ " + T["reject_all"], key="rv_rej_go", type="primary", disabled=not (reason or "").strip() or not keys):
        for k in keys:
            DC.not_relevant(store, user, k, reason)
        st.session_state.pop("rv_reject_all", None)
        st.rerun()


def _revised_file(cfg, store, user, model, vid):
    """A Word file of the document with the accepted changes applied (to edit and upload as the next version)."""
    st.caption(T["revised_help"])
    if st.button(T["build_revised"], key="br", use_container_width=True):
        from kg.services.revise import build_revised
        ids = [t["proposal_id"] for t in model["tasks"].values() if t["status"] == "accepted" and t["proposal_id"]]
        if not ids:
            st.info(T["no_accepted"])
        else:
            st.session_state["revised"] = with_llm(
                cfg, store, "revise", lambda run, llm: build_revised(cfg, store, run, llm, user, vid, ids))
    rev = st.session_state.get("revised")
    if rev:
        st.write(f"{T['changed_clauses']}: {rev['changed_clauses']} · {T['added']}: {rev['added']}")
        for key, label, mime in (("docx", T["download_docx"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                                 ("md", T["download_md"], "text/markdown")):
            p = cfg.path("uploads") / rev[key]
            if p.exists():
                st.download_button(label, p.read_bytes(), file_name=p.name, mime=mime, key=f"dl{key}",
                                   use_container_width=True)


def _publish(store, user, vid):
    """ONE action: publishing; when issues are still open the same button says so and publishes anyway."""
    from kg.services import documents as D
    st.caption(T["publish_help"])
    n_open = D.open_task_count(store, vid)
    label = T["publish_anyway"].format(n=n_open) if n_open else T["publish"]
    if st.button(label, type="primary", key="pub", use_container_width=True):
        D.publish(store, user, vid, force=bool(n_open))
        st.success(T["published_ok"])
        st.rerun()


def _proposed_actions(cfg, store, user, model, vid, rv, roles, rels):
    """Under "Proposed version": accept all, reject all, the Word file and publishing (one row, equal buttons)."""
    slots = _shown_slots(rv, roles, rels)
    a, r, f, p = st.columns(4, gap="small")
    if a.button(T["accept_all"], key="rv_acc_all", icon=":material/done_all:", help=T["accept_all_help"],
                use_container_width=True, disabled=not slots):
        n = _accept_all(store, user, model, vid, slots)
        st.toast(T["accepted_all"].format(n=n))
        st.rerun()
    if r.button(T["reject_all"], key="rv_rej_all", icon=":material/close:", help=T["reject_all_help"],
                use_container_width=True, disabled=not slots):
        st.session_state["rv_reject_all"] = True
    with f.popover(T["revised_file"], icon=":material/download:", use_container_width=True):
        _revised_file(cfg, store, user, model, vid)
    with p.popover(T["publish"], icon=":material/publish:", use_container_width=True):
        _publish(store, user, vid)
    if st.session_state.get("rv_reject_all"):
        st.dialog(T["reject_all_title"], on_dismiss=lambda: st.session_state.pop("rv_reject_all", None))(
            _reject_all_window)(store, user, model, slots)


def _merge_button(cfg, store, user, vid, pending: list[str]):
    """Clauses that several proposals change and that only have the word-level preview: merge them with the model."""
    if not pending:
        return
    if st.button(f"{T['merge_model']} ({len(pending)})", key="mrg", help=T["merge_pending_n"]):
        from kg.services.revise import merge_pending
        with st.spinner(T["merge_model"]):
            with_llm(cfg, store, "merge", lambda run, llm: merge_pending(cfg, store, run, llm, vid))
        st.rerun()


def _proposal_window(cfg, store, user, model, vid, oids, can_edit):
    """The window of one block: one proposal, or several merged ones (each in its own section)."""
    if len(oids) == 1:
        import view_chat
        if st.button(view_chat._s("ask_followup"), key=f"ask_fu_{oids[0]}", icon=":material/chat:"):
            view_chat.open_with(store, cfg, version_id=vid, obligation_id=oids[0])
            _close_proposal()                       # the window closes; the panel opens on the same item
            st.rerun()
        _task_panel(cfg, store, user, model, vid, oids[0], can_edit)
        return
    from review_lib import revised_view
    s = next((x["change"] for x in revised_view(model)["units"] if x["change"] and x["change"]["oids"] == oids), None)
    if s:
        st.markdown(T["merged_from"].format(n=len(oids)))
        st.markdown(f"**{T['proposed']}:**")
        st.write(s["text"])
    from kg.services import decisions as DC
    if can_edit:
        reason = st.text_input(T["reason_reject"], key="rs_all")
        if st.button("✕ " + T["reject_all"], key="nr_all") and reason.strip():
            for o in oids:
                t = model["tasks"].get(o)
                if t and t["status"] in ("open", "accepted"):
                    DC.not_relevant(store, user, t["key"], reason)
            _done()
    for i, o in enumerate(oids, 1):
        with st.container(border=True):          # (not an expander: the panel has one inside)
            st.markdown(f"**{i}.**")
            _task_panel(cfg, store, user, model, vid, o, can_edit)


def render(cfg, store, user):
    vid = _pick_version(cfg, store, user)
    if not vid:
        return
    from kg.services import documents as D
    ver = D.get_version(store, user, vid)
    from kg.services import jobs
    if jobs.is_running(store, vid):
        st.info(T["review_running"])
    can_edit = user["user_type"] == "entity" and ver["status"] == "draft"
    if user["user_type"] == "regulator" or ver["status"] != "draft":
        st.caption(T["read_only"])
    model = build_review(cfg, store, vid)
    cnt = model["counts"]
    st.write(" · ".join(f"{tr(TASK_STATUS, k)}: {v}" for k, v in sorted(cnt.items())) or T["no_tasks"])
    focus = st.session_state.get("review_focus")
    if focus and focus.get("version_id") != vid:
        focus = None
    if focus:
        b1, b2 = st.columns([8, 1], vertical_alignment="center")
        where = T["focus_new_doc"] if focus.get("kind") == "new_document" else ""
        from i18n_lang import localize
        b1.info(f"📍 {localize(focus['title'])}" + (f" — {where}" if where else ""))
        if b2.button("✕", key="focus_off", help=T["close"]):
            st.session_state.pop("review_focus", None)
            st.rerun()
    sel = [o for o in (st.session_state.get("prop_dlg") or "").split("|") if o in model["cards"]]
    if sel:
        st.dialog(T["proposal_title"], width="large", on_dismiss=_close_proposal)(_proposal_window)(
            cfg, store, user, model, vid, sel, can_edit)
    rv = revised_view(model)
    # row 1: the controls of each half; row 2: one-line titles; row 3: the two documents (same box, same height)
    top_l, top_r = st.columns(2, vertical_alignment="bottom")
    with top_l:
        roles, rels = _doc_filters()
    with top_r:
        from review_lib import slot_shown
        slots = [x["change"] for x in rv["units"] if x["change"]] + [y for x in rv["units"] for y in x["inserts"]]
        n_shown = sum(1 for y in slots if slot_shown(y, roles, rels))
        st.caption(f"{T['changes_n']}: {n_shown} / {rv['count']} · {T['accepted_n']}: {rv['accepted']}")
        if can_edit:
            _merge_button(cfg, store, user, vid, rv["pending_merge"])
    left, right = st.columns(2)
    left.markdown(f"**{T['doc_original']}**")
    right.markdown(f"**{T['doc_proposed']}**")
    if can_edit:                     # right under the title: what can be done with the whole proposed version
        _, right = st.columns(2)
        with right:
            _proposed_actions(cfg, store, user, model, vid, rv, roles, rels)
    left, right = st.columns(2)
    with left:
        from review_lib import DOC_HEIGHT
        html_frame(render_html(model, roles, rels, focus=(focus or {}).get("clause_id")), DOC_HEIGHT + 6)
    with right:
        _revised_document(cfg, store, user, model, vid, can_edit, roles, rels, focus=(focus or {}).get("slot"),
                          focus_clause=(focus or {}).get("clause_id"))
    # under both documents, full width: the proposals for new documents and the obligations table
    new_n = len(rv["new_documents"])
    tab_new, tab_obl = st.tabs([f"{T['new_docs']} ({new_n})", f"{T['obl_table']} ({len(model['table'])})"])
    with tab_new:
        st.caption(T["unplaced_hint"])
        if new_n:
            _revised_document(cfg, store, user, model, vid, can_edit, roles, rels, part="new",
                              focus=(focus or {}).get("slot"))
        else:
            st.caption(T["no_match"])
    with tab_obl:
        _obligation_table(model)
    if can_edit:
        st.divider()
        if st.button(T["discard"], key="disc", help=T["discard_help"]):
            D.discard_draft(store, user, vid, cfg)
            st.session_state.pop("review_version", None)
            st.rerun()
