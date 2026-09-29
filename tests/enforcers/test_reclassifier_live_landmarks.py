"""Unit tests for the --with-live-landmarks path in reclassifier.

These exercise the pure helpers (`_load_validated_landmarks`,
`_apply_landmarks_to_report`, and the per-row ``_reclassify_row`` with
the optional ``landmark_map`` arg) without touching the DB. The
end-to-end smoke against approval_queue is covered separately by the
Step 3 / Step 4 runs in this session.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from hades.enforcers.reclassifier import (
    _apply_landmarks_to_report,
    _load_validated_landmarks,
    _reclassify_row,
)


# ---------------------------------------------------------------------------
# _apply_landmarks_to_report
# ---------------------------------------------------------------------------

def _sc_with_two_tier4_gaps() -> dict:
    """Synthetic stop_coverage_report with two tier-4 unresolved gaps."""
    return {
        "route_code": "ROUTE-A",
        "version": 1,
        "zone": "urban_dense",
        "classification": "ship_pending_dr",
        "n_stops": 12,
        "route_length_m": 8000.0,
        "gaps": [
            {
                "idx": 0, "prev_stop_idx": 3, "next_stop_idx": 4,
                "gap_m": 700.0, "midpoint_cum_m": 1500.0,
                "midpoint_coord": [-0.18, -78.48],
                "severity_band": "acceptable",
                "resolution": {"tier": 4, "tier_label": "dr_prepared",
                                "resolved": False},
            },
            {
                "idx": 1, "prev_stop_idx": 7, "next_stop_idx": 8,
                "gap_m": 800.0, "midpoint_cum_m": 4500.0,
                "midpoint_coord": [-0.19, -78.49],
                "severity_band": "acceptable",
                "resolution": {"tier": 4, "tier_label": "dr_prepared",
                                "resolved": False},
            },
        ],
    }


def test_apply_landmarks_flips_only_matching_tier4_gaps():
    sc = _sc_with_two_tier4_gaps()
    landmark_map = {
        ("ROUTE-A", 0): {"name": "Plaza Sample Region", "lat": -0.18, "lon": -78.48,
                          "final_confidence": 0.85, "decision": "ACCEPT",
                          "source_batch": "qc_001"},
    }
    out, n = _apply_landmarks_to_report(sc, "ROUTE-A", landmark_map)
    assert n == 1
    assert out["gaps"][0]["resolution"]["resolved"] is True
    assert out["gaps"][0]["resolution"]["tier_label"] == "dr_prepared_landmark_validated"
    assert out["gaps"][0]["resolution"]["candidate_metadata"]["source_batch"] == "qc_001"
    # Gap 1 has no matching landmark — stays unresolved.
    assert out["gaps"][1]["resolution"]["resolved"] is False
    # Original input not mutated.
    assert sc["gaps"][0]["resolution"]["resolved"] is False


def test_apply_landmarks_skips_non_tier4_and_already_resolved_gaps():
    sc = _sc_with_two_tier4_gaps()
    # Pretend gap 0 was already tier-1 resolved; landmark should not stomp it.
    sc["gaps"][0]["resolution"] = {"tier": 1, "tier_label": "cross_route_borrow",
                                    "resolved": True}
    landmark_map = {
        ("ROUTE-A", 0): {"name": "X", "lat": 0, "lon": 0,
                          "final_confidence": 0.9, "decision": "ACCEPT",
                          "source_batch": "qc_001"},
        ("ROUTE-A", 1): {"name": "Y", "lat": 0, "lon": 0,
                          "final_confidence": 0.9, "decision": "ACCEPT",
                          "source_batch": "qc_002"},
    }
    out, n = _apply_landmarks_to_report(sc, "ROUTE-A", landmark_map)
    # Only gap 1 (tier-4, unresolved) should flip.
    assert n == 1
    assert out["gaps"][0]["resolution"]["tier"] == 1
    assert out["gaps"][1]["resolution"]["resolved"] is True


def test_apply_landmarks_returns_zero_for_unknown_route():
    sc = _sc_with_two_tier4_gaps()
    landmark_map = {
        ("OTHER-ROUTE", 0): {"name": "X", "lat": 0, "lon": 0,
                              "final_confidence": 0.9, "decision": "ACCEPT",
                              "source_batch": "qc_001"},
    }
    out, n = _apply_landmarks_to_report(sc, "ROUTE-A", landmark_map)
    assert n == 0
    assert out["gaps"][0]["resolution"]["resolved"] is False


# ---------------------------------------------------------------------------
# _reclassify_row with landmark_map → upgrade
# ---------------------------------------------------------------------------

def test_reclassify_row_with_landmarks_drives_class_upgrade():
    """A route with one tier-4 unresolved gap and clean geometry is
    ship_pending_dr today. Apply a landmark for that gap and the class
    flips to good (no unresolved → rule 'no_unresolved')."""
    sc = {
        "route_code": "ROUTE-X",
        "version": 1,
        "zone": "urban_dense",
        "classification": "ship_pending_dr",
        "n_stops": 8,
        "route_length_m": 5000.0,
        "gaps": [
            {
                "idx": 0, "prev_stop_idx": 2, "next_stop_idx": 3,
                "gap_m": 700.0, "midpoint_cum_m": 1500.0,
                "midpoint_coord": [-0.18, -78.48],
                "severity_band": "acceptable",
                "resolution": {"tier": 4, "tier_label": "dr_prepared",
                                "resolved": False},
            },
        ],
    }
    geom = {"classification": "clean"}

    # Without landmarks → ship_pending_dr (1 tier-4 unresolved, clean shape).
    cls, _, tier4, n_applied = _reclassify_row(sc, geom)
    assert cls == "ship_pending_dr"
    assert tier4 == 1
    assert n_applied == 0

    # With a matching landmark → good (no unresolved).
    landmark_map = {
        ("ROUTE-X", 0): {"name": "Iglesia San Roque", "lat": -0.18, "lon": -78.48,
                          "final_confidence": 0.92, "decision": "ACCEPT",
                          "source_batch": "qc_007"},
    }
    cls2, _, tier4_2, n_applied_2 = _reclassify_row(
        sc, geom, route_code="ROUTE-X", landmark_map=landmark_map,
    )
    assert cls2 == "good"
    assert tier4_2 == 0
    assert n_applied_2 == 1


# ---------------------------------------------------------------------------
# _load_validated_landmarks
# ---------------------------------------------------------------------------

def test_load_validated_landmarks_reads_accepted_and_uncertain_only(tmp_path):
    """Reject bucket entries are ignored; accepted + accept_uncertain feed
    the map; highest-confidence wins on duplicate (route, gap)."""
    vdir = tmp_path / "validated"
    vdir.mkdir()
    (vdir / "qc_001.json").write_text(json.dumps({
        "schema": "dr_stop_coverage_validator/v1",
        "accepted": [
            {"route_code": "R1", "gap_number": 1, "name": "A",
             "approx_lat": 0, "approx_lng": 0,
             "final_confidence": 0.7, "decision": "ACCEPT"},
            # Same gap, lower confidence — should lose to the next file.
            {"route_code": "R2", "gap_number": 2, "name": "B-low",
             "approx_lat": 0, "approx_lng": 0,
             "final_confidence": 0.5, "decision": "ACCEPT"},
        ],
        "accept_uncertain": [
            {"route_code": "R3", "gap_number": 1, "name": "C",
             "approx_lat": 0, "approx_lng": 0,
             "final_confidence": 0.45, "decision": "ACCEPT_UNCERTAIN"},
        ],
        "rejected": [
            {"route_code": "R1", "gap_number": 99, "name": "REJECTED",
             "approx_lat": 0, "approx_lng": 0,
             "final_confidence": 0.1, "decision": "REJECT"},
        ],
    }))
    (vdir / "qc_002.json").write_text(json.dumps({
        "schema": "dr_stop_coverage_validator/v1",
        "accepted": [
            # Higher-confidence dup of R2 gap 2 — should win.
            {"route_code": "R2", "gap_number": 2, "name": "B-high",
             "approx_lat": 0, "approx_lng": 0,
             "final_confidence": 0.9, "decision": "ACCEPT"},
        ],
        "accept_uncertain": [],
        "rejected": [],
    }))

    m = _load_validated_landmarks(validated_dir=vdir)
    assert ("R1", 0) in m  # gap_number 1 → idx 0
    assert ("R2", 1) in m
    assert ("R3", 0) in m
    assert ("R1", 98) not in m  # rejected ignored
    # Highest-confidence wins.
    assert m[("R2", 1)]["name"] == "B-high"
    assert m[("R2", 1)]["final_confidence"] == 0.9


def test_load_validated_landmarks_filters_by_batch_id(tmp_path):
    vdir = tmp_path / "validated"
    vdir.mkdir()
    (vdir / "qc_001.json").write_text(json.dumps({
        "accepted": [{"route_code": "R1", "gap_number": 1, "name": "x",
                       "approx_lat": 0, "approx_lng": 0,
                       "final_confidence": 0.7, "decision": "ACCEPT"}],
    }))
    (vdir / "qc_002.json").write_text(json.dumps({
        "accepted": [{"route_code": "R2", "gap_number": 1, "name": "y",
                       "approx_lat": 0, "approx_lng": 0,
                       "final_confidence": 0.7, "decision": "ACCEPT"}],
    }))
    m = _load_validated_landmarks(validated_dir=vdir, only_batch_ids={"qc_001"})
    assert ("R1", 0) in m
    assert ("R2", 0) not in m
