# views/phases/phase2/widgets/node_mapping_confidence_table.py
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
import streamlit as st


def render_node_mapping_confidence_table(
    client: Any,
    *,
    place_set_id: str,
    limit: int = 5000,
    key_prefix: str = "p2_node_map_conf",
) -> None:
    """
    Phase 2 — Node mapping confidence table

    STRICTLY aligned with your current Phase2Client:
      - Uses: client.get_node_mapping_confidence(place_set_id)
      - Columns you can show from returned rows:
          node_id, place_candidate_id, confidence
    """
    st.subheader("Node → Place mapping confidence")

    if not place_set_id:
        st.info("Select a place_set_id first.")
        return

    try:
        rows: List[Dict[str, Any]] = client.get_node_mapping_confidence(place_set_id)
    except Exception as e:
        st.error(f"Failed to load node mapping confidence: {e}")
        return

    if not rows:
        st.warning("No node mappings found for this place_set_id.")
        return

    df = pd.DataFrame(rows)

    # Ensure expected cols exist (don’t invent new data)
    expected = ["node_id", "place_candidate_id", "confidence"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        st.error(f"Client returned rows missing columns: {missing}")
        st.dataframe(df.head(50), use_container_width=True)
        return

    # Normalize types
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")

    # Controls
    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 1.4])
    with c1:
        min_conf = st.number_input(
            "Min confidence",
            value=0.0,
            step=0.05,
            key=f"{key_prefix}:min_conf",
        )
    with c2:
        sort_by = st.selectbox(
            "Sort by",
            options=["confidence", "node_id", "place_candidate_id"],
            index=0,
            key=f"{key_prefix}:sort_by",
        )
    with c3:
        sort_desc = st.checkbox("Desc", value=True, key=f"{key_prefix}:desc")
    with c4:
        max_rows = st.number_input(
            "Max rows",
            min_value=50,
            max_value=20000,
            value=int(limit),
            step=250,
            key=f"{key_prefix}:max_rows",
        )

    # Filter
    if min_conf is not None:
        df = df[df["confidence"].fillna(-1.0) >= float(min_conf)]

    # Sort
    df = df.sort_values(by=sort_by, ascending=not bool(sort_desc), kind="mergesort")

    # Clip to max rows
    df = df.head(int(max_rows))

    # Simple summary
    if len(df) > 0:
        c = df["confidence"].dropna()
        if len(c) > 0:
            st.caption(
                f"rows={len(df)} | conf: min={c.min():.4f} mean={c.mean():.4f} p50={c.median():.4f} max={c.max():.4f}"
            )
        else:
            st.caption(f"rows={len(df)} | confidence had only nulls after filtering")

    st.dataframe(
        df[expected],
        use_container_width=True,
        hide_index=True,
    )
