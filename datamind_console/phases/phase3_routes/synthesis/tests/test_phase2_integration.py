"""
Phase 2 end-to-end integration test.

Three scenarios that exercise the full Phase 1 + Phase 2 surface with
stubbed Valhalla/Overpass:

1. Stage 3a happy path — POI-anchored synthesis writes node row, audit
   event, review file, and (implicit) path cache.
2. Cascade — 3a fails (no POI), 3b fails (no corridor), 3c fails, 3d
   succeeds at 30–80m band → low confidence, review in pending/.
3. osm_route_fill on a long gap → two synthetics land with
   stage=osm_route_fill, never stage 4.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
import requests

from datamind_console.phases.phase3_routes.synthesis import (
    core,
    osm_route_fill as orf,
    path_inference as pi,
    poi_matcher as pm,
    stages as st,
)


# ---------------------------------------------------------------------------
# richer fake conn that handles all modules' SQL
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self):
        self.routes: dict[str, dict] = {}
        self.osm_relations: dict[int, str] = {}
        self.nodes: list[dict] = []
        self.events: list[dict] = []
        self._osm_seq = itertools.count(-1_000_000, -1)
        self._event_seq = itertools.count(1)


class _Cursor:
    def __init__(self, store: _Store):
        self._store = store
        self._result: list[Any] = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql: str, params=None):
        params = tuple(params or ())
        up = " ".join(sql.split()).upper()

        if up.startswith("SELECT NEXTVAL"):
            self._result = [(next(self._store._osm_seq),)]
            return
        if up.startswith("SELECT ENCODED_POLYLINE FROM ROUTE_RAW.OSM_RELATIONS"):
            osm_id = params[0]
            poly = self._store.osm_relations.get(osm_id)
            self._result = [(poly,)] if poly is not None else []
            return
        if up.startswith("SELECT INFERRED_PATH_POLYLINE"):
            rid = params[0]
            row = self._store.routes.get(rid)
            self._result = (
                [(row.get("polyline"), row.get("source"), row.get("computed_at"))]
                if row else []
            )
            return
        if up.startswith("UPDATE ROUTE_PROD.ROUTES SET"):
            if len(params) == 1:
                (rid,) = params
                self._store.routes[rid] = {"polyline": None, "source": None, "computed_at": None}
                return
            poly, source, computed_at, rid = params
            self._store.routes[rid] = {
                "polyline": poly, "source": source, "computed_at": computed_at,
            }
            return
        if up.startswith("INSERT INTO NODE_PROD.NODES"):
            # two shapes: core 13-col or osm_route_fill 12-col. Differentiate by len.
            if len(params) == 13:
                keys = [
                    "node_id", "osm_id", "lat", "lon",
                    "source", "source_type", "synthetic_confidence",
                    "route_id", "anchor_name",
                    "poi_osm_id",
                    "poi_to_path_distance_m", "path_projection_distance_m",
                    "research_to_projection_distance_m",
                ]
            else:
                keys = [
                    "node_id", "osm_id", "lat", "lon",
                    "source", "source_type", "synthetic_confidence",
                    "route_id", "anchor_name",
                    "poi_osm_id",
                    "poi_to_path_distance_m", "osm_route_fill_context",
                ]
            self._store.nodes.append(dict(zip(keys, params)))
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
                "id": next(self._store._event_seq),
                "created_at": datetime.now(timezone.utc),
                "node_id": node_id, "route_id": route_id,
                "unit": unit, "province": province,
                "stage": stage, "anchor_name": anchor_name,
                "research_coords_lat": rc_lat, "research_coords_lon": rc_lon,
                "final_coords_lat": fc_lat, "final_coords_lon": fc_lon,
                "match_score": match_score, "rejected_reason": rejected_reason,
                "triggered_by_skill": triggered_by_skill,
                "research_output_file": research_output_file,
            }
            self._store.events.append(row)
            self._result = [row]
            return
        if "COUNT(*)" in up and "SYNTHESIS_EVENTS" in up:
            route_id, stage = params
            n = sum(
                1 for e in self._store.events
                if e["route_id"] == route_id
                and e["stage"] == stage
                and e["rejected_reason"] is None
            )
            self._result = [{"n": n}]
            return
        raise AssertionError(f"unexpected SQL: {sql!r}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class _Conn:
    def __init__(self, store: _Store):
        self._store = store
        self.committed = 0

    def cursor(self, *a, **kw):
        return _Cursor(self._store)

    def commit(self):
        self.committed += 1


@pytest.fixture()
def workspace(tmp_path: Path):
    review_root = tmp_path / "synthetic_review"
    for sub in ("pending", "verified", "rejected", "semantic_spatial_conflicts"):
        (review_root / sub).mkdir(parents=True, exist_ok=True)

    research_root = tmp_path / "research_queue"
    for sub in ("pending", "sent", "responses", "ingested", "archive"):
        (research_root / sub).mkdir(parents=True, exist_ok=True)
    return {"review": review_root, "research": research_root}


@pytest.fixture()
def store():
    return _Store()


@pytest.fixture()
def conn(store):
    return _Conn(store)


# ---------------------------------------------------------------------------
# scenario 1 — stage 3a happy path across Phase 1 + Phase 2 modules
# ---------------------------------------------------------------------------


def test_phase2_stage_3a_end_to_end(store, conn, workspace, monkeypatch):
    # preload path cache
    route_id = str(uuid.uuid4())
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    store.routes[route_id] = {
        "polyline": poly,
        "source": pi.PATH_SOURCE_VALHALLA_TERMINUS,
        "computed_at": datetime.now(timezone.utc),
    }

    # stub Overpass via monkeypatch on pm.query_pois_by_name
    poi = pm.POIMatch(
        osm_id=42, osm_type="node",
        name="Mercado Central de Sangolquí",
        centroid=(-0.180, -78.470),
        tags={"name": "Mercado Central de Sangolquí", "amenity": "marketplace"},
        class_name="marketplace", class_priority=100,
    )
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [poi])

    route = core.Route(
        route_id=route_id, route_code="CAL-03",
        unit="ruminahui", province="sample_region",
        termini=((-0.180, -78.475), (-0.180, -78.465)),
    )
    anchor = core.Anchor(
        name="Mercado Central de Sangolqui",  # accent-less variant
        research_output_file="01_stop_grounding_detail_ruminahui_CAL-03_abc.json",
    )
    result = core.path_aware_synthesis(
        anchor, route, conn=conn, review_root=workspace["review"],
    )

    assert result.success is True
    assert result.stage == st.STAGE_3A
    assert result.source_type == "poi_anchored_path_projected"
    assert result.synthetic_confidence == "high"

    # Phase 1 persistence: single node + single event
    assert len(store.nodes) == 1
    assert store.nodes[0]["source_type"] == "poi_anchored_path_projected"
    assert store.nodes[0]["poi_osm_id"] == 42

    assert len(store.events) == 1
    assert store.events[0]["stage"] == st.STAGE_3A
    assert store.events[0]["rejected_reason"] is None

    # review file
    assert result.review_path is not None
    text = result.review_path.read_text(encoding="utf-8")
    assert "source_type: poi_anchored_path_projected" in text
    # research_output_file basename stored
    assert "01_stop_grounding_detail_ruminahui_CAL-03_abc.json" in text


# ---------------------------------------------------------------------------
# scenario 2 — cascade 3a fail → 3b fail → 3c fail → 3d succeeds at low band
# ---------------------------------------------------------------------------


def test_phase2_cascade_to_stage_3d_low_band(store, conn, workspace, monkeypatch):
    route_id = str(uuid.uuid4())
    poly = pi.encode_polyline([(-0.180, -78.475), (-0.180, -78.465)])
    store.routes[route_id] = {
        "polyline": poly,
        "source": pi.PATH_SOURCE_VALHALLA_TERMINUS,
        "computed_at": datetime.now(timezone.utc),
    }

    # 3a: empty POI candidates
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    # 3b/3c: Overpass fetcher returns nothing
    def _empty_fetcher(name, bbox):
        return []

    route = core.Route(
        route_id=route_id, route_code="CAL-03",
        unit="ruminahui", province="sample_region",
        termini=((-0.180, -78.475), (-0.180, -78.465)),
    )
    # research coords ~55m south of path (in the 30–80 m band → low confidence)
    anchor = core.Anchor(
        name="Y de Calsig",
        road_tokens=("Sucre", "Maldonado"),
        research_coords=(-0.1805, -78.4701),
    )
    result = core.path_aware_synthesis(
        anchor, route, conn=conn,
        review_root=workspace["review"],
        overpass_fetcher=_empty_fetcher,
    )
    assert result.success is True
    assert result.stage == st.STAGE_3D
    assert result.source_type == "research_coords_path_snapped"
    assert result.synthetic_confidence == "low"

    # event trail
    assert len(store.events) == 1
    assert store.events[0]["stage"] == st.STAGE_3D

    # review landed in pending/ (low band still pending, no semantic conflict
    # because 3d is the chosen stage)
    assert result.review_path.parent.name == "pending"


# ---------------------------------------------------------------------------
# scenario 3 — osm_route_fill fills a long gap with POI-anchored synthetics
# ---------------------------------------------------------------------------


def test_phase2_osm_route_fill_long_gap(store, conn, workspace, monkeypatch):
    # 2.2km polyline, stops at both ends → single big gap
    pts = [(-0.180 + i * 0.0005, -78.470) for i in range(41)]
    poly = pi.encode_polyline(pts)
    stop_coords = [pts[0], pts[-1]]

    # two POIs spaced ~500m apart within the gap (both on path)
    poi_a = pm.POIMatch(
        osm_id=501, osm_type="node", name="Escuela Rural La Esperanza",
        centroid=pts[10],
        tags={}, class_name="school", class_priority=85,
    )
    poi_b = pm.POIMatch(
        osm_id=502, osm_type="node", name="Mercado San Juan",
        centroid=pts[30],
        tags={}, class_name="marketplace", class_priority=100,
    )

    def _query(name, bbox, **kw):
        south, west, north, east = bbox
        r = []
        for p in (poi_a, poi_b):
            if south <= p.centroid[0] <= north and west <= p.centroid[1] <= east:
                r.append(p)
        return r

    monkeypatch.setattr(pm, "query_pois_by_name", _query)

    route_id = str(uuid.uuid4())
    report = orf.fill_osm_route_gaps(
        conn=conn,
        route_id=route_id,
        route_code="RUR-01",
        unit="ruminahui",
        province="sample_region",
        osm_relation_id=999888,
        polyline=poly,
        stop_coords=stop_coords,
        termini=(pts[0], pts[-1]),
        operator_type="urbano",
        review_root=workspace["review"],
    )

    # one gap, two synthetics (cap = 2.2km // 400 = 5, so both fit)
    assert len(report.gaps) == 1
    assert len(report.filled) == 2

    # both events have stage=osm_route_fill
    fill_events = [e for e in store.events if e["stage"] == orf.STAGE_OSM_ROUTE_FILL]
    assert len(fill_events) == 2

    # no pure_synthesis ever emitted by this pipeline
    assert not any(e["stage"] == "4_pure_synthesis" for e in store.events)

    # node rows use source_type=poi_anchored_path_projected with
    # source="osm_route_fill:<relation_id>:<gap_idx>"
    assert len(store.nodes) == 2
    for n in store.nodes:
        assert n["source_type"] == "poi_anchored_path_projected"
        assert n["source"].startswith("osm_route_fill:999888:")
        assert n["synthetic_confidence"] == "medium"

    # review files use stage=osm_route_fill slot
    for fs in report.filled:
        assert fs.review_path is not None
        assert fs.review_path.name.startswith("03_osm_route_fill_")
