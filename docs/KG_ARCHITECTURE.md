# Regulatory Knowledge Graph — Architecture & Implementation Plan (draft v0.1)

Scope: a working draft of the product on a small corpus. It is designed so that more acts and more company documents can be added later without redesign.

## 1. Goals and scope

**In scope (draft)**

| Item | Scope |
|---|---|
| Acts | 2060636 (capital adequacy, 13 amendments), 5679194 (ECL, 1), 4822603 (lending to individuals, 7), 4964423 (model risk, 2) |
| Excluded | 32962 (Law on Commercial Banks), postponed |
| Obligation extraction | **4964423 only** (all versions). The other acts are used for Q&A, version comparison and cross-references. |
| Entity type | `commercial_bank` only |
| Company documents | the 7 synthetic PDFs in `model_risk_company_docs/` with the gold labels `answer_key_gold_labels.csv` |
| LLM | Gemini via LiteLLM, model rotation (§6) |
| Graph store | SQLite (source of truth) + NetworkX (traversal), behind an interface (Neo4j later) |
| UI | Streamlit, Georgian |

**Use cases**

| ID | Use case | Mode |
|---|---|---|
| UC1 | General question → relevant act and provision → cited answer | online |
| UC2 | Compare two versions of the same act | online |
| UC3 | New amendment arrives → analyse change → new or changed obligations → impact on internal documents → report | offline job, triggered |
| UC4 | Alert when an obligation has no `COVERED_BY` edge | offline job, after UC3 and mapping |
| UC5 | Check an existing internal document against requirements | online (uses precomputed mapping) |
| UC6 | User uploads a PDF and asks to check it | online |

**Corpus facts found during review** (they drive the decisions below)

- 2060636, 5679194 and 4964423 are stored in their **original form** (Matsne "პირველადი სახე"), with structured element ids (`DOCUMENT:1;ARTICLE:n;…`).
- **4822603 is stored as the latest consolidated text (27/02/2025)**, with inline amendment notes. Decision: treat it as a **single snapshot version** (§4.3). The original publication will not be scraped.
- Amendments are instructions in fixed Georgian patterns ("… ჩამოყალიბდეს შემდეგი რედაქციით", "… დაემატოს …", "… ამოღებულ იქნეს", "სიტყვა … შეიცვალოს …"). They can be parsed with rules, with an LLM fallback.
- Twelve amendments contain transitional provisions (`has_transitional=true`).

## 2. Architecture overview

```
OFFLINE (CLI jobs)                                      ONLINE (Streamlit → services)
─────────────────────────────────────────────           ─────────────────────────────────
raw HTML ─► act parser ─► Provision / ProvisionVersion   UC1 qa        : retrieve ► expand ► LLM
amendments ─► instruction parser ─► consolidator          UC2 compare   : graph diff ► LLM summary
         └► REFERS_TO extractor (regex)                   UC5/6 doccheck: parse ► map ► judge ► report
4964423 versions ─► obligation extractor (LLM)            UC4 alerts    : read alert table
company PDFs ─► PDF parser ─► Clause                      reports, eval dashboard
obligations × clauses ─► mapper (retrieve + LLM judge) ─► COVERED_BY + Finding
amendment replay (UC3) ─► diff ► re-extract ► re-map ► Report + Alerts
                │
                ▼
     SQLite (nodes, edges, findings, alerts, llm_calls, runs) ◄──► NetworkX view
     Vector index (BGE-M3 dense + BM25 char-ngram), per node type
```

Principles:
1. All LLM work is offline, cached and logged. Online paths make at most one or two LLM calls.
2. Every node and edge is versioned (`valid_from`, `valid_to`). Nothing is deleted.
3. Rules first, LLM as fallback, both for amendment parsing and for PDF structuring.

## 3. Knowledge graph schema

### 3.1 Nodes

| Type | Key properties | ID format |
|---|---|---|
| `Act` | doc_id, title, type, number, authority, adopted | `act:4964423` |
| `AmendingAct` | doc_id, number, date, effective_from, has_transitional, transitional_note | `amend:5451815` |
| `Provision` | act_id, path (art/para/sub), heading | `prov:4964423/art6/p4.1` |
| `ProvisionVersion` | text, valid_from, valid_to, status (`active`/`repealed`), source (`original`/`amendment`/`snapshot`) | `pv:4964423/art6/p4.1@2022-05-02` |
| `Obligation` | statement, subject, action, condition, deadline_or_frequency, severity, category, entity_types | `obl:4964423/art6/p4.1#1@2022-05-02` |
| `EntityType` | name | `ent:commercial_bank` |
| `ExternalAct` | title or number as cited (acts outside the corpus) | `ext:<normalized number>` |
| `InternalDoc` | file, title, doc_code, doc_type, doc_date, company | `doc:POL-RM-014@1.0` |
| `Clause` | text, section_no, heading, unit_type, page, order | `cl:POL-RM-014@1.0/5.2.1` |

Path convention: `art{N}[^k]` → `art9_1`; paragraph `p{N}[.k]`; sub-point `s{letter}` (e.g. `sბ.ა`). It is derived from the Matsne element ids where available, and from regex otherwise.

### 3.2 Edges

| Edge | From → To | Properties |
|---|---|---|
| `HAS_PROVISION` | Act → Provision | order |
| `VERSION_OF` | ProvisionVersion → Provision | — |
| `UPDATES` | ProvisionVersion(new) → ProvisionVersion(old) | change_type: `modified` / `added` / `repealed` |
| `AMENDED_BY` | ProvisionVersion → AmendingAct | instruction_id |
| `AMENDS` | AmendingAct → Act | seq |
| `REFERS_TO` | Provision → Provision / Act / ExternalAct | ref_text, method (`regex`) |
| `IMPOSES` | ProvisionVersion → Obligation | — |
| `APPLIES_TO` | Obligation → EntityType | — |
| `HAS_CLAUSE` | InternalDoc → Clause | order |
| `COVERED_BY` | Obligation → Clause | coverage (`full`/`partial`/`contradicts`), issue_chance, severity, priority, rationale, evidence_quote, status (`proposed`/`confirmed`/`rejected`), run_id |

A **Finding** is a record, not an edge. It is created when coverage is `partial` or `contradicts`, or when no clause covers an obligation that is relevant to the document type.

### 3.3 Risk labels (UC "chance × severity")

- `issue_chance` (how likely the clause or document is problematic), from the LLM judge: `very_high` / `high` / `moderate` / `low` (UI: ძალიან მაღალი / მაღალი / საშუალო / დაბალი).
- `severity` (how crucial the obligation is): set on the **Obligation** at extraction time, using this rubric: `critical` = supervisory consent or reporting to the NBG, independence of the lines of defence, launching a model before validation, anything with explicit sanctions; `high` = core process duties (validation, monitoring, data quality); `medium` = documentation and methodology details; `low` = definitions and formal references. UI: კრიტიკული / მაღალი / საშუალო / დაბალი.
- `priority` = severity, except that if `issue_chance = low` the priority drops by one level. A `moderate` chance on a `critical` obligation therefore stays **critical**.

### 3.4 Storage

SQLite file `data/kg/kg.sqlite`:

```
nodes(id PK, type, props JSON, valid_from, valid_to)
edges(id PK, src, dst, type, props JSON, valid_from, valid_to)
findings(id, run_id, doc_id, clause_id NULL, obligation_id, version_basis, gap_class, action_type,
         issue_chance, severity, priority, rationale, evidence_quote, suggested_fix, created_at)
alerts(id, run_id, kind, obligation_id, message, created_at, acknowledged)
runs(id, kind, started_at, finished_at, params JSON, status)
llm_calls(id, run_id, purpose, model, prompt_hash, input_tokens, output_tokens, latency_ms, cache_hit, error, ts)
llm_quota(model, day, requests)
```

**SQLite conventions: follow `src/chat_db.py`.** `src/kg/store/sqlite_nx.py`, together with the run, LLM-usage, finding and alert tables, uses the same pattern:

- Standard-library `sqlite3` only, no ORM. The module has no Streamlit imports, so it can be tested on its own.
- One module-level `SCHEMA` string with `CREATE TABLE IF NOT EXISTS …` and `CREATE INDEX IF NOT EXISTS …`, run with `executescript` in the class `__init__`, which also creates the parent folder (`Path(path).parent.mkdir(parents=True, exist_ok=True)`).
- A `_tx()` context manager per operation: `sqlite3.connect(path, timeout=10)`, `row_factory = sqlite3.Row`, `PRAGMA foreign_keys=ON`, then commit on success, roll back on error, and always close. There are no long-lived connections, which keeps Streamlit and the CLI jobs safe to run side by side.
- Light in-place migrations: in `__init__`, check `PRAGMA table_info(<table>)` and `ALTER TABLE … ADD COLUMN` for missing columns.
- Parameterised queries only (`?` placeholders). JSON columns are written with `json.dumps(…, ensure_ascii=False)` so Georgian text stays readable, and are parsed back on read.
- Timestamps are ISO strings in Tbilisi time, via a `now_tbilisi()` helper (UTC+4, no DST), `isoformat(timespec="seconds")`.
- Methods return plain `dict`s (`dict(row)`), not `sqlite3.Row`.
- The module docstring gives a ready `sqlite3 -header -csv data/kg/kg.sqlite "SELECT …"` example for inspecting `llm_calls`, `findings` and `alerts` from a terminal.

`GraphStore` interface (`src/kg/store/base.py`): `upsert_node`, `upsert_edge`, `get_node`, `neighbors(id, edge_type, direction)`, `nodes(type, **filters)`, `as_of(date)` (returns a filtered NetworkX view), `subgraph(ids)`. The NetworkX graph is rebuilt from SQLite on load. A `Neo4jStore` can later implement the same interface.

## 4. Offline pipeline

Run with `uv run python -m kg.cli <command>`. Every command creates a `runs` row and writes a JSONL log to `data/kg/runs/<run_id>.jsonl`.

### 4.1 `parse-acts` — base acts → Provision and ProvisionVersion (no LLM)

- Input: `matsne_data/graph_docs/raw/{id}.html` and `amendment_docs.jsonl` for metadata.
- Reuse the logic from `src/chunker.py` (element-id walk, `RE_ART` / `RE_PARA` / `RE_SUB`), but emit one node per **paragraph and sub-point** with a stable path rather than retrieval chunks.
- For an original-form act: `ProvisionVersion.valid_from` = the act's effective date, `source=original`.
- For 4822603: see §4.3.

### 4.2 `parse-amendments` and `consolidate` — instructions → new versions

1. Extract the instruction list from each amending act: numbered items in Article 1, including nested `ა)`, `ბ)` items.
2. **Rule parser.** Patterns map to operations:

   | Pattern (Georgian) | Operation |
   |---|---|
   | `X ჩამოყალიბდეს შემდეგი რედაქციით: „…“` | `REPLACE(target, text)` |
   | `X-ს დაემატოს შემდეგი შინაარსის Y: „…“`, `X-ის შემდეგ დაემატოს …` | `INSERT(parent/after, new_path, text)` |
   | `X ამოღებულ იქნეს` / `ძალადაკარგულად ჩაითვალოს` | `REPEAL(target)` |
   | `X-ში სიტყვები „a“ შეიცვალოს სიტყვებით „b“` | `TEXT_SUB(target, a, b)` |

   The target resolver converts Georgian ordinals to paths ("მე-6 მუხლის მე-5 პუნქტის „ბ.ა“ ქვეპუნქტი" → `art6/p5/sბ.ა`). It needs an ordinal table (პირველი = 1, მე-N = N) and superscript handling (`9¹`, also written "91").
3. **LLM fallback** for instructions the rules cannot parse. The prompt receives the instruction text and the target act's table of contents and returns the same operation JSON. Each fallback is logged (`purpose=amend_parse`).
4. **Consolidator.** Apply the operations in `seq` order. Each touched provision gets a new `ProvisionVersion` (`valid_from` = the amendment's effective date), the previous version gets `valid_to`, and `UPDATES` + `AMENDED_BY` edges are added. Unresolved operations go to `runs/<id>_unresolved.json`.
5. **Effective date.** Parse the final article of the amending act ("ამოქმედდეს …", "გამოქვეყნებისთანავე"). The fallback is `published_date`. Transitional clauses (e.g. 46/04: "from 1 October 2022 for all models") are stored as `AmendingAct.transitional_note`, plus `applies_to_existing_from` when a date is found.
6. **Consolidated view.** No full-text documents are stored. `as_of(date)` selects, for each Provision, the version where `valid_from ≤ date < valid_to`. The "consolidated text" for the LLM and UI is assembled on the fly in the act's order.

### 4.3 4822603 (snapshot act)

Load the current text as one version (`source=snapshot`, `valid_from` = 27/02/2025). Its 7 amendments become `AmendingAct` nodes with `AMENDS` edges only; there are no historical versions. Inline amendment notes ("… – ვებგვერდი, dd.mm.yyyy") are stripped from the text and stored as `amendment_notes` on the version. UC2 is disabled for this act in the UI ("ისტორიული ვერსიები მიუწვდომელია").

### 4.4 `extract-refs` — REFERS_TO (regex, no LLM)

- **Internal:** "ამ დებულების / ამ მუხლის … მუხლის … პუნქტ…" → resolved with the same target resolver to a Provision in the same act.
- **External:** quoted titles „…“ followed by an order or law number ("2014 წლის 13 ივნისის №47/04 ბრძანებით"). Match the number and date, or the normalized title, against the `Act` nodes. If there's no match, create an `ExternalAct` stub.
- Output also goes to `matsne_data/graph_docs/cross_references.csv`, which keeps the existing header.

### 4.5 `extract-obligations` — 4964423 only (LLM)

- V0: all normative provisions. V1 and V2: **only provisions changed** by that amendment (taken from `UPDATES`). Unchanged obligations are carried forward by copying them with the new `valid_from`.
- Batch about 8–12 provisions per call. Output is a JSON list: `statement, subject, action, condition, deadline_or_frequency, severity, category, source_path`. Severity follows the §3.3 rubric, which is included in the prompt.
- Definitions, scope and entry-into-force provisions are tagged `category=non_obligation`, and no Obligation is created for them.
- Each obligation gets `APPLIES_TO → ent:commercial_bank`. For the draft, every obligation in 4964423 applies to commercial banks.

### 4.6 `ingest-company-docs` — PDF → Clause

Use one parser for both the offline corpus and UC6 uploads (`src/kg/company/pdf_parser.py`):

1. Extract with `pdfplumber`: words with font size and bold flag, plus tables (`extract_tables`).
2. Detect units with heuristics, in order:
   - Tables become a `table` unit with header+row text (one unit per table, or per row if more than 15 rows).
   - Lines starting with a section number (`^\d+(\.\d+)*\.?\s`) start a new `clause`; ones in bold or larger font become `heading`.
   - Cover/metadata tables (key–value, 2 columns, page 1) become `metadata`. Doc code, date and approver are extracted with regex.
   - Signature blocks ("ხელმოწერა", "დაამტკიცა", "_____") become `signature`.
   - Running headers and footers (the same text on every page) are dropped.
3. **LLM fallback** (one call per document) runs only when the heuristics fail: fewer than 5 units, or a unit longer than 3,000 characters. The LLM returns unit boundaries and types for the extracted text.
4. **Document classification** (doc_type: `policy`, `inventory`, `validation_report`, `monitoring_report`, `ethics_guideline`, `audit_report`, `supervisory_application`, `other`). Use regex on the title first, then an LLM fallback. The doc_type limits which obligations are relevant (§4.7).
5. `doc_date` comes from the cover metadata and is needed for the as-of check (§4.7).

### 4.7 `map-coverage` — obligations × clauses → COVERED_BY + Findings (LLM)

For each document and each **version basis** (V_doc = version in force at `doc_date`; V_now = latest version):

1. **Relevant obligations.** A static table `configs/doc_type_obligations.yaml` lists which obligation categories each doc_type must address. For example, `validation_report` → validation, verification, outcomes analysis, overrides, explainability, bias. Keep it short and hand-written.
2. **Candidate clauses.** For each relevant obligation, take the top 5 clauses of that document by hybrid score.
3. **LLM judge.** Batch about 6–10 obligations per call, each with its candidate clauses. For each obligation it returns `coverage, best_clause_id, issue_chance, rationale, evidence_quote, suggested_fix, action_type` (`ADJUST_SECTION` / `ADD_SECTION` / `NEW_DOCUMENT`).
4. Write `COVERED_BY` edges for `full`, `partial` and `contradicts`. Write a Finding for `partial`, `contradicts` and "not covered", with severity and priority taken from the obligation.
5. **Gap class:**
   - Issue under V_doc → `B` (non-compliant already at the document date).
   - Issue only under V_now → `A` (amendment-triggered).
   - No issue under either → nothing written. The C items in the gold set must stay here.
6. `NEW_DOCUMENT`: when an obligation's category has no relevant doc_type in the company's inventory. For example, an ethics obligation exists but the company has no ethics document.

### 4.8 `coverage-alerts` (UC4)

After `map-coverage`: for every active obligation that applies to `commercial_bank` and has **no** `COVERED_BY` edge with coverage `full` or `partial` in any document, insert an `alerts` row (`kind=uncovered_obligation`). Do the same for obligations whose only coverage is `contradicts`. Alerts are deduplicated per obligation and version.

### 4.9 `replay-amendment` (UC3, time-travel demo)

```
kg.cli replay --act 4964423 --amendment 5451815      # then 5840572
```

1. Start state: the graph as of 2021 (V0), with company documents mapped against V0.
2. Run §4.2 for that amendment only, then §4.5 for the changed provisions, then §4.7 restricted to the new or changed obligations and to clauses previously linked to the obligations they replace, then §4.8.
3. Produce an impact report (`data/kg/reports/<run_id>.md` and `.json`):
   - The change summary.
   - New, changed and removed obligations.
   - Affected documents and clauses, each with an action type (adjust, add section, new document), chance, severity, priority and suggested fix.
   - New alerts.
4. Log every step (inputs, outputs, LLM calls) to the run log.

## 5. Online services (`src/kg/services/`)

| Service | Flow | LLM calls |
|---|---|---|
| `qa.answer(q, as_of=today, act=None)` | hybrid retrieval over `ProvisionVersion` valid at `as_of` (top 30) → optional reranking → keep top 6 → expand: parent article + 1-hop `REFERS_TO` (in-corpus only) → prompt with passages grouped by act → JSON `{answer, citations[]}` → verify that every cited id was in the context | 1 |
| `compare.versions(act, date_a, date_b)` | per Provision, pick the version at each date → classify added, removed or modified, with a word-level diff (`difflib`) → optional LLM summary in Georgian | 0–1 |
| `doccheck.check_existing(doc_id)` | read precomputed findings for the document (§4.7) | 0 |
| `doccheck.check_upload(pdf)` | §4.6 parse → classify → §4.7 for that document only, both version bases, with live progress | about 2–6 |
| `alerts.list()` | read the `alerts` table | 0 |
| `reports.get(run_id)` | read an impact report | 0 |

**Retrieval index** (`src/kg/retrieval/`): BGE-M3 dense vectors stored as `.npy`, plus BM25 over character 4-grams, which copes with Georgian morphology. Scores are fused with RRF. There is one index per node type (ProvisionVersion, Clause). It is rebuilt incrementally after offline jobs. The reranker (`bge-reranker-v2-m3`, local) is optional behind a config flag.

## 6. LLM layer (`src/kg/llm.py`)

- One entry point, `llm.call(purpose, messages, schema=None, max_tokens=…)`. It wraps `litellm.completion`, following the pattern in `src/generate.py` and `src/chatbot.py`.
- **Model rotation.** Read an ordered list from `configs/kg.yaml`:
  ```yaml
  llm:
    models:              # LiteLLM ids, same format as src/chatbot.py; tried in this order
      - gemini/gemini-2.5-flash
      - gemini/gemini-2.5-flash-lite
      - gemini/gemini-3.5-flash
      - gemini/gemini-3.5-flash-lite
      - gemini/gemini-3.6-flash
      - gemini/gemini-3.6-flash-lite
    daily_request_limit: 20
    temperature: 0
  ```
  Before each call, choose the first model with `llm_quota.requests < limit` for today. On a 429 or quota error, mark that model exhausted for the day and move to the next. If all are exhausted, raise `BudgetExhausted`; the offline job saves its progress and can be resumed.
- **Cache.** Responses are cached by a SHA-1 of (purpose, messages, schema), *not* the model, so a rerun after rotation reuses earlier answers. Cache hits don't count toward quota.
- **Usage log.** Every call writes to `llm_calls`: model, tokens in and out, latency and cache_hit.
- **Structured output.** Request JSON, validate with pydantic, retry once with the error message, then fail the item (logged) without failing the whole job.
- **Secrets.** `python-dotenv` loads from the path in the environment variable `FINRAG_ENV_FILE` (default: `../.env` relative to the repo). The code never prints, logs or copies key values.
- **Budget.** The 6 models × 20 requests give about 120 requests per day. Batching (§4.5, §4.7) keeps the full offline build within a day:

  | Step | Estimated requests |
  |---|---|
  | Amendment-parse fallback (22 amendments, most parsed by rules) | 0–10 |
  | Obligations for 4964423: V0 plus the changes in V1 and V2 | ~12 |
  | PDF structuring and classification fallback (7 documents) | 0–7 |
  | Coverage mapping: 7 documents × 2 version bases, batched | ~30–40 |
  | Replaying 2 amendments | ~10 |
  | **Total** | **~55–80** |

  `kg.cli <job> --dry-run` prints the estimated number of calls before running.

## 7. Evaluation (`src/kg/eval/`)

Run `kg.cli eval --suite compliance|consolidation|ops`. Results go to `evaluation/results/<run_id>/` as `metrics.json`, `items.csv` and `report.md`, and a row is appended to `evaluation/results/index.csv`.

**Compliance suite** (gold: `model_risk_company_docs/answer_key_gold_labels.csv`)

- **Matching rule:** a predicted Finding matches a gold item if it is the same document, the clause section number appears in the gold `document_section`, and the article overlaps after normalising (e.g. `Art. 6 para 4¹` → `art6/p4.1`). If the section is missing, the article alone is used and recorded as `match=article_only`.
- Metrics:
  - Recall on A items and on B items separately.
  - Precision on all findings.
  - **False-positive rate on C items** (a C item flagged is an error).
  - **Version-attribution accuracy:** A items found under V_now but not V_doc, and B items found under V_doc.
  - Chance calibration: the distribution of gold A and B items across chance buckets, and precision per bucket.
  - Action-type distribution.
- **Replay check:** after `replay 5451815` and then `replay 5840572`, the fraction of gold A items surfaced in each impact report.

**Consolidation suite** (no external gold)

- Share of instructions parsed by the rules, the LLM fallback, or left unresolved.
- Structural assertions on known facts, e.g. `art9_1` exists in 4964423 from 2022-05-02; `art4/p3` text changes at V1 and again at V2; the number of versions per act matches the amendment count.

**Ops metrics** (for every suite and every UI call)

- Latency: p50 and p95 per service and per offline step.
- Tokens in and out, LLM calls, and cache-hit rate per run and per purpose.
- All of it computed from `llm_calls` and `runs`.

Q&A evaluation is **deferred**, since there is no gold set yet. The harness has a stub `qa` suite ready for it.

## 8. UI (Streamlit, Georgian) — `app/kg_app.py`

| Page | Content |
|---|---|
| კითხვა-პასუხი | question box, act filter, "as of" date; answer with clickable citations (act, article, version) |
| ვერსიების შედარება | choose act, two dates; side-by-side diff per provision; LLM summary button |
| ცვლილების ანალიზი | choose amendment → run or view replay; impact report (change summary, obligations, affected clauses with priority badges) |
| დოკუმენტის შემოწმება | choose an existing document or upload a PDF; findings table sortable by priority; filters by chance, severity, version basis (V_doc/V_now) and action type; clause text with highlighted evidence |
| შეტყობინებები | open alerts (uncovered obligations), acknowledge button |
| გრაფი | small pyvis view of an impact path: version → obligation → clause |
| შეფასება | latest eval metrics, latency and token charts from `evaluation/results/` |

Labels and enum translations come from `app/i18n_ka.py`.

## 9. Repository layout

```
configs/kg.yaml                 # paths, models, quotas, retrieval params
configs/doc_type_obligations.yaml
src/kg/
  cli.py                        # all offline commands + eval
  config.py  llm.py  ids.py     # ids: path & ID builders, Georgian ordinal resolver
  store/      base.py  sqlite_nx.py
  ingest/     act_parser.py  amendment_parser.py  consolidate.py  crossrefs.py
  extract/    obligations.py  prompts/
  company/    pdf_parser.py  classify.py
  mapping/    candidates.py  judge.py  coverage.py  alerts.py
  retrieval/  index.py
  services/   qa.py  compare.py  doccheck.py  replay.py  reports.py
  eval/       compliance.py  consolidation.py  ops.py  matching.py
app/kg_app.py  app/i18n_ka.py
data/kg/                        # kg.sqlite, indexes/, runs/, reports/ (gitignored)
evaluation/results/
tests/                          # parser + resolver unit tests on fixtures
```

Extra dependencies (uv): `pdfplumber`, `networkx`, `rank-bm25`, `pydantic`, `pyyaml`, `pyvis`. Existing dependencies are reused: `litellm`, `sentence-transformers`, `bs4`, `streamlit`, `python-dotenv`.

## 10. Implementation plan

| Phase | Deliverable | Acceptance criteria | Est. |
|---|---|---|---|
| 0. Foundations | config, `GraphStore` (SQLite following the `src/chat_db.py` pattern, + NetworkX), `llm.py` with rotation over the six models in §6, cache and usage log, run logging, IDs and ordinal resolver | unit tests for the store and resolver; one LLM call is logged with model, tokens and latency; rotation tested by simulating a 429 | 1 d |
| 1. Acts & versions | `parse-acts`, `parse-amendments`, `consolidate`, 4822603 snapshot, `extract-refs` | 4 acts loaded; consolidation suite passes its structural assertions; under 10% of instructions unresolved; `cross_references.csv` filled | 2 d |
| 2. Obligations | `extract-obligations` for 4964423, V0, V1 and V2 | every normative provision in 4964423 has an obligation or a non-obligation tag; obligations carried forward correctly across versions | 1 d |
| 3. Company docs | PDF parser, classifier, `ingest-company-docs` | all 7 PDFs split into units whose section numbers match the PDFs; tables kept as units; doc_date parsed | 1 d |
| 4. Mapping & alerts | `map-coverage` (both bases), findings, `coverage-alerts` | compliance suite runs end to end; findings carry chance, severity, priority, gap class and action type | 1.5 d |
| 5. Services | qa, compare, doccheck (existing and upload), replay with impact report | each service callable from the CLI; replay of 46/04 produces a report listing new obligations and affected clauses | 1.5 d |
| 6. Evaluation | compliance, consolidation and ops suites; results index | `evaluation/results/<run_id>/` written; metrics reproducible from cache with 0 new LLM calls | 1 d |
| 7. UI | Streamlit app, Georgian | all 7 pages work on the draft data; upload check finishes with progress shown | 1.5 d |

The order matters: phases 1–4 consume the LLM budget, so run them on separate days if quota runs out. All jobs can be resumed from the cache.

## 11. Decisions and known limitations

- No scraping of Matsne's consolidated versions (they need a paid account). Our consolidation is checked only with structural assertions and manual spot checks.
- 32962 is excluded. 4822603 is kept as a single 2025 snapshot with no version history; its original publication will not be scraped, so UC2 and UC3 don't apply to it.
- Severity comes from an LLM using a fixed rubric. It is not yet validated against real fine data.
- The doc-type → obligation-category table is hand-written. It is acceptable for 7 documents and should become learned or configurable as the corpus grows.
- Gold labels exist only for compliance checking, so Q&A quality is unmeasured in the draft.

## 12. Open questions

None at this stage. The earlier questions are resolved: 4822603 stays in snapshot mode (§4.3), because its original publication won't be scraped; the model ids are fixed in §6.
