#!/usr/bin/env python3
"""Targeted tests for repair_pass2_novel_alternative_trail.py logic."""
from __future__ import annotations

import uuid
from typing import Any, Dict

from repair_pass2_novel_alternative_trail import analyze_row, _to_int, _source_name


def _make_row(
    host_rel: int,
    attempts: list,
    source: str = "test.json",
    novelty: str = "novel_alternative_selected",
) -> Dict[str, Any]:
    return {
        "route_id": str(uuid.uuid4()),
        "chosen_osm_relation_id": host_rel,
        "extractor_source": source,
        "status": "new",
        "extractor_review": {
            "source_document": source,
            "discover": {
                "novelty_status": novelty,
            },
            "dedupe": {
                "novelty_status": novelty,
            },
            "attempt_history": attempts,
        },
    }


def _make_attempt(rel: int, novelty: str = "novel_alternative_selected") -> Dict[str, Any]:
    return {
        "chosen_osm_relation_id": rel,
        "novelty_status": novelty,
        "source_document": "test.json",
        "place": "TestPlace",
    }


def test_matching_attempt_not_flagged():
    """When attempt rel matches host rel, no repair needed."""
    row = _make_row(100, [_make_attempt(100)])
    result = analyze_row(row)
    assert result is None, "Matching attempt should produce no repair plan"
    print("PASS: test_matching_attempt_not_flagged")


def test_mismatched_novel_alternative_flagged():
    """When attempt rel differs from host rel on novel_alternative, flag it."""
    row = _make_row(200, [_make_attempt(100, "novel_alternative_selected")])
    result = analyze_row(row)
    assert result is not None, "Mismatched novel_alternative should be flagged"
    assert result["host_rel"] == 200
    assert result["mismatched_indices"] == [0]
    assert result["mismatched_relations"] == [100]
    print("PASS: test_mismatched_novel_alternative_flagged")


def test_non_novel_alternative_not_flagged():
    """Mismatched rel but non-novel-alternative novelty should NOT be flagged."""
    row = _make_row(200, [_make_attempt(100, "novel_relation_selected")])
    result = analyze_row(row)
    assert result is None, "Non-novel-alternative mismatch should not be flagged"
    print("PASS: test_non_novel_alternative_not_flagged")


def test_mixed_attempts():
    """Only novel_alternative mismatches are flagged, others left alone."""
    row = _make_row(200, [
        _make_attempt(200, "novel_relation_selected"),  # matches host, skip
        _make_attempt(100, "novel_alternative_selected"),  # mismatch + novel_alt -> flag
        _make_attempt(200, "novel_alternative_selected"),  # matches host even though novel_alt
    ])
    result = analyze_row(row)
    assert result is not None
    assert result["mismatched_indices"] == [1]
    assert result["mismatched_relations"] == [100]
    print("PASS: test_mixed_attempts")


def test_null_host_relation_skipped():
    """Null host relation means no repair."""
    row = _make_row(100, [_make_attempt(200)])
    row["chosen_osm_relation_id"] = None
    result = analyze_row(row)
    assert result is None
    print("PASS: test_null_host_relation_skipped")


def test_empty_history_skipped():
    """No attempt_history means no repair."""
    row = _make_row(100, [])
    result = analyze_row(row)
    assert result is None
    print("PASS: test_empty_history_skipped")


if __name__ == "__main__":
    test_matching_attempt_not_flagged()
    test_mismatched_novel_alternative_flagged()
    test_non_novel_alternative_not_flagged()
    test_mixed_attempts()
    test_null_host_relation_skipped()
    test_empty_history_skipped()
    print("\n=== ALL TESTS PASSED ===")
