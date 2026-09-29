"""Copy-on-write canton state for speculative fix application.

WorkingCopy holds deep copies of all entities so that fixes can be applied,
evaluated, and reverted without touching the original QualityGateInput or
any production database.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...shared.models import (
    Fare,
    FixAttempt,
    QualityGateInput,
    Route,
    RouteSemantics,
    ScheduleProfile,
    Shape,
    Stop,
)


@dataclass
class Mutation:
    """Record of a single field mutation on an entity."""
    entity_type: str
    entity_id: str
    field_name: str
    old_value: Any
    new_value: Any
    pass_num: int


@dataclass
class WorkingCopy:
    """In-memory deep copy of all entities for a canton.

    The gate reads from and writes to this during convergence iterations.
    Nothing touches prod until commit_to_input() is called.
    """
    canton: str
    province: str
    stops: Dict[str, Stop] = field(default_factory=dict)
    routes: Dict[str, Route] = field(default_factory=dict)
    shapes: Dict[str, Shape] = field(default_factory=dict)
    semantics: Dict[str, RouteSemantics] = field(default_factory=dict)
    schedules: Dict[str, ScheduleProfile] = field(default_factory=dict)
    fares: Dict[str, Fare] = field(default_factory=dict)
    mutations_log: List[Mutation] = field(default_factory=list)

    # Snapshot of the original state for revert
    _originals: Optional[Dict[str, dict]] = field(default=None, repr=False)

    @classmethod
    def from_input(cls, inp: QualityGateInput) -> "WorkingCopy":
        """Create a working copy by deep-copying all entities from the input."""
        wc = cls(canton=inp.canton, province=inp.province)
        wc.stops = {s.stop_id: copy.deepcopy(s) for s in inp.stops}
        wc.routes = {r.route_id: copy.deepcopy(r) for r in inp.routes}
        wc.shapes = {s.shape_id: copy.deepcopy(s) for s in inp.shapes}
        wc.semantics = {s.route_id: copy.deepcopy(s) for s in inp.semantics}
        wc.schedules = {s.route_id: copy.deepcopy(s) for s in inp.schedules}
        wc.fares = {f.fare_id: copy.deepcopy(f) for f in inp.fares}

        # Save originals for revert
        wc._originals = {
            "stops": {s.stop_id: copy.deepcopy(s) for s in inp.stops},
            "routes": {r.route_id: copy.deepcopy(r) for r in inp.routes},
            "shapes": {s.shape_id: copy.deepcopy(s) for s in inp.shapes},
            "semantics": {s.route_id: copy.deepcopy(s) for s in inp.semantics},
            "schedules": {s.route_id: copy.deepcopy(s) for s in inp.schedules},
            "fares": {f.fare_id: copy.deepcopy(f) for f in inp.fares},
        }
        return wc

    def _get_entity_store(self, entity_type: str) -> dict:
        """Get the dict store for a given entity type."""
        mapping = {
            "stop": self.stops,
            "route": self.routes,
            "shape": self.shapes,
            "naming": self.semantics,
            "timing": self.schedules,
            "fare": self.fares,
        }
        store = mapping.get(entity_type)
        if store is None:
            raise ValueError(f"Unknown entity_type: {entity_type!r}")
        return store

    def get_entity(self, entity_type: str, entity_id: str):
        """Look up a single entity by type and ID."""
        store = self._get_entity_store(entity_type)
        return store.get(entity_id)

    def apply_fix(self, entity_type: str, entity_id: str,
                  fix: FixAttempt, pass_num: int) -> bool:
        """Apply a fix's new_value to the entity in the working copy.

        The fix.new_value is expected to be a dict of {field_name: value} pairs
        to patch onto the entity. Returns True if any mutation was recorded.
        """
        entity = self.get_entity(entity_type, entity_id)
        if entity is None:
            return False

        patches = fix.new_value
        if not isinstance(patches, dict):
            # Scalar new_value — try to infer the field from entity_type
            patches = _infer_patch(entity_type, patches)

        mutated = False
        for field_name, new_val in patches.items():
            old_val = getattr(entity, field_name, None)
            if old_val != new_val:
                setattr(entity, field_name, new_val)
                self.mutations_log.append(Mutation(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    field_name=field_name,
                    old_value=old_val,
                    new_value=new_val,
                    pass_num=pass_num,
                ))
                mutated = True
        return mutated

    def remove_entity(self, entity_type: str, entity_id: str,
                      pass_num: int) -> bool:
        """Remove an entity (e.g., for duplicate merging). Returns True if found."""
        store = self._get_entity_store(entity_type)
        if entity_id in store:
            self.mutations_log.append(Mutation(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name="__removed__",
                old_value=True,
                new_value=None,
                pass_num=pass_num,
            ))
            del store[entity_id]
            return True
        return False

    def revert_all(self) -> None:
        """Restore all entities to their original state."""
        if self._originals is None:
            return
        self.stops = copy.deepcopy(self._originals["stops"])
        self.routes = copy.deepcopy(self._originals["routes"])
        self.shapes = copy.deepcopy(self._originals["shapes"])
        self.semantics = copy.deepcopy(self._originals["semantics"])
        self.schedules = copy.deepcopy(self._originals["schedules"])
        self.fares = copy.deepcopy(self._originals["fares"])
        self.mutations_log.clear()

    def to_entity_lists(self) -> dict:
        """Return current state as lists (matching QualityGateInput shape)."""
        return {
            "stops": list(self.stops.values()),
            "routes": list(self.routes.values()),
            "shapes": list(self.shapes.values()),
            "semantics": list(self.semantics.values()),
            "schedules": list(self.schedules.values()),
            "fares": list(self.fares.values()),
        }

    @property
    def total_mutations(self) -> int:
        return len(self.mutations_log)

    def mutations_for_pass(self, pass_num: int) -> List[Mutation]:
        return [m for m in self.mutations_log if m.pass_num == pass_num]


def _infer_patch(entity_type: str, scalar_value) -> dict:
    """Best-effort: map a scalar fix value to a field name."""
    defaults = {
        "stop": "name",
        "route": "route_name",
        "shape": "points",
        "naming": "route_long_name",
        "timing": "peak_runtime_min",
        "fare": "price",
    }
    field_name = defaults.get(entity_type, "extra")
    return {field_name: scalar_value}
