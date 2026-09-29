"""
ML DATAMIND GTFS – Settings (Console-Specific)

RULE:
- Console settings are built on top of datamind_core global settings.
- The console uses the SAME physical database as phases (datamind_ml),
  but a dedicated schema: console.
- No magic strings across services/repos. Tables defined here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from datamind_core.settings import (
    APP_ENV,
    LOG_LEVEL,
    DB_DSN as CORE_DB_DSN,
    SCHEMA_CONSOLE,
    tbl,
)


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(__file__).resolve().parents[1]  # datamind_console/


# ============================================================
# SETTINGS OBJECT
# ============================================================

@dataclass(frozen=True)
class ConsoleSettings:
    # Environment
    env: str
    log_level: str

    # Database
    db_dsn: str
    db_connect_timeout_s: int

    # Console schema
    schema_console: str

    # Auth / sessions
    session_ttl_hours: int
    password_min_len: int

    # API/UI
    cors_allow_origins: Tuple[str, ...]


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v and v.strip() else default


def _env_tuple(key: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    v = os.getenv(key)
    if not v or not v.strip():
        return default
    # comma-separated list
    parts = [p.strip() for p in v.split(",") if p.strip()]
    return tuple(parts) if parts else default


def build_console_settings() -> ConsoleSettings:
    """
    Console Settings resolution order:
      1) DATAMIND_CONSOLE_DB_DSN (optional override)
      2) DB_DSN / DATABASE_URL (core)
      3) fallback dev DSN from core
    """
    db_dsn = os.getenv("DATAMIND_CONSOLE_DB_DSN") or CORE_DB_DSN

    return ConsoleSettings(
        env=os.getenv("APP_ENV", APP_ENV),
        log_level=os.getenv("LOG_LEVEL", LOG_LEVEL),

        db_dsn=db_dsn,
        db_connect_timeout_s=_env_int("DB_CONNECT_TIMEOUT_S", 10),

        schema_console=os.getenv("SCHEMA_CONSOLE", SCHEMA_CONSOLE),

        session_ttl_hours=_env_int("CONSOLE_SESSION_TTL_HOURS", 24),
        password_min_len=_env_int("CONSOLE_PASSWORD_MIN_LEN", 6),

        cors_allow_origins=_env_tuple("CONSOLE_CORS_ALLOW_ORIGINS", ("*",)),
    )


# Single global settings instance (what all console code imports)
CFG = build_console_settings()


# ============================================================
# CONSOLE TABLES (NO HARDCODE IN REPOS)
# ============================================================

T_USERS = tbl(CFG.schema_console, "users")
T_ROLES = tbl(CFG.schema_console, "roles")
T_USER_ROLES = tbl(CFG.schema_console, "user_roles")

T_SESSIONS = tbl(CFG.schema_console, "sessions")
T_AUDIT_EVENTS = tbl(CFG.schema_console, "audit_events")
T_PHASE_DECISIONS = tbl(CFG.schema_console, "phase_decisions")
T_WORKSPACE_STATE = tbl(CFG.schema_console, "workspace_state")


# ============================================================
# VALIDATION
# ============================================================

def validate_console_settings() -> None:
    if not CFG.db_dsn or not CFG.db_dsn.strip():
        raise RuntimeError(
            "[datamind_console] Missing DB DSN. "
            "Set DB_DSN (recommended) or DATAMIND_CONSOLE_DB_DSN."
        )
