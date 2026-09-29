"""Phase 3 enforcer coordinator.

Canonical choke point between "route passed quality gate" and
"route_prod_writer is called". Runs both enforcers, threads DR queries
through the budget tracker, hands the reports to the policy engine, and
returns an ``EnforcerResult`` for the caller to act on.

The coordinator never writes to ``route_prod.*``. It analyzes and
decides — the promotion path (``write_to_route_prod`` vs
``approval_queue`` vs Phase-2 feedback) is the caller's responsibility.

If an enforcer crashes, the result carries
``policy_decision = "queue_for_approval"`` with an ``enforcer_crashed``
reason so the route lands in the approval queue instead of silently
promoting. This is the contract the prompt requires: "every Phase 3
substep wrapped in try/except → if an enforcer crashes, route goes to
error queue, NOT silently promoted."

Side effect: if the policy profile says "queue DR", we append each
budgeted DR query to the per-unit pending batch markdown via the
``dr_queue_writer`` callable (injected, defaults to the filesystem
writer at ``workspace/dr_stop_coverage/queries/pending_batch_<unit>.md``).
Tests pass in an in-memory callable to keep the side effect hermetic.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence


Phase3Mode = Literal["publish", "enhance"]

from hades.enforcers.dr_budget_tracker import (
    DEFAULT_MAX_PER_UNIT,
    DRBudgetRecord,
    DRBudgetTracker,
)
from hades.enforcers.geometry_enforcer import GeometryEnforcer
from hades.enforcers.policy_engine import (
    PolicyDecision,
    PolicyProfile,
    PolicyVerdict,
    decide,
)
from hades.enforcers.stop_coverage_enforcer import (
    CrossRouteResolverFn,
    OverpassResolverFn,
    StopCoverageEnforcer,
    StopCoverageThresholds,
)


ROOT = Path(__file__).resolve().parents[2]
PENDING_BATCH_DIR = ROOT / "workspace" / "dr_stop_coverage" / "queries"


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class RouteContext:
    """Minimal payload a caller passes into the coordinator.

    ``coords`` is the route shape as ``(lon, lat)`` tuples; ``stop_coords``
    is the stop list as ``(lat, lon)`` tuples — matches the enforcer APIs
    verbatim so the coordinator does not rotate axes silently.
    """

    route_code: str
    coords: Sequence[tuple[float, float]]
    stop_coords: Sequence[tuple[float, float]]
    stop_ids: Optional[Sequence[str]] = None
    zone: Optional[str] = None
    version: int = 1
    unit_id: str = "unassigned"


@dataclass(slots=True)
class EnforcerResult:
    route_code: str
    version: int
    geometry_report: dict[str, Any]
    stop_coverage_report: dict[str, Any]
    policy_decision: PolicyDecision
    decision_reasons: list[str] = field(default_factory=list)
    policy_flags: dict[str, Any] = field(default_factory=dict)
    dr_queries_queued: list[dict[str, Any]] = field(default_factory=list)
    dr_queries_deferred: list[dict[str, Any]] = field(default_factory=list)
    budget_usage: dict[str, Any] = field(default_factory=dict)
    crashed: bool = False
    crash_payload: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_code": self.route_code,
            "version": int(self.version),
            "policy_decision": self.policy_decision,
            "decision_reasons": list(self.decision_reasons),
            "policy_flags": dict(self.policy_flags),
            "geometry_report": self.geometry_report,
            "stop_coverage_report": self.stop_coverage_report,
            "dr_queries_queued": list(self.dr_queries_queued),
            "dr_queries_deferred": list(self.dr_queries_deferred),
            "budget_usage": dict(self.budget_usage),
            "crashed": self.crashed,
            "crash_payload": self.crash_payload,
        }


DRQueueWriter = Callable[[str, dict[str, Any]], None]


def _default_dr_queue_writer(unit_id: str, query: dict[str, Any]) -> None:
    """Append one DR query to ``pending_batch_<unit_id>.md``.

    The coordinator owns the write; the budget tracker has already
    authorised it. The file format is intentionally compatible with the
    exporter's batch template so the same importer/validator can ingest
    pipeline-queued queries alongside the diagnostic-generated ones.
    """
    PENDING_BATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = PENDING_BATCH_DIR / f"pending_batch_{unit_id}.md"
    header_needed = not path.exists()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    if header_needed:
        lines.extend(
            [
                f"# DR pending batch — unit {unit_id}",
                "",
                "**Type:** Stop Coverage Gap Filling (DR Type 2) — pipeline-queued",
                f"**Started:** {ts}",
                "",
                "> Appended at pipeline time by `phase3_coordinator`. Process with the",
                "> same importer/validator as the diagnostic batches once full.",
                "",
                "## Queries",
                "",
            ]
        )
    # Batch-local Q-numbering is handled by an index-of-appearance marker
    # rather than counting lines in the file on every call. A separate
    # pass before handing to the responder can renumber.
    lines.extend(
        [
            f"### Q-pending — {ts}",
            f"- Route code: {query.get('route_code','?')}",
            f"- Zone: {query.get('zone','?')}",
            f"- Gap index: {query.get('gap_idx','?')}",
            f"- Gap length: {int(query.get('gap_m', 0))}m",
            f"- Gap midpoint: {query.get('midpoint_lat','?')}, {query.get('midpoint_lon','?')}",
            f"- Prompt stub: {query.get('prompt_stub','(none)')}",
            "",
            "Find 1-3 real landmarks where buses actually stop in this specific tramo.",
            "",
            "---",
            "",
        ]
    )
    with path.open("a") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def run_phase3_enforcers(
    route_context: RouteContext,
    *,
    policy_profile: PolicyProfile = "balanced",
    mode: Phase3Mode = "publish",
    unit_dr_budget: Optional[DRBudgetTracker] = None,
    cross_route_resolver: Optional[CrossRouteResolverFn] = None,
    overpass_resolver: Optional[OverpassResolverFn] = None,
    stop_coverage_thresholds: Optional[StopCoverageThresholds] = None,
    dr_queue_writer: DRQueueWriter = _default_dr_queue_writer,
) -> EnforcerResult:
    """Run both enforcers and return the promotion verdict.

    ``mode`` selects the promotion regime:

    - ``publish`` (default) — new route from Constructor V2 or
      canton_pipeline. Verdict is whatever the policy engine decides;
      callers act on it (write / queue / reject) as normal.
    - ``enhance`` — a grandfathered legacy route is being re-analyzed
      with current enforcers + fixers to produce a v2 candidate. Every
      verdict is forced to ``queue_for_approval`` regardless of profile
      because a v1→v2 swap affects production and the operator must
      visually confirm v2 is better than v1. ``reject_send_to_phase2``
      is preserved as-is — it signals the Fixer made the route worse,
      so the worker must NOT write a v2 and instead marks the re-entry
      queue row as ``failed``.

    ``cross_route_resolver`` / ``overpass_resolver`` are passed straight
    through to :class:`StopCoverageEnforcer`. In production the caller
    wires them to the local PostGIS DB + the self-hosted Overpass. In
    unit tests they default to no-op lambdas so no network / DB is
    touched.
    """
    try:
        return _run(
            route_context,
            policy_profile=policy_profile,
            mode=mode,
            unit_dr_budget=unit_dr_budget,
            cross_route_resolver=cross_route_resolver,
            overpass_resolver=overpass_resolver,
            stop_coverage_thresholds=stop_coverage_thresholds,
            dr_queue_writer=dr_queue_writer,
        )
    except Exception as exc:  # noqa: BLE001 - coordinator-level safety net
        crash_payload = {
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(limit=5),
        }
        return EnforcerResult(
            route_code=route_context.route_code,
            version=route_context.version,
            geometry_report={"classification": "unknown", "anomalies": []},
            stop_coverage_report={
                "classification": "unknown",
                "summary": {"n_gaps_total": 0, "tier_usage": {}},
            },
            policy_decision="queue_for_approval",
            decision_reasons=[
                "enforcer_crashed_caught_by_coordinator",
                f"exception:{type(exc).__name__}",
            ],
            policy_flags={"coordinator_fault": True, "mode": mode},
            crashed=True,
            crash_payload=crash_payload,
        )


def _run(
    route_context: RouteContext,
    *,
    policy_profile: PolicyProfile,
    mode: Phase3Mode,
    unit_dr_budget: Optional[DRBudgetTracker],
    cross_route_resolver: Optional[CrossRouteResolverFn],
    overpass_resolver: Optional[OverpassResolverFn],
    stop_coverage_thresholds: Optional[StopCoverageThresholds],
    dr_queue_writer: DRQueueWriter,
) -> EnforcerResult:
    geometry = GeometryEnforcer()
    geometry_report = geometry.analyze(
        route_context.coords,
        route_code=route_context.route_code,
        version=route_context.version,
    )
    geom_dict = geometry_report.to_dict()

    from hades.enforcers.stop_coverage_enforcer import DEFAULT_THRESHOLDS
    sc_enforcer = StopCoverageEnforcer(
        thresholds=stop_coverage_thresholds or DEFAULT_THRESHOLDS,
        cross_route_resolver=cross_route_resolver,
        overpass_resolver=overpass_resolver,
    )
    sc_report = sc_enforcer.analyze(
        route_code=route_context.route_code,
        coords=route_context.coords,
        stop_coords=route_context.stop_coords,
        stop_ids=route_context.stop_ids,
        zone=route_context.zone,
        version=route_context.version,
    )
    sc_dict = sc_report.to_dict()

    verdict = decide(geom_dict, sc_dict, profile=policy_profile)

    tracker = unit_dr_budget or DRBudgetTracker(
        unit_id=route_context.unit_id,
        max_queries_per_unit=DEFAULT_MAX_PER_UNIT,
    )
    queued, deferred = _route_dr_queries_through_budget(
        profile=policy_profile,
        stop_coverage_dict=sc_dict,
        route_context=route_context,
        tracker=tracker,
        dr_queue_writer=dr_queue_writer,
    )

    reasons = list(verdict.reasons)
    if queued:
        reasons.append(f"dr_queued_{len(queued)}")
    if deferred:
        reasons.append(f"dr_deferred_{len(deferred)}")

    # Enhance-mode override: any auto_accept collapses to queue_for_approval.
    # A v1→v2 swap touches production and always needs operator sign-off.
    # reject_send_to_phase2 is preserved — it means the Fixer produced a
    # worse v2 and the worker must NOT write it.
    decision: PolicyDecision = verdict.decision
    flags = dict(verdict.flags)
    flags["mode"] = mode
    if mode == "enhance" and decision == "auto_accept":
        decision = "queue_for_approval"
        reasons.append("enhance_mode_forced_queue")
        flags["enhance_forced_queue"] = True

    return EnforcerResult(
        route_code=route_context.route_code,
        version=route_context.version,
        geometry_report=geom_dict,
        stop_coverage_report=sc_dict,
        policy_decision=decision,
        decision_reasons=reasons,
        policy_flags=flags,
        dr_queries_queued=queued,
        dr_queries_deferred=deferred,
        budget_usage=tracker.get_unit_usage(),
    )


# ---------------------------------------------------------------------------
# DR routing
# ---------------------------------------------------------------------------

def _should_queue_for_profile(profile: PolicyProfile, classification: str) -> bool:
    """Return True iff the coordinator should attempt to book DR queries
    through the budget gate for this profile + stop classification.

    Prompt contract:
      - conservative → never queue DR pipeline-side (queue_for_approval
        handles the decision manually);
      - balanced    → queue DR for degraded / unroutable;
      - aggressive_supervised → never queue DR pipeline-side.
    """
    if profile != "balanced":
        return False
    return classification in {"degraded", "unroutable"}


def _route_dr_queries_through_budget(
    *,
    profile: PolicyProfile,
    stop_coverage_dict: dict[str, Any],
    route_context: RouteContext,
    tracker: DRBudgetTracker,
    dr_queue_writer: DRQueueWriter,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    classification = str(stop_coverage_dict.get("classification", "")).lower()
    prepared = stop_coverage_dict.get("dr_queries_prepared") or []
    gaps = stop_coverage_dict.get("gaps") or []
    gap_by_idx: dict[int, dict[str, Any]] = {int(g.get("idx", -1)): g for g in gaps}

    queued: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []

    if not _should_queue_for_profile(profile, classification):
        for q in prepared:
            deferred.append({**q, "deferred_reason": f"profile_{profile}_no_pipeline_dr"})
        return queued, deferred

    for q in prepared:
        gap_idx = int(q.get("gap_idx", -1))
        gap = gap_by_idx.get(gap_idx) or {
            "idx": gap_idx,
            "gap_m": q.get("gap_m", 0.0),
            "zone": q.get("zone"),
        }
        allow, reason = tracker.should_queue_dr(
            gap=gap,
            route_code=route_context.route_code,
            zone=q.get("zone") or gap.get("zone"),
        )
        if not allow:
            deferred.append({**q, "deferred_reason": reason})
            continue
        tracker.record_queue(gap, q, route_code=route_context.route_code)
        queued.append(dict(q))
        try:
            dr_queue_writer(route_context.unit_id, q)
        except Exception as exc:  # noqa: BLE001 - writer must not sink a promotion
            deferred.append(
                {
                    **q,
                    "deferred_reason": f"dr_queue_writer_failed:{type(exc).__name__}",
                }
            )
    return queued, deferred


__all__ = [
    "EnforcerResult",
    "Phase3Mode",
    "RouteContext",
    "run_phase3_enforcers",
    "DRQueueWriter",
    "PENDING_BATCH_DIR",
]
