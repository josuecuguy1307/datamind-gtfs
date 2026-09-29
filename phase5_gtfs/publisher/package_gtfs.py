from __future__ import annotations

import csv
import os
import zipfile
from pathlib import Path
from typing import Dict, Any, List, Sequence

from phase5_gtfs.common.config import db_conn, GTFS_OUT_DIR
from phase5_gtfs.compiler.build_feed_info import build_feed_info
from phase5_gtfs.errors import assert_no_null_direction_ids, assert_no_unapproved_semantics
from phase5_gtfs.validator.gtfs_validate import validate_export_run
from phase5_gtfs.validator.stop_coverage import (
    get_stop_coverage_metrics,
    list_orphan_stops,
    list_served_stops,
)


GTFS_FILE_SPECS: Dict[str, Dict[str, Any]] = {
    "gtfs_agency": {
        "filename": "agency.txt",
        "required": True,
        "columns": ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
        "order_by": ["agency_id"],
    },
    "gtfs_stops": {
        "filename": "stops.txt",
        "required": True,
        "columns": [
            "stop_id",
            "stop_name",
            "stop_lat",
            "stop_lon",
            "location_type",
            "parent_station",
        ],
        "order_by": ["stop_id"],
    },
    "gtfs_routes": {
        "filename": "routes.txt",
        "required": True,
        "columns": [
            "route_id",
            "agency_id",
            "route_short_name",
            "route_long_name",
            "route_type",
            "route_color",
            "route_text_color",
        ],
        "order_by": ["route_id"],
    },
    "gtfs_trips": {
        "filename": "trips.txt",
        "required": True,
        "columns": [
            "route_id",
            "service_id",
            "trip_id",
            "trip_headsign",
            "direction_id",
            "shape_id",
            "block_id",
        ],
        "order_by": ["route_id", "service_id", "trip_id"],
    },
    "gtfs_stop_times": {
        "filename": "stop_times.txt",
        "required": True,
        "columns": [
            "trip_id",
            "arrival_time",
            "departure_time",
            "stop_id",
            "stop_sequence",
            "timepoint",
            "shape_dist_traveled",
        ],
        "order_by": ["trip_id", "stop_sequence"],
    },
    "gtfs_shapes": {
        "filename": "shapes.txt",
        "required": False,
        "columns": [
            "shape_id",
            "shape_pt_lat",
            "shape_pt_lon",
            "shape_pt_sequence",
            "shape_dist_traveled",
        ],
        "order_by": ["shape_id", "shape_pt_sequence"],
    },
    "gtfs_calendar": {
        "filename": "calendar.txt",
        "required": True,
        "columns": [
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ],
        "order_by": ["service_id"],
    },
    "gtfs_calendar_dates": {
        "filename": "calendar_dates.txt",
        "required": False,
        "columns": ["service_id", "date", "exception_type"],
        "order_by": ["service_id", "date"],
    },
    "gtfs_frequencies": {
        "filename": "frequencies.txt",
        "required": False,
        "columns": ["trip_id", "start_time", "end_time", "headway_secs", "exact_times"],
        "order_by": ["trip_id", "start_time"],
    },
}


def _table_columns(table_name: str) -> List[str]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'gtfs_work'
                  AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            return [str(r["column_name"]) for r in (cur.fetchall() or []) if r.get("column_name")]


def _fetch_rows(
    table_name: str,
    export_run_id: str,
    *,
    columns: Sequence[str],
    order_by: Sequence[str],
) -> List[Dict[str, Any]]:
    actual_cols = set(_table_columns(table_name))
    selected_cols = [c for c in columns if c in actual_cols]
    if not selected_cols:
        return []

    order_cols = [c for c in order_by if c in selected_cols]
    select_list = ", ".join(selected_cols)
    order_sql = f" ORDER BY {', '.join(order_cols)}" if order_cols else ""
    sql = f"SELECT {select_list} FROM gtfs_work.{table_name} WHERE export_run_id = %s{order_sql}"
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (export_run_id,))
            rows = list(cur.fetchall() or [])
    # Normalize NULLs to empty strings for CSV output consistency.
    return [{k: ("" if v is None else v) for k, v in dict(row).items()} for row in rows]


def package_gtfs(export_run_id: str, *, require_valid: bool = True) -> Dict[str, Any]:
    # Phase 5 contract: every GTFS-eligible route must have a direction_id
    # written by Phase 4.5 (PROMETHEUS). NULL = "operator_pending" route
    # that was never resolved; refuse to export.
    # ``operator_pending`` routes are intentionally NULL (Phase 4.5 contract)
    # and excluded from this gate; the compiler filters them out by direction_id.
    province_scope = os.getenv("PHASE5_PROVINCE_SCOPE") or None
    assert_no_null_direction_ids(province=province_scope)

    # Phase 4 contract: every GTFS-eligible route must have an approved
    # catalog.route_semantics row (or be legacy_grandfathered). Unapproved
    # rows are DR work that was never operator-reviewed; refuse to export.
    assert_no_unapproved_semantics(province=province_scope)

    validator_report: Dict[str, Any] | None = None
    if require_valid:
        validator_report = validate_export_run(export_run_id)
        if not bool(validator_report.get("ok")):
            raise RuntimeError(
                "GTFS validation failed; refusing to package invalid export_run_id "
                f"{export_run_id}: {validator_report.get('errors') or []}"
            )

    GTFS_OUT_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = GTFS_OUT_DIR / f"run_{export_run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Remove old file artifacts from prior packaging runs for the same export_run_id.
    for fp in run_dir.glob("*.txt"):
        fp.unlink()

    files_written: List[str] = []
    files_in_zip: List[Path] = []
    qa_files: List[str] = []
    row_counts: Dict[str, int] = {}
    missing_required: List[str] = []
    stop_coverage: Dict[str, Any] | None = None
    for table, spec in GTFS_FILE_SPECS.items():
        filename = str(spec["filename"])
        if table == "gtfs_stops":
            stop_cols = list(spec.get("columns") or [])
            rows = list_served_stops(export_run_id, columns=stop_cols, limit=None)
            stop_coverage = get_stop_coverage_metrics(export_run_id, include_duplicate_metrics=True)

            orphan_rows = list_orphan_stops(export_run_id, columns=stop_cols, limit=None)
            if orphan_rows:
                orphan_fp = run_dir / "orphan_stops_qa.csv"
                with orphan_fp.open("w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(
                        f, fieldnames=list(orphan_rows[0].keys()), extrasaction="ignore"
                    )
                    writer.writeheader()
                    writer.writerows(orphan_rows)
                qa_files.append(str(orphan_fp))
        else:
            rows = _fetch_rows(
                table,
                export_run_id,
                columns=list(spec.get("columns") or []),
                order_by=list(spec.get("order_by") or []),
            )
        row_counts[filename] = len(rows)
        if not rows:
            if bool(spec.get("required")):
                missing_required.append(filename)
            continue
        fp = run_dir / filename
        with fp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        files_written.append(str(fp))
        files_in_zip.append(fp)

    if missing_required:
        raise RuntimeError(
            f"Cannot package GTFS export {export_run_id}: missing required file data for {missing_required}"
        )

    # Generate feed_info.txt (not DB-backed — written directly to output dir)
    feed_info_path = build_feed_info(str(run_dir))
    files_written.append(feed_info_path)
    files_in_zip.append(Path(feed_info_path))

    zip_path = GTFS_OUT_DIR / f"gtfs_{export_run_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for fp in sorted(files_in_zip, key=lambda p: p.name):
            zf.write(fp, arcname=fp.name)

    return {
        "zip_path": str(zip_path),
        "files": files_written,
        "qa_files": qa_files,
        "row_counts": row_counts,
        "stop_coverage": stop_coverage,
        "validator_report": validator_report,
    }
