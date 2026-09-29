from __future__ import annotations
import os
from urllib.parse import quote_plus

"""
Phase 1 – Nodes Extractor
Central configuration file.

RULE:
- No table names hardcoded anywhere else.
- No magic strings in pipeline code.
- If DB schema changes, this file is updated first.

GLOBAL ASSUMPTION:
- One single database: datamind_ml
- Phases are separated by schemas (node_raw / node_work / node_prod)
"""

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

# Local-only sessions must opt into an explicitly local database.  This keeps
# Phase 1 consistent with the console and prevents an accidental server or
# libpq default-socket connection when the local DSN is absent.
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
# Phase-1 (Nodes) Schemas
# ============================================================

SCHEMA_RAW  = os.getenv("SCHEMA_RAW", "node_raw")
SCHEMA_WORK = os.getenv("SCHEMA_WORK", "node_work")
SCHEMA_PROD = os.getenv("SCHEMA_PROD", "node_prod")

# ============================================================
# Raw OSM (Overpass)
# ============================================================

T_ACTIONS  = os.getenv("T_ACTIONS",  f"{SCHEMA_RAW}.overpass_actions")
T_QUERIES  = os.getenv("T_QUERIES",  f"{SCHEMA_RAW}.overpass_queries")
T_RUNS     = os.getenv("T_RUNS",     f"{SCHEMA_RAW}.overpass_runs")
T_ELEMENTS = os.getenv("T_ELEMENTS", f"{SCHEMA_RAW}.overpass_elements")

# ============================================================
# Phase-1 Work tables (Nodes)
# ============================================================

# --- Candidate sets (node_sets) ---
T_NODE_SETS        = os.getenv("T_NODE_SETS",        f"{SCHEMA_WORK}.node_candidate_sets")
T_NODE_SET_METRICS = os.getenv("T_NODE_SET_METRICS", f"{SCHEMA_WORK}.node_set_metrics")

# --- Candidates / features / clusters / resolved ---
T_NODE_CANDIDATES = os.getenv("T_NODE_CANDIDATES", f"{SCHEMA_WORK}.node_candidates")
T_NODE_FEATURES   = os.getenv("T_NODE_FEATURES",   f"{SCHEMA_WORK}.node_features")
T_NODE_CLUSTERS   = os.getenv("T_NODE_CLUSTERS",   f"{SCHEMA_WORK}.node_clusters")
T_NODES_RESOLVED  = os.getenv("T_NODES_RESOLVED",  f"{SCHEMA_WORK}.nodes_resolved")

# --- Learning support ---
T_SELECTION_LOG  = os.getenv("T_SELECTION_LOG",  f"{SCHEMA_WORK}.selection_log")
T_MODEL_REGISTRY = os.getenv("T_MODEL_REGISTRY", f"{SCHEMA_WORK}.model_registry")
T_BANDIT_STATE   = os.getenv("T_BANDIT_STATE",   f"{SCHEMA_WORK}.bandit_state")

# ============================================================
# Views
# ============================================================

# Your SQL defines: node_work.v_raw_to_node_candidate
V_RAW_TO_NODE_CANDIDATE = os.getenv(
    "V_RAW_TO_NODE_CANDIDATE",
    f"{SCHEMA_WORK}.v_raw_to_node_candidate",
)

# ============================================================
# Prod tables
# ============================================================

T_PROD_NODES = os.getenv("T_PROD_NODES", f"{SCHEMA_PROD}.nodes")

# ============================================================
# ✅ Backwards-compatible aliases (old imports keep working)
# ============================================================

T_CANDIDATES = T_NODE_CANDIDATES
T_FEATURES   = T_NODE_FEATURES
T_CLUSTERS   = T_NODE_CLUSTERS
T_RESOLVED   = T_NODES_RESOLVED

V_RAW_TO_CANDIDATE = V_RAW_TO_NODE_CANDIDATE
T_PROD_STOPS       = T_PROD_NODES

# ============================================================
# External services
# ============================================================

# ============================================================
# Quality gates
# ============================================================

PROMOTE_MIN_CONFIDENCE = float(os.getenv("PROMOTE_MIN_CONFIDENCE", "0.3"))

# ============================================================
# Default area group (configurable for multi-city)
# ============================================================

# A job must supply a regional group when it needs one.  "default" carries no
# geographic bias and preserves the caller's candidate-action order.
DEFAULT_AREA_GROUP = os.getenv("DEFAULT_AREA_GROUP", "default")

OVERPASS_URL = os.getenv(
    "OVERPASS_URL",
    "https://overpass.kumi.systems/api/interpreter",
)

DEFAULT_BBOX = {
    "south": -0.220,
    "west":  -78.515,
    "north": -0.210,
    "east":  -78.505,
}

# ============================================================
# Safety checks
# ============================================================

REQUIRED_ENV_VARS = ["DB_DSN"]

def validate_settings() -> None:
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k) and not DB_DSN]
    # We allow a default fallback DSN, but we STILL warn if env is missing
    # (because in production you want explicit config).
    if missing:
        # Optional: turn into warning instead of error if you prefer
        raise RuntimeError(f"Missing required env vars: {missing}")
