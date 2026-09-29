# phase3_routes/scripts/run_all.py
"""
Phase 3 – Routes pipeline
Single-runner to test the entire route pipeline end-to-end.

Supports TWO entry modes:

A) Explicit relation id (old mode):
  python scripts/run_all.py new 123456789
  python scripts/run_all.py <route_uuid> 123456789

B) Dynamic discover (new mode):
  python scripts/run_all.py new --refs E1,E2 --bbox "-0.35,-78.55,-0.10,-78.35"
  python scripts/run_all.py <route_uuid> --refs E1 --bbox "s,w,n,e" --operator "Cooperativa" --name "Ecovia"

Steps (default):
  05_discover_relation.py      (only if discover mode used)
  10_fetch_relation.py
  20_build_stop_sequences.py
  30_build_geometry_candidates.py
  32_geometry_stop_recovery.py
  35_rank_geometry_candidates.py   (optional, if present and not skipped)
  40_approve_geometry.py
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()


# -----------------------------
# Steps
# -----------------------------

@dataclass(frozen=True)
class Step:
    code: int
    filename: str
    name: str


STEPS_EXPLICIT: List[Step] = [
    Step(10, "10_fetch_relation.py",            "Fetch OSM relation (Overpass) -> store raw"),
    Step(20, "20_build_stop_sequences.py",      "Build stop sequence candidates"),
    Step(30, "30_build_geometry_candidates.py", "Build geometry candidates (Valhalla)"),
    Step(32, "32_geometry_stop_recovery.py",    "Recover geometry-aligned nearby canonical stops"),
    Step(35, "35_rank_geometry_candidates.py",  "Rank geometry candidates with ML (optional)"),
    Step(40, "40_approve_geometry.py",          "Approve best geometry -> prod"),
]

STEPS_DISCOVER: List[Step] = [
    Step(5,  "05_discover_relation.py",         "Discover best OSM relation (bbox/refs/etc)"),
    *STEPS_EXPLICIT,
]


# -----------------------------
# Helpers
# -----------------------------

def _phase3_root() -> Path:
    # scripts/run_all.py -> phase3_routes/
    return Path(__file__).resolve().parents[1]


def _print_banner(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _require_env() -> None:
    has_supabase_parts = bool(os.getenv("SUPABASE_DB_HOST"))
    if not os.getenv("DB_DSN") and not os.getenv("DATABASE_URL") and not has_supabase_parts:
        raise RuntimeError(
            "Missing database config. Set DB_DSN or DATABASE_URL,\n"
            "or SUPABASE_DB_HOST/SUPABASE_DB_PORT/SUPABASE_DB_NAME/"
            "SUPABASE_DB_USER/SUPABASE_DB_PASSWORD.\n"
        )


def _run_script(
    root: Path,
    script_path: Path,
    *,
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    cmd = [sys.executable, str(script_path)]
    if args:
        cmd.extend(args)

    print(f"\n▶ Running: {' '.join(cmd)}")

    env_vars = (env or os.environ.copy())
    # Include project root so absolute imports like
    # `phase3_routes.services.route_constructor.src.settings` resolve.
    project_root = root.parents[2]
    env_vars["PYTHONPATH"] = f"{project_root}{os.pathsep}{root}"

    p = subprocess.run(
        cmd,
        cwd=str(root),
        env=env_vars,
        text=True,
        capture_output=True,
    )

    if p.stdout:
        print(p.stdout, end="" if p.stdout.endswith("\n") else "\n")
    if p.stderr:
        print(p.stderr, end="" if p.stderr.endswith("\n") else "\n", file=sys.stderr)

    if p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, output=p.stdout, stderr=p.stderr)

    return p.stdout or ""


def _extract_uuid(label: str, text: str) -> uuid.UUID:
    m = re.search(rf"{re.escape(label)}\s*([0-9a-fA-F-]{{36}})", text)
    if not m:
        raise ValueError(f"Could not find '{label} <uuid>' in output.")
    return uuid.UUID(m.group(1))


def _extract_int(label: str, text: str) -> int:
    """
    Extract int printed like:
      "osm_relation_id: 123456"
    """
    m = re.search(rf"{re.escape(label)}\s*([0-9]+)", text)
    if not m:
        raise ValueError(f"Could not find '{label} <int>' in output.")
    return int(m.group(1))


def _pick_stop_sequence_candidate_id(seq_set_id: uuid.UUID) -> uuid.UUID:
    """
    OUR schema:
      route_work.stop_sequence_candidates(candidate_id, set_id, rank, stop_node_ids, metrics, created_at)

    Picks rank=1 if exists, else newest.
    """
    from src.db.conn import db_conn, db_cursor

    with db_conn() as conn:
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT candidate_id
                FROM route_work.stop_sequence_candidates
                WHERE set_id=%s
                ORDER BY rank ASC NULLS LAST, created_at DESC NULLS LAST
                LIMIT 1
                """,
                (str(seq_set_id),),
            )
            row = cur.fetchone()
            if row and row.get("candidate_id"):
                return uuid.UUID(row["candidate_id"])

    raise RuntimeError(f"Could not auto-pick stop_sequence_candidate_id for set_id={seq_set_id}")


def _filter_steps(steps: List[Step], from_step: int, to_step: int, skip_ml: bool) -> List[Step]:
    selected = [s for s in steps if from_step <= s.code <= to_step]
    if skip_ml:
        selected = [s for s in selected if s.code != 35]
    return selected


def _is_discover_mode(args: argparse.Namespace) -> bool:
    # Discover mode is enabled if bbox is set OR refs/operator/name provided.
    return bool(args.bbox or args.refs or args.operator or args.name)


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase3 single runner (end-to-end test)")

    # Keep positional compatibility:
    #   run_all.py new 123456789
    # but allow omitting osm_relation_id when discover mode is used.
    parser.add_argument("route_id_or_new", type=str, help="'new' or an existing route UUID")

    parser.add_argument(
        "osm_relation_id",
        nargs="?",
        type=int,
        default=None,
        help="OSM relation id (integer). Optional if using --bbox/--refs discover mode.",
    )

    # Discover args (new)
    parser.add_argument("--bbox", type=str, default=None, help="south,west,north,east for discover step")
    parser.add_argument("--refs", type=str, default=None, help="comma-separated refs (e.g. E1,E2)")
    parser.add_argument("--operator", type=str, default=None, help="operator contains filter (discover)")
    parser.add_argument("--name", type=str, default=None, help="name contains filter (discover)")
    parser.add_argument("--max-candidates", type=int, default=20, help="discover: how many relations to score")
    parser.add_argument("--discover-store", action="store_true", help="discover: store candidates/chosen (best effort)")

    # Runner controls
    parser.add_argument("--from-step", type=int, default=5, help="Start step code (5,10,20,30,32,35,40)")
    parser.add_argument("--to-step", type=int, default=40, help="End step code (5..40)")
    parser.add_argument("--skip-ml", action="store_true", help="Skip step 35 (ML ranking)")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep going if a step fails")

    args = parser.parse_args()

    root = _phase3_root()
    scripts_dir = root / "scripts"
    # ✅ Make 'src' importable from the runner process too
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    # ✅ Also project root, so `phase3_routes.services.route_constructor...` absolute imports resolve
    _project_root = root.parents[2]
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))



    _print_banner("🔧 Phase 3 – Routes Pipeline (Single Runner)")
    print(f"Root:   {root}")
    print(f"Python: {sys.executable}")

    _require_env()

    discover_mode = _is_discover_mode(args)

    # Validate inputs
    if not discover_mode and args.osm_relation_id is None:
        raise SystemExit(
            "You must provide osm_relation_id (old mode) OR use discover mode with --bbox/--refs.\n"
            "Examples:\n"
            "  python scripts/run_all.py new 123456789\n"
            '  python scripts/run_all.py new --refs E1,E2 --bbox "-0.35,-78.55,-0.10,-78.35"\n'
        )

    all_steps = STEPS_DISCOVER if discover_mode else STEPS_EXPLICIT
    selected_steps = _filter_steps(all_steps, args.from_step, args.to_step, args.skip_ml)
    if not selected_steps:
        raise RuntimeError("No steps selected. Check --from-step / --to-step / --skip-ml.")

    # Ensure scripts exist
    for step in selected_steps:
        sp = scripts_dir / step.filename
        if not sp.exists():
            if step.code == 35:
                print(f"⚠️  Missing optional step 35 script: {sp} (will be skipped)")
                selected_steps = [s for s in selected_steps if s.code != 35]
                break
            raise FileNotFoundError(f"Missing script: {sp}")

    ok: List[int] = []
    failed: List[Tuple[int, str]] = []

    route_id: Optional[uuid.UUID] = None
    osm_relation_id: Optional[int] = args.osm_relation_id
    seq_set_id: Optional[uuid.UUID] = None
    seq_candidate_id: Optional[uuid.UUID] = None
    geom_set_id: Optional[uuid.UUID] = None
    approved_id: Optional[uuid.UUID] = None

    for step in selected_steps:
        script_path = scripts_dir / step.filename
        _print_banner(f"STEP {step.code}: {step.name}")

        try:
            if step.code == 5:
                # 05_discover_relation.py prints:
                #   route_id: <uuid>
                #   osm_relation_id: <int>
                if not args.bbox:
                    raise RuntimeError("Discover mode requires --bbox 'south,west,north,east'.")

                discover_args = [args.route_id_or_new, "--bbox", args.bbox]
                if args.refs:
                    discover_args += ["--refs", args.refs]
                if args.operator:
                    discover_args += ["--operator", args.operator]
                if args.name:
                    discover_args += ["--name", args.name]
                if args.max_candidates:
                    discover_args += ["--max-candidates", str(args.max_candidates)]
                if args.discover_store:
                    discover_args += ["--store"]

                out = _run_script(root, script_path, args=discover_args)

                # parse route_id
                if args.route_id_or_new == "new":
                    route_id = _extract_uuid("route_id:", out)
                else:
                    route_id = uuid.UUID(args.route_id_or_new)

                # parse discovered relation id
                osm_relation_id = _extract_int("osm_relation_id:", out)
                print(f"✅ Discovered osm_relation_id: {osm_relation_id}")

            elif step.code == 10:
                    if osm_relation_id is None:
                        raise RuntimeError("osm_relation_id not known before Step 10.")

                    # ✅ Use the SAME route_id created in Step 05
                    route_id_arg = str(route_id) if route_id is not None else args.route_id_or_new

                    out = _run_script(
                        root,
                        script_path,
                        args=[route_id_arg, str(osm_relation_id)],
                    )

                    # ✅ If we already have route_id from Step 05, DO NOT overwrite it.
                    if route_id is None:
                        if args.route_id_or_new == "new":
                            route_id = _extract_uuid("route_id:", out)
                        else:
                            route_id = uuid.UUID(args.route_id_or_new)


            elif step.code == 20:
                if not route_id:
                    route_id = uuid.UUID(args.route_id_or_new) if args.route_id_or_new != "new" else None
                if not route_id:
                    raise RuntimeError("route_id not known before Step 20.")

                out = _run_script(root, script_path, args=[str(route_id)])
                seq_set_id = _extract_uuid("stop_sequence_candidate_set_id:", out)

                seq_candidate_id = _pick_stop_sequence_candidate_id(seq_set_id)
                print(f"✅ Auto-picked stop_sequence_candidate_id: {seq_candidate_id}")

                # Auto-approve the picked sequence so Step 30 can build geometry.
                # Legacy run_all had no auto-approve; the V2 constructor does,
                # so we mirror that behavior here for batch use.
                from src.db.conn import db_conn as _db_conn, db_cursor as _db_cursor
                with _db_conn() as _conn:
                    with _db_cursor(_conn) as _cur:
                        _cur.execute(
                            """
                            INSERT INTO route_work.sequence_approvals
                              (route_id, stop_sequence_set_id,
                               chosen_stop_sequence_candidate_id,
                               approval_status, approved_at, approved_by, notes)
                            VALUES (%s, %s, %s, 'approved', now(), %s, %s)
                            ON CONFLICT (route_id) DO UPDATE SET
                              stop_sequence_set_id = EXCLUDED.stop_sequence_set_id,
                              chosen_stop_sequence_candidate_id = EXCLUDED.chosen_stop_sequence_candidate_id,
                              approval_status = 'approved',
                              approved_at = now(),
                              approved_by = EXCLUDED.approved_by,
                              notes = EXCLUDED.notes,
                              invalidated_at = NULL,
                              invalidated_reason = NULL
                            """,
                            (
                                str(route_id),
                                str(seq_set_id),
                                str(seq_candidate_id),
                                "run_all.py",
                                "Auto-approved by run_all.py batch runner",
                            ),
                        )
                    _conn.commit()
                print(f"✅ Auto-approved sequence {seq_candidate_id}")

            elif step.code == 30:
                if not route_id or not seq_candidate_id:
                    raise RuntimeError("route_id or stop_sequence_candidate_id not known before Step 30.")
                out = _run_script(root, script_path, args=[str(route_id), str(seq_candidate_id)])
                geom_set_id = _extract_uuid("geometry_candidate_set_id:", out)

            elif step.code == 32:
                if not route_id or not geom_set_id:
                    raise RuntimeError("route_id or geometry_set_id not known before Step 32.")
                _run_script(root, script_path, args=[str(route_id), str(geom_set_id)])

            elif step.code == 35:
                if not route_id or not geom_set_id:
                    raise RuntimeError("route_id or geometry_set_id not known before Step 35.")
                _run_script(root, script_path, args=[str(route_id), str(geom_set_id)])

            elif step.code == 40:
                if not route_id or not geom_set_id:
                    raise RuntimeError("route_id or geometry_set_id not known before Step 40.")
                out = _run_script(root, script_path, args=[str(route_id), str(geom_set_id)])
                approved_id = _extract_uuid("approved geometry_candidate_id:", out)

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
    print("\nIDs produced:")
    print("  route_id:", route_id)
    print("  osm_relation_id:", osm_relation_id)
    print("  stop_sequence_candidate_set_id:", seq_set_id)
    print("  stop_sequence_candidate_id:", seq_candidate_id)
    print("  geometry_candidate_set_id:", geom_set_id)
    print("  approved_geometry_candidate_id:", approved_id)

    if failed:
        raise SystemExit(1)

    print("\n🎉 Phase3 pipeline completed successfully.")


if __name__ == "__main__":
    main()
