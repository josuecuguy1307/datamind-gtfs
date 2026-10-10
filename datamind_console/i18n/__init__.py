"""Bilingual (EN/ES) UI strings for ML DATAMIND GTFS.

Explicit API, no monkey-patching of Streamlit:

    from datamind_console.i18n import t, status_label
    st.subheader(t("page.overview.title"))
    st.badge(status_label("cleanliness", row["cleanliness_status"]))

Catalogs live in ``en.py`` and ``es.py`` with identical keys
(``datamind_console/tests/test_i18n_catalog.py`` enforces it, plus that every
``t("literal.key")`` used in code exists). Product data (stop and route
names, operator names) is never translated; only interface text is.
"""
from datamind_console.i18n.core import (
    DEFAULT_LANG,
    LANGUAGES,
    get_lang,
    has_key,
    humanize,
    init_language,
    language_label,
    option_label,
    set_lang,
    status_label,
    t,
)

__all__ = [
    "DEFAULT_LANG",
    "LANGUAGES",
    "get_lang",
    "has_key",
    "humanize",
    "init_language",
    "language_label",
    "option_label",
    "set_lang",
    "status_label",
    "t",
]
