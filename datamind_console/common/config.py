from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote_plus


# ============================================================
# ENV HELPERS
# ============================================================

def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(key)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _env_bool(key: str, default: bool = False) -> bool:
    v = _env(key)
    if v is None:
        return default
    return v.lower() in {"1", "true", "t", "yes", "y", "on"}


def _env_int(key: str, default: int) -> int:
    v = _env(key)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _env_float(key: str, default: float) -> float:
    v = _env(key)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


def _env_tuple(key: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    v = _env(key)
    if not v:
        return default
    parts = [p.strip() for p in v.split(",") if p.strip()]
    return tuple(parts) if parts else default


# ============================================================
# REPO ROOT DETECTION
# ============================================================

def repo_root() -> Path:
    """
    Detect ML_DATAMIND repo root.
    Works if you run from repo root OR from datamind_console/.
    Override with DATAMIND_REPO_ROOT if needed.
    """
    override = _env("DATAMIND_REPO_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()

    for parent in here.parents:
        # typical root contains these folders
        if (parent / "datamind_console").exists() and (
            (parent / "phase1_nodes").exists()
            or (parent / "phase2_semantics").exists()
            or (parent / "phase3_routes").exists()
            or (parent / "phase4_naming").exists()
        ):
            return parent

    # fallback: current working directory
    return Path.cwd().resolve()


def _tbl(schema: str, name: str) -> str:
    return f"{schema}.{name}"


def _local_only_mode_enabled() -> bool:
    return _env_bool("DATAMIND_LOCAL_ONLY_MODE", False)


def _resolve_db_dsn() -> Optional[str]:
    """
    DSN resolution priority (new standard):
      1) DATAMIND_CONSOLE_DB_DSN  (console override)
      2) DB_DSN                (global repo DSN you want everywhere)
      3) DATABASE_URL          (common convention)
      4) DATAMIND_DB_DSN         (legacy/older in-console var)

    ✅ Recommended: export DB_DSN="postgresql://user:pass@host:5432/dbname?sslmode=require"
    """
    if _local_only_mode_enabled():
        return _resolve_local_db_dsn()

    supabase_host = _env("SUPABASE_DB_HOST")
    supabase_dsn: Optional[str] = None
    if supabase_host:
        supabase_port = _env("SUPABASE_DB_PORT", "5432") or "5432"
        supabase_name = _env("SUPABASE_DB_NAME", "postgres") or "postgres"
        supabase_user = _env("SUPABASE_DB_USER", "postgres") or "postgres"
        supabase_password = _env("SUPABASE_DB_PASSWORD", "") or ""
        supabase_dsn = (
            f"postgresql://{quote_plus(supabase_user)}:{quote_plus(supabase_password)}"
            f"@{supabase_host}:{supabase_port}/{supabase_name}?sslmode=require"
        )

    return (
        _env("DATAMIND_CONSOLE_DB_DSN")
        or _env("DB_DSN")
        or _env("DATABASE_URL")
        or _env("DATAMIND_DB_DSN")
        or supabase_dsn
    )


def _resolve_server_db_dsn() -> Optional[str]:
    if _local_only_mode_enabled():
        return None

    supabase_host = _env("SUPABASE_DB_HOST")
    supabase_dsn: Optional[str] = None
    if supabase_host:
        supabase_port = _env("SUPABASE_DB_PORT", "5432") or "5432"
        supabase_name = _env("SUPABASE_DB_NAME", "postgres") or "postgres"
        supabase_user = _env("SUPABASE_DB_USER", "postgres") or "postgres"
        supabase_password = _env("SUPABASE_DB_PASSWORD", "") or ""
        supabase_dsn = (
            f"postgresql://{quote_plus(supabase_user)}:{quote_plus(supabase_password)}"
            f"@{supabase_host}:{supabase_port}/{supabase_name}?sslmode=require"
        )
    return (
        _env("SERVER_DB_DSN")
        or _env("DATAMIND_SERVER_DB_DSN")
        or _resolve_db_dsn()
        or supabase_dsn
    )


def _resolve_local_db_dsn() -> Optional[str]:
    return (
        _env("LOCAL_DB_DSN")
        or _env("DATAMIND_LOCAL_DB_DSN")
        or _env("DB_DSN_LOCAL")
    )


def _resolve_data_mode() -> str:
    if _local_only_mode_enabled():
        return "local"

    mode = (_env("DATA_MODE", "server") or "server").strip().lower()
    if mode not in {"local", "server"}:
        return "server"
    return mode


def resolve_active_db_dsn(*, mode: Optional[str] = None) -> Optional[str]:
    """
    Active runtime DSN resolver.
    - local mode -> LOCAL_DB_DSN (fallback to server DSN if missing)
    - server mode -> SERVER_DB_DSN/supabase path
    """
    m = (mode or _resolve_data_mode()).strip().lower()
    local = _resolve_local_db_dsn()
    server = _resolve_server_db_dsn()
    if _local_only_mode_enabled():
        return local
    if m == "local":
        return local or server
    return server or local


# ============================================================
# APP CONFIG
# ============================================================

@dataclass(frozen=True)
class AppConfig:
    # --------- meta ----------
    app_name: str = "ML DATAMIND GTFS"
    debug: bool = False
    app_env: str = "dev"
    log_level: str = "INFO"

    # --------- DB ----------
    # ✅ One DB for everything: datamind_ml
    # phases live in node_raw/node_work/node_prod/geo_* /route_* /semantics /console
    db_dsn: Optional[str] = None
    db_dsn_local: Optional[str] = None
    db_dsn_server: Optional[str] = None
    data_mode: str = "server"
    local_only_mode: bool = False
    db_connect_timeout_s: int = 10

    # --------- console schema ----------
    schema_console: str = "console"

    # --------- Console auth ----------
    session_ttl_hours: int = 24
    password_min_len: int = 6

    # --------- API URLs ----------
    # (Phase4 Semantics API used by Console UI)
    phase4_api_url: str = "http://127.0.0.1:8000"
    phase4_api_key: Optional[str] = None
    phase4_timeout_s: float = 20.0

    # Geo API (commercial endpoint contract)
    geo_api_base_url: str = ""
    geo_api_public_base_url: str = ""
    geo_api_timeout_s: float = 20.0

    # --------- Defaults ----------
    default_bbox: str = "-0.38,-78.60,-0.02,-78.35"  # Quito-ish default
    default_limit: int = 50

    # --------- UI / Web ----------
    cors_allow_origins: Tuple[str, ...] = ("*",)

    # --------- Repo ----------
    repo_root: Path = repo_root()

    @staticmethod
    def from_env() -> "AppConfig":
        return AppConfig(
            app_name=_env("DATAMIND_CONSOLE_APP_NAME", "ML DATAMIND GTFS") or "ML DATAMIND GTFS",
            debug=_env_bool("DATAMIND_DEBUG", False),
            app_env=_env("APP_ENV", "dev") or "dev",
            log_level=_env("LOG_LEVEL", "INFO") or "INFO",

            # DB (NEW DSN RULE)
            db_dsn=resolve_active_db_dsn(),
            db_dsn_local=_resolve_local_db_dsn(),
            db_dsn_server=_resolve_server_db_dsn(),
            data_mode=_resolve_data_mode(),
            local_only_mode=_local_only_mode_enabled(),
            db_connect_timeout_s=_env_int("DB_CONNECT_TIMEOUT_S", 10),

            # Console schema (shared DB)
            schema_console=_env("SCHEMA_CONSOLE", "console") or "console",

            # Auth
            session_ttl_hours=_env_int("CONSOLE_SESSION_TTL_HOURS", 24),
            password_min_len=_env_int("CONSOLE_PASSWORD_MIN_LEN", 6),

            # Phase4 API
            phase4_api_url=_env("PHASE4_API_URL", "http://127.0.0.1:8000") or "http://127.0.0.1:8000",
            phase4_api_key=_env("PHASE4_API_KEY", None),
            phase4_timeout_s=_env_float("PHASE4_TIMEOUT_S", 20.0),

            # Geo API
            geo_api_base_url=_env("GEO_API_BASE_URL", "") or "",
            geo_api_public_base_url=_env("GEO_API_PUBLIC_BASE_URL", _env("GEO_API_BASE_URL", "")) or "",
            geo_api_timeout_s=_env_float("GEO_API_TIMEOUT_S", 20.0),

            # Defaults
            default_bbox=_env("DATAMIND_DEFAULT_BBOX", "-0.38,-78.60,-0.02,-78.35") or "-0.38,-78.60,-0.02,-78.35",
            default_limit=_env_int("DATAMIND_DEFAULT_LIMIT", 50),

            # CORS
            cors_allow_origins=_env_tuple("CONSOLE_CORS_ALLOW_ORIGINS", ("*",)),

            # Repo root
            repo_root=repo_root(),
        )


# Global singleton config (Streamlit-safe)
CFG = AppConfig.from_env()


# ============================================================
# CONSOLE TABLES (NO HARD-CODE)
# ============================================================

T_USERS = _tbl(CFG.schema_console, "users")
T_ROLES = _tbl(CFG.schema_console, "roles")
T_USER_ROLES = _tbl(CFG.schema_console, "user_roles")

T_SESSIONS = _tbl(CFG.schema_console, "sessions")
T_AUDIT_EVENTS = _tbl(CFG.schema_console, "audit_events")
T_PHASE_DECISIONS = _tbl(CFG.schema_console, "phase_decisions")
T_WORKSPACE_STATE = _tbl(CFG.schema_console, "workspace_state")


def validate_config() -> None:
    if not (resolve_active_db_dsn() or CFG.db_dsn):
        raise RuntimeError(
            "No database DSN configured.\n"
            "Fix it by exporting:\n"
            '  export DB_DSN="postgresql://user:pass@host:5432/dbname?sslmode=require"\n'
            "or set DATABASE_URL / DATAMIND_CONSOLE_DB_DSN / SUPABASE_DB_*."
        )
