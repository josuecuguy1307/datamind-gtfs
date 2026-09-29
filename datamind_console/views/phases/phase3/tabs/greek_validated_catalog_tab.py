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

import json
import os
from typing import Any, Optional

import pandas as pd
import psycopg2
import psycopg2.extras
import pydeck as pdk
import streamlit as st


_LINE_COLOR = [255, 90, 95]
_STOP_COLOR = [255, 180, 0, 220]


def _dsn() -> str:
    value = os.environ.get("DB_DSN") or os.environ.get("LOCAL_DB_DSN")
    if not value:
        raise RuntimeError("DB_DSN or LOCAL_DB_DSN must be configured before loading catalog data.")
    return value


def _connect():
    return psycopg2.connect(_dsn())


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
        st.info("Sin geometría ni stops para esta ruta.")
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
    st.markdown("### GREEK-Validated Catalog")
    st.caption(
        "Routes that cleared the GREEK swap pipeline "
        "(`route_prod.routes.last_swap_at IS NOT NULL`). Polylines and "
        "stops shown are the canonical ones dictated by the Phase 3 "
        "entry section."
    )

    df = _load_catalog()
    if df.empty:
        st.info("Aún no hay routes con `last_swap_at` set.")
        return

    # filters
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    with c1:
        province = st.selectbox(
            "Provincia",
            options=["(all)"] + sorted([p for p in df["province"].dropna().unique()]),
            key="gvc.province",
        )
    with c2:
        sectors = sorted([s for s in df["sector"].dropna().unique()])
        sector = st.selectbox("Sector", options=["(all)"] + sectors, key="gvc.sector")
    with c3:
        only_gamma = st.checkbox("Sólo γ-clean", value=False, key="gvc.gamma")
    with c4:
        q = st.text_input("Buscar (route_name / route_id)", key="gvc.q").strip().lower()

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
        f"**{len(view)} / {len(df)}** routes. "
        f"γ-clean: **{int(df['gamma_clean'].fillna(False).sum())}** · "
        f"refill_applied: **{int(df['refill_applied'].fillna(False).sum())}**"
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

    st.markdown("#### Inspección de ruta")
    if view.empty:
        st.info("No hay routes con los filtros actuales.")
        return

    # picker
    options = [
        f"{(row['route_name'] or '(sin nombre)')[:60]} · d{int(row['direction_id'])} · {row['route_id'][:8]}"
        for _, row in view.iterrows()
    ]
    idx = st.selectbox(
        "Seleccioná una ruta para ver polyline + stops",
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
        st.markdown(f"**{selected['route_name'] or '(sin nombre)'}**")
        st.caption(f"`{selected['route_id']}` · dir `{int(selected['direction_id'])}`")
        st.markdown(
            f"- **Operador:** {selected.get('operator') or '—'}\n"
            f"- **Sector:** {selected.get('sector') or '—'}\n"
            f"- **Provincia:** {selected.get('province') or '—'}\n"
            f"- **Pipeline:** `{selected.get('pipeline_version') or '—'}`\n"
            f"- **Engine geom:** `{selected.get('geom_engine') or '—'}`\n"
            f"- **Stops:** {int(selected.get('n_stops') or 0)}\n"
            f"- **Length:** {int(selected.get('length_m') or 0)} m\n"
            f"- **Shape pts:** {int(selected.get('shape_pts') or 0)}\n"
            f"- **γ-clean:** {'✅' if selected.get('gamma_clean') else '—'}\n"
            f"- **Last swap:** {selected.get('last_swap_at')}"
        )

        if stops:
            st.markdown("##### Paradas (en orden)")
            stops_df = pd.DataFrame(stops)[["seq", "stop_name", "stop_id"]]
            st.dataframe(stops_df, use_container_width=True, hide_index=True, height=320)
        else:
            st.warning("Sin stops para esta ruta.")
