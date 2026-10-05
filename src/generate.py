#!/usr/bin/env python3
"""
RAG generation for the Matsne corpus.

Commands:

ask     Run RAG evaluation for one row or a row range from the precomputed
        evaluation questions.

bakeoff Run several models over evaluation questions with frozen contexts
        and score them.

For `ask`, the question embedding is read from the precomputed
eval_questions_bge_m3.jsonl file. BGE-M3 is therefore NOT loaded at runtime.

Examples:

    uv run src/generate.py ask 10

    uv run src/generate.py ask 10:20

    uv run src/generate.py bakeoff \
        --children matsne_data/embeddings/children_bge_m3.jsonl \
        --questions matsne_data/embeddings/eval_questions_bge_m3.jsonl \
        --parents matsne_data/chunks/parents.jsonl \
        --models "gemini/gemini-2.5-flash"

For `ask`, the output is written to:

    matsne_data/generation/evaluation_results.csv
"""

import argparse
import hashlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import numpy as np


REFUSAL = "ამ კითხვაზე პასუხის მოძიება ვერ შევძელი, სცადეთ კითხვის უფრო დაწვრილებით დასმა."


# ============================================================
# PROMPTS
# ============================================================

SYSTEM_PROMPT = f"""You answer questions about Georgian financial and administrative regulations using ONLY the passages provided.

Rules:
1. Answer in Georgian, concisely and precisely. Quote numbers, dates, thresholds and deadlines exactly as they appear.
2. Use only the provided passages. Do not use outside knowledge.
3. If the provided passages do not contain enough information to answer the question, return this JSON:
   {{"answer": "{REFUSAL}", "sources": []}}
4. Each passage header contains its parent/chunk id in square brackets.
5. Identify ONLY the passage ids that are necessary to support your answer. Do not include passages that are merely related or that you did not need to answer the question.
6. If one passage is sufficient to answer the question, include only that passage id.
7. If passages come from different documents or versions, distinguish them clearly, prefer the rule currently in force, and mention when a rule is repealed or not yet in force.
8. If the question concerns two documents, answer for each separately and include only the sources necessary for each answer.

Return JSON ONLY. Do not write anything before or after the JSON.

Use exactly this structure:

{{
  "answer": "<your answer in Georgian>",
  "sources": ["parent_id_1", "parent_id_2"]
}}

The "sources" list must contain only ids that appear in the provided passages.
Do not put source ids inside the answer itself.
"""

JUDGE_PROMPT = """You are grading an answer to a question about Georgian regulations.
Compare the MODEL ANSWER with the REFERENCE ANSWER. Judge only factual agreement (numbers, dates, conditions); ignore wording and citations.
Return JSON only: {"verdict": "correct" | "partial" | "incorrect", "reason": "<one short sentence>"}"""


CITE_RE = re.compile(r"\b(\d{4,10}:\d{4})\b")


# ============================================================
# DATA
# ============================================================

def load_jsonl(p):
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def ids(s):
    s = "" if s is None else str(s)
    return [] if s == "nan" else [x.strip() for x in s.split(";") if x.strip()]


def parse_row_range(value):
    """
    Parse:
        10       -> row 10
        10:20    -> rows 10 through 20 inclusive

    Rows are 1-based, matching normal spreadsheet numbering.
    """
    value = str(value).strip()

    if ":" not in value:
        try:
            n = int(value)
        except ValueError:
            raise SystemExit("Row must be an integer or range such as 10:20.")

        if n < 1:
            raise SystemExit("Row numbers are 1-based and must be >= 1.")

        return n, n

    parts = value.split(":")

    if len(parts) != 2:
        raise SystemExit("Use a row range like 10:20.")

    try:
        start = int(parts[0])
        end = int(parts[1])
    except ValueError:
        raise SystemExit("Row range must look like 10:20.")

    if start < 1 or end < 1:
        raise SystemExit("Row numbers are 1-based and must be >= 1.")

    if end < start:
        raise SystemExit("Range end must be >= range start.")

    return start, end


class Corpus:
    def __init__(self, children_path, parents_path, docs_path=None):
        ch = load_jsonl(children_path)

        self.cid = [c.get("child_id") or c.get("chunk_id") for c in ch]
        self.c_parent = [c["parent_id"] for c in ch]
        self.c_doc = [str(c["doc_id"]) for c in ch]
        self.c_text = [c["text"] for c in ch]

        self.C = np.array(
            [c["embedding"] for c in ch],
            dtype=np.float32,
        )

        self.parents = {
            p["parent_id"]: p
            for p in load_jsonl(parents_path)
        }

        self.docs = (
            {
                str(d["doc_id"]): d
                for d in load_jsonl(docs_path)
            }
            if docs_path and Path(docs_path).exists()
            else {}
        )

        self.parent_len = {
            pid: len(p.get("text", ""))
            for pid, p in self.parents.items()
        }

    def rank(self, qvec):
        q = np.asarray(qvec, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        return np.argsort(-(self.C @ q))

    def header(self, pid, doc_id):
        p = self.parents.get(pid, {})
        d = self.docs.get(str(doc_id), {})

        bc = p.get("breadcrumb") or d.get("title") or ""

        return (
            f"[{pid}] {bc} | "
            f"ძალაში შესვლა: {d.get('effective_from') or '-'} | "
            f"ძალადაკარგულია: {d.get('expires') or '-'}"
        )

    def assemble(
        self,
        ranked,
        budget_tokens,
        cap_chars,
        per_doc,
        cpt,
        depth=200,
    ):
        """Greedy: walk ranked children, add whole parents (or just matched
        children for oversized parents) until the token budget is used.
        At most `per_doc` parents per document.
        """

        budget_chars = budget_tokens * cpt

        inc, order, doc_n, used = {}, [], defaultdict(int), 0

        for ci in ranked[:depth]:
            pid, did = self.c_parent[ci], self.c_doc[ci]

            if pid in inc:
                p = inc[pid]

                if (
                    p["capped"]
                    and len(p["kids"]) < 2
                    and ci not in p["kids"]
                    and used + len(self.c_text[ci]) <= budget_chars
                ):
                    p["kids"].append(ci)
                    used += len(self.c_text[ci])

                continue

            if per_doc and doc_n[did] >= per_doc:
                continue

            full = self.parent_len.get(
                pid,
                len(self.c_text[ci]),
            )

            capped = bool(cap_chars) and full > cap_chars

            cost = len(self.c_text[ci]) if capped else full

            if used + cost > budget_chars:
                continue

            inc[pid] = {
                "capped": capped,
                "kids": [ci],
                "doc": did,
            }

            order.append(pid)
            doc_n[did] += 1
            used += cost

        passages = []

        for pid in order:
            p = inc[pid]

            if p["capped"]:
                kids = sorted(
                    p["kids"],
                    key=lambda i: self.cid[i],
                )

                text = "\n[…]\n".join(
                    self.c_text[i]
                    for i in kids
                )

            else:
                text = (
                    self.parents[pid]["text"]
                    if pid in self.parents
                    else self.c_text[p["kids"][0]]
                )

            passages.append(
                {
                    "parent_id": pid,
                    "doc_id": p["doc"],
                    "capped": p["capped"],
                    "header": self.header(pid, p["doc"]),
                    "text": text,
                }
            )

        return passages, used / cpt


def format_context(passages):
    return "\n\n".join(
        f"{p['header']}\n{p['text']}"
        for p in passages
    )


def build_messages(question, passages):
    user = (
        f"Passages:\n\n"
        f"{format_context(passages)}"
        f"\n\nQuestion: {question}"
    )

    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": user,
        },
    ]


# ============================================================
# LLM CALLS
# ============================================================

class Cache:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {}

        if self.path.exists():
            for l in self.path.open(encoding="utf-8"):
                try:
                    r = json.loads(l)
                    self.data[r["key"]] = r["out"]
                except Exception:
                    pass

    def put(self, key, out):
        self.data[key] = out

        with self.path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "key": key,
                        "out": out,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


_last_call = {}


def throttle(model, rpm):
    gap = 60.0 / max(rpm, 0.1)

    wait = (
        _last_call.get(model, 0)
        + gap
        - time.time()
    )

    if wait > 0:
        time.sleep(wait)

    _last_call[model] = time.time()


def mock_llm(model, messages):
    user = messages[-1]["content"]

    first = CITE_RE.search(user)

    if model == "mock/refuse":
        return json.dumps(
            {
                "answer": REFUSAL,
                "sources": [],
            },
            ensure_ascii=False,
        )

    if model == "mock/hallucinate":
        return json.dumps(
            {
                "answer": "პასუხი არის 42.",
                "sources": ["9999999:0000"],
            },
            ensure_ascii=False,
        )

    if model == "mock/judge":
        return '{"verdict": "correct", "reason": "mock"}'

    return json.dumps(
        {
            "answer": f"ტესტ-პასუხი.",
            "sources": [first.group(1)] if first else [],
        },
        ensure_ascii=False,
    )


def call_llm(
    model,
    messages,
    max_tokens,
    rpm,
    cache,
    retries=5,
):
    key = hashlib.sha1(
        json.dumps(
            {
                "m": model,
                "x": messages,
                "t": max_tokens,
            },
            ensure_ascii=False,
        ).encode()
    ).hexdigest()

    if key in cache.data:
        return dict(
            cache.data[key],
            cached=True,
        )

    if model.startswith("mock/"):
        out = {
            "text": mock_llm(model, messages),
            "latency": 0.0,
            "prompt_tokens": None,
            "completion_tokens": None,
        }

        cache.put(key, out)
        return out

    import litellm

    litellm.drop_params = True

    err = None

    for attempt in range(retries):
        throttle(model, rpm)

        t0 = time.time()

        try:
            r = litellm.completion(
                model=model,
                messages=messages,
                temperature=0,
                max_tokens=max_tokens,
            )

            text = (
                r.choices[0].message.content
                or ""
            )

            u = getattr(r, "usage", None)

            out = {
                "text": text.strip(),
                "latency": time.time() - t0,
                "prompt_tokens": getattr(
                    u,
                    "prompt_tokens",
                    None,
                ),
                "completion_tokens": getattr(
                    u,
                    "completion_tokens",
                    None,
                ),
            }

            cache.put(key, out)

            return out

        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:400]}"
            print(f"    attempt {attempt+1}/{retries} failed: {err}")
            if any(s in err for s in ("AuthenticationError", "BadRequestError", "NotFoundError",
                                      "413", "Request too large", "context_length")):
                break
            time.sleep(15 * (attempt + 1) if ("RateLimit" in err or "429" in err) else 5)


    return {"error": err}


# ============================================================
# LLM RESPONSE PARSING
# ============================================================

def parse_llm_response(text, valid_ids):
    """
    Extract the LLM's answer and the parent IDs it says it used.

    Expected response:

    {
        "answer": "...",
        "sources": ["1234567:0001", "1234567:0002"]
    }

    If JSON parsing fails, fall back to citation extraction so that
    an imperfect model response does not break the whole evaluation.
    """

    text = (text or "").strip()

    answer = text
    sources = []

    cleaned = text

    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

        cleaned = re.sub(
            r"\s*```$",
            "",
            cleaned,
        ).strip()

    try:
        obj = json.loads(cleaned)

        answer = str(
            obj.get("answer", "")
        ).strip()

        raw_sources = obj.get(
            "sources",
            [],
        )

        if isinstance(raw_sources, str):
            raw_sources = [raw_sources]

        sources = [
            str(x).strip()
            for x in raw_sources
            if str(x).strip()
        ]

    except Exception:
        answer = text
        sources = CITE_RE.findall(text)

        answer = CITE_RE.sub("", answer)

        answer = re.sub(
            r"\s+",
            " ",
            answer,
        ).strip()

    sources = list(
        dict.fromkeys(
            s for s in sources
            if s in valid_ids
        )
    )

    answer = CITE_RE.sub("", answer)

    answer = re.sub(
        r"\s+",
        " ",
        answer,
    ).strip()

    return answer, sources


# ============================================================
# SCORING
# ============================================================

def extract_facts(ref):
    ref = ref or ""

    nums = [
        re.sub(r"\s+", "", x)
        for x in re.findall(
            r"\d[\d\s]*\d",
            ref,
        )
    ]

    nums += re.findall(
        r"\d{2,}",
        ref,
    )

    nums = [
        n
        for n in dict.fromkeys(nums)
        if len(n) >= 2
    ]

    names = [
        re.sub(
            r"\s+",
            "",
            n.lower(),
        )
        for n in re.findall(
            r"„([^“]{3,60})“",
            ref,
        )
    ]

    return nums, names


def fact_recall(ref, answer):
    nums, names = extract_facts(ref)

    if not nums and not names:
        return None

    a = re.sub(
        r"\s+",
        "",
        (answer or "").lower(),
    )

    hits = (
        sum(n in a for n in nums)
        + sum(n in a for n in names)
    )

    return hits / (len(nums) + len(names))


def judge(
    model,
    question,
    ref,
    answer,
    cache,
    rpm,
    max_tokens=300,
):
    msgs = [
        {
            "role": "system",
            "content": JUDGE_PROMPT,
        },
        {
            "role": "user",
            "content": (
                f"QUESTION: {question}\n\n"
                f"REFERENCE ANSWER: {ref}\n\n"
                f"MODEL ANSWER: {answer}"
            ),
        },
    ]

    out = call_llm(
        model,
        msgs,
        max_tokens,
        rpm,
        cache,
    )

    if "error" in out:
        return None

    m = re.search(
        r'"verdict"\s*:\s*'
        r'"(correct|partial|incorrect)"',
        out["text"],
    )

    return (
        {
            "correct": 1.0,
            "partial": 0.5,
            "incorrect": 0.0,
        }.get(m.group(1))
        if m
        else None
    )


def retrieval_recall(row, passages):
    """
    Parent-level recall for this individual question.

    1.0 = all gold parents retrieved
    0.5 = half retrieved
    0.0 = none retrieved

    For one gold parent, this naturally becomes 0 or 1.
    """

    gold = set(
        ids(row.get("source_parent"))
    )

    if not gold:
        return None

    retrieved = {
        p["parent_id"]
        for p in passages
    }

    return len(gold & retrieved) / len(gold)


def score(row, passages, out):
    text = out.get(
        "text",
        "",
    )

    gold = ids(
        row.get("source_parent")
    )

    ctx_ids = {
        p["parent_id"]
        for p in passages
    }

    cited = list(
        dict.fromkeys(
            CITE_RE.findall(text)
        )
    )

    refused = (
        "ვერ მოიძებნა" in text
        and len(text) < 200
    )

    invalid = [
        c
        for c in cited
        if c not in ctx_ids
    ]

    return {
        "answerable": bool(gold),
        "refused": refused,
        "cited": cited,
        "n_cited": len(cited),
        "n_invalid_cites": len(invalid),
        "ctx_has_gold_all": (
            bool(gold)
            and all(
                g in ctx_ids
                for g in gold
            )
        ),
        "cites_gold_all": (
            bool(gold)
            and all(
                g in cited
                for g in gold
            )
        ),
        "cites_gold_any": (
            bool(gold)
            and any(
                g in cited
                for g in gold
            )
        ),
        "fact_recall": (
            None
            if refused
            else fact_recall(
                row.get("answer"),
                text,
            )
        ),
    }


def summarize(df):
    import pandas as pd

    rows = []

    for model, g in df.groupby("model"):
        g = g[g.error.isna()]

        ans = g[g.answerable]

        in_ctx = ans[
            ans.ctx_has_gold_all
        ]

        answered = g[
            ~g.refused
        ]

        unans = g[
            ~g.answerable
        ]

        rows.append(
            {
                "model": model,
                "n": len(g),
                "ctx_has_gold": (
                    ans.ctx_has_gold_all.mean()
                    if len(ans)
                    else None
                ),
                "over_refusal(gold in ctx)": (
                    in_ctx.refused.mean()
                    if len(in_ctx)
                    else None
                ),
                "answered_without_gold_ctx": (
                    (~ans[
                        ~ans.ctx_has_gold_all
                    ].refused).mean()
                    if (
                        ~ans.ctx_has_gold_all
                    ).any()
                    else None
                ),
                "refusal_ok(unanswerable)": (
                    unans.refused.mean()
                    if len(unans)
                    else None
                ),
                "no_citation": (
                    (answered.n_cited == 0).mean()
                    if len(answered)
                    else None
                ),
                "invalid_cite": (
                    (answered.n_invalid_cites > 0).mean()
                    if len(answered)
                    else None
                ),
                "cites_gold|ctx": (
                    in_ctx[
                        ~in_ctx.refused
                    ].cites_gold_all.mean()
                    if (
                        ~in_ctx.refused
                    ).any()
                    else None
                ),
                "fact_recall|ctx": (
                    in_ctx.fact_recall.dropna().mean()
                    if in_ctx.fact_recall.notna().any()
                    else None
                ),
                "judge|ctx": (
                    in_ctx.judge.dropna().mean()
                    if in_ctx.judge.notna().any()
                    else None
                ),
                "latency_s": g.latency.mean(),
                "prompt_tok": g.prompt_tokens.mean(),
            }
        )

    return pd.DataFrame(rows)


# ============================================================
# COMMAND ARGUMENTS
# ============================================================

def add_common(ap):
    ap.add_argument(
        "--children",
        default="matsne_data/embeddings/children_bge_m3.jsonl",
    )

    ap.add_argument(
        "--parents",
        default="matsne_data/chunks/parents.jsonl",
    )

    ap.add_argument(
        "--docs",
        default=None,
    )

    ap.add_argument(
        "--budget-tokens",
        type=int,
        default=12000,
    )

    ap.add_argument(
        "--cpt",
        type=float,
        default=1.8,
        help="chars per token (calibrate for your tokenizer)",
    )

    ap.add_argument(
        "--cap-chars",
        type=int,
        default=6000,
    )

    ap.add_argument(
        "--per-doc",
        type=int,
        default=10,
    )

    ap.add_argument(
        "--max-tokens",
        type=int,
        default=5000,
        help="maximum LLM output tokens",
    )

    ap.add_argument(
        "--rpm",
        type=float,
        default=8,
        help="max requests/min per model",
    )

    ap.add_argument(
        "--out",
        default="matsne_data/generation",
    )


# ============================================================
# BAKEOFF
# ============================================================

def cmd_bakeoff(a):
    import pandas as pd

    out_dir = Path(a.out)

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    corpus = Corpus(
        a.children,
        a.parents,
        a.docs,
    )

    qs = load_jsonl(
        a.questions
    )[: a.limit]

    cache = Cache(
        out_dir / "cache.jsonl"
    )

    models = [
        m.strip()
        for m in a.models.split(",")
        if m.strip()
    ]

    contexts = []

    for q in qs:
        passages, toks = corpus.assemble(
            corpus.rank(q["embedding"]),
            a.budget_tokens,
            a.cap_chars,
            a.per_doc,
            a.cpt,
        )

        contexts.append(
            (
                passages,
                toks,
            )
        )

    with (
        out_dir / "contexts.jsonl"
    ).open(
        "w",
        encoding="utf-8",
    ) as f:
        for q, (ps, t) in zip(
            qs,
            contexts,
        ):
            f.write(
                json.dumps(
                    {
                        "question": q["question"],
                        "est_tokens": t,
                        "parents": [
                            p["parent_id"]
                            for p in ps
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    print(
        f"questions={len(qs)} "
        f"models={models} "
        f"avg context ≈ "
        f"{np.mean([t for _, t in contexts]):,.0f} tokens"
    )

    results = []

    for m in models:
        for i, (
            q,
            (passages, _),
        ) in enumerate(
            zip(qs, contexts),
            1,
        ):
            out = call_llm(
                m,
                build_messages(
                    q["question"],
                    passages,
                ),
                a.max_tokens,
                a.rpm,
                cache,
            )

            rec = {
                "model": m,
                "qi": i,
                "question": q["question"],
                "type": q.get("type"),
                "answer_text": out.get("text"),
                "error": out.get("error"),
                "latency": out.get("latency"),
                "prompt_tokens": out.get("prompt_tokens"),
                "completion_tokens": out.get("completion_tokens"),
            }

            if not out.get("error"):
                rec.update(
                    score(
                        q,
                        passages,
                        out,
                    )
                )

                if (
                    a.judge
                    and rec["answerable"]
                    and not rec["refused"]
                    and q.get("answer")
                ):
                    rec["judge"] = judge(
                        a.judge,
                        q["question"],
                        q["answer"],
                        out["text"],
                        cache,
                        a.rpm,
                    )

            else:
                print(
                    f"  [{m}] q{i}: "
                    f"ERROR {out['error'][:120]}"
                )

            results.append(rec)

            if i % 10 == 0:
                print(
                    f"  [{m}] "
                    f"{i}/{len(qs)}"
                )

    df = pd.DataFrame(results)

    for col in (
        "judge",
        "answerable",
        "refused",
        "ctx_has_gold_all",
        "n_cited",
        "n_invalid_cites",
        "fact_recall",
        "cites_gold_all",
        "latency",
        "prompt_tokens",
    ):
        if col not in df:
            df[col] = np.nan

    df["answerable"] = (
        df["answerable"]
        .astype("boolean")
        .fillna(False)
        .astype(bool)
    )

    for c in (
        "refused",
        "ctx_has_gold_all",
    ):
        df[c] = (
            df[c]
            .astype("boolean")
            .fillna(False)
            .astype(bool)
        )

    df.to_json(
        out_dir / "results.jsonl",
        orient="records",
        lines=True,
        force_ascii=False,
    )

    summ = summarize(df)

    summ.to_csv(
        out_dir / "summary.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.set_option(
        "display.width",
        220,
    )

    pd.set_option(
        "display.max_columns",
        30,
    )

    print(
        "\n"
        + summ.to_string(
            index=False,
            float_format=lambda x: f"{x:.2f}",
        )
    )

    print(
        f"\nWrote results.jsonl, "
        f"summary.csv, contexts.jsonl, "
        f"cache.jsonl in {out_dir}"
    )


# ============================================================
# ASK / EVALUATION
# ============================================================

def cmd_ask(a):
    import pandas as pd

    out_dir = Path(a.out)

    out_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    corpus = Corpus(
        a.children,
        a.parents,
        a.docs,
    )

    questions = load_jsonl(
        a.questions
    )

    start, end = parse_row_range(
        a.row_range
    )

    if start > len(questions):
        raise SystemExit(
            f"Start row {start} is outside the "
            f"question file ({len(questions)} rows)."
        )

    end = min(
        end,
        len(questions),
    )

    selected = questions[
        start - 1 : end
    ]

    print(
        f"Processing rows {start}:{end} "
        f"({len(selected)} questions)"
    )

    cache = Cache(
        out_dir / "cache.jsonl"
    )

    # --------------------------------------------------------
    # Incremental CSV loading
    # --------------------------------------------------------

    output_path = (
        out_dir / "evaluation_results.csv"
    )

    if output_path.exists():
        existing_df = pd.read_csv(
            output_path
        )

        results = existing_df.to_dict(
            "records"
        )

        if "error" in existing_df.columns:
            completed_rows = set(
                existing_df.loc[
                    existing_df["error"].isna(),
                    "row_number",
                ].dropna().astype(int)
            )
        else:
            completed_rows = set(
                existing_df["row_number"]
                .dropna()
                .astype(int)
            )

        print(
            f"Found existing results: "
            f"{len(completed_rows)} completed rows"
        )

    else:
        results = []
        completed_rows = set()

    # --------------------------------------------------------
    # Process questions
    # --------------------------------------------------------

    for row_number, q in enumerate(
        selected,
        start,
    ):

        # Skip successfully completed rows.
        if row_number in completed_rows:
            print(
                f"[{row_number}/{end}] "
                f"already completed — skipping"
            )
            continue

        question = q.get(
            "question",
            "",
        )

        print(
            f"[{row_number}/{end}] "
            f"{question[:100]}"
        )

        # ----------------------------------------------------
        # 1. Use the EXISTING BGE-M3 question embedding.
        # ----------------------------------------------------

        qvec = q.get("embedding")

        if qvec is None:
            print(
                f"  ERROR: no embedding for row {row_number}"
            )

            rec = dict(q)

            rec.pop(
                "embedding",
                None,
            )

            rec["row_number"] = row_number
            rec["retrieval_recall"] = None
            rec["llm_answer"] = None
            rec["llm_sources"] = None
            rec["latency"] = None
            rec["prompt_tokens"] = None
            rec["completion_tokens"] = None
            rec["error"] = "missing question embedding"

            results.append(rec)

            # Save immediately.
            pd.DataFrame(
                results
            ).to_csv(
                output_path,
                index=False,
                encoding="utf-8-sig",
            )

            continue

        # ----------------------------------------------------
        # 2. Retrieval / parent assembly.
        # ----------------------------------------------------

        passages, toks = corpus.assemble(
            corpus.rank(qvec),
            a.budget_tokens,
            a.cap_chars,
            a.per_doc,
            a.cpt,
        )

        retrieved_parent_ids = [
            p["parent_id"]
            for p in passages
        ]

        # ----------------------------------------------------
        # 3. Parent retrieval recall.
        # ----------------------------------------------------

        gold_parents = set(
            ids(
                q.get("source_parent")
            )
        )

        retrieved_parents = set(
            retrieved_parent_ids
        )

        if gold_parents:
            recall = (
                len(
                    gold_parents
                    & retrieved_parents
                )
                / len(gold_parents)
            )
        else:
            recall = None

        # ----------------------------------------------------
        # 4. Ask LLM.
        # ----------------------------------------------------

        out = call_llm(
            a.model,
            build_messages(
                question,
                passages,
            ),
            a.max_tokens,
            a.rpm,
            cache,
        )

        rec = dict(q)

        # Do not carry the huge embedding into the CSV.
        rec.pop(
            "embedding",
            None,
        )

        rec["row_number"] = row_number

        rec["retrieved_parents"] = ";".join(
            retrieved_parent_ids
        )

        rec["retrieval_recall"] = recall

        rec["context_tokens_est"] = toks

        if "error" in out:
            rec["llm_answer"] = None
            rec["llm_sources"] = None
            rec["latency"] = None
            rec["prompt_tokens"] = None
            rec["completion_tokens"] = None
            rec["error"] = out["error"]

            print(
                f"  ERROR: {out['error'][:120]}"
            )

        else:
            valid_ids = set(
                retrieved_parent_ids
            )

            answer, llm_sources = parse_llm_response(
                out.get("text", ""),
                valid_ids,
            )

            rec["llm_answer"] = answer

            rec["llm_sources"] = ";".join(
                llm_sources
            )

            rec["latency"] = out.get(
                "latency"
            )

            rec["prompt_tokens"] = out.get(
                "prompt_tokens"
            )

            rec["completion_tokens"] = out.get(
                "completion_tokens"
            )

            rec["error"] = None

            print(
                f"  retrieval recall={recall} "
                f"| LLM sources={len(llm_sources)} "
                f"| context≈{toks:,.0f} tokens"
            )

        # ----------------------------------------------------
        # Save result immediately after this question.
        # ----------------------------------------------------

        results.append(rec)

        pd.DataFrame(
            results
        ).to_csv(
            output_path,
            index=False,
            encoding="utf-8-sig",
        )

        # Only successful rows are skipped on future runs.
        if rec["error"] is None:
            completed_rows.add(row_number)

    # --------------------------------------------------------
    # Final save
    # --------------------------------------------------------

    df = pd.DataFrame(results)

    df.to_csv(
        output_path,
        index=False,
        encoding="utf-8-sig",
    )

    print(
        f"\nWrote {output_path}"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    ap = argparse.ArgumentParser()

    sub = ap.add_subparsers(
        dest="cmd",
        required=True,
    )

    # --------------------------------------------------------
    # BAKEOFF
    # --------------------------------------------------------

    b = sub.add_parser(
        "bakeoff"
    )

    add_common(b)

    b.add_argument(
        "--questions",
        required=True,
    )

    b.add_argument(
        "--models",
        required=True,
        help="comma-separated LiteLLM model strings",
    )

    b.add_argument(
        "--judge",
        default=None,
        help="LiteLLM model for answer grading",
    )

    b.add_argument(
        "--limit",
        type=int,
        default=None,
    )

    # --------------------------------------------------------
    # ASK / EVALUATION
    # --------------------------------------------------------

    k = sub.add_parser(
        "ask"
    )

    add_common(k)

    k.add_argument(
        "row_range",
        help="1-based row number or inclusive range, e.g. 10 or 10:20",
    )

    k.add_argument(
        "--model",
        default="gemini/gemini-3.1-pro",
    )

    k.add_argument(
        "--questions",
        default="matsne_data/embeddings/eval_questions_bge_m3.jsonl",
    )

    a = ap.parse_args()

    if a.cmd == "bakeoff":
        cmd_bakeoff(a)
    else:
        cmd_ask(a)


if __name__ == "__main__":
    main()