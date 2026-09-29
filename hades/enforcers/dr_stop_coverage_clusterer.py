"""[LEGACY — 2026-04-22] Clusterer for DR Type 2 queries.

Implements the K-means clustering DR generation flow described in
skill 14. Produced the historical
``workspace/dr_stop_coverage/queries/batch_NN_<zone>.md`` corpus and is
preserved for backward compatibility with that corpus.

NEW DR WORK SHOULD USE: ``hades.enforcers.dr_prompt_generator`` (skill 17).

Do not call this module for new route processing. See
``workspace/skills/17_DEPRECATION_AUDIT.md`` for the full conflict map.

---

Reads the `dr_queries_prepared` entries emitted by the stop coverage
enforcer into geographic clusters suitable for human-in-loop Claude.ai
processing. One cluster becomes one production batch file.

This module builds **DR Type 2**. The DR Type 1 skill + queue
(``workspace/research_queue/`` and ``06c_DEEP_RESEARCH_STOP_GROUNDING``)
are fully separate — do not import from this module into any Type 1
pipeline or vice versa. See
``workspace/skills/14_DR_STOP_COVERAGE_GAP_FILLING.md`` for the full
contract.

Algorithm
~~~~~~~~~

1. Load every ``dr_queries_prepared`` entry from the diagnostic JSONL.
2. K-means ``k=10`` on (lat, lon) midpoints.
3. If any cluster has <20 queries → retry ``k=9``; if >150 → retry
   ``k=11``. If both bounds still fail, fall back to HDBSCAN with
   ``min_cluster_size=30``.
4. For each cluster: centroid, bbox, route / canton / cooperativa mix.
5. Soft-match centroid against the 10 seed cluster names (≤ 5 km
   haversine to a known seed).
6. Write ``workspace/dr_stop_coverage/_clustering_plan.json``.

No writes to ``route_prod.*``. Read-only on the local DB — only to look
up per-route ``direction_semantics`` ({canton, cooperative}) for cluster
metadata.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

try:
    from sklearn.cluster import KMeans
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "scikit-learn is required for the DR stop coverage clusterer. "
        "Install with `pip install scikit-learn`."
    ) from exc

try:
    import hdbscan  # type: ignore[import]
    _HAS_HDBSCAN = True
except ImportError:
    _HAS_HDBSCAN = False


ROOT = Path(__file__).resolve().parents[2]
DIAG_JSONL = ROOT / "workspace" / "diagnostics" / "stop_coverage_diagnostic_full.jsonl"
OUT_DIR = ROOT / "workspace" / "dr_stop_coverage"
OUT_PLAN = OUT_DIR / "_clustering_plan.json"

DSN = os.environ.get("DB_DSN", "")

# Approximate centroids (lat, lon) of the known clusters we want to
# soft-match against. 5 km match radius per spec.
SEED_CENTROIDS: dict[str, tuple[float, float]] = {
    "sangolquí_rumiñahui":     (-0.332, -78.454),
    "sur_quito_chillogallo":   (-0.294, -78.548),
    "norte_quito_calderón":    (-0.098, -78.425),
    "valle_chillos_conocoto":  (-0.302, -78.475),
    "duran_milagro":           (-2.287, -79.650),
    "guayaquil_centro":        (-2.180, -79.890),
    "guayaquil_sur_tarqui":    (-2.240, -79.905),
    "cayambe_pedro_moncayo":   ( 0.040, -78.152),
    "pedro_vicente_rural":     ( 0.050, -78.800),
    "misc_cross_province":     (-1.200, -79.300),
}

SEED_MATCH_RADIUS_M = 5_000.0
MIN_CLUSTER_SIZE = 20
MAX_CLUSTER_SIZE = 150
HDBSCAN_MIN_CLUSTER = 30


from hades.geometry.canonical import haversine_m as _canonical_haversine_m
from datamind_core.dsn import need_dsn


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    return _canonical_haversine_m(a[0], a[1], b[0], b[1])


@dataclass(slots=True)
class DRQuery:
    """One DR-prepared gap, with enough metadata to land in a cluster."""

    global_idx: int
    route_code: str
    route_name: str
    province: str
    source_type: str
    zone: str
    gap_idx: int
    gap_m: float
    midpoint_lat: float
    midpoint_lon: float

    def key(self) -> str:
        """Stable Q-id-agnostic key used to dedupe if the JSONL is regen'd."""
        return f"{self.route_code}:{self.gap_idx}"


def _iter_diag_queries(path: Path) -> list[DRQuery]:
    queries: list[DRQuery] = []
    global_idx = 0
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            for q in rec.get("dr_queries_prepared", []):
                queries.append(
                    DRQuery(
                        global_idx=global_idx,
                        route_code=q["route_code"],
                        route_name=rec.get("route_name") or "",
                        province=rec.get("province") or "",
                        source_type=rec.get("source_type") or "",
                        zone=q["zone"],
                        gap_idx=q["gap_idx"],
                        gap_m=float(q["gap_m"]),
                        midpoint_lat=float(q["midpoint_lat"]),
                        midpoint_lon=float(q["midpoint_lon"]),
                    )
                )
                global_idx += 1
    return queries


def _run_kmeans(
    coords: np.ndarray, k: int, seed: int = 42
) -> np.ndarray:
    # Deterministic for reproducibility.
    km = KMeans(n_clusters=k, n_init=20, random_state=seed)
    return km.fit_predict(coords)


def _cluster_sizes_ok(labels: np.ndarray, k: int) -> tuple[bool, int, int]:
    counts = np.bincount(labels, minlength=k)
    return (
        int(counts.min()) >= MIN_CLUSTER_SIZE
        and int(counts.max()) <= MAX_CLUSTER_SIZE,
        int(counts.min()),
        int(counts.max()),
    )


def _run_hdbscan(coords: np.ndarray) -> np.ndarray:
    if not _HAS_HDBSCAN:  # pragma: no cover
        raise RuntimeError(
            "HDBSCAN fallback requested but hdbscan not installed. "
            "`pip install hdbscan`."
        )
    model = hdbscan.HDBSCAN(min_cluster_size=HDBSCAN_MIN_CLUSTER)
    return model.fit_predict(coords)


def _rebalance(
    coords: np.ndarray, labels: np.ndarray
) -> np.ndarray:
    """Recursively split oversized clusters and merge undersized ones.

    Splitting: any cluster with >MAX_CLUSTER_SIZE points gets
    subdivided by a 2-way k-means on its own points, replacing its old
    label with two new labels.

    Merging: any cluster with <MIN_CLUSTER_SIZE points is absorbed by
    its nearest neighbour (by centroid), iteratively until all clusters
    are at or above the minimum (or only one remains).

    Runs until both conditions hold or an iteration cap is reached.
    """
    labels = labels.copy()
    # Target ~140 as the split floor so that subsequent merges have some
    # headroom before a cluster reclimbs the 150 ceiling.
    split_target = 140
    for iteration in range(80):
        changed = False

        # --- split oversized ---
        unique_labels = np.unique(labels)
        next_label = int(unique_labels.max()) + 1 if unique_labels.size else 0
        for lbl in unique_labels:
            if lbl < 0:
                continue
            mask = labels == lbl
            n = int(mask.sum())
            if n > MAX_CLUSTER_SIZE:
                # Aim for pieces of ≤ split_target (slightly below
                # MAX_CLUSTER_SIZE) by splitting into
                # k = ceil(n / split_target). That gives headroom for
                # future merges before any piece re-climbs the ceiling.
                k_split = max(2, math.ceil(n / split_target))
                sub = _run_kmeans(coords[mask], k=k_split, seed=int(lbl) + 7)
                # Reuse lbl for sub==0, allocate fresh ids for the rest.
                new_assignment = np.where(sub == 0, lbl, next_label - 1)
                for i in range(1, k_split):
                    new_assignment = np.where(
                        sub == i, next_label + i - 1, new_assignment
                    )
                labels[mask] = new_assignment
                next_label += k_split - 1
                changed = True

        # --- merge undersized ---
        unique_labels = np.unique(labels)
        centroids: dict[int, np.ndarray] = {}
        counts: dict[int, int] = {}
        for lbl in unique_labels:
            if lbl < 0:
                continue
            mask = labels == lbl
            centroids[int(lbl)] = coords[mask].mean(axis=0)
            counts[int(lbl)] = int(mask.sum())

        # Merge only the single smallest undersized cluster per pass.
        # Merging many-at-once can funnel several tiny clusters into
        # the same neighbour and push it well past the upper bound,
        # which forces a follow-up split and an oscillation. Merging
        # one-at-a-time converges cleanly.
        smallest = min(counts.items(), key=lambda x: x[1], default=None)
        if smallest is not None and smallest[1] < MIN_CLUSTER_SIZE and len(counts) > 1:
            lbl = smallest[0]
            my_centroid = centroids[lbl]
            best_lbl = None
            best_d = float("inf")
            my_size = counts[lbl]
            for other_lbl, other_c in centroids.items():
                if other_lbl == lbl:
                    continue
                combined = counts[other_lbl] + my_size
                if combined > MAX_CLUSTER_SIZE:
                    continue  # don't merge into a neighbour that will
                              # then need splitting (prevents oscillation).
                d = float(np.hypot(*(other_c - my_centroid)))
                if d < best_d:
                    best_d = d
                    best_lbl = other_lbl
            if best_lbl is None:
                # Every near neighbour is already near-full; pick the
                # geometrically nearest regardless of size and let a
                # subsequent split pass handle the overflow.
                for other_lbl, other_c in centroids.items():
                    if other_lbl == lbl:
                        continue
                    d = float(np.hypot(*(other_c - my_centroid)))
                    if d < best_d:
                        best_d = d
                        best_lbl = other_lbl
            if best_lbl is not None:
                labels[labels == lbl] = best_lbl
                changed = True

        if not changed:
            print(
                f"[clusterer]   rebalance converged at iter={iteration} "
                f"k={len(np.unique(labels[labels>=0]))} "
                f"sizes {sorted(np.bincount(labels[labels>=0]).tolist())}"
            )
            break
    else:
        print(
            f"[clusterer]   rebalance hit 80-iter cap, final sizes "
            f"{sorted(np.bincount(labels[labels>=0]).tolist())}"
        )

    # Compact label space so labels are 0..K-1.
    unique = sorted(int(x) for x in np.unique(labels) if x >= 0)
    remap = {old: new for new, old in enumerate(unique)}
    compact = np.array([remap.get(int(x), -1) for x in labels])
    return compact


def _fetch_route_meta(
    route_codes: list[str],
) -> dict[str, dict[str, Any]]:
    """Read-only pull of direction_semantics per route_id.

    Returns { route_code (uuid str) → { canton, cooperative } }. Missing
    routes get empty dicts. Any DB error degrades silently — metadata is
    a nice-to-have for the cluster plan, not a blocker.
    """
    if not route_codes:
        return {}
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError:  # pragma: no cover
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        with psycopg2.connect(need_dsn(DSN)) as conn:
            conn.set_session(readonly=True)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT route_id::text AS route_code,
                           direction_semantics
                    FROM route_prod.routes
                    WHERE route_id = ANY(%s::uuid[])
                    """,
                    (route_codes,),
                )
                for row in cur.fetchall():
                    sem = row["direction_semantics"] or {}
                    out[row["route_code"]] = {
                        "canton": sem.get("canton"),
                        "cooperative": sem.get("cooperative"),
                    }
    except Exception as exc:  # noqa: BLE001
        print(f"[clusterer] WARN direction_semantics fetch failed: {exc}")
    return out


def _soft_match_seed(
    centroid: tuple[float, float],
) -> Optional[str]:
    best = None
    best_d = SEED_MATCH_RADIUS_M
    for name, seed in SEED_CENTROIDS.items():
        d = _haversine_m(centroid, seed)
        if d <= best_d:
            best = name
            best_d = d
    return best


def _auto_name(
    centroid: tuple[float, float],
    cantons: list[str],
) -> str:
    canton = cantons[0] if cantons else "unknown"
    return (
        canton.lower()
        .replace(" ", "_")
        .replace("/", "_")
        .replace(",", "")
    )


def _assign_q_ids(queries: list[DRQuery], labels: np.ndarray, k_used: int):
    """Yield (cluster_id, query, batch_local_q_id).

    Query IDs are per-batch (Q001 .. Q<N>), sorted by route_code + gap_idx.
    """
    buckets: dict[int, list[DRQuery]] = {i: [] for i in range(k_used)}
    for q, lbl in zip(queries, labels):
        if lbl < 0:  # HDBSCAN noise point — dump to misc bucket (last).
            continue
        buckets[int(lbl)].append(q)
    results: dict[int, list[tuple[DRQuery, str]]] = {}
    for cid, bucket in buckets.items():
        bucket.sort(key=lambda q: (q.route_code, q.gap_idx))
        results[cid] = [
            (q, f"Q{i + 1:03d}") for i, q in enumerate(bucket)
        ]
    return results


def build_plan() -> dict[str, Any]:
    queries = _iter_diag_queries(DIAG_JSONL)
    if not queries:
        raise SystemExit(f"[clusterer] no queries loaded from {DIAG_JSONL}")
    print(f"[clusterer] loaded {len(queries)} DR queries from {DIAG_JSONL.name}")

    coords = np.asarray(
        [(q.midpoint_lat, q.midpoint_lon) for q in queries], dtype=float
    )

    method = "kmeans"
    k_used = 10
    labels = _run_kmeans(coords, 10)
    ok, lo, hi = _cluster_sizes_ok(labels, 10)
    print(f"[clusterer] k=10 sizes: min={lo} max={hi} ok={ok}")

    if not ok:
        # Try k=9 if hi too large; k=11 if lo too small; try both.
        tried = [(10, lo, hi)]
        for k_try in (9, 11):
            labels_try = _run_kmeans(coords, k_try)
            ok, lo, hi = _cluster_sizes_ok(labels_try, k_try)
            tried.append((k_try, lo, hi))
            print(f"[clusterer] k={k_try} sizes: min={lo} max={hi} ok={ok}")
            if ok:
                labels = labels_try
                k_used = k_try
                break
        else:
            # Neither plain k-means nor HDBSCAN keeps all queries AND
            # respects the 20–150 bound on this dataset — the metro
            # concentration is too extreme. Use a deterministic
            # rebalance pass (recursive split + nearest-neighbour
            # merge) seeded from k-means k=10.
            print(
                f"[clusterer] k-means retries did not satisfy 20–150 "
                f"bounds; tried {tried}; applying deterministic "
                f"rebalance (split >{MAX_CLUSTER_SIZE} / merge "
                f"<{MIN_CLUSTER_SIZE})."
            )
            labels = _rebalance(coords, _run_kmeans(coords, 10))
            method = "kmeans_rebalanced"
            k_used = int(labels.max()) + 1 if labels.size else 0
            ok2, lo2, hi2 = _cluster_sizes_ok(labels, k_used)
            print(
                f"[clusterer] after rebalance: k={k_used} "
                f"min={lo2} max={hi2} ok={ok2}"
            )
            if not ok2 and _HAS_HDBSCAN:
                # Rebalance shouldn't fail, but if it does the spec's
                # contracted fallback is HDBSCAN; keep that path live.
                print("[clusterer] rebalance failed; falling back to HDBSCAN.")
                labels = _run_hdbscan(coords)
                method = "hdbscan"
                k_used = int(labels.max()) + 1 if labels.size else 0
            ok = ok2

    route_meta = _fetch_route_meta(
        sorted({q.route_code for q in queries})
    )
    buckets = _assign_q_ids(queries, labels, max(k_used, int(labels.max()) + 1))

    # Build cluster records.
    seeds_used: dict[str, int] = {}
    clusters: list[dict[str, Any]] = []
    used_batch_names: set[str] = set()

    for cluster_id in sorted(buckets.keys()):
        bucket = buckets[cluster_id]
        if not bucket:
            continue
        qs = [q for q, _qid in bucket]
        qids = [qid for _q, qid in bucket]
        lats = [q.midpoint_lat for q in qs]
        lons = [q.midpoint_lon for q in qs]
        centroid = (float(np.mean(lats)), float(np.mean(lons)))
        bbox = [min(lats), min(lons), max(lats), max(lons)]
        cantons = sorted({
            route_meta.get(q.route_code, {}).get("canton")
            for q in qs
            if route_meta.get(q.route_code, {}).get("canton")
        })
        cooperativas = sorted({
            route_meta.get(q.route_code, {}).get("cooperative")
            for q in qs
            if route_meta.get(q.route_code, {}).get("cooperative")
        })
        provinces = sorted({q.province for q in qs if q.province})

        matched = _soft_match_seed(centroid)
        seeds_used[matched] = seeds_used.get(matched, 0) + 1 if matched else 0
        label = matched or _auto_name(centroid, cantons or provinces)
        batch_idx = len(clusters) + 1
        base_name = f"batch_{batch_idx:02d}_{label}"
        # Guard against duplicate seed matches.
        name = base_name
        suffix = 2
        while name in used_batch_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_batch_names.add(name)

        clusters.append({
            "cluster_id": cluster_id,
            "batch_name": name,
            "query_count": len(qs),
            "centroid": [round(centroid[0], 6), round(centroid[1], 6)],
            "bbox": [round(v, 6) for v in bbox],
            "cantons": cantons,
            "cooperativas": cooperativas,
            "provinces": provinces,
            "matched_seed": matched,
            "zone_mix": _zone_mix(qs),
            "gap_m_min": round(min(q.gap_m for q in qs), 1),
            "gap_m_max": round(max(q.gap_m for q in qs), 1),
            "query_ids": qids,
            "route_codes": sorted({q.route_code for q in qs}),
            "query_pointers": [
                {
                    "qid": qid,
                    "route_code": q.route_code,
                    "gap_idx": q.gap_idx,
                    "zone": q.zone,
                    "midpoint_lat": q.midpoint_lat,
                    "midpoint_lon": q.midpoint_lon,
                }
                for (q, qid) in bucket
            ],
        })

    # Order clusters so the batch numbering in queries/ is stable:
    # within the final list (already appended above) they follow the
    # order clusters were discovered by numpy's label ids.
    plan = {
        "method": method,
        "k": k_used,
        "total_queries": len(queries),
        "min_cluster_size_target": MIN_CLUSTER_SIZE,
        "max_cluster_size_target": MAX_CLUSTER_SIZE,
        "size_bounds_satisfied": ok,
        "clusters": clusters,
    }
    return plan


def _zone_mix(qs: list[DRQuery]) -> dict[str, int]:
    out: dict[str, int] = {}
    for q in qs:
        out[q.zone] = out.get(q.zone, 0) + 1
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    plan = build_plan()
    OUT_PLAN.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
    print(
        f"[clusterer] wrote {OUT_PLAN} "
        f"({plan['method']}, k={plan['k']}, "
        f"{len(plan['clusters'])} clusters, "
        f"{plan['total_queries']} queries total)"
    )
    for c in plan["clusters"]:
        print(
            f"  {c['batch_name']:<52s} "
            f"n={c['query_count']:>3d}  "
            f"seed={c['matched_seed']}  "
            f"cantons={c['cantons'][:3]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
