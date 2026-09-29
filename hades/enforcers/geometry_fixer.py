"""HADES Geometry Fixer — produces v2 geometry that removes anomalies from v1.

Called exclusively by the re-entry worker in enhance mode. Given a v1 shape
and its :class:`GeometryReport`, the Fixer applies targeted vertex-drop
strategies (one per anomaly type), re-runs the enforcer, and emits the
candidate v2 ONLY if its report is strictly better than v1's. Otherwise the
fix is refused and v1 is preserved — this is the "Fixer never replaces v1
with worse v2" invariant from the Prompt 8 spec.

Strategies (one pass, unified index-drop set, executed in the order below):

1. SPIKE                   — drop the spike vertex (keeps its two neighbours).
2. exact-duplicate repeat  — dedupe consecutive duplicates (chord_m=0 loops
                             from duplicate-vertex encoding failures).
3. IMPOSSIBLE_LOOP         — drop the interior of the closure (closes_on+1
                             through location-1), collapsing the loop to a
                             single chord.
4. DIRECTION_INCONSISTENCY — drop the backtracking run's interior vertices
                             (segments [s, e] → vertices s+1 … e).
5. U_TURN                  — drop the apex vertex; the two neighbours pull
                             the direction back onto the corridor.
6. OSCILLATION             — Ramer-Douglas-Peucker simplification over the
                             flagged ~1 km window, ε = 20 m. Collapses
                             micro-reversals while preserving real turns.

All strategies share one rule: indices 0 and n-1 are never dropped.
Endpoints are preserved by construction.

Strict-better comparator (classification ladder: clean < minor < moderate < severe):

    v2_strictly_better iff (
        rank(v2.classification) < rank(v1.classification)
        OR (
            rank(v2.classification) == rank(v1.classification)
            AND len(v2.anomalies) < len(v1.anomalies)
        )
        OR (
            rank(v2.classification) == rank(v1.classification)
            AND len(v2.anomalies) == len(v1.anomalies)
            AND v2.max_severity + EPS < v1.max_severity
        )
    )

The Fixer is stateless and read-only on v1 — callers own persistence of
the v2 candidate (via route_prod.approval_queue) and the fix_report audit
(route_prod.fix_reports). Nothing here writes to the database.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from hades.enforcers.geometry_enforcer import (
    DEFAULT_THRESHOLDS,
    Anomaly,
    GeometryEnforcer,
    GeometryEnforcerThresholds,
    GeometryReport,
    haversine_m,
    perpendicular_distance_m,
)


# ---------------------------------------------------------------------------
# Classification ladder.
# ---------------------------------------------------------------------------

_CLASSIFICATION_RANK = {
    "clean": 0,
    "minor": 1,
    "moderate": 2,
    "severe": 3,
}

_SEVERITY_EPS = 1e-4

# Douglas-Peucker tolerance for OSCILLATION smoothing. Chosen to collapse
# sub-road-width reversals (lane paint, map-match jitter) without eating
# legitimate mid-block turns (which produce off-chord offsets of 30 m+).
_OSCILLATION_DP_EPS_M = 20.0


# ---------------------------------------------------------------------------
# Result schema.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class GeometryFixResult:
    success: bool
    reason: str  # "improved" | "no_anomalies" | "no_change" | "would_regress"
    coords_before: list[tuple[float, float]]
    coords_after: list[tuple[float, float]]
    report_before: GeometryReport
    report_after: Optional[GeometryReport]
    fixes_applied: list[dict[str, Any]] = field(default_factory=list)
    dropped_indices: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": bool(self.success),
            "reason": self.reason,
            "vertices_before": len(self.coords_before),
            "vertices_after": len(self.coords_after),
            "dropped_indices": list(self.dropped_indices),
            "fixes_applied": list(self.fixes_applied),
            "report_before": self.report_before.to_dict(),
            "report_after": (
                self.report_after.to_dict() if self.report_after is not None else None
            ),
        }


# ---------------------------------------------------------------------------
# Anomaly context parsers. The enforcer emits context as free-form strings;
# here we parse the fields the fix strategies need. All parsers return None
# on failure so strategies can safely skip malformed anomalies.
# ---------------------------------------------------------------------------

_RE_LOOP_CLOSES = re.compile(r"closes_on_idx=(-?\d+)")
_RE_LOOP_CHORD = re.compile(r"chord_m=([0-9]+(?:\.[0-9]+)?)")
_RE_DI_SEGMENTS = re.compile(r"segments=\[(-?\d+),(-?\d+)\]")


def _parse_loop_context(context: str) -> Optional[tuple[int, float]]:
    """Parse IMPOSSIBLE_LOOP context → (closes_on_idx, chord_m)."""
    m_close = _RE_LOOP_CLOSES.search(context or "")
    m_chord = _RE_LOOP_CHORD.search(context or "")
    if not m_close:
        return None
    closes_on = int(m_close.group(1))
    chord = float(m_chord.group(1)) if m_chord else -1.0
    return (closes_on, chord)


def _parse_di_context(context: str) -> Optional[tuple[int, int]]:
    """Parse DIRECTION_INCONSISTENCY context → (run_start_seg, run_end_seg)."""
    m = _RE_DI_SEGMENTS.search(context or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))


# ---------------------------------------------------------------------------
# Douglas-Peucker (used only for OSCILLATION smoothing).
# ---------------------------------------------------------------------------

def _dp_kept_indices(
    coords: Sequence[tuple[float, float]],
    start_idx: int,
    end_idx: int,
    eps_m: float,
) -> set[int]:
    """Return the subset of indices in [start_idx, end_idx] that DP keeps.

    Endpoints are always kept. Interior vertices are kept only if their
    perpendicular distance from the chord between the two active anchors
    exceeds ``eps_m``.
    """
    kept: set[int] = {start_idx, end_idx}
    if end_idx - start_idx < 2:
        return kept

    stack: list[tuple[int, int]] = [(start_idx, end_idx)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        a = coords[lo]
        b = coords[hi]
        max_d = -1.0
        max_k = -1
        for k in range(lo + 1, hi):
            d = perpendicular_distance_m(coords[k], a, b)
            if d > max_d:
                max_d = d
                max_k = k
        if max_d > eps_m and max_k > 0:
            kept.add(max_k)
            stack.append((lo, max_k))
            stack.append((max_k, hi))
    return kept


# ---------------------------------------------------------------------------
# Fix strategies. Each returns the indices it wants dropped plus a manifest
# entry describing what it did. The final drop set is the union of all
# strategy outputs minus {0, n-1}.
# ---------------------------------------------------------------------------

def _spike_drops(anomalies: Iterable[Anomaly]) -> tuple[set[int], list[dict[str, Any]]]:
    drops: set[int] = set()
    manifest: list[dict[str, Any]] = []
    for a in anomalies:
        if a.type != "SPIKE":
            continue
        drops.add(a.location_idx)
        manifest.append(
            {
                "anomaly_type": "SPIKE",
                "action": "drop_vertex",
                "location_idx": a.location_idx,
                "severity_before": a.severity,
            }
        )
    return drops, manifest


def _duplicate_drops(
    coords: Sequence[tuple[float, float]],
) -> tuple[set[int], list[dict[str, Any]]]:
    """Drop vertex i if coords[i] is identical (within 0.5 m) to coords[i-1]."""
    drops: set[int] = set()
    manifest: list[dict[str, Any]] = []
    n = len(coords)
    for i in range(1, n):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        if haversine_m(lat_a, lon_a, lat_b, lon_b) < 0.5:
            drops.add(i)
            manifest.append(
                {
                    "anomaly_type": "DUPLICATE_VERTEX",
                    "action": "drop_vertex",
                    "location_idx": i,
                    "matched_with_idx": i - 1,
                }
            )
    return drops, manifest


def _loop_drops(
    anomalies: Iterable[Anomaly], n: int
) -> tuple[set[int], list[dict[str, Any]]]:
    """Snip each IMPOSSIBLE_LOOP: drop vertices (closes_on+1 … location-1)."""
    drops: set[int] = set()
    manifest: list[dict[str, Any]] = []
    for a in anomalies:
        if a.type != "IMPOSSIBLE_LOOP":
            continue
        parsed = _parse_loop_context(a.context)
        if parsed is None:
            continue
        closes_on, chord_m = parsed
        lo = closes_on + 1
        hi = a.location_idx  # exclusive upper bound below
        if lo >= hi:
            continue
        for idx in range(lo, hi):
            drops.add(idx)
        manifest.append(
            {
                "anomaly_type": "IMPOSSIBLE_LOOP",
                "action": "snip_loop_interior",
                "location_idx": a.location_idx,
                "closes_on_idx": closes_on,
                "dropped_range": [lo, hi - 1],
                "chord_m": chord_m,
                "severity_before": a.severity,
            }
        )
    return drops, manifest


def _direction_inconsistency_drops(
    anomalies: Iterable[Anomaly], n: int
) -> tuple[set[int], list[dict[str, Any]]]:
    """Drop the backtracking-run interior vertices.

    A DI anomaly flags a run of consecutive segments [s, e] whose alignment
    against the half's dominant vector is below threshold. Segment i lives
    between vertices i and i+1, so the run covers vertices s through e+1.
    We keep the boundary vertices s and e+1 and drop interior vertices
    s+1 … e. (If e == s, nothing is dropped — too tight to snip safely.)
    """
    drops: set[int] = set()
    manifest: list[dict[str, Any]] = []
    for a in anomalies:
        if a.type != "DIRECTION_INCONSISTENCY":
            continue
        parsed = _parse_di_context(a.context)
        if parsed is None:
            continue
        run_start_seg, run_end_seg = parsed
        interior_lo = run_start_seg + 1
        interior_hi = run_end_seg  # inclusive
        if interior_lo > interior_hi:
            continue
        for idx in range(interior_lo, interior_hi + 1):
            drops.add(idx)
        manifest.append(
            {
                "anomaly_type": "DIRECTION_INCONSISTENCY",
                "action": "drop_backtrack_run_interior",
                "run_start_seg": run_start_seg,
                "run_end_seg": run_end_seg,
                "dropped_range": [interior_lo, interior_hi],
                "severity_before": a.severity,
            }
        )
    return drops, manifest


def _u_turn_drops(anomalies: Iterable[Anomaly]) -> tuple[set[int], list[dict[str, Any]]]:
    drops: set[int] = set()
    manifest: list[dict[str, Any]] = []
    for a in anomalies:
        if a.type != "U_TURN":
            continue
        drops.add(a.location_idx)
        manifest.append(
            {
                "anomaly_type": "U_TURN",
                "action": "drop_apex_vertex",
                "location_idx": a.location_idx,
                "severity_before": a.severity,
            }
        )
    return drops, manifest


def _oscillation_drops(
    coords: Sequence[tuple[float, float]],
    anomalies: Iterable[Anomaly],
    thresholds: GeometryEnforcerThresholds,
) -> tuple[set[int], list[dict[str, Any]]]:
    """For each OSCILLATION, DP-simplify the flagged ~window-sized slice.

    The anomaly is anchored at the middle of its window; we centre a slice
    of ``oscillation_window_m`` around ``location_idx`` and DP-simplify it
    with ε=20 m. Indices dropped by DP (and not already pinned elsewhere)
    are added to the drop set.
    """
    drops: set[int] = set()
    manifest: list[dict[str, Any]] = []
    n = len(coords)
    if n < 3:
        return drops, manifest

    # Pre-compute cumulative distances (cheap: used only if there are OSC).
    osc = [a for a in anomalies if a.type == "OSCILLATION"]
    if not osc:
        return drops, manifest

    cum = [0.0]
    for i in range(1, n):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        cum.append(cum[-1] + haversine_m(lat_a, lon_a, lat_b, lon_b))
    half = thresholds.oscillation_window_m / 2.0

    for a in osc:
        centre_m = cum[a.location_idx] if 0 <= a.location_idx < n else cum[-1] / 2.0
        win_lo_m = max(0.0, centre_m - half)
        win_hi_m = min(cum[-1], centre_m + half)
        # Find the index range covering this polyline slice.
        lo_idx = 0
        while lo_idx < n - 1 and cum[lo_idx + 1] <= win_lo_m:
            lo_idx += 1
        hi_idx = n - 1
        while hi_idx > 0 and cum[hi_idx - 1] >= win_hi_m:
            hi_idx -= 1
        if hi_idx - lo_idx < 3:
            continue
        kept = _dp_kept_indices(coords, lo_idx, hi_idx, _OSCILLATION_DP_EPS_M)
        dropped_here: list[int] = []
        for k in range(lo_idx + 1, hi_idx):
            if k not in kept:
                drops.add(k)
                dropped_here.append(k)
        if dropped_here:
            manifest.append(
                {
                    "anomaly_type": "OSCILLATION",
                    "action": "dp_simplify_window",
                    "location_idx": a.location_idx,
                    "window_idx_range": [lo_idx, hi_idx],
                    "dp_eps_m": _OSCILLATION_DP_EPS_M,
                    "dropped_count": len(dropped_here),
                    "severity_before": a.severity,
                }
            )
    return drops, manifest


# ---------------------------------------------------------------------------
# Classification comparator.
# ---------------------------------------------------------------------------

def _strictly_better(before: GeometryReport, after: GeometryReport) -> bool:
    b_rank = _CLASSIFICATION_RANK.get(before.classification, 99)
    a_rank = _CLASSIFICATION_RANK.get(after.classification, 99)
    if a_rank < b_rank:
        return True
    if a_rank > b_rank:
        return False
    # Same classification ladder — break tie on anomaly count, then severity.
    if len(after.anomalies) < len(before.anomalies):
        return True
    if len(after.anomalies) > len(before.anomalies):
        return False
    return (after.max_severity + _SEVERITY_EPS) < before.max_severity


# ---------------------------------------------------------------------------
# Public Fixer.
# ---------------------------------------------------------------------------

class GeometryFixer:
    """Produce v2 geometry that strictly improves on v1.

    The Fixer is the only component permitted to modify route shape during
    enhance mode. It never writes to the database; the caller is responsible
    for queueing the v2 candidate + fix manifest.
    """

    def __init__(
        self,
        thresholds: GeometryEnforcerThresholds = DEFAULT_THRESHOLDS,
    ):
        self.thresholds = thresholds
        self._enforcer = GeometryEnforcer(thresholds)

    def fix(
        self,
        coords: Sequence[tuple[float, float]],
        *,
        route_code: str,
        report_before: Optional[GeometryReport] = None,
    ) -> GeometryFixResult:
        coords_list = [(float(lon), float(lat)) for (lon, lat) in coords]
        n = len(coords_list)

        report_v1 = report_before or self._enforcer.analyze(
            coords_list, route_code=route_code
        )

        if not report_v1.anomalies:
            return GeometryFixResult(
                success=False,
                reason="no_anomalies",
                coords_before=coords_list,
                coords_after=list(coords_list),
                report_before=report_v1,
                report_after=None,
            )

        # Collect drop sets from each strategy.
        drops_spike, man_spike = _spike_drops(report_v1.anomalies)
        drops_dup, man_dup = _duplicate_drops(coords_list)
        drops_loop, man_loop = _loop_drops(report_v1.anomalies, n)
        drops_di, man_di = _direction_inconsistency_drops(report_v1.anomalies, n)
        drops_uturn, man_uturn = _u_turn_drops(report_v1.anomalies)
        drops_osc, man_osc = _oscillation_drops(
            coords_list, report_v1.anomalies, self.thresholds
        )

        all_drops: set[int] = set()
        all_drops |= drops_spike
        all_drops |= drops_dup
        all_drops |= drops_loop
        all_drops |= drops_di
        all_drops |= drops_uturn
        all_drops |= drops_osc

        # Endpoint guard — never drop the terminal vertices.
        all_drops.discard(0)
        if n > 0:
            all_drops.discard(n - 1)
        # Filter out any out-of-range indices defensively.
        all_drops = {i for i in all_drops if 0 <= i < n}

        manifest = (
            man_spike + man_dup + man_loop + man_di + man_uturn + man_osc
        )

        if not all_drops:
            return GeometryFixResult(
                success=False,
                reason="no_change",
                coords_before=coords_list,
                coords_after=list(coords_list),
                report_before=report_v1,
                report_after=None,
                fixes_applied=manifest,
            )

        coords_after = [pt for i, pt in enumerate(coords_list) if i not in all_drops]

        # Degenerate output — not enough vertices for the enforcer to even
        # classify. Treat as regression and keep v1.
        if len(coords_after) < 3:
            return GeometryFixResult(
                success=False,
                reason="would_regress",
                coords_before=coords_list,
                coords_after=list(coords_list),
                report_before=report_v1,
                report_after=None,
                fixes_applied=manifest,
                dropped_indices=sorted(all_drops),
            )

        # Endpoint invariant — constructed above but assert defensively.
        assert coords_after[0] == coords_list[0]
        assert coords_after[-1] == coords_list[-1]

        report_v2 = self._enforcer.analyze(coords_after, route_code=route_code)

        if _strictly_better(report_v1, report_v2):
            return GeometryFixResult(
                success=True,
                reason="improved",
                coords_before=coords_list,
                coords_after=coords_after,
                report_before=report_v1,
                report_after=report_v2,
                fixes_applied=manifest,
                dropped_indices=sorted(all_drops),
            )

        return GeometryFixResult(
            success=False,
            reason="would_regress",
            coords_before=coords_list,
            coords_after=list(coords_list),
            report_before=report_v1,
            report_after=report_v2,
            fixes_applied=manifest,
            dropped_indices=sorted(all_drops),
        )


# ---------------------------------------------------------------------------
# Convenience wrapper.
# ---------------------------------------------------------------------------
# Phase 2 strategy — sequence coherence optimizer.
# ---------------------------------------------------------------------------
#
# Invoked when local-drop strategies cannot eliminate
# DIRECTION_INCONSISTENCY / OSCILLATION / corridor IMPOSSIBLE_LOOP because
# the stops are in the wrong order. Reorders stops, retraces via Valhalla,
# promotes the permutation with the best coherence_score.
# Caller owns the Fixer result wrapping — this returns a stand-alone
# ``OptimizeResult``.

from hades.enforcers.sequence_coherence_optimizer import (  # noqa: E402
    OptimizeResult,
    optimize_sequence,
)


def _should_trigger_sequence_optimizer(report: GeometryReport) -> tuple[bool, str]:
    from hades.enforcers.sequence_coherence_optimizer import _anomaly_location_label
    has_di_severe = any(
        a.type == "DIRECTION_INCONSISTENCY" and float(a.severity) > 0.6
        for a in report.anomalies
    )
    has_osc = any(a.type == "OSCILLATION" for a in report.anomalies)
    has_corridor_loop = any(
        a.type == "IMPOSSIBLE_LOOP"
        and "corridor" in _anomaly_location_label(a).lower()
        for a in report.anomalies
    )
    if has_di_severe:
        return True, "direction_inconsistency_severe"
    if has_osc:
        return True, "oscillation"
    if has_corridor_loop:
        return True, "impossible_loop_in_corridor"
    return False, "no_trigger"


def fix_sequence_incoherence(
    stops: Sequence[tuple[float, float]],      # (lat, lon)
    coords_v1: Sequence[tuple[float, float]],  # (lon, lat)
    *,
    route_code: str = "unknown",
    valhalla_costing_options: Optional[dict] = None,
    max_retraces: int = 20,
    timeout_s: float = 60.0,
    thresholds: GeometryEnforcerThresholds = DEFAULT_THRESHOLDS,
    enforcer: Optional[GeometryEnforcer] = None,
    retrace_fn: Optional[Any] = None,
    force: bool = False,
    report_before: Optional[GeometryReport] = None,
) -> Optional[OptimizeResult]:
    """Phase 2 Fixer strategy. Returns None iff trigger condition not met."""
    _enforcer = enforcer or GeometryEnforcer(thresholds=thresholds)
    report = report_before or _enforcer.analyze(list(coords_v1), route_code=route_code)
    if not force:
        should, _ = _should_trigger_sequence_optimizer(report)
        if not should:
            return None
    return optimize_sequence(
        stops=stops, coords_v1=coords_v1, route_code=route_code,
        valhalla_costing_options=valhalla_costing_options,
        max_retraces=max_retraces, timeout_s=timeout_s,
        thresholds=thresholds, retrace_fn=retrace_fn, enforcer=_enforcer,
    )


# ---------------------------------------------------------------------------

def fix_shape(
    coords: Sequence[tuple[float, float]],
    *,
    route_code: str,
    thresholds: GeometryEnforcerThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    return GeometryFixer(thresholds).fix(coords, route_code=route_code).to_dict()


__all__ = [
    "GeometryFixer",
    "GeometryFixResult",
    "fix_shape",
]
