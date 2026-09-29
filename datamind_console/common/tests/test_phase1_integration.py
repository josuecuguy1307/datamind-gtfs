"""
Phase 1 end-to-end integration test.

Exercises the three Phase 1 modules against a shared disk workspace:
1. research_queue writes a prompt → operator "sends" it → response lands
2. review_queue_writer emits a review file for a synthesis event
3. synthesis_events logs the event to an in-memory fake DB

The real DB path is exercised separately by the Phase 3 canary.  Here we
just want to prove the three modules compose without surprises (no
filename-format mismatches, no contract drift, both ``source_type`` and
``source`` make it into the review frontmatter, etc.).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from datamind_console.common import research_queue as rq
from datamind_console.common import review_queue_writer as rw
from datamind_console.common import synthesis_events as se

from datamind_console.common.tests.test_synthesis_events import (
    _FakeConn, _FakeStore,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    rq_root = tmp_path / "research_queue"
    rv_root = rq_root / "synthetic_review"
    for sub in ("pending", "sent", "responses", "ingested", "archive"):
        (rq_root / sub).mkdir(parents=True, exist_ok=True)
    for sub in ("pending", "verified", "rejected", "semantic_spatial_conflicts"):
        (rv_root / sub).mkdir(parents=True, exist_ok=True)
    return {"queue": rq_root, "review": rv_root}


def test_phase1_end_to_end(workspace):
    """Full cycle: research prompt → response → synthesis event → review file."""
    queue = workspace["queue"]
    review = workspace["review"]

    # --- 1. skill emits a research prompt for an un-grounded route ---------
    prompt_path = rq.write_prompt(
        queue_root=queue,
        prompt_type="stop_grounding_detail",
        route_code="CAL-03",
        unit="ruminahui",
        province="sample_region",
        trigger_condition="Phase 3 grounded_stops=0 for CAL-03",
        priority=1,
        estimated_research_budget="standard",
        depends_on=[],
        dedup_key="stop_grounding_detail:ruminahui:CAL-03:grounded_stops_zero",
        prompt_markdown_content="# Stop grounding request\n\nBody.\n",
        generated_by_skill="06c_DEEP_RESEARCH_STOP_GROUNDING",
    )
    assert prompt_path.parent == queue / "pending"
    assert "cal-03" in prompt_path.name
    assert "ruminahui" in prompt_path.name

    # --- 2. operator "pastes" and moves to sent/ --------------------------
    sent_path = rq.move_to_sent(queue_root=queue, filename=prompt_path.name)
    assert sent_path.parent == queue / "sent"
    assert "sent_at:" in sent_path.read_text(encoding="utf-8")

    # --- 3. response lands, operator ingests ------------------------------
    response_name = sent_path.stem + ".json"
    (queue / "responses" / response_name).write_text(
        json.dumps({
            "stops": [
                {"anchor": "Y de Calsig", "lat": -0.316, "lon": -78.443},
            ],
        }),
        encoding="utf-8",
    )
    ingest_result = {"status": "ok", "fields_merged": ["stops"], "warnings": []}
    prompt_dst, response_dst, sidecar = rq.ingest_and_pair_move(
        queue_root=queue,
        response_filename=response_name,
        ingestion_result=ingest_result,
    )
    assert prompt_dst.parent == queue / "ingested"
    assert response_dst.parent == queue / "ingested"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == ingest_result
    # no stragglers
    for sub in ("pending", "sent", "responses"):
        assert not list((queue / sub).glob("*")), \
            f"{sub}/ should be empty after ingestion"

    # --- 4. Phase 3 (future) runs path-aware synthesis ---------------------
    # stage 3a succeeded: POI found on-path. Log the event and emit a
    # review file.
    store = _FakeStore()
    conn = _FakeConn(store)

    node_id = str(uuid.uuid4())
    route_id = str(uuid.uuid4())
    se.log_synthesis_event(
        conn,
        node_id=node_id,
        route_id=route_id,
        unit="ruminahui",
        province="sample_region",
        stage="3a_poi_on_path",
        anchor_name="Y de Calsig",
        final_coords=(-0.316, -78.443),
        match_score=0.92,
        triggered_by_skill="hades-path-aware-synthesis",
        research_output_file=response_name,
    )
    assert len(store.rows) == 1
    assert store.rows[0]["stage"] == "3a_poi_on_path"
    # synthesis_events stored only the basename, not the full path.
    assert store.rows[0]["research_output_file"] == response_name

    # --- 5. review file written for operator approval ----------------------
    review_path = rw.write_synthetic_review(
        review_root=review,
        node_id=node_id,
        osm_id=-1000042,
        stage="3a_poi_on_path",
        source_type="poi_anchored_path_projected",
        source="poi_anchored_path_projected:CAL-03:y-de-calsig",
        unit="ruminahui",
        province="sample_region",
        route_code="CAL-03",
        route_id=route_id,
        anchor_name="Y de Calsig",
        synthetic_confidence="medium",
        final_coords=(-0.316, -78.443),
        poi_to_path_distance_m=12.4,
        path_projection_distance_m=5.1,
        polyline_url="https://example.invalid/viz?encoded=abc",
        polyline_source="valhalla_with_anchors",
        cap_weight_consumed=0.0,
        triggered_by_skill="hades-path-aware-synthesis",
        research_output_file=response_name,
        evidence_markdown="- POI name match\n- 12.4m from Valhalla path\n",
        why_synthesis_fired="Stage 3a matched on POI name and distance.\n",
    )
    assert review_path.parent == review / "pending"
    assert review_path.name.startswith("03_3a_poi_on_path_")

    text = review_path.read_text(encoding="utf-8")
    # Dual-column schema round-trips
    assert "source_type: poi_anchored_path_projected" in text
    assert (
        'source: "poi_anchored_path_projected:CAL-03:y-de-calsig"' in text
    )
    # Research-queue filename is retained as the foreign key
    assert response_name in text

    # --- 6. operator approves ---------------------------------------------
    verified = rw.mv_to_verified(review_root=review, filename=review_path.name)
    assert verified.parent == review / "verified"
    assert "synthetic_review_state: verified" in verified.read_text(
        encoding="utf-8"
    )


def test_phase1_cap_trip_emits_rejection_event(workspace):
    """When the per-route cap is hit, synthesis is rejected and logged with
    ``rejected_reason='route_synthesis_cap_hit'`` — no review file emitted."""
    review = workspace["review"]
    store = _FakeStore()
    conn = _FakeConn(store)
    route_id = str(uuid.uuid4())

    # Consume the full 3.0 cap with 3 stage-4 events.
    for _ in range(3):
        se.log_synthesis_event(
            conn,
            node_id=str(uuid.uuid4()),
            route_id=route_id,
            unit="ruminahui",
            province="sample_region",
            stage="4_pure_synthesis",
            triggered_by_skill="hades-path-aware-synthesis",
        )
    consumed = se.compute_route_cap_consumed(conn, route_id=route_id)
    assert consumed == pytest.approx(3.0)
    assert se.is_route_at_cap(consumed)

    # Next attempt is rejected; logged with node_id=None.
    se.log_synthesis_event(
        conn,
        node_id=None,
        route_id=route_id,
        unit="ruminahui",
        province="sample_region",
        stage="4_pure_synthesis",
        rejected_reason="route_synthesis_cap_hit",
        triggered_by_skill="hades-path-aware-synthesis",
    )
    # 4 writes total, one a rejection.
    rejections = [r for r in store.rows if r["rejected_reason"]]
    assert len(rejections) == 1
    # No review files written — the rejection never became a node.
    assert not list((review / "pending").glob("*.md"))


def test_phase1_semantic_spatial_conflict_routes_to_conflicts(workspace):
    """POI inference vs path inference diverged > 80 m. Review file lands
    in semantic_spatial_conflicts/ with priority 00."""
    review = workspace["review"]
    review_path = rw.write_synthetic_review(
        review_root=review,
        node_id=str(uuid.uuid4()),
        osm_id=-1000100,
        stage="3a_poi_on_path",
        source_type="poi_anchored_path_projected",
        source="poi_anchored_path_projected:CAL-03:ambiguous-anchor",
        unit="ruminahui",
        province="sample_region",
        route_code="CAL-03",
        route_id=str(uuid.uuid4()),
        anchor_name="Ambiguous Anchor",
        synthetic_confidence="low",
        final_coords=(-0.316, -78.443),
        research_coords=(-0.320, -78.450),
        poi_to_path_distance_m=95.7,
        path_projection_distance_m=11.2,
        research_to_projection_distance_m=420.3,
        polyline_url="https://example.invalid/viz?encoded=xyz",
        polyline_source="valhalla_with_anchors",
        semantic_spatial_conflict=True,
        cap_weight_consumed=0.0,
        triggered_by_skill="hades-path-aware-synthesis",
        research_output_file="01_stop_grounding_detail_ruminahui_CAL-03_x.json",
        evidence_markdown="- POI 95.7m from path\n- Research coords 420m from projection\n",
        why_synthesis_fired="Semantic-spatial conflict: POI vs research coords disagree > 80m.\n",
    )
    assert review_path.parent == review / "semantic_spatial_conflicts"
    assert review_path.name.startswith("00_")
    text = review_path.read_text(encoding="utf-8")
    assert "semantic_spatial_conflict: true" in text
