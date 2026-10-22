<!-- version: fields-v1.0 -->
- `statement`      one checkable sentence IN GEORGIAN that a reviewer can compare with a bank document, with the
                   numbers, frequencies and roles of the text;
- `deontic`        must | must_not | may   (see DEONTIC below);
- `subject`        who must act, in the words of the text (e.g. "დაცვის მეორე ხაზი", "მოდელის გამომყენებელი სუბიექტი");
- `applies_to`     entity types the duty is for; use only: {entity_types}. Default = `act.default_applies_to`.
                   Follow the text: "კომერციულმა ბანკმა და მიკრობანკმა" -> [commercial_bank, microbank];
                   "მიკრობანკების შემთხვევაში ..." (only microbanks) -> [microbank];
- `category` is always a label, never null (use "policy" when unsure);
- `action`         what must be done;
- `condition`      when the duty applies (null if always);
- `exceptions`     exceptions or relaxations that the unit or a referenced provision states (null if none);
- `deadline_or_frequency`  deadline or frequency exactly as in the text (null if none);
- `severity`       critical | high | medium | low (rubric below);
- `category`       one short English label of the topic (free text, e.g. validation, governance, inventory);
- `domain`         the topic area of the duty, default = `act.default_domains[0]`;
- `required_doc`   which internal document must contain or evidence the duty, or null:
      {"doc_types": [...], "basis": "explicit" | "inferred", "quote": "..."}
      * "explicit" ONLY when the provision itself names the document (policy, procedure, register/inventory, report,
        methodology, application ...). Then `quote` is MANDATORY and must be copied verbatim from `unit.text`.
      * "inferred" when you conclude it from the nature of the duty (no quote needed). Use null when you cannot say.
      * `doc_types`: short English snake_case names such as policy, procedure, model_inventory, validation_report,
        monitoring_report, ethics_guideline, audit_report, methodology, supervisory_application, governance_charter.
- `evidence_type`  what would prove compliance (e.g. "clause in the policy", "inventory field", "annual report",
                   "submission to the National Bank"), or null;
- `location_hint`  1-2 sentences: WHERE and HOW a bank document should address the duty (kind of section, what it
                   should state). Do not invent section numbers;
- `depends_on`     paths (like "art6/p5") of provisions this duty depends on through references or definitions;
- `source_path`    path of the provision the duty comes from (the path in square brackets, or `unit.path`);
- `citations`      at least one: `{"kind": "provision", "id": "<pv_id>", "quote": "<verbatim words from that
                   provision, at most 25 words>"}`. Use `kind: "provision"` and ids from `unit.provisions`,
                   `definitions` or `references` only.

Severity rubric (apply strictly):
- critical: requires supervisory consent or reporting to the National Bank; independence of the lines of defence;
  launching a model before validation; anything with explicit sanctions or a hard prohibition.
- high: core process duties: validation, monitoring, data quality, outcomes analysis, completeness of the model inventory.
- medium: documentation and methodology details, proportionality, ethics principles, explainability techniques.
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
