"""Targeted Troublemaker Stop Swap — surgical local fix.

⚠ **EXPERIMENTAL — proven 0-regression on QC, but minimal gain.**
QC 116-route eval (2026-04-22): 1 full anomaly resolution, 0 regressions,
115 routes had no target anomalies to swap (prior passes already scrubbed
them). The strict promotion gate held: the module never made things worse.
Safe to leave on for first-pass T2-T8 runs (raw routes have more anomalies
to work with), but DO NOT expect significant gains on already-iterated
QC-style cohorts. Default OFF — opt in via ``--enable-troublemaker-swap``.


Hypothesis test (after the 3-way global-reorder comparison showed
+3 to +8.64 anomalies per route): the problem on QC degraded/unroutable
routes is NOT the full sequence but **1-3 specific stops** whose position
forces Valhalla into a U-turn or backtrack. Fix those stops LOCALLY —
swap 1 position at a time in the anomaly region, retrace, validate.

Contract (strict, matches the user spec):

  For each anomaly on the v1 shape:
    1. Locate the vertex range (via ``location_idx``); project to cum_m
       along the polyline; find the two stops whose cum_m bracket it.
    2. The "troublemaker" is the bracketing stop closest to the anomaly
       location in cum_m.
    3. Try up to 3 local swap kinds in order:
        a. swap_right  — swap troublemaker with next stop
        b. swap_left   — swap troublemaker with previous stop
        c. move_to_skip — move troublemaker two positions forward
    4. After each swap, retrace via the caller-supplied Valhalla client
       and re-run the Geometry Enforcer.
    5. Accept the swap iff:
         - the original anomaly is RESOLVED (gone), AND
         - no NEW anomalies were introduced, AND
         - class did not worsen.
    6. If accepted, continue to the next unresolved anomaly (sometimes
       fixing one resolves others).

  Hard caps (enforced internally):
    - Max 5 accepted swaps per route
    - Max 15 Valhalla retraces per route
    - Refuse any swap that introduces new anomalies or worsens class

All results are returned in a :class:`FixResult`. The caller decides
whether to promote to ``route_prod.approval_queue`` (this module never
writes to the DB).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from hades.enforcers.geometry_enforcer import (
    DEFAULT_THRESHOLDS,
    GeometryEnforcer,
    GeometryEnforcerThresholds,
    GeometryReport,
    Anomaly,
)
from hades.enforcers.stop_coverage_enforcer import (
    cumulative_m,
    project_point_to_polyline,
    haversine_m,
)


# Valhalla retrace callable — takes list of (lon, lat), returns polyline as
# list of (lon, lat). Injectable so the module is unit-testable without
# network.
RetraceFn = Callable[[list[tuple[float, float]]], list[tuple[float, float]]]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SwapAttempt:
    anomaly_type: str
    anomaly_location_idx: int
    troublemaker_stop_idx: int
    swap_kind: str                  # "swap_right" | "swap_left" | "move_to_skip"
    accepted: bool
    reason: str                     # "resolved" | "introduced_new" | "class_worsened" | "retrace_error" | "no_op"
    anomalies_resolved_count: int = 0
    new_anomalies_introduced_count: int = 0
    coherence_before: float = 0.0
    coherence_after: float = 0.0


@dataclass(slots=True)
class FixResult:
    success: bool
    route_id: str
    proposed_shape: Optional[list[tuple[float, float]]] = None   # (lon, lat)
    proposed_stops: Optional[list[tuple[float, float]]] = None   # (lat, lon)
    fixes_applied: list[SwapAttempt] = field(default_factory=list)
    swap_attempts: list[SwapAttempt] = field(default_factory=list)
    anomalies_v1: list[str] = field(default_factory=list)
    anomalies_resolved: list[str] = field(default_factory=list)
    anomalies_remaining: list[str] = field(default_factory=list)
    new_anomalies_introduced: list[str] = field(default_factory=list)
    retraces_used: int = 0
    elapsed_s: float = 0.0
    class_before: Optional[str] = None
    class_after: Optional[str] = None
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "success": bool(self.success),
            "class_before": self.class_before,
            "class_after": self.class_after,
            "retraces_used": int(self.retraces_used),
            "elapsed_s": round(self.elapsed_s, 2),
            "anomalies_v1": list(self.anomalies_v1),
            "anomalies_resolved": list(self.anomalies_resolved),
            "anomalies_remaining": list(self.anomalies_remaining),
            "new_anomalies_introduced": list(self.new_anomalies_introduced),
            "n_swap_attempts": len(self.swap_attempts),
            "n_accepted_swaps": sum(1 for s in self.swap_attempts if s.accepted),
            "swap_attempts": [
                {
                    "anomaly_type": s.anomaly_type,
                    "troublemaker_stop_idx": s.troublemaker_stop_idx,
                    "swap_kind": s.swap_kind,
                    "accepted": s.accepted,
                    "reason": s.reason,
                    "coh_before": round(s.coherence_before, 4),
                    "coh_after": round(s.coherence_after, 4),
                    "anomalies_resolved_count": s.anomalies_resolved_count,
                    "new_anomalies_introduced_count": s.new_anomalies_introduced_count,
                } for s in self.swap_attempts
            ],
            "notes": dict(self.notes),
        }


# ---------------------------------------------------------------------------
# Troublemaker identification.
# ---------------------------------------------------------------------------

_TARGET_ANOMALY_TYPES = {"U_TURN", "IMPOSSIBLE_LOOP", "DIRECTION_INCONSISTENCY", "OSCILLATION", "BACKTRACK"}


def _anomaly_cum_m(a: Anomaly, cum: list[float]) -> float:
    """cum_m of the anomaly's reported vertex index (clamped to polyline)."""
    idx = int(a.location_idx)
    if idx < 0:
        return 0.0
    if idx >= len(cum):
        return cum[-1] if cum else 0.0
    return cum[idx]


def _project_stops(
    stops_latlon: Sequence[tuple[float, float]],
    coords_lonlat: Sequence[tuple[float, float]],
    cum: list[float],
) -> list[float]:
    """Projected cum_m per stop."""
    out: list[float] = []
    for (lat, lon) in stops_latlon:
        p, _ = project_point_to_polyline(float(lat), float(lon), coords_lonlat, cum)
        out.append(p)
    return out


def _find_troublemaker_stop(
    anomaly: Anomaly,
    stop_cums: list[float],
    cum: list[float],
) -> int:
    """Return the index of the stop whose cum_m is closest to the anomaly's
    cum_m (the one most likely responsible for the backtrack / loop)."""
    anom_cum = _anomaly_cum_m(anomaly, cum)
    best = 1
    best_d = float("inf")
    for i in range(1, len(stop_cums) - 1):  # skip endpoints
        d = abs(stop_cums[i] - anom_cum)
        if d < best_d:
            best_d = d
            best = i
    return best


# ---------------------------------------------------------------------------
# Local swap kinds.
# ---------------------------------------------------------------------------

def _apply_swap(
    stops: list[tuple[float, float]],
    i: int,
    kind: str,
) -> Optional[list[tuple[float, float]]]:
    """Return a new stop list with the swap applied, or None if invalid."""
    n = len(stops)
    if kind == "swap_right":
        if i + 1 >= n - 1:  # don't touch the terminal endpoint
            return None
        out = list(stops)
        out[i], out[i + 1] = out[i + 1], out[i]
        return out
    if kind == "swap_left":
        if i - 1 <= 0:
            return None
        out = list(stops)
        out[i - 1], out[i] = out[i], out[i - 1]
        return out
    if kind == "move_to_skip":
        # Move stop i to position i+2 (skip one): pop i, insert at i+2 (now i+1 after pop)
        if i + 2 >= n - 1:
            return None
        out = list(stops)
        s = out.pop(i)
        out.insert(i + 2, s)
        return out
    return None


# ---------------------------------------------------------------------------
# Validation of a swap attempt.
# ---------------------------------------------------------------------------

_CLASS_RANK = {"good": 0, "acceptable": 1, "degraded": 2, "unroutable": 3}


def _anomaly_signatures(report: GeometryReport) -> set[tuple[str, int]]:
    return {(a.type, int(a.location_idx)) for a in report.anomalies}


def _compute_coherence(report: GeometryReport, coords: Sequence[tuple[float, float]]) -> float:
    from hades.enforcers.sequence_coherence_optimizer import coherence_score
    return coherence_score(report, coords)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def try_local_swaps(
    *,
    route_id: str,
    shape: Sequence[tuple[float, float]],         # (lon, lat)
    stops: Sequence[tuple[float, float]],         # (lat, lon)
    retrace_fn: RetraceFn,
    stop_cov_classifier: Optional[Callable[..., str]] = None,
    geometry_report: Optional[GeometryReport] = None,
    thresholds: GeometryEnforcerThresholds = DEFAULT_THRESHOLDS,
    enforcer: Optional[GeometryEnforcer] = None,
    max_swaps: int = 5,
    max_retraces: int = 15,
) -> FixResult:
    """Apply targeted local stop swaps to resolve specific anomalies.

    ``stop_cov_classifier(stops, coords) -> str`` optional; used to track
    class_before/class_after. Default = not tracked.

    Returns a :class:`FixResult`. ``success=True`` iff ≥1 swap was accepted
    (i.e. at least one anomaly resolved with no new ones and no class regression).
    Caller decides whether to promote.
    """
    t0 = time.monotonic()
    _enforcer = enforcer or GeometryEnforcer(thresholds=thresholds)
    coords = [(float(lon), float(lat)) for (lon, lat) in shape]
    stops_list = [(float(lat), float(lon)) for (lat, lon) in stops]

    result = FixResult(route_id=route_id, success=False)

    if len(coords) < 3 or len(stops_list) < 4:
        result.notes["skip"] = "too_few_coords_or_stops"
        result.elapsed_s = time.monotonic() - t0
        return result

    report_v1 = geometry_report or _enforcer.analyze(coords, route_code=route_id)
    v1_anomalies = [a for a in report_v1.anomalies if a.type in _TARGET_ANOMALY_TYPES]
    result.anomalies_v1 = [f"{a.type}@{a.location_idx}" for a in v1_anomalies]

    if stop_cov_classifier is not None:
        try:
            result.class_before = stop_cov_classifier(stops_list, coords)
            result.class_after = result.class_before
        except Exception:
            result.class_before = None

    if not v1_anomalies:
        result.notes["skip"] = "no_target_anomalies"
        result.elapsed_s = time.monotonic() - t0
        return result

    # Current state evolves as we accept swaps.
    current_coords = list(coords)
    current_stops = list(stops_list)
    current_report = report_v1
    current_signatures = _anomaly_signatures(current_report)

    accepted_swaps = 0
    retraces = 0

    # Iterate over v1 anomalies in severity order (most severe first).
    v1_anomalies_sorted = sorted(
        v1_anomalies, key=lambda a: -float(a.severity)
    )

    for anomaly in v1_anomalies_sorted:
        if accepted_swaps >= max_swaps or retraces >= max_retraces:
            break
        sig = (anomaly.type, int(anomaly.location_idx))
        # Only retry if still present in current_report
        if sig not in _anomaly_signatures(current_report):
            continue

        cum = cumulative_m(current_coords)
        stop_cums = _project_stops(current_stops, current_coords, cum)
        troublemaker_idx = _find_troublemaker_stop(anomaly, stop_cums, cum)

        for kind in ("swap_right", "swap_left", "move_to_skip"):
            if retraces >= max_retraces or accepted_swaps >= max_swaps:
                break
            candidate_stops = _apply_swap(current_stops, troublemaker_idx, kind)
            if candidate_stops is None:
                result.swap_attempts.append(SwapAttempt(
                    anomaly_type=anomaly.type,
                    anomaly_location_idx=int(anomaly.location_idx),
                    troublemaker_stop_idx=troublemaker_idx,
                    swap_kind=kind, accepted=False, reason="no_op",
                ))
                continue
            # Retrace candidate order
            try:
                locs = [(lon, lat) for (lat, lon) in candidate_stops]
                new_coords = list(retrace_fn(locs))
                retraces += 1
            except Exception as exc:
                result.swap_attempts.append(SwapAttempt(
                    anomaly_type=anomaly.type,
                    anomaly_location_idx=int(anomaly.location_idx),
                    troublemaker_stop_idx=troublemaker_idx,
                    swap_kind=kind, accepted=False,
                    reason=f"retrace_error:{type(exc).__name__}",
                ))
                continue

            new_report = _enforcer.analyze(new_coords, route_code=route_id)
            new_signatures = _anomaly_signatures(new_report)

            resolved_count = len(current_signatures - new_signatures)
            introduced_count = len(new_signatures - current_signatures)
            original_still_present = sig in new_signatures
            coh_before = _compute_coherence(current_report, current_coords)
            coh_after = _compute_coherence(new_report, new_coords)

            # Class check (if available)
            worsened = False
            new_class = None
            if stop_cov_classifier is not None:
                try:
                    new_class = stop_cov_classifier(candidate_stops, new_coords)
                except Exception:
                    new_class = None
                if new_class and result.class_after:
                    if _CLASS_RANK.get(new_class, 9) > _CLASS_RANK.get(result.class_after, 9):
                        worsened = True

            if original_still_present:
                reason = "not_resolved"
                accepted = False
            elif introduced_count > 0:
                reason = "introduced_new"
                accepted = False
            elif worsened:
                reason = "class_worsened"
                accepted = False
            else:
                reason = "resolved"
                accepted = True

            result.swap_attempts.append(SwapAttempt(
                anomaly_type=anomaly.type,
                anomaly_location_idx=int(anomaly.location_idx),
                troublemaker_stop_idx=troublemaker_idx,
                swap_kind=kind, accepted=accepted, reason=reason,
                anomalies_resolved_count=resolved_count,
                new_anomalies_introduced_count=introduced_count,
                coherence_before=coh_before, coherence_after=coh_after,
            ))

            if accepted:
                accepted_swaps += 1
                current_coords = new_coords
                current_stops = candidate_stops
                current_report = new_report
                current_signatures = new_signatures
                if new_class:
                    result.class_after = new_class
                result.fixes_applied.append(result.swap_attempts[-1])
                break  # next anomaly

    # Compute result buckets
    final_signatures = _anomaly_signatures(current_report)
    v1_sig_set = {(a.type, int(a.location_idx)) for a in v1_anomalies}
    original_still = v1_sig_set & final_signatures
    original_resolved = v1_sig_set - original_still
    introduced_overall = final_signatures - v1_sig_set

    result.anomalies_resolved = [f"{t}@{i}" for (t, i) in original_resolved]
    result.anomalies_remaining = [f"{t}@{i}" for (t, i) in original_still]
    result.new_anomalies_introduced = [f"{t}@{i}" for (t, i) in introduced_overall]
    result.retraces_used = retraces
    result.elapsed_s = time.monotonic() - t0

    if accepted_swaps > 0 and not introduced_overall:
        result.success = True
        result.proposed_shape = current_coords
        result.proposed_stops = current_stops
    else:
        result.success = False
    return result


__all__ = ["try_local_swaps", "FixResult", "SwapAttempt"]
