"""AI bot / insights schema unit (verified at runtime, applied only via CLI)."""
from __future__ import annotations

from pathlib import Path

from datamind_console.db.schema_guard import SchemaUnit

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

AI_LEARNING = SchemaUnit(
    key="ai.learning",
    title="AI bot · suggestions, labels, schedule, escalations, run logs",
    sql_files=(
        SQL_DIR / "004_ai_learning_agent.sql",
        SQL_DIR / "006_ai_insights_bot_logs.sql",
        SQL_DIR / "004b_ai_schedule_escalations_bootstrap.sql",
    ),
)

UNITS: tuple[SchemaUnit, ...] = (AI_LEARNING,)
