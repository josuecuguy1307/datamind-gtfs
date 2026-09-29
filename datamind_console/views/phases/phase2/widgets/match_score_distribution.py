# views/phases/phase2/widgets/match_score_distribution.py
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
import streamlit as st


def render_match_score_distribution(
    client: Any,
    *,
    place_set_id: str,
    bins: int = 30,
    key_prefix: str = "p2_matchdist",
) -> None:
    """
    Phase 2 — Match score distribution

    STRICTLY aligned with your current Phase2Client:
      - Uses: client.get_alias_confidence_distribution(place_set_id)
      - Data you can plot from returned rows:
          confidence   (alias candidate score)
    """
    st.subheader("Match score distribution (alias confidence)")

    if not place_set_id:
        st.info("Select a place_set_id first.")
        return

    try:
        rows: List[Dict[str, Any]] = client.get_alias_confidence_distribution(place_set_id)
    except Exception as e:
        st.error(f"Failed to load confidence distribution: {e}")
        return

    if not rows:
        st.warning("No confidence scores found for this place_set_id.")
        return

    df = pd.DataFrame(rows)
    if "confidence" not in df.columns:
        st.error("Client returned rows without 'confidence'.")
        return

    # Normalize to numeric + drop nulls
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    df = df.dropna(subset=["confidence"])

    if df.empty:
        st.warning("All confidence values were null/unparseable.")
        return

    # Small stats header
    c = df["confidence"]
    st.caption(
        f"n={len(c)} | min={c.min():.4f} | p50={c.median():.4f} | mean={c.mean():.4f} | p90={c.quantile(0.9):.4f} | max={c.max():.4f}"
    )

    # Controls (UI only)
    cols = st.columns([1, 1, 1])
    with cols[0]:
        bins = st.number_input("Bins", min_value=5, max_value=200, value=int(bins), step=5, key=f"{key_prefix}:bins")
    with cols[1]:
        clamp01 = st.checkbox("Clamp to [0,1]", value=False, key=f"{key_prefix}:clamp")
    with cols[2]:
        show_table = st.checkbox("Show raw values", value=False, key=f"{key_prefix}:table")

    data = c.copy()
    if clamp01:
        data = data.clip(lower=0.0, upper=1.0)

    # Convert interval bins to plain strings (Altair/Streamlit schema-safe).
    hist = pd.cut(data, bins=int(bins)).value_counts().sort_index()
    hist_df = hist.reset_index()
    hist_df.columns = ["bin", "count"]
    hist_df["bin"] = hist_df["bin"].astype(str)
    hist_df["count"] = pd.to_numeric(hist_df["count"], errors="coerce").fillna(0).astype(int)

    st.bar_chart(
        hist_df.set_index("bin")["count"],
        use_container_width=True,
    )

    if show_table:
        st.dataframe(
            pd.DataFrame({"confidence": data}),
            use_container_width=True,
            hide_index=True,
        )
