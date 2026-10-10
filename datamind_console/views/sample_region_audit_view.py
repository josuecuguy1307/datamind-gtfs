"""Sample Region route audit.

Read-only audit surface for the Phase-4 cleanliness re-classification of
Sample Region route directions, filterable by cleanliness, reason, operator,
canton and name, with per-route drill-down (map, stops, catalog rows,
runtime, timeline).

Route counts come from ``datamind_console.metrics.routes`` (one definition
per number, see docs/METRICS.md). The old hard-coded "646 active routes"
caption was the 2026-05-02 count, before the 2026-05-04 purge.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
import streamlit as st

from datamind_console.db.db import query_all
from datamind_console.i18n import status_label, t
from datamind_console.metrics import routes as route_metrics
from datamind_console.ui.components.kpi import Kpi, kpi_row
from datamind_console.ui.components.page import page_header, section
from datamind_console.ui.components.states import empty_state, error_state, loading
from datamind_console.ui.components.status import Chip, status_badge
from datamind_console.ui.formatting import format_date, format_number
from datamind_console.ui.theme import palette


# Maps the messy phase3_global_catalog_v1.sector_key into a canton/sub-sector
# bucket the operator can filter by. Unassigned rows use the sentinel
# '__unassigned__', translated at display time. Inline as a SQL fragment so we can use it
# both in SELECT and in WHERE without repeating logic.
_UNIT_CASE_SQL = """
  CASE
    WHEN v.sector_key IS NULL OR v.sector_key='unassigned' THEN '__unassigned__'
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
    ELSE COALESCE(v.sector_key, '__unassigned__')
  END
"""


# ──────────────────────────────────────────────────────────────────────────
# DB
# ──────────────────────────────────────────────────────────────────────────

def _fetch(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """One read-only query via the canonical helper.

    This used to run on a process-wide ``st.cache_resource`` psycopg2
    connection that never committed, so after the first query it stayed
    "idle in transaction" for the server's lifetime. It held locks on
    ``route_prod.routes`` (blocking ALTER/migrations), and after any SQL
    error every later query failed with "current transaction is aborted".
    Each call now opens a connection, runs READ ONLY, ends the transaction
    and closes it.
    """
    return query_all(sql, params, statement_timeout_ms=30_000)


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


def _distinct_stop_count(df: pd.DataFrame) -> int:
    """Distinct stop nodes across the listed route directions (not references)."""
    if "stop_ids" not in df:
        return 0
    return len({sid for ids in df["stop_ids"] if ids for sid in ids})


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
    deploy_scope: str = "all",
) -> pd.DataFrame:
    where = ["r.province='sample_region'"]
    params: list = []
    if deploy_scope == "deployed":
        where.append("r.deploy_status = 'active'")
    elif deploy_scope == "not_deployed":
        where.append("r.deploy_status IS DISTINCT FROM 'active'")
    if name_substr:
        where.append("r.route_name ILIKE %s")
        params.append(f"%{name_substr}%")
    if agency_substr:
        where.append("COALESCE(rs.operator_name, cs.operator, ac.agency_name) ILIKE %s")
        params.append(f"%{agency_substr}%")
    if canton:
        where.append(f"{_UNIT_CASE_SQL} = %s")
        params.append(canton)

    rows = _fetch(f"""
        SELECT
          r.route_id::text                        AS route_id,
          r.route_name,
          r.direction_id,
          r.deploy_status,
          r.cleanliness_status,
          COALESCE(rs.operator_name, cs.operator) AS agency,
          ac.agency_name                          AS gtfs_agency,
          {_UNIT_CASE_SQL}                        AS canton,
          COALESCE(array_length(r.stop_node_ids, 1), 0) AS n_stops,
          r.stop_node_ids::text[]                 AS stop_ids,
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

_ALL = "All"            # internal filter sentinel used by _routes_table
_NO_REASON = "(none)"   # internal filter sentinel used by _routes_table
_SECTIONS = ("inventory", "routes", "stops", "cohort")


@st.cache_data(ttl=60, show_spinner=False)
def _canonical_counts() -> Dict[str, int]:
    return route_metrics.route_counts("sample_region")


def _unit_label(value: Any) -> str:
    if value in (None, "", "__unassigned__"):
        return t("audit.unit.unassigned")
    return str(value)


def _route_map(polyline: List[Dict[str, float]], stops: List[Dict[str, Any]]) -> None:
    """Route polyline + numbered stops on the themed basemap."""
    import pydeck as pdk

    pal = palette()
    points = polyline or [{"lat": float(r["lat"]), "lon": float(r["lon"])} for r in stops]
    if not points:
        empty_state("audit.drill.no_geometry")
        return
    lat0 = sum(p_["lat"] for p_ in points) / len(points)
    lon0 = sum(p_["lon"] for p_ in points) / len(points)
    layers = []
    if polyline:
        layers.append(pdk.Layer(
            "PathLayer",
            data=[{"path": [[p_["lon"], p_["lat"]] for p_ in polyline]}],
            get_path="path", get_color=pal.rgba(pal.info, 230), width_scale=4, width_min_pixels=3,
        ))
    if stops:
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=stops,
            get_position="[lon, lat]",
            get_radius=22,
            get_fill_color=pal.rgba(pal.accent, 235),
            get_line_color=pal.rgba(pal.background, 255),
            line_width_min_pixels=1,
            stroked=True,
            pickable=True,
        ))
    deck = pdk.Deck(
        initial_view_state=pdk.ViewState(latitude=lat0, longitude=lon0, zoom=12),
        layers=layers,
        tooltip={"text": "{seq}. {name}"} if stops else None,
        map_style=pal.map_style,
    )
    st.pydeck_chart(deck, height=440)


def render_sample_region_audit_view(analytics: Any = None, audit: Any = None) -> None:  # noqa: ARG001
    page_header(
        t("page.audit.title"),
        t("page.audit.subtitle"),
        eyebrow=t("page.audit.eyebrow"),
        chips=[Chip(t("audit.read_only"), tone="info", icon=":material/visibility:")],
    )

    try:
        with loading("audit.loading"):
            counts = _canonical_counts()
            kpis = _kpis()
    except Exception as exc:  # noqa: BLE001 - rendered and logged
        error_state(exc, context_key="error.page.audit", key="audit.retry")
        return

    kpi_row([
        Kpi(t("metric.route_directions_deployed.label"), counts["route_directions_deployed"], help=t("metric.route_directions_deployed.help")),
        Kpi(t("metric.route_directions_clean.label"), counts["route_directions_clean"], help=t("metric.route_directions_clean.help")),
        Kpi(t("metric.route_directions_ship_eligible.label"), counts["route_directions_ship_eligible"], help=t("metric.route_directions_ship_eligible.help")),
        Kpi(t("metric.route_directions_needs_work.label"), counts["route_directions_needs_work"], help=t("metric.route_directions_needs_work.help")),
        Kpi(t("metric.route_directions_not_deployed.label"), counts["route_directions_not_deployed"], help=t("metric.route_directions_not_deployed.help")),
        Kpi(t("metric.lines_clean.label"), counts["lines_clean"], help=t("metric.lines_clean.help")),
    ])
    st.caption(t("audit.scope_note", total=format_number(counts["route_directions_total"]),
                 deployed=format_number(counts["route_directions_deployed"]),
                 not_deployed=format_number(counts["route_directions_not_deployed"])))
    if kpis["canon_gap"] != counts["route_directions_needs_work"]:
        st.caption(t("audit.reason_note", canon_gap=format_number(kpis["canon_gap"])))

    page = st.segmented_control(
        t("audit.section.label"),
        options=list(_SECTIONS),
        default=st.session_state.get("audit.section") or "inventory",
        format_func=lambda key: t(f"audit.section.{key}"),
        key="audit.section",
        label_visibility="collapsed",
    ) or "inventory"

    if page == "inventory":
        _render_route_inventory()
    elif page == "cohort":
        _render_cohort_summary(kpis)
    elif page == "stops":
        _render_stops_quality()
    else:
        _render_routes()


def _render_route_inventory() -> None:
    section(t("audit.inventory.title"), t("audit.inventory.caption"))

    f1, f2, f3, f4 = st.columns([0.24, 0.26, 0.26, 0.24])
    with f1:
        canton = st.selectbox(
            t("audit.filter.canton"), [""] + _distinct_cantons(), index=0, key="inv.canton",
            format_func=lambda v: t("filter.all") if v == "" else _unit_label(v),
        )
    with f2:
        agency_pick = st.selectbox(
            t("audit.filter.agency"), [""] + _distinct_agencies(), index=0, key="inv.agency",
            format_func=lambda v: t("filter.all") if v == "" else v,
        )
    with f3:
        name_q = st.text_input(t("audit.filter.name"), value="", key="inv.name")
    with f4:
        deploy_scope = st.selectbox(
            t("audit.filter.deploy_scope"), ["all", "deployed", "not_deployed"], index=0, key="inv.deploy_scope",
            format_func=lambda v: t(f"audit.deploy_scope.{v}"),
        )

    df = _inventory_table(canton or None, agency_pick or None, name_q.strip() or None, deploy_scope)
    if df.empty:
        empty_state("audit.no_match")
        return

    kpi_row([
        Kpi(t("audit.inventory.kpi.directions"), len(df), help=t("audit.inventory.kpi.directions_help")),
        Kpi(t("audit.inventory.kpi.agencies"), int(df["agency"].nunique())),
        Kpi(t("audit.inventory.kpi.stop_references"), int(df["n_stops"].sum()), help=t("audit.inventory.kpi.stop_references_help")),
        Kpi(t("audit.inventory.kpi.distinct_stops"), _distinct_stop_count(df), help=t("audit.inventory.kpi.distinct_stops_help")),
        Kpi(t("audit.inventory.kpi.avg_stops"), format_number(float(df["n_stops"].mean()), decimals=1)),
    ])

    view = df.copy()
    view["canton"] = view["canton"].map(_unit_label)
    view["deploy_status"] = view["deploy_status"].map(lambda v: status_label("deploy", v))
    view["cleanliness_status"] = view["cleanliness_status"].map(lambda v: status_label("cleanliness", v))
    st.dataframe(
        view[["route_name", "agency", "canton", "direction_id", "deploy_status", "cleanliness_status", "n_stops", "km", "canon_ready"]],
        hide_index=True,
        height=420,
        width="stretch",
        column_config={
            "route_name": st.column_config.TextColumn(t("col.route"), width="medium"),
            "agency": st.column_config.TextColumn(t("col.operator"), width="medium"),
            "canton": st.column_config.TextColumn(t("col.canton"), width="medium"),
            "direction_id": st.column_config.NumberColumn(t("col.direction"), width="small"),
            "deploy_status": st.column_config.TextColumn(t("col.deploy_status"), width="medium"),
            "cleanliness_status": st.column_config.TextColumn(t("col.cleanliness"), width="medium"),
            "n_stops": st.column_config.NumberColumn(t("col.stops"), width="small"),
            "km": st.column_config.NumberColumn(t("col.km"), width="small"),
            "canon_ready": st.column_config.CheckboxColumn(t("col.canonical_sequence"), width="small", help=t("col.canonical_sequence_help")),
        },
    )

    section(t("audit.inventory.stops_title"), divider=True)
    options = df["route_id"].tolist()
    labels = {r["route_id"]: f"{(r['route_name'] or '—')[:55]}  —  {r['agency'] or '?'}" for _, r in df.iterrows()}
    pick = st.selectbox(t("audit.pick_route"), options=options, format_func=lambda rid: labels.get(rid, rid), key="inv.pick")
    if pick:
        _render_inventory_drilldown(pick)


def _render_inventory_drilldown(route_id: str) -> None:
    sdf = _route_stops(route_id)
    if sdf.empty:
        empty_state("audit.drill.no_stops")
        return
    st.caption(t("audit.drill.stop_count", n=format_number(len(sdf))))
    col_map, col_tbl = st.columns([0.5, 0.5])
    with col_tbl:
        st.dataframe(
            sdf[["seq", "name", "lat", "lon"]],
            hide_index=True, height=440, width="stretch",
            column_config={
                "seq": st.column_config.NumberColumn(t("col.seq"), width="small"),
                "name": st.column_config.TextColumn(t("col.stop_name")),
                "lat": st.column_config.NumberColumn(t("col.lat"), format="%.6f"),
                "lon": st.column_config.NumberColumn(t("col.lon"), format="%.6f"),
            },
        )
    with col_map:
        valid = sdf.dropna(subset=["lat", "lon"])
        stops = [
            {"lat": float(r["lat"]), "lon": float(r["lon"]), "seq": int(r["seq"]) if r.get("seq") is not None else 0, "name": str(r.get("name") or "")}
            for _, r in valid.iterrows()
        ]
        try:
            _route_map(_route_geom_polyline(route_id), stops)
        except Exception as exc:  # noqa: BLE001 - rendered and logged
            error_state(exc, context_key="audit.drill.map_failed", retry=False)


def _render_cohort_summary(kpis: Dict[str, int]) -> None:
    section(t("audit.cohort.title"), t("audit.cohort.caption"))
    df = _bucket_breakdown()
    if df.empty:
        empty_state("audit.no_data")
        return

    def _bucket_label(bucket: str) -> str:
        if bucket.startswith("dirty/"):
            return f"{status_label('cleanliness', 'dirty')} · {status_label('dirty_reason', bucket.split('/', 1)[1])}"
        return status_label("cleanliness", bucket)

    shown = pd.DataFrame({t("col.bucket"): df["bucket"].map(_bucket_label), t("col.route_directions"): df["n"]})
    col_a, col_b = st.columns([0.55, 0.45])
    with col_a:
        st.bar_chart(shown.set_index(t("col.bucket"))[t("col.route_directions")], horizontal=True)
    with col_b:
        st.dataframe(shown, hide_index=True, width="stretch")

    section(t("audit.cohort.blockers_title"), t("audit.cohort.blockers_caption"))
    blockers = [
        ("stop_quality_pending", kpis["stop_quality"]),
        ("stop_duplicate_residual", kpis["stop_dup"]),
        ("canon_gap", kpis["canon_gap"]),
        ("unprocessed", kpis["unprocessed"]),
        ("unrealistic_runtime", kpis["runtime"]),
    ]
    st.dataframe(
        pd.DataFrame({
            t("col.blocker"): [t(f"audit.blocker.{key}") for key, _ in blockers],
            t("col.route_directions"): [n for _, n in blockers],
        }),
        hide_index=True,
        width="stretch",
    )


def _render_routes() -> None:
    section(t("audit.routes.title"), t("audit.routes.caption"))
    f1, f2, f3, f4, f5, f6 = st.columns([0.14, 0.18, 0.18, 0.16, 0.18, 0.16])
    with f1:
        status = st.selectbox(t("audit.filter.status"), [_ALL, "clean", "dirty", "unprocessed"], index=0,
                              format_func=lambda v: t("filter.all") if v == _ALL else status_label("cleanliness", v))
    with f2:
        reasons = [_ALL, _NO_REASON] + _distinct_dirty_reasons()
        reason = st.selectbox(t("audit.filter.reason"), reasons, index=0,
                              format_func=lambda v: t("filter.all") if v == _ALL else (t("filter.none") if v == _NO_REASON else status_label("dirty_reason", v)))
    with f3:
        units = [_ALL] + _distinct_units()
        unit = st.selectbox(t("audit.filter.unit"), units, index=0,
                            format_func=lambda v: t("filter.all") if v == _ALL else _unit_label(v))
    with f4:
        operator = st.text_input(t("audit.filter.operator"), value="")
    with f5:
        name_q = st.text_input(t("audit.filter.name"), value="")
    with f6:
        route_id_q = st.text_input(t("audit.filter.route_id"), value="")

    df = _routes_table(status, reason, operator.strip() or None, name_q.strip() or None, route_id_q.strip() or None, unit)
    if df.empty:
        empty_state("audit.no_match")
        return

    st.caption(t("audit.routes.matched", n=format_number(len(df))))
    view = df.copy()
    view["status"] = view["status"].map(lambda v: status_label("cleanliness", v))
    view["reason"] = view["reason"].map(lambda v: status_label("dirty_reason", v) if v else "—")
    view["unit"] = view["unit"].map(_unit_label)
    st.dataframe(
        view,
        hide_index=True, height=320, width="stretch",
        column_config={
            "route_id": None,
            "route_name": st.column_config.TextColumn(t("col.route"), width="large"),
            "status": st.column_config.TextColumn(t("col.cleanliness")),
            "reason": st.column_config.TextColumn(t("col.reason")),
            "conf": st.column_config.NumberColumn(t("col.naming_confidence"), format="%.2f", help=t("col.naming_confidence_help")),
            "operator": st.column_config.TextColumn(t("col.operator")),
            "short": st.column_config.TextColumn(t("col.short_name")),
            "unit": st.column_config.TextColumn(t("col.canton")),
            "km": st.column_config.NumberColumn(t("col.km")),
            "n_stops": st.column_config.NumberColumn(t("col.stops")),
        },
    )

    section(t("audit.drill.title"), divider=True)
    options = df["route_id"].tolist()
    labels = {r["route_id"]: f"{(r['route_name'] or '—')[:60]}  ({status_label('cleanliness', r['status'])})" for _, r in df.iterrows()}
    pick = st.selectbox(t("audit.pick_route"), options=options, format_func=lambda rid: labels.get(rid, rid))
    if pick:
        _render_route_drilldown(pick)


def _render_route_drilldown(route_id: str) -> None:
    detail = _route_detail(route_id)
    if not detail:
        empty_state("audit.drill.not_found")
        return

    with st.container(horizontal=True, gap="small"):
        status_badge("cleanliness", detail.get("cleanliness_status"))
        if detail.get("dirty_reason"):
            status_badge("dirty_reason", detail.get("dirty_reason"))
        if detail.get("canonical_sequence_ready"):
            st.badge(t("audit.drill.canonical_ready"), color="green", icon=":material/check:")
        if detail.get("legacy_grandfathered"):
            st.badge(t("audit.drill.grandfathered"), color="gray")
    nc = detail.get("naming_confidence")
    kpi_row([
        Kpi(t("col.naming_confidence"), format_number(float(nc), decimals=2) if nc is not None else "—", help=t("col.naming_confidence_help")),
        Kpi(t("col.stops"), int(detail.get("n_stops") or 0)),
        Kpi(t("audit.drill.length_km"), format_number(float(detail["km"]), decimals=1) if detail.get("km") is not None else "—"),
        Kpi(t("col.direction"), detail.get("direction_id") if detail.get("direction_id") is not None else "—"),
    ])
    st.markdown(f"**{detail.get('route_name') or '—'}**")
    st.caption(t("audit.drill.route_id", route_id=route_id))

    tabs = st.tabs([t("audit.drill.tab.map"), t("audit.drill.tab.stops"), t("audit.drill.tab.catalog"),
                    t("audit.drill.tab.runtime"), t("audit.drill.tab.timeline")])

    with tabs[0]:
        stops_df = _route_stops(route_id)
        stops = stops_df.dropna(subset=["lat", "lon"]).to_dict("records") if not stops_df.empty else []
        try:
            _route_map(_route_geom_polyline(route_id), stops)
        except Exception as exc:  # noqa: BLE001 - rendered and logged
            error_state(exc, context_key="audit.drill.map_failed", retry=False)

    with tabs[1]:
        sdf = _route_stops(route_id)
        if sdf.empty:
            empty_state("audit.drill.no_stops")
        else:
            def flag(row):
                nm = (row.get("name") or "").strip()
                if not nm:
                    return t("audit.stop_flag.empty")
                low = nm.lower()
                if low in ("parada", "parada aislada", "parada sin nombre", "sin nombre", "unnamed", "unknown", "na", "sn"):
                    return t("audit.stop_flag.placeholder")
                if nm.startswith("Cerca de "):
                    return t("audit.stop_flag.predicted")
                return ""
            sdf = sdf.copy()
            sdf["flag"] = sdf.apply(flag, axis=1)
            st.dataframe(sdf, hide_index=True, height=420, width="stretch",
                         column_config={"flag": st.column_config.TextColumn(t("col.name_quality"))})

    with tabs[2]:
        cat = _route_catalog(route_id)
        st.caption(t("audit.drill.catalog_caption"))
        for title_key, rows, height in (
            ("audit.catalog.semantics", cat["semantics"], None),
            ("audit.catalog.schedule", cat["schedule"], 180),
            ("audit.catalog.service_days", cat["days"], 160),
            ("audit.catalog.layover", cat["layover"], None),
        ):
            st.markdown(f"**{t(title_key)}**")
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", **({"height": height} if height else {}))
            else:
                st.caption(t("audit.catalog.missing"))
        with st.expander(t("audit.catalog.exceptions", n=len(cat["exceptions"]))):
            st.dataframe(pd.DataFrame(cat["exceptions"]), hide_index=True, width="stretch")

    with tabs[3]:
        rdf = _route_runtime(route_id)
        if rdf.empty:
            empty_state("audit.drill.no_runtime")
        else:
            st.dataframe(rdf, hide_index=True, width="stretch")

    with tabs[4]:
        st.dataframe(
            pd.DataFrame([
                {t("col.event"): t("audit.timeline.updated"), t("col.when"): format_date(detail.get("updated_at"), with_time=True)},
                {t("col.event"): t("audit.timeline.semantics"), t("col.when"): format_date(detail.get("semantics_updated_at"), with_time=True)},
                {t("col.event"): t("audit.timeline.classified"), t("col.when"): format_date(detail.get("cleanliness_classified_at"), with_time=True)},
            ]),
            hide_index=True, width="stretch",
        )
        st.caption(t("audit.timeline.note"))


def _render_stops_quality() -> None:
    section(t("audit.stops.title"), t("audit.stops.caption"))
    s = _stop_quality_summary()
    kpi_row([
        Kpi(t("audit.stops.total"), int(s.get("total") or 0), help=t("audit.stops.total_help")),
        Kpi(t("audit.stop_flag.empty"), int(s.get("empty") or 0)),
        Kpi(t("audit.stop_flag.placeholder"), int(s.get("placeholder") or 0)),
        Kpi(t("audit.stop_flag.predicted"), int(s.get("predicted") or 0), help=t("audit.stops.predicted_help")),
        Kpi(t("audit.stops.superseded"), int(s.get("superseded") or 0)),
    ])
    kind = st.selectbox(t("audit.stops.show"), ["empty", "placeholder", "predicted"], index=0,
                        format_func=lambda k: t(f"audit.stop_flag.{k}"))
    df = _stops_with_issues_table(kind)
    if df.empty:
        empty_state("audit.stops.none")
    else:
        st.caption(t("audit.stops.listed", n=format_number(len(df))))
        st.dataframe(df, hide_index=True, height=520, width="stretch")
