"""
Phase 2 – Semantic Geocoder
Central configuration file.

RULE:
- No table names hardcoded anywhere else.
- No magic strings in pipeline code.
- If DB schema changes, this file is updated first.

GLOBAL ASSUMPTION:
- One single database: datamind_ml
- Phases separated by schemas (node_* / geo_* / route_* / console / semantics)
"""

from __future__ import annotations
import os
from pathlib import Path
from urllib.parse import quote_plus

# ============================================================
# ENV / PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]

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


DB_DSN: str = (
    os.getenv("DB_DSN")
    or os.getenv("DATABASE_URL")
    or _supabase_dsn_from_env()
    or ""
)

# No historic city context is selected unless an operation supplies one.
GEO_CONTEXT_KEY: str = os.getenv("GEO_CONTEXT_KEY", "default")

# ============================================================
# EXTRACT / PIPELINE METADATA
# ============================================================

EXTRACTOR_VERSION: str = os.getenv(
    "EXTRACTOR_VERSION",
    "phase2_semantic_geocoder_v1",
)


# ============================================================
# SEARCH
# ============================================================

SEARCH_TOP_K: int = int(os.getenv("SEARCH_TOP_K", "50"))

# ============================================================
# EMBEDDINGS
# ============================================================

EMBED_MODEL_NAME: str = os.getenv(
    "EMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

EMBED_DIM: int = int(os.getenv("EMBED_DIM", "384"))
EMBED_BATCH_SIZE: int = int(os.getenv("EMBED_BATCH_SIZE", "64"))

# ============================================================
# DATABASE SCHEMAS
# ============================================================

SCHEMA_NODE_PROD = os.getenv("SCHEMA_NODE_PROD", "node_prod")
SCHEMA_GEO_RAW   = os.getenv("SCHEMA_GEO_RAW",   "geo_raw")
SCHEMA_GEO_WORK  = os.getenv("SCHEMA_GEO_WORK",  "geo_work")
SCHEMA_GEO_PROD  = os.getenv("SCHEMA_GEO_PROD",  "geo_prod")

# ============================================================
# PHASE 1 INPUT TABLES
# ============================================================

T_NODE_PROD_NODES = os.getenv("T_NODE_PROD_NODES", f"{SCHEMA_NODE_PROD}.nodes")

NODE_PROD_COLUMNS = {
    "node_id",
    "node_type",
    "geom",
    "chosen_tags",
    "confidence",
}

# ============================================================
# GEO_RAW TABLES
# ============================================================

T_GEO_EXTRACT_RUNS   = os.getenv("T_GEO_EXTRACT_RUNS",   f"{SCHEMA_GEO_RAW}.extract_runs")
T_GEO_NAME_EVIDENCE  = os.getenv("T_GEO_NAME_EVIDENCE",  f"{SCHEMA_GEO_RAW}.name_evidence")

GEO_NAME_EVIDENCE_COLUMNS = {
    "extract_run_id",
    "node_id",
    "source",
    "raw_text",
    "lang",
    "weight_hint",
    "tags_snapshot",
    "inserted_at",
}

# ============================================================
# GEO_WORK TABLES
# ============================================================

T_PLACE_CANDIDATE_SETS = os.getenv("T_PLACE_CANDIDATE_SETS", f"{SCHEMA_GEO_WORK}.place_candidate_sets")
T_PLACE_CANDIDATES     = os.getenv("T_PLACE_CANDIDATES",     f"{SCHEMA_GEO_WORK}.place_candidates")
T_ALIAS_CANDIDATES     = os.getenv("T_ALIAS_CANDIDATES",     f"{SCHEMA_GEO_WORK}.alias_candidates")
T_NODE_PLACE_WORK      = os.getenv("T_NODE_PLACE_WORK",      f"{SCHEMA_GEO_WORK}.node_place_map_work")
T_PLACE_SET_METRICS    = os.getenv("T_PLACE_SET_METRICS",    f"{SCHEMA_GEO_WORK}.place_set_metrics")
T_SELECTION_LOG        = os.getenv("T_SELECTION_LOG",        f"{SCHEMA_GEO_WORK}.selection_log")
T_MODEL_REGISTRY       = os.getenv("T_MODEL_REGISTRY",       f"{SCHEMA_GEO_WORK}.model_registry")
T_PLACE_NAME_CANDIDATES = os.getenv("T_PLACE_NAME_CANDIDATES", f"{SCHEMA_GEO_WORK}.place_name_candidates")
T_PLACE_NAME_FEEDBACK   = os.getenv("T_PLACE_NAME_FEEDBACK",   f"{SCHEMA_GEO_WORK}.place_name_feedback")
T_NODE_GEO_CONTEXT      = os.getenv("T_NODE_GEO_CONTEXT",      f"{SCHEMA_GEO_WORK}.node_geo_context")
T_POI_STOP_FEEDBACK     = os.getenv("T_POI_STOP_FEEDBACK",     f"{SCHEMA_GEO_WORK}.poi_stop_feedback")

# ============================================================
# GEO_PROD TABLES (FINAL OUTPUT)
# ============================================================

T_GEO_PLACES            = os.getenv("T_GEO_PLACES",          f"{SCHEMA_GEO_PROD}.places")
T_GEO_PLACE_ALIASES     = os.getenv("T_GEO_PLACE_ALIASES",   f"{SCHEMA_GEO_PROD}.place_aliases")
T_NODE_PLACE_MAP        = os.getenv("T_NODE_PLACE_MAP",      f"{SCHEMA_GEO_PROD}.node_place_map")

T_PLACE_EMBEDDINGS       = os.getenv("T_PLACE_EMBEDDINGS",        f"{SCHEMA_GEO_PROD}.place_embeddings")
T_PLACE_ALIAS_EMBEDDINGS = os.getenv("T_PLACE_ALIAS_EMBEDDINGS",  f"{SCHEMA_GEO_PROD}.place_alias_embeddings")

# ============================================================
# SEMANTIC EVIDENCE RULES
# ============================================================

# Which tag keys we extract and how strong they are
EVIDENCE_SOURCES = {
    "name": {"weight": 3.0, "lang": None, "split": False},
    "name:es": {"weight": 3.0, "lang": "es", "split": False},
    "official_name": {"weight": 2.5, "lang": None, "split": False},
    "short_name": {"weight": 2.0, "lang": None, "split": False},
    "alt_name": {"weight": 1.5, "lang": None, "split": True},   # split by ;
    "old_name": {"weight": 1.0, "lang": None, "split": True},
    "loc_name": {"weight": 1.0, "lang": None, "split": False},
    "ref": {"weight": 1.2, "lang": None, "split": False},
    "operator": {"weight": 0.6, "lang": None, "split": False},
    "network": {"weight": 0.4, "lang": None, "split": False},
    "wikipedia": {"weight": 1.8, "lang": None, "split": False},
    "wikidata": {"weight": 1.0, "lang": None, "split": False},
}

# ============================================================
# PLACE / ALIAS TYPES
# ============================================================

PLACE_TYPES = {"STOP", "POI", "STATION", "TERMINAL", "OTHER"}

ALIAS_KINDS = {"official", "short", "alt", "abbr", "historic", "typo_common"}

# ============================================================
# DEFAULT BEHAVIOR (MVP)
# ============================================================

# MVP = 1 place per node
MVP_ONE_PLACE_PER_NODE = True

DEFAULT_PLACE_CONFIDENCE = float(os.getenv("DEFAULT_PLACE_CONFIDENCE", "1.0"))
DEFAULT_MAPPING_SOURCE   = os.getenv("DEFAULT_MAPPING_SOURCE", "auto")

# ============================================================
# SEARCH / RERANKING
# ============================================================

FUZZY_WEIGHT     = float(os.getenv("FUZZY_WEIGHT", "0.4"))
EMBEDDING_WEIGHT = float(os.getenv("EMBEDDING_WEIGHT", "0.6"))

MAX_RETURN_PLACES = int(os.getenv("MAX_RETURN_PLACES", "10"))

# ============================================================
# MODEL REGISTRY NAMES
# ============================================================

MODEL_EMBEDDINGS = os.getenv("MODEL_EMBEDDINGS", "embeddings_multilingual_e5")
MODEL_RANKER     = os.getenv("MODEL_RANKER", "place_set_ranker_lgbm")

# ============================================================
# SAFETY CHECKS
# ============================================================

REQUIRED_ENV_VARS = ["DB_DSN"]

def validate_settings() -> None:
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k) and not DB_DSN]
    # Like Phase 1: you *can* rely on fallback defaults in dev,
    # but for production you want explicit env vars.
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")
