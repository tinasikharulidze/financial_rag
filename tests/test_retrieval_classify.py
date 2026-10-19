from kg.mapping.coverage import classify, predecessor
from kg.retrieval.index import HybridIndex


def test_hybrid_index_georgian():
    items = [{"id": "a", "text": "შიდა ვალიდაციის პროცესი უნდა წარიმართოს რისკზე დაფუძნებული მიდგომით"},
             {"id": "b", "text": "მოდელების რეესტრი უნდა განახლდეს ყოველკვარტალურად"},
             {"id": "c", "text": "ეთიკის პრინციპები და გადახრის შეფასება"}]
    idx = HybridIndex(items)
    assert idx.search("რისკზე დაფუძნებული ვალიდაცია", 1)[0][0] == "a"
    assert idx.search("რეესტრის განახლება", 1)[0][0] == "b"
    assert "c" not in [i for i, _ in idx.search("ეთიკის პრინციპები", 3, allowed={"a", "b"})]


def _ob(i, unit, text, sev="high"):
    return {"id": i, "valid_from": "2021-01-01", "props": {"act": "1", "unit": unit, "statement": text, "severity": sev}}


class J:
    def __init__(self, cov, chance="high"):
        self.coverage, self.issue_chance = cov, chance
        self.best_clause_id = "cl:1"; self.rationale = ""; self.evidence_quote = None
        self.suggested_fix = None; self.action_type = None


def test_gap_classification():
    old = _ob("o_old", "art6/p4", "validation must be annual")
    same = _ob("o_same", "art3/p1", "policy must exist")
    new = _ob("o_new", "art6/p4", "validation must be risk based and annual")
    added = _ob("o_add", "art9_1/p2", "avoid deviation in results")
    v_doc, v_now = [old, same], [new, same, added]
    # old ok, new broken -> class A; same broken -> B; added broken -> A
    judged = {"o_old": J("full"), "o_new": J("partial"), "o_same": J("none"), "o_add": J("none")}
    rows, stats = classify("doc:x", v_doc, v_now, judged, "r1")
    got = {(r["obligation_id"], r["gap_class"], r["version_basis"]) for r in rows}
    assert got == {("o_new", "A", "V_now"), ("o_same", "B", "V_doc"), ("o_add", "A", "V_now")}
    # old already broken -> class B on the old obligation, no extra A for its successor
    judged = {"o_old": J("partial"), "o_new": J("partial"), "o_same": J("full"), "o_add": J("full")}
    rows, _ = classify("doc:x", v_doc, v_now, judged, "r1")
    assert {(r["obligation_id"], r["gap_class"]) for r in rows} == {("o_old", "B")}
    # nothing wrong -> nothing written
    rows, stats = classify("doc:x", v_doc, v_now, {k: J("full") for k in ("o_old", "o_new", "o_same", "o_add")}, "r1")
    assert rows == [] and stats["ok"] >= 2
    assert predecessor(new, [old, same])["id"] == "o_old"
    assert predecessor(added, [old, same]) is None
