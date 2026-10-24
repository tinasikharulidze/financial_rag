"""Compliance Copilot: a slim bar on the left that expands into a chat panel.

The panel sends the agent a small context envelope (view, document version, open flag, clause, chips: ids and
labels, never text) and shows short answers with sources. People see readable names (``kg.agent.labels``); the ids
stay in the data for the machine. The agent is read-only in this version (kg/agent/loop.py).
Strings live here (not in i18n_ka.py) so the panel is self-contained.
"""
from __future__ import annotations

import html

import streamlit as st
from i18n_lang import current, localize
from i18n_ka import TASK_STATUS, VERDICT, tr

S = {
    "ka": {
        "title": "Goni", "new": "ახალი საუბარი", "close": "ჩაკეცვა", "open": "Goni-ს გახსნა",
        "input": "დასვით კითხვა რეგულაციაზე ან დოკუმენტზე…", "context": "კონტექსტი", "you": "თქვენ",
        "suggested": "შემოთავაზებული კითხვები", "sources": "წყაროები", "thinking": "ვფიქრობ…",
        "not_backed": "პასუხი არ ემყარება ნაპოვნ წყაროებს.", "no_quote": "ციტატა ვერ დადასტურდა",
        "readonly": "ცვლილებებს მხოლოდ შემოგთავაზებთ: ძალაში შედის თქვენი დადასტურებით.", "history": "ისტორია",
        "a_dismiss_flag": "ნიშნულის დახურვა", "a_mark_covered": "მოთხოვნა უკვე ასახულია", "a_change_mapping": "შესაბამისობის შეცვლა",
        "a_suggest_wording": "შემოთავაზებული ფორმულირების განახლება", "a_new_wording": "ახალი ფორმულირება",
        "a_replaces_edit": "თქვენი ხელით შესწორება ძველ ტექსტზე ჩანაცვლდება.", "a_ungrounded": "ეს რიცხვები არ არის კანონში, დოკუმენტში და საუბარში:",
        "a_review_next": "ტექსტი გადავა განხილვაში, სადაც ეთანხმებით, უარყოფთ ან ასწორებთ.", "a_save_fact": "ფაქტის შენახვა პროფილში", "a_report_extraction": "შეტყობინება არასწორ ამოღებაზე",
        "a_confirm": "დადასტურება", "a_cancel": "გაუქმება", "a_undo": "უკან დაბრუნება", "a_reason": "მიზეზი (ჩაიწერება)",
        "a_pending": "ელოდება დადასტურებას", "a_confirmed": "შესრულდა", "a_cancelled": "გაუქმდა", "a_undone": "დაბრუნდა უკან",
        "a_expired": "ვადა გაუვიდა: დასვით კითხვა თავიდან", "a_stale": "ამასობაში შეიცვალა: დასვით კითხვა თავიდან",
        "a_failed": "ვერ შესრულდა", "a_flag": "ნიშნული", "a_clauses": "პუნქტები", "a_add": "დამატება", "a_remove": "ამოღება",
        "a_recheck": "ამ ვალდებულების შემოწმება თავიდან უნდა გაიმართოს.", "a_fact": "ახალი ფაქტი", "a_replaces": "ანაცვლებს",
        "a_note": "შენიშვნა", "a_nothing": "დადასტურებამდე არაფერი იცვლება.", "history": "ისტორია",
        "e_budget": "ამ მოდელების დღიური ლიმიტი ამოიწურა. სცადეთ მოგვიანებით ან დაამატეთ სხვა მოდელი configs/kg.yaml-ში.",
        "e_down": "მოდელი ახლა არ პასუხობს (პროვაიდერი შეცდომებს აბრუნებს). სცადეთ ხელახლა ერთ წუთში.", "e_rejected": "მოთხოვნა უარყოფილია:",
        "empty": "იკითხეთ ამ დოკუმენტზე, ნიშნულზე ან რეგულაციაზე. Goni ასევე შემოგთავაზებთ ცვლილებებს, რომლებსაც თქვენ დაადასტურებთ.",
        "no_history": "ამ დოკუმენტზე ადრინდელი საუბრები არ არის", "point": "მიუთითეთ ადგილი", "pick_clause": "აირჩიეთ პუნქტი", "add": "დამატება",
        "show_doc": "დოკუმენტში ჩვენება", "ask_followup": "დამატებითი კითხვა", "remove": "ამოღება",
        "proposed": "შემოთავაზებული", "flag": "ნიშნული", "verdict": "შეფასება", "missing": "რა აკლია", "why": "დასაბუთება", "auto": "ავტომატურად",
        "kinds": {"Document": "დოკუმენტი", "Obligation": "ვალდებულება", "Provision": "ნორმა", "Clause": "პუნქტი",
                  "Act": "აქტი", "Amendment": "ცვლილება", "Proposal": "შემოთავაზება", "Other": "სხვა"},
    },
    "en": {
        "title": "Goni", "new": "New chat", "close": "Collapse", "open": "Open Goni",
        "input": "Ask about the regulation or this document…", "context": "Context", "you": "You",
        "suggested": "Suggested questions", "sources": "Sources", "thinking": "Thinking…",
        "not_backed": "This answer is not backed by retrieved sources.", "no_quote": "quote could not be verified",
        "readonly": "It only proposes changes: nothing happens until you confirm.", "history": "History",
        "a_dismiss_flag": "Close the flag", "a_mark_covered": "Obligation already covered", "a_change_mapping": "Change the mapping",
        "a_suggest_wording": "Update the suggested wording", "a_new_wording": "New wording",
        "a_replaces_edit": "Your own edit of the old wording will be replaced.", "a_ungrounded": "These numbers are not in the law, the document or the conversation:",
        "a_review_next": "The wording goes to the review, where you accept, reject or edit it.", "a_save_fact": "Save a fact to the profile", "a_report_extraction": "Report a wrong extraction",
        "a_confirm": "Confirm", "a_cancel": "Cancel", "a_undo": "Undo", "a_reason": "Reason (will be recorded)",
        "a_pending": "waiting for your confirmation", "a_confirmed": "done", "a_cancelled": "cancelled", "a_undone": "undone",
        "a_expired": "expired: ask again", "a_stale": "changed in the meantime: ask again",
        "a_failed": "could not be done", "a_flag": "Flag", "a_clauses": "Clauses", "a_add": "add", "a_remove": "remove",
        "a_recheck": "This obligation needs to be re-checked afterwards.", "a_fact": "New fact", "a_replaces": "Replaces",
        "a_note": "Note", "a_nothing": "Nothing changes until you confirm.",
        "e_budget": "The daily limit of the models is used up. Try again later or add another model in configs/kg.yaml.",
        "e_down": "The model is not responding right now (the provider keeps returning errors). Ask again in a minute.", "e_rejected": "The request was rejected:",
        "empty": "Ask about this document, a flag or the regulation. Goni can also propose changes for you to confirm.",
        "no_history": "No earlier chats about this document", "point": "Point to a place", "pick_clause": "Choose a clause", "add": "Add",
        "show_doc": "Show in document", "ask_followup": "Ask follow-up", "remove": "Remove",
        "proposed": "proposed", "flag": "Flag", "verdict": "Verdict", "missing": "What is missing", "why": "Reasoning", "auto": "automatic",
        "kinds": {"Document": "Document", "Obligation": "Obligation", "Provision": "Provision", "Clause": "Clause",
                  "Act": "Act", "Amendment": "Amendment", "Proposal": "Suggestion", "Other": "Other"},
    },
}

VIEW_OF_PAGE = {"docs": "documents", "review": "analysis", "board": "board"}

CSS = """
<style>
[class*="st-key-cp_fit"], div[data-testid="stElementContainer"]:has(> [class*="st-key-cp_fit"]) {display: none !important;}
/* a proposed change waiting for the person's confirmation */
[class*="st-key-cpc_"] {border: 1px solid rgba(49,51,63,0.22); border-radius: 10px; padding: 0.6rem 0.8rem; margin: 0.4rem 0;
                        background: rgba(255,255,255,0.9); gap: 0.35rem;}
[class*="st-key-cpc_"] textarea {font-size: 0.85rem; min-height: 3.2rem;}
/* no link icon next to the panel title */
div[class*="st-key-cp_top"] [data-testid="stHeaderActionElements"] {display: none;}
/* the slim bar on the left (collapsed state) */
[class*="st-key-cp_rail"] {background: rgba(31,111,178,0.09); border-right: 2px solid rgba(31,111,178,0.4);
                           border-radius: 10px; min-height: 72vh; align-items: center; padding-top: 0.4rem;}
[class*="st-key-cp_rail"]:hover {background: rgba(31,111,178,0.15);}
.cp-vert {writing-mode: vertical-rl; transform: rotate(180deg); letter-spacing: 0.1em; font-size: 0.78rem;
          color: #1f6fb2; font-weight: 600; margin-top: 0.4rem; white-space: nowrap;}
/* the panel: docked to the left and as tall as the screen. The wrapper Streamlit puts around it sticks to the bottom
   of the window while the page scrolls; inside: header and context on top, the messages scroll in the middle, and
   the question box sits at the bottom of the screen */
div[data-testid="stLayoutWrapper"]:has(> div[class*="st-key-cp_panel"]) {position: sticky; top: 3.9rem; bottom: 0.6rem;}
div[class*="st-key-cp_panel"] {border-right: 1px solid rgba(49,51,63,0.12); padding-right: 0.9rem;
                               height: var(--cp-h, calc(100vh - 7.5rem)) !important; flex: 0 0 auto !important;
                               display: flex !important; flex-direction: column; gap: 0.4rem; overflow: hidden;}
div[class*="st-key-cp_top"] {max-height: 42vh; overflow-y: auto;}
div[data-testid="stLayoutWrapper"]:has(> div[class*="st-key-cp_msgs"]) {flex: 1 1 0 !important; min-height: 4rem; overflow: hidden;}
div[class*="st-key-cp_msgs"] {height: 100% !important; overflow-y: auto !important; gap: 0.2rem;}
/* chat bubbles: the user on the right, the assistant on the left */
[class*="st-key-cpu_"] {margin: 0.35rem 0 0.35rem auto; width: fit-content; max-width: 88%; background: #1f6fb2;
                        border-radius: 14px 14px 3px 14px; padding: 0.5rem 0.8rem;}
[class*="st-key-cpu_"] * {color: #fff !important;}
[class*="st-key-cpa_"] {margin: 0.35rem auto 0.35rem 0; width: fit-content; max-width: 94%;
                        background: rgba(127,127,127,0.13); border-radius: 14px 14px 14px 3px; padding: 0.5rem 0.8rem;}
/* the chat box: a quiet border, the blue of the app on focus (no red) */
[data-testid="stChatInput"] > div {border-color: rgba(49,51,63,0.25) !important;}
[data-testid="stChatInput"]:focus-within > div {border-color: #1f6fb2 !important; box-shadow: 0 0 0 1px #1f6fb2 !important;}
.cp-who {font-size: 0.7rem; opacity: 0.75; margin-bottom: 0.1rem;}
/* chips: rectangles with the kind, a readable title and a one-line description */
.cp-chip {display: block; border: 1px solid rgba(31,111,178,0.35); background: rgba(31,111,178,0.07);
          border-radius: 8px; padding: 0.25rem 0.55rem; margin: 0.18rem 0; font-size: 0.82rem; line-height: 1.25;}
.cp-chip .k {display: inline-block; font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.04em;
             color: #1f6fb2; font-weight: 600; margin-right: 0.45rem;}
.cp-chip .t {font-weight: 600;}
.cp-chip .s {display: block; opacity: 0.75; font-size: 0.76rem; margin-top: 0.05rem;}
.cp-chip .a {float: right; font-size: 0.66rem; opacity: 0.6;}
[class*="st-key-cpu_"] .cp-chip {background: rgba(255,255,255,0.15); border-color: rgba(255,255,255,0.4);}
</style>
"""


def _s(key: str):
    return S[current()][key]


def _kind(t: str) -> str:
    return S[current()]["kinds"].get(t, t)


def _chip_html(kind: str, title: str, sub: str = "", auto: bool = False) -> str:
    return ("<span class='cp-chip'>" + (f"<span class='a'>{html.escape(_s('auto'))}</span>" if auto else "")
            + f"<span class='k'>{html.escape(_kind(kind))}</span><span class='t'>{html.escape(localize(title))}</span>"
            + (f"<span class='s'>{html.escape(sub)}</span>" if sub else "") + "</span>")


def build_context(user: dict) -> dict:
    """What the current screen sends: ids and small state only."""
    page = st.session_state.get("page_now")
    ctx: dict = {"view": VIEW_OF_PAGE.get(page, "other"), "chips": list(st.session_state.get("chat_chips", [])),
                 "state": {"page": page}}
    if page == "review":
        ctx["version_id"] = st.session_state.get("review_version")
        prop = st.session_state.get("prop_dlg")
        if prop and "|" not in prop:                      # one open flag; a merged window has several
            ctx["obligation_id"] = prop
    if not ctx.get("obligation_id") and st.session_state.get("chat_flag"):
        ctx["obligation_id"] = st.session_state["chat_flag"]       # set by "Ask follow-up" in the flag popup
        ctx.setdefault("version_id", st.session_state.get("chat_flag_version"))
    if st.session_state.get("chat_clause"):
        ctx["clause_id"] = st.session_state["chat_clause"]
    return ctx


def open_with(store, cfg, version_id: str | None = None, obligation_id: str | None = None):
    """'Ask follow-up' in a flag popup: opens the panel with the flag and its document attached as chips; the flag
    becomes the focus of the conversation (the popup closes, so the page no longer shows it)."""
    from kg.agent.labels import node_label
    acts = cfg.get("acts")
    chips = []
    if obligation_id:
        st.session_state["chat_flag"] = obligation_id
        st.session_state["chat_flag_version"] = version_id
        lab = node_label(store, obligation_id, acts)
        chips.append({"type": "obligation", "id": obligation_id, "label": lab["title"], "sub": lab["sub"], "origin": "popup"})
    have = {c["id"] for c in st.session_state.get("chat_chips", [])}
    st.session_state["chat_chips"] = st.session_state.get("chat_chips", []) + [c for c in chips if c["id"] not in have]
    st.session_state["chat_open"] = True
    st.session_state.pop("chat_thread", None)         # a new topic starts a new thread


def _toggle():
    st.session_state["chat_open"] = not st.session_state.get("chat_open", False)


def rail():
    """The slim bar on the left while the panel is collapsed."""
    st.markdown(CSS, unsafe_allow_html=True)
    with st.container(key="cp_rail"):
        st.button("", icon=":material/chevron_right:", key="chat_toggle", type="tertiary", help=_s("open"), on_click=_toggle)
        st.markdown(f"<div class='cp-vert'>{html.escape(_s('title'))}</div>", unsafe_allow_html=True)


def _current_thread(store, user, ctx: dict) -> dict | None:
    """The open conversation, if it belongs to the document on screen. No thread is created until a question is sent
    (empty conversations would only clutter the history)."""
    from kg.agent import storage
    tid = st.session_state.get("chat_thread")
    t = storage.get_thread(store, user, tid) if tid else None
    if t and ctx.get("version_id") and not storage.same_document(store, t["root_id"], ctx["version_id"]):
        t = None                                   # another document is open: its conversations are in the history
    if not t:
        st.session_state.pop("chat_thread", None)
    return t


def _reset_attachments():
    for k in ("chat_chips", "chat_flag", "chat_flag_version", "chat_clause"):
        st.session_state.pop(k, None)


def _new_thread():
    st.session_state.pop("chat_thread", None)
    _reset_attachments()


def _open_thread(tid: int):
    st.session_state["chat_thread"] = tid
    _reset_attachments()


def _history(store, user, ctx: dict, current: int | None):
    from kg.agent import storage
    rows = storage.threads_for(store, user, ctx.get("version_id"))
    if not rows:
        st.caption(_s("no_history"))
    for r in rows:
        title = " ".join((r["title"] or "").split())
        title = title if len(title) <= 58 else title[:57] + "…"
        st.button(f"{title}  ·  {str(r['created_at'])[:10]}", key=f"chat_h{r['id']}", type="tertiary", use_container_width=True,
                  icon=":material/chat_bubble:" if r["id"] != current else ":material/chat:",
                  on_click=_open_thread, args=(r["id"],))


def _show_in_document(store, c: dict):
    """on_click callback: it runs BEFORE the page's widgets exist, so setting ``rf_pick`` here is allowed."""
    n = store.get_node(c["id"])
    vid = (n or {}).get("props", {}).get("doc")
    if not vid:
        return
    st.session_state["review_version"] = vid
    st.session_state["rf_pick"] = vid
    st.session_state["review_focus"] = {"version_id": vid, "clause_id": c["id"], "title": c["label"]}
    st.session_state["goto"] = "review"


def _context_section(cfg, store, user, ctx: dict):
    """What the agent is looking at: readable chips (the open flag and clause, then the attached ones). The document
    itself is not a chip: the person can see it. "Point to a place" sits on the same line as the title."""
    from kg.agent.labels import flag_card, node_label
    from kg.agent.tools import Scope
    acts = cfg.get("acts")
    chips = st.session_state.get("chat_chips", [])
    chip_ids = {c["id"] for c in chips}
    auto = [i for i in (ctx.get("obligation_id"), ctx.get("clause_id")) if i and i not in chip_ids]
    can_point = bool(ctx.get("version_id")) and Scope(store, user).doc_version_ok(ctx["version_id"])
    shown_ids = set(auto)
    if auto or chips or can_point:
        h, p = st.columns([1, 1.7], vertical_alignment="center")
        h.markdown(f"**{_s('context')}**")
        if can_point:
            with p:
                _clause_picker(store, ctx["version_id"])
    for i in auto:
        lab = node_label(store, i, acts)
        st.markdown(_chip_html(lab["type"], lab["title"], lab["sub"], auto=True), unsafe_allow_html=True)
    for n, c in enumerate(chips):
        if c["id"] in shown_ids or c.get("type") == "version":
            continue
        lab = node_label(store, c["id"], acts)
        a, b = st.columns([8, 1], vertical_alignment="center")
        a.markdown(_chip_html(lab["type"], lab["title"], lab["sub"]), unsafe_allow_html=True)
        if b.button("", icon=":material/close:", key=f"chip_rm{n}", type="tertiary", help=_s("remove")):
            st.session_state["chat_chips"] = [x for j, x in enumerate(chips) if j != n]
            if c["id"] == st.session_state.get("chat_clause"):
                st.session_state.pop("chat_clause", None)
            st.rerun()
    # the flag the popup showed: verdict and reasoning, so the conversation starts from the same facts
    if ctx.get("obligation_id") and ctx.get("version_id"):
        card = flag_card(store, Scope(store, user), ctx["version_id"], ctx["obligation_id"], acts)
        if card and card["verdict"]:
            with st.expander(f"{_s('flag')}: {tr(VERDICT, card['verdict'])}"):
                if card["missing"]:
                    st.markdown(f"**{_s('missing')}.** {card['missing']}")
                if card["rationale"]:
                    st.markdown(f"**{_s('why')}.** {card['rationale']}")


def _clause_picker(store, version_id: str):
    """A button next to the title; it opens a small window to choose a place in the open document: a clause, or a NEW
    section that is only proposed so far (listed right after the clause it would follow)."""
    rows = sorted((it["node"] for it in store.neighbors(version_id, "HAS_CLAUSE")),
                  key=lambda n: n["props"].get("order", 0))
    rows = [n for n in rows if n["props"].get("unit_type") not in ("heading", "signature") and n["props"].get("text")]
    new = {}
    for r in store.q("SELECT p.id, p.clause_id, p.heading, p.proposed_text FROM proposals p JOIN tasks t ON "
                     "t.doc_version_id=p.doc_version_id AND t.obligation_id=p.obligation_id WHERE p.doc_version_id=? "
                     "AND p.kind='add_section' AND t.status IN ('open','accepted') ORDER BY p.id", (version_id,)):
        new.setdefault(r["clause_id"], []).append(r)
    options, names = [], {}
    for n in rows:
        options.append(n["id"])
        names[n["id"]] = f"{n['props'].get('section_no') or ''} {n['props']['text'][:70]}".strip()
        for r in new.pop(n["id"], []):
            options.append(f"prop:{r['id']}")
            names[f"prop:{r['id']}"] = f"＋ {_s('proposed')}: " + " ".join((r["heading"] or r["proposed_text"] or "")[:64].split())
    for rest in new.values():                                   # a proposal whose anchor is not a listed clause
        for r in rest:
            options.append(f"prop:{r['id']}")
            names[f"prop:{r['id']}"] = f"＋ {_s('proposed')}: " + " ".join((r["heading"] or r["proposed_text"] or "")[:64].split())
    if not options:
        return
    with st.popover(_s("point"), icon=":material/pin_drop:", use_container_width=True):
        pick = st.selectbox(_s("pick_clause"), options, key="chat_clause_pick", label_visibility="collapsed",
                            format_func=lambda i: names.get(i, i))
        if st.button(_s("add"), key="chat_clause_add"):
            from kg.agent.labels import node_label
            lab = node_label(store, pick)
            if not pick.startswith("prop:"):                     # a clause becomes the focus; a proposed section is a chip
                st.session_state["chat_clause"] = pick
            st.session_state["chat_chips"] = [c for c in st.session_state.get("chat_chips", []) if c["id"] != pick] + [
                {"type": "proposal" if pick.startswith("prop:") else "clause", "id": pick, "label": lab["title"],
                 "sub": lab["sub"], "origin": "user"}]
            st.rerun()


def _bubble(role: str, key: str, body, who: str):
    with st.container(key=f"cp{'u' if role == 'user' else 'a'}_{key}"):
        st.markdown(f"<div class='cp-who'>{html.escape(who)}</div>", unsafe_allow_html=True)
        if callable(body):
            body()
        else:
            st.markdown(body if role != "user" else html.escape(body).replace("\n", "  \n"))


def _citations(store, mid: str, cites: list[dict]):
    """Source cards only where they add something the answer does not say: a verified quote, or a clause of our own
    document (with "Show in document"). A bare list of the obligations the answer already names would repeat it."""
    shown = [(i, c) for i, c in enumerate(cites) if c.get("quote") or c.get("type") == "Clause"]
    if not shown:
        return
    st.markdown(f"<div class='cp-who'>{html.escape(_s('sources'))}</div>", unsafe_allow_html=True)
    for i, c in shown:
        st.markdown(_chip_html(c.get("type") or "Other", c["label"], c.get("sub") or ""), unsafe_allow_html=True)
        if c.get("quote"):
            st.caption("“" + c["quote"] + "”")
        elif c.get("quote_ok") is False:
            st.caption(_s("no_quote"))
        if c.get("type") == "Clause":
            st.button(_s("show_doc"), key=f"{mid}_show{i}", type="tertiary", icon=":material/open_in_new:",
                      on_click=_show_in_document, args=(store, c))


def _name(store, cfg, node_id: str) -> str:
    from kg.agent.labels import node_label
    lab = node_label(store, node_id, cfg.get("acts"))
    return localize(lab["title"]) + (f" — {lab['sub']}" if lab.get("sub") else "")


def _act_confirm(cfg, store, user, aid: int):
    """on_click: the person's click is the ONLY thing that commits a proposal."""
    from kg.agent import actions
    try:
        actions.confirm(cfg, store, user, aid, st.session_state.get(f"act_reason_{aid}"))
        st.session_state.pop(f"act_err_{aid}", None)
    except (actions.ActionError, PermissionError, ValueError) as exc:
        st.session_state[f"act_err_{aid}"] = str(exc)


def _act_cancel(store, user, aid: int):
    from kg.agent import actions
    actions.cancel(store, user, aid)


def _act_undo(cfg, store, user, aid: int):
    from kg.agent import actions
    try:
        actions.undo(cfg, store, user, aid)
        st.session_state.pop(f"act_err_{aid}", None)
    except (actions.ActionError, PermissionError, ValueError) as exc:
        st.session_state[f"act_err_{aid}"] = str(exc)


def _action_lines(cfg, store, r: dict) -> list[str]:
    """What exactly changes, before and after, in words a person reads."""
    b, a, k, p = r["before"], r["after"], r["kind"], r["params"]
    out = []
    if k in ("dismiss_flag", "mark_covered", "change_mapping"):
        out.append(f"**{_s('a_flag')}:** {_name(store, cfg, r['obligation_id'])}")
    if k in ("dismiss_flag", "mark_covered"):
        out.append(f"{tr(TASK_STATUS, b.get('status'))} → **{tr(TASK_STATUS, a['status'])}**")
    if k == "mark_covered":
        out.append(f"**{_s('a_clauses')}:** " + "; ".join(_name(store, cfg, c) for c in p["clause_ids"]))
    if k == "change_mapping":
        for ch in p["changes"]:
            was = tr(VERDICT, (b.get("clauses") or {}).get(ch["clause_id"]), "—")
            now = _s("a_remove") if ch["op"] == "remove" else tr(VERDICT, ch["role"])
            out.append(f"{_name(store, cfg, ch['clause_id'])}: {was} → **{now}**")
        out.append(f"*{_s('a_recheck')}*")
    if k == "save_fact":
        out.append(f"**{_s('a_fact')}:** {a['fact']}")
        if b.get("fact"):
            out.append(f"{_s('a_replaces')}: {b['fact']}")
    if k == "report_extraction":
        out.append(f"**{_s('a_flag')}:** {_name(store, cfg, r['obligation_id'] or p['obligation_id'])}")
    if k == "suggest_wording":
        out.append(f"**{_s('a_flag')}:** {_name(store, cfg, r['obligation_id'])}")
        if a.get("heading"):
            out.append(f"**{html.escape(a['heading'])}**")
    return out


def _wording_diff(r: dict) -> str:
    """The new wording with the words that changed marked (green = new, struck red = removed)."""
    from kg.services.edits import diff_segments
    parts = []
    for kind, text, *_ in diff_segments(r["before"].get("text") or "", r["after"].get("text") or ""):
        t = html.escape(text)
        parts.append(f"<span style='background:#e3f4e8'>{t}</span>" if kind == "ins" else
                     f"<s style='color:#b03a2e;background:#fbeaea'>{t}</s>" if kind == "del" else t)
    return "<div style='font-size:0.88rem;line-height:1.5'>" + "".join(parts) + "</div>"


def _action_cards(cfg, store, user, ids: list):
    """The proposals of one answer: exactly what changes (before -> after), the reason, one click to commit, an undo."""
    from kg.agent import actions
    for aid in ids:
        r = actions.view(store, user, aid)
        if not r:
            continue
        with st.container(key=f"cpc_{aid}"):
            state = r["status"]
            st.markdown(f"**{_s('a_' + r['kind'])}** · _{_s('a_' + state)}_")
            for line in _action_lines(cfg, store, r):
                st.markdown(line)
            if r["kind"] == "suggest_wording":
                st.markdown(f"**{_s('a_new_wording')}**")
                st.markdown(_wording_diff(r), unsafe_allow_html=True)
                if r["after"].get("ungrounded_numbers"):
                    st.warning(f"{_s('a_ungrounded')} {', '.join(r['after']['ungrounded_numbers'])}")
                if r["before"].get("replaces_edit") and state == "pending":
                    st.caption(_s("a_replaces_edit"))
            if state == "pending":
                st.text_area(_s("a_reason"), value=r["reason"] or "", key=f"act_reason_{aid}", height=80)
                c1, c2 = st.columns(2)
                c1.button(_s("a_confirm"), key=f"act_ok_{aid}", type="primary", use_container_width=True,
                          on_click=_act_confirm, args=(cfg, store, user, aid))
                c2.button(_s("a_cancel"), key=f"act_no_{aid}", use_container_width=True,
                          on_click=_act_cancel, args=(store, user, aid))
                st.caption(_s("a_nothing"))
            else:
                if r["reason"]:
                    st.caption(f"{_s('a_note')}: {r['reason']}")
                if state == "confirmed" and r["kind"] == "suggest_wording" and r["params"].get("clause_id"):
                    st.caption(_s("a_review_next"))
                    st.button(_s("show_doc"), key=f"act_show_{aid}", icon=":material/pin_drop:", on_click=_show_in_document,
                              args=(store, {"id": r["params"]["clause_id"], "label": _name(store, cfg, r["params"]["clause_id"])}))
                if state == "confirmed":
                    st.button(_s("a_undo"), key=f"act_undo_{aid}", icon=":material/undo:",
                              on_click=_act_undo, args=(cfg, store, user, aid))
                elif state == "failed" and r.get("error"):
                    st.caption(r["error"])
            if st.session_state.get(f"act_err_{aid}"):
                st.error(st.session_state[f"act_err_{aid}"])


def _assistant_body(cfg, store, user, m: dict, last: bool):
    def body():
        st.markdown(m["content"])
        tr_ = m.get("trace") or {}
        _citations(store, f"m{m['id']}", tr_.get("citations") or [])
        _action_cards(cfg, store, user, tr_.get("actions") or [])
        if tr_.get("kind") == "answer" and not tr_.get("citations") and tr_.get("tools"):
            st.caption(_s("not_backed"))
        if last and tr_.get("kind") == "ask" and tr_.get("options"):
            for i, o in enumerate(tr_["options"]):
                if st.button(o, key=f"m{m['id']}_opt{i}"):
                    st.session_state["chat_pending"] = o
                    st.rerun()
    return body


def _run(cfg, store, user, ctx: dict, text: str, hist: list[dict], thread: dict):
    """One agent turn. Returns (reply, None) or (None, a readable reason why there is no answer)."""
    from common import make_llm
    from kg.agent import Cursor, run_turn
    from kg.llm import BudgetExhausted, LLMRequestRejected, LLMUnavailable
    from kg.runlog import Run
    try:
        with Run(store, cfg.path("runs"), "agent", {"question": text[:200]}) as run:
            return run_turn(cfg, store, make_llm(cfg, store, run.id), user, ctx, text, hist,
                            Cursor.from_dict(thread["cursor"]), lang=current()), None
    except BudgetExhausted:
        return None, _s("e_budget")
    except LLMUnavailable:
        return None, _s("e_down")
    except LLMRequestRejected as exc:
        return None, f"{_s('e_rejected')} {exc}"


def _send(cfg, store, user, thread: dict | None, text: str, ctx: dict) -> int:
    from kg.agent import storage
    if thread is None:                              # the conversation starts with the first question
        tid = storage.new_thread(store, user, ctx.get("version_id"), text)
        st.session_state["chat_thread"] = tid
        thread = storage.get_thread(store, user, tid)
    else:
        tid = thread["id"]
        storage.set_title(store, tid, text)
    hist = [{"role": m["role"], "content": m["content"]} for m in storage.messages(store, user, tid)
            if m["role"] in ("user", "assistant")]
    uid = storage.add_message(store, tid, "user", text, ctx)
    with st.spinner(_s("thinking")):
        reply, problem = _run(cfg, store, user, ctx, text, hist, thread)
    if reply is None:                               # keep the failure IN the conversation: a st.error is gone on the rerun
        storage.add_message(store, tid, "assistant", problem, None, {"kind": "error"})
        return tid
    mid = storage.add_message(store, tid, "assistant", reply.text, None, {
        "kind": reply.kind, "citations": reply.citations, "dropped": reply.dropped, "options": reply.options,
        "steps": reply.steps, "tools": [x["tool"] for x in reply.trace], "notes": reply.notes, "trace": reply.trace,
        "actions": reply.actions})
    if reply.actions:                               # the card links back to the question and its context snapshot
        from kg.agent import actions
        actions.attach(store, reply.actions, tid, uid, mid, ctx)
    storage.save_cursor(store, tid, reply.cursor)
    return tid


FIT_JS = """
export default function() {
  /* the panel must end at the bottom of the window wherever it starts on the page: height = window - its top */
  if (window.__cpFit) return;
  const fit = () => {
    const p = document.querySelector('[class*="st-key-cp_panel"]');
    if (!p) return;
    const w = p.closest('[data-testid="stLayoutWrapper"]') || p;
    const top = Math.max(w.getBoundingClientRect().top, 62);
    document.documentElement.style.setProperty('--cp-h', Math.max(320, window.innerHeight - top - 12) + 'px');
    const m = p.querySelector('[class*="st-key-cp_msgs"]');      // new content (an answer, a card): show its end
    if (m && m.scrollHeight !== window.__cpH) { window.__cpH = m.scrollHeight; m.scrollTop = m.scrollHeight; }
  };
  window.__cpFit = setInterval(fit, 250);
  window.addEventListener('resize', fit); window.addEventListener('scroll', fit, true);
  fit();
}
"""


def _fit_panel():
    try:
        st.components.v2.component("goni_panel_fit", js=FIT_JS)(key="cp_fit", on_x_change=lambda: None)
    except Exception:  # noqa: BLE001 - an older Streamlit: the CSS fallback height is used
        pass


def render(cfg, store, user):
    from kg.agent import storage
    st.markdown(CSS, unsafe_allow_html=True)
    ctx = build_context(user)
    with st.container(key="cp_panel"):
        thread = _current_thread(store, user, ctx)
        tid = thread["id"] if thread else None
        with st.container(key="cp_top"):
            head, close = st.columns([5, 1], vertical_alignment="center", gap="small")
            head.markdown(f"#### {_s('title')}")
            close.button("", icon=":material/chevron_left:", key="chat_close", type="tertiary", help=_s("close"),
                         on_click=_toggle)
            hist, new = st.columns(2, gap="small")              # side by side, the same size
            with hist:
                with st.popover(_s("history"), icon=":material/history:", use_container_width=True):
                    _history(store, user, ctx, tid)
            new.button(_s("new"), key="chat_new", icon=":material/add:", on_click=_new_thread, use_container_width=True)
            st.caption(_s("readonly"))
            _context_section(cfg, store, user, ctx)

        box = st.container(key="cp_msgs")
        typed = st.chat_input(_s("input"), key="chat_input")
        pending = st.session_state.pop("chat_pending", None)
        text = (typed or pending or "").strip()
        msgs = storage.messages(store, user, tid) if tid else []

        with box:
            if not msgs and not text:              # an empty container is not drawn: keep the space (and say what to ask)
                st.caption(_s("empty"))
            for i, m in enumerate(msgs):
                if m["role"] == "user":
                    _bubble("user", f"m{m['id']}", m["content"], _s("you"))
                else:
                    _bubble("assistant", f"m{m['id']}", _assistant_body(cfg, store, user, m, i == len(msgs) - 1), _s("title"))
            if text:
                _bubble("user", "new", text, _s("you"))
                _send(cfg, store, user, thread, text, ctx)
        _fit_panel()
        if text:
            st.rerun()
