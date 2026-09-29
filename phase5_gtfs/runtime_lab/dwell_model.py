"""
Bus dwell time model.

Valhalla computes road travel time.  This model adds stop dwell on top.
No manual catalogs — all defaults are hardcoded.  Deep Research overrides them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Hardcoded defaults (sensible starting values, NOT a catalog)
DEFAULT_DWELL_SECS: Dict[str, int] = {
    "urban_core": 30,
    "suburban": 25,
    "periurban": 20,
    "rural": 15,
    "terminal": 120,
    "transfer": 45,
}

# Coefficient of variation per area type
DEFAULT_DWELL_CV: Dict[str, float] = {
    "urban_core": 0.40,
    "suburban": 0.30,
    "periurban": 0.25,
    "rural": 0.20,
    "terminal": 0.50,
    "transfer": 0.35,
}


class DwellModel:
    """Estimate per-stop dwell time (mean + std) using defaults + research overrides."""

    def __init__(self, conn: Optional[Any] = None):
        self.conn = conn
        self._demand_overrides: Dict[str, Dict[str, Any]] = {}
        self._terminus_overrides: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_research_overrides(self, research_data: dict) -> None:
        """Load demand-driven dwell and terminus dwell from 06b research."""
        for dd in research_data.get("demand_driven_dwell", []):
            key = f"{round(dd.get('approx_lat', 0), 4)}_{round(dd.get('approx_lon', 0), 4)}"
            self._demand_overrides[key] = dd

        for route in research_data.get("routes", []):
            layover = route.get("layover")
            if layover:
                code = route.get("route_code") or route.get("route_ref", "")
                if code:
                    self._terminus_overrides[code] = layover

    # ------------------------------------------------------------------
    # Estimation
    # ------------------------------------------------------------------

    def estimate_dwell(
        self,
        stop_node_id: str,
        stop_lat: float,
        stop_lon: float,
        area_type: str = "suburban",
        is_terminus: bool = False,
        route_code: str = "",
        time_period: str = "off_peak",
        day_type: str = "weekday",
    ) -> Dict[str, Any]:
        """
        Estimate dwell time for a single stop.

        Returns
        -------
        dict with ``mean_secs``, ``std_secs``, ``source``.
        """
        # 1. Terminus override from research
        if is_terminus and route_code and route_code in self._terminus_overrides:
            lo = self._terminus_overrides[route_code]
            mean = lo.get("layover_at_destination_min", 5) * 60
            std = mean * 0.50
            return {"mean_secs": mean, "std_secs": std, "source": "research_terminus"}

        # 2. Default terminus
        if is_terminus:
            mean = DEFAULT_DWELL_SECS["terminal"]
            std = mean * DEFAULT_DWELL_CV["terminal"]
            return {"mean_secs": mean, "std_secs": std, "source": "default_terminus"}

        # 3. Demand override by proximity
        key = f"{round(stop_lat, 4)}_{round(stop_lon, 4)}"
        if key in self._demand_overrides:
            dd = self._demand_overrides[key]

            if dd.get("market_days") and day_type in dd["market_days"]:
                mean = dd.get("market_day_dwell_secs", 120)
            elif dd.get("class_change_times") and time_period in ("peak_am", "peak_pm"):
                mean = dd.get("class_change_dwell_secs", 90)
            else:
                mean = dd.get("normal_dwell_secs", DEFAULT_DWELL_SECS.get(area_type, 25))

            std = mean * 0.35
            return {"mean_secs": mean, "std_secs": std, "source": "research_demand"}

        # 4. Default by area type
        mean = DEFAULT_DWELL_SECS.get(area_type, 25)
        std = mean * DEFAULT_DWELL_CV.get(area_type, 0.30)
        return {"mean_secs": mean, "std_secs": std, "source": "default"}
