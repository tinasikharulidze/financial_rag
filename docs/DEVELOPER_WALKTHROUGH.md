# Developer walkthrough: the Regulatory Knowledge Graph (`src/kg`, `app`, `configs`, `tests`)

*Written 2026-10-19. It describes the code as it stands in the repository now, and the data it produced in the real runs of today. For the plan this follows, see `docs/KG_ARCHITECTURE.md`. For the short summary and the list of deviations, see `docs/IMPLEMENTATION_REPORT.md`. This document goes deeper: file by file, in the order things were built, and in the order they run when you use the product.*

---

## Contents

1. [The product in one page](#1-the-product-in-one-page)
2. [Repository map: what was there, what I added](#2-repository-map)
3. [Build chronology: phase by phase](#3-build-chronology)
4. [File-by-file reference](#4-file-by-file-reference)
5. [The data model: nodes, edges, tables](#5-the-data-model)
6. [Where every output file lives](#6-where-every-output-file-lives)
7. [The product flow: what runs, in what order](#7-the-product-flow)
8. [The LLM layer in detail](#8-the-llm-layer-in-detail)
9. [Evaluation: what it measures and where we stand](#9-evaluation)
10. [Tests](#10-tests)
11. [Current state of the data (today's real run)](#11-current-state-of-the-data)
12. [Known limits and what to look at next](#12-known-limits-and-next-steps)
13. [Command cheat sheet](#13-command-cheat-sheet)

---

## 1. The product in one page

**What it is.** A tool for a Georgian commercial bank's compliance team. It reads the National Bank of Georgia (NBG) regulations from Matsne (the official legal portal), including every amendment they ever received. It turns them into a **time-aware knowledge graph**: for any date you can say what the text of any provision was. From the model-risk regulation (act **4964423**) it extracts **obligations** (what a bank must do). It then compares those obligations with the bank's **internal documents** (policy, model inventory, validation report, monitoring report, ethics guideline, audit report, NBG consent application) and reports **where the documents fall short**, how likely the problem is, how severe it is, and what to change.

**The idea that makes it work.** Regulations change. A document written in 2021 can have been compliant then and be non-compliant now. So every finding carries a *version basis*:

| Class | Meaning | Compared against |
|---|---|---|
| **B** | The document already failed the rules that were in force when it was written. | the regulation version at the document's date (`V_doc`) |
| **A** | The document met the old wording, but an amendment changed or added the rule and now it fails. | today's regulation version (`V_now`) |

**Two halves.**

```
OFFLINE (run once, or when data changes; uses Gemini)        ONLINE (what a user touches; Streamlit UI or CLI)
─────────────────────────────────────────────────────        ───────────────────────────────────────────────
Matsne HTML ─► parse acts ─► parse amendments ─►              UC1  ask a question, get a cited answer "as of" a date
               consolidate (version history) ─► refs          UC2  compare two dates of one act
Obligations (LLM) ─┐                                          UC3  replay an amendment: what does it mean for us?
Company PDFs ──────┼─► map-coverage (LLM judge) ─► findings   UC4  alerts: obligations no document covers
                   └─► coverage-alerts                        UC5  review the findings of an existing document
                                                              UC6  upload a new PDF and check it
```

Everything is stored in **one SQLite file**, `data/kg/kg.sqlite` (the graph, findings, alerts, run history, LLM usage). Nothing is deleted from it when the law changes; old versions are closed with a `valid_to` date.

**Language.** The regulation, the documents and the UI are Georgian. LLM prompts are in English (more reliable) and ask for Georgian rationales.

---

## 2. Repository map

Legend: **[existing]** was in the repo before this work, **[new]** I created it, **[generated]** a program writes it, **[gitignored]** is not committed.

```
financial_rag/
├── README.md                          [existing]
├── pyproject.toml                     [edited] added networkx, pdfplumber, pydantic, pyvis, pyyaml, rank-bm25; pytest (dev)
├── uv.lock                            [existing] NOT regenerated yet -> run `uv lock`
├── .gitignore                         [edited] ignores data/kg/ and .pytest_cache/
├── .streamlit/config.toml             [existing] theme for the UI
│
├── configs/                           [new]
│   ├── kg.yaml                        all settings: paths, acts, LLM models, batch sizes, retrieval
│   └── doc_type_obligations.yaml      which obligation categories each document type must address
│
├── src/
│   ├── chat_db.py, chatbot.py, check.py, chunker.py, generate.py   [existing] prototype RAG; I only reuse chunker.py helpers
│   ├── embed/, evaluation_set/, scrapers/                          [existing] earlier prototype work
│   └── kg/                            [new] THE KNOWLEDGE-GRAPH PACKAGE
│       ├── config.py  ids.py  runlog.py  llm.py  cli.py
│       ├── store/      base.py  sqlite_nx.py
│       ├── ingest/     texttools.py  act_parser.py  amendment_parser.py  consolidate.py  jobs.py
│       ├── extract/    obligations.py  prompts/obligations.md
│       ├── company/    classify.py  pdf_parser.py  ingest.py
│       ├── retrieval/  index.py
│       ├── mapping/    labels.py  candidates.py  judge.py  coverage.py  alerts.py
│       ├── services/   qa.py  compare.py  doccheck.py  replay.py  reports.py
│       └── eval/       matching.py  compliance.py  consolidation.py  ops.py  runner.py
│
├── app/                               [new]
│   ├── kg_app.py                      Streamlit UI (7 Georgian pages)
│   └── i18n_ka.py                     Georgian strings and enum translations
│
├── tests/                             [new] 23 tests
│
├── matsne_data/                       [existing] raw Matsne data; graph_docs/ holds the amendment inputs
│   └── graph_docs/cross_references.csv   [REWRITTEN by extract-refs, keeping its header]
├── model_risk_company_docs/           [existing] the 7 bank PDFs + answer_key_gold_labels.csv + answer key PDF
├── evaluation/                        [existing] old prototype eval files
│   └── results/                       [generated] one folder per eval run + index.csv
├── docs/
│   ├── KG_ARCHITECTURE.md             [existing] the plan
│   ├── IMPLEMENTATION_REPORT.md       [new] short report
│   └── DEVELOPER_WALKTHROUGH.md       [new] this file
│
└── data/kg/                           [generated, gitignored]
    ├── kg.sqlite                      the database
    ├── parsed/{acts,amendments,consolidation}/   JSON debug/audit copies of parsing
    ├── runs/<run_id>.jsonl            one log per job run
    ├── llm_cache/<sha1>.json          one file per LLM answer
    ├── reports/<run_id>.{md,json}     replay (impact) reports
    ├── _to_delete/                    leftovers for you to delete
    └── kg.sqlite-journal              zero-byte leftover, safe to delete
```

**Rule I followed:** everything runs from the repository root and writes only inside it. The only file outside it that code touches is the secrets file named in `FINRAG_ENV_FILE` (default `../.env`), and only through `python-dotenv`, so I never read the key.

---

## 3. Build chronology

I built it in the phase order of `docs/KG_ARCHITECTURE.md`. Each phase rests on the previous one. The "when verified" column says what I could and could not check at that point.

| Phase | What I built | Main files | How it was verified |
|---|---|---|---|
| **0 Foundations** | Settings loader, ID/path conventions, SQLite+NetworkX store, run logging, LLM layer with model rotation and cache, CLI skeleton | `config.py`, `ids.py`, `runlog.py`, `llm.py`, `store/*`, `cli.py`, `configs/kg.yaml` | Unit tests; rotation simulated with a fake backend |
| **1 Acts and versions** | Parse 4 acts; parse 23 amending acts into typed operations; replay them to build version history; extract cross-references | `ingest/*` | Real corpus; 13 structural assertions pass; 117 instructions parsed, 105 by rules, 12 unsupported, 0 unresolved |
| **2 Obligations** | Cut act 4964423 into extraction units; LLM turns each into obligations with category and severity | `extract/*` | First with a mock LLM, then real Gemini: **145 obligations**, 14 units with no obligation, 0 failures |
| **3 Company documents** | PDF parser (headers, tables, numbered clauses, signatures), document classifier, graph writer | `company/*` | All 7 real PDFs parsed |
| **4 Mapping and alerts** | Per-document retrieval of candidate clauses, LLM judge, A/B classification, `COVERED_BY` edges, uncovered-obligation alerts | `retrieval/*`, `mapping/*` | Mock first; then real: **7 docs, 451 obligations judged, 290 findings, 40 requests** |
| **5 Services** | Q&A with citations, version compare, document check, amendment replay, impact reports | `services/*` | Mock LLM in tests; replay refused until mapping exists |
| **6 Evaluation** | Matching findings to the 66 gold items; consolidation assertions; ops metrics | `eval/*` | Consolidation and ops real; compliance real after mapping (see §9) |
| **7 UI** | Georgian Streamlit app with 7 pages | `app/*` | Rendered page by page with Streamlit's `AppTest` |

**What happened between "built" and "worked"** (real-API lessons that changed the code):

1. **Truncated JSON.** Gemini's hidden "thinking" tokens ate the output cap, so answers were cut off. Fix: `reasoning_effort: low`, a 32,768-token cap, and no retry on a truncated answer (a retry would be cut the same way).
2. **Retired model ids.** Two ids returned 404. Fix: a "model unavailable" error now marks the model exhausted for the day and moves to the next one; the dead ids were removed from the config.
3. **503 "high demand".** Fix: a 503 is not charged to the quota (the count is refunded) and the model is skipped after 2 failures.
4. **Quota day.** Google resets quotas at **Pacific midnight (about 11:00 in Tbilisi)**, not Tbilisi midnight. Fix: `quota_day()` uses `America/Los_Angeles`.
5. **Bare JSON lists.** Models sometimes answered `[...]` instead of `{"results": [...]}`. Fix: accepted in `_validate`.
6. **Replay before mapping.** Running `replay` before `map-coverage` had finished burned quota and wrote partial findings. Fix: `replay` refuses to run until `COVERED_BY` edges exist.

---

## 4. File-by-file reference

Each entry gives: **purpose**, **how it works**, **reads / writes**, **what you see from it**.

### 4.1 Configuration

#### `configs/kg.yaml`
The single place for settings. All paths are relative to the repo root.
- `paths`: where inputs are (`raw_html`, `amendment_docs`, `amendment_edges`, `company_docs`, `gold_labels`) and where outputs go (`db`, `runs`, `reports`, `llm_cache`, `eval_results`).
- `acts`: the four acts and their mode. **versioned** (original text plus amendments are replayed): 2060636 (capital adequacy), 5679194 (expected credit loss), 4964423 (model risk). **snapshot** (latest consolidated text only, dated 2025-02-27): 4822603 (lending to individuals).
- `obligation_acts: ["4964423"]`: obligations are extracted from this act only. `entity_type: commercial_bank`.
- `llm`: the model list in the order tried (today: `gemini/gemini-3.6-flash`, `gemini/gemini-3.5-flash`, `gemini/gemini-3.5-flash-lite`, `gemini/gemini-2.5-flash`), `daily_request_limit: 20`, `temperature: 0`, `max_output_tokens: 32768`, `reasoning_effort: low`, `retries_on_invalid_json: 1`.
- `extraction.obligation_batch: 10` (units per LLM call), `mapping.judge_batch: 12` (obligations per LLM call), `mapping.top_k_clauses: 5`.
- `retrieval`: BM25 settings. Dense vectors (BGE-M3) and a reranker exist in code but are **disabled**.

**To switch the model**, edit the list. Ids use the LiteLLM form `gemini/<name>`, same as `src/chatbot.py`. Nothing else changes.

#### `configs/doc_type_obligations.yaml`
Hand-written table: which obligation **categories** each **document type** must address. Example: a `validation_report` must address `validation, development, explainability, monitoring, outsourcing`; an `inventory` must address `inventory, supervisory, validation, outsourcing`. Type `other` maps to nothing and is skipped. This table decides *which obligations are judged against which document*; it is what keeps the number of LLM requests small (40 instead of hundreds).

### 4.2 Foundations (`src/kg/`)

#### `config.py`
Loads `kg.yaml` (override with env var `KG_CONFIG`). `Config.path("db")` resolves a path against the repo root; `Config.get_in("llm.models")` reads nested keys. Also the Tbilisi clock (`now_iso()`, `today_iso()`, UTC+4, no daylight saving) used for every timestamp.

#### `ids.py`
The naming rules for everything in the graph. Georgian legal numbering is awkward (superscripts like 4¹, letter sub-points like ბ.ე, ordinals like "მე-5"), so this file makes it uniform.
- **Provision paths:** `art6` (article 6), `art9_1` (article 9¹), `art6/p4` (paragraph 4), `art6/p4.1` (paragraph 4¹), `art6/p5/sბ.ე` (nested sub-point), `ordN` (article of the approving order), `annexN`.
- **Node ids:** `act:<doc>`, `amend:<doc>`, `prov:<act>/<path>`, `pv:<act>/<path>@<date>`, `obl:<act>/<path>#<n>@<date>`, `ent:<name>`, `ext:<key>`, `doc:<code>@<ver>`, `cl:<doc>/<section>`.
- Functions: `parse_ordinal` / `find_ordinals` (Georgian ordinal words and numbers), `art_seg`/`para_seg`/`sub_seg`/`join_path` (build paths), `path_sort_key` (sort 4, 4¹, 4², 5 correctly), `display_path` (path back to readable "Art. 6 para 4¹").
- **Why it matters:** the same path is what the gold file, the amendment parser and the UI all talk about, so a finding can be traced back to one exact paragraph.

#### `runlog.py`
Every CLI job runs inside `with Run(...)`. It inserts a row in the `runs` table, creates `data/kg/runs/<kind>-<date>-<time>-<hex>.jsonl`, and logs events (`run_start`, `doc_plan`, `doc_done`, `error`, `run_end` ...). Status becomes `ok`, `failed`, or `partial` (budget ran out). *You look here when something goes wrong or when you want to know what a run did.*

#### `llm.py` (see also §8)
The only place that talks to Gemini. One call: `LLM.call(purpose, messages, schema)`. It handles the cache, model choice and rotation, quota counting, JSON validation, and writes every call to the `llm_calls` table.

#### `cli.py`
The command line for every job. Run from the repo root as `uv run python src/kg/cli.py <command>`. It opens the config and the store, wraps the job in a `Run`, and prints a JSON result with the run id and log path. If the LLM budget runs out it exits with code 2 and tells you to re-run later (finished work is cached). Commands are listed in §13.

### 4.3 Storage (`src/kg/store/`)

#### `store/base.py`
`GraphStore`, an abstract interface (`upsert_node`, `upsert_edge`, `get_node`, `neighbors`, `nodes`, `as_of`, `subgraph`). Services depend on this, not on SQLite, so a graph database such as Neo4j could replace it later.

#### `store/sqlite_nx.py`
`SQLiteStore`: SQLite is the source of truth, NetworkX is a traversal view built on demand. It follows the `src/chat_db.py` conventions you asked for: stdlib `sqlite3` only, one `SCHEMA` string run with `executescript` in `__init__`, a `_tx()` context manager per operation (no long-lived connections, so the UI and a CLI job can run side by side), light migrations through `PRAGMA table_info`, parameterised queries only, JSON columns written with `ensure_ascii=False` so Georgian stays readable, Tbilisi timestamps.

Tables: `nodes`, `edges`, `findings`, `alerts`, `runs`, `llm_calls`, `llm_quota` (see §5). Useful methods:
- graph: `upsert_nodes/edges` (bulk), `nodes(type, as_of=date)` (only what is valid on that date), `neighbors(id, edge_type, direction)`, `edges(type)`, `delete_edges(type, src, dst, dst_prefix)`, `as_of(date)` (a NetworkX graph of that date).
- bookkeeping: `start_run/finish_run`, `log_llm_call`, `quota_used/inc/dec/mark_exhausted`.
- results: `add_findings`, `clear_findings(doc_id, obligation_ids)`, `findings(doc_id, run_id)`, `add_alert` (returns False if the same alert already exists), `alerts()`, `acknowledge_alert`.

To inspect by hand: `sqlite3 -header -csv data/kg/kg.sqlite "SELECT ... FROM findings"` (examples are in the file's docstring).

### 4.4 Phase 1: acts and versions (`src/kg/ingest/`)

The goal of this folder is to answer: *what did article X say on date D?* without storing a full copy of the act for each date.

#### `ingest/texttools.py`
Shared helpers: regexes that recognise a paragraph label (`4.`, `4^1.`), a sub-point label (`ა)`), a nested label (`ბ.ე)`), an article start (`მუხლი 6`), a chapter start. `segment_lines()` cuts a flat list of lines into new provisions with paths. `NewProv` is the little record it produces.

#### `ingest/act_parser.py`
`parse_act_html(html, doc_id)` turns a Matsne HTML act into `ParsedProvision` rows, one per article, paragraph and sub-point, each with a stable path, label, heading, text and chapter. It reuses the element-walking helpers of `src/chunker.py`, but, unlike the prototype chunker, it also reads the SUBPOINT elements (act 2060636 keeps its lettered lists there; the chunker silently dropped them). For the snapshot act (4822603) it strips the trailing amendment notes ("ბრძანება №193/04 - ვებგვერდი, ...").

#### `ingest/amendment_parser.py` (the largest file, 621 lines)
Turns an amending act into machine-readable operations. An amending act's Article 1 is a list of numbered instructions like "Article 5, paragraph 10: replace with the following text: „...“". Three steps:
1. `extract_amendment` / `split_instructions`: read the HTML, split Article 1 into instructions (it tracks Georgian quote marks „ “ so a quoted new text is never confused with the next instruction), read the final articles for the **effective date** and transitional provisions.
2. `interpret` → `ParsedInstruction` with an operation: **REPLACE**, **INSERT**, **REPEAL**, **TEXT_SUB** (replace words inside a paragraph), plus chapter operations and title changes. The target comes from the Georgian ordinals in the head sentence ("მე-5 მუხლის მე-10 პუნქტის შემდეგ ... 10¹–10³"); the paths of *new* provisions come from the labels in the quoted text.
3. `llm_fallback`: only for instructions the rules cannot resolve (purpose `amend_parse`). It was never needed on the real data (0 unresolved).
Tables, annexes and chapter titles are not modelled; such instructions are marked `unsupported` (12 of 117), not hidden.

#### `ingest/consolidate.py`
`VersionedAct` keeps, for each provision path, a list of `Version` records (`valid_from`, `valid_to`, `status`, `text`, label, heading ...). `apply(instruction, date, amendment)` performs one operation: it closes the current version of the target (`valid_to = date`) and opens a new one. Repeal does **not** delete: it adds a final version with `status="repealed"`. `as_of(date)` assembles the text on demand; no consolidated full text is stored.

#### `ingest/jobs.py`
The four Phase-1 jobs that the CLI calls:

| Job | What it does | Writes |
|---|---|---|
| `parse_acts` | parses each configured act's raw HTML, builds the base version, writes Act / Provision / ProvisionVersion nodes | `data/kg/parsed/acts/<doc>.json`; graph |
| `parse_amendments` | parses each amendment into typed instructions; creates `AmendingAct` nodes and `AMENDS` edges | `data/kg/parsed/amendments/<doc>.json` (23 files); graph |
| `consolidate` | replays all instructions of an act in date order | `data/kg/parsed/consolidation/<doc>.json` (log + unresolved list); new ProvisionVersions, `UPDATES` (new version → the one it replaced) and `AMENDED_BY` (version → amending act) edges |
| `extract_refs` | finds references in provision text: to other provisions of the act, to other acts in the corpus by order number, to external laws by title | `REFERS_TO` edges, `ExternalAct` nodes, and **rewrites `matsne_data/graph_docs/cross_references.csv`** keeping its header |

Dates: V0 of act 4964423 is 2021-01-01 (from config), V1 = 2022-05-03 (amendment 5451815, order 46/04), V2 = 2023-07-01 (amendment 5840572, order 135/04). The gold file says V1 is 02.05.2022; this deviation is documented in the implementation report.

### 4.5 Phase 2: obligations (`src/kg/extract/`)

#### `extract/prompts/obligations.md`
The system prompt. Tells the model: for each provision unit, give a `category` (governance, policy, inventory, development, validation, monitoring, explainability, ethics, audit, outsourcing, supervisory, launch, non_obligation), and list every distinct duty with `statement` (Georgian, checkable), `subject`, `action`, `condition`, `deadline_or_frequency`, `severity`, `source_path`. It includes the **severity rubric**: critical (supervisory consent or reporting to NBG, independence of the lines of defence, launching an unvalidated model, explicit sanctions), high (validation, monitoring, data quality, inventory completeness), medium (documentation, proportionality, ethics, explainability techniques), low (definitions, references).

#### `extract/obligations.py`
- `build_units`: groups provisions into **extraction units**: a paragraph together with its sub-points (so "must cover: a) b) c)" stays together); an article without paragraphs with its direct sub-points. A unit has a new *version* whenever any member provision changes.
- `extract_obligations`: batches the units (10 per call), calls the LLM (purpose `extract_obligations`), and writes for each duty an `Obligation` node `obl:4964423/<unit>#<n>@<date>` valid over the unit-version span, an `IMPOSES` edge from the provision version that contains it, an `APPLIES_TO` edge to `ent:commercial_bank`, and an `ExtractionUnit` node recording whether the unit produced obligations.
- **Carry-forward:** an obligation is valid from its unit version's start to the next version's start. An unchanged unit therefore needs no copy for V1 or V2. Only changed units are re-extracted. This is why V1 and V2 cost few requests.
- Result today: 145 obligations, 76 ExtractionUnit nodes, 14 non-obligation units, 0 failures.

### 4.6 Phase 3: company documents (`src/kg/company/`)

#### `company/classify.py`
Decides the `doc_type` (policy, inventory, validation_report, monitoring_report, ethics_guideline, audit_report, supervisory_application, other). First a regex on the Georgian title (the first rule that matches wins); if nothing matches, an LLM fallback.

#### `company/pdf_parser.py`
One parser for both the offline corpus and UC6 uploads. Using `pdfplumber`, in this order: (1) remove repeated headers/footers; (2) turn tables into `table` units (or one unit per row for tables over 15 rows), and cover metadata tables on page 1; (3) numbered lines (`1.2.3`) start `clause` units, bold larger numbered lines are `heading`s; un-numbered text continues the current unit; (4) signature blocks become `signature` units; (5) text before the first heading yields title, document code, version, dates, company. An LLM structure fallback exists (one call per document) and triggers only when fewer than 5 units are found or one unit is over 3,000 characters.

#### `company/ingest.py`
`write_doc` stores an `InternalDoc` node (`doc:POL-RM-014@1.0`, with title, doc_type, dates, file, sha1, warnings, `source` = `corpus` or `upload`) and one `Clause` node per unit (`cl:POL-RM-014@1.0/5.3.2`) with `HAS_CLAUSE` edges. Re-ingesting a document replaces its clauses (idempotent). Only `clause`, `paragraph` and `table` units are *mapped* against obligations. `ingest_company_docs` reads `model_risk_company_docs/D*.pdf` and **never** the answer key (`00_*` and the gold CSV).

### 4.7 Retrieval (`src/kg/retrieval/index.py`)
`HybridIndex`: BM25 over **character 4-grams** (Georgian suffixes change word forms, so word tokens miss; 4-grams survive). It supports optional dense vectors and a cross-encoder reranker, fused with reciprocal-rank fusion (`rrf`), but both are disabled in the config. `build_index` builds one index per set of items: per document's clauses (mapping), per date's provision versions (Q&A). `maybe_rerank` does nothing when the reranker is off.

### 4.8 Phase 4: mapping (`src/kg/mapping/`)

#### `labels.py`
Risk labels. `priority(severity, issue_chance)`: priority equals the obligation's severity, lowered one level **only** if the chance is low. Also Georgian display names (`KA`) and `sort_rank`.

#### `candidates.py`
`doc_index(cfg, store, doc_id)` builds the index of one document's mappable clauses. `candidates_for(index, obligation, k)` returns the top-5 clauses (text capped at 900 characters) for one obligation.

#### `judge.py`
The LLM judge (purpose `judge_coverage`). For each obligation it gets the obligation, the regulation text it came from and up to 5 candidate clauses, and answers: `coverage` (full / partial / contradicts / none), `best_clause_id`, `issue_chance` (very_high … low), `rationale` (Georgian), `evidence_quote` (verbatim from the clause), `suggested_fix` (Georgian), `action_type` (ADJUST_SECTION, ADD_SECTION, NEW_DOCUMENT). `judge_batch` validates the answer with pydantic (unknown coverage values become "none"). The prompt tells the model to compare numbers, frequencies, deadlines and roles literally and not to invent problems.

#### `coverage.py` (`map-coverage`, the core of the product)
For each `InternalDoc`:
1. `category_map` → the categories this doc type must address; skip docs whose type has none.
2. `doc_versions` → `V_doc` (the document's own date, or its effective date, never earlier than the first obligation) and `V_now` (today).
3. `active_obligations(V_doc)` and `active_obligations(V_now)` → the two sets to judge. Distinct obligations are judged **once**, not twice.
4. `judge_all` → batches of 12 obligations, each with its top-5 candidate clauses, to the judge.
5. `classify` (a pure function, unit-tested): an issue at `V_doc` that still matters today → **class B**, basis `V_doc`; an obligation that is new or changed since `V_doc` and not met → **class A**, basis `V_now`; if the earlier wording was already broken (the predecessor is a B) it is not counted again; compliant → no row. `predecessor()` matches a new obligation to the older one in the same unit by word overlap (Jaccard ≥ 0.25).
6. `write_covered_by` → `COVERED_BY` edges (obligation → best clause, with the coverage level) for full, partial and contradicting clauses.
7. Save findings. At the end, `new_document_findings` adds findings for obligation categories that **no** document type is expected to address (`doc_id = __company__`, action `NEW_DOCUMENT`).
`--dry-run` only prints the number of requests it would need. The function also accepts `only_obligations`, `now` and `links` (used by replay).

#### `alerts.py` (`coverage-alerts`, UC4)
Active obligations (today) that have no `COVERED_BY` edge with coverage full or partial from any corpus document (uploads are ignored) become `uncovered_obligation` alerts. Alerts whose obligation has since become covered are acknowledged automatically. No LLM is used.

### 4.9 Phase 5: services (`src/kg/services/`)

| File | Use case | How it works |
|---|---|---|
| `qa.py` | **UC1** question → cited answer | Builds (and caches) an index of the provision versions valid on the chosen date; retrieves the top 30; `expand()` adds each hit's parent article and the provisions it refers to (`REFERS_TO`), up to 12 passages; the LLM (purpose `qa_answer`) must answer only from those passages and cite them; citations that are not among the passages are dropped and reported in `dropped_citations`. |
| `compare.py` | **UC2** two dates of one act | `versions(store, act, date_a, date_b)` reads each date's state and classifies every path as added / removed / modified, with a word-level diff. `summarize()` (optional, LLM) writes a 5–10 sentence Georgian summary. |
| `doccheck.py` | **UC5 / UC6** | `check_existing` returns the stored findings of a document, enriched with obligation text and clause. `check_upload` parses a PDF, writes it as an `InternalDoc` with `source="upload"`, runs `map_coverage` for that document only, and returns its findings. |
| `replay.py` | **UC3** amendment impact | See §7.4. |
| `reports.py` | impact reports | `render_markdown`, `save` (writes `.md` and `.json`), `list_reports`, `get`. |

### 4.10 Phase 6: evaluation (`src/kg/eval/`)

| File | What it does |
|---|---|
| `matching.py` | Decides whether a finding matches a gold item. Same document; the clause's section number is a prefix match of the gold section; the article overlaps down to paragraph level after normalising (`Art. 6 para 4¹` → `art6/p4.1`). If the finding has no clause, or the gold section has no number (a table row), the match is `article_only`; if the gold item has no article, `section_only`. |
| `compliance.py` | Reads `answer_key_gold_labels.csv` (66 items: 35 class A, 22 class B, 9 class C), matches findings, computes the metrics in §9 and the per-item table; `replay_check` measures what share of each amendment's gold A items appear in that amendment's impact report. |
| `consolidation.py` | Parse statistics and 13 structural assertions (e.g. versions of a path do not overlap, repealed provisions are kept, the effective dates are as expected). No external gold needed. |
| `ops.py` | Calls, cache hit rate, tokens, p50/p95 latency per purpose and per run kind, from `llm_calls` and `runs`. |
| `runner.py` | `run_suite` runs one suite and writes `evaluation/results/<run_id>/{metrics.json, items.csv, report.md}` plus a row in `evaluation/results/index.csv`. The `qa` suite is a stub ("deferred: no gold set yet"). |

### 4.11 Phase 7: the UI (`app/`)

#### `app/i18n_ka.py`
All Georgian strings (`T`), and translations of enum values (priority, chance, coverage, action, doc type, gap class, version basis) plus the priority colours.

#### `app/kg_app.py`
Run with `uv run streamlit run app/kg_app.py`. A sidebar menu with 7 pages:

| Page (Georgian) | Function | What you do and see |
|---|---|---|
| კითხვა-პასუხი | `page_qa` | Pick an act (or all) and a date, ask a question, get an answer with citations that show the provision text valid on that date. |
| ვერსიების შედარება | `page_compare` | Pick an act and two dates, see added / removed / modified provisions with highlighted word diffs, optionally an LLM summary. |
| ცვლილების ანალიზი | `page_replay` | Pick an amendment, run the analysis (or open the saved report): changed provisions, new / changed / removed obligations, affected documents and clauses sorted by priority, new alerts. |
| დოკუმენტის შემოწმება | `page_doccheck` | Either choose one of the 7 stored documents and see its findings with filters, or upload a PDF (progress bar: parse → index → judge) and see its findings. Company-level (`NEW_DOCUMENT`) findings are shown too. |
| შეტყობინებები | `page_alerts` | The open alerts (obligations no document covers), each with an acknowledge button. |
| გრაფი | `page_graph` | Pick an obligation; an interactive pyvis graph shows the provision versions that impose it and the clauses that cover it (edge label = coverage). Rendered in memory, no files written. |
| შეფასება | `page_eval` | Latest metrics per suite from `index.csv`, and charts of LLM calls, latency and tokens. |

Any LLM action in the UI goes through `with_llm(...)`, which creates a `Run`, an `LLM`, and shows the "daily limit reached" message instead of crashing when the budget is exhausted.

### 4.12 Tests (`tests/`)
See §10.

---

## 5. The data model

### 5.1 Node types (in table `nodes`)

| Type | One per | Id example | Key properties | Valid period |
|---|---|---|---|---|
| `Act` | act in the corpus | `act:4964423` | number, title, adopted, mode, short | from base date |
| `AmendingAct` | amendment | `amend:5451815` | number (46/04), effective_from, transitional note, base_act, seq | from effective date |
| `Provision` | stable location | `prov:4964423/art6/p4.1` | act, path, level | permanent |
| `ProvisionVersion` | text valid for a period | `pv:4964423/art6/p4.1@2022-05-03` | text, label, heading, status (active / repealed), change_type | `valid_from` … `valid_to` |
| `Obligation` | duty in a unit version | `obl:4964423/art6/p5#2@2022-05-03` | statement, subject, action, condition, deadline_or_frequency, severity, category, source_path | unit-version span |
| `ExtractionUnit` | unit version | `unit:4964423/art6/p5@2022-05-03` | status, category, n_obligations, text_hash | unit-version span |
| `EntityType` | entity type | `ent:commercial_bank` | name | – |
| `ExternalAct` | cited act outside the corpus | `ext:law:...` | number / title | – |
| `InternalDoc` | company document | `doc:POL-RM-014@1.0` | title, doc_type, doc_date, file, source | doc date |
| `Clause` | unit of a document | `cl:POL-RM-014@1.0/5.3.2` | text, section_no, heading, unit_type, page, order | doc date |

### 5.2 Edge types (in table `edges`)

`HAS_PROVISION` (Act → Provision), `VERSION_OF` (ProvisionVersion → Provision), `UPDATES` (new version → the version it replaced), `AMENDED_BY` (version → AmendingAct), `AMENDS` (AmendingAct → Act), `REFERS_TO` (provision → provision / act / external act), `IMPOSES` (provision version → obligation), `APPLIES_TO` (obligation → entity type), `HAS_CLAUSE` (InternalDoc → Clause), `COVERED_BY` (obligation → clause; property `coverage` = full / partial / contradicts).

### 5.3 Result and bookkeeping tables (not graph edges)

| Table | Content |
|---|---|
| `findings` | One row per gap: `doc_id`, `clause_id`, `obligation_id`, `version_basis` (V_doc / V_now), `gap_class` (A / B), `action_type`, `coverage`, `issue_chance`, `severity`, `priority`, `rationale`, `evidence_quote`, `suggested_fix`, `run_id` |
| `alerts` | `kind` (`uncovered_obligation`), `obligation_id`, `message`, `acknowledged` |
| `runs` | one row per job: kind, params, started / finished, status |
| `llm_calls` | one row per LLM attempt: purpose, model, tokens, latency, cache_hit, error |
| `llm_quota` | requests per (model, quota day), and an `exhausted` flag |

---

## 6. Where every output file lives

| Created by | Files | Location | What it shows you |
|---|---|---|---|
| every job | run log | `data/kg/runs/<kind>-<YYYYMMDD>-<HHMMSS>-<hex>.jsonl` | step-by-step events and counters of that run |
| `consolidate` | unresolved list (only if any) | `data/kg/runs/<run_id>_unresolved_<act>.json` | instructions that could not be applied (act 2060636 has some) |
| `parse-acts` | parsed base acts | `data/kg/parsed/acts/<doc>.json` | every provision with its path, label, text |
| `parse-amendments` | parsed amendments | `data/kg/parsed/amendments/<doc>.json` | per amendment: effective date, transitional note, each instruction with its operation, target paths and status |
| `consolidate` | consolidation logs | `data/kg/parsed/consolidation/<doc>.json` | what each instruction touched, with warnings |
| `extract-refs` | cross-reference table | `matsne_data/graph_docs/cross_references.csv` | which act cites which (rewritten, header kept) |
| any LLM job | answer cache | `data/kg/llm_cache/<sha1>.json` | one file per distinct prompt; re-runs reuse them for free |
| all graph jobs | the database | `data/kg/kg.sqlite` | everything above, in tables |
| `replay` | impact report | `data/kg/reports/<run_id>.md` and `.json` | see §7.4 (currently empty: no replay has completed yet) |
| `eval` | results | `evaluation/results/<run_id>/metrics.json`, `items.csv`, `report.md`; row in `evaluation/results/index.csv` | the metrics, one row per gold item, a readable report |

---

## 7. The product flow

### 7.1 One-time offline build (the order matters)

Commands run from the repo root. `$` marks a command; arrows mark what each function does. LLM = uses Gemini requests.

```
$ uv sync                                            (install dependencies)

$ uv run python src/kg/cli.py parse-acts             [no LLM]
    jobs.parse_acts
      -> act_parser.parse_act_html   (per act in configs/kg.yaml)
      -> consolidate.VersionedAct.load_base
      -> jobs.write_act_graph        => Act, Provision, ProvisionVersion(V0), HAS_PROVISION, VERSION_OF

$ uv run python src/kg/cli.py parse-amendments       [no LLM unless --llm-fallback]
    jobs.parse_amendments
      -> amendment_parser.extract_amendment   (split Article 1 into instructions, read effective date)
      -> amendment_parser.interpret_all       (rules: REPLACE / INSERT / REPEAL / TEXT_SUB)
      => AmendingAct nodes, AMENDS edges, data/kg/parsed/amendments/*.json

$ uv run python src/kg/cli.py consolidate            [no LLM]
    jobs.consolidate
      -> VersionedAct.apply   (per instruction, in date order: close old version, open new one)
      -> jobs.write_act_graph        => ProvisionVersion V1, V2 ..., UPDATES, AMENDED_BY

$ uv run python src/kg/cli.py extract-refs           [no LLM]
    jobs.extract_refs             => REFERS_TO edges, ExternalAct nodes, cross_references.csv

$ uv run python src/kg/cli.py extract-obligations --dry-run     (how many requests?)
$ uv run python src/kg/cli.py extract-obligations               [LLM, one request per batch of 10 units]
    obligations.build_units -> batches of 10 -> LLM.call("extract_obligations")
      => Obligation, ExtractionUnit, IMPOSES, APPLIES_TO

$ uv run python src/kg/cli.py ingest-company-docs    [no LLM unless a PDF needs the structure fallback]
    ingest.ingest_company_docs -> pdf_parser.parse_pdf -> classify -> ingest.write_doc
      => InternalDoc (7), Clause (273), HAS_CLAUSE

$ uv run python src/kg/cli.py map-coverage --dry-run            (shows estimated_requests)
$ uv run python src/kg/cli.py map-coverage                      [LLM, about 40 requests]
    coverage.map_coverage  (per document)
      -> doc_versions (V_doc, V_now) -> active_obligations (doc-type categories)
      -> candidates.doc_index -> candidates_for (top-5 clauses by BM25 4-grams)
      -> judge.judge_batch (12 obligations per request) -> classify (A / B)
      => findings, COVERED_BY edges, plus NEW_DOCUMENT findings for uncovered categories

$ uv run python src/kg/cli.py coverage-alerts        [no LLM]
    alerts.coverage_alerts        => alerts table (uncovered_obligation)
```

Quota arithmetic: 4 models × 20 requests = 80 per quota day. The `llm_calls` table shows 30 extraction attempts and 95 judge attempts in total. Many of those were failed attempts (429, 404, 503) from the early runs, not successful answers; only 1 call was served from the cache. This is why the work spread over several models and why the judge groups 12 obligations per request.

### 7.2 What the evaluation commands do (no LLM)

```
$ uv run python src/kg/cli.py eval --suite consolidation   -> 13 structural assertions + parse stats
$ uv run python src/kg/cli.py eval --suite compliance      -> findings vs the 66 gold items
$ uv run python src/kg/cli.py eval --suite ops             -> LLM calls, tokens, latency
```

### 7.3 Everyday use (the UI or CLI)

| Need | UI page | CLI | LLM? |
|---|---|---|---|
| "What did Art. 6 say on 2022-01-01?" | კითხვა-პასუხი | `qa "<question>" --as-of 2022-01-01 --act 4964423` | yes (1 request) |
| "What changed between two dates?" | ვერსიების შედარება | `compare --act 4964423 --date-a 2021-06-01 --date-b 2023-08-01 [--summary]` | only for `--summary` |
| "What are the problems in the policy?" | დოკუმენტის შემოწმება | `check-doc doc:POL-RM-014@1.0` | no (reads stored findings) |
| "Is this new PDF compliant?" | დოკუმენტის შემოწმება → upload | `check-upload path/to/file.pdf` | yes (one request per 12 relevant obligations, so a few requests) |
| "What isn't covered at all?" | შეტყობინებები | `alerts` | no |
| "What does amendment X mean for us?" | ცვლილების ანალიზი | `replay --act ... --amendment ...` | yes, few |
| "Show me how an obligation is linked" | გრაფი | – | no |
| `status` of the database | – | `status` | no |

### 7.4 The `replay` command step by step (UC3)

`replay --act 4964423 --amendment 5451815` runs `services/replay.replay`:

1. **Guard:** refuses if no `COVERED_BY` edges exist (`map-coverage` must have finished).
2. `amendment_changes`: from the `AMENDED_BY` edges, list the provision versions created by this amendment, with text before (via `UPDATES`) and after.
3. `obligation_delta(act, effective_date)`: obligations that **start** on that date are *new* or *changed*; obligations that **end** that day and have no successor are *removed*. A successor is matched to its predecessor (same unit, similar wording), producing `links`.
4. If obligations do not exist yet for that date, it runs `extract_obligations` for the act first.
5. `map_coverage(only_obligations=focus, now=effective_date, links=links)`: judges only those obligations against the documents. For a changed obligation, the clauses that covered its predecessor are added as **extra candidates**, so the judge sees what the bank wrote for the old wording.
6. `coverage_alerts(now=effective_date)`.
7. `reports.save` writes `data/kg/reports/<run_id>.md` and `.json`: change summary, new / changed / removed obligations, affected documents and clauses (priority, chance, severity, action, suggested fix), new alerts.

Why two replays: amendment **5451815** (order 46/04, effective 2022-05-03) is V1 and carries nearly all the gold A items (34 of 35); amendment **5840572** (order 135/04, effective 2023-07-01) is V2, small, with one gold A item. Their reports feed the `replay_check` in the compliance evaluation. Their prompts differ from the full mapping, so they use new requests: run them after the daily quota has reset (about 11:00 Tbilisi time).

---

## 8. The LLM layer in detail

**Entry point:** `LLM.call(purpose, messages, schema=None, max_tokens=None, use_cache=True)`. Purposes: `extract_obligations`, `judge_coverage`, `qa_answer`, `compare_summary`, `amend_parse`, document structure and classification fallbacks.

**Order of operations in a call:**
1. Compute `sha1(purpose, messages, schema)`. If `data/kg/llm_cache/<sha1>.json` exists, return it. Cache hits cost no quota and are logged as `cache_hit=1`. The key does *not* include the model, so an answer obtained from one model is reused after rotation.
2. `_pick_model`: the first model in `llm.models` that is below 20 requests today and not flagged exhausted.
3. `quota_inc`, then `litellm.completion(model="gemini/...", ...)` with JSON mode, `temperature: 0` (not sent to gemini-3 models) and `reasoning_effort: low`.
4. Error handling:

| Error | Action |
|---|---|
| 404 / "no longer available" | mark the model exhausted for the day, try the next |
| 429 per minute | wait 30 s and retry (up to 3 times) |
| 429 daily | mark the model exhausted, try the next |
| 503 / timeout / other | refund the quota count, retry; after 2 failures skip this model |
| all models exhausted | raise `BudgetExhausted` → CLI exits 2 with "re-run tomorrow" |
| output cut off (`finish_reason == "length"`) | fail without retry |
| invalid JSON | one retry with the validation error in the prompt, then `LLMValidationError` |

5. Validate with pydantic (`_validate`; a bare JSON list is wrapped in `{"results": ...}` when the schema has a `results` field), write the cache file, log the call in `llm_calls`.

**Quota day** is the Pacific date (`quota_day()`), because that is when Google resets. The `llm_quota` table keeps one row per model per day.

**Mock backends:** model names starting with `mock/` (and `LLM(backend=...)`) never touch the network. Tests use them.

**Secrets:** `load_env()` loads the file named in `FINRAG_ENV_FILE` (default `../.env`). Key values are not printed, logged or copied.

---

## 9. Evaluation

### 9.1 What the compliance suite measures

| Metric | Meaning |
|---|---|
| `recall_A`, `recall_B` | share of the gold A (amendment-caused) and B (always-wrong) items that at least one finding matches |
| `precision` | share of findings that match a gold A or B item |
| `c_false_positive_rate` | share of the 9 gold **C** items (clauses that are fine) that we flagged anyway |
| `version_attribution_accuracy` | among matched A/B items, how often our class (A / B) equals the gold class |
| `chance_calibration` | precision per `issue_chance` level: are "very high" findings really more often right? |
| `match_kinds` | how matches were made (`section+article`, `article_only`) |
| `replay_check` | per amendment, the share of its gold A items that its impact report surfaces |

### 9.2 Latest compliance result (run `eval-20261019-134416-0baf`, 13:44 Tbilisi)

| Metric | Value |
|---|---|
| findings | 286 (the 5 company-level findings are not matched to documents) |
| **recall A** | **0.686** (24 of 35) |
| **recall B** | **0.636** (14 of 22) |
| precision | 0.304 |
| C false-positive rate | 0.556 (5 of 9) |
| version attribution | 1.000 |
| matches | 27 `section+article`, 123 `article_only` |
| calibration (precision) | very_high 0.34, high 0.41, moderate 0.05, low 0.40 |

**How to read it.** The system finds roughly two thirds of the real gaps and attributes them to the right regulation version every time. Low precision is expected: we raise 286 findings against 57 real A/B items, and many extra findings may be real but unlabelled, which the gold file cannot tell us. Of the C items, 5 of 9 were flagged, which is a real weakness. "Moderate" findings are almost never gold (0.05), so filtering them out is a cheap precision gain. The next step is to open `evaluation/results/eval-20261019-134416-0baf/items.csv` and `report.md`, look at the misses (for example D1-04, D1-09, D5-03, D5-04, D6-02, D6-04) and the false positives, and adjust the judge prompt (`mapping/judge.py`).

### 9.3 Other suites

- **consolidation:** 117 instructions; 105 rules, 0 LLM, 12 unsupported, 0 unresolved; 13/13 assertions pass.
- **ops:** from `llm_calls` and `runs`: calls per purpose, cache hit rate, token counts, latency percentiles.
- **qa:** stub, because there is no gold question set for the KG yet.

---

## 10. Tests

`uv run pytest tests` (23 tests, a few seconds, no network, no API key).

| File | Covers |
|---|---|
| `conftest.py` | the deterministic mock LLM backend (`mock_backend`) and the `built` fixture that builds Phases 0–3 from the real corpus in a temp folder |
| `test_ids_matching.py` | Georgian ordinals and paths, gold article parsing, section tokens, match kinds (including no gold article), the priority rule |
| `test_store_llm.py` | store nodes / edges / as-of queries; alert de-duplication and findings; LLM logging, cache and schema; rotation on 429 and at the daily limit; invalid JSON retried then failed; quota-error detection; unavailable model skipped; truncated output not retried; transient errors rotate and are refunded; bare-list answers |
| `test_retrieval_classify.py` | 4-gram search on Georgian text; the pure A / B `classify` function |
| `test_pipeline_real_data.py` | on the real corpus: consolidation assertions, amendment parse statistics, versions and texts, company documents, and the full mock-LLM flow (extraction, mapping, alerts, replay, Q&A, compare, document check, eval suites) |

---

## 11. Current state of the data

From `data/kg/kg.sqlite` after today's real runs:

| Item | Count |
|---|---|
| Acts / AmendingActs | 4 / 23 |
| Provisions / ProvisionVersions | 1,205 / 1,319 |
| ExternalActs | 14 |
| Obligations / ExtractionUnits | 145 / 76 |
| InternalDocs / Clauses | 7 / 273 |
| Edges | VERSION_OF 1,319; HAS_PROVISION 1,205; AMENDED_BY 234; COVERED_BY 233; IMPOSES 145; APPLIES_TO 145; REFERS_TO 144; HAS_CLAUSE 273; UPDATES 114; AMENDS 23 |
| Findings | 291: class A 117, class B 174 (286 on documents, 5 company-level) |
| Findings per document | POL-RM-014 53, VR-2021-07 54, NBG-APP-2021-02 48, IA-2022-03 42, MR-2022-Q1 31, REG-RM-003 31, EG-01 27 |
| Open alerts | 102 (`uncovered_obligation`) |
| Runs / LLM calls logged | 33 / 126 |
| Replay reports | none yet (`data/kg/reports/` is empty) |

The 102 alerts are high. They list obligations with no full or partial covering clause. Some are real gaps; some reflect the limits of retrieval (top-5 clauses) or of the judge. Treat them as a to-review list, not as verified gaps.

---

## 12. Known limits and next steps

1. **Replays not run.** Both replays and therefore `replay_check` are waiting for quota (resets about 11:00 Tbilisi on 2026-10-20).
2. **Precision.** 0.30 and a 0.56 false-positive rate on C items. Likely levers: a stricter judge prompt (do not flag when a clause substantially meets the requirement), dropping or de-prioritising `moderate` findings, de-duplicating several findings that point to the same clause.
3. **Article-level matching.** 123 of 150 matches are `article_only`, so the evaluation is lenient about which clause; many document clauses have no clean section number (tables, headings).
4. **One category outside the list.** One obligation has the category "data quality", which is not in the category list; it is not mapped to any document type. Fix: normalise the category (not done, to keep the data as the model produced it).
5. **Dense retrieval and reranker** are implemented but disabled; Georgian-capable embeddings might improve candidate recall.
6. **Obligations come from one act only** (4964423). The other acts are versioned and searchable (Q&A, compare), but they have no obligations.
7. **Unsupported amendment instructions** (tables, annexes, chapter titles; 12 of 117) are not applied to the version history.
8. **Housekeeping for you:** delete `data/kg/_to_delete/` and the zero-byte `data/kg/kg.sqlite-journal` (my sandbox could not delete files in the repo); run `uv lock` to refresh `uv.lock`.
9. **The `qa` evaluation suite** needs a gold question set.

---

## 13. Command cheat sheet

All commands run from the repo root. `--dry-run` shows the number of LLM requests without spending any.

```bash
# environment
uv sync
uv run pytest tests

# offline build (see §7.1 for what each does)
uv run python src/kg/cli.py parse-acts
uv run python src/kg/cli.py parse-amendments            # add --llm-fallback to use the LLM for unresolved instructions
uv run python src/kg/cli.py consolidate
uv run python src/kg/cli.py extract-refs
uv run python src/kg/cli.py extract-obligations --dry-run
uv run python src/kg/cli.py extract-obligations
uv run python src/kg/cli.py ingest-company-docs
uv run python src/kg/cli.py map-coverage --dry-run
uv run python src/kg/cli.py map-coverage                # --doc doc:POL-RM-014@1.0 to do a single document
uv run python src/kg/cli.py coverage-alerts

# amendment impact (UC3), after the quota reset
uv run python src/kg/cli.py replay --act 4964423 --amendment 5451815
uv run python src/kg/cli.py replay --act 4964423 --amendment 5840572

# evaluation
uv run python src/kg/cli.py eval --suite compliance      # --refresh re-runs mapping first (cache; no new calls if unchanged)
uv run python src/kg/cli.py eval --suite consolidation
uv run python src/kg/cli.py eval --suite ops

# use
uv run python src/kg/cli.py qa "რა არის მოდელის რისკი?" --as-of 2022-06-01 --act 4964423
uv run python src/kg/cli.py compare --act 4964423 --date-a 2021-06-01 --date-b 2023-08-01 --summary
uv run python src/kg/cli.py check-doc doc:POL-RM-014@1.0
uv run python src/kg/cli.py check-upload path/to/document.pdf
uv run python src/kg/cli.py alerts
uv run python src/kg/cli.py status
uv run streamlit run app/kg_app.py
```

Environment variables: `FINRAG_ENV_FILE` (path of the `.env` with the Gemini key; default `../.env` next to the repo), `KG_CONFIG` (alternative config file).
