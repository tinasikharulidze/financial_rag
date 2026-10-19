"""Consolidation suite: parse statistics and structural assertions (no external gold)."""
from __future__ import annotations

import json
from collections import Counter

from kg.ids import prov_id


def parse_stats(cfg) -> dict:
    d = cfg.path("data_dir") / "parsed" / "amendments"
    tot: Counter = Counter()
    per_amendment = {}
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("snapshot_only"):
            continue
        c = Counter()
        for ins in data["instructions"]:
            key = "llm" if (ins["status"] == "parsed" and ins.get("method") == "llm") else \
                  "rules" if ins["status"] == "parsed" else ins["status"]
            c[key] += 1
        per_amendment[data["doc_id"]] = {"base": data["base_doc_id"], "number": data["number"],
                                         "effective_from": data["effective_from"], **dict(c)}
        tot.update(c)
    n = sum(tot.values())
    return {"instructions": n, **{k: tot.get(k, 0) for k in ("rules", "llm", "unsupported", "unresolved")},
            "share_rules": tot.get("rules", 0) / n if n else None,
            "share_llm": tot.get("llm", 0) / n if n else None,
            "share_unresolved": tot.get("unresolved", 0) / n if n else None,
            "per_amendment": per_amendment}


def _versions(store, act: str, path: str) -> list[dict]:
    vs = [n for n in store.nodes("ProvisionVersion", act=act, path=path)]
    return sorted(vs, key=lambda n: n["valid_from"])


def assertions(cfg, store) -> list[dict]:
    res = []

    def check(name, ok, detail=""):
        res.append({"assertion": name, "passed": bool(ok), "detail": str(detail)})

    act = "4964423"
    ams = sorted(store.nodes("AmendingAct", base_act=act), key=lambda n: n["props"]["seq"])
    dates = [a["props"]["effective_from"] for a in ams]
    v1 = dates[0] if dates else None
    # known facts about the model-risk regulation
    a91 = _versions(store, act, "art9_1")
    check("art9_1 exists in 4964423 and starts at the first amendment's effective date",
          a91 and a91[0]["valid_from"] == v1, f"{[v['valid_from'] for v in a91]} vs {v1}")
    p3 = _versions(store, act, "art4/p3")
    check("art4/p3 changes at V1 and again at V2",
          len(p3) >= 3 and p3[1]["valid_from"] == v1 and (len(dates) < 2 or p3[2]["valid_from"] == dates[1])
          and len({v["props"]["text"] for v in p3}) == len(p3), f"{[v['valid_from'] for v in p3]}")
    # structural properties for every versioned act
    for doc, ac in cfg["acts"].items():
        if ac["mode"] != "versioned":
            continue
        n_am = len(store.nodes("AmendingAct", base_act=str(doc)))
        all_dates = sorted({n["valid_from"] for n in store.nodes("ProvisionVersion", act=str(doc))})
        check(f"{doc}: number of distinct version dates is between 2 and amendments+1",
              2 <= len(all_dates) <= n_am + 1 if n_am else len(all_dates) == 1, f"{len(all_dates)} dates, {n_am} amendments")
        gaps, overlaps = 0, 0
        paths: dict[str, list] = {}
        for n in store.nodes("ProvisionVersion", act=str(doc)):
            paths.setdefault(n["props"]["path"], []).append(n)
        for pth, vs in paths.items():
            vs.sort(key=lambda n: n["valid_from"])
            for a, b in zip(vs, vs[1:]):
                if a["valid_to"] != b["valid_from"]:
                    gaps += 1 if (a["valid_to"] is None or a["valid_to"] < b["valid_from"]) else 0
                    overlaps += 1 if (a["valid_to"] is not None and a["valid_to"] > b["valid_from"]) else 0
            if vs[-1]["valid_to"] is not None and vs[-1]["props"].get("status") == "active":
                gaps += 1
        check(f"{doc}: provision versions are contiguous (no gaps or overlaps)", gaps == 0 and overlaps == 0,
              f"gaps={gaps}, overlaps={overlaps}")
        check(f"{doc}: every Provision has at least one version", all(
            store.neighbors(prov_id(str(doc), p), "VERSION_OF", "in") for p in list(paths)[:50]))
    stats = parse_stats(cfg)
    check("less than 10% of amendment instructions are unresolved",
          stats["share_unresolved"] is not None and stats["share_unresolved"] < 0.10,
          f"{stats['unresolved']}/{stats['instructions']}")
    snap = [d for d, a in cfg["acts"].items() if a["mode"] == "snapshot"]
    for d in snap:
        vs = store.nodes("ProvisionVersion", act=d)
        check(f"{d}: snapshot act has exactly one version date", len({v['valid_from'] for v in vs}) == 1)
    return res


def run_consolidation(cfg, store) -> dict:
    stats = parse_stats(cfg)
    a = assertions(cfg, store)
    metrics = {"parse": {k: v for k, v in stats.items() if k != "per_amendment"},
               "assertions_passed": sum(1 for x in a if x["passed"]), "assertions_total": len(a)}
    return {"metrics": metrics, "items": a, "per_amendment": stats["per_amendment"]}
