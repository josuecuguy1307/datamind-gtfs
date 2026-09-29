"""HADES Geometry Enforcer — v3 (direction-vector redesign).

The geodesy primitives below are thin re-exports from
``hades.geometry.canonical`` (the single source of truth per
``workspace/skills/direction_construction.md``). They remain importable
from this module so existing call sites keep working.


Inspects a route shape (list of (lon, lat) points) and surfaces anomalies
that indicate the trace is implausible as a real bus route.

v1 history (kept here so future archaeologists can trace the design):
The first iteration used five geometry-only detectors: U_TURN,
IMPOSSIBLE_LOOP, ZIGZAG, BACKTRACK, SPIKE. Running over all 1,112 routes
in ``route_prod.routes`` flagged 80.4 % severe. Visual inspection of the
calibration sample (see ``workspace/diagnostics/geometry_calibration_viz.html``)
showed the bucket-B and bucket-C routes were NOT broken — they were normal
ida-vuelta bus routes. The enforcer was conflating:

- Same-avenue backtracks at the ida→vuelta pivot with mid-corridor
  phantom reversals (both fired BACKTRACK).
- Terminal-yard hairpins with corridor U-turns (both fired U_TURN).
- Dense-intersection urban zig-zag with pathological oscillation (both
  fired ZIGZAG).

The physical insight from the user that drove the v3 redesign:

    Within each half (ida or vuelta), the direction vector must remain
    relatively constant. A bus does NOT advance 500 m, reverse 300 m,
    advance 500 m, reverse 200 m within the same half. Shape validity =
    direction monotonicity within each half + a single legitimate pivot.

v3 introduces:

- **Pivot detection** — the farthest point from the start is the
  ida→vuelta pivot. Falls back to a sliding-window dot-product scan if
  the pivot lands outside the [30 %, 70 %] index range (non-standard
  topology).
- **Dominant-direction vectors** — one unit vector per half, from
  start → pivot and pivot → end.
- **Segment alignment** — cosine of each segment against its half's
  dominant vector. A run of segments with alignment < -0.5 that is
  outside the terminal and pivot zones is a DIRECTION_INCONSISTENCY —
  the SANPEDROAMAGUANA phantom-square pattern.
- **Terminal zone** (300 m from each endpoint) and **pivot zone**
  (100 m either side of the pivot) — U_TURN and IMPOSSIBLE_LOOP do
  NOT fire inside these zones, because bus terminals legitimately
  contain sharp turns and the pivot is an unavoidable topological
  reversal.
- **OSCILLATION** replaces ZIGZAG — only fires if the alignment flips
  sign ≥ 4 times within a 1 km window AND at least one flip coincides
  with an IMPOSSIBLE_LOOP. Raw urban-grid oscillation alone is fine;
  oscillation + loop is the pathology.

SPIKE is unchanged — it is the only pure-signal rule (single vertex off
the chord to its neighbours; always a map-match / GPS glitch).

All detector thresholds live in :class:`GeometryEnforcerThresholds`.
Severity is normalized to [0.0, 1.0]. The enforcer is read-only and
stateless — it receives a polyline and returns a :class:`GeometryReport`.

Consolidates the ``haversine_m`` / ``bearing_deg`` / ``angular_delta_deg``
utilities identified in the Phase 3 architecture audit — do not duplicate
elsewhere in HADES code paths; import from here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence


# ---------------------------------------------------------------------------
# Geodesy primitives — re-exported from hades.geometry.canonical.
# ---------------------------------------------------------------------------

from hades.geometry.canonical import (
    EARTH_RADIUS_M,
    angular_delta_deg,
    bearing_deg,
    haversine_m,
)


def turn_angle_deg(
    p_prev: tuple[float, float],
    p_curr: tuple[float, float],
    p_next: tuple[float, float],
) -> float:
    """Turn angle at `p_curr` between the incoming and outgoing segment.

    Points are (lon, lat). A straight line returns 0°, a sharp U-turn 180°.
    """
    lon_a, lat_a = p_prev
    lon_b, lat_b = p_curr
    lon_c, lat_c = p_next
    incoming = bearing_deg(lat_a, lon_a, lat_b, lon_b)
    outgoing = bearing_deg(lat_b, lon_b, lat_c, lon_c)
    return angular_delta_deg(incoming, outgoing)


def perpendicular_distance_m(
    point: tuple[float, float],
    seg_a: tuple[float, float],
    seg_b: tuple[float, float],
) -> float:
    """Great-circle perpendicular distance from ``point`` to the chord a→b.

    Uses a local equirectangular projection; fine for sub-kilometre chords
    at mid-latitudes (error < 0.1 % for spike detection).
    """
    lon0, lat0 = point
    lon_a, lat_a = seg_a
    lon_b, lat_b = seg_b
    lat_ref = 0.5 * (lat_a + lat_b)
    m_per_deg_lat = 111_132.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat_ref))
    x0 = (lon0 - lon_a) * m_per_deg_lon
    y0 = (lat0 - lat_a) * m_per_deg_lat
    xb = (lon_b - lon_a) * m_per_deg_lon
    yb = (lat_b - lat_a) * m_per_deg_lat
    seg_len_sq = xb * xb + yb * yb
    if seg_len_sq < 1e-9:
        return math.hypot(x0, y0)
    t = (x0 * xb + y0 * yb) / seg_len_sq
    t = max(0.0, min(1.0, t))
    proj_x = t * xb
    proj_y = t * yb
    return math.hypot(x0 - proj_x, y0 - proj_y)


# ---------------------------------------------------------------------------
# Direction-vector primitives (v3). Vectors live in a local metre frame
# centred on the polyline midpoint so we can dot-product them directly.
# For sub-metropolitan routes (< 200 km) this is sufficient; longer routes
# keep direction-coherence because dominant vectors absorb the curvature.
# ---------------------------------------------------------------------------

from hades.geometry.canonical import _M_PER_DEG_LAT, _m_per_deg_lon  # noqa: E402,F401


def _to_metres(
    point: tuple[float, float], lat_ref: float, m_per_deg_lon: float
) -> tuple[float, float]:
    """(lon, lat) → (x_m, y_m) in a local equirectangular frame."""
    lon, lat = point
    return (lon * m_per_deg_lon, lat * _M_PER_DEG_LAT)


def _unit_vec(vx: float, vy: float) -> tuple[float, float]:
    mag = math.hypot(vx, vy)
    if mag < 1e-9:
        return (0.0, 0.0)
    return (vx / mag, vy / mag)


def detect_pivot(
    coords: Sequence[tuple[float, float]],
    *,
    min_fraction: float = 0.30,
    max_fraction: float = 0.70,
    fallback_window: int = 5,
) -> tuple[int, str]:
    """Return (pivot_index, detection_method).

    Primary: argmax of great-circle distance from ``coords[0]``. For a
    clean ida-vuelta route this is the farthest point — i.e. the turnaround
    terminal. Validation: the argmax must sit between 30 %–70 % of the
    **polyline distance** (not vertex index — index fractions are biased
    by non-uniform sampling density, which is common in real data where
    Valhalla returns dense points around turns and sparse points along
    straights). Outside that band we fall back.

    Fallback (sliding window dot-product): for each interior vertex, compare
    a smoothed backward-window vector against a smoothed forward-window
    vector. The most-negative normalized dot-product marks the pivot; a
    value < -0.5 (> 120° reversal) is considered a valid pivot candidate.
    If no candidate is found we return ``(len(coords) // 2, "fallback_midpoint")``
    so downstream code still has one half each side — this is the
    "non-standard topology" case documented in the v3 spec.
    """
    n = len(coords)
    if n < 3:
        return (max(0, n // 2), "degenerate")

    lon0, lat0 = coords[0]
    best_idx = 0
    best_d = -1.0
    cum = [0.0]
    for i in range(1, n):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        cum.append(cum[-1] + haversine_m(lat_a, lon_a, lat_b, lon_b))
        d_from_start = haversine_m(lat0, lon0, lat_b, lon_b)
        if d_from_start > best_d:
            best_d = d_from_start
            best_idx = i

    total_polyline = cum[-1]
    pivot_polyline_frac = cum[best_idx] / total_polyline if total_polyline > 0 else 0.0
    if min_fraction <= pivot_polyline_frac <= max_fraction:
        return (best_idx, "argmax_distance")

    # Fallback: sliding-window reversal detector.
    N = fallback_window
    if n <= 2 * N + 2:
        return (n // 2, "fallback_midpoint")

    lat_ref = sum(p[1] for p in coords) / n
    m_per_deg_lon = _m_per_deg_lon(lat_ref)

    best_i = -1
    best_dot = 1.0
    for i in range(N, n - N):
        lon_b, lat_b = coords[i - N]
        lon_c, lat_c = coords[i]
        lon_f, lat_f = coords[i + N]
        bx = (lon_c - lon_b) * m_per_deg_lon
        by = (lat_c - lat_b) * _M_PER_DEG_LAT
        fx = (lon_f - lon_c) * m_per_deg_lon
        fy = (lat_f - lat_c) * _M_PER_DEG_LAT
        ubx, uby = _unit_vec(bx, by)
        ufx, ufy = _unit_vec(fx, fy)
        if (ubx, uby) == (0.0, 0.0) or (ufx, ufy) == (0.0, 0.0):
            continue
        dot = ubx * ufx + uby * ufy
        if dot < best_dot:
            best_dot = dot
            best_i = i
    if best_i > 0 and best_dot < -0.5:
        return (best_i, "fallback_dot_product")
    return (n // 2, "fallback_midpoint")


def dominant_vectors(
    coords: Sequence[tuple[float, float]], pivot_idx: int
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return ((ida_ux, ida_uy), (vuelta_ux, vuelta_uy)) in local metres."""
    n = len(coords)
    if n < 2:
        return ((0.0, 0.0), (0.0, 0.0))
    lat_ref = sum(p[1] for p in coords) / n
    m_per_deg_lon = _m_per_deg_lon(lat_ref)

    lon_s, lat_s = coords[0]
    lon_p, lat_p = coords[pivot_idx]
    lon_e, lat_e = coords[-1]
    ida = _unit_vec(
        (lon_p - lon_s) * m_per_deg_lon, (lat_p - lat_s) * _M_PER_DEG_LAT
    )
    vuelta = _unit_vec(
        (lon_e - lon_p) * m_per_deg_lon, (lat_e - lat_p) * _M_PER_DEG_LAT
    )
    return (ida, vuelta)


def segment_alignments(
    coords: Sequence[tuple[float, float]], pivot_idx: int
) -> list[float]:
    """Return alignment in [-1, +1] for each segment ``i → i+1``.

    alignment_i = cos(angle between segment_i and its half's dominant vector).
    The pivot segment itself is placed in the ida half (arbitrary but
    deterministic; doesn't affect DIRECTION_INCONSISTENCY firing because
    pivot zone is excluded).
    """
    n_seg = max(0, len(coords) - 1)
    if n_seg == 0:
        return []
    (ida, vuelta) = dominant_vectors(coords, pivot_idx)
    lat_ref = sum(p[1] for p in coords) / len(coords)
    m_per_deg_lon = _m_per_deg_lon(lat_ref)

    out: list[float] = []
    for i in range(n_seg):
        lon_a, lat_a = coords[i]
        lon_b, lat_b = coords[i + 1]
        sx = (lon_b - lon_a) * m_per_deg_lon
        sy = (lat_b - lat_a) * _M_PER_DEG_LAT
        usx, usy = _unit_vec(sx, sy)
        dom = ida if i < pivot_idx else vuelta
        if (usx, usy) == (0.0, 0.0) or dom == (0.0, 0.0):
            out.append(0.0)
            continue
        out.append(usx * dom[0] + usy * dom[1])
    return out


# ---------------------------------------------------------------------------
# Thresholds.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GeometryEnforcerThresholds:
    # --- U_TURN (now corridor-only: terminal + pivot zones excluded) ---
    # Raised from 150° → 165° after v1 calibration showed many 150°–160°
    # vertices were legitimate hairpins on dense urban streets. Keep the
    # 30° normalizer — at 195° a theoretical measurement would max out,
    # but real ≤180° caps severity around 0.5.
    u_turn_angle_deg: float = 165.0
    u_turn_severity_full_scale_deg: float = 30.0

    # --- IMPOSSIBLE_LOOP (corridor-only, tightened for v3) ---
    # v1 defaults (50 m proximity, 200 m min travel) flagged every dense-
    # urban block detour as a loop. Bus routes legitimately circle around
    # one-way blocks (200-300 m travel, chord 30-50 m). Tightening to
    # 20 m proximity + 300 m min travel means we only fire on geometry
    # that could not be a road at all: return to within 20 m after more
    # than 300 m of travel. Zone filter is applied at firing time —
    # loops whose closure sits inside the terminal or pivot zone are
    # suppressed because bus terminals legitimately contain small loops.
    loop_proximity_m: float = 20.0
    loop_polyline_budget_m: float = 500.0
    loop_polyline_min_m: float = 300.0

    # --- DIRECTION_INCONSISTENCY (replaces BACKTRACK) ---
    # A "run" of consecutive segments whose alignment against the
    # half's dominant vector is below -0.5 (i.e. heading more than
    # 120° against the dominant ida/vuelta direction). If the run's
    # polyline length exceeds 150 m AND the run sits outside the
    # terminal + pivot zones, it fires. Severity saturates at 500 m
    # of antidirectional travel.
    direction_alignment_threshold: float = -0.5
    direction_run_min_m: float = 150.0
    direction_severity_full_scale_m: float = 500.0

    # --- OSCILLATION (replaces ZIGZAG) ---
    # Within a sliding 1 km window, count alignment sign-flips where
    # both sides of the flip have magnitude > 0.3 (ignore tiny wiggles).
    # Fires only if the window contains ≥ 4 such flips AND at least one
    # flip coincides (within the same window) with an IMPOSSIBLE_LOOP.
    # The coincidence requirement is what distinguishes urban-grid
    # normal oscillation from pathological corridor failure.
    oscillation_window_m: float = 1000.0
    oscillation_flip_magnitude: float = 0.3
    oscillation_max_flips_per_window: int = 4
    oscillation_severity_full_scale_extra_flips: int = 4

    # --- SPIKE (unchanged) ---
    # A vertex > 200 m off the chord between its immediate neighbours.
    # Full-scale severity at 800 m off-chord (extreme map-match glitch).
    spike_offset_m: float = 200.0
    spike_severity_full_scale_m: float = 800.0

    # --- Zone sizes ---
    # Bus terminals legitimately have hairpins, cul-de-sac turnarounds,
    # and loops around yard gates. Excluding 300 m from each endpoint
    # absorbs almost every terminal footprint seen in Quito's inventory
    # (largest visible terminal yard in the calibration sample was
    # ~250 m across).
    terminal_zone_m: float = 300.0
    # The ida→vuelta pivot itself is a topologically required
    # reversal. A ±100 m pivot zone lets the reversal happen without
    # firing U_TURN or DIRECTION_INCONSISTENCY.
    pivot_zone_m: float = 100.0

    # --- Classification ladder (v3, calibrated on Quito bucket B/C) ---
    # A single IMPOSSIBLE_LOOP on a dense urban bus route may still be
    # legitimate (roundabout + terminal pass-through + plaza return).
    # The real pathology is a CLUSTER of them. We treat IMPOSSIBLE_LOOP
    # as severe only when either:
    #   (1) exact-chord repeats (chord_m = 0) — a true duplicate-vertex
    #       pathology, or
    #   (2) more than ``loop_cluster_threshold`` loops in the whole route
    #       (dense cluster indicating systematic bad geometry, like the
    #       SANPEDROAMAGUANA phantom-square family).
    # Otherwise IMPOSSIBLE_LOOP lands in moderate.
    direction_severe_severity: float = 0.6
    spike_severe_offset_m: float = 300.0
    moderate_min_anomalies: int = 2
    oscillation_moderate_severity: float = 0.5
    loop_cluster_threshold: int = 5


DEFAULT_THRESHOLDS = GeometryEnforcerThresholds()


# ---------------------------------------------------------------------------
# Output schema.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Anomaly:
    type: str
    severity: float
    location_idx: int
    coords: tuple[float, float]  # (lat, lon)
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "severity": round(float(self.severity), 4),
            "location_idx": int(self.location_idx),
            "coords": [round(float(self.coords[0]), 6), round(float(self.coords[1]), 6)],
            "context": self.context,
        }


@dataclass(slots=True)
class GeometryReport:
    route_code: str
    version: int
    anomalies: list[Anomaly] = field(default_factory=list)
    classification: str = "clean"
    max_severity: float = 0.0
    pivot_idx: int = -1
    pivot_method: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_code": self.route_code,
            "version": int(self.version),
            "anomalies": [a.to_dict() for a in self.anomalies],
            "summary": {
                "total_anomalies": len(self.anomalies),
                "max_severity": round(float(self.max_severity), 4),
                "classification": self.classification,
                "pivot_idx": int(self.pivot_idx),
                "pivot_method": self.pivot_method,
            },
        }


# ---------------------------------------------------------------------------
# Core enforcer.
# ---------------------------------------------------------------------------

class GeometryEnforcer:
    """v3 enforcer. See module docstring for the design rationale."""

    VERSION = 3

    def __init__(self, thresholds: GeometryEnforcerThresholds = DEFAULT_THRESHOLDS):
        self.thresholds = thresholds

    # -- Public --------------------------------------------------------

    def analyze(
        self,
        coords: Sequence[tuple[float, float]],
        *,
        route_code: str,
        version: int = VERSION,
    ) -> GeometryReport:
        report = GeometryReport(route_code=route_code, version=version)
        n = len(coords)
        if n < 3:
            report.classification = "clean"
            return report

        pivot_idx, pivot_method = detect_pivot(coords)
        report.pivot_idx = pivot_idx
        report.pivot_method = pivot_method

        # Cumulative polyline distance (used by multiple detectors + zone tests).
        cum = self._cumulative_m(coords)
        terminal_start_m = self.thresholds.terminal_zone_m
        terminal_end_m = cum[-1] - self.thresholds.terminal_zone_m
        pivot_cum_m = cum[pivot_idx] if 0 <= pivot_idx < len(cum) else cum[-1] / 2.0
        pivot_lo_m = pivot_cum_m - self.thresholds.pivot_zone_m
        pivot_hi_m = pivot_cum_m + self.thresholds.pivot_zone_m

        def in_protected_zone(vertex_cum_m: float) -> bool:
            if vertex_cum_m <= terminal_start_m:
                return True
            if vertex_cum_m >= terminal_end_m:
                return True
            if pivot_lo_m <= vertex_cum_m <= pivot_hi_m:
                return True
            return False

        alignments = segment_alignments(coords, pivot_idx)

        anomalies: list[Anomaly] = []
        anomalies.extend(self._detect_u_turns(coords, cum, in_protected_zone))
        # Same-half filter: argmax_distance and fallback_dot_product both
        # give a reasonable pivot (the former confident, the latter best-
        # effort). Apply the filter in both cases so natural ida-vuelta
        # adjacency (vuelta leg passing within 20 m of its ida counterpart
        # along the same avenue) does NOT fire IMPOSSIBLE_LOOP. Only
        # fallback_midpoint (where we genuinely have no pivot signal) is
        # treated as "no pivot" so single-corridor routes still fire loops
        # on actual phantom closures.
        trusted_pivot = (
            pivot_idx
            if pivot_method in ("argmax_distance", "fallback_dot_product")
            else -1
        )
        loops = self._detect_impossible_loops(
            coords, cum, in_protected_zone, trusted_pivot
        )
        anomalies.extend(loops)
        # DIRECTION_INCONSISTENCY only applies to confident ida-vuelta
        # topologies. For one-way or unclassified routes, the "dominant
        # direction within each half" premise doesn't hold — a long one-way
        # route legitimately meanders through the street grid.
        if pivot_method == "argmax_distance":
            anomalies.extend(
                self._detect_direction_inconsistency(
                    coords, cum, alignments, pivot_idx, in_protected_zone
                )
            )
        anomalies.extend(
            self._detect_oscillation(coords, cum, alignments, loops)
        )
        anomalies.extend(self._detect_spikes(coords))

        report.anomalies = anomalies
        report.max_severity = max((a.severity for a in anomalies), default=0.0)
        report.classification = self._classify(anomalies)
        return report

    # -- Helpers -------------------------------------------------------

    @staticmethod
    def _cumulative_m(coords: Sequence[tuple[float, float]]) -> list[float]:
        cum = [0.0]
        for i in range(1, len(coords)):
            lon_a, lat_a = coords[i - 1]
            lon_b, lat_b = coords[i]
            cum.append(cum[-1] + haversine_m(lat_a, lon_a, lat_b, lon_b))
        return cum

    # -- Detectors -----------------------------------------------------

    def _detect_u_turns(
        self,
        coords: Sequence[tuple[float, float]],
        cum: list[float],
        in_zone,
    ) -> list[Anomaly]:
        t = self.thresholds
        out: list[Anomaly] = []
        for i in range(1, len(coords) - 1):
            if in_zone(cum[i]):
                continue
            angle = turn_angle_deg(coords[i - 1], coords[i], coords[i + 1])
            if angle > t.u_turn_angle_deg:
                severity = min(
                    1.0, (angle - t.u_turn_angle_deg) / t.u_turn_severity_full_scale_deg
                )
                lon, lat = coords[i]
                out.append(
                    Anomaly(
                        type="U_TURN",
                        severity=severity,
                        location_idx=i,
                        coords=(lat, lon),
                        context=f"turn_angle={angle:.1f}°",
                    )
                )
        return out

    def _detect_impossible_loops(
        self,
        coords: Sequence[tuple[float, float]],
        cum: list[float],
        in_zone,
        pivot_idx: int,
    ) -> list[Anomaly]:
        """A true loop closes within the SAME half (ida or vuelta).

        On a normal ida-vuelta route, any vuelta vertex is geometrically
        near its ida counterpart (sometimes within a few metres if the
        bus returns along the same avenue). That is NOT a loop — it's
        topologically required by the route design. We require the
        closure endpoint ``j`` and the firing vertex ``i`` to live on
        the same side of the pivot; cross-pivot adjacency is filtered
        out silently.
        """
        t = self.thresholds
        out: list[Anomaly] = []
        skip_until = -1
        i = 0
        n_coords = len(coords)
        while i < n_coords:
            if i <= skip_until:
                i += 1
                continue
            j = i - 1
            closure_j: Optional[int] = None
            closure_polyline = 0.0
            closure_chord = 0.0
            while j >= 0 and (cum[i] - cum[j]) <= t.loop_polyline_budget_m:
                travel = cum[i] - cum[j]
                if travel >= t.loop_polyline_min_m and (i - j) >= 2:
                    d = haversine_m(coords[j][1], coords[j][0], coords[i][1], coords[i][0])
                    if d <= t.loop_proximity_m:
                        # Same-half check: reject if j and i straddle the
                        # pivot. pivot_idx == -1 disables the check (fallback
                        # pivot is untrusted → all loops fire).
                        same_half = (
                            pivot_idx < 0
                            or (j < pivot_idx) == (i < pivot_idx)
                        )
                        if same_half:
                            closure_j = j
                            closure_polyline = travel
                            closure_chord = d
                            break
                j -= 1
            if closure_j is not None:
                # Zone filter: suppress if BOTH endpoints are inside a protected zone.
                if in_zone(cum[i]) and in_zone(cum[closure_j]):
                    skip_until = i + max(1, (i - closure_j))
                    i += 1
                    continue
                severity = max(
                    0.0, min(1.0, 1.0 - closure_polyline / t.loop_polyline_budget_m)
                )
                lon, lat = coords[i]
                out.append(
                    Anomaly(
                        type="IMPOSSIBLE_LOOP",
                        severity=severity,
                        location_idx=i,
                        coords=(lat, lon),
                        context=(
                            f"closes_on_idx={closure_j} "
                            f"chord_m={closure_chord:.1f} "
                            f"polyline_m={closure_polyline:.1f}"
                        ),
                    )
                )
                skip_until = i + max(1, (i - closure_j))
            i += 1
        return out

    def _detect_direction_inconsistency(
        self,
        coords: Sequence[tuple[float, float]],
        cum: list[float],
        alignments: list[float],
        pivot_idx: int,
        in_zone,
    ) -> list[Anomaly]:
        """Flag runs of consecutive segments with alignment < threshold.

        The segment alignment_i lives between vertex i and vertex i+1.
        We consider a run flagged if the total polyline length of the
        run exceeds ``direction_run_min_m`` AND the run's midpoint is
        outside the protected (terminal + pivot) zones.
        """
        t = self.thresholds
        out: list[Anomaly] = []
        n_seg = len(alignments)
        if n_seg == 0:
            return out
        run_start: Optional[int] = None
        for i in range(n_seg):
            below = alignments[i] < t.direction_alignment_threshold
            if below and run_start is None:
                run_start = i
            if (not below or i == n_seg - 1) and run_start is not None:
                run_end = i if below and i == n_seg - 1 else i - 1
                if run_end >= run_start:
                    run_len = cum[run_end + 1] - cum[run_start]
                    run_mid_cum = 0.5 * (cum[run_start] + cum[run_end + 1])
                    if run_len >= t.direction_run_min_m and not in_zone(run_mid_cum):
                        severity = min(
                            1.0, run_len / t.direction_severity_full_scale_m
                        )
                        loc = run_start
                        lon, lat = coords[loc]
                        out.append(
                            Anomaly(
                                type="DIRECTION_INCONSISTENCY",
                                severity=severity,
                                location_idx=loc,
                                coords=(lat, lon),
                                context=(
                                    f"anti_direction_run_m={run_len:.1f} "
                                    f"segments=[{run_start},{run_end}] "
                                    f"half={'ida' if run_start < pivot_idx else 'vuelta'}"
                                ),
                            )
                        )
                run_start = None
        return out

    def _detect_oscillation(
        self,
        coords: Sequence[tuple[float, float]],
        cum: list[float],
        alignments: list[float],
        loops: list[Anomaly],
    ) -> list[Anomaly]:
        """Flag 1 km windows with ≥ 4 sign-flips coincident with a loop."""
        t = self.thresholds
        n_seg = len(alignments)
        if n_seg == 0 or not loops:
            return []

        # Flip indices: segment i has a flip if sign(align[i-1]) != sign(align[i])
        # AND both sides have magnitude > oscillation_flip_magnitude.
        flip_positions_m: list[float] = []
        for i in range(1, n_seg):
            a, b = alignments[i - 1], alignments[i]
            if (
                (a > 0 and b < 0 or a < 0 and b > 0)
                and abs(a) > t.oscillation_flip_magnitude
                and abs(b) > t.oscillation_flip_magnitude
            ):
                # Flip happens at vertex i (between seg i-1 and seg i).
                flip_positions_m.append(cum[i])

        loop_positions_m = [cum[a.location_idx] for a in loops]
        out: list[Anomaly] = []
        if len(flip_positions_m) <= t.oscillation_max_flips_per_window:
            return out

        emitted_window_starts: set[int] = set()
        # Slide an explicit 1 km window across the flips and check the count.
        lo = 0
        for hi in range(len(flip_positions_m)):
            while flip_positions_m[hi] - flip_positions_m[lo] > t.oscillation_window_m:
                lo += 1
            count = hi - lo + 1
            if count <= t.oscillation_max_flips_per_window:
                continue
            window_lo = flip_positions_m[lo]
            window_hi = flip_positions_m[hi]
            has_loop = any(window_lo <= lp <= window_hi for lp in loop_positions_m)
            if not has_loop:
                continue
            # Dedupe by window start (in metres rounded to 100m so nearby slides don't re-emit).
            key = int(window_lo // 100)
            if key in emitted_window_starts:
                continue
            emitted_window_starts.add(key)
            extra = count - t.oscillation_max_flips_per_window
            severity = min(
                1.0, extra / max(1, t.oscillation_severity_full_scale_extra_flips)
            )
            # Anchor the anomaly near the middle of the window.
            mid_m = 0.5 * (window_lo + window_hi)
            mid_idx = min(
                range(len(cum)), key=lambda k: abs(cum[k] - mid_m)
            )
            lon, lat = coords[mid_idx]
            out.append(
                Anomaly(
                    type="OSCILLATION",
                    severity=severity,
                    location_idx=mid_idx,
                    coords=(lat, lon),
                    context=(
                        f"flips={count} in {t.oscillation_window_m:.0f}m window "
                        f"coincident_with_loop=true"
                    ),
                )
            )
        return out

    def _detect_spikes(self, coords: Sequence[tuple[float, float]]) -> list[Anomaly]:
        t = self.thresholds
        out: list[Anomaly] = []
        for i in range(1, len(coords) - 1):
            offset = perpendicular_distance_m(coords[i], coords[i - 1], coords[i + 1])
            if offset > t.spike_offset_m:
                severity = min(
                    1.0,
                    max(0.0, (offset - t.spike_offset_m) / t.spike_severity_full_scale_m),
                )
                lon, lat = coords[i]
                out.append(
                    Anomaly(
                        type="SPIKE",
                        severity=severity,
                        location_idx=i,
                        coords=(lat, lon),
                        context=f"offset_from_chord_m={offset:.1f}",
                    )
                )
        return out

    # -- Classification ------------------------------------------------

    def _classify(self, anomalies: Sequence[Anomaly]) -> str:
        t = self.thresholds
        n = len(anomalies)
        if n == 0:
            return "clean"

        loops = [a for a in anomalies if a.type == "IMPOSSIBLE_LOOP"]
        # Loop-cluster pathology: either lots of them, or any exact-chord
        # zero-distance repeats (duplicate-vertex encoding failures).
        loop_cluster = len(loops) > t.loop_cluster_threshold
        loop_exact_repeat = any(
            "chord_m=0.0" in a.context for a in loops
        )

        # DIRECTION_INCONSISTENCY → severe only when co-located with a loop
        # (within 500 m of each other along the polyline). Calibration showed
        # that on long, curved urban corridors the "dominant direction per
        # half" premise breaks down and DI fires 20+ times legitimately. The
        # real pathology (SANPEDROAMAGUANA phantom-square family) has DI and
        # IL clustered at the same spot — that's the coincidence we keep.
        dir_severe_with_loop = False
        for a in anomalies:
            if (
                a.type == "DIRECTION_INCONSISTENCY"
                and a.severity > t.direction_severe_severity
            ):
                for l in loops:
                    if abs(l.location_idx - a.location_idx) <= 50:
                        dir_severe_with_loop = True
                        break
                if dir_severe_with_loop:
                    break

        severe = False
        if loop_cluster or loop_exact_repeat or dir_severe_with_loop:
            severe = True
        if not severe:
            for a in anomalies:
                if a.type == "SPIKE" and a.severity > (
                    (t.spike_severe_offset_m - t.spike_offset_m)
                    / t.spike_severity_full_scale_m
                ):
                    severe = True
                    break
        if severe:
            return "severe"

        moderate = False
        if loops:
            moderate = True
        if any(
            a.type == "OSCILLATION" and a.severity > t.oscillation_moderate_severity
            for a in anomalies
        ):
            moderate = True
        if n >= t.moderate_min_anomalies:
            moderate = True
        if moderate:
            return "moderate"

        return "minor"


# ---------------------------------------------------------------------------
# Convenience wrapper for single-call usage.
# ---------------------------------------------------------------------------

def analyze_shape(
    coords: Sequence[tuple[float, float]],
    *,
    route_code: str,
    version: int = GeometryEnforcer.VERSION,
    thresholds: GeometryEnforcerThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    return GeometryEnforcer(thresholds).analyze(
        coords, route_code=route_code, version=version
    ).to_dict()
