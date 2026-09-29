from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from phase3_routes.services.route_constructor.src.db.conn import db_cursor

DEFAULT_SCAN_RADIUS_M = 25.0
DEFAULT_RECOVERY_RADIUS_M = 12.0
DEFAULT_NEARBY_LIMIT = 256
DUPLICATE_STOP_GUARD_M = 12.0
MIN_SLOT_GAP_M = 70.0
RECOVERY_STRAIGHTNESS_RATIO = 1.12
AMBIGUOUS_STRAIGHTNESS_RATIO = 1.25
ANCHOR_BOUNDARY_BUFFER_RATIO = 0.08
CLEAR_WIN_MARGIN = 0.55
CLOSE_COMPETITOR_MARGIN = 0.20
COMPETITOR_PROGRESS_GAP = 0.025


def ensure_geometry_stop_recovery_schema(conn: Any) -> None:
    sql_path = Path(__file__).resolve().parents[2] / "sql" / "027_geometry_stop_recovery.sql"
    if not sql_path.exists():
        return
    sql = sql_path.read_text(encoding="utf-8")
    with db_cursor(conn) as cur:
        cur.execute(sql)


def _to_uuid_text(value: Any) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except Exception:
        return None


def _uuid_text_list(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        txt = _to_uuid_text(raw)
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def _int_list(values: Iterable[Any]) -> List[int]:
    out: List[int] = []
    seen: set[int] = set()
    for raw in list(values or []):
        try:
            value = int(raw)
        except Exception:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _haversine_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    r = 6371000.0
    phi_a = math.radians(lat_a)
    phi_b = math.radians(lat_b)
    d_phi = math.radians(lat_b - lat_a)
    d_lam = math.radians(lon_b - lon_a)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lam / 2.0) ** 2
    return 2.0 * r * math.asin(math.sqrt(a))


def _clamp_progress(value: Any) -> float:
    return max(0.0, min(1.0, _coerce_float(value, 0.0)))


def _sorted_stop_ids(rows: List[Dict[str, Any]]) -> List[str]:
    return [str(row.get("stop_id") or "") for row in rows if str(row.get("stop_id") or "").strip()]


def _serialize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "stop_id": row.get("stop_id"),
                "name": row.get("name"),
                "ref": row.get("ref"),
                "slot_index": row.get("slot_index"),
                "progress": row.get("progress"),
                "dist_to_geometry_m": row.get("dist_to_geometry_m"),
                "nearest_original_stop_m": row.get("nearest_original_stop_m"),
                "straightness_ratio": row.get("straightness_ratio"),
                "confidence_score": row.get("confidence_score"),
                "decision_reason": row.get("decision_reason"),
                "classification": row.get("classification"),
                "insert_after_stop_id": row.get("insert_after_stop_id"),
                "insert_before_stop_id": row.get("insert_before_stop_id"),
            }
        )
    return out


def build_geometry_stop_recovery_result(
    candidate_context: Dict[str, Any],
    *,
    original_stops: List[Dict[str, Any]],
    nearby_stops: List[Dict[str, Any]],
    scan_radius_m: float = DEFAULT_SCAN_RADIUS_M,
    recovery_radius_m: float = DEFAULT_RECOVERY_RADIUS_M,
) -> Dict[str, Any]:
    route_id = str(candidate_context.get("route_id") or "")
    geometry_candidate_id = str(candidate_context.get("geometry_candidate_id") or "")
    set_id = str(candidate_context.get("set_id") or "")
    stop_sequence_candidate_id = _to_uuid_text(candidate_context.get("stop_sequence_candidate_id"))
    length_m = max(_coerce_float(candidate_context.get("length_m"), 0.0), 0.0)

    anchors: List[Dict[str, Any]] = []
    monotonic_progress = -1.0
    for index, raw in enumerate(list(original_stops or []), start=1):
        stop_id = _to_uuid_text(raw.get("stop_id"))
        if not stop_id:
            continue
        progress = _clamp_progress(raw.get("progress"))
        if progress < monotonic_progress:
            progress = monotonic_progress
        monotonic_progress = progress
        anchors.append(
            {
                "stop_id": stop_id,
                "name": raw.get("name"),
                "ref": raw.get("ref"),
                "order_index": int(raw.get("order_index") or index),
                "lat": _coerce_float(raw.get("lat")),
                "lon": _coerce_float(raw.get("lon")),
                "progress": progress,
                "dist_to_geometry_m": _coerce_float(raw.get("dist_to_geometry_m")),
            }
        )

    rejected: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    recovered: List[Dict[str, Any]] = []

    if len(anchors) < 2:
        for raw in list(nearby_stops or []):
            stop_id = _to_uuid_text(raw.get("stop_id"))
            if not stop_id:
                continue
            rejected.append(
                {
                    "stop_id": stop_id,
                    "name": raw.get("name"),
                    "ref": raw.get("ref"),
                    "classification": "rejected",
                    "decision_reason": "insufficient_sequence_anchors",
                    "progress": _clamp_progress(raw.get("progress")),
                    "dist_to_geometry_m": _coerce_float(raw.get("dist_to_geometry_m")),
                    "nearest_original_stop_m": None,
                    "straightness_ratio": None,
                    "confidence_score": 0.0,
                    "slot_index": None,
                    "insert_after_stop_id": None,
                    "insert_before_stop_id": None,
                }
            )
        return _finalize_geometry_stop_recovery_result(
            candidate_context,
            anchors=anchors,
            recovered=recovered,
            ambiguous=ambiguous,
            rejected=rejected,
            scan_radius_m=scan_radius_m,
            recovery_radius_m=recovery_radius_m,
            length_m=length_m,
        )

    grouped_candidates: Dict[int, List[Dict[str, Any]]] = {}
    for raw in list(nearby_stops or []):
        stop_id = _to_uuid_text(raw.get("stop_id"))
        if not stop_id:
            continue

        candidate = {
            "stop_id": stop_id,
            "name": raw.get("name"),
            "ref": raw.get("ref"),
            "lat": _coerce_float(raw.get("lat")),
            "lon": _coerce_float(raw.get("lon")),
            "progress": _clamp_progress(raw.get("progress")),
            "dist_to_geometry_m": _coerce_float(raw.get("dist_to_geometry_m")),
            "closest_point_wkt": raw.get("closest_point_wkt"),
            "classification": "rejected",
            "decision_reason": "unclassified",
            "slot_index": None,
            "nearest_original_stop_m": None,
            "straightness_ratio": None,
            "confidence_score": 0.0,
            "insert_after_stop_id": None,
            "insert_before_stop_id": None,
        }

        nearest_anchor_m = min(
            _haversine_m(anchor["lon"], anchor["lat"], candidate["lon"], candidate["lat"])
            for anchor in anchors
        )
        candidate["nearest_original_stop_m"] = round(nearest_anchor_m, 2)
        if nearest_anchor_m <= DUPLICATE_STOP_GUARD_M:
            candidate["decision_reason"] = "duplicate_near_existing_stop"
            rejected.append(candidate)
            continue

        slot_index: Optional[int] = None
        prev_anchor: Optional[Dict[str, Any]] = None
        next_anchor: Optional[Dict[str, Any]] = None
        for idx in range(len(anchors) - 1):
            left = anchors[idx]
            right = anchors[idx + 1]
            if left["progress"] <= candidate["progress"] <= right["progress"]:
                slot_index = idx
                prev_anchor = left
                next_anchor = right
                break
        if slot_index is None or prev_anchor is None or next_anchor is None:
            candidate["decision_reason"] = "outside_anchor_progress_window"
            rejected.append(candidate)
            continue

        gap_progress = max(next_anchor["progress"] - prev_anchor["progress"], 0.0)
        gap_length_m = gap_progress * length_m if length_m > 0 else 0.0
        anchor_span_m = _haversine_m(prev_anchor["lon"], prev_anchor["lat"], next_anchor["lon"], next_anchor["lat"])
        effective_gap_m = max(gap_length_m, anchor_span_m)
        if effective_gap_m < MIN_SLOT_GAP_M:
            candidate["slot_index"] = slot_index
            candidate["insert_after_stop_id"] = prev_anchor["stop_id"]
            candidate["insert_before_stop_id"] = next_anchor["stop_id"]
            candidate["decision_reason"] = "anchor_gap_too_small"
            rejected.append(candidate)
            continue

        within_slot = 0.5
        if gap_progress > 0:
            within_slot = (candidate["progress"] - prev_anchor["progress"]) / gap_progress
        boundary_buffer = ANCHOR_BOUNDARY_BUFFER_RATIO
        if within_slot <= boundary_buffer or within_slot >= (1.0 - boundary_buffer):
            candidate["slot_index"] = slot_index
            candidate["insert_after_stop_id"] = prev_anchor["stop_id"]
            candidate["insert_before_stop_id"] = next_anchor["stop_id"]
            candidate["decision_reason"] = "too_close_to_anchor_boundary"
            rejected.append(candidate)
            continue

        left_m = _haversine_m(prev_anchor["lon"], prev_anchor["lat"], candidate["lon"], candidate["lat"])
        right_m = _haversine_m(candidate["lon"], candidate["lat"], next_anchor["lon"], next_anchor["lat"])
        denom = max(anchor_span_m, 1.0)
        straightness_ratio = (left_m + right_m) / denom
        candidate["straightness_ratio"] = round(straightness_ratio, 4)
        candidate["slot_index"] = slot_index
        candidate["insert_after_stop_id"] = prev_anchor["stop_id"]
        candidate["insert_before_stop_id"] = next_anchor["stop_id"]

        confidence = 0.0
        confidence += max(0.0, recovery_radius_m - candidate["dist_to_geometry_m"]) / max(recovery_radius_m, 1.0)
        confidence += max(0.0, RECOVERY_STRAIGHTNESS_RATIO - straightness_ratio) * 3.0
        confidence += min(within_slot, 1.0 - within_slot)
        confidence += min(candidate["nearest_original_stop_m"] / 40.0, 1.0)
        candidate["confidence_score"] = round(confidence, 4)

        if candidate["dist_to_geometry_m"] <= recovery_radius_m and straightness_ratio <= RECOVERY_STRAIGHTNESS_RATIO:
            candidate["classification"] = "recoverable"
            candidate["decision_reason"] = "geometry_and_slot_consistent"
            grouped_candidates.setdefault(slot_index, []).append(candidate)
            continue

        if candidate["dist_to_geometry_m"] <= scan_radius_m and straightness_ratio <= AMBIGUOUS_STRAIGHTNESS_RATIO:
            candidate["classification"] = "ambiguous"
            candidate["decision_reason"] = "near_geometry_but_not_clear_enough"
            grouped_candidates.setdefault(slot_index, []).append(candidate)
            continue

        candidate["decision_reason"] = "corridor_or_progress_mismatch"
        rejected.append(candidate)

    for slot_index, slot_candidates in sorted(grouped_candidates.items(), key=lambda item: item[0]):
        slot_candidates.sort(
            key=lambda row: (
                0 if row.get("classification") == "recoverable" else 1,
                -_coerce_float(row.get("confidence_score"), 0.0),
                _coerce_float(row.get("dist_to_geometry_m"), 1e9),
                str(row.get("stop_id") or ""),
            )
        )
        best = slot_candidates[0]
        second = slot_candidates[1] if len(slot_candidates) > 1 else None
        score_gap = _coerce_float(best.get("confidence_score"), 0.0) - _coerce_float(
            second.get("confidence_score") if second else 0.0, 0.0
        )
        progress_gap = abs(_coerce_float(best.get("progress")) - _coerce_float(second.get("progress"))) if second else 1.0
        clear_winner = bool(
            best.get("classification") == "recoverable"
            and (
                second is None
                or (
                    second.get("classification") != "recoverable"
                    and score_gap >= CLOSE_COMPETITOR_MARGIN
                )
                or (
                    second.get("classification") == "recoverable"
                    and score_gap >= CLEAR_WIN_MARGIN
                    and progress_gap >= COMPETITOR_PROGRESS_GAP
                )
            )
        )

        if clear_winner:
            best["classification"] = "recovered"
            best["decision_reason"] = "clear_slot_winner"
            recovered.append(best)
            for other in slot_candidates[1:]:
                score_delta = _coerce_float(best.get("confidence_score"), 0.0) - _coerce_float(
                    other.get("confidence_score"), 0.0
                )
                if score_delta < CLEAR_WIN_MARGIN:
                    other["classification"] = "ambiguous"
                    other["decision_reason"] = "competing_slot_candidate"
                    ambiguous.append(other)
                else:
                    other["classification"] = "rejected"
                    other["decision_reason"] = "slot_competition_lost"
                    rejected.append(other)
            continue

        for row in slot_candidates:
            row["classification"] = "ambiguous"
            row["decision_reason"] = "slot_not_unique"
            ambiguous.append(row)

    return _finalize_geometry_stop_recovery_result(
        candidate_context,
        anchors=anchors,
        recovered=recovered,
        ambiguous=ambiguous,
        rejected=rejected,
        scan_radius_m=scan_radius_m,
        recovery_radius_m=recovery_radius_m,
        length_m=length_m,
    )


def _finalize_geometry_stop_recovery_result(
    candidate_context: Dict[str, Any],
    *,
    anchors: List[Dict[str, Any]],
    recovered: List[Dict[str, Any]],
    ambiguous: List[Dict[str, Any]],
    rejected: List[Dict[str, Any]],
    scan_radius_m: float,
    recovery_radius_m: float,
    length_m: float,
) -> Dict[str, Any]:
    geometry_candidate_id = str(candidate_context.get("geometry_candidate_id") or "")
    route_id = str(candidate_context.get("route_id") or "")
    set_id = str(candidate_context.get("set_id") or "")
    stop_sequence_candidate_id = _to_uuid_text(candidate_context.get("stop_sequence_candidate_id"))
    original_stop_ids = _sorted_stop_ids(anchors)
    recovered.sort(key=lambda row: (int(row.get("slot_index") or -1), _coerce_float(row.get("progress"), 0.0), str(row.get("stop_id") or "")))
    ambiguous.sort(key=lambda row: (int(row.get("slot_index") or -1), _coerce_float(row.get("progress"), 0.0), str(row.get("stop_id") or "")))
    rejected.sort(key=lambda row: (str(row.get("decision_reason") or ""), int(row.get("slot_index") or -1), _coerce_float(row.get("progress"), 0.0), str(row.get("stop_id") or "")))

    recovered_by_slot: Dict[int, List[Dict[str, Any]]] = {}
    for row in recovered:
        recovered_by_slot.setdefault(int(row.get("slot_index") or 0), []).append(row)

    enriched_stop_ids: List[str] = []
    insertion_proposals: List[Dict[str, Any]] = []
    for idx, anchor in enumerate(anchors):
        enriched_stop_ids.append(anchor["stop_id"])
        for row in recovered_by_slot.get(idx, []):
            enriched_stop_ids.append(str(row.get("stop_id") or ""))
            insertion_proposals.append(
                {
                    "slot_index": int(row.get("slot_index") or 0),
                    "status": "recovered",
                    "proposed_stop_id": row.get("stop_id"),
                    "insert_after_stop_id": row.get("insert_after_stop_id"),
                    "insert_before_stop_id": row.get("insert_before_stop_id"),
                    "progress": row.get("progress"),
                    "dist_to_geometry_m": row.get("dist_to_geometry_m"),
                    "straightness_ratio": row.get("straightness_ratio"),
                    "decision_reason": row.get("decision_reason"),
                }
            )

    for row in ambiguous:
        insertion_proposals.append(
            {
                "slot_index": row.get("slot_index"),
                "status": "ambiguous",
                "proposed_stop_id": row.get("stop_id"),
                "insert_after_stop_id": row.get("insert_after_stop_id"),
                "insert_before_stop_id": row.get("insert_before_stop_id"),
                "progress": row.get("progress"),
                "dist_to_geometry_m": row.get("dist_to_geometry_m"),
                "straightness_ratio": row.get("straightness_ratio"),
                "decision_reason": row.get("decision_reason"),
            }
        )

    recovered_stop_ids = _sorted_stop_ids(recovered)
    ambiguous_stop_ids = _sorted_stop_ids(ambiguous)
    rejected_stop_ids = _sorted_stop_ids(rejected)
    enriched_stop_ids = _uuid_text_list(enriched_stop_ids)

    summary_metrics = {
        "length_m": round(length_m, 2),
        "scan_radius_m": float(scan_radius_m),
        "recovery_radius_m": float(recovery_radius_m),
        "nearby_stops_scanned": int(len(recovered) + len(ambiguous) + len(rejected)),
        "recovered_count": int(len(recovered_stop_ids)),
        "ambiguous_count": int(len(ambiguous_stop_ids)),
        "rejected_count": int(len(rejected_stop_ids)),
        "original_stop_count": int(len(original_stop_ids)),
        "enriched_stop_count": int(len(enriched_stop_ids)),
    }

    provenance = {
        "original_extracted_stop_prior_seqs": _int_list(candidate_context.get("stop_prior_seqs") or []),
        "sequence_stop_ids": list(original_stop_ids),
        "recovered_candidates": _serialize_rows(recovered),
        "ambiguous_candidates": _serialize_rows(ambiguous),
        "rejected_candidates": _serialize_rows(rejected),
        "evidence_classes": {
            "original_extracted_stops": _int_list(candidate_context.get("stop_prior_seqs") or []),
            "sequence_derived_stops": list(original_stop_ids),
            "geometry_recovered_stops": list(recovered_stop_ids),
            "ambiguous_nearby_stops": list(ambiguous_stop_ids),
            "rejected_nearby_stops": list(rejected_stop_ids),
        },
    }

    return {
        "geometry_candidate_id": geometry_candidate_id,
        "set_id": set_id,
        "route_id": route_id,
        "stop_sequence_candidate_id": stop_sequence_candidate_id,
        "original_stop_ids": list(original_stop_ids),
        "original_stop_prior_seqs": _int_list(candidate_context.get("stop_prior_seqs") or []),
        "recovered_stop_ids": list(recovered_stop_ids),
        "ambiguous_nearby_stop_ids": list(ambiguous_stop_ids),
        "rejected_nearby_stop_ids": list(rejected_stop_ids),
        "enriched_stop_ids": list(enriched_stop_ids),
        "insertion_proposals": insertion_proposals,
        "provenance": provenance,
        "summary_metrics": summary_metrics,
    }


def _load_geometry_candidate_context(conn: Any, geometry_candidate_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT
              gc.geometry_candidate_id,
              gc.set_id,
              gcs.route_id,
              gc.stop_sequence_candidate_id,
              gc.length_m,
              ssc.stop_node_ids,
              ssc.stop_prior_seqs
            FROM route_work.geometry_candidates gc
            JOIN route_work.geometry_candidate_sets gcs
              ON gcs.set_id = gc.set_id
            LEFT JOIN route_work.stop_sequence_candidates ssc
              ON ssc.candidate_id = gc.stop_sequence_candidate_id
            WHERE gc.geometry_candidate_id = %s
            """,
            (str(geometry_candidate_id),),
        )
        row = cur.fetchone() or {}
    if not row:
        return None
    return {
        "geometry_candidate_id": str(row.get("geometry_candidate_id") or ""),
        "set_id": str(row.get("set_id") or ""),
        "route_id": str(row.get("route_id") or ""),
        "stop_sequence_candidate_id": _to_uuid_text(row.get("stop_sequence_candidate_id")),
        "length_m": _coerce_float(row.get("length_m")),
        "stop_node_ids": _uuid_text_list(row.get("stop_node_ids") or []),
        "stop_prior_seqs": _int_list(row.get("stop_prior_seqs") or []),
    }


def _load_original_stops(conn: Any, *, geometry_candidate_id: uuid.UUID, stop_node_ids: List[str]) -> List[Dict[str, Any]]:
    if not stop_node_ids:
        return []
    with db_cursor(conn) as cur:
        cur.execute(
            """
            WITH requested AS (
              SELECT stop_id, ord
              FROM unnest(%s::uuid[]) WITH ORDINALITY AS t(stop_id, ord)
            ),
            line AS (
              SELECT geom
              FROM route_work.geometry_candidates
              WHERE geometry_candidate_id = %s
            )
            SELECT
              requested.stop_id::text AS stop_id,
              requested.ord::int AS order_index,
              COALESCE(NULLIF(BTRIM(p.canonical_name), ''), NULLIF(BTRIM(n.name), ''), NULLIF(BTRIM(n.ref), ''), ('stop_' || LEFT(n.node_id::text, 8))) AS name,
              n.ref,
              ST_Y(n.geom)::float8 AS lat,
              ST_X(n.geom)::float8 AS lon,
              ST_LineLocatePoint((SELECT geom FROM line), n.geom)::float8 AS progress,
              ST_Distance(n.geom::geography, (SELECT geom FROM line)::geography)::float8 AS dist_to_geometry_m
            FROM requested
            JOIN node_prod.nodes n
              ON n.node_id = requested.stop_id
            LEFT JOIN geo_prod.node_place_map m
              ON m.node_id = n.node_id
            LEFT JOIN geo_prod.places p
              ON p.place_id = m.place_id
            ORDER BY requested.ord ASC
            """,
            (stop_node_ids, str(geometry_candidate_id)),
        )
        rows = cur.fetchall() or []
    return [dict(row or {}) for row in rows]


def _load_nearby_stops(
    conn: Any,
    *,
    geometry_candidate_id: uuid.UUID,
    original_stop_ids: List[str],
    scan_radius_m: float,
    limit: int,
) -> List[Dict[str, Any]]:
    with db_cursor(conn) as cur:
        cur.execute(
            """
            WITH original AS (
              SELECT stop_id
              FROM unnest(%s::uuid[]) AS t(stop_id)
            ),
            line AS (
              SELECT geom
              FROM route_work.geometry_candidates
              WHERE geometry_candidate_id = %s
            )
            SELECT
              n.node_id::text AS stop_id,
              COALESCE(NULLIF(BTRIM(p.canonical_name), ''), NULLIF(BTRIM(n.name), ''), NULLIF(BTRIM(n.ref), ''), ('stop_' || LEFT(n.node_id::text, 8))) AS name,
              n.ref,
              ST_Y(n.geom)::float8 AS lat,
              ST_X(n.geom)::float8 AS lon,
              ST_Distance(n.geom::geography, (SELECT geom FROM line)::geography)::float8 AS dist_to_geometry_m,
              ST_LineLocatePoint((SELECT geom FROM line), n.geom)::float8 AS progress,
              ST_AsText(ST_ClosestPoint((SELECT geom FROM line), n.geom)) AS closest_point_wkt
            FROM geo_prod.node_place_map m
            JOIN node_prod.nodes n
              ON n.node_id = m.node_id
            JOIN geo_prod.places p
              ON p.place_id = m.place_id
            WHERE n.node_type = 'STOP'
              AND COALESCE(p.status, 'active') = 'active'
              AND NOT EXISTS (
                SELECT 1
                FROM original o
                WHERE o.stop_id = n.node_id
              )
              AND ST_DWithin(
                n.geom::geography,
                (SELECT geom FROM line)::geography,
                %s
              )
            ORDER BY dist_to_geometry_m ASC, progress ASC, n.node_id ASC
            LIMIT %s
            """,
            (original_stop_ids, str(geometry_candidate_id), float(scan_radius_m), int(limit)),
        )
        rows = cur.fetchall() or []
    return [dict(row or {}) for row in rows]


def _upsert_geometry_stop_recovery(conn: Any, result: Dict[str, Any]) -> None:
    with db_cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO route_work.geometry_stop_recovery (
              geometry_candidate_id,
              set_id,
              route_id,
              stop_sequence_candidate_id,
              original_stop_ids,
              original_stop_prior_seqs,
              recovered_stop_ids,
              ambiguous_nearby_stop_ids,
              rejected_nearby_stop_ids,
              enriched_stop_ids,
              insertion_proposals,
              provenance,
              summary_metrics,
              updated_at
            )
            VALUES (
              %s, %s, %s, %s,
              %s::uuid[],
              %s::int[],
              %s::uuid[],
              %s::uuid[],
              %s::uuid[],
              %s::uuid[],
              %s::jsonb,
              %s::jsonb,
              %s::jsonb,
              now()
            )
            ON CONFLICT (geometry_candidate_id)
            DO UPDATE SET
              set_id = EXCLUDED.set_id,
              route_id = EXCLUDED.route_id,
              stop_sequence_candidate_id = EXCLUDED.stop_sequence_candidate_id,
              original_stop_ids = EXCLUDED.original_stop_ids,
              original_stop_prior_seqs = EXCLUDED.original_stop_prior_seqs,
              recovered_stop_ids = EXCLUDED.recovered_stop_ids,
              ambiguous_nearby_stop_ids = EXCLUDED.ambiguous_nearby_stop_ids,
              rejected_nearby_stop_ids = EXCLUDED.rejected_nearby_stop_ids,
              enriched_stop_ids = EXCLUDED.enriched_stop_ids,
              insertion_proposals = EXCLUDED.insertion_proposals,
              provenance = EXCLUDED.provenance,
              summary_metrics = EXCLUDED.summary_metrics,
              updated_at = now()
            """,
            (
                str(result.get("geometry_candidate_id") or ""),
                str(result.get("set_id") or ""),
                str(result.get("route_id") or ""),
                result.get("stop_sequence_candidate_id"),
                _uuid_text_list(result.get("original_stop_ids") or []),
                _int_list(result.get("original_stop_prior_seqs") or []),
                _uuid_text_list(result.get("recovered_stop_ids") or []),
                _uuid_text_list(result.get("ambiguous_nearby_stop_ids") or []),
                _uuid_text_list(result.get("rejected_nearby_stop_ids") or []),
                _uuid_text_list(result.get("enriched_stop_ids") or []),
                json.dumps(result.get("insertion_proposals") or [], ensure_ascii=False),
                json.dumps(result.get("provenance") or {}, ensure_ascii=False),
                json.dumps(result.get("summary_metrics") or {}, ensure_ascii=False),
            ),
        )


def recover_geometry_candidate_stop_intersections(
    conn: Any,
    *,
    geometry_candidate_id: uuid.UUID,
    scan_radius_m: float = DEFAULT_SCAN_RADIUS_M,
    recovery_radius_m: float = DEFAULT_RECOVERY_RADIUS_M,
    nearby_limit: int = DEFAULT_NEARBY_LIMIT,
) -> Dict[str, Any]:
    ensure_geometry_stop_recovery_schema(conn)
    context = _load_geometry_candidate_context(conn, geometry_candidate_id)
    if not context:
        raise ValueError(f"geometry_candidate_id not found: {geometry_candidate_id}")
    original_stops = _load_original_stops(
        conn,
        geometry_candidate_id=geometry_candidate_id,
        stop_node_ids=list(context.get("stop_node_ids") or []),
    )
    nearby_stops = _load_nearby_stops(
        conn,
        geometry_candidate_id=geometry_candidate_id,
        original_stop_ids=list(context.get("stop_node_ids") or []),
        scan_radius_m=scan_radius_m,
        limit=nearby_limit,
    )
    result = build_geometry_stop_recovery_result(
        context,
        original_stops=original_stops,
        nearby_stops=nearby_stops,
        scan_radius_m=scan_radius_m,
        recovery_radius_m=recovery_radius_m,
    )
    _upsert_geometry_stop_recovery(conn, result)
    return result


def run_geometry_stop_recovery_for_set(
    conn: Any,
    *,
    route_id: uuid.UUID,
    geometry_set_id: uuid.UUID,
    scan_radius_m: float = DEFAULT_SCAN_RADIUS_M,
    recovery_radius_m: float = DEFAULT_RECOVERY_RADIUS_M,
    nearby_limit: int = DEFAULT_NEARBY_LIMIT,
) -> Dict[str, Any]:
    ensure_geometry_stop_recovery_schema(conn)
    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT gc.geometry_candidate_id
            FROM route_work.geometry_candidates gc
            JOIN route_work.geometry_candidate_sets gcs
              ON gcs.set_id = gc.set_id
            WHERE gc.set_id = %s
              AND gcs.route_id = %s
            ORDER BY gc.created_at ASC, gc.geometry_candidate_id ASC
            """,
            (str(geometry_set_id), str(route_id)),
        )
        rows = cur.fetchall() or []

    candidate_ids = [_to_uuid_text(row.get("geometry_candidate_id")) for row in rows]
    candidate_ids = [row for row in candidate_ids if row]
    results: List[Dict[str, Any]] = []
    recovered_total = 0
    ambiguous_total = 0
    rejected_total = 0
    nearby_scanned_total = 0
    for candidate_id in candidate_ids:
        result = recover_geometry_candidate_stop_intersections(
            conn,
            geometry_candidate_id=uuid.UUID(candidate_id),
            scan_radius_m=scan_radius_m,
            recovery_radius_m=recovery_radius_m,
            nearby_limit=nearby_limit,
        )
        results.append(result)
        summary = dict(result.get("summary_metrics") or {})
        recovered_total += int(summary.get("recovered_count") or 0)
        ambiguous_total += int(summary.get("ambiguous_count") or 0)
        rejected_total += int(summary.get("rejected_count") or 0)
        nearby_scanned_total += int(summary.get("nearby_stops_scanned") or 0)

    return {
        "route_id": str(route_id),
        "geometry_set_id": str(geometry_set_id),
        "geometry_candidate_count": len(candidate_ids),
        "nearby_stops_scanned": nearby_scanned_total,
        "recovered_total": recovered_total,
        "ambiguous_total": ambiguous_total,
        "rejected_total": rejected_total,
        "candidates": results,
    }
