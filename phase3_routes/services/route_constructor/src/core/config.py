# services/route_constructor/src/core/config.py
"""
Backward-compatible config facade.

All shared constants are defined in src.settings (single source of truth).
This module re-exports them via a frozen Settings dataclass so that any
code importing ``from src.core.config import settings`` continues to work.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.settings import (
    DB_DSN as _DB_DSN,
    OVERPASS_URL as _OVERPASS_URL,
    VALHALLA_URL as _VALHALLA_URL,
    HTTP_TIMEOUT_S as _HTTP_TIMEOUT_S,
    MAX_STOP_MATCH_RADIUS_M_STRICT as _STRICT,
    MAX_STOP_MATCH_RADIUS_M_RELAXED as _RELAXED,
    MIN_MATCHED_STOPS_STRICT as _MIN_STRICT,
    MIN_MATCHED_STOPS_RELAXED as _MIN_RELAXED,
    GENERATOR_VERSION as _GEN_VERSION,
)


@dataclass(frozen=True)
class Settings:
    # DB
    DB_DSN: str

    # External services
    OVERPASS_URL: str
    VALHALLA_BASE_URL: str
    HTTP_TIMEOUT_S: int

    # Matching thresholds (tune here)
    MAX_STOP_MATCH_RADIUS_M_STRICT: float
    MAX_STOP_MATCH_RADIUS_M_RELAXED: float
    MIN_MATCHED_STOPS_STRICT: int
    MIN_MATCHED_STOPS_RELAXED: int

    # Pipeline version tags
    GENERATOR_VERSION: str


settings = Settings(
    DB_DSN=_DB_DSN,
    OVERPASS_URL=_OVERPASS_URL,
    VALHALLA_BASE_URL=_VALHALLA_URL,
    HTTP_TIMEOUT_S=_HTTP_TIMEOUT_S,
    MAX_STOP_MATCH_RADIUS_M_STRICT=_STRICT,
    MAX_STOP_MATCH_RADIUS_M_RELAXED=_RELAXED,
    MIN_MATCHED_STOPS_STRICT=_MIN_STRICT,
    MIN_MATCHED_STOPS_RELAXED=_MIN_RELAXED,
    GENERATOR_VERSION=_GEN_VERSION,
)
