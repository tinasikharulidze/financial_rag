"""
Sample documents from chunked corpus and export them as readable batches, with exact parent_id / chunk_id labels, so eval questions can be written with exact gold IDs.

Reads   <chunks>/docs.jsonl, parents.jsonl, children.jsonl    (output of chunker.py)
Writes  <out>/batch_01.txt ... batch_NN.txt                   (upload these one at a time)
        <out>/manifest.csv                                    (which docs, which batch, why)

Sampling (seeded, reproducible):
  * SIBLING_DOCS docs come from "sibling groups": titles that share a template
    (e.g. special procurement rules for different companies, yearly editions) - groups are kept together
    in one batch so sibling-disambiguation and two-document questions are possible.
  * MULTI_DOCS docs are chosen to contain at least one parent split into several children,
    so the child-level metric differs from the parent-level one.
  * The rest are random.
Per document at most DOC_CAP characters are exported (in document order); anything cut is marked.

Usage: python export_eval_batches.py [--chunks matsne_data/chunks] [--out matsne_data/eval_batches]
                                     [--n-docs 80] [--seed 42] [--doc-cap 30000] [--batch-chars 110000]
"""
import argparse
import csv
import json
import random
import re
from collections import defaultdict
from pathlib import Path

PARENT_SHOW = 12000          # max chars shown for one parent (children beyond this are omitted + marked)


def load(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def norm(s):
    return re.sub(r"[^\w]+", "", (s or "").lower())


def grams(s, n=4):
    s = norm(s)
    return {s[i:i + n] for i in range(max(0, len(s) - n + 1))}


def jaccard(a, b):
    return len(a & b) / max(len(a | b), 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default="matsne_data/chunks")
    ap.add_argument("--out", default="evaluation")
    ap.add_argument("--n-docs", type=int, default=80)
    ap.add_argument("--sibling-docs", type=int, default=24)
    ap.add_argument("--multi-docs", type=int, default=25)
    ap.add_argument("--sim", type=float, default=0.45, help="title similarity for sibling grouping")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--doc-cap", type=int, default=30000)
    ap.add_argument("--batch-chars", type=int, default=110000)
    a = ap.parse_args()

    rng = random.Random(a.seed)
    cdir, out = Path(a.chunks), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    docs = {str(d["doc_id"]): d for d in load(cdir / "docs.jsonl")}
    parents = load(cdir / "parents.jsonl")
    children = load(cdir / "children.jsonl")

    pby = defaultdict(list)
    for p in parents:
        pby[str(p["doc_id"])].append(p)
    kids = defaultdict(list)
    for c in children:
        kids[c["parent_id"]].append(c)

    # ---------- eligible docs
    elig = []
    for did, ps in pby.items():
        body = [p for p in ps if p["kind"] == "unit" and not p.get("table_part")]
        if sum(p["char_len"] for p in body) >= 300:
            elig.append(did)
    elig.sort()
    print(f"eligible docs: {len(elig)} / {len(docs)}")

    tg = {d: grams(docs[d].get("title") or "") for d in elig}

    # ---------- sibling groups (seed + up to 2 nearest unused neighbours)
    used, groups = set(), []
    order = elig[:]
    rng.shuffle(order)
    n_sib = 0
    for d in order:
        if n_sib >= a.sibling_docs:
            break
        if d in used:
            continue
        cands = sorted(((jaccard(tg[d], tg[o]), o) for o in elig if o != d and o not in used), reverse=True)
        near = [o for s, o in cands[:2] if s >= a.sim]
        if near:
            g = [d] + near
            used.update(g)
            groups.append(g)
            n_sib += len(g)

    # ---------- multi-child docs, then random fill
    def has_multi(d):
        return any(p["n_children"] > 1 for p in pby[d])

    rest = [d for d in elig if d not in used]
    rng.shuffle(rest)
    multi = [d for d in rest if has_multi(d)][: max(0, a.multi_docs - sum(has_multi(x) for g in groups for x in g))]
    chosen_single = list(multi)
    pool = [d for d in rest if d not in set(chosen_single)]
    need = a.n_docs - sum(len(g) for g in groups) - len(chosen_single)
    chosen_single += pool[: max(0, need)]
    units = [(g, "sibling") for g in groups] + [([d], "multi" if d in multi else "random") for d in chosen_single]
    rng.shuffle(units)

    # ---------- render docs
    def render_doc(did, why, gid):
        d = docs[did]
        ps = pby[did]
        total = sum(p["char_len"] for p in ps)
        lines, used_chars, truncated, shown = [], 0, False, 0
        for p in ps:
            ks = sorted(kids[p["parent_id"]], key=lambda c: c["chunk_id"])
            if used_chars >= a.doc_cap:
                truncated = True
                break
            head = (f"--- PARENT {p['parent_id']} | kind={p['kind']} | annex={p['annex']} | "
                    f"{('მუხლი ' + str(p['article_no'])) if p['article_no'] else '-'}"
                    f"{(' ' + p['article_title']) if p['article_title'] else ''} | "
                    f"chapter={p['chapter'] or '-'} | children={p['n_children']} | chars={p['char_len']}"
                    f"{' | flags=' + ','.join(p['flags']) if p['flags'] else ''}")
            lines.append(head)
            if len(ks) <= 1:
                body = (ks[0]["text"] if ks else p["text"])[:PARENT_SHOW]
                lines.append(body + (" …[CUT]" if len((ks[0]["text"] if ks else p["text"])) > PARENT_SHOW else ""))
                used_chars += len(body)
            else:
                acc = 0
                for k in ks:
                    if acc >= PARENT_SHOW:
                        lines.append(f"[…{len(ks)} children total; remaining children omitted]")
                        truncated = True
                        break
                    lines.append(f"[CHILD {k['chunk_id']}]")
                    lines.append(k["text"])
                    acc += len(k["text"])
                used_chars += acc
            shown += 1
        if shown < len(ps):
            truncated = True
        header = (f"=== DOC {did} | {d.get('title')} | {d.get('type')} №{d.get('number')} | adopted={d.get('adopted')} "
                  f"| effective={d.get('effective_from')} | expires={d.get('expires')} | group={gid or '-'} "
                  f"| parents={len(ps)} shown={shown} total_chars={total}"
                  f"{' | TRUNCATED' if truncated else ''}")
        return header + "\n" + "\n".join(lines) + "\n", used_chars, truncated, shown, len(ps)

    batches, cur, cur_chars = [], [], 0
    manifest = []
    for gi, (members, why) in enumerate(units, 1):
        gid = f"G{gi:02d}" if len(members) > 1 else None
        rendered = [render_doc(d, why, gid) for d in members]
        size = sum(r[1] for r in rendered) + 400 * len(members)
        if cur and cur_chars + size > a.batch_chars:
            batches.append(cur)
            cur, cur_chars = [], 0
        for d, r in zip(members, rendered):
            cur.append(r[0])
            manifest.append({"doc_id": d, "title": docs[d].get("title"), "why": why, "group": gid or "",
                             "batch": len(batches) + 1, "parents": r[4], "parents_shown": r[3],
                             "truncated": r[2], "has_multi_child_parent": has_multi(d)})
        cur_chars += size
    if cur:
        batches.append(cur)

    for i, b in enumerate(batches, 1):
        text = (f"EVAL BATCH {i}/{len(batches)}  (each PARENT is a retrieval parent; a CHILD line marks a "
                f"retrievable chunk. Single-child parents have child id = <parent_id>:00)\n\n" + "\n".join(b))
        (out / f"batch_{i:02d}.txt").write_text(text, encoding="utf-8")
        print(f"batch_{i:02d}.txt  docs={len(b)}  chars={len(text):,}")

    with (out / "manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
        w.writeheader()
        w.writerows(manifest)
    sel = len(manifest)
    print(f"\nselected docs: {sel} | sibling-group docs: {sum(1 for m in manifest if m['why'] == 'sibling')} | "
          f"with multi-child parent: {sum(m['has_multi_child_parent'] for m in manifest)} | "
          f"truncated: {sum(m['truncated'] for m in manifest)}")


if __name__ == "__main__":
    main()