# Compliance Copilot: the graph and the tool layer

What the agent can see, how the data is shaped, and which tool answers which question. The tools live in
`src/kg/agent/tools.py`; the read tools are filtered by the signed-in user's company (`Scope`); the only write tools only *propose* (section 5).

## 1. What the database holds (measured on `data/kg/kg.sqlite`)

**Regulations (public, shared by every company)**

| Node | Count | What it is |
| --- | --- | --- |
| `Act` | 4 | A regulation (`act:4964423`, short name, number, adopted date). The tools take the number (`4964423`). |
| `Provision` | 1207 | A place in an act (`prov:<act>/<path>`, e.g. `art5/p10/sა`). Levels: article, paragraph, sub. |
| `ProvisionVersion` | 1350 | The dated text of a provision (`pv:<act>/<path>@<date>`), `valid_from` / `valid_to`, `change_type` added / modified / repealed. |
| `AmendingAct` | 23 | An order that changed an act: number, date, `effective_from`, `base_act`. |
| `Obligation` | 949 (106 superseded) | One duty extracted from a provision version: statement, subject, action, `condition`, `exceptions`, `deadline_or_frequency`, severity, category, `domain`, `applies_to` (entity types), source citations with quotes, `valid_from` / `valid_to`. |
| `EntityType`, `ExternalAct`, `ExternalProvision`, `ExtractionUnit` | | Entity types, laws the regulations refer to, pipeline bookkeeping. |

**Company data (private)**

| Node / table | What it is |
| --- | --- |
| `InternalDoc` (id = document version, `doc:<company>/<code>@<version>`) | One uploaded document version. |
| `Clause` | A clause of a document version (`cl:<doc>/<section>`): text, `section_no`, `unit_type` (clause, heading, table, ...). |
| `documents`, `document_versions`, `doc_profiles`, `document_directions` | Families, versions (status draft or published, `+N` = edited copy), type and topic. |
| `coverage` | The check result per (document version, obligation): verdict (`full`, `partial`, `none`, `contradicts`), `missing`, `rationale`, cited provision versions, `cause` (`pre_existing` or `amendment`) and the `amendment_id`. |
| `triage`, `locations` | Why the obligation is relevant (`relevant`, `possibly`, `irrelevant`) and where the model looked in the document. |
| `tasks`, `proposals`, `user_decisions`, `task_events` | The flag as a task (status, priority), the proposed fix, the person's decisions. |
| `companies`, `company_roles` | The profile. Only the entity type is structured (`props` is empty today). |

**Edges**

```
Act -HAS_PROVISION-> Provision <-VERSION_OF- ProvisionVersion -IMPOSES-> Obligation -APPLIES_TO-> EntityType
ProvisionVersion -UPDATES-> older ProvisionVersion        ProvisionVersion -AMENDED_BY-> AmendingAct -AMENDS-> Act
Provision -REFERS_TO-> Provision | Act | ExternalAct       (ref_text, resolved_to)
Obligation -SUPERSEDES-> older Obligation                  (change, what_changed, amendment)
Obligation -COVERED_BY-> Clause <-HAS_CLAUSE- InternalDoc  (role = coverage = full | partial | contradicts, quote, why,
                                                            doc_version, run)
```

Things worth knowing:

* The check verdicts are **also** on the `COVERED_BY` edges (`role`), but only for mapped clauses. "None" verdicts and
  the relevance of an obligation live only in the `coverage` / `triage` tables.
* A check cites the provision versions it was made against (`coverage.citations`). That is the law date of the check,
  so staleness is computed, not stored: a check is stale when a cited provision got a newer version after the check.
* `coverage.cause = 'amendment'` marks flags that exist because of an amendment (its id is in `amendment_id`).
* Obligations of **every** company hang on the same public nodes, so `COVERED_BY` from an obligation leads to clauses
  of all companies. Every tool filters clauses by the signed-in company; the model never sees a count of hidden nodes.

## 2. Tools

| Group | Tool | Answers |
| --- | --- | --- |
| Orientation | `list_acts` | Which regulations exist, their ids and domains. |
| | `list_documents` | Our documents, versions, status, verdict counts, when checked. |
| | `company_profile` | Entity type, roles, known facts, document topics. |
| Regulation | `list_provisions` | The outline: what is under article 5. |
| | `provision_text`, `provision_history`, `compare_versions` | What it says on a date, all versions with the amending acts, what changed between two dates. |
| | `recent_amendments` | Amending acts, newest first, how many provisions each changed. |
| | `search` | Provisions or obligations by words. Obligations are limited to our entity type; `domain` filters ("only model risk"). |
| | `obligation_details` | Entity types, condition, exceptions, deadline, source text with quote, earlier and later versions. |
| | `read_text` | The full text of a provision version, clause or obligation (so a quote can be verified). |
| Our documents | `document_flags` | The check results of a document, worst first, with the clauses involved. |
| | `document_clauses` | The clauses of a document. |
| | `clause_coverage` | One flag in full: verdict, reasoning, mapped clauses with quotes, task, proposed fix, cause. |
| | `clauses_for_obligation` | Where else in our documents it is covered. |
| | `clauses_for_provision` | Provision -> obligations -> our clauses, in one call. |
| | `coverage_gaps` | Obligations for our entity type that no document covers, per domain. |
| Change | `document_law_changes` | When the check ran, which provisions it relied on changed since, which flags come from an amendment. |
| | `affected_documents` | Our documents touched by law changes (since a date, per act). |
| Graph | `get_node`, `neighbors` | Any node and its links, with the edge details. |
| Propose (entity users only) | `propose_dismiss_flag`, `propose_mark_covered`, `propose_mapping_change`, `propose_wording`, `propose_profile_fact`, `propose_extraction_report` | Each stores a pending card; nothing changes until the person confirms (section 5). |

## 3. Use case to tools (design document, section 7)

| Use case | Steps |
| --- | --- |
| Applicability check | `obligation_details` (entity types, condition, exceptions, source) + `company_profile`; one question only for an unknown that changes the verdict. |
| Challenging a flag | `clause_coverage`, then by the type of argument: *covered elsewhere* `clauses_for_obligation` + `read_text`; *does not apply* `obligation_details`; *wording is enough* `read_text` of clause and source; *the law changed* `document_law_changes`, `provision_history`, `obligation_details`. |
| Guided graph exploration | `neighbors` (REFERS_TO), `clauses_for_provision`, `provision_history`, `compare_versions`, `list_provisions`, `coverage_gaps`; the cursor follows the visited nodes. |
| Documents view | `affected_documents`, `list_documents`. |
| Iterative redrafting | `clause_coverage`, `read_text`, `obligation_details`, then `propose_wording` (again for each instruction). |

Items `prop:<n>` (a suggestion of the check, e.g. a new section that is not in the original yet) can be pointed at in the panel and read with `read_text`.

(Challenging a flag ends in `propose_dismiss_flag` / `propose_mark_covered`; applicability in `propose_profile_fact` and
`propose_dismiss_flag`; a wrong mapping in `propose_mapping_change`.)

## 4. Rules that hold for every tool

* Tenant and user come from the session, never from the model's arguments.
* Public regulation nodes are visible to everyone; a company's documents and clauses only to that company (a regulator
  sees published versions only). Unknown node types are refused.
* Everything a tool returns is recorded for grounding: the answer may cite only ids returned in the same turn, and a
  quote counts only if it occurs in the recorded text.
* Results are capped at 5000 characters; lists are trimmed from the end.

## 5. Confirmed writes (`src/kg/agent/actions.py`)

The agent proposes, the person commits. A `propose_*` tool validates and stores a row in `agent_actions` (status
`pending`: what will be written, before and after, the reason) and returns. The panel shows it as a card with an editable
reason, **Confirm** and **Cancel**. The click calls `actions.confirm`, which runs the same service as the normal screens
(`decisions.not_relevant`, `already_addressed`, `edge_add/change/remove`, `accounts.update_company`) with the same
permission checks. The model has no way to reach `confirm`.

| Action | Tool | On confirm | Undo |
| --- | --- | --- | --- |
| Close a flag (not applicable / clause is enough) | `propose_dismiss_flag` | `decisions.not_relevant` with the reason | `decisions.reopen` |
| Flag already covered by clauses | `propose_mark_covered` | `decisions.already_addressed` | `decisions.reopen` |
| Change the mapping (bulk allowed, max 10, all items on the card) | `propose_mapping_change` | `edge_add` / `edge_change` / `edge_remove` (removal is soft: a `rejected` user edge) | the inverse operations |
| Revise the suggested wording (redrafting) | `propose_wording` | replaces the text of the flag's suggestion (`proposals`) and drops a hand edit of the old text; the person still accepts, rejects or edits it in the review. Numbers that are not in the law, the document or the conversation are flagged | restores the old wording and the hand edit |
| Save a profile fact | `propose_profile_fact` | `company.props.facts[<id>]` | restores the previous facts |
| Wrong extraction | `propose_extraction_report` | a row in `extraction_reports` (queue for the platform team) | marked `withdrawn` |

Rules enforced in code: only an entity user proposes or confirms; the company comes from the session; every id in a
proposal must have been read in the same turn; only drafts can be changed; at most 3 proposals per answer; a newer
proposal on the same target cancels the older pending one; a proposal expires after 30 minutes;
a **fingerprint** of the flag / mapping / profile is compared at confirm time (changed meanwhile -> `stale`, ask again)
and before an undo; every confirm writes `user_decisions` (kind `chat_action`: reason, before, after, thread, message and
the context snapshot of the question). A mapping change says the obligation needs re-checking; a new check run keeps the
old one. Obligations themselves are never edited (they are shared): "does not apply to us" closes the flag in this
document only.
