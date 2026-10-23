"""Compliance suite: findings vs the gold labels (answer_key_gold_labels.csv)."""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict

from kg.eval.matching import match, parse_gold_articles, section_tokens
from kg.mapping.coverage import COMPANY

ART_LEVELS = ("critical", "high", "medium", "low")


GOLD_COLUMNS = ("id", "document", "document_file", "document_section", "class", "regulation_version",
                "regulation_article", "what_the_document_says", "requirement", "verbatim_quote_ka", "expected_fix")


def _gold_files(cfg) -> list:
    """``paths.gold_labels`` plus ``paths.gold_labels_extra`` (a list), e.g. the gold keys of other directions."""
    files = [cfg.path("gold_labels")]
    for extra in (cfg.get("paths") or {}).get("gold_labels_extra") or []:
        from kg.config import REPO_ROOT
        files.append(REPO_ROOT / extra)
    return files


def load_gold(cfg) -> list[dict]:
    """All gold rows; files that write the column names without underscores (``documentfile``) are read too."""
    rows = []
    for path in _gold_files(cfg):
        if not path.exists():
            continue
        with open(path, encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                for col in GOLD_COLUMNS:
                    if col not in r and col.replace("_", "") in r:
                        r[col] = r[col.replace("_", "")]
                r["gold_file"] = path.name
                rows.append(r)
    for r in rows:
        r["paths"] = parse_gold_articles(r["regulation_article"])
        r["tokens"] = section_tokens(r["document_section"])
    return rows


def finding_views(store) -> list[dict]:
    docs = {n["id"]: n["props"] for n in store.nodes("InternalDoc")}
    out = []
    for f in store.findings():
        if f["doc_id"] == COMPANY:
            continue
        ob = store.get_node(f["obligation_id"])
        cl = store.get_node(f["clause_id"]) if f.get("clause_id") else None
        out.append({**f, "doc_file": Path_name(docs.get(f["doc_id"], {}).get("file")),
                    "section_no": (cl or {}).get("props", {}).get("section_no"),
                    "source_path": (ob or {}).get("props", {}).get("source_path")})
    return out


def Path_name(p) -> str | None:
    return p.replace("\\", "/").split("/")[-1] if p else None


def evaluate(gold: list[dict], findings: list[dict]) -> dict:
    """Pure function over gold rows and finding views; returns metrics and per-item rows."""
    matches: dict[str, list[tuple[dict, str]]] = defaultdict(list)       # gold id -> [(finding, how)]
    f_hits: dict[int, list[dict]] = defaultdict(list)                   # finding id -> gold items
    for g in gold:
        for f in findings:
            how = match(f, g, g["paths"], g["tokens"])
            if how:
                matches[g["id"]].append((f, how))
                f_hits[f["id"]].append(g)
    by_cls = {c: [g for g in gold if g["class"] == c] for c in "ABC"}

    def recall(c):
        items = by_cls[c]
        return (sum(1 for g in items if matches.get(g["id"])) / len(items)) if items else None

    c_flagged = [g for g in by_cls["C"] if matches.get(g["id"])]
    tp = [f for f in findings if any(g["class"] in "AB" for g in f_hits.get(f["id"], []))]
    attribution = []
    for c in "AB":
        for g in by_cls[c]:
            ms = matches.get(g["id"])
            if ms:
                attribution.append(any(f["gap_class"] == c for f, _ in ms))
    buckets = defaultdict(lambda: {"findings": 0, "true_positive": 0})
    for f in findings:
        b = buckets[f["issue_chance"] or "unknown"]
        b["findings"] += 1
        b["true_positive"] += 1 if f in tp else 0
    metrics = {
        "gold_items": {c: len(v) for c, v in by_cls.items()},
        "findings_total": len(findings),
        "recall_A": recall("A"), "recall_B": recall("B"),
        "precision": (len(tp) / len(findings)) if findings else None,
        "c_false_positive_rate": (len(c_flagged) / len(by_cls["C"])) if by_cls["C"] else None,
        "version_attribution_accuracy": (sum(attribution) / len(attribution)) if attribution else None,
        "chance_calibration": {k: {**v, "precision": v["true_positive"] / v["findings"]} for k, v in buckets.items()},
        "gold_by_chance": {c: dict(Counter(f["issue_chance"] for g in by_cls[c] for f, _ in matches.get(g["id"], [])[:1]))
                           for c in "AB"},
        "action_types": dict(Counter(f["action_type"] for f in findings)),
        "match_kinds": dict(Counter(how for v in matches.values() for _, how in v)),
    }
    items = []
    for g in gold:
        ms = matches.get(g["id"], [])
        items.append({"gold_id": g["id"], "class": g["class"], "document": g["document"],
                      "section": g["document_section"], "article": g["regulation_article"],
                      "matched": bool(ms), "match_kind": ms[0][1] if ms else "",
                      "finding_ids": ",".join(str(f["id"]) for f, _ in ms),
                      "gap_classes": ",".join(sorted({f["gap_class"] for f, _ in ms})),
                      "priorities": ",".join(sorted({f["priority"] for f, _ in ms}))})
    return {"metrics": metrics, "items": items, "matched_findings": [f["id"] for f in tp]}


def replay_check(cfg, gold: list[dict], reports: list[dict]) -> dict:
    """Share of the gold A items of each amendment that appear in its impact report."""
    out = {}
    for rep in reports:
        num = (rep.get("amendment_number") or "").strip()
        a_items = [g for g in gold if g["class"] == "A" and num and num in g["regulation_version"]]
        affected = [{**f, "doc_file": f.get("doc_file"), "section_no": f.get("section_no"),
                     "source_path": f.get("source_path")} for f in rep.get("affected", [])]
        hit = 0
        for g in a_items:
            if any(match(f, g, g["paths"], g["tokens"]) for f in affected):
                hit += 1
        out[rep["amendment"]] = {"amendment_number": num, "gold_A": len(a_items), "surfaced": hit,
                                 "share": (hit / len(a_items)) if a_items else None}
    return out
