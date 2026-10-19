"""UC2: compare two dates of one act (added / removed / modified provisions with a word-level diff)."""
from __future__ import annotations

import difflib
import json
import re

from pydantic import BaseModel

from kg.ids import display_path, path_sort_key
from kg.llm import system_user

_TOK = re.compile(r"\s+|[^\s\w]|\w+", re.U)


def word_diff(a: str, b: str) -> list[dict]:
    """Token-level diff: [{'op': equal|delete|insert|replace, 'a': str, 'b': str}] with whitespace kept."""
    ta, tb = _TOK.findall(a or ""), _TOK.findall(b or "")
    sm = difflib.SequenceMatcher(a=ta, b=tb, autojunk=False)
    return [{"op": op, "a": "".join(ta[i1:i2]), "b": "".join(tb[j1:j2])} for op, i1, i2, j1, j2 in sm.get_opcodes()]


def _state(store, act: str, date: str) -> dict[str, dict]:
    out = {}
    for n in store.nodes("ProvisionVersion", as_of=date):
        p = n["props"]
        if str(p["act"]) == str(act):
            out[p["path"]] = {"id": n["id"], "text": p.get("text") or "", "status": p.get("status"),
                              "valid_from": n["valid_from"], "heading": p.get("heading")}
    return out


def versions(store, act: str, date_a: str, date_b: str) -> dict:
    sa, sb = _state(store, act, date_a), _state(store, act, date_b)
    changes = []
    for path in sorted(set(sa) | set(sb), key=path_sort_key):
        a, b = sa.get(path), sb.get(path)
        a_on = a is not None and a["status"] == "active"
        b_on = b is not None and b["status"] == "active"
        if not a_on and not b_on:
            continue
        if not a_on and b_on:
            kind = "added"
        elif a_on and not b_on:
            kind = "removed"
        elif a["text"] != b["text"] or a["heading"] != b["heading"]:
            kind = "modified"
        else:
            continue
        ta, tb = (a["text"] if a_on else ""), (b["text"] if b_on else "")
        changes.append({"path": path, "label": display_path(path), "kind": kind, "text_a": ta, "text_b": tb,
                        "version_a": a["id"] if a_on else None, "version_b": b["id"] if b_on else None,
                        "valid_from_b": b["valid_from"] if b else None,
                        "diff": word_diff(ta, tb) if kind == "modified" else []})
    counts = {k: sum(1 for c in changes if c["kind"] == k) for k in ("added", "removed", "modified")}
    return {"act": act, "date_a": date_a, "date_b": date_b, "counts": counts, "changes": changes}


class Summary(BaseModel):
    summary: str


SUMMARY_SYSTEM = ("You summarise changes between two versions of a Georgian regulation for a bank compliance officer. "
                  "Write a short summary IN GEORGIAN (5-10 sentences): what was added, removed or changed and what "
                  "it means in practice. Mention provisions by their labels. JSON only: {\"summary\": \"...\"}")


def summarize(llm, result: dict, max_changes: int = 40) -> str:
    items = [{"provision": c["label"], "change": c["kind"], "before": c["text_a"][:700], "after": c["text_b"][:700]}
             for c in result["changes"][:max_changes]]
    if not items:
        return "ცვლილებები არ არის."
    user = f"Compare {result['date_a']} -> {result['date_b']}:\n" + json.dumps(items, ensure_ascii=False, indent=1)
    return llm.call("compare_summary", system_user(SUMMARY_SYSTEM, user), Summary, max_tokens=2048).parsed.summary
