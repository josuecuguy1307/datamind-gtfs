from __future__ import annotations

import sys
import uuid
from dotenv import load_dotenv

load_dotenv()

from src.db.conn import db_conn
from src.geometry.stop_recovery import run_geometry_stop_recovery_for_set


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: 32_geometry_stop_recovery.py <route_id> <geometry_set_id>")
        raise SystemExit(1)

    route_id = uuid.UUID(sys.argv[1])
    geometry_set_id = uuid.UUID(sys.argv[2])

    with db_conn() as conn:
        out = run_geometry_stop_recovery_for_set(
            conn,
            route_id=route_id,
            geometry_set_id=geometry_set_id,
        )

    print("geometry_stop_recovery_set_id:", out.get("geometry_set_id"))
    print("geometry_candidates_processed:", int(out.get("geometry_candidate_count") or 0))
    print("recovered_total:", int(out.get("recovered_total") or 0))
    print("ambiguous_total:", int(out.get("ambiguous_total") or 0))
    print("rejected_total:", int(out.get("rejected_total") or 0))
