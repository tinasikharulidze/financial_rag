#!/usr/bin/env python3
"""
Scrape the amendments listed under each base law on Matsne.

For every base document you give it, the script
  1. reads the base page from matsne_data/documents_raw/<id>.html   (no re-download needed)
  2. extracts the amendment list ("document-changes-bottom" block): amending doc id, sequence number,
     date, issuer/type, number, publication date, "contains transitional provision" flag, title
  3. downloads each amending document once -> matsne_data/graph_docs/raw/<amend_id>.html
  4. writes the graph data (merged with earlier runs, so you can run it in several batches):
       graph_docs/amendment_edges.jsonl   one row per (base doc -> amending doc)   = the "amended_by" edges
       graph_docs/amendment_docs.jsonl    one row per amending doc: title, number, type, dates ...
       graph_docs/failed.json             downloads that failed (rerun to retry)

The amending docs are ordinary Matsne documents, so the chunker works on them unchanged:
  python chunker.py --raw matsne_data/graph_docs/raw --out matsne_data/graph_docs/chunks

Usage
  python scrape_amendments.py 6398327 5679194            # base doc ids as arguments
  python scrape_amendments.py --ids-file ids.txt         # one id per line (or comma separated)
  python scrape_amendments.py --all                      # every page in documents_raw
  python scrape_amendments.py --all --dry-run            # only list the edges, download nothing
Options: --raw-dir, --out, --delay (seconds between requests, default 1.0), --limit N, --fetch-missing
"""
import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://matsne.gov.ge"
TRANSITIONAL = "ცვლილება შეიცავს გარდამავალ დებულებას"

# ============================================================
# HTTP
# ============================================================
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
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.text


# ============================================================
# PARSING
# ============================================================
def clean(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def iso(d):
    try:
        return datetime.strptime((d or "").strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def parse_amendments(html, base_id):
    """-> list of edge dicts, in the order Matsne lists them (1 = oldest amendment)."""
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".document-changes-bottom a.list-group-item")
    if not items:                                           # fallback if the block is named differently
        items = soup.select(".document-changes a.list-group-item")
    edges = []
    for a in items:
        m = re.search(r"/document/view/(\d+)", a.get("href", ""))
        if not m:
            continue
        amend_id = m.group(1)
        text = clean(a.get_text(" "))
        edge = {
            "base_doc_id": str(base_id), "amendment_doc_id": amend_id,
            "url": f"{BASE_URL}/ka/document/view/{amend_id}",
            "title": clean(a.get("title")), "seq": None, "amendment_date": None,
            "amendment_type": None, "amendment_number": None, "published_source": None,
            "published_date": None, "has_transitional": TRANSITIONAL in text, "raw_text": text,
        }
        # "13. 31/01/2025 - <type/issuer> - 12/04 - ვებგვერდი, 31/01/2025 [- <transitional note>]"
        mm = re.match(r"^(\d+)\.\s*(\d{2}/\d{2}/\d{4})\s*-\s*(.*)$", text)
        if mm:
            edge["seq"] = int(mm.group(1))
            edge["amendment_date"] = iso(mm.group(2))
            rest = mm.group(3).replace("- " + TRANSITIONAL, "").replace(TRANSITIONAL, "").strip(" -")
            parts = rest.rsplit(" - ", 2)
            if len(parts) == 3:
                edge["amendment_type"], edge["amendment_number"], pub = (clean(p) for p in parts)
                pm = re.match(r"^(.*?),\s*(\d{2}/\d{2}/\d{4})$", pub)
                if pm:
                    edge["published_source"], edge["published_date"] = clean(pm.group(1)), iso(pm.group(2))
                else:
                    edge["published_source"] = pub
        edges.append(edge)
    return edges


def parse_doc_meta(html, doc_id):
    soup = BeautifulSoup(html, "html.parser")
    if not soup.select_one("#maindoc"):
        raise ValueError("no #maindoc (not a document page / blocked?)")
    d = {}
    ti = soup.select_one("table.table-info")
    if ti:
        for tr in ti.find_all("tr"):
            c = tr.find_all(["th", "td"])
            if len(c) == 2:
                d[clean(c[0].get_text(" "))] = clean(c[1].get_text(" "))
    h1 = soup.select_one("h1.page-header")
    source = d.get("გამოქვეყნების წყარო, თარიღი", "")
    return {
        "doc_id": str(doc_id), "url": f"{BASE_URL}/ka/document/view/{doc_id}",
        "title": clean(h1.get_text(" ")) if h1 else None,
        "number": d.get("დოკუმენტის ნომერი"), "type": d.get("დოკუმენტის ტიპი"),
        "authority": d.get("დოკუმენტის მიმღები"), "adopted": iso(d.get("მიღების თარიღი", "")),
        "published": iso(source.split(",")[-1]) if "," in source else None,
        "effective_from": iso(d.get("ძალაში შესვლის თარიღი", "")),
        "registration_code": d.get("სარეგისტრაციო კოდი"),
        "scraped_at": datetime.now().isoformat(timespec="seconds"),
    }


# ============================================================
# FILES
# ============================================================
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


# ============================================================
# MAIN
# ============================================================
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

    edges = load_jsonl(out / "amendment_edges.jsonl", lambda r: (r["base_doc_id"], r["amendment_doc_id"]))
    docs = load_jsonl(out / "amendment_docs.jsonl", lambda r: r["doc_id"])
    failed = []

    # ---------- 1-2: amendment lists from the base pages
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
    wanted = sorted({e["amendment_doc_id"] for (b, _), e in edges.items() if b in set(base_ids)})
    print(f"\nbase docs: {len(base_ids)} | with amendments: {with_amend} | without: {without} | "
          f"missing html: {missing}\nedges (all runs): {len(edges)} | amending docs to fetch for this run: {len(wanted)}")
    if a.dry_run:
        print("--dry-run: nothing downloaded.")
        return

    # ---------- 3: download each amending doc once
    downloaded = cached = 0
    for i, aid in enumerate(wanted, 1):
        path = out / "raw" / f"{aid}.html"
        try:
            if path.exists():
                html, cached = path.read_text(encoding="utf-8"), cached + 1
            else:
                html = fetch(f"{BASE_URL}/ka/document/view/{aid}")
                meta = parse_doc_meta(html, aid)          # raises if it is not a real document page
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