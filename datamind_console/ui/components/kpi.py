"""KPI rows built on bordered ``st.metric`` with localized number formatting."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

import streamlit as st

from datamind_console.ui.formatting import format_number


@dataclass(frozen=True)
class Kpi:
    label: str
    value: Any
    help: Optional[str] = None
    delta: Optional[str] = None
    delta_color: str = "normal"


def kpi_row(items: Sequence[Kpi], *, columns: Optional[int] = None) -> None:
    if not items:
        return
    cols = st.columns(columns or len(items))
    for i, item in enumerate(items):
        with cols[i % len(cols)]:
            st.metric(
                item.label,
                format_number(item.value),
                delta=item.delta,
                delta_color=item.delta_color,
                help=item.help,
                border=True,
            )
