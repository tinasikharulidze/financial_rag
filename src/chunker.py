#!/usr/bin/env python3
"""
Matsne chunker v2 (parent/child).

Reads   matsne_data/documents_raw/*.html   (+ documents.jsonl for url/title fallback)
Writes  <out>/docs.jsonl       one record per document (clean metadata, full preamble, footer)
        <out>/parents.jsonl    one record per legal unit (article / point / preamble / table part) = what the LLM reads + cites
        <out>/children.jsonl   retrieval chunks with parent_id = what gets embedded
        <out>/report.txt       QA report (also printed)
        <out>/sample.txt       a few documents rendered chunk by chunk

Changes vs v1 (all discussed and agreed):
  * Heading + body in ONE block ("მუხლი 1. <paragraph>") no longer swallowed as a title (was silently
    dropping whole articles). Safety net: a unit is never dropped if text sits in its title.
  * Units up to SPLIT_ABOVE chars stay ONE child. Bigger units split at numbered paragraphs / sub-items,
    never leave a heading stranded at the end of a child, never cut mid-word.
  * Continuation children of a split paragraph carry the paragraph's lead-in line in retrieval_text.
  * Huge tables are no longer skipped: rows are grouped into table-part parents (~TABLE_PARENT_TARGET chars).
  * Parents store structured + untruncated metadata (breadcrumb_full, full preamble, annex_title, chapter);
    children store filter fields (doc_type, adopted, effective_from, expires). Doc-level footer + annex footers
    are in docs.jsonl. Preamble snippet stays in retrieval_text of the first child only.
  * Multiple PREAMBLE parts in one annex are joined (v1 overwrote).
  * A "real" multi-column <table> part is rendered as a table (v1 read only its first cell).

Usage:  python chunker.py [--raw matsne_data/documents_raw] [--out matsne_data/chunks] [--limit N]
"""
import argparse
import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString

# ============================================================
# CONFIG (tune against the eval set)
# ============================================================
SPLIT_ABOVE = 3000          # a unit up to this many chars stays ONE child
TARGET = 1800               # when a unit must be split: preferred child size
MAX_CHILD = 3000            # ceiling for a child (a few chars over only if a heading is carried along)
MIN_CHILD = 400             # a tiny trailing child is merged back into the previous one
TABLE_PARENT_MAX = 6000     # a table bigger than this becomes its own table-part parent(s)
TABLE_PARENT_TARGET = 5000  # rows are grouped into table parents of about this size
LEAD_IN_CAP = 250           # max chars of the paragraph lead-in repeated on continuation children
PREAMBLE_CAP = 300
PREAMBLE_ON = "first"       # "first" | "all" | "none": which children of a unit get the preamble snippet
TITLE_MAX = 200             # max length of a block treated as an article/chapter title or heading

BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "center", "blockquote", "h1", "h2", "h3",
              "h4", "h5", "h6", "section", "article", "tr", "td", "th", "tbody",
              "thead", "dl", "dt", "dd"}

RE_ART = re.compile(r"^მუხლი\s+(\d+)(?:\^(\d+))?(?=\s|\.|:|$)\s*[.:]?\s*(.*)$", re.S)
RE_CH = re.compile(r"^თავი\s+([IVXLCDM]+|\d+)(?:\^(\d+))?(?=\s|\.|:|[ა-ჰ]|$)\s*[.:]?\s*(.*)$", re.S)
RE_PARA = re.compile(r"^(\d+)(?:\^(\d+))?\s*[.)](?=\s|$)")
RE_NUM = re.compile(r"^\d+(?:\.\d+)+\.?\s+\S")          # 1.1.1. Heading text
RE_SUB = re.compile(r"^([ა-ჰ])(?:\^(\d+))?\s*\)")
RE_SUBSUB = re.compile(r"^([ა-ჰ])\.([ა-ჰ])\s*\)")


# ============================================================
# TEXT EXTRACTION
# ============================================================
def tidy(s):
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:)\]])", r"\1", s)      # no space before closing punctuation
    s = re.sub(r"([(\[„«])\s+", r"\1", s)          # no space after opening bracket / „ «
    s = re.sub(r"\s+\^", "^", s)
    return s


def parse_label(text):
    m = RE_ART.match(text)
    if m:
        return {"kind": "article", "num": m.group(1) + ("^" + m.group(2) if m.group(2) else ""),
                "rest": tidy(m.group(3))}
    m = RE_CH.match(text)
    if m:
        return {"kind": "chapter", "num": m.group(1) + ("^" + m.group(2) if m.group(2) else ""),
                "rest": tidy(m.group(3))}
    if RE_PARA.match(text):
        return {"kind": "para"}
    if RE_NUM.match(text):
        return {"kind": "num"}
    if RE_SUBSUB.match(text) or RE_SUB.match(text):
        return {"kind": "sub"}
    return None


def cell_text(td):
    out, buf = [], []
    walk(td, out, buf)
    flush(buf, out)
    return tidy(" ".join(b["text"] for b in out))


def table_block(tbl):
    rows = []
    for tr in tbl.find_all("tr"):
        if tr.find_parent("table") is not tbl:
            continue
        cells = [cell_text(c) for c in tr.find_all(["td", "th"], recursive=False)]
        if any(cells):
            rows.append(cells)
    if not rows:
        return None
    header = rows[0] if len(rows) >= 2 and all(rows[0]) else None
    rendered = []
    for r in (rows[1:] if header else rows):
        if header and len(r) == len(header):
            rendered.append(" | ".join(f"{h}: {v}" for h, v in zip(header, r) if v))
        else:
            rendered.append(" | ".join(c for c in r if c))
    rendered = [r for r in rendered if r]
    if not rendered:
        return None
    return {"type": "table", "text": "\n".join(rendered), "rows": rendered, "label": None}


def flush(buf, out):
    t = tidy("".join(buf))
    buf.clear()
    if t:
        out.append({"type": "text", "text": t, "label": parse_label(t)})


def walk(node, out, buf):
    for ch in node.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            buf.append(str(ch))
            continue
        name = ch.name
        if name in ("script", "style"):
            continue
        if name == "a" and "hide" in (ch.get("class") or []):
            continue
        if name == "br":
            flush(buf, out)
            continue
        if name == "sup":
            buf.append("^" + ch.get_text(""))
            continue
        if name == "table":
            flush(buf, out)
            tb = table_block(ch)
            if tb:
                out.append(tb)
            continue
        if name in BLOCK_TAGS:
            flush(buf, out)
            walk(ch, out, buf)
            flush(buf, out)
            continue
        walk(ch, out, buf)       # inline element: no break


def part_blocks(el):
    root = el
    if el.name == "table":
        own_rows = [tr for tr in el.find_all("tr") if tr.find_parent("table") is el]
        max_cells = max((len(tr.find_all(["td", "th"], recursive=False)) for tr in own_rows), default=0)
        if max_cells > 1:                       # a real multi-column table, not a one-cell layout wrapper
            tb = table_block(el)
            return [tb] if tb else []
        # layout wrapper (one cell per row): walk everything inside it
    out, buf = [], []
    walk(root, out, buf)
    flush(buf, out)
    return out


def plain_text(el):
    return tidy(" ".join(b["text"] for b in part_blocks(el)))


# ============================================================
# METADATA
# ============================================================
def iso(d):
    try:
        return datetime.strptime(d.strip(), "%d/%m/%Y").date().isoformat()
    except Exception:
        return None


def doc_meta(soup, doc_id, jrow):
    d = {}
    ti = soup.select_one("table.table-info")
    if ti:
        for tr in ti.find_all("tr"):
            c = tr.find_all(["th", "td"])
            if len(c) == 2:
                d[tidy(c[0].get_text(" "))] = tidy(c[1].get_text(" "))
    pubs = sorted({iso(x) for x in d.get("კონსოლიდირებული პუბლიკაციები", "").split() if iso(x)},
                  reverse=True)
    source = d.get("გამოქვეყნების წყარო, თარიღი", "")
    h1 = soup.select_one("h1.page-header")
    return {
        "doc_id": doc_id,
        "url": (jrow or {}).get("url") or f"https://matsne.gov.ge/ka/document/view/{doc_id}",
        "number": d.get("დოკუმენტის ნომერი") or ((jrow or {}).get("document_number") or "").lstrip("N"),
        "type": d.get("დოკუმენტის ტიპი") or (jrow or {}).get("document_type"),
        "authority": d.get("დოკუმენტის მიმღები") or (jrow or {}).get("issuing_authority"),
        "adopted": iso(d.get("მიღების თარიღი", "")),
        "published": iso(source.split(",")[-1]) if "," in source else None,
        "effective_from": iso(d.get("ძალაში შესვლის თარიღი", "")),
        "expires": iso(d.get("ძალის დაკარგვის თარიღი", "")),
        "registration_code": d.get("სარეგისტრაციო კოდი"),
        "consolidated_versions": pubs,
        "last_amended": pubs[0] if len(pubs) > 1 else None,
        "title_fallback": tidy(h1.get_text(" ")) if h1 else (jrow or {}).get("title"),
    }


# ============================================================
# STRUCTURE
# ============================================================
def split_id(i):
    p = i.split(";")
    if p and p[-1].startswith("_"):
        return ";".join(p[:-1]), p[-1]
    return i, ""


def key_list(prefix):
    out = []
    for c in prefix.split(";"):
        if not c or c.startswith("DOCUMENT") or ":" not in c:
            continue
        k, v = c.split(":", 1)
        out.append((k, v))
    return out


def prefix_until(prefix, key):
    res = []
    for c in prefix.split(";"):
        if not c:
            continue
        res.append(c)
        if c.startswith(key + ":"):
            break
    return ";".join(res)


def is_title_like(b):
    t = b["text"]
    return (b["type"] == "text" and b["label"] is None and len(t) <= TITLE_MAX
            and not re.search(r"[.;:,]$", t))


def split_heading(rest, blocks, i, stats):
    """`rest` = text after 'მუხლი N.' / 'თავი X.' in the SAME block.
    returns (title, body_block_or_None, new_index).
    A heading and its body often share ONE block; only short, unpunctuated text is a title."""
    if rest:
        if len(rest) <= TITLE_MAX and not re.search(r"[.;:,]$", rest):
            return rest, None, i                                    # genuine short title
        stats["heading_with_inline_body"] += 1                      # body glued to the heading
        return None, {"type": "text", "text": rest, "label": parse_label(rest)}, i
    if i + 1 < len(blocks) and is_title_like(blocks[i + 1]):        # title is the next block
        stats["heading_title_from_next_block"] += 1
        return blocks[i + 1]["text"], None, i + 1
    return None, None, i


def segment(blocks, base, new_unit, stats):
    """Split a part's blocks into legal units at inline 'მუხლი N' / 'თავი X' headings."""
    cur, chapter, i, n_art_heads = base, base["chapter"], 0, 0
    created = []
    while i < len(blocks):
        b = blocks[i]
        lab = b.get("label")
        if lab and lab["kind"] == "chapter":
            title, body, i = split_heading(lab["rest"], blocks, i, stats)
            chapter = f"თავი {lab['num']}" + (f". {title}" if title else "")
            stats["inline_chapter_headings"] += 1
            if not cur["blocks"]:
                cur["chapter"] = chapter
            if body:
                cur["blocks"].append(body)
        elif lab and lab["kind"] == "article":
            n_art_heads += 1
            title, body, i = split_heading(lab["rest"], blocks, i, stats)
            if not cur["blocks"]:
                cur.update(art_no=lab["num"], art_title=title or cur["art_title"], chapter=chapter)
            else:
                cur = new_unit(base["prefix"], base["annex"], chapter, lab["num"], title)
                created.append(cur)
            cur["flags"].add("inline_heading")
            if body:
                cur["blocks"].append(body)
        else:
            cur["blocks"].append(b)
        i += 1
    if n_art_heads >= 2:
        stats["blob_parts_split"] += 1
        for u in [base] + created:
            u["flags"].add("blob_split")


def heading_line(u):
    if u["art_no"]:
        return f"მუხლი {u['art_no']}" + (f". {u['art_title']}" if u["art_title"] else "")
    return u["art_title"]


def cap(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + "…"


def breadcrumb(meta, annex, annex_title, u, capped=True):
    c = cap if capped else (lambda s, n: s or "")
    bits = [f"{meta['type']} №{meta['number']}" + (f" ({meta['adopted']})" if meta["adopted"] else "")]
    if meta["title"]:
        bits.append(c(meta["title"], 160))
    if annex:
        bits.append(f"დანართი {annex}" + (f": {c(annex_title, 140)}" if annex_title else ""))
    if u.get("chapter"):
        bits.append(c(u["chapter"], 120))
    h = heading_line(u) if u.get("art_no") or u.get("art_title") else None
    if h:
        bits.append(c(h, 160))
    if u.get("table_part"):
        bits.append(u["table_part"])
    return " › ".join(bits)


# ============================================================
# CHILDREN
# ============================================================
def split_long(text, limit):
    """Sentence-aware split; a sentence longer than `limit` is cut at a space, never mid-word."""
    if len(text) <= limit:
        return [text]
    sents = re.split(r"(?<=[.;:])\s+", text)
    merged = []
    for s in sents:                                  # keep list numbers ("1.") attached to their text
        if merged and re.fullmatch(r"\d+(?:\.\d+)*[.)]?", merged[-1]):
            merged[-1] = f"{merged[-1]} {s}"
        else:
            merged.append(s)
    pieces, cur = [], ""
    for sent in merged:
        while len(sent) > limit:
            cut = sent.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sent[:cut].rstrip())
            sent = sent[cut:].lstrip()
        if cur and len(cur) + len(sent) + 1 > limit:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        pieces.append(cur)
    return pieces


def para_start(b):
    lab = b.get("label")
    return bool(lab and lab["kind"] in ("para", "article", "num"))


def keep_with_next(b):
    """short unpunctuated block = a heading; it must never end a child."""
    if b["type"] != "text" or len(b["text"]) > TITLE_MAX or re.search(r"[.;:,]$", b["text"]):
        return False
    lab = b.get("label")
    return lab is None or lab["kind"] == "num"


def group_rows(rows, target):
    groups, grp, size = [], [], 0
    for r in rows:
        if grp and size + len(r) > target:
            groups.append(grp)
            grp, size = [], 0
        grp.append(r)
        size += len(r) + 1
    if grp:
        groups.append(grp)
    return groups


def table_children(rows):
    return [{"kind": "table", "text": "\n".join(g), "lead_in": None}
            for g in group_rows([p for r in rows for p in split_long(r, MAX_CHILD)], TARGET)]


def make_children(blocks):
    """-> list of {"kind", "text", "lead_in"}"""
    total = sum(len(b["text"]) for b in blocks)
    if total <= SPLIT_ABOVE:
        kind = "table" if blocks and all(b["type"] == "table" for b in blocks) else "text"
        return [{"kind": kind, "text": "\n".join(b["text"] for b in blocks), "lead_in": None}]

    out = []
    st = {"cur": [], "size": 0}          # cur entries: (text, keep_with_next, starts_para, lead_at_that_point)
    last_para = None

    def flush():
        cur = st["cur"]
        if cur:
            first = cur[0]
            out.append({"kind": "text", "text": "\n".join(e[0] for e in cur),
                        "lead_in": None if first[2] else first[3]})
        st["cur"], st["size"] = [], 0

    def add(e):
        st["cur"].append(e)
        st["size"] += len(e[0]) + 1

    for b in blocks:
        if b["type"] == "table":
            flush()
            out.extend(table_children(b["rows"]))
            continue
        for k, t in enumerate(split_long(b["text"], MAX_CHILD)):
            first = k == 0
            starts = first and para_start(b)
            is_sub = first and bool(b.get("label")) and b["label"]["kind"] == "sub"
            kwn = first and keep_with_next(b)
            cur = st["cur"]
            if cur:
                over = st["size"] + len(t) + 1 > MAX_CHILD
                soft = st["size"] >= TARGET and (starts or is_sub)
                prev_kwn = cur[-1][1]
                if (over or soft) and not prev_kwn:
                    flush()
                elif over and prev_kwn:                         # carry trailing heading(s) to the next child
                    tail = []
                    while len(st["cur"]) > 1 and st["cur"][-1][1]:
                        tail.insert(0, st["cur"].pop())
                    flush()
                    for e in tail:
                        add(e)
            add((t, kwn, starts, last_para))
            if starts:
                last_para = cap(t, LEAD_IN_CAP)
    flush()
    # merge a tiny trailing text child into the previous one
    if len(out) >= 2 and out[-1]["kind"] == "text" and out[-2]["kind"] == "text" \
            and len(out[-1]["text"]) < MIN_CHILD \
            and len(out[-2]["text"]) + len(out[-1]["text"]) + 1 <= MAX_CHILD:
        out[-2]["text"] += "\n" + out[-1]["text"]
        out.pop()
    return out


# ============================================================
# PER-DOCUMENT PROCESSING
# ============================================================
def expand_tables(units, stats):
    """Tables above TABLE_PARENT_MAX chars become their own table-part parents (never skipped)."""
    out = []
    for u in units:
        normal, big = [], []
        for b in u["blocks"]:
            if b["type"] == "table" and sum(len(r) for r in b["rows"]) > TABLE_PARENT_MAX:
                big.append(b)
            else:
                normal.append(b)
        if normal or not big:
            u["blocks"] = normal
            out.append(u)
        for ti, b in enumerate(big, 1):
            groups = group_rows(b["rows"], TABLE_PARENT_TARGET)
            for k, g in enumerate(groups, 1):
                v = dict(u)
                v["flags"] = set(u["flags"]) | {"table_part"}
                v["table_part"] = (f"ცხრილი{'' if len(big) == 1 else ' ' + str(ti)}, ნაწილი {k}/{len(groups)}")
                v["blocks"] = [{"type": "table", "text": "\n".join(g), "rows": g, "label": None}]
                out.append(v)
                stats["table_parents"] += 1
    return out


def process_doc(doc_id, html, jrow, stats):
    soup = BeautifulSoup(html, "html.parser")
    md = soup.select_one("#maindoc")
    meta = doc_meta(soup, doc_id, jrow)
    meta["text_length"] = len(md.get_text(" ", strip=True)) if md else 0
    if not md:
        stats["docs_without_maindoc"] += 1
        return meta, [], []
        
    els = [e for e in md.find_all(id=True)]

    # ---- pass 1: titles, headers, preambles, footers
    titles, header_texts = {}, defaultdict(list)
    preamble_parts, annex_footers = defaultdict(list), {}
    doc_title, header_line, footer = None, None, None
    for e in els:
        prefix, suf = split_id(e["id"])
        if suf == "_Title":
            titles[prefix] = plain_text(e)
    for e in els:
        prefix, suf = split_id(e["id"])
        if suf != "_Content":
            continue
        keys = key_list(prefix)
        if not keys:
            continue
        leaf, annex = keys[-1][0], dict(keys).get("ENCLOSURE")
        if leaf == "HEADER":
            txt = plain_text(e)
            if annex is None:
                doc_title = txt or doc_title
                header_line = titles.get(prefix)
            else:
                for t in (titles.get(prefix), txt):
                    if t and t not in header_texts[annex]:
                        header_texts[annex].append(t)
        elif leaf == "PREAMBLE":
            txt = plain_text(e)
            if txt:
                preamble_parts[annex].append(txt)
        elif leaf == "FOOTER":
            txt = plain_text(e)
            if annex is None:
                footer = txt or footer
            elif txt:
                annex_footers[annex] = txt
                stats["annex_footers_nonempty"] += 1
    preambles = {a: "\n".join(v) for a, v in preamble_parts.items()}
    meta["title"] = doc_title or meta["title_fallback"]
    meta["header_line"] = header_line
    meta["footer"] = footer
    meta["annex_footers"] = annex_footers or None
    meta["preamble"] = preambles.get(None)
    annex_titles = {a: " – ".join(v) for a, v in header_texts.items()}

    # ---- pass 2: legal units
    units, by_article = [], {}

    def new_unit(prefix, annex, chapter, art_no, art_title):
        u = {"prefix": prefix, "annex": annex, "chapter": chapter, "art_no": art_no,
             "art_title": art_title, "blocks": [], "flags": set()}
        units.append(u)
        return u

    for e in els:
        prefix, suf = split_id(e["id"])
        if suf != "_Content":
            continue
        keys = key_list(prefix)
        if not keys or keys[-1][0] not in ("ARTICLE", "POINT"):
            continue
        kd = dict(keys)
        leaf, annex = keys[-1][0], kd.get("ENCLOSURE")
        chapter = None
        if "CHAPTER" in kd:
            ct = titles.get(prefix_until(prefix, "CHAPTER"))
            if ct:
                m = RE_CH.match(ct)
                chapter = (f"თავი {m.group(1)}" + (f". {m.group(3).strip()}" if m.group(3).strip() else "")
                           ) if m else ct
        blocks = part_blocks(e)
        if not blocks:
            stats["empty_parts_skipped"] += 1
            continue

        if leaf == "ARTICLE":
            tt = titles.get(prefix)
            art_no, art_title = None, None
            if tt:
                m = RE_ART.match(tt)
                if m:
                    art_no = m.group(1) + ("^" + m.group(2) if m.group(2) else "")
                    art_title = tidy(m.group(3)) or None
                else:
                    art_title = tt
            base = new_unit(prefix, annex, chapter, art_no, art_title)
            by_article[prefix] = base
        else:
            ap = prefix_until(prefix, "ARTICLE") if "ARTICLE" in kd else None
            base = by_article.get(ap) if ap else None
            if base is None:
                base = new_unit(prefix, annex, chapter, None, None)
                base["flags"].add("point")
                if ap:
                    by_article[ap] = base
            else:
                stats["points_attached_to_article"] += 1
        segment(blocks, base, new_unit, stats)

    # safety net: never lose text that ended up in a title
    for u in units:
        if not u["blocks"] and u["art_title"]:
            u["blocks"] = [{"type": "text", "text": u["art_title"], "label": None}]
            u["art_title"] = None
            u["flags"].add("rescued_from_title")
            stats["empty_unit_rescued_from_title"] += 1
    stats["empty_units_dropped"] += sum(1 for u in units if not u["blocks"])
    units = [u for u in units if u["blocks"]]
    units = expand_tables(units, stats)

    # ---- build parents and children
    parents, children = [], []

    def preamble_snippet(annex):
        p = preambles.get(annex)
        return cap(p, PREAMBLE_CAP) if p else ""

    def emit(unit_idx, u, kind="unit"):
        pid = f"{doc_id}:{unit_idx:04d}"
        annex = u["annex"]
        at = annex_titles.get(annex)
        bc = breadcrumb(meta, annex, at, u)
        bc_full = breadcrumb(meta, annex, at, u, capped=False)
        hl = heading_line(u) if (u.get("art_no") or u.get("art_title")) else None
        body = "\n".join(b["text"] for b in u["blocks"])
        flags = sorted(u["flags"])
        kids = make_children(u["blocks"])
        parents.append({
            "parent_id": pid, "doc_id": doc_id, "kind": kind, "annex": annex,
            "annex_title": at, "chapter": u.get("chapter"),
            "article_no": u.get("art_no"), "article_title": u.get("art_title"),
            "table_part": u.get("table_part"),
            "breadcrumb": bc, "breadcrumb_full": bc_full,
            "preamble": preambles.get(annex) if kind == "unit" else None,
            "text": (hl + "\n" if hl else "") + body,
            "char_len": len(body), "n_children": len(kids), "flags": flags,
        })
        use_snip = kind == "unit" and PREAMBLE_ON != "none" and not u.get("table_part")
        snip = preamble_snippet(annex) if use_snip else ""
        for j, kid in enumerate(kids):
            rt = bc + "\n"
            if snip and (PREAMBLE_ON == "all" or j == 0):
                rt += f"[საფუძველი] {snip}\n"
            if kid["lead_in"]:
                rt += f"[კონტექსტი] {kid['lead_in']}\n"
            rt += kid["text"]
            children.append({
                "chunk_id": f"{pid}:{j:02d}", "parent_id": pid, "doc_id": doc_id,
                "kind": "preamble" if kind == "preamble" else kid["kind"], "text": kid["text"],
                "retrieval_text": rt, "char_len": len(kid["text"]), "breadcrumb": bc,
                "lead_in": kid["lead_in"],
                "article_no": u.get("art_no"), "article_title": u.get("art_title"),
                "chapter": u.get("chapter"), "annex": annex, "annex_title": at,
                "doc_type": meta["type"], "adopted": meta["adopted"],
                "effective_from": meta["effective_from"], "expires": meta["expires"],
            })

    idx = 0
    for annex in sorted(preambles, key=lambda a: (a is not None, str(a))):
        pu = {"annex": annex, "chapter": None, "art_no": None,
              "art_title": "სამართლებრივი საფუძველი (პრეამბულა)",
              "blocks": [{"type": "text", "text": preambles[annex], "label": None}], "flags": set()}
        emit(idx, pu, kind="preamble")
        idx += 1
    for u in units:
        emit(idx, u)
        idx += 1

    meta["n_parents"], meta["n_children"] = len(parents), len(children)
    return meta, parents, children


# ============================================================
# REPORT
# ============================================================
def sort_key_no(no):
    m = re.match(r"(\d+)(?:\^(\d+))?", no or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def pct(vals):
    if not vals:
        return {}
    v = sorted(vals)
    q = lambda p: v[min(len(v) - 1, int(p * len(v)))]
    return {"n": len(v), "min": v[0], "p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "max": v[-1]}


def build_report(stats, docs, parents, children):
    L = []
    L.append("=" * 78)
    L.append("CHUNKING REPORT")
    L.append("=" * 78)
    L.append(f"config: SPLIT_ABOVE={SPLIT_ABOVE} TARGET={TARGET} MAX_CHILD={MAX_CHILD} MIN_CHILD={MIN_CHILD} "
             f"TABLE_PARENT_MAX={TABLE_PARENT_MAX} PREAMBLE_ON={PREAMBLE_ON}")
    L.append(f"documents: {len(docs)} | parents: {len(parents)} | children: {len(children)}")
    L.append(f"docs with zero children: {[d['doc_id'] for d in docs if d['n_children'] == 0][:15]}")
    L.append("")
    L.append("stats: " + json.dumps(dict(stats), ensure_ascii=False))
    L.append("   (expect empty_units_dropped = 0; watch heading_with_inline_body, empty_unit_rescued_from_title,")
    L.append("    annex_footers_nonempty, table_parents)")
    L.append("")
    L.append("parent kinds: " + str(Counter(p["kind"] for p in parents)))
    L.append("parent flags: " + str(Counter(f for p in parents for f in p["flags"])))
    L.append("child kinds:  " + str(Counter(c["kind"] for c in children)))
    L.append("")
    L.append("child char_len:  " + str(pct([c["char_len"] for c in children])))
    L.append("parent char_len: " + str(pct([p["char_len"] for p in parents])))
    L.append(f"children over MAX_CHILD ({MAX_CHILD}): {sum(c['char_len'] > MAX_CHILD for c in children)}")
    L.append(f"parents with 1 child: {sum(p['n_children'] == 1 for p in parents)} | "
             f"with >10 children: {sum(p['n_children'] > 10 for p in parents)}")
    L.append(f"children with lead_in (continuations of a split paragraph): {sum(bool(c['lead_in']) for c in children)}")
    L.append("")
    units = [p for p in parents if p["kind"] == "unit"]
    L.append(f"units with article number: {sum(bool(p['article_no']) for p in units)}/{len(units)}")
    titled = [p for p in units if p["article_title"]]
    L.append(f"units with article title : {len(titled)}/{len(units)}")
    L.append(f"   body-like titles (>{TITLE_MAX} chars or ending . ; : ,) -> should be ~0: "
             f"{sum(len(p['article_title']) > TITLE_MAX or p['article_title'][-1] in '.;:,' for p in titled)}")
    L.append(f"units with chapter       : {sum(bool(p['chapter']) for p in units)}/{len(units)}")

    seq = defaultdict(list)
    for p in units:
        k = sort_key_no(p["article_no"])
        if k and not p["table_part"]:
            seq[(p["doc_id"], p["annex"])].append((k, p["article_no"]))
    anomalies = []
    for key, items in seq.items():
        for (a, an), (b, bn) in zip(items, items[1:]):
            if b <= a:
                anomalies.append((key[0], key[1], an, bn))
    L.append(f"\narticle-number anomalies (non-increasing within doc/annex): {len(anomalies)}")
    for a in anomalies[:12]:
        L.append(f"   {a}")

    big = sorted(parents, key=lambda p: -p["char_len"])[:8]
    L.append("\nlargest parents (chars, parent_id, children, flags):")
    for p in big:
        L.append(f"   {p['char_len']:>8}  {p['parent_id']}  {p['n_children']:>4}  {p['flags']}")
    return "\n".join(L)


def write_sample(path, docs, parents, children, k=3):
    random.seed(7)
    by_doc = defaultdict(list)
    for c in children:
        by_doc[c["doc_id"]].append(c)
    ids = [d["doc_id"] for d in docs if d["n_children"] > 5]
    lines = []
    for did in random.sample(ids, min(k, len(ids))):
        lines.append("=" * 90)
        lines.append(f"DOC {did}")
        for c in by_doc[did][:14]:
            lines.append(f"\n[{c['chunk_id']}] ({c['kind']}, {c['char_len']} chars)")
            lines.append(f"  BC: {c['breadcrumb']}")
            if c["lead_in"]:
                lines.append(f"  LEAD-IN: {cap(c['lead_in'], 120)}")
            lines.append("  " + cap(c["text"].replace("\n", " ⏎ "), 260))
    path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="matsne_data/documents_raw")
    ap.add_argument("--jsonl", default="matsne_data/documents.jsonl")
    ap.add_argument("--out", default="matsne_data/chunks")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    raw, out = Path(a.raw), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    jrows = {}
    jp = Path(a.jsonl)
    if jp.exists():
        for line in jp.open(encoding="utf-8"):
            try:
                r = json.loads(line)
                jrows[str(r["document_id"])] = r
            except Exception:
                pass

    files = sorted(raw.glob("*.html"))[: a.limit]
    stats = Counter()
    docs, parents, children = [], [], []
    for f in files:
        meta, ps, cs = process_doc(f.stem, f.read_text(encoding="utf-8"), jrows.get(f.stem), stats)
        docs.append(meta)
        parents += ps
        children += cs

    def dump(name, rows):
        with (out / name).open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    dump("docs.jsonl", docs)
    dump("parents.jsonl", parents)
    dump("children.jsonl", children)
    report = build_report(stats, docs, parents, children)
    (out / "report.txt").write_text(report, encoding="utf-8")
    write_sample(out / "sample.txt", docs, parents, children)
    print(report)
    print(f"\nWrote {out}/docs.jsonl, parents.jsonl, children.jsonl, report.txt, sample.txt")


if __name__ == "__main__":
    main()