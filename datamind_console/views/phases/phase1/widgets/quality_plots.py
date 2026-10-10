# views/phases/phase1/widgets/quality_plots.py
from __future__ import annotations

import streamlit as st
from typing import Any, Dict

from phases.phase1_nodes.client import _get_phase1_client
from datamind_console.i18n import t


def render_quality_plots(*, node_set_id: str) -> None:
    """
    Phase 1 — Quality plots

    Shows:
      - Confidence histogram
      - Summary stats

    Wired directly to:
      Phase1Client.get_node_confidence_distribution(node_set_id)
    """

    if not node_set_id:
        st.info(t("ui.p124.quality_plots.select_node_set_see_quality_metrics"))
        return

    phase1 = _get_phase1_client()

    try:
         data: Dict[str, Any] = phase1.get_node_confidence_distribution(node_set_id, source="work")
    except Exception as e:
        st.error(t("ui.p124.quality_plots.failed_load_confidence_data_e", e=(e)))
        return

    if not data or not data.get("count"):
        st.info(t("ui.p124.quality_plots.no_confidence_data_available_this_node"))
        return

    st.subheader(t("ui.p124.quality_plots.node_confidence_quality"))

    # -----------------------
    # Summary metrics
    # -----------------------
    c1, c2, c3, c4 = st.columns(4)

    c1.metric(t("ui.p124.quality_plots.nodes"), data["count"])
    c2.metric(t("ui.p124.quality_plots.avg_confidence"), f"{data['avg']:.2f}" if data["avg"] is not None else "—")
    c3.metric(t("ui.p124.quality_plots.min"), f"{data['min']:.2f}" if data["min"] is not None else "—")
    c4.metric(t("ui.p124.quality_plots.max"), f"{data['max']:.2f}" if data["max"] is not None else "—")

    # -----------------------
    # Histogram
    # -----------------------
    st.markdown(t("ui.p124.quality_plots.confidence_distribution"))

    hist = data.get("histogram", {})

    if not hist:
        st.info(t("ui.p124.quality_plots.histogram_empty"))
        return

    st.bar_chart(
        {
            "confidence": list(hist.keys()),
            "count": list(hist.values()),
        },
        x="confidence",
        y="count",
    )

    # -----------------------
    # Quality hints (UX)
    # -----------------------
    low = sum(
        v for k, v in hist.items()
        if k.startswith("0.0") or k.startswith("0.3")
    )
    high = sum(
        v for k, v in hist.items()
        if k.startswith("0.7") or k.startswith("0.9")
    )

    st.caption(
        t("ui.p124.quality_plots.low_confidence_0_5_low_nodes", low=(low), high=(high))
    )
