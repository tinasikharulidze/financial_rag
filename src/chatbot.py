#!/usr/bin/env python3
"""
Streamlit chatbot on top of the Matsne RAG logic in generate.py.

Put this file next to generate.py (src/) and run from the project root:

    export GEMINI_API_KEY=...          # or whatever key your LiteLLM model needs
    uv run streamlit run src/chatbot.py

Optional environment variables (defaults in CONFIG below):
    CHAT_MODEL, REWRITE_MODEL, CHILDREN_PATH, PARENTS_PATH, DOCS_PATH, OUT_DIR
"""

import os
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

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

GREETING = "გამარჯობა, მე შემიძლია ინფორმაცია მოგაწოდო ქართული ფინანსური რეგულაციების შესახებ. რა გაინტერესებს?"
CLOSING = "ამ დროისთვის მეტ კითხვაზე პასუხის გაცემა არ შემიძლია. გთხოვ გახსნა ახალი ჩათი."
ERROR_MSG = "მოხდა შეცდომა პასუხის გენერირებისას. გთხოვ, სცადე ხელახლა."

MAX_QUESTIONS = 5

CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini/gemini-3-flash-preview")
REWRITE_MODEL = os.getenv("REWRITE_MODEL", CHAT_MODEL)
EMBED_MODEL = "BAAI/bge-m3"

CHILDREN_PATH = os.getenv("CHILDREN_PATH", "matsne_data/embeddings/children_bge_m3.jsonl")
PARENTS_PATH = os.getenv("PARENTS_PATH", "matsne_data/chunks/parents.jsonl")
DOCS_PATH = os.getenv("DOCS_PATH", "matsne_data/chunks/docs.jsonl")
OUT_DIR = Path(os.getenv("OUT_DIR", "matsne_data/generation"))

# Retrieval settings: same defaults as generate.py
BUDGET_TOKENS = 12000
CPT = 1.8
CAP_CHARS = 6000
PER_DOC = 10
MAX_TOKENS = 5000
RPM = 30

HISTORY_TURNS_FOR_REWRITE = 3
ANSWER_CHARS_FOR_REWRITE = 600

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


@st.cache_resource(show_spinner="ვტვირთავ ემბედინგის მოდელს...")
def load_embedder():
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    return SentenceTransformer(EMBED_MODEL, device=device)


@st.cache_resource
def load_cache():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return Cache(OUT_DIR / "chat_cache.jsonl")


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
        or a.startswith("ამ კითხვაზე პასუხის მოძიება ვერ შევძელი")
    )


def source_info(corpus, pid):
    """Return (label, url) for a parent id: regulation name + article, link."""
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

    label = name + (f", {', '.join(parts)}" if parts else "")
    url = d.get("url") or f"https://matsne.gov.ge/ka/document/view/{doc_id}"
    return label, url


def answer_question(user_q, history):
    """Full pipeline: rewrite -> embed -> retrieve -> generate.
    Returns dict(answer, sources[(label,url)], rewritten, error)."""
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

    out = call_llm(
        CHAT_MODEL,
        build_messages(rewritten, passages),
        MAX_TOKENS,
        RPM,
        cache,
    )

    if "error" in out:
        return {"error": out["error"], "rewritten": rewritten}

    valid_ids = {p["parent_id"] for p in passages}
    answer, source_ids = parse_llm_response(out.get("text", ""), valid_ids)

    if is_refusal(answer):
        return {"answer": REFUSAL, "sources": [], "rewritten": rewritten, "error": None}

    sources, seen = [], set()
    for pid in source_ids:
        label, url = source_info(corpus, pid)
        if (label, url) not in seen:
            seen.add((label, url))
            sources.append((label, url))

    return {"answer": answer, "sources": sources, "rewritten": rewritten, "error": None}


# ============================================================
# UI
# ============================================================

def reset_chat():
    st.session_state.messages = [{"role": "assistant", "content": GREETING, "sources": []}]
    st.session_state.history = []  # (question, answer) pairs used for rewriting
    st.session_state.n_questions = 0


def render_message(m):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        for label, url in m.get("sources", []):
            st.markdown(
                f"ეს ამოვიკითხე შემდეგი რეგულაციიდან: **{label}**, "
                f"ეს რეგულაცია შეგიძლია იხილო აქ: [{url}]({url})"
            )
        if m.get("rewritten"):
            st.caption(f"🔎 საძიებო კითხვა: {m['rewritten']}")


def main():
    st.set_page_config(page_title="ფინანსური რეგულაციების ჩატბოტი", page_icon="💬")
    st.title("ფინანსური რეგულაციების ჩატბოტი")

    if "messages" not in st.session_state:
        reset_chat()

    with st.sidebar:
        if st.button("ახალი ჩათი", use_container_width=True):
            reset_chat()
            st.rerun()
        st.caption(f"კითხვები: {st.session_state.n_questions}/{MAX_QUESTIONS}")

    for m in st.session_state.messages:
        render_message(m)

    finished = st.session_state.n_questions >= MAX_QUESTIONS

    user_q = st.chat_input(
        "დასვი კითხვა..." if not finished else "ჩათი დასრულებულია",
        disabled=finished,
    )

    if not user_q:
        return

    st.session_state.messages.append({"role": "user", "content": user_q, "sources": []})
    with st.chat_message("user"):
        st.markdown(user_q)

    with st.chat_message("assistant"):
        with st.spinner("ვეძებ პასუხს..."):
            try:
                res = answer_question(user_q, st.session_state.history)
            except Exception as e:  # keep the UI alive on unexpected failures
                res = {"error": f"{type(e).__name__}: {e}"}

    if res.get("error"):
        # failed turns do not count towards the limit
        st.session_state.messages.append(
            {"role": "assistant", "content": ERROR_MSG, "sources": []}
        )
        print("ERROR:", res["error"])
    else:
        st.session_state.n_questions += 1
        st.session_state.history.append((user_q, res["answer"]))
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": res["answer"],
                "sources": res["sources"],
                "rewritten": res["rewritten"] if res["rewritten"] != user_q else None,
            }
        )
        if st.session_state.n_questions >= MAX_QUESTIONS:
            st.session_state.messages.append(
                {"role": "assistant", "content": CLOSING, "sources": []}
            )

    st.rerun()


main()