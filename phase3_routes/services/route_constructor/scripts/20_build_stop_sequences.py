from __future__ import annotations

import json
import sys
import uuid
from dotenv import load_dotenv

load_dotenv()

from src.db.conn import db_conn, db_cursor
from src.db.route_raw_repo import fetch_relation_raw
from src.db.route_work_repo import replace_stop_prior  # ✅ NEW: persist prior
from src.evidence.parse_relation import extract_stop_prior
from src.sequence.candidates import build_sequence_candidates


def _print_candidates(conn, set_id: uuid.UUID) -> None:
    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT candidate_id, rank, metrics, stop_node_ids
            FROM route_work.stop_sequence_candidates
            WHERE set_id=%s
            ORDER BY rank ASC
            """,
            (str(set_id),),
        )
        rows = cur.fetchall()

    if not rows:
        print("No candidates inserted (did not meet MIN_MATCHED_STOPS thresholds).")
        return

    print("\nCandidates created:")
    for r in rows:
        metrics = r.get("metrics") or {}
        stop_ids = r.get("stop_node_ids") or []

        matched_stops = metrics.get("matched_stops", len(stop_ids))
        avg_m = metrics.get("avg_match_dist_m")
        max_m = metrics.get("max_match_dist_m")

        print(
            f"  rank={r.get('rank')}  candidate_id={r.get('candidate_id')}  "
            f"matched_stops={matched_stops}  avg_m={avg_m}  max_m={max_m}"
        )


def _normalize_and_validate_prior(prior: list[dict]) -> list[dict]:
    """
    Enforce minimum keys and normalize member_type/osm_ref back-compat.
    This keeps the pipeline robust even if extractor changes slightly.
    """
    needed = {"seq", "lat", "lon"}
    bad = [p for p in prior if not needed.issubset(p.keys())]
    if bad:
        raise SystemExit(
            "extract_stop_prior output missing required keys. "
            "Expected at least: seq, lat, lon. "
            f"Bad example: {bad[0]}"
        )

    out: list[dict] = []
    for p in prior:
        q = dict(p)

        # Normalize member_type if present
        mt = q.get("member_type")
        if mt is not None:
            mt = str(mt).strip().lower()
            if mt not in ("node", "way"):
                mt = None
        q["member_type"] = mt

        # Back-compat: allow osm_node_id -> osm_ref
        if q.get("osm_ref") is None and q.get("osm_node_id") is not None:
            q["osm_ref"] = q.get("osm_node_id")
            q["member_type"] = q["member_type"] or "node"

        # If we have osm_ref but no member_type, default to node (safe)
        if q.get("osm_ref") is not None and not q.get("member_type"):
            q["member_type"] = "node"

        # Coerce types lightly (avoid psycopg surprises later)
        q["seq"] = int(q["seq"])
        q["lat"] = float(q["lat"])
        q["lon"] = float(q["lon"])

        # osm_ref should be int if present
        if q.get("osm_ref") is not None:
            q["osm_ref"] = int(q["osm_ref"])

        out.append(q)

    return out


def _sync_phase3_review_requests(conn, route_id: uuid.UUID) -> tuple[int, int]:
    """
    Sync unmatched stops from route_work.relation_stop_prior into
    node_work.node_review_requests (source='phase3_route').

    Returns:
      (n_unmatched, n_created_requested)

    Notes:
    - If node_work.node_review_requests does not exist yet, skip gracefully.
    - Rebuild only 'requested' rows for this route+source on each run.
    """
    try:
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT seq, member_type, osm_ref, role, lat, lon
                FROM route_work.relation_stop_prior
                WHERE route_id=%s
                  AND matched_stop_node_id IS NULL
                ORDER BY seq
                """,
                (str(route_id),),
            )
            rows = cur.fetchall() or []

            n_unmatched = len(rows)

            cur.execute(
                """
                DELETE FROM node_work.node_review_requests
                WHERE source='phase3_route'
                  AND route_id=%s
                  AND status='requested'
                """,
                (str(route_id),),
            )

            for r in rows:
                tags = {
                    "role": r.get("role"),
                    "member_type": r.get("member_type"),
                    "osm_ref": r.get("osm_ref"),
                }
                cur.execute(
                    """
                    INSERT INTO node_work.node_review_requests
                      (source, route_id, seq, status, lat, lon, node_type, tags, requested_by, notes)
                    VALUES
                      ('phase3_route', %s, %s, 'requested', %s, %s, 'STOP', %s::jsonb, 'phase3_step20', %s)
                    """,
                    (
                        str(route_id),
                        int(r.get("seq")),
                        float(r.get("lat")),
                        float(r.get("lon")),
                        json.dumps(tags, ensure_ascii=False),
                        "Auto-created from Step 20 unmatched route stops.",
                    ),
                )
        return n_unmatched, n_unmatched
    except Exception as e:
        print(f"phase3_request_sync_skipped: {e}")
        return 0, 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: 20_build_stop_sequences.py <route_id>")
        raise SystemExit(1)

    route_id = uuid.UUID(sys.argv[1])

    with db_conn() as conn:
        raw = fetch_relation_raw(conn, route_id)
        prior = []
        # Preferred path: use already stored prior rows first (preserves UI edits + matches).
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT seq, lat, lon, member_type, osm_ref, osm_node_id, role, matched_stop_node_id, match_dist_m
                FROM route_work.relation_stop_prior
                WHERE route_id=%s
                ORDER BY seq
                """,
                (str(route_id),),
            )
            prior = cur.fetchall() or []

        if not prior:
            # Fallback: extract prior from Overpass JSON relation members.
            overpass_json = (raw or {}).get("overpass_json")
            if overpass_json:
                prior = extract_stop_prior(overpass_json)

        if not prior:
            if not raw:
                raise SystemExit(
                    "No route_raw.osm_relations_raw for that route_id and no stored relation_stop_prior rows. "
                    "Run Step 10 fetch or seed prior rows first."
                )
            raise SystemExit("No stop prior extracted from relation members or stored prior rows")

        prior = _normalize_and_validate_prior(prior)

        # ✅ NEW: persist the prior into route_work.relation_stop_prior
        replace_stop_prior(conn, route_id, prior)

        # Build stop-sequence candidates (uses Phase 2 final prod STOP mappings)
        set_id = build_sequence_candidates(conn, route_id, prior)
        n_unmatched, n_requests = _sync_phase3_review_requests(conn, route_id)

        print("stop_sequence_candidate_set_id:", set_id)
        print("unmatched_stops:", n_unmatched)
        print("phase3_requests_created:", n_requests)
        _print_candidates(conn, set_id)

    print("\nNext:")
    print("  1) Pick a stop_sequence_candidate_id from route_work.stop_sequence_candidates")
    print("  2) Run: 30_build_geometry_candidates.py <route_id> <stop_sequence_candidate_id>")
    print("  3) Run: 32_geometry_stop_recovery.py <route_id> <geometry_set_id>")
