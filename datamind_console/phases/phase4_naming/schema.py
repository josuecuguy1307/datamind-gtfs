"""Phase 4 schema unit for Evidence / Matching (verified at runtime, applied only via CLI).

    python -m datamind_console.db.schema_cli plan phase4.evidence_matching
    python -m datamind_console.db.schema_cli apply phase4.evidence_matching --yes
"""
from __future__ import annotations

from pathlib import Path

from datamind_console.db.schema_guard import SchemaUnit

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = PROJECT_ROOT / "phase4_naming" / "db" / "migrations"

EVIDENCE_MATCHING = SchemaUnit(
    key="phase4.evidence_matching",
    title="Phase 4 · evidence matches and review labels (V4_8)",
    sql_files=(MIGRATIONS_DIR / "V4_8__evidence_matching.sql",),
    extra_relations=("semantics.route_evidence_records", "console.phase_decisions"),
    notes="DDL only: creates two tables and their indexes; writes no rows.",
)

UNITS: tuple[SchemaUnit, ...] = (EVIDENCE_MATCHING,)
