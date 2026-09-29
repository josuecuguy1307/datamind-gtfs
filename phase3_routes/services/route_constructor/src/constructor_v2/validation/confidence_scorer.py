from __future__ import annotations

from typing import Mapping

from src.constructor_v2.schemas.route_output import ConfidenceResult, ValidationResult


CORE_VALIDATIONS = frozenset({"anchor_respect", "detour_ratio", "monotonic_progress"})
PROXY_VALIDATIONS = frozenset({"corridor_consistency", "repeated_segments"})


def _importance(name: str) -> str:
    if name in CORE_VALIDATIONS:
        return "core"
    if name in PROXY_VALIDATIONS:
        return "proxy"
    return "supporting"


def _score_penalty(validation: ValidationResult) -> float:
    importance = _importance(validation.name)
    if validation.status == "fail":
        if importance == "core":
            return (1.0 - validation.score) * 22.0 + 9.0
        if importance == "proxy":
            return (1.0 - validation.score) * 9.0 + 3.0
        return (1.0 - validation.score) * 12.0 + 5.0
    if validation.status == "warn":
        if importance == "core":
            return (1.0 - validation.score) * 10.0 + 3.0
        if importance == "proxy":
            return (1.0 - validation.score) * 4.0 + 1.5
        return (1.0 - validation.score) * 6.0 + 2.0
    if importance == "core":
        return (1.0 - validation.score) * 4.0
    if importance == "proxy":
        return (1.0 - validation.score) * 1.5
    return (1.0 - validation.score) * 3.0


def score_confidence(
    validations: Mapping[str, ValidationResult],
    *,
    diagnostics: dict | None = None,
) -> ConfidenceResult:
    score = 100.0
    reasons: list[str] = []
    fail_count = 0
    core_fail_count = 0
    proxy_fail_count = 0
    warn_count = 0
    for validation in validations.values():
        if validation.status == "fail":
            fail_count += 1
            if validation.name in CORE_VALIDATIONS:
                core_fail_count += 1
            elif validation.name in PROXY_VALIDATIONS:
                proxy_fail_count += 1
            score -= _score_penalty(validation)
            reasons.extend(validation.issues[:2])
        elif validation.status == "warn":
            warn_count += 1
            score -= _score_penalty(validation)
            reasons.extend(validation.issues[:1])
        else:
            score -= _score_penalty(validation)

    ambiguity = False
    if diagnostics and diagnostics.get("solution_divergence"):
        ambiguity = True
        score -= 3.0
        reasons.append("baseline and matrix solutions diverged materially")

    anchor_failed = validations["anchor_respect"].status == "fail"
    monotonic_failed = validations["monotonic_progress"].status == "fail"
    detour_failed = validations["detour_ratio"].status == "fail"
    label = "strong"
    if anchor_failed or monotonic_failed or core_fail_count >= 2 or score < 35.0:
        label = "needs_manual_review"
    elif ambiguity and score < 80.0:
        label = "ambiguous"
    elif core_fail_count or proxy_fail_count >= 2 or score < 60.0:
        label = "weak"
    elif warn_count >= 2 or score < 82.0:
        label = "acceptable"

    auto_accept = (
        label in {"strong", "acceptable"}
        and not anchor_failed
        and not monotonic_failed
        and not detour_failed
    )
    return ConfidenceResult(
        label=label,
        score=max(0.0, min(100.0, score)),
        auto_accept=auto_accept,
        reasons=reasons[:6],
        metrics={
            "fail_count": fail_count,
            "core_fail_count": core_fail_count,
            "proxy_fail_count": proxy_fail_count,
            "warn_count": warn_count,
            "ambiguity": ambiguity,
        },
    )
