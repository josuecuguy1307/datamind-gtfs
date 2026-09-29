"""
Phase 2 – Semantic Geocoder
Single-runner to test the entire Phase 2 pipeline end-to-end.

Runs (in order):
  00_migrate.py
  10_extract_evidence.py
  15_build_geo_context.py
  20_build_candidates.py
  25_build_name_candidates.py
  30_approve.py
  35_train_name_ranker.py
  40_build_embeddings.py
  50_reindex_opensearch.py (Postgres refresh mode)
  60_search_demo.py

Usage:
  cd phase2_semantics
  export DB_DSN="postgresql://user:pass@host:5432/dbname?sslmode=require"
  python scripts/run_all.py
  python scripts/run_all.py --skip-opensearch
  python scripts/run_all.py --from-step 20
  python scripts/run_all.py --to-step 40
  python scripts/run_all.py --query "terminal quitumbe"
  python scripts/run_all.py --continue-on-error
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


# -----------------------------
# Steps
# -----------------------------

@dataclass(frozen=True)
class Step:
    code: int
    filename: str
    name: str
    needs_query: bool = False


STEPS: List[Step] = [
    Step(0,  "00_migrate.py",           "Migrate DB schemas + tables"),
    Step(10, "10_extract_evidence.py",  "Extract semantic name evidence"),
    Step(15, "15_build_geo_context.py", "Build geo-context features from node_prod"),
    Step(20, "20_build_candidates.py",  "Build alias + place candidates"),
    Step(25, "25_build_name_candidates.py", "Build name candidates + baseline scoring"),
    Step(26, "26_contextual_names.py",  "Contextual name disambiguation"),
    Step(30, "30_approve.py",           "Approve candidates to geo_prod"),
    Step(35, "35_train_name_ranker.py", "Train name ranker from feedback + rescore"),
    Step(40, "40_build_embeddings.py",  "Build embeddings for approved aliases"),
    Step(50, "50_reindex_opensearch.py","Refresh PostgreSQL vector search stats"),
    Step(60, "60_search_demo.py",       "Run semantic search demo", needs_query=True),
]


# -----------------------------
# Utilities
# -----------------------------

def _phase2_root() -> Path:
    """
    scripts/run_all.py -> phase2_semantics/
    """
    return Path(__file__).resolve().parents[1]


def _require_env() -> None:
    # Minimal env required for DB scripts
    has_supabase_parts = bool(os.getenv("SUPABASE_DB_HOST"))
    if not os.getenv("DB_DSN") and not os.getenv("DATABASE_URL") and not has_supabase_parts:
        raise RuntimeError(
            "Missing database config. Set DB_DSN or DATABASE_URL,\n"
            "or SUPABASE_DB_HOST/SUPABASE_DB_PORT/SUPABASE_DB_NAME/"
            "SUPABASE_DB_USER/SUPABASE_DB_PASSWORD.\n"
        )


def _print_banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _run_script(
    root: Path,
    script_path: Path,
    *,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
) -> None:
    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)

    print(f"\n▶ Running: {' '.join(cmd)}")
    env_vars = (env or os.environ.copy())
    env_vars["PYTHONPATH"] = str(root)

    subprocess.run(
        cmd,
        cwd=str(root),
        env=env_vars,
        check=True,
    )


def _filter_steps(
    *,
    from_step: int,
    to_step: int,
    skip_opensearch: bool,
    skip_search_demo: bool,
) -> List[Step]:
    selected = [s for s in STEPS if from_step <= s.code <= to_step]

    if skip_opensearch:
        selected = [s for s in selected if s.code != 50]

    if skip_search_demo:
        selected = [s for s in selected if s.code != 60]

    return selected


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser(description="Phase2 single runner (end-to-end test)")
    parser.add_argument("--from-step", type=int, default=0, help="Start at step code (0,10,20,...)")
    parser.add_argument("--to-step", type=int, default=60, help="End at step code (0..60)")
    parser.add_argument("--skip-opensearch", action="store_true", help="Skip step 50 (legacy flag; now PostgreSQL refresh)")
    parser.add_argument("--skip-search-demo", action="store_true", help="Skip step 60 (search demo)")
    parser.add_argument("--query", type=str, default="terminal quitumbe", help="Query text for step 60")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep going if a step fails")
    args = parser.parse_args()

    root = _phase2_root()
    scripts_dir = root / "scripts"

    _print_banner("🔧 Phase 2 – Semantic Geocoder (Single Runner)")
    print(f"Root: {root}")
    print(f"Python: {sys.executable}")

    _require_env()

    selected_steps = _filter_steps(
        from_step=args.from_step,
        to_step=args.to_step,
        skip_opensearch=args.skip_opensearch,
        skip_search_demo=args.skip_search_demo,
    )

    if not selected_steps:
        raise RuntimeError("No steps selected. Check --from-step / --to-step / skips.")

    ok: List[int] = []
    failed: List[Tuple[int, str]] = []

    for step in selected_steps:
        script_path = scripts_dir / step.filename
        if not script_path.exists():
            raise FileNotFoundError(f"Missing script: {script_path}")

        _print_banner(f"STEP {step.code}: {step.name}")

        try:
            if step.needs_query:
                _run_script(root, script_path, args=[args.query])
            else:
                _run_script(root, script_path)

            ok.append(step.code)
            print(f"\n✅ Step {step.code} OK")

        except subprocess.CalledProcessError as e:
            msg = f"Step {step.code} FAILED (exit={e.returncode})"
            failed.append((step.code, msg))
            print(f"\n❌ {msg}")

            if not args.continue_on_error:
                break

        except Exception as e:
            msg = f"Step {step.code} FAILED: {e}"
            failed.append((step.code, msg))
            print(f"\n❌ {msg}")

            if not args.continue_on_error:
                break

    _print_banner("✅ RUN SUMMARY")
    print(f"OK steps:     {ok if ok else 'None'}")
    print(f"Failed steps: {failed if failed else 'None'}")

    if failed:
        raise SystemExit(1)

    print("\n🎉 Phase2 pipeline completed successfully.")


if __name__ == "__main__":
    main()
