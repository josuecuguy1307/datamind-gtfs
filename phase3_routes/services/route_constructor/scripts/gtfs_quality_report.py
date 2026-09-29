#!/usr/bin/env python3
"""
GTFS Quality Report: Compare corridor-constrained construction results
against confirmed GTFS ground truth.

Usage:
    python gtfs_quality_report.py

Inputs (hardcoded paths relative to repo root):
    - constructor_artifacts/valle_v2_CONSTRUCTED_39.json
    - phase3_routes/.../references/confirmed_routes.json
    - phase3_routes/.../references/confirmed_corridors.json

Output:
    - constructor_artifacts/valle_v2_CONSTRUCTED_39/gtfs_quality_report.md
"""

import json
import math
import sys
from pathlib import Path
from datetime import datetime, timezone
from itertools import combinations

# ── paths ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[4]  # ML DATAMIND
CONSTRUCTED_PATH = ROOT / "constructor_artifacts" / "valle_v2_ENRICHED_39.json"
CONFIRMED_ROUTES_PATH = (
    ROOT / "phase3_routes" / "services" / "route_constructor"
    / "src" / "constructor_v2" / "references" / "confirmed_routes.json"
)
CONFIRMED_CORRIDORS_PATH = (
    ROOT / "phase3_routes" / "services" / "route_constructor"
    / "src" / "constructor_v2" / "references" / "confirmed_corridors.json"
)
OUTPUT_DIR = ROOT / "constructor_artifacts" / "valle_v2_ENRICHED_39"
OUTPUT_PATH = OUTPUT_DIR / "gtfs_quality_report.md"

# ── before-corridors baseline (hardcoded from prior run) ─────────────
BEFORE = {
    "auto_accepted": 19,
    "strong": 10,
    "avg_confidence": 66.8,
    "avg_agreement": 0.937,
    "total_routes": 39,
    "classification": {
        "strong": 10,
        "acceptable": 9,
        "ambiguous": 7,
        "needs_manual_review": 11,
        "special_case": 2,
    },
}


# ── haversine ────────────────────────────────────────────────────────────
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in metres."""
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── proximity matching ───────────────────────────────────────────────────
MATCH_RADIUS_M = 120  # maximum distance to consider a stop "matched"


def match_stops_by_proximity(
    constructed: list[dict], confirmed: list[dict]
) -> list[tuple[dict, dict]]:
    """Match constructed stops to nearest confirmed stop within MATCH_RADIUS_M.
    Returns list of (constructed_stop, confirmed_stop) pairs.
    Uses greedy nearest-first to avoid double-matching."""
    pairs = []
    used_confirmed = set()
    # Build distance matrix for candidates
    candidates = []
    for cs in constructed:
        for gs in confirmed:
            d = haversine_m(cs["lat"], cs["lon"], gs["lat"], gs["lon"])
            if d <= MATCH_RADIUS_M:
                candidates.append((d, cs, gs))
    candidates.sort(key=lambda x: x[0])
    used_constructed = set()
    for d, cs, gs in candidates:
        cid = cs.get("stop_id", id(cs))
        gid = gs.get("stop_id", id(gs))
        if cid not in used_constructed and gid not in used_confirmed:
            pairs.append((cs, gs))
            used_constructed.add(cid)
            used_confirmed.add(gid)
    return pairs


# ── order agreement ──────────────────────────────────────────────────────
def order_agreement(
    constructed: list[dict], confirmed: list[dict], pairs: list[tuple[dict, dict]]
) -> float:
    """Pairwise order agreement: for each pair of matched stops,
    check if their relative order in constructed matches confirmed."""
    if len(pairs) < 2:
        return 1.0 if len(pairs) == 1 else 0.0

    # Build seq maps
    c_seq = {}  # stop_id -> seq in constructed
    for i, s in enumerate(constructed):
        c_seq[s.get("stop_id", id(s))] = i
    g_seq = {}  # stop_id -> seq in confirmed
    for i, s in enumerate(confirmed):
        g_seq[s.get("stop_id", id(s))] = i

    # For matched pairs, use confirmed stop_id as key
    matched = []
    for cs, gs in pairs:
        cid = cs.get("stop_id", id(cs))
        gid = gs.get("stop_id", id(gs))
        matched.append((c_seq[cid], g_seq[gid]))

    concordant = 0
    total = 0
    for (c1, g1), (c2, g2) in combinations(matched, 2):
        total += 1
        # same relative order?
        if (c1 < c2) == (g1 < g2):
            concordant += 1

    return concordant / total if total > 0 else 1.0


# ── stop coverage ────────────────────────────────────────────────────────
def stop_coverage(confirmed: list[dict], pairs: list[tuple[dict, dict]]) -> float:
    """Fraction of confirmed stops matched."""
    if not confirmed:
        return 0.0
    return len(pairs) / len(confirmed)


# ── shape distance (modified Hausdorff-like) ─────────────────────────────
def directed_hausdorff_sample(
    coords_a: list[list[float]], coords_b: list[list[float]], sample_step: int = 3
) -> float:
    """Sampled directed Hausdorff: for each point in A (sampled),
    find min distance to any point in B.  Return mean of those mins.
    coords are [lon, lat]."""
    if not coords_a or not coords_b:
        return float("inf")
    dists = []
    for i in range(0, len(coords_a), sample_step):
        lon_a, lat_a = coords_a[i]
        min_d = float("inf")
        for j in range(0, len(coords_b), sample_step):
            lon_b, lat_b = coords_b[j]
            d = haversine_m(lat_a, lon_a, lat_b, lon_b)
            if d < min_d:
                min_d = d
        dists.append(min_d)
    return sum(dists) / len(dists) if dists else float("inf")


def symmetric_mean_hausdorff(
    coords_a: list[list[float]], coords_b: list[list[float]]
) -> float:
    d_ab = directed_hausdorff_sample(coords_a, coords_b)
    d_ba = directed_hausdorff_sample(coords_b, coords_a)
    return max(d_ab, d_ba)


# ── build shape from constructed stops ───────────────────────────────────
def stops_to_coords(stops: list[dict]) -> list[list[float]]:
    """Convert ordered stops to [lon, lat] list for shape comparison."""
    return [[s["lon"], s["lat"]] for s in stops]


# ── best direction match ─────────────────────────────────────────────────
def find_best_confirmed_direction(
    route_code: str, confirmed_routes: list[dict]
) -> list[dict]:
    """Find the two direction entries for a route_code, return both."""
    return [r for r in confirmed_routes if r["route_code"] == route_code]


def best_direction_match(
    constructed_stops: list[dict], candidates: list[dict]
) -> tuple[dict, list[tuple[dict, dict]], float, bool]:
    """Pick the confirmed direction that maximises order agreement.

    Tries both directions AND both orientations (forward/reverse of
    constructed stops) and returns the combination with the highest
    composite score (order_agreement * 10 + coverage).

    Returns (best_candidate, pairs, composite_score, was_reversed).
    """
    best = None
    best_pairs: list[tuple[dict, dict]] = []
    best_composite = -1.0
    best_reversed = False

    for cand in candidates:
        gtfs_stops = cand["ordered_stops"]
        for reversed_constructed in (False, True):
            c_stops = list(reversed(constructed_stops)) if reversed_constructed else constructed_stops
            pairs = match_stops_by_proximity(c_stops, gtfs_stops)
            if len(pairs) < 2:
                composite = len(pairs) * 0.01
            else:
                agree = order_agreement(c_stops, gtfs_stops, pairs)
                coverage = len(pairs) / max(1, len(gtfs_stops))
                composite = agree * 10 + coverage
            if composite > best_composite:
                best = cand
                best_pairs = pairs
                best_composite = composite
                best_reversed = reversed_constructed

    return best, best_pairs, best_composite, best_reversed


# ══════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════
def main():
    # ── load data ─────────────────────────────────────────────────────────
    constructed = json.loads(CONSTRUCTED_PATH.read_text())
    confirmed_routes_data = json.loads(CONFIRMED_ROUTES_PATH.read_text())
    corridors_data = json.loads(CONFIRMED_CORRIDORS_PATH.read_text())

    confirmed_routes = confirmed_routes_data["routes"]
    benchmark = corridors_data["benchmark_mapping"]

    after = constructed["summary"]
    routes = constructed["routes"]

    # Index constructed routes by name
    route_by_name = {r["route"]: r for r in routes}

    # ── per-route GTFS comparison ─────────────────────────────────────────
    route_results = []
    for route_name, mapping in benchmark.items():
        code = mapping.get("confirmed_route")
        if not code:
            continue
        if route_name not in route_by_name:
            continue

        cr = route_by_name[route_name]
        constructed_stops = cr["ordered_stops"]

        # Find best direction match
        candidates = find_best_confirmed_direction(code, confirmed_routes)
        if not candidates:
            continue

        best_dir, pairs, _, was_reversed = best_direction_match(constructed_stops, candidates)
        if best_dir is None:
            continue

        gtfs_stops = best_dir["ordered_stops"]
        effective_stops = list(reversed(constructed_stops)) if was_reversed else constructed_stops

        agree = order_agreement(effective_stops, gtfs_stops, pairs)
        coverage = stop_coverage(gtfs_stops, pairs)

        # Shape comparison
        shape_dist = None
        if best_dir.get("shape_coords"):
            constructed_coords = stops_to_coords(effective_stops)
            shape_dist = symmetric_mean_hausdorff(
                constructed_coords, best_dir["shape_coords"]
            )

        route_results.append(
            {
                "route_name": route_name,
                "gtfs_code": code,
                "direction_id": best_dir["direction_id"],
                "direction_reversed": was_reversed,
                "classification": cr["classification"],
                "confidence": cr["confidence"]["score"],
                "auto_accept": cr["auto_accept"],
                "stops_constructed": cr["stops_constructed"],
                "stops_gtfs": best_dir["stop_count"],
                "stops_matched": len(pairs),
                "stop_coverage": coverage,
                "order_agreement": agree,
                "shape_mean_hausdorff_m": shape_dist,
                "corridors_used": mapping.get("corridors", []),
            }
        )

    route_results.sort(key=lambda r: r["order_agreement"], reverse=True)

    # ── aggregate metrics ─────────────────────────────────────────────────
    if route_results:
        avg_agree = sum(r["order_agreement"] for r in route_results) / len(route_results)
        avg_coverage = sum(r["stop_coverage"] for r in route_results) / len(route_results)
        avg_hausdorff = [
            r["shape_mean_hausdorff_m"]
            for r in route_results
            if r["shape_mean_hausdorff_m"] is not None
        ]
        avg_hd = sum(avg_hausdorff) / len(avg_hausdorff) if avg_hausdorff else None
    else:
        avg_agree = avg_coverage = 0.0
        avg_hd = None

    # ── identify improvements ─────────────────────────────────────────────
    improved = [
        r for r in route_results if len(r["corridors_used"]) > 0
    ]
    no_corridor = [
        r for r in route_results if len(r["corridors_used"]) == 0
    ]

    # ── routes ready for GTFS ─────────────────────────────────────────────
    ready_routes = [
        r
        for r in routes
        if r["classification"] in ("strong", "acceptable")
    ]

    # ══════════════════════════════════════════════════════════════════════
    #  BUILD REPORT
    # ══════════════════════════════════════════════════════════════════════
    lines = []
    w = lines.append

    w("# GTFS Quality Report: Constructor v2 Corridor-Constrained Results")
    w("")
    w(f"**Generated**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    w(f"**Artifact**: `{CONSTRUCTED_PATH.name}`")
    w(f"**Ground truth**: `{CONFIRMED_ROUTES_PATH.name}` ({confirmed_routes_data['route_count']} confirmed GTFS route entries)")
    w(f"**Benchmark routes matched**: {len(route_results)} routes with confirmed GTFS ground truth")
    w("")

    # ── Section A: Before/After ───────────────────────────────────────────
    w("---")
    w("## A. Before / After Corridor Constraints")
    w("")
    w("| Metric | Before (no corridors) | After (with corridors) | Delta |")
    w("|--------|----------------------|----------------------|-------|")

    def delta(a, b, fmt=".1f", pct=False):
        d = b - a
        sign = "+" if d > 0 else ""
        if pct:
            return f"{sign}{d:{fmt}}%"
        return f"{sign}{d:{fmt}}"

    w(f"| Total routes | {BEFORE['total_routes']} | {after['total_routes']} | {delta(BEFORE['total_routes'], after['total_routes'], 'd')} |")
    w(f"| Auto-accepted | {BEFORE['auto_accepted']} | {after['auto_accepted']} | {delta(BEFORE['auto_accepted'], after['auto_accepted'], 'd')} |")
    w(f"| Strong | {BEFORE['strong']} | {after['classification_breakdown']['strong']} | {delta(BEFORE['strong'], after['classification_breakdown']['strong'], 'd')} |")
    w(f"| Avg confidence | {BEFORE['avg_confidence']:.1f}% | {after['avg_confidence']:.1f}% | {delta(BEFORE['avg_confidence'], after['avg_confidence'])} |")
    w(f"| Avg agreement | {BEFORE['avg_agreement']:.3f} | {after['avg_reference_agreement']:.3f} | {delta(BEFORE['avg_agreement'], after['avg_reference_agreement'], '.3f')} |")
    w("")
    w("**Classification shift:**")
    w("")
    w("| Class | Before | After | Delta |")
    w("|-------|--------|-------|-------|")
    for cls in ("strong", "acceptable", "ambiguous", "needs_manual_review", "special_case"):
        bv = BEFORE["classification"].get(cls, 0)
        av = after["classification_breakdown"].get(cls, 0)
        w(f"| {cls} | {bv} | {av} | {delta(bv, av, 'd')} |")
    w("")

    # ── Section B: Per-route quality ──────────────────────────────────────
    w("---")
    w("## B. Per-Route Quality vs Confirmed GTFS Ground Truth")
    w("")
    w(f"**Avg order agreement**: {avg_agree:.3f}")
    w(f"**Avg stop coverage**: {avg_coverage:.1%}")
    if avg_hd is not None:
        w(f"**Avg shape distance (mean Hausdorff)**: {avg_hd:.0f} m")
    w("")
    w("| Route | GTFS | Dir | Class | Conf | Stops Built/GTFS | Matched | Coverage | Order Agree | Shape Dist (m) |")
    w("|-------|------|-----|-------|------|-----------------|---------|----------|------------|---------------|")
    for r in route_results:
        hd = f"{r['shape_mean_hausdorff_m']:.0f}" if r["shape_mean_hausdorff_m"] is not None else "n/a"
        agree_icon = "**" if r["order_agreement"] >= 0.95 else ""
        rev_flag = " (rev)" if r.get("direction_reversed") else ""
        w(
            f"| {r['route_name']} | {r['gtfs_code']} d{r['direction_id']}{rev_flag} "
            f"| {'rev' if r.get('direction_reversed') else 'fwd'} "
            f"| {r['classification']} | {r['confidence']:.1f} "
            f"| {r['stops_constructed']}/{r['stops_gtfs']} "
            f"| {r['stops_matched']} "
            f"| {r['stop_coverage']:.0%} "
            f"| {agree_icon}{r['order_agreement']:.3f}{agree_icon} "
            f"| {hd} |"
        )
    w("")

    # Per-route detail
    w("### B.1 Detail per Matched Route")
    w("")
    for r in route_results:
        emoji_label = "EXCELLENT" if r["order_agreement"] >= 0.95 else (
            "GOOD" if r["order_agreement"] >= 0.85 else (
                "FAIR" if r["order_agreement"] >= 0.70 else "POOR"
            )
        )
        rev_note = " **[sequence reversed for matching]**" if r.get("direction_reversed") else ""
        w(f"**{r['route_name']}** ({r['gtfs_code']} direction {r['direction_id']}){rev_note}")
        w(f"- Classification: {r['classification']} (confidence {r['confidence']:.1f})")
        w(f"- Stops: {r['stops_constructed']} constructed / {r['stops_gtfs']} GTFS / {r['stops_matched']} matched")
        w(f"- Stop coverage: {r['stop_coverage']:.0%}")
        w(f"- Order agreement: {r['order_agreement']:.3f} [{emoji_label}]")
        if r["shape_mean_hausdorff_m"] is not None:
            w(f"- Shape distance: {r['shape_mean_hausdorff_m']:.0f} m (mean Hausdorff)")
        if r["corridors_used"]:
            w(f"- Corridors applied: {', '.join(r['corridors_used'])}")
        else:
            w(f"- Corridors applied: none")
        w("")

    # ── Section C: Corridor impact ────────────────────────────────────────
    w("---")
    w("## C. Impact of Corridor Constraints")
    w("")
    if improved:
        w(f"**Routes with corridor constraints** ({len(improved)}):")
        w("")
        avg_a_corr = sum(r["order_agreement"] for r in improved) / len(improved)
        avg_c_corr = sum(r["stop_coverage"] for r in improved) / len(improved)
        w(f"- Avg order agreement: {avg_a_corr:.3f}")
        w(f"- Avg stop coverage: {avg_c_corr:.1%}")
        w("")
        for r in improved:
            w(f"  - **{r['route_name']}**: agree={r['order_agreement']:.3f}, "
              f"coverage={r['stop_coverage']:.0%}, corridors={', '.join(r['corridors_used'])}")
        w("")
    if no_corridor:
        w(f"**Routes without corridor constraints** ({len(no_corridor)}):")
        w("")
        avg_a_nc = sum(r["order_agreement"] for r in no_corridor) / len(no_corridor)
        avg_c_nc = sum(r["stop_coverage"] for r in no_corridor) / len(no_corridor)
        w(f"- Avg order agreement: {avg_a_nc:.3f}")
        w(f"- Avg stop coverage: {avg_c_nc:.1%}")
        w("")
        for r in no_corridor:
            w(f"  - **{r['route_name']}**: agree={r['order_agreement']:.3f}, "
              f"coverage={r['stop_coverage']:.0%}")
        w("")

    # ── Section D: GTFS-ready routes ──────────────────────────────────────
    w("---")
    w("## D. Routes Ready for GTFS Export")
    w("")
    w(f"Routes classified as **strong** or **acceptable**: {len(ready_routes)}/{after['total_routes']}")
    w("")
    w("| # | Route | Class | Confidence | Stops | Solver |")
    w("|---|-------|-------|-----------|-------|--------|")
    for i, r in enumerate(ready_routes, 1):
        w(f"| {i} | {r['route']} | {r['classification']} | {r['confidence']['score']:.1f} | {r['stops_constructed']} | {r['solver_used']} |")
    w("")

    # Check which GTFS-matched routes are export-ready
    matched_and_ready = [
        r for r in route_results
        if r["classification"] in ("strong", "acceptable")
    ]
    matched_not_ready = [
        r for r in route_results
        if r["classification"] not in ("strong", "acceptable")
    ]
    w(f"Of the {len(route_results)} GTFS-matched routes:")
    w(f"- **{len(matched_and_ready)}** are export-ready (strong/acceptable)")
    w(f"- **{len(matched_not_ready)}** need further work")
    w("")
    if matched_not_ready:
        w("Routes with GTFS ground truth but NOT yet export-ready:")
        w("")
        for r in matched_not_ready:
            w(f"- **{r['route_name']}** ({r['gtfs_code']}): {r['classification']}, "
              f"agree={r['order_agreement']:.3f}, coverage={r['stop_coverage']:.0%}")
        w("")

    # ── Section E: Gaps and recommendations ───────────────────────────────
    w("---")
    w("## E. Remaining Gaps and Recommendations")
    w("")

    # Routes in benchmark with no confirmed match
    no_gtfs = [
        name for name, m in benchmark.items()
        if m.get("confirmed_route") is None
    ]
    w(f"### E.1 Routes without GTFS ground truth ({len(no_gtfs)})")
    w("")
    w("These routes are in the benchmark mapping but have no confirmed GTFS route for validation:")
    w("")
    for name in sorted(no_gtfs):
        cls = route_by_name.get(name, {}).get("classification", "?")
        corridors = benchmark[name].get("corridors", [])
        w(f"- **{name}** (class: {cls}, corridors: {', '.join(corridors) if corridors else 'none'})")
    w("")

    # Low-quality matched routes
    low_quality = [r for r in route_results if r["order_agreement"] < 0.85]
    if low_quality:
        w(f"### E.2 Low Order Agreement Routes ({len(low_quality)})")
        w("")
        w("Routes where pairwise order agreement < 0.85 (may have stop sequencing issues):")
        w("")
        for r in low_quality:
            w(f"- **{r['route_name']}** ({r['gtfs_code']}): agree={r['order_agreement']:.3f}, "
              f"coverage={r['stop_coverage']:.0%}, "
              f"shape={r['shape_mean_hausdorff_m']:.0f}m" if r["shape_mean_hausdorff_m"] else
              f"- **{r['route_name']}** ({r['gtfs_code']}): agree={r['order_agreement']:.3f}, "
              f"coverage={r['stop_coverage']:.0%}")
        w("")

    low_coverage = [r for r in route_results if r["stop_coverage"] < 0.30]
    if low_coverage:
        w(f"### E.3 Low Coverage Routes ({len(low_coverage)})")
        w("")
        w("Routes where fewer than 30% of confirmed GTFS stops were matched:")
        w("")
        for r in low_coverage:
            w(f"- **{r['route_name']}** ({r['gtfs_code']}): coverage={r['stop_coverage']:.0%}, "
              f"built {r['stops_constructed']} vs {r['stops_gtfs']} GTFS stops")
        w("")

    # Summary recommendations
    not_ready_routes = [
        r for r in routes
        if r["classification"] not in ("strong", "acceptable")
    ]
    w("### E.4 Summary Recommendations")
    w("")
    w(f"1. **{len(ready_routes)} routes** ({len(ready_routes)}/{after['total_routes']}) are ready for GTFS export")
    w(f"2. **{len(not_ready_routes)} routes** need further refinement (ambiguous/needs_manual_review/special_case)")
    w(f"3. Corridor constraints improved classification: strong went from {BEFORE['strong']} to {after['classification_breakdown']['strong']} (+{after['classification_breakdown']['strong'] - BEFORE['strong']})")
    w(f"4. Auto-accepted routes increased from {BEFORE['auto_accepted']} to {after['auto_accepted']} (+{after['auto_accepted'] - BEFORE['auto_accepted']})")
    if low_quality:
        w(f"5. {len(low_quality)} routes have low order agreement and need sequence review")
    if low_coverage:
        w(f"6. {len(low_coverage)} routes have low stop coverage -- likely missing stops from grounding")
    w("")

    # ── write output ──────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written to: {OUTPUT_PATH}")
    print(f"  - {len(route_results)} routes compared against GTFS ground truth")
    print(f"  - Avg order agreement: {avg_agree:.3f}")
    print(f"  - Avg stop coverage:   {avg_coverage:.1%}")
    if avg_hd is not None:
        print(f"  - Avg shape distance:  {avg_hd:.0f} m")


if __name__ == "__main__":
    main()
