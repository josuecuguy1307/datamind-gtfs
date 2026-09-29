from __future__ import annotations

from pathlib import Path


CONSTRUCTOR_V2_VERSION = "constructor_v2.v1"
DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL = "http://127.0.0.1:8003"
LEGACY_CONSTRUCTOR_V2_VALHALLA_URL = "http://127.0.0.1:8002"

# Duplicate handling keeps route structure conservative: exact ID duplicates collapse first,
# then same-name near-collisions within one boarding area are clustered.
DUPLICATE_DISTANCE_M = 60.0
VERY_CLOSE_DISTANCE_M = 18.0
WEAK_STOP_SCORE_THRESHOLD = 0.35
WEAK_STOP_DEFAULT_PENALTY = 4_000

# The legacy Phase 3 Valhalla in this repo exposes only /route. Constructor V2 can point
# at a dedicated instance with a wider API surface via CONSTRUCTOR_V2_VALHALLA_URL.
# These flags keep fallback behavior explicit in diagnostics rather than implicit.
VALHALLA_HTTP_TIMEOUT_S = 45
VALHALLA_PAIR_TIMEOUT_S = 20
VALHALLA_STATUS_TIMEOUT_S = 10
VALHALLA_SUPPORTED_PATHS = ("/status", "/locate", "/optimized_route", "/sources_to_targets", "/route")

# Validation thresholds are intentionally explicit so benchmark reports can explain why a route
# was accepted or escalated.
MAX_ACCEPTABLE_DETOUR_RATIO = 3.2
MAX_STRONG_DETOUR_RATIO = 2.2
MAX_REPEAT_SEGMENT_RATIO = 0.12
MAX_MONOTONIC_REGRESSION = 0.03
MAX_CORRIDOR_SWITCHES = 3

# OR-Tools uses integer arc costs. Millisecond granularity is unnecessary here.
OBJECTIVE_SCALE = 1
SOLVER_TIME_LIMIT_MS = 12_000

BENCHMARK_ROUTE_BUCKETS = {
    "easy": [
        "San Fernando - El Triangulo",
        "La Libertad - Sangolqui",
    ],
    "medium": [
        "Quito - La Armenia - Conocoto",
    ],
    "hard": [
        "Marin - Autopista - Parque Turismo Sangolqui",
        "Marin - Puengasi - Conocoto - Santa Isabel",
    ],
}

BENCHMARK_ARTIFACT_DIR = Path("constructor_artifacts/constructor_v2_v1")
BENCHMARK_COMPARISON_ARTIFACT_DIR = Path("constructor_artifacts/constructor_v2_compare")
DEFAULT_CACHE_DIR = BENCHMARK_ARTIFACT_DIR / "cache"
