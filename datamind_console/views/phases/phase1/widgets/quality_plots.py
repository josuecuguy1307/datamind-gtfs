# views/phases/phase1/widgets/quality_plots.py
from __future__ import annotations

import streamlit as st
from typing import Any, Dict

from phases.phase1_nodes.client import _get_phase1_client


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
        st.info("Select a node set to see quality metrics.")
        return

    phase1 = _get_phase1_client()

    try:
         data: Dict[str, Any] = phase1.get_node_confidence_distribution(node_set_id, source="work")
    except Exception as e:
        st.error(f"Failed to load confidence data: {e}")
        return

    if not data or not data.get("count"):
        st.info("No confidence data available for this node set.")
        return

    st.subheader("Node confidence quality")

    # -----------------------
    # Summary metrics
    # -----------------------
    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Nodes", data["count"])
    c2.metric("Avg confidence", f"{data['avg']:.2f}" if data["avg"] is not None else "—")
    c3.metric("Min", f"{data['min']:.2f}" if data["min"] is not None else "—")
    c4.metric("Max", f"{data['max']:.2f}" if data["max"] is not None else "—")

    # -----------------------
    # Histogram
    # -----------------------
    st.markdown("### Confidence distribution")

    hist = data.get("histogram", {})

    if not hist:
        st.info("Histogram empty.")
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
        f"⚠️ Low confidence (<0.5): {low} nodes · "
        f"✅ High confidence (≥0.7): {high} nodes"
    )
