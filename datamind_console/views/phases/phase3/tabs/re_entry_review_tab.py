"""Phase 3 — Re-Entry Review tab.

Streamlit panel for the Control Tower that lists pending
``route_prod.approval_queue`` rows carrying a
``policy_flags.re_entry_queue_id``, renders the v1 route and the v2
proposal side-by-side on a pydeck map, and exposes the three
swap-service actions: APPROVE, REJECT, QUARANTINE.

This is Prompt 8 Commit 7b — the operator-facing half of the
re-entry pipeline. The v1-vs-v2 render relies on the geometry +
stops already on the row; no extra RPC calls are made at click
time except the swap-service action itself.
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

from hades.enforcers import re_entry_swap_service as swap_svc


_FIX_CATEGORY_COLORS = {
    "geometry": [255, 90, 95],
    "stop_coverage": [90, 180, 255],
    "structural": [220, 150, 255],
}

_V1_COLOR = [100, 100, 100]
_V2_COLOR = [255, 90, 95]
_V1_STOP_COLOR = [90, 90, 90, 200]
_V2_STOP_COLOR = [255, 180, 0, 220]


def _dsn() -> str:
    value = os.environ.get("DB_DSN") or os.environ.get("LOCAL_DB_DSN")
    if not value:
        raise RuntimeError("DB_DSN or LOCAL_DB_DSN must be configured before loading re-entry data.")
    return value


def _connect():
    return psycopg2.connect(_dsn())


def _load_pending_re_entry_rows() -> list[dict[str, Any]]:
    """Pull pending approval_queue rows that are re-entry proposals.

    We filter by ``policy_flags ? 're_entry_queue_id'`` so the tab
    never mixes in non-re-entry approvals (which have their own tab).
    """
    sql = """
      SELECT queue_id::text       AS queue_id,
             route_code            AS route_id,
             version               AS proposed_version,
             policy_flags,
             geometry_report,
             stop_coverage_report,
             proposed_stops,
             proposed_shape,
             enqueued_at,
             quality_class,
             tier4_pending_count,
             pending_dr_batches,
             classified_at,
             reclassified_count,
             pre_ship_cleanup_applied,
             pre_ship_cleanup_report,
             pre_ship_cleanup_at
        FROM route_prod.approval_queue
       WHERE status = 'pending'
         AND policy_flags ? 're_entry_queue_id'
       ORDER BY enqueued_at ASC
       LIMIT 200
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def _load_v1_route(route_id: str) -> Optional[dict[str, Any]]:
    sql = """
      SELECT route_id::text       AS route_id,
             version,
             ST_AsGeoJSON(geom)   AS geom_json,
             stop_node_ids::text[] AS stop_node_ids,
             legacy_grandfathered,
             grandfathered_until,
             pipeline_version
        FROM route_prod.routes
       WHERE route_id = %s::uuid
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (route_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _load_v1_stop_coords(stop_ids: list[str]) -> list[dict[str, float]]:
    if not stop_ids:
        return []
    sql = """
      SELECT node_id::text AS stop_id,
             ST_Y(geom)    AS lat,
             ST_X(geom)    AS lon
        FROM node_prod.nodes
       WHERE node_id = ANY(%s::uuid[])
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (stop_ids,))
        return [
            {"stop_id": r["stop_id"], "lat": float(r["lat"]), "lon": float(r["lon"])}
            for r in cur.fetchall()
        ]


def _coords_from_linestring(geom_json: Any) -> list[tuple[float, float]]:
    if not geom_json:
        return []
    if isinstance(geom_json, str):
        try:
            geom_json = json.loads(geom_json)
        except Exception:
            return []
    if not isinstance(geom_json, dict):
        return []
    gtype = str(geom_json.get("type") or "").lower()
    if gtype == "linestring":
        raw = geom_json.get("coordinates") or []
        return [
            (float(xy[0]), float(xy[1]))
            for xy in raw
            if isinstance(xy, (list, tuple)) and len(xy) >= 2
        ]
    if gtype == "multilinestring":
        flat: list[tuple[float, float]] = []
        for line in geom_json.get("coordinates") or []:
            for xy in line:
                if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                    flat.append((float(xy[0]), float(xy[1])))
        return flat
    return []


def _operator_identity() -> tuple[Optional[str], str]:
    user = st.session_state.get("auth.user") or {}
    operator_id = user.get("user_id")
    operator_username = (
        user.get("display_name")
        or user.get("email")
        or str(st.session_state.get("auth.username") or "operator")
    )
    return operator_id, operator_username


def _render_v1_v2_map(
    *,
    v1_coords: list[tuple[float, float]],
    v2_coords: list[tuple[float, float]],
    v1_stops: list[dict[str, Any]],
    v2_stops: list[dict[str, Any]],
) -> None:
    if not v2_coords and not v1_coords:
        st.info("No geometry available to render.")
        return

    centre = (v2_coords or v1_coords)[len(v2_coords or v1_coords) // 2]

    v1_path = [{"name": "v1", "path": [[lon, lat] for lon, lat in v1_coords]}]
    v2_path = [{"name": "v2", "path": [[lon, lat] for lon, lat in v2_coords]}]

    v1_stops_df = pd.DataFrame(
        [{"lon": s["lon"], "lat": s["lat"], "label": str(s.get("stop_id") or "")}
         for s in v1_stops]
    )
    v2_stops_df = pd.DataFrame(
        [{"lon": s["lon"], "lat": s["lat"], "label": str(s.get("stop_id") or "")}
         for s in v2_stops]
    )

    layers = []
    if v1_coords:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=v1_path,
                get_path="path",
                get_width=4,
                width_min_pixels=2,
                get_color=_V1_COLOR + [140],
            )
        )
    if v2_coords:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=v2_path,
                get_path="path",
                get_width=5,
                width_min_pixels=3,
                get_color=_V2_COLOR + [220],
            )
        )
    if not v1_stops_df.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=v1_stops_df,
                get_position="[lon, lat]",
                get_radius=30,
                radius_min_pixels=3,
                get_fill_color=_V1_STOP_COLOR,
            )
        )
    if not v2_stops_df.empty:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=v2_stops_df,
                get_position="[lon, lat]",
                get_radius=40,
                radius_min_pixels=4,
                get_fill_color=_V2_STOP_COLOR,
            )
        )

    st.pydeck_chart(
        pdk.Deck(
            map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
            initial_view_state=pdk.ViewState(
                latitude=float(centre[1]),
                longitude=float(centre[0]),
                zoom=12,
                pitch=0,
            ),
            layers=layers,
            tooltip={"text": "{label}"},
        ),
        use_container_width=True,
    )
    st.caption(
        "Legend — grey line + grey dots: v1 (current). Red line + "
        "amber dots: v2 proposal."
    )


def _summary_table(row: dict[str, Any]) -> pd.DataFrame:
    flags = row.get("policy_flags") or {}
    return pd.DataFrame(
        [
            {"field": "route_id", "value": row.get("route_id")},
            {"field": "fix_category", "value": flags.get("fix_category")},
            {"field": "classification", "value": flags.get("re_entry_classification")},
            {"field": "n_coords v1 → v2",
             "value": f"{flags.get('v1_n_coords')} → {flags.get('v2_n_coords')}"},
            {"field": "n_stops v1 → v2",
             "value": f"{flags.get('v1_n_stops')} → {flags.get('v2_n_stops')}"},
            {"field": "geom_fix_reason", "value": flags.get("geom_fix_reason")},
            {"field": "cov_fix_reason", "value": flags.get("cov_fix_reason")},
            {"field": "enqueued_at", "value": row.get("enqueued_at")},
        ]
    )


# ---------------------------------------------------------------------------
# v2 quality-class buckets (matches hades.enforcers.stop_coverage_enforcer
# taxonomy + the migration 034 CHECK constraint).
# ---------------------------------------------------------------------------

_BUCKETS = {
    "Ready to Ship": ("good", "acceptable", "ship_pending_dr"),
    "Review First":  ("degraded_minor",),
    "Needs Work":    ("degraded",),
    "Cannot Fix":    ("unroutable",),
}
_BUCKET_BADGE = {
    "Ready to Ship": "🟢",
    "Review First":  "🟡",
    "Needs Work":    "🟠",
    "Cannot Fix":    "🔴",
}


def _render_bucket_header(rows: list[dict[str, Any]]) -> None:
    """Top-of-tab summary by v2 quality_class bucket + DR-blocking panel."""
    by_class: dict[str, int] = {}
    last_classified: Optional[str] = None
    for r in rows:
        cls = r.get("quality_class") or "unknown"
        by_class[cls] = by_class.get(cls, 0) + 1
        cat = r.get("classified_at")
        if cat:
            cat_str = str(cat)
            if last_classified is None or cat_str > last_classified:
                last_classified = cat_str

    bucket_counts: dict[str, int] = {}
    for name, classes in _BUCKETS.items():
        bucket_counts[name] = sum(by_class.get(c, 0) for c in classes)

    st.markdown(
        f"**{len(rows)} pending** · last reclassified "
        f"`{last_classified or '(never)'}`"
    )
    cols = st.columns(len(_BUCKETS))
    for i, (name, classes) in enumerate(_BUCKETS.items()):
        breakdown = " · ".join(
            f"{c}: {by_class.get(c, 0)}" for c in classes
        )
        cols[i].metric(
            f"{_BUCKET_BADGE[name]} {name}",
            bucket_counts[name],
            delta=breakdown,
            delta_color="off",
        )

    # DR batches blocking shipments — aggregate pending_dr_batches.
    blocking: dict[str, int] = {}
    for r in rows:
        for b in (r.get("pending_dr_batches") or []):
            blocking[b] = blocking.get(b, 0) + 1
    if blocking:
        with st.expander(
            f"🚧 DR Batches blocking shipments — {len(blocking)} batch(es)",
            expanded=False,
        ):
            df = pd.DataFrame(
                sorted(blocking.items(), key=lambda kv: -kv[1]),
                columns=["dr_batch_id", "routes_waiting"],
            )
            st.dataframe(df, hide_index=True, use_container_width=True)


_SHIPPABLE_CLASSES_FOR_CLEANUP: frozenset[str] = frozenset(
    {"good", "acceptable", "ship_pending_dr"}
)


def _load_preship_cleanup_audit_rows() -> list[dict[str, Any]]:
    """Pull every approval_queue row that has been touched by cleanup.

    Returns rows where either ``pre_ship_cleanup_applied=TRUE`` (cleanup
    landed) or ``pre_ship_cleanup_report IS NOT NULL`` (cleanup ran but
    the safety gate rejected it). Plus aggregate counters so the panel
    can show totals against the whole eligible cohort.
    """
    sql = """
      SELECT queue_id::text             AS queue_id,
             route_code                  AS route_id,
             quality_class,
             status,
             pre_ship_cleanup_applied,
             pre_ship_cleanup_at,
             pre_ship_cleanup_report
        FROM route_prod.approval_queue
       WHERE pre_ship_cleanup_applied = TRUE
          OR pre_ship_cleanup_report IS NOT NULL
       ORDER BY pre_ship_cleanup_at DESC NULLS LAST,
                enqueued_at ASC
    """
    sql_eligible = """
      SELECT count(*) AS n
        FROM route_prod.approval_queue
       WHERE status='pending'
         AND pre_ship_cleanup_applied = FALSE
         AND pre_ship_cleanup_report IS NULL
         AND quality_class IN ('good','acceptable','ship_pending_dr')
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql)
        touched = [dict(r) for r in cur.fetchall()]
        cur.execute(sql_eligible)
        eligible_pending = int(cur.fetchone()["n"])
    for r in touched:
        report = r.get("pre_ship_cleanup_report") or {}
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except Exception:
                report = {}
        r["_report"] = report
    return touched + [{"__eligible_pending": eligible_pending}]


def _audit_row_to_record(r: dict[str, Any]) -> dict[str, Any]:
    """Flatten a cleanup-touched row into a single record for the table."""
    report = r.get("_report") or {}
    applied = bool(r.get("pre_ship_cleanup_applied"))
    before = int(report.get("total_stops_before", 0))
    after = int(report.get("total_stops_after", 0))
    removed = int(report.get("stops_removed", 0))
    snapped = int(report.get("stops_snapped", report.get("stops_projected", 0)))
    removal_pct = (removed / before) if before else 0.0
    if applied:
        outcome = "applied"
    elif report.get("validate_ok") is False:
        outcome = "rejected"
    else:
        outcome = "unknown"
    return {
        "outcome": outcome,
        "route_id": str(r.get("route_id"))[:8],
        "class": r.get("quality_class") or "?",
        "v": int(report.get("cleanup_version", 0)),
        "before": before,
        "after": after,
        "snapped": snapped,
        "removed": removed,
        "removal_%": f"{removal_pct:.0%}" if before else "—",
        "applied_at": (
            r["pre_ship_cleanup_at"].strftime("%m-%d %H:%M")
            if r.get("pre_ship_cleanup_at") else ""
        ),
        "reject_reason": (report.get("validate_reason") or "")
            if not applied else "",
        "queue_id": str(r.get("queue_id"))[:8] if r.get("queue_id") else "",
    }


def _render_preship_cleanup_audit_panel() -> None:
    """Cohort-level audit of pre-ship cleanup outcomes.

    Renders a top-of-tab summary (applied / rejected / pending) plus a
    sortable table of every cleanup-touched row so the operator can
    verify the backfill landed correctly without drilling into each
    proposal individually.
    """
    try:
        all_rows = _load_preship_cleanup_audit_rows()
    except Exception as exc:
        st.error(f"Could not load cleanup audit: {exc}")
        return

    eligible_pending = 0
    rows: list[dict[str, Any]] = []
    for r in all_rows:
        if "__eligible_pending" in r:
            eligible_pending = int(r["__eligible_pending"])
        else:
            rows.append(r)

    if not rows and eligible_pending == 0:
        return  # cleanup hasn't run anywhere yet — skip the panel

    applied_rows = [r for r in rows if r.get("pre_ship_cleanup_applied")]
    rejected_rows = [
        r for r in rows
        if not r.get("pre_ship_cleanup_applied")
        and (r.get("_report") or {}).get("validate_ok") is False
    ]

    total_snapped = sum(
        int((r.get("_report") or {}).get("stops_snapped",
            (r.get("_report") or {}).get("stops_projected", 0)))
        for r in applied_rows
    )
    total_removed = sum(
        int((r.get("_report") or {}).get("stops_removed", 0))
        for r in applied_rows
    )
    total_blocked_removals = sum(
        int((r.get("_report") or {}).get("stops_removed", 0))
        for r in rejected_rows
    )
    v2_applied = sum(
        1 for r in applied_rows
        if int((r.get("_report") or {}).get("cleanup_version", 0)) == 2
    )

    st.markdown("##### Pre-ship orphan cleanup — cohort audit")
    with st.expander("How to read this panel", expanded=False):
        st.markdown(
            "Pre-ship cleanup removes or snaps stops that drift off "
            "the route polyline before a v2 ships. Three outcomes:\n\n"
            "- **Applied**: cleanup landed on the row. `proposed_stops` "
            "now has snapped + aligned stops only; orphans (>60 m off "
            "polyline) are gone.\n"
            "- **Rejected (safety gate)**: cleanup would have removed "
            ">20 % of stops, so it was blocked. `proposed_stops` "
            "is unchanged, but the report records what the cleanup "
            "*would* have done. These rows usually need a polyline "
            "rebuild, not a threshold tweak.\n"
            "- **Eligible, not yet run**: pending row in a shippable "
            "class with no DR blockers. Backfill will pick these up.\n\n"
            "**Per-row table columns:** `before/after` = stops count, "
            "`snapped` = stops moved onto polyline, `removed` = "
            "orphans dropped, `removal_%` = removed/before. v=cleanup "
            "schema version (2 = current 3-category design).\n\n"
            "**Inspector below** shows the chosen row's BEFORE / "
            "AFTER map so you can see exactly what changed."
        )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Applied", len(applied_rows), help=f"{v2_applied} at v2")
    c2.metric("Rejected (safety gate)", len(rejected_rows))
    c3.metric("Eligible, not yet run", eligible_pending)
    c4.metric("Stops snapped (applied total)", total_snapped)

    sub1, sub2 = st.columns(2)
    sub1.caption(f"Stops removed across applied rows: **{total_removed}**")
    sub2.caption(
        f"Stops blocked from removal by safety gate (rejected rows): "
        f"**{total_blocked_removals}**"
    )

    if rows:
        records = [_audit_row_to_record(r) for r in rows]
        df = pd.DataFrame.from_records(records)
        # Stable column order; outcome first so it's visible.
        cols = [
            "outcome", "route_id", "class", "v",
            "before", "after", "snapped", "removed", "removal_%",
            "applied_at", "reject_reason", "queue_id",
        ]
        df = df.reindex(columns=[c for c in cols if c in df.columns])
        st.dataframe(df, use_container_width=True, hide_index=True)

        _render_preship_cleanup_inspector(rows)

    _render_refill_audit_panel()
    st.divider()


def _load_refill_audit_summary() -> dict[str, int]:
    """Aggregate the γ phase metrics for the cohort audit panel."""
    sql_aq = """
        SELECT
          COUNT(*) FILTER (WHERE refill_applied = TRUE) AS applied,
          COUNT(*) FILTER (
            WHERE status = 'pending'
              AND refill_applied = FALSE
              AND quality_class IN ('good','acceptable','ship_pending_dr')
          ) AS eligible_pending
          FROM route_prod.approval_queue
    """
    sql_audit = """
        SELECT decision, COUNT(*) AS n
          FROM route_prod.refill_audit
         GROUP BY decision
    """
    out = {
        "applied": 0,
        "eligible_pending": 0,
        "total_decisions": 0,
        "accepted": 0,
        "skipped": 0,
        "reviewed_later": 0,
    }
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql_aq)
        r = cur.fetchone()
        if r is not None:
            out["applied"] = int(r["applied"] or 0)
            out["eligible_pending"] = int(r["eligible_pending"] or 0)
        cur.execute(sql_audit)
        for row in cur.fetchall():
            d = row.get("decision")
            if d in out:
                out[d] = int(row.get("n") or 0)
            out["total_decisions"] += int(row.get("n") or 0)
    return out


def _render_refill_audit_panel() -> None:
    """Cohort-level audit of γ phase (Stop Refill) outcomes.

    Renders only when the cohort already shows refill activity (any
    refill_applied row OR any refill_audit entry). Empty cohorts skip
    the panel entirely so it doesn't clutter pre-ship workflows where
    GREEK pipeline hasn't run yet.
    """
    try:
        m = _load_refill_audit_summary()
    except Exception as exc:
        st.caption(f"Could not load refill audit metrics: {exc}")
        return

    if m["applied"] == 0 and m["total_decisions"] == 0 and m["eligible_pending"] == 0:
        return

    st.markdown("##### γ — Stop Refill cohort metrics")
    with st.expander("How to read this panel", expanded=False):
        st.markdown(
            "Stop Refill (γ) is the inverse complement to cleanup: it "
            "ADDS stops the polyline visits but `proposed_stops` omits, "
            "sourced from production routes via geometric matching.\n\n"
            "- **Routes with refill**: rows where `refill_applied=TRUE` "
            "(at least one accepted candidate landed on the row).\n"
            "- **Eligible for refill**: pending + shippable rows that "
            "have not yet run GREEK pipeline.\n"
            "- **Total decisions / Accepted / Skipped / Reviewed later**: "
            "per-candidate operator outcomes from `refill_audit`. "
            "Acceptance rate = accepted / total."
        )

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Routes with refill", m["applied"])
    c2.metric("Eligible for refill", m["eligible_pending"])
    c3.metric("Total decisions", m["total_decisions"])
    c4.metric("Accepted", m["accepted"])
    c5.metric("Skipped", m["skipped"])

    if m["total_decisions"] > 0:
        accept_rate = m["accepted"] / m["total_decisions"] * 100.0
        st.caption(
            f"Acceptance rate: **{accept_rate:.1f}%** "
            f"({m['accepted']}/{m['total_decisions']})  ·  "
            f"Reviewed later: {m['reviewed_later']}"
        )


# ---------------------------------------------------------------------------
# Per-row BEFORE / AFTER map inspector (cohort audit panel)
# ---------------------------------------------------------------------------

# Visual coding shared by both maps.
_INSP_ALIGNED_COLOR = [60, 180, 90, 220]    # green
_INSP_SNAPPED_COLOR = [255, 180, 0, 230]    # amber
_INSP_ORPHAN_COLOR  = [230, 60, 60, 230]    # red
_INSP_LINE_COLOR    = [70, 130, 220, 220]   # blue


def _load_inspector_payload(queue_id: str) -> Optional[dict[str, Any]]:
    """Pull proposed_shape + proposed_stops for one queue row."""
    sql = """
      SELECT proposed_shape, proposed_stops
        FROM route_prod.approval_queue
       WHERE queue_id = %s::uuid
    """
    with _connect() as conn, conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:
        cur.execute(sql, (queue_id,))
        row = cur.fetchone()
    if row is None:
        return None
    stops = row.get("proposed_stops") or []
    if isinstance(stops, str):
        try:
            stops = json.loads(stops)
        except Exception:
            stops = []
    return {
        "proposed_shape": row.get("proposed_shape"),
        "proposed_stops": stops,
    }


def _reconstruct_before_after(
    *,
    proposed_stops: list[dict[str, Any]],
    report: dict[str, Any],
    applied: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the BEFORE and AFTER stop lists for the inspector maps.

    Each list element carries ``stop_id``, ``lat``, ``lon`` and a
    ``role`` field (``aligned`` / ``snapped`` / ``orphan``).

    BEFORE: every stop at its **original** position (snapped stops use
    ``original_lat/lon`` from the report; orphans are added back).

    AFTER (applied): ``proposed_stops`` as-is — aligned untouched,
    snapped stops at their new on-polyline position, orphans gone.

    AFTER (rejected): the counterfactual — aligned only. Orphans
    would have been removed and snapped stops would have moved, but
    the safety gate blocked the write. We render this so the operator
    can see *why* the gate fired (lots of red disappearing).
    """
    snapped = report.get("snapped_stops") or report.get("projections") or []
    orphans = report.get("orphans_removed") or []

    snapped_by_id = {str(s.get("stop_id")): s for s in snapped if s.get("stop_id")}
    orphan_ids = {str(o.get("stop_id")) for o in orphans if o.get("stop_id")}

    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []

    seen_ids: set[str] = set()
    for s in proposed_stops:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("stop_id") or "")
        seen_ids.add(sid)
        try:
            cur_lat = float(s["lat"])
            cur_lon = float(s["lon"])
        except (KeyError, TypeError, ValueError):
            continue

        if sid in snapped_by_id:
            sn = snapped_by_id[sid]
            try:
                orig_lat = float(sn["original_lat"])
                orig_lon = float(sn["original_lon"])
            except (KeyError, TypeError, ValueError):
                orig_lat, orig_lon = cur_lat, cur_lon
            before.append(
                {"stop_id": sid, "lat": orig_lat, "lon": orig_lon, "role": "snapped"}
            )
            if applied:
                after.append(
                    {"stop_id": sid, "lat": cur_lat, "lon": cur_lon, "role": "snapped"}
                )
            else:
                # Rejected: snap was never written; show counterfactual
                # snapped position so the operator sees the intended move.
                try:
                    sn_lat = float(sn["snapped_lat"])
                    sn_lon = float(sn["snapped_lon"])
                    after.append(
                        {"stop_id": sid, "lat": sn_lat, "lon": sn_lon, "role": "snapped"}
                    )
                except (KeyError, TypeError, ValueError):
                    pass
        elif sid in orphan_ids:
            # Orphan still present in proposed_stops only happens for
            # rejected rows (cleanup did not modify the array).
            before.append(
                {"stop_id": sid, "lat": cur_lat, "lon": cur_lon, "role": "orphan"}
            )
            # AFTER: orphan is removed (counterfactual for rejected).
        else:
            before.append(
                {"stop_id": sid, "lat": cur_lat, "lon": cur_lon, "role": "aligned"}
            )
            after.append(
                {"stop_id": sid, "lat": cur_lat, "lon": cur_lon, "role": "aligned"}
            )

    # For applied rows the orphans are no longer in proposed_stops, so
    # add them back to BEFORE using their original positions.
    for o in orphans:
        sid = str(o.get("stop_id") or "")
        if sid in seen_ids:
            continue
        try:
            o_lat = float(o["original_lat"])
            o_lon = float(o["original_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        before.append(
            {"stop_id": sid, "lat": o_lat, "lon": o_lon, "role": "orphan"}
        )
        # AFTER: orphan removed, so absent.

    # Snapped stops not present in proposed_stops (shouldn't happen,
    # but be defensive): add to BEFORE at original, AFTER at snapped.
    for sid, sn in snapped_by_id.items():
        if sid in seen_ids:
            continue
        try:
            o_lat = float(sn["original_lat"])
            o_lon = float(sn["original_lon"])
            sn_lat = float(sn["snapped_lat"])
            sn_lon = float(sn["snapped_lon"])
        except (KeyError, TypeError, ValueError):
            continue
        before.append({"stop_id": sid, "lat": o_lat, "lon": o_lon, "role": "snapped"})
        after.append({"stop_id": sid, "lat": sn_lat, "lon": sn_lon, "role": "snapped"})

    return before, after


def _render_inspector_map(
    *,
    title: str,
    coords: list[tuple[float, float]],
    stops: list[dict[str, Any]],
    centre: tuple[float, float],
) -> None:
    """Render a single pydeck map with polyline + role-coloured stops."""
    st.markdown(f"**{title}** — {len(stops)} stops")

    if not coords and not stops:
        st.info("No geometry or stops to render.")
        return

    layers = []
    if coords:
        layers.append(
            pdk.Layer(
                "PathLayer",
                data=[{"path": [[lon, lat] for lon, lat in coords]}],
                get_path="path",
                get_width=4,
                width_min_pixels=2,
                get_color=_INSP_LINE_COLOR,
            )
        )

    role_to_color = {
        "aligned": _INSP_ALIGNED_COLOR,
        "snapped": _INSP_SNAPPED_COLOR,
        "orphan":  _INSP_ORPHAN_COLOR,
    }
    for role, colour in role_to_color.items():
        subset = [s for s in stops if s.get("role") == role]
        if not subset:
            continue
        df = pd.DataFrame(
            [{"lon": s["lon"], "lat": s["lat"], "label": f"{role}: {str(s.get('stop_id') or '')[:8]}"}
             for s in subset]
        )
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=df,
                get_position="[lon, lat]",
                get_radius=35,
                radius_min_pixels=4,
                get_fill_color=colour,
                pickable=True,
            )
        )

    st.pydeck_chart(
        pdk.Deck(
            map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
            initial_view_state=pdk.ViewState(
                latitude=float(centre[1]),
                longitude=float(centre[0]),
                zoom=13,
                pitch=0,
            ),
            layers=layers,
            tooltip={"text": "{label}"},
        ),
        use_container_width=True,
    )


def _render_preship_cleanup_inspector(
    rows: list[dict[str, Any]],
) -> None:
    """Per-row BEFORE / AFTER map split for cleanup-touched rows."""
    if not rows:
        return

    st.markdown("##### Inspector — before vs. after cleanup")

    # Selectbox keyed by queue_id; show outcome + route + counts so
    # the operator can pick a meaningful row to scrutinise.
    def _label(r: dict[str, Any]) -> str:
        rep = r.get("_report") or {}
        applied = bool(r.get("pre_ship_cleanup_applied"))
        outcome = "applied" if applied else (
            "rejected" if rep.get("validate_ok") is False else "?"
        )
        snap = int(rep.get("stops_snapped", rep.get("stops_projected", 0)))
        rem = int(rep.get("stops_removed", 0))
        return (
            f"{outcome:<8} {str(r.get('route_id'))[:8]}  "
            f"snap={snap:<2} remove={rem:<2}  "
            f"q={str(r.get('queue_id'))[:8]}"
        )

    queue_ids = [str(r.get("queue_id")) for r in rows]
    selected = st.selectbox(
        "Inspect a cleanup-touched row",
        options=queue_ids,
        format_func=lambda qid: next(
            (_label(r) for r in rows if str(r.get("queue_id")) == qid), qid
        ),
        key="p3.re_entry.preship_audit.inspect",
    )

    chosen = next((r for r in rows if str(r.get("queue_id")) == selected), None)
    if chosen is None:
        return

    payload = _load_inspector_payload(selected)
    if payload is None:
        st.warning("Row not found in approval_queue.")
        return

    coords = _coords_from_linestring(payload.get("proposed_shape"))
    if not coords:
        st.warning("Proposed polyline is empty — nothing to draw.")
        return

    report = chosen.get("_report") or {}
    applied = bool(chosen.get("pre_ship_cleanup_applied"))
    before_stops, after_stops = _reconstruct_before_after(
        proposed_stops=payload.get("proposed_stops") or [],
        report=report,
        applied=applied,
    )

    centre = coords[len(coords) // 2]
    n_aligned_before = sum(1 for s in before_stops if s["role"] == "aligned")
    n_snapped = sum(1 for s in before_stops if s["role"] == "snapped")
    n_orphan = sum(1 for s in before_stops if s["role"] == "orphan")

    st.caption(
        f"🟢 **aligned** ({n_aligned_before}, ≤10 m): unchanged · "
        f"🟡 **snapped** ({n_snapped}, 10–60 m): moved onto polyline · "
        f"🔴 **orphan** ({n_orphan}, >60 m): "
        + ("removed" if applied else "would be removed (blocked by safety gate)")
        + " · 🔵 polyline = canonical."
    )

    left, right = st.columns(2, gap="medium")
    with left:
        _render_inspector_map(
            title="BEFORE — every stop at its original position",
            coords=coords,
            stops=before_stops,
            centre=centre,
        )
    with right:
        title = (
            "AFTER — applied (orphans gone, snaps moved)"
            if applied
            else "AFTER (counterfactual) — what cleanup *would* produce"
        )
        _render_inspector_map(
            title=title,
            coords=coords,
            stops=after_stops,
            centre=centre,
        )

    if not applied:
        reason = report.get("validate_reason") or "unknown"
        st.warning(
            f"Safety gate fired: {reason}. The AFTER map is a "
            "counterfactual — `proposed_stops` was NOT modified. "
            "Look at the red dots in BEFORE: that is the cluster of "
            "stops that drifted >60 m from the polyline. A high "
            "removal % usually means the polyline itself is wrong "
            "(corridor shifted), not that the threshold is too tight."
        )


def _render_preship_cleanup_panel(row: dict[str, Any]) -> None:
    """Read-only summary of cleanup state for the selected row.

    As of the 2026-04-27 trigger refactor cleanup runs ONLY through
    the approve → preview → confirm flow below. This panel just
    renders the persisted report (when cleanup has already landed) so
    the operator can audit what the swap did.
    """
    cls = row.get("quality_class")
    applied = bool(row.get("pre_ship_cleanup_applied"))
    report = row.get("pre_ship_cleanup_report") or {}
    if isinstance(report, str):
        try:
            report = json.loads(report)
        except Exception:
            report = {}

    if applied and report:
        st.divider()
        st.markdown("##### Pre-ship orphan cleanup applied")
        col1, col2, col3 = st.columns(3)
        before = int(report.get("total_stops_before", 0))
        after = int(report.get("total_stops_after", 0))
        col1.metric("Stops before", before)
        col2.metric("Stops after", after)
        col3.metric("Net change", after - before)

        applied_at = row.get("pre_ship_cleanup_at")
        if applied_at is not None:
            st.caption(
                f"Applied at {applied_at}; cleanup_version="
                f"{report.get('cleanup_version', '?')}; thresholds="
                f"{report.get('thresholds_used', {})}"
            )

        orphans = report.get("orphans_removed") or []
        if orphans:
            st.markdown("**Orphans removed (too far from polyline)**")
            st.dataframe(
                pd.DataFrame(orphans),
                use_container_width=True,
                hide_index=True,
            )

        snapped = (
            report.get("snapped_stops")
            or report.get("projections")
            or []
        )
        if snapped:
            st.markdown("**Snapped to polyline**")
            st.dataframe(
                pd.DataFrame(snapped),
                use_container_width=True,
                hide_index=True,
            )

        if not report.get("validate_ok", True):
            st.warning(
                "Safety gate triggered during the swap: "
                f"{report.get('validate_reason', 'unknown')}."
            )
        return

    if cls in _SHIPPABLE_CLASSES_FOR_CLEANUP and not applied:
        st.divider()
        st.caption(
            "Pre-ship orphan cleanup will run automatically when you "
            "click APPROVE — the swap service shows a BEFORE/AFTER "
            "preview before anything commits."
        )


def _render_dr_dependency_panel(row: dict[str, Any]) -> None:
    """Per-route DR-dependency block, shown only for ship_pending_dr /
    degraded_minor rows that have at least one pending batch."""
    cls = row.get("quality_class")
    pending = list(row.get("pending_dr_batches") or [])
    if cls not in ("ship_pending_dr", "degraded_minor") or not pending:
        return
    st.divider()
    st.markdown("##### 🚧 DR dependencies")
    n = int(row.get("tier4_pending_count") or 0)
    st.markdown(
        f"This route is waiting on **{len(pending)}** DR batch(es) "
        f"to resolve **{n}** tier-4 gap(s)."
    )
    for b in pending:
        if b.startswith("batch_"):
            link = f"workspace/dr_stop_coverage/queries/{b}.md"
            note = "(existing batch — process if not yet sent)"
        else:
            link = f"workspace/dr_stop_coverage/pending_requests/{b}.md"
            note = "(on-demand request — paste into Claude.ai with web search)"
        st.markdown(f"- `{b}` → `{link}`  {note}")
    st.caption(
        "Approving now ships the v2 with current resolutions (Tier 1/2 + "
        "synthetic fallback for unresolved gaps). When the DR batch lands, "
        "the reclassifier upgrades the row automatically — but if the row "
        "is already approved, no further reclassification happens."
    )


def _render_swap_preview_block(
    *,
    preview: Any,
    queue_id: str,
    operator_id: str,
    operator_username: str,
    preview_key: str,
    row: dict[str, Any],
) -> None:
    """Render the swap preview + confirm/cancel/override controls.

    Driven by an in-memory ``SwapPreview`` (from
    :func:`re_entry_swap_service.preview_swap_with_cleanup`). The
    operator sees BEFORE/AFTER maps, a change summary, and a button
    that calls ``confirm_swap_with_cleanup`` atomically.
    """
    st.divider()
    st.markdown("##### Swap preview")

    if preview.swap_blockers:
        st.error(
            "This route cannot be swapped: "
            + "; ".join(preview.swap_blockers)
        )
        if st.button("Dismiss preview", key=f"{preview_key}.dismiss_blocked"):
            del st.session_state[preview_key]
            st.rerun()
        return

    if not preview.cleanup_eligible:
        st.info(
            "Cleanup will be skipped for this swap — "
            f"{preview.cleanup_skip_reason}. "
            "The swap can still proceed with the current proposed_stops."
        )
        c_ok, c_cancel = st.columns(2)
        if c_ok.button(
            "✅ Confirm and ship (no cleanup)",
            key=f"{preview_key}.confirm_no_cleanup",
            type="primary",
            use_container_width=True,
        ):
            _execute_confirm(
                queue_id=queue_id,
                operator_id=operator_id,
                operator_username=operator_username,
                operator_override=False,
                override_reason=None,
                preview_key=preview_key,
                expected_fingerprint=preview.state_fingerprint,
            )
        if c_cancel.button(
            "Cancel",
            key=f"{preview_key}.cancel_no_cleanup",
            use_container_width=True,
        ):
            del st.session_state[preview_key]
            st.rerun()
        return

    # Cleanup eligible: render BEFORE/AFTER maps + change summary.
    report = preview.cleanup_report or {}
    proposed_stops_raw = row.get("proposed_stops") or []
    if isinstance(proposed_stops_raw, str):
        try:
            proposed_stops_raw = json.loads(proposed_stops_raw)
        except Exception:
            proposed_stops_raw = []

    before_stops, after_stops = _reconstruct_before_after(
        proposed_stops=proposed_stops_raw,
        report=report,
        applied=False,  # always counterfactual in preview mode
    )

    coords = _coords_from_linestring(row.get("proposed_shape"))
    if coords:
        centre = coords[len(coords) // 2]
        col_b, col_a = st.columns(2, gap="medium")
        with col_b:
            _render_inspector_map(
                title="BEFORE — current stops",
                coords=coords,
                stops=before_stops,
                centre=centre,
            )
        with col_a:
            _render_inspector_map(
                title="AFTER — what cleanup will produce",
                coords=coords,
                stops=after_stops,
                centre=centre,
            )

    n_aligned = int(report.get("stops_aligned", 0))
    n_snap = int(report.get("stops_snapped", 0))
    n_remove = int(report.get("stops_removed", 0))
    st.markdown(
        f"**Cleanup will:** snap **{n_snap}** stops onto polyline · "
        f"remove **{n_remove}** orphan(s) · "
        f"keep **{n_aligned}** aligned stop(s) as-is."
    )

    if preview.safety_gate_status == "pass":
        c_ok, c_cancel = st.columns(2)
        if c_ok.button(
            "✅ Confirm and ship",
            key=f"{preview_key}.confirm",
            type="primary",
            use_container_width=True,
        ):
            _execute_confirm(
                queue_id=queue_id,
                operator_id=operator_id,
                operator_username=operator_username,
                operator_override=False,
                override_reason=None,
                preview_key=preview_key,
                expected_fingerprint=preview.state_fingerprint,
            )
        if c_cancel.button(
            "Cancel",
            key=f"{preview_key}.cancel",
            use_container_width=True,
        ):
            del st.session_state[preview_key]
            st.rerun()
        return

    # Safety gate would reject the cleanup.
    st.warning(
        f"⚠️ Safety gate would reject this cleanup: "
        f"{preview.safety_gate_reason}. This usually means corridor-shift "
        "(the polyline does not match where the stops actually are). "
        "Consider rebuilding the polyline instead of overriding."
    )
    with st.expander("Override and ship anyway (advanced)"):
        override_reason = st.text_area(
            "Reason for override (required, will be logged to "
            "route_prod.cleanup_override_audit):",
            key=f"{preview_key}.override_reason",
            placeholder=(
                "e.g. 'Visual inspection confirms orphans are real bus "
                "stops on a side-road; polyline is correct.' "
                "Min 20 characters."
            ),
        )
        ovr_col1, ovr_col2 = st.columns(2)
        if ovr_col1.button(
            "⚠️ Override & ship",
            key=f"{preview_key}.override_confirm",
            use_container_width=True,
        ):
            if len((override_reason or "").strip()) < 20:
                st.error("Reason must be at least 20 characters.")
            else:
                _execute_confirm(
                    queue_id=queue_id,
                    operator_id=operator_id,
                    operator_username=operator_username,
                    operator_override=True,
                    override_reason=override_reason.strip(),
                    preview_key=preview_key,
                    expected_fingerprint=preview.state_fingerprint,
                )
        if ovr_col2.button(
            "Cancel",
            key=f"{preview_key}.override_cancel",
            use_container_width=True,
        ):
            del st.session_state[preview_key]
            st.rerun()


def _execute_confirm(
    *,
    queue_id: str,
    operator_id: str,
    operator_username: str,
    operator_override: bool,
    override_reason: Optional[str],
    preview_key: str,
    expected_fingerprint: str,
) -> None:
    """Call ``confirm_swap_with_cleanup`` and surface the outcome."""
    try:
        result = swap_svc.confirm_swap_with_cleanup(
            approval_queue_id=queue_id,
            expected_fingerprint=expected_fingerprint,
            operator_id=operator_id,
            operator_username=operator_username,
            dsn=_dsn(),
            operator_override=operator_override,
            override_reason=override_reason,
        )
        msg = (
            f"Swap applied — v{result.version_before} → "
            f"v{result.version_after}. Fix report: "
            f"{result.fix_report_id}. "
            f"Synthetic stops created: "
            f"{len(result.created_synthetic_node_ids)}."
        )
        if result.cleanup_applied:
            msg += " Cleanup applied"
            if result.override_used:
                msg += " (with operator override)"
            msg += "."
        st.success(msg)
    except swap_svc.SwapError as exc:
        st.error(f"Swap rejected by business rule: {exc}")
        return
    except Exception as exc:
        st.exception(exc)
        return
    if preview_key in st.session_state:
        del st.session_state[preview_key]
    st.rerun()


# ---------------------------------------------------------------------------
# GREEK pipeline preview block (β → α → γ → δ).
# ---------------------------------------------------------------------------

_DECISION_LABEL_TO_VALUE = {
    "Accept": "accepted",
    "Skip": "skipped",
    "Review later": "reviewed_later",
}
_DECISION_VALUE_TO_LABEL = {v: k for k, v in _DECISION_LABEL_TO_VALUE.items()}


def _render_refill_candidates_section(
    *,
    preview: Any,
    queue_id: str,
    decisions_key: str,
) -> int:
    """Render the per-candidate review section. Returns the number of
    decisions made so the caller can gate the Confirm button."""
    candidates = preview.refill_candidates
    if not candidates:
        return 0

    st.markdown("### γ — Refill candidates from production routes")
    st.caption(
        f"Found **{len(candidates)}** candidate(s) "
        f"({preview.refill_high_confidence_count} high confidence, "
        f"{preview.refill_medium_confidence_count} medium). "
        "Review EACH candidate individually before confirming. "
        "No bulk accept; per-candidate decision only."
    )

    decisions = dict(st.session_state.get(decisions_key, {}))

    for i, cand in enumerate(candidates):
        stop_id = cand.get("stop_id") or ""
        if not stop_id:
            continue

        col_info, col_decision = st.columns([3, 2], gap="small")
        with col_info:
            band = cand.get("confidence_band") or "medium"
            band_emoji = "🟢" if band == "high" else "🟡"
            st.markdown(
                f"**{band_emoji} Candidate {i + 1}** — "
                f"score: **{float(cand.get('score') or 0.0):.2f}**"
            )
            mcols = st.columns(5)
            mcols[0].metric(
                "Dist to A",
                f"{float(cand.get('distance_to_polyline_m') or 0.0):.1f} m",
            )
            mcols[1].metric(
                "Origin align",
                f"{float(cand.get('origin_distance_m') or 0.0):.1f} m",
                help="Distance to source-route polyline. Spec: stop must "
                     "be ≤10 m on its origin to qualify.",
            )
            mcols[2].metric("Popularity", int(cand.get("popularity") or 0))
            mcols[3].metric(
                "Max segment",
                f"{float(cand.get('max_shared_segment_m') or 0.0):.0f} m",
            )
            n_sources = len(cand.get("source_route_ids") or [])
            mcols[4].caption(f"Sources: {n_sources} route(s)")
            st.caption(
                f"Stop id: `{stop_id}` · "
                f"({float(cand.get('lat') or 0.0):.5f}, "
                f"{float(cand.get('lon') or 0.0):.5f})"
            )

        with col_decision:
            current = decisions.get(stop_id)
            current_label = _DECISION_VALUE_TO_LABEL.get(current)
            options = list(_DECISION_LABEL_TO_VALUE.keys())
            try:
                idx = options.index(current_label) if current_label else None
            except ValueError:
                idx = None
            choice = st.radio(
                f"Decision for candidate {i + 1}",
                options=options,
                index=idx,
                key=f"p3.re_entry.greek.decision.{queue_id}.{stop_id}",
                horizontal=False,
            )
            if choice is not None:
                decisions[stop_id] = _DECISION_LABEL_TO_VALUE[choice]
                st.session_state[decisions_key] = decisions

        st.divider()

    decided = len(decisions)
    total = len(candidates)
    if decided < total:
        st.warning(
            f"⚠️ {decided}/{total} candidates reviewed. All candidates must "
            "have a decision before the Confirm button enables."
        )
    return decided


def _execute_greek_confirm(
    *,
    queue_id: str,
    operator_id: str,
    operator_username: str,
    operator_override: bool,
    override_reason: Optional[str],
    expected_fingerprint: str,
    refill_decisions: dict[str, str],
    preview_key: str,
    decisions_key: str,
) -> None:
    """Call confirm_greek_pipeline and surface the GreekPipelineResult."""
    try:
        result = swap_svc.confirm_greek_pipeline(
            approval_queue_id=queue_id,
            expected_fingerprint=expected_fingerprint,
            refill_decisions=refill_decisions,
            operator_id=operator_id,
            operator_username=operator_username,
            dsn=_dsn(),
            operator_override=operator_override,
            override_reason=override_reason,
        )
    except Exception as exc:
        st.exception(exc)
        return

    if result.success:
        msg_parts = [f"Shipped! Fix report: {result.swap_id}."]
        if result.cleanup_applied:
            msg_parts.append("Cleanup applied.")
        if result.refill_decisions_count:
            msg_parts.append(
                f"Refill: {result.refill_accepted_count} accepted of "
                f"{result.refill_decisions_count} candidates."
            )
        if result.override_used:
            msg_parts.append("Operator override used.")
        st.success(" ".join(msg_parts))
        for k in (preview_key, decisions_key):
            if k in st.session_state:
                del st.session_state[k]
        st.rerun()
    else:
        st.error(f"GREEK pipeline rejected: {result.error}")


def _render_greek_preview_block(
    *,
    preview: Any,
    queue_id: str,
    operator_id: str,
    operator_username: str,
    preview_key: str,
    row: dict[str, Any],
) -> None:
    """Single combined preview for cleanup (α) + refill (γ).

    Renders three sections in order:
      1. α cleanup BEFORE/AFTER maps + summary
      2. γ refill candidates with per-candidate Accept | Skip | Review later
      3. Confirm / Cancel / Override (Confirm disabled until all
         refill candidates have been decided)
    """
    decisions_key = f"p3.re_entry.greek.decisions.{queue_id}"

    if preview.blockers:
        st.error(
            "Cannot ship this route: " + "; ".join(preview.blockers)
        )
        if st.button(
            "Dismiss preview", key=f"{preview_key}.dismiss_blocked"
        ):
            for k in (preview_key, decisions_key):
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()
        return

    if not preview.cleanup_eligible:
        st.info(
            "Cleanup will be skipped for this swap — "
            f"{preview.cleanup_skip_reason}. "
            "GREEK pipeline requires a shippable, DR-clean row."
        )
        if st.button("Dismiss preview", key=f"{preview_key}.dismiss_skip"):
            for k in (preview_key, decisions_key):
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()
        return

    # ----- Section 1 — α cleanup BEFORE/AFTER maps -----
    st.markdown("### α — Cleanup preview")
    report = preview.cleanup_report or {}
    proposed_stops_raw = row.get("proposed_stops") or []
    if isinstance(proposed_stops_raw, str):
        try:
            proposed_stops_raw = json.loads(proposed_stops_raw)
        except Exception:
            proposed_stops_raw = []

    before_stops, after_stops = _reconstruct_before_after(
        proposed_stops=proposed_stops_raw,
        report=report,
        applied=False,
    )

    coords = _coords_from_linestring(row.get("proposed_shape"))
    if coords:
        centre = coords[len(coords) // 2]
        cb, ca = st.columns(2, gap="medium")
        with cb:
            _render_inspector_map(
                title="BEFORE — current stops",
                coords=coords, stops=before_stops, centre=centre,
            )
        with ca:
            _render_inspector_map(
                title="AFTER cleanup",
                coords=coords, stops=after_stops, centre=centre,
            )

    n_aligned = int(report.get("stops_aligned", 0))
    n_snap = int(report.get("stops_snapped", 0))
    n_remove = int(report.get("stops_removed", 0))
    st.markdown(
        f"**Cleanup will:** snap **{n_snap}** stops onto polyline · "
        f"remove **{n_remove}** orphan(s) · "
        f"keep **{n_aligned}** aligned stop(s) as-is."
    )

    # ----- Section 2 — γ refill candidates -----
    if preview.refill_candidates:
        st.divider()
    decided_count = _render_refill_candidates_section(
        preview=preview,
        queue_id=queue_id,
        decisions_key=decisions_key,
    )
    total_candidates = len(preview.refill_candidates)

    # ----- Section 3 — Confirm / Cancel / Override -----
    st.divider()
    decisions = dict(st.session_state.get(decisions_key, {}))
    decisions_complete = (
        total_candidates == 0 or decided_count >= total_candidates
    )

    if preview.cleanup_safety_gate == "pass":
        col_ok, col_cancel = st.columns(2)
        confirm_help = (
            "All refill candidates must be reviewed before Confirm enables"
            if not decisions_complete else None
        )
        if col_ok.button(
            "✅ Confirm and ship (GREEK pipeline)",
            key=f"{preview_key}.confirm_greek",
            type="primary",
            disabled=not decisions_complete,
            help=confirm_help,
            use_container_width=True,
        ):
            _execute_greek_confirm(
                queue_id=queue_id,
                operator_id=operator_id,
                operator_username=operator_username,
                operator_override=False,
                override_reason=None,
                expected_fingerprint=preview.state_fingerprint,
                refill_decisions=decisions,
                preview_key=preview_key,
                decisions_key=decisions_key,
            )
        if col_cancel.button(
            "Cancel",
            key=f"{preview_key}.cancel_greek",
            use_container_width=True,
        ):
            for k in (preview_key, decisions_key):
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()
        return

    # Safety-gate reject path.
    st.warning(
        f"⚠️ Cleanup safety gate would reject: "
        f"{preview.cleanup_safety_reason}. This usually means corridor-shift "
        "(the polyline does not match where the stops actually are). "
        "Consider rebuilding the polyline instead of overriding."
    )
    with st.expander("Override and ship anyway (advanced)"):
        override_reason = st.text_area(
            "Reason for override (required, will be logged to "
            "route_prod.cleanup_override_audit). Min 20 characters:",
            key=f"{preview_key}.override_reason_greek",
            placeholder=(
                "e.g. 'Visual inspection confirms orphans are real bus stops "
                "on a side-road; polyline is correct.'"
            ),
        )
        ovr1, ovr2 = st.columns(2)
        if ovr1.button(
            "⚠️ Override & ship (GREEK pipeline)",
            key=f"{preview_key}.override_greek_confirm",
            disabled=not decisions_complete,
            help=(
                None if decisions_complete
                else "Review all refill candidates first"
            ),
            use_container_width=True,
        ):
            if len((override_reason or "").strip()) < 20:
                st.error("Reason must be at least 20 characters.")
            else:
                _execute_greek_confirm(
                    queue_id=queue_id,
                    operator_id=operator_id,
                    operator_username=operator_username,
                    operator_override=True,
                    override_reason=override_reason.strip(),
                    expected_fingerprint=preview.state_fingerprint,
                    refill_decisions=decisions,
                    preview_key=preview_key,
                    decisions_key=decisions_key,
                )
        if ovr2.button(
            "Cancel",
            key=f"{preview_key}.override_greek_cancel",
            use_container_width=True,
        ):
            for k in (preview_key, decisions_key):
                if k in st.session_state:
                    del st.session_state[k]
            st.rerun()


def render_re_entry_review_tab(**_: Any) -> None:
    st.markdown("### Re-Entry Review — Grandfathered Route Swaps")
    st.caption(
        "Queue of v2 proposals generated by the re-entry worker for "
        "pre-gate legacy routes. Each approval lifts a row from "
        "`legacy_grandfathered=TRUE` into a re-enforced v2."
    )

    try:
        rows = _load_pending_re_entry_rows()
    except Exception as exc:
        st.error(f"Could not load approval queue: {exc}")
        return

    _render_preship_cleanup_audit_panel()

    if not rows:
        st.success(
            "No pending re-entry proposals. Worker backlog is either empty, "
            "all swapped, or still planning."
        )
        return

    _render_bucket_header(rows)
    st.divider()

    queue_ids = [r["queue_id"] for r in rows]
    label_map = {}
    for r in rows:
        flags = r.get("policy_flags") or {}
        cls = r.get("quality_class") or "unknown"
        label_map[r["queue_id"]] = (
            f"{str(r['route_id'])[:8]}…  "
            f"q={cls:<18}  "
            f"fix={flags.get('fix_category', '?'):<14}  "
            f"orig={flags.get('re_entry_classification', '?')}"
        )

    selected = st.selectbox(
        "Proposal",
        options=queue_ids,
        format_func=lambda qid: label_map.get(qid, qid),
        key="p3.re_entry.selected_queue_id",
    )
    row = next((r for r in rows if r["queue_id"] == selected), None)
    if row is None:
        return

    route_id = str(row["route_id"])
    v1 = _load_v1_route(route_id)
    if v1 is None:
        st.warning(
            "v1 route row not found — it may have been swapped while you "
            "were viewing the queue. Refresh to re-sync."
        )
        return

    v1_coords = _coords_from_linestring(v1.get("geom_json"))
    v1_stops = _load_v1_stop_coords(list(v1.get("stop_node_ids") or []))
    v2_coords = _coords_from_linestring(row.get("proposed_shape"))
    v2_stops_raw = row.get("proposed_stops") or []
    if isinstance(v2_stops_raw, str):
        try:
            v2_stops_raw = json.loads(v2_stops_raw)
        except Exception:
            v2_stops_raw = []
    v2_stops = [
        {"stop_id": s.get("stop_id"), "lat": float(s["lat"]), "lon": float(s["lon"])}
        for s in v2_stops_raw
        if isinstance(s, dict) and "lat" in s and "lon" in s
    ]

    left, right = st.columns([1.0, 2.0], gap="large")
    with right:
        _render_v1_v2_map(
            v1_coords=v1_coords,
            v2_coords=v2_coords,
            v1_stops=v1_stops,
            v2_stops=v2_stops,
        )

    with left:
        st.markdown("**Proposal summary**")
        st.dataframe(
            _summary_table(row),
            use_container_width=True,
            hide_index=True,
        )

        with st.expander("geometry_report"):
            st.json(row.get("geometry_report") or {})
        with st.expander("stop_coverage_report"):
            st.json(row.get("stop_coverage_report") or {})
        with st.expander("policy_flags"):
            st.json(row.get("policy_flags") or {})

    _render_dr_dependency_panel(row)

    _render_preship_cleanup_panel(row)

    st.divider()

    operator_id, operator_username = _operator_identity()
    if not operator_id:
        st.warning(
            "No authenticated user — sign in from the Settings tab "
            "before approving/rejecting. You can still preview."
        )
        return

    st.markdown("**Action**")
    a1, a2, a3 = st.columns(3)
    with a1:
        do_preview = st.button(
            "APPROVE v2 (preview GREEK pipeline)",
            key=f"p3.re_entry.preview.{selected}",
            type="primary",
            use_container_width=True,
        )
    with a2:
        reject_reason = st.text_input(
            "Reject reason",
            key=f"p3.re_entry.reject_reason.{selected}",
            placeholder="why v2 is worse than v1",
        )
        do_reject = st.button(
            "REJECT v2 → queue back to pending",
            key=f"p3.re_entry.reject.{selected}",
            use_container_width=True,
        )
    with a3:
        quarantine_reason = st.text_input(
            "Quarantine reason",
            key=f"p3.re_entry.quar_reason.{selected}",
            placeholder="needs hand-authored fix",
        )
        do_quarantine = st.button(
            "QUARANTINE → lock from auto-retry",
            key=f"p3.re_entry.quar.{selected}",
            use_container_width=True,
        )

    preview_key = f"p3.re_entry.greek_preview.{selected}"
    if do_preview:
        try:
            preview = swap_svc.preview_greek_pipeline(
                selected, dsn=_dsn(),
            )
            st.session_state[preview_key] = preview
            decisions_key = f"p3.re_entry.greek.decisions.{selected}"
            if decisions_key in st.session_state:
                del st.session_state[decisions_key]
        except Exception as exc:
            st.exception(exc)
        else:
            st.rerun()

    if preview_key in st.session_state:
        _render_greek_preview_block(
            preview=st.session_state[preview_key],
            queue_id=selected,
            operator_id=operator_id,
            operator_username=operator_username,
            preview_key=preview_key,
            row=row,
        )

    if do_reject:
        if not (reject_reason or "").strip():
            st.error("Reject reason is required.")
        else:
            try:
                swap_svc.reject_v2(
                    approval_queue_id=selected,
                    operator_id=operator_id,
                    operator_username=operator_username,
                    reason=reject_reason.strip(),
                    dsn=_dsn(),
                )
                st.success("v2 rejected — re-entry queue reset to pending.")
                st.rerun()
            except swap_svc.SwapError as exc:
                st.error(f"Reject rejected by business rule: {exc}")
            except Exception as exc:
                st.exception(exc)

    if do_quarantine:
        if not (quarantine_reason or "").strip():
            st.error("Quarantine reason is required.")
        else:
            try:
                swap_svc.quarantine_v2(
                    approval_queue_id=selected,
                    operator_id=operator_id,
                    operator_username=operator_username,
                    reason=quarantine_reason.strip(),
                    dsn=_dsn(),
                )
                st.success(
                    "Route quarantined — worker will not auto-retry "
                    "until manually unlocked."
                )
                st.rerun()
            except swap_svc.SwapError as exc:
                st.error(f"Quarantine rejected by business rule: {exc}")
            except Exception as exc:
                st.exception(exc)
