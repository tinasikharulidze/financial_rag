"""The tool layer after the graph analysis: every tool on the small two-company graph of test_agent.py, plus the
tenant rules (company B's documents and clauses never show up for company A)."""
import json

import pytest
from test_agent import (ACT, CL_A, CL_B, DOC_A, DOC_B, ENTITY_A, ENTITY_B, OBL, PV1, PV2, SECRET_B, build_store)

from kg.agent.tools import ToolBox

AMEND = "amend:9"
OBL_PSP = f"obl:{ACT}/art2#1@2020-01-01"       # addressed to payment service providers only
OBL_GAP = f"obl:{ACT}/art3#1@2020-01-01"       # applies to us, no document of ours covers it


@pytest.fixture
def rich(tmp_path):
    s = build_store(tmp_path / "kg.sqlite")
    s.upsert_node(f"act:{ACT}", "Act", {"title": "Test act", "number": "1/04", "doc_id": ACT, "short": "Model risk"})
    s.upsert_node(AMEND, "AmendingAct", {"number": "9/04", "date": "2021-12-20", "effective_from": "2022-01-01",
                                         "base_act": ACT, "title": "Amends art 1"})
    s.upsert_edge(PV2, AMEND, "AMENDED_BY", {"instruction_id": "1"}, valid_from="2022-01-01")
    s.upsert_edge(PV2, PV1, "UPDATES", {"change_type": "modified"}, valid_from="2022-01-01")
    for oid, applies, src in ((OBL, ["commercial_bank"], "art1"), (OBL_PSP, ["payment_service_provider"], "art2"),
                              (OBL_GAP, ["commercial_bank"], "art3")):
        props = {"statement": f"statement of {oid}", "act": ACT, "severity": "high", "source_path": src,
                 "category": "governance", "domain": "model_risk", "applies_to": applies,
                 "condition": "if the bank uses scoring models", "exceptions": "small banks",
                 "citations": [{"kind": "provision", "id": PV1, "label": "Model risk › art 1"}]}
        s.upsert_node(oid, "Obligation", props, valid_from="2020-01-01")
    s.upsert_edge(PV1, OBL_GAP, "IMPOSES")
    s.upsert_edge(PV1, OBL_PSP, "IMPOSES")
    s.upsert_edge(OBL, CL_A, "COVERED_BY", {"role": "partial", "coverage": "partial", "doc_version": DOC_A,
                                            "quote": "ჩვენი პოლიტიკა", "why": "annual only"})
    s.upsert_edge(OBL, CL_B, "COVERED_BY", {"role": "full", "coverage": "full", "doc_version": DOC_B})
    s.x("UPDATE coverage SET citations=?, cause='amendment', amendment_id=? WHERE doc_version_id=?",
        (json.dumps([{"kind": "provision", "id": PV1}]), AMEND, DOC_A))
    s.x("INSERT INTO check_jobs(version_id,status,started_at,finished_at) VALUES(?,?,?,?)",
        (DOC_A, "done", "2021-06-01T09:00:00+04:00", "2021-06-01T10:00:00+04:00"))
    s.x("INSERT INTO tasks(key,company_id,doc_version_id,obligation_id,title,status,score,priority,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)", ("k", "A", DOC_A, OBL, "t", "open", 0.5, "high", "2026-01-01", "2026-01-01"))
    s.x("INSERT INTO proposals(doc_version_id,obligation_id,kind,clause_id,proposed_text,explanation,created_at) "
        "VALUES(?,?,?,?,?,?,?)", (DOC_A, OBL, "adjust_clause", CL_A, "new text", "make it quarterly", "2026-01-01"))
    return s


def tb(store, actor=ENTITY_A):
    return ToolBox(store, actor)


# --------------------------------------------------------------------------------------------- orientation
def test_list_acts_and_outline(rich):
    t = tb(rich)
    acts = t.call("list_acts", {})["acts"]
    assert acts[0]["act"] == ACT and acts[0]["short"] == "Model risk" and acts[0]["domain"] == "model_risk"
    out = t.call("list_provisions", {"act": ACT})
    assert [c["path"] for c in out["children"]] == ["art1"]
    assert "error" in t.call("list_provisions", {"act": ACT, "parent_path": "nope"})


def test_list_documents_only_shows_our_own(rich):
    a = tb(rich).call("list_documents", {})["documents"]
    b = tb(rich, ENTITY_B).call("list_documents", {})["documents"]
    assert [d["id"] for d in a] == [DOC_A] and a[0]["checks"] == {"partial": 1} and a[0]["checked_at"] == "2021-06-01"
    assert [d["id"] for d in b] == [DOC_B]                                 # B's list holds B's published document only
    assert DOC_B not in json.dumps(a)


def test_company_profile_has_roles_and_entity_type(rich):
    r = tb(rich).call("company_profile", {})
    assert r["entity_type"] == "commercial_bank" and "roles" in r and "facts" in r


# --------------------------------------------------------------------------------------------- the regulation
def test_provision_history_names_the_amending_act(rich):
    rows = tb(rich).call("provision_history", {"act": ACT, "path": "art1"})["versions"]
    assert rows[-1]["amended_by"][0]["number"] == "9/04" and rows[0]["amended_by"] == []


def test_recent_amendments_counts_changed_provisions(rich):
    r = tb(rich).call("recent_amendments", {"act": ACT})
    assert r["amendments"][0]["number"] == "9/04" and r["amendments"][0]["provisions_changed"] == 1
    assert tb(rich).call("recent_amendments", {"since": "2030-01-01"})["amendments"] == []


def test_obligation_details_applicability_and_source(rich):
    r = tb(rich).call("obligation_details", {"obligation_id": OBL})
    assert r["applies_to_us"] is True and r["our_entity_type"] == "commercial_bank"
    assert r["condition"] and r["exceptions"] and r["source"][0]["id"] == PV1
    assert tb(rich).call("obligation_details", {"obligation_id": OBL_PSP})["applies_to_us"] is False


def test_obligation_source_text_is_citable(rich):
    t = tb(rich)
    t.call("obligation_details", {"obligation_id": OBL})
    assert PV1 in t.seen and OBL in t.seen


def test_search_leaves_out_other_entity_types(rich):
    hits = tb(rich).call("search", {"query": "statement", "kind": "obligation", "act": ACT})["hits"]
    ids = {h["id"] for h in hits}
    assert OBL in ids and OBL_PSP not in ids
    assert tb(rich).call("search", {"query": "statement", "kind": "obligation", "domain": "other"})["hits"] == []


def test_read_text_returns_the_whole_text_and_allows_quotes(rich):
    t = tb(rich)
    r = t.call("read_text", {"id": CL_A})
    assert r["text"].startswith("ჩვენი პოლიტიკა") and r["more"] is False and CL_A in t.seen
    assert "error" in t.call("read_text", {"id": CL_B})                   # another company's clause


# --------------------------------------------------------------------------------------------- our side
def test_clause_coverage_has_mapped_clauses_task_proposal_and_cause(rich):
    r = tb(rich).call("clause_coverage", {"doc_version_id": DOC_A, "obligation_id": OBL})
    assert [c["id"] for c in r["mapped_clauses"]] == [CL_A] and r["mapped_clauses"][0]["why"] == "annual only"
    assert r["task"]["status"] == "open" and r["proposal"]["explanation"] == "make it quarterly"
    assert r["caused_by_amendment"]["number"] == "9/04"
    assert "error" in tb(rich, ENTITY_B).call("clause_coverage", {"doc_version_id": DOC_A, "obligation_id": OBL})


def test_clauses_for_obligation_never_shows_company_b(rich):
    r = tb(rich).call("clauses_for_obligation", {"obligation_id": OBL})
    assert [d["document"] for d in r["documents"]] == [DOC_A] and SECRET_B not in json.dumps(r)
    assert r["documents"][0]["mapped_clauses"][0]["id"] == CL_A


def test_clauses_for_provision_goes_three_hops_for_our_company_only(rich):
    r = tb(rich).call("clauses_for_provision", {"act": ACT, "path": "art1", "as_of": "2020-06-01"})
    by = {o["obligation"]: o for o in r["obligations"]}
    assert [c["id"] for c in by[OBL]["our_clauses"]] == [CL_A]
    assert OBL_PSP not in by                                               # not addressed to our entity type
    assert by[OBL_GAP]["our_clauses"] == [] and r["without_any_clause"] == 1
    assert SECRET_B not in json.dumps(r) and CL_B not in json.dumps(r)


def test_coverage_gaps_lists_what_nothing_covers(rich):
    r = tb(rich).call("coverage_gaps", {"domain": "model_risk"})
    assert [g["obligation"] for g in r["gaps"]] == [OBL_GAP]
    assert r["by_domain"]["model_risk"]["in_force"] == 2                  # OBL + OBL_GAP (OBL_PSP is not ours)


# --------------------------------------------------------------------------------------------- change and staleness
def test_document_law_changes_separates_the_two_causes(rich):
    r = tb(rich).call("document_law_changes", {"doc_version_id": DOC_A})
    assert r["law_as_of"] == "2021-06-01"
    ch = r["changed_since_check"]
    assert ch["count"] == 1 and ch["items"][0]["new_version"] == PV2 and ch["items"][0]["amended_by"][0]["number"] == "9/04"
    assert r["flags_from_amendments"][0]["number"] == "9/04" and r["flags_from_amendments"][0]["flags"] == 1
    assert "error" in tb(rich, ENTITY_B).call("document_law_changes", {"doc_version_id": DOC_A})


def test_affected_documents(rich):
    r = tb(rich).call("affected_documents", {})
    assert [d["id"] for d in r["documents"]] == [DOC_A] and r["documents"][0]["checked_provisions_changed_since"] == 1
    assert tb(rich).call("affected_documents", {"since": "2030-01-01"})["documents"] == []     # nothing after 2030
    assert tb(rich, ENTITY_B).call("affected_documents", {})["documents"] == []


def test_neighbors_show_edge_details(rich):
    r = tb(rich).call("neighbors", {"id": PV2, "edge_type": "AMENDED_BY", "direction": "out"})
    assert r["neighbors"][0]["node"]["number"] == "9/04"
    r = tb(rich).call("neighbors", {"id": CL_A, "edge_type": "COVERED_BY", "direction": "in"})
    assert r["neighbors"][0]["role"] == "partial"


def test_every_tool_is_listed_in_the_prompt_and_callable(rich):
    t = tb(rich)
    names = list(t.tools)
    assert len(names) == len(set(names)) and {"document_flags", "obligation_details", "clauses_for_provision",
                                              "coverage_gaps", "document_law_changes", "list_acts"} <= set(names)
    for n in names:
        assert n in t.describe()


# --------------------------------------------------------------------------------------------- forgiving arguments
def test_paths_are_normalised_and_a_wrong_path_says_where_to_look(rich):
    t = tb(rich)
    assert ToolBox._norm_path("9/1") == "art9/p1" and ToolBox._norm_path("art9/1") == "art9/p1"
    assert ToolBox._norm_path("art9/p1") == "art9/p1" and ToolBox._norm_path("art9") == "art9"
    ok = t.call("provision_text", {"act": ACT, "path": "1", "as_of": "2021-06-01"})
    assert "provision" in ok                                   # "1" is read as art1
    bad = t.call("provision_text", {"act": ACT, "path": "7/3", "as_of": "2021-06-01"})
    assert "error" in bad and bad["valid_paths_here"] == ["art1"]


def test_a_missing_act_defaults_to_the_one_on_screen(rich):
    t = tb(rich)
    assert "error" in t.call("list_provisions", {})            # nothing on screen yet
    t.cursor.visit(PV1)
    assert [c["path"] for c in t.call("list_provisions", {})["children"]] == ["art1"]
