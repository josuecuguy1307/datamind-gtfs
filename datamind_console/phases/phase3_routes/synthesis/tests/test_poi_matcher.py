"""
Unit tests for datamind_console.phases.phase3_routes.synthesis.poi_matcher.

Overpass HTTP is stubbed via an injected ``http_post`` callable; no live
network.  Coverage: name scoring + accent normalisation, Overpass
parsing, in-process cache (hit/miss/TTL), tag-filter composition, access
point decision, error handling.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

import pytest
import requests

from datamind_console.phases.phase3_routes.synthesis import (
    path_inference as pi,
    poi_matcher as pm,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _overpass_poi(
    osm_id: int,
    osm_type: str = "node",
    name: str = "Mercado Central de Sangolquí",
    lat: float = -0.180,
    lon: float = -78.470,
    amenity: str = "marketplace",
    extra_tags: Optional[dict] = None,
) -> dict:
    tags = {"name": name, "amenity": amenity}
    if extra_tags:
        tags.update(extra_tags)
    el: dict = {"id": osm_id, "type": osm_type, "tags": tags}
    if osm_type == "node":
        el["lat"] = lat
        el["lon"] = lon
    else:
        el["center"] = {"lat": lat, "lon": lon}
    return el


@pytest.fixture(autouse=True)
def _clear_cache():
    pm.clear_overpass_cache()
    yield
    pm.clear_overpass_cache()


# ---------------------------------------------------------------------------
# 1. score_poi_match — exact + partial + accent + empty
# ---------------------------------------------------------------------------


def test_score_poi_match_exact_is_one():
    assert pm.score_poi_match("Mercado Central", "Mercado Central") == 1.0


def test_score_poi_match_accent_insensitive():
    # Same text modulo diacritics → must score > 0.95
    s = pm.score_poi_match(
        "Mercado Central de Sangolquí",
        "Mercado Central de Sangolqui",
    )
    assert s > 0.95


def test_score_poi_match_partial_crosses_threshold():
    # "Colegio Juan Montalvo" vs "Unidad Educativa Juan Montalvo"
    # token_set_ratio tolerates disjoint tokens when the overlap is full.
    s = pm.score_poi_match(
        "Colegio Juan Montalvo", "Unidad Educativa Juan Montalvo"
    )
    # This should be well above 0.70 because "Juan Montalvo" matches as a token set
    assert s >= pm.NAME_SIMILARITY_MIN


def test_score_poi_match_empty_returns_zero():
    assert pm.score_poi_match("", "Mercado Central") == 0.0
    assert pm.score_poi_match("Mercado Central", "") == 0.0
    assert pm.score_poi_match("", "") == 0.0


def test_score_poi_match_unrelated_below_threshold():
    s = pm.score_poi_match("Hospital Metropolitano", "Parque La Carolina")
    assert s < pm.NAME_SIMILARITY_MIN


# ---------------------------------------------------------------------------
# 2. query_pois_by_name — tag filter, parsing, cache
# ---------------------------------------------------------------------------


def test_query_pois_by_name_happy_path():
    calls = {"n": 0}

    def _fake_post(url, data=None, timeout=None, **kw):
        calls["n"] += 1
        ql = data["data"]
        assert "Mercado" in ql
        assert "marketplace" in ql
        return _FakeResponse(
            status_code=200,
            json_data={"elements": [_overpass_poi(1001)]},
        )

    matches = pm.query_pois_by_name(
        "Mercado", (-0.2, -78.5, -0.15, -78.45), http_post=_fake_post,
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.osm_id == 1001
    assert m.class_name == "marketplace"
    assert m.class_priority == pm.POI_CLASS_PRIORITY["marketplace"]
    assert m.centroid == (-0.180, -78.470)
    assert calls["n"] == 1


def test_query_pois_by_name_cache_hit_skips_http():
    calls = {"n": 0}

    def _fake_post(url, data=None, timeout=None, **kw):
        calls["n"] += 1
        return _FakeResponse(
            status_code=200,
            json_data={"elements": [_overpass_poi(1001)]},
        )

    bbox = (-0.2, -78.5, -0.15, -78.45)
    pm.query_pois_by_name("Mercado", bbox, http_post=_fake_post)
    pm.query_pois_by_name("Mercado", bbox, http_post=_fake_post)  # cached
    assert calls["n"] == 1


def test_query_pois_by_name_cache_ttl_expires(monkeypatch):
    calls = {"n": 0}

    def _fake_post(url, data=None, timeout=None, **kw):
        calls["n"] += 1
        return _FakeResponse(
            status_code=200,
            json_data={"elements": [_overpass_poi(1001)]},
        )

    bbox = (-0.2, -78.5, -0.15, -78.45)
    pm.query_pois_by_name("Mercado", bbox, http_post=_fake_post, now=1000.0)
    # just before expiry — still cached
    pm.query_pois_by_name(
        "Mercado", bbox, http_post=_fake_post, now=1000.0 + pm.CACHE_TTL_S - 1
    )
    assert calls["n"] == 1
    # after expiry — re-fetches
    pm.query_pois_by_name(
        "Mercado", bbox, http_post=_fake_post, now=1000.0 + pm.CACHE_TTL_S + 10
    )
    assert calls["n"] == 2


def test_query_pois_by_name_parses_way_centroid():
    """Ways return via 'center' — parser must honour it."""
    el_way = {
        "id": 42, "type": "way",
        "center": {"lat": -0.181, "lon": -78.471},
        "tags": {"name": "Mercado Grande", "amenity": "marketplace"},
    }

    def _fake_post(url, data=None, timeout=None, **kw):
        return _FakeResponse(status_code=200, json_data={"elements": [el_way]})

    matches = pm.query_pois_by_name(
        "Mercado", (-0.2, -78.5, -0.15, -78.45), http_post=_fake_post,
    )
    assert len(matches) == 1
    assert matches[0].osm_type == "way"
    assert matches[0].centroid == (-0.181, -78.471)


def test_query_pois_by_name_http_error_raises():
    def _fake_post(url, data=None, timeout=None, **kw):
        return _FakeResponse(status_code=502, text="bad gateway")

    with pytest.raises(pm.POIMatcherError, match="HTTP 502"):
        pm.query_pois_by_name(
            "Mercado", (-0.2, -78.5, -0.15, -78.45), http_post=_fake_post,
        )


def test_query_pois_by_name_timeout_raises():
    def _fake_post(url, data=None, timeout=None, **kw):
        raise requests.Timeout("overpass slow")

    with pytest.raises(pm.POIMatcherError, match="timeout"):
        pm.query_pois_by_name(
            "Mercado", (-0.2, -78.5, -0.15, -78.45), http_post=_fake_post,
        )


def test_query_pois_by_name_drops_unnamed_elements():
    def _fake_post(url, data=None, timeout=None, **kw):
        return _FakeResponse(
            status_code=200,
            json_data={
                "elements": [
                    _overpass_poi(1001),
                    {"id": 2, "type": "node", "lat": -0.18, "lon": -78.47,
                     "tags": {"amenity": "marketplace"}},  # no name — skip
                ],
            },
        )

    matches = pm.query_pois_by_name(
        "Mercado", (-0.2, -78.5, -0.15, -78.45), http_post=_fake_post,
    )
    assert len(matches) == 1
    assert matches[0].osm_id == 1001


def test_query_pois_by_name_honours_custom_tag_filter():
    captured = {}

    def _fake_post(url, data=None, timeout=None, **kw):
        captured["ql"] = data["data"]
        return _FakeResponse(status_code=200, json_data={"elements": []})

    pm.query_pois_by_name(
        "Sangolquí",
        (-0.2, -78.5, -0.15, -78.45),
        tag_filters=pm.OSM_FILL_TAG_FILTERS,
        http_post=_fake_post,
    )
    assert "village" in captured["ql"]
    assert "hamlet" in captured["ql"]


# ---------------------------------------------------------------------------
# 3. filter_and_score_candidates — name threshold + 80m ceiling
# ---------------------------------------------------------------------------


def test_filter_candidates_drops_below_similarity():
    # polyline runs straight on latitude -0.180
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    bad = pm.POIMatch(
        osm_id=1, osm_type="node",
        name="Parque Totalmente Distinto",
        centroid=(-0.180, -78.470),  # right on the line
        tags={}, class_name="marketplace", class_priority=100,
    )
    out = pm.filter_and_score_candidates("Mercado Central", [bad], poly)
    assert out == []


def test_filter_candidates_drops_over_80m_ceiling():
    # point ~200m north of an E-W line
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    far = pm.POIMatch(
        osm_id=1, osm_type="node",
        name="Mercado Central",
        centroid=(-0.178, -78.470),  # ~220m north
        tags={}, class_name="marketplace", class_priority=100,
    )
    out = pm.filter_and_score_candidates("Mercado Central", [far], poly)
    assert out == []


def test_filter_candidates_keeps_nearby_high_similarity_and_sorts():
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    close_market = pm.POIMatch(
        osm_id=1, osm_type="node",
        name="Mercado Central de Sangolquí",
        centroid=(-0.180, -78.470),  # on path
        tags={}, class_name="marketplace", class_priority=100,
    )
    # school that is further but also on path
    school = pm.POIMatch(
        osm_id=2, osm_type="node",
        name="Escuela Mercado Central",
        centroid=(-0.1801, -78.469),  # ~11m south of line + 100m east
        tags={}, class_name="school", class_priority=85,
    )
    out = pm.filter_and_score_candidates(
        "Mercado Central de Sangolquí", [school, close_market], poly,
    )
    assert len(out) == 2
    # sorted by distance ASC → marketplace (on line) comes before school
    assert out[0].osm_id == 1
    assert out[0].poi_to_path_distance_m is not None
    assert out[0].name_similarity is not None
    assert out[0].name_similarity > 0.85


# ---------------------------------------------------------------------------
# 4. rank_for_gap_fill — class priority ordering
# ---------------------------------------------------------------------------


def test_rank_for_gap_fill_prefers_higher_class_priority():
    a = pm.POIMatch(osm_id=1, osm_type="node", name="Casa Cultural",
                    centroid=(0, 0), tags={}, class_name="place_of_worship",
                    class_priority=75, poi_to_path_distance_m=20.0)
    b = pm.POIMatch(osm_id=2, osm_type="node", name="Mercado",
                    centroid=(0, 0), tags={}, class_name="marketplace",
                    class_priority=100, poi_to_path_distance_m=30.0)
    c = pm.POIMatch(osm_id=3, osm_type="node", name="Junction",
                    centroid=(0, 0), tags={}, class_name="junction",
                    class_priority=20, poi_to_path_distance_m=5.0)
    ranked = pm.rank_for_gap_fill([a, b, c])
    assert [r.osm_id for r in ranked] == [2, 1, 3]


# ---------------------------------------------------------------------------
# 5. poi_to_access_point — entrance > road_projection > centroid_fallback
# ---------------------------------------------------------------------------


def test_access_point_entrance_node_wins_when_near_path():
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    # entrance node ~10m south of path; POI centroid 80m south
    poi = pm.POIMatch(
        osm_id=1, osm_type="way", name="Mercado",
        centroid=(-0.1807, -78.470),  # ~78m off
        tags={}, class_name="marketplace", class_priority=100,
        entrance_nodes=((-0.18009, -78.470),),  # ~10m off path
    )
    ap = pm.poi_to_access_point(poi, poly)
    assert ap.access_type == "entrance_node"
    assert ap.entrance_node_coords == (-0.18009, -78.470)


def test_access_point_road_projection_when_road_near_poi():
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    # POI with no entrances, road edge at (-0.1802, -78.470) within 50m of POI
    poi = pm.POIMatch(
        osm_id=2, osm_type="way", name="Mercado",
        centroid=(-0.1803, -78.470),  # ~33m south of line
        tags={}, class_name="marketplace", class_priority=100,
    )
    ap = pm.poi_to_access_point(
        poi, poly, road_projection_coords=(-0.1802, -78.470),
    )
    assert ap.access_type == "road_projection"


def test_access_point_centroid_fallback_when_no_entrance_no_road():
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    poi = pm.POIMatch(
        osm_id=3, osm_type="node", name="Small Shop",
        centroid=(-0.1801, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )
    ap = pm.poi_to_access_point(poi, poly)
    assert ap.access_type == "centroid_fallback"
    # projection should land on the path
    assert abs(ap.lat - -0.180) < 1e-5


def test_access_point_ignores_entrance_further_than_40m():
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    # entrance_node 100m off the path — ignored, should fall through
    poi = pm.POIMatch(
        osm_id=4, osm_type="way", name="Big Facility",
        centroid=(-0.1802, -78.470),
        tags={}, class_name="hospital", class_priority=65,
        entrance_nodes=((-0.179, -78.470),),  # ~111m off the line
    )
    ap = pm.poi_to_access_point(poi, poly)
    assert ap.access_type != "entrance_node"


# ---------------------------------------------------------------------------
# 6. Centroid geometry helpers
# ---------------------------------------------------------------------------


def test_polygon_centroid_of_square():
    square = [(-0.180, -78.470), (-0.181, -78.470), (-0.181, -78.471), (-0.180, -78.471)]
    lat, lon = pm._polygon_centroid(square)
    assert abs(lat - -0.1805) < 1e-5
    assert abs(lon - -78.4705) < 1e-5


def test_polygon_centroid_from_geometry_list():
    el = {
        "id": 7, "type": "way",
        "geometry": [
            {"lat": -0.180, "lon": -78.470},
            {"lat": -0.181, "lon": -78.470},
            {"lat": -0.181, "lon": -78.471},
            {"lat": -0.180, "lon": -78.471},
            {"lat": -0.180, "lon": -78.470},
        ],
        "tags": {"name": "X", "amenity": "marketplace"},
    }
    latlon = pm._element_centroid(el)
    assert latlon is not None
    assert abs(latlon[0] - -0.1805) < 1e-5


# ---------------------------------------------------------------------------
# 7. Normalisation
# ---------------------------------------------------------------------------


def test_normalise_name_strips_accents_and_case():
    assert pm._normalise_name("Sangolquí") == "sangolqui"
    assert pm._normalise_name("  MERCADO  ") == "mercado"
    assert pm._normalise_name("") == ""
