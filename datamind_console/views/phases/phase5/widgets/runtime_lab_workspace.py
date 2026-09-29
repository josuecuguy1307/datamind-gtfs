from __future__ import annotations

import inspect
from typing import Any, Dict, List

import pydeck as pdk
import streamlit as st

from .route_inputs_table import render_route_inputs_table


@st.cache_data(ttl=120, show_spinner=False)
def _cached_catalog_items(*, _client: Any, active_only: bool, limit: int) -> list[dict]:
    return _client.list_runtime_catalog_items(active_only=bool(active_only), limit=int(limit))


@st.cache_data(ttl=90, show_spinner=False)
def _cached_runtime_estimate(
    *,
    _client: Any,
    route_id: str,
    direction_id: int,
    area_profile_code: str,
    speed_profile_code: str,
    dwell_profile_code: str,
    intersection_profile_code: str,
    peak_profile_code: str,
    confidence_profile_code: str,
    use_external_elevation: bool,
    use_external_signals: bool,
    persist_snapshot: bool,
    mode_strategy: str,
    section_min_score: float,
    allow_haversine_fallback: bool,
) -> dict:
    fn = getattr(_client, "estimate_runtime_from_catalog")
    kwargs = {
        "route_id": str(route_id),
        "direction_id": int(direction_id),
        "area_profile_code": str(area_profile_code),
        "speed_profile_code": (str(speed_profile_code) or None),
        "dwell_profile_code": (str(dwell_profile_code) or None),
        "intersection_profile_code": (str(intersection_profile_code) or None),
        "peak_profile_code": (str(peak_profile_code) or None),
        "confidence_profile_code": str(confidence_profile_code),
        "use_external_elevation": bool(use_external_elevation),
        "use_external_signals": bool(use_external_signals),
        "persist_snapshot": bool(persist_snapshot),
        "mode_strategy": str(mode_strategy),
        "section_min_score": float(section_min_score),
        "allow_haversine_fallback": bool(allow_haversine_fallback),
    }
    sig = inspect.signature(fn)
    safe = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(**safe)


@st.cache_data(ttl=90, show_spinner=False)
def _cached_area_suggestion(
    *,
    _client: Any,
    route_id: str,
    direction_id: int,
    use_external_elevation: bool,
    use_external_signals: bool,
    allow_haversine_fallback: bool,
) -> dict:
    fn = getattr(_client, "suggest_area_profile_for_route", None)
    if not callable(fn):
        raise RuntimeError("Client does not expose suggest_area_profile_for_route.")
    kwargs = {
        "route_id": str(route_id),
        "direction_id": int(direction_id),
        "use_external_elevation": bool(use_external_elevation),
        "use_external_signals": bool(use_external_signals),
        "allow_haversine_fallback": bool(allow_haversine_fallback),
    }
    sig = inspect.signature(fn)
    safe = {k: v for k, v in kwargs.items() if k in sig.parameters}
    return fn(**safe)


@st.cache_data(ttl=60, show_spinner=False)
def _cached_route_map_payload(*, _client: Any, route_id: str) -> dict:
    return _client.get_route_map_payload(route_id)


def _geojson_path(geojson: Dict[str, Any]) -> List[List[float]]:
    gtype = str(geojson.get("type") or "")
    coords = geojson.get("coordinates") or []
    out: List[List[float]] = []
    if gtype == "LineString" and isinstance(coords, list):
        for pt in coords:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                out.append([float(pt[1]), float(pt[0])])  # lat, lon
        return out
    if gtype == "MultiLineString" and isinstance(coords, list):
        for seg in coords:
            if isinstance(seg, list):
                for pt in seg:
                    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                        out.append([float(pt[1]), float(pt[0])])
        return out
    return out


def _catalog_options(items: List[Dict[str, Any]], key: str) -> List[str]:
    return sorted({str(x.get("item_code") or "") for x in items if str(x.get("catalog_key") or "") == key and str(x.get("item_code") or "")})


def render_runtime_lab_workspace(*, ctx: Any, client: Any, key: str = "p5_runtime_lab", **_) -> None:
    ss = st.session_state
    pending_set = ss.pop(f"{key}.est.pending_set", None)
    if isinstance(pending_set, dict):
        for k, v in pending_set.items():
            ss[k] = v
    st.markdown("### Runtime Lab")
    st.caption("Catalog-driven runtime approximation from shape points + ordered stops + direction context.")

    picked = render_route_inputs_table(client=client, key=f"{key}.inputs")
    if isinstance(picked, str) and picked.strip():
        ss["phase5.route_id"] = picked.strip()

    route_id = str(ss.get("phase5.route_id") or "").strip()
    service_route_id = str(ss.get("phase5.service_route_id") or "").strip()
    direction_id = int(ss.get("phase5.direction_id") or 0)
    if not route_id:
        st.info("Select `service_route_id + direction` first.")
        return
    st.caption(f"service_route_id: `{service_route_id or '-'}` | direction_id: `{direction_id}` | route_id: `{route_id}`")
    try:
        bound = client.get_runtime_estimate_binding(route_id=route_id, direction_id=int(direction_id))
    except Exception:
        bound = {}
    if bound:
        st.caption(
            "Bound leg-runtime estimate: "
            f"`{str(bound.get('estimate_id') or '')[:8]}` "
            f"(updated `{bound.get('updated_at')}`)"
        )
    else:
        st.caption("Bound leg-runtime estimate: `none` (Step 05 is blocked until you apply+bind one).")

    ctl1, ctl2, ctl3 = st.columns([1, 1, 1.4])
    with ctl1:
        replace_existing = st.checkbox("Replace existing defaults", value=False, key=f"{key}.seed.replace")
    with ctl2:
        if st.button("Seed Quito v1 catalogs", use_container_width=True, key=f"{key}.seed"):
            try:
                out = client.seed_runtime_catalog_defaults(replace_existing=bool(replace_existing))
            except Exception as e:
                st.error(f"Seed failed: {e}")
            else:
                st.cache_data.clear()
                st.success(f"Seed done: inserted={out.get('inserted')} updated={out.get('updated')}")
    with ctl3:
        if st.button("Refresh Runtime Lab cache", use_container_width=True, key=f"{key}.refresh"):
            st.cache_data.clear()
            st.rerun()

    items = _cached_catalog_items(_client=client, active_only=False, limit=8000)
    if not items:
        st.warning("No Runtime Lab catalogs yet. Click `Seed Quito v1 catalogs`.")
        return

    st.caption("Catalog editing moved to `Phase 5 screen -> Catalog Tuning`.")

    # Estimator controls
    st.markdown("#### Per-direction estimation")
    area_opts = ["auto"] + _catalog_options(items, "area_profile_catalog")
    speed_opts = [""] + _catalog_options(items, "speed_catalog")
    dwell_opts = [""] + _catalog_options(items, "dwell_catalog")
    inter_opts = [""] + _catalog_options(items, "intersection_delay_catalog")
    peak_opts = [""] + _catalog_options(items, "peak_penalty_catalog")
    conf_opts = _catalog_options(items, "confidence_rules_catalog") or ["default_v1"]

    c1, c2, c3 = st.columns(3)
    with c1:
        area_code = st.selectbox("area_profile_code", area_opts, index=0, key=f"{key}.est.area")
        speed_code = st.selectbox("speed_profile_code (optional override)", speed_opts, index=0, key=f"{key}.est.speed")
    with c2:
        dwell_code = st.selectbox("dwell_profile_code (optional override)", dwell_opts, index=0, key=f"{key}.est.dwell")
        inter_code = st.selectbox("intersection_profile_code (optional override)", inter_opts, index=0, key=f"{key}.est.inter")
    with c3:
        peak_code = st.selectbox("peak_profile_code (optional override)", peak_opts, index=0, key=f"{key}.est.peak")
        conf_code = st.selectbox("confidence_profile_code", conf_opts, index=0, key=f"{key}.est.conf")

    auto_fill_on_area = st.checkbox(
        "Auto-fill overrides when area_profile changes",
        value=True,
        key=f"{key}.est.autofill_on_area",
    )
    area_track_key = f"{key}.est.last_area"
    last_area = str(ss.get(area_track_key) or "")
    current_area = str(area_code or "")
    if auto_fill_on_area and current_area and current_area != "auto" and current_area != last_area:
        area_rows = [dict(x) for x in items if str(x.get("catalog_key") or "") == "area_profile_catalog"]
        area_row = next((r for r in area_rows if str(r.get("item_code") or "") == current_area), {})
        payload = dict(area_row.get("payload") or {})
        if payload:
            ss[f"{key}.est.pending_set"] = {
                f"{key}.est.speed": str(payload.get("default_speed_profile") or ""),
                f"{key}.est.dwell": str(payload.get("default_dwell_profile") or ""),
                f"{key}.est.inter": str(payload.get("default_intersection_profile") or ""),
                f"{key}.est.peak": str(payload.get("default_peak_profile") or ""),
            }
            ss[area_track_key] = current_area
            st.rerun()
    ss[area_track_key] = current_area

    e1, e2, e3, e4 = st.columns(4)
    with e1:
        use_external_elevation = st.checkbox("Use external elevation API", value=True, key=f"{key}.est.ext_elev")
    with e2:
        use_external_signals = st.checkbox("Use Overpass traffic signals", value=False, key=f"{key}.est.ext_signals")
    with e3:
        persist_snapshot = st.checkbox("Persist estimate snapshot in DB", value=True, key=f"{key}.est.persist")
    with e4:
        allow_haversine_fallback = st.checkbox(
            "Allow stop-to-stop fallback",
            value=False,
            key=f"{key}.est.allow_fallback",
            help="Use haversine distance for legs that cannot be projected to shape points.",
        )

    s1, s2 = st.columns(2)
    with s1:
        mode_strategy = st.selectbox(
            "Mode strategy",
            options=["single_mode", "mixed_sections"],
            index=0,
            key=f"{key}.est.mode",
            help="single_mode: one profile for all route. mixed_sections: score and apply profile by contiguous sections.",
        )
    with s2:
        section_min_score = st.slider(
            "Section score threshold",
            min_value=0.0,
            max_value=1.0,
            value=0.58,
            step=0.01,
            key=f"{key}.est.section_score",
            help="For mixed_sections, low-score legs fall back to selected area profile.",
        )

    af1, af2, af3 = st.columns([1, 1, 2])
    with af1:
        if st.button("Analyze route + suggest profile", use_container_width=True, key=f"{key}.est.analyze"):
            try:
                sug = _cached_area_suggestion(
                    _client=client,
                    route_id=route_id,
                    direction_id=int(direction_id),
                    use_external_elevation=bool(use_external_elevation),
                    use_external_signals=bool(use_external_signals),
                    allow_haversine_fallback=bool(allow_haversine_fallback),
                )
            except Exception as e:
                st.error(f"Suggestion failed: {e}")
            else:
                ss[f"{key}.analysis"] = sug
                st.success("Route analysis ready.")
    with af2:
        if st.button("Autofill defaults from selected area", use_container_width=True, key=f"{key}.est.autofill"):
            area_rows = [dict(x) for x in items if str(x.get("catalog_key") or "") == "area_profile_catalog"]
            area_row = next((r for r in area_rows if str(r.get("item_code") or "") == str(area_code)), {})
            payload = dict(area_row.get("payload") or {})
            if not payload:
                st.warning("Selected area profile has no defaults.")
            else:
                ss[f"{key}.est.pending_set"] = {
                    f"{key}.est.speed": str(payload.get("default_speed_profile") or ""),
                    f"{key}.est.dwell": str(payload.get("default_dwell_profile") or ""),
                    f"{key}.est.inter": str(payload.get("default_intersection_profile") or ""),
                    f"{key}.est.peak": str(payload.get("default_peak_profile") or ""),
                }
                st.success("Autofilled from area profile defaults.")
                st.rerun()
    with af3:
        st.caption("Use `Analyze route + suggest profile` for a recommendation, or manually pick catalog overrides.")

    analysis = ss.get(f"{key}.analysis") or {}
    if analysis:
        rec = str(analysis.get("suggested_area_profile_code") or "")
        if rec:
            st.info(f"Suggested area profile: `{rec}`")
        feats = analysis.get("features") or {}
        st.caption(
            f"stop_spacing_m={float(feats.get('stop_spacing_m') or 0):.2f} | "
            f"signals_per_km={float(feats.get('signals_per_km') or 0):.2f} | "
            f"terrain_density={float(feats.get('terrain_density_score') or 0):.3f} | "
            f"curve_ratio={float(feats.get('curve_ratio') or 0):.3f}"
        )
        sug_rows = [dict(x) for x in (analysis.get("suggestions") or [])]
        if sug_rows:
            st.dataframe(sug_rows, use_container_width=True, hide_index=True, height=180)
            if st.button("Apply suggested area + defaults", use_container_width=True, key=f"{key}.est.apply_suggested", disabled=(not rec)):
                top = sug_rows[0] if sug_rows else {}
                defaults = dict(top.get("defaults") or {})
                ss[f"{key}.est.pending_set"] = {
                    f"{key}.est.area": rec,
                    f"{key}.est.speed": str(defaults.get("speed_profile_code") or ""),
                    f"{key}.est.dwell": str(defaults.get("dwell_profile_code") or ""),
                    f"{key}.est.inter": str(defaults.get("intersection_profile_code") or ""),
                    f"{key}.est.peak": str(defaults.get("peak_profile_code") or ""),
                }
                st.success("Applied suggested profile into form.")
                st.rerun()

    if st.button("Compute runtime estimate", type="primary", use_container_width=True, key=f"{key}.est.run"):
        try:
            est = _cached_runtime_estimate(
                _client=client,
                route_id=route_id,
                direction_id=int(direction_id),
                area_profile_code=str(area_code),
                speed_profile_code=str(speed_code),
                dwell_profile_code=str(dwell_code),
                intersection_profile_code=str(inter_code),
                peak_profile_code=str(peak_code),
                confidence_profile_code=str(conf_code),
                use_external_elevation=bool(use_external_elevation),
                use_external_signals=bool(use_external_signals),
                persist_snapshot=bool(persist_snapshot),
                mode_strategy=str(mode_strategy),
                section_min_score=float(section_min_score),
                allow_haversine_fallback=bool(allow_haversine_fallback),
            )
        except Exception as e:
            st.error(f"Estimate failed: {e}")
            return
        ss[f"{key}.estimate"] = est
        st.success("Estimate computed.")

    est = ss.get(f"{key}.estimate") or {}
    if est:
        m = est.get("metrics") or {}
        mi = est.get("model_inputs") or {}
        leg_rows = [dict(x) for x in (est.get("leg_preview") or [])]
        stop_dwell_rows = [dict(x) for x in (est.get("stop_dwell_preview") or [])]
        section_rows = [dict(x) for x in (est.get("sections") or [])]

        left_res, right_res = st.columns([1.25, 1.0], gap="large")
        with right_res:
            st.markdown("##### Whole-route runtime")
            rm1, rm2 = st.columns(2)
            rm1.metric("Route len (m)", int(m.get("route_len_m") or 0))
            rm2.metric("Confidence", f"{float(m.get('confidence') or 0):.3f}")
            rm3, rm4 = st.columns(2)
            rm3.metric("Offpeak runtime", f"{int(m.get('runtime_offpeak_secs') or 0)} s")
            rm4.metric("Peak runtime", f"{int(m.get('runtime_peak_secs') or 0)} s")
            rm5, rm6 = st.columns(2)
            rm5.metric("offpeak_min_per_leg", f"{float(m.get('offpeak_min_per_leg') or 0):.3f}")
            rm6.metric("peak_min_per_leg", f"{float(m.get('peak_min_per_leg') or 0):.3f}")
            st.caption(
                f"offpeak_headway_secs={int(m.get('offpeak_headway_secs') or 0)} | "
                f"peak_headway_secs={int(m.get('peak_headway_secs') or 0)} | "
                f"terrain_density={float(m.get('terrain_density_score') or 0):.3f}"
            )
            st.caption(
                f"elevation_source={mi.get('elevation_source')} ({int(mi.get('elevation_samples') or 0)} samples) | "
                f"signal_source={mi.get('signal_source')} ({int(mi.get('signal_points') or 0)} points) | "
                f"signals_per_km_used={float(mi.get('signals_per_km_used') or 0):.2f}"
            )
            auto_rebuild = st.checkbox(
                "Auto rebuild Step 04 + Step 05 on apply (current route+direction only)",
                value=True,
                key=f"{key}.est.auto_rebuild",
            )
            r1, r2 = st.columns(2)
            with r1:
                if st.button("Apply estimate to Phase 5 editor", use_container_width=True, key=f"{key}.est.apply"):
                    st.session_state["p5_profile.offpeak_leg_min"] = float(m.get("offpeak_min_per_leg") or 1.5)
                    st.session_state["p5_profile.peak_leg_min"] = float(m.get("peak_min_per_leg") or 2.5)
                    st.session_state["p5_profile.offpeak_headway"] = int(m.get("offpeak_headway_secs") or 600)
                    st.session_state["p5_profile.peak_headway"] = int(m.get("peak_headway_secs") or 360)
                    st.session_state["p5_profile.runtime_offpeak_secs"] = int(m.get("runtime_offpeak_secs") or 0)
                    st.session_state["p5_profile.runtime_peak_secs"] = int(m.get("runtime_peak_secs") or 0)
                    off_h = int(m.get("offpeak_headway_secs") or 600)
                    peak_h = int(m.get("peak_headway_secs") or max(120, off_h))
                    min_h = max(60, min(off_h, peak_h))
                    rt_off = max(300, int(m.get("runtime_offpeak_secs") or 3600))
                    suggested_blocks = max(1, int(((rt_off * 2.0) + float(min_h) - 1.0) // float(min_h)))
                    st.session_state["p5_profile.suggested_blocks"] = int(suggested_blocks)
                    st.session_state["p5_profile.n_blocks"] = int(suggested_blocks)
                    est_id = str(est.get("estimate_id") or "").strip()
                    if est_id:
                        try:
                            client.bind_runtime_estimate_to_route_direction(
                                route_id=route_id,
                                direction_id=int(direction_id),
                                estimate_id=est_id,
                            )
                        except Exception as e:
                            st.warning(f"Editor values applied, but estimate binding failed: {e}")
                        else:
                            if auto_rebuild:
                                run_id = str(st.session_state.get("phase5.export_run_id") or "").strip()
                                if run_id:
                                    try:
                                        out4 = client.run_step_04_trips(
                                            run_id,
                                            route_id=str(route_id),
                                            direction_id=int(direction_id),
                                        )
                                        out5 = client.run_step_05_stop_times(
                                            run_id,
                                            route_id=str(route_id),
                                            direction_id=int(direction_id),
                                        )
                                    except Exception as e:
                                        st.warning(
                                            "Applied to editor + bound estimate, but auto rebuild failed: "
                                            f"{e}"
                                        )
                                    else:
                                        st.success("Applied + bound + rebuilt Step 04/05 for current route+direction.")
                                        st.caption(
                                            f"Step 04: trips={int(out4.get('trips') or 0)} | "
                                            f"Step 05: stop_times={int(out5.get('stop_times') or 0)}"
                                        )
                                else:
                                    st.success("Applied + bound estimate. Select export_run_id to auto rebuild stop_times.")
                            else:
                                st.success("Applied to editor + bound leg-by-leg estimate for Step 05.")
                    else:
                        st.warning(
                            "Applied to editor fields only. "
                            "No persisted estimate_id found; enable `Persist estimate snapshot in DB` and recompute."
                        )
            with r2:
                if st.button("Show full estimate JSON", use_container_width=True, key=f"{key}.est.show_json"):
                    st.json(est)

            st.markdown("##### Suggestion by section")
            if section_rows:
                st.dataframe(section_rows, use_container_width=True, hide_index=True, height=220)
            else:
                st.info("No per-section assignment available yet. Use `mixed_sections` mode and recompute.")

        with left_res:
            if stop_dwell_rows:
                st.markdown("##### Dwell seconds by stop")
                dwell_chart = [
                    {
                        "stop_seq": int(r.get("stop_seq") or i + 1),
                        "dwell_offpeak_secs": float(r.get("offpeak_secs") or r.get("dwell_offpeak_secs") or 0.0),
                        "dwell_peak_secs": float(r.get("peak_secs") or r.get("dwell_peak_secs") or 0.0),
                    }
                    for i, r in enumerate(stop_dwell_rows)
                ]
                st.line_chart(
                    dwell_chart,
                    x="stop_seq",
                    y=["dwell_offpeak_secs", "dwell_peak_secs"],
                    use_container_width=True,
                )
            if leg_rows:
                st.markdown("##### Leg-by-leg preview (shape-aware)")
                chart_data = [
                    {
                        "leg_idx": int(r.get("leg_idx") or i + 1),
                        "offpeak_secs": float(r.get("offpeak_secs") or 0.0),
                        "peak_secs": float(r.get("peak_secs") or 0.0),
                        "grade_pct": float(r.get("grade_pct") or 0.0),
                    }
                    for i, r in enumerate(leg_rows)
                ]
                st.line_chart(chart_data, x="leg_idx", y=["offpeak_secs", "peak_secs", "grade_pct"], use_container_width=True)
                elev_points = []
                for r in leg_rows:
                    idx = int(r.get("leg_idx") or 0)
                    if r.get("elev_from_m") is not None:
                        elev_points.append({"seq": idx, "elev_m": float(r.get("elev_from_m"))})
                    if r.get("elev_to_m") is not None:
                        elev_points.append({"seq": idx + 1, "elev_m": float(r.get("elev_to_m"))})
                if elev_points:
                    st.line_chart(elev_points, x="seq", y=["elev_m"], use_container_width=True)
                st.dataframe(leg_rows, use_container_width=True, hide_index=True, height=280)
            fallback_legs = [str(x) for x in (est.get("fallback_legs") or []) if str(x).strip()]
            if fallback_legs:
                st.warning(
                    "Fallback used for some legs (stop-to-stop haversine). "
                    f"Count={len(fallback_legs)} | Examples: {', '.join(fallback_legs[:6])}"
                )

    # Map
    st.markdown("#### Route map + stops")
    try:
        mp = _cached_route_map_payload(_client=client, route_id=route_id)
    except Exception as e:
        st.warning(f"Map payload unavailable: {e}")
        return

    stops = [dict(x) for x in (mp.get("stops") or [])]
    path = _geojson_path(mp.get("route_geojson") or {})
    if not stops and not path:
        st.info("No geometry/stops available for this route.")
        return

    center_lat = float(stops[0].get("lat")) if stops else float(path[0][0])
    center_lon = float(stops[0].get("lon")) if stops else float(path[0][1])
    layers: List[pdk.Layer] = []
    ui_style = str(st.session_state.get("ui.style") or "Light")
    dark_visual = ui_style == "Graphite"
    map_style = (
        "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"
        if dark_visual
        else "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
    )
    est = ss.get(f"{key}.estimate") or {}
    leg_rows = [dict(x) for x in (est.get("leg_preview") or [])]
    if leg_rows:
        segs = []
        for r in leg_rows:
            flon = r.get("from_lon")
            flat = r.get("from_lat")
            tlon = r.get("to_lon")
            tlat = r.get("to_lat")
            if None in (flon, flat, tlon, tlat):
                continue
            g = float(r.get("grade_pct") or 0.0)
            if g >= 6.0:
                color = [255, 95, 55, 240] if dark_visual else [213, 63, 34, 230]
            elif g >= 2.0:
                color = [255, 176, 70, 240] if dark_visual else [242, 146, 47, 230]
            elif g <= -6.0:
                color = [70, 178, 255, 240] if dark_visual else [35, 122, 194, 230]
            elif g <= -2.0:
                color = [102, 210, 255, 240] if dark_visual else [63, 167, 214, 230]
            else:
                color = [202, 208, 255, 220] if dark_visual else [95, 95, 95, 180]
            segs.append(
                {
                    "path": [[float(flon), float(flat)], [float(tlon), float(tlat)]],
                    "color": color,
                    "grade_pct": g,
                    "signal_count": int(r.get("signal_count") or 0),
                    "leg_idx": int(r.get("leg_idx") or 0),
                }
            )
        if segs:
            layers.append(
                pdk.Layer(
                    "PathLayer",
                    data=segs,
                    get_path="path",
                    get_width=6,
                    get_color="color",
                    width_min_pixels=3,
                    pickable=True,
                )
            )
    elif path:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=[{"path": [[p[1], p[0]] for p in path]}],
                get_path="path",
                get_width=5,
                get_color=[250, 84, 84, 200],
                width_min_pixels=3,
                pickable=False,
            )
        )
    if stops:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=stops,
                get_position="[lon, lat]",
                get_radius=9,
                radius_min_pixels=3,
                radius_max_pixels=7,
                get_fill_color=([102, 206, 255, 235] if dark_visual else [30, 102, 245, 220]),
                get_line_color=([14, 18, 36, 240] if dark_visual else [255, 255, 255, 220]),
                line_width_min_pixels=1,
                stroked=True,
                pickable=True,
            )
        )
    signal_points = [dict(x) for x in (est.get("signal_points") or [])]
    if signal_points:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=signal_points,
                get_position="[lon, lat]",
                get_radius=10,
                radius_min_pixels=4,
                radius_max_pixels=9,
                get_fill_color=([255, 214, 80, 240] if dark_visual else [250, 191, 20, 230]),
                get_line_color=([20, 20, 20, 235] if dark_visual else [60, 60, 60, 220]),
                line_width_min_pixels=1,
                stroked=True,
                pickable=True,
            )
        )
    st.pydeck_chart(
        pdk.Deck(
            map_style=map_style,
            initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=12, pitch=0),
            layers=layers,
            tooltip={"text": "seq:{seq} {name}\nleg:{leg_idx} grade:{grade_pct}\nsignals:{signal_count}"},
        ),
        use_container_width=True,
        key=f"{key}.map",
    )
