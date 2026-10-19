"""One PDF parser for the offline company corpus and for UC6 uploads.

Heuristics (pdfplumber), in order:
  1. drop running headers/footers (same text, digits normalised, on at least half of the pages);
  2. tables become ``table`` units (one per table, or one per row when a table has more than 15 rows);
     2-column tables on page 1 before the first heading are cover ``metadata``;
  3. numbered lines start units: ``1.2.3`` -> ``clause``; bold, larger numbered lines -> ``heading``;
     un-numbered text continues the current unit (or becomes a ``paragraph`` under a heading);
  4. signature blocks ("_____", "ხელმოწერა") become ``signature`` units;
  5. cover text before the first heading yields title, doc code, version, dates, company.
LLM fallback (one call per document): fewer than 5 units or a unit longer than 3,000 characters.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
from pydantic import BaseModel

from kg.company.classify import classify

RE_HEADING_NUM = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+(\S.*)$")
RE_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
RE_CODE = re.compile(r"\b([A-Z]{1,6}(?:-[A-Z0-9]{1,6}){1,3})\b")
RE_SIGN = re.compile(r"_{4,}|ხელმოწერა")
RE_BULLET = re.compile(r"^[•·▪‣\-–—]\s+")
MAX_UNIT_CHARS = 3000
MIN_UNITS = 5
TABLE_ROWS_PER_UNIT_LIMIT = 15


@dataclass
class DocUnit:
    order: int
    unit_type: str                 # clause | paragraph | heading | table | metadata | signature
    text: str
    section_no: str | None = None
    heading: str | None = None
    page: int = 1
    extra: dict = field(default_factory=dict)


@dataclass
class ParsedDoc:
    file: str
    title: str
    doc_code: str | None
    version: str
    doc_type: str
    doc_type_method: str
    doc_date: str | None
    effective_date: str | None
    company: str | None
    units: list[DocUnit]
    pages: int
    llm_fallback: bool = False
    warnings: list[str] = field(default_factory=list)
    sha1: str = ""

    @property
    def doc_id(self) -> str:
        code = self.doc_code or f"UPLOAD-{self.sha1[:8]}"
        return f"doc:{code}@{self.version}"


def iso_date(s: str) -> str | None:
    m = RE_DATE.search(s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _norm_key(t: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", t.strip()))


def _clean_cell(c) -> str:
    s = re.sub(r"\s+", " ", (c or "").replace("\n", " ")).strip()
    return re.sub(r"(?<=[A-Za-z])-\s+(?=\d)", "-", s)        # "M- 001" -> "M-001" (wrapped ids)


def _line_style(line) -> tuple[float, bool]:
    chars = [c for c in line["chars"] if c["text"].strip()]
    if not chars:
        return 0.0, False
    sizes = Counter(round(c["size"], 1) for c in chars)
    bold = sum(1 for c in chars if "bold" in c["fontname"].lower()) / len(chars)
    return sizes.most_common(1)[0][0], bold > 0.6


def _inside(bbox, y_mid: float, x_mid: float) -> bool:
    x0, top, x1, bottom = bbox
    return top - 1 <= y_mid <= bottom + 1 and x0 - 1 <= x_mid <= x1 + 1


def _table_units(rows: list[list[str]]) -> list[tuple[str, dict]]:
    rows = [[_clean_cell(c) for c in r] for r in rows if any(_clean_cell(c) for c in r)]
    if not rows:
        return []
    header = rows[0] if len(rows) >= 2 and all(rows[0]) else None
    body = rows[1:] if header else rows
    rendered = []
    for r in body:
        if header and len(r) == len(header):
            rendered.append(" | ".join(f"{h}: {v}" for h, v in zip(header, r) if v))
        else:
            rendered.append(" | ".join(c for c in r if c))
    wide = bool(header) and len(header) >= 6          # record tables (one row = one record, e.g. a model inventory)
    if len(rendered) > TABLE_ROWS_PER_UNIT_LIMIT or sum(len(r) for r in rendered) > MAX_UNIT_CHARS or wide:
        return [(t, {"row": i + 1, "header": header}) for i, t in enumerate(rendered)]
    head_txt = (" | ".join(header) + "\n") if header else ""
    return [(head_txt + "\n".join(rendered), {"header": header, "rows": len(rendered)})]


def _is_section_no(num: str) -> bool:
    parts = num.split(".")
    return all(p.isdigit() and 0 < int(p) <= 99 for p in parts) and not (len(parts) == 3 and len(parts[2]) == 4)


def parse_pdf(path: str | Path, llm=None, classify_llm: bool = True) -> ParsedDoc:
    path = Path(path)
    data = path.read_bytes()
    sha1 = hashlib.sha1(data).hexdigest()
    warnings: list[str] = []
    page_items: list[list[dict]] = []
    header_texts: list[str] = []

    with pdfplumber.open(str(path)) as pdf:
        n_pages = len(pdf.pages)
        raw_pages = []
        for pi, pg in enumerate(pdf.pages, 1):
            tables = pg.find_tables()
            bboxes = [t.bbox for t in tables]
            lines = []
            for ln in pg.extract_text_lines(return_chars=True):
                ymid, xmid = (ln["top"] + ln["bottom"]) / 2, (ln["x0"] + ln["x1"]) / 2
                if any(_inside(b, ymid, xmid) for b in bboxes):
                    continue
                size, bold = _line_style(ln)
                lines.append({"kind": "line", "top": ln["top"], "bottom": ln["bottom"], "text": ln["text"].strip(),
                              "size": size, "bold": bold, "page": pi, "height": pg.height})
            for t in tables:
                lines.append({"kind": "table", "top": t.bbox[1], "bottom": t.bbox[3], "rows": t.extract(), "page": pi,
                              "height": pg.height})
            lines.sort(key=lambda x: x["top"])
            raw_pages.append(lines)

    # 1. running headers / footers
    edge_counts: Counter = Counter()
    for lines in raw_pages:
        for ln in lines:
            if ln["kind"] == "line" and (ln["top"] < 40 or ln["bottom"] > ln["height"] - 40):
                edge_counts[_norm_key(ln["text"])] += 1
    threshold = max(2, (n_pages + 1) // 2) if n_pages > 1 else 99
    repeating = {k for k, c in edge_counts.items() if c >= threshold}
    for lines in raw_pages:
        kept = []
        for ln in lines:
            if ln["kind"] == "line" and (ln["top"] < 40 or ln["bottom"] > ln["height"] - 40):
                if _norm_key(ln["text"]) in repeating:
                    if ln["page"] == 1 and ln["top"] < 40:
                        header_texts.append(ln["text"])
                    continue
                if ln["bottom"] > ln["height"] - 40 and re.fullmatch(r"[\s\d/გვ.]+", ln["text"]):
                    continue
            kept.append(ln)
        page_items.append(kept)

    # body font size
    size_counter: Counter = Counter()
    for lines in page_items:
        for ln in lines:
            if ln["kind"] == "line":
                size_counter[ln["size"]] += len(ln["text"])
    body = size_counter.most_common(1)[0][0] if size_counter else 10.5

    company_hint = header_texts[0].split(" — ")[0].strip() if header_texts else None

    # 2./3. units
    last_header: list[str] | None = None            # header row of the previous table (for tables split over pages)
    prev_table: dict | None = None
    units: list[DocUnit] = []
    cover_lines: list[str] = []
    title_lines: list[str] = []
    cover = True
    cur: DocUnit | None = None
    cur_heading: DocUnit | None = None
    stats_tables = 0

    def flush():
        nonlocal cur
        if cur is not None and cur.text.strip():
            units.append(cur)
        cur = None

    for lines in page_items:
        for ln in lines:
            pg = ln["page"]
            if ln["kind"] == "table":
                rows = ln["rows"] or []
                rows_clean = [[_clean_cell(c) for c in r] for r in rows]
                is_meta = (cover and pg == 1 and rows_clean and all(len(r) == 2 for r in rows_clean))
                if is_meta:
                    cover_lines += [f"{r[0]} {r[1]}" for r in rows_clean]
                    continue
                flush()
                if any(RE_SIGN.search(" ".join(r)) for r in rows_clean):
                    units.append(DocUnit(len(units), "signature", "\n".join(" ".join(c for c in r if c) for r in rows_clean),
                                         page=pg))
                    continue
                continuation = bool(prev_table and prev_table["page"] == pg - 1 and ln["top"] < 150)
                if len(rows_clean) == 1 and len(rows_clean[0]) > 2:   # header row left alone at the bottom of a page
                    last_header = rows_clean[0]
                    prev_table = ln
                    continue
                if continuation and last_header and len(last_header) == len(rows_clean[0]) \
                        and rows_clean[0] != last_header:
                    rows = [last_header] + list(rows)
                elif len(rows_clean) >= 2 and all(rows_clean[0]):
                    last_header = rows_clean[0]
                prev_table = ln
                for txt, extra in _table_units(rows):
                    units.append(DocUnit(len(units), "table", txt, None,
                                         cur_heading.text if cur_heading else None, pg, extra))
                stats_tables += 1
                continue
            text = ln["text"]
            if not text:
                continue
            is_big_bold = ln["bold"] and ln["size"] >= body + 0.4
            m = RE_HEADING_NUM.match(text)
            if m and not _is_section_no(m.group(1)):
                m = None
            if cover and not (m and is_big_bold):
                if company_hint and text.strip() == company_hint:
                    continue
                if is_big_bold or ln["size"] >= body + 3:
                    title_lines.append(text)
                else:
                    cover_lines.append(text)
                continue
            cover = False
            if RE_SIGN.search(text):
                if cur is None or cur.unit_type != "signature":
                    flush()
                    cur = DocUnit(len(units), "signature", text, None, None, pg)
                else:
                    cur.text += "\n" + text
                continue
            if m and is_big_bold:
                flush()
                cur_heading = DocUnit(len(units), "heading", text, m.group(1), None, pg)
                units.append(cur_heading)
                continue
            if m and not ln["bold"] and "." in m.group(1) or (m and not ln["bold"] and m.group(1).isdigit() and text[len(m.group(1))] == "."):
                flush()
                cur = DocUnit(len(units), "clause", m.group(2), m.group(1),
                              cur_heading.text if cur_heading else None, pg)
                continue
            if cur is None or cur.unit_type in ("heading", "signature"):
                flush()
                cur = DocUnit(len(units), "paragraph", text, cur_heading.section_no if cur_heading else None,
                              cur_heading.text if cur_heading else None, pg)
                continue
            if RE_BULLET.match(text):
                cur.text += "\n" + text
            else:
                cur.text += " " + text
    flush()
    for i, u in enumerate(units):
        u.order = i

    # 4. cover metadata
    title = " ".join(title_lines).strip()
    cover_text = "\n".join(cover_lines)
    head_blob = " ".join(header_texts)
    code = None
    for src in (head_blob, cover_text):
        m = RE_CODE.search(src)
        if m:
            code = m.group(1)
            break
    vm = re.search(r"\bv\s?(\d+(?:\.\d+)*)", head_blob) or re.search(r"ვერსია\s+(\d+(?:\.\d+)*)", cover_text)
    version = vm.group(1) if vm else "1.0"
    doc_date = eff_date = None
    for label_re in (r"(?m)^თარიღი\s+(\d{2}\.\d{2}\.\d{4})", r"მომზადების\s+თარიღი\s+(\d{2}\.\d{2}\.\d{4})",
                     r"მდგომარეობა\s+(\d{2}\.\d{2}\.\d{4})", r"დამტკიცებულია[^\n]*(?:\n[^\n]*?)?(\d{2}\.\d{2}\.\d{4})"):
        m = re.search(label_re, cover_text)
        if m:
            doc_date = iso_date(m.group(1))
            break
    if doc_date is None:
        doc_date = iso_date(cover_text) or iso_date(head_blob)
    m = re.search(r"ძალაში\s+შესვლის\s+თარიღი\s+(\d{2}\.\d{2}\.\d{4})", cover_text)
    if m:
        eff_date = iso_date(m.group(1))
    company = None
    if header_texts:
        company = header_texts[0].split(" — ")[0].strip() or None
    if cover_text.strip():
        units.insert(0, DocUnit(0, "metadata", cover_text, None, None, 1))
        for i, u in enumerate(units):
            u.order = i

    # LLM fallback (structure)
    used_llm = False
    needs = (len([u for u in units if u.unit_type in ("clause", "paragraph", "table")]) < MIN_UNITS
             or any(len(u.text) > MAX_UNIT_CHARS for u in units))
    if needs:
        warnings.append("heuristics_weak")
        if llm is not None:
            new_units = structure_with_llm(llm, page_items)
            if new_units:
                units, used_llm = new_units, True
    dt, method = classify(title, " ".join(u.text for u in units[:6]), llm if classify_llm else None)
    if doc_date is None:
        warnings.append("doc_date_missing")
    return ParsedDoc(str(path.name), title, code, version, dt, method, doc_date, eff_date, company, units, n_pages,
                     used_llm, warnings, sha1)


# ---------------------------------------------------------------------------- LLM structure fallback
class LLMUnit(BaseModel):
    start_line: int
    end_line: int
    type: str = "clause"
    section_no: str | None = None
    heading: str | None = None


class LLMUnits(BaseModel):
    units: list[LLMUnit]


def structure_with_llm(llm, page_items) -> list[DocUnit]:
    from kg.llm import LLMValidationError, system_user

    lines = []
    for pg in page_items:
        for ln in pg:
            if ln["kind"] == "line":
                lines.append((ln["page"], ln["text"]))
            else:
                for r in ln["rows"] or []:
                    lines.append((ln["page"], " | ".join(_clean_cell(c) for c in r)))
    numbered = "\n".join(f"{i}: {t}" for i, (_, t) in enumerate(lines))
    system = ("You split the extracted text of an internal bank document into logical units. Lines are numbered. "
              "Return JSON {\"units\": [{\"start_line\": int, \"end_line\": int, \"type\": "
              "\"clause|heading|table|metadata|signature|paragraph\", \"section_no\": \"1.2.3\" or null, "
              "\"heading\": parent heading text or null}]}. Units must cover the lines in order without overlap; "
              "cover/title lines are one `metadata` unit; each numbered clause is its own unit.")
    try:
        res = llm.call("pdf_structure", system_user(system, numbered[:60000]), LLMUnits)
    except LLMValidationError:
        return []
    out = []
    for i, u in enumerate(res.parsed.units):
        seg = lines[u.start_line:u.end_line + 1]
        if not seg:
            continue
        out.append(DocUnit(i, u.type if u.type in ("clause", "heading", "table", "metadata", "signature", "paragraph")
                           else "paragraph", " ".join(t for _, t in seg), u.section_no, u.heading, seg[0][0]))
    return out
