from __future__ import annotations

from typing import Dict, Any

from phase5_gtfs.common.config import db_conn
from phase5_gtfs.validator.stop_coverage import get_stop_coverage_metrics


REQUIRED_TABLES = [
    "gtfs_work.gtfs_agency",
    "gtfs_work.gtfs_stops",
    "gtfs_work.gtfs_routes",
    "gtfs_work.gtfs_trips",
    "gtfs_work.gtfs_stop_times",
    "gtfs_work.gtfs_calendar",
]


def validate_export_run(export_run_id: str) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "export_run_id": export_run_id,
        "errors": [],
        "warnings": [],
        "counts": {},
        "ok": True,
    }

    with db_conn() as conn:
        with conn.cursor() as cur:
            for t in REQUIRED_TABLES:
                cur.execute(f"SELECT COUNT(*)::int AS n FROM {t} WHERE export_run_id = %s", (export_run_id,))
                n = int((cur.fetchone() or {}).get("n", 0))
                report["counts"][t] = n
                if n == 0:
                    report["errors"].append(f"{t} has no rows")

            # orphan check
            cur.execute(
                """
                SELECT COUNT(*)::int AS n
                FROM gtfs_work.gtfs_stop_times st
                LEFT JOIN gtfs_work.gtfs_stops s
                  ON s.export_run_id = st.export_run_id
                 AND s.stop_id = st.stop_id
                WHERE st.export_run_id = %s
                  AND s.stop_id IS NULL
                """,
                (export_run_id,),
            )
            orphan_stops = int((cur.fetchone() or {}).get("n", 0))
            report["counts"]["orphan_stop_times"] = orphan_stops
            if orphan_stops > 0:
                report["errors"].append(f"stop_times with missing stop_id: {orphan_stops}")

            cur.execute(
                """
                SELECT COUNT(*)::int AS n
                FROM gtfs_work.gtfs_stop_times st
                LEFT JOIN gtfs_work.gtfs_trips t
                  ON t.export_run_id = st.export_run_id
                 AND t.trip_id = st.trip_id
                WHERE st.export_run_id = %s
                  AND t.trip_id IS NULL
                """,
                (export_run_id,),
            )
            orphan_trips = int((cur.fetchone() or {}).get("n", 0))
            report["counts"]["orphan_trip_stop_times"] = orphan_trips
            if orphan_trips > 0:
                report["errors"].append(f"stop_times with missing trip_id: {orphan_trips}")

    stop_cov = get_stop_coverage_metrics(export_run_id, include_duplicate_metrics=True)
    report["counts"]["total_stops"] = int(stop_cov.get("total_stops") or 0)
    report["counts"]["served_stops_count"] = int(stop_cov.get("served_stops_count") or 0)
    report["counts"]["orphan_stops_count"] = int(stop_cov.get("orphan_stops_count") or 0)
    report["counts"]["orphan_stop_pct"] = float(stop_cov.get("orphan_stop_pct") or 0.0)
    if "duplicate_stop_name_counts" in stop_cov:
        report["counts"]["duplicate_stop_name_counts"] = stop_cov.get("duplicate_stop_name_counts") or []
    if "duplicate_coordinate_clusters" in stop_cov:
        report["counts"]["duplicate_coordinate_clusters"] = stop_cov.get("duplicate_coordinate_clusters") or []

    orphan_pct = float(stop_cov.get("orphan_stop_pct") or 0.0)
    orphan_count = int(stop_cov.get("orphan_stops_count") or 0)
    total_stops = int(stop_cov.get("total_stops") or 0)
    if total_stops > 0 and orphan_pct >= 10.0:
        report["warnings"].append(
            f"High orphan stop percentage: {orphan_count}/{total_stops} ({orphan_pct:.2f}%). "
            "Planner/autocomplete should use served_stops-only output."
        )
    elif total_stops > 0 and orphan_count > 0:
        report["warnings"].append(
            f"Orphan stops present: {orphan_count}/{total_stops} ({orphan_pct:.2f}%)."
        )

    report["ok"] = len(report["errors"]) == 0
    return report
