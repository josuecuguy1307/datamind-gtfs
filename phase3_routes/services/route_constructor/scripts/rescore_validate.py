#!/usr/bin/env python3
"""Re-score all routes using updated confidence scorer, without re-running solver.

Reads existing validation results from the artifact and re-computes confidence
scores with modified penalty weights and thresholds. Inline scorer to avoid
importing the full constructor_v2 package (which needs ortools).
"""
from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
ARTIFACT = REPO_ROOT / "constructor_artifacts" / "valle_v2_ENRICHED_39.json"

# ── Updated thresholds ─────────────────────────────────────────────────
# These MUST match what's in constants.py and confidence_scorer.py
MAX_REPEAT_SEGMENT_RATIO = 0.12  # was 0.08
SOLUTION_DIVERGENCE_PENALTY = 3.0  # was 8.0


# ── Inline scorer (mirrors confidence_scorer.py with new values) ───────

CORE_VALIDATIONS = frozenset({"anchor_respect", "detour_ratio", "monotonic_progress"})
PROXY_VALIDATIONS = frozenset({"corridor_consistency", "repeated_segments"})


@dataclass
class ValidationResult:
    name: str
    status: str
    score: float
    metrics: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass
class ConfidenceResult:
    label: str
    score: float
    auto_accept: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


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


def _rescore_repeat_segment(vdata: dict, route_type: str) -> ValidationResult:
    """Re-run repeated_segments scoring with new MAX_REPEAT_SEGMENT_RATIO."""
    import re as _re
    metrics = vdata.get("metrics") or {}
    repeat_ratio = metrics.get("repeat_ratio", 0.0)

    # Parse ratio from issues text if not in metrics
    if not repeat_ratio:
        for issue in vdata.get("issues", []):
            m = _re.search(r"repeated segment ratio ([\d.]+)", issue)
            if m:
                repeat_ratio = float(m.group(1))
                break

    if not repeat_ratio:
        return ValidationResult(
            name="repeated_segments",
            status=vdata["status"],
            score=vdata["score"],
            issues=vdata.get("issues", []),
        )

    allowed_ratio = MAX_REPEAT_SEGMENT_RATIO * (1.8 if "loop" in route_type else 1.0)
    warn_ratio = allowed_ratio * 0.75
    fail_ratio = allowed_ratio * (2.2 if "loop" in route_type else 1.8)

    status = "pass"
    issues: list[str] = []
    if repeat_ratio > fail_ratio:
        status = "fail"
        issues.append(f"repeated segment ratio {repeat_ratio:.3f} exceeds {fail_ratio:.3f}")
    elif repeat_ratio > warn_ratio:
        status = "warn"
        issues.append(f"repeated segment ratio {repeat_ratio:.3f} is elevated")

    if repeat_ratio <= warn_ratio:
        score = max(0.0, 1.0 - 0.2 * (repeat_ratio / max(warn_ratio, 1e-6)))
    else:
        excess = (repeat_ratio - warn_ratio) / max(fail_ratio - warn_ratio, 1e-6)
        score = max(0.0, 0.8 - (0.8 * min(excess, 1.0)))

    return ValidationResult(
        name="repeated_segments",
        status=status,
        score=score,
        issues=issues,
    )


def score_confidence(
    validations: dict[str, ValidationResult],
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
        score -= SOLUTION_DIVERGENCE_PENALTY
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


def classify_confidence(score: float, topology: str) -> str:
    if topology != "linear":
        return "special_case"
    if score >= 85:
        return "strong"
    if score >= 70:
        return "acceptable"
    if score >= 50:
        return "ambiguous"
    return "needs_manual_review"


def rescore_route(route: dict) -> dict:
    validation_data = route.get("validation") or {}
    old_conf = route["confidence"]
    route_type = route.get("route_type") or route.get("topology") or "linear"

    # Reconstruct ValidationResult objects, re-scoring repeated_segments
    validations = {}
    for name, vdata in validation_data.items():
        if name == "repeated_segments":
            validations[name] = _rescore_repeat_segment(vdata, route_type)
        else:
            validations[name] = ValidationResult(
                name=name,
                status=vdata["status"],
                score=vdata["score"],
                metrics=vdata.get("metrics", {}),
                issues=vdata.get("issues", []),
            )

    # Reconstruct diagnostics
    diagnostics = {}
    if any("diverged materially" in r for r in old_conf.get("reasons", [])):
        diagnostics["solution_divergence"] = True

    new_conf = score_confidence(validations, diagnostics=diagnostics)
    topology = route.get("topology", "linear")
    new_cls = classify_confidence(new_conf.score, topology)

    return {
        "route": route["route"],
        "topology": topology,
        "route_type": route_type,
        "old_score": old_conf["score"],
        "new_score": new_conf.score,
        "delta": round(new_conf.score - old_conf["score"], 2),
        "old_label": old_conf["label"],
        "new_label": new_conf.label,
        "old_classification": route["classification"],
        "new_classification": new_cls,
        "new_auto_accept": new_conf.auto_accept,
        "had_divergence": diagnostics.get("solution_divergence", False),
        "new_confidence": new_conf,
        "new_validations": {
            name: {"status": v.status, "score": v.score, "issues": v.issues}
            for name, v in validations.items()
        },
    }


def main():
    data = json.loads(ARTIFACT.read_text())
    routes = data["routes"]

    print(f"Re-scoring {len(routes)} routes")
    print(f"MAX_REPEAT_SEGMENT_RATIO = {MAX_REPEAT_SEGMENT_RATIO} (was 0.08)")
    print(f"Solution divergence penalty = -{SOLUTION_DIVERGENCE_PENALTY} (was -8.0)")
    print()

    results = []
    for r in routes:
        results.append(rescore_route(r))

    results.sort(key=lambda x: x["delta"], reverse=True)

    print(f"{'Route':<55s} {'Old':>5s} {'New':>5s} {'Δ':>6s} {'Old Class':>22s} → {'New Class':<22s}")
    print("─" * 130)

    upgrades = []
    regressions = []

    for r in results:
        old_cls = r["old_classification"]
        new_cls = r["new_classification"]
        marker = ""
        if new_cls in ("strong", "acceptable") and old_cls not in ("strong", "acceptable"):
            marker = " ★ RESCUED"
            upgrades.append(r)
        elif new_cls not in ("strong", "acceptable") and old_cls in ("strong", "acceptable"):
            marker = " ✗ REGRESSION"
            regressions.append(r)
        elif r["delta"] < -2.0 and old_cls in ("strong", "acceptable"):
            marker = " ⚠ LOST >2pts"
            regressions.append(r)

        print(f"  {r['route']:<53s} {r['old_score']:5.1f} {r['new_score']:5.1f} {r['delta']:+6.1f}   {old_cls:>22s} → {new_cls:<22s}{marker}")

    old_auto = sum(1 for r in results if r["old_classification"] in ("strong", "acceptable"))
    new_auto = sum(1 for r in results if r["new_classification"] in ("strong", "acceptable"))
    old_strong = sum(1 for r in results if r["old_classification"] == "strong")
    new_strong = sum(1 for r in results if r["new_classification"] == "strong")

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Auto-accepted:  {old_auto} → {new_auto}  ({new_auto - old_auto:+d})")
    print(f"    Strong:       {old_strong} → {new_strong}  ({new_strong - old_strong:+d})")
    print(f"    Acceptable:   {old_auto - old_strong} → {new_auto - new_strong}  ({(new_auto - new_strong) - (old_auto - old_strong):+d})")

    if upgrades:
        print(f"\n  ★ RESCUED ({len(upgrades)}):")
        for r in upgrades:
            print(f"    {r['route']}: {r['old_classification']} ({r['old_score']:.1f}) → {r['new_classification']} ({r['new_score']:.1f})")

    if regressions:
        print(f"\n  ✗ REGRESSIONS ({len(regressions)}):")
        for r in regressions:
            print(f"    {r['route']}: {r['old_classification']} ({r['old_score']:.1f}) → {r['new_classification']} ({r['new_score']:.1f})")
    else:
        print("\n  ✓ No regressions. All currently-accepted routes maintained or improved.")

    remaining_ambig = [r for r in results if r["new_classification"] == "ambiguous"]
    if remaining_ambig:
        print(f"\n  Still ambiguous ({len(remaining_ambig)}):")
        for r in sorted(remaining_ambig, key=lambda x: x["new_score"], reverse=True):
            gap = 70 - r["new_score"]
            print(f"    {r['route']}: {r['new_score']:.1f} (gap to 70: {gap:.1f})")

    remaining_manual = [r for r in results if r["new_classification"] == "needs_manual_review"]
    if remaining_manual:
        print(f"\n  Still needs_manual_review ({len(remaining_manual)}):")
        for r in sorted(remaining_manual, key=lambda x: x["new_score"], reverse=True):
            print(f"    {r['route']}: {r['new_score']:.1f}")


if __name__ == "__main__":
    main()
