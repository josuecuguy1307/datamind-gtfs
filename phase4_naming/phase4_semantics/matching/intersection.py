# phase4_semantics/matching/intersection.py
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from phase4_semantics.common.db import fetchall, fetchone


# ============================================================
# Text helpers (semantic feature; NOT the main intersection)
# ============================================================

def _tokenize(text: str) -> Set[str]:
    return {t.lower() for t in re.split(r"[\s\-_/]+", text or "") if t}


def semantic_token_overlap(route_name: str, stop_names: Iterable[str]) -> Dict[str, float]:
    """
    A *semantic* overlap feature (string tokens), useful in scoring.
    Not the Phase-4 "intersection" signal.
    """
    route_tokens = _tokenize(route_name)
    if not route_tokens:
        return {"token_overlap_count": 0.0, "token_overlap_ratio": 0.0}

    stop_tokens: Set[str] = set()
    for s in stop_names:
        stop_tokens |= _tokenize(s)

    overlap = route_tokens & stop_tokens
    return {
        "token_overlap_count": float(len(overlap)),
        "token_overlap_ratio": float(len(overlap) / max(1, len(route_tokens))),
    }


# ============================================================
# Core intersection: stop OSM nodes ∩ relation member nodes
# ============================================================

def _columns(schema: str, table: str) -> Set[str]:
    rows = fetchall(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return {str(r["column_name"]) for r in rows if r.get("column_name")}


def _table_exists(schema: str, table: str) -> bool:
    row = fetchone(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, table),
    )
    return bool(row)


def _load_route_stop_node_ids(route_id: str) -> List[str]:
    """
    Read route_prod.routes.stop_node_ids (uuid[]).
    """
    if not _table_exists("route_prod", "routes"):
        raise RuntimeError("Missing table route_prod.routes")

    cols = _columns("route_prod", "routes")
    if "stop_node_ids" not in cols:
        raise RuntimeError("route_prod.routes does not have stop_node_ids column")

    row = fetchone(
        """
        SELECT stop_node_ids
        FROM route_prod.routes
        WHERE route_id = %s
        """,
        (route_id,),
    )
    if not row:
        raise RuntimeError(f"route_id not found: {route_id}")

    stop_ids = row.get("stop_node_ids") or []
    return [str(x) for x in stop_ids]


def _load_stop_osm_node_ids(stop_node_ids: List[str], *, route_id: str) -> List[int]:
    """
    Phase-3 aligned stop→OSM lookup.

    IMPORTANT:
    - We DO NOT use node_prod.nodes at all.
    - We read OSM member refs from Phase 3:
        route_work.relation_stop_prior(route_id, seq, member_type, osm_ref, osm_node_id)

    Logic:
    - Prefer rows where member_type='node' and osm_ref is present (osm_ref is OSM node id).
    - Fallback to legacy osm_node_id if present.
    - Preserve order by seq, de-dup.
    - stop_node_ids param is accepted for compatibility but not used in this strategy.
    """
    if not _table_exists("route_work", "relation_stop_prior"):
        raise RuntimeError("Missing table route_work.relation_stop_prior (Phase 3 stop prior not available).")

    cols = _columns("route_work", "relation_stop_prior")
    needed_any = {"osm_ref", "osm_node_id"}  # must have at least one
    if not (needed_any & cols):
        raise RuntimeError(
            "route_work.relation_stop_prior missing both osm_ref and osm_node_id. "
            "Need at least one to map to OSM nodes."
        )
    if "seq" not in cols:
        raise RuntimeError("route_work.relation_stop_prior must have seq column for ordering.")

    # member_type is optional (older table versions). If missing/null, we still accept osm_node_id/osm_ref.
    has_member_type = "member_type" in cols

    if has_member_type:
        rows = fetchall(
            """
            SELECT member_type, osm_ref, osm_node_id
            FROM route_work.relation_stop_prior
            WHERE route_id = %s
            ORDER BY seq ASC
            """,
            (route_id,),
        )
    else:
        rows = fetchall(
            """
            SELECT NULL::text AS member_type, osm_ref, osm_node_id
            FROM route_work.relation_stop_prior
            WHERE route_id = %s
            ORDER BY seq ASC
            """,
            (route_id,),
        )

    ordered: List[int] = []
    for r in rows:
        mtype = (r.get("member_type") or "").lower()
        osm_ref = r.get("osm_ref")
        legacy = r.get("osm_node_id")

        # Best case: explicit node member with osm_ref
        if mtype == "node" and osm_ref is not None:
            try:
                ordered.append(int(osm_ref))
            except Exception:
                pass
            continue

        # If member_type missing/empty, treat osm_ref as node id if it exists
        if (not mtype) and osm_ref is not None:
            try:
                ordered.append(int(osm_ref))
            except Exception:
                pass
            continue

        # Fallback: legacy osm_node_id
        if legacy is not None:
            try:
                ordered.append(int(legacy))
            except Exception:
                pass

    # de-dup preserve order
    seen: Set[int] = set()
    out: List[int] = []
    for nid in ordered:
        if nid in seen:
            continue
        seen.add(nid)
        out.append(nid)

    if not out:
        raise RuntimeError(
            "Phase 3 stop prior produced 0 OSM node ids for this route. "
            "Check route_work.relation_stop_prior: you need member_type='node' with osm_ref, "
            "or legacy osm_node_id populated."
        )

    return out


def _load_seed_relations_for_route(route_id: str, sample_version: str) -> List[Dict[str, Any]]:
    """
    We support two layouts:
      A) route_prod.routes has osm_relation_id (already chosen/approved)
      B) semantics.route_seed_candidates exists (you persist multiple candidates)

    Return rows with at least:
      { "relation_id": int, "overlap_ratio_hint": Optional[float], "overlap_count_hint": Optional[int] }
    """
    # A) simplest: a single relation on route row
    route_cols = _columns("route_prod", "routes")
    if "osm_relation_id" in route_cols:
        row = fetchone(
            """
            SELECT osm_relation_id
            FROM route_prod.routes
            WHERE route_id = %s
            """,
            (route_id,),
        )
        if row and row.get("osm_relation_id"):
            return [{
                "relation_id": int(row["osm_relation_id"]),
                "overlap_ratio_hint": None,
                "overlap_count_hint": None,
            }]

    # B) optional candidates table
    if _table_exists("semantics", "route_seed_candidates"):
        rows = fetchall(
            """
            SELECT
              relation_id,
              overlap_ratio AS overlap_ratio_hint,
              overlap_count AS overlap_count_hint
            FROM semantics.route_seed_candidates
            WHERE route_id = %s AND sample_version = %s
            ORDER BY overlap_ratio DESC NULLS LAST, overlap_count DESC NULLS LAST
            """,
            (route_id, sample_version),
        )
        out: List[Dict[str, Any]] = []
        for r in rows:
            if r.get("relation_id") is None:
                continue
            out.append({
                "relation_id": int(r["relation_id"]),
                "overlap_ratio_hint": float(r["overlap_ratio_hint"]) if r.get("overlap_ratio_hint") is not None else None,
                "overlap_count_hint": int(r["overlap_count_hint"]) if r.get("overlap_count_hint") is not None else None,
            })
        return out

    return []


def _fetch_relation_member_nodes_from_overpass_cache(relation_id: int) -> Optional[Set[int]]:
    """
    Optional: if you cache Overpass expanded member nodes in DB.
    We support a generic cache table if it exists:
      semantics.overpass_relation_cache(relation_id, member_node_ids int[])
    """
    if not _table_exists("semantics", "overpass_relation_cache"):
        return None

    row = fetchone(
        """
        SELECT member_node_ids
        FROM semantics.overpass_relation_cache
        WHERE relation_id = %s
        """,
        (relation_id,),
    )
    if not row or not row.get("member_node_ids"):
        return None

    member_ids = row["member_node_ids"] or []
    out: Set[int] = set()
    for x in member_ids:
        try:
            out.add(int(x))
        except Exception:
            continue
    return out


def _fetch_relation_member_nodes_live(relation_id: int) -> Set[int]:
    """
    Live Overpass fetch fallback (no cache).
    """
    from phase4_semantics.ingest.overpass.fetch_relation_members import fetch_relation_members

    resp = fetch_relation_members(relation_id=relation_id)
    out: Set[int] = set()

    for el in resp.get("elements", []):
        t = el.get("type")
        if t == "node" and "id" in el:
            try:
                out.add(int(el["id"]))
            except Exception:
                pass
        elif t == "way":
            for nid in (el.get("nodes") or []):
                try:
                    out.add(int(nid))
                except Exception:
                    pass

    return out


def _compute_overlap(member_nodes: Set[int], stop_nodes: Set[int]) -> Tuple[int, float, float]:
    """
    Returns: (overlap_count, overlap_ratio_stop, overlap_ratio_relation)
    """
    if not stop_nodes or not member_nodes:
        return (0, 0.0, 0.0)

    overlap = member_nodes.intersection(stop_nodes)
    oc = len(overlap)

    ratio_stop = oc / max(1, len(stop_nodes))
    ratio_rel = oc / max(1, len(member_nodes))

    return (oc, float(ratio_stop), float(ratio_rel))


def compute_intersections(route_id: str, sample_version: str = "v1") -> List[Dict[str, Any]]:
    """
    Phase-4 intersection stage (aligned with run_phase4.py).

    Updated behavior:
      1) Get stop_node_ids from route_prod.routes
      2) Get OSM node ids from Phase 3 route_work.relation_stop_prior (NOT node_prod)
      3) Get 1+ candidate relations for the route (route_prod.routes.osm_relation_id
         or semantics.route_seed_candidates)
      4) For each relation, fetch member nodes (cache if available, else live Overpass)
      5) Compute intersection metrics
      6) Optionally persist into semantics.route_intersections if that table exists
    """
    stop_node_ids = _load_route_stop_node_ids(route_id)
    stop_osm_ids = _load_stop_osm_node_ids(stop_node_ids, route_id=route_id)
    stop_set = set(stop_osm_ids)

    seeds = _load_seed_relations_for_route(route_id, sample_version=sample_version)
    if not seeds:
        return []

    results: List[Dict[str, Any]] = []

    for s in seeds:
        relation_id = int(s["relation_id"])

        member_nodes = _fetch_relation_member_nodes_from_overpass_cache(relation_id)
        if member_nodes is None:
            member_nodes = _fetch_relation_member_nodes_live(relation_id)

        oc, r_stop, r_rel = _compute_overlap(member_nodes, stop_set)

        results.append({
            "route_id": str(route_id),
            "sample_version": str(sample_version),
            "relation_id": relation_id,
            "stop_osm_nodes": len(stop_set),
            "relation_member_nodes": len(member_nodes),
            "overlap_count": int(oc),
            "overlap_ratio_stop": float(r_stop),
            "overlap_ratio_relation": float(r_rel),
            "overlap_ratio_hint": s.get("overlap_ratio_hint"),
            "overlap_count_hint": s.get("overlap_count_hint"),
        })

    results.sort(
        key=lambda x: (
            int(x.get("overlap_count", 0)),
            float(x.get("overlap_ratio_stop", 0.0)),
            float(x.get("overlap_ratio_relation", 0.0)),
        ),
        reverse=True,
    )

    # Persist if table exists (optional, non-breaking)
    if _table_exists("semantics", "route_intersections"):
        fetchone(
            """
            DELETE FROM semantics.route_intersections
            WHERE route_id = %s AND sample_version = %s
            """,
            (route_id, sample_version),
        )
        for r in results:
            fetchone(
                """
                INSERT INTO semantics.route_intersections (
                  route_id,
                  sample_version,
                  relation_id,
                  stop_osm_nodes,
                  relation_member_nodes,
                  overlap_count,
                  overlap_ratio_stop,
                  overlap_ratio_relation,
                  overlap_ratio_hint,
                  overlap_count_hint
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    r["route_id"],
                    r["sample_version"],
                    r["relation_id"],
                    r["stop_osm_nodes"],
                    r["relation_member_nodes"],
                    r["overlap_count"],
                    r["overlap_ratio_stop"],
                    r["overlap_ratio_relation"],
                    r["overlap_ratio_hint"],
                    r["overlap_count_hint"],
                ),
            )

    return results
