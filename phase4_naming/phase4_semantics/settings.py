"""
Phase 4 – Semantics + Naming
Central configuration file.

RULE:
- No table names hardcoded anywhere else.
- No magic strings inside pipeline modules.
- If schema/table/view names change, update this file first.

GLOBAL ASSUMPTION:
- One single database: datamind_ml
- Phases separated by schemas
  (node_* / geo_* / route_* / console / semantics)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List
from urllib.parse import quote_plus

# ============================================================
# PATHS
# ============================================================

# phase4_semantics/settings.py
PHASE4_DIR = Path(__file__).resolve().parents[0]          # .../phase4_semantics
PHASE4_REPO_ROOT = Path(__file__).resolve().parents[1]    # .../phase4_naming

MODELS_DIR = Path(os.getenv("PHASE4_MODELS_DIR", str(PHASE4_REPO_ROOT / "phase4_models")))
LGBM_DIR = Path(os.getenv("PHASE4_LGBM_DIR", str(MODELS_DIR / "lgbm")))

# ============================================================
# DATABASE (GLOBAL: datamind_ml)
# ============================================================

# Official standard across all phases:
#   DB_DSN="postgresql://user:pass@host:5432/datamind_ml"
# Legacy alias still allowed:
#   DATABASE_URL="postgresql://user:pass@host:5432/datamind_ml"

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


DB_DSN: str = (
    os.getenv("DB_DSN")
    or os.getenv("DATABASE_URL")
    or _supabase_dsn_from_env()
    or ""
)

# ============================================================
# SCHEMAS
# ============================================================

SCHEMA_ROUTE_PROD = os.getenv("SCHEMA_ROUTE_PROD", "route_prod")
SCHEMA_SEMANTICS  = os.getenv("SCHEMA_SEMANTICS", "semantics")

# ============================================================
# CORE TABLES (Phase 4 reads/writes)
# ============================================================

# Canonical route table (Phase 3 output + Phase 4 semantic enrichment)
T_ROUTE_PROD_ROUTES = os.getenv("T_ROUTE_PROD_ROUTES", f"{SCHEMA_ROUTE_PROD}.routes")

# Evidence records (normalized facts from PDF/OSM/manual)
T_EVIDENCE_RECORDS = os.getenv("T_EVIDENCE_RECORDS", f"{SCHEMA_SEMANTICS}.route_evidence_records")

# Labels for ranker training (LambdaRank relevance judgments)
T_MATCH_LABELS = os.getenv("T_MATCH_LABELS", f"{SCHEMA_SEMANTICS}.match_labels")

# Ranker predictions / match candidates (optional future table)
T_MATCH_PREDICTIONS = os.getenv("T_MATCH_PREDICTIONS", f"{SCHEMA_SEMANTICS}.match_predictions")

# ============================================================
# CORE VIEWS (used by API/UI)
# ============================================================

V_ROUTES_PENDING = os.getenv("V_ROUTES_PENDING", f"{SCHEMA_SEMANTICS}.v_routes_pending")

# ============================================================
# ROUTE SEMANTIC FIELDS (Phase 4 columns living inside route_prod.routes)
# ============================================================

ROUTE_SEMANTIC_COLUMNS = {
    "route_name",
    "route_aliases",
    "landmark_tags",
    "direction_semantics",
    "naming_confidence",
    "human_verified",
    "semantics_updated_at",
    "search_tsv",
}

# ============================================================
# SEARCH CONFIG (Postgres full-text)
# ============================================================

# Matches your current query usage:
# plainto_tsquery('simple'::regconfig, q)
SEARCH_REGCONFIG = os.getenv("SEARCH_REGCONFIG", "simple")

DEFAULT_SEARCH_LIMIT = int(os.getenv("DEFAULT_SEARCH_LIMIT", "50"))
MAX_SEARCH_LIMIT = int(os.getenv("MAX_SEARCH_LIMIT", "200"))

# ============================================================
# API CONFIG (FastAPI service)
# ============================================================

API_HOST = os.getenv("PHASE4_API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("PHASE4_API_PORT", "8004"))

# CORS: keep permissive for now; tighten later
CORS_ALLOW_ALL = os.getenv("PHASE4_CORS_ALLOW_ALL", "true").lower() == "true"
CORS_ORIGINS_RAW = os.getenv("PHASE4_CORS_ORIGINS", "*")

def cors_origins() -> List[str]:
    if CORS_ALLOW_ALL or CORS_ORIGINS_RAW.strip() == "*":
        return ["*"]
    return [x.strip() for x in CORS_ORIGINS_RAW.split(",") if x.strip()]

# ============================================================
# RANKER SETTINGS (LightGBM LambdaRank)
# ============================================================

RANKER_MODEL_NAME = os.getenv("PHASE4_RANKER_MODEL_NAME", "route_match_ranker_lgbm.txt")

# relevance labels: 0 (bad), 1 (maybe), 2 (correct)
LABEL_MAX = int(os.getenv("PHASE4_LABEL_MAX", "2"))

TOP_K_CANDIDATES = int(os.getenv("PHASE4_TOP_K_CANDIDATES", "50"))

# Feature weights can live here later if needed
# (currently your ranking pipeline is still MVP)

# ============================================================
# SAFETY CHECKS
# ============================================================

REQUIRED_ENV_VARS = []  # keep empty for dev (fallback exists)

def validate_settings() -> None:
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")
