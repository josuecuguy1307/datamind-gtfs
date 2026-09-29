from __future__ import annotations

import os
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import streamlit as st

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

from datamind_console.services.geo_api_service import GeoApiService


def _auth_headers(api_key: Optional[str]) -> dict[str, str]:
    token = (api_key or "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _response_ok(resp: Any) -> bool:
    return isinstance(resp, dict) and "error" not in resp


def _http_get(
    *,
    base_url: str,
    path: str,
    params: dict[str, Any],
    headers: Optional[dict[str, str]] = None,
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    base = str(base_url or "").rstrip("/")
    url = f"{base}{path}"
    query = {k: v for k, v in (params or {}).items() if v is not None}
    req_headers = dict(headers or {})

    if requests is not None:
        try:
            resp = requests.get(url, params=query, headers=req_headers, timeout=timeout_s)
        except Exception as e:
            return {"ok": False, "error": str(e), "url": url}
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = {"raw": (resp.text or "")[:2000]}
        return {
            "ok": 200 <= resp.status_code < 300,
            "status_code": int(resp.status_code),
            "url": url,
            "payload": body,
        }

    qs = urlencode(query)
    full = url if not qs else f"{url}?{qs}"
    req = Request(full, method="GET")
    for k, v in req_headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200) or 200)
    except Exception as e:
        return {"ok": False, "error": str(e), "url": full}

    try:
        import json

        parsed: Any = json.loads(raw) if raw else {}
    except Exception:
        parsed = {"raw": raw[:2000]}
    return {"ok": 200 <= status < 300, "status_code": status, "url": full, "payload": parsed}


def _check_geo_contract(endpoint: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": False, "reason": "Payload is not JSON object"}
    if "error" in payload:
        err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        return {"ok": False, "reason": f"{err.get('code') or 'ERROR'}: {err.get('message') or 'Request failed'}"}
    root_keys = {"query", "endpoint", "timings_ms", "candidates"}
    missing_root = [k for k in root_keys if k not in payload]
    if missing_root:
        return {"ok": False, "reason": f"Missing root keys: {', '.join(missing_root)}"}
    if str(payload.get("endpoint") or "") != endpoint:
        return {"ok": False, "reason": f"Endpoint mismatch: expected {endpoint}"}
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return {"ok": False, "reason": "candidates is not a list"}
    if candidates:
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        cand_keys = {"id", "name", "entity_type", "lat", "lon", "score", "score_parts", "source"}
        missing = [k for k in cand_keys if k not in first]
        if missing:
            return {"ok": False, "reason": f"Missing candidate keys: {', '.join(missing)}"}
    return {"ok": True, "reason": "Contract OK"}


def _status_message(title: str, ok: bool, detail: str) -> None:
    if ok:
        st.success(f"{title}: {detail}")
    else:
        st.error(f"{title}: {detail}")


def render_geo_api_fusion_view(*, analytics: Any = None, audit: Any = None, **_) -> None:
    _ = analytics
    _ = audit
    service = GeoApiService()
    ss = st.session_state
    ss.setdefault("geo.fusion.results", {})

    transport_base = (
        os.getenv("TRANSPORT_API_BASE_URL")
        or os.getenv("TRANSPORT_BACKEND_BASE_URL")
        or "http://127.0.0.1:3000"
    ).rstrip("/")

    st.title("Geo API Fusion")
    st.caption("Single geocoder API contract for app + backend (`/api/geo/*`).")

    c1, c2 = st.columns(2)
    c1.metric("DataMind Geo API", service.http_base_url)
    c2.metric("Transport backend", transport_base)

    api_key = st.text_input(
        "Geo API key (optional; required only if auth is enabled)",
        type="password",
        key="geo.fusion.api_key",
    )

    if st.button("Run Fusion Check", type="primary", use_container_width=True):
        headers = _auth_headers(api_key)
        ss["geo.fusion.results"] = {
            "datamind_health": service.health_http(api_key=(api_key or None)),
            "transport_health": _http_get(base_url=transport_base, path="/api/geo/health", params={}, headers=headers),
            "transport_geocode": _http_get(
                base_url=transport_base,
                path="/api/geo/geocode",
                params={"q": "terminal quitumbe", "top_k": 3},
                headers=headers,
            ),
            "transport_autocomplete": _http_get(
                base_url=transport_base,
                path="/api/geo/autocomplete",
                params={"q": "term", "top_k": 5},
                headers=headers,
            ),
            "transport_reverse": _http_get(
                base_url=transport_base,
                path="/api/geo/reverse",
                params={"lat": -0.28, "lon": -78.52, "top_k": 3, "radius_m": 1200},
                headers=headers,
            ),
            "legacy_alias_geocode": _http_get(
                base_url=transport_base,
                path="/api/search/geocode",
                params={"q": "terminal quitumbe", "top_k": 3},
                headers=headers,
            ),
        }

    results = ss.get("geo.fusion.results") or {}
    if not results:
        st.info("Run fusion check to validate one-API integration.")
        return

    datamind_health = results.get("datamind_health")
    transport_health = results.get("transport_health") or {}
    geo = results.get("transport_geocode") or {}
    auto = results.get("transport_autocomplete") or {}
    rev = results.get("transport_reverse") or {}
    legacy = results.get("legacy_alias_geocode") or {}

    geo_check = _check_geo_contract("geocode", geo.get("payload"))
    auto_check = _check_geo_contract("autocomplete", auto.get("payload"))
    rev_check = _check_geo_contract("reverse", rev.get("payload"))
    legacy_check = _check_geo_contract("geocode", legacy.get("payload"))

    st.markdown("#### Fusion Status")
    s1, s2 = st.columns(2)
    with s1:
        _status_message(
            "DataMind Geo API health",
            _response_ok(datamind_health),
            "reachable" if _response_ok(datamind_health) else str((datamind_health or {}).get("error") or "failed"),
        )
        _status_message(
            "Transport /api/geo/health",
            bool(transport_health.get("ok")),
            f"HTTP {transport_health.get('status_code')}" if transport_health.get("status_code") else str(transport_health.get("error") or "failed"),
        )
        _status_message("Transport /api/geo/geocode", bool(geo_check.get("ok")), str(geo_check.get("reason")))
    with s2:
        _status_message("Transport /api/geo/autocomplete", bool(auto_check.get("ok")), str(auto_check.get("reason")))
        _status_message("Transport /api/geo/reverse", bool(rev_check.get("ok")), str(rev_check.get("reason")))
        _status_message("Legacy alias /api/search/geocode", bool(legacy_check.get("ok")), str(legacy_check.get("reason")))

    st.markdown("#### Single API Contract")
    st.code(
        "\n".join(
            [
                f"TRANSPORT_API_BASE_URL={transport_base}",
                f"GEO_API_BASE_URL={service.public_base_url}",
                "GEO_API_GEOCODE_PATH=/api/geo/geocode",
                "# Keep /api/search/geocode only as compatibility alias (same response contract).",
            ]
        ),
        language="bash",
    )

    header_cmd = '-H "Authorization: Bearer <API_KEY>"' if api_key else ""
    st.markdown("#### Curl")
    st.code(
        "\n".join(
            [
                f'curl -sS -G "{transport_base}/api/geo/health" {header_cmd}'.strip(),
                f'curl -sS -G "{transport_base}/api/geo/geocode" --data-urlencode "q=terminal quitumbe" --data-urlencode "top_k=3" {header_cmd}'.strip(),
                f'curl -sS -G "{transport_base}/api/geo/autocomplete" --data-urlencode "q=term" --data-urlencode "top_k=5" {header_cmd}'.strip(),
                f'curl -sS -G "{transport_base}/api/geo/reverse" --data-urlencode "lat=-0.28" --data-urlencode "lon=-78.52" --data-urlencode "top_k=3" {header_cmd}'.strip(),
            ]
        ),
        language="bash",
    )

    with st.expander("Raw fusion check payloads", expanded=False):
        st.json(results)
