"""Phase 2: permissions, accounts, companies, document upload / versions / publish, demo seed."""
from pathlib import Path

import pytest

from kg.services import accounts, documents, permissions
from kg.services.demo import seed_demo
from kg.services.permissions import PermissionDenied

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "model_risk_company_docs"
D1 = DOCS / "D1_Model_Risk_Management_Policy_POL-RM-014.pdf"
D2 = DOCS / "D2_Model_Inventory_REG-RM-003.pdf"


def mk(store, email, kind="entity", company=None, join=None):
    return accounts.register(store, email=email, name="N " + email, password="secret1", user_type=kind,
                             company=company, join_company=join, entity_types=["commercial_bank", "other"])


@pytest.fixture
def env(kg):
    cfg, store = kg
    a = mk(store, "a@x.ge", company={"name": "ბანკი ა", "entity_type": "commercial_bank"})
    b = mk(store, "b@x.ge", company={"name": "Bank B", "entity_type": "other"})
    r = mk(store, "r@x.ge", "regulator")
    return cfg, store, a, b, r


def test_slug_and_register(env):
    cfg, store, a, b, r = env
    assert a["company_id"] == "banki-a" and b["company_id"] == "bank-b" and r["company_id"] is None
    assert accounts.authenticate(store, "A@x.ge", "secret1")["id"] == a["id"]
    assert accounts.authenticate(store, "a@x.ge", "wrong") is None
    assert "password_hash" not in a
    for kw, code in [(dict(email="bad"), "bad_email"), (dict(password="1"), "short_password"),
                     (dict(email="a@x.ge"), "email_taken"), (dict(user_type="x"), "bad_user_type")]:
        args = dict(email="n@x.ge", name="n", password="secret1", user_type="entity", join_company="bank-b")
        args.update(kw)
        with pytest.raises(accounts.AccountError) as e:
            accounts.register(store, **args)
        assert e.value.code == code
    with pytest.raises(accounts.AccountError):
        accounts.register(store, email="n@x.ge", name="n", password="secret1", user_type="entity")


def test_company_search_and_edit_permissions(env):
    cfg, store, a, b, r = env
    assert {c["id"] for c in accounts.list_companies(store, r)} == {"banki-a", "bank-b"}
    assert [c["id"] for c in accounts.list_companies(store, r, "bank b")] == ["bank-b"]
    with pytest.raises(PermissionDenied):
        accounts.list_companies(store, a)
    with pytest.raises(PermissionDenied):
        accounts.update_company(store, a, "bank-b", name="hack")
    with pytest.raises(PermissionDenied):
        accounts.update_company(store, r, "banki-a", name="hack")
    c = accounts.update_company(store, a, "banki-a", props={"size": "large"})
    assert c["props"]["size"] == "large"


def test_roles(env):
    cfg, store, a, b, r = env
    role = accounts.add_role(store, a, "banki-a", "Risk")
    accounts.assign_role(store, a, "banki-a", a["id"], role["id"])
    assert accounts.members(store, a, "banki-a")[0]["roles"] == ["Risk"]
    assert accounts.members(store, r, "banki-a")
    with pytest.raises(PermissionDenied):
        accounts.members(store, b, "banki-a")
    with pytest.raises(PermissionDenied):
        accounts.assign_role(store, b, "bank-b", b["id"], role["id"])
    accounts.remove_role(store, a, "banki-a", role["id"])
    assert accounts.list_roles(store, "banki-a") == []


def test_matrix():
    ent = {"id": 1, "user_type": "entity", "company_id": "c"}
    reg = {"id": 2, "user_type": "regulator", "company_id": None}
    assert permissions.allowed(ent, "document.upload", "c") and not permissions.allowed(ent, "document.upload", "d")
    assert not permissions.allowed(reg, "document.upload", "c")
    assert permissions.allowed(reg, "document.view", "c") and permissions.allowed(reg, "company.search")
    assert not permissions.allowed(ent, "company.search")
    assert not permissions.allowed(None, "document.view", "c")
    assert permissions.visible_statuses(reg) == ("published",) and permissions.visible_statuses(ent) is None


def test_upload_publish_flow(env):
    cfg, store, a, b, r = env
    data = D1.read_bytes()
    res = documents.upload(cfg, store, a, data, D1.name)
    assert res["status"] == "ok" and res["units"] > 5
    vid = res["version_id"]
    assert vid.startswith("doc:banki-a/") and (cfg.path("uploads") / "banki-a").exists()
    # clauses carry bbox and company
    cl = [n for n in store.nodes("Clause") if n["props"]["doc"] == vid]
    assert cl and any(n["props"].get("bbox") for n in cl) and all(n["props"]["company_id"] == "banki-a" for n in cl)
    assert store.get_node(vid)["props"]["status"] == "draft"
    # duplicate file
    assert documents.upload(cfg, store, a, data, "again.pdf")["status"] == "duplicate"
    # same code as new document is refused
    # visibility
    assert documents.list_families(store, r, "banki-a") == []
    with pytest.raises(PermissionDenied):
        documents.get_version(store, r, vid)
    with pytest.raises(PermissionDenied):
        documents.list_families(store, b, "banki-a")
    with pytest.raises(PermissionDenied):
        documents.upload(cfg, store, r, data, "x.pdf")
    # publish
    with pytest.raises(PermissionDenied):
        documents.publish(store, b, vid)
    documents.publish(store, a, vid)
    assert store.get_node(vid)["props"]["status"] == "published"
    assert documents.list_families(store, r, "banki-a")[0]["versions"][0]["id"] == vid
    with pytest.raises(documents.DocumentError):
        documents.publish(store, a, vid)


def test_bad_pdf(env):
    cfg, store, a, *_ = env
    with pytest.raises(documents.DocumentError) as e:
        documents.upload(cfg, store, a, b"not a pdf", "x.pdf")
    assert e.value.code == "unreadable_pdf"
    assert not list((cfg.path("uploads") / "banki-a").glob("*")) if (cfg.path("uploads") / "banki-a").exists() else True


def test_new_version_keep_and_replace(env):
    cfg, store, a, b, r = env
    v1 = documents.upload(cfg, store, a, D1.read_bytes(), D1.name)
    documents.publish(store, a, v1["version_id"])
    fam = v1["family_id"]
    # a different file uploaded as a new version of the family (code is taken from the family)
    v2 = documents.upload(cfg, store, a, D2.read_bytes(), "v2.pdf", mode="new_version", family_id=fam,
                          version_policy="replace")
    assert v2["status"] == "ok" and v2["family_id"] == fam and v2["code"] == v1["code"]
    assert v2["version_id"] != v1["version_id"]
    documents.publish(store, a, v2["version_id"])
    st = {v["id"]: v["status"] for f in documents.list_families(store, a, "banki-a") for v in f["versions"]}
    assert st[v1["version_id"]] == "replaced" and st[v2["version_id"]] == "published"
    # a version of someone else's family is refused
    with pytest.raises(documents.DocumentError):
        documents.upload(cfg, store, b, D1.read_bytes(), "z.pdf", mode="new_version", family_id=fam)


def test_open_tasks_block_publish_and_discard(env):
    cfg, store, a, *_ = env
    v = documents.upload(cfg, store, a, D1.read_bytes(), D1.name)
    vid = v["version_id"]
    store.x("INSERT INTO tasks(key,company_id,doc_version_id,obligation_id,title,status,score,priority,created_at,"
            "updated_at) VALUES('t1','banki-a',?, 'o','t','open',1,'high','x','x')", (vid,))
    with pytest.raises(documents.DocumentError) as e:
        documents.publish(store, a, vid)
    assert e.value.code == "open_tasks"
    store.x("DELETE FROM tasks")
    documents.discard_draft(store, a, vid, cfg)
    assert store.get_node(vid) is None
    assert not [n for n in store.nodes("Clause") if n["props"]["doc"] == vid]
    assert store.q("SELECT * FROM documents") == []
    assert not list((cfg.path("uploads") / "banki-a").glob("*.pdf"))


def test_seed_demo(kg):
    cfg, store = kg
    out = seed_demo(cfg, store)
    assert len(out["documents"]) == 7 and len(out["users"]) == 2
    again = seed_demo(cfg, store)
    assert again["documents"] == []
    nodes = [n for n in store.nodes("InternalDoc") if n["props"].get("company_id") == "bank-a"]
    assert len(nodes) == 7
    assert {n["props"]["status"] for n in nodes} == {"published"}
    assert all(n["id"].startswith("doc:bank-a/") for n in nodes)
    reg = accounts.authenticate(store, "nbg@demo.ge", "demo1234")
    assert len(documents.list_families(store, reg, "bank-a")) == 7
