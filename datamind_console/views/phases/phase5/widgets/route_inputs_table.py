from __future__ import annotations

from typing import Any, Dict, List, Optional

import streamlit as st
from datamind_console.i18n import t


@st.cache_data(ttl=120, show_spinner=False)
def _cached_service_direction_inputs(
    *,
    _client: Any,
    verified_only: bool,
    limit: int,
) -> list[dict]:
    return _client.list_service_direction_inputs(verified_only=bool(verified_only), limit=int(limit))


def _short_id(v: Any, n: int = 8) -> str:
    s = str(v or "").strip()
    if not s:
        return "-"
    return s if len(s) <= n else s[:n]


def _group_service_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_sid: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sid = str(r.get("service_route_id") or "").strip()
        if not sid:
            continue
        did = int(r.get("direction_id") or 0)
        route_id = str(r.get("route_id") or "").strip()
        entry = by_sid.setdefault(
            sid,
            {
                "service_route_id": sid,
                "route_name": str(r.get("route_name") or ""),
                "route_ref": str(r.get("route_ref") or ""),
                "operator_name": str(r.get("operator_name") or ""),
                "route_id_0": "",
                "route_id_1": "",
                "n_stops_0": 0,
                "n_stops_1": 0,
                "geom_verified_0": False,
                "geom_verified_1": False,
                "naming_verified_0": False,
                "naming_verified_1": False,
            },
        )
        if did in (0, 1):
            entry[f"route_id_{did}"] = route_id
            entry[f"n_stops_{did}"] = int(r.get("n_stops") or 0)
            entry[f"geom_verified_{did}"] = bool(r.get("geom_verified"))
            entry[f"naming_verified_{did}"] = bool(r.get("naming_verified"))
            # Prefer latest non-empty metadata.
            if not entry.get("route_name"):
                entry["route_name"] = str(r.get("route_name") or "")
            if not entry.get("route_ref"):
                entry["route_ref"] = str(r.get("route_ref") or "")
            if not entry.get("operator_name"):
                entry["operator_name"] = str(r.get("operator_name") or "")

    out: List[Dict[str, Any]] = []
    for sid, e in by_sid.items():
        available = [d for d in (0, 1) if str(e.get(f"route_id_{d}") or "").strip()]
        e["available_directions"] = ",".join(str(x) for x in available) if available else "-"
        e["ready_pair"] = bool(len(available) == 2)
        e["service_route_short"] = _short_id(sid)
        out.append(e)
    out.sort(key=lambda x: str(x.get("service_route_id") or ""))
    return out


def render_route_inputs_table(*, client: Any, key: str = "p5_inputs") -> Optional[str]:
    ss = st.session_state
    st.markdown(t("ui.p5.route_inputs_table.route_inputs_service_route_id_direction"))
    verified_only = st.checkbox(t("ui.p5.route_inputs_table.verified_only"), value=True, key=f"{key}.verified")
    limit = st.number_input(t("ui.p5.route_inputs_table.limit"), min_value=20, max_value=1000, value=120, step=20, key=f"{key}.limit")
    c1, c2 = st.columns([1, 1])
    with c1:
        do_load = st.button(t("ui.p5.route_inputs_table.load_refresh_routes"), use_container_width=True, key=f"{key}.refresh")
    with c2:
        if st.button(t("ui.p5.route_inputs_table.clear_cache"), use_container_width=True, key=f"{key}.clear_cache"):
            st.cache_data.clear()
            st.rerun()

    if not do_load and f"{key}.rows" in st.session_state:
        rows = st.session_state.get(f"{key}.rows") or []
    elif not do_load:
        st.info(t("ui.p5.route_inputs_table.click_load_refresh_routes_query_db"))
        return None

    try:
        if do_load:
            rows = _cached_service_direction_inputs(_client=client, verified_only=bool(verified_only), limit=int(limit))
            st.session_state[f"{key}.rows"] = rows
    except Exception as e:
        st.error(t("ui.p5.route_inputs_table.failed_load_route_inputs_e", e=(e)))
        return None

    if not rows:
        st.info(t("ui.p5.route_inputs_table.no_routes_available_yet"))
        return None

    grouped = _group_service_rows([dict(x) for x in rows])
    if not grouped:
        st.info(t("ui.p5.route_inputs_table.no_service_route_id_rows_available"))
        return None

    st.dataframe(
        grouped,
        use_container_width=True,
        hide_index=True,
        column_order=[
            "service_route_id",
            "route_ref",
            "route_name",
            "operator_name",
            "route_id_0",
            "route_id_1",
            "available_directions",
            "ready_pair",
            "n_stops_0",
            "n_stops_1",
        ],
    )

    sid_options = [str(r.get("service_route_id") or "") for r in grouped if str(r.get("service_route_id") or "")]
    if not sid_options:
        return None

    current_sid = str(ss.get("phase5.service_route_id") or "")
    sid_idx = sid_options.index(current_sid) if current_sid in sid_options else 0
    selected_sid = st.selectbox(
        "service_route_id",
        sid_options,
        index=sid_idx,
        key=f"{key}.service_route_id",
        help=t("ui.p5.route_inputs_table.main_logical_route_id_shared_by"),
    )
    selected_group = next((r for r in grouped if str(r.get("service_route_id")) == str(selected_sid)), {})

    dir_available = [d for d in (0, 1) if str(selected_group.get(f"route_id_{d}") or "").strip()]
    if not dir_available:
        st.warning(t("ui.p5.route_inputs_table.selected_service_route_id_has_no"))
        ss["phase5.service_route_id"] = str(selected_sid)
        ss["phase5.route_id"] = ""
        return None

    current_dir = int(ss.get("phase5.direction_id") or dir_available[0])
    dir_idx = dir_available.index(current_dir) if current_dir in dir_available else 0
    selected_dir = st.radio(
        t("ui.p5.route_inputs_table.direction_toggle"),
        options=dir_available,
        index=dir_idx,
        horizontal=True,
        key=f"{key}.direction_toggle",
        format_func=lambda d: f"{d} | route_id:{_short_id(selected_group.get(f'route_id_{d}'))}",
    )

    selected_route_id = str(selected_group.get(f"route_id_{int(selected_dir)}") or "").strip()
    missing_other = 1 - int(selected_dir)
    if not str(selected_group.get(f"route_id_{missing_other}") or "").strip():
        st.caption(t("ui.p5.route_inputs_table.direction_missing_other_not_available_yet", missing_other=(missing_other)))

    ss["phase5.service_route_id"] = str(selected_sid)
    ss["phase5.direction_id"] = int(selected_dir)
    ss["phase5.route_id"] = selected_route_id

    st.divider()
    st.markdown(t("ui.p5.route_inputs_table.delete_phase_5_context_route_id"))
    st.caption(t("ui.p5.route_inputs_table.deletes_only_phase_5_generated_edit"))
    run_id = str(ss.get("phase5.export_run_id") or "").strip()
    d1, d2 = st.columns(2)
    with d1:
        confirm_cleanup = st.checkbox(
            t("ui.p5.route_inputs_table.confirm_cleanup_selected_route_direction"),
            value=False,
            key=f"{key}.cleanup.confirm",
        )
    with d2:
        drop_logs = st.checkbox(
            t("ui.p5.route_inputs_table.also_delete_matching_revision_logs"),
            value=False,
            key=f"{key}.cleanup.logs",
        )
    if not run_id:
        st.info(t("ui.p5.route_inputs_table.select_create_export_run_id_first"))
    if st.button(
        t("ui.p5.route_inputs_table.delete_phase_5_context_now"),
        use_container_width=True,
        key=f"{key}.cleanup.run",
        disabled=(not run_id or not selected_route_id),
    ):
        if not confirm_cleanup:
            st.warning(t("ui.p5.route_inputs_table.confirm_cleanup_first"))
        else:
            try:
                out_cleanup = client.delete_phase5_route_direction_context(
                    export_run_id=run_id,
                    route_id=selected_route_id,
                    direction_id=int(selected_dir),
                    delete_revision_logs=bool(drop_logs),
                )
            except Exception as e:
                st.error(t("ui.p5.route_inputs_table.cleanup_failed_e", e=(e)))
            else:
                st.success(t("ui.p5.route_inputs_table.cleanup_completed"))
                st.json(out_cleanup)
                st.cache_data.clear()

    st.caption(t("ui.p5.route_inputs_table.bulk_cleanup"))
    b1, b2 = st.columns(2)
    with b1:
        confirm_bulk = st.checkbox(
            t("ui.p5.route_inputs_table.confirm_full_cleanup_all_route_id"),
            value=False,
            key=f"{key}.cleanup.bulk.confirm",
        )
    with b2:
        bulk_verified_only = st.checkbox(
            t("ui.p5.route_inputs_table.bulk_verified_routes_only"),
            value=False,
            key=f"{key}.cleanup.bulk.verified",
        )
    if st.button(
        t("ui.p5.route_inputs_table.delete_all_phase_5_route_context"),
        use_container_width=True,
        key=f"{key}.cleanup.bulk.run",
        disabled=(not run_id),
    ):
        if not confirm_bulk:
            st.warning(t("ui.p5.route_inputs_table.confirm_full_cleanup_first"))
        else:
            try:
                out_bulk = client.delete_phase5_all_route_context(
                    export_run_id=run_id,
                    delete_revision_logs=bool(drop_logs),
                    verified_only=bool(bulk_verified_only),
                )
            except Exception as e:
                st.error(t("ui.p5.route_inputs_table.bulk_cleanup_failed_e", e=(e)))
            else:
                if bool(out_bulk.get("ok")):
                    st.success(t("ui.p5.route_inputs_table.bulk_cleanup_completed"))
                else:
                    st.warning(t("ui.p5.route_inputs_table.bulk_cleanup_finished_some_failures"))
                st.json(out_bulk)
                st.cache_data.clear()

    return selected_route_id or None
