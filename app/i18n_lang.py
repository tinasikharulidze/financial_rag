"""Georgian / English switch. The tables of ``i18n_ka.py`` stay plain dicts that people edit; at the end of that file
they are wrapped in ``Lang``: reading a key gives the English text (``i18n_en.py``) when the current session chose
English (``st.session_state["lang"] == "en"``), otherwise the Georgian one. Missing English keys fall back to Georgian.
Per session, so two users can work in different languages at the same time."""
from __future__ import annotations

from collections.abc import Mapping

LANGS = {"ka": "ქართული", "en": "English"}


def current() -> str:
    try:
        import streamlit as st
        return st.session_state.get("lang", "ka")
    except Exception:  # noqa: BLE001 - outside a Streamlit run (tests, scripts): Georgian
        return "ka"


class Lang(Mapping):
    def __init__(self, ka: dict, name: str):
        self.ka = ka
        self.name = name

    def _en(self) -> dict:
        import i18n_en
        return getattr(i18n_en, self.name, {}) or {}

    def __getitem__(self, key):
        if current() == "en":
            en = self._en()
            if key in en:
                return en[key]
        return self.ka[key]

    def __iter__(self):
        return iter(self.ka)

    def __len__(self):
        return len(self.ka)

    def __contains__(self, key):
        return key in self.ka


# ------------------------------------------------------------------------------------------- labels built from data
# Some labels are put together by the pipeline in Georgian (a law reference "მუხლი 5, პუნქტი 10", a task title
# "შესწორება: ...", the short name of an act). In English the interface words in them are translated; the CONTENT
# (the text of the law, an obligation, a clause) stays as it is. Only apply this to labels, never to content.
_PREFIX_EN = (("შესწორება: ", "Amend clause: "), ("ახალი პუნქტი: ", "New clause: "),
              ("ახალი დოკუმენტი: ", "New document: "))
_WORDS_EN = (("ბრძანების მუხლი", "Order, article"), ("ქვეპუნქტი", "subparagraph"), ("პუნქტი", "paragraph"),
             ("მუხლი", "Article"), ("დანართი", "Annex"))
ACT_NAMES_EN: dict[str, str] = {}          # Georgian short name of an act -> English (filled from the config)


def register_acts(acts: dict | None, domain_en: dict | None):
    """The English name of an act = the English name of its first direction (configs/kg.yaml ``acts.*.domains``)."""
    for a in (acts or {}).values():
        a = a or {}
        doms = a.get("domains") or []
        if a.get("short") and (a.get("short_en") or (doms and (domain_en or {}).get(doms[0]))):
            ACT_NAMES_EN[a["short"]] = a.get("short_en") or domain_en[doms[0]]


def localize(label):
    """A label made from data, in the language of the interface (see above). Georgian: unchanged."""
    if not label or not isinstance(label, str) or current() != "en":
        return label
    for ka, en in _PREFIX_EN:
        if label.startswith(ka):
            label = en + label[len(ka):]
            break
    for ka, en in sorted(ACT_NAMES_EN.items(), key=lambda kv: -len(kv[0])):
        label = label.replace(ka, en)
    for ka, en in _WORDS_EN:
        label = label.replace(ka, en)
    return label
