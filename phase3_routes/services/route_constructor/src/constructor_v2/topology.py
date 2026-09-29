from __future__ import annotations

from src.constructor_v2.schemas.route_output import ConfidenceResult


LINEAR_ROUTE_TYPES = frozenset({"lineal", "linear", "internal_linear"})
EDGE_CASE_ROUTE_TYPES = frozenset({"internal_loop", "possible_circular"})


def canonical_topology(route_type: str | None) -> str:
    normalized = (route_type or "").strip().lower()
    if normalized in EDGE_CASE_ROUTE_TYPES:
        return normalized
    if normalized in LINEAR_ROUTE_TYPES or not normalized:
        return "linear"
    return "linear"


def is_special_case_topology(route_type: str | None) -> bool:
    return canonical_topology(route_type) != "linear"


def topology_strategy(route_type: str | None) -> str:
    if is_special_case_topology(route_type):
        return "special_case_preserve_sequence"
    return "linear_ordering_backbone"


def apply_topology_confidence_policy(confidence: ConfidenceResult, route_type: str | None) -> ConfidenceResult:
    topology = canonical_topology(route_type)
    if topology == "linear":
        return confidence

    reasons = list(confidence.reasons)
    reasons.append(f"{topology} topology kept in special-case mode pending dedicated handling")
    metrics = dict(confidence.metrics)
    metrics.update(
        {
            "topology": topology,
            "topology_special_case": True,
        }
    )

    if confidence.label == "needs_manual_review":
        return ConfidenceResult(
            label=confidence.label,
            score=confidence.score,
            auto_accept=False,
            reasons=reasons[:6],
            metrics=metrics,
        )

    return ConfidenceResult(
        label="ambiguous",
        score=min(confidence.score, 79.0),
        auto_accept=False,
        reasons=reasons[:6],
        metrics=metrics,
    )
