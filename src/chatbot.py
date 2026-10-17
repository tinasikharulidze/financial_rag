#!/usr/bin/env python3
"""
Streamlit chatbot on top of the Matsne RAG logic in generate.py.

Files (all in src/):  chatbot.py (this file), chat_db.py, generate.py
Theme file (project root, next to src/):  .streamlit/config.toml   (see streamlit_config.toml)

    export GEMINI_API_KEY=...          # or whatever key your LiteLLM model needs
    uv run streamlit run src/chatbot.py

Optional environment variables (defaults in CONFIG below):
    CHAT_MODEL, REWRITE_MODEL, CHILDREN_PATH, PARENTS_PATH, DOCS_PATH, OUT_DIR, DB_PATH

Features: sign-up / login (any password; stored hashed), conversations saved per user and titled by the date
of their first question, reopen-and-continue (5 questions per chat), one source list per answer (grouped by
document), thumbs up/down, full RAG logging in SQLite.
"""

import os
import sys
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chat_db import ChatDB  # noqa: E402
from generate import (  # noqa: E402
    REFUSAL,
    Cache,
    Corpus,
    build_messages,
    call_llm,
    parse_llm_response,
)

# ============================================================
# CONFIG
# ============================================================

TITLE = "მიიღე პასუხი ნებისმიერ კითხვაზე ქართულ ფინანსურ რეგულაციებთან დაკავშირებით"
GREETING = "გამარჯობა, მე გონი მქვია და შემიძლია ინფორმაცია მოგაწოდო ქართული ფინანსური რეგულაციების შესახებ. რა გაინტერესებს?"
CLOSING = "ამ დროისთვის მეტ კითხვაზე პასუხის გაცემა არ შემიძლია. გთხოვ გახსნა ახალი ჩათი."
ERROR_MSG = "პასუხის გენერირებისას რაღაც შეცდომა მოხდა. გთხოვ, სცადე ხელახლა."

# Shown ONCE per answer, followed by a list of documents (singular / plural form).
SOURCES_INTRO_ONE  = "\n \n ეს ამოვიკითხე შემდეგი რეგულაციიდან:"
SOURCES_INTRO_MANY = "\n \n ეს ამოვიკითხე შემდეგი რეგულაციებიდან:"
LINK_TEXT = "იხილე აქ"

MAX_QUESTIONS = 5

CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini/gemini-3.6-flash")
REWRITE_MODEL = os.getenv("REWRITE_MODEL", CHAT_MODEL)
EMBED_MODEL = "BAAI/bge-m3"

CHILDREN_PATH = os.getenv("CHILDREN_PATH", "matsne_data/embeddings/children_bge_m3.jsonl")
PARENTS_PATH = os.getenv("PARENTS_PATH", "matsne_data/chunks/parents.jsonl")
DOCS_PATH = os.getenv("DOCS_PATH", "matsne_data/chunks/docs.jsonl")
OUT_DIR = Path(os.getenv("OUT_DIR", "matsne_data/generation"))
DB_PATH = Path(os.getenv("DB_PATH", "DB/app.db"))

# Retrieval settings: same defaults as generate.py
BUDGET_TOKENS = 20000
CPT = 1.8
CAP_CHARS = 6000
PER_DOC = 10
MAX_TOKENS = 5000
RPM = 30

HISTORY_TURNS_FOR_REWRITE = 3
ANSWER_CHARS_FOR_REWRITE = 600

# Palette (sampled from your image, left to right)
PAL_PINK = "#e6f0fa"      # sidebar
PAL_PAGE = "#FAFBFE"      # page background
PAL_BOT = "#e6e6fa"       # chatbot bubble
PAL_ACCENT = "#d1e8fc"    # buttons / accents
PAL_USER = "#ccccff"      # user bubble
TEXT = "#2b2a4c"          # dark text: all five colours are pastel
ACTIVE_CONV = "#B8D9F8"
BORDER = "#A2D2FF"
HOVER = "#B8D9F8"

USER_AVATAR = None
BOT_AVATAR = None      

REWRITE_PROMPT = """You rewrite follow-up questions in a conversation about Georgian financial and administrative regulations.

Given the chat history and the user's NEW message, produce ONE standalone question in Georgian that contains all the context needed to be understood without the history (names of laws, entities, topics, dates mentioned earlier).

Rules:
- If the new message is already a standalone question, return it unchanged.
- Do not answer the question. Do not add information that is not in the history or the new message.
- Return ONLY the rewritten question, with no quotes, labels or explanations."""


# ============================================================
# RESOURCES (loaded once)
# ============================================================

@st.cache_resource(show_spinner="ვტვირთავ მონაცემებს...")
def load_corpus():
    docs = DOCS_PATH if Path(DOCS_PATH).exists() else None
    return Corpus(CHILDREN_PATH, PARENTS_PATH, docs)


@st.cache_resource(show_spinner="ვტვირთავ ემბედინგ მოდელს...")
def load_embedder():
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    return SentenceTransformer(EMBED_MODEL, device=device)


@st.cache_resource
def load_cache():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return Cache(OUT_DIR / "chat_cache.jsonl")


@st.cache_resource
def get_db():
    return ChatDB(DB_PATH)


# ============================================================
# RAG STEPS
# ============================================================

def rewrite_question(history, new_q, cache):
    """history: list of (question, answer). Returns a standalone question."""
    if not history:
        return new_q

    lines = []
    for q, a in history[-HISTORY_TURNS_FOR_REWRITE:]:
        lines.append(f"User: {q}")
        lines.append(f"Assistant: {a[:ANSWER_CHARS_FOR_REWRITE]}")
    user = "Chat history:\n" + "\n".join(lines) + f"\n\nNEW message: {new_q}"

    out = call_llm(
        REWRITE_MODEL,
        [
            {"role": "system", "content": REWRITE_PROMPT},
            {"role": "user", "content": user},
        ],
        2000,  # generous: thinking models spend output tokens on reasoning
        RPM,
        cache,
    )

    if "error" in out:
        return new_q

    text = (out.get("text") or "").strip().strip('"„“«»').strip()
    return text or new_q


def embed_question(text):
    vec = load_embedder().encode(
        [text],
        normalize_embeddings=True,
        convert_to_tensor=False,
    )[0]
    return vec


def is_refusal(answer):
    a = (answer or "").strip()
    if not a:
        return True
    return (
        a == REFUSAL
        or "{REFUSAL}" in a
        or a.startswith("ამ კითხვაზე ჩემს ხელთ არსებული ინფორმაციიდან პასუხის მოძიება ვერ შევძელი")
    )


def source_info(corpus, pid):
    """For a parent id: its document (id, name, url) and the article/annex part within it."""
    p = corpus.parents.get(pid, {})
    doc_id = str(p.get("doc_id", pid.split(":")[0]))
    d = corpus.docs.get(doc_id, {})

    doc_type = d.get("type") or ""
    number = d.get("number") or ""
    title = d.get("title") or d.get("title_fallback") or ""

    name = f"{doc_type} №{number}".strip() if number else doc_type
    if title:
        name = f"{name}, „{title}“" if name else f"„{title}“"
    if not name:
        # fallback: first two breadcrumb parts
        name = " › ".join((p.get("breadcrumb") or pid).split(" › ")[:2])

    parts = []
    if p.get("annex"):
        parts.append(f"დანართი {p['annex']}")
    if p.get("article_no"):
        art = f"მუხლი {p['article_no']}"
        if p.get("article_title"):
            art += f". {p['article_title']}"
        parts.append(art)
    elif p.get("kind") == "preamble":
        parts.append("პრეამბულა")
    elif p.get("table_part"):
        parts.append(p["table_part"])

    url = d.get("url") or f"https://matsne.gov.ge/ka/document/view/{doc_id}"
    return {"doc_id": doc_id, "name": name, "part": ", ".join(parts), "url": url}


def group_sources(corpus, source_ids):
    """One entry per DOCUMENT (in the order the LLM cited them); articles from the same document are merged."""
    docs = {}
    for pid in source_ids:
        info = source_info(corpus, pid)
        d = docs.setdefault(
            info["doc_id"],
            {"doc_id": info["doc_id"], "name": info["name"], "url": info["url"], "parts": []},
        )
        if info["part"] and info["part"] not in d["parts"]:
            d["parts"].append(info["part"])
    return list(docs.values())


def answer_question(user_q, history):
    """Full pipeline: rewrite -> embed -> retrieve -> generate.
    Returns dict(answer, sources[doc dicts], rewritten, retrieved, retrieved_docs, cited_parents,
    cited_docs, latency, error)."""
    t0 = time.perf_counter()
    corpus = load_corpus()
    cache = load_cache()

    rewritten = rewrite_question(history, user_q, cache)

    qvec = embed_question(rewritten)
    passages, _ = corpus.assemble(
        corpus.rank(qvec),
        BUDGET_TOKENS,
        CAP_CHARS,
        PER_DOC,
        CPT,
    )

    retrieved = [p["parent_id"] for p in passages]
    retrieved_docs = list(dict.fromkeys(str(p["doc_id"]) for p in passages))
    base = {"rewritten": rewritten, "retrieved": retrieved, "retrieved_docs": retrieved_docs,
            "cited_parents": [], "cited_docs": [], "sources": []}

    out = call_llm(
        CHAT_MODEL,
        build_messages(rewritten, passages),
        MAX_TOKENS,
        RPM,
        cache,
    )
    latency = time.perf_counter() - t0

    if "error" in out:
        return {**base, "error": out["error"], "latency": latency}

    valid_ids = set(retrieved)
    answer, source_ids = parse_llm_response(out.get("text", ""), valid_ids)

    if is_refusal(answer):
        return {**base, "answer": REFUSAL, "error": None, "latency": latency}

    sources = group_sources(corpus, source_ids)
    return {
        **base,
        "answer": answer,
        "sources": sources,
        "cited_parents": source_ids,
        "cited_docs": [s["doc_id"] for s in sources],
        "error": None,
        "latency": latency,
    }


# ============================================================
# STYLE
# ============================================================
def inject_css():
    st.markdown(
        f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Georgian:wght@400;600;700&display=swap');

html, body, .stApp, .stMarkdown, .stMarkdown p, .stMarkdown li, label p,
.stTextInput input, [data-testid="stChatInput"] textarea,
[data-testid="stSidebar"] button p, .stTabs button p {{
    font-family: 'Noto Sans Georgian', 'Segoe UI', sans-serif !important;
}}

.stApp {{
    background: {PAL_PAGE};
    color: {TEXT};
}}

.block-container {{
    max-width: 1150px;
    padding-top: 5.5rem;   /* header is ~3.75rem, plus breathing room */
    padding-bottom: 6rem;
}}

/* Chat input: same width as the conversation column */
[data-testid="stBottomBlockContainer"] {{
    max-width: 1150px !important;
    margin-left: auto !important;
    margin-right: auto !important;
    box-sizing: border-box !important;
}}

/* Title in the top header bar */
[data-testid="stHeader"] {{
    background: {PAL_PAGE};
    border-bottom: 1px solid {BORDER};
}}

[data-testid="stHeader"]::before {{
    content: "{TITLE}";
    position: absolute;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    max-width: calc(100% - 14rem);   /* leave room for the sidebar toggle and menu */
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    font-family: 'Noto Sans Georgian', 'Segoe UI', sans-serif;
    font-size: clamp(0.95rem, 1.6vw, 1.3rem);
    font-weight: 700;
    color: {TEXT};
    pointer-events: none;            /* don't block the header buttons */
}}

/* Sidebar */
[data-testid="stSidebar"] {{
    background: {PAL_PINK};
}}

/* Conversation list: scrolls on its own when there are many chats */
.st-key-conv_list {{
    max-height: calc(100vh - 19rem);   /* larger number = shorter list */
    overflow-y: auto;
    overflow-x: hidden;
    padding-right: .25rem;
}}

/* Logout: pinned to the bottom of the sidebar, level with the chat input */
.st-key-logout_section {{
    position: fixed;
    bottom: 1.4rem;
    left: 1rem;
    width: calc(17rem - 2rem);         /* sidebar width minus left and right margins */
    box-sizing: border-box;
    background: {PAL_PINK};
    border-top: 1px solid {BORDER};
    padding: .75rem 0 0 0;
    z-index: 10;
}}

.st-key-logout_section .stButton,
.st-key-logout_section button {{
    width: 100% !important;
    box-sizing: border-box;
}}

/* "⋯" menu button: compact */
[data-testid="stSidebar"] [data-testid="stPopover"] button {{
    padding: .25rem 0;
    min-height: 2.2rem;
}}

/* Chat message rows */
[data-testid="stChatMessage"] {{
    display: flex !important;
    gap: 0 !important;
    background: transparent !important;
    border-radius: 18px;
    padding: 0.4rem 0 !important;
    margin-bottom: 0.9rem;
    width: 100%;
    align-items: center !important;
}}

/* Remove avatar icons and their space */
[data-testid="stChatMessage"] > div:first-child {{
    display: none !important;
}}

/* Source lists: indent bullets, tidy nested ones */
[data-testid="stChatMessage"] ul {{
    margin: 0 !important;
    padding-left: 2rem !important;      /* roughly one tab */
}}

[data-testid="stChatMessage"] ul ul {{
    margin-top: .3rem !important;
    padding-left: 1.5rem !important;    /* sub-bullets indented a bit further */
}}

/* Message bubble container: content is centered vertically */
[data-testid="stChatMessage"] > div:last-child {{
    flex: 0 1 auto !important;
    width: fit-content !important;
    max-width: 85% !important;
    min-width: 0 !important;
    box-sizing: border-box !important;
    padding: 0.8rem 1.4rem !important;
    border-radius: 18px;
    overflow-wrap: anywhere;
    display: flex !important;
    flex-direction: column !important;
    justify-content: center !important;
    align-items: stretch !important;
}}

/* Inner block Streamlit puts inside the bubble: no stray gap, centered */
[data-testid="stChatMessage"] [data-testid="stVerticalBlock"] {{
    display: flex !important;
    flex-direction: column !important;
    justify-content: center !important;
    gap: 0.5rem !important;
    margin: 0 !important;
    padding: 0 !important;
}}

/* Hide the invisible marker's wrapper so it no longer pushes the text down.
   (The :has() rules below still work because the span stays in the DOM.) */
[data-testid="stChatMessage"] [data-testid="stElementContainer"]:has(.msg-user),
[data-testid="stChatMessage"] [data-testid="stElementContainer"]:has(.msg-bot) {{
    display: none !important;
}}

/* Remaining element containers / markdown: no extra margins or padding */
[data-testid="stChatMessage"] [data-testid="stElementContainer"],
[data-testid="stChatMessage"] [data-testid="stMarkdownContainer"],
[data-testid="stChatMessage"] .stMarkdown {{
    margin: 0 !important;
    padding: 0 !important;
}}

/* User messages: right */
[data-testid="stChatMessage"]:has(.msg-user) {{
    justify-content: flex-end !important;
}}

[data-testid="stChatMessage"]:has(.msg-user) > div:last-child {{
    background: {PAL_USER} !important;
    margin-left: auto !important;
    margin-right: 0 !important;
}}

/* Assistant messages: left */
[data-testid="stChatMessage"]:has(.msg-bot) {{
    justify-content: flex-start !important;
}}

[data-testid="stChatMessage"]:has(.msg-bot) > div:last-child {{
    background: {PAL_BOT} !important;
    margin-left: 0 !important;
    margin-right: auto !important;
}}

/* Text inside bubbles */
[data-testid="stChatMessage"] > div:last-child p,
[data-testid="stChatMessage"] > div:last-child li {{
    font-size: 1.15rem;
    line-height: 1.5;
    color: {TEXT};
    margin: 0 !important;
    padding: 0 !important;
}}

[data-testid="stChatMessage"] [data-testid="stCaptionContainer"] p {{
    font-size: .95rem;
    opacity: .8;
}}

[data-testid="stChatMessage"] a {{
    color: #4a3fb5;
}}

/* Chat input */
[data-testid="stChatInput"] textarea {{
    font-size: 1.1rem;
    color: {TEXT};
}}

/* Buttons */
.stButton button,
.stFormSubmitButton button {{
    border-radius: 12px;
    border: 1px solid {BORDER};
    background: {PAL_ACCENT};
    color: {TEXT};
}}

.stButton button[kind="primary"],
.stButton button[data-testid="stBaseButton-primary"] {{
    background: {ACTIVE_CONV};
    border: 1px solid {BORDER};
    color: {TEXT};
    font-weight: 600;
}}

/* Button hover / focus / active: no default red */
.stButton button:hover,
.stButton button:focus,
.stButton button:focus:not(:active),
.stButton button:active,
.stButton button[kind="primary"]:hover,
.stButton button[kind="primary"]:focus,
.stButton button[kind="primary"]:active,
.stFormSubmitButton button:hover,
.stFormSubmitButton button:focus,
.stFormSubmitButton button:focus:not(:active),
.stFormSubmitButton button:active {{
    background: {HOVER} !important;
    border-color: {BORDER} !important;
    color: {TEXT} !important;
    box-shadow: none !important;
    outline: none !important;
}}

.stButton button:hover p,
.stButton button:focus p,
.stButton button:active p,
.stFormSubmitButton button:hover p,
.stFormSubmitButton button:focus p,
.stFormSubmitButton button:active p {{
    color: {TEXT} !important;
}}

/* Tabs */
.stTabs [data-baseweb="tab-highlight"] {{
    background-color: {BORDER} !important;
}}
.stTabs [data-baseweb="tab"]:hover,
.stTabs [data-baseweb="tab"][aria-selected="true"],
.stTabs [data-baseweb="tab"]:hover p,
.stTabs [data-baseweb="tab"][aria-selected="true"] p {{
    color: {TEXT} !important;
}}

/* Text input and chat input focus */
.stTextInput [data-baseweb="input"]:focus-within,
.stTextInput [data-baseweb="base-input"]:focus-within,
[data-testid="stChatInput"] > div:focus-within {{
    border-color: {BORDER} !important;
    box-shadow: 0 0 0 1px {BORDER} !important;
}}
</style>
""",
        unsafe_allow_html=True,
    )

# ============================================================
# SESSION STATE HELPERS
# ============================================================

def reset_chat():
    st.session_state.conv_id = None
    st.session_state.messages = [{"role": "assistant", "content": GREETING, "sources": []}]
    st.session_state.history = []  # (question, answer) pairs used for rewriting
    st.session_state.n_questions = 0


def rows_to_messages(rows):
    """DB rows -> (UI messages, history pairs)."""
    msgs, history, pending_q = [], [], None
    for r in rows:
        if r["role"] == "user":
            pending_q = r["content"]
            msgs.append({"role": "user", "content": r["content"], "sources": [], "id": r["id"]})
        else:
            msgs.append(
                {
                    "role": "assistant",
                    "content": r["content"],
                    "sources": r["sources"],
                    "rewritten": r["rewritten"] if r["rewritten"] != pending_q else None,
                    "id": r["id"],
                    "feedback": r["feedback"],
                    "error": r["error"],
                }
            )
            if not r["error"] and pending_q is not None:
                history.append((pending_q, r["content"]))
            pending_q = None
    return msgs, history


def open_conversation(db, user_id, conv_id):
    conv = db.get_conversation(conv_id, user_id)  # ownership-checked
    if not conv:
        return
    msgs, history = rows_to_messages(db.load_messages(conv_id))
    messages = [{"role": "assistant", "content": GREETING, "sources": []}] + msgs
    if conv["n_questions"] >= MAX_QUESTIONS:
        messages.append({"role": "assistant", "content": CLOSING, "sources": []})
    st.session_state.conv_id = conv_id
    st.session_state.messages = messages
    st.session_state.history = history
    st.session_state.n_questions = conv["n_questions"]


# ============================================================
# UI PIECES
# ============================================================

def render_auth(db):
    # st.title(TITLE)
    tab_in, tab_up = st.tabs(["შესვლა", "რეგისტრაცია"])

    with tab_in:
        with st.form("login_form"):
            u = st.text_input("მომხმარებლის სახელი", key="login_user")
            p = st.text_input("პაროლი", type="password", key="login_pass")
            ok = st.form_submit_button("შესვლა")
        if ok:
            user = db.verify_user(u, p)
            if user:
                st.session_state.user = user
                reset_chat()
                st.rerun()
            else:
                st.error("მომხმარებლის სახელი ან პაროლი არასწორია.")

    with tab_up:
        with st.form("signup_form"):
            u = st.text_input("მომხმარებლის სახელი", key="signup_user")
            p = st.text_input("პაროლი", type="password", key="signup_pass")
            ok = st.form_submit_button("რეგისტრაცია")
        if ok:
            if not (u or "").strip() or not p:
                st.error("შეავსე ორივე ველი.")
            else:
                user = db.create_user(u, p)
                if user:
                    st.session_state.user = user
                    reset_chat()
                    st.rerun()
                else:
                    st.error("ეს მომხმარებლის სახელი დაკავებულია.")


def render_sidebar(db, user):
    with st.sidebar:
        # Top section
        st.markdown(f"**{user['username']}**")
        if st.button("ახალი ჩათი", key="new_chat", use_container_width=True):
            reset_chat()
            st.rerun()
        st.caption(f"კითხვები: {st.session_state.n_questions}/{MAX_QUESTIONS}")
        st.divider()
        st.caption("საუბრები")

        # Middle section: scrolls when the list is long
        with st.container(key="conv_list"):
            for c in db.list_conversations(user["id"]):
                active = c["id"] == st.session_state.conv_id
                col_title, col_menu = st.columns([5, 1], vertical_alignment="center")

                with col_title:
                    if st.button(
                        c["title"],
                        key=f"conv_{c['id']}",
                        type="primary" if active else "secondary",
                        use_container_width=True,
                    ) and not active:
                        open_conversation(db, user["id"], c["id"])
                        st.rerun()

                with col_menu:
                    with st.popover("⋯", use_container_width=True):
                        new_title = st.text_input(
                            "ახალი სახელი", value=c["title"], key=f"rename_in_{c['id']}"
                        )
                        if st.button("სახელის შენახვა", key=f"rename_{c['id']}", use_container_width=True):
                            if new_title.strip():
                                db.rename_conversation(c["id"], user["id"], new_title.strip())
                                st.rerun()
                        if st.button("საუბრის წაშლა", key=f"delete_{c['id']}", use_container_width=True):
                            db.delete_conversation(c["id"], user["id"])
                            if active:
                                reset_chat()
                            st.rerun()

        # Bottom section: pinned by CSS
        with st.container(key="logout_section"):
            if st.button("გასვლა", key="logout", use_container_width=True):
                for k in list(st.session_state.keys()):
                    del st.session_state[k]
                st.rerun()

def render_sources(sources):
    """Intro paragraph shown ONCE; then one bullet per document, with a sub-bullet per article/annex."""
    if not sources:
        return

    intro = SOURCES_INTRO_ONE if len(sources) == 1 else SOURCES_INTRO_MANY
    st.markdown(intro)

    lines = []
    for s in sources:
        lines.append(f"- **{s['name']}**. [{LINK_TEXT}]({s['url']})")
        for part in s.get("parts", []):
            lines.append(f"    - {part}")  # 4 spaces = nested bullet
    st.markdown("\n".join(lines))


def render_message(m, db, user_id):
    with st.chat_message(m["role"], avatar=None):
        # Marker for CSS styling
        st.markdown(
            f'<span class="msg-{"user" if m["role"] == "user" else "bot"}"></span>',
            unsafe_allow_html=True,
        )

        st.markdown(m["content"])
        render_sources(m.get("sources", []))

        if m.get("rewritten"):
            st.caption(f"🔎 საძიებო კითხვა: {m['rewritten']}")

        if m["role"] == "assistant" and m.get("id") and not m.get("error"):
            c_up, c_down, _ = st.columns([1, 1, 10])
            fb = m.get("feedback")

            up = c_up.button(
                "👍",
                key=f"up_{m['id']}",
                type="primary" if fb == 1 else "secondary",
            )
            down = c_down.button(
                "👎",
                key=f"down_{m['id']}",
                type="primary" if fb == -1 else "secondary",
            )

            if up or down:
                new = 1 if up else -1
                new = None if fb == new else new

                db.set_feedback(m["id"], user_id, new)
                m["feedback"] = new
                st.rerun()

def handle_question(db, user, user_q):
    if st.session_state.conv_id is None:
        conv = db.create_conversation(user["id"])  # titled with the date/time of the first question
        st.session_state.conv_id = conv["id"]
    conv_id = st.session_state.conv_id

    uid = db.add_message(conv_id, "user", user_q)
    st.session_state.messages.append({"role": "user", "content": user_q, "sources": [], "id": uid})
    with st.chat_message("user", avatar = None):
        st.markdown(user_q)

    with st.chat_message("assistant", avatar = None):
        with st.spinner("ვეძებ პასუხს..."):
            try:
                res = answer_question(user_q, st.session_state.history)
            except Exception as e:  # keep the UI alive on unexpected failures
                res = {"error": f"{type(e).__name__}: {e}"}

    common = dict(
        rewritten=res.get("rewritten"),
        retrieved=res.get("retrieved"),
        retrieved_docs=res.get("retrieved_docs"),
        model=CHAT_MODEL,
        latency=res.get("latency"),
    )

    if res.get("error"):
        # failed turns are logged but do not count towards the limit
        mid = db.add_message(conv_id, "assistant", ERROR_MSG, error=res["error"], **common)
        st.session_state.messages.append(
            {"role": "assistant", "content": ERROR_MSG, "sources": [], "id": mid, "error": res["error"]}
        )
        print("ERROR:", res["error"])
    else:
        mid = db.add_message(
            conv_id,
            "assistant",
            res["answer"],
            sources=res["sources"],
            cited_parents=res["cited_parents"],
            cited_docs=res["cited_docs"],
            **common,
        )
        db.increment_questions(conv_id)
        st.session_state.n_questions += 1
        st.session_state.history.append((user_q, res["answer"]))
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": res["answer"],
                "sources": res["sources"],
                "rewritten": res["rewritten"] if res["rewritten"] != user_q else None,
                "id": mid,
                "feedback": None,
                "error": None,
            }
        )
        if st.session_state.n_questions >= MAX_QUESTIONS:
            st.session_state.messages.append(
                {"role": "assistant", "content": CLOSING, "sources": []}
            )

    st.rerun()


# ============================================================
# MAIN
# ============================================================

def main():
    st.set_page_config(page_title=TITLE, page_icon="💬", layout="wide")
    inject_css()
    db = get_db()

    if "user" not in st.session_state:
        render_auth(db)
        return

    user = st.session_state.user
    if "messages" not in st.session_state:
        reset_chat()

    render_sidebar(db, user)
    # st.title(TITLE)

    for m in st.session_state.messages:
        render_message(m, db, user["id"])

    finished = st.session_state.n_questions >= MAX_QUESTIONS

    user_q = st.chat_input(
        "დასვი კითხვა..." if not finished else "ჩათი დასრულებულია",
        disabled=finished,
    )

    if user_q:
        handle_question(db, user, user_q)


if __name__ == "__main__":
    main()