"""DR budget tracker for pipeline-time DR Type 2 queuing.

The Phase 3 coordinator invokes this to decide whether an unresolved
stop-coverage gap should be appended to the per-unit DR queue file
(``workspace/dr_stop_coverage/queries/pending_batch_<unit>.md``).

The tracker is a **per-run, in-memory counter**. Persistence between
runs happens implicitly through the pending batch file on disk, which
the coordinator re-reads on start if callers want cross-run
continuity. For the wiring MVP we keep it per-run — the counter's job
is to stop any single pipeline invocation from burying the operator in
DR work.

Defaults (see the prompt):

- ``max_queries_per_unit`` = 15
- ``max_per_route``        = 2
- ``min_gap_m_for_dr``     = 1200 m
- ``allowed_zones``        = {"urban_dense", "urban_peripheral"}

Everything is overridable at construction for tests and future tuning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


DEFAULT_MAX_PER_UNIT = 15
DEFAULT_MAX_PER_ROUTE = 2
DEFAULT_MIN_GAP_M = 1_200.0
DEFAULT_ALLOWED_ZONES: frozenset[str] = frozenset({"urban_dense", "urban_peripheral"})


@dataclass(slots=True)
class DRBudgetRecord:
    route_code: str
    gap_idx: int
    gap_m: float
    zone: str
    midpoint_lat: float
    midpoint_lon: float
    queued_at: str
    query_payload: dict[str, Any]


class DRBudgetTracker:
    """Mutable counter with a deterministic ``should_queue_dr`` gate.

    The class never writes to disk; the coordinator owns the pending
    batch file write. Keeping IO out of the tracker keeps the gating
    decisions trivially unit-testable.
    """

    def __init__(
        self,
        unit_id: str,
        *,
        max_queries_per_unit: int = DEFAULT_MAX_PER_UNIT,
        max_per_route: int = DEFAULT_MAX_PER_ROUTE,
        min_gap_m_for_dr: float = DEFAULT_MIN_GAP_M,
        allowed_zones: Iterable[str] = DEFAULT_ALLOWED_ZONES,
    ) -> None:
        self.unit_id = unit_id
        self.max_queries_per_unit = int(max_queries_per_unit)
        self.max_per_route = int(max_per_route)
        self.min_gap_m_for_dr = float(min_gap_m_for_dr)
        self.allowed_zones = frozenset(allowed_zones)
        self._total_queued: int = 0
        self._per_route: dict[str, int] = {}
        self._records: list[DRBudgetRecord] = []

    # ------------------------------------------------------------------
    # Gating
    # ------------------------------------------------------------------

    def should_queue_dr(
        self,
        gap: dict[str, Any],
        *,
        route_code: str,
        zone: Optional[str] = None,
    ) -> tuple[bool, str]:
        """Return ``(allow_queue, reason)`` for a single gap.

        ``gap`` is the stop-coverage enforcer's gap dict. ``zone`` falls
        back to the gap payload if not supplied. Rules are applied in
        order; the first failing rule wins so the reason string
        faithfully reflects *why* the gate said no.
        """
        gap_m = float(gap.get("gap_m") or gap.get("length_m") or 0.0)
        if gap_m < self.min_gap_m_for_dr:
            return False, f"gap_below_min_{int(gap_m)}m_lt_{int(self.min_gap_m_for_dr)}m"

        zone_effective = (zone or gap.get("zone") or "").strip().lower() or "unknown"
        if zone_effective not in self.allowed_zones:
            return False, f"zone_not_allowed_{zone_effective}"

        if self._total_queued >= self.max_queries_per_unit:
            return False, "unit_budget_exhausted"

        if self._per_route.get(route_code, 0) >= self.max_per_route:
            return False, "route_cap_reached"

        return True, "within_budget"

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record_queue(
        self,
        gap: dict[str, Any],
        query: dict[str, Any],
        *,
        route_code: str,
    ) -> DRBudgetRecord:
        """Book a gap against the budget. Call only after ``should_queue_dr``
        returned ``(True, ...)``.
        """
        if self._total_queued >= self.max_queries_per_unit:
            raise RuntimeError("record_queue called past unit budget; gate this first")
        if self._per_route.get(route_code, 0) >= self.max_per_route:
            raise RuntimeError(
                f"record_queue called past per-route cap for {route_code}; gate this first"
            )
        rec = DRBudgetRecord(
            route_code=route_code,
            gap_idx=int(gap.get("idx", -1)),
            gap_m=float(gap.get("gap_m") or 0.0),
            zone=str(gap.get("zone") or query.get("zone") or "unknown"),
            midpoint_lat=float(
                (gap.get("midpoint_coord") or [0.0, 0.0])[0]
                if gap.get("midpoint_coord") is not None
                else query.get("midpoint_lat", 0.0)
            ),
            midpoint_lon=float(
                (gap.get("midpoint_coord") or [0.0, 0.0])[1]
                if gap.get("midpoint_coord") is not None
                else query.get("midpoint_lon", 0.0)
            ),
            queued_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            query_payload=dict(query),
        )
        self._records.append(rec)
        self._total_queued += 1
        self._per_route[route_code] = self._per_route.get(route_code, 0) + 1
        return rec

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_unit_usage(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "total_queued": self._total_queued,
            "max_queries_per_unit": self.max_queries_per_unit,
            "max_per_route": self.max_per_route,
            "per_route": dict(self._per_route),
            "remaining": max(0, self.max_queries_per_unit - self._total_queued),
        }

    def records(self) -> list[DRBudgetRecord]:
        return list(self._records)


__all__ = [
    "DEFAULT_MAX_PER_UNIT",
    "DEFAULT_MAX_PER_ROUTE",
    "DEFAULT_MIN_GAP_M",
    "DEFAULT_ALLOWED_ZONES",
    "DRBudgetRecord",
    "DRBudgetTracker",
]
