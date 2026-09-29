from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Any

from phase5_gtfs.common.config import db_conn


def build_calendar(export_run_id: str, *, start_date: date, end_date: date) -> Dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gtfs_work.gtfs_calendar WHERE export_run_id = %s::uuid", (export_run_id,))
            cur.execute("DELETE FROM gtfs_work.gtfs_calendar_dates WHERE export_run_id = %s::uuid", (export_run_id,))

            cur.execute(
                """
                SELECT p.profile_id, p.route_id, p.direction_id, p.service_name,
                       w.monday, w.tuesday, w.wednesday, w.thursday, w.friday, w.saturday, w.sunday
                FROM gtfs_work.route_schedule_profiles p
                JOIN gtfs_work.service_windows w ON w.profile_id = p.profile_id
                WHERE p.is_active = true AND w.is_active = true
                GROUP BY p.profile_id, p.route_id, p.direction_id, p.service_name,
                         w.monday, w.tuesday, w.wednesday, w.thursday, w.friday, w.saturday, w.sunday
                """
            )
            rows = list(cur.fetchall() or [])

            inserted = 0
            for r in rows:
                service_id = f"svc_{str(r['profile_id'])[:8]}"
                cur.execute(
                    """
                    INSERT INTO gtfs_work.gtfs_calendar (
                      export_run_id, service_id,
                      monday, tuesday, wednesday, thursday, friday, saturday, sunday,
                      start_date, end_date
                    ) VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (export_run_id, service_id) DO UPDATE SET
                      monday = EXCLUDED.monday,
                      tuesday = EXCLUDED.tuesday,
                      wednesday = EXCLUDED.wednesday,
                      thursday = EXCLUDED.thursday,
                      friday = EXCLUDED.friday,
                      saturday = EXCLUDED.saturday,
                      sunday = EXCLUDED.sunday,
                      start_date = EXCLUDED.start_date,
                      end_date = EXCLUDED.end_date
                    """,
                    (
                        export_run_id,
                        service_id,
                        int(bool(r["monday"])),
                        int(bool(r["tuesday"])),
                        int(bool(r["wednesday"])),
                        int(bool(r["thursday"])),
                        int(bool(r["friday"])),
                        int(bool(r["saturday"])),
                        int(bool(r["sunday"])),
                        start_date.strftime("%Y%m%d"),
                        end_date.strftime("%Y%m%d"),
                    ),
                )
                inserted += 1

            # exceptions
            cur.execute(
                """
                SELECT e.profile_id, e.service_date, e.exception_type
                FROM gtfs_work.calendar_exceptions e
                JOIN gtfs_work.route_schedule_profiles p ON p.profile_id = e.profile_id
                WHERE p.is_active = true
                """
            )
            ex_rows = list(cur.fetchall() or [])
            ex_inserted = 0
            for e in ex_rows:
                service_id = f"svc_{str(e['profile_id'])[:8]}"
                cur.execute(
                    """
                    INSERT INTO gtfs_work.gtfs_calendar_dates (export_run_id, service_id, date, exception_type)
                    VALUES (%s::uuid,%s,%s,%s)
                    ON CONFLICT (export_run_id, service_id, date) DO UPDATE SET
                      exception_type = EXCLUDED.exception_type
                    """,
                    (
                        export_run_id,
                        service_id,
                        e["service_date"].strftime("%Y%m%d"),
                        int(e["exception_type"]),
                    ),
                )
                ex_inserted += 1

    return {"calendar_rows": inserted, "calendar_dates_rows": ex_inserted}
