from kg.eval.matching import article_overlap, match, parse_gold_articles, section_match, section_tokens
from kg.ids import display_path, parse_ordinal, path_sort_key
from kg.mapping.labels import priority


def test_ordinals_and_paths():
    assert parse_ordinal("მე-6") == 6 or parse_ordinal("მე-6")[0] == 6
    paths = ["art10", "art9_1", "art9", "art2/p10", "art2/p2", "art2/p2/sბ", "art2/p2/sა"]
    assert sorted(paths, key=path_sort_key) == ["art2/p2", "art2/p2/sა", "art2/p2/sბ", "art2/p10", "art9", "art9_1",
                                                "art10"]
    assert display_path("art6/p4.1") == "მუხლი 6, პუნქტი 4¹"


def test_gold_article_parsing():
    assert parse_gold_articles("Art. 6 para 4¹–4²") == ["art6/p4.1", "art6/p4.2"]
    assert parse_gold_articles("Art. 9 para 4, 4¹–4³") == ["art9/p4", "art9/p4.1", "art9/p4.2", "art9/p4.3"]
    assert parse_gold_articles("Art. 10–11; Art. 8 para 1") == ["art10", "art11", "art8/p1"]
    assert parse_gold_articles("Art. 2 ზ; Art. 9¹") == ["art2", "art9_1"]
    assert parse_gold_articles("—") == []


def test_section_tokens_and_match():
    assert section_tokens("1–5, 9, 10") == ["1", "2", "3", "4", "5", "9", "10"]
    assert section_tokens("4.1–4.3") == ["4.1", "4.2", "4.3"]
    assert section_tokens("5.2.1 (also 1.4.1)") == ["5.2.1", "1.4.1"]
    assert section_tokens("Row M-002") == [] and section_tokens("F1, F2") == []
    assert section_match("5.2.1", ["5.2"]) and section_match("5.2", ["5.2.1"]) and not section_match("5.3", ["5.2"])
    assert article_overlap("art6/p4.1", ["art6/p4.1"]) and not article_overlap("art6/p4", ["art6/p4.1"])
    assert article_overlap("art6/p5/sბ.ა", ["art6/p5"]) and article_overlap("art2", ["art2"])


def test_match_kinds():
    gold = {"document_file": "D1.pdf"}
    f = {"doc_file": "D1.pdf", "section_no": "5.2.1", "clause_id": "cl:x", "source_path": "art6/p4.1"}
    assert match(f, gold, ["art6/p4.1"], ["5.2"]) == "section+article"
    assert match(f, gold, ["art6/p4.1"], ["7.1"]) is None
    assert match({**f, "clause_id": None}, gold, ["art6/p4.1"], ["7.1"]) == "article_only"
    assert match({**f, "doc_file": "D2.pdf"}, gold, ["art6/p4.1"], ["5.2"]) is None


def test_priority_rule():
    assert priority("critical", "moderate") == "critical"
    assert priority("critical", "low") == "high"
    assert priority("low", "low") == "low"
    assert priority("medium", "very_high") == "medium"


def test_match_without_gold_article():
    gold = {"document_file": "D9.pdf"}
    f = {"doc_file": "D9.pdf", "section_no": "2.1", "clause_id": "cl:y", "source_path": "art3"}
    assert match(f, gold, [], ["2"]) == "section_only"
    assert match(f, gold, [], ["5"]) is None
