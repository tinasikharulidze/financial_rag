"""Documents of the company: a grid of document tiles with search and filters, an upload window (upload ->
profile confirmation), the long check, and the versions of each document."""
from __future__ import annotations

import html

import streamlit as st
from common import badge, direction_label, directions, get_ctx, version_label, with_llm
from i18n_ka import DOC_TYPE, T, tr

STATUS = {"draft": "status_draft", "published": "status_published", "replaced": "status_replaced",
          "archived": "status_archived"}
STATUS_COLOR = {"draft": "#e67e22", "published": "#27ae60", "replaced": "#7f8c8d", "archived": "#7f8c8d"}
PER_ROW = 5


def _status_label(s: str) -> str:
    return tr({k: T[x] for k, x in STATUS.items()}, s)


# ------------------------------------------------------------------------------------------------ background check
JOB_LABEL = {"queued": "job_queued", "running": "job_running", "done": "job_done", "failed": "job_failed",
             "budget": "job_budget", "unavailable": "job_unavailable", "rejected": "job_rejected"}


def launch_check(cfg, store, user, version_id: str):
    """Start the check in the background. Tests replace the launcher with ``st.session_state['_job_launcher']``."""
    from kg.services import jobs
    launcher = st.session_state.get("_job_launcher")
    if launcher:
        return launcher(cfg, store, user, version_id)
    return jobs.launch(cfg, store, user, version_id)


def _job_line(store, version_id: str) -> tuple[str | None, str | None]:
    """(status, one Georgian line for the tile) of the version's background check."""
    from kg.services import jobs
    j = jobs.get_job(store, version_id)
    if not j:
        return None, None
    label = T[JOB_LABEL.get(j["status"], "job_failed")]
    if j["status"] in jobs.ACTIVE:
        p = jobs.progress(store, version_id)
        return j["status"], (f"⏳ {label} · {T['pr_triage']} {p['triage']} · {T['pr_judged']} {p['judged']} · "
                             f"{T['pr_proposals']} {p['proposals']}")
    if j["status"] == "done":
        return "done", f"✓ {label}"
    return j["status"], f"⚠ {label}" + (f": {j['message'][:160]}" if j.get("message") else "")


# ------------------------------------------------------------------------------------------------ upload window
def _close_dialog():
    st.session_state.pop("dlg", None)


def _profile_form(cfg, store, version_id: str, prof: dict) -> dict:
    from kg.company import profile as P
    st.caption(T["profile_hint"] if prof["from_model"] else T["profile_failed"])
    types = P.doc_type_vocab(store)
    if prof["doc_type"] not in types:
        types = types + [prof["doc_type"]]
    doc_type = st.selectbox(T["doc_type"], types, index=types.index(prof["doc_type"]),
                            format_func=lambda x: tr(DOC_TYPE, x), key=f"pt{version_id}")
    avail = P.domains_available(cfg, store)
    domains = st.multiselect(T["domains"], sorted(set(avail) | set(prof["domains"])), default=prof["domains"],
                             key=f"pd{version_id}")
    purpose = st.text_area(T["purpose"], prof["purpose"] or "", key=f"pp{version_id}")
    secs = []
    with st.expander(f"{T['sections']} ({len(prof['sections'])})"):
        for i, s in enumerate(prof["sections"]):
            st.markdown(f"**{s.get('section_no')}** {s.get('title') or s.get('heading') or ''}")
            summary = st.text_input(T["summary"], s.get("summary", ""), key=f"ps{version_id}{i}")
            goal = st.text_input(T["goal"], s.get("goal", ""), key=f"pg{version_id}{i}")
            secs.append({"section_no": s.get("section_no"), "summary": summary, "goal": goal})
    return {"doc_type": doc_type, "domains": domains, "purpose": purpose, "sections": secs}


def _dialog_upload_step(cfg, store, user, fams):
    from kg.services import documents as D
    d = st.session_state["dlg"]
    st.markdown(f"**{T['step_upload']}** · {T['step_profile']}")
    modes = ["new_document", "new_version"] if fams else ["new_document"]
    mode = st.radio(T["upload_mode"], modes, horizontal=True, format_func=lambda m: T[m], key="up_mode")
    family_id, policy = None, "keep"
    if mode == "new_version":
        family_id = st.selectbox(T["document"], [f["id"] for f in fams], key="up_fam",
                                 format_func=lambda i: next(f"{f['code']} · {f['title'] or ''}" for f in fams
                                                            if f["id"] == i))
        policy = st.radio(T["version_policy"], ["keep", "replace"], horizontal=True,
                          format_func=lambda p: T["policy_" + p], key="up_pol")
    from kg.services.directions import get_directions
    dirs = directions(cfg)
    pre = get_directions(store).get(family_id) if family_id else None
    if mode == "new_version" and pre in dirs and st.session_state.get("_up_dir_for") != family_id:
        st.session_state["up_dir"] = pre                   # a new version starts with its document's direction
        st.session_state["_up_dir_for"] = family_id
    direction = st.selectbox(T["filter_direction"], dirs, index=None, key="up_dir", placeholder=T["choose_direction"],
                             format_func=lambda x: direction_label(cfg, x))
    up = st.file_uploader(T["pdf_file"], type="pdf", key="up_file")
    if d.get("error"):
        st.error(d["error"])
    c1, c2 = st.columns(2)
    if c2.button(T["cancel"], key="up_cancel", use_container_width=True):
        _close_dialog()
        st.rerun()
    if c1.button(T["upload"], type="primary", key="up_btn", disabled=up is None or direction is None,
                 use_container_width=True):
        try:
            res = D.upload(cfg, store, user, up.getvalue(), up.name, mode=mode, family_id=family_id,
                           version_policy=policy)
        except Exception as exc:  # noqa: BLE001 - DocumentError and unreadable files are shown in the window
            d["error"] = str(exc)
            st.rerun(scope="fragment")
        if res["status"] == "duplicate":
            d["error"] = f"{T['duplicate']} ({version_label(store, res['version_id'])})"
            st.rerun(scope="fragment")
        from kg.services.directions import set_direction
        fam = store.q1("SELECT family_id FROM document_versions WHERE id=?", (res["version_id"],))
        if fam:
            set_direction(store, user, fam["family_id"], direction)
        d.clear()
        d.update(step="profile", version=res["version_id"],
                 info=f"{T['parsed']}: {res['units']} {T['units']}, {res.get('pages', '')} {T['pages']}"
                      + ("" if res.get("doc_date") else f" · {T['no_date']}"),
                 warnings=list(res.get("warnings") or []))
        st.rerun(scope="fragment")


def _dialog_profile_step(cfg, store, user):
    from kg.company import profile as P
    d = st.session_state["dlg"]
    vid = d["version"]
    st.markdown(f"{T['step_upload']} · **{T['step_profile']}** — {version_label(store, vid)}")
    if d.get("info"):
        st.success(d["info"])
    for w in d.get("warnings", []):
        st.caption(f"{T['warnings']}: {w}")
    prof = P.get_profile(store, vid)
    if prof is None:
        with st.spinner(T["building_profile"]):
            prof = with_llm(cfg, store, "profile", lambda run, llm: P.build_profile(cfg, store, run, llm, vid),
                            {"version": vid})
        if prof is None:                                     # quota / unavailable: the message is shown above
            if st.button(T["close"], key="pf_close"):
                _close_dialog()
                st.rerun()
            return
    values = _profile_form(cfg, store, vid, prof)
    st.caption(T["confirm_starts_check"])
    c1, c2 = st.columns(2)
    confirm = c1.button(T["confirm"], type="primary", key="pf_confirm", use_container_width=True)
    if c2.button(T["later"], key="pf_later", use_container_width=True):
        _close_dialog()
        st.rerun()
    if confirm:
        P.confirm_profile(store, user, vid, **values)
        launch_check(cfg, store, user, vid)
        st.session_state["flash"] = f"{T['uploaded_ok']}: {version_label(store, vid)}. {T['check_started']}"
        _close_dialog()
        st.rerun()


def _upload_dialog_body(cfg, store, user, fams):
    if st.session_state.get("dlg", {}).get("step") == "profile":
        _dialog_profile_step(cfg, store, user)
    else:
        _dialog_upload_step(cfg, store, user, fams)


# ------------------------------------------------------------------------------------------------ tiles
def _tile_data(store, f: dict) -> dict:
    from kg.company import profile as P
    from kg.services import documents as D
    v = f["versions"][0] if f["versions"] else None
    prof = P.get_profile(store, v["id"]) if v else None
    node = store.get_node(v["id"]) if v else None
    doc_type = (prof or {}).get("doc_type") or ((node or {}).get("props") or {}).get("doc_type") or "other"
    checked = bool(v and store.q1("SELECT 1 FROM coverage WHERE doc_version_id=? LIMIT 1", (v["id"],)))
    job_status, job_line = _job_line(store, v["id"]) if v else (None, None)
    from kg.services.directions import get_directions
    direction = get_directions(store).get(f["id"]) or "other"             # the tag chosen by a person
    return {"family": f, "latest": v, "doc_type": doc_type, "direction": direction, "open": D.open_task_count(store, v["id"]) if v else 0,
            "job": job_status, "job_line": job_line,
            "profile_confirmed": bool(prof and prof["confirmed"]), "checked": checked,
            "file": ((node or {}).get("props") or {}).get("file") or "", "uploaded": (v or {}).get("uploaded_at") or ""}


def _matches(t: dict, query: str, statuses: list, dirs: list) -> bool:
    f, v = t["family"], t["latest"]
    if statuses and (v or {}).get("status") not in statuses:
        return False
    if dirs and t.get("direction", "other") not in dirs:
        return False
    q = (query or "").strip().lower()
    if not q:
        return True
    hay = " ".join([f["code"] or "", f["title"] or "", t["file"], t["doc_type"], tr(DOC_TYPE, t["doc_type"])]).lower()
    return all(w in hay for w in q.split())


def _tile(cfg, store, user, t: dict):
    f, v = t["family"], t["latest"]
    status = (v or {}).get("status", "draft")
    title = html.escape(f["title"] or "")
    if t["job_line"]:
        note = t["job_line"]
    elif v and not t["profile_confirmed"]:
        note = "⚠ " + T["needs_profile"]
    elif v and not t["checked"]:
        note = "⚠ " + T["needs_check"]
    else:
        note = ""
    # every part has a fixed height (long text is cut with "…"), so all tiles of a row are equally tall
    clamp = "display:-webkit-box;-webkit-box-orient:vertical;overflow:hidden;-webkit-line-clamp:"
    st.markdown(
        f"<div style='display:flex;gap:10px;align-items:flex-start;height:4.6em'>"
        f"<div style='font-size:38px;line-height:1'>📄</div><div style='min-width:0'>"
        f"<div style='font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>"
        f"{html.escape(f['code'])}</div>"
        f"<div style='font-size:0.85em;color:#666;line-height:1.3em;{clamp}2' title='{title}'>{title}</div>"
        f"</div></div>"
        f"<div style='white-space:nowrap;overflow:hidden;text-overflow:ellipsis;height:1.9em'>"
        + badge(_status_label(status), STATUS_COLOR.get(status, "#555")) + " "
        + badge(tr(DOC_TYPE, t["doc_type"]), "#34495e") + " "
        + f"<span style='font-size:0.8em'>v{html.escape(str((v or {}).get('version', '')))}"
        + f" · {T['col_open']}: {t['open']}</span></div>"
        f"<div class='tile-note' style='font-size:0.8em;color:#777;line-height:1.3em;height:2.6em;{clamp}2'"
        f" title='{html.escape(note)}'>{html.escape(note)}</div>",
        unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    if v and c1.button(T["review"], key=f"tile_{f['id']}", type="primary", use_container_width=True):
        st.session_state["review_version"] = v["id"]
        st.session_state["goto"] = "review"
        st.rerun()
    with c2.popover(f"{T['versions']} ({len(f['versions'])})", use_container_width=True):
        if user["user_type"] == "entity":
            dirs = directions(cfg)
            cur = t.get("direction")
            dc1, dc2 = st.columns([3, 1], vertical_alignment="bottom")
            new = dc1.selectbox(T["filter_direction"], dirs, index=dirs.index(cur) if cur in dirs else None,
                                key=f"dir{f['id']}", placeholder=T["choose_direction"],
                                format_func=lambda x: direction_label(cfg, x))
            if dc2.button(T["save"], key=f"dirs{f['id']}", disabled=new is None or new == cur):
                from kg.services.directions import set_direction
                set_direction(store, user, f["id"], new)
                st.rerun()
            st.divider()
        for ver in f["versions"]:
            st.markdown(f"**v{ver['version']}** · {_status_label(ver['status'])} · {(ver['uploaded_at'] or '')[:10]}")
            b = st.columns(3)
            if b[0].button(T["review"], key=f"rv{ver['id']}"):
                st.session_state["review_version"] = ver["id"]
                st.session_state["goto"] = "review"
                st.rerun()
            from kg.company import profile as P
            from kg.services import jobs
            pr = P.get_profile(store, ver["id"])
            job = jobs.get_job(store, ver["id"])
            if ver["status"] != "replaced" and not (pr and pr["confirmed"]):
                if b[1].button(T["profile_step"], key=f"pr{ver['id']}"):
                    st.session_state["dlg"] = {"step": "profile", "version": ver["id"]}
                    st.rerun()
            elif ver["status"] != "replaced" and not (job and job["status"] in jobs.ACTIVE) and (
                    (job and job["status"] != "done") or not store.q1(
                        "SELECT 1 FROM coverage WHERE doc_version_id=? LIMIT 1", (ver["id"],))):
                if b[1].button(T["retry_check"], key=f"ck{ver['id']}"):
                    launch_check(cfg, store, user, ver["id"])
                    st.session_state["flash"] = T["check_started"]
                    st.rerun()
            if ver["status"] == "draft" and not (job and job["status"] in jobs.ACTIVE):
                if b[2].button(T["discard"], key=f"dc{ver['id']}"):
                    from kg.services import documents as D
                    D.discard_draft(store, user, ver["id"], cfg)
                    st.rerun()


# ------------------------------------------------------------------------------------------------ filters
def filter_bar(prefix: str, tiles: list[dict], sort: bool = True, status_default: list | None = None) -> tuple:
    """Search, status, direction (and sort) controls, shared by the documents page and the review page."""
    c = st.columns([4, 2, 2, 2] if sort else [4, 2, 2])
    query = c[0].text_input(T["search_docs"], key=f"{prefix}_q", placeholder=T["search_hint"])
    statuses = c[1].multiselect(T["status"], list(STATUS), key=f"{prefix}_status", format_func=_status_label,
                                default=status_default if f"{prefix}_status" not in st.session_state else None)
    cfg = get_ctx()[0]
    opts = directions(cfg) + (["other"] if any(t.get("direction") == "other" for t in tiles) else [])
    types = c[2].multiselect(T["filter_direction"], opts, key=f"{prefix}_dirs",
                             format_func=lambda x: direction_label(cfg, x))
    order = c[3].selectbox(T["sort"], ["code", "new", "tasks"], key=f"{prefix}_sort",
                           format_func=lambda k: T["sort_" + k]) if sort else "code"
    return query, statuses, types, order


def apply_filters(tiles: list[dict], query, statuses, types, order) -> list[dict]:
    shown = [t for t in tiles if _matches(t, query, statuses, types)]
    key = {"code": lambda t: (t["family"]["code"], t["uploaded"]), "new": lambda t: t["uploaded"],
           "tasks": lambda t: t["open"]}[order]
    return sorted(shown, key=key, reverse=order in ("new", "tasks"))


# ------------------------------------------------------------------------------------------------ page
def render(cfg, store, user):
    from kg.services import documents as D
    cid = user["company_id"]
    fams = D.list_families(store, user, cid)

    st.subheader(T["families"])
    if st.session_state.get("dlg"):              # the window's title follows the chosen language
        st.dialog(T["upload_title"], width="large", on_dismiss=_close_dialog)(_upload_dialog_body)(
            cfg, store, user, fams)
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))
    tiles = [_tile_data(store, f) for f in fams]
    shown = apply_filters(tiles, *filter_bar("doc", tiles))
    st.caption(f"{len(shown)} / {len(tiles)}")
    if not tiles:
        st.info(T["no_docs"])
    elif not shown:
        st.info(T["no_match"])
    running = any(t["job"] in ("queued", "running") for t in tiles)
    if running:
        st.caption(T["auto_refresh"])
    # while a check runs, the grid refreshes itself every 15 s (only this part of the page is redrawn)
    st.fragment(run_every=15 if running else None)(_grid)(cfg, store, user, [t["family"]["id"] for t in shown])


def _grid(cfg, store, user, family_ids: list[str]):
    """The upload card, then one collapsible group per direction (all closed at first) with its document tiles."""
    from kg.services import documents as D
    fams = {f["id"]: f for f in D.list_families(store, user, user["company_id"])}
    tiles = [_tile_data(store, fams[i]) for i in family_ids if i in fams]
    _cards(cfg, store, user, [None])                                     # None = the upload card
    chosen = st.session_state.get("doc_dirs") or []
    order = directions(cfg) + (["other"] if any(t["direction"] == "other" for t in tiles) else [])
    for d in order:
        if chosen and d not in chosen:
            continue
        group = [t for t in tiles if t["direction"] == d]
        with st.expander(f"{direction_label(cfg, d)} ({len(group)})", expanded=False, key=f"docdir_{d}"):
            if group:
                _cards(cfg, store, user, group)
            else:
                st.caption(T["no_docs_direction"])


def _cards(cfg, store, user, shown: list):
    for i in range(0, len(shown), PER_ROW):
        cols = st.columns(PER_ROW)
        for col, t in zip(cols, shown[i:i + PER_ROW]):
            with col.container(border=True):
                if t is None:
                    _upload_card()
                else:
                    _tile(cfg, store, user, t)


def _upload_card():
    """The first card of the grid: opens the upload window. Same height as a document tile."""
    st.markdown(
        "<div style='height:9.1em;display:flex;flex-direction:column;align-items:center;justify-content:center;"
        "border:2px dashed #c8d3df;border-radius:10px;color:#1f6fb2'>"
        "<div style='font-size:46px;line-height:1;font-weight:300'>+</div>"
        f"<div style='font-size:0.9em;margin-top:6px'>{html.escape(T['upload_new'])}</div></div>",
        unsafe_allow_html=True)
    if st.button(T["upload_new"], type="primary", key="open_upload", use_container_width=True):
        st.session_state["dlg"] = {"step": "upload"}
        st.rerun()                                # the whole page, so the upload window opens
