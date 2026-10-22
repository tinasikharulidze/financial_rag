"""Company documents: upload (new document / new version), parsing, status, publish (docs/KG_V2_PLAN.md section 6.1).

Lifecycle of a document version:   draft  --publish-->  published  --(a newer version replaces it)-->  replaced

* ``new_document``                  a new family (one logical document of a company).
* ``new_version`` + ``keep``        the old version stays as it is; the new one is a draft until published.
* ``new_version`` + ``replace``     when the new version is published, the old one becomes ``replaced`` and its
                                    open tasks become ``replaced``.
* The same file (same sha1) uploaded twice for a company is refused.
* A regulator sees published versions only; a regulated entity sees its own drafts as well.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kg.company.ingest import MAPPED_TYPES, company_doc_id, write_doc
from kg.company.pdf_parser import ParsedDoc, parse_pdf
from kg.config import now_iso
from kg.services.decisions import record_decision
from kg.services.permissions import PermissionDenied, require, visible_statuses

STATUSES = ("draft", "published", "replaced", "archived")


class DocumentError(ValueError):
    def __init__(self, code: str, message: str = "", **data):
        super().__init__(message or code)
        self.code = code
        self.data = data


# ------------------------------------------------------------------------------------------------ reading
def _family(store, family_id: str) -> dict | None:
    return store.q1("SELECT * FROM documents WHERE id=?", (family_id,))


def _version(store, version_id: str) -> dict | None:
    return store.q1("SELECT dv.*, d.company_id, d.code, d.title FROM document_versions dv "
                    "JOIN documents d ON d.id=dv.family_id WHERE dv.id=?", (version_id,))


def get_version(store, actor: dict, version_id: str) -> dict:
    v = _version(store, version_id)
    if v is None:
        raise DocumentError("no_version", "document version not found")
    require(actor, "document.view", v["company_id"])
    vis = visible_statuses(actor)
    if vis is not None and v["status"] not in vis:
        raise PermissionDenied("this version is not published")
    return v


def list_families(store, actor: dict, company_id: str) -> list[dict]:
    """Families of a company with their (visible) versions, newest first."""
    require(actor, "document.view", company_id)
    vis = visible_statuses(actor)
    out = []
    for f in store.q("SELECT * FROM documents WHERE company_id=? ORDER BY code", (company_id,)):
        vs = store.q("SELECT * FROM document_versions WHERE family_id=? ORDER BY uploaded_at DESC, id DESC",
                     (f["id"],))
        if vis is not None:
            vs = [v for v in vs if v["status"] in vis]
        if vs or vis is None:
            out.append({**f, "versions": vs})
    return out


def latest_version(store, family_id: str, statuses: tuple[str, ...] = ("published", "draft")) -> dict | None:
    for v in store.q("SELECT * FROM document_versions WHERE family_id=? ORDER BY uploaded_at DESC, id DESC",
                     (family_id,)):
        if v["status"] in statuses:
            return v
    return None


def file_path(cfg, version: dict) -> Path:
    return cfg.path("uploads") / version["file_path"]


# ------------------------------------------------------------------------------------------------ writing
def _unique_version(store, company_id: str, code: str, version: str) -> str:
    v, n = version, 1
    while store.q1("SELECT 1 FROM document_versions WHERE id=?", (f"doc:{company_id}/{code}@{v}",)):
        n += 1
        v = f"{version}+{n}"
    return v


def register_version(cfg, store, actor_id: int | None, company_id: str, pd: ParsedDoc, rel_file: str, *, mode: str,
                     family_id: str | None, status: str = "draft", source: str = "upload",
                     company_name: str | None = None) -> dict:
    """Create the family (if new), the document_versions row and the graph nodes of a parsed document."""
    code = pd.doc_code or f"UPLOAD-{pd.sha1[:8]}"
    if family_id:
        fam = _family(store, family_id)
        code = fam["code"]
        pd.doc_code = code
    else:
        family_id = f"{company_id}/{code}"
        pd.doc_code = code
    pd.version = _unique_version(store, company_id, code, pd.version)
    vid = company_doc_id(company_id, pd)
    if _family(store, family_id) is None:
        store.x("INSERT INTO documents(id,company_id,code,title,created_at) VALUES(?,?,?,?,?)",
                (family_id, company_id, code, pd.title, now_iso()))
    store.x("INSERT INTO document_versions(id,family_id,version,status,file_path,sha1,uploaded_by,uploaded_at,"
            "published_at,replaces,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (vid, family_id, pd.version, status, rel_file, pd.sha1, actor_id, now_iso(),
             now_iso() if status == "published" else None, None, mode))
    write_doc(store, pd, company=company_name, source=source, company_id=company_id, family_id=family_id,
              status=status, doc_id=vid, extra_props={"version_id": vid})
    return {"version_id": vid, "family_id": family_id, "version": pd.version, "code": code}


def upload(cfg, store, actor: dict, data: bytes, filename: str, *, mode: str = "new_document",
           family_id: str | None = None, version_policy: str = "keep", llm=None) -> dict:
    """Upload a PDF as a draft. ``mode``: new_document | new_version; ``version_policy``: keep | replace."""
    company_id = actor.get("company_id")
    require(actor, "document.upload", company_id)
    if mode not in ("new_document", "new_version"):
        raise DocumentError("bad_mode", "mode must be new_document or new_version")
    if version_policy not in ("keep", "replace"):
        raise DocumentError("bad_policy", "version_policy must be keep or replace")
    if mode == "new_version":
        fam = _family(store, family_id) if family_id else None
        if fam is None or fam["company_id"] != company_id:
            raise DocumentError("no_family", "choose one of your documents")
    sha1 = hashlib.sha1(data).hexdigest()
    dup = store.q1("SELECT dv.id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                   "WHERE d.company_id=? AND dv.sha1=?", (company_id, sha1))
    if dup:
        return {"status": "duplicate", "version_id": dup["id"]}
    rel = f"{company_id}/{sha1}.pdf"
    target = cfg.path("uploads") / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        pd = parse_pdf(target, llm=llm, classify_llm=llm is not None)
    except Exception as exc:  # noqa: BLE001 - unreadable / not a PDF
        target.unlink(missing_ok=True)
        raise DocumentError("unreadable_pdf", f"could not read the PDF: {exc}") from exc
    if not pd.units:
        target.unlink(missing_ok=True)
        raise DocumentError("empty_pdf", "no text could be extracted from the PDF")
    pd.file = filename or pd.file
    if mode == "new_document":
        code = pd.doc_code or f"UPLOAD-{pd.sha1[:8]}"
        if _family(store, f"{company_id}/{code}"):
            raise DocumentError("family_exists", "a document with this code exists: upload it as a new version",
                                family_id=f"{company_id}/{code}")
    company = store.q1("SELECT name FROM companies WHERE id=?", (company_id,))
    info = register_version(cfg, store, actor["id"], company_id, pd, rel,
                            mode="new" if mode == "new_document" else version_policy,
                            family_id=family_id if mode == "new_version" else None,
                            company_name=(company or {}).get("name"))
    return {"status": "ok", **info, "warnings": list(pd.warnings), "units": len(pd.units),
            "mapped_units": sum(1 for u in pd.units if u.unit_type in MAPPED_TYPES), "title": pd.title,
            "doc_date": pd.doc_date, "pages": pd.pages}


def discard_draft(store, actor: dict, version_id: str, cfg=None):
    v = _version(store, version_id)
    if v is None:
        raise DocumentError("no_version")
    require(actor, "decision.make", v["company_id"])
    if v["status"] != "draft":
        raise DocumentError("not_draft", "only drafts can be discarded")
    ids = [n["id"] for n in store.nodes("Clause") if n["props"].get("doc") == version_id] + [version_id]
    store.delete_nodes(ids)
    for t in ("triage", "locations", "coverage", "proposals", "doc_profiles"):
        store.x(f"DELETE FROM {t} WHERE doc_version_id=?", (version_id,))
    store.x("DELETE FROM tasks WHERE doc_version_id=?", (version_id,))
    store.x("DELETE FROM document_versions WHERE id=?", (version_id,))
    fam = _family(store, v["family_id"])
    if fam and not store.q1("SELECT 1 FROM document_versions WHERE family_id=?", (v["family_id"],)):
        store.x("DELETE FROM documents WHERE id=?", (v["family_id"],))
    # the uploaded file is kept only while another version of the company still uses it
    if cfg is not None and not store.q1("SELECT 1 FROM document_versions WHERE file_path=?", (v["file_path"],)):
        (cfg.path("uploads") / v["file_path"]).unlink(missing_ok=True)


def open_task_count(store, version_id: str) -> int:
    """Open tasks of a version, without the hidden duplicates (``tasks.duplicate_new_documents``)."""
    from kg.services.tasks import duplicate_new_documents
    rows = store.q("SELECT t.obligation_id, p.kind FROM tasks t LEFT JOIN proposals p ON p.id=t.proposal_id "
                   "WHERE t.doc_version_id=? AND t.status='open'", (version_id,))
    if not any(r["kind"] == "new_document" for r in rows):
        return len(rows)
    c = store.q1("SELECT d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                 "WHERE dv.id=?", (version_id,))
    dup = duplicate_new_documents(store, c["company_id"]) if c else set()
    return sum(1 for r in rows if not (r["kind"] == "new_document" and (version_id, r["obligation_id"]) in dup))


def publish(store, actor: dict, version_id: str, force: bool = False) -> dict:
    """draft -> published. Refused while the version still has open tasks (unless ``force``)."""
    v = _version(store, version_id)
    if v is None:
        raise DocumentError("no_version")
    require(actor, "decision.make", v["company_id"])
    if v["status"] != "draft":
        raise DocumentError("not_draft", "only a draft can be published")
    n_open = open_task_count(store, version_id)
    if n_open and not force:
        raise DocumentError("open_tasks", f"{n_open} open tasks", open_tasks=n_open)
    replaced = []
    if v["mode"] == "replace":
        for old in store.q("SELECT id FROM document_versions WHERE family_id=? AND status='published' AND id<>?",
                           (v["family_id"], version_id)):
            store.x("UPDATE document_versions SET status='replaced' WHERE id=?", (old["id"],))
            store.x("UPDATE tasks SET status='replaced', updated_at=? WHERE doc_version_id=? "
                    "AND status IN ('open','accepted')", (now_iso(), old["id"]))
            for t in store.q("SELECT key FROM tasks WHERE doc_version_id=? AND status='replaced'", (old["id"],)):
                store.x("INSERT INTO task_events(task_key,actor,event,data,ts) VALUES(?,?,?,?,?)",
                        (t["key"], actor["id"], "status_change",
                         json.dumps({"to": "replaced", "by_version": version_id}), now_iso()))
            _set_node_status(store, old["id"], "replaced")
            replaced.append(old["id"])
        store.x("UPDATE document_versions SET replaces=? WHERE id=?", (replaced[0] if replaced else None, version_id))
    store.x("UPDATE document_versions SET status='published', published_at=? WHERE id=?", (now_iso(), version_id))
    _set_node_status(store, version_id, "published")
    record_decision(store, actor, "publish", doc_version_id=version_id, data={"replaced": replaced})
    return {"version_id": version_id, "status": "published", "replaced": replaced}


def _set_node_status(store, version_id: str, status: str):
    if store.get_node(version_id):
        store.patch_node(version_id, {"status": status})
