from __future__ import annotations

from typing import Any

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview


def _suggested_blocks(runtime_offpeak_secs: int, offpeak_headway_secs: int, peak_headway_secs: int) -> int:
    rt = max(300, int(runtime_offpeak_secs or 0))
    off_h = max(60, int(offpeak_headway_secs or 0) or 600)
    peak_h = int(peak_headway_secs or 0)
    if peak_h <= 0:
        peak_h = off_h
    min_h = max(60, min(off_h, peak_h))
    return max(1, int(((rt * 2.0) + float(min_h) - 1.0) // float(min_h)))


def render_step04_trips_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown("### Step 04 Build trips + stop_times (combined)")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip()
    service_route_id = (st.session_state.get("phase5.service_route_id") or "").strip()
    selected_direction = int(st.session_state.get("phase5.direction_id") or 0)
    run_mode = str(st.session_state.get("phase5.run_mode") or "One by one")
    bulk_mode = run_mode == "Bulk (multi-route)"
    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return

    st.caption(
        f"Context: service_route_id=`{service_route_id or '-'}` | selected direction=`{selected_direction}` | route_id=`{route_id or '-'}`"
    )
    st.caption("This step runs Step 04 (trips+frequencies) and Step 05 (stop_times) together for the selected route+direction.")
    if bulk_mode:
        st.info("Bulk mode is enabled. Use the bulk section below. Single-route tools are hidden.")

    st.divider()
    st.markdown("#### Bulk run (multiple routes + directions)")
    st.caption("Runs Step 04+05 across every eligible route+direction in one go.")
    bulk_verified = st.checkbox("Bulk: verified routes only", value=True, key="p5.step04.bulk.verified")
    bulk_limit_current = st.checkbox(
        "Limit bulk to current service_route_id",
        value=False,
        key="p5.step04.bulk.limit_current",
    )
    bulk_require_binding = st.checkbox(
        "Require Runtime Lab estimate (skip missing)",
        value=True,
        key="p5.step04.bulk.require_binding",
    )
    skip_if_exists = st.checkbox(
        "Skip if trips already exist in this export",
        value=True,
        key="p5.step04.bulk.skip_existing",
    )
    auto_apply_blocks = st.checkbox(
        "Auto-apply suggested n_blocks per route+direction",
        value=True,
        key="p5.step04.bulk.auto_blocks",
    )
    try:
        bulk_rows = client.list_service_direction_inputs(verified_only=bool(bulk_verified), limit=3000)
    except Exception as e:
        st.error(f"Failed to load bulk inputs: {e}")
        bulk_rows = []
    if bulk_limit_current and service_route_id:
        bulk_rows = [r for r in bulk_rows if str(r.get("service_route_id") or "") == service_route_id]
    st.caption(f"Eligible pairs: {len(bulk_rows)}")
    if st.button("Run Step 04 + Step 05 (bulk)", type="primary", use_container_width=True, key="p5.step04.bulk.run"):
        if not bulk_rows:
            st.warning("No eligible route+direction pairs.")
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
                st.warning("Some routes are missing Runtime Lab estimates; they will be skipped.")
            skip_set = {(m.get("route_id"), int(m.get("direction_id") or 0)) for m in missing}
            results = []
            with st.spinner("Running Step 04 + Step 05 in bulk..."):
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
            st.success("Bulk Step 04 + Step 05 finished.")
            st.dataframe(results, use_container_width=True, hide_index=True, height=260)

    if not bulk_mode:
        st.divider()
        st.markdown("#### Cleanup block (route_id + direction_id)")
        st.caption("Deletes Phase 5 generated context for this route+direction without deleting the route row.")
        cl1, cl2 = st.columns(2)
        with cl1:
            confirm_cleanup = st.checkbox("Confirm cleanup for selected route+direction", value=False, key="p5.step04.cleanup.confirm")
        with cl2:
            drop_logs = st.checkbox("Also delete matching revision logs", value=False, key="p5.step04.cleanup.logs")
        if st.button("Delete Phase 5 context (route+direction)", use_container_width=True, key="p5.step04.cleanup.run"):
            if not confirm_cleanup:
                st.warning("Confirm cleanup first.")
            else:
                try:
                    out_cleanup = client.delete_phase5_route_direction_context(
                        export_run_id=run_id,
                        route_id=route_id,
                        direction_id=selected_direction,
                        delete_revision_logs=bool(drop_logs),
                    )
                except Exception as e:
                    st.error(f"Cleanup failed: {e}")
                else:
                    st.success("Cleanup completed.")
                    st.json(out_cleanup)

        st.divider()
        r1, r2 = st.columns(2)
        with r1:
            if st.button("Run Step 04 + Step 05", type="primary", use_container_width=True, key="p5.step04plus.run"):
                with st.spinner("Building trips+frequencies and stop_times..."):
                    try:
                        out4 = client.run_step_04_trips(run_id, route_id=route_id, direction_id=selected_direction)
                        out5 = client.run_step_05_stop_times(run_id, route_id=route_id, direction_id=selected_direction)
                    except Exception as e:
                        st.error(str(e))
                        return
                st.success("Step 04 + Step 05 completed.")
                st.json({"step_04": out4, "step_05": out5})
        with r2:
            with st.expander("Advanced run controls", expanded=False):
                if st.button("Run Step 04 only", use_container_width=True, key="p5.step04.only"):
                    try:
                        out4_only = client.run_step_04_trips(run_id, route_id=route_id, direction_id=selected_direction)
                    except Exception as e:
                        st.error(str(e))
                    else:
                        st.success("Step 04 completed.")
                        st.json(out4_only)
                if st.button("Run Step 05 only", use_container_width=True, key="p5.step05.only"):
                    try:
                        out5_only = client.run_step_05_stop_times(run_id, route_id=route_id, direction_id=selected_direction)
                    except Exception as e:
                        st.error(str(e))
                    else:
                        st.success("Step 05 completed.")
                        st.json(out5_only)

    st.markdown("#### Output preview (by direction)")
    preview_limit = st.number_input("preview rows limit", min_value=50, max_value=5000, value=600, step=50, key="p5.step04.preview.limit")
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
        st.info("Select service_route_id + direction in Route inputs to visualize output.")
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
                    st.error(f"Failed to load profiles/windows preview: {e}")
                    dir_profiles = []
                if dir_profiles:
                    st.caption("Schedule profiles used")
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
                        st.caption("Frequency windows used")
                        st.dataframe(all_windows, use_container_width=True, hide_index=True, height=150)

                try:
                    trip_rows = client.list_generated_trips_with_departures(
                        export_run_id=run_id,
                        route_id=rid,
                        direction_id=did,
                        limit=int(preview_limit),
                    )
                except Exception as e:
                    st.error(f"Failed to load generated trips preview: {e}")
                    trip_rows = []
                if trip_rows:
                    block_ids = sorted({str(x.get("block_id") or "") for x in trip_rows if str(x.get("block_id") or "").strip()})
                    dep_vals = [str(x.get("departure_time") or "") for x in trip_rows if str(x.get("departure_time") or "").strip()]
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("Trips", len(trip_rows))
                    c2.metric("Blocks", len(block_ids))
                    c3.metric("First departure", min(dep_vals) if dep_vals else "-")
                    c4.metric("Last departure", max(dep_vals) if dep_vals else "-")
                    st.caption("Generated trips + block + departure")
                    st.dataframe(trip_rows, use_container_width=True, hide_index=True, height=240)
                else:
                    st.info("No trips generated yet for this direction in current export_run_id.")

                try:
                    freq_rows = client.list_generated_frequencies(
                        export_run_id=run_id,
                        route_id=rid,
                        direction_id=did,
                        limit=int(preview_limit),
                    )
                except Exception as e:
                    st.error(f"Failed to load generated frequencies preview: {e}")
                    freq_rows = []
                if freq_rows:
                    st.caption("Generated frequencies rows")
                    st.dataframe(freq_rows, use_container_width=True, hide_index=True, height=180)
                else:
                    st.caption("No frequencies rows for this direction.")

                try:
                    stop_time_rows = client.list_generated_stop_times(
                        export_run_id=run_id,
                        route_id=rid,
                        direction_id=did,
                        limit=int(preview_limit),
                    )
                except Exception as e:
                    st.error(f"Failed to load generated stop_times preview: {e}")
                    stop_time_rows = []
                if stop_time_rows:
                    trip_ids = sorted({str(x.get("trip_id") or "") for x in stop_time_rows if str(x.get("trip_id") or "").strip()})
                    st.caption(f"Generated stop_times rows ({len(stop_time_rows)} rows across {len(trip_ids)} trips)")
                    st.dataframe(stop_time_rows, use_container_width=True, hide_index=True, height=220)
                else:
                    st.caption("No stop_times rows for this direction.")

    with st.expander("Deletion log (Phase 5 revisions)", expanded=False):
        try:
            logs = client.list_revision_events(export_run_id=run_id, limit=200)
        except Exception as e:
            st.error(f"Failed to load deletion log: {e}")
            logs = []
        if logs:
            st.dataframe(logs, use_container_width=True, hide_index=True, height=220)
        else:
            st.info("No deletion log entries yet.")

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step04.preview.tables",
        route_id=route_id,
        title="Step 04/05 GTFS table preview (all columns)",
        default_tables=["gtfs_trips", "gtfs_frequencies", "gtfs_stop_times", "gtfs_calendar"],
        note="Use this to inspect the raw GTFS rows/columns generated for the selected route. The custom preview above remains direction-aware.",
    )
