from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from psycopg2.extras import execute_values
from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
    exec_sql,
    fetchone,
    fetchall,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_NODE_CANDIDATES,
    T_NODE_FEATURES,
)

try:
    from phase1_nodes.datamind.services.openmaps_extractor.src.models.stop_poi.lgbm import (
        predict as lgbm_predict,
    )
except Exception:  # pragma: no cover
    lgbm_predict = None

from phase1_nodes.datamind.services.openmaps_extractor.src.models.stop_poi.baseline import (
    predict_from_tags,
)

logger = logging.getLogger(__name__)


# ============================================================
# Tag category classification (for expanded features)
# ============================================================

_TAG_CATEGORY_MAP = [
    # TRANSIT indicators (highest priority)
    (("highway", "bus_stop"), "TRANSIT"),
    (("public_transport", "platform"), "TRANSIT"),
    (("public_transport", "stop_position"), "TRANSIT"),
    (("public_transport", "station"), "TRANSIT"),
    (("amenity", "bus_station"), "TRANSIT"),
    (("railway", "tram_stop"), "TRANSIT"),
    (("railway", "halt"), "TRANSIT"),
    (("railway", "station"), "TRANSIT"),
    # COMMERCIAL
    (("amenity", "cafe"), "COMMERCIAL"),
    (("amenity", "restaurant"), "COMMERCIAL"),
    (("amenity", "pharmacy"), "COMMERCIAL"),
    (("amenity", "bank"), "COMMERCIAL"),
    (("amenity", "fast_food"), "COMMERCIAL"),
    (("amenity", "fuel"), "COMMERCIAL"),
    # CIVIC
    (("amenity", "school"), "CIVIC"),
    (("amenity", "hospital"), "CIVIC"),
    (("amenity", "clinic"), "CIVIC"),
    (("amenity", "police"), "CIVIC"),
    (("amenity", "place_of_worship"), "CIVIC"),
    (("amenity", "townhall"), "CIVIC"),
    # RECREATION
    (("amenity", "park"), "RECREATION"),
    (("leisure", "park"), "RECREATION"),
]


def _classify_primary_tag(tags: Dict[str, Any]) -> str:
    """Determine primary category from OSM tags."""
    for (key, value), category in _TAG_CATEGORY_MAP:
        if tags.get(key) == value:
            return category
    # Wildcard checks
    if any(k in tags for k in ("bus", "public_transport", "railway")):
        return "TRANSIT"
    if "shop" in tags and tags["shop"]:
        return "COMMERCIAL"
    if "tourism" in tags and tags["tourism"]:
        return "RECREATION"
    return "UNKNOWN"


def _compute_tag_features(tags: Dict[str, Any]) -> Dict[str, Any]:
    """Compute expanded tag-based features from OSM tags."""
    return {
        "has_shelter": tags.get("shelter") in ("yes", "pole", "covered"),
        "has_bench": tags.get("bench") == "yes",
        "has_route_ref": bool(tags.get("route_ref")),
        "primary_tag_category": _classify_primary_tag(tags),
        "tag_richness": len([k for k, v in tags.items() if v is not None and v != ""]),
    }


def _coerce_tags(tags: Any) -> Dict[str, Any]:
    """
    Ensure tags is always a dict (jsonb) to avoid .get/.items errors.
    - If tags is already dict -> return as-is
    - If tags is a JSON string -> parse
    - Else -> empty dict
    """
    if isinstance(tags, dict):
        return tags
    if isinstance(tags, str):
        # Sometimes drivers accidentally store json as text.
        # Try to parse; if not, return empty.
        try:
            import json
            obj = json.loads(tags)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def run_features(node_set_id: str) -> dict:
    """
    Phase 1 – FEATURES (Nodes)

    A) Heuristic features (fast, deterministic)
    B) STOP vs POI classification:
       - LightGBM if artifact exists
       - fallback to baseline heuristic if not
    C) Persist into node_work.node_features

    Robustness fixes included:
    - Prevent uuid=text error by keeping UUID objects + casting in SQL
    - Coerce tags to dict so baseline never crashes
    - Use UPSERT for predictions to avoid UPDATE join typing issues
    """

    with db_conn() as conn:
        # ------------------------------------------------------------
        # Guard: candidates exist?
        # ------------------------------------------------------------
        row0 = fetchone(
            conn,
            f"""
            SELECT COUNT(*) AS n
            FROM {T_NODE_CANDIDATES}
            WHERE node_set_id = %s::uuid
            """,
            (node_set_id,),
        )
        n_candidates = int(row0["n"] or 0)

        if n_candidates == 0:
            logger.info("features.run.empty node_set_id=%s", node_set_id)
            return {"node_set_id": node_set_id, "features_rows": 0, "candidates": 0}

        logger.info("features.run.start node_set_id=%s candidates=%s", node_set_id, n_candidates)

        # ------------------------------------------------------------
        # Step A: heuristic features
        # ------------------------------------------------------------
        exec_sql(
            conn,
            f"""
            INSERT INTO {T_NODE_FEATURES}
              (node_candidate_id, has_name, has_ref, has_operator, confidence_v0)
            SELECT
              c.node_candidate_id,
              (c.tags ? 'name')     AS has_name,
              (c.tags ? 'ref')      AS has_ref,
              (c.tags ? 'operator') AS has_operator,
              (
                0.30
                + CASE WHEN (c.tags ? 'name')     THEN 0.25 ELSE 0 END
                + CASE WHEN (c.tags ? 'ref')      THEN 0.15 ELSE 0 END
                + CASE WHEN (c.tags ? 'operator') THEN 0.10 ELSE 0 END
                + CASE WHEN c.tag_kind = 'station' THEN 0.10 ELSE 0 END
              )::double precision AS confidence_v0
            FROM {T_NODE_CANDIDATES} c
            WHERE c.node_set_id = %s::uuid
            ON CONFLICT (node_candidate_id) DO UPDATE SET
              has_name      = EXCLUDED.has_name,
              has_ref       = EXCLUDED.has_ref,
              has_operator  = EXCLUDED.has_operator,
              confidence_v0 = EXCLUDED.confidence_v0,
              computed_at   = now()
            """,
            (node_set_id,),
        )

        # ------------------------------------------------------------
        # Step B: load rows and compute expanded features
        # ------------------------------------------------------------
        rows = fetchall(
            conn,
            f"""
            SELECT
              c.node_candidate_id,
              c.tags,
              c.tag_kind,
              f.has_name,
              f.has_ref,
              f.has_operator,
              f.confidence_v0
            FROM {T_NODE_CANDIDATES} c
            JOIN {T_NODE_FEATURES} f
              ON f.node_candidate_id = c.node_candidate_id
            WHERE c.node_set_id = %s::uuid
            """,
            (node_set_id,),
        )

        logger.info("features.run.loaded node_set_id=%s rows=%s", node_set_id, len(rows))

        # We'll UPSERT predictions + expanded features keyed by node_candidate_id
        pred_rows: List[Tuple] = []
        fallback_logged = False

        for r in rows:
            node_candidate_id = r["node_candidate_id"]
            tag_kind = r["tag_kind"]
            tags = _coerce_tags(r["tags"])

            # Compute expanded tag features
            tag_feats = _compute_tag_features(tags)

            features_row: Dict[str, Any] = {
                "has_name": r["has_name"],
                "has_ref": r["has_ref"],
                "has_operator": r["has_operator"],
                "tag_kind": tag_kind,
                "confidence_v0": r["confidence_v0"],
                "has_shelter": tag_feats["has_shelter"],
                "has_bench": tag_feats["has_bench"],
                "has_route_ref": tag_feats["has_route_ref"],
                "primary_tag_category": tag_feats["primary_tag_category"],
                "tag_richness": tag_feats["tag_richness"],
            }

            try:
                if callable(lgbm_predict):
                    prob_stop, prob_poi, pred, model_ver = lgbm_predict(features_row)
                else:
                    raise RuntimeError("lightgbm_unavailable")
            except Exception as e:
                if not fallback_logged:
                    logger.warning(
                        "features.fallback node_candidate_id=%s tag_kind=%s error=%s",
                        node_candidate_id,
                        tag_kind,
                        str(e),
                    )
                    fallback_logged = True
                prob_stop, prob_poi, pred, model_ver = predict_from_tags(tags, tag_kind)

            pred_rows.append(
                (
                    node_candidate_id,
                    float(prob_stop),
                    float(prob_poi),
                    str(pred),
                    str(model_ver),
                    tag_feats["has_shelter"],
                    tag_feats["has_bench"],
                    tag_feats["has_route_ref"],
                    tag_feats["primary_tag_category"],
                    tag_feats["tag_richness"],
                )
            )

        # ------------------------------------------------------------
        # Step C: persist predictions + expanded features (UPSERT)
        # ------------------------------------------------------------
        if pred_rows:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    f"""
                    INSERT INTO {T_NODE_FEATURES}
                      (node_candidate_id, prob_stop, prob_poi, node_class_pred, model_version,
                       has_shelter, has_bench, has_route_ref, primary_tag_category, tag_richness,
                       computed_at)
                    VALUES %s
                    ON CONFLICT (node_candidate_id) DO UPDATE SET
                      prob_stop             = EXCLUDED.prob_stop,
                      prob_poi              = EXCLUDED.prob_poi,
                      node_class_pred       = EXCLUDED.node_class_pred,
                      model_version         = EXCLUDED.model_version,
                      has_shelter           = EXCLUDED.has_shelter,
                      has_bench             = EXCLUDED.has_bench,
                      has_route_ref         = EXCLUDED.has_route_ref,
                      primary_tag_category  = EXCLUDED.primary_tag_category,
                      tag_richness          = EXCLUDED.tag_richness,
                      computed_at           = now()
                    """,
                    pred_rows,
                    template=(
                        "(%s::uuid, %s::double precision, %s::double precision, %s::text, %s::text,"
                        " %s::boolean, %s::boolean, %s::boolean, %s::text, %s::integer, now())"
                    ),
                )

        # ------------------------------------------------------------
        # Final count
        # ------------------------------------------------------------
        row1 = fetchone(
            conn,
            f"""
            SELECT COUNT(*) AS n
            FROM {T_NODE_FEATURES} f
            JOIN {T_NODE_CANDIDATES} c
              ON c.node_candidate_id = f.node_candidate_id
            WHERE c.node_set_id = %s::uuid
            """,
            (node_set_id,),
        )
        features_rows = int(row1["n"] or 0)

        logger.info("features.run.result node_set_id=%s features_rows=%s", node_set_id, features_rows)

    return {
        "node_set_id": node_set_id,
        "features_rows": features_rows,
        "candidates": n_candidates,
    }
