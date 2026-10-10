# views/phases/phase2_view.py
"""Phase 2 — Semantic Geocoder (Monitoring)"""
from __future__ import annotations

from typing import Any

import streamlit as st

from datamind_console.phases.phase2_semantics.client import Phase2Client
from datamind_console.i18n import t
from datamind_console.views.phases.panels import call_panel as _call, load_panel as _load


def render_phase2_view(*, ctx: Any, analytics: Any = None, audit: Any = None, **_) -> None:
    if not hasattr(ctx, "phase2") or getattr(ctx, "phase2") is None:
        ctx.phase2 = Phase2Client()

    phase2 = ctx.phase2
    st.subheader(t("phase2.places.title"), anchor=False)

    # Place points map
    w_map = _load("views.phases.phase2.widgets.place_points_map", "render_place_points_map")
    _call(w_map, ctx=ctx, client=phase2, phase2=phase2, mode="prod")

