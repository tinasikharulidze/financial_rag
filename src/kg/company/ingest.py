"""``ingest-company-docs``: PDFs -> InternalDoc + Clause nodes."""
from __future__ import annotations

import re
from pathlib import Path

from kg.company.pdf_parser import ParsedDoc, parse_pdf

MAPPED_TYPES = ("clause", "paragraph", "table")


def clause_id(doc_id: str, u) -> str:
    base = doc_id.replace("doc:", "cl:")
    if u.unit_type == "clause" and u.section_no:
        return f"{base}/{u.section_no}"
    if u.unit_type == "heading" and u.section_no:
        return f"{base}/{u.section_no}#h"
    if u.unit_type == "paragraph" and u.section_no:
        return f"{base}/{u.section_no}#p{u.order}"
    if u.unit_type == "table":
        return f"{base}/t{u.order}"
    if u.unit_type == "metadata":
        return f"{base}/meta"
    return f"{base}/{u.unit_type[:3]}{u.order}"


def company_doc_id(company_id: str, pd: ParsedDoc) -> str:
    """Node id of one document VERSION of a company: ``doc:<company>/<code>@<version>`` (plan section 4.1)."""
    code = pd.doc_code or f"UPLOAD-{pd.sha1[:8]}"
    return f"doc:{company_id}/{code}@{pd.version}"


def write_doc(store, pd: ParsedDoc, company: str | None = None, source: str = "corpus", company_id: str | None = None,
              family_id: str | None = None, status: str | None = None, doc_id: str | None = None,
              extra_props: dict | None = None) -> dict:
    doc_id = doc_id or (company_doc_id(company_id, pd) if company_id else pd.doc_id)
    # drop a previous version of the same document (idempotent re-ingestion)
    old = [n["id"] for n in store.nodes("Clause") if n["props"].get("doc") == doc_id]
    if old:
        store.delete_nodes(old)
    props = {"file": pd.file, "title": pd.title, "doc_code": pd.doc_code, "version": pd.version,
             "doc_type": pd.doc_type, "doc_type_method": pd.doc_type_method, "doc_date": pd.doc_date,
             "effective_date": pd.effective_date, "company": company or pd.company, "pages": pd.pages,
             "units": len(pd.units), "sha1": pd.sha1, "llm_fallback": pd.llm_fallback, "warnings": pd.warnings,
             "source": source, "company_id": company_id, "family_id": family_id, "status": status,
             **(extra_props or {})}
    nodes = [(doc_id, "InternalDoc", props, pd.doc_date, None)]
    edges = []
    seen: set[str] = set()
    for u in pd.units:
        cid = clause_id(doc_id, u)
        if cid in seen:
            cid = f"{cid}~{u.order}"
        seen.add(cid)
        nodes.append((cid, "Clause", {
            "doc": doc_id, "text": u.text, "section_no": u.section_no, "heading": u.heading,
            "unit_type": u.unit_type, "page": u.page, "order": u.order, "mapped": u.unit_type in MAPPED_TYPES,
            "extra": u.extra, "bbox": u.bbox, "company_id": company_id}, pd.doc_date, None))
        edges.append((doc_id, cid, "HAS_CLAUSE", {"order": u.order}, pd.doc_date, None))
    store.upsert_nodes(nodes)
    store.upsert_edges(edges)
    return {"doc_id": doc_id, **{k: props[k] for k in ("doc_type", "doc_date", "units")}}


def ingest_company_docs(cfg, store, run, llm=None, files: list[Path] | None = None) -> list[dict]:
    folder = cfg.path("company_docs")
    files = files or sorted(p for p in folder.glob("D*.pdf"))   # the answer key (00_*) is never ingested
    out = []
    for f in files:
        pd = parse_pdf(f, llm=llm)
        info = write_doc(store, pd)
        run.log("ingest_doc", file=f.name, **info, warnings=pd.warnings, llm_fallback=pd.llm_fallback)
        run.count("docs")
        out.append(info)
    return out
