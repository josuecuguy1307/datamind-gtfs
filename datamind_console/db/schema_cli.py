"""Explicit schema administration for DataMind runtime schema units.

Runtime code (Streamlit pages, service clients) only *verifies* schema
objects. This command is the single place that may *apply* the SQL behind a
unit. Usage, from the repository root:

    python -m datamind_console.db.schema_cli status            # read-only check of every unit
    python -m datamind_console.db.schema_cli plan phase3.direction_gate
    python -m datamind_console.db.schema_cli apply phase3.direction_gate --yes

``apply`` refuses to run without ``--yes`` and always prints the plan first,
including how many data-changing statements each file contains. Treat any
``apply`` against an operational database as a data migration that needs
owner approval.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence

from datamind_console.common.env import load_project_env

load_project_env()  # before datamind_console.common.config builds CFG

from datamind_console.db import ai_schema  # noqa: E402
from datamind_console.db import schema_guard  # noqa: E402
from datamind_console.db.db import db_conn  # noqa: E402
from datamind_console.phases.phase3_routes import schema as phase3_schema  # noqa: E402
from datamind_console.phases.phase4_naming import schema as phase4_schema  # noqa: E402
from datamind_console.phases.phase5_gtfs import schema as phase5_schema  # noqa: E402


def all_units() -> dict[str, schema_guard.SchemaUnit]:
    units = [*phase3_schema.UNITS, *phase4_schema.UNITS, *phase5_schema.UNITS, *ai_schema.UNITS]
    return {u.key: u for u in units}


def _target_label() -> str:
    """Host/port/dbname of the active connection (never credentials)."""
    try:
        with db_conn(readonly=True) as conn:
            info = conn.info
            return f"{info.host}:{info.port}/{info.dbname}"
    except Exception as exc:  # pragma: no cover - surfaced to the operator
        return f"unavailable ({exc.__class__.__name__}: {exc})"


def _integrity_hints() -> list[str]:
    """Read-only counts showing whether a unit's backfill would do anything."""
    hints: list[str] = []
    try:
        with db_conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM route_raw.route_jobs "
                    "WHERE service_route_id IS NULL OR direction_id IS NULL"
                )
                unlinked = int(cur.fetchone()[0])
        hints.append(
            f"phase3.direction_gate: {unlinked} route job(s) not linked to a service route "
            "(the 012 backfill would link them)."
        )
    except Exception as exc:
        hints.append(f"phase3.direction_gate: integrity hint unavailable ({exc.__class__.__name__}).")
    return hints


def cmd_status(args: argparse.Namespace) -> int:
    units = all_units()
    rows: list[dict[str, Any]] = []
    for key, unit in units.items():
        try:
            result = schema_guard.check(unit, lambda: db_conn(readonly=True))
            rows.append({"unit": key, "ok": result.ok, "missing": result.missing, "missing_files": result.missing_files})
        except Exception as exc:
            rows.append({"unit": key, "ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
    if args.json:
        print(json.dumps({"target": _target_label(), "units": rows, "hints": _integrity_hints()}, indent=2))
    else:
        print(f"Target: {_target_label()}")
        for row in rows:
            state = "OK     " if row.get("ok") else "MISSING"
            print(f"  {state} {row['unit']}")
            for item in (row.get("missing") or [])[:12]:
                print(f"           - {item}")
            if row.get("error"):
                print(f"           ! {row['error']}")
        for hint in _integrity_hints():
            print(f"  note: {hint}")
    return 0 if all(r.get("ok") for r in rows) else 1


def _print_plan(unit: schema_guard.SchemaUnit, include_dependencies: bool) -> None:
    print(f"Plan for {unit.key} — {unit.title}")
    if unit.notes:
        print(f"  NOTE: {unit.notes}")
    for step in schema_guard.plan(unit, include_dependencies=include_dependencies):
        if step["kind"] == "statement":
            print(f"  [{step['unit']}] statement: {step['sql']}")
            continue
        profile = step.get("profile") or {}
        flag = "  ⚠ data-changing statements" if profile.get("data_changes") else ""
        missing = "" if step.get("exists") else "  (file missing — skipped)"
        print(
            f"  [{step['unit']}] {step['path']}{missing}\n"
            f"      creates={profile.get('creates', 0)} alters={profile.get('alters', 0)} "
            f"drops={profile.get('drops', 0)} data_changes={profile.get('data_changes', 0)}{flag}"
        )


def cmd_plan(args: argparse.Namespace) -> int:
    unit = all_units().get(args.unit)
    if unit is None:
        print(f"Unknown unit: {args.unit}. Known: {', '.join(sorted(all_units()))}", file=sys.stderr)
        return 2
    _print_plan(unit, include_dependencies=not args.no_deps)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    unit = all_units().get(args.unit)
    if unit is None:
        print(f"Unknown unit: {args.unit}. Known: {', '.join(sorted(all_units()))}", file=sys.stderr)
        return 2
    print(f"Target: {_target_label()}")
    _print_plan(unit, include_dependencies=not args.no_deps)
    if not args.yes:
        print("\nRefusing to apply without --yes. Review the plan above; data-changing files are a data migration.")
        return 3
    executed = schema_guard.apply(
        unit,
        lambda: db_conn(),
        include_dependencies=not args.no_deps,
        echo=print,
    )
    result = schema_guard.check(unit, lambda: db_conn(readonly=True))
    print(f"Applied {len(executed)} step(s). Verification: {'OK' if result.ok else 'MISSING ' + ', '.join(result.missing)}")
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="schema_cli", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Read-only verification of every unit")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_plan = sub.add_parser("plan", help="Show what apply would execute (no database access)")
    p_plan.add_argument("unit")
    p_plan.add_argument("--no-deps", action="store_true")
    p_plan.set_defaults(func=cmd_plan)

    p_apply = sub.add_parser("apply", help="Apply a unit's SQL (requires --yes)")
    p_apply.add_argument("unit")
    p_apply.add_argument("--yes", action="store_true", help="Confirm the plan; without it nothing runs")
    p_apply.add_argument("--no-deps", action="store_true")
    p_apply.set_defaults(func=cmd_apply)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
