from __future__ import annotations

from typing import Any

import streamlit as st


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
    st.markdown("### Step 01 Load GTFS stops (from Phase 2 final prod canonical STOPs)")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    gtfs_id = (st.session_state.get("phase5.gtfs_id") or "").strip()

    st.caption(f"Context: gtfs_id=`{gtfs_id or '-'}`")
    st.caption("Source is Phase 2 final prod canonical stop table (`geo_prod.v_place_points` + `node_prod.nodes`).")
    st.caption("This step is independent from route/service_route selection. It loads final canonical `STOP` nodes with canonical names.")
    st.caption("This step previews what will be inserted into GTFS stops, then upserts those rows into the selected gtfs_id context.")

    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return

    prev_key = "p5.step01stops.preview_rows"
    sets_key = "p5.step01stops.place_sets"
    scope_mode = st.radio(
        "Phase 2 stop source scope",
        options=["Full Phase 2 final prod", "Specific Phase 2 place_set"],
        horizontal=True,
        key="p5.step01stops.scope_mode",
    )
    selected_place_set_id = ""
    if scope_mode == "Specific Phase 2 place_set":
        if st.button("Load Phase 2 place sets", key="p5.step01stops.load_sets"):
            try:
                st.session_state[sets_key] = client.list_phase2_place_sets_for_gtfs_stops(limit=500)
            except Exception as e:
                st.error(f"Failed to load Phase 2 place sets: {e}")
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
            chosen = st.selectbox("Select Phase 2 place_set_id", labels, key="p5.step01stops.place_set_label")
            selected_place_set_id = by_label.get(chosen, "")
            st.caption(f"Selected place_set_id: `{selected_place_set_id}`")
        else:
            st.info("Load Phase 2 place sets to choose a specific set source.")

    lim = st.number_input("Preview limit", min_value=50, max_value=5000, value=1000, step=50, key="p5.step01stops.limit")
    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("Preview stops to load (Phase 2 final)", use_container_width=True, key="p5.step01stops.preview"):
            try:
                st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(
                    limit=int(lim),
                    place_set_id=(selected_place_set_id or None),
                )
            except TypeError as e:
                msg = str(e)
                if "place_set_id" in msg:
                    if selected_place_set_id:
                        st.error("This running app version does not support place_set_id source scope yet. Restart Streamlit and try again.")
                        st.session_state[prev_key] = []
                    else:
                        try:
                            st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(limit=int(lim))
                            st.warning("Compatibility fallback active (old client signature). Restart Streamlit to use set-scoped/full direct preview.")
                        except Exception as e2:
                            st.error(f"Preview failed: {e2}")
                            st.session_state[prev_key] = []
                elif "route_ids" in msg:
                    try:
                        all_route_ids = _all_route_ids_from_inputs(client)
                        st.session_state[prev_key] = client.preview_phase2_final_gtfs_stops(
                            route_ids=all_route_ids,
                            limit=int(lim),
                        )
                        st.warning("Compatibility fallback active (old client signature). Restart Streamlit to use direct all-stops preview.")
                    except Exception as e2:
                        st.error(f"Preview failed: {e2}")
                        st.session_state[prev_key] = []
                else:
                    st.error(f"Preview failed: {e}")
                    st.session_state[prev_key] = []
            except Exception as e:
                st.error(f"Preview failed: {e}")
                st.session_state[prev_key] = []
    with c2:
        if st.button("Run Step 01 (upsert GTFS stops)", type="primary", use_container_width=True, key="p5.step01stops.run"):
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
                        st.error("This running app version does not support place_set_id source scope yet. Restart Streamlit and try again.")
                        out = None
                    else:
                        try:
                            out = client.run_step_015_load_stops_from_phase2_final(
                                export_run_id=run_id,
                                route_ids=None,
                            )
                            st.warning("Compatibility fallback active (old client signature). Restart Streamlit to use set-scoped/full direct load.")
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
                        st.warning("Compatibility fallback active (old client signature). Restart Streamlit to use direct all-stops load.")
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
                st.success("Step 01 completed.")
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
        st.markdown("#### Preview: stops to load into selected gtfs_id")
        st.caption(f"Rows: {len(preview_rows)} | columns shown exclude internal export ids")
        show_cols = ["stop_id", "stop_name", "stop_lat", "stop_lon", "place_id", "place_type", "node_type"]
        slim_rows = [{k: r.get(k) for k in show_cols if k in r} for r in preview_rows]
        st.dataframe(slim_rows, use_container_width=True, hide_index=True, height=260)
    else:
        st.info("Click preview to visualize canonical Phase 2 stops before inserting.")

    st.divider()
    st.markdown("#### Current GTFS stops in selected gtfs_id")
    try:
        current_rows = client.list_gtfs_rows("gtfs_stops", run_id, limit=int(lim)) or []
    except Exception as e:
        st.error(f"Failed to load current GTFS stops: {e}")
        return

    if current_rows:
        show_cols = ["stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station"]
        slim_current = [{k: r.get(k) for k in show_cols if k in r} for r in current_rows]
        st.dataframe(slim_current, use_container_width=True, hide_index=True, height=240)
    else:
        st.info("No GTFS stops inserted yet for this gtfs_id.")
