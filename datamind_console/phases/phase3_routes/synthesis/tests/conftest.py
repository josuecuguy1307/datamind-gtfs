"""Shared fixtures for the synthesis test suites.

Since ed956604 (2026-04-27) synthesized nodes are written through the
universal stop treater (``phase3_routes.services.stop_quality.treat_stop``),
which normalizes names, runs the Overpass naming tiers, maps places and
audits the treatment. These suites predate that migration: their fake
cursors still emulated the old direct ``INSERT INTO node_prod.nodes``, so the
treater's real SQL and network calls made 14 tests fail.

The treater has its own suite (``tests/phase3_routes/services/stop_quality``).
Here it is replaced by a fake that records what synthesis asked for, so these
tests keep checking synthesis itself: stage choice, caps, events, rollback.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest


def _fake_treat_stop(inp: Any, conn: Any) -> Any:
    from phase3_routes.services.stop_quality.stop_treater import TreatmentResult

    store = getattr(conn, "_store", None)
    if store is None:
        raise AssertionError("fake treat_stop needs a fake connection with a _store")
    assert inp.operation == "synthetic_insert", inp.operation
    extras = dict(inp.extras or {})
    node_id = uuid.uuid4()
    store.nodes.append(
        {
            "node_id": str(node_id),
            "osm_id": extras.get("osm_id"),
            "lat": inp.proposed_lat,
            "lon": inp.proposed_lon,
            "source": extras.get("source"),
            "source_type": extras.get("source_type"),
            "synthetic_confidence": extras.get("synthetic_confidence"),
            "anchor_name": inp.proposed_name,
            "poi_osm_id": extras.get("poi_anchor_osm_id"),
            "poi_to_path_distance_m": extras.get("poi_to_path_distance_m"),
            "path_projection_distance_m": extras.get("path_projection_distance_m"),
            "research_to_projection_distance_m": extras.get("research_to_projection_distance_m"),
            "osm_route_fill_context": extras.get("osm_route_fill_context"),
            "caller": inp.caller,
        }
    )
    return TreatmentResult(
        success=True,
        treatment_id=str(uuid.uuid4()),
        node_id=node_id,
        final_name=inp.proposed_name or None,
        final_lat=inp.proposed_lat,
        final_lon=inp.proposed_lon,
        place_id=None,
        name_was_forbidden_input=False,
        name_normalized=False,
        context_name_applied=False,
        context_name_method=None,
        place_mapping_created=False,
        place_mapping_validated=False,
    )


@pytest.fixture(autouse=True)
def _fake_stop_treater(monkeypatch: pytest.MonkeyPatch) -> None:
    import phase3_routes.services.stop_quality as stop_quality

    monkeypatch.setattr(stop_quality, "treat_stop", _fake_treat_stop)
