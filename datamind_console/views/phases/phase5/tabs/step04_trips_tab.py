from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview
from datamind_console.i18n import t


def _suggested_blocks(runtime_offpeak_secs: int, offpeak_headway_secs: int, peak_headway_secs: int) -> int:
    rt = max(300, int(runtime_offpeak_secs or 0))
    off_h = max(60, int(offpeak_headway_secs or 0) or 600)
    peak_h = int(peak_headway_secs or 0)
    if peak_h <= 0:
        peak_h = off_h
    min_h = max(60, min(off_h, peak_h))
    return max(1, int(((rt * 2.0) + float(min_h) - 1.0) // float(min_h)))


def render_step04_trips_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown(t("ui.p5.step04_trips.step_04_build_trips_stop_times"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip()
    service_route_id = (st.session_state.get("phase5.service_route_id") or "").strip()
    selected_direction = int(st.session_state.get("phase5.direction_id") or 0)
    run_mode = str(st.session_state.get("phase5.run_mode") or "One by one")
    bulk_mode = run_mode == "Bulk (multi-route)"
    if not run_id:
        st.info(t("ui.p5.step04_trips.create_select_gtfs_id_first_internal"))
        return

    st.caption(
        t("ui.p5.step04_trips.context_service_route_id_service_route", service_route_id=(service_route_id or '-'), selected_direction=(selected_direction), route_id=(route_id or '-'))
    )
    st.caption(t("ui.p5.step04_trips.this_step_runs_step_04_trips"))
    if bulk_mode:
        st.info(t("ui.p5.step04_trips.bulk_mode_enabled_use_bulk_section"))

    st.divider()
    st.markdown(t("ui.p5.step04_trips.bulk_run_multiple_routes_directions"))
    st.caption(t("ui.p5.step04_trips.runs_step_04_05_across_every"))
    bulk_verified = st.checkbox(t("ui.p5.step04_trips.bulk_verified_routes_only"), value=True, key="p5.step04.bulk.verified")
    bulk_limit_current = st.checkbox(
        t("ui.p5.step04_trips.limit_bulk_current_service_route_id"),
        value=False,
        key="p5.step04.bulk.limit_current",
    )
    bulk_require_binding = st.checkbox(
        t("ui.p5.step04_trips.require_runtime_lab_estimate_skip_missing"),
        value=True,
        key="p5.step04.bulk.require_binding",
    )
    skip_if_exists = st.checkbox(
        t("ui.p5.step04_trips.skip_if_trips_already_exist_this"),
        value=True,
        key="p5.step04.bulk.skip_existing",
    )
    auto_apply_blocks = st.checkbox(
        t("ui.p5.step04_trips.auto_apply_suggested_n_blocks_per"),
        value=True,
        key="p5.step04.bulk.auto_blocks",
    )
    try:
        bulk_rows = client.list_service_direction_inputs(verified_only=bool(bulk_verified), limit=3000)
    except Exception as e:
        st.error(t("ui.p5.step04_trips.failed_load_bulk_inputs_e", e=(e)))
        bulk_rows = []
    if bulk_limit_current and service_route_id:
        bulk_rows = [r for r in bulk_rows if str(r.get("service_route_id") or "") == service_route_id]
    st.caption(t("ui.p5.step04_trips.eligible_pairs_n_bulk_rows", n_bulk_rows=(len(bulk_rows))))
    if st.button(t("ui.p5.step04_trips.run_step_04_step_05_bulk"), type="primary", use_container_width=True, key="p5.step04.bulk.run"):
        if not bulk_rows:
            st.warning(t("ui.p5.step04_trips.no_eligible_route_direction_pairs"))
        else:
            missing = []
            for pair in bulk_rows:
                try:
                    binding = client.get_runtime_estimate_binding(
                        route_id=str(pair.get("route_id") or ""),
                        direction_id=int(pair.get("direction_id") or 0),
                    )
                except Exception:
                    binding = {}
                if not binding or not str(binding.get("estimate_id") or "").strip():
                    missing.append(pair)
            if missing and bulk_require_binding:
                st.warning(t("ui.p5.step04_trips.some_routes_missing_runtime_lab_estimates"))
            skip_set = {(m.get("route_id"), int(m.get("direction_id") or 0)) for m in missing}
            results = []
            with st.spinner(t("ui.p5.step04_trips.running_step_04_step_05_bulk")):
                for pair in bulk_rows:
                    rid = str(pair.get("route_id") or "")
                    did = int(pair.get("direction_id") or 0)
                    if (rid, did) in skip_set:
                        results.append(
                            {"route_id": rid, "direction_id": did, "ok": False, "skipped": True, "reason": "missing_binding"}
                        )
                        continue
                    if skip_if_exists:
                        try:
                            existing = client.list_generated_trips_with_departures(
                                export_run_id=run_id,
                                route_id=rid,
                                direction_id=did,
                                limit=1,
                            )
                        except Exception:
                            existing = []
                        if existing:
                            results.append(
                                {"route_id": rid, "direction_id": did, "ok": True, "skipped": True, "reason": "already_exists"}
                            )
                            continue
                    try:
                        applied_blocks = None
                        if auto_apply_blocks:
                            binding = client.get_runtime_estimate_binding(
                                route_id=str(rid),
                                direction_id=int(did),
                            )
                            rt_off = int(binding.get("runtime_offpeak_secs") or 0)
                            off_h = int(binding.get("offpeak_headway_secs") or 0)
                            peak_h = int(binding.get("peak_headway_secs") or 0)
                            suggested = _suggested_blocks(rt_off, off_h, peak_h)
                            client.upsert_profile(
                                route_id=rid,
                                direction_id=int(did),
                                service_name="weekday_base",
                                runtime_secs=max(300, int(rt_off or 3600)),
                                dwell_secs=0,
                                n_blocks=int(suggested),
                                is_active=True,
                            )
                            applied_blocks = int(suggested)
                        out4 = client.run_step_04_trips(run_id, route_id=rid, direction_id=did)
                        out5 = client.run_step_05_stop_times(run_id, route_id=rid, direction_id=did)
                        results.append(
                            {
                                "route_id": rid,
                                "direction_id": did,
                                "ok": True,
                                "trips": int(out4.get("trips") or 0),
                                "stop_times": int(out5.get("stop_times") or 0),
                                "n_blocks": applied_blocks,
                            }
                        )
                    except Exception as e:
                        results.append({"route_id": rid, "direction_id": did, "ok": False, "error": str(e)})
            st.success(t("ui.p5.step04_trips.bulk_step_04_step_05_finished"))
            st.dataframe(results, use_container_width=True, hide_index=True, height=260)

    if not bulk_mode:
        st.divider()
        st.markdown(t("ui.p5.step04_trips.cleanup_block_route_id_direction_id"))
        st.caption(t("ui.p5.step04_trips.deletes_phase_5_generated_context_this"))
        cl1, cl2 = st.columns(2)
        with cl1:
            confirm_cleanup = st.checkbox(t("ui.p5.step04_trips.confirm_cleanup_selected_route_direction"), value=False, key="p5.step04.cleanup.confirm")
        with cl2:
            drop_logs = st.checkbox(t("ui.p5.step04_trips.also_delete_matching_revision_logs"), value=False, key="p5.step04.cleanup.logs")
        if st.button(t("ui.p5.step04_trips.delete_phase_5_context_route_direction"), use_container_width=True, key="p5.step04.cleanup.run"):
            if not confirm_cleanup:
                st.warning(t("ui.p5.step04_trips.confirm_cleanup_first"))
            else:
                try:
                    out_cleanup = client.delete_phase5_route_direction_context(
                        export_run_id=run_id,
                        route_id=route_id,
                        direction_id=selected_direction,
                        delete_revision_logs=bool(drop_logs),
                    )
                except Exception as e:
                    st.error(t("ui.p5.step04_trips.cleanup_failed_e", e=(e)))
                else:
                    st.success(t("ui.p5.step04_trips.cleanup_completed"))
                    st.json(out_cleanup)

        st.divider()
        r1, r2 = st.columns(2)
        with r1:
            if st.button(t("ui.p5.step04_trips.run_step_04_step_05"), type="primary", use_container_width=True, key="p5.step04plus.run"):
                with st.spinner(t("ui.p5.step04_trips.building_trips_frequencies_stop_times")):
                    try:
                        out4 = client.run_step_04_trips(run_id, route_id=route_id, direction_id=selected_direction)
                        out5 = client.run_step_05_stop_times(run_id, route_id=route_id, direction_id=selected_direction)
                    except Exception as e:
                        st.error(str(e))
                        return
                st.success(t("ui.p5.step04_trips.step_04_step_05_completed"))
                st.json({"step_04": out4, "step_05": out5})
        with r2:
            with st.expander(t("ui.p5.step04_trips.advanced_run_controls"), expanded=False):
                if st.button(t("ui.p5.step04_trips.run_step_04_only"), use_container_width=True, key="p5.step04.only"):
                    try:
                        out4_only = client.run_step_04_trips(run_id, route_id=route_id, direction_id=selected_direction)
                    except Exception as e:
                        st.error(str(e))
                    else:
                        st.success(t("ui.p5.step04_trips.step_04_completed"))
                        st.json(out4_only)
                if st.button(t("ui.p5.step04_trips.run_step_05_only"), use_container_width=True, key="p5.step05.only"):
                    try:
                        out5_only = client.run_step_05_stop_times(run_id, route_id=route_id, direction_id=selected_direction)
                    except Exception as e:
                        st.error(str(e))
                    else:
                        st.success(t("ui.p5.step04_trips.step_05_completed"))
                        st.json(out5_only)

    st.markdown(t("ui.p5.step04_trips.output_preview_by_direction"))
    preview_limit = st.number_input(t("ui.p5.step04_trips.preview_rows_limit"), min_value=50, max_value=5000, value=600, step=50, key="p5.step04.preview.limit")
    route_pairs: list[dict[str, Any]] = []
    try:
        ctx_rows = client.list_service_direction_inputs(verified_only=False, limit=2000)
    except Exception:
        ctx_rows = []
    if service_route_id:
        for r in (ctx_rows or []):
            if str(r.get("service_route_id") or "") == service_route_id:
                rid = str(r.get("route_id") or "").strip()
                did = int(r.get("direction_id") or 0)
                if rid:
                    route_pairs.append(
                        {
                            "route_id": rid,
                            "direction_id": did,
                            "route_name": str(r.get("route_name") or ""),
                            "route_ref": str(r.get("route_ref") or ""),
                        }
                    )
    if not route_pairs and route_id:
        route_pairs = [{"route_id": route_id, "direction_id": selected_direction, "route_name": "", "route_ref": ""}]

    if not route_pairs:
        st.info(t("ui.p5.step04_trips.select_service_route_id_direction_route"))
    else:
        for pair in sorted(route_pairs, key=lambda x: int(x.get("direction_id") or 0)):
            rid = str(pair.get("route_id") or "")
            did = int(pair.get("direction_id") or 0)
            title = (
                f"Direction {did} | route_id `{rid[:8]}`"
                + (f" | ref `{pair.get('route_ref')}`" if str(pair.get("route_ref") or "").strip() else "")
                + (f" | {pair.get('route_name')}" if str(pair.get("route_name") or "").strip() else "")
            )
            with st.expander(title, expanded=(did == selected_direction)):
                try:
                    dir_profiles = [
                        r for r in client.list_profiles(route_id=rid, limit=50)
                        if int(r.get("direction_id") or 0) == did
                    ]
                except Exception as e:
                    st.error(t("ui.p5.step04_trips.failed_load_profiles_windows_preview_e", e=(e)))
                    dir_profiles = []
                if dir_profiles:
                    st.caption(t("ui.p5.step04_trips.schedule_profiles_used"))
                    st.dataframe(dir_profiles, use_container_width=True, hide_index=True, height=120)
                    all_windows = []
                    for p in dir_profiles:
                        pid = str(p.get("profile_id") or "").strip()
                        if not pid:
                            continue
                        try:
                            ws = client.list_windows(pid)
                        except Exception:
                            ws = []
                        for w in ws:
                            ww = dict(w)
                            ww["profile_id"] = pid
                            all_windows.append(ww)
                    if all_windows:
                        st.caption(t("ui.p5.step04_trips.frequency_windows_used"))
                        st.dataframe(all_windows, use_container_width=True, hide_index=True, height=150)

                try:
                    trip_rows = client.list_generated_trips_with_departures(
                        export_run_id=run_id,
                        route_id=rid,
                        direction_id=did,
                        limit=int(preview_limit),
                    )
                except Exception as e:
                    st.error(t("ui.p5.step04_trips.failed_load_generated_trips_preview_e", e=(e)))
                    trip_rows = []
                if trip_rows:
                    block_ids = sorted({str(x.get("block_id") or "") for x in trip_rows if str(x.get("block_id") or "").strip()})
                    dep_vals = [str(x.get("departure_time") or "") for x in trip_rows if str(x.get("departure_time") or "").strip()]
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric(t("ui.p5.step04_trips.trips"), len(trip_rows))
                    c2.metric(t("ui.p5.step04_trips.blocks"), len(block_ids))
                    c3.metric(t("ui.p5.step04_trips.first_departure"), min(dep_vals) if dep_vals else "-")
                    c4.metric(t("ui.p5.step04_trips.last_departure"), max(dep_vals) if dep_vals else "-")
                    st.caption(t("ui.p5.step04_trips.generated_trips_block_departure"))
                    st.dataframe(trip_rows, use_container_width=True, hide_index=True, height=240)
                else:
                    st.info(t("ui.p5.step04_trips.no_trips_generated_yet_this_direction"))

                try:
                    freq_rows = client.list_generated_frequencies(
                        export_run_id=run_id,
                        route_id=rid,
                        direction_id=did,
                        limit=int(preview_limit),
                    )
                except Exception as e:
                    st.error(t("ui.p5.step04_trips.failed_load_generated_frequencies_preview_e", e=(e)))
                    freq_rows = []
                if freq_rows:
                    st.caption(t("ui.p5.step04_trips.generated_frequencies_rows"))
                    st.dataframe(freq_rows, use_container_width=True, hide_index=True, height=180)
                else:
                    st.caption(t("ui.p5.step04_trips.no_frequencies_rows_this_direction"))

                try:
                    stop_time_rows = client.list_generated_stop_times(
                        export_run_id=run_id,
                        route_id=rid,
                        direction_id=did,
                        limit=int(preview_limit),
                    )
                except Exception as e:
                    st.error(t("ui.p5.step04_trips.failed_load_generated_stop_times_preview", e=(e)))
                    stop_time_rows = []
                if stop_time_rows:
                    trip_ids = sorted({str(x.get("trip_id") or "") for x in stop_time_rows if str(x.get("trip_id") or "").strip()})
                    st.caption(t("ui.p5.step04_trips.generated_stop_times_rows_n_stop", n_stop_time_rows=(len(stop_time_rows)), n_trip_ids=(len(trip_ids))))
                    st.dataframe(stop_time_rows, use_container_width=True, hide_index=True, height=220)
                else:
                    st.caption(t("ui.p5.step04_trips.no_stop_times_rows_this_direction"))

    with st.expander(t("ui.p5.step04_trips.deletion_log_phase_5_revisions"), expanded=False):
        try:
            logs = client.list_revision_events(export_run_id=run_id, limit=200)
        except Exception as e:
            st.error(t("ui.p5.step04_trips.failed_load_deletion_log_e", e=(e)))
            logs = []
        if logs:
            st.dataframe(logs, use_container_width=True, hide_index=True, height=220)
        else:
            st.info(t("ui.p5.step04_trips.no_deletion_log_entries_yet"))

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step04.preview.tables",
        route_id=route_id,
        title=t("p5.step04.table_preview_title"),
        default_tables=["gtfs_trips", "gtfs_frequencies", "gtfs_stop_times", "gtfs_calendar"],
        note=t("p5.step04.table_preview_note"),
    )
