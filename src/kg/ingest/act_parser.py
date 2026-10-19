"""Parse a base act (Matsne HTML) into Provisions: one per article, paragraph and sub-point.

Reuses the element-id walk and text helpers of ``src/chunker.py`` (``part_blocks``, ``doc_meta``, ``tidy`` ...) but
emits stable-path provisions instead of retrieval chunks. Unlike the prototype chunker it also reads SUBPOINT
elements (2060636 keeps its lettered lists there; the chunker silently dropped them).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

import chunker  # src/chunker.py (the CLI puts src/ on sys.path)
from kg.ids import art_seg, join_path, norm_sup, para_seg, sub_seg
from kg.ingest.texttools import (NewProv, RE_ART_START, RE_CHAPTER_START, classify_label, level_of)

# line that is only an amendment note in a consolidated text (4822603): "... ბრძანება №193/04 - ვებგვერდი, 30.12.2022 წ."
RE_NOTE = re.compile(r"ბრძანება\s*№?\s*[\d/]+\s*[-–]\s*ვებგვერდი,\s*[\d./]+\s*წ?\.?\s*$")


@dataclass
class ParsedProvision:
    path: str
    level: str
    label: str = ""
    heading: str | None = None
    text: str = ""
    chapter: str | None = None
    chapter_title: str | None = None
    amendment_notes: list = field(default_factory=list)   # stripped "… - ვებგვერდი, dd.mm.yyyy" lines (snapshot acts)
    para_seg: str | None = None                            # internal: paragraph segment while parsing


@dataclass
class ParsedAct:
    doc_id: str
    meta: dict
    provisions: list[ParsedProvision]
    stats: dict


def _leaf(prefix: str):
    keys = chunker.key_list(prefix)
    return keys, (keys[-1][0] if keys else None)


def parse_act_html(html: str, doc_id: str, strip_notes: bool = False) -> ParsedAct:
    soup = BeautifulSoup(html, "html.parser")
    md = soup.select_one("#maindoc")
    meta = chunker.doc_meta(soup, doc_id, None)
    stats = {"articles": 0, "paragraphs": 0, "subs": 0, "dup_paths": 0, "notes_stripped": 0, "order_articles": 0}
    if md is None:
        return ParsedAct(doc_id, meta, [], stats)

    els = md.find_all(id=True)
    titles = {}
    for e in els:
        prefix, suf = chunker.split_id(e["id"])
        if suf == "_Title":
            titles[prefix] = chunker.plain_text(e)
    meta["title"] = None
    for e in els:
        prefix, suf = chunker.split_id(e["id"])
        if suf == "_Content" and prefix.endswith("HEADER:1;") is False and prefix.startswith("DOCUMENT:1;HEADER"):
            meta["title"] = chunker.plain_text(e) or meta["title"]
    meta["title"] = meta["title"] or meta["title_fallback"]

    provs: list[ParsedProvision] = []
    index: dict[str, ParsedProvision] = {}

    def add(p: ParsedProvision):
        if p.path in index:
            stats["dup_paths"] += 1
            n = 2
            while f"{p.path}~{n}" in index:
                n += 1
            p.path = f"{p.path}~{n}"
        index[p.path] = p
        provs.append(p)
        return p

    def clean(t: str) -> str:
        if not strip_notes:
            return t
        kept = []
        for line in t.split("\n"):
            if RE_NOTE.search(line.strip()):
                stats["notes_stripped"] += 1
                continue
            kept.append(line)
        return "\n".join(kept)

    # --- order (document level) articles: "ord{n}"
    # --- regulation: ENCLOSURE:1 articles; further enclosures become annexes
    content: dict[str, list] = {}        # article prefix -> ordered blocks
    art_meta: dict[str, dict] = {}
    enclosure_text: dict[str, list] = {}
    order_prefixes = []
    for e in els:
        prefix, suf = chunker.split_id(e["id"])
        keys = chunker.key_list(prefix)
        if not keys:
            continue
        kd = dict(keys)
        enc = kd.get("ENCLOSURE")
        if suf == "_Title" and keys[-1][0] == "ARTICLE" and enc is None:
            order_prefixes.append(prefix)
        if suf != "_Content":
            continue
        if enc is None and any(k == "ARTICLE" for k, _ in keys) and keys[-1][0] in ("ARTICLE", "POINT", "SUBPOINT"):
            content.setdefault(chunker.prefix_until(prefix, "ARTICLE"), []).extend(chunker.part_blocks(e))
            continue
        if enc == "1" and any(k == "ARTICLE" for k, _ in keys) and keys[-1][0] in ("ARTICLE", "POINT", "SUBPOINT"):
            ap = chunker.prefix_until(prefix, "ARTICLE")
            content.setdefault(ap, []).extend(chunker.part_blocks(e))
            continue
        if enc and enc != "1" and keys[-1][0] not in ("HEADER", "FOOTER"):
            enclosure_text.setdefault(enc, []).extend(chunker.part_blocks(e))

    # article metadata (number, title, chapter) in document order
    for e in els:
        prefix, suf = chunker.split_id(e["id"])
        if suf != "_Title":
            continue
        keys = chunker.key_list(prefix)
        if not keys or keys[-1][0] != "ARTICLE":
            continue
        kd = dict(keys)
        ch_prefix = chunker.prefix_until(prefix, "CHAPTER") if "CHAPTER" in kd else None
        ch_no = ch_title = None
        if ch_prefix and ch_prefix in titles:
            m = RE_CHAPTER_START.match(titles[ch_prefix])
            if m:
                ch_no, ch_title = m.group(1), m.group(2).strip() or None
        art_meta[prefix] = {"title": titles.get(prefix, ""), "enc": kd.get("ENCLOSURE"), "chapter": ch_no,
                            "chapter_title": ch_title}

    def segment(art_path: str, heading, blocks, chapter, ch_title):
        art = add(ParsedProvision(art_path, level_of(art_path), heading=heading, chapter=chapter,
                                  chapter_title=ch_title))
        cur, cur_para = art, None
        for b in blocks:
            text = b["text"]
            if strip_notes and RE_NOTE.search(text.strip()):
                stats["notes_stripped"] += 1
                cur.amendment_notes.append(text.strip())
                continue
            if b["type"] == "table":
                cur.text = (cur.text + "\n" + text).strip()
                continue
            for line in text.split("\n"):
                line = norm_sup(line.strip())
                if not line:
                    continue
                if strip_notes and RE_NOTE.search(line):
                    stats["notes_stripped"] += 1
                    cur.amendment_notes.append(line)
                    continue
                kind, g, rest = classify_label(line)
                label = line[: len(line) - len(rest)].strip()
                if kind == "para":
                    seg = para_seg(int(g[0]), int(g[1]) if g[1] else None)
                    cur = cur_para = add(ParsedProvision(join_path(art_path, seg), "paragraph", label, None,
                                                         rest.strip(), chapter, ch_title))
                    cur_para.para_seg = seg
                elif kind in ("sub", "subsub"):
                    seg = (sub_seg(g[0], int(g[1]) if g[1] else None) if kind == "sub"
                           else sub_seg(g[0], None, g[1], int(g[2]) if g[2] else None))
                    parent = join_path(art_path, cur_para.para_seg if cur_para else None)
                    cur = add(ParsedProvision(join_path(parent, seg), "sub", label, None, rest.strip(), chapter,
                                              ch_title))
                else:
                    cur.text = (cur.text + "\n" + line).strip() if cur.text else line

    # order articles
    for prefix in order_prefixes:
        m = RE_ART_START.match(titles.get(prefix, ""))
        if not m:
            continue
        n = int(m.group(1))
        blocks = content.get(prefix, [])
        segment_path = f"ord{n}"
        art = add(ParsedProvision(segment_path, "order_article", heading=None))
        txt = "\n".join(b["text"] for b in blocks)
        art.text = txt
        stats["order_articles"] += 1

    # regulation articles in document order
    for prefix in [k for k in art_meta if art_meta[k]["enc"] == "1"]:
        am = art_meta[prefix]
        m = RE_ART_START.match(norm_sup(am["title"]))
        if not m:
            continue
        seg = art_seg(int(m.group(1)), int(m.group(2)) if m.group(2) else None)
        heading = m.group(3).strip() or None
        segment(seg, heading, content.get(prefix, []), am["chapter"], am["chapter_title"])
        stats["articles"] += 1

    # annexes: every enclosure after the regulation becomes one provision
    for enc in sorted(enclosure_text, key=lambda x: int(x)):
        txt = "\n".join(clean(b["text"]) for b in enclosure_text[enc] if b["text"].strip())
        if txt.strip():
            add(ParsedProvision(f"annex{int(enc) - 1}", "annex", text=txt))

    stats["paragraphs"] = sum(1 for p in provs if p.level == "paragraph")
    stats["subs"] = sum(1 for p in provs if p.level == "sub")
    return ParsedAct(doc_id, meta, provs, stats)
