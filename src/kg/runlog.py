"""Run bookkeeping: every CLI job creates a ``runs`` row and a JSONL log in ``data/kg/runs/<run_id>.jsonl``."""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path

from kg.config import now_iso, now_tbilisi


def new_run_id(kind: str) -> str:
    return f"{kind}-{now_tbilisi().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class Run:
    """Context manager: ``with Run(store, cfg, 'parse-acts', params) as run: run.log('event', k=v)``."""

    def __init__(self, store, runs_dir: Path | str, kind: str, params: dict | None = None, run_id: str | None = None):
        self.store = store
        self.kind = kind
        self.params = params or {}
        self.id = run_id or new_run_id(kind)
        self.dir = Path(runs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.dir / f"{self.id}.jsonl"
        self.status = "ok"
        self.counters: dict[str, int] = {}

    def __enter__(self):
        self.store.start_run(self.id, self.kind, self.params)
        self.log("run_start", kind=self.kind, params=self.params)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            status = self.status
        elif exc_type.__name__ == "BudgetExhausted":
            status = "partial"
            self.log("budget_exhausted", message=str(exc))
        else:
            status = "failed"
            self.log("error", type=exc_type.__name__, message=str(exc))
        self.log("run_end", status=status, counters=self.counters)
        self.store.finish_run(self.id, status)
        return False  # never swallow

    def log(self, event: str, **data):
        rec = {"ts": now_iso(), "event": event, **data}
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def count(self, key: str, n: int = 1):
        self.counters[key] = self.counters.get(key, 0) + n

    def write_json(self, suffix: str, obj) -> Path:
        p = self.dir / f"{self.id}_{suffix}.json"
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return p
