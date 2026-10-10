"""Page registry for ``st.navigation``: sections, deep links, programmatic jumps.

Every console page has a stable id and URL path (``/review``, ``/phases``…),
so screens can be linked and reloaded. Code navigates with
``navigation.go("phases", phase=3)`` or renders ``navigation.link(...)``
instead of writing ``st.session_state["ui.page"]`` and calling ``st.rerun()``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

import streamlit as st

from datamind_console.i18n import t


@dataclass(frozen=True)
class PageSpec:
    id: str
    url_path: str
    title_key: str
    icon: str
    section_key: str
    render: Callable[[], None]
    default: bool = False


SECTION_ORDER = ("nav.section.operate", "nav.section.review", "nav.section.audit", "nav.section.integrate", "nav.section.system")

_PAGES: dict[str, Any] = {}


def build(specs: list[PageSpec]) -> Any:
    """Create ``st.Page`` objects for this run and return the selected page."""
    _PAGES.clear()
    grouped: dict[str, list[Any]] = {}
    for spec in specs:
        page = st.Page(
            spec.render,
            title=t(spec.title_key),
            icon=spec.icon,
            url_path=spec.url_path,
            default=spec.default,
        )
        _PAGES[spec.id] = page
        grouped.setdefault(spec.section_key, []).append(page)
    ordered = {t(key): grouped[key] for key in SECTION_ORDER if key in grouped}
    return st.navigation(ordered, position="sidebar", expanded=True)


def page(page_id: str) -> Any:
    return _PAGES[page_id]


def go(page_id: str, **query: Any) -> None:
    """Switch to a page, optionally with query parameters (e.g. ``phase=3``)."""
    params = {k: str(v) for k, v in query.items() if v is not None}
    st.switch_page(_PAGES[page_id], query_params=params or None)


def link(page_id: str, label: str, *, icon: Optional[str] = None, help: Optional[str] = None, **query: Any) -> None:
    params = {k: str(v) for k, v in query.items() if v is not None}
    if page_id not in _PAGES:
        st.caption(label)
        return
    st.page_link(_PAGES[page_id], label=label, icon=icon, help=help, query_params=params or None)
