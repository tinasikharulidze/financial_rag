#!/usr/bin/env python3
"""
Scrape one (or more) specific Matsne documents AND their amendments, in the same
format as the rest of your pipeline.

  python scrape_one.py 32962

What it does
  1. downloads the current consolidated page  -> matsne_data/documents_raw/32962.html
  2. parses it (modern OR legacy layout)      -> upserts into matsne_data/documents.jsonl
  3. runs scrape_amendments.py for it         -> matsne_data/graph_docs/{amendment_edges,amendment_docs}.jsonl, raw/*.html
  4. parses every amending doc too            -> matsne_data/graph_docs/amendment_documents.jsonl
                                                 (documents.jsonl format, ready for the chunker / embedding)

Options: --data-dir matsne_data  --delay 1.0  --refetch  --no-amendments
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from matsne_parser import BASE_URL, parse_base_metadata, parse_document

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5, connect=5, read=5, status=5, backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])))
session.headers.update({
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/154.0 Safari/537.36"),
    "Accept-Language": "ka,en;q=0.8",
})


def upsert_jsonl(path, rows, key="document_id"):
    existing = {}
    if path.exists():
        for line in path.open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                existing[r[key]] = r
    for r in rows:
        existing[r[key]] = r
    with path.open("w", encoding="utf-8") as f:
        for r in existing.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--data-dir", default="matsne_data")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--no-amendments", action="store_true")
    a = ap.parse_args()

    data = Path(a.data_dir)
    raw_dir = data / "documents_raw"
    graph = data / "graph_docs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    graph.mkdir(parents=True, exist_ok=True)

    for did in [str(int(i)) for i in a.ids]:
        # 1. download base page (latest consolidated version)
        raw = raw_dir / f"{did}.html"
        if raw.exists() and not a.refetch:
            html = raw.read_text(encoding="utf-8")
            print(f"[{did}] using cached {raw}")
        else:
            r = session.get(f"{BASE_URL}/ka/document/view/{did}", timeout=60)
            r.raise_for_status()
            html = r.text
            raw.write_text(html, encoding="utf-8")
            print(f"[{did}] downloaded")
            time.sleep(a.delay)

        # 2. parse + upsert
        doc = parse_document(html, parse_base_metadata(html, did))
        upsert_jsonl(data / "documents.jsonl", [doc])
        legacy = any(p["id"].startswith("LEGACY") for p in doc["parts"])
        print(f"[{did}] {doc['title']!r}: {doc['part_count']} parts, {len(doc['text'])} chars "
              f"({'legacy' if legacy else 'modern'} layout)")
        if doc["part_count"] == 0 or not doc["text"]:
            print(f"[{did}] WARNING: no text extracted - inspect {raw}")

        if a.no_amendments:
            continue

        # 3. amendments (edges + raw html)
        subprocess.run([sys.executable, str(Path(__file__).with_name("scrape_amendments.py")), did,
                        "--raw-dir", str(raw_dir), "--out", str(graph), "--delay", str(a.delay)], check=True)

        # 4. parse the amending documents into documents.jsonl format
        edges_path = graph / "amendment_edges.jsonl"
        amend_ids = []
        if edges_path.exists():
            for line in edges_path.open(encoding="utf-8"):
                e = json.loads(line) if line.strip() else None
                if e and e["base_doc_id"] == did:
                    amend_ids.append(e["amendment_doc_id"])
        rows, empty = [], []
        for aid in amend_ids:
            p = graph / "raw" / f"{aid}.html"
            if not p.exists():
                continue
            try:
                h = p.read_text(encoding="utf-8")
                d = parse_document(h, parse_base_metadata(h, aid))
                d["amends_document_id"] = did
                rows.append(d)
                if not d["text"]:
                    empty.append(aid)
            except Exception as exc:
                print(f"  [parse fail] {aid}: {exc}")
        upsert_jsonl(graph / "amendment_documents.jsonl", rows)
        print(f"[{did}] amendment documents parsed: {len(rows)}/{len(amend_ids)} | empty text: {empty or 0}")


if __name__ == "__main__":
    main()