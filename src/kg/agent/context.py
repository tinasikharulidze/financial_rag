"""The context envelope a view sends with every message: IDs, labels and small UI state, never document text.

Tenant and user are NOT part of the envelope: they come from the authenticated session (the ``actor``). The backend
re-validates everything the client sends (``validate``): ids the actor may not see are dropped.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

VIEWS = ("documents", "analysis", "board", "other")
ORIGINS = ("view", "popup", "user")
MAX_CHIPS = 8


@dataclass
class Chip:
    type: str                 # version | clause | obligation | provision | act | ...
    id: str
    label: str = ""
    origin: str = "view"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChatContext:
    view: str = "other"
    focus: Chip | None = None
    chips: list[Chip] = field(default_factory=list)
    version_id: str | None = None            # analysis view: the document version on screen
    compare_version_id: str | None = None
    clause_id: str | None = None             # the selected clause
    obligation_id: str | None = None         # the open flag
    as_of: str | None = None                 # regulation as-of date of the check run
    stale: bool | None = None
    state: dict = field(default_factory=dict)  # documents view: filters, sort, counts (small, no rows)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in (None, [], {})}


def _chip(raw) -> Chip | None:
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("type"):
        return None
    origin = raw.get("origin") if raw.get("origin") in ORIGINS else "view"
    return Chip(str(raw["type"])[:30], str(raw["id"])[:200], str(raw.get("label") or "")[:120], origin)


def from_payload(payload: dict | None) -> ChatContext:
    """Lenient parse of what the client sent; unknown keys are ignored. Focus follows the precedence rule."""
    p = payload if isinstance(payload, dict) else {}
    ctx = ChatContext(
        view=p.get("view") if p.get("view") in VIEWS else "other",
        chips=[c for c in (_chip(x) for x in (p.get("chips") or [])) if c][:MAX_CHIPS],
        version_id=p.get("version_id") or None, compare_version_id=p.get("compare_version_id") or None,
        clause_id=p.get("clause_id") or None, obligation_id=p.get("obligation_id") or None,
        as_of=p.get("as_of") or None, stale=p.get("stale") if isinstance(p.get("stale"), bool) else None,
        state={k: v for k, v in (p.get("state") or {}).items() if isinstance(k, str)} if isinstance(
            p.get("state"), dict) else {})
    ctx.focus = pick_focus(ctx, _chip(p.get("focus")))
    return ctx


def pick_focus(ctx: ChatContext, explicit: Chip | None = None) -> Chip | None:
    """Focus precedence: the open flag, then the selected clause, then the document version. The less specific
    items stay as chips. An explicit focus from the client is only used when none of the three is set."""
    if ctx.obligation_id:
        return Chip("obligation", ctx.obligation_id, origin="popup")
    if ctx.clause_id:
        return Chip("clause", ctx.clause_id)
    if ctx.version_id:
        return Chip("version", ctx.version_id)
    return explicit


def validate(scope, ctx: ChatContext) -> tuple[ChatContext, list[str]]:
    """Drop everything the actor may not see. ``scope`` is a ``tools.Scope``. Returns (clean context, notes)."""
    notes: list[str] = []

    def keep_doc(vid, what):
        if vid and not scope.doc_version_ok(vid):
            notes.append(f"{what} dropped: not accessible")
            return None
        return vid

    ctx.version_id = keep_doc(ctx.version_id, "version")
    ctx.compare_version_id = keep_doc(ctx.compare_version_id, "compare version")
    if ctx.clause_id:
        n = scope.store.get_node(ctx.clause_id)
        if not n or not scope.node_ok(n) or (ctx.version_id and n["props"].get("doc") != ctx.version_id):
            notes.append("clause dropped: not accessible or not in the open document")
            ctx.clause_id = None
    if ctx.obligation_id:
        n = scope.store.get_node(ctx.obligation_id)
        if not n or n["type"] != "Obligation":
            notes.append("flag dropped: unknown obligation")
            ctx.obligation_id = None
    clean = []
    for c in ctx.chips:
        n = scope.store.get_node(c.id)
        if (n and scope.node_ok(n)) or (c.type == "version" and scope.doc_version_ok(c.id)) \
                or scope.proposal_node(c.id):
            clean.append(c)
        else:
            notes.append(f"chip {c.id} dropped")
    ctx.chips = clean
    ctx.focus = pick_focus(ctx, ctx.focus if ctx.focus and any(c.id == ctx.focus.id for c in clean) else None)
    return ctx, notes


@dataclass
class Cursor:
    """Where the user is in the graph: structured state updated by the tools, not remembered by the model."""
    current: str | None = None
    trail: list[str] = field(default_factory=list)

    def visit(self, node_id: str | None, cap: int = 12):
        if not node_id or node_id == self.current:
            return
        self.current = node_id
        self.trail = [t for t in self.trail if t != node_id] + [node_id]
        self.trail = self.trail[-cap:]

    def to_dict(self) -> dict:
        return {"current": self.current, "trail": list(self.trail)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Cursor":
        d = d if isinstance(d, dict) else {}
        return cls(d.get("current"), [str(x) for x in (d.get("trail") or [])][-12:])
