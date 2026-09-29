import pytest

from src.pipeline.extraction_policy import (
    prioritize_actions_with_recommendation,
    save_sector_recommendation,
    score_extraction_quality,
    sector_recommendation_for_context,
)


def test_score_extraction_quality_marks_extraction_only_stage_provisional():
    out = score_extraction_quality(
        {
            "stage": "step_build_node_set",
            "quality_scope": "extraction_only",
            "area_group": "valle_core",
            "raw_count": 120,
            "candidate_count": 0,
            "resolved_count": 0,
        },
        area_group="valle_core",
    )

    assert out.get("score") is None
    assert out.get("score_ready") is False
    assert out.get("score_provisional") is not None
    flags = ((out.get("diagnostics") or {}).get("contract_flags") or [])
    assert "candidate_materialization_pending" in flags
    assert "resolve_materialization_pending" in flags


def test_score_extraction_quality_accepts_resolved_total_alias():
    out = score_extraction_quality(
        {
            "stage": "step_build_node_set_tuning_attempt",
            "area_group": "valle_core",
            "raw_count": 400,
            "candidate_count": 120,
            "stop_count": 90,
            "poi_count": 30,
            "name_coverage": 0.75,
            "tag_coverage_public_transport": 0.70,
            "tag_coverage_highway_bus_stop": 0.66,
            "tag_coverage_amenity_bus_station": 0.12,
            "tag_coverage_platform": 0.64,
            "cluster_count": 40,
            "singletons": 9,
            "noise_count": 12,
            "resolved_total": 84,
            "approved_count": 40,
            "spatial_spread_indicator": 0.52,
        },
        area_group="valle_core",
    )

    assert out.get("score_ready") is True
    assert out.get("score") is not None
    components = dict(out.get("components_raw") or {})
    assert components.get("resolve_rate") == pytest.approx(84 / 120, rel=1e-4)


def test_prioritize_actions_with_recommendation_promotes_preferred_action():
    ordered = prioritize_actions_with_recommendation(
        ["stops_quality_bbox", "stops_broad_bbox", "platforms_bbox"],
        preferred_action="platforms_bbox",
    )
    assert ordered[0] == "platforms_bbox"
    assert set(ordered) == {"stops_quality_bbox", "stops_broad_bbox", "platforms_bbox"}


def test_sector_recommendation_for_context_reads_saved_recommendation(tmp_path):
    rec_path = tmp_path / "phase1_recommendations.json"
    save_sector_recommendation(
        sector_key="conocoto_test",
        sector_name="Conocoto Test",
        area_group="conocoto_corridor",
        recommendation={
            "best_action": "stops_broad_bbox",
            "best_bbox_buffer_ratio": 0.25,
            "best_score": 72.3,
        },
        path=str(rec_path),
    )

    out = sector_recommendation_for_context(
        sector_key="conocoto_test",
        area_group="conocoto_corridor",
        path=str(rec_path),
    )
    assert out.get("recommendation_key") == "conocoto_test"
    assert out.get("best_action") == "stops_broad_bbox"
    assert out.get("best_bbox_buffer_ratio") == 0.25
