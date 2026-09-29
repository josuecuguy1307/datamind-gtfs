"""
Append-only audit log for path-aware synthesis attempts.

One row per ``node_prod.synthesis_events`` insert. Never UPDATE, never DELETE.
This module owns the write path and the read helpers (counts for per-route
cap-accounting, per-unit-week calibration, event timeline for a node).

**Scope boundary.** This module does NOT construct the free-text ``source``
audit string. That is the caller's job (the Phase 2 synthesis core). Callers
pass only the enum ``stage`` plus structured columns; this module writes what
it was given and validates only ``stage`` against the CHECK constraint.
``source`` and ``source_type`` live on ``node_prod.nodes`` and are written
by the node insert, not by this event log.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

VALID_STAGES: frozenset[str] = frozenset({
    "3a_poi_on_path",
    "3b_path_corridor",
    "3c_path_intersection",
    "3d_research_coords_snapped",
    "4_pure_synthesis",
    "osm_route_fill",
})

# Per-spec hades-path-aware-synthesis §8
_STAGE_WEIGHTS: dict[str, float] = {
    "3a_poi_on_path": 0.0,
    "3b_path_corridor": 0.0,
    "3c_path_intersection": 0.0,
    "3d_research_coords_snapped": 0.5,
    "4_pure_synthesis": 1.0,
    "osm_route_fill": 0.0,
}

ROUTE_CAP_WEIGHTED_UNITS: float = 3.0


def stage_weight(stage: str) -> float:
    if stage not in _STAGE_WEIGHTS:
        raise ValueError(f"unknown stage: {stage!r}")
    return _STAGE_WEIGHTS[stage]


def is_route_at_cap(consumed: float) -> bool:
    return consumed >= ROUTE_CAP_WEIGHTED_UNITS


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_nonempty(name: str, value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _coords_or_none(name: str, coords: Any) -> tuple[Optional[float], Optional[float]]:
    if coords is None:
        return (None, None)
    if not isinstance(coords, (tuple, list)) or len(coords) != 2:
        raise ValueError(f"{name} must be a (lat, lon) pair or None, got {coords!r}")
    lat, lon = coords
    return (float(lat), float(lon))


def _validate_stage(stage: str) -> str:
    if stage not in VALID_STAGES:
        raise ValueError(
            f"stage {stage!r} is not one of {sorted(VALID_STAGES)}"
        )
    return stage


def _to_basename(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    return os.path.basename(path) or path


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


_INSERT_SQL = """
INSERT INTO node_prod.synthesis_events (
    node_id, route_id, unit, province,
    stage, anchor_name,
    research_coords_lat, research_coords_lon,
    final_coords_lat, final_coords_lon,
    match_score, rejected_reason,
    triggered_by_skill, research_output_file
) VALUES (
    %s, %s, %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s,
    %s, %s
)
RETURNING id, created_at, node_id, route_id, stage
""".strip()


def log_synthesis_event(
    conn,
    *,
    node_id: Optional[str],
    route_id: Optional[str],
    unit: str,
    province: str,
    stage: str,
    anchor_name: Optional[str] = None,
    research_coords: Optional[tuple[float, float]] = None,
    final_coords: Optional[tuple[float, float]] = None,
    match_score: Optional[float] = None,
    rejected_reason: Optional[str] = None,
    triggered_by_skill: str = "",
    research_output_file: Optional[str] = None,
) -> dict:
    """Append one event row to ``node_prod.synthesis_events``.

    The free-text ``source`` column on ``node_prod.nodes`` and its companion
    ``source_type`` enum are written by the caller during the node insert —
    this module does not touch them. See module docstring.
    """
    _validate_stage(stage)
    _require_nonempty("unit", unit)
    _require_nonempty("province", province)
    _require_nonempty("triggered_by_skill", triggered_by_skill)

    rc_lat, rc_lon = _coords_or_none("research_coords", research_coords)
    fc_lat, fc_lon = _coords_or_none("final_coords", final_coords)

    params = (
        node_id, route_id, unit, province,
        stage, anchor_name,
        rc_lat, rc_lon,
        fc_lat, fc_lon,
        match_score, rejected_reason,
        triggered_by_skill, _to_basename(research_output_file),
    )

    with conn.cursor() as cur:
        cur.execute(_INSERT_SQL, params)
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("synthesis_events insert returned no row")
    return dict(row)


# ---------------------------------------------------------------------------
# reads — cap accounting and calibration
# ---------------------------------------------------------------------------


def count_synthesis_events_for_route(
    conn, *, route_id: str, succeeded_only: bool = True
) -> int:
    clauses = ["route_id = %s"]
    params: list[Any] = [route_id]
    if succeeded_only:
        clauses.append("rejected_reason IS NULL")
    sql = f"SELECT COUNT(*) AS n FROM node_prod.synthesis_events WHERE {' AND '.join(clauses)}"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    return int((row or {}).get("n", 0))


def count_pure_synthesis_events_for_route(conn, *, route_id: str) -> int:
    sql = (
        "SELECT COUNT(*) AS n FROM node_prod.synthesis_events "
        "WHERE route_id = %s AND stage = %s AND rejected_reason IS NULL"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (route_id, "4_pure_synthesis"))
        row = cur.fetchone()
    return int((row or {}).get("n", 0))


def compute_route_cap_consumed(conn, *, route_id: str) -> float:
    """Sum weighted-unit contributions from all succeeded events on a route.

    Follows §8 of hades-path-aware-synthesis: stages 3d @ 0.5, stage 4 @ 1.0,
    everything else @ 0.0. Cap trip occurs at 3.0.
    """
    counted = ["3d_research_coords_snapped", "4_pure_synthesis"]
    total = 0.0
    with conn.cursor() as cur:
        for stage in counted:
            cur.execute(
                "SELECT COUNT(*) AS n FROM node_prod.synthesis_events "
                "WHERE route_id = %s AND stage = %s AND rejected_reason IS NULL",
                (route_id, stage),
            )
            row = cur.fetchone() or {}
            total += _STAGE_WEIGHTS[stage] * int(row.get("n", 0))
    return total


def count_synthesis_events_for_unit_week(
    conn,
    *,
    unit: str,
    reference: Optional[datetime] = None,
    succeeded_only: bool = True,
) -> int:
    _require_nonempty("unit", unit)
    reference = reference or datetime.now(timezone.utc)
    since = reference - timedelta(days=7)

    clauses = ["unit = %s", "created_at >= %s"]
    params: list[Any] = [unit, since]
    if succeeded_only:
        clauses.append("rejected_reason IS NULL")
    sql = f"SELECT COUNT(*) AS n FROM node_prod.synthesis_events WHERE {' AND '.join(clauses)}"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
    return int((row or {}).get("n", 0))


def get_events_for_node(conn, *, node_id: str) -> list[dict]:
    sql = (
        "SELECT id, created_at, node_id, route_id, unit, province, stage, "
        "anchor_name, research_coords_lat, research_coords_lon, "
        "final_coords_lat, final_coords_lon, match_score, rejected_reason, "
        "triggered_by_skill, research_output_file "
        "FROM node_prod.synthesis_events "
        "WHERE node_id = %s "
        "ORDER BY created_at DESC"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (node_id,))
        rows = cur.fetchall()
    return [dict(r) for r in (rows or [])]


def get_calibration_stats(conn) -> dict:
    """Pure-synthesis fraction + per-stage counts across all succeeded events.

    Matches the canary metric described in hades-path-aware-synthesis §11
    ("Canary metric: pure-synthesis fraction").
    """
    sql = (
        "SELECT stage, COUNT(*) AS n "
        "FROM node_prod.synthesis_events "
        "WHERE rejected_reason IS NULL "
        "GROUP BY stage"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall() or []

    by_stage: dict[str, int] = {}
    total = 0
    for r in rows:
        stage = r["stage"]
        n = int(r["n"])
        by_stage[stage] = n
        total += n
    pure = by_stage.get("4_pure_synthesis", 0)
    fraction = (pure / total) if total > 0 else 0.0
    return {
        "total_succeeded": total,
        "by_stage": by_stage,
        "pure_synthesis_fraction": fraction,
    }
