#!/usr/bin/env python3
"""
SQLite storage for the chatbot: users, conversations, messages.

No Streamlit imports, so it can be tested on its own.

* Passwords: any password is accepted (no strength rules), but it is stored as a salted PBKDF2 hash,
  never as plain text.
* Conversations are titled with the date/time of their first question, in Tbilisi time (UTC+4, no DST).
* Every message keeps what you need for later analysis: rewritten search question, retrieved parent ids and
  document ids, the parents/documents the LLM actually cited, model, latency, error text, thumbs up/down.

Inspect the logs from a terminal, e.g.:
  sqlite3 -header -csv matsne_data/app.db \
    "SELECT c.title, u.username, m.role, m.content, m.rewritten, m.retrieved_docs, m.cited_docs, m.feedback
     FROM messages m JOIN conversations c ON c.id=m.conversation_id JOIN users u ON u.id=c.user_id
     ORDER BY m.id" > chat_logs.csv
"""
import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

TBILISI = timezone(timedelta(hours=4))        # Georgia has no daylight saving time
PBKDF2_ROUNDS = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
    salt        BLOB NOT NULL,
    pw_hash     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    n_questions INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id),
    role              TEXT NOT NULL,
    content           TEXT NOT NULL,
    sources           TEXT,
    rewritten         TEXT,
    retrieved_parents TEXT,
    retrieved_docs    TEXT,
    cited_parents     TEXT,
    cited_docs        TEXT,
    model             TEXT,
    latency_s         REAL,
    error             TEXT,
    feedback          INTEGER,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, id);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);
"""


def now_tbilisi():
    return datetime.now(TBILISI)


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


class ChatDB:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as con:
            con.executescript(SCHEMA)
            have = {r["name"] for r in con.execute("PRAGMA table_info(messages)")}
            for col in ("retrieved_docs", "cited_parents", "cited_docs"):
                if col not in have:                      # older app.db: add the column in place
                    con.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")

    @contextmanager
    def _tx(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    # ------------------------------------------------------------ users
    def create_user(self, username, password):
        """-> {"id", "username"} or None if the name is empty/taken (case-insensitive)."""
        username = (username or "").strip()
        if not username or not password:
            return None
        salt = os.urandom(16)
        try:
            with self._tx() as con:
                cur = con.execute(
                    "INSERT INTO users(username, salt, pw_hash, created_at) VALUES (?,?,?,?)",
                    (username, salt, _hash(password, salt), now_tbilisi().isoformat(timespec="seconds")),
                )
                return {"id": cur.lastrowid, "username": username}
        except sqlite3.IntegrityError:
            return None

    def verify_user(self, username, password):
        """-> {"id", "username"} if the credentials match, else None."""
        username = (username or "").strip()
        if not username or not password:
            return None
        with self._tx() as con:
            row = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row and hmac.compare_digest(row["pw_hash"], _hash(password, row["salt"])):
            return {"id": row["id"], "username": row["username"]}
        return None

    # ---------------------------------------------------- conversations
    def create_conversation(self, user_id):
        """Titled with the current Tbilisi date/time, e.g. '2026-10-14 15:32'."""
        now = now_tbilisi()
        with self._tx() as con:
            title = now.strftime("%Y-%m-%d %H:%M")
            if con.execute("SELECT 1 FROM conversations WHERE user_id=? AND title=?",
                           (user_id, title)).fetchone():
                title = now.strftime("%Y-%m-%d %H:%M:%S")          # two chats in one minute
            cur = con.execute(
                "INSERT INTO conversations(user_id, title, created_at) VALUES (?,?,?)",
                (user_id, title, now.isoformat(timespec="seconds")),
            )
            return {"id": cur.lastrowid, "title": title, "n_questions": 0}

    def list_conversations(self, user_id, limit=50):
        with self._tx() as con:
            rows = con.execute(
                "SELECT id, title, n_questions FROM conversations WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_conversation(self, conv_id, user_id):
        """Ownership-checked: returns None if the conversation belongs to someone else."""
        with self._tx() as con:
            row = con.execute(
                "SELECT id, title, n_questions FROM conversations WHERE id=? AND user_id=?",
                (conv_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def increment_questions(self, conv_id):
        with self._tx() as con:
            con.execute("UPDATE conversations SET n_questions = n_questions + 1 WHERE id=?", (conv_id,))

    # ---------------------------------------------------------- messages
    def add_message(self, conv_id, role, content, sources=None, rewritten=None, retrieved=None,
                    retrieved_docs=None, cited_parents=None, cited_docs=None,
                    model=None, latency=None, error=None):
        """retrieved / retrieved_docs: what the retriever put in the prompt (parent ids / document ids).
        cited_parents / cited_docs: what the LLM said it used. sources: the doc list shown to the user."""
        join = lambda xs: ";".join(map(str, xs)) if xs else None
        with self._tx() as con:
            cur = con.execute(
                """INSERT INTO messages(conversation_id, role, content, sources, rewritten,
                                        retrieved_parents, retrieved_docs, cited_parents, cited_docs,
                                        model, latency_s, error, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (conv_id, role, content,
                 json.dumps(sources, ensure_ascii=False) if sources else None,
                 rewritten, join(retrieved), join(retrieved_docs), join(cited_parents), join(cited_docs),
                 model, latency, error, now_tbilisi().isoformat(timespec="seconds")),
            )
            return cur.lastrowid

    def load_messages(self, conv_id):
        with self._tx() as con:
            rows = con.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id",
                               (conv_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = json.loads(d["sources"]) if d["sources"] else []
            out.append(d)
        return out

    def set_feedback(self, msg_id, user_id, value):
        """value: 1 (thumbs up), -1 (thumbs down) or None (clear). Only the owner can set it."""
        with self._tx() as con:
            con.execute(
                """UPDATE messages SET feedback=? WHERE id=? AND conversation_id IN
                   (SELECT id FROM conversations WHERE user_id=?)""",
                (value, msg_id, user_id),
            )

    def rename_conversation(self, conv_id, user_id, title):
        """Only the owner can rename. Empty titles are ignored."""
        title = (title or "").strip()
        if not title:
            return
        with self._tx() as con:
            con.execute(
                "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
                (title, conv_id, user_id),
            )

    def delete_conversation(self, conv_id, user_id):
        """Only the owner can delete. Messages go first because of the foreign key."""
        with self._tx() as con:
            row = con.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conv_id, user_id),
            ).fetchone()
            if not row:
                return
            con.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            con.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))