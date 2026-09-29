#!/usr/bin/env python3
"""Post-process: re-score all routes in the tuned artifact with the new scorer.

Routes kept via fallback still have old confidence scores. This script
re-scores them using the updated penalty weights and thresholds, then
updates classifications and auto_accept flags.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
ARTIFACT = REPO_ROOT / "constructor_artifacts" / "valle_v2_TUNED_39.json"

# Must match the new values in constants.py and confidence_scorer.py
MAX_REPEAT_SEGMENT_RATIO = 0.12
SOLUTION_DIVERGENCE_PENALTY = 3.0

CORE_VALIDATIONS = frozenset({"anchor_respect", "detour_ratio", "monotonic_progress"})
PROXY_VALIDATIONS = frozenset({"corridor_consistency", "repeated_segments"})

TOPOLOGY_OVERRIDES = {
    "El Colibri - Loreto (interno)": "linear",
}


def _importance(name: str) -> str:
    if name in CORE_VALIDATIONS:
        return "core"
    if name in PROXY_VALIDATIONS:
        return "proxy"
    return "supporting"


def _penalty(status: str, score: float, importance: str) -> float:
    if status == "fail":
        if importance == "core":
            return (1.0 - score) * 22.0 + 9.0
        if importance == "proxy":
            return (1.0 - score) * 9.0 + 3.0
        return (1.0 - score) * 12.0 + 5.0
    if status == "warn":
        if importance == "core":
            return (1.0 - score) * 10.0 + 3.0
        if importance == "proxy":
            return (1.0 - score) * 4.0 + 1.5
        return (1.0 - score) * 6.0 + 2.0
    if importance == "core":
        return (1.0 - score) * 4.0
    if importance == "proxy":
        return (1.0 - score) * 1.5
    return (1.0 - score) * 3.0


def _rescore_repeat(vdata: dict, route_type: str) -> tuple[str, float]:
    """Re-compute repeated_segments status and score with new threshold."""
    repeat_ratio = 0.0
    for issue in vdata.get("issues", []):
        m = re.search(r"repeated segment ratio ([\d.]+)", issue)
        if m:
            repeat_ratio = float(m.group(1))
            break
    if not repeat_ratio:
        return vdata["status"], vdata["score"]

    allowed = MAX_REPEAT_SEGMENT_RATIO * (1.8 if "loop" in route_type else 1.0)
    warn_ratio = allowed * 0.75
    fail_ratio = allowed * (2.2 if "loop" in route_type else 1.8)

    if repeat_ratio > fail_ratio:
        status = "fail"
    elif repeat_ratio > warn_ratio:
        status = "warn"
    else:
        status = "pass"

    if repeat_ratio <= warn_ratio:
        score = max(0.0, 1.0 - 0.2 * (repeat_ratio / max(warn_ratio, 1e-6)))
    else:
        excess = (repeat_ratio - warn_ratio) / max(fail_ratio - warn_ratio, 1e-6)
        score = max(0.0, 0.8 - (0.8 * min(excess, 1.0)))

    return status, score


def classify(score: float, topology: str) -> str:
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
    """Re-score a route and update its classification."""
    validation = route.get("validation") or {}
    old_conf = route["confidence"]
    route_type = route.get("route_type") or route.get("topology") or "linear"
    topology = route.get("topology", "linear")

    # Apply topology override
    if route["route"] in TOPOLOGY_OVERRIDES:
        topology = TOPOLOGY_OVERRIDES[route["route"]]
        route["topology"] = topology

    # Compute new score
    score = 100.0
    for name, vdata in validation.items():
        status = vdata["status"]
        vscore = vdata["score"]

        # Re-score repeated_segments with new threshold
        if name == "repeated_segments":
            status, vscore = _rescore_repeat(vdata, route_type)

        score -= _penalty(status, vscore, _importance(name))

    # Apply divergence penalty
    has_divergence = any("diverged materially" in r for r in old_conf.get("reasons", []))
    if has_divergence:
        score -= SOLUTION_DIVERGENCE_PENALTY

    score = max(0.0, min(100.0, score))
    new_cls = classify(score, topology)
    new_auto = new_cls in ("strong", "acceptable")

    return {
        "new_score": score,
        "new_cls": new_cls,
        "new_auto": new_auto,
        "old_score": old_conf["score"],
        "old_cls": route["classification"],
    }


def main():
    data = json.loads(ARTIFACT.read_text())
    routes = data["routes"]

    print(f"Re-scoring {len(routes)} routes in {ARTIFACT.name}")
    print()

    changes = 0
    for route in routes:
        result = rescore_route(route)

        if abs(result["new_score"] - result["old_score"]) > 0.1 or result["new_cls"] != result["old_cls"]:
            print(f"  {route['route']:<55s} {result['old_score']:5.1f} → {result['new_score']:5.1f}  "
                  f"{result['old_cls']:>22s} → {result['new_cls']}")

            # Update the route in the artifact
            route["confidence"]["score"] = result["new_score"]
            route["classification"] = result["new_cls"]
            route["auto_accept"] = result["new_auto"]
            changes += 1

    # Update summary
    strong = [r for r in routes if r["classification"] == "strong"]
    acceptable = [r for r in routes if r["classification"] == "acceptable"]
    ambiguous = [r for r in routes if r["classification"] == "ambiguous"]
    manual = [r for r in routes if r["classification"] == "needs_manual_review"]
    special = [r for r in routes if r["classification"] == "special_case"]

    avg_conf = sum(r["confidence"]["score"] for r in routes) / len(routes)

    data["summary"]["auto_accepted"] = len(strong) + len(acceptable)
    data["summary"]["classification_breakdown"] = {
        "strong": len(strong), "acceptable": len(acceptable),
        "ambiguous": len(ambiguous), "needs_manual_review": len(manual),
        "special_case": len(special),
    }
    data["summary"]["avg_confidence"] = round(avg_conf, 1)

    ARTIFACT.write_text(json.dumps(data, indent=2, default=str))

    print()
    print(f"Updated {changes} routes")
    print(f"Auto-accepted: {len(strong) + len(acceptable)} ({len(strong)} strong + {len(acceptable)} acceptable)")
    print(f"Ambiguous: {len(ambiguous)}")
    print(f"Manual review: {len(manual)}")
    print(f"Special case: {len(special)}")
    print(f"Avg confidence: {avg_conf:.1f}")


if __name__ == "__main__":
    main()
