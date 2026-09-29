"""Quality Gate shared thresholds and configuration.

All magic numbers live here. Rules import from this module.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Ecuador geography
# ---------------------------------------------------------------------------
ECUADOR_BBOX = (-5.0, -81.5, 1.5, -75.0)  # (min_lat, min_lon, max_lat, max_lon)
NULL_ISLAND_THRESHOLD = 0.01               # lat/lon both < this → null island

# ---------------------------------------------------------------------------
# Stop rules
# ---------------------------------------------------------------------------
MIN_STOP_NAME_LENGTH = 1
DUPLICATE_DISTANCE_M = 15.0
DUPLICATE_NAME_SIMILARITY = 0.85           # ratio threshold for "same name"

# ---------------------------------------------------------------------------
# Route rules
# ---------------------------------------------------------------------------
MIN_STOPS_PER_ROUTE = 3

# ---------------------------------------------------------------------------
# Shape rules
# ---------------------------------------------------------------------------
SHAPE_GAP_MAX_KM = 5.0
SELF_INTERSECTION_TOLERANCE_M = 5.0

# Route geometry validation
ROUTE_GEOM_MIN_SINUOSITY = 1.05        # below this = suspiciously straight
ROUTE_GEOM_MIN_POINTS_PER_KM = 3.0     # below this = low-detail trace
ROUTE_GEOM_STRAIGHT_LINE_RATIO = 0.98  # direct/path > this = straight line
ROUTE_GEOM_MIN_LENGTH_KM = 1.0         # skip very short routes

# ---------------------------------------------------------------------------
# Timing rules
# ---------------------------------------------------------------------------
RUNTIME_MIN_MINUTES = 5.0
RUNTIME_MAX_MINUTES = 240.0               # 4 hours

# ---------------------------------------------------------------------------
# Fare rules
# ---------------------------------------------------------------------------
FARE_MIN_USD = 0.10
FARE_MAX_USD = 5.00

# ---------------------------------------------------------------------------
# Naming rules
# ---------------------------------------------------------------------------
SHORT_NAME_MAX_LENGTH = 12

# ---------------------------------------------------------------------------
# Benchmark defaults
# ---------------------------------------------------------------------------
BENCHMARK_SEED = 42

# ---------------------------------------------------------------------------
# Name pattern lists — re-exported from the single source of truth.
# See datamind_console/common/naming_patterns.py and
# workspace/skills/hades-naming-standard/SKILL.md.
# ---------------------------------------------------------------------------
from datamind_console.common.naming_patterns import (
    STOP_FORBIDDEN_PATTERNS as PLACEHOLDER_PATTERNS,
    ROUTE_FORBIDDEN_PATTERNS as ROUTE_NAME_GARBAGE_PATTERNS,
    OPERATOR_FORBIDDEN_PATTERNS as OPERATOR_NAME_GARBAGE_PATTERNS,
)

OPERATOR_CATALOG_PATH = "phase4_naming/phase4_semantics/naming/operator_catalog.json"

# ---------------------------------------------------------------------------
# Haversine helper (used by rules and fixers)
# ---------------------------------------------------------------------------
import math

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two (lat, lon) points."""
    R = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
