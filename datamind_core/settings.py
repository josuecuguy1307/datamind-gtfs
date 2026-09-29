"""
DataMind Core – Global Settings
---------------------------------

RULE:
- DB_DSN lives here as the official source of truth.
- Schemas are standardized here.
- External services default config lives here.
- Each phase imports from here and extends, not reinvent.

GLOBAL ASSUMPTION:
- One single database: datamind_ml
- Separation is done ONLY via schemas (node_*, geo_*, route_*, console, semantics)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List
from urllib.parse import quote_plus


# ============================================================
# PATHS (repo structure)
# ============================================================

REPO_ROOT = Path(__file__).resolve().parents[1]  # ML DATAMIND/
CORE_DIR = Path(__file__).resolve().parents[0]   # datamind_core/


# ============================================================
# ENV (app mode / debug)
# ============================================================

APP_ENV = os.getenv("APP_ENV", "dev")         # dev | staging | prod
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")    # DEBUG | INFO | WARNING | ERROR


# ============================================================
# DATABASE (ONE DB ONLY)
# ============================================================

# Official env var for EVERYTHING:
#   DB_DSN="postgresql://user:pass@127.0.0.1:5432/datamind_ml"
#
# Legacy alias allowed temporarily:
#   DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/datamind_ml"

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


def _resolve_core_db_dsn() -> str:
    mode = (os.getenv("DATA_MODE", "server") or "server").strip().lower()
    local = os.getenv("LOCAL_DB_DSN") or os.getenv("DB_DSN_LOCAL")
    server = (
        os.getenv("SERVER_DB_DSN")
        or os.getenv("DB_DSN")
        or os.getenv("DATABASE_URL")
        or _supabase_dsn_from_env()
    )
    if mode == "local":
        return local or server or ""
    return server or local or ""


DB_DSN: str = _resolve_core_db_dsn()


# ============================================================
# STANDARD SCHEMAS (shared across all phases)
# ============================================================

# Console / admin DB
SCHEMA_CONSOLE = os.getenv("SCHEMA_CONSOLE", "console")

# Phase 1
SCHEMA_NODE_RAW  = os.getenv("SCHEMA_NODE_RAW",  "node_raw")
SCHEMA_NODE_WORK = os.getenv("SCHEMA_NODE_WORK", "node_work")
SCHEMA_NODE_PROD = os.getenv("SCHEMA_NODE_PROD", "node_prod")

# Phase 2
SCHEMA_GEO_RAW   = os.getenv("SCHEMA_GEO_RAW",   "geo_raw")
SCHEMA_GEO_WORK  = os.getenv("SCHEMA_GEO_WORK",  "geo_work")
SCHEMA_GEO_PROD  = os.getenv("SCHEMA_GEO_PROD",  "geo_prod")

# Phase 3
SCHEMA_ROUTE_RAW  = os.getenv("SCHEMA_ROUTE_RAW",  "route_raw")
SCHEMA_ROUTE_WORK = os.getenv("SCHEMA_ROUTE_WORK", "route_work")
SCHEMA_ROUTE_PROD = os.getenv("SCHEMA_ROUTE_PROD", "route_prod")

# Phase 4 (semantics)
SCHEMA_SEMANTICS = os.getenv("SCHEMA_SEMANTICS", "semantics")


# ============================================================
# EXTERNAL SERVICES (shared defaults)
# ============================================================

OVERPASS_LOCAL_URL = "http://127.0.0.1:12346/api/interpreter"
OVERPASS_PUBLIC_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_URL = os.getenv("OVERPASS_URL", OVERPASS_LOCAL_URL)
VALHALLA_URL = os.getenv("VALHALLA_URL", "http://127.0.0.1:8003")

OPENSEARCH_URL = os.getenv("OPENSEARCH_URL", "http://127.0.0.1:9200")
OPENSEARCH_USER = os.getenv("OPENSEARCH_USER")
OPENSEARCH_PASS = os.getenv("OPENSEARCH_PASS")


# ============================================================
# GLOBAL CONTEXT / REGION (optional but useful)
# ============================================================

# Use this to namespace data sets (sample_v1, guayaquil_v1, etc.)
GLOBAL_CONTEXT_KEY = os.getenv("GLOBAL_CONTEXT_KEY", "sample_v1")


# ============================================================
# HELP
# ============================================================

def tbl(schema: str, name: str) -> str:
    """Build schema-qualified table/view name safely."""
    return f"{schema}.{name}"


# ============================================================
# SAFETY CHECKS
# ============================================================

REQUIRED_ENV_VARS: List[str] = []  # keep empty for dev (fallback exists)

def validate_core_settings() -> None:
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"[datamind_core] Missing required env vars: {missing}")
