from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any, Iterable, Sequence

import requests


def _sha256_canonical(obj: Any) -> str:
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()

from src.constructor_v2.common import stable_hash
from src.constructor_v2.constants import (
    DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL,
    VALHALLA_HTTP_TIMEOUT_S,
    VALHALLA_PAIR_TIMEOUT_S,
    VALHALLA_STATUS_TIMEOUT_S,
)
from src.settings import VALHALLA_COSTING, VALHALLA_URL
from src.utils.polyline import decode_polyline6


LonLat = tuple[float, float]


@dataclass(slots=True)
class PairwiseCost:
    distance_m: float
    duration_s: float
    geometry: list[LonLat] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValhallaRouteResult:
    coordinates: list[LonLat]
    distance_m: float
    duration_s: float
    legs: list[dict[str, Any]]
    maneuvers: list[dict[str, Any]]
    diagnostics: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    # Full request/response capture — lands on
    # ``route_work.geometry_candidates.valhalla_request`` and graduates to
    # ``route_prod.routes.valhalla_request`` on promotion. Shape matches the
    # legacy client's ``valhalla_route_with_meta`` metadata.
    request_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValhallaOptimizedOrder:
    ordered_indices: list[int]
    diagnostics: dict[str, Any] = field(default_factory=dict)


def _action_name_for_path(path: str) -> str:
    return path.lstrip("/")


def _parse_available_actions_from_error(text: str) -> set[str]:
    if "Try any of:" not in text:
        return set()
    return set(re.findall(r"'(/?[^']+)'", text))


def _max_locations_per_call() -> int:
    raw = (os.getenv("VALHALLA_MAX_LOCATIONS_PER_CALL") or "").strip()
    if not raw:
        return 20
    try:
        return max(2, int(raw))
    except ValueError:
        return 20


def _decode_shape(shape: Any) -> list[LonLat]:
    if isinstance(shape, str):
        return [(float(lon), float(lat)) for lat, lon in decode_polyline6(shape)]
    if isinstance(shape, list):
        return [(float(pt[0]), float(pt[1])) for pt in shape]
    raise ValueError(f"Unsupported Valhalla shape payload: {type(shape)}")


class ValhallaClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        costing: str = VALHALLA_COSTING,
        shape_format: str = "geojson",
    ) -> None:
        resolved_base_url = (
            base_url
            or os.getenv("CONSTRUCTOR_V2_VALHALLA_URL")
            or DEFAULT_CONSTRUCTOR_V2_VALHALLA_URL
            or VALHALLA_URL
        )
        self.base_url = resolved_base_url.rstrip("/")
        self.costing = costing
        self.shape_format = shape_format
        self.session = requests.Session()
        self._pair_cache: dict[str, PairwiseCost] = {}
        self.endpoint_support: dict[str, bool | None] = {path: None for path in ("/status", "/locate", "/optimized_route", "/sources_to_targets", "/route")}
        self.available_actions: set[str] = set()
        self.capability_source: str = "uninitialized"
        self._capabilities_checked = False

    def _payload_locations(self, locations: Sequence[LonLat]) -> list[dict[str, Any]]:
        return [{"lat": lat, "lon": lon, "type": "break"} for lon, lat in locations]

    def _post(self, path: str, payload: dict[str, Any], *, timeout_s: int) -> requests.Response:
        url = f"{self.base_url}{path}"
        response = self.session.post(url, json=payload, timeout=timeout_s)
        if response.status_code == 404:
            self.endpoint_support[path] = False
        elif response.status_code < 500:
            self.endpoint_support[path] = True
        return response

    def _set_available_actions(self, actions: set[str], *, source: str, status_supported: bool) -> None:
        normalized = {_action_name_for_path(action) for action in actions}
        self.available_actions = normalized
        self.capability_source = source
        self.endpoint_support["/status"] = status_supported
        for path in self.endpoint_support:
            if path == "/status":
                continue
            self.endpoint_support[path] = _action_name_for_path(path) in normalized
        self._capabilities_checked = True

    def refresh_capabilities(self, *, force: bool = False) -> dict[str, bool | None]:
        if self._capabilities_checked and not force:
            return dict(self.endpoint_support)
        try:
            response = self.session.get(f"{self.base_url}/status", timeout=VALHALLA_STATUS_TIMEOUT_S)
        except requests.RequestException:
            self.capability_source = "status_request_error"
            self._capabilities_checked = True
            return dict(self.endpoint_support)

        if response.status_code < 400:
            payload = response.json()
            actions = set(payload.get("available_actions") or [])
            self._set_available_actions(actions, source="status_endpoint", status_supported=True)
            return dict(self.endpoint_support)

        self.endpoint_support["/status"] = False
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        actions = _parse_available_actions_from_error(str(payload.get("error") or response.text or ""))
        if actions:
            self._set_available_actions(actions, source="status_error_hint", status_supported=False)
        else:
            self.capability_source = f"status_http_{response.status_code}"
            self._capabilities_checked = True
        return dict(self.endpoint_support)

    def supports(self, path: str) -> bool:
        if path not in self.endpoint_support:
            raise KeyError(f"Unknown Valhalla capability path: {path}")
        self.refresh_capabilities()
        return bool(self.endpoint_support.get(path))

    def status(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/status", timeout=VALHALLA_STATUS_TIMEOUT_S)
        if response.status_code >= 400:
            raise RuntimeError(f"Valhalla /status failed ({response.status_code}): {response.text[:400]}")
        payload = response.json()
        self._set_available_actions(set(payload.get("available_actions") or []), source="status_endpoint", status_supported=True)
        return payload

    def _parse_route(self, payload: dict[str, Any]) -> ValhallaRouteResult:
        trip = (payload.get("trip") or {})
        legs = trip.get("legs") or []
        coordinates: list[LonLat] = []
        leg_rows: list[dict[str, Any]] = []
        maneuvers: list[dict[str, Any]] = []
        distance_m = 0.0
        duration_s = 0.0

        for index, leg in enumerate(legs):
            shape = _decode_shape(leg.get("shape"))
            if coordinates and shape and shape[0] == coordinates[-1]:
                shape = shape[1:]
            coordinates.extend(shape)
            summary = leg.get("summary") or {}
            leg_distance_m = float(summary.get("length", 0.0)) * 1000.0
            leg_duration_s = float(summary.get("time", 0.0))
            distance_m += leg_distance_m
            duration_s += leg_duration_s
            maneuvers.extend(leg.get("maneuvers") or [])
            leg_rows.append(
                {
                    "index": index,
                    "distance_m": leg_distance_m,
                    "duration_s": leg_duration_s,
                    "shape": shape,
                    "summary": summary,
                }
            )

        if len(legs) == 1 and not distance_m:
            summary = payload.get("trip", {}).get("summary") or {}
            distance_m = float(summary.get("length", 0.0)) * 1000.0
            duration_s = float(summary.get("time", 0.0))

        return ValhallaRouteResult(
            coordinates=coordinates,
            distance_m=distance_m,
            duration_s=duration_s,
            legs=leg_rows,
            maneuvers=maneuvers,
            raw=payload,
        )

    def _valhalla_version(self) -> str:
        """Best-effort Valhalla version, cached via status refresh."""
        try:
            self.refresh_capabilities()
        except Exception:
            pass
        return "unknown"

    def route(self, locations: Sequence[LonLat], *, timeout_s: int = VALHALLA_HTTP_TIMEOUT_S) -> ValhallaRouteResult:
        if len(locations) < 2:
            raise ValueError("Valhalla route requires at least 2 locations")
        payload = {
            "locations": self._payload_locations(locations),
            "costing": self.costing,
            "shape_format": self.shape_format,
            "shape_match": "map_snap",
        }
        max_locations = _max_locations_per_call()
        if len(locations) <= max_locations:
            requested_at = datetime.now(timezone.utc).isoformat()
            response = self._post("/route", payload, timeout_s=timeout_s)
            if response.status_code >= 400:
                raise RuntimeError(f"Valhalla /route failed ({response.status_code}): {response.text[:400]}")
            response_body = response.content or b""
            result = self._parse_route(response.json())
            result.diagnostics = {"engine": "route", "chunked": False}
            chunk_meta = {
                "endpoint_url": f"{self.base_url}/route",
                "http_method": "POST",
                "request_payload": payload,
                "requested_at": requested_at,
                "valhalla_version": self._valhalla_version(),
                "request_hash": _sha256_canonical(payload),
                "response_hash": hashlib.sha256(response_body).hexdigest(),
                "n_locations": len(locations),
            }
            result.request_meta = {
                "chunked": False,
                "n_locations": len(locations),
                "n_chunks": 1,
                "endpoint_url": chunk_meta["endpoint_url"],
                "valhalla_version": chunk_meta["valhalla_version"],
                "requested_at": chunk_meta["requested_at"],
                "max_locations_per_call": max_locations,
                "costing": self.costing,
                "shape_format": self.shape_format,
                "chunks": [chunk_meta],
                "request_hash": chunk_meta["request_hash"],
                "response_hash": chunk_meta["response_hash"],
            }
            return result

        stitched_coords: list[LonLat] = []
        stitched_legs: list[dict[str, Any]] = []
        stitched_maneuvers: list[dict[str, Any]] = []
        chunk_metas: list[dict[str, Any]] = []
        total_distance_m = 0.0
        total_duration_s = 0.0
        start = 0
        while start < len(locations) - 1:
            end = min(start + max_locations, len(locations))
            result = self.route(locations[start:end], timeout_s=timeout_s)
            if stitched_coords and result.coordinates and result.coordinates[0] == stitched_coords[-1]:
                result.coordinates = result.coordinates[1:]
            stitched_coords.extend(result.coordinates)
            stitched_legs.extend(result.legs)
            stitched_maneuvers.extend(result.maneuvers)
            total_distance_m += result.distance_m
            total_duration_s += result.duration_s
            for chunk_meta in (result.request_meta or {}).get("chunks", []):
                chunk_meta = dict(chunk_meta)
                chunk_meta["chunk_slice"] = [start, end]
                chunk_metas.append(chunk_meta)
            if end == len(locations):
                break
            start = end - 1

        first = chunk_metas[0] if chunk_metas else {}
        aggregate_meta = {
            "chunked": True,
            "n_locations": len(locations),
            "n_chunks": len(chunk_metas),
            "endpoint_url": first.get("endpoint_url", f"{self.base_url}/route"),
            "valhalla_version": first.get("valhalla_version", self._valhalla_version()),
            "requested_at": first.get("requested_at"),
            "max_locations_per_call": max_locations,
            "costing": self.costing,
            "shape_format": self.shape_format,
            "chunks": chunk_metas,
            "request_hash": _sha256_canonical([c.get("request_hash") for c in chunk_metas]),
            "response_hash": _sha256_canonical([c.get("response_hash") for c in chunk_metas]),
        }
        return ValhallaRouteResult(
            coordinates=stitched_coords,
            distance_m=total_distance_m,
            duration_s=total_duration_s,
            legs=stitched_legs,
            maneuvers=stitched_maneuvers,
            diagnostics={"engine": "route", "chunked": True},
            request_meta=aggregate_meta,
        )

    def try_optimized_route(self, locations: Sequence[LonLat]) -> ValhallaOptimizedOrder | None:
        if not self.supports("/optimized_route"):
            return None
        payload = {
            "locations": self._payload_locations(locations),
            "costing": self.costing,
            "shape_format": self.shape_format,
        }
        response = self._post("/optimized_route", payload, timeout_s=VALHALLA_HTTP_TIMEOUT_S)
        if response.status_code >= 400:
            return None
        data = response.json()
        trip_locations = ((data.get("trip") or {}).get("locations") or [])
        ordered_indices = [int(item.get("original_index", idx)) for idx, item in enumerate(trip_locations)]
        return ValhallaOptimizedOrder(
            ordered_indices=ordered_indices,
            diagnostics={
                "engine": "optimized_route",
                "raw_location_count": len(trip_locations),
                "available_actions": sorted(self.available_actions),
            },
        )

    def try_sources_to_targets_matrix(self, locations: Sequence[LonLat]) -> dict[str, Any] | None:
        if not self.supports("/sources_to_targets"):
            return None
        payload = {
            "sources": [{"lat": lat, "lon": lon} for lon, lat in locations],
            "targets": [{"lat": lat, "lon": lon} for lon, lat in locations],
            "costing": self.costing,
        }
        response = self._post("/sources_to_targets", payload, timeout_s=VALHALLA_HTTP_TIMEOUT_S)
        if response.status_code >= 400:
            return None
        return response.json()

    def locate(self, locations: Sequence[LonLat], *, timeout_s: int = VALHALLA_HTTP_TIMEOUT_S) -> list[dict[str, Any]]:
        if not self.supports("/locate"):
            return []
        payload = {
            "locations": [{"lat": lat, "lon": lon} for lon, lat in locations],
            "costing": self.costing,
        }
        response = self._post("/locate", payload, timeout_s=timeout_s)
        if response.status_code >= 400:
            raise RuntimeError(f"Valhalla /locate failed ({response.status_code}): {response.text[:400]}")
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError("Valhalla /locate returned unexpected payload")
        return data

    def pairwise_cost(
        self,
        source: tuple[str, LonLat],
        target: tuple[str, LonLat],
    ) -> PairwiseCost:
        cache_key = stable_hash([source[0], source[1], target[0], target[1], self.costing])
        cached = self._pair_cache.get(cache_key)
        if cached is not None:
            return cached
        result = self.route([source[1], target[1]], timeout_s=VALHALLA_PAIR_TIMEOUT_S)
        pair = PairwiseCost(
            distance_m=result.distance_m,
            duration_s=result.duration_s,
            geometry=result.coordinates,
            diagnostics={"engine": "route_pair"},
        )
        self._pair_cache[cache_key] = pair
        return pair

    def debug_snapshot(self) -> dict[str, Any]:
        self.refresh_capabilities()
        return {
            "base_url": self.base_url,
            "costing": self.costing,
            "shape_format": self.shape_format,
            "pair_cache_entries": len(self._pair_cache),
            "available_actions": sorted(self.available_actions),
            "capability_source": self.capability_source,
            "endpoint_support": dict(self.endpoint_support),
        }
