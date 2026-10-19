"""Amending acts -> instruction list -> typed operations (rules first, LLM fallback).

An amending act is a Matsne HTML document whose Article 1 holds numbered instructions such as::

    4. მე-5 მუხლის:
       ა) მე-5 პუნქტი ჩამოყალიბდეს შემდეგი რედაქციით:
       „5. ...“;
       ბ) მე-10 პუნქტის შემდეგ დაემატოს შემდეგი შინაარსის 10^1–10^3 პუნქტები:
       „10^1. ...

This module does three things:

1. ``extract_amendment``: read the HTML, split Article 1 into instructions (quote tracking, 3 label levels) and read
   the final articles (effective date, transitional provisions).
2. ``interpret``: turn one instruction into a ``ParsedInstruction`` (REPLACE / INSERT / REPEAL / TEXT_SUB, plus
   chapter operations and article-title changes). Target locations come from the Georgian ordinals in the head
   sentence; the paths of NEW provisions come from the labels in the quoted text.
3. ``llm_fallback``: for instructions the rules cannot resolve, ask the LLM for the same structure (purpose
   ``amend_parse``).

Tables, annexes and chapter titles are not modelled as provisions in the draft: such instructions are reported with
status ``unsupported`` (not ``unresolved``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

import chunker
from kg.ids import art_seg, find_ordinals, join_path, norm_sup, para_seg, roman_to_int, sub_seg
from kg.ingest.texttools import NewProv, classify_label, level_of, segment_lines, strip_quote_marks

MONTHS = {"იანვ": 1, "თებერვ": 2, "მარტ": 3, "აპრილ": 4, "მაის": 5, "ივნის": 6, "ივლის": 7, "აგვისტ": 8,
          "სექტემბ": 9, "ოქტომბ": 10, "ნოემბ": 11, "დეკემბ": 12}
RE_DATE = re.compile(r"(\d{4})\s*წლის\s*(\d{1,2})\s*([ა-ჰ]+)")
VERB = re.compile(r"ჩამოყალიბდეს|და(?:ე)?მატოს|ამოღებულ\s+იქნეს|ძალადაკარგულად|შეიცვალოს|ჩაითვალოს|ამოიღოს|გაუქმდეს")
RE_MATSNE_REF = re.compile(r"\(www\.matsne\.gov\.ge[^)]*\)")
NOUN = re.compile(r"(ქვეპუნქტ|პუნქტ|მუხლ|(?<![ა-ჰ])თავ(?![ა-ჰ])|(?<![ა-ჰ])თავი|(?<![ა-ჰ])თავის|(?<![ა-ჰ])თავს|(?<![ა-ჰ])თავში|სათაურ|დანართ|ცხრილ)[ა-ჰ]*")
LETTER_TOKEN = re.compile(r"[„“\"]\s*([ა-ჰ])(?:\.([ა-ჰ]))?(?:\^(\d+))?\.?\s*[“”\"]")
ROMAN_TOKEN = re.compile(r"(?<![A-Za-z])([IVXLC]{1,6})(?![A-Za-z])")


# ----------------------------------------------------------------------------- data classes
@dataclass
class Instruction:
    id: str                      # e.g. "4", "4.ა", "4.ბ.ა"
    level: int
    label: str
    head: str
    quotes: list[list[str]] = field(default_factory=list)
    context: list[str] = field(default_factory=list)   # heads of enclosing header instructions
    order_level: bool = False

    @property
    def raw(self) -> str:
        return self.head + "\n" + "\n".join("„" + "\n".join(q) for q in self.quotes)


@dataclass
class ParsedInstruction:
    instr_id: str
    op: str                                  # REPLACE | INSERT | REPEAL | TEXT_SUB | CHAPTER_REPLACE | CHAPTER_REPEAL | ARTICLE_TITLE | NONE
    status: str                              # parsed | unsupported | unresolved
    reason: str = ""
    method: str = "rules"
    targets: list[str] = field(default_factory=list)      # REPEAL / TEXT_SUB / ARTICLE_TITLE
    new: list[NewProv] = field(default_factory=list)      # REPLACE / INSERT / CHAPTER_REPLACE
    chapter: str | None = None
    find: str | None = None
    repl: str | None = None
    head: str = ""
    warnings: list[str] = field(default_factory=list)
    loc: dict = field(default_factory=dict)       # resolved location (for sibling hints)


@dataclass
class AmendingAct:
    doc_id: str
    meta: dict
    instructions: list[Instruction]
    effective_from: str | None
    effective_basis: str
    transitional_note: str
    applies_to_existing_from: str | None
    final_articles: dict[int, str]


# ----------------------------------------------------------------------------- helpers
def parse_ge_date(text: str) -> str | None:
    for m in RE_DATE.finditer(norm_sup(text)):
        mon = next((v for k, v in MONTHS.items() if m.group(3).startswith(k)), None)
        if mon:
            try:
                return date(int(m.group(1)), mon, int(m.group(2))).isoformat()
            except ValueError:
                continue
    return None


def _lines_from_blocks(blocks) -> list[str]:
    out = []
    for b in blocks:
        for line in b["text"].split("\n"):
            if line.strip():
                out.append(line.strip())
    return out


def _depth(s: str) -> int:
    return s.count("„") - s.count("“") - s.count("”")


def _closes_quote(line: str) -> bool:
    return bool(re.search(r"[“”]\s*[.;,]?\s*[.;,]?\s*$", line))


LABEL1 = re.compile(r"^(\d{1,3})\s*\.\s*(?=\S)")
LABEL2 = re.compile(r"^([ა-ჰ])\s*\)\s*")
LABEL3 = re.compile(r"^([ა-ჰ])\.([ა-ჰ])\s*\)\s*")


def _instr_label(line: str):
    m = LABEL3.match(line)
    if m:
        return 3, f"{m.group(1)}.{m.group(2)}", line[m.end():]
    m = LABEL2.match(line)
    if m:
        return 2, m.group(1), line[m.end():]
    m = LABEL1.match(line)
    if m:
        return 1, m.group(1), line[m.end():]
    return 0, "", line


def _strip_prefix(text: str) -> tuple[str, bool]:
    """Drop the '„title“ ... ბრძანებით (www.matsne...) დამტკიცებული' prefix. Returns (rest, order_level)."""
    m = RE_MATSNE_REF.search(text)
    if not m:
        return text, False
    before, rest = text[: m.start()], text[m.end():].strip()
    order_level = ("დებულებ" not in rest) and bool(re.search(r"ბრძანებ", before[-40:]))
    return rest, order_level


def _confirms_end(next_line: str | None) -> bool:
    """A line that ends with a closing quote really ends the quote when what follows is not quoted text."""
    if next_line is None:
        return True
    nl = next_line.strip()
    if nl.startswith("„"):
        return True
    lvl, lab, rest = _instr_label(nl)
    probe = rest if lvl else nl
    return bool(VERB.search(probe)) or probe.rstrip().endswith(":")


def split_instructions(lines: list[str]) -> list[Instruction]:
    """Split Article 1 into instructions. Quoted text („ ... “) is never searched for labels.

    A quote ends at a line that ends with a closing quote (optionally followed by . or ;) when the next line is an
    instruction (has an operative verb or ends with ':') or a new quote. Quote depth is NOT used, because the
    source text contains unbalanced nested quotes.
    """
    instrs: list[Instruction] = []
    stack: dict[int, Instruction] = {}
    cur: Instruction | None = None
    in_quote = False
    qlines: list[str] = []

    def close_quote():
        nonlocal in_quote, qlines
        if cur is not None and qlines:
            cur.quotes.append(qlines)
        in_quote, qlines = False, []

    for i, raw in enumerate(lines):
        line = raw.strip()
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if in_quote:
            qlines.append(line)
            if _closes_quote(line) and _confirms_end(nxt):
                close_quote()
            continue
        if line.startswith("„") or line.startswith("“"):
            in_quote = True
            body = line[1:].strip()
            qlines = [body] if body else []
            if body and _closes_quote(body) and _confirms_end(nxt):
                close_quote()
            continue
        lvl, lab, rest = _instr_label(line)
        if lvl:
            ins_id = ".".join(([stack[l].label for l in sorted(stack) if l < lvl] + [lab]))
            ins = Instruction(id=ins_id, level=lvl, label=lab, head=rest.strip(),
                              context=[stack[l].head for l in sorted(stack) if l < lvl])
            for l in [l for l in stack if l >= lvl]:
                del stack[l]
            stack[lvl] = ins
            instrs.append(ins)
            cur = ins
        else:
            rest2, ol = _strip_prefix(line)
            if cur is not None and not VERB.search(line) and not cur.quotes:
                cur.head = (cur.head + " " + line).strip()
            elif VERB.search(rest2):
                ins = Instruction(id=str(len(instrs) + 1), level=1, label=str(len(instrs) + 1), head=rest2,
                                  order_level=ol)
                stack = {1: ins}
                instrs.append(ins)
                cur = ins
            elif cur is not None:
                cur.head = (cur.head + " " + line).strip()
    if in_quote:
        close_quote()
    # header instructions ("4. მე-5 მუხლის:") only provide context for their children
    ids = [x.id for x in instrs]
    out = []
    for x in instrs:
        is_header = (not VERB.search(x.head)) and not x.quotes and any(o.startswith(x.id + ".") for o in ids)
        if not is_header:
            out.append(x)
    return out


# ----------------------------------------------------------------------------- reading the amending act
def extract_amendment(html: str, doc_id: str, published: str | None, edge_meta: dict | None = None) -> AmendingAct:
    soup = BeautifulSoup(html, "html.parser")
    md = soup.select_one("#maindoc")
    meta = chunker.doc_meta(soup, doc_id, None)
    articles: dict[int, list[dict]] = {}
    if md is not None:
        for e in md.find_all(id=True):
            prefix, suf = chunker.split_id(e["id"])
            keys = chunker.key_list(prefix)
            if (suf == "_Content" and keys and keys[-1][0] in ("ARTICLE", "POINT", "SUBPOINT")
                    and not any(k == "ENCLOSURE" for k, _ in keys) and any(k == "ARTICLE" for k, _ in keys)):
                n = int(dict(keys)["ARTICLE"])
                articles.setdefault(n, []).extend(chunker.part_blocks(e))
    lines = _lines_from_blocks(articles.get(1, []))
    lines = [norm_sup(l) for l in lines]
    # drop the "...შეტანილ იქნეს შემდეგი ცვლილება:" preamble (keep an instruction glued to the matsne reference)
    cleaned: list[str] = []
    glued_order_level = False
    for i, l in enumerate(lines):
        if i == 0 or not cleaned:
            rest, ol = _strip_prefix(l)
            if rest != l or "შეტანილ იქნეს" in l:
                if VERB.search(rest) and "შეტანილ იქნეს შემდეგი" not in rest:
                    glued_order_level = ol
                    cleaned.append(rest)
                continue
        cleaned.append(l)
    instrs = split_instructions(cleaned)
    if glued_order_level and instrs:
        instrs[0].order_level = True
    # final articles
    final_txt = {n: " ".join(l for l in _lines_from_blocks(b)) for n, b in articles.items() if n != 1}
    eff, basis = None, "published_date"
    effective_art = None
    for n in sorted(final_txt, reverse=True):
        if re.search(r"ამოქმედდეს", final_txt[n]):
            effective_art = n
            if re.search(r"გამოქვეყნებისთანავე", final_txt[n]):
                eff, basis = published, "on_publication"
            else:
                d = parse_ge_date(final_txt[n])
                if d:
                    eff, basis = d, "explicit_date"
            break
    if eff is None:
        eff = published or meta.get("adopted")
    trans_parts = [final_txt[n] for n in sorted(final_txt) if n != effective_art and final_txt[n].strip()
                   and not re.fullmatch(r"მუხლი\s*\d+\s*", final_txt[n])]
    trans = "\n".join(re.sub(r"^მუხლი\s*\d+\s*", "", t).strip() for t in trans_parts).strip()
    existing_from = None
    m = re.search(r"2\.[^.]*?(\d{4}\s*წლის\s*\d{1,2}\s*[ა-ჰ]+)[^.]*გავრცელდეს[^.]*", trans) if trans else None
    if trans:
        for sentence in re.split(r"(?<=[.;])\s+", trans):
            if "გავრცელდეს" in sentence and parse_ge_date(sentence):
                existing_from = parse_ge_date(sentence)
    return AmendingAct(doc_id, meta, instrs, eff, basis, trans, existing_from, final_txt)


# ----------------------------------------------------------------------------- interpretation
def _loc_from_span(parts):
    """parts: list of (kind, values). Returns dict(article=(n,sup), para=(n,sup), sub=(letter,sup,sub,subsup),
    chapter=roman) with the LAST value of each kind; plus lists for multi-targets."""
    loc: dict = {}
    multi: dict = {}
    for kind, vals in parts:
        if kind == "მუხლ":
            if vals:
                loc["article"] = vals[-1]
                multi["article"] = vals
        elif kind == "პუნქტ":
            if vals:
                loc["para"] = vals[-1]
                multi["para"] = vals
        elif kind == "ქვეპუნქტ":
            if vals:
                loc["sub"] = vals[-1]
                multi["sub"] = vals
        elif kind == "თავ":
            if vals:
                loc["chapter"] = vals[-1]
        else:
            loc[kind] = True
    return loc, multi


def parse_head_parts(head: str):
    """Walk the nouns (მუხლი, პუნქტი, ქვეპუნქტი, თავი ...) of a head and collect the ordinals before each."""
    head = norm_sup(head)
    pos, parts = 0, []
    for m in NOUN.finditer(head):
        span = head[pos:m.start()]
        pos = m.end()
        noun = m.group(1)
        key = ("თავ" if noun.startswith("თავ") else noun)
        if key == "ქვეპუნქტ":
            vals = []
            for lm in LETTER_TOKEN.finditer(span):
                vals.append((lm.group(1), int(lm.group(3)) if lm.group(3) and not lm.group(2) else None,
                             lm.group(2), int(lm.group(3)) if lm.group(3) and lm.group(2) else None))
        elif key == "თავ":
            vals = [roman_to_int(r) and r for r in ROMAN_TOKEN.findall(span)]
            vals = [v for v in vals if v]
        elif key in ("სათაურ", "დანართ", "ცხრილ"):
            vals = [True]
        else:
            vals = find_ordinals(span)
        parts.append((key, vals))
    return parts


def _paths_for(art, para, sub):
    segs = []
    if art:
        segs.append(art_seg(*art))
    if para:
        segs.append(para_seg(*para))
    if sub:
        segs.append(sub_seg(*sub))
    return join_path(*segs)


def _merge(ctx: dict, own: dict) -> dict:
    out = dict(ctx)
    for k in ("article", "para", "sub"):
        if k in own:
            out[k] = own[k]
            # a more specific own location drops deeper context
            if k == "article":
                out.pop("para", None)
                out.pop("sub", None) if "sub" not in own else None
            if k == "para":
                out.pop("sub", None) if "sub" not in own else None
    for k in ("chapter", "title", "annex", "table"):
        if k in own:
            out[k] = own[k]
    return out


def _context_loc(instr: Instruction) -> dict:
    ctx: dict = {}
    for h in instr.context:
        head, _ = _strip_prefix(h)
        v = VERB.search(head)
        head_part = head[: v.start()] if v else head
        loc, _ = _loc_from_span(parse_head_parts(head_part))
        ctx = _merge(ctx, loc)
    return ctx


def _interpret(instr: Instruction, hint: dict | None, out_loc: dict) -> ParsedInstruction:
    """``hint`` = location of the previous sibling instruction under the same header (its article/para carry over
    when this item names only a sub-point, e.g. "ბ) „თ“ ქვეპუნქტი ამოღებულ იქნეს")."""
    head, _ = _strip_prefix(instr.head)
    head = norm_sup(re.sub(r"\s+", " ", head)).strip()
    v = VERB.search(head)
    base = ParsedInstruction(instr.id, "NONE", "unresolved", head=head)
    if not v:
        base.reason = "no_operative_verb"
        return base
    verb = v.group(0)
    pre, post = head[: v.start()], head[v.end():]

    # kind of operation
    if re.match(r"ჩამოყალიბდეს", verb):
        op = "REPLACE"
    elif re.match(r"და(?:ე)?მატოს", verb):
        op = "INSERT"
    elif re.match(r"ამოღებულ|ძალადაკარგულად|ამოიღოს|გაუქმდეს", verb):
        op = "REPEAL"
    elif verb.startswith("შეიცვალოს"):
        op = "TEXT_SUB"
    else:
        base.reason = f"unsupported_verb:{verb}"
        return base

    anchor = re.search(r"(შემდეგ|შემდგომ)\s*$", pre.strip())
    pre_loc = pre[: anchor.start()] if anchor else pre
    if op == "INSERT" and not anchor:
        # "X მუხლს დაემატოს ... პუნქტი"  -> location is the parent
        pass

    parts = parse_head_parts(pre_loc)
    kinds = [k for k, _ in parts]
    if "დანართ" in kinds or "ცხრილ" in kinds or re.search(r"დანართ|ცხრილ", post):
        base.status, base.reason = "unsupported", "annex_or_table"
        return base
    if "სათაურ" in kinds and ("თავ" in kinds):
        base.status, base.reason = "unsupported", "chapter_title"
        return base
    own, multi = _loc_from_span(parts)
    ctx = _context_loc(instr)
    if hint and "sub" in own and "para" not in own and "para" not in ctx and "article" not in own:
        ctx = _merge(ctx, {k: v for k, v in hint.items() if k in ("article", "para")})
    loc = _merge(ctx, own)
    out_loc.update({k: v for k, v in loc.items() if k in ("article", "para", "sub")})

    # ---- chapter operations
    if "chapter" in own and "article" not in own and "para" not in own and "სათაურ" not in kinds:
        chapter = own["chapter"]
        if op == "REPEAL":
            return ParsedInstruction(instr.id, "CHAPTER_REPEAL", "parsed", chapter=chapter, head=head)
        if op == "REPLACE":
            lines = [l for q in instr.quotes for l in q]
            news, problems = segment_lines(lines, None, None, allow_articles=True, chapter=chapter)
            if not news:
                base.reason = "chapter_replace_without_text"
                return base
            return ParsedInstruction(instr.id, "CHAPTER_REPLACE", "parsed", new=news, chapter=chapter, head=head,
                                     warnings=problems)
        base.reason = "chapter_op_unsupported"
        return base
    if "chapter" in own and "სათაურ" in kinds:
        base.status, base.reason = "unsupported", "chapter_title"
        return base

    # ---- order-level article (amendment of the approving order itself)
    if instr.order_level and "article" in loc:
        art = loc["article"]
        path = f"ord{art[0]}"
        if op == "REPEAL":
            return ParsedInstruction(instr.id, "REPEAL", "parsed", targets=[path], head=head)
        base.reason = "order_level_edit_unsupported"
        return base

    # ---- article title
    if "title" in loc and "title" in own or "სათაურ" in kinds:
        if "article" not in loc:
            base.reason = "title_without_article"
            return base
        text = strip_quote_marks(" ".join(l for q in instr.quotes for l in q))
        text = re.sub(r"^მუხლი\s+\d+(?:\^\d+)?\s*[.:]?\s*", "", text).strip()
        if not text:
            base.reason = "title_without_text"
            return base
        art_path = _paths_for(loc["article"], None, None)
        return ParsedInstruction(instr.id, "ARTICLE_TITLE", "parsed", targets=[art_path],
                                 new=[NewProv(art_path, heading=text, level="article")], head=head)

    # ---- which level is the target?
    if "sub" in own:
        level = "sub"
    elif "para" in own:
        level = "para"
    elif "article" in own:
        level = "article"
    elif ctx:
        level = "sub" if ("sub" in loc and "sub" not in ctx) else ("para" if "para" in loc else "article")
    else:
        base.reason = "no_target_found"
        return base
    # for inserts the noun AFTER the verb tells what is added (e.g. "... მუხლის შემდეგ დაემატოს ... 9^1 მუხლი")
    if op == "INSERT":
        n = NOUN.search(post)
        nouns = [m.group(1) for m in NOUN.finditer(post)]
        if "ქვეპუნქტ" in nouns:
            level = "sub"
        elif "პუნქტ" in nouns:
            level = "para"
        elif "მუხლ" in nouns:
            level = "article"

    if "article" not in loc and not (op == "INSERT" and level == "article"):
        base.reason = "article_not_resolved"
        return base

    art_seg_s = art_seg(*loc["article"]) if "article" in loc else None
    para_seg_s = para_seg(*loc["para"]) if "para" in loc else None

    # ---- REPEAL
    if op == "REPEAL":
        targets: list[str] = []
        if level == "sub":
            for s in multi.get("sub", [loc["sub"]]) if "sub" in own else [loc["sub"]]:
                targets.append(join_path(art_seg_s, para_seg_s, sub_seg(*s)))
        elif level == "para":
            for p in multi.get("para", [loc["para"]]) if "para" in own else [loc["para"]]:
                targets.append(join_path(art_seg_s, para_seg(*p)))
        else:
            for a in multi.get("article", [loc["article"]]) if "article" in own else [loc["article"]]:
                targets.append(art_seg(*a))
        return ParsedInstruction(instr.id, "REPEAL", "parsed", targets=targets, head=head)

    # ---- TEXT_SUB: „old“ ... „new“
    if op == "TEXT_SUB":
        toks = re.findall(r"[„“\"]([^„“”\"]+)[“”\"]", pre + " " + post)
        if len(toks) < 2:
            base.reason = "text_sub_tokens_missing"
            return base
        path = _paths_for(loc.get("article"), loc.get("para") if level != "article" else None,
                          loc.get("sub") if level == "sub" else None)
        return ParsedInstruction(instr.id, "TEXT_SUB", "parsed", targets=[path], find=toks[0], repl=toks[1],
                                 head=head)

    # ---- REPLACE / INSERT: new provisions come from the quoted text
    lines = [l for q in instr.quotes for l in q]
    if not lines:
        base.reason = "no_quoted_text"
        return base
    if lines:
        lines[-1] = re.sub(r"\s*[“”]\s*[.;,]*\s*$", "", lines[-1]).strip()
    default_path = None
    if op == "REPLACE":
        default_path = _paths_for(loc.get("article"), loc.get("para") if level != "article" else None,
                                  loc.get("sub") if level == "sub" else None)
    allow_articles = level == "article"
    news, problems = segment_lines(lines, art_seg_s, para_seg_s, default_path=default_path,
                                   allow_articles=allow_articles)
    if op == "REPLACE" and level == "article" and art_seg_s and news and art_seg_s not in {n.path for n in news}:
        news.insert(0, NewProv(art_seg_s, level="article"))      # blank the old lead text of a replaced article
    if not news:
        base.reason = "no_provisions_in_quote:" + ",".join(problems)
        return base
    # sanity: a paragraph/sub-level instruction must not produce provisions outside its article
    if art_seg_s and any(not (p.path == art_seg_s or p.path.startswith(art_seg_s + "/")) for p in news) and not allow_articles:
        base.reason = "labels_outside_target_article"
        return base
    return ParsedInstruction(instr.id, op, "parsed", new=news, head=head, warnings=problems)


def interpret(instr: Instruction, hint: dict | None = None) -> ParsedInstruction:
    out_loc: dict = {}
    res = _interpret(instr, hint, out_loc)
    res.loc = out_loc
    return res


# ----------------------------------------------------------------------------- LLM fallback
class LLMOp(BaseModel):
    op: str = Field(description="REPLACE | INSERT | REPEAL | TEXT_SUB")
    path: str = Field(description="provision path, e.g. art6/p5/sბ.ე, art9_1, art4/p3")
    label: str = ""
    heading: str | None = None
    text: str = ""
    find: str | None = None
    replace: str | None = None


class LLMOps(BaseModel):
    ops: list[LLMOp] = []
    unsupported_reason: str | None = None


FALLBACK_SYSTEM = """You convert one amendment instruction of a Georgian legal act into machine-readable operations.
Paths: article 6 = art6, article 9^1 = art9_1, paragraph 4 of article 6 = art6/p4, paragraph 4^1 = art6/p4.1,
sub-point ბ of paragraph 5 = art6/p5/sბ, nested sub-point ბ.ე = art6/p5/sბ.ე.
Operations: REPLACE (new text of ONE provision), INSERT (a new provision), REPEAL (path, no text),
TEXT_SUB (find/replace inside a provision). Emit one operation per provision; split quoted text so that each
paragraph and each lettered sub-point is its own operation (text WITHOUT the leading label, label in `label`).
Use ONLY the table of contents to resolve ordinals. If the instruction concerns tables, annexes or chapter titles,
return no ops and set unsupported_reason. Return JSON: {"ops": [...], "unsupported_reason": null}."""


def llm_fallback(llm, instr: Instruction, toc: str) -> ParsedInstruction:
    from kg.llm import LLMValidationError, system_user

    user = (f"Instruction head:\n{instr.head}\n\nEnclosing instructions:\n" + "\n".join(instr.context) +
            "\n\nQuoted text:\n" + "\n---\n".join("\n".join(q) for q in instr.quotes) +
            f"\n\nTable of contents of the amended act (article: paragraph numbers):\n{toc}")
    try:
        res = llm.call("amend_parse", system_user(FALLBACK_SYSTEM, user), LLMOps, max_tokens=4096)
    except LLMValidationError as exc:
        return ParsedInstruction(instr.id, "NONE", "unresolved", reason=f"llm_invalid:{str(exc)[:80]}", method="llm")
    out = res.parsed
    if not out.ops:
        status = "unsupported" if out.unsupported_reason else "unresolved"
        return ParsedInstruction(instr.id, "NONE", status, reason=out.unsupported_reason or "llm_no_ops", method="llm")
    ops = {o.op.upper() for o in out.ops}
    news = [NewProv(o.path, o.label, o.heading, o.text, level=level_of(o.path))
            for o in out.ops if o.op.upper() in ("REPLACE", "INSERT")]
    targets = [o.path for o in out.ops if o.op.upper() in ("REPEAL", "TEXT_SUB")]
    if ops == {"REPEAL"}:
        return ParsedInstruction(instr.id, "REPEAL", "parsed", targets=targets, method="llm")
    if ops <= {"REPLACE", "INSERT"}:
        return ParsedInstruction(instr.id, "REPLACE" if "REPLACE" in ops else "INSERT", "parsed", new=news,
                                 method="llm")
    if ops == {"TEXT_SUB"}:
        o = out.ops[0]
        return ParsedInstruction(instr.id, "TEXT_SUB", "parsed", targets=targets, find=o.find, repl=o.replace,
                                 method="llm")
    return ParsedInstruction(instr.id, "NONE", "unresolved", reason="llm_mixed_ops", method="llm")


def interpret_all(instrs: list[Instruction]) -> list[ParsedInstruction]:
    """Interpret in order, chaining the location of siblings (same parent id) as a hint."""
    out, last_by_parent = [], {}
    for ins in instrs:
        parent = ins.id.rsplit(".", 1)[0] if "." in ins.id else ""
        res = interpret(ins, last_by_parent.get(parent))
        if res.loc:
            last_by_parent[parent] = res.loc
        out.append(res)
    return out
