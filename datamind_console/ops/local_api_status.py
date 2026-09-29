from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib import error as urlerror
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import OpsConfig

LogFn = Callable[[str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(log: Optional[LogFn], message: str) -> None:
    if log:
        log(message)


def _default_backend_base() -> str:
    explicit = (
        os.getenv("LOCAL_BACKEND_BASE_URL")
        or os.getenv("TRANSPORT_API_BASE_URL")
        or os.getenv("TRANSPORT_BACKEND_BASE_URL")
        or ""
    ).strip()
    if explicit:
        return explicit.rstrip("/")

    port_raw = (os.getenv("BACKEND_HOST_PORT") or "3000").strip()
    try:
        port = int(port_raw)
    except Exception:
        port = 3000
    return f"http://127.0.0.1:{port}"


def _json_http_get(url: str, *, timeout: int = 12) -> dict[str, Any]:
    req = Request(url, method="GET")
    raw = b""
    status_code: Optional[int] = None

    try:
        with urlopen(req, timeout=timeout) as resp:
            status_code = int(getattr(resp, "status", 200) or 200)
            raw = resp.read(1024 * 1024)
    except urlerror.HTTPError as exc:
        status_code = int(exc.code)
        try:
            raw = exc.read(1024 * 1024)
        except Exception:
            raw = b""
    except Exception as exc:
        return {
            "ok": False,
            "reachable": False,
            "json_ok": False,
            "status_code": None,
            "url": url,
            "error": str(exc),
            "body_preview": "",
            "payload": None,
        }

    body = raw.decode("utf-8", errors="replace")
    payload: Any = None
    json_ok = False
    parse_error = ""
    if body.strip():
        try:
            payload = json.loads(body)
            json_ok = True
        except Exception as exc:
            parse_error = str(exc)

    ok = bool(status_code is not None and 200 <= int(status_code) < 300 and json_ok)
    out = {
        "ok": ok,
        "reachable": True,
        "json_ok": json_ok,
        "status_code": status_code,
        "url": url,
        "error": parse_error,
        "body_preview": body[:500],
        "payload": payload,
    }
    return out


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return num


def _collect_coords(payload: Any, *, limit: int = 10) -> list[tuple[float, float]]:
    found: list[tuple[float, float]] = []

    def add(lat_raw: Any, lon_raw: Any) -> None:
        if len(found) >= limit:
            return
        lat = _to_float(lat_raw)
        lon = _to_float(lon_raw)
        if lat is None or lon is None:
            return
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return
        for prev_lat, prev_lon in found:
            if abs(prev_lat - lat) < 1e-8 and abs(prev_lon - lon) < 1e-8:
                return
        found.append((lat, lon))

    def walk(obj: Any, depth: int) -> None:
        if depth > 4 or len(found) >= limit:
            return
        if isinstance(obj, dict):
            add(
                obj.get("lat", obj.get("stop_lat", obj.get("stopLat", obj.get("latitude")))),
                obj.get("lon", obj.get("stop_lon", obj.get("stopLon", obj.get("longitude")))),
            )
            for value in obj.values():
                if isinstance(value, (dict, list, tuple)):
                    walk(value, depth + 1)
        elif isinstance(obj, (list, tuple)):
            for item in obj[:200]:
                walk(item, depth + 1)
                if len(found) >= limit:
                    break

    walk(payload, 0)
    return found


def _build_route_url(*, backend_base: str, from_lat: float, from_lon: float, to_lat: float, to_lon: float) -> str:
    now = datetime.now()
    params = {
        "fromLat": f"{from_lat:.6f}",
        "fromLon": f"{from_lon:.6f}",
        "toLat": f"{to_lat:.6f}",
        "toLon": f"{to_lon:.6f}",
        "profile": "walk",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "forceFresh": "true",
        "numItineraries": "1",
    }
    return f"{backend_base.rstrip('/')}/api/routes?{urlencode(params)}"


def check_local_backend_apis(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    checked_at = _now_iso()
    backend_base = _default_backend_base()
    otp_router_url = (cfg.otp_local_health_url or "http://127.0.0.1:8080/otp/routers/default").rstrip("/")
    otp_stops_url = f"{otp_router_url}/index/stops"

    _log(log, f"Checking local OTP router: {otp_router_url}")
    otp_router = _json_http_get(otp_router_url, timeout=12)

    _log(log, f"Checking local OTP stops endpoint: {otp_stops_url}")
    otp_stops = _json_http_get(otp_stops_url, timeout=20)

    _log(log, f"Checking local backend health: {backend_base}/health")
    backend_health = _json_http_get(f"{backend_base}/health", timeout=12)

    _log(log, f"Checking backend geo health: {backend_base}/api/geo/health")
    backend_geo_health = _json_http_get(f"{backend_base}/api/geo/health", timeout=12)
    geo_payload = backend_geo_health.get("payload") if isinstance(backend_geo_health.get("payload"), dict) else {}
    geo_checks = geo_payload.get("checks") if isinstance(geo_payload.get("checks"), dict) else {}
    geo_status = str(geo_payload.get("status") or "").strip().lower()
    geo_db_ok = bool(geo_checks.get("db"))
    geo_nominatim_ok = bool(geo_checks.get("nominatim"))
    geo_service_ok = bool(
        backend_geo_health.get("ok")
        and geo_status == "healthy"
        and geo_db_ok
        and geo_nominatim_ok
    )
    geo_reason = "ok" if geo_service_ok else (
        str(backend_geo_health.get("error") or "")
        or f"Geo health is {geo_payload.get('status') or 'unknown'} (db={geo_db_ok}, nominatim={geo_nominatim_ok})"
    )

    route_probe: dict[str, Any] = {
        "ok": False,
        "status_code": None,
        "url": "",
        "reason": "OTP stops payload did not expose usable coordinates",
        "response": None,
    }

    coords = _collect_coords(otp_stops.get("payload")) if otp_stops.get("json_ok") else []
    if coords:
        from_lat, from_lon = coords[0]
        candidate_targets: list[tuple[float, float]] = []
        candidate_targets.append((from_lat + 0.001, from_lon + 0.001))
        if len(coords) > 1:
            candidate_targets.append(coords[1])

        for to_lat, to_lon in candidate_targets:
            route_url = _build_route_url(
                backend_base=backend_base,
                from_lat=from_lat,
                from_lon=from_lon,
                to_lat=to_lat,
                to_lon=to_lon,
            )
            _log(log, f"Checking backend /api/routes with local OTP: {route_url}")
            route_res = _json_http_get(route_url, timeout=35)
            payload = route_res.get("payload") if isinstance(route_res.get("payload"), dict) else {}
            status_code = route_res.get("status_code")
            success = bool(payload.get("success"))

            route_probe = {
                "ok": bool(route_res.get("json_ok") and status_code == 200 and success),
                "status_code": status_code,
                "url": route_url,
                "reason": "ok" if (status_code == 200 and success) else str(payload.get("message") or route_res.get("error") or "route check failed"),
                "response": payload,
            }
            if route_probe["ok"]:
                break

    checks = {
        "otp_router_health": {
            "ok": bool(otp_router.get("ok")),
            "status_code": otp_router.get("status_code"),
            "url": otp_router.get("url"),
            "reason": "ok" if otp_router.get("ok") else str(otp_router.get("error") or "OTP router endpoint is not healthy"),
        },
        "otp_stops_json": {
            "ok": bool(otp_stops.get("ok") and otp_stops.get("json_ok")),
            "status_code": otp_stops.get("status_code"),
            "url": otp_stops.get("url"),
            "reason": "ok" if (otp_stops.get("ok") and otp_stops.get("json_ok")) else str(otp_stops.get("error") or "OTP stops endpoint did not return valid JSON"),
            "found_coords": len(coords),
        },
        "backend_health": {
            "ok": bool(backend_health.get("ok")),
            "status_code": backend_health.get("status_code"),
            "url": backend_health.get("url"),
            "reason": "ok" if backend_health.get("ok") else str(backend_health.get("error") or "Backend /health failed"),
        },
        "backend_geo_health": {
            "ok": geo_service_ok,
            "status_code": backend_geo_health.get("status_code"),
            "url": backend_geo_health.get("url"),
            "reason": geo_reason,
            "status": geo_payload.get("status"),
            "checks": geo_checks,
        },
        "backend_routes": route_probe,
    }

    all_ok = all(bool(item.get("ok")) for item in checks.values())

    return {
        "ok": all_ok,
        "status": "STATUS_AWESOME" if all_ok else "STATUS_NEEDS_ATTENTION",
        "checked_at": checked_at,
        "backend_base_url": backend_base,
        "otp_router_url": otp_router_url,
        "checks": checks,
        "raw": {
            "otp_router": otp_router,
            "otp_stops": otp_stops,
            "backend_health": backend_health,
            "backend_geo_health": backend_geo_health,
        },
    }


def _endpoint_url(base_url: str, path: str, params: Optional[dict[str, Any]] = None) -> str:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
    clean = f"{base_url.rstrip('/')}{path}"
    return clean if not query else f"{clean}?{query}"


def _payload_success(payload: Any) -> Optional[bool]:
    if isinstance(payload, dict) and "success" in payload:
        return bool(payload.get("success"))
    return None


def _run_json_endpoint(
    *,
    name: str,
    base_url: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    timeout: int = 16,
) -> dict[str, Any]:
    url = _endpoint_url(base_url, path, params)
    res = _json_http_get(url, timeout=timeout)
    payload = res.get("payload")
    ok = bool(res.get("ok"))
    reason = "ok"

    success_flag = _payload_success(payload)
    if ok and success_flag is False:
        ok = False
        reason = "JSON payload contains success=false"

    if not ok and reason == "ok":
        reason = str(res.get("error") or f"HTTP {res.get('status_code') or '?'}")

    return {
        "ok": ok,
        "status_code": res.get("status_code"),
        "url": url,
        "reason": reason,
        "response": payload if isinstance(payload, dict) else None,
        "name": name,
    }


def run_all_local_server_api_checks(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    core = check_local_backend_apis(cfg, log=log)
    backend_base = str(core.get("backend_base_url") or _default_backend_base())

    fallback_lat = -0.210000
    fallback_lon = -78.490000
    coords = _collect_coords(((core.get("raw") or {}).get("otp_stops") or {}).get("payload"))
    if coords:
        fallback_lat, fallback_lon = coords[0]

    _log(log, "Running full local server API checks (Run ALL APIs)")
    checks = dict(core.get("checks") or {})

    endpoint_checks = [
        ("geo_geocode", "/api/geo/geocode", {"q": "terminal quitumbe", "top_k": 2}),
        ("geo_autocomplete", "/api/geo/autocomplete", {"q": "term", "top_k": 3}),
        ("geo_reverse", "/api/geo/reverse", {"lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}", "top_k": 2}),
        ("search_global", "/api/search", {"q": "terminal"}),
        ("search_lines", "/api/search/lines", {"q": "ecovia"}),
        ("search_stations", "/api/search/stations", {"q": "terminal"}),
        ("search_geocode_alias", "/api/search/geocode", {"q": "terminal quitumbe", "top_k": 2}),
        ("search_reverse_alias", "/api/search/reversegeocode", {"lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}", "top_k": 2}),
        ("search_autocomplete", "/api/search/autocomplete", {"q": "term", "lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}"}),
        ("lines_search", "/api/lines/search", {"q": "ecovia"}),
        ("lines_mode_bus", "/api/lines/mode/bus", {}),
        ("stations_nearby", "/api/stations/nearby", {"lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}", "radius": 1500}),
    ]

    for name, path, params in endpoint_checks:
        _log(log, f"Checking {path}")
        probe = _run_json_endpoint(
            name=name,
            base_url=backend_base,
            path=path,
            params=params,
            timeout=22,
        )

        if name == "geo_reverse" and probe.get("ok"):
            payload = probe.get("response") or {}
            if isinstance(payload, dict) and "candidates" in payload and not isinstance(payload.get("candidates"), list):
                probe["ok"] = False
                probe["reason"] = "Geo reverse candidates is not a JSON list"

        checks[name] = probe

    all_ok = all(bool(item.get("ok")) for item in checks.values())
    failed = sorted(name for name, item in checks.items() if not bool(item.get("ok")))

    return {
        "ok": all_ok,
        "all_ok": all_ok,
        "status": "STATUS_AWESOME" if all_ok else "STATUS_FALSE",
        "checked_at": _now_iso(),
        "backend_base_url": backend_base,
        "checks": checks,
        "failed_checks": failed,
        "raw": {
            "core": core,
        },
    }
