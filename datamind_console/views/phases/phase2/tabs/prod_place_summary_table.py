# views/phases/phase2/widgets/evidence_coverage_card.py
from __future__ import annotations

from typing import Any, Dict, Optional

import streamlit as st
from datamind_console.i18n import t


def _as_kv_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove noisy / empty values, keep primitives + JSON-serializable.
    """
    out: Dict[str, Any] = {}
    for k, v in (row or {}).items():
        if v is None:
            continue
        # Skip huge blobs if any appear
        if k in {"params_used", "context", "raw", "payload"} and isinstance(v, (dict, list)):
            continue
        out[k] = v
    return out


def render_evidence_coverage_card(
    client: Any,
    *,
    place_set_id: Optional[str],
    title: Optional[str] = None,
    key_prefix: str = "p2_metrics",
) -> Optional[Dict[str, Any]]:
    """
    Phase 2 — Evidence coverage / metrics card

    STRICTLY aligned with your Phase2Client:
      - client.get_place_set_metrics(place_set_id) -> Optional[Dict[str,Any]]

    This widget does NOT assume specific metric columns exist.
    It renders:
      - a few common keys if present
      - full JSON view + key/value table fallback
    """
    st.subheader(title or t("p2.evidence_metrics_title"))

    if not place_set_id:
        st.info(t("ui.p124.prod_place_summary_table.select_place_set_id_view_metrics"))
        return None

    try:
        metrics = client.get_place_set_metrics(place_set_id)
    except Exception as e:
        st.error(t("ui.p124.prod_place_summary_table.failed_load_metrics_e", e=(e)))
        return None

    if not metrics:
        st.warning(t("ui.p124.prod_place_summary_table.no_metrics_row_found_this_place"))
        return None

    # Compact highlights if those keys exist (NO assumptions)
    highlights = []
    for k in [
        "n_places",
        "n_candidates",
        "n_nodes",
        "n_aliases",
        "alias_conflict_rate",
        "coverage",
        "coverage_rate",
        "avg_confidence",
        "min_confidence",
        "max_confidence",
        "updated_at",
        "created_at",
    ]:
        if k in metrics and metrics[k] is not None:
            highlights.append((k, metrics[k]))

    if highlights:
        cols = st.columns(min(4, len(highlights)))
        for i, (k, v) in enumerate(highlights):
            with cols[i % len(cols)]:
                st.metric(label=k, value=str(v))

    cleaned = _as_kv_dict(metrics)

    # Two useful render modes
    with st.expander(t("ui.p124.prod_place_summary_table.metrics_json"), expanded=False):
        st.json(metrics)

    with st.expander(t("ui.p124.prod_place_summary_table.metrics_table"), expanded=True):
        # Turn dict into a tiny 2-col table without pandas dependency
        kv_rows = [{"key": k, "value": cleaned.get(k)} for k in sorted(cleaned.keys())]
        st.dataframe(kv_rows, use_container_width=True)

    return metrics
