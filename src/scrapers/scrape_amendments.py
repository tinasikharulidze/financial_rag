#!/usr/bin/env python3
"""
Scrape the amendments listed under each base law on Matsne (same CLI/outputs as before).

  python scrape_amendments.py 32962
  python scrape_amendments.py --ids-file ids.txt
  python scrape_amendments.py --all [--dry-run]
Options: --raw-dir, --out, --delay, --limit N, --fetch-missing

Outputs in --out (default matsne_data/graph_docs):
  amendment_edges.jsonl, amendment_docs.jsonl, failed.json, raw/<amend_id>.html

Parsing lives in matsne_parser.py (works for modern and legacy page layouts).
"""
import argparse
import json
import re
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from matsne_parser import BASE_URL, parse_amendments, parse_doc_meta

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, connect=5, read=5, status=5, backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])))
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/154.0 Safari/537.36"),
    "Accept-Language": "ka,en;q=0.8",
})


def fetch(url):
    r = session.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def load_jsonl(path, key):
    out = {}
    if Path(path).exists():
        for line in Path(path).open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                out[key(r)] = r
    return out


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_ids(args, raw_dir):
    ids = list(args.ids)
    if args.ids_file:
        text = Path(args.ids_file).read_text(encoding="utf-8")
        ids += [x for x in re.split(r"[\s,;]+", text) if x]
    if args.all:
        ids += sorted(p.stem for p in raw_dir.glob("*.html") if p.stem.isdigit())
    ids = [str(int(i)) for i in dict.fromkeys(ids) if str(i).strip().isdigit()]
    return ids[: args.limit] if args.limit else ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="base document ids")
    ap.add_argument("--ids-file")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--raw-dir", default="matsne_data/documents_raw")
    ap.add_argument("--out", default="matsne_data/graph_docs")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fetch-missing", action="store_true", help="download a base page if it is not in --raw-dir")
    a = ap.parse_args()

    raw_dir, out = Path(a.raw_dir), Path(a.out)
    (out / "raw").mkdir(parents=True, exist_ok=True)
    base_ids = read_ids(a, raw_dir)
    if not base_ids:
        raise SystemExit("No base ids given (use ids, --ids-file or --all).")
    base_set = set(base_ids)

    edges = load_jsonl(out / "amendment_edges.jsonl", lambda r: (r["base_doc_id"], r["amendment_doc_id"]))
    docs = load_jsonl(out / "amendment_docs.jsonl", lambda r: r["doc_id"])
    failed = []

    # ---------- amendment lists from the base pages
    with_amend = without = missing = 0
    for bid in base_ids:
        path = raw_dir / f"{bid}.html"
        try:
            if path.exists():
                html = path.read_text(encoding="utf-8")
            elif a.fetch_missing:
                html = fetch(f"{BASE_URL}/ka/document/view/{bid}")
                raw_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(html, encoding="utf-8")
                time.sleep(a.delay)
            else:
                missing += 1
                print(f"  [skip] base page not found: {path}  (use --fetch-missing)")
                continue
        except Exception as e:
            failed.append({"doc_id": bid, "stage": "base", "error": str(e)})
            continue
        found = parse_amendments(html, bid)
        for e in found:
            edges[(e["base_doc_id"], e["amendment_doc_id"])] = e
        with_amend += bool(found)
        without += not found
        print(f"  base {bid}: {len(found)} amendments")

    write_jsonl(out / "amendment_edges.jsonl", sorted(edges.values(), key=lambda r: (r["base_doc_id"], r["seq"] or 0)))
    wanted = sorted({e["amendment_doc_id"] for (b, _), e in edges.items() if b in base_set})
    print(f"\nbase docs: {len(base_ids)} | with amendments: {with_amend} | without: {without} | "
          f"missing html: {missing}\nedges (all runs): {len(edges)} | amending docs to fetch for this run: {len(wanted)}")
    if a.dry_run:
        print("--dry-run: nothing downloaded.")
        return

    # ---------- download each amending doc once
    downloaded = cached = 0
    for i, aid in enumerate(wanted, 1):
        path = out / "raw" / f"{aid}.html"
        try:
            if path.exists():
                html, cached = path.read_text(encoding="utf-8"), cached + 1
            else:
                html = fetch(f"{BASE_URL}/ka/document/view/{aid}")
                parse_doc_meta(html, aid)          # raises if it is not a real document page
                path.write_text(html, encoding="utf-8")
                downloaded += 1
                time.sleep(a.delay)
            if aid not in docs:
                docs[aid] = parse_doc_meta(html, aid)
            if i % 10 == 0:
                print(f"  {i}/{len(wanted)}")
        except Exception as e:
            failed.append({"doc_id": aid, "stage": "amendment", "error": str(e)})
            print(f"  [fail] {aid}: {str(e)[:100]}")

    write_jsonl(out / "amendment_docs.jsonl", sorted(docs.values(), key=lambda r: r["doc_id"]))
    (out / "failed.json").write_text(json.dumps(failed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\ndownloaded: {downloaded} | already on disk: {cached} | failed: {len(failed)}")
    print(f"wrote {out}/amendment_edges.jsonl, amendment_docs.jsonl, failed.json, raw/*.html")


if __name__ == "__main__":
    main()