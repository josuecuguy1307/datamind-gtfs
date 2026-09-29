"""
Stochastic variance model.

Takes Valhalla's point estimate per edge and Deep Research's runtime ranges
to produce per-leg distributions (p05, p50, p95).

Combined models:
  Model 1: Log-normal fit from min/typical/max (route-level)
  Model 2: Factor-based variance (edge-level, from road characteristics)
  Model 3: Proportional scaling with variance preservation
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

# Road-class coefficient of variation (code defaults)
ROAD_CLASS_CV: Dict[str, float] = {
    "motorway": 0.10,
    "trunk": 0.12,
    "primary": 0.18,
    "secondary": 0.22,
    "tertiary": 0.28,
    "residential": 0.30,
    "service": 0.35,
}

# Peak periods have wider variance
PERIOD_VARIANCE_MULT: Dict[str, float] = {
    "peak_am": 1.5,
    "off_peak": 1.0,
    "peak_pm": 1.4,
    "night": 0.7,
}


class VarianceModel:
    """Fit route-level distributions and propagate variance to individual legs."""

    def __init__(self) -> None:
        self._reliability_scores: Dict[str, Dict[str, Any]] = {}
        self._seasonal_factors: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_research_data(self, research_data: dict) -> None:
        """Load reliability and seasonal data from 06b."""
        for route in research_data.get("routes", []):
            rt = route.get("runtime") or {}
            rel = rt.get("reliability") or {}
            if rel:
                code = route.get("route_code") or route.get("route_ref", "")
                if code:
                    self._reliability_scores[code] = {
                        "score": rel.get("score", "medium"),
                        "variance_min": rel.get("typical_variance_min", 12),
                    }

        sf = research_data.get("seasonal_factors")
        if sf:
            self._seasonal_factors = sf

    # ------------------------------------------------------------------
    # Route-level distribution
    # ------------------------------------------------------------------

    def fit_route_distribution(self, runtime_range: dict) -> Dict[str, float]:
        """
        Fit a log-normal distribution from min/typical/max.

        Parameters
        ----------
        runtime_range : dict
            ``{"min_min": 55, "typical_min": 65, "max_min": 80}``

        Returns
        -------
        dict with ``mu``, ``sigma``, ``p05_secs``, ``p50_secs``, ``p95_secs``, ``cv``.
        """
        typical = runtime_range.get("typical_min", 45) * 60
        mn = runtime_range.get("min_min", typical / 60 * 0.8) * 60
        mx = runtime_range.get("max_min", typical / 60 * 1.3) * 60

        mu = math.log(max(1, typical))

        if mx > mn > 0:
            sigma = (math.log(mx) - math.log(mn)) / (2 * 1.645)
        else:
            sigma = 0.15

        sigma = max(0.02, sigma)  # floor

        p05 = math.exp(mu - 1.645 * sigma)
        p50 = math.exp(mu)
        p95 = math.exp(mu + 1.645 * sigma)

        return {
            "mu": mu,
            "sigma": sigma,
            "p05_secs": p05,
            "p50_secs": p50,
            "p95_secs": p95,
            "cv": sigma,
        }

    # ------------------------------------------------------------------
    # Leg-level variance propagation
    # ------------------------------------------------------------------

    def distribute_variance_to_legs(
        self,
        route_distribution: Dict[str, float],
        leg_times_secs: List[float],
        leg_road_classes: List[str],
        time_period: str = "off_peak",
        route_code: str = "",
    ) -> List[Dict[str, float]]:
        """
        Distribute route-level variance to individual legs.

        Legs through congested/complex roads get wider sigma;
        legs on highways get narrower sigma.

        Returns
        -------
        list of dicts with ``mean_secs``, ``std_secs``, ``p05_secs``, ``p50_secs``,
        ``p95_secs``, ``cv``.
        """
        total_time = sum(leg_times_secs)
        route_sigma = route_distribution["sigma"]

        period_mult = PERIOD_VARIANCE_MULT.get(time_period, 1.0)

        reliability_mult = 1.0
        if route_code and route_code in self._reliability_scores:
            score = self._reliability_scores[route_code]["score"]
            reliability_mult = {"high": 0.7, "medium": 1.0, "low": 1.5}.get(score, 1.0)

        legs: List[Dict[str, float]] = []
        for leg_time, road_class in zip(leg_times_secs, leg_road_classes):
            if total_time <= 0 or leg_time <= 0:
                legs.append({
                    "mean_secs": leg_time,
                    "std_secs": 0.0,
                    "p05_secs": leg_time,
                    "p50_secs": leg_time,
                    "p95_secs": leg_time,
                    "cv": 0.0,
                })
                continue

            leg_fraction = leg_time / total_time
            road_cv = ROAD_CLASS_CV.get(road_class, 0.22)

            # Leg sigma: route sigma scaled by sqrt(fraction), road variability, period, reliability
            leg_sigma = (
                route_sigma
                * math.sqrt(leg_fraction)
                * (road_cv / 0.22)
                * period_mult
                * reliability_mult
            )
            leg_sigma = max(0.01, leg_sigma)

            leg_mu = math.log(max(1, leg_time))
            leg_p05 = math.exp(leg_mu - 1.645 * leg_sigma)
            leg_p50 = math.exp(leg_mu)
            leg_p95 = math.exp(leg_mu + 1.645 * leg_sigma)
            leg_std = leg_time * leg_sigma

            legs.append({
                "mean_secs": round(leg_time, 1),
                "std_secs": round(leg_std, 1),
                "p05_secs": round(leg_p05, 1),
                "p50_secs": round(leg_p50, 1),
                "p95_secs": round(leg_p95, 1),
                "cv": round(leg_sigma, 3),
            })

        return legs

    # ------------------------------------------------------------------
    # Seasonal adjustments
    # ------------------------------------------------------------------

    def apply_seasonal_adjustment(
        self,
        time_secs: float,
        area: str,
        month: int,
        has_steep_grade: bool = False,
    ) -> float:
        """Apply seasonal multiplier if applicable."""
        rainy = self._seasonal_factors.get("rainy_season") or {}
        if rainy and month in rainy.get("months", []):
            key = "steep_segment_multiplier" if has_steep_grade else "runtime_multiplier"
            return time_secs * rainy.get(key, 1.0)

        vacation = self._seasonal_factors.get("vacation") or {}
        if vacation and month in vacation.get("months", []):
            return time_secs * vacation.get("runtime_multiplier", 1.0)

        return time_secs
