"""In-memory shadow entity copies for S2 cross-validation.

Shadows are NEVER written to the database.  They exist only long enough for a
cross-validator to inspect the post-fix state and vote commit/reject.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Union

from ...shared.models import (
    FixAttempt,
    Route,
    RouteSemantics,
    ScheduleProfile,
    Shape,
    Stop,
)

Entity = Union[Stop, Route, Shape, RouteSemantics, ScheduleProfile]


@dataclass
class Shadow:
    """A before/after snapshot of an entity with a proposed fix applied."""

    original: Entity
    mutated: Entity
    mutation_log: list[str] = field(default_factory=list)

    @classmethod
    def from_fix(cls, entity: Entity, fix_attempt: FixAttempt) -> Shadow:
        """Create a shadow by deep-copying the entity and applying the fix."""
        mutated = deepcopy(entity)
        _apply_fix(mutated, fix_attempt)
        return cls(
            original=entity,
            mutated=mutated,
            mutation_log=[fix_attempt.log],
        )


def _apply_fix(entity: Entity, fix: FixAttempt) -> None:
    """Mutate *entity* in place according to *fix.new_value*.

    Each entity type has a small set of mutable fields; the fix's
    ``new_value`` determines which one to update.
    """
    val = fix.new_value
    if val is None:
        return

    # --- Stop mutations ------------------------------------------------
    if isinstance(entity, Stop):
        if isinstance(val, str):
            # Reverse-geocoded name
            entity.name = val
        elif isinstance(val, tuple) and len(val) == 2:
            # Swapped (lat, lon)
            entity.lat, entity.lon = val
        elif isinstance(val, dict) and "keep" in val:
            # Merge duplicate — mark as merged in extra
            entity.extra["merged_from"] = val.get("remove")
        return

    # --- Route mutations -----------------------------------------------
    if isinstance(entity, Route):
        if isinstance(val, dict) and "merge_into" in val:
            entity.extra["merged_into"] = val["merge_into"]
        elif isinstance(val, dict) and "clone_from" in val:
            entity.extra["cloned_from"] = val["clone_from"]
        return

    # --- Shape mutations -----------------------------------------------
    if isinstance(entity, Shape):
        if isinstance(val, dict) and val.get("action") == "valhalla_retrace":
            entity.extra = getattr(entity, "extra", {})  # Shape has no extra by default
            entity.extra = {"retrace_pending": True, **val}
        return

    # --- RouteSemantics mutations --------------------------------------
    if isinstance(entity, RouteSemantics):
        if isinstance(val, str):
            # Could be reconstructed name, canonical operator, or deduplicated short_name
            # Determine by inspecting what changed
            if entity.operator and val != entity.operator:
                entity.operator = val
            elif entity.route_short_name and "-" in val and val.startswith(entity.route_short_name):
                entity.route_short_name = val
            else:
                entity.route_long_name = val
        return

    # --- ScheduleProfile mutations -------------------------------------
    if isinstance(entity, ScheduleProfile):
        if isinstance(val, dict) and "monday" in val:
            entity.service_days = val
        elif isinstance(val, (int, float)):
            # Clamped runtime — apply to whichever field was out of range
            if entity.peak_runtime_min is not None:
                entity.peak_runtime_min = float(val)
            if entity.offpeak_runtime_min is not None:
                entity.offpeak_runtime_min = float(val)
        return

