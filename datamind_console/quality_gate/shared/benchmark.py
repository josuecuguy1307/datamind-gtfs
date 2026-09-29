"""Benchmark harness — runs strategies against error-injected snapshots, computes metrics.

CLI:
    python -m datamind_console.quality_gate.shared.benchmark \
        --province sample_region --cantons cayambe,ruminahui \
        --strategies s1,s2,s3,s4 --output benchmark_results.csv
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set

from .config import BENCHMARK_SEED
from .contract import StrategyBase
from .error_injector import ErrorProfile, InjectedError, get_profile, inject
from .models import DecisionType, GateReport, QualityGateInput
from .fixers import reset_confidence_rng
from .snapshot import generate_synthetic_canton, restore_in_memory, snapshot


@dataclass
class StrategyMetrics:
    """Metrics for a single (strategy, canton) evaluation run."""
    strategy: str
    canton: str
    precision: float            # correct fixes / total fixes applied
    recall: float               # errors caught / errors injected
    autonomy: float             # 1 - (rerouted / total_entities)
    corruption: int             # fixes applied to non-injected entities
    false_positives: int        # rerouted entities that were actually fine
    wall_time_s: float
    api_calls: int              # Nominatim + Valhalla calls
    f1: float                   # harmonic mean of precision and recall


def _compute_metrics(
    report: GateReport,
    ground_truth: List[InjectedError],
    total_entities: int,
    wall_time: float,
) -> StrategyMetrics:
    """Compare gate report against ground truth to compute metrics."""
    injected_ids: Set[str] = {e.entity_id for e in ground_truth}
    injected_rules: Dict[str, str] = {e.entity_id: e.rule_name for e in ground_truth}

    # Committed entity IDs: issues where the strategy committed a fix.
    # Strategy report formats differ:
    #   S1/S2: issues+fixes+decisions are 1:1 aligned (all detected issues).
    #   S3/S4: issues+fixes = committed fixes only; decisions span all passes.
    # We check if issues/decisions are aligned (same length) to choose approach.
    committed_entity_ids: Set[str] = set()
    if len(report.issues) == len(report.decisions):
        # S1/S2 style: 1:1 alignment, use decision to filter
        for i, issue in enumerate(report.issues):
            if report.decisions[i].decision == DecisionType.COMMIT:
                committed_entity_ids.add(issue.entity_id)
    else:
        # S3/S4 style: issues list already contains only committed fixes
        for issue in report.issues:
            committed_entity_ids.add(issue.entity_id)

    # Issues detected (regardless of fix)
    detected_ids = {i.entity_id for i in report.issues}

    # True positives: detected AND injected
    true_pos = len(detected_ids & injected_ids)
    # False positives: detected but NOT injected
    false_pos = len(detected_ids - injected_ids)

    total_detected = true_pos + false_pos
    precision = true_pos / total_detected if total_detected > 0 else 1.0
    recall = true_pos / len(injected_ids) if injected_ids else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    rerouted = len({rb.entity_id for rb in report.route_backs})
    autonomy = max(0.0, min(1.0, 1.0 - (rerouted / total_entities))) if total_entities > 0 else 1.0

    # Corruption: fixes committed to entities that had NO injected error.
    # This is the key safety metric — a non-zero value means the strategy
    # is silently damaging clean data.
    corruption = len(committed_entity_ids - injected_ids)

    return StrategyMetrics(
        strategy=report.strategy_name,
        canton=report.canton,
        precision=round(precision, 4),
        recall=round(recall, 4),
        autonomy=round(autonomy, 4),
        corruption=corruption,
        false_positives=false_pos,
        wall_time_s=round(wall_time, 2),
        api_calls=0,  # tracked per-strategy if they report it
        f1=round(f1, 4),
    )


@dataclass
class BenchmarkRunDetail:
    """Detailed report data for a single (strategy, canton) run."""
    strategy: str
    canton: str
    report: GateReport
    ground_truth: List[InjectedError]
    metrics: StrategyMetrics


def run_benchmark(
    strategies: List[StrategyBase],
    cantons: List[str],
    profile: Optional[ErrorProfile] = None,
    seed: int = BENCHMARK_SEED,
    province: str = "sample_region",
    db_dsn: Optional[str] = None,
    collect_details: bool = False,
) -> List[StrategyMetrics]:
    """Run benchmark across all (strategy, canton) combinations.

    For each (strategy, canton):
      1. Snapshot clean canton (or generate synthetic)
      2. Inject errors (reproducible via seed)
      3. Run strategy
      4. Compute metrics vs ground truth
      5. Restore canton to clean state

    If collect_details=True, stores BenchmarkRunDetail objects in
    _last_run_details (module-level) for downstream analysis scripts.
    """
    global _last_run_details
    if profile is None:
        profile = ErrorProfile()

    # Reset confidence RNG for reproducible fixer variance
    reset_confidence_rng(seed)

    all_metrics: List[StrategyMetrics] = []
    details: List[BenchmarkRunDetail] = []

    for canton in cantons:
        # 1. Snapshot
        if db_dsn:
            clean = snapshot(province, canton, db_dsn)
        else:
            clean = generate_synthetic_canton(canton=canton, province=province, seed=seed)

        for strategy in strategies:
            # 2. Inject errors (canton-specific seed for different error sets)
            canton_seed = hash((seed, canton.lower())) % (2**31)
            poisoned, ground_truth = inject(
                restore_in_memory(clean), profile, seed=canton_seed,
            )

            total_entities = (
                len(poisoned.stops) + len(poisoned.routes) + len(poisoned.shapes) +
                len(poisoned.semantics) + len(poisoned.schedules) + len(poisoned.fares)
            )

            # 3. Run strategy
            t0 = time.monotonic()
            try:
                report = strategy.run(poisoned)
            except Exception as e:
                print(f"  ERROR: {strategy.name} on {canton}: {e}", file=sys.stderr)
                report = GateReport(
                    canton=canton, province=province, export_run_id="benchmark",
                    timestamp=datetime.utcnow().isoformat(),
                    verdict=__import__("datamind_console.quality_gate.shared.models",
                                       fromlist=["QualityVerdict"]).QualityVerdict(status="fail"),
                    strategy_name=strategy.name,
                )
            wall_time = time.monotonic() - t0

            # 4. Compute metrics
            metrics = _compute_metrics(report, ground_truth, total_entities, wall_time)
            all_metrics.append(metrics)

            if collect_details:
                details.append(BenchmarkRunDetail(
                    strategy=strategy.name,
                    canton=canton,
                    report=report,
                    ground_truth=ground_truth,
                    metrics=metrics,
                ))

            print(
                f"  {strategy.name:20s} | {canton:15s} | "
                f"P={metrics.precision:.3f} R={metrics.recall:.3f} F1={metrics.f1:.3f} "
                f"A={metrics.autonomy:.3f} | {metrics.wall_time_s:.1f}s"
            )

    if collect_details:
        _last_run_details = details

    return all_metrics


# Module-level storage for detailed run results (used by analysis scripts)
_last_run_details: List[BenchmarkRunDetail] = []


def get_last_run_details() -> List[BenchmarkRunDetail]:
    """Retrieve detailed results from the last run_benchmark(collect_details=True) call."""
    return _last_run_details


def metrics_to_dataframe(metrics: List[StrategyMetrics]):
    """Convert metrics list to a pandas DataFrame (optional dependency)."""
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("pandas is required for DataFrame output: pip install pandas")

    rows = []
    for m in metrics:
        rows.append({
            "strategy": m.strategy,
            "canton": m.canton,
            "precision": m.precision,
            "recall": m.recall,
            "f1": m.f1,
            "autonomy": m.autonomy,
            "corruption": m.corruption,
            "false_positives": m.false_positives,
            "wall_time_s": m.wall_time_s,
            "api_calls": m.api_calls,
        })

    df = pd.DataFrame(rows)

    # Add summary rows per strategy
    summary = df.groupby("strategy").agg({
        "precision": "mean", "recall": "mean", "f1": "mean",
        "autonomy": "mean", "corruption": "sum",
        "false_positives": "sum", "wall_time_s": "sum", "api_calls": "sum",
    }).reset_index()
    summary["canton"] = "SUMMARY"
    return pd.concat([df, summary], ignore_index=True)


def _load_strategies(names: List[str]) -> List[StrategyBase]:
    """Dynamically import strategies by name.

    Looks for ``Strategy`` alias first (conventional), then falls back to the
    first ``*Gate`` class that subclasses StrategyBase.
    """
    strategies = []
    for name in names:
        if not name.strip():
            continue
        try:
            mod = __import__(
                f"datamind_console.quality_gate.strategies.{name}",
                fromlist=["Strategy"],
            )
            if hasattr(mod, "Strategy"):
                strategies.append(mod.Strategy())
            else:
                # Fallback: find the first *Gate class
                gate_cls = None
                for attr_name in dir(mod):
                    obj = getattr(mod, attr_name)
                    if (isinstance(obj, type)
                            and issubclass(obj, StrategyBase)
                            and obj is not StrategyBase):
                        gate_cls = obj
                        break
                if gate_cls:
                    strategies.append(gate_cls())
                else:
                    print(f"  WARNING: No Strategy or *Gate class in {name!r}", file=sys.stderr)
        except (ImportError, AttributeError) as e:
            print(f"  WARNING: Could not load strategy {name!r}: {e}", file=sys.stderr)
    return strategies


def main():
    parser = argparse.ArgumentParser(description="Quality Gate Benchmark Harness")
    parser.add_argument("--province", default="sample_region")
    parser.add_argument("--cantons", default="cayambe", help="Comma-separated canton names")
    parser.add_argument("--strategies", default="", help="Comma-separated strategy names (e.g. s1,s2)")
    parser.add_argument("--output", default=None, help="Output CSV path")
    parser.add_argument("--seed", type=int, default=BENCHMARK_SEED)
    parser.add_argument("--db-dsn", default=None, help="PostgreSQL DSN (omit for synthetic data)")
    parser.add_argument(
        "--profile", default="full", choices=["smoke", "full"],
        help="Error injection profile: 'smoke' (~30 errors) or 'full' (~206 errors, default)",
    )
    args = parser.parse_args()

    cantons = [c.strip() for c in args.cantons.split(",") if c.strip()]
    strategy_names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    profile = get_profile(args.profile)

    print(f"Quality Gate Benchmark")
    print(f"  Province: {args.province}")
    print(f"  Cantons:  {cantons}")
    print(f"  Strategies: {strategy_names or '(none — smoke test)'}")
    print(f"  Profile: {args.profile}")
    print(f"  Seed: {args.seed}")
    print()

    strategies = _load_strategies(strategy_names)

    if not strategies:
        print("No strategies loaded — running smoke test (inject + verify reproducibility)")
        for canton in cantons:
            clean = generate_synthetic_canton(canton=canton, province=args.province, seed=args.seed)
            p1, t1 = inject(restore_in_memory(clean), profile, seed=args.seed)
            p2, t2 = inject(restore_in_memory(clean), profile, seed=args.seed)
            assert len(t1) == len(t2), f"Truth length mismatch: {len(t1)} vs {len(t2)}"
            assert t1 == t2, "Injection not reproducible!"
            total_entities = (
                len(p1.stops) + len(p1.routes) + len(p1.shapes) +
                len(p1.semantics) + len(p1.schedules) + len(p1.fares)
            )
            print(f"  {canton}: {len(t1)} errors injected into {total_entities} entities — reproducible OK")
        print("\nSmoke test passed.")
        return

    metrics = run_benchmark(
        strategies=strategies,
        cantons=cantons,
        profile=profile,
        seed=args.seed,
        province=args.province,
        db_dsn=args.db_dsn,
    )

    if args.output and metrics:
        try:
            df = metrics_to_dataframe(metrics)
            df.to_csv(args.output, index=False)
            print(f"\nResults written to {args.output}")
        except ImportError:
            # Fallback: write CSV manually
            import csv
            with open(args.output, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["strategy", "canton", "precision", "recall", "f1",
                            "autonomy", "corruption", "false_positives", "wall_time_s", "api_calls"])
                for m in metrics:
                    w.writerow([m.strategy, m.canton, m.precision, m.recall, m.f1,
                                m.autonomy, m.corruption, m.false_positives, m.wall_time_s, m.api_calls])
            print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
