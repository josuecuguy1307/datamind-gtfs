#!/usr/bin/env python3
"""
Ingest catalog data (semantics, schedules, service days, layovers, exceptions)
from a Deep Research output JSON into the catalog schema.

Usage:
    cd phase4_naming
    PYTHONPATH=. python scripts/ingest_research_catalogs.py --input /path/to/research.json
    PYTHONPATH=. python scripts/ingest_research_catalogs.py --input /path/to/research.json --dry-run

Expected JSON structure:
{
  "routes": [
    {
      "route_code": "SGA-01",          // matches route_prod.routes.route_code
      "route_id": "uuid-optional",     // if absent, resolved via route_code
      "operator": "Termas Turis",
      "route_short_name": "SGA-01",
      "route_long_name": "Sangolquí - Quito",
      "route_type": 3,
      "public_origin": "Sangolquí",
      "public_destination": "Quito",
      "aliases": [],
      "description": "...",
      "jurisdiction": "DMQ",           // DMQ | ANT | MIXED
      "evidence_source": "research_cayambe_2026",
      "confidence": 0.65,
      "schedules": [
        {
          "direction_id": 0,
          "service_pattern_id": "weekday",
          "window_start": "05:30",
          "window_end": "09:00",
          "headway_min": 10,
          "peak_type": "peak",
          "source": "...",
          "confidence": 0.6
        }
      ],
      "service_days": [
        {
          "service_pattern_id": "weekday",
          "monday": true, "tuesday": true, "wednesday": true,
          "thursday": true, "friday": true, "saturday": false, "sunday": false,
          "first_departure": "05:00", "last_departure": "21:00",
          "headway_min": 12,
          "valid_from": "2026-01-01",
          "source": "...",
          "confidence": 0.6
        }
      ],
      "layover": {
        "layover_at_destination_min": 5.0,
        "layover_at_origin_min": 5.0,
        "min_layover_min": 3.0,
        "max_layover_min": 15.0,
        "source": "...",
        "confidence": 0.45
      }
    }
  ],
  "holidays_2026": [
    {"date": "2026-01-01", "reason": "Año Nuevo"},
    {"date": "2026-02-16", "reason": "Carnaval"},
    {"date": "2026-02-17", "reason": "Carnaval"},
    {"date": "2026-04-03", "reason": "Viernes Santo"},
    {"date": "2026-05-01", "reason": "Día del Trabajo"},
    {"date": "2026-05-25", "reason": "Batalla de Sample Region (trasladado)"},
    {"date": "2026-08-10", "reason": "Primer Grito de Independencia"},
    {"date": "2026-10-09", "reason": "Independencia de Guayaquil"},
    {"date": "2026-11-02", "reason": "Día de los Difuntos"},
    {"date": "2026-11-03", "reason": "Independencia de Cuenca"},
    {"date": "2026-12-06", "reason": "Fundación de Quito"},
    {"date": "2026-12-25", "reason": "Navidad"}
  ]
}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running from phase4_naming/ with PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from phase4_semantics.common.db import get_conn

# Ecuador 2026 holidays (fallback if not in JSON)
ECUADOR_HOLIDAYS_2026 = [
    ("2026-01-01", "Año Nuevo"),
    ("2026-02-16", "Carnaval"),
    ("2026-02-17", "Carnaval"),
    ("2026-04-03", "Viernes Santo"),
    ("2026-05-01", "Día del Trabajo"),
    ("2026-05-25", "Batalla de Sample Region (trasladado)"),
    ("2026-08-10", "Primer Grito de Independencia"),
    ("2026-10-09", "Independencia de Guayaquil"),
    ("2026-11-02", "Día de los Difuntos"),
    ("2026-11-03", "Independencia de Cuenca"),
    ("2026-12-06", "Fundación de Quito"),
    ("2026-12-25", "Navidad"),
]


def _resolve_route_id(cur, route: Dict[str, Any]) -> Optional[str]:
    """Resolve route_id, verifying it exists in route_prod.routes.

    Resolution order:
      1. Direct route_id UUID (verified against route_prod)
      2. route_short_name lookup via catalog.route_semantics
      3. route_name fuzzy match against route_prod.routes
    """
    # 1. Direct route_id
    rid = route.get("route_id")
    if rid:
        cur.execute(
            "SELECT route_id::text FROM route_prod.routes WHERE route_id = %s::uuid LIMIT 1",
            (rid,),
        )
        row = cur.fetchone()
        if row:
            return row[0]

    # 2. route_short_name via catalog.route_semantics
    short = route.get("route_short_name") or route.get("route_code")
    if short:
        cur.execute(
            "SELECT route_id::text FROM catalog.route_semantics WHERE route_short_name = %s LIMIT 1",
            (short,),
        )
        row = cur.fetchone()
        if row:
            return row[0]

    # 3. Exact route_name match
    long_name = route.get("route_long_name")
    if long_name:
        cur.execute(
            "SELECT route_id::text FROM route_prod.routes WHERE route_name = %s LIMIT 1",
            (long_name,),
        )
        row = cur.fetchone()
        if row:
            return row[0]

    return None


def _upsert_semantics(cur, route_id: str, r: Dict[str, Any]) -> bool:
    cur.execute(
        """
        INSERT INTO catalog.route_semantics (
            route_id, operator, route_short_name, route_long_name,
            route_type, public_origin, public_destination,
            aliases, description, jurisdiction,
            evidence_source, confidence, approved
        ) VALUES (
            %s::uuid, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, false
        )
        ON CONFLICT (route_id) DO UPDATE SET
            operator = EXCLUDED.operator,
            route_short_name = EXCLUDED.route_short_name,
            route_long_name = EXCLUDED.route_long_name,
            route_type = EXCLUDED.route_type,
            public_origin = EXCLUDED.public_origin,
            public_destination = EXCLUDED.public_destination,
            aliases = EXCLUDED.aliases,
            description = EXCLUDED.description,
            jurisdiction = EXCLUDED.jurisdiction,
            evidence_source = CASE
                WHEN catalog.route_semantics.confidence < EXCLUDED.confidence
                THEN EXCLUDED.evidence_source
                ELSE catalog.route_semantics.evidence_source
            END,
            confidence = GREATEST(catalog.route_semantics.confidence, EXCLUDED.confidence)
        """,
        (
            route_id,
            r["operator"],
            r["route_short_name"],
            r["route_long_name"],
            r.get("route_type", 3),
            r["public_origin"],
            r["public_destination"],
            r.get("aliases", []),
            r.get("description"),
            r["jurisdiction"],
            r["evidence_source"],
            r.get("confidence", 0.5),
        ),
    )
    return True


def _insert_schedules(cur, route_id: str, schedules: List[Dict[str, Any]]) -> int:
    count = 0
    for s in schedules:
        cur.execute(
            """
            INSERT INTO catalog.route_schedule_profile (
                route_id, direction_id, service_pattern_id,
                window_start, window_end, headway_min,
                exact_departures, runtime_override_min,
                peak_type, estimated_vehicles, cycle_time_min,
                source, confidence, notes
            ) VALUES (
                %s::uuid, %s, %s,
                %s::time, %s::time, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (route_id, direction_id, service_pattern_id, window_start)
            DO UPDATE SET
                headway_min = EXCLUDED.headway_min,
                window_end = EXCLUDED.window_end,
                peak_type = EXCLUDED.peak_type,
                source = EXCLUDED.source,
                confidence = GREATEST(
                    catalog.route_schedule_profile.confidence, EXCLUDED.confidence
                )
            """,
            (
                route_id,
                s["direction_id"],
                s["service_pattern_id"],
                s["window_start"],
                s["window_end"],
                s.get("headway_min"),
                s.get("exact_departures"),
                s.get("runtime_override_min"),
                s.get("peak_type"),
                s.get("estimated_vehicles"),
                s.get("cycle_time_min"),
                s["source"],
                s.get("confidence", 0.5),
                s.get("notes"),
            ),
        )
        count += 1
    return count


def _insert_service_days(cur, route_id: str, days: List[Dict[str, Any]]) -> int:
    count = 0
    for d in days:
        cur.execute(
            """
            INSERT INTO catalog.route_service_days (
                route_id, service_pattern_id,
                monday, tuesday, wednesday, thursday, friday,
                saturday, sunday,
                first_departure, last_departure, headway_min,
                valid_from, valid_to, source, confidence, notes
            ) VALUES (
                %s::uuid, %s,
                %s, %s, %s, %s, %s,
                %s, %s,
                %s::time, %s::time, %s,
                %s::date, %s, %s, %s, %s
            )
            ON CONFLICT (route_id, service_pattern_id) DO UPDATE SET
                monday = EXCLUDED.monday, tuesday = EXCLUDED.tuesday,
                wednesday = EXCLUDED.wednesday, thursday = EXCLUDED.thursday,
                friday = EXCLUDED.friday, saturday = EXCLUDED.saturday,
                sunday = EXCLUDED.sunday,
                first_departure = EXCLUDED.first_departure,
                last_departure = EXCLUDED.last_departure,
                headway_min = EXCLUDED.headway_min,
                valid_from = EXCLUDED.valid_from,
                source = EXCLUDED.source,
                confidence = GREATEST(
                    catalog.route_service_days.confidence, EXCLUDED.confidence
                )
            """,
            (
                route_id,
                d["service_pattern_id"],
                d.get("monday", False),
                d.get("tuesday", False),
                d.get("wednesday", False),
                d.get("thursday", False),
                d.get("friday", False),
                d.get("saturday", False),
                d.get("sunday", False),
                d["first_departure"],
                d["last_departure"],
                d.get("headway_min"),
                d.get("valid_from", "2026-01-01"),
                d.get("valid_to"),
                d["source"],
                d.get("confidence", 0.5),
                d.get("notes"),
            ),
        )
        count += 1
    return count


def _upsert_layover(cur, route_id: str, lay: Dict[str, Any]) -> bool:
    cur.execute(
        """
        INSERT INTO catalog.route_layover_policy (
            route_id, layover_at_destination_min, layover_at_origin_min,
            min_layover_min, max_layover_min, applies_to_pattern,
            source, confidence, notes
        ) VALUES (
            %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (route_id, applies_to_pattern) DO UPDATE SET
            layover_at_destination_min = EXCLUDED.layover_at_destination_min,
            layover_at_origin_min = EXCLUDED.layover_at_origin_min,
            min_layover_min = EXCLUDED.min_layover_min,
            max_layover_min = EXCLUDED.max_layover_min,
            source = EXCLUDED.source,
            confidence = GREATEST(
                catalog.route_layover_policy.confidence, EXCLUDED.confidence
            )
        """,
        (
            route_id,
            lay.get("layover_at_destination_min", 5.0),
            lay.get("layover_at_origin_min", 5.0),
            lay.get("min_layover_min", 3.0),
            lay.get("max_layover_min", 15.0),
            lay.get("applies_to_pattern", "all"),
            lay["source"],
            lay.get("confidence", 0.3),
            lay.get("notes"),
        ),
    )
    return True


def _insert_holidays(cur, route_id: str, holidays: List[tuple]) -> int:
    count = 0
    for date_str, reason in holidays:
        cur.execute(
            """
            INSERT INTO catalog.route_service_exceptions (
                route_id, exception_date, exception_type, reason
            ) VALUES (%s::uuid, %s::date, 2, %s)
            ON CONFLICT (route_id, exception_date) DO NOTHING
            """,
            (route_id, date_str, reason),
        )
        count += cur.rowcount
    return count


def ingest(input_path: str, dry_run: bool = False) -> Dict[str, int]:
    with open(input_path) as f:
        data = json.load(f)

    routes = data.get("routes", [])
    holidays_raw = data.get("holidays_2026")
    holidays = (
        [(h["date"], h["reason"]) for h in holidays_raw]
        if holidays_raw
        else ECUADOR_HOLIDAYS_2026
    )

    stats = {
        "routes_processed": 0,
        "routes_skipped": 0,
        "semantics": 0,
        "schedules": 0,
        "service_days": 0,
        "layovers": 0,
        "exceptions": 0,
    }

    with get_conn() as conn:
        cur = conn.cursor()
        for r in routes:
            route_id = _resolve_route_id(cur, r)
            if not route_id:
                code = r.get("route_code", r.get("route_id", "?"))
                print(f"  SKIP: cannot resolve route_id for {code}")
                stats["routes_skipped"] += 1
                continue

            stats["routes_processed"] += 1

            if _upsert_semantics(cur, route_id, r):
                stats["semantics"] += 1

            stats["schedules"] += _insert_schedules(
                cur, route_id, r.get("schedules", [])
            )
            stats["service_days"] += _insert_service_days(
                cur, route_id, r.get("service_days", [])
            )

            lay = r.get("layover")
            if lay:
                _upsert_layover(cur, route_id, lay)
                stats["layovers"] += 1

            stats["exceptions"] += _insert_holidays(cur, route_id, holidays)

        if dry_run:
            conn.rollback()
            print("\n[DRY RUN] All changes rolled back.")
        else:
            conn.commit()
            print("\n[COMMIT] All changes persisted.")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Ingest Deep Research catalog JSON")
    parser.add_argument("--input", required=True, help="Path to research JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Roll back after ingestion")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: {args.input} not found")
        sys.exit(1)

    print(f"Ingesting: {args.input}")
    stats = ingest(args.input, dry_run=args.dry_run)

    print("\n--- Ingestion Summary ---")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
