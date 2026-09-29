"""HADES Sequence Coherence Optimizer (Fixer Phase 2 strategy).

⚠ **DEPRECATED / EXPERIMENTAL — DO NOT USE IN PRODUCTION (2026-04-22).**
Empirical eval on 116 QC routes (3-way comparison: bipartition vs 2-opt vs
simulated annealing) showed all three approaches added +3 to +8.64 anomalies
per route on average. The hypothesis "reordering stops eliminates zigzags"
did not survive contact with real data — Valhalla retraces through reordered
stops generate NEW anomalies in the new corridors. Kept in repo for
reference; do not enable ``--enable-sequence-optimizer`` without re-evaluating.


Triggered when Geometry Enforcer v3 flags sequence incoherence on the
existing stop order:

- any ``DIRECTION_INCONSISTENCY`` with severity > 0.6
- any ``OSCILLATION``
- ``IMPOSSIBLE_LOOP`` in corridor (secondary: after local-drop fixers failed)

Strategy: permute the stop order so the route walks the corridor once-
through (ida + vuelta halves split by the argmax-distance pivot), retrace
via Valhalla, and pick the permutation whose retraced polyline scores the
highest on a coherence metric.

Two paths:

  1. **Direction-vector bipartition** (primary). One retrace. The pivot
     point splits stops into an ida half (front leg) and a vuelta half
     (return leg). Each half is sorted by progress along its dominant
     vector. This is O(N) and usually enough.

  2. **2-opt local search** (fallback). Activated only when primary's
     coherence_score < 0.7. Swaps stop pairs greedily and retraces
     incrementally. Bounded by ``max_retraces`` (default 20) and a
     wall-clock timeout (default 60 s).

Coherence score (per spec):

    1.0
     - 0.4 * norm(direction_inconsistency_count)
     - 0.3 * norm(oscillation_count)
     - 0.2 * norm(impossible_loop_in_corridor_count)
     - 0.1 * norm(average_turn_angle / 90°)

Normalization maps each metric into [0, 1] with clamping.

Hard constraints (enforced internally, safe to call unattended):

- Max retraces per route (default 20) → abort and return best-so-far.
- Max wall clock per route (default 60 s) → timeout, return best-so-far.
- Valhalla retrace uses ``phase3_routes…valhalla_client.valhalla_route``
  with the caller-supplied ``costing_options`` so re-traces match the
  original ``valhalla_request`` metadata (same costing, same config).
- Never writes to the DB. Caller owns v2 swap and fix-manifest.

The optimizer is **opt-in** — callers must explicitly pass a
``trigger_condition`` other than ``"disabled"`` or set ``enabled=True``
when wiring into the Fixer.
"""
from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_SRC = ROOT / "phase3_routes" / "services" / "route_constructor" / "src"
if str(_SETTINGS_SRC) not in sys.path:
    sys.path.insert(0, str(_SETTINGS_SRC))

from hades.enforcers.geometry_enforcer import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    GeometryEnforcer,
    GeometryReport,
    GeometryEnforcerThresholds,
    turn_angle_deg,
)


# The Valhalla client lives in the Constructor V2 tree. Optional import so
# unit tests can stub it without requiring a live Valhalla at module load.
try:  # pragma: no cover — network-side import
    from geometry.valhalla_client import valhalla_route as _live_valhalla_route
except Exception:  # pragma: no cover
    _live_valhalla_route = None


LonLat = tuple[float, float]  # (lon, lat) — GeoJSON order
LatLon = tuple[float, float]  # (lat, lon) — matches stop_coords everywhere


# Type alias for a retrace callable so tests can inject a stub.
RetraceFn = Callable[[list[LonLat], Optional[dict]], list[LonLat]]


# ---------------------------------------------------------------------------
# Coherence score.
# ---------------------------------------------------------------------------

def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _normalize_count(n: int, ceiling: int) -> float:
    """Map an anomaly count into [0, 1]. `ceiling` is "clearly awful"."""
    if ceiling <= 0:
        return 0.0
    return _clamp01(float(n) / float(ceiling))


def _average_turn_angle_deg(coords: Sequence[LonLat]) -> float:
    if len(coords) < 3:
        return 0.0
    total = 0.0
    count = 0
    for i in range(1, len(coords) - 1):
        # turn_angle_deg takes (p_prev, p_curr, p_next) each as (lon, lat).
        total += turn_angle_deg(coords[i - 1], coords[i], coords[i + 1])
        count += 1
    return total / max(1, count)


def _anomaly_location_label(a) -> str:
    """Pull the 'location' label off an Anomaly defensively — ``context``
    is typically a dict but older shapes sometimes have it as a bare string
    or None."""
    ctx = getattr(a, "context", None)
    if isinstance(ctx, dict):
        return str(ctx.get("location") or "")
    if isinstance(ctx, str):
        return ctx
    return ""


def coherence_score(report: GeometryReport, coords: Sequence[LonLat]) -> float:
    """Compute coherence in [0, 1]. Higher = more coherent."""
    dir_inc = sum(1 for a in report.anomalies if a.type == "DIRECTION_INCONSISTENCY")
    osc = sum(1 for a in report.anomalies if a.type == "OSCILLATION")
    imp_loop_corridor = sum(
        1 for a in report.anomalies
        if a.type == "IMPOSSIBLE_LOOP"
        and "corridor" in _anomaly_location_label(a).lower()
    )
    avg_turn = _average_turn_angle_deg(coords)

    score = 1.0
    score -= 0.4 * _normalize_count(dir_inc, ceiling=6)
    score -= 0.3 * _normalize_count(osc, ceiling=4)
    score -= 0.2 * _normalize_count(imp_loop_corridor, ceiling=3)
    score -= 0.1 * _clamp01(avg_turn / 90.0)
    return _clamp01(score)


# ---------------------------------------------------------------------------
# Direction-vector bipartition.
# ---------------------------------------------------------------------------

def _haversine_m(a: LatLon, b: LatLon) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    R = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _unit_vector(a: LatLon, b: LatLon) -> tuple[float, float]:
    """Planar east/north unit vector in degrees (suitable for short segments)."""
    dlon = b[1] - a[1]
    dlat = b[0] - a[0]
    n = math.hypot(dlon, dlat)
    if n < 1e-12:
        return (1.0, 0.0)
    return (dlon / n, dlat / n)


def _proj(p: LatLon, origin: LatLon, v: tuple[float, float]) -> float:
    """Scalar projection of (p - origin) onto v (in degrees, same frame as v)."""
    dlon = p[1] - origin[1]
    dlat = p[0] - origin[0]
    return dlon * v[0] + dlat * v[1]


def bipartition_by_direction(
    stops: Sequence[LatLon],
) -> tuple[list[int], list[int], int]:
    """Split stop indices into (ida_idxs, vuelta_idxs, pivot_idx).

    Algorithm:
      1. pivot_idx = argmax_i haversine(stops[0], stops[i])
      2. v_ida     = unit(stops[pivot] - stops[0])
      3. v_vuelta  = unit(stops[-1] - stops[pivot])
      4. For each stop i != 0, pivot, last: compute
          s_ida    = proj(stops[i] - stops[0],    v_ida)
          s_vuelta = proj(stops[i] - stops[pivot], v_vuelta)
         Assign to ida if s_ida >= s_vuelta else vuelta.
      5. Order ida by s_ida ascending (start → pivot).
      6. Order vuelta by s_vuelta ascending (pivot → end).
      7. Endpoints always bracket: ida starts with 0, vuelta ends with N-1.
    """
    n = len(stops)
    if n < 3:
        return list(range(n)), [], 0

    start = stops[0]
    end = stops[-1]

    # Pivot: farthest from start by haversine.
    best_d = -1.0
    pivot_idx = n // 2
    for i in range(1, n - 1):
        d = _haversine_m(start, stops[i])
        if d > best_d:
            best_d, pivot_idx = d, i

    pivot = stops[pivot_idx]
    v_ida = _unit_vector(start, pivot)
    v_vuelta = _unit_vector(pivot, end)

    ida_with_score: list[tuple[float, int]] = [(0.0, 0)]
    vuelta_with_score: list[tuple[float, int]] = []

    for i in range(1, n - 1):
        if i == pivot_idx:
            ida_with_score.append((_proj(stops[i], start, v_ida), i))
            continue
        s_ida = _proj(stops[i], start, v_ida)
        s_vuelta = _proj(stops[i], pivot, v_vuelta)
        # If the stop is markedly past the pivot along v_ida AND has positive
        # forward projection on v_vuelta, assign to vuelta.
        if s_vuelta > 0 and s_ida >= _haversine_m(start, pivot) * 0.70 / 111_000.0:
            vuelta_with_score.append((s_vuelta, i))
        elif s_ida >= s_vuelta:
            ida_with_score.append((s_ida, i))
        else:
            vuelta_with_score.append((s_vuelta, i))

    vuelta_with_score.append((_proj(end, pivot, v_vuelta), n - 1))

    ida_with_score.sort(key=lambda t: t[0])
    vuelta_with_score.sort(key=lambda t: t[0])

    ida_idxs = [i for (_, i) in ida_with_score]
    vuelta_idxs = [i for (_, i) in vuelta_with_score]
    return ida_idxs, vuelta_idxs, pivot_idx


# ---------------------------------------------------------------------------
# Retrace primitive.
# ---------------------------------------------------------------------------

def _default_retrace(locations: list[LonLat], costing_options: Optional[dict]) -> list[LonLat]:
    """Live Valhalla retrace. Raises if the client isn't importable."""
    if _live_valhalla_route is None:
        raise RuntimeError(
            "Valhalla client not importable — supply a retrace_fn stub "
            "or set up phase3_routes.services.route_constructor on sys.path."
        )
    return _live_valhalla_route(locations, costing_options=costing_options)


def _retrace_ordered(
    ordered_stops: Sequence[LatLon],
    costing_options: Optional[dict],
    retrace_fn: RetraceFn,
) -> list[LonLat]:
    """stop_coords are (lat, lon); Valhalla expects (lon, lat)."""
    locs = [(float(lon), float(lat)) for (lat, lon) in ordered_stops]
    return list(retrace_fn(locs, costing_options))


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class OptimizeResult:
    success: bool
    reason: str  # "improved" | "already_coherent" | "would_regress" | "timeout" | "retrace_cap"
    stops_before: list[LatLon]
    stops_after: list[LatLon]
    new_order_indices: list[int]
    coords_before: list[LonLat]
    coords_after: list[LonLat]
    coherence_before: float
    coherence_after: float
    report_before: Optional[GeometryReport] = None
    report_after: Optional[GeometryReport] = None
    strategy_used: str = "none"  # "primary_bipartition" | "two_opt" | "none"
    retraces_used: int = 0
    elapsed_s: float = 0.0
    notes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": bool(self.success),
            "reason": self.reason,
            "n_stops_before": len(self.stops_before),
            "n_stops_after": len(self.stops_after),
            "new_order_indices": list(self.new_order_indices),
            "coherence_before": round(self.coherence_before, 4),
            "coherence_after": round(self.coherence_after, 4),
            "strategy_used": self.strategy_used,
            "retraces_used": int(self.retraces_used),
            "elapsed_s": round(self.elapsed_s, 2),
            "notes": dict(self.notes),
        }


def optimize_sequence(
    *,
    stops: Sequence[LatLon],
    coords_v1: Sequence[LonLat],
    route_code: str = "unknown",
    valhalla_costing_options: Optional[dict] = None,
    fallback_threshold: float = 0.7,
    max_retraces: int = 20,
    timeout_s: float = 60.0,
    thresholds: GeometryEnforcerThresholds = DEFAULT_THRESHOLDS,
    retrace_fn: Optional[RetraceFn] = None,
    enforcer: Optional[GeometryEnforcer] = None,
) -> OptimizeResult:
    """Permute the stop order and retrace to maximise coherence_score.

    Inputs:
      - ``stops``: v1 stop list, each as (lat, lon).
      - ``coords_v1``: v1 polyline, each as (lon, lat).
      - ``valhalla_costing_options``: mirror from the original
        ``valhalla_request`` so retraces match the original costing.
      - ``retrace_fn``: override for tests (returns the polyline for a
        given ordered stop list). Default hits live Valhalla.

    Output: :class:`OptimizeResult`. ``success=True`` only when the
    retraced v2 beats v1 on coherence; otherwise v1 is preserved.
    """
    _enforcer = enforcer or GeometryEnforcer(thresholds=thresholds)
    _retrace = retrace_fn or _default_retrace

    t_start = time.monotonic()
    stops_list: list[LatLon] = [(float(a), float(b)) for (a, b) in stops]
    coords_list: list[LonLat] = [(float(a), float(b)) for (a, b) in coords_v1]
    n = len(stops_list)

    report_v1 = _enforcer.analyze(coords_list, route_code=route_code)
    score_v1 = coherence_score(report_v1, coords_list)

    if n < 3:
        return OptimizeResult(
            success=False, reason="too_few_stops",
            stops_before=stops_list, stops_after=list(stops_list),
            new_order_indices=list(range(n)),
            coords_before=coords_list, coords_after=list(coords_list),
            coherence_before=score_v1, coherence_after=score_v1,
            report_before=report_v1, report_after=None,
            strategy_used="none", retraces_used=0,
            elapsed_s=time.monotonic() - t_start,
            notes={"n_stops": n},
        )

    retraces = 0
    best_order = list(range(n))
    best_stops = list(stops_list)
    best_coords = list(coords_list)
    best_report = report_v1
    best_score = score_v1

    def _elapsed() -> float:
        return time.monotonic() - t_start

    def _budget_left() -> bool:
        return retraces < max_retraces and _elapsed() < timeout_s

    # --- Primary: direction-vector bipartition ----------------------------
    ida, vuelta, pivot = bipartition_by_direction(stops_list)
    primary_order = ida + vuelta
    # Dedup while preserving order (bipartition endpoints may overlap on 0/N-1)
    seen: set[int] = set()
    primary_order = [i for i in primary_order if not (i in seen or seen.add(i))]
    # Ensure endpoints are present exactly once in the expected positions
    if primary_order and primary_order[0] != 0:
        primary_order = [0] + [i for i in primary_order if i != 0]
    if primary_order and primary_order[-1] != n - 1:
        primary_order = [i for i in primary_order if i != n - 1] + [n - 1]

    strategy_used = "primary_bipartition"

    if primary_order != list(range(n)) and _budget_left():
        try:
            reordered = [stops_list[i] for i in primary_order]
            new_coords = _retrace_ordered(reordered, valhalla_costing_options, _retrace)
            retraces += 1
            new_report = _enforcer.analyze(new_coords, route_code=route_code)
            new_score = coherence_score(new_report, new_coords)
            if new_score > best_score + 1e-6:
                best_order = primary_order
                best_stops = reordered
                best_coords = new_coords
                best_report = new_report
                best_score = new_score
        except Exception as exc:  # Valhalla failure, timeout, etc.
            return OptimizeResult(
                success=False, reason=f"primary_retrace_error:{type(exc).__name__}",
                stops_before=stops_list, stops_after=list(stops_list),
                new_order_indices=list(range(n)),
                coords_before=coords_list, coords_after=list(coords_list),
                coherence_before=score_v1, coherence_after=score_v1,
                report_before=report_v1, report_after=None,
                strategy_used="primary_bipartition_error",
                retraces_used=retraces, elapsed_s=_elapsed(),
                notes={"error": str(exc)},
            )

    # --- Fallback: 2-opt local search -------------------------------------
    if best_score < fallback_threshold and _budget_left():
        strategy_used = "two_opt"
        current_order = list(best_order)
        current_stops = list(best_stops)
        current_score = best_score
        iters = 0
        improved = True
        while improved and _budget_left() and iters < 50:
            improved = False
            iters += 1
            for i in range(1, n - 1):
                for j in range(i + 1, n - 1):
                    if not _budget_left():
                        break
                    trial = list(current_order)
                    trial[i], trial[j] = trial[j], trial[i]
                    try:
                        trial_stops = [stops_list[k] for k in trial]
                        trial_coords = _retrace_ordered(
                            trial_stops, valhalla_costing_options, _retrace
                        )
                        retraces += 1
                    except Exception:
                        continue
                    trial_report = _enforcer.analyze(trial_coords, route_code=route_code)
                    trial_score = coherence_score(trial_report, trial_coords)
                    if trial_score > current_score + 1e-6:
                        current_order = trial
                        current_stops = trial_stops
                        current_score = trial_score
                        improved = True
                        if trial_score > best_score:
                            best_order = trial
                            best_stops = trial_stops
                            best_coords = trial_coords
                            best_report = trial_report
                            best_score = trial_score
                if not _budget_left():
                    break

    # --- Validate + decide ------------------------------------------------
    elapsed = _elapsed()
    if retraces >= max_retraces:
        reason_base = "retrace_cap"
    elif elapsed >= timeout_s:
        reason_base = "timeout"
    else:
        reason_base = "explored"

    # Strict-better gate: must beat v1 coherence.
    if best_score > score_v1 + 1e-6 and best_order != list(range(n)):
        return OptimizeResult(
            success=True,
            reason=f"improved({reason_base})",
            stops_before=stops_list,
            stops_after=best_stops,
            new_order_indices=best_order,
            coords_before=coords_list,
            coords_after=best_coords,
            coherence_before=score_v1,
            coherence_after=best_score,
            report_before=report_v1,
            report_after=best_report,
            strategy_used=strategy_used,
            retraces_used=retraces,
            elapsed_s=elapsed,
            notes={"pivot_idx": pivot},
        )

    return OptimizeResult(
        success=False,
        reason=("already_coherent" if best_score >= score_v1 - 1e-6 else "would_regress")
               + f"({reason_base})",
        stops_before=stops_list,
        stops_after=list(stops_list),
        new_order_indices=list(range(n)),
        coords_before=coords_list,
        coords_after=list(coords_list),
        coherence_before=score_v1,
        coherence_after=score_v1,
        report_before=report_v1,
        report_after=best_report,
        strategy_used=strategy_used,
        retraces_used=retraces,
        elapsed_s=elapsed,
        notes={"best_score_seen": best_score, "pivot_idx": pivot},
    )


__all__ = [
    "OptimizeResult",
    "RetraceFn",
    "bipartition_by_direction",
    "coherence_score",
    "optimize_sequence",
]
