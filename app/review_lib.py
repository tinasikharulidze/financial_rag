"""Data and HTML of the document review page (plan section 14.4); no Streamlit here so it can be unit-tested."""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict

from i18n_ka import CAUSE, KIND, PRIORITY, ROLE, T, TASK_STATUS, VERDICT, VERDICT_COLOR, tr
from kg.mapping.docview import load_doc
from kg.store.v2tables import jload

ISSUE_VERDICTS = ("partial", "contradicts", "none")
CLASS_FOR = {"contradicts": "bad", "partial": "part", "anchor": "anchor", "full": "ok"}


def _cites(raw) -> list[dict]:
    return [c for c in (jload(raw, []) if isinstance(raw, str) else (raw or []))]


def build_review(cfg, store, version_id: str) -> dict:
    view = load_doc(store, version_id)
    cov = {r["obligation_id"]: r for r in store.q("SELECT * FROM coverage WHERE doc_version_id=?", (version_id,))}
    props = {r["obligation_id"]: r for r in store.q("SELECT * FROM proposals WHERE doc_version_id=?", (version_id,))}
    tasks = {r["obligation_id"]: r for r in store.q("SELECT * FROM tasks WHERE doc_version_id=?", (version_id,))}
    tri = {r["obligation_id"]: r for r in store.q("SELECT * FROM triage WHERE doc_version_id=?", (version_id,))}
    edges = defaultdict(list)
    for e in store.edges("COVERED_BY"):
        if e["props"].get("doc_version") == version_id:
            edges[e["src"]].append(e)
    nodes = store.get_nodes(list(cov) + list(tri))
    cards_by_clause: dict[str, list[dict]] = defaultdict(list)
    cards = {}
    for oid, c in cov.items():
        ob = nodes.get(oid)
        if ob is None:
            continue
        p = ob["props"]
        amend = c["cause"] == "amendment"
        card = {"oid": oid, "verdict": c["verdict"], "severity": p.get("severity"), "cause": c["cause"],
                "amendment_number": p.get("amendment_number") if amend else None,
                "amendment_effective": ob["valid_from"] if amend else None,
                "statement": p.get("statement"), "missing": c["missing"], "rationale": c["rationale"],
                "regulation": [x for x in _cites(c["citations"]) if x.get("kind") == "provision"],
                "kind": (props.get(oid) or {}).get("kind"),
                "task_key": (tasks.get(oid) or {}).get("key"), "task_status": (tasks.get(oid) or {}).get("status"),
                "relevance": (tri.get(oid) or {}).get("relevance")}
        cards[oid] = card
        oe = edges.get(oid, [])
        # the judge may call the obligation "contradicts" but label the clause it cites "partial": the clause is then
        # shown as contradicting (the same rule the judge step now applies when it stores the result)
        fix_contra = c["verdict"] == "contradicts" and not any(x["props"].get("role") == "contradicts" for x in oe)
        for e in oe:
            role = e["props"].get("role")
            if fix_contra and role == "partial":
                role = "contradicts"
            if role in ("partial", "contradicts") or (role == "full" and c["verdict"] == "full"):
                cards_by_clause[e["dst"]].append({**card, "role": role, "clause_quote": e["props"].get("quote"),
                                                  "why": e["props"].get("why"), "edge_source": e["props"].get("source")})
        pr = props.get(oid)
        if pr and pr["clause_id"] and c["verdict"] in ISSUE_VERDICTS:
            if pr["kind"] == "add_section":
                cards_by_clause[pr["clause_id"]].append({**card, "role": "anchor", "clause_quote": None, "why": None,
                                                         "edge_source": None})
            elif not any(x["oid"] == oid for x in cards_by_clause[pr["clause_id"]]):
                cards_by_clause[pr["clause_id"]].append({**card, "role": c["verdict"], "clause_quote": None,
                                                         "why": None, "edge_source": None})
    units = [{"id": u["id"], "section_no": u["section_no"], "unit_type": u["unit_type"], "text": u["text"],
              "cards": cards_by_clause.get(u["id"], [])} for u in view.units if u["unit_type"] != "signature"]
    irrelevant = [{"oid": oid, "statement": (nodes.get(oid) or {"props": {}})["props"].get("statement"),
                   "reason": r["reason"], "source": r["source"]} for oid, r in tri.items()
                  if r["relevance"] == "irrelevant"]
    covered = [{"oid": oid, "statement": cards[oid]["statement"],
                "edges": [{"clause_id": e["dst"], "role": e["props"].get("role"), "quote": e["props"].get("quote"),
                           "source": e["props"].get("source")} for e in edges.get(oid, [])]}
               for oid, c in cov.items() if c["verdict"] == "full" and oid in cards]
    undecided = [{"oid": oid, "statement": cards[oid]["statement"], "problem": c["rationale"]}
                 for oid, c in cov.items() if c["verdict"] == "undecided" and oid in cards]
    counts = Counter(t["status"] for t in tasks.values())
    # the lists under the document: relevance (triage) first, then coverage (judge)
    by_relevance = {k: [] for k in ("irrelevant", "possibly", "relevant")}
    for oid, r in tri.items():
        if r["relevance"] in by_relevance:
            by_relevance[r["relevance"]].append({"oid": oid, "reason": r["reason"], "source": r["source"],
                                                 "statement": (nodes.get(oid) or {"props": {}})["props"].get("statement")})
    by_verdict = {k: [] for k in ("contradicts", "none", "partial", "full", "undecided")}
    for oid, c in cov.items():
        if c["verdict"] in by_verdict and oid in cards:
            by_verdict[c["verdict"]].append({"oid": oid, "statement": cards[oid]["statement"], "missing": c["missing"],
                                             "rationale": c["rationale"], "task_status": cards[oid]["task_status"]})
    # one row per obligation of the check: obligation | relevance | coverage | reasoning | source (+ link)
    table = []
    for oid, r in tri.items():
        ob = nodes.get(oid) or {"props": {}}
        c = cov.get(oid)
        cites = [x for x in (ob["props"].get("citations") or []) if x.get("kind") == "provision"]
        if c is not None:
            reason = " ".join(x for x in (c["rationale"], c["missing"] and f"({c['missing']})") if x)
        else:
            reason = r["reason"] or ""
        table.append({"oid": oid, "statement": ob["props"].get("statement") or oid, "relevance": r["relevance"],
                      "verdict": c["verdict"] if c is not None else None, "reason": reason,
                      "source": (cites[0].get("label") if cites else None) or ob["props"].get("source_path") or "",
                      "url": cites[0].get("url") if cites else None, "task_status": (tasks.get(oid) or {}).get("status")})
    order_r = {"relevant": 0, "possibly": 1, "irrelevant": 2}
    order_v = {"contradicts": 0, "none": 1, "partial": 2, "undecided": 3, "full": 4, None: 5}
    table.sort(key=lambda x: (order_r.get(x["relevance"], 9), order_v.get(x["verdict"], 9), x["source"]))
    from kg.services.edits import get_edits
    from kg.services.tasks import duplicate_new_documents
    comp = store.q1("SELECT d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                    "WHERE dv.id=?", (version_id,))
    hidden = {o for v, o in duplicate_new_documents(store, comp["company_id"]) if v == version_id} if comp else set()
    return {"hidden_new": hidden, "version_id": version_id, "view": view, "units": units, "cards": cards, "tasks": tasks, "proposals": props,
            "edits": get_edits(store, version_id),
            "irrelevant": irrelevant, "covered": covered, "undecided": undecided, "counts": dict(counts),
            "by_relevance": by_relevance, "by_verdict": by_verdict, "table": table}


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def card_html(c: dict) -> str:
    color = VERDICT_COLOR.get(c["verdict"], "#555")
    parts = [f"<div class='cd'><div class='cdh' style='border-color:{color}'>"
             f"<b style='color:{color}'>{_e(tr(VERDICT, c['verdict']))}</b>"
             f" · {_e(tr(PRIORITY, c.get('severity')))}"
             f" · {_e(tr(CAUSE, c.get('cause')))}"
             + (f" · {_e(c['amendment_number'])} ({_e(c['amendment_effective'])})" if c.get("amendment_number") else "")
             + (f" · {_e(tr(KIND, c['kind']))}" if c.get("kind") else "") + "</div>"]
    parts.append(f"<div><b>{_e(T['obligation'])}:</b> {_e(c.get('statement'))}</div>")
    if c.get("rationale"):
        parts.append(f"<div><b>{_e(T['explanation'])}:</b> {_e(c['rationale'])}</div>")
    if c.get("missing"):
        parts.append(f"<div class='miss'>{_e(c['missing'])}</div>")
    if c.get("clause_quote"):
        parts.append(f"<div><b>{_e(T['clause'])}:</b> „{_e(c['clause_quote'])}“</div>")
    for r in c.get("regulation", []):
        link = f"<a href='{_e(r['url'])}' target='_blank'>{_e(r.get('label') or r['id'])}</a>" if r.get("url") \
            else _e(r.get("label") or r["id"])
        quote = f" — „{_e(r['quote'])}“" if r.get("quote") else ""
        parts.append(f"<div><b>{_e(T['regulation'])}:</b> {link}{quote}</div>")
    parts.append("</div>")
    return "".join(parts)


# ------------------------------------------------------------------------------------------------ tables (display only)
def parse_table(text: str | None):
    """The stored text of a table unit -> (header | None, rows) or None when it is not in the table pattern.
    The parser stores a table as "h1 | h2 | ..." then one line per row "h1: v1 | h2: v2 | ..." (empty cells left
    out); a long table is stored one row per unit, without the header line. Display only: nothing is changed."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    if not lines:
        return None
    first = [c.strip() for c in lines[0].split(" | ")]
    header, body = None, lines
    if len(lines) >= 2 and len(first) >= 2 and any(lines[1].startswith(h + ": ") for h in first if h):
        header, body = first, lines[1:]
    if header is None:                         # rows only: take the column names from "name: value"
        kv = [c.split(": ", 1) for c in first]
        if len(kv) >= 2 and all(len(x) == 2 and x[0] and len(x[0]) <= 60 for x in kv):
            header = [x[0].strip() for x in kv]
    if header is None or len(header) < 2:
        return None
    names = sorted([h for h in header if h], key=len, reverse=True)
    rows = []
    for ln in body:
        parts = [c.strip() for c in ln.split(" | ")]
        row: dict[str, str] = {}
        extra = []
        for part in parts:
            h = next((h for h in names if part.startswith(h + ": ") and h not in row), None)
            if h:
                row[h] = part[len(h) + 2:]
            else:
                extra.append(part)
        if not row and len(parts) == len(header):          # a row stored without the column names
            rows.append(parts)
            continue
        cells = [row.get(h, "") for h in header]
        if extra:
            cells[-1] = " | ".join([cells[-1]] + extra) if cells[-1] else " | ".join(extra)
        rows.append(cells)
    return header, rows


def table_html_of(text: str | None, original: str | None = None, head: bool = True) -> str | None:
    """A table unit as an HTML table; with ``original`` the cells that differ show the word changes and rows that
    are new are marked. None when the text is not in the table pattern."""
    t = parse_table(text)
    if t is None:
        return None
    header, rows = t
    old = parse_table(original) if original is not None else None
    old_rows = old[1] if old and len(old[0]) == len(header) else None
    wide = len(header) >= 6           # a record table (e.g. the model inventory): fixed equal columns + side scroll,
    out = ["<div class='dtw'>" if wide else "",  # so consecutive one-row units line up and words are not cut
           f"<table class='dt{' wide' if wide else ''}'"
           + (f" style='min-width:{len(header) * 120}px'" if wide else "") + ">"]
    if head:
        out.append("<thead><tr>" + "".join(f"<th>{_e(h)}</th>" for h in header) + "</tr></thead>")
    out.append("<tbody>")
    for i, r in enumerate(rows):
        o = old_rows[i] if old_rows is not None and i < len(old_rows) else None
        cells = []
        for j, c in enumerate(r):
            if original is None:
                cells.append(f"<td>{_e(c)}</td>")
            elif o is None:
                cells.append(f"<td><ins>{_e(c)}</ins></td>" if c else "<td></td>")
            elif (o[j] if j < len(o) else "") == c:
                cells.append(f"<td>{_e(c)}</td>")
            else:
                cells.append(f"<td class='cc'>{_words_html(o[j] if j < len(o) else '', c)}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table>" + ("</div>" if wide else ""))
    return "".join(out)


def is_row_unit(text: str | None) -> bool:
    """A long table is stored one row per unit (no header line): such units are shown as one continuous table."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    t = parse_table(text)
    return bool(t) and len(lines) == 1 and len(t[1]) == 1


class _Runs:
    """Remembers the header of the previous one-row table unit, so consecutive rows share one header."""
    def __init__(self):
        self.prev = None

    def head(self, text: str | None) -> bool:
        if not is_row_unit(text):
            self.prev = None
            return True
        h = parse_table(text)[0]
        show = h != self.prev
        self.prev = h
        return show


TABLE_DOC_CSS = """
.goni-scroll table.dt{border-collapse:collapse;width:100%;margin:3px 0;font-size:13px;line-height:1.4;
  background:rgba(255,255,255,.55);table-layout:auto}
.goni-scroll .dt th,.goni-scroll .dt td{border:1px solid #d5d9de;padding:4px 7px;vertical-align:top;text-align:left;
  overflow-wrap:break-word;word-break:normal;hyphens:auto}
.goni-scroll .dtw{overflow-x:auto}.goni-scroll table.dt.wide{table-layout:fixed}
.goni-scroll .dt th{background:rgba(236,239,242,.9);font-weight:600;color:#333}
.goni-scroll .dt td.cc{background:rgba(207,227,246,.35)}
.goni-scroll .tb{background:transparent}
.goni-scroll .tb.cont{margin-top:-3px;padding-top:0}.goni-scroll .tb.cont table.dt{margin-top:0;border-top:none}
"""


# both halves of the analysis page use the SAME box, font and line spacing, so the two documents line up
DOC_HEIGHT = 840
DOC_CSS = """
.goni-scroll{box-sizing:border-box;height:840px;overflow:auto;border:1px solid #e3e3e3;border-radius:6px;
  padding:8px 12px;font-family:'Noto Sans Georgian',Arial,sans-serif;font-size:14px;line-height:1.5;background:#fff;
  color:#222;text-align:left}
.goni-scroll .h{font-weight:bold;margin:10px 0 2px}
.goni-scroll .u,.goni-scroll .cl,.goni-scroll .chg{position:relative;padding:3px 6px;margin:2px 0;
  border-left:4px solid transparent;border-radius:3px}
.goni-scroll .sn{color:#888;margin-right:6px}
.goni-scroll .t{background:#f6f6f6;white-space:pre-wrap}
.goni-scroll .bad{background:#fde8e6;border-color:#c0392b!important}
.goni-scroll .part{background:#fdf1e3;border-color:#e67e22!important}
.goni-scroll .anchor{background:#e7f1fa;border-color:#2980b9!important}
.goni-scroll .ok{background:#eaf6ee;border-color:#27ae60!important}
"""
DOC_CSS = DOC_CSS + TABLE_DOC_CSS
CSS = DOC_CSS + """
html,body{margin:0;padding:0;overflow:hidden;background:transparent}
.cds{display:none;position:absolute;z-index:10;left:24px;top:100%;width:min(560px,90%);background:#fff;
border:1px solid #bbb;box-shadow:0 4px 14px rgba(0,0,0,.25);padding:8px;font-size:12.5px;border-radius:6px}
.cl:hover>.cds{display:block}.cd+.cd{border-top:1px dashed #ccc;margin-top:6px;padding-top:6px}
.cdh{border-left:4px solid;padding-left:6px;margin-bottom:4px}.miss{color:#a04000}
"""


ROLES = ("contradicts", "anchor", "partial", "full")       # what a coloured clause can mean


FOCUS_CSS = """
@keyframes goniFocus{0%{box-shadow:0 0 0 0 rgba(142,68,173,.7)}100%{box-shadow:0 0 0 10px rgba(142,68,173,0)}}
.goni-scroll .focus{outline:3px solid #8e44ad;outline-offset:1px;animation:goniFocus 1.2s ease-out 3}
"""


def render_html(model: dict, roles=None, relevances=None, focus: str | None = None) -> str:
    """The document by sections; clauses with issues are coloured and show a hover card (CSS only).
    ``roles`` / ``relevances`` keep only those cards (None = all); a clause without kept cards is shown plain."""
    out = [f"<!doctype html><meta charset='utf-8'><style>{CSS}{FOCUS_CSS}</style><div class='goni-scroll'>"]
    runs = _Runs()
    for u0 in model["units"]:
        u = {**u0, "cards": [c for c in u0["cards"] if (roles is None or c["role"] in roles)
                             and (relevances is None or c.get("relevance") in relevances)]}
        text = _e(u["text"])
        if u["unit_type"] == "heading":
            runs.head(None)
            out.append(f"<div class='h' id='{_e(u['id'])}'>{text}</div>")
            continue
        sn = f"<span class='sn'>{_e(u['section_no'])}</span>" if u["unit_type"] == "clause" and u["section_no"] else ""
        head = runs.head(u["text"]) if u["unit_type"] == "table" else runs.head(None)
        tbl = table_html_of(u["text"], head=head) if u["unit_type"] == "table" else None
        if tbl:
            text = tbl
        cls = "cl" + (" tb" + ("" if head else " cont") if tbl else
                      " t" if u["unit_type"] in ("table", "metadata") else "")
        if u["cards"]:
            worst = ("contradicts" if any(c["role"] == "contradicts" for c in u["cards"])
                     else "partial" if any(c["role"] == "partial" for c in u["cards"])
                     else "anchor" if any(c["role"] == "anchor" for c in u["cards"]) else "full")
            cls += " " + CLASS_FOR[worst]
            hover = "".join(card_html(c) for c in u["cards"][:6])
            out.append(f"<div class='{cls}' id='{_e(u['id'])}'>{sn}{text}<div class='cds'>{hover}</div></div>")
        else:
            out.append(f"<div class='{cls}' id='{_e(u['id'])}'>{sn}{text}</div>")
    out.append("</div>")
    if focus:                                 # a task opened from the board: scroll to its clause and mark it
        out.append("<script>(function(){var el=document.getElementById(%s);if(!el)return;el.classList.add('focus');"
                   "var sc=document.querySelector('.goni-scroll');sc.scrollTop=el.offsetTop-sc.clientHeight/3;})();"
                   "</script>" % json.dumps(focus))
    return "".join(out)


def legend_html() -> str:
    items = [("bad", T["lg_contradicts"]), ("part", T["lg_partial"]), ("anchor", T["lg_anchor"]), ("ok", T["lg_ok"])]
    return "<style>" + CSS + "</style>" + " ".join(f"<span class='cl {c}' style='display:inline-block'>{_e(t)}</span>"
                                                  for c, t in items)


TABLE_CSS = """
body{font-family:Noto Sans Georgian,Arial,sans-serif;font-size:13px;margin:0}
table{border-collapse:collapse;width:100%;table-layout:fixed}
th,td{border-bottom:1px solid #e5e5e5;padding:6px 8px;text-align:left;vertical-align:top;white-space:normal;
      word-break:break-word;overflow-wrap:anywhere}
th{position:sticky;top:0;background:#f7f7f9;cursor:pointer;user-select:none;font-weight:600;border-bottom:2px solid #ddd}
th:hover{background:#eef3f8}th .arr{color:#999;font-size:10px;margin-left:4px}
tr:hover td{background:#fafcff}td.n{color:#888;text-align:right}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;color:#fff;font-size:11.5px;white-space:nowrap}
a{color:#1f6fb2;text-decoration:none}a:hover{text-decoration:underline}
"""
TABLE_JS = """
<script>
document.querySelectorAll('th').forEach(function(th, col){
  th.addEventListener('click', function(){
    var tb = th.closest('table').tBodies[0], rows = Array.from(tb.rows);
    var asc = th.dataset.asc !== '1'; th.dataset.asc = asc ? '1' : '0';
    document.querySelectorAll('th .arr').forEach(function(a){a.textContent='';});
    th.querySelector('.arr').textContent = asc ? '▲' : '▼';
    rows.sort(function(a, b){
      var x = a.cells[col].dataset.k || a.cells[col].innerText, y = b.cells[col].dataset.k || b.cells[col].innerText;
      var nx = parseFloat(x), ny = parseFloat(y);
      var r = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y, 'ka');
      return asc ? r : -r;
    });
    rows.forEach(function(r){tb.appendChild(r);});
  });
});
</script>
"""


def table_html(rows: list[dict], labels: dict, rel_label: dict, cov_label: dict, rel_color: dict, cov_color: dict) -> str:
    """The obligations table: row numbers, every column wraps, a click on a header sorts (CSS + a little JS)."""
    order_r = {"relevant": 0, "possibly": 1, "irrelevant": 2}
    order_v = {"contradicts": 0, "none": 1, "partial": 2, "undecided": 3, "full": 4}
    widths = ("4%", "27%", "10%", "11%", "30%", "18%")
    head = "".join(f"<th style='width:{w}'>{_e(lbl)}<span class='arr'></span></th>"
                   for w, lbl in zip(widths, labels.values()))
    body = []
    for i, r in enumerate(rows, 1):
        rel = r["relevance"]
        cov = r["verdict"] if rel != "irrelevant" else None
        rel_html = f"<span class='pill' style='background:{rel_color.get(rel, '#777')}'>{_e(rel_label[rel])}</span>"
        cov_html = (f"<span class='pill' style='background:{cov_color.get(cov, '#777')}'>{_e(cov_label[cov])}</span>"
                    if rel != "irrelevant" else "—")
        src = (f"<a href='{_e(r['url'])}' target='_blank' rel='noopener'>{_e(r['source'])} ↗</a>" if r.get("url")
               else _e(r["source"]))
        body.append(f"<tr><td class='n' data-k='{i}'>{i}</td><td>{_e(r['statement'])}</td>"
                    f"<td data-k='{order_r.get(rel, 9)}'>{rel_html}</td>"
                    f"<td data-k='{order_v.get(cov, 9)}'>{cov_html}</td><td>{_e(r['reason'])}</td><td>{src}</td></tr>")
    return (f"<style>{TABLE_CSS}</style><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
            + TABLE_JS)


ACTIVE_TASK = ("open", "accepted")
# the colour of a change on the right = the verdict of its obligation, as on the left (missing -> blue: new text)
ITEM_ROLE = {"contradicts": "contradicts", "partial": "partial", "none": "anchor", "undecided": "partial",
             "full": "full"}


def slot_shown(s: dict, roles=None, relevances=None) -> bool:
    """The document filters (what to show / relevance) applied to one change of the proposed version."""
    if roles is not None and s["role"] not in roles:
        return False
    return relevances is None or any(r in relevances for r in s["relevance"])


def _slot_status(items: list[dict]) -> str:
    st_ = {p["status"] for p in items}
    return "accepted" if st_ == {"accepted"} else "open" if st_ == {"open"} else "mixed"


def revised_view(model: dict) -> dict:
    """The document as it would read with the proposals applied (open and accepted ones; dismissed ones are left out).

    Every place that changes is ONE *slot*: a clause changed by any number of proposals (merged into one wording), an
    added section, or a new document. -> {"units": [{"unit", "change": slot | None, "inserts": [slot]}],
    "new_documents": [slot], "count", "accepted", "pending_merge": [clause_id]}
    slot = {"slot", "kind", "items": [proposal...], "oids", "pids", "status": open|accepted|mixed, "text",
            "segments": [(equal|ins|del, text, [n])], "source": None|merge|preview|user, "notes", "conflicts",
            "overlap", "heading"}; a proposal item = the proposals row + "oid", "status", "statement"."""
    from collections import defaultdict
    from kg.services.edits import diff_segments, merge_words, slot_of, valid_edit
    edits = model.get("edits") or {}
    adjust, insert, new_docs = defaultdict(list), defaultdict(list), []
    for oid, p in model["proposals"].items():
        t = model["tasks"].get(oid) or {}
        if t.get("status") not in ACTIVE_TASK:
            continue
        if p["kind"] == "new_document" and oid in (model.get("hidden_new") or set()):
            continue                                  # handled in another document: a duplicate, not shown
        item = {**p, "oid": oid, "status": t.get("status"), "priority": t.get("priority"),
                "statement": (model["cards"].get(oid) or {}).get("statement")}
        if p["kind"] == "adjust_clause" and p["clause_id"]:
            adjust[p["clause_id"]].append(item)
        elif p["kind"] == "add_section" and p["clause_id"]:
            insert[p["clause_id"]].append(item)
        else:
            new_docs.append(item)

    def slot(items: list[dict], original: str | None) -> dict:
        items = sorted(items, key=lambda p: p["id"])
        name = slot_of(items[0])
        pids = [p["id"] for p in items]
        e = valid_edit(edits, name, pids)
        roles = [ITEM_ROLE.get((model["cards"].get(p["oid"]) or {}).get("verdict"), "partial") for p in items]
        role = next((r for r in ("contradicts", "partial", "anchor", "full") if r in roles), "partial")
        if items[0]["kind"] != "adjust_clause":
            role = "anchor"                                   # a new section / document: blue
        out = {"slot": name, "kind": items[0]["kind"], "items": items, "oids": [p["oid"] for p in items],
               "role": role, "relevance": [(model["cards"].get(p["oid"]) or {}).get("relevance") for p in items],
               "pids": pids, "status": _slot_status(items), "heading": items[0].get("heading"), "source": None,
               "notes": {}, "conflicts": [], "overlap": False}
        if e:
            out.update(text=e["text"], source=e["source"], notes=e["notes"], conflicts=e["notes"].get("conflicts", []))
        elif len(items) == 1:
            out["text"] = items[0]["proposed_text"]
        else:
            m = merge_words(original or "", [(str(i), p["proposed_text"]) for i, p in enumerate(items, 1)])
            out.update(text=m["text"], segments=m["segments"], source="preview", overlap=m["overlap"])
        if "segments" not in out:
            out["segments"] = (diff_segments(original, out["text"]) if original is not None
                               else [("ins", out["text"], [])])
        return out

    units = []
    for u in model["units"]:
        ch = adjust.get(u["id"])
        units.append({"unit": u,
                      "change": slot(ch, ch[0].get("original_text") or u["text"]) if ch else None,
                      "inserts": [slot([p], None) for p in insert.get(u["id"], [])]})
    nds = [slot([p], None) for p in new_docs]
    slots = [x["change"] for x in units if x["change"]] + [s for x in units for s in x["inserts"]] + nds
    return {"units": units, "new_documents": nds, "count": len(slots),
            "accepted": sum(1 for s in slots if s["status"] == "accepted"),
            "pending_merge": [s["slot"].split(":", 1)[1] for s in slots if s["source"] == "preview"]}


# ------------------------------------------------------------------------------------------------ proposed version (right)
REVISED_CSS = DOC_CSS + FOCUS_CSS + """
.goni-scroll .chg.accepted{box-shadow:inset 0 0 0 1px #27ae60}
.goni-scroll .chg.add{border-left-style:double}
.chg ins{background:#cfe3f6;text-decoration:none}.chg del{color:#b03a2e;font-size:0.85em}
.chg sup.k{color:#8e44ad;font-size:10px;margin-left:1px}
.act{display:none;position:absolute;z-index:20;left:24px;top:100%;width:min(540px,92%);max-height:380px;overflow:auto;
  background:#fff;border:1px solid #bbb;box-shadow:0 4px 14px rgba(0,0,0,.22);padding:8px 10px;font-size:12.5px;
  border-radius:6px}
.chg:hover>.act{display:block}.chg.editing>.act{display:none!important}
.act .ob{margin-bottom:4px}.act .ex{color:#555;margin-bottom:6px}.act ol{margin:2px 0 6px 18px;padding:0}
.act li{margin-bottom:5px}.act .warn{color:#a04000;margin-bottom:6px}.act .src{color:#888;margin-bottom:4px}
.goni-scroll button{border:1px solid #ccc;background:#fff;border-radius:6px;padding:3px 10px;margin:2px 6px 0 0;
  cursor:pointer;font-size:12.5px;font-family:inherit}
.goni-scroll button.ok{background:#27ae60;color:#fff;border-color:#27ae60}
.goni-scroll button.no{background:#c0392b;color:#fff;border-color:#c0392b}.goni-scroll button:hover{filter:brightness(.95)}
.st{font-size:11px;color:#27ae60;margin-left:6px}
.edbar{display:none;margin-top:4px}.chg.editing>.edbar{display:block}
.chg.editing .txt{display:block;outline:2px solid #2980b9;background:#fff;padding:4px 6px;border-radius:4px;
  white-space:pre-wrap;cursor:text}
.nd{margin-top:14px;font-weight:bold}
.goni-scroll.free{height:auto;max-height:700px}
"""

REVISED_JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  let root = parentElement.querySelector('.goni-root');
  let top = 0;
  if (root) { const sc = root.querySelector('.goni-scroll'); if (sc) top = sc.scrollTop; }
  else { root = document.createElement('div'); root.className = 'goni-root'; parentElement.appendChild(root); }
  root.innerHTML = data || '';
  const sc = root.querySelector('.goni-scroll'); if (sc) sc.scrollTop = top;
  const mem = parentElement.__goni || (parentElement.__goni = {});
  const f = sc && sc.dataset.focus;                 // a task opened from the board: scroll to it once
  if (f && mem.focus !== f) {
    const el = root.querySelector('.focus');
    if (el) {
      sc.scrollTop = el.getBoundingClientRect().top - sc.getBoundingClientRect().top + sc.scrollTop - sc.clientHeight / 3;
    }
  }
  mem.focus = f;
  function startEdit(box) {
    const t = box.querySelector('.txt');
    box.dataset.html = t.innerHTML;
    t.textContent = t.dataset.plain || t.innerText;
    t.contentEditable = 'plaintext-only';
    if (t.contentEditable !== 'plaintext-only') t.contentEditable = 'true';
    box.classList.add('editing');
    t.focus();
  }
  function stopEdit(box) {
    const t = box.querySelector('.txt');
    t.contentEditable = 'false';
    t.innerHTML = box.dataset.html || t.innerHTML;
    box.classList.remove('editing');
  }
  root.querySelectorAll('[data-action]').forEach(function (b) {
    b.onclick = function (e) {
      e.stopPropagation();
      const box = b.closest('.chg');
      const a = b.dataset.action;
      if (a === 'edit') { startEdit(box); return; }
      if (a === 'cancel') { stopEdit(box); return; }
      const msg = { action: a, oids: box.dataset.oids, slot: box.dataset.slot, t: Date.now() };
      if (a === 'save') msg.text = box.querySelector('.txt').innerText;
      setTriggerValue('action', msg);
    };
  });
}
"""


def _words_html(original: str, proposed: str) -> str:
    """The NEW text, with the inserted words highlighted and the removed words shown small and struck through."""
    from kg.services.edits import diff_segments
    return _segments_html(diff_segments(original, proposed), False)


def _segments_html(segs, numbered: bool) -> str:
    out = []
    for kind, text, keys in segs:
        if kind == "equal":
            out.append(_e(text))
        else:
            mark = f"<sup class='k'>{_e(','.join(keys))}</sup>" if numbered and keys else ""
            out.append(f"<{kind}>{_e(text)}</{kind}>{mark}")
    return " ".join(out)


def revised_html(model: dict, labels: dict, can_edit: bool, roles=None, relevances=None, part: str = "doc",
                 focus: str | None = None) -> str:
    """The document as it reads with the open and accepted proposals: ONE block per changed clause (several
    proposals for a clause are merged); hovering a block shows why each obligation changed it and the buttons
    (accept / reject / edit / details, or undo once accepted). "Edit" makes the text editable in place.
    The colour of a change is the verdict of its obligation (red / orange as on the left, blue = new text);
    ``roles`` / ``relevances`` are the document filters. ``part`` = "doc" (the document) or "new" (only the
    proposals for new documents, shown in their own tab)."""
    rv = revised_view(model)

    def block(s: dict, sn: str = "", table_original: str | None = None) -> str:
        multi = len(s["items"]) > 1
        cls = "chg " + CLASS_FOR[s["role"]] + {"accepted": " accepted", "mixed": " mixed"}.get(s["status"], "") + \
            (" add" if s["kind"] != "adjust_clause" else "")
        head = f"<b>{_e(s['heading'])}</b><br>" if s.get("heading") and s["kind"] != "adjust_clause" else ""
        body = (table_html_of(s["text"], table_original) if table_original is not None else None) or \
            _segments_html(s["segments"], multi and s["source"] == "preview")
        txt = f"<span class='txt' data-plain='{_e(s['text'])}'>{body}</span>"
        st_ = f"<span class='st'>✓ {_e(labels['accepted'])}</span>" if s["status"] == "accepted" else ""
        # why: one line per obligation (what it changed), then warnings
        if multi:
            lis = "".join(f"<li><b>{_e(p.get('statement'))}</b><br>"
                          f"{_e(s['notes'].get(str(p['id'])) or p.get('explanation'))}"
                          + (" ✓" if p["status"] == "accepted" else "") + "</li>" for p in s["items"])
            why = (f"<div class='ob'>{_e(labels['merged_from'].format(n=len(s['items'])))}</div><ol>{lis}</ol>")
        else:
            p = s["items"][0]
            why = (f"<div class='ob'><b>{_e(labels['obligation'])}:</b> {_e(p.get('statement'))}</div>"
                   f"<div class='ex'>{_e(p.get('explanation'))}</div>")
        src = {"user": labels["user_edited"], "merge": labels["model_merged"]}.get(s["source"])
        if src:
            why = f"<div class='src'>✎ {_e(src)}</div>" + why
        warns = list(s.get("conflicts") or [])
        if s["source"] == "preview":
            warns.insert(0, labels["overlap_warn"] if s["overlap"] else labels["preview_merge"])
        why += "".join(f"<div class='warn'>⚠ {_e(w)}</div>" for w in warns)
        if can_edit:
            if s["status"] == "accepted":
                btns = f"<button data-action='undo'>↺ {_e(labels['undo'])}</button>"
            else:
                btns = (f"<button class='ok' data-action='accept'>✓ {_e(labels['accept'])}</button>"
                        f"<button class='no' data-action='reject'>✕ {_e(labels['reject'])}</button>")
            btns += f"<button data-action='edit'>✎ {_e(labels['edit'])}</button>"
        else:
            btns = ""
        btns += f"<button data-action='details'>ⓘ {_e(labels['details'])}</button>"
        edbar = (f"<div class='edbar'><button class='ok' data-action='save'>✓ {_e(labels['save_accept'])}</button>"
                 f"<button data-action='cancel'>{_e(labels['cancel'])}</button></div>") if can_edit else ""
        if focus and s["slot"] == focus:
            cls += " focus"
        return (f"<div class='{cls}' data-oids='{_e('|'.join(s['oids']))}' data-slot='{_e(s['slot'])}'>"
                f"{sn}{head}{txt}{st_}<div class='act'>{why}{btns}</div>{edbar}</div>")

    def shown(s):
        return slot_shown(s, roles, relevances)

    if part == "new":
        items = [block(s) for s in rv["new_documents"] if shown(s)]
        return ("<div class='goni-scroll free'" + (f" data-focus='{_e(focus)}'" if focus else "") + ">"
                + "".join(items) + "</div>") if items else ""
    out = ["<div class='goni-scroll'" + (f" data-focus='{_e(focus)}'" if focus else "") + ">"]
    runs = _Runs()
    for x in rv["units"]:
        u = x["unit"]
        sn = f"<span class='sn'>{_e(u['section_no'])}</span>" if u["unit_type"] == "clause" and u["section_no"] else ""
        if x["change"] and shown(x["change"]):
            runs.head(None)
            out.append(block(x["change"], sn, u["text"] if u["unit_type"] == "table" else None))
        elif u["unit_type"] == "heading":
            runs.head(None)
            out.append(f"<div class='h'>{_e(u['text'])}</div>")
        else:
            head = runs.head(u["text"] if u["unit_type"] == "table" else None)
            tbl = table_html_of(u["text"], head=head) if u["unit_type"] == "table" else None
            cls = "u" + (" tb" + ("" if head else " cont") if tbl else
                         " t" if u["unit_type"] in ("table", "metadata") else "")
            out.append(f"<div class='{cls}'>{sn}{tbl or _e(u['text'])}</div>")
        for s in x["inserts"]:
            if shown(s):
                out.append(block(s))
    out.append("</div>")
    return "".join(out)
