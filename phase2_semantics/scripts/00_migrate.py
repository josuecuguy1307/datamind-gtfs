"""
Phase 2 – Semantic Geocoder
Migration bootstrap script.

Responsibilities:
- Validate environment
- Apply SQL migrations in order
- Ensure DB schemas are ready

This script is SAFE to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from psycopg2 import connect
from psycopg2.extensions import connection as PGConnection

from src.settings import (
    BASE_DIR,
    DB_DSN,
    validate_settings,
)


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

SQL_DIR = BASE_DIR / "sql"

SQL_FILES: List[str] = [
    "001_geo_raw.sql",
    "010_geo_work_core.sql",
    "011_geo_work_views.sql",
    "012_geo_work_ml.sql",
    "015_geo_work_geo_context.sql",
    "020_geo_prod_core.sql",
    "030_geo_prod_embeddings.sql",
    "035_geo_work_name_candidates.sql",
    "036_geo_work_poi_stop_feedback.sql",
    "037_geo_prod_search_indexes.sql",
    # Optional, uncomment if managed here:
    # "040_opensearch_places_alias.sql",
]


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def apply_sql_file(conn: PGConnection, path: Path) -> None:
    print(f"→ Applying {path.name}")

    sql = path.read_text(encoding="utf-8")

    with conn.cursor() as cur:
        cur.execute(sql)


def migrate(conn: PGConnection) -> None:
    for filename in SQL_FILES:
        path = SQL_DIR / filename

        if not path.exists():
            raise FileNotFoundError(f"Missing SQL migration: {path}")

        apply_sql_file(conn, path)


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main() -> None:
    print("🔧 Phase 2 – Semantic Geocoder migration")
    print("======================================")

    # 1️⃣ Validate env
    validate_settings()
    print("✓ Environment validated")

    # 2️⃣ Connect DB
    print("→ Connecting to Postgres")
    conn = connect(DB_DSN)
    conn.autocommit = False

    try:
        # 3️⃣ Apply migrations
        migrate(conn)

        # 4️⃣ Commit
        conn.commit()
        print("✓ All migrations applied successfully")

    except Exception as e:
        conn.rollback()
        print("✗ Migration failed, rolled back")
        print(f"ERROR: {e}")
        raise

    finally:
        conn.close()
        print("✓ Connection closed")


if __name__ == "__main__":
    main()
