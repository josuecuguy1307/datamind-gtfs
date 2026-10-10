"""Phase 3 — Greek-Validated Catalog tab.

Lists every route that has cleared the GREEK swap pipeline
(``route_prod.routes.last_swap_at IS NOT NULL``) with its canonical
polyline and stops as dictated by the entry section. Acts as the
operator-facing inventory of "trusted" routes — the ones that can
ship to production.

Polylines come from ``route_work.geometry_candidates.geom`` of the
``chosen_geometry_candidate_id``; stops come from
``route_work.stop_sequence_candidates.stop_node_ids`` of the
``chosen_stop_sequence_candidate_id``, joined to ``node_prod.nodes``
for current names + coordinates.
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
from datamind_console.i18n import t, option_label


_LINE_COLOR = [255, 90, 95]
_STOP_COLOR = [255, 180, 0, 220]


def _dsn() -> str:
    """Canonical active DSN (honours local-only mode); never displayed."""
    return active_dsn()


@contextmanager
def _connect():
    """Short-lived READ ONLY connection, always closed (canonical helper)."""
    with read_only_connection(statement_timeout_ms=30_000) as conn:
        yield conn


def _load_catalog() -> pd.DataFrame:
    sql = """
      SELECT
        r.route_id::text                    AS route_id,
        r.direction_id                      AS direction_id,
        r.route_name                        AS route_name,
        r.province                          AS province,
        r.deploy_status                     AS deploy_status,
        r.pipeline_version                  AS pipeline_version,
        r.last_swap_at                      AS last_swap_at,
        r.canonical_sequence_ready          AS canonical_ready,
        c.sector_label                      AS sector,
        c.service_route_operator            AS operator,
        gc.engine                           AS geom_engine,
        ROUND(ST_Length(gc.geom::geography)::numeric)        AS length_m,
        ST_NPoints(gc.geom)                                  AS shape_pts,
        COALESCE(array_length(ssc.stop_node_ids, 1), 0)      AS n_stops,
        aq.refill_applied                   AS refill_applied,
        aq.pre_ship_cleanup_applied         AS gamma_clean,
        r.chosen_geometry_candidate_id::text         AS geom_cid,
        r.chosen_stop_sequence_candidate_id::text    AS seq_cid
      FROM route_prod.routes r
      LEFT JOIN route_work.geometry_candidates gc
        ON gc.geometry_candidate_id = r.chosen_geometry_candidate_id
      LEFT JOIN route_work.stop_sequence_candidates ssc
        ON ssc.candidate_id = r.chosen_stop_sequence_candidate_id
      LEFT JOIN route_review.phase3_global_catalog_v1 c
        ON c.service_route_id = r.service_route_id::text
       AND c.direction_id     = r.direction_id
      LEFT JOIN route_prod.approval_queue aq
        ON aq.route_code = r.route_id::text
       AND aq.status     = 'approved'
      WHERE r.last_swap_at IS NOT NULL
      ORDER BY r.last_swap_at DESC
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []
    return pd.DataFrame([dict(r) for r in rows])


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


def _render_map(line: list[list[float]], stops: list[dict[str, Any]]) -> None:
    if not line and not stops:
        st.info(t("ui.p3.greek_validated_catalog.sin_geometria_ni_stops_para_esta"))
        return
    layers = []
    if line:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=[{"path": line}],
                get_path="path",
                get_color=_LINE_COLOR,
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
                get_fill_color=_STOP_COLOR,
                get_radius=18,
                radius_min_pixels=4,
                pickable=True,
            )
        )

    pts = list(line or []) + [[s["lon"], s["lat"]] for s in stops]
    if pts:
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        view = pdk.ViewState(
            longitude=sum(lons) / len(lons),
            latitude=sum(lats) / len(lats),
            zoom=12,
        )
    else:
        view = pdk.ViewState(longitude=-78.49, latitude=-0.21, zoom=11)

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view,
        tooltip={"text": "{seq}. {stop_name}"},
        map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
    )
    st.pydeck_chart(deck, use_container_width=True)


def render_greek_validated_catalog_tab(**_: Any) -> None:
    st.markdown(t("ui.p3.greek_validated_catalog.greek_validated_catalog"))
    st.caption(
        t("ui.p3.greek_validated_catalog.routes_that_cleared_greek_swap_pipeline")
    )

    df = _load_catalog()
    if df.empty:
        st.info(t("ui.p3.greek_validated_catalog.aun_no_hay_routes_con_last"))
        return

    # filters
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    with c1:
        province = st.selectbox(
            t("ui.p3.greek_validated_catalog.provincia"),
            options=["(all)"] + sorted([p for p in df["province"].dropna().unique()]),
            key="gvc.province",
            format_func=option_label(),
        )
    with c2:
        sectors = sorted([s for s in df["sector"].dropna().unique()])
        sector = st.selectbox(t("ui.p3.greek_validated_catalog.sector"), options=["(all)"] + sectors, key="gvc.sector", format_func=option_label())
    with c3:
        only_gamma = st.checkbox(t("ui.p3.greek_validated_catalog.solo_clean"), value=False, key="gvc.gamma")
    with c4:
        q = st.text_input(t("ui.p3.greek_validated_catalog.buscar_route_name_route_id"), key="gvc.q").strip().lower()

    view = df.copy()
    if province != "(all)":
        view = view[view["province"] == province]
    if sector != "(all)":
        view = view[view["sector"] == sector]
    if only_gamma:
        view = view[view["gamma_clean"] == True]
    if q:
        mask = (
            view["route_name"].fillna("").str.lower().str.contains(q, regex=False)
            | view["route_id"].str.lower().str.contains(q, regex=False)
        )
        view = view[mask]

    st.markdown(
        t("ui.p3.greek_validated_catalog.n_view_n_df_routes_clean", n_view=(len(view)), n_df=(len(df)), sum=(int(df['gamma_clean'].fillna(False).sum())), sum_2=(int(df['refill_applied'].fillna(False).sum())))
    )

    show_cols = [
        "route_name", "direction_id", "province", "sector", "operator",
        "n_stops", "length_m", "shape_pts", "geom_engine",
        "pipeline_version", "gamma_clean", "refill_applied", "last_swap_at",
        "route_id",
    ]
    show_cols = [c for c in show_cols if c in view.columns]
    st.dataframe(
        view[show_cols],
        use_container_width=True,
        hide_index=True,
        height=420,
    )

    st.markdown(t("ui.p3.greek_validated_catalog.inspeccion_de_ruta"))
    if view.empty:
        st.info(t("ui.p3.greek_validated_catalog.no_hay_routes_con_los_filtros"))
        return

    # picker
    options = [
        f"{(row['route_name'] or t('common.unnamed'))[:60]} · d{int(row['direction_id'])} · {row['route_id'][:8]}"
        for _, row in view.iterrows()
    ]
    idx = st.selectbox(
        t("ui.p3.greek_validated_catalog.selecciona_una_ruta_para_ver_polyline"),
        options=range(len(options)),
        format_func=lambda i: options[i],
        key="gvc.pick",
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
        _render_map(line or [], stops)

    with cols[1]:
        st.markdown(f"**{selected['route_name'] or t('common.unnamed')}**")
        st.caption(t("ui.p3.greek_validated_catalog.route_id_dir_direction_id", route_id=(selected['route_id']), direction_id=(int(selected['direction_id']))))
        st.markdown(
            t("ui.p3.greek_validated_catalog.operador_operator_sector_sector_provincia_province", operator=(selected.get('operator') or '—'), sector=(selected.get('sector') or '—'), province=(selected.get('province') or '—'), pipeline_version=(selected.get('pipeline_version') or '—'), geom_engine=(selected.get('geom_engine') or '—'), n_stops=(int(selected.get('n_stops') or 0)), length_m=(int(selected.get('length_m') or 0)), shape_pts=(int(selected.get('shape_pts') or 0)), v17=('✅' if selected.get('gamma_clean') else '—'), last_swap_at=(selected.get('last_swap_at')))
        )

        if stops:
            st.markdown(t("ui.p3.greek_validated_catalog.paradas_en_orden"))
            stops_df = pd.DataFrame(stops)[["seq", "stop_name", "stop_id"]]
            st.dataframe(stops_df, use_container_width=True, hide_index=True, height=320)
        else:
            st.warning(t("ui.p3.greek_validated_catalog.sin_stops_para_esta_ruta"))
