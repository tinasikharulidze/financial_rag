"""Impact reports written by ``replay`` (data/kg/reports/<run_id>.md|json)."""
from __future__ import annotations

import json
from pathlib import Path

from kg.ids import display_path
from kg.mapping.labels import KA, sort_rank

ACTION_KA = {"ADJUST_SECTION": "არსებული პუნქტის შესწორება", "ADD_SECTION": "ახალი პუნქტის დამატება",
             "NEW_DOCUMENT": "ახალი დოკუმენტის შექმნა"}


def reports_dir(cfg) -> Path:
    d = cfg.path("reports")
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_reports(cfg) -> list[dict]:
    out = []
    for p in sorted(reports_dir(cfg).glob("*.json"), reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"run_id": p.stem, "act": d.get("act"), "amendment": d.get("amendment"),
                        "date": d.get("effective_date"), "findings": len(d.get("affected", []))})
        except Exception:  # noqa: BLE001
            continue
    return out


def get(cfg, run_id: str) -> dict | None:
    p = reports_dir(cfg) / f"{run_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def render_markdown(rep: dict) -> str:
    L = [f"# Impact report: amendment {rep['amendment']} to act {rep['act']}",
         f"Effective date: **{rep.get('effective_date')}** · run `{rep['run_id']}`", ""]
    L += ["## Change summary", f"- Provisions changed: {len(rep['changes'])}",
          f"- New obligations: {len(rep['new_obligations'])}, changed: {len(rep['changed_obligations'])}, "
          f"removed: {len(rep['removed_obligations'])}", ""]
    for c in rep["changes"]:
        L.append(f"- `{c['path']}` ({display_path(c['path'])}): {c['kind']}")
    for title, key in (("New obligations", "new_obligations"), ("Changed obligations", "changed_obligations"),
                       ("Removed obligations", "removed_obligations")):
        L += ["", f"## {title}"]
        L += [f"- [{o['severity']}] {display_path(o['source_path'])}: {o['statement']}" for o in rep[key]] or ["- none"]
    L += ["", "## Affected documents and clauses"]
    if not rep["affected"]:
        L.append("- none")
    for f in sorted(rep["affected"], key=lambda f: -sort_rank(f["priority"])):
        L += [f"- **{f['priority']}** · {f['doc_id']} · clause `{f.get('clause_id')}` · {f['action_type']} · "
              f"chance {f['issue_chance']} · severity {f['severity']} · class {f['gap_class']}",
              f"  - {f.get('rationale') or ''}", f"  - Suggested fix: {f.get('suggested_fix') or '-'}"]
    L += ["", "## New alerts"]
    L += [f"- {a['message']}" for a in rep["new_alerts"]] or ["- none"]
    return "\n".join(L) + "\n"


def save(cfg, rep: dict) -> tuple[Path, Path]:
    d = reports_dir(cfg)
    j, m = d / f"{rep['run_id']}.json", d / f"{rep['run_id']}.md"
    j.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    m.write_text(render_markdown(rep), encoding="utf-8")
    return j, m
