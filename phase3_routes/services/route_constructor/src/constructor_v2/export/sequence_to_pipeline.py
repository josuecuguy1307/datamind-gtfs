"""
Constructor V2 → Phase 3 Pipeline Bridge
=========================================

Takes a V2 artifact (ordered stop sequences) and injects them into
the Phase 3 pipeline so Step 30 (geometry) can consume them.

V2 produces SEQUENCES ONLY. Step 30+ stays as-is.

Input:  V2 artifact JSON (valle_v2_TUNED_39.json)
Output: DB records in route_work.stop_sequence_candidate_sets
        + route_work.stop_sequence_candidates
        + route_work.sequence_approvals (auto-approved)

Usage:
    from constructor_v2.export.sequence_to_pipeline import inject_v2_sequence
    inject_v2_sequence(conn, route_id=uuid, direction_id=0, ordered_stops=[...])
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass
class InjectionResult:
    route_name: str
    route_id: uuid.UUID
    direction_id: int
    set_id: uuid.UUID
    candidate_id: uuid.UUID
    stops_injected: int
    stops_resolved: int
    stops_unresolved: int
    unresolved_ids: list[str] = field(default_factory=list)
    auto_approved: bool = False


def _is_canonical_uuid(stop_id: str) -> bool:
    """Check if a stop_id is a canonical UUID (from node_prod.nodes)."""
    if stop_id.startswith(("synthetic:", "hint_", "gtfs_", "corridor_")):
        return False
    try:
        uuid.UUID(stop_id)
        return True
    except ValueError:
        return False


def _resolve_non_uuid_stops(
    conn,
    stops: list[dict],
    *,
    max_match_radius_m: float = 80.0,
) -> dict[str, Optional[uuid.UUID]]:
    """
    For non-UUID stop_ids, find the nearest canonical stop in node_prod.nodes
    within max_match_radius_m using PostGIS.

    Returns: {non_uuid_stop_id: canonical_uuid_or_None}
    """
    non_uuid = [s for s in stops if not _is_canonical_uuid(s["stop_id"])]
    if not non_uuid:
        return {}

    result: dict[str, Optional[uuid.UUID]] = {}
    with conn.cursor() as cur:
        for s in non_uuid:
            cur.execute(
                """
                SELECT node_id, ST_Distance(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                ) AS dist_m
                FROM node_prod.nodes
                WHERE node_type = 'STOP'
                  AND ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    %s
                  )
                ORDER BY dist_m ASC
                LIMIT 1
                """,
                (s["lon"], s["lat"], s["lon"], s["lat"], max_match_radius_m),
            )
            row = cur.fetchone()
            if row:
                nid = row[0] if not hasattr(row, "get") else row.get("node_id", row[0])
                result[s["stop_id"]] = uuid.UUID(str(nid))
            else:
                result[s["stop_id"]] = None
                log.warning(
                    "No canonical stop within %.0fm of %s (%.6f, %.6f)",
                    max_match_radius_m,
                    s["stop_id"],
                    s["lat"],
                    s["lon"],
                )
    return result


def inject_v2_sequence(
    conn,
    *,
    route_id: uuid.UUID,
    direction_id: int,
    route_name: str,
    ordered_stops: list[dict],
    classification: str = "unknown",
    confidence_score: float = 0.0,
    auto_approve: bool = True,
    approved_by: str = "constructor_v2",
    max_match_radius_m: float = 80.0,
) -> InjectionResult:
    """
    Inject a single V2 route sequence into the Phase 3 pipeline.

    Steps:
    1. Resolve all stop_ids to canonical node_prod.nodes UUIDs
    2. Create stop_sequence_candidate_sets record
    3. Insert stop_sequence_candidates record (rank=1)
    4. Optionally auto-approve via sequence_approvals

    Args:
        conn: psycopg2 connection
        route_id: route_raw.route_jobs.route_id
        direction_id: 0 or 1
        route_name: human-readable route name
        ordered_stops: list of V2 stop dicts with stop_id, lat, lon, seq
        classification: V2 classification label
        confidence_score: V2 confidence score (0-100)
        auto_approve: whether to auto-approve the sequence
        approved_by: approver identifier
        max_match_radius_m: radius for resolving non-UUID stops

    Returns:
        InjectionResult with set_id, candidate_id, resolution stats
    """
    # Step 1: Resolve stop IDs
    non_uuid_map = _resolve_non_uuid_stops(
        conn, ordered_stops, max_match_radius_m=max_match_radius_m
    )

    canonical_ids: list[uuid.UUID] = []
    unresolved: list[str] = []
    for s in ordered_stops:
        sid = s["stop_id"]
        if _is_canonical_uuid(sid):
            canonical_ids.append(uuid.UUID(sid))
        elif non_uuid_map.get(sid) is not None:
            canonical_ids.append(non_uuid_map[sid])
        else:
            unresolved.append(sid)
            # Skip unresolved stops - they won't appear in the sequence

    if not canonical_ids:
        raise ValueError(
            f"No canonical stops resolved for route '{route_name}'. "
            f"All {len(ordered_stops)} stops were unresolvable."
        )

    # Deduplicate consecutive identical UUIDs (can happen after resolution)
    deduped: list[uuid.UUID] = [canonical_ids[0]]
    for uid in canonical_ids[1:]:
        if uid != deduped[-1]:
            deduped.append(uid)
    canonical_ids = deduped

    # Step 2: Create candidate set
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_work.stop_sequence_candidate_sets
              (route_id, notes, generator_version)
            VALUES (%s, %s, %s)
            RETURNING set_id
            """,
            (
                str(route_id),
                f"constructor_v2 | {route_name} | d{direction_id} | "
                f"{classification} | confidence={confidence_score:.1f}",
                "constructor_v2",
            ),
        )
        row = cur.fetchone()
        set_id_val = row[0] if not hasattr(row, "get") else row.get("set_id", row[0])
        set_id = uuid.UUID(str(set_id_val))

    # Step 3: Insert candidate (rank 1 = best)
    metrics = {
        "source": "constructor_v2",
        "classification": classification,
        "confidence_score": confidence_score,
        "direction_id": direction_id,
        "stops_input": len(ordered_stops),
        "stops_resolved": len(canonical_ids),
        "stops_unresolved": len(unresolved),
        "unresolved_ids": unresolved,
        "family": "constructor_v2",
        "label": f"V2 {classification} (confidence={confidence_score:.1f})",
        "candidate_generation_version": "constructor_v2",
    }

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_work.stop_sequence_candidates
              (set_id, rank, stop_node_ids, stop_prior_seqs, metrics,
               matched_stops, avg_match_dist_m, max_match_dist_m)
            VALUES (%s, %s, %s::uuid[], %s::int[], %s::jsonb, %s, %s, %s)
            RETURNING candidate_id
            """,
            (
                str(set_id),
                1,  # rank=1, best
                [str(uid) for uid in canonical_ids],
                [],  # no prior seqs for V2
                json.dumps(metrics, ensure_ascii=False),
                len(canonical_ids),
                0.0,
                0.0,
            ),
        )
        row = cur.fetchone()
        cand_val = row[0] if not hasattr(row, "get") else row.get("candidate_id", row[0])
        candidate_id = uuid.UUID(str(cand_val))

    # Step 4: Auto-approve
    approved = False
    if auto_approve:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO route_work.sequence_approvals
                  (route_id, stop_sequence_set_id,
                   chosen_stop_sequence_candidate_id,
                   approval_status, approved_at, approved_by, notes)
                VALUES (%s, %s, %s, 'approved', now(), %s, %s)
                ON CONFLICT (route_id) DO UPDATE SET
                  stop_sequence_set_id = EXCLUDED.stop_sequence_set_id,
                  chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
                  approval_status = 'approved',
                  approved_at = now(),
                  approved_by = EXCLUDED.approved_by,
                  notes = EXCLUDED.notes,
                  invalidated_at = NULL,
                  invalidated_reason = NULL
                """,
                (
                    str(route_id),
                    str(set_id),
                    str(candidate_id),
                    approved_by,
                    f"Auto-approved by V2: {classification} confidence={confidence_score:.1f}",
                ),
            )
        approved = True

    conn.commit()

    return InjectionResult(
        route_name=route_name,
        route_id=route_id,
        direction_id=direction_id,
        set_id=set_id,
        candidate_id=candidate_id,
        stops_injected=len(canonical_ids),
        stops_resolved=len(canonical_ids),
        stops_unresolved=len(unresolved),
        unresolved_ids=unresolved,
        auto_approved=approved,
    )


def resolve_route_id(
    conn,
    route_name: str,
    direction_id: int = 0,
) -> Optional[uuid.UUID]:
    """
    Look up route_id from route_review.phase3_global_catalog_v1
    by matching the route name against service_route_name or route_hint.
    Falls back to route_raw.route_jobs with text search on notes.
    """
    with conn.cursor() as cur:
        # Try catalog view: service_route_name match
        cur.execute(
            """
            SELECT route_job_id
            FROM route_review.phase3_global_catalog_v1
            WHERE service_route_name = %s
              AND direction_id = %s
            LIMIT 1
            """,
            (route_name, direction_id),
        )
        row = cur.fetchone()
        if row:
            val = row[0] if not hasattr(row, "get") else row.get("route_job_id", row[0])
            return uuid.UUID(str(val))

        # Try catalog: ILIKE match on service_route_name
        cur.execute(
            """
            SELECT route_job_id
            FROM route_review.phase3_global_catalog_v1
            WHERE service_route_name ILIKE %s
              AND direction_id = %s
            LIMIT 1
            """,
            (f"%{route_name}%", direction_id),
        )
        row = cur.fetchone()
        if row:
            val = row[0] if not hasattr(row, "get") else row.get("route_job_id", row[0])
            return uuid.UUID(str(val))

        # Try catalog: route_hint match
        cur.execute(
            """
            SELECT route_job_id
            FROM route_review.phase3_global_catalog_v1
            WHERE route_hint ILIKE %s
              AND direction_id = %s
            LIMIT 1
            """,
            (f"%{route_name}%", direction_id),
        )
        row = cur.fetchone()
        if row:
            val = row[0] if not hasattr(row, "get") else row.get("route_job_id", row[0])
            return uuid.UUID(str(val))

        # Fallback: search without direction filter
        cur.execute(
            """
            SELECT route_job_id
            FROM route_review.phase3_global_catalog_v1
            WHERE service_route_name ILIKE %s
               OR route_hint ILIKE %s
            ORDER BY direction_id ASC
            LIMIT 1
            """,
            (f"%{route_name}%", f"%{route_name}%"),
        )
        row = cur.fetchone()
        if row:
            val = row[0] if not hasattr(row, "get") else row.get("route_job_id", row[0])
            return uuid.UUID(str(val))

    return None


def batch_inject_from_artifact(
    conn,
    artifact_path: str,
    *,
    direction_id: int = 0,
    min_confidence: float = 0.0,
    classifications: Optional[set[str]] = None,
    auto_approve: bool = True,
    dry_run: bool = False,
    route_id_overrides: Optional[dict[str, str]] = None,
) -> list[InjectionResult]:
    """
    Batch inject V2 sequences from an artifact JSON file.

    Args:
        conn: psycopg2 connection
        artifact_path: path to V2 artifact JSON
        direction_id: direction to inject (0 or 1)
        min_confidence: minimum confidence score to inject
        classifications: allowed classifications (None = all)
        auto_approve: auto-approve injected sequences
        dry_run: if True, don't write to DB
        route_id_overrides: {route_name: route_id_str} for manual mapping

    Returns:
        list of InjectionResult
    """
    with open(artifact_path) as f:
        artifact = json.load(f)

    routes = artifact.get("routes", [])
    overrides = route_id_overrides or {}
    results: list[InjectionResult] = []
    skipped: list[str] = []

    for route in routes:
        name = route["route"]
        classification = route.get("classification", "unknown")
        confidence = route.get("confidence", {}).get("score", 0.0)
        ordered_stops = route.get("ordered_stops", [])

        # Filter
        if confidence < min_confidence:
            skipped.append(f"{name}: confidence {confidence:.1f} < {min_confidence}")
            continue
        if classifications and classification not in classifications:
            skipped.append(f"{name}: classification '{classification}' not in allowed set")
            continue
        if not ordered_stops:
            skipped.append(f"{name}: no ordered stops")
            continue

        # Resolve route_id
        if name in overrides:
            route_id = uuid.UUID(overrides[name])
        else:
            route_id = resolve_route_id(conn, name, direction_id)

        if route_id is None:
            skipped.append(f"{name}: could not resolve route_id in route_raw.route_jobs")
            continue

        if dry_run:
            log.info("[DRY RUN] Would inject %s (d%d) -> route_id=%s", name, direction_id, route_id)
            continue

        try:
            result = inject_v2_sequence(
                conn,
                route_id=route_id,
                direction_id=direction_id,
                route_name=name,
                ordered_stops=ordered_stops,
                classification=classification,
                confidence_score=confidence,
                auto_approve=auto_approve,
            )
            results.append(result)
            log.info(
                "Injected %s (d%d): %d stops (%d unresolved) -> set=%s",
                name,
                direction_id,
                result.stops_injected,
                result.stops_unresolved,
                result.set_id,
            )
        except Exception as e:
            log.error("Failed to inject %s: %s", name, e)
            skipped.append(f"{name}: {e}")

    if skipped:
        log.info("Skipped %d routes:", len(skipped))
        for s in skipped:
            log.info("  %s", s)

    return results
