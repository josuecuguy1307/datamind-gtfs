from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from datamind_console.common import review_queue_writer as rw


@pytest.fixture()
def review_root(tmp_path: Path) -> Path:
    for sub in ("pending", "verified", "rejected", "semantic_spatial_conflicts"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _base_kwargs(**overrides):
    kw = dict(
        review_root=Path("."),  # overridden per test
        node_id=str(uuid.uuid4()),
        osm_id=-1000042,
        stage="3a_poi_on_path",
        source_type="poi_anchored_path_projected",
        source="poi_anchored_path_projected:CAL-03:y-de-calsig",
        unit="ruminahui",
        province="sample_region",
        route_code="CAL-03",
        route_id=str(uuid.uuid4()),
        anchor_name="Y de Calsig",
        anchor_slug="y-de-calsig",
        synthetic_confidence="medium",
        final_coords=(-0.316, -78.443),
        research_coords=None,
        poi_osm_url="https://osm.org/node/123456",
        matched_road=None,
        poi_to_path_distance_m=12.4,
        path_projection_distance_m=5.1,
        research_to_projection_distance_m=None,
        polyline_url="https://example.invalid/viz?encoded=abc",
        polyline_source="valhalla_with_anchors",
        semantic_spatial_conflict=False,
        osm_route_fill_context=None,
        cap_weight_consumed=0.0,
        triggered_by_skill="hades-path-aware-synthesis",
        research_output_file="01_stop_grounding_detail_ruminahui_CAL-03_20260419-1445.json",
        evidence_markdown="- POI matched by name slug\n- 12.4m from Valhalla path\n",
        why_synthesis_fired="Stage 3a succeeded on first POI candidate.",
    )
    kw.update(overrides)
    return kw


# ---------------------------------------------------------------------------
# 1. happy path — file created in pending/, frontmatter carries both
#    source_type AND source
# ---------------------------------------------------------------------------


def test_write_synthetic_review_creates_file_in_pending(review_root):
    path = rw.write_synthetic_review(**_base_kwargs(review_root=review_root))
    assert path.exists()
    assert path.parent == review_root / "pending"

    text = path.read_text(encoding="utf-8")
    # Both columns present — skill doc §9 requires the dual-column schema.
    assert "source_type: poi_anchored_path_projected" in text
    assert "source:" in text
    assert "poi_anchored_path_projected:CAL-03:y-de-calsig" in text

    # required body sections
    assert "## Anchor" in text
    assert "Y de Calsig" in text
    assert "## Why synthesis fired" in text
    assert "## Evidence" in text
    assert "## Operator decision" in text


# ---------------------------------------------------------------------------
# 2. filename convention uses STAGE, not source_type
# ---------------------------------------------------------------------------


def test_filename_uses_stage_not_source_type(review_root):
    path = rw.write_synthetic_review(**_base_kwargs(review_root=review_root))
    # stage goes in the slot, not source_type
    assert "_3a_poi_on_path_" in path.name
    assert "poi_anchored_path_projected" not in path.name


def test_filename_slots_all_present(review_root):
    path = rw.write_synthetic_review(
        **_base_kwargs(
            review_root=review_root,
            stage="4_pure_synthesis",
            anchor_name="Parada Intermedia Sin POI",
            anchor_slug="parada-intermedia-sin-poi",
            source_type="pure_synthesis",
            source="pure_synthesis:CAL-03:parada-intermedia-sin-poi",
            cap_weight_consumed=1.0,
        )
    )
    # priority_stage_unit_routecode_anchorslug_timestamp.md
    parts = path.stem.split("_")
    assert parts[0] == "01"  # pure_synthesis → priority 01
    assert "4_pure_synthesis" in path.stem
    assert "ruminahui" in path.stem
    assert "cal-03" in path.stem
    assert "parada-intermedia-sin-poi" in path.stem


# ---------------------------------------------------------------------------
# 3. priority-bucket rules per skill §9
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage,expected_priority", [
    ("3a_poi_on_path", "03"),
    ("3b_path_corridor", "03"),
    ("3c_path_intersection", "03"),
    ("osm_route_fill", "03"),
    ("3d_research_coords_snapped", "02"),
    ("4_pure_synthesis", "01"),
])
def test_compute_priority_by_stage(stage, expected_priority):
    p = rw.compute_priority(stage=stage, semantic_spatial_conflict=False)
    assert p == expected_priority


def test_compute_priority_semantic_conflict_always_00():
    for stage in (
        "3a_poi_on_path", "3b_path_corridor", "3c_path_intersection",
        "3d_research_coords_snapped", "4_pure_synthesis", "osm_route_fill",
    ):
        assert rw.compute_priority(
            stage=stage, semantic_spatial_conflict=True
        ) == "00"


def test_compute_priority_rejects_unknown_stage():
    with pytest.raises(ValueError):
        rw.compute_priority(stage="bogus", semantic_spatial_conflict=False)


# ---------------------------------------------------------------------------
# 4. slugify handles Spanish accents
# ---------------------------------------------------------------------------


def test_slugify_handles_spanish_accents():
    assert rw.slugify("Y de Cálsig") == "y-de-calsig"
    assert rw.slugify("Parroquia San Pedro de Tabacundo") == \
        "parroquia-san-pedro-de-tabacundo"
    assert rw.slugify("Ñuñoa – Centro") == "nunoa-centro"


def test_slugify_length_limit():
    # slugify's default max_len is 64 in text_utils
    long = "a" * 100
    assert len(rw.slugify(long)) <= 64


# ---------------------------------------------------------------------------
# 5. source_type must match synthesis enum
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_type", [
    "osm",
    "backfill",
    "poi_anchored_path_projected",
    "path_corridor_projected",
    "path_intersection",
    "research_coords_path_snapped",
    "pure_synthesis",
    "gps_trace",
])
def test_write_accepts_all_8_source_types(review_root, source_type):
    path = rw.write_synthetic_review(
        **_base_kwargs(review_root=review_root, source_type=source_type)
    )
    text = path.read_text(encoding="utf-8")
    assert f"source_type: {source_type}" in text


def test_write_rejects_unknown_source_type(review_root):
    with pytest.raises(ValueError, match="source_type"):
        rw.write_synthetic_review(
            **_base_kwargs(review_root=review_root, source_type="bogus_value")
        )


# ---------------------------------------------------------------------------
# 6. semantic_spatial_conflict goes into semantic_spatial_conflicts/ bucket
#    (or pending/ with 00 priority — skill says pending/ with 00 sort)
# ---------------------------------------------------------------------------


def test_semantic_spatial_conflict_lands_in_conflicts_folder(review_root):
    path = rw.write_synthetic_review(
        **_base_kwargs(review_root=review_root, semantic_spatial_conflict=True)
    )
    assert path.parent == review_root / "semantic_spatial_conflicts"
    text = path.read_text(encoding="utf-8")
    assert "semantic_spatial_conflict: true" in text
    assert path.name.startswith("00_")


# ---------------------------------------------------------------------------
# 7. idempotent write — same node_id + stage → same path, no overwrite
# ---------------------------------------------------------------------------


def test_idempotent_write_same_node_id_stage(review_root):
    kw = _base_kwargs(review_root=review_root)
    first = rw.write_synthetic_review(**kw)
    first_body = first.read_bytes()
    second = rw.write_synthetic_review(**{**kw, "why_synthesis_fired": "DIFFERENT"})
    assert second == first
    # body was NOT overwritten with the second call's content
    assert first.read_bytes() == first_body


# ---------------------------------------------------------------------------
# 8. malformed final_coords rejected
# ---------------------------------------------------------------------------


def test_malformed_final_coords_rejected(review_root):
    with pytest.raises(ValueError, match="final_coords"):
        rw.write_synthetic_review(
            **_base_kwargs(review_root=review_root, final_coords=(-0.316,))
        )


def test_missing_final_coords_rejected(review_root):
    with pytest.raises(ValueError, match="final_coords"):
        rw.write_synthetic_review(
            **_base_kwargs(review_root=review_root, final_coords=None)
        )


# ---------------------------------------------------------------------------
# 9. cap_weight_consumed validated against stage
# ---------------------------------------------------------------------------


def test_cap_weight_must_match_stage(review_root):
    # stage 3a has weight 0.0 — passing 1.0 is a caller bug
    with pytest.raises(ValueError, match="cap_weight"):
        rw.write_synthetic_review(
            **_base_kwargs(
                review_root=review_root,
                stage="3a_poi_on_path",
                cap_weight_consumed=1.0,
            )
        )


# ---------------------------------------------------------------------------
# 10. atomic write — no partial files on failure
# ---------------------------------------------------------------------------


def test_atomic_write_no_partial_files(review_root, monkeypatch):
    original_replace = rw.os.replace

    def bad_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(rw.os, "replace", bad_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        rw.write_synthetic_review(**_base_kwargs(review_root=review_root))

    # no *.tmp files left behind
    tmps = list((review_root / "pending").glob("*.tmp"))
    assert not tmps, f"tmp files left behind: {tmps}"
    # no .md files either — the failed write produced nothing visible
    mds = list((review_root / "pending").glob("*.md"))
    assert not mds
    monkeypatch.setattr(rw.os, "replace", original_replace)


# ---------------------------------------------------------------------------
# 11. move helpers: mv_to_verified / mv_to_rejected / mv_to_conflicts
# ---------------------------------------------------------------------------


def test_mv_to_verified_moves_file(review_root):
    p = rw.write_synthetic_review(**_base_kwargs(review_root=review_root))
    moved = rw.mv_to_verified(review_root=review_root, filename=p.name)
    assert not p.exists()
    assert moved == review_root / "verified" / p.name
    text = moved.read_text(encoding="utf-8")
    assert "synthetic_review_state: verified" in text


def test_mv_to_rejected_records_reason(review_root):
    p = rw.write_synthetic_review(**_base_kwargs(review_root=review_root))
    moved = rw.mv_to_rejected(
        review_root=review_root,
        filename=p.name,
        rejected_reason="POI is not actually a stop",
    )
    assert moved == review_root / "rejected" / p.name
    text = moved.read_text(encoding="utf-8")
    assert "synthetic_review_state: rejected" in text
    assert "rejected_reason:" in text
    assert "POI is not actually a stop" in text


def test_mv_to_conflicts_from_pending(review_root):
    p = rw.write_synthetic_review(**_base_kwargs(review_root=review_root))
    moved = rw.mv_to_conflicts(review_root=review_root, filename=p.name)
    assert moved == review_root / "semantic_spatial_conflicts" / p.name
    assert not p.exists()


def test_mv_raises_when_source_missing(review_root):
    with pytest.raises(FileNotFoundError):
        rw.mv_to_verified(review_root=review_root, filename="nope.md")


# ---------------------------------------------------------------------------
# 12. frontmatter round-trip — every required skill-§9 field present
# ---------------------------------------------------------------------------


def test_frontmatter_round_trip(review_root):
    p = rw.write_synthetic_review(**_base_kwargs(review_root=review_root))
    text = p.read_text(encoding="utf-8")

    required_keys = [
        "node_id:", "osm_id:", "stage:", "source_type:", "source:",
        "unit:", "province:", "route_code:", "route_id:",
        "anchor_name:", "synthetic_confidence:", "synthetic_review_state:",
        "final_coords:", "research_coords:", "poi_osm_url:", "matched_road:",
        "poi_to_path_distance_m:", "path_projection_distance_m:",
        "research_to_projection_distance_m:",
        "polyline_url:", "polyline_source:",
        "semantic_spatial_conflict:", "osm_route_fill_context:",
        "cap_weight_consumed:",
        "triggered_by_skill:", "research_output_file:", "created_at:",
    ]
    for k in required_keys:
        assert k in text, f"missing frontmatter key: {k}"


# ---------------------------------------------------------------------------
# 13. deterministic anchor_slug fallback
# ---------------------------------------------------------------------------


def test_anchor_slug_auto_derived_from_anchor_name(review_root):
    kw = _base_kwargs(
        review_root=review_root,
        anchor_name="Centro Comercial El Recreo",
    )
    kw.pop("anchor_slug")
    path = rw.write_synthetic_review(**kw)
    assert "centro-comercial-el-recreo" in path.name


# ---------------------------------------------------------------------------
# 14. node_id must be a valid UUID-shaped string
# ---------------------------------------------------------------------------


def test_rejects_non_uuid_node_id(review_root):
    with pytest.raises(ValueError, match="node_id"):
        rw.write_synthetic_review(
            **_base_kwargs(review_root=review_root, node_id="not-a-uuid")
        )


# ---------------------------------------------------------------------------
# 15. osm_id must be in the synthetic range (negative, <= -1000000)
# ---------------------------------------------------------------------------


def test_osm_id_must_be_in_synthetic_range(review_root):
    with pytest.raises(ValueError, match="osm_id"):
        rw.write_synthetic_review(
            **_base_kwargs(review_root=review_root, osm_id=12345)  # positive = real OSM id
        )
    with pytest.raises(ValueError, match="osm_id"):
        rw.write_synthetic_review(
            **_base_kwargs(review_root=review_root, osm_id=-5000)  # > -1000000
        )


# ---------------------------------------------------------------------------
# 16. compute_filename exposed and deterministic for a fixed timestamp
# ---------------------------------------------------------------------------


def test_compute_filename_deterministic():
    name = rw.compute_filename(
        priority="00",
        stage="3a_poi_on_path",
        unit="ruminahui",
        route_code="CAL-03",
        anchor_slug="y-de-calsig",
        timestamp="20260419-1445",
    )
    assert name == "00_3a_poi_on_path_ruminahui_cal-03_y-de-calsig_20260419-1445.md"
