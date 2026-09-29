"""Tests for hades.enforcers.policy_engine."""
from __future__ import annotations

import pytest

from hades.enforcers.policy_engine import decide


def _clean_geometry() -> dict:
    return {"classification": "clean", "anomalies": [], "max_severity": 0.0}


def _severe_geometry(anomaly_type: str = "U_TURN", in_corridor: bool = False) -> dict:
    return {
        "classification": "severe",
        "max_severity": 0.9,
        "anomalies": [
            {
                "type": anomaly_type,
                "severity": 0.9,
                "location_idx": 142,
                "in_corridor": in_corridor,
            }
        ],
    }


def _stop_coverage(classification: str, n_gaps_total: int = 0, tier5: int = 0, n_dr: int = 0) -> dict:
    return {
        "classification": classification,
        "summary": {
            "n_gaps_total": n_gaps_total,
            "tier_usage": {"tier5_prepared": tier5},
            "n_dr_queries_prepared": n_dr,
        },
    }


# ---------------------------------------------------------------------------
# Conservative profile
# ---------------------------------------------------------------------------

def test_conservative_clean_route_auto_accepts():
    verdict = decide(_clean_geometry(), _stop_coverage("good"), profile="conservative")
    assert verdict.decision == "auto_accept"


def test_conservative_severe_geometry_rejects():
    verdict = decide(_severe_geometry(), _stop_coverage("good"), profile="conservative")
    assert verdict.decision == "reject_send_to_phase2"
    assert any("severe" in r for r in verdict.reasons)


def test_conservative_unroutable_queues():
    verdict = decide(_clean_geometry(), _stop_coverage("unroutable", n_gaps_total=3), profile="conservative")
    assert verdict.decision == "queue_for_approval"


def test_conservative_degraded_tier5_majority_queues():
    # 3 gaps total, 2 resolved via tier5 => 66% → majority.
    verdict = decide(
        _clean_geometry(),
        _stop_coverage("degraded", n_gaps_total=3, tier5=2),
        profile="conservative",
    )
    assert verdict.decision == "queue_for_approval"
    assert "tier5_majority" in verdict.reasons[0]


def test_conservative_degraded_tier5_minority_accepts():
    # 3 gaps, 1 tier5 → 33% → accept.
    verdict = decide(
        _clean_geometry(),
        _stop_coverage("degraded", n_gaps_total=3, tier5=1),
        profile="conservative",
    )
    assert verdict.decision == "auto_accept"


# ---------------------------------------------------------------------------
# Balanced profile
# ---------------------------------------------------------------------------

def test_balanced_severe_corridor_impossible_loop_rejects():
    verdict = decide(
        _severe_geometry("IMPOSSIBLE_LOOP", in_corridor=True),
        _stop_coverage("good"),
        profile="balanced",
    )
    assert verdict.decision == "reject_send_to_phase2"
    assert verdict.reasons[0] == "geometry_severe_impossible_loop_in_corridor"


def test_balanced_severe_non_corridor_queues():
    verdict = decide(
        _severe_geometry("U_TURN", in_corridor=False),
        _stop_coverage("good"),
        profile="balanced",
    )
    assert verdict.decision == "queue_for_approval"


def test_balanced_degraded_auto_accepts_with_flag():
    verdict = decide(
        _clean_geometry(),
        _stop_coverage("degraded", n_gaps_total=2, n_dr=1),
        profile="balanced",
    )
    assert verdict.decision == "auto_accept"
    assert verdict.flags.get("dr_queue_populated") is True
    assert verdict.flags.get("n_dr_queries") == 1


def test_balanced_unroutable_queues():
    verdict = decide(
        _clean_geometry(), _stop_coverage("unroutable", n_gaps_total=1), profile="balanced"
    )
    assert verdict.decision == "queue_for_approval"


# ---------------------------------------------------------------------------
# Aggressive supervised profile
# ---------------------------------------------------------------------------

def test_supervised_corridor_loop_still_rejects():
    verdict = decide(
        _severe_geometry("IMPOSSIBLE_LOOP", in_corridor=True),
        _stop_coverage("good"),
        profile="aggressive_supervised",
    )
    assert verdict.decision == "reject_send_to_phase2"


def test_supervised_unroutable_accepts_with_patch_flag():
    verdict = decide(
        _clean_geometry(),
        _stop_coverage("unroutable", n_gaps_total=3),
        profile="aggressive_supervised",
    )
    assert verdict.decision == "auto_accept"
    assert verdict.flags.get("operator_patch_required") is True


def test_supervised_severe_non_corridor_accepts_with_review_flag():
    verdict = decide(
        _severe_geometry("U_TURN", in_corridor=False),
        _stop_coverage("good"),
        profile="aggressive_supervised",
    )
    assert verdict.decision == "auto_accept"
    assert verdict.flags.get("operator_review_suggested") is True


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_unknown_profile_raises():
    with pytest.raises(ValueError):
        decide(_clean_geometry(), _stop_coverage("good"), profile="made_up")  # type: ignore[arg-type]
