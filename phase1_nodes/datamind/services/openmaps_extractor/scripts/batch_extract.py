"""
Phase 1 Batch Extractor
=======================
Reads target places from phase1_extract_targets.json,
resolves each to a bbox via Nominatim geocoder + hardcoded fallbacks,
runs multiple Overpass extraction actions per place,
creates node_sets, and logs diagnostics.

Usage:
    cd <project_root>
    python -m phase1_nodes.datamind.services.openmaps_extractor.scripts.batch_extract \
        --targets /path/to/phase1_extract_targets.json \
        [--actions stops_broad_bbox,stops_quality_bbox,platforms_bbox,terminals_and_stations_bbox] \
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Province-namespaced bbox catalog loaded from
# ``phase1_nodes/catalogs/extraction_bboxes.json``. For each province,
# the catalog holds a flat dict of area_id → {south,west,north,east}.
def _load_bboxes(province: Optional[str] = None) -> Dict[str, Dict[str, float]]:
    from datamind_core.province_config import (
        DEFAULT_PROVINCE,
        load_province_namespaced_catalog,
    )
    p = Path(__file__).resolve().parent.parent.parent.parent / "catalogs" / "extraction_bboxes.json"
    if not p.exists():
        return {}
    return load_province_namespaced_catalog(p, province or DEFAULT_PROVINCE) or {}


_HARDCODED_BBOXES: Dict[str, Dict[str, float]] = _load_bboxes()


from datamind_core.province_config import (  # noqa: E402
    DEFAULT_PROVINCE as _DEFAULT_PROVINCE,
    get_nominatim_bias as _province_nominatim_bias,
)

_ACTIVE_PROVINCE = _DEFAULT_PROVINCE



def _setup_env():
    if load_dotenv:
        load_dotenv()


def _normalize(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower().strip()
    raw = re.sub(r"[^a-z0-9 ]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


_QUITO_SUR_GROUPS = {
    "core places",
    "stops and terminals",
    "route corridor seeds",
    "poi and landmark seeds",
    "operator and cooperative seeds",
    "sectors",
}
_QUITO_SUR_HINT_TOKENS = {
    "quitumbe",
    "guamani",
    "chillogallo",
    "ecuatoriana",
    "turubamba",
    "argelia",
    "magdalena",
    "trebol",
    "23",
}


def _derive_group_hint(place_name: str, group: str, target_document: str) -> str:
    norm_group = _normalize(group)
    norm_place = _normalize(place_name)
    norm_doc = _normalize(target_document)

    if "quito sur" in norm_doc:
        return "quito sur"
    if any(tok in norm_place.split() for tok in _QUITO_SUR_HINT_TOKENS):
        return "quito sur"
    if "quito urbano" in norm_group:
        return "quito urbano"
    if "tumbaco" in norm_group or "cumbaya" in norm_group:
        return "tumbaco-cumbaya"
    if "valle de los chillos" in norm_group or "ruminahui" in norm_group:
        return "valle de los chillos"
    return group


_GEOCODER_LAST_TS = 0.0


def _expand_bbox_min_span(
    bbox: Dict[str, float],
    *,
    min_lat_span: float = 0.009,
    min_lon_span: float = 0.009,
) -> Dict[str, float]:
    out = {
        "south": float(bbox["south"]),
        "west": float(bbox["west"]),
        "north": float(bbox["north"]),
        "east": float(bbox["east"]),
    }
    lat_span = out["north"] - out["south"]
    lon_span = out["east"] - out["west"]
    if lat_span < min_lat_span:
        center_lat = (out["south"] + out["north"]) / 2.0
        half_span = min_lat_span / 2.0
        out["south"] = center_lat - half_span
        out["north"] = center_lat + half_span
    if lon_span < min_lon_span:
        center_lon = (out["west"] + out["east"]) / 2.0
        half_span = min_lon_span / 2.0
        out["west"] = center_lon - half_span
        out["east"] = center_lon + half_span
    return out


def _merge_bboxes(bboxes: List[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if not bboxes:
        return None
    return {
        "south": min(float(b["south"]) for b in bboxes),
        "west": min(float(b["west"]) for b in bboxes),
        "north": max(float(b["north"]) for b in bboxes),
        "east": max(float(b["east"]) for b in bboxes),
    }


def _strip_seed_prefixes(text: str) -> str:
    cleaned = str(text or "").strip()
    patterns = [
        r"^(?:parada|paradero)\s+",
        r"^terminal\s+terrestre\s+de\s+",
        r"^terminal\s+terrestre\s+",
        r"^terminal\s+",
        r"^corredor\s+",
        r"^ruta\s+",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned


def _looks_like_corridor_seed(place_name: str, group: str) -> bool:
    raw = str(place_name or "")
    group_norm = _normalize(group)
    return bool(re.search(r"\s[-/]\s", raw)) or "corridor" in group_norm


def _split_corridor_segments(place_name: str) -> List[str]:
    raw = str(place_name or "").replace("–", "-").replace("—", "-").strip()
    if not raw or not re.search(r"\s[-/]\s", raw):
        return []

    parts = re.split(r"\s[-/]\s", raw)
    out: List[str] = []
    seen: set[str] = set()
    for part in parts:
        cleaned = _strip_seed_prefixes(part)
        norm = _normalize(cleaned)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(cleaned)
    return out


def _nominatim_resolve(
    place_name: str,
    group: str,
    province: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Try Nominatim geocoder directly for a place name.

    Province-agnostic: bias strings and group-level refinements come
    from ``workspace/config/supported_provinces.json`` for the active
    province. Retro-compat for Sample Region is guaranteed because the
    refinements it used before have been moved there verbatim.
    """
    global _GEOCODER_LAST_TS

    from datamind_core.province_config import get_province

    province_key = (province or _DEFAULT_PROVINCE).strip().lower() or _DEFAULT_PROVINCE
    province_bias = _province_nominatim_bias(province_key)
    entry = get_province(province_key)

    group_refinements = entry.get("nominatim_group_bias_refinements") or {}
    if not isinstance(group_refinements, dict):
        group_refinements = {}
    bias = group_refinements.get(group.lower(), province_bias) if group else province_bias

    extra_fallback = entry.get("nominatim_extra_fallback_query")
    queries: List[str] = [f"{place_name}, {bias}"]
    if isinstance(extra_fallback, str) and extra_fallback.strip():
        queries.append(f"{place_name}, {extra_fallback}")
    queries.append(f"{place_name}, Ecuador")

    for query in queries:
        payload = {
            "q": query,
            "format": "jsonv2",
            "limit": 3,
            "countrycodes": "ec",
            "viewbox": "-78.65,0.05,-78.20,-0.60",
            "bounded": 0,
            "dedupe": 1,
        }

        now = time.monotonic()
        wait = 1.1 - max(0.0, now - _GEOCODER_LAST_TS)
        if wait > 0:
            time.sleep(wait)

        url = f"https://nominatim.openstreetmap.org/search?{urllib_parse.urlencode(payload)}"
        req = urllib_request.Request(url, headers={"User-Agent": "DataMindPhase1Extractor/1.0"})
        _GEOCODER_LAST_TS = time.monotonic()

        try:
            with urllib_request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue

        if not isinstance(data, list) or not data:
            continue

        for row in data:
            bb = row.get("boundingbox")
            if not bb or len(bb) < 4:
                continue
            try:
                south, north, west, east = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
            except (ValueError, TypeError):
                continue
            if south >= north or west >= east:
                continue

            display = row.get("display_name", "")
            importance = float(row.get("importance", 0))

            return {
                "bbox": _expand_bbox_min_span({"south": south, "west": west, "north": north, "east": east}),
                "interpretation_source": "nominatim_geocoder",
                "interpretation_status": "ok",
                "interpreted_place_meaning": display,
                "interpretation_confidence": min(0.88, 0.55 + importance * 0.3),
            }

    return None


def _resolve_with_shared_geography(place_name: str, group: str) -> Optional[Dict[str, Any]]:
    try:
        from datamind_console.common.geography_input_resolver import SharedGeographyResolver

        resolver = SharedGeographyResolver(advisory_mode=None)
        result = resolver.resolve(
            phase="phase1",
            place_input=place_name,
            supporting_hints={"group": group},
            allow_ai_assist=False,
        )
        bbox = result.get("bbox_candidate")
        if bbox and result.get("bbox_validation_status") == "valid":
            return {
                "bbox": _expand_bbox_min_span(dict(bbox)),
                "interpretation_source": result.get("interpretation_source"),
                "interpretation_status": result.get("interpretation_status") or "ok",
                "interpreted_place_meaning": result.get("interpreted_place_meaning"),
                "interpretation_confidence": result.get("interpretation_confidence"),
                "resolution_payload": {
                    "bbox_validation_status": result.get("bbox_validation_status"),
                    "area_group_hint": result.get("area_group_hint"),
                    "sector_hint": result.get("sector_hint"),
                    "corridor_hint": result.get("corridor_hint"),
                    "fallback_used": result.get("fallback_used"),
                    "fallback_reason": result.get("fallback_reason"),
                    "geographic_interpretation_source": result.get("geographic_interpretation_source"),
                    "geographic_interpretation_status": result.get("geographic_interpretation_status"),
                },
            }
    except Exception:
        return None
    return None


def _resolve_corridor_bbox(place_name: str, group: str) -> Optional[Dict[str, Any]]:
    segments = _split_corridor_segments(place_name)
    if len(segments) < 2:
        return None

    resolved_segments: List[Dict[str, Any]] = []
    merged_inputs: List[Dict[str, float]] = []
    for segment in segments[:4]:
        result = _resolve_place_to_bbox(segment, group, allow_corridor_merge=False)
        if not result:
            continue
        bbox = result.get("bbox")
        if not isinstance(bbox, dict):
            continue
        merged_inputs.append(dict(bbox))
        resolved_segments.append(
            {
                "segment": segment,
                "bbox": dict(bbox),
                "interpretation_source": result.get("interpretation_source"),
                "interpreted_place_meaning": result.get("interpreted_place_meaning"),
                "interpretation_confidence": result.get("interpretation_confidence"),
            }
        )

    if len(merged_inputs) < 2:
        return None

    merged = _merge_bboxes(merged_inputs)
    if not merged:
        return None

    confidences = [float(r.get("interpretation_confidence") or 0.0) for r in resolved_segments]
    avg_confidence = (sum(confidences) / len(confidences)) if confidences else 0.72
    return {
        "bbox": _expand_bbox_min_span(merged, min_lat_span=0.015, min_lon_span=0.015),
        "interpretation_source": "corridor_segment_merge",
        "interpretation_status": "ok",
        "interpreted_place_meaning": " + ".join(r.get("segment") or "" for r in resolved_segments),
        "interpretation_confidence": round(min(0.93, max(0.70, avg_confidence * 0.95)), 4),
        "segment_resolutions": resolved_segments,
    }


def _resolve_place_to_bbox(
    place_name: str,
    group: str,
    *,
    allow_corridor_merge: bool = True,
) -> Optional[Dict[str, Any]]:
    """Resolution priority: shared resolver > corridor merge > hardcoded > direct Nominatim."""
    norm = _normalize(place_name)

    if allow_corridor_merge and _looks_like_corridor_seed(place_name, group):
        merged = _resolve_corridor_bbox(place_name, group)
        if merged:
            return merged

    shared = _resolve_with_shared_geography(place_name, group)
    if shared:
        return shared

    if norm in _HARDCODED_BBOXES:
        return {
            "bbox": _expand_bbox_min_span(dict(_HARDCODED_BBOXES[norm])),
            "interpretation_source": "hardcoded_bbox",
            "interpretation_status": "ok",
            "interpreted_place_meaning": place_name,
            "interpretation_confidence": 0.95,
        }

    direct = _nominatim_resolve(place_name, group, province=_ACTIVE_PROVINCE)
    if direct:
        return direct

    stripped = _strip_seed_prefixes(place_name)
    if stripped and _normalize(stripped) != norm:
        shared = _resolve_with_shared_geography(stripped, group)
        if shared:
            return shared

    return None


def _update_node_set_params(node_set_id: str, payload: Dict[str, Any]) -> None:
    if not node_set_id or not payload:
        return
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn, exec_sql

    with db_conn() as conn:
        exec_sql(
            conn,
            """
            UPDATE node_work.node_candidate_sets
            SET params_used = COALESCE(params_used, '{}'::jsonb) || %s::jsonb
            WHERE node_set_id = %s::uuid
            """,
            (json.dumps(payload), str(node_set_id)),
        )


def _node_set_review_snapshot(node_set_id: str) -> Dict[str, Any]:
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn, fetchone

    with db_conn() as conn:
        candidate_row = fetchone(
            conn,
            """
            SELECT
              COUNT(*)::int AS candidate_count,
              COUNT(*) FILTER (
                WHERE COALESCE(tag_kind, '') IN ('bus_stop', 'platform', 'stop_position', 'station', 'tram_stop')
              )::int AS stop_count,
              COUNT(*) FILTER (
                WHERE COALESCE(tag_kind, '') NOT IN ('bus_stop', 'platform', 'stop_position', 'station', 'tram_stop')
              )::int AS poi_count
            FROM node_work.node_candidates
            WHERE node_set_id = %s::uuid
            """,
            (str(node_set_id),),
        ) or {}
        resolved_row = fetchone(
            conn,
            """
            SELECT
              COUNT(*)::int AS resolved_count,
              COUNT(*) FILTER (WHERE status = 'approved')::int AS approved_count,
              COUNT(*) FILTER (WHERE status = 'work')::int AS work_count,
              COUNT(*) FILTER (WHERE status = 'rejected')::int AS rejected_count
            FROM node_work.nodes_resolved
            WHERE node_set_id = %s::uuid
            """,
            (str(node_set_id),),
        ) or {}
        meta_row = fetchone(
            conn,
            """
            SELECT rank_score, rank_model_ver
            FROM node_work.node_candidate_sets
            WHERE node_set_id = %s::uuid
            """,
            (str(node_set_id),),
        ) or {}

    candidate_count = int(candidate_row.get("candidate_count") or 0)
    stop_count = int(candidate_row.get("stop_count") or 0)
    poi_count = int(candidate_row.get("poi_count") or 0)
    return {
        "candidate_count": candidate_count,
        "stop_count": stop_count,
        "poi_count": poi_count,
        "other_count": max(0, candidate_count - stop_count - poi_count),
        "resolved_count": int(resolved_row.get("resolved_count") or 0),
        "approved_count": int(resolved_row.get("approved_count") or 0),
        "work_count": int(resolved_row.get("work_count") or 0),
        "rejected_count": int(resolved_row.get("rejected_count") or 0),
        "rank_score": meta_row.get("rank_score"),
        "rank_model_ver": meta_row.get("rank_model_ver"),
        "reviewable": candidate_count > 0,
    }


def _materialize_node_set_for_review(node_set_id: str) -> Dict[str, Any]:
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.features_job import run_features
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.normalize_job import run_normalize

    out: Dict[str, Any] = {
        "scope": "phase1_extractor_candidates_reviewable",
        "normalize": None,
        "features": None,
        "snapshot": None,
        "warnings": [],
    }

    try:
        normalized = run_normalize(str(node_set_id))
        out["normalize"] = normalized
    except Exception as exc:
        out["warnings"].append(f"normalize_failed:{exc}")
        return out

    if int(dict(out.get("normalize") or {}).get("candidates_total") or 0) > 0:
        try:
            out["features"] = run_features(str(node_set_id))
        except Exception as exc:
            out["warnings"].append(f"features_failed:{exc}")

    out["snapshot"] = _node_set_review_snapshot(str(node_set_id))

    return out


def _bbox_to_str(bbox: Dict[str, float]) -> str:
    return f"{bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"


_NAME_REGEX_STOPWORDS = {
    "de", "del", "la", "las", "los", "el", "y", "parada", "paradero", "terminal", "estacion",
}


def _ordered_unique(values: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        txt = str(value or "").strip()
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def _derive_name_regex(place_name: str) -> Optional[str]:
    norm = _normalize(place_name)
    if not norm:
        return None
    tokens = [
        tok for tok in norm.split()
        if len(tok) >= 3 and tok not in _NAME_REGEX_STOPWORDS
    ]
    phrases = [norm]
    if tokens:
        phrases.append(" ".join(tokens))
        phrases.extend(tokens[:6])
    parts = _ordered_unique([re.escape(piece) for piece in phrases if piece])
    if not parts:
        return None
    return "(?:" + "|".join(parts[:8]) + ")"


def _derive_ref_regex(place_name: str) -> Optional[str]:
    ref_like = [
        part for part in re.findall(r"[A-Za-z0-9]+", str(place_name or ""))
        if any(ch.isdigit() for ch in part)
    ]
    parts = _ordered_unique([re.escape(part) for part in ref_like[:4]])
    if not parts:
        return None
    return "(?:" + "|".join(parts) + ")"


def _make_params(
    bbox: Dict[str, float],
    *,
    place_name: str,
    group: str,
    priority: str,
    action_id: str,
) -> Dict[str, Any]:
    bbox_str = _bbox_to_str(bbox)
    params = {
        "bbox": bbox_str,
        "bbox_str": bbox_str,
        **bbox,
        "place_name": place_name,
        "group": group,
        "priority": priority,
        "action_id": action_id,
    }
    name_rx = _derive_name_regex(place_name)
    if name_rx:
        params["name_rx"] = name_rx
        params["operator_rx"] = name_rx
    ref_rx = _derive_ref_regex(place_name)
    if ref_rx:
        params["ref_rx"] = ref_rx
    return params


def _run_extract_for_place(
    place_name: str,
    group: str,
    priority: str,
    target_document: str,
    bbox: Dict[str, float],
    resolution_meta: Dict[str, Any],
    action_ids: List[str],
    actions_path: str,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.extract_job import run_extract
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn
    from phase1_nodes.datamind.services.openmaps_extractor.src.db.phase1_repo import create_node_set

    area_id = f"{group}::{place_name}"
    results = []

    for action_id in action_ids:
        params = _make_params(
            bbox,
            place_name=place_name,
            group=group,
            priority=priority,
            action_id=action_id,
        )
        run_id = None
        node_set_id = None
        if dry_run:
            results.append({
                "place": place_name,
                "group": group,
                "priority": priority,
                "action_id": action_id,
                "bbox": bbox,
                "query_params": params,
                "status": "dry_run",
                "elements": 0,
            })
            continue

        try:
            out = run_extract(
                action_id=action_id,
                params=params,
                actions_path=actions_path,
                area_id=area_id,
            )

            run_id = out["run_id"]
            elements = out["elements"]

            node_set_id = None
            review_materialization = None
            snapshot: Dict[str, Any] = {}
            normalize_out: Dict[str, Any] = {}
            features_out: Dict[str, Any] = {}
            if int(elements or 0) > 0:
                params_used = {
                    "workflow": "phase1_batch_extract",
                    "phase": "phase1",
                    "review_surface": "manual_phase1",
                    "manual_review_surface": "Phase 1 > Evidence / Workspace Nodes",
                    "target_document": target_document,
                    "extractor_scope": "phase1_extraction_only",
                    "place_name": place_name,
                    "group": group,
                    "priority": priority,
                    "area_id": area_id,
                    "bbox": dict(bbox),
                    "bbox_str": _bbox_to_str(bbox),
                    "action_id": action_id,
                    "query_params": dict(params),
                    "extractor_review": {
                        "place_input": place_name,
                        "target_group": group,
                        "priority": priority,
                        "target_file": target_document,
                        "area_id": area_id,
                        "bbox": dict(bbox),
                        "bbox_str": _bbox_to_str(bbox),
                        "action_id": action_id,
                        "status": "extracted",
                        "interpretation_source": resolution_meta.get("interpretation_source"),
                        "interpretation_status": resolution_meta.get("interpretation_status") or "ok",
                        "interpreted_place_meaning": resolution_meta.get("interpreted_place_meaning"),
                        "interpretation_confidence": resolution_meta.get("interpretation_confidence"),
                        "resolution_payload": dict(resolution_meta.get("resolution_payload") or {}),
                        "segment_resolutions": list(resolution_meta.get("segment_resolutions") or []),
                        "group_hint": resolution_meta.get("group_hint"),
                        "runtime_ms": out.get("runtime_ms"),
                        "http_status": out.get("http_status"),
                        "element_count": int(elements),
                        "query_id": out.get("query_id"),
                        "run_id": run_id,
                        "template": out.get("template"),
                    },
                }
                with db_conn() as conn:
                    node_set_id = create_node_set(
                        conn,
                        source_run_ids=[run_id],
                        action_ids=[action_id],
                        params_used=params_used,
                    )
                review_materialization = _materialize_node_set_for_review(str(node_set_id))
                snapshot = dict((review_materialization or {}).get("snapshot") or {})
                normalize_out = dict((review_materialization or {}).get("normalize") or {})
                features_out = dict((review_materialization or {}).get("features") or {})
                _update_node_set_params(
                    str(node_set_id),
                    {
                        "review_materialization": review_materialization,
                        "candidate_count": snapshot.get("candidate_count"),
                        "stop_count": snapshot.get("stop_count"),
                        "poi_count": snapshot.get("poi_count"),
                        "features_rows": features_out.get("features_rows"),
                        "extractor_review": {
                            "place_input": place_name,
                            "target_group": group,
                            "priority": priority,
                            "target_file": target_document,
                            "area_id": area_id,
                            "bbox": dict(bbox),
                            "bbox_str": _bbox_to_str(bbox),
                            "action_id": action_id,
                            "status": (
                                "candidate_materialized"
                                if int(snapshot.get("candidate_count") or 0) > 0
                                else "extracted_empty"
                            ),
                            "interpretation_source": resolution_meta.get("interpretation_source"),
                            "interpretation_status": resolution_meta.get("interpretation_status") or "ok",
                            "interpreted_place_meaning": resolution_meta.get("interpreted_place_meaning"),
                            "interpretation_confidence": resolution_meta.get("interpretation_confidence"),
                            "resolution_payload": dict(resolution_meta.get("resolution_payload") or {}),
                            "segment_resolutions": list(resolution_meta.get("segment_resolutions") or []),
                            "group_hint": resolution_meta.get("group_hint"),
                            "candidate_count": snapshot.get("candidate_count"),
                            "stop_like_count": snapshot.get("stop_count"),
                            "poi_like_count": snapshot.get("poi_count"),
                            "normalize_run": normalize_out,
                            "features_rows": features_out.get("features_rows"),
                            "runtime_ms": out.get("runtime_ms"),
                            "http_status": out.get("http_status"),
                            "element_count": int(elements),
                            "query_id": out.get("query_id"),
                            "run_id": run_id,
                            "template": out.get("template"),
                        },
                        "extraction_attempts": [
                            {
                                "attempt_no": 1,
                                "action_id": action_id,
                                "status": "ok",
                                "raw_elements_count": int(elements),
                                "candidate_count": snapshot.get("candidate_count"),
                                "stop_count": snapshot.get("stop_count"),
                                "poi_count": snapshot.get("poi_count"),
                                "features_rows": features_out.get("features_rows"),
                                "runtime_ms": out.get("runtime_ms"),
                                "http_status": out.get("http_status"),
                                "query_id": out.get("query_id"),
                                "run_id": run_id,
                                "template": out.get("template"),
                            }
                        ],
                        "extraction_diagnostics": {
                            "raw_elements_count": int(elements),
                            "candidate_count": snapshot.get("candidate_count"),
                            "stop_count": snapshot.get("stop_count"),
                            "poi_count": snapshot.get("poi_count"),
                            "features_rows": features_out.get("features_rows"),
                            "runtime_ms": out.get("runtime_ms"),
                            "http_status": out.get("http_status"),
                            "query_id": out.get("query_id"),
                            "run_id": run_id,
                            "template": out.get("template"),
                        },
                    },
                )

            results.append(
                {
                "place": place_name,
                "group": group,
                "priority": priority,
                "action_id": action_id,
                "bbox": bbox,
                "status": "ok",
                "elements": elements,
                "run_id": run_id,
                "node_set_id": str(node_set_id) if node_set_id else None,
                "candidate_count": snapshot.get("candidate_count"),
                "stop_count": snapshot.get("stop_count"),
                "poi_count": snapshot.get("poi_count"),
                "features_rows": features_out.get("features_rows"),
                "review_materialization": review_materialization,
                "query_params": params,
                "runtime_ms": out.get("runtime_ms"),
                "http_status": out.get("http_status"),
                "interpretation_source": resolution_meta.get("interpretation_source"),
                "interpretation_status": resolution_meta.get("interpretation_status") or "ok",
                "interpreted_meaning": resolution_meta.get("interpreted_place_meaning"),
                "confidence": resolution_meta.get("interpretation_confidence"),
                }
            )

            # Throttle between Overpass calls
            time.sleep(1.0)

        except Exception as exc:
            results.append({
                "place": place_name,
                "group": group,
                "priority": priority,
                "action_id": action_id,
                "bbox": bbox,
                "status": "error",
                "error": str(exc),
                "elements": 0,
                "run_id": run_id,
                "node_set_id": str(node_set_id) if node_set_id else None,
                "query_params": params,
            })
            time.sleep(3.0)

    return results


DEFAULT_ACTIONS = [
    "stops_broad_bbox",
    "stops_quality_bbox",
    "platforms_bbox",
    "terminals_and_stations_bbox",
    "stops_named_regex_bbox",
    "poi_transit_support_bbox",
]


def _load_existing_area_ids() -> set:
    """Load area_ids already extracted from DB to skip duplicates."""
    try:
        from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT area_id FROM node_raw.overpass_runs WHERE area_id IS NOT NULL")
                return {r[0] for r in cur.fetchall()}
    except Exception:
        return set()


def run_batch(
    targets_path: str,
    actions_path: str,
    action_ids: List[str],
    dry_run: bool = False,
    max_places: Optional[int] = None,
    priority_filter: Optional[str] = None,
    skip_existing: bool = False,
    skip_groups: Optional[List[str]] = None,
    stop_after_node_sets: Optional[int] = None,
) -> Dict[str, Any]:
    # Support comma-separated paths for multiple target files
    target_paths = [p.strip() for p in targets_path.split(",") if p.strip()]
    places: List[Dict[str, Any]] = []
    for tp in target_paths:
        t = json.loads(Path(tp).read_text(encoding="utf-8"))
        document_name = str(t.get("document_name") or Path(tp).name)
        for row in list(t.get("target_places", []) or []):
            item = dict(row or {})
            item["_source_document"] = document_name
            item["_source_path"] = tp
            places.append(item)

    if priority_filter:
        places = [p for p in places if p.get("priority") == priority_filter]

    if skip_groups:
        skip_norm = {g.lower() for g in skip_groups}
        places = [p for p in places if p.get("group", "").lower() not in skip_norm]

    # Deduplicate by normalized name
    seen_names: set = set()
    deduped: List[Dict[str, Any]] = []
    for p in places:
        norm = _normalize(p["name"])
        if norm not in seen_names:
            seen_names.add(norm)
            deduped.append(p)
    places = deduped

    existing_area_ids: set = set()
    if skip_existing and not dry_run:
        existing_area_ids = _load_existing_area_ids()
        before = len(places)
        places = [p for p in places if f"{p.get('group', 'unknown')}::{p['name']}" not in existing_area_ids]
        print(f"Skip-existing: {before - len(places)} already extracted, {len(places)} remaining")

    if max_places:
        places = places[:max_places]

    print(f"\n{'='*70}")
    print(f"Phase 1 Batch Extractor")
    print(f"{'='*70}")
    print(f"Targets:  {len(places)} places")
    print(f"Actions:  {action_ids}")
    print(f"Dry run:  {dry_run}")
    print(f"{'='*70}\n")

    all_results: List[Dict[str, Any]] = []
    total_node_sets = 0
    total_elements = 0
    total_candidate_count = 0
    total_stop_count = 0
    total_poi_count = 0
    failed_places: List[str] = []
    geocode_failures: List[str] = []
    place_summaries: List[Dict[str, Any]] = []

    for i, place in enumerate(places, 1):
        name = place["name"]
        group = place.get("group", "unknown")
        priority = place.get("priority", "medium")
        target_document = str(place.get("_source_document") or Path(target_paths[0]).name)
        group_hint = _derive_group_hint(name, group, target_document)

        print(f"\n[{i}/{len(places)}] {name} (group={group}, priority={priority}, hint={group_hint})")

        # Honor explicit bbox_hint from targets file (bypasses Sample Region-centric resolvers)
        explicit_bbox = place.get("bbox_hint")
        if isinstance(explicit_bbox, dict) and {"south","west","north","east"}.issubset(explicit_bbox.keys()):
            resolution = {
                "bbox": _expand_bbox_min_span(dict(explicit_bbox)),
                "interpretation_source": "explicit_bbox_hint",
                "interpretation_status": "ok",
                "interpreted_place_meaning": name,
                "interpretation_confidence": 1.0,
            }
        else:
            resolution = _resolve_place_to_bbox(name, group_hint)
        if resolution is None:
            print(f"  SKIP: could not resolve bbox for '{name}'")
            geocode_failures.append(name)
            continue
        resolution["group_hint"] = group_hint

        bbox = resolution["bbox"]
        source = resolution["interpretation_source"]
        interpretation_status = resolution.get("interpretation_status", "ok")
        meaning = resolution.get("interpreted_place_meaning", "")
        confidence = resolution.get("interpretation_confidence", 0)

        print(f"  bbox: {_bbox_to_str(bbox)}")
        print(
            f"  source: {source} | status: {interpretation_status} | "
            f"meaning: {meaning} | confidence: {confidence}"
        )

        results = _run_extract_for_place(
            place_name=name,
            group=group,
            priority=priority,
            target_document=target_document,
            bbox=bbox,
            resolution_meta=resolution,
            action_ids=action_ids,
            actions_path=actions_path,
            dry_run=dry_run,
        )

        place_node_sets = sum(1 for r in results if r.get("node_set_id") and int(r.get("candidate_count") or 0) > 0)
        place_elements = sum(r.get("elements", 0) for r in results)
        place_candidates = sum(int(r.get("candidate_count") or 0) for r in results)
        place_stops = sum(int(r.get("stop_count") or 0) for r in results)
        place_pois = sum(int(r.get("poi_count") or 0) for r in results)
        place_errors = sum(1 for r in results if r.get("status") == "error")

        total_node_sets += place_node_sets
        total_elements += place_elements
        total_candidate_count += place_candidates
        total_stop_count += place_stops
        total_poi_count += place_pois

        if place_errors > 0:
            failed_places.append(name)

        for r in results:
            status_icon = "OK" if r["status"] == "ok" else ("DRY" if r["status"] == "dry_run" else "ERR")
            ns_label = f" -> node_set={r['node_set_id'][:8]}" if r.get("node_set_id") else ""
            cand_label = f" | candidates={int(r.get('candidate_count') or 0)}" if r.get("node_set_id") else ""
            stop_poi_label = ""
            if r.get("node_set_id"):
                stop_poi_label = (
                    f" stop={int(r.get('stop_count') or 0)}"
                    f" poi={int(r.get('poi_count') or 0)}"
                )
            err_label = f" | {r.get('error', '')[:60]}" if r.get("error") else ""
            print(f"  [{status_icon}] {r['action_id']}: {r['elements']} elements{ns_label}{cand_label}{stop_poi_label}{err_label}")

        place_summaries.append(
            {
                "place": name,
                "group": group,
                "priority": priority,
                "target_document": target_document,
                "bbox": bbox,
                "interpretation_source": source,
                "interpretation_status": interpretation_status,
                "interpreted_meaning": meaning,
                "interpretation_confidence": confidence,
                "reviewable_node_sets": place_node_sets,
                "total_candidates": place_candidates,
                "total_stop_candidates": place_stops,
                "total_poi_candidates": place_pois,
                "errors": place_errors,
            }
        )

        all_results.extend(results)
        print(
            f"  Running total: {total_node_sets} node_sets, {total_elements} elements, "
            f"{total_candidate_count} candidates, {total_stop_count} stops, {total_poi_count} pois"
        )
        if stop_after_node_sets and total_node_sets >= int(stop_after_node_sets):
            print(
                f"\nReached stop-after-node-sets target ({int(stop_after_node_sets)}). "
                "Stopping batch early."
            )
            break

    print(f"\n{'='*70}")
    print(f"BATCH EXTRACTION COMPLETE")
    print(f"{'='*70}")
    print(f"Places attempted:     {len(places)}")
    print(f"Geocode failures:     {len(geocode_failures)}")
    print(f"Places with errors:   {len(failed_places)}")
    print(f"Total node_sets:      {total_node_sets}")
    print(f"Total elements:       {total_elements}")
    print(f"Total candidates:     {total_candidate_count}")
    print(f"Total stop cand.:     {total_stop_count}")
    print(f"Total poi cand.:      {total_poi_count}")
    print(f"Total extraction runs:{len(all_results)}")

    if geocode_failures:
        print(f"\nGeocoding failures: {geocode_failures}")
    if failed_places:
        print(f"\nExtraction failures: {failed_places}")

    diagnostics = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "summary": {
            "places_attempted": len(places),
            "geocode_failures": len(geocode_failures),
            "extraction_errors": len(failed_places),
            "total_node_sets": total_node_sets,
            "total_elements": total_elements,
            "total_candidates": total_candidate_count,
            "total_stop_candidates": total_stop_count,
            "total_poi_candidates": total_poi_count,
            "total_runs": len(all_results),
        },
        "geocode_failures": geocode_failures,
        "failed_places": failed_places,
        "place_summaries": sorted(
            place_summaries,
            key=lambda row: (
                int(row.get("reviewable_node_sets") or 0),
                int(row.get("total_candidates") or 0),
                int(row.get("total_stop_candidates") or 0),
            ),
            reverse=True,
        ),
        "results": all_results,
    }

    diag_root = Path(target_paths[0]).expanduser().parent if target_paths else Path(targets_path).expanduser().parent
    diag_path = diag_root / "phase1_batch_diagnostics.json"
    diag_payload = json.dumps(diagnostics, indent=2, default=str)
    try:
        diag_path.write_text(diag_payload, encoding="utf-8")
    except Exception:
        diag_root = Path.cwd()
        diag_path = diag_root / "phase1_batch_diagnostics.json"
        diag_path.write_text(diag_payload, encoding="utf-8")
    print(f"\nDiagnostics saved: {diag_path}")

    return diagnostics


if __name__ == "__main__":
    _setup_env()

    parser = argparse.ArgumentParser(description="Phase 1 Batch Extractor")
    parser.add_argument("--targets", type=str, required=True, help="Path to phase1_extract_targets.json")
    parser.add_argument("--actions", type=str, default=",".join(DEFAULT_ACTIONS),
                        help="Comma-separated action_ids")
    parser.add_argument("--dry-run", action="store_true", help="Resolve bboxes but don't run extraction")
    parser.add_argument("--max-places", type=int, default=None, help="Limit number of places")
    parser.add_argument("--priority", type=str, default=None, help="Filter by priority (high/medium/low)")
    parser.add_argument("--actions-json", type=str, default=None, help="Path to actions.json")
    parser.add_argument("--skip-existing", action="store_true", help="Skip places already extracted")
    parser.add_argument("--skip-groups", type=str, default=None,
                        help="Comma-separated groups to skip (e.g. 'Operator and Cooperative Seeds')")
    parser.add_argument("--stop-after-node-sets", type=int, default=None,
                        help="Stop once this many reviewable node_sets have been materialized")
    parser.add_argument("--province", type=str, default=_DEFAULT_PROVINCE,
                        help="Active province key (default: sample_region). Used for Nominatim bias.")
    args = parser.parse_args()

    _ACTIVE_PROVINCE = (args.province or _DEFAULT_PROVINCE).strip().lower() or _DEFAULT_PROVINCE

    action_ids = [a.strip() for a in args.actions.split(",") if a.strip()]

    actions_json_path = args.actions_json
    if not actions_json_path:
        actions_json_path = str(
            Path(__file__).resolve().parents[1] / "actions.json"
        )

    skip_groups = None
    if args.skip_groups:
        skip_groups = [g.strip() for g in args.skip_groups.split(",") if g.strip()]

    run_batch(
        targets_path=args.targets,
        actions_path=actions_json_path,
        action_ids=action_ids,
        dry_run=args.dry_run,
        max_places=args.max_places,
        priority_filter=args.priority,
        skip_existing=args.skip_existing,
        skip_groups=skip_groups,
        stop_after_node_sets=args.stop_after_node_sets,
    )
