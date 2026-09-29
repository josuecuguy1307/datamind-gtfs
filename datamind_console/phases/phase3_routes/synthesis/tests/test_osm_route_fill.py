"""
Unit tests for datamind_console.phases.phase3_routes.synthesis.osm_route_fill.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from datamind_console.phases.phase3_routes.synthesis import (
    osm_route_fill as orf,
    path_inference as pi,
    poi_matcher as pm,
)


# ---------------------------------------------------------------------------
# fake conn
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self):
        self.nodes: list[dict] = []
        self.events: list[dict] = []
        self._osm_seq = itertools.count(-1_000_000, -1)
        self._event_seq = itertools.count(1)


class _Cursor:
    def __init__(self, store: _Store):
        self._store = store
        self._result: list[Any] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql: str, params=None):
        params = tuple(params or ())
        up = " ".join(sql.split()).upper()
        if up.startswith("SELECT NEXTVAL"):
            self._result = [(next(self._store._osm_seq),)]
            return
        if up.startswith("INSERT INTO NODE_PROD.NODES"):
            (
                node_id, osm_id, lat, lon,
                source, source_type, conf,
                route_id, anchor_name,
                poi_osm_id, poi_path_dist, context,
            ) = params
            self._store.nodes.append({
                "node_id": node_id, "osm_id": osm_id,
                "lat": lat, "lon": lon,
                "source": source, "source_type": source_type,
                "synthetic_confidence": conf,
                "route_id": route_id, "anchor_name": anchor_name,
                "poi_osm_id": poi_osm_id,
                "poi_to_path_distance_m": poi_path_dist,
                "osm_route_fill_context": context,
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
def store():
    return _Store()


@pytest.fixture()
def conn(store):
    return _Conn(store)


@pytest.fixture()
def review_root(tmp_path: Path) -> Path:
    root = tmp_path / "synthetic_review"
    for sub in ("pending", "verified", "rejected", "semantic_spatial_conflicts"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(autouse=True)
def _stub_treater_insert(store, monkeypatch):
    """Bypass the universal treater for unit tests of gap-detection /
    POI-selection logic. The treater is exercised by the live-DB
    integration tests in tests/phase3_routes/services/stop_quality/;
    here we just record the insert call shape on `store.nodes`."""
    def _stub(_conn, *, osm_id, lat, lon, source, source_type,
              poi_osm_id, poi_to_path_distance_m, osm_route_fill_context):
        new_id = str(uuid.uuid4())
        store.nodes.append({
            "node_id": new_id, "osm_id": osm_id,
            "lat": lat, "lon": lon,
            "source": source, "source_type": source_type,
            "synthetic_confidence": "medium",
            "poi_osm_id": poi_osm_id,
            "poi_to_path_distance_m": poi_to_path_distance_m,
            "osm_route_fill_context": osm_route_fill_context,
        })
        return new_id

    monkeypatch.setattr(orf, "_insert_osm_fill_node", _stub)


# ---------------------------------------------------------------------------
# gap detection
# ---------------------------------------------------------------------------


def test_detect_gaps_flags_large_inter_stop_spacing():
    # polyline: 2km straight line
    pts = [(-0.180 + i * 0.0005, -78.470) for i in range(41)]  # ~55m per step, 41 pts, ~2.2km
    poly = pi.encode_polyline(pts)

    # two stops: the termini → entire 2.2km is one gap
    gaps = orf.detect_gaps(
        poly, [pts[0], pts[-1]], gap_threshold_m=600.0,
    )
    assert len(gaps) == 1
    assert gaps[0].length_m > 600.0


def test_detect_gaps_returns_empty_when_stops_close_together():
    pts = [(-0.180 + i * 0.0005, -78.470) for i in range(41)]
    poly = pi.encode_polyline(pts)
    # every 5 vertices (~275m) — under the urban 600m threshold
    stops = pts[::5]
    gaps = orf.detect_gaps(poly, stops, gap_threshold_m=600.0)
    assert gaps == []


def test_rural_routes_get_relaxed_threshold():
    # operator_type mentions "interparroquial"
    t1 = (-0.180, -78.470)
    t2 = (-0.185, -78.470)  # ~550m — short route
    assert orf.is_rural_route(operator_type="interparroquial XYZ", termini=[t1, t2]) is True
    # long-span route even without the operator type
    t3 = (-0.180, -78.470)
    t4 = (-0.180, -78.270)  # ~22km east
    assert orf.is_rural_route(operator_type=None, termini=[t3, t4]) is True
    # ordinary urban route
    assert orf.is_rural_route(operator_type="urbano", termini=[t1, t2]) is False


def test_gap_threshold_for_urban_vs_rural():
    urban = orf.gap_threshold_for(operator_type="urbano", termini=[(-0.18, -78.47), (-0.185, -78.47)])
    rural = orf.gap_threshold_for(operator_type="interparroquial", termini=[(-0.18, -78.47), (-0.185, -78.47)])
    assert urban == 600.0
    assert rural == 1200.0


# ---------------------------------------------------------------------------
# gap cap — 1 synthetic per 400 m
# ---------------------------------------------------------------------------


def test_gap_cap_matches_spec():
    g_400 = orf.Gap(idx=0, start_coords=(0,0), end_coords=(0,0),
                   length_m=400.0, start_index=0, end_index=1)
    g_800 = orf.Gap(idx=0, start_coords=(0,0), end_coords=(0,0),
                   length_m=800.0, start_index=0, end_index=1)
    g_1500 = orf.Gap(idx=0, start_coords=(0,0), end_coords=(0,0),
                    length_m=1500.0, start_index=0, end_index=1)
    assert orf._gap_cap(g_400) == 1
    assert orf._gap_cap(g_800) == 2
    assert orf._gap_cap(g_1500) == 3


# ---------------------------------------------------------------------------
# end-to-end fill with stubbed Overpass
# ---------------------------------------------------------------------------


def _long_polyline():
    pts = [(-0.180 + i * 0.0005, -78.470) for i in range(41)]  # ~2.2km
    return pts, pi.encode_polyline(pts)


def test_fill_osm_route_gaps_happy_path(
    store, conn, review_root, monkeypatch
):
    pts, poly = _long_polyline()
    stop_coords = [pts[0], pts[-1]]  # 1 big gap

    # POI near the middle of the gap
    poi_mid = pm.POIMatch(
        osm_id=5555, osm_type="node",
        name="Escuela Rural", centroid=(-0.180 + 20 * 0.0005, -78.470),
        tags={}, class_name="school", class_priority=85,
    )

    def _fake_query(name, bbox, **kw):
        return [poi_mid]

    monkeypatch.setattr(pm, "query_pois_by_name", _fake_query)

    route_id = str(uuid.uuid4())
    report = orf.fill_osm_route_gaps(
        conn=conn, route_id=route_id, route_code="RUR-01",
        unit="ruminahui", province="sample_region",
        osm_relation_id=222333,
        polyline=poly,
        stop_coords=stop_coords,
        termini=(pts[0], pts[-1]),
        operator_type="urbano",
        review_root=review_root,
    )
    assert len(report.gaps) == 1
    assert len(report.filled) >= 1
    # no more than cap (1 per 400m; 2.2km gap → 5 max)
    assert len(report.filled) <= orf._gap_cap(report.gaps[0])

    # synthetic node inserted
    assert store.nodes
    n = store.nodes[0]
    assert n["source_type"] == orf.SOURCE_TYPE_POI
    assert n["source"].startswith("osm_route_fill:222333:")
    assert n["synthetic_confidence"] == "medium"
    assert n["osm_id"] < 0
    assert n["osm_route_fill_context"].startswith("relation_222333_gap_")

    # event logged with stage=osm_route_fill
    assert any(e["stage"] == orf.STAGE_OSM_ROUTE_FILL for e in store.events)

    # review file exists
    assert report.filled[0].review_path is not None
    assert report.filled[0].review_path.parent.name == "pending"
    text = report.filled[0].review_path.read_text(encoding="utf-8")
    assert "osm_route_fill" in text


def test_fill_osm_route_gaps_no_poi_stays_sparse(
    store, conn, review_root, monkeypatch
):
    pts, poly = _long_polyline()
    stop_coords = [pts[0], pts[-1]]

    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [])

    route_id = str(uuid.uuid4())
    report = orf.fill_osm_route_gaps(
        conn=conn, route_id=route_id, route_code="RUR-01",
        unit="ruminahui", province="sample_region",
        osm_relation_id=222333, polyline=poly, stop_coords=stop_coords,
        termini=(pts[0], pts[-1]),
        operator_type="urbano",
        review_root=review_root,
    )
    assert len(report.gaps) == 1
    assert report.filled == []
    assert report.skipped and report.skipped[0]["reason"] == "no_poi_evidence"
    # no node, no event
    assert store.nodes == []
    assert store.events == []


def test_fill_osm_route_gaps_enforces_200m_separation(
    store, conn, review_root, monkeypatch
):
    pts, poly = _long_polyline()
    stop_coords = [pts[0], pts[-1]]

    # 3 POIs all clustered within ~100m of each other → only one can survive
    pois = [
        pm.POIMatch(
            osm_id=1, osm_type="node", name="A",
            centroid=(-0.180 + 20 * 0.0005, -78.470),
            tags={}, class_name="marketplace", class_priority=100,
        ),
        pm.POIMatch(
            osm_id=2, osm_type="node", name="B",
            centroid=(-0.180 + 21 * 0.0005, -78.470),  # ~55m later
            tags={}, class_name="school", class_priority=85,
        ),
        pm.POIMatch(
            osm_id=3, osm_type="node", name="C",
            centroid=(-0.180 + 22 * 0.0005, -78.470),  # ~110m after first
            tags={}, class_name="hospital", class_priority=65,
        ),
    ]
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: pois)

    report = orf.fill_osm_route_gaps(
        conn=conn, route_id=str(uuid.uuid4()), route_code="RUR-01",
        unit="ruminahui", province="sample_region",
        osm_relation_id=1, polyline=poly, stop_coords=stop_coords,
        termini=(pts[0], pts[-1]),
        operator_type="urbano",
        review_root=review_root,
    )
    # all three clustered within 200m → only highest-priority survives
    assert len(report.filled) == 1
    assert report.filled[0].poi.osm_id == 1  # marketplace (prio 100) wins


def test_fill_osm_route_gaps_rural_threshold_skips_short_gap(
    store, conn, review_root, monkeypatch
):
    # 800m gap — exceeds urban 600m but is under rural 1200m
    pts = [(-0.180 + i * 0.0005, -78.470) for i in range(16)]  # ~825m
    poly = pi.encode_polyline(pts)
    stop_coords = [pts[0], pts[-1]]

    def _fake_query(name, bbox, **kw):
        return []

    monkeypatch.setattr(pm, "query_pois_by_name", _fake_query)

    report = orf.fill_osm_route_gaps(
        conn=conn, route_id=str(uuid.uuid4()), route_code="RUR-01",
        unit="ruminahui", province="sample_region",
        osm_relation_id=1, polyline=poly, stop_coords=stop_coords,
        termini=(pts[0], pts[-1]),
        operator_type="interparroquial",  # rural → threshold = 1200m
        review_root=review_root,
    )
    # 825m gap < 1200m rural threshold → no gap detected
    assert report.gaps == []
    assert report.filled == []


def test_fill_osm_route_gaps_multiple_gaps_fills_each(
    store, conn, review_root, monkeypatch
):
    # Two gaps: 0..20 (~1.1km) and 20..40 (~1.1km) with a stop mid-route
    pts = [(-0.180 + i * 0.0005, -78.470) for i in range(41)]
    poly = pi.encode_polyline(pts)
    stop_coords = [pts[0], pts[20], pts[-1]]

    # two POIs, one in each gap
    poi1 = pm.POIMatch(
        osm_id=101, osm_type="node", name="School1",
        centroid=(-0.180 + 10 * 0.0005, -78.470),
        tags={}, class_name="school", class_priority=85,
    )
    poi2 = pm.POIMatch(
        osm_id=102, osm_type="node", name="Market2",
        centroid=(-0.180 + 30 * 0.0005, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )

    # overpass returns both each time; gap bbox filter keeps only the relevant one
    # per-gap check requires the POI to be near the gap's polyline section, which
    # our filter_candidates does implicitly. Our helper projects all POIs to path —
    # both would survive the 80m ceiling. But we need to prove each gap gets its
    # own synth; since separation is along-path (>200m), both end up picked overall.
    def _query(name, bbox, **kw):
        # scope: return POIs within that bbox (simple lat filter)
        south, west, north, east = bbox
        result = []
        for p in (poi1, poi2):
            if south <= p.centroid[0] <= north and west <= p.centroid[1] <= east:
                result.append(p)
        return result

    monkeypatch.setattr(pm, "query_pois_by_name", _query)

    report = orf.fill_osm_route_gaps(
        conn=conn, route_id=str(uuid.uuid4()), route_code="RUR-01",
        unit="ruminahui", province="sample_region",
        osm_relation_id=777, polyline=poly, stop_coords=stop_coords,
        termini=(pts[0], pts[-1]),
        operator_type="urbano",
        review_root=review_root,
    )
    assert len(report.gaps) == 2
    # one fill in each gap
    gap_idxs_filled = sorted({f.gap_idx for f in report.filled})
    assert gap_idxs_filled == [0, 1]


# ---------------------------------------------------------------------------
# no stage 4 ever — this pipeline must never emit pure_synthesis
# ---------------------------------------------------------------------------


def test_stage_is_always_osm_route_fill_never_pure_synthesis(
    store, conn, review_root, monkeypatch
):
    pts, poly = _long_polyline()
    stop_coords = [pts[0], pts[-1]]
    poi = pm.POIMatch(
        osm_id=1, osm_type="node", name="Name",
        centroid=(-0.180 + 20 * 0.0005, -78.470),
        tags={}, class_name="marketplace", class_priority=100,
    )
    monkeypatch.setattr(pm, "query_pois_by_name", lambda *a, **kw: [poi])

    orf.fill_osm_route_gaps(
        conn=conn, route_id=str(uuid.uuid4()), route_code="RUR-01",
        unit="ruminahui", province="sample_region",
        osm_relation_id=1, polyline=poly, stop_coords=stop_coords,
        termini=(pts[0], pts[-1]),
        operator_type="urbano",
        review_root=review_root,
    )
    for e in store.events:
        assert e["stage"] == orf.STAGE_OSM_ROUTE_FILL
        assert e["stage"] != "4_pure_synthesis"
    for n in store.nodes:
        assert n["source_type"] == orf.SOURCE_TYPE_POI
        assert n["source_type"] != "pure_synthesis"


# ---------------------------------------------------------------------------
# detect_gaps edge cases
# ---------------------------------------------------------------------------


def test_detect_gaps_zero_stops():
    poly = pi.encode_polyline([(-0.18, -78.47), (-0.181, -78.471)])
    assert orf.detect_gaps(poly, []) == []


def test_detect_gaps_one_stop():
    poly = pi.encode_polyline([(-0.18, -78.47), (-0.181, -78.471)])
    assert orf.detect_gaps(poly, [(-0.18, -78.47)]) == []
