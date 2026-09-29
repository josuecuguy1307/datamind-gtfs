# ============================================================
# DEPRECATED — Constructor V2 replaces this module.
# See DEPRECATED_V1_MODULES.md for details.
# This file is preserved for historical reference only.
# ============================================================
"""
Sequences Review Tab

Upload a sequences JSON file, optionally upload an overrides JSON,
view all routes and their stop sequences, and reorder/edit stops
using the same move-up/move-down editor pattern as the manual builder.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

_STATUS_COLORS = {
    "usable": "#2ecc71",
    "weak": "#f39c12",
    "blocked": "#e74c3c",
    "geometry_only": "#3498db",
    "geometry_confirmed": "#9b59b6",
    "geographically_implausible": "#95a5a6",
}


def render_sequences_review_tab(ctx=None, **kwargs) -> None:
    ss = st.session_state

    st.markdown("#### Sequences Review")
    st.caption("Upload sequences JSON, apply overrides, browse routes, and reorder stops.")

    # ── Uploads ─────────────────────────────────────────────────
    u1, u2 = st.columns(2)
    with u1:
        uploaded_seq = st.file_uploader(
            "Sequences JSON",
            type=["json"],
            key="p3.seqrev.upload",
        )
    with u2:
        uploaded_ovr = st.file_uploader(
            "Overrides JSON (optional)",
            type=["json"],
            key="p3.seqrev.ovr_upload",
        )

    # Parse sequences
    if uploaded_seq is not None:
        try:
            raw = json.loads(uploaded_seq.getvalue())
            routes = raw.get("routes") or raw
            if isinstance(routes, dict):
                routes = list(routes.values())
            if not isinstance(routes, list):
                st.error("JSON must contain a 'routes' list.")
                return
            ss["p3.seqrev.data"] = raw
            ss["p3.seqrev.routes"] = routes
            ss["p3.seqrev.filename"] = uploaded_seq.name
            ss.pop("p3.seqrev.ovr_applied", None)
        except Exception as exc:
            st.error(f"Failed to parse sequences JSON: {exc}")
            return

    # Parse overrides
    if uploaded_ovr is not None:
        try:
            ovr_raw = json.loads(uploaded_ovr.getvalue())
            ss["p3.seqrev.overrides"] = ovr_raw
            ss["p3.seqrev.ovr_filename"] = uploaded_ovr.name
            ss.pop("p3.seqrev.ovr_applied", None)
        except Exception as exc:
            st.error(f"Failed to parse overrides JSON: {exc}")

    routes: List[Dict[str, Any]] = ss.get("p3.seqrev.routes") or []
    if not routes:
        st.info("Upload a sequences JSON to get started.")
        return

    filename = ss.get("p3.seqrev.filename", "sequences.json")

    # ── Overrides panel ─────────────────────────────────────────
    overrides_data = ss.get("p3.seqrev.overrides")
    _render_overrides_panel(routes, overrides_data)

    # ── Summary metrics ─────────────────────────────────────────
    n_total = len(routes)
    n_usable = sum(1 for r in routes if r.get("status") == "usable")
    n_geom = sum(1 for r in routes if r.get("status") == "geometry_only")
    total_stops = sum(len(r.get("ordered_stops") or []) for r in routes)
    n_overridden = sum(1 for r in routes if r.get("override_applied"))
    n_with_geometry = sum(1 for r in routes if r.get("geometry_geojson"))
    n_with_issues = sum(1 for r in routes if r.get("geometry_issues"))

    cols = st.columns(5)
    cols[0].metric("Routes", n_total)
    cols[1].metric("Usable", n_usable)
    cols[2].metric("Total stops", total_stops)
    cols[3].metric("With geometry", f"{n_with_geometry}/{n_total}")
    cols[4].metric("Overridden", n_overridden)

    if n_with_issues:
        st.caption(f"{n_with_issues} route(s) with geometry issues")

    # ── Route list table ────────────────────────────────────────
    st.markdown("##### All Routes")
    summary_rows = []
    for i, r in enumerate(routes):
        stops = r.get("ordered_stops") or []
        override_badge = r.get("override_action", "")
        geom_km = r.get("geometry_km", 0)
        geom_status = r.get("geometry_status", "")
        geom_issues_count = len(r.get("geometry_issues", []))
        summary_rows.append({
            "#": i + 1,
            "Route": r.get("route", "?"),
            "Cooperative": r.get("cooperative", ""),
            "Type": r.get("route_type", ""),
            "Status": r.get("status", "unknown"),
            "Stops": len(stops),
            "Corridor km": round(r.get("corridor_km", 0), 1),
            "Geom km": round(geom_km, 1) if geom_km else "",
            "Geom": geom_status[:12] if geom_status else "",
            "Issues": geom_issues_count if geom_issues_count else "",
            "Override": override_badge,
        })

    df = pd.DataFrame(summary_rows)
    st.dataframe(df, use_container_width=True, hide_index=True, height=min(400, 35 * len(summary_rows) + 40))

    # ── Route selector ──────────────────────────────────────────
    route_labels = []
    for r in routes:
        ovr_tag = " [OVR]" if r.get("override_applied") else ""
        route_labels.append(f"{r.get('cooperative', '')}: {r.get('route', '?')}{ovr_tag}")

    selected_idx = int(ss.get("p3.seqrev.route_idx") or 0)
    selected_idx = max(0, min(selected_idx, len(routes) - 1))

    picked = st.selectbox(
        "Select route to edit",
        options=range(len(route_labels)),
        format_func=lambda i: route_labels[i],
        index=selected_idx,
        key="seq_review_pick_route",
    )
    ss["p3.seqrev.route_idx"] = picked
    route = routes[picked]

    st.divider()

    # ── Route header ────────────────────────────────────────────
    st.markdown(f"### {route.get('cooperative', '')}: {route.get('route', '?')}")

    h1, h2, h3 = st.columns(3)
    h1.metric("Status", route.get("status", "unknown"))
    h2.metric("Corridor km", f"{route.get('corridor_km', 0):.1f}")
    h3.metric("Stops", len(route.get("ordered_stops") or []))

    if route.get("override_applied"):
        st.info(
            f"Override active ({route.get('override_action', '?')}): "
            f"{route.get('override_applied', '')}"
        )

    stops: List[Dict[str, Any]] = list(route.get("ordered_stops") or [])

    if not stops:
        st.info("This route has no stops (geometry-only).")
        _render_geometry_map(route)
        return

    # ── Sequence Editor ─────────────────────────────────────────
    st.markdown("#### Sequence Editor")

    item_labels = []
    for s in stops:
        seq = s.get("seq", 0)
        name = s.get("stop_name", "?")
        pf = s.get("path_fraction", 0)
        score = s.get("on_route_score", 0)
        anchor = " [A]" if s.get("is_known_anchor") else ""
        item_labels.append(f"{seq:03d} | {name}{anchor} | pf={pf:.3f} | score={score:.3f}")

    pick_idx = int(ss.get("p3.seqrev.stop_idx") or 0)
    pick_idx = max(0, min(pick_idx, len(stops) - 1))

    picked_label = st.selectbox(
        "Selected stop",
        options=item_labels,
        index=pick_idx,
        key="p3.seqrev.stop_pick",
    )
    current_idx = item_labels.index(picked_label) if picked_label in item_labels else pick_idx
    ss["p3.seqrev.stop_idx"] = current_idx

    # ── Move / Remove buttons ───────────────────────────────────
    a1, a2, a3 = st.columns(3)
    with a1:
        if st.button("Move up", use_container_width=True, disabled=(current_idx <= 0), key="p3.seqrev.up"):
            stops[current_idx - 1], stops[current_idx] = stops[current_idx], stops[current_idx - 1]
            _renumber(stops)
            route["ordered_stops"] = stops
            ss["p3.seqrev.stop_idx"] = max(0, current_idx - 1)
            st.rerun()
    with a2:
        if st.button("Move down", use_container_width=True, disabled=(current_idx >= len(stops) - 1), key="seq_review_move_down"):
            stops[current_idx + 1], stops[current_idx] = stops[current_idx], stops[current_idx + 1]
            _renumber(stops)
            route["ordered_stops"] = stops
            ss["p3.seqrev.stop_idx"] = min(len(stops) - 1, current_idx + 1)
            st.rerun()
    with a3:
        if st.button("Remove selected", use_container_width=True, key="p3.seqrev.remove"):
            del stops[current_idx]
            _renumber(stops)
            route["ordered_stops"] = stops
            ss["p3.seqrev.stop_idx"] = max(0, min(current_idx, len(stops) - 1))
            st.rerun()

    # ── Reverse / Recalculate PF ────────────────────────────────
    e1, e2 = st.columns(2)
    with e1:
        if st.button("Reverse sequence", use_container_width=True, disabled=(len(stops) < 2), key="p3.seqrev.reverse"):
            stops.reverse()
            _renumber(stops)
            _recalculate_path_fractions(stops)
            route["ordered_stops"] = stops
            st.rerun()
    with e2:
        if st.button("Recalculate path fractions", use_container_width=True, disabled=(len(stops) < 2), key="seq_review_recalc_pf"):
            _recalculate_path_fractions(stops)
            route["ordered_stops"] = stops
            st.rerun()

    # ── Stop table ──────────────────────────────────────────────
    st.markdown("#### Stop Sequence")
    stop_rows = []
    for s in stops:
        stop_rows.append({
            "Seq": s.get("seq", 0),
            "Name": s.get("stop_name", "?"),
            "Lat": round(s.get("lat", 0), 6),
            "Lon": round(s.get("lon", 0), 6),
            "PF": round(s.get("path_fraction", 0), 4),
            "Score": round(s.get("on_route_score", 0), 3),
            "Anchor": "Y" if s.get("is_known_anchor") else "",
            "Source": s.get("stop_source", ""),
        })

    st.dataframe(
        pd.DataFrame(stop_rows),
        use_container_width=True,
        hide_index=True,
        height=min(500, 35 * len(stop_rows) + 40),
    )

    # ── Maps (separate) ──────────────────────────────────────────
    _render_stop_map(stops, route.get("route", "Route"))
    _render_geometry_map(route)

    # ── Export ──────────────────────────────────────────────────
    st.divider()
    st.markdown("#### Export")
    raw_data = ss.get("p3.seqrev.data") or {}
    if isinstance(raw_data, dict) and "routes" in raw_data:
        raw_data["routes"] = routes
    export_json = json.dumps(raw_data if isinstance(raw_data, dict) else routes, indent=2, ensure_ascii=False)

    st.download_button(
        "Download modified sequences JSON",
        data=export_json,
        file_name=filename.replace(".json", "_edited.json"),
        mime="application/json",
        use_container_width=True,
        key="seq_review_move_download",
    )


# ── Overrides panel ─────────────────────────────────────────────

def _render_overrides_panel(routes: List[Dict[str, Any]], overrides_data: Any) -> None:
    """Render the overrides summary and apply button."""
    ss = st.session_state

    if not overrides_data:
        # Check for auto-detected override file on disk
        try:
            from datamind_console.phases.phase3_routes.stop_grounding.sequence_overrides import (
                find_latest_override_file,
                load_overrides,
            )
            latest = find_latest_override_file()
            if latest:
                st.caption(f"Latest override file on disk: `{latest}`")
                if st.button("Load from disk", key="p3.seqrev.ovr_load_disk"):
                    overrides_data = load_overrides(latest)
                    ss["p3.seqrev.overrides"] = overrides_data
                    ss["p3.seqrev.ovr_filename"] = latest.split("/")[-1]
                    ss.pop("p3.seqrev.ovr_applied", None)
                    st.rerun()
        except Exception:
            pass
        return

    from datamind_console.phases.phase3_routes.stop_grounding.sequence_overrides import (
        apply_sequence_overrides,
        override_summary,
    )

    ovr_version = overrides_data.get("version", "?")
    ovr_desc = overrides_data.get("description", "")
    ovr_file = ss.get("p3.seqrev.ovr_filename", "?")
    ovr_list = overrides_data.get("overrides", [])

    with st.expander(f"Overrides: {ovr_file} ({len(ovr_list)} routes, version {ovr_version})", expanded=False):
        if ovr_desc:
            st.caption(ovr_desc)

        summary = override_summary(overrides_data)
        if summary:
            st.dataframe(
                pd.DataFrame(summary),
                use_container_width=True,
                hide_index=True,
            )

        already_applied = bool(ss.get("p3.seqrev.ovr_applied"))
        if already_applied:
            st.success(f"Overrides applied (version {ovr_version}).")
        else:
            if st.button("Apply overrides", type="primary", key="seq_review_override_apply"):
                raw_data = ss.get("p3.seqrev.data") or {}
                if isinstance(raw_data, dict) and "routes" in raw_data:
                    raw_data["routes"] = routes

                apply_sequence_overrides(
                    raw_data if isinstance(raw_data, dict) else {"routes": routes},
                    overrides_data,
                )
                # routes list is mutated in-place
                ss["p3.seqrev.ovr_applied"] = True
                st.rerun()

        if st.button("Clear overrides", key="seq_review_override_clear"):
            ss.pop("p3.seqrev.overrides", None)
            ss.pop("p3.seqrev.ovr_applied", None)
            ss.pop("p3.seqrev.ovr_filename", None)
            st.rerun()


# ── Helpers ─────────────────────────────────────────────────────

def _renumber(stops: List[Dict[str, Any]]) -> None:
    for i, s in enumerate(stops, 1):
        s["seq"] = i


def _recalculate_path_fractions(stops: List[Dict[str, Any]]) -> None:
    n = len(stops)
    if n == 0:
        return
    if n == 1:
        stops[0]["path_fraction"] = 0.0
        return
    for i, s in enumerate(stops):
        s["path_fraction"] = round(i / (n - 1), 4)


def _auto_zoom(lats: List[float], lons: List[float]) -> int:
    """Pick zoom level from coordinate spread."""
    if not lats or not lons:
        return 12
    span = max(max(lats) - min(lats), max(lons) - min(lons))
    if span > 0.3:
        return 10
    if span > 0.15:
        return 11
    if span > 0.06:
        return 12
    if span > 0.03:
        return 13
    return 14


def _render_stop_map(stops: List[Dict[str, Any]], route_name: str) -> None:
    """Map 1: ONLY stops — numbered dots + connecting line. No geometry."""
    try:
        import pydeck as pdk
    except ImportError:
        return

    if not stops:
        return

    st.markdown("#### Stop Sequence")

    stop_data = []
    for i, s in enumerate(stops):
        is_anchor = s.get("is_known_anchor", False)
        score = s.get("on_route_score", 0)
        if is_anchor:
            color = [155, 89, 182, 255]
        elif score >= 0.5:
            color = [46, 204, 113, 220]
        elif score >= 0.1:
            color = [241, 196, 15, 220]
        else:
            color = [231, 76, 60, 200]
        stop_data.append({
            "lat": s.get("lat", 0),
            "lon": s.get("lon", 0),
            "name": s.get("stop_name", f"stop_{i}"),
            "seq": s.get("seq", i + 1),
            "score": round(score, 3),
            "color": color,
            "radius": 60 if is_anchor else 35,
        })

    mid = stop_data[len(stop_data) // 2]
    zoom = _auto_zoom([s["lat"] for s in stop_data], [s["lon"] for s in stop_data])
    layers = []

    # Connecting line between stops
    if len(stop_data) >= 2:
        path = [[s["lon"], s["lat"]] for s in stop_data]
        layers.append(pdk.Layer(
            "PathLayer",
            data=[{"path": path, "name": route_name}],
            get_path="path",
            get_width=3,
            get_color=[41, 128, 185, 180],
            width_min_pixels=2,
        ))

    # Stop dots
    layers.append(pdk.Layer(
        "ScatterplotLayer",
        data=stop_data,
        get_position=["lon", "lat"],
        get_radius="radius",
        get_fill_color="color",
        pickable=True,
    ))

    # Seq number labels
    label_data = [
        {"lat": s["lat"], "lon": s["lon"], "label": str(s["seq"])}
        for s in stop_data
    ]
    layers.append(pdk.Layer(
        "TextLayer",
        data=label_data,
        get_position=["lon", "lat"],
        get_text="label",
        get_size=11,
        get_color=[0, 0, 0, 255],
        get_angle=0,
        get_text_anchor='"middle"',
        get_alignment_baseline='"bottom"',
        get_pixel_offset=[0, -12],
        font_family='"Helvetica, Arial, sans-serif"',
        font_weight=700,
        background=True,
        get_background_color=[255, 255, 255, 220],
        background_padding=[3, 1],
    ))

    # Route title at bottom
    all_lats = [s["lat"] for s in stop_data]
    all_lons = [s["lon"] for s in stop_data]
    title_lat = min(all_lats) - (max(all_lats) - min(all_lats)) * 0.08
    title_lon = (min(all_lons) + max(all_lons)) / 2
    layers.append(pdk.Layer(
        "TextLayer",
        data=[{"lat": title_lat, "lon": title_lon, "label": route_name}],
        get_position=["lon", "lat"],
        get_text="label",
        get_size=16,
        get_color=[41, 128, 185, 255],
        get_angle=0,
        get_text_anchor='"middle"',
        get_alignment_baseline='"top"',
        font_family='"Helvetica, Arial, sans-serif"',
        font_weight=700,
        background=True,
        get_background_color=[255, 255, 255, 220],
        background_padding=[8, 4],
    ))

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(
            latitude=mid["lat"], longitude=mid["lon"], zoom=zoom, pitch=0,
        ),
        map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        tooltip={"text": "#{seq} {name}\nscore: {score}"},
    )
    st.pydeck_chart(deck, use_container_width=True)
    st.caption(
        "Purple = anchor | Green = high score | Yellow = medium | Red = low"
    )


def _render_geometry_map(route: Dict[str, Any]) -> None:
    """Map 2: ONLY Valhalla geometry — blue road line. No stops."""
    try:
        import pydeck as pdk
    except ImportError:
        return

    geojson = route.get("geometry_geojson")
    if isinstance(geojson, str):
        try:
            geojson = json.loads(geojson)
        except Exception:
            geojson = None
    if not geojson:
        return

    coords = geojson.get("coordinates", [])
    if len(coords) < 2:
        return

    route_name = route.get("route", "Geometry")
    st.markdown("#### Valhalla Geometry")

    # Metrics
    gm1, gm2, gm3 = st.columns(3)
    gm1.metric("Geometry km", f"{route.get('geometry_km', 0):.1f}")
    gm2.metric("Mode", route.get("geometry_mode", "?"))
    gm3.metric("Status", route.get("geometry_status", "?"))
    issues = route.get("geometry_issues", [])
    if issues:
        st.warning(f"Issues: {'; '.join(str(i) for i in issues)}")

    mid = coords[len(coords) // 2]
    sampled_lats = [c[1] for c in coords[::20]]
    sampled_lons = [c[0] for c in coords[::20]]
    zoom = _auto_zoom(sampled_lats, sampled_lons)

    layers = [
        pdk.Layer(
            "PathLayer",
            data=[{"path": coords, "name": route_name}],
            get_path="path",
            get_width=5,
            get_color=[41, 128, 185, 220],
            width_min_pixels=3,
        ),
    ]

    # Route title at bottom
    title_lat = min(sampled_lats) - (max(sampled_lats) - min(sampled_lats)) * 0.08
    title_lon = (min(sampled_lons) + max(sampled_lons)) / 2
    layers.append(pdk.Layer(
        "TextLayer",
        data=[{"lat": title_lat, "lon": title_lon, "label": route_name}],
        get_position=["lon", "lat"],
        get_text="label",
        get_size=16,
        get_color=[41, 128, 185, 255],
        get_angle=0,
        get_text_anchor='"middle"',
        get_alignment_baseline='"top"',
        font_family='"Helvetica, Arial, sans-serif"',
        font_weight=700,
        background=True,
        get_background_color=[255, 255, 255, 220],
        background_padding=[8, 4],
    ))

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(
            latitude=mid[1], longitude=mid[0], zoom=zoom, pitch=0,
        ),
        map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        tooltip={"text": "{name}"},
    )
    st.pydeck_chart(deck, use_container_width=True)
    st.caption(f"Blue line = Valhalla road geometry ({len(coords)} points)")
