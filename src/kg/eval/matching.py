"""Matching of predicted findings to gold items (docs/KG_ARCHITECTURE.md section 7).

A finding matches a gold item when it is the same document, the clause section number appears in the gold
``document_section`` (prefix match in either direction), and the article overlaps after normalisation
(``Art. 6 para 4¹`` -> ``art6/p4.1``; overlap is checked down to paragraph level). When the finding has no clause, or
the gold section has no section number (table rows, "whole document"), the article alone is used and the match is
recorded as ``article_only``; a gold item without an article is matched on the section alone (``section_only``).
"""
from __future__ import annotations

import re

SUP = {"⁰": 0, "¹": 1, "²": 2, "³": 3, "⁴": 4, "⁵": 5, "⁶": 6, "⁷": 7, "⁸": 8, "⁹": 9}
_SUPCLS = "".join(SUP)
_NUM = re.compile(r"(?<![\w.\-])(\d+(?:\.\d+)*)(?:\s*[–-]\s*(\d+(?:\.\d+)*))?")


def section_tokens(gold_section: str) -> list[str]:
    s = re.sub(r"\(also ([^)]*)\)", r", \1", gold_section or "")
    s = re.sub(r"\([^)]*\)", " ", s)
    out: list[str] = []
    for m in _NUM.finditer(s):
        a, b = m.group(1), m.group(2)
        out.append(a)
        if b:
            pa, pb = a.split("."), b.split(".")
            if len(pa) == len(pb) and pa[:-1] == pb[:-1] and pa[-1].isdigit() and pb[-1].isdigit() \
                    and 0 <= int(pb[-1]) - int(pa[-1]) <= 30:
                out += [".".join(pa[:-1] + [str(i)]) for i in range(int(pa[-1]) + 1, int(pb[-1]) + 1)]
            else:
                out.append(b)
    return out


def section_match(clause_section: str | None, tokens: list[str]) -> bool:
    if not clause_section:
        return False
    cs = clause_section.strip().rstrip(".")
    return any(cs == t or cs.startswith(t + ".") or t.startswith(cs + ".") for t in tokens)


def _num_sup(tok: str) -> tuple[int, int | None]:
    m = re.match(rf"(\d+)([{_SUPCLS}]*)", tok)
    n = int(m.group(1))
    sup = "".join(str(SUP[c]) for c in m.group(2)) if m.group(2) else ""
    return n, (int(sup) if sup else None)


def _art_path(n: int, sup: int | None) -> str:
    return f"art{n}" + (f"_{sup}" if sup else "")


def _para_path(n: int, sup: int | None) -> str:
    return f"p{n}" + (f".{sup}" if sup else "")


def parse_gold_articles(text: str) -> list[str]:
    """'Art. 6 para 4¹–4²; Art. 9¹' -> ['art6/p4.1', 'art6/p4.2', 'art9_1']  (paragraph level)."""
    out: list[str] = []
    for part in re.split(r"\s*;\s*", text or ""):
        m = re.match(rf"Art\.\s*(\d+[{_SUPCLS}]*)(?:\s*[–-]\s*(\d+[{_SUPCLS}]*))?(.*)$", part.strip())
        if not m:
            continue
        a1, a2, rest = m.group(1), m.group(2), m.group(3)
        arts = [_art_path(*_num_sup(a1))]
        if a2:
            n1, s1 = _num_sup(a1)
            n2, s2 = _num_sup(a2)
            arts = [_art_path(i, None) for i in range(n1, n2 + 1)] if not s1 and not s2 else arts + [_art_path(n2, s2)]
        pm = re.search(r"para\s+(.*)$", rest)
        if not pm:
            out += arts
            continue
        plist = re.split(r"[ა-ჰ]", pm.group(1))[0]          # drop sub-point letters
        paras: list[str] = []
        for chunk in re.split(r"\s*,\s*", plist.strip()):
            rm = re.match(rf"(\d+[{_SUPCLS}]*)(?:\s*[–-]\s*(\d+[{_SUPCLS}]*))?", chunk)
            if not rm:
                continue
            n1, s1 = _num_sup(rm.group(1))
            if rm.group(2):
                n2, s2 = _num_sup(rm.group(2))
                if n1 == n2 and s1 is not None or (n1 == n2 and s2 is not None):
                    paras += [_para_path(n1, None if i == 0 else i) for i in range(s1 or 0, (s2 or 0) + 1)]
                else:
                    paras += [_para_path(i, None) for i in range(n1, n2 + 1)]
            else:
                paras.append(_para_path(n1, s1))
        out += [f"{arts[0]}/{p}" for p in paras] or arts
    return out


def article_overlap(source_path: str | None, gold_paths: list[str]) -> bool:
    if not source_path or not gold_paths:
        return False
    segs = source_path.split("/")
    sa, sp = segs[0], (segs[1] if len(segs) > 1 and segs[1].startswith("p") else None)
    for g in gold_paths:
        gs = g.split("/")
        ga, gp = gs[0], (gs[1] if len(gs) > 1 else None)
        if sa == ga and (sp is None or gp is None or sp == gp):
            return True
    return False


def match(finding: dict, gold: dict, gold_paths: list[str], tokens: list[str]) -> str | None:
    """finding: {'doc_file', 'section_no', 'clause_id', 'source_path'}; returns 'section+article',
    'article_only' or None."""
    if finding["doc_file"] != gold["document_file"]:
        return None
    if not gold_paths:                      # gold item without an article ("—"): the section decides
        if finding.get("section_no") and tokens and section_match(finding["section_no"], tokens):
            return "section_only"
        return None
    if not article_overlap(finding.get("source_path"), gold_paths):
        return None
    if finding.get("clause_id") and finding.get("section_no") and tokens:
        return "section+article" if section_match(finding["section_no"], tokens) else None
    return "article_only"
