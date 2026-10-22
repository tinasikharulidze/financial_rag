"""Suites over the v2 tables: ``triage``, ``compliance`` (from coverage/tasks) and ``proposals`` (sample to review).

The gold file is the existing ``answer_key_gold_labels.csv`` (class A = gap caused by an amendment, B = gap of the
base act, C = compliant decoys). A v2 "finding" is one problem the check found: a coverage row with verdict
partial / contradicts / none, expanded to one view per clause the judge pointed at (or one without a clause).
"""
from __future__ import annotations

import csv
import random
from collections import Counter, defaultdict

from kg.eval.compliance import load_gold
from kg.eval.compliance import evaluate as evaluate_findings
from kg.eval.matching import article_overlap, match
from kg.store.v2tables import jload

ISSUE = ("partial", "contradicts", "none")


def _versions(store, version: str | None) -> list[dict]:
    rows = store.q("SELECT id, status FROM document_versions")
    return [r for r in rows if version in (None, r["id"])]


def _file_of(store, version_id: str) -> str | None:
    n = store.get_node(version_id)
    f = (n or {"props": {}})["props"].get("file")
    return f.replace("\\", "/").split("/")[-1] if f else None


def finding_views_v2(store, version: str | None = None) -> list[dict]:
    out, k = [], 0
    for v in _versions(store, version):
        fname = _file_of(store, v["id"])
        edges = defaultdict(list)
        for e in store.edges("COVERED_BY"):
            if e["props"].get("doc_version") == v["id"] and e["props"].get("role") in ("partial", "contradicts"):
                edges[e["src"]].append(e)
        tri = {r["obligation_id"]: r["relevance"] for r in store.q(
            "SELECT obligation_id, relevance FROM triage WHERE doc_version_id=?", (v["id"],))}
        tasks = {r["obligation_id"]: r for r in store.q("SELECT * FROM tasks WHERE doc_version_id=?", (v["id"],))}
        for c in store.q("SELECT * FROM coverage WHERE doc_version_id=?", (v["id"],)):
            if c["verdict"] not in ISSUE:
                continue
            ob = store.get_node(c["obligation_id"])
            if ob is None:
                continue
            base = {"doc_file": fname, "source_path": ob["props"].get("source_path"),
                    "gap_class": "A" if c["cause"] == "amendment" else "B",
                    "issue_chance": tri.get(c["obligation_id"]) or "unknown",
                    "action_type": c["verdict"], "version_id": v["id"], "obligation_id": c["obligation_id"],
                    "priority": (tasks.get(c["obligation_id"]) or {}).get("priority") or "none"}
            clauses = [store.get_node(e["dst"]) for e in edges.get(c["obligation_id"], [])] or [None]
            for cl in clauses:
                k += 1
                out.append({**base, "id": k, "clause_id": cl["id"] if cl else None,
                            "section_no": (cl["props"].get("section_no") if cl else None)})
    return out


def run_compliance_v2(cfg, store, version: str | None = None) -> dict:
    gold = load_gold(cfg)
    res = evaluate_findings(gold, finding_views_v2(store, version))
    n_tasks = store.q1("SELECT COUNT(*) n FROM tasks")["n"]
    res["metrics"]["tasks"] = n_tasks
    res["metrics"]["source"] = "v2 coverage/edges"
    return res


# ------------------------------------------------------------------------------------------------ triage
def run_triage(cfg, store, version: str | None = None) -> dict:
    """Triage recall: for each gold A/B item, some obligation on its article is relevant/possibly for its document."""
    gold = load_gold(cfg)
    by_file = {}
    for v in _versions(store, version):
        by_file.setdefault(_file_of(store, v["id"]), []).append(v["id"])
    obs = {n["id"]: n for n in store.nodes("Obligation")}
    items, hit = [], Counter()
    total = Counter()
    for g in gold:
        if g["class"] not in "AB" or g["document_file"] not in by_file:
            continue
        total[g["class"]] += 1
        rel = []
        for vid in by_file[g["document_file"]]:
            for t in store.q("SELECT obligation_id, relevance FROM triage WHERE doc_version_id=?", (vid,)):
                ob = obs.get(t["obligation_id"])
                if ob and article_overlap(ob["props"].get("source_path"), g["paths"]):
                    rel.append(t["relevance"])
        ok = any(r in ("relevant", "possibly") for r in rel)
        hit[g["class"]] += int(ok)
        items.append({"gold_id": g["id"], "class": g["class"], "obligations_on_article": len(rel),
                      "passed": ok, "relevances": ",".join(sorted(set(rel)))})
    allt = Counter(r["relevance"] for v in _versions(store, version) for r in store.q(
        "SELECT relevance FROM triage WHERE doc_version_id=?", (v["id"],)))
    n = sum(allt.values())
    return {"metrics": {"gold_items": dict(total),
                        "triage_recall_A": hit["A"] / total["A"] if total["A"] else None,
                        "triage_recall_B": hit["B"] / total["B"] if total["B"] else None,
                        "relevance_counts": dict(allt),
                        "irrelevant_share": allt["irrelevant"] / n if n else None}, "items": items}


# ------------------------------------------------------------------------------------------------ proposals
def run_proposals(cfg, store, version: str | None = None, sample: int = 20, seed: int = 7) -> dict:
    """A reproducible sample of proposals for manual review + groundedness flag rate. The reviewer fills the
    ``verdict`` column of ``proposals_review.csv`` with correct | partly | wrong; ``score_review`` reads it back."""
    rows = [r for v in _versions(store, version) for r in store.q("SELECT * FROM proposals WHERE doc_version_id=?",
                                                                 (v["id"],))]
    flagged = sum(1 for r in rows if "ungrounded_numbers" in (r["flags"] or ""))
    pick = random.Random(seed).sample(rows, min(sample, len(rows)))
    path = cfg.path("eval_gold") / "proposals_review.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if path.exists():
        with open(path, encoding="utf-8-sig", newline="") as fh:
            done = {r["proposal_id"]: r for r in csv.DictReader(fh) if r.get("verdict")}
    else:
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["proposal_id", "doc_version_id", "kind", "obligation", "original_text", "proposed_text",
                        "flags", "verdict", "comment"])
            for r in pick:
                ob = store.get_node(r["obligation_id"])
                w.writerow([r["id"], r["doc_version_id"], r["kind"], (ob or {"props": {}})["props"].get("statement"),
                            r["original_text"], r["proposed_text"], r["flags"], "", ""])
    c = Counter(r["verdict"] for r in done.values())
    return {"metrics": {"proposals": len(rows), "flagged_ungrounded": flagged,
                        "flag_rate": flagged / len(rows) if rows else None, "sample": len(pick),
                        "reviewed": sum(c.values()), "review_file": str(path),
                        "correct_share": c["correct"] / sum(c.values()) if c else None,
                        "partly_share": c["partly"] / sum(c.values()) if c else None,
                        "wrong_share": c["wrong"] / sum(c.values()) if c else None},
            "items": [{"proposal_id": r["id"], "kind": r["kind"], "flags": r["flags"]} for r in pick]}
