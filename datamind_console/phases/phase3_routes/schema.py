"""Phase 3 schema units (verification at runtime, application only via CLI).

Each unit maps one of the former ``Phase3Client._ensure_*`` routines to the
SQL files it used to execute lazily. Runtime code now only *verifies* these
objects (see :mod:`datamind_console.db.schema_guard`). Applying the files is an
explicit administrative action:

    python -m datamind_console.db.schema_cli status
    python -m datamind_console.db.schema_cli plan phase3.direction_gate
    python -m datamind_console.db.schema_cli apply phase3.direction_gate --yes

Several files mix DDL with data backfills, so they must be reviewed before
being applied:

- ``012`` registers service routes for unlinked route jobs and recomputes
  every ``route_raw.service_routes.route_approval_status`` with a looser rule
  than the client uses.
- ``023`` upserts ``route_work.sequence_approvals`` (re-approving invalidated
  rows) and touches ``route_prod.routes``.

``schema_cli plan`` prints a statement profile before anything runs.
"""
from __future__ import annotations

from pathlib import Path

from datamind_console.db.schema_guard import SchemaUnit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = PROJECT_ROOT / "phase3_routes" / "services" / "route_constructor" / "sql"


def _sql(name: str) -> Path:
    return SQL_DIR / name


DIRECTION_GATE = SchemaUnit(
    key="phase3.direction_gate",
    title="Phase 3 · direction gate (service routes and direction slots)",
    sql_files=(_sql("012_route_direction_gate.sql"),),
    notes="Contains backfills: route_jobs → service_routes, route_prod.routes mapping, approval-status recompute.",
)

MANUAL_SEQUENCE = SchemaUnit(
    key="phase3.manual_sequence",
    title="Phase 3 · manual sequence builder",
    sql_files=(_sql("021_manual_sequence_builder.sql"),),
)

TRASH = SchemaUnit(
    key="phase3.trash",
    title="Phase 3 · route trash and delete events",
    sql_files=(_sql("030_route_trash.sql"),),
)

SEQUENCE_RESOLUTION = SchemaUnit(
    key="phase3.sequence_resolution",
    title="Phase 3 · canonical sequence approvals",
    sql_files=(_sql("023_sequence_resolution.sql"),),
    notes="Contains backfills: sequence_approvals upsert (re-approves invalidated rows) and route_prod.routes sync.",
)

EXTRACTOR_REVIEW = SchemaUnit(
    key="phase3.extractor_review",
    title="Phase 3 · extractor review columns",
    sql_files=(_sql("013_route_extract_review.sql"),),
)

ROUTE_REVIEW = SchemaUnit(
    key="phase3.route_review",
    title="Phase 3 · route review, dedupe and coverage catalog",
    sql_files=(_sql("022_route_review_coverage.sql"),),
    depends_on=(MANUAL_SEQUENCE, TRASH),
    notes="Drops and recreates catalog views with CASCADE.",
)

INVERSE_COMPLETION = SchemaUnit(
    key="phase3.inverse_completion",
    title="Phase 3 · inverse direction completion",
    sql_files=(
        _sql("024_inverse_direction_status.sql"),
        _sql("025_inverse_direction_proposals.sql"),
        _sql("026_inverse_direction_search.sql"),
    ),
    depends_on=(DIRECTION_GATE,),
    pre_statements=("DROP VIEW IF EXISTS route_work.v_direction_readiness",),
    single_transaction=True,
)

GEOMETRY_STOP_RECOVERY = SchemaUnit(
    key="phase3.geometry_stop_recovery",
    title="Phase 3 · geometry stop recovery",
    sql_files=(_sql("027_geometry_stop_recovery.sql"),),
)

UNITS: tuple[SchemaUnit, ...] = (
    DIRECTION_GATE,
    MANUAL_SEQUENCE,
    TRASH,
    SEQUENCE_RESOLUTION,
    EXTRACTOR_REVIEW,
    ROUTE_REVIEW,
    INVERSE_COMPLETION,
    GEOMETRY_STOP_RECOVERY,
)
