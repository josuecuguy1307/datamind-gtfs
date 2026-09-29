from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from datamind_console.db.db import db_conn


def apply_sql(path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def main() -> None:
    sql_dir = ROOT / "datamind_console" / "sql"
    files = sorted(sql_dir.glob("*.sql"))
    if not files:
        print("No SQL files found.")
        return

    print(f"Applying {len(files)} SQL migration file(s) from {sql_dir}...")
    for path in files:
        print(f" - {path.name}")
        apply_sql(path)
    print("Done.")


if __name__ == "__main__":
    main()
