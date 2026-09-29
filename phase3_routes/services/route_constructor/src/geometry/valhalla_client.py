from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import hashlib
import json
import os
import requests

from phase3_routes.services.route_constructor.src.settings import (
    VALHALLA_URL,
    VALHALLA_COSTING,
    VALHALLA_SHAPE_FORMAT,
)
from phase3_routes.services.route_constructor.src.utils.polyline import decode_polyline6


LonLat = tuple[float, float]  # (lon, lat)

_VALHALLA_VERSION_CACHE: str | None = None


def _valhalla_version() -> str:
    """Best-effort fetch of the Valhalla build version from /status.

    Cached for the lifetime of the process. Returns 'unknown' on any error so
    metadata capture never fails the trace.
    """
    global _VALHALLA_VERSION_CACHE
    if _VALHALLA_VERSION_CACHE is not None:
        return _VALHALLA_VERSION_CACHE
    try:
        url = f"{VALHALLA_URL.rstrip('/')}/status"
        r = requests.get(url, timeout=5)
        if r.status_code < 400:
            data = r.json() or {}
            _VALHALLA_VERSION_CACHE = str(
                data.get("version") or data.get("valhalla_version") or "unknown"
            )
        else:
            _VALHALLA_VERSION_CACHE = "unknown"
    except Exception:
        _VALHALLA_VERSION_CACHE = "unknown"
    return _VALHALLA_VERSION_CACHE


def _sha256_canonical(obj: Any) -> str:
    """SHA-256 of a canonicalised JSON encoding (sorted keys, no whitespace)."""
    data = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _max_locations_per_call() -> int:
    raw = (os.getenv("VALHALLA_MAX_LOCATIONS_PER_CALL") or "").strip()
    if not raw:
        return 20
    try:
        n = int(raw)
        return max(2, n)
    except Exception:
        return 20


def _build_payload(locations: list[LonLat], costing_options: dict | None) -> dict[str, Any]:
    return {
        "locations": [
            {
                "lat": lat,
                "lon": lon,
                # NOTE:
                # Some Valhalla builds fail with:
                # "leg_shape_index not set for intermediate location"
                # when intermediate locations use type="through".
                # Using break for all locations is stable across these builds.
                "type": "break",
            }
            for i, (lon, lat) in enumerate(locations)
        ],
        "costing": VALHALLA_COSTING,
        "shape_format": VALHALLA_SHAPE_FORMAT,
        "costing_options": costing_options or {},
        "shape_match": "map_snap",
    }


def _decode_route_response(data: dict[str, Any]) -> list[LonLat]:
    legs = (data.get("trip") or {}).get("legs") or []
    if not legs:
        raise ValueError("Valhalla response missing trip.legs")

    out: list[LonLat] = []

    for leg in legs:
        shape = leg.get("shape")
        if shape is None:
            raise ValueError("Valhalla leg missing shape")

        # Case A: polyline6 string -> decode usually returns (lat,lon)
        if isinstance(shape, str):
            latlon = decode_polyline6(shape)  # assumed [(lat,lon), ...]
            pts = [(float(lon), float(lat)) for (lat, lon) in latlon]

        # Case B: geojson list -> usually [[lon,lat], ...]
        elif isinstance(shape, list):
            pts = [(float(pt[0]), float(pt[1])) for pt in shape]

        else:
            raise ValueError(f"Unknown shape type from Valhalla: {type(shape)}")

        # concat legs, avoid duplicating the first point of each next leg
        if out and pts and pts[0] == out[-1]:
            pts = pts[1:]
        out.extend(pts)

    if len(out) < 2:
        raise ValueError("Valhalla returned <2 shape points")
    return out


def _route_once(
    locations: list[LonLat],
    costing_options: dict | None = None,
    timeout_s: int = 60,
) -> list[LonLat]:
    coords, _meta = _route_once_with_meta(
        locations, costing_options=costing_options, timeout_s=timeout_s
    )
    return coords


def _route_once_with_meta(
    locations: list[LonLat],
    costing_options: dict | None = None,
    timeout_s: int = 60,
) -> tuple[list[LonLat], dict]:
    """Single (non-chunked) Valhalla trace + full request/response metadata.

    Returned metadata schema (stable):
      endpoint_url, http_method, request_payload, requested_at, valhalla_version,
      request_hash, response_hash, n_locations
    Never raises for metadata reasons — only for genuine trace failures.
    """
    payload = _build_payload(locations, costing_options)
    url = f"{VALHALLA_URL.rstrip('/')}/route"
    requested_at = datetime.now(timezone.utc).isoformat()

    r = requests.post(url, json=payload, timeout=timeout_s)
    response_body: bytes = r.content or b""
    http_method = "POST"
    if r.status_code >= 400:
        post_detail = (r.text or "").strip()
        # Keep legacy GET fallback only for smaller payloads / compatibility cases.
        if len(locations) <= _max_locations_per_call():
            retry = requests.get(url, params={"json": json.dumps(payload)}, timeout=timeout_s)
            if retry.status_code < 400:
                r = retry
                response_body = retry.content or b""
                http_method = "GET"
            else:
                detail = (retry.text or "").strip()
                raise RuntimeError(
                    f"Valhalla POST /route failed ({r.status_code}): {post_detail[:300]} | "
                    f"GET fallback failed ({retry.status_code}): {detail[:300]}"
                )
        else:
            raise RuntimeError(
                f"Valhalla POST /route failed ({r.status_code}): {post_detail[:500]}"
            )

    coords = _decode_route_response(r.json())
    meta = {
        "endpoint_url": url,
        "http_method": http_method,
        "request_payload": payload,
        "requested_at": requested_at,
        "valhalla_version": _valhalla_version(),
        "request_hash": _sha256_canonical(payload),
        "response_hash": hashlib.sha256(response_body).hexdigest(),
        "n_locations": len(locations),
    }
    return coords, meta


def valhalla_route(
    locations: list[LonLat],
    costing_options: dict | None = None,
    timeout_s: int = 60,
) -> list[LonLat]:
    """
    locations: list of (lon, lat) points (GeoJSON order).
    returns:   list of (lon, lat) shape points.

    Handles:
      - shape_format=polyline6 -> shape is encoded str
      - shape_format=geojson   -> shape is list of [lon,lat]
      - multiple legs -> concatenates legs (dedup boundary points)
    """
    coords, _meta = valhalla_route_with_meta(
        locations, costing_options=costing_options, timeout_s=timeout_s
    )
    return coords


def valhalla_route_with_meta(
    locations: list[LonLat],
    costing_options: dict | None = None,
    timeout_s: int = 60,
) -> tuple[list[LonLat], dict]:
    """Same as ``valhalla_route`` but also returns a metadata dict.

    Metadata schema:
      - chunks: list of per-/route-call metadata dicts (1 entry for short
        traces, N entries for long stitched traces)
      - chunked: bool
      - n_locations, n_chunks
      - endpoint_url, valhalla_version, requested_at (first chunk's values)
      - aggregate request_hash and response_hash computed over the chunk list
        so a re-trace with identical input is provably identical

    This is the single source of truth for Valhalla request metadata
    captured into ``route_work.geometry_candidates.valhalla_request``.
    """
    if len(locations) < 2:
        raise ValueError("Need at least 2 locations")

    max_locs = _max_locations_per_call()
    if len(locations) <= max_locs:
        coords, chunk_meta = _route_once_with_meta(
            locations, costing_options=costing_options, timeout_s=timeout_s
        )
        meta = {
            "chunked": False,
            "n_locations": len(locations),
            "n_chunks": 1,
            "endpoint_url": chunk_meta["endpoint_url"],
            "valhalla_version": chunk_meta["valhalla_version"],
            "requested_at": chunk_meta["requested_at"],
            "max_locations_per_call": max_locs,
            "costing": VALHALLA_COSTING,
            "shape_format": VALHALLA_SHAPE_FORMAT,
            "costing_options": costing_options or {},
            "chunks": [chunk_meta],
            "request_hash": chunk_meta["request_hash"],
            "response_hash": chunk_meta["response_hash"],
        }
        return coords, meta

    stitched: list[LonLat] = []
    chunks_meta: list[dict] = []
    start = 0
    n = len(locations)
    while start < n - 1:
        end = min(start + max_locs, n)
        chunk = locations[start:end]
        part, chunk_meta = _route_once_with_meta(
            chunk, costing_options=costing_options, timeout_s=timeout_s
        )
        chunk_meta["chunk_slice"] = [start, end]
        chunks_meta.append(chunk_meta)
        if stitched and part and stitched[-1] == part[0]:
            part = part[1:]
        stitched.extend(part)
        if end == n:
            break
        start = end - 1

    if len(stitched) < 2:
        raise ValueError("Valhalla stitched shape has <2 points")

    first = chunks_meta[0]
    meta = {
        "chunked": True,
        "n_locations": len(locations),
        "n_chunks": len(chunks_meta),
        "endpoint_url": first["endpoint_url"],
        "valhalla_version": first["valhalla_version"],
        "requested_at": first["requested_at"],
        "max_locations_per_call": max_locs,
        "costing": VALHALLA_COSTING,
        "shape_format": VALHALLA_SHAPE_FORMAT,
        "costing_options": costing_options or {},
        "chunks": chunks_meta,
        "request_hash": _sha256_canonical([c["request_hash"] for c in chunks_meta]),
        "response_hash": _sha256_canonical([c["response_hash"] for c in chunks_meta]),
    }
    return stitched, meta
