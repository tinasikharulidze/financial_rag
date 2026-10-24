"""Confirmed writes of the Compliance Copilot (design document, section 9: "Confirmed writes").

The agent can only *propose*: ``propose(...)`` stores a pending action (what changes, before and after, the reason) and
nothing else. The person's click calls ``confirm(...)``, which runs the same service functions as the normal screens
(``services.decisions`` / ``services.accounts``) with the same permission checks. The agent never reaches ``confirm``.

    propose -> pending --confirm--> confirmed --undo--> undone
                  |--cancel--> cancelled      |--(state changed since)--> stale      |--(too old)--> expired

Rules enforced here, not in the prompt:
  * only an entity user can propose or confirm; the company comes from the session, never from the model's arguments;
  * every id a proposal names must have been read in the same turn (``seen``) and belong to the user's company;
  * a proposal expires (30 minutes, ``DEFAULT_TTL_MIN``) and is refused when the flag / mapping / profile changed
    after it was made (a fingerprint of the state is compared at confirm time and again before an undo);
  * at most ``MAX_PER_TURN`` proposals per answer; a bulk change shows every item and needs the explicit confirm;
  * every confirm writes ``user_decisions`` (kind ``chat_action``): who, when, reason, message, before and after.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from kg.config import now_iso
from kg.services import accounts
from kg.services import decisions as D
from kg.services import edits as E
from kg.services import tasks as T
from kg.services.permissions import PermissionDenied, require

KINDS = ("dismiss_flag", "mark_covered", "change_mapping", "save_fact", "report_extraction", "suggest_wording")
MAX_PER_TURN = 3
MAX_CHANGES = 10
DEFAULT_TTL_MIN = 30
ROLES = D.USER_ROLES                      # full | partial | contradicts

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,            -- pending | confirmed | cancelled | expired | stale | failed | undone
    actor           INTEGER NOT NULL,
    company_id      TEXT NOT NULL,
    doc_version_id  TEXT,
    obligation_id   TEXT,
    params          TEXT NOT NULL DEFAULT '{}',   -- exactly what will be written
    before          TEXT NOT NULL DEFAULT '{}',
    after           TEXT NOT NULL DEFAULT '{}',
    reason          TEXT,
    fingerprint     TEXT,                     -- the state the proposal was made against
    result_fp       TEXT,                     -- the state right after the commit (an undo needs it unchanged)
    thread_id       INTEGER,
    user_message_id INTEGER,
    message_id      INTEGER,
    context         TEXT NOT NULL DEFAULT '{}',   -- the context snapshot of the question
    result          TEXT NOT NULL DEFAULT '{}',
    error           TEXT,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_msg ON agent_actions(message_id);
CREATE TABLE IF NOT EXISTS extraction_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_id  TEXT NOT NULL,
    company_id     TEXT NOT NULL,
    actor          INTEGER NOT NULL,
    note           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',      -- open | withdrawn | closed
    action_id      INTEGER,
    created_at     TEXT NOT NULL
);
"""


class ActionError(ValueError):
    """A refused proposal / commit. The message is written for the person (or the model)."""


def ensure(store):
    with store._tx() as con:
        con.executescript(SCHEMA)


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str, sort_keys=True)


def _row(r: dict | None) -> dict | None:
    if r:
        for k in ("params", "before", "after", "context", "result"):
            r[k] = json.loads(r[k] or "{}")
    return r


# ----------------------------------------------------------------------------------------------- state fingerprints
def _doc_company(store, version_id: str) -> str | None:
    r = store.q1("SELECT d.company_id FROM document_versions dv JOIN documents d ON d.id=dv.family_id WHERE dv.id=?",
                 (version_id,))
    return r["company_id"] if r else None


def _flag_state(store, version_id: str, oid: str) -> dict:
    t = store.q1("SELECT status, verdict, relevance, proposal_id FROM tasks WHERE doc_version_id=? AND obligation_id=?",
                 (version_id, oid)) or {}
    cov = store.q1("SELECT verdict, source, invalid FROM coverage WHERE doc_version_id=? AND obligation_id=?",
                   (version_id, oid)) or {}
    tri = store.q1("SELECT relevance, source FROM triage WHERE doc_version_id=? AND obligation_id=?",
                   (version_id, oid)) or {}
    edges = sorted((e["clause_id"], e.get("role"), e.get("source")) for e in D.edges_of(store, version_id, oid))
    pr = store.q1("SELECT id, kind, clause_id, proposed_text, heading, explanation FROM proposals "
                  "WHERE doc_version_id=? AND obligation_id=?", (version_id, oid))
    ed = E.get_edits(store, version_id).get(E.slot_of(pr)) if pr else None
    return {"task": t, "coverage": cov, "triage": tri, "edges": edges, "proposal": pr,
            "edit": {"text": ed["text"], "source": ed["source"]} if ed else None}


def _facts(store, company_id: str) -> dict:
    c = accounts.get_company(store, company_id) or {"props": {}}
    return dict(c["props"].get("facts") or {})


def _state(store, kind: str, company_id: str, p: dict) -> dict:
    if kind == "save_fact":
        return {"facts": _facts(store, company_id)}
    if kind == "report_extraction":
        return {}
    return _flag_state(store, p["doc_version_id"], p["obligation_id"])


def _fp(state: dict) -> str:
    return hashlib.sha1(_j(state).encode()).hexdigest()[:16]


def _task_of(store, version_id: str, oid: str) -> dict:
    t = store.q1("SELECT * FROM tasks WHERE doc_version_id=? AND obligation_id=?", (version_id, oid))
    if t is None:
        raise ActionError("this obligation has no flag in the document, so there is nothing to change")
    return t


# ----------------------------------------------------------------------------------------------- propose
def _need_seen(seen: dict, *ids: str):
    for i in ids:
        if i not in seen:
            raise ActionError(f"read it in this conversation turn first (not retrieved yet): {i}")


def _reason(text) -> str:
    r = " ".join(str(text or "").split())
    if len(r) < 8:
        raise ActionError("a reason is required: write the grounds in a sentence or two, as they will be recorded")
    return r[:600]


def _numbers(*texts) -> set[str]:
    from kg.mapping.proposals import numbers_in
    out: set[str] = set()
    for t in texts:
        out |= numbers_in(str(t or ""))
    return out


def propose(store, actor: dict, scope, seen: dict, kind: str, args: dict, ttl_min: int = DEFAULT_TTL_MIN,
            extra_text: str = "") -> dict:
    """Validate and store ONE pending action. Returns {"id", "kind", "before", "after", ...}; nothing is written to the
    company's data. Raises ActionError with a readable reason."""
    ensure(store)
    if kind not in KINDS:
        raise ActionError(f"unknown action {kind}")
    cid = scope.company_id
    if actor.get("user_type") != "entity" or not cid:
        raise ActionError("changes can only be proposed for the signed-in company's own data")
    require(actor, "decision.make", cid)
    p, before, after = {}, {}, {}
    version = obl = None
    reason = None

    if kind in ("dismiss_flag", "mark_covered", "change_mapping", "suggest_wording"):
        version, obl = str(args.get("doc_version_id") or ""), str(args.get("obligation_id") or "")
        if not scope.doc_version_ok(version) or _doc_company(store, version) != cid:
            raise ActionError("that document version is not one of your company's documents")
        doc = store.q1("SELECT status FROM document_versions WHERE id=?", (version,))
        if doc["status"] != "draft":
            raise ActionError("only a draft can be changed; this version is published")
        _need_seen(seen, obl)
        ob = store.get_node(obl)
        if not ob or ob["type"] != "Obligation":
            raise ActionError("unknown obligation")
        task = _task_of(store, version, obl)
        reason = _reason(args.get("reason"))
        p = {"doc_version_id": version, "obligation_id": obl, "task_key": task["key"]}
        state = _flag_state(store, version, obl)
        mapped = {e["clause_id"]: e.get("role") for e in D.edges_of(store, version, obl)
                  if e.get("role") in ROLES}

        if kind == "dismiss_flag":
            if task["status"] not in T.OPEN:
                raise ActionError(f"the flag is already {task['status']}")
            before = {"status": task["status"], "verdict": task["verdict"]}
            after = {"status": "not_relevant"}
        elif kind == "mark_covered":
            clauses = [str(c) for c in (args.get("clause_ids") or [])]
            if not clauses:
                raise ActionError("name the clauses that cover the obligation (clause_ids)")
            if task["status"] not in T.OPEN:
                raise ActionError(f"the flag is already {task['status']}")
            for c in clauses:
                _need_seen(seen, c)
                try:
                    D._clause_ok(store, version, c)
                except D.DecisionError as exc:
                    raise ActionError(str(exc)) from exc
            p["clause_ids"] = clauses
            before = {"status": task["status"], "verdict": task["verdict"], "clauses": mapped}
            after = {"status": "already_addressed", "verdict": "full", "clauses": {c: "full" for c in clauses}}
        elif kind == "suggest_wording":
            pr = state["proposal"]
            if not pr:
                raise ActionError("this flag has no suggestion to revise")
            if task["status"] != "open":
                raise ActionError(f"the suggestion is {task['status']}: undo that in the review first, then revise it")
            text = str(args.get("text") or "").strip()
            if not 15 <= len(text) <= 4000:
                raise ActionError("give the complete new wording (15-4000 characters)")
            norm = lambda x: " ".join(str(x or "").split()).casefold()  # noqa: E731
            if norm(text) == norm(pr["proposed_text"]) or (pr["kind"] == "adjust_clause" and norm(text) == norm(
                    (store.get_node(pr["clause_id"]) or {"props": {}})["props"].get("text"))):
                raise ActionError("that is the same wording as now")
            if pr["clause_id"]:
                _need_seen(seen, pr["clause_id"])
            heading = " ".join(str(args.get("heading") or "").split())[:160] or pr["heading"]
            if pr["kind"] == "adjust_clause":
                heading = None
            ob_p = ob["props"]
            clause_text = (store.get_node(pr["clause_id"]) or {"props": {}})["props"].get("text") if pr["clause_id"] else ""
            known = _numbers(ob_p.get("statement"), ob_p.get("condition"), ob_p.get("exceptions"),
                             ob_p.get("deadline_or_frequency"), clause_text, pr["proposed_text"], extra_text,
                             *[v for k, v in seen.items() if k.startswith("pv:")])
            bad = sorted(_numbers(text) - known)
            flags = ["chat_redraft"] + (["ungrounded_numbers:" + ",".join(bad)] if bad else [])
            p.update({"proposal_id": pr["id"], "slot": E.slot_of(pr), "clause_id": pr["clause_id"], "text": text,
                      "heading": heading, "explanation": reason, "flags": flags, "kind": pr["kind"]})
            ed = state["edit"]
            before = {"text": pr["proposed_text"], "heading": pr["heading"],
                      "replaces_edit": ed["text"] if ed else None}
            after = {"text": text, "heading": heading, "ungrounded_numbers": bad}
        else:                                                              # change_mapping
            changes = args.get("changes") or []
            if not changes or len(changes) > MAX_CHANGES:
                raise ActionError(f"give between 1 and {MAX_CHANGES} changes")
            norm, new_map = [], dict(mapped)
            for ch in changes:
                c, op, role = str(ch.get("clause_id") or ""), str(ch.get("op") or ""), ch.get("role")
                _need_seen(seen, c)
                try:
                    D._clause_ok(store, version, c)
                except D.DecisionError as exc:
                    raise ActionError(str(exc)) from exc
                if op not in ("add", "change", "remove"):
                    raise ActionError("op must be add, change or remove")
                if op != "remove" and role not in ROLES:
                    raise ActionError(f"role must be one of {list(ROLES)}")
                if op == "add" and c in mapped:
                    raise ActionError("that clause is already mapped to the obligation: use op=change")
                if op in ("change", "remove") and c not in mapped:
                    raise ActionError("that clause is not mapped to the obligation yet: use op=add")
                norm.append({"clause_id": c, "op": op, "role": role if op != "remove" else None})
                if op == "remove":
                    new_map.pop(c, None)
                else:
                    new_map[c] = role
            p["changes"] = norm
            before = {"verdict": (state["coverage"] or {}).get("verdict"), "clauses": mapped}
            after = {"clauses": new_map, "needs_recheck": True}

    elif kind == "save_fact":
        text = " ".join(str(args.get("fact") or "").split())
        if not 6 <= len(text) <= 400:
            raise ActionError("state the fact in one plain sentence (6-400 characters)")
        reason = _reason(args.get("reason") or "confirmed by the user in the chat")
        facts = _facts(store, cid)
        fid = "f" + hashlib.sha1(text.casefold().encode()).hexdigest()[:8]
        if fid in facts:
            raise ActionError("the profile already says exactly this")
        repl = args.get("replaces")
        if repl and repl not in facts:
            raise ActionError("the fact to replace is not in the profile")
        p = {"fact_id": fid, "fact": text, "replaces": repl}
        before = {"fact": facts[repl]["text"] if repl else None}
        after = {"fact": text}

    else:                                                                    # report_extraction
        obl = str(args.get("obligation_id") or "")
        _need_seen(seen, obl)
        ob = store.get_node(obl)
        if not ob or ob["type"] != "Obligation":
            raise ActionError("unknown obligation")
        reason = _reason(args.get("note") or args.get("reason"))
        p = {"obligation_id": obl, "note": reason}
        before, after = {"report": None}, {"report": reason}

    # the newest proposal of the same kind on the same target replaces the older pending one
    for old in store.q("SELECT id FROM agent_actions WHERE actor=? AND kind=? AND status='pending' "
                       "AND COALESCE(doc_version_id,'')=? AND COALESCE(obligation_id,'')=?",
                       (actor["id"], kind, version or "", obl or "")):
        store.x("UPDATE agent_actions SET status='cancelled', decided_at=? WHERE id=?", (now_iso(), old["id"]))
    state = _state(store, kind, cid, p)
    aid = store.x(
        "INSERT INTO agent_actions(kind,status,actor,company_id,doc_version_id,obligation_id,params,before,after,reason,"
        "fingerprint,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, "pending", actor["id"], cid, version, obl or None, _j(p), _j(before), _j(after), reason, _fp(state),
         now_iso(), (datetime.now() + timedelta(minutes=ttl_min)).isoformat(timespec="seconds")))
    return {"id": aid, "kind": kind, "before": before, "after": after, "reason": reason}


def attach(store, action_ids: list[int], thread_id: int, user_message_id: int, message_id: int, context: dict):
    """Link the proposals of an answer to the conversation: the audit trail from the card back to the question."""
    for aid in action_ids:
        store.x("UPDATE agent_actions SET thread_id=?, user_message_id=?, message_id=?, context=? WHERE id=?",
                (thread_id, user_message_id, message_id, _j(context), aid))


# ----------------------------------------------------------------------------------------------- read
def get(store, actor: dict, action_id: int) -> dict | None:
    """Only the person who asked can see (and confirm) a proposal."""
    ensure(store)
    return _row(store.q1("SELECT * FROM agent_actions WHERE id=? AND actor=?", (action_id, actor["id"])))


def _expired(r: dict) -> bool:
    return r["status"] == "pending" and r["expires_at"] < datetime.now().isoformat(timespec="seconds")


def view(store, actor: dict, action_id: int) -> dict | None:
    """The row as the card shows it; a pending proposal that is too old or whose target changed is marked on the way."""
    r = get(store, actor, action_id)
    if not r:
        return None
    if _expired(r):
        store.x("UPDATE agent_actions SET status='expired', decided_at=? WHERE id=?", (now_iso(), action_id))
        r["status"] = "expired"
    if r["status"] == "pending":
        try:
            if _fp(_state(store, r["kind"], r["company_id"], r["params"])) != r["fingerprint"]:
                r["status"] = "stale"
        except Exception:  # noqa: BLE001 - a vanished target is stale as well
            r["status"] = "stale"
    return r


# ----------------------------------------------------------------------------------------------- confirm / undo
def _check_actor(actor: dict, r: dict):
    require(actor, "decision.make", r["company_id"])
    if r["actor"] != actor["id"]:
        raise PermissionDenied("this proposal belongs to somebody else")


def confirm(cfg, store, actor: dict, action_id: int, reason: str | None = None) -> dict:
    """The person's click. Commits through the normal services; returns the updated row."""
    r = get(store, actor, action_id)
    if not r:
        raise ActionError("unknown proposal")
    _check_actor(actor, r)
    if r["status"] != "pending":
        raise ActionError(f"the proposal is already {r['status']}")
    if _expired(r):
        store.x("UPDATE agent_actions SET status='expired', decided_at=? WHERE id=?", (now_iso(), action_id))
        raise ActionError("the proposal expired: ask again")
    p, kind = r["params"], r["kind"]
    if _fp(_state(store, kind, r["company_id"], p)) != r["fingerprint"]:
        store.x("UPDATE agent_actions SET status='stale', decided_at=? WHERE id=?", (now_iso(), action_id))
        raise ActionError("this changed after the proposal was made: ask again and I will propose it afresh")
    why = _reason(reason) if reason else r["reason"]
    if kind == "dismiss_flag":
        p = {**p, "reason": why}
    try:
        result = _apply(cfg, store, actor, kind, p, why, action_id)
    except (D.DecisionError, T.TaskError, accounts.AccountError, ActionError) as exc:
        store.x("UPDATE agent_actions SET status='failed', error=?, decided_at=? WHERE id=?",
                (str(exc)[:300], now_iso(), action_id))
        raise ActionError(str(exc)) from exc
    store.x("UPDATE agent_actions SET status='confirmed', reason=?, result=?, result_fp=?, decided_at=? WHERE id=?",
            (why, _j(result), _fp(_state(store, kind, r["company_id"], r["params"])), now_iso(), action_id))
    D.record_decision(store, actor, "chat_action", doc_version_id=r["doc_version_id"], obligation_id=r["obligation_id"],
                      data={"action_id": action_id, "action": kind, "reason": why, "before": r["before"],
                            "after": r["after"], "result": result, "thread_id": r["thread_id"],
                            "message_id": r["message_id"], "context": r["context"]})
    return get(store, actor, action_id)


def _apply(cfg, store, actor: dict, kind: str, p: dict, why: str, action_id: int) -> dict:
    if kind == "dismiss_flag":
        t = D.not_relevant(store, actor, p["task_key"], why)
        return {"status": t["status"]}
    if kind == "mark_covered":
        t = D.already_addressed(store, actor, p["task_key"], p["clause_ids"])
        return {"status": t["status"]}
    if kind == "suggest_wording":
        pr = store.q1("SELECT * FROM proposals WHERE id=?", (p["proposal_id"],))
        if pr is None:
            raise ActionError("the suggestion no longer exists")
        edits = E.get_edits(store, pr["doc_version_id"])
        snap = {"proposal": {k: pr[k] for k in ("proposed_text", "heading", "explanation", "flags", "llm_output_id",
                                                "created_at")}, "edit": edits.get(p["slot"])}
        store.x("UPDATE proposals SET proposed_text=?, heading=?, explanation=?, flags=?, llm_output_id=NULL, created_at=? "
                "WHERE id=?", (p["text"], p["heading"], p["explanation"], _j(p["flags"]), now_iso(), pr["id"]))
        E.drop_edit(store, pr["doc_version_id"], p["slot"])                    # a hand edit of the old wording is replaced
        T.event(store, p["task_key"], actor["id"], "chat_redraft", {"action_id": action_id})
        return {"proposal_id": pr["id"], "slot": p["slot"], "snapshot": json.loads(_j(snap))}
    if kind == "change_mapping":
        from kg.runlog import Run
        out = {}
        with Run(store, cfg.path("runs"), "agent_write", {"action": action_id}) as run:
            for ch in p["changes"]:
                args = (cfg, store, run, actor, p["doc_version_id"], p["obligation_id"], ch["clause_id"])
                if ch["op"] == "remove":
                    v = D.edge_remove(*args)
                elif ch["op"] == "add":
                    v = D.edge_add(*args, ch["role"])
                else:
                    v = D.edge_change(*args, ch["role"])
                out = {"verdict": v}
        out["needs_recheck"] = True
        return out
    if kind == "save_fact":
        cid = actor["company_id"]
        facts = _facts(store, cid)
        if p.get("replaces"):
            facts.pop(p["replaces"], None)
        facts[p["fact_id"]] = {"text": p["fact"], "source": "chat", "by": actor["id"], "ts": now_iso()}
        accounts.update_company(store, actor, cid, props={"facts": facts})
        return {"fact_id": p["fact_id"]}
    rid = store.x("INSERT INTO extraction_reports(obligation_id,company_id,actor,note,action_id,created_at) "
                  "VALUES(?,?,?,?,?,?)", (p["obligation_id"], actor["company_id"], actor["id"], p["note"], action_id,
                                          now_iso()))
    return {"report_id": rid}


def cancel(store, actor: dict, action_id: int) -> dict:
    r = get(store, actor, action_id)
    if not r:
        raise ActionError("unknown proposal")
    _check_actor(actor, r)
    if r["status"] == "pending":
        store.x("UPDATE agent_actions SET status='cancelled', decided_at=? WHERE id=?", (now_iso(), action_id))
    return get(store, actor, action_id)


def undo(cfg, store, actor: dict, action_id: int) -> dict:
    """Take a confirmed write back through the normal services. Refused when something else changed the target since."""
    r = get(store, actor, action_id)
    if not r:
        raise ActionError("unknown proposal")
    _check_actor(actor, r)
    if r["status"] != "confirmed":
        raise ActionError("only a confirmed change can be undone")
    kind, p = r["kind"], r["params"]
    if _fp(_state(store, kind, r["company_id"], p)) != r["result_fp"]:
        raise ActionError("it was changed again after this: undo it where it was changed")
    try:
        if kind in ("dismiss_flag", "mark_covered"):
            D.reopen(store, actor, p["task_key"])
        elif kind == "change_mapping":
            from kg.runlog import Run
            before = r["before"]["clauses"]
            with Run(store, cfg.path("runs"), "agent_write", {"undo": action_id}) as run:
                for ch in reversed(p["changes"]):
                    args = (cfg, store, run, actor, p["doc_version_id"], p["obligation_id"], ch["clause_id"])
                    old = before.get(ch["clause_id"])
                    if ch["op"] == "add":
                        D.edge_remove(*args)
                    elif old:
                        (D.edge_add if ch["op"] == "remove" else D.edge_change)(*args, old)
        elif kind == "suggest_wording":
            snap = r["result"]["snapshot"]
            old = snap["proposal"]
            store.x("UPDATE proposals SET proposed_text=?, heading=?, explanation=?, flags=?, llm_output_id=?, created_at=? "
                    "WHERE id=?", (old["proposed_text"], old["heading"], old["explanation"], old["flags"],
                                   old["llm_output_id"], old["created_at"], p["proposal_id"]))
            E.drop_edit(store, p["doc_version_id"], p["slot"])
            if snap.get("edit"):
                e = snap["edit"]
                E.save_edit(store, {"id": e.get("actor")} if e.get("actor") else None, p["doc_version_id"], p["slot"],
                            e["text"], e["proposal_ids"], e["source"], e.get("notes"))
            T.event(store, p["task_key"], actor["id"], "chat_redraft_undone", {"action_id": action_id})
        elif kind == "save_fact":
            facts = _facts(store, r["company_id"])
            facts.pop(p["fact_id"], None)
            if p.get("replaces") and r["before"].get("fact"):
                facts[p["replaces"]] = {"text": r["before"]["fact"], "source": "restored", "by": actor["id"],
                                        "ts": now_iso()}
            accounts.update_company(store, actor, r["company_id"], props={"facts": facts})
        else:
            store.x("UPDATE extraction_reports SET status='withdrawn' WHERE action_id=?", (action_id,))
    except (D.DecisionError, T.TaskError, accounts.AccountError) as exc:
        raise ActionError(str(exc)) from exc
    store.x("UPDATE agent_actions SET status='undone', decided_at=? WHERE id=?", (now_iso(), action_id))
    D.record_decision(store, actor, "chat_action_undo", doc_version_id=r["doc_version_id"],
                      obligation_id=r["obligation_id"], data={"action_id": action_id, "action": kind})
    return get(store, actor, action_id)
