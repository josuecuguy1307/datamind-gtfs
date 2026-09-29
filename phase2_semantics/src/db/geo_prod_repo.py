from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, List, Optional, Dict, Any

from psycopg2.extensions import connection as PGConnection


# -------------------------------------------------------------------
# Tables (keep explicit; prod tables should be stable + obvious)
# -------------------------------------------------------------------

T_PLACES = "geo_prod.places"
T_ALIASES = "geo_prod.place_aliases"


# -------------------------------------------------------------------
# Domain models
# -------------------------------------------------------------------

@dataclass(frozen=True)
class PlaceProd:
    place_id: str
    lat: float
    lon: float
    score: float = 0.0


@dataclass(frozen=True)
class PlaceProdFull:
    place_id: str
    lat: float
    lon: float
    score: float
    aliases: List[str]


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _alias_id(place_id: str, alias: str) -> str:
    """
    Deterministic alias id: same (place_id, alias) -> same id forever.
    NOTE: This is for ID stability, not security.
    """
    s = f"{place_id}:{alias}".encode("utf-8")
    return hashlib.sha1(s).hexdigest()


# -------------------------------------------------------------------
# READ
# -------------------------------------------------------------------

def place_exists(conn: PGConnection, place_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT 1 FROM {T_PLACES} WHERE place_id = %s LIMIT 1",
            (place_id,),
        )
        return cur.fetchone() is not None


def get_place(conn: PGConnection, place_id: str) -> Optional[PlaceProdFull]:
    """
    Fetch place + all aliases.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              p.place_id,
              p.lat,
              p.lon,
              p.score,
              COALESCE(array_agg(a.alias ORDER BY a.alias) FILTER (WHERE a.alias IS NOT NULL), '{{}}') AS aliases
            FROM {T_PLACES} p
            LEFT JOIN {T_ALIASES} a
              ON a.place_id = p.place_id
            WHERE p.place_id = %s
            GROUP BY p.place_id, p.lat, p.lon, p.score
            """,
            (place_id,),
        )
        row = cur.fetchone()

    if not row:
        return None

    return PlaceProdFull(
        place_id=row["place_id"],
        lat=float(row["lat"]),
        lon=float(row["lon"]),
        score=float(row["score"]),
        aliases=list(row["aliases"] or []),
    )


def get_places(conn: PGConnection, place_ids: Iterable[str]) -> List[PlaceProd]:
    """
    Fetch many places (without aliases).
    """
    ids = list(place_ids)
    if not ids:
        return []

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT place_id, lat, lon, score
            FROM {T_PLACES}
            WHERE place_id = ANY(%s)
            """,
            (ids,),
        )
        rows = cur.fetchall() or []

    return [
        PlaceProd(
            place_id=r["place_id"],
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            score=float(r["score"]),
        )
        for r in rows
    ]


# -------------------------------------------------------------------
# WRITE (canonical truth only)
# -------------------------------------------------------------------

def upsert_place(conn: PGConnection, place: PlaceProd) -> None:
    """
    Upsert canonical place record.

    Idempotent:
      - same place_id overwrites lat/lon/score deterministically
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_PLACES} (place_id, lat, lon, score)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (place_id)
            DO UPDATE SET
              lat   = EXCLUDED.lat,
              lon   = EXCLUDED.lon,
              score = EXCLUDED.score
            """,
            (place.place_id, place.lat, place.lon, place.score),
        )


def upsert_place_aliases(conn: PGConnection, place_id: str, aliases: List[str]) -> int:
    """
    Upsert aliases for a canonical place.

    - Inserts new aliases
    - Keeps existing ones (idempotent)
    - Returns number of attempted upserts (not DB inserted count)
    """
    if not aliases:
        return 0

    # normalize trivial duplicates at call site
    uniq = []
    seen = set()
    for a in aliases:
        if not a:
            continue
        if a in seen:
            continue
        seen.add(a)
        uniq.append(a)

    if not uniq:
        return 0

    with conn.cursor() as cur:
        # bulk insert via executemany (simple and reliable)
        cur.executemany(
            f"""
            INSERT INTO {T_ALIASES} (alias_id, place_id, alias)
            VALUES (%s, %s, %s)
            ON CONFLICT (alias_id)
            DO UPDATE SET
              place_id = EXCLUDED.place_id,
              alias    = EXCLUDED.alias
            """,
            [(_alias_id(place_id, a), place_id, a) for a in uniq],
        )

    return len(uniq)


def upsert_place_with_aliases(conn: PGConnection, place: PlaceProd, aliases: List[str]) -> None:
    """
    Convenience: write canonical place + its aliases in one transaction.
    """
    upsert_place(conn, place)
    upsert_place_aliases(conn, place.place_id, aliases)


# -------------------------------------------------------------------
# Optional: write a selection record (if you have a prod audit table)
# -------------------------------------------------------------------

def write_place_selection_log(
    conn: PGConnection,
    *,
    run_id: str,
    query_text: str,
    approved_place_id: str,
    meta: Optional[Dict[str, Any]] = None,
    table: str = "geo_prod.place_selection_log",
) -> None:
    """
    Optional: if you created a geo_prod.place_selection_log table.
    Keep this separate from pipeline logs; this is an audit trail in DB.

    If you don't have that table yet, ignore this function.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {table} (run_id, query_text, approved_place_id, meta)
            VALUES (%s, %s, %s, %s::jsonb)
            """,
            (run_id, query_text, approved_place_id, (meta or {})),
        )
