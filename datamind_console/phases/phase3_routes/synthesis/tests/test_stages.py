"""
Unit tests for datamind_console.phases.phase3_routes.synthesis.stages.

Each stage function is called with explicit candidates / fetchers so no
real HTTP is performed.
"""
from __future__ import annotations

import pytest

from datamind_console.phases.phase3_routes.synthesis import (
    path_inference as pi,
    poi_matcher as pm,
    stages,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def polyline():
    # straight E-W line at lat=-0.180, lons from -78.475 to -78.465
    return pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])


@pytest.fixture()
def ctx(polyline):
    return stages.StageContext(
        route_id="route-x",
        route_code="CAL-03",
        unit="ruminahui",
        province="sample_region",
        polyline=polyline,
    )


# ---------------------------------------------------------------------------
# stage 3a — POI-on-path
# ---------------------------------------------------------------------------


def test_stage_3a_happy_path_returns_high_confidence(ctx):
    poi = pm.POIMatch(
        osm_id=1, osm_type="node",
        name="Mercado Central de Sangolquí",
        centroid=(-0.180, -78.470),  # on the line
        tags={}, class_name="marketplace", class_priority=100,
    )
    res = stages.stage_3a_poi_on_path(
        ctx, "Mercado Central de Sangolqui", candidates=[poi]
    )
    assert res.success is True
    assert res.stage == stages.STAGE_3A
    assert res.source_type == stages.SOURCE_TYPE_POI
    assert res.source == "poi_anchored_path_projected:CAL-03:mercado-central-de-sangolqui"
    assert res.synthetic_confidence == stages.CONF_HIGH
    assert res.final_coords is not None
    assert res.poi_to_path_distance_m is not None
    assert res.poi_match is not None
    assert res.poi_match.osm_id == 1


def test_stage_3a_no_matches_returns_rejection(ctx):
    # POI that doesn't match the anchor by name
    bad = pm.POIMatch(
        osm_id=1, osm_type="node",
        name="Something Else Entirely",
        centroid=(-0.180, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )
    res = stages.stage_3a_poi_on_path(
        ctx, "Mercado Central", candidates=[bad]
    )
    assert res.success is False
    assert res.rejected_reason == "no_poi_within_80m_of_path"


def test_stage_3a_overpass_failure_returns_rejection(ctx, monkeypatch):
    def _boom(*a, **kw):
        raise pm.POIMatcherError("overpass unreachable")

    monkeypatch.setattr(pm, "query_pois_by_name", _boom)
    res = stages.stage_3a_poi_on_path(ctx, "Mercado Central")
    assert res.success is False
    assert res.rejected_reason.startswith("overpass_unreachable")


# ---------------------------------------------------------------------------
# stage 3b — path-corridor-projected
# ---------------------------------------------------------------------------


def test_stage_3b_success_uses_corridor_midpoint(polyline):
    ctx = stages.StageContext(
        route_id="route-x", route_code="CAL-03",
        unit="ruminahui", province="sample_region",
        polyline=pi.encode_polyline([
            (-0.180, -78.475), (-0.180, -78.472),
            (-0.180, -78.470), (-0.180, -78.468), (-0.180, -78.465),
        ]),
    )

    def _fake_fetcher(name, bbox):
        # return a way that overlaps the middle of the path (>50m)
        return [[
            (-0.180, -78.472),
            (-0.180, -78.470),
            (-0.180, -78.468),
        ]]

    ctx.overpass_fetcher = _fake_fetcher
    res = stages.stage_3b_path_corridor(ctx, "Y de Calsig", road_token="Calsig")
    assert res.success is True
    assert res.source_type == stages.SOURCE_TYPE_CORRIDOR
    assert res.synthetic_confidence == stages.CONF_MEDIUM
    assert res.final_coords is not None


def test_stage_3b_no_corridor_returns_rejection(ctx):
    def _fake_fetcher(name, bbox):
        return []

    ctx.overpass_fetcher = _fake_fetcher
    res = stages.stage_3b_path_corridor(ctx, "Y de Calsig", road_token="Calsig")
    assert res.success is False
    assert res.rejected_reason == "no_named_road_on_path"


def test_stage_3b_short_overlap_rejected():
    # Very short overlap — shorter than 50 m
    # polyline of tiny extent: two points 5m apart
    short_poly = pi.encode_polyline([(-0.180, -78.4700), (-0.18004, -78.4700)])
    ctx = stages.StageContext(
        route_id="route-x", route_code="CAL-03",
        unit="ruminahui", province="sample_region",
        polyline=short_poly,
    )

    def _fake_fetcher(name, bbox):
        return [[(-0.180, -78.4700), (-0.18004, -78.4700)]]

    ctx.overpass_fetcher = _fake_fetcher
    res = stages.stage_3b_path_corridor(ctx, "Y de Calsig", road_token="Calsig")
    assert res.success is False
    assert "corridor_overlap_too_short" in (res.rejected_reason or "")


def test_stage_3b_norte_modifier_biases_to_first_vertex():
    poly = pi.encode_polyline([
        (-0.180, -78.475), (-0.180, -78.473), (-0.180, -78.471),
        (-0.180, -78.469), (-0.180, -78.467),
    ])
    ctx = stages.StageContext(
        route_id="route-x", route_code="CAL-03",
        unit="ruminahui", province="sample_region",
        polyline=poly,
    )

    def _fake_fetcher(name, bbox):
        return [[
            (-0.180, -78.475), (-0.180, -78.473),
            (-0.180, -78.471), (-0.180, -78.469),
        ]]

    ctx.overpass_fetcher = _fake_fetcher
    # "norte" → bias toward the first vertex of the overlap
    res_norte = stages.stage_3b_path_corridor(
        ctx, "Calle Calsig norte", road_token="Calsig"
    )
    res_sur = stages.stage_3b_path_corridor(
        ctx, "Calle Calsig sur", road_token="Calsig"
    )
    assert res_norte.success and res_sur.success
    # norte should be further west (more negative lon) than sur
    assert res_norte.final_coords[1] < res_sur.final_coords[1]


# ---------------------------------------------------------------------------
# stage 3c — path-intersection
# ---------------------------------------------------------------------------


def test_stage_3c_success(ctx):
    def _fake_fetcher(name, bbox):
        if "Sucre" in name or "sucre" in name:
            return [[(-0.181, -78.470), (-0.179, -78.470)]]  # N-S
        if "Maldonado" in name or "maldonado" in name:
            return [[(-0.180, -78.471), (-0.180, -78.469)]]  # E-W (along path)
        return []

    ctx.overpass_fetcher = _fake_fetcher
    res = stages.stage_3c_path_intersection(
        ctx, "Calle Sucre y Av. Maldonado",
        road_token_a="Sucre", road_token_b="Maldonado",
    )
    assert res.success is True
    assert res.source_type == stages.SOURCE_TYPE_INTERSECTION
    assert res.synthetic_confidence == stages.CONF_MEDIUM
    assert res.final_coords is not None
    assert abs(res.final_coords[0] - -0.180) < 1e-4
    assert abs(res.final_coords[1] - -78.470) < 1e-4


def test_stage_3c_no_intersection_returns_rejection(ctx):
    def _fake_fetcher(name, bbox):
        return []

    ctx.overpass_fetcher = _fake_fetcher
    res = stages.stage_3c_path_intersection(
        ctx, "Calle X y Calle Y", road_token_a="X", road_token_b="Y",
    )
    assert res.success is False
    assert res.rejected_reason == "no_intersection_found"


def test_stage_3c_rejects_intersection_too_far_from_path(ctx):
    def _fake_fetcher(name, bbox):
        if name == "A":
            # N-S road far south
            return [[(-0.200, -78.470), (-0.210, -78.470)]]
        if name == "B":
            return [[(-0.205, -78.471), (-0.205, -78.469)]]
        return []

    ctx.overpass_fetcher = _fake_fetcher
    res = stages.stage_3c_path_intersection(
        ctx, "A y B", road_token_a="A", road_token_b="B",
    )
    assert res.success is False
    assert "intersection_too_far_from_path" in (res.rejected_reason or "")


# ---------------------------------------------------------------------------
# stage 3d — research-coords-path-snapped
# ---------------------------------------------------------------------------


def test_stage_3d_within_30m_returns_high(ctx):
    # point ~11m off the path
    res = stages.stage_3d_research_coords_snapped(
        ctx, "Y de Calsig", research_coords=(-0.1801, -78.470),
    )
    assert res.success is True
    assert res.synthetic_confidence == stages.CONF_HIGH
    assert res.source_type == stages.SOURCE_TYPE_RESEARCH_SNAPPED
    assert res.research_to_projection_distance_m < 30.0


def test_stage_3d_30_to_80_returns_low_confidence(ctx):
    # ~55m off the path (0.0005 deg lat ≈ 55m)
    res = stages.stage_3d_research_coords_snapped(
        ctx, "Y de Calsig", research_coords=(-0.1805, -78.470),
    )
    assert res.success is True
    assert res.synthetic_confidence == stages.CONF_LOW
    assert 30.0 < res.research_to_projection_distance_m < 80.0


def test_stage_3d_beyond_80m_fails(ctx):
    # ~222m off path
    res = stages.stage_3d_research_coords_snapped(
        ctx, "Y de Calsig", research_coords=(-0.182, -78.470),
    )
    assert res.success is False
    assert res.rejected_reason == "research_coords_too_far_from_path_80m"
    assert res.research_to_projection_distance_m > 80.0


# ---------------------------------------------------------------------------
# stage 4 — pure synthesis
# ---------------------------------------------------------------------------


def test_stage_4_with_between_grounded_uses_midpoint(ctx):
    a = (-0.180, -78.474)
    b = (-0.180, -78.466)
    res = stages.stage_4_pure_synthesis(
        ctx, "Anchor X", between_grounded=(a, b),
    )
    assert res.success is True
    assert res.source_type == stages.SOURCE_TYPE_PURE
    assert res.synthetic_confidence == stages.CONF_LOW
    assert abs(res.final_coords[1] - -78.470) < 1e-4
    assert "midpoint_between_grounded" in " ".join(res.evidence)


def test_stage_4_with_corridor_midpoint_hint(ctx):
    res = stages.stage_4_pure_synthesis(
        ctx, "Anchor X", corridor_midpoint=(-0.180, -78.4695),
    )
    assert res.success is True
    assert "corridor_midpoint" in " ".join(res.evidence)


def test_stage_4_falls_back_to_path_midpoint(ctx):
    res = stages.stage_4_pure_synthesis(ctx, "Anchor X")
    assert res.success is True
    assert "path_midpoint" in " ".join(res.evidence)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_slugify_anchor():
    assert stages.slugify_anchor("Y de Calsig") == "y-de-calsig"
    assert stages.slugify_anchor("Mercado Central de Sangolquí") == \
        "mercado-central-de-sangolqui"
    assert stages.slugify_anchor("") == "anchor"


def test_detect_modifier():
    assert stages._detect_modifier("Calle Calsig norte") == "norte"
    assert stages._detect_modifier("Av Mariscal sur") == "sur"
    assert stages._detect_modifier("Y de Calsig") is None
