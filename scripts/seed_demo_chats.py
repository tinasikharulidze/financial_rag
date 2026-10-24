"""Put evaluation conversations into the app's database as ordinary chat threads, so a demo user sees them in the
chat panel (stored answers only: no model is called, no quota is used).

    uv run scripts/seed_demo_chats.py --user-email demo@company.ge --dry-run
    uv run scripts/seed_demo_chats.py --user-email demo@company.ge --ids RD-02,CH-02,AP-03,EX-03

--results   the results.jsonl to take the answers from (default: evaluation/chat/results/flash2/results.jsonl)
--cases     the CSV the conversations were run from (it holds each conversation's screen context)
--ids       which conversations (default: every one that passed)
--user-id / --user-email   whose conversations they become (the demo user; must belong to the company of the document)

Back the database up first (copy data/kg/kg.sqlite). A conversation that is already there (same user, same first
question) is skipped, so the script can be run twice. Confirmation cards are not copied: their proposals no longer exist.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kg.agent import storage  # noqa: E402
from kg.agent.labels import node_label  # noqa: E402
from kg.config import load_config  # noqa: E402
from kg.services.accounts import get_user  # noqa: E402
from kg.store.sqlite_nx import SQLiteStore  # noqa: E402


def citations(store, acts, ids):
    out = []
    for cid in ids or []:
        lab = node_label(store, cid, acts)
        out.append({"id": cid, "type": lab["type"], "label": lab["title"], "sub": lab["sub"], "quote": None, "quote_ok": None})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=str(ROOT / "evaluation/chat/results/flash2/results.jsonl"))
    ap.add_argument("--cases", default=str(ROOT / "evaluation/chat/chat_cases.csv"))
    ap.add_argument("--ids")
    ap.add_argument("--user-id")
    ap.add_argument("--user-email")
    ap.add_argument("--db", help="database file (default: the one in configs/kg.yaml)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()
    store = SQLiteStore(Path(args.db) if args.db else cfg.path("db"))
    acts = cfg.get("acts")
    if args.user_email:
        row = store.q1("SELECT id FROM users WHERE lower(email)=lower(?)", (args.user_email,))
    elif args.user_id:
        row = {"id": int(args.user_id)}
    else:
        sys.exit("Say whose conversations these are: --user-email or --user-id")
    user = get_user(store, row["id"]) if row else None
    if not user:
        sys.exit("No such user.")

    contexts = {}
    with open(args.cases, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            contexts[r["id"]] = json.loads(r["context"] or "{}")
    results = [json.loads(line) for line in open(args.results, encoding="utf-8") if line.strip()]
    want = {x.strip() for x in args.ids.split(",")} if args.ids else None
    chosen = [r for r in results if (r["id"] in want if want else r.get("status") == "pass")]
    print(f"{len(chosen)} conversation(s) for {user.get('email') or user['id']}")

    done = 0
    for r in chosen:
        turns = [t for t in r.get("turns", []) if t.get("kind") in ("answer", "ask")]
        if not turns:
            continue
        ctx = contexts.get(r["id"], {})
        first = turns[0]["message"]
        if store.q1("SELECT t.id FROM chat_threads t JOIN chat_messages m ON m.thread_id=t.id AND m.role='user' "
                    "WHERE t.user_id=? AND m.content=?", (user["id"], first)):
            print(f"  {r['id']}: already there, skipped")
            continue
        print(f"  {r['id']}: {len(turns)} question(s)  {first[:60]}")
        if args.dry_run:
            continue
        tid = storage.new_thread(store, user, ctx.get("version_id"), first)
        for t in turns:
            storage.add_message(store, tid, "user", t["message"], ctx)
            storage.add_message(store, tid, "assistant", t["text"], None, {
                "kind": t["kind"], "citations": citations(store, acts, t.get("cites")), "dropped": [],
                "options": t.get("options") or [], "steps": t.get("steps"), "tools": t.get("tools") or [],
                "notes": [], "trace": [], "actions": []})
        done += 1
    print(f"Added {done} conversation(s)." if not args.dry_run else "Dry run: nothing written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
