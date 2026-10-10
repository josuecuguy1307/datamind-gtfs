"""Translation lookup, language state and status vocabularies."""
from __future__ import annotations

import logging
from typing import Any, Callable

from datamind_console.i18n import en, es

LOGGER = logging.getLogger(__name__)

LANGUAGES: tuple[str, ...] = ("en", "es")
DEFAULT_LANG = "en"
SESSION_KEY = "ui.lang"
QUERY_PARAM = "lang"

_CATALOGS: dict[str, dict[str, str]] = {"en": en.STRINGS, "es": es.STRINGS}
_NATIVE_NAMES = {"en": "English", "es": "Español"}
_MISSING_LOGGED: set[tuple[str, str]] = set()


def _session() -> Any:
    try:
        import streamlit as st

        return st.session_state
    except Exception:  # pragma: no cover - outside Streamlit
        return {}


def _normalize(lang: Any) -> str:
    code = str(lang or "").strip().lower()[:2]
    return code if code in LANGUAGES else DEFAULT_LANG


def get_lang() -> str:
    """Current UI language for this browser session ('en' or 'es')."""
    try:
        return _normalize(_session().get(SESSION_KEY))
    except Exception:
        return DEFAULT_LANG


def set_lang(lang: Any) -> str:
    """Set the session language and mirror it to ``?lang=`` so reloads keep it."""
    code = _normalize(lang)
    try:
        _session()[SESSION_KEY] = code
    except Exception:
        pass
    try:
        import streamlit as st

        if st.query_params.get(QUERY_PARAM) != code:
            st.query_params[QUERY_PARAM] = code
    except Exception:
        pass
    return code


def init_language() -> str:
    """Pick the language once per session: ``?lang=`` wins, then session, then default."""
    try:
        import streamlit as st

        requested = st.query_params.get(QUERY_PARAM)
    except Exception:
        requested = None
    ss = _session()
    if requested:
        return set_lang(requested)
    if not ss.get(SESSION_KEY):
        return set_lang(DEFAULT_LANG)
    return get_lang()


def has_key(key: str) -> bool:
    return key in _CATALOGS[DEFAULT_LANG]


def t(key: str, /, lang: str | None = None, **params: Any) -> str:
    """Translate *key*; falls back to English, then to the key itself (logged once)."""
    code = _normalize(lang) if lang else get_lang()
    text = _CATALOGS[code].get(key)
    if text is None:
        text = _CATALOGS[DEFAULT_LANG].get(key)
        if (code, key) not in _MISSING_LOGGED:
            _MISSING_LOGGED.add((code, key))
            LOGGER.warning("i18n.missing_key lang=%s key=%s", code, key)
        if text is None:
            return key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            LOGGER.warning("i18n.format_error key=%s params=%s", key, sorted(params))
            return text
    return text


def language_label(code: str) -> str:
    """Language names are shown in their own language ("English", "Español")."""
    return _NATIVE_NAMES.get(_normalize(code), code)


def humanize(value: Any) -> str:
    """Fallback label for an unknown code: ``real_canon_gap`` → "Real canon gap"."""
    text = str(value if value is not None else "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else "—"


_ALL_SENTINELS = frozenset({"all", "(all)", "All", "__all__"})
_MISSING_SENTINELS = frozenset({"(missing)", "(none)"})


def option_label(domain: str | None = None) -> Callable[[Any], str]:
    """``format_func`` for option widgets whose options mix sentinels and codes.

    "all"/"(all)" render as ``common.all`` and "(missing)"/"(none)" as
    ``common.missing``; the option values themselves stay unchanged, so the
    code that compares against them keeps working. Other values go through
    ``status_label(domain, value)`` when a domain is given, else are shown as
    they are (product data such as province or route ids).
    """

    def _format(value: Any) -> str:
        if isinstance(value, str) and value in _ALL_SENTINELS:
            return t("common.all")
        if isinstance(value, str) and value in _MISSING_SENTINELS:
            return t("common.missing")
        return status_label(domain, value) if domain else str(value)

    return _format


def status_label(domain: str, value: Any, *, lang: str | None = None) -> str:
    """Translated label for a status code in a known vocabulary.

    ``None``/empty values use the domain's ``__none__`` entry. Codes without
    a catalog entry are humanised rather than hidden, so new pipeline states
    stay visible.
    """
    raw = "__none__" if value in (None, "") else str(value)
    key = f"status.{domain}.{raw}"
    if has_key(key):
        return t(key, lang=lang)
    return humanize(value) if raw != "__none__" else t("status.generic.__none__", lang=lang)
