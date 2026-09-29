"""Phase 5 errors.

``PhaseSequenceError`` is raised when Phase 5 detects upstream-pipeline
state that should have been resolved before export. It is the explicit
signal that "you ran Phase 5 too early" — see
``workspace/skills/direction_construction.md`` for the contract.
"""

from __future__ import annotations

from typing import Optional

from phase5_gtfs.common.config import db_conn


class PhaseSequenceError(RuntimeError):
    """Phase 5 cannot proceed because upstream phases are incomplete."""


def assert_no_null_direction_ids(
    *,
    province: Optional[str] = None,
    require_canonical_sequence_ready: bool = True,
    exclude_operator_pending: bool = True,
) -> None:
    """Hard-fail if any GTFS-eligible route has ``direction_id IS NULL``.

    The default scope is "all GTFS-eligible routes" — routes with
    ``canonical_sequence_ready = TRUE`` and an active schedule profile,
    matching the WHERE clause in ``phase5_gtfs/compiler/build_routes.py``.
    Pass ``province`` to narrow further; pass ``require_canonical_sequence_ready=False``
    to widen.

    ``exclude_operator_pending`` (default True) honours the Phase 4.5 contract:
    routes whose latest direction_construction_audit row is ``operator_pending``
    are intentionally NULL and filtered from Phase 5; they are NOT a gate
    violation. Set False to flag them too.
    """
    where_parts = ["r.direction_id IS NULL"]
    if exclude_operator_pending:
        where_parts.append(
            "NOT EXISTS (SELECT 1 FROM node_prod.direction_construction_audit d "
            "WHERE d.route_id = r.route_id AND d.new_state = 'operator_pending')"
        )
    params: list = []
    if require_canonical_sequence_ready:
        where_parts.append("COALESCE(r.canonical_sequence_ready, FALSE) = TRUE")
        where_parts.append("r.chosen_stop_sequence_candidate_id IS NOT NULL")
        where_parts.append(
            "EXISTS (SELECT 1 FROM gtfs_work.route_schedule_profiles sp "
            "WHERE sp.route_id = r.route_id AND sp.is_active = true)"
        )
    if province:
        where_parts.append("r.province = %s")
        params.append(province)
    sql = "SELECT count(*) AS null_count FROM route_prod.routes r WHERE " + " AND ".join(where_parts)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            if row is None:
                null_count = 0
            elif isinstance(row, dict):
                null_count = int(row.get("null_count", 0))
            else:
                null_count = int(row[0])
    if null_count > 0:
        scope = f"province={province!r}" if province else "all GTFS-eligible routes"
        raise PhaseSequenceError(
            f"Phase 4.5 incomplete — {null_count} routes have NULL direction_id "
            f"in {scope}. Run Phase 4.5 to completion or filter operator_pending "
            f"routes before export. See workspace/skills/direction_construction.md."
        )


def assert_no_unapproved_semantics(
    *,
    province: Optional[str] = None,
    require_canonical_sequence_ready: bool = True,
) -> None:
    """Hard-fail if any GTFS-eligible route has an unapproved catalog row.

    "Approved" means ``catalog.route_semantics.approved = TRUE``. Routes
    flagged ``legacy_grandfathered = TRUE`` are exempted — they're known
    pre-pipeline imports kept active intentionally.

    Mirrors the Phase 4.5 direction_id check pattern. The bypass this
    closes: routes shipping to GTFS via ``deploy_status='active'`` even
    though their catalog row was never operator-approved (most catalog
    data comes from Deep Research; the human review step was being skipped).
    """
    where_parts = [
        "EXISTS (SELECT 1 FROM catalog.route_semantics cs "
        " WHERE cs.route_id = r.route_id "
        " AND (cs.approved = FALSE OR cs.approved IS NULL))",
        "COALESCE(r.legacy_grandfathered, FALSE) = FALSE",
    ]
    params: list = []
    if require_canonical_sequence_ready:
        where_parts.append("COALESCE(r.canonical_sequence_ready, FALSE) = TRUE")
        where_parts.append("r.chosen_stop_sequence_candidate_id IS NOT NULL")
        where_parts.append("r.deploy_status = 'active'")
        where_parts.append(
            "EXISTS (SELECT 1 FROM gtfs_work.route_schedule_profiles sp "
            "WHERE sp.route_id = r.route_id AND sp.is_active = true)"
        )
    if province:
        where_parts.append("r.province = %s")
        params.append(province)
    sql = (
        "SELECT count(*) AS unapproved_count "
        "FROM route_prod.routes r WHERE " + " AND ".join(where_parts)
    )
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            if row is None:
                unapproved = 0
            elif isinstance(row, dict):
                unapproved = int(row.get("unapproved_count", 0))
            else:
                unapproved = int(row[0])
    if unapproved > 0:
        scope = f"province={province!r}" if province else "all GTFS-eligible routes"
        raise PhaseSequenceError(
            f"Phase 4 bypass detected — {unapproved} routes have unapproved "
            f"catalog.route_semantics rows in {scope} (and are not "
            f"legacy_grandfathered). Approve the catalog rows or set "
            f"legacy_grandfathered = TRUE before export."
        )
