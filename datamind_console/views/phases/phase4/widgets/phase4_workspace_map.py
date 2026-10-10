from __future__ import annotations

from typing import Any, List

import streamlit as st
from datamind_console.i18n import t


def render_phase4_workspace_map(*, client: Any, key: str = "p4_workspace") -> None:
    ss = st.session_state

    st.markdown(t("ui.p124.phase4_workspace_map.workspace_map"))

    # Build route selector options from DB
    route_ids: List[str] = []
    try:
        pending = client.list_pending(limit=300)
        route_ids.extend([str(r.route_id) for r in pending if getattr(r, "route_id", None)])
    except Exception:
        pass

    try:
        sem_rows = client.list_semantics(limit=300)
        route_ids.extend([str(r.get("route_id")) for r in sem_rows if r.get("route_id")])
    except Exception:
        pass

    # de-duplicate while preserving order
    seen = set()
    route_ids = [x for x in route_ids if not (x in seen or seen.add(x))]

    if route_ids:
        current = (ss.get("phase4.route_id") or "").strip()
        idx = route_ids.index(current) if current in route_ids else 0
        pick = st.selectbox("route_id", route_ids, index=idx, key=f"{key}.route_id")
        ss["phase4.route_id"] = pick
    else:
        pick = (ss.get("phase4.route_id") or "").strip()
        st.caption(t("ui.p124.phase4_workspace_map.no_route_id_options_available_yet"))

    st.markdown(t("ui.p124.phase4_workspace_map.naming_seed"))
    try:
        seed = client.get_seed_summary(pick) if pick else {}
    except Exception:
        seed = {}

    if seed:
        seed_payload = seed.get("seed_payload") or {}
        gtfs_route = seed_payload.get("gtfs_route") if isinstance(seed_payload, dict) else {}
        gtfs_short = ""
        gtfs_long = ""
        if isinstance(gtfs_route, dict):
            gtfs_short = str(gtfs_route.get("route_short_name") or "").strip()
            gtfs_long = str(gtfs_route.get("route_long_name") or "").strip()
        st.caption(
            t("ui.p124.phase4_workspace_map.source_seed_source_ref_seed_route", seed_source=(seed.get('seed_source') or '-'), seed_route_ref=(seed.get('seed_route_ref') or '-'), seed_operator_name=(seed.get('seed_operator_name') or '-'))
        )
        if gtfs_short or gtfs_long:
            st.caption(t("ui.p124.phase4_workspace_map.gtfs_names_short_gtfs_short_long", gtfs_short=(gtfs_short or '-'), gtfs_long=(gtfs_long or '-')))
        st.code(
            (seed.get("seed_route_name") or t("p4map.no_route_name_seed")) + "\n"
            + f"{seed.get('seed_from_name') or '-'} -> {seed.get('seed_to_name') or '-'}",
            language="text",
        )
    else:
        st.info(t("ui.p124.phase4_workspace_map.no_naming_seed_yet_run_step"))

    mode = st.selectbox(
        t("ui.p124.phase4_workspace_map.display"),
        ["Route geometry", "Seed summary", "Name candidates", "Review status"],
        index=1,
        format_func=lambda v: t(f"p4map.mode.{v.lower().replace(' ', '_')}"),
        key=f"{key}.mode",
    )

    if not pick:
        st.info(t("ui.p124.phase4_workspace_map.select_route_id_render_workspace_data"))
        return

    if mode == "Route geometry":
        # Reuse existing geometry widget
        try:
            from views.phases.phase4.widgets.route_geometry_map import render_route_geometry_map

            render_route_geometry_map(pick, key=f"{key}.geom")
        except Exception as e:
            st.error(t("ui.p124.phase4_workspace_map.failed_render_route_geometry_e", e=(e)))

    elif mode == "Seed summary":
        try:
            row = client.get_seed_summary(pick)
        except Exception as e:
            st.error(t("ui.p124.phase4_workspace_map.failed_load_seed_summary_e", e=(e)))
            row = {}
        if row:
            st.json(row)
        else:
            st.info(t("ui.p124.phase4_workspace_map.no_seed_summary_yet_run_step"))

    elif mode == "Name candidates":
        try:
            rows = client.list_name_candidates(pick)
        except Exception as e:
            st.error(t("ui.p124.phase4_workspace_map.failed_load_name_candidates_e", e=(e)))
            rows = []
        if rows:
            st.dataframe(rows, use_container_width=True, height=280)
            options = [f"{r.get('candidate_id')} | {r.get('route_name')}" for r in rows]
            selected = st.selectbox(t("ui.p124.phase4_workspace_map.active_candidate"), options, key=f"{key}.active_candidate")
            st.session_state["phase4.active_candidate_id"] = selected.split("|", 1)[0].strip()
        else:
            st.info(t("ui.p124.phase4_workspace_map.no_candidates_yet_run_step_20"))

    else:
        try:
            rows = client.list_semantics(limit=300)
            rows = [r for r in rows if str(r.get("route_id")) == pick]
        except Exception as e:
            st.error(t("ui.p124.phase4_workspace_map.failed_load_review_status_e", e=(e)))
            rows = []
        if rows:
            st.dataframe(rows, use_container_width=True, height=280)
        else:
            st.info(t("ui.p124.phase4_workspace_map.no_approved_semantics_row_yet_run"))
