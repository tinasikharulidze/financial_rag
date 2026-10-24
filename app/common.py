"""Shared helpers of the Streamlit app: context, current user, LLM wrapper, small HTML pieces."""
from __future__ import annotations

import html
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / "app"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from i18n_ka import PRIORITY, PRIORITY_COLOR, T, VERDICT, VERDICT_COLOR, tr  # noqa: E402
from kg.config import load_config  # noqa: E402
from kg.llm import LLM, BudgetExhausted, LLMRequestRejected, LLMUnavailable  # noqa: E402
from kg.runlog import Run  # noqa: E402
from kg.store.sqlite_nx import SQLiteStore  # noqa: E402


@st.cache_resource
def _default_ctx():
    cfg = load_config()
    db = cfg.path("db")
    return cfg, (SQLiteStore(db) if db.exists() else None)


def get_ctx():
    """(config, store). Tests put their own pair in ``st.session_state['_ctx']``."""
    ctx = st.session_state.get("_ctx") or _default_ctx()
    import i18n_en
    from i18n_lang import register_acts
    register_acts(ctx[0].get("acts") if ctx[0] else None, i18n_en.DOMAIN)     # English names of the acts
    return ctx


def current_user(store) -> dict | None:
    from kg.services.accounts import get_user
    uid = st.session_state.get("user_id")
    return get_user(store, uid) if uid else None


def login_as(user: dict):
    st.session_state["user_id"] = user["id"]


def logout():
    for k in list(st.session_state.keys()):
        if not k.startswith("_"):
            del st.session_state[k]


def make_llm(cfg, store, run_id=None):
    """Overridable in tests: ``st.session_state['_llm_factory']`` returns an LLM with a fake backend."""
    factory = st.session_state.get("_llm_factory")
    return factory(cfg, store, run_id) if factory else LLM(cfg, store, run_id)


def with_llm(cfg, store, kind: str, fn, params=None):
    """Run ``fn(run, llm)`` inside a logged Run; shows a Georgian message when the quota is spent."""
    try:
        with Run(store, cfg.path("runs"), kind, params or {}) as run:
            return fn(run, make_llm(cfg, store, run.id))
    except BudgetExhausted:
        st.error(T["budget"])
        return None
    except LLMUnavailable:
        st.error(T["llm_unavailable"])
        return None
    except LLMRequestRejected as exc:
        st.error(f"{T['llm_rejected']} {exc}")
        return None


def badge(text: str, color: str) -> str:
    return (f"<span style='background:{color};color:#fff;padding:2px 8px;border-radius:10px;font-size:0.8em'>"
            f"{html.escape(str(text))}</span>")


def prio_badge(p: str) -> str:
    return badge(tr(PRIORITY, p), PRIORITY_COLOR.get(p, "#555"))


def verdict_badge(v: str) -> str:
    return badge(tr(VERDICT, v), VERDICT_COLOR.get(v, "#555"))


def diff_html(diff: list[dict]) -> str:
    out = []
    for d in diff:
        a, b = html.escape(d["a"]), html.escape(d["b"])
        if d["op"] == "equal":
            out.append(a)
        else:
            if a:
                out.append(f"<del style='background:#f8d7da'>{a}</del>")
            if b:
                out.append(f"<ins style='background:#d4edda;text-decoration:none'>{b}</ins>")
    return "".join(out)


def act_choices(cfg) -> dict[str, str]:
    return {f"{a} – {c.get('short', '')}": a for a, c in cfg["acts"].items()}


def company_label(store, company_id: str) -> str:
    c = store.q1("SELECT name FROM companies WHERE id=?", (company_id,))
    return c["name"] if c else company_id


def version_label(store, version_id: str) -> str:
    v = store.q1("SELECT dv.version, dv.status, d.code, d.title FROM document_versions dv "
                 "JOIN documents d ON d.id=dv.family_id WHERE dv.id=?", (version_id,))
    if not v:
        return version_id
    return f"{v['code']} · v{v['version']} · {tr({'draft': T['status_draft'], 'published': T['status_published'], 'replaced': T['status_replaced'], 'archived': T['status_archived']}, v['status'])}"


def word_diff(a: str, b: str) -> list[dict]:
    """Word-level diff in the shape ``diff_html`` expects: [{op: equal|replace|insert|delete, a, b}]."""
    import difflib
    x, y = (a or "").split(), (b or "").split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
        out.append({"op": op, "a": " ".join(x[i1:i2]) + " ", "b": " ".join(y[j1:j2]) + " "})
    return out


def html_frame(html_str: str, height: int = 720):
    """Show an HTML document in an iframe (``st.iframe`` in new Streamlit, components.html in older ones)."""
    if hasattr(st, "iframe"):
        st.iframe(html_str, height=height)
    else:
        import streamlit.components.v1 as components
        components.html(html_str, height=height, scrolling=True)


# directions of work = the regulation acts in configs/kg.yaml; an act with ``domains`` uses its first domain, the
# others get a fixed key here (display only: nothing in the pipeline reads this)
ACT_DIRECTION = {"2060636": "capital_adequacy", "5679194": "expected_credit_loss", "4822603": "retail_lending",
                 "4964423": "model_risk"}


def directions(cfg) -> list[str]:
    """Direction keys in display order: model risk first, then the other acts (even without documents yet)."""
    out = []
    for act_id, a in (cfg.get("acts") or {}).items():
        d = ((a or {}).get("domains") or [None])[0] or ACT_DIRECTION.get(str(act_id)) or f"act_{act_id}"
        if d not in out:
            out.append(d)
    return sorted(out, key=lambda d: (d != "model_risk", out.index(d)))


def direction_label(cfg, key: str) -> str:
    from i18n_ka import DOMAIN
    if key in DOMAIN:
        return DOMAIN[key]
    for act_id, a in (cfg.get("acts") or {}).items():
        if ACT_DIRECTION.get(str(act_id)) == key or f"act_{act_id}" == key:
            return (a or {}).get("short") or key
    return key
