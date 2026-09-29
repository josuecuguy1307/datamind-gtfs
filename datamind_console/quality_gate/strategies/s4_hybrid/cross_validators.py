"""Cross-validators for Layer 2 (S2-style), copied inline for independence.

Each cross-validator is an independent check on the post-fix shadow entity.
Signature: (issue, fix, shadow_entity) -> (bool, str)
"""
from __future__ import annotations

import re
from typing import Callable, Tuple, Union

from ...shared.config import (
    ECUADOR_BBOX,
    RUNTIME_MAX_MINUTES,
    RUNTIME_MIN_MINUTES,
    haversine_m,
)
from ...shared.models import (
    EntityIssue,
    FixAttempt,
    Route,
    RouteSemantics,
    ScheduleProfile,
    Shape,
    Stop,
)

Entity = Union[Stop, Route, Shape, RouteSemantics, ScheduleProfile]
CrossValidator = Callable[[EntityIssue, FixAttempt, Entity], Tuple[bool, str]]

URBAN_MEDIAN_SPEED_KMH = 25.0
RUNTIME_TOLERANCE = 0.15

GENERIC_TOKENS = frozenset({
    "street", "road", "avenue", "lane", "drive", "way", "path", "trail",
    "calle", "avenida", "pasaje", "via", "camino", "carrera", "sendero",
    "transversal", "diagonal", "autopista", "paseo", "callejon",
    "s/n", "sn", "sin nombre", "unnamed", "unknown",
})


# === STOP ===

def xv_reverse_geocode(issue, fix, shadow) -> Tuple[bool, str]:
    name = fix.new_value
    if isinstance(name, dict):
        name = name.get("name", "")
    if not isinstance(name, str) or len(name.strip()) < 5:
        return False, f"Name too short: {name!r}"
    normalized = name.lower().strip()
    if normalized in GENERIC_TOKENS:
        return False, f"Generic street type only: {name!r}"
    if not any(c.isalpha() for c in name):
        return False, f"Name has no alphabetic characters: {name!r}"
    return True, "ok"


def xv_swapped_coords(issue, fix, shadow) -> Tuple[bool, str]:
    if not isinstance(fix.new_value, tuple) or len(fix.new_value) != 2:
        return False, f"Invalid coord format: {fix.new_value!r}"
    new_lat, new_lon = fix.new_value
    min_lat, min_lon, max_lat, max_lon = ECUADOR_BBOX
    if not (min_lat <= new_lat <= max_lat and min_lon <= new_lon <= max_lon):
        return False, f"Swapped coords still outside Ecuador bbox"
    if new_lon > -75.0:
        return False, f"Longitude {new_lon} too far east for continental Ecuador"
    return True, "ok"


def xv_null_island(issue, fix, shadow) -> Tuple[bool, str]:
    return False, "Null-island coords cannot be auto-fixed"


def xv_garbage_ref(issue, fix, shadow) -> Tuple[bool, str]:
    return False, "Garbage stop ref cannot be auto-fixed"


def xv_merged_stop(issue, fix, shadow) -> Tuple[bool, str]:
    val = fix.new_value
    if isinstance(val, dict) and "__merge_remove__" in val:
        orig = issue.original_value
        if isinstance(orig, dict):
            dist = orig.get("distance_m", float("inf"))
            if dist > 25.0:
                return False, f"Merge distance {dist:.1f}m exceeds cross-validator threshold 25m"
        return True, "ok"
    if not isinstance(val, dict) or "keep" not in val:
        return False, f"Invalid merge result: {val!r}"
    orig = issue.original_value
    if isinstance(orig, dict):
        dist = orig.get("distance_m", float("inf"))
        if dist > 25.0:
            return False, f"Merge distance {dist:.1f}m exceeds cross-validator threshold 25m"
    if isinstance(shadow, Stop):
        min_lat, min_lon, max_lat, max_lon = ECUADOR_BBOX
        if not (min_lat <= shadow.lat <= max_lat and min_lon <= shadow.lon <= max_lon):
            return False, f"Kept stop coords outside Ecuador"
    return True, "ok"


# === ROUTE ===

def xv_merged_route_fragment(issue, fix, shadow) -> Tuple[bool, str]:
    if not isinstance(fix.new_value, dict):
        return False, f"Invalid merge result: {fix.new_value!r}"
    if "merge_into" in fix.new_value:
        overlap = fix.new_value.get("overlap_ratio", 0)
        if overlap < 0.3:
            return False, f"Merge overlap ratio {overlap:.2f} too low"
        return True, "ok"
    if "clone_from" in fix.new_value:
        if fix.confidence < 0.5:
            return False, f"Clone confidence {fix.confidence:.2f} below 0.5"
        return True, "ok"
    return False, f"Unknown merge strategy: {fix.new_value!r}"


def xv_no_schedule(issue, fix, shadow) -> Tuple[bool, str]:
    return False, "Missing schedule cannot be auto-generated"


# === SHAPE ===

def xv_shape_retrace(issue, fix, shadow) -> Tuple[bool, str]:
    if not isinstance(fix.new_value, dict):
        return False, f"Invalid retrace result: {fix.new_value!r}"
    gap_km = fix.new_value.get("gap_km", 0)
    if gap_km <= 0:
        return False, f"Invalid gap distance: {gap_km}"
    if gap_km > 20.0:
        return False, f"Gap {gap_km:.1f}km too large for auto re-trace"
    from_coord = fix.new_value.get("from_coord", (0, 0))
    to_coord = fix.new_value.get("to_coord", (0, 0))
    if from_coord == (0, 0) or to_coord == (0, 0):
        return False, "Re-trace endpoints contain null-island coordinates"
    return True, "ok"


def xv_shape_self_intersection(issue, fix, shadow) -> Tuple[bool, str]:
    return False, "Shape self-intersection requires Phase 3 reconstruction"


# === NAMING ===

def xv_reconstructed_route_name(issue, fix, shadow) -> Tuple[bool, str]:
    name = fix.new_value
    if not isinstance(name, str) or len(name.strip()) < 5:
        return False, f"Reconstructed name too short: {name!r}"
    stripped = name.strip()
    if stripped.isdigit():
        return False, f"Reconstructed name is still pure numeric"
    if stripped == str(issue.original_value).strip():
        return False, "Reconstructed name identical to original garbage"
    words = re.findall(r'[a-zA-Z\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1\u00c1\u00c9\u00cd\u00d3\u00da\u00d1]{3,}', name)
    if not words:
        return False, f"Reconstructed name has no meaningful words: {name!r}"
    return True, "ok"


def xv_short_name_dedup(issue, fix, shadow) -> Tuple[bool, str]:
    name = fix.new_value
    if not isinstance(name, str):
        return False, f"Invalid short name: {name!r}"
    if name == issue.original_value:
        return False, "Deduplicated name is identical to original"
    if len(name) > 12:
        return False, f"Deduplicated name too long ({len(name)} chars)"
    if not re.search(r'-[A-Z0-9]+$', name):
        return False, f"No direction suffix in deduplicated name: {name!r}"
    return True, "ok"


def xv_canonical_operator(issue, fix, shadow) -> Tuple[bool, str]:
    name = fix.new_value
    if not isinstance(name, str) or len(name.strip()) < 3:
        return False, f"Canonical operator name too short: {name!r}"
    if not any(c.isalpha() for c in name):
        return False, f"Canonical operator has no alphabetic chars"
    if name.strip() == str(issue.original_value).strip():
        return False, "Canonical form identical to flagged form"
    return True, "ok"


# === TIMING ===

def xv_clamped_runtime(issue, fix, shadow) -> Tuple[bool, str]:
    clamped = fix.new_value
    if not isinstance(clamped, (int, float)):
        return False, f"Invalid clamped value: {clamped!r}"
    if clamped < RUNTIME_MIN_MINUTES or clamped > RUNTIME_MAX_MINUTES:
        return False, f"Clamped value {clamped} still outside bounds"
    if isinstance(shadow, ScheduleProfile):
        route_length_km = shadow.extra.get("route_length_km")
        if route_length_km and route_length_km > 0:
            expected_min = (route_length_km / URBAN_MEDIAN_SPEED_KMH) * 60.0
            lo = expected_min * (1 - RUNTIME_TOLERANCE)
            hi = expected_min * (1 + RUNTIME_TOLERANCE)
            if not (lo <= clamped <= hi):
                return False, f"Clamped runtime {clamped:.1f}min outside expected [{lo:.1f}, {hi:.1f}]"
    return True, "ok"


def xv_default_calendar(issue, fix, shadow) -> Tuple[bool, str]:
    return False, "Default calendar must not be auto-committed"


# === REGISTRY ===

CROSS_VALIDATORS: dict[str, CrossValidator] = {
    "stop_name_placeholder": xv_reverse_geocode,
    "stop_name_empty": xv_reverse_geocode,
    "stop_name_uuid_prefix": xv_reverse_geocode,
    "stop_coords_outside_bbox": xv_swapped_coords,
    "stop_coords_null_island": xv_null_island,
    "stop_ref_garbage": xv_garbage_ref,
    "stop_duplicate_nearby": xv_merged_stop,
    "route_too_few_stops": xv_merged_route_fragment,
    "route_no_schedule": xv_no_schedule,
    "shape_gap_too_large": xv_shape_retrace,
    "shape_self_intersection": xv_shape_self_intersection,
    "route_name_garbage": xv_reconstructed_route_name,
    "short_name_collision": xv_short_name_dedup,
    "operator_inconsistency": xv_canonical_operator,
    "unrealistic_runtime": xv_clamped_runtime,
    "calendar_no_active_days": xv_default_calendar,
}
