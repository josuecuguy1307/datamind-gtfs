from __future__ import annotations

from typing import Any

import streamlit as st
from datamind_console.i18n import t


def render_naming_candidate_insights(*, client: Any, route_id: str, key: str = "p4_name_insights") -> None:
    st.markdown(t("ui.p124.naming_candidate_insights.naming_insights"))

    try:
        sem = client.get_semantics(route_id)
    except Exception:
        sem = {}

    aliases = sem.get("route_aliases") or []
    c1, c2 = st.columns(2)
    c1.metric(t("ui.p124.naming_candidate_insights.aliases_count"), len(aliases))
    c2.metric(t("ui.p124.naming_candidate_insights.semantics_confidence"), f"{float(sem.get('naming_confidence') or 0):.3f}")

    if aliases:
        st.caption(t("ui.p124.naming_candidate_insights.aliases"))
        st.code("\n".join(str(a) for a in aliases), language="text")

    try:
        rows = client.list_name_candidates(route_id)
    except Exception as e:
        st.info(t("ui.p124.naming_candidate_insights.candidate_insights_unavailable_e", e=(e)))
        return

    if not rows:
        st.info(t("ui.p124.naming_candidate_insights.no_candidates_yet_this_route"))
        return

    chart_rows = []
    for r in rows:
        chart_rows.append(
            {
                "rank_pos": int(r.get("rank_pos") or 0),
                "heuristic_score": float(r.get("heuristic_score") or 0.0),
                "final_score": float(r.get("final_score") or 0.0),
            }
        )

    st.caption(t("ui.p124.naming_candidate_insights.top_candidate_score_curve"))
    st.line_chart(chart_rows, x="rank_pos", y=["heuristic_score", "final_score"], use_container_width=True)
