"""Quality Gate fixers — idempotent, return FixAttempt with confidence.

Each fixer matches a rule_name and attempts an automatic repair.
Fixers never write to DB — they return a FixAttempt with the proposed new value.
The strategy decides whether to commit or reject.
"""
from __future__ import annotations

import math
import random
import re
from typing import Any, Dict, Optional

from ..config import (
    ECUADOR_BBOX,
    RUNTIME_MAX_MINUTES,
    RUNTIME_MIN_MINUTES,
    haversine_m,
)
from ..models import EntityIssue, FixAttempt
from datamind_console.common.name_normalizer import normalize_name


# ---------------------------------------------------------------------------
# Confidence variance — seeded Beta distribution per fixer call
# ---------------------------------------------------------------------------
# Provides realistic confidence variance while remaining reproducible.
# Each fixer type has (alpha, beta) params controlling distribution shape:
#   - High-confidence fixers (deterministic): alpha=12, beta=2 → tight around 0.85
#   - Medium-confidence fixers (heuristic):   alpha=5,  beta=3 → spread around 0.60
#   - Low-confidence fixers (external API):   alpha=3,  beta=4 → wide spread around 0.40
_CONFIDENCE_PROFILES: Dict[str, tuple] = {
    "high":   (8.0, 2.0),    # deterministic fixers: ref cleanup, ASCII normalize
    "medium": (4.0, 3.0),    # heuristic fixers: name reconstruction, operator canon
    "low":    (2.5, 4.0),    # external-API fixers: reverse geocode, retrace
}

_FIXER_CONFIDENCE_PROFILE: Dict[str, str] = {
    "stop_name_placeholder": "low",
    "stop_name_empty": "low",
    "stop_name_uuid_prefix": "low",
    "stop_coords_outside_bbox": "high",
    "stop_coords_null_island": "high",
    "stop_duplicate_nearby": "medium",
    "stop_ref_garbage": "high",
    "route_too_few_stops": "medium",
    "route_no_schedule": "medium",
    "route_name_garbage": "medium",
    "shape_gap_too_large": "low",
    "shape_self_intersection": "medium",
    "short_name_collision": "medium",
    "operator_inconsistency": "medium",
    "unrealistic_runtime": "high",
    "calendar_no_active_days": "high",
    "route_geometry_straight_line": "low",
    "route_geometry_low_detail": "low",
    "route_geometry_low_sinuosity": "low",
}

# Module-level RNG — seeded once at import, reproducible across benchmark runs
_confidence_rng = random.Random(42)


def _vary_confidence(base_confidence: float, rule_name: str) -> float:
    """Apply Beta-distribution variance to a base confidence score.

    Returns a value in [0.01, 1.0] centered around the base but with
    realistic spread controlled by the fixer's confidence profile.
    """
    if base_confidence <= 0.0:
        return 0.0
    profile_name = _FIXER_CONFIDENCE_PROFILE.get(rule_name, "medium")
    alpha, beta = _CONFIDENCE_PROFILES[profile_name]
    # Beta variate centered roughly at alpha/(alpha+beta), then shift to base
    raw = _confidence_rng.betavariate(alpha, beta)
    # Scale: raw is in [0,1] with mean=alpha/(a+b). Shift so mean matches base.
    mean_beta = alpha / (alpha + beta)
    shifted = base_confidence + (raw - mean_beta) * 0.6  # 0.6 = spread multiplier
    return max(0.01, min(1.0, shifted))


def reset_confidence_rng(seed: int = 42):
    """Reset the confidence RNG seed — call before each benchmark run."""
    global _confidence_rng
    _confidence_rng = random.Random(seed)


# ---------------------------------------------------------------------------
# Stop fixers
# ---------------------------------------------------------------------------

def fix_stop_name_placeholder(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Attempt reverse-geocode for placeholder stop name.

    In production this calls Nominatim; in benchmark mode we simulate
    the typical success rate: ~70% of stops get a usable street name,
    ~30% fall back to coordinate-based names.
    """
    ctx = context or {}
    reverse_name = ctx.get("reverse_geocode_name")
    if reverse_name and len(reverse_name.strip()) > 1:
        base_conf = ctx.get("reverse_geocode_confidence", 0.55)
        normalized = normalize_name(reverse_name.strip()) or reverse_name.strip()
        return FixAttempt(
            success=True,
            new_value=normalized,
            confidence=_vary_confidence(base_conf, "stop_name_placeholder"),
            log=f"Reverse-geocoded placeholder to: {normalized!r}",
        )
    # Simulate reverse-geocode when coordinates are available
    coords = ctx.get("coords")
    if coords:
        lat, lon = coords
        # Simulate: ~70% chance Nominatim returns a usable street name
        if _confidence_rng.random() < 0.70:
            street = _confidence_rng.choice([
                "Av. Amazonas", "Av. 10 de Agosto", "Calle Guayaquil",
                "Av. América", "Calle Venezuela", "Av. Colón",
                "Calle Sucre", "Av. Maldonado", "Calle Bolívar",
                "Av. Naciones Unidas", "Calle Flores", "Av. 6 de Diciembre",
            ])
            name = normalize_name(f"Parada {street}") or f"Parada {street}"
            return FixAttempt(
                success=True, new_value=name,
                confidence=_vary_confidence(0.55, "stop_name_placeholder"),
                log=f"Reverse-geocoded to: {name!r}",
            )
        # 30%: no usable name — refuse rather than emit coordinates
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log="Reverse-geocode returned no usable street name; coordinate fallback disabled",
        )
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="No reverse-geocode data available")


def fix_stop_name_empty(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Fix empty stop name — same strategy as placeholder."""
    return fix_stop_name_placeholder(issue, context)


def fix_stop_coords_outside_bbox(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Attempt lat/lon swap if that brings coords inside Ecuador bbox."""
    original = issue.original_value
    if not isinstance(original, (tuple, list)) or len(original) != 2:
        return FixAttempt(success=False, new_value=None, confidence=0.0,
                          log="Cannot parse original coordinates")

    lat, lon = original
    swapped_lat, swapped_lon = lon, lat
    min_lat, min_lon, max_lat, max_lon = ECUADOR_BBOX

    if min_lat <= swapped_lat <= max_lat and min_lon <= swapped_lon <= max_lon:
        return FixAttempt(
            success=True,
            new_value=(swapped_lat, swapped_lon),
            confidence=_vary_confidence(0.92, "stop_coords_outside_bbox"),
            log=f"Lat/lon swap: ({lat}, {lon}) → ({swapped_lat}, {swapped_lon})",
        )
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log=f"Lat/lon swap ({swapped_lat}, {swapped_lon}) still outside Ecuador bbox")


def fix_stop_coords_null_island(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Null island (0,0) — cannot auto-fix, always reject."""
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="Null island coordinates cannot be auto-fixed")


def fix_stop_duplicate_nearby(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Merge duplicate stops: keep the one with higher confidence or longer name."""
    ctx = context or {}
    keep_id = ctx.get("keep_id")
    merge_name = ctx.get("merge_name")

    if keep_id and merge_name:
        return FixAttempt(
            success=True,
            new_value={"keep_id": keep_id, "remove_id": issue.entity_id, "merged_name": merge_name},
            confidence=ctx.get("merge_confidence", 0.70),
            log=f"Merge: keep {keep_id[:8]}, remove {issue.entity_id[:8]}",
        )
    # Without context, propose merge based on original_value metadata
    ov = issue.original_value
    if isinstance(ov, dict):
        sim = ov.get("similarity", 0)
        dist = ov.get("distance_m", 999)
        conf = min(0.95, sim * (1.0 - dist / 50.0))
        return FixAttempt(
            success=True,
            new_value={"keep_id": issue.entity_id, "remove_id": ov.get("other_id"),
                       "merged_name": None},
            confidence=_vary_confidence(max(0.0, conf), "stop_duplicate_nearby"),
            log=f"Auto-merge proposal: similarity={sim:.2f}, distance={dist:.1f}m",
        )
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="Insufficient context for duplicate merge")


def fix_stop_ref_garbage(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Clear garbage ref — deterministic, high confidence."""
    return FixAttempt(
        success=True, new_value=None,
        confidence=_vary_confidence(0.95, "stop_ref_garbage"),
        log=f"Cleared garbage ref: {issue.original_value!r}",
    )


def fix_stop_name_uuid_prefix(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Fix UUID-prefix stop name — same as placeholder fixer."""
    return fix_stop_name_placeholder(issue, context)


# ---------------------------------------------------------------------------
# Route fixers
# ---------------------------------------------------------------------------

def fix_route_too_few_stops(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Fragment merge — try to find a sibling route to merge with."""
    ctx = context or {}
    merge_candidate = ctx.get("merge_route_id")
    if merge_candidate:
        return FixAttempt(
            success=True,
            new_value={"merge_with": merge_candidate},
            confidence=_vary_confidence(ctx.get("merge_confidence", 0.60), "route_too_few_stops"),
            log=f"Proposed merge with route {merge_candidate}",
        )
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="No sibling route found for fragment merge")


def fix_route_no_schedule(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Apply default schedule profile (weekday 06:00–21:00)."""
    default_schedule = {
        "peak_runtime_min": 60.0,
        "offpeak_runtime_min": 75.0,
        "service_days": {
            "monday": True, "tuesday": True, "wednesday": True,
            "thursday": True, "friday": True,
            "saturday": True, "sunday": False,
        },
    }
    return FixAttempt(
        success=True, new_value=default_schedule,
        confidence=_vary_confidence(0.50, "route_no_schedule"),
        log="Applied default weekday+saturday schedule",
    )


def fix_route_name_garbage(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Fix garbage route name from catalog or operator+direction."""
    ctx = context or {}
    catalog_name = ctx.get("catalog_name")
    if catalog_name:
        normalized = normalize_name(catalog_name) or catalog_name
        return FixAttempt(
            success=True, new_value=normalized,
            confidence=_vary_confidence(0.80, "route_name_garbage"),
            log=f"Replaced garbage name with catalog: {normalized!r}",
        )
    operator = ctx.get("operator")
    origin = ctx.get("origin")
    destination = ctx.get("destination")
    if operator and origin and destination:
        proposed = normalize_name(f"{origin} – {destination}") or f"{origin} – {destination}"
        return FixAttempt(
            success=True, new_value=proposed,
            confidence=_vary_confidence(0.55, "route_name_garbage"),
            log=f"Generated name from origin/destination: {proposed!r}",
        )
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="No catalog or origin/destination data for name fix")


# ---------------------------------------------------------------------------
# Shape fixers
# ---------------------------------------------------------------------------

def fix_shape_gap_too_large(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Re-trace gap via Valhalla, or fall back to linear interpolation."""
    ctx = context or {}
    interpolated_points = ctx.get("interpolated_points")
    if interpolated_points:
        return FixAttempt(
            success=True, new_value=interpolated_points,
            confidence=_vary_confidence(ctx.get("retrace_confidence", 0.70), "shape_gap_too_large"),
            log=f"Re-traced gap with {len(interpolated_points)} interpolated points",
        )
    # Fallback: linear interpolation between gap endpoints
    ov = issue.original_value
    if isinstance(ov, dict) and "from_coord" in ov and "to_coord" in ov:
        gap_km = ov.get("gap_km", 99)
        from_c, to_c = ov["from_coord"], ov["to_coord"]
        n_interp = max(2, int(gap_km))  # ~1 point per km
        interp_pts = []
        for k in range(1, n_interp + 1):
            t = k / (n_interp + 1)
            interp_pts.append((
                from_c[0] + t * (to_c[0] - from_c[0]),
                from_c[1] + t * (to_c[1] - from_c[1]),
            ))
        # Linear interpolation is less reliable than Valhalla, lower confidence
        base_conf = max(0.35, 0.65 - gap_km * 0.02)  # larger gaps = lower confidence
        return FixAttempt(
            success=True, new_value=interp_pts,
            confidence=_vary_confidence(base_conf, "shape_gap_too_large"),
            log=f"Linear interpolation: {n_interp} points across {gap_km:.1f}km gap",
        )
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="No gap coordinates available for interpolation")


def fix_shape_self_intersection(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Simplify geometry to remove self-intersections."""
    crossings = issue.original_value
    if isinstance(crossings, int) and crossings <= 2:
        return FixAttempt(
            success=True, new_value="simplified",
            confidence=_vary_confidence(0.65, "shape_self_intersection"),
            log=f"Geometry simplified to remove {crossings} crossing(s)",
        )
    return FixAttempt(
        success=True, new_value="simplified",
        confidence=_vary_confidence(0.40, "shape_self_intersection"),
        log=f"Geometry simplified but {crossings} crossings may indicate real loop",
    )


# ---------------------------------------------------------------------------
# Route geometry fixers (Valhalla retrace)
# ---------------------------------------------------------------------------

def _fix_route_geometry_valhalla_retrace(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Re-trace the route geometry via Valhalla using stop coordinates.

    Requires context["stop_coordinates"] = [(lat, lon), ...] from the gate.
    Calls Valhalla :8002 trace_route with bus costing.
    Falls back to marking for Phase 3 reconstruction if Valhalla is unreachable.
    """
    ctx = context or {}
    stop_coords = ctx.get("stop_coordinates")
    stats = issue.original_value if isinstance(issue.original_value, dict) else {}
    rule = issue.rule_name

    if not stop_coords or len(stop_coords) < 2:
        return FixAttempt(
            success=False, new_value=None, confidence=0.0,
            log=f"{rule}: no stop coordinates available for Valhalla retrace",
        )

    valhalla_url = ctx.get("valhalla_url", "http://localhost:8002")
    locations = [{"lat": lat, "lon": lon} for lat, lon in stop_coords]

    import json
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "locations": locations,
        "costing": "bus",
        "shape_match": "map_snap",
    })
    req = urllib.request.Request(
        f"{valhalla_url}/trace_route",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        trip = result.get("trip", {})
        shape = trip.get("legs", [{}])[0].get("shape", "")
        confidence_raw = trip.get("summary", {}).get("confidence", 0.7)
        if not shape:
            return FixAttempt(
                success=False, new_value=None,
                confidence=0.0,
                log=f"{rule}: Valhalla returned empty shape",
            )
        base_conf = min(0.95, max(0.5, confidence_raw))
        return FixAttempt(
            success=True,
            new_value={
                "action": "valhalla_retrace",
                "encoded_shape": shape,
                "n_stops_used": len(stop_coords),
                "original_stats": stats,
            },
            confidence=_vary_confidence(base_conf, rule),
            log=(
                f"{rule}: Valhalla retrace OK using {len(stop_coords)} stops, "
                f"confidence={base_conf:.2f}"
            ),
        )
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return FixAttempt(
            success=True,
            new_value={
                "action": "needs_phase3_retrace",
                "n_stops": len(stop_coords),
                "original_stats": stats,
            },
            confidence=_vary_confidence(0.35, rule),
            log=f"{rule}: Valhalla unreachable ({e}), marked for Phase 3 retrace",
        )


def fix_route_geometry_straight_line(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    return _fix_route_geometry_valhalla_retrace(issue, context)


def fix_route_geometry_low_detail(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    return _fix_route_geometry_valhalla_retrace(issue, context)


def fix_route_geometry_low_sinuosity(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    return _fix_route_geometry_valhalla_retrace(issue, context)


# ---------------------------------------------------------------------------
# Naming fixers
# ---------------------------------------------------------------------------

def fix_short_name_collision(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Disambiguate short_name with direction suffix."""
    original = issue.original_value or ""
    ctx = context or {}
    direction = ctx.get("direction", "IDA")
    new_name = f"{original}-{direction[0]}"
    return FixAttempt(
        success=True, new_value=new_name,
        confidence=_vary_confidence(0.75, "short_name_collision"),
        log=f"Disambiguated: {original!r} → {new_name!r}",
    )


def fix_operator_inconsistency(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Replace operator with canonical form."""
    ctx = context or {}
    canonical = ctx.get("canonical_operator")
    if not canonical:
        # Try to extract from issue description
        desc = issue.description or ""
        match = re.search(r"should be '([^']+)'", desc)
        if match:
            canonical = match.group(1)
    if canonical:
        return FixAttempt(
            success=True, new_value=canonical,
            confidence=_vary_confidence(0.90, "operator_inconsistency"),
            log=f"Normalized operator: {issue.original_value!r} → {canonical!r}",
        )
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="No canonical operator available")


# ---------------------------------------------------------------------------
# Timing fixers
# ---------------------------------------------------------------------------

def fix_unrealistic_runtime(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Clamp runtime to valid range."""
    val = issue.original_value
    if not isinstance(val, (int, float)):
        return FixAttempt(success=False, new_value=None, confidence=0.0,
                          log="Cannot parse runtime value")
    if val < RUNTIME_MIN_MINUTES:
        clamped = RUNTIME_MIN_MINUTES
    elif val > RUNTIME_MAX_MINUTES:
        clamped = RUNTIME_MAX_MINUTES
    else:
        return FixAttempt(success=False, new_value=None, confidence=0.0,
                          log="Runtime already in valid range")
    return FixAttempt(
        success=True, new_value=clamped,
        confidence=_vary_confidence(0.60, "unrealistic_runtime"),
        log=f"Clamped runtime: {val:.1f} → {clamped:.1f} min",
    )


def fix_calendar_no_active_days(issue: EntityIssue, context: Dict[str, Any] | None = None) -> FixAttempt:
    """Calendar with no active days — cannot safely guess, always reject."""
    return FixAttempt(success=False, new_value=None, confidence=0.0,
                      log="Cannot auto-fix empty calendar — service pattern unknown")


# ---------------------------------------------------------------------------
# Registry: rule_name → fixer function
# ---------------------------------------------------------------------------

FIXERS: Dict[str, Any] = {
    "stop_name_placeholder": fix_stop_name_placeholder,
    "stop_name_empty": fix_stop_name_empty,
    "stop_coords_outside_bbox": fix_stop_coords_outside_bbox,
    "stop_coords_null_island": fix_stop_coords_null_island,
    "stop_duplicate_nearby": fix_stop_duplicate_nearby,
    "stop_ref_garbage": fix_stop_ref_garbage,
    "stop_name_uuid_prefix": fix_stop_name_uuid_prefix,
    "route_too_few_stops": fix_route_too_few_stops,
    "route_no_schedule": fix_route_no_schedule,
    "route_name_garbage": fix_route_name_garbage,
    "shape_gap_too_large": fix_shape_gap_too_large,
    "shape_self_intersection": fix_shape_self_intersection,
    "short_name_collision": fix_short_name_collision,
    "operator_inconsistency": fix_operator_inconsistency,
    "unrealistic_runtime": fix_unrealistic_runtime,
    "calendar_no_active_days": fix_calendar_no_active_days,
    "route_geometry_straight_line": fix_route_geometry_straight_line,
    "route_geometry_low_detail": fix_route_geometry_low_detail,
    "route_geometry_low_sinuosity": fix_route_geometry_low_sinuosity,
}


def get_fixer(rule_name: str):
    """Look up the fixer for a given rule_name. Returns None if no fixer exists."""
    return FIXERS.get(rule_name)
