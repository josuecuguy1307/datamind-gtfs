from __future__ import annotations

from typing import Any

import streamlit as st
from datamind_console.i18n import t


def _all_route_ids_from_inputs(client: Any) -> list[str]:
    try:
        rows = client.list_service_direction_inputs(verified_only=False, limit=20000) or []
    except Exception:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        rid = str((r or {}).get("route_id") or "").strip()
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def render_step015_stops_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown(t("ui.p5.step015_stops.step_01_load_gtfs_stops_from"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    gtfs_id = (st.session_state.get("phase5.gtfs_id") or "").strip()

    st.caption(t("ui.p5.step015_stops.context_gtfs_id_gtfs_id", gtfs_id=(gtfs_id or '-')))
    st.caption(t("ui.p5.step015_stops.source_phase_2_final_prod_canonical"))
    st.caption(t("ui.p5.step015_stops.this_step_independent_from_route_service"))
    st.caption(t("ui.p5.step015_stops.this_step_previews_what_will_be"))

    if not run_id:
        st.info(t("ui.p5.step015_stops.create_select_gtfs_id_first_internal"))
        return

    prev_key = "p5.step01stops.preview_rows"
    sets_key = "p5.step01stops.place_sets"
    scope_mode = st.radio(
        t("ui.p5.step015_stops.phase_2_stop_source_scope"),
        options=["Full Phase 2 final prod", "Specific Phase 2 place_set"],
        format_func=lambda v: t("p5.stops_scope.specific") if v == "Specific Phase 2 place_set" else t("p5.stops_scope.full"),
        horizontal=True,
        key="p5.step01stops.scope_mode",
    )
    selected_place_set_id = ""
    if scope_mode == "Specific Phase 2 place_set":
        if st.button(t("ui.p5.step015_stops.load_phase_2_place_sets"), key="p5.step01stops.load_sets"):
            try:
                st.session_state[sets_key] = client.list_phase2_place_sets_for_gtfs_stops(limit=500)
            except Exception as e:
                st.error(t("ui.p5.step015_stops.failed_load_phase_2_place_sets", e=(e)))
                st.session_state[sets_key] = []
        place_sets = [dict(r) for r in (st.session_state.get(sets_key) or [])]
        if place_sets:
            labels = []
            by_label: dict[str, str] = {}
            for r in place_sets:
                psid = str(r.get("place_set_id") or "")
                lbl = f"{psid} | stops:{int(r.get('n_stop_rows') or 0)} | points:{int(r.get('n_points') or 0)}"
                labels.append(lbl)
                by_label[lbl] = psid
            chosen = st.selectbox(t("ui.p5.step015_stops.select_phase_2_place_set_id"), labels, key="p5.step01stops.place_set_label")
            selected_place_set_id = by_label.get(chosen, "")
            st.caption(t("ui.p5.step015_stops.selected_place_set_id_selected_place", selected_place_set_id=(selected_place_set_id)))
        else:
            st.info(t("ui.p5.step015_stops.load_phase_2_place_sets_choose"))

    lim = st.number_input(t("ui.p5.step015_stops.preview_limit"), min_value=50, max_value=5000, value=1000, step=50, key="p5.step01stops.limit")
    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button(t("ui.p5.step015_stops.preview_stops_load_phase_2_final"), use_container_width=True, key="p5.step01stops.preview"):
            try:
                st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(
                    limit=int(lim),
                    place_set_id=(selected_place_set_id or None),
                )
            except TypeError as e:
                msg = str(e)
                if "place_set_id" in msg:
                    if selected_place_set_id:
                        st.error(t("ui.p5.step015_stops.this_running_app_version_does_not"))
                        st.session_state[prev_key] = []
                    else:
                        try:
                            st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(limit=int(lim))
                            st.warning(t("ui.p5.step015_stops.compatibility_fallback_active_old_client_signature"))
                        except Exception as e2:
                            st.error(t("ui.p5.step015_stops.preview_failed_e2", e2=(e2)))
                            st.session_state[prev_key] = []
                elif "route_ids" in msg:
                    try:
                        all_route_ids = _all_route_ids_from_inputs(client)
                        st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(
                            route_ids=all_route_ids,
                            limit=int(lim),
                        )
                        st.warning(t("ui.p5.step015_stops.compatibility_fallback_active_old_client_signature_2"))
                    except Exception as e2:
                        st.error(t("ui.p5.step015_stops.preview_failed_e2", e2=(e2)))
                        st.session_state[prev_key] = []
                else:
                    st.error(t("ui.p5.step015_stops.preview_failed_e", e=(e)))
                    st.session_state[prev_key] = []
            except Exception as e:
                st.error(t("ui.p5.step015_stops.preview_failed_e", e=(e)))
                st.session_state[prev_key] = []
    with c2:
        if st.button(t("ui.p5.step015_stops.run_step_01_upsert_gtfs_stops"), type="primary", use_container_width=True, key="p5.step01stops.run"):
            try:
                out = client.run_step_015_load_stops_from_phase2_final(
                    export_run_id=run_id,
                    route_ids=None,
                    place_set_id=(selected_place_set_id or None),
                )
            except TypeError as e:
                msg = str(e)
                if "place_set_id" in msg:
                    if selected_place_set_id:
                        st.error(t("ui.p5.step015_stops.this_running_app_version_does_not"))
                        out = None
                    else:
                        try:
                            out = client.run_step_015_load_stops_from_phase2_final(
                                export_run_id=run_id,
                                route_ids=None,
                            )
                            st.warning(t("ui.p5.step015_stops.compatibility_fallback_active_old_client_signature_3"))
                        except Exception as e2:
                            st.error(str(e2))
                            out = None
                elif "route_ids" in msg:
                    try:
                        all_route_ids = _all_route_ids_from_inputs(client)
                        out = client.run_step_015_load_stops_from_phase2_final(
                            export_run_id=run_id,
                            route_ids=all_route_ids,
                        )
                        st.warning(t("ui.p5.step015_stops.compatibility_fallback_active_old_client_signature_4"))
                    except Exception as e2:
                        st.error(str(e2))
                        out = None
                else:
                    st.error(str(e))
                    out = None
            except Exception as e:
                st.error(str(e))
                out = None
            else:
                pass

            if out:
                st.success(t("ui.p5.step015_stops.step_01_completed"))
                st.json(out)
                try:
                    st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(
                        limit=int(lim),
                        place_set_id=(selected_place_set_id or None),
                    )
                except TypeError:
                    try:
                        st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(limit=int(lim))
                    except TypeError:
                        try:
                            all_route_ids = _all_route_ids_from_inputs(client)
                            st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(route_ids=all_route_ids, limit=int(lim))
                        except Exception:
                            pass
                    except Exception:
                        pass
                except Exception:
                    pass

    preview_rows = [dict(r) for r in (st.session_state.get(prev_key) or [])]
    if preview_rows:
        st.markdown(t("ui.p5.step015_stops.preview_stops_load_into_selected_gtfs"))
        st.caption(t("ui.p5.step015_stops.rows_n_preview_rows_columns_shown", n_preview_rows=(len(preview_rows))))
        show_cols = ["stop_id", "stop_name", "stop_lat", "stop_lon", "place_id", "place_type", "node_type"]
        slim_rows = [{k: r.get(k) for k in show_cols if k in r} for r in preview_rows]
        st.dataframe(slim_rows, use_container_width=True, hide_index=True, height=260)
    else:
        st.info(t("ui.p5.step015_stops.click_preview_visualize_canonical_phase_2"))

    st.divider()
    st.markdown(t("ui.p5.step015_stops.current_gtfs_stops_selected_gtfs_id"))
    try:
        current_rows = client.list_gtfs_rows("gtfs_stops", run_id, limit=int(lim)) or []
    except Exception as e:
        st.error(t("ui.p5.step015_stops.failed_load_current_gtfs_stops_e", e=(e)))
        return

    if current_rows:
        show_cols = ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"]
        slim_current = [{k: r.get(k) for k in show_cols if k in r} for r in current_rows]
        st.dataframe(slim_current, use_container_width=True, hide_index=True, height=240)
    else:
        st.info(t("ui.p5.step015_stops.no_gtfs_stops_inserted_yet_this"))
