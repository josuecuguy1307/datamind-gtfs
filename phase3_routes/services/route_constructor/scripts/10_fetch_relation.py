from __future__ import annotations

import os
import sys
import uuid
from dotenv import load_dotenv
import json

load_dotenv()

from src.db.conn import db_conn, db_cursor
from src.db.route_raw_repo import create_route_job, upsert_relation_raw
from src.evidence.overpass import fetch_relation_overpass_json


def _get_chosen_relation_id(conn, route_id: uuid.UUID) -> int | None:
    with db_cursor(conn) as cur:
        cur.execute(
            "SELECT chosen_osm_relation_id FROM route_raw.route_jobs WHERE route_id=%s",
            (str(route_id),),
        )
        row = cur.fetchone()
    if not row:
        return None
    val = row.get("chosen_osm_relation_id")
    return int(val) if val is not None else None


if __name__ == "__main__":
    # Usage:
    #   10_fetch_relation.py <route_id|new> [osm_relation_id] [--province <name>]
    #
    # --province is REQUIRED when the first arg is "new" (Skill 11 §7).
    # For existing route_ids it is ignored. The flag may appear anywhere after
    # the positional args; env var ROUTE_JOB_PROVINCE is a fallback.
    argv = list(sys.argv[1:])
    province_arg: str | None = None
    if "--province" in argv:
        i = argv.index("--province")
        if i + 1 >= len(argv):
            print("error: --province requires a value", file=sys.stderr)
            raise SystemExit(2)
        province_arg = argv[i + 1]
        del argv[i : i + 2]
    if province_arg is None:
        province_arg = os.getenv("ROUTE_JOB_PROVINCE")

    if len(argv) < 1:
        print("usage: 10_fetch_relation.py <route_id|new> [osm_relation_id] --province <name>")
        raise SystemExit(1)

    route_id_arg = argv[0]
    osm_relation_id: int | None = int(argv[1]) if len(argv) >= 2 else None

    with db_conn() as conn:
        if route_id_arg == "new":
            if not province_arg:
                print(
                    "error: --province <name> is required when creating a new route_job "
                    "(Skill 11 §7). Set --province or ROUTE_JOB_PROVINCE env var.",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            route_id = create_route_job(
                conn,
                created_by=os.getenv("USER"),
                notes="created via 10_fetch_relation.py",
                province=province_arg,
            )
        else:
            route_id = uuid.UUID(route_id_arg)

        if osm_relation_id is None:
            osm_relation_id = _get_chosen_relation_id(conn, route_id)
            if osm_relation_id is None:
                raise SystemExit(
                    "No osm_relation_id passed AND route_raw.route_jobs.chosen_osm_relation_id is NULL. "
                    "Run 05_discover_relation.py first or pass the relation id explicitly."
                )

        overpass_json = fetch_relation_overpass_json(osm_relation_id)
        upsert_relation_raw(conn, route_id, osm_relation_id, overpass_json)

        print("route_id:", route_id)
        print("stored OSM relation:", osm_relation_id)

        # ✅ Optional: machine-readable payload for Streamlit/UI parsing
        print(
            "P3_JSON:",
            json.dumps(
                {
                    "route_id": str(route_id),
                    "osm_relation_id": int(osm_relation_id),
                    "stored": True,
                },
                ensure_ascii=False,
            ),
        )
