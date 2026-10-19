"""Risk labels (docs/KG_ARCHITECTURE.md section 3.3): issue_chance x severity -> priority."""
from __future__ import annotations

CHANCES = ["very_high", "high", "moderate", "low"]
SEVERITIES = ["critical", "high", "medium", "low"]
LEVEL_ORDER = ["low", "medium", "high", "critical"]

KA = {
    "very_high": "ძალიან მაღალი", "high": "მაღალი", "moderate": "საშუალო", "low": "დაბალი",
    "critical": "კრიტიკული", "medium": "საშუალო",
}


def priority(severity: str, issue_chance: str) -> str:
    """priority = severity, except that issue_chance == low drops it by one level (a moderate chance on a critical
    obligation therefore stays critical)."""
    sev = severity if severity in LEVEL_ORDER else "medium"
    if issue_chance == "low":
        return LEVEL_ORDER[max(0, LEVEL_ORDER.index(sev) - 1)]
    return sev


def norm_chance(v: str | None) -> str:
    v = (v or "moderate").strip().lower().replace(" ", "_").replace("-", "_")
    return v if v in CHANCES else {"medium": "moderate", "very high": "very_high"}.get(v, "moderate")


def sort_rank(prio: str) -> int:
    return LEVEL_ORDER.index(prio) if prio in LEVEL_ORDER else -1
