# KG v2 — implementation report and run guide

This file describes what was built from `docs/KG_V2_PLAN.md`, how it works, how to run it, and where the code deviates from the plan. All LLM steps were tested with a **fake model** (`tests/fake_llm.py`) on the real corpus; **nothing was run against Gemini**. Section 11 lists what must be checked on the first real run.

---

## 1. Overview

| Plan phase | Result | Main code |
|---|---|---|
| 0 Foundations | v2 tables, LLM layer with model rotation and daily budget, resumable step engine, citations | `store/v2tables.py`, `llm.py`, `steps.py`, `citations.py` |
| 1 Extraction v2 | Obligations with deontic/applies_to/required_doc/…, amendment diff replay, titles for provisions | `extract/{schema,context,obligations_v2}.py`, `ingest/titles.py` |
| 2 Accounts and documents | Users, companies, roles, permissions, upload with parsing and bbox, versions, publish | `services/{permissions,accounts,documents,demo}.py`, `company/{pdf_parser,ingest}.py` |
| 3 Profile, filter, triage | Document profile (confirmed by the user), rule filter, triage | `company/profile.py`, `mapping/{docview,filter,triage}.py` |
| 4 Mapping | Hybrid search, locate, judge (whole-document or candidates), multi-edge `COVERED_BY` | `mapping/{search,locate,judging,pipeline}.py`, `retrieval/index.py` |
| 5 Proposals and revision | One proposal per issue, merge into a revised `.docx`/`.md` | `mapping/proposals.py`, `services/revise.py` |
| 6 Ranking, tasks, decisions | Score, task sync and lifecycle, user decisions | `mapping/ranking.py`, `services/{tasks,decisions}.py` |
| 7 UI | Georgian Streamlit app: login, documents, review, board, regulator pages | `app/*` |
| 8 Evaluation | Suites: extraction, triage, compliance (v2), proposals, ops; gold templates | `eval/{extraction,v2suites,gold_v2,runner}.py` |

Design rules that hold everywhere:

* **Every LLM call is a *step*** (`kg/steps.py`): `(step name, subject, inputs, prompt version)` → stored in `step_outputs`. The input hash covers the inputs, so a rerun costs **zero requests**. A rerun after an interrupted run (daily limit) continues where it stopped.
* **Results of code beat results of the model.** Verbatim quotes, ids, dates, numbers, `original_text` are checked or copied by code. A non-verbatim quote is dropped with a note instead of costing a retry.
* **User beats rule beats model.** User rows (`source='user'`) are never overwritten by a pipeline run.
* **The service layer enforces permissions**, not the UI (`services/permissions.py`).

---

## 2. Setup

```bash
uv sync                        # python-docx>=1.1 was added to pyproject.toml → run `uv lock` once
cp ../.env.example ../.env     # the key lives OUTSIDE the repo; the code never prints or reads it into logs
export FINRAG_ENV_FILE=../.env # default: ../.env next to the repo
uv run pytest tests -q         # ~2-4 min; uses the fake model, needs no key
export PYTHONPATH=src          # the package lives in src/ and is not installed: needed for `python -m kg.cli`
```

Every `python -m kg.cli …` command below assumes `PYTHONPATH=src` (run `export PYTHONPATH=src` once per terminal, or
prefix each command with `PYTHONPATH=src`). Without it Python answers `No module named 'kg'`.

Smoke test of the real model (1 request): `uv run python -m kg.cli smoke-llm`.
Probe of Gemma's thinking settings (≈6 requests, no database writes): `uv run python scripts/probe_gemma.py --model gemma-4-26b-a4b-it`.
Size of the corpus documents (decides the whole-document mode): `uv run python -m kg.cli count-tokens`.

---

## 3. Models and the 20-requests-per-day budget

All model choices are in `configs/kg.yaml → llm:`

```yaml
llm:
  single_model: null          # e.g. gemini/gemini-3.5-flash → EVERY step uses only this one model
  models: [gemini/gemini-3.6-flash, gemini/gemini-3.5-flash, gemini/gemini-3.5-flash-lite, gemini/gemini-2.5-flash]
  step_models: {}             # per-step lists, e.g.  judge: [gemini/gemini-3.6-flash]
  daily_request_limit: 20     # per model and day; null = unlimited (billed)
  max_requests_per_run: null  # safety valve for one process
  json_mode: true
  model_overrides: {}         # substring of the model id → {json_mode: false, reasoning_effort: ...}
```

* **Rotation:** a call takes the first model of its list that still has quota today (`quota_day()`), so with four models the day offers 80 requests. When all are spent, `BudgetExhausted` is raised → the CLI exits with **code 2** and prints that the run can be repeated; the UI shows the Georgian message `budget`.
* **One model for everything:** set `single_model`. Nothing else changes. To go back, set it to `null`.
* **A different model for one step:** `step_models: {judge: [gemini/...]}`. Step names: `extract_obligations, amend_diff, doc_profile, triage, locate, judge, propose, merge, qa_answer, compare_summary, classify_doc`.
* **A model with no JSON mode (e.g. Gemma):** `model_overrides: {"gemma": {json_mode: false}}`. The prompts ask for JSON anyway and the parser tolerates fences/prose. Whether Gemma follows the schema well is open (§11).
* **Unlimited:** `daily_request_limit: null`.
* Requests per phase are bounded by batching: `mapping.triage_batch 60`, `locate_batch 40`, `judge_batch 8`, `proposal_batch 5`, `extraction.obligation_batch / diff_batch`. `--dry-run` on `extract-obligations` and `map` prints the upper bound **before** spending anything.
* Responses are also cached on disk (`paths.llm_cache`), keyed by model + messages, so identical calls are free even when the step table was cleared.

### 3.1 Generation options and errors

* `model_overrides` (substring of the model id) and `step_options` (step name) set `json_mode`, `reasoning_effort`,
  `thinking_level` and `timeout_s`; order: defaults < model_overrides < step_options.
* `thinking_level` (Gemma 4, Gemini 3): `minimal` = thinking off, `high` = on. It is sent as
  `generationConfig.thinkingConfig.thinkingLevel` through LiteLLM's `extra_body`, because LiteLLM turns
  `reasoning_effort` into a thinking *budget* for every non-Gemini-3 model and Gemma rejects a budget (HTTP 400).
* The LLM cache key contains the model and these options: changing the model or the thinking level asks the model
  again. Stored **step** results (`step_outputs`) are still reused until the step runs with `--force`.
* Errors: 429 → quota handling (per-minute limit: wait, daily limit: next model). 5xx, overload, timeouts → waits of
  `server_error_backoff_s` (default 5, 15, 30, 60, 90 s, ±15 %, or the provider's own retry hint), then the next
  model, then `LLMUnavailable` (CLI exit code **3**; repeat the command later). HTTP 400 and unknown errors →
  `LLMRequestRejected` at once (exit code 1) with the provider's message — repeating the same request cannot help.
* `request_timeout_s` (default 300) caps one request.
* Pacing: `tokens_per_minute` (per model through `model_overrides`; Gemma 4 free tier: 16,000 input tokens/min).
  Before each request the input tokens sent to that model in the last 60 s are summed (estimate: characters /
  `chars_per_token`, 2.0) and the call waits until the next one fits. A request larger than the whole limit stops at
  once with a message naming the batch setting to lower. Refused attempts count in the window too.
* 429 handling: only a 429 naming a daily quota (`…PerDay…`), or our own counter at `daily_request_limit`, marks a
  model as used up for today. Any other 429 waits `rate_limit_wait_s` (65 s) or Google's retry hint, up to
  `rate_limit_retries` (5) times, then the next model / `LLMUnavailable` (exit 3). The full 429 text is logged.
* `reset-quota [--model ID]` clears today's "used up" flag (e.g. after a wrongly detected limit).
* Progress: every finished LLM step writes a `step_done` line to the run log (`data/kg/runs/<run>.jsonl`).

### 3.2 Gemma 4 on the Gemini API (findings, 21.10.2026)

* `gemini/gemma-4-31b-it` and `gemini/gemma-4-26b-a4b-it` work with our prompts; system instructions and JSON mode
  are accepted. AI Studio quota for each: 14,400 requests/day, 30/minute → `daily_request_limit: 14400`.
* A thinking budget is refused (400) → `model_overrides: {"gemma": {reasoning_effort: null, thinking_level: minimal}}`.
  With nothing sent, Gemma thinks (default on).
* Reliability that afternoon: 31B 1 of 8 probe requests answered (500 / 503 "high demand"); 26B 7 of 8. Errors and
  long waits come from load on Google's side, hence the retry policy above.
* Speed (26B): ≈2.5 s with thinking off, ≈13 s on, for a tiny task; the real `profile` step (11 K tokens in, 3.8 K
  out, thinking on) took 84 s. Answers were valid JSON, quotes verbatim; Georgian summaries had spelling slips
  (`განსაზღვრს`).
* With thinking off the 26B model wraps its JSON object in a list (`[ {...} ]`); the parser unwraps a one-element list.
* `max_requests_per_run: 60` is a safety valve while testing; set it to `null` for full runs.
* The amendment-diff prompt for 4 units was ≈14,900 tokens and was refused repeatedly (429) under the 16K/min
  limit → `extraction.diff_batch: 1` (largest prompt ≈11,800 tokens). The whole-document judge (up to
  `mapping.whole_doc_max_tokens` 30,000) cannot fit either: it is now 9,000 (document text), bigger documents use the candidates mode.

Typical day-by-day plan with 20 requests per model: run the command, if exit code is 2 wait until the next day (or add a model), run the **same command** again.

---

## 4. Data flow

```
regulation (Matsne)                                      company PDF
 parse-acts → parse-amendments → consolidate → extract-refs        │ upload (parse, bbox, sha1)
 (no LLM)  rebuild-graph                                            ▼
        │                                                 draft document version
 extract-obligations (LLM: extract + amend diff)                    │ profile (LLM) → user confirms
        │  Obligation nodes, SUPERSEDES, IMPOSES                    ▼
        └───────────────────────────────►  filter (rules, no LLM) → triage (LLM) → locate (LLM)
                                            → judge (LLM, whole document or candidates)
                                            → propose (LLM) → sync tasks (rules)
                                                     │
                                       review UI / board / decisions → revise (LLM merge) → publish
```

### 4.1 Regulation side (once, then after each new amendment)

1. `rebuild-graph` — parse acts, amendments, consolidated versions, references (no LLM). Provisions get `title`/`display` (`ingest/titles.py`).
2. `extract-obligations` — per extraction unit (a paragraph with its sub-points): **extract** for the first version, **diff** for each later version (old text + old obligations + new text → unchanged / modified / new / removed). The graph (`Obligation`, `ExtractionUnit`, `IMPOSES`, `APPLIES_TO`, `SUPERSEDES`) is a pure function of the stored step outputs: the whole act is replayed from `step_outputs` on every run, so it is free to repeat and resumable. Obligation id: `obl:<act>/<unit path>#<k>@<date>`.
   * `--dry-run` request estimate, `--force` ask again, `--retry-invalid` retry steps whose validation failed, `--legacy` v1 extraction.
   * Obligation fields: statement, deontic (`must|must_not|may`), subject, applies_to, action, condition, exceptions, deadline_or_frequency, severity, category, domain, required_doc {doc_types, basis explicit|inferred, quote}, evidence_type, location_hint, depends_on, source_path, citations.
   * Validators check ids, verbatim quotes, that every old obligation is handled in a diff. Hard problems → one retry with the problem list shown; `note:` problems never retry.
   * If the database was reset, `rebuild-graph` followed by the (free) `extract-obligations` restores the obligations from `step_outputs`.

### 4.2 Company side (per document version)

**Upload** (`services/documents.py`): sha1 duplicate check per company; `new_document` or `new_version` with policy `keep` (old stays) or `replace` (old becomes `replaced` when the new one is published, its open/accepted tasks become `replaced`). The PDF is parsed with bounding boxes; the version is a `draft`. A regulator sees **published** versions only; an entity acts only on its own company.

**Profile** (`company/profile.py`): LLM proposes doc type, domains (from `domains_available`), purpose and per-section summary/goal; falls back to a parser profile if the model fails. The **user must confirm** (and may edit) before mapping (`pipeline` refuses with "profile not confirmed"). Edits are recorded as `profile_edit` decisions.

**Filter** (`mapping/filter.py`, no LLM) drops obligations that cannot apply: wrong entity type, not yet valid / already ended at the document date, `may` (permission-only), required document type that the profile does not match when the requirement is **explicit**. Every dropped obligation has a reason in the run log. The document date `V_doc` = `effective_date`, else `doc_date`. Cause of a gap: `amendment` if the obligation's `valid_from` > `V_doc`, else `pre_existing`; no date → `pre_existing`.

**Triage** (`mapping/triage.py`): per document, obligations in batches → `relevant | possibly | irrelevant` with a reason. Explicit requirements are never dropped as irrelevant by the model (code guard); user rows are preserved.

**Locate** (`mapping/locate.py`, optional `mapping.locate`, default true): the model sees section digests (not clauses) and names sections where each obligation would belong. `mapping.max_located_clauses` (default 12) caps the clauses passed on.

**Judge** (`mapping/judging.py`) — two modes, chosen by size (`mapping.whole_doc_max_tokens` 30000, `chars_per_token` 2.2; all 7 corpus documents fit):
* *whole*: the full document text is in the prompt;
* *candidates*: located sections + hybrid search (BM25 char 4-grams, optional dense, fused with RRF; `search_multi` fuses the obligation text and its source provision).

Result per obligation: verdict `full | partial | contradicts | none` (or `undecided` if the model's item is invalid) and **edges** `COVERED_BY(obligation → clause)` with role `full|partial|contradicts`, quote and why. Edge id `oid|COVERED_BY|clause|source`, props `{role, coverage, source: llm|user, doc_version, run, output_id, quote, why}`. Rules in code (`final_verdict`): a `contradicts` edge → contradicts; `full` needs a full edge; `none` with edges → partial. A user role `rejected` means "this clause does not cover it" and is shown to the model on re-check.

**Propose** (`mapping/proposals.py`) — one proposal per issue (partial/contradicts/none):
* `adjust_clause` — the clause of the best edge; `original_text` is **copied by code**;
* `add_section` — anchor = last clause of the first located section, else the document's last clause if triage was `relevant`;
* `new_document` — triage `possibly` and nothing located.
The flag `ungrounded_numbers` is set when numbers in the proposal appear neither in the obligation, its source provision nor the original text (the UI warns).

**Tasks** (`services/tasks.py`, §6) and **decisions** (`services/decisions.py`, §7). **Revise** (`services/revise.py`): accepted proposals → `merge` step when several touch one clause → `.docx` (python-docx, changes highlighted) and `.md` under `uploads/<company>/revised/`. **Publish**: refused with open tasks unless forced.

---

## 5. Ranking

Current setting (21.10.2026): **`score = gap × relevance`** — the model's severity was too uniform (95 of 147
obligations "high"), so its weights in `ranking.severity` are all 1 and it does not influence the score.

* gap: contradicts 1.0, none 1.0, partial 0.6 · relevance: relevant 1.0, possibly 0.6
* priority: score 1.0 → critical, 0.6 → medium, 0.36 → low (`priority_thresholds: {critical: 0.95, high: 0.95,
  medium: 0.55}`; "high" is unused for now).

Severity is still extracted and stored; giving `ranking.severity` different weights again (e.g. 4/3/2/1 with
thresholds 3.5/2.4/1.2) brings it back. `explain-task` / the board show the factors; the amendment cause is a
**badge and filter only**. Changing weights then `recompute` re-scores everything without any LLM call.

## 6. Tasks

Task key = `company | <document version id> | <lineage root of the obligation>` (new-document tasks use `__company__`). Statuses: `open, accepted, not_relevant, already_addressed, resolved, replaced`. `sync_tasks` (run after propose, after every decision, by `recompute`):

* creates a task for every proposal; updates title/score/priority of open tasks;
* a task closed **by the user** (not_relevant / already_addressed) reopens only through an amendment (`reopened_by_amendment`);
* `resolved`/`replaced` tasks reopen when the gap appears again (`reopened`);
* a task whose proposal vanished (gap closed) → `resolved` (`auto_resolved`);
* a newer version of the same document resolves the older version's tasks (when the gap is gone there);
* every change writes a `task_events` row. Assignment (user/role, due date) and comments are events too.

---

## 7. User decisions (`services/decisions.py`)

Every decision is a row in `user_decisions` **and** takes effect immediately in code:

| action | effect |
|---|---|
| accept | task `accepted`; proposal enters the revised text |
| not_relevant (reason required) | triage row = irrelevant (source user), task `not_relevant` |
| already_addressed (clauses required) | user `COVERED_BY` edges (full), coverage verdict `full` (source user), task `already_addressed` |
| reopen | undoes the three above |
| edge_add / edge_change / edge_remove | user edge (removal writes a `rejected` role edge); the verdict is **recomputed by rule**, tasks re-synced, no LLM |
| recheck (pipeline `recheck_obligation`) | asks the judge again for one obligation; the model sees the user's edges |

---

## 8. Running it

### 8.1 Command line (`uv run python -m kg.cli <command>`; `--config FILE` before the command)

Regulation side:
```bash
rebuild-graph                         # no LLM
extract-obligations --dry-run         # request estimate
extract-obligations                   # repeat the same command after exit code 2
```
Company side — demo data (company `bank-a`, users `bank@demo.ge` / `nbg@demo.ge`, password `demo1234`, 7 published documents):
```bash
seed-demo                             # prints the 7 document version ids, e.g. doc:bank-a/POL-RM-014@3.0
profile  <version> --as bank@demo.ge                  # build the profile (1 request)
profile  <version> --as bank@demo.ge --confirm --domain model_risk
map      <version> --dry-run --now 2025-01-01         # request upper bound per step, filter result
map      <version> --now 2025-01-01                   # triage + locate + judge
propose  <version>
sync-tasks <version>
# or everything in one go (stops with a message if the profile is not confirmed):
pipeline <version> --as bank@demo.ge --confirm-profile --domain model_risk --now 2025-01-01
tasks --as bank@demo.ge [--status open --priority high --cause amendment]
explain-task "<task key>" --as bank@demo.ge
revise  <version> --as bank@demo.ge                    # accepted proposals (or --all-proposals) → .docx/.md
publish <version> --as bank@demo.ge [--force]
recompute [--company bank-a]                           # re-score and re-sync, no LLM
upload file.pdf --as bank@demo.ge [--new-version-of bank-a/POL-RM-014 --replace]
create-user --email … --name … --password … --type entity --company-name … --entity-type commercial_bank
```
Exit codes: 0 ok, 1 expected error (message printed), 2 daily budget exhausted, 3 model unavailable (server errors).
`--now` fixes "today" (reproducible runs). Re-running any command with the same inputs costs **0 requests**. Expected errors (permission, unknown user or document version, document problems, a request the model rejects)
print a message and exit 1; budget exhaustion exits 2; a model that keeps failing exits 3.

`status` shows node counts, users/companies, document versions and tasks by status, step reuse, recent runs, and the model list with the remaining budget of the day. `migrate` creates/updates the v2 tables (idempotent).

### 8.2 Web app

```bash
uv run streamlit run app/kg_app.py
```
Georgian UI (`app/i18n_ka.py`). Login/registration (an entity creates a company or joins one; a regulator may be gated by an optional `auth.regulator_code` in the config — absent = open).
Entity menu: **My documents** (families/versions, upload, profile confirmation → *Confirm and check* runs map/propose/sync with progress), **Document review**, **Task board**, **Company profile** (details, roles, members), then Q&A, version comparison, graph, evaluation.
Regulator menu: **Company search** → published documents → read-only review and board, plus the regulation pages.

*Review page:* left, the document in an HTML frame; clauses with issues are coloured (red contradicts, orange partial, blue = where a missing section should be added, green covers) and **hovering a clause shows the cards** (obligation, explanation, quote, regulation link). Right, proposal cards sorted by score: diff of original vs proposed text, accept / not relevant (with reason) / already addressed (choose clauses) / edit edges / re-check / reopen. Below: *Build revised text* (download `.docx` and `.md`), publish (with a force checkbox when tasks are open) and discard-draft. Collapsed lists: irrelevant, fully covered, undecided obligations. Only a **draft** of the user's own company is editable; published versions and every regulator view are read-only.
*Board:* columns open / accepted / closed / done, filters (priority, cause, document, only-new), detail panel with ranking factors, decisions, history, assignment and comments.

*Background check (შემოწმება):* confirming the profile in the upload window starts the check in its own process
(`python -m kg.cli check <version> --as <email>`, `services/jobs.py`); the window closes and the UI stays usable. The
`check_jobs` table holds its status (queued → running → done, or budget / unavailable / rejected / failed with the
message); document tiles show it with progress (triage / judged / proposals) and refresh every 15 s while it runs, and
the review page (განხილვა) marks partial results. A stopped check is started again from the tile's *Versions* menu
and continues where it stopped. The process log is `data/kg/runs/check-<version>.log`.

The replay (amendment impact) page of v1 was removed as planned; its function is now "new obligation from an amendment → task with cause *amendment*".

---

## 9. Evaluation

```bash
eval-templates                                 # writes evaluation/gold/obligations_v2.csv and amend_diff_v1.csv
eval --suite extraction|triage|compliance|proposals|ops [--version <id>]
```
Results go to `evaluation/results/<run id>/{metrics.json,items.csv,report.md}` and `index.csv` (shown on the Evaluation page).

* **Gold templates** (`eval/gold_v2.py`): ~20 units of the main act pre-filled with the unit text (the 6 units changed by amendments first, then one unit per feature: definition, exception, reference, sub-points, deadline) — **the team fills the obligation columns**; a unit without obligations gets `no_obligations=1`. Existing files are never overwritten. `amend_diff_v1.csv` has the old/new text of the changed units; fill `old_statement`, `new_statement`, `expected_op` (`unchanged|modified|new|removed`).
* **extraction**: recall/precision per labelled unit, attribute accuracy (deontic, severity, required-doc basis, evidence type), precision of *explicit* requirements, diff-operation accuracy. Statements are matched by stem overlap (`eval/extraction.py: MATCH = 0.4`); a judge-prompt match can replace it later. With no labels it reports `no gold labels yet`.
* **triage**: for each gold A/B item, some obligation on its article is `relevant`/`possibly` for that document → triage recall; share of `irrelevant`.
* **compliance** (adapted): findings are built from `coverage` rows (partial/contradicts/none), expanded to one per judged clause; matched to `answer_key_gold_labels.csv` with the unchanged matcher. Class A = gap from an amendment, B = base act. Same metrics as v1 (recall A/B, precision, C false-positive rate, version attribution). Falls back to v1 findings if no coverage rows exist.
* **proposals**: writes a reproducible sample (20, seed 7) to `evaluation/gold/proposals_review.csv`; a reviewer fills `verdict` (`correct|partly|wrong`); rerunning reports the shares and the `ungrounded_numbers` flag rate.
* **ops**: requests, tokens, latency, cache hits per step.

**Ablations** (plan §15.3): copy `configs/kg.yaml` per variant and run `pipeline … --force` + `eval --suite compliance` with `--config`:
(a) default (locate on, whole-document judge); (b) `mapping.locate: false`; (c) `mapping.whole_doc_max_tokens: 0` (forces candidates mode); (d) `retrieval.dense.enabled: true`; (e) extraction v1 (`extract-obligations --legacy`). A triage on/off toggle was **not** added. Each variant needs real requests, so plan them against the daily budget.

---

## 10. Where the implementation differs from the plan

1. **Task key uses the document *version*, not the family**, so `keep`-mode versions keep their own tasks; the "newer version resolves older tasks" rule restores the family behaviour.
2. **Quotes are soft-verified:** a non-verbatim *edge* quote is dropped with a `note:` and the edge stays (no retry spent).
3. **`may` (permission-only) obligations are dropped by the filter** (nothing to comply with).
4. **Verdict `undecided`** for judge items that stay invalid after the retry; they appear in the "undecided" list and are retried with `--retry-invalid`.
5. **Edge role `rejected`** (user) added so that a removed edge is remembered and the model is told.
6. **`mapping.locate` flag** (default true) and `mapping.max_located_clauses` (default 12) were added to allow the ablation.
7. **`not_relevant` is not carried over** to a new version of a document (the user is asked again); only the pipeline's own rows are re-derived.
8. **v1 modules kept** (`map-coverage`, `replay`, `check-doc`, v1 `findings`/`alerts` tables) until v2 is validated; the plan's removal is a one-commit clean-up (§12).
9. **Regulator self-registration** is open unless `auth.regulator_code` is set.
10. **Compliance eval** drops v1's `replay_check` (replay removed).

---

## 11. Not verified / to check on the first real run

1. **Real Gemini calls** — only the fake model ran. Start with `smoke-llm`, then `extract-obligations --dry-run`, then one document with `map --dry-run`.
2. **Model ids** in `llm.models` (`gemini/gemini-3.6-flash` etc.) must exist for your key; `step_models`/`single_model` fix that quickly.
3. **Gemma** — see §3.2. Open: answer quality of the 26B model on the big steps (extraction, amendment diff, judge)
   with thinking off vs on (`step_options`), and Georgian spelling in proposals.
4. **Whole-document threshold** (30000 tokens): the char/token ratio 2.2 is an estimate; use `count-tokens --model …` and adjust. Compare whole vs candidates on the gold items (§9 ablation c).
5. **Embedding model:** dense retrieval is off (`retrieval.dense.enabled: false`). Options: local BGE-M3 (`provider: local`) or `provider: litellm` with a Gemini embedding id (the embedding cache key includes the model name). Choose by candidate recall on the gold items.
6. **Matsne deep links** in the hover cards use the stored provision URL; check that they open the right article.
7. **.docx rendering** — generated with python-docx (highlighted changes); open a sample in Word once. Page layout of the original PDF is not reproduced; the revised file is rebuilt from parsed units.
8. **Hover cards** are CSS-only inside an iframe: they work with the mouse, not on touch screens, and a card near the bottom of the frame may be clipped.
9. **Gold labels** for extraction/diff are templates only — extraction metrics appear after the team labels them.
10. **Streamlit** `st.iframe` is used when available (`components.html` otherwise).

---

## 12. Housekeeping

* `uv lock` (python-docx added to `pyproject.toml`).
* Delete `data/kg/_to_delete/` and the zero-byte `data/kg/kg.sqlite-journal` left by earlier moves (the sandbox could not delete them).
* `data/uploads/` is git-ignored (uploaded PDFs, revised files).
* After validating v2 on real data: remove the v1 modules and tables named in §10.8.
* Nothing was committed to git; all files are in the working tree.

---

## 13. Tests

`uv run pytest tests -q` (fake model, real corpus; 120 tests passed when run in three chunks — the whole suite takes several minutes). Files: `test_v2_phase0/1` (store, LLM layer, steps, extraction), `test_v2_phase2` (accounts, permissions, documents), `test_v2_phase3` (profile, filter, triage), `test_v2_phase4` (search, locate, judge, pipeline), `test_v2_phase5` (proposals, revision), `test_v2_phase6` (ranking, tasks, decisions), `test_v2_cli` (every command, resumability, model switching), `test_v2_ui` (Streamlit AppTest: login, pages for both user types, decisions, publish, registration), `test_v2_phase8` (eval suites). The older v1 tests remain.
Key properties tested: reruns cost 0 requests; budget exhaustion is resumable; permissions (regulator sees only published, entity only its company); user rows survive reruns; decisions change verdicts by rule.
