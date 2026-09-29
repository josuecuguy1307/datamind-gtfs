from phase4_semantics.common.db import fetchone
from phase4_semantics.ingest.overpass.fetch_relation_members import (
    fetch_relation_members,
    fetch_relation_tags_only,
)

def fetch_relation_for_route(
    route_id: str,
    tags_only: bool = False,
    **kwargs,
):
    row = fetchone(
        """
        SELECT osm_relation_id
        FROM route_prod.routes
        WHERE route_id = %s
        """,
        (route_id,),
    )
    if not row or not row["osm_relation_id"]:
        raise RuntimeError(f"No approved OSM relation for route_id={route_id}")

    relation_id = int(row["osm_relation_id"])

    if tags_only:
        return fetch_relation_tags_only(relation_id, **kwargs)

    return fetch_relation_members(relation_id, **kwargs)
