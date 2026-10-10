"""Phase 5 schema unit (verification at runtime, application only via CLI).

``Phase5Client._ensure_schema`` used to call ``run_all_migrations()`` the
first time any GTFS method ran in a process. Its "already applied?" probe
only ran once the flag was already set, so every fresh process re-applied
all V5 migrations, string-sorted (V5_10 before V5_2). V5_11 alone has
16 data-changing statements and 12 DROPs.

Runtime code now only verifies the resulting objects. Applying the migrations
is explicit:

    python -m datamind_console.db.schema_cli plan phase5.gtfs_work
    python -m datamind_console.db.schema_cli apply phase5.gtfs_work --yes
"""
from __future__ import annotations

from pathlib import Path

from datamind_console.db.schema_guard import SchemaUnit
from phase5_gtfs.schedules.store import list_migrations

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = PROJECT_ROOT / "phase5_gtfs" / "db" / "migrations"

GTFS_WORK = SchemaUnit(
    key="phase5.gtfs_work",
    title="Phase 5 · GTFS work schema (V5 migrations)",
    sql_files=tuple(list_migrations(MIGRATIONS_DIR)),
    notes="V5_5, V5_9 and V5_11 contain data backfills; review `plan` output first.",
)

UNITS: tuple[SchemaUnit, ...] = (GTFS_WORK,)
