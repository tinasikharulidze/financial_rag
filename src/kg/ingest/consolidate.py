"""Replay parsed amendment operations on top of the original act -> ProvisionVersions with valid_from / valid_to.

``VersionedAct`` keeps, per provision path, a list of versions. Nothing is deleted: a repealed provision gets a final
version with ``status='repealed'``. ``as_of(date)`` assembles the consolidated text on the fly; no consolidated
full text is stored (docs/KG_ARCHITECTURE.md section 4.2).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from kg.ids import (display_path, path_sort_key, prov_id, pv_id, act_id, amend_id)
from kg.ingest.amendment_parser import ParsedInstruction
from kg.ingest.texttools import NewProv, level_of


@dataclass
class Version:
    path: str
    valid_from: str
    valid_to: str | None
    status: str                     # active | repealed
    text: str
    label: str = ""
    heading: str | None = None
    chapter: str | None = None
    level: str = "paragraph"
    source: str = "original"        # original | amendment | snapshot
    change_type: str = "added"      # added | modified | repealed
    amended_by: list = field(default_factory=list)   # [(amend_doc_id, instruction_id)]
    amendment_notes: list = field(default_factory=list)

    @property
    def id_suffix(self) -> str:
        return f"{self.path}@{self.valid_from}"


def is_descendant(path: str, base: str) -> bool:
    if path.startswith(base + "/"):
        return True
    last = base.split("/")[-1]
    return last.startswith("s") and path.startswith(base + ".")


class VersionedAct:
    def __init__(self, doc_id: str):
        self.doc_id = doc_id
        self.versions: dict[str, list[Version]] = {}
        self.log: list[dict] = []        # per-instruction outcome (for the consolidation suite)

    # ---------------------------------------------------------------- loading
    def load_base(self, provisions: list, valid_from: str, source: str = "original"):
        for p in provisions:
            self.versions[p["path"]] = [Version(
                path=p["path"], valid_from=valid_from, valid_to=None, status="active", text=p.get("text", ""),
                label=p.get("label", ""), heading=p.get("heading"), chapter=p.get("chapter"),
                level=p.get("level") or level_of(p["path"]), source=source, change_type="added",
                amendment_notes=p.get("amendment_notes", []))]

    # ---------------------------------------------------------------- queries
    def current(self, path: str) -> Version | None:
        vs = self.versions.get(path)
        return vs[-1] if vs else None

    def active_paths(self) -> list[str]:
        return [p for p, vs in self.versions.items() if vs[-1].status == "active"]

    def as_of(self, date: str) -> dict[str, Version]:
        out = {}
        for path, vs in self.versions.items():
            for v in vs:
                if v.valid_from <= date and (v.valid_to is None or date < v.valid_to) and v.status == "active":
                    out[path] = v
        return out

    def ordered(self, date: str) -> list[Version]:
        view = self.as_of(date)
        return [view[p] for p in sorted(view, key=path_sort_key)]

    def consolidated_text(self, date: str) -> str:
        parts = []
        for v in self.ordered(date):
            head = display_path(v.path)
            label = f"{v.label} " if v.label else ""
            heading = f" — {v.heading}" if v.heading else ""
            parts.append(f"[{head}]{heading}\n{label}{v.text}".strip())
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- mutation primitives
    def _put(self, path: str, date: str, amend: tuple[str, str], *, text=None, label=None, heading=..., chapter=None,
             status="active") -> Version:
        vs = self.versions.setdefault(path, [])
        last = vs[-1] if vs else None
        if last is not None:
            new = Version(path, date, None, status,
                          last.text if text is None else text,
                          last.label if label is None else label,
                          last.heading if heading is ... else heading,
                          last.chapter if chapter is None else chapter, last.level, "amendment", "modified",
                          [amend])
            if status == "repealed":
                new.text, new.change_type = "", "repealed"
            elif last.status == "repealed":
                new.change_type = "added"
            if (last.status, last.text, last.label, last.heading, last.chapter) == (
                    new.status, new.text, new.label, new.heading, new.chapter):
                return last                                  # no-op
            if last.valid_from == date:                      # two instructions of one amendment on one path
                last.status, last.text, last.label = new.status, new.text, new.label
                last.heading, last.chapter = new.heading, new.chapter
                last.change_type = new.change_type if last.change_type != "added" else "added"
                if amend not in last.amended_by:
                    last.amended_by.append(amend)
                return last
            last.valid_to = date
            vs.append(new)
            return new
        ch = chapter or self._inherit_chapter(path)
        v = Version(path, date, None, status, text or "", label or "", None if heading is ... else heading, ch,
                    level_of(path), "amendment", "added", [amend])
        vs.append(v)
        return v

    def _inherit_chapter(self, path: str) -> str | None:
        art = path.split("/")[0]
        if self.versions.get(art) and self.versions[art][-1].chapter:
            return self.versions[art][-1].chapter
        key = path_sort_key(art)
        best = None
        for p, vs in self.versions.items():
            if not vs or "/" in p or vs[-1].status != "active" or not p.startswith("art"):
                continue
            if path_sort_key(p) < key and (best is None or path_sort_key(p) > path_sort_key(best)):
                best = p
        return self.versions[best][-1].chapter if best else None

    def _descendants(self, base: str, active_only=True) -> list[str]:
        return [p for p, vs in self.versions.items() if is_descendant(p, base) and (not active_only or vs[-1].status == "active")]

    def _repeal(self, path: str, date: str, amend):
        n = 0
        for p in [path] + self._descendants(path):
            cur = self.current(p)
            if cur is not None and cur.status == "active":
                self._put(p, date, amend, status="repealed")
                n += 1
        return n

    # ---------------------------------------------------------------- applying one instruction
    def apply(self, ins: ParsedInstruction, date: str, amend_doc: str) -> dict:
        amend = (amend_doc, ins.instr_id)
        warnings = list(ins.warnings)
        touched = 0
        op = ins.op

        def put_new(news: list[NewProv], is_insert: bool):
            nonlocal touched
            new_paths = {n.path for n in news}
            for n in news:
                cur = self.current(n.path)
                if cur is None or cur.status == "repealed":
                    if not is_insert and cur is None:
                        warnings.append(f"replace_missing_target:{n.path}")
                elif is_insert:
                    warnings.append(f"insert_existing:{n.path}")
                self._put(n.path, date, amend, text=n.text, label=n.label,
                          heading=(n.heading if n.heading is not None else ...), chapter=n.chapter)
                touched += 1
            for n in news:
                for d in self._descendants(n.path):
                    if d not in new_paths:
                        self._put(d, date, amend, status="repealed")
                        touched += 1

        if op in ("REPLACE", "INSERT"):
            put_new(ins.new, op == "INSERT")
        elif op == "REPEAL":
            for t in ins.targets:
                n = self._repeal(t, date, amend) if self.current(t) is not None else 0
                if n == 0:
                    warnings.append(f"repeal_missing_target:{t}")
                touched += n
        elif op == "ARTICLE_TITLE":
            for n in ins.new:
                if self.current(n.path) is None:
                    warnings.append(f"title_missing_target:{n.path}")
                    continue
                self._put(n.path, date, amend, heading=n.heading)
                touched += 1
        elif op == "TEXT_SUB":
            for t in ins.targets:
                done = False
                for p in [t] + self._descendants(t):
                    cur = self.current(p)
                    if cur and cur.status == "active" and ins.find and ins.find in cur.text:
                        self._put(p, date, amend, text=cur.text.replace(ins.find, ins.repl or ""))
                        touched += 1
                        done = True
                if not done:
                    warnings.append(f"text_sub_not_found:{t}")
        elif op == "CHAPTER_REPEAL":
            for p, vs in list(self.versions.items()):
                if "/" not in p and p.startswith("art") and vs[-1].status == "active" and vs[-1].chapter == ins.chapter:
                    touched += self._repeal(p, date, amend)
        elif op == "CHAPTER_REPLACE":
            new_articles = {n.path for n in ins.new if "/" not in n.path}
            put_new(ins.new, False)
            for p, vs in list(self.versions.items()):
                if ("/" not in p and p.startswith("art") and vs[-1].status == "active"
                        and vs[-1].chapter == ins.chapter and p not in new_articles):
                    touched += self._repeal(p, date, amend)
        else:
            warnings.append(f"op_not_applied:{op}")
        outcome = {"amend": amend_doc, "instr": ins.instr_id, "op": op, "method": ins.method, "status": ins.status,
                   "touched": touched, "warnings": warnings, "head": ins.head[:140]}
        self.log.append(outcome)
        return outcome

    # ---------------------------------------------------------------- export
    def all_versions(self) -> list[Version]:
        return [v for p in sorted(self.versions, key=path_sort_key) for v in self.versions[p]]


# ---------------------------------------------------------------------- (de)serialisation of parsed instructions
def instr_to_dict(ins: ParsedInstruction) -> dict:
    return asdict(ins)


def instr_from_dict(d: dict) -> ParsedInstruction:
    d = dict(d)
    d["new"] = [NewProv(**n) for n in d.get("new", [])]
    return ParsedInstruction(**d)
