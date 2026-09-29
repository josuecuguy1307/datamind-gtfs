"""Tests for S4 Hybrid strategy (Threshold + Shadow + Convergence)."""
from __future__ import annotations

import pytest

from ...shared.models import (
    EntityIssue,
    FixAttempt,
    QualityGateInput,
    Route,
    Severity,
    Shape,
    ShapePoint,
    Stop,
)
from .gate import (
    PassResult,
    S4HybridGate,
    _is_converged,
    _is_diverged,
    _make_issue_key,
)
from .layered_decision import (
    LayeredDecision,
    LayerResult,
    layer1_threshold,
    layer2_cross_validator,
)
from .thresholds import threshold_for


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_input(stops=None, routes=None, shapes=None,
                canton="test_canton", province="test_province"):
    return QualityGateInput(
        canton=canton, province=province,
        stops=stops or [], routes=routes or [], shapes=shapes or [],
    )


def _good_stop(stop_id="s1", name="Terminal Norte", lat=-0.1, lon=-78.5):
    return Stop(stop_id=stop_id, name=name, ref=None, lat=lat, lon=lon)


def _bad_stop_placeholder(stop_id="s_bad"):
    return Stop(stop_id=stop_id, name="parada sin nombre", ref=None,
                lat=-0.2, lon=-78.4)


def _bad_stop_empty(stop_id="s_empty"):
    return Stop(stop_id=stop_id, name="   ", ref=None, lat=-0.15, lon=-78.45)


def _bad_stop_null_island(stop_id="s_null"):
    return Stop(stop_id=stop_id, name="Ghost", ref=None, lat=0.0, lon=0.0)


# ---------------------------------------------------------------------------
# Layer 1: threshold tests
# ---------------------------------------------------------------------------

class TestLayer1Threshold:
    def test_high_confidence_passes(self):
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "x", None, 1)
        fix = FixAttempt(True, {"name": "Fixed"}, 0.8, "ok")
        passed, decision = layer1_threshold(issue, fix)
        assert passed is True
        assert decision.result == LayerResult.PASSED

    def test_low_confidence_rejected(self):
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "x", None, 1)
        fix = FixAttempt(True, {"name": "Maybe"}, 0.3, "unsure")
        passed, decision = layer1_threshold(issue, fix)
        assert passed is False
        assert decision.result == LayerResult.REJECTED_BY_THRESHOLD

    def test_exact_threshold_passes(self):
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "x", None, 1)
        thr = threshold_for("stop_name_placeholder")
        fix = FixAttempt(True, {"name": "Exact"}, thr, "ok")
        passed, _ = layer1_threshold(issue, fix)
        assert passed is True

    def test_just_below_threshold_rejected(self):
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "x", None, 1)
        thr = threshold_for("stop_name_placeholder")
        fix = FixAttempt(True, {"name": "Almost"}, thr - 0.01, "close")
        passed, _ = layer1_threshold(issue, fix)
        assert passed is False


# ---------------------------------------------------------------------------
# Layer 2: cross-validator tests
# ---------------------------------------------------------------------------

class TestLayer2CrossValidator:
    def test_good_name_fix_passes(self):
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "placeholder", "old name", 1)
        fix = FixAttempt(True, {"name": "Parada (-0.2000, -78.4000)"}, 0.6, "ok")
        entity = _bad_stop_placeholder()
        passed, decision = layer2_cross_validator(issue, fix, entity)
        assert passed is True
        assert decision.cross_validator_passed is True

    def test_too_short_name_rejected(self):
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "placeholder", None, 1)
        fix = FixAttempt(True, {"name": "ab"}, 0.6, "ok")
        entity = _bad_stop_placeholder()
        passed, decision = layer2_cross_validator(issue, fix, entity)
        assert passed is False
        assert decision.result == LayerResult.REJECTED_BY_CROSS_VALIDATOR

    def test_unregistered_rule_passes_by_default(self):
        issue = EntityIssue("stop", "s1", "made_up_rule_xyz",
                            Severity.WARNING, "test", None, 1)
        fix = FixAttempt(True, "whatever", 0.6, "ok")
        entity = _good_stop()
        passed, decision = layer2_cross_validator(issue, fix, entity)
        assert passed is True
        assert "no cross-validator registered" in decision.cross_validator_reason


# ---------------------------------------------------------------------------
# Convergence logic tests
# ---------------------------------------------------------------------------

class TestConvergence:
    def test_single_pass_not_converged(self):
        passes = [PassResult(1, 5, 3, 3, 0, 0, 0, 0)]
        assert not _is_converged(passes)

    def test_two_passes_converged(self):
        passes = [
            PassResult(1, 5, 3, 3, 0, 0, 0, 0),
            PassResult(2, 2, 1, 1, 0, 0, 3, 0),
        ]
        assert _is_converged(passes)

    def test_divergence_detected(self):
        passes = [
            PassResult(1, 5, 3, 3, 0, 0, 0, 1),
            PassResult(2, 4, 2, 2, 0, 1, 2, 1),
            PassResult(3, 5, 3, 3, 0, 2, 0, 1),
        ]
        assert _is_diverged(passes)

    def test_issue_key_format(self):
        k = _make_issue_key("stop", "s123", "stop_name_empty")
        assert k == "stop::s123::stop_name_empty"


# ---------------------------------------------------------------------------
# Gate integration tests
# ---------------------------------------------------------------------------

class TestS4Gate:
    def test_clean_canton_passes(self):
        inp = _make_input(stops=[
            _good_stop("s1", "Terminal Norte"),
            _good_stop("s2", "Estacion Sur", lat=-0.3, lon=-78.6),
        ])
        gate = S4HybridGate()
        report = gate.run(inp)
        assert report.verdict.status == "pass"
        assert report.pass_to_phase5 is True
        assert report.strategy_name == "s4_hybrid"

    def test_fixable_issues_converge(self):
        # Placeholder and empty stop names are detected but not auto-fixed:
        # Phase 4 owns context naming, and S4's inline fixers explicitly
        # disable the coord-based fallback (gate.py:_fix_stop_name_placeholder
        # and _fix_stop_name_empty). The gate must still converge without
        # oscillating, and the report must flow forward to Phase 4 rather
        # than block on these deferred issues.
        inp = _make_input(stops=[
            _bad_stop_placeholder("s1"),
            _bad_stop_empty("s2"),
            _good_stop("s3", "Real Name"),
        ])
        gate = S4HybridGate()
        report = gate.run(inp)
        assert report.verdict.issues_found >= 2
        assert report.verdict.issues_auto_fixed == 0
        assert report.pass_to_phase5 is True
        trace = report.__dict__.get("convergence_trace", {})
        assert trace.get("converged") is True
        assert trace.get("diverged") is False
        assert all(n == 0 for n in trace.get("new_per_pass") or [])
        assert all(f == 0 for f in trace.get("fixes_applied_per_pass") or [])

    def test_unfixable_issues_detected(self):
        inp = _make_input(stops=[_bad_stop_null_island()])
        gate = S4HybridGate()
        report = gate.run(inp)
        assert report.verdict.issues_found > 0

    def test_layer_diagnostics_present(self):
        inp = _make_input(stops=[
            _bad_stop_placeholder("s1"),
            _good_stop("s2", "Fine"),
        ])
        gate = S4HybridGate()
        report = gate.run(inp)
        diag = report.__dict__.get("layer_diagnostics")
        assert diag is not None
        assert "l1_rejections" in diag
        assert "l2_rejections" in diag
        assert "per_layer_breakdown" in diag

    def test_decide_rejects_failed_fix(self):
        gate = S4HybridGate()
        issue = EntityIssue("stop", "s1", "test", Severity.ERROR, "x", None, 1)
        fix = FixAttempt(False, None, 0.0, "nope")
        decision = gate.decide(issue, fix)
        assert decision.decision.value == "reject"

    def test_decide_commits_high_confidence(self):
        gate = S4HybridGate()
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "x", None, 1)
        fix = FixAttempt(True, {"name": "Fixed"}, 0.8, "ok")
        decision = gate.decide(issue, fix)
        assert decision.decision.value == "commit"

    def test_decide_rejects_low_confidence(self):
        gate = S4HybridGate()
        issue = EntityIssue("stop", "s1", "stop_name_placeholder",
                            Severity.ERROR, "x", None, 1)
        fix = FixAttempt(True, {"name": "Maybe"}, 0.2, "low")
        decision = gate.decide(issue, fix)
        assert decision.decision.value == "reject"

    def test_max_iterations(self):
        gate = S4HybridGate()
        assert gate.MAX_ITERATIONS == 3

    def test_report_has_convergence_trace(self):
        inp = _make_input(stops=[_good_stop()])
        gate = S4HybridGate()
        report = gate.run(inp)
        assert "convergence_trace" in report.__dict__
        trace = report.__dict__["convergence_trace"]
        assert "num_passes" in trace
        assert "converged" in trace


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
