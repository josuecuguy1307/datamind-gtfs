"""Policy engine for Phase 3 enforcer pipeline.

Consumes the geometry + stop-coverage reports produced by the coordinator
and returns a promotion decision plus human-readable reasons. Pure
function, no side effects — every decision is fully explainable from the
inputs.

Three profiles are supported:

- ``conservative``  — safety first. Anything severe / unroutable queues
  or rejects.
- ``balanced``      — default. Severe geometry with an IMPOSSIBLE_LOOP in
  corridor rejects; degraded stop coverage auto-accepts with DR-queue
  flag.
- ``aggressive_supervised`` — fast iteration for active units
  (Sample Region B backfill). Only the hardest failure mode (IMPOSSIBLE_LOOP
  in corridor) blocks promotion; every other anomaly slips through
  with a flag for operator follow-up.

The engine does NOT apply the decision. It only *decides*. The caller
(``phase3_coordinator``) is responsible for threading the decision into
``route_prod_writer`` or ``approval_queue``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------

PolicyDecision = Literal[
    "auto_accept",
    "queue_for_approval",
    "reject_send_to_phase2",
]

PolicyProfile = Literal[
    "conservative",
    "balanced",
    "aggressive_supervised",
]

_PROFILES: tuple[PolicyProfile, ...] = (
    "conservative",
    "balanced",
    "aggressive_supervised",
)


@dataclass(slots=True)
class PolicyVerdict:
    decision: PolicyDecision
    reasons: list[str] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Report introspection helpers.
#
# The coordinator passes us the enforcer ``to_dict()`` payloads unchanged.
# Keys here match the schema at the top of
# ``hades/enforcers/stop_coverage_enforcer.py`` and
# ``hades/enforcers/geometry_enforcer.py``.
# ---------------------------------------------------------------------------

def _geometry_classification(geometry_report: dict[str, Any]) -> str:
    """Geometry enforcer nests ``classification`` under ``summary``; we
    accept either shape so callers passing a flattened test fixture keep
    working.
    """
    summary = geometry_report.get("summary") or {}
    cls = summary.get("classification") or geometry_report.get("classification") or ""
    return str(cls).lower()


def _is_severe(geometry_report: dict[str, Any]) -> bool:
    return _geometry_classification(geometry_report) == "severe"


def _has_corridor_impossible_loop(geometry_report: dict[str, Any]) -> bool:
    """True iff any IMPOSSIBLE_LOOP anomaly sits in the route corridor.

    The geometry enforcer tags endpoint-adjacent loops differently from
    mid-shape ones. ``corridor`` is the segment between the two pivot
    ends — a loop there means the shape physically can't be driven.
    """
    for a in geometry_report.get("anomalies", []) or []:
        if str(a.get("type", "")).upper() != "IMPOSSIBLE_LOOP":
            continue
        location = str(a.get("location", a.get("zone", ""))).lower()
        if "corridor" in location or a.get("in_corridor") is True:
            return True
    return False


def _stop_classification(stop_coverage_report: dict[str, Any]) -> str:
    return str(stop_coverage_report.get("classification", "")).lower()


def _tier_usage(stop_coverage_report: dict[str, Any]) -> dict[str, int]:
    summary = stop_coverage_report.get("summary") or {}
    usage = summary.get("tier_usage") or {}
    out: dict[str, int] = {}
    for k, v in usage.items():
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _fraction_tier5(stop_coverage_report: dict[str, Any]) -> float:
    summary = stop_coverage_report.get("summary") or {}
    usage = _tier_usage(stop_coverage_report)
    tier5 = usage.get("tier5_prepared", 0)
    n_gaps = int(summary.get("n_gaps_total", 0) or 0)
    if n_gaps <= 0:
        return 0.0
    return tier5 / n_gaps


def _n_dr_queries(stop_coverage_report: dict[str, Any]) -> int:
    summary = stop_coverage_report.get("summary") or {}
    return int(summary.get("n_dr_queries_prepared", 0) or 0)


# ---------------------------------------------------------------------------
# Per-profile decision.
# ---------------------------------------------------------------------------

def _conservative(
    geometry_report: dict[str, Any],
    stop_coverage_report: dict[str, Any],
) -> PolicyVerdict:
    reasons: list[str] = []
    flags: dict[str, Any] = {}
    stop_cls = _stop_classification(stop_coverage_report)

    if _is_severe(geometry_report):
        reasons.append("geometry_severe_conservative_reject")
        return PolicyVerdict("reject_send_to_phase2", reasons, flags)

    if stop_cls == "unroutable":
        reasons.append("stop_coverage_unroutable_conservative_queue")
        return PolicyVerdict("queue_for_approval", reasons, flags)

    if stop_cls == "degraded" and _fraction_tier5(stop_coverage_report) > 0.5:
        reasons.append("stop_coverage_degraded_tier5_majority_queue")
        return PolicyVerdict("queue_for_approval", reasons, flags)

    return PolicyVerdict("auto_accept", ["conservative_clean_path"], flags)


def _balanced(
    geometry_report: dict[str, Any],
    stop_coverage_report: dict[str, Any],
) -> PolicyVerdict:
    reasons: list[str] = []
    flags: dict[str, Any] = {}
    stop_cls = _stop_classification(stop_coverage_report)

    if _is_severe(geometry_report):
        if _has_corridor_impossible_loop(geometry_report):
            reasons.append("geometry_severe_impossible_loop_in_corridor")
            return PolicyVerdict("reject_send_to_phase2", reasons, flags)
        reasons.append("geometry_severe_non_corridor_queue")
        return PolicyVerdict("queue_for_approval", reasons, flags)

    if stop_cls == "unroutable":
        reasons.append("stop_coverage_unroutable_balanced_queue")
        return PolicyVerdict("queue_for_approval", reasons, flags)

    if stop_cls == "degraded":
        reasons.append("stop_coverage_degraded_auto_accept_with_flag")
        n_dr = _n_dr_queries(stop_coverage_report)
        flags["dr_queue_populated"] = n_dr > 0
        flags["n_dr_queries"] = n_dr
        return PolicyVerdict("auto_accept", reasons, flags)

    return PolicyVerdict("auto_accept", ["balanced_clean_path"], flags)


def _aggressive_supervised(
    geometry_report: dict[str, Any],
    stop_coverage_report: dict[str, Any],
) -> PolicyVerdict:
    reasons: list[str] = []
    flags: dict[str, Any] = {}
    stop_cls = _stop_classification(stop_coverage_report)

    if _is_severe(geometry_report) and _has_corridor_impossible_loop(geometry_report):
        reasons.append("geometry_severe_impossible_loop_in_corridor_block")
        return PolicyVerdict("reject_send_to_phase2", reasons, flags)

    if stop_cls == "unroutable":
        reasons.append("stop_coverage_unroutable_supervised_accept_with_patch_flag")
        flags["operator_patch_required"] = True
        return PolicyVerdict("auto_accept", reasons, flags)

    if _is_severe(geometry_report):
        reasons.append("geometry_severe_non_corridor_supervised_accept")
        flags["operator_review_suggested"] = True
        return PolicyVerdict("auto_accept", reasons, flags)

    if stop_cls == "degraded":
        reasons.append("stop_coverage_degraded_supervised_accept")
        n_dr = _n_dr_queries(stop_coverage_report)
        flags["dr_queue_populated"] = n_dr > 0
        flags["n_dr_queries"] = n_dr
        return PolicyVerdict("auto_accept", reasons, flags)

    return PolicyVerdict("auto_accept", ["aggressive_supervised_clean_path"], flags)


# ---------------------------------------------------------------------------
# Public entry.
# ---------------------------------------------------------------------------

def decide(
    geometry_report: dict[str, Any],
    stop_coverage_report: dict[str, Any],
    profile: PolicyProfile = "balanced",
) -> PolicyVerdict:
    """Return a policy verdict for the two enforcer reports.

    Raises ``ValueError`` on an unknown profile name. The two reports
    must already be dict-shaped — call ``report.to_dict()`` on the
    dataclasses before invoking.
    """
    if profile not in _PROFILES:
        raise ValueError(f"Unknown policy profile: {profile!r}")
    if profile == "conservative":
        return _conservative(geometry_report, stop_coverage_report)
    if profile == "balanced":
        return _balanced(geometry_report, stop_coverage_report)
    return _aggressive_supervised(geometry_report, stop_coverage_report)


__all__ = [
    "PolicyDecision",
    "PolicyProfile",
    "PolicyVerdict",
    "decide",
]
