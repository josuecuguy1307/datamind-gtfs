"""Phase 3 — GREEK Ship Overview tab.

Shows the full re_entry_queue status with semantics coverage,
a multi-route map of all shipped routes, and individual route
inspection with polyline + stops + semantic details.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import pydeck as pdk
import streamlit as st

from datamind_console.db.db import active_dsn, read_only_connection
from datamind_console.i18n import status_label, t


_STATUS_COLORS = {
    "swapped": [46, 204, 113],
    "v2_ready": [241, 196, 15],
    "failed": [231, 76, 60],
    "pending": [149, 165, 166],
}


def _dsn() -> str:
    """Canonical active DSN (honours local-only mode); never displayed."""
    return active_dsn()


@contextmanager
def _connect():
    """Short-lived READ ONLY connection, always closed (canonical helper)."""
    with read_only_connection(statement_timeout_ms=30_000) as conn:
        yield conn


@st.cache_data(ttl=60, show_spinner=False)
def _load_overview() -> pd.DataFrame:
    sql = """
      SELECT
        rq.route_id::text                          AS route_id,
        rq.status                                  AS greek_status,
        rq.classification                          AS classification,
        rq.last_error                              AS last_error,
        COALESCE(rs.route_name, r.route_name)      AS route_name,
        rs.operator_name                           AS operator,
        rs.naming_confidence                       AS naming_confidence,
        rs.human_verified                          AS human_verified,
        r.direction_id                             AS direction_id,
        r.province                                 AS province,
        r.chosen_geometry_candidate_id::text       AS geom_cid,
        r.chosen_stop_sequence_candidate_id::text  AS seq_cid,
        COALESCE(array_length(ssc.stop_node_ids, 1), 0) AS n_stops,
        ROUND(ST_Length(gc.geom::geography)::numeric)   AS length_m,
        cs.route_id IS NOT NULL                    AS has_catalog_sem,
        sp.route_id IS NOT NULL                    AS has_schedule,
        sd.route_id IS NOT NULL                    AS has_service_days,
        lp.route_id IS NOT NULL                    AS has_layover
      FROM route_prod.re_entry_queue rq
      JOIN route_prod.routes r ON r.route_id = rq.route_id
      LEFT JOIN route_prod.route_semantics rs ON rs.route_id = rq.route_id
      LEFT JOIN route_work.geometry_candidates gc
        ON gc.geometry_candidate_id = r.chosen_geometry_candidate_id
      LEFT JOIN route_work.stop_sequence_candidates ssc
        ON ssc.candidate_id = r.chosen_stop_sequence_candidate_id
      LEFT JOIN catalog.route_semantics cs ON cs.route_id = rq.route_id
      LEFT JOIN (SELECT DISTINCT route_id FROM catalog.route_schedule_profile) sp
        ON sp.route_id = rq.route_id
      LEFT JOIN (SELECT DISTINCT route_id FROM catalog.route_service_days) sd
        ON sd.route_id = rq.route_id
      LEFT JOIN catalog.route_layover_policy lp ON lp.route_id = rq.route_id
      ORDER BY rq.status, r.route_name
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    return pd.DataFrame([dict(r) for r in rows])


@st.cache_data(ttl=120, show_spinner=False)
def _load_all_polylines(route_ids: list[str]) -> pd.DataFrame:
    sql = """
      SELECT
        r.route_id::text AS route_id,
        ST_AsGeoJSON(gc.geom) AS gj
      FROM route_prod.routes r
      JOIN route_work.geometry_candidates gc
        ON gc.geometry_candidate_id = r.chosen_geometry_candidate_id
      WHERE r.route_id::text = ANY(%s)
        AND gc.geom IS NOT NULL
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (route_ids,))
        rows = cur.fetchall() or []
    results = []
    for row in rows:
        gj = json.loads(row["gj"])
        coords = gj.get("coordinates", [])
        if coords:
            results.append({
                "route_id": row["route_id"],
                "path": [[c[0], c[1]] for c in coords],
            })
    return pd.DataFrame(results)


def _load_route_polyline(geom_cid: str) -> Optional[list[list[float]]]:
    sql = """
      SELECT ST_AsGeoJSON(geom) AS gj
        FROM route_work.geometry_candidates
       WHERE geometry_candidate_id = %s::uuid
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (geom_cid,))
        row = cur.fetchone()
    if not row or not row.get("gj"):
        return None
    coords = json.loads(row["gj"]).get("coordinates") or []
    return [[c[0], c[1]] for c in coords]


def _load_stop_points(seq_cid: str) -> list[dict[str, Any]]:
    sql = """
      WITH ordered AS (
        SELECT sid.node_id AS node_id, sid.ord AS ord
          FROM route_work.stop_sequence_candidates ssc
          JOIN LATERAL unnest(ssc.stop_node_ids) WITH ORDINALITY AS sid(node_id, ord)
            ON TRUE
         WHERE ssc.candidate_id = %s::uuid
      )
      SELECT o.ord                AS seq,
             n.node_id::text       AS stop_id,
             COALESCE(NULLIF(BTRIM(n.name),''), 'STOP_'||substr(n.node_id::text,1,8)) AS stop_name,
             ST_Y(n.geom)::float   AS lat,
             ST_X(n.geom)::float   AS lon
        FROM ordered o
        JOIN node_prod.nodes n ON n.node_id = o.node_id
       ORDER BY o.ord
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (seq_cid,))
        return [dict(r) for r in (cur.fetchall() or [])]


def _render_overview_metrics(df: pd.DataFrame) -> None:
    shipped = len(df[df["greek_status"] == "swapped"])
    ready = len(df[df["greek_status"] == "v2_ready"])
    failed = len(df[df["greek_status"] == "failed"])
    total = len(df)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("ui.p3.greek_ship_overview.shipped"), shipped)
    c2.metric("v2_ready", ready)
    c3.metric(t("ui.p3.greek_ship_overview.failed"), failed)
    c4.metric(t("ui.p3.greek_ship_overview.total"), total)

    st.markdown(t("ui.p3.greek_ship_overview.semantics_coverage_all_routes"))
    sem_cols = st.columns(5)
    has_prod_sem = df["naming_confidence"].notna().sum()
    sem_cols[0].metric("route_prod.semantics", f"{int(has_prod_sem)}/{total}")
    sem_cols[1].metric("catalog.semantics", f"{int(df['has_catalog_sem'].sum())}/{total}")
    sem_cols[2].metric("schedule_profile", f"{int(df['has_schedule'].sum())}/{total}")
    sem_cols[3].metric("service_days", f"{int(df['has_service_days'].sum())}/{total}")
    sem_cols[4].metric("layover_policy", f"{int(df['has_layover'].sum())}/{total}")

    st.markdown(t("ui.p3.greek_ship_overview.naming_confidence_shipped_only"))
    shipped_df = df[df["greek_status"] == "swapped"]
    if not shipped_df.empty:
        bins = pd.cut(
            shipped_df["naming_confidence"].fillna(-1),
            bins=[-2, 0, 0.6, 0.8, 1.01],
            labels=["NONE", "LOW (<0.6)", "MEDIUM (0.6-0.8)", "HIGH (>=0.8)"],
        )
        conf_dist = bins.value_counts().sort_index()
        chart_df = conf_dist.reset_index()
        chart_df.columns = ["Confidence", "Count"]
        st.bar_chart(chart_df.set_index("Confidence"))


def _render_multi_route_map(df: pd.DataFrame) -> None:
    shipped_ids = df[df["greek_status"] == "swapped"]["route_id"].tolist()
    if not shipped_ids:
        st.info(t("ui.p3.greek_ship_overview.no_shipped_routes_display"))
        return

    with st.spinner(t("ui.p3.greek_ship_overview.loading_n_shipped_ids_polylines", n_shipped_ids=(len(shipped_ids)))):
        poly_df = _load_all_polylines(shipped_ids[:200])

    if poly_df.empty:
        st.warning(t("ui.p3.greek_ship_overview.no_polylines_found"))
        return

    layer = pdk.Layer(
        "PathLayer",
        data=poly_df.to_dict("records"),
        get_path="path",
        get_color=[46, 204, 113, 180],
        width_min_pixels=2,
        pickable=True,
    )

    all_lons = []
    all_lats = []
    for _, row in poly_df.iterrows():
        for pt in row["path"][:10]:
            all_lons.append(pt[0])
            all_lats.append(pt[1])

    view = pdk.ViewState(
        longitude=sum(all_lons) / len(all_lons) if all_lons else -78.49,
        latitude=sum(all_lats) / len(all_lats) if all_lats else -0.21,
        zoom=10,
    )

    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=view,
        tooltip={"text": "{route_id}"},
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    )
    st.pydeck_chart(deck, use_container_width=True, height=500)


def _render_route_inspector(df: pd.DataFrame) -> None:
    st.markdown(t("ui.p3.greek_ship_overview.route_inspector"))

    status_filter = st.selectbox(
        t("ui.p3.greek_ship_overview.filter_by_status"),
        options=["swapped", "v2_ready", "failed", "(all)"],
        format_func=lambda v: t("common.all") if v == "(all)" else status_label("reentry", v),
        key="gso.status_filter",
    )
    view = df if status_filter == "(all)" else df[df["greek_status"] == status_filter]

    q = st.text_input(t("ui.p3.greek_ship_overview.search_route_name_id"), key="gso.search").strip().lower()
    if q:
        mask = (
            view["route_name"].fillna("").str.lower().str.contains(q, regex=False)
            | view["route_id"].str.lower().str.contains(q, regex=False)
        )
        view = view[mask]

    if view.empty:
        st.info(t("ui.p3.greek_ship_overview.no_routes_match_filters"))
        return

    show_cols = [
        "route_name", "greek_status", "classification", "direction_id",
        "operator", "naming_confidence", "n_stops", "length_m",
        "has_catalog_sem", "has_schedule", "has_service_days", "has_layover",
        "route_id",
    ]
    show_cols = [c for c in show_cols if c in view.columns]
    st.dataframe(view[show_cols], use_container_width=True, hide_index=True, height=350)

    options = [
        f"{row['route_name'] or '(unnamed)'}  [{row['greek_status']}]  {row['route_id'][:8]}"
        for _, row in view.iterrows()
    ]
    idx = st.selectbox(
        t("ui.p3.greek_ship_overview.select_route_inspect"),
        options=range(len(options)),
        format_func=lambda i: options[i],
        key="gso.pick",
    )
    selected = view.iloc[idx]

    cols = st.columns([3, 2])
    with cols[0]:
        line = (
            _load_route_polyline(selected["geom_cid"])
            if selected.get("geom_cid")
            else None
        )
        stops = (
            _load_stop_points(selected["seq_cid"])
            if selected.get("seq_cid")
            else []
        )
        layers = []
        if line:
            layers.append(
                pdk.Layer(
                    "PathLayer",
                    data=[{"path": line}],
                    get_path="path",
                    get_color=[255, 90, 95],
                    width_min_pixels=3,
                )
            )
        if stops:
            layers.append(
                pdk.Layer(
                    "ScatterplotLayer",
                    data=stops,
                    get_position="[lon, lat]",
                    get_fill_color=[255, 180, 0, 220],
                    get_radius=18,
                    radius_min_pixels=4,
                    pickable=True,
                )
            )
        pts = list(line or []) + [[s["lon"], s["lat"]] for s in stops]
        if pts:
            lons = [p[0] for p in pts]
            lats = [p[1] for p in pts]
            vw = pdk.ViewState(
                longitude=sum(lons) / len(lons),
                latitude=sum(lats) / len(lats),
                zoom=12,
            )
        else:
            vw = pdk.ViewState(longitude=-78.49, latitude=-0.21, zoom=11)

        deck = pdk.Deck(
            layers=layers,
            initial_view_state=vw,
            tooltip={"text": "{seq}. {stop_name}"},
            map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        )
        st.pydeck_chart(deck, use_container_width=True)

    with cols[1]:
        st.markdown(f"**{selected['route_name'] or '(unnamed)'}**")
        st.caption(t("ui.p3.greek_ship_overview.route_id_dir_direction_id", route_id=(selected['route_id']), direction_id=(int(selected['direction_id']))))

        status_emoji = {"swapped": "OK", "v2_ready": "READY", "failed": "FAIL"}.get(
            selected["greek_status"], "?"
        )
        st.markdown(
            t("ui.p3.greek_ship_overview.greek_status_greek_status_status_emoji", greek_status=(selected['greek_status']), status_emoji=(status_emoji), classification=(selected.get('classification') or '-'), operator=(selected.get('operator') or '-'), naming_confidence=(selected.get('naming_confidence') or '-'), human_verified=(selected.get('human_verified') or False), n_stops=(int(selected.get('n_stops') or 0)), length_m=(int(selected.get('length_m') or 0)), province=(selected.get('province') or '-'))
        )

        st.markdown(t("ui.p3.greek_ship_overview.catalog_coverage"))
        sem_ok = "present" if selected.get("has_catalog_sem") else "MISSING"
        sch_ok = "present" if selected.get("has_schedule") else "MISSING"
        sd_ok = "present" if selected.get("has_service_days") else "MISSING"
        lp_ok = "present" if selected.get("has_layover") else "MISSING"
        st.markdown(
            t("ui.p3.greek_ship_overview.catalog_route_semantics_sem_ok_schedule", sem_ok=(sem_ok), sch_ok=(sch_ok), sd_ok=(sd_ok), lp_ok=(lp_ok))
        )

        if selected.get("last_error"):
            st.error(t("ui.p3.greek_ship_overview.last_error_last_error", last_error=(selected['last_error'])))

        if stops:
            st.markdown(t("ui.p3.greek_ship_overview.stops"))
            stops_df = pd.DataFrame(stops)[["seq", "stop_name", "stop_id"]]
            st.dataframe(stops_df, use_container_width=True, hide_index=True, height=280)


def render_greek_ship_overview_tab(**_: Any) -> None:
    st.markdown(t("ui.p3.greek_ship_overview.greek_ship_overview"))
    st.caption(
        t("ui.p3.greek_ship_overview.full_view_all_re_entry_queue")
    )

    df = _load_overview()
    if df.empty:
        st.info(t("ui.p3.greek_ship_overview.no_routes_re_entry_queue"))
        return

    _render_overview_metrics(df)

    st.markdown("---")
    st.markdown(t("ui.p3.greek_ship_overview.shipped_routes_map"))
    _render_multi_route_map(df)

    st.markdown("---")
    _render_route_inspector(df)
