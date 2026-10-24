"""Chat threads and messages. Every user message stores its context snapshot and every answer its tool trace
(audit record, reproducible answers). A thread belongs to one user; nobody else can read it."""
from __future__ import annotations

import json

from kg.config import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_threads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    company_id  TEXT,
    title       TEXT,
    root_id     TEXT,                 -- the document version the thread was opened on (a new document = a new thread)
    cursor      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL,
    role        TEXT NOT NULL,        -- user | assistant | divider
    content     TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '{}',   -- the context snapshot sent with a user message
    trace       TEXT NOT NULL DEFAULT '{}',   -- tools called, citations kept and dropped (assistant messages)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_thread ON chat_messages(thread_id, id);
"""


def ensure(store):
    with store._tx() as con:
        con.executescript(SCHEMA)


def new_thread(store, actor: dict, root_id: str | None = None, title: str = "") -> int:
    ensure(store)
    return store.x("INSERT INTO chat_threads(user_id, company_id, title, root_id, created_at) VALUES(?,?,?,?,?)",
                   (actor["id"], actor.get("company_id"), title[:80], root_id, now_iso()))


def get_thread(store, actor: dict, thread_id: int) -> dict | None:
    """Only the owner can open a thread."""
    ensure(store)
    t = store.q1("SELECT * FROM chat_threads WHERE id=? AND user_id=?", (thread_id, actor["id"]))
    if t:
        t["cursor"] = json.loads(t["cursor"] or "{}")
    return t


def add_message(store, thread_id: int, role: str, content: str, context: dict | None = None,
                trace: dict | None = None) -> int:
    return store.x("INSERT INTO chat_messages(thread_id, role, content, context, trace, created_at) VALUES(?,?,?,?,?,?)",
                   (thread_id, role, content, json.dumps(context or {}, ensure_ascii=False),
                    json.dumps(trace or {}, ensure_ascii=False, default=str), now_iso()))


def save_cursor(store, thread_id: int, cursor: dict):
    store.x("UPDATE chat_threads SET cursor=? WHERE id=?", (json.dumps(cursor, ensure_ascii=False), thread_id))


def messages(store, actor: dict, thread_id: int, limit: int = 200) -> list[dict]:
    if not get_thread(store, actor, thread_id):
        return []
    rows = store.q("SELECT * FROM chat_messages WHERE thread_id=? ORDER BY id LIMIT ?", (thread_id, limit))
    for r in rows:
        r["context"] = json.loads(r["context"] or "{}")
        r["trace"] = json.loads(r["trace"] or "{}")
    return rows


def family_versions(store, version_id: str) -> list[str]:
    """All versions of the same document (v1.0, v1.1, ...): a conversation belongs to the document, not to one version."""
    r = store.q1("SELECT family_id FROM document_versions WHERE id=?", (version_id,))
    if not r:
        return [version_id]
    return [x["id"] for x in store.q("SELECT id FROM document_versions WHERE family_id=?", (r["family_id"],))]


def same_document(store, root_id: str | None, version_id: str | None) -> bool:
    return bool(root_id and version_id and root_id in family_versions(store, version_id))


def set_title(store, thread_id: int, title: str):
    store.x("UPDATE chat_threads SET title=? WHERE id=? AND COALESCE(title,'')=''", (" ".join(title.split())[:80], thread_id))


def threads_for(store, actor: dict, version_id: str | None, limit: int = 30) -> list[dict]:
    """The actor's own conversations about this document (all its versions), newest first; only those with a question.
    Without a document (other pages): the conversations that were not about a document."""
    ensure(store)
    if version_id:
        roots = family_versions(store, version_id)
        where, args = f"t.root_id IN ({','.join('?' * len(roots))})", roots
    else:
        where, args = "t.root_id IS NULL", []
    return store.q(
        "SELECT t.id, t.created_at, COALESCE(NULLIF(t.title,''), (SELECT m.content FROM chat_messages m "
        "WHERE m.thread_id=t.id AND m.role='user' ORDER BY m.id LIMIT 1)) AS title FROM chat_threads t "
        f"WHERE t.user_id=? AND {where} AND EXISTS (SELECT 1 FROM chat_messages m WHERE m.thread_id=t.id AND m.role='user') "
        "ORDER BY t.id DESC LIMIT ?", (actor["id"], *args, limit))
