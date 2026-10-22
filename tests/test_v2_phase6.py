"""Phase 6: ranking, tasks (sync, lifecycle, board filters), user decisions and rule-based recomputation."""
import json
from pathlib import Path

import pytest
from fake_llm import FakeGemini

from kg.company import profile as prof
from kg.extract.obligations_v2 import extract_obligations_v2
from kg.llm import LLM
from kg.mapping import ranking
from kg.mapping.docview import load_doc
from kg.mapping.pipeline import map_document
from kg.mapping.proposals import get_proposals, propose_document
from kg.runlog import Run
from kg.services import accounts, decisions as D, documents, tasks as T
from kg.services.demo import seed_demo
from kg.services.permissions import PermissionDenied

NOW = "2025-01-01"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def env(kg, tmp_path):
    cfg, store = kg
    fake = FakeGemini()
    run = Run(store, cfg.path("runs"), "t-phase6")
    run.__enter__()
    llm = LLM(cfg, store, run.id, backend=fake, cache_dir=tmp_path / "c")
    extract_obligations_v2(cfg, store, run, llm)
    docs = seed_demo(cfg, store)["documents"]
    owner = accounts.authenticate(store, "bank@demo.ge", "demo1234")
    reg = accounts.authenticate(store, "nbg@demo.ge", "demo1234")
    v = next(d for d in docs if "POL-RM-014" in d)
    prof.build_profile(cfg, store, run, llm, v)
    prof.confirm_profile(store, owner, v, domains=["model_risk"])
    map_document(cfg, store, run, llm, v, now=NOW)
    propose_document(cfg, store, run, llm, v)
    stats = T.sync_tasks(cfg, store, run, v)
    yield cfg, store, run, llm, fake, v, owner, reg, stats
    run.__exit__(None, None, None)


def board(env, **kw):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    return T.list_tasks(cfg, store, owner, "bank-a", **kw)


def test_ranking_formula(env):
    cfg = json.loads(json.dumps(dict(env[0])))                  # the formula with explicit severity weights
    cfg["ranking"] = {"severity": {"critical": 4, "high": 3, "medium": 2, "low": 1},
                      "priority_thresholds": {"critical": 3.5, "high": 2.4, "medium": 1.2}}
    assert ranking.score(cfg, "critical", "none", "relevant") == 4.0
    assert ranking.score(cfg, "critical", "contradicts", "relevant") == 4.0            # same as none
    assert ranking.score(cfg, "high", "partial", "possibly") == pytest.approx(3 * 0.6 * 0.6)
    assert [ranking.priority(cfg, s) for s in (4, 3.5, 2.4, 1.2, 1.19)] == ["critical", "critical", "high", "medium", "low"]
    assert ranking.explain(cfg, "high", "partial", "relevant")["factors"] == {"severity": 3, "gap": 0.6, "relevance": 1.0}


def test_ranking_without_severity_from_the_config(env):
    cfg = env[0]                                                 # configs/kg.yaml: severity not used
    assert ranking.score(cfg, "low", "none", "relevant") == ranking.score(cfg, "critical", "none", "relevant") == 0.8
    P = lambda v, r: ranking.priority(cfg, ranking.score(cfg, "high", v, r))  # noqa: E731
    assert P("contradicts", "relevant") == "critical"          # 1.0
    assert P("none", "relevant") == "high"                     # 0.8 (missing)
    assert P("partial", "relevant") == "medium"                # 0.6
    assert P("contradicts", "possibly") == "medium"            # 0.5
    assert P("none", "possibly") == "medium"                   # 0.4
    assert P("partial", "possibly") == "low"                   # 0.3
    assert ranking.explain(cfg, "high", "partial", "relevant")["formula"] == "gap x relevance"


def test_sync_creates_one_task_per_proposal(env):
    cfg, store, run, llm, fake, v, owner, reg, stats = env
    props = get_proposals(store, v)
    rows = board(env)
    assert stats["created"] == len(props) == len(rows) > 0
    assert all(r["status"] == "open" and r["key"].startswith(f"bank-a|{v}|") or r["kind"] == "new_document" for r in rows)
    assert [r["score"] for r in rows] == sorted((r["score"] for r in rows), reverse=True)
    for r in rows:
        assert r["score"] == ranking.score(cfg, r["severity"], r["verdict"], r["relevance"])
        assert r["priority"] == ranking.priority(cfg, r["score"])
        assert r["proposal_id"] in {p["id"] for p in props}
    ev = store.q("SELECT event FROM task_events")
    assert {e["event"] for e in ev} == {"created"}
    again = T.sync_tasks(cfg, store, env[2], v)
    assert again["created"] == 0 and again["updated"] == 0 and again["kept"] == len(rows)
    assert store.q1("SELECT COUNT(*) n FROM task_events")["n"] == len(rows)


def test_changing_weights_rescores_without_llm(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    before = {r["key"]: (r["score"], r["priority"]) for r in board(env)}
    calls = sum(fake.calls.values())
    cfg["ranking"]["gap"]["partial"] = 0.1
    cfg["ranking"]["priority_thresholds"]["high"] = 2.0
    n = T.rescore(cfg, store)
    assert sum(fake.calls.values()) == calls
    after = {r["key"]: (r["score"], r["priority"]) for r in board(env)}
    assert n > 0 and after != before
    partial = [r for r in board(env) if r["verdict"] == "partial"]
    assert partial and all(r["score"] == pytest.approx(ranking.score(cfg, r["severity"], "partial", r["relevance"])) for r in partial)
    ex = T.explain_task(cfg, store, owner, partial[0]["key"])
    assert ex["ranking"]["factors"]["gap"] == 0.1 and ex["coverage"]["verdict"] == "partial" and ex["events"]


def test_accept_and_permissions(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    key = board(env)[0]["key"]
    with pytest.raises(PermissionDenied):
        D.accept(store, reg, key)
    other = accounts.register(store, email="x@y.ge", name="X", password="secret1", user_type="entity",
                              company={"name": "Other", "entity_type": "other"})
    with pytest.raises(PermissionDenied):
        D.accept(store, other, key)
    with pytest.raises(PermissionDenied):
        T.list_tasks(cfg, store, other, "bank-a")
    t = D.accept(store, owner, key)
    assert t["status"] == "accepted"
    with pytest.raises(D.DecisionError):
        D.accept(store, owner, key)
    dec = D.decisions_of(store, reg, v, t["obligation_id"])                  # the regulator sees who decided what
    assert dec[-1]["kind"] == "accept" and dec[-1]["actor_name"]
    assert [e["event"] for e in T.events_of(store, reg, key)][-1] == "status_change"


def test_regulator_sees_published_versions_only(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    assert T.list_tasks(cfg, store, reg, "bank-a")
    store.x("UPDATE document_versions SET status='draft' WHERE id=?", (v,))
    assert T.list_tasks(cfg, store, reg, "bank-a") == []
    assert T.list_tasks(cfg, store, owner, "bank-a")


def test_not_relevant_is_final(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    t0 = next(r for r in board(env) if r["kind"] != "new_document")
    key, oid = t0["key"], t0["obligation_id"]
    with pytest.raises(D.DecisionError):
        D.not_relevant(store, owner, key, "  ")
    D.not_relevant(store, owner, key, "ეს მოთხოვნა სხვა დოკუმენტში გვაქვს.")
    tri = store.q1("SELECT * FROM triage WHERE doc_version_id=? AND obligation_id=?", (v, oid))
    assert tri["source"] == "user" and tri["relevance"] == "irrelevant"
    n = sum(fake.calls.values())
    map_document(cfg, store, run, llm, v, now=NOW)                           # the pipeline never writes over user rows
    propose_document(cfg, store, run, llm, v)
    T.sync_tasks(cfg, store, run, v)
    assert store.q1("SELECT status FROM tasks WHERE key=?", (key,))["status"] == "not_relevant"
    assert store.q1("SELECT source FROM triage WHERE doc_version_id=? AND obligation_id=?", (v, oid))["source"] == "user"
    assert sum(fake.calls.values()) == n
    # reopen -> the triage row of the user disappears
    D.reopen(store, owner, key)
    assert store.q1("SELECT status FROM tasks WHERE key=?", (key,))["status"] == "open"
    assert not store.q1("SELECT 1 FROM triage WHERE doc_version_id=? AND obligation_id=?", (v, oid))


def test_already_addressed_uses_the_chosen_clauses(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    t0 = next(r for r in board(env) if r["kind"] != "new_document")
    key, oid = t0["key"], t0["obligation_id"]
    view = load_doc(store, v)
    clause = [u["id"] for u in view.mapped][7]
    with pytest.raises(D.DecisionError):
        D.already_addressed(store, owner, key, [])
    with pytest.raises(D.DecisionError):
        D.already_addressed(store, owner, key, ["cl:nope"])
    D.already_addressed(store, owner, key, [clause])
    cov = store.q1("SELECT * FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v, oid))
    assert cov["verdict"] == "full" and cov["source"] == "user"
    assert any(e["dst"] == clause and e["props"]["source"] == "user" and e["props"]["role"] == "full"
               for e in store.edges("COVERED_BY") if e["src"] == oid)
    assert store.q1("SELECT status FROM tasks WHERE key=?", (key,))["status"] == "already_addressed"
    # the audit trail keeps the proposal; the judge is not asked again
    propose_document(cfg, store, run, llm, v)
    assert any(p["obligation_id"] == oid for p in get_proposals(store, v))
    map_document(cfg, store, run, llm, v, now=NOW, rejudge=True)        # skips the user-decided obligation
    assert store.q1("SELECT source FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v, oid))["source"] == "user"
    D.reopen(store, owner, key)
    assert not store.q1("SELECT 1 FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v, oid))


def test_edge_edits_recompute_the_verdict_by_rule(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    t0 = next(r for r in board(env) if r["verdict"] == "partial")
    key, oid = t0["key"], t0["obligation_id"]
    view = load_doc(store, v)
    clause = [u["id"] for u in view.mapped][11]
    assert D.edge_add(cfg, store, run, owner, v, oid, clause, "full") == "full"
    assert store.q1("SELECT status FROM tasks WHERE key=?", (key,))["status"] == "resolved"      # no longer a gap
    assert [e["event"] for e in T.events_of(store, owner, key)][-1] == "auto_resolved"
    # change to contradicts: the gap is back (the task is re-opened by the sync)
    assert D.edge_change(cfg, store, run, owner, v, oid, clause, "contradicts") == "contradicts"
    assert store.q1("SELECT status, verdict FROM tasks WHERE key=?", (key,)) == {"status": "open", "verdict": "contradicts"}
    # remove the user edge and every model edge: nothing covers the obligation any more
    for e in D.edges_of(store, v, oid):
        if e["role"] in D.USER_ROLES:
            D.edge_remove(cfg, store, run, owner, v, oid, e["clause_id"])
    assert store.q1("SELECT verdict FROM coverage WHERE doc_version_id=? AND obligation_id=?", (v, oid))["verdict"] == "none"
    rej = [e for e in D.edges_of(store, v, oid) if e["role"] == "rejected"]
    assert rej
    # a re-check tells the model which clauses are rejected and never keeps them
    from kg.mapping.pipeline import recheck_obligation
    fake.judge_force = {oid: {"verdict": "partial", "edges": [(rej[0]["clause_id"], "partial")]}}
    recheck_obligation(cfg, store, run, llm, v, oid)
    assert "rejected" in fake.last_user["judge"]
    kinds = [d["kind"] for d in D.decisions_of(store, owner, v, oid)]
    assert {"edge_add", "edge_change", "edge_remove"} <= set(kinds)
    with pytest.raises(PermissionDenied):
        D.edge_add(cfg, store, run, reg, v, oid, clause, "full")
    with pytest.raises(D.DecisionError):
        D.edge_add(cfg, store, run, owner, v, oid, "cl:nope", "full")


def test_task_reopened_only_by_an_amendment(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    t0 = next(r for r in board(env) if r["kind"] != "new_document")
    key, oid = t0["key"], t0["obligation_id"]
    D.not_relevant(store, owner, key, "მიზეზი")
    ob = store.get_node(oid)
    new_id = oid + "~amended"
    props = {**ob["props"], "amendment_id": "amend:1", "amendment_number": "99/04", "statement": ob["props"]["statement"] + " ახალი."}
    store.upsert_nodes([(new_id, "Obligation", props, "2024-06-01", None)])
    store.x("INSERT INTO coverage(doc_version_id,obligation_id,verdict,missing,rationale,citations,cause,amendment_id,"
            "llm_output_id,source,invalid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (v, new_id, "none", "აკლია", "r", "[]", "amendment", "amend:1", None, "llm", 0))
    p = get_proposals(store, v)[0]
    store.x("INSERT INTO proposals(doc_version_id,obligation_id,kind,clause_id,original_text,proposed_text,heading,"
            "explanation,citations,flags,llm_output_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (v, new_id, "add_section", p["clause_id"] or [u["id"] for u in load_doc(store, v).mapped][0], None, "ტექსტი",
             "სათაური", "e", "[]", "[]", None, "x"))
    T.sync_tasks(cfg, store, run, v)
    t = store.q1("SELECT * FROM tasks WHERE key=?", (key,))
    assert t["status"] == "open" and t["obligation_id"] == new_id and t["cause"] == "amendment"
    assert t["amendment_number"] == "99/04" and t["amendment_effective"] == "2024-06-01"
    assert "reopened_by_amendment" in [e["event"] for e in T.events_of(store, owner, key)]
    assert T.is_new(cfg, t, today="2024-09-01") and not T.is_new(cfg, t, today="2026-01-01")
    only_new = T.list_tasks(cfg, store, owner, "bank-a", only_new=True, today="2024-09-01")
    assert [r["key"] for r in only_new] == [key]
    assert [r["key"] for r in T.list_tasks(cfg, store, owner, "bank-a", amendment_number="99/04")] == [key]


def test_newer_version_resolves_older_tasks(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    t0 = board(env)[0]
    fam = store.q1("SELECT family_id FROM document_versions WHERE id=?", (v,))["family_id"]
    v2 = v.replace("@1.0", "@2.0")
    store.x("INSERT INTO document_versions(id,family_id,version,status,file_path,sha1,uploaded_at,mode) "
            "VALUES(?,?,?,?,?,?,?,?)", (v2, fam, "2.0", "draft", "x", "y", "now", "replace"))
    store.x("INSERT INTO coverage(doc_version_id,obligation_id,verdict,cause,source,invalid) VALUES(?,?,?,?,?,?)",
            (v2, t0["obligation_id"], "full", "pre_existing", "llm", 0))
    T.sync_tasks(cfg, store, run, v2)
    assert store.q1("SELECT status FROM tasks WHERE key=?", (t0["key"],))["status"] == "resolved"
    assert "auto_resolved" in [e["event"] for e in T.events_of(store, owner, t0["key"])]


def test_publish_replace_retires_open_tasks(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    d2 = (ROOT / "model_risk_company_docs" / "D2_Model_Inventory_REG-RM-003.pdf").read_bytes() + b"\n"   # new sha1
    fam = store.q1("SELECT family_id FROM document_versions WHERE id=?", (v,))["family_id"]
    res = documents.upload(cfg, store, owner, d2, "new.pdf", mode="new_version", family_id=fam, version_policy="replace")
    n_open = store.q1("SELECT COUNT(*) n FROM tasks WHERE doc_version_id=? AND status='open'", (v,))["n"]
    assert n_open > 0
    out = documents.publish(store, owner, res["version_id"])
    assert out["replaced"] == [v]
    assert store.q1("SELECT COUNT(*) n FROM tasks WHERE doc_version_id=? AND status='open'", (v,))["n"] == 0
    assert store.q1("SELECT COUNT(*) n FROM tasks WHERE doc_version_id=? AND status='replaced'", (v,))["n"] == n_open
    assert store.get_node(v)["props"]["status"] == "replaced"


def test_assign_and_comment(env):
    cfg, store, run, llm, fake, v, owner, reg, _ = env
    key = board(env)[0]["key"]
    role = accounts.add_role(store, owner, "bank-a", "Risk")
    T.assign(store, owner, key, user_id=owner["id"], role_id=role["id"], due_date="2025-03-01")
    t = T.get_task(store, reg, key)
    assert t["assignee_user"] == owner["id"] and t["due_date"] == "2025-03-01"
    other = accounts.register(store, email="x@y.ge", name="X", password="secret1", user_type="entity",
                              company={"name": "Other", "entity_type": "other"})
    with pytest.raises(PermissionDenied):
        T.assign(store, owner, key, user_id=other["id"])
    T.comment(store, owner, key, "შენიშვნა")
    with pytest.raises(T.TaskError):
        T.comment(store, owner, key, " ")
    assert [r["key"] for r in T.list_tasks(cfg, store, owner, "bank-a", assignee_user=owner["id"])] == [key]
    ev = T.events_of(store, reg, key)
    assert ev[-1]["event"] == "comment" and ev[-1]["data"]["text"] == "შენიშვნა"
    assert T.list_tasks(cfg, store, owner, "bank-a", priority="nonexistent") == []
