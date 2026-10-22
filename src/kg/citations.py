"""Citations: verification and links (docs/KG_V2_PLAN.md section 11).

Every LLM output that labels or proposes something carries ``citations``. The model gives only ``kind``, ``id`` and a
verbatim ``quote``; the CODE then

* checks that the id exists and has the right node type,
* checks that the quote is a verbatim substring of that node's text (whitespace, quote marks, dashes and
  superscripts are normalised; ``...`` / ``…`` inside a quote splits it into segments that must appear in order),
* fills ``url`` and ``label`` itself (never taken from the model).

Links: provisions point to the act's page on Matsne (deep anchors are not guaranteed, see the plan section 18);
clauses and obligations get internal links used by the Streamlit app (``?doc=...&clause=...``).
"""
from __future__ import annotations

import re
from typing import Literal
from urllib.parse import quote as urlquote

from pydantic import BaseModel

from kg.ids import display_path, norm_sup

CITE_KINDS = ("provision", "clause", "obligation", "act", "external")
_NODE_TYPES = {
    "provision": {"ProvisionVersion", "Provision"},
    "clause": {"Clause", "InternalDoc"},
    "obligation": {"Obligation"},
    "act": {"Act", "AmendingAct"},
    "external": {"ExternalAct", "ExternalProvision"},
}


class Citation(BaseModel):
    kind: Literal["provision", "clause", "obligation", "act", "external"]
    id: str
    quote: str | None = None
    url: str | None = None          # filled by code
    label: str | None = None        # filled by code (human readable name of the cited node)


# quotation marks are dropped on both sides (a model may or may not copy them); dashes are unified
_QUOTES = str.maketrans({"„": None, "“": None, "”": None, "«": None, "»": None, "’": None, "‘": None, "`": None,
                         '"': None, "'": None, "–": "-", "—": "-", "−": "-", " ": " "})


def norm_text(s: str | None) -> str:
    s = norm_sup(s or "").translate(_QUOTES)
    return re.sub(r"\s+", " ", s).strip().strip(".;,:").lower()


def quote_in_text(quote: str, text: str) -> bool:
    """Verbatim (after normalisation); a quote with '...' must have all its segments in order."""
    q, t = norm_text(quote), norm_text(text)
    if not q:
        return False
    segs = [s.strip() for s in re.split(r"\.\.\.|…", q) if s.strip(" .;,:")]
    pos = 0
    for s in segs:
        s = s.strip(" .;,:")
        i = t.find(s, pos)
        if i < 0:
            return False
        pos = i + len(s)
    return bool(segs)


def node_text(store, node_id: str) -> str | None:
    """The text a citation to this node may quote from (None when the node does not exist)."""
    n = store.get_node(node_id)
    if n is None:
        return None
    p, t = n["props"], n["type"]
    if t == "ProvisionVersion":
        return " ".join(x for x in (p.get("heading"), p.get("label"), p.get("text")) if x)
    if t == "Provision":                                  # any version of the provision may be quoted
        parts = []
        for it in store.neighbors(node_id, "VERSION_OF", "in"):
            q = it["node"]["props"]
            parts.append(" ".join(x for x in (q.get("heading"), q.get("label"), q.get("text")) if x))
        return "\n".join(parts)
    if t == "Clause":
        return " ".join(x for x in (p.get("heading"), p.get("text")) if x)
    if t == "Obligation":
        return " ".join(str(p.get(k) or "") for k in ("statement", "subject", "action", "condition",
                                                       "deadline_or_frequency", "exceptions"))
    if t in ("Act", "AmendingAct", "ExternalAct"):
        return p.get("title") or ""
    if t == "InternalDoc":
        return p.get("title") or ""
    if t == "ExternalProvision":
        return ""
    return json_text(p)


def json_text(p: dict) -> str:
    return " ".join(str(v) for v in p.values() if isinstance(v, str))


def verify_citations(store, cites: list[Citation], allowed_ids: set[str] | None = None,
                     require_quote: bool = False, soft_quotes: bool = False) -> tuple[list[Citation], list[str]]:
    """-> (verified citations with url/label filled, error messages for the dropped ones).

    ``soft_quotes``: a citation whose id is fine but whose quote is not verbatim is KEPT without the quote (the UI
    then shows the provision but no quote) and reported as a "note: ..." message instead of an error."""
    good, errors = [], []
    for c in cites or []:
        if allowed_ids is not None and c.id not in allowed_ids:
            errors.append(f"citation {c.id}: id was not part of the input")
            continue
        n = store.get_node(c.id)
        if n is None:
            errors.append(f"citation {c.id}: unknown id")
            continue
        if n["type"] not in _NODE_TYPES[c.kind]:
            errors.append(f"citation {c.id}: kind '{c.kind}' does not match node type {n['type']}")
            continue
        if c.quote:
            text = node_text(store, c.id) or ""
            if not quote_in_text(c.quote, text):
                if soft_quotes:
                    errors.append(f"note: citation {c.id}: quote not verbatim, dropped: '{c.quote[:80]}'")
                    good.append(fill_link(store, c.model_copy(update={"quote": None}), n))
                else:
                    errors.append(f"citation {c.id}: quote not found verbatim: '{c.quote[:80]}'")
                continue
        elif require_quote:
            errors.append(f"citation {c.id}: quote required")
            continue
        good.append(fill_link(store, c, n))
    return good, errors


def display_of(store, node: dict) -> str:
    """Human label of a node (Georgian)."""
    p, t = node["props"], node["type"]
    if t in ("ProvisionVersion", "Provision"):
        act = store.get_node(f"act:{p['act']}")
        short = (act or {}).get("props", {}).get("short") or p["act"]
        return f"{short} › {p.get('display') or display_path(p['path'])}"
    if t == "Clause":
        return f"{p.get('section_no') or ''} {p.get('heading') or ''}".strip() or node["id"]
    if t == "Obligation":
        return f"{p.get('statement', '')[:80]}"
    if t in ("Act", "AmendingAct", "ExternalAct", "InternalDoc"):
        return p.get("title") or p.get("number") or node["id"]
    return node["id"]


def fill_link(store, c: Citation, node: dict | None = None) -> Citation:
    node = node or store.get_node(c.id)
    out = c.model_copy()
    if node is None:
        return out
    p, t = node["props"], node["type"]
    out.label = display_of(store, node)
    if t in ("ProvisionVersion", "Provision"):
        act = store.get_node(f"act:{p['act']}")
        out.url = (act or {}).get("props", {}).get("url")
    elif t == "Clause":
        out.url = f"?doc={urlquote(p.get('doc', ''), safe='')}&clause={urlquote(node['id'], safe='')}"
    elif t == "InternalDoc":
        out.url = f"?doc={urlquote(node['id'], safe='')}"
    elif t == "Obligation":
        out.url = f"?obligation={urlquote(node['id'], safe='')}"
    elif t in ("Act", "AmendingAct"):
        out.url = p.get("url")
    return out


def dump(cites: list[Citation]) -> list[dict]:
    return [c.model_dump() for c in cites]
