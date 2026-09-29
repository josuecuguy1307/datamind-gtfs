"""
TEMP comparison section: Variant-D enriched quito_sur routes vs the routes
that were already in prod for the same unit (OSM-relation-derived and earlier
cycles). Reads the latest Variant-D run_summary.json to identify the enriched
set, then queries route_prod.routes for all other quito_sur rows as the
"pre-existing" comparison set.

Remove this file once quito_sur enrichment is signed off.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pydeck as pdk
import streamlit as st

from datamind_console.db import db_conn, fetch_all
from datamind_console.ui.components.maps import _deck_theme, _resolve_theme

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SUMMARY = (
    _PROJECT_ROOT / "constructor_artifacts" / "variant_d_quito_sur" / "run_summary.json"
)

_PREEXISTING_SOURCES = (
    "osm_relation_quito_sur_cycle0",
    "osm_relation_quito_sur_cycle1",
    "discovery_pipeline_quito_sur_06a",
)


def _map_theme_from_style() -> str:
    style = str(st.session_state.get("ui.style") or "Graphite")
    if style == "Graphite":
        return "dark"
    return "light"


@st.cache_data(ttl=60, show_spinner=False)
def _load_summary(path_str: str) -> List[Dict[str, Any]]:
    p = Path(path_str)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner="Loading enriched routes…")
def _fetch_enriched(route_ids: tuple[str, ...]) -> List[Dict[str, Any]]:
    if not route_ids:
        return []
    sql = """
    SELECT
      r.route_id::text AS route_id,
      COALESCE(r.route_name, r.route_id::text) AS route_name,
      r.source,
      COALESCE(array_length(r.stop_node_ids,1),0) AS stops,
      (ST_Length(r.geom::geography)/1000.0)::numeric(10,2) AS km,
      r.naming_confidence,
      r.direction_semantics,
      ST_AsGeoJSON(r.geom)::json -> 'coordinates' AS coords,
      ST_GeometryType(r.geom) AS gtype
    FROM route_prod.routes r
    WHERE r.route_id::text = ANY(%(ids)s)
    ORDER BY r.route_name
    """
    try:
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, {"ids": list(route_ids)}) or []
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner="Loading pre-existing routes…")
def _fetch_preexisting(sources: tuple[str, ...]) -> List[Dict[str, Any]]:
    if not sources:
        return []
    sql = """
    SELECT
      r.route_id::text AS route_id,
      COALESCE(r.route_name, r.route_id::text) AS route_name,
      r.source,
      COALESCE(array_length(r.stop_node_ids,1),0) AS stops,
      (ST_Length(r.geom::geography)/1000.0)::numeric(10,2) AS km,
      r.naming_confidence,
      cat.chosen_rel_operator     AS chosen_rel_operator,
      cat.service_route_operator  AS service_route_operator,
      cat.cooperative_hint        AS cooperative_hint,
      cat.operator_hint           AS operator_hint,
      ST_AsGeoJSON(r.geom)::json -> 'coordinates' AS coords,
      ST_GeometryType(r.geom) AS gtype
    FROM route_prod.routes r
    LEFT JOIN route_review.phase3_global_catalog_v1 cat
      ON cat.route_job_id::uuid = r.route_id
    WHERE r.source = ANY(%(srcs)s)
      AND r.province = 'sample_region'
    ORDER BY r.route_name
    """
    try:
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, {"srcs": list(sources)}) or []
    except Exception:
        return []


@st.cache_data(ttl=60, show_spinner="Loading stops…")
def _fetch_stops_for(route_ids: tuple[str, ...]) -> List[Dict[str, Any]]:
    if not route_ids:
        return []
    sql = """
    SELECT
      r.route_id::text AS route_id,
      r.route_name,
      n.node_id::text  AS stop_id,
      COALESCE(NULLIF(n.name, ''), 'Stop ' || LEFT(n.node_id::text, 8)) AS stop_name,
      ST_Y(n.geom) AS lat,
      ST_X(n.geom) AS lon,
      pos.ord AS ord
    FROM route_prod.routes r
    JOIN LATERAL unnest(r.stop_node_ids) WITH ORDINALITY AS pos(node_id, ord) ON true
    JOIN node_prod.nodes n ON n.node_id = pos.node_id
    WHERE r.route_id::text = ANY(%(ids)s)
    ORDER BY r.route_id, pos.ord
    """
    try:
        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, {"ids": list(route_ids)}) or []
    except Exception:
        return []


def _coords_to_path(coords, gtype: str):
    if not coords:
        return []
    if gtype and "Multi" in gtype:
        merged: list = []
        for seg in coords:
            if isinstance(seg, list):
                merged.extend(seg)
        return merged
    return coords


def _resolve_coop_enriched(row: dict) -> str:
    ds = row.get("direction_semantics") or {}
    if isinstance(ds, str):
        try:
            ds = json.loads(ds)
        except Exception:
            ds = {}
    if isinstance(ds, dict) and ds.get("cooperative"):
        return str(ds["cooperative"])
    return ""


def _resolve_coop_pre(row: dict) -> str:
    for k in ("operator_hint", "chosen_rel_operator", "service_route_operator", "cooperative_hint"):
        v = row.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _normalize_name(s: str | None) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", " ", s)


_COLOR_ENRICHED = [231, 76, 60, 230]    # red
_COLOR_PREEXIST = [52, 152, 219, 230]   # blue
_COLOR_ENRICHED_STOP = [255, 120, 100, 230]
_COLOR_PREEXIST_STOP = [120, 180, 255, 230]


def render_variant_d_vs_prod_section(summary_path: str | Path = _DEFAULT_SUMMARY) -> None:
    st.divider()
    st.markdown("### TEMP: Variant-D enriched **vs** pre-existing prod — quito_sur")
    st.caption(
        "Red = newly enriched from Variant-D shards (`canton_pipeline_06a_quito_sur`). "
        "Blue = pre-existing prod from OSM relations + earlier cycles. Remove this "
        "section once quito_sur enrichment is signed off."
    )

    summary_rows = _load_summary(str(summary_path))
    if not summary_rows:
        st.warning(f"No run_summary.json at `{summary_path}`. Run the driver first.")
        return

    built = [r for r in summary_rows if r.get("status") == "built"]
    blocked = [r for r in summary_rows if r.get("status") != "built"]
    enriched_ids = tuple(r["route_id"] for r in built if r.get("route_id"))

    enriched = _fetch_enriched(enriched_ids)
    preexisting = _fetch_preexisting(_PREEXISTING_SOURCES)

    for row in enriched:
        row["cooperative"] = _resolve_coop_enriched(row)
    for row in preexisting:
        row["cooperative"] = _resolve_coop_pre(row)

    e_stops_total = sum(int(r.get("stops") or 0) for r in enriched)
    p_stops_total = sum(int(r.get("stops") or 0) for r in preexisting)
    e_km_total = sum(float(r.get("km") or 0) for r in enriched)
    p_km_total = sum(float(r.get("km") or 0) for r in preexisting)

    # KPI row ---------------------------------------------------------
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Enriched", f"{len(enriched)}", f"{len(blocked)} blocked")
    c2.metric("Pre-existing", f"{len(preexisting)}")
    c3.metric(
        "Enriched stops",
        f"{e_stops_total:,}",
        f"avg {e_stops_total / max(len(enriched), 1):.1f}",
    )
    c4.metric(
        "Pre-existing stops",
        f"{p_stops_total:,}",
        f"avg {p_stops_total / max(len(preexisting), 1):.1f}",
    )
    c5.metric("Enriched km", f"{e_km_total:,.1f}")
    c6.metric("Pre-existing km", f"{p_km_total:,.1f}")

    # Blocked shards expander ----------------------------------------
    if blocked:
        with st.expander(f"⚠️  {len(blocked)} blocked shards — show details"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "route_code": b.get("route_code"),
                            "route_id": b.get("route_id"),
                            "status": b.get("status"),
                            "reason": b.get("reason") or b.get("error"),
                        }
                        for b in blocked
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "Most likely cause: seed anchors could not be resolved to any node during "
                "grounding. Investigate seed JSON vs node_prod coverage."
            )

    # Name overlap (possible duplicates) -----------------------------
    e_by_norm: dict[str, list[dict]] = {}
    for r in enriched:
        k = _normalize_name(r.get("route_name"))
        if not k:
            continue
        e_by_norm.setdefault(k, []).append(r)
    overlaps = []
    for r in preexisting:
        k = _normalize_name(r.get("route_name"))
        if not k:
            continue
        if k in e_by_norm:
            for e in e_by_norm[k]:
                overlaps.append(
                    {
                        "name": r.get("route_name"),
                        "enriched_stops": int(e.get("stops") or 0),
                        "pre_stops": int(r.get("stops") or 0),
                        "Δ stops": int(e.get("stops") or 0) - int(r.get("stops") or 0),
                        "enriched_km": float(e.get("km") or 0),
                        "pre_km": float(r.get("km") or 0),
                        "enriched_id": (e.get("route_id") or "")[:8],
                        "pre_id": (r.get("route_id") or "")[:8],
                    }
                )
    if overlaps:
        st.markdown("#### 🔁 Possible duplicates (name overlap)")
        st.caption(
            "Routes whose normalized name appears in both sets — candidates for "
            "deduplication / reconciliation."
        )
        st.dataframe(
            pd.DataFrame(overlaps).sort_values("Δ stops", ascending=False),
            use_container_width=True,
            hide_index=True,
            column_config={
                "enriched_km": st.column_config.NumberColumn(format="%.2f"),
                "pre_km": st.column_config.NumberColumn(format="%.2f"),
            },
        )

    # Cooperative-level comparison table -----------------------------
    coop_agg: dict[str, dict] = {}
    for r in enriched:
        k = r.get("cooperative") or "(unknown)"
        d = coop_agg.setdefault(k, {"e_routes": 0, "p_routes": 0, "e_stops": 0, "p_stops": 0, "e_km": 0.0, "p_km": 0.0})
        d["e_routes"] += 1
        d["e_stops"] += int(r.get("stops") or 0)
        d["e_km"] += float(r.get("km") or 0)
    for r in preexisting:
        k = r.get("cooperative") or "(unknown)"
        d = coop_agg.setdefault(k, {"e_routes": 0, "p_routes": 0, "e_stops": 0, "p_stops": 0, "e_km": 0.0, "p_km": 0.0})
        d["p_routes"] += 1
        d["p_stops"] += int(r.get("stops") or 0)
        d["p_km"] += float(r.get("km") or 0)
    coop_rows = [
        {
            "cooperative": c,
            "variant_d_routes": v["e_routes"],
            "pre_routes": v["p_routes"],
            "Δ routes": v["e_routes"] - v["p_routes"],
            "variant_d_stops": v["e_stops"],
            "pre_stops": v["p_stops"],
            "variant_d_km": round(v["e_km"], 2),
            "pre_km": round(v["p_km"], 2),
        }
        for c, v in coop_agg.items()
    ]
    st.markdown("#### By cooperative")
    if not coop_rows:
        st.info("No cooperative comparison is available for the selected data source.")
    else:
        coop_df = pd.DataFrame(coop_rows).sort_values(
            by=["variant_d_routes", "pre_routes"], ascending=False
        )
        st.dataframe(
            coop_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "variant_d_km": st.column_config.NumberColumn(format="%.2f"),
                "pre_km": st.column_config.NumberColumn(format="%.2f"),
            },
        )

    # Side-by-side tables --------------------------------------------
    st.markdown("#### Route lists")
    left, right = st.columns(2)
    with left:
        st.markdown("**Variant-D enriched (new)**")
        df_l = pd.DataFrame(
            [
                {
                    "route_name": r.get("route_name"),
                    "cooperative": r.get("cooperative"),
                    "stops": int(r.get("stops") or 0),
                    "km": float(r.get("km") or 0),
                    "conf": (float(r["naming_confidence"]) if r.get("naming_confidence") else None),
                    "route_id": r.get("route_id"),
                }
                for r in enriched
            ]
        )
        st.dataframe(
            df_l,
            use_container_width=True,
            hide_index=True,
            height=320,
            column_config={
                "km": st.column_config.NumberColumn(format="%.2f"),
                "conf": st.column_config.NumberColumn(format="%.3f"),
            },
        )
    with right:
        st.markdown("**Pre-existing in prod**")
        df_r = pd.DataFrame(
            [
                {
                    "route_name": r.get("route_name"),
                    "cooperative": r.get("cooperative"),
                    "stops": int(r.get("stops") or 0),
                    "km": float(r.get("km") or 0),
                    "conf": (float(r["naming_confidence"]) if r.get("naming_confidence") else None),
                    "source": r.get("source"),
                    "route_id": r.get("route_id"),
                }
                for r in preexisting
            ]
        )
        st.dataframe(
            df_r,
            use_container_width=True,
            hide_index=True,
            height=320,
            column_config={
                "km": st.column_config.NumberColumn(format="%.2f"),
                "conf": st.column_config.NumberColumn(format="%.3f"),
            },
        )

    # Paired map -----------------------------------------------------
    st.markdown("#### Side-by-side map")
    ctrl_l, ctrl_m, ctrl_r = st.columns([2, 2, 1])
    all_coops = sorted(coop_agg.keys())
    with ctrl_l:
        picked_coop = st.selectbox(
            "Filter by cooperative", ["All"] + all_coops, index=0, key="vd_vs_prod.coop"
        )
    with ctrl_m:
        show_stops = st.checkbox(
            "Show stops", value=False, key="vd_vs_prod.show_stops",
            help="Adds stop markers to the map. Only the current selection is queried.",
        )
    with ctrl_r:
        show_e = st.checkbox("Red", value=True, key="vd_vs_prod.show_e")
        show_p = st.checkbox("Blue", value=True, key="vd_vs_prod.show_p")

    if picked_coop == "All":
        e_pool = enriched
        p_pool = preexisting
    else:
        e_pool = [r for r in enriched if (r.get("cooperative") or "(unknown)") == picked_coop]
        p_pool = [r for r in preexisting if (r.get("cooperative") or "(unknown)") == picked_coop]

    c_left, c_right = st.columns(2)
    e_names = ["All Variant-D"] + [r.get("route_name") for r in e_pool]
    p_names = ["All pre-existing"] + [r.get("route_name") for r in p_pool]
    with c_left:
        pick_e = st.selectbox(
            "Variant-D route", e_names, index=0, key="vd_vs_prod.pick_e"
        )
    with c_right:
        pick_p = st.selectbox(
            "Pre-existing route", p_names, index=0, key="vd_vs_prod.pick_p"
        )

    sel_e = e_pool if pick_e == "All Variant-D" else [r for r in e_pool if r.get("route_name") == pick_e]
    sel_p = p_pool if pick_p == "All pre-existing" else [r for r in p_pool if r.get("route_name") == pick_p]

    if not show_e:
        sel_e = []
    if not show_p:
        sel_p = []

    path_e = []
    for r in sel_e:
        p = _coords_to_path(r.get("coords"), r.get("gtype") or "")
        if len(p) >= 2:
            path_e.append({"path": p, "name": r.get("route_name"), "color": _COLOR_ENRICHED, "bucket": "Variant-D"})
    path_p = []
    for r in sel_p:
        p = _coords_to_path(r.get("coords"), r.get("gtype") or "")
        if len(p) >= 2:
            path_p.append({"path": p, "name": r.get("route_name"), "color": _COLOR_PREEXIST, "bucket": "Pre-existing"})

    stops_e: list[dict] = []
    stops_p: list[dict] = []
    if show_stops:
        stops_e = _fetch_stops_for(tuple(r["route_id"] for r in sel_e if r.get("route_id")))
        stops_p = _fetch_stops_for(tuple(r["route_id"] for r in sel_p if r.get("route_id")))

    if not path_e and not path_p and not stops_e and not stops_p:
        st.info("No renderable geometries for the current selection.")
        return

    all_lats: list[float] = []
    all_lons: list[float] = []
    for src in (path_e, path_p):
        for p in src:
            for c in p["path"]:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    all_lons.append(float(c[0]))
                    all_lats.append(float(c[1]))
    for s in (*stops_e, *stops_p):
        if s.get("lat") is not None and s.get("lon") is not None:
            all_lats.append(float(s["lat"]))
            all_lons.append(float(s["lon"]))
    if all_lats and all_lons:
        center_lat = (min(all_lats) + max(all_lats)) / 2
        center_lon = (min(all_lons) + max(all_lons)) / 2
        span = max(max(all_lats) - min(all_lats), max(all_lons) - min(all_lons), 1e-6)
        zoom = (
            10.0 if span > 0.6
            else 11.0 if span > 0.25
            else 12.0 if span > 0.1
            else 13.0 if span > 0.05
            else 13.8
        )
    else:
        center_lat, center_lon, zoom = -0.26, -78.52, 11

    resolved = _resolve_theme(_map_theme_from_style())
    layers = []
    if path_e:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=pd.DataFrame(path_e),
                get_path="path",
                get_color="color",
                get_width=5,
                pickable=True,
                auto_highlight=True,
                width_min_pixels=3,
            )
        )
    if path_p:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=pd.DataFrame(path_p),
                get_path="path",
                get_color="color",
                get_width=4,
                pickable=True,
                auto_highlight=True,
                width_min_pixels=2,
            )
        )
    if stops_e:
        df_se = pd.DataFrame(
            [
                {"lat": float(s["lat"]), "lon": float(s["lon"]), "name": s.get("stop_name", ""),
                 "route": s.get("route_name", ""), "bucket": "Variant-D"}
                for s in stops_e
                if s.get("lat") is not None and s.get("lon") is not None
            ]
        )
        if not df_se.empty:
            layers.append(
                pdk.Layer(
                    "ScatterplotLayer",
                    data=df_se,
                    get_position="[lon, lat]",
                    get_radius=38,
                    get_fill_color=_COLOR_ENRICHED_STOP,
                    get_line_color=[20, 20, 40, 230],
                    line_width_min_pixels=1,
                    stroked=True,
                    pickable=True,
                    auto_highlight=True,
                )
            )
    if stops_p:
        df_sp = pd.DataFrame(
            [
                {"lat": float(s["lat"]), "lon": float(s["lon"]), "name": s.get("stop_name", ""),
                 "route": s.get("route_name", ""), "bucket": "Pre-existing"}
                for s in stops_p
                if s.get("lat") is not None and s.get("lon") is not None
            ]
        )
        if not df_sp.empty:
            layers.append(
                pdk.Layer(
                    "ScatterplotLayer",
                    data=df_sp,
                    get_position="[lon, lat]",
                    get_radius=32,
                    get_fill_color=_COLOR_PREEXIST_STOP,
                    get_line_color=[20, 20, 40, 230],
                    line_width_min_pixels=1,
                    stroked=True,
                    pickable=True,
                    auto_highlight=True,
                )
            )

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=pdk.ViewState(
            latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0
        ),
        tooltip={"text": "{bucket} · {name}\n{route}"},
        map_style=_deck_theme(resolved),
    )
    st.pydeck_chart(deck, use_container_width=True, height=600)
    st.caption(
        f"🔴 {len(path_e)} Variant-D path(s), {len(stops_e)} stop(s). "
        f"🔵 {len(path_p)} pre-existing path(s), {len(stops_p)} stop(s)."
    )
