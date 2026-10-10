"""Page header and section primitives shared by every console page."""
from __future__ import annotations

import html
from typing import Optional, Sequence

import streamlit as st

from datamind_console.ui.components.status import Chip, chips_row


def page_header(
    title: str,
    subtitle: Optional[str] = None,
    *,
    eyebrow: Optional[str] = None,
    chips: Sequence[Chip] = (),
) -> None:
    """Eyebrow label, title, amber rule, optional subtitle and status chips."""
    if eyebrow:
        st.html(f'<div class="dm-eyebrow">{html.escape(eyebrow)}</div>')
    st.title(title, anchor=False)
    if subtitle:
        st.html(f'<div class="dm-subtitle">{html.escape(subtitle)}</div>')
    st.html('<div class="dm-rule"></div>')
    if chips:
        chips_row(chips)


def section(title: str, caption: Optional[str] = None, *, divider: bool = False) -> None:
    if divider:
        st.divider()
    st.subheader(title, anchor=False)
    if caption:
        st.caption(caption)
