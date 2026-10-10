# ============================================================
# DEPRECATED — Constructor V2 replaces this module.
# See DEPRECATED_V1_MODULES.md for details.
# This file is preserved for historical reference only.
# ============================================================
"""
Stop Grounding Catalog — v3 catalog viewer with jurisdiction/route_type filters.

Shows all routes from the v3 hints catalog, their pipeline run results,
and provides filters for jurisdiction, route_type, and cooperative.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pydeck as pdk
import streamlit as st
from datamind_console.i18n import t, option_label


PROJECT_ROOT = Path(__file__).resolve().parents[5]
V3_CATALOG = PROJECT_ROOT / "constructor_artifacts" / "valle_de_los_chillos_hints_catalog_v3.json"
PIPELINE_AUDIT = PROJECT_ROOT / "constructor_artifacts" / "valle_v3_pipeline_run" / "v3_pipeline_audit.json"
ARTIFACT_DIRS = {
    "routes": PROJECT_ROOT / "constructor_artifacts" / "valle_v3_pipeline_run" / "routes",
    # Legacy dirs from earlier split runs — still valid for viewing past results
    "critical": PROJECT_ROOT / "constructor_artifacts" / "valle_v3_pipeline_run" / "critical",
    "remaining": PROJECT_ROOT / "constructor_artifacts" / "valle_v3_pipeline_run" / "remaining",
}


def _load_v3_catalog() -> Dict[str, Any]:
    if not V3_CATALOG.exists():
        return {}
    with open(V3_CATALOG, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_pipeline_audit() -> Dict[str, Any]:
    if not PIPELINE_AUDIT.exists():
        return {}
    with open(PIPELINE_AUDIT, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_route_dir(route_name: str, cooperative: str) -> Optional[Path]:
    """Find the artifact directory for a route."""
    safe_name = f"{route_name.replace(' ', '_')}__{cooperative.replace(' ', '_')}"
    for group_dir in ARTIFACT_DIRS.values():
        candidate = group_dir / safe_name
        if candidate.exists():
            return candidate
    return None


def _load_route_artifact(route_name: str, cooperative: str) -> Optional[Dict[str, Any]]:
    """Try to load the constructor_run_summary for a specific route."""
    route_dir = _find_route_dir(route_name, cooperative)
    if not route_dir:
        return None
    summary = route_dir / "constructor_run_summary.json"
    if summary.exists():
        with open(summary, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _load_route_geometry(route_name: str, cooperative: str) -> Optional[Dict[str, Any]]:
    """Load geometry_candidate.json for a route."""
    route_dir = _find_route_dir(route_name, cooperative)
    if not route_dir:
        return None
    geom_file = route_dir / "geometry_candidate.json"
    if geom_file.exists():
        with open(geom_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _load_route_skeleton(route_name: str, cooperative: str) -> Optional[Dict[str, Any]]:
    """Load sequence_skeleton.json for a route."""
    route_dir = _find_route_dir(route_name, cooperative)
    if not route_dir:
        return None
    skel_file = route_dir / "sequence_skeleton.json"
    if skel_file.exists():
        with open(skel_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def _render_route_map(
    geometry: Optional[Dict[str, Any]],
    skeleton: Optional[Dict[str, Any]],
    anchors: List[Dict[str, Any]],
    route_name: str = "",
) -> None:
    """Render a pydeck map with geometry line, skeleton stops, and anchor points."""
    layers = []
    all_lats = []
    all_lons = []

    # Geometry line
    geom_coords = None
    if geometry:
        geom_coords = geometry.get("coordinates") or (geometry.get("geometry", {}) or {}).get("coordinates")
    if not geom_coords and geometry:
        # Try geojson wrapper
        geojson = geometry.get("geometry_geojson") or geometry.get("geojson")
        if geojson:
            geom_coords = geojson.get("coordinates")

    if geom_coords and len(geom_coords) > 1:
        path_data = [{"path": [[c[0], c[1]] for c in geom_coords]}]
        all_lons.extend([c[0] for c in geom_coords])
        all_lats.extend([c[1] for c in geom_coords])
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=path_data,
                get_path="path",
                get_color=[0, 120, 255, 180],
                width_min_pixels=3,
                width_max_pixels=6,
            )
        )

    # Skeleton stops
    stop_points = []
    if skeleton:
        for s in skeleton.get("ordered_stops", []):
            lat = s.get("lat", s.get("latitude"))
            lon = s.get("lon", s.get("longitude"))
            if lat and lon:
                stop_points.append({
                    "lat": lat, "lon": lon,
                    "name": s.get("stop_name", "?"),
                    "color": [0, 200, 80, 220],
                })
                all_lats.append(lat)
                all_lons.append(lon)

    # Anchor points from catalog
    for anchor in anchors:
        if isinstance(anchor, dict) and anchor.get("lat") and anchor.get("lon"):
            stop_points.append({
                "lat": anchor["lat"], "lon": anchor["lon"],
                "name": f"[anchor] {anchor.get('name', '?')}",
                "color": [255, 100, 0, 220] if anchor.get("terminus_type") else [255, 200, 0, 200],
            })
            all_lats.append(anchor["lat"])
            all_lons.append(anchor["lon"])

    if stop_points:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=stop_points,
                get_position=["lon", "lat"],
                get_fill_color="color",
                get_radius=120,
                pickable=True,
            )
        )
        layers.append(
            pdk.Layer(
                "TextLayer",
                data=stop_points,
                get_position=["lon", "lat"],
                get_text="name",
                get_size=11,
                get_color=[40, 40, 40, 255],
                get_alignment_baseline="'bottom'",
                get_pixel_offset=[0, -14],
            )
        )

    if not layers:
        st.info(t("ui.p3.grounding_catalog.no_geometry_stop_data_available_this"))
        return

    center_lat = sum(all_lats) / len(all_lats)
    center_lon = sum(all_lons) / len(all_lons)
    lat_range = max(all_lats) - min(all_lats)
    lon_range = max(all_lons) - min(all_lons)
    spread = max(lat_range, lon_range)
    zoom = 14 if spread < 0.01 else 13 if spread < 0.03 else 12 if spread < 0.06 else 11 if spread < 0.12 else 10

    # Route name label — large, centered on the corridor
    if route_name:
        # Pick the midpoint of the geometry line for the label anchor
        if geom_coords and len(geom_coords) > 1:
            mid_idx = len(geom_coords) // 2
            label_lon, label_lat = geom_coords[mid_idx][0], geom_coords[mid_idx][1]
        else:
            label_lat, label_lon = center_lat, center_lon
        layers.append(
            pdk.Layer(
                "TextLayer",
                data=[{"lat": label_lat, "lon": label_lon, "label": route_name}],
                get_position=["lon", "lat"],
                get_text="label",
                get_size=22,
                get_color=[10, 10, 10, 255],
                get_background_color=[255, 255, 255, 200],
                background=True,
                get_border_color=[0, 120, 255, 200],
                border_radius=4,
                get_pixel_offset=[0, 0],
                font_weight=700,
                billboard=True,
            )
        )

    view = pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0)
    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view,
        map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        tooltip={"text": "{name}"},
    )
    st.pydeck_chart(deck, use_container_width=True)


def _route_status_badge(status: str) -> str:
    badges = {
        "confirmed": "🟢",
        "need_to_create": "🟡",
        "probable": "🟠",
        "pilot": "🔵",
    }
    return badges.get(status, "⚪")


def _terminus_type_label(ttype: str) -> str:
    labels = {
        "formal_terminal": t("grounding.terminus.formal_terminal"),
        "street_terminus": t("grounding.terminus.street_terminus"),
        "neighborhood_endpoint": t("grounding.terminus.neighborhood_endpoint"),
    }
    return labels.get(ttype, ttype or "—")


def _build_route_table(catalog: Dict[str, Any], audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build display table joining catalog entries with pipeline results."""
    routes = catalog.get("routes", [])

    # Index audit results by route name
    audit_by_route: Dict[str, Dict] = {}
    # Support both new flat format (route_audits) and legacy split format
    for section in ("route_audits", "critical_route_audits", "remaining_v3_audits"):
        for entry in audit.get(section, []):
            key = entry.get("route", "")
            audit_by_route[key] = entry

    rows = []
    for r in routes:
        route_name = r.get("route", "?")
        coop = r.get("cooperative", "?")
        jurisdiction = r.get("jurisdiction", "both")
        route_type = r.get("route_type", "—")
        route_status = r.get("route_status", "—")
        service_schedule = r.get("service_schedule", "—")

        # Extract terminus info from researched_anchors
        terminus_types = []
        for anchor in r.get("researched_anchors", []):
            if isinstance(anchor, dict) and anchor.get("terminus_type"):
                terminus_types.append(
                    f"{anchor.get('name', '?')}: {anchor['terminus_type']}"
                )

        # Pipeline results
        a = audit_by_route.get(route_name, {})
        stops = a.get("stops", a.get("skeleton_stops", "—"))
        geom_km = a.get("geometry_km", a.get("geometry_length_km", "—"))
        if isinstance(geom_km, (int, float)):
            geom_km = f"{geom_km:.1f}"
        pipeline_status = a.get("status") or t("grounding.not_run")
        terminus_survived = a.get("terminus_survived")
        term_label = "—"
        if terminus_survived is True:
            term_label = t("grounding.terminus_survived", km=a.get('terminus_distance_km', a.get('distance_to_expected_terminus_km', '?')))
        elif terminus_survived is False:
            term_label = t("grounding.terminus_pruned", km=a.get('terminus_distance_km', a.get('distance_to_expected_terminus_km', '?')))

        rows.append({
            "Status": _route_status_badge(route_status),
            "Cooperative": coop,
            "Route": route_name,
            "Jurisdiction": jurisdiction.upper(),
            "Type": route_type,
            "Schedule": service_schedule,
            "Pipeline": pipeline_status,
            "Stops": stops,
            "Geometry (km)": geom_km,
            "Terminus": term_label,
            "Terminus Types": "; ".join(terminus_types) if terminus_types else "—",
            "route_status": route_status,  # for filtering, hidden
        })

    return rows


def render_grounding_catalog_tab(ctx, client) -> None:
    del ctx, client
    ss = st.session_state

    st.subheader(t("ui.p3.grounding_catalog.stop_grounding_catalog_v3"))
    st.caption(
        t("ui.p3.grounding_catalog.routes_from_valle_de_los_chillos")
    )

    catalog = _load_v3_catalog()
    if not catalog:
        st.error(t("ui.p3.grounding_catalog.v3_catalog_not_found_at_v3", v3_catalog=(V3_CATALOG)))
        return

    audit = _load_pipeline_audit()

    # ── Metrics ──
    routes = catalog.get("routes", [])
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(t("ui.p3.grounding_catalog.total_routes"), len(routes))
    with m2:
        coops = set(r.get("cooperative", "") for r in routes)
        st.metric(t("ui.p3.grounding_catalog.cooperatives"), len(coops))
    with m3:
        jurisdictions = {}
        for r in routes:
            j = r.get("jurisdiction", "both")
            jurisdictions[j] = jurisdictions.get(j, 0) + 1
        st.metric(t("ui.p3.grounding_catalog.jurisdictions"), ", ".join(f"{k}:{v}" for k, v in sorted(jurisdictions.items())))
    with m4:
        run_count = len(audit.get("route_audits", [])) or (
            len(audit.get("critical_route_audits", [])) + len(audit.get("remaining_v3_audits", []))
        )
        st.metric(t("ui.p3.grounding_catalog.pipeline_runs"), run_count)

    # ── Filters ──
    st.divider()
    f1, f2, f3, f4 = st.columns(4)

    all_coops = sorted(set(r.get("cooperative", "?") for r in routes))
    all_jurisdictions = sorted(set(r.get("jurisdiction", "both") for r in routes))
    all_types = sorted(set(r.get("route_type", "—") for r in routes))
    all_statuses = sorted(set(r.get("route_status", "—") for r in routes))

    with f1:
        jurisdiction_filter = st.selectbox(
            t("ui.p3.grounding_catalog.jurisdiction"),
            options=["all"] + all_jurisdictions,
            key="p3.grounding.jurisdiction_filter",
            format_func=option_label(),
        )
    with f2:
        route_type_filter = st.selectbox(
            t("ui.p3.grounding_catalog.route_type"),
            options=["all"] + all_types,
            key="p3.grounding.route_type_filter",
            format_func=option_label(),
        )
    with f3:
        coop_filter = st.selectbox(
            t("ui.p3.grounding_catalog.cooperative"),
            options=["all"] + all_coops,
            key="p3.grounding.coop_filter",
            format_func=option_label(),
        )
    with f4:
        status_filter = st.selectbox(
            t("ui.p3.grounding_catalog.route_status"),
            options=["all"] + all_statuses,
            key="p3.grounding.status_filter",
            format_func=option_label(),
        )

    # ── Build and filter table ──
    all_rows = _build_route_table(catalog, audit)

    filtered = all_rows
    if jurisdiction_filter != "all":
        filtered = [r for r in filtered if r["Jurisdiction"].lower() == jurisdiction_filter.lower()]
    if route_type_filter != "all":
        filtered = [r for r in filtered if r["Type"] == route_type_filter]
    if coop_filter != "all":
        filtered = [r for r in filtered if r["Cooperative"] == coop_filter]
    if status_filter != "all":
        filtered = [r for r in filtered if r["route_status"] == status_filter]

    # Remove internal column before display
    # Row keys stay English (filters use them); headers are translated for display only.
    display_rows = [
        {t(f"grounding.col.{k.lower().replace(' ', '_').replace('(', '').replace(')', '')}"): v for k, v in r.items() if k != "route_status"}
        for r in filtered
    ]

    st.markdown(t("ui.p3.grounding_catalog.route_catalog_n_display_rows_routes", n_display_rows=(len(display_rows))))
    if display_rows:
        st.dataframe(display_rows, use_container_width=True, hide_index=True, height=400)
    else:
        st.info(t("ui.p3.grounding_catalog.no_routes_match_current_filters"))

    # ── Route detail inspector ──
    if filtered:
        st.divider()
        route_names = [r["Route"] for r in filtered]
        selected_route = st.selectbox(
            t("ui.p3.grounding_catalog.inspect_route_detail"),
            options=route_names,
            format_func=lambda name: next(
                (f"{r['Status']} {r['Cooperative']} — {name}" for r in filtered if r["Route"] == name),
                name,
            ),
            key="p3.grounding.selected_route",
        )

        if selected_route:
            # Find catalog entry
            entry = next((r for r in routes if r.get("route") == selected_route), {})
            if entry:
                detail_left, detail_right = st.columns(2)

                with detail_left:
                    st.markdown(t("ui.p3.grounding_catalog.catalog_entry"))
                    st.json({
                        "cooperative": entry.get("cooperative"),
                        "route": entry.get("route"),
                        "route_status": entry.get("route_status"),
                        "jurisdiction": entry.get("jurisdiction"),
                        "route_type": entry.get("route_type"),
                        "service_schedule": entry.get("service_schedule"),
                        "fleet_size": entry.get("fleet_size"),
                        "explicit_anchors": entry.get("explicit_anchors"),
                        "sequence_seed": entry.get("sequence_seed"),
                        "sequence_confidence": entry.get("sequence_confidence"),
                        "why": entry.get("why"),
                    })

                with detail_right:
                    st.markdown(t("ui.p3.grounding_catalog.researched_anchors"))
                    anchors = entry.get("researched_anchors", [])
                    for anchor in anchors:
                        if isinstance(anchor, dict):
                            ttype = anchor.get("terminus_type")
                            badge = f" `{ttype}`" if ttype else ""
                            conf = anchor.get("confidence", "?")
                            st.markdown(
                                f"- **{anchor.get('name', '?')}**{badge} "
                                f"({anchor.get('lat', '?')}, {anchor.get('lon', '?')}) "
                                f"— {conf}"
                            )
                        else:
                            st.markdown(f"- {anchor}")

                    if entry.get("schedule"):
                        st.markdown(t("ui.p3.grounding_catalog.schedule"))
                        st.json(entry["schedule"])

                    if entry.get("source_notes"):
                        st.markdown(t("ui.p3.grounding_catalog.source_notes"))
                        for note in entry["source_notes"]:
                            st.caption(f"- {note}")

            # ── Route Map ──
            coop_name = entry.get("cooperative", "?")
            geometry = _load_route_geometry(selected_route, coop_name)
            skeleton = _load_route_skeleton(selected_route, coop_name)
            if geometry or skeleton or anchors:
                st.markdown(t("ui.p3.grounding_catalog.route_geometry_stops"))
                _render_route_map(geometry, skeleton, anchors, route_name=selected_route)
                if skeleton:
                    stops = skeleton.get("ordered_stops", [])
                    st.caption(
                        t("ui.p3.grounding_catalog.n_stops_skeleton_stops_n_marginal", n_stops=(len(stops)), n_marginal_stops=(len(skeleton.get('marginal_stops', []))), total_length_km=(skeleton.get('total_length_km', '?')))
                    )

            # Pipeline artifacts
            artifact = _load_route_artifact(selected_route, coop_name)
            if artifact:
                with st.expander(t("ui.p3.grounding_catalog.pipeline_run_summary"), expanded=False):
                    st.json(artifact)

    # ── Legend ──
    with st.expander(t("ui.p3.grounding_catalog.legend"), expanded=False):
        st.markdown(
            t("ui.p3.grounding_catalog.status_badges_confirmed_need_create_probable")
        )
