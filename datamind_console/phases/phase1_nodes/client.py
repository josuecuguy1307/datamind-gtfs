from __future__ import annotations

from functools import lru_cache
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional
from uuid import UUID

import streamlit as st

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
    exec_sql,
    fetchall,
    fetchone,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.settings import T_RESOLVED

from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.area_targeting import (
    match_sector_alias,
    normalize_area_text,
    sector_catalog_as_rows,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.db.phase1_repo import (
    update_node_set_params_used,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.build_node_set_job import (
    run_build_node_set as build_node_set,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline import extraction_policy as _extraction_policy
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.normalize_job import (
    run_normalize as normalize,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.features_job import (
    run_features as extract_features,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.cluster_job import (
    run_cluster as cluster,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.resolve_job import (
    run_resolve as resolve,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.rank_sets_job import (
    run_rank_set as rank,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.promote_job import (
    run_promote as promote,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.resolve_job import (
    run_resolve,
)
from phase1_nodes.datamind.services.openmaps_extractor.src.overpass.actions import build_query
from phase1_nodes.datamind.services.openmaps_extractor.src.utils.geo import haversine_m

try:
    from datamind_console.ai_insights.telemetry import log_phase1_run as _ai_log_phase1_run
except Exception:
    _ai_log_phase1_run = None


bbox_retry_plan = _extraction_policy.bbox_retry_plan
is_low_quality = _extraction_policy.is_low_quality
ordered_actions_for_area = _extraction_policy.ordered_actions_for_area
quality_threshold_for_area = _extraction_policy.quality_threshold_for_area
save_sector_recommendation = _extraction_policy.save_sector_recommendation
score_extraction_quality = _extraction_policy.score_extraction_quality


def _fallback_prioritize_actions_with_recommendation(
    action_order: List[str],
    *,
    preferred_action: Optional[str],
) -> List[str]:
    pref = str(preferred_action or "").strip()
    if not pref:
        return list(action_order)
    ordered = [str(a) for a in list(action_order or []) if str(a).strip()]
    if pref not in ordered:
        return ordered
    return [pref] + [a for a in ordered if a != pref]


def _fallback_sector_recommendation_for_context(
    *,
    sector_key: Optional[str],
    area_group: Optional[str],
    path: Optional[str] = None,
) -> Dict[str, Any]:
    del sector_key, area_group, path
    return {}


prioritize_actions_with_recommendation = getattr(
    _extraction_policy,
    "prioritize_actions_with_recommendation",
    _fallback_prioritize_actions_with_recommendation,
)
sector_recommendation_for_context = getattr(
    _extraction_policy,
    "sector_recommendation_for_context",
    _fallback_sector_recommendation_for_context,
)


@st.cache_resource
def _get_phase1_client(_version: str = "v3") -> "Phase1Client":
    return Phase1Client()


class Phase1Client:
    """
    UI / API safe Phase 1 client.
    Wraps atomic pipeline functions.
    """

    # -----------------------------
    # Queries (used by UI)
    # -----------------------------

    def list_node_sets(self, limit: int = 4000):
        return self._node_set_summary_rows(limit=max(1, int(limit)))

    @staticmethod
    def _normalize_node_set_id(node_set_id: UUID | str | Any) -> str | None:
        txt = str(node_set_id or "").strip()
        if not txt:
            return None
        try:
            return str(uuid.UUID(txt))
        except Exception:
            return None


    def get_node_set_summary(self, node_set_id: UUID | str) -> Dict[str, Any]:
        normalized = self._normalize_node_set_id(node_set_id)
        if normalized is None:
            return {
                "node_set_id": str(node_set_id),
                "status": "invalid_node_set_id",
                "invalid_node_set_id": True,
            }
        rows = self._node_set_summary_rows(node_set_id=normalized, limit=1)
        return rows[0] if rows else {"node_set_id": str(node_set_id), "status": "not_found"}

    def delete_node_set(
        self,
        node_set_id: UUID | str,
        *,
        delete_prod_nodes: bool = False,
    ) -> Dict[str, Any]:
        """
        Delete a Phase 1 node_set and its node_work artifacts.
        - Removes selection_log rows that RESTRICT set deletion.
        - Optionally deletes node_prod.nodes rows sourced from this node_set.
        """
        nid = str(node_set_id)
        out: Dict[str, Any] = {
            "node_set_id": nid,
            "deleted_selection_logs": 0,
            "deleted_prod_nodes": 0,
            "deleted_node_set": 0,
        }
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT to_regclass('node_work.selection_log') IS NOT NULL AS ok
                    """
                )
                has_sel = bool((cur.fetchone() or {}).get("ok"))
                if has_sel:
                    cur.execute(
                        """
                        UPDATE node_work.selection_log
                        SET rejected_set_ids = array_remove(COALESCE(rejected_set_ids, ARRAY[]::uuid[]), %s::uuid)
                        WHERE %s::uuid = ANY(COALESCE(rejected_set_ids, ARRAY[]::uuid[]))
                        """,
                        (nid, nid),
                    )
                    cur.execute(
                        """
                        DELETE FROM node_work.selection_log
                        WHERE chosen_set_id::text = %s
                        """,
                        (nid,),
                    )
                    out["deleted_selection_logs"] = int(cur.rowcount or 0)

                if delete_prod_nodes:
                    cur.execute(
                        """
                        DELETE FROM node_prod.nodes
                        WHERE source_node_set_id::text = %s
                        """,
                        (nid,),
                    )
                    out["deleted_prod_nodes"] = int(cur.rowcount or 0)

                cur.execute(
                    """
                    DELETE FROM node_work.node_candidate_sets
                    WHERE node_set_id::text = %s
                    """,
                    (nid,),
                )
                out["deleted_node_set"] = int(cur.rowcount or 0)

        if out["deleted_node_set"] == 0:
            raise RuntimeError(f"node_set_id not found: {nid}")
        return out

    # -----------------------------
    # Decisions (used by UI)
    # -----------------------------

    def run_resolve_step(self, node_set_id: UUID | str) -> Dict[str, Any]:
        """Run the resolve pipeline step (best-per-cluster selection).
        Previously named 'approve' which was misleading — this resolves,
        not approves. The actual approve is approve_all_resolved()."""
        return run_resolve(str(node_set_id))

    def reject(self, node_set_id: UUID | str) -> Dict[str, Any]:
        with db_conn() as conn:
            exec_sql(
                conn,
                f"""
                UPDATE {T_RESOLVED}
                SET status = 'rejected'
                WHERE node_set_id = %s
                """,
                (str(node_set_id),),
            )
        return {"node_set_id": str(node_set_id), "status": "rejected"}

    # -----------------------------
    # Step wrappers (Phase 1 UI)
    # -----------------------------

    @staticmethod
    def _as_uuid_list(raw: Any) -> List[str]:
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw if str(x or "").strip()]
        txt = str(raw).strip()
        if not txt:
            return []
        if txt.startswith("{") and txt.endswith("}"):
            inner = txt[1:-1].strip()
            if not inner:
                return []
            return [p.strip().strip('"') for p in inner.split(",") if p.strip()]
        return [txt]

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[3]

    def _resolve_actions_path(self, actions_path: str) -> str:
        p = Path(str(actions_path))
        if p.is_absolute():
            return str(p)
        return str(self._repo_root() / p)

    @staticmethod
    def _bbox_to_string(bbox: Dict[str, float]) -> str:
        return f"{float(bbox['south'])},{float(bbox['west'])},{float(bbox['north'])},{float(bbox['east'])}"

    @staticmethod
    def _safe_ratio(numer: Any, denom: Any) -> float:
        try:
            n = float(numer)
            d = float(denom)
            if d <= 0:
                return 0.0
            return n / d
        except Exception:
            return 0.0

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return int(default)

    @staticmethod
    def _coerce_bbox_payload(raw: Any) -> Optional[Dict[str, float]]:
        if isinstance(raw, dict):
            keys = {"south", "west", "north", "east"}
            if keys.issubset(set(raw.keys())):
                try:
                    return {
                        "south": float(raw["south"]),
                        "west": float(raw["west"]),
                        "north": float(raw["north"]),
                        "east": float(raw["east"]),
                    }
                except Exception:
                    return None
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            if len(parts) == 4:
                try:
                    return {
                        "south": float(parts[0]),
                        "west": float(parts[1]),
                        "north": float(parts[2]),
                        "east": float(parts[3]),
                    }
                except Exception:
                    return None
        return None

    @staticmethod
    def _as_text_list(raw: Any) -> List[str]:
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return [str(x).strip() for x in raw if str(x or "").strip()]
        txt = str(raw or "").strip()
        if not txt:
            return []
        if txt.startswith("{") and txt.endswith("}"):
            inner = txt[1:-1].strip()
            if not inner:
                return []
            return [p.strip().strip('"') for p in inner.split(",") if p.strip()]
        return [txt]

    @classmethod
    def _extractor_review_payload(
        cls,
        params_used: Any,
        *,
        action_ids: Any = None,
    ) -> Dict[str, Any]:
        params = dict(params_used or {}) if isinstance(params_used, dict) else {}
        review = dict(params.get("extractor_review") or {})
        geo = dict(params.get("geography_interpretation") or {}) if isinstance(params.get("geography_interpretation"), dict) else {}
        diagnostics = dict(params.get("extraction_diagnostics") or {}) if isinstance(params.get("extraction_diagnostics"), dict) else {}
        actions = cls._as_text_list(action_ids)
        bbox = cls._coerce_bbox_payload(review.get("bbox") or params.get("bbox"))
        bbox_str = review.get("bbox_str")
        if not bbox_str and bbox:
            bbox_str = (
                f"{bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"
            )
        return {
            "place_name": (
                str(
                    review.get("place_input")
                    or review.get("place_name")
                    or params.get("place_name")
                    or params.get("place_input")
                    or ""
                ).strip()
                or None
            ),
            "target_group": (
                str(
                    review.get("target_group")
                    or review.get("group")
                    or params.get("group")
                    or ""
                ).strip()
                or None
            ),
            "area_id": (str(review.get("area_id") or params.get("area_id") or "").strip() or None),
            "bbox": bbox,
            "bbox_str": (str(bbox_str).strip() if bbox_str else None),
            "interpretation_source": (
                str(review.get("interpretation_source") or geo.get("source") or "").strip() or None
            ),
            "interpreted_place_meaning": (
                str(review.get("interpreted_place_meaning") or geo.get("meaning") or "").strip() or None
            ),
            "interpretation_confidence": (
                review.get("interpretation_confidence")
                if review.get("interpretation_confidence") is not None
                else geo.get("confidence")
            ),
            "extraction_action": (
                str(review.get("action_id") or params.get("action_id") or (actions[0] if actions else "")).strip()
                or None
            ),
            "extractor_status": (str(review.get("status") or "").strip() or None),
            "extractor_runtime_ms": (
                review.get("runtime_ms")
                if review.get("runtime_ms") is not None
                else diagnostics.get("runtime_ms")
            ),
            "extractor_http_status": (
                review.get("http_status")
                if review.get("http_status") is not None
                else diagnostics.get("http_status")
            ),
            "extractor_element_count": (
                review.get("element_count")
                if review.get("element_count") is not None
                else diagnostics.get("raw_elements_count")
            ),
            "extractor_candidate_count": (
                review.get("candidate_count")
                if review.get("candidate_count") is not None
                else diagnostics.get("candidate_count")
            ),
            "extractor_stop_like_count": review.get("stop_like_count"),
            "extractor_poi_like_count": review.get("poi_like_count"),
            "target_file": (str(review.get("target_file") or "").strip() or None),
            "extractor_review": (review or {"geography_interpretation": geo, "extraction_diagnostics": diagnostics}),
        }

    def _node_set_summary_rows(
        self,
        *,
        node_set_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        where_sql = "WHERE s.node_set_id = %s::uuid" if node_set_id else ""
        limit_sql = "LIMIT %s" if limit is not None else ""
        params: List[Any] = []
        if node_set_id:
            params.append(str(node_set_id))
        if limit is not None:
            params.append(max(1, int(limit)))
        sql = f"""
        WITH candidate_counts AS (
          SELECT
            node_set_id,
            COUNT(*)::int AS candidate_count,
            COUNT(*) FILTER (
              WHERE COALESCE(tag_kind, '') IN ('bus_stop','platform','stop_position','station','tram_stop')
            )::int AS stop_like_count,
            COUNT(*) FILTER (
              WHERE COALESCE(tag_kind, '') NOT IN ('bus_stop','platform','stop_position','station','tram_stop')
            )::int AS poi_like_count
          FROM node_work.node_candidates
          GROUP BY node_set_id
        ),
        raw_counts AS (
          SELECT
            s.node_set_id,
            COALESCE(SUM(r.element_count), 0)::int AS raw_count
          FROM node_work.node_candidate_sets s
          LEFT JOIN LATERAL unnest(COALESCE(s.source_run_ids, ARRAY[]::uuid[])) AS src(run_id) ON TRUE
          LEFT JOIN node_raw.overpass_runs r
            ON r.run_id = src.run_id
          GROUP BY s.node_set_id
        )
        SELECT
          s.node_set_id,
          COALESCE(v.status, 'empty') AS status,
          s.created_at,
          v.resolved_at,
          COALESCE(v.n_resolved, 0)::int AS n_resolved,
          COALESCE(v.n_approved, 0)::int AS n_approved,
          COALESCE(v.n_work, 0)::int AS n_work,
          COALESCE(v.n_rejected, 0)::int AS n_rejected,
          s.action_ids,
          s.params_used,
          COALESCE(raw_counts.raw_count, 0)::int AS raw_count,
          COALESCE(candidate_counts.candidate_count, 0)::int AS candidate_count,
          COALESCE(candidate_counts.stop_like_count, 0)::int AS stop_like_count,
          COALESCE(candidate_counts.poi_like_count, 0)::int AS poi_like_count
        FROM node_work.node_candidate_sets s
        LEFT JOIN node_work.v_node_sets v
          ON v.node_set_id = s.node_set_id
        LEFT JOIN candidate_counts
          ON candidate_counts.node_set_id = s.node_set_id
        LEFT JOIN raw_counts
          ON raw_counts.node_set_id = s.node_set_id
        {where_sql}
        ORDER BY s.created_at DESC
        {limit_sql}
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, tuple(params)) or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row or {})
            item.update(
                self._extractor_review_payload(
                    item.get("params_used"),
                    action_ids=item.get("action_ids"),
                )
            )
            out.append(item)
        return out

    @staticmethod
    @lru_cache(maxsize=64)
    def _table_exists(table_name: str) -> bool:
        try:
            with db_conn() as conn:
                rows = fetchall(
                    conn,
                    "SELECT to_regclass(%s) IS NOT NULL AS ok",
                    (str(table_name),),
                ) or []
            return bool((rows[0] or {}).get("ok")) if rows else False
        except Exception:
            return False

    def _resolve_sector_context(
        self,
        *,
        sector_hint: Optional[str] = None,
        area_group_hint: Optional[str] = None,
        route_tokens: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        route_tokens = list(route_tokens or [])
        matched = match_sector_alias(
            (sector_hint or ""),
            route_tokens=route_tokens,
        )
        if matched is None and route_tokens:
            matched = match_sector_alias(" ".join(route_tokens), route_tokens=route_tokens)

        area_group = str(area_group_hint or "").strip().lower()
        if matched and not area_group:
            area_group = str(matched.area_group)
        if not area_group:
            area_group = "default"

        return {
            "area_group": area_group,
            "sector": (matched.sector if matched else (str(sector_hint).strip() or None)),
            "sector_type": (matched.sector_type if matched else None),
            "sector_priority": (int(matched.priority) if matched else None),
            "sector_alias_match": bool(matched is not None),
            "sector_bbox_suggestion": (dict(matched.bbox_suggestion) if matched and matched.bbox_suggestion else None),
        }

    def _collect_phase1_metrics(self, node_set_id: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "node_set_id": str(node_set_id),
            "raw_count": None,
            "candidate_count": None,
            "stop_count": None,
            "poi_count": None,
            "stop_signal_count": None,
            "poi_signal_count": None,
            "stop_ratio": None,
            "poi_ratio": None,
            "name_coverage": None,
            "tag_coverage_public_transport": None,
            "tag_coverage_highway_bus_stop": None,
            "tag_coverage_amenity_bus_station": None,
            "tag_coverage_platform": None,
            "tag_coverage": None,
            "cluster_count": None,
            "n_clusters": None,
            "singleton_count": None,
            "singletons": None,
            "noise_count": None,
            "resolved_count": None,
            "approved_count": None,
            "ambiguity_proxy_count": None,
            "unmatched_proxy_count": None,
            "spatial_spread_indicator": None,
            "spatial_center_lat": None,
            "spatial_center_lon": None,
            "spatial_avg_center_dist_m": None,
            "spatial_max_center_dist_m": None,
            "extraction_action": None,
            "params_snapshot": None,
        }
        try:
            with db_conn() as conn:
                meta_rows = fetchall(
                    conn,
                    """
                    SELECT
                      node_set_id::text AS node_set_id,
                      source_run_ids,
                      action_ids,
                      params_used
                    FROM node_work.node_candidate_sets
                    WHERE node_set_id = %s::uuid
                    LIMIT 1
                    """,
                    (str(node_set_id),),
                ) or []
                meta = dict(meta_rows[0]) if meta_rows else {}
                run_ids = self._as_uuid_list(meta.get("source_run_ids"))
                action_ids = self._as_uuid_list(meta.get("action_ids"))
                out["extraction_action"] = (action_ids[0] if action_ids else None)
                out["params_snapshot"] = meta.get("params_used")

                if run_ids:
                    raw_rows = fetchall(
                        conn,
                        """
                        SELECT COUNT(*)::int AS n
                        FROM node_raw.overpass_elements
                        WHERE run_id = ANY(%s::uuid[])
                        """,
                        (run_ids,),
                    ) or [{"n": 0}]
                    out["raw_count"] = int((raw_rows[0] or {}).get("n") or 0)

                cand_rows = fetchall(
                    conn,
                    """
                    SELECT
                      COUNT(*)::int AS candidate_count,
                      COUNT(*) FILTER (
                        WHERE COALESCE(tag_kind,'') IN ('bus_stop','platform','stop_position','station','tram_stop')
                      )::int AS stop_signal_count,
                      COUNT(*) FILTER (
                        WHERE COALESCE(tag_kind,'') NOT IN ('bus_stop','platform','stop_position','station','tram_stop')
                      )::int AS poi_signal_count
                    FROM node_work.node_candidates
                    WHERE node_set_id = %s::uuid
                    """,
                    (str(node_set_id),),
                ) or [{}]
                c0 = dict(cand_rows[0] or {})
                out.update(
                    {
                        "candidate_count": c0.get("candidate_count"),
                        "stop_signal_count": c0.get("stop_signal_count"),
                        "poi_signal_count": c0.get("poi_signal_count"),
                    }
                )
                out["stop_count"] = c0.get("stop_signal_count")
                out["poi_count"] = c0.get("poi_signal_count")

                coverage_rows = fetchall(
                    conn,
                    """
                    SELECT
                      COUNT(*)::int AS candidate_count,
                      COUNT(*) FILTER (
                        WHERE NULLIF(BTRIM(COALESCE(tags->>'name', '')), '') IS NOT NULL
                      )::int AS with_name_count,
                      COUNT(*) FILTER (
                        WHERE tags ? 'public_transport'
                      )::int AS with_public_transport_tag_count,
                      COUNT(*) FILTER (
                        WHERE LOWER(COALESCE(tags->>'highway', '')) = 'bus_stop'
                      )::int AS with_highway_bus_stop_count,
                      COUNT(*) FILTER (
                        WHERE LOWER(COALESCE(tags->>'amenity', '')) = 'bus_station'
                      )::int AS with_amenity_bus_station_count,
                      COUNT(*) FILTER (
                        WHERE LOWER(COALESCE(tags->>'public_transport', '')) = 'platform'
                           OR LOWER(COALESCE(tag_kind, '')) = 'platform'
                      )::int AS with_platform_count
                    FROM node_work.node_candidates
                    WHERE node_set_id = %s::uuid
                    """,
                    (str(node_set_id),),
                ) or [{}]
                c1 = dict(coverage_rows[0] or {})
                cand_n = float(c1.get("candidate_count") or 0.0)
                out["name_coverage"] = self._safe_ratio(c1.get("with_name_count"), cand_n)
                out["tag_coverage_public_transport"] = self._safe_ratio(c1.get("with_public_transport_tag_count"), cand_n)
                out["tag_coverage_highway_bus_stop"] = self._safe_ratio(c1.get("with_highway_bus_stop_count"), cand_n)
                out["tag_coverage_amenity_bus_station"] = self._safe_ratio(c1.get("with_amenity_bus_station_count"), cand_n)
                out["tag_coverage_platform"] = self._safe_ratio(c1.get("with_platform_count"), cand_n)
                tag_vals = [
                    out.get("tag_coverage_public_transport"),
                    out.get("tag_coverage_highway_bus_stop"),
                    out.get("tag_coverage_amenity_bus_station"),
                    out.get("tag_coverage_platform"),
                ]
                out["tag_coverage"] = sum(float(v or 0.0) for v in tag_vals) / float(max(1, len(tag_vals)))

                cl_rows = fetchall(
                    conn,
                    """
                    SELECT COUNT(DISTINCT cluster_id)::int AS cluster_count
                    FROM node_work.node_clusters
                    WHERE node_set_id = %s::uuid
                    """,
                    (str(node_set_id),),
                ) or [{"cluster_count": 0}]
                out["cluster_count"] = int((cl_rows[0] or {}).get("cluster_count") or 0)
                out["n_clusters"] = out["cluster_count"]

                singleton_rows = fetchall(
                    conn,
                    """
                    SELECT COUNT(*)::int AS singleton_count
                    FROM (
                      SELECT cluster_id
                      FROM node_work.node_clusters
                      WHERE node_set_id = %s::uuid
                      GROUP BY cluster_id
                      HAVING COUNT(*) = 1
                    ) q
                    """,
                    (str(node_set_id),),
                ) or [{"singleton_count": 0}]
                out["singleton_count"] = int((singleton_rows[0] or {}).get("singleton_count") or 0)
                out["singletons"] = out["singleton_count"]

                noise_rows = fetchall(
                    conn,
                    """
                    SELECT COUNT(*)::int AS noise_count
                    FROM node_work.node_candidates c
                    LEFT JOIN node_work.node_clusters cl
                      ON cl.node_set_id = c.node_set_id
                     AND cl.node_candidate_id = c.node_candidate_id
                    WHERE c.node_set_id = %s::uuid
                      AND cl.node_candidate_id IS NULL
                    """,
                    (str(node_set_id),),
                ) or [{"noise_count": 0}]
                out["noise_count"] = int((noise_rows[0] or {}).get("noise_count") or 0)

                res_rows = fetchall(
                    conn,
                    """
                    SELECT
                      COUNT(*)::int AS resolved_count,
                      COUNT(*) FILTER (WHERE status='approved')::int AS approved_count,
                      COUNT(*) FILTER (WHERE status='work')::int AS ambiguity_proxy_count,
                      COUNT(*) FILTER (WHERE status='rejected')::int AS unmatched_proxy_count
                    FROM node_work.nodes_resolved
                    WHERE node_set_id = %s::uuid
                    """,
                    (str(node_set_id),),
                ) or [{}]
                out.update(
                    {
                        k: (res_rows[0] or {}).get(k)
                        for k in ("resolved_count", "approved_count", "ambiguity_proxy_count", "unmatched_proxy_count")
                    }
                )

                spread_rows = fetchall(
                    conn,
                    """
                    SELECT
                      ST_Y(geom)::double precision AS lat,
                      ST_X(geom)::double precision AS lon
                    FROM node_work.node_candidates
                    WHERE node_set_id = %s::uuid
                      AND geom IS NOT NULL
                    """,
                    (str(node_set_id),),
                ) or []
                if spread_rows:
                    points = [
                        (float(r.get("lat")), float(r.get("lon")))
                        for r in spread_rows
                        if r.get("lat") is not None and r.get("lon") is not None
                    ]
                    if points:
                        lats = [p[0] for p in points]
                        lons = [p[1] for p in points]
                        c_lat = sum(lats) / len(lats)
                        c_lon = sum(lons) / len(lons)
                        dists = [haversine_m(c_lat, c_lon, la, lo) for la, lo in points]
                        avg_d = (sum(dists) / len(dists)) if dists else 0.0
                        max_d = max(dists) if dists else 0.0
                        min_lat = min(lats)
                        max_lat = max(lats)
                        min_lon = min(lons)
                        max_lon = max(lons)
                        diag_m = haversine_m(min_lat, min_lon, max_lat, max_lon)
                        denom = max(1.0, diag_m / 2.0)
                        out["spatial_center_lat"] = c_lat
                        out["spatial_center_lon"] = c_lon
                        out["spatial_avg_center_dist_m"] = avg_d
                        out["spatial_max_center_dist_m"] = max_d
                        out["spatial_spread_indicator"] = max(0.0, min(1.0, avg_d / denom))

                out["stop_ratio"] = self._safe_ratio(out.get("stop_count"), out.get("candidate_count"))
                out["poi_ratio"] = self._safe_ratio(out.get("poi_count"), out.get("candidate_count"))
        except Exception as e:
            out.setdefault("warnings", []).append(f"phase1_metrics_snapshot_failed:{e}")
        return out

    def _safe_ai_log_phase1(
        self,
        *,
        stage: str,
        node_set_id: Optional[str] = None,
        run_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        warnings: Optional[List[str]] = None,
        notes: Optional[List[str]] = None,
    ) -> None:
        if not callable(_ai_log_phase1_run):
            return
        try:
            row: Dict[str, Any] = {
                "stage": str(stage),
                "node_set_id": (str(node_set_id) if node_set_id else None),
                "run_id": (str(run_id) if run_id else None),
                "warnings": list(warnings or []),
                "notes": list(notes or []),
            }
            if node_set_id:
                row.update(self._collect_phase1_metrics(str(node_set_id)))
            if payload:
                row.update(dict(payload))
            _ai_log_phase1_run(row)
        except Exception:
            # Logging must never break pipeline behavior.
            pass

    def run_step_build_node_set(
        self,
        *,
        actions_path: str,
        candidate_actions: List[str],
        bbox: Dict[str, float],
        n_runs: int = 1,
        extra_params: Optional[Dict[str, Any]] = None,
        bandit_key: Optional[str] = None,
        review_context: Optional[Dict[str, Any]] = None,
        materialize_candidates: bool = False,
    ) -> Dict[str, Any]:
        actions_path = self._resolve_actions_path(actions_path)
        bbox_str = self._bbox_to_string(bbox)
        base_params: Dict[str, Any] = {"bbox": bbox_str}
        if extra_params:
            base_params.update({k: v for k, v in extra_params.items() if v is not None})
        out = build_node_set(
            actions_path=actions_path,
            candidate_actions=candidate_actions,
            base_params=base_params,
            n_runs=n_runs,
            bandit_key=(str(bandit_key) if bandit_key else "phase1_nodes_default"),
        )
        node_set_id = str(out.get("node_set_id") or "")
        params_snapshot = dict(out.get("params") or base_params)

        if node_set_id and review_context:
            extractor_review = dict(review_context or {})
            extractor_review.setdefault("bbox", dict(bbox))
            extractor_review.setdefault("bbox_str", bbox_str)
            extractor_review.setdefault("candidate_actions", list(candidate_actions or []))
            extractor_review.setdefault("action_id", ((out.get("action_ids") or [None])[0]))
            params_snapshot = {
                **params_snapshot,
                "extractor_review": extractor_review,
            }
            with db_conn() as conn:
                update_node_set_params_used(conn, node_set_id, params_snapshot)
            out["params"] = params_snapshot

        if node_set_id and materialize_candidates:
            norm_out = normalize(node_set_id, include_other=True)
            out["normalize"] = dict(norm_out or {})
            extractor_review = dict(params_snapshot.get("extractor_review") or {})
            extractor_review.update(
                {
                    "status": "candidate_materialized",
                    "candidate_count": norm_out.get("candidates_total"),
                    "stop_like_count": norm_out.get("stop_like_count"),
                    "poi_like_count": norm_out.get("poi_like_count"),
                }
            )
            params_snapshot = {
                **params_snapshot,
                "extractor_review": extractor_review,
            }
            with db_conn() as conn:
                update_node_set_params_used(conn, node_set_id, params_snapshot)
            out["params"] = params_snapshot

        self._safe_ai_log_phase1(
            stage="step_build_node_set",
            node_set_id=node_set_id,
            run_id=((out.get("run_ids") or [None])[0]),
            payload={
                "bbox": bbox,
                "candidate_actions": list(candidate_actions or []),
                "actions_path": actions_path,
                "params_snapshot": params_snapshot,
                "extraction_action": ((out.get("action_ids") or [None])[0]),
                "run_ids": list(out.get("run_ids") or []),
                "quality_scope": "extraction_only",
                "quality_score_ready": False,
                "materialize_candidates": bool(materialize_candidates),
                "normalized_candidate_count": (
                    dict(out.get("normalize") or {}).get("candidates_total")
                    if materialize_candidates
                    else None
                ),
            },
        )
        return out

    def get_sector_catalog(self) -> List[Dict[str, Any]]:
        return sector_catalog_as_rows()

    def _available_action_ids(self, actions_path: str) -> List[str]:
        try:
            data = json.loads(Path(actions_path).read_text(encoding="utf-8"))
        except Exception:
            return []
        actions_raw = list(data.get("actions") or []) if isinstance(data, dict) else []
        out: List[str] = []
        for item in actions_raw:
            action_id = str(item.get("id") or "").strip()
            if action_id:
                out.append(action_id)
        return out

    def _run_tuning_attempt_pipeline(
        self,
        *,
        node_set_id: str,
        eps_m: float,
        min_pts: int,
    ) -> Dict[str, Any]:
        out_norm = normalize(node_set_id)
        out_feat = extract_features(node_set_id)
        out_cluster = cluster(node_set_id, eps_m=float(eps_m), min_pts=int(min_pts))
        out_resolve = resolve(node_set_id)
        out_rank = rank(node_set_id)
        return {
            "normalize": out_norm,
            "features": out_feat,
            "cluster": out_cluster,
            "resolve": out_resolve,
            "rank": out_rank,
        }

    def run_step_build_node_set_extract_only(
        self,
        *,
        actions_path: str,
        bbox: Dict[str, float],
        candidate_actions: Optional[List[str]] = None,
        sector_hint: Optional[str] = None,
        area_group_hint: Optional[str] = None,
        route_tokens: Optional[List[str]] = None,
        extra_params: Optional[Dict[str, Any]] = None,
        review_context: Optional[Dict[str, Any]] = None,
        max_actions: int = 4,
        max_bbox_retries: int = 4,
        bandit_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        actions_path = self._resolve_actions_path(actions_path)
        available_actions = self._available_action_ids(actions_path)
        sector_ctx = self._resolve_sector_context(
            sector_hint=sector_hint,
            area_group_hint=area_group_hint,
            route_tokens=route_tokens,
        )
        area_group = str(sector_ctx.get("area_group") or "default")
        sector_name = str(sector_ctx.get("sector") or "unspecified")
        sector_key = normalize_area_text(sector_name) or normalize_area_text(area_group) or "default"

        action_order = ordered_actions_for_area(
            area_group,
            available_actions=available_actions,
            requested_actions=(candidate_actions or []),
        )
        sector_rec = sector_recommendation_for_context(
            sector_key=sector_key,
            area_group=area_group,
        )
        recommended_action = str(sector_rec.get("best_action") or "").strip()
        action_order = prioritize_actions_with_recommendation(
            action_order,
            preferred_action=recommended_action,
        )
        recommendation_applied = bool(recommended_action and action_order and action_order[0] == recommended_action)
        action_order = action_order[: max(1, min(int(max_actions), 5))]
        retry_plan = bbox_retry_plan(
            bbox,
            max_retries=max(1, int(max_bbox_retries)),
        )
        threshold = quality_threshold_for_area(area_group)

        def _to_float(v: Any) -> Optional[float]:
            try:
                return float(v)
            except Exception:
                return None

        attempts: List[Dict[str, Any]] = []
        best_attempt: Optional[Dict[str, Any]] = None
        grouped_scores: Dict[str, List[float]] = {}
        previous_attempt_by_action: Dict[str, Dict[str, Any]] = {}
        base_bandit_key = str(bandit_key or f"phase1_extract_only_{normalize_area_text(sector_name) or area_group}")

        for action_idx, action_id in enumerate(action_order, start=1):
            grouped_scores.setdefault(action_id, [])
            for retry in retry_plan:
                bbox_use = dict(retry.get("bbox") or bbox)
                buffer_ratio = float(retry.get("bbox_buffer_ratio") or 0.0)
                retry_index = int(retry.get("attempt_index") or 1)
                previous_attempt = dict(previous_attempt_by_action.get(action_id) or {})
                retry_reason = "initial_attempt"
                retry_delta: Dict[str, Any] = {
                    "bbox_buffer_ratio_from": None,
                    "bbox_buffer_ratio_to": buffer_ratio,
                }
                if previous_attempt:
                    retry_reason = "quality_below_threshold"
                    prev_buffer = _to_float(previous_attempt.get("bbox_buffer_ratio"))
                    if prev_buffer is not None:
                        retry_delta["bbox_buffer_ratio_from"] = prev_buffer
                    prev_metrics = dict(previous_attempt.get("metrics") or {})
                    prev_raw = _to_float(prev_metrics.get("raw_count"))
                    prev_candidates = _to_float(prev_metrics.get("candidate_count"))
                    if prev_raw is not None and prev_raw > 0 and (prev_candidates is None or prev_candidates <= 0):
                        retry_reason = "candidate_yield_zero_after_extraction"

                attempt_review_context = dict(review_context or {})
                attempt_review_context.update(
                    {
                        "bbox": dict(bbox_use),
                        "bbox_str": self._bbox_to_string(bbox_use),
                        "action_id": action_id,
                        "target_group": attempt_review_context.get("target_group") or area_group,
                        "sector_hint": sector_name,
                    }
                )

                out_build = self.run_step_build_node_set(
                    actions_path=actions_path,
                    candidate_actions=[action_id],
                    bbox=bbox_use,
                    n_runs=1,
                    extra_params=(dict(extra_params or {})),
                    bandit_key=f"{base_bandit_key}|{action_id}",
                    review_context=attempt_review_context,
                    materialize_candidates=True,
                )
                node_set_id = str(out_build.get("node_set_id") or "")
                run_id = ((out_build.get("run_ids") or [None])[0])
                if not node_set_id:
                    continue

                metrics = self._collect_phase1_metrics(node_set_id)
                score_out = score_extraction_quality(
                    {
                        **metrics,
                        "area_group": area_group,
                        "stage": "step_build_node_set",
                        "quality_scope": "extraction_only",
                    },
                    area_group=area_group,
                )
                score = score_out.get("score")
                score_proxy = score if score is not None else score_out.get("score_provisional")
                if score_proxy is not None:
                    grouped_scores[action_id].append(float(score_proxy))

                params_snapshot = dict(out_build.get("params") or {})
                extractor_review = dict(params_snapshot.get("extractor_review") or {})
                extractor_review.update(
                    {
                        "quality_score": score_proxy,
                        "quality_score_ready": bool(score_out.get("score_ready")),
                        "quality_score_provisional": score_out.get("score_provisional"),
                        "quality_threshold": threshold,
                        "status": (
                            "candidate_materialized"
                            if int(metrics.get("candidate_count") or 0) > 0
                            else "extracted_empty"
                        ),
                    }
                )
                params_snapshot["extractor_review"] = extractor_review
                with db_conn() as conn:
                    update_node_set_params_used(conn, node_set_id, params_snapshot)

                attempt = {
                    "attempt_global_index": len(attempts) + 1,
                    "action_order_index": int(action_idx),
                    "action_id": action_id,
                    "bbox": bbox_use,
                    "bbox_buffer_ratio": buffer_ratio,
                    "bbox_buffer_pct": round(buffer_ratio * 100.0, 1),
                    "retry_strategy": "bbox_expand",
                    "retry_attempt_index": retry_index,
                    "retry_reason": retry_reason,
                    "retry_parameter_delta": retry_delta,
                    "node_set_id": node_set_id,
                    "run_id": run_id,
                    "area_group": area_group,
                    "sector": sector_name,
                    "sector_recommendation_key": sector_rec.get("recommendation_key"),
                    "sector_recommended_action": (recommended_action or None),
                    "sector_recommendation_applied": recommendation_applied,
                    "quality_score": score_proxy,
                    "quality_score_ready": bool(score_out.get("score_ready")),
                    "quality_score_provisional": score_out.get("score_provisional"),
                    "quality_threshold": threshold,
                    "quality_breakdown": list(score_out.get("breakdown") or []),
                    "quality_components": dict(score_out.get("components_raw") or {}),
                    "quality_warnings": list(score_out.get("warnings") or []),
                    "metrics": metrics,
                    "pipeline_steps": {
                        "normalize": dict(out_build.get("normalize") or {}),
                    },
                }
                attempts.append(attempt)

                self._safe_ai_log_phase1(
                    stage="step_build_node_set_extract_only_attempt",
                    node_set_id=node_set_id,
                    run_id=(str(run_id) if run_id else None),
                    payload={
                        **sector_ctx,
                        "tuning_mode": "extractor_only_v1",
                        "attempt_index": attempt["attempt_global_index"],
                        "action_order_index": int(action_idx),
                        "bbox_buffer_ratio": buffer_ratio,
                        "bbox_buffer_pct": attempt["bbox_buffer_pct"],
                        "retry_strategy": attempt["retry_strategy"],
                        "retry_attempt_index": attempt["retry_attempt_index"],
                        "retry_reason": attempt["retry_reason"],
                        "retry_parameter_delta": dict(attempt["retry_parameter_delta"] or {}),
                        "bbox": bbox_use,
                        "quality_threshold": threshold,
                        "quality_score_proxy": score_proxy,
                        "quality_breakdown_proxy": attempt["quality_breakdown"],
                        "candidate_actions_ordered": list(action_order),
                        "sector_recommendation_key": sector_rec.get("recommendation_key"),
                        "sector_recommended_action": (recommended_action or None),
                        "sector_recommendation_applied": recommendation_applied,
                        "extraction_action": action_id,
                        "params_snapshot": params_snapshot,
                    },
                    warnings=list(score_out.get("warnings") or []),
                )
                previous_attempt_by_action[action_id] = dict(attempt)

                if best_attempt is None:
                    best_attempt = attempt
                else:
                    prev_score = _to_float(best_attempt.get("quality_score"))
                    cur_score = _to_float(attempt.get("quality_score"))
                    if cur_score is not None and (prev_score is None or cur_score > prev_score):
                        best_attempt = attempt
                    elif cur_score is not None and prev_score is not None and abs(cur_score - prev_score) < 1e-9:
                        prev_candidates = self._safe_int(dict(best_attempt.get("metrics") or {}).get("candidate_count"))
                        cur_candidates = self._safe_int(metrics.get("candidate_count"))
                        if cur_candidates > prev_candidates:
                            best_attempt = attempt

                if not is_low_quality(score_proxy, area_group=area_group):
                    break

        if best_attempt is None:
            return {
                "ok": False,
                "reason": "no_attempts_executed",
                "area_group": area_group,
                "sector": sector_name,
                "candidate_actions": action_order,
                "sector_recommendation": {
                    "recommendation_key": sector_rec.get("recommendation_key"),
                    "recommended_action": (recommended_action or None),
                    "applied": recommendation_applied,
                },
            }

        best_score = best_attempt.get("quality_score")
        best_action = str(best_attempt.get("action_id") or "")
        best_buffer = float(best_attempt.get("bbox_buffer_ratio") or 0.0)
        low_quality = is_low_quality(best_score, area_group=area_group)

        avg_by_action: List[Dict[str, Any]] = []
        for action_id, values in grouped_scores.items():
            if not values:
                continue
            avg_by_action.append(
                {
                    "action_id": action_id,
                    "avg_score": round(sum(values) / len(values), 3),
                    "runs": len(values),
                    "max_score": round(max(values), 3),
                }
            )
        avg_by_action.sort(key=lambda x: (x["avg_score"], x["max_score"]), reverse=True)

        next_action = next((a for a in action_order if a != best_action), None)
        if low_quality and next_action:
            next_suggestion = f"Try fallback action '{next_action}' with bbox buffer +25%."
        elif low_quality:
            next_suggestion = "Increase bbox expansion or switch to a narrower geographic seed."
        else:
            next_suggestion = (
                f"Use '{best_action}' with bbox buffer +{round(best_buffer * 100.0, 1)}% "
                "as the extractor-only baseline."
            )

        self._safe_ai_log_phase1(
            stage="step_build_node_set_extract_only_recommendation",
            node_set_id=str(best_attempt.get("node_set_id") or ""),
            run_id=(str(best_attempt.get("run_id")) if best_attempt.get("run_id") else None),
            payload={
                **sector_ctx,
                "tuning_mode": "extractor_only_v1",
                "best_action": best_action,
                "best_bbox_buffer_ratio": best_buffer,
                "best_bbox_buffer_pct": round(best_buffer * 100.0, 1),
                "best_score_proxy": best_score,
                "quality_threshold": threshold,
                "recommended_next_attempt": next_suggestion,
                "attempt_count": len(attempts),
                "attempts_summary": [
                    {
                        "attempt": int(a.get("attempt_global_index") or 0),
                        "action_id": a.get("action_id"),
                        "bbox_buffer_pct": a.get("bbox_buffer_pct"),
                        "score": a.get("quality_score"),
                        "node_set_id": a.get("node_set_id"),
                    }
                    for a in attempts
                ],
            },
        )

        return {
            "ok": True,
            "area_group": area_group,
            "sector": sector_name,
            "quality_threshold": threshold,
            "best": best_attempt,
            "attempts": attempts,
            "recommended_next_attempt": next_suggestion,
            "action_score_summary": avg_by_action,
            "sector_recommendation": {
                "recommendation_key": sector_rec.get("recommendation_key"),
                "recommended_action": (recommended_action or None),
                "applied": recommendation_applied,
            },
        }

    def run_step_build_node_set_tuned(
        self,
        *,
        actions_path: str,
        bbox: Dict[str, float],
        candidate_actions: Optional[List[str]] = None,
        sector_hint: Optional[str] = None,
        area_group_hint: Optional[str] = None,
        route_tokens: Optional[List[str]] = None,
        extra_params: Optional[Dict[str, Any]] = None,
        max_actions: int = 3,
        max_bbox_retries: int = 4,
        eps_m: float = 35.0,
        min_pts: int = 3,
        bandit_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        actions_path = self._resolve_actions_path(actions_path)
        available_actions = self._available_action_ids(actions_path)
        sector_ctx = self._resolve_sector_context(
            sector_hint=sector_hint,
            area_group_hint=area_group_hint,
            route_tokens=route_tokens,
        )
        area_group = str(sector_ctx.get("area_group") or "default")
        sector_name = str(sector_ctx.get("sector") or "unspecified")
        sector_key = normalize_area_text(sector_name) or normalize_area_text(area_group) or "default"

        action_order = ordered_actions_for_area(
            area_group,
            available_actions=available_actions,
            requested_actions=(candidate_actions or []),
        )
        sector_rec = sector_recommendation_for_context(
            sector_key=sector_key,
            area_group=area_group,
        )
        recommended_action = str(sector_rec.get("best_action") or "").strip()
        action_order = prioritize_actions_with_recommendation(
            action_order,
            preferred_action=recommended_action,
        )
        recommendation_applied = bool(recommended_action and action_order and action_order[0] == recommended_action)
        action_order = action_order[: max(1, min(int(max_actions), 4))]
        retry_plan = bbox_retry_plan(
            bbox,
            max_retries=max(1, int(max_bbox_retries)),
        )
        threshold = quality_threshold_for_area(area_group)

        def _to_float(v: Any) -> Optional[float]:
            try:
                return float(v)
            except Exception:
                return None

        attempts: List[Dict[str, Any]] = []
        best_attempt: Optional[Dict[str, Any]] = None
        grouped_scores: Dict[str, List[float]] = {}
        previous_attempt_by_action: Dict[str, Dict[str, Any]] = {}
        base_bandit_key = str(bandit_key or f"phase1_area_tuning_{normalize_area_text(sector_name) or area_group}")

        for action_idx, action_id in enumerate(action_order, start=1):
            grouped_scores.setdefault(action_id, [])
            for retry in retry_plan:
                bbox_use = dict(retry.get("bbox") or bbox)
                buffer_ratio = float(retry.get("bbox_buffer_ratio") or 0.0)
                retry_index = int(retry.get("attempt_index") or 1)
                previous_attempt = dict(previous_attempt_by_action.get(action_id) or {})
                retry_reason = "initial_attempt"
                retry_delta: Dict[str, Any] = {
                    "bbox_buffer_ratio_from": None,
                    "bbox_buffer_ratio_to": buffer_ratio,
                }
                if previous_attempt:
                    retry_reason = "quality_below_threshold"
                    prev_buffer = _to_float(previous_attempt.get("bbox_buffer_ratio"))
                    if prev_buffer is not None:
                        retry_delta["bbox_buffer_ratio_from"] = prev_buffer
                    prev_metrics = dict(previous_attempt.get("metrics") or {})
                    prev_raw = _to_float(prev_metrics.get("raw_count"))
                    prev_candidates = _to_float(prev_metrics.get("candidate_count"))
                    if prev_raw is not None and prev_raw > 0 and (prev_candidates is None or prev_candidates <= 0):
                        retry_reason = "candidate_yield_zero_after_extraction"
                    elif previous_attempt.get("quality_score") is None:
                        retry_reason = "score_unavailable"

                out_build = self.run_step_build_node_set(
                    actions_path=actions_path,
                    candidate_actions=[action_id],
                    bbox=bbox_use,
                    n_runs=1,
                    extra_params=(dict(extra_params or {})),
                    bandit_key=f"{base_bandit_key}|{action_id}",
                )
                node_set_id = str(out_build.get("node_set_id") or "")
                run_id = ((out_build.get("run_ids") or [None])[0])
                if not node_set_id:
                    continue

                step_out = self._run_tuning_attempt_pipeline(
                    node_set_id=node_set_id,
                    eps_m=float(eps_m),
                    min_pts=int(min_pts),
                )
                metrics = self._collect_phase1_metrics(node_set_id)
                score_out = score_extraction_quality(
                    {**metrics, "area_group": area_group},
                    area_group=area_group,
                )
                score = score_out.get("score")
                if score is not None:
                    grouped_scores[action_id].append(float(score))

                attempt = {
                    "attempt_global_index": len(attempts) + 1,
                    "action_order_index": int(action_idx),
                    "action_id": action_id,
                    "bbox": bbox_use,
                    "bbox_buffer_ratio": buffer_ratio,
                    "bbox_buffer_pct": round(buffer_ratio * 100.0, 1),
                    "retry_strategy": "bbox_expand",
                    "retry_attempt_index": retry_index,
                    "retry_reason": retry_reason,
                    "retry_parameter_delta": retry_delta,
                    "node_set_id": node_set_id,
                    "run_id": run_id,
                    "area_group": area_group,
                    "sector": sector_name,
                    "sector_recommendation_key": sector_rec.get("recommendation_key"),
                    "sector_recommended_action": (recommended_action or None),
                    "sector_recommendation_applied": recommendation_applied,
                    "quality_score": score,
                    "quality_threshold": threshold,
                    "quality_breakdown": list(score_out.get("breakdown") or []),
                    "quality_components": dict(score_out.get("components_raw") or {}),
                    "quality_warnings": list(score_out.get("warnings") or []),
                    "metrics": metrics,
                    "pipeline_steps": step_out,
                }
                attempts.append(attempt)

                self._safe_ai_log_phase1(
                    stage="step_build_node_set_tuning_attempt",
                    node_set_id=node_set_id,
                    run_id=(str(run_id) if run_id else None),
                    payload={
                        **sector_ctx,
                        "tuning_mode": "area_targeting_v1",
                        "attempt_index": attempt["attempt_global_index"],
                        "action_order_index": int(action_idx),
                        "bbox_buffer_ratio": buffer_ratio,
                        "bbox_buffer_pct": attempt["bbox_buffer_pct"],
                        "retry_strategy": attempt["retry_strategy"],
                        "retry_attempt_index": attempt["retry_attempt_index"],
                        "retry_reason": attempt["retry_reason"],
                        "retry_parameter_delta": dict(attempt["retry_parameter_delta"] or {}),
                        "bbox": bbox_use,
                        "quality_threshold": threshold,
                        "quality_score_proxy": score,
                        "quality_breakdown_proxy": attempt["quality_breakdown"],
                        "candidate_actions_ordered": list(action_order),
                        "sector_recommendation_key": sector_rec.get("recommendation_key"),
                        "sector_recommended_action": (recommended_action or None),
                        "sector_recommendation_applied": recommendation_applied,
                        "extraction_action": action_id,
                        "params_snapshot": {
                            "bbox": self._bbox_to_string(bbox_use),
                            **dict(extra_params or {}),
                        },
                    },
                    warnings=list(score_out.get("warnings") or []),
                )
                previous_attempt_by_action[action_id] = dict(attempt)

                if best_attempt is None:
                    best_attempt = attempt
                else:
                    prev = best_attempt.get("quality_score")
                    cur = attempt.get("quality_score")
                    if cur is not None and (prev is None or float(cur) > float(prev)):
                        best_attempt = attempt

                # If this action already reached quality target, stop expanding bbox for this action.
                if not is_low_quality(score, area_group=area_group):
                    break

        if best_attempt is None:
            return {
                "ok": False,
                "reason": "no_attempts_executed",
                "area_group": area_group,
                "sector": sector_name,
                "candidate_actions": action_order,
                "sector_recommendation": {
                    "recommendation_key": sector_rec.get("recommendation_key"),
                    "recommended_action": (recommended_action or None),
                    "applied": recommendation_applied,
                },
            }

        best_score = best_attempt.get("quality_score")
        best_action = str(best_attempt.get("action_id") or "")
        best_buffer = float(best_attempt.get("bbox_buffer_ratio") or 0.0)
        low_quality = is_low_quality(best_score, area_group=area_group)

        avg_by_action: List[Dict[str, Any]] = []
        for action_id, values in grouped_scores.items():
            if not values:
                continue
            avg_by_action.append(
                {
                    "action_id": action_id,
                    "avg_score": round(sum(values) / len(values), 3),
                    "runs": len(values),
                    "max_score": round(max(values), 3),
                }
            )
        avg_by_action.sort(key=lambda x: (x["avg_score"], x["max_score"]), reverse=True)

        next_action = None
        for action_id in action_order:
            if action_id == best_action:
                continue
            next_action = action_id
            break
        if low_quality and next_action:
            next_suggestion = f"Try fallback action '{next_action}' with bbox buffer +25%."
        elif low_quality:
            next_suggestion = "Increase bbox expansion and review filters (name/operator/ref regex)."
        else:
            next_suggestion = f"Use '{best_action}' with bbox buffer +{round(best_buffer * 100.0, 1)}% as next extraction baseline."

        recommendation_payload = {
            "area_group": area_group,
            "sector": sector_name,
            "best_action": best_action,
            "best_bbox_buffer_ratio": best_buffer,
            "best_bbox_buffer_pct": round(best_buffer * 100.0, 1),
            "best_node_set_id": best_attempt.get("node_set_id"),
            "best_run_id": best_attempt.get("run_id"),
            "best_score": best_score,
            "quality_threshold": threshold,
            "score_trend": [
                {"attempt": int(a.get("attempt_global_index") or 0), "score": a.get("quality_score")}
                for a in attempts
            ],
            "action_score_summary": avg_by_action,
            "recommended_next_attempt": next_suggestion,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "prior_recommendation_key": sector_rec.get("recommendation_key"),
            "prior_recommended_action": (recommended_action or None),
            "prior_recommendation_applied": recommendation_applied,
        }

        save_sector_recommendation(
            sector_key=(normalize_area_text(sector_name) or area_group),
            sector_name=sector_name,
            area_group=area_group,
            recommendation=recommendation_payload,
        )

        self._safe_ai_log_phase1(
            stage="step_build_node_set_tuning_recommendation",
            node_set_id=str(best_attempt.get("node_set_id") or ""),
            run_id=(str(best_attempt.get("run_id")) if best_attempt.get("run_id") else None),
            payload={
                **sector_ctx,
                "tuning_mode": "area_targeting_v1",
                "best_action": best_action,
                "best_bbox_buffer_ratio": best_buffer,
                "best_bbox_buffer_pct": round(best_buffer * 100.0, 1),
                "best_score_proxy": best_score,
                "quality_threshold": threshold,
                "recommended_next_attempt": next_suggestion,
                "prior_recommendation_key": sector_rec.get("recommendation_key"),
                "prior_recommended_action": (recommended_action or None),
                "prior_recommendation_applied": recommendation_applied,
                "attempt_count": len(attempts),
                "attempts_summary": [
                    {
                        "attempt": int(a.get("attempt_global_index") or 0),
                        "action_id": a.get("action_id"),
                        "bbox_buffer_pct": a.get("bbox_buffer_pct"),
                        "score": a.get("quality_score"),
                        "node_set_id": a.get("node_set_id"),
                    }
                    for a in attempts
                ],
            },
        )

        return {
            "ok": True,
            "area_group": area_group,
            "sector": sector_name,
            "quality_threshold": threshold,
            "best": best_attempt,
            "attempts": attempts,
            "recommended_next_attempt": next_suggestion,
            "action_score_summary": avg_by_action,
            "sector_recommendation": {
                "recommendation_key": sector_rec.get("recommendation_key"),
                "recommended_action": (recommended_action or None),
                "applied": recommendation_applied,
            },
        }

    def run_action_comparison_batch(
        self,
        *,
        actions_path: str,
        bbox: Dict[str, float],
        candidate_actions: List[str],
        sector_hint: Optional[str] = None,
        area_group_hint: Optional[str] = None,
        route_tokens: Optional[List[str]] = None,
        extra_params: Optional[Dict[str, Any]] = None,
        max_actions: int = 4,
    ) -> Dict[str, Any]:
        return self.run_step_build_node_set_tuned(
            actions_path=actions_path,
            bbox=bbox,
            candidate_actions=candidate_actions,
            sector_hint=sector_hint,
            area_group_hint=area_group_hint,
            route_tokens=route_tokens,
            extra_params=extra_params,
            max_actions=max(2, min(int(max_actions), 4)),
            max_bbox_retries=4,
        )

    def preview_overpass_queries(
        self,
        *,
        actions_path: str,
        candidate_actions: List[str],
        bbox: Dict[str, float],
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        actions_path = self._resolve_actions_path(actions_path)
        bbox_str = self._bbox_to_string(bbox)
        merged_params: Dict[str, Any] = {"bbox": bbox_str}
        if extra_params:
            merged_params.update({k: v for k, v in extra_params.items() if v is not None})

        previews: List[Dict[str, Any]] = []
        for action_id in candidate_actions:
            spec, params_used, query_text = build_query(actions_path, action_id, merged_params)
            previews.append(
                {
                    "action_id": action_id,
                    "template_path": str(spec.template_path),
                    "params_used": params_used,
                    "query_text": query_text,
                }
            )
        return previews

    def run_step_normalize(self, node_set_id: str) -> Dict[str, Any]:
        out = normalize(node_set_id)
        self._safe_ai_log_phase1(
            stage="step_normalize",
            node_set_id=str(node_set_id),
            run_id=((out.get("run_ids") or [None])[0]),
            payload=out,
        )
        return out

    def run_step_features(self, node_set_id: str) -> Dict[str, Any]:
        out = extract_features(node_set_id)
        self._safe_ai_log_phase1(
            stage="step_features",
            node_set_id=str(node_set_id),
            payload=out,
        )
        return out

    def run_step_cluster(self, node_set_id: str, *, eps_m: float = 35.0, min_pts: int = 3) -> Dict[str, Any]:
        out = cluster(node_set_id, eps_m=eps_m, min_pts=min_pts)
        self._safe_ai_log_phase1(
            stage="step_cluster",
            node_set_id=str(node_set_id),
            payload={
                **dict(out or {}),
                "cluster_eps_m": float(eps_m),
                "cluster_min_pts": int(min_pts),
            },
        )
        return out

    def run_step_resolve(self, node_set_id: str) -> Dict[str, Any]:
        out = resolve(node_set_id)
        self._safe_ai_log_phase1(
            stage="step_resolve",
            node_set_id=str(node_set_id),
            payload=out,
        )
        return out

    def run_step_rank(self, node_set_id: str) -> Dict[str, Any]:
        out = rank(node_set_id)
        self._safe_ai_log_phase1(
            stage="step_rank",
            node_set_id=str(node_set_id),
            payload=out,
        )
        return out

    def run_step_promote(self, node_set_id: str) -> Dict[str, Any]:
        out = promote(node_set_id)
        self._safe_ai_log_phase1(
            stage="step_promote",
            node_set_id=str(node_set_id),
            payload=out,
        )
        return out

    # -----------------------------
    # Map data + point actions
    # -----------------------------

    def list_resolved_nodes(self, node_set_id: str, *, limit: int = 4000) -> List[dict]:
        sql = """
        SELECT
          node_id,
          node_set_id,
          ST_Y(geom)::double precision AS lat,
          ST_X(geom)::double precision AS lon,
          chosen_candidate_id,
          chosen_tags,
          confidence,
          status,
          resolved_at
        FROM node_work.nodes_resolved
        WHERE node_set_id = %s
        ORDER BY resolved_at ASC
        LIMIT %s
        """
        with db_conn() as conn:
            return fetchall(conn, sql, (str(node_set_id), int(limit))) or []

    def list_candidate_nodes(self, node_set_id: str, *, limit: int = 4000) -> List[dict]:
        sql = """
        SELECT
          c.node_candidate_id,
          c.node_set_id,
          ST_Y(c.geom)::double precision AS lat,
          ST_X(c.geom)::double precision AS lon,
          c.tags,
          c.tag_kind,
          COALESCE(f.prob_stop, f.confidence_v0) AS confidence_v0
        FROM node_work.node_candidates c
        LEFT JOIN node_work.node_features f
          ON f.node_candidate_id = c.node_candidate_id
        WHERE c.node_set_id = %s
        ORDER BY c.created_at ASC
        LIMIT %s
        """
        with db_conn() as conn:
            return fetchall(conn, sql, (str(node_set_id), int(limit))) or []

    def get_nodes_for_node_set(
        self,
        node_set_id: str,
        *,
        mode: str = "resolved",
        limit: int = 4000,
        offset: int = 0,
    ) -> List[dict]:
        mode_norm = str(mode or "resolved").strip().lower()
        lim = max(1, min(int(limit or 4000), 10000))
        off = max(0, int(offset or 0))
        if mode_norm == "candidates":
            sql = """
            SELECT
              c.node_candidate_id::text AS point_id,
              c.node_set_id::text AS node_set_id,
              ST_Y(c.geom)::double precision AS lat,
              ST_X(c.geom)::double precision AS lon,
              COALESCE(c.tags->>'name', c.tags->>'ref', '') AS name,
              c.tags AS tags,
              c.tag_kind AS tag_kind,
              COALESCE(f.prob_stop, f.confidence_v0) AS confidence,
              NULL::text AS approval_status,
              NULL::text AS cluster_id,
              c.created_at
            FROM node_work.node_candidates c
            LEFT JOIN node_work.node_features f
              ON f.node_candidate_id = c.node_candidate_id
            WHERE c.node_set_id = %s
            ORDER BY c.created_at ASC
            LIMIT %s OFFSET %s
            """
            with db_conn() as conn:
                rows = fetchall(conn, sql, (str(node_set_id), lim, off)) or []
            out: List[dict] = []
            for row in rows:
                tags = dict(row.get("tags") or {}) if isinstance(row.get("tags"), dict) else {}
                out.append(
                    {
                        "point_id": str(row.get("point_id") or ""),
                        "node_set_id": str(row.get("node_set_id") or node_set_id),
                        "mode": "candidates",
                        "lat": row.get("lat"),
                        "lon": row.get("lon"),
                        "name": str(row.get("name") or "").strip(),
                        "tags": tags,
                        "tags_summary": ", ".join(sorted(tags.keys())[:8]) if tags else "",
                        "tag_kind": row.get("tag_kind"),
                        "confidence": row.get("confidence"),
                        "approval_status": None,
                        "cluster_id": row.get("cluster_id"),
                        "created_at": row.get("created_at"),
                    }
                )
            return out

        sql = """
        SELECT
          r.node_id::text AS point_id,
          r.node_set_id::text AS node_set_id,
          ST_Y(r.geom)::double precision AS lat,
          ST_X(r.geom)::double precision AS lon,
          COALESCE(r.chosen_tags->>'name', r.chosen_tags->>'ref', '') AS name,
          r.chosen_tags AS tags,
          NULL::text AS tag_kind,
          r.confidence AS confidence,
          r.status AS approval_status,
          NULL::text AS cluster_id,
          r.resolved_at AS created_at
        FROM node_work.nodes_resolved r
        WHERE r.node_set_id = %s
        ORDER BY r.resolved_at ASC
        LIMIT %s OFFSET %s
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (str(node_set_id), lim, off)) or []
        out = []
        for row in rows:
            tags = dict(row.get("tags") or {}) if isinstance(row.get("tags"), dict) else {}
            out.append(
                {
                    "point_id": str(row.get("point_id") or ""),
                    "node_set_id": str(row.get("node_set_id") or node_set_id),
                    "mode": "resolved",
                    "lat": row.get("lat"),
                    "lon": row.get("lon"),
                    "name": str(row.get("name") or "").strip(),
                    "tags": tags,
                    "tags_summary": ", ".join(sorted(tags.keys())[:8]) if tags else "",
                    "tag_kind": row.get("tag_kind"),
                    "confidence": row.get("confidence"),
                    "approval_status": str(row.get("approval_status") or "").strip() or None,
                    "cluster_id": row.get("cluster_id"),
                    "created_at": row.get("created_at"),
                }
            )
        return out

    def update_resolved_node_location(self, node_id: str, *, lat: float, lon: float) -> Dict[str, Any]:
        sql = """
        UPDATE node_work.nodes_resolved
        SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)
        WHERE node_id = %s
        RETURNING
          node_id,
          ST_Y(geom)::double precision AS lat,
          ST_X(geom)::double precision AS lon
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (float(lon), float(lat), str(node_id))) or []
        return rows[0] if rows else {}

    def get_resolved_node(self, node_id: str) -> Optional[Dict[str, Any]]:
        sql = """
        SELECT
          node_id,
          node_set_id,
          ST_Y(geom)::double precision AS lat,
          ST_X(geom)::double precision AS lon,
          chosen_candidate_id,
          chosen_tags,
          confidence,
          status,
          resolved_at
        FROM node_work.nodes_resolved
        WHERE node_id = %s
        LIMIT 1
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (str(node_id),)) or []
        return rows[0] if rows else None

    def approve_resolved_node(self, node_id: str, *, node_type: str = "STOP") -> Dict[str, Any]:
        """Mark a node approved in node_work and promote it to node_prod
        via the universal stop-quality treater.

        Pre-migration: a single ``INSERT ... SELECT FROM node_work ON CONFLICT
        DO UPDATE`` did the move in SQL. Post: we fetch the resolved row and
        candidate's tag_kind, then call ``treat_stop(approve_promote)`` so
        the contextual-name cascade runs, the ``geo_prod.places`` mapping is
        kept consistent, and an audit row is written.
        """
        from phase3_routes.services.stop_quality import (
            StopTreatmentInput,
            treat_stop,
        )
        import psycopg2.extras as _ppx

        node_type = str(node_type or "STOP").upper()
        if node_type not in {"STOP", "POI"}:
            node_type = "STOP"
        nid = str(node_id)

        with db_conn() as conn:
            exec_sql(
                conn,
                """
                UPDATE node_work.nodes_resolved
                SET status = 'approved'
                WHERE node_id = %s
                """,
                (nid,),
            )
            row = fetchone(
                conn,
                """
                SELECT
                  ST_Y(CASE WHEN ST_SRID(r.geom)=0 THEN ST_SetSRID(r.geom, 4326) ELSE r.geom END)::float8 AS lat,
                  ST_X(CASE WHEN ST_SRID(r.geom)=0 THEN ST_SetSRID(r.geom, 4326) ELSE r.geom END)::float8 AS lon,
                  NULLIF(r.chosen_tags->>'name', '') AS name,
                  NULLIF(r.chosen_tags->>'ref', '') AS ref,
                  NULLIF(r.chosen_tags->>'operator', '') AS operator,
                  c.tag_kind                AS tag_kind,
                  r.node_set_id::text       AS source_node_set_id,
                  r.chosen_candidate_id::text AS chosen_candidate_id,
                  r.chosen_tags             AS chosen_tags,
                  r.confidence              AS confidence
                FROM node_work.nodes_resolved r
                JOIN node_work.node_candidates c
                  ON c.node_candidate_id = r.chosen_candidate_id
                WHERE r.node_id = %s
                """,
                (nid,),
            )
            if not row:
                raise RuntimeError(f"resolved node not found: {nid}")

            extras: Dict[str, Any] = {
                "source": "work_review",
                "tag_kind": row.get("tag_kind"),
                "source_node_set_id": row.get("source_node_set_id"),
                "chosen_candidate_id": row.get("chosen_candidate_id"),
                "chosen_tags": _ppx.Json(row.get("chosen_tags") or {}),
                "approved_at": datetime.now(timezone.utc),
            }
            if row.get("ref"):
                extras["ref"] = row["ref"]
            if row.get("operator"):
                extras["operator"] = row["operator"]

            res = treat_stop(
                StopTreatmentInput(
                    operation="approve_promote",
                    caller="phase1_client.approve_resolved",
                    node_id=nid,
                    proposed_name=row.get("name") or "",
                    proposed_lat=float(row["lat"]),
                    proposed_lon=float(row["lon"]),
                    node_type=node_type,
                    confidence=float(row.get("confidence") or 0.0),
                    extras=extras,
                ),
                conn,
            )
            if not res.success:
                raise RuntimeError(f"approve_resolved_node failed: {res.error}")
        return {"node_id": nid, "status": "approved", "node_type": node_type}

    def reject_resolved_node(self, node_id: str) -> Dict[str, Any]:
        with db_conn() as conn:
            exec_sql(
                conn,
                """
                UPDATE node_work.nodes_resolved
                SET status = 'rejected'
                WHERE node_id = %s
                """,
                (str(node_id),),
            )
            try:
                exec_sql(
                    conn,
                    "DELETE FROM node_prod.nodes WHERE node_id = %s",
                    (str(node_id),),
                )
            except Exception:
                pass
        return {"node_id": str(node_id), "status": "rejected"}

    # -----------------------------
    # New node requests (Phase 3 + free create)
    # -----------------------------

    def list_node_review_requests(
        self,
        *,
        source: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          request_id,
          source,
          route_id,
          seq,
          status,
          lat,
          lon,
          node_type,
          name,
          ref,
          operator,
          tags,
          requested_by,
          reviewed_by,
          reviewed_at,
          approved_node_id,
          created_at,
          notes
        FROM node_work.node_review_requests
        WHERE (%s IS NULL OR source = %s)
          AND (%s IS NULL OR status = %s)
        ORDER BY created_at DESC
        LIMIT %s
        """
        with db_conn() as conn:
            return fetchall(conn, sql, (source, source, status, status, int(limit))) or []

    def get_node_review_request_counts(
        self,
        *,
        source: Optional[str] = None,
    ) -> Dict[str, int]:
        sql = """
        SELECT
          COUNT(*)::int AS total,
          COUNT(*) FILTER (WHERE status = 'requested')::int AS requested,
          COUNT(*) FILTER (WHERE status = 'approved')::int AS approved,
          COUNT(*) FILTER (WHERE status = 'rejected')::int AS rejected
        FROM node_work.node_review_requests
        WHERE (%s IS NULL OR source = %s)
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (source, source)) or []
        row = (rows[0] if rows else {}) or {}
        return {
            "total": int(row.get("total") or 0),
            "requested": int(row.get("requested") or 0),
            "approved": int(row.get("approved") or 0),
            "rejected": int(row.get("rejected") or 0),
        }

    def create_node_review_request(
        self,
        *,
        source: str,
        lat: float,
        lon: float,
        node_type: str = "STOP",
        route_id: Optional[str] = None,
        seq: Optional[int] = None,
        name: Optional[str] = None,
        ref: Optional[str] = None,
        operator: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None,
        requested_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        node_type = str(node_type or "STOP").upper()
        if node_type not in {"STOP", "POI"}:
            node_type = "STOP"

        sql = """
        INSERT INTO node_work.node_review_requests
          (source, route_id, seq, status, lat, lon, node_type, name, ref, operator, tags, requested_by, notes)
        VALUES
          (%s, %s, %s, 'requested', %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING request_id, status, created_at
        """
        with db_conn() as conn:
            rows = fetchall(
                conn,
                sql,
                (
                    str(source or "free_create"),
                    str(route_id) if route_id else None,
                    int(seq) if seq is not None else None,
                    float(lat),
                    float(lon),
                    node_type,
                    name,
                    ref,
                    operator,
                    json.dumps(tags or {}, ensure_ascii=False),
                    requested_by,
                    notes,
                ),
            ) or []
        return rows[0] if rows else {}

    def list_stop_candidates_near_point(
        self,
        *,
        lat: float,
        lon: float,
        radius_m: float = 3.0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          m.node_id::text AS node_id,
          COALESCE(NULLIF(BTRIM(p.canonical_name), ''), n.name) AS name,
          n.ref AS ref,
          ('phase2_final:' || COALESCE(m.mapping_source, 'unknown')) AS source,
          ST_Y(n.geom) AS lat,
          ST_X(n.geom) AS lon,
          ST_Distance(
            n.geom::geography,
            ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography
          ) AS dist_m,
          n.updated_at AS updated_at
        FROM geo_prod.node_place_map m
        JOIN node_prod.nodes n
          ON n.node_id = m.node_id
        JOIN geo_prod.places p
          ON p.place_id = m.place_id
        WHERE n.node_type='STOP'
          AND p.status = 'active'
          AND ST_DWithin(
            n.geom::geography,
            ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography,
            %s
          )
        ORDER BY dist_m ASC, m.node_id ASC
        LIMIT %s
        """
        with db_conn() as conn:
            rows = fetchall(
                conn,
                sql,
                (float(lon), float(lat), float(lon), float(lat), float(radius_m), int(limit)),
            ) or []

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "node_id": str(r.get("node_id") or ""),
                    "name": r.get("name"),
                    "ref": r.get("ref"),
                    "source": r.get("source"),
                    "lat": (float(r.get("lat")) if r.get("lat") is not None else None),
                    "lon": (float(r.get("lon")) if r.get("lon") is not None else None),
                    "dist_m": float(r.get("dist_m") or 0.0),
                    "updated_at": (
                        r.get("updated_at").isoformat() if hasattr(r.get("updated_at"), "isoformat") else r.get("updated_at")
                    ),
                }
            )
        return out

    def get_node_prod_node(self, node_id: str) -> Dict[str, Any]:
        nid = str(node_id or "").strip()
        if not nid:
            return {}
        sql = """
        SELECT
          node_id::text AS node_id,
          node_type,
          name,
          ref,
          source,
          ST_Y(geom) AS lat,
          ST_X(geom) AS lon,
          updated_at
        FROM node_prod.nodes
        WHERE node_id::text = %s
        LIMIT 1
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (nid,)) or []
        return dict(rows[0]) if rows else {}

    def is_phase2_final_stop_node(self, node_id: str) -> bool:
        nid = str(node_id or "").strip()
        if not nid:
            return False
        sql = """
        SELECT 1
        FROM geo_prod.node_place_map m
        JOIN node_prod.nodes n
          ON n.node_id = m.node_id
        JOIN geo_prod.places p
          ON p.place_id = m.place_id
        WHERE m.node_id::text = %s
          AND n.node_type = 'STOP'
          AND p.status = 'active'
        LIMIT 1
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (nid,)) or []
        return bool(rows)

    def resolve_phase3_ambiguity_request(
        self,
        request_id: str,
        *,
        selected_node_id: str,
        selection_kind: Optional[str] = None,
        resolver: Optional[str] = None,
        delete_replaced_approved_node: bool = False,
        close_request: bool = False,
    ) -> Dict[str, Any]:
        rid = str(request_id or "").strip()
        selected_nid = str(selected_node_id or "").strip()
        if not rid:
            raise RuntimeError("request_id is required")
        if not selected_nid:
            raise RuntimeError("selected_node_id is required")

        with db_conn() as conn:
            rows = fetchall(
                conn,
                """
                SELECT request_id::text AS request_id,
                       source,
                       route_id::text AS route_id,
                       seq,
                       status,
                       lat,
                       lon,
                       approved_node_id::text AS approved_node_id,
                       tags
                FROM node_work.node_review_requests
                WHERE request_id::text = %s
                LIMIT 1
                """,
                (rid,),
            ) or []
            if not rows:
                raise RuntimeError(f"Request not found: {rid}")
            req = dict(rows[0])
            if str(req.get("source") or "").strip().lower() != "phase3_route":
                raise RuntimeError("resolve_phase3_ambiguity_request only supports source=phase3_route")
            req_tags = req.get("tags") or {}
            if isinstance(req_tags, str):
                try:
                    parsed = json.loads(req_tags)
                    req_tags = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    req_tags = {}
            if not isinstance(req_tags, dict):
                req_tags = {}

            route_id = str(req.get("route_id") or "").strip()
            seq = req.get("seq")
            if not route_id or seq is None:
                raise RuntimeError("Request has no route_id/seq; cannot resolve ambiguity.")

            node_rows = fetchall(
                conn,
                """
                SELECT node_id::text AS node_id,
                       ST_Distance(
                         geom::geography,
                         ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography
                       ) AS dist_m
                FROM node_prod.nodes
                WHERE node_id::text = %s
                LIMIT 1
                """,
                (float(req.get("lon") or 0.0), float(req.get("lat") or 0.0), selected_nid),
            ) or []
            if not node_rows:
                raise RuntimeError(f"Selected node_id not found in node_prod.nodes: {selected_nid}")
            dist_m = float(node_rows[0].get("dist_m") or 0.0)

            exec_sql(
                conn,
                """
                UPDATE route_work.relation_stop_prior
                SET matched_stop_node_id = %s::uuid,
                    match_dist_m = %s
                WHERE route_id::text = %s
                  AND seq = %s
                """,
                (selected_nid, dist_m, route_id, int(seq)),
            )
            updated_rows = fetchall(
                conn,
                """
                SELECT COUNT(*)::int AS n
                FROM route_work.relation_stop_prior
                WHERE route_id::text = %s
                  AND seq = %s
                  AND matched_stop_node_id::text = %s
                """,
                (route_id, int(seq), selected_nid),
            ) or [{"n": 0}]
            if int((updated_rows[0] or {}).get("n") or 0) <= 0:
                raise RuntimeError(f"Failed to resolve relation_stop_prior for route_id={route_id}, seq={seq}")

            resolved_choice = str(selection_kind or "").strip().lower()
            if resolved_choice not in {"request", "candidate"}:
                approved_node_id = str(req.get("approved_node_id") or "").strip()
                resolved_choice = "request" if (approved_node_id and approved_node_id == selected_nid) else "candidate"

            deleted_replaced_approved = 0
            approved_node_id = str(req.get("approved_node_id") or "").strip()
            if delete_replaced_approved_node and approved_node_id and approved_node_id != selected_nid:
                exec_sql(
                    conn,
                    "DELETE FROM node_prod.nodes WHERE node_id::text = %s",
                    (approved_node_id,),
                )
                drows = fetchall(
                    conn,
                    "SELECT 1 FROM node_prod.nodes WHERE node_id::text = %s LIMIT 1",
                    (approved_node_id,),
                ) or []
                deleted_replaced_approved = 1 if not drows else 0

            closed = 0
            if close_request:
                exec_sql(
                    conn,
                    "DELETE FROM node_work.node_review_requests WHERE request_id::text = %s",
                    (rid,),
                )
                exists = fetchall(
                    conn,
                    "SELECT 1 FROM node_work.node_review_requests WHERE request_id::text = %s LIMIT 1",
                    (rid,),
                ) or []
                closed = 1 if not exists else 0
            else:
                updated_tags = dict(req_tags)
                updated_tags["conflict_resolved"] = True
                updated_tags["resolved_choice"] = resolved_choice
                updated_tags["resolved_node_id"] = selected_nid
                updated_tags["resolved_route_id"] = route_id
                updated_tags["resolved_seq"] = int(seq)
                updated_tags["resolved_at"] = datetime.now(timezone.utc).isoformat()
                status_now = str(req.get("status") or "").strip().lower()
                status_after = status_now if status_now else "requested"
                # If request never got approved and candidate won, request is not selected.
                if resolved_choice == "candidate" and status_after == "requested":
                    status_after = "rejected"
                exec_sql(
                    conn,
                    """
                    UPDATE node_work.node_review_requests
                    SET status = %s,
                        tags = %s::jsonb,
                        reviewed_by = COALESCE(%s, reviewed_by),
                        reviewed_at = now()
                    WHERE request_id::text = %s
                    """,
                    (status_after, json.dumps(updated_tags, ensure_ascii=False), resolver, rid),
                )

        return {
            "request_id": rid,
            "route_id": route_id,
            "seq": int(seq),
            "selected_node_id": selected_nid,
            "selection_kind": resolved_choice,
            "match_dist_m": dist_m,
            "deleted_replaced_approved_node": int(deleted_replaced_approved),
            "request_closed": int(closed),
        }

    def approve_node_review_request(
        self,
        request_id: str,
        *,
        node_type: str = "STOP",
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        name: Optional[str] = None,
        ref: Optional[str] = None,
        operator: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None,
        reviewer: Optional[str] = None,
    ) -> Dict[str, Any]:
        node_type = str(node_type or "STOP").upper()
        if node_type not in {"STOP", "POI"}:
            node_type = "STOP"

        with db_conn() as conn:
            cur_rows = fetchall(
                conn,
                """
                SELECT *
                FROM node_work.node_review_requests
                WHERE request_id = %s
                LIMIT 1
                """,
                (str(request_id),),
            ) or []
            if not cur_rows:
                raise RuntimeError(f"Request not found: {request_id}")
            req = cur_rows[0]

            use_lat = float(lat) if lat is not None else float(req.get("lat"))
            use_lon = float(lon) if lon is not None else float(req.get("lon"))
            use_name = name if name is not None else req.get("name")
            use_ref = ref if ref is not None else req.get("ref")
            use_operator = operator if operator is not None else req.get("operator")
            use_tags = dict(req.get("tags") or {})
            if isinstance(tags, dict):
                use_tags.update(tags)
            if use_name:
                use_tags["name"] = use_name
            if use_ref:
                use_tags["ref"] = use_ref
            if use_operator:
                use_tags["operator"] = use_operator

            from phase3_routes.services.stop_quality import (
                StopTreatmentInput,
                treat_stop,
            )
            import psycopg2.extras as _ppx

            existing_node_id = req.get("approved_node_id")
            input_node_id = str(existing_node_id) if existing_node_id else None

            extras: Dict[str, Any] = {
                "source": "review_request",
                "tag_kind": "bus_stop" if node_type == "STOP" else "other",
                "chosen_tags": _ppx.Json(use_tags),
                "approved_at": datetime.now(timezone.utc),
            }
            if use_ref:
                extras["ref"] = use_ref
            if use_operator:
                extras["operator"] = use_operator

            res = treat_stop(
                StopTreatmentInput(
                    operation="approve_promote",
                    caller="phase1_client.approve_review_request",
                    node_id=input_node_id,
                    proposed_name=use_name or "",
                    proposed_lat=float(use_lat),
                    proposed_lon=float(use_lon),
                    node_type=node_type,
                    confidence=1.0,
                    extras=extras,
                ),
                conn,
            )
            if not res.success:
                raise RuntimeError(f"approve_node_review_request failed: {res.error}")
            node_id = str(res.node_id)

            exec_sql(
                conn,
                """
                UPDATE node_work.node_review_requests
                SET status = 'approved',
                    lat = %s,
                    lon = %s,
                    node_type = %s,
                    name = %s,
                    ref = %s,
                    operator = %s,
                    tags = %s::jsonb,
                    reviewed_by = %s,
                    reviewed_at = now(),
                    approved_node_id = %s
                WHERE request_id = %s
                """,
                (
                    float(use_lat),
                    float(use_lon),
                    node_type,
                    use_name,
                    use_ref,
                    use_operator,
                    json.dumps(use_tags, ensure_ascii=False),
                    reviewer,
                    node_id,
                    str(request_id),
                ),
            )

        return {"request_id": str(request_id), "status": "approved", "node_id": node_id}

    def reject_node_review_request(self, request_id: str, *, reviewer: Optional[str] = None) -> Dict[str, Any]:
        with db_conn() as conn:
            exec_sql(
                conn,
                """
                UPDATE node_work.node_review_requests
                SET status = 'rejected',
                    reviewed_by = %s,
                    reviewed_at = now()
                WHERE request_id = %s
                """,
                (reviewer, str(request_id)),
            )
        return {"request_id": str(request_id), "status": "rejected"}

    def delete_node_review_request(self, request_id: str) -> Dict[str, Any]:
        rid = str(request_id)
        with db_conn() as conn:
            rows = fetchall(
                conn,
                """
                SELECT request_id, status, approved_node_id
                FROM node_work.node_review_requests
                WHERE request_id = %s
                LIMIT 1
                """,
                (rid,),
            ) or []
            if not rows:
                raise RuntimeError(f"Request not found: {rid}")
            row = rows[0]
            if row.get("approved_node_id"):
                raise RuntimeError(
                    "Cannot delete an already approved request. "
                    "Use reject for pending requests or handle node_prod cleanup first."
                )

            exec_sql(
                conn,
                "DELETE FROM node_work.node_review_requests WHERE request_id = %s",
                (rid,),
            )
        return {"request_id": rid, "status": "deleted"}

    def create_prod_node_manual(
        self,
        *,
        node_type: str,
        lat: float,
        lon: float,
        name: Optional[str] = None,
        ref: Optional[str] = None,
        operator: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None,
        source_node_set_id: Optional[str] = None,
        source: str = "manual_workspace",
        confidence: float = 1.0,
        node_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        node_type = str(node_type or "STOP").upper()
        if node_type not in {"STOP", "POI"}:
            node_type = "STOP"

        from phase3_routes.services.stop_quality import (
            StopTreatmentInput,
            treat_stop,
        )
        import psycopg2.extras as _ppx

        nid = str(node_id) if node_id else None
        merged_tags = dict(tags or {})
        if name:
            merged_tags["name"] = name
        if ref:
            merged_tags["ref"] = ref
        if operator:
            merged_tags["operator"] = operator

        extras: Dict[str, Any] = {
            "source": source,
            "tag_kind": "bus_stop" if node_type == "STOP" else "other",
            "chosen_tags": _ppx.Json(merged_tags),
            "approved_at": datetime.now(timezone.utc),
        }
        if ref:
            extras["ref"] = ref
        if operator:
            extras["operator"] = operator
        if source_node_set_id:
            extras["source_node_set_id"] = source_node_set_id

        with db_conn() as conn:
            res = treat_stop(
                StopTreatmentInput(
                    operation="approve_promote",
                    caller="phase1_client.create_manual",
                    node_id=nid,
                    proposed_name=name or "",
                    proposed_lat=float(lat),
                    proposed_lon=float(lon),
                    node_type=node_type,
                    confidence=float(confidence),
                    extras=extras,
                ),
                conn,
            )
            if not res.success:
                raise RuntimeError(f"create_prod_node_manual failed: {res.error}")
            final_id = str(res.node_id)
        return {"node_id": final_id, "status": "approved", "source": source}

    def delete_resolved_node(self, node_id: str) -> None:
        with db_conn() as conn:
            exec_sql(
                conn,
                "DELETE FROM node_work.nodes_resolved WHERE node_id = %s",
                (str(node_id),),
            )
            try:
                exec_sql(
                    conn,
                    "DELETE FROM node_prod.nodes WHERE node_id = %s",
                    (str(node_id),),
                )
            except Exception:
                pass

    def get_node_set_details(self, node_set_id: str) -> Dict[str, Any]:
        normalized = self._normalize_node_set_id(node_set_id)
        if normalized is None:
            return {
                "node_set_id": str(node_set_id),
                "invalid_node_set_id": True,
                "n_candidates": 0,
                "n_stop_like": 0,
                "n_poi_like": 0,
                "n_resolved": 0,
                "n_approved": 0,
                "n_work": 0,
                "n_rejected": 0,
                "n_prod": 0,
                "n_prod_stop": 0,
                "n_prod_poi": 0,
                "last_promoted_at": None,
            }
        with db_conn() as conn:
            meta = fetchall(
                conn,
                """
                SELECT node_set_id, created_at, source_run_ids, action_ids, params_used
                FROM node_work.node_candidate_sets
                WHERE node_set_id = %s
                LIMIT 1
                """,
                (normalized,),
            ) or []
            row = dict(meta[0]) if meta else {"node_set_id": str(node_set_id)}
            row.update(
                self._extractor_review_payload(
                    row.get("params_used"),
                    action_ids=row.get("action_ids"),
                )
            )

            run_ids = self._as_uuid_list(row.get("source_run_ids"))
            if run_ids:
                raw_rows = fetchall(
                    conn,
                    """
                    SELECT COUNT(*)::int AS raw_count
                    FROM node_raw.overpass_elements
                    WHERE run_id = ANY(%s::uuid[])
                    """,
                    (run_ids,),
                ) or [{"raw_count": 0}]
                row.update(raw_rows[0] or {})

            cands = fetchall(
                conn,
                """
                SELECT
                  COUNT(*)::int AS n_candidates,
                  COUNT(*) FILTER (WHERE COALESCE(tag_kind,'') IN ('bus_stop','platform','stop_position','station','tram_stop'))::int AS n_stop_like,
                  COUNT(*) FILTER (WHERE COALESCE(tag_kind,'') NOT IN ('bus_stop','platform','stop_position','station','tram_stop'))::int AS n_poi_like
                FROM node_work.node_candidates
                WHERE node_set_id = %s
                """,
                (normalized,),
            ) or [{}]

            resolved = fetchall(
                conn,
                """
                SELECT
                  COUNT(*)::int AS n_resolved,
                  COUNT(*) FILTER (WHERE status='approved')::int AS n_approved,
                  COUNT(*) FILTER (WHERE status='work')::int AS n_work,
                  COUNT(*) FILTER (WHERE status='rejected')::int AS n_rejected
                FROM node_work.nodes_resolved
                WHERE node_set_id = %s
                """,
                (normalized,),
            ) or [{}]

            prod = fetchall(
                conn,
                """
                SELECT
                  COUNT(*)::int AS n_prod,
                  COUNT(*) FILTER (WHERE node_type='STOP')::int AS n_prod_stop,
                  COUNT(*) FILTER (WHERE node_type='POI')::int AS n_prod_poi,
                  MAX(approved_at) AS last_promoted_at
                FROM node_prod.nodes
                WHERE source_node_set_id = %s
                """,
                (normalized,),
            ) or [{}]

            row.update(cands[0] or {})
            row.update(resolved[0] or {})
            row.update(prod[0] or {})
            return row

    def get_workspace_state_summary(self, node_set_id: str) -> Dict[str, Any]:
        details = dict(self.get_node_set_details(str(node_set_id)) or {})
        pending_review = (
            self._safe_int(details.get("n_work"))
            if details.get("n_work") is not None
            else None
        )
        return {
            "node_set_id": str(node_set_id),
            "resolved_total": self._safe_int(details.get("n_resolved")),
            "approved_count": self._safe_int(details.get("n_approved")),
            "rejected_count": self._safe_int(details.get("n_rejected")),
            "pending_review_count": pending_review,
        }

    @staticmethod
    def _classify_promote_dry_run_status(
        *,
        legacy_status: str,
        dry_run_resolved: int,
        workspace_state_summary: Dict[str, Any],
    ) -> str:
        legacy = str(legacy_status or "").strip().lower()
        approved_count = int(workspace_state_summary.get("approved_count") or 0)
        resolved_total = int(workspace_state_summary.get("resolved_total") or 0)
        if approved_count <= 0 and resolved_total > 0:
            return "no_approved_nodes"
        if legacy in {"", "not_found"}:
            if resolved_total > 0 or approved_count > 0:
                return "staging_missing"
            return "node_set_missing"
        if approved_count <= 0:
            return "no_approved_nodes"
        if int(dry_run_resolved or 0) <= 0:
            if resolved_total > 0:
                return "staging_missing"
            return "query_filtered_empty"
        return "ok"

    @staticmethod
    def _promote_precondition_for_status(status: str) -> str:
        code = str(status or "").strip().lower()
        if code == "no_approved_nodes":
            return "no_approved_nodes"
        if code == "node_set_missing":
            return "node_set_missing"
        if code == "ok":
            return "preconditions_met"
        return "lookup_or_staging_issue"

    def get_promote_dry_run(self, node_set_id: str) -> Dict[str, Any]:
        summary_row = dict(self.get_node_set_summary(str(node_set_id)) or {})
        workspace_state_summary = self.get_workspace_state_summary(str(node_set_id))
        n_resolved = self._safe_int(summary_row.get("n_resolved"))
        n_approved = self._safe_int(summary_row.get("n_approved"))
        n_rejected = self._safe_int(summary_row.get("n_rejected"))
        legacy_status = str(summary_row.get("status") or "")
        status = self._classify_promote_dry_run_status(
            legacy_status=legacy_status,
            dry_run_resolved=n_resolved,
            workspace_state_summary=workspace_state_summary,
        )
        diagnostics_hint = None
        if status in {"staging_missing", "query_filtered_empty"}:
            diagnostics_hint = (
                "Workspace has approved/resolved evidence but promote dry-run returned empty; "
                "inspect promote lookup joins/filters."
            )
        elif status == "no_approved_nodes":
            diagnostics_hint = "No approved nodes yet in workspace; approve nodes before promote."
        elif status == "node_set_missing":
            diagnostics_hint = "Node set was not found in promote lookup source."

        recommended_action = None
        if status == "no_approved_nodes":
            recommended_action = "approve_nodes_before_promote"
        elif status in {"staging_missing", "query_filtered_empty", "node_set_missing"}:
            recommended_action = "inspect_promote_lookup"

        return {
            "node_set_id": str(node_set_id),
            "n_resolved": int(n_resolved),
            "n_approved": int(n_approved),
            "n_rejected": int(n_rejected),
            "status": status,
            "legacy_status": legacy_status or None,
            "promote_precondition": self._promote_precondition_for_status(status),
            "workspace_state_summary": workspace_state_summary,
            "recommended_action": recommended_action,
            "diagnostics_hint": diagnostics_hint,
        }

    def get_promote_status(self, node_set_id: str) -> Dict[str, Any]:
        d = self.get_node_set_details(node_set_id)
        if int(d.get("n_prod") or 0) > 0:
            status = "promoted_to_node_prod"
        elif int(d.get("n_approved") or 0) > 0:
            status = "ready_to_promote"
        else:
            status = "pending"
        return {
            "node_set_id": str(node_set_id),
            "promote_status": status,
            "n_approved": int(d.get("n_approved") or 0),
            "n_prod": int(d.get("n_prod") or 0),
            "n_prod_stop": int(d.get("n_prod_stop") or 0),
            "n_prod_poi": int(d.get("n_prod_poi") or 0),
            "last_promoted_at": d.get("last_promoted_at"),
        }

    # -----------------------------
    # Candidates (read-only)
    # -----------------------------

    def get_resolved_points(self, node_set_id: str, *, limit: int = 4000) -> List[dict]:
        sql = """
        SELECT
        ST_Y(r.geom)::double precision AS lat,
        ST_X(r.geom)::double precision AS lon,
        COALESCE(r.chosen_tags->>'name', r.chosen_tags->>'ref', '') AS label,
        r.confidence AS value,
        r.resolved_at
        FROM node_work.nodes_resolved r
        WHERE r.node_set_id = %s
        ORDER BY r.resolved_at ASC
        LIMIT %s
        """
        with db_conn() as conn:
            return fetchall(conn, sql, (str(node_set_id), int(limit))) or []
    def get_resolved_points_kpis(self, node_set_id: str | UUID) -> Dict[str, Any]:
        sql = """
        SELECT confidence
        FROM node_work.nodes_resolved
        WHERE node_set_id = %s
        AND confidence IS NOT NULL
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (str(node_set_id),)) or []

        vals = [float(r["confidence"]) for r in rows if r.get("confidence") is not None]
        n = len(vals)

        if n == 0:
            return {
                "n_total": 0,
                "avg_confidence": None,
                "median_confidence": None,
                "n_high": 0,
                "n_medium": 0,
                "n_low": 0,
            }

        avg_c = sum(vals) / n
        med_c = median(vals)

        n_high = sum(v >= 0.75 for v in vals)
        n_low = sum(v < 0.40 for v in vals)
        n_medium = n - n_high - n_low

        return {
            "n_total": n,
            "avg_confidence": round(avg_c, 4),
            "median_confidence": round(med_c, 4),
            "n_high": n_high,
            "n_medium": n_medium,
            "n_low": n_low,
        }

    def get_ranked_sets(self, *, limit: int = 50) -> List[dict]:
        sql = """
        SELECT
        node_set_id,
        rank_score AS score,
        rank_model_ver,
        source_run_ids,
        action_ids,
        params_used,
        created_at
        FROM node_work.node_candidate_sets
        ORDER BY rank_score DESC NULLS LAST, created_at DESC
        LIMIT %s
        """
        with db_conn() as conn:
            return fetchall(conn, sql, (int(limit),)) or []

    def get_phase1_evidence_metrics(self, node_set_id: str) -> Dict[str, Any]:
        metrics = dict(self._collect_phase1_metrics(str(node_set_id)) or {})
        workspace = self.get_workspace_state_summary(str(node_set_id))
        summary = dict(self.get_node_set_summary(str(node_set_id)) or {})
        return {
            "node_set_id": str(node_set_id),
            "metrics": metrics,
            "workspace_state_summary": workspace,
            "node_set_summary": summary,
        }

    def get_phase1_cluster_size_histogram(self, node_set_id: str, *, limit: int = 120) -> List[Dict[str, Any]]:
        sql = """
        SELECT
          row_number() OVER (ORDER BY cluster_size DESC, cluster_id) AS rank_idx,
          cluster_id::text AS cluster_id,
          cluster_size::int AS cluster_size
        FROM (
          SELECT cluster_id, COUNT(*)::int AS cluster_size
          FROM node_work.node_clusters
          WHERE node_set_id = %s::uuid
          GROUP BY cluster_id
        ) q
        ORDER BY cluster_size DESC, cluster_id
        LIMIT %s
        """
        try:
            with db_conn() as conn:
                rows = fetchall(conn, sql, (str(node_set_id), max(1, int(limit or 120)))) or []
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "rank_idx": self._safe_int(row.get("rank_idx")),
                    "cluster_id": str(row.get("cluster_id") or ""),
                    "cluster_size": self._safe_int(row.get("cluster_size")),
                }
            )
        return out

    def _synthetic_phase1_attempt_history_rows(self, node_set_id: str, *, limit: int = 80) -> List[Dict[str, Any]]:
        details = dict(self.get_node_set_details(str(node_set_id)) or {})
        params = dict(details.get("params_used") or {}) if isinstance(details.get("params_used"), dict) else {}
        attempts = list(params.get("extraction_attempts") or [])
        if not attempts:
            diagnostics = dict(params.get("extraction_diagnostics") or {}) if isinstance(params.get("extraction_diagnostics"), dict) else {}
            if diagnostics:
                attempts = [diagnostics]

        if not attempts:
            return []

        out: List[Dict[str, Any]] = []
        for idx, raw_attempt in enumerate(attempts[: max(1, int(limit))], 1):
            attempt = dict(raw_attempt or {})
            candidate_count = attempt.get("candidate_count")
            raw_elements_count = attempt.get("raw_elements_count")
            quality_score = None
            if candidate_count is not None:
                try:
                    denom = max(float(raw_elements_count or candidate_count or 1), 1.0)
                    quality_score = round(min(1.0, float(candidate_count) / denom), 4)
                except Exception:
                    quality_score = None
            status = str(
                attempt.get("status")
                or ("ok" if int(candidate_count or raw_elements_count or 0) > 0 else "empty")
            ).strip() or None
            out.append(
                {
                    "attempt_no": self._safe_int(attempt.get("attempt_no") or idx),
                    "action_or_template": (
                        str(
                            attempt.get("action_id")
                            or details.get("extraction_action")
                            or attempt.get("template")
                            or ""
                        ).strip()
                        or None
                    ),
                    "candidate_count": (
                        self._safe_int(candidate_count)
                        if candidate_count is not None
                        else None
                    ),
                    "quality_score": quality_score,
                    "duration_ms": (
                        self._safe_int(attempt.get("runtime_ms"))
                        if attempt.get("runtime_ms") is not None
                        else None
                    ),
                    "status": status,
                    "best_attempt": idx == 1,
                }
            )
        return out

    def get_phase1_attempt_history_rows(self, node_set_id: str, *, limit: int = 80) -> List[Dict[str, Any]]:
        if not self._table_exists("console.pipeline_autopilot_step_attempts"):
            return self._synthetic_phase1_attempt_history_rows(str(node_set_id), limit=limit)
        sql = """
        SELECT
          created_at,
          executor_result_summary
        FROM console.pipeline_autopilot_step_attempts
        WHERE phase = 'phase1'
          AND step_id = 'P1.1_EXTRACT_BUILD_NODE_SET'
          AND (
            executor_result_summary->>'node_set_id' = %s
            OR executor_result_summary#>>'{best,node_set_id}' = %s
            OR executor_result_summary#>>'{best_attempt,node_set_id}' = %s
          )
        ORDER BY created_at DESC
        LIMIT %s
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (str(node_set_id), str(node_set_id), str(node_set_id), max(1, int(limit)))) or []
        if not rows:
            return self._synthetic_phase1_attempt_history_rows(str(node_set_id), limit=limit)
        summary = dict((rows[0] or {}).get("executor_result_summary") or {})
        history = dict(summary.get("extraction_attempt_history_summary") or {})
        attempts = list(history.get("attempts") or [])
        best_idx = self._safe_int(history.get("best_attempt_index"), 0)
        out: List[Dict[str, Any]] = []
        for row in attempts[:120]:
            attempt = dict(row or {})
            out.append(
                {
                    "attempt_no": self._safe_int(attempt.get("attempt_no") or attempt.get("attempt_index") or 0),
                    "action_or_template": (
                        str(
                            attempt.get("action_or_template_used")
                            or attempt.get("action_id")
                            or attempt.get("template")
                            or ""
                        ).strip()
                        or None
                    ),
                    "candidate_count": (
                        self._safe_int(attempt.get("candidate_count"))
                        if attempt.get("candidate_count") is not None
                        else None
                    ),
                    "quality_score": attempt.get("quality_score"),
                    "duration_ms": (
                        self._safe_int(attempt.get("duration_ms"))
                        if attempt.get("duration_ms") is not None
                        else None
                    ),
                    "status": str(attempt.get("status") or "").strip() or None,
                    "best_attempt": bool(best_idx > 0 and self._safe_int(attempt.get("attempt_no")) == best_idx),
                }
            )
        return out

    def get_phase1_patch_impact_records(self, node_set_id: str, *, limit: int = 40) -> List[Dict[str, Any]]:
        if not self._table_exists("console.pipeline_autopilot_patch_registry"):
            return []
        if not self._table_exists("console.pipeline_autopilot_step_attempts"):
            return []
        sql = """
        WITH matched AS (
          SELECT DISTINCT pr.record_json
          FROM console.pipeline_autopilot_patch_registry pr
          LEFT JOIN console.pipeline_autopilot_step_attempts sa
            ON sa.run_id = pr.run_id
           AND sa.phase = 'phase1'
           AND sa.step_id = pr.step_id
           AND sa.attempt_no = pr.attempt_no
          WHERE pr.phase = 'phase1'
            AND (
              sa.executor_result_summary->>'node_set_id' = %s
              OR sa.executor_result_summary#>>'{best,node_set_id}' = %s
              OR sa.executor_result_summary#>>'{best_attempt,node_set_id}' = %s
              OR pr.record_json#>>'{baseline_run_ref,node_set_id}' = %s
              OR pr.record_json#>>'{retest_run_refs,0,node_set_id}' = %s
            )
          ORDER BY (pr.record_json->>'updated_at') DESC NULLS LAST
          LIMIT %s
        )
        SELECT record_json
        FROM matched
        """
        with db_conn() as conn:
            rows = fetchall(
                conn,
                sql,
                (
                    str(node_set_id),
                    str(node_set_id),
                    str(node_set_id),
                    str(node_set_id),
                    str(node_set_id),
                    max(1, int(limit or 40)),
                ),
            ) or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            rec = dict(row.get("record_json") or {})
            if rec:
                out.append(rec)
        return out

    def get_phase1_health_over_time(self, node_set_id: str, *, limit: int = 60) -> List[Dict[str, Any]]:
        if not self._table_exists("console.pipeline_autopilot_step_attempts"):
            return []
        sql = """
        SELECT
          created_at,
          run_id,
          step_id,
          ai_bot_snapshot,
          executor_result_summary
        FROM console.pipeline_autopilot_step_attempts
        WHERE phase = 'phase1'
          AND step_id = 'P1.1_EXTRACT_BUILD_NODE_SET'
          AND (
            executor_result_summary->>'node_set_id' = %s
            OR executor_result_summary#>>'{best,node_set_id}' = %s
            OR executor_result_summary#>>'{best_attempt,node_set_id}' = %s
          )
        ORDER BY created_at DESC
        LIMIT %s
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (str(node_set_id), str(node_set_id), str(node_set_id), max(1, int(limit)))) or []
        out: List[Dict[str, Any]] = []
        for row in rows:
            snap = dict(row.get("ai_bot_snapshot") or {})
            scores = dict(snap.get("scores") or {})
            extractor_status = dict(snap.get("extractor_status") or {})
            extractor_scores = dict(extractor_status.get("scores") or {})
            out.append(
                {
                    "created_at": row.get("created_at"),
                    "run_id": str(row.get("run_id") or ""),
                    "step_id": str(row.get("step_id") or ""),
                    "extractor_efficiency_health_score": (
                        extractor_status.get("extractor_efficiency_health_score")
                        if extractor_status.get("extractor_efficiency_health_score") is not None
                        else scores.get("extractor_efficiency_health_score")
                    ),
                    "completion_quality_score": (
                        extractor_scores.get("completion_quality_score")
                        if extractor_scores.get("completion_quality_score") is not None
                        else scores.get("completion_quality_score")
                    ),
                    "shadow_top1_match": (
                        dict(snap.get("metrics") or {}).get("shadow_retry_top1_match")
                    ),
                }
            )
        out.sort(key=lambda x: str(x.get("created_at") or ""))
        return out

    def list_candidates(self, node_set_id: str) -> List[dict]:
        sql = """
        SELECT
        c.node_candidate_id AS candidate_id,
        c.node_set_id,
        c.source_run_id,
        c.osm_type,
        c.osm_id,
        c.tag_kind,
        c.created_at,
        c.tags,
        ST_Y(c.geom)::double precision AS lat,
        ST_X(c.geom)::double precision AS lon
        FROM node_work.node_candidates c
        WHERE c.node_set_id = %s
        ORDER BY c.created_at ASC
        """
        with db_conn() as conn:
            return fetchall(conn, sql, (str(node_set_id),)) or []


    def get_node_confidence_distribution(
        self,
        node_set_id: str | UUID,
        *,
        bins: Optional[list[float]] = None,
        source: str = "work",   # "work" (default) or "prod"
    ) -> dict:
        if bins is None:
            bins = [0.0, 0.3, 0.5, 0.7, 0.9, 1.0]

        if source == "prod":
            sql = """
            SELECT confidence
            FROM node_prod.nodes
            WHERE source_node_set_id = %s
              AND confidence IS NOT NULL
            """
        else:
            sql = """
            SELECT confidence
            FROM node_work.nodes_resolved
            WHERE node_set_id = %s
              AND confidence IS NOT NULL
            """

        with db_conn() as conn:
            rows = fetchall(conn, sql, (str(node_set_id),)) or []

        values = [float(r["confidence"]) for r in rows if r.get("confidence") is not None]
        if not values:
            return {"count": 0, "avg": None, "min": None, "max": None, "histogram": {}}

        hist: Dict[str, int] = {}
        for i in range(len(bins) - 1):
            lo, hi = bins[i], bins[i + 1]
            key = f"{lo:.1f}–{hi:.1f}"
            hist[key] = sum(lo <= v < hi for v in values)

        # include 1.0 in last bucket
        last_key = f"{bins[-2]:.1f}–{bins[-1]:.1f}"
        hist[last_key] = hist.get(last_key, 0) + sum(v == 1.0 for v in values)

        return {
            "count": len(values),
            "avg": round(sum(values) / len(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "histogram": hist,
        }

    def approve_all_resolved(self, node_set_id: UUID | str) -> Dict[str, Any]:
        """Mark all resolved nodes in a node_set as 'approved'."""
        with db_conn() as conn:
            exec_sql(
                conn,
                f"""
                UPDATE {T_RESOLVED}
                SET status = 'approved'
                WHERE node_set_id = %s
                """,
                (str(node_set_id),),
            )
        return {"node_set_id": str(node_set_id), "status": "approved"}


    def get_candidate(self, candidate_id: str) -> Dict[str, Any]:
        sql = """
        SELECT
        c.node_candidate_id AS candidate_id,
        c.node_set_id,
        c.source_run_id,
        c.osm_type,
        c.osm_id,
        c.tag_kind,
        c.created_at,
        c.tags,

        -- geometry as JSON-safe data
        ST_Y(c.geom)::double precision AS lat,
        ST_X(c.geom)::double precision AS lon,
        ST_AsGeoJSON(c.geom)::jsonb AS geom_geojson,

        -- optional: attach features as "score/status"
        COALESCE(f.prob_stop, f.confidence_v0, 0.0) AS score,
        COALESCE(f.node_class_pred, '—') AS status,
        f.model_version,
        f.prob_stop,
        f.prob_poi,
        f.confidence_v0
        FROM node_work.node_candidates c
        LEFT JOIN node_work.node_features f
        ON f.node_candidate_id = c.node_candidate_id
        WHERE c.node_candidate_id = %s
        """
        with db_conn() as conn:
            rows = fetchall(conn, sql, (str(candidate_id),)) or []
        if not rows:
            raise RuntimeError(f"Candidate not found: {candidate_id}")
        return rows[0]


    # -----------------------------
    # Explorer / analytics
    # -----------------------------

    def promote(self, node_set_id: str) -> dict:
        summary = self.get_node_set_summary(node_set_id)
        status = (summary or {}).get("status")
        if status != "approved":
            raise RuntimeError(f"Cannot promote node_set_id={node_set_id}: status={status!r}")
        return promote(node_set_id)
