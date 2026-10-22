"""Extraction suite: obligations of the graph against ``evaluation/gold/obligations_v2.csv`` and ``amend_diff_v1.csv``.

Statements are matched by word-stem overlap (a cheap, deterministic stand-in for the judge prompt of the plan; the
threshold ``MATCH`` can be tuned; unmatched rows are listed so a person can overrule). Reported:
recall / precision per labelled unit, attribute accuracy on matched pairs (deontic, severity, required-document
basis, evidence type), precision of ``explicit`` requirements, and diff-operation accuracy.
"""
from __future__ import annotations

import re
from collections import defaultdict

from kg.eval.gold_v2 import gold_paths, load_csv

MATCH = 0.4
_W = re.compile(r"\w+", re.U)


def stems(text: str | None, n: int = 5) -> set[str]:
    return {w[:n] for w in _W.findall((text or "").lower()) if len(w) > 2}


def similar(a: str | None, b: str | None) -> float:
    x, y = stems(a), stems(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def pair_up(gold: list[str], pred: list[str], threshold: float = MATCH) -> list[tuple[int, int, float]]:
    """Greedy one-to-one matching by descending similarity."""
    cand = sorted(((similar(g, p), i, j) for i, g in enumerate(gold) for j, p in enumerate(pred)), reverse=True)
    used_g, used_p, out = set(), set(), []
    for s, i, j in cand:
        if s < threshold:
            break
        if i in used_g or j in used_p:
            continue
        used_g.add(i)
        used_p.add(j)
        out.append((i, j, s))
    return out


def _norm(v) -> str:
    return (v or "").strip().lower()


def _pred_basis(p: dict) -> str:
    rd = p.get("required_doc") or {}
    return "none" if not rd.get("doc_types") else _norm(rd.get("basis")) or "inferred"


def _of_unit(store, unit_path: str) -> list[dict]:
    return [n for n in store.nodes("Obligation") if n["props"].get("unit") == unit_path]


def evaluate_obligations(store, rows: list[dict]) -> tuple[dict, list[dict]]:
    labelled = defaultdict(list)
    for r in rows:
        if _norm(r.get("statement")) or _norm(r.get("no_obligations")) in ("1", "true", "yes"):
            labelled[r["unit_path"]].append(r)
    if not labelled:
        return {"status": "no gold labels yet", "labelled_units": 0}, []
    n_gold = n_pred = n_match = 0
    attr = defaultdict(lambda: [0, 0])
    explicit = [0, 0]
    items = []
    for up, grows in sorted(labelled.items()):
        g = [r for r in grows if _norm(r.get("statement"))]
        preds = [n for n in _of_unit(store, up) if n["valid_to"] is None]
        pairs = pair_up([r["statement"] for r in g], [n["props"].get("statement") for n in preds])
        n_gold += len(g)
        n_pred += len(preds)
        n_match += len(pairs)
        for i, j, _s in pairs:
            p, r = preds[j]["props"], g[i]
            for name, gv, pv in (("deontic", _norm(r.get("deontic")), _norm(p.get("deontic"))),
                                 ("severity", _norm(r.get("severity")), _norm(p.get("severity"))),
                                 ("required_doc_basis", _norm(r.get("required_doc_basis")) or "none", _pred_basis(p)),
                                 ("evidence_type", _norm(r.get("evidence_type")), _norm(p.get("evidence_type")))):
                if gv:
                    attr[name][1] += 1
                    attr[name][0] += int(gv == pv)
            if _pred_basis(p) == "explicit":
                explicit[1] += 1
                explicit[0] += int((_norm(r.get("required_doc_basis")) or "none") == "explicit")
        items.append({"unit_path": up, "gold": len(g), "predicted": len(preds), "matched": len(pairs),
                      "recall": (len(pairs) / len(g)) if g else None,
                      "precision": (len(pairs) / len(preds)) if preds else None})

    def div(a, b):
        return (a / b) if b else None
    return {"labelled_units": len(labelled), "gold_obligations": n_gold, "predicted_obligations": n_pred,
            "matched": n_match, "recall": div(n_match, n_gold), "precision": div(n_match, n_pred),
            "attribute_accuracy": {k: div(a, b) for k, (a, b) in attr.items()},
            "explicit_precision": div(*explicit)}, items


def predicted_op(store, old_statement: str, unit_path: str, superseded: set[str]) -> str | None:
    """The diff operation the graph recorded for an old obligation of a unit."""
    best, score = None, 0.0
    for n in _of_unit(store, unit_path):
        s = similar(old_statement, n["props"].get("statement"))
        if s > score:
            best, score = n, s
    if best is None or score < MATCH:
        return None
    if best["valid_to"] is None:
        return "unchanged"
    return "modified" if best["id"] in superseded else "removed"


def evaluate_diff(store, rows: list[dict]) -> tuple[dict, list[dict]]:
    rows = [r for r in rows if _norm(r.get("expected_op"))]
    if not rows:
        return {"status": "no gold labels yet", "labelled": 0}, []
    edges = store.edges("SUPERSEDES")
    superseded, replacing = {e["dst"] for e in edges}, {e["src"] for e in edges}
    ok, items = 0, []
    for r in rows:
        exp = _norm(r["expected_op"])
        if exp == "new":
            news = [n for n in _of_unit(store, r["unit_path"]) if n["props"].get("unit_date") == r.get("amendment_date")
                    and n["id"] not in replacing]
            got = "new" if any(similar(r.get("new_statement"), n["props"].get("statement")) >= MATCH
                               for n in news) else None
        else:
            got = predicted_op(store, r.get("old_statement"), r["unit_path"], superseded)
        ok += int(got == exp)
        items.append({"unit_path": r["unit_path"], "date": r.get("amendment_date"), "expected": exp, "got": got})
    return {"labelled": len(rows), "accuracy": ok / len(rows)}, items


def run_extraction(cfg, store) -> dict:
    obl_f, diff_f = gold_paths(cfg)
    m1, i1 = evaluate_obligations(store, load_csv(obl_f))
    m2, i2 = evaluate_diff(store, load_csv(diff_f))
    return {"metrics": {"obligations": m1, "diff": m2, "gold_files": [str(obl_f), str(diff_f)],
                        "graph": {"obligations": len(store.nodes("Obligation")),
                                  "failed_units": sum(1 for n in store.nodes("ExtractionUnit")
                                                      if n["props"].get("status") == "failed")}},
            "items": [{"part": "obligations", **x} for x in i1] + [{"part": "diff", **x} for x in i2]}
