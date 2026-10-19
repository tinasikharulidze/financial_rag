"""Configuration loader for the knowledge-graph draft.

Reads ``configs/kg.yaml`` (path can be overridden with the environment variable ``KG_CONFIG``).
All relative paths in the file are resolved against the repository root.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TBILISI = timezone(timedelta(hours=4))  # Georgia has no daylight saving time


def now_tbilisi() -> datetime:
    return datetime.now(TBILISI)


def now_iso() -> str:
    return now_tbilisi().isoformat(timespec="seconds")


def today_iso() -> str:
    return now_tbilisi().date().isoformat()


class Config(dict):
    """dict with dotted access helpers and resolved paths."""

    def path(self, key: str) -> Path:
        p = Path(self["paths"][key])
        return p if p.is_absolute() else REPO_ROOT / p

    def get_in(self, dotted: str, default=None):
        cur = self
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


@lru_cache(maxsize=1)
def _load(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        return Config(yaml.safe_load(fh))


def load_config(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.getenv("KG_CONFIG") or REPO_ROOT / "configs" / "kg.yaml")
    return _load(str(p))
