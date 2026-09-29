from __future__ import annotations

from typing import Any, Dict, Optional

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

from datamind_console.common.config import CFG
from datamind_console.phases.phase2_semantics.client import Phase2Client


def geo_api_error(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "error": {
            "code": str(code),
            "message": str(message),
            "details": details or {},
        }
    }


class GeoApiService:
    def __init__(
        self,
        *,
        phase2: Optional[Phase2Client] = None,
        http_base_url: Optional[str] = None,
        public_base_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self.phase2 = phase2 or Phase2Client()
        self.http_base_url = str(http_base_url or CFG.geo_api_base_url or "http://127.0.0.1:8006").rstrip("/")
        self.public_base_url = str(public_base_url or CFG.geo_api_public_base_url or self.http_base_url).rstrip("/")
        self.timeout_s = float(timeout_s or CFG.geo_api_timeout_s or 20.0)

    # ------------------------------------------------------------------
    # Product metadata
    # ------------------------------------------------------------------
    def product_meta(self) -> Dict[str, Any]:
        return {
            "name": "DataMind Geo API",
            "version": "Geo API v1",
            "base_url": self.public_base_url,
            "auth_header": "Authorization: Bearer <API_KEY>",
            "rate_limits": "Placeholder: standard per-key quotas coming soon.",
        }

    # ------------------------------------------------------------------
    # Local implementation (Phase2Client-backed)
    # ------------------------------------------------------------------
    def local_health(self) -> Dict[str, Any]:
        return self.phase2.geo_api_health()

    def geocode_local(
        self,
        *,
        q: str,
        top_k: int = 10,
        bbox: Optional[str] = None,
        area_key: Optional[str] = None,
        types: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.phase2.geo_api_geocode(
            query_text=q,
            top_k=top_k,
            bbox=bbox,
            area_key=area_key,
            types=types,
            language=language,
        )

    def autocomplete_local(
        self,
        *,
        q: str,
        top_k: int = 10,
        bbox: Optional[str] = None,
        area_key: Optional[str] = None,
        types: Optional[str] = None,
        language: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.phase2.geo_api_autocomplete(
            query_text=q,
            top_k=top_k,
            bbox=bbox,
            area_key=area_key,
            types=types,
            language=language,
        )

    def reverse_local(
        self,
        *,
        lat: float,
        lon: float,
        top_k: int = 10,
        radius_m: float = 1200.0,
        types: Optional[str] = None,
        area_key: Optional[str] = None,
        q: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.phase2.geo_api_reverse(
            lat=lat,
            lon=lon,
            top_k=top_k,
            radius_m=radius_m,
            types=types,
            area_key=area_key,
            query_text=q,
        )

    # ------------------------------------------------------------------
    # HTTP implementation (calls /api/geo/* contract)
    # ------------------------------------------------------------------
    @staticmethod
    def _auth_headers(api_key: Optional[str]) -> Dict[str, str]:
        key = (api_key or "").strip()
        if not key:
            return {}
        return {"Authorization": f"Bearer {key}"}

    def _http_get(
        self,
        path: str,
        *,
        params: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.http_base_url}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        req_headers = dict(headers or {})

        if requests is not None:
            try:
                resp = requests.get(url, params=clean_params, timeout=self.timeout_s, headers=req_headers)
            except Exception as e:
                return geo_api_error(
                    code="HTTP_REQUEST_FAILED",
                    message="Could not reach Geo API HTTP endpoint.",
                    details={"url": url, "exception": str(e)},
                )

            try:
                body = resp.json()
            except Exception:
                body = None

            if resp.status_code >= 400:
                if isinstance(body, dict) and "error" in body:
                    return body
                return geo_api_error(
                    code=f"HTTP_{resp.status_code}",
                    message="Geo API HTTP endpoint returned an error.",
                    details={
                        "url": url,
                        "status_code": int(resp.status_code),
                        "body": (resp.text or "")[:2000],
                    },
                )

            if isinstance(body, dict):
                return body
            return geo_api_error(
                code="INVALID_RESPONSE",
                message="Geo API HTTP endpoint did not return JSON object.",
                details={"url": url, "status_code": int(resp.status_code)},
            )

        query = urlencode(clean_params)
        full_url = url if not query else f"{url}?{query}"
        req = Request(full_url, method="GET")
        for k, v in req_headers.items():
            req.add_header(k, v)
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                status_code = int(getattr(resp, "status", 200) or 200)
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return geo_api_error(
                code="HTTP_REQUEST_FAILED",
                message="Could not reach Geo API HTTP endpoint.",
                details={"url": full_url, "exception": str(e)},
            )

        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = None

        if status_code >= 400:
            if isinstance(body, dict) and "error" in body:
                return body
            return geo_api_error(
                code=f"HTTP_{status_code}",
                message="Geo API HTTP endpoint returned an error.",
                details={"url": full_url, "status_code": status_code, "body": (raw or "")[:2000]},
            )

        if isinstance(body, dict):
            return body
        return geo_api_error(
            code="INVALID_RESPONSE",
            message="Geo API HTTP endpoint did not return JSON object.",
            details={"url": full_url, "status_code": status_code},
        )

    def health_http(self, *, api_key: Optional[str] = None) -> Dict[str, Any]:
        return self._http_get(
            "/api/geo/health",
            params={},
            headers=self._auth_headers(api_key),
        )

    def geocode_http(
        self,
        *,
        q: str,
        top_k: int = 10,
        bbox: Optional[str] = None,
        area_key: Optional[str] = None,
        types: Optional[str] = None,
        language: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._http_get(
            "/api/geo/geocode",
            params={
                "q": q,
                "top_k": int(top_k),
                "bbox": bbox,
                "area_key": area_key,
                "types": types,
                "language": language,
            },
            headers=self._auth_headers(api_key),
        )

    def autocomplete_http(
        self,
        *,
        q: str,
        top_k: int = 10,
        bbox: Optional[str] = None,
        area_key: Optional[str] = None,
        types: Optional[str] = None,
        language: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._http_get(
            "/api/geo/autocomplete",
            params={
                "q": q,
                "top_k": int(top_k),
                "bbox": bbox,
                "area_key": area_key,
                "types": types,
                "language": language,
            },
            headers=self._auth_headers(api_key),
        )

    def reverse_http(
        self,
        *,
        lat: float,
        lon: float,
        top_k: int = 10,
        radius_m: float = 1200.0,
        types: Optional[str] = None,
        area_key: Optional[str] = None,
        q: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._http_get(
            "/api/geo/reverse",
            params={
                "lat": float(lat),
                "lon": float(lon),
                "top_k": int(top_k),
                "radius_m": float(radius_m),
                "types": types,
                "area_key": area_key,
                "q": q,
            },
            headers=self._auth_headers(api_key),
        )
