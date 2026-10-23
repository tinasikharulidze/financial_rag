# Compliance Copilot Chat: Design Overview

Status: draft v1. Covers UI placement, context model, and use cases. Agent tools and system prompt are out of scope for this document.

## 1. Purpose

The platform maps public Georgian financial regulations to extracted obligations, then maps and checks a company's internal documents clause by clause against those obligations. Flags, reasoning, and citations already exist as popups, and proposed fixes go through the accept, reject, or edit flow.

The chat agent extends this by answering questions that move across the knowledge graph (provisions, provision versions, articles, amending acts, obligations, documents, clauses). It does not duplicate the popups. Its value is in questions the current UI cannot answer: comparison across versions, impact of amendments, gap analysis, and navigation of the regulation.

## 2. Where it lives

A single app-wide panel docked on the right, collapsed by default.

- Present on every screen (documents view, document analysis view), with the same thread history. Only the context changes per screen.
- Collapsed: a slim rail or top-bar button, so the two-pane diff keeps its space.
- Open: a third column that pushes the layout instead of overlaying it, so highlighted mistakes stay visible while the user asks about them. On narrow screens it falls back to an overlay.
- Resizable, and the user can collapse one document pane while chatting.

Two ways to open it:

1. **Top bar:** for general questions (for example, what changed in this act since January).
2. **"Ask follow-up" in the existing flag popup:** the chat opens with the clause, its obligation, and the popup reasoning attached. This is the primary entry point, since the user is already looking at the item.

## 3. Output principle

The panel gives short answers with clickable citations. Anything large or stateful opens in the main workspace instead of being pasted into chat:

- Version comparisons reuse the two-pane diff view, with a summary in the panel.
- Lists and reports (affected documents, gaps) open as a filterable results view.
- Drafted text appears as suggestions in the existing accept, reject, or edit flow. The agent never applies edits directly.

## 4. Context model

Every view sends IDs, labels, and small UI state, never document text. The agent retrieves content through tools, which keeps prompts small, makes lookups auditable, and keeps tenant checks in one place.

**Common envelope:** tenant, user, entity type, current view, a single `focus`, and a list of `chips` (attached items, each with an origin: view, popup, or user).

**Documents view** sends filters, sort, selected document IDs, result count, and a status summary. The agent does not receive the rows themselves.

**Document analysis view** sends document and version IDs, an optional compared version, the check run ID, the regulation as-of date, a staleness flag, the selected clause, the open flag, and a suggestion summary.

**Rules**

- Focus precedence: open flag, then selected clause, then document version. Less specific items stay as chips.
- Chips are visible at the top of the chat and editable by the user.
- Navigating within a document updates focus and inserts a small context divider in the thread. Switching documents prompts a new thread.
- The context object is stored with every user message, giving an audit record and reproducible answers.

## 5. Versioning and staleness

Each compliance check is stored against the regulation versions in force at the time it was run (the check date is the as-of date). Consequences:

- A check run is a historical, auditable fact. A re-check always creates a new run and never overwrites the old one.
- A run is **stale** when any provision version it relied on has since been superseded. The UI shows a badge (for example, checked against law as of 3 March, 2 provisions amended since), which also opens the chat with the question of what changed and whether it afA change in a clause's status has two possible causes: the document was edited, or the law changed. The agent separates them by comparing runs along one axis at a time (documefects the document.fects the document.
- nt version A vs B at the same law version, or the same document at law date A vs B).
- Obligations are extracted per provision version, so obligation-level diffs between law versions are possible.

## 6. Scope and boundaries

The current screen is the default anchor, not a wall. Questions are handled in four tiers:

| Tier | Example | Handling |
| --- | --- | --- |
| In context | Why is this clause flagged | Answer directly |
| Adjacent (graph neighbors) | Previous version, amending act | Answer and show consulted nodes |
| Elsewhere in workspace or corpus | Another policy covering the same obligation | Answer, state the focus is leaving, offer to switch |
| Out of domain | Unrelated requests | Decline politely and redirect |

Hard limits are enforced in the data layer, not the prompt: every graph query is filtered by tenant and entity, so no question can retrieve another tenant's data. The prompt carries only the soft domain scope.

## 7. Use cases

**Use cases**

Each case needs a conversation: the agent asks the user for missing input, or the next question depends on the last answer. Anything a fixed screen or an automated alert can do is a normal feature, not an agent use case.

| Use case | Example dialogue | Why it needs a conversation | Where the result lands |
| --- | --- | --- | --- |
| Applicability check | Does this obligation apply to us? | The agent evaluates the obligation's conditions against the company profile and asks only for facts the profile lacks (a specific product, activity, or threshold). It offers to save the answers to the profile. | Verdict (applies, does not apply, unclear) with reasoning and citations in the panel |
| Challenging a flag | I think this clause is fine, our other policy covers it. | The agent re-examines the clause against the user's argument, checking the other document and the cross-referenced provisions. Its verdict can change in either direction during the dialogue. | Flag dismissed with a recorded reason, or kept with a clearer account of what is missing |
| Guided graph exploration | What does this provision refer to? Which of our clauses map to it? When did that part change? | Each follow-up builds on the previous answer. The agent tracks where the user is in the graph and suggests the next hop. | Panel answers with clickable citations, main view moves to the node discussed |
| Iterative redrafting | Fix this clause, but keep our terminology and don't commit us to a 24-hour deadline. | The agent proposes wording, checks it against the obligation, states what it traded off, and revises with each instruction. | Suggestions in the accept, reject, or edit flow |

### Applicability check

Users say: "Does this obligation apply to us?", "We're starting to offer product X, what does that bring in?", "Why is this obligation mapped to our document? We don't do that."

1. Obligations addressed to other entity types are excluded up front, using the structured entity type.
2. The agent reads the obligation's conditions from the provision text and checks each against the company profile: met, not met, or unknown.
3. For an unknown condition that could change the verdict, it asks one question in business terms. If every unknown leads to the same verdict, it does not ask.
4. It gives a verdict (applies, does not apply, unclear) listing each condition, whether the fact came from the profile or the user, and the quoted source text.
5. It offers to save new facts to the profile, or to flag the mapping for review if the obligation does not apply. Both happen only after the user confirms. If an answer contradicts the profile, the agent says so.

Only entity type is structured today, so conditions (activity, thresholds, exceptions) are read from the provision text at question time. Rules for v1: always show the quoted source next to the verdict, present the result as an assessment, and use "unclear" freely. Later option: extract applicability conditions in the pipeline pass that extracts obligations, stored as short statements linked to their source text, so verdicts become repeatable and auditable.

### Challenging a flag

Users say: "This is fine, our onboarding procedure covers it.", "Is this actually mandatory, or just recommended?", "Even if it's missing here, is it covered anywhere else?"

1. The agent restates the flag in a sentence: the obligation, what the clause says, and what was found missing or contradicting.
2. It treats the user's argument as a claim to test and identifies its type: covered elsewhere, does not apply to us, the wording is sufficient, or the law changed.
3. It tests that type against the graph. Covered elsewhere: clauses in other documents mapped to the same obligation. Does not apply: the applicability check above. Wording is sufficient: the clause re-read against the obligation. Law changed: the law version used for the check compared with the version in force now.
4. It answers plainly: the flag looks wrong and why, partly right, or the gap stands and what exactly is missing. It changes its verdict only on new evidence, and cites that evidence for any reversal.
5. If it agrees, it proposes dismissing the flag with a recorded reason, and the user confirms. If the coverage sits in another document, it may suggest a cross-reference in this clause.

### Guided graph exploration

Users say, in a chain: "What does this provision refer to?" Then "Which of our clauses map to it?" Then "Only AML." Then "When did that part change?" Then "What did it say before?"

1. The agent keeps a cursor: the current node and the trail of where the user has been.
2. It resolves phrases like "that part" or "those" to specific nodes and shows them as chips, so the user can catch a wrong guess.
3. It answers briefly, then offers two or three sensible next steps: the nearest amendment, a related obligation with no mapped clause, a cross-referenced article.
4. When a follow-up leaves the current area of the graph it says so, and it moves the main view to a node when the user asks to see it.

### Shared behavior

- Every answer is grounded in graph nodes and cited.
- The agent asks only questions that can change the outcome.
- It is read-only by default. Saving to the profile, dismissing a flag, or creating a suggestion always needs the user's confirmation.

**Suggested questions per view** appear in the panel to surface these use cases (for example, in the documents view: which documents are affected by recent amendments).

## 8. Design principles

- The chat does not repeat popup explanations. It handles what goes beyond them.
- Scope is explicit and visible through chips.
- Agent output goes through the existing review flow, so everything stays traceable.
- Humans validate. The agent proposes and the user confirms.

## 9. Implementation

One agent loop with tool calling, a server-side tool layer, and a strict split between reading (free) and writing (confirmation-gated). No multi-agent framework is needed.

### Architecture

- The right-hand panel sends the message and the context object to a streaming chat endpoint.
- The backend runs a plain agent loop: model call, tool calls, results, repeat, with a step cap.
- The tool layer is the only component that touches the graph, check runs, and company profile.
- Tenant and user come from the authenticated session, never from the payload or the model's tool arguments. The client context supplies only focus and view state, and the backend validates it.

### Tool layer (v1 sketch)

- **Read:** a node and its neighbors by edge type, provision text as of a date, a clause's mapped obligations with stored flag reasoning and citations, the company profile, clauses mapped to an obligation, a provision's version history.
- **Ask the user:** a structured clarifying question with answer options, shown as quick replies. The applicability check depends on it.
- **Propose:** write tools only create a pending confirmation card (dismiss flag, change mapping, save profile fact, mark obligation not applicable, create suggestion). The agent cannot commit a write.
- **UI commands:** open a view, highlight a clause.

### Confirmed writes

Obligations come from public regulations and are shared across companies. Mappings, flags, check runs, and profile facts belong to one company.

| Object | Owner | What chat can do on confirm |
| --- | --- | --- |
| Obligation | Shared | Nothing directly. "Does not apply to us" creates a company-level not-applicable override with a reason. "The extraction is wrong" goes to a review queue for the platform team. |
| Mapping | Company | Add, change, or remove, effective immediately |
| Flag | Company | Dismiss with a reason, effective immediately |
| Profile fact | Company | Save, effective immediately |
| Suggestion | Company | Create in the existing accept, reject, or edit flow |

Rules for every write:

- The card shows exactly what changes, before and after, with the reason prefilled from the conversation. One click commits it, with an undo.
- The click calls the same backend endpoint as the normal UI, with the same permission checks. The agent never calls the write endpoint itself.
- Deletes are soft, so everything can be restored.
- Commit is refused if the flag or mapping changed after the proposal was made, and the agent re-proposes. Proposals expire.
- Bulk changes show the full list and count on the card and need an explicit confirm.
- Each write gets an audit record: who, when, the reason, the message and context snapshot it came from, and the before and after values.
- Changing a mapping or override affects check results. The agent says which clauses need re-checking and offers a new check run, which keeps the old one.

### Grounding, state, and security

- Tool results return node IDs and quoted spans. The agent cites IDs, and the backend rejects any ID not retrieved in that turn. Quotes are checked against the source text before they render as clickable citations.
- The graph cursor (current node and trail) is structured state updated by tools, not something the model remembers. It is what makes "that part" resolvable.
- Every message stores its context snapshot. Long threads are summarized.
- Scraped regulations and company documents are untrusted text. Instructions inside them are treated as data, and any write needs confirmation.

### Evaluation before release

- 30 to 50 realistic conversations per use case, scored on cited nodes and verdicts.
- Adversarial cases: a user pushing back on a flag without evidence (the agent must hold its verdict), applicability questions with missing profile facts, and tenant-isolation probes.
- Full traces of tool calls logged for debugging.

### Build order

1. Tool layer and read-only graph exploration: lowest risk, and it forces citations and the cursor to work.
2. Challenging a flag: reuses stored reasoning and adds the first confirmation-gated write.
3. Applicability check: needs the company profile and provision-text reading.
4. Iterative redrafting into the suggestion flow.
