from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional, Sequence, Tuple

from phase3_routes.services.route_constructor.src.db.conn import db_cursor


def _uuid_str(x: Any) -> str:
    if isinstance(x, uuid.UUID):
        return str(x)
    return str(uuid.UUID(str(x)))


def _row_get(row: Any, key: str, idx: int) -> Any:
    """
    Support RealDictCursor rows (dict) or regular cursor rows (tuple).
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    return row[idx]


# -----------------------------
# Stop priors
# -----------------------------
def replace_stop_prior(conn, route_id: uuid.UUID, rows: list[dict]) -> None:
    """
    rows: list of dicts with at least:
      seq, lat, lon
    and optionally:
      osm_ref, member_type, role, matched_stop_node_id, match_dist_m

    Back-compat:
      - if osm_node_id exists, we map it into osm_ref with member_type='node'
    """
    route_id_s = _uuid_str(route_id)

    with db_cursor(conn) as cur:
        cur.execute(
            "DELETE FROM route_work.relation_stop_prior WHERE route_id=%s",
            (route_id_s,),
        )

        for r in rows:
            seq = int(r["seq"])
            lat = float(r["lat"])
            lon = float(r["lon"])

            member_type = (r.get("member_type") or None)
            osm_ref = r.get("osm_ref")

            # Back-compat: old key name
            osm_node_id = r.get("osm_node_id")
            if osm_ref is None and osm_node_id is not None:
                osm_ref = osm_node_id
                member_type = member_type or "node"

            osm_ref_i = int(osm_ref) if osm_ref is not None else None

            # Keep legacy column populated only for nodes
            osm_node_id_i = int(osm_ref) if (member_type == "node" and osm_ref is not None) else None

            cur.execute(
                """
                INSERT INTO route_work.relation_stop_prior
                  (route_id, seq, member_type, osm_ref, osm_node_id, role, lat, lon,
                   matched_stop_node_id, match_dist_m)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    route_id_s,
                    seq,
                    member_type,
                    osm_ref_i,
                    osm_node_id_i,
                    r.get("role"),
                    lat,
                    lon,
                    _uuid_str(r["matched_stop_node_id"]) if r.get("matched_stop_node_id") else None,
                    float(r["match_dist_m"]) if r.get("match_dist_m") is not None else None,
                ),
            )

def create_stop_sequence_set(conn, route_id: uuid.UUID, notes: str = "") -> uuid.UUID:
    route_id_s = _uuid_str(route_id)

    with db_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO route_work.stop_sequence_candidate_sets(route_id, notes)
            VALUES (%s, %s)
            RETURNING set_id
            """,
            (route_id_s, notes),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("create_stop_sequence_set: INSERT returned no row")

    set_id_val = _row_get(row, "set_id", 0)
    return uuid.UUID(str(set_id_val))

def insert_stop_sequence_candidate(
    conn,
    set_id: uuid.UUID,
    rank: int,
    stop_node_ids: list[uuid.UUID],
    stop_prior_seqs: Optional[list[int]] = None,
    metrics: Optional[dict] = None,
) -> uuid.UUID:
    set_id_s = _uuid_str(set_id)
    metrics = metrics or {}

    # Default metrics fallback
    matched_stops = metrics.get("matched_stops")
    if matched_stops is None:
        matched_stops = len(stop_node_ids)

    avg_match = metrics.get("avg_match_dist_m")
    if avg_match is None:
        avg_match = 0.0

    max_match = metrics.get("max_match_dist_m")
    if max_match is None:
        max_match = 0.0

    # ✅ IMPORTANT: ensure python types are UUID (not str)
    stop_node_ids_uuid = [uuid.UUID(str(x)) for x in stop_node_ids]
    stop_prior_seqs_int = [int(x) for x in (stop_prior_seqs or [])]

    with db_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO route_work.stop_sequence_candidates
              (set_id, rank, stop_node_ids, stop_prior_seqs, metrics, matched_stops, avg_match_dist_m, max_match_dist_m)
            VALUES (%s,%s,%s::uuid[],%s::int[],%s::jsonb,%s,%s,%s)
            RETURNING candidate_id
            """,
            (
                set_id_s,
                int(rank),
                [str(x) for x in stop_node_ids_uuid],  # ok because we CAST to uuid[]
                stop_prior_seqs_int,
                json.dumps(metrics, ensure_ascii=False),
                int(matched_stops),
                float(avg_match),
                float(max_match),
            ),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("insert_stop_sequence_candidate: INSERT returned no row")

    cand_id_val = _row_get(row, "candidate_id", 0)
    return uuid.UUID(str(cand_id_val))


def fetch_stop_sequence_candidate(conn, candidate_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    """
    Returns a dict regardless of cursor row type.
    """
    cand_id_s = _uuid_str(candidate_id)

    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT candidate_id, set_id, rank, stop_node_ids, stop_prior_seqs, matched_stops,
                   avg_match_dist_m, max_match_dist_m, metrics, created_at
            FROM route_work.stop_sequence_candidates
            WHERE candidate_id=%s
            """,
            (cand_id_s,),
        )
        row = cur.fetchone()

    if not row:
        return None

    if isinstance(row, dict):
        return row

    # tuple mapping must match SELECT order
    return {
        "candidate_id": str(row[0]),
        "set_id": str(row[1]),
        "rank": row[2],
        "stop_node_ids": row[3],
        "stop_prior_seqs": row[4],
        "matched_stops": row[5],
        "avg_match_dist_m": row[6],
        "max_match_dist_m": row[7],
        "metrics": row[8],
        "created_at": row[9],
    }


# -----------------------------
# Geometry sets + candidates
# -----------------------------

def create_geometry_set(
    conn,
    route_id: uuid.UUID,
    notes: str = "",
    *,
    stop_sequence_set_id: Optional[uuid.UUID] = None,
) -> uuid.UUID:
    route_id_s = _uuid_str(route_id)

    with db_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO route_work.geometry_candidate_sets(route_id, stop_sequence_set_id, notes)
            VALUES (%s, %s, %s)
            RETURNING set_id
            """,
            (
                route_id_s,
                (_uuid_str(stop_sequence_set_id) if stop_sequence_set_id is not None else None),
                notes,
            ),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("create_geometry_set: INSERT returned no row")

    set_id_val = _row_get(row, "set_id", 0)
    return uuid.UUID(str(set_id_val))

def insert_geometry_candidate(
    conn,
    set_id: uuid.UUID,
    stop_sequence_candidate_id: Optional[uuid.UUID],  # ✅ allow None
    engine: str,
    params: dict,
    linestring_wkt: str,
    score: float,
    length_m: float,
    avg_stop_dist_m: float,
    max_stop_dist_m: float,
    metrics: dict,
    valhalla_request: Optional[dict] = None,
    valhalla_response_hash: Optional[str] = None,
) -> uuid.UUID:
    set_id_s = _uuid_str(set_id)
    seq_id_s = _uuid_str(stop_sequence_candidate_id) if stop_sequence_candidate_id else None

    with db_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO route_work.geometry_candidates
              (set_id, stop_sequence_candidate_id, engine, params, geom,
               score, length_m, avg_stop_dist_m, max_stop_dist_m, metrics,
               valhalla_request, valhalla_response_hash)
            VALUES
              (%s,%s,%s,%s::jsonb,
               ST_SetSRID(ST_GeomFromText(%s),4326),
               %s,%s,%s,%s,%s::jsonb,
               %s::jsonb, %s)
            RETURNING geometry_candidate_id
            """,
            (
                set_id_s,
                seq_id_s,  # ✅ can be NULL
                engine,
                json.dumps(params, ensure_ascii=False),
                linestring_wkt,
                float(score),
                float(length_m),
                float(avg_stop_dist_m),
                float(max_stop_dist_m),
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(valhalla_request, ensure_ascii=False) if valhalla_request is not None else None,
                valhalla_response_hash,
            ),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError("insert_geometry_candidate: INSERT returned no row")

    gc_id_val = _row_get(row, "geometry_candidate_id", 0)
    return uuid.UUID(str(gc_id_val))
