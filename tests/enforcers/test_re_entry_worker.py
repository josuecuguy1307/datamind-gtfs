"""Unit tests for hades.enforcers.re_entry_worker.

These cover the pure helpers (fix-category inference, LineString /
stop-payload serialisation, geometry-JSON parsing) plus a small
integration check for :func:`deadline_monitor` that runs against the
live local DB. The full state-machine matrix (H.1-H.4) lives in C8's
end-to-end suite.
"""
from __future__ import annotations

import json
import os
import pytest

from hades.enforcers import re_entry_worker as worker
from hades.enforcers.re_entry_worker import (
    DEADLINE,
    MAX_ATTEMPTS,
    _extract_linestring,
    _fix_category,
    _linestring_to_geojson,
    _proposed_stops_payload,
)


# ---------------------------------------------------------------------------
# _fix_category — maps (coords_changed, stops_changed) → category.
# ---------------------------------------------------------------------------

def test_fix_category_geometry_only():
    assert _fix_category(True, False) == "geometry"


def test_fix_category_stop_coverage_only():
    assert _fix_category(False, True) == "stop_coverage"


def test_fix_category_structural_when_both_changed():
    assert _fix_category(True, True) == "structural"


def test_fix_category_returns_none_when_nothing_changed():
    assert _fix_category(False, False) is None


# ---------------------------------------------------------------------------
# _linestring_to_geojson — coords come in as (lon, lat).
# ---------------------------------------------------------------------------

def test_linestring_to_geojson_preserves_lon_lat_order():
    coords = [(-78.5, -0.2), (-78.48, -0.18)]
    payload = _linestring_to_geojson(coords)
    assert payload["type"] == "LineString"
    assert payload["coordinates"] == [[-78.5, -0.2], [-78.48, -0.18]]


def test_linestring_to_geojson_is_json_serialisable():
    payload = _linestring_to_geojson([(-78.5, -0.2), (-78.48, -0.18)])
    # Must round-trip through json without custom encoders.
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# _proposed_stops_payload — lat/lon + stop_id.
# ---------------------------------------------------------------------------

def test_proposed_stops_payload_aligns_ids_with_stops():
    stops = [(-0.2, -78.5), (-0.18, -78.48)]
    ids = ["S1", "S2"]
    payload = _proposed_stops_payload(stops, ids)
    assert payload == [
        {"stop_id": "S1", "lat": -0.2, "lon": -78.5},
        {"stop_id": "S2", "lat": -0.18, "lon": -78.48},
    ]


def test_proposed_stops_payload_synthesises_ids_when_short():
    stops = [(-0.2, -78.5), (-0.18, -78.48)]
    ids = ["S1"]  # Fewer ids than stops (can happen for synthetic fills).
    payload = _proposed_stops_payload(stops, ids)
    assert payload[0]["stop_id"] == "S1"
    assert payload[1]["stop_id"] == "synthesized_1"


# ---------------------------------------------------------------------------
# _extract_linestring — parses LineString, flattens MultiLineString.
# ---------------------------------------------------------------------------

def test_extract_linestring_parses_linestring():
    geom_json = json.dumps(
        {"type": "LineString", "coordinates": [[-78.5, -0.2], [-78.48, -0.18]]}
    )
    coords = _extract_linestring(geom_json)
    assert coords == [(-78.5, -0.2), (-78.48, -0.18)]


def test_extract_linestring_flattens_multilinestring():
    geom_json = json.dumps(
        {
            "type": "MultiLineString",
            "coordinates": [
                [[-78.5, -0.2], [-78.48, -0.18]],
                [[-78.47, -0.17]],
            ],
        }
    )
    coords = _extract_linestring(geom_json)
    assert coords == [(-78.5, -0.2), (-78.48, -0.18), (-78.47, -0.17)]


def test_extract_linestring_rejects_polygons():
    geom_json = json.dumps({"type": "Polygon", "coordinates": []})
    with pytest.raises(ValueError, match="Unexpected geometry type"):
        _extract_linestring(geom_json)


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

def test_deadline_is_2026_07_20():
    # This is the hard deadline from Prompt 8 — changing it would be a
    # scope change, not a config tweak. The assertion flags accidental
    # edits.
    assert DEADLINE.isoformat() == "2026-07-20"


def test_max_attempts_is_positive():
    assert MAX_ATTEMPTS >= 1


# ---------------------------------------------------------------------------
# deadline_monitor — live-DB integration (skipped if DB not reachable).
# ---------------------------------------------------------------------------

def _dsn() -> str:
    return os.environ.get("DB_DSN", "")


def _db_reachable() -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect(_dsn(), connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _db_reachable(), reason="local datamind_ml DB not reachable"
)
def test_deadline_monitor_returns_expected_shape():
    summary = worker.deadline_monitor(dsn=_dsn())
    assert summary["deadline"] == "2026-07-20"
    assert summary["days_remaining"] == (
        DEADLINE - __import__("datetime").date.fromisoformat(summary["today"])
    ).days
    assert "status_counts" in summary
    assert "class_counts" in summary
    assert isinstance(summary["unfixed_grandfathered"], int)
    assert isinstance(summary["pending_v2_proposals"], int)
    # Total queue count across statuses is the size of the populated queue.
    total = sum(summary["status_counts"].values())
    assert total > 0, "populator should have seeded route_prod.re_entry_queue"
