from __future__ import annotations

import streamlit as st
from typing import Any, Dict, List, Optional
from uuid import UUID

from datamind_console.phases.phase3_routes.client import _get_phase3_client
from .geom import parse_linestring_wkt, line_to_points_df


def _geojson_to_points(geojson: Dict[str, Any]) -> List[Dict[str, float]]:
    if not geojson:
        return []
    geom = geojson.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []

    points: List[Dict[str, float]] = []
    if gtype == "LineString":
        for lon, lat in coords:
            points.append({"lon": float(lon), "lat": float(lat)})
    elif gtype == "MultiLineString":
        for line in coords:
            for lon, lat in line:
                points.append({"lon": float(lon), "lat": float(lat)})
    return points


def render_phase3_workspace_map(*, route_id: str, key: str = "p3_workspace") -> None:
    c = _get_phase3_client()
    ss = st.session_state

    st.subheader("Workspace Map")

    # Route selector (from DB list)
    try:
        jobs = c.list_route_jobs(limit=200)
        route_ids = [str(r.get("route_id")) for r in jobs if r.get("route_id")]
    except Exception:
        route_ids = []

    if route_ids:
        current = ss.get("p3.route_id") or route_id
        idx = route_ids.index(current) if current in route_ids else 0
        chosen = st.selectbox("route_id", route_ids, index=idx, key=f"{key}.route_id")
        ss["p3.route_id"] = chosen
        route_id = chosen

    # Relation candidates (ids for selection)
    rel_ids: List[str] = []
    try:
        rels = c.list_relation_candidates(UUID(route_id))
        rel_ids = [str(r.get("osm_relation_id")) for r in rels if r.get("osm_relation_id")]
    except Exception:
        rel_ids = []
    if not rel_ids:
        try:
            job = c.get_route_job(UUID(route_id))
            if job.get("osm_relation_id"):
                rel_ids = [str(job.get("osm_relation_id"))]
        except Exception:
            pass
    if not rel_ids:
        try:
            rel_ids = [str(x) for x in c.list_relation_raw_ids(UUID(route_id))]
        except Exception:
            rel_ids = []

    # Stop sequence candidates
    seq_ids: List[str] = []
    try:
        seq = c.get_sequence_candidates(UUID(route_id))
        seq_ids = [str(r.get("candidate_id")) for r in seq.get("candidates", []) if r.get("candidate_id")]
    except Exception:
        seq_ids = []

    # Geometry candidates
    geom_ids: List[str] = []
    try:
        geom = c.get_geometry_candidates(UUID(route_id))
        geom_ids = [str(r.get("geometry_candidate_id")) for r in geom.get("candidates", []) if r.get("geometry_candidate_id")]
    except Exception:
        geom_ids = []
    if not geom_ids:
        try:
            flat = c.list_geometry_candidates_flat(UUID(route_id))
            geom_ids = [str(r.get("geometry_candidate_id")) for r in flat if r.get("geometry_candidate_id")]
        except Exception:
            geom_ids = []

    # Production check
    prod = None
    try:
        prod = c.get_production_route_geometry(UUID(route_id))
    except Exception:
        prod = None

    # Render priority
    st.markdown("#### Map")

    mode = st.selectbox(
        "Render mode",
        [
            "Stops only",
            "Shape points (best available)",
        ],
        index=1,
        key=f"{key}.render_mode",
    )

    geom_id = ss.get("p3.active_geometry_candidate_id")
    seq_id = ss.get("p3.active_sequence_candidate_id")

    def _show_points(pts: List[Dict[str, float]], empty_msg: str) -> bool:
        if pts:
            st.map(pts)
            return True
        st.info(empty_msg)
        return False

    if mode == "Stops only":
        if seq_id:
            stops = c.get_stop_points_for_candidate(route_id=UUID(route_id), stop_sequence_candidate_id=UUID(seq_id))
            pts = [{"lat": float(r["lat"]), "lon": float(r["lon"])} for r in stops]
            _show_points(pts, "No stop points for this sequence.")
        else:
            stops = c.get_stop_prior_points(UUID(route_id))
            pts = [{"lat": float(r["lat"]), "lon": float(r["lon"])} for r in stops]
            _show_points(pts, "No stop prior points yet.")
    else:
        # Shape points: prefer prod -> geometry candidate -> stop sequence -> relation
        if prod and prod.get("geojson"):
            pts = _geojson_to_points(prod.get("geojson"))
            if _show_points(pts, "Production geometry has no drawable points."):
                pass
        elif geom_id:
            cand = c.get_geometry_candidate(UUID(geom_id))
            wkt = (cand.get("geom_wkt") or "").strip()
            line = parse_linestring_wkt(wkt)
            if line:
                st.map(line_to_points_df(line))
            else:
                st.info("Geometry candidate has no drawable line.")
        elif seq_id:
            seq_geom = c.get_sequence_geometry(UUID(seq_id))
            pts = _geojson_to_points(seq_geom.get("geojson") or {})
            _show_points(pts, "Stop sequence has no drawable points.")
        else:
            rel_geom = c.get_relation_geometry(UUID(route_id))
            pts = _geojson_to_points(rel_geom.get("geojson") or {})
            if not _show_points(pts, "No geometry available yet. Run Step 10 or later."):
                return

    # Selection controls (below map)
    st.markdown("#### Select what to display")

    if rel_ids:
        rel_pick = st.selectbox(
            "OSM relation id",
            options=rel_ids,
            index=rel_ids.index(ss.get("p3.osm_relation_id")) if ss.get("p3.osm_relation_id") in rel_ids else 0,
            key=f"{key}.rel_pick",
        )
        ss["p3.osm_relation_id"] = int(rel_pick)
    else:
        st.caption("No relation candidates available.")

    if seq_ids:
        seq_pick = st.selectbox(
            "stop_sequence_candidate_id",
            options=seq_ids,
            index=seq_ids.index(ss.get("p3.active_sequence_candidate_id")) if ss.get("p3.active_sequence_candidate_id") in seq_ids else 0,
            key=f"{key}.seq_pick",
        )
        ss["p3.active_sequence_candidate_id"] = seq_pick
    else:
        st.caption("No stop sequence candidates available.")

    if geom_ids:
        geom_pick = st.selectbox(
            "geometry_candidate_id",
            options=geom_ids,
            index=geom_ids.index(ss.get("p3.active_geometry_candidate_id")) if ss.get("p3.active_geometry_candidate_id") in geom_ids else 0,
            key=f"{key}.geom_pick",
        )
        ss["p3.active_geometry_candidate_id"] = geom_pick
        ss["p3.geometry_candidate_id"] = geom_pick
    else:
        st.caption("No geometry candidates available.")
