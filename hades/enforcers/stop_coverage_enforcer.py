"""HADES Stop Coverage Enforcer.

Audits a route's stop list for OTP-breaking gaps — consecutive stops whose
polyline distance exceeds what the route's urban/rural zone can tolerate —
and records how each gap can be resolved via a tier-ordered search:

  * **Tier 1 — cross-route borrow**: another route already has a stop near
    this gap midpoint and close to our route corridor. Borrow the node_id.
  * **Tier 2 — OSM POI anchor** (Overpass): Overpass returns a POI
    (`highway=bus_stop`, `public_transport=platform`, or a generic amenity
    anchor) within the buffer. Snap to it.
  * **Tier 4 — DR prepared** (LLM fallback): we *do not* call Deep
    Research from the enforcer; we record a structured query that a
    downstream job can feed into the LLM advisor. `tier=4_prepared`.
  * **Tier 5 — synthetic prepared**: last-resort synthetic fill at the
    gap midpoint; also recorded, never inserted here.

Tier 3 (OSM intersection snap) is deferred — no OSM road layer in prod.

The enforcer is read-only and stateless: it consumes a route polyline,
the current stop coordinates, and two injected resolvers (cross-route +
Overpass). Tests pass in mock resolvers; the diagnostic script wires
them to the local PostGIS DB and the self-hosted Overpass on :12346.

Zone inference uses deterministic length bands (see
`StopCoverageThresholds` for the rationale) so the zone field is
reproducible without external metadata. Callers may override the
inferred zone — this is what lets us unit-test zone classification.

Re-uses :func:`haversine_m` and the local metre frame helpers from
:mod:`hades.enforcers.geometry_enforcer` so distance math is consistent
across the two enforcers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from hades.enforcers.geometry_enforcer import (
    GeometryReport,
    _M_PER_DEG_LAT,
    _m_per_deg_lon,
    haversine_m,
)


# ---------------------------------------------------------------------------
# Zone thresholds.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ZoneThresholds:
    """Per-zone consecutive-stop gap tolerance.

    * ``good_gap_m`` — anything at or below is acceptable for OTP in the zone.
    * ``acceptable_gap_m`` — above this the route is classified at best
      ``acceptable`` (downgraded one rung).
    * ``degraded_gap_m`` — above this the route falls to ``degraded`` /
      ``unroutable`` unless a resolution tier fires.
    * ``dead_head_max_m`` — max acceptable distance from the first/last
      stop to the corresponding route endpoint (dead-head).
    """

    good_gap_m: float
    acceptable_gap_m: float
    degraded_gap_m: float
    dead_head_max_m: float


# Defaults tuned to OTP's ~500 m urban radius and typical Ecuadorian
# transit spacing. If these shift they should shift per-zone together.
ZONE_DEFAULTS: dict[str, ZoneThresholds] = {
    # Dense core: stops every 250-400 m, riders expect walk ≤ 200 m.
    "urban_dense": ZoneThresholds(
        good_gap_m=500.0,
        acceptable_gap_m=800.0,
        degraded_gap_m=1500.0,
        dead_head_max_m=250.0,
    ),
    # Outer Quito/Guayaquil, Valle de los Chillos, Cumbayá.
    "urban_peripheral": ZoneThresholds(
        good_gap_m=800.0,
        acceptable_gap_m=1500.0,
        degraded_gap_m=2500.0,
        dead_head_max_m=500.0,
    ),
    # Rural parishes, canton-canton connectors.
    "rural": ZoneThresholds(
        good_gap_m=2000.0,
        acceptable_gap_m=4000.0,
        degraded_gap_m=8000.0,
        dead_head_max_m=1500.0,
    ),
    # Long-haul between cities. Gaps of several km are normal (highway
    # stretch with no populated nodes).
    "interprovincial": ZoneThresholds(
        good_gap_m=6000.0,
        acceptable_gap_m=15000.0,
        degraded_gap_m=30000.0,
        dead_head_max_m=3000.0,
    ),
}


@dataclass(frozen=True, slots=True)
class StopCoverageThresholds:
    # --- Zone inference from polyline length ---
    # Length bands are chosen so that the 1,112 Ecuadorian routes fall
    # cleanly into four buckets. Cross-checked against the mean/median
    # of ``ST_Length(route_prod.routes.geom::geography)`` on 2026-04-21:
    # median ≈ 17 km, p90 ≈ 80 km, max = 586 km.
    urban_dense_max_length_m: float = 6000.0
    urban_peripheral_max_length_m: float = 15000.0
    rural_max_length_m: float = 50000.0
    # Anything above ``rural_max_length_m`` is interprovincial.

    # --- Tier 1: cross-route borrow ---
    # A stop is borrowable if it sits within ``borrow_buffer_m`` of the
    # gap midpoint AND within ``corridor_buffer_m`` of the route's own
    # polyline (so we do not pull in stops from a parallel arterial a
    # block over that the bus does not actually serve).
    borrow_buffer_m: float = 75.0
    corridor_buffer_m: float = 35.0

    # --- Tier 2: Overpass POI ---
    # We query a bounding circle of ``overpass_buffer_m`` around each
    # gap midpoint. The first POI that lands within ``corridor_buffer_m``
    # of the route polyline wins.
    overpass_buffer_m: float = 80.0
    overpass_max_candidates: int = 8

    # --- Zone overrides ---
    # Dense zones may want the tier-1 buffer tighter so we don't steal a
    # stop from a parallel avenue. Peripheral / rural zones get a looser
    # buffer because stops are farther apart anyway.
    zone_to_thresholds: dict[str, ZoneThresholds] = field(
        default_factory=lambda: dict(ZONE_DEFAULTS)
    )


DEFAULT_THRESHOLDS = StopCoverageThresholds()


# ---------------------------------------------------------------------------
# Output schema.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class GapResolution:
    tier: int                        # 1, 2, 4, 5 (4+ are "prepared" only)
    tier_label: str                  # human-readable label
    resolved: bool                   # True for 1 & 2; False for 4_prepared / 5
    candidate_coord: Optional[tuple[float, float]] = None  # (lat, lon)
    candidate_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": int(self.tier),
            "tier_label": self.tier_label,
            "resolved": bool(self.resolved),
            "candidate_coord": (
                [round(float(self.candidate_coord[0]), 6),
                 round(float(self.candidate_coord[1]), 6)]
                if self.candidate_coord else None
            ),
            "candidate_metadata": self.candidate_metadata,
        }


@dataclass(slots=True)
class Gap:
    idx: int                         # index in the gap list (0..n_gaps-1)
    prev_stop_idx: int               # -1 = start dead-head
    next_stop_idx: int               # len(stops) = end dead-head
    gap_m: float                     # polyline distance of the gap
    midpoint_cum_m: float            # where along the route the gap centers
    midpoint_coord: tuple[float, float]  # (lat, lon) of gap midpoint
    severity_band: str               # 'good' | 'acceptable' | 'degraded' | 'unroutable'
    resolution: Optional[GapResolution] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "idx": int(self.idx),
            "prev_stop_idx": int(self.prev_stop_idx),
            "next_stop_idx": int(self.next_stop_idx),
            "gap_m": round(float(self.gap_m), 2),
            "midpoint_cum_m": round(float(self.midpoint_cum_m), 2),
            "midpoint_coord": [
                round(float(self.midpoint_coord[0]), 6),
                round(float(self.midpoint_coord[1]), 6),
            ],
            "severity_band": self.severity_band,
            "resolution": self.resolution.to_dict() if self.resolution else None,
        }


@dataclass(slots=True)
class StopCoverageReport:
    route_code: str
    version: int
    zone: str
    classification: str              # good | acceptable | ship_pending_dr |
                                     # degraded_minor | degraded | unroutable
    n_stops: int
    route_length_m: float
    gaps: list[Gap] = field(default_factory=list)
    n_gaps_unresolved: int = 0
    tier_usage: dict[str, int] = field(default_factory=dict)
    dr_queries_prepared: list[dict[str, Any]] = field(default_factory=list)
    synthetic_prepared: list[dict[str, Any]] = field(default_factory=list)
    # Structural explanation for the classification: tier counts,
    # gaps_per_km, geometry_class, and the named rule that fired. Lets
    # the dashboard answer "why is this route in this class?" without
    # re-running the classifier.
    classification_reasoning: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_code": self.route_code,
            "version": int(self.version),
            "zone": self.zone,
            "classification": self.classification,
            "classification_reasoning": dict(self.classification_reasoning),
            "n_stops": int(self.n_stops),
            "route_length_m": round(float(self.route_length_m), 2),
            "gaps": [g.to_dict() for g in self.gaps],
            "summary": {
                "n_gaps_total": len(self.gaps),
                "n_gaps_unresolved": int(self.n_gaps_unresolved),
                "tier_usage": dict(self.tier_usage),
                "n_dr_queries_prepared": len(self.dr_queries_prepared),
                "n_synthetic_prepared": len(self.synthetic_prepared),
            },
            "dr_queries_prepared": self.dr_queries_prepared,
            "synthetic_prepared": self.synthetic_prepared,
        }


# ---------------------------------------------------------------------------
# Resolvers (injected — mockable in tests).
# ---------------------------------------------------------------------------

# Cross-route resolver: given a gap midpoint + route corridor, return a
# list of candidate stop dicts (``{"node_id", "lat", "lon", "name"}``).
CrossRouteResolverFn = Callable[..., list[dict[str, Any]]]

# Overpass resolver: given a lat/lon and buffer_m, return a list of POI
# candidate dicts (``{"osm_id", "lat", "lon", "name", "tags"}``).
OverpassResolverFn = Callable[[float, float, float], list[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Geometry helpers (polyline + projections).
# ---------------------------------------------------------------------------

def cumulative_m(coords: Sequence[tuple[float, float]]) -> list[float]:
    cum = [0.0]
    for i in range(1, len(coords)):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        cum.append(cum[-1] + haversine_m(lat_a, lon_a, lat_b, lon_b))
    return cum


def project_point_to_polyline(
    point_lat: float,
    point_lon: float,
    coords: Sequence[tuple[float, float]],
    cum: Sequence[float],
) -> tuple[float, float]:
    """Project a (lat, lon) point onto the polyline.

    Returns ``(cum_m, perpendicular_distance_m)`` — the arc-length at the
    projected foot and the metre-distance from the point to the polyline.
    Uses a local equirectangular frame centred at the polyline midpoint.
    Good to < 0.1 % error for sub-metropolitan routes.
    """
    n = len(coords)
    if n == 0:
        return (0.0, float("inf"))
    if n == 1:
        lon0, lat0 = coords[0]
        return (0.0, haversine_m(lat0, lon0, point_lat, point_lon))

    lat_ref = sum(p[1] for p in coords) / n
    m_per_deg_lon = _m_per_deg_lon(lat_ref)

    px = point_lon * m_per_deg_lon
    py = point_lat * _M_PER_DEG_LAT

    best_cum = 0.0
    best_dist = float("inf")
    for i in range(1, n):
        lon_a, lat_a = coords[i - 1]
        lon_b, lat_b = coords[i]
        ax = lon_a * m_per_deg_lon
        ay = lat_a * _M_PER_DEG_LAT
        bx = lon_b * m_per_deg_lon
        by = lat_b * _M_PER_DEG_LAT
        abx = bx - ax
        aby = by - ay
        seg_len_sq = abx * abx + aby * aby
        if seg_len_sq < 1e-9:
            t = 0.0
            fx, fy = ax, ay
        else:
            t = ((px - ax) * abx + (py - ay) * aby) / seg_len_sq
            t = max(0.0, min(1.0, t))
            fx = ax + t * abx
            fy = ay + t * aby
        dx = px - fx
        dy = py - fy
        d = math.hypot(dx, dy)
        if d < best_dist:
            best_dist = d
            seg_m = cum[i] - cum[i - 1]
            best_cum = cum[i - 1] + t * seg_m
    return (best_cum, best_dist)


def _interpolate_on_polyline(
    target_cum_m: float,
    coords: Sequence[tuple[float, float]],
    cum: Sequence[float],
) -> tuple[float, float]:
    """Return the (lat, lon) at a given cumulative polyline distance."""
    n = len(coords)
    if n == 0:
        return (0.0, 0.0)
    if target_cum_m <= 0.0:
        lon, lat = coords[0]
        return (lat, lon)
    if target_cum_m >= cum[-1]:
        lon, lat = coords[-1]
        return (lat, lon)
    for i in range(1, n):
        if cum[i] >= target_cum_m:
            seg_m = cum[i] - cum[i - 1]
            if seg_m < 1e-9:
                lon, lat = coords[i]
                return (lat, lon)
            t = (target_cum_m - cum[i - 1]) / seg_m
            lon_a, lat_a = coords[i - 1]
            lon_b, lat_b = coords[i]
            return (lat_a + t * (lat_b - lat_a), lon_a + t * (lon_b - lon_a))
    lon, lat = coords[-1]
    return (lat, lon)


# ---------------------------------------------------------------------------
# Zone inference.
# ---------------------------------------------------------------------------

def infer_zone(
    route_length_m: float,
    thresholds: StopCoverageThresholds = DEFAULT_THRESHOLDS,
) -> str:
    """Infer the coverage zone from polyline length alone.

    Deterministic and reproducible — callers with better metadata (a
    bounding-box-derived population density, for example) may override.
    """
    if route_length_m <= thresholds.urban_dense_max_length_m:
        return "urban_dense"
    if route_length_m <= thresholds.urban_peripheral_max_length_m:
        return "urban_peripheral"
    if route_length_m <= thresholds.rural_max_length_m:
        return "rural"
    return "interprovincial"


# ---------------------------------------------------------------------------
# Core enforcer.
# ---------------------------------------------------------------------------

class StopCoverageEnforcer:
    """v1 stop-coverage enforcer. See module docstring."""

    VERSION = 1

    def __init__(
        self,
        thresholds: StopCoverageThresholds = DEFAULT_THRESHOLDS,
        cross_route_resolver: Optional[CrossRouteResolverFn] = None,
        overpass_resolver: Optional[OverpassResolverFn] = None,
    ):
        self.thresholds = thresholds
        self.cross_route_resolver = cross_route_resolver
        self.overpass_resolver = overpass_resolver

    # -- Public --------------------------------------------------------

    def analyze(
        self,
        *,
        route_code: str,
        coords: Sequence[tuple[float, float]],   # (lon, lat) points
        stop_coords: Sequence[tuple[float, float]],  # (lat, lon) stop points
        stop_ids: Optional[Sequence[str]] = None,
        zone: Optional[str] = None,
        version: int = VERSION,
        geometry_report: Optional[GeometryReport] = None,
    ) -> StopCoverageReport:
        """Analyze stop coverage and classify the v2.

        ``geometry_report`` is optional — when supplied, the classifier
        applies the geometry-severity veto (severe shape → unroutable
        regardless of gap state). Pre-v2 callers that omit it get the
        original "no signal from geometry" behavior, which is safe.
        """
        t = self.thresholds
        n = len(coords)
        if n < 2:
            empty = StopCoverageReport(
                route_code=route_code,
                version=version,
                zone="urban_dense",
                classification="unroutable",
                n_stops=len(stop_coords),
                route_length_m=0.0,
            )
            empty.classification_reasoning = {
                "rule_fired": "polyline_too_short",
                "n_coords": int(n),
            }
            return empty

        cum = cumulative_m(coords)
        total_m = cum[-1]
        resolved_zone = zone or infer_zone(total_m, t)
        zone_t = t.zone_to_thresholds.get(resolved_zone, ZONE_DEFAULTS["urban_peripheral"])

        report = StopCoverageReport(
            route_code=route_code,
            version=version,
            zone=resolved_zone,
            classification="good",
            n_stops=len(stop_coords),
            route_length_m=total_m,
        )

        if len(stop_coords) == 0:
            # No stops → unroutable. Record a dead-head gap spanning the
            # entire route so Prompt 7 can see the shape.
            full = Gap(
                idx=0,
                prev_stop_idx=-1,
                next_stop_idx=0,
                gap_m=total_m,
                midpoint_cum_m=total_m / 2.0,
                midpoint_coord=_interpolate_on_polyline(total_m / 2.0, coords, cum),
                severity_band="unroutable",
            )
            self._try_resolve(full, coords, cum, zone_t, report,
                              exclude_stop_ids=set(stop_ids or []))
            report.gaps = [full]
            self._tally_tiers(report)
            report.classification, report.classification_reasoning = (
                self._classify(report, zone_t, geometry_report)
            )
            return report

        # Project each stop onto the polyline; sort by cumulative distance.
        projected: list[tuple[int, float, float]] = []
        for i, (lat, lon) in enumerate(stop_coords):
            cum_m, _perp_m = project_point_to_polyline(lat, lon, coords, cum)
            projected.append((i, cum_m, _perp_m))
        projected.sort(key=lambda row: row[1])

        excluded_ids: set[str] = set(stop_ids or [])

        # Start dead-head.
        first_i, first_cum, _ = projected[0]
        if first_cum > zone_t.dead_head_max_m:
            g = Gap(
                idx=len(report.gaps),
                prev_stop_idx=-1,
                next_stop_idx=first_i,
                gap_m=first_cum,
                midpoint_cum_m=first_cum / 2.0,
                midpoint_coord=_interpolate_on_polyline(first_cum / 2.0, coords, cum),
                severity_band=self._band(first_cum, zone_t),
            )
            if g.severity_band != "good":
                self._try_resolve(g, coords, cum, zone_t, report, exclude_stop_ids=excluded_ids)
                report.gaps.append(g)

        # Between-stop gaps.
        for k in range(1, len(projected)):
            prev_i, prev_cum, _ = projected[k - 1]
            next_i, next_cum, _ = projected[k]
            gap_m = next_cum - prev_cum
            band = self._band(gap_m, zone_t)
            if band == "good":
                continue
            mid_cum = 0.5 * (prev_cum + next_cum)
            g = Gap(
                idx=len(report.gaps),
                prev_stop_idx=prev_i,
                next_stop_idx=next_i,
                gap_m=gap_m,
                midpoint_cum_m=mid_cum,
                midpoint_coord=_interpolate_on_polyline(mid_cum, coords, cum),
                severity_band=band,
            )
            self._try_resolve(g, coords, cum, zone_t, report, exclude_stop_ids=excluded_ids)
            report.gaps.append(g)

        # End dead-head.
        last_i, last_cum, _ = projected[-1]
        end_gap = total_m - last_cum
        if end_gap > zone_t.dead_head_max_m:
            band = self._band(end_gap, zone_t)
            if band != "good":
                mid_cum = 0.5 * (last_cum + total_m)
                g = Gap(
                    idx=len(report.gaps),
                    prev_stop_idx=last_i,
                    next_stop_idx=len(stop_coords),
                    gap_m=end_gap,
                    midpoint_cum_m=mid_cum,
                    midpoint_coord=_interpolate_on_polyline(mid_cum, coords, cum),
                    severity_band=band,
                )
                self._try_resolve(g, coords, cum, zone_t, report, exclude_stop_ids=excluded_ids)
                report.gaps.append(g)

        self._tally_tiers(report)
        report.classification, report.classification_reasoning = (
            self._classify(report, zone_t, geometry_report)
        )
        return report

    # -- Helpers -------------------------------------------------------

    @staticmethod
    def _band(gap_m: float, zone_t: ZoneThresholds) -> str:
        if gap_m <= zone_t.good_gap_m:
            return "good"
        if gap_m <= zone_t.acceptable_gap_m:
            return "acceptable"
        if gap_m <= zone_t.degraded_gap_m:
            return "degraded"
        return "unroutable"

    def _try_resolve(
        self,
        gap: Gap,
        coords: Sequence[tuple[float, float]],
        cum: Sequence[float],
        zone_t: ZoneThresholds,
        report: StopCoverageReport,
        *,
        exclude_stop_ids: set[str],
    ) -> None:
        """Attempt Tier 1 → Tier 2 → Tier 4 prepared → Tier 5 prepared."""
        mid_lat, mid_lon = gap.midpoint_coord

        # Tier 1: cross-route borrow.
        if self.cross_route_resolver is not None:
            try:
                candidates = self.cross_route_resolver(
                    lat=mid_lat,
                    lon=mid_lon,
                    buffer_m=self.thresholds.borrow_buffer_m,
                    route_corridor_coords=coords,
                    corridor_buffer_m=self.thresholds.corridor_buffer_m,
                    exclude_stop_ids=exclude_stop_ids,
                ) or []
            except Exception as exc:  # noqa: BLE001
                candidates = []
                report.dr_queries_prepared.append({
                    "gap_idx": gap.idx,
                    "tier_attempt_error": f"cross_route:{exc!r}",
                })
            best = self._pick_best_candidate(candidates, mid_lat, mid_lon, coords, cum)
            if best is not None:
                cand_lat, cand_lon, meta = best
                gap.resolution = GapResolution(
                    tier=1,
                    tier_label="cross_route_borrow",
                    resolved=True,
                    candidate_coord=(cand_lat, cand_lon),
                    candidate_metadata=meta,
                )
                return

        # Tier 2: Overpass POI.
        if self.overpass_resolver is not None:
            try:
                pois = self.overpass_resolver(
                    mid_lat, mid_lon, self.thresholds.overpass_buffer_m
                ) or []
            except Exception as exc:  # noqa: BLE001
                pois = []
                report.dr_queries_prepared.append({
                    "gap_idx": gap.idx,
                    "tier_attempt_error": f"overpass:{exc!r}",
                })
            best = self._pick_best_candidate(
                pois[: self.thresholds.overpass_max_candidates],
                mid_lat, mid_lon, coords, cum,
            )
            if best is not None:
                cand_lat, cand_lon, meta = best
                gap.resolution = GapResolution(
                    tier=2,
                    tier_label="osm_poi",
                    resolved=True,
                    candidate_coord=(cand_lat, cand_lon),
                    candidate_metadata=meta,
                )
                return

        # Tier 4 prepared: structured DR query (NOT issued here).
        dr_query = {
            "route_code": report.route_code,
            "zone": report.zone,
            "gap_idx": gap.idx,
            "gap_m": round(gap.gap_m, 2),
            "midpoint_lat": round(mid_lat, 6),
            "midpoint_lon": round(mid_lon, 6),
            "prompt_stub": (
                f"Route {report.route_code} has a {gap.severity_band} "
                f"{gap.gap_m:.0f} m gap centred at ({mid_lat:.5f}, {mid_lon:.5f}). "
                "Identify the nearest known bus-serving stop or landmark; "
                "return name and lat/lon."
            ),
        }
        report.dr_queries_prepared.append(dr_query)
        gap.resolution = GapResolution(
            tier=4,
            tier_label="dr_prepared",
            resolved=False,
            candidate_coord=None,
            candidate_metadata={"dr_query_idx": len(report.dr_queries_prepared) - 1},
        )

        # Tier 5 prepared: synthetic fill at the midpoint (for Prompt 7).
        report.synthetic_prepared.append({
            "gap_idx": gap.idx,
            "lat": round(mid_lat, 6),
            "lon": round(mid_lon, 6),
            "synthetic_confidence": "low" if gap.severity_band == "unroutable" else "medium",
        })

    @staticmethod
    def _pick_best_candidate(
        candidates: list[dict[str, Any]],
        mid_lat: float,
        mid_lon: float,
        coords: Sequence[tuple[float, float]],
        cum: Sequence[float],
    ) -> Optional[tuple[float, float, dict[str, Any]]]:
        """Choose the candidate closest to the gap midpoint.

        The caller has already enforced the corridor-buffer check server-
        side (for Tier 1) / pre-filtered Overpass results (for Tier 2).
        Here we just rank by euclidean metre distance to the midpoint.
        """
        best = None
        best_d = float("inf")
        for c in candidates:
            lat = c.get("lat")
            lon = c.get("lon")
            if lat is None or lon is None:
                continue
            d = haversine_m(mid_lat, mid_lon, float(lat), float(lon))
            if d < best_d:
                best_d = d
                meta = {k: v for k, v in c.items() if k not in ("lat", "lon")}
                meta["distance_m"] = round(d, 2)
                best = (float(lat), float(lon), meta)
        return best

    # -- Classification -------------------------------------------------

    # Hard cap on tier-4 (DR-prepared) gaps a route can carry while still
    # qualifying as ship_pending_dr. A 30-km route with 15 pending gaps
    # is not "ship now, improve later" — it's "wait for DR, then re-look".
    SHIP_PENDING_DR_TIER4_CAP = 8

    def _classify(
        self,
        report: StopCoverageReport,
        zone_t: ZoneThresholds,
        geometry_report: Optional[GeometryReport] = None,
    ) -> tuple[str, dict[str, Any]]:
        """Return ``(class_name, reasoning_dict)`` for this v2.

        Decision tree (top wins):

        1. ``n_stops == 0`` → ``unroutable``
        2. ``geometry_report.classification == "severe"`` → ``unroutable``
        3. No unresolved gaps → ``good``
        4. Any unresolved gap with ``severity_band == "unroutable"`` →
           ``unroutable``
        5. Synthetic-heavy (≥70 % tier-5 share of unresolved) →
           ``degraded``
        6. Geometry clean/minor + only tier-4 pending + count ≤ 8 +
           density ≤ 0.5/km → ``ship_pending_dr``
        7. Any unresolved gap in the degraded band:
              - geometry clean/minor + density ≤ 0.5/km →
                ``degraded_minor``
              - else → ``degraded``
        8. Per-km bands on remaining unresolved (acceptable-band only):
              - ≤ 0.3/km → ``acceptable``
              - ≤ 0.8/km → ``degraded_minor``
              - ≤ 1.5/km → ``degraded``
              - else → ``unroutable``

        Reasoning dict is always populated and includes
        ``rule_fired`` so callers can answer "why this class?".
        """
        reasoning: dict[str, Any] = {
            "rule_fired": "",
            "n_stops": int(report.n_stops),
            "route_length_m": round(float(report.route_length_m), 2),
            "geometry_class": None,
            "n_gaps_total": len(report.gaps),
            "n_unresolved": 0,
            "tier4_pending": 0,
            "tier5_synth": 0,
            "truly_broken": 0,
            "unres_per_km": 0.0,
            "synthetic_heavy": False,
        }

        if report.n_stops == 0:
            reasoning["rule_fired"] = "n_stops_zero"
            return "unroutable", reasoning

        geom_class = "clean"
        if geometry_report is not None:
            geom_class = (getattr(geometry_report, "classification", "clean")
                          or "clean")
        reasoning["geometry_class"] = geom_class

        if geom_class == "severe":
            reasoning["rule_fired"] = "geometry_severe_veto"
            return "unroutable", reasoning
        geom_ok = geom_class in ("clean", "minor")

        unresolved = [
            g for g in report.gaps
            if g.resolution is None or not g.resolution.resolved
        ]
        if not unresolved:
            reasoning["rule_fired"] = "no_unresolved"
            return "good", reasoning

        n_unres = len(unresolved)
        tier4_pending = sum(
            1 for g in unresolved
            if g.resolution is not None and g.resolution.tier == 4
        )
        tier5_synth = sum(
            1 for g in unresolved
            if g.resolution is not None and g.resolution.tier == 5
        )
        truly_broken = n_unres - tier4_pending - tier5_synth

        # Per-km uses a 1 km floor so a 200 m feeder with one gap is not
        # ranked worse than a 5 km arterial with five.
        route_len_km = max(float(report.route_length_m) / 1000.0, 1.0)
        unres_per_km = n_unres / route_len_km
        synthetic_heavy = (tier5_synth / n_unres) >= 0.7

        reasoning.update({
            "n_unresolved": n_unres,
            "tier4_pending": tier4_pending,
            "tier5_synth": tier5_synth,
            "truly_broken": truly_broken,
            "unres_per_km": round(unres_per_km, 4),
            "synthetic_heavy": synthetic_heavy,
        })

        if any(g.severity_band == "unroutable" for g in unresolved):
            reasoning["rule_fired"] = "severity_band_unroutable"
            return "unroutable", reasoning

        if synthetic_heavy:
            reasoning["rule_fired"] = "synthetic_heavy"
            return "degraded", reasoning

        if (geom_ok
                and truly_broken == 0
                and tier4_pending > 0
                and tier4_pending <= self.SHIP_PENDING_DR_TIER4_CAP
                and unres_per_km <= 0.5):
            reasoning["rule_fired"] = "ship_pending_dr"
            return "ship_pending_dr", reasoning

        if any(g.severity_band == "degraded" for g in unresolved):
            if geom_ok and unres_per_km <= 0.5:
                reasoning["rule_fired"] = "degraded_band_minor_density"
                return "degraded_minor", reasoning
            reasoning["rule_fired"] = "degraded_band"
            return "degraded", reasoning

        if unres_per_km <= 0.3:
            reasoning["rule_fired"] = "acceptable_band_low_density"
            return "acceptable", reasoning
        if unres_per_km <= 0.8:
            reasoning["rule_fired"] = "moderate_density"
            return "degraded_minor", reasoning
        if unres_per_km <= 1.5:
            reasoning["rule_fired"] = "high_density"
            return "degraded", reasoning

        reasoning["rule_fired"] = "extreme_density"
        return "unroutable", reasoning

    @staticmethod
    def _tally_tiers(report: StopCoverageReport) -> None:
        counter: dict[str, int] = {
            "tier1_cross_route_borrow": 0,
            "tier2_osm_poi": 0,
            "tier4_prepared": 0,
            "tier5_prepared": 0,
            "unresolved": 0,
        }
        report.n_gaps_unresolved = 0
        for g in report.gaps:
            if g.resolution is None:
                counter["unresolved"] += 1
                report.n_gaps_unresolved += 1
                continue
            if g.resolution.tier == 1:
                counter["tier1_cross_route_borrow"] += 1
            elif g.resolution.tier == 2:
                counter["tier2_osm_poi"] += 1
            elif g.resolution.tier == 4:
                counter["tier4_prepared"] += 1
                report.n_gaps_unresolved += 1
            elif g.resolution.tier == 5:
                counter["tier5_prepared"] += 1
                report.n_gaps_unresolved += 1
            else:
                counter["unresolved"] += 1
                report.n_gaps_unresolved += 1
        counter["tier5_prepared"] = len(report.synthetic_prepared)
        report.tier_usage = counter


# ---------------------------------------------------------------------------
# Convenience wrapper for single-call usage.
# ---------------------------------------------------------------------------

def classify_persisted(
    sc_dict: dict[str, Any],
    geom_dict: Optional[dict[str, Any]] = None,
    *,
    thresholds: StopCoverageThresholds = DEFAULT_THRESHOLDS,
) -> tuple[str, dict[str, Any]]:
    """Re-run the v2 classifier against a persisted stop_coverage_report dict.

    Reconstructs minimal :class:`StopCoverageReport` / :class:`Gap` /
    :class:`GapResolution` instances from the JSONB shape and feeds them
    into :meth:`StopCoverageEnforcer._classify`. Used by the reclassifier
    service to re-evaluate rows in ``approval_queue`` without re-running
    the enforcer's resolvers.

    ``geom_dict`` follows :meth:`GeometryReport.to_dict` shape; the
    classification can be at the top level or nested under ``summary``.
    """
    gaps: list[Gap] = []
    for g in (sc_dict.get("gaps") or []):
        res_dict = g.get("resolution")
        resolution: Optional[GapResolution] = None
        if res_dict:
            try:
                tier = int(res_dict["tier"])
            except (KeyError, TypeError, ValueError):
                tier = 0
            resolution = GapResolution(
                tier=tier,
                tier_label=str(res_dict.get("tier_label") or ""),
                resolved=bool(res_dict.get("resolved")),
            )
        mc = g.get("midpoint_coord") or [0.0, 0.0]
        gaps.append(Gap(
            idx=int(g.get("idx", 0)),
            prev_stop_idx=int(g.get("prev_stop_idx", 0)),
            next_stop_idx=int(g.get("next_stop_idx", 0)),
            gap_m=float(g.get("gap_m") or 0.0),
            midpoint_cum_m=float(g.get("midpoint_cum_m") or 0.0),
            midpoint_coord=(float(mc[0]), float(mc[1])),
            severity_band=str(g.get("severity_band") or ""),
            resolution=resolution,
        ))
    report = StopCoverageReport(
        route_code=str(sc_dict.get("route_code") or ""),
        version=int(sc_dict.get("version") or 1),
        zone=str(sc_dict.get("zone") or "urban_dense"),
        classification=str(sc_dict.get("classification") or ""),
        n_stops=int(sc_dict.get("n_stops") or 0),
        route_length_m=float(sc_dict.get("route_length_m") or 0.0),
        gaps=gaps,
    )
    geom: Optional[GeometryReport] = None
    if geom_dict:
        geom_class = (
            geom_dict.get("classification")
            or (geom_dict.get("summary") or {}).get("classification")
            or "clean"
        )
        geom = GeometryReport(
            route_code=report.route_code,
            version=3,
            anomalies=[],
            classification=str(geom_class),
            max_severity=float(
                geom_dict.get("max_severity")
                or (geom_dict.get("summary") or {}).get("max_severity")
                or 0.0
            ),
        )
    enf = StopCoverageEnforcer(thresholds=thresholds)
    zone_t = thresholds.zone_to_thresholds.get(
        report.zone, ZONE_DEFAULTS["urban_peripheral"]
    )
    return enf._classify(report, zone_t, geom)


def analyze_stop_coverage(
    coords: Sequence[tuple[float, float]],
    stop_coords: Sequence[tuple[float, float]],
    *,
    route_code: str,
    stop_ids: Optional[Sequence[str]] = None,
    zone: Optional[str] = None,
    version: int = StopCoverageEnforcer.VERSION,
    thresholds: StopCoverageThresholds = DEFAULT_THRESHOLDS,
    cross_route_resolver: Optional[CrossRouteResolverFn] = None,
    overpass_resolver: Optional[OverpassResolverFn] = None,
    geometry_report: Optional[GeometryReport] = None,
) -> dict[str, Any]:
    enforcer = StopCoverageEnforcer(
        thresholds=thresholds,
        cross_route_resolver=cross_route_resolver,
        overpass_resolver=overpass_resolver,
    )
    return enforcer.analyze(
        route_code=route_code,
        coords=coords,
        stop_coords=stop_coords,
        stop_ids=stop_ids,
        zone=zone,
        version=version,
        geometry_report=geometry_report,
    ).to_dict()
