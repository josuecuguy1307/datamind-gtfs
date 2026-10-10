"""collect_evidence turns OSM tags into weighted stop/POI evidence, deterministically."""
import pytest

from src.pipeline.extract.evidence_rules import Evidence, aggregate_evidence_score, collect_evidence


def test_bus_stop_tags_give_stop_evidence() -> None:
    ev = collect_evidence({"highway": "bus_stop", "public_transport": "platform", "bus": "yes"})
    assert [(e.kind, e.weight) for e in ev] == [("stop", 1.0), ("stop", 0.9), ("stop", 0.6)]


def test_poi_tags_give_poi_evidence() -> None:
    ev = collect_evidence({"amenity": "hospital"})
    assert ev == [Evidence("poi", 0.7, "amenity=healthcare")]


def test_unrelated_tags_give_no_evidence() -> None:
    assert collect_evidence({"building": "yes", "name": "Casa"}) == []
    assert collect_evidence({}) == []


def test_is_deterministic() -> None:
    tags = {"highway": "bus_stop", "amenity": "school"}
    assert collect_evidence(tags) == collect_evidence(dict(tags))


def test_aggregate_is_clipped() -> None:
    ev = collect_evidence({"highway": "bus_stop", "public_transport": "platform"})
    assert aggregate_evidence_score(ev) == 1.0
    assert aggregate_evidence_score(ev, clip=5.0) == pytest.approx(1.9)
    assert aggregate_evidence_score([]) == 0.0
