"""Offline jobs of Phase 1: parse-acts, parse-amendments, consolidate, extract-refs."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from kg.ids import act_id, amend_id, display_path, norm_sup, path_sort_key, prov_id, pv_id
from kg.ingest.act_parser import parse_act_html
from kg.ingest.titles import heading_at, make_index_text, provision_title
from kg.ingest.amendment_parser import (extract_amendment, interpret_all, llm_fallback)
from kg.ingest.consolidate import VersionedAct, instr_from_dict, instr_to_dict

META_KEYS = ["doc_id", "url", "number", "type", "authority", "adopted", "published", "effective_from",
             "registration_code", "title"]


def parsed_dir(cfg, sub: str) -> Path:
    p = cfg.path("data_dir") / "parsed" / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]


def amendment_edges(cfg, base_doc: str) -> list[dict]:
    rows = [r for r in read_jsonl(cfg.path("amendment_edges")) if str(r["base_doc_id"]) == str(base_doc)]
    return sorted(rows, key=lambda r: r["seq"])


# ============================================================================ graph writing
def write_act_graph(store, doc_id: str, va: VersionedAct, act_props: dict):
    """(Re)write Act, Provision, ProvisionVersion nodes and HAS_PROVISION / VERSION_OF / UPDATES / AMENDED_BY edges."""
    store.purge_act(doc_id)
    act_short = (act_props or {}).get("short")
    nodes, edges = [(act_id(doc_id), "Act", act_props, None, None)], []
    for order, path in enumerate(sorted(va.versions, key=path_sort_key)):
        vs = va.versions[path]
        if not vs:
            continue
        pid = prov_id(doc_id, path)
        newest = next((x for x in reversed(vs) if x.status == "active"), vs[-1])
        nodes.append((pid, "Provision", {
            "act": doc_id, "path": path, "level": vs[0].level, "order": order, "placeholder": False,
            "display": display_path(path),
            "title": provision_title(path, newest.label, newest.text, heading_at(va.versions, path, newest.valid_from))},
            vs[0].valid_from, None))
        edges.append((act_id(doc_id), pid, "HAS_PROVISION", {"order": order}, None, None))
        prev = None
        for v in vs:
            vid = pv_id(doc_id, path, v.valid_from)
            art_heading = heading_at(va.versions, path, v.valid_from)
            nodes.append((vid, "ProvisionVersion", {
                "act": doc_id, "path": path, "text": v.text, "label": v.label, "heading": v.heading,
                "chapter": v.chapter, "status": v.status, "source": v.source, "change_type": v.change_type,
                "level": v.level, "amendment_notes": v.amendment_notes, "order": order,
                "article_title": art_heading, "act_short": act_short, "display": display_path(path),
                "title": provision_title(path, v.label, v.text, art_heading),
                "index_text": make_index_text(act_short, path, art_heading, v.label, v.text)},
                v.valid_from, v.valid_to))
            edges.append((vid, pid, "VERSION_OF", {}, None, None))
            if prev is not None:
                edges.append((vid, prev, "UPDATES", {"change_type": v.change_type}, v.valid_from, None))
            for adoc, instr in v.amended_by:
                edges.append((vid, amend_id(adoc), "AMENDED_BY", {"instruction_id": instr}, v.valid_from, None,
                              f"{vid}|AMENDED_BY|{amend_id(adoc)}#{instr}"))
            prev = vid
    store.upsert_nodes(nodes)
    store.upsert_edges(edges)
    return len(nodes)


def _base_date(cfg, doc_id: str, meta: dict) -> str:
    mode = cfg["acts"][doc_id]["mode"]
    if mode == "snapshot":
        return cfg["acts"][doc_id]["snapshot_date"]
    return meta.get("effective_from") or meta.get("published") or meta.get("adopted")


# ============================================================================ parse-acts
def parse_acts(cfg, store, run) -> dict:
    summary = {}
    for doc_id, ac in cfg["acts"].items():
        doc_id = str(doc_id)
        html = (cfg.path("raw_html") / f"{doc_id}.html").read_text(encoding="utf-8")
        snapshot = ac["mode"] == "snapshot"
        pa = parse_act_html(html, doc_id, strip_notes=snapshot)
        valid_from = _base_date(cfg, doc_id, pa.meta)
        prov_dicts = [asdict(p) for p in pa.provisions]
        (parsed_dir(cfg, "acts") / f"{doc_id}.json").write_text(json.dumps({
            "doc_id": doc_id, "mode": ac["mode"], "valid_from": valid_from,
            "meta": {k: pa.meta.get(k) for k in META_KEYS}, "stats": pa.stats, "provisions": prov_dicts},
            ensure_ascii=False, indent=1), encoding="utf-8")
        va = VersionedAct(doc_id)
        va.load_base(prov_dicts, valid_from, "snapshot" if snapshot else "original")
        act_props = {**{k: pa.meta.get(k) for k in META_KEYS}, "doc_id": doc_id, "mode": ac["mode"],
                     "short": ac.get("short"), "base_valid_from": valid_from}
        n = write_act_graph(store, doc_id, va, act_props)
        summary[doc_id] = {**pa.stats, "nodes": n, "valid_from": valid_from, "mode": ac["mode"]}
        run.log("parse_act", doc_id=doc_id, **summary[doc_id])
        run.count("acts")
    return summary


# ============================================================================ parse-amendments
def toc_of(provisions: list[dict]) -> str:
    arts: dict[str, list[str]] = {}
    for p in provisions:
        parts = p["path"].split("/")
        if parts[0].startswith("art"):
            arts.setdefault(parts[0], [])
            if len(parts) > 1 and parts[1].startswith("p") and parts[1] not in arts[parts[0]]:
                arts[parts[0]].append(parts[1])
    return "\n".join(f"{a}: {' '.join(ps)}" for a, ps in sorted(arts.items(), key=lambda kv: path_sort_key(kv[0])))


def parse_amendments(cfg, store, run, llm=None, dry_run: bool = False) -> dict:
    out = {"instructions": 0, "rules": 0, "llm": 0, "unsupported": 0, "unresolved": 0, "fallback_needed": 0}
    docs = {str(r["doc_id"]): r for r in read_jsonl(cfg.path("amendment_docs"))}
    for base, ac in cfg["acts"].items():
        base = str(base)
        snapshot = ac["mode"] == "snapshot"
        base_json = parsed_dir(cfg, "acts") / f"{base}.json"
        provisions = json.loads(base_json.read_text(encoding="utf-8"))["provisions"] if base_json.exists() else []
        for e in amendment_edges(cfg, base):
            d = str(e["amendment_doc_id"])
            html = (cfg.path("raw_html") / f"{d}.html").read_text(encoding="utf-8")
            am = extract_amendment(html, d, e.get("published_date"))
            parsed = []
            if not snapshot:
                for pi in interpret_all(am.instructions):
                    out["instructions"] += 1
                    if pi.status == "unresolved":
                        out["fallback_needed"] += 1
                        if llm is not None and not dry_run:
                            ins = next(i for i in am.instructions if i.id == pi.instr_id)
                            fb = llm_fallback(llm, ins, toc_of(provisions))
                            if fb.status != "unresolved":
                                fb.head = pi.head
                                pi = fb
                    if pi.status == "parsed":
                        out["llm" if pi.method == "llm" else "rules"] += 1
                    else:
                        out[pi.status] += 1
                    parsed.append(pi)
            data = {
                "doc_id": d, "base_doc_id": base, "seq": e["seq"], "number": e["amendment_number"],
                "amendment_date": e["amendment_date"], "published": e.get("published_date"),
                "effective_from": am.effective_from, "effective_basis": am.effective_basis,
                "has_transitional": bool(e.get("has_transitional")) or bool(am.transitional_note),
                "transitional_note": am.transitional_note, "applies_to_existing_from": am.applies_to_existing_from,
                "title": e.get("title"), "url": e.get("url"), "snapshot_only": snapshot,
                "instructions": [instr_to_dict(p) for p in parsed]}
            (parsed_dir(cfg, "amendments") / f"{d}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            if not dry_run:
                store.upsert_node(amend_id(d), "AmendingAct", {
                    "doc_id": d, "number": e["amendment_number"], "date": e["amendment_date"],
                    "published": e.get("published_date"), "effective_from": am.effective_from,
                    "effective_basis": am.effective_basis, "has_transitional": data["has_transitional"],
                    "transitional_note": am.transitional_note,
                    "applies_to_existing_from": am.applies_to_existing_from, "title": e.get("title"),
                    "url": e.get("url"), "base_act": base, "seq": e["seq"]}, am.effective_from, None)
                store.upsert_edge(amend_id(d), act_id(base), "AMENDS", {"seq": e["seq"]}, am.effective_from)
            run.log("parse_amendment", doc_id=d, base=base, seq=e["seq"], effective_from=am.effective_from,
                    instructions=len(parsed), statuses=dict(Counter(p.status for p in parsed)))
    run.count("amendments")
    return out


# ============================================================================ consolidate
def consolidate(cfg, store, run) -> dict:
    summary = {}
    for base, ac in cfg["acts"].items():
        base = str(base)
        if ac["mode"] != "versioned":
            continue
        base_data = json.loads((parsed_dir(cfg, "acts") / f"{base}.json").read_text(encoding="utf-8"))
        va = VersionedAct(base)
        va.load_base(base_data["provisions"], base_data["valid_from"], "original")
        unresolved, applied = [], 0
        for e in amendment_edges(cfg, base):
            d = str(e["amendment_doc_id"])
            am = json.loads((parsed_dir(cfg, "amendments") / f"{d}.json").read_text(encoding="utf-8"))
            for raw in am["instructions"]:
                ins = instr_from_dict(raw)
                if ins.status != "parsed":
                    unresolved.append({"amendment": d, "instruction": ins.instr_id, "status": ins.status,
                                       "reason": ins.reason, "head": ins.head[:200]})
                    va.log.append({"amend": d, "instr": ins.instr_id, "op": ins.op, "method": ins.method,
                                   "status": ins.status, "touched": 0, "warnings": [ins.reason], "head": ins.head[:140]})
                    continue
                va.apply(ins, am["effective_from"], d)
                applied += 1
        amends = store.nodes("AmendingAct", base_act=base)
        act_node = store.get_node(act_id(base))
        act_props = act_node["props"] if act_node else {"doc_id": base}
        n = write_act_graph(store, base, va, act_props)
        versions = sum(len(v) for v in va.versions.values())
        summary[base] = {"provisions": len(va.versions), "versions": versions, "active_now": len(va.active_paths()),
                         "amendments": len(amends), "applied": applied, "unresolved": len(unresolved)}
        (parsed_dir(cfg, "consolidation") / f"{base}.json").write_text(json.dumps(
            {"summary": summary[base], "log": va.log, "unresolved": unresolved}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        run.log("consolidate", doc_id=base, **summary[base])
        if unresolved:
            run.write_json(f"unresolved_{base}", unresolved)
    return summary


# ============================================================================ extract-refs
ORD = r"(?:მე-\d+|\d+-ე|\d+(?:\^\d+)?|პირველ\w*)(?:\^\d+)?"
RE_ART_REF = re.compile(
    rf"(?P<scope>ამ\s+დებულების\s+|ამავე\s+დებულების\s+|ამ\s+მუხლის\s+|ამავე\s+მუხლის\s+)?(?P<art>{ORD})(?:\s*[–-]\s*(?P<art2>{ORD}))?\s+მუხლ[ა-ჰ]*"
    rf"(?:\s+(?P<para>{ORD})\s+პუნქტ[ა-ჰ]*)?(?:\s+„(?P<sub>[ა-ჰ](?:\.[ა-ჰ])?)“\s*ქვეპუნქტ[ა-ჰ]*)?")
RE_REL_ART = re.compile(rf"(?P<scope>ამ\s+მუხლის|ამავე\s+მუხლის)\s+(?P<para>{ORD})\s+პუნქტ[ა-ჰ]*(?:\s+„(?P<sub>[ა-ჰ](?:\.[ა-ჰ])?)“\s*ქვეპუნქტ[ა-ჰ]*)?")
RE_EXT_NO = re.compile(r"№\s*(\d+/\d+)")
RE_EXT_DATE = re.compile(r"(\d{4})\s*წლის\s*(\d{1,2})\s*([ა-ჰ]+)")
RE_EXT_LAW = re.compile(r"(?:„(?P<t1>[^„“”]{8,200})[“”]\s*საქართველოს\s+(?:ორგანულ\w*\s+)?კანონ\w*|კანონ\w*\s+„(?P<t2>[^„“”]{8,200})[“”])")


def _ext_tail_segments(tail: str) -> list[str] | None:
    """Article / paragraph named right after an external law ("... კანონის მე-5 მუხლის მე-2 პუნქტი")."""
    from kg.ids import join_path
    m = RE_ART_REF.match(tail.strip(" ,"))
    if not m or (m.group("scope") or "").strip():
        return None
    art = _seg_from_ord(m.group("art"), "art")
    if not art:
        return None
    segs = [art]
    if m.group("para"):
        para = _seg_from_ord(m.group("para"), "para")
        if para:
            segs.append(para)
    return segs


def _seg_from_ord(token: str, kind: str) -> str | None:
    from kg.ids import parse_ordinal, art_seg, para_seg
    o = parse_ordinal(token)
    if not o:
        return None
    return art_seg(*o) if kind == "art" else para_seg(*o)


def extract_refs(cfg, store, run) -> dict:
    from kg.ids import sub_seg, join_path
    from kg.ingest.texttools import level_of
    stats = Counter()
    acts = {n["id"].split(":")[1]: n for n in store.nodes("Act")}
    by_number = {}
    for doc, n in acts.items():
        by_number.setdefault(n["props"].get("number"), []).append(doc)
    prov_paths = {}
    for p in store.nodes("Provision"):
        prov_paths.setdefault(str(p["props"]["act"]), set()).add(p["props"]["path"])
    rows_csv = {}
    node_rows, edge_rows = [], []
    store.delete_edges("REFERS_TO")
    for pv in store.nodes("ProvisionVersion"):
        props = pv["props"]
        if props.get("status") != "active" or not props.get("text"):
            continue
        act, path = str(props["act"]), props["path"]
        if not path.startswith(("art", "ann")):
            continue
        text = norm_sup(props["text"])
        cur_art = path.split("/")[0]
        taken: list[tuple[int, int]] = []

        def add_edge(dst: str, ref_text: str, method: str, match_type: str, title: str = "", doc: str = ""):
            edge_rows.append((prov_id(act, path), dst, "REFERS_TO", {"ref_text": ref_text, "method": method},
                              pv["valid_from"], pv["valid_to"], f"{prov_id(act, path)}|REFERS_TO|{dst}@{pv['valid_from']}"))
            stats[match_type] += 1
            rows_csv[(act, doc, title, match_type)] = rows_csv.get((act, doc, title, match_type), 0) + 1

        def link_internal(act_: str, src_path: str, wanted: str, ref_text: str):
            """Edge to the EXACT referenced provision. If that path does not exist in the act, a placeholder
            Provision (no text) is created and ``resolved_to`` names the nearest existing ancestor."""
            have = prov_paths.get(act_, set())
            near = wanted
            while near and near not in have:
                near = "/".join(near.split("/")[:-1])
            if not near:
                return
            anchor = near                                # existing node we resolve to (== wanted when it exists)
            if anchor == src_path or src_path.startswith(anchor + "/"):
                return                                   # reference to itself / its own ancestor
            if wanted == near:
                add_edge(prov_id(act_, wanted), ref_text, "regex", "internal", wanted, act_)
                return
            pid = prov_id(act_, wanted)
            node_rows.append((pid, "Provision", {
                "act": act_, "path": wanted, "level": level_of(wanted), "order": None, "placeholder": True,
                "display": display_path(wanted), "title": display_path(wanted) + " (ტექსტი მიუწვდომელია)"},
                None, None))
            add_edge(pid, ref_text, "regex", "internal_placeholder", wanted, act_)
            edge_rows[-1][3]["resolved_to"] = prov_id(act_, near)

        # --- external acts by order number
        for m in RE_EXT_NO.finditer(text):
            num = m.group(1)
            ctx = text[max(0, m.start() - 260): m.start()]
            dm = list(RE_EXT_DATE.finditer(ctx))
            year = dm[-1].group(1) if dm else None
            taken.append((max(0, m.start() - 260), m.end() + 80))
            targets = [d for d in by_number.get(num, [])
                       if year is None or str(acts[d]["props"].get("adopted") or "").startswith(year)]
            tq = re.findall(r"„([^„“”]{8,200})[“”]", ctx)
            title = tq[-1] if tq else ""
            if targets and targets[0] != act:
                add_edge(act_id(targets[0]), m.group(0), "regex", "external_in_corpus", title, targets[0])
            elif not targets:
                key = f"ext:{num}" + (f"@{year}" if year else "")
                node_rows.append((key, "ExternalAct", {"number": num, "year": year, "title": title}, None, None))
                add_edge(key, m.group(0), "regex", "external_number", title)
        # --- external laws by title (+ the article / paragraph named right after the law, as a placeholder node)
        for m in RE_EXT_LAW.finditer(text):
            title = (m.group("t1") or m.group("t2") or "").strip()
            key = "ext:law:" + re.sub(r"\s+", " ", title.lower())
            node_rows.append((key, "ExternalAct", {"title": title, "kind": "law"}, None, None))
            segs_ext = _ext_tail_segments(text[m.end(): m.end() + 140])
            if segs_ext:
                epath = join_path(*segs_ext)
                ekey = f"{key}/{epath}"
                node_rows.append((ekey, "ExternalProvision", {
                    "act_key": key, "path": epath, "placeholder": True, "display": display_path(epath),
                    "title": f"{title} › {display_path(epath)}"}, None, None))
                edge_rows.append((ekey, key, "PART_OF", {}, None, None))
                add_edge(ekey, m.group(0)[:120], "regex", "external_provision", title)
            else:
                add_edge(key, m.group(0)[:120], "regex", "external_title", title)
            taken.append((m.start(), m.end() + 100))
        # --- internal article / paragraph refs
        for m in RE_ART_REF.finditer(text):
            if any(a <= m.start() < b for a, b in taken) and not (m.group("scope") or "").startswith("ამ"):
                continue
            scope = (m.group("scope") or "").strip()
            if scope.startswith(("ამ მუხლ", "ამავე მუხლ")):
                continue                      # "ამ მუხლის" is handled by RE_REL_ART below
            art = _seg_from_ord(m.group("art"), "art")
            if not art:
                continue
            segs = [art]
            if m.group("para"):
                segs.append(_seg_from_ord(m.group("para"), "para"))
            if m.group("sub"):
                a, _, b = m.group("sub").partition(".")
                segs.append(sub_seg(a, None, b or None))
            link_internal(act, path, join_path(*segs), m.group(0))
        for m in RE_REL_ART.finditer(text):
            segs = [cur_art, _seg_from_ord(m.group("para"), "para")]
            if m.group("sub"):
                a, _, b = m.group("sub").partition(".")
                segs.append(sub_seg(a, None, b or None))
            link_internal(act, path, join_path(*[s for s in segs if s]), m.group(0))
    if node_rows:
        store.upsert_nodes(node_rows)
    store.upsert_edges(edge_rows)
    # cross_references.csv (keeps the existing header)
    csv_path = cfg.path("cross_references")
    header = ["source_file", "source_document_id", "target_document_id", "target_title", "match_type"]
    if csv_path.exists():
        first = csv_path.read_text(encoding="utf-8").splitlines()[:1]
        if first:
            header = first[0].strip().split(",")
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for (src, doc, title, mt), n in sorted(rows_csv.items()):
            w.writerow([f"{src}.html", src, doc, title, mt])
    run.log("extract_refs", edges=len(edge_rows), **dict(stats))
    return dict(stats)
