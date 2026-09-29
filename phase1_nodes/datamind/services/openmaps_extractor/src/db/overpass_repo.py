from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import UUID

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    exec_sql,
    fetchone,
    exec_values,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_ACTIONS,
    T_QUERIES,
    T_RUNS,
    T_ELEMENTS,
)

JsonDict = Dict[str, Any]

def ensure_action(conn, action_id: str, template_path: str, default_params: Optional[JsonDict] = None):
    exec_sql(
        conn,
        f"""
        INSERT INTO {T_ACTIONS} (action_id, template_path, default_params)
        VALUES (%s, %s, %s)
        ON CONFLICT (action_id) DO UPDATE
        SET template_path = EXCLUDED.template_path,
            default_params = COALESCE(EXCLUDED.default_params, {T_ACTIONS}.default_params)
        """,
        (action_id, template_path, json.dumps(default_params or {})),
    )

def insert_query(conn, action_id: str, query_text: str, params: JsonDict) -> UUID:
    row = fetchone(
        conn,
        f"""
        INSERT INTO {T_QUERIES} (action_id, query_text, params)
        VALUES (%s, %s, %s)
        RETURNING query_id
        """,
        (action_id, query_text, json.dumps(params)),
    )
    return row["query_id"]

def insert_run(
    conn,
    query_id: UUID,
    bbox: Optional[JsonDict],
    status: str,
    runtime_ms: Optional[int],
    element_count: Optional[int],
    response_bytes: Optional[int],
    area_id: Optional[str] = None,
) -> UUID:
    row = fetchone(
        conn,
        f"""
        INSERT INTO {T_RUNS}
          (query_id, bbox, area_id, status, runtime_ms, element_count, response_bytes, fetched_at)
        VALUES
          (%s, %s, %s, %s, %s, %s, %s, now())
        RETURNING run_id
        """,
        (query_id, json.dumps(bbox) if bbox else None, area_id, status, runtime_ms, element_count, response_bytes),
    )
    return row["run_id"]

def bulk_insert_elements(conn, run_id: UUID, elements: List[dict]):
    rows = []
    for el in elements:
        osm_type = el.get("type")
        osm_id = el.get("id")
        tags = el.get("tags") or {}

        lat = el.get("lat")
        lon = el.get("lon")

        center = el.get("center") or {}
        center_lat = center.get("lat")
        center_lon = center.get("lon")

        # geometry: prefer node coords else center coords
        use_lat = lat if lat is not None else center_lat
        use_lon = lon if lon is not None else center_lon
        wkt = f"POINT({use_lon} {use_lat})" if (use_lat is not None and use_lon is not None) else None

        rows.append((run_id, osm_type, osm_id, lat, lon, center_lat, center_lon, json.dumps(tags), wkt))

    if not rows:
        return

    exec_values(
        conn,
        f"""
        INSERT INTO {T_ELEMENTS}
          (run_id, osm_type, osm_id, lat, lon, center_lat, center_lon, tags, geom)
        VALUES %s
        ON CONFLICT (run_id, osm_type, osm_id) DO NOTHING
        """,
        rows,
        template="(%s,%s,%s,%s,%s,%s,%s,%s, ST_GeomFromText(%s,4326))",
    )
