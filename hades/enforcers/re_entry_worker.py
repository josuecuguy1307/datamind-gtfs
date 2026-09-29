"""HADES Re-Entry Worker.

Claims pending rows from ``route_prod.re_entry_queue``, runs the
Geometry Fixer and Stop Coverage Fixer against each v1 route, hands the
v2 candidate to the Phase 3 coordinator in ``enhance`` mode, and — if
v2 is strictly better — inserts the proposal into
``route_prod.approval_queue`` for operator review.

The worker NEVER swaps v1 for v2. It only queues. The swap is the
operator's job in the Control Tower (C7), which consumes the
``approval_queue`` row + the ``re_entry_queue`` row and performs the
in-place UPDATE under routes_audit.

State machine (matches migration 032):

    pending → in_progress → v2_ready      (coordinator queued v2)
                          → failed        (fixer refused / coordinator rejected)
                          → quarantined   (attempts >= MAX_ATTEMPTS)

CLI:
    python -m hades.enforcers.re_entry_worker run --batch 5 [--dry-run] [--verbose]
    python -m hades.enforcers.re_entry_worker deadline-monitor
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import psycopg2
import psycopg2.extras

from hades.enforcers.geometry_enforcer import GeometryEnforcer
from hades.enforcers.geometry_fixer import GeometryFixer, fix_sequence_incoherence
from hades.enforcers.troublemaker_stop_swap import try_local_swaps
from hades.enforcers.phase3_coordinator import (
    RouteContext,
    run_phase3_enforcers,
)
from hades.enforcers.stop_coverage_enforcer import (
    StopCoverageEnforcer,
    DEFAULT_THRESHOLDS as SC_DEFAULT_THRESHOLDS,
    classify_persisted,
)
from hades.enforcers.stop_coverage_fixer import StopCoverageFixer
from hades.enforcers.reclassifier import predict_dr_batches_for_report
from datamind_core.dsn import need_dsn

# Self-hosted Overpass for Tier 2 (courtesy 5 req/sec).
_OVERPASS_URL = os.environ.get(
    "HADES_OVERPASS_URL", "http://127.0.0.1:12346/api/interpreter"
)
_OVERPASS_MIN_INTERVAL = 0.2
_overpass_last_call = 0.0


DEFAULT_DSN = os.environ.get("DB_DSN", "")
DEADLINE = date(2026, 7, 20)
MAX_ATTEMPTS = 3

# Re-entry default: conservative. Rationale — v1→v2 swaps touch production;
# queueing a severe-geometry reject to phase2 beats silently promoting it.
# Overridable via --policy CLI or HADES_POLICY_PROFILE env var.
POLICY_PROFILES = ("conservative", "balanced", "aggressive_supervised")
DEFAULT_POLICY_PROFILE = os.environ.get("HADES_POLICY_PROFILE", "conservative")
if DEFAULT_POLICY_PROFILE not in POLICY_PROFILES:
    raise RuntimeError(
        f"HADES_POLICY_PROFILE={DEFAULT_POLICY_PROFILE!r} invalid; "
        f"expected one of {POLICY_PROFILES}"
    )


# ---------------------------------------------------------------------------
# Row loading.
# ---------------------------------------------------------------------------

def _claim_batch(
    conn,
    batch_size: int,
    *,
    route_ids: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Claim up to ``batch_size`` pending rows (FOR UPDATE SKIP LOCKED).

    Only rows with ``attempts < MAX_ATTEMPTS`` and ``status='pending'``
    are eligible. Rows are ordered by priority asc then enqueued_at asc
    so the highest-priority oldest work runs first. The caller must
    issue the subsequent UPDATE (to ``in_progress``) inside the same
    transaction; we return the hydrated rows with geom + stop ids.

    ``route_ids`` — optional allow-list. When provided, restricts the
    claim to rows whose ``route_id`` is in the list. Used for per-unit
    processing (Quito Centro etc.) so T1 doesn't drain other units' queues.
    """
    clauses = ["q.status = 'pending'", "q.attempts < %s"]
    params: list[Any] = [MAX_ATTEMPTS]
    if route_ids is not None:
        if not route_ids:
            return []  # empty allow-list → nothing to claim
        clauses.append("q.route_id::text = ANY(%s)")
        params.append(list(route_ids))
    params.append(batch_size)
    sql = f"""
        SELECT q.queue_id, q.route_id, q.current_version, q.priority,
               q.classification, q.attempts, q.priority_reason,
               r.source_type, r.route_name, r.province,
               ST_AsGeoJSON(r.geom) AS geom_json,
               r.stop_node_ids::text[] AS stop_node_ids
        FROM route_prod.re_entry_queue q
        JOIN route_prod.routes r
          ON r.route_id = q.route_id AND r.version = q.current_version
        WHERE {' AND '.join(clauses)}
        ORDER BY q.priority, q.enqueued_at
        LIMIT %s
        FOR UPDATE OF q SKIP LOCKED
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, tuple(params))
        return [dict(r) for r in cur.fetchall()]


def _mark_in_progress(conn, queue_ids: list[str]) -> None:
    if not queue_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE route_prod.re_entry_queue
               SET status = 'in_progress',
                   attempts = attempts + 1,
                   attempted_at = NOW()
             WHERE queue_id = ANY(%s::uuid[])
            """,
            (queue_ids,),
        )


def _fetch_stop_coords(
    conn, stop_node_ids: list[str]
) -> tuple[list[tuple[float, float]], list[str]]:
    if not stop_node_ids:
        return [], []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT node_id::text, ST_Y(geom) AS lat, ST_X(geom) AS lon
              FROM node_prod.nodes
             WHERE node_id = ANY(%s::uuid[])
             ORDER BY array_position(%s::uuid[], node_id)
            """,
            (stop_node_ids, stop_node_ids),
        )
        rows = cur.fetchall()
    coords = [(float(r[1]), float(r[2])) for r in rows]
    ids = [str(r[0]) for r in rows]
    return coords, ids


def _extract_linestring(geom_json: str) -> list[tuple[float, float]]:
    geom = json.loads(geom_json)
    if geom["type"] == "LineString":
        return [(float(c[0]), float(c[1])) for c in geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        flat: list[tuple[float, float]] = []
        for line in geom["coordinates"]:
            for c in line:
                flat.append((float(c[0]), float(c[1])))
        return flat
    raise ValueError(f"Unexpected geometry type: {geom['type']}")


# ---------------------------------------------------------------------------
# Tier 1 / Tier 2 resolver builders.
# ---------------------------------------------------------------------------
#
# Mirrors scripts/stop_coverage_diagnostic_full.py. Resolvers are built
# per-route with a fresh read-only connection so they can run inside the
# worker's main transactional connection without interfering with it.
# Both are opt-in — callers decide whether to use them.


def _build_tier1_resolver(
    *,
    dsn: str,
    route_geom_wkt: str,
    borrow_buffer_m: float,
    corridor_buffer_m: float,
    exclude_stop_ids: list[str],
) -> Callable[..., list[dict[str, Any]]]:
    """Return a CrossRouteResolverFn that queries node_prod.nodes.

    Opens its own short-lived read-only connection per call. Each call is
    a single indexed SQL — sub-millisecond on :5432 local.
    """
    def _resolver(*, lat, lon, buffer_m, route_corridor_coords,
                  corridor_buffer_m: float = corridor_buffer_m,
                  exclude_stop_ids=exclude_stop_ids) -> list[dict[str, Any]]:
        try:
            conn = psycopg2.connect(need_dsn(dsn))
            conn.set_session(readonly=True)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT n.node_id::text,
                               ST_Y(n.geom) AS lat,
                               ST_X(n.geom) AS lon,
                               n.name,
                               ST_Distance(n.geom::geography,
                                           ST_MakePoint(%s, %s)::geography) AS gap_dist_m
                        FROM node_prod.nodes n
                        WHERE n.superseded_by IS NULL
                          AND NOT (n.node_id = ANY(%s::uuid[]))
                          AND ST_DWithin(n.geom::geography,
                                         ST_MakePoint(%s, %s)::geography, %s)
                          AND ST_DWithin(n.geom::geography,
                                         ST_GeomFromText(%s, 4326)::geography, %s)
                        ORDER BY gap_dist_m
                        LIMIT 12
                        """,
                        (float(lon), float(lat),
                         list(exclude_stop_ids or []),
                         float(lon), float(lat), float(buffer_m),
                         route_geom_wkt, float(corridor_buffer_m)),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return [
            {"node_id": r[0], "lat": float(r[1]), "lon": float(r[2]),
             "name": r[3], "distance_m": round(float(r[4]), 2)}
            for r in rows
        ]
    return _resolver


def _overpass_query(
    lat: float, lon: float, buffer_m: float, *, timeout_s: int = 10
) -> list[dict[str, Any]]:
    """Single-point Overpass query for bus_stop / platform / stop_position nodes."""
    global _overpass_last_call
    delta = time.time() - _overpass_last_call
    if delta < _OVERPASS_MIN_INTERVAL:
        time.sleep(_OVERPASS_MIN_INTERVAL - delta)
    deg_lat = buffer_m / 111_132.0
    deg_lon = buffer_m / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
    s, n = lat - deg_lat, lat + deg_lat
    w, e = lon - deg_lon, lon + deg_lon
    ql = (
        f"[out:json][timeout:{timeout_s}];"
        f'(node["highway"="bus_stop"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});'
        f'node["public_transport"="platform"]({s:.6f},{w:.6f},{n:.6f},{e:.6f});'
        f'node["public_transport"="stop_position"]({s:.6f},{w:.6f},{n:.6f},{e:.6f}););'
        f"out body;"
    )
    try:
        import urllib.request
        req = urllib.request.Request(
            _OVERPASS_URL,
            data=ql.encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": "hades-re-entry/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            elements = (json.loads(resp.read()) or {}).get("elements", []) or []
    except Exception:
        elements = []
    finally:
        _overpass_last_call = time.time()
    out: list[dict[str, Any]] = []
    for el in elements:
        if el.get("type") != "node":
            continue
        elat, elon = el.get("lat"), el.get("lon")
        if elat is None or elon is None:
            continue
        dy = (float(elat) - lat) * 111_132.0
        dx = (float(elon) - lon) * 111_320.0 * math.cos(math.radians(0.5 * (float(elat) + lat)))
        d = math.hypot(dx, dy)
        if d > buffer_m:
            continue
        out.append({
            "osm_id": el.get("id"), "lat": float(elat), "lon": float(elon),
            "name": (el.get("tags") or {}).get("name"),
            "tags": el.get("tags") or {}, "distance_m": round(d, 2),
        })
    return out


def _build_tier2_resolver() -> Callable[[float, float, float], list[dict[str, Any]]]:
    def _resolver(lat: float, lon: float, buffer_m: float) -> list[dict[str, Any]]:
        return _overpass_query(float(lat), float(lon), float(buffer_m))
    return _resolver


def _coords_to_wkt_linestring(coords) -> str:
    """coords are (lon, lat); return a WKT LineString."""
    pts = ", ".join(f"{float(lon)} {float(lat)}" for (lon, lat) in coords)
    return f"LINESTRING({pts})"


# ---------------------------------------------------------------------------
# Core per-route processing.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ProcessOutcome:
    queue_id: str
    route_id: str
    terminal_status: str               # "v2_ready" | "failed" | "quarantined"
    last_error: Optional[str] = None
    approval_queue_id: Optional[str] = None
    fix_category: Optional[str] = None  # "geometry" | "stop_coverage" | "structural"
    n_coords_before: int = 0
    n_coords_after: int = 0
    n_stops_before: int = 0
    n_stops_after: int = 0


def _fix_category(coords_changed: bool, stops_changed: bool) -> Optional[str]:
    if coords_changed and stops_changed:
        return "structural"
    if coords_changed:
        return "geometry"
    if stops_changed:
        return "stop_coverage"
    return None


def _linestring_to_geojson(coords: list[tuple[float, float]]) -> dict[str, Any]:
    return {
        "type": "LineString",
        "coordinates": [[float(lon), float(lat)] for (lon, lat) in coords],
    }


def _proposed_stops_payload(
    stops: list[tuple[float, float]], ids: list[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, (lat, lon) in enumerate(stops):
        sid = ids[i] if i < len(ids) else f"synthesized_{i}"
        out.append({"stop_id": sid, "lat": float(lat), "lon": float(lon)})
    return out


def _process_route(
    row: dict[str, Any],
    *,
    conn,
    geom_fixer: GeometryFixer,
    cov_fixer: StopCoverageFixer,
    policy_profile: str = DEFAULT_POLICY_PROFILE,
    dr_landmarks_by_route: Optional[dict[str, dict[int, dict[str, Any]]]] = None,
    dsn: Optional[str] = None,
    enable_tier1: bool = False,
    enable_tier2: bool = False,
    strictness: str = "strict",
    clean_outliers: bool = False,
    outlier_threshold_m: float = 120.0,
    mode: str = "gap_fill",
    type1_anchors_by_route: Optional[dict[str, list[dict[str, Any]]]] = None,
    enable_sequence_optimizer: bool = False,
    sequence_optimizer_max_retraces: int = 20,
    sequence_optimizer_timeout_s: float = 60.0,
    enable_troublemaker_swap: bool = False,
    troublemaker_max_swaps: int = 5,
    troublemaker_max_retraces: int = 15,
) -> ProcessOutcome:
    queue_id = str(row["queue_id"])
    route_id = str(row["route_id"])
    current_version = int(row["current_version"])
    classification = row["classification"]

    coords_v1 = _extract_linestring(row["geom_json"])
    stop_ids_v1_raw = list(row["stop_node_ids"] or [])
    stops_v1, stop_ids_v1 = _fetch_stop_coords(conn, stop_ids_v1_raw)

    n_coords_before = len(coords_v1)
    n_stops_before = len(stops_v1)

    # Unclassified routes have no agreed playbook — defer.
    if classification == "unclassified":
        return ProcessOutcome(
            queue_id=queue_id,
            route_id=route_id,
            terminal_status="failed",
            last_error="classification=unclassified — no playbook",
            n_coords_before=n_coords_before,
            n_stops_before=n_stops_before,
        )

    # Can't fix without a shape.
    if n_coords_before < 3:
        return ProcessOutcome(
            queue_id=queue_id,
            route_id=route_id,
            terminal_status="failed",
            last_error=f"v1 shape too small (n_coords={n_coords_before})",
            n_coords_before=n_coords_before,
            n_stops_before=n_stops_before,
        )

    route_code_for_reports = route_id  # Legacy routes have no route_code column.

    # ---------- Geometry fix ----------
    geom_res = geom_fixer.fix(coords=coords_v1, route_code=route_code_for_reports)
    coords_v2 = list(geom_res.coords_after)
    coords_changed = (
        geom_res.success and len(coords_v2) != len(coords_v1)
    )

    # ---------- Phase 1.5: troublemaker stop swap (opt-in, surgical) ------
    # Strict-gated local swaps: accepts a swap iff the original anomaly is
    # resolved AND no new anomalies are introduced AND class does not
    # worsen. Proven on QC 116-cohort eval: 0 regressions, 14 % U_TURN fix
    # rate, ~9 s total. Safe to leave on.
    tms_audit: dict[str, Any] = {"attempted": False}
    if enable_troublemaker_swap and len(stops_v1) >= 4:
        from phase3_routes.services.route_constructor.src.geometry.valhalla_client import (
            valhalla_route as _valhalla_route_tms,
        )
        try:
            tms_result = try_local_swaps(
                route_id=route_code_for_reports,
                shape=coords_v2,
                stops=stops_v1,
                retrace_fn=lambda locs: list(_valhalla_route_tms(locs)),
                max_swaps=troublemaker_max_swaps,
                max_retraces=troublemaker_max_retraces,
            )
        except Exception as exc:  # noqa: BLE001
            tms_result = None
            tms_audit = {"attempted": True, "error": f"{type(exc).__name__}: {exc}"}
        if tms_result is not None:
            tms_audit = {
                "attempted": True,
                "success": bool(tms_result.success),
                "anomalies_v1": list(tms_result.anomalies_v1),
                "anomalies_resolved": list(tms_result.anomalies_resolved),
                "anomalies_remaining": list(tms_result.anomalies_remaining),
                "new_anomalies_introduced": list(tms_result.new_anomalies_introduced),
                "n_accepted_swaps": sum(1 for s in tms_result.swap_attempts if s.accepted),
                "retraces_used": int(tms_result.retraces_used),
                "elapsed_s": round(tms_result.elapsed_s, 2),
            }
            if tms_result.success and tms_result.proposed_shape and tms_result.proposed_stops:
                coords_v2 = list(tms_result.proposed_shape)
                stops_v1 = list(tms_result.proposed_stops)
                stop_ids_v1 = [f"tms_reordered_{i}" for i in range(len(stops_v1))]

    # ---------- Phase 2: sequence coherence optimizer (opt-in) ----------
    # If the Geometry Enforcer flags DI>0.6 / OSCILLATION / corridor
    # IMPOSSIBLE_LOOP on the geometry-fixed shape, permute stop order +
    # retrace via Valhalla and keep the permutation with highest coherence.
    # Bounded to 20 retraces / 60s per route. No change if trigger
    # condition isn't met or optimizer can't beat v1.
    seq_opt_audit: dict[str, Any] = {"attempted": False}
    if enable_sequence_optimizer:
        try:
            seq_result = fix_sequence_incoherence(
                stops=stops_v1,
                coords_v1=coords_v2,
                route_code=route_code_for_reports,
                valhalla_costing_options=None,  # fall back to Valhalla's default
                max_retraces=sequence_optimizer_max_retraces,
                timeout_s=sequence_optimizer_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            seq_result = None
            seq_opt_audit = {"attempted": True, "error": f"{type(exc).__name__}: {exc}"}
        if seq_result is not None:
            seq_opt_audit = {
                "attempted": True,
                "triggered": True,
                "success": bool(seq_result.success),
                "strategy": seq_result.strategy_used,
                "coherence_before": round(seq_result.coherence_before, 4),
                "coherence_after": round(seq_result.coherence_after, 4),
                "retraces_used": int(seq_result.retraces_used),
                "elapsed_s": round(seq_result.elapsed_s, 2),
                "reason": seq_result.reason,
            }
            if seq_result.success:
                # Optimizer produced a strictly-better-coherent retrace.
                # Feed the new polyline + reordered stops into the coverage
                # fixer so Tier cascades operate on the improved shape.
                coords_v2 = list(seq_result.coords_after)
                stops_v1 = list(seq_result.stops_after)
                # stop_ids no longer align 1-to-1 with reordered stops —
                # synthesise new ids (Fixer won't care; it uses coords).
                stop_ids_v1 = [f"seq_reordered_{i}" for i in range(len(stops_v1))]
        else:
            seq_opt_audit = {
                "attempted": True, "triggered": False,
                "reason": "trigger_condition_not_met",
            }

    # ---------- Stop Coverage fix ----------
    # Per-route enforcer + fixer so Tier 1 / Tier 2 resolvers can close
    # over this route's specific geom. Tier 4 DR landmarks come from
    # validated/ JSON loaded once per worker run.
    route_landmarks = (dr_landmarks_by_route or {}).get(route_code_for_reports)
    cr_resolver = None
    ov_resolver = None
    if enable_tier1 and dsn:
        cr_resolver = _build_tier1_resolver(
            dsn=dsn,
            route_geom_wkt=_coords_to_wkt_linestring(coords_v2),
            borrow_buffer_m=SC_DEFAULT_THRESHOLDS.borrow_buffer_m,
            corridor_buffer_m=SC_DEFAULT_THRESHOLDS.corridor_buffer_m,
            exclude_stop_ids=list(stop_ids_v1),
        )
    if enable_tier2:
        ov_resolver = _build_tier2_resolver()

    per_route_enforcer = StopCoverageEnforcer(
        thresholds=SC_DEFAULT_THRESHOLDS,
        cross_route_resolver=cr_resolver,
        overpass_resolver=ov_resolver,
    )
    per_route_fixer = StopCoverageFixer(enforcer=per_route_enforcer)
    route_type1 = (type1_anchors_by_route or {}).get(route_code_for_reports)
    cov_res = per_route_fixer.fix(
        route_code=route_code_for_reports,
        coords=coords_v2,
        stop_coords=stops_v1,
        stop_ids=stop_ids_v1,
        dr_landmarks=route_landmarks,
        strictness=strictness,
        clean_outliers=clean_outliers,
        outlier_threshold_m=outlier_threshold_m,
        mode=mode,
        type1_anchors=route_type1,
        cross_route_resolver=cr_resolver,
        overpass_resolver=ov_resolver,
    )
    stops_v2 = list(cov_res.stops_after) if cov_res.success else list(stops_v1)
    ids_v2 = list(cov_res.stop_ids_after) if cov_res.success else list(stop_ids_v1)
    stops_changed = cov_res.success and len(stops_v2) != len(stops_v1)

    category = _fix_category(coords_changed, stops_changed)
    if category is None:
        return ProcessOutcome(
            queue_id=queue_id,
            route_id=route_id,
            terminal_status="failed",
            last_error=(
                f"no_improvement (geom={geom_res.reason},"
                f" coverage={cov_res.reason})"
            ),
            n_coords_before=n_coords_before,
            n_coords_after=len(coords_v2),
            n_stops_before=n_stops_before,
            n_stops_after=len(stops_v2),
        )

    # ---------- Coordinator in enhance mode ----------
    route_ctx = RouteContext(
        route_code=route_code_for_reports,
        coords=coords_v2,
        stop_coords=stops_v2,
        stop_ids=ids_v2,
        version=current_version + 1,
        unit_id=row.get("province") or "unassigned",
    )
    result = run_phase3_enforcers(
        route_ctx, mode="enhance", policy_profile=policy_profile
    )

    # reject_send_to_phase2 = Fixer made v2 worse; do NOT queue.
    if result.policy_decision == "reject_send_to_phase2":
        return ProcessOutcome(
            queue_id=queue_id,
            route_id=route_id,
            terminal_status="failed",
            last_error="coordinator rejected v2 (send_to_phase2)",
            fix_category=category,
            n_coords_before=n_coords_before,
            n_coords_after=len(coords_v2),
            n_stops_before=n_stops_before,
            n_stops_after=len(stops_v2),
        )

    # Anything else (queue_for_approval is the enhance-mode default) — enqueue.
    policy_flags = dict(result.policy_flags)
    policy_flags["re_entry_queue_id"] = queue_id
    policy_flags["re_entry_classification"] = classification
    policy_flags["fix_category"] = category
    policy_flags["v1_n_coords"] = n_coords_before
    policy_flags["v2_n_coords"] = len(coords_v2)
    policy_flags["v1_n_stops"] = n_stops_before
    policy_flags["v2_n_stops"] = len(stops_v2)
    policy_flags["geom_fix_reason"] = geom_res.reason
    policy_flags["cov_fix_reason"] = cov_res.reason
    policy_flags["sequence_optimizer"] = seq_opt_audit
    policy_flags["troublemaker_swap"] = tms_audit
    if cov_res.outlier_audit:
        policy_flags["outlier_audit"] = cov_res.outlier_audit

    proposed_stops = _proposed_stops_payload(stops_v2, ids_v2)
    proposed_shape = _linestring_to_geojson(coords_v2)

    # v2 classifier — runs against the persisted shapes of the reports we
    # are about to write. Same source-of-truth function the dashboard
    # reclassifier uses, so re-runs over time stay deterministic.
    quality_class, _classifier_reasoning = classify_persisted(
        result.stop_coverage_report or {},
        result.geometry_report or {},
    )
    deps_prediction = predict_dr_batches_for_report(
        result.stop_coverage_report or {},
    )
    pending_dr_batches = list(deps_prediction["pending_dr_batches"])
    tier4_pending_count = int(deps_prediction["tier4_pending_count"])

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_prod.approval_queue (
              route_code, version, policy_profile,
              decision_reasons, policy_flags,
              geometry_report, stop_coverage_report,
              proposed_stops, proposed_shape,
              dr_queries_queued, dr_queries_deferred,
              crashed, crash_payload,
              quality_class, tier4_pending_count,
              pending_dr_batches, classified_at, reclassified_count
            )
            VALUES (%s, %s, %s,
                    %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb,
                    %s, %s::jsonb,
                    %s, %s,
                    %s::text[], NOW(), 0)
            RETURNING queue_id
            """,
            (
                route_code_for_reports,
                current_version + 1,
                policy_profile,
                json.dumps(list(result.decision_reasons)),
                json.dumps(policy_flags),
                json.dumps(result.geometry_report),
                json.dumps(result.stop_coverage_report),
                json.dumps(proposed_stops),
                json.dumps(proposed_shape),
                json.dumps(list(result.dr_queries_queued)),
                json.dumps(list(result.dr_queries_deferred)),
                bool(result.crashed),
                json.dumps(result.crash_payload) if result.crash_payload else None,
                quality_class,
                tier4_pending_count,
                pending_dr_batches,
            ),
        )
        approval_queue_id = str(cur.fetchone()[0])

    # Insert dr_batch_dependencies for every predicted (batch, gap) pair.
    # ON CONFLICT DO NOTHING: re-runs of the same v2 are idempotent.
    if deps_prediction["matched"]:
        with conn.cursor() as cur:
            for batch_id, gap_idx, lat, lon in deps_prediction["matched"]:
                cur.execute(
                    """
                    INSERT INTO route_prod.dr_batch_dependencies
                        (dr_batch_id, route_id, gap_idx, gap_coords, status)
                    VALUES (%s, %s::uuid, %s, %s::jsonb, 'waiting')
                    ON CONFLICT (dr_batch_id, route_id, gap_idx) DO NOTHING
                    """,
                    (batch_id, route_code_for_reports, int(gap_idx),
                     json.dumps({"lat": lat, "lon": lon})),
                )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE route_prod.re_entry_queue
               SET status = 'v2_ready',
                   resolved_at = NOW()
             WHERE queue_id = %s::uuid
            """,
            (queue_id,),
        )

    return ProcessOutcome(
        queue_id=queue_id,
        route_id=route_id,
        terminal_status="v2_ready",
        approval_queue_id=approval_queue_id,
        fix_category=category,
        n_coords_before=n_coords_before,
        n_coords_after=len(coords_v2),
        n_stops_before=n_stops_before,
        n_stops_after=len(stops_v2),
    )


def _record_failure(conn, queue_id: str, last_error: str) -> None:
    """Update queue row with a failure verdict. If the row has now hit
    MAX_ATTEMPTS, escalate status to ``quarantined`` so the worker never
    re-claims it without human intervention.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE route_prod.re_entry_queue
               SET status = CASE
                     WHEN attempts >= %s THEN 'quarantined'
                     ELSE 'failed'
                   END,
                   last_error = %s,
                   resolved_at = CASE
                     WHEN attempts >= %s THEN NOW()
                     ELSE resolved_at
                   END
             WHERE queue_id = %s::uuid
            """,
            (MAX_ATTEMPTS, last_error, MAX_ATTEMPTS, queue_id),
        )


# ---------------------------------------------------------------------------
# Public batch runner.
# ---------------------------------------------------------------------------

def load_dr_landmarks(
    globs: list[str],
    *,
    include_uncertain: bool = True,
) -> dict[str, dict[int, dict[str, Any]]]:
    """Load validated DR Type 2 landmarks into {route_code: {gap_idx: lm_dict}}.

    Reads every file matching the provided glob patterns (relative to CWD).
    Picks the highest-final_confidence landmark per (route_code, gap_idx).
    ``gap_number`` in the validated JSON is 1-indexed; the Fixer keys off
    0-indexed ``gap_idx``, so we decrement here.

    ``include_uncertain`` — also fold in ACCEPT_UNCERTAIN items (0.4..0.6).
    Default True: the Fixer does a v1-vs-v2 comparison and only promotes
    strict improvements, so uncertain landmarks that regress coverage get
    filtered out naturally.
    """
    out: dict[str, dict[int, dict[str, Any]]] = {}
    seen_files = 0
    accepted_ct = 0
    uncertain_ct = 0
    for pattern in globs:
        for path_str in sorted(glob.glob(pattern)):
            path = Path(path_str)
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
            except Exception as exc:
                print(f"[re-entry] failed to parse {path}: {exc}", file=sys.stderr)
                continue
            seen_files += 1
            buckets = [("accepted", data.get("accepted") or [])]
            if include_uncertain:
                buckets.append((
                    "accept_uncertain",
                    data.get("accept_uncertain") or data.get("uncertain") or [],
                ))
            for bucket_name, items in buckets:
                for item in items:
                    rc = item.get("route_code")
                    if not rc:
                        continue
                    # gap_number → gap_idx (0-based inside enforcer)
                    gnum = item.get("gap_number")
                    if gnum is None:
                        continue
                    gap_idx = int(gnum) - 1
                    lat = item.get("approx_lat")
                    lon = item.get("approx_lng") or item.get("approx_lon")
                    if lat is None or lon is None:
                        continue
                    final_conf = float(item.get("final_confidence") or 0.0)
                    lm = {
                        "lat": float(lat),
                        "lon": float(lon),
                        "landmark_name": item.get("landmark_name"),
                        "final_confidence": final_conf,
                        "decision": bucket_name.upper(),
                        "source_file": path.name,
                    }
                    prev = out.setdefault(rc, {}).get(gap_idx)
                    if prev is None or final_conf > prev.get("final_confidence", 0.0):
                        out[rc][gap_idx] = lm
                    if bucket_name == "accepted":
                        accepted_ct += 1
                    else:
                        uncertain_ct += 1
    total_gaps = sum(len(gmap) for gmap in out.values())
    print(
        f"[re-entry] dr_landmarks: files={seen_files} "
        f"accepted_scored={accepted_ct} uncertain_scored={uncertain_ct} "
        f"routes={len(out)} gaps_covered={total_gaps}"
    )
    return out


def load_type1_anchors(
    unit: str,
) -> dict[str, list[dict[str, Any]]]:
    """Load Type 1 stop-grounding response JSONs into {route_code: [anchors]}.

    Reads every JSON under ``workspace/research_queue/responses/<unit>/``.
    Each file's ``seed_catalog_completion`` block provides the terminus pair
    + intermediate anchors for one route. Returns them in a uniform schema
    for the Fixer's scratch-rebuild mode.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    base = Path(os.environ.get("HADES_WORKSPACE_ROOT", ".")) / "workspace" / "research_queue" / "responses" / unit
    if not base.exists():
        base = Path("workspace") / "research_queue" / "responses" / unit
    if not base.exists():
        return out
    for p in sorted(base.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        rc = d.get("route_code")
        if not rc:
            continue
        seed = d.get("seed_catalog_completion") or {}
        anchors: list[dict[str, Any]] = []
        # Terminus origin / destination (seq 0 / last)
        term_o = seed.get("terminus_origin") or {}
        term_d = seed.get("terminus_destination") or {}
        if term_o.get("lat") is not None and term_o.get("lon") is not None:
            anchors.append({
                "lat": float(term_o["lat"]), "lon": float(term_o["lon"]),
                "sequence_index": 0, "name": term_o.get("name"),
                "role": "terminus_origin",
            })
        # Intermediate anchors
        for a in (seed.get("intermediate_anchors") or []):
            if a.get("lat") is None or a.get("lon") is None: continue
            anchors.append({
                "lat": float(a["lat"]), "lon": float(a["lon"]),
                "sequence_index": a.get("sequence_index"),
                "name": a.get("name"),
                "landmark_near": a.get("landmark_near"),
                "role": "intermediate_anchor",
            })
        if term_d.get("lat") is not None and term_d.get("lon") is not None:
            anchors.append({
                "lat": float(term_d["lat"]), "lon": float(term_d["lon"]),
                "sequence_index": 9999,
                "name": term_d.get("name"),
                "role": "terminus_destination",
            })
        if anchors:
            out[str(rc)] = anchors
    print(f"[re-entry] type1_anchors: {len(out)} routes have grounding responses")
    return out


def _load_route_ids_from_file(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        for key in ("pending_route_ids", "all_route_ids", "route_ids"):
            if key in data and isinstance(data[key], list):
                return [str(x) for x in data[key]]
    raise ValueError(f"Could not find a route_id list in {path}")


def run_batch(
    *,
    dsn: str = DEFAULT_DSN,
    batch_size: int = 5,
    dry_run: bool = False,
    verbose: bool = False,
    policy_profile: str = DEFAULT_POLICY_PROFILE,
    route_ids: Optional[list[str]] = None,
    dr_landmarks_by_route: Optional[dict[str, dict[int, dict[str, Any]]]] = None,
    enable_tier1: bool = False,
    enable_tier2: bool = False,
    strictness: str = "strict",
    clean_outliers: bool = False,
    outlier_threshold_m: float = 120.0,
    mode: str = "gap_fill",
    type1_anchors_by_route: Optional[dict[str, list[dict[str, Any]]]] = None,
    enable_sequence_optimizer: bool = False,
    sequence_optimizer_max_retraces: int = 20,
    sequence_optimizer_timeout_s: float = 60.0,
    enable_troublemaker_swap: bool = False,
    troublemaker_max_swaps: int = 5,
    troublemaker_max_retraces: int = 15,
) -> dict[str, Any]:
    """Run one batch. Returns a summary dict for logging / tests."""
    if policy_profile not in POLICY_PROFILES:
        raise ValueError(
            f"policy_profile={policy_profile!r} invalid; expected one of {POLICY_PROFILES}"
        )

    # ⚠ Experimental-flag banner (2026-04-22 review). Each of the optional
    # capabilities below was found to either regress or barely move the
    # needle on the QC 116-route eval. Standard pipeline = none of these on.
    _experimental = []
    if enable_tier1:               _experimental.append("--enable-tier1")
    if enable_tier2:               _experimental.append("--enable-tier2")
    if strictness != "strict":     _experimental.append(f"--strictness={strictness}")
    if clean_outliers:             _experimental.append("--clean-outliers")
    if mode != "gap_fill":         _experimental.append(f"--mode={mode}")
    if enable_sequence_optimizer:  _experimental.append("--enable-sequence-optimizer (DEPRECATED)")
    if enable_troublemaker_swap:   _experimental.append("--enable-troublemaker-swap (experimental)")
    if _experimental:
        print(
            "[re-entry] ⚠ EXPERIMENTAL FLAGS ENABLED: "
            + ", ".join(_experimental)
            + " — see hades/enforcers/sequence_coherence_optimizer.py + "
            "stop_main_road_snapper.py docstrings for the eval data behind"
            " these flags. Default pipeline runs none of them.",
            flush=True,
        )
    conn = psycopg2.connect(need_dsn(dsn))
    conn.autocommit = False
    geom_fixer = GeometryFixer()
    cov_fixer = StopCoverageFixer(enforcer=StopCoverageEnforcer())

    summary: dict[str, Any] = {
        "claimed": 0,
        "v2_ready": 0,
        "failed": 0,
        "quarantined": 0,
        "errors": 0,
        "outcomes": [],
    }

    try:
        rows = _claim_batch(conn, batch_size, route_ids=route_ids)
        summary["claimed"] = len(rows)
        if not rows:
            conn.commit()
            return summary

        _mark_in_progress(conn, [r["queue_id"] for r in rows])

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

        for row in rows:
            queue_id = str(row["queue_id"])
            try:
                outcome = _process_route(
                    row,
                    conn=conn,
                    geom_fixer=geom_fixer,
                    cov_fixer=cov_fixer,
                    policy_profile=policy_profile,
                    dr_landmarks_by_route=dr_landmarks_by_route,
                    dsn=dsn,
                    enable_tier1=enable_tier1,
                    enable_tier2=enable_tier2,
                    strictness=strictness,
                    clean_outliers=clean_outliers,
                    outlier_threshold_m=outlier_threshold_m,
                    mode=mode,
                    type1_anchors_by_route=type1_anchors_by_route,
                    enable_sequence_optimizer=enable_sequence_optimizer,
                    sequence_optimizer_max_retraces=sequence_optimizer_max_retraces,
                    sequence_optimizer_timeout_s=sequence_optimizer_timeout_s,
                    enable_troublemaker_swap=enable_troublemaker_swap,
                    troublemaker_max_swaps=troublemaker_max_swaps,
                    troublemaker_max_retraces=troublemaker_max_retraces,
                )
                if outcome.terminal_status == "v2_ready":
                    summary["v2_ready"] += 1
                else:
                    _record_failure(conn, queue_id, outcome.last_error or "unknown")
                    # Consult the *post-update* row to see whether we
                    # bumped into quarantine territory.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT status FROM route_prod.re_entry_queue WHERE queue_id=%s::uuid",
                            (queue_id,),
                        )
                        final_status = cur.fetchone()[0]
                    outcome.terminal_status = final_status
                    summary[final_status] = summary.get(final_status, 0) + 1

                if dry_run:
                    conn.rollback()
                else:
                    conn.commit()

                summary["outcomes"].append(
                    {
                        "queue_id": queue_id,
                        "route_id": outcome.route_id,
                        "status": outcome.terminal_status,
                        "fix_category": outcome.fix_category,
                        "n_coords_before": outcome.n_coords_before,
                        "n_coords_after": outcome.n_coords_after,
                        "n_stops_before": outcome.n_stops_before,
                        "n_stops_after": outcome.n_stops_after,
                        "approval_queue_id": outcome.approval_queue_id,
                        "last_error": outcome.last_error,
                    }
                )
                if verbose:
                    print(
                        f"[{outcome.terminal_status:<12}] {outcome.route_id[:8]} "
                        f"coords {outcome.n_coords_before}→{outcome.n_coords_after} "
                        f"stops {outcome.n_stops_before}→{outcome.n_stops_after} "
                        f"cat={outcome.fix_category or '-'}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                summary["errors"] += 1
                err = f"{type(exc).__name__}: {exc}"
                if not dry_run:
                    try:
                        _record_failure(conn, queue_id, err)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                summary["outcomes"].append(
                    {
                        "queue_id": queue_id,
                        "route_id": str(row["route_id"]),
                        "status": "errored",
                        "last_error": err,
                        "traceback": traceback.format_exc(limit=5),
                    }
                )
                if verbose:
                    print(f"[ERROR] {row['route_id']}: {err}", flush=True, file=sys.stderr)
    finally:
        conn.close()

    return summary


# ---------------------------------------------------------------------------
# Deadline monitor.
# ---------------------------------------------------------------------------

def deadline_monitor(*, dsn: str = DEFAULT_DSN) -> dict[str, Any]:
    """Summary: queue status distribution + days remaining until deadline.

    Pure read path — never writes. Used by operators and CI to track
    re-entry burn-down before 2026-07-20.
    """
    today = datetime.now(timezone.utc).date()
    days_remaining = (DEADLINE - today).days

    conn = psycopg2.connect(need_dsn(dsn))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) FROM route_prod.re_entry_queue
                GROUP BY status ORDER BY status
                """
            )
            status_counts = {r[0]: int(r[1]) for r in cur.fetchall()}

            cur.execute(
                """
                SELECT classification, COUNT(*)
                  FROM route_prod.re_entry_queue
                 GROUP BY classification ORDER BY classification
                """
            )
            class_counts = {r[0]: int(r[1]) for r in cur.fetchall()}

            cur.execute(
                """
                SELECT COUNT(*) FROM route_prod.routes
                 WHERE grandfathered_until IS NOT NULL
                   AND last_swap_at IS NULL
                """
            )
            unfixed_grandfathered = int(cur.fetchone()[0])

            cur.execute(
                """
                SELECT COUNT(*) FROM route_prod.approval_queue
                 WHERE status = 'pending'
                   AND policy_flags ? 're_entry_queue_id'
                """
            )
            pending_v2_proposals = int(cur.fetchone()[0])
    finally:
        conn.close()

    return {
        "deadline": DEADLINE.isoformat(),
        "today": today.isoformat(),
        "days_remaining": days_remaining,
        "unfixed_grandfathered": unfixed_grandfathered,
        "pending_v2_proposals": pending_v2_proposals,
        "status_counts": status_counts,
        "class_counts": class_counts,
    }


def _print_deadline_monitor(summary: dict[str, Any]) -> None:
    print("=" * 72)
    print("HADES Re-Entry Deadline Monitor")
    print("=" * 72)
    print(f"Deadline:              {summary['deadline']}")
    print(f"Today:                 {summary['today']}")
    print(f"Days remaining:        {summary['days_remaining']}")
    print(f"Unfixed grandfathered: {summary['unfixed_grandfathered']}")
    print(f"Pending v2 proposals:  {summary['pending_v2_proposals']}")
    print()
    print("Queue status distribution:")
    for status, count in summary["status_counts"].items():
        print(f"  {status:<20} {count:>5}")
    print()
    print("Queue classification distribution:")
    for cls, count in summary["class_counts"].items():
        print(f"  {cls:<32} {count:>5}")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m hades.enforcers.re_entry_worker",
        description="HADES re-entry worker (queue claim → v2 proposal).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run one batch of the worker.")
    run_p.add_argument("--batch", type=int, default=5, help="Max rows to claim.")
    run_p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN.")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Rollback all writes; compute outcomes only.")
    run_p.add_argument("--verbose", "-v", action="store_true")
    run_p.add_argument(
        "--policy",
        choices=POLICY_PROFILES,
        default=DEFAULT_POLICY_PROFILE,
        help=(
            "Policy profile for the enforcer coordinator (default: "
            "HADES_POLICY_PROFILE env var or 'conservative'). "
            "Also stored as approval_queue.policy_profile for each v2 proposal."
        ),
    )
    run_p.add_argument(
        "--route-ids-file",
        type=Path,
        help=(
            "JSON file containing a route_id allow-list (list, or dict with "
            "pending_route_ids/all_route_ids/route_ids key). Restricts the "
            "claim to these routes — used for per-unit runs (e.g. Quito Centro). "
            "Mutually exclusive with --unit."
        ),
    )
    run_p.add_argument(
        "--unit",
        help=(
            "Unit name (snake_case). Sugar for "
            "--route-ids-file workspace/unit_logs/_assignment_maps/<unit>.json. "
            "Per skill 17 §Cold Start, this is the canonical way to scope a "
            "worker run to one unit. Mutually exclusive with --route-ids-file."
        ),
    )
    run_p.add_argument(
        "--dr-landmarks-glob",
        action="append",
        default=[],
        help=(
            "Glob(s) for validated DR Type 2 landmarks JSON "
            "(e.g. 'workspace/dr_stop_coverage/validated/unit_quito_centro_part*.json'). "
            "Repeatable. Loaded once; Fixer consumes per route."
        ),
    )
    run_p.add_argument(
        "--enable-tier1",
        action="store_true",
        help=(
            "Enable Tier 1 cross-route borrow — queries node_prod.nodes "
            "for existing stops within buffer of each gap. Big unlock "
            "(diagnostic resolved 612 gaps catalog-wide at Tier 1)."
        ),
    )
    run_p.add_argument(
        "--enable-tier2",
        action="store_true",
        help=(
            "Enable Tier 2 Overpass POI — queries self-hosted :12346 "
            "for highway=bus_stop / public_transport=platform|stop_position "
            "within buffer of each gap. Rate-limited 5 req/sec."
        ),
    )
    run_p.add_argument(
        "--strictness",
        choices=("strict", "relaxed"),
        default="strict",
        help=(
            "Fixer promotion gate. 'strict' (default) = v2 must strictly "
            "beat v1 (better class OR same-class with fewer unresolved). "
            "'relaxed' = promote when class did not worsen AND ≥1 Fixer "
            "fill was applied — operator judges quality on review."
        ),
    )
    run_p.add_argument(
        "--clean-outliers",
        action="store_true",
        help=(
            "Before the Fixer cascade, scan each v1 stop. If its distance to "
            "the route polyline > --outlier-threshold-m, the stop is 'off the "
            "normal route trace'. Try to replace it with a DR landmark that "
            "is (a) near the outlier and (b) on-corridor (≤60m from polyline). "
            "If no replacement exists, the stop is dropped. All decisions are "
            "recorded in the v2 report's outlier_audit block."
        ),
    )
    run_p.add_argument(
        "--outlier-threshold-m",
        type=float,
        default=120.0,
        help="Distance-to-polyline (m) above which a stop is an outlier.",
    )
    run_p.add_argument(
        "--mode",
        choices=("gap_fill", "scratch"),
        default="gap_fill",
        help=(
            "Fixer mode. 'gap_fill' (default) — patch v1 stops with tier "
            "cascade fills. 'scratch' — discard v1 stops, rebuild from "
            "scratch using ALL available anchors (Type 1 grounding, Type 2 "
            "DR, cross-route nodes, Overpass POIs, synthetic vertex "
            "fallback). Best for routes where the v1 stop list is "
            "structurally broken and gap-fill can't salvage it."
        ),
    )
    run_p.add_argument(
        "--unit-for-type1",
        default=None,
        help=(
            "Unit slug to load Type 1 grounding responses for "
            "(workspace/research_queue/responses/<unit>/*.json). Used in "
            "scratch mode. Usually matches --unit from run_unit_worker."
        ),
    )
    run_p.add_argument(
        "--enable-sequence-optimizer",
        action="store_true",
        help=(
            "Phase 2 Fixer strategy. After GeometryFixer, if the enforcer "
            "flags DIRECTION_INCONSISTENCY sev>0.6 / OSCILLATION / corridor "
            "IMPOSSIBLE_LOOP, permute stop order and retrace via Valhalla, "
            "promoting the permutation with the highest coherence_score. "
            "Hard-capped to --seq-opt-max-retraces and --seq-opt-timeout-s."
        ),
    )
    run_p.add_argument("--seq-opt-max-retraces", type=int, default=20,
                       help="Max Valhalla retraces per route in sequence optimizer (default 20).")
    run_p.add_argument("--seq-opt-timeout-s", type=float, default=60.0,
                       help="Wall-clock timeout per route in sequence optimizer (default 60s).")
    run_p.add_argument(
        "--enable-troublemaker-swap",
        action="store_true",
        help=(
            "Phase 1.5 — surgical local stop swap. Identifies the "
            "troublemaker stop bordering each anomaly (U_TURN / "
            "IMPOSSIBLE_LOOP / DIRECTION_INCONSISTENCY / OSCILLATION / "
            "BACKTRACK), tries up to 3 local swap kinds per anomaly "
            "(swap_right, swap_left, move_to_skip), accepts ONLY when the "
            "original anomaly is resolved AND no new anomalies are "
            "introduced AND class didn't worsen. Proven 0-regression on "
            "the QC 116-cohort eval; safe to leave on."
        ),
    )
    run_p.add_argument("--tms-max-swaps", type=int, default=5,
                       help="Max accepted swaps per route in troublemaker module (default 5).")
    run_p.add_argument("--tms-max-retraces", type=int, default=15,
                       help="Max Valhalla retraces per route in troublemaker module (default 15).")

    mon_p = sub.add_parser("deadline-monitor",
                           help="Print re-entry burn-down against the deadline.")
    mon_p.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN.")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "run":
        route_ids = None
        # --unit is sugar for --route-ids-file pointing at the assignment map.
        if args.unit and args.route_ids_file:
            print("[re-entry] ERROR: --unit and --route-ids-file are mutually exclusive.")
            return 2
        if args.unit:
            assignment_path = (
                Path(__file__).resolve().parents[2]
                / "workspace" / "unit_logs" / "_assignment_maps"
                / f"{args.unit}.json"
            )
            if not assignment_path.exists():
                print(f"[re-entry] ERROR: assignment map not found for unit {args.unit!r}: {assignment_path}")
                return 2
            args.route_ids_file = assignment_path
        if args.route_ids_file:
            route_ids = _load_route_ids_from_file(args.route_ids_file)
            print(f"[re-entry] unit-scoped: {len(route_ids)} route_ids from {args.route_ids_file}")
        dr_landmarks = None
        if args.dr_landmarks_glob:
            dr_landmarks = load_dr_landmarks(args.dr_landmarks_glob)
        type1_anchors = None
        if args.unit_for_type1 and args.mode == "scratch":
            type1_anchors = load_type1_anchors(args.unit_for_type1)
        summary = run_batch(
            dsn=args.dsn,
            batch_size=args.batch,
            dry_run=args.dry_run,
            verbose=args.verbose,
            policy_profile=args.policy,
            route_ids=route_ids,
            dr_landmarks_by_route=dr_landmarks,
            enable_tier1=args.enable_tier1,
            enable_tier2=args.enable_tier2,
            strictness=args.strictness,
            clean_outliers=args.clean_outliers,
            outlier_threshold_m=args.outlier_threshold_m,
            mode=args.mode,
            type1_anchors_by_route=type1_anchors,
            enable_sequence_optimizer=args.enable_sequence_optimizer,
            sequence_optimizer_max_retraces=args.seq_opt_max_retraces,
            sequence_optimizer_timeout_s=args.seq_opt_timeout_s,
            enable_troublemaker_swap=args.enable_troublemaker_swap,
            troublemaker_max_swaps=args.tms_max_swaps,
            troublemaker_max_retraces=args.tms_max_retraces,
        )
        print("=" * 72)
        print(f"Re-entry worker batch complete (dry_run={args.dry_run})")
        print("=" * 72)
        print(f"Claimed:     {summary['claimed']}")
        print(f"v2_ready:    {summary['v2_ready']}")
        print(f"failed:      {summary['failed']}")
        print(f"quarantined: {summary['quarantined']}")
        print(f"errors:      {summary['errors']}")
        return 0
    if args.cmd == "deadline-monitor":
        summary = deadline_monitor(dsn=args.dsn)
        _print_deadline_monitor(summary)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "run_batch",
    "load_dr_landmarks",
    "deadline_monitor",
    "ProcessOutcome",
    "DEADLINE",
    "MAX_ATTEMPTS",
    "POLICY_PROFILES",
    "DEFAULT_POLICY_PROFILE",
]
