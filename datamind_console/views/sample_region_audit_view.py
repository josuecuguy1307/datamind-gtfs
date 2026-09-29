"""Sample Region Audit Dashboard.

Read-only audit surface for the Phase-4 cleanliness re-classification.
Shows the active routes of the region filtered by cleanliness_status /
dirty_reason / operator / unit / name, with per-route drill-down (map,
stops, catalog rows, runtime, timeline).

Wired in via `datamind_console/app.py` as the "Sample Region Audit" page.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

import pandas as pd
import psycopg2
import psycopg2.extras
import streamlit as st


# Maps the messy phase3_global_catalog_v1.sector_key into a canton/sub-sector
# bucket the operator can filter by. Inline as a SQL fragment so we can use it
# both in SELECT and in WHERE without repeating logic.
_UNIT_CASE_SQL = """
  CASE
    WHEN v.sector_key IS NULL OR v.sector_key='unassigned' THEN 'Sin asignar'
    WHEN v.sector_key IN ('valle_de_los_chillos','Valle de Los Chillos','sangolqui')
      THEN 'Rumiñahui · Valle de los Chillos'
    WHEN v.sector_key='ANT_PEDRO_MONCAYO' THEN 'Pedro Moncayo'
    WHEN v.sector_key='cayambe' THEN 'Cayambe'
    WHEN v.sector_key IN ('mejia','tambillo') THEN 'Mejía'
    WHEN v.sector_key IN ('quito_centro','de las universidades','centro historico',
                          'centro historico de quito','la marin','23 de mayo','el comercio')
      THEN 'DMQ · Centro'
    WHEN v.sector_key IN ('quito_norte','quito_norte_cycle1','quito_norte_cycle1b','quito_norte_extra',
                          'terminal norte la y','calderon','pomasqui','capuli','comite del pueblo',
                          'terminal rio coca')
      THEN 'DMQ · Norte'
    WHEN v.sector_key IN ('quito_sur','quito_sur_chillogallo_corridor','quito_sur_outer_axis',
                          'quito_sur_gateway_connector','moran valverde','guamani',
                          'puente de guajalo','pintag_corridor','chillogallo','quitumbe',
                          'terminal quitumbe')
      THEN 'DMQ · Sur'
    WHEN v.sector_key IN ('tumbaco','tumbaco_cumbaya','Tumbaco-Cumbaya','cumbaya',
                          'cumbaya centro','pifo','pifo centro','puembo','checa',
                          'interoceanica','la morita')
      THEN 'DMQ · Valles'
    WHEN v.sector_key IN ('amaguana','amaguana_corridor','conocoto') THEN 'DMQ · Valles Sur'
    WHEN v.sector_key='Quito Urbano' THEN 'DMQ · Urbano'
    WHEN v.sector_key IN ('rumipamba','scala shopping','la salle','oton de velez')
      THEN 'DMQ · Otros'
    ELSE COALESCE(v.sector_key, 'Sin asignar')
  END
"""


# ──────────────────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────────────────

def _dsn() -> str:
    local_only = str(os.getenv("DATAMIND_LOCAL_ONLY_MODE", "")).strip().lower() in {
        "1", "true", "t", "yes", "y", "on"
    }
    if local_only:
        value = os.getenv("LOCAL_DB_DSN") or os.getenv("DATAMIND_LOCAL_DB_DSN")
        if not value:
            raise RuntimeError(
                "Sample Region audit data is unavailable: configure LOCAL_DB_DSN for this local-only session. "
                "Remote database fallbacks are disabled."
            )
        return value

    value = os.getenv("DB_DSN") or os.getenv("LOCAL_DB_DSN")
    if not value:
        raise RuntimeError("DB_DSN or LOCAL_DB_DSN must be configured before loading audit data.")
    return value


@st.cache_resource(show_spinner=False)
def _conn(dsn: str):
    return psycopg2.connect(dsn)


def _fetch(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    conn = _conn(_dsn())
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        try:
            return cur.fetchall()
        except psycopg2.ProgrammingError:
            return []


# ──────────────────────────────────────────────────────────────────────────
# KPIs
# ──────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _kpis() -> Dict[str, int]:
    rows = _fetch("""
        SELECT cleanliness_status, dirty_reason, COUNT(*) AS n
        FROM route_prod.routes
        WHERE province='sample_region' AND deploy_status='active'
        GROUP BY 1,2;
    """)
    total = sum(r["n"] for r in rows)
    clean = sum(r["n"] for r in rows if r["cleanliness_status"] == "clean")
    synth = sum(r["n"] for r in rows if r["dirty_reason"] == "phase4_catalog_synthesized")
    pending = sum(r["n"] for r in rows if r["dirty_reason"] == "stop_quality_pending")
    dup = sum(r["n"] for r in rows if r["dirty_reason"] == "stop_duplicate_residual")
    canon_gap = sum(r["n"] for r in rows if r["dirty_reason"] in ("real_canon_gap", "phantom_canon"))
    unproc = sum(r["n"] for r in rows if r["cleanliness_status"] == "unprocessed")
    runtime = sum(r["n"] for r in rows if r["dirty_reason"] == "unrealistic_runtime")
    return {
        "total": total, "clean": clean, "synth": synth,
        "stop_quality": pending, "stop_dup": dup, "canon_gap": canon_gap,
        "unprocessed": unproc, "runtime": runtime,
        "ship_eligible": clean + synth,
    }


@st.cache_data(ttl=60, show_spinner=False)
def _bucket_breakdown() -> pd.DataFrame:
    rows = _fetch("""
        SELECT
          CASE
            WHEN cleanliness_status='clean' THEN 'clean'
            WHEN cleanliness_status='unprocessed' THEN 'unprocessed'
            ELSE 'dirty/' || dirty_reason
          END AS bucket,
          COUNT(*) AS n
        FROM route_prod.routes
        WHERE province='sample_region' AND deploy_status='active'
        GROUP BY 1 ORDER BY n DESC;
    """)
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────
# Routes table (filtered)
# ──────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30, show_spinner=False)
def _routes_table(
    status: str | None,
    reason: str | None,
    operator_substr: str | None,
    name_substr: str | None,
    route_id: str | None,
    unit: str | None,
) -> pd.DataFrame:
    where = ["r.province='sample_region'", "r.deploy_status='active'"]
    params: list = []
    if status and status != "All":
        where.append("r.cleanliness_status = %s")
        params.append(status)
    if reason and reason != "All":
        if reason == "(none)":
            where.append("r.dirty_reason IS NULL")
        else:
            where.append("r.dirty_reason = %s")
            params.append(reason)
    if name_substr:
        where.append("r.route_name ILIKE %s")
        params.append(f"%{name_substr}%")
    if operator_substr:
        where.append("cs.operator ILIKE %s")
        params.append(f"%{operator_substr}%")
    if route_id:
        where.append("r.route_id::text = %s")
        params.append(route_id)
    if unit and unit != "All":
        where.append(f"{_UNIT_CASE_SQL} = %s")
        params.append(unit)

    rows = _fetch(f"""
        SELECT
          r.route_id::text AS route_id,
          r.route_name,
          r.cleanliness_status AS status,
          r.dirty_reason AS reason,
          ROUND(r.naming_confidence::numeric, 2) AS conf,
          cs.operator,
          cs.route_short_name AS short,
          {_UNIT_CASE_SQL} AS unit,
          ROUND((ST_Length(r.geom::geography)/1000.0)::numeric, 1) AS km,
          COALESCE(array_length(r.stop_node_ids, 1), 0) AS n_stops
        FROM route_prod.routes r
        LEFT JOIN catalog.route_semantics cs ON cs.route_id = r.route_id
        LEFT JOIN route_review.phase3_global_catalog_v1 v
               ON v.canonical_route_job_id::text = r.route_id::text
        WHERE {' AND '.join(where)}
        ORDER BY r.cleanliness_status, r.dirty_reason NULLS FIRST, r.route_name
        LIMIT 1000;
    """, tuple(params))
    return pd.DataFrame(rows)


@st.cache_data(ttl=120, show_spinner=False)
def _distinct_dirty_reasons() -> List[str]:
    rows = _fetch("""
        SELECT DISTINCT dirty_reason
        FROM route_prod.routes
        WHERE province='sample_region' AND deploy_status='active' AND dirty_reason IS NOT NULL
        ORDER BY 1;
    """)
    return [r["dirty_reason"] for r in rows]


@st.cache_data(ttl=120, show_spinner=False)
def _distinct_units() -> List[str]:
    rows = _fetch(f"""
        SELECT {_UNIT_CASE_SQL} AS unit, COUNT(*) AS n
        FROM route_prod.routes r
        LEFT JOIN route_review.phase3_global_catalog_v1 v
               ON v.canonical_route_job_id::text = r.route_id::text
        WHERE r.province='sample_region' AND r.deploy_status='active'
        GROUP BY 1 ORDER BY n DESC;
    """)
    return [r["unit"] for r in rows if r.get("unit")]


# ──────────────────────────────────────────────────────────────────────────
# Drill-down loaders
# ──────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=30, show_spinner=False)
def _route_detail(route_id: str) -> Dict[str, Any]:
    rs = _fetch("""
        SELECT
          r.route_id::text AS route_id, r.route_name, r.cleanliness_status, r.dirty_reason,
          r.naming_confidence, r.canonical_sequence_ready, r.legacy_grandfathered,
          r.semantics_updated_at, r.cleanliness_classified_at, r.updated_at,
          r.direction_id, COALESCE(array_length(r.stop_node_ids,1),0) AS n_stops,
          ROUND((ST_Length(r.geom::geography)/1000.0)::numeric, 1) AS km,
          ST_AsText(ST_StartPoint(r.geom)) AS start_wkt,
          ST_AsText(ST_EndPoint(r.geom)) AS end_wkt
        FROM route_prod.routes r
        WHERE r.route_id::text = %s
    """, (route_id,))
    return rs[0] if rs else {}


@st.cache_data(ttl=30, show_spinner=False)
def _route_stops(route_id: str) -> pd.DataFrame:
    rows = _fetch("""
        WITH ord AS (
          SELECT s.candidate_id,
                 s.stop_node_ids[i] AS node_id,
                 i AS seq
          FROM route_work.stop_sequence_candidates s,
               generate_subscripts(s.stop_node_ids, 1) AS i
          WHERE s.candidate_id = (
            SELECT chosen_stop_sequence_candidate_id FROM route_prod.routes WHERE route_id = %s::uuid
          )
        )
        SELECT o.seq, o.node_id::text AS node_id, n.name, n.confidence,
               ROUND(ST_Y(n.geom)::numeric, 6) AS lat, ROUND(ST_X(n.geom)::numeric, 6) AS lon,
               n.superseded_by IS NOT NULL AS superseded
        FROM ord o
        LEFT JOIN node_prod.nodes n ON n.node_id = o.node_id
        ORDER BY o.seq;
    """, (route_id,))
    if not rows:
        # Fallback: use route_prod.routes.stop_node_ids array order.
        rows = _fetch("""
            SELECT su.seq, n.node_id::text AS node_id, n.name, n.confidence,
                   ROUND(ST_Y(n.geom)::numeric, 6) AS lat, ROUND(ST_X(n.geom)::numeric, 6) AS lon,
                   n.superseded_by IS NOT NULL AS superseded
            FROM (
              SELECT unnest(stop_node_ids) AS node_id,
                     generate_subscripts(stop_node_ids, 1) AS seq
              FROM route_prod.routes WHERE route_id = %s::uuid
            ) su
            LEFT JOIN node_prod.nodes n ON n.node_id = su.node_id
            ORDER BY su.seq;
        """, (route_id,))
    return pd.DataFrame(rows)


@st.cache_data(ttl=30, show_spinner=False)
def _route_catalog(route_id: str) -> Dict[str, Any]:
    sem = _fetch("""SELECT * FROM catalog.route_semantics WHERE route_id = %s::uuid""", (route_id,))
    sched = _fetch("""SELECT direction_id, service_pattern_id, window_start::text, window_end::text,
                            headway_min, runtime_override_min, runtime_override_reason, peak_type,
                            estimated_vehicles, cycle_time_min, source, confidence
                     FROM catalog.route_schedule_profile WHERE route_id = %s::uuid
                     ORDER BY direction_id, service_pattern_id""", (route_id,))
    days = _fetch("""SELECT service_pattern_id, monday, tuesday, wednesday, thursday, friday,
                            saturday, sunday, first_departure::text, last_departure::text,
                            headway_min, source, confidence
                     FROM catalog.route_service_days WHERE route_id = %s::uuid
                     ORDER BY service_pattern_id""", (route_id,))
    exc = _fetch("""SELECT exception_date::text, exception_type, reason
                    FROM catalog.route_service_exceptions WHERE route_id = %s::uuid
                    ORDER BY exception_date""", (route_id,))
    lay = _fetch("""SELECT layover_at_destination_min, layover_at_origin_min, min_layover_min,
                          max_layover_min, applies_to_pattern, source, confidence
                   FROM catalog.route_layover_policy WHERE route_id = %s::uuid""", (route_id,))
    return {"semantics": sem, "schedule": sched, "days": days, "exceptions": exc, "layover": lay}


@st.cache_data(ttl=30, show_spinner=False)
def _route_runtime(route_id: str) -> pd.DataFrame:
    rows = _fetch("""
        SELECT direction_id,
               ROUND((metrics->>'runtime_peak_secs')::numeric / 60, 1) AS peak_min,
               ROUND((metrics->>'runtime_offpeak_secs')::numeric / 60, 1) AS offpeak_min,
               metrics->>'n_legs' AS n_legs,
               metrics->>'method' AS method,
               metrics->>'confidence' AS conf,
               area_profile_code AS area, speed_profile_code AS speed,
               dwell_profile_code AS dwell,
               status, estimated_at::text AS estimated_at
        FROM gtfs_work.runtime_route_estimates
        WHERE route_id = %s::uuid
        ORDER BY status, estimated_at DESC;
    """, (route_id,))
    return pd.DataFrame(rows)


@st.cache_data(ttl=30, show_spinner=False)
def _route_geom_polyline(route_id: str) -> List[Dict[str, float]]:
    rs = _fetch("""
        SELECT ST_AsGeoJSON(ST_Transform(geom,4326))::jsonb AS gj
        FROM route_prod.routes WHERE route_id = %s::uuid
    """, (route_id,))
    if not rs or not rs[0].get("gj"):
        return []
    coords = rs[0]["gj"].get("coordinates", [])
    return [{"lon": float(c[0]), "lat": float(c[1])} for c in coords]


# ──────────────────────────────────────────────────────────────────────────
# Stops Quality page
# ──────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _stop_quality_summary() -> Dict[str, int]:
    rows = _fetch("""
        WITH cohort_stops AS (
          SELECT DISTINCT unnest(stop_node_ids) AS node_id
          FROM route_prod.routes
          WHERE province='sample_region' AND deploy_status='active'
        )
        SELECT
          COUNT(*) AS total,
          COUNT(*) FILTER (WHERE n.name IS NULL OR LENGTH(TRIM(n.name))=0) AS empty,
          COUNT(*) FILTER (WHERE LOWER(TRIM(COALESCE(n.name,''))) IN
              ('parada','parada aislada','parada sin nombre','sin nombre','unnamed','unknown','na','sn')) AS placeholder,
          COUNT(*) FILTER (WHERE n.name LIKE 'Cerca de %%') AS predicted,
          COUNT(*) FILTER (WHERE n.superseded_by IS NOT NULL) AS superseded
        FROM cohort_stops cs
        JOIN node_prod.nodes n ON n.node_id = cs.node_id
        WHERE n.node_type='STOP';
    """)
    return rows[0] if rows else {}


@st.cache_data(ttl=60, show_spinner=False)
def _stops_with_issues_table(kind: str) -> pd.DataFrame:
    if kind == "empty":
        cond = "n.name IS NULL OR LENGTH(TRIM(n.name))=0"
    elif kind == "placeholder":
        cond = "LOWER(TRIM(COALESCE(n.name,''))) IN ('parada','parada aislada','parada sin nombre','sin nombre','unnamed','unknown','na','sn')"
    elif kind == "predicted":
        cond = "n.name LIKE 'Cerca de %%'"
    else:
        return pd.DataFrame()
    rows = _fetch(f"""
        WITH cohort_stops AS (
          SELECT DISTINCT unnest(stop_node_ids) AS node_id
          FROM route_prod.routes
          WHERE province='sample_region' AND deploy_status='active'
        )
        SELECT n.node_id::text, n.name,
               ROUND(ST_Y(n.geom)::numeric, 6) AS lat, ROUND(ST_X(n.geom)::numeric, 6) AS lon,
               n.confidence
        FROM cohort_stops cs JOIN node_prod.nodes n ON n.node_id = cs.node_id
        WHERE n.node_type='STOP' AND ({cond})
        LIMIT 500;
    """)
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────
# Route Inventory (all Sample Region routes + stops + agency)
# ──────────────────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def _inventory_table(
    canton: str | None,
    agency_substr: str | None,
    name_substr: str | None,
) -> pd.DataFrame:
    where = ["r.province='sample_region'"]
    params: list = []
    if name_substr:
        where.append("r.route_name ILIKE %s")
        params.append(f"%{name_substr}%")
    if agency_substr:
        where.append("COALESCE(rs.operator_name, cs.operator, ac.agency_name) ILIKE %s")
        params.append(f"%{agency_substr}%")
    if canton and canton != "Todos":
        where.append(f"{_UNIT_CASE_SQL} = %s")
        params.append(canton)

    rows = _fetch(f"""
        SELECT
          r.route_id::text                        AS route_id,
          r.route_name,
          r.direction_id,
          COALESCE(rs.operator_name, cs.operator) AS agency,
          ac.agency_name                          AS gtfs_agency,
          {_UNIT_CASE_SQL}                        AS canton,
          COALESCE(array_length(r.stop_node_ids, 1), 0) AS n_stops,
          ROUND((ST_Length(r.geom::geography)/1000.0)::numeric, 1) AS km,
          r.canonical_sequence_ready              AS canon_ready
        FROM route_prod.routes r
        LEFT JOIN route_prod.route_semantics rs   ON rs.route_id = r.route_id
        LEFT JOIN catalog.route_semantics cs      ON cs.route_id = r.route_id
        LEFT JOIN gtfs_work.route_agency_links ral ON ral.route_id = r.route_id
        LEFT JOIN gtfs_work.agency_catalog ac      ON ac.agency_id = ral.agency_id
        LEFT JOIN route_review.phase3_global_catalog_v1 v
               ON v.canonical_route_job_id::text = r.route_id::text
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(rs.operator_name, cs.operator, '') , r.route_name
    """, tuple(params))
    return pd.DataFrame(rows)


@st.cache_data(ttl=120, show_spinner=False)
def _distinct_cantons() -> List[str]:
    rows = _fetch(f"""
        SELECT {_UNIT_CASE_SQL} AS canton, COUNT(*) AS n
        FROM route_prod.routes r
        LEFT JOIN route_review.phase3_global_catalog_v1 v
               ON v.canonical_route_job_id::text = r.route_id::text
        WHERE r.province='sample_region'
        GROUP BY 1 ORDER BY n DESC;
    """)
    return [r["canton"] for r in rows if r.get("canton")]


@st.cache_data(ttl=120, show_spinner=False)
def _distinct_agencies() -> List[str]:
    rows = _fetch("""
        SELECT COALESCE(rs.operator_name, cs.operator) AS agency, COUNT(*) AS n
        FROM route_prod.routes r
        LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
        LEFT JOIN catalog.route_semantics cs    ON cs.route_id = r.route_id
        WHERE r.province='sample_region'
          AND COALESCE(rs.operator_name, cs.operator) IS NOT NULL
        GROUP BY 1 ORDER BY n DESC;
    """)
    return [r["agency"] for r in rows if r.get("agency")]


# ──────────────────────────────────────────────────────────────────────────
# View
# ──────────────────────────────────────────────────────────────────────────

def render_sample_region_audit_view(analytics: Any = None, audit: Any = None) -> None:  # noqa: ARG001
    st.title("Sample Region Audit Dashboard")
    st.caption("Phase-4 post-state, active routes of the region. Read-only.")

    try:
        kpis = _kpis()
    except (RuntimeError, psycopg2.Error) as exc:
        st.info(str(exc))
        st.caption("This read-only regional audit remains available when its local database is configured.")
        return

    # KPI strip
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total active", kpis["total"])
    c2.metric("Ship-eligible", kpis["ship_eligible"], help="clean + phase4_catalog_synthesized")
    c3.metric("Clean", kpis["clean"])
    c4.metric("Stop quality pending", kpis["stop_quality"])
    c5.metric("Stop duplicates", kpis["stop_dup"])
    c6.metric("Canon gap (P3)", kpis["canon_gap"])

    page = st.radio(
        "Section",
        options=["Route Inventory", "Routes", "Stops Quality", "Cohort Summary"],
        horizontal=True,
        label_visibility="collapsed",
    )

    if page == "Route Inventory":
        _render_route_inventory()
    elif page == "Cohort Summary":
        _render_cohort_summary()
    elif page == "Stops Quality":
        _render_stops_quality()
    else:
        _render_routes()


def _render_route_inventory() -> None:
    st.subheader("Route Inventory — Sample Region")

    # Filters
    f1, f2, f3 = st.columns(3)
    with f1:
        cantons = ["Todos"] + _distinct_cantons()
        canton = st.selectbox("Canton", cantons, index=0, key="inv.canton")
    with f2:
        agencies = ["Todos"] + _distinct_agencies()
        agency_pick = st.selectbox("Agency / Operator", agencies, index=0, key="inv.agency")
    with f3:
        name_q = st.text_input("Route name contains", value="", key="inv.name")

    agency_filter = None
    if agency_pick and agency_pick != "Todos":
        agency_filter = agency_pick

    df = _inventory_table(
        canton if canton != "Todos" else None,
        agency_filter,
        name_q.strip() or None,
    )

    if df.empty:
        st.info("No routes match these filters.")
        return

    # KPI row
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Routes", len(df))
    k2.metric("Agencies", df["agency"].nunique())
    k3.metric("Total stops", int(df["n_stops"].sum()))
    k4.metric("Avg stops/route", f"{df['n_stops'].mean():.1f}")

    st.dataframe(
        df[["route_name", "agency", "canton", "direction_id", "n_stops", "km", "canon_ready"]],
        use_container_width=True,
        hide_index=True,
        height=420,
        column_config={
            "route_name": st.column_config.TextColumn("Route", width="large"),
            "agency": st.column_config.TextColumn("Agency / Operator", width="medium"),
            "canton": st.column_config.TextColumn("Canton", width="medium"),
            "direction_id": st.column_config.NumberColumn("Dir", width="small"),
            "n_stops": st.column_config.NumberColumn("Stops", width="small"),
            "km": st.column_config.NumberColumn("km", width="small"),
            "canon_ready": st.column_config.CheckboxColumn("Canon", width="small"),
        },
    )

    # Drill-down: select a route to see its stops
    st.divider()
    st.subheader("Route stops")
    options = df["route_id"].tolist()
    labels = {
        r["route_id"]: f"{(r['route_name'] or '—')[:55]}  —  {r['agency'] or '?'}"
        for _, r in df.iterrows()
    }
    pick = st.selectbox(
        "Select a route",
        options=options,
        format_func=lambda rid: labels.get(rid, rid),
        key="inv.pick",
    )
    if pick:
        _render_inventory_drilldown(pick)


def _render_inventory_drilldown(route_id: str) -> None:
    sdf = _route_stops(route_id)
    if sdf.empty:
        st.info("No stops linked to this route.")
        return

    st.caption(f"{len(sdf)} stops")

    col_map, col_tbl = st.columns([0.5, 0.5])

    with col_tbl:
        st.dataframe(
            sdf[["seq", "name", "lat", "lon"]],
            use_container_width=True,
            hide_index=True,
            height=400,
        )

    with col_map:
        valid = sdf.dropna(subset=["lat", "lon"])
        if valid.empty:
            st.info("No coordinates available.")
        else:
            polyline = _route_geom_polyline(route_id)
            try:
                import pydeck as pdk
                lat0 = valid["lat"].astype(float).mean()
                lon0 = valid["lon"].astype(float).mean()
                layers = []
                if polyline:
                    layers.append(pdk.Layer(
                        "PathLayer",
                        data=[{"path": [[p["lon"], p["lat"]] for p in polyline]}],
                        get_path="path", get_color=[30, 144, 255],
                        width_scale=4, width_min_pixels=3,
                    ))
                stop_data = [
                    {"lat": float(r["lat"]), "lon": float(r["lon"]),
                     "seq": int(r["seq"]) if r.get("seq") is not None else 0,
                     "name": str(r.get("name") or "")}
                    for _, r in valid.iterrows()
                ]
                layers.append(pdk.Layer(
                    "ScatterplotLayer",
                    data=stop_data,
                    get_position="[lon, lat]",
                    get_radius=25,
                    get_fill_color=[255, 99, 71],
                    pickable=True,
                ))
                deck = pdk.Deck(
                    initial_view_state=pdk.ViewState(latitude=lat0, longitude=lon0, zoom=12),
                    layers=layers,
                    tooltip={"text": "{seq}. {name}"},
                    map_style=None,
                )
                st.pydeck_chart(deck, use_container_width=True)
            except Exception as exc:
                st.error(f"Map render failed: {exc}")


def _render_cohort_summary() -> None:
    st.subheader("Distribution by bucket")
    df = _bucket_breakdown()
    if df.empty:
        st.info("No data.")
        return
    col_a, col_b = st.columns([0.55, 0.45])
    with col_a:
        st.bar_chart(df.set_index("bucket")["n"])
    with col_b:
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("Ship-readiness")
    k = _kpis()
    blockers = {
        "Stop quality pending (Phase 1 stop names)": k["stop_quality"],
        "Stop duplicate residual (Phase 1 dedup)":   k["stop_dup"],
        "Canon gap (Phase 3 grounding)":             k["canon_gap"],
        "Unprocessed (Phase 1–3 not run)":           k["unprocessed"],
        "Unrealistic runtime (rare)":                k["runtime"],
    }
    st.dataframe(pd.DataFrame({"blocker": list(blockers), "routes": list(blockers.values())}),
                 use_container_width=True, hide_index=True)


def _render_routes() -> None:
    st.subheader("Routes")

    # Filters
    f1, f2, f3, f4, f5, f6 = st.columns([0.14, 0.18, 0.18, 0.16, 0.18, 0.16])
    with f1:
        status = st.selectbox("Status", ["All", "clean", "dirty", "unprocessed"], index=0)
    with f2:
        reasons = ["All", "(none)"] + _distinct_dirty_reasons()
        reason = st.selectbox("Dirty reason", reasons, index=0)
    with f3:
        units = ["All"] + _distinct_units()
        unit = st.selectbox("Unit / canton", units, index=0)
    with f4:
        operator = st.text_input("Operator contains", value="")
    with f5:
        name_q = st.text_input("Route name contains", value="")
    with f6:
        route_id_q = st.text_input("Route ID (exact)", value="")

    df = _routes_table(
        status, reason,
        operator.strip() or None,
        name_q.strip() or None,
        route_id_q.strip() or None,
        unit,
    )

    if df.empty:
        st.info("No routes match these filters.")
        return

    st.caption(f"{len(df)} route{'s' if len(df) != 1 else ''} matched (limit 1000).")
    st.dataframe(df, use_container_width=True, hide_index=True, height=320)

    # Drill-down
    st.divider()
    st.subheader("Drill-down")
    options = df["route_id"].tolist()
    labels = {r["route_id"]: f"{(r['route_name'] or '—')[:60]}  ({r['status']}/{r['reason'] or '—'})"
              for _, r in df.iterrows()}
    pick = st.selectbox("Pick a route", options=options, format_func=lambda rid: labels.get(rid, rid))
    if pick:
        _render_route_drilldown(pick)


def _render_route_drilldown(route_id: str) -> None:
    detail = _route_detail(route_id)
    if not detail:
        st.warning("Route not found.")
        return

    head_a, head_b, head_c, head_d, head_e = st.columns(5)
    head_a.metric("Status", str(detail.get("cleanliness_status") or "—"))
    head_b.metric("Reason", str(detail.get("dirty_reason") or "—"))
    nc = detail.get("naming_confidence")
    head_c.metric("Naming conf.", f"{float(nc):.2f}" if nc is not None else "—")
    head_d.metric("Stops", int(detail.get("n_stops") or 0))
    head_e.metric("Length (km)", f"{detail.get('km')}" if detail.get("km") is not None else "—")

    st.markdown(f"**Route**: {detail.get('route_name','')}  \n"
                f"**ID**: `{route_id}`  \n"
                f"**Canon ready**: {bool(detail.get('canonical_sequence_ready'))}  ·  "
                f"**Grandfathered**: {bool(detail.get('legacy_grandfathered'))}")

    tabs = st.tabs(["Map", "Stops", "Catalog", "Runtime", "Timeline"])

    # --- Map
    with tabs[0]:
        polyline = _route_geom_polyline(route_id)
        stops_df = _route_stops(route_id)
        if not polyline:
            st.info("No geometry for this route.")
        else:
            try:
                import pydeck as pdk
                lat0 = sum(p["lat"] for p in polyline) / len(polyline)
                lon0 = sum(p["lon"] for p in polyline) / len(polyline)
                line_layer = pdk.Layer(
                    "PathLayer", data=[{"path": [[p["lon"], p["lat"]] for p in polyline]}],
                    get_path="path", get_color=[30, 144, 255], width_scale=4,
                    width_min_pixels=3,
                )
                stop_layer = None
                if not stops_df.empty:
                    stop_data = stops_df.dropna(subset=["lat", "lon"]).to_dict("records")
                    stop_layer = pdk.Layer(
                        "ScatterplotLayer", data=stop_data,
                        get_position="[lon, lat]", get_radius=20,
                        get_fill_color=[255, 99, 71], pickable=True,
                    )
                layers = [line_layer] + ([stop_layer] if stop_layer else [])
                deck = pdk.Deck(
                    initial_view_state=pdk.ViewState(latitude=lat0, longitude=lon0, zoom=12),
                    layers=layers,
                    tooltip={"text": "{name}\nseq={seq}"} if stop_layer else None,
                    map_style=None,
                )
                st.pydeck_chart(deck, use_container_width=True)
            except Exception as exc:
                st.error(f"Map render failed: {exc}")

    # --- Stops
    with tabs[1]:
        sdf = _route_stops(route_id)
        if sdf.empty:
            st.info("No stops linked.")
        else:
            def flag(row):
                nm = (row.get("name") or "").strip()
                if not nm:
                    return "empty"
                low = nm.lower()
                if low in ("parada", "parada aislada", "parada sin nombre", "sin nombre", "unnamed", "unknown", "na", "sn"):
                    return "placeholder"
                if nm.startswith("Cerca de "):
                    return "predicted"
                return ""
            sdf = sdf.copy()
            sdf["flag"] = sdf.apply(flag, axis=1)
            st.dataframe(sdf, use_container_width=True, hide_index=True, height=420)

    # --- Catalog
    with tabs[2]:
        cat = _route_catalog(route_id)
        st.markdown("**catalog.route_semantics**")
        st.dataframe(pd.DataFrame(cat["semantics"]), use_container_width=True, hide_index=True)
        st.markdown("**catalog.route_schedule_profile**")
        st.dataframe(pd.DataFrame(cat["schedule"]), use_container_width=True, hide_index=True, height=180)
        st.markdown("**catalog.route_service_days**")
        st.dataframe(pd.DataFrame(cat["days"]), use_container_width=True, hide_index=True, height=160)
        st.markdown("**catalog.route_layover_policy**")
        st.dataframe(pd.DataFrame(cat["layover"]), use_container_width=True, hide_index=True)
        with st.expander(f"catalog.route_service_exceptions ({len(cat['exceptions'])} rows)"):
            st.dataframe(pd.DataFrame(cat["exceptions"]), use_container_width=True, hide_index=True)

    # --- Runtime
    with tabs[3]:
        rdf = _route_runtime(route_id)
        if rdf.empty:
            st.info("No runtime estimates for this route.")
        else:
            st.dataframe(rdf, use_container_width=True, hide_index=True)

    # --- Timeline
    with tabs[4]:
        st.markdown(
            f"- **created/updated**: `{detail.get('updated_at')}`  \n"
            f"- **semantics_updated_at**: `{detail.get('semantics_updated_at') or '—'}`  \n"
            f"- **cleanliness_classified_at**: `{detail.get('cleanliness_classified_at') or '—'}`"
        )


def _render_stops_quality() -> None:
    st.subheader("Cohort stop quality")
    s = _stop_quality_summary()
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total cohort stops", int(s.get("total") or 0))
    c2.metric("Empty / null", int(s.get("empty") or 0))
    c3.metric("Placeholder", int(s.get("placeholder") or 0))
    c4.metric("Predicted (Cerca de)", int(s.get("predicted") or 0))
    c5.metric("Superseded", int(s.get("superseded") or 0))

    kind = st.selectbox("Show", ["empty", "placeholder", "predicted"], index=0)
    df = _stops_with_issues_table(kind)
    if df.empty:
        st.info("No stops match.")
    else:
        st.caption(f"{len(df)} stops (limit 500)")
        st.dataframe(df, use_container_width=True, hide_index=True, height=520)
