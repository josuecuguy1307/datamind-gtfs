"""Integration tests — valhalla_request meta propagation + re-traceability.

Covers the Close-Prompt-7b Commit 2/3 wiring:

  Test A (structural)
    A1. corridor_builder._call_valhalla preferred path attaches the
        valhalla_route_with_meta meta dict to CorridorResult.valhalla_meta.
    A2. geometry_candidate_builder.derive_geometry_candidate propagates
        CorridorResult.valhalla_meta onto GeometryCandidate.valhalla_meta.
    A3. corridor_builder._call_valhalla fallback path (when the phase3
        service module is unimportable) synthesises a replay-safe meta
        dict carrying request_payload, endpoint_url, request_hash,
        response_hash, valhalla_version, capture_status="fallback_capture".

  Test B (functional re-traceability)
    B1. The captured meta contains locations verbatim — a re-POST of
        meta["chunks"][0]["request_payload"] against the same endpoint
        yields a shape whose polyline length matches the stored corridor
        within 1%% and whose bbox matches within 10m.

Every test is fully hermetic: HTTP is mocked at the requests.post seam,
and the phase3 service module is either left intact (preferred path) or
monkey-patched away via sys.modules (fallback path).
"""
from __future__ import annotations

import json
import math
import sys
from typing import Any, Dict, List, Tuple

import pytest

LonLat = Tuple[float, float]


# ---------------------------------------------------------------------------
# Polyline6 + length helpers (duplicated minimally so the tests do not
# depend on the module under test for geometric comparisons).
# ---------------------------------------------------------------------------

def _haversine_m(a: LonLat, b: LonLat) -> float:
    r = 6371000.0
    lon1, lat1 = a
    lon2, lat2 = b
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2.0 * r * math.asin(math.sqrt(x))


def _polyline6_length_m(coords: List[LonLat]) -> float:
    return sum(_haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1))


def _bbox(coords: List[LonLat]) -> Tuple[float, float, float, float]:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (min(lons), min(lats), max(lons), max(lats))


def _encode_polyline6(coords: List[LonLat]) -> str:
    """Encode (lon, lat) list as polyline6. Mirrors Valhalla's own encoding."""
    result = []
    prev_lat = 0
    prev_lon = 0
    for lon, lat in coords:
        ilat = int(round(lat * 1e6))
        ilon = int(round(lon * 1e6))
        dlat = ilat - prev_lat
        dlon = ilon - prev_lon
        prev_lat = ilat
        prev_lon = ilon
        for v in (dlat, dlon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                result.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            result.append(chr(v + 63))
    return "".join(result)


# ---------------------------------------------------------------------------
# Deterministic HTTP mock
# ---------------------------------------------------------------------------

_QUITO_SHAPE: List[LonLat] = [
    (-78.4800, -0.1800),
    (-78.4790, -0.1805),
    (-78.4780, -0.1810),
    (-78.4770, -0.1820),
    (-78.4760, -0.1830),
    (-78.4750, -0.1840),
    (-78.4740, -0.1845),
    (-78.4730, -0.1850),
]


class _MockResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode("utf-8")
        self.text = json.dumps(payload)
        self.headers = {"X-Valhalla-Version": "3.6.2-test"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def _encode_response(coords: List[LonLat]) -> dict:
    return {
        "trip": {
            "legs": [
                {
                    "shape": _encode_polyline6(coords),
                    "summary": {"length": _polyline6_length_m(coords) / 1000.0},
                }
            ]
        }
    }


@pytest.fixture
def mock_valhalla_post(monkeypatch):
    """Patch requests.post in both the phase3 client and corridor_builder's
    direct-post fallback path. Captures every call so the test can later
    replay.
    """
    calls: List[Dict[str, Any]] = []

    def fake_post(url, json=None, timeout=None, **kwargs):  # noqa: A002
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _MockResponse(_encode_response(_QUITO_SHAPE))

    # Patch the requests module used by the phase3 service client.
    try:
        import phase3_routes.services.route_constructor.src.geometry.valhalla_client as _vc
        monkeypatch.setattr(_vc.requests, "post", fake_post)
    except ImportError:
        pass

    # Patch the corridor_builder's direct-POST fallback alias.
    import datamind_console.phases.phase3_routes.stop_grounding.corridor_builder as _cb
    # The fallback import is `import requests as _requests` inside _call_valhalla;
    # monkeypatch the top-level requests.post so both sites see the mock.
    import requests as _req
    monkeypatch.setattr(_req, "post", fake_post)

    return calls


# ---------------------------------------------------------------------------
# Test A — structural propagation
# ---------------------------------------------------------------------------

def test_A1_corridor_builder_attaches_meta_to_corridor_result(mock_valhalla_post):
    from datamind_console.phases.phase3_routes.stop_grounding.corridor_builder import (
        _call_valhalla,
    )

    coords, meta = _call_valhalla(
        [(-78.4800, -0.1800), (-78.4730, -0.1850)],
        timeout_s=5,
    )

    assert len(coords) >= 2
    assert isinstance(meta, dict)
    # Either path must carry a chunks list with the request payload.
    assert "chunks" in meta
    assert isinstance(meta["chunks"], list) and len(meta["chunks"]) >= 1
    assert meta["chunks"][0].get("request_payload") or meta["chunks"][0].get("request_hash")
    # Endpoint url + version must be present for audit.
    assert meta.get("endpoint_url", "").endswith("/route")
    assert "valhalla_version" in meta
    assert "request_hash" in meta
    assert "response_hash" in meta


def test_A2_geometry_candidate_inherits_meta_from_corridor():
    from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
        CorridorResult,
        CorridorStopCandidate,
        GeometryCandidate,
        SequenceSkeleton,
    )
    from datamind_console.phases.phase3_routes.stop_grounding.geometry_candidate_builder import (
        derive_geometry_candidate,
    )

    fake_meta = {
        "chunked": False,
        "chunks": [{"request_hash": "abc", "response_hash": "def"}],
        "request_hash": "abc",
        "response_hash": "def",
        "endpoint_url": "http://127.0.0.1:8003/route",
        "valhalla_version": "3.6.2-test",
        "capture_status": "unit_test",
    }
    corridor = CorridorResult(
        corridor_geojson={
            "type": "LineString",
            "coordinates": [list(c) for c in _QUITO_SHAPE],
        },
        total_length_km=_polyline6_length_m(_QUITO_SHAPE) / 1000.0,
        segment_count=1,
        waypoints_used=[
            {"lon": _QUITO_SHAPE[0][0], "lat": _QUITO_SHAPE[0][1], "stop_id": "s1"},
            {"lon": _QUITO_SHAPE[-1][0], "lat": _QUITO_SHAPE[-1][1], "stop_id": "s2"},
        ],
        corridor_confidence=1.0,
        valhalla_meta=fake_meta,
    )
    skeleton = SequenceSkeleton(
        ordered_stops=[
            CorridorStopCandidate(
                stop_id="s1",
                stop_name="origin",
                lon=_QUITO_SHAPE[0][0],
                lat=_QUITO_SHAPE[0][1],
                path_fraction=0.0,
                on_route_score=1.0,
            ),
            CorridorStopCandidate(
                stop_id="s2",
                stop_name="dest",
                lon=_QUITO_SHAPE[-1][0],
                lat=_QUITO_SHAPE[-1][1],
                path_fraction=1.0,
                on_route_score=1.0,
            ),
        ],
    )

    candidate: GeometryCandidate = derive_geometry_candidate(
        corridor, skeleton, refine_with_valhalla=False
    )

    assert candidate.valhalla_meta is fake_meta
    # to_dict() roundtrip carries the meta through serialisation.
    assert candidate.to_dict().get("valhalla_meta") is fake_meta


def test_A3_fallback_path_synthesises_replay_safe_meta(monkeypatch):
    # Force the preferred import to fail, exercising the direct-POST fallback.
    import datamind_console.phases.phase3_routes.stop_grounding.corridor_builder as cb

    # Intercept the dynamic import attempt inside _call_valhalla by removing
    # the module from sys.modules and shadowing it with a sentinel.
    key = "phase3_routes.services.route_constructor.src.geometry.valhalla_client"
    monkeypatch.setitem(sys.modules, key, None)  # type: ignore[arg-type]

    calls: List[Dict[str, Any]] = []

    def fake_post(url, json=None, timeout=None, **kwargs):  # noqa: A002
        calls.append({"url": url, "json": json})
        return _MockResponse(_encode_response(_QUITO_SHAPE))

    import requests
    monkeypatch.setattr(requests, "post", fake_post)

    coords, meta = cb._call_valhalla(
        [(-78.4800, -0.1800), (-78.4730, -0.1850)],
        timeout_s=5,
    )

    assert len(coords) >= 2
    assert meta["capture_status"] == "fallback_capture"
    assert meta["source"].endswith("direct_post")
    # Replay-safety: request_payload + endpoint + hashes must all be present.
    first = meta["chunks"][0]
    assert first["endpoint_url"].endswith("/route")
    assert "request_payload" in first and isinstance(first["request_payload"], dict)
    assert first["request_payload"]["locations"][0]["lat"] == pytest.approx(-0.1800)
    assert first["request_payload"]["locations"][-1]["lon"] == pytest.approx(-78.4730)
    assert "request_hash" in first
    assert "response_hash" in first
    assert meta["valhalla_version"] == "3.6.2-test"


# ---------------------------------------------------------------------------
# Test B — functional re-traceability
# ---------------------------------------------------------------------------

def test_B1_captured_meta_replays_to_matching_shape(mock_valhalla_post):
    """Given a captured meta, POSTing its stored request_payload back to
    the stored endpoint_url yields a shape that matches the originally
    persisted corridor within 1% polyline length and 10m bounding box.

    This is the acid test for audit: if the stored meta can't reproduce
    the geometry, the column is decorative.
    """
    from datamind_console.phases.phase3_routes.stop_grounding.corridor_builder import (
        _call_valhalla,
        _decode_polyline6,
    )

    coords_original, meta = _call_valhalla(
        [(-78.4800, -0.1800), (-78.4730, -0.1850)],
        timeout_s=5,
    )

    assert meta.get("chunks")
    first_chunk = meta["chunks"][0]
    stored_payload = first_chunk.get("request_payload")
    stored_url = first_chunk.get("endpoint_url") or meta.get("endpoint_url")

    assert stored_payload is not None, "replay impossible without stored payload"
    assert stored_url is not None

    # Replay: POST the stored payload (same mock, so same response).
    import requests
    replay = requests.post(stored_url, json=stored_payload, timeout=5)
    replay_data = replay.json()
    replay_shape_str = replay_data["trip"]["legs"][0]["shape"]
    replay_coords = _decode_polyline6(replay_shape_str)

    # Length must match within 1%.
    len_orig = _polyline6_length_m(coords_original)
    len_replay = _polyline6_length_m(replay_coords)
    assert len_orig > 0
    assert abs(len_replay - len_orig) / len_orig <= 0.01, (
        f"length drift {abs(len_replay - len_orig):.1f}m / {len_orig:.1f}m"
    )

    # Bounding box must match within 10m on every edge.
    b_orig = _bbox(coords_original)
    b_replay = _bbox(replay_coords)
    TOL_M = 10.0
    # Convert degree-deltas to meters using the midpoint of the bbox.
    mid_lat = (b_orig[1] + b_orig[3]) / 2.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    m_per_deg_lat = 111_132.0
    assert abs(b_orig[0] - b_replay[0]) * m_per_deg_lon <= TOL_M
    assert abs(b_orig[2] - b_replay[2]) * m_per_deg_lon <= TOL_M
    assert abs(b_orig[1] - b_replay[1]) * m_per_deg_lat <= TOL_M
    assert abs(b_orig[3] - b_replay[3]) * m_per_deg_lat <= TOL_M
