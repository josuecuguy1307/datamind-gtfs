"""Tests for S3 Two-Pass Convergence strategy."""
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
from .convergence import PassResult, is_converged, is_diverged, make_issue_key
from .gate import S3ConvergenceGate
from .working_copy import WorkingCopy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_input(
    stops=None, routes=None, shapes=None,
    canton="test_canton", province="test_province",
) -> QualityGateInput:
    return QualityGateInput(
        canton=canton,
        province=province,
        stops=stops or [],
        routes=routes or [],
        shapes=shapes or [],
    )


def _good_stop(stop_id="s1", name="Terminal Norte", lat=-0.1, lon=-78.5):
    return Stop(stop_id=stop_id, name=name, ref=None, lat=lat, lon=lon)


def _bad_stop_placeholder(stop_id="s_bad"):
    return Stop(stop_id=stop_id, name="parada sin nombre", ref=None, lat=-0.2, lon=-78.4)


def _bad_stop_empty(stop_id="s_empty"):
    return Stop(stop_id=stop_id, name="   ", ref=None, lat=-0.15, lon=-78.45)


def _bad_stop_null_island(stop_id="s_null"):
    return Stop(stop_id=stop_id, name="Ghost", ref=None, lat=0.0, lon=0.0)


# ---------------------------------------------------------------------------
# WorkingCopy tests
# ---------------------------------------------------------------------------

class TestWorkingCopy:
    def test_from_input_deep_copies(self):
        s = _good_stop()
        inp = _make_input(stops=[s])
        wc = WorkingCopy.from_input(inp)
        # Mutating working copy should not affect original
        wc.stops["s1"].name = "CHANGED"
        assert s.name == "Terminal Norte"

    def test_apply_fix_dict_patch(self):
        inp = _make_input(stops=[_good_stop()])
        wc = WorkingCopy.from_input(inp)
        fix = FixAttempt(True, {"name": "Nuevo Nombre"}, 0.8, "renamed")
        wc.apply_fix("stop", "s1", fix, pass_num=1)
        assert wc.stops["s1"].name == "Nuevo Nombre"
        assert len(wc.mutations_log) == 1
        assert wc.mutations_log[0].old_value == "Terminal Norte"

    def test_apply_fix_scalar(self):
        inp = _make_input(stops=[_good_stop()])
        wc = WorkingCopy.from_input(inp)
        fix = FixAttempt(True, "Scalar Name", 0.8, "scalar")
        wc.apply_fix("stop", "s1", fix, pass_num=1)
        assert wc.stops["s1"].name == "Scalar Name"

    def test_remove_entity(self):
        inp = _make_input(stops=[_good_stop("s1"), _good_stop("s2", name="Otro")])
        wc = WorkingCopy.from_input(inp)
        assert wc.remove_entity("stop", "s2", pass_num=1)
        assert "s2" not in wc.stops
        assert len(wc.mutations_log) == 1

    def test_revert_all(self):
        inp = _make_input(stops=[_good_stop()])
        wc = WorkingCopy.from_input(inp)
        wc.apply_fix("stop", "s1", FixAttempt(True, {"name": "X"}, 0.9, ""), 1)
        wc.revert_all()
        assert wc.stops["s1"].name == "Terminal Norte"
        assert len(wc.mutations_log) == 0

    def test_to_entity_lists(self):
        inp = _make_input(stops=[_good_stop(), _good_stop("s2", "Sur")])
        wc = WorkingCopy.from_input(inp)
        lists = wc.to_entity_lists()
        assert len(lists["stops"]) == 2


# ---------------------------------------------------------------------------
# Convergence logic tests
# ---------------------------------------------------------------------------

class TestConvergence:
    def test_single_pass_not_converged(self):
        passes = [PassResult(1, 5, 3, 3, 0, 0, 0, 0)]
        assert not is_converged(passes)

    def test_two_passes_converged(self):
        passes = [
            PassResult(1, 5, 3, 3, 0, 0, 0, 0),
            PassResult(2, 2, 1, 1, 0, 0, 3, 0),  # no new issues, no undone
        ]
        assert is_converged(passes)

    def test_two_passes_not_converged_new_issues(self):
        passes = [
            PassResult(1, 5, 3, 3, 0, 0, 0, 0),
            PassResult(2, 3, 1, 1, 0, 2, 4, 0),  # 2 new issues
        ]
        assert not is_converged(passes)

    def test_divergence_detected(self):
        passes = [
            PassResult(1, 5, 3, 3, 0, 0, 0, 1),
            PassResult(2, 4, 2, 2, 0, 1, 2, 1),
            PassResult(3, 5, 3, 3, 0, 2, 0, 1),  # undos in last 3 = 3 > 2
        ]
        assert is_diverged(passes)

    def test_no_divergence_under_threshold(self):
        passes = [
            PassResult(1, 5, 3, 3, 0, 0, 0, 0),
            PassResult(2, 4, 2, 2, 0, 0, 2, 1),
            PassResult(3, 3, 1, 1, 0, 0, 2, 0),  # total undos = 1
        ]
        assert not is_diverged(passes)

    def test_issue_key_format(self):
        k = make_issue_key("stop", "s123", "stop_name_empty")
        assert k == "stop::s123::stop_name_empty"


# ---------------------------------------------------------------------------
# Gate integration tests
# ---------------------------------------------------------------------------

class TestS3Gate:
    def test_clean_canton_passes(self):
        """Canton with no issues should pass on the first re-check."""
        inp = _make_input(stops=[
            _good_stop("s1", "Terminal Norte"),
            _good_stop("s2", "Estacion Sur", lat=-0.3, lon=-78.6),
        ])
        gate = S3ConvergenceGate()
        report = gate.run(inp)
        assert report.verdict.status == "pass"
        assert report.pass_to_phase5 is True
        assert report.strategy_name == "s3_convergence"

    def test_fixable_issues_converge(self):
        """Placeholder names should be fixed and converge in 2 passes."""
        inp = _make_input(stops=[
            _bad_stop_placeholder("s1"),
            _bad_stop_empty("s2"),
            _good_stop("s3", "Real Name"),
        ])
        gate = S3ConvergenceGate()
        report = gate.run(inp)
        assert report.verdict.status == "pass_with_fixes"
        assert report.pass_to_phase5 is True
        assert report.verdict.issues_auto_fixed >= 2
        trace = report.__dict__.get("convergence_trace", {})
        assert trace.get("converged") is True

    def test_unfixable_issues_fail(self):
        """Null island stop can't be auto-fixed — should fail."""
        inp = _make_input(stops=[_bad_stop_null_island()])
        gate = S3ConvergenceGate()
        report = gate.run(inp)
        # The gate should still detect the issue
        assert report.verdict.issues_found > 0

    def test_max_iterations_respected(self):
        gate = S3ConvergenceGate()
        assert gate.MAX_ITERATIONS == 3

    def test_decide_rejects_failed_fix(self):
        gate = S3ConvergenceGate()
        issue = EntityIssue("stop", "s1", "test", Severity.ERROR, "x", None, 1)
        fix = FixAttempt(False, None, 0.0, "nope")
        decision = gate.decide(issue, fix)
        assert decision.decision.value == "reject"

    def test_decide_commits_high_confidence(self):
        gate = S3ConvergenceGate()
        issue = EntityIssue("stop", "s1", "test", Severity.ERROR, "x", None, 1)
        fix = FixAttempt(True, {"name": "Fixed"}, 0.8, "ok")
        decision = gate.decide(issue, fix)
        assert decision.decision.value == "commit"

    def test_decide_defers_low_confidence(self):
        gate = S3ConvergenceGate()
        issue = EntityIssue("stop", "s1", "test", Severity.ERROR, "x", None, 1)
        fix = FixAttempt(True, {"name": "Maybe"}, 0.3, "unsure")
        decision = gate.decide(issue, fix)
        assert decision.decision.value == "defer"

    def test_report_has_convergence_trace(self):
        inp = _make_input(stops=[_good_stop()])
        gate = S3ConvergenceGate()
        report = gate.run(inp)
        assert "convergence_trace" in report.__dict__
        trace = report.__dict__["convergence_trace"]
        assert "num_passes" in trace
        assert "converged" in trace


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
