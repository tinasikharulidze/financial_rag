"""Shared text helpers for act and amendment parsing: label detection and provision segmentation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from kg.ids import art_seg, join_path, norm_sup, para_seg, sub_seg

# paragraph "4." / "4^1." (digits then a dot, not a decimal number); sub-point "ა)" / "ა^1)"; nested "ბ.ე)" / "ბ.ბ^1)"
RE_PARA_LABEL = re.compile(r"^(\d{1,3})(?:\^(\d{1,2}))?\s*\.(?!\d)\s*")
RE_SUBSUB_LABEL = re.compile(r"^([ა-ჰ])\.([ა-ჰ])(?:\^(\d{1,2}))?\s*\)\s*")
RE_SUB_LABEL = re.compile(r"^([ა-ჰ])(?:\^(\d{1,2}))?\s*\)\s*")
RE_ART_START = re.compile(r"^მუხლი\s+(\d{1,3})(?:\^(\d{1,2}))?\s*[.:]?\s*(.*)$", re.S)
RE_CHAPTER_START = re.compile(r"^თავი\s+([IVXLC]+|\d+)\s*[.:]?\s*(.*)$", re.S)


def classify_label(text: str):
    """-> (kind, groups, rest) with kind in para|sub|subsub|None. Text is expected to be norm_sup'd."""
    m = RE_SUBSUB_LABEL.match(text)
    if m:
        return "subsub", m.groups(), text[m.end():]
    m = RE_SUB_LABEL.match(text)
    if m:
        return "sub", m.groups(), text[m.end():]
    m = RE_PARA_LABEL.match(text)
    if m:
        return "para", m.groups(), text[m.end():]
    return None, (), text


@dataclass
class NewProv:
    path: str
    label: str = ""
    heading: str | None = None
    text: str = ""
    chapter: str | None = None
    level: str = "paragraph"

    def append(self, line: str):
        self.text = (self.text + "\n" + line).strip() if self.text else line


def level_of(path: str) -> str:
    last = path.split("/")[-1]
    if last.startswith("ord"):
        return "order_article"
    if last.startswith("annex"):
        return "annex"
    if last.startswith("art"):
        return "article"
    if last.startswith("p"):
        return "paragraph"
    return "sub"


def segment_lines(lines: list[str], art: str | None, para: str | None = None, default_path: str | None = None,
                  allow_articles: bool = False, chapter: str | None = None):
    """Turn quoted/body lines into provisions.

    ``art`` / ``para`` are the path segments of the enclosing article/paragraph (e.g. 'art6', 'p5'). Labels in the
    lines decide the new paths. Unlabelled leading text goes to ``default_path``. Returns (list[NewProv], problems).
    """
    out: list[NewProv] = []
    seen: dict[str, int] = {}
    problems: list[str] = []
    cur: NewProv | None = None
    cur_art, cur_para = art, para
    cur_chapter = chapter
    skip_chapter_title = False

    def start(path: str, **kw) -> NewProv:
        if path in seen:
            seen[path] += 1
            problems.append(f"duplicate_path:{path}")
            path = f"{path}~{seen[path]}"
        else:
            seen[path] = 1
        p = NewProv(path=path, level=level_of(path), chapter=cur_chapter, **kw)
        out.append(p)
        return p

    for raw in lines:
        line = norm_sup(raw.strip())
        if not line:
            continue
        if skip_chapter_title:               # the line after "თავი XII" is the chapter title
            skip_chapter_title = False
            if not classify_label(line)[0] and not RE_ART_START.match(line):
                continue
        m = RE_CHAPTER_START.match(line) if allow_articles else None
        if m:
            cur_chapter = m.group(1)
            if m.group(2).strip():
                continue
            skip_chapter_title = True
            continue
        m = RE_ART_START.match(line) if allow_articles else None
        if m:
            seg = art_seg(int(m.group(1)), int(m.group(2)) if m.group(2) else None)
            cur_art, cur_para = seg, None
            cur = start(seg, heading=m.group(3).strip() or None)
            continue
        kind, g, rest = classify_label(line)
        if kind == "para" and cur_art:
            seg = para_seg(int(g[0]), int(g[1]) if g[1] else None)
            cur_para = seg
            cur = start(join_path(cur_art, seg), label=line[: len(line) - len(rest)].strip(), text=rest.strip())
            continue
        if kind in ("sub", "subsub") and cur_art:
            if kind == "sub":
                seg = sub_seg(g[0], int(g[1]) if g[1] else None)
            else:
                seg = sub_seg(g[0], None, g[1], int(g[2]) if g[2] else None)
            cur = start(join_path(cur_art, cur_para, seg), label=line[: len(line) - len(rest)].strip(),
                        text=rest.strip())
            continue
        if kind in ("sub", "subsub") and not cur_art and default_path:
            # e.g. sub-point replacement where the parent was only known through default_path
            parent = "/".join(default_path.split("/")[:-1])
            seg = sub_seg(g[0], int(g[1]) if g[1] else None) if kind == "sub" else sub_seg(g[0], None, g[1], int(g[2]) if g[2] else None)
            cur = start(join_path(parent, seg), label=line[: len(line) - len(rest)].strip(), text=rest.strip())
            continue
        # continuation text
        if cur is None:
            if default_path:
                cur = start(default_path, text=line)
            else:
                problems.append("unlabelled_text_without_target")
            continue
        cur.append(line)
    return out, problems


def strip_quote_marks(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^[„“”\"«]\s*", "", s)
    s = re.sub(r"\s*[“”\"»]\s*[.;,]?\s*$", "", s)
    return s.strip()
