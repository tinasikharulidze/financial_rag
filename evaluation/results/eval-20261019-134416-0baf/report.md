# Evaluation: compliance
Run `eval-20261019-134416-0baf` at 2026-10-19T13:44:16+04:00

## Metrics

| metric | value |
|---|---|
| gold_items.A | 35 |
| gold_items.B | 22 |
| gold_items.C | 9 |
| findings_total | 286 |
| recall_A | 0.686 |
| recall_B | 0.636 |
| precision | 0.304 |
| c_false_positive_rate | 0.556 |
| version_attribution_accuracy | 1.000 |
| chance_calibration.high.findings | 161 |
| chance_calibration.high.true_positive | 66 |
| chance_calibration.high.precision | 0.410 |
| chance_calibration.moderate.findings | 76 |
| chance_calibration.moderate.true_positive | 4 |
| chance_calibration.moderate.precision | 0.053 |
| chance_calibration.very_high.findings | 44 |
| chance_calibration.very_high.true_positive | 15 |
| chance_calibration.very_high.precision | 0.341 |
| chance_calibration.low.findings | 5 |
| chance_calibration.low.true_positive | 2 |
| chance_calibration.low.precision | 0.400 |
| gold_by_chance.A.high | 17 |
| gold_by_chance.A.moderate | 3 |
| gold_by_chance.A.very_high | 3 |
| gold_by_chance.A.low | 1 |
| gold_by_chance.B.high | 12 |
| gold_by_chance.B.very_high | 1 |
| gold_by_chance.B.moderate | 1 |
| action_types.ADD_SECTION | 149 |
| action_types.ADJUST_SECTION | 137 |
| match_kinds.article_only | 123 |
| match_kinds.section+article | 27 |

## Items (66)

| gold_id | class | document | section | article | matched | match_kind | finding_ids | gap_classes | priorities |
|---|---|---|---|---|---|---|---|---|---|
| D1-01 | A | Model Risk Management Policy | 5.2.1 (also 1.4.1) | Art. 6 para 4¹–4² | True | article_only | 264,265,266 | A | high |
| D1-02 | A | Model Risk Management Policy | 5.3.2 | Art. 6 para 5 ბ.ა | True | article_only | 249,252,285,286,287 | A,B | high,medium |
| D1-03 | A | Model Risk Management Policy | 4.6.1 | Art. 6 para 5 ბ.ბ¹ | True | article_only | 249,252,285,286,287 | A,B | high,medium |
| D1-04 | A | Model Risk Management Policy | 4.3.3 | Art. 5 para 5 | False |  |  |  |  |
| D1-05 | A | Model Risk Management Policy | 4.2.3, 4.4.2 | Art. 5 para 10¹–10³ | True | article_only | 262,263 | A | high,medium |
| D1-06 | A | Model Risk Management Policy | 2.1 (definitions), 9 | Art. 2 ზ; Art. 9¹ | True | section+article | 272,274,275,276,277,279,280,281,282,283 | A | medium |
| D1-07 | A | Model Risk Management Policy | 9.1 | Art. 4 para 3 | True | section+article | 284 | A | medium |
| D1-08 | A | Model Risk Management Policy | 7.1.2–7.1.3 | Art. 9 para 4, 4¹–4³ | True | article_only | 268,269,270,271 | A | high |
| D1-09 | A | Model Risk Management Policy | 7.2.1 | Art. 9 para 1 | False |  |  |  |  |
| D1-10 | B | Model Risk Management Policy | 3.3.1–3.3.2, 4.5.1 | Art. 8 para 1 | True | article_only | 237 | B | critical |
| D1-11 | B | Model Risk Management Policy | 3.5.1–3.5.2 | Art. 6 para 4; Art. 4 para 1 | False |  |  |  |  |
| D1-C1 | C | Model Risk Management Policy | 3.4.1 | Art. 4 para 1 | False |  |  |  |  |
| D1-C2 | C | Model Risk Management Policy | 7.3.1 | Art. 9 para 5 | False |  |  |  |  |
| D1-C3 | C | Model Risk Management Policy | 4.2.2, 4.4.1 | Art. 5 para 8, 10 | True | section+article | 244,245,246 | B | high,medium |
| D1-C4 | C | Model Risk Management Policy | 8.1–8.3 | Art. 10–11 | True | article_only | 256,257,258,259,260 | B | critical,high |
| D2-01 | A | Model Inventory | Section 1 note; 'next validation' column | Art. 6 para 4¹–4² | True | article_only | 308,309,310 | A | high |
| D2-02 | B | Model Inventory | Row M-005 – 'contingency plan' column | Art. 9 para 5 | True | article_only | 292 | B | high |
| D2-03 | B | Model Inventory | Row M-007 – 'last validation' column | Art. 6 para 3; Art. 3 para 3; Art. 8 para 1 | False |  |  |  |  |
| D2-04 | B | Model Inventory | Row M-002 | Art. 10–11; Art. 8 para 1 | True | article_only | 304,305,306,307 | B | critical,high |
| D2-05 | B | Model Inventory | Row M-005 – 'last validation' column | Art. 6 para 3 | False |  |  |  |  |
| D5-01 | A | Internal Validation Report – M-001 | 5.1–5.2 | Art. 6 para 5 ბ.ა | True | article_only | 351,352,353,369,371 | A,B | high,medium |
| D5-02 | A | Internal Validation Report – M-001 | 7.1–7.2 | Art. 5 para 10²–10³ | True | section+article | 362 | A | medium |
| D5-03 | A | Internal Validation Report – M-001 | 2.2–2.3, 7.1 | Art. 9¹ para 3–4 ე | False |  |  |  |  |
| D5-04 | A | Internal Validation Report – M-001 | 3.1, 3.3 | Art. 9¹ para 4 დ | False |  |  |  |  |
| D5-05 | A | Internal Validation Report – M-001 | 1.1 | Art. 6 para 4² | True | section+article | 365 | A | high |
| D5-06 | A | Internal Validation Report – M-001 | 8.2 and recommendation R2 | Art. 6 para 5 ბ.ბ¹ | True | article_only | 351,353,369,372 | A,B | high,medium |
| D5-07 | B | Internal Validation Report – M-001 | 4.3 | Art. 6 para 5 ა | True | section+article | 348,349,351,353,369 | A,B | high,medium |
| D5-08 | B | Internal Validation Report – M-001 | Section 10 (signatures); D1 3.5 | Art. 6 para 4 | False |  |  |  |  |
| D5-09 | B | Internal Validation Report – M-001 | 1.2 | Art. 8 para 1 | False |  |  |  |  |
| D5-10 | B | Internal Validation Report – M-001 | 9.1 vs 6.2 | Art. 6 para 6; Art. 8 para 1–3 | True | article_only | 321 | B | high |
| D5-C1 | C | Internal Validation Report – M-001 | 3.2, 6 (benchmark) | Art. 5 para 4; Art. 6 para 5 ბ.დ | True | article_only | 351,353,354,355,356,369 | A,B | high,medium |
| D6-01 | B | Quarterly Model Monitoring Report Q1 2022 | Section 7 | Art. 6 para 5 ბ.ე | True | article_only | 164,165,167,168,170,171,184,186,187 | A,B | high,medium |
| D6-02 | B | Quarterly Model Monitoring Report Q1 2022 | 2.2–2.3 | Art. 8 para 3 | False |  |  |  |  |
| D6-03 | A | Quarterly Model Monitoring Report Q1 2022 | 2.2.3 | Art. 6 para 5 ბ.ბ¹ | True | article_only | 164,165,167,168,170,171,184,186,187 | A,B | high,medium |
| D6-04 | A | Quarterly Model Monitoring Report Q1 2022 | 3.2 | Art. 5 para 5; Art. 8 para 3 | False |  |  |  |  |
| D6-05 | A | Quarterly Model Monitoring Report Q1 2022 | 4.1–4.3 | Art. 9 para 4¹–4² | False |  |  |  |  |
| D6-06 | A | Quarterly Model Monitoring Report Q1 2022 | Whole report (no section) | Art. 9¹ para 4 გ | False |  |  |  |  |
| D6-07 | A | Quarterly Model Monitoring Report Q1 2022 | Annex A, row M-007 | Art. 9 para 4³ | False |  |  |  |  |
| D6-C1 | C | Quarterly Model Monitoring Report Q1 2022 | Section 5 | Art. 6 para 5 ბ.გ | True | article_only | 164,165,166,167,168,170,171,184,186,187,188 | A,B | high,medium |
| D9-01 | A | AI Ethics Guideline | Section 2, 2nd bullet | Art. 4 para 3 | True | section+article | 113 | A | medium |
| D9-02 | A | AI Ethics Guideline | Section 3 (principles) | Art. 9¹ para 1–3; Art. 2 ზ | True | article_only | 102 | A | medium |
| D9-03 | A | AI Ethics Guideline | 4.2 | Art. 9¹ para 4 ე | True | article_only | 103,104,105,106,107,109 | A | medium |
| D9-04 | A | AI Ethics Guideline | 3.2 | Art. 9¹ para 4 ვ | True | article_only | 103,104,105,106,107,108,109 | A | medium |
| D9-05 | A | AI Ethics Guideline | 5.1–5.2 | Art. 9¹ para 5; Art. 5 para 10³ | True | article_only | 110,111 | A | medium |
| D9-06 | A | AI Ethics Guideline | Whole document (missing) | Art. 9¹ para 6 | True | article_only | 112 | A | medium |
| D9-07 | A | AI Ethics Guideline | Section 7 (roles) | Art. 9¹ para 4 ა–დ, ზ | True | article_only | 103,104,105,106,107,109 | A | medium |
| D9-08 | A | AI Ethics Guideline | Whole document (missing) | Art. 5 para 10¹–10² | True | article_only | 98,99,100 | A | high,medium |
| D9-C1 | C | AI Ethics Guideline | 6.1–6.3, 3.5 | — | False |  |  |  |  |
| D10-01 | B | Internal Audit Report – MRM framework | 2.2 (bullets 1–2) | Art. 7 para 2 | True | section+article | 142,143 | B | high |
| D10-02 | B | Internal Audit Report – MRM framework | 2.2 (bullet 3) | Art. 7 para 1 | False |  |  |  |  |
| D10-03 | B | Internal Audit Report – MRM framework | 3.2 | Art. 4 para 1; Art. 7 | True | article_only | 123,124,125,157 | A,B | critical,high |
| D10-04 | B | Internal Audit Report – MRM framework | 3.3 | Art. 9 para 1 | True | section+article | 117 | B | high |
| D10-05 | A | Internal Audit Report – MRM framework | 3.1 | Art. 7 para 3 | True | article_only | 157 | A | high |
| D10-06 | A | Internal Audit Report – MRM framework | Finding F6 | Art. 6 para 4¹–4² | True | article_only | 147,148,149 | A | high |
| D10-07 | A | Internal Audit Report – MRM framework | Section 2.1 (scope) – missing | Art. 7 para 1; Art. 9¹; Art. 5 para 10¹ | False |  |  |  |  |
| D10-08 | B | Internal Audit Report – MRM framework | F3, F4 ratings; 5.2 | Art. 8 para 1; Art. 10–11 | True | article_only | 144,145 | B | high |
| D10-C1 | C | Internal Audit Report – MRM framework | F1, F2 | Art. 6 para 3; Art. 3 | True | article_only | 130 | B | high |
| D11-01 | B | NBG consent application – M-002 | Section 6 | Art. 11 para 2 | False |  |  |  |  |
| D11-02 | B | NBG consent application – M-002 | 12.1 | Art. 11 para 5 | True | section+article | 221,223 | B | high |
| D11-03 | B | NBG consent application – M-002 | 12.2 (1st sentence) | Art. 11 para 5 | True | section+article | 222,223 | B | high |
| D11-04 | B | NBG consent application – M-002 | 12.2 (2nd sentence) | Art. 11 para 5 | True | section+article | 222,223 | B | high |
| D11-05 | B | NBG consent application – M-002 | Section 2 | Art. 10; Art. 11 | True | article_only | 220,223,224 | B | critical,high |
| D11-06 | A | NBG consent application – M-002 | 5.1, 8.2 | Art. 5 para 10²–10³ | True | section+article | 227 | A | medium |
| D11-07 | A | NBG consent application – M-002 | 4, 5.2, 10.1 | Art. 9¹ para 3–4 | False |  |  |  |  |
| D11-08 | A | NBG consent application – M-002 | 4 (data 2017–2020) | Art. 9¹ para 4 დ | False |  |  |  |  |
| D11-C1 | C | NBG consent application – M-002 | 1–5, 9, 10 | Art. 11 para 2 | False |  |  |  |  |
