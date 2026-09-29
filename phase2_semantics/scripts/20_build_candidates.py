"""
Phase 2 – Semantic Geocoder
Step 20: Build geo_work candidates from geo_raw evidence.

DB-aligned with:
  - 001_geo_raw.sql
  - 010_geo_work_core.sql

This step:
✅ builds DB structures (candidate_sets, place_candidates, alias_candidates, node_place_map_work)
❌ does NOT score
❌ does NOT embed
❌ does NOT search
"""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
import re
from typing import Any, Dict, List, Optional

from psycopg2.extras import execute_values

from src.db.conn import db_conn
from src.settings import (
    GEO_CONTEXT_KEY,
    DEFAULT_PLACE_CONFIDENCE,
    DEFAULT_MAPPING_SOURCE,
    MVP_ONE_PLACE_PER_NODE,
    T_GEO_EXTRACT_RUNS,
    T_GEO_NAME_EVIDENCE,
    T_NODE_PROD_NODES,
    T_PLACE_CANDIDATE_SETS,
    T_PLACE_CANDIDATES,
    T_ALIAS_CANDIDATES,
    T_NODE_PLACE_WORK,
    T_NODE_GEO_CONTEXT,
)
from src.pipeline.naming.build_name_candidates import build_for_place_set
from src.utils.jsonlog import get_logger
from src.utils.text import normalize_text

logger = get_logger("phase2.build_candidates")

# ============================================================
# Helpers
# ============================================================

def _latest_extract_run_id(conn, *, context_key: Optional[str]) -> str:
    with conn.cursor() as cur:
        if context_key:
            cur.execute(
                f"""
                SELECT extract_run_id
                FROM {T_GEO_EXTRACT_RUNS}
                WHERE context_key = %s
                ORDER BY extracted_at DESC
                LIMIT 1
                """,
                (context_key,),
            )
        else:
            cur.execute(
                f"""
                SELECT extract_run_id
                FROM {T_GEO_EXTRACT_RUNS}
                ORDER BY extracted_at DESC
                LIMIT 1
                """
            )

        row = cur.fetchone()

    if not row:
        raise RuntimeError("No extract_run found")

    return str(row[0] if not isinstance(row, dict) else row["extract_run_id"])


def _resolve_extract_run_id(conn, *, context_key: Optional[str]) -> str:
    env_extract_run_id = str(os.getenv("EXTRACT_RUN_ID") or "").strip()
    source_node_set_id = str(os.getenv("SOURCE_NODE_SET_ID") or "").strip()

    with conn.cursor() as cur:
        if env_extract_run_id:
            cur.execute(
                f"""
                SELECT extract_run_id
                FROM {T_GEO_EXTRACT_RUNS}
                WHERE extract_run_id::text = %s
                LIMIT 1
                """,
                (env_extract_run_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"Unknown extract_run_id for Step 20: {env_extract_run_id}")
            return str(row[0] if not isinstance(row, dict) else row["extract_run_id"])

        if source_node_set_id:
            if context_key:
                cur.execute(
                    f"""
                    SELECT extract_run_id
                    FROM {T_GEO_EXTRACT_RUNS}
                    WHERE source_node_set_id::text = %s
                      AND context_key = %s
                    ORDER BY extracted_at DESC
                    LIMIT 1
                    """,
                    (source_node_set_id, context_key),
                )
            else:
                cur.execute(
                    f"""
                    SELECT extract_run_id
                    FROM {T_GEO_EXTRACT_RUNS}
                    WHERE source_node_set_id::text = %s
                    ORDER BY extracted_at DESC
                    LIMIT 1
                    """,
                    (source_node_set_id,),
                )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(
                    f"No extract_run found for Step 20 with source_node_set_id={source_node_set_id}"
                )
            return str(row[0] if not isinstance(row, dict) else row["extract_run_id"])

    return _latest_extract_run_id(conn, context_key=context_key)


def _fetch_name_evidence(conn, *, extract_run_id: str) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              extract_run_id,
              node_id::text AS node_id,
              source,
              raw_text,
              lang,
              weight_hint,
              tags_snapshot
            FROM {T_GEO_NAME_EVIDENCE}
            WHERE extract_run_id = %s
            """,
            (extract_run_id,),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    if isinstance(rows[0], dict):
        return rows

    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _fetch_node_geoms(conn, node_ids: List[str]) -> Dict[str, str]:
    if not node_ids:
        return {}

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              node_id::text AS node_id,
              ST_AsEWKT(geom) AS ewkt
            FROM {T_NODE_PROD_NODES}
            WHERE node_id = ANY(%s::uuid[])
            """,
            (node_ids,),
        )
        rows = cur.fetchall()

    out: Dict[str, str] = {}
    for r in rows:
        out[r[0] if not isinstance(r, dict) else r["node_id"]] = (
            r[1] if not isinstance(r, dict) else r["ewkt"]
        )
    return out


def _fetch_geo_context(conn, *, extract_run_id: str, node_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not node_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              node_id::text AS node_id,
              transit_density_300m,
              poi_density_300m,
              tag_stop_weight,
              tag_poi_weight,
              features
            FROM {T_NODE_GEO_CONTEXT}
            WHERE extract_run_id = %s
              AND node_id = ANY(%s::uuid[])
            """,
            (extract_run_id, node_ids),
        )
        rows = cur.fetchall() or []
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        rr = dict(r) if isinstance(r, dict) else {
            "node_id": r[0],
            "transit_density_300m": r[1],
            "poi_density_300m": r[2],
            "tag_stop_weight": r[3],
            "tag_poi_weight": r[4],
            "features": r[5],
        }
        out[str(rr["node_id"])] = rr
    return out


def _pick_canonical_name(tags: Dict[str, Any], fallback: str) -> str:
    def _title_case_name(raw: str) -> str:
        txt = re.sub(r"\s+", " ", str(raw or "").strip())
        if not txt:
            return "(sin nombre)"
        lowers = {"de", "del", "la", "las", "el", "los", "y", "a", "al", "en"}
        out: List[str] = []
        for i, p in enumerate(txt.split(" ")):
            if not p:
                continue
            low = p.lower()
            if i > 0 and low in lowers:
                out.append(low)
                continue
            if p.isupper() and p.isalpha() and len(p) <= 4:
                out.append(p)
                continue
            out.append(p[:1].upper() + p[1:].lower())
        return (" ".join(out)).strip() or "(sin nombre)"

    def _is_unknownish(raw: str) -> bool:
        txt = str(raw or "").strip()
        if not txt:
            return True
        low = txt.lower()
        if low.startswith("node_") or low.startswith("node-") or low.startswith("node "):
            return True
        compact = re.sub(r"[^0-9]", "", txt)
        has_alpha = bool(re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ]", txt))
        if compact and not has_alpha:
            return True
        norm = normalize_text(txt)
        if norm in {"sin nombre", "unknown", "unnamed", "no name", "s n", "na", "n a"}:
            return True
        return False

    for k in [
        "name",
        "name:es",
        "official_name",
        "short_name",
        "alt_name",
        "loc_name",
        "ref",
    ]:
        v = tags.get(k)
        if isinstance(v, str) and v.strip():
            if not _is_unknownish(v):
                return _title_case_name(v)
    if _is_unknownish(fallback):
        return "(sin nombre)"
    return _title_case_name(fallback)


def _infer_place_type(raw_aliases: List[str]) -> str:
    t = " ".join(a.lower() for a in raw_aliases)
    if any(w in t for w in ["terminal"]):
        return "TERMINAL"
    if any(w in t for w in ["station", "estación", "estacion"]):
        return "STATION"
    if any(w in t for w in ["parada", "stop", "bus"]):
        return "STOP"
    if any(w in t for w in ["hospital", "universidad", "mall", "museo"]):
        return "POI"
    return "OTHER"


def _uuid5(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


# ============================================================
# Main Orchestrator
# ============================================================

def main() -> None:
    logger.info("Starting candidate construction", extra={"context_key": GEO_CONTEXT_KEY})

    with db_conn() as conn:
        extract_run_id = _resolve_extract_run_id(conn, context_key=GEO_CONTEXT_KEY)
        logger.info("Using extract_run", extra={"extract_run_id": extract_run_id})

        evidence = _fetch_name_evidence(conn, extract_run_id=extract_run_id)
        if not evidence:
            logger.info("No evidence found → nothing to build")
            return

        by_node: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for ev in evidence:
            by_node[ev["node_id"]].append(ev)

        node_ids = list(by_node.keys())
        node_geoms = _fetch_node_geoms(conn, node_ids)
        node_geo_ctx = _fetch_geo_context(conn, extract_run_id=extract_run_id, node_ids=node_ids)

        place_set_id = str(uuid.uuid4())
        params_used = {"mvp_one_place_per_node": MVP_ONE_PLACE_PER_NODE}

        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {T_PLACE_CANDIDATE_SETS}
                (place_set_id, source_extract_run_id, context_key, params_used)
                VALUES (%s, %s, %s, %s)
                """,
                (place_set_id, extract_run_id, GEO_CONTEXT_KEY, json.dumps(params_used)),
            )

        place_rows, alias_rows, map_rows = [], [], []

        for node_id, rows in by_node.items():
            tags = rows[0].get("tags_snapshot") or {}
            raw_aliases = [r["raw_text"] for r in rows if r.get("raw_text")]

            canonical = _pick_canonical_name(tags, f"node_{node_id[:8]}")
            place_type = _infer_place_type(raw_aliases)

            place_id = _uuid5(f"{place_set_id}:{node_id}")
            ctx = node_geo_ctx.get(node_id) or {}
            alias_quality = min(len(set(normalize_text(a) for a in raw_aliases if normalize_text(a))) / 8.0, 1.0)
            stop_signal = float(ctx.get("tag_stop_weight") or 0.0) + min(float(ctx.get("transit_density_300m") or 0) / 15.0, 0.8)
            poi_signal = float(ctx.get("tag_poi_weight") or 0.0) + min(float(ctx.get("poi_density_300m") or 0) / 15.0, 0.8)
            ctx_signal = max(stop_signal, poi_signal)
            score = float(min(0.65 * alias_quality + 0.35 * min(ctx_signal, 1.5), 1.0))

            place_rows.append(
                (
                    place_id,
                    place_set_id,
                    canonical,
                    place_type,
                    node_geoms.get(node_id),
                    json.dumps({"node_id": node_id, "tags_snapshot": tags, "geo_context": ctx}),
                    score,
                )
            )

            seen = set()
            for a in raw_aliases:
                na = normalize_text(a)
                if na and na not in seen:
                    seen.add(na)
                    alias_rows.append(
                        (
                            _uuid5(f"{place_id}:{na}"),
                            place_id,
                            na,
                            "alt",
                            None,
                            None,
                        )
                    )

            map_rows.append(
                (
                    place_set_id,
                    node_id,
                    place_id,
                    DEFAULT_PLACE_CONFIDENCE,
                    DEFAULT_MAPPING_SOURCE,
                )
            )

        with conn.cursor() as cur:
            execute_values(
                cur,
                f"""
                INSERT INTO {T_PLACE_CANDIDATES}
                (place_candidate_id, place_set_id, proposed_canonical_name,
                 proposed_place_type, center_geom, provenance, score)
                VALUES %s
                """,
                place_rows,
            )

            execute_values(
                cur,
                f"""
                INSERT INTO {T_ALIAS_CANDIDATES}
                (alias_candidate_id, place_candidate_id, alias,
                 alias_kind, lang, score)
                VALUES %s
                ON CONFLICT (place_candidate_id, alias) DO NOTHING
                """,
                alias_rows,
            )

            execute_values(
                cur,
                f"""
                INSERT INTO {T_NODE_PLACE_WORK}
                (place_set_id, node_id, place_candidate_id,
                 confidence, mapping_source)
                VALUES %s
                ON CONFLICT DO NOTHING
                """,
                map_rows,
            )

        naming_out = build_for_place_set(conn, place_set_id=place_set_id, ranker_artifact=None)

        conn.commit()

    logger.info(
        "✅ Step 20 completed",
        extra={
            "place_set_id": place_set_id,
            "extract_run_id": extract_run_id,
            "source_node_set_id": str(os.getenv("SOURCE_NODE_SET_ID") or "").strip() or None,
            "places": len(place_rows),
            "aliases": len(alias_rows),
            "node_maps": len(map_rows),
            "name_candidates": int(naming_out.get("name_candidates") or 0),
        },
    )


if __name__ == "__main__":
    main()
