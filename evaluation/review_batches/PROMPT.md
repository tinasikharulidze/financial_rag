You are an independent reviewer of a compliance-checking system. The system compared a bank's internal document with
a regulation of the National Bank of Georgia and produced findings. All texts are Georgian; write your answer in
English and quote the decisive Georgian words where needed.

Attached: document.txt (the whole internal document, one clause per line as "[clause id] §section text") and a batch
file with findings. Each finding is ONE obligation taken from a regulation, plus the system's assessment:
- obligation: the system's one-sentence statement of the requirement.
- regulation_text and article_context: the source provision, and its article ("THIS PARAGRAPH" first, then the rest).
- verdict: contradicts = the document says something that conflicts with the requirement; partial = part of the
  requirement is met but a specific element is missing; none = the document does not address it at all;
  full = every element is met.
- rationale, missing: the system's reasoning. evidence: the clauses it cites, as "§section [role] „quote“ — why".
- proposal_*: the change the system suggests.
- The block after "answer key" is an expert label. Read it only AFTER you have decided review_correct_verdict.

For each finding, work in this order:
1. Obligation vs regulation. Does the obligation say what the regulation text says? Check the modality (a
   recommendation such as "რეკომენდებულია" or a permission is not a duty), the scope, and whether examples after
   "მათ შორის" ("including") were turned into a closed, mandatory list.
2. Scope. Could this TYPE of document contain this requirement at all? (A duty of a policy is not a gap in an audit
   report, a monitoring report or a register.)
3. Your own verdict, decided from document.txt and the regulation BEFORE you read the system's rationale. For "none",
   search the whole document, including synonyms, paraphrases and tables, before agreeing it is absent. For
   "contradicts", the document must state something that conflicts, not merely omit. For "partial", name the exact
   missing element and check that the regulation really requires it. Apply the regulation as written: do not be
   stricter or more lenient than its text.
4. Then check the system's claims. Every quote must really be in document.txt and say what the system claims; every
   number and section it names must exist.
5. Then the proposal. Then, only if an answer key is given, compare it with the system's reason.

Return ONLY a JSON list (no markdown fences), one object per finding, in batch order, none omitted:
{"finding_id": "<copy exactly>",
 "review_in_scope": "yes | no | unclear",
 "review_obligation_faithful": "yes | no | unclear",   (obligation matches the regulation: modality, scope, no examples made mandatory)
 "review_hallucination": "none | minor | major",        (a wrong quote or a false claim about the document or the regulation = major)
 "review_unsupported_claims": "short list, or empty",
 "review_correct_verdict": "contradicts | partial | none | full | not_applicable",    (your own verdict)
 "review_verdict_correct": "yes | partly | no",         (yes = same as yours; partly = right direction, wrong degree, e.g. partial vs none;
                                                         no = gap where the document complies, or compliance where there is a gap)
 "review_error_type": "none | too_strict | too_lenient | evidence_missed | wrong_clause | not_in_scope | extraction_error | other",
                                                        (too_strict = demands more than the regulation requires; evidence_missed = a clause that
                                                         satisfies it exists but was not found; wrong_clause = cites an unrelated clause;
                                                         extraction_error = the obligation misstates the regulation)
 "review_argument_quality": 1-5,                        (1 = no real argument, 5 = names the right clause and the decisive difference)
 "review_proposal_alignment": 1-5 or null,              (fixes exactly what rationale / missing say, invents no numbers or roles; null if none)
 "review_gold_reason_alignment": 1-5 or null,           (only with an answer key: the same problem as gold_says / gold_requirement?)
 "review_comment": "one or two sentences quoting the decisive words from document.txt or the regulation"}
If you cannot decide, answer "unclear" rather than guessing. Give your own reasoning; do not repeat the system's wording.