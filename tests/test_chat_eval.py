"""The chat evaluation set: the CSV is well-formed and the automatic checks behave."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_chat_eval", ROOT / "eval" / "chat" / "run_chat_eval.py")
rce = importlib.util.module_from_spec(spec)
sys.modules["run_chat_eval"] = rce
spec.loader.exec_module(rce)

KNOWN = {"kind", "kind_in", "tools_any", "tools_all", "tools_none", "cites_any", "cites_all", "cites_min", "no_dropped",
         "actions", "actions_any", "actions_not", "actions_none", "text_any", "text_all", "text_none", "proposal_any",
         "proposal_all", "proposal_none", "cursor_any", "confirm", "task_status", "either"}


def turn(**kw):
    base = {"kind": "answer", "text": "This is a long enough English answer about the flag to be judged.", "tools": ["clause_coverage"],
            "cites": ["cl:bank-a/X/1.1"], "dropped": [], "actions": [], "proposals": [], "cursor": {}}
    return {**base, **kw}


def test_cases_are_well_formed():
    cases = rce.load_cases(ROOT / "eval" / "chat" / "chat_cases.csv")
    assert len(cases) == 20
    assert {c["use_case"] for c in cases} == {"redrafting", "challenge_flag", "applicability", "exploration"}
    assert [c["token_tier"] for c in cases] == sorted(c["token_tier"] for c in cases)
    for c in cases:
        assert 2 <= len(c["turns"]) <= 3 and c["lang"] in ("ka", "en"), c["id"]
        for t in c["turns"]:
            assert {k[5:] if k.startswith("soft_") else k for k in t["expect"]} <= KNOWN, c["id"]
            for sub in t["expect"].get("either", []):
                assert {k[5:] if k.startswith("soft_") else k for k in sub} <= KNOWN
        assert "AR-P3" not in c["document"] and "capital" not in c["use_case"]


def test_checks_pass_and_fail_as_described():
    ok = rce.run_checks({"tools_any": ["clause_coverage"], "cites_min": 1, "actions_none": True}, turn(), "en")
    assert all(c["status"] == "pass" for c in ok)
    bad = rce.run_checks({"actions": ["suggest_wording"], "proposal_none": ["24"]},
                         turn(actions=[{"kind": "suggest_wording"}], proposals=["ვადა 24 საათი"]), "en")
    assert [c["check"] for c in bad if c["status"] == "fail"] == ["proposal_none"]


def test_soft_checks_only_warn_and_either_takes_the_best_alternative():
    r = rce.run_checks({"soft_text_any": ["zzz"], "either": [{"cites_min": 5}, {"tools_any": ["clause_coverage"]}]}, turn(), "en")
    assert [c["status"] for c in r if c["check"] in ("soft_text_any", "either")] == ["warn", "pass"]


def test_language_and_leaks_are_caught():
    ka = rce.run_checks({}, turn(text="This answer is in English although the interface language is Georgian, clearly."), "ka")
    assert any(c["check"] == "language" and c["status"] == "fail" for c in ka)
    leak = rce.run_checks({}, turn(text="See obl:4964423/art5/p2#1@2021-01-01 and the clause_coverage tool result for details."), "en")
    assert any(c["check"] == "no_ids_or_tool_names_in_text" and c["status"] == "fail" for c in leak)
