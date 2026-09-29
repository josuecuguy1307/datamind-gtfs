"""
Phase 3 – Route Constructor (Geometry)
Central configuration file.

RULE:
- No magic strings in pipeline code.
- DB connection is shared globally (datamind_ml).
- If thresholds change, update this file first.

GLOBAL ASSUMPTION:
- One single database: datamind_ml
- Phases separated by schemas (route_raw / route_work / route_prod)
"""

from __future__ import annotations
import os
from urllib.parse import quote_plus

# ============================================================
# Database (GLOBAL: datamind_ml)
# ============================================================

def _supabase_dsn_from_env() -> str | None:
    host = os.getenv("SUPABASE_DB_HOST")
    if not host:
        return None
    port = os.getenv("SUPABASE_DB_PORT", "5432")
    name = os.getenv("SUPABASE_DB_NAME", "postgres")
    user = os.getenv("SUPABASE_DB_USER", "postgres")
    password = os.getenv("SUPABASE_DB_PASSWORD", "")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
        "?sslmode=require"
    )


_LOCAL_ONLY_MODE = os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {
    "1", "true", "t", "yes", "y", "on"
}

DB_DSN = (
    (
        os.getenv("LOCAL_DB_DSN")
        or os.getenv("DATAMIND_LOCAL_DB_DSN")
        or os.getenv("DB_DSN_LOCAL")
        or ""
    )
    if _LOCAL_ONLY_MODE
    else (
        os.getenv("DB_DSN")
        or os.getenv("DATABASE_URL")
        or _supabase_dsn_from_env()
        or ""
    )
)

# ============================================================
# External services
# ============================================================

OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass.private.coffee/api/interpreter")
VALHALLA_URL = os.getenv("VALHALLA_URL", "http://127.0.0.1:8003")
HTTP_TIMEOUT_S = int(os.getenv("HTTP_TIMEOUT_S", "180"))

# ============================================================
# Matching thresholds (Stop ↔ Geometry)
# ============================================================

MAX_STOP_MATCH_RADIUS_M_STRICT  = float(os.getenv("MAX_STOP_MATCH_RADIUS_M_STRICT", "60"))
MAX_STOP_MATCH_RADIUS_M_RELAXED = float(os.getenv("MAX_STOP_MATCH_RADIUS_M_RELAXED", "120"))

MIN_MATCHED_STOPS_STRICT  = int(os.getenv("MIN_MATCHED_STOPS_STRICT", "6"))
MIN_MATCHED_STOPS_RELAXED = int(os.getenv("MIN_MATCHED_STOPS_RELAXED", "4"))

# ============================================================
# Valhalla config
# ============================================================

VALHALLA_COSTING      = os.getenv("VALHALLA_COSTING", "bus")
VALHALLA_SHAPE_FORMAT = os.getenv("VALHALLA_SHAPE_FORMAT", "polyline6")
STEP20_VALHALLA_MAX_CANDIDATES = int(os.getenv("STEP20_VALHALLA_MAX_CANDIDATES", "5"))
STEP20_VALHALLA_MIN_STRUCTURAL_SCORE = float(
    os.getenv("STEP20_VALHALLA_MIN_STRUCTURAL_SCORE", "60.0")
)
STEP20_VALHALLA_BLEND_WEIGHT = float(os.getenv("STEP20_VALHALLA_BLEND_WEIGHT", "0.22"))
STEP20_VALHALLA_TIMEOUT_S = int(os.getenv("STEP20_VALHALLA_TIMEOUT_S", "45"))

# ============================================================
# Candidate scoring weights
# ============================================================

W_STOP_DIST = float(os.getenv("W_STOP_DIST", "1.0"))
W_LENGTH    = float(os.getenv("W_LENGTH", "0.001"))

# ============================================================
# Pipeline version tags
# ============================================================

GENERATOR_VERSION = os.getenv("GENERATOR_VERSION", "v1")

# ============================================================
# Safety checks
# ============================================================

REQUIRED_ENV_VARS = ["DB_DSN"]

def validate_settings() -> None:
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k) and not DB_DSN]
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")
