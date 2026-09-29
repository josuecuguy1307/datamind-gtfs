"""
Unit tests for datamind_console.phases.phase3_routes.synthesis.core.

Uses a fake conn/cursor that speaks the minimum subset of SQL our module
emits (routes cache + osm_relations + nodes + synthesis_events +
synthetic_osm_id_seq). The real DB is exercised by the canary, not here.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from datamind_console.phases.phase3_routes.synthesis import (
    core,
    path_inference as pi,
    poi_matcher as pm,
    stages as st,
)


# ---------------------------------------------------------------------------
# fake conn
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self):
        self.routes: dict[str, dict] = {}
        self.osm_relations: dict[int, str] = {}
        self.nodes: list[dict] = []
        self.events: list[dict] = []
        self._osm_id_counter = itertools.count(-1_000_000, -1)
        self._event_id_counter = itertools.count(1)


class _FakeCursor:
    def __init__(self, store: _FakeStore):
        self._store = store
        self._result: list[Any] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None):
        params = tuple(params or ())
        up = " ".join(sql.split()).upper()

        if up.startswith("SELECT NEXTVAL"):
            val = next(self._store._osm_id_counter)
            self._result = [(val,)]
            return

        if up.startswith("SELECT ENCODED_POLYLINE FROM ROUTE_RAW.OSM_RELATIONS"):
            osm_id = params[0]
            poly = self._store.osm_relations.get(osm_id)
            self._result = [(poly,)] if poly is not None else []
            return

        if up.startswith("SELECT INFERRED_PATH_POLYLINE"):
            route_id = params[0]
            row = self._store.routes.get(route_id)
            if row is None:
                self._result = []
            else:
                self._result = [(
                    row.get("polyline"),
                    row.get("source"),
                    row.get("computed_at"),
                )]
            return

        if up.startswith("UPDATE ROUTE_PROD.ROUTES SET"):
            if len(params) == 1:
                (rid,) = params
                self._store.routes[rid] = {
                    "polyline": None, "source": None, "computed_at": None,
                }
                return
            poly, source, computed_at, rid = params
            self._store.routes[rid] = {
                "polyline": poly, "source": source, "computed_at": computed_at,
            }
            return

        if up.startswith("INSERT INTO NODE_PROD.NODES"):
            (
                node_id, osm_id, lat, lon,
                source, source_type, synth_conf,
                route_id, anchor_name,
                poi_osm_id,
                poi_path_dist, path_proj_dist, research_proj_dist,
            ) = params
            self._store.nodes.append({
                "node_id": node_id, "osm_id": osm_id,
                "lat": lat, "lon": lon,
                "source": source, "source_type": source_type,
                "synthetic_confidence": synth_conf,
                "route_id": route_id, "anchor_name": anchor_name,
                "poi_osm_id": poi_osm_id,
                "poi_to_path_distance_m": poi_path_dist,
                "path_projection_distance_m": path_proj_dist,
                "research_to_projection_distance_m": research_proj_dist,
            })
            return

        if up.startswith("INSERT INTO NODE_PROD.SYNTHESIS_EVENTS"):
            (
                node_id, route_id, unit, province,
                stage, anchor_name,
                rc_lat, rc_lon, fc_lat, fc_lon,
                match_score, rejected_reason,
                triggered_by_skill, research_output_file,
            ) = params
            row = {
                "id": next(self._store._event_id_counter),
                "created_at": datetime.now(timezone.utc),
                "node_id": node_id,
                "route_id": route_id,
                "unit": unit,
                "province": province,
                "stage": stage,
                "anchor_name": anchor_name,
                "research_coords_lat": rc_lat,
                "research_coords_lon": rc_lon,
                "final_coords_lat": fc_lat,
                "final_coords_lon": fc_lon,
                "match_score": match_score,
                "rejected_reason": rejected_reason,
                "triggered_by_skill": triggered_by_skill,
                "research_output_file": research_output_file,
            }
            self._store.events.append(row)
            self._result = [row]
            return

        if "COUNT(*)" in up and "SYNTHESIS_EVENTS" in up:
            # compute_route_cap_consumed: (route_id, stage)
            if (
                "ROUTE_ID = %S" in up and "STAGE = %S" in up
                and "UNIT =" not in up and len(params) == 2
            ):
                route_id, stage = params
                n = sum(
                    1 for e in self._store.events
                    if e["route_id"] == route_id
                    and e["stage"] == stage
                    and e["rejected_reason"] is None
                )
                self._result = [{"n": n}]
                return
            # count_synthesis_events_for_unit_week: (unit, since)
            if "UNIT = %S" in up and "CREATED_AT" in up:
                unit = params[0]
                n = sum(
                    1 for e in self._store.events
                    if e["unit"] == unit
                    and e["rejected_reason"] is None
                )
                self._result = [{"n": n}]
                return

        raise AssertionError(f"fake cursor unexpected SQL: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _FakeConn:
    def __init__(self, store: _FakeStore):
        self._store = store
        self.committed = 0

    def cursor(self, *a, **kw):
        return _FakeCursor(self._store)

    def commit(self):
        self.committed += 1


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store():
    return _FakeStore()


@pytest.fixture()
def conn(store):
    return _FakeConn(store)


@pytest.fixture()
def review_root(tmp_path: Path) -> Path:
    root = tmp_path / "synthetic_review"
    for sub in ("pending", "verified", "rejected", "semantic_spatial_conflicts"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def research_root(tmp_path: Path) -> Path:
    root = tmp_path / "research_queue"
    for sub in ("pending", "sent", "responses", "ingested", "archive"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def seeded_path(store):
    """Preload a cached path on the route so we don't need Valhalla."""
    def _seed(route_id: str):
        poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
        store.routes[route_id] = {
            "polyline": poly,
            "source": pi.PATH_SOURCE_VALHALLA_TERMINUS,
            "computed_at": datetime.now(timezone.utc),
        }
        return poly
    return _seed


def _route(route_id: Optional[str] = None) -> core.Route:
    return core.Route(
        route_id=route_id or str(uuid.uuid4()),
        route_code="CAL-03",
        unit="ruminahui",
        province="sample_region",
        termini=((-0.180, -78.475), (-0.180, -78.465)),
    )


# ---------------------------------------------------------------------------
# 1. Stage 3a happy path — POI wins, node + event + review file written
# ---------------------------------------------------------------------------


def test_path_aware_synthesis_stage_3a_happy_path(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    poi = pm.POIMatch(
        osm_id=9876, osm_type="node",
        name="Mercado Central de Sangolquí",
        centroid=(-0.180, -78.470),  # on path
        tags={}, class_name="marketplace", class_priority=100,
    )

    def _fake_query(name, bbox, **kw):
        return [poi]

    monkeypatch.setattr(pm, "query_pois_by_name", _fake_query)

    anchor = core.Anchor(
        name="Mercado Central de Sangolqui",
        research_output_file="01_stop_grounding_detail_ruminahui_CAL-03_abc.json",
    )
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )

    assert result.success is True
    assert result.stage == st.STAGE_3A
    assert result.source_type == "poi_anchored_path_projected"
    assert result.source.startswith("poi_anchored_path_projected:CAL-03:")
    assert result.synthetic_confidence == "high"
    assert result.node_id is not None
    assert result.osm_id < 0  # synthetic
    assert result.semantic_spatial_conflict is False

    # node row was inserted with dual-column schema
    assert len(store.nodes) == 1
    n = store.nodes[0]
    assert n["source_type"] == "poi_anchored_path_projected"
    assert n["source"] == result.source
    assert n["poi_osm_id"] == 9876

    # event row was inserted with non-null node_id + stage 3a + no rejection
    assert len(store.events) == 1
    e = store.events[0]
    assert e["node_id"] == result.node_id
    assert e["stage"] == st.STAGE_3A
    assert e["rejected_reason"] is None
    assert e["triggered_by_skill"] == core.SKILL_ID
    assert e["research_output_file"] == anchor.research_output_file

    # review file landed in pending/ (no conflict)
    assert result.review_path is not None
    assert result.review_path.parent.name == "pending"
    text = result.review_path.read_text(encoding="utf-8")
    assert "source_type: poi_anchored_path_projected" in text
    assert f"source:" in text

    # conn.commit() was called
    assert conn.committed >= 1


# ---------------------------------------------------------------------------
# 2. Semantic-spatial conflict: 3a wins but research coords diverge > 80m
# ---------------------------------------------------------------------------


def test_semantic_spatial_conflict_routes_to_conflicts_folder(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    poi = pm.POIMatch(
        osm_id=111, osm_type="node",
        name="Mercado X", centroid=(-0.180, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [poi])

    # research coords ~230m away from (POI on path)
    anchor = core.Anchor(
        name="Mercado X",
        research_coords=(-0.1821, -78.4701),
    )
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )
    assert result.success is True
    assert result.semantic_spatial_conflict is True
    assert result.review_path.parent.name == "semantic_spatial_conflicts"


# ---------------------------------------------------------------------------
# 3. Stage ladder falls through 3a → 3b → 3c → 3d → 4
# ---------------------------------------------------------------------------


def test_falls_through_to_3d_when_earlier_stages_fail(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    # 3a: no POI candidates
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    anchor = core.Anchor(
        name="Calle Sucre y Av. Maldonado",
        road_tokens=("Sucre", "Maldonado"),
        research_coords=(-0.1801, -78.4701),  # ~11m off path → stage 3d high
    )

    # 3b/3c: overpass fetcher returns nothing
    def _empty_fetcher(name, bbox):
        return []

    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
        overpass_fetcher=_empty_fetcher,
    )
    assert result.success is True
    assert result.stage == st.STAGE_3D
    assert result.source_type == "research_coords_path_snapped"
    # synth_confidence = high (≤30m band)
    assert result.synthetic_confidence == "high"


def test_stage_4_fires_when_all_else_fails(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])
    anchor = core.Anchor(name="Lonely Anchor")  # no road tokens, no research coords

    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )
    assert result.success is True
    assert result.stage == st.STAGE_4
    assert result.source_type == "pure_synthesis"
    assert result.synthetic_confidence == "low"


# ---------------------------------------------------------------------------
# 4. Cap enforcement — 3.0 weighted units refuses stage 4
# ---------------------------------------------------------------------------


def test_cap_hit_refuses_stage_4_and_logs_rejection(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    # preload 3 stage-4 events worth 3.0 cap units
    for _ in range(3):
        store.events.append({
            "id": next(store._event_id_counter),
            "created_at": datetime.now(timezone.utc),
            "node_id": str(uuid.uuid4()),
            "route_id": route.route_id,
            "unit": route.unit, "province": route.province,
            "stage": st.STAGE_4,
            "anchor_name": "prev", "research_coords_lat": None,
            "research_coords_lon": None, "final_coords_lat": -0.18,
            "final_coords_lon": -78.47, "match_score": None,
            "rejected_reason": None,
            "triggered_by_skill": core.SKILL_ID,
            "research_output_file": None,
        })

    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    anchor = core.Anchor(name="New Anchor")
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )
    assert result.success is False
    assert result.rejected_reason == "route_synthesis_cap_hit"

    # A rejection event with node_id=None was appended
    rejections = [e for e in store.events if e["rejected_reason"] == "route_synthesis_cap_hit"]
    assert len(rejections) == 1
    assert rejections[0]["node_id"] is None
    # No node row was inserted
    assert store.nodes == []


def test_cap_hit_also_refuses_stage_3d(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    for _ in range(3):
        store.events.append({
            "id": next(store._event_id_counter),
            "created_at": datetime.now(timezone.utc),
            "node_id": str(uuid.uuid4()),
            "route_id": route.route_id,
            "unit": route.unit, "province": route.province,
            "stage": st.STAGE_4,
            "anchor_name": "prev",
            "research_coords_lat": None, "research_coords_lon": None,
            "final_coords_lat": -0.18, "final_coords_lon": -78.47,
            "match_score": None, "rejected_reason": None,
            "triggered_by_skill": core.SKILL_ID,
            "research_output_file": None,
        })
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])
    anchor = core.Anchor(
        name="Anchor", research_coords=(-0.1801, -78.4701),
    )
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )
    assert result.success is False
    assert result.rejected_reason == "route_synthesis_cap_hit"
    # attempted stage was 3d
    assert result.stage == st.STAGE_3D


# ---------------------------------------------------------------------------
# 5. Stage 4 enqueues 06c rerun
# ---------------------------------------------------------------------------


def test_stage_4_success_enqueues_06c_rerun(
    store, conn, review_root, research_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])
    anchor = core.Anchor(name="Lonely Anchor")

    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
        research_queue_root=research_root,
    )
    assert result.success is True
    assert result.stage == st.STAGE_4
    pending_files = list((research_root / "pending").glob("*.md"))
    assert len(pending_files) == 1
    text = pending_files[0].read_text(encoding="utf-8")
    assert "stop_grounding_detail" in text
    assert "stage_4_pure_synthesis_fired" in text


# ---------------------------------------------------------------------------
# 6. Path cache is respected (no Valhalla call if cache fresh)
# ---------------------------------------------------------------------------


def test_uses_cached_path_when_fresh(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    def _boom_valhalla(*a, **kw):
        raise AssertionError("should not call Valhalla with fresh cache")

    import requests as _requests
    monkeypatch.setattr(_requests, "post", _boom_valhalla)

    poi = pm.POIMatch(
        osm_id=1, osm_type="node",
        name="X", centroid=(-0.180, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [poi])

    result = core.path_aware_synthesis(
        core.Anchor(name="X"), route,
        conn=conn, review_root=review_root,
    )
    assert result.success is True


# ---------------------------------------------------------------------------
# 7. All stages fail → logs rejection, no node, no review file
# ---------------------------------------------------------------------------


def test_all_stages_fail_logs_rejection(
    store, conn, review_root, seeded_path, monkeypatch
):
    # Use a very short polyline so stage 4 projection still succeeds.
    # To make stage 4 fail, we need _path_midpoint to return None, which
    # happens only with empty polyline. So instead, we patch
    # stage_4_pure_synthesis to return a failure.
    route = _route()
    seeded_path(route.route_id)

    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    import datamind_console.phases.phase3_routes.synthesis.stages as _st

    def _fail4(ctx, anchor_name, **kw):
        return _st.StageResult(
            stage=_st.STAGE_4, success=False, anchor_name=anchor_name,
            rejected_reason="synthetic_placement_impossible",
        )

    monkeypatch.setattr(core.st, "stage_4_pure_synthesis", _fail4)

    result = core.path_aware_synthesis(
        core.Anchor(name="Nothing Works"), route,
        conn=conn, review_root=review_root,
    )
    assert result.success is False
    assert result.rejected_reason == "all_stages_failed"
    assert store.nodes == []
    rejections = [e for e in store.events if e["rejected_reason"] == "all_stages_failed"]
    assert len(rejections) == 1


# ---------------------------------------------------------------------------
# 8. Dataclass sanity
# ---------------------------------------------------------------------------


def test_anchor_dataclass_defaults():
    a = core.Anchor(name="X")
    assert a.research_coords is None
    assert a.road_tokens == ()
    assert a.research_output_file is None


def test_route_dataclass_defaults():
    r = _route()
    assert r.osm_relation_id is None
    assert r.grounded_stop_coords == ()


# ---------------------------------------------------------------------------
# 9. stage 3b win — named road corridor on path
# ---------------------------------------------------------------------------


def test_stage_3b_wins_when_3a_misses_and_corridor_overlaps(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    # Return a corridor segment ~200m long straddling the polyline
    segment = [(-0.180, -78.472), (-0.180, -78.470), (-0.180, -78.468)]
    monkeypatch.setattr(
        "datamind_console.phases.phase3_routes.synthesis.stages.pi"
        ".polyline_segment_by_road_name",
        lambda polyline, road, route_id, **kw: segment,
    )
    anchor = core.Anchor(name="Av. Maldonado", road_tokens=("Maldonado",))
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )
    assert result.success is True
    assert result.stage == st.STAGE_3B
    assert result.source_type == "path_corridor_projected"
    assert result.synthetic_confidence == "medium"


# ---------------------------------------------------------------------------
# 10. stage 3c win — two-road intersection on path
# ---------------------------------------------------------------------------


def test_stage_3c_wins_when_intersection_is_close_to_path(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])
    monkeypatch.setattr(
        "datamind_console.phases.phase3_routes.synthesis.stages.pi"
        ".find_intersection_along_polyline",
        lambda polyline, a, b, route_id, **kw: (-0.180, -78.470),
    )
    # 3b must be bypassed — stage ladder runs 3c first when 2+ road tokens
    anchor = core.Anchor(
        name="Sucre y Maldonado",
        road_tokens=("Sucre", "Maldonado"),
    )
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )
    assert result.success is True
    assert result.stage == st.STAGE_3C
    assert result.source_type == "path_intersection"


# ---------------------------------------------------------------------------
# 11. Cap boundary: 2.5 consumed (5× 3d + 0× 4) → 3d passes, 4 blocked
# ---------------------------------------------------------------------------


def test_cap_boundary_3d_passes_stage_4_blocked(
    store, conn, review_root, research_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    # 5× stage 3d = 2.5 weighted units (below 3.0 cap)
    for _ in range(5):
        store.events.append({
            "id": next(store._event_id_counter),
            "created_at": datetime.now(timezone.utc),
            "node_id": str(uuid.uuid4()),
            "route_id": route.route_id,
            "unit": route.unit, "province": route.province,
            "stage": st.STAGE_3D,
            "anchor_name": "prev",
            "research_coords_lat": -0.180, "research_coords_lon": -78.47,
            "final_coords_lat": -0.18, "final_coords_lon": -78.47,
            "match_score": None, "rejected_reason": None,
            "triggered_by_skill": core.SKILL_ID,
            "research_output_file": None,
        })

    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    # 3d succeeds (coords ~11m off path) at consumed=2.5 → tips to 3.0
    r3d = core.path_aware_synthesis(
        core.Anchor(
            name="Anchor A", research_coords=(-0.1801, -78.4701),
        ),
        route, conn=conn, review_root=review_root,
        research_queue_root=research_root,
    )
    assert r3d.success is True
    assert r3d.stage == st.STAGE_3D

    # Now at 3.0 consumed — a new stage-4 attempt must be blocked
    r4 = core.path_aware_synthesis(
        core.Anchor(name="Anchor B"),
        route, conn=conn, review_root=review_root,
        research_queue_root=research_root,
    )
    assert r4.success is False
    assert r4.rejected_reason == "route_synthesis_cap_hit"
    assert r4.stage == st.STAGE_4


# ---------------------------------------------------------------------------
# 12. Unit-weekly cap triggers enqueue not DB insert
# ---------------------------------------------------------------------------


def test_unit_weekly_cap_blocks_stage_4_and_enqueues_rerun(
    store, conn, review_root, research_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    # Seed UNIT_WEEKLY_CAP events on this unit within the past 7 days,
    # all on a *different* route so route cap is not tripped
    other_route_id = str(uuid.uuid4())
    for _ in range(core.UNIT_WEEKLY_CAP):
        store.events.append({
            "id": next(store._event_id_counter),
            "created_at": datetime.now(timezone.utc),
            "node_id": str(uuid.uuid4()),
            "route_id": other_route_id,
            "unit": route.unit, "province": route.province,
            "stage": st.STAGE_3A,  # weight 0 → route cap not affected
            "anchor_name": "prev",
            "research_coords_lat": None, "research_coords_lon": None,
            "final_coords_lat": -0.18, "final_coords_lon": -78.47,
            "match_score": None, "rejected_reason": None,
            "triggered_by_skill": core.SKILL_ID,
            "research_output_file": None,
        })

    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    result = core.path_aware_synthesis(
        core.Anchor(name="Anchor X"),
        route, conn=conn, review_root=review_root,
        research_queue_root=research_root,
    )
    assert result.success is False
    assert result.rejected_reason == "unit_weekly_cap_hit"
    # No node row inserted
    assert store.nodes == []
    # Rerun prompt was enqueued with priority_bump
    pending = list((research_root / "pending").glob("*.md"))
    assert len(pending) == 1
    text = pending[0].read_text(encoding="utf-8")
    assert "priority_bump: true" in text
    assert "unit_weekly_cap_hit" in text


# ---------------------------------------------------------------------------
# 13. Transactional rollback when synthesis_events insert fails
# ---------------------------------------------------------------------------


def test_persist_rolls_back_on_synthesis_events_insert_failure(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    poi = pm.POIMatch(
        osm_id=42, osm_type="node",
        name="X", centroid=(-0.180, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [poi])

    # Track rollback
    conn.rolled_back = 0

    def _rollback():
        conn.rolled_back += 1
        # Reverse the node insert to simulate a real DB rollback
        store.nodes.clear()

    conn.rollback = _rollback

    # Make log_synthesis_event raise after the node insert has happened
    def _boom(*a, **kw):
        raise RuntimeError("events insert exploded")

    monkeypatch.setattr(
        "datamind_console.phases.phase3_routes.synthesis.core.se.log_synthesis_event",
        _boom,
    )

    with pytest.raises(RuntimeError, match="events insert exploded"):
        core.path_aware_synthesis(
            core.Anchor(name="X"),
            route, conn=conn, review_root=review_root,
        )
    assert conn.rolled_back == 1
    # node insert was rolled back
    assert store.nodes == []
    # no commit
    assert conn.committed == 0


# ---------------------------------------------------------------------------
# 14. Preconditions: research_coords with <4 decimals returns success=False
# ---------------------------------------------------------------------------


def test_low_precision_research_coords_fails_without_db_writes(
    store, conn, review_root, seeded_path, monkeypatch
):
    route = _route()
    # Note: we do NOT seed path — the precision check must fail before
    # _ensure_path is even called.
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    # Only 2 decimals → fails precondition
    anchor = core.Anchor(
        name="Imprecise",
        research_coords=(-0.18, -78.47),
    )
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=review_root,
    )
    assert result.success is False
    assert result.rejected_reason == "research_coords_low_precision"
    # No DB writes of any kind
    assert store.nodes == []
    assert store.events == []
    assert conn.committed == 0


# ---------------------------------------------------------------------------
# 15. Route-cap 06c rerun enqueue fires and carries priority_bump flag
# ---------------------------------------------------------------------------


def test_route_cap_hit_enqueues_rerun_with_priority_bump(
    store, conn, review_root, research_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)

    # 3× stage_4 → 3.0 consumed → cap hit
    for _ in range(3):
        store.events.append({
            "id": next(store._event_id_counter),
            "created_at": datetime.now(timezone.utc),
            "node_id": str(uuid.uuid4()),
            "route_id": route.route_id,
            "unit": route.unit, "province": route.province,
            "stage": st.STAGE_4, "anchor_name": "prev",
            "research_coords_lat": None, "research_coords_lon": None,
            "final_coords_lat": -0.18, "final_coords_lon": -78.47,
            "match_score": None, "rejected_reason": None,
            "triggered_by_skill": core.SKILL_ID,
            "research_output_file": None,
        })
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    result = core.path_aware_synthesis(
        core.Anchor(name="New"),
        route, conn=conn, review_root=review_root,
        research_queue_root=research_root,
    )
    assert result.success is False
    pending = list((research_root / "pending").glob("*.md"))
    assert len(pending) == 1
    text = pending[0].read_text(encoding="utf-8")
    assert "priority_bump: true" in text
    # priority bump moves the file to priority 00 (filename prefix)
    assert pending[0].name.startswith("00_")


# ---------------------------------------------------------------------------
# 16. Stage 3a success does NOT enqueue 06c rerun
# ---------------------------------------------------------------------------


def test_stage_3a_success_does_not_enqueue_rerun(
    store, conn, review_root, research_root, seeded_path, monkeypatch
):
    route = _route()
    seeded_path(route.route_id)
    poi = pm.POIMatch(
        osm_id=1, osm_type="node",
        name="X", centroid=(-0.180, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [poi])

    result = core.path_aware_synthesis(
        core.Anchor(name="X"), route,
        conn=conn, review_root=review_root,
        research_queue_root=research_root,
    )
    assert result.success is True
    assert result.stage == st.STAGE_3A
    pending = list((research_root / "pending").glob("*.md"))
    assert pending == []


# ---------------------------------------------------------------------------
# 17. Valhalla timeout propagates out of path_aware_synthesis
# ---------------------------------------------------------------------------


def test_valhalla_timeout_propagates(
    store, conn, review_root, monkeypatch
):
    route = _route()
    # No seeded path, and no osm_relation_id → compute_route_path will try
    # Valhalla. Force it to raise.

    def _boom(*a, **kw):
        raise pi.PathInferenceError("valhalla timeout after 30s")

    monkeypatch.setattr(
        "datamind_console.phases.phase3_routes.synthesis.core.pi.compute_route_path",
        _boom,
    )

    with pytest.raises(pi.PathInferenceError, match="timeout"):
        core.path_aware_synthesis(
            core.Anchor(name="X"),
            route, conn=conn, review_root=review_root,
        )
    # No partial DB state
    assert store.nodes == []
    assert store.events == []
