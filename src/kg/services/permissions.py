"""Authorization (docs/KG_V2_PLAN.md section 3.4). Enforced in the service layer, not only in the UI.

    actor = a user dict from ``accounts.get_user`` ({'id', 'user_type': 'regulator' | 'entity', 'company_id', ...})

The matrix is data (``MATRIX``); ``require(actor, action, company_id)`` raises ``PermissionDenied``.
``visible_statuses(actor)`` tells which document-version statuses an actor may see (the regulator sees published
versions only).
"""
from __future__ import annotations

REGULATOR, ENTITY = "regulator", "entity"


class PermissionDenied(PermissionError):
    pass


# action -> (user types that may do it, must the resource belong to the actor's own company?)
MATRIX: dict[str, tuple[tuple[str, ...], bool]] = {
    "company.edit":      ((ENTITY,), True),            # own company profile and roles
    "document.upload":   ((ENTITY,), True),
    "document.view":     ((ENTITY, REGULATOR), True),  # entity: own company; regulator: any company (published only)
    "task.view":         ((ENTITY, REGULATOR), True),
    "decision.make":     ((ENTITY,), True),            # accept / dismiss / edit edges / edit profile / revise / publish
    "company.search":    ((REGULATOR,), False),
    "regulation.ask":    ((ENTITY, REGULATOR), False),  # Q&A and version comparison
}


def require(actor: dict | None, action: str, company_id: str | None = None) -> dict:
    if not actor:
        raise PermissionDenied("not logged in")
    if action not in MATRIX:
        raise PermissionDenied(f"unknown action {action}")
    types, own_only = MATRIX[action]
    if actor.get("user_type") not in types:
        raise PermissionDenied(f"{actor.get('user_type')} may not {action}")
    if own_only and actor["user_type"] == ENTITY and company_id != actor.get("company_id"):
        raise PermissionDenied(f"{action}: only for the user's own company")
    return actor


def allowed(actor: dict | None, action: str, company_id: str | None = None) -> bool:
    try:
        require(actor, action, company_id)
        return True
    except PermissionDenied:
        return False


def visible_statuses(actor: dict) -> tuple[str, ...] | None:
    """None = every status (a regulated entity looking at its own documents); regulators see published only."""
    return ("published",) if actor.get("user_type") == REGULATOR else None
