from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from phase3_routes.services.route_constructor.src.db.conn import db_conn, db_cursor

from .route_pairing import (
    clip01,
    corridor_overlap_score,
    direction_word_conflict_flag,
    endpoint_swap_name_similarity,
    endpoint_swap_score,
    haversine_m,
    name_family_match_score,
    pair_points_by_distance,
    parse_linestring_wkt,
    path_similarity_score,
    polyline_length_m,
    reverse_order_score,
    reverse_progression_score,
    reverse_sequence_similarity,
    route_loop_suspicion,
    safe_float,
    score_from_distance,
    shape_direction_opposition_score,
    text_similarity,
)
from datamind_console.ai_insights.sequence_quality import evaluate_sequence_quality

LonLat = Tuple[float, float]


class RoutePairEvidenceExtractor:
    """
    v1 route-pair evidence extractor for merge-assist proposals.

    This extractor intentionally does not bind routes. It only computes evidence.
    """

    def __init__(self, *, pair_distance_m: float = 180.0) -> None:
        self.pair_distance_m = float(pair_distance_m)
        self._profile_cache: Dict[str, Dict[str, Any]] = {}

    def load_route_profile(self, route_id: str, *, use_cache: bool = True) -> Dict[str, Any]:
        rid = self._normalize_uuid(route_id)
        if not rid:
            return {"route_id": str(route_id or ""), "error": "invalid_route_id"}
        if use_cache and rid in self._profile_cache:
            return dict(self._profile_cache[rid])

        profile: Dict[str, Any] = {
            "route_id": rid,
            "service_route_id": None,
            "direction_id": None,
            "route_ref": "",
            "route_name": "",
            "operator_name": "",
            "network_name": "",
            "aliases": [],
            "relation_name": "",
            "relation_ref": "",
            "relation_operator": "",
            "relation_network": "",
            "relation_from": "",
            "relation_to": "",
            "relation_tags": {},
            "direction_semantics": {},
            "stop_points": [],
            "stop_ids": [],
            "stop_names": [],
            "stop_source": "none",
            "prior_rows": [],
            "sequence_quality": {},
            "geometry_points": [],
            "geometry_length_m": None,
            "geometry_source": "none",
            "coverage": {},
        }

        with db_conn() as conn:
            base = self._load_base_context(conn, rid)
            profile.update(base)

            relation = self._load_relation_context(conn, rid, chosen_relation_id=base.get("chosen_osm_relation_id"))
            profile.update(relation)

            prior_rows = self._load_prior_rows(conn, rid)
            profile["prior_rows"] = prior_rows

            stop_ctx = self._load_stop_context(conn, rid)
            profile["stop_points"] = stop_ctx.get("points") or []
            profile["stop_ids"] = stop_ctx.get("stop_ids") or []
            profile["stop_names"] = stop_ctx.get("stop_names") or []
            profile["stop_source"] = stop_ctx.get("source") or "none"

            geom = self._load_geometry_context(conn, rid)
            profile["geometry_points"] = geom.get("points") or []
            profile["geometry_length_m"] = geom.get("length_m")
            profile["geometry_source"] = geom.get("source") or "none"

        if prior_rows:
            matched = sum(1 for r in prior_rows if str(r.get("matched_stop_node_id") or "").strip())
            unmatched = max(0, len(prior_rows) - matched)
            ambiguous = sum(1 for r in prior_rows if str(r.get("match_state") or "").strip().lower() == "ambiguous")
            seq_out = evaluate_sequence_quality(
                prior_rows=prior_rows,
                matched_count=matched,
                unmatched_count=unmatched,
                ambiguous_count=ambiguous,
                sequence_edit_count=None,
            )
        else:
            seq_out = evaluate_sequence_quality(prior_rows=[])
        profile["sequence_quality"] = seq_out

        profile["coverage"] = {
            "has_semantics_name": bool(str(profile.get("route_name") or "").strip()),
            "has_ref": bool(str(profile.get("route_ref") or "").strip()),
            "has_operator": bool(str(profile.get("operator_name") or "").strip()),
            "has_relation_tags": bool(profile.get("relation_tags")),
            "has_stop_points": bool(profile.get("stop_points")),
            "has_stop_ids": bool(profile.get("stop_ids")),
            "has_geometry": bool(profile.get("geometry_points")),
            "has_prior_rows": bool(profile.get("prior_rows")),
        }

        self._profile_cache[rid] = dict(profile)
        return profile

    def extract_route_pair_evidence(self, route_a_id: str, route_b_id: str) -> Dict[str, Any]:
        a = self.load_route_profile(route_a_id)
        b = self.load_route_profile(route_b_id)
        return self.extract_route_pair_evidence_from_profiles(a, b)

    def extract_route_pair_evidence_from_profiles(
        self,
        profile_a: Dict[str, Any],
        profile_b: Dict[str, Any],
    ) -> Dict[str, Any]:
        a = dict(profile_a or {})
        b = dict(profile_b or {})
        rid_a = str(a.get("route_id") or "")
        rid_b = str(b.get("route_id") or "")

        stop_points_a = self._as_points(a.get("stop_points"))
        stop_points_b = self._as_points(b.get("stop_points"))
        stop_ids_a = [str(x) for x in (a.get("stop_ids") or []) if str(x)]
        stop_ids_b = [str(x) for x in (b.get("stop_ids") or []) if str(x)]
        geom_a = self._as_points(a.get("geometry_points"))
        geom_b = self._as_points(b.get("geometry_points"))

        pairings = pair_points_by_distance(
            stop_points_a,
            stop_points_b,
            max_distance_m=float(self.pair_distance_m),
        )

        exact_overlap = self._exact_overlap_ratio(stop_ids_a, stop_ids_b)
        reverse_exact = reverse_sequence_similarity(stop_ids_a, stop_ids_b)

        paired_alignment = self._paired_alignment_score(
            pairings,
            len_a=len(stop_points_a),
            len_b=len(stop_points_b),
            max_distance_m=self.pair_distance_m,
        )
        reverse_order_pairs = reverse_order_score(pairings, len_b=len(stop_points_b))

        endpoint_swap = endpoint_swap_score(
            stop_points_a[0] if stop_points_a else (geom_a[0] if geom_a else None),
            stop_points_a[-1] if stop_points_a else (geom_a[-1] if geom_a else None),
            stop_points_b[0] if stop_points_b else (geom_b[0] if geom_b else None),
            stop_points_b[-1] if stop_points_b else (geom_b[-1] if geom_b else None),
        )

        shared_middle = self._shared_middle_alignment(
            stop_points_a or geom_a,
            stop_points_b or geom_b,
        )

        shared_corridor = corridor_overlap_score(geom_a, geom_b, threshold_m=120.0)
        reverse_progress_a = reverse_progression_score(stop_points_a, geom_b)
        reverse_progress_b = reverse_progression_score(stop_points_b, geom_a)
        reverse_corridor = self._avg([reverse_progress_a, reverse_progress_b])

        path_sim = path_similarity_score(geom_a, geom_b, good_m=80.0, bad_m=600.0)
        shape_opp = shape_direction_opposition_score(geom_a, geom_b)

        len_a = safe_float(a.get("geometry_length_m"))
        len_b = safe_float(b.get("geometry_length_m"))
        if len_a is None:
            len_a = polyline_length_m(geom_a) if len(geom_a) >= 2 else None
        if len_b is None:
            len_b = polyline_length_m(geom_b) if len(geom_b) >= 2 else None
        length_ratio = self._length_ratio_score(len_a, len_b)

        ref_match = text_similarity(a.get("route_ref") or a.get("relation_ref"), b.get("route_ref") or b.get("relation_ref"))
        operator_match = text_similarity(
            a.get("operator_name") or a.get("relation_operator"),
            b.get("operator_name") or b.get("relation_operator"),
        )
        network_match = text_similarity(a.get("network_name") or (a.get("relation_tags") or {}).get("network"), b.get("network_name") or (b.get("relation_tags") or {}).get("network"))
        overpass_name = text_similarity(a.get("relation_name"), b.get("relation_name"))

        from_to_swapped = self._from_to_swapped_similarity(a, b)
        relation_consistency = self._avg([ref_match, operator_match, network_match, overpass_name, from_to_swapped])

        route_name_sim = text_similarity(a.get("route_name") or a.get("relation_name"), b.get("route_name") or b.get("relation_name"))
        endpoint_name_swap = endpoint_swap_name_similarity(a.get("route_name") or a.get("relation_name"), b.get("route_name") or b.get("relation_name"))
        alias_match = self._alias_match_score(a.get("aliases") or [], b.get("aliases") or [])
        direction_conflict = direction_word_conflict_flag(a.get("route_name"), b.get("route_name"))
        family_name = name_family_match_score(a.get("route_name") or a.get("relation_name"), b.get("route_name") or b.get("relation_name"))

        penalties_a = self._route_penalties(a)
        penalties_b = self._route_penalties(b)

        loop_branch_penalty = self._avg(
            [
                penalties_a.get("loop_or_branch_suspicion_penalty"),
                penalties_b.get("loop_or_branch_suspicion_penalty"),
            ]
        )

        features: Dict[str, Any] = {
            "exact_stop_overlap_ratio": exact_overlap,
            "reverse_exact_sequence_similarity": reverse_exact,
            "paired_stop_alignment_score": paired_alignment,
            "reverse_order_of_paired_stops_score": reverse_order_pairs,
            "endpoint_region_swap_score": endpoint_swap,
            "shared_middle_corridor_alignment_score": shared_middle,
            "shared_corridor_overlap_score": shared_corridor,
            "reverse_corridor_progression_score": reverse_corridor,
            "path_similarity_score": path_sim,
            "shape_direction_opposition_score": shape_opp,
            "length_ratio_score": length_ratio,
            "ref_match_score": ref_match,
            "operator_match_score": operator_match,
            "network_match_score": network_match,
            "overpass_name_similarity_score": overpass_name,
            "from_to_swapped_match_score": from_to_swapped,
            "relation_tag_consistency_score": relation_consistency,
            "normalized_route_name_similarity": route_name_sim,
            "endpoint_name_swap_similarity": endpoint_name_swap,
            "alias_match_score": alias_match,
            "direction_word_conflict_flag": bool(direction_conflict),
            "name_family_match_score": family_name,
            "sequence_quality_penalty_a": penalties_a.get("sequence_quality_penalty"),
            "sequence_quality_penalty_b": penalties_b.get("sequence_quality_penalty"),
            "unmatched_penalty_a": penalties_a.get("unmatched_penalty"),
            "unmatched_penalty_b": penalties_b.get("unmatched_penalty"),
            "ambiguous_penalty_a": penalties_a.get("ambiguous_penalty"),
            "ambiguous_penalty_b": penalties_b.get("ambiguous_penalty"),
            "loop_or_branch_suspicion_penalty": loop_branch_penalty,
            "low_evidence_coverage_penalty": None,
        }

        coverage_ratio = self._coverage_ratio(features)
        low_coverage_penalty = clip01(1.0 - coverage_ratio)
        features["low_evidence_coverage_penalty"] = low_coverage_penalty

        coverage = {
            "feature_coverage_ratio": round(coverage_ratio, 4),
            "available_feature_count": int(sum(1 for k, v in features.items() if k != "direction_word_conflict_flag" and v is not None)),
            "total_feature_count": int(sum(1 for k in features.keys() if k != "direction_word_conflict_flag")),
            "route_a_coverage": dict(a.get("coverage") or {}),
            "route_b_coverage": dict(b.get("coverage") or {}),
        }

        diagnostics = {
            "paired_stops": {
                "pair_count": int(len(pairings)),
                "pair_distance_avg_m": self._avg([p[2] for p in pairings]),
                "pair_distance_max_m": (max((p[2] for p in pairings), default=None)),
            },
            "geometry": {
                "route_a_length_m": len_a,
                "route_b_length_m": len_b,
                "route_a_source": a.get("geometry_source"),
                "route_b_source": b.get("geometry_source"),
            },
            "stop_sources": {
                "route_a": a.get("stop_source"),
                "route_b": b.get("stop_source"),
            },
        }

        return {
            "route_a_id": rid_a,
            "route_b_id": rid_b,
            "route_a_profile": a,
            "route_b_profile": b,
            "features": features,
            "coverage": coverage,
            "diagnostics": diagnostics,
            "requires_operator_confirmation": True,
        }

    @staticmethod
    def _normalize_uuid(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return str(uuid.UUID(raw))
        except Exception:
            return ""

    @staticmethod
    def _parse_uuid_array(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            out: List[str] = []
            for item in value:
                txt = RoutePairEvidenceExtractor._normalize_uuid(item)
                if txt:
                    out.append(txt)
            return out
        raw = str(value).strip()
        if not raw:
            return []
        if raw.startswith("{") and raw.endswith("}"):
            inner = raw[1:-1].strip()
            if not inner:
                return []
            out: List[str] = []
            for token in inner.split(","):
                txt = RoutePairEvidenceExtractor._normalize_uuid(token.strip().strip('"'))
                if txt:
                    out.append(txt)
            return out
        single = RoutePairEvidenceExtractor._normalize_uuid(raw)
        return [single] if single else []

    @staticmethod
    def _parse_int_array(value: Any) -> List[int]:
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            out: List[int] = []
            for item in value:
                try:
                    out.append(int(item))
                except Exception:
                    continue
            return out
        raw = str(value).strip()
        if not raw:
            return []
        if raw.startswith("{") and raw.endswith("}"):
            inner = raw[1:-1].strip()
            if not inner:
                return []
            out: List[int] = []
            for token in inner.split(","):
                try:
                    out.append(int(token.strip().strip('"')))
                except Exception:
                    continue
            return out
        try:
            return [int(raw)]
        except Exception:
            return []

    @staticmethod
    def _avg(values: Sequence[Optional[float]]) -> Optional[float]:
        vals = [float(v) for v in values if v is not None]
        if not vals:
            return None
        return float(sum(vals) / float(len(vals)))

    @staticmethod
    def _as_points(points: Any) -> List[LonLat]:
        out: List[LonLat] = []
        if not isinstance(points, list):
            return out
        for item in points:
            if isinstance(item, dict):
                lon = safe_float(item.get("lon"))
                lat = safe_float(item.get("lat"))
                if lon is None or lat is None:
                    continue
                out.append((lon, lat))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                lon = safe_float(item[0])
                lat = safe_float(item[1])
                if lon is None or lat is None:
                    continue
                out.append((lon, lat))
        return out

    def _query_one(self, conn: Any, sql: str, params: Tuple[Any, ...]) -> Dict[str, Any]:
        with db_cursor(conn) as cur:
            try:
                cur.execute(sql, params)
                return dict(cur.fetchone() or {})
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return {}

    def _query_all(self, conn: Any, sql: str, params: Tuple[Any, ...]) -> List[Dict[str, Any]]:
        with db_cursor(conn) as cur:
            try:
                cur.execute(sql, params)
                rows = cur.fetchall() or []
                return [dict(r) for r in rows]
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                return []

    def _load_base_context(self, conn: Any, route_id: str) -> Dict[str, Any]:
        base = self._query_one(
            conn,
            """
            SELECT
              rj.route_id::text AS route_id,
              rj.service_route_id::text AS service_route_id,
              rj.direction_id::int AS direction_id,
              COALESCE(rj.known_ref, '') AS known_ref,
              COALESCE(sr.route_ref, '') AS service_route_ref,
              COALESCE(sr.route_name, '') AS service_route_name,
              COALESCE(sr.operator_name, '') AS service_operator_name,
              rj.chosen_osm_relation_id
            FROM route_raw.route_jobs rj
            LEFT JOIN route_raw.service_routes sr
              ON sr.service_route_id = rj.service_route_id
            WHERE rj.route_id = %s::uuid
            LIMIT 1
            """,
            (route_id,),
        )

        sem = self._query_one(
            conn,
            """
            SELECT
              COALESCE(route_name, '') AS route_name,
              COALESCE(route_ref, '') AS route_ref,
              COALESCE(operator_name, '') AS operator_name,
              COALESCE(route_aliases, ARRAY[]::text[]) AS route_aliases,
              COALESCE(direction_semantics, '{}'::jsonb) AS direction_semantics
            FROM route_prod.route_semantics
            WHERE route_id = %s::uuid
            LIMIT 1
            """,
            (route_id,),
        )

        route_name = str(sem.get("route_name") or "").strip() or str(base.get("service_route_name") or "").strip()
        route_ref = str(sem.get("route_ref") or "").strip() or str(base.get("service_route_ref") or "").strip() or str(base.get("known_ref") or "").strip()
        operator_name = str(sem.get("operator_name") or "").strip() or str(base.get("service_operator_name") or "").strip()

        return {
            "route_id": route_id,
            "service_route_id": base.get("service_route_id"),
            "direction_id": base.get("direction_id"),
            "route_name": route_name,
            "route_ref": route_ref,
            "operator_name": operator_name,
            "aliases": [str(x).strip() for x in (sem.get("route_aliases") or []) if str(x).strip()],
            "direction_semantics": dict(sem.get("direction_semantics") or {}),
            "chosen_osm_relation_id": base.get("chosen_osm_relation_id"),
        }

    def _load_relation_context(self, conn: Any, route_id: str, *, chosen_relation_id: Any = None) -> Dict[str, Any]:
        rc = self._query_one(
            conn,
            """
            SELECT
              rc.ref,
              rc.name,
              rc.operator,
              rc.tags,
              rc.osm_relation_id
            FROM route_raw.relation_candidates rc
            WHERE rc.route_id = %s::uuid
            ORDER BY
              CASE
                WHEN %s::bigint IS NOT NULL AND rc.osm_relation_id = %s::bigint THEN 0
                ELSE 1
              END,
              rc.found_at DESC
            LIMIT 1
            """,
            (route_id, chosen_relation_id, chosen_relation_id),
        )

        raw = self._query_one(
            conn,
            """
            SELECT
              osm_relation_id,
              overpass_json
            FROM route_raw.osm_relations_raw
            WHERE route_id = %s::uuid
            ORDER BY fetched_at DESC NULLS LAST
            LIMIT 1
            """,
            (route_id,),
        )

        tags: Dict[str, Any] = {}
        if isinstance(rc.get("tags"), dict):
            tags.update(dict(rc.get("tags") or {}))
        rel_tags = self._extract_relation_tags_from_overpass(raw.get("overpass_json"))
        tags.update({k: v for k, v in rel_tags.items() if v not in (None, "")})

        relation_name = str(tags.get("name") or rc.get("name") or "").strip()
        relation_ref = str(tags.get("ref") or rc.get("ref") or "").strip()
        relation_operator = str(tags.get("operator") or rc.get("operator") or "").strip()
        relation_network = str(tags.get("network") or "").strip()

        relation_from = str(tags.get("from") or "").strip()
        relation_to = str(tags.get("to") or "").strip()

        return {
            "relation_tags": tags,
            "relation_name": relation_name,
            "relation_ref": relation_ref,
            "relation_operator": relation_operator,
            "relation_network": relation_network,
            "relation_from": relation_from,
            "relation_to": relation_to,
            "network_name": relation_network,
            "chosen_osm_relation_id": raw.get("osm_relation_id") or chosen_relation_id,
        }

    def _load_prior_rows(self, conn: Any, route_id: str) -> List[Dict[str, Any]]:
        rows = self._query_all(
            conn,
            """
            SELECT
              seq,
              role,
              lat,
              lon,
              matched_stop_node_id::text AS matched_stop_node_id,
              match_dist_m
            FROM route_work.relation_stop_prior
            WHERE route_id = %s::uuid
            ORDER BY seq ASC
            """,
            (route_id,),
        )

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "seq": int(r.get("seq") or 0),
                    "role": r.get("role"),
                    "lat": safe_float(r.get("lat")) or 0.0,
                    "lon": safe_float(r.get("lon")) or 0.0,
                    "matched_stop_node_id": str(r.get("matched_stop_node_id") or "").strip() or None,
                    "match_dist_m": safe_float(r.get("match_dist_m")),
                    "match_state": "matched" if str(r.get("matched_stop_node_id") or "").strip() else "unmatched",
                }
            )
        return out

    def _load_stop_context(self, conn: Any, route_id: str) -> Dict[str, Any]:
        chosen = self._query_one(
            conn,
            """
            SELECT ra.chosen_stop_sequence_candidate_id::text AS candidate_id
            FROM route_work.route_approvals ra
            WHERE ra.route_id = %s::uuid
            LIMIT 1
            """,
            (route_id,),
        )
        candidate_id = str(chosen.get("candidate_id") or "").strip()
        if not candidate_id:
            top = self._query_one(
                conn,
                """
                SELECT ssc.candidate_id::text AS candidate_id
                FROM route_work.stop_sequence_candidate_sets scs
                JOIN route_work.stop_sequence_candidates ssc
                  ON ssc.set_id = scs.set_id
                WHERE scs.route_id = %s::uuid
                ORDER BY scs.created_at DESC, COALESCE(ssc.rank, 999999) ASC, ssc.created_at DESC
                LIMIT 1
                """,
                (route_id,),
            )
            candidate_id = str(top.get("candidate_id") or "").strip()

        if candidate_id:
            seq_row = self._query_one(
                conn,
                """
                SELECT stop_node_ids, stop_prior_seqs
                FROM route_work.stop_sequence_candidates
                WHERE candidate_id = %s::uuid
                LIMIT 1
                """,
                (candidate_id,),
            )
            node_ids = self._parse_uuid_array(seq_row.get("stop_node_ids"))
            prior_seqs = self._parse_int_array(seq_row.get("stop_prior_seqs"))
            if node_ids:
                return self._load_points_from_node_ids(conn, node_ids)
            if prior_seqs:
                return self._load_points_from_prior(conn, route_id, prior_seqs)

        return self._load_points_from_prior(conn, route_id, [])

    def _load_geometry_context(self, conn: Any, route_id: str) -> Dict[str, Any]:
        prod = self._query_one(
            conn,
            """
            SELECT
              ST_AsText(geom) AS geom_wkt,
              ST_Length(geom::geography) AS length_m
            FROM route_prod.routes
            WHERE route_id = %s::uuid
            LIMIT 1
            """,
            (route_id,),
        )
        if prod.get("geom_wkt"):
            pts = parse_linestring_wkt(prod.get("geom_wkt"))
            return {
                "points": [{"lon": p[0], "lat": p[1]} for p in pts],
                "length_m": safe_float(prod.get("length_m")),
                "source": "route_prod",
            }

        appr = self._query_one(
            conn,
            """
            SELECT
              ST_AsText(gc.geom) AS geom_wkt,
              ST_Length(gc.geom::geography) AS length_m
            FROM route_work.route_approvals ra
            JOIN route_work.geometry_candidates gc
              ON gc.geometry_candidate_id = ra.chosen_geometry_candidate_id
            WHERE ra.route_id = %s::uuid
            LIMIT 1
            """,
            (route_id,),
        )
        if appr.get("geom_wkt"):
            pts = parse_linestring_wkt(appr.get("geom_wkt"))
            return {
                "points": [{"lon": p[0], "lat": p[1]} for p in pts],
                "length_m": safe_float(appr.get("length_m")),
                "source": "approved_candidate",
            }

        cand = self._query_one(
            conn,
            """
            SELECT
              ST_AsText(gc.geom) AS geom_wkt,
              ST_Length(gc.geom::geography) AS length_m
            FROM route_work.geometry_candidate_sets gcs
            JOIN route_work.geometry_candidates gc
              ON gc.set_id = gcs.set_id
            WHERE gcs.route_id = %s::uuid
            ORDER BY gcs.created_at DESC, COALESCE(gc.score, 0.0) DESC, gc.created_at DESC
            LIMIT 1
            """,
            (route_id,),
        )
        if cand.get("geom_wkt"):
            pts = parse_linestring_wkt(cand.get("geom_wkt"))
            return {
                "points": [{"lon": p[0], "lat": p[1]} for p in pts],
                "length_m": safe_float(cand.get("length_m")),
                "source": "latest_candidate",
            }

        return {"points": [], "length_m": None, "source": "none"}

    def _load_points_from_node_ids(self, conn: Any, node_ids: List[str]) -> Dict[str, Any]:
        rows = self._query_all(
            conn,
            """
            SELECT
              u.ord::int AS ord,
              u.node_id::text AS node_id,
              ST_X(n.geom) AS lon,
              ST_Y(n.geom) AS lat,
              COALESCE(NULLIF(BTRIM(nn.canonical_name), ''), n.name, n.ref, '') AS stop_name
            FROM unnest(%s::uuid[]) WITH ORDINALITY AS u(node_id, ord)
            LEFT JOIN node_prod.nodes n
              ON n.node_id = u.node_id
            LEFT JOIN LATERAL (
              SELECT p.canonical_name
              FROM geo_prod.node_place_map m
              JOIN geo_prod.places p
                ON p.place_id = m.place_id
              WHERE m.node_id = n.node_id
                AND p.status = 'active'
              ORDER BY p.updated_at DESC NULLS LAST
              LIMIT 1
            ) nn ON TRUE
            ORDER BY u.ord ASC
            """,
            (node_ids,),
        )

        points: List[Dict[str, Any]] = []
        stop_ids: List[str] = []
        stop_names: List[str] = []
        for idx, r in enumerate(rows, start=1):
            lon = safe_float(r.get("lon"))
            lat = safe_float(r.get("lat"))
            if lon is None or lat is None:
                continue
            sid = str(r.get("node_id") or "").strip()
            sname = str(r.get("stop_name") or "").strip()
            points.append({"i": idx, "lon": lon, "lat": lat, "stop_id": sid, "stop_name": sname, "source": "canonical"})
            if sid:
                stop_ids.append(sid)
            if sname:
                stop_names.append(sname)

        return {
            "points": points,
            "stop_ids": stop_ids,
            "stop_names": stop_names,
            "source": "canonical_stop_node_ids" if points else "none",
        }

    def _load_points_from_prior(self, conn: Any, route_id: str, prior_seqs: List[int]) -> Dict[str, Any]:
        if prior_seqs:
            rows = self._query_all(
                conn,
                """
                SELECT
                  rsp.seq,
                  rsp.lon,
                  rsp.lat,
                  rsp.matched_stop_node_id::text AS matched_stop_node_id,
                  COALESCE(NULLIF(BTRIM(nn.canonical_name), ''), n.name, n.ref, '') AS stop_name
                FROM route_work.relation_stop_prior rsp
                LEFT JOIN node_prod.nodes n
                  ON n.node_id = rsp.matched_stop_node_id
                LEFT JOIN LATERAL (
                  SELECT p.canonical_name
                  FROM geo_prod.node_place_map m
                  JOIN geo_prod.places p
                    ON p.place_id = m.place_id
                  WHERE m.node_id = n.node_id
                    AND p.status = 'active'
                  ORDER BY p.updated_at DESC NULLS LAST
                  LIMIT 1
                ) nn ON TRUE
                WHERE rsp.route_id = %s::uuid
                  AND rsp.seq = ANY(%s::int[])
                ORDER BY array_position(%s::int[], rsp.seq)
                """,
                (route_id, prior_seqs, prior_seqs),
            )
        else:
            rows = self._query_all(
                conn,
                """
                SELECT
                  rsp.seq,
                  rsp.lon,
                  rsp.lat,
                  rsp.matched_stop_node_id::text AS matched_stop_node_id,
                  COALESCE(NULLIF(BTRIM(nn.canonical_name), ''), n.name, n.ref, '') AS stop_name
                FROM route_work.relation_stop_prior rsp
                LEFT JOIN node_prod.nodes n
                  ON n.node_id = rsp.matched_stop_node_id
                LEFT JOIN LATERAL (
                  SELECT p.canonical_name
                  FROM geo_prod.node_place_map m
                  JOIN geo_prod.places p
                    ON p.place_id = m.place_id
                  WHERE m.node_id = n.node_id
                    AND p.status = 'active'
                  ORDER BY p.updated_at DESC NULLS LAST
                  LIMIT 1
                ) nn ON TRUE
                WHERE rsp.route_id = %s::uuid
                ORDER BY rsp.seq ASC
                """,
                (route_id,),
            )

        points: List[Dict[str, Any]] = []
        stop_ids: List[str] = []
        stop_names: List[str] = []
        for idx, r in enumerate(rows, start=1):
            lon = safe_float(r.get("lon"))
            lat = safe_float(r.get("lat"))
            if lon is None or lat is None:
                continue
            sid = str(r.get("matched_stop_node_id") or "").strip()
            sname = str(r.get("stop_name") or "").strip()
            points.append({"i": idx, "lon": lon, "lat": lat, "stop_id": sid, "stop_name": sname, "source": "prior"})
            if sid:
                stop_ids.append(sid)
            if sname:
                stop_names.append(sname)

        return {
            "points": points,
            "stop_ids": stop_ids,
            "stop_names": stop_names,
            "source": "relation_stop_prior" if points else "none",
        }

    @staticmethod
    def _extract_relation_tags_from_overpass(overpass_json: Any) -> Dict[str, Any]:
        if not isinstance(overpass_json, dict):
            return {}
        elements = overpass_json.get("elements")
        if not isinstance(elements, list):
            return {}
        for el in elements:
            if not isinstance(el, dict):
                continue
            if str(el.get("type") or "") != "relation":
                continue
            tags = el.get("tags")
            if isinstance(tags, dict):
                return dict(tags)
        return {}

    @staticmethod
    def _exact_overlap_ratio(stop_ids_a: List[str], stop_ids_b: List[str]) -> Optional[float]:
        a = [x for x in stop_ids_a if x]
        b = [x for x in stop_ids_b if x]
        if not a or not b:
            return None
        inter = len(set(a).intersection(set(b)))
        denom = float(max(1, min(len(set(a)), len(set(b)))))
        return clip01(float(inter) / denom)

    @staticmethod
    def _paired_alignment_score(
        pairings: Sequence[Tuple[int, int, float]],
        *,
        len_a: int,
        len_b: int,
        max_distance_m: float,
    ) -> Optional[float]:
        if len_a <= 0 or len_b <= 0:
            return None
        p = list(pairings or [])
        if not p:
            return 0.0
        coverage = float(len(p)) / float(max(1, min(len_a, len_b)))
        avg_dist = float(sum(pp[2] for pp in p) / float(len(p)))
        dist_score = score_from_distance(avg_dist, good_m=35.0, bad_m=max_distance_m)
        if dist_score is None:
            return None
        return clip01(float(coverage) * float(dist_score))

    @staticmethod
    def _shared_middle_alignment(points_a: List[LonLat], points_b: List[LonLat]) -> Optional[float]:
        if len(points_a) < 2 or len(points_b) < 2:
            return None
        mid_a = points_a[len(points_a) // 2]
        mid_b = points_b[len(points_b) // 2]
        dist = haversine_m(mid_a, mid_b)
        return score_from_distance(dist, good_m=60.0, bad_m=1000.0)

    @staticmethod
    def _length_ratio_score(length_a: Optional[float], length_b: Optional[float]) -> Optional[float]:
        la = safe_float(length_a)
        lb = safe_float(length_b)
        if la is None or lb is None or la <= 0.0 or lb <= 0.0:
            return None
        return clip01(min(la, lb) / max(la, lb))

    @staticmethod
    def _alias_match_score(aliases_a: Sequence[str], aliases_b: Sequence[str]) -> Optional[float]:
        left = [str(x).strip() for x in (aliases_a or []) if str(x).strip()]
        right = [str(x).strip() for x in (aliases_b or []) if str(x).strip()]
        if not left or not right:
            return None
        best: Optional[float] = None
        for a in left:
            for b in right:
                sim = text_similarity(a, b)
                if sim is None:
                    continue
                if best is None or sim > best:
                    best = float(sim)
        return best

    @staticmethod
    def _from_to_swapped_similarity(profile_a: Dict[str, Any], profile_b: Dict[str, Any]) -> Optional[float]:
        a_from = str(profile_a.get("relation_from") or "").strip()
        a_to = str(profile_a.get("relation_to") or "").strip()
        b_from = str(profile_b.get("relation_from") or "").strip()
        b_to = str(profile_b.get("relation_to") or "").strip()

        if not a_from and not a_to:
            ds = dict(profile_a.get("direction_semantics") or {})
            a_from = str(ds.get("from") or ds.get("origin") or "").strip()
            a_to = str(ds.get("to") or ds.get("destination") or "").strip()
        if not b_from and not b_to:
            ds = dict(profile_b.get("direction_semantics") or {})
            b_from = str(ds.get("from") or ds.get("origin") or "").strip()
            b_to = str(ds.get("to") or ds.get("destination") or "").strip()

        if not all([a_from, a_to, b_from, b_to]):
            return None

        swapped = RoutePairEvidenceExtractor._avg([text_similarity(a_from, b_to), text_similarity(a_to, b_from)])
        same = RoutePairEvidenceExtractor._avg([text_similarity(a_from, b_from), text_similarity(a_to, b_to)])
        if swapped is None or same is None:
            return None
        return clip01(0.5 + 0.5 * (float(swapped) - float(same)))

    @staticmethod
    def _route_penalties(profile: Dict[str, Any]) -> Dict[str, Optional[float]]:
        seq = dict(profile.get("sequence_quality") or {})
        details = dict(seq.get("details") or {})

        seq_score = safe_float(seq.get("sequence_quality_score"))
        seq_penalty = (clip01(1.0 - (seq_score / 100.0)) if seq_score is not None else None)

        total_stops = int(details.get("total_stops") or 0)
        unmatched = int(details.get("unmatched_count") or 0)
        ambiguous = int(details.get("ambiguous_count") or 0)

        unmatched_penalty = (clip01(float(unmatched) / float(total_stops)) if total_stops > 0 else None)
        ambiguous_penalty = (clip01(float(ambiguous) / float(total_stops)) if total_stops > 0 else None)

        stop_ids = [str(x) for x in (profile.get("stop_ids") or []) if str(x)]
        repeated = 0
        seen: set[str] = set()
        for sid in stop_ids:
            if sid in seen:
                repeated += 1
            else:
                seen.add(sid)
        repeated_ratio = (float(repeated) / float(max(1, len(stop_ids)))) if stop_ids else 0.0

        geom_pts: List[LonLat] = []
        for p in profile.get("geometry_points") or []:
            if isinstance(p, dict):
                lon = safe_float(p.get("lon"))
                lat = safe_float(p.get("lat"))
                if lon is not None and lat is not None:
                    geom_pts.append((lon, lat))
        loop_pen = route_loop_suspicion(geom_pts)

        suspicion = clip01(max(loop_pen or 0.0, repeated_ratio))

        return {
            "sequence_quality_penalty": seq_penalty,
            "unmatched_penalty": unmatched_penalty,
            "ambiguous_penalty": ambiguous_penalty,
            "loop_or_branch_suspicion_penalty": suspicion,
        }

    @staticmethod
    def _coverage_ratio(features: Dict[str, Any]) -> float:
        keys = [k for k in features.keys() if k != "direction_word_conflict_flag"]
        if not keys:
            return 0.0
        have = 0
        for key in keys:
            if features.get(key) is not None:
                have += 1
        return clip01(float(have) / float(len(keys)))
