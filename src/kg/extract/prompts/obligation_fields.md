<!-- version: fields-v1.1 -->
- `statement`      one checkable sentence IN GEORGIAN that a reviewer can compare with a bank document, with the
                   numbers, frequencies and roles of the text;
- `deontic`        must | must_not | may   (see DEONTIC below);
- `subject`        who must act, in the words of the text (e.g. "კომერციული ბანკი", "სესხის გამცემი ორგანიზაცია",
                   "დაცვის მეორე ხაზი", "სამეთვალყურეო საბჭო");
- `applies_to`     entity types the duty is for; use only: {entity_types}. Default = `act.default_applies_to`.
                   Follow the text: "კომერციულმა ბანკმა და მიკრობანკმა" -> [commercial_bank, microbank];
                   "მიკრობანკების შემთხვევაში ..." (only microbanks) -> [microbank];
- `category` is always a label, never null (use "policy" when unsure);
- `action`         what must be done;
- `condition`      when the duty applies (null if always);
- `exceptions`     exceptions or relaxations that the unit or a referenced provision states (null if none);
- `deadline_or_frequency`  deadline or frequency exactly as in the text (null if none);
- `severity`       critical | high | medium | low (rubric below);
- `category`       one short English label of the topic (free text, e.g. governance, creditworthiness_assessment,
                   provisioning, capital_calculation, disclosure, reporting, validation, record_keeping);
- `domain`         the topic area of the duty, default = `act.default_domains[0]`;
- `required_doc`   which internal document must contain or evidence the duty, or null:
      {"doc_types": [...], "basis": "explicit" | "inferred", "quote": "..."}
      * "explicit" ONLY when the provision itself names the document (policy, procedure, register, report,
        methodology, disclosure, application ...). Then `quote` is MANDATORY and must be copied verbatim from
        `unit.text`.
      * "inferred" when you conclude it from the nature of the duty (no quote needed). Use null when you cannot say.
      * `doc_types`: short English snake_case names of the kind of document, as general as the duty allows:
        policy, procedure, methodology, register, internal_report, regulatory_report, public_disclosure,
        audit_report, supervisory_application, governance_charter, contract_or_template; more specific ones
        when the text points to them (e.g. credit_policy, provisioning_methodology, pillar3_disclosure,
        model_inventory, validation_report).
- `evidence_type`  what would prove compliance (e.g. "clause in the policy", "step of the procedure", "field of a
                   register", "line of a published report", "submission to the National Bank"), or null;
- `location_hint`  1-2 sentences: WHERE and HOW a bank document should address the duty (kind of section, what it
                   should state). Do not invent section numbers;
- `depends_on`     paths (like "art6/p5") of provisions this duty depends on through references or definitions;
- `source_path`    path of the provision the duty comes from (the path in square brackets, or `unit.path`);
- `citations`      at least one: `{"kind": "provision", "id": "<pv_id>", "quote": "<verbatim words from that
                   provision, at most 25 words>"}`. Use `kind: "provision"` and ids from `unit.provisions`,
                   `definitions` or `references` only.

Severity rubric (apply strictly):
- critical: requires supervisory consent or reporting to the National Bank; a hard prohibition or a quantitative
  limit (ratios, thresholds, maximum terms); capital that must be held or deducted; independence of control
  functions; anything with explicit sanctions.
- high: core process duties of the regulated activity (e.g. assessing a borrower's repayment capacity, classifying
  assets and calculating reserves or expected losses, calculating capital, validating and monitoring models, keeping
  required registers complete).
- medium: documentation and methodology details, proportionality, internal reporting, principles without a
  measurable criterion.
- low: formal duties of minor consequence.

DEONTIC (keep the polarity of the text; Georgian cue words):
- must      a duty:        "უნდა", "ვალდებულია", "უზრუნველყოფს", "პასუხისმგებელია";
- must_not  a prohibition: "არ უნდა", "დაუშვებელია", "აკრძალულია", "არ შეიძლება", "არ დაიშვება";
- may       a permission:  "შეუძლია", "უფლებამოსილია", "შესაძლებელია", "დასაშვებია", "შეიძლება".
Rules:
- A prohibition is its OWN obligation with `deontic: "must_not"`; its `statement` keeps the negation
  ("... არ უნდა ..."). Never rewrite a prohibition as a positive duty and never fold it into another item.
  Example: "ჩამნაცვლებელი ცვლადი არ უნდა გამოიყენებოდეს, თუ ..." -> must_not, with the "თუ ..." part as `condition`.
- A permission is `deontic: "may"` ("მოდელის გამომყენებელ სუბიექტს შეუძლია განახორციელოს აუთსორსინგი" -> may).
  Duties attached to it ("in that case it must ...") are SEPARATE items with `deontic: "must"` and the permission
  as their `condition`.
- A limit stated negatively ("... არ უნდა აღემატებოდეს 90 დღეს") is must_not.
