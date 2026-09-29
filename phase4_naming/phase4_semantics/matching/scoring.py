# phase4_semantics/matching/scoring.py
"""
Phase 4 – Scoring (aligned with run_phase4.py)

This module scores *intersection candidates* deterministically and explainably.

Pipeline alignment:
  - run_phase4.py calls: score_intersections(route_id, sample_version)
  - compute_intersections() produces intersection metrics per (route_id, relation_id)
  - seed_overpass_candidates() (or route_prod.routes.osm_relation_id) provides relation tags
  - we combine:
      (A) HARD signal: stop-osm ∩ relation-member overlap
      (B) SOFT semantic signals: token overlap, ref match, alias hit, name sanity

No ML. Pure heuristics. Always returns best-first.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple

from phase4_semantics.common.db import fetchall, fetchone
from phase4_semantics.matching.intersection import compute_intersections, semantic_token_overlap


# ============================================================
# Default weights (sum ~ 1.0)
# ============================================================

DEFAULT_WEIGHTS: Dict[str, float] = {
    # core geometric intersection signals (dominant)
    "overlap_ratio_stop": 0.55,
    "overlap_ratio_relation": 0.10,

    # semantic helpers (secondary)
    "token_overlap_ratio": 0.15,
    "ref_match": 0.10,
    "alias_hit": 0.05,
    "name_length_ok": 0.05,
}


# ============================================================
# DB introspection helpers
# ============================================================

def _table_exists(schema: str, table: str) -> bool:
    r = fetchone(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, table),
    )
    return bool(r)


def _columns(schema: str, table: str) -> Set[str]:
    rows = fetchall(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return {str(x["column_name"]) for x in rows if x.get("column_name")}


def _cap01(x: float) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


# ============================================================
# Route context helpers (for semantic signals)
# ============================================================

def _pick_route_label_col(cols: Set[str]) -> Optional[str]:
    """
    Try common columns that might store a human route name/ref in route_prod.routes.
    """
    for c in ("label", "title", "name", "route_name", "short_name", "ref"):
        if c in cols:
            return c
    return None


def _load_route_label_and_ref(route_id: str) -> Tuple[Optional[str], Optional[str]]:
    if not _table_exists("route_prod", "routes"):
        return (None, None)

    cols = _columns("route_prod", "routes")
    label_col = _pick_route_label_col(cols)

    # Try to read something meaningful. If a column doesn't exist, we keep None.
    parts = []
    if label_col:
        parts.append(f"{label_col}::text AS route_label")
    else:
        parts.append("NULL::text AS route_label")

    if "ref" in cols:
        parts.append("ref::text AS route_ref")
    else:
        parts.append("NULL::text AS route_ref")

    row = fetchone(
        f"""
        SELECT {", ".join(parts)}
        FROM route_prod.routes
        WHERE route_id = %s
        """,
        (route_id,),
    )
    if not row:
        return (None, None)

    lbl = row.get("route_label")
    ref = row.get("route_ref")
    return (str(lbl).strip() if lbl else None, str(ref).strip() if ref else None)


def _load_stop_names(route_id: str) -> List[str]:
    """
    Pull stop names (if available) from node_prod.nodes for the route stop_node_ids.
    """
    if not _table_exists("route_prod", "routes"):
        return []
    if not _table_exists("node_prod", "nodes"):
        return []

    rcols = _columns("route_prod", "routes")
    ncols = _columns("node_prod", "nodes")

    if "stop_node_ids" not in rcols:
        return []
    if "node_id" not in ncols:
        return []
    if "name" not in ncols:
        # no names available -> semantic overlap can't use stop names
        return []

    rows = fetchall(
        """
        SELECT n.name::text AS name
        FROM (
          SELECT unnest(stop_node_ids) AS node_id
          FROM route_prod.routes
          WHERE route_id = %s
        ) s
        JOIN node_prod.nodes n
          ON n.node_id = s.node_id
        WHERE n.name IS NOT NULL AND btrim(n.name::text) <> ''
        """,
        (route_id,),
    )
    return [str(r["name"]).strip() for r in rows if r.get("name")]


# ============================================================
# Relation tags helpers (semantic evidence)
# ============================================================

def _load_relation_tags_from_seed_table(route_id: str, sample_version: str, relation_id: int) -> Optional[Dict[str, str]]:
    """
    If you persisted seed candidates somewhere, we can reuse their tags.

    Supported table:
      semantics.route_seed_candidates(route_id, sample_version, relation_id, relation_tags jsonb)
    """
    if not _table_exists("semantics", "route_seed_candidates"):
        return None

    cols = _columns("semantics", "route_seed_candidates")
    if "relation_tags" not in cols:
        return None

    row = fetchone(
        """
        SELECT relation_tags
        FROM semantics.route_seed_candidates
        WHERE route_id = %s AND sample_version = %s AND relation_id = %s
        LIMIT 1
        """,
        (route_id, sample_version, int(relation_id)),
    )
    if not row or row.get("relation_tags") is None:
        return None

    tags = row["relation_tags"] or {}
    # ensure str->str
    out: Dict[str, str] = {}
    if isinstance(tags, dict):
        for k, v in tags.items():
            if k is None or v is None:
                continue
            out[str(k)] = str(v)
    return out or None


def _fetch_relation_tags_live(relation_id: int) -> Dict[str, str]:
    """
    Cheap fallback: tags-only overpass call.
    """
    from phase4_semantics.ingest.overpass.fetch_relation_members import (
        fetch_relation_tags_only,
        get_relation_tags,
    )

    resp = fetch_relation_tags_only(relation_id=int(relation_id))
    return get_relation_tags(resp, relation_id=int(relation_id)) or {}


def _relation_aliases(tags: Dict[str, str]) -> List[str]:
    """
    Build a small alias list from common OSM keys.
    """
    keys = ("ref", "short_name", "official_name", "alt_name", "name")
    out: List[str] = []
    for k in keys:
        v = tags.get(k)
        if v:
            vv = str(v).strip()
            if vv and vv not in out:
                out.append(vv)
    return out


# ============================================================
# Core scoring
# ============================================================

def score_semantic_evidence(
    evidence: Dict[str, object],
    weights: Dict[str, float] = DEFAULT_WEIGHTS,
) -> Tuple[float, Dict[str, float]]:
    """
    Generic weighted scorer for evidence dict.
    """
    score = 0.0
    breakdown: Dict[str, float] = {}

    for signal, weight in weights.items():
        value = evidence.get(signal)

        if value is None:
            contrib = 0.0
        elif isinstance(value, bool):
            contrib = float(weight) if value else 0.0
        elif isinstance(value, (int, float)):
            contrib = float(weight) * _cap01(float(value))
        else:
            contrib = 0.0

        breakdown[signal] = float(contrib)
        score += float(contrib)

    return _cap01(score), breakdown


def score_intersections(
    route_id: str,
    sample_version: str = "v1",
    weights: Dict[str, float] = DEFAULT_WEIGHTS,
) -> List[Dict[str, Any]]:
    """
    ALIGNED ENTRYPOINT for run_phase4.py

    Returns list of scored candidates, best-first.
    Each row includes:
      - relation_id
      - score
      - breakdown (per-signal contribution)
      - evidence (raw feature values)
      - tags (if available)
      - intersection metrics
    """
    # 1) Get intersection candidates (from table or live compute)
    inters = compute_intersections(route_id=route_id, sample_version=sample_version)
    if not inters:
        return []

    # 2) Load optional semantic context
    route_label, route_ref = _load_route_label_and_ref(route_id)
    stop_names = _load_stop_names(route_id)

    scored_rows: List[Dict[str, Any]] = []

    for it in inters:
        rid = int(it["relation_id"])

        # 3) Tags: prefer seed table; else live tags-only fetch
        tags = _load_relation_tags_from_seed_table(route_id, sample_version, rid)
        if tags is None:
            tags = _fetch_relation_tags_live(rid)

        # 4) Build semantic evidence
        rel_name = (tags.get("name") or "").strip()
        rel_ref = (tags.get("ref") or "").strip()
        aliases = _relation_aliases(tags)

        # token overlap uses route name-like string vs stop names
        # Prefer relation "name" if it exists; else route_label; else empty.
        name_for_tokens = rel_name or (route_label or "")
        tok = semantic_token_overlap(name_for_tokens, stop_names) if stop_names and name_for_tokens else {
            "token_overlap_count": 0,
            "token_overlap_ratio": 0.0,
        }

        # ref match: if you have route_ref in route_prod.routes and relation has ref
        ref_match = bool(route_ref and rel_ref and route_ref.strip().lower() == rel_ref.strip().lower())

        # alias hit: do any alias tokens appear in stop-name tokens? (weak but useful)
        alias_hit = False
        if stop_names and aliases:
            stop_tok: Set[str] = set()
            for s in stop_names:
                stop_tok |= {t.lower() for t in re.split(r"[\s\-_/]+", s or "") if t}
            for a in aliases:
                a_tok = {t.lower() for t in re.split(r"[\s\-_/]+", a or "") if t}
                if a_tok and (a_tok & stop_tok):
                    alias_hit = True
                    break

        name_length_ok = bool(rel_name and len(rel_name) >= 4)

        # 5) Combine intersection + semantic into one evidence dict
        evidence: Dict[str, object] = {
            "overlap_ratio_stop": float(it.get("overlap_ratio_stop") or 0.0),
            "overlap_ratio_relation": float(it.get("overlap_ratio_relation") or 0.0),
            "token_overlap_ratio": float(tok.get("token_overlap_ratio") or 0.0),
            "ref_match": bool(ref_match),
            "alias_hit": bool(alias_hit),
            "name_length_ok": bool(name_length_ok),
        }

        score, breakdown = score_semantic_evidence(evidence=evidence, weights=weights)

        scored_rows.append({
            "route_id": str(route_id),
            "sample_version": str(sample_version),
            "relation_id": rid,
            "score": float(score),
            "breakdown": breakdown,
            "evidence": evidence,
            "intersection": {
                "overlap_count": int(it.get("overlap_count") or 0),
                "stop_osm_nodes": int(it.get("stop_osm_nodes") or 0),
                "relation_member_nodes": int(it.get("relation_member_nodes") or 0),
                "overlap_ratio_stop": float(it.get("overlap_ratio_stop") or 0.0),
                "overlap_ratio_relation": float(it.get("overlap_ratio_relation") or 0.0),
            },
            "relation_tags": tags,
        })

    # 6) Best-first sort
    scored_rows.sort(
        key=lambda r: (
            float(r.get("score") or 0.0),
            int((r.get("intersection") or {}).get("overlap_count") or 0),
        ),
        reverse=True,
    )

    # 7) Optional persist (non-breaking)
    if _table_exists("semantics", "route_scored_candidates"):
        # delete then insert per (route_id, sample_version)
        fetchone(
            """
            DELETE FROM semantics.route_scored_candidates
            WHERE route_id = %s AND sample_version = %s
            """,
            (route_id, sample_version),
        )
        for r in scored_rows:
            fetchone(
                """
                INSERT INTO semantics.route_scored_candidates (
                  route_id,
                  sample_version,
                  relation_id,
                  score,
                  breakdown,
                  evidence,
                  relation_tags
                )
                VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
                """,
                (
                    r["route_id"],
                    r["sample_version"],
                    r["relation_id"],
                    r["score"],
                    r["breakdown"],
                    r["evidence"],
                    r["relation_tags"],
                ),
            )

    return scored_rows
