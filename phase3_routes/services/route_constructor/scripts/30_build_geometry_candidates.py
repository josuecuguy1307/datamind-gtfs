from __future__ import annotations

import sys
import uuid
from dotenv import load_dotenv

load_dotenv()

from src.db.conn import db_conn
from src.geometry.candidates import build_geometry_candidates_for_sequence


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: 30_build_geometry_candidates.py <route_id> <stop_sequence_candidate_id>")
        raise SystemExit(1)

    route_id = uuid.UUID(sys.argv[1])
    seq_id = uuid.UUID(sys.argv[2])

    with db_conn() as conn:
        geom_set_id = build_geometry_candidates_for_sequence(conn, route_id, seq_id)

    print("geometry_candidate_set_id:", geom_set_id)
    print("Note: builder supports canonical stop_node_ids OR raw stop_prior_seqs fallback.")
