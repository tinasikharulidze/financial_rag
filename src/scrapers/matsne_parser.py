"""
Shared Matsne parsing code. Import this from every script so that all of them
understand the same page layouts:

  * modern pages  : #maindoc contains elements whose id ends with "_Content"
  * legacy pages  : #maindoc is plain Word-style HTML (<p>, <div>, <br>, tables ...)

Output format of parse_document() is identical to documents.jsonl:
  metadata + parts[{id, structure, type, enclosure, text}] + text + part_count
"""
import re
from datetime import datetime

from bs4 import BeautifulSoup, NavigableString

BASE_URL = "https://matsne.gov.ge"
TRANSITIONAL = "ცვლილება შეიცავს გარდამავალ დებულებას"

BR = "\u0001"  # sentinel that replaces <br> so line breaks survive get_text()
BLOCK_TAGS = ["p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"]
COVERAGE_OK = 0.90  # a legacy extraction must capture >= 90% of #maindoc's text

ARTICLE_RE = re.compile(r"^მუხლი\s*\d+\S*\.")
CHAPTER_RE = re.compile(r"^(თავი|ნაწილი|კარი|წიგნი)\s+[IVXLCDM\d]+", re.I)


# ============================================================
# small helpers
# ============================================================
def clean_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


clean = clean_text


def iso(d):
    try:
        return datetime.strptime((d or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


# ============================================================
# DOCUMENT BODY
# ============================================================
_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def _lines(el):
    """Text of one element as a list of cleaned lines (<br> = line break)."""
    for sup in el.find_all("sup"):
        sup.replace_with(NavigableString(sup.get_text().translate(_SUP)))
    for br in el.find_all("br"):
        br.replace_with(NavigableString(BR))
    raw = el.get_text("", strip=False)   # inline tags join like a browser renders them
    return [t for t in (clean_text(x) for x in raw.split(BR)) if t]


def _leaf_blocks(root):
    return [el for el in root.find_all(BLOCK_TAGS) if not el.find(BLOCK_TAGS)]


ROLE_BY_CLASS = {"muxlixml": "article", "tavixml": "chapter", "tavisataurixml": "chapter_title"}

def _legacy_part(index, sub, tag, classes, text):
    text = re.sub(r"^მუხლი(?=\d)", "მუხლი ", text)
    ptype = classes[0] if classes else ("paragraph" if tag == "p" else tag)
    role = next((ROLE_BY_CLASS[c] for c in classes if c in ROLE_BY_CLASS), None)
    if role == "article" and not ARTICLE_RE.match(text):
        role = "note"                       # e.g. "საქართველოს 2017 წლის ... კანონი №1895 -"
    if role is None:
        role = ("article" if ARTICLE_RE.match(text)
                else "chapter" if CHAPTER_RE.match(text) else "text")
    return {
        "id": f"LEGACY:{index:04d}" + (f".{sub}" if sub else ""),
        "structure": ["LEGACY", ptype],
        "type": ptype,
        "enclosure": None,
        "text": text,
        "role": role,
    }

def _parts_from_blocks(blocks):
    parts = []
    for i, el in enumerate(blocks):
        classes = el.get("class") or []
        for n, line in enumerate(_lines(el)):
            parts.append(_legacy_part(i, n, el.name, classes, line))
    return parts


def _parts_from_text_lines(root):
    for br in root.find_all("br"):
        br.replace_with(NavigableString(BR))
    lines = []
    for chunk in root.get_text("\n").split("\n"):
        for x in chunk.split(BR):
            x = clean_text(x)
            if x:
                lines.append(x)
    return [_legacy_part(i, 0, "line", [], t) for i, t in enumerate(lines)]


def _chars(parts):
    return sum(len(p["text"]) for p in parts)


def _legacy_parts(maindoc, total_chars):
    """Try progressively more forgiving strategies; keep the one that captures the most text."""
    best, best_cov = [], -1.0
    strategies = (
        lambda: _parts_from_blocks(maindoc.select("p")),     # original behaviour
        lambda: _parts_from_blocks(_leaf_blocks(maindoc)),   # p/div/li/td/h*
        lambda: _parts_from_text_lines(maindoc),             # last resort: everything
    )
    for build in strategies:
        parts = build()
        cov = _chars(parts) / total_chars if total_chars else 1.0
        if cov > best_cov + 1e-9:
            best, best_cov = parts, cov
        if cov >= COVERAGE_OK:
            break
    return best


def parse_document(html, metadata):
    soup = BeautifulSoup(html, "html.parser")
    maindoc = soup.select_one("#maindoc")
    if not maindoc:
        raise ValueError("Could not find #maindoc")

    # strip things that are never legal text (only if they do not hold the content)
    for junk in maindoc.select("script, style"):
        junk.decompose()
    for junk in maindoc.select(".document-changes-bottom"):
        if not junk.select('[id$="_Content"]'):
            junk.decompose()

    parts = []

    # ---- modern structure (unchanged logic) ----
    for part in maindoc.select('[id$="_Content"]'):
        part_id = part.get("id")
        if not part_id:
            continue
        text = clean_text(part.get_text(" ", strip=True))
        components = [c for c in part_id[: -len("_Content")].split(";") if c]

        part_type = None
        for component in components:
            if ":" in component:
                key, _ = component.split(":", 1)
                if key != "DOCUMENT":
                    part_type = key
                    break
        enclosure = None
        for component in components:
            if component.startswith("ENCLOSURE:"):
                enclosure = component.split(":", 1)[1]

        parts.append({
            "id": part_id, "structure": components, "type": part_type or "UNKNOWN",
            "enclosure": enclosure, "text": text,
        })

    # ---- legacy / Word-style structure ----
    if not any(p["text"] for p in parts):
        total = len(clean_text(maindoc.get_text(" ", strip=True)))
        parts = _legacy_parts(maindoc, total)

    result = dict(metadata)
    result["parts"] = parts
    result["text"] = "\n\n".join(p["text"] for p in parts if p["text"])
    result["part_count"] = len(parts)
    return result


# ============================================================
# METADATA OF A DOCUMENT PAGE
# ============================================================
def _info_table(soup):
    d = {}
    ti = soup.select_one("table.table-info")
    if ti:
        for tr in ti.find_all("tr"):
            c = tr.find_all(["th", "td"])
            if len(c) == 2:
                d[clean_text(c[0].get_text(" "))] = clean_text(c[1].get_text(" "))
    return d


def _title(soup):
    h1 = soup.select_one("h1.page-header") or soup.find("h1")
    if h1 and clean_text(h1.get_text(" ")):
        return clean_text(h1.get_text(" "))
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return clean_text(og["content"])
    t = soup.find("title")
    return clean_text(t.get_text()).split(" | ")[0] if t else None


def parse_base_metadata(html, doc_id):
    """Metadata in the same shape the search-page scraper writes to documents.jsonl."""
    soup = BeautifulSoup(html, "html.parser")
    d = _info_table(soup)
    return {
        "document_id": str(doc_id),
        "title": _title(soup),
        "url": f"{BASE_URL}/ka/document/view/{doc_id}",
        "language": "ka",
        "document_type": d.get("დოკუმენტის ტიპი"),
        "issuing_authority": d.get("დოკუმენტის მიმღები"),
        "document_number": d.get("დოკუმენტის ნომერი"),
        "signing_date": d.get("მიღების თარიღი"),
    }


def parse_doc_meta(html, doc_id):
    """Metadata for the graph (amendment_docs.jsonl)."""
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one("#maindoc"):
        raise ValueError("no #maindoc (not a document page / blocked?)")
    d = _info_table(soup)
    source = d.get("გამოქვეყნების წყარო, თარიღი", "")
    return {
        "doc_id": str(doc_id), "url": f"{BASE_URL}/ka/document/view/{doc_id}",
        "title": _title(soup),
        "number": d.get("დოკუმენტის ნომერი"), "type": d.get("დოკუმენტის ტიპი"),
        "authority": d.get("დოკუმენტის მიმღები"), "adopted": iso(d.get("მიღების თარიღი", "")),
        "published": iso(source.split(",")[-1]) if "," in source else None,
        "effective_from": iso(d.get("ძალაში შესვლის თარიღი", "")),
        "registration_code": d.get("სარეგისტრაციო კოდი"),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


# ============================================================
# AMENDMENT LIST ON A BASE PAGE
# ============================================================
VIEW_RE = re.compile(r"/document/view/(\d+)")
ITEM_RE = re.compile(r"^\d+\.\s*\d{2}/\d{2}/\d{4}")


def parse_amendments(html, base_id):
    """-> edge dicts in Matsne order (seq 1 = oldest). Works with or without the CSS block names."""
    soup = BeautifulSoup(html, "html.parser")
    items = (soup.select(".document-changes-bottom a.list-group-item")
             or soup.select(".document-changes a.list-group-item"))
    if not items:  # structure-independent fallback: "<n>. dd/mm/yyyy - ..." links to other documents
        items = [a for a in soup.find_all("a", href=VIEW_RE)
                 if ITEM_RE.match(clean_text(a.get_text(" ")))]

    edges, seen = [], set()
    for a in items:
        m = VIEW_RE.search(a.get("href", ""))
        if not m or m.group(1) == str(base_id) or m.group(1) in seen:
            continue
        amend_id = m.group(1)
        seen.add(amend_id)
        text = clean_text(a.get_text(" "))
        edge = {
            "base_doc_id": str(base_id), "amendment_doc_id": amend_id,
            "url": f"{BASE_URL}/ka/document/view/{amend_id}",
            "title": clean_text(a.get("title")), "seq": None, "amendment_date": None,
            "amendment_type": None, "amendment_number": None, "published_source": None,
            "published_date": None, "has_transitional": TRANSITIONAL in text, "raw_text": text,
        }
        mm = re.match(r"^(\d+)\.\s*(\d{2}/\d{2}/\d{4})\s*-\s*(.*)$", text)
        if mm:
            edge["seq"] = int(mm.group(1))
            edge["amendment_date"] = iso(mm.group(2))
            rest = mm.group(3).replace("- " + TRANSITIONAL, "").replace(TRANSITIONAL, "").strip(" -")
            parts = rest.rsplit(" - ", 2)
            if len(parts) == 3:
                edge["amendment_type"], edge["amendment_number"], pub = (clean_text(p) for p in parts)
                pm = re.match(r"^(.*?),\s*(\d{2}/\d{2}/\d{4})$", pub)
                if pm:
                    edge["published_source"], edge["published_date"] = clean_text(pm.group(1)), iso(pm.group(2))
                else:
                    edge["published_source"] = pub
        edges.append(edge)
    return edges