from __future__ import annotations
import uuid, sys, os
from dotenv import load_dotenv
load_dotenv()

from src.db.conn import db_conn, db_cursor
from src.approve import approve_best_in_set

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: 40_approve_geometry.py <route_id> <geometry_set_id>")
        raise SystemExit(1)

    route_id = uuid.UUID(sys.argv[1])
    geom_set_id = uuid.UUID(sys.argv[2])

    with db_conn() as conn:
        best_id = approve_best_in_set(conn, route_id, geom_set_id)

    print("approved geometry_candidate_id:", best_id)
    print("route_prod.routes updated for route_id:", route_id)
