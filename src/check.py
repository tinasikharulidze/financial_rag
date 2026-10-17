import json
import sqlite3
import sys
from collections import defaultdict

DB_PATH = "DB/app.db"
DOCS_PATH = "matsne_data/chunks/docs.jsonl"
CHILDREN_PATH = "matsne_data/embeddings/children_bge_m3.jsonl"

if len(sys.argv) < 3:
    raise SystemExit('Usage: python inspect_chat.py USERNAME "CONVERSATION TITLE"')
username, title = sys.argv[1], sys.argv[2]

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

user = con.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
if not user:
    raise SystemExit(f"No user '{username}'")

conv = con.execute(
    "SELECT id, title FROM conversations WHERE user_id = ? AND title = ?",
    (user["id"], title),
).fetchone()
if not conv:
    print(f"No conversation titled '{title}' for {username}. Their conversations:")
    for r in con.execute("SELECT title FROM conversations WHERE user_id = ? ORDER BY id DESC", (user["id"],)):
        print("  ", r["title"])
    raise SystemExit

msgs = con.execute("SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv["id"],)).fetchall()

# 1. Collect all parent IDs and doc IDs across messages
needed_parents = set()
needed_docs = set()

for m in msgs:
    if m["retrieved_parents"]:
        needed_parents.update(p for p in m["retrieved_parents"].split(";") if p)
    if m["retrieved_docs"]:
        needed_docs.update(d for d in m["retrieved_docs"].split(";") if d)

# 2. Map Document Titles
doc_titles = {}
try:
    with open(DOCS_PATH, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            did = str(d.get("doc_id") or d.get("document_id") or d.get("id"))
            if did in needed_docs:
                doc_titles[did] = (d.get("title") or d.get("title_fallback") or "?")[:140]
except FileNotFoundError:
    pass

# 3. Map Parent IDs -> Child IDs from children JSONL
parent_to_children = defaultdict(list)
try:
    with open(CHILDREN_PATH, encoding="utf-8") as f:
        for line in f:
            c = json.loads(line)
            pid = str(c.get("parent_id") or c.get("pid"))
            cid = str(c.get("child_id") or c.get("id"))
            if pid in needed_parents:
                parent_to_children[pid].append(cid)
except FileNotFoundError:
    pass

# 4. Print inspection output
last_q = None
for m in msgs:
    if m["role"] == "user":
        last_q = m["content"]
        continue

    print("=" * 100)
    print("QUESTION :", last_q)
    print("REWRITTEN:", m["rewritten"])
    print("ANSWER   :", (m["content"] or "")[:150].replace("\n", " "), "...")
    if m["error"]:
        print("ERROR    :", m["error"])

    retrieved_parents = [p for p in (m["retrieved_parents"] or "").split(";") if p]
    cited_parents = set((m["cited_parents"] or "").split(";"))
    
    retrieved_docs = [d for d in (m["retrieved_docs"] or "").split(";") if d]
    cited_docs = set((m["cited_docs"] or "").split(";"))

    # Group parents by document
    docs_to_parents = defaultdict(list)
    for pid in retrieved_parents:
        doc_id = pid.split(":")[0]
        docs_to_parents[doc_id].append(pid)

    print(f"\nRETRIEVED {len(retrieved_docs)} docs / {len(retrieved_parents)} parents (★ = cited by LLM):")
    
    for i, doc_id in enumerate(retrieved_docs, 1):
        doc_mark = "★" if doc_id in cited_docs else " "
        doc_title = doc_titles.get(doc_id, "?")
        parents = docs_to_parents.get(doc_id, [])
        
        print(f"\n {i:2d}. [{doc_mark}] DOC {doc_id:>8} — {doc_title}")
        
        for p_idx, pid in enumerate(parents, 1):
            p_mark = "★" if pid in cited_parents else " "
            children = parent_to_children.get(pid, [])
            child_str = ", ".join(children) if children else "none found"
            
            print(f"      [{p_mark}] Parent: {pid}")
            print(f"          Children ({len(children)}): {child_str}")

    print()