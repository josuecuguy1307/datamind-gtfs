"""Error injector — injects synthetic errors into a canton snapshot for benchmarking.

Reproducible via seed. Returns (poisoned_snapshot, ground_truth).
"""
from __future__ import annotations

import copy
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Tuple

from .config import ECUADOR_BBOX
from .models import (
    QualityGateInput,
    Route,
    RouteSemantics,
    ScheduleProfile,
    Shape,
    ShapePoint,
    Stop,
)


@dataclass
class InjectedError:
    """Record of a single injected error for ground-truth comparison."""
    entity_id: str
    entity_type: str
    original_value: Any
    corrupted_value: Any
    rule_name: str

    def __eq__(self, other):
        if not isinstance(other, InjectedError):
            return NotImplemented
        return (self.entity_id == other.entity_id and
                self.entity_type == other.entity_type and
                self.rule_name == other.rule_name and
                self.corrupted_value == other.corrupted_value)

    def __hash__(self):
        return hash((self.entity_id, self.entity_type, self.rule_name))


@dataclass
class ErrorProfile:
    """Defines how many of each error type to inject."""
    blank_names: int = 50
    placeholder_names: int = 20
    uuid_prefix_names: int = 10
    swapped_coords: int = 30
    null_island_coords: int = 10
    duplicates_3m: int = 5
    duplicates_8m: int = 5
    duplicates_14m: int = 5
    short_routes: int = 10
    shape_gaps_6km: int = 5
    runtime_7h: int = 8
    runtime_3min: int = 5
    numeric_route_names: int = 10
    operator_typos: int = 15
    short_name_collisions: int = 5
    no_active_days: int = 3


PROFILES: dict[str, "ErrorProfile"] = {}  # populated after class def


def _register_profiles():
    """Register named profiles after ErrorProfile is defined."""
    PROFILES["smoke"] = ErrorProfile(
        blank_names=5, placeholder_names=2, uuid_prefix_names=1,
        swapped_coords=3, null_island_coords=1,
        duplicates_3m=2, duplicates_8m=1, duplicates_14m=1,
        short_routes=2, shape_gaps_6km=1,
        runtime_7h=1, runtime_3min=1,
        numeric_route_names=2, operator_typos=2,
        short_name_collisions=1, no_active_days=1,
    )
    PROFILES["full"] = ErrorProfile()  # defaults = the 206-error spec


_register_profiles()


def get_profile(name: str) -> "ErrorProfile":
    """Look up a named error profile. Raises ValueError if unknown."""
    if name not in PROFILES:
        raise ValueError(
            f"Unknown profile {name!r}. Available: {', '.join(sorted(PROFILES))}"
        )
    return PROFILES[name]


def inject(
    snapshot: QualityGateInput,
    profile: ErrorProfile,
    seed: int = 42,
) -> Tuple[QualityGateInput, List[InjectedError]]:
    """Inject errors into a canton snapshot. Returns (poisoned, ground_truth).

    Reproducible: same seed + same snapshot = same errors injected.
    """
    rng = random.Random(seed)
    poisoned = _deep_copy_input(snapshot)
    truth: List[InjectedError] = []

    # --- Stop errors ---
    stops = poisoned.stops
    if stops:
        truth.extend(_inject_blank_names(stops, profile.blank_names, rng))
        truth.extend(_inject_placeholder_names(stops, profile.placeholder_names, rng))
        truth.extend(_inject_uuid_prefix_names(stops, profile.uuid_prefix_names, rng))
        truth.extend(_inject_swapped_coords(stops, profile.swapped_coords, rng))
        truth.extend(_inject_null_island(stops, profile.null_island_coords, rng))
        truth.extend(_inject_duplicate_stops(poisoned, profile.duplicates_3m, 3.0, rng))
        truth.extend(_inject_duplicate_stops(poisoned, profile.duplicates_8m, 8.0, rng))
        truth.extend(_inject_duplicate_stops(poisoned, profile.duplicates_14m, 14.0, rng))

    # --- Route errors ---
    routes = poisoned.routes
    if routes:
        truth.extend(_inject_short_routes(routes, profile.short_routes, rng))

    # --- Shape errors ---
    shapes = poisoned.shapes
    if shapes:
        truth.extend(_inject_shape_gaps(shapes, profile.shape_gaps_6km, rng))

    # --- Naming errors ---
    sems = poisoned.semantics
    if sems:
        truth.extend(_inject_numeric_route_names(sems, profile.numeric_route_names, rng))
        truth.extend(_inject_operator_typos(sems, profile.operator_typos, rng))
        truth.extend(_inject_short_name_collisions(sems, profile.short_name_collisions, rng))

    # --- Timing errors ---
    scheds = poisoned.schedules
    if scheds:
        truth.extend(_inject_runtime_too_long(scheds, profile.runtime_7h, rng))
        truth.extend(_inject_runtime_too_short(scheds, profile.runtime_3min, rng))
        truth.extend(_inject_no_active_days(scheds, profile.no_active_days, rng))

    return poisoned, truth


def _deep_copy_input(inp: QualityGateInput) -> QualityGateInput:
    return copy.deepcopy(inp)


def _pick(items: list, n: int, rng: random.Random) -> list:
    """Pick up to n unique items from list."""
    n = min(n, len(items))
    if n <= 0:
        return []
    return rng.sample(items, n)


# ---------------------------------------------------------------------------
# Stop injectors
# ---------------------------------------------------------------------------

def _inject_blank_names(stops: List[Stop], n: int, rng: random.Random) -> List[InjectedError]:
    chosen = _pick([s for s in stops if s.name], n, rng)
    errors = []
    for s in chosen:
        orig = s.name
        s.name = ""
        errors.append(InjectedError(s.stop_id, "stop", orig, "", "stop_name_empty"))
    return errors


def _inject_placeholder_names(stops: List[Stop], n: int, rng: random.Random) -> List[InjectedError]:
    placeholders = ["parada sin nombre", "Bus Stop 1", "unnamed", "Bus Stop 99"]
    chosen = _pick([s for s in stops if s.name and s.name not in placeholders], n, rng)
    errors = []
    for s in chosen:
        orig = s.name
        s.name = rng.choice(placeholders)
        errors.append(InjectedError(s.stop_id, "stop", orig, s.name, "stop_name_placeholder"))
    return errors


def _inject_uuid_prefix_names(stops: List[Stop], n: int, rng: random.Random) -> List[InjectedError]:
    chosen = _pick([s for s in stops if s.name], n, rng)
    errors = []
    for s in chosen:
        orig = s.name
        fake_uuid = uuid.UUID(int=rng.getrandbits(128)).hex[:8]
        s.name = f"Stop {fake_uuid}"
        errors.append(InjectedError(s.stop_id, "stop", orig, s.name, "stop_name_uuid_prefix"))
    return errors


def _inject_swapped_coords(stops: List[Stop], n: int, rng: random.Random) -> List[InjectedError]:
    min_lat, min_lon, max_lat, max_lon = ECUADOR_BBOX
    valid = [s for s in stops if min_lat <= s.lat <= max_lat and min_lon <= s.lon <= max_lon]
    chosen = _pick(valid, n, rng)
    errors = []
    for s in chosen:
        orig = (s.lat, s.lon)
        s.lat, s.lon = s.lon, s.lat  # swap
        errors.append(InjectedError(s.stop_id, "stop", orig, (s.lat, s.lon), "stop_coords_outside_bbox"))
    return errors


def _inject_null_island(stops: List[Stop], n: int, rng: random.Random) -> List[InjectedError]:
    chosen = _pick([s for s in stops if s.lat != 0 or s.lon != 0], n, rng)
    errors = []
    for s in chosen:
        orig = (s.lat, s.lon)
        s.lat, s.lon = 0.0, 0.0
        errors.append(InjectedError(s.stop_id, "stop", orig, (0.0, 0.0), "stop_coords_null_island"))
    return errors


def _inject_duplicate_stops(inp: QualityGateInput, n: int, offset_m: float,
                             rng: random.Random) -> List[InjectedError]:
    """Create duplicate stops at offset_m distance."""
    chosen = _pick(inp.stops, n, rng)
    errors = []
    for s in chosen:
        import math
        # Offset in degrees (~1m at equator ≈ 0.000009 degrees)
        deg_offset = (offset_m / 111_320.0)
        angle = rng.uniform(0, 2 * math.pi)
        new_lat = s.lat + deg_offset * math.sin(angle)
        new_lon = s.lon + deg_offset * math.cos(angle)
        dup = Stop(
            stop_id=uuid.UUID(int=rng.getrandbits(128)).hex,
            name=s.name,
            ref=s.ref,
            lat=new_lat, lon=new_lon,
            operator=s.operator,
            confidence=s.confidence * 0.9,
        )
        inp.stops.append(dup)
        errors.append(InjectedError(
            dup.stop_id, "stop",
            None, {"original_id": s.stop_id, "distance_m": offset_m},
            "stop_duplicate_nearby",
        ))
    return errors


# ---------------------------------------------------------------------------
# Route injectors
# ---------------------------------------------------------------------------

def _inject_short_routes(routes: List[Route], n: int, rng: random.Random) -> List[InjectedError]:
    valid = [r for r in routes if len(r.stop_node_ids) >= 3]
    chosen = _pick(valid, n, rng)
    errors = []
    for r in chosen:
        orig = list(r.stop_node_ids)
        r.stop_node_ids = r.stop_node_ids[:2]
        errors.append(InjectedError(r.route_id, "route", orig, r.stop_node_ids, "route_too_few_stops"))
    return errors


# ---------------------------------------------------------------------------
# Shape injectors
# ---------------------------------------------------------------------------

def _inject_shape_gaps(shapes: List[Shape], n: int, rng: random.Random) -> List[InjectedError]:
    valid = [s for s in shapes if len(s.points) >= 4]
    chosen = _pick(valid, n, rng)
    errors = []
    for s in chosen:
        pts = sorted(s.points, key=lambda p: p.sequence)
        idx = rng.randint(1, len(pts) - 2)
        orig = (pts[idx].lat, pts[idx].lon)
        # Jump 6km away
        pts[idx].lat += 0.054  # ~6km in latitude
        errors.append(InjectedError(
            s.shape_id, "shape", orig, (pts[idx].lat, pts[idx].lon),
            "shape_gap_too_large",
        ))
    return errors


# ---------------------------------------------------------------------------
# Naming injectors
# ---------------------------------------------------------------------------

def _inject_numeric_route_names(sems: List[RouteSemantics], n: int,
                                  rng: random.Random) -> List[InjectedError]:
    chosen = _pick([s for s in sems if s.route_short_name], n, rng)
    errors = []
    for s in chosen:
        orig = s.route_short_name
        s.route_short_name = str(rng.randint(10000, 99999))
        errors.append(InjectedError(s.route_id, "naming", orig, s.route_short_name, "route_name_garbage"))
    return errors


def _inject_operator_typos(sems: List[RouteSemantics], n: int,
                            rng: random.Random) -> List[InjectedError]:
    typo_map = {"a": "á", "e": "é", "i": "í", "o": "ó", "n": "ñ"}
    chosen = _pick([s for s in sems if s.operator and len(s.operator) > 3], n, rng)
    errors = []
    for s in chosen:
        orig = s.operator
        chars = list(s.operator)
        # Insert a random typo
        candidates = [(i, c) for i, c in enumerate(chars) if c.lower() in typo_map]
        if candidates:
            idx, c = rng.choice(candidates)
            chars[idx] = typo_map[c.lower()]
        else:
            chars.insert(rng.randint(0, len(chars)), " ")
        s.operator = "".join(chars)
        s.extra["canonical_operator"] = orig
        errors.append(InjectedError(s.route_id, "naming", orig, s.operator, "operator_inconsistency"))
    return errors


def _inject_short_name_collisions(sems: List[RouteSemantics], n: int,
                                    rng: random.Random) -> List[InjectedError]:
    with_names = [s for s in sems if s.route_short_name]
    chosen = _pick(with_names, min(n, len(with_names) // 2), rng)
    errors = []
    for s in chosen:
        # Find another sem to give the same short_name
        others = [o for o in sems if o.route_id != s.route_id and o.route_short_name != s.route_short_name]
        if others:
            target = rng.choice(others)
            orig = target.route_short_name
            target.route_short_name = s.route_short_name
            target.extra["short_name_collision"] = True
            errors.append(InjectedError(
                target.route_id, "naming", orig, target.route_short_name, "short_name_collision",
            ))
    return errors


# ---------------------------------------------------------------------------
# Timing injectors
# ---------------------------------------------------------------------------

def _inject_runtime_too_long(scheds: List[ScheduleProfile], n: int,
                              rng: random.Random) -> List[InjectedError]:
    chosen = _pick(scheds, n, rng)
    errors = []
    for s in chosen:
        orig = s.peak_runtime_min
        s.peak_runtime_min = rng.uniform(420.0, 600.0)  # 7-10 hours
        errors.append(InjectedError(s.route_id, "timing", orig, s.peak_runtime_min, "unrealistic_runtime"))
    return errors


def _inject_runtime_too_short(scheds: List[ScheduleProfile], n: int,
                               rng: random.Random) -> List[InjectedError]:
    chosen = _pick([s for s in scheds if s not in _pick(scheds, 0, rng)], n, rng)
    errors = []
    for s in chosen:
        orig = s.offpeak_runtime_min
        s.offpeak_runtime_min = rng.uniform(0.5, 3.0)
        errors.append(InjectedError(s.route_id, "timing", orig, s.offpeak_runtime_min, "unrealistic_runtime"))
    return errors


def _inject_no_active_days(scheds: List[ScheduleProfile], n: int,
                            rng: random.Random) -> List[InjectedError]:
    chosen = _pick(scheds, n, rng)
    errors = []
    for s in chosen:
        orig = s.service_days
        s.service_days = {
            "monday": False, "tuesday": False, "wednesday": False,
            "thursday": False, "friday": False, "saturday": False, "sunday": False,
        }
        errors.append(InjectedError(s.route_id, "timing", orig, s.service_days, "calendar_no_active_days"))
    return errors


# ---------------------------------------------------------------------------
