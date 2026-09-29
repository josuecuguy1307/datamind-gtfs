import pytest
from pathlib import Path

ACTIONS_PATH = Path(__file__).resolve().parents[1] / "actions.json"

from phase1_nodes.datamind.services.openmaps_extractor.src.overpass.actions import build_query

def test_actions_can_render_stops_broad_bbox():
    bbox = {"south": -0.220, "west": -78.515, "north": -0.210, "east": -78.505}
    spec, merged, query = build_query(str(ACTIONS_PATH), "stops_broad_bbox", {"bbox": bbox})

    assert spec.id == "stops_broad_bbox"
    assert "timeout" in query or "timeout:" in query
    assert "out" in query.lower()

def test_missing_required_param_raises():
    with pytest.raises(ValueError):
        build_query(str(ACTIONS_PATH), "stops_broad_bbox", {})   # bbox required
