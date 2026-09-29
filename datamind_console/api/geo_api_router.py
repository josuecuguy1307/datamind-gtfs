
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from datamind_console.services.geo_api_service import GeoApiService, geo_api_error


router = APIRouter(prefix="/api/geo", tags=["Geo API"])
_service = GeoApiService()
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, deque[float]] = {}


def _error(status_code: int, code: str, message: str, details: Optional[dict] = None) -> JSONResponse:
    return JSONResponse(status_code=int(status_code), content=geo_api_error(code, message, details))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _allowed_api_keys() -> set[str]:
    raw = (
        os.getenv("GEO_API_API_KEYS")
        or os.getenv("GEO_API_KEYS")
        or os.getenv("GEO_API_ALLOWED_KEYS")
        or ""
    )
    out: set[str] = set()
    for item in raw.split(","):
        key = item.strip()
        if key:
            out.add(key)
    return out


def _extract_api_key(request: Request) -> Optional[str]:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    x_key = (request.headers.get("X-API-Key") or "").strip()
    return x_key or None


def _is_rate_limited(bucket_key: str, limit_per_min: int) -> bool:
    if limit_per_min <= 0:
        return False
    now = time.monotonic()
    window_start = now - 60.0
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS.get(bucket_key)
        if bucket is None:
            bucket = deque()
            _RATE_BUCKETS[bucket_key] = bucket
        while bucket and bucket[0] < window_start:
            bucket.popleft()
        if len(bucket) >= limit_per_min:
            return True
        bucket.append(now)
    return False


def _guard(request: Request, *, enforce_auth: bool = True) -> Optional[JSONResponse]:
    require_auth = _env_bool("GEO_API_REQUIRE_AUTH", False)
    keys = _allowed_api_keys()
    api_key = _extract_api_key(request)
    key_hint = "anon" if not api_key else "key"
    client = request.client.host if request.client else "unknown"

    if enforce_auth and require_auth:
        if not api_key:
            return _error(
                401,
                "AUTH_REQUIRED",
                "Missing API key. Use Authorization: Bearer <API_KEY>.",
                {"header": "Authorization or X-API-Key"},
            )
        if not keys:
            return _error(
                503,
                "AUTH_MISCONFIGURED",
                "GEO_API_REQUIRE_AUTH is enabled but no API keys are configured.",
                {"expected_env": "GEO_API_API_KEYS"},
            )
        if api_key not in keys:
            return _error(403, "AUTH_FORBIDDEN", "Invalid API key.", {})

    rate_limit = _env_int("GEO_API_RATE_LIMIT_PER_MIN", 0)
    bucket_key = f"{client}:{key_hint}"
    if _is_rate_limited(bucket_key, rate_limit):
        return _error(
            429,
            "RATE_LIMITED",
            "Rate limit exceeded. Retry in a minute.",
            {"limit_per_min": rate_limit},
        )
    return None


@router.get("/health")
def geo_health(request: Request):
    public_health = _env_bool("GEO_API_PUBLIC_HEALTH", True)
    block = _guard(request, enforce_auth=not public_health)
    if block is not None:
        return block
    try:
        payload = _service.local_health()
        payload["security"] = {
            "auth_required": _env_bool("GEO_API_REQUIRE_AUTH", False),
            "public_health": public_health,
            "api_keys_configured": len(_allowed_api_keys()),
            "rate_limit_per_min": _env_int("GEO_API_RATE_LIMIT_PER_MIN", 0),
        }
        return payload
    except Exception as e:
        return _error(503, "GEO_API_HEALTH_FAILED", "Geo API health check failed.", {"exception": str(e)})


@router.get("/geocode")
def geo_geocode(
    request: Request,
    q: str = Query(..., min_length=1),
    top_k: int = Query(10, ge=1, le=100),
    bbox: Optional[str] = Query(None),
    area_key: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
):
    block = _guard(request)
    if block is not None:
        return block
    try:
        return _service.geocode_local(
            q=q,
            top_k=int(top_k),
            bbox=bbox,
            area_key=area_key,
            types=types,
            language=language,
        )
    except Exception as e:
        return _error(500, "GEOCODE_FAILED", "Failed to run geocode request.", {"exception": str(e)})


@router.get("/autocomplete")
def geo_autocomplete(
    request: Request,
    q: str = Query(..., min_length=1),
    top_k: int = Query(10, ge=1, le=100),
    bbox: Optional[str] = Query(None),
    area_key: Optional[str] = Query(None),
    types: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
):
    block = _guard(request)
    if block is not None:
        return block
    try:
        return _service.autocomplete_local(
            q=q,
            top_k=int(top_k),
            bbox=bbox,
            area_key=area_key,
            types=types,
            language=language,
        )
    except Exception as e:
        return _error(500, "AUTOCOMPLETE_FAILED", "Failed to run autocomplete request.", {"exception": str(e)})


@router.get("/reverse")
def geo_reverse(
    request: Request,
    lat: float = Query(...),
    lon: float = Query(...),
    top_k: int = Query(10, ge=1, le=100),
    radius_m: float = Query(1200.0, ge=50.0, le=20000.0),
    types: Optional[str] = Query(None),
    area_key: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
):
    block = _guard(request)
    if block is not None:
        return block
    try:
        return _service.reverse_local(
            lat=float(lat),
            lon=float(lon),
            top_k=int(top_k),
            radius_m=float(radius_m),
            types=types,
            area_key=area_key,
            q=q,
        )
    except Exception as e:
        return _error(500, "REVERSE_FAILED", "Failed to run reverse geocode request.", {"exception": str(e)})
