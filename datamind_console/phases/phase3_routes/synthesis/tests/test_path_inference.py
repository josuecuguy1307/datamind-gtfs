"""
Unit tests for datamind_console.phases.phase3_routes.synthesis.path_inference.

HTTP is stubbed via monkeypatch on ``requests.post``; the DB via an in-memory
fake cursor/conn.  No real Valhalla or Overpass call is made.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
import requests

from datamind_console.phases.phase3_routes.synthesis import path_inference as pi


# ---------------------------------------------------------------------------
# fake psycopg2-ish conn/cursor
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, store: "_Store"):
        self._store = store
        self._result: list = []
        self.rowcount = 0
        self.last_sql: str | None = None
        self.last_params: tuple | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None):
        params = tuple(params or ())
        self.last_sql = sql
        self.last_params = params
        up = " ".join(sql.split()).upper()

        # SELECT encoded_polyline FROM route_raw.osm_relations WHERE osm_relation_id = %s
        if up.startswith("SELECT ENCODED_POLYLINE FROM ROUTE_RAW.OSM_RELATIONS"):
            osm_id = params[0]
            poly = self._store.osm_relations.get(osm_id)
            self._result = [(poly,)] if poly is not None else []
            self.rowcount = len(self._result)
            return

        # SELECT inferred_path_polyline, ... FROM route_prod.routes WHERE route_id = %s
        if up.startswith("SELECT INFERRED_PATH_POLYLINE"):
            route_id = params[0]
            row = self._store.routes.get(route_id)
            if row is None:
                self._result = []
            else:
                self._result = [
                    (row.get("polyline"), row.get("source"), row.get("computed_at"))
                ]
            self.rowcount = len(self._result)
            return

        # UPDATE route_prod.routes SET ... WHERE route_id = %s
        if up.startswith("UPDATE ROUTE_PROD.ROUTES SET"):
            # invalidate_path binds only the route_id (NULLs are literals)
            if len(params) == 1:
                (route_id,) = params
                self._store.routes[route_id] = {
                    "polyline": None,
                    "source": None,
                    "computed_at": None,
                }
                self._store.update_calls.append(
                    {
                        "route_id": route_id,
                        "polyline": None,
                        "source": None,
                        "computed_at": None,
                    }
                )
                self.rowcount = 1
                return
            # cache_path binds (polyline, source, computed_at, route_id)
            poly, source, computed_at, route_id = params
            self._store.routes[route_id] = {
                "polyline": poly,
                "source": source,
                "computed_at": computed_at,
            }
            self._store.update_calls.append(
                {"route_id": route_id, "polyline": poly, "source": source, "computed_at": computed_at}
            )
            self.rowcount = 1
            return

        raise AssertionError(f"fake cursor got unexpected SQL: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, store: "_Store"):
        self._store = store

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self._store)


@dataclass
class _Store:
    osm_relations: dict
    routes: dict
    update_calls: list


@pytest.fixture()
def store():
    return _Store(osm_relations={}, routes={}, update_calls=[])


@pytest.fixture()
def conn(store):
    return _FakeConn(store)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_polyline(coords):
    """Encode a list of (lat, lon) at precision 6."""
    return pi.encode_polyline(coords)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


# ---------------------------------------------------------------------------
# 1. compute_route_path with osm_relation_id uses relation geometry
# ---------------------------------------------------------------------------


def test_compute_route_path_uses_osm_relation_when_provided(
    store, conn, monkeypatch
):
    osm_poly = _make_polyline([(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)])
    store.osm_relations[12345] = osm_poly

    # Valhalla must NOT be called
    def _boom(*a, **kw):
        raise AssertionError("Valhalla must not be called when OSM relation is given")

    monkeypatch.setattr(requests, "post", _boom)

    rp = pi.compute_route_path(
        route_id="route-a",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        osm_relation_id=12345,
        conn=conn,
    )

    assert isinstance(rp, pi.RoutePath)
    assert rp.polyline == osm_poly
    assert rp.source == pi.PATH_SOURCE_OSM

    # cache_path was called to persist the result
    assert len(store.update_calls) == 1
    assert store.update_calls[0]["source"] == pi.PATH_SOURCE_OSM
    assert store.update_calls[0]["polyline"] == osm_poly


# ---------------------------------------------------------------------------
# 2. compute_route_path without osm_relation_id calls Valhalla
# ---------------------------------------------------------------------------


def test_compute_route_path_calls_valhalla_without_osm_relation(
    store, conn, monkeypatch
):
    expected_shape = _make_polyline(
        [(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)]
    )
    captured = {}

    def _fake_post(url, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(
            status_code=200,
            json_data={"trip": {"legs": [{"shape": expected_shape}]}},
        )

    monkeypatch.setattr(requests, "post", _fake_post)

    rp = pi.compute_route_path(
        route_id="route-b",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        conn=conn,
    )
    assert rp.polyline == expected_shape
    assert rp.source == pi.PATH_SOURCE_VALHALLA_TERMINUS
    assert captured["url"].endswith("/route")
    assert captured["json"]["costing"] == "bus"
    assert captured["json"]["shape_format"] == "polyline6"
    assert captured["timeout"] == pi.DEFAULT_TIMEOUT_S
    assert len(captured["json"]["locations"]) == 2


def test_compute_route_path_with_anchors_bumps_source(store, conn, monkeypatch):
    expected_shape = _make_polyline(
        [(-0.180, -78.470), (-0.1805, -78.4705), (-0.181, -78.471)]
    )

    def _fake_post(url, json=None, timeout=None, **kw):
        # 2 termini + 1 grounded + 1 must_pass = 4 waypoints
        assert len(json["locations"]) == 4
        return _FakeResponse(
            status_code=200,
            json_data={"trip": {"legs": [{"shape": expected_shape}]}},
        )

    monkeypatch.setattr(requests, "post", _fake_post)

    rp = pi.compute_route_path(
        route_id="route-anchored",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        grounded_stops=[(-0.1805, -78.4705)],
        must_pass_through=[(-0.181, -78.471)],
        conn=conn,
    )
    assert rp.source == pi.PATH_SOURCE_VALHALLA_ANCHORS


# ---------------------------------------------------------------------------
# 3. Cache hit returns cached value without calling Valhalla
# ---------------------------------------------------------------------------


def test_cache_hit_skips_valhalla(store, conn, monkeypatch):
    cached_poly = _make_polyline(
        [(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)]
    )
    now = datetime.now(timezone.utc)
    store.routes["route-cached"] = {
        "polyline": cached_poly,
        "source": pi.PATH_SOURCE_VALHALLA_TERMINUS,
        "computed_at": now - timedelta(days=1),  # fresh
    }

    def _boom(*a, **kw):
        raise AssertionError("Valhalla must not be called on cache hit")

    monkeypatch.setattr(requests, "post", _boom)

    rp = pi.compute_route_path(
        route_id="route-cached",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        conn=conn,
    )
    assert rp.polyline == cached_poly
    assert rp.source == pi.PATH_SOURCE_VALHALLA_TERMINUS
    # no UPDATE issued on cache hit
    assert store.update_calls == []


def test_stale_cache_is_ignored(store, conn, monkeypatch):
    stale_poly = _make_polyline(
        [(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)]
    )
    store.routes["route-stale"] = {
        "polyline": stale_poly,
        "source": pi.PATH_SOURCE_VALHALLA_TERMINUS,
        "computed_at": datetime.now(timezone.utc) - timedelta(days=30),
    }

    fresh_shape = _make_polyline(
        [(-0.180, -78.470), (-0.1815, -78.4715), (-0.182, -78.472)]
    )

    def _fake_post(url, json=None, timeout=None, **kw):
        return _FakeResponse(
            status_code=200,
            json_data={"trip": {"legs": [{"shape": fresh_shape}]}},
        )

    monkeypatch.setattr(requests, "post", _fake_post)

    rp = pi.compute_route_path(
        route_id="route-stale",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        conn=conn,
    )
    assert rp.polyline == fresh_shape
    assert rp.polyline != stale_poly
    assert len(store.update_calls) == 1  # refreshed


# ---------------------------------------------------------------------------
# 4. Cache miss triggers Valhalla call
# ---------------------------------------------------------------------------


def test_cache_miss_triggers_valhalla(store, conn, monkeypatch):
    shape = _make_polyline(
        [(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)]
    )
    calls = {"n": 0}

    def _fake_post(url, json=None, timeout=None, **kw):
        calls["n"] += 1
        return _FakeResponse(
            status_code=200,
            json_data={"trip": {"legs": [{"shape": shape}]}},
        )

    monkeypatch.setattr(requests, "post", _fake_post)

    # empty store: cache miss
    rp = pi.compute_route_path(
        route_id="route-new",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        conn=conn,
    )
    assert calls["n"] == 1
    assert rp.polyline == shape

    # subsequent call should hit cache and NOT call Valhalla
    rp2 = pi.compute_route_path(
        route_id="route-new",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        conn=conn,
    )
    assert calls["n"] == 1  # unchanged
    assert rp2.polyline == shape


def test_force_recompute_bypasses_cache(store, conn, monkeypatch):
    cached_poly = _make_polyline(
        [(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)]
    )
    store.routes["route-force"] = {
        "polyline": cached_poly,
        "source": pi.PATH_SOURCE_VALHALLA_TERMINUS,
        "computed_at": datetime.now(timezone.utc),
    }
    fresh = _make_polyline([(-0.180, -78.470), (-0.1815, -78.4715), (-0.182, -78.472)])

    def _fake_post(url, json=None, timeout=None, **kw):
        return _FakeResponse(
            status_code=200,
            json_data={"trip": {"legs": [{"shape": fresh}]}},
        )

    monkeypatch.setattr(requests, "post", _fake_post)

    rp = pi.compute_route_path(
        route_id="route-force",
        termini=[(-0.180, -78.470), (-0.182, -78.472)],
        conn=conn,
        force_recompute=True,
    )
    assert rp.polyline == fresh


# ---------------------------------------------------------------------------
# 5. invalidate_path sets columns to NULL
# ---------------------------------------------------------------------------


def test_invalidate_path_nulls_three_columns(store, conn):
    store.routes["route-x"] = {
        "polyline": "abcd",
        "source": pi.PATH_SOURCE_VALHALLA_TERMINUS,
        "computed_at": datetime.now(timezone.utc),
    }
    pi.invalidate_path("route-x", conn=conn)
    call = store.update_calls[-1]
    assert call["polyline"] is None
    assert call["source"] is None
    assert call["computed_at"] is None


def test_cache_path_rejects_unknown_source(store, conn):
    with pytest.raises(ValueError, match="invalid inferred_path_source"):
        pi.cache_path("route-z", "abc", "not_a_real_source", conn=conn)


# ---------------------------------------------------------------------------
# 6. project_point_to_polyline returns correct distance for known fixture
# ---------------------------------------------------------------------------


def test_project_point_to_polyline_on_path_returns_near_zero():
    # straight E-W line along latitude -0.180
    coords = [(-0.180, -78.470), (-0.180, -78.460)]
    poly = _make_polyline(coords)

    # point ON the line, mid-segment
    proj = pi.project_point_to_polyline(-0.180, -78.465, poly)
    assert proj.distance_m < 1.0
    assert pytest.approx(proj.lat, abs=1e-5) == -0.180


def test_project_point_to_polyline_off_path_has_expected_distance():
    # straight line along longitude -78.470, south to north
    coords = [(-0.181, -78.470), (-0.179, -78.470)]
    poly = _make_polyline(coords)

    # offset ~22m east of the line at mid-point.
    # 1 deg lon at lat -0.180 ≈ 111_319m, so 0.0002 deg ≈ 22.3 m
    proj = pi.project_point_to_polyline(-0.180, -78.4698, poly)
    assert 15 < proj.distance_m < 35
    # projection lat is near the midpoint
    assert pytest.approx(proj.lat, abs=1e-3) == -0.180


# ---------------------------------------------------------------------------
# 7. polyline_segment_by_road_name returns None for road not in path
# ---------------------------------------------------------------------------


def test_polyline_segment_returns_none_when_no_overpass_ways():
    poly = _make_polyline([(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)])

    def _fetcher(name, bbox):
        return []

    out = pi.polyline_segment_by_road_name(
        poly, "Calle Inexistente", "route-x", overpass_fetcher=_fetcher
    )
    assert out is None


def test_polyline_segment_returns_none_when_ways_far_from_path():
    poly = _make_polyline([(-0.180, -78.470), (-0.181, -78.471), (-0.182, -78.472)])

    def _fetcher(name, bbox):
        # a way 10km away, nothing near our polyline
        return [[(-0.500, -78.700), (-0.501, -78.701)]]

    out = pi.polyline_segment_by_road_name(
        poly, "Avenida Lejana", "route-x", overpass_fetcher=_fetcher
    )
    assert out is None


def test_polyline_segment_returns_sub_path_when_road_overlaps():
    # Long polyline — we want a named-road subsegment match
    coords = [
        (-0.180, -78.470),
        (-0.181, -78.470),
        (-0.182, -78.470),
        (-0.183, -78.470),
        (-0.184, -78.470),
    ]
    poly = _make_polyline(coords)

    def _fetcher(name, bbox):
        # fake "way" coincident with middle three vertices
        return [[(-0.181, -78.470), (-0.182, -78.470), (-0.183, -78.470)]]

    out = pi.polyline_segment_by_road_name(
        poly, "Av Test", "route-x", overpass_fetcher=_fetcher
    )
    assert out is not None
    assert len(out) >= 2


# ---------------------------------------------------------------------------
# 8. Valhalla timeout raises PathInferenceError
# ---------------------------------------------------------------------------


def test_valhalla_timeout_raises_path_inference_error(store, conn, monkeypatch):
    def _timeout(*a, **kw):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "post", _timeout)

    with pytest.raises(pi.PathInferenceError, match="timeout"):
        pi.compute_route_path(
            route_id="route-timeout",
            termini=[(-0.180, -78.470), (-0.182, -78.472)],
            conn=conn,
        )


def test_valhalla_http_error_raises_path_inference_error(store, conn, monkeypatch):
    def _post(*a, **kw):
        return _FakeResponse(status_code=502, text="bad gateway")

    monkeypatch.setattr(requests, "post", _post)

    with pytest.raises(pi.PathInferenceError, match="HTTP 502"):
        pi.compute_route_path(
            route_id="route-502",
            termini=[(-0.180, -78.470), (-0.182, -78.472)],
            conn=conn,
        )


def test_valhalla_malformed_response_raises(store, conn, monkeypatch):
    def _post(*a, **kw):
        return _FakeResponse(status_code=200, json_data={"unexpected": 1})

    monkeypatch.setattr(requests, "post", _post)

    with pytest.raises(pi.PathInferenceError, match="missing trip.legs"):
        pi.compute_route_path(
            route_id="route-bad",
            termini=[(-0.180, -78.470), (-0.182, -78.472)],
            conn=conn,
        )


# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------


def test_termini_must_have_two_or_more_waypoints(conn):
    with pytest.raises(ValueError, match="at least 2 waypoints"):
        pi.compute_route_path(
            route_id="route-short",
            termini=[(-0.180, -78.470)],
            conn=conn,
        )


# ---------------------------------------------------------------------------
# Polyline codec round-trip
# ---------------------------------------------------------------------------


def test_polyline_codec_round_trip():
    coords = [(-0.180, -78.470), (-0.1815, -78.4715), (-0.182, -78.472)]
    enc = pi.encode_polyline(coords)
    dec = pi.decode_polyline(enc)
    assert len(dec) == len(coords)
    for (a_lat, a_lon), (b_lat, b_lon) in zip(coords, dec):
        assert abs(a_lat - b_lat) < 1e-5
        assert abs(a_lon - b_lon) < 1e-5


def test_haversine_known_distance():
    # ~1 degree lat = ~111,195 m at equator
    d = pi.haversine_m((0.0, 0.0), (1.0, 0.0))
    assert 110_000 < d < 112_000
