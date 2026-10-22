"""Titles, readable paths and the search text of provisions (docs/KG_V2_PLAN.md section 5.1).

``index_text`` is the text used by EVERY search and embedding over provisions:

    {act short name} › მუხლი {N}. {article title} › {paragraph / sub-point path}
    {label} {text}

so that a paragraph like "4. ... must be repeated annually" carries the topic of its article ("Model validation").
Order articles ("ord1") and annexes have no heading in the source; they get a descriptive title from their text.
"""
from __future__ import annotations

from kg.ids import display_path


def article_of(path: str) -> str:
    return path.split("/")[0]


def display_parts(path: str) -> list[str]:
    return display_path(path).split(", ")


def article_display(path: str) -> str:
    return display_parts(path)[0]


def remainder_display(path: str) -> str:
    return ", ".join(display_parts(path)[1:])


def fallback_title(path: str, text: str | None) -> str:
    """Title for an order article or annex without heading."""
    txt = (text or "").strip()
    first = article_of(path)
    if first.startswith("annex"):
        lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
        return " – ".join(lines[:2])[:120]
    return " ".join(txt.split())[:90]


def heading_at(versions: dict, path: str, date: str) -> str:
    """Heading of the article that contains ``path``, in the version valid on ``date`` (``versions`` is
    ``VersionedAct.versions``: path -> list of versions)."""
    art = article_of(path)
    vs = versions.get(art) or []
    cur = None
    for v in vs:
        if v.valid_from <= date:
            cur = v
    cur = cur or (vs[0] if vs else None)
    if cur is None:
        return ""
    return cur.heading or fallback_title(art, cur.text)


def provision_title(path: str, label: str, text: str, article_heading: str) -> str:
    """Title of a provision: the article heading for articles; label + the first words for the rest."""
    if "/" not in path:
        return article_heading
    snippet = " ".join((text or "").split())[:80]
    return f"{label} {snippet}".strip()


def make_index_text(act_short: str | None, path: str, article_heading: str, label: str, text: str) -> str:
    head = f"{act_short or ''} › {article_display(path)}".strip()
    if article_heading:
        head += f". {article_heading}"
    rest = remainder_display(path)
    if rest:
        head += f" › {rest}"
    body = " ".join(x for x in ((label or "").strip(), (text or "").strip()) if x)
    return f"{head}\n{body}" if body else head
