"""The task board as a Jira-like table: direction -> document -> tasks, each group collapsible, columns sortable per
document, a click on a row opens the task window (an HTML component; the click goes back to Python)."""
from __future__ import annotations

import html
from collections import defaultdict

from i18n_lang import localize

PRIO_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
COV_ORDER = {"contradicts": 0, "none": 1, "undecided": 2, "partial": 3, "full": 4}
REL_ORDER = {"relevant": 0, "possibly": 1}
STATUS_ORDER = {"open": 0, "accepted": 1, "not_relevant": 2, "already_addressed": 3, "resolved": 4, "replaced": 5}

# Jira-like priority icons (chevrons)
_UP2 = ("<svg width='14' height='14' viewBox='0 0 16 16'><path d='M3 9l5-5 5 5' fill='none' stroke='{c}' "
        "stroke-width='2'/><path d='M3 13l5-5 5 5' fill='none' stroke='{c}' stroke-width='2'/></svg>")
_UP = "<svg width='14' height='14' viewBox='0 0 16 16'><path d='M3 11l5-5 5 5' fill='none' stroke='{c}' stroke-width='2.2'/></svg>"
_EQ = ("<svg width='14' height='14' viewBox='0 0 16 16'><path d='M3 6h10M3 10h10' fill='none' stroke='{c}' "
       "stroke-width='2.2'/></svg>")
_DOWN = "<svg width='14' height='14' viewBox='0 0 16 16'><path d='M3 5l5 5 5-5' fill='none' stroke='{c}' stroke-width='2.2'/></svg>"
PRIO_ICON = {"critical": _UP2.format(c="#cd1317"), "high": _UP.format(c="#e9494a"), "medium": _EQ.format(c="#e97f33"),
             "low": _DOWN.format(c="#2d8738")}
COV_COLOR = {"contradicts": "#c0392b", "none": "#2980b9", "undecided": "#7f8c8d", "partial": "#e67e22",
             "full": "#27ae60"}
REL_COLOR = {"relevant": "#2c3e50", "possibly": "#95a5a6"}
STATUS_COLOR = {"open": "#42526e", "accepted": "#0065ff", "not_relevant": "#97a0af", "already_addressed": "#00875a",
                "resolved": "#00875a", "replaced": "#97a0af"}


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def group(rows: list[dict], doc_info: dict[str, dict]) -> list[dict]:
    """[{"direction", "docs": [{"doc_version_id", "label", "title", "rows": [...]}]}] in a stable order; a document
    belongs to the first domain of its profile (``doc_info[v]["direction"]``)."""
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        d = (doc_info.get(r["doc_version_id"]) or {}).get("direction") or "other"
        by[d][r["doc_version_id"]].append(r)
    out = []
    for d in sorted(by, key=lambda x: (x == "other", x)):
        docs = []
        for v in sorted(by[d], key=lambda v: (doc_info.get(v) or {}).get("label") or v):
            rs = sorted(by[d][v], key=lambda r: (PRIO_ORDER.get(r["priority"], 9), COV_ORDER.get(r["verdict"], 9),
                                                 r["title"]))
            docs.append({"doc_version_id": v, "label": (doc_info.get(v) or {}).get("label") or v,
                         "title": (doc_info.get(v) or {}).get("title") or "", "rows": rs})
        out.append({"direction": d, "docs": docs})
    return out


def _counts(rows: list[dict], L: dict) -> str:
    n = defaultdict(int)
    for r in rows:
        n[r["priority"]] += 1
    return "".join(f"<span class='cnt'>{PRIO_ICON[p]}{n[p]}</span>" for p in PRIO_ORDER if n.get(p))


def pill(text: str, color: str, outline: bool = False) -> str:
    if outline:
        return f"<span class='pill o' style='color:{color};border-color:{color}'>{_e(text)}</span>"
    return f"<span class='pill' style='background:{color}'>{_e(text)}</span>"


def _irrelevant_html(direction: str, items: list[dict], L: dict) -> str:
    """The extra group under a direction: obligations judged irrelevant for every document of the direction."""
    rid = "irr:" + direction
    if items:
        body = "".join(f"<tr class='ro'><td class='n'>{i}</td><td>{_e(x['statement'])}</td><td>"
                       + (f"<a href='{_e(x['url'])}' target='_blank' rel='noopener'>{_e(x['source'])} ↗</a>"
                          if x.get("url") else _e(x.get("source") or "")) + "</td></tr>"
                       for i, x in enumerate(items, 1))
        inner = (f"<table class='irr'><thead><tr><th style='width:5%'>#</th><th style='width:70%'>{_e(L['obligation'])}"
                 f"</th><th>{_e(L['source'])}</th></tr></thead><tbody>{body}</tbody></table>")
    else:
        inner = f"<div class='none'>{_e(L['none_found'])}</div>"
    return (f"<details class='doc irr' data-id='{_e(rid)}'><summary><span class='car'></span>"
            f"<b>{_e(L['irr_everywhere'])}</b><span class='meta'>{len(items)}</span></summary>{inner}</details>")


def board_html(groups: list[dict], L: dict, dir_label: dict, users: dict[int, str],
               irrelevant: dict[str, list[dict]] | None = None) -> str:
    """``L`` = labels (columns, tasks, docs, new, prio/cov/rel/status/cause tables). ``irrelevant``: per direction,
    the obligations not relevant for any of its documents (an extra group at the end of the direction)."""
    out = ["<div class='jb'>"]
    for g in groups:
        rows_all = [r for d in g["docs"] for r in d["rows"]]
        gid = "dir:" + g["direction"]
        out.append(f"<details class='dir' data-id='{_e(gid)}'><summary><span class='car'></span>"
                   f"<b>{_e(dir_label.get(g['direction'], g['direction']))}</b>"
                   f"<span class='meta'>{len(g['docs'])} {_e(L['docs'])} · {len(rows_all)} {_e(L['tasks'])}</span>"
                   f"{_counts(rows_all, L)}</summary>")
        for d in g["docs"]:
            did = "doc:" + d["doc_version_id"]
            out.append(f"<details class='doc' data-id='{_e(did)}'><summary><span class='car'></span>"
                       f"<b>{_e(d['label'])}</b> <span class='dt'>{_e(d['title'])}</span>"
                       f"<a class='lnk' data-vid='{_e(d['doc_version_id'])}'>↗ {_e(L['open_analysis'])}</a>"
                       f"<span class='meta'>{len(d['rows'])} {_e(L['tasks'])}</span>{_counts(d['rows'], L)}</summary>")
            head = "".join(f"<th data-col='{i}' style='width:{w}'>{_e(t)}<span class='arr'></span></th>"
                           for i, (t, w) in enumerate(zip(L["columns"], ("11%", "37%", "11%", "11%", "11%", "9%",
                                                                         "10%"))))
            body = []
            for r in d["rows"]:
                p, v, rel, stt = r["priority"], r["verdict"], r["relevance"], r["status"]
                cause = L["cause"].get(r["cause"], r["cause"] or "")
                if r.get("is_new"):
                    cause += f" <span class='new'>{_e(L['new'])}</span>"
                body.append(
                    f"<tr data-key='{_e(r['key'])}'>"
                    f"<td data-k='{PRIO_ORDER.get(p, 9)}'><span class='pr'>{PRIO_ICON.get(p, '')}"
                    f"{_e(L['prio'].get(p, p))}</span></td>"
                    f"<td class='sum'>{_e(localize(r['title']))}</td>"
                    f"<td data-k='{COV_ORDER.get(v, 9)}'>{pill(L['cov'].get(v, v or '—'), COV_COLOR.get(v, '#777'))}</td>"
                    f"<td data-k='{REL_ORDER.get(rel, 9)}'>"
                    f"{pill(L['rel'].get(rel, rel or '—'), REL_COLOR.get(rel, '#777'), outline=True)}</td>"
                    f"<td>{cause}</td>"
                    f"<td data-k='{STATUS_ORDER.get(stt, 9)}'>"
                    f"<span class='st' style='color:{STATUS_COLOR.get(stt, '#42526e')}'>"
                    f"{_e(L['status'].get(stt, stt))}</span></td>"
                    f"<td>{_e(users.get(r.get('assignee_user'), '—'))}</td></tr>")
            out.append(f"<table data-id='{_e(did)}'><thead><tr>{head}</tr></thead><tbody>{''.join(body)}"
                       f"</tbody></table></details>")
        if not g["docs"]:
            out.append(f"<div class='none'>{_e(L['no_tasks_direction'])}</div>")
        elif irrelevant is not None:
            out.append(_irrelevant_html(g["direction"], irrelevant.get(g["direction"], []), L))
        out.append("</details>")
    out.append("</div>")
    return "".join(out)


BOARD_CSS = """
.jb{font-family:'Noto Sans Georgian',-apple-system,'Segoe UI',Arial,sans-serif;font-size:13.5px;color:#172b4d}
.jb details{border:1px solid #dfe1e6;border-radius:6px;margin:6px 0;background:#fff}
.jb details.doc{margin:6px 10px 8px 22px;border-color:#ebecf0}
.jb summary{list-style:none;cursor:pointer;padding:8px 10px;display:flex;align-items:center;gap:10px;user-select:none}
.jb summary::-webkit-details-marker{display:none}
.jb details.dir>summary{background:#f4f5f7;border-radius:6px;font-size:14.5px}
.jb summary:hover{background:#ebecf0}
.jb .car{display:inline-block;width:0;height:0;border-left:6px solid #5e6c84;border-top:5px solid transparent;
  border-bottom:5px solid transparent;transition:transform .12s}
.jb details[open]>summary .car{transform:rotate(90deg)}
.jb .meta{color:#6b778c;font-size:12.5px}.jb .dt{color:#42526e;font-weight:normal;max-width:45%;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.jb .cnt{display:inline-flex;align-items:center;gap:2px;color:#42526e;font-size:12.5px;margin-left:4px}
.jb table{border-collapse:collapse;width:100%;table-layout:fixed}
.jb th{background:#fafbfc;color:#5e6c84;font-weight:600;font-size:12px;text-align:left;padding:7px 8px;
  border-bottom:2px solid #dfe1e6;cursor:pointer;user-select:none;white-space:nowrap;overflow:hidden}
.jb th:hover{background:#ebecf0}.jb th .arr{font-size:9px;margin-left:4px;color:#97a0af}
.jb td{padding:7px 8px;border-bottom:1px solid #ebecf0;vertical-align:top;overflow-wrap:anywhere}
.jb tbody tr{cursor:pointer}.jb tbody tr:hover td{background:#f4f8ff}
.jb td.sum{color:#0052cc}
.jb .pr{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.jb .pill{display:inline-block;padding:1px 8px;border-radius:3px;color:#fff;font-size:11.5px;font-weight:600;
  white-space:nowrap}
.jb .pill.o{background:#fff;border:1px solid;padding:0 7px}
.jb .st{font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.2px}
.jb details.irr>summary b{color:#5e6c84}.jb .none{padding:8px 14px 12px;color:#6b778c;font-style:italic}
.jb tr.ro{cursor:default}.jb td.n{color:#97a0af;text-align:right}
.jb .lnk{color:#0052cc;font-size:12.5px;cursor:pointer;text-decoration:none;white-space:nowrap}
.jb .lnk:hover{text-decoration:underline}
.jb .new{background:#ffab00;color:#172b4d;border-radius:3px;padding:0 5px;font-size:10.5px;font-weight:700}
"""

BOARD_JS = """
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const mem = parentElement.__goni || (parentElement.__goni = { open: new Set(), sort: {} });
  let root = parentElement.querySelector('.goni-board');
  if (!root) { root = document.createElement('div'); root.className = 'goni-board'; parentElement.appendChild(root); }
  root.innerHTML = data || '';
  function sortTable(tb, col, asc) {
    const body = tb.tBodies[0]; const rows = Array.from(body.rows);
    rows.sort(function (a, b) {
      const x = a.cells[col].dataset.k || a.cells[col].innerText, y = b.cells[col].dataset.k || b.cells[col].innerText;
      const nx = parseFloat(x), ny = parseFloat(y);
      const r = (!isNaN(nx) && !isNaN(ny)) ? nx - ny : x.localeCompare(y, 'ka');
      return asc ? r : -r;
    });
    rows.forEach(function (r) { body.appendChild(r); });
    tb.querySelectorAll('th .arr').forEach(function (a) { a.textContent = ''; });
    tb.querySelector('th[data-col="' + col + '"] .arr').textContent = asc ? '▲' : '▼';
  }
  root.querySelectorAll('details').forEach(function (d) {
    if (mem.open.has(d.dataset.id)) d.open = true;
    d.addEventListener('toggle', function () { if (d.open) mem.open.add(d.dataset.id); else mem.open.delete(d.dataset.id); });
  });
  root.querySelectorAll('table').forEach(function (tb) {
    const s = mem.sort[tb.dataset.id]; if (s) sortTable(tb, s[0], s[1]);
    tb.querySelectorAll('th').forEach(function (th) {
      th.onclick = function () {
        const col = parseInt(th.dataset.col); const cur = mem.sort[tb.dataset.id];
        const asc = !(cur && cur[0] === col && cur[1]);
        mem.sort[tb.dataset.id] = [col, asc]; sortTable(tb, col, asc);
      };
    });
  });
  root.querySelectorAll('a.lnk').forEach(function (a) {
    a.onclick = function (e) {
      e.preventDefault(); e.stopPropagation();
      setTriggerValue('goto', { vid: a.dataset.vid, t: Date.now() });
    };
  });
  root.querySelectorAll('tbody tr[data-key]').forEach(function (tr) {
    tr.onclick = function () { setTriggerValue('open', { key: tr.dataset.key, t: Date.now() }); };
  });
}
"""
