"""Relational tables of KG v2 (docs/KG_V2_PLAN.md sections 3.2 and 4.3) and thin helpers.

The graph itself (nodes / edges) stays in ``sqlite_nx.py``. Everything that is "records about people, documents,
decisions and stored LLM outputs" lives here, in the same SQLite file, with the same conventions: stdlib
``sqlite3``, ``CREATE TABLE IF NOT EXISTS``, one short transaction per call, JSON columns written with
``ensure_ascii=False``, Tbilisi-time ISO timestamps.

``V2Mixin`` is mixed into ``SQLiteStore`` and only offers four generic helpers (``q``, ``q1``, ``x``, ``xmany``);
the domain logic (accounts, documents, tasks ...) lives in ``kg/services/*`` and runs plain SQL through them.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

SCHEMA_V2 = """
-- background checks of a document version (one row per version; the UI polls it)
CREATE TABLE IF NOT EXISTS check_jobs (
    version_id  TEXT PRIMARY KEY,
    status      TEXT NOT NULL,          -- queued | running | done | failed | budget | unavailable | rejected
    pid         INTEGER,
    actor       INTEGER,
    run_id      TEXT,
    log_path    TEXT,
    message     TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
-- accounts (section 3.2)
CREATE TABLE IF NOT EXISTS companies (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    entity_type  TEXT NOT NULL,
    reg_number   TEXT,
    props        TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    user_type     TEXT NOT NULL,
    company_id    TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    UNIQUE(company_id, name)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id  INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (user_id, role_id)
);

-- documents (section 4.3)
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    company_id  TEXT NOT NULL,
    code        TEXT NOT NULL,
    title       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions (
    id            TEXT PRIMARY KEY,
    family_id     TEXT NOT NULL,
    version       TEXT NOT NULL,
    status        TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    sha1          TEXT NOT NULL,
    uploaded_by   INTEGER,
    uploaded_at   TEXT NOT NULL,
    published_at  TEXT,
    replaces      TEXT,
    mode          TEXT
);
CREATE TABLE IF NOT EXISTS doc_profiles (
    doc_version_id TEXT PRIMARY KEY,
    llm_output_id  INTEGER,
    doc_type       TEXT,
    domains        TEXT NOT NULL DEFAULT '[]',
    purpose        TEXT,
    sections       TEXT NOT NULL DEFAULT '[]',
    citations      TEXT NOT NULL DEFAULT '[]',
    confirmed_by   INTEGER,
    confirmed_at   TEXT
);

-- mapping results (sections 7 and 8)
CREATE TABLE IF NOT EXISTS triage (
    doc_version_id TEXT NOT NULL,
    obligation_id  TEXT NOT NULL,
    relevance      TEXT NOT NULL,
    reason         TEXT,
    citations      TEXT NOT NULL DEFAULT '[]',
    llm_output_id  INTEGER,
    source         TEXT NOT NULL DEFAULT 'llm',
    PRIMARY KEY (doc_version_id, obligation_id)
);
CREATE TABLE IF NOT EXISTS locations (
    doc_version_id TEXT NOT NULL,
    obligation_id  TEXT NOT NULL,
    sections       TEXT NOT NULL DEFAULT '[]',
    llm_output_id  INTEGER,
    PRIMARY KEY (doc_version_id, obligation_id)
);
CREATE TABLE IF NOT EXISTS coverage (
    doc_version_id TEXT NOT NULL,
    obligation_id  TEXT NOT NULL,
    verdict        TEXT NOT NULL,
    missing        TEXT,
    rationale      TEXT,
    citations      TEXT NOT NULL DEFAULT '[]',
    cause          TEXT,
    amendment_id   TEXT,
    llm_output_id  INTEGER,
    source         TEXT NOT NULL DEFAULT 'llm',
    invalid        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (doc_version_id, obligation_id)
);
CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_version_id TEXT NOT NULL,
    obligation_id  TEXT NOT NULL,
    kind           TEXT NOT NULL,
    clause_id      TEXT,
    original_text  TEXT,
    proposed_text  TEXT NOT NULL,
    heading        TEXT,
    explanation    TEXT NOT NULL,
    citations      TEXT NOT NULL DEFAULT '[]',
    flags          TEXT NOT NULL DEFAULT '[]',
    llm_output_id  INTEGER,
    created_at     TEXT NOT NULL,
    UNIQUE(doc_version_id, obligation_id)
);
-- the text a user (or the merge step) settled on for one place of the document: a clause changed by several
-- proposals ("clause:<id>"), an added section ("add:<proposal id>") or a new document ("new:<proposal id>")
CREATE TABLE IF NOT EXISTS clause_edits (
    doc_version_id TEXT NOT NULL,
    slot           TEXT NOT NULL,
    text           TEXT NOT NULL,
    proposal_ids   TEXT NOT NULL DEFAULT '[]',
    notes          TEXT NOT NULL DEFAULT '{}',
    source         TEXT NOT NULL,
    actor          INTEGER,
    ts             TEXT NOT NULL,
    PRIMARY KEY (doc_version_id, slot)
);

-- tasks and decisions (sections 9 and 10)
CREATE TABLE IF NOT EXISTS tasks (
    key                 TEXT PRIMARY KEY,
    company_id          TEXT NOT NULL,
    doc_version_id      TEXT NOT NULL,
    obligation_id       TEXT NOT NULL,
    proposal_id         INTEGER,
    title               TEXT NOT NULL,
    status              TEXT NOT NULL,
    score               REAL NOT NULL,
    priority            TEXT NOT NULL,
    severity            TEXT,
    verdict             TEXT,
    relevance           TEXT,
    cause               TEXT,
    amendment_number    TEXT,
    amendment_effective TEXT,
    assignee_user       INTEGER,
    assignee_role       INTEGER,
    due_date            TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key  TEXT NOT NULL,
    actor     INTEGER,
    event     TEXT NOT NULL,
    data      TEXT NOT NULL DEFAULT '{}',
    ts        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    actor          INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    doc_version_id TEXT,
    obligation_id  TEXT,
    proposal_id    INTEGER,
    clause_id      TEXT,
    data           TEXT NOT NULL DEFAULT '{}',
    ts             TEXT NOT NULL
);

-- stored LLM step outputs (section 12)
CREATE TABLE IF NOT EXISTS step_outputs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    step           TEXT NOT NULL,
    subject        TEXT NOT NULL,
    input_hash     TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model          TEXT,
    output         TEXT NOT NULL,
    valid          INTEGER NOT NULL DEFAULT 1,
    errors         TEXT,
    run_id         TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE(step, subject, input_hash, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_users_company ON users(company_id);
CREATE INDEX IF NOT EXISTS idx_docs_company ON documents(company_id);
CREATE INDEX IF NOT EXISTS idx_docv_family ON document_versions(family_id);
CREATE INDEX IF NOT EXISTS idx_triage_doc ON triage(doc_version_id);
CREATE INDEX IF NOT EXISTS idx_cov_doc ON coverage(doc_version_id);
CREATE INDEX IF NOT EXISTS idx_prop_doc ON proposals(doc_version_id);
CREATE INDEX IF NOT EXISTS idx_tasks_company ON tasks(company_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_doc ON tasks(doc_version_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_key);
CREATE INDEX IF NOT EXISTS idx_dec_doc ON user_decisions(doc_version_id, obligation_id);
CREATE INDEX IF NOT EXISTS idx_steps_step ON step_outputs(step, subject);
"""


def jdump(obj: Any) -> str:
    return json.dumps(obj if obj is not None else {}, ensure_ascii=False, default=str)


def jload(text: str | None, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


class V2Mixin:
    """Generic SQL helpers used by the v2 services. Requires ``self._tx()`` (see SQLiteStore)."""

    def q(self, sql: str, args: Iterable = ()) -> list[dict]:
        with self._tx() as con:
            return [dict(r) for r in con.execute(sql, tuple(args))]

    def q1(self, sql: str, args: Iterable = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: Iterable = ()) -> int:
        """Execute one write statement; returns lastrowid."""
        with self._tx() as con:
            cur = con.execute(sql, tuple(args))
            return cur.lastrowid

    def xmany(self, sql: str, rows: Iterable[Iterable]) -> None:
        with self._tx() as con:
            con.executemany(sql, [tuple(r) for r in rows])
