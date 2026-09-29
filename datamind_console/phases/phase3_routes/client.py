# datamind_console/services/phase3/client.py
from __future__ import annotations
import subprocess
import json
import os
import sys
import uuid
import logging
from itertools import combinations
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import math
from statistics import median

# --- DB helpers (your project) ---
from phase3_routes.services.route_constructor.src.db.conn import db_conn, db_cursor

# --- Phase 3 pipeline helpers you already have ---
from phase3_routes.services.route_constructor.src.db.geo_prod_repo import nearest_stop_node
from phase3_routes.services.route_constructor.src.geometry.valhalla_client import valhalla_route
from phase3_routes.services.route_constructor.src.geometry.scoring import score_geometry
from phase3_routes.services.route_constructor.src.settings import VALHALLA_COSTING, VALHALLA_SHAPE_FORMAT, VALHALLA_URL
from phase3_routes.services.route_constructor.src.db.route_work_repo import (
    create_stop_sequence_set,
    insert_stop_sequence_candidate,
)
from phase3_routes.services.route_constructor.src.db.trash_repo import (
    trash_route as _trash_route_impl,
    restore_route as _restore_route_impl,
    trash_route_for_merge as _trash_route_for_merge_impl,
    get_trash_item as _get_trash_item_impl,
    list_trash as _list_trash_impl,
    list_delete_events as _list_delete_events_impl,
    is_route_trashed as _is_route_trashed_impl,
    deactivate_trashed_route as _deactivate_trashed_route_impl,
)

import streamlit as st
from phase3_routes.services.route_constructor.src.sequence.candidates import (
    build_sequence_candidates,
)
from datamind_console.phases.phase3_routes.manual_sequence_contracts import (
    ManualSequenceExportRequest,
    normalize_manual_sequence_export_request,
    validate_manual_sequence_rows,
)
from datamind_console.common.extractor_input_contracts import derive_phase3_route_hint_contract
from datamind_console.persistence import (
    patch_route_prod_fields,
    write_to_route_prod,
)
from datamind_console.persistence import (
    delete_route_prod as _delete_route_prod_row,
)

_PIPELINE_VERSION_APPROVAL = "phase3_client.approve_service_route_direction"
_PIPELINE_VERSION_BIND = "phase3_client.bind_route_to_direction"
_PIPELINE_VERSION_DELETE = "phase3_client.delete_route_job"
_PIPELINE_VERSION_INVALIDATE = "phase3_client.invalidate_route_resolution"
_SOURCE_TYPE_PHASE3 = "phase3_client"
from datamind_console.common.geography_input_resolver import (
    SharedGeographyResolver,
    normalize_geographic_text,
    normalize_group_hint_key,
)
from pipeline.phase_4_5.pair_detection.inverse_completion.src.pipeline.step15_inverse_completion import (
    analyze_inverse_completion as _analyze_inverse_completion_readonly,
    analyze_inverse_proposals as _analyze_inverse_proposals,
    dispatch_targeted_inverse_search_for_slot as _dispatch_targeted_inverse_search,
    get_persisted_direction_readiness as _get_persisted_direction_readiness,
    get_inverse_completion_summary as _get_inverse_completion_summary,
    get_step20_direction_gate as _get_step20_direction_gate,
    get_direction_readiness as _get_direction_readiness_readonly,
    get_inverse_proposal_row as _get_inverse_proposal_row,
    get_targeted_inverse_search_result as _get_targeted_inverse_search_result,
    list_direction_readiness as _list_direction_readiness_readonly,
    list_inverse_completion_rows as _list_inverse_completion_rows,
    list_inverse_proposal_rows as _list_inverse_proposal_rows,
    list_targeted_inverse_search_results as _list_targeted_inverse_search_results,
    list_persisted_direction_readiness as _list_persisted_direction_readiness,
    refresh_direction_readiness as _refresh_direction_readiness,
    refresh_inverse_proposals as _refresh_inverse_proposals,
    refresh_targeted_inverse_search as _refresh_targeted_inverse_search,
)

import re

try:
    from datamind_console.ai_insights.telemetry import (
        log_phase3_run as _ai_log_phase3_run,
        log_phase3_sequence_edit as _ai_log_phase3_sequence_edit,
    )
except Exception:
    _ai_log_phase3_run = None
    _ai_log_phase3_sequence_edit = None

try:
    from datamind_console.ai_insights.sequence_quality import (
        evaluate_sequence_quality as _evaluate_sequence_quality,
    )
except Exception:
    _evaluate_sequence_quality = None

try:
    from pipeline.phase_4_5.pair_detection.merge_evidence import RoutePairEvidenceExtractor
    from pipeline.phase_4_5.pair_detection.merge_scoring import score_route_pair_evidence
except Exception:
    RoutePairEvidenceExtractor = None
    score_route_pair_evidence = None


@st.cache_resource
def _get_phase3_client():
    return Phase3Client()



LonLat = Tuple[float, float]  # (lon, lat)
_LOG = logging.getLogger(__name__)


# =============================================================================
# Small utilities
# =============================================================================

def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v and v.strip() else default


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in list(value or []):
        if isinstance(row, dict):
            out.append(dict(row))
    return out


def _merge_nested_dicts(base: Optional[Dict[str, Any]], patch: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    left = dict(base or {})
    right = dict(patch or {})
    out = dict(left)
    for key, value in right.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = _merge_nested_dicts(existing, value)
        else:
            out[key] = value
    return out


_TEXT_STOPWORDS = {
    "a",
    "al",
    "and",
    "de",
    "del",
    "el",
    "la",
    "las",
    "los",
    "the",
    "to",
    "y",
}


def _normalized_text_key(value: Any) -> str:
    txt = normalize_geographic_text(value)
    txt = re.sub(r"\s+", " ", str(txt or "").strip())
    return txt


def _normalized_text_tokens(value: Any) -> List[str]:
    txt = _normalized_text_key(value)
    out: List[str] = []
    for token in re.split(r"[^a-z0-9]+", txt):
        tok = str(token or "").strip()
        if not tok or tok in _TEXT_STOPWORDS:
            continue
        if len(tok) <= 1 and not tok.isdigit():
            continue
        out.append(tok)
    return out


def _source_name(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return Path(text).name if text else None


def _normalized_text_equivalent(left: Any, right: Any) -> bool:
    left_key = _normalized_text_key(left)
    right_key = _normalized_text_key(right)
    if not left_key or not right_key:
        return False
    return bool(
        left_key == right_key
        or left_key in right_key
        or right_key in left_key
    )


def _route_hint_signature(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text)
    parts = [
        re.sub(r"[^a-z0-9]+", " ", part.lower()).strip()
        for part in re.split(r"\s*(?:-|/|>| to | a )\s*", cleaned, flags=re.IGNORECASE)
    ]
    parts = [part for part in parts if part]
    if len(parts) >= 2:
        return f"{parts[0]} -> {parts[-1]}"
    return parts[0] if parts else None


def _bbox_center_km_point(raw: Any) -> Optional[tuple[float, float]]:
    bbox = _coerce_bbox_dict(raw)
    if not bbox:
        return None
    lat = (bbox["south"] + bbox["north"]) / 2.0
    lon = (bbox["west"] + bbox["east"]) / 2.0
    return lon, lat


def _bbox_distance_km(left: Any, right: Any) -> Optional[float]:
    start = _bbox_center_km_point(left)
    end = _bbox_center_km_point(right)
    if not start or not end:
        return None
    return round(_haversine_m(start[0], start[1], end[0], end[1]) / 1000.0, 2)


def _max_bbox_spread_km(values: Iterable[Any]) -> Optional[float]:
    points = [_bbox_center_km_point(value) for value in list(values or [])]
    points = [point for point in points if point is not None]
    if len(points) < 2:
        return None
    max_km = 0.0
    for idx, left in enumerate(points):
        for right in points[idx + 1 :]:
            max_km = max(
                max_km,
                _haversine_m(left[0], left[1], right[0], right[1]) / 1000.0,
            )
    return round(max_km, 2)


def _dedupe_text_values(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        txt = str(raw or "").strip()
        key = _normalized_text_key(txt)
        if not txt or not key or key in seen:
            continue
        seen.add(key)
        out.append(txt)
    return out


def _coerce_uuid_text_list(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        try:
            txt = str(uuid.UUID(str(raw or "").strip()))
        except Exception:
            continue
        if txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def _pretty_sector_label(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    txt = raw.replace("_", " ").replace("/", " / ")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt.title()


def _split_gap_route_family_hint(route_family_hint: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    raw = str(route_family_hint or "").strip()
    if not raw:
        return None, None, None
    normalized = re.sub(r"\s+", " ", raw)
    direction_hint: Optional[str] = None
    lower = normalized.lower()
    if any(tok in lower for tok in (" ida", " vuelta", " outbound", " inbound", " clockwise", " counterclockwise")):
        direction_hint = normalized
    parts = [
        p.strip()
        for p in re.split(r"\s*(?:-|–|—|->|/| to )\s*", normalized)
        if str(p or "").strip()
    ]
    if len(parts) >= 2:
        return parts[0], parts[-1], direction_hint
    return None, None, direction_hint


def _route_family_match_score(expected: Any, observed_values: Iterable[Any]) -> float:
    expected_key = _normalized_text_key(expected)
    expected_tokens = set(_normalized_text_tokens(expected))
    if not expected_key:
        return 0.0
    best = 0.0
    for raw in list(observed_values or []):
        observed_key = _normalized_text_key(raw)
        if not observed_key:
            continue
        if observed_key == expected_key:
            return 1.0
        if expected_key in observed_key or observed_key in expected_key:
            best = max(best, 0.86)
        observed_tokens = set(_normalized_text_tokens(observed_key))
        if expected_tokens and observed_tokens:
            overlap = len(expected_tokens & observed_tokens)
            union = len(expected_tokens | observed_tokens)
            jaccard = float(overlap) / float(union) if union > 0 else 0.0
            if overlap >= 2:
                best = max(best, min(0.82, 0.35 + jaccard))
            else:
                best = max(best, jaccard)
    return round(float(best), 4)


def _coverage_gap_dedupe_key(
    *,
    source_catalog: str,
    sector_key: str,
    route_family_hint: str,
    direction_hint: Optional[str] = None,
) -> str:
    return "|".join(
        [
            _normalized_text_key(source_catalog) or "catalog",
            _normalized_text_key(sector_key) or "unassigned",
            _normalized_text_key(route_family_hint) or "route",
            _normalized_text_key(direction_hint) or "-",
        ]
    )


def _coerce_bbox_dict(raw: Any) -> Optional[Dict[str, float]]:
    if not isinstance(raw, dict):
        return None
    try:
        south = float(raw["south"])
        west = float(raw["west"])
        north = float(raw["north"])
        east = float(raw["east"])
    except Exception:
        return None
    if south >= north or west >= east:
        return None
    return {"south": south, "west": west, "north": north, "east": east}


def _centered_bbox(
    *,
    center_lat: float,
    center_lon: float,
    lat_span: float,
    lon_span: float,
) -> Dict[str, float]:
    half_lat = max(0.0005, float(lat_span) / 2.0)
    half_lon = max(0.0005, float(lon_span) / 2.0)
    return {
        "south": float(center_lat) - half_lat,
        "west": float(center_lon) - half_lon,
        "north": float(center_lat) + half_lat,
        "east": float(center_lon) + half_lon,
    }


def _normalized_phase3_extract_bbox(
    bbox: Dict[str, float],
    *,
    group_hint: Optional[str] = None,
    priority: Optional[str] = None,
    extra_expand_pct: float = 0.0,
) -> Dict[str, float]:
    candidate = _coerce_bbox_dict(bbox) or {}
    if not candidate:
        return {}

    center_lat = (float(candidate["south"]) + float(candidate["north"])) / 2.0
    center_lon = (float(candidate["west"]) + float(candidate["east"])) / 2.0
    current_lat_span = max(0.001, float(candidate["north"]) - float(candidate["south"]))
    current_lon_span = max(0.001, float(candidate["east"]) - float(candidate["west"]))

    raw_group_key = normalize_geographic_text(group_hint)
    group_key = normalize_group_hint_key(group_hint)
    is_corridor_group = any(token in raw_group_key for token in ("corridor", "connector", "wide"))
    is_core_group = any(token in raw_group_key for token in ("core", "urbano", "urban"))
    min_lat_span = 0.035
    min_lon_span = 0.045
    max_lat_span = 0.100
    max_lon_span = 0.120

    if group_key == "valle de los chillos":
        min_lat_span = 0.050
        min_lon_span = 0.060
        max_lat_span = 0.150
        max_lon_span = 0.160
        if is_corridor_group:
            min_lat_span = 0.075
            min_lon_span = 0.090
            max_lat_span = 0.185
            max_lon_span = 0.205
        elif is_core_group:
            min_lat_span = 0.040
            min_lon_span = 0.050
            max_lat_span = 0.125
            max_lon_span = 0.145
    elif group_key == "tumbaco cumbaya":
        min_lat_span = 0.045
        min_lon_span = 0.060
        max_lat_span = 0.160
        max_lon_span = 0.160
    elif group_key == "quito sur":
        min_lat_span = 0.045
        min_lon_span = 0.055
        max_lat_span = 0.130
        max_lon_span = 0.140
        if is_corridor_group:
            min_lat_span = 0.060
            min_lon_span = 0.080
            max_lat_span = 0.150
            max_lon_span = 0.170
        elif is_core_group:
            min_lat_span = 0.040
            min_lon_span = 0.050
            max_lat_span = 0.100
            max_lon_span = 0.120

    priority_key = _normalize_priority_label(priority, default="medium")
    base_expand = 0.18 if priority_key != "low" else 0.12
    if group_key == "valle de los chillos" and is_corridor_group:
        base_expand += 0.08
    expand_pct = max(0.0, base_expand + float(extra_expand_pct or 0.0))
    target_lat_span = min(max_lat_span, max(min_lat_span, current_lat_span * (1.0 + expand_pct)))
    target_lon_span = min(max_lon_span, max(min_lon_span, current_lon_span * (1.0 + expand_pct)))
    return _centered_bbox(
        center_lat=center_lat,
        center_lon=center_lon,
        lat_span=target_lat_span,
        lon_span=target_lon_span,
    )


def _priority_sort_key(raw: Any) -> int:
    txt = str(raw or "").strip().lower()
    if txt == "high":
        return 0
    if txt == "medium":
        return 1
    if txt == "low":
        return 2
    return 9


def _normalize_priority_label(raw: Any, *, default: str = "medium") -> str:
    txt = str(raw or "").strip().lower()
    if txt in {"high", "medium", "low"}:
        return txt
    if txt in {
        "confirmed",
        "confirmed_in_provided_research",
        "primary",
        "priority_1",
        "p1",
        "critical",
    }:
        return "high"
    if txt in {
        "inferred",
        "inferred_from_provided_research",
        "secondary",
        "priority_2",
        "p2",
    }:
        return "medium"
    if txt in {
        "heuristic",
        "heuristic_for_extractor",
        "exploratory",
        "priority_3",
        "p3",
    }:
        return "low"
    return default


def _merge_priority_label(current: Optional[str], candidate: Optional[str], *, default: str = "medium") -> str:
    current_norm = _normalize_priority_label(current, default=default)
    candidate_norm = _normalize_priority_label(candidate, default=default)
    return current_norm if _priority_sort_key(current_norm) <= _priority_sort_key(candidate_norm) else candidate_norm


def _text_from_item(item: Any, *, keys: Tuple[str, ...] = ("name", "place", "label", "value")) -> Optional[str]:
    if isinstance(item, str):
        txt = str(item).strip()
        return txt or None
    if isinstance(item, dict):
        for key in keys:
            txt = str(item.get(key) or "").strip()
            if txt:
                return txt
    return None


def _text_list_from_items(
    items: Any,
    *,
    keys: Tuple[str, ...] = ("name", "place", "label", "value"),
) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        txt = _text_from_item(item, keys=keys)
        if not txt:
            continue
        norm = normalize_geographic_text(txt)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(txt)
    return out


def _normalized_priority_label(raw: Any, *, fallback: str = "medium") -> str:
    txt = str(raw or "").strip().lower()
    if txt in {"high", "medium", "low"}:
        return txt
    if txt in {
        "confirmed_recent_official",
        "official_registry_or_planning",
        "confirmed_in_provided_research",
        "confirmed",
        "primary",
        "critical",
    }:
        return "high"
    if txt in {
        "historical_or_secondary",
        "inferred_from_provided_research",
        "inferred",
        "secondary",
    }:
        return "medium"
    if txt in {"heuristic_for_extractor", "heuristic", "exploratory"}:
        return "low"
    fallback_txt = str(fallback or "").strip().lower()
    return fallback_txt if fallback_txt in {"high", "medium", "low"} else "medium"


def _better_priority(left: Any, right: Any) -> str:
    left_txt = _normalized_priority_label(left)
    right_txt = _normalized_priority_label(right)
    return left_txt if _priority_sort_key(left_txt) <= _priority_sort_key(right_txt) else right_txt


def _confidence_rank(raw: Any) -> int:
    txt = str(raw or "").strip().lower()
    if txt in {
        "confirmed_recent_official",
        "official_registry_or_planning",
        "confirmed_in_provided_research",
        "confirmed",
        "high",
    }:
        return 0
    if txt in {
        "historical_or_secondary",
        "inferred_from_provided_research",
        "inferred",
        "medium",
    }:
        return 1
    if txt in {"heuristic_for_extractor", "heuristic", "exploratory", "low"}:
        return 2
    return 9


def _coerce_catalog_text_list(value: Any) -> List[str]:
    out: List[str] = []
    for raw in list(value or []):
        txt = str(raw or "").strip()
        if txt:
            out.append(txt)
    return out


def _dedupe_text_rows(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        txt = str(raw or "").strip()
        norm = normalize_geographic_text(txt)
        if not txt or not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(txt)
    return out


def _phase3_catalog_group_for_place(
    place: Any,
    *,
    place_kind: Optional[str] = None,
    default_region: Optional[str] = None,
) -> Optional[str]:
    norm = normalize_geographic_text(place)
    kind_norm = normalize_geographic_text(place_kind)
    region_norm = normalize_geographic_text(default_region)

    if (
        "quito sur" in norm
        or any(
            tok in norm
            for tok in (
                "quitumbe",
                "chillogallo",
                "guamani",
                "la ecuatoriana",
                "turubamba",
                "la argelia",
                "la magdalena",
                "23 de mayo",
            )
        )
    ):
        return "Quito Sur"
    if any(tok in norm for tok in ("marin", "quito", "quitumbe", "trebol")):
        return "Quito Urbano"
    if any(
        tok in norm
        for tok in (
            "interoceanica",
            "ruta viva",
            "rio coca",
            "guayasamin",
            "usfq",
            "scala",
            "arenal",
            "primavera",
            "morita",
        )
    ):
        return "Tumbaco-Cumbaya"
    if any(tok in norm for tok in ("tumbaco", "cumbaya", "puembo", "pifo", "yaruqui", "checa", "quinche", "tababela", "nayon", "lumbisi")):
        return "Tumbaco-Cumbaya"
    if (
        "valle" in norm
        or "ruminahui" in norm
        or "chillos" in norm
        or any(
            tok in norm
            for tok in (
                "sangolqui",
                "conocoto",
                "san rafael",
                "alangasi",
                "amaguana",
                "pintag",
                "la merced",
                "guangopolo",
                "armenia",
                "taboada",
                "cotogchoa",
                "selva alegre",
                "colibri",
                "triangulo",
                "capelo",
                "tingo",
                "cashapamba",
                "rumipamba",
                "fajardo",
                "san alfonso",
            )
        )
    ):
        return "Valle de Los Chillos"
    if "terminal_or_anchor" in kind_norm:
        return "Quito Urbano"
    if "corridor_phrase" in kind_norm or "road_anchor" in kind_norm:
        if any(tok in region_norm for tok in ("tumbaco", "cumbaya", "oriental", "interoceanica")):
            return "Tumbaco-Cumbaya"
        return "Valle de Los Chillos"
    if "chillos" in region_norm or "ruminahui" in region_norm:
        return "Valle de Los Chillos"
    if any(tok in region_norm for tok in ("tumbaco", "cumbaya", "interoceanica", "oriental", "ruta viva")):
        return "Tumbaco-Cumbaya"
    return None


def _phase3_catalog_place_bundle(place: Any, *, place_kind: Optional[str] = None) -> str:
    norm = normalize_geographic_text(place)
    kind_norm = normalize_geographic_text(place_kind)
    if "corridor" in kind_norm or any(tok in norm for tok in ("interoceanica", "ruta viva")):
        return "tumbaco_corridor_hints"
    if (
        "terminal" in kind_norm
        or any(tok in norm for tok in ("rio coca", "guayasamin", "arenal"))
    ):
        return "tumbaco_gateway_anchors"
    if "landmark" in kind_norm or any(tok in norm for tok in ("usfq", "scala", "parque central")):
        return "tumbaco_landmark_anchors"
    if any(tok in norm for tok in ("cumbaya", "tumbaco", "primavera", "lumbisi", "morita")):
        return "tumbaco_core_bundle"
    if any(tok in norm for tok in ("pifo", "puembo", "tababela", "yaruqui", "checa", "quinche")):
        return "tumbaco_outer_axis"
    if any(
        tok in norm
        for tok in (
            "quitumbe",
            "chillogallo",
            "guamani",
            "la ecuatoriana",
            "turubamba",
            "la argelia",
            "la magdalena",
            "23 de mayo",
        )
    ):
        return "quito_sur_bundle"
    if "corridor" in kind_norm:
        return "valle_corridor_hints"
    if "terminal" in kind_norm or any(tok in norm for tok in ("marin", "quitumbe", "trebol")):
        return "valle_gateway_anchors"
    if any(tok in norm for tok in ("pintag", "amaguana", "alangasi", "la merced", "cotogchoa", "selva alegre", "san alfonso")):
        return "valle_outer_axis"
    if any(tok in norm for tok in ("sangolqui", "conocoto", "san rafael", "armenia", "taboada", "cashapamba", "fajardo")):
        return "valle_core_bundle"
    return "valle_catalog_places"


_PHASE3_CATALOG_BBOX_HINT_ROWS: List[Dict[str, Any]] = [
    {
        "label": "Tumbaco - Cumbaya Corridor",
        "bbox": {"south": -0.29, "west": -78.53, "north": -0.10, "east": -78.20},
        "aliases": [
            "cumbaya tumbaco",
            "tumbaco cumbaya",
            "corredor oriental de quito",
            "corredor interoceanico",
            "valle de cumbaya",
            "valle de tumbaco",
        ],
    },
    {
        "label": "Cumbaya Core",
        "bbox": {"south": -0.24, "west": -78.48, "north": -0.16, "east": -78.38},
        "aliases": [
            "cumbaya",
            "cumbaya centro",
            "san pedro de cumbaya",
            "usfq",
            "scala shopping",
            "lumbisi",
            "la primavera",
        ],
    },
    {
        "label": "Tumbaco Core",
        "bbox": {"south": -0.28, "west": -78.43, "north": -0.17, "east": -78.30},
        "aliases": [
            "tumbaco",
            "tumbaco centro",
            "tumbaco parque central",
            "la morita",
            "el nacional",
        ],
    },
    {
        "label": "Puembo - Pifo Axis",
        "bbox": {"south": -0.24, "west": -78.36, "north": -0.11, "east": -78.20},
        "aliases": [
            "puembo",
            "puembo centro",
            "pifo",
            "pifo centro",
            "el arenal",
        ],
    },
    {
        "label": "Rio Coca - SampleRegionBamin",
        "bbox": {"south": -0.24, "west": -78.50, "north": -0.14, "east": -78.41},
        "aliases": [
            "terminal rio coca",
            "rio coca",
            "terminal río coca",
            "oswaldo guayasamin",
        ],
    },
    {
        "label": "Valle de Los Chillos",
        "bbox": {"south": -0.35, "west": -78.60, "north": -0.05, "east": -78.35},
        "aliases": [
            "valle de los chillos",
            "administracion zonal los chillos",
            "valle chillos",
        ],
    },
    {
        "label": "Sangolqui Core",
        "bbox": {"south": -0.30, "west": -78.50, "north": -0.20, "east": -78.40},
        "aliases": [
            "sangolqui",
            "sangolqui centro",
            "san pedro de taboada",
            "cashapamba",
            "fajardo",
            "rumipamba de las rosas",
            "el triangulo",
            "capelo",
            "plaza valle",
        ],
    },
    {
        "label": "San Rafael - Conocoto",
        "bbox": {"south": -0.33, "west": -78.48, "north": -0.22, "east": -78.38},
        "aliases": [
            "san rafael",
            "conocoto",
            "la armenia",
            "san juan de conocoto",
            "puente 3",
        ],
    },
    {
        "label": "Alangasi - La Merced",
        "bbox": {"south": -0.40, "west": -78.46, "north": -0.28, "east": -78.34},
        "aliases": [
            "alangasi",
            "la merced",
            "cotogchoa",
            "guangopolo",
        ],
    },
    {
        "label": "Amaguana Axis",
        "bbox": {"south": -0.41, "west": -78.44, "north": -0.32, "east": -78.33},
        "aliases": [
            "amaguana",
            "selva alegre",
            "el tingo",
            "el colibri",
        ],
    },
    {
        "label": "Pintag Axis",
        "bbox": {"south": -0.5057432, "west": -78.41576035, "north": -0.3557432, "east": -78.25576035},
        "aliases": [
            "pintag",
            "pintag la marin",
            "san alfonso",
        ],
    },
    {
        "label": "La Marin",
        "bbox": {"south": -0.245, "west": -78.525, "north": -0.195, "east": -78.485},
        "aliases": [
            "la marin",
            "marin",
        ],
    },
    {
        "label": "Quitumbe Terminal",
        "bbox": {"south": -0.34, "west": -78.57, "north": -0.30, "east": -78.50},
        "aliases": [
            "quitumbe",
            "terminal quitumbe",
        ],
    },
    {
        "label": "Terminal Terrestre Quitumbe",
        "bbox": {"south": -0.34, "west": -78.57, "north": -0.30, "east": -78.50},
        "aliases": [
            "terminal terrestre quitumbe",
            "terminal terrestre de quitumbe",
        ],
    },
    {
        "label": "Chillogallo",
        "bbox": {"south": -0.31, "west": -78.58, "north": -0.24, "east": -78.52},
        "aliases": [
            "chillogallo",
            "23 de mayo",
        ],
    },
    {
        "label": "Guamani",
        "bbox": {"south": -0.38, "west": -78.57, "north": -0.31, "east": -78.51},
        "aliases": [
            "guamani",
        ],
    },
    {
        "label": "La Ecuatoriana",
        "bbox": {"south": -0.34, "west": -78.58, "north": -0.29, "east": -78.53},
        "aliases": [
            "la ecuatoriana",
        ],
    },
    {
        "label": "Turubamba",
        "bbox": {"south": -0.31, "west": -78.56, "north": -0.27, "east": -78.50},
        "aliases": [
            "turubamba",
        ],
    },
    {
        "label": "La Argelia",
        "bbox": {"south": -0.29, "west": -78.54, "north": -0.24, "east": -78.49},
        "aliases": [
            "la argelia",
        ],
    },
    {
        "label": "La Magdalena",
        "bbox": {"south": -0.27, "west": -78.54, "north": -0.22, "east": -78.49},
        "aliases": [
            "la magdalena",
        ],
    },
    {
        "label": "Centro Historico",
        "bbox": {"south": -0.235, "west": -78.53, "north": -0.20, "east": -78.50},
        "aliases": [
            "centro historico",
            "centro histórico",
        ],
    },
]


def _phase3_catalog_bbox_hint(place: Any, *, group: Optional[str] = None) -> Optional[Dict[str, float]]:
    norm = normalize_geographic_text(place)
    if not norm:
        return None
    for row in _PHASE3_CATALOG_BBOX_HINT_ROWS:
        aliases = {normalize_geographic_text(v) for v in list(row.get("aliases") or [])}
        if norm in aliases:
            return dict(row.get("bbox") or {})
    if any(tok in normalize_geographic_text(group) for tok in ("tumbaco", "cumbaya", "interoceanica", "oriental")):
        return {"south": -0.29, "west": -78.53, "north": -0.10, "east": -78.20}
    if "quito sur" in normalize_geographic_text(group):
        return {"south": -0.38, "west": -78.58, "north": -0.20, "east": -78.48}
    if "valle" in normalize_geographic_text(group):
        return {"south": -0.35, "west": -78.60, "north": -0.05, "east": -78.35}
    return None


def _phase3_catalog_route_keywords(place: Any) -> List[str]:
    norm = normalize_geographic_text(place)
    keyword_rows = {
        "quitumbe": ["quitumbe", "terminal quitumbe", "la magdalena"],
        "terminal terrestre quitumbe": ["quitumbe", "terminal quitumbe", "riobamba", "cuenca", "banos"],
        "chillogallo": ["chillogallo", "23 de mayo", "marin"],
        "guamani": ["guamani", "quitumbe", "sur"],
        "la ecuatoriana": ["la ecuatoriana", "quitumbe", "sur"],
        "turubamba": ["turubamba", "quitumbe", "centro historico"],
        "la argelia": ["la argelia", "quitumbe", "sur"],
        "la magdalena": ["la magdalena", "quitumbe", "centro historico"],
        "centro historico": ["centro historico", "quitumbe", "sur"],
        "marin": ["marin", "quitumbe", "chillogallo"],
        "23 de mayo": ["23 de mayo", "chillogallo", "marin"],
        "cumbaya": ["cumbaya", "usfq", "scala", "floresta", "arenal", "tumbaco"],
        "cumbaya centro": ["cumbaya", "usfq", "scala", "tumbaco"],
        "tumbaco": ["tumbaco", "morita", "rio coca", "cumbaya", "pifo"],
        "tumbaco centro": ["tumbaco", "morita", "rio coca", "cumbaya"],
        "tumbaco parque central": ["tumbaco", "rio coca", "pifo", "morita"],
        "usfq": ["usfq", "cumbaya", "scala", "arenal", "tumbaco"],
        "scala shopping": ["scala", "cumbaya", "rio coca", "pifo"],
        "la primavera": ["primavera", "cumbaya", "tumbaco"],
        "la morita": ["morita", "rio coca", "tumbaco", "pifo"],
        "puembo": ["puembo", "pifo", "interoceanica", "rio coca"],
        "puembo centro": ["puembo", "pifo", "interoceanica", "rio coca"],
        "pifo": ["pifo", "rio coca", "puembo", "interoceanica"],
        "pifo centro": ["pifo", "rio coca", "puembo", "interoceanica"],
        "terminal rio coca": ["rio coca", "pifo", "morita", "cumbaya"],
        "terminal río coca": ["rio coca", "pifo", "morita", "cumbaya"],
        "oswaldo guayasamin": ["guayasamin", "cumbaya", "tumbaco", "interoceanica"],
        "el arenal": ["arenal", "cumbaya", "pifo", "rio coca"],
        "interoceanica": ["interoceanica", "cumbaya", "tumbaco", "puembo", "pifo"],
        "ruta viva": ["ruta viva", "cumbaya", "tumbaco", "puembo"],
        "sangolqui": ["sangolqui", "metro", "marin"],
        "sangolqui centro": ["sangolqui", "metro", "marin"],
        "conocoto": ["conocoto", "san rafael", "metro", "marin"],
        "san rafael": ["san rafael", "conocoto", "metro", "marin"],
        "valle de los chillos": ["valle de los chillos", "metro", "marin", "quitumbe"],
        "amaguana": ["amaguana", "quito", "metro"],
        "tambillo": ["tambillo", "amaguana", "conocoto"],
        "pintag": ["pintag", "san alfonso", "marin"],
        "san alfonso": ["san alfonso", "pintag", "marin"],
        "selva alegre": ["selva alegre", "sangolqui", "marin"],
        "rumipamba": ["rumipamba", "sangolqui", "loreto", "cabre"],
        "loreto": ["loreto", "sangolqui", "cabre"],
        "cotogchoa": ["cotogchoa", "sangolqui", "quito"],
        "fajardo": ["fajardo", "sangolqui", "san rafael"],
        "el triangulo": ["triangulo", "sangolqui", "san rafael"],
        "rumiloma": ["rumiloma", "sangolqui", "san rafael"],
        "san fernando": ["san fernando", "sangolqui", "san rafael"],
        "san vicente": ["san vicente", "sangolqui", "san rafael"],
        "la marin": ["marin", "pintag", "sangolqui", "conocoto"],
        "quitumbe": ["quitumbe", "los chillos", "valle"],
    }
    if norm in keyword_rows:
        return keyword_rows[norm]
    tokens = [tok for tok in norm.split(" ") if len(tok) >= 4]
    return tokens or [norm]


def _route_hint_matches_place(place: Any, route_hint: Any) -> bool:
    place_norm = normalize_geographic_text(place)
    hint_norm = normalize_geographic_text(route_hint)
    if not place_norm or not hint_norm:
        return False
    place_tokens = {tok for tok in place_norm.split(" ") if len(tok) >= 4}
    hint_tokens = set(hint_norm.split(" "))
    if place_tokens.intersection(hint_tokens):
        return True
    keywords = _phase3_catalog_route_keywords(place)
    return any(normalize_geographic_text(keyword) in hint_norm for keyword in keywords if keyword)


def _phase3_harvest_dimension_summary(
    rows: Iterable[Dict[str, Any]],
    *,
    key: str,
    label: str,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    aggregates: Dict[str, Dict[str, Any]] = {}
    for row in list(rows or []):
        item = dict(row or {})
        raw = str(item.get(key) or "").strip()
        norm = normalize_geographic_text(raw)
        if not raw or not norm:
            continue
        bucket = aggregates.setdefault(
            norm,
            {
                label: raw,
                "extracted_count": 0,
                "new_relation_count": 0,
                "reused_relation_count": 0,
                "unique_relation_ids": set(),
                "selection_confidence_total": 0.0,
                "selection_confidence_count": 0,
                "top_stop_prior_total": 0,
                "top_stop_prior_count": 0,
            },
        )
        bucket[label] = bucket.get(label) or raw
        bucket["extracted_count"] += 1
        novelty_status = str(item.get("novelty_status") or "").strip()
        if novelty_status in {"novel_relation_selected", "novel_alternative_selected"}:
            bucket["new_relation_count"] += 1
        if novelty_status == "duplicate_reused_existing_route":
            bucket["reused_relation_count"] += 1
        rel_id = _to_int(item.get("chosen_osm_relation_id"))
        if rel_id is not None:
            bucket["unique_relation_ids"].add(int(rel_id))
        conf = _to_float(item.get("selection_confidence"))
        if conf is not None:
            bucket["selection_confidence_total"] += float(conf)
            bucket["selection_confidence_count"] += 1
        top_prior = _to_int(item.get("top_stop_prior_count"))
        if top_prior is not None:
            bucket["top_stop_prior_total"] += int(top_prior)
            bucket["top_stop_prior_count"] += 1

    summarized: List[Dict[str, Any]] = []
    for payload in aggregates.values():
        conf_count = int(payload.pop("selection_confidence_count") or 0)
        top_prior_count = int(payload.pop("top_stop_prior_count") or 0)
        unique_ids = sorted(int(v) for v in list(payload.pop("unique_relation_ids") or []))
        payload["unique_relation_count"] = int(len(unique_ids))
        payload["unique_relation_ids"] = unique_ids
        payload["avg_selection_confidence"] = (
            round(float(payload.pop("selection_confidence_total") or 0.0) / float(conf_count), 4)
            if conf_count > 0
            else None
        )
        payload["avg_top_stop_prior_count"] = (
            round(float(payload.pop("top_stop_prior_total") or 0.0) / float(top_prior_count), 2)
            if top_prior_count > 0
            else None
        )
        summarized.append(payload)

    summarized.sort(
        key=lambda row: (
            -int(row.get("extracted_count") or 0),
            -int(row.get("new_relation_count") or 0),
            -int(row.get("unique_relation_count") or 0),
            -float(row.get("avg_selection_confidence") or 0.0),
            str(row.get(label) or ""),
        )
    )
    return summarized[: max(1, int(limit or 8))]


def _extractor_review_context_count(rows: Iterable[Dict[str, Any]]) -> int:
    total = 0
    for row in list(rows or []):
        item = dict(row or {})
        if not bool(item.get("extractor_review_ready")):
            continue
        attempt_history_count = int(item.get("attempt_history_count") or 0)
        total += max(1, attempt_history_count)
    return int(total)


def _json_payload_mentions_source_document(payload: Any, extractor_source: str) -> bool:
    source_name = str(extractor_source or "").strip()
    if not source_name:
        return False
    source_name = Path(source_name).name
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if str(key) == "source_document":
                    doc_name = Path(str(value or "").strip()).name
                    if doc_name == source_name:
                        return True
                elif isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(list(current))
    return False


def _extractor_job_matches_source(job: Dict[str, Any], extractor_source: str) -> bool:
    source_name = Path(str(extractor_source or "").strip()).name
    if not source_name:
        return False
    direct = Path(str(job.get("extractor_source") or "").strip()).name
    if direct == source_name:
        return True
    review = job.get("extractor_review")
    return _json_payload_mentions_source_document(review, source_name)


def _scope_extractor_job_to_source(job: Dict[str, Any], extractor_source: str) -> Dict[str, Any]:
    source_name = Path(str(extractor_source or "").strip()).name
    if not source_name:
        return dict(job or {})
    out = dict(job or {})
    review = _as_dict(out.get("extractor_review"))
    if not review:
        return out
    attempt_history = _as_dict_list(review.get("attempt_history"))
    source_attempts = [
        attempt
        for attempt in attempt_history
        if _json_payload_mentions_source_document(attempt, source_name)
    ]
    if not source_attempts:
        return out

    latest = dict(source_attempts[-1])
    target = _as_dict(review.get("target"))
    hints = _as_dict(review.get("hints"))
    geography = _as_dict(review.get("geography"))
    dedupe = _as_dict(review.get("dedupe"))

    review["attempt_history"] = [dict(row) for row in source_attempts]
    if latest.get("place"):
        target["place"] = latest.get("place")
    if latest.get("group"):
        target["group"] = latest.get("group")
    if latest.get("priority"):
        target["priority"] = latest.get("priority")
    if latest.get("place_bundle"):
        target["place_bundle"] = latest.get("place_bundle")
    if latest.get("seed_origin"):
        target["seed_origin"] = latest.get("seed_origin")
    if latest.get("attempt_type"):
        target["attempt_type"] = latest.get("attempt_type")
    if latest.get("route_hint_raw"):
        hints["route_hint_raw"] = latest.get("route_hint_raw")
        hints["route_name"] = latest.get("route_hint_raw")
    if latest.get("cooperative_hint"):
        hints["cooperative_hint"] = latest.get("cooperative_hint")
        hints["operator"] = latest.get("cooperative_hint")
    if _coerce_bbox_dict(latest.get("bbox_used")):
        geography["bbox_used"] = _coerce_bbox_dict(latest.get("bbox_used"))
    if latest.get("interpretation_source"):
        geography["interpretation_source"] = latest.get("interpretation_source")
    if latest.get("novelty_status"):
        dedupe["novelty_status"] = latest.get("novelty_status")

    review["target"] = target
    review["hints"] = hints
    review["geography"] = geography
    review["dedupe"] = dedupe
    out["extractor_review"] = review
    out["target_place"] = target.get("place") or out.get("target_place")
    out["target_group"] = target.get("group") or out.get("target_group")
    out["target_priority"] = target.get("priority") or out.get("target_priority")
    out["target_place_bundle"] = target.get("place_bundle") or out.get("target_place_bundle")
    out["target_seed_origin"] = target.get("seed_origin") or out.get("target_seed_origin")
    out["target_attempt_type"] = target.get("attempt_type") or out.get("target_attempt_type")
    out["route_hint"] = hints.get("route_hint_raw") or out.get("route_hint")
    out["cooperative_hint"] = hints.get("cooperative_hint") or out.get("cooperative_hint")
    out["bbox_used"] = geography.get("bbox_used") or out.get("bbox_used")
    out["interpretation_source"] = geography.get("interpretation_source") or out.get("interpretation_source")
    out["extractor_novelty_status"] = dedupe.get("novelty_status") or out.get("extractor_novelty_status")
    out["selected_osm_relation_id"] = latest.get("chosen_osm_relation_id") or out.get("selected_osm_relation_id")
    out["selection_confidence"] = latest.get("selection_confidence") or out.get("selection_confidence")
    if latest.get("fetch_relation_stored") is not None:
        out["fetch_relation_stored"] = latest.get("fetch_relation_stored")
    out["attempt_history_count"] = int(len(source_attempts))
    out["source_attempt_history_count"] = int(len(source_attempts))
    return out


def _phase3_harvest_output_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "phase3_routes" / "harvest_outputs"


def _phase3_coverage_output_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "phase3_routes" / "coverage_outputs"


def _phase3_overpass_urls(preferred_url: Optional[str] = None) -> List[str]:
    urls: List[str] = []
    raw = str(os.getenv("DATAMIND_OVERPASS_URLS") or "").strip()
    if raw:
        urls.extend([u.strip() for u in raw.split(",") if u.strip()])
    else:
        env_single = str(os.getenv("OVERPASS_URL") or "").strip()
        if env_single:
            urls.append(env_single)
    if str(preferred_url or "").strip():
        urls.insert(0, str(preferred_url).strip())
    urls.extend(
        [
            "http://127.0.0.1:12346/api/interpreter",
            "https://overpass-api.de/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
        ]
    )
    ordered: List[str] = []
    for row in urls:
        txt = str(row or "").strip()
        if txt and txt not in ordered:
            ordered.append(txt)
    return ordered


def _script_path(name: str) -> str:
    """
    Resolve a route_constructor script path from repo root.
    """
    root = Path(__file__).resolve().parents[3]
    return str(root / "phase3_routes" / "services" / "route_constructor" / "scripts" / name)


def _phase3_root() -> Path:
    return Path(__file__).resolve().parents[3] / "phase3_routes" / "services" / "route_constructor"


def _run_script(args: List[str], *, env_overrides: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    """
    Run a route_constructor script with proper cwd/PYTHONPATH so 'src' imports work.
    """
    root = _phase3_root()
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[3]
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in [str(root), str(repo_root), existing] if p]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
    )


def _jsonable(x: Any) -> Any:
    """Best-effort conversion to something JSON serializable."""
    if isinstance(x, uuid.UUID):
        return str(x)
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    return x


def _parse_uuid(x: Any) -> uuid.UUID:
    return x if isinstance(x, uuid.UUID) else uuid.UUID(str(x))


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in meters."""
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _percentile(xs: List[float], p: float) -> float:
    """p in [0,100]. Linear interpolation."""
    if not xs:
        return 0.0
    ys = sorted(xs)
    if len(ys) == 1:
        return float(ys[0])
    k = (len(ys) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(ys[int(k)])
    d0 = ys[f] * (c - k)
    d1 = ys[c] * (k - f)
    return float(d0 + d1)


def _parse_linestring_wkt(wkt: str) -> List[LonLat]:
    """
    Parse LINESTRING/MULTILINESTRING WKT into [(lon,lat), ...].
    If MULTILINESTRING, flattens parts.
    """
    if not wkt:
        return []
    s = wkt.strip()
    if s.upper().startswith("LINESTRING"):
        inner = s[s.find("(") + 1 : s.rfind(")")]
        pts = []
        for part in inner.split(","):
            part = part.strip()
            if not part:
                continue
            a, b = part.split()[:2]
            pts.append((float(a), float(b)))
        return pts

    if s.upper().startswith("MULTILINESTRING"):
        inner = s[s.find("(") + 1 : s.rfind(")")]
        # inner like: ((x y, x y),(x y, x y))
        pts: List[LonLat] = []
        # Split on "),(" boundaries (safe-ish for this WKT form)
        chunks = inner.replace("),(", ")|(").split("|")
        for ch in chunks:
            ch = ch.strip()
            ch = ch.strip("(").strip(")")
            if not ch:
                continue
            for part in ch.split(","):
                part = part.strip()
                if not part:
                    continue
                a, b = part.split()[:2]
                pts.append((float(a), float(b)))
        return pts

    # Unknown geometry
    return []


def _turn_severity_deg(p0: LonLat, p1: LonLat, p2: LonLat) -> float:
    """
    Turn severity in degrees.
    0 = straight, larger = sharper turn.
    """
    x1, y1 = (p1[0] - p0[0], p1[1] - p0[1])
    x2, y2 = (p2[0] - p1[0], p2[1] - p1[1])
    n1 = math.hypot(x1, y1)
    n2 = math.hypot(x2, y2)
    if n1 == 0 or n2 == 0:
        return 0.0
    dot = x1 * x2 + y1 * y2
    cosang = max(-1.0, min(1.0, dot / (n1 * n2)))
    ang = math.degrees(math.acos(cosang))  # 0..180
    # Straight continuation yields ang ~ 0? Actually vectors aligned => acos(1)=0.
    # Severity should be 0 for straight, so use ang directly.
    return float(ang)



def _parse_uuid_array(v: Any) -> List[uuid.UUID]:
    """
    Robust UUID[] parser.
    Handles:
      - None
      - list/tuple of uuid/str
      - Postgres uuid[] text: "{a,b,c}"
      - single uuid/str
    """
    if v is None:
        return []

    if isinstance(v, (list, tuple)):
        out: List[uuid.UUID] = []
        for x in v:
            if x is None:
                continue
            out.append(_parse_uuid(x))
        return out

    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("{") and s.endswith("}"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            parts = [p.strip().strip('"') for p in inner.split(",") if p.strip()]
            return [uuid.UUID(p) for p in parts]
        return [uuid.UUID(s)]

    # fallback
    try:
        return [uuid.UUID(str(v))]
    except Exception:
        return []


def _parse_int_array(v: Any) -> List[int]:
    """
    Robust int[] parser.
    Handles:
      - None
      - list/tuple of int/str
      - Postgres int[] text: "{1,2,3}"
      - single int/str
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v]
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        if s.startswith("{") and s.endswith("}"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            parts = [p.strip().strip('"') for p in inner.split(",") if p.strip()]
            return [int(p) for p in parts]
        return [int(s)]
    return [int(v)]


def _looks_like_latlon(p0: LonLat, p1: LonLat) -> bool:
    # p = (a,b) but could be (lat,lon) or (lon,lat)
    a0, b0 = p0
    a1, b1 = p1
    a_is_lat = -90 <= a0 <= 90 and -90 <= a1 <= 90
    b_is_lon = -180 <= b0 <= 180 and -180 <= b1 <= 180
    a_is_lon = -180 <= a0 <= 180 and -180 <= a1 <= 180
    b_is_lat = -90 <= b0 <= 90 and -90 <= b1 <= 90
    return (a_is_lat and b_is_lon) and not (a_is_lon and b_is_lat)


def _dedup_consecutive(coords: List[LonLat]) -> List[LonLat]:
    out: List[LonLat] = []
    last: Optional[LonLat] = None
    for c in coords:
        if last is None or c != last:
            out.append(c)
            last = c
    return out


def _to_linestring_wkt(points: List[LonLat]) -> str:
    """
    Accepts points that might be (lon,lat) OR (lat,lon).
    Returns LINESTRING(lon lat, lon lat, ...)
    """
    if not points:
        raise ValueError("Empty shape points")

    pts = [(round(float(a), 6), round(float(b), 6)) for (a, b) in points]
    pts = _dedup_consecutive(pts)

    if len(pts) < 2 or len(set(pts)) < 2:
        raise ValueError("LineString needs at least 2 distinct points")

    is_latlon = _looks_like_latlon(pts[0], pts[1])
    lonlat = [(lon, lat) for (lat, lon) in pts] if is_latlon else pts

    if len(lonlat) < 2 or len(set(lonlat)) < 2:
        raise ValueError("LineString needs at least 2 distinct points")

    coord_str = ", ".join(f"{lon} {lat}" for (lon, lat) in lonlat)
    return f"LINESTRING({coord_str})"


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _candidate_meta_from_tags(tags: Any) -> Dict[str, Any]:
    payload = dict(tags or {}) if isinstance(tags, dict) else {}
    meta = payload.get("_datamind_candidate_meta")
    return dict(meta or {}) if isinstance(meta, dict) else {}


def _summarize_discover_candidates(
    candidates: List[Dict[str, Any]],
    *,
    chosen_relation_id: Optional[int] = None,
    top_candidate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rows = [dict(c or {}) for c in list(candidates or [])]
    rows.sort(
        key=lambda r: (
            float(r.get("score") or 0.0),
            float(r.get("stop_prior_count") or 0.0),
        ),
        reverse=True,
    )
    top = dict(rows[0] or {}) if rows else dict(top_candidate or {})
    second = dict(rows[1] or {}) if len(rows) > 1 else {}

    top_score = _to_float(top.get("score"))
    second_score = _to_float(second.get("score"))
    top_stop_prior = int(top.get("stop_prior_count") or 0) if top else 0
    score_gap_top2 = (
        (float(top_score) - float(second_score))
        if top_score is not None and second_score is not None
        else None
    )

    flags: List[str] = []
    if len(rows) <= 0:
        flags.append("discover_candidates_empty")
    elif len(rows) == 1:
        flags.append("discover_candidate_pool_single")
    if top and top_stop_prior <= 0:
        flags.append("discover_top_candidate_no_stop_prior_signal")
    elif top and top_stop_prior < 3:
        flags.append("discover_top_candidate_weak_stop_prior_signal")
    if score_gap_top2 is not None and score_gap_top2 < 15.0:
        flags.append("discover_top_candidates_low_separation")

    chosen_present = None
    chosen_score = None
    chosen_rank = None
    if chosen_relation_id is not None:
        chosen_present = False
        for idx, row in enumerate(rows, start=1):
            rid = int(row.get("osm_relation_id") or 0) if row.get("osm_relation_id") is not None else None
            if rid == int(chosen_relation_id):
                chosen_present = True
                chosen_rank = idx
                chosen_score = _to_float(row.get("score"))
                break
        if chosen_present is False:
            flags.append("discover_chosen_relation_missing_from_candidates")

    signal_strength = "high"
    if any(
        f in flags
        for f in (
            "discover_candidates_empty",
            "discover_top_candidate_no_stop_prior_signal",
            "discover_top_candidate_weak_stop_prior_signal",
        )
    ):
        signal_strength = "low"
    elif any(
        f in flags
        for f in (
            "discover_candidate_pool_single",
            "discover_top_candidates_low_separation",
            "discover_chosen_relation_missing_from_candidates",
        )
    ):
        signal_strength = "medium"

    return {
        "candidate_count": int(len(rows)),
        "chosen_relation_id": (int(chosen_relation_id) if chosen_relation_id is not None else None),
        "chosen_present": chosen_present,
        "chosen_rank": chosen_rank,
        "chosen_score": chosen_score,
        "selection_confidence": _to_float(top.get("selection_confidence")),
        "top_relation_id": (int(top.get("osm_relation_id")) if top.get("osm_relation_id") is not None else None),
        "top_score": top_score,
        "top_stop_prior_count": int(top_stop_prior),
        "second_score": second_score,
        "score_gap_top2": score_gap_top2,
        "quality_flags": flags,
        "signal_strength": signal_strength,
    }


def _build_phase3_candidate_preview(candidates: List[Dict[str, Any]], *, limit: int = 10) -> List[Dict[str, Any]]:
    preview: List[Dict[str, Any]] = []
    for row in list(candidates or [])[: max(1, int(limit or 10))]:
        item = dict(row or {})
        preview.append(
            {
                "osm_relation_id": _to_int(item.get("osm_relation_id")),
                "selection_rank": _to_int(item.get("selection_rank")),
                "selection_confidence": _to_float(item.get("selection_confidence")),
                "score": _to_float(item.get("score")),
                "stop_prior_count": _to_int(item.get("stop_prior_count")),
                "ref": item.get("ref"),
                "name": item.get("name"),
                "operator": item.get("operator"),
                "route_mode": item.get("route_mode"),
                "matched_soft_signals": list(item.get("matched_soft_signals") or []),
                "selection_reason_codes": list(item.get("selection_reason_codes") or []),
            }
        )
    return preview


def _build_phase3_candidate_universe_summary(
    candidates: List[Dict[str, Any]],
    *,
    chosen_relation_id: Optional[int] = None,
    parsed_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rows = [dict(c or {}) for c in list(candidates or [])]
    diag = _summarize_discover_candidates(rows, chosen_relation_id=chosen_relation_id)
    top = dict(rows[0] or {}) if rows else {}
    parsed = dict(parsed_summary or {})
    return {
        **diag,
        "query_strategy": str(parsed.get("query_strategy") or top.get("query_strategy") or "").strip() or None,
        "candidate_universe_count": int(parsed.get("candidate_universe_count") or len(rows)),
        "candidate_scored_count": int(parsed.get("candidate_scored_count") or len(rows)),
        "candidate_fetch_evaluated_count": int(parsed.get("candidate_fetch_evaluated_count") or len(rows)),
        "max_candidates_requested": _to_int(parsed.get("max_candidates_requested")),
        "hard_filters_applied": list(parsed.get("hard_filters_applied") or top.get("hard_filters_applied") or []),
        "soft_signals_used": list(parsed.get("soft_signals_used") or top.get("soft_signals_used") or []),
        "operator_variants_attempted": _to_int(parsed.get("operator_variants_attempted")),
    }


def _build_phase3_selection_summary(
    candidates: List[Dict[str, Any]],
    *,
    chosen_relation_id: Optional[int] = None,
    parsed_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    parsed = dict(parsed_summary or {})
    rows = [dict(c or {}) for c in list(candidates or [])]
    selected: Dict[str, Any] = {}
    if chosen_relation_id is not None:
        for row in rows:
            rid = _to_int(row.get("osm_relation_id"))
            if rid is not None and int(rid) == int(chosen_relation_id):
                selected = dict(row or {})
                break
    if not selected and rows:
        selected = dict(rows[0] or {})
    return {
        "selection_status": str(parsed.get("selection_status") or ("provisional_selected" if selected else "none")).strip() or "none",
        "selected_osm_relation_id": _to_int(
            parsed.get("selected_osm_relation_id")
            if parsed.get("selected_osm_relation_id") is not None
            else selected.get("osm_relation_id")
        ),
        "selected_rank": _to_int(
            parsed.get("selected_rank")
            if parsed.get("selected_rank") is not None
            else selected.get("selection_rank")
        ),
        "selected_score": _to_float(
            parsed.get("selected_score")
            if parsed.get("selected_score") is not None
            else selected.get("score")
        ),
        "selection_confidence": _to_float(
            parsed.get("selection_confidence")
            if parsed.get("selection_confidence") is not None
            else selected.get("selection_confidence")
        ),
        "score_gap_top2": _to_float(parsed.get("score_gap_top2")),
        "selected_relation_stop_prior_count": _to_int(
            parsed.get("selected_relation_stop_prior_count")
            if parsed.get("selected_relation_stop_prior_count") is not None
            else selected.get("stop_prior_count")
        ),
        "selection_reason_codes": list(
            parsed.get("selection_reason_codes")
            or selected.get("selection_reason_codes")
            or []
        ),
    }


def _classify_step20_blocker_origin(
    *,
    prior_stop_count: int,
    unmatched_count: int,
    ambiguous_count: int,
    extractor_diagnostics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    diag = dict(extractor_diagnostics or {})
    flags = set(str(x) for x in (diag.get("quality_flags") or []))
    top_prior = int(diag.get("top_stop_prior_count") or 0)
    candidate_count = int(diag.get("candidate_count") or 0)

    if int(ambiguous_count) > 0:
        return {
            "dominant_cause": "matching_ambiguity",
            "confidence": "high",
            "reason": "Step20 ambiguity is dominant (ambiguous_count > 0).",
        }

    if int(unmatched_count) <= 0:
        return {
            "dominant_cause": "unknown",
            "confidence": "low",
            "reason": "No unmatched/ambiguous blocker present in Step20 payload.",
        }

    if int(prior_stop_count) <= 2:
        return {
            "dominant_cause": "extractor_config",
            "confidence": "medium",
            "reason": "Very low extracted prior stop count suggests weak extraction context.",
        }

    if (
        "discover_top_candidate_no_stop_prior_signal" in flags
        or "discover_top_candidate_weak_stop_prior_signal" in flags
        or (top_prior <= 2 and candidate_count > 0)
    ):
        return {
            "dominant_cause": "extractor_config",
            "confidence": "medium",
            "reason": "Discover diagnostics show weak relation evidence for Step20 matching.",
        }

    return {
        "dominant_cause": "node_db_gap",
        "confidence": "high",
        "reason": "Step20 unmatched dominates while extraction context is sufficiently populated.",
    }


def _classify_discover_attempt_error(stderr_text: str) -> str:
    txt = str(stderr_text or "").lower()
    if "429" in txt:
        return "rate_limited"
    if "504" in txt or "timeout" in txt:
        return "timeout"
    if "non-json" in txt or "<?xml" in txt:
        return "non_json_response"
    if "no route relations found" in txt:
        return "no_candidates"
    return "other"


def _parse_prefixed_json_line(stdout: str, prefix: str) -> Any:
    marker = f"{prefix}:"
    for raw in str(stdout or "").splitlines():
        line = str(raw or "").strip()
        if not line.startswith(marker):
            continue
        payload = line.split(":", 1)[1].strip()
        if not payload:
            return None
        try:
            return json.loads(payload)
        except Exception:
            return None
    return None


def _truncate_at_first_repeat_markers(
    markers: List[str],
    coords: List[LonLat],
) -> Tuple[List[str], List[LonLat], bool]:
    seen: set[str] = set()
    for i, m in enumerate(markers):
        if m in seen and i >= 2:
            return markers[:i], coords[:i], True
        seen.add(m)
    return markers, coords, False


def _truncate_at_first_coordinate_return(
    markers: List[str],
    coords: List[LonLat],
    *,
    tolerance_m: float = 35.0,
) -> Tuple[List[str], List[LonLat], bool]:
    for i in range(2, len(coords)):
        lon_i, lat_i = coords[i]
        for j in range(0, i - 1):
            lon_j, lat_j = coords[j]
            if _haversine_m(lon_i, lat_i, lon_j, lat_j) <= tolerance_m:
                return markers[:i], coords[:i], True
    return markers, coords, False


def _nearest_vertex_index(point: LonLat, shape: List[LonLat]) -> int:
    lon, lat = point
    best_i = 0
    best_d = float("inf")
    for i, (slon, slat) in enumerate(shape):
        d = _haversine_m(lon, lat, slon, slat)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i


def _directional_tightness(requested_coords: List[LonLat], shape_pts: List[LonLat]) -> Dict[str, Any]:
    start_anchor_dist_m = _haversine_m(
        requested_coords[0][0], requested_coords[0][1], shape_pts[0][0], shape_pts[0][1]
    )
    end_anchor_dist_m = _haversine_m(
        requested_coords[-1][0], requested_coords[-1][1], shape_pts[-1][0], shape_pts[-1][1]
    )
    nearest_idx = [_nearest_vertex_index(p, shape_pts) for p in requested_coords]
    monotonic_violations = 0
    last = -1
    for idx in nearest_idx:
        if idx < last:
            monotonic_violations += 1
        last = max(last, idx)
    avg_stop_to_shape_m = 0.0
    if requested_coords:
        total = 0.0
        for p in requested_coords:
            ni = _nearest_vertex_index(p, shape_pts)
            total += _haversine_m(p[0], p[1], shape_pts[ni][0], shape_pts[ni][1])
        avg_stop_to_shape_m = total / len(requested_coords)
    is_tight = (
        start_anchor_dist_m <= 60.0
        and end_anchor_dist_m <= 60.0
        and monotonic_violations == 0
    )
    return {
        "start_anchor_dist_m": float(start_anchor_dist_m),
        "end_anchor_dist_m": float(end_anchor_dist_m),
        "avg_stop_to_shape_m": float(avg_stop_to_shape_m),
        "monotonic_violations": int(monotonic_violations),
        "is_direction_tight": bool(is_tight),
    }


def _pin_shape_endpoints(requested_coords: List[LonLat], shape_pts: List[LonLat]) -> List[LonLat]:
    """
    Force geometry endpoints to match first/last requested stop coordinates.
    """
    if len(shape_pts) < 2:
        return shape_pts
    pinned = list(shape_pts)
    pinned[0] = requested_coords[0]
    pinned[-1] = requested_coords[-1]
    return pinned


# =============================================================================
# Presets (DB-backed if available, fallback if not)
# =============================================================================

@dataclass(frozen=True)
class ValhallaPreset:
    name: str
    costing_options: Dict[str, Any]
    active: bool = True
    notes: Optional[str] = None


DEFAULT_PRESETS: List[ValhallaPreset] = [
    ValhallaPreset(
        name="bus_low_highways",
        costing_options={VALHALLA_COSTING: {"use_highways": 0.05, "use_tolls": 0.0}},
        active=True,
        notes="Avoid highways strongly",
    ),
    ValhallaPreset(
        name="bus_balanced",
        costing_options={VALHALLA_COSTING: {"use_highways": 0.20, "use_tolls": 0.0}},
        active=True,
        notes="Balanced",
    ),
    ValhallaPreset(
        name="bus_more_highways",
        costing_options={VALHALLA_COSTING: {"use_highways": 0.35, "use_tolls": 0.0}},
        active=True,
        notes="Use highways more",
    ),
]


# =============================================================================
# Phase 3 Client (console-facing)
# =============================================================================

class Phase3Client:
    """
    Single-file Phase 3 client for Streamlit Console.

    Implements what your tabs expect:

    Jobs tab:
      - create_route_job()
      - list_route_jobs()
      - get_route_job()
      - health()

    Stop Prior tab:
      - fetch_relation_overpass_json()
      - replace_relation_stop_prior()
      - get_relation_stop_prior()
      - match_prior_to_canonical()

    Geometry tab:
      - list_valhalla_presets()
      - build_geometry_candidates_from_stop_sequence()
      - list_geometry_sets()
      - list_geometry_candidates()

    Approve/Publish tab:
      - dashboard_snapshot()
    """

    def __init__(
        self,
        *,
        overpass_url: Optional[str] = None,
    ) -> None:
        self.overpass_url = overpass_url or _env("OVERPASS_URL", "http://127.0.0.1:12346/api/interpreter")
        self._direction_schema_ready = False
        self._trash_schema_ready = False
        self._sequence_resolution_schema_ready = False
        self._manual_sequence_schema_ready = False
        self._extractor_review_schema_ready = False
        self._route_review_schema_ready = False
        self._inverse_completion_schema_ready = False
        self._geometry_stop_recovery_schema_ready = False
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        self._ensure_sequence_resolution_schema()

    def raw_query(self, sql: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        """Execute a read-only SQL query and return rows as dicts."""
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql, params or ())
                return [dict(r) for r in cur.fetchall()]

    def _collect_phase3_snapshot(
        self,
        *,
        route_id: str,
        prior_rows: Optional[List[Dict[str, Any]]] = None,
        match_radius_m: Optional[float] = None,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "route_id": str(route_id),
            "prior_stop_count": None,
            "matched_count": None,
            "unmatched_count": None,
            "ambiguous_count": None,
            "sequence_gate_pass": None,
            "area_key": None,
            "bbox": None,
            "known_ref": None,
            "service_route_id": None,
            "direction_id": None,
        }
        try:
            job = self.get_route_job(uuid.UUID(str(route_id)))
            if job:
                out["area_key"] = job.get("area_key")
                out["bbox"] = job.get("bbox")
                out["known_ref"] = job.get("known_ref")
                out["service_route_id"] = job.get("service_route_id")
                out["direction_id"] = job.get("direction_id")
                if job.get("osm_relation_id") is not None:
                    out["osm_relation_id"] = job.get("osm_relation_id")
        except Exception:
            pass

        try:
            pr = list(prior_rows or self.get_relation_stop_prior(uuid.UUID(str(route_id))) or [])
            out["prior_stop_count"] = len(pr)
            if pr:
                report = self.get_prior_match_report(
                    uuid.UUID(str(route_id)),
                    radius_m=float(match_radius_m or 3.0),
                    prior_rows=pr,
                    write=False,
                )
                out["matched_count"] = int(report.get("matched") or 0)
                out["unmatched_count"] = int(report.get("unmatched") or 0)
                out["ambiguous_count"] = int(report.get("ambiguous") or 0)
                out["sequence_gate_pass"] = bool(report.get("all_matched"))
        except Exception:
            pass
        return out

    def _safe_ai_log_phase3(
        self,
        *,
        stage: str,
        route_id: Optional[str],
        payload: Optional[Dict[str, Any]] = None,
        prior_rows: Optional[List[Dict[str, Any]]] = None,
        warnings: Optional[List[str]] = None,
        notes: Optional[List[str]] = None,
    ) -> None:
        if not callable(_ai_log_phase3_run):
            return
        try:
            rid = str(route_id or "").strip()
            row: Dict[str, Any] = {
                "stage": str(stage),
                "route_id": rid or None,
                "warnings": list(warnings or []),
                "notes": list(notes or []),
            }
            if payload:
                row.update(dict(payload))
            if rid:
                snap = self._collect_phase3_snapshot(
                    route_id=rid,
                    prior_rows=prior_rows,
                    match_radius_m=(float(row["match_radius_m"]) if row.get("match_radius_m") is not None else None),
                )
                for k, v in snap.items():
                    if row.get(k) is None:
                        row[k] = v
            if prior_rows:
                row["prior_rows"] = [dict(r) for r in prior_rows]
            _ai_log_phase3_run(row)
        except Exception:
            # Telemetry should never affect Phase 3 execution.
            pass

    def _safe_ai_log_sequence_edit(
        self,
        *,
        route_id: str,
        edit_type: str,
        row_count: Optional[int] = None,
        warnings: Optional[List[str]] = None,
        notes: Optional[List[str]] = None,
    ) -> None:
        if not callable(_ai_log_phase3_sequence_edit):
            return
        try:
            _ai_log_phase3_sequence_edit(
                {
                    "route_id": str(route_id),
                    "edit_type": str(edit_type or "replace").strip().lower(),
                    "row_count": (int(row_count) if row_count is not None else None),
                    "warnings": list(warnings or []),
                    "notes": list(notes or []),
                }
            )
        except Exception:
            pass

    def _ensure_direction_schema(self) -> None:
        if self._direction_schema_ready:
            return
        sql_path = _phase3_root() / "sql" / "012_route_direction_gate.sql"
        if not sql_path.exists():
            self._direction_schema_ready = True
            return
        sql = sql_path.read_text(encoding="utf-8")
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql)
        self._direction_schema_ready = True

    def _ensure_manual_sequence_schema(self) -> None:
        if self._manual_sequence_schema_ready:
            return
        sql_path = _phase3_root() / "sql" / "021_manual_sequence_builder.sql"
        if not sql_path.exists():
            self._manual_sequence_schema_ready = True
            return
        sql = sql_path.read_text(encoding="utf-8")
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql)
        self._manual_sequence_schema_ready = True

    def _ensure_trash_schema(self) -> None:
        if self._trash_schema_ready:
            return
        sql_path = _phase3_root() / "sql" / "030_route_trash.sql"
        if not sql_path.exists():
            self._trash_schema_ready = True
            return
        sql = sql_path.read_text(encoding="utf-8")
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql)
        self._trash_schema_ready = True

    def _ensure_sequence_resolution_schema(self) -> None:
        if self._sequence_resolution_schema_ready:
            return
        sql_path = _phase3_root() / "sql" / "023_sequence_resolution.sql"
        if not sql_path.exists():
            self._sequence_resolution_schema_ready = True
            return
        sql = sql_path.read_text(encoding="utf-8")
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql)
        self._sequence_resolution_schema_ready = True

    def _ensure_extractor_review_schema(self) -> None:
        if self._extractor_review_schema_ready:
            return
        sql_path = _phase3_root() / "sql" / "013_route_extract_review.sql"
        if not sql_path.exists():
            self._extractor_review_schema_ready = True
            return
        sql = sql_path.read_text(encoding="utf-8")
        try:
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute(sql)
        except Exception:
            # Review persistence must not block extractor execution.
            return
        self._extractor_review_schema_ready = True

    def _ensure_route_review_schema(self) -> None:
        if self._route_review_schema_ready:
            return
        self._ensure_manual_sequence_schema()
        self._ensure_trash_schema()
        sql_path = _phase3_root() / "sql" / "022_route_review_coverage.sql"
        if not sql_path.exists():
            self._route_review_schema_ready = True
            return
        sql = sql_path.read_text(encoding="utf-8")
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql)
        self._route_review_schema_ready = True

    def _ensure_inverse_completion_schema(self) -> None:
        if self._inverse_completion_schema_ready:
            return
        self._ensure_direction_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute("DROP VIEW IF EXISTS route_work.v_direction_readiness")
                for filename in (
                    "024_inverse_direction_status.sql",
                    "025_inverse_direction_proposals.sql",
                    "026_inverse_direction_search.sql",
                ):
                    sql_path = _phase3_root() / "sql" / filename
                    if not sql_path.exists():
                        continue
                    sql = sql_path.read_text(encoding="utf-8")
                    cur.execute(sql)
        self._inverse_completion_schema_ready = True

    def _ensure_geometry_stop_recovery_schema(self) -> None:
        if self._geometry_stop_recovery_schema_ready:
            return
        sql_path = _phase3_root() / "sql" / "027_geometry_stop_recovery.sql"
        if not sql_path.exists():
            self._geometry_stop_recovery_schema_ready = True
            return
        sql = sql_path.read_text(encoding="utf-8")
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql)
        self._geometry_stop_recovery_schema_ready = True

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": True, "db": None, "valhalla": None, "overpass": None}

        # DB ping
        try:
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute("SELECT 1 AS ok")
                    row = cur.fetchone()
            out["db"] = {"ok": bool(row and row.get("ok") == 1)}
        except Exception as e:
            out["ok"] = False
            out["db"] = {"ok": False, "error": str(e)}

        # Valhalla ping (best effort)
        try:
            url = f"{VALHALLA_URL.rstrip('/')}/status"
            r = requests.get(url, timeout=3)
            out["valhalla"] = {"ok": r.status_code < 500, "status_code": r.status_code}
        except Exception as e:
            out["valhalla"] = {"ok": False, "error": str(e)}

        # Overpass ping (best effort)
        try:
            # very small query
            q = '[out:json][timeout:5];node(0,0,0.0001,0.0001);out 1;'
            r = requests.post(self.overpass_url, data=q.encode("utf-8"), timeout=5)
            out["overpass"] = {"ok": r.status_code < 500, "status_code": r.status_code}
        except Exception as e:
            out["overpass"] = {"ok": False, "error": str(e)}

        return out

    def build_phase3_extract_bbox(
        self,
        *,
        bbox: Optional[Dict[str, Any]],
        group_hint: Optional[str] = None,
        priority: Optional[str] = None,
        extra_expand_pct: float = 0.0,
    ) -> Dict[str, float]:
        candidate = _coerce_bbox_dict(bbox)
        if not candidate:
            return {}
        return _normalized_phase3_extract_bbox(
            candidate,
            group_hint=group_hint,
            priority=priority,
            extra_expand_pct=extra_expand_pct,
        )

    def _persist_route_job_extractor_review(
        self,
        *,
        route_id: uuid.UUID | str,
        review_patch: Optional[Dict[str, Any]],
        extractor_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        patch_payload = dict(review_patch or {})
        if not patch_payload and not extractor_source:
            return {}
        self._ensure_extractor_review_schema()

        try:
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute(
                        """
                        SELECT extractor_source, extractor_review
                        FROM route_raw.route_jobs
                        WHERE route_id = %s
                        """,
                        (str(route_id),),
                    )
                    row = cur.fetchone() or {}
                    existing_review = _as_dict(row.get("extractor_review"))
                    merged = _merge_nested_dicts(existing_review, patch_payload)
                    cur.execute(
                        """
                        UPDATE route_raw.route_jobs
                        SET extractor_source = COALESCE(%s, extractor_source),
                            extractor_review = %s::jsonb
                        WHERE route_id = %s
                        """,
                        (
                            (str(extractor_source).strip() if extractor_source else None),
                            json.dumps(merged, ensure_ascii=False),
                            str(route_id),
                        ),
                    )
                    return merged
        except Exception:
            return {}

    def _upsert_route_job_dedupe_cluster(
        self,
        *,
        canonical_route_id: uuid.UUID | str,
        duplicate_route_id: uuid.UUID | str,
        chosen_osm_relation_id: Optional[int],
        dedupe_reason: str,
        evidence: Optional[Dict[str, Any]] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        canonical_txt = str(uuid.UUID(str(canonical_route_id)))
        duplicate_txt = str(uuid.UUID(str(duplicate_route_id)))
        evidence_payload = dict(evidence or {})
        group_row: Dict[str, Any] = {}
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT dedupe_group_id::text AS dedupe_group_id,
                           canonical_route_id::text AS canonical_route_id,
                           group_status
                    FROM route_review.route_job_dedupe_groups
                    WHERE canonical_route_id = %s::uuid
                      AND (
                        (%s IS NULL AND chosen_osm_relation_id IS NULL)
                        OR chosen_osm_relation_id = %s
                      )
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (
                        canonical_txt,
                        chosen_osm_relation_id,
                        chosen_osm_relation_id,
                    ),
                )
                group_row = self._row(cur.fetchone())
                group_id = str(group_row.get("dedupe_group_id") or "").strip()
                if not group_id:
                    cur.execute(
                        """
                        INSERT INTO route_review.route_job_dedupe_groups
                          (canonical_route_id, chosen_osm_relation_id, dedupe_source, dedupe_reason,
                           evidence_json, group_status, reviewable, notes)
                        VALUES
                          (%s::uuid, %s, 'extractor_relation_id', %s,
                           %s::jsonb, 'proposed', TRUE, %s)
                        RETURNING dedupe_group_id::text AS dedupe_group_id,
                                  canonical_route_id::text AS canonical_route_id,
                                  group_status
                        """,
                        (
                            canonical_txt,
                            chosen_osm_relation_id,
                            dedupe_reason,
                            json.dumps(_jsonable(evidence_payload), ensure_ascii=False),
                            notes,
                        ),
                    )
                    group_row = self._row(cur.fetchone())
                    group_id = str(group_row.get("dedupe_group_id") or "").strip()
                else:
                    cur.execute(
                        """
                        UPDATE route_review.route_job_dedupe_groups
                        SET evidence_json = COALESCE(evidence_json, '{}'::jsonb) || %s::jsonb,
                            dedupe_reason = COALESCE(NULLIF(dedupe_reason, ''), %s),
                            notes = COALESCE(%s, notes),
                            group_status = CASE
                              WHEN group_status = 'rejected' THEN group_status
                              ELSE 'proposed'
                            END,
                            updated_at = now()
                        WHERE dedupe_group_id = %s::uuid
                        """,
                        (
                            json.dumps(_jsonable(evidence_payload), ensure_ascii=False),
                            dedupe_reason,
                            notes,
                            group_id,
                        ),
                    )

                for member_route_id, role, status in (
                    (canonical_txt, "canonical", "active"),
                    (duplicate_txt, "duplicate", "suppressed"),
                ):
                    cur.execute(
                        """
                        INSERT INTO route_review.route_job_dedupe_memberships
                          (route_id, dedupe_group_id, canonical_route_id, membership_role, membership_status,
                           review_status, dedupe_reason, reason_json, notes)
                        VALUES
                          (%s::uuid, %s::uuid, %s::uuid, %s, %s,
                           'proposed', %s, %s::jsonb, %s)
                        ON CONFLICT (route_id) DO UPDATE SET
                          dedupe_group_id = EXCLUDED.dedupe_group_id,
                          canonical_route_id = EXCLUDED.canonical_route_id,
                          membership_role = EXCLUDED.membership_role,
                          membership_status = EXCLUDED.membership_status,
                          review_status = CASE
                            WHEN route_review.route_job_dedupe_memberships.review_status = 'rejected'
                              THEN route_review.route_job_dedupe_memberships.review_status
                            ELSE 'proposed'
                          END,
                          dedupe_reason = EXCLUDED.dedupe_reason,
                          reason_json = EXCLUDED.reason_json,
                          notes = COALESCE(EXCLUDED.notes, route_review.route_job_dedupe_memberships.notes),
                          updated_at = now()
                        """,
                        (
                            member_route_id,
                            group_id,
                            canonical_txt,
                            role,
                            status,
                            dedupe_reason,
                            json.dumps(_jsonable(evidence_payload), ensure_ascii=False),
                            notes,
                        ),
                    )
        return {
            "dedupe_group_id": str(group_row.get("dedupe_group_id") or ""),
            "canonical_route_id": canonical_txt,
            "duplicate_route_id": duplicate_txt,
            "membership_status": "suppressed",
            "review_status": "proposed",
            "dedupe_reason": dedupe_reason,
        }

    def _get_route_job_canonicalization_index(
        self,
        route_ids: Iterable[str],
    ) -> Dict[str, Dict[str, Any]]:
        clean_ids = _coerce_uuid_text_list(route_ids)
        if not clean_ids:
            return {}
        self._ensure_route_review_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      route_id,
                      canonical_route_id,
                      dedupe_group_id,
                      membership_role,
                      membership_status,
                      review_status,
                      dedupe_reason,
                      group_status,
                      reviewable
                    FROM route_review.route_job_canonicalization_v1
                    WHERE route_id::uuid = ANY(%s::uuid[])
                    """,
                    (clean_ids,),
                )
                rows = cur.fetchall() or []
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = self._row(row)
            rid = str(item.get("route_id") or "").strip()
            if rid:
                out[rid] = item
        return out

    @staticmethod
    def _build_extractor_attempt_entry(
        review: Optional[Dict[str, Any]],
        *,
        route_id: uuid.UUID | str,
        deleted_duplicate_route_id: Optional[uuid.UUID | str] = None,
    ) -> Dict[str, Any]:
        payload = _as_dict(review)
        target = _as_dict(payload.get("target"))
        geography = _as_dict(payload.get("geography"))
        hints = _as_dict(payload.get("hints"))
        discover = _as_dict(payload.get("discover"))
        fetch = _as_dict(payload.get("fetch"))
        dedupe = _as_dict(payload.get("dedupe"))
        selection = _as_dict(discover.get("selection_summary") or discover.get("selected_relation_summary"))
        identity = {
            "route_id": str(route_id),
            "batch_id": payload.get("batch_id"),
            "place": target.get("place"),
            "attempt_type": target.get("attempt_type"),
            "route_hint_raw": hints.get("route_hint_raw"),
            "cooperative_hint": hints.get("cooperative_hint"),
            "chosen_osm_relation_id": (
                discover.get("chosen_osm_relation_id")
                or discover.get("osm_relation_id")
                or selection.get("osm_relation_id")
            ),
        }
        return {
            "attempt_key": json.dumps(identity, sort_keys=True, ensure_ascii=True, default=str),
            "source_route_id": str(route_id),
            "deleted_duplicate_route_id": (
                str(deleted_duplicate_route_id).strip() if deleted_duplicate_route_id else None
            ),
            "batch_id": payload.get("batch_id"),
            "source_document": payload.get("source_document"),
            "place": target.get("place"),
            "group": target.get("group"),
            "priority": target.get("priority"),
            "place_bundle": target.get("place_bundle"),
            "seed_origin": target.get("seed_origin"),
            "attempt_type": target.get("attempt_type"),
            "bbox_used": dict(geography.get("bbox_used") or {}) if geography.get("bbox_used") else None,
            "interpretation_source": geography.get("interpretation_source"),
            "route_hint_raw": hints.get("route_hint_raw"),
            "cooperative_hint": hints.get("cooperative_hint"),
            "query_strategy": discover.get("query_strategy"),
            "chosen_osm_relation_id": (
                discover.get("chosen_osm_relation_id")
                or discover.get("osm_relation_id")
                or selection.get("osm_relation_id")
            ),
            "selection_confidence": selection.get("selection_confidence"),
            "top_stop_prior_count": (
                _as_dict(discover.get("candidate_universe_summary")).get("top_stop_prior_count")
                or _as_dict(discover.get("extractor_diagnostics")).get("top_stop_prior_count")
            ),
            "fetch_relation_stored": fetch.get("fetch_relation_stored"),
            "novelty_status": (
                dedupe.get("novelty_status")
                or discover.get("novelty_status")
                or "unknown"
            ),
        }

    def _list_existing_extractor_relation_routes(
        self,
        relation_ids: Iterable[int],
        *,
        exclude_route_id: Optional[uuid.UUID | str] = None,
    ) -> Dict[int, List[Dict[str, Any]]]:
        rels = sorted({int(r) for r in list(relation_ids or []) if _to_int(r) is not None})
        if not rels:
            return {}

        out: Dict[int, List[Dict[str, Any]]] = {}
        exclude_txt = str(exclude_route_id).strip() if exclude_route_id else None
        self._ensure_trash_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      rj.route_id::text AS route_id,
                      rj.created_at,
                      rj.status,
                      rj.extractor_source,
                      rj.extractor_review,
                      rj.chosen_osm_relation_id AS osm_relation_id,
                      (orr.route_id IS NOT NULL) AS raw_relation_available
                    FROM route_raw.active_route_jobs rj
                    LEFT JOIN route_raw.osm_relations_raw orr
                      ON orr.route_id = rj.route_id
                    WHERE rj.extractor_review IS NOT NULL
                      AND rj.chosen_osm_relation_id = ANY(%s)
                      AND (%s IS NULL OR rj.route_id::text <> %s)
                    ORDER BY rj.created_at ASC
                    """,
                    (rels, exclude_txt, exclude_txt),
                )
                rows = cur.fetchall() or []

        for row in rows:
            rel_id = _to_int(row.get("osm_relation_id"))
            if rel_id is None:
                continue
            item = self._extractor_review_summary(self._row(row))
            out.setdefault(int(rel_id), []).append(item)
        return out

    @staticmethod
    def _pick_canonical_extractor_route(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        candidates = [dict(row or {}) for row in list(rows or []) if row]
        if not candidates:
            return {}
        candidates.sort(
            key=lambda row: (
                0 if bool(row.get("raw_relation_available") or row.get("fetch_relation_stored")) else 1,
                -float(row.get("selection_confidence") or 0.0),
                -int(row.get("duplicate_attempt_count") or 0),
                str(row.get("created_at") or ""),
            )
        )
        return candidates[0]

    def _extractor_reuse_guard_profile(self, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        item = dict(row or {})
        attempts = _as_dict_list(item.get("attempt_history"))
        current_snapshot = {
            "source_document": (
                item.get("source_document")
                or item.get("extractor_source")
            ),
            "place": item.get("target_place") or item.get("place"),
            "group": item.get("target_group") or item.get("group"),
            "place_bundle": item.get("target_place_bundle") or item.get("place_bundle"),
            "route_hint_raw": item.get("route_hint") or item.get("route_hint_raw"),
            "bbox_used": _coerce_bbox_dict(item.get("bbox_used")),
            "chosen_osm_relation_id": (
                item.get("selected_osm_relation_id")
                or item.get("chosen_osm_relation_id")
                or item.get("osm_relation_id")
            ),
            "selection_confidence": item.get("selection_confidence"),
        }
        attempts = [*attempts, current_snapshot]

        relation_ids = sorted(
            {
                int(rel_id)
                for rel_id in (
                    _to_int(attempt.get("chosen_osm_relation_id"))
                    for attempt in attempts
                )
                if rel_id is not None
            }
        )
        source_keys = sorted(
            {
                str(source)
                for source in (
                    _source_name(attempt.get("source_document"))
                    for attempt in attempts
                )
                if source
            }
        )
        route_hint_signatures = sorted(
            {
                str(sig)
                for sig in (
                    _route_hint_signature(attempt.get("route_hint_raw"))
                    for attempt in attempts
                )
                if sig
            }
        )
        place_bundles = sorted(
            {
                str(bundle)
                for bundle in (
                    _normalized_text_key(attempt.get("place_bundle"))
                    for attempt in attempts
                )
                if bundle
            }
        )
        places = sorted(
            {
                str(place)
                for place in (
                    _normalized_text_key(attempt.get("place"))
                    for attempt in attempts
                )
                if place
            }
        )
        groups = sorted(
            {
                str(group)
                for group in (
                    _normalized_text_key(attempt.get("group"))
                    for attempt in attempts
                )
                if group
            }
        )
        bbox_spread_km = _max_bbox_spread_km(attempt.get("bbox_used") for attempt in attempts)
        source_key = (
            _source_name(item.get("extractor_source"))
            or _source_name(item.get("source_document"))
            or (source_keys[-1] if source_keys else None)
        )
        relation_id = _to_int(
            item.get("selected_osm_relation_id")
            or item.get("chosen_osm_relation_id")
            or item.get("osm_relation_id")
        )
        if relation_id is None and relation_ids:
            relation_id = int(relation_ids[-1])

        return {
            "source_key": source_key,
            "relation_id": relation_id,
            "relation_ids": relation_ids,
            "place": item.get("target_place") or item.get("place"),
            "group": item.get("target_group") or item.get("group"),
            "place_bundle": item.get("target_place_bundle") or item.get("place_bundle"),
            "route_hint_raw": item.get("route_hint") or item.get("route_hint_raw"),
            "route_hint_signature": _route_hint_signature(item.get("route_hint") or item.get("route_hint_raw")),
            "bbox_used": _coerce_bbox_dict(item.get("bbox_used")),
            "selection_confidence": _to_float(item.get("selection_confidence")),
            "source_keys": source_keys,
            "route_hint_signatures": route_hint_signatures,
            "place_bundles": place_bundles,
            "places": places,
            "groups": groups,
            "bbox_spread_km": bbox_spread_km,
            "heterogeneous_host": bool(
                len(relation_ids) > 1
                or len(source_keys) > 1
                or len(route_hint_signatures) > 1
                or len(place_bundles) > 1
                or (bbox_spread_km is not None and bbox_spread_km >= 4.0)
            ),
        }

    def _evaluate_extractor_canonical_reuse(
        self,
        *,
        host_row: Dict[str, Any],
        incoming_row: Dict[str, Any],
    ) -> Dict[str, Any]:
        host = self._extractor_reuse_guard_profile(host_row)
        incoming = self._extractor_reuse_guard_profile(incoming_row)
        host_relation_id = _to_int(host.get("relation_id"))
        incoming_relation_id = _to_int(incoming.get("relation_id"))
        source_conflict = bool(
            host.get("source_key")
            and incoming.get("source_key")
            and host.get("source_key") != incoming.get("source_key")
        )
        same_place = bool(
            _normalized_text_equivalent(host.get("place"), incoming.get("place"))
            or _normalized_text_equivalent(host.get("group"), incoming.get("group"))
        )
        same_bundle = _normalized_text_equivalent(host.get("place_bundle"), incoming.get("place_bundle"))
        same_hint_signature = bool(
            host.get("route_hint_signature")
            and incoming.get("route_hint_signature")
            and host.get("route_hint_signature") == incoming.get("route_hint_signature")
        )
        same_hint_text = _normalized_text_equivalent(host.get("route_hint_raw"), incoming.get("route_hint_raw"))
        bbox_distance_km = _bbox_distance_km(host.get("bbox_used"), incoming.get("bbox_used"))
        strong_cross_source_match = bool(
            source_conflict
            and host_relation_id is not None
            and incoming_relation_id is not None
            and host_relation_id == incoming_relation_id
            and (same_hint_signature or same_hint_text)
            and (same_bundle or same_place)
            and (bbox_distance_km is None or bbox_distance_km <= 2.0)
        )

        reasons: List[str] = []
        if (
            host_relation_id is not None
            and incoming_relation_id is not None
            and host_relation_id != incoming_relation_id
        ):
            reasons.append("relation_mismatch")
        if (
            incoming_relation_id is not None
            and any(rel_id != incoming_relation_id for rel_id in list(host.get("relation_ids") or []))
        ):
            reasons.append("heterogeneous_host_relation_ids")
        if bool(host.get("heterogeneous_host")):
            reasons.append("heterogeneous_host")
        if (
            host.get("route_hint_raw")
            and incoming.get("route_hint_raw")
            and not (same_hint_signature or same_hint_text)
        ):
            reasons.append("route_hint_divergence")
        if (
            host.get("place_bundle")
            and incoming.get("place_bundle")
            and not same_bundle
            and not (same_place and (same_hint_signature or same_hint_text))
        ):
            reasons.append("place_bundle_divergence")
        if bbox_distance_km is not None and bbox_distance_km >= 4.0 and not same_place:
            reasons.append("geography_divergence")
        if source_conflict and not strong_cross_source_match:
            reasons.append("cross_source_weak_evidence")

        return {
            "allow_merge": not reasons,
            "reason_codes": list(dict.fromkeys(reasons)),
            "host_relation_id": host_relation_id,
            "incoming_relation_id": incoming_relation_id,
            "host_source_key": host.get("source_key"),
            "incoming_source_key": incoming.get("source_key"),
            "same_hint_signature": bool(same_hint_signature),
            "same_hint_text": bool(same_hint_text),
            "same_place": bool(same_place),
            "same_place_bundle": bool(same_bundle),
            "bbox_distance_km": bbox_distance_km,
            "host_heterogeneous": bool(host.get("heterogeneous_host")),
            "strong_cross_source_match": bool(strong_cross_source_match),
        }

    def _annotate_relation_candidate_novelty(
        self,
        *,
        candidate_rows: Iterable[Dict[str, Any]],
        current_route_id: Optional[uuid.UUID | str] = None,
    ) -> List[Dict[str, Any]]:
        rows = [dict(row or {}) for row in list(candidate_rows or []) if isinstance(row, dict)]
        try:
            usage_map = self._list_existing_extractor_relation_routes(
                [_to_int(row.get("osm_relation_id")) for row in rows if _to_int(row.get("osm_relation_id")) is not None],
                exclude_route_id=current_route_id,
            )
        except Exception:
            usage_map = {}
        out: List[Dict[str, Any]] = []
        for row in rows:
            rel_id = _to_int(row.get("osm_relation_id"))
            existing = list(usage_map.get(int(rel_id), [])) if rel_id is not None else []
            item = dict(row)
            item["existing_relation_usage_count"] = int(len(existing))
            item["existing_relation_route_ids"] = [str(r.get("route_id")) for r in existing if str(r.get("route_id") or "").strip()]
            item["raw_relation_available_elsewhere"] = bool(
                any(bool(r.get("raw_relation_available") or r.get("fetch_relation_stored")) for r in existing)
            )
            item["relation_novelty"] = "novel" if not existing else "already_stored"
            out.append(item)
        return out

    def _resolve_extractor_candidate_novelty(
        self,
        *,
        route_id: uuid.UUID | str,
        candidate_rows: Iterable[Dict[str, Any]],
        chosen_relation_id: Optional[int],
        reuse_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        annotated = self._annotate_relation_candidate_novelty(
            candidate_rows=candidate_rows,
            current_route_id=route_id,
        )
        if not annotated:
            return {
                "candidate_rows": [],
                "chosen_relation_id": chosen_relation_id,
                "novelty_status": "unknown",
                "selection_override": None,
                "reused_existing_route_id": None,
                "existing_relation_usage_count": 0,
                "existing_relation_route_ids": [],
            }

        by_rel = {
            int(row["osm_relation_id"]): row
            for row in annotated
            if _to_int(row.get("osm_relation_id")) is not None
        }
        selected = by_rel.get(int(chosen_relation_id)) if _to_int(chosen_relation_id) is not None else None
        if selected is None:
            selected = sorted(
                annotated,
                key=lambda row: (
                    0 if bool(row.get("is_chosen")) else 1,
                    int(row.get("selection_rank")) if row.get("selection_rank") is not None else 10**6,
                    -float(row.get("score") or 0.0),
                ),
            )[0]
            chosen_relation_id = int(selected.get("osm_relation_id"))

        existing_count = int(selected.get("existing_relation_usage_count") or 0)
        existing_route_ids = [str(v) for v in list(selected.get("existing_relation_route_ids") or []) if str(v or "").strip()]
        if existing_count <= 0:
            return {
                "candidate_rows": annotated,
                "chosen_relation_id": int(chosen_relation_id) if _to_int(chosen_relation_id) is not None else None,
                "novelty_status": "novel_relation_selected",
                "selection_override": None,
                "reused_existing_route_id": None,
                "existing_relation_usage_count": 0,
                "existing_relation_route_ids": [],
            }

        top_score = float(selected.get("score") or 0.0)
        top_stop_prior = int(selected.get("stop_prior_count") or 0)
        novel_candidates = [
            dict(row)
            for row in annotated
            if int(row.get("existing_relation_usage_count") or 0) <= 0
            and _to_int(row.get("osm_relation_id")) is not None
        ]
        qualifying: List[Dict[str, Any]] = []
        for row in novel_candidates:
            score = float(row.get("score") or 0.0)
            stop_prior = int(row.get("stop_prior_count") or 0)
            selection_rank = int(row.get("selection_rank") or 10**6)
            soft = list(row.get("matched_soft_signals") or [])
            if stop_prior <= 0 and not soft and selection_rank > 5:
                continue
            if (
                score >= (top_score * 0.65)
                or stop_prior >= max(1, int(math.ceil(float(top_stop_prior) * 0.35)))
                or bool(soft)
            ):
                qualifying.append(row)

        if qualifying:
            qualifying.sort(
                key=lambda row: (
                    int(row.get("selection_rank") or 10**6),
                    -float(row.get("score") or 0.0),
                    -int(row.get("stop_prior_count") or 0),
                )
            )
            replacement = dict(qualifying[0])
            replacement_relation_id = int(replacement["osm_relation_id"])
            if replacement_relation_id != int(selected["osm_relation_id"]):
                try:
                    self.set_chosen_relation(uuid.UUID(str(route_id)), replacement_relation_id)
                except Exception:
                    pass
                for row in annotated:
                    row["is_chosen"] = bool(int(row.get("osm_relation_id") or 0) == replacement_relation_id)
                return {
                    "candidate_rows": annotated,
                    "chosen_relation_id": replacement_relation_id,
                    "novelty_status": "novel_alternative_selected",
                    "selection_override": {
                        "replaced_duplicate_relation_id": int(selected["osm_relation_id"]),
                        "replacement_relation_id": replacement_relation_id,
                        "replacement_selection_rank": replacement.get("selection_rank"),
                        "replacement_selection_confidence": replacement.get("selection_confidence"),
                    },
                    "reused_existing_route_id": None,
                    "existing_relation_usage_count": existing_count,
                    "existing_relation_route_ids": existing_route_ids,
            }

        existing_routes = self._list_existing_extractor_relation_routes(
            [int(selected["osm_relation_id"])],
            exclude_route_id=route_id,
        ).get(int(selected["osm_relation_id"]), [])
        incoming_context = {
            "route_id": str(route_id),
            "extractor_source": _source_name(_as_dict(reuse_context).get("source_document")),
            "source_document": _as_dict(reuse_context).get("source_document"),
            "selected_osm_relation_id": int(selected["osm_relation_id"]),
            "target_place": _as_dict(reuse_context).get("place"),
            "target_group": _as_dict(reuse_context).get("group"),
            "target_place_bundle": _as_dict(reuse_context).get("place_bundle"),
            "route_hint": _as_dict(reuse_context).get("route_hint_raw"),
            "bbox_used": _coerce_bbox_dict(_as_dict(reuse_context).get("bbox_used")),
            "selection_confidence": (
                _to_float(_as_dict(reuse_context).get("selection_confidence"))
                or _to_float(selected.get("selection_confidence"))
            ),
        }
        compatible_routes = [
            dict(row)
            for row in existing_routes
            if bool(
                self._evaluate_extractor_canonical_reuse(
                    host_row=dict(row or {}),
                    incoming_row=incoming_context,
                ).get("allow_merge")
            )
        ]
        if not compatible_routes:
            return {
                "candidate_rows": annotated,
                "chosen_relation_id": int(selected["osm_relation_id"]),
                "novelty_status": "separate_review_required",
                "selection_override": None,
                "reused_existing_route_id": None,
                "existing_relation_usage_count": existing_count,
                "existing_relation_route_ids": existing_route_ids,
            }
        canonical = self._pick_canonical_extractor_route(compatible_routes)
        return {
            "candidate_rows": annotated,
            "chosen_relation_id": int(selected["osm_relation_id"]),
            "novelty_status": "duplicate_reused_existing_route",
            "selection_override": None,
            "reused_existing_route_id": str(canonical.get("route_id") or "") or None,
            "existing_relation_usage_count": existing_count,
            "existing_relation_route_ids": existing_route_ids,
        }

    def _merge_duplicate_extractor_attempt(
        self,
        *,
        canonical_route_id: uuid.UUID | str,
        duplicate_route_id: uuid.UUID | str,
        duplicate_review: Dict[str, Any],
    ) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        canonical_job = self.get_route_job(uuid.UUID(str(canonical_route_id))) or {}
        duplicate_job = self.get_route_job(uuid.UUID(str(duplicate_route_id))) or {}
        canonical_review = _as_dict(canonical_job.get("extractor_review"))
        duplicate_review_payload = _merge_nested_dicts(_as_dict(duplicate_job.get("extractor_review")), _as_dict(duplicate_review))
        canonical_entry = self._build_extractor_attempt_entry(
            canonical_review,
            route_id=canonical_route_id,
        )
        duplicate_entry = self._build_extractor_attempt_entry(
            duplicate_review_payload,
            route_id=duplicate_route_id,
            deleted_duplicate_route_id=duplicate_route_id,
        )

        history = _as_dict_list(canonical_review.get("attempt_history"))
        seen = {str(row.get("attempt_key") or "") for row in history if str(row.get("attempt_key") or "").strip()}
        if canonical_entry and str(canonical_entry.get("attempt_key") or "").strip() and canonical_entry["attempt_key"] not in seen:
            history.append(canonical_entry)
            seen.add(canonical_entry["attempt_key"])
        if duplicate_entry and str(duplicate_entry.get("attempt_key") or "").strip() and duplicate_entry["attempt_key"] not in seen:
            history.append(duplicate_entry)
            seen.add(duplicate_entry["attempt_key"])

        dedupe = _as_dict(canonical_review.get("dedupe"))
        duplicate_attempts = _as_dict_list(dedupe.get("duplicate_attempts"))
        duplicate_seen = {
            str(row.get("attempt_key") or "")
            for row in duplicate_attempts
            if str(row.get("attempt_key") or "").strip()
        }
        if duplicate_entry and str(duplicate_entry.get("attempt_key") or "").strip() and duplicate_entry["attempt_key"] not in duplicate_seen:
            duplicate_attempts.append(duplicate_entry)
            duplicate_seen.add(duplicate_entry["attempt_key"])

        suppressed_route_ids = [
            str(v)
            for v in list(dedupe.get("suppressed_duplicate_route_ids") or [])
            if str(v or "").strip()
        ]
        duplicate_route_txt = str(duplicate_route_id).strip()
        if duplicate_route_txt and duplicate_route_txt != str(canonical_route_id):
            suppressed_route_ids.append(duplicate_route_txt)
        suppressed_route_ids = list(dict.fromkeys(suppressed_route_ids))
        existing_route_ids = list(dict.fromkeys([str(canonical_route_id), *suppressed_route_ids]))
        chosen_osm_relation_id = _to_int(
            canonical_job.get("osm_relation_id")
            or duplicate_job.get("osm_relation_id")
            or duplicate_entry.get("chosen_osm_relation_id")
            or canonical_entry.get("chosen_osm_relation_id")
        )
        cluster = self._upsert_route_job_dedupe_cluster(
            canonical_route_id=canonical_route_id,
            duplicate_route_id=duplicate_route_id,
            chosen_osm_relation_id=chosen_osm_relation_id,
            dedupe_reason="same_chosen_osm_relation_id",
            evidence={
                "source": "dedupe_extractor_review_jobs",
                "canonical_route_id": str(canonical_route_id),
                "duplicate_route_id": str(duplicate_route_id),
                "chosen_osm_relation_id": chosen_osm_relation_id,
                "canonical_attempt_key": canonical_entry.get("attempt_key"),
                "duplicate_attempt_key": duplicate_entry.get("attempt_key"),
                "preserved_attempt_history": True,
                "destructive_cleanup": False,
            },
            notes="Non-destructive extractor canonicalization",
        )

        patch = {
            "schema_version": "phase3_extractor_review_v1",
            "success_criterion": "relation_extraction_and_persistence",
            "attempt_history": history[-100:],
            "dedupe": {
                "novelty_status": "duplicate_reused_existing_route",
                "reused_existing_route_id": str(canonical_route_id),
                "canonical_route_id": str(canonical_route_id),
                "dedupe_group_id": cluster.get("dedupe_group_id"),
                "dedupe_membership_role": "canonical",
                "dedupe_membership_status": "active",
                "dedupe_review_status": "proposed",
                "dedupe_group_status": "proposed",
                "dedupe_reviewable": True,
                "destructive_cleanup": False,
                "existing_relation_usage_count": int(len(existing_route_ids)),
                "existing_relation_route_ids": existing_route_ids,
                "duplicate_attempt_count": int(len(duplicate_attempts)),
                "duplicate_attempts": duplicate_attempts[-100:],
                "suppressed_duplicate_route_ids": suppressed_route_ids[-100:],
                "last_duplicate_attempt": duplicate_entry or None,
            },
            "downstream": {
                "step20_required": False,
                "matching_success_required": False,
                "notes": [
                    "downstream_sequence_matching_intentionally_not_used_as_success_criterion",
                ],
            },
        }
        self._persist_route_job_extractor_review(
            route_id=str(canonical_route_id),
            review_patch=patch,
            extractor_source=None,
        )
        if str(duplicate_route_id) != str(canonical_route_id):
            duplicate_attempt_history = _as_dict_list(duplicate_review_payload.get("attempt_history"))
            duplicate_seen = {
                str(row.get("attempt_key") or "")
                for row in duplicate_attempt_history
                if str(row.get("attempt_key") or "").strip()
            }
            if duplicate_entry and str(duplicate_entry.get("attempt_key") or "").strip() and duplicate_entry["attempt_key"] not in duplicate_seen:
                duplicate_attempt_history.append(duplicate_entry)
            self._persist_route_job_extractor_review(
                route_id=str(duplicate_route_id),
                review_patch={
                    "schema_version": "phase3_extractor_review_v1",
                    "attempt_history": duplicate_attempt_history[-100:],
                    "dedupe": {
                        "novelty_status": "duplicate_suppressed_under_canonical_review",
                        "reused_existing_route_id": str(canonical_route_id),
                        "canonical_route_id": str(canonical_route_id),
                        "dedupe_group_id": cluster.get("dedupe_group_id"),
                        "dedupe_membership_role": "duplicate",
                        "dedupe_membership_status": "suppressed",
                        "dedupe_review_status": "proposed",
                        "dedupe_group_status": "proposed",
                        "dedupe_reviewable": True,
                        "destructive_cleanup": False,
                        "existing_relation_usage_count": int(len(existing_route_ids)),
                        "existing_relation_route_ids": existing_route_ids,
                        "suppressed_under_route_id": str(canonical_route_id),
                        "suppressed_duplicate_route_ids": suppressed_route_ids[-100:],
                        "last_duplicate_attempt": duplicate_entry or None,
                    },
                    "downstream": {
                        "step20_required": False,
                        "matching_success_required": False,
                        "notes": [
                            "duplicate_context_preserved_under_non_destructive_canonicalization",
                        ],
                    },
                },
                extractor_source=None,
            )
        return patch

    @staticmethod
    def _extractor_review_summary(job: Dict[str, Any]) -> Dict[str, Any]:
        review = _as_dict(job.get("extractor_review"))
        target = _as_dict(review.get("target"))
        geography = _as_dict(review.get("geography"))
        discover = _as_dict(review.get("discover"))
        fetch = _as_dict(review.get("fetch"))
        dedupe = _as_dict(review.get("dedupe"))
        selection = _as_dict(discover.get("selection_summary") or {})
        universe = _as_dict(discover.get("candidate_universe_summary") or {})
        diagnostics = _as_dict(discover.get("extractor_diagnostics") or {})
        attempt_history = _as_dict_list(review.get("attempt_history"))
        duplicate_attempts = _as_dict_list(dedupe.get("duplicate_attempts"))
        out = dict(job)
        out["target_place"] = target.get("place") or geography.get("place_input")
        out["target_group"] = target.get("group")
        out["target_priority"] = target.get("priority")
        out["target_place_bundle"] = target.get("place_bundle")
        out["target_seed_origin"] = target.get("seed_origin")
        out["target_attempt_type"] = target.get("attempt_type")
        out["catalog_confidence"] = target.get("catalog_confidence")
        out["source_document"] = review.get("source_document")
        out["batch_id"] = review.get("batch_id")
        out["route_hint"] = _as_dict(review.get("hints")).get("route_hint_raw")
        out["cooperative_hint"] = _as_dict(review.get("hints")).get("cooperative_hint")
        out["bbox_used"] = geography.get("bbox_used")
        out["interpretation_source"] = geography.get("interpretation_source")
        out["candidate_universe_count"] = universe.get("candidate_universe_count")
        out["selection_confidence"] = selection.get("selection_confidence")
        out["selection_status"] = selection.get("selection_status")
        out["selected_osm_relation_id"] = (
            selection.get("selected_osm_relation_id")
            or discover.get("chosen_osm_relation_id")
            or out.get("osm_relation_id")
        )
        out["extractor_novelty_status"] = (
            dedupe.get("novelty_status")
            or discover.get("novelty_status")
            or "unknown"
        )
        out["reused_existing_route_id"] = dedupe.get("reused_existing_route_id")
        out["existing_relation_usage_count"] = (
            dedupe.get("existing_relation_usage_count")
            or discover.get("existing_relation_usage_count")
            or 0
        )
        out["canonical_route_id"] = dedupe.get("canonical_route_id")
        out["dedupe_group_id"] = (
            job.get("dedupe_group_id")
            or dedupe.get("dedupe_group_id")
        )
        out["dedupe_membership_role"] = (
            job.get("dedupe_membership_role")
            or job.get("membership_role")
            or dedupe.get("dedupe_membership_role")
            or "canonical"
        )
        out["dedupe_membership_status"] = (
            job.get("dedupe_membership_status")
            or job.get("membership_status")
            or dedupe.get("dedupe_membership_status")
            or "active"
        )
        out["dedupe_review_status"] = (
            job.get("dedupe_review_status")
            or job.get("review_status")
            or dedupe.get("dedupe_review_status")
            or "confirmed"
        )
        out["dedupe_group_status"] = (
            job.get("dedupe_group_status")
            or job.get("group_status")
            or dedupe.get("dedupe_group_status")
            or "confirmed"
        )
        out["dedupe_reviewable"] = bool(
            job.get("dedupe_reviewable")
            if job.get("dedupe_reviewable") is not None
            else dedupe.get("dedupe_reviewable")
            if dedupe.get("dedupe_reviewable") is not None
            else False
        )
        out["is_suppressed_duplicate"] = bool(
            str(out.get("dedupe_membership_role") or "").strip() == "duplicate"
            and str(out.get("dedupe_membership_status") or "").strip() == "suppressed"
        )
        out["canonicalization_status"] = (
            "suppressed_duplicate"
            if out["is_suppressed_duplicate"]
            else "canonical_cluster"
            if str(out.get("dedupe_group_id") or "").strip()
            else "standalone"
        )
        out["duplicate_attempt_count"] = max(
            int(dedupe.get("duplicate_attempt_count") or 0),
            int(len(duplicate_attempts)),
        )
        out["attempt_history_count"] = int(len(attempt_history))
        out["top_stop_prior_count"] = universe.get("top_stop_prior_count") or diagnostics.get("top_stop_prior_count")
        out["extractor_signal_strength"] = diagnostics.get("signal_strength")
        out["fetch_relation_stored"] = fetch.get("fetch_relation_stored")
        out["fetch_status"] = fetch.get("fetch_status")
        out["fetch_status_classification"] = fetch.get("fetch_status_classification")
        out["relation_extraction_success"] = bool(discover.get("relation_extraction_success"))
        out["candidate_preview_count"] = int(len(list(discover.get("candidate_preview") or [])))
        out["candidate_preview"] = [dict(row or {}) for row in list(discover.get("candidate_preview") or [])]
        out["selection_summary"] = dict(selection or {})
        out["candidate_universe_summary"] = dict(universe or {})
        out["extractor_diagnostics"] = dict(diagnostics or {})
        out["attempt_history"] = [dict(row or {}) for row in attempt_history]
        out["duplicate_attempts"] = [dict(row or {}) for row in duplicate_attempts]
        out["extractor_review_ready"] = bool(discover.get("relation_extraction_success"))
        return out

    def list_extractor_review_jobs(
        self,
        *,
        limit: int = 100,
        extractor_source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self._ensure_extractor_review_schema()
        self._ensure_route_review_schema()
        self._ensure_trash_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                sql = """
                    SELECT
                      rj.route_id,
                      rj.status,
                      rj.created_at,
                      rj.created_by,
                      rj.notes,
                      rj.area_key,
                      rj.bbox,
                      rj.known_ref,
                      rj.service_route_id,
                      rj.direction_id,
                      rj.chosen_osm_relation_id AS osm_relation_id,
                      rj.extractor_source,
                      rj.extractor_review,
                      COALESCE(rc.candidate_count, 0) AS relation_candidate_count,
                      (orr.route_id IS NOT NULL) AS raw_relation_available
                    FROM route_raw.active_route_jobs rj
                    LEFT JOIN (
                      SELECT route_id, COUNT(*)::int AS candidate_count
                      FROM route_raw.relation_candidates
                      GROUP BY route_id
                    ) rc ON rc.route_id = rj.route_id
                    LEFT JOIN route_raw.osm_relations_raw orr
                      ON orr.route_id = rj.route_id
                    WHERE rj.extractor_review IS NOT NULL
                """
                params: List[Any] = []
                if extractor_source:
                    sql += " AND (rj.extractor_source = %s OR rj.extractor_review::text ILIKE %s)"
                    params.append(str(extractor_source).strip())
                    params.append(f"%{Path(str(extractor_source).strip()).name}%")
                sql += " ORDER BY rj.created_at DESC LIMIT %s"
                params.append(int(limit))
                cur.execute(sql, tuple(params))
                rows = cur.fetchall() or []
        raw_rows = [self._row(r) for r in rows]
        canonical_index = self._get_route_job_canonicalization_index(
            [
                str(row.get("route_id") or "").strip()
                for row in raw_rows
                if str(row.get("route_id") or "").strip()
            ]
        )
        out: List[Dict[str, Any]] = []
        for row in raw_rows:
            route_id_txt = str(row.get("route_id") or "").strip()
            if route_id_txt and route_id_txt in canonical_index:
                dedupe_row = dict(canonical_index.get(route_id_txt) or {})
                row = {
                    **row,
                    "dedupe_group_id": dedupe_row.get("dedupe_group_id"),
                    "membership_role": dedupe_row.get("membership_role"),
                    "membership_status": dedupe_row.get("membership_status"),
                    "review_status": dedupe_row.get("review_status"),
                    "group_status": dedupe_row.get("group_status"),
                    "dedupe_reviewable": dedupe_row.get("reviewable"),
                }
            out.append(self._extractor_review_summary(row))
        if extractor_source:
            source_name = str(extractor_source).strip()
            out = [row for row in out if _extractor_job_matches_source(row, source_name)]
            out = [_scope_extractor_job_to_source(row, source_name) for row in out]
        return out[: int(limit)]

    def dedupe_extractor_review_jobs(
        self,
        *,
        extractor_source: Optional[str] = None,
        limit_groups: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._ensure_extractor_review_schema()
        self._ensure_route_review_schema()
        self._ensure_trash_schema()
        sql = """
            SELECT
              chosen_osm_relation_id AS osm_relation_id,
              COUNT(*)::int AS duplicate_count
            FROM route_raw.active_route_jobs
            WHERE extractor_review IS NOT NULL
              AND chosen_osm_relation_id IS NOT NULL
        """
        params: List[Any] = []
        if extractor_source:
            sql += " AND extractor_source = %s"
            params.append(str(extractor_source).strip())
        sql += """
            GROUP BY chosen_osm_relation_id
            HAVING COUNT(*) > 1
            ORDER BY duplicate_count DESC, chosen_osm_relation_id ASC
        """
        if limit_groups is not None:
            sql += " LIMIT %s"
            params.append(max(1, int(limit_groups)))

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql, tuple(params))
                groups = cur.fetchall() or []

        merged_route_count = 0
        merged_attempt_count = 0
        touched_canonical_route_ids: List[str] = []
        for group in groups:
            relation_id = _to_int(group.get("osm_relation_id"))
            if relation_id is None:
                continue
            existing = self._list_existing_extractor_relation_routes([relation_id]).get(int(relation_id), [])
            if len(existing) <= 1:
                continue
            remaining = [dict(row or {}) for row in existing if row]
            while remaining:
                canonical_pool = [
                    row
                    for row in remaining
                    if not bool(self._extractor_reuse_guard_profile(row).get("heterogeneous_host"))
                ] or list(remaining)
                canonical = self._pick_canonical_extractor_route(canonical_pool)
                canonical_route_id = str(canonical.get("route_id") or "").strip()
                if not canonical_route_id:
                    break
                touched_any = False
                next_remaining: List[Dict[str, Any]] = []
                for row in remaining:
                    duplicate_route_id = str(row.get("route_id") or "").strip()
                    if not duplicate_route_id:
                        continue
                    if duplicate_route_id == canonical_route_id:
                        continue
                    merge_decision = self._evaluate_extractor_canonical_reuse(
                        host_row=canonical,
                        incoming_row=row,
                    )
                    if not bool(merge_decision.get("allow_merge")):
                        next_remaining.append(dict(row))
                        continue
                    duplicate_review = _as_dict(row.get("extractor_review"))
                    if not duplicate_review:
                        next_remaining.append(dict(row))
                        continue
                    self._merge_duplicate_extractor_attempt(
                        canonical_route_id=canonical_route_id,
                        duplicate_route_id=duplicate_route_id,
                        duplicate_review=duplicate_review,
                    )
                    touched_any = True
                    merged_route_count += 1
                    merged_attempt_count += 1
                if touched_any:
                    touched_canonical_route_ids.append(canonical_route_id)
                remaining = [
                    dict(row)
                    for row in next_remaining
                    if str(row.get("route_id") or "").strip() != canonical_route_id
                ]

        return {
            "extractor_source": (str(extractor_source).strip() if extractor_source else None),
            "duplicate_relation_groups": int(len(groups)),
            "canonicalized_route_count": int(merged_route_count),
            "suppressed_route_count": int(merged_route_count),
            "merged_route_count": int(merged_route_count),
            "merged_attempt_count": int(merged_attempt_count),
            "deleted_route_count": 0,
            "destructive": False,
            "canonical_route_count": int(len(set(touched_canonical_route_ids))),
            "canonical_route_ids": list(dict.fromkeys(touched_canonical_route_ids)),
        }

    def finalize_single_survivor_dedupe_groups(
        self,
        *,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Confirm dedupe groups that now have exactly one surviving active route.

        This fixes a real catalog drift case: a merge/trash pass can remove the original
        canonical route while leaving a surviving active route still marked as
        `membership_status='suppressed'`. In that situation the survivor should become
        the canonical active member rather than remain suppressed in the live catalog.
        """
        self._ensure_route_review_schema()
        actor_name = (
            str(actor).strip()
            if actor
            else os.getenv("USER")
            or os.getenv("USERNAME")
            or "phase3_route_catalog"
        )
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    WITH active_members AS (
                      SELECT
                        g.dedupe_group_id,
                        g.chosen_osm_relation_id,
                        g.canonical_route_id,
                        g.group_status,
                        g.reviewable,
                        m.route_id,
                        m.membership_role,
                        m.membership_status,
                        m.review_status,
                        aj.route_id IS NOT NULL AS is_active
                      FROM route_review.route_job_dedupe_groups g
                      JOIN route_review.route_job_dedupe_memberships m
                        ON m.dedupe_group_id = g.dedupe_group_id
                      LEFT JOIN route_raw.active_route_jobs aj
                        ON aj.route_id = m.route_id
                    ),
                    survivor_groups AS (
                      SELECT
                        dedupe_group_id,
                        chosen_osm_relation_id,
                        canonical_route_id,
                        group_status,
                        reviewable,
                        COUNT(*) FILTER (WHERE is_active) AS active_member_count,
                        MAX(route_id::text) FILTER (WHERE is_active) AS survivor_route_id,
                        BOOL_OR(is_active AND route_id = canonical_route_id) AS canonical_is_active,
                        BOOL_OR(is_active AND membership_status = 'suppressed') AS survivor_was_suppressed
                      FROM active_members
                      GROUP BY dedupe_group_id, chosen_osm_relation_id, canonical_route_id, group_status, reviewable
                      HAVING COUNT(*) FILTER (WHERE is_active) = 1
                    )
                    SELECT
                      sg.dedupe_group_id::text AS dedupe_group_id,
                      sg.chosen_osm_relation_id,
                      sg.canonical_route_id::text AS canonical_route_id,
                      sg.survivor_route_id::text AS survivor_route_id,
                      sg.canonical_is_active,
                      sg.survivor_was_suppressed,
                      sg.group_status,
                      sg.reviewable
                    FROM survivor_groups sg
                    ORDER BY sg.chosen_osm_relation_id NULLS LAST, sg.dedupe_group_id
                    """
                )
                candidate_rows = [self._row(r) for r in (cur.fetchall() or [])]

                updated_groups: List[Dict[str, Any]] = []
                promoted_route_ids: List[str] = []
                confirmed_route_ids: List[str] = []

                for row in candidate_rows:
                    group_id = str(row.get("dedupe_group_id") or "").strip()
                    survivor_route_id = str(row.get("survivor_route_id") or "").strip()
                    if not group_id or not survivor_route_id:
                        continue
                    previous_canonical_route_id = str(row.get("canonical_route_id") or "").strip() or None
                    promoted = previous_canonical_route_id != survivor_route_id

                    cur.execute(
                        """
                        UPDATE route_review.route_job_dedupe_groups
                        SET canonical_route_id = %s::uuid,
                            group_status = 'confirmed',
                            reviewable = FALSE,
                            reviewed_at = now(),
                            reviewed_by = %s,
                            notes = TRIM(BOTH ' ' FROM CONCAT_WS(
                              ' | ',
                              NULLIF(notes, ''),
                              %s
                            )),
                            updated_at = now()
                        WHERE dedupe_group_id = %s::uuid
                        """,
                        (
                            survivor_route_id,
                            actor_name,
                            (
                                "single-survivor dedupe finalized; promoted surviving active route to canonical"
                                if promoted
                                else "single-survivor dedupe finalized"
                            ),
                            group_id,
                        ),
                    )
                    cur.execute(
                        """
                        UPDATE route_review.route_job_dedupe_memberships
                        SET canonical_route_id = %s::uuid,
                            membership_role = CASE
                              WHEN route_id = %s::uuid THEN 'canonical'
                              ELSE 'duplicate'
                            END,
                            membership_status = CASE
                              WHEN route_id = %s::uuid THEN 'active'
                              ELSE membership_status
                            END,
                            review_status = 'confirmed',
                            reviewed_at = now(),
                            reviewed_by = %s,
                            notes = CASE
                              WHEN route_id = %s::uuid THEN TRIM(BOTH ' ' FROM CONCAT_WS(
                                ' | ',
                                NULLIF(notes, ''),
                                %s
                              ))
                              ELSE notes
                            END,
                            updated_at = now()
                        WHERE dedupe_group_id = %s::uuid
                        """,
                        (
                            survivor_route_id,
                            survivor_route_id,
                            survivor_route_id,
                            actor_name,
                            survivor_route_id,
                            (
                                "promoted to canonical survivor after original canonical was no longer active"
                                if promoted
                                else "confirmed as surviving canonical member"
                            ),
                            group_id,
                        ),
                    )

                    updated_groups.append(
                        {
                            "dedupe_group_id": group_id,
                            "chosen_osm_relation_id": row.get("chosen_osm_relation_id"),
                            "previous_canonical_route_id": previous_canonical_route_id,
                            "survivor_route_id": survivor_route_id,
                            "promoted_survivor_to_canonical": promoted,
                            "survivor_was_suppressed": bool(row.get("survivor_was_suppressed")),
                        }
                    )
                    confirmed_route_ids.append(survivor_route_id)
                    if promoted:
                        promoted_route_ids.append(survivor_route_id)

        return {
            "group_count": int(len(updated_groups)),
            "promoted_group_count": int(sum(1 for row in updated_groups if row["promoted_survivor_to_canonical"])),
            "promoted_route_ids": list(dict.fromkeys(promoted_route_ids)),
            "confirmed_route_ids": list(dict.fromkeys(confirmed_route_ids)),
            "groups": updated_groups,
        }

    # -------------------------------------------------------------------------
    # Coverage Review / Catalog
    # -------------------------------------------------------------------------
    @staticmethod
    def list_phase3_catalog_documents() -> List[Dict[str, Any]]:
        catalog_dir = Path(__file__).resolve().parents[3] / "phase3_routes" / "catalogs"
        docs: List[Dict[str, Any]] = []
        for path in sorted(catalog_dir.glob("*phase3_catalog*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            docs.append(
                {
                    "path": str(path),
                    "catalog_id": payload.get("catalog_id") or path.stem,
                    "catalog_name": payload.get("catalog_name") or payload.get("document_name") or path.stem,
                    "region": payload.get("region"),
                    "catalog_version": payload.get("catalog_version"),
                }
            )
        return docs

    @staticmethod
    def _load_phase3_catalog_document(catalog_path: str) -> Dict[str, Any]:
        path = Path(str(catalog_path)).expanduser()
        return dict(json.loads(path.read_text(encoding="utf-8")) or {})

    def _collect_phase3_catalog_expected_routes(
        self,
        *,
        doc: Dict[str, Any],
        source_catalog: str,
    ) -> List[Dict[str, Any]]:
        document = dict(doc or {})
        source_catalog_name = (
            str(document.get("catalog_id") or document.get("catalog_name") or Path(source_catalog).name).strip()
            or Path(source_catalog).name
        )
        def _sector_key_value(value: Any, fallback: str) -> str:
            compact = re.sub(r"[^a-z0-9]+", "_", _normalized_text_key(value)).strip("_")
            return compact or fallback

        default_sector_key = (
            normalize_group_hint_key(document.get("region") or source_catalog_name)
            or _sector_key_value(document.get("region") or source_catalog_name, "")
            or _sector_key_value(Path(source_catalog).stem, "")
            or "unassigned"
        )
        default_sector_label = _pretty_sector_label(document.get("region") or default_sector_key) or "Unassigned"

        place_index: Dict[str, Dict[str, Any]] = {}
        for row in list(document.get("target_places") or []) + list(document.get("priority_places") or []):
            if not isinstance(row, dict):
                continue
            place = str(row.get("name") or "").strip()
            place_key = _normalized_text_key(place)
            if not place_key:
                continue
            sector_key = _sector_key_value(
                row.get("group") or row.get("place_bundle") or default_sector_key,
                default_sector_key,
            )
            place_index[place_key] = {
                "sector_key": sector_key,
                "sector_label": _pretty_sector_label(row.get("group") or row.get("place_bundle") or sector_key)
                or default_sector_label,
                "place_bundle": row.get("place_bundle"),
                "group": row.get("group"),
            }

        items: Dict[str, Dict[str, Any]] = {}

        def _infer_sector_from_hint(route_family_hint: str) -> Tuple[str, str]:
            hint_key = _normalized_text_key(route_family_hint)
            if not hint_key:
                return default_sector_key, default_sector_label
            for place_key, meta in place_index.items():
                if place_key and place_key in hint_key:
                    return (
                        str(meta.get("sector_key") or default_sector_key),
                        str(meta.get("sector_label") or default_sector_label),
                    )
            best_sector_key = default_sector_key
            best_sector_label = default_sector_label
            best_score = 0.0
            for row in items.values():
                score = _route_family_match_score(route_family_hint, [row.get("route_family_hint")])
                if score > best_score:
                    best_score = score
                    best_sector_key = str(row.get("sector_key") or default_sector_key)
                    best_sector_label = str(row.get("sector_label") or default_sector_label)
            return best_sector_key, best_sector_label

        def _register(
            route_family_hint: Any,
            *,
            sector_key: Optional[str] = None,
            sector_label: Optional[str] = None,
            aliases: Optional[Iterable[Any]] = None,
            operator_hints: Optional[Iterable[Any]] = None,
            place_hints: Optional[Iterable[Any]] = None,
            evidence_sources: Optional[Iterable[Any]] = None,
        ) -> None:
            route_family_txt = str(route_family_hint or "").strip()
            if not route_family_txt:
                return
            inferred_sector_key, inferred_sector_label = _infer_sector_from_hint(route_family_txt)
            sector_key_txt = _sector_key_value(sector_key or inferred_sector_key, default_sector_key)
            sector_label_txt = _pretty_sector_label(sector_label or sector_key_txt) or inferred_sector_label or default_sector_label
            start_hint, end_hint, direction_hint = _split_gap_route_family_hint(route_family_txt)
            dedupe_key = _coverage_gap_dedupe_key(
                source_catalog=source_catalog_name,
                sector_key=sector_key_txt,
                route_family_hint=route_family_txt,
                direction_hint=direction_hint,
            )
            row = items.get(dedupe_key)
            if row is None:
                row = {
                    "dedupe_key": dedupe_key,
                    "source_catalog": source_catalog_name,
                    "sector_key": sector_key_txt,
                    "sector_label": sector_label_txt,
                    "route_family_hint": route_family_txt,
                    "known_aliases": [],
                    "operator_hints": [],
                    "place_hints": [],
                    "evidence_sources": [],
                    "start_hint": start_hint,
                    "end_hint": end_hint,
                    "direction_hint": direction_hint,
                }
                items[dedupe_key] = row
            row["known_aliases"] = _dedupe_text_values([*(row.get("known_aliases") or []), route_family_txt, *(list(aliases or []))])
            row["operator_hints"] = _dedupe_text_values([*(row.get("operator_hints") or []), *(list(operator_hints or []))])
            row["place_hints"] = _dedupe_text_values([*(row.get("place_hints") or []), *(list(place_hints or []))])
            row["evidence_sources"] = _dedupe_text_values([*(row.get("evidence_sources") or []), *(list(evidence_sources or []))])
            if not row.get("start_hint") and start_hint:
                row["start_hint"] = start_hint
            if not row.get("end_hint") and end_hint:
                row["end_hint"] = end_hint
            if not row.get("direction_hint") and direction_hint:
                row["direction_hint"] = direction_hint

        for bundle in list(document.get("priority_place_bundles") or document.get("place_bundles") or []):
            if not isinstance(bundle, dict):
                continue
            bundle_sector_key = _sector_key_value(bundle.get("group") or bundle.get("name") or default_sector_key, default_sector_key)
            bundle_sector_label = _pretty_sector_label(bundle.get("group") or bundle.get("name") or bundle_sector_key) or default_sector_label
            bundle_places = list(bundle.get("places") or [])
            bundle_operators = list(bundle.get("operator_hints") or [])
            for route_hint in list(bundle.get("route_hints") or []):
                _register(
                    route_hint,
                    sector_key=bundle_sector_key,
                    sector_label=bundle_sector_label,
                    operator_hints=bundle_operators,
                    place_hints=bundle_places,
                    evidence_sources=[bundle.get("name") or "priority_place_bundle"],
                )

        for combo in list(document.get("seed_extraction_combos") or []):
            if not isinstance(combo, dict):
                continue
            route_hint = str(combo.get("route_hint") or "").strip()
            if not route_hint:
                continue
            combo_sector_key = _sector_key_value(combo.get("group") or combo.get("place_bundle") or default_sector_key, default_sector_key)
            combo_sector_label = _pretty_sector_label(combo.get("group") or combo.get("place_bundle") or combo_sector_key) or default_sector_label
            _register(
                route_hint,
                sector_key=combo_sector_key,
                sector_label=combo_sector_label,
                operator_hints=[combo.get("cooperative_hint") or combo.get("operator_hint")],
                place_hints=[combo.get("place")],
                evidence_sources=[combo.get("seed_origin") or "seed_extraction_combo"],
            )

        for route_hint in list(document.get("route_hint_strings") or []):
            _register(
                route_hint,
                aliases=[route_hint],
                evidence_sources=["route_hint_strings"],
            )

        return list(items.values())

    def list_phase3_global_catalog(
        self,
        *,
        limit: int = 500,
        sector_key: Optional[str] = None,
        include_suppressed: bool = True,
        include_quarantined: bool = False,
        inventory_status: Optional[str] = None,
        route_family_search: Optional[str] = None,
        source_document: Optional[str] = None,
        route_job_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        self._ensure_route_review_schema()
        sql = """
            SELECT *
            FROM route_review.phase3_global_catalog_v1
            WHERE 1=1
        """
        params: List[Any] = []
        route_job_ids_clean = _coerce_uuid_text_list(route_job_ids or [])
        if route_job_ids_clean:
            sql += " AND route_job_id::uuid = ANY(%s::uuid[])"
            params.append(route_job_ids_clean)
        if not include_suppressed:
            sql += " AND COALESCE(dedupe_membership_status, 'active') <> 'suppressed'"
        if not include_quarantined:
            sql += " AND COALESCE(inventory_status, 'active') NOT IN ('test_quarantine', 'merged_duplicate')"
        if inventory_status:
            sql += " AND inventory_status = %s"
            params.append(str(inventory_status).strip())
        if sector_key:
            sql += " AND sector_key = %s"
            params.append(str(sector_key).strip())
        if source_document:
            sql += " AND source_document ILIKE %s"
            params.append(f"%{str(source_document).strip()}%")
        if route_family_search:
            sql += """
                AND (
                  route_family_label ILIKE %s
                  OR COALESCE(service_route_name, '') ILIKE %s
                  OR COALESCE(prod_route_name, '') ILIKE %s
                  OR COALESCE(route_hint, '') ILIKE %s
                  OR COALESCE(target_place, '') ILIKE %s
                  OR COALESCE(known_ref, '') ILIKE %s
                )
            """
            like = f"%{str(route_family_search).strip()}%"
            params.extend([like, like, like, like, like, like])
        sql += " ORDER BY sector_label ASC, route_family_label ASC, route_job_created_at DESC LIMIT %s"
        params.append(max(1, int(limit)))
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def list_phase3_sector_coverage(
        self,
        *,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        self._ensure_route_review_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM route_review.phase3_sector_coverage_v1
                    ORDER BY incomplete_family_count DESC, sector_label ASC
                    LIMIT %s
                    """,
                    (max(1, int(limit)),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def get_phase3_catalog_summary(self) -> Dict[str, Any]:
        coverage_rows = self.list_phase3_sector_coverage(limit=500)
        gap_rows = self.list_phase3_coverage_gaps(limit=5000)
        return {
            "sector_count": int(len(coverage_rows)),
            "route_family_count": int(sum(int(row.get("route_family_count") or 0) for row in coverage_rows)),
            "prod_family_count": int(sum(int(row.get("prod_family_count") or 0) for row in coverage_rows)),
            "incomplete_family_count": int(sum(int(row.get("incomplete_family_count") or 0) for row in coverage_rows)),
            "coverage_gap_count": int(len(gap_rows)),
            "open_gap_count": int(sum(1 for row in gap_rows if str(row.get("resolution_status") or "") == "open")),
            "in_progress_gap_count": int(sum(1 for row in gap_rows if str(row.get("resolution_status") or "") == "in_progress")),
            "resolved_gap_count": int(sum(1 for row in gap_rows if str(row.get("resolution_status") or "") == "resolved")),
        }

    def list_phase3_coverage_gaps(
        self,
        *,
        limit: int = 500,
        sector_key: Optional[str] = None,
        resolution_status: Optional[str] = None,
        effective_classification: Optional[str] = None,
        source_catalog: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        self._ensure_route_review_schema()
        sql = """
            SELECT *
            FROM route_review.phase3_coverage_gap_catalog_v1
            WHERE 1=1
        """
        params: List[Any] = []
        if sector_key:
            sql += " AND sector_key = %s"
            params.append(str(sector_key).strip())
        if resolution_status:
            sql += " AND resolution_status = %s"
            params.append(str(resolution_status).strip())
        if effective_classification:
            sql += " AND effective_classification = %s"
            params.append(str(effective_classification).strip())
        if source_catalog:
            sql += " AND source_catalog = %s"
            params.append(str(source_catalog).strip())
        sql += " ORDER BY sector_label ASC, manual_priority DESC, route_family_hint ASC LIMIT %s"
        params.append(max(1, int(limit)))
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(sql, tuple(params))
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def get_phase3_coverage_gap(self, gap_id: uuid.UUID | str) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM route_review.phase3_coverage_gap_catalog_v1
                    WHERE gap_id::uuid = %s::uuid
                    LIMIT 1
                    """,
                    (str(gap_id),),
                )
                row = cur.fetchone() or {}
        return self._row(row)

    def update_phase3_coverage_gap(
        self,
        *,
        gap_id: uuid.UUID | str,
        operator_override_classification: Optional[str] = None,
        manual_priority: Optional[str] = None,
        resolution_status: Optional[str] = None,
        reviewed_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        assignments: List[str] = []
        params: List[Any] = []
        if operator_override_classification is not None:
            assignments.append("operator_override_classification = %s")
            params.append(str(operator_override_classification).strip() or None)
        if manual_priority is not None:
            assignments.append("manual_priority = %s")
            params.append(str(manual_priority).strip() or None)
        if resolution_status is not None:
            assignments.append("resolution_status = %s")
            params.append(str(resolution_status).strip() or None)
        if reviewed_by is not None:
            assignments.append("reviewed_by = %s")
            params.append(str(reviewed_by).strip() or None)
            assignments.append("reviewed_at = now()")
        if notes is not None:
            assignments.append("notes = %s")
            params.append(str(notes).strip() or None)
        if not assignments:
            return self.get_phase3_coverage_gap(gap_id)
        params.append(str(gap_id))
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    f"""
                    UPDATE route_review.coverage_gaps
                    SET {", ".join(assignments)},
                        updated_at = now()
                    WHERE gap_id = %s::uuid
                    """,
                    tuple(params),
                )
        return self.get_phase3_coverage_gap(gap_id)

    def link_phase3_coverage_gap_to_route(
        self,
        *,
        gap_id: uuid.UUID | str,
        route_id: uuid.UUID | str,
        resolution_status: str = "in_progress",
        reviewed_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        try:
            self._apply_coverage_gap_metadata_to_route_job(
                gap_id=gap_id,
                route_id=route_id,
            )
        except Exception:
            pass
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE route_review.coverage_gaps
                    SET resolved_route_id = %s::uuid,
                        related_route_ids = ARRAY(
                          SELECT DISTINCT x
                          FROM unnest(
                            COALESCE(related_route_ids, ARRAY[]::uuid[]) || ARRAY[%s::uuid]
                          ) AS x
                        ),
                        resolution_status = %s,
                        reviewed_by = COALESCE(%s, reviewed_by),
                        reviewed_at = CASE
                          WHEN %s IS NULL THEN reviewed_at
                          ELSE now()
                        END,
                        notes = COALESCE(%s, notes),
                        updated_at = now()
                    WHERE gap_id = %s::uuid
                    """,
                    (
                        str(route_id),
                        str(route_id),
                        str(resolution_status).strip() or "in_progress",
                        reviewed_by,
                        reviewed_by,
                        notes,
                        str(gap_id),
                    ),
                )
        return self.get_phase3_coverage_gap(gap_id)

    def _apply_coverage_gap_metadata_to_route_job(
        self,
        *,
        gap_id: uuid.UUID | str,
        route_id: uuid.UUID | str,
    ) -> None:
        gap = self.get_phase3_coverage_gap(gap_id)
        if not gap:
            return

        sector_key = str(gap.get("sector_key") or "").strip() or None
        sector_label = str(gap.get("sector_label") or "").strip() or None
        route_family_hint = str(gap.get("route_family_hint") or "").strip() or None
        start_hint = str(gap.get("start_hint") or "").strip() or None
        end_hint = str(gap.get("end_hint") or "").strip() or None
        heuristic_notes = _as_dict(gap.get("heuristic_notes"))
        operator_hints = _dedupe_text_values(heuristic_notes.get("operator_hints") or [])
        cooperative_hint = operator_hints[0] if operator_hints else None

        review_patch: Dict[str, Any] = {
            "coverage_gap": {
                "gap_id": str(gap.get("gap_id") or ""),
                "sector_key": sector_key,
                "sector_label": sector_label,
                "route_family_hint": route_family_hint,
                "start_hint": start_hint,
                "end_hint": end_hint,
            },
            "source_document": str(gap.get("source_catalog") or "").strip() or None,
            "geography": {
                "sector_hint": sector_key,
                "corridor_hint": sector_label,
            },
            "target": {
                "group": sector_key,
                "place_bundle": sector_key,
                "place": end_hint or start_hint,
            },
            "hints": {
                "route_hint_raw": route_family_hint,
                "cooperative_hint": cooperative_hint,
            },
        }
        self._persist_route_job_extractor_review(
            route_id=route_id,
            review_patch=review_patch,
            extractor_source="coverage_gap_manual",
        )

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE route_raw.route_jobs
                    SET area_key = COALESCE(NULLIF(BTRIM(area_key), ''), %s),
                        known_ref = COALESCE(NULLIF(BTRIM(known_ref), ''), %s)
                    WHERE route_id = %s::uuid
                    """,
                    (
                        sector_key,
                        route_family_hint,
                        str(route_id),
                    ),
                )

    def mark_phase3_coverage_gap_resolved(
        self,
        *,
        gap_id: uuid.UUID | str,
        resolved_route_id: Optional[uuid.UUID | str] = None,
        resolved_prod_route_id: Optional[uuid.UUID | str] = None,
        reviewed_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE route_review.coverage_gaps
                    SET resolved_route_id = COALESCE(%s::uuid, resolved_route_id),
                        resolved_prod_route_id = COALESCE(%s::uuid, resolved_prod_route_id),
                        resolution_status = 'resolved',
                        reviewed_by = COALESCE(%s, reviewed_by),
                        reviewed_at = CASE
                          WHEN %s IS NULL THEN reviewed_at
                          ELSE now()
                        END,
                        notes = COALESCE(%s, notes),
                        updated_at = now()
                    WHERE gap_id = %s::uuid
                    """,
                    (
                        str(resolved_route_id) if resolved_route_id else None,
                        str(resolved_prod_route_id) if resolved_prod_route_id else None,
                        reviewed_by,
                        reviewed_by,
                        notes,
                        str(gap_id),
                    ),
                )
        return self.get_phase3_coverage_gap(gap_id)

    def get_phase3_gap_manual_context(
        self,
        gap_id: uuid.UUID | str,
    ) -> Dict[str, Any]:
        gap = self.get_phase3_coverage_gap(gap_id)
        if not gap:
            return {}
        heuristic_notes = _as_dict(gap.get("heuristic_notes"))
        route_family_hint = str(gap.get("route_family_hint") or "").strip()
        start_hint = str(gap.get("start_hint") or "").strip()
        end_hint = str(gap.get("end_hint") or "").strip()
        operator_hints = _dedupe_text_values(heuristic_notes.get("operator_hints") or [])

        start_candidates = self.list_manual_builder_approved_stops(search=start_hint, limit=25) if start_hint else []
        end_candidates = self.list_manual_builder_approved_stops(search=end_hint, limit=25) if end_hint else []
        combined_candidates: List[Dict[str, Any]] = []
        seen_stop_ids: set[str] = set()
        for row in [*start_candidates, *end_candidates]:
            stop_id = str(row.get("stop_id") or "").strip()
            if not stop_id or stop_id in seen_stop_ids:
                continue
            seen_stop_ids.add(stop_id)
            combined_candidates.append(dict(row))

        related_route_ids = _coerce_uuid_text_list(gap.get("related_route_ids") or [])
        if related_route_ids:
            related_catalog_rows = self.list_phase3_global_catalog(
                limit=max(40, len(related_route_ids)),
                include_suppressed=True,
                route_job_ids=related_route_ids,
            )
        else:
            related_catalog_rows = self.list_phase3_global_catalog(
                limit=40,
                sector_key=(str(gap.get("sector_key") or "").strip() or None),
                route_family_search=(route_family_hint or None),
                include_suppressed=True,
            )
        return {
            "gap": gap,
            "name_hint": route_family_hint or None,
            "operator_hint": (operator_hints[0] if operator_hints else None),
            "variant_hint": str(gap.get("direction_hint") or "").strip() or None,
            "recommended_stop_ids": [str(row.get("stop_id") or "") for row in combined_candidates if str(row.get("stop_id") or "").strip()],
            "start_stop_candidates": start_candidates,
            "end_stop_candidates": end_candidates,
            "recommended_stops": combined_candidates,
            "related_catalog_rows": related_catalog_rows,
            "heuristic_notes": heuristic_notes,
            "recommended_next_action": gap.get("recommended_next_action"),
        }

    def sync_phase3_coverage_gaps(
        self,
        *,
        catalog_paths: Optional[Iterable[str]] = None,
        limit_per_catalog: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        catalog_docs = list(catalog_paths or [row.get("path") for row in self.list_phase3_catalog_documents()])
        observed_rows = self.list_phase3_global_catalog(limit=20000, include_suppressed=True)
        synced_gaps: List[Dict[str, Any]] = []
        per_catalog_summary: List[Dict[str, Any]] = []

        for raw_path in catalog_docs:
            catalog_path = str(raw_path or "").strip()
            if not catalog_path:
                continue
            try:
                doc = self._load_phase3_catalog_document(catalog_path)
            except Exception:
                continue
            expected_rows = self._collect_phase3_catalog_expected_routes(
                doc=doc,
                source_catalog=catalog_path,
            )
            if limit_per_catalog is not None:
                expected_rows = expected_rows[: max(1, int(limit_per_catalog))]
            catalog_name = str(doc.get("catalog_id") or doc.get("catalog_name") or Path(catalog_path).name).strip()
            synced_for_catalog = 0
            open_for_catalog = 0

            for expected in expected_rows:
                route_family_hint = str(expected.get("route_family_hint") or "").strip()
                sector_key = str(expected.get("sector_key") or "").strip() or "unassigned"
                if not route_family_hint:
                    continue
                related_rows: List[Dict[str, Any]] = []
                for observed in observed_rows:
                    observed_route_values = [
                        observed.get("route_family_label"),
                        observed.get("service_route_name"),
                        observed.get("route_hint"),
                        observed.get("target_place"),
                        observed.get("known_ref"),
                        observed.get("service_route_ref"),
                    ]
                    route_score = _route_family_match_score(route_family_hint, observed_route_values)
                    if route_score < 0.58:
                        continue
                    observed_sector_values = [
                        observed.get("sector_key"),
                        observed.get("sector_label"),
                        observed.get("target_group"),
                        observed.get("target_place_bundle"),
                        observed.get("area_key"),
                    ]
                    sector_score = _route_family_match_score(sector_key, observed_sector_values)
                    if route_score < 0.9 and sector_score < 0.45:
                        continue
                    row = dict(observed)
                    row["route_family_match_score"] = route_score
                    row["sector_match_score"] = sector_score
                    related_rows.append(row)

                related_rows.sort(
                    key=lambda row: (
                        -float(row.get("route_family_match_score") or 0.0),
                        -float(row.get("sector_match_score") or 0.0),
                        0 if str(row.get("prod_status") or "") == "in_prod" else 1,
                        0 if str(row.get("approval_status") or "") in {"approved", "prod"} else 1,
                        0 if str(row.get("step05_state") or "") == "complete" else 1,
                    )
                )
                related_route_ids = _coerce_uuid_text_list(row.get("route_job_id") for row in related_rows)
                prod_related = [row for row in related_rows if str(row.get("prod_status") or "") == "in_prod"]
                in_progress_related = [
                    row
                    for row in related_rows
                    if str(row.get("geometry_status") or "") in {"generated", "approved", "prod", "sequence_ready"}
                    or bool(row.get("manual_origin"))
                ]
                extracted_related = [row for row in related_rows if str(row.get("step05_state") or "") == "complete"]
                heuristic_notes = {
                    "operator_hints": list(expected.get("operator_hints") or []),
                    "place_hints": list(expected.get("place_hints") or []),
                    "aliases": list(expected.get("known_aliases") or []),
                    "evidence_sources": list(expected.get("evidence_sources") or []),
                    "related_catalog_rows": [
                        {
                            "route_job_id": row.get("route_job_id"),
                            "route_family_label": row.get("route_family_label"),
                            "step05_state": row.get("step05_state"),
                            "step20_state": row.get("step20_state"),
                            "geometry_status": row.get("geometry_status"),
                            "approval_status": row.get("approval_status"),
                            "prod_status": row.get("prod_status"),
                            "manual_origin": bool(row.get("manual_origin")),
                            "route_family_match_score": row.get("route_family_match_score"),
                            "sector_match_score": row.get("sector_match_score"),
                        }
                        for row in related_rows[:8]
                    ],
                }
                if prod_related:
                    classification_status = "still_extractable"
                    classification_confidence = 0.98
                    resolution_status = "resolved"
                    recommended_next_action = "already_resolved"
                    resolved_route_id = str(prod_related[0].get("route_job_id") or "") or None
                    resolved_prod_route_id = str(prod_related[0].get("route_job_id") or "") or None
                elif in_progress_related:
                    classification_status = "still_extractable"
                    classification_confidence = 0.86
                    resolution_status = "in_progress"
                    recommended_next_action = "continue_existing_phase3_route_job"
                    resolved_route_id = str(in_progress_related[0].get("route_job_id") or "") or None
                    resolved_prod_route_id = None
                elif extracted_related:
                    classification_status = "still_extractable"
                    classification_confidence = 0.74
                    resolution_status = "open"
                    recommended_next_action = "retry_extraction_or_patch_matching"
                    resolved_route_id = None
                    resolved_prod_route_id = None
                elif expected.get("start_hint") and expected.get("end_hint") and (
                    len(list(expected.get("operator_hints") or [])) > 0 or len(list(expected.get("known_aliases") or [])) > 1
                ):
                    classification_status = "still_extractable"
                    classification_confidence = 0.58
                    resolution_status = "open"
                    recommended_next_action = "retry_extraction_with_route_specific_hints"
                    resolved_route_id = None
                    resolved_prod_route_id = None
                else:
                    classification_status = "non_reliably_extractable"
                    classification_confidence = 0.43
                    resolution_status = "open"
                    recommended_next_action = "manual_construct"
                    resolved_route_id = None
                    resolved_prod_route_id = None

                manual_priority = (
                    "low"
                    if resolution_status == "resolved"
                    else "high"
                    if classification_status == "non_reliably_extractable"
                    else "medium"
                )
                evidence_summary = {
                    "observed_route_count": len(related_rows),
                    "observed_prod_count": len(prod_related),
                    "observed_in_progress_count": len(in_progress_related),
                    "observed_extract_count": len(extracted_related),
                    "highest_route_family_match_score": (
                        max(float(row.get("route_family_match_score") or 0.0) for row in related_rows)
                        if related_rows
                        else 0.0
                    ),
                    "highest_sector_match_score": (
                        max(float(row.get("sector_match_score") or 0.0) for row in related_rows)
                        if related_rows
                        else 0.0
                    ),
                }

                with db_conn() as conn:
                    with db_cursor(conn) as cur:
                        cur.execute(
                            """
                            INSERT INTO route_review.coverage_gaps
                              (dedupe_key, source_catalog, sector_key, sector_label, route_family_hint,
                               known_aliases, start_hint, end_hint, direction_hint, evidence_summary,
                               related_route_ids, classification_status, classification_confidence,
                               classification_source, manual_priority, recommended_next_action,
                               heuristic_notes, resolution_status, resolved_route_id, resolved_prod_route_id)
                            VALUES
                              (%s, %s, %s, %s, %s,
                               %s::text[], %s, %s, %s, %s::jsonb,
                               %s::uuid[], %s, %s,
                               'catalog_gap_sync', %s, %s,
                               %s::jsonb, %s, %s::uuid, %s::uuid)
                            ON CONFLICT (dedupe_key) DO UPDATE SET
                              source_catalog = EXCLUDED.source_catalog,
                              sector_key = EXCLUDED.sector_key,
                              sector_label = EXCLUDED.sector_label,
                              route_family_hint = EXCLUDED.route_family_hint,
                              known_aliases = EXCLUDED.known_aliases,
                              start_hint = EXCLUDED.start_hint,
                              end_hint = EXCLUDED.end_hint,
                              direction_hint = EXCLUDED.direction_hint,
                              evidence_summary = EXCLUDED.evidence_summary,
                              related_route_ids = EXCLUDED.related_route_ids,
                              classification_status = EXCLUDED.classification_status,
                              classification_confidence = EXCLUDED.classification_confidence,
                              classification_source = EXCLUDED.classification_source,
                              manual_priority = EXCLUDED.manual_priority,
                              recommended_next_action = EXCLUDED.recommended_next_action,
                              heuristic_notes = EXCLUDED.heuristic_notes,
                              resolution_status = CASE
                                WHEN route_review.coverage_gaps.resolution_status = 'dismissed'
                                  AND EXCLUDED.resolution_status = 'open'
                                  THEN route_review.coverage_gaps.resolution_status
                                ELSE EXCLUDED.resolution_status
                              END,
                              resolved_route_id = COALESCE(EXCLUDED.resolved_route_id, route_review.coverage_gaps.resolved_route_id),
                              resolved_prod_route_id = COALESCE(EXCLUDED.resolved_prod_route_id, route_review.coverage_gaps.resolved_prod_route_id),
                              updated_at = now()
                            RETURNING gap_id::text AS gap_id
                            """,
                            (
                                expected.get("dedupe_key"),
                                catalog_name,
                                sector_key,
                                expected.get("sector_label"),
                                route_family_hint,
                                list(expected.get("known_aliases") or []),
                                expected.get("start_hint"),
                                expected.get("end_hint"),
                                expected.get("direction_hint"),
                                json.dumps(_jsonable(evidence_summary), ensure_ascii=False),
                                related_route_ids,
                                classification_status,
                                classification_confidence,
                                manual_priority,
                                recommended_next_action,
                                json.dumps(_jsonable(heuristic_notes), ensure_ascii=False),
                                resolution_status,
                                resolved_route_id,
                                resolved_prod_route_id,
                            ),
                        )
                        row = cur.fetchone() or {}
                gap_id = str(row.get("gap_id") or "").strip()
                if gap_id:
                    synced_gaps.append(self.get_phase3_coverage_gap(gap_id))
                    synced_for_catalog += 1
                    if resolution_status == "open":
                        open_for_catalog += 1

            per_catalog_summary.append(
                {
                    "source_catalog": catalog_name,
                    "catalog_path": catalog_path,
                    "synced_gap_count": int(synced_for_catalog),
                    "open_gap_count": int(open_for_catalog),
                }
            )

        return {
            "catalog_count": int(len(per_catalog_summary)),
            "synced_gap_count": int(len(synced_gaps)),
            "open_gap_count": int(sum(1 for row in synced_gaps if str(row.get("resolution_status") or "") == "open")),
            "resolved_gap_count": int(sum(1 for row in synced_gaps if str(row.get("resolution_status") or "") == "resolved")),
            "in_progress_gap_count": int(sum(1 for row in synced_gaps if str(row.get("resolution_status") or "") == "in_progress")),
            "catalogs": per_catalog_summary,
        }

    def export_phase3_missing_route_catalogs(
        self,
        *,
        output_dir: Optional[str] = None,
        sector_keys: Optional[Iterable[str]] = None,
        include_resolved: bool = False,
    ) -> Dict[str, Any]:
        self._ensure_route_review_schema()
        out_dir = Path(output_dir).expanduser() if output_dir else _phase3_coverage_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = self.list_phase3_coverage_gaps(limit=10000)
        allowed_sector_keys = {
            str(normalize_group_hint_key(v) or "").strip()
            for v in list(sector_keys or [])
            if str(normalize_group_hint_key(v) or "").strip()
        }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            sector_key = str(row.get("sector_key") or "").strip() or "unassigned"
            if allowed_sector_keys and sector_key not in allowed_sector_keys:
                continue
            if not include_resolved and str(row.get("resolution_status") or "") == "resolved":
                continue
            grouped.setdefault(sector_key, []).append(dict(row))

        files: List[Dict[str, Any]] = []
        for sector_key, sector_rows in grouped.items():
            if not sector_rows:
                continue
            sector_label = str(sector_rows[0].get("sector_label") or _pretty_sector_label(sector_key) or sector_key).strip()
            file_name = f"{sector_key}_missing_routes.json"
            out_path = out_dir / file_name
            payload = {
                "sector_key": sector_key,
                "sector_label": sector_label,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "gap_count": int(len(sector_rows)),
                "items": [
                    {
                        "gap_id": row.get("gap_id"),
                        "sector": row.get("sector_label"),
                        "route_family": row.get("route_family_hint"),
                        "aliases": list(row.get("known_aliases") or []),
                        "likely_start": row.get("start_hint"),
                        "likely_end": row.get("end_hint"),
                        "direction_hint": row.get("direction_hint"),
                        "classification": row.get("effective_classification"),
                        "classification_status": row.get("classification_status"),
                        "classification_confidence": row.get("classification_confidence"),
                        "evidence_summary": dict(row.get("evidence_summary") or {}),
                        "related_extracted_routes": list(row.get("related_route_ids") or []),
                        "recommended_next_action": row.get("recommended_next_action"),
                        "heuristic_notes": dict(row.get("heuristic_notes") or {}),
                        "resolution_status": row.get("resolution_status"),
                        "resolved_route_id": row.get("resolved_route_id"),
                        "resolved_prod_route_id": row.get("resolved_prod_route_id"),
                    }
                    for row in sector_rows
                ],
            }
            out_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            files.append(
                {
                    "sector_key": sector_key,
                    "sector_label": sector_label,
                    "path": str(out_path),
                    "gap_count": int(len(sector_rows)),
                }
            )
        return {
            "output_dir": str(out_dir),
            "file_count": int(len(files)),
            "files": files,
        }

    # -------------------------------------------------------------------------
    # Jobs
    # -------------------------------------------------------------------------
    def list_queue_routes(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        self._ensure_trash_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                    rj.route_id,
                    rj.status,
                    rj.created_at,
                    rj.created_by,
                    rj.notes,
                    rj.area_key,
                    rj.known_ref,
                    rj.chosen_osm_relation_id AS osm_relation_id,

                    (SELECT COUNT(1) FROM route_work.relation_stop_prior rsp WHERE rsp.route_id=rj.route_id) AS n_prior,
                    (SELECT COUNT(1) FROM route_work.stop_sequence_candidate_sets scs WHERE scs.route_id=rj.route_id) AS n_seq_sets,
                    (SELECT COUNT(1) FROM route_work.geometry_candidate_sets gcs WHERE gcs.route_id=rj.route_id) AS n_geom_sets,
                    EXISTS (SELECT 1 FROM route_work.route_approvals ra WHERE ra.route_id=rj.route_id) AS is_approved
                    FROM route_raw.active_route_jobs rj
                    ORDER BY rj.created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def create_service_route(
        self,
        *,
        route_ref: Optional[str] = None,
        route_name: Optional[str] = None,
        operator_name: Optional[str] = None,
        created_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str:
        self._ensure_direction_schema()
        sid = str(uuid.uuid4())
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_raw.service_routes
                      (service_route_id, route_ref, route_name, operator_name, created_by, notes, route_approval_status)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (service_route_id) DO NOTHING
                    """,
                    (
                        sid,
                        route_ref,
                        route_name,
                        operator_name,
                        created_by or os.getenv("USER") or os.getenv("USERNAME") or "console",
                        notes,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO route_raw.service_route_directions
                      (service_route_id, direction_id, direction_approval_status, geom_source)
                    VALUES
                      (%s, 0, 'pending', 'unknown'),
                      (%s, 1, 'pending', 'unknown')
                    ON CONFLICT (service_route_id, direction_id) DO NOTHING
                    """,
                    (sid, sid),
                )
        return sid

    def list_service_routes(self, *, limit: int = 100) -> List[Dict[str, Any]]:
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      sr.service_route_id,
                      sr.route_ref,
                      sr.route_name,
                      sr.operator_name,
                      sr.route_approval_status,
                      sr.created_at,
                      sr.updated_at,
                      CASE WHEN aj0.route_id IS NULL THEN NULL ELSE d0.route_id::text END AS route_id_0,
                      CASE WHEN aj0.route_id IS NULL THEN 0 ELSE COALESCE(d0.phase3_progress_step, 0)::int END AS progress_0,
                      CASE WHEN aj0.route_id IS NULL THEN 'pending' ELSE COALESCE(d0.direction_approval_status, 'pending') END AS direction_status_0,
                      CASE WHEN aj0.route_id IS NULL THEN 'unknown' ELSE COALESCE(d0.geom_source, 'unknown') END AS geom_source_0,
                      CASE WHEN aj1.route_id IS NULL THEN NULL ELSE d1.route_id::text END AS route_id_1,
                      CASE WHEN aj1.route_id IS NULL THEN 0 ELSE COALESCE(d1.phase3_progress_step, 0)::int END AS progress_1,
                      CASE WHEN aj1.route_id IS NULL THEN 'pending' ELSE COALESCE(d1.direction_approval_status, 'pending') END AS direction_status_1,
                      CASE WHEN aj1.route_id IS NULL THEN 'unknown' ELSE COALESCE(d1.geom_source, 'unknown') END AS geom_source_1,
                      EXISTS (
                        SELECT 1
                        FROM route_work.service_route_approvals sra
                        WHERE sra.service_route_id = sr.service_route_id
                      ) AS has_route_approval
                    FROM route_raw.service_routes sr
                    LEFT JOIN route_raw.service_route_directions d0
                      ON d0.service_route_id = sr.service_route_id
                     AND d0.direction_id = 0
                    LEFT JOIN route_raw.active_route_jobs aj0
                      ON aj0.route_id = d0.route_id
                    LEFT JOIN route_raw.service_route_directions d1
                      ON d1.service_route_id = sr.service_route_id
                     AND d1.direction_id = 1
                    LEFT JOIN route_raw.active_route_jobs aj1
                      ON aj1.route_id = d1.route_id
                    ORDER BY sr.updated_at DESC NULLS LAST, sr.created_at DESC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def get_direction_context(self, *, service_route_id: str, direction_id: int) -> Dict[str, Any]:
        self._ensure_direction_schema()
        did = 0 if int(direction_id) <= 0 else 1
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      d.service_route_id::text AS service_route_id,
                      d.direction_id::int AS direction_id,
                      d.route_id::text AS route_id,
                      COALESCE(d.phase3_progress_step, 0)::int AS phase3_progress_step,
                      COALESCE(d.direction_approval_status, 'pending') AS direction_approval_status,
                      COALESCE(d.geom_source, 'unknown') AS geom_source,
                      d.progress_notes,
                      d.approved_at,
                      d.approved_by
                    FROM route_raw.service_route_directions d
                    WHERE d.service_route_id = %s::uuid
                      AND d.direction_id = %s
                    LIMIT 1
                    """,
                    (str(service_route_id), did),
                )
                row = cur.fetchone()
                if row:
                    return self._row(row)
                cur.execute(
                    """
                    INSERT INTO route_raw.service_route_directions
                      (service_route_id, direction_id, direction_approval_status, geom_source)
                    VALUES
                      (%s::uuid, %s, 'pending', 'unknown')
                    RETURNING
                      service_route_id::text AS service_route_id,
                      direction_id::int AS direction_id,
                      route_id::text AS route_id,
                      phase3_progress_step::int AS phase3_progress_step,
                      direction_approval_status,
                      geom_source,
                      progress_notes,
                      approved_at,
                      approved_by
                    """,
                    (str(service_route_id), did),
                )
                inserted = cur.fetchone()
        return self._row(inserted)

    def bind_route_to_direction(
        self,
        *,
        service_route_id: str,
        direction_id: int,
        route_id: str,
        geom_source: str = "observed",
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        sid = str(service_route_id)
        rid = str(route_id)
        did = 0 if int(direction_id) <= 0 else 1
        gsrc = str(geom_source or "observed")
        if gsrc not in {"unknown", "observed", "reversed", "inferred", "manual"}:
            gsrc = "observed"

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_raw.service_route_directions
                      (service_route_id, direction_id, direction_approval_status, geom_source)
                    VALUES
                      (%s::uuid, 0, 'pending', 'unknown'),
                      (%s::uuid, 1, 'pending', 'unknown')
                    ON CONFLICT (service_route_id, direction_id) DO NOTHING
                    """,
                    (sid, sid),
                )
                cur.execute(
                    """
                    SELECT route_id
                    FROM route_raw.active_route_jobs
                    WHERE route_id = %s::uuid
                    LIMIT 1
                    """,
                    (rid,),
                )
                if not cur.fetchone():
                    raise ValueError(f"route_id {rid} is missing or currently trashed")
                cur.execute(
                    """
                    UPDATE route_raw.service_route_directions
                    SET route_id = NULL,
                        updated_at = now()
                    WHERE route_id = %s::uuid
                      AND NOT (service_route_id = %s::uuid AND direction_id = %s)
                    """,
                    (rid, sid, did),
                )
                cur.execute(
                    """
                    INSERT INTO route_raw.service_route_directions
                      (service_route_id, direction_id, route_id, direction_approval_status, geom_source, updated_at)
                    VALUES
                      (%s::uuid, %s, %s::uuid, 'in_progress', %s, now())
                    ON CONFLICT (service_route_id, direction_id)
                    DO UPDATE SET
                      route_id = EXCLUDED.route_id,
                      geom_source = EXCLUDED.geom_source,
                      direction_approval_status = CASE
                        WHEN route_raw.service_route_directions.direction_approval_status = 'approved'
                          THEN 'approved'
                        ELSE 'in_progress'
                      END,
                      updated_at = now()
                    """,
                    (sid, did, rid, gsrc),
                )
                cur.execute(
                    """
                    UPDATE route_raw.route_jobs
                    SET service_route_id = %s::uuid,
                        direction_id = %s
                    WHERE route_id = %s::uuid
                    """,
                    (sid, did, rid),
                )
                patch_route_prod_fields(
                    conn=conn,
                    route_id=rid,
                    fields={
                        "service_route_id": sid,
                        "direction_id": did,
                    },
                    source_type="phase3_client_bind_route_to_direction",
                    pipeline_version=_PIPELINE_VERSION_BIND,
                )
                cur.execute(
                    """
                    UPDATE route_raw.service_routes
                    SET route_approval_status = CASE
                          WHEN route_approval_status = 'approved' THEN 'approved'
                          ELSE 'in_progress'
                        END,
                        updated_at = now()
                    WHERE service_route_id = %s::uuid
                    """,
                    (sid,),
                )
                cur.execute(
                    """
                    SELECT
                      service_route_id::text AS service_route_id,
                      direction_id::int AS direction_id,
                      route_id::text AS route_id,
                      COALESCE(phase3_progress_step, 0)::int AS phase3_progress_step,
                      COALESCE(direction_approval_status, 'pending') AS direction_approval_status,
                      COALESCE(geom_source, 'unknown') AS geom_source
                    FROM route_raw.service_route_directions
                    WHERE service_route_id = %s::uuid
                      AND direction_id = %s
                    LIMIT 1
                    """,
                    (sid, did),
                )
                row = cur.fetchone()
        return self._row(row)

    def _refresh_service_route_status(self, conn, *, service_route_id: str) -> None:
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*)::int AS n_dirs,
                  SUM(CASE WHEN aj.route_id IS NOT NULL THEN 1 ELSE 0 END)::int AS n_bound_routes,
                  SUM(CASE WHEN aj.route_id IS NOT NULL AND COALESCE(d.phase3_progress_step, 0) >= 3 THEN 1 ELSE 0 END)::int AS n_step3_ready,
                  SUM(CASE
                        WHEN aj.route_id IS NOT NULL AND COALESCE(d.direction_approval_status, 'pending') = 'approved' THEN 1
                        WHEN aj.route_id IS NOT NULL AND EXISTS (
                          SELECT 1 FROM route_work.route_approvals ra WHERE ra.route_id = d.route_id
                        ) THEN 1
                        ELSE 0
                      END)::int AS n_direction_approved
                FROM route_raw.service_route_directions d
                LEFT JOIN route_raw.active_route_jobs aj
                  ON aj.route_id = d.route_id
                WHERE d.service_route_id = %s::uuid
                """,
                (str(service_route_id),),
            )
            agg = cur.fetchone() or {}
            n_dirs = int(agg.get("n_dirs") or 0)
            n_bound = int(agg.get("n_bound_routes") or 0)
            n_step3 = int(agg.get("n_step3_ready") or 0)
            n_appr = int(agg.get("n_direction_approved") or 0)

            cur.execute(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM route_work.service_route_approvals sra
                  WHERE sra.service_route_id = %s::uuid
                ) AS has_service_approval
                """,
                (str(service_route_id),),
            )
            has_service_approval = bool((cur.fetchone() or {}).get("has_service_approval"))

            if has_service_approval:
                status = "approved"
            elif n_dirs >= 2 and n_step3 >= 2 and n_appr >= 2:
                status = "ready"
            elif n_bound >= 1:
                status = "in_progress"
            else:
                status = "pending"

            cur.execute(
                """
                UPDATE route_raw.service_routes
                SET route_approval_status = %s,
                    updated_at = now()
                WHERE service_route_id = %s::uuid
                """,
                (status, str(service_route_id)),
            )

    def mark_direction_progress_by_route(
        self,
        *,
        route_id: uuid.UUID | str,
        step: int,
        progress_notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        rid = str(route_id)
        step_i = int(step)
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE route_raw.service_route_directions d
                    SET phase3_progress_step = GREATEST(COALESCE(d.phase3_progress_step, 0), %s),
                        progress_notes = COALESCE(%s, d.progress_notes),
                        direction_approval_status = CASE
                          WHEN d.direction_approval_status = 'approved' THEN 'approved'
                          WHEN GREATEST(COALESCE(d.phase3_progress_step, 0), %s) >= 3 THEN 'ready'
                          ELSE 'in_progress'
                        END,
                        updated_at = now()
                    WHERE d.route_id = %s::uuid
                    RETURNING d.service_route_id::text AS service_route_id, d.direction_id::int AS direction_id, d.route_id::text AS route_id,
                              d.phase3_progress_step::int AS phase3_progress_step, d.direction_approval_status
                    """,
                    (step_i, progress_notes, step_i, rid),
                )
                row = cur.fetchone()
                if row and row.get("service_route_id"):
                    self._refresh_service_route_status(conn, service_route_id=str(row.get("service_route_id")))
        return self._row(row)

    def mark_direction_approved_by_route(
        self,
        *,
        route_id: uuid.UUID | str,
        approved_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        rid = str(route_id)
        reviewer = approved_by or os.getenv("USER") or os.getenv("USERNAME") or "console"
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE route_raw.service_route_directions d
                    SET phase3_progress_step = GREATEST(COALESCE(d.phase3_progress_step, 0), 4),
                        direction_approval_status = 'approved',
                        approved_at = now(),
                        approved_by = %s,
                        updated_at = now()
                    WHERE d.route_id = %s::uuid
                    RETURNING d.service_route_id::text AS service_route_id, d.direction_id::int AS direction_id, d.route_id::text AS route_id,
                              d.phase3_progress_step::int AS phase3_progress_step, d.direction_approval_status, d.approved_at, d.approved_by
                    """,
                    (reviewer, rid),
                )
                row = cur.fetchone()
                if row and row.get("service_route_id"):
                    self._refresh_service_route_status(conn, service_route_id=str(row.get("service_route_id")))
        return self._row(row)

    def get_service_route_gate(self, *, service_route_id: str) -> Dict[str, Any]:
        self._ensure_direction_schema()
        sid = str(service_route_id)
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      sr.service_route_id::text AS service_route_id,
                      sr.route_ref,
                      sr.route_name,
                      sr.route_approval_status,
                      sr.created_at,
                      sr.updated_at
                    FROM route_raw.service_routes sr
                    WHERE sr.service_route_id = %s::uuid
                    LIMIT 1
                    """,
                    (sid,),
                )
                sr = self._row(cur.fetchone())
                cur.execute(
                    """
                    SELECT
                      d.direction_id::int AS direction_id,
                      d.route_id::text AS route_id,
                      COALESCE(d.phase3_progress_step, 0)::int AS phase3_progress_step,
                      COALESCE(d.direction_approval_status, 'pending') AS direction_approval_status,
                      COALESCE(d.geom_source, 'unknown') AS geom_source,
                      d.approved_at,
                      d.approved_by,
                      EXISTS (
                        SELECT 1 FROM route_work.route_approvals ra
                        WHERE ra.route_id = d.route_id
                      ) AS has_route_approval
                    FROM route_raw.service_route_directions d
                    WHERE d.service_route_id = %s::uuid
                    ORDER BY d.direction_id
                    """,
                    (sid,),
                )
                dirs = [self._row(r) for r in (cur.fetchall() or [])]

                cur.execute(
                    """
                    SELECT EXISTS (
                      SELECT 1 FROM route_work.service_route_approvals sra
                      WHERE sra.service_route_id = %s::uuid
                    ) AS has_service_approval
                    """,
                    (sid,),
                )
                has_service_approval = bool((cur.fetchone() or {}).get("has_service_approval"))

        reasons: List[str] = []
        by_dir = {int(d.get("direction_id") or 0): d for d in dirs}
        for did in (0, 1):
            d = by_dir.get(did)
            if not d:
                reasons.append(f"direction {did} row missing")
                continue
            if not d.get("route_id"):
                reasons.append(f"direction {did} has no route_id assigned")
            if int(d.get("phase3_progress_step") or 0) < 3:
                reasons.append(f"direction {did} progress is below Step 30")
            if not bool(d.get("has_route_approval")):
                reasons.append(f"direction {did} has no route approval")

        can_approve = len(reasons) == 0
        return {
            "service_route": sr,
            "directions": dirs,
            "has_service_approval": bool(has_service_approval),
            "can_approve": bool(can_approve),
            "reasons": reasons,
        }

    def list_direction_readiness(
        self,
        *,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        out = _list_direction_readiness_readonly(
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def get_direction_readiness(
        self,
        *,
        service_route_id: Optional[str] = None,
        route_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        out = _get_direction_readiness_readonly(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            route_id=(str(route_id).strip() if route_id else None),
        )
        return asdict(out)

    def analyze_inverse_completion(
        self,
        *,
        service_route_id: Optional[str] = None,
        route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        out = _analyze_inverse_completion_readonly(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            route_id=(str(route_id).strip() if route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def refresh_direction_readiness(
        self,
        *,
        service_route_id: Optional[str] = None,
        route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _refresh_direction_readiness(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            route_id=(str(route_id).strip() if route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def list_persisted_direction_readiness(
        self,
        *,
        service_route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _list_persisted_direction_readiness(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def get_persisted_direction_readiness(
        self,
        *,
        service_route_id: str,
        direction_id: int,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _get_persisted_direction_readiness(
            service_route_id=str(service_route_id).strip(),
            direction_id=int(direction_id),
        )
        return asdict(out)

    def analyze_inverse_proposals(
        self,
        *,
        service_route_id: Optional[str] = None,
        route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _analyze_inverse_proposals(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            route_id=(str(route_id).strip() if route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def refresh_inverse_proposals(
        self,
        *,
        service_route_id: Optional[str] = None,
        route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _refresh_inverse_proposals(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            route_id=(str(route_id).strip() if route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def list_inverse_proposal_rows(
        self,
        *,
        service_route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _list_inverse_proposal_rows(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def get_inverse_proposal_row(
        self,
        *,
        service_route_id: str,
        direction_id: int,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _get_inverse_proposal_row(
            service_route_id=str(service_route_id).strip(),
            direction_id=int(direction_id),
        )
        return asdict(out)

    def dispatch_targeted_inverse_search(
        self,
        *,
        service_route_id: str,
        direction_id: int,
        force: bool = False,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _dispatch_targeted_inverse_search(
            service_route_id=str(service_route_id).strip(),
            direction_id=int(direction_id),
            phase3_client=self,
            force=bool(force),
        )
        return asdict(out)

    def refresh_targeted_inverse_search(
        self,
        *,
        service_route_id: str,
        direction_id: int,
        force: bool = False,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _refresh_targeted_inverse_search(
            service_route_id=str(service_route_id).strip(),
            direction_id=int(direction_id),
            phase3_client=self,
            force=bool(force),
        )
        return asdict(out)

    def list_targeted_inverse_search_results(
        self,
        *,
        service_route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _list_targeted_inverse_search_results(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def get_targeted_inverse_search_result(
        self,
        *,
        service_route_id: str,
        direction_id: int,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _get_targeted_inverse_search_result(
            service_route_id=str(service_route_id).strip(),
            direction_id=int(direction_id),
        )
        return asdict(out)

    def list_inverse_completion_rows(
        self,
        *,
        service_route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _list_inverse_completion_rows(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            limit=max(1, int(limit)),
            include_ready=bool(include_ready),
        )
        return asdict(out)

    def get_inverse_completion_summary(
        self,
        *,
        service_route_id: Optional[str] = None,
        limit: int = 100,
        include_ready: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        return dict(
            _get_inverse_completion_summary(
                service_route_id=(str(service_route_id).strip() if service_route_id else None),
                limit=max(1, int(limit)),
                include_ready=bool(include_ready),
            )
            or {}
        )

    def get_step20_direction_gate(
        self,
        *,
        service_route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
        route_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_inverse_completion_schema()
        out = _get_step20_direction_gate(
            service_route_id=(str(service_route_id).strip() if service_route_id else None),
            direction_id=(int(direction_id) if direction_id in (0, 1) else None),
            route_id=(str(route_id).strip() if route_id else None),
        )
        return asdict(out)

    def ensure_direction_ready_for_step20(
        self,
        *,
        service_route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
        route_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        gate = self.get_step20_direction_gate(
            service_route_id=service_route_id,
            direction_id=direction_id,
            route_id=route_id,
        )
        if bool(gate.get("gate_passed")):
            return gate

        blocker_codes = {
            str(code or "").strip()
            for code in list(gate.get("blocker_codes") or [])
            if str(code or "").strip()
        }
        refresh_service_route_id = str(gate.get("service_route_id") or service_route_id or "").strip() or None
        refresh_route_id = str(gate.get("route_id") or route_id or "").strip() or None
        refresh_direction_id = gate.get("direction_id")
        if refresh_direction_id not in (0, 1):
            refresh_direction_id = int(direction_id) if direction_id in (0, 1) else None

        if "service_route_context_missing" in blocker_codes or (not refresh_service_route_id and not refresh_route_id):
            return gate

        try:
            self.refresh_direction_readiness(
                service_route_id=refresh_service_route_id,
                route_id=refresh_route_id,
                include_ready=True,
            )
        except Exception as exc:
            return {
                **gate,
                "direction_gate_refresh_attempted": True,
                "direction_gate_refresh_error": str(exc),
            }

        refreshed_gate = self.get_step20_direction_gate(
            service_route_id=refresh_service_route_id,
            direction_id=(int(refresh_direction_id) if refresh_direction_id in (0, 1) else None),
            route_id=refresh_route_id,
        )
        return {
            **refreshed_gate,
            "direction_gate_refresh_attempted": True,
            "direction_gate_pre_refresh_code": gate.get("gate_code"),
            "direction_gate_pre_refresh_blocker_codes": list(gate.get("blocker_codes") or []),
        }

    def evaluate_route_pair_merge_proposal(
        self,
        *,
        route_a_id: str,
        route_b_id: str,
        target_service_route_id: Optional[str] = None,
        log_event: bool = True,
    ) -> Dict[str, Any]:
        if RoutePairEvidenceExtractor is None or not callable(score_route_pair_evidence):
            raise RuntimeError("Merge-assist helpers are unavailable.")

        a = str(route_a_id or "").strip()
        b = str(route_b_id or "").strip()
        if not a or not b:
            raise ValueError("route_a_id and route_b_id are required.")
        if a == b:
            raise ValueError("Route pair must contain two different route_ids.")

        extractor = RoutePairEvidenceExtractor()
        evidence = extractor.extract_route_pair_evidence(a, b)
        scored = score_route_pair_evidence(evidence)
        scored["target_service_route_id"] = (str(target_service_route_id).strip() if target_service_route_id else None)
        scored["requires_operator_confirmation"] = True
        scored["proposal_only"] = True

        if log_event:
            warnings = [str(x) for x in (scored.get("review_flags") or []) if str(x).strip()]
            payload = {
                "merge_assist": "route_pair_eval",
                "route_a_id": a,
                "route_b_id": b,
                "target_service_route_id": scored.get("target_service_route_id"),
                "same_route_family_score": scored.get("same_route_family_score"),
                "opposite_direction_score": scored.get("opposite_direction_score"),
                "merge_readiness_score": scored.get("merge_readiness_score"),
                "requires_operator_confirmation": True,
                "review_flags": warnings,
                "gate_state": scored.get("gate_state"),
                "coverage": (scored.get("evidence_breakdown") or {}).get("coverage"),
            }
            self._safe_ai_log_phase3(
                stage="merge_proposal_eval",
                route_id=a,
                payload=payload,
                warnings=warnings,
                notes=["proposal_only_no_auto_bind"],
            )

        return scored

    def list_route_pair_merge_proposals(
        self,
        *,
        route_ids: List[str],
        top_k: int = 8,
        max_pairs: int = 40,
        min_merge_readiness: float = 0.0,
        target_service_route_id: Optional[str] = None,
        log_event: bool = False,
    ) -> Dict[str, Any]:
        if RoutePairEvidenceExtractor is None or not callable(score_route_pair_evidence):
            raise RuntimeError("Merge-assist helpers are unavailable.")

        clean_ids: List[str] = []
        seen: set[str] = set()
        for rid in route_ids or []:
            txt = str(rid or "").strip()
            if not txt:
                continue
            try:
                norm = str(uuid.UUID(txt))
            except Exception:
                continue
            if norm in seen:
                continue
            seen.add(norm)
            clean_ids.append(norm)

        if len(clean_ids) < 2:
            return {
                "target_service_route_id": (str(target_service_route_id).strip() if target_service_route_id else None),
                "pairs_total": 0,
                "pairs_evaluated": 0,
                "proposals": [],
                "requires_operator_confirmation": True,
            }

        extractor = RoutePairEvidenceExtractor()
        profile_cache: Dict[str, Dict[str, Any]] = {}
        for rid in clean_ids:
            profile_cache[rid] = extractor.load_route_profile(rid)

        pair_counter = 0
        proposals: List[Dict[str, Any]] = []
        for a, b in combinations(clean_ids, 2):
            if pair_counter >= int(max_pairs):
                break
            pair_counter += 1
            evidence = extractor.extract_route_pair_evidence_from_profiles(profile_cache.get(a, {}), profile_cache.get(b, {}))
            scored = score_route_pair_evidence(evidence)
            scored["target_service_route_id"] = (str(target_service_route_id).strip() if target_service_route_id else None)
            scored["requires_operator_confirmation"] = True
            scored["proposal_only"] = True
            readiness = float(scored.get("merge_readiness_score") or 0.0)
            if readiness < float(min_merge_readiness):
                continue
            proposals.append(scored)

        proposals.sort(
            key=lambda row: (
                float(row.get("merge_readiness_score") or 0.0),
                float(row.get("opposite_direction_score") or 0.0),
                float(row.get("same_route_family_score") or 0.0),
            ),
            reverse=True,
        )
        proposals = proposals[: max(1, int(top_k))]

        out = {
            "target_service_route_id": (str(target_service_route_id).strip() if target_service_route_id else None),
            "pairs_total": int((len(clean_ids) * (len(clean_ids) - 1)) / 2),
            "pairs_evaluated": int(pair_counter),
            "proposals": proposals,
            "requires_operator_confirmation": True,
            "proposal_only": True,
        }

        if log_event:
            top = proposals[0] if proposals else {}
            warnings = [str(x) for x in (top.get("review_flags") or []) if str(x).strip()]
            self._safe_ai_log_phase3(
                stage="merge_proposal_rank",
                route_id=str(top.get("route_a_id") or clean_ids[0]),
                payload={
                    "merge_assist": "route_pair_rank",
                    "target_service_route_id": out.get("target_service_route_id"),
                    "pairs_total": out.get("pairs_total"),
                    "pairs_evaluated": out.get("pairs_evaluated"),
                    "top_k": int(top_k),
                    "top_pair": {
                        "route_a_id": top.get("route_a_id"),
                        "route_b_id": top.get("route_b_id"),
                        "same_route_family_score": top.get("same_route_family_score"),
                        "opposite_direction_score": top.get("opposite_direction_score"),
                        "merge_readiness_score": top.get("merge_readiness_score"),
                    },
                },
                warnings=warnings,
                notes=["proposal_only_no_auto_bind"],
            )

        return out

    def approve_service_route(
        self,
        *,
        service_route_id: str,
        approved_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        gate = self.get_service_route_gate(service_route_id=str(service_route_id))
        if not gate.get("can_approve"):
            raise RuntimeError("Service-route approval blocked: " + "; ".join(gate.get("reasons") or []))

        sid = str(service_route_id)
        reviewer = approved_by or os.getenv("USER") or os.getenv("USERNAME") or "console"
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_work.service_route_approvals (service_route_id, approved_by, notes)
                    VALUES (%s::uuid, %s, %s)
                    ON CONFLICT (service_route_id) DO UPDATE SET
                      approved_at = now(),
                      approved_by = EXCLUDED.approved_by,
                      notes = EXCLUDED.notes
                    RETURNING approval_id::text AS approval_id, service_route_id::text AS service_route_id, approved_at, approved_by, notes
                    """,
                    (sid, reviewer, notes),
                )
                row = self._row(cur.fetchone())
                cur.execute(
                    """
                    UPDATE route_raw.service_routes
                    SET route_approval_status = 'approved',
                        updated_at = now()
                    WHERE service_route_id = %s::uuid
                    """,
                    (sid,),
                )
        return row

    # ------------------------------------------------------------------
    # Trash / Papelera -- safe deletion with snapshot + audit
    # ------------------------------------------------------------------

    def trash_route_job(
        self,
        route_id: uuid.UUID | str,
        *,
        reason: str,
        workflow: str = "manual_delete",
        actor: str = "operator",
        replaced_by_route_id: uuid.UUID | str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Move a route to trash (soft-delete with snapshot).

        Use this instead of delete_route_job when you want recoverable deletion.
        """
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        rid = uuid.UUID(str(route_id))
        repl = uuid.UUID(str(replaced_by_route_id)) if replaced_by_route_id else None
        with db_conn() as conn:
            already_trashed = _is_route_trashed_impl(conn, rid)
            trash_id = _trash_route_impl(
                conn, rid,
                reason=reason, workflow=workflow, actor=actor,
                replaced_by_route_id=repl, metadata=metadata,
            )
            cleanup = _deactivate_trashed_route_impl(
                conn,
                rid,
                actor=actor,
                workflow=workflow,
                reason=reason,
                trash_id=trash_id,
                metadata=metadata,
            )
        return {
            "route_id": str(rid),
            "trash_id": str(trash_id),
            "status": ("already_trashed" if already_trashed else "trashed"),
            **cleanup,
        }

    def trash_route_for_merge(
        self,
        route_id: uuid.UUID | str,
        canonical_route_id: uuid.UUID | str,
        *,
        reason: str = "merged into canonical route",
        workflow: str = "dedupe_merge",
        actor: str = "operator",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Trash a route as part of a merge/dedupe into a canonical route."""
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        rid = uuid.UUID(str(route_id))
        canonical = uuid.UUID(str(canonical_route_id))
        with db_conn() as conn:
            already_trashed = _is_route_trashed_impl(conn, rid)
            trash_id = _trash_route_for_merge_impl(
                conn, rid, canonical,
                reason=reason, workflow=workflow, actor=actor, metadata=metadata,
            )
            cleanup = _deactivate_trashed_route_impl(
                conn,
                rid,
                actor=actor,
                workflow=workflow,
                reason=reason,
                trash_id=trash_id,
                metadata={
                    **(metadata or {}),
                    "canonical_route_id": str(canonical),
                },
            )
        return {
            "route_id": str(rid),
            "canonical_route_id": str(canonical),
            "trash_id": str(trash_id),
            "status": ("already_trashed" if already_trashed else "trashed"),
            **cleanup,
        }

    def restore_route(
        self,
        trash_id: uuid.UUID | str,
        *,
        actor: str = "operator",
        restore_status: str = "new",
        notes: str | None = None,
    ) -> Dict[str, Any]:
        """Restore a trashed route back to active state."""
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        tid = uuid.UUID(str(trash_id))
        with db_conn() as conn:
            route_id = _restore_route_impl(
                conn, tid,
                actor=actor, restore_status=restore_status, notes=notes,
            )
        job = self.get_route_job(uuid.UUID(str(route_id)), include_trashed=False) or {}
        return {
            "route_id": str(route_id),
            "trash_id": str(tid),
            "status": "restored",
            "service_route_id": job.get("service_route_id"),
            "direction_id": job.get("direction_id"),
        }

    def get_trash_item(self, trash_id: uuid.UUID | str) -> Dict[str, Any] | None:
        """Fetch a single trash item."""
        self._ensure_trash_schema()
        with db_conn() as conn:
            return _get_trash_item_impl(conn, uuid.UUID(str(trash_id)))

    def list_trash(
        self,
        *,
        route_id: uuid.UUID | str | None = None,
        workflow: str | None = None,
        restore_status: str = "trashed",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List trashed routes."""
        self._ensure_trash_schema()
        rid = uuid.UUID(str(route_id)) if route_id else None
        with db_conn() as conn:
            return _list_trash_impl(
                conn, route_id=rid, workflow=workflow,
                restore_status=restore_status, limit=limit,
            )

    def list_delete_events(
        self,
        *,
        route_id: uuid.UUID | str | None = None,
        action_type: str | None = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List delete/audit events."""
        self._ensure_trash_schema()
        rid = uuid.UUID(str(route_id)) if route_id else None
        with db_conn() as conn:
            return _list_delete_events_impl(
                conn, route_id=rid, action_type=action_type, limit=limit,
            )

    def is_route_trashed(self, route_id: uuid.UUID | str) -> bool:
        """Check if a route is currently in trash."""
        self._ensure_trash_schema()
        with db_conn() as conn:
            return _is_route_trashed_impl(conn, uuid.UUID(str(route_id)))

    def delete_route_job(
        self,
        route_id: uuid.UUID | str,
        *,
        delete_route_prod: bool = True,
        delete_node_requests: bool = True,
        delete_all_related: bool = True,
        prune_empty_service_routes: bool = True,
        dry_run: bool = False,
        skip_trash: bool = False,
        purge: bool = False,
        trash_reason: str = "trashed_via_delete_route_job",
        trash_workflow: str = "delete_route_job",
        trash_actor: str = "operator",
    ) -> Dict[str, Any]:
        """Archive a route job by default; only hard-delete when purge/skip_trash is set."""
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        rid = str(route_id)

        # --- Trash-first safety: snapshot before hard delete ---
        # Note: skip_trash=True intentionally bypasses the soft-delete, so we still
        # take a snapshot before any hard-delete (purge OR skip_trash).
        if (purge or skip_trash) and not dry_run:
            try:
                with db_conn() as conn:
                    if not _is_route_trashed_impl(conn, uuid.UUID(rid)):
                        _trash_route_impl(
                            conn, uuid.UUID(rid),
                            reason=trash_reason,
                            workflow=trash_workflow,
                            actor=trash_actor,
                        )
            except Exception as e:
                logging.warning(
                    "Trash snapshot failed for route %s (proceeding with delete): %s",
                    rid, e,
                )

        out: Dict[str, Any] = {
            "route_id": rid,
            "route_job_exists": False,
            "route_already_trashed": False,
            "route_prod_rows": 0,
            "node_review_requests": 0,
            "trashed_route_job": 0,
            "deleted_route_prod_rows": 0,
            "deleted_node_review_requests": 0,
            "deleted_route_job": 0,
            "reset_direction_rows": 0,
            "deleted_service_route_approvals": 0,
            "related_rows_by_table": {},
            "deleted_related_rows_by_table": {},
            "deleted_empty_service_routes": 0,
            "deleted_service_route_ids": [],
            "affected_service_route_ids": [],
            "trash_id": None,
            "status": None,
            "planned_action": ("purge" if (purge or skip_trash) else "trash"),
            "purged": bool(purge or skip_trash),
            "dry_run": bool(dry_run),
        }

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                def _table_exists(name: str) -> bool:
                    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))
                    return bool((cur.fetchone() or {}).get("ok"))

                def _count_route_rows(table_name: str) -> int:
                    cur.execute(
                        f"""
                        SELECT COUNT(*)::int AS n
                        FROM {table_name}
                        WHERE route_id::text = %s
                        """,
                        (rid,),
                    )
                    return int((cur.fetchone() or {}).get("n") or 0)

                related_tables = [
                    "route_work.route_approvals",
                    "route_work.valhalla_run_logs",
                    "route_work.route_context_features",
                    "route_work.geometry_candidate_sets",
                    "route_work.stop_sequence_candidate_sets",
                    "route_work.relation_stop_prior",
                    "route_raw.unmatched_stop_points",
                    "route_raw.relation_candidates",
                    "route_raw.osm_relations_raw",
                ]

                affected_dirs: List[Tuple[str, int]] = []
                affected_service_route_ids: List[str] = []
                if _table_exists("route_raw.service_route_directions"):
                    cur.execute(
                        """
                        SELECT service_route_id::text AS service_route_id, direction_id::int AS direction_id
                        FROM route_raw.service_route_directions
                        WHERE route_id::text = %s
                        ORDER BY direction_id
                        """,
                        (rid,),
                    )
                    affected_dirs = [
                        (str(r.get("service_route_id")), int(r.get("direction_id")))
                        for r in (cur.fetchall() or [])
                        if r.get("service_route_id") is not None and r.get("direction_id") is not None
                    ]
                    affected_service_route_ids = sorted({sid for sid, _ in affected_dirs if sid})
                    out["affected_service_route_ids"] = affected_service_route_ids

                cur.execute(
                    """
                    SELECT 1 AS ok,
                           COALESCE(is_trashed, FALSE) AS is_trashed
                    FROM route_raw.route_jobs
                    WHERE route_id::text = %s
                    LIMIT 1
                    """,
                    (rid,),
                )
                route_job_row = self._row(cur.fetchone()) or {}
                out["route_job_exists"] = bool(route_job_row)
                out["route_already_trashed"] = bool(route_job_row.get("is_trashed"))

                if _table_exists("route_prod.routes"):
                    cur.execute(
                        """
                        SELECT COUNT(*)::int AS n
                        FROM route_prod.routes
                        WHERE route_id::text = %s
                        """,
                        (rid,),
                    )
                    out["route_prod_rows"] = int((cur.fetchone() or {}).get("n") or 0)

                if _table_exists("node_work.node_review_requests"):
                    cur.execute(
                        """
                        SELECT COUNT(*)::int AS n
                        FROM node_work.node_review_requests
                        WHERE route_id::text = %s
                          AND source = 'phase3_route'
                        """,
                        (rid,),
                    )
                    out["node_review_requests"] = int((cur.fetchone() or {}).get("n") or 0)

                if delete_all_related:
                    rel_counts: Dict[str, int] = {}
                    for tbl in related_tables:
                        if _table_exists(tbl):
                            rel_counts[tbl] = _count_route_rows(tbl)
                    out["related_rows_by_table"] = rel_counts

                if dry_run:
                    return out

                if not (purge or skip_trash):
                    trash_out = self.trash_route_job(
                        rid,
                        reason=trash_reason,
                        workflow=trash_workflow,
                        actor=trash_actor,
                        metadata={
                            "requested_delete_route_prod": bool(delete_route_prod),
                            "requested_delete_node_requests": bool(delete_node_requests),
                            "requested_delete_all_related": bool(delete_all_related),
                            "requested_prune_empty_service_routes": bool(prune_empty_service_routes),
                        },
                    )
                    out["trash_id"] = trash_out.get("trash_id")
                    out["status"] = trash_out.get("status")
                    out["trashed_route_job"] = 0 if out["route_already_trashed"] else 1
                    out["deleted_route_prod_rows"] = int(trash_out.get("deleted_route_prod_rows") or 0)
                    out["deleted_node_review_requests"] = int(trash_out.get("deleted_node_review_requests") or 0)
                    out["reset_direction_rows"] = int(trash_out.get("reset_direction_rows") or 0)
                    out["deleted_service_route_approvals"] = int(trash_out.get("deleted_service_route_approvals") or 0)
                    out["affected_service_route_ids"] = list(trash_out.get("affected_service_route_ids") or out["affected_service_route_ids"] or [])
                    out["purged"] = False
                    return out

                if delete_node_requests and _table_exists("node_work.node_review_requests"):
                    cur.execute(
                        """
                        DELETE FROM node_work.node_review_requests
                        WHERE route_id::text = %s
                          AND source = 'phase3_route'
                        """,
                        (rid,),
                    )
                    out["deleted_node_review_requests"] = int(cur.rowcount or 0)

                if delete_route_prod and _table_exists("route_prod.routes"):
                    _del_result = _delete_route_prod_row(
                        conn=conn,
                        route_id=rid,
                        source_type=_SOURCE_TYPE_PHASE3,
                        pipeline_version=_PIPELINE_VERSION_DELETE,
                        reason=trash_reason,
                    )
                    out["deleted_route_prod_rows"] = int(
                        _del_result.rows_affected.get("route_prod.routes", 0) or 0
                    )

                if delete_all_related:
                    deleted_rel: Dict[str, int] = {}
                    for tbl in related_tables:
                        if not _table_exists(tbl):
                            continue
                        cur.execute(
                            f"""
                            DELETE FROM {tbl}
                            WHERE route_id::text = %s
                            """,
                            (rid,),
                        )
                        deleted_rel[tbl] = int(cur.rowcount or 0)
                    out["deleted_related_rows_by_table"] = deleted_rel

                cur.execute(
                    """
                    DELETE FROM route_raw.route_jobs
                    WHERE route_id::text = %s
                    """,
                    (rid,),
                )
                out["deleted_route_job"] = int(cur.rowcount or 0)

                if out["deleted_route_job"] > 0 and affected_dirs:
                    reset_rows = 0
                    for sid, did in affected_dirs:
                        cur.execute(
                            """
                            UPDATE route_raw.service_route_directions
                            SET route_id = NULL,
                                phase3_progress_step = 0,
                                direction_approval_status = 'pending',
                                geom_source = 'unknown',
                                progress_notes = NULL,
                                approved_at = NULL,
                                approved_by = NULL,
                                updated_at = now()
                            WHERE service_route_id = %s::uuid
                              AND direction_id = %s
                            """,
                            (sid, int(did)),
                        )
                        reset_rows += int(cur.rowcount or 0)
                    out["reset_direction_rows"] = reset_rows

                    if _table_exists("route_work.service_route_approvals"):
                        cur.execute(
                            """
                            DELETE FROM route_work.service_route_approvals
                            WHERE service_route_id::text = ANY(%s)
                            """,
                            (affected_service_route_ids,),
                        )
                        out["deleted_service_route_approvals"] = int(cur.rowcount or 0)

                    deleted_sids: List[str] = []
                    if prune_empty_service_routes and _table_exists("route_raw.service_routes"):
                        for sid in affected_service_route_ids:
                            cur.execute(
                                """
                                SELECT COUNT(*)::int AS n_bound
                                FROM route_raw.service_route_directions
                                WHERE service_route_id = %s::uuid
                                  AND route_id IS NOT NULL
                                """,
                                (sid,),
                            )
                            n_bound = int((cur.fetchone() or {}).get("n_bound") or 0)
                            if n_bound == 0:
                                cur.execute(
                                    """
                                    DELETE FROM route_raw.service_routes
                                    WHERE service_route_id = %s::uuid
                                    """,
                                    (sid,),
                                )
                                if int(cur.rowcount or 0) > 0:
                                    deleted_sids.append(sid)
                        out["deleted_empty_service_routes"] = len(deleted_sids)
                        out["deleted_service_route_ids"] = deleted_sids

                    for sid in affected_service_route_ids:
                        if sid in set(out.get("deleted_service_route_ids") or []):
                            continue
                        self._refresh_service_route_status(conn, service_route_id=sid)

        if out["deleted_route_job"] == 0:
            raise RuntimeError(
                "route_id not deleted. If route_prod row exists, enable delete_route_prod."
            )
        out["status"] = "purged"
        return out

    def delete_service_route(
        self,
        service_route_id: uuid.UUID | str,
        *,
        delete_route_prod: bool = True,
        delete_node_requests: bool = True,
        delete_all_related: bool = True,
        dry_run: bool = False,
        purge: bool = False,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        self._ensure_trash_schema()
        sid = str(service_route_id)
        out: Dict[str, Any] = {
            "service_route_id": sid,
            "service_route_exists": False,
            "bound_route_ids": [],
            "extra_route_ids": [],
            "all_route_ids": [],
            "trashed_route_jobs": 0,
            "deleted_route_jobs": 0,
            "deleted_service_route": 0,
            "archived_service_route_shell": 0,
            "deleted_service_route_approvals": 0,
            "route_delete_results": {},
            "planned_action": ("purge" if purge else "trash"),
            "purged": bool(purge),
            "dry_run": bool(dry_run),
        }

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                def _table_exists(name: str) -> bool:
                    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))
                    return bool((cur.fetchone() or {}).get("ok"))

                cur.execute(
                    """
                    SELECT 1 AS ok
                    FROM route_raw.service_routes
                    WHERE service_route_id::text = %s
                    LIMIT 1
                    """,
                    (sid,),
                )
                out["service_route_exists"] = bool(cur.fetchone())
                if not out["service_route_exists"]:
                    if dry_run:
                        return out
                    raise RuntimeError("service_route_id not found")

                cur.execute(
                    """
                    SELECT route_id::text AS route_id
                    FROM route_raw.service_route_directions
                    WHERE service_route_id::text = %s
                      AND route_id IS NOT NULL
                    ORDER BY direction_id
                    """,
                    (sid,),
                )
                bound_route_ids = [str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id")]
                out["bound_route_ids"] = list(bound_route_ids)

                cur.execute(
                    """
                    SELECT route_id::text AS route_id
                    FROM route_raw.active_route_jobs
                    WHERE service_route_id::text = %s
                    ORDER BY created_at DESC NULLS LAST
                    """,
                    (sid,),
                )
                all_jobs = [str(r.get("route_id")) for r in (cur.fetchall() or []) if r.get("route_id")]
                extra_route_ids = [rid for rid in all_jobs if rid not in set(bound_route_ids)]
                out["extra_route_ids"] = extra_route_ids

                all_route_ids: List[str] = []
                for rid in bound_route_ids + extra_route_ids:
                    if rid and rid not in all_route_ids:
                        all_route_ids.append(rid)
                out["all_route_ids"] = all_route_ids

                if _table_exists("route_work.service_route_approvals"):
                    cur.execute(
                        """
                        SELECT COUNT(*)::int AS n
                        FROM route_work.service_route_approvals
                        WHERE service_route_id::text = %s
                        """,
                        (sid,),
                    )
                    out["deleted_service_route_approvals"] = int((cur.fetchone() or {}).get("n") or 0)

                if dry_run:
                    return out

        route_delete_results: Dict[str, Any] = {}
        deleted_route_jobs = 0
        trashed_route_jobs = 0
        for rid in out["all_route_ids"]:
            try:
                r_out = self.delete_route_job(
                    rid,
                    delete_route_prod=bool(delete_route_prod),
                    delete_node_requests=bool(delete_node_requests),
                    delete_all_related=bool(delete_all_related),
                    prune_empty_service_routes=False,
                    dry_run=False,
                    purge=bool(purge),
                )
                route_delete_results[rid] = r_out
                deleted_route_jobs += int(r_out.get("deleted_route_job") or 0)
                trashed_route_jobs += int(r_out.get("trashed_route_job") or 0)
            except Exception as e:
                route_delete_results[rid] = {"error": str(e)}

        out["route_delete_results"] = route_delete_results
        out["deleted_route_jobs"] = deleted_route_jobs
        out["trashed_route_jobs"] = trashed_route_jobs

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", ("route_work.service_route_approvals",))
                if bool((cur.fetchone() or {}).get("ok")):
                    cur.execute(
                        """
                        DELETE FROM route_work.service_route_approvals
                        WHERE service_route_id::text = %s
                        """,
                        (sid,),
                    )
                    out["deleted_service_route_approvals"] = int(cur.rowcount or 0)

                if purge:
                    cur.execute(
                        """
                        DELETE FROM route_raw.service_routes
                        WHERE service_route_id::text = %s
                        """,
                        (sid,),
                    )
                    out["deleted_service_route"] = int(cur.rowcount or 0)
                elif out["all_route_ids"]:
                    # Non-purge (trash) mode: demote to pending instead of hard-deleting.
                    # The shell is preserved so direction slots and restore paths remain intact.
                    cur.execute(
                        """
                        UPDATE route_raw.service_routes
                        SET route_approval_status = 'pending',
                            updated_at = now()
                        WHERE service_route_id::text = %s
                        """,
                        (sid,),
                    )
                    out["archived_service_route_shell"] = int(cur.rowcount or 0)

        if purge and out["deleted_service_route"] == 0:
            raise RuntimeError("service_route_id not deleted")
        return out
    def list_geometry_candidates_flat(self, route_id: uuid.UUID) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                    gc.geometry_candidate_id,
                    gcs.set_id,
                    gcs.route_id,
                    gc.stop_sequence_candidate_id,
                    gc.engine,
                    gc.score,
                    gc.length_m,
                    gc.avg_stop_dist_m,
                    gc.max_stop_dist_m,
                    gc.params,
                    gc.metrics,
                    gc.created_at,
                    ST_AsText(gc.geom) AS geom_wkt
                    FROM route_work.geometry_candidate_sets gcs
                    JOIN route_work.geometry_candidates gc ON gc.set_id = gcs.set_id
                    WHERE gcs.route_id=%s
                    ORDER BY gc.score DESC NULLS LAST, gc.created_at ASC
                    """,
                    (str(route_id),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]
    



    
    def run_step_05_discover(
        self,
        *,
        route_id: Optional[uuid.UUID],  # None = "new"
        service_route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
        bbox: Tuple[float, float, float, float],
        refs: Optional[List[str]] = None,
        operator: Optional[str] = None,
        name: Optional[str] = None,
        max_candidates: int = 50,
        timeout_s: int = 180,
        query_strategy: str = "bbox_first_broad",
        store: bool = True,
        place_input: Optional[str] = None,
        geography_resolution: Optional[Dict[str, Any]] = None,
        route_hint_raw: Optional[str] = None,
        route_hint_contract: Optional[Dict[str, Any]] = None,
        cooperative_hint: Optional[str] = None,
        source_document: Optional[str] = None,
        target_group: Optional[str] = None,
        target_priority: Optional[str] = None,
        target_attempt_type: Optional[str] = None,
        target_place_bundle: Optional[str] = None,
        target_seed_origin: Optional[str] = None,
        target_catalog_confidence: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes scripts/05_discover_relation.py
        Mirrors CLI exactly.
        """

        overpass_urls = _phase3_overpass_urls(self.overpass_url)

        # --- 1. Build command ---
        script_path = _script_path("05_discover_relation.py")

        route_arg = str(route_id) if route_id else "new"

        south, west, north, east = bbox
        bbox_str = f"{south},{west},{north},{east}"
        bbox_payload = {
            "south": float(south),
            "west": float(west),
            "north": float(north),
            "east": float(east),
        }

        cmd = [
            sys.executable,
            script_path,
            route_arg,
            f"--bbox={bbox_str}",
            "--max-candidates",
            str(max_candidates),
            "--timeout-s",
            str(timeout_s),
            "--query-strategy",
            str(query_strategy or "bbox_first_broad"),
        ]

        if refs:
            cmd.extend(["--refs", ",".join(refs)])

        if operator:
            cmd.extend(["--operator", operator])

        if name:
            cmd.extend(["--name", name])

        if store:
            cmd.append("--store")

        # --- 2. Execute script ---
        result = None
        last_err = None
        last_stderr = ""
        attempt_logs: List[Dict[str, Any]] = []
        fallback_retry_used = False
        fallback_profile: Optional[Dict[str, Any]] = None
        for url in overpass_urls:
            result = _run_script(cmd, env_overrides={"OVERPASS_URL": url})
            attempt_logs.append(
                {
                    "phase": "primary",
                    "overpass_url": url,
                    "returncode": int(result.returncode),
                    "error_class": _classify_discover_attempt_error(result.stderr or ""),
                }
            )
            if result.returncode == 0:
                break
            last_err = result
            last_stderr = result.stderr or ""
            if "429" in last_stderr or "504" in last_stderr or "non-JSON" in last_stderr or "<?xml" in last_stderr:
                continue
            break

        # Fallback: if Overpass timed out, retry with smaller candidate limit
        if (result is None or result.returncode != 0) and "504" in last_stderr and max_candidates > 5:
            try:
                cmd2 = list(cmd)
                if "--max-candidates" in cmd2:
                    i = cmd2.index("--max-candidates") + 1
                    cmd2[i] = "5"
                if "--timeout-s" in cmd2:
                    i = cmd2.index("--timeout-s") + 1
                    cmd2[i] = "60"
                fallback_profile = {
                    "max_candidates": 5,
                    "timeout_s": 60,
                    "trigger": "upstream_timeout_504",
                }
                for url in overpass_urls:
                    result = _run_script(cmd2, env_overrides={"OVERPASS_URL": url})
                    attempt_logs.append(
                        {
                            "phase": "timeout_fallback",
                            "overpass_url": url,
                            "returncode": int(result.returncode),
                            "error_class": _classify_discover_attempt_error(result.stderr or ""),
                        }
                    )
                    if result.returncode == 0:
                        fallback_retry_used = True
                        break
                    last_err = result
                    last_stderr = result.stderr or ""
                    if "429" in last_stderr or "504" in last_stderr or "non-JSON" in last_stderr or "<?xml" in last_stderr:
                        continue
                    break
            except Exception:
                pass

        if result is None or result.returncode != 0:
            err_out = (last_err.stderr if last_err else "")
            attempted_urls = [str(dict(a or {}).get("overpass_url") or "").strip() for a in list(attempt_logs or []) if str(dict(a or {}).get("overpass_url") or "").strip()]
            raise RuntimeError(
                f"Step 05 failed.\nAttempted Overpass URLs: {attempted_urls}\nSTDOUT:\n{(last_err.stdout if last_err else '')}\n\nSTDERR:\n{err_out}"
            )

        stdout = result.stdout.strip()

        # --- 3. Parse stdout ---
        route_match = re.search(r"route_id:\s*([0-9a-fA-F-]+)", stdout)
        relation_match = re.search(r"osm_relation_id:\s*(\d+)", stdout)

        if not route_match or not relation_match:
            raise RuntimeError(f"Could not parse discover output:\n{stdout}")

        parsed_route_id = route_match.group(1)
        parsed_relation_id = int(relation_match.group(1))
        top_candidate = _parse_prefixed_json_line(stdout, "top_candidate")
        candidate_universe = _as_dict(_parse_prefixed_json_line(stdout, "candidate_universe"))
        selection_summary = _as_dict(_parse_prefixed_json_line(stdout, "selection_summary"))
        candidate_preview = list(_parse_prefixed_json_line(stdout, "candidate_preview") or [])

        candidate_rows: List[Dict[str, Any]] = []
        try:
            candidate_rows = list(self.list_relation_candidates(uuid.UUID(parsed_route_id)) or [])
        except Exception:
            candidate_rows = []
        novelty_resolution = self._resolve_extractor_candidate_novelty(
            route_id=parsed_route_id,
            candidate_rows=candidate_rows,
            chosen_relation_id=parsed_relation_id,
            reuse_context={
                "source_document": (str(source_document).strip() if source_document else None),
                "place": (str(place_input).strip() if place_input else None),
                "group": (str(target_group).strip() if target_group else None),
                "place_bundle": (str(target_place_bundle).strip() if target_place_bundle else None),
                "route_hint_raw": (str(route_hint_raw).strip() if route_hint_raw else None),
                "bbox_used": dict(bbox_payload),
                "selection_confidence": (
                    _to_float(selection_summary.get("selection_confidence"))
                    or _to_float((top_candidate or {}).get("selection_confidence"))
                ),
            },
        )
        candidate_rows = [dict(row or {}) for row in list(novelty_resolution.get("candidate_rows") or [])]
        parsed_relation_id = int(
            novelty_resolution.get("chosen_relation_id")
            or parsed_relation_id
        )
        novelty_status = str(novelty_resolution.get("novelty_status") or "unknown").strip() or "unknown"
        selection_override = _as_dict(novelty_resolution.get("selection_override"))
        reused_existing_route_id = (
            str(novelty_resolution.get("reused_existing_route_id") or "").strip() or None
        )
        existing_relation_usage_count = int(
            novelty_resolution.get("existing_relation_usage_count") or 0
        )
        existing_relation_route_ids = [
            str(v)
            for v in list(novelty_resolution.get("existing_relation_route_ids") or [])
            if str(v or "").strip()
        ]
        effective_route_id = reused_existing_route_id or parsed_route_id
        extractor_diag = _summarize_discover_candidates(
            candidate_rows,
            chosen_relation_id=parsed_relation_id,
            top_candidate=(top_candidate or {}),
        )
        candidate_universe_summary = _build_phase3_candidate_universe_summary(
            candidate_rows,
            chosen_relation_id=parsed_relation_id,
            parsed_summary=candidate_universe,
        )
        selection_summary_payload = _build_phase3_selection_summary(
            candidate_rows,
            chosen_relation_id=parsed_relation_id,
            parsed_summary=selection_summary,
        )
        selection_summary_payload["novelty_status"] = novelty_status
        selection_summary_payload["existing_relation_usage_count"] = existing_relation_usage_count
        selection_summary_payload["existing_relation_route_ids"] = existing_relation_route_ids
        if reused_existing_route_id:
            selection_summary_payload["reused_existing_route_id"] = reused_existing_route_id
        if selection_override:
            selection_summary_payload["selection_override"] = dict(selection_override)
        candidate_preview_payload = (
            [dict(row or {}) for row in candidate_preview]
            if candidate_preview
            else _build_phase3_candidate_preview(candidate_rows)
        )
        preview_by_relation = {
            int(row["osm_relation_id"]): row
            for row in candidate_rows
            if _to_int(row.get("osm_relation_id")) is not None
        }
        candidate_preview_payload = [
            _merge_nested_dicts(
                dict(row or {}),
                {
                    "existing_relation_usage_count": preview_by_relation.get(int(row.get("osm_relation_id")), {}).get("existing_relation_usage_count"),
                    "relation_novelty": preview_by_relation.get(int(row.get("osm_relation_id")), {}).get("relation_novelty"),
                } if _to_int(row.get("osm_relation_id")) is not None else {},
            )
            for row in candidate_preview_payload
        ]
        extractor_diag["novelty_status"] = novelty_status
        extractor_diag["existing_relation_usage_count"] = existing_relation_usage_count
        extractor_diag["existing_relation_route_ids"] = existing_relation_route_ids
        if reused_existing_route_id:
            extractor_diag["reused_existing_route_id"] = reused_existing_route_id
        if selection_override:
            extractor_diag["selection_override"] = dict(selection_override)
        if attempt_logs:
            attempt_logs[-1]["candidate_count"] = candidate_universe_summary.get("candidate_universe_count")
            attempt_logs[-1]["selection_confidence"] = selection_summary_payload.get("selection_confidence")
            attempt_logs[-1]["query_strategy"] = (
                candidate_universe_summary.get("query_strategy")
                or str(query_strategy or "").strip()
                or "bbox_first_broad"
            )
            attempt_logs[-1]["novelty_status"] = novelty_status

        sid = (str(service_route_id).strip() if service_route_id else "")
        did = int(direction_id) if direction_id in (0, 1) else None
        if sid and did in (0, 1):
            try:
                self.bind_route_to_direction(
                    service_route_id=sid,
                    direction_id=int(did),
                    route_id=effective_route_id,
                    geom_source="observed",
                )
            except Exception:
                pass
        try:
            self.mark_direction_progress_by_route(
                route_id=effective_route_id,
                step=1,
                progress_notes="Step 05 discover completed.",
            )
        except Exception:
            pass

        out = {
            "route_id": effective_route_id,
            "chosen_osm_relation_id": parsed_relation_id,
            "top_candidate": top_candidate,
            "query_strategy": (
                candidate_universe_summary.get("query_strategy")
                or str(query_strategy or "").strip()
                or "bbox_first_broad"
            ),
            "candidate_universe_summary": candidate_universe_summary,
            "selection_summary": selection_summary_payload,
            "candidate_preview": candidate_preview_payload,
            "raw_stdout": stdout,
            "extractor_diagnostics": extractor_diag,
            "extractor_attempts": attempt_logs,
            "extractor_fallback_used": bool(fallback_retry_used),
            "extractor_fallback_profile": fallback_profile,
            "novelty_status": novelty_status,
            "reused_existing_route_id": reused_existing_route_id,
            "existing_relation_usage_count": existing_relation_usage_count,
            "existing_relation_route_ids": existing_relation_route_ids,
            "selection_override": dict(selection_override or {}),
        }
        review_packet = {
            "schema_version": "phase3_extractor_review_v1",
            "success_criterion": "relation_extraction_and_persistence",
            "source_document": (str(source_document).strip() if source_document else None),
            "batch_id": (str(batch_id).strip() if batch_id else None),
            "target": {
                "place": (str(place_input).strip() if place_input else None),
                "group": (str(target_group).strip() if target_group else None),
                "priority": (str(target_priority).strip() if target_priority else None),
                "attempt_type": (str(target_attempt_type).strip() if target_attempt_type else None),
                "place_bundle": (str(target_place_bundle).strip() if target_place_bundle else None),
                "seed_origin": (str(target_seed_origin).strip() if target_seed_origin else None),
                "catalog_confidence": (str(target_catalog_confidence).strip() if target_catalog_confidence else None),
            },
            "geography": {
                "place_input": (str(place_input).strip() if place_input else None),
                "bbox_used": dict(bbox_payload),
                "interpretation_source": _as_dict(geography_resolution).get("interpretation_source"),
                "interpretation_status": _as_dict(geography_resolution).get("interpretation_status"),
                "interpreted_place_meaning": _as_dict(geography_resolution).get("interpreted_place_meaning"),
                "bbox_candidate_confidence": _as_dict(geography_resolution).get("bbox_candidate_confidence"),
                "route_hints_used_as_secondary_signal": _as_dict(geography_resolution).get("route_hints_used_as_secondary_signal"),
                "route_hints_overconstrained_geography": _as_dict(geography_resolution).get("route_hints_overconstrained_geography"),
            },
            "hints": {
                "route_hint_raw": (str(route_hint_raw).strip() if route_hint_raw else None),
                "route_hint_contract": _as_dict(route_hint_contract),
                "cooperative_hint": (str(cooperative_hint).strip() if cooperative_hint else None),
                "refs": list(refs or []),
                "operator": operator,
                "route_name": name,
            },
            "discover": {
                "route_id": effective_route_id,
                "chosen_osm_relation_id": parsed_relation_id,
                "query_strategy": out.get("query_strategy"),
                "top_candidate": dict(top_candidate or {}),
                "candidate_universe_summary": dict(candidate_universe_summary or {}),
                "selection_summary": dict(selection_summary_payload or {}),
                "candidate_preview": [dict(row or {}) for row in list(candidate_preview_payload or [])],
                "extractor_diagnostics": dict(extractor_diag or {}),
                "extractor_attempts": [dict(row or {}) for row in list(attempt_logs or [])],
                "extractor_fallback_used": bool(fallback_retry_used),
                "extractor_fallback_profile": dict(fallback_profile or {}) if fallback_profile else None,
                "relation_extraction_success": bool(parsed_relation_id),
                "novelty_status": novelty_status,
                "existing_relation_usage_count": existing_relation_usage_count,
                "existing_relation_route_ids": existing_relation_route_ids,
                "reused_existing_route_id": reused_existing_route_id,
                "selection_override": dict(selection_override or {}),
            },
            "dedupe": {
                "novelty_status": novelty_status,
                "existing_relation_usage_count": existing_relation_usage_count,
                "existing_relation_route_ids": existing_relation_route_ids,
                "reused_existing_route_id": reused_existing_route_id,
                "selection_override": dict(selection_override or {}),
            },
            "downstream": {
                "step20_required": False,
                "matching_success_required": False,
                "notes": [
                    "downstream_sequence_matching_intentionally_not_used_as_success_criterion",
                ],
            },
        }
        review_packet["attempt_history"] = [
            self._build_extractor_attempt_entry(
                review_packet,
                route_id=effective_route_id,
                deleted_duplicate_route_id=(parsed_route_id if reused_existing_route_id else None),
            )
        ]
        if reused_existing_route_id and str(reused_existing_route_id) != str(parsed_route_id):
            self._merge_duplicate_extractor_attempt(
                canonical_route_id=reused_existing_route_id,
                duplicate_route_id=parsed_route_id,
                duplicate_review=review_packet,
            )
        else:
            self._persist_route_job_extractor_review(
                route_id=effective_route_id,
                review_patch=review_packet,
                extractor_source=(Path(source_document).name if source_document else "manual_step05_discover"),
            )
        self._safe_ai_log_phase3(
            stage="step_05_discover",
            route_id=effective_route_id,
            payload={
                **out,
                "bbox": bbox,
                "known_ref": ((refs or [None])[0] if refs else None),
                "service_route_id": service_route_id,
                "direction_id": direction_id,
                "operator": operator,
                "route_name": name,
                "max_candidates": int(max_candidates),
                "timeout_s": int(timeout_s),
                "query_strategy": out.get("query_strategy"),
                "candidate_universe_summary": candidate_universe_summary,
                "selection_summary": selection_summary_payload,
                "candidate_preview": candidate_preview_payload,
                "extractor_diagnostics": extractor_diag,
                "extractor_attempts": attempt_logs,
                "extractor_fallback_used": bool(fallback_retry_used),
                "extractor_fallback_profile": fallback_profile,
                "progressed_to_next_step": bool(effective_route_id),
            },
        )
        return out
    def list_relation_candidates(
            self,
            route_id: uuid.UUID,
        ) -> List[Dict[str, Any]]:
            """
            Returns relation candidates for a given route_id
            ordered by score DESC.

            Mirrors:
            route_raw.relation_candidates
            """

            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    queries = [
                        """
                        SELECT
                          rc.osm_relation_id,
                          rc.tags,
                          rc.ref,
                          rc.name,
                          rc.operator,
                          rc.route_mode,
                          rc.rel_type,
                          rc.found_at,
                          (rc.osm_relation_id = rj.chosen_osm_relation_id) AS is_chosen
                        FROM route_raw.relation_candidates rc
                        LEFT JOIN route_raw.route_jobs rj ON rj.route_id = rc.route_id
                        WHERE rc.route_id = %s
                        ORDER BY rc.found_at DESC, rc.osm_relation_id ASC
                        """,
                        """
                        SELECT osm_relation_id, tags, ref, name, operator, route_mode, rel_type, found_at
                        FROM route_raw.relation_candidates
                        WHERE route_id = %s
                        ORDER BY found_at DESC, osm_relation_id ASC
                        """,
                    ]

                    rows = []
                    last_err = None
                    for sql in queries:
                        try:
                            cur.execute(sql, (str(route_id),))
                            rows = cur.fetchall() or []
                            last_err = None
                            break
                        except Exception as e:
                            last_err = e
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            continue
                    if last_err:
                        raise last_err

            # normalize return
            results = []
            for r in rows:
                tags = r.get("tags") or {}
                meta = _candidate_meta_from_tags(tags)
                results.append(
                    {
                        "osm_relation_id": int(r["osm_relation_id"]),
                        "tags": tags,
                        "stop_prior_count": int(meta.get("stop_prior_count") or 0),
                        "score": float(meta.get("score") or 0.0),
                        "is_chosen": bool(r.get("is_chosen")),
                        "ref": r.get("ref"),
                        "name": r.get("name"),
                        "operator": r.get("operator"),
                        "route_mode": r.get("route_mode"),
                        "rel_type": r.get("rel_type"),
                        "found_at": r.get("found_at"),
                        "selection_rank": _to_int(meta.get("selection_rank")),
                        "selection_confidence": _to_float(meta.get("selection_confidence")),
                        "matched_soft_signals": list(meta.get("matched_soft_signals") or []),
                        "selection_reason_codes": list(meta.get("selection_reason_codes") or []),
                        "hard_filters_applied": list(meta.get("hard_filters_applied") or []),
                        "soft_signals_used": list(meta.get("soft_signals_used") or []),
                        "query_strategy": (
                            str(meta.get("query_strategy") or "").strip() or None
                        ),
                    }
                )
            results.sort(
                key=lambda row: (
                    0 if bool(row.get("is_chosen")) else 1,
                    int(row.get("selection_rank")) if row.get("selection_rank") is not None else 10**6,
                    -float(row.get("score") or 0.0),
                    -int(row.get("stop_prior_count") or 0),
                    str(row.get("found_at") or ""),
                )
            )
            return results

    def set_chosen_relation(self, route_id: uuid.UUID, osm_relation_id: int) -> None:
        """
        Persists the chosen relation on route_raw.route_jobs.
        """
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    UPDATE route_raw.route_jobs
                    SET chosen_osm_relation_id = %s
                    WHERE route_id = %s
                    """,
                    (int(osm_relation_id), str(route_id)),
                )
    
    def get_relation_geometry(
        self,
        route_id: uuid.UUID,
        osm_relation_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Returns GeoJSON geometry for the fetched OSM relation
        so the left map can render it.

        Reads from:
            route_raw.osm_relations_raw
        """

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                queries = [
                    (
                        """
                        SELECT osm_relation_id, overpass_json
                        FROM route_raw.osm_relations_raw
                        WHERE route_id = %s AND osm_relation_id = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (str(route_id), int(osm_relation_id)),
                    ) if osm_relation_id is not None else None,
                    (
                        """
                        SELECT osm_relation_id, overpass_json
                        FROM route_raw.osm_relations_raw
                        WHERE route_id = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (str(route_id),),
                    ),
                    (
                        """
                        SELECT osm_relation_id, overpass_json
                        FROM route_raw.osm_relations_raw
                        WHERE route_id = %s
                        LIMIT 1
                        """,
                        (str(route_id),),
                    ),
                ]
                row = None
                last_err = None
                for item in queries:
                    if not item:
                        continue
                    sql, params = item
                    try:
                        cur.execute(sql, params)
                        row = cur.fetchone()
                        last_err = None
                        if row:
                            break
                    except Exception as e:
                        last_err = e
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        continue
                if last_err:
                    raise last_err

        if not row:
            return {
                "route_id": str(route_id),
                "osm_relation_id": None,
                "geojson": None,
            }

        overpass_json = row.get("overpass_json") or {}
        osm_relation_id = row.get("osm_relation_id")

        # Extract geometry from Overpass structure
        # We expect something like:
        # overpass_json["elements"] → relation + member ways with geometry

        elements = overpass_json.get("elements", [])

        # Collect all way geometries
        lines = []

        for el in elements:
            if el.get("type") == "way" and "geometry" in el:
                coords = [
                    [pt["lon"], pt["lat"]]
                    for pt in el["geometry"]
                ]
                if len(coords) >= 2:
                    lines.append({
                        "type": "LineString",
                        "coordinates": coords,
                    })

        if not lines:
            return {
                "route_id": str(route_id),
                "osm_relation_id": osm_relation_id,
                "geojson": None,
            }

        # If multiple ways → MultiLineString
        if len(lines) == 1:
            geometry = lines[0]
        else:
            geometry = {
                "type": "MultiLineString",
                "coordinates": [l["coordinates"] for l in lines],
            }

        geojson = {
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "route_id": str(route_id),
                "osm_relation_id": osm_relation_id,
            },
        }

        return {
            "route_id": str(route_id),
            "osm_relation_id": osm_relation_id,
            "geojson": geojson,
        }

    def list_relation_raw_ids(self, route_id: uuid.UUID) -> List[int]:
        """
        Returns osm_relation_id values stored in route_raw.osm_relations_raw for route_id.
        """
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                try:
                    cur.execute(
                        """
                        SELECT DISTINCT osm_relation_id
                        FROM route_raw.osm_relations_raw
                        WHERE route_id = %s
                        """,
                        (str(route_id),),
                    )
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    return []
                rows = cur.fetchall() or []
        out: List[int] = []
        for r in rows:
            try:
                out.append(int(r.get("osm_relation_id")))
            except Exception:
                continue
        return out

    def get_stop_prior_points(self, route_id: uuid.UUID) -> List[Dict[str, Any]]:
        """
        Returns stop prior points from route_work.relation_stop_prior.
        """
        rows = self.get_relation_stop_prior(route_id)
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "seq": int(r.get("seq") or 0),
                    "lat": float(r.get("lat") or 0.0),
                    "lon": float(r.get("lon") or 0.0),
                    "osm_node_id": r.get("osm_node_id"),
                    "role": r.get("role"),
                    "matched_stop_node_id": r.get("matched_stop_node_id"),
                    "match_dist_m": r.get("match_dist_m"),
                }
            )
        return out

    # -------------------------------------------------------------------------
    # Manual Sequence Builder
    # -------------------------------------------------------------------------
    def list_manual_builder_approved_stops(
        self,
        *,
        search: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        q = str(search or "").strip().lower()
        q_like = f"%{q}%"
        safe_limit = max(1, min(int(limit), 5000))

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      n.node_id::text AS stop_id,
                      m.place_id::text AS place_id,
                      COALESCE(NULLIF(BTRIM(p.canonical_name), ''), NULLIF(BTRIM(n.name), ''), NULLIF(BTRIM(n.ref), ''), ('stop_' || LEFT(n.node_id::text, 8))) AS name,
                      n.ref,
                      n.operator,
                      m.mapping_source,
                      m.confidence,
                      ST_Y(n.geom)::float8 AS lat,
                      ST_X(n.geom)::float8 AS lon,
                      p.place_type,
                      p.status AS place_status
                    FROM geo_prod.node_place_map m
                    JOIN node_prod.nodes n
                      ON n.node_id = m.node_id
                    JOIN geo_prod.places p
                      ON p.place_id = m.place_id
                    WHERE n.node_type = 'STOP'
                      AND COALESCE(p.status, 'active') = 'active'
                      AND (
                        %s = ''
                        OR LOWER(COALESCE(p.canonical_name, '')) LIKE %s
                        OR LOWER(COALESCE(n.name, '')) LIKE %s
                        OR LOWER(COALESCE(n.ref, '')) LIKE %s
                        OR LOWER(COALESCE(n.operator, '')) LIKE %s
                        OR n.node_id::text LIKE %s
                      )
                    ORDER BY LOWER(COALESCE(p.canonical_name, n.name, n.ref, n.node_id::text)) ASC, n.node_id::text ASC
                    LIMIT %s
                    """,
                    (
                        q,
                        q_like,
                        q_like,
                        q_like,
                        q_like,
                        q_like,
                        safe_limit,
                    ),
                )
                rows = cur.fetchall() or []

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "stop_id": str(r.get("stop_id") or ""),
                    "node_id": str(r.get("stop_id") or ""),
                    "place_id": str(r.get("place_id") or ""),
                    "name": r.get("name"),
                    "ref": r.get("ref"),
                    "operator": r.get("operator"),
                    "mapping_source": r.get("mapping_source"),
                    "confidence": (float(r.get("confidence")) if r.get("confidence") is not None else None),
                    "lat": (float(r.get("lat")) if r.get("lat") is not None else None),
                    "lon": (float(r.get("lon")) if r.get("lon") is not None else None),
                    "place_type": r.get("place_type"),
                    "place_status": r.get("place_status"),
                    "is_approved": True,
                }
            )
        _LOG.info(
            "phase3.manual_builder.stop_load search=%r limit=%s count=%s",
            q,
            safe_limit,
            len(out),
        )
        return out

    def _resolve_manual_builder_stop_rows(self, stop_ids: List[str]) -> List[Dict[str, Any]]:
        clean_ids: List[str] = []
        for sid in (stop_ids or []):
            raw = str(sid or "").strip()
            if not raw:
                continue
            try:
                clean_ids.append(str(uuid.UUID(raw)))
            except Exception:
                continue
        if not clean_ids:
            return []

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      n.node_id::text AS stop_id,
                      m.place_id::text AS place_id,
                      COALESCE(NULLIF(BTRIM(p.canonical_name), ''), NULLIF(BTRIM(n.name), ''), NULLIF(BTRIM(n.ref), ''), ('stop_' || LEFT(n.node_id::text, 8))) AS name,
                      n.ref,
                      n.operator,
                      ST_Y(n.geom)::float8 AS lat,
                      ST_X(n.geom)::float8 AS lon,
                      p.place_type,
                      p.status AS place_status
                    FROM geo_prod.node_place_map m
                    JOIN node_prod.nodes n
                      ON n.node_id = m.node_id
                    JOIN geo_prod.places p
                      ON p.place_id = m.place_id
                    WHERE n.node_type = 'STOP'
                      AND COALESCE(p.status, 'active') = 'active'
                      AND n.node_id = ANY(%s::uuid[])
                    """,
                    (clean_ids,),
                )
                rows = cur.fetchall() or []
        return [
            {
                "stop_id": str(r.get("stop_id") or ""),
                "node_id": str(r.get("stop_id") or ""),
                "place_id": str(r.get("place_id") or ""),
                "name": r.get("name"),
                "ref": r.get("ref"),
                "operator": r.get("operator"),
                "lat": (float(r.get("lat")) if r.get("lat") is not None else None),
                "lon": (float(r.get("lon")) if r.get("lon") is not None else None),
                "place_type": r.get("place_type"),
                "place_status": r.get("place_status"),
            }
            for r in rows
        ]

    def get_manual_builder_stops_by_ids(self, stop_ids: List[str]) -> List[Dict[str, Any]]:
        return self._resolve_manual_builder_stop_rows(stop_ids)

    def validate_manual_sequence_export(
        self,
        payload: ManualSequenceExportRequest | Dict[str, Any],
        *,
        min_stops: int = 2,
        jump_warn_m: float = 3500.0,
    ) -> Dict[str, Any]:
        req = normalize_manual_sequence_export_request(payload)
        rows = self._resolve_manual_builder_stop_rows(req.ordered_stop_ids)
        validation = validate_manual_sequence_rows(
            ordered_stop_ids=req.ordered_stop_ids,
            resolved_rows=rows,
            is_loop=bool(req.is_loop),
            min_stops=int(min_stops),
            jump_warn_m=float(jump_warn_m),
        )
        return {
            "request": req,
            "errors": list(validation.errors),
            "warnings": list(validation.warnings),
            "ordered_stops": list(validation.ordered_stops),
        }

    def save_manual_sequence_draft(
        self,
        payload: ManualSequenceExportRequest | Dict[str, Any],
    ) -> Dict[str, Any]:
        self._ensure_manual_sequence_schema()
        req = normalize_manual_sequence_export_request(payload)
        created_by = req.created_by or os.getenv("USER") or os.getenv("USERNAME") or "console"

        rows = self._resolve_manual_builder_stop_rows(req.ordered_stop_ids)
        by_stop = {str(r.get("stop_id") or ""): r for r in rows}
        coords = list(req.ordered_coords or [])
        if not coords:
            coords = [
                [float(by_stop[sid]["lon"]), float(by_stop[sid]["lat"])]
                for sid in req.ordered_stop_ids
                if sid in by_stop
                and by_stop[sid].get("lat") is not None
                and by_stop[sid].get("lon") is not None
            ]

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_work.manual_sequence_drafts
                      (route_id, service_route_id, direction_id, source,
                       ordered_stop_ids, ordered_node_ids, ordered_coords, is_loop,
                       name_hint, operator_hint, variant_hint, created_by, updated_at)
                    VALUES
                      (%s::uuid, %s::uuid, %s, 'manual_builder',
                       %s::uuid[], %s::uuid[], %s::jsonb, %s,
                       %s, %s, %s, %s, now())
                    RETURNING draft_id::text AS draft_id, created_at, updated_at
                    """,
                    (
                        req.route_job_id,
                        req.service_route_id,
                        req.direction_id,
                        req.ordered_stop_ids,
                        req.ordered_node_ids or req.ordered_stop_ids,
                        json.dumps(_jsonable(coords), ensure_ascii=False),
                        bool(req.is_loop),
                        req.name_hint,
                        req.operator_hint,
                        req.variant_hint,
                        created_by,
                    ),
                )
                row = cur.fetchone() or {}

        return {
            "draft_id": str(row.get("draft_id") or ""),
            "route_job_id": req.route_job_id,
            "service_route_id": req.service_route_id,
            "direction_id": req.direction_id,
            "coverage_gap_id": req.coverage_gap_id,
            "source": "manual_builder",
            "ordered_stop_count": len(req.ordered_stop_ids),
            "created_at": (row.get("created_at").isoformat() if hasattr(row.get("created_at"), "isoformat") else row.get("created_at")),
            "updated_at": (row.get("updated_at").isoformat() if hasattr(row.get("updated_at"), "isoformat") else row.get("updated_at")),
        }

    def export_manual_sequence_to_phase3(
        self,
        payload: ManualSequenceExportRequest | Dict[str, Any],
        *,
        min_stops: int = 2,
        jump_warn_m: float = 3500.0,
        auto_create_route_job: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_manual_sequence_schema()
        check = self.validate_manual_sequence_export(
            payload,
            min_stops=int(min_stops),
            jump_warn_m=float(jump_warn_m),
        )
        req: ManualSequenceExportRequest = check["request"]
        errors = list(check.get("errors") or [])
        warnings = list(check.get("warnings") or [])
        ordered_stops = list(check.get("ordered_stops") or [])
        if errors:
            raise ValueError("Manual sequence export validation failed: " + " | ".join(errors))

        created_by = req.created_by or os.getenv("USER") or os.getenv("USERNAME") or "console"

        route_id: Optional[str] = req.route_job_id
        if route_id:
            job = self.get_route_job(uuid.UUID(route_id))
            if not job:
                raise ValueError(f"route_job_id not found: {route_id}")
        elif auto_create_route_job:
            rid = self.create_route_job(
                service_route_id=req.service_route_id,
                direction_id=req.direction_id,
                created_by=created_by,
                notes="Created by Manual Sequence Builder export.",
            )
            route_id = str(rid)
        else:
            raise ValueError("route_job_id is required when auto_create_route_job is disabled")

        if req.service_route_id and req.direction_id in (0, 1):
            try:
                self.bind_route_to_direction(
                    service_route_id=req.service_route_id,
                    direction_id=int(req.direction_id),
                    route_id=str(route_id),
                    geom_source="manual",
                )
            except Exception:
                pass

        prior_rows: List[Dict[str, Any]] = []
        for i, stop in enumerate(ordered_stops, start=1):
            prior_rows.append(
                {
                    "seq": int(i),
                    "lat": float(stop["lat"]),
                    "lon": float(stop["lon"]),
                    "role": "manual_builder",
                    "member_type": "node",
                    "osm_ref": None,
                    "matched_stop_node_id": str(stop["stop_id"]),
                    "match_dist_m": 0.0,
                }
            )

        self.replace_relation_stop_prior(uuid.UUID(str(route_id)), prior_rows, edit_source="manual_builder_export")

        ordered_stop_ids = [str(stop["stop_id"]) for stop in ordered_stops]
        ordered_node_ids = list(req.ordered_node_ids or ordered_stop_ids)
        ordered_coords = list(req.ordered_coords or [])
        if not ordered_coords:
            ordered_coords = [[float(stop["lon"]), float(stop["lat"])] for stop in ordered_stops]

        _LOG.info(
            "phase3.manual_builder.export summary route_id=%s count=%s first=%s last=%s is_loop=%s",
            route_id,
            len(ordered_stop_ids),
            (ordered_stop_ids[0] if ordered_stop_ids else None),
            (ordered_stop_ids[-1] if ordered_stop_ids else None),
            bool(req.is_loop),
        )

        with db_conn() as conn:
            set_notes = f"source=manual_builder is_loop={bool(req.is_loop)}"
            set_id = create_stop_sequence_set(conn, uuid.UUID(str(route_id)), notes=set_notes)
            candidate_metrics = {
                "mode": "manual_builder",
                "source": "manual_builder",
                "is_loop": bool(req.is_loop),
                "ordered_stop_count": len(ordered_stop_ids),
                "name_hint": req.name_hint,
                "operator_hint": req.operator_hint,
                "variant_hint": req.variant_hint,
            }
            candidate_id = insert_stop_sequence_candidate(
                conn,
                set_id=set_id,
                rank=1,
                stop_node_ids=[uuid.UUID(sid) for sid in ordered_stop_ids],
                stop_prior_seqs=list(range(1, len(ordered_stop_ids) + 1)),
                metrics=candidate_metrics,
            )
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_work.manual_sequence_exports
                      (route_id, service_route_id, direction_id, source,
                       ordered_stop_ids, ordered_node_ids, ordered_coords, is_loop,
                       name_hint, operator_hint, variant_hint, created_by,
                       stop_sequence_set_id, stop_sequence_candidate_id, coverage_gap_id)
                    VALUES
                      (%s::uuid, %s::uuid, %s, 'manual_builder',
                       %s::uuid[], %s::uuid[], %s::jsonb, %s,
                       %s, %s, %s, %s,
                       %s::uuid, %s::uuid, %s::uuid)
                    RETURNING export_id::text AS export_id, created_at
                    """,
                    (
                        str(route_id),
                        req.service_route_id,
                        req.direction_id,
                        ordered_stop_ids,
                        ordered_node_ids,
                        json.dumps(_jsonable(ordered_coords), ensure_ascii=False),
                        bool(req.is_loop),
                        req.name_hint,
                        req.operator_hint,
                        req.variant_hint,
                        created_by,
                        str(set_id),
                        str(candidate_id),
                        req.coverage_gap_id,
                    ),
                )
                export_row = cur.fetchone() or {}

        try:
            self.mark_direction_progress_by_route(
                route_id=str(route_id),
                step=2,
                progress_notes="Manual sequence exported to Step 20 pipeline.",
            )
        except Exception:
            pass
        if req.coverage_gap_id:
            try:
                self.link_phase3_coverage_gap_to_route(
                    gap_id=req.coverage_gap_id,
                    route_id=str(route_id),
                    resolution_status="in_progress",
                    reviewed_by=created_by,
                    notes="Manual Sequence Builder export linked to Phase 3 route job.",
                )
            except Exception:
                pass

        out = {
            "export_id": str(export_row.get("export_id") or ""),
            "route_job_id": str(route_id),
            "service_route_id": req.service_route_id,
            "direction_id": req.direction_id,
            "coverage_gap_id": req.coverage_gap_id,
            "source": "manual_builder",
            "ordered_stop_ids": ordered_stop_ids,
            "ordered_node_ids": ordered_node_ids,
            "ordered_coords": ordered_coords,
            "is_loop": bool(req.is_loop),
            "name_hint": req.name_hint,
            "operator_hint": req.operator_hint,
            "variant_hint": req.variant_hint,
            "created_by": created_by,
            "created_at": (
                export_row.get("created_at").isoformat()
                if hasattr(export_row.get("created_at"), "isoformat")
                else export_row.get("created_at")
            ),
            "stop_sequence_set_id": str(set_id),
            "stop_sequence_candidate_id": str(candidate_id),
            "next_screen": "Sequence",
            "warnings": warnings,
        }
        _LOG.info(
            "phase3.manual_builder.export generated route_id=%s set_id=%s candidate_id=%s export_id=%s",
            out.get("route_job_id"),
            out.get("stop_sequence_set_id"),
            out.get("stop_sequence_candidate_id"),
            out.get("export_id"),
        )
        self._safe_ai_log_phase3(
            stage="manual_sequence_export",
            route_id=str(route_id),
            payload=out,
            prior_rows=prior_rows,
            warnings=warnings,
            notes=["source=manual_builder"],
        )
        return out

    def get_manual_sequence_export(self, export_id: uuid.UUID) -> Dict[str, Any]:
        self._ensure_manual_sequence_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      export_id::text AS export_id,
                      route_id::text AS route_job_id,
                      service_route_id::text AS service_route_id,
                      direction_id::int AS direction_id,
                      coverage_gap_id::text AS coverage_gap_id,
                      source,
                      ordered_stop_ids,
                      ordered_node_ids,
                      ordered_coords,
                      is_loop,
                      name_hint,
                      operator_hint,
                      variant_hint,
                      created_by,
                      created_at,
                      stop_sequence_set_id::text AS stop_sequence_set_id,
                      stop_sequence_candidate_id::text AS stop_sequence_candidate_id,
                      draft_id::text AS draft_id
                    FROM route_work.manual_sequence_exports
                    WHERE export_id = %s::uuid
                    LIMIT 1
                    """,
                    (str(export_id),),
                )
                row = cur.fetchone()
        if not row:
            return {}
        return {
            "export_id": str(row.get("export_id") or ""),
            "route_job_id": str(row.get("route_job_id") or ""),
            "service_route_id": row.get("service_route_id"),
            "direction_id": row.get("direction_id"),
            "coverage_gap_id": row.get("coverage_gap_id"),
            "source": row.get("source") or "manual_builder",
            "ordered_stop_ids": [str(x) for x in _parse_uuid_array(row.get("ordered_stop_ids"))],
            "ordered_node_ids": [str(x) for x in _parse_uuid_array(row.get("ordered_node_ids"))],
            "ordered_coords": list(row.get("ordered_coords") or []),
            "is_loop": bool(row.get("is_loop")),
            "name_hint": row.get("name_hint"),
            "operator_hint": row.get("operator_hint"),
            "variant_hint": row.get("variant_hint"),
            "created_by": row.get("created_by"),
            "created_at": (row.get("created_at").isoformat() if hasattr(row.get("created_at"), "isoformat") else row.get("created_at")),
            "stop_sequence_set_id": row.get("stop_sequence_set_id"),
            "stop_sequence_candidate_id": row.get("stop_sequence_candidate_id"),
            "draft_id": row.get("draft_id"),
        }

    def get_latest_manual_sequence_export_for_route(self, route_id: uuid.UUID) -> Dict[str, Any]:
        self._ensure_manual_sequence_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT export_id::text AS export_id
                    FROM route_work.manual_sequence_exports
                    WHERE route_id = %s::uuid
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (str(route_id),),
                )
                row = cur.fetchone() or {}
        eid = str(row.get("export_id") or "").strip()
        if not eid:
            return {}
        return self.get_manual_sequence_export(uuid.UUID(eid))

    def get_manual_sequence_handoff(self, route_id: uuid.UUID) -> Dict[str, Any]:
        export_row = self.get_latest_manual_sequence_export_for_route(route_id)
        out: Dict[str, Any] = {
            "route_job_id": str(route_id),
            "manual_export": export_row,
            "coverage_gap_id": export_row.get("coverage_gap_id"),
            "sequence": {"route_id": str(route_id), "set_id": None, "candidates": []},
        }
        set_id = str(export_row.get("stop_sequence_set_id") or "").strip()
        if set_id:
            try:
                out["sequence"] = self.get_sequence_candidates(route_id, set_id=uuid.UUID(set_id))
            except Exception:
                pass
        return out

    def _resolve_linked_coverage_gaps_for_route(
        self,
        *,
        route_id: uuid.UUID | str,
        reviewed_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> List[str]:
        self._ensure_route_review_schema()
        route_txt = str(route_id)
        gap_ids: List[str] = []
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT coverage_gap_id::text AS gap_id
                    FROM route_work.manual_sequence_exports
                    WHERE route_id = %s::uuid
                      AND coverage_gap_id IS NOT NULL
                    UNION
                    SELECT gap_id::text AS gap_id
                    FROM route_review.coverage_gaps
                    WHERE resolved_route_id = %s::uuid
                    """,
                    (route_txt, route_txt),
                )
                rows = cur.fetchall() or []
        for row in rows:
            gap_id = str((row or {}).get("gap_id") or "").strip()
            if not gap_id:
                continue
            try:
                self.mark_phase3_coverage_gap_resolved(
                    gap_id=gap_id,
                    resolved_route_id=route_txt,
                    resolved_prod_route_id=route_txt,
                    reviewed_by=reviewed_by,
                    notes=notes,
                )
                gap_ids.append(gap_id)
            except Exception:
                continue
        return list(dict.fromkeys(gap_ids))

    def get_sequence_candidates(
        self,
        route_id: uuid.UUID,
        *,
        set_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """
        Returns candidates for a given route_id.
        If set_id is None, uses the most recent set.
        """
        if set_id is None:
            sets = self.list_stop_sequence_sets(route_id)
            if not sets:
                return {"route_id": str(route_id), "set_id": None, "candidates": []}
            set_id = uuid.UUID(str(sets[0]["set_id"]))

        cands = self.list_stop_sequence_candidates(set_id)
        out: List[Dict[str, Any]] = []
        for c in cands:
            stop_node_ids = _parse_uuid_array(c.get("stop_node_ids"))
            out.append(
                {
                    "candidate_id": c.get("candidate_id"),
                    "set_id": c.get("set_id"),
                    "rank": c.get("rank"),
                    "stop_node_ids": [str(x) for x in stop_node_ids],
                    "stop_prior_seqs": _parse_int_array(c.get("stop_prior_seqs")),
                    "metrics": c.get("metrics") or {},
                    "created_at": c.get("created_at"),
                }
            )

        variant_summary = self._assess_variant_resolution(out)
        return {
            "route_id": str(route_id),
            "set_id": str(set_id),
            "candidates": out,
            "variant_groups": list(variant_summary.get("variant_groups") or []),
            "variant_state": variant_summary.get("variant_state"),
            "variant_pressure_detected": bool(variant_summary.get("variant_pressure_detected")),
            "variant_pressure_reasons": list(variant_summary.get("variant_pressure_reasons") or []),
            "recommended_variant_group_key": variant_summary.get("recommended_variant_group_key"),
        }

    def get_sequence_approval(self, route_id: uuid.UUID) -> Dict[str, Any]:
        self._ensure_sequence_resolution_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      sa.sequence_approval_id,
                      sa.route_id,
                      sa.stop_sequence_set_id,
                      sa.chosen_stop_sequence_candidate_id,
                      sa.approval_status,
                      sa.approved_at,
                      sa.approved_by,
                      sa.notes,
                      sa.invalidated_at,
                      sa.invalidated_reason,
                      sa.created_at,
                      sa.updated_at,
                      ssc.rank AS candidate_rank,
                      ssc.metrics AS candidate_metrics
                    FROM route_work.sequence_approvals sa
                    LEFT JOIN route_work.stop_sequence_candidates ssc
                      ON ssc.candidate_id = sa.chosen_stop_sequence_candidate_id
                    WHERE sa.route_id = %s
                    LIMIT 1
                    """,
                    (str(route_id),),
                )
                row = cur.fetchone()
        out = self._row(row) if row else {}
        if out and not isinstance(out.get("candidate_metrics"), dict):
            out["candidate_metrics"] = dict(out.get("candidate_metrics") or {})
        if out:
            metrics = dict(out.get("candidate_metrics") or {})
            approval_status = str(out.get("approval_status") or "").strip().lower()
            has_chosen_candidate = bool(str(out.get("chosen_stop_sequence_candidate_id") or "").strip())
            if approval_status == "approved" and has_chosen_candidate:
                out["approved_variant_group_key"] = str(metrics.get("variant_group_key") or "").strip() or None
                out["approved_variant_group_label"] = str(metrics.get("variant_group_label") or "").strip() or None
            else:
                out["approved_variant_group_key"] = None
                out["approved_variant_group_label"] = None
        return out

    @staticmethod
    def _sequence_candidate_signature(candidate: Dict[str, Any]) -> str:
        prior = [str(int(x)) for x in _parse_int_array(candidate.get("stop_prior_seqs"))]
        if prior:
            return "prior:" + ",".join(prior)
        stops = [str(x) for x in _parse_uuid_array(candidate.get("stop_node_ids"))]
        return "stops:" + ",".join(stops)

    @staticmethod
    def _sequence_candidate_variant_identity(candidate: Dict[str, Any]) -> Dict[str, Any]:
        metrics = dict(candidate.get("metrics") or {})
        prior = [int(x) for x in _parse_int_array(candidate.get("stop_prior_seqs"))]
        stop_node_ids = [str(x) for x in _parse_uuid_array(candidate.get("stop_node_ids"))]
        markers = stop_node_ids or [f"seq:{seq}" for seq in prior]
        window = 1
        if markers:
            if len(markers) <= 2:
                window = len(markers)
            else:
                window = max(2, min(4, int(math.ceil(float(len(markers)) * 0.18))))
        head_markers = list(markers[:window])
        tail_markers = list(markers[-window:]) if window > 0 else []
        head_signature = str(metrics.get("variant_head_signature") or "").strip() or "|".join(sorted(head_markers))
        tail_signature = str(metrics.get("variant_tail_signature") or "").strip() or "|".join(sorted(tail_markers))
        orientation = str(metrics.get("sequence_orientation") or "").strip().lower()
        if not orientation:
            forward_steps = 0
            reverse_steps = 0
            for idx in range(1, len(prior)):
                delta = prior[idx] - prior[idx - 1]
                if delta > 0:
                    forward_steps += 1
                elif delta < 0:
                    reverse_steps += 1
            if reverse_steps > forward_steps or (reverse_steps == forward_steps and len(prior) >= 2 and prior[-1] < prior[0]):
                orientation = "inverse"
            else:
                orientation = "forward"

        def _short_marker(marker: Any) -> str:
            text = str(marker or "").strip()
            if not text:
                return "-"
            if text.startswith("seq:"):
                return text
            return text[:8]

        group_key = str(metrics.get("variant_group_key") or "").strip()
        if not group_key:
            group_key = f"{orientation}:{head_signature}->{tail_signature}"
        group_label = str(metrics.get("variant_group_label") or "").strip()
        if not group_label:
            group_label = (
                f"{orientation} | head "
                f"{','.join(_short_marker(marker) for marker in head_markers[:2]) or '-'} | tail "
                f"{','.join(_short_marker(marker) for marker in tail_markers[:2]) or '-'}"
            )
        return {
            "sequence_orientation": orientation or "forward",
            "inverse_orientation_candidate": bool(
                metrics.get("inverse_orientation_candidate")
                if metrics.get("inverse_orientation_candidate") is not None
                else (orientation == "inverse")
            ),
            "variant_group_key": group_key,
            "variant_group_label": group_label,
            "variant_head_signature": head_signature,
            "variant_tail_signature": tail_signature,
            "variant_boundary_window": int(metrics.get("variant_boundary_window") or window or 0),
            "variant_marker_source": str(metrics.get("variant_marker_source") or ("stop_node_ids" if stop_node_ids else "prior_seq")),
        }

    @classmethod
    def _build_variant_group_summaries(cls, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[str, Dict[str, Any]] = {}
        for candidate in list(candidates or []):
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            if not candidate_id:
                continue
            identity = cls._sequence_candidate_variant_identity(candidate)
            row = cls._sequence_shortlist_row(candidate)
            key = str(identity.get("variant_group_key") or "").strip() or f"ungrouped:{candidate_id}"
            group = groups.setdefault(
                key,
                {
                    "variant_group_key": key,
                    "variant_group_label": identity.get("variant_group_label"),
                    "sequence_orientation": identity.get("sequence_orientation"),
                    "inverse_orientation_candidate": bool(identity.get("inverse_orientation_candidate")),
                    "head_signature": identity.get("variant_head_signature"),
                    "tail_signature": identity.get("variant_tail_signature"),
                    "candidate_count": 0,
                    "candidate_ids": [],
                    "candidate_shortlist": [],
                    "top_candidate_id": None,
                    "top_candidate_rank": None,
                    "top_sequence_score": None,
                    "top_structural_sequence_score": None,
                    "top_traversability_score": None,
                    "top_segment_success_rate": None,
                    "top_failed_segment_count": None,
                    "source_families": [],
                    "risk_indicators": [],
                },
            )
            group["candidate_count"] = int(group.get("candidate_count") or 0) + 1
            group["candidate_ids"].append(candidate_id)
            group["candidate_shortlist"].append(row)
            group["source_families"].extend(list(row.get("source_families") or []))
            group["risk_indicators"].extend(list(row.get("risk_indicators") or []))
            current_top_score = _to_float(group.get("top_sequence_score"))
            row_score = _to_float(row.get("sequence_score"))
            current_top_rank = int(group.get("top_candidate_rank") or 10**9)
            row_rank = int(row.get("rank") or 10**9)
            if (
                group.get("top_candidate_id") is None
                or (row_score is not None and (current_top_score is None or row_score > current_top_score))
                or (
                    row_score is not None
                    and current_top_score is not None
                    and abs(row_score - current_top_score) < 1e-9
                    and row_rank < current_top_rank
                )
            ):
                group["top_candidate_id"] = candidate_id
                group["top_candidate_rank"] = row_rank
                group["top_sequence_score"] = row_score
                group["top_structural_sequence_score"] = _to_float(row.get("structural_sequence_score"))
                group["top_traversability_score"] = _to_float(row.get("traversability_score"))
                group["top_segment_success_rate"] = _to_float(row.get("segment_success_rate"))
                group["top_failed_segment_count"] = _to_int(row.get("failed_segment_count"))

        out = sorted(
            groups.values(),
            key=lambda row: (
                float(row.get("top_sequence_score") or 0.0),
                -int(row.get("top_candidate_rank") or 10**6),
            ),
            reverse=True,
        )
        for idx, group in enumerate(out, start=1):
            group["variant_group_rank"] = idx
            group["candidate_shortlist"] = sorted(
                list(group.get("candidate_shortlist") or []),
                key=lambda row: (
                    int(row.get("rank") or 10**6),
                    -(float(row.get("sequence_score") or 0.0)),
                ),
            )
            group["source_families"] = sorted(set(str(x) for x in list(group.get("source_families") or []) if str(x).strip()))
            group["risk_indicators"] = sorted(set(str(x) for x in list(group.get("risk_indicators") or []) if str(x).strip()))
        return out

    @staticmethod
    def _candidate_supports_variant_grouping(candidate: Dict[str, Any]) -> bool:
        metrics = dict(candidate.get("metrics") or {})
        family = str(metrics.get("family") or metrics.get("mode") or "").strip().lower()
        generation_version = str(metrics.get("candidate_generation_version") or "").strip().lower()
        if generation_version == "sequence_resolution_v1":
            return True
        if metrics.get("sequence_score") is not None:
            return True
        if family in {"strict", "relaxed", "raw_prior_fallback"}:
            return False
        return bool(candidate.get("stop_prior_seqs") or candidate.get("stop_node_ids"))

    @classmethod
    def _assess_variant_resolution(cls, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        variant_candidates = [
            dict(candidate)
            for candidate in list(candidates or [])
            if cls._candidate_supports_variant_grouping(candidate)
        ]
        groups = cls._build_variant_group_summaries(variant_candidates)
        if not groups:
            return {
                "variant_groups": [],
                "variant_group_count": 0,
                "variant_state": "no_variant_issue",
                "variant_pressure_detected": False,
                "variant_pressure_reasons": [],
                "variant_evidence_summary": [],
                "recommended_variant_group_key": None,
                "recommended_candidates_by_variant": [],
                "dominant_variant_group_key": None,
            }
        dominant_orientation = str(groups[0].get("sequence_orientation") or "forward")
        same_direction_groups = [g for g in groups if str(g.get("sequence_orientation") or "forward") == dominant_orientation]
        recommended_candidates_by_variant = [
            {
                "variant_group_key": group.get("variant_group_key"),
                "variant_group_label": group.get("variant_group_label"),
                "candidate_id": group.get("top_candidate_id"),
                "sequence_orientation": group.get("sequence_orientation"),
                "top_sequence_score": group.get("top_sequence_score"),
                "top_structural_sequence_score": group.get("top_structural_sequence_score"),
                "top_traversability_score": group.get("top_traversability_score"),
                "top_segment_success_rate": group.get("top_segment_success_rate"),
                "top_failed_segment_count": group.get("top_failed_segment_count"),
                "candidate_count": group.get("candidate_count"),
            }
            for group in same_direction_groups
        ]
        reasons: List[str] = []
        evidence_summary: List[str] = []
        variant_state = "no_variant_issue"
        if len(same_direction_groups) >= 2:
            top_group = same_direction_groups[0]
            next_group = same_direction_groups[1]
            top_score = _to_float(top_group.get("top_sequence_score")) or 0.0
            next_score = _to_float(next_group.get("top_sequence_score")) or 0.0
            score_gap = abs(float(top_score) - float(next_score))
            top_structural = _to_float(top_group.get("top_structural_sequence_score")) or top_score
            next_structural = _to_float(next_group.get("top_structural_sequence_score")) or next_score
            structural_gap = abs(float(top_structural) - float(next_structural))
            top_count = int(top_group.get("candidate_count") or 0)
            next_count = int(next_group.get("candidate_count") or 0)
            dominant_group_supported = (
                top_count >= 2
                and next_count <= 1
                and score_gap >= 5.0
                and structural_gap >= 5.0
            )
            head_same = str(top_group.get("head_signature") or "") == str(next_group.get("head_signature") or "")
            tail_same = str(top_group.get("tail_signature") or "") == str(next_group.get("tail_signature") or "")
            if dominant_group_supported:
                variant_state = "no_variant_issue"
                evidence_summary.append(
                    "Dominant same-direction candidate group has stronger support; secondary group treated as an alternate ordering."
                )
            elif (
                score_gap <= 4.0
                or structural_gap <= 4.0
                or (next_count >= 2 and min(score_gap, structural_gap) <= 8.0)
            ):
                variant_state = "unresolved_multi_variant"
            else:
                variant_state = "mild_variant_pressure_dominant_group"
            if variant_state != "no_variant_issue":
                if head_same and not tail_same:
                    reasons.append("divergent_tails_with_shared_corridor")
                elif tail_same and not head_same:
                    reasons.append("divergent_heads_with_shared_corridor")
                elif not head_same and not tail_same:
                    reasons.append("divergent_terminals_same_direction")
                else:
                    reasons.append("alternate_subsequence_same_direction")
                evidence_summary.append(
                    "Top same-direction variant groups disagree "
                    f"(score gap={score_gap:.2f}, structural gap={structural_gap:.2f}, dominant_orientation={dominant_orientation})."
                )
            top_network = _to_float(top_group.get("top_traversability_score"))
            next_network = _to_float(next_group.get("top_traversability_score"))
            if top_network is not None or next_network is not None:
                evidence_summary.append(
                    "Valhalla traversability comparison "
                    f"(top={top_network if top_network is not None else '-'}, "
                    f"next={next_network if next_network is not None else '-'})."
                )
        inverse_groups = [g for g in groups if str(g.get("sequence_orientation") or "forward") != dominant_orientation]
        if inverse_groups:
            evidence_summary.append(
                "Inverse-orientation candidates were separated from same-direction variant grouping."
            )
        return {
            "variant_groups": groups,
            "variant_group_count": len(groups),
            "variant_state": variant_state,
            "variant_pressure_detected": bool(variant_state != "no_variant_issue"),
            "variant_pressure_reasons": sorted(set(reasons)),
            "variant_evidence_summary": evidence_summary,
            "recommended_variant_group_key": groups[0].get("variant_group_key"),
            "recommended_candidates_by_variant": recommended_candidates_by_variant,
            "dominant_variant_group_key": (same_direction_groups[0].get("variant_group_key") if same_direction_groups else groups[0].get("variant_group_key")),
        }

    @staticmethod
    def _sequence_shortlist_row(candidate: Dict[str, Any]) -> Dict[str, Any]:
        metrics = dict(candidate.get("metrics") or {})
        identity = Phase3Client._sequence_candidate_variant_identity(candidate)
        return {
            "candidate_id": str(candidate.get("candidate_id") or ""),
            "rank": int(candidate.get("rank") or 0),
            "family": str(metrics.get("family") or metrics.get("mode") or "").strip() or None,
            "label": str(metrics.get("label") or metrics.get("mode") or "").strip() or None,
            "sequence_score": _to_float(metrics.get("sequence_score")),
            "score": _to_float(metrics.get("sequence_score")),
            "structural_sequence_score": _to_float(metrics.get("structural_sequence_score")),
            "combined_sequence_score": _to_float(metrics.get("combined_sequence_score")),
            "direction_consistency": _to_float(metrics.get("direction_consistency")),
            "terminal_consistency": _to_float(metrics.get("terminal_consistency")),
            "monotonic_spatial_progression": _to_float(metrics.get("monotonic_spatial_progression")),
            "risk_indicators": list(metrics.get("sequence_risk_indicators") or []),
            "source_families": list(metrics.get("source_families") or []),
            "valhalla_evidence_status": str(metrics.get("valhalla_evidence_status") or "").strip() or None,
            "traversability_score": _to_float(metrics.get("valhalla_traversability_score")),
            "segment_success_rate": _to_float(metrics.get("valhalla_segment_success_rate")),
            "failed_segment_count": _to_int(metrics.get("valhalla_failed_segment_count")),
            "detour_penalty": _to_float(metrics.get("valhalla_detour_penalty")),
            "network_backtrack_penalty": _to_float(metrics.get("valhalla_backtrack_penalty")),
            "path_vs_geodesic_ratio": _to_float(metrics.get("valhalla_path_vs_geodesic_ratio")),
            "network_risk_indicators": list(metrics.get("network_risk_indicators") or []),
            "valhalla_evidence_summary": list(metrics.get("valhalla_evidence_summary") or []),
            "variant_group_key": identity.get("variant_group_key"),
            "variant_group_label": identity.get("variant_group_label"),
            "sequence_orientation": identity.get("sequence_orientation"),
            "inverse_orientation_candidate": bool(identity.get("inverse_orientation_candidate")),
            "metrics": metrics,
        }

    def _get_direction_stability(self, route_id: uuid.UUID) -> Dict[str, Any]:
        self._ensure_direction_schema()
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT service_route_id::text AS service_route_id, direction_id::int AS direction_id
                    FROM route_raw.route_jobs
                    WHERE route_id = %s
                    LIMIT 1
                    """,
                    (str(route_id),),
                )
                job = self._row(cur.fetchone()) or {}
                sid = str(job.get("service_route_id") or "").strip()
                did = job.get("direction_id")
                if sid and did in (0, 1):
                    cur.execute(
                        """
                        SELECT
                          route_id::text AS route_id,
                          direction_approval_status,
                          COALESCE(phase3_progress_step, 0)::int AS phase3_progress_step
                        FROM route_raw.service_route_directions
                        WHERE service_route_id = %s::uuid
                          AND direction_id = %s
                        LIMIT 1
                        """,
                        (sid, int(did)),
                    )
                    row = self._row(cur.fetchone()) or {}
                else:
                    row = {}
        stable = True
        reasons: List[str] = []
        if str(job.get("service_route_id") or "").strip():
            if did not in (0, 1):
                stable = False
                reasons.append("direction_slot_missing")
            elif not row:
                stable = False
                reasons.append("direction_binding_missing")
            elif str(row.get("route_id") or "") != str(route_id):
                stable = False
                reasons.append("direction_binding_conflict")
        return {
            "service_route_id": job.get("service_route_id"),
            "direction_id": did,
            "direction_stable": bool(stable),
            "direction_reasons": reasons,
            "direction_row": row,
        }

    def get_sequence_resolution_state(self, route_id: uuid.UUID) -> Dict[str, Any]:
        self._ensure_sequence_resolution_schema()
        set_rows = self.list_stop_sequence_sets(route_id)
        latest_set_id = str(set_rows[0].get("set_id")) if set_rows else None
        candidates_payload = self.get_sequence_candidates(route_id) if latest_set_id else {"candidates": []}
        candidates = list(candidates_payload.get("candidates") or [])
        variant_summary = self._assess_variant_resolution(candidates)
        shortlist = [self._sequence_shortlist_row(c) for c in candidates[:5]]
        approval = self.get_sequence_approval(route_id)
        approval_status = str(approval.get("approval_status") or "").strip().lower() or None
        approved_candidate_id = str(approval.get("chosen_stop_sequence_candidate_id") or "").strip() or None
        approved_set_id = str(approval.get("stop_sequence_set_id") or "").strip() or None
        approval_matches_latest_set = bool(
            approval_status == "approved"
            and approved_candidate_id
            and latest_set_id
            and approved_set_id == latest_set_id
        )
        recommended = dict(candidates[0]) if candidates else {}
        recommended_id = str(recommended.get("candidate_id") or "").strip() or None
        recommended_metrics = dict(recommended.get("metrics") or {})
        recommended_identity = self._sequence_candidate_variant_identity(recommended) if recommended_id else {}
        approved_candidate = next(
            (dict(c) for c in candidates if str(c.get("candidate_id") or "").strip() == approved_candidate_id),
            {},
        )
        approved_candidate_is_valid = bool(approval_status == "approved" and approved_candidate_id and approved_candidate)
        approved_identity = self._sequence_candidate_variant_identity(approved_candidate) if approved_candidate_is_valid else {}
        recommended_variant_group_key = str(
            variant_summary.get("recommended_variant_group_key")
            or recommended_identity.get("variant_group_key")
            or ""
        ).strip() or None
        approved_variant_group_key = str(
            (approval.get("approved_variant_group_key") if approved_candidate_is_valid else None)
            or approved_identity.get("variant_group_key")
            or ""
        ).strip() or None
        current_signature = "prior:" + ",".join(
            str(int(r.get("seq") or 0))
            for r in sorted((dict(r) for r in (self.get_relation_stop_prior(route_id) or [])), key=lambda r: int(r.get("seq") or 0))
        )
        recommended_signature = self._sequence_candidate_signature(recommended) if recommended else None
        variant_state = str(variant_summary.get("variant_state") or "no_variant_issue")
        variant_pressure_detected = bool(variant_summary.get("variant_pressure_detected"))
        variant_pressure_reasons: List[str] = [str(x) for x in (variant_summary.get("variant_pressure_reasons") or []) if str(x).strip()]
        if bool(recommended_metrics.get("variant_pressure_detected")):
            variant_pressure_detected = True
            variant_pressure_reasons.extend(
                [str(x) for x in (recommended_metrics.get("variant_pressure_reasons") or []) if str(x).strip()]
            )
            if variant_state == "no_variant_issue":
                variant_state = "mild_variant_pressure_dominant_group"
        direction_state = self._get_direction_stability(route_id)
        sequence_stabilized = bool(approval_matches_latest_set and approved_candidate_id)
        reorder_needed = bool(recommended_signature and current_signature and recommended_signature != current_signature)
        return {
            "route_id": str(route_id),
            "latest_stop_sequence_set_id": latest_set_id,
            "candidate_count": len(candidates),
            "candidate_shortlist": shortlist,
            "recommended_stop_sequence_candidate_id": recommended_id,
            "recommended_signature": recommended_signature,
            "current_signature": current_signature,
            "reorder_needed": bool(reorder_needed),
            "sequence_stabilized": bool(sequence_stabilized),
            "approval_status": approval_status,
            "approved_stop_sequence_candidate_id": approved_candidate_id,
            "approved_stop_sequence_set_id": approved_set_id,
            "approval_matches_latest_set": bool(approval_matches_latest_set),
            "variant_state": variant_state,
            "variant_pressure_detected": bool(variant_pressure_detected),
            "variant_pressure_reasons": sorted(set(variant_pressure_reasons)),
            "variant_evidence_summary": list(variant_summary.get("variant_evidence_summary") or []),
            "variant_groups": list(variant_summary.get("variant_groups") or []),
            "variant_group_count": int(variant_summary.get("variant_group_count") or 0),
            "recommended_variant_group_key": recommended_variant_group_key,
            "approved_variant_group_key": approved_variant_group_key,
            "recommended_candidates_by_variant": list(variant_summary.get("recommended_candidates_by_variant") or []),
            "dominant_variant_group_key": variant_summary.get("dominant_variant_group_key"),
            "direction_stable": bool(direction_state.get("direction_stable")),
            "direction_reasons": list(direction_state.get("direction_reasons") or []),
            "service_route_id": direction_state.get("service_route_id"),
            "direction_id": direction_state.get("direction_id"),
            "sequence_approval": approval,
        }

    def invalidate_route_resolution(
        self,
        route_id: uuid.UUID,
        *,
        reason: str,
        clear_sequence_candidates: bool = True,
        clear_geometry_candidates: bool = True,
        clear_route_approvals: bool = True,
        clear_route_prod: bool = True,
        clear_sequence_approval: bool = True,
        demote_direction: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_direction_schema()
        self._ensure_sequence_resolution_schema()
        rid = str(route_id)
        out: Dict[str, Any] = {
            "route_id": rid,
            "reason": str(reason or "route_resolution_invalidated"),
            "deleted_stop_sequence_sets": 0,
            "deleted_geometry_sets": 0,
            "deleted_route_approvals": 0,
            "deleted_route_prod_rows": 0,
            "invalidated_sequence_approvals": 0,
            "deleted_service_route_approvals": 0,
            "demoted_direction_rows": 0,
        }
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT service_route_id::text AS service_route_id
                    FROM route_raw.route_jobs
                    WHERE route_id = %s
                    LIMIT 1
                    """,
                    (rid,),
                )
                job = self._row(cur.fetchone()) or {}
                service_route_id = str(job.get("service_route_id") or "").strip() or None

                if clear_sequence_approval:
                    cur.execute(
                        """
                        UPDATE route_work.sequence_approvals
                        SET approval_status = 'invalidated',
                            invalidated_at = now(),
                            invalidated_reason = %s,
                            updated_at = now()
                        WHERE route_id = %s
                          AND approval_status <> 'invalidated'
                        """,
                        (out["reason"], rid),
                    )
                    out["invalidated_sequence_approvals"] = int(cur.rowcount or 0)

                if clear_route_approvals:
                    cur.execute(
                        """
                        DELETE FROM route_work.route_approvals
                        WHERE route_id = %s
                        """,
                        (rid,),
                    )
                    out["deleted_route_approvals"] = int(cur.rowcount or 0)

                if clear_route_prod:
                    _del_result = _delete_route_prod_row(
                        conn=conn,
                        route_id=rid,
                        source_type=_SOURCE_TYPE_PHASE3,
                        pipeline_version=_PIPELINE_VERSION_INVALIDATE,
                        reason=str(out["reason"] or "route_resolution_invalidated"),
                    )
                    out["deleted_route_prod_rows"] = int(
                        _del_result.rows_affected.get("route_prod.routes", 0) or 0
                    )

                if clear_geometry_candidates:
                    cur.execute(
                        """
                        DELETE FROM route_work.geometry_candidate_sets
                        WHERE route_id = %s
                        """,
                        (rid,),
                    )
                    out["deleted_geometry_sets"] = int(cur.rowcount or 0)

                if clear_sequence_candidates:
                    cur.execute(
                        """
                        DELETE FROM route_work.stop_sequence_candidate_sets
                        WHERE route_id = %s
                        """,
                        (rid,),
                    )
                    out["deleted_stop_sequence_sets"] = int(cur.rowcount or 0)

                if service_route_id:
                    cur.execute(
                        """
                        DELETE FROM route_work.service_route_approvals
                        WHERE service_route_id = %s::uuid
                        """,
                        (service_route_id,),
                    )
                    out["deleted_service_route_approvals"] = int(cur.rowcount or 0)

                if demote_direction:
                    cur.execute(
                        """
                        UPDATE route_raw.service_route_directions
                        SET phase3_progress_step = LEAST(COALESCE(phase3_progress_step, 0), 2),
                            direction_approval_status = CASE
                              WHEN LEAST(COALESCE(phase3_progress_step, 0), 2) >= 2 THEN 'in_progress'
                              ELSE 'pending'
                            END,
                            approved_at = NULL,
                            approved_by = NULL,
                            updated_at = now()
                        WHERE route_id = %s::uuid
                        """,
                        (rid,),
                    )
                    out["demoted_direction_rows"] = int(cur.rowcount or 0)

                cur.execute(
                    """
                    UPDATE route_raw.route_jobs
                    SET status = CASE
                      WHEN status = 'approved' THEN 'needs_rebuild'
                      ELSE status
                    END
                    WHERE route_id = %s
                    """,
                    (rid,),
                )

                if service_route_id:
                    try:
                        self._refresh_service_route_status(conn, service_route_id=service_route_id)
                    except Exception:
                        pass
            try:
                conn.commit()
            except Exception:
                pass
        self._safe_ai_log_phase3(
            stage="sequence_invalidation",
            route_id=rid,
            payload=out,
            warnings=["sequence_resolution_invalidated"],
            notes=[str(reason or "route_resolution_invalidated")],
        )
        return out

    def _rows_reordered_for_candidate(
        self,
        route_id: uuid.UUID,
        candidate_id: uuid.UUID,
    ) -> Optional[List[Dict[str, Any]]]:
        candidate = self.get_stop_sequence_candidate(candidate_id)
        prior_rows = self.get_relation_stop_prior(route_id)
        prior_seqs = _parse_int_array(candidate.get("stop_prior_seqs"))
        if not prior_rows or not prior_seqs:
            return None
        current = sorted((dict(r) for r in prior_rows), key=lambda r: int(r.get("seq") or 0))
        seq_values = [int(r.get("seq") or 0) for r in current]
        if sorted(prior_seqs) != sorted(seq_values):
            return None
        by_seq = {int(r.get("seq") or 0): dict(r) for r in current}
        out: List[Dict[str, Any]] = []
        for idx, seq in enumerate(prior_seqs, start=1):
            row = dict(by_seq[int(seq)])
            row["seq"] = idx
            out.append(row)
        return out

    def approve_stop_sequence_candidate(
        self,
        *,
        route_id: uuid.UUID,
        stop_sequence_candidate_id: uuid.UUID,
        approved_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._ensure_sequence_resolution_schema()
        approver = approved_by or os.getenv("USER") or os.getenv("USERNAME") or "console"
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT ssc.candidate_id, ssc.set_id, scs.route_id, ssc.metrics
                    FROM route_work.stop_sequence_candidates ssc
                    JOIN route_work.stop_sequence_candidate_sets scs
                      ON scs.set_id = ssc.set_id
                    WHERE ssc.candidate_id = %s
                    LIMIT 1
                    """,
                    (str(stop_sequence_candidate_id),),
                )
                row = self._row(cur.fetchone())
        if not row:
            raise ValueError("stop_sequence_candidate_id not found")
        if str(row.get("route_id") or "") != str(route_id):
            raise ValueError("stop_sequence_candidate_id does not belong to route_id")

        self.invalidate_route_resolution(
            route_id,
            reason="canonical_sequence_changed",
            clear_sequence_candidates=False,
            clear_geometry_candidates=True,
            clear_route_approvals=True,
            clear_route_prod=True,
            clear_sequence_approval=False,
            demote_direction=True,
        )
        reordered = self._rows_reordered_for_candidate(route_id, stop_sequence_candidate_id)
        if reordered:
            self.replace_relation_stop_prior(
                route_id,
                reordered,
                edit_source="sequence_approval_reorder",
                invalidate_resolution=False,
            )

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_work.sequence_approvals (
                      route_id,
                      stop_sequence_set_id,
                      chosen_stop_sequence_candidate_id,
                      approval_status,
                      approved_at,
                      approved_by,
                      notes,
                      invalidated_at,
                      invalidated_reason
                    )
                    VALUES (%s, %s, %s, 'approved', now(), %s, %s, NULL, NULL)
                    ON CONFLICT (route_id)
                    DO UPDATE SET
                      stop_sequence_set_id = EXCLUDED.stop_sequence_set_id,
                      chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
                      approval_status = 'approved',
                      approved_at = now(),
                      approved_by = EXCLUDED.approved_by,
                      notes = EXCLUDED.notes,
                      invalidated_at = NULL,
                      invalidated_reason = NULL,
                      updated_at = now()
                    RETURNING sequence_approval_id, approved_at
                    """,
                    (
                        str(route_id),
                        str(row.get("set_id")),
                        str(stop_sequence_candidate_id),
                        approver,
                        notes,
                    ),
                )
                approval_row = self._row(cur.fetchone()) or {}
            try:
                conn.commit()
            except Exception:
                pass
        try:
            self.mark_direction_progress_by_route(
                route_id=str(route_id),
                step=2,
                progress_notes="Canonical sequence approved; geometry build unlocked.",
            )
        except Exception:
            pass
        out = {
            "route_id": str(route_id),
            "sequence_approval_id": approval_row.get("sequence_approval_id"),
            "approved_stop_sequence_candidate_id": str(stop_sequence_candidate_id),
            "stop_sequence_set_id": str(row.get("set_id")),
            "approved_at": approval_row.get("approved_at"),
            "approved_by": approver,
            "reordered_relation_stop_prior": bool(reordered),
            "approved_variant_group_key": str(dict(row.get("metrics") or {}).get("variant_group_key") or "").strip() or None,
            "approved_variant_group_label": str(dict(row.get("metrics") or {}).get("variant_group_label") or "").strip() or None,
        }
        self._safe_ai_log_phase3(
            stage="sequence_approval",
            route_id=str(route_id),
            payload=out,
            notes=["operator_sequence_approval_required"],
        )
        return out

    def get_step30_gate(
        self,
        route_id: uuid.UUID,
        *,
        stop_sequence_candidate_id: Optional[uuid.UUID] = None,
        match_radius_m: float = 3.0,
    ) -> Dict[str, Any]:
        resolution = self.get_sequence_resolution_state(route_id)
        match_report = self.get_prior_match_report(route_id, radius_m=float(match_radius_m), write=False)
        approved_candidate_id = str(resolution.get("approved_stop_sequence_candidate_id") or "").strip() or None
        requested_candidate_id = (
            str(stop_sequence_candidate_id) if stop_sequence_candidate_id is not None else approved_candidate_id
        )
        reasons: List[str] = []
        if not bool(match_report.get("all_matched")):
            reasons.append("matching_incomplete")
        if not bool(resolution.get("direction_stable")):
            reasons.append("direction_instability")
        if not bool(resolution.get("sequence_stabilized")):
            reasons.append("sequence_not_approved")
        if str(resolution.get("variant_state") or "") == "unresolved_multi_variant" and not bool(
            resolution.get("sequence_stabilized")
        ):
            reasons.append("variant_pressure_blocking")
        if approved_candidate_id and requested_candidate_id and requested_candidate_id != approved_candidate_id:
            reasons.append("requested_sequence_not_canonical")
        return {
            "route_id": str(route_id),
            "all_matched": bool(match_report.get("all_matched")),
            "total_count": int(match_report.get("total") or 0),
            "total": int(match_report.get("total") or 0),
            "matched_count": int(match_report.get("matched") or 0),
            "unmatched_count": int(match_report.get("unmatched") or 0),
            "ambiguous_count": int(match_report.get("ambiguous") or 0),
            "approved_stop_sequence_candidate_id": approved_candidate_id,
            "requested_stop_sequence_candidate_id": requested_candidate_id,
            "sequence_stabilized": bool(resolution.get("sequence_stabilized")),
            "approval_status": resolution.get("approval_status"),
            "variant_state": resolution.get("variant_state"),
            "variant_pressure_detected": bool(resolution.get("variant_pressure_detected")),
            "variant_pressure_reasons": list(resolution.get("variant_pressure_reasons") or []),
            "variant_groups": list(resolution.get("variant_groups") or []),
            "recommended_variant_group_key": resolution.get("recommended_variant_group_key"),
            "approved_variant_group_key": resolution.get("approved_variant_group_key"),
            "direction_stable": bool(resolution.get("direction_stable")),
            "direction_reasons": list(resolution.get("direction_reasons") or []),
            "blocking_reasons": reasons,
            "can_run_step30": len(reasons) == 0,
        }

    def get_sequence_geometry(self, stop_sequence_candidate_id: uuid.UUID) -> Dict[str, Any]:
        """
        Returns GeoJSON LineString for a stop sequence candidate.
        """
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT ssc.candidate_id, ssc.stop_node_ids, ssc.stop_prior_seqs, ssc.set_id, scs.route_id
                    FROM route_work.stop_sequence_candidates ssc
                    JOIN route_work.stop_sequence_candidate_sets scs
                      ON scs.set_id = ssc.set_id
                    WHERE ssc.candidate_id = %s
                    """,
                    (str(stop_sequence_candidate_id),),
                )
                row = cur.fetchone()

            if not row:
                return {
                    "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
                    "route_id": None,
                    "geojson": None,
                }

            route_id = uuid.UUID(str(row.get("route_id")))
            stop_node_ids = _parse_uuid_array(row.get("stop_node_ids"))
            stop_prior_seqs = _parse_int_array(row.get("stop_prior_seqs"))

            coords: List[LonLat]
            if stop_node_ids:
                coords = self._fetch_stop_points_from_canonical(conn, stop_node_ids)
            else:
                coords = self._fetch_stop_points_from_prior(conn, route_id, stop_prior_seqs)

        if len(coords) < 2:
            return {
                "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
                "route_id": str(route_id),
                "geojson": None,
            }

        geometry = {
            "type": "LineString",
            "coordinates": [[lon, lat] for (lon, lat) in coords],
        }
        geojson = {
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "route_id": str(route_id),
                "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
            },
        }
        return {
            "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
            "route_id": str(route_id),
            "geojson": geojson,
        }

    def get_geometry_candidates(
        self,
        route_id: uuid.UUID,
        *,
        geometry_set_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """
        Returns geometry candidates for a route_id (latest set by default).
        """
        sets = self.list_geometry_sets(route_id)
        if not sets:
            return {"route_id": str(route_id), "geometry_set_id": None, "candidates": []}

        if geometry_set_id is None:
            geometry_set_id = uuid.UUID(str(sets[0]["set_id"]))
        else:
            allowed = {str(s.get("set_id")) for s in sets if s.get("set_id")}
            if str(geometry_set_id) not in allowed:
                raise ValueError(
                    f"geometry_set_id {geometry_set_id} does not belong to route_id {route_id}"
                )

        cands = self.list_geometry_candidates(geometry_set_id)
        return {
            "route_id": str(route_id),
            "geometry_set_id": str(geometry_set_id),
            "candidates": cands,
        }

    def get_geometry_candidate_geometry(self, geometry_candidate_id: uuid.UUID) -> Dict[str, Any]:
        """
        Returns GeoJSON for a geometry candidate.
        """
        cand = self.get_geometry_candidate(geometry_candidate_id)
        wkt = (cand.get("geom_wkt") or "").strip()
        coords = _parse_linestring_wkt(wkt)
        if len(coords) < 2:
            return {"geometry_candidate_id": str(geometry_candidate_id), "geojson": None}

        geometry = {
            "type": "LineString",
            "coordinates": [[lon, lat] for (lon, lat) in coords],
        }
        geojson = {
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "geometry_candidate_id": str(geometry_candidate_id),
                "set_id": cand.get("set_id"),
                "stop_sequence_candidate_id": cand.get("stop_sequence_candidate_id"),
            },
        }
        return {"geometry_candidate_id": str(geometry_candidate_id), "geojson": geojson}

    def get_geometry_stop_recovery_set(
        self,
        route_id: uuid.UUID,
        *,
        geometry_set_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        self._ensure_geometry_stop_recovery_schema()
        if geometry_set_id is None:
            sets = self.list_geometry_sets(route_id)
            if not sets:
                return {"route_id": str(route_id), "geometry_set_id": None, "candidates": []}
            geometry_set_id = uuid.UUID(str(sets[0]["set_id"]))

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      gsr.geometry_candidate_id,
                      gsr.set_id,
                      gsr.route_id,
                      gsr.stop_sequence_candidate_id,
                      gsr.original_stop_ids,
                      gsr.original_stop_prior_seqs,
                      gsr.recovered_stop_ids,
                      gsr.ambiguous_nearby_stop_ids,
                      gsr.rejected_nearby_stop_ids,
                      gsr.enriched_stop_ids,
                      gsr.insertion_proposals,
                      gsr.provenance,
                      gsr.summary_metrics,
                      gsr.updated_at
                    FROM route_work.geometry_stop_recovery gsr
                    WHERE gsr.route_id = %s
                      AND gsr.set_id = %s
                    ORDER BY gsr.updated_at DESC, gsr.geometry_candidate_id ASC
                    """,
                    (str(route_id), str(geometry_set_id)),
                )
                rows = cur.fetchall() or []

        return {
            "route_id": str(route_id),
            "geometry_set_id": str(geometry_set_id),
            "candidates": [self._row(r) for r in rows],
        }

    def get_ranked_candidates(
        self,
        route_id: uuid.UUID,
        *,
        geometry_set_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """
        Returns ranked candidates for a geometry set (latest by default).
        """
        if geometry_set_id is None:
            sets = self.list_geometry_sets(route_id)
            if not sets:
                return {"route_id": str(route_id), "geometry_set_id": None, "candidates": []}
            geometry_set_id = uuid.UUID(str(sets[0]["set_id"]))

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      gc.set_id,
                      gc.geometry_candidate_id,
                      gc.stop_sequence_candidate_id,
                      grl.label,
                      NULLIF(gc.metrics->>'ml_rank', '')::int AS ml_rank,
                      gc.score,
                      gc.length_m,
                      gc.avg_stop_dist_m,
                      gc.max_stop_dist_m,
                      gc.metrics,
                      gc.created_at
                    FROM route_work.geometry_candidates gc
                    LEFT JOIN route_work.geometry_ranking_labels grl
                      ON grl.set_id = gc.set_id
                     AND grl.geometry_candidate_id = gc.geometry_candidate_id
                    WHERE gc.set_id = %s
                    ORDER BY
                      grl.label DESC NULLS LAST,
                      NULLIF(gc.metrics->>'ml_rank', '')::int ASC NULLS LAST,
                      gc.score DESC NULLS LAST,
                      gc.created_at ASC
                    """,
                    (str(geometry_set_id),),
                )
                rows = cur.fetchall() or []

        return {
            "route_id": str(route_id),
            "geometry_set_id": str(geometry_set_id),
            "candidates": [self._row(r) for r in rows],
        }

    def approve_geometry(
        self,
        *,
        route_id: uuid.UUID,
        geometry_candidate_id: uuid.UUID,
        stop_sequence_candidate_id: Optional[uuid.UUID] = None,
        approved_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """
        Approve a specific geometry candidate and upsert into route_prod.routes.
        """
        self._ensure_sequence_resolution_schema()
        approved_by = approved_by or os.getenv("USER") or os.getenv("USERNAME") or "console"

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT gc.geometry_candidate_id, gc.geom, gc.stop_sequence_candidate_id,
                           gc.valhalla_request, gcs.route_id
                    FROM route_work.geometry_candidates gc
                    JOIN route_work.geometry_candidate_sets gcs
                      ON gcs.set_id = gc.set_id
                    WHERE gc.geometry_candidate_id = %s
                    """,
                    (str(geometry_candidate_id),),
                )
                row = cur.fetchone()

                if not row:
                    raise ValueError("geometry_candidate_id not found")
                if str(row.get("route_id")) != str(route_id):
                    raise ValueError("geometry_candidate_id does not belong to route_id")

                chosen_seq_id = stop_sequence_candidate_id or row.get("stop_sequence_candidate_id")
                cur.execute(
                    """
                    SELECT
                      sa.chosen_stop_sequence_candidate_id,
                      sa.approval_status,
                      sa.approved_at AS sequence_approved_at,
                      sa.approved_by AS sequence_approved_by,
                      rj.service_route_id::text AS service_route_id,
                      rj.direction_id::int AS direction_id,
                      rj.province AS province
                    FROM route_raw.route_jobs rj
                    LEFT JOIN route_work.sequence_approvals sa
                      ON sa.route_id = rj.route_id
                    WHERE rj.route_id = %s
                    LIMIT 1
                    """,
                    (str(route_id),),
                )
                state_row = self._row(cur.fetchone()) or {}
                approved_seq_id = str(state_row.get("chosen_stop_sequence_candidate_id") or "").strip() or None
                approval_status = str(state_row.get("approval_status") or "").strip().lower()
                sequence_approved_at = state_row.get("sequence_approved_at")
                sequence_approved_by = state_row.get("sequence_approved_by") or approved_by
                province_val = state_row.get("province")
                if not approved_seq_id or approval_status != "approved":
                    raise RuntimeError("Canonical stop sequence is not approved for this route.")
                if str(chosen_seq_id or "") != approved_seq_id:
                    raise RuntimeError("geometry_candidate_id is not linked to the approved canonical stop sequence.")
                if province_val is None or not str(province_val).strip():
                    raise RuntimeError(
                        f"route_raw.route_jobs.province is NULL/empty for route_id={route_id}. "
                        "Skill 11 §7 forbids falling back to the 'sample_region' DEFAULT. "
                        "Fix the upstream INSERT to route_raw.route_jobs to set province explicitly."
                    )

                stop_node_ids: List[uuid.UUID] = []
                if chosen_seq_id:
                    cur.execute(
                        """
                        SELECT stop_node_ids
                        FROM route_work.stop_sequence_candidates
                        WHERE candidate_id = %s
                        """,
                        (str(chosen_seq_id),),
                    )
                    srow = cur.fetchone()
                    stop_node_ids = _parse_uuid_array(srow.get("stop_node_ids") if srow else None)

                cur.execute(
                    """
                    INSERT INTO route_work.route_approvals
                      (route_id, chosen_geometry_candidate_id, chosen_stop_sequence_candidate_id, approved_by, notes)
                    VALUES
                      (%s, %s, %s, %s, %s)
                    ON CONFLICT (route_id)
                    DO UPDATE SET
                      chosen_geometry_candidate_id = EXCLUDED.chosen_geometry_candidate_id,
                      chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
                      approved_at = now(),
                      approved_by = EXCLUDED.approved_by,
                      notes = EXCLUDED.notes
                    """,
                    (
                        str(route_id),
                        str(geometry_candidate_id),
                        str(chosen_seq_id) if chosen_seq_id else None,
                        approved_by,
                        notes,
                    ),
                )

                # Route through the canonical persistence wrapper.
                # NOTE: the wrapper's "skip if None" rule reproduces the
                # legacy COALESCE(EXCLUDED.x, route_prod.routes.x) semantics
                # for service_route_id and direction_id — callers pass None
                # → column is omitted from the UPSERT → existing value is
                # preserved on conflict.
                _sr = state_row.get("service_route_id")
                _dir = state_row.get("direction_id")
                _route_data: Dict[str, Any] = {
                    "route_id": str(route_id),
                    "province": province_val,
                    "source": "route_constructor",
                    "chosen_geometry_candidate_id": str(geometry_candidate_id),
                    "chosen_stop_sequence_candidate_id": approved_seq_id,
                    "canonical_sequence_ready": True,
                    "sequence_approved_at": sequence_approved_at,
                    "sequence_approved_by": sequence_approved_by,
                }
                if _sr is not None:
                    _route_data["service_route_id"] = _sr
                if _dir is not None:
                    _route_data["direction_id"] = _dir
                _write_result = write_to_route_prod(
                    route_code=str(route_id),
                    route_data=_route_data,
                    stops=[str(x) for x in stop_node_ids],
                    shape={"raw": row.get("geom")},
                    source_type="phase3_client_approve_geometry",
                    pipeline_version=_PIPELINE_VERSION_APPROVAL,
                    conn=conn,
                    mode="upsert",
                    valhalla_request=row.get("valhalla_request"),
                )
                if not _write_result.success:
                    raise RuntimeError(
                        f"route_prod write failed for {route_id}: {_write_result.error}"
                    )

                cur.execute(
                    """
                    UPDATE route_raw.route_jobs
                    SET status = 'approved'
                    WHERE route_id = %s
                    """,
                    (str(route_id),),
                )

            try:
                conn.commit()
            except Exception:
                pass
        try:
            self.mark_direction_approved_by_route(route_id=str(route_id), approved_by=approved_by)
        except Exception:
            pass
        try:
            self._resolve_linked_coverage_gaps_for_route(
                route_id=str(route_id),
                reviewed_by=approved_by,
                notes="Resolved via direct Phase 3 geometry approval.",
            )
        except Exception:
            pass

    def get_production_route_geometry(self, route_id: uuid.UUID) -> Dict[str, Any]:
        """
        Returns GeoJSON for route_prod.routes geometry.
        """
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT route_id, chosen_geometry_candidate_id, ST_AsText(geom) AS geom_wkt
                    FROM route_prod.routes
                    WHERE route_id = %s
                    """,
                    (str(route_id),),
                )
                row = cur.fetchone()

        if not row:
            return {"route_id": str(route_id), "geojson": None}

        coords = _parse_linestring_wkt((row.get("geom_wkt") or "").strip())
        if len(coords) < 2:
            return {"route_id": str(route_id), "geojson": None}

        geometry = {
            "type": "LineString",
            "coordinates": [[lon, lat] for (lon, lat) in coords],
        }
        geojson = {
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "route_id": str(route_id),
                "chosen_geometry_candidate_id": row.get("chosen_geometry_candidate_id"),
            },
        }
        return {"route_id": str(route_id), "geojson": geojson}



    def run_step_10_fetch(
        self,
        *,
        route_id: uuid.UUID | str,
        osm_relation_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Mirrors CLI:

            10_fetch_relation.py <route_id|new> [osm_relation_id]

        If osm_relation_id is provided → pass explicitly.
        If None → script auto-reads chosen_osm_relation_id from DB.
        """

        script_path = _script_path("10_fetch_relation.py")
        overpass_urls = _phase3_overpass_urls(self.overpass_url)
        candidate_rows: List[Dict[str, Any]] = []
        try:
            candidate_rows = list(self.list_relation_candidates(route_id=uuid.UUID(str(route_id))) or [])
        except Exception:
            candidate_rows = []

        # Resolve relation id from DB if not explicitly provided.
        rid_txt = str(route_id)
        if osm_relation_id is None:
            try:
                with db_conn() as conn:
                    with db_cursor(conn) as cur:
                        cur.execute(
                            "SELECT chosen_osm_relation_id FROM route_raw.route_jobs WHERE route_id = %s",
                            (rid_txt,),
                        )
                        row = cur.fetchone() or {}
                        val = row.get("chosen_osm_relation_id")
                        if val:
                            osm_relation_id = int(val)
            except Exception:
                pass
        if osm_relation_id is None:
            candidates = list(candidate_rows or [])
            chosen = [c for c in candidates if c.get("is_chosen")]
            if chosen:
                osm_relation_id = int(chosen[0]["osm_relation_id"])
                try:
                    self.set_chosen_relation(uuid.UUID(rid_txt), osm_relation_id)
                except Exception:
                    pass
            elif len(candidates) == 1:
                osm_relation_id = int(candidates[0]["osm_relation_id"])
                try:
                    self.set_chosen_relation(uuid.UUID(rid_txt), osm_relation_id)
                except Exception:
                    pass
            elif candidates:
                preview = ", ".join(str(c.get("osm_relation_id")) for c in candidates[:6])
                extra = "" if len(candidates) <= 6 else f" (+{len(candidates) - 6} more)"
                raise RuntimeError(
                    "No chosen osm_relation_id set for this route. "
                    f"Candidates found: {preview}{extra}. "
                    "Select one in Step 05 (Discover) or pass osm_relation_id explicitly."
                )

        existing_raw_relation_ids: List[int] = []
        try:
            existing_raw_relation_ids = list(self.list_relation_raw_ids(uuid.UUID(rid_txt)) or [])
        except Exception:
            existing_raw_relation_ids = []
        if osm_relation_id is not None and int(osm_relation_id) in {int(v) for v in existing_raw_relation_ids if _to_int(v) is not None}:
            selected_summary: Dict[str, Any] = {}
            for row in list(candidate_rows or []):
                rid = _to_int(row.get("osm_relation_id"))
                if rid is None:
                    continue
                if int(rid) != int(osm_relation_id):
                    continue
                selected_summary = {
                    "osm_relation_id": rid,
                    "selection_rank": _to_int(row.get("selection_rank")),
                    "selection_confidence": _to_float(row.get("selection_confidence")),
                    "score": _to_float(row.get("score")),
                    "stop_prior_count": _to_int(row.get("stop_prior_count")),
                    "query_strategy": row.get("query_strategy"),
                    "selection_reason_codes": list(row.get("selection_reason_codes") or []),
                    "matched_soft_signals": list(row.get("matched_soft_signals") or []),
                }
                break
            payload = {
                "route_id": str(route_id),
                "osm_relation_id": int(osm_relation_id),
                "stored": True,
                "already_stored": True,
                "fetch_relation_stored": True,
                "fetch_status": "already_stored",
                "fetch_status_classification": "already_stored",
                "fetch_observability_gap": False,
                "candidate_universe_count": int(len(candidate_rows or [])),
                "selected_relation_summary": selected_summary or None,
            }
            self._persist_route_job_extractor_review(
                route_id=str(route_id),
                review_patch={
                    "fetch": {
                        "route_id": str(route_id),
                        "osm_relation_id": int(osm_relation_id),
                        "stored": True,
                        "already_stored": True,
                        "fetch_relation_stored": True,
                        "fetch_status": "already_stored",
                        "fetch_status_classification": "already_stored",
                        "fetch_observability_gap": False,
                        "candidate_universe_count": int(len(candidate_rows or [])),
                        "selected_relation_summary": dict(selected_summary or {}),
                    },
                    "discover": {
                        "selected_relation_summary": dict(selected_summary or {}),
                        "relation_fetch_success": True,
                    },
                },
            )
            self._safe_ai_log_phase3(
                stage="step_10_fetch",
                route_id=str(route_id),
                payload={
                    **payload,
                    "progressed_to_next_step": True,
                },
            )
            return payload

        # Build args exactly like CLI
        args = [sys.executable, script_path, str(route_id)]

        if osm_relation_id is not None:
            args.append(str(osm_relation_id))

        result = None
        last_err = None
        for url in overpass_urls:
            result = _run_script(args, env_overrides={"OVERPASS_URL": url})
            if result.returncode == 0:
                break
            last_err = result
            if "429" in (result.stderr or "") or "504" in (result.stderr or ""):
                continue
            break

        if result is None or result.returncode != 0:
            raise RuntimeError(
                f"Step 10 failed:\nSTDOUT:\n{(last_err.stdout if last_err else '')}\nSTDERR:\n{(last_err.stderr if last_err else '')}"
            )

        # Parse machine-readable JSON line
        payload = None

        for line in result.stdout.splitlines():
            if line.startswith("P3_JSON:"):
                payload = json.loads(line.replace("P3_JSON:", "").strip())
                break

        if not payload:
            raise RuntimeError(
                "Step 10 succeeded but no P3_JSON payload was found in output."
            )
        selected_summary: Dict[str, Any] = {}
        for row in list(candidate_rows or []):
            rid = _to_int(row.get("osm_relation_id"))
            if rid is None or osm_relation_id is None:
                continue
            if int(rid) != int(osm_relation_id):
                continue
            selected_summary = {
                "osm_relation_id": rid,
                "selection_rank": _to_int(row.get("selection_rank")),
                "selection_confidence": _to_float(row.get("selection_confidence")),
                "score": _to_float(row.get("score")),
                "stop_prior_count": _to_int(row.get("stop_prior_count")),
                "query_strategy": row.get("query_strategy"),
                "selection_reason_codes": list(row.get("selection_reason_codes") or []),
                "matched_soft_signals": list(row.get("matched_soft_signals") or []),
            }
            break
        payload["candidate_universe_count"] = int(len(candidate_rows or []))
        payload["selected_relation_summary"] = selected_summary or None
        payload["fetch_relation_stored"] = bool(payload.get("stored"))
        payload["fetch_status"] = "success" if bool(payload.get("stored")) else "error"
        payload["fetch_status_classification"] = (
            "fetch_partial_after_valid_selection"
            if (
                selected_summary
                and int(_to_int(selected_summary.get("stop_prior_count")) or 0) > 0
                and payload.get("raw_count") is None
                and payload.get("candidate_count") is None
            )
            else payload["fetch_status"]
        )
        payload["fetch_observability_gap"] = bool(
            payload.get("fetch_status_classification") == "fetch_partial_after_valid_selection"
        )
        self._persist_route_job_extractor_review(
            route_id=str(route_id),
            review_patch={
                "fetch": {
                    "route_id": str(route_id),
                    "osm_relation_id": osm_relation_id,
                    "stored": bool(payload.get("stored")),
                    "fetch_relation_stored": bool(payload.get("fetch_relation_stored")),
                    "fetch_status": payload.get("fetch_status"),
                    "fetch_status_classification": payload.get("fetch_status_classification"),
                    "fetch_observability_gap": bool(payload.get("fetch_observability_gap")),
                    "candidate_universe_count": int(len(candidate_rows or [])),
                    "selected_relation_summary": dict(selected_summary or {}),
                },
                "discover": {
                    "selected_relation_summary": dict(selected_summary or {}),
                    "relation_fetch_success": bool(payload.get("fetch_relation_stored")),
                },
            },
        )

        try:
            self.mark_direction_progress_by_route(
                route_id=str(route_id),
                step=1,
                progress_notes="Step 10 fetch completed.",
            )
        except Exception:
            pass

        self._safe_ai_log_phase3(
            stage="step_10_fetch",
            route_id=str(route_id),
            payload={
                **dict(payload or {}),
                "route_id": str(route_id),
                "osm_relation_id": osm_relation_id,
                "candidate_universe_count": int(len(candidate_rows or [])),
                "selected_relation_summary": selected_summary or None,
                "progressed_to_next_step": True,
            },
        )
        return payload

    @staticmethod
    def _build_phase3_target_attempts(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
        document = dict(doc or {})
        region = str(
            document.get("region")
            or document.get("catalog_name")
            or document.get("catalog_id")
            or document.get("document_name")
            or ""
        ).strip()
        default_group = _phase3_catalog_group_for_place(region, default_region=region)

        places = [dict(row or {}) for row in list(document.get("target_places") or []) if isinstance(row, dict)]
        priority_places = [dict(row or {}) for row in list(document.get("priority_places") or []) if isinstance(row, dict)]
        suggested = [dict(row or {}) for row in list(document.get("suggested_combinations") or []) if isinstance(row, dict)]
        seed_combos = [dict(row or {}) for row in list(document.get("seed_extraction_combos") or []) if isinstance(row, dict)]
        operator_hint_pairs = [dict(row or {}) for row in list(document.get("operator_hint_pairs") or []) if isinstance(row, dict)]
        place_bundles = [
            dict(row or {})
            for row in list(document.get("priority_place_bundles") or document.get("place_bundles") or [])
            if isinstance(row, dict)
        ]

        place_index: Dict[str, Dict[str, Any]] = {}
        operator_index: Dict[str, Dict[str, Any]] = {}
        route_hint_rows: List[Dict[str, Any]] = []

        def register_place(
            name: Any,
            *,
            group: Optional[str] = None,
            priority: Optional[str] = None,
            place_kind: Optional[str] = None,
            seed_origin: Optional[str] = None,
            confidence: Optional[str] = None,
            place_bundle: Optional[str] = None,
            bbox_hint: Optional[Dict[str, Any]] = None,
        ) -> None:
            place = str(name or "").strip()
            norm = normalize_geographic_text(place)
            if not place or not norm:
                return
            group_txt = (
                str(group or "").strip()
                or _phase3_catalog_group_for_place(place, place_kind=place_kind, default_region=region)
                or default_group
            )
            priority_txt = _normalized_priority_label(
                priority or confidence,
                fallback=(
                    "high"
                    if str(seed_origin or "").strip() in {"target_places", "priority_places", "place_seeds_primary", "parishes_official"}
                    else "medium"
                ),
            )
            payload = place_index.get(norm)
            if payload is None:
                place_index[norm] = {
                    "name": place,
                    "group": group_txt,
                    "priority": priority_txt,
                    "place_kind": (str(place_kind).strip() if place_kind else None),
                    "seed_origin": (str(seed_origin).strip() if seed_origin else None),
                    "catalog_confidence": (str(confidence).strip() if confidence else None),
                    "place_bundle": (
                        str(place_bundle).strip()
                        if place_bundle
                        else _phase3_catalog_place_bundle(place, place_kind=place_kind)
                    ),
                    "bbox_hint": _coerce_bbox_dict(bbox_hint) or _phase3_catalog_bbox_hint(place, group=group_txt),
                }
                return
            payload["priority"] = _better_priority(payload.get("priority"), priority_txt)
            if not payload.get("group") and group_txt:
                payload["group"] = group_txt
            if not payload.get("place_kind") and place_kind:
                payload["place_kind"] = str(place_kind).strip()
            if not payload.get("seed_origin") and seed_origin:
                payload["seed_origin"] = str(seed_origin).strip()
            if (
                confidence is not None
                and (
                    payload.get("catalog_confidence") is None
                    or _confidence_rank(confidence) < _confidence_rank(payload.get("catalog_confidence"))
                )
            ):
                payload["catalog_confidence"] = str(confidence).strip()
            if not payload.get("place_bundle"):
                payload["place_bundle"] = (
                    str(place_bundle).strip()
                    if place_bundle
                    else _phase3_catalog_place_bundle(place, place_kind=place_kind)
                )
            if not payload.get("bbox_hint"):
                payload["bbox_hint"] = _coerce_bbox_dict(bbox_hint) or _phase3_catalog_bbox_hint(place, group=group_txt)

        def register_operator(
            name: Any,
            *,
            seed_origin: Optional[str] = None,
            confidence: Optional[str] = None,
            aliases: Optional[Iterable[Any]] = None,
        ) -> None:
            operator_name = str(name or "").strip()
            norm = normalize_geographic_text(operator_name)
            if not operator_name or not norm:
                return
            row = operator_index.get(norm)
            if row is None:
                row = {
                    "name": operator_name,
                    "seed_origin": (str(seed_origin).strip() if seed_origin else None),
                    "catalog_confidence": (str(confidence).strip() if confidence else None),
                    "aliases": _dedupe_text_rows(aliases or []),
                }
                operator_index[norm] = row
                return
            if (
                confidence is not None
                and (
                    row.get("catalog_confidence") is None
                    or _confidence_rank(confidence) < _confidence_rank(row.get("catalog_confidence"))
                )
            ):
                row["catalog_confidence"] = str(confidence).strip()
            if not row.get("seed_origin") and seed_origin:
                row["seed_origin"] = str(seed_origin).strip()
            row["aliases"] = _dedupe_text_rows([*(row.get("aliases") or []), *(list(aliases or []))])

        def register_route_hint(
            hint: Any,
            *,
            seed_origin: Optional[str] = None,
            confidence: Optional[str] = None,
        ) -> None:
            route_hint = str(hint or "").strip()
            norm = normalize_geographic_text(route_hint)
            if not route_hint or not norm:
                return
            for existing in route_hint_rows:
                if normalize_geographic_text(existing.get("route_hint")) == norm:
                    if (
                        confidence is not None
                        and (
                            existing.get("catalog_confidence") is None
                            or _confidence_rank(confidence) < _confidence_rank(existing.get("catalog_confidence"))
                        )
                    ):
                        existing["catalog_confidence"] = str(confidence).strip()
                    return
            route_hint_rows.append(
                {
                    "route_hint": route_hint,
                    "seed_origin": (str(seed_origin).strip() if seed_origin else None),
                    "catalog_confidence": (str(confidence).strip() if confidence else None),
                }
            )

        for row in places:
            register_place(
                row.get("name"),
                group=row.get("group"),
                priority=row.get("priority"),
                place_kind=row.get("kind"),
                seed_origin="target_places",
                confidence=row.get("confidence"),
                place_bundle=row.get("place_bundle"),
                bbox_hint=row.get("bbox_hint"),
            )

        for row in priority_places:
            place_name = str(row.get("name") or "").strip()
            kind = str(row.get("kind") or "").strip()
            kind_norm = normalize_geographic_text(kind)
            confidence = row.get("confidence")
            if "corridor_phrase" in kind_norm:
                register_route_hint(place_name, seed_origin="priority_places_corridor", confidence=confidence)
                continue
            register_place(
                place_name,
                group=row.get("group"),
                priority=row.get("priority") or confidence,
                place_kind=kind,
                seed_origin="priority_places",
                confidence=confidence,
                bbox_hint=row.get("bbox_hint"),
            )

        for place_name in _coerce_catalog_text_list(document.get("place_seeds_primary")):
            register_place(place_name, priority="high", seed_origin="place_seeds_primary", confidence="high")
        for place_name in _coerce_catalog_text_list(document.get("place_seeds_secondary")):
            register_place(place_name, priority="medium", seed_origin="place_seeds_secondary", confidence="medium")
        for place_name in _coerce_catalog_text_list(document.get("parishes_official")):
            register_place(place_name, priority="high", seed_origin="parishes_official", confidence="high")
        for place_name in _coerce_catalog_text_list(document.get("critical_nodes")):
            register_place(place_name, priority="high", seed_origin="critical_nodes", confidence="high")

        for row in list(document.get("cooperative_or_operator_hints") or []):
            if not isinstance(row, dict):
                continue
            register_operator(
                row.get("name"),
                seed_origin="cooperative_or_operator_hints",
                confidence=row.get("confidence"),
                aliases=row.get("aliases"),
            )
        for row in list(document.get("official_operators") or []):
            if not isinstance(row, dict):
                continue
            register_operator(
                row.get("name"),
                seed_origin="official_operators",
                confidence=row.get("confidence") or "high",
                aliases=row.get("aliases"),
            )
        for row in list(document.get("exploratory_local_operators") or []):
            if not isinstance(row, dict):
                continue
            register_operator(
                row.get("name"),
                seed_origin="exploratory_local_operators",
                confidence=row.get("confidence") or "medium",
                aliases=row.get("aliases"),
            )

        for hint in _coerce_catalog_text_list(document.get("route_hint_strings")):
            register_route_hint(hint, seed_origin="route_hint_strings")
        for hint in _coerce_catalog_text_list(document.get("route_hints_priority")):
            register_route_hint(hint, seed_origin="route_hints_priority", confidence="high")
        for hint in _coerce_catalog_text_list(document.get("intracantonal_ruminahui_hints")):
            register_route_hint(hint, seed_origin="intracantonal_ruminahui_hints", confidence="medium")
        for row in list(document.get("transport_corridors") or []):
            if not isinstance(row, dict):
                continue
            register_route_hint(
                row.get("name"),
                seed_origin="transport_corridors",
                confidence=row.get("confidence"),
            )
        for row in list(document.get("relation_search_hints") or []):
            if not isinstance(row, dict):
                continue
            key_norm = normalize_geographic_text(row.get("key"))
            value = str(row.get("value") or "").strip()
            if not value:
                continue
            if key_norm in {"ref", "name"}:
                register_route_hint(
                    value,
                    seed_origin="relation_search_hints",
                    confidence=row.get("confidence") or "high",
                )
            elif key_norm == "operator":
                register_operator(
                    value,
                    seed_origin="relation_search_hints",
                    confidence=row.get("confidence") or "medium",
                )

        attempts: List[Dict[str, Any]] = []
        attempt_seen: set[str] = set()

        def add_attempt(
            *,
            place: Any,
            attempt_type: str,
            group: Optional[str] = None,
            priority: Optional[str] = None,
            route_hint: Optional[str] = None,
            cooperative_hint: Optional[str] = None,
            place_bundle: Optional[str] = None,
            seed_origin: Optional[str] = None,
            catalog_confidence: Optional[str] = None,
            bbox_hint: Optional[Dict[str, Any]] = None,
        ) -> None:
            place_txt = str(place or "").strip()
            place_norm = normalize_geographic_text(place_txt)
            if not place_txt or not place_norm:
                return
            route_txt = str(route_hint or "").strip() or None
            cooperative_txt = str(cooperative_hint or "").strip() or None
            key = json.dumps(
                {
                    "place": place_norm,
                    "route_hint": normalize_geographic_text(route_txt),
                    "cooperative_hint": normalize_geographic_text(cooperative_txt),
                },
                sort_keys=True,
                ensure_ascii=True,
            )
            if key in attempt_seen:
                return
            attempt_seen.add(key)
            base = dict(place_index.get(place_norm) or {})
            attempts.append(
                {
                    "attempt_type": str(attempt_type).strip() or "catalog_attempt",
                    "place": place_txt,
                    "group": (
                        str(group or "").strip()
                        or str(base.get("group") or "").strip()
                        or _phase3_catalog_group_for_place(place_txt, default_region=region)
                    ),
                    "priority": _normalized_priority_label(priority or base.get("priority")),
                    "route_hint": route_txt,
                    "cooperative_hint": cooperative_txt,
                    "place_bundle": (
                        str(place_bundle or "").strip()
                        or str(base.get("place_bundle") or "").strip()
                        or _phase3_catalog_place_bundle(place_txt, place_kind=base.get("place_kind"))
                    ),
                    "seed_origin": (
                        str(seed_origin or "").strip()
                        or str(base.get("seed_origin") or "").strip()
                        or "catalog"
                    ),
                    "catalog_confidence": (
                        str(catalog_confidence or "").strip()
                        or str(base.get("catalog_confidence") or "").strip()
                        or None
                    ),
                    "bbox_hint": _coerce_bbox_dict(bbox_hint or base.get("bbox_hint")),
                }
            )

        def build_combo_attempt(
            row: Dict[str, Any],
            *,
            attempt_type: str,
            seed_origin: str,
            bundle_name: Optional[str] = None,
        ) -> None:
            place_name = str(row.get("place") or "").strip()
            if not place_name:
                return
            register_place(
                place_name,
                group=row.get("group"),
                priority=row.get("priority"),
                seed_origin=seed_origin,
                confidence=row.get("confidence"),
                place_bundle=bundle_name,
            )
            route_hint = str(row.get("route_hint") or "").strip() or None
            operator_hint = (
                str(row.get("cooperative_hint") or "").strip()
                or str(row.get("operator_hint") or "").strip()
                or None
            )
            if operator_hint:
                register_operator(operator_hint, seed_origin=seed_origin, confidence=row.get("confidence"))
            if route_hint:
                register_route_hint(route_hint, seed_origin=seed_origin, confidence=row.get("confidence"))
            base = dict(place_index.get(normalize_geographic_text(place_name)) or {})
            add_attempt(
                place=place_name,
                attempt_type=attempt_type,
                group=row.get("group") or base.get("group"),
                priority=row.get("priority") or base.get("priority"),
                route_hint=route_hint,
                cooperative_hint=operator_hint,
                place_bundle=bundle_name or base.get("place_bundle"),
                seed_origin=seed_origin,
                catalog_confidence=row.get("confidence") or base.get("catalog_confidence"),
                bbox_hint=row.get("bbox_hint") or base.get("bbox_hint"),
            )

        for row in suggested:
            build_combo_attempt(row, attempt_type="suggested_combination", seed_origin="suggested_combinations")
        for row in seed_combos:
            build_combo_attempt(row, attempt_type="catalog_seed_combination", seed_origin="seed_extraction_combos")
        for row in operator_hint_pairs:
            build_combo_attempt(row, attempt_type="catalog_operator_pair", seed_origin="operator_hint_pairs")

        for bundle in place_bundles:
            bundle_name = str(bundle.get("bundle_id") or bundle.get("name") or bundle.get("label") or "").strip() or None
            bundle_priority = bundle.get("priority")
            bundle_confidence = bundle.get("confidence")
            bundle_places = _coerce_catalog_text_list(bundle.get("places") or bundle.get("place_names"))
            bundle_operators = _dedupe_text_rows(bundle.get("operator_hints") or bundle.get("cooperative_hints") or [])
            bundle_routes = _dedupe_text_rows(bundle.get("route_hints") or [])
            for place_name in bundle_places:
                register_place(
                    place_name,
                    group=bundle.get("group"),
                    priority=bundle_priority,
                    seed_origin="place_bundles",
                    confidence=bundle_confidence,
                    place_bundle=bundle_name,
                    bbox_hint=bundle.get("bbox_hint"),
                )
                add_attempt(
                    place=place_name,
                    attempt_type="catalog_place_bundle",
                    group=bundle.get("group"),
                    priority=bundle_priority,
                    place_bundle=bundle_name,
                    seed_origin="place_bundles",
                    catalog_confidence=bundle_confidence,
                )
                for operator_hint in bundle_operators[:2]:
                    add_attempt(
                        place=place_name,
                        attempt_type="catalog_bundle_operator",
                        group=bundle.get("group"),
                        priority=bundle_priority,
                        cooperative_hint=operator_hint,
                        place_bundle=bundle_name,
                        seed_origin="place_bundles",
                        catalog_confidence=bundle_confidence,
                    )
                for route_hint in bundle_routes[:2]:
                    add_attempt(
                        place=place_name,
                        attempt_type="catalog_bundle_route",
                        group=bundle.get("group"),
                        priority=bundle_priority,
                        route_hint=route_hint,
                        place_bundle=bundle_name,
                        seed_origin="place_bundles",
                        catalog_confidence=bundle_confidence,
                    )

        ordered_places = sorted(
            place_index.values(),
            key=lambda row: (
                _priority_sort_key(row.get("priority")),
                str(row.get("group") or ""),
                str(row.get("name") or ""),
            ),
        )
        ordered_operators = sorted(
            operator_index.values(),
            key=lambda row: (
                _confidence_rank(row.get("catalog_confidence")),
                str(row.get("seed_origin") or ""),
                str(row.get("name") or ""),
            ),
        )
        ordered_route_hints = sorted(
            route_hint_rows,
            key=lambda row: (
                _confidence_rank(row.get("catalog_confidence")),
                str(row.get("seed_origin") or ""),
                str(row.get("route_hint") or ""),
            ),
        )

        operator_pairs_by_place: Dict[str, List[str]] = {}
        for row in operator_hint_pairs + seed_combos:
            place_name = str(row.get("place") or "").strip()
            operator_hint = (
                str(row.get("cooperative_hint") or "").strip()
                or str(row.get("operator_hint") or "").strip()
            )
            if not place_name or not operator_hint:
                continue
            key = normalize_geographic_text(place_name)
            operator_pairs_by_place.setdefault(key, [])
            operator_pairs_by_place[key] = _dedupe_text_rows([*operator_pairs_by_place[key], operator_hint])

        route_hints_by_place: Dict[str, List[str]] = {}
        for route_row in ordered_route_hints:
            route_hint = str(route_row.get("route_hint") or "").strip()
            if not route_hint:
                continue
            for place_row in ordered_places:
                place_name = str(place_row.get("name") or "").strip()
                if not _route_hint_matches_place(place_name, route_hint):
                    continue
                key = normalize_geographic_text(place_name)
                route_hints_by_place.setdefault(key, [])
                route_hints_by_place[key] = _dedupe_text_rows([*route_hints_by_place[key], route_hint])

        fallback_route_hints = [str(row.get("route_hint") or "").strip() for row in ordered_route_hints]

        for row in ordered_places:
            place_name = str(row.get("name") or "").strip()
            if not place_name:
                continue
            place_key = normalize_geographic_text(place_name)
            priority_txt = _normalized_priority_label(row.get("priority"))

            add_attempt(
                place=place_name,
                attempt_type="place_only",
                group=row.get("group"),
                priority=priority_txt,
                place_bundle=row.get("place_bundle"),
                seed_origin=row.get("seed_origin"),
                catalog_confidence=row.get("catalog_confidence"),
                bbox_hint=row.get("bbox_hint"),
            )

            operator_choices = _dedupe_text_rows(operator_pairs_by_place.get(place_key, []))
            operator_choices = operator_choices[: (2 if priority_txt == "high" else 1)]
            for operator_hint in operator_choices:
                add_attempt(
                    place=place_name,
                    attempt_type="catalog_operator_bundle",
                    group=row.get("group"),
                    priority=priority_txt,
                    cooperative_hint=operator_hint,
                    place_bundle=row.get("place_bundle"),
                    seed_origin="catalog_operator_bundle",
                    catalog_confidence=row.get("catalog_confidence"),
                    bbox_hint=row.get("bbox_hint"),
                )

            route_choices = route_hints_by_place.get(place_key, [])
            if not route_choices:
                route_choices = [
                    hint
                    for hint in fallback_route_hints
                    if any(normalize_geographic_text(keyword) in normalize_geographic_text(hint) for keyword in _phase3_catalog_route_keywords(place_name))
                ]
            route_choices = _dedupe_text_rows(route_choices)[: (2 if priority_txt == "high" else 1)]
            for route_hint in route_choices:
                add_attempt(
                    place=place_name,
                    attempt_type="catalog_route_bundle",
                    group=row.get("group"),
                    priority=priority_txt,
                    route_hint=route_hint,
                    place_bundle=row.get("place_bundle"),
                    seed_origin="catalog_route_bundle",
                    catalog_confidence=row.get("catalog_confidence"),
                    bbox_hint=row.get("bbox_hint"),
                )

        attempts.sort(
            key=lambda row: (
                _priority_sort_key(row.get("priority")),
                {
                    "catalog_seed_combination": 0,
                    "suggested_combination": 1,
                    "catalog_operator_pair": 2,
                    "catalog_place_bundle": 3,
                    "catalog_bundle_operator": 4,
                    "catalog_bundle_route": 5,
                    "catalog_operator_bundle": 6,
                    "catalog_route_bundle": 7,
                    "place_only": 8,
                }.get(str(row.get("attempt_type") or ""), 9),
                str(row.get("group") or ""),
                str(row.get("place") or ""),
                str(row.get("route_hint") or ""),
                str(row.get("cooperative_hint") or ""),
            )
        )
        return attempts

    @staticmethod
    def _write_phase3_harvest_output(payload: Dict[str, Any]) -> Optional[str]:
        batch_id = str(payload.get("batch_id") or "").strip()
        if not batch_id:
            return None
        out_dir = _phase3_harvest_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{batch_id}.json"
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return str(out_path)

    def run_phase3_extractor_harvest(
        self,
        *,
        targets_path: str,
        minimum_goal: int = 50,
        max_candidates: int = 12,
        timeout_s: int = 120,
        fetch_selected_relation: bool = True,
        allow_ai_assist: bool = True,
        advisory_mode: Optional[str] = None,
        max_attempts: Optional[int] = None,
    ) -> Dict[str, Any]:
        target_doc = json.loads(Path(targets_path).expanduser().read_text(encoding="utf-8"))
        attempts = self._build_phase3_target_attempts(target_doc)
        harvesting_policy = _as_dict(target_doc.get("harvesting_policy"))
        doc_goal = _to_int(
            _as_dict(_as_dict(target_doc.get("workflow_scope")).get("minimum_collection_goal")).get("routes")
        )
        policy_goal = _to_int(harvesting_policy.get("minimum_reviewable_outputs"))
        effective_minimum_goal = max(1, int(max(minimum_goal, doc_goal or 0, policy_goal or 0)))
        expansion_steps: List[float] = []
        for raw in list(
            harvesting_policy.get("expand_bbox_pcts")
            or harvesting_policy.get("bbox_expand_pcts")
            or []
        ):
            try:
                expansion_steps.append(max(0.0, float(raw)))
            except Exception:
                continue
        if not expansion_steps:
            expansion_steps = [0.0, 0.18, 0.35]
        else:
            expansion_steps = sorted(set(expansion_steps))
        harvest_query_strategy = (
            str(harvesting_policy.get("query_strategy") or "bbox_first_broad").strip()
            or "bbox_first_broad"
        )
        if max_attempts is not None:
            attempts = attempts[: max(1, int(max_attempts))]

        advisory_setting = (
            str(
                advisory_mode
                or os.getenv("DATAMIND_CHATGPT_MODE")
                or os.getenv("DATAMIND_ADVISORY_MODE")
                or ""
            ).strip()
            or None
        )
        resolver = SharedGeographyResolver(advisory_mode=advisory_setting)
        batch_id = f"phase3_extract_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        source_name = Path(targets_path).name
        source_document = str(Path(targets_path).expanduser())
        preexisting_dedupe = self.dedupe_extractor_review_jobs(extractor_source=source_name)
        preexisting_reviews = self.list_extractor_review_jobs(limit=10000, extractor_source=source_name)
        preexisting_reviewable_count = _extractor_review_context_count(preexisting_reviews)
        remaining_goal = max(0, int(effective_minimum_goal) - int(preexisting_reviewable_count))

        successes: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        for index, attempt in enumerate(attempts, start=1):
            if remaining_goal <= 0 or len(successes) >= int(remaining_goal):
                break

            place = str(attempt.get("place") or "").strip()
            group = str(attempt.get("group") or "").strip() or None
            priority = str(attempt.get("priority") or "").strip() or None
            route_hint_raw = str(attempt.get("route_hint") or "").strip() or None
            cooperative_hint = str(attempt.get("cooperative_hint") or "").strip() or None
            place_bundle = str(attempt.get("place_bundle") or "").strip() or None
            seed_origin = str(attempt.get("seed_origin") or "").strip() or None
            catalog_confidence = str(attempt.get("catalog_confidence") or "").strip() or None
            catalog_bbox_hint = _coerce_bbox_dict(attempt.get("bbox_hint"))
            route_hint_contract = derive_phase3_route_hint_contract(route_hint_raw)
            route_name = str(route_hint_contract.get("name") or "").strip() or None
            operator_hint = cooperative_hint or (
                str(route_hint_contract.get("operator_signal") or "").strip() or None
            )
            route_tokens = _dedupe_text_rows(
                [
                    *(normalize_geographic_text(route_hint_raw).split(" ") if route_hint_raw else []),
                    *(normalize_geographic_text(route_name).split(" ") if route_name else []),
                    *(str(ref or "").strip() for ref in list(route_hint_contract.get("refs") or [])),
                ]
            )

            geography = dict(
                resolver.resolve(
                    phase="phase3",
                    place_input=place,
                    supporting_hints={
                        "phase3_target_group": group,
                        "phase3_operator_hint": operator_hint,
                        "phase3_route_hint": route_hint_raw,
                        "phase3_refs_hint": list(route_hint_contract.get("refs") or []),
                        "route_tokens": route_tokens,
                    },
                    allow_ai_assist=allow_ai_assist,
                )
                or {}
            )
            resolved_bbox = _as_dict(geography.get("bbox_candidate"))
            if not resolved_bbox and catalog_bbox_hint:
                resolved_bbox = dict(catalog_bbox_hint)
                geography = _merge_nested_dicts(
                    geography,
                    {
                        "interpreted_place_meaning": place,
                        "interpretation_source": "phase3_catalog_bbox_hint",
                        "interpretation_status": "ok",
                        "interpretation_confidence": 0.58,
                        "bbox_candidate_confidence": 0.58,
                        "bbox_candidate": dict(catalog_bbox_hint),
                        "bbox_validation_status": "valid",
                        "fallback_used": True,
                        "fallback_reason": "phase3_catalog_bbox_hint",
                        "effective_bbox_fingerprint": bbox_hash(catalog_bbox_hint),
                        "route_hints_used_as_secondary_signal": bool(route_hint_raw or cooperative_hint),
                    },
                )
            if not resolved_bbox:
                failures.append(
                    {
                        "place": place,
                        "group": group,
                        "priority": priority,
                        "place_bundle": place_bundle,
                        "seed_origin": seed_origin,
                        "route_hint": route_hint_raw,
                        "cooperative_hint": cooperative_hint,
                        "reason": "bbox_unresolved",
                        "interpretation_source": geography.get("interpretation_source"),
                        "interpretation_status": geography.get("interpretation_status"),
                    }
                )
                continue

            route_id: Optional[uuid.UUID] = None
            discover_out: Optional[Dict[str, Any]] = None
            fetch_out: Optional[Dict[str, Any]] = None
            last_error: Optional[str] = None
            bbox_used: Dict[str, float] = {}

            for expand_pct in expansion_steps:
                bbox_used = self.build_phase3_extract_bbox(
                    bbox=resolved_bbox,
                    group_hint=group,
                    priority=priority,
                    extra_expand_pct=expand_pct,
                )
                if not bbox_used:
                    continue

                if route_id is None:
                    route_id = self.create_route_job(
                        area_key=(normalize_geographic_text(place) or None),
                        bbox=bbox_used,
                        known_ref=((route_hint_contract.get("refs") or [None])[0] if route_hint_contract.get("refs") else None),
                        created_by=os.getenv("USER") or os.getenv("USERNAME") or "console",
                        notes=(
                            f"phase3 extractor harvest | source={source_name} | batch_id={batch_id} "
                            f"| attempt_type={attempt.get('attempt_type')} | place_bundle={place_bundle or ''} "
                            f"| seed_origin={seed_origin or ''}"
                        ),
                    )

                try:
                    discover_out = self.run_step_05_discover(
                        route_id=route_id,
                        bbox=(
                            float(bbox_used["south"]),
                            float(bbox_used["west"]),
                            float(bbox_used["north"]),
                            float(bbox_used["east"]),
                        ),
                        refs=list(route_hint_contract.get("refs") or []) or None,
                        operator=operator_hint,
                        name=route_name,
                        max_candidates=max_candidates,
                        timeout_s=timeout_s,
                        query_strategy=harvest_query_strategy,
                        store=True,
                        place_input=place,
                        geography_resolution=geography,
                        route_hint_raw=route_hint_raw,
                        route_hint_contract=route_hint_contract,
                        cooperative_hint=cooperative_hint,
                        source_document=source_document,
                        target_group=group,
                        target_priority=priority,
                        target_attempt_type=(
                            f"{attempt.get('attempt_type')}@expand_{int(round(expand_pct * 100.0))}"
                        ),
                        target_place_bundle=place_bundle,
                        target_seed_origin=seed_origin,
                        target_catalog_confidence=catalog_confidence,
                        batch_id=batch_id,
                    )
                    last_error = None
                    diag = _as_dict(discover_out.get("extractor_diagnostics"))
                    if int(diag.get("candidate_count") or 0) > 0:
                        break
                except Exception as exc:
                    last_error = str(exc)
                    if route_id is not None:
                        self._persist_route_job_extractor_review(
                            route_id=route_id,
                            review_patch={
                                "schema_version": "phase3_extractor_review_v1",
                                "source_document": source_document,
                                "batch_id": batch_id,
                                "target": {
                                    "place": place,
                                    "group": group,
                                    "priority": priority,
                                    "attempt_type": attempt.get("attempt_type"),
                                    "place_bundle": place_bundle,
                                    "seed_origin": seed_origin,
                                    "catalog_confidence": catalog_confidence,
                                },
                                "geography": {
                                    "place_input": place,
                                    "bbox_used": dict(bbox_used or {}),
                                    "interpretation_source": geography.get("interpretation_source"),
                                    "interpretation_status": geography.get("interpretation_status"),
                                },
                                "hints": {
                                    "route_hint_raw": route_hint_raw,
                                    "route_hint_contract": dict(route_hint_contract or {}),
                                    "cooperative_hint": cooperative_hint,
                                },
                                "discover": {
                                    "relation_extraction_success": False,
                                    "last_error": last_error,
                                },
                                "downstream": {
                                    "step20_required": False,
                                    "matching_success_required": False,
                                },
                            },
                            extractor_source=source_name,
                        )
                    continue

            if not discover_out:
                failures.append(
                    {
                        "place": place,
                        "group": group,
                        "priority": priority,
                        "place_bundle": place_bundle,
                        "seed_origin": seed_origin,
                        "route_hint": route_hint_raw,
                        "cooperative_hint": cooperative_hint,
                        "reason": "discover_failed",
                        "error": last_error,
                    }
                )
                continue

            effective_route_id = str(discover_out.get("route_id") or route_id or "").strip() or None
            if fetch_selected_relation:
                try:
                    fetch_out = self.run_step_10_fetch(
                        route_id=effective_route_id or route_id or discover_out.get("route_id"),
                        osm_relation_id=int(discover_out.get("chosen_osm_relation_id")),
                    )
                except Exception as exc:
                    fetch_out = {
                        "stored": False,
                        "fetch_relation_stored": False,
                        "fetch_status": "error",
                        "error": str(exc),
                    }
                    if effective_route_id is not None or route_id is not None:
                        self._persist_route_job_extractor_review(
                            route_id=effective_route_id or route_id,
                            review_patch={
                                "fetch": {
                                    "route_id": str(effective_route_id or route_id),
                                    "osm_relation_id": discover_out.get("chosen_osm_relation_id"),
                                    "stored": False,
                                    "fetch_relation_stored": False,
                                    "fetch_status": "error",
                                    "error": str(exc),
                                }
                            },
                        )

            selection = _as_dict(discover_out.get("selection_summary"))
            universe = _as_dict(discover_out.get("candidate_universe_summary"))
            diagnostics = _as_dict(discover_out.get("extractor_diagnostics"))
            successes.append(
                {
                    "route_id": str(effective_route_id or route_id or ""),
                    "place": place,
                    "group": group,
                    "priority": priority,
                    "attempt_type": attempt.get("attempt_type"),
                    "place_bundle": place_bundle,
                    "seed_origin": seed_origin,
                    "catalog_confidence": catalog_confidence,
                    "route_hint": route_hint_raw,
                    "cooperative_hint": cooperative_hint,
                    "bbox_used": dict(bbox_used or {}),
                    "interpretation_source": geography.get("interpretation_source"),
                    "chosen_osm_relation_id": discover_out.get("chosen_osm_relation_id"),
                    "candidate_universe_count": universe.get("candidate_universe_count"),
                    "selection_confidence": selection.get("selection_confidence"),
                    "top_stop_prior_count": universe.get("top_stop_prior_count") or diagnostics.get("top_stop_prior_count"),
                    "signal_strength": diagnostics.get("signal_strength"),
                    "novelty_status": discover_out.get("novelty_status"),
                    "reused_existing_route_id": discover_out.get("reused_existing_route_id"),
                    "existing_relation_usage_count": discover_out.get("existing_relation_usage_count"),
                    "relation_extraction_success": bool(discover_out.get("chosen_osm_relation_id")),
                    "fetch_relation_stored": bool(_as_dict(fetch_out).get("fetch_relation_stored")),
                    "fetch_status": _as_dict(fetch_out).get("fetch_status"),
                }
            )

        post_dedupe = self.dedupe_extractor_review_jobs(extractor_source=source_name)
        post_reviews = self.list_extractor_review_jobs(limit=10000, extractor_source=source_name)
        reviewable_count_after_run = _extractor_review_context_count(post_reviews)
        unique_relation_ids = {
            int(row.get("chosen_osm_relation_id"))
            for row in successes
            if _to_int(row.get("chosen_osm_relation_id")) is not None
        }
        payload = {
            "batch_id": batch_id,
            "targets_path": source_document,
            "catalog_id": target_doc.get("catalog_id") or target_doc.get("catalog_name") or target_doc.get("document_name"),
            "query_strategy": harvest_query_strategy,
            "expand_bbox_pcts": list(expansion_steps),
            "minimum_goal": int(effective_minimum_goal),
            "preexisting_reviewable_count": int(preexisting_reviewable_count),
            "remaining_goal_at_start": int(remaining_goal),
            "reviewable_count_after_run": int(reviewable_count_after_run),
            "attempt_catalog_count": int(len(attempts)),
            "attempted_count": int(len(successes) + len(failures)),
            "extracted_context_count": int(len(successes)),
            "fetched_relation_count": int(sum(1 for row in successes if bool(row.get("fetch_relation_stored")))),
            "new_relation_count": int(
                sum(
                    1
                    for row in successes
                    if str(row.get("novelty_status") or "").strip()
                    in {"novel_relation_selected", "novel_alternative_selected"}
                )
            ),
            "novel_alternative_count": int(
                sum(1 for row in successes if str(row.get("novelty_status") or "").strip() == "novel_alternative_selected")
            ),
            "reused_existing_relation_count": int(
                sum(1 for row in successes if str(row.get("novelty_status") or "").strip() == "duplicate_reused_existing_route")
            ),
            "unique_relation_count": int(len(unique_relation_ids)),
            "canonicalized_duplicate_route_count": int(post_dedupe.get("canonicalized_route_count") or post_dedupe.get("merged_route_count") or 0),
            "compacted_duplicate_route_count": int(post_dedupe.get("canonicalized_route_count") or post_dedupe.get("merged_route_count") or 0),
            "goal_met": bool(int(reviewable_count_after_run) >= int(effective_minimum_goal)),
            "goal_already_met_before_run": bool(int(preexisting_reviewable_count) >= int(effective_minimum_goal)),
            "preexisting_dedupe": preexisting_dedupe,
            "post_dedupe": post_dedupe,
            "best_place_targets": _phase3_harvest_dimension_summary(successes, key="place", label="place"),
            "best_cooperative_hints": _phase3_harvest_dimension_summary(successes, key="cooperative_hint", label="cooperative_hint"),
            "best_route_hints": _phase3_harvest_dimension_summary(successes, key="route_hint", label="route_hint"),
            "review_surface": "Phase 3 Step 05 -> Stored extractor review",
            "results": successes,
            "failures": failures,
        }
        artifact_path = self._write_phase3_harvest_output(payload)
        if artifact_path:
            payload["artifact_path"] = artifact_path
            self._write_phase3_harvest_output(payload)
        return payload



    def run_step_20_sequences(
        self,
        *,
        route_id: uuid.UUID,
        match_radius_m: Optional[float] = None,
    ) -> Dict[str, Any]:
        # Canonical Phase 3 order requires the persisted inverse-completion sidecar
        # to mark the logical-route slot direction-ready before any Step 20 work starts.
        direction_gate = self.ensure_direction_ready_for_step20(route_id=str(route_id))
        if not bool(direction_gate.get("gate_passed")):
            return {
                **direction_gate,
                "route_id": str(route_id),
                "match_radius_m": (max(0.5, float(match_radius_m)) if match_radius_m is not None else None),
                "step20_blocked": True,
            }

        # Single source of truth: always build from current relation_stop_prior rows
        # (including manual unmatched/ambiguous resolve decisions).
        prior_rows = self.get_relation_stop_prior(route_id)
        if not prior_rows:
            raise RuntimeError(
                "Step 20 requires route_work.relation_stop_prior rows for this route. "
                "Load/build stop prior first (Step 10/Discover or manual prior sync)."
            )

        r_used = max(0.5, float(match_radius_m)) if match_radius_m is not None else None
        if r_used is not None:
            try:
                # Keep match fields aligned with current UI radius before building.
                self.get_prior_match_report(
                    route_id,
                    radius_m=r_used,
                    prior_rows=prior_rows,
                    write=True,
                    invalidate_resolution=False,
                )
                prior_rows = self.get_relation_stop_prior(route_id)
            except Exception:
                pass

        match_report: Dict[str, Any] = {}
        try:
            match_report = self.get_prior_match_report(
                route_id,
                radius_m=float(r_used or 3.0),
                prior_rows=prior_rows,
                write=False,
            ) or {}
        except Exception:
            match_report = {}

        chosen_relation_id: Optional[int] = None
        try:
            job = self.get_route_job(uuid.UUID(str(route_id)))
            if job and job.get("osm_relation_id") is not None:
                chosen_relation_id = int(job.get("osm_relation_id"))
        except Exception:
            chosen_relation_id = None
        candidate_rows: List[Dict[str, Any]] = []
        try:
            candidate_rows = list(self.list_relation_candidates(route_id=uuid.UUID(str(route_id))) or [])
        except Exception:
            candidate_rows = []
        extractor_diag = _summarize_discover_candidates(
            candidate_rows,
            chosen_relation_id=chosen_relation_id,
            top_candidate=None,
        )

        self.invalidate_route_resolution(
            route_id,
            reason="step20_sequence_rebuild",
            clear_sequence_candidates=True,
            clear_geometry_candidates=True,
            clear_route_approvals=True,
            clear_route_prod=True,
            clear_sequence_approval=True,
            demote_direction=True,
        )
        set_id = self.build_sequence_candidates_strict_relaxed(route_id=route_id, prior_rows=prior_rows)
        candidates_payload = self.get_sequence_candidates(route_id, set_id=set_id)
        resolution = self.get_sequence_resolution_state(route_id)
        candidate_shortlist = list(resolution.get("candidate_shortlist") or [])
        recommended_candidate_id = str(resolution.get("recommended_stop_sequence_candidate_id") or "").strip() or None
        approved_candidate_id = str(resolution.get("approved_stop_sequence_candidate_id") or "").strip() or None
        blocker_origin = _classify_step20_blocker_origin(
            prior_stop_count=int(match_report.get("total") or len(prior_rows or [])),
            unmatched_count=int(match_report.get("unmatched") or 0),
            ambiguous_count=int(match_report.get("ambiguous") or 0),
            extractor_diagnostics=extractor_diag,
        )
        seq_diag: Dict[str, Any] = {}
        if callable(_evaluate_sequence_quality):
            try:
                seq_diag = dict(
                    _evaluate_sequence_quality(
                        prior_rows=list(prior_rows or []),
                        matched_count=int(match_report.get("matched") or 0),
                        unmatched_count=int(match_report.get("unmatched") or 0),
                        ambiguous_count=int(match_report.get("ambiguous") or 0),
                    )
                    or {}
                )
            except Exception:
                seq_diag = {}

        seq_dominant = str(seq_diag.get("dominant_cause") or "").strip()
        dominant_cause = str(blocker_origin.get("dominant_cause") or "").strip()
        dominant_conf = str(blocker_origin.get("confidence") or "").strip()
        dominant_reason = str(blocker_origin.get("reason") or "").strip()
        triage_route = "phase1_new_nodes_resolution"
        if int(match_report.get("ambiguous") or 0) <= 0 and int(match_report.get("unmatched") or 0) <= 0 and seq_dominant:
            dominant_cause = seq_dominant
            dominant_conf = str(seq_diag.get("dominant_cause_confidence") or "low")
            dominant_reason = str(
                (seq_diag.get("details") or {}).get("dominant_cause_reason")
                or "Derived from sequence diagnostics.",
            )
            triage_route = str(seq_diag.get("triage_route") or "operator_triage")

        step20_diag_payload = {
            "dominant_cause": dominant_cause or "unknown",
            "dominant_cause_confidence": dominant_conf or "low",
            "dominant_cause_reason": dominant_reason or "No clear dominant cause.",
            "triage_route": triage_route,
            "threshold_profile": dict(seq_diag.get("threshold_profile") or {}),
            "warning_tags": list(seq_diag.get("warning_tags") or []),
            "warning_subtypes": dict(seq_diag.get("warning_subtypes") or {}),
            "reorder_recommended": bool(seq_diag.get("reorder_recommended")) if seq_diag else None,
            "reorder_confidence": _to_float(seq_diag.get("reorder_confidence")),
            "candidate_shortlist": candidate_shortlist,
            "recommended_stop_sequence_candidate_id": recommended_candidate_id,
            "variant_state": resolution.get("variant_state"),
            "variant_groups": list(resolution.get("variant_groups") or []),
            "recommended_variant_group_key": resolution.get("recommended_variant_group_key"),
            "recommended_valhalla_summary": (
                list(dict(candidate_shortlist[0] or {}).get("valhalla_evidence_summary") or [])
                if candidate_shortlist
                else []
            ),
        }
        proposal_requires_approval = bool(recommended_candidate_id and not resolution.get("sequence_stabilized"))
        proposal_blocking_reason = "operator_canonical_sequence_selection_required"
        if not candidate_shortlist:
            proposal_blocking_reason = "sequence_candidate_shortlist_empty"
        elif str(resolution.get("variant_state") or "") == "unresolved_multi_variant":
            proposal_blocking_reason = "unresolved_multi_variant_operator_review_required"
        elif str(resolution.get("variant_state") or "") == "mild_variant_pressure_dominant_group":
            proposal_blocking_reason = "mild_variant_pressure_operator_review_required"
        elif bool(seq_diag.get("reorder_recommended")):
            proposal_blocking_reason = "sequence_risk_requires_operator_confirmation"
        recommended_summary = candidate_shortlist[0] if candidate_shortlist else {}
        recommended_family = str(recommended_summary.get("family") or "").strip() or "sequence_candidate"
        recommended_score = _to_float(recommended_summary.get("sequence_score"))
        recommended_traversability = _to_float(recommended_summary.get("traversability_score"))
        recommended_success_rate = _to_float(recommended_summary.get("segment_success_rate"))
        recommendation_parts = [f"Top ranked candidate comes from `{recommended_family}`."]
        if recommended_score is not None:
            recommendation_parts.append(f"Composite score `{recommended_score:.2f}`.")
        if recommended_traversability is not None:
            recommendation_parts.append(f"Valhalla traversability `{recommended_traversability:.2f}`.")
        if recommended_success_rate is not None:
            recommendation_parts.append(f"Segment success rate `{recommended_success_rate:.2f}`.")
        if recommended_summary.get("risk_indicators"):
            recommendation_parts.append(
                "Risk indicators: " + ", ".join(str(x) for x in (recommended_summary.get("risk_indicators") or [])[:4]) + "."
            )
        if resolution.get("recommended_variant_group_key"):
            recommendation_parts.append(
                f"Variant group `{resolution.get('recommended_variant_group_key')}` is currently preferred."
            )
        if resolution.get("variant_evidence_summary"):
            recommendation_parts.append(
                "Variant evidence: " + " ".join(str(x) for x in list(resolution.get("variant_evidence_summary") or [])[:2])
            )
        reorder_proposal = {
            "requires_approval": bool(proposal_requires_approval),
            "operator_approval_required": bool(proposal_requires_approval),
            "needs_reorder": bool(
                resolution.get("reorder_needed")
                or seq_diag.get("reorder_recommended")
                or resolution.get("variant_pressure_detected")
                or proposal_requires_approval
            ),
            "apply_recommended": bool(recommended_candidate_id),
            "blocking": bool(proposal_requires_approval),
            "blocking_reason": proposal_blocking_reason,
            "risk_level": ("blocking" if proposal_requires_approval else "warning"),
            "recommended_candidate_id": recommended_candidate_id,
            "approved_candidate_id": approved_candidate_id,
            "candidate_shortlist": candidate_shortlist,
            "variant_groups": list(resolution.get("variant_groups") or []),
            "variant_state": resolution.get("variant_state"),
            "recommended_variant_group_key": resolution.get("recommended_variant_group_key"),
            "approved_variant_group_key": resolution.get("approved_variant_group_key"),
            "recommended_candidates_by_variant": list(resolution.get("recommended_candidates_by_variant") or []),
            "variant_evidence_summary": list(resolution.get("variant_evidence_summary") or []),
            "recommendation_reason": " ".join(recommendation_parts),
            "variant_pressure_detected": bool(resolution.get("variant_pressure_detected")),
            "variant_pressure_reasons": list(resolution.get("variant_pressure_reasons") or []),
            "direction_stable": bool(resolution.get("direction_stable")),
            "direction_reasons": list(resolution.get("direction_reasons") or []),
            "sequence_stabilized": bool(resolution.get("sequence_stabilized")),
        }
        out = {
            "route_id": str(route_id),
            "stop_sequence_set_id": str(set_id),
            "unmatched_stops": int(sum(1 for r in (prior_rows or []) if not r.get("matched_stop_node_id"))),
            "matched_count": int(match_report.get("matched") or 0),
            "unmatched_count": int(match_report.get("unmatched") or 0),
            "ambiguous_count": int(match_report.get("ambiguous") or 0),
            "prior_stop_count": int(match_report.get("total") or len(prior_rows or [])),
            "sequence_gate_pass": bool(match_report.get("all_matched")) if match_report else None,
            "sequence_quality_score": _to_float(seq_diag.get("sequence_quality_score")),
            "sequence_warnings": list(seq_diag.get("warnings") or []),
            "sequence_warning_tags": list(seq_diag.get("warning_tags") or []),
            "sequence_warning_subtypes": dict(seq_diag.get("warning_subtypes") or {}),
            "reorder_recommended": (
                bool(seq_diag.get("reorder_recommended"))
                if seq_diag.get("reorder_recommended") is not None
                else None
            ),
            "reorder_confidence": _to_float(seq_diag.get("reorder_confidence")),
            "phase3_requests_created": None,
            "match_radius_m": r_used,
            "raw_stdout": "in-process Step 20 build from relation_stop_prior",
            "extractor_diagnostics": extractor_diag,
            "candidate_universe_count": int(len(candidate_rows or [])),
            "selection_summary": _build_phase3_selection_summary(
                candidate_rows,
                chosen_relation_id=chosen_relation_id,
                parsed_summary=None,
            ),
            "blocker_origin_hint": dominant_cause or "unknown",
            "blocker_origin_confidence": dominant_conf or "low",
            "blocker_origin_reason": dominant_reason,
            "sequence_diagnostic_profile_version": str(
                (seq_diag.get("threshold_profile") or {}).get("version") or ""
            )
            or None,
            "step20_diagnostics_payload": step20_diag_payload,
            "candidate_count": int(len(candidates_payload.get("candidates") or [])),
            "candidate_shortlist": candidate_shortlist,
            "recommended_stop_sequence_candidate_id": recommended_candidate_id,
            "approved_stop_sequence_candidate_id": approved_candidate_id,
            "sequence_stabilized": bool(resolution.get("sequence_stabilized")),
            "sequence_approval_status": resolution.get("approval_status"),
            "variant_state": resolution.get("variant_state"),
            "variant_pressure_detected": bool(resolution.get("variant_pressure_detected")),
            "variant_pressure_reasons": list(resolution.get("variant_pressure_reasons") or []),
            "variant_groups": list(resolution.get("variant_groups") or []),
            "recommended_variant_group_key": resolution.get("recommended_variant_group_key"),
            "approved_variant_group_key": resolution.get("approved_variant_group_key"),
            "recommended_candidates_by_variant": list(resolution.get("recommended_candidates_by_variant") or []),
            "variant_evidence_summary": list(resolution.get("variant_evidence_summary") or []),
            "direction_stable": bool(resolution.get("direction_stable")),
            "direction_reasons": list(resolution.get("direction_reasons") or []),
            "reorder_proposal": reorder_proposal,
        }
        try:
            self.mark_direction_progress_by_route(
                route_id=str(route_id),
                step=2,
                progress_notes="Step 20 sequence build completed (relation_stop_prior source).",
            )
        except Exception:
            pass
        self._safe_ai_log_phase3(
            stage="step_20_sequences",
            route_id=str(route_id),
            payload={
                **out,
                "progressed_to_next_step": bool(out.get("stop_sequence_set_id")),
            },
            prior_rows=prior_rows,
        )
        return out
    
    def run_step_30_geometry(
        self,
        *,
        route_id: uuid.UUID,
        stop_sequence_candidate_id: uuid.UUID,
    ) -> Dict[str, Any]:
        gate = self.get_step30_gate(
            route_id,
            stop_sequence_candidate_id=stop_sequence_candidate_id,
            match_radius_m=3.0,
        )
        if not bool(gate.get("can_run_step30")):
            raise RuntimeError(
                "Step 30 blocked: "
                + ", ".join(str(x) for x in (gate.get("blocking_reasons") or []) if str(x).strip())
            )

        # Precheck so UI gets a clear error before script call.
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM route_work.stop_sequence_candidates ssc
                    JOIN route_work.stop_sequence_candidate_sets scs
                      ON scs.set_id = ssc.set_id
                    WHERE ssc.candidate_id = %s
                      AND scs.route_id = %s
                    LIMIT 1
                    """,
                    (str(stop_sequence_candidate_id), str(route_id)),
                )
                ok_row = cur.fetchone()
        if not ok_row:
            raise RuntimeError(
                "Step 30 precheck failed: stop_sequence_candidate_id does not exist for this route_id. "
                "Load sequence candidates from Step 20 and pick one from that route."
            )

        script_path = _script_path("30_build_geometry_candidates.py")
        args = [sys.executable, script_path, str(route_id), str(stop_sequence_candidate_id)]

        result = _run_script(args)

        if result.returncode != 0:
            raise RuntimeError(
                f"Step 30 failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        stdout = result.stdout or ""
        m = re.search(r"geometry_candidate_set_id:\s*([0-9a-fA-F-]+)", stdout)
        if not m:
            raise RuntimeError(f"Step 30 succeeded but no set id found.\nSTDOUT:\n{stdout}")

        out = {
            "route_id": str(route_id),
            "geometry_set_id": m.group(1),
            "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
            "raw_stdout": stdout.strip(),
            "step30_gate": gate,
        }
        # Guard against silent empty sets: Step 30 must produce at least one candidate.
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)::int AS n
                    FROM route_work.geometry_candidates
                    WHERE set_id = %s
                    """,
                    (str(out["geometry_set_id"]),),
                )
                n_row = cur.fetchone() or {}
        n_candidates = int(n_row.get("n") or 0)
        out["n_candidates"] = n_candidates
        if n_candidates <= 0:
            raise RuntimeError(
                "Step 30 created a geometry set but produced 0 candidates.\n"
                "Valhalla variants likely failed for this stop sequence.\n"
                f"geometry_set_id={out['geometry_set_id']}\n"
                f"STDOUT:\n{stdout}"
            )
        try:
            self.mark_direction_progress_by_route(
                route_id=str(route_id),
                step=3,
                progress_notes="Step 30 geometry build completed.",
            )
        except Exception:
            pass
        self._safe_ai_log_phase3(
            stage="step_30_geometry",
            route_id=str(route_id),
            payload={
                **out,
                "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
                "progressed_to_next_step": bool(n_candidates > 0),
            },
        )
        return out

    def run_step_32_stop_recovery(
        self,
        *,
        route_id: uuid.UUID,
        geometry_set_id: uuid.UUID,
    ) -> Dict[str, Any]:
        self._ensure_geometry_stop_recovery_schema()
        script_path = _script_path("32_geometry_stop_recovery.py")
        args = [sys.executable, script_path, str(route_id), str(geometry_set_id)]

        result = _run_script(args)

        if result.returncode != 0:
            raise RuntimeError(
                f"Step 32 failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        stdout = result.stdout or ""
        m = re.search(r"geometry_stop_recovery_set_id:\s*([0-9a-fA-F-]+)", stdout)
        if not m:
            raise RuntimeError(f"Step 32 succeeded but no set id found.\nSTDOUT:\n{stdout}")

        processed = re.search(r"geometry_candidates_processed:\s*([0-9]+)", stdout)
        recovered_total = re.search(r"recovered_total:\s*([0-9]+)", stdout)
        ambiguous_total = re.search(r"ambiguous_total:\s*([0-9]+)", stdout)
        rejected_total = re.search(r"rejected_total:\s*([0-9]+)", stdout)

        out = {
            "route_id": str(route_id),
            "geometry_set_id": m.group(1),
            "geometry_candidate_count": int(processed.group(1)) if processed else 0,
            "recovered_total": int(recovered_total.group(1)) if recovered_total else 0,
            "ambiguous_total": int(ambiguous_total.group(1)) if ambiguous_total else 0,
            "rejected_total": int(rejected_total.group(1)) if rejected_total else 0,
            "raw_stdout": stdout.strip(),
        }
        try:
            self.mark_direction_progress_by_route(
                route_id=str(route_id),
                step=3,
                progress_notes="Step 32 geometry-based stop recovery completed.",
            )
        except Exception:
            pass
        self._safe_ai_log_phase3(
            stage="step_32_stop_recovery",
            route_id=str(route_id),
            payload={
                **out,
                "geometry_set_id": str(geometry_set_id),
                "progressed_to_next_step": True,
            },
        )
        return out

    def run_step_35_rank(
        self,
        *,
        route_id: uuid.UUID,
        geometry_set_id: uuid.UUID,
        explain: bool = False,
    ) -> Dict[str, Any]:
        script_path = _script_path("35_rank_geometry_candidates.py")
        args = [sys.executable, script_path, str(route_id), str(geometry_set_id)]
        if explain:
            args.append("--explain")

        result = _run_script(args)

        if result.returncode != 0:
            raise RuntimeError(
                f"Step 35 failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        stdout = result.stdout or ""
        m = re.search(r"ML ranking done for geometry_set_id:\s*([0-9a-fA-F-]+)", stdout)
        if not m:
            raise RuntimeError(f"Step 35 succeeded but no set id found.\nSTDOUT:\n{stdout}")

        out = {
            "route_id": str(route_id),
            "geometry_set_id": m.group(1),
            "raw_stdout": stdout.strip(),
        }
        try:
            recovery = self.get_geometry_stop_recovery_set(route_id, geometry_set_id=geometry_set_id)
            recovery_rows = list(recovery.get("candidates") or [])
            out["step32_candidate_count"] = len(recovery_rows)
            out["step32_recovered_total"] = sum(
                int(dict(row or {}).get("summary_metrics", {}).get("recovered_count") or 0)
                for row in recovery_rows
            )
        except Exception:
            pass
        try:
            self.mark_direction_progress_by_route(
                route_id=str(route_id),
                step=3,
                progress_notes="Step 35 ranking completed.",
            )
        except Exception:
            pass
        self._safe_ai_log_phase3(
            stage="step_35_rank",
            route_id=str(route_id),
            payload={
                **out,
                "geometry_set_id": str(geometry_set_id),
                "progressed_to_next_step": True,
                "rank_explain_mode": bool(explain),
            },
        )
        return out

    def run_step_40_approve(
        self,
        *,
        route_id: uuid.UUID,
        geometry_set_id: uuid.UUID,
    ) -> Dict[str, Any]:
        script_path = _script_path("40_approve_geometry.py")
        args = [sys.executable, script_path, str(route_id), str(geometry_set_id)]

        result = _run_script(args)

        if result.returncode != 0:
            raise RuntimeError(
                f"Step 40 failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )

        stdout = result.stdout or ""
        m = re.search(r"approved geometry_candidate_id:\s*([0-9a-fA-F-]+)", stdout)
        if not m:
            raise RuntimeError(f"Step 40 succeeded but no approved id found.\nSTDOUT:\n{stdout}")

        out = {
            "route_id": str(route_id),
            "approved_geometry_candidate_id": m.group(1),
            "raw_stdout": stdout.strip(),
        }
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      chosen_stop_sequence_candidate_id::text AS chosen_stop_sequence_candidate_id,
                      canonical_sequence_ready,
                      service_route_id::text AS service_route_id,
                      direction_id::int AS direction_id
                    FROM route_prod.routes
                    WHERE route_id = %s
                    LIMIT 1
                    """,
                    (str(route_id),),
                )
                prod_row = self._row(cur.fetchone()) or {}
        out.update(
            {
                "chosen_stop_sequence_candidate_id": prod_row.get("chosen_stop_sequence_candidate_id"),
                "canonical_sequence_ready": bool(prod_row.get("canonical_sequence_ready")),
                "service_route_id": prod_row.get("service_route_id"),
                "direction_id": prod_row.get("direction_id"),
            }
        )
        try:
            self.mark_direction_approved_by_route(route_id=str(route_id))
        except Exception:
            pass
        try:
            self._resolve_linked_coverage_gaps_for_route(
                route_id=str(route_id),
                notes="Resolved via Step 40 Phase 3 approval.",
            )
        except Exception:
            pass
        self._safe_ai_log_phase3(
            stage="step_40_approve",
            route_id=str(route_id),
            payload={
                **out,
                "geometry_set_id": str(geometry_set_id),
                "progressed_to_next_step": True,
            },
        )
        return out

    def get_geometry_candidate(self, geometry_candidate_id: uuid.UUID) -> Dict[str, Any]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                    geometry_candidate_id,
                    set_id,
                    stop_sequence_candidate_id,
                    engine,
                    preset_id,
                    score,
                    length_m,
                    avg_stop_dist_m,
                    max_stop_dist_m,
                    params,
                    metrics,
                    created_at,
                    ST_AsText(geom) AS geom_wkt
                    FROM route_work.geometry_candidates
                    WHERE geometry_candidate_id=%s
                    """,
                    (str(geometry_candidate_id),),
                )
                row = cur.fetchone()
        return self._row(row) if row else {}


    def get_stop_sequence_candidate(self, candidate_id: uuid.UUID) -> Dict[str, Any]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                    candidate_id, set_id, rank,
                    stop_node_ids, stop_prior_seqs,
                    metrics, created_at
                    FROM route_work.stop_sequence_candidates
                    WHERE candidate_id=%s
                    """,
                    (str(candidate_id),),
                )
                row = cur.fetchone()
        return self._row(row) if row else {}

    def get_stop_points_for_candidate(
        self,
        *,
        route_id: uuid.UUID,
        stop_sequence_candidate_id: uuid.UUID,
    ) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT stop_node_ids, stop_prior_seqs
                    FROM route_work.stop_sequence_candidates
                    WHERE candidate_id=%s
                    """,
                    (str(stop_sequence_candidate_id),),
                )
                row = cur.fetchone()
            if not row:
                return []

            stop_node_ids = _parse_uuid_array(row.get("stop_node_ids"))
            stop_prior_seqs = _parse_int_array(row.get("stop_prior_seqs"))

            if stop_node_ids:
                coords = self._fetch_stop_points_from_canonical(conn, stop_node_ids)
                # return with index so UI can label 1..N
                return [{"i": i + 1, "lon": lon, "lat": lat, "source": "canonical"} for i, (lon, lat) in enumerate(coords)]

            coords = self._fetch_stop_points_from_prior(conn, route_id, stop_prior_seqs)
            return [{"i": i + 1, "lon": lon, "lat": lat, "source": "prior"} for i, (lon, lat) in enumerate(coords)]




    def create_route_job(
        self,
        *,
        osm_relation_id: Optional[int] = None,
        service_route_id: Optional[str] = None,
        direction_id: Optional[int] = None,
        area_key: Optional[str] = None,
        bbox: Optional[Dict[str, Any]] = None,   # {"south":..,"west":..,"north":..,"east":..}
        known_ref: Optional[str] = None,
        created_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> uuid.UUID:
        rid = uuid.uuid4()
        created_by = created_by or os.getenv("USER") or os.getenv("USERNAME") or "console"

        bbox_json = json.dumps(_jsonable(bbox), ensure_ascii=False) if bbox else None

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_raw.route_jobs
                    (route_id, chosen_osm_relation_id, status, created_by, notes,
                    area_key, bbox, known_ref, service_route_id, direction_id)
                    VALUES
                    (%s, %s, 'new', %s, %s,
                    %s, %s::jsonb, %s, %s::uuid, %s)
                    """,
                    (
                        str(rid),
                        osm_relation_id,
                        created_by,
                        notes,
                        area_key,
                        bbox_json,
                        known_ref,
                        (str(service_route_id) if service_route_id else None),
                        (int(direction_id) if direction_id in (0, 1) else None),
                    ),
                )

            try:
                conn.commit()
            except Exception:
                pass

        if service_route_id and direction_id in (0, 1):
            try:
                self.bind_route_to_direction(
                    service_route_id=str(service_route_id),
                    direction_id=int(direction_id),
                    route_id=str(rid),
                    geom_source="observed",
                )
            except Exception:
                pass

        return rid


    def list_route_jobs(self, *, limit: int = 50, include_trashed: bool = False) -> List[Dict[str, Any]]:
        self._ensure_extractor_review_schema()
        self._ensure_trash_schema()
        table_name = "route_raw.route_jobs" if include_trashed else "route_raw.active_route_jobs"
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                queries = [
                    f"""
                    SELECT
                    route_id,
                    status,
                    created_at,
                    created_by,
                    notes,

                    area_key,
                    bbox,
                    known_ref,
                    service_route_id,
                    direction_id,
                    extractor_source,
                    extractor_review,

                    chosen_osm_relation_id AS osm_relation_id
                    FROM {table_name}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    f"""
                    SELECT
                    route_id,
                    status,
                    created_at,
                    created_by,
                    notes,

                    area_key,
                    bbox,
                    known_ref,
                    service_route_id,
                    direction_id,

                    chosen_osm_relation_id AS osm_relation_id
                    FROM {table_name}
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                ]
                rows = []
                last_err = None
                for sql in queries:
                    try:
                        cur.execute(sql, (limit,))
                        rows = cur.fetchall() or []
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        continue
                if last_err:
                    raise last_err

        return [self._extractor_review_summary(self._row(r)) for r in rows]


    def get_route_job(self, route_id: uuid.UUID, *, include_trashed: bool = True) -> Dict[str, Any]:
        self._ensure_extractor_review_schema()
        self._ensure_trash_schema()
        table_name = "route_raw.route_jobs" if include_trashed else "route_raw.active_route_jobs"
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                queries = [
                    f"""
                    SELECT
                    route_id,
                    status,
                    created_at,
                    created_by,
                    notes,

                    area_key,
                    bbox,
                    known_ref,
                    service_route_id,
                    direction_id,
                    extractor_source,
                    extractor_review,

                    chosen_osm_relation_id AS osm_relation_id
                    FROM {table_name}
                    WHERE route_id=%s
                    """,
                    f"""
                    SELECT
                    route_id,
                    status,
                    created_at,
                    created_by,
                    notes,

                    area_key,
                    bbox,
                    known_ref,
                    service_route_id,
                    direction_id,

                    chosen_osm_relation_id AS osm_relation_id
                    FROM {table_name}
                    WHERE route_id=%s
                    """,
                ]
                row = None
                last_err = None
                for sql in queries:
                    try:
                        cur.execute(sql, (str(route_id),))
                        row = cur.fetchone()
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        continue
                if last_err:
                    raise last_err

        return self._extractor_review_summary(self._row(row)) if row else {}


    # -------------------------------------------------------------------------
    # Stop Prior / Overpass
    # -------------------------------------------------------------------------

    def fetch_relation_overpass_json(self, osm_relation_id: int, timeout_s: Optional[int] = None) -> Dict[str, Any]:
        """
        Fetch relation + members as Overpass JSON.
        """
        timeout_s = int(timeout_s or 60)
        q = (
            f"[out:json][timeout:{timeout_s}];"
            f"relation({int(osm_relation_id)});"
            f"(._;>;);"
            f"out body;"
        )
        r = requests.post(self.overpass_url, data=q.encode("utf-8"), timeout=timeout_s + 10)
        r.raise_for_status()
        return r.json()

    def replace_relation_stop_prior(
        self,
        route_id: uuid.UUID,
        prior_rows: List[Dict[str, Any]],
        *,
        edit_source: str = "replace",
        invalidate_resolution: bool = True,
    ) -> None:
        """
        Replaces route_work.relation_stop_prior for a route.
        Expects each row has: seq, lat, lon.
        Optional keys preserved when present: member_type, osm_ref, osm_node_id, role,
        matched_stop_node_id, match_dist_m.
        """
        cleaned: List[Dict[str, Any]] = []
        for rr in prior_rows:
            seq = int(rr.get("seq"))
            lat = float(rr.get("lat"))
            lon = float(rr.get("lon"))
            member_type = rr.get("member_type")
            osm_ref = rr.get("osm_ref")
            osm_node_id = rr.get("osm_node_id")
            if osm_ref is None and osm_node_id is not None:
                osm_ref = osm_node_id
            if member_type is None and osm_ref is not None:
                member_type = "node"
            cleaned.append(
                {
                    "seq": seq,
                    "lat": lat,
                    "lon": lon,
                    "member_type": member_type,
                    "osm_ref": osm_ref,
                    "osm_node_id": osm_node_id,
                    "role": rr.get("role"),
                    "matched_stop_node_id": rr.get("matched_stop_node_id"),
                    "match_dist_m": rr.get("match_dist_m"),
                }
            )

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    "DELETE FROM route_work.relation_stop_prior WHERE route_id=%s",
                    (str(route_id),),
                )
                for rr in cleaned:
                    cur.execute(
                        """
                        INSERT INTO route_work.relation_stop_prior
                          (route_id, seq, member_type, osm_ref, osm_node_id, role, lat, lon, matched_stop_node_id, match_dist_m)
                        VALUES
                          (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            str(route_id),
                            rr["seq"],
                            rr["member_type"],
                            (int(rr["osm_ref"]) if rr.get("osm_ref") is not None else None),
                            (int(rr["osm_node_id"]) if rr.get("osm_node_id") is not None else None),
                            rr["role"],
                            rr["lat"],
                            rr["lon"],
                            str(rr["matched_stop_node_id"]) if rr["matched_stop_node_id"] else None,
                            rr["match_dist_m"],
                        ),
                    )
            try:
                conn.commit()
            except Exception:
                pass
        if invalidate_resolution:
            try:
                self.invalidate_route_resolution(
                    route_id,
                    reason=f"relation_stop_prior_changed:{str(edit_source or 'replace')}",
                    clear_sequence_candidates=True,
                    clear_geometry_candidates=True,
                    clear_route_approvals=True,
                    clear_route_prod=True,
                    clear_sequence_approval=True,
                    demote_direction=True,
                )
            except Exception:
                pass
        self._safe_ai_log_sequence_edit(
            route_id=str(route_id),
            edit_type=str(edit_source or "replace"),
            row_count=len(cleaned),
        )

    def add_relation_stop_prior_row(
        self,
        route_id: uuid.UUID,
        *,
        lat: float,
        lon: float,
        role: Optional[str] = None,
        seq: Optional[int] = None,
        member_type: Optional[str] = None,
        osm_ref: Optional[int] = None,
        osm_node_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        rows = self.get_relation_stop_prior(route_id)
        ordered = sorted((dict(r) for r in (rows or [])), key=lambda r: int(r.get("seq") or 0))
        insert_pos = int(seq) if seq is not None else (len(ordered) + 1)
        insert_pos = max(1, min(insert_pos, len(ordered) + 1))
        new_row = {
            "seq": insert_pos,
            "lat": float(lat),
            "lon": float(lon),
            "role": role,
            "member_type": member_type,
            "osm_ref": (int(osm_ref) if osm_ref is not None else None),
            "osm_node_id": (int(osm_node_id) if osm_node_id is not None else None),
            "matched_stop_node_id": None,
            "match_dist_m": None,
        }
        ordered.insert(insert_pos - 1, new_row)
        for i, r in enumerate(ordered, start=1):
            r["seq"] = i
            if i != insert_pos:
                # Any sequence shift should be rematched explicitly.
                r["matched_stop_node_id"] = None
                r["match_dist_m"] = None
        self.replace_relation_stop_prior(route_id, ordered, edit_source="add")
        return {"route_id": str(route_id), "added_seq": insert_pos, "n_rows": len(ordered)}

    def update_relation_stop_prior_row(
        self,
        route_id: uuid.UUID,
        *,
        seq: int,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        role: Optional[str] = None,
        member_type: Optional[str] = None,
        osm_ref: Optional[int] = None,
        osm_node_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        rows = self.get_relation_stop_prior(route_id)
        if not rows:
            raise RuntimeError("No relation_stop_prior rows for route.")
        ordered = sorted((dict(r) for r in rows), key=lambda r: int(r.get("seq") or 0))
        target = next((r for r in ordered if int(r.get("seq") or 0) == int(seq)), None)
        if not target:
            raise RuntimeError(f"seq not found: {seq}")
        if lat is not None:
            target["lat"] = float(lat)
        if lon is not None:
            target["lon"] = float(lon)
        if role is not None:
            target["role"] = role
        if member_type is not None:
            target["member_type"] = member_type
        if osm_ref is not None:
            target["osm_ref"] = int(osm_ref)
        if osm_node_id is not None:
            target["osm_node_id"] = int(osm_node_id)
        target["matched_stop_node_id"] = None
        target["match_dist_m"] = None
        self.replace_relation_stop_prior(route_id, ordered, edit_source="update")
        return {"route_id": str(route_id), "updated_seq": int(seq), "n_rows": len(ordered)}

    def delete_relation_stop_prior_row(self, route_id: uuid.UUID, *, seq: int) -> Dict[str, Any]:
        rows = self.get_relation_stop_prior(route_id)
        ordered = sorted((dict(r) for r in (rows or [])), key=lambda r: int(r.get("seq") or 0))
        before = len(ordered)
        ordered = [r for r in ordered if int(r.get("seq") or 0) != int(seq)]
        if len(ordered) == before:
            raise RuntimeError(f"seq not found: {seq}")
        for i, r in enumerate(ordered, start=1):
            r["seq"] = i
            r["matched_stop_node_id"] = None
            r["match_dist_m"] = None
        self.replace_relation_stop_prior(route_id, ordered, edit_source="delete")
        return {"route_id": str(route_id), "deleted_seq": int(seq), "n_rows": len(ordered)}

    def get_relation_stop_prior(self, route_id: uuid.UUID) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT route_id, seq, member_type, osm_ref, osm_node_id, role, lat, lon, matched_stop_node_id, match_dist_m
                    FROM route_work.relation_stop_prior
                    WHERE route_id=%s
                    ORDER BY seq ASC
                    """,
                    (str(route_id),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def _find_stop_candidates_within_radius(
        self,
        conn: Any,
        *,
        lat: float,
        lon: float,
        radius_m: float,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                  m.node_id::text AS node_id,
                  ST_Distance(
                    n.geom::geography,
                    ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography
                  ) AS dist_m,
                  COALESCE(NULLIF(BTRIM(p.canonical_name), ''), n.name) AS name,
                  n.ref AS ref,
                  ('phase2_final:' || COALESCE(m.mapping_source, 'unknown')) AS source,
                  ST_Y(n.geom) AS lat,
                  ST_X(n.geom) AS lon,
                  n.updated_at AS updated_at
                FROM geo_prod.node_place_map m
                JOIN node_prod.nodes n
                  ON n.node_id = m.node_id
                JOIN geo_prod.places p
                  ON p.place_id = m.place_id
                WHERE n.node_type = 'STOP'
                  AND p.status = 'active'
                  AND ST_DWithin(
                    n.geom::geography,
                    ST_SetSRID(ST_MakePoint(%s,%s), 4326)::geography,
                    %s
                  )
                ORDER BY dist_m ASC, m.node_id ASC
                LIMIT %s
                """,
                (lon, lat, lon, lat, float(radius_m), int(limit)),
            )
            rows = cur.fetchall() or []
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "node_id": str(r.get("node_id") or ""),
                    "dist_m": float(r.get("dist_m") or 0.0),
                    "name": r.get("name"),
                    "ref": r.get("ref"),
                    "source": r.get("source"),
                    "lat": (float(r.get("lat")) if r.get("lat") is not None else None),
                    "lon": (float(r.get("lon")) if r.get("lon") is not None else None),
                    "updated_at": (
                        r.get("updated_at").isoformat() if hasattr(r.get("updated_at"), "isoformat") else r.get("updated_at")
                    ),
                }
            )
        return out

    def get_prior_match_report(
        self,
        route_id: uuid.UUID,
        radius_m: float = 3.0,
        prior_rows: Optional[List[Dict[str, Any]]] = None,
        *,
        write: bool = False,
        invalidate_resolution: bool = False,
    ) -> Dict[str, Any]:
        """
        Match prior rows to Phase 2 final prod STOP nodes using UNIQUE-in-radius policy.
        - 0 candidates in radius: unmatched
        - 1 candidate in radius: matched
        - 2+ candidates in radius: ambiguous (no auto assignment)
        If write=True, writes matched_stop_node_id + match_dist_m back to relation_stop_prior.
        """
        radius_m = float(radius_m)
        prior = prior_rows if prior_rows is not None else self.get_relation_stop_prior(route_id)

        updated: List[Dict[str, Any]] = []
        n_total = 0
        n_matched = 0
        n_unmatched = 0
        n_ambiguous = 0
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                for rr in prior:
                    seq = int(rr["seq"])
                    lat = float(rr["lat"])
                    lon = float(rr["lon"])
                    n_total += 1

                    # Preserve explicit/manual winner already stored in relation_stop_prior.
                    existing_match = str(rr.get("matched_stop_node_id") or "").strip()
                    if existing_match:
                        rr["match_state"] = "matched"
                        rr["candidate_count"] = int(rr.get("candidate_count") or 1)
                        rr["candidate_node_ids"] = [existing_match]
                        rr["nearest_dist_m"] = rr.get("match_dist_m")
                        rr["candidates"] = [
                            {
                                "node_id": existing_match,
                                "dist_m": float(rr.get("match_dist_m") or 0.0),
                                "name": None,
                                "ref": None,
                                "source": "manual_resolve",
                                "lat": None,
                                "lon": None,
                                "updated_at": None,
                            }
                        ]
                        n_matched += 1
                        if write:
                            cur.execute(
                                """
                                UPDATE route_work.relation_stop_prior
                                SET matched_stop_node_id=%s, match_dist_m=%s
                                WHERE route_id=%s AND seq=%s
                                """,
                                (existing_match, rr.get("match_dist_m"), str(route_id), seq),
                            )
                        updated.append(rr)
                        continue

                    hits = self._find_stop_candidates_within_radius(
                        conn,
                        lat=lat,
                        lon=lon,
                        radius_m=radius_m,
                        limit=5,
                    )
                    candidate_count = len(hits)
                    rr["candidate_count"] = candidate_count
                    rr["candidate_node_ids"] = [str(h.get("node_id") or "") for h in hits if h.get("node_id")]
                    rr["nearest_dist_m"] = float(hits[0]["dist_m"]) if hits else None
                    rr["candidates"] = hits

                    if candidate_count == 1:
                        rr["match_state"] = "matched"
                        rr["matched_stop_node_id"] = str(hits[0].get("node_id") or "")
                        rr["match_dist_m"] = float(hits[0].get("dist_m") or 0.0)
                        n_matched += 1
                    elif candidate_count == 0:
                        rr["match_state"] = "unmatched"
                        rr["matched_stop_node_id"] = None
                        rr["match_dist_m"] = None
                        n_unmatched += 1
                    else:
                        rr["match_state"] = "ambiguous"
                        rr["matched_stop_node_id"] = None
                        rr["match_dist_m"] = None
                        n_ambiguous += 1

                    if write:
                        cur.execute(
                            """
                            UPDATE route_work.relation_stop_prior
                            SET matched_stop_node_id=%s, match_dist_m=%s
                            WHERE route_id=%s AND seq=%s
                            """,
                            (rr["matched_stop_node_id"], rr["match_dist_m"], str(route_id), seq),
                        )
                    updated.append(rr)

            if write:
                try:
                    conn.commit()
                except Exception:
                    pass
        if write and invalidate_resolution:
            try:
                self.invalidate_route_resolution(
                    route_id,
                    reason="prior_stop_matching_changed",
                    clear_sequence_candidates=True,
                    clear_geometry_candidates=True,
                    clear_route_approvals=True,
                    clear_route_prod=True,
                    clear_sequence_approval=True,
                    demote_direction=True,
                )
            except Exception:
                pass

        return {
            "route_id": str(route_id),
            "radius_m": radius_m,
            "total": int(n_total),
            "matched": int(n_matched),
            "unmatched": int(n_unmatched),
            "ambiguous": int(n_ambiguous),
            "all_matched": bool(n_total > 0 and n_matched == n_total and n_unmatched == 0 and n_ambiguous == 0),
            "rows": updated,
        }

    def match_prior_to_canonical(
        self,
        route_id: uuid.UUID,
        radius_m: float,
        prior_rows: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        report = self.get_prior_match_report(
            route_id,
            radius_m=radius_m,
            prior_rows=prior_rows,
            write=True,
            invalidate_resolution=True,
        )
        return list(report.get("rows") or [])

    def sync_unresolved_prior_to_phase1_requests(
        self,
        route_id: uuid.UUID,
        *,
        radius_m: float = 3.0,
        match_states: Optional[List[str]] = None,
        requested_by: Optional[str] = None,
        notes: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        report = self.get_prior_match_report(
            route_id,
            radius_m=radius_m,
            write=(not dry_run),
            invalidate_resolution=False,
        )
        rows = [dict(r) for r in (report.get("rows") or [])]
        target_states = {str(s or "").strip().lower() for s in (match_states or ["unmatched", "ambiguous"])}
        target_states = {s for s in target_states if s in {"unmatched", "ambiguous"}}
        if not target_states:
            target_states = {"unmatched", "ambiguous"}
        unresolved = [r for r in rows if str(r.get("match_state") or "").strip().lower() in target_states]

        out: Dict[str, Any] = {
            "route_id": str(route_id),
            "radius_m": float(radius_m),
            "match_states": sorted(target_states),
            "unresolved_total": len(unresolved),
            "inserted": 0,
            "updated": 0,
            "deleted_stale_requested": 0,
            "dry_run": bool(dry_run),
        }
        if dry_run:
            out["unresolved_rows"] = unresolved
            return out

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                # If table is missing, return informative payload but do not hard crash.
                cur.execute("SELECT to_regclass('node_work.node_review_requests') IS NOT NULL AS ok")
                if not bool((cur.fetchone() or {}).get("ok")):
                    out["error"] = "node_work.node_review_requests table not found"
                    return out

                unresolved_by_seq = {int(r.get("seq") or 0): r for r in unresolved}
                unresolved_seqs = sorted([s for s in unresolved_by_seq.keys() if s > 0])

                cur.execute(
                    """
                    SELECT request_id::text AS request_id, seq, status,
                           COALESCE(tags->>'match_state', '') AS existing_match_state
                    FROM node_work.node_review_requests
                    WHERE source='phase3_route'
                      AND route_id::text = %s
                    """,
                    (str(route_id),),
                )
                existing = cur.fetchall() or []
                existing_requested_by_seq: Dict[int, str] = {}
                stale_requested_ids: List[str] = []
                for r in existing:
                    seq = int(r.get("seq") or 0)
                    status = str(r.get("status") or "")
                    req_id = str(r.get("request_id") or "")
                    existing_state = str(r.get("existing_match_state") or "").strip().lower()
                    if status == "requested":
                        if seq in unresolved_by_seq:
                            existing_requested_by_seq[seq] = req_id
                        elif req_id and existing_state in target_states:
                            stale_requested_ids.append(req_id)

                for seq, rr in unresolved_by_seq.items():
                    state = str(rr.get("match_state") or "unmatched")
                    candidates = [dict(c) for c in (rr.get("candidates") or [])]
                    ambiguity_pack = {
                        "issue_type": state,
                        "route_id": str(route_id),
                        "seq": int(seq),
                        "radius_m": float(radius_m),
                        "candidate_count": int(rr.get("candidate_count") or 0),
                        "nearest_dist_m": rr.get("nearest_dist_m"),
                        "candidate_node_ids": [str(x) for x in (rr.get("candidate_node_ids") or []) if str(x or "").strip()],
                        "candidates": candidates,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    tags = {
                        "issue_type": state,
                        "route_id": str(route_id),
                        "seq": int(seq),
                        "role": rr.get("role"),
                        "member_type": rr.get("member_type"),
                        "osm_ref": rr.get("osm_ref"),
                        "osm_node_id": rr.get("osm_node_id"),
                        "match_state": state,
                        "candidate_count": int(rr.get("candidate_count") or 0),
                        "nearest_dist_m": rr.get("nearest_dist_m"),
                        "radius_m": float(radius_m),
                        "ambiguity_pack": ambiguity_pack,
                    }
                    request_notes = notes or "Auto-synced from Phase3 unresolved stop-prior rows."
                    if seq in existing_requested_by_seq:
                        cur.execute(
                            """
                            UPDATE node_work.node_review_requests
                            SET lat=%s,
                                lon=%s,
                                node_type='STOP',
                                tags=%s::jsonb,
                                requested_by=COALESCE(%s, requested_by),
                                notes=%s
                            WHERE request_id::text = %s
                            """,
                            (
                                float(rr.get("lat") or 0.0),
                                float(rr.get("lon") or 0.0),
                                json.dumps(tags, ensure_ascii=False),
                                requested_by,
                                request_notes,
                                existing_requested_by_seq[seq],
                            ),
                        )
                        out["updated"] += int(cur.rowcount or 0)
                    else:
                        cur.execute(
                            """
                            INSERT INTO node_work.node_review_requests
                              (source, route_id, seq, status, lat, lon, node_type, tags, requested_by, notes)
                            VALUES
                              ('phase3_route', %s, %s, 'requested', %s, %s, 'STOP', %s::jsonb, %s, %s)
                            """,
                            (
                                str(route_id),
                                int(seq),
                                float(rr.get("lat") or 0.0),
                                float(rr.get("lon") or 0.0),
                                json.dumps(tags, ensure_ascii=False),
                                requested_by,
                                request_notes,
                            ),
                        )
                        out["inserted"] += int(cur.rowcount or 0)

                if stale_requested_ids:
                    cur.execute(
                        """
                        DELETE FROM node_work.node_review_requests
                        WHERE request_id = ANY(%s::uuid[])
                        """,
                        (stale_requested_ids,),
                    )
                    out["deleted_stale_requested"] = int(cur.rowcount or 0)

                out["unresolved_seqs"] = unresolved_seqs
        return out

    # -------------------------------------------------------------------------
    # Stop sequences (optional, but useful for console completeness)
    # -------------------------------------------------------------------------

    def list_stop_sequence_sets(self, route_id: uuid.UUID) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT set_id, route_id, notes, created_at
                    FROM route_work.stop_sequence_candidate_sets
                    WHERE route_id=%s
                    ORDER BY created_at DESC NULLS LAST
                    """,
                    (str(route_id),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]
    
    def build_sequence_candidates_strict_relaxed(
        self,
        route_id: uuid.UUID,
        prior_rows: Optional[List[Dict[str, Any]]] = None,
        created_by: Optional[str] = None,
    ) -> uuid.UUID:
        with db_conn() as conn:
            # load prior if not provided
            if prior_rows is None:
                with db_cursor(conn) as cur:
                    cur.execute(
                        """
                        SELECT seq, lat, lon, osm_node_id, osm_ref, member_type, role,
                               matched_stop_node_id, match_dist_m
                        FROM route_work.relation_stop_prior
                        WHERE route_id=%s
                        ORDER BY seq
                        """,
                        (str(route_id),),
                    )
                    prior_rows = cur.fetchall() or []

            set_id = build_sequence_candidates(
                conn=conn,
                route_id=route_id,
                prior_rows=prior_rows,
            )

            try:
                conn.commit()
            except Exception:
                pass

        return set_id


    def list_stop_sequence_candidates(self, set_id: uuid.UUID) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT candidate_id, set_id, rank, stop_node_ids, stop_prior_seqs, metrics, created_at
                    FROM route_work.stop_sequence_candidates
                    WHERE set_id=%s
                    ORDER BY rank ASC NULLS LAST, created_at DESC NULLS LAST
                    """,
                    (str(set_id),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    # -------------------------------------------------------------------------
    # Geometry presets
    # -------------------------------------------------------------------------
    def list_valhalla_presets(self, *, active_only: bool = False) -> List[Dict[str, Any]]:
        try:
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute(
                        """
                        SELECT name, params, is_active
                        FROM route_work.valhalla_presets
                        ORDER BY name ASC
                        """
                    )
                    rows = cur.fetchall() or []

            presets: List[Dict[str, Any]] = []
            for r in rows:
                active = bool(r.get("is_active", True))
                if active_only and not active:
                    continue
                presets.append(
                    {
                        "name": r.get("name"),
                        "costing_options": r.get("params") or {},
                        "active": active,
                        "notes": None,
                    }
                )
            if presets:
                return presets
        except Exception:
            pass

        # fallback
        out = []
        for p in DEFAULT_PRESETS:
            if active_only and not p.active:
                continue
            out.append(
                {
                    "name": p.name,
                    "costing_options": p.costing_options,
                    "active": p.active,
                    "notes": p.notes,
                }
            )
        return out


        # -------------------------------------------------------------------------
    # Analytics for widgets: divergence / scatter / histograms / hotspots / audit
    # -------------------------------------------------------------------------

    def list_valhalla_run_logs(
        self,
        *,
        route_id: Optional[uuid.UUID] = None,
        geometry_candidate_set_id: Optional[uuid.UUID] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        For selection_learning_panel.py
        SQL: route_work.valhalla_run_logs
        """
        limit = int(limit)
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                if route_id and geometry_candidate_set_id:
                    cur.execute(
                        """
                        SELECT run_id, route_id, geometry_candidate_set_id, preset_id, engine,
                               request_json, response_meta, reward, created_at
                        FROM route_work.valhalla_run_logs
                        WHERE route_id=%s AND geometry_candidate_set_id=%s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (str(route_id), str(geometry_candidate_set_id), limit),
                    )
                elif route_id:
                    cur.execute(
                        """
                        SELECT run_id, route_id, geometry_candidate_set_id, preset_id, engine,
                               request_json, response_meta, reward, created_at
                        FROM route_work.valhalla_run_logs
                        WHERE route_id=%s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (str(route_id), limit),
                    )
                elif geometry_candidate_set_id:
                    cur.execute(
                        """
                        SELECT run_id, route_id, geometry_candidate_set_id, preset_id, engine,
                               request_json, response_meta, reward, created_at
                        FROM route_work.valhalla_run_logs
                        WHERE geometry_candidate_set_id=%s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (str(geometry_candidate_set_id), limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT run_id, route_id, geometry_candidate_set_id, preset_id, engine,
                               request_json, response_meta, reward, created_at
                        FROM route_work.valhalla_run_logs
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def list_geometry_ranking_labels(self, *, set_id: uuid.UUID) -> List[Dict[str, Any]]:
        """
        For selection_learning_panel.py
        SQL: route_work.geometry_ranking_labels
        """
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT set_id, geometry_candidate_id, label
                    FROM route_work.geometry_ranking_labels
                    WHERE set_id=%s
                    ORDER BY label DESC, geometry_candidate_id ASC
                    """,
                    (str(set_id),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def list_decision_audit(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        """
        For decision_audit_table.py (upgraded)
        Joins approvals + jobs + chosen candidate metrics.
        """
        limit = int(limit)
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      ra.approval_id,
                      ra.route_id,
                      ra.chosen_geometry_candidate_id,
                      ra.chosen_stop_sequence_candidate_id,
                      ra.approved_at,
                      ra.approved_by,
                      ra.notes,

                      rj.status,
                      rj.area_key,
                      rj.known_ref,
                      rj.chosen_osm_relation_id AS osm_relation_id,

                      gc.score AS chosen_score,
                      gc.length_m AS chosen_length_m,
                      gc.avg_stop_dist_m AS chosen_avg_stop_dist_m,
                      gc.max_stop_dist_m AS chosen_max_stop_dist_m,
                      gc.engine AS chosen_engine,
                      gc.created_at AS chosen_candidate_created_at
                    FROM route_work.route_approvals ra
                    JOIN route_raw.route_jobs rj ON rj.route_id = ra.route_id
                    LEFT JOIN route_work.geometry_candidates gc
                      ON gc.geometry_candidate_id = ra.chosen_geometry_candidate_id
                    ORDER BY ra.approved_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def get_route_metrics_for_candidate(self, *, geometry_candidate_id: uuid.UUID) -> Dict[str, Any]:
        """
        For route_metrics_bar.py (convenience wrapper)
        SQL: route_work.geometry_candidates
        """
        c = self.get_geometry_candidate(geometry_candidate_id)
        if not c:
            return {}
        return {
            "geometry_candidate_id": c.get("geometry_candidate_id"),
            "score": c.get("score"),
            "length_m": c.get("length_m"),
            "avg_stop_dist_m": c.get("avg_stop_dist_m"),
            "max_stop_dist_m": c.get("max_stop_dist_m"),
            "engine": c.get("engine"),
            "params": c.get("params") or {},
            "metrics": c.get("metrics") or {},
            "created_at": c.get("created_at"),
        }

    def compute_stop_spacing_histogram(
        self,
        *,
        route_id: uuid.UUID,
        stop_sequence_candidate_id: uuid.UUID,
    ) -> Dict[str, Any]:
        """
        For stop_spacing_histogram.py
        Uses your existing stop-point fetcher + haversine.
        """
        pts = self.get_stop_points_for_candidate(route_id=route_id, stop_sequence_candidate_id=stop_sequence_candidate_id)
        if not pts or len(pts) < 2:
            return {"spacings_m": [], "n": 0}

        spacings: List[float] = []
        for i in range(len(pts) - 1):
            lon1, lat1 = float(pts[i]["lon"]), float(pts[i]["lat"])
            lon2, lat2 = float(pts[i + 1]["lon"]), float(pts[i + 1]["lat"])
            spacings.append(_haversine_m(lon1, lat1, lon2, lat2))

        return {
            "route_id": str(route_id),
            "stop_sequence_candidate_id": str(stop_sequence_candidate_id),
            "n": len(spacings),
            "spacings_m": spacings,
            "mean_m": float(sum(spacings) / len(spacings)) if spacings else 0.0,
            "median_m": float(median(spacings)) if spacings else 0.0,
            "p90_m": float(_percentile(spacings, 90)) if spacings else 0.0,
            "max_m": float(max(spacings)) if spacings else 0.0,
        }

    def compute_turn_angle_histogram(
        self,
        *,
        geometry_candidate_id: uuid.UUID,
        sample_every_n: int = 1,
    ) -> Dict[str, Any]:
        """
        For turn_angle_histogram.py
        Turn severity computed from polyline vertices (degrees).
        """
        sample_every_n = max(1, int(sample_every_n))
        cand = self.get_geometry_candidate(geometry_candidate_id)
        wkt = (cand.get("geom_wkt") or "").strip()
        coords = _parse_linestring_wkt(wkt)
        if len(coords) < 3:
            return {"geometry_candidate_id": str(geometry_candidate_id), "angles_deg": [], "n": 0}

        # optional sampling to reduce noise / heavy polylines
        if sample_every_n > 1:
            coords = coords[::sample_every_n]
            if coords[-1] != _parse_linestring_wkt(wkt)[-1]:
                coords.append(_parse_linestring_wkt(wkt)[-1])

        angles: List[float] = []
        for i in range(1, len(coords) - 1):
            angles.append(_turn_severity_deg(coords[i - 1], coords[i], coords[i + 1]))

        return {
            "geometry_candidate_id": str(geometry_candidate_id),
            "n": len(angles),
            "angles_deg": angles,
            "mean_deg": float(sum(angles) / len(angles)) if angles else 0.0,
            "median_deg": float(median(angles)) if angles else 0.0,
            "p90_deg": float(_percentile(angles, 90)) if angles else 0.0,
            "max_deg": float(max(angles)) if angles else 0.0,
        }

    def compute_turn_hotspots(
        self,
        *,
        geometry_candidate_id: uuid.UUID,
        threshold_deg: float = 50.0,
        top_k: int = 40,
        sample_every_n: int = 1,
    ) -> List[Dict[str, Any]]:
        """
        For turn_hotspots_map.py
        Returns points (lon,lat) with turn severity >= threshold_deg.
        """
        threshold_deg = float(threshold_deg)
        top_k = max(1, int(top_k))
        sample_every_n = max(1, int(sample_every_n))

        cand = self.get_geometry_candidate(geometry_candidate_id)
        wkt = (cand.get("geom_wkt") or "").strip()
        coords = _parse_linestring_wkt(wkt)
        if len(coords) < 3:
            return []

        if sample_every_n > 1:
            full = coords
            coords = full[::sample_every_n]
            if coords[-1] != full[-1]:
                coords.append(full[-1])

        hits: List[Dict[str, Any]] = []
        for i in range(1, len(coords) - 1):
            sev = _turn_severity_deg(coords[i - 1], coords[i], coords[i + 1])
            if sev >= threshold_deg:
                lon, lat = coords[i]
                hits.append(
                    {
                        "geometry_candidate_id": str(geometry_candidate_id),
                        "i": i,
                        "lon": float(lon),
                        "lat": float(lat),
                        "angle_deg": float(sev),
                        "severity": float(sev),
                    }
                )

        hits.sort(key=lambda x: x["severity"], reverse=True)
        return hits[:top_k]

    def compute_geometry_divergence_pair(
        self,
        *,
        a_geometry_candidate_id: uuid.UUID,
        b_geometry_candidate_id: uuid.UUID,
        buffer_m: float = 25.0,
    ) -> Dict[str, Any]:
        """
        For route_divergence_map.py
        PostGIS-based divergence between two candidate lines:
        - hausdorff distance in meters (3857)
        - symmetric overlap fraction within buffer_m
        """
        buffer_m = float(buffer_m)

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    WITH
                      a AS (
                        SELECT geometry_candidate_id, geom
                        FROM route_work.geometry_candidates
                        WHERE geometry_candidate_id=%s
                      ),
                      b AS (
                        SELECT geometry_candidate_id, geom
                        FROM route_work.geometry_candidates
                        WHERE geometry_candidate_id=%s
                      ),
                      ax AS (
                        SELECT ST_Transform(a.geom, 3857) AS g FROM a
                      ),
                      bx AS (
                        SELECT ST_Transform(b.geom, 3857) AS g FROM b
                      ),
                      lens AS (
                        SELECT
                          NULLIF(ST_Length(ax.g), 0) AS len_a,
                          NULLIF(ST_Length(bx.g), 0) AS len_b
                        FROM ax, bx
                      )
                    SELECT
                      ST_HausdorffDistance(ax.g, bx.g) AS hausdorff_m,

                      CASE
                        WHEN lens.len_a IS NULL THEN 0
                        ELSE ST_Length(ST_Intersection(ax.g, ST_Buffer(bx.g, %s))) / lens.len_a
                      END AS a_within_b,

                      CASE
                        WHEN lens.len_b IS NULL THEN 0
                        ELSE ST_Length(ST_Intersection(bx.g, ST_Buffer(ax.g, %s))) / lens.len_b
                      END AS b_within_a,

                      ST_AsText((SELECT geom FROM route_work.geometry_candidates WHERE geometry_candidate_id=%s)) AS a_wkt,
                      ST_AsText((SELECT geom FROM route_work.geometry_candidates WHERE geometry_candidate_id=%s)) AS b_wkt
                    FROM ax, bx, lens
                    """,
                    (
                        str(a_geometry_candidate_id),
                        str(b_geometry_candidate_id),
                        buffer_m,
                        buffer_m,
                        str(a_geometry_candidate_id),
                        str(b_geometry_candidate_id),
                    ),
                )
                row = cur.fetchone() or {}

        a_within_b = float(row.get("a_within_b") or 0.0)
        b_within_a = float(row.get("b_within_a") or 0.0)
        overlap = 0.5 * (a_within_b + b_within_a)

        return {
            "a_id": str(a_geometry_candidate_id),
            "b_id": str(b_geometry_candidate_id),
            "buffer_m": buffer_m,
            "hausdorff_m": float(row.get("hausdorff_m") or 0.0),
            "a_within_b": a_within_b,
            "b_within_a": b_within_a,
            "overlap_ratio": float(overlap),
            "a_wkt": row.get("a_wkt"),
            "b_wkt": row.get("b_wkt"),
        }

    def compute_stop_route_deviation(
        self,
        *,
        route_id: uuid.UUID,
        geometry_candidate_id: uuid.UUID,
    ) -> List[Dict[str, Any]]:
        """
        For stop_route_deviation_map.py
        For each stop (from the candidate's stop_sequence_candidate_id), compute distance to route geom.

        Uses PostGIS:
        - ST_DistanceSphere(point, line)
        - ST_ClosestPoint(line, point) (optional)
        """
        cand = self.get_geometry_candidate(geometry_candidate_id)
        if not cand:
            return []

        ssc_id = cand.get("stop_sequence_candidate_id")
        if not ssc_id:
            return []

        stops = self.get_stop_points_for_candidate(
            route_id=route_id,
            stop_sequence_candidate_id=uuid.UUID(str(ssc_id)),
        )
        if not stops:
            return []

        # build point wkts in same order
        pt_wkts = [f"POINT({float(r['lon'])} {float(r['lat'])})" for r in stops]

        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    WITH pts AS (
                      SELECT row_number() OVER () AS i, ST_GeomFromText(wkt, 4326) AS p
                      FROM unnest(%s::text[]) AS wkt
                    ),
                    line AS (
                      SELECT geom AS g
                      FROM route_work.geometry_candidates
                      WHERE geometry_candidate_id=%s
                    )
                    SELECT
                      pts.i,
                      ST_X(pts.p) AS lon,
                      ST_Y(pts.p) AS lat,
                      ST_DistanceSphere(pts.p, line.g) AS dist_to_route_m,
                      ST_AsText(ST_ClosestPoint(line.g, pts.p)) AS closest_point_wkt
                    FROM pts, line
                    ORDER BY pts.i ASC
                    """,
                    (pt_wkts, str(geometry_candidate_id)),
                )
                rows = cur.fetchall() or []

        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "route_id": str(route_id),
                    "geometry_candidate_id": str(geometry_candidate_id),
                    "i": int(r["i"]),
                    "lon": float(r["lon"]),
                    "lat": float(r["lat"]),
                    "dist_to_route_m": float(r["dist_to_route_m"] or 0.0),
                    "closest_point_wkt": r.get("closest_point_wkt"),
                }
            )
        return out

    def list_detour_vs_coverage(
        self,
        *,
        route_id: uuid.UUID,
        stop_buffer_m: float = 40.0,
    ) -> List[Dict[str, Any]]:
        """
        For detour_vs_coverage_scatter.py

        For each geometry candidate in this route:
          - detour_ratio = length_m / min_length_m (within this route)
          - coverage = fraction of stops within stop_buffer_m from the route line

        Coverage uses PostGIS distance to candidate geom.
        """
        stop_buffer_m = float(stop_buffer_m)

        # candidates for this route
        cands = self.list_geometry_candidates_flat(route_id)
        if not cands:
            return []

        lengths = [float(c.get("length_m") or 0.0) for c in cands if float(c.get("length_m") or 0.0) > 0]
        min_len = min(lengths) if lengths else 0.0

        out: List[Dict[str, Any]] = []

        for c in cands:
            cid = uuid.UUID(str(c["geometry_candidate_id"]))
            ssc = c.get("stop_sequence_candidate_id")

            coverage: Optional[float] = None
            n_stops = 0

            if ssc:
                stops = self.get_stop_points_for_candidate(
                    route_id=route_id,
                    stop_sequence_candidate_id=uuid.UUID(str(ssc)),
                )
                n_stops = len(stops)
                if stops:
                    pt_wkts = [f"POINT({float(r['lon'])} {float(r['lat'])})" for r in stops]
                    with db_conn() as conn:
                        with db_cursor(conn) as cur:
                            cur.execute(
                                """
                                WITH pts AS (
                                  SELECT ST_GeomFromText(wkt, 4326) AS p
                                  FROM unnest(%s::text[]) AS wkt
                                ),
                                line AS (
                                  SELECT geom AS g
                                  FROM route_work.geometry_candidates
                                  WHERE geometry_candidate_id=%s
                                )
                                SELECT
                                  SUM(CASE WHEN ST_DistanceSphere(pts.p, line.g) <= %s THEN 1 ELSE 0 END)::float AS within,
                                  COUNT(*)::float AS total
                                FROM pts, line
                                """,
                                (pt_wkts, str(cid), stop_buffer_m),
                            )
                            rr = cur.fetchone() or {}
                    total = float(rr.get("total") or 0.0)
                    within = float(rr.get("within") or 0.0)
                    coverage = (within / total) if total > 0 else None

            length_m = float(c.get("length_m") or 0.0)
            detour_ratio = (length_m / min_len) if (min_len > 0 and length_m > 0) else None

            out.append(
                {
                    "route_id": str(route_id),
                    "geometry_candidate_id": str(cid),
                    "set_id": c.get("set_id"),
                    "stop_sequence_candidate_id": str(ssc) if ssc else None,
                    "score": float(c.get("score") or 0.0),
                    "length_m": length_m,
                    "detour_ratio": float(detour_ratio) if detour_ratio is not None else None,
                    "coverage": float(coverage) if coverage is not None else None,
                    "n_stops": int(n_stops),
                    "avg_stop_dist_m": c.get("avg_stop_dist_m"),
                    "max_stop_dist_m": c.get("max_stop_dist_m"),
                }
            )

        # nice ordering for plotting
        out.sort(key=lambda r: (r["score"] if r["score"] is not None else -1), reverse=True)
        return out


    def _preset_map(self) -> Dict[str, Dict[str, Any]]:
        presets = self.list_valhalla_presets(active_only=False)
        return {str(p["name"]): (p.get("costing_options") or {}) for p in presets if p.get("name")}

    # -------------------------------------------------------------------------
    # Geometry sets + candidates
    # -------------------------------------------------------------------------

    def list_geometry_sets(self, route_id: uuid.UUID) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                      gcs.set_id,
                      gcs.route_id,
                      gcs.created_at,
                      gcs.created_by,
                      gcs.notes,
                      (SELECT COUNT(1) FROM route_work.geometry_candidates gc WHERE gc.set_id=gcs.set_id) AS n_candidates
                    FROM route_work.geometry_candidate_sets gcs
                    WHERE gcs.route_id=%s
                    ORDER BY gcs.created_at DESC NULLS LAST
                    """,
                    (str(route_id),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def list_geometry_candidates(self, geometry_set_id: uuid.UUID) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT
                        geometry_candidate_id,
                        set_id,
                        stop_sequence_candidate_id,
                        engine,
                        score,
                        length_m,
                        avg_stop_dist_m,
                        max_stop_dist_m,
                        params,
                        metrics,
                        created_at,
                        ST_AsText(geom) AS geom_wkt
                    FROM route_work.geometry_candidates
                    WHERE set_id=%s
                    ORDER BY score DESC NULLS LAST, created_at ASC
                    """,
                    (str(geometry_set_id),),
                )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def build_geometry_candidates_from_stop_sequence(
        self,
        *,
        route_id: uuid.UUID,
        stop_sequence_candidate_id: uuid.UUID,
        preset_names: List[str],
        created_by: Optional[str] = None,
    ) -> uuid.UUID:
        """
        Builds geometry candidates via Valhalla for the chosen stop_sequence_candidate_id
        using selected preset_names.

        Returns geometry_set_id.
        """
        created_by = created_by or os.getenv("USER") or os.getenv("USERNAME") or "console"
        gate = self.get_step30_gate(
            route_id,
            stop_sequence_candidate_id=stop_sequence_candidate_id,
            match_radius_m=3.0,
        )
        if not bool(gate.get("can_run_step30")):
            raise RuntimeError(
                "Step 30 blocked: "
                + ", ".join(str(x) for x in list(gate.get("blocking_reasons") or []) if str(x).strip())
            )

        preset_map = self._preset_map()
        chosen: List[Tuple[str, Dict[str, Any]]] = []
        for name in preset_names:
            if name in preset_map:
                chosen.append((name, preset_map[name]))
        if not chosen:
            # fallback to defaults if user gave unknown names
            for p in DEFAULT_PRESETS[:2]:
                chosen.append((p.name, p.costing_options))

        with db_conn() as conn:
            # 1) Load sequence candidate
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT stop_node_ids, stop_prior_seqs, metrics, set_id
                    FROM route_work.stop_sequence_candidates
                    WHERE candidate_id=%s
                    """,
                    (str(stop_sequence_candidate_id),),
                )
                row = cur.fetchone()
            if not row:
                raise ValueError("stop_sequence_candidate_id not found")

            stop_node_ids = _parse_uuid_array(row.get("stop_node_ids"))
            stop_prior_seqs = _parse_int_array(row.get("stop_prior_seqs"))
            metrics = row.get("metrics") or {}

            # 2) Get stop locations (lon,lat) in correct order
            coords: List[LonLat]
            coords_source: str

            if stop_node_ids:
                coords = self._fetch_stop_points_from_canonical(conn, stop_node_ids)
                markers = [str(x) for x in stop_node_ids]
                coords_source = "canonical_stop_node_ids"
            elif stop_prior_seqs:
                coords = self._fetch_stop_points_from_prior(conn, route_id, stop_prior_seqs)
                markers = [f"seq:{x}" for x in stop_prior_seqs]
                coords_source = "relation_stop_prior_seqs"
            else:
                raise ValueError("Candidate has neither stop_node_ids nor stop_prior_seqs")

            markers, coords, truncated_by_marker_repeat = _truncate_at_first_repeat_markers(markers, coords)
            markers, coords, truncated_by_coord_loop = _truncate_at_first_coordinate_return(markers, coords)

            if len(coords) < 2:
                raise ValueError("Need at least 2 stops to build geometry")

            # 3) Create geometry set
            set_id = uuid.uuid4()
            notes = f"valhalla presets for seq={stop_sequence_candidate_id} (source={coords_source}, mode={metrics.get('mode')})"
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_work.geometry_candidate_sets
                      (set_id, route_id, stop_sequence_set_id, created_by, notes)
                    VALUES
                      (%s, %s, %s, %s, %s)
                    """,
                    (str(set_id), str(route_id), str(row.get("set_id")), created_by, notes),
                )

            # 4) Build candidates
            for rank, (preset_name, costing_options) in enumerate(chosen, start=1):
                try:
                    shape_pts = valhalla_route(coords, costing_options=costing_options)
                    shape_pts = _pin_shape_endpoints(coords, shape_pts)
                    tight = _directional_tightness(coords, shape_pts)
                    if not tight["is_direction_tight"]:
                        raise ValueError(
                            f"direction_tightness_failed(start={tight['start_anchor_dist_m']:.1f}m, "
                            f"end={tight['end_anchor_dist_m']:.1f}m, mono={tight['monotonic_violations']})"
                        )
                    wkt = _to_linestring_wkt(shape_pts)

                    # scoring: best when canonical stop ids exist
                    if stop_node_ids:
                        score, length_m, avg_d, max_d, m = score_geometry(conn, wkt, stop_node_ids)
                    else:
                        score, length_m, avg_d, max_d, m = 0.0, 0.0, 0.0, 0.0, {"note": "raw-first (no canonical scoring)"}

                    params = {
                        "preset_name": preset_name,
                        "costing": VALHALLA_COSTING,
                        "shape_format": VALHALLA_SHAPE_FORMAT,
                        "costing_options": costing_options,
                        "coords_source": coords_source,
                        "variant_rank": rank,
                        "direction_from": markers[0],
                        "direction_to": markers[-1],
                        "direction_label": f"{markers[0]} -> {markers[-1]}",
                        "truncated_by_marker_repeat": truncated_by_marker_repeat,
                        "truncated_by_coord_loop": truncated_by_coord_loop,
                    }

                    cid = uuid.uuid4()
                    with db_cursor(conn) as cur:
                        cur.execute(
                            """
                            INSERT INTO route_work.geometry_candidates
                                (geometry_candidate_id, set_id, stop_sequence_candidate_id, engine, params, geom,
                                score, length_m, avg_stop_dist_m, max_stop_dist_m, metrics)
                            VALUES
                                (%s, %s, %s, %s, %s::jsonb, ST_GeomFromText(%s, 4326),
                                %s, %s, %s, %s, %s::jsonb)

                            """,
                            (
                                str(cid),
                                str(set_id),
                                str(stop_sequence_candidate_id),
                                "valhalla_route",
                                json.dumps(_jsonable(params), ensure_ascii=False),
                                wkt,
                                float(score),
                                float(length_m),
                                float(avg_d),
                                float(max_d),
                                json.dumps(_jsonable({**(m or {}), "direction_tightness": tight}), ensure_ascii=False),
                            ),
                        )

                except Exception as e:
                    # Log failure (do NOT insert geometry_candidates with NULL geom)
                    with db_cursor(conn) as cur:
                        cur.execute(
                            """
                            INSERT INTO route_work.valhalla_run_logs
                            (run_id, route_id, geometry_candidate_set_id, preset_id, engine,
                            request_json, response_meta, reward)
                            VALUES
                            (%s, %s, %s, %s, %s,
                            %s::jsonb, %s::jsonb, %s)
                            """,
                            (
                                str(uuid.uuid4()),
                                str(route_id),
                                str(set_id),
                                None,  # preset_id optional, you're using preset_name in params
                                "valhalla_route_failed",
                                json.dumps(_jsonable({"coords_source": coords_source, "preset_name": preset_name}), ensure_ascii=False),
                                json.dumps(_jsonable({"error": str(e)}), ensure_ascii=False),
                                None,
                            ),
                        )
                    # keep going to next preset
                    continue


            try:
                conn.commit()
            except Exception:
                pass

        return set_id

    # -------------------------------------------------------------------------
    # Approve/Publish tab support
    # -------------------------------------------------------------------------
    def approve_route(
        self,
        *,
        route_id: uuid.UUID,
        geometry_candidate_id: uuid.UUID,
        stop_sequence_candidate_id: Optional[uuid.UUID] = None,
        approved_by: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        approved_by = approved_by or os.getenv("USER") or os.getenv("USERNAME") or "console"
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_work.route_approvals
                    (route_id, chosen_geometry_candidate_id, chosen_stop_sequence_candidate_id, approved_by, notes)
                    VALUES
                    (%s, %s, %s, %s, %s)
                    ON CONFLICT (route_id)
                    DO UPDATE SET
                    chosen_geometry_candidate_id = EXCLUDED.chosen_geometry_candidate_id,
                    chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
                    approved_at = now(),
                    approved_by = EXCLUDED.approved_by,
                    notes = EXCLUDED.notes
                    """,
                    (str(route_id), str(geometry_candidate_id), str(stop_sequence_candidate_id) if stop_sequence_candidate_id else None, approved_by, notes),
                )
            try:
                conn.commit()
            except Exception:
                pass


    def list_route_approvals(self, *, limit: int = 200, route_id: Optional[uuid.UUID] = None) -> List[Dict[str, Any]]:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                if route_id:
                    cur.execute(
                        """
                        SELECT approval_id, route_id, chosen_geometry_candidate_id, chosen_stop_sequence_candidate_id,
                            approved_at, approved_by, notes
                        FROM route_work.route_approvals
                        WHERE route_id=%s
                        ORDER BY approved_at DESC
                        LIMIT %s
                        """,
                        (str(route_id), limit),
                    )
                else:
                    cur.execute(
                        """
                        SELECT approval_id, route_id, chosen_geometry_candidate_id, chosen_stop_sequence_candidate_id,
                            approved_at, approved_by, notes
                        FROM route_work.route_approvals
                        ORDER BY approved_at DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                rows = cur.fetchall() or []
        return [self._row(r) for r in rows]

    def upsert_geometry_ranking_label(
        self,
        *,
        set_id: uuid.UUID,
        geometry_candidate_id: uuid.UUID,
        label: int,
    ) -> None:
        with db_conn() as conn:
            with db_cursor(conn) as cur:
                cur.execute(
                    """
                    INSERT INTO route_work.geometry_ranking_labels (set_id, geometry_candidate_id, label)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (set_id, geometry_candidate_id)
                    DO UPDATE SET label = EXCLUDED.label
                    """,
                    (str(set_id), str(geometry_candidate_id), int(label)),
                )
            try:
                conn.commit()
            except Exception:
                pass



    def dashboard_snapshot(self, route_id: uuid.UUID) -> Dict[str, Any]:
        """
        A compact snapshot for the Approve/Publish tab.
        """
        snap: Dict[str, Any] = {"route_id": str(route_id)}

        job = self.get_route_job(route_id)
        snap["route_job"] = job

        try:
            prior = self.get_relation_stop_prior(route_id)
            snap["prior"] = {"n_rows": len(prior), "sample": prior[:3]}
        except Exception as e:
            snap["prior"] = {"error": str(e)}

        try:
            seq_sets = self.list_stop_sequence_sets(route_id)
            snap["stop_sequence_sets"] = {"n_sets": len(seq_sets), "sets": seq_sets[:10]}
        except Exception as e:
            snap["stop_sequence_sets"] = {"error": str(e)}

        try:
            geom_sets = self.list_geometry_sets(route_id)
            snap["geometry_sets"] = {"n_sets": len(geom_sets), "sets": geom_sets[:10]}
        except Exception as e:
            snap["geometry_sets"] = {"error": str(e)}

        # approvals + prod route (best effort)
        try:
            with db_conn() as conn:
                with db_cursor(conn) as cur:
                    cur.execute(
                        """
                        SELECT route_id, chosen_geometry_candidate_id, chosen_stop_sequence_candidate_id,
                               approved_at, approved_by, notes
                        FROM route_work.route_approvals
                        WHERE route_id=%s
                        """,
                        (str(route_id),),
                    )
                    snap["approval"] = self._row(cur.fetchone()) or None

                    cur.execute(
                        """
                        SELECT route_id, chosen_geometry_candidate_id, source, updated_at,
                               array_length(stop_node_ids, 1) AS n_stop_node_ids,
                               ST_AsText(geom) AS geom_wkt
                        FROM route_prod.routes
                        WHERE route_id=%s
                        """,
                        (str(route_id),),
                    )
                    snap["route_prod"] = self._row(cur.fetchone()) or None
        except Exception as e:
            snap["approval"] = {"error": str(e)}
            snap["route_prod"] = {"error": str(e)}

        return snap

    # -------------------------------------------------------------------------
    # Internal DB helpers
    # -------------------------------------------------------------------------

    def _fetch_stop_points_from_canonical(self, conn, stop_node_ids: List[uuid.UUID]) -> List[LonLat]:
        """
        Returns [(lon,lat), ...] in SAME order as stop_node_ids.
        Pulls from node_prod.nodes (STOP).
        """
        if not stop_node_ids:
            return []

        ids = [str(x) for x in stop_node_ids]
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT node_id, ST_Y(geom) AS lat, ST_X(geom) AS lon
                FROM node_prod.nodes
                WHERE node_type='STOP'
                  AND node_id = ANY(%s::uuid[])
                """,
                (ids,),
            )
            rows = cur.fetchall() or []

        by_id = {uuid.UUID(str(r["node_id"])): (float(r["lon"]), float(r["lat"])) for r in rows}

        missing = [x for x in stop_node_ids if x not in by_id]
        if missing:
            raise ValueError(f"Missing {len(missing)} stop(s) in node_prod.nodes: {missing[:5]}")

        return [by_id[x] for x in stop_node_ids]

    def _fetch_stop_points_from_prior(self, conn, route_id: uuid.UUID, stop_prior_seqs: List[int]) -> List[LonLat]:
        """
        Returns [(lon,lat), ...] in SAME order as stop_prior_seqs.
        Pulls from route_work.relation_stop_prior.
        """
        if not stop_prior_seqs:
            return []

        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT seq, lat, lon
                FROM route_work.relation_stop_prior
                WHERE route_id=%s
                  AND seq = ANY(%s::int[])
                """,
                (str(route_id), stop_prior_seqs),
            )
            rows = cur.fetchall() or []

        by_seq = {int(r["seq"]): (float(r["lon"]), float(r["lat"])) for r in rows}

        missing = [s for s in stop_prior_seqs if s not in by_seq]
        if missing:
            raise ValueError(f"Missing {len(missing)} seq(s) in relation_stop_prior: {missing[:10]}")

        return [by_seq[s] for s in stop_prior_seqs]

    # ==================================================================
    # Sequence Discovery Pipeline
    # ==================================================================

    def run_sequence_discovery(
        self,
        seed_payload: Dict[str, Any],
        *,
        artifact_dir: Optional[str] = None,
        refine_geometry: bool = True,
        run_llm_advisory: bool = False,
        llm_mode: str = "mock",
        valhalla_timeout_s: int = 60,
        buffer_passes: Optional[List[int]] = None,
        scoring_mode: str = "ensemble",
    ) -> Dict[str, Any]:
        """
        Run the full sequence discovery pipeline from a route seed.

        Returns the ConstructorRunSummary dict with all intermediate
        results (grounding, corridor, intersection, skeleton, geometry).
        """
        from datamind_console.phases.phase3_routes.constructor_orchestrator import (
            ConstructorOrchestrator,
        )
        orch = ConstructorOrchestrator()
        return orch.run_sequence_discovery(
            seed_payload,
            artifact_dir=artifact_dir,
            refine_geometry=refine_geometry,
            run_llm_advisory=run_llm_advisory,
            llm_mode=llm_mode,
            valhalla_timeout_s=valhalla_timeout_s,
            buffer_passes=buffer_passes,
            scoring_mode=scoring_mode,
        )

    def ground_stop_hint(
        self,
        hint_text: str,
        *,
        locality_hint: Optional[str] = None,
        operator_id: Optional[int] = None,
        max_results: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Ground a single text hint against the DB stop universe.
        Returns ranked StopMatch dicts.
        """
        from datamind_console.phases.phase3_routes.stop_grounding.stop_grounding_service import (
            ground_hint,
        )
        matches = ground_hint(
            hint_text,
            locality_hint=locality_hint,
            operator_id=operator_id,
            max_results=max_results,
        )
        return [m.to_dict() for m in matches]

    def build_valhalla_corridor(
        self,
        waypoints: List[Dict[str, Any]],
        *,
        timeout_s: int = 60,
    ) -> Dict[str, Any]:
        """
        Build a Valhalla corridor from ordered waypoint dicts
        (each with 'lon', 'lat').
        """
        from datamind_console.phases.phase3_routes.stop_grounding.corridor_builder import (
            rebuild_corridor_with_sequence,
        )
        result = rebuild_corridor_with_sequence(waypoints, timeout_s=timeout_s)
        return result.to_dict()

    def intersect_corridor_stops(
        self,
        corridor_geojson: Dict[str, Any],
        *,
        operator_id: Optional[int] = None,
        buffer_passes: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """
        Intersect a corridor GeoJSON with the DB stop universe.
        Returns ordered corridor candidates.
        """
        from datamind_console.phases.phase3_routes.stop_grounding.corridor_stop_intersector import (
            intersect_corridor_with_stops,
        )
        result = intersect_corridor_with_stops(
            corridor_geojson,
            operator_id=operator_id,
            buffer_passes=buffer_passes,
        )
        return result.to_dict()

    @staticmethod
    def _row(r: Any) -> Dict[str, Any]:
        if not r:
            return {}
        # r is usually dict-like (RealDictCursor)
        out = {}
        for k in r.keys():
            v = r.get(k)
            if isinstance(v, uuid.UUID):
                out[k] = str(v)
            else:
                out[k] = v
        return out
