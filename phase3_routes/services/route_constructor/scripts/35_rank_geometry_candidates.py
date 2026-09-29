from __future__ import annotations

import sys
import uuid
import argparse
from dotenv import load_dotenv

load_dotenv()

from src.db.conn import db_conn, db_cursor
from src.learning.rank_geometry import rank_geometry_set  # DB-aware ranker


def _detect_set_mode(conn, geometry_set_id: uuid.UUID) -> str:
    """
    Returns: 'sequence_based' | 'raw_first' | 'empty'
    """
    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT
              COUNT(*) AS n,
              SUM(CASE WHEN stop_sequence_candidate_id IS NULL THEN 1 ELSE 0 END) AS n_null_seq,
              SUM(CASE WHEN stop_sequence_candidate_id IS NOT NULL THEN 1 ELSE 0 END) AS n_has_seq
            FROM route_work.geometry_candidates
            WHERE set_id = %s
            """,
            (str(geometry_set_id),),
        )
        row = cur.fetchone() or {}

    n = int(row.get("n") or 0)
    n_null = int(row.get("n_null_seq") or 0)
    n_has = int(row.get("n_has_seq") or 0)

    if n == 0:
        return "empty"
    if n_has > 0 and n_null == 0:
        return "sequence_based"
    if n_null > 0 and n_has == 0:
        return "raw_first"
    # mixed sets are allowed, but unusual
    return "mixed"


def _print_set_summary(conn, route_id: uuid.UUID, geometry_set_id: uuid.UUID) -> None:
    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT set_id, route_id, stop_sequence_set_id, created_at, notes
            FROM route_work.geometry_candidate_sets
            WHERE set_id=%s AND route_id=%s
            """,
            (str(geometry_set_id), str(route_id)),
        )
        srow = cur.fetchone()

        cur.execute(
            """
            SELECT
              COUNT(*) AS n,
              MIN(score) AS min_score,
              AVG(score) AS avg_score,
              MAX(score) AS max_score
            FROM route_work.geometry_candidates
            WHERE set_id=%s
            """,
            (str(geometry_set_id),),
        )
        grow = cur.fetchone() or {}

    print("\n[rank] geometry_candidate_set summary")
    if srow:
        print(f"  set_id: {srow.get('set_id')}")
        print(f"  route_id: {srow.get('route_id')}")
        print(f"  stop_sequence_set_id: {srow.get('stop_sequence_set_id')}")
        print(f"  created_at: {srow.get('created_at')}")
        print(f"  notes: {srow.get('notes')}")
    else:
        print("  (no geometry_candidate_sets row found for that set_id+route_id)")

    print("\n[rank] geometry_candidates stats")
    print(f"  n: {int(grow.get('n') or 0)}")
    print(f"  min_score: {grow.get('min_score')}")
    print(f"  avg_score: {grow.get('avg_score')}")
    print(f"  max_score: {grow.get('max_score')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("route_id", help="route_raw.route_jobs.route_id (uuid)")
    ap.add_argument("geometry_set_id", help="route_work.geometry_candidate_sets.set_id (uuid)")
    ap.add_argument("--explain", action="store_true", help="Print set stats before ranking")
    args = ap.parse_args()

    route_id = uuid.UUID(args.route_id)
    geom_set_id = uuid.UUID(args.geometry_set_id)

    with db_conn() as conn:
        mode = _detect_set_mode(conn, geom_set_id)

        if args.explain:
            _print_set_summary(conn, route_id, geom_set_id)
            print(f"\n[rank] detected mode: {mode}")

        if mode == "empty":
            print("[rank] No geometry_candidates found for that set_id. Nothing to rank.")
            return 0

        # ✅ This call must tolerate stop_sequence_candidate_id being NULL.
        # Your ranker should read geometry_candidates rows and produce labels/ranks.
        rank_geometry_set(conn, route_id=route_id, geometry_set_id=geom_set_id)

    print("\nML ranking done for geometry_set_id:", geom_set_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
