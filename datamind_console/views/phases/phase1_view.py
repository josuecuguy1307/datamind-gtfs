from __future__ import annotations

from typing import Any
import streamlit as st

from phases.phase1_nodes.client import _get_phase1_client
from datamind_console.i18n import t
from datamind_console.ui.components.states import empty_state
from datamind_console.views.phases.panels import call_panel as _call, load_panel as _load


def _init_phase1_state() -> None:
    ss = st.session_state
    ss.setdefault("p1.node_set_id", None)


def render_phase1_view(*, ctx: Any, **_) -> None:
    _init_phase1_state()
    ss = st.session_state

    client = _get_phase1_client("v3.newnodes.fix_json_approve2")
    deps = {"client": client}

    st.subheader(t("phase1.node_sets.title"), anchor=False)

    # Node set picker
    w_node_sets = _load("views.phases.phase1.widgets.node_sets_table", "render_node_sets_table")
    picked = _call(w_node_sets, ctx=ctx, **deps)

    if isinstance(picked, str) and picked.strip():
        ss["p1.node_set_id"] = picked.strip()

    node_set_id = ss.get("p1.node_set_id")
    if not node_set_id:
        empty_state("phase1.select_node_set")
        return

    st.divider()

    # Summary + Quality
    w_summary = _load("views.phases.phase1.widgets.node_set_summary_card", "render_node_set_summary_card")
    w_quality = _load("views.phases.phase1.widgets.quality_plots", "render_quality_plots")
    w_kpis = _load("views.phases.phase1.widgets.resolved_points_kpis", "render_resolved_points_kpis")

    col1, col2 = st.columns([1.3, 1.0])
    with col1:
        _call(w_summary, ctx=ctx, node_set_id=node_set_id, **deps)
    with col2:
        _call(w_quality, ctx=ctx, node_set_id=node_set_id, **deps)

    st.divider()
    _call(w_kpis, ctx=ctx, node_set_id=node_set_id, **deps)
