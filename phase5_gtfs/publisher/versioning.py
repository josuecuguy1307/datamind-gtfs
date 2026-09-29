from __future__ import annotations

from typing import Dict, Any, Optional
import psycopg2.extras

from phase5_gtfs.common.config import db_conn


def publish_feed_version(
    export_run_id: str,
    *,
    zip_path: str,
    validator_report: Dict[str, Any],
    published_by: Optional[str] = None,
) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE gtfs_prod.feed_versions SET is_current = false WHERE is_current = true")
            cur.execute(
                """
                INSERT INTO gtfs_prod.feed_versions (
                  export_run_id, gtfs_zip_path, validator_report, published_by, is_current
                ) VALUES (%s::uuid,%s,%s::jsonb,%s,true)
                RETURNING *
                """,
                (export_run_id, zip_path, psycopg2.extras.Json(validator_report), published_by),
            )
            row = cur.fetchone() or {}

            cur.execute(
                """
                UPDATE gtfs_work.export_runs
                SET status = 'published', output_zip_path = %s, completed_at = now()
                WHERE export_run_id = %s::uuid
                """,
                (zip_path, export_run_id),
            )

    return dict(row)
