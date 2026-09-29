from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from phase5_gtfs.common.config import db_conn


def run_migration(migration_path: Path) -> None:
    sql = migration_path.read_text(encoding="utf-8")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def run_all_migrations(migrations_dir: Path) -> None:
    for path in sorted(migrations_dir.glob("V5_*.sql")):
        run_migration(path)


def ensure_default_profiles_for_verified_routes() -> int:
    sql = """
    INSERT INTO gtfs_work.route_schedule_profiles (route_id, direction_id, service_name, runtime_secs, dwell_secs)
    SELECT r.route_id, d.direction_id, 'weekday_base', 3600, 20
    FROM route_prod.routes r
    JOIN route_prod.route_semantics s ON s.route_id = r.route_id
    CROSS JOIN (VALUES (0), (1)) AS d(direction_id)
    WHERE s.human_verified = true
      AND COALESCE(r.canonical_sequence_ready, FALSE) = TRUE
      AND r.chosen_stop_sequence_candidate_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1
        FROM gtfs_work.route_schedule_profiles p
        WHERE p.route_id = r.route_id
          AND p.direction_id = d.direction_id
          AND p.service_name = 'weekday_base'
      )
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return int(cur.rowcount or 0)


def ensure_default_windows() -> int:
    sql = """
    INSERT INTO gtfs_work.service_windows (
      profile_id, start_time, end_time, headway_secs,
      monday, tuesday, wednesday, thursday, friday, saturday, sunday
    )
    SELECT p.profile_id, '06:00:00', '22:00:00', 600,
           true, true, true, true, true, false, false
    FROM gtfs_work.route_schedule_profiles p
    WHERE p.is_active = true
      AND NOT EXISTS (
        SELECT 1 FROM gtfs_work.service_windows w
        WHERE w.profile_id = p.profile_id
      )
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return int(cur.rowcount or 0)


def list_profiles(*, route_id: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
    where = ""
    params: List[Any] = []
    if route_id:
        where = "WHERE p.route_id = %s"
        params.append(route_id)
    sql = f"""
    SELECT p.*, v.route_name, v.route_ref, v.operator_name
    FROM gtfs_work.route_schedule_profiles p
    LEFT JOIN gtfs_work.v_route_inputs v ON v.route_id = p.route_id
    {where}
    ORDER BY p.updated_at DESC
    LIMIT %s
    """
    params.append(int(limit))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return list(cur.fetchall() or [])
