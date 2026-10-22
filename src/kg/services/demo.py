"""Demo data: company ``bank-a`` with the 7 corpus documents as published versions, one entity user, one regulator."""
from __future__ import annotations

import shutil

from kg.company.pdf_parser import parse_pdf
from kg.services import accounts
from kg.services.documents import register_version


def seed_demo(cfg, store, llm=None, with_docs: bool = True) -> dict:
    """Idempotent: existing company / users / documents are left untouched."""
    d = cfg.get("demo")
    c = d["company"]
    out = {"company": c["id"], "users": [], "documents": []}
    if accounts.get_company(store, c["id"]) is None:
        accounts.create_company(store, name=c["name"], entity_type=c["entity_type"], reg_number=c.get("reg_number"),
                                company_id=c["id"], entity_types=cfg.get("entity_types"))
    for u in d["users"]:
        row = store.q1("SELECT id FROM users WHERE email=?", (u["email"],))
        if row is None:
            kw = {"join_company": c["id"]} if u["user_type"] == "entity" else {}
            row = accounts.register(store, email=u["email"], name=u["name"], password=u["password"],
                                    user_type=u["user_type"], **kw)
        out["users"].append(row["id"])
    owner = store.q1("SELECT id FROM users WHERE email=?", (d["users"][0]["email"],))
    actor = accounts.get_user(store, owner["id"])
    for r in d.get("roles", []):
        accounts.add_role(store, actor, c["id"], r)
    if with_docs:
        for f in sorted(cfg.path("company_docs").glob("D*.pdf")):
            sha = None
            pd = parse_pdf(f, llm=llm, classify_llm=False)
            if store.q1("SELECT 1 FROM document_versions dv JOIN documents x ON x.id=dv.family_id "
                        "WHERE x.company_id=? AND dv.sha1=?", (c["id"], pd.sha1)):
                continue
            rel = f"{c['id']}/{pd.sha1}.pdf"
            (cfg.path("uploads") / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, cfg.path("uploads") / rel)
            info = register_version(cfg, store, owner["id"], c["id"], pd, rel, mode="new", family_id=None,
                                    status="published", source="demo", company_name=c["name"])
            out["documents"].append(info["version_id"])
    return out
