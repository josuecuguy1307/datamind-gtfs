"""Human review queues that had no console screen: synthetic stops and
approval proposals outside Re-Entry Review.

Principle: the pipeline proposes, a person decides. Nothing here decides on
its own. Reads are read-only. Each write is one explicit operator decision,
applied in a single transaction together with its audit row in
``console.phase_decisions``: if the audit insert fails, the decision is
rolled back, so there is never an unrecorded decision. Every write is
guarded on the row still being ``pending``; a second decision on the same
item is refused, not overwritten.

What a decision means (kept to what the schema already defines):

* synthetic stop: ``node_prod.nodes.synthetic_review_state`` goes
  ``pending -> verified`` or ``pending -> rejected``. Per the column comment,
  verified means the operator approved and rejected means the operator
  rejected, with the row retained. Route stop sequences are not edited here;
  the pipeline acts on the state.
* other proposals: ``route_prod.approval_queue`` rows without a re-entry
  link can be rejected with a reason (``status='rejected'``). They cannot be
  approved here: the swap service refuses rows without ``re_entry_queue_id``,
  and ``unroutable`` proposals are not shippable.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from datamind_console.db.db import fetch_one, query_all, query_one, transaction

QueryAll = Callable[[str, Optional[Sequence]], list]
QueryOne = Callable[[str, Optional[Sequence]], Optional[dict]]

SYNTHETIC_DECISIONS = {"verified": ("APPROVE", "synthetic_stop_verified"), "rejected": ("REJECT", "synthetic_stop_rejected")}
PROPOSAL_REJECT_REASON = "proposal_rejected"
MIN_REASON_CHARS = 10
PAGE_SIZE = 25


class ReviewConflict(RuntimeError):
    """The item is no longer pending (decided elsewhere, or does not exist)."""


class ReviewInputError(ValueError):
    """The decision is incomplete (reviewer or reason missing)."""


@dataclass(frozen=True)
class Reviewer:
    """Who decides. ``user_id`` is set for authenticated sessions; local mode
    has no accounts, so the reviewer types a name, kept in the audit row."""

    name: str
    user_id: Optional[str] = None

    def validated(self) -> "Reviewer":
        name = (self.name or "").strip()
        if len(name) < 2:
            raise ReviewInputError("reviewer_required")
        uid = None
        if self.user_id:
            try:
                uid = str(uuid.UUID(str(self.user_id)))
            except ValueError:
                uid = None
        return Reviewer(name=name, user_id=uid)


def _audit_notes(reviewer: Reviewer, text: str) -> str:
    text = (text or "").strip()
    return f"[reviewer: {reviewer.name}] {text}".strip()


def _insert_decision(conn, *, phase: int, item_id: str, decision: str, reason_code: str, notes: str, user_id: Optional[str]) -> dict:
    row = fetch_one(
        conn,
        """
        INSERT INTO console.phase_decisions (phase, item_id, decision, reason_code, notes, user_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING decision_id::text AS decision_id, created_at
        """,
        (phase, item_id, decision, reason_code, notes, user_id),
    )
    return dict(row or {})


# ---------------------------------------------------------------------------
# Synthetic stops
# ---------------------------------------------------------------------------

SYNTHETIC_FILTER_SQL = """
  n.synthetic_review_state = 'pending'
  AND (%(confidence)s::text IS NULL OR n.synthetic_confidence = %(confidence)s::text)
  AND (%(province)s::text IS NULL OR n.province = %(province)s::text)
  AND (%(superseded)s::text IS NULL
       OR (%(superseded)s = 'yes' AND n.superseded_by IS NOT NULL)
       OR (%(superseded)s = 'no' AND n.superseded_by IS NULL))
  AND (%(usage)s::text IS NULL
       OR (%(usage)s = 'used' AND u.n_routes > 0)
       OR (%(usage)s = 'unused' AND COALESCE(u.n_routes, 0) = 0))
  AND (%(name)s::text IS NULL OR n.name ILIKE '%%' || %(name)s::text || '%%')
"""

SYNTHETIC_USAGE_CTE = """
WITH refs AS (
  SELECT DISTINCT unnest(r.stop_node_ids) AS node_id, r.route_id, r.deploy_status
  FROM route_prod.routes r
),
u AS (
  SELECT node_id,
         count(*) AS n_routes,
         count(*) FILTER (WHERE deploy_status = 'active') AS n_deployed
  FROM refs GROUP BY node_id
)
"""

SYNTHETIC_PAGE_SQL = SYNTHETIC_USAGE_CTE + """
SELECT n.node_id::text AS node_id,
       n.name,
       n.province,
       n.synthetic_confidence AS confidence,
       n.source_type,
       n.synthetic_created_at AS created_at,
       (n.superseded_by IS NOT NULL) AS superseded,
       COALESCE(u.n_routes, 0) AS n_routes,
       COALESCE(u.n_deployed, 0) AS n_deployed,
       ST_Y(n.geom) AS lat,
       ST_X(n.geom) AS lon
FROM node_prod.nodes n
LEFT JOIN u ON u.node_id = n.node_id
WHERE """ + SYNTHETIC_FILTER_SQL + """
ORDER BY COALESCE(u.n_deployed, 0) DESC, n.synthetic_created_at DESC, n.node_id
LIMIT %(limit)s OFFSET %(offset)s
"""

SYNTHETIC_COUNT_SQL = SYNTHETIC_USAGE_CTE + """
SELECT count(*) AS n
FROM node_prod.nodes n
LEFT JOIN u ON u.node_id = n.node_id
WHERE """ + SYNTHETIC_FILTER_SQL

SYNTHETIC_SUMMARY_SQL = """
SELECT synthetic_review_state AS state, count(*) AS n
FROM node_prod.nodes
WHERE synthetic_review_state IS NOT NULL
GROUP BY 1
"""

SYNTHETIC_DETAIL_SQL = """
SELECT n.node_id::text AS node_id, n.name, n.province, n.node_type,
       n.synthetic_confidence AS confidence, n.source, n.source_type,
       n.synthetic_created_at AS created_at, n.synthetic_created_by AS created_by,
       n.synthetic_review_state AS review_state,
       n.superseded_by::text AS superseded_by,
       n.poi_anchor_class, n.poi_to_path_distance_m, n.path_projection_distance_m,
       n.research_to_projection_distance_m, n.semantic_spatial_conflict,
       ST_Y(n.geom) AS lat, ST_X(n.geom) AS lon
FROM node_prod.nodes n
WHERE n.node_id = %s::uuid
"""

SYNTHETIC_ROUTES_SQL = """
SELECT r.route_id::text AS route_id, r.route_name, r.direction_id, r.deploy_status,
       r.cleanliness_status, array_position(r.stop_node_ids, %s::uuid) AS position,
       COALESCE(array_length(r.stop_node_ids, 1), 0) AS n_stops
FROM route_prod.routes r
WHERE %s::uuid = ANY (r.stop_node_ids)
ORDER BY (r.deploy_status = 'active') DESC, r.route_name
"""

SYNTHETIC_NEIGHBOURS_SQL = """
SELECT o.node_id::text AS node_id, o.name, o.node_type,
       (o.synthetic_review_state IS NOT NULL) AS synthetic,
       round(ST_Distance(o.geom::geography, n.geom::geography)::numeric, 1) AS distance_m,
       ST_Y(o.geom) AS lat, ST_X(o.geom) AS lon
FROM node_prod.nodes n
JOIN node_prod.nodes o
  ON o.node_id <> n.node_id
 AND ST_DWithin(o.geom::geography, n.geom::geography, %s)
WHERE n.node_id = %s::uuid AND o.node_type = 'STOP'
ORDER BY distance_m
LIMIT 10
"""


def _synthetic_params(filters: dict[str, Any]) -> dict[str, Any]:
    def clean(key: str) -> Optional[str]:
        value = filters.get(key)
        value = str(value).strip() if value not in (None, "") else None
        return value
    return {
        "confidence": clean("confidence"),
        "province": clean("province"),
        "superseded": clean("superseded"),
        "usage": clean("usage"),
        "name": clean("name"),
    }


def synthetic_summary(*, query: QueryAll = query_all) -> dict[str, int]:
    return {str(r["state"]): int(r["n"]) for r in query(SYNTHETIC_SUMMARY_SQL, None)}


def synthetic_page(filters: dict[str, Any], *, page: int = 0, page_size: int = PAGE_SIZE,
                   query_rows: QueryAll = query_all, query_row: QueryOne = query_one) -> tuple[list[dict], int]:
    params = _synthetic_params(filters)
    total = int((query_row(SYNTHETIC_COUNT_SQL, params) or {}).get("n") or 0)
    page_params = dict(params, limit=int(page_size), offset=int(max(page, 0)) * int(page_size))
    return [dict(r) for r in query_rows(SYNTHETIC_PAGE_SQL, page_params)], total


def synthetic_detail(node_id: str, *, query_rows: QueryAll = query_all, query_row: QueryOne = query_one,
                     neighbour_radius_m: float = 80.0) -> dict[str, Any]:
    node = query_row(SYNTHETIC_DETAIL_SQL, (node_id,))
    if not node:
        return {}
    return {
        "node": dict(node),
        "routes": [dict(r) for r in query_rows(SYNTHETIC_ROUTES_SQL, (node_id, node_id))],
        "neighbours": [dict(r) for r in query_rows(SYNTHETIC_NEIGHBOURS_SQL, (neighbour_radius_m, node_id))],
    }


def decide_synthetic_stop(node_id: str, decision: str, *, reviewer: Reviewer, reason: str = "",
                          transaction_factory: Callable = transaction) -> dict[str, Any]:
    """Record one human decision on one pending synthetic stop."""
    if decision not in SYNTHETIC_DECISIONS:
        raise ReviewInputError("unknown_decision")
    who = reviewer.validated()
    reason = (reason or "").strip()
    if decision == "rejected" and len(reason) < MIN_REASON_CHARS:
        raise ReviewInputError("reason_required")
    audit_decision, reason_code = SYNTHETIC_DECISIONS[decision]
    with transaction_factory() as conn:
        updated = fetch_one(
            conn,
            """
            UPDATE node_prod.nodes
               SET synthetic_review_state = %s
             WHERE node_id = %s::uuid
               AND synthetic_review_state = 'pending'
            RETURNING node_id::text AS node_id
            """,
            (decision, node_id),
        )
        if not updated:
            raise ReviewConflict("not_pending")
        audit = _insert_decision(
            conn, phase=1, item_id=f"node:{node_id}", decision=audit_decision,
            reason_code=reason_code, notes=_audit_notes(who, reason), user_id=who.user_id,
        )
    return {"node_id": node_id, "state": decision, **audit}


# ---------------------------------------------------------------------------
# Approval proposals outside Re-Entry Review
# ---------------------------------------------------------------------------

PROPOSALS_SQL = """
SELECT q.queue_id::text AS queue_id, q.route_code, q.version, q.quality_class,
       q.enqueued_at, q.policy_profile,
       COALESCE(jsonb_array_length(CASE WHEN jsonb_typeof(q.proposed_stops) = 'array' THEN q.proposed_stops END), 0) AS n_proposed_stops,
       COALESCE(r.route_name, NULLIF(j.known_ref, ''), NULLIF(j.notes, ''), j.area_key, q.route_code) AS route_label,
       j.status AS job_status, COALESCE(r.province, j.province) AS province,
       (r.route_id IS NOT NULL) AS in_production,
       -- Copies of one candidate often share version 1; rank them by queue time.
       row_number() OVER (PARTITION BY q.route_code ORDER BY q.enqueued_at DESC, q.queue_id) AS copy_rank,
       count(*) OVER (PARTITION BY q.route_code) AS copies_pending
FROM route_prod.approval_queue q
LEFT JOIN route_prod.routes r ON r.route_id::text = q.route_code
LEFT JOIN route_raw.route_jobs j ON j.route_id::text = q.route_code
WHERE q.status = 'pending'
  AND NOT (COALESCE(q.policy_flags, '{}'::jsonb) ? 're_entry_queue_id')
ORDER BY route_label, q.route_code, q.enqueued_at DESC
"""

PROPOSAL_DETAIL_SQL = """
SELECT q.queue_id::text AS queue_id, q.route_code, q.version, q.status, q.quality_class,
       q.enqueued_at, q.policy_profile, q.decision_reasons, q.policy_flags,
       q.geometry_report, q.stop_coverage_report, q.proposed_stops,
       ST_AsGeoJSON(ST_SetSRID(ST_GeomFromGeoJSON(
         CASE WHEN q.proposed_shape ? 'type' THEN q.proposed_shape::text END), 4326)) AS proposed_shape_geojson,
       q.dr_queries_queued, q.tier4_pending_count,
       COALESCE(r.route_name, NULLIF(j.known_ref, ''), NULLIF(j.notes, ''), j.area_key, q.route_code) AS route_label,
       j.status AS job_status, j.area_key, COALESCE(r.province, j.province) AS province,
       r.deploy_status, r.cleanliness_status,
       ST_AsGeoJSON(r.geom) AS current_geojson
FROM route_prod.approval_queue q
LEFT JOIN route_prod.routes r ON r.route_id::text = q.route_code
LEFT JOIN route_raw.route_jobs j ON j.route_id::text = q.route_code
WHERE q.queue_id = %s::uuid
"""


def proposals(*, query: QueryAll = query_all) -> list[dict]:
    return [dict(r) for r in query(PROPOSALS_SQL, None)]


def proposal_detail(queue_id: str, *, query_row: QueryOne = query_one) -> dict[str, Any]:
    return dict(query_row(PROPOSAL_DETAIL_SQL, (queue_id,)) or {})


def why_not_approvable(row: dict[str, Any]) -> list[str]:
    """Reasons a proposal from this queue cannot be shipped from the console."""
    reasons = ["no_reentry_link"]
    if row.get("quality_class") in ("unroutable", "unknown", None):
        reasons.append("not_shippable_class")
    return reasons


def reject_proposals(queue_ids: Sequence[str], *, reviewer: Reviewer, reason: str,
                     transaction_factory: Callable = transaction) -> list[dict[str, Any]]:
    """Reject one or more pending proposals with one shared reason.

    All-or-nothing: if any selected row is no longer pending, nothing is
    written. Each rejected row gets its own audit row.
    """
    who = reviewer.validated()
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON_CHARS:
        raise ReviewInputError("reason_required")
    ids = [str(uuid.UUID(str(q))) for q in queue_ids]
    if not ids:
        raise ReviewInputError("nothing_selected")
    results = []
    with transaction_factory() as conn:
        for queue_id in ids:
            updated = fetch_one(
                conn,
                """
                UPDATE route_prod.approval_queue
                   SET status = 'rejected',
                       resolved_at = now(),
                       resolved_by = %s,
                       resolution_notes = %s
                 WHERE queue_id = %s::uuid
                   AND status = 'pending'
                   AND NOT (COALESCE(policy_flags, '{}'::jsonb) ? 're_entry_queue_id')
                RETURNING queue_id::text AS queue_id
                """,
                (f"console:{who.name}", reason, queue_id),
            )
            if not updated:
                raise ReviewConflict(f"not_pending:{queue_id}")
            audit = _insert_decision(
                conn, phase=3, item_id=f"approval:{queue_id}", decision="REJECT",
                reason_code=PROPOSAL_REJECT_REASON, notes=_audit_notes(who, reason), user_id=who.user_id,
            )
            results.append({"queue_id": queue_id, **audit})
    return results


RECENT_DECISIONS_SQL = """
SELECT created_at, phase, item_id, decision, reason_code, notes
FROM console.phase_decisions
WHERE item_id LIKE %s
ORDER BY created_at DESC
LIMIT %s
"""


def recent_decisions(prefix: str, limit: int = 15, *, query: QueryAll = query_all) -> list[dict]:
    return [dict(r) for r in query(RECENT_DECISIONS_SQL, (f"{prefix}:%", int(limit)))]
