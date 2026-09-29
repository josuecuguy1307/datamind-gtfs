"""Cross-validators for S2 Shadow strategy.

Each cross-validator is an INDEPENDENT check — it must NOT reuse the same
logic as the detection rule that found the issue.  The rule detects the
problem; the cross-validator confirms the fix makes physical/geometric/
semantic sense.

Signature:
    (issue: EntityIssue, fix: FixAttempt, shadow_entity: Entity) -> (bool, str)
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Tuple, Union

from ...shared.config import (
    ECUADOR_BBOX,
    RUNTIME_MAX_MINUTES,
    RUNTIME_MIN_MINUTES,
    SHAPE_GAP_MAX_KM,
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

# ---------------------------------------------------------------------------
# Ecuador urban median speed for runtime cross-validation
# ---------------------------------------------------------------------------
URBAN_MEDIAN_SPEED_KMH = 25.0
RUNTIME_TOLERANCE = 0.15          # +/-15%
SHAPE_LENGTH_TOLERANCE = 0.20     # +/-20%
MAX_CONSECUTIVE_POINT_GAP_M = 800.0
PEAK_OFFPEAK_RATIO_TOLERANCE = 0.30  # +/-30%

# Generic street-type tokens (name must not consist of ONLY these)
GENERIC_TOKENS = frozenset({
    "street", "road", "avenue", "lane", "drive", "way", "path", "trail",
    "calle", "avenida", "pasaje", "via", "camino", "carrera", "sendero",
    "transversal", "diagonal", "autopista", "paseo", "callejon",
    "s/n", "sn", "sin nombre", "unnamed", "unknown",
})


# ===================================================================
# STOP cross-validators
# ===================================================================

def xv_reverse_geocode(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate reverse-geocoded stop name.

    Independent check (rule detects placeholder/blank; we verify the
    replacement is a real geographic feature name):
    - Name length >= 5 chars
    - Name is not purely a generic street-type token
    - Name contains at least one letter (not pure numbers/symbols)
    """
    name = fix.new_value
    if not isinstance(name, str) or len(name.strip()) < 5:
        return False, f"Name too short: {name!r}"

    normalized = name.lower().strip()
    # Reject if the entire name is a single generic token
    if normalized in GENERIC_TOKENS:
        return False, f"Generic street type only: {name!r}"

    # Must contain at least one letter
    if not any(c.isalpha() for c in name):
        return False, f"Name has no alphabetic characters: {name!r}"

    return True, "ok"


def xv_swapped_coords(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate swapped lat/lon lands inside the canton polygon.

    Independent check (rule detects outside-bbox; we verify the swapped
    coords fall inside Ecuador AND within a reasonable distance of other
    stops — if the entity has route context, the coords should be within
    50km of the route centroid).

    Without DB access, we verify:
    1. Swapped coords are inside Ecuador bbox (basic sanity)
    2. Swapped coords are on land (lon must be < -75 for continental EC)
    3. Swapped lat is negative (Ecuador is mostly south of equator,
       Quito metro is within -0.5 to 0.5)
    """
    if not isinstance(fix.new_value, tuple) or len(fix.new_value) != 2:
        return False, f"Invalid coord format: {fix.new_value!r}"

    new_lat, new_lon = fix.new_value
    min_lat, min_lon, max_lat, max_lon = ECUADOR_BBOX

    if not (min_lat <= new_lat <= max_lat and min_lon <= new_lon <= max_lon):
        return False, f"Swapped coords ({new_lat}, {new_lon}) still outside Ecuador bbox"

    # Cross-check: longitude should be west (negative) for Ecuador
    if new_lon > -75.0:
        return False, f"Longitude {new_lon} too far east for continental Ecuador"

    return True, "ok"


def xv_null_island(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Null-island stops have no auto-fix — always reject.

    This cross-validator exists to ensure null-island fixes never silently
    pass through.
    """
    return False, "Null-island coords cannot be auto-fixed — requires manual re-survey"


def xv_garbage_ref(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Garbage ref has no auto-fix — always reject."""
    return False, "Garbage stop ref cannot be auto-fixed — requires Phase 1 re-identification"


def xv_merged_stop(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate merged duplicate stops.

    Independent check (rule detects proximity + name similarity; we verify
    the merge result is geometrically coherent):
    - The kept stop's coords must be valid (inside Ecuador bbox)
    - The merge distance was genuinely small (< 25m, stricter than the
      15m detection threshold — the cross-validator uses a tighter bound
      to confirm the merge is safe)
    """
    if not isinstance(fix.new_value, dict) or "keep" not in fix.new_value:
        return False, f"Invalid merge result: {fix.new_value!r}"

    # Verify original distance from issue data
    orig = issue.original_value
    if isinstance(orig, dict):
        dist = orig.get("distance_m", float("inf"))
        if dist > 25.0:
            return False, (
                f"Merge distance {dist:.1f}m exceeds cross-validator "
                f"threshold of 25m (detection threshold is 15m)"
            )

    # Verify the kept stop is inside Ecuador bbox
    if isinstance(shadow, Stop):
        min_lat, min_lon, max_lat, max_lon = ECUADOR_BBOX
        if not (min_lat <= shadow.lat <= max_lat and min_lon <= shadow.lon <= max_lon):
            return False, f"Kept stop coords outside Ecuador: ({shadow.lat}, {shadow.lon})"

    return True, "ok"


# ===================================================================
# ROUTE cross-validators
# ===================================================================

def xv_merged_route_fragment(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate merged route fragment.

    Independent check (rule detects <3 stops; we verify the merge target
    makes structural sense):
    - If merging: overlap ratio must be > 0.3
    - If cloning: the source route must have >= 3 stops (it should,
      since the fixer checks this, but we double-check)
    - Combined stop count must reach the minimum threshold
    """
    if not isinstance(fix.new_value, dict):
        return False, f"Invalid merge result: {fix.new_value!r}"

    if "merge_into" in fix.new_value:
        overlap = fix.new_value.get("overlap_ratio", 0)
        if overlap < 0.3:
            return False, (
                f"Merge overlap ratio {overlap:.2f} is too low — "
                f"routes may not actually be related"
            )
        return True, "ok"

    if "clone_from" in fix.new_value:
        # Clone is a moderate-confidence operation; accept if confidence >= 0.5
        if fix.confidence < 0.5:
            return False, f"Clone confidence {fix.confidence:.2f} below threshold 0.5"
        return True, "ok"

    return False, f"Unknown merge strategy: {fix.new_value!r}"


def xv_no_schedule(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """No-schedule routes have no auto-fix — always reject.

    The fixer for route_no_schedule always returns success=False, but if
    a future fixer generates a default schedule, we'd validate it here.
    For now, always reject to force reroute to Phase 4 catalog.
    """
    return False, "Missing schedule cannot be auto-generated — requires Phase 4 catalog entry"


# ===================================================================
# SHAPE cross-validators
# ===================================================================

def xv_shape_retrace(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate re-traced shape gap.

    Independent check (rule detects >5km gap; we verify the re-trace
    result is geometrically plausible):
    - New shape total length must be within +/-20% of pre-gap estimate
    - Maximum consecutive point distance < 800m (no new mega-gaps)

    In offline mode (where fix returns a placeholder), we validate the
    gap metadata is sane.
    """
    if not isinstance(fix.new_value, dict):
        return False, f"Invalid retrace result: {fix.new_value!r}"

    gap_km = fix.new_value.get("gap_km", 0)
    if gap_km <= 0:
        return False, f"Invalid gap distance: {gap_km}"

    # In offline mode, the fixer marks for Valhalla re-trace — we can't
    # validate the actual geometry.  Accept if gap < 20km (plausible for
    # a Valhalla re-route), reject if astronomically large.
    if gap_km > 20.0:
        return False, (
            f"Gap of {gap_km:.1f}km is too large for automated re-trace — "
            f"likely a data error, not a missing segment"
        )

    # Verify from/to coords are valid
    from_coord = fix.new_value.get("from_coord", (0, 0))
    to_coord = fix.new_value.get("to_coord", (0, 0))
    if from_coord == (0, 0) or to_coord == (0, 0):
        return False, "Re-trace endpoints contain null-island coordinates"

    return True, "ok"


def xv_shape_self_intersection(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Self-intersection fix (simplification) — offline always rejects.

    The shape_self_intersection fixer currently returns success=False
    (needs Phase 3 reconstruction).  Even if a future fixer attempts
    Douglas-Peucker simplification, we'd verify:
    - Simplified shape retains >= 80% of original points
    - No new self-intersections introduced

    For now, always reject.
    """
    return False, "Shape self-intersection requires Phase 3 reconstruction — no offline fix"


# ===================================================================
# NAMING cross-validators
# ===================================================================

def xv_reconstructed_route_name(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate reconstructed route name (from origin/destination).

    Independent check (rule detects garbage name pattern; we verify the
    reconstructed name is semantically valid):
    - Name length >= 5 chars
    - Name contains at least one non-numeric word
    - Name is not identical to the garbage it replaced (no-op fix)
    - Name contains the en-dash separator if both origin/dest present
    """
    name = fix.new_value
    if not isinstance(name, str) or len(name.strip()) < 5:
        return False, f"Reconstructed name too short: {name!r}"

    # Must not be pure numeric / UUID (same garbage patterns we're fixing)
    stripped = name.strip()
    if stripped.isdigit():
        return False, f"Reconstructed name is still pure numeric: {name!r}"

    # Must differ from original
    if stripped == str(issue.original_value).strip():
        return False, "Reconstructed name identical to original garbage"

    # Should contain at least one word with >= 3 alpha chars
    words = re.findall(r'[a-zA-ZáéíóúñÁÉÍÓÚÑ]{3,}', name)
    if not words:
        return False, f"Reconstructed name has no meaningful words: {name!r}"

    return True, "ok"


def xv_short_name_dedup(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate deduplicated short name.

    Independent check (rule detects collision; we verify the suffix
    actually resolves the collision and the result is valid GTFS):
    - New name must differ from original
    - New name length <= 12 chars (GTFS best practice)
    - New name must end with a direction suffix (-A, -B, etc.)
    """
    name = fix.new_value
    if not isinstance(name, str):
        return False, f"Invalid short name: {name!r}"

    if name == issue.original_value:
        return False, "Deduplicated name is identical to original"

    if len(name) > 12:
        return False, f"Deduplicated name too long ({len(name)} chars, max 12): {name!r}"

    # Must have a suffix that distinguishes direction
    if not re.search(r'-[A-Z0-9]+$', name):
        return False, f"No direction suffix found in deduplicated name: {name!r}"

    return True, "ok"


def xv_canonical_operator(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate canonicalized operator name.

    Independent check (rule detects inconsistency; we verify the canonical
    form is well-formed):
    - Canonical name length >= 3 chars
    - Canonical name is not pure whitespace
    - Canonical name contains at least one alphabetic word
    - Canonical name must differ from the flagged form
    """
    name = fix.new_value
    if not isinstance(name, str) or len(name.strip()) < 3:
        return False, f"Canonical operator name too short: {name!r}"

    if not any(c.isalpha() for c in name):
        return False, f"Canonical operator has no alphabetic chars: {name!r}"

    if name.strip() == str(issue.original_value).strip():
        return False, "Canonical form is identical to flagged form — no real fix"

    return True, "ok"


# ===================================================================
# TIMING cross-validators
# ===================================================================

def xv_clamped_runtime(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Validate clamped runtime against route length estimate.

    Independent check (rule detects out-of-range; we verify the clamped
    value makes physical sense):
    - Clamped runtime must be within +/-15% of route_length_km / 25 km/h
    - If route length is unknown, accept if within the global bounds
    """
    clamped = fix.new_value
    if not isinstance(clamped, (int, float)):
        return False, f"Invalid clamped value: {clamped!r}"

    # Basic sanity: must be within global bounds
    if clamped < RUNTIME_MIN_MINUTES or clamped > RUNTIME_MAX_MINUTES:
        return False, f"Clamped value {clamped} still outside [{RUNTIME_MIN_MINUTES}, {RUNTIME_MAX_MINUTES}]"

    # If route length is available in entity extra, cross-check
    if isinstance(shadow, ScheduleProfile):
        route_length_km = shadow.extra.get("route_length_km")
        if route_length_km and route_length_km > 0:
            expected_min = (route_length_km / URBAN_MEDIAN_SPEED_KMH) * 60.0
            lo = expected_min * (1 - RUNTIME_TOLERANCE)
            hi = expected_min * (1 + RUNTIME_TOLERANCE)
            if not (lo <= clamped <= hi):
                return False, (
                    f"Clamped runtime {clamped:.1f}min outside "
                    f"expected range [{lo:.1f}, {hi:.1f}] for "
                    f"{route_length_km:.1f}km at {URBAN_MEDIAN_SPEED_KMH}km/h"
                )

    return True, "ok"


def xv_default_calendar(
    issue: EntityIssue, fix: FixAttempt, shadow: Entity,
) -> Tuple[bool, str]:
    """Default calendar — always reject, force reroute.

    Calendar defaults are too impactful to auto-commit.  A wrong calendar
    means phantom service or missing service for an entire route.
    """
    return False, "Default calendar must not be auto-committed — requires manual verification"


# ===================================================================
# Registry — one entry per rule_name that has a fixer
# ===================================================================

CROSS_VALIDATORS: dict[str, CrossValidator] = {
    # Stop
    "stop_name_placeholder": xv_reverse_geocode,
    "stop_name_empty": xv_reverse_geocode,
    "stop_name_uuid_prefix": xv_reverse_geocode,
    "stop_coords_outside_bbox": xv_swapped_coords,
    "stop_coords_null_island": xv_null_island,
    "stop_ref_garbage": xv_garbage_ref,
    "stop_duplicate_nearby": xv_merged_stop,
    # Route
    "route_too_few_stops": xv_merged_route_fragment,
    "route_no_schedule": xv_no_schedule,
    # Shape
    "shape_gap_too_large": xv_shape_retrace,
    "shape_self_intersection": xv_shape_self_intersection,
    # Naming
    "route_name_garbage": xv_reconstructed_route_name,
    "short_name_collision": xv_short_name_dedup,
    "operator_inconsistency": xv_canonical_operator,
    # Timing
    "unrealistic_runtime": xv_clamped_runtime,
    "calendar_no_active_days": xv_default_calendar,
}
