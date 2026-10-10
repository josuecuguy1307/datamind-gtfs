from __future__ import annotations

from typing import Any, Dict, Optional
import streamlit as st

from datamind_console.phases.phase4_naming.client import _get_phase4_client
from datamind_console.i18n import t


def render_semantics_search_table(*, key: str = "p4_search") -> Optional[str]:
    """
    Uses client.search() (FTS over route_prod.route_semantics).
    Returns selected route_id (or None).
    """
    c = _get_phase4_client()

    q = st.text_input(t("ui.p124.semantics_search_table.search_routes_name_ref_operator"), value="", key=f"{key}_q")
    limit = st.number_input(t("ui.p124.semantics_search_table.limit"), min_value=5, max_value=200, value=25, step=5, key=f"{key}_limit")
    offset = st.number_input(t("ui.p124.semantics_search_table.offset"), min_value=0, value=0, step=25, key=f"{key}_offset")

    if not q.strip():
        st.info(t("ui.p124.semantics_search_table.type_something_search"))
        return None

    try:
        res = c.search(q, limit=int(limit), offset=int(offset))
    except Exception as e:
        st.error(t("ui.p124.semantics_search_table.search_failed_e", e=(e)))
        return None

    data: list[Dict[str, Any]] = []
    for r in res:
        data.append(
            {
                "route_id": r.route_id,
                "service_route_id": (r.raw or {}).get("service_route_id"),
                "direction_id": (r.raw or {}).get("direction_id"),
                "label": r.label,
                "score": r.score,
                "status": r.status,
                "operator": (r.raw or {}).get("operator_name"),
                "ref": (r.raw or {}).get("route_ref"),
                "human_verified": (r.raw or {}).get("human_verified"),
            }
        )

    st.dataframe(data, use_container_width=True, hide_index=True)

    chosen = st.text_input(t("ui.p124.semantics_search_table.open_route_id"), value="", key=f"{key}_open")
    return chosen.strip() or None
