# views/phases/phase2/widgets/match_score_distribution.py
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
import streamlit as st
from datamind_console.i18n import t


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
    st.subheader(t("ui.p124.match_score_distribution.match_score_distribution_alias_confidence"))

    if not place_set_id:
        st.info(t("ui.p124.match_score_distribution.select_place_set_id_first"))
        return

    try:
        rows: List[Dict[str, Any]] = client.get_alias_confidence_distribution(place_set_id)
    except Exception as e:
        st.error(t("ui.p124.match_score_distribution.failed_load_confidence_distribution_e", e=(e)))
        return

    if not rows:
        st.warning(t("ui.p124.match_score_distribution.no_confidence_scores_found_this_place"))
        return

    df = pd.DataFrame(rows)
    if "confidence" not in df.columns:
        st.error(t("ui.p124.match_score_distribution.client_returned_rows_without_confidence"))
        return

    # Normalize to numeric + drop nulls
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    df = df.dropna(subset=["confidence"])

    if df.empty:
        st.warning(t("ui.p124.match_score_distribution.all_confidence_values_were_null_unparseable"))
        return

    # Small stats header
    c = df["confidence"]
    st.caption(
        t("ui.p124.match_score_distribution.n_n_c_min_min_p50", n_c=(len(c)), min=format((c.min()), '.4f'), median=format((c.median()), '.4f'), mean=format((c.mean()), '.4f'), quantile=format((c.quantile(0.9)), '.4f'), max=format((c.max()), '.4f'))
    )

    # Controls (UI only)
    cols = st.columns([1, 1, 1])
    with cols[0]:
        bins = st.number_input(t("ui.p124.match_score_distribution.bins"), min_value=5, max_value=200, value=int(bins), step=5, key=f"{key_prefix}:bins")
    with cols[1]:
        clamp01 = st.checkbox(t("ui.p124.match_score_distribution.clamp_0_1"), value=False, key=f"{key_prefix}:clamp")
    with cols[2]:
        show_table = st.checkbox(t("ui.p124.match_score_distribution.show_raw_values"), value=False, key=f"{key_prefix}:table")

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
