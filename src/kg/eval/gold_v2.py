"""Gold templates for the extraction suite (docs/KG_V2_PLAN.md section 15.1) and their loaders.

``make_templates`` writes two CSV files with ~20 pre-filled UNITS of the main act; the team fills the obligation
columns by hand (one row per obligation of the unit; a unit without obligations keeps one row with ``statement`` empty
and ``no_obligations=1``). Nothing is overwritten once a file exists.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from kg.extract.obligations import build_units
from kg.ids import path_sort_key

OBL_COLS = ["unit_path", "unit_date", "unit_text", "no_obligations", "statement", "deontic", "subject", "applies_to",
            "condition", "exceptions", "deadline_or_frequency", "severity", "required_doc_types",
            "required_doc_basis", "required_doc_quote", "evidence_type", "location_hint", "depends_on", "source_path"]
DIFF_COLS = ["unit_path", "amendment_date", "old_text", "new_text", "old_statement", "new_statement",
             "expected_op"]                       # expected_op: unchanged | modified | new | removed
_FEATURES = (("definition", re.compile(r"განმარტ|ტერმინ|განსაზღვრ")), ("exception", re.compile(r"გარდა|გამონაკლის")),
             ("reference", re.compile(r"მუხლის|პუნქტის|ქვეპუნქტ")), ("subpoints", re.compile(r"(?m)^\s*[ა-ჰ]\)")),
             ("deadline", re.compile(r"დღის ვადაში|ყოველწლიურად|კვარტალ|ვადაში")))


def gold_paths(cfg) -> tuple[Path, Path]:
    d = cfg.path("eval_gold")
    return d / "obligations_v2.csv", d / "amend_diff_v1.csv"


def pick_units(cfg, store, act_doc: str, n: int = 20) -> tuple[list, list]:
    """(units for the extraction gold, (old, new) pairs for the diff gold). Deterministic."""
    units = build_units(store, act_doc)
    by_path: dict[str, list] = {}
    for u in sorted(units, key=lambda x: (path_sort_key(x.unit_path), x.date)):
        by_path.setdefault(u.unit_path, []).append(u)
    changed = [(vs[i - 1], vs[i]) for vs in by_path.values() for i in range(1, len(vs))][:6]
    chosen, seen = [], set()
    for _old, new in changed:                                      # the units changed by amendments come first
        chosen.append(new)
        seen.add(new.unit_path)
    for _feat, rx in _FEATURES:                                    # one unit per feature
        for vs in by_path.values():
            u = vs[0]
            if len(chosen) >= n or u.unit_path in seen or not rx.search(u.text_for_llm):
                continue
            chosen.append(u)
            seen.add(u.unit_path)
            break
    for vs in by_path.values():                                    # fill up with the first units not yet chosen
        if len(chosen) >= n:
            break
        if vs[0].unit_path not in seen:
            chosen.append(vs[0])
            seen.add(vs[0].unit_path)
    return chosen, changed


def make_templates(cfg, store, act_doc: str | None = None) -> dict:
    act_doc = str(act_doc or cfg["obligation_acts"][0])
    obl_f, diff_f = gold_paths(cfg)
    obl_f.parent.mkdir(parents=True, exist_ok=True)
    chosen, changed = pick_units(cfg, store, act_doc)
    out = {"obligations_v2": str(obl_f), "amend_diff_v1": str(diff_f), "written": []}
    if not obl_f.exists():
        with open(obl_f, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=OBL_COLS)
            w.writeheader()
            for u in chosen:
                w.writerow({"unit_path": u.unit_path, "unit_date": u.date, "unit_text": u.text_for_llm})
        out["written"].append("obligations_v2")
    if not diff_f.exists():
        with open(diff_f, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=DIFF_COLS)
            w.writeheader()
            for old, new in changed:
                w.writerow({"unit_path": new.unit_path, "amendment_date": new.date, "old_text": old.text_for_llm,
                            "new_text": new.text_for_llm})
        out["written"].append("amend_diff_v1")
    out["units"] = len(chosen)
    out["diff_units"] = len(changed)
    return out


def load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))
