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
