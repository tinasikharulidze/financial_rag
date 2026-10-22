# KG v2 — implementation plan

*Written 2026-10-21. Status: plan for review, nothing implemented yet.*

This document describes what we change and add in the repository, how, and why. It is written for the developer and for an LLM coding assistant, so every section names the files, tables, functions and acceptance criteria. Read `docs/DEVELOPER_WALKTHROUGH.md` first for the current system; this plan only describes the differences.

---

## Contents

0. [Summary of decisions](#0-summary-of-decisions)
1. [Principles that apply everywhere](#1-principles-that-apply-everywhere)
2. [Target architecture in one picture](#2-target-architecture-in-one-picture)
3. [Users, companies, roles and permissions](#3-users-companies-roles-and-permissions)
4. [Data model changes](#4-data-model-changes)
5. [Regulation side: titles, references, obligation extraction v2, amendments](#5-regulation-side)
6. [Document side: upload, versions, parsing, profile](#6-document-side)
7. [Mapping v2: filter, triage, locate, judge](#7-mapping-v2)
8. [Proposals and the revised document](#8-proposals-and-the-revised-document)
9. [Tasks, ranking and the task board](#9-tasks-ranking-and-the-task-board)
10. [User decisions and edits](#10-user-decisions-and-edits)
11. [Explainability: citations and links](#11-explainability-citations-and-links)
12. [Logging and reproducibility: never re-call the LLM unnecessarily](#12-logging-and-reproducibility)
13. [LLM: Gemma on Google Cloud](#13-llm-gemma-on-google-cloud)
14. [UI pages](#14-ui-pages)
15. [Evaluation v2 and the gold sets](#15-evaluation-v2-and-the-gold-sets)
16. [What is removed or deferred](#16-what-is-removed-or-deferred)
17. [Roadmap: phases, files, acceptance criteria](#17-roadmap)
18. [Open items to verify during implementation](#18-open-items-to-verify)

---

## 0. Summary of decisions

| Topic | Decision |
|---|---|
| Users | Simple registration and login (no strong password rules). Two user types chosen at registration: **regulator** and **regulated entity**. |
| Company | Regulated-entity users belong to a company with a profile: name, **entity type**, and **roles** (used later for assigning tasks). |
| Visibility | A regulated entity sees only its own company's documents and tasks. A regulator searches companies and sees each company's published documents and task board, **read-only**. |
| Regulator rights | Cannot upload, cannot accept or dismiss anything, cannot edit coverage. Sees issues, explanations, proposed solutions, and the company's decisions (who, when, why). |
| Upload | The user says whether the file is a **new version of an existing document** or a **new document**. For a new version: **keep as a new version** or **replace the current version**. |
| Document lifecycle | An upload is a **draft** visible only to the company. When every issue is handled, the user **publishes** it. The regulator sees published versions only. |
| Document profile | LLM summary on upload (purpose, domains, type, sections with short summaries and goals). **The user confirms or edits it** before mapping runs. |
| Filtering | Obligations are filtered by the document's **domain** and the company's **entity type**, then triaged by the LLM into **relevant / possibly relevant / irrelevant**. Irrelevant ones are not checked further. |
| Coverage | One obligation can have **several `COVERED_BY` edges** to different clauses, each labelled **full / partial / contradicts**. A separate per-document **verdict** says whether the clauses together cover it. |
| Cross-document coverage | **Not now.** Focus on the quality of each document. |
| Proposals | **One proposal per obligation** (the user wants to see each update). When the revised text is built, several accepted proposals on the same clause are merged. |
| Accept | Accepting a proposal marks it accepted **and** the system can build a **revised document text** (download) with all accepted changes applied. |
| Dismissals | "Already addressed" requires choosing the covering clause(s) and creates a user-confirmed edge. "Not relevant" requires a reason. Both are **visible to the regulator** with who and when. |
| Edge editing | The user can add, change or remove obligation–clause edges. User edits are never overwritten by re-runs. |
| Ranking | Severity × gap × relevance. **Contradicts and not covered rank the same.** |
| Amendment flag | Tasks caused by an amendment show the amendment number and its **effective date** (plus a "new" badge when recent). |
| Hybrid search text | Provisions are indexed and embedded **with their own title and the article title** (and the act's short name). |
| Titles | Fix missing titles on Provision and ProvisionVersion nodes. |
| References | Every referenced provision gets a node, **even without text** (placeholder). |
| Logging | Every LLM output and every user decision is stored; anything that can be recomputed without the LLM is recomputed from stored outputs. |
| LLM | Gemma on Google Cloud (billed). The 20-requests-per-day limit is no longer a design constraint. |
| Removed | The amendment impact page (UI). Phase 9 (escalation agent) is not planned. |

---

## 1. Principles that apply everywhere

1. **LLM output is data, stored once.** Every LLM step writes a typed, versioned record (§12). Downstream logic (ranking, tasks, diffs, UI) is deterministic code over stored records. Changing a weight, a filter, or the UI never needs a new LLM call.
2. **Every label carries its evidence.** Any LLM label or proposal includes citations: provision ids, clause ids, verbatim quotes, links (§11). Outputs whose quotes or ids do not verify are rejected and logged, not saved.
3. **The human has the last word.** User decisions (profile corrections, dismissals, edge edits, accepted proposals) are stored separately from LLM output and always take precedence. Re-runs read them and never overwrite them (§10).
4. **Authorization lives in the service layer**, not only in the UI. Every service function takes the acting user and checks access (§3).
5. **Keep the current conventions**: SQLite through `SQLiteStore` (stdlib `sqlite3`, `SCHEMA` + `_tx()` + light migrations, `src/chat_db.py` style), runs and JSONL logs (`runlog.py`), one LLM entry point (`llm.py`), config in `configs/kg.yaml`, Georgian UI strings in `app/i18n_ka.py`.
6. **Incremental by default.** A step runs only for inputs whose content hash or prompt version changed (§12).

---

## 2. Target architecture in one picture

```
REGULATION SIDE (offline, when acts/amendments change)
  parse-acts ─► parse-amendments ─► consolidate ─► extract-refs (+ placeholder nodes)
     │                                              │
     └────────── titles on every node ──────────────┘
  build-context (no LLM) ─► extract-obligations v2 (LLM) ─► amend-diff (LLM, per changed unit)
                                                         => Obligation nodes v2 + SUPERSEDES lineage

DOCUMENT SIDE (online, per upload)
  upload (new doc | new version: keep | replace) ─► parse PDF (+ bbox) ─► profile (LLM)
     ─► user confirms profile ─► filter (domain + entity, no LLM) ─► triage (LLM)
     ─► locate (LLM, section map) ─► judge (LLM: whole document or hybrid candidates)
     ─► proposals (LLM, one per obligation) ─► tasks (no LLM: ranking, cause, dates)
     ─► review page: user accepts / not relevant / already addressed / edits edges
     ─► [optional] build revised text (merge accepted proposals) ─► publish
REGULATION CHANGE
  new amendment ─► consolidate ─► amend-diff ─► for each published document version:
     triage + locate + judge + proposals for the new/modified obligations only ─► tasks (cause = amendment)
```

---

## 3. Users, companies, roles and permissions

### 3.1 Why
The product has two audiences with different rights. The regulated entity works on its documents; the regulator supervises. A company also needs roles to assign tasks to.

### 3.2 Tables (new, in `src/kg/store/sqlite_nx.py` `SCHEMA`, or a sibling `src/kg/store/accounts.py` following the same conventions)

```sql
CREATE TABLE IF NOT EXISTS companies (
    id           TEXT PRIMARY KEY,          -- slug, e.g. "bank-a"
    name         TEXT NOT NULL,
    entity_type  TEXT NOT NULL,             -- e.g. commercial_bank (same vocabulary as EntityType nodes)
    reg_number   TEXT,
    props        TEXT NOT NULL DEFAULT '{}',-- address, contacts, anything else from the profile form
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    password_hash TEXT NOT NULL,            -- hashlib.pbkdf2_hmac('sha256', ...) + per-user salt, stdlib only
    user_type     TEXT NOT NULL,            -- regulator | entity
    company_id    TEXT,                     -- NULL for regulators
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company_roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id  TEXT NOT NULL,
    name        TEXT NOT NULL,              -- e.g. "Model risk manager", "Internal audit"
    UNIQUE(company_id, name)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id  INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (user_id, role_id)
);
```

### 3.3 Registration and login
- `app/pages/auth.py` (or a section in `app/kg_app.py`): registration form with name, email, password, **user type** (regulator / regulated entity). A regulated-entity user either creates a company (fills the company profile) or joins an existing one by name. Simple password rules (non-empty, at least 6 characters).
- Login stores the user in `st.session_state["user"]`. Logout clears it. No OAuth for now.
- `src/kg/services/accounts.py`: `register`, `authenticate`, `get_user`, `company_of(user)`, `list_companies(query)`.

### 3.4 Permissions (enforced in `src/kg/services/*`, checked by a helper `require(user, action, resource)`)

| Action | Regulated entity | Regulator |
|---|---|---|
| Edit own company profile and roles | yes | no |
| Upload a document / new version | yes (own company) | no |
| See documents | own company, drafts and published | any company, **published only** |
| See tasks and issues | own company | any company (published versions) |
| Accept / dismiss a proposal, edit edges, edit profile | yes (own company) | no |
| Build revised text, publish | yes (own company) | no |
| Search companies | no | yes |
| Ask regulation questions (Q&A), compare versions | yes | yes |

Every function that reads or writes company data takes `actor: dict` (the user) and calls `require(...)`. Tests cover the matrix.

---

## 4. Data model changes

### 4.1 Graph nodes (table `nodes`)

| Node | Change |
|---|---|
| `Act` | add `domains: [..]` (from config, e.g. `["model_risk"]`) and `short` (already exists). |
| `Provision` | add `title` (article heading for articles; `label` + article title for paragraphs and sub-points), `display` (readable path), `placeholder: bool`. |
| `ProvisionVersion` | add `article_title`, `act_short`, `display`, `index_text` (the text used for search and embeddings, §5.1). |
| `ExternalAct` | unchanged. |
| `ExternalProvision` (**new**) | a provision of an act outside the corpus that is referenced, e.g. `ext:law:<title>/art5/p2`; `placeholder: true`, no text. `PART_OF` edge to its `ExternalAct`. |
| `Obligation` | schema v2 (§5.3); id format unchanged (`obl:<act>/<unit>#<n>@<date>`). |
| `InternalDoc` | becomes **one node per document version**: id `doc:<company>/<code>@<version>`; props add `company_id`, `family_id`, `status`, `profile` reference. |
| `Clause` | add `bbox` (`[x0, top, x1, bottom]`) and `page` (exists); id `cl:<company>/<code>@<version>/<section>`. |

### 4.2 Graph edges

| Edge | Change |
|---|---|
| `REFERS_TO` | points to the **exact** referenced path; if it does not exist, a placeholder `Provision` or `ExternalProvision` is created instead of falling back to the parent (§5.2). |
| `SUPERSEDES` (**new**) | new obligation → obligation it replaces (from the amendment diff, §5.4). Props: `change: modified`, `amendment`, `reason`. |
| `COVERED_BY` | obligation → clause; props: `role` (`full` / `partial` / `contradicts`), `source` (`llm` / `user`), `doc_version`, `run`, `output_id` (link to the stored LLM output), `quote`. **Several per obligation and document.** |
| `PART_OF` (**new**) | `ExternalProvision` → `ExternalAct`. |

### 4.3 Relational tables (new)

```sql
-- document families and versions (one family = one logical document of a company)
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,        -- family id, e.g. "bank-a/POL-RM-014"
    company_id  TEXT NOT NULL,
    code        TEXT NOT NULL,
    title       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions (
    id            TEXT PRIMARY KEY,      -- = InternalDoc node id
    family_id     TEXT NOT NULL,
    version       TEXT NOT NULL,         -- "1.0", "2.0" ...
    status        TEXT NOT NULL,         -- draft | published | replaced | archived
    file_path     TEXT NOT NULL,         -- data/uploads/<company>/<sha1>.pdf
    sha1          TEXT NOT NULL,
    uploaded_by   INTEGER NOT NULL,
    uploaded_at   TEXT NOT NULL,
    published_at  TEXT,
    replaces      TEXT                   -- version id this one replaced (replace mode)
);
-- document profile (LLM proposal + user-confirmed values, see §6.3)
CREATE TABLE IF NOT EXISTS doc_profiles (
    doc_version_id TEXT PRIMARY KEY,
    llm_output_id  INTEGER,              -- step_outputs.id
    doc_type       TEXT, domains TEXT,   -- confirmed values (JSON list for domains)
    purpose        TEXT,
    sections       TEXT NOT NULL,        -- JSON [{section_no, title, summary, goal}]
    confirmed_by   INTEGER, confirmed_at TEXT
);
-- triage, one row per (document version, obligation)
CREATE TABLE IF NOT EXISTS triage (
    doc_version_id TEXT, obligation_id TEXT,
    relevance      TEXT NOT NULL,        -- relevant | possibly | irrelevant
    reason         TEXT, citations TEXT, -- JSON
    llm_output_id  INTEGER, source TEXT NOT NULL DEFAULT 'llm',   -- llm | user | rule
    PRIMARY KEY (doc_version_id, obligation_id)
);
-- per-document coverage verdict for one obligation
CREATE TABLE IF NOT EXISTS coverage (
    doc_version_id TEXT, obligation_id TEXT,
    verdict        TEXT NOT NULL,        -- full | partial | contradicts | none
    missing        TEXT,                 -- what is still missing (Georgian)
    rationale      TEXT, citations TEXT,
    cause          TEXT,                 -- pre_existing | amendment
    amendment_id   TEXT,                 -- amend:<doc> when cause = amendment
    llm_output_id  INTEGER, source TEXT NOT NULL DEFAULT 'llm',
    PRIMARY KEY (doc_version_id, obligation_id)
);
-- proposals, one per (document version, obligation) and target
CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_version_id TEXT NOT NULL, obligation_id TEXT NOT NULL,
    kind           TEXT NOT NULL,        -- adjust_clause | add_section | new_document
    clause_id      TEXT,                 -- target clause (adjust) or anchor clause (add: insert after)
    original_text  TEXT, proposed_text TEXT NOT NULL,
    explanation    TEXT NOT NULL, citations TEXT NOT NULL,
    llm_output_id  INTEGER, created_at TEXT NOT NULL
);
-- tasks (the board), §9
CREATE TABLE IF NOT EXISTS tasks (
    key            TEXT PRIMARY KEY,     -- stable: <company>|<doc family>|<obligation lineage root>
    company_id     TEXT NOT NULL, doc_version_id TEXT NOT NULL, obligation_id TEXT NOT NULL,
    proposal_id    INTEGER,
    title          TEXT NOT NULL,
    status         TEXT NOT NULL,        -- open | accepted | resolved | not_relevant | already_addressed | replaced
    score REAL NOT NULL, priority TEXT NOT NULL,
    severity TEXT, verdict TEXT, relevance TEXT,
    cause TEXT, amendment_number TEXT, amendment_effective TEXT,
    assignee_user INTEGER, assignee_role INTEGER, due_date TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL,
    actor INTEGER, event TEXT NOT NULL,  -- created | status_change | assigned | comment | auto_resolved ...
    data TEXT NOT NULL DEFAULT '{}', ts TEXT NOT NULL
);
-- user decisions (never overwritten by the pipeline), §10
CREATE TABLE IF NOT EXISTS user_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor INTEGER NOT NULL, kind TEXT NOT NULL,   -- accept | not_relevant | already_addressed | edge_add | edge_change | edge_remove | profile_edit | publish
    doc_version_id TEXT, obligation_id TEXT, proposal_id INTEGER, clause_id TEXT,
    data TEXT NOT NULL DEFAULT '{}',              -- reason, chosen clauses, old/new role ...
    ts TEXT NOT NULL
);
-- stored LLM step outputs, §12
CREATE TABLE IF NOT EXISTS step_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step TEXT NOT NULL,                  -- extract_obligations | amend_diff | doc_profile | triage | locate | judge | propose | merge
    subject TEXT NOT NULL,               -- what it is about, e.g. unit id, doc version id, (doc, batch) ...
    input_hash TEXT NOT NULL, prompt_version TEXT NOT NULL, model TEXT,
    output TEXT NOT NULL,                -- validated JSON
    valid INTEGER NOT NULL, errors TEXT,
    run_id TEXT, created_at TEXT NOT NULL,
    UNIQUE(step, subject, input_hash, prompt_version)
);
```

The old `findings` and `alerts` tables are kept read-only for the old evaluation until Phase 8, then removed.

---

## 5. Regulation side

### 5.1 Titles on every node, and the search/embedding text

**Problem.** `Provision` nodes store only `act, path, level, order` and no title; paragraph versions have no `heading`; 13 of 137 articles have no heading. The graph page therefore shows bare ids.

**Change** (`src/kg/ingest/jobs.py` `write_act_graph`, `src/kg/ingest/consolidate.py`):
- For every provision path compute:
  - `article_title`: heading of the article the path belongs to (from the article's version valid on the same date);
  - `title`: the article heading for articles; for paragraphs and sub-points, `label` + the first ~80 characters of the text;
  - `display`: `ids.display_path(path)` in Georgian form (e.g. "მუხლი 6, პუნქტი 4¹");
  - `act_short`: from config `acts.<id>.short`.
- `index_text` for each ProvisionVersion, used by **every** search and embedding over provisions:

```
{act_short} › მუხლი {N}. {article_title} › {display}
{label} {text}
```

- Articles without a heading get `article_title = ""` and the line still carries the article number.
- `app/kg_app.py` graph page and every UI label use `display` + `title` instead of raw ids.

**Why.** A paragraph like "4. ... must be repeated annually" only makes sense under "Article 6. Model validation". Search and embeddings without the article title miss that topic; this was the user's explicit requirement.

### 5.2 References: exact targets and placeholder nodes

**Change** (`src/kg/ingest/jobs.py` `extract_refs`):
- Stop trimming the target path to the nearest existing ancestor. If the exact path does not exist in the act, create a `Provision` with `placeholder: true` and no versions, and point `REFERS_TO` to it. Store `resolved_to` (the nearest existing ancestor) as an edge prop so the context builder can still fetch text.
- References to articles of external acts (law titles or order numbers followed by an article/paragraph) create an `ExternalProvision` node `ext:<key>/<path>` with `PART_OF` to the `ExternalAct`.
- Keep `cross_references.csv` output.

**Why.** The user wants the graph to show every referenced provision even when we do not have its text. Placeholders also make missing context visible ("text not available") instead of silently substituting a parent.

### 5.3 Obligation extraction v2

#### 5.3.1 Context builder (new, no LLM): `src/kg/extract/context.py`
For each extraction unit (unchanged definition: paragraph + its sub-points; article without paragraphs + its sub-points) build:

| Context part | Source |
|---|---|
| act title, act short name, domains | `Act` node |
| article number, title and lead text | article ProvisionVersion at the unit date |
| the unit text (with sub-point paths in brackets) | as today |
| definitions used | Article 2 (definitions) sub-points whose defined term appears in the unit (string match on the term in quotes) |
| referenced provisions | one hop of `REFERS_TO`, each with `index_text`, capped at 800 characters; placeholders shown as "(text not available)" |
| previous version and its obligations | only when the unit has an earlier version (used by §5.4) |

Output: a dict serialised into the prompt with clear labels. The model is told that only the **unit** is the source of obligations; everything else is context.

#### 5.3.2 Schema v2 (pydantic, `src/kg/extract/obligations.py`)

```python
class Citation(BaseModel):
    kind: Literal["provision", "clause", "obligation", "act", "external"]
    id: str                  # node id (pv:..., cl:..., obl:..., act:..., ext:...)
    quote: str | None        # verbatim, checked against the node text
    url: str | None          # filled by code, not by the model (§11)

class RequiredDoc(BaseModel):
    doc_types: list[str]     # open vocabulary, e.g. ["policy"], ["model_inventory"]
    basis: Literal["explicit", "inferred"]
    quote: str | None        # required when basis == "explicit"

class ObligationV2(BaseModel):
    statement: str           # one checkable sentence, Georgian
    deontic: Literal["must", "must_not", "may"]
    subject: str             # who acts (role in the entity, e.g. "დაცვის მეორე ხაზი")
    applies_to: list[str]    # entity types, e.g. ["commercial_bank"]; from text and act scope
    action: str
    condition: str | None
    exceptions: str | None   # text of exceptions; links to excepting provisions go in citations
    deadline_or_frequency: str | None
    severity: Literal["critical", "high", "medium", "low"]
    category: str            # label only (no longer used for routing); open vocabulary
    domain: str              # e.g. model_risk; defaults to the act's domain
    required_doc: RequiredDoc | None    # null when nothing is said or inferable
    evidence_type: str | None           # what would prove compliance: policy clause, inventory field, report, NBG submission ...
    location_hint: str | None           # 1–2 sentences: where/how a document should address it
    depends_on: list[str]               # paths of provisions this duty depends on (from references)
    source_path: str
    citations: list[Citation]           # at least the source provision with a verbatim quote
```

- New prompt `src/kg/extract/prompts/obligations_v2.md`, prompt version `obl-v2.0`.
- `explicit` only when the regulation itself names the document; then the quote is mandatory and verified.
- `category` stays as a display label; the fixed list is no longer enforced (the model may use a new label).
- `domain` and `applies_to` default from the act (`configs/kg.yaml`) and can be narrowed by the model with a citation.
- Batch size: about 5 units per call (longer prompts than v1). Configurable.

#### 5.3.3 Writing the graph
As today (Obligation nodes, `IMPOSES`, `APPLIES_TO`), plus:
- `APPLIES_TO` edges to one `EntityType` node per value in `applies_to` (no longer always `commercial_bank`).
- obligation props store the full v2 record and `output_id` (§12).

### 5.4 Amendment diff (replaces the Jaccard `predecessor`): `src/kg/extract/amend_diff.py`

**When:** a unit has a new version (its text changed) **or** a provision it refers to changed (rule: follow incoming `REFERS_TO` from changed provisions).

**Input:** old unit text, new unit text, the old obligations of the unit (statement + key attributes), the amendment number and effective date, and the context of §5.3.1.

**Output (LLM, prompt `amend-diff-v1`):**

```json
{"operations": [
  {"op": "unchanged", "old_id": "obl:...#1@2021-01-01"},
  {"op": "modified",  "old_id": "obl:...#2@2021-01-01", "new": { ObligationV2 }, "what_changed": "..."},
  {"op": "new",       "new": { ObligationV2 }},
  {"op": "removed",   "old_id": "obl:...#3@2021-01-01", "reason": "..."}
]}
```

**Writing:**
- `unchanged`: the old obligation's `valid_to` stays open; no new node. (Fixes the false "changed" problem: an edit elsewhere in the unit no longer creates a new obligation.)
- `modified`: close the old one (`valid_to = effective date`), create the new one, add `SUPERSEDES` with `what_changed` and the amendment id.
- `new`: create it with `valid_from = effective date`.
- `removed`: close it.

**Why.** The new/modified/removed decision is what makes a task "caused by amendment X". The current word-overlap matching is too weak for that.

For the **initial** build (V0 of an act) there is no diff: units are extracted with §5.3. Later versions use §5.4. The command `extract-obligations` does both in date order.

---

## 6. Document side

### 6.1 Upload flow (`src/kg/services/documents.py`, UI §14.3)

1. The user (regulated entity) chooses: **new document** or **new version of an existing document** (select from the company's document families).
2. For a new version: **keep as a new version** (old version stays, status unchanged until the new one is published) or **replace the current version** (on publish, the old version gets `status = replaced`, its open tasks get `status = replaced`).
3. The file is saved to `data/uploads/<company_id>/<sha1>.pdf` (gitignored). Same sha1 as an existing version → tell the user and stop.
4. Parse (§6.2), create the `document_versions` row (`status = draft`) and the graph nodes.
5. Profile (§6.3). The user confirms or edits it.
6. Mapping (§7) and proposals (§8) run. Progress is shown.
7. The review page opens (§14.4).
8. When every task of this version is handled (accepted / not relevant / already addressed), **Publish** becomes available. Publish sets `status = published`, `published_at`, and (replace mode) retires the old version.
9. Optionally the user builds the **revised text** (§8.3) and later uploads it as the next version; on that check, tasks whose obligations are now covered are auto-resolved (§9.4).

### 6.2 Parsing changes (`src/kg/company/pdf_parser.py`, `src/kg/company/ingest.py`)
- Keep the heuristics. Store the bounding box of every unit (`bbox`, union of its lines' boxes) and keep `page`.
- Clause ids include company and version (§4.1) so two companies' documents never collide.
- Add `doc_family_id`, `company_id` to the `InternalDoc` props.
- Warnings (`doc_date_missing`, `heuristics_weak`, very few units) are shown to the user on the profile step, not hidden.

### 6.3 Document profile (LLM): `src/kg/company/profile.py`

**Input:** the title, the cover metadata, the parser's section list (section numbers + headings), and the text (whole document when it fits the threshold, §13.3; otherwise the first N characters of every section).

**Output (prompt `doc-profile-v1`):**

```json
{
  "purpose": "2–4 sentences: what the document is and what it is for",
  "doc_type": "open label, e.g. model_risk_policy, model_inventory, validation_report",
  "domains": ["model_risk"],
  "sections": [{"section_no": "5.3", "title": "...", "summary": "1–2 sentences", "goal": "1 sentence"}],
  "citations": [{"kind": "clause", "id": "cl:...", "quote": "..."}]
}
```

- `section_no` must be one of the parser's sections (validated).
- The owner entity type comes from the **company profile**, not from the LLM.
- The user confirms or edits `doc_type`, `domains`, `purpose` and section summaries on the profile step. Edits are a `profile_edit` decision (§10). Mapping uses the confirmed values.

---

## 7. Mapping v2

New package layout under `src/kg/mapping/`: `filter.py`, `triage.py`, `locate.py`, `judge.py` (rewritten), `coverage.py` (rewritten orchestration), `ranking.py`, plus `src/kg/mapping/proposals.py` (§8).

### 7.1 Filter (no LLM): `filter.py`
Keep an obligation for a document version when:
- it is active at the document's regulation date (as today, `V_doc` / `V_now`, see §7.6), and
- its `domain` is in the document's confirmed `domains`, and
- the company's `entity_type` is in its `applies_to`.

Record the dropped count per reason in the run log.

### 7.2 Triage (LLM): `triage.py`, prompt `triage-v1`
**Input:** the confirmed profile (purpose, type, section summaries) and the filtered obligations in compact form (`id`, `statement`, `required_doc`, `evidence_type`, `location_hint`, `severity`). About 60–100 tokens per obligation; batches of up to ~80 obligations per call.

**Output:** for each obligation `relevance` (`relevant` / `possibly` / `irrelevant`), `reason` (Georgian, one sentence) and citations to the profile sections that justify it.

**Guard (code):** when `required_doc.basis == "explicit"` and the document's type matches one of `required_doc.doc_types` (normalised), the obligation cannot be `irrelevant`; the code raises it to `relevant` and logs the override (`source = rule`).

Only `relevant` and `possibly` continue. Irrelevant obligations are stored (visible in a collapsed list on the review page, with reasons) but produce no tasks.

### 7.3 Locate (LLM): `locate.py`, prompt `locate-v1`
**Input:** the section map (section_no, title, summary, goal) and the relevant/possibly-relevant obligations with their `location_hint`.
**Output:** for each obligation, up to 5 candidate `section_no`s with a one-line reason.

**Why.** The regulation describes *what kind* of section is expected, never the bank's section numbers. The section map links the two.

### 7.4 Judge (LLM): `judge.py`, prompt `judge-v2`

Two modes, chosen per document by size (§13.3):

| Mode | Used when | What the model sees |
|---|---|---|
| **whole document** | document text ≤ `mapping.whole_doc_max_tokens` | the full document (every mapped clause with its id and section number) + a batch of ~8 obligations with their source provision text (`index_text`) and the located sections |
| **candidates** | larger documents | for each obligation: the clauses of the located sections + top-8 from hybrid search (§7.5), deduplicated |

**Output per obligation:**

```json
{
  "obligation_id": "obl:...",
  "edges": [{"clause_id": "cl:...", "role": "full|partial|contradicts", "quote": "verbatim from the clause", "why": "1 sentence"}],
  "verdict": "full|partial|contradicts|none",
  "missing": "what is still missing or wrong (Georgian), null if full",
  "rationale": "1–3 sentences (Georgian)",
  "citations": [{"kind": "provision", "id": "pv:...", "quote": "..."}]
}
```

Rules in the prompt and enforced in code:
- `verdict = full` only if the listed clauses **together** satisfy every element (numbers, frequency, roles, deadlines) and none contradicts.
- Any `contradicts` edge → verdict is `contradicts`, regardless of other edges.
- Per-clause `role` is never rewritten when the verdict is `full` (the clause on its own may be partial; the verdict says the combination is enough).
- Quotes must appear verbatim in the clause text; clause ids must exist; the source provision quote must appear in its text. A failing item is retried once with the error, then stored as `invalid` and shown to the user as "could not be decided".
- User edges (§10) are given to the model as fixed facts ("the user confirmed clause X covers this") and are never removed by the model's output.

**Writing:** `COVERED_BY` edges (`source = llm`) for this document version and obligation are replaced; `source = user` edges are kept. The `coverage` row is written.

### 7.5 Hybrid search (candidate mode, Q&A, fallback)
- `src/kg/retrieval/index.py`: dense retrieval switched on. The embedding model is configurable (`retrieval.dense.model`). Candidates: BGE-M3 (local) or a Google embedding model through the same LLM layer. Choose by a small comparison in Phase 4 (§18).
- Provisions are indexed with `index_text` (§5.1). Clauses are indexed with `section heading + parent heading + clause text`.
- Queries for an obligation: `statement + location_hint + deadline_or_frequency + condition` and, separately, the source provision `index_text`; the two rankings and BM25 are fused with RRF (`retrieval.rrf_k`).
- Embeddings are cached on disk per content hash (`data/kg/indexes/`), so re-indexing unchanged text is free.

### 7.6 Cause: pre-existing or amendment (keeps the A/B idea)
For each non-full coverage row:
- `cause = amendment` when the obligation (or the obligation it `SUPERSEDES`) became valid **after** the document's regulation date (`V_doc` = the document's effective or document date). `amendment_id` = the amending act that created it (via `IMPOSES` → ProvisionVersion → `AMENDED_BY`).
- otherwise `cause = pre_existing`.
- A document with no date: `V_doc = V_now`, `cause = pre_existing`, and the profile step warns the user that no date was found.

---

## 8. Proposals and the revised document

### 8.1 One proposal per obligation (`proposals.py`, prompt `propose-v1`)
For every coverage row whose verdict is not `full`:

| Verdict | Proposal kind | Target |
|---|---|---|
| partial | `adjust_clause` | the partial clause that matters most (from the judge's edges) |
| contradicts | `adjust_clause` | the contradicting clause |
| none, the document is the right place | `add_section` | anchor clause to insert after (from locate) and a proposed heading |
| none, and triage was only `possibly` and nothing fits | `new_document` | a suggested document type |

**Output:** `original_text` (copied by code from the clause, never from the model), `proposed_text` (full new wording of the clause, or of the new section), `explanation` (Georgian, 1–3 sentences), citations (the provision with a verbatim quote and the clause).

**Grounding rule:** numbers, deadlines, frequencies and roles in `proposed_text` must come from the obligation or its source provision; the code checks that every number in the proposal appears in the obligation, the provision or the original clause, and flags the proposal otherwise.

### 8.2 Diff
`src/kg/services/compare.py` `word_diff(original, proposed)` produces the inline diff shown on the review page (deletions struck through, insertions highlighted).

### 8.3 Revised document text (`src/kg/services/revise.py`, prompt `merge-v1`)
When the user clicks **Build revised text**:
1. Take the document's clauses in order.
2. For each clause with **one** accepted `adjust_clause` proposal, use its `proposed_text`.
3. For each clause with **several** accepted proposals, call the merge prompt once: original clause + all accepted proposals → one merged text that satisfies all of them; the user sees the merged text and its diff before download. Conflicts the model reports are shown.
4. Insert accepted `add_section` proposals after their anchor clauses.
5. Produce a `.docx` (python-docx; headings and numbering from the parser's section numbers) and a `.md`. Store under `data/uploads/<company>/revised/<doc_version>-<timestamp>.docx`.

The revised text is a **draft** for the user to edit in their own editor and upload as the next version. It is not published automatically.

---

## 9. Tasks, ranking and the task board

### 9.1 One task per proposal (`src/kg/services/tasks.py`)
- **Key:** `<company_id>|<doc family id>|<lineage root of the obligation>`. The lineage root follows `SUPERSEDES` back to the first obligation, so a modified obligation updates the same task instead of creating a new one.
- **Title:** short Georgian title from the obligation statement and the proposal kind.
- Tasks for `new_document` proposals use the doc family `__company__`.

### 9.2 Ranking (`src/kg/mapping/ranking.py`, weights in `configs/kg.yaml`)

```yaml
ranking:
  severity:  {critical: 4, high: 3, medium: 2, low: 1}
  gap:       {contradicts: 1.0, none: 1.0, partial: 0.6}     # contradicts and none rank the same
  relevance: {relevant: 1.0, possibly: 0.6}
  priority_thresholds: {critical: 3.5, high: 2.4, medium: 1.2}  # score >= threshold -> bucket, else low
```

`score = severity × gap × relevance`. The amendment cause is **not** in the score (the board filters and badges it instead), so a regulator compares like with like. Changing weights recomputes scores without any LLM call.

### 9.3 Amendment information on the task
`cause`, `amendment_number` (e.g. 46/04), `amendment_effective` (date), and a **"new" badge** when the effective date is within `tasks.new_amendment_days` (config, default 180) of today. The due date defaults to the amendment's effective date or its transitional date when known; otherwise empty.

### 9.4 Lifecycle

| Status | Set by |
|---|---|
| `open` | created by the pipeline |
| `accepted` | user accepts the proposal |
| `not_relevant` | user, with reason |
| `already_addressed` | user, with chosen clauses |
| `resolved` | automatically, when a newer version of the document is checked and the obligation's verdict is `full` |
| `replaced` | automatically, when the version is replaced |

Every change writes `task_events` (actor, old/new status, data). Re-runs **update** a task (score, proposal, verdict) and never reopen a task the user closed unless the obligation itself was modified by a new amendment (then a `reopened_by_amendment` event).

### 9.5 Board (UI §14.5)
Columns by status, cards sorted by score. Filters: document, priority, cause (amendment / pre-existing), amendment number, "new" amendments, assignee, severity. Card shows: title, priority, severity, verdict, relevance, cause with amendment number and effective date, assignee. Regulated entities can assign a user or a company role and change status from the review page; regulators see the same board read-only.

---

## 10. User decisions and edits

Everything the user decides is a row in `user_decisions` and takes effect immediately in deterministic code:

| Decision | Effect |
|---|---|
| **accept** proposal | task `accepted`; proposal included in the revised text |
| **not relevant** (reason required) | task `not_relevant`; triage row overridden (`source = user`, relevance `irrelevant`) |
| **already addressed** (clauses required) | task `already_addressed`; `COVERED_BY` edges with `source = user`, role `full`; coverage verdict `full` (`source = user`) |
| **edge add / change / remove** | `COVERED_BY` with `source = user`; the coverage verdict is recomputed by a rule (any user `full` edge → `full`; user `contradicts` → `contradicts`) and the task updated. If the user wants the LLM to re-judge with the new edges, a **Re-check** button runs §7.4 for this obligation only. |
| **profile edit** | confirmed profile values; re-running triage is offered (it changes the LLM input) |
| **publish** | version status |

The regulator sees decisions on each task: who, when, reason, chosen clauses.

**Precedence:** user > rule > llm, everywhere. The pipeline reads user rows first and never writes over them.

---

## 11. Explainability: citations and links

### 11.1 Rule
Every LLM output that labels or proposes something carries `citations: list[Citation]` (schema in §5.3.2). The code:
- checks that each `id` exists and each `quote` is a verbatim substring of that node's text (after whitespace normalisation);
- fills `url` itself (never from the model):
  - provision → the act's Matsne URL (`Act.url`) plus the readable path (`display`); Matsne deep anchors are not guaranteed (§18), so the UI shows the URL and the path;
  - clause → internal link to the review page anchor (`?doc=<id>#<clause_id>`);
  - obligation → internal link to the obligation detail;
  - external act → no URL unless known.

### 11.2 Where citations are shown
- **Review page hover cards** (§14.4): explanation, the regulation quote with its link, the clause quote, the obligation statement, severity, cause and amendment.
- **Task cards:** the same in the detail panel.
- **Profile step:** the clauses that support the domain/type.
- **Triage list:** the reason and the cited section.

### 11.3 Stored with the output
Citations live inside the stored step output (§12) and are copied into the relational rows (`triage.citations`, `coverage.citations`, `proposals.citations`), so the UI does not need the LLM output to explain anything.

---

## 12. Logging and reproducibility

### 12.1 What is stored
| Store | Content |
|---|---|
| `step_outputs` (new) | the validated JSON of every LLM step, keyed by `(step, subject, input_hash, prompt_version)` |
| `llm_calls` (exists) | every request: model, tokens, latency, error, cache hit |
| `data/kg/llm_cache/` (exists) | raw responses by prompt hash (now including the prompt version) |
| `runs` + `data/kg/runs/*.jsonl` (exist) | job runs and events |
| `user_decisions`, `task_events` (new) | every human action |

### 12.2 How "no new LLM call unless needed" works
- Every LLM step computes `input_hash` = sha1 of the exact inputs it uses (texts, ids, confirmed profile values, user edges, config values that enter the prompt) and uses `prompt_version` from its prompt file header.
- Before calling the LLM, the step looks up `step_outputs` for `(step, subject, input_hash, prompt_version)`. Found → reuse. Not found → call, validate, store.
- Everything downstream of LLM outputs is pure code: filtering, guards, cause, ranking, task creation and updates, diffs, the UI. Changing weights, thresholds, filters or templates recomputes from stored outputs (`kg recompute --company X`), with zero LLM calls.
- Model changes do not invalidate outputs by themselves (the model is stored for information). To force a fresh answer, bump the prompt version or use `--force <step>`.
- The LLM cache key gains `prompt_version` so an old cached response is never reused for a new prompt.

### 12.3 Audit
For any task the UI can show the chain: task → proposal → coverage row → judge output (`step_outputs.id`) → the obligation → its extraction output → the provision version → the amendment. A `kg explain-task <key>` CLI prints the same chain.

---

## 13. LLM: Gemma on Google Cloud

### 13.1 Model ids and access
- Gemma 4 is available as `gemma-4-31b-it` and `gemma-4-26b-a4b-it` on the Gemini API (Google AI Studio), and as the managed `gemma-4-26b-a4b-it-maas` on Vertex AI (region `global`). Context window about 256K tokens.
- Sources disagree on whether the AI Studio Gemma models have a paid tier; with GCP billing, Vertex AI is the safe route. **Verify in Phase 0** (§18) which id and LiteLLM prefix work (`gemini/…` or `vertex_ai/…`), and keep the choice in `configs/kg.yaml` only.
- `llm.daily_request_limit` becomes optional (`null` = no daily limit). Rotation stays available but is off by default. Per-minute 429 handling (wait and retry) stays.

### 13.2 Structured output
Structured-output support for Gemma through LiteLLM is not confirmed. **Do not depend on a JSON-schema response mode.** Keep the current approach: ask for JSON in the prompt, extract with `extract_json`, validate with pydantic, retry once with the validation error. If schema mode works for the chosen id, enable it per step with a config flag.

### 13.3 Whole-document threshold (answer to question 2)
- Gemma 4's window (≈256K) is far larger than our documents, but answer quality drops with very long inputs and long outputs, and Georgian text costs many tokens per word.
- Default: `mapping.whole_doc_max_tokens: 30000` (document text only), with **~8 obligations per judge call** and `max_output_tokens` large enough for 8 items with edges (start at 8,192).
- Phase 0 measures the token count of the 7 corpus documents with the chosen model's tokenizer (`litellm.token_counter` or the API's count endpoint). If all fit, all use whole-document mode, which is what we expect.
- Phase 4 checks the threshold empirically: run the judge on one document in both modes and compare against gold; lower the threshold if whole-document mode misses clauses.

---

## 14. UI pages

All pages in Georgian (`app/i18n_ka.py`). Streamlit stays. Navigation depends on the user type.

### 14.1 Common
- **Login / registration** (§3.3).
- **Regulation Q&A** and **version compare** (existing pages, unchanged apart from titles from §5.1).

### 14.2 Company profile (regulated entity)
Company name, entity type (select from the `EntityType` vocabulary), registration number, contacts, **roles** (add/remove), members (list of users with their roles).

### 14.3 My documents (regulated entity)
- List of document families with their versions and statuses (draft / published / replaced).
- **Upload**: new document or new version; for a new version: keep or replace (§6.1).
- **Profile step** after parsing: shows parser warnings, the LLM profile; the user confirms or edits type, domains, purpose and section summaries; then mapping runs with a progress bar.

### 14.4 Document review (regulated entity; regulator read-only)
Split view:
- **Left:** the document text by section (from the parser), clauses with issues highlighted by verdict colour (contradicts, none-anchor, partial). **Hover** shows a card with the explanation, the obligation, the cited regulation text with its Matsne link, the cited clause quote, severity, cause and amendment number/date. Implemented as an HTML component (`streamlit.components.v1.html`) with CSS hover cards, because Streamlit widgets have no hover.
- **Right:** one card per proposal: diff (original vs proposed), explanation, citations, and the actions **Accept**, **Not relevant** (reason), **Already addressed** (pick clauses), **Edit edges** (add/remove/change role), **Re-check** (§10).
- Top bar: counts by status, **Build revised text** (download .docx/.md), **Publish** (enabled when nothing is open).
- Collapsed lists: irrelevant obligations with reasons; obligations fully covered (with their edges).
- Limitation: the HTML component cannot send clicks back to Streamlit, so clicking a highlight does not open the right-hand card; cards are labelled with the clause number and filterable. A bidirectional custom component is a later improvement.

### 14.5 Task board
- Regulated entity: own company. Regulator: chosen company, read-only.
- Columns by status (§9.4), cards sorted by score, filters (§9.5), detail panel with the full explanation chain and decision history.

### 14.6 Regulator: company search
Search by name or registration number; company page with entity type, published documents (with versions) and the task board.

### 14.7 Graph and evaluation pages
Graph page uses titles (§5.1) and shows `SUPERSEDES`, placeholders and user edges distinctly. Evaluation page shows the new metrics (§15).

### 14.8 Removed
The amendment impact page (`page_replay`).

---

## 15. Evaluation v2 and the gold sets

### 15.1 Answer to question 6: what to label
**Obligations, not documents.** The existing gold file (`answer_key_gold_labels.csv`) already labels document gaps; what we cannot measure today is extraction quality, which v2 changes most.

Label about **20 extraction units** of act 4964423, chosen to include: a definitions article, paragraphs with references, a paragraph with sub-points, one with an exception, and units changed by amendments 46/04 and 135/04. For each unit, a CSV row per obligation:

```
unit_path, statement, deontic, subject, applies_to, condition, exceptions, deadline_or_frequency,
severity, required_doc_types, required_doc_basis (explicit|inferred|none), required_doc_quote,
evidence_type, location_hint, depends_on, source_path
```

Plus, for the 6 amended units: the expected diff operation of each old obligation (unchanged / modified / new / removed). File: `evaluation/gold/obligations_v2.csv` and `evaluation/gold/amend_diff_v1.csv`. I will prepare the empty templates with the units pre-filled.

### 15.2 Metrics
| Suite | Metric |
|---|---|
| `extraction` (new) | obligation recall/precision per unit (statement matched by a judge prompt or by hand), attribute accuracy per field, `explicit` precision, diff-operation accuracy |
| `triage` (new) | for every gold A/B item, its obligation is `relevant` or `possibly` for that document (triage recall); share of obligations marked irrelevant |
| `compliance` (adapted) | the existing recall A/B, precision, C false-positive rate, version attribution — computed from tasks/coverage instead of findings; clause-level matching uses the judge's edges |
| `proposals` (new, manual) | a reviewer marks 20 sampled proposals correct / partly / wrong; groundedness flag rate |
| `ops` (exists) | requests, tokens, latency per step; reuse rate of `step_outputs` |

### 15.3 Ablations
Run the compliance suite after each phase: baseline (today) → extraction v2 → + triage → + locate → whole-document judge vs candidates. Keep the results in `evaluation/results/` as today.

---

## 16. What is removed or deferred

| Item | Status |
|---|---|
| `configs/doc_type_obligations.yaml` and category routing | removed (Phase 4) |
| `mapping/candidates.py` top-5 BM25 judging | replaced by §7 |
| `predecessor()` word-overlap matching | replaced by §5.4 |
| `issue_chance` | removed; ranking uses severity × gap × relevance |
| `findings`, `alerts` tables, `coverage-alerts` | replaced by `coverage`, `proposals`, `tasks`; old tables kept until Phase 8 for comparison |
| amendment impact page | removed from the UI; `replay` CLI removed in Phase 8 (the amendment flow is now part of `extract-obligations` + mapping) |
| cross-document coverage | deferred |
| escalation agent (old Phase 9) | not planned |

---

## 17. Roadmap

Each phase ends with tests passing (`uv run pytest tests`), the run logs clean, and an evaluation run where stated. Phases 1–2 can start in parallel after Phase 0.

### Phase 0 — Foundations
**Goal:** everything later phases need, with no behaviour change.
- `configs/kg.yaml`: `acts.<id>.domains`, `llm` block for Gemma (§13), `daily_request_limit: null`, `mapping.whole_doc_max_tokens`, `ranking`, `tasks.new_amendment_days`, prompt versions.
- Store migrations: all tables of §4.3 and §3.2; new edge props.
- `step_outputs` helper in `src/kg/llm.py` or `src/kg/steps.py`: `run_step(step, subject, inputs, prompt, schema)` that hashes inputs, looks up, calls, validates, stores (§12.2). Prompt version added to the LLM cache key.
- Citation verifier `src/kg/citations.py`: id exists, quote verbatim, url filling (§11.1).
- Smoke test of the Gemma id and LiteLLM prefix; token counts of the 7 documents (§13.3).
- *Done when:* migrations run on the current database; a mock-LLM test shows a second identical `run_step` makes no call; the Gemma smoke test returns valid JSON.

### Phase 1 — Regulation side v2
- Titles and `index_text` (§5.1); graph page labels.
- Exact references and placeholders (§5.2).
- `src/kg/extract/context.py` (§5.3.1); schema v2 and prompt `obligations_v2.md` (§5.3.2–5.3.3).
- `src/kg/extract/amend_diff.py` (§5.4); `SUPERSEDES` edges; remove `predecessor()` usage.
- Gold templates for §15.1; extraction suite.
- *Done when:* every Provision/ProvisionVersion has `title`/`display`; no `REFERS_TO` points to a trimmed parent; extraction gold metrics reported; replaying 46/04 yields a reviewed list of modified/new/removed obligations.

### Phase 2 — Accounts, companies, documents
- Tables and `services/accounts.py`, `services/documents.py` with the permission helper (§3.4).
- Upload flow with versions (§6.1), storage under `data/uploads/` (gitignored), parser bbox and new ids (§6.2).
- Pages: login/registration, company profile, my documents (§14.1–14.3).
- Migrate the 7 corpus documents into a demo company (`bank-a`) as published version 1.0.
- *Done when:* the permission matrix tests pass; a user can register, create a company, upload, see versions; a regulator user can search and open the company (empty board).

### Phase 3 — Profile, filter, triage
- `company/profile.py` (§6.3) and the profile confirmation step.
- `mapping/filter.py`, `mapping/triage.py` with the explicit guard (§7.1–7.2).
- Triage suite (§15.2).
- *Done when:* triage recall on gold items is reported and every dropped obligation has a reason.

### Phase 4 — Locate, judge, coverage
- `mapping/locate.py`, rewritten `mapping/judge.py` and `mapping/coverage.py` (§7.3–7.4, §7.6); multi-edge `COVERED_BY`; verification.
- Hybrid search with dense retrieval and `index_text` (§7.5); embedding model choice (§18).
- Compliance suite adapted to `coverage` (§15.2); remove `doc_type_obligations.yaml`.
- *Done when:* compliance metrics are reported against today's baseline (recall A 0.69, recall B 0.64, precision 0.30, C false-positive rate 0.56) and whole-document vs candidate mode is compared on at least one document.

### Phase 5 — Proposals and revised text
- `mapping/proposals.py` (§8.1) with the grounding check; diff rendering (§8.2).
- `services/revise.py` with the merge prompt and .docx/.md export (§8.3); add `python-docx` to `pyproject.toml`.
- *Done when:* 20 sampled proposals are reviewed (§15.2) and a revised .docx opens correctly.

### Phase 6 — Tasks, ranking, decisions
- `mapping/ranking.py`, `services/tasks.py` (§9), `user_decisions` handling (§10), `kg recompute`, `kg explain-task`.
- *Done when:* changing a ranking weight and running `recompute` changes scores with zero LLM calls; re-running mapping does not duplicate tasks or overwrite user decisions; uploading a corrected version auto-resolves covered tasks.

### Phase 7 — UI
- Review page (§14.4), task board (§14.5), regulator company search (§14.6), graph and evaluation pages (§14.7); remove the amendment impact page.
- *Done when:* each page renders in `AppTest` with a seeded database for both user types, and the regulator sees no action buttons.

### Phase 8 — Evaluation and clean-up
- All suites (§15.2), ablation report (§15.3), removal of old tables, `replay`, `coverage-alerts` and `findings` code paths; update `docs/DEVELOPER_WALKTHROUGH.md`.
- *Done when:* the walkthrough describes v2 and the ablation table is in `evaluation/results/`.

### New and changed files at a glance

```
configs/kg.yaml                         changed: domains, Gemma, ranking, thresholds, prompt versions
configs/doc_type_obligations.yaml       removed (Phase 4)
src/kg/steps.py                         new: run_step, input hashing, step_outputs
src/kg/citations.py                     new: verification and URLs
src/kg/store/sqlite_nx.py               changed: new tables, migrations, edge props
src/kg/ingest/jobs.py                   changed: titles, index_text, exact references, placeholders
src/kg/extract/context.py               new
src/kg/extract/obligations.py           changed: schema v2, writes applies_to edges
src/kg/extract/prompts/obligations_v2.md new
src/kg/extract/amend_diff.py            new  (+ prompts/amend_diff.md)
src/kg/company/pdf_parser.py            changed: bbox, ids
src/kg/company/ingest.py                changed: company/version ids
src/kg/company/profile.py               new  (+ prompts/doc_profile.md)
src/kg/mapping/filter.py                new
src/kg/mapping/triage.py                new  (+ prompts/triage.md)
src/kg/mapping/locate.py                new  (+ prompts/locate.md)
src/kg/mapping/judge.py                 rewritten (+ prompts/judge.md)
src/kg/mapping/coverage.py              rewritten
src/kg/mapping/proposals.py             new  (+ prompts/propose.md)
src/kg/mapping/ranking.py               new
src/kg/mapping/candidates.py, alerts.py, labels.py   removed or reduced
src/kg/retrieval/index.py               changed: dense on, index_text, clause text
src/kg/services/accounts.py             new
src/kg/services/documents.py            new
src/kg/services/tasks.py                new
src/kg/services/revise.py               new  (+ prompts/merge.md)
src/kg/services/replay.py, reports.py   removed (Phase 8)
src/kg/eval/extraction.py, triage.py, proposals.py   new; compliance.py adapted
src/kg/cli.py                           changed: new commands (recompute, explain-task, profile, map, tasks)
app/kg_app.py                           split into pages: auth, company, documents, review, board, search, qa, compare, graph, eval
app/i18n_ka.py                          new strings
tests/                                  new tests per module; permission matrix; reproducibility tests
evaluation/gold/obligations_v2.csv, amend_diff_v1.csv   new (templates, labelled by the team)
```

---

## 18. Open items to verify during implementation

1. **Gemma access:** which model id and LiteLLM prefix work with your GCP project (Vertex `gemma-4-26b-a4b-it-maas` or AI Studio `gemma-4-31b-it`), the maximum output tokens, and whether a JSON-schema response mode works. Phase 0 smoke test.
2. **Georgian token counts** of the corpus documents with the chosen model (sets `whole_doc_max_tokens`).
3. **Embedding model** for Georgian: BGE-M3 locally vs a Google embedding model; pick by candidate recall on the gold items in Phase 4.
4. **Matsne deep links:** whether article anchors exist in Matsne URLs; if not, link to the document and show the provision path.
5. **python-docx** output with Georgian fonts (check rendering in Word and LibreOffice).
6. **Streamlit hover cards** inside `components.html`: check that links open in a new tab and that long cards do not overflow the iframe.

Sources for §13: [Gemma 4 with the Gemini API](https://www.philschmid.de/gemma-4-gemini-api), [Gemma 4 API tutorial: Vertex AI and Google AI Studio](https://cloudinsight.cc/en/blog/gemma-4-api-tutorial).
