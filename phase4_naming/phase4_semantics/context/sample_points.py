from __future__ import annotations
from typing import List, Tuple, Any

from phase4_semantics.common.db import get_conn
from phase4_semantics.context.sample_points_postgis import sample_points_from_ewkt


def generate_sample_points(
    route_id: str,
    geom: Any,
    version: str = "v1",
    n_points: int = 50,
) -> List[Tuple[float, float]]:
    """
    Phase 4 sampling: expects EWKT (or WKT) text in `geom`.
    Returns a list; NEVER returns None.
    """

    if geom is None:
        return []

    if not isinstance(geom, str):
        # Defensive fallback: try to stringify (but ideally geom is already EWKT from SQL)
        geom = str(geom)

    geom = geom.strip()
    if not geom:
        return []

    with get_conn(readonly=True) as conn:
        with conn.cursor() as cur:
            pts = sample_points_from_ewkt(cur, geom, n_points=n_points)

    return pts or []
