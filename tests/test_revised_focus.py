"""What the left document highlights, the proposed version highlights too (a clause without a proposal as well)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import review_lib as R  # noqa: E402


def test_revised_html_marks_the_focus_clause(monkeypatch):
    unit = {"id": "cl:x/1", "unit_type": "clause", "section_no": "1.1", "text": "plain clause"}
    rv = {"units": [{"unit": unit, "change": None, "inserts": []}], "new_documents": [], "count": 0, "accepted": 0}
    monkeypatch.setattr(R, "revised_view", lambda model: rv)
    html = R.revised_html({}, {}, False, focus_clause="cl:x/1")
    assert "class='u focus'" in html and "data-focus='cl:x/1'" in html
    assert "focus" not in R.revised_html({}, {}, False).replace("goniFocus", "")


def test_the_original_has_no_blue_highlight():
    card = {"role": "anchor", "relevance": "relevant", "verdict": "none", "title": "t", "statement": "s", "oid": "o",
            "id": "o", "missing": "", "why": "", "quote": "", "kind": "add_section", "slot": "add:1"}
    unit = {"id": "cl:x/1", "unit_type": "clause", "section_no": "1", "text": "plain clause", "cards": [card]}
    html = R.render_html({"units": [unit]})
    assert "anchor" not in html.split("</style>")[1] and "class='cl'" in html
