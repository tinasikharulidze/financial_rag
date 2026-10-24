"""Tasks and the board (plan section 9): one task per proposal, kept up to date by deterministic code.

* key = ``<company>|<document version>|<lineage root of the obligation>`` (``__company__`` instead of the version for
  ``new_document`` proposals). The lineage root follows the amendment chain, so an obligation that an amendment
  modifies updates the SAME task instead of creating a new one. (Plan adjustment: the version is part of the key so a
  version kept next to a newer one keeps its own tasks; the newer version resolves older tasks explicitly.)
* statuses: open | accepted | not_relevant | already_addressed | resolved | replaced.
* every change writes ``task_events``; the pipeline never re-opens a task the user closed, except when a new amendment
  modified the obligation (``reopened_by_amendment``).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from kg.config import now_iso, today_iso
from kg.mapping import ranking
from kg.services.permissions import PermissionDenied, require, visible_statuses
from kg.store.v2tables import jload

OPEN = ("open", "accepted")
CLOSED_BY_USER = ("not_relevant", "already_addressed")
STATUSES = ("open", "accepted", "not_relevant", "already_addressed", "resolved", "replaced")
COMPANY_FAMILY = "__company__"
KIND_LABEL = {"adjust_clause": "შესწორება", "add_section": "ახალი პუნქტი", "new_document": "ახალი დოკუმენტი"}


class TaskError(ValueError):
    pass


def event(store, key: str, actor: int | None, name: str, data: dict | None = None):
    store.x("INSERT INTO task_events(task_key,actor,event,data,ts) VALUES(?,?,?,?,?)",
            (key, actor, name, json.dumps(data or {}, ensure_ascii=False, default=str), now_iso()))


def lineage_root(ob: dict) -> str:
    return ob["props"].get("lineage") or ob["id"]


def task_key(company_id: str, version_id: str | None, ob: dict, kind: str) -> str:
    return f"{company_id}|{COMPANY_FAMILY if kind == 'new_document' else version_id}|{lineage_root(ob)}"


def title_of(kind: str, ob: dict) -> str:
    st = " ".join((ob["props"].get("statement") or "").split())
    return f"{KIND_LABEL.get(kind, kind)}: {st[:90]}" + ("…" if len(st) > 90 else "")


def profile_type(store, version_id: str | None) -> str | None:
    r = store.q1("SELECT doc_type FROM doc_profiles WHERE doc_version_id=?", (version_id,)) if version_id else None
    return r["doc_type"] if r else None


def doc_match_of(store, version_id: str | None, ob: dict | None, types: dict | None = None) -> bool:
    """Ranking signal: the obligation names this document's type as the place it must be written."""
    if ob is None:
        return False
    if types is not None:
        if version_id not in types:
            types[version_id] = profile_type(store, version_id)
        dt = types[version_id]
    else:
        dt = profile_type(store, version_id)
    return ranking.doc_type_match(ob["props"], dt)


# ------------------------------------------------------------------------------------------------ sync
def sync_tasks(cfg, store, run, version_id: str, actor_id: int | None = None, only: set[str] | None = None) -> dict:
    """Create / update the tasks of one document version from its proposals, coverage and triage rows."""
    v = store.q1("SELECT dv.id, dv.family_id, d.company_id FROM document_versions dv "
                 "JOIN documents d ON d.id=dv.family_id WHERE dv.id=?", (version_id,))
    if v is None:
        raise KeyError(f"document version not found: {version_id}")
    company = v["company_id"]
    stats = {"created": 0, "updated": 0, "reopened": 0, "resolved": 0, "kept": 0}
    cov = {r["obligation_id"]: r for r in store.q("SELECT * FROM coverage WHERE doc_version_id=?", (version_id,))}
    rel = {r["obligation_id"]: r["relevance"] for r in store.q(
        "SELECT obligation_id, relevance FROM triage WHERE doc_version_id=?", (version_id,))}
    dtype = profile_type(store, version_id)
    now = now_iso()
    current: set[str] = set()
    for p in store.q("SELECT * FROM proposals WHERE doc_version_id=? ORDER BY id", (version_id,)):
        oid = p["obligation_id"]
        c = cov.get(oid)
        ob = store.get_node(oid)
        if ob is None or c is None or c["verdict"] not in ("partial", "contradicts", "none"):
            continue
        key = task_key(company, version_id, ob, p["kind"])
        current.add(key)
        if only is not None and oid not in only:
            continue
        pr = ob["props"]
        sev = pr.get("severity")
        sc = ranking.score(cfg, sev, c["verdict"], rel.get(oid), c["cause"], ranking.doc_type_match(pr, dtype))
        amend = c["cause"] == "amendment"
        fields = {"proposal_id": p["id"], "obligation_id": oid, "doc_version_id": version_id, "severity": sev,
                  "verdict": c["verdict"], "relevance": rel.get(oid), "cause": c["cause"],
                  "amendment_number": pr.get("amendment_number") if amend else None,
                  "amendment_effective": ob["valid_from"] if amend else None,
                  "score": sc, "priority": ranking.priority(cfg, sc), "title": title_of(p["kind"], ob)}
        row = store.q1("SELECT * FROM tasks WHERE key=?", (key,))
        if row is None:
            store.x("INSERT INTO tasks(key,company_id,doc_version_id,obligation_id,proposal_id,title,status,score,"
                    "priority,severity,verdict,relevance,cause,amendment_number,amendment_effective,due_date,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, company, version_id, oid, p["id"], fields["title"], "open", sc, fields["priority"], sev,
                     c["verdict"], rel.get(oid), c["cause"], fields["amendment_number"],
                     fields["amendment_effective"], fields["amendment_effective"], now, now))
            event(store, key, actor_id, "created", {"verdict": c["verdict"], "score": sc, "run": getattr(run, "id", None)})
            stats["created"] += 1
            continue
        status, ev = row["status"], None
        if status in CLOSED_BY_USER and row["obligation_id"] != oid and amend:
            status, ev = "open", "reopened_by_amendment"
        elif status in ("resolved", "replaced"):
            status, ev = "open", "reopened"
        changed = {k: [row[k], fields[k]] for k in ("verdict", "score", "priority", "obligation_id") if row[k] != fields[k]}
        sets = ", ".join(f"{k}=?" for k in fields)
        store.x(f"UPDATE tasks SET {sets}, status=?, updated_at=? WHERE key=?",
                (*fields.values(), status, now, key))
        if ev:
            event(store, key, actor_id, ev, {"from": row["status"], "obligation": oid})
            stats["reopened"] += 1
        elif changed:
            event(store, key, actor_id, "updated", changed)
            stats["updated"] += 1
        else:
            stats["kept"] += 1
    # tasks of this version whose proposal is gone: the obligation is covered now (or no longer applies)
    for t in store.q("SELECT * FROM tasks WHERE doc_version_id=? AND status IN ('open','accepted')", (version_id,)):
        if t["key"] in current:
            continue
        if only is not None and t["obligation_id"] not in only:
            continue
        store.x("UPDATE tasks SET status='resolved', updated_at=? WHERE key=?", (now, t["key"]))
        verdict = (cov.get(t["obligation_id"]) or {}).get("verdict")
        event(store, t["key"], actor_id, "auto_resolved", {"verdict": verdict, "reason":
              "covered" if verdict == "full" else "no longer applicable"})
        stats["resolved"] += 1
    stats["resolved"] += resolve_older_versions(store, v, cov, actor_id, only)
    return stats


def resolve_older_versions(store, v: dict, cov: dict, actor_id, only: set[str] | None) -> int:
    """Tasks of OLDER versions of the same document whose obligation is satisfied in this (newer) version."""
    n = 0
    for t in store.q("SELECT t.* FROM tasks t JOIN document_versions dv ON dv.id=t.doc_version_id "
                     "WHERE dv.family_id=? AND t.doc_version_id<>? AND t.status IN ('open','accepted')",
                     (v["family_id"], v["id"])):
        ob = store.get_node(t["obligation_id"])
        if ob is None:
            continue
        lineage = lineage_root(ob)
        now_ob = [oid for oid in cov if (store.get_node(oid) or {"props": {}})["props"].get("lineage") == lineage]
        if now_ob and all(cov[o]["verdict"] == "full" for o in now_ob) and (only is None or set(now_ob) & only):
            store.x("UPDATE tasks SET status='resolved', updated_at=? WHERE key=?", (now_iso(), t["key"]))
            event(store, t["key"], actor_id, "auto_resolved", {"by_version": v["id"], "verdict": "full"})
            n += 1
    return n


def rescore(cfg, store, company_id: str | None = None) -> int:
    """Recompute score and priority of all tasks from the stored verdict / relevance / cause (+ severity in the
    formula mode) and the document-type signal (no LLM)."""
    n = 0
    types: dict = {}
    for t in store.q("SELECT * FROM tasks" + (" WHERE company_id=?" if company_id else ""),
                     (company_id,) if company_id else ()):
        dm = doc_match_of(store, t["doc_version_id"], store.get_node(t["obligation_id"]), types)
        sc = ranking.score(cfg, t["severity"], t["verdict"], t["relevance"], t["cause"], dm)
        pr = ranking.priority(cfg, sc)
        if sc != t["score"] or pr != t["priority"]:
            store.x("UPDATE tasks SET score=?, priority=?, updated_at=? WHERE key=?", (sc, pr, now_iso(), t["key"]))
            event(store, t["key"], None, "rescored", {"score": [t["score"], sc], "priority": [t["priority"], pr]})
            n += 1
    return n


# ------------------------------------------------------------------------------------------------ reading
def is_new(cfg, task: dict, today: str | None = None) -> bool:
    if task.get("cause") != "amendment" or not task.get("amendment_effective"):
        return False
    days = int(cfg.get_in("tasks.new_amendment_days", 180))
    today_d = date.fromisoformat(today or today_iso())
    eff = date.fromisoformat(task["amendment_effective"])
    return today_d - timedelta(days=days) <= eff <= today_d


def get_task(store, actor: dict, key: str) -> dict:
    t = store.q1("SELECT * FROM tasks WHERE key=?", (key,))
    if t is None:
        raise TaskError("task not found")
    require(actor, "task.view", t["company_id"])
    return t


def _company_versions(store, company_id: str) -> list[str]:
    return [r["id"] for r in store.q("SELECT dv.id FROM document_versions dv JOIN documents d ON d.id=dv.family_id "
                                     "WHERE d.company_id=? AND dv.status IN ('draft','published')", (company_id,))]


def duplicate_new_documents(store, company_id: str) -> set[tuple[str, str]]:
    """(version, obligation) of the "no place found" proposals (kind ``new_document``) whose obligation is already
    handled in some document of the company: covered / partly covered / contradicted there, a proposal says where
    to change or add it, or it is judged "relevant" there. Such proposals and their tasks are duplicates: they are
    kept in the database but not shown."""
    vers = _company_versions(store, company_id)
    if not vers:
        return set()
    ph = ",".join("?" * len(vers))
    nd = store.q(f"SELECT doc_version_id, obligation_id FROM proposals WHERE kind='new_document' "
                 f"AND doc_version_id IN ({ph})", vers)
    if not nd:
        return set()
    handled = {r["obligation_id"] for r in store.q(
        f"SELECT obligation_id FROM coverage WHERE doc_version_id IN ({ph}) AND invalid=0 "
        "AND verdict IN ('full','partial','contradicts')", vers)}
    handled |= {r["obligation_id"] for r in store.q(
        f"SELECT obligation_id FROM proposals WHERE doc_version_id IN ({ph}) "
        "AND kind IN ('adjust_clause','add_section')", vers)}
    handled |= {r["obligation_id"] for r in store.q(
        f"SELECT obligation_id FROM triage WHERE doc_version_id IN ({ph}) AND relevance='relevant'", vers)}
    return {(r["doc_version_id"], r["obligation_id"]) for r in nd if r["obligation_id"] in handled}


def irrelevant_everywhere(store, version_ids) -> list[str]:
    """Obligations judged irrelevant in EVERY one of these document versions (and checked in at least one)."""
    vers = list(version_ids)
    if not vers:
        return []
    ph = ",".join("?" * len(vers))
    rows = store.q(f"SELECT obligation_id, relevance FROM triage WHERE doc_version_id IN ({ph})", vers)
    rel: dict[str, set] = {}
    for r in rows:
        rel.setdefault(r["obligation_id"], set()).add(r["relevance"])
    return sorted(o for o, s in rel.items() if s == {"irrelevant"})


def list_tasks(cfg, store, actor: dict, company_id: str, *, status: str | list[str] | None = None,
               doc_version_id: str | None = None, priority: str | None = None, cause: str | None = None,
               amendment_number: str | None = None, severity: str | None = None, assignee_user: int | None = None,
               only_new: bool = False, today: str | None = None, include_duplicates: bool = False) -> list[dict]:
    """Board rows sorted by score (highest first). Regulators see tasks of published versions only. Tasks of
    duplicate "no place found" proposals (``duplicate_new_documents``) are left out unless asked for."""
    require(actor, "task.view", company_id)
    rows = store.q("SELECT t.*, dv.status AS version_status, p.kind AS kind, p.flags AS flags "
                   "FROM tasks t JOIN document_versions dv ON dv.id=t.doc_version_id "
                   "LEFT JOIN proposals p ON p.id=t.proposal_id WHERE t.company_id=?", (company_id,))
    vis = visible_statuses(actor)
    sts = [status] if isinstance(status, str) else status
    dup = set() if include_duplicates else duplicate_new_documents(store, company_id)
    out = []
    for r in rows:
        if r.get("kind") == "new_document" and (r["doc_version_id"], r["obligation_id"]) in dup:
            continue
        if vis is not None and r["version_status"] not in vis:
            continue
        if sts and r["status"] not in sts:
            continue
        if doc_version_id and r["doc_version_id"] != doc_version_id:
            continue
        if priority and r["priority"] != priority:
            continue
        if cause and r["cause"] != cause:
            continue
        if amendment_number and r["amendment_number"] != amendment_number:
            continue
        if severity and r["severity"] != severity:
            continue
        if assignee_user is not None and r["assignee_user"] != assignee_user:
            continue
        r["flags"] = jload(r["flags"], [])
        r["is_new"] = is_new(cfg, r, today)
        if only_new and not r["is_new"]:
            continue
        out.append(r)
    return sorted(out, key=lambda r: (-r["score"], r["title"]))


def events_of(store, actor: dict, key: str) -> list[dict]:
    get_task(store, actor, key)
    rows = store.q("SELECT e.*, u.name AS actor_name FROM task_events e LEFT JOIN users u ON u.id=e.actor "
                   "WHERE e.task_key=? ORDER BY e.id", (key,))
    for r in rows:
        r["data"] = jload(r["data"], {})
    return rows


def explain_task(cfg, store, actor: dict, key: str) -> dict:
    """Everything that explains one task: ranking factors, coverage, triage, proposal, decisions (no LLM)."""
    t = get_task(store, actor, key)
    cov = store.q1("SELECT * FROM coverage WHERE doc_version_id=? AND obligation_id=?",
                   (t["doc_version_id"], t["obligation_id"]))
    tri = store.q1("SELECT * FROM triage WHERE doc_version_id=? AND obligation_id=?",
                   (t["doc_version_id"], t["obligation_id"]))
    decisions = store.q("SELECT d.*, u.name AS actor_name FROM user_decisions d LEFT JOIN users u ON u.id=d.actor "
                        "WHERE d.doc_version_id=? AND d.obligation_id=? ORDER BY d.id",
                        (t["doc_version_id"], t["obligation_id"]))
    dm = doc_match_of(store, t["doc_version_id"], store.get_node(t["obligation_id"]))
    return {"task": t, "ranking": ranking.explain(cfg, t["severity"], t["verdict"], t["relevance"], t["cause"], dm),
            "coverage": cov, "triage": tri, "decisions": decisions,
            "step_outputs": {"coverage": (cov or {}).get("llm_output_id"), "triage": (tri or {}).get("llm_output_id")},
            "events": events_of(store, actor, key)}


# ------------------------------------------------------------------------------------------------ people
def assign(store, actor: dict, key: str, user_id: int | None = None, role_id: int | None = None,
           due_date: str | None = None):
    t = get_task(store, actor, key)
    require(actor, "decision.make", t["company_id"])
    if user_id is not None:
        u = store.q1("SELECT company_id FROM users WHERE id=?", (user_id,))
        if not u or u["company_id"] != t["company_id"]:
            raise PermissionDenied("assignee must belong to the company")
    if role_id is not None:
        r = store.q1("SELECT company_id FROM company_roles WHERE id=?", (role_id,))
        if not r or r["company_id"] != t["company_id"]:
            raise PermissionDenied("role must belong to the company")
    store.x("UPDATE tasks SET assignee_user=?, assignee_role=?, due_date=COALESCE(?, due_date), updated_at=? "
            "WHERE key=?", (user_id, role_id, due_date, now_iso(), key))
    event(store, key, actor["id"], "assigned", {"user": user_id, "role": role_id, "due_date": due_date})


def comment(store, actor: dict, key: str, text: str):
    t = get_task(store, actor, key)
    require(actor, "decision.make", t["company_id"])
    if not (text or "").strip():
        raise TaskError("empty comment")
    event(store, key, actor["id"], "comment", {"text": text.strip()})
