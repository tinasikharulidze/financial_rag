"""UC5 / UC6: check an internal document against the obligations."""
from __future__ import annotations

from pathlib import Path

from kg.ids import display_path
from kg.mapping.coverage import COMPANY, map_coverage
from kg.mapping.labels import sort_rank


def _enrich(store, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        ob = store.get_node(r["obligation_id"])
        cl = store.get_node(r["clause_id"]) if r.get("clause_id") else None
        out.append({**r,
                    "obligation": (ob or {}).get("props", {}).get("statement"),
                    "source": display_path(ob["props"]["source_path"]) if ob else None,
                    "clause_text": (cl or {}).get("props", {}).get("text"),
                    "section_no": (cl or {}).get("props", {}).get("section_no")})
    return sorted(out, key=lambda r: (-sort_rank(r["priority"]), r["id"]))


def list_documents(store) -> list[dict]:
    return [{"id": n["id"], **{k: n["props"].get(k) for k in ("title", "doc_type", "doc_date", "version", "source")}}
            for n in sorted(store.nodes("InternalDoc"), key=lambda n: n["id"])]


def check_existing(store, doc_id: str, include_company: bool = True) -> list[dict]:
    rows = store.findings(doc_id)
    return _enrich(store, rows)


def company_findings(store) -> list[dict]:
    return _enrich(store, store.findings(COMPANY))


def check_upload(cfg, store, llm, run, pdf_path: str | Path, progress=None) -> dict:
    """Parse -> classify -> map this document only (both version bases)."""
    from kg.company.ingest import write_doc
    from kg.company.pdf_parser import parse_pdf

    pdf_path = Path(pdf_path)
    if progress:
        progress("parse", 0, 1)
    pd = parse_pdf(pdf_path, llm=llm)
    info = write_doc(store, pd, source="upload")
    run.log("upload_parsed", file=pdf_path.name, **info, warnings=pd.warnings)
    if progress:
        progress("parse", 1, 1)
    summary = map_coverage(cfg, store, run, llm, docs=[pd.doc_id], progress=progress)
    return {"doc_id": pd.doc_id, "doc_type": pd.doc_type, "summary": summary,
            "findings": check_existing(store, pd.doc_id)}
