#!/usr/bin/env python3
"""Build the EMPTY DataMind database: extensions + full schema (db/schema.sql).

    cp .env.example .env        # and fill in DB_DSN
    python scripts/init_db.py                 # creates everything (fails if it already exists)
    python scripts/init_db.py --reset --yes   # DROPS the DataMind schemas and recreates them

Requirements: PostgreSQL 15+ with the postgis, vector (pgvector), pgcrypto, pg_trgm, citext,
unaccent and dblink extensions available, and a user allowed to CREATE EXTENSION (or the
extensions already created). Tested with PostgreSQL 17.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from datamind_core.dsn import MissingConfigError, need_dsn  # noqa: E402

SCHEMA_SQL = ROOT / "db" / "schema.sql"
EXTENSIONS = ["postgis", "vector", "pgcrypto", "pg_trgm", "citext", "unaccent", "dblink"]
APP_SCHEMAS = [
    "ai", "automation", "catalog", "console", "geo_prod", "geo_raw", "geo_work", "gtfs", "gtfs_prod",
    "gtfs_work", "node_prod", "node_raw", "node_work", "route_prod", "route_raw", "route_review",
    "route_trash", "route_work", "semantics",
]


def load_dotenv() -> None:
    """Load .env if it exists (no extra dependencies)."""
    import os

    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip("\"'"))


def existing_schemas(cur) -> list[str]:
    cur.execute("SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)", (APP_SCHEMAS,))
    return sorted(r[0] for r in cur.fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", help="PostgreSQL DSN (default: DB_DSN / DATABASE_URL from the environment or .env)")
    ap.add_argument("--reset", action="store_true", help="drop the DataMind schemas before creating")
    ap.add_argument("--yes", action="store_true", help="confirm --reset (destructive)")
    args = ap.parse_args()

    load_dotenv()
    try:
        dsn = need_dsn(args.dsn)
    except MissingConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SHOW server_version_num")
    if int(cur.fetchone()[0]) < 150000:
        print("ERROR: PostgreSQL 15 or newer is required.", file=sys.stderr)
        return 2

    present = existing_schemas(cur)
    if present and not args.reset:
        print(f"The database already has DataMind schemas ({', '.join(present)}). "
              "Use --reset --yes to drop and recreate them.", file=sys.stderr)
        return 1
    if present and args.reset:
        if not args.yes:
            print("--reset deletes ALL data in the DataMind schemas. Repeat with --yes to confirm.",
                  file=sys.stderr)
            return 1
        for s in APP_SCHEMAS:
            cur.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
        print(f"Schemas dropped: {', '.join(present)}")

    for ext in EXTENSIONS:
        try:
            cur.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')
        except psycopg2.Error as e:
            print(f"ERROR creating extension {ext}: {e.pgerror or e}\n"
                  f"Install it on the server (postgis, pgvector…) or ask a superuser to create it.",
                  file=sys.stderr)
            return 3

    conn.autocommit = False
    try:
        cur = conn.cursor()
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        conn.commit()
    except psycopg2.Error as e:
        conn.rollback()
        print(f"ERROR applying db/schema.sql (nothing was applied): {e.pgerror or e}", file=sys.stderr)
        return 4

    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = ANY(%s) AND table_type='BASE TABLE'", (APP_SCHEMAS,))
    tables = cur.fetchone()[0]
    print(f"✓ Database created: {len(APP_SCHEMAS)} schemas, {tables} tables. Next: python scripts/load_sample_data.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
