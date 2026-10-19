# Implementation report — Regulatory Knowledge Graph

Written 2026-10-19 (night session). It covers what I built from `docs/KG_ARCHITECTURE.md`, what I could and could not check, where I deviated from the plan, and how to run the system. Everything lives inside the repository. I never opened or read the `.env` file.

## 1. Summary

| Phase | Status | Verified how |
|---|---|---|
| 0. Foundations (config, store, LLM layer, run log, ids) | done | unit tests; rotation and budget exhaustion simulated |
| 1. Acts and versions | done | run on the real corpus; 13/13 structural assertions pass |
| 2. Obligations | code done, **not run on real data** | needs Gemini; run with a mock LLM only |
| 3. Company documents | done | all 7 PDFs parsed on the real files |
| 4. Mapping and alerts | code done, **not run on real data** | needs Gemini; run with a mock LLM only |
| 5. Services (qa, compare, doccheck, replay, reports) | done | replay, compare, qa and doccheck exercised with a mock LLM |
| 6. Evaluation (compliance, consolidation, ops, qa stub) | done | consolidation and ops run for real; compliance run on mock findings |
| 7. UI (Georgian Streamlit, 7 pages) | done | rendered page by page with Streamlit's `AppTest` against a mock-filled database |

**What I could not do:** call Gemini. The sandbox has no API key and no network route to the provider. Every LLM-dependent step (obligation extraction, coverage judging, Q&A, summaries, the parser fallbacks) was tested only with a deterministic mock backend. So the quality numbers you care about most — recall, precision, false positives on C items — do not exist yet. The first real run will produce them (§4).

Tests: `pytest tests` → **19 passed** (about 5 s). The pipeline test builds Phases 0–3 from the real corpus in a temp directory and then runs extraction, mapping, alerts, replay, Q&A, compare, document check and all four eval suites with the mock LLM.

## 2. What was built

```
configs/kg.yaml                  paths, 6 Gemini models (20 requests/day each), batch sizes, retrieval params
configs/doc_type_obligations.yaml  doc_type -> obligation categories (hand-written)
src/kg/
  config.py ids.py runlog.py llm.py cli.py
  store/      base.py (GraphStore ABC)  sqlite_nx.py (SQLite + NetworkX)
  ingest/     act_parser.py  amendment_parser.py  consolidate.py  texttools.py  jobs.py
  extract/    obligations.py  prompts/obligations.md
  company/    classify.py  pdf_parser.py  ingest.py
  retrieval/  index.py            BM25 over char 4-grams + optional BGE-M3 + RRF + optional reranker
  mapping/    candidates.py judge.py labels.py coverage.py alerts.py
  services/   qa.py compare.py doccheck.py replay.py reports.py
  eval/       matching.py compliance.py consolidation.py ops.py runner.py
app/          kg_app.py  i18n_ka.py
tests/        19 tests
```

Key behaviours:

- **Store.** `SQLiteStore` follows `src/chat_db.py` (stdlib `sqlite3`, `SCHEMA` string, `_tx()` context manager, PRAGMA-based migrations, `ensure_ascii=False` JSON, Tbilisi timestamps). It sits behind a `GraphStore` ABC; `as_of(date)` returns a NetworkX view.
- **LLM layer.** LiteLLM `gemini/...` ids, same format as `chatbot.py`. It tries the six models in order and counts requests per model per day in SQLite. A 429 or quota error marks that model exhausted for the day; when all are exhausted it raises `BudgetExhausted`, the job ends as `partial`, and it can be resumed tomorrow. Responses are cached on disk by a hash of (purpose, messages, schema), so a re-run costs no requests. Invalid JSON gets one retry. Every call is logged in `llm_calls`.
- **Amendment parsing.** Rule-based, with an LLM fallback (`--llm-fallback`). It handles replace, insert, repeal, text substitution, chapter replace and repeal, and article titles. New provision paths come from the labels in the quoted text.
- **Consolidation.** Replays operations per provision and closes versions. Nothing is deleted: a repeal creates a version with `status=repealed`.
- **Obligations.** Units are a paragraph plus its sub-points (or an article with no paragraphs). Obligations are valid over the version span of their unit, so unchanged units are carried forward without being copied.
- **Mapping.** For each document, the obligations of its doc_type's categories are judged per distinct obligation node (8 per request, top-5 candidate clauses each from a per-document hybrid index). Findings are classified as A or B by comparing the obligation at the document's date (V_doc) with the one in force now (V_now). `priority` equals severity, lowered one step only when the chance is `low`.
- **Replay.** Re-enacts one amendment on top of the built graph: the provisions it changed, the obligations that started or ended on its effective date, the clauses that covered the replaced obligations (added as extra candidates), new alerts. It writes `data/kg/reports/<run_id>.md` and `.json`.
- **UI.** Pages: კითხვა-პასუხი, ვერსიების შედარება, ცვლილების ანალიზი, დოკუმენტის შემოწმება (existing or uploaded PDF, with progress), შეტყობინებები, გრაფი (pyvis), შეფასება.

## 3. Results on the real corpus (no LLM involved)

| Item | Result |
|---|---|
| Acts parsed | 4 (2060636, 4822603, 4964423, 5679194) |
| Graph nodes | 1,205 Provisions, 1,319 ProvisionVersions, 23 AmendingActs, 14 ExternalActs, 7 InternalDocs, 273 Clauses |
| Amendment instructions | 117 in total: 105 parsed by rules (89.7%), 0 unresolved, 12 `unsupported` |
| Versions | 2060636: 692 versions from 591 provisions (13 amendments); 4964423: 179 from 167 (2 amendments); 5679194: 307 from 306 (1 amendment) |
| Cross-references | 113 internal, 20 external by number, 10 by title, 5 pointing into the corpus; `cross_references.csv` now has 92 rows |
| Obligation units for 4964423 | 76 units → **8 requests** for extraction (dry run) |
| Company PDFs | 7 of 7 parsed, no LLM fallback needed, no warnings. Units: D1 93, D2 20, D5 39, D6 34, D9 29, D10 20, D11 38. Doc types and dates were detected correctly |
| Consolidation suite | 13/13 assertions pass (see below) |

The 12 `unsupported` instructions are 9 chapter-title edits in amendment 3922106 and 3 annex/table edits (2470749, 3778783, 6398384). Chapter titles, annexes and tables are out of scope for this draft; they are recorded, not applied.

Structural assertions that pass: `art9_1` exists in 4964423 from the first amendment's effective date; `art4/p3` has three distinct versions (V0, V1, V2); version dates are contiguous with no gaps or overlaps for every versioned act; every Provision has a version; the 4822603 snapshot has a single version date; under 10% of instructions are unresolved.

The database with all of the above is in `data/kg/kg.sqlite`. The consolidation and ops eval results are in `evaluation/results/`.

## 4. How to run it

From the repo root (install the new dependencies first: `uv sync`; `uv.lock` was not regenerated, so run `uv lock` once):

```
uv run python src/kg/cli.py parse-acts
uv run python src/kg/cli.py parse-amendments            # add --llm-fallback to let Gemini resolve leftovers
uv run python src/kg/cli.py consolidate
uv run python src/kg/cli.py extract-refs
uv run python src/kg/cli.py ingest-company-docs
uv run python src/kg/cli.py extract-obligations --dry-run   # request estimate: 8
uv run python src/kg/cli.py extract-obligations
uv run python src/kg/cli.py map-coverage --dry-run          # estimate before spending quota
uv run python src/kg/cli.py map-coverage
uv run python src/kg/cli.py coverage-alerts
uv run python src/kg/cli.py replay --act 4964423 --amendment 5451815
uv run python src/kg/cli.py replay --act 4964423 --amendment 5840572
uv run python src/kg/cli.py eval --suite compliance         # also consolidation | ops | qa
uv run python src/kg/cli.py status                          # counts, last runs, remaining LLM budget
uv run streamlit run app/kg_app.py
```

Also available: `qa`, `compare`, `check-doc`, `check-upload`, `alerts`. Phases 0–3 are already done in the shipped database, so you can start at `extract-obligations`.

**Budget:** the API key is read from the `.env` named by `FINRAG_ENV_FILE` (default: next to the repo, as you set it up). Extraction needs 8 requests. Mapping needs roughly one request per 8 obligations per document; my mock run with 76 obligations needed 54, and real extraction will probably give more obligations, so expect 60–120. That is at most one day's quota across the six models (120). If the quota runs out the job stops as `partial`; run it again the next day and the cache supplies everything already done. Replays reuse cached prompts and should cost few or no new requests.

**Before trusting the first real run:** read the first 10 or so obligations and findings by eye, then run `eval --suite compliance`. The severity rubric and the doc_type → category table are my drafts.

## 5. Deviations from the plan and things to know

1. **V1 date.** The consolidated V1 is dated 2022-05-03 (the amendment's publication date, "effective on publication"). The plan and the gold file say 02.05.2022. My consolidation assertion uses the amendment's own effective date, so it is consistent with the data. V0 is dated 2021-01-01 in `configs/kg.yaml`, not 17.08.2020; all seven documents were written after it, so this changes no result.
2. **Obligation carry-forward is implicit.** There are no copies of unchanged obligations. An Obligation is valid from its unit-version's start to the next change. I added an `ExtractionUnit` node (not in the plan) to record which units were extracted and which had no obligation.
3. **Replay re-enacts, it does not rebuild.** `consolidate` builds all versions at once. Replay then analyses one amendment's slice of that graph (changed provisions, obligations starting or ending on its date, restricted mapping, alerts). The results are the same as the plan's flow would give, but there is no separate "state at V0" snapshot.
4. **NEW_DOCUMENT** has two sources: the LLM may return it as the action type, and, after a full `map-coverage`, any obligation whose category no ingested document type is expected to cover gets a company-level finding (`doc_id='__company__'`). With the seven documents, expect few or none.
5. **Findings for repealed obligations are skipped.** A document that was non-compliant with an obligation that has since been repealed gets no class B finding.
6. **Document uploads** (UC6) are stored with `source=upload` and their coverage edges do not count when computing UC4 alerts.
7. **Matching in the compliance suite.** Article overlap is checked down to paragraph level; sub-point letters in the gold article column are ignored. A finding without a clause, or a gold item without a section number (table rows, "whole document"), matches on the article alone (`article_only`). A gold item with no article ("—") matches on the section alone (`section_only`). "(also X)" in a gold section is honoured. These rules are slightly lenient and are recorded per item in `items.csv`.
8. **Prototype chunker.** `src/chunker.py` ignores `SUBPOINT` elements in act 2060636. `src/kg/ingest/act_parser.py` does not. I did not touch `chunker.py`.
9. **Warnings.** A few `replace_missing_target` warnings appear during consolidation: an amendment replaces a provision that did not exist as a separate node, so it is added instead. They are benign.
10. **Layout.** Cross-reference extraction lives in `ingest/jobs.py`, not a separate `crossrefs.py`. The CLI is run as `python src/kg/cli.py ...` (it puts `src/` on the path itself).
11. **Dense retrieval and the reranker are off** (`retrieval.dense.enabled`, `retrieval.reranker.enabled`). The BM25 character 4-gram index works on its own and is what the tests cover. The BGE-M3 and cross-encoder code paths are untested.
12. **Q&A quality is unmeasured** (the `qa` eval suite is a stub, as planned). Q&A verifies that every cited passage id was in the context and reports dropped ones.
13. **Python version.** I ran everything on Python 3.10 in the sandbox; `pyproject.toml` still says `>=3.13`. I used no 3.13-only features, but I did not test on 3.13.
14. **Dependencies added** to `pyproject.toml`: `networkx`, `pdfplumber`, `pydantic`, `pyvis`, `pyyaml`, `rank-bm25`, and `pytest` in the dev group.

## 6. Housekeeping for you

- My sandbox could not let SQLite create its journal file on the mounted folder, so I built the database in a scratch directory and copied it in. On your machine SQLite works normally; nothing in the code depends on this.
- I cannot delete files in your folder. Please delete these yourself: `data/kg/_to_delete/` (four mock replay reports and a pyvis `lib` folder that my testing created) and the zero-byte `data/kg/kg.sqlite-journal` (harmless but stale).
- `data/kg/` is git-ignored; `evaluation/results/` is not, on purpose, so results can be committed.
- I changed `.gitignore` (added `data/kg/`, `.pytest_cache/`) and `pyproject.toml` only. No existing source files were modified. `matsne_data/graph_docs/cross_references.csv` was regenerated by `extract-refs` (it is git-ignored).

## 7. Suggested next steps

1. Run `extract-obligations`, then `map-coverage`, then `eval --suite compliance`, and read the misses. The most likely early problems: severity levels, the doc_type → category table, and candidate retrieval for table-heavy documents (D2, D10).
2. Tune the judge prompt (`src/kg/mapping/judge.py`) against the C items first — false positives on compliant parts are the expensive error.
3. Run both replays and compare their reports with the gold A items (`replay_check` in the compliance report).
4. Decide whether to support chapter titles and annex edits in consolidation (the 12 unsupported instructions).
