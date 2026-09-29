"""
Phase 4-5 Automation — Agent 4: Patch Agent

Reads diagnostic failures from automation.diagnostics and suggestions
from Agent 1 reports. Applies safe auto-fixes and queues items that
need human approval.

Auto-fixable patches (requires_human_approval=False):
  - Fill missing catalog.route_schedule_profile entries (from partner/defaults)
  - Fill missing catalog.route_service_days entries (from defaults)
  - Create missing runtime estimate bindings (triggers runtime lab)

Human-approval patches (logged but not auto-applied):
  - Missing catalog.route_semantics entries
  - Route approval decisions
  - Layover policy overrides

Usage:
    python src/automation/agent_patch.py --all
    python src/automation/agent_patch.py --route <route_id>
    python src/automation/agent_patch.py --batch <id1>,<id2>
    python src/automation/agent_patch.py --all --dry-run
    python src/automation/agent_patch.py --all --auto-fix   # apply safe patches
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

DB_DSN = (
    os.environ.get("DB_DSN")
    or os.environ.get("DATABASE_URL")
    or "postgresql://localhost:5432/datamind_ml"
)

# Default schedule values for auto-fill
DEFAULT_WEEKDAY_START = "05:30:00"
DEFAULT_WEEKDAY_END = "20:00:00"
DEFAULT_SATURDAY_START = "06:00:00"
DEFAULT_SATURDAY_END = "18:00:00"
DEFAULT_HEADWAY_MIN = 15
DEFAULT_LAYOVER_MIN = 5.0


# ------------------------------------------------------------------
# Data loading
# ------------------------------------------------------------------

def load_latest_diagnostics(
    cur, route_ids: list[str] | None = None,
) -> list[dict]:
    """Load the latest diagnostic report per route from automation.diagnostics."""
    where = "phase = 'pre_execution'"
    params: list[Any] = []
    if route_ids:
        where += " AND route_id = ANY(%s::uuid[])"
        params.append(route_ids)

    cur.execute(f"""
        SELECT DISTINCT ON (route_id)
            route_id::text, run_id, status, report
        FROM automation.diagnostics
        WHERE {where}
        ORDER BY route_id, created_at DESC
    """, params)
    return [dict(row) for row in cur.fetchall()]


def load_valhalla_diagnostics(
    cur, route_ids: list[str],
) -> dict[str, dict]:
    """Load latest valhalla cross-check results keyed by route_id."""
    cur.execute("""
        SELECT DISTINCT ON (route_id)
            route_id::text, report
        FROM automation.diagnostics
        WHERE phase = 'valhalla_crosscheck'
          AND route_id = ANY(%s::uuid[])
        ORDER BY route_id, created_at DESC
    """, (route_ids,))
    return {row["route_id"]: row["report"] for row in cur.fetchall()}


def load_route_info(cur, route_ids: list[str]) -> dict[str, dict]:
    """Load route_prod.routes info."""
    cur.execute("""
        SELECT r.route_id::text, r.direction_id, r.service_route_id::text
        FROM route_prod.routes r
        WHERE r.route_id = ANY(%s::uuid[])
    """, (route_ids,))
    return {row["route_id"]: dict(row) for row in cur.fetchall()}


# ------------------------------------------------------------------
# Patch analysis
# ------------------------------------------------------------------

def analyze_route(
    route_id: str,
    diagnostic: dict,
    valhalla: dict | None,
    route_info: dict[str, dict],
) -> list[dict]:
    """Analyze a single route's diagnostics and produce patch proposals.
    Returns list of patch dicts."""
    report = diagnostic.get("report", {})
    if isinstance(report, str):
        report = json.loads(report)

    status = diagnostic["status"]
    gates = report.get("gates", [])
    suggestions = report.get("suggestions", [])
    layover = report.get("layover_analysis", {})

    patches = []

    # ── Process gate failures ──────────────────────────────────────
    failed_hard = [g for g in gates if g.get("gate") == "HARD" and not g.get("passed")]
    failed_soft = [g for g in gates if g.get("gate") == "SOFT" and not g.get("passed")]

    for gate in failed_hard:
        patch = _patch_for_gate(route_id, gate, route_info)
        if patch:
            patches.append(patch)

    for gate in failed_soft:
        patch = _patch_for_gate(route_id, gate, route_info)
        if patch:
            patches.append(patch)

    # ── Process suggestions from Agent 1 ───────────────────────────
    for sug in suggestions:
        patch = _patch_from_suggestion(route_id, sug)
        if patch:
            # Dedup: skip if we already have a patch for the same target
            existing = {p["target"] for p in patches}
            if patch["target"] not in existing:
                patches.append(patch)

    # ── Valhalla cross-check patches ───────────────────────────────
    if valhalla:
        v_report = valhalla if isinstance(valhalla, dict) else json.loads(valhalla)
        v_status = v_report.get("status")
        if v_status == "FLAGGED":
            divergence = v_report.get("divergence_pct", 0)
            patches.append({
                "route_id": route_id,
                "patch_type": "valhalla_divergence",
                "severity": "REVIEW",
                "target": "runtime_estimate",
                "auto_fixable": False,
                "description": (
                    f"Valhalla runtime diverges {divergence:+.1f}% from catalog estimate. "
                    f"Review stop placement and runtime model."
                ),
                "proposed_action": "Re-run estimate_runtime_from_catalog() after reviewing stops",
            })

    return patches


def _patch_for_gate(
    route_id: str,
    gate: dict,
    route_info: dict[str, dict],
) -> dict | None:
    """Generate a patch proposal for a failed gate."""
    name = gate.get("rule") or gate.get("name", "")

    if name == "route_exists_in_semantics":
        return {
            "route_id": route_id,
            "patch_type": "missing_semantics",
            "severity": "HARD",
            "target": "catalog.route_semantics",
            "auto_fixable": False,
            "description": "Route not in catalog — requires manual entry with operator/name data",
            "proposed_action": "INSERT INTO catalog.route_semantics with route identity data",
        }

    if name in ("approved", "route_approved"):
        return {
            "route_id": route_id,
            "patch_type": "not_approved",
            "severity": "HARD",
            "target": "catalog.route_semantics",
            "auto_fixable": False,
            "description": "Route not approved — operator must review and approve",
            "proposed_action": "UPDATE catalog.route_semantics SET approved=TRUE",
        }

    if name == "has_service_days":
        return {
            "route_id": route_id,
            "patch_type": "missing_service_days",
            "severity": "HARD",
            "target": "catalog.route_service_days",
            "auto_fixable": True,
            "description": "No service_days entries — will insert default weekday+saturday patterns",
            "proposed_action": "insert_default_service_days",
            "proposed_values": {
                "patterns": ["weekday_normal", "saturday_reduced"],
            },
        }

    if name == "schedule_profile_both_directions":
        info = route_info.get(route_id, {})
        my_dir = int(info.get("direction_id", 0))
        missing_dir = gate.get("detail", "")
        return {
            "route_id": route_id,
            "patch_type": "missing_schedule_profile",
            "severity": "HARD",
            "target": "catalog.route_schedule_profile",
            "auto_fixable": True,
            "description": f"Missing schedule profile — {missing_dir}",
            "proposed_action": "insert_default_schedule_profile",
            "proposed_values": {
                "service_pattern_id": "weekday_normal",
                "window_start": DEFAULT_WEEKDAY_START,
                "window_end": DEFAULT_WEEKDAY_END,
                "headway_min": DEFAULT_HEADWAY_MIN,
            },
        }

    if name == "runtime_model_both_directions":
        return {
            "route_id": route_id,
            "patch_type": "missing_runtime_binding",
            "severity": "SOFT",
            "target": "gtfs_work.route_runtime_estimate_bindings",
            "auto_fixable": False,
            "description": "Missing runtime estimate binding — run Runtime Lab",
            "proposed_action": "Run estimate_runtime_from_catalog() for missing direction(s)",
        }

    if name == "layover_policy_exists":
        return {
            "route_id": route_id,
            "patch_type": "missing_layover_policy",
            "severity": "SOFT",
            "target": "catalog.route_layover_policy",
            "auto_fixable": True,
            "description": "No layover policy — will insert default 5-min turnaround",
            "proposed_action": "insert_default_layover_policy",
            "proposed_values": {
                "layover_at_destination_min": DEFAULT_LAYOVER_MIN,
                "layover_at_origin_min": DEFAULT_LAYOVER_MIN,
            },
        }

    return None


def _patch_from_suggestion(route_id: str, sug: dict) -> dict | None:
    """Convert an Agent 1 suggestion into a patch proposal."""
    stype = sug.get("type", "")

    if stype == "catalog_fill":
        target = sug.get("target", "unknown")
        return {
            "route_id": route_id,
            "patch_type": f"catalog_fill_{target.split('.')[-1]}",
            "severity": "HARD" if sug.get("requires_human_approval") else "SOFT",
            "target": target,
            "auto_fixable": not sug.get("requires_human_approval", True),
            "description": sug.get("detail", "Missing catalog data"),
            "proposed_action": "fill_from_defaults",
            "proposed_values": sug.get("proposed_values"),
        }

    if stype == "runtime_estimate_needed":
        return {
            "route_id": route_id,
            "patch_type": "runtime_estimate_needed",
            "severity": "SOFT",
            "target": sug.get("target", "gtfs_work.route_runtime_estimate_bindings"),
            "auto_fixable": False,
            "description": sug.get("detail", "Missing runtime binding"),
            "proposed_action": "run_runtime_lab",
        }

    if stype == "approval_needed":
        return {
            "route_id": route_id,
            "patch_type": "approval_needed",
            "severity": "HARD",
            "target": sug.get("target", "catalog.route_semantics"),
            "auto_fixable": False,
            "description": sug.get("detail", "Route needs approval"),
            "proposed_action": "operator_approve",
        }

    return None


# ------------------------------------------------------------------
# Auto-fix execution
# ------------------------------------------------------------------

def apply_patch(cur, patch: dict, dry_run: bool) -> dict:
    """Apply a single auto-fixable patch. Returns result dict."""
    route_id = patch["route_id"]
    action = patch.get("proposed_action", "")
    result = {
        "route_id": route_id,
        "patch_type": patch["patch_type"],
        "applied": False,
        "action": "DRY_RUN" if dry_run else None,
        "detail": None,
    }

    if action == "insert_default_service_days":
        result = _apply_default_service_days(cur, route_id, dry_run)
        result["patch_type"] = patch["patch_type"]

    elif action == "insert_default_schedule_profile":
        vals = patch.get("proposed_values", {})
        result = _apply_default_schedule_profile(cur, route_id, vals, dry_run)
        result["patch_type"] = patch["patch_type"]

    elif action == "insert_default_layover_policy":
        vals = patch.get("proposed_values", {})
        result = _apply_default_layover_policy(cur, route_id, vals, dry_run)
        result["patch_type"] = patch["patch_type"]

    else:
        result["action"] = "SKIP_NOT_AUTO_FIXABLE"
        result["detail"] = f"Action '{action}' requires human intervention"

    return result


def _apply_default_service_days(cur, route_id: str, dry_run: bool) -> dict:
    """Insert default weekday + saturday service day patterns."""
    patterns = [
        {
            "id": "weekday_normal",
            "mon": True, "tue": True, "wed": True, "thu": True, "fri": True,
            "sat": False, "sun": False,
            "first": DEFAULT_WEEKDAY_START, "last": DEFAULT_WEEKDAY_END,
        },
        {
            "id": "saturday_reduced",
            "mon": False, "tue": False, "wed": False, "thu": False, "fri": False,
            "sat": True, "sun": False,
            "first": DEFAULT_SATURDAY_START, "last": DEFAULT_SATURDAY_END,
        },
    ]

    if dry_run:
        return {"route_id": route_id, "applied": True, "action": "DRY_RUN",
                "detail": f"Would insert {len(patterns)} service_days patterns"}

    inserted = 0
    for p in patterns:
        cur.execute("""
            INSERT INTO catalog.route_service_days (
                route_id, service_pattern_id,
                monday, tuesday, wednesday, thursday, friday, saturday, sunday,
                first_departure, last_departure,
                valid_from, source, confidence
            ) VALUES (
                %(rid)s::uuid, %(pat)s,
                %(mon)s, %(tue)s, %(wed)s, %(thu)s, %(fri)s, %(sat)s, %(sun)s,
                %(first)s, %(last)s,
                CURRENT_DATE, 'agent_patch_default', 0.20
            )
            ON CONFLICT (route_id, service_pattern_id) DO NOTHING
        """, {
            "rid": route_id, "pat": p["id"],
            "mon": p["mon"], "tue": p["tue"], "wed": p["wed"],
            "thu": p["thu"], "fri": p["fri"], "sat": p["sat"], "sun": p["sun"],
            "first": p["first"], "last": p["last"],
        })
        if cur.rowcount > 0:
            inserted += 1

    return {"route_id": route_id, "applied": inserted > 0,
            "action": "INSERTED" if inserted > 0 else "ALREADY_EXISTS",
            "detail": f"{inserted} service_days patterns inserted"}


def _apply_default_schedule_profile(
    cur, route_id: str, vals: dict, dry_run: bool,
) -> dict:
    """Insert a default schedule profile for a route."""
    # Find which direction_ids exist for this route
    cur.execute("""
        SELECT direction_id FROM route_prod.routes
        WHERE route_id = %s::uuid
    """, (route_id,))
    row = cur.fetchone()
    if not row:
        return {"route_id": route_id, "applied": False,
                "action": "SKIP", "detail": "Route not found in route_prod"}

    direction_id = int(row["direction_id"])

    # Check which directions already have profiles
    cur.execute("""
        SELECT direction_id FROM catalog.route_schedule_profile
        WHERE route_id = %s::uuid
    """, (route_id,))
    existing_dirs = {int(r["direction_id"]) for r in cur.fetchall()}

    if direction_id in existing_dirs:
        return {"route_id": route_id, "applied": False,
                "action": "ALREADY_EXISTS",
                "detail": f"Profile already exists for direction {direction_id}"}

    if dry_run:
        return {"route_id": route_id, "applied": True, "action": "DRY_RUN",
                "detail": f"Would insert profile for direction {direction_id}"}

    cur.execute("""
        INSERT INTO catalog.route_schedule_profile (
            route_id, direction_id, service_pattern_id,
            window_start, window_end, headway_min,
            peak_type, source, confidence
        ) VALUES (
            %(rid)s::uuid, %(dir)s, %(pat)s,
            %(start)s, %(end)s, %(headway)s,
            'offpeak', 'agent_patch_default', 0.20
        )
        ON CONFLICT (route_id, direction_id, service_pattern_id, window_start) DO NOTHING
    """, {
        "rid": route_id,
        "dir": direction_id,
        "pat": vals.get("service_pattern_id", "weekday_normal"),
        "start": vals.get("window_start", DEFAULT_WEEKDAY_START),
        "end": vals.get("window_end", DEFAULT_WEEKDAY_END),
        "headway": vals.get("headway_min", DEFAULT_HEADWAY_MIN),
    })

    return {"route_id": route_id, "applied": cur.rowcount > 0,
            "action": "INSERTED" if cur.rowcount > 0 else "ALREADY_EXISTS",
            "detail": f"Profile inserted for direction {direction_id}"}


def _apply_default_layover_policy(
    cur, route_id: str, vals: dict, dry_run: bool,
) -> dict:
    """Insert a default layover policy."""
    if dry_run:
        return {"route_id": route_id, "applied": True, "action": "DRY_RUN",
                "detail": "Would insert default layover policy"}

    dest_min = float(vals.get("layover_at_destination_min", DEFAULT_LAYOVER_MIN))
    orig_min = float(vals.get("layover_at_origin_min", DEFAULT_LAYOVER_MIN))

    cur.execute("""
        INSERT INTO catalog.route_layover_policy (
            route_id,
            layover_at_destination_min, layover_at_origin_min,
            min_layover_min, max_layover_min,
            applies_to_pattern, source, confidence
        ) VALUES (
            %(rid)s::uuid,
            %(dest)s, %(orig)s,
            3.0, 15.0,
            'all', 'agent_patch_default', 0.20
        )
        ON CONFLICT (route_id, applies_to_pattern) DO NOTHING
    """, {"rid": route_id, "dest": dest_min, "orig": orig_min})

    return {"route_id": route_id, "applied": cur.rowcount > 0,
            "action": "INSERTED" if cur.rowcount > 0 else "ALREADY_EXISTS",
            "detail": "Layover policy inserted"}


# ------------------------------------------------------------------
# Diagnostic persistence
# ------------------------------------------------------------------

def persist_patch_records(cur, run_id: str, patches: list[dict], results: list[dict]) -> None:
    """Save patch attempts to automation.patches."""
    for patch, result in zip(patches, results):
        cur.execute("""
            INSERT INTO automation.patches (
                run_id, route_id, failed_agent, error_trace,
                file_patched, patch_diff, tests_passed, attempt_number
            ) VALUES (
                %s, %s::uuid, %s, %s,
                %s, %s, %s, 1
            )
        """, (
            run_id,
            patch["route_id"],
            patch.get("patch_type", "unknown"),
            patch.get("description", ""),
            patch.get("target", ""),
            json.dumps(patch.get("proposed_values") or {}, default=str),
            result.get("applied", False),
        ))


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Agent 4 — Patch agent for Phase 4-5 automation"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Analyze all routes with diagnostics")
    group.add_argument("--route", type=str, help="Single route_id UUID")
    group.add_argument("--batch", type=str, help="Comma-separated route_id UUIDs")
    parser.add_argument("--dry-run", action="store_true", help="Preview patches without applying")
    parser.add_argument("--auto-fix", action="store_true",
                        help="Apply safe auto-fixable patches (default: report only)")
    parser.add_argument("--persist", action="store_true",
                        help="Save patch records to automation.patches")
    args = parser.parse_args()

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "_patch"

    conn = psycopg2.connect(DB_DSN, cursor_factory=RealDictCursor)
    conn.autocommit = False

    try:
        cur = conn.cursor()

        # Resolve route_ids
        route_ids = None
        if args.route:
            route_ids = [args.route]
        elif args.batch:
            route_ids = [r.strip() for r in args.batch.split(",")]

        # Load diagnostics
        diagnostics = load_latest_diagnostics(cur, route_ids)
        diag_route_ids = [d["route_id"] for d in diagnostics]

        # Load supporting data
        valhalla_diags = load_valhalla_diagnostics(cur, diag_route_ids) if diag_route_ids else {}
        route_info = load_route_info(cur, diag_route_ids) if diag_route_ids else {}

        print(f"Agent 4 — Patch Agent")
        print(f"Run ID: {run_id}")
        print(f"Mode: {'DRY RUN' if args.dry_run else 'AUTO-FIX' if args.auto_fix else 'REPORT ONLY'}")
        print(f"Routes with diagnostics: {len(diagnostics)}")
        blocked = sum(1 for d in diagnostics if d["status"] == "BLOCKED")
        warned = sum(1 for d in diagnostics if d["status"] == "WARNINGS")
        clean = sum(1 for d in diagnostics if d["status"] == "CLEAN")
        print(f"  BLOCKED: {blocked}  |  WARNINGS: {warned}  |  CLEAN: {clean}")
        print("=" * 80)

        if not diagnostics:
            print("\nNo diagnostics found. Run agent_prepare.py first.")
            conn.rollback()
            return

        # Analyze all routes
        all_patches: list[dict] = []
        all_results: list[dict] = []
        route_patch_counts: dict[str, int] = {}

        for diag in diagnostics:
            rid = diag["route_id"]
            v_diag = valhalla_diags.get(rid)

            patches = analyze_route(rid, diag, v_diag, route_info)
            route_patch_counts[rid] = len(patches)

            if not patches:
                continue

            ref = diag.get("report", {})
            if isinstance(ref, str):
                ref = json.loads(ref)
            route_ref = ref.get("route_short_name", rid[:12])

            for patch in patches:
                auto = "AUTO" if patch.get("auto_fixable") else "MANUAL"
                sev = patch.get("severity", "?")
                desc = patch.get("description", "")[:60]
                print(f"  [{sev:6s}] [{auto:6s}] {route_ref:8s} d{route_info.get(rid, {}).get('direction_id', '?')} | "
                      f"{patch['patch_type']:30s} | {desc}")

                # Apply if auto-fix mode
                if args.auto_fix and patch.get("auto_fixable") and not args.dry_run:
                    result = apply_patch(cur, patch, dry_run=False)
                    all_results.append(result)
                    applied = result.get("action", "?")
                    print(f"    → {applied}: {result.get('detail', '')}")
                elif args.dry_run and patch.get("auto_fixable"):
                    result = apply_patch(cur, patch, dry_run=True)
                    all_results.append(result)
                    print(f"    → DRY_RUN: {result.get('detail', '')}")
                else:
                    all_results.append({
                        "route_id": rid,
                        "patch_type": patch["patch_type"],
                        "applied": False,
                        "action": "REPORT_ONLY",
                    })

                all_patches.append(patch)

        # Persist patch records
        if args.persist and all_patches and not args.dry_run:
            persist_patch_records(cur, run_id, all_patches, all_results)

        # Summary
        print()
        print("=" * 80)
        total_patches = len(all_patches)
        auto_fixable = sum(1 for p in all_patches if p.get("auto_fixable"))
        manual = total_patches - auto_fixable
        applied = sum(1 for r in all_results if r.get("applied"))
        routes_with_patches = sum(1 for c in route_patch_counts.values() if c > 0)
        routes_clean = sum(1 for c in route_patch_counts.values() if c == 0)

        print(f"  Routes analyzed:     {len(diagnostics)}")
        print(f"  Routes with patches: {routes_with_patches}")
        print(f"  Routes clean:        {routes_clean}")
        print(f"  Total patches:       {total_patches}")
        print(f"    Auto-fixable:      {auto_fixable}")
        print(f"    Manual/review:     {manual}")
        print(f"  Applied:             {applied}")

        # Breakdown by type
        type_counts: dict[str, int] = {}
        for p in all_patches:
            pt = p.get("patch_type", "unknown")
            type_counts[pt] = type_counts.get(pt, 0) + 1
        if type_counts:
            print(f"\n  Patch type breakdown:")
            for pt, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                print(f"    {pt:35s} {count}")

        print("=" * 80)

        if args.dry_run:
            print("\nDRY RUN — no changes committed.")
            conn.rollback()
        elif args.auto_fix or args.persist:
            conn.commit()
            print(f"\nChanges committed. {applied} patches applied.")
        else:
            conn.rollback()
            print("\nREPORT ONLY — no changes. Use --auto-fix to apply safe patches.")

    except Exception as e:
        conn.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
