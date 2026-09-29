#!/usr/bin/env python3
"""Targeted tests for repair_contaminated_hosts.py logic."""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List

from repair_contaminated_hosts import (
    SplitCandidate,
    build_repair_plan,
    _build_split_row_review,
    _source_name,
    _route_hint_signature,
)


def _make_attempt(
    relation_id: int,
    source: str = "test_source.json",
    place: str = "TestPlace",
    hint: str = "A - B",
    **extra: Any,
) -> Dict[str, Any]:
    return {
        "attempt_key": str(uuid.uuid4()),
        "chosen_osm_relation_id": relation_id,
        "source_document": source,
        "place": place,
        "route_hint_raw": hint,
        "group": extra.get("group", "test_group"),
        "place_bundle": extra.get("place_bundle", "test_bundle"),
        "seed_origin": extra.get("seed_origin", "catalog"),
        "attempt_type": extra.get("attempt_type", "test_type"),
        "batch_id": extra.get("batch_id", "batch_001"),
        "selection_confidence": extra.get("selection_confidence", 0.85),
        "novelty_status": extra.get("novelty_status", "novel_relation_selected"),
        "fetch_relation_stored": extra.get("fetch_relation_stored", False),
        "cooperative_hint": extra.get("cooperative_hint"),
        "bbox_used": extra.get("bbox_used"),
        "interpretation_source": extra.get("interpretation_source"),
        "priority": extra.get("priority", "normal"),
    }


def _make_host(
    relation_id: int,
    attempts: List[Dict[str, Any]],
    source: str = "test_source.json",
) -> Dict[str, Any]:
    return {
        "route_id": str(uuid.uuid4()),
        "chosen_osm_relation_id": relation_id,
        "extractor_source": source,
        "status": "new",
        "extractor_review": {
            "source_document": source,
            "discover": {
                "chosen_osm_relation_id": relation_id,
                "relation_extraction_success": True,
            },
            "attempt_history": attempts,
        },
    }


# ---- Tests ----

def test_healthy_host_not_split():
    """A host where all attempts match the host relation should NOT be split."""
    attempts = [
        _make_attempt(100, source="a.json", place="PlaceA"),
        _make_attempt(100, source="b.json", place="PlaceB"),
        _make_attempt(100, source="c.json", place="PlaceC"),
    ]
    host = _make_host(100, attempts)
    plan = build_repair_plan(host)
    assert plan is None, "Healthy host should produce no plan"
    print("PASS: test_healthy_host_not_split")


def test_single_divergent_attempt_splits():
    """A host with one divergent attempt should produce exactly one split."""
    attempts = [
        _make_attempt(100, source="a.json", place="PlaceA"),
        _make_attempt(100, source="a.json", place="PlaceB"),
        _make_attempt(200, source="b.json", place="PlaceC"),  # divergent
    ]
    host = _make_host(100, attempts)
    plan = build_repair_plan(host)
    assert plan is not None, "Should produce a plan"
    assert len(plan.splits) == 1, f"Expected 1 split, got {len(plan.splits)}"
    assert plan.splits[0].split_relation_id == 200
    assert plan.kept_attempts == 2
    print("PASS: test_single_divergent_attempt_splits")


def test_multi_relation_divergence():
    """Multiple divergent relations from different sources produce separate splits."""
    attempts = [
        _make_attempt(100, source="host.json", place="Home"),
        _make_attempt(200, source="a.json", place="PlaceA"),
        _make_attempt(200, source="b.json", place="PlaceA"),  # same rel, diff source
        _make_attempt(300, source="c.json", place="PlaceC"),
    ]
    host = _make_host(100, attempts)
    plan = build_repair_plan(host)
    assert plan is not None
    assert len(plan.splits) == 3, f"Expected 3 splits (200/a, 200/b, 300/c), got {len(plan.splits)}"
    split_keys = {(s.split_relation_id, s.split_source_document) for s in plan.splits}
    assert (200, "a.json") in split_keys
    assert (200, "b.json") in split_keys
    assert (300, "c.json") in split_keys
    assert plan.kept_attempts == 1
    print("PASS: test_multi_relation_divergence")


def test_provenance_retained_in_split_review():
    """Split row review should carry full provenance."""
    attempt = _make_attempt(200, source="foreign.json", place="FarPlace", hint="X - Y")
    split = SplitCandidate(
        host_route_id="host-001",
        host_relation_id=100,
        host_extractor_source="host.json",
        split_relation_id=200,
        split_source_document="foreign.json",
        attempts=[attempt],
        distinct_places=["FarPlace"],
        distinct_hints=["X - Y"],
        distinct_hint_signatures=["x -> y"],
        reason="test reason",
    )
    review = _build_split_row_review(split, attempt)
    prov = review.get("repair_provenance", {})
    assert prov["split_from_host_route_id"] == "host-001"
    assert prov["host_chosen_osm_relation_id"] == 100
    assert prov["split_relation_id"] == 200
    assert prov["attempt_count_migrated"] == 1
    assert review["discover"]["chosen_osm_relation_id"] == 200
    assert review["target"]["place"] == "FarPlace"
    assert review["hints"]["route_hint_raw"] == "X - Y"
    assert review["source_document"] == "foreign.json"
    assert review["attempt_history"] == [attempt]
    print("PASS: test_provenance_retained_in_split_review")


def test_no_relation_host_skipped():
    """Host with NULL chosen_osm_relation_id should be skipped."""
    attempts = [
        _make_attempt(100, source="a.json"),
        _make_attempt(200, source="b.json"),
    ]
    host = _make_host(100, attempts)
    host["chosen_osm_relation_id"] = None
    plan = build_repair_plan(host)
    assert plan is None, "NULL-relation host should be skipped"
    print("PASS: test_no_relation_host_skipped")


def test_source_name_extraction():
    assert _source_name("/home/foo/bar/catalog.json") == "catalog.json"
    assert _source_name("catalog.json") == "catalog.json"
    assert _source_name(None) is None
    assert _source_name("") is None
    print("PASS: test_source_name_extraction")


def test_route_hint_signature():
    assert _route_hint_signature("Quitumbe - Centro Historico") == "quitumbe -> centro historico"
    assert _route_hint_signature("Marin - Simon Bolivar - Chillogallo") == "marin -> chillogallo"
    assert _route_hint_signature(None) is None
    print("PASS: test_route_hint_signature")


if __name__ == "__main__":
    test_healthy_host_not_split()
    test_single_divergent_attempt_splits()
    test_multi_relation_divergence()
    test_provenance_retained_in_split_review()
    test_no_relation_host_skipped()
    test_source_name_extraction()
    test_route_hint_signature()
    print("\n=== ALL TESTS PASSED ===")
