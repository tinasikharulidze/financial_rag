"""Georgian UI strings and enum translations for the KG app."""

T = {
    "title": "მარეგულირებელი ცოდნის გრაფი",
    "pages": ["კითხვა-პასუხი", "ვერსიების შედარება", "ცვლილების ანალიზი", "დოკუმენტის შემოწმება",
              "შეტყობინებები", "გრაფი", "შეფასება"],
    "no_db": "მონაცემთა ბაზა ვერ მოიძებნა. ჯერ გაუშვით ოფლაინ ჯობები (იხ. docs/IMPLEMENTATION_REPORT.md).",
    "budget": "LLM-ის დღიური ლიმიტი ამოიწურა. სცადეთ ხვალ — შედეგები ქეშირებულია.",
    "act": "აქტი", "all_acts": "ყველა აქტი", "as_of": "თარიღი (რედაქცია)", "question": "თქვენი კითხვა",
    "ask": "კითხვა", "answer": "პასუხი", "citations": "წყაროები", "no_citations": "წყაროები არ არის მითითებული",
    "version_from": "რედაქცია", "provision": "ნორმა",
    "date_a": "თარიღი A", "date_b": "თარიღი B", "compare": "შედარება", "added": "დამატებული",
    "removed": "ამოღებული", "modified": "შეცვლილი", "summary_btn": "შეჯამება (LLM)", "no_changes": "ცვლილებები არ არის.",
    "amendment": "ცვლილება", "run_replay": "ანალიზის გაშვება", "view_report": "ანგარიშის ნახვა",
    "change_summary": "ცვლილებების შეჯამება", "new_obl": "ახალი ვალდებულებები",
    "changed_obl": "შეცვლილი ვალდებულებები", "removed_obl": "ამოღებული ვალდებულებები",
    "affected": "დაზარალებული დოკუმენტები და პუნქტები", "new_alerts": "ახალი შეტყობინებები",
    "no_report": "ამ ცვლილებისთვის ანგარიში ჯერ არ არის. გაუშვით ანალიზი.",
    "existing_doc": "არსებული დოკუმენტი", "upload_pdf": "PDF-ის ატვირთვა", "check": "შემოწმება",
    "findings": "მიგნებები", "no_findings": "მიგნებები არ არის.", "clause": "პუნქტი", "evidence": "მტკიცებულება",
    "fix": "შემოთავაზებული შესწორება", "rationale": "დასაბუთება", "obligation": "ვალდებულება",
    "filters": "ფილტრები", "priority": "პრიორიტეტი", "chance": "პრობლემის ალბათობა", "severity": "სიმძიმე",
    "basis": "რეგულაციის ვერსია", "action": "ქმედება", "gap": "კლასი",
    "alerts_none": "ღია შეტყობინებები არ არის.", "ack": "დადასტურება", "uncovered": "დაუფარავი ვალდებულება",
    "graph_pick": "ვალდებულება", "graph_none": "გრაფისთვის მონაცემები არ არის.",
    "eval_none": "შეფასების შედეგები ჯერ არ არის (evaluation/results/index.csv).",
    "latency": "დაყოვნება (წმ)", "tokens": "ტოკენები", "calls": "LLM გამოძახებები", "cache": "ქეშის წილი",
    "progress_parse": "PDF-ის დამუშავება", "progress_judge": "ვალდებულებების შეფასება",
    "doc": "დოკუმენტი", "company_level": "კომპანიის დონის მიგნებები (ახალი დოკუმენტი)",
    "gold": "ეტალონური მონიტორინგი",
}

PRIORITY = {"critical": "კრიტიკული", "high": "მაღალი", "medium": "საშუალო", "low": "დაბალი"}
CHANCE = {"very_high": "ძალიან მაღალი", "high": "მაღალი", "moderate": "საშუალო", "low": "დაბალი"}
SEVERITY = PRIORITY
BASIS = {"V_doc": "დოკუმენტის შედგენისას მოქმედი (V_doc)", "V_now": "მოქმედი რედაქცია (V_now)"}
ACTION = {"ADJUST_SECTION": "არსებული პუნქტის შესწორება", "ADD_SECTION": "ახალი პუნქტის დამატება",
          "NEW_DOCUMENT": "ახალი დოკუმენტის შექმნა"}
GAP = {"A": "A – ცვლილებით გამოწვეული", "B": "B – თავიდანვე შეუსაბამო"}
COVERAGE = {"full": "სრულად", "partial": "ნაწილობრივ", "contradicts": "ეწინააღმდეგება", "none": "არ არის"}
DOC_TYPE = {"policy": "პოლიტიკა", "inventory": "მოდელების რეესტრი", "validation_report": "ვალიდაციის ანგარიში",
            "monitoring_report": "მონიტორინგის ანგარიში", "ethics_guideline": "ეთიკის სახელმძღვანელო",
            "audit_report": "აუდიტის ანგარიში", "supervisory_application": "საზედამხედველო განაცხადი",
            "other": "სხვა"}
PRIORITY_COLOR = {"critical": "#c0392b", "high": "#e67e22", "medium": "#f1c40f", "low": "#7f8c8d"}


def tr(table: dict, key, default=None):
    return table.get(key, default if default is not None else key)
