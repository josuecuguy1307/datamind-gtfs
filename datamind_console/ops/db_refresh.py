from __future__ import annotations

import csv
import os
import shlex
from pathlib import Path
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from .config import OpsConfig
from .otp_control import CommandResult, run_local_shell, run_ssh

GTFS_IMPORT_TABLES: list[tuple[str, str, str]] = [
    ("agency.txt", "gtfs", "agency"),
    ("stops.txt", "gtfs", "stops"),
    ("routes.txt", "gtfs", "routes"),
    ("calendar.txt", "gtfs", "calendar"),
    ("calendar_dates.txt", "gtfs", "calendar_dates"),
    ("trips.txt", "gtfs", "trips"),
    ("frequencies.txt", "gtfs", "frequencies"),
    ("shapes.txt", "gtfs", "shapes_points"),
    ("stop_times.txt", "gtfs", "stop_times"),
]

GTFS_SPECIAL_COLUMN_MAP: dict[str, dict[str, str]] = {
    "calendar.txt": {
        "start_date": "start_date_text",
        "end_date": "end_date_text",
    },
    "calendar_dates.txt": {
        "date": "date_text",
    },
}

GTFS_TRUNCATE_SQL = """
TRUNCATE TABLE
  gtfs.stop_times,
  gtfs.frequencies,
  gtfs.trips,
  gtfs.routes,
  gtfs.stops,
  gtfs.shapes_points,
  gtfs.calendar_dates,
  gtfs.calendar,
  gtfs.agency
CASCADE
"""


AWS_GTFS_DB_IMPORT_SCRIPT = r'''
import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TABLES = [
    ("agency.txt", "gtfs", "agency"),
    ("stops.txt", "gtfs", "stops"),
    ("routes.txt", "gtfs", "routes"),
    ("calendar.txt", "gtfs", "calendar"),
    ("calendar_dates.txt", "gtfs", "calendar_dates"),
    ("trips.txt", "gtfs", "trips"),
    ("frequencies.txt", "gtfs", "frequencies"),
    ("shapes.txt", "gtfs", "shapes_points"),
    ("stop_times.txt", "gtfs", "stop_times"),
]

SPECIAL_COLUMN_MAP = {
    "calendar.txt": {
        "start_date": "start_date_text",
        "end_date": "end_date_text",
    },
    "calendar_dates.txt": {
        "date": "date_text",
    },
}

TRUNCATE_SQL = """
TRUNCATE TABLE
  gtfs.stop_times,
  gtfs.frequencies,
  gtfs.trips,
  gtfs.routes,
  gtfs.stops,
  gtfs.shapes_points,
  gtfs.calendar_dates,
  gtfs.calendar,
  gtfs.agency
CASCADE
"""


def norm(text):
    return (text or "").strip().lstrip("\ufeff").lower()


def load_env_file(path):
    out = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if key:
            out[key] = value
    return out


def run(cmd, stdin=None):
    return subprocess.run(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def psql(sql, db_user, db_name, db_service, stdin_file=None, require_output=False):
    cmd = [
        "docker",
        "exec",
        "-i",
        db_service,
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        db_user,
        "-d",
        db_name,
    ]

    if stdin_file is None:
        cmd.extend(["-tAc", sql])
        proc = run(cmd)
    else:
        cmd.extend(["-c", sql])
        proc = run(cmd, stdin=stdin_file)

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace") or proc.stdout.decode("utf-8", errors="replace"))

    out = proc.stdout.decode("utf-8", errors="replace")
    if require_output and not out.strip():
        raise RuntimeError("Expected SQL output but got empty result")
    return out


def get_insertable_columns(db_user, db_name, db_service, schema, table):
    sql = (
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
        "AND COALESCE(is_generated, 'NEVER') = 'NEVER' "
        "AND is_identity = 'NO' "
        "ORDER BY ordinal_position;"
    )
    out = psql(sql, db_user, db_name, db_service, require_output=True)
    return {line.strip().lower() for line in out.splitlines() if line.strip()}


def main():
    gtfs_dir = Path(os.environ.get("GTFS_DIR", "")).expanduser()
    app_dir = Path(os.environ.get("APP_DIR", "")).expanduser()
    db_service = (os.environ.get("DB_SERVICE") or "postgres").strip() or "postgres"

    if not gtfs_dir.exists() or not gtfs_dir.is_dir():
        print(f"GTFS dir not found: {gtfs_dir}", file=sys.stderr)
        return 2

    env_map = {}
    env_file = app_dir / ".env"
    if env_file.exists():
        env_map.update(load_env_file(env_file))

    db_user = (os.environ.get("PGUSER") or env_map.get("PGUSER") or "").strip()
    db_name = (os.environ.get("PGDATABASE") or env_map.get("PGDATABASE") or "").strip()

    if not db_user or not db_name:
        print("Missing PGUSER or PGDATABASE (expected in APP_DIR/.env or env)", file=sys.stderr)
        return 2

    print(f"Using GTFS dir: {gtfs_dir}")
    print(f"Using DB service/user/db: {db_service}/{db_user}/{db_name}")

    try:
        psql(TRUNCATE_SQL, db_user, db_name, db_service)
        print("Truncated GTFS core tables")

        for file_name, schema, table in TABLES:
            csv_path = gtfs_dir / file_name
            if not csv_path.exists() or not csv_path.is_file():
                print(f"SKIP {file_name}: file not found")
                continue

            with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                reader = csv.DictReader(f)
                raw_headers = list(reader.fieldnames or [])
                headers = [norm(h) for h in raw_headers]
                if not headers:
                    print(f"SKIP {file_name}: empty header")
                    continue

                special = {norm(k): norm(v) for k, v in (SPECIAL_COLUMN_MAP.get(file_name) or {}).items()}
                insertable = get_insertable_columns(db_user, db_name, db_service, schema, table)
                cols = []
                source_for_col = {}
                for src_col in headers:
                    preferred = special.get(src_col, src_col)
                    candidate = preferred if preferred in insertable else src_col
                    if candidate not in insertable:
                        continue
                    if candidate in source_for_col:
                        continue
                    cols.append(candidate)
                    source_for_col[candidate] = src_col
                if not cols:
                    print(f"SKIP {file_name}: no matching insertable columns for {schema}.{table}")
                    continue

                row_count = 0
                tmp_name = None
                try:
                    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", delete=False) as tmp:
                        tmp_name = tmp.name
                        writer = csv.writer(tmp)
                        writer.writerow(cols)

                        for row in reader:
                            normalized = {norm(k): ("" if v is None else str(v)) for k, v in row.items()}
                            values = []
                            for db_col in cols:
                                src_col = source_for_col.get(db_col, db_col)
                                values.append((normalized.get(src_col) or "").strip())
                            writer.writerow(values)
                            row_count += 1
                    copy_sql = (
                        f"\\copy {schema}.{table} ({', '.join(cols)}) "
                        "FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')"
                    )
                    with open(tmp_name, "rb") as src:
                        psql(copy_sql, db_user, db_name, db_service, stdin_file=src)
                finally:
                    if tmp_name:
                        try:
                            Path(tmp_name).unlink(missing_ok=True)
                        except Exception:
                            pass

                print(f"Loaded {row_count} rows from {file_name} -> {schema}.{table}")

    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("AWS GTFS DB import completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _split_sql(sql_text: str) -> list[str]:
    statements = [chunk.strip() for chunk in sql_text.split(";")]
    return [s for s in statements if s]


def _connect_status(dsn: str) -> dict[str, Any]:
    conn = psycopg2.connect(dsn, connect_timeout=8)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                  current_database()::text AS db_name,
                  current_user::text AS db_user,
                  now()::text AS checked_at
                """
            )
            row = dict(cur.fetchone() or {})
        return {
            "ok": True,
            "db_name": row.get("db_name"),
            "db_user": row.get("db_user"),
            "checked_at": row.get("checked_at"),
        }
    finally:
        conn.close()


def _aws_transportapp_dirs(cfg: OpsConfig) -> list[str]:
    candidates: list[str] = []
    if cfg.aws_transportapp_dir:
        candidates.append(cfg.aws_transportapp_dir.rstrip("/"))
    candidates.extend(
        [
            "/opt/gtfs_app",
            "/opt/gtfs_app",
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _run_aws_docker_psql(
    cfg: OpsConfig,
    sql: str,
    *,
    timeout: int = 180,
    require_output: bool = False,
) -> tuple[Optional[str], CommandResult]:
    last = CommandResult(False, 1, "", "No AWS gtfs_app directory found.", "")
    for app_dir in _aws_transportapp_dirs(cfg):
        cmd = (
            f"if [ -f {shlex.quote(app_dir + '/.env')} ]; then "
            f"cd {shlex.quote(app_dir)} && "
            "source .env >/dev/null 2>&1 && "
            f"docker exec postgres psql -v ON_ERROR_STOP=1 -U \"$PGUSER\" -d \"$PGDATABASE\" -tAc {shlex.quote(sql)}; "
            "fi"
        )
        res = run_ssh(cfg, cmd, timeout=timeout)
        last = res
        if res.ok and ((not require_output) or (res.stdout or "").strip()):
            return app_dir, res
    return None, last


def _detect_aws_transportapp_dir(cfg: OpsConfig) -> Optional[str]:
    for app_dir in _aws_transportapp_dirs(cfg):
        check = run_ssh(
            cfg,
            (
                f"if [ -f {shlex.quote(app_dir + '/.env')} ]; then "
                f"printf '%s' {shlex.quote(app_dir)}; "
                "fi"
            ),
            timeout=60,
        )
        if check.ok and (check.stdout or "").strip():
            return app_dir
    return None


def _get_insertable_columns(cur: Any, schema: str, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
          AND COALESCE(is_generated, 'NEVER') = 'NEVER'
          AND is_identity = 'NO'
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return {str(row[0]).strip().lower() for row in cur.fetchall()}


def _normalize_header(name: str) -> str:
    return (name or "").strip().lstrip("\ufeff").lower()


def _run_builtin_local_gtfs_import(cfg: OpsConfig, gtfs_extract_dir: Path) -> CommandResult:
    if not cfg.local_db_dsn:
        return CommandResult(False, 1, "", "Missing LOCAL_GTFS_DB_DSN or LOCAL_DB_DSN", "builtin_gtfs_import")
    if not gtfs_extract_dir.exists():
        return CommandResult(False, 1, "", f"GTFS extract dir not found: {gtfs_extract_dir}", "builtin_gtfs_import")

    conn = None
    logs: list[str] = []
    try:
        conn = psycopg2.connect(cfg.local_db_dsn, connect_timeout=10)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(GTFS_TRUNCATE_SQL)
            logs.append("Truncated GTFS core tables (CASCADE).")

            for file_name, schema, table in GTFS_IMPORT_TABLES:
                file_path = gtfs_extract_dir / file_name
                if not file_path.exists() or not file_path.is_file():
                    logs.append(f"SKIP {file_name}: file not found")
                    continue

                with file_path.open("r", encoding="utf-8", errors="replace", newline="") as f:
                    reader = csv.DictReader(f)
                    raw_headers = list(reader.fieldnames or [])
                    headers = [_normalize_header(h) for h in raw_headers]
                    if not headers:
                        logs.append(f"SKIP {file_name}: empty header")
                        continue

                    special_map = {
                        _normalize_header(k): _normalize_header(v)
                        for k, v in (GTFS_SPECIAL_COLUMN_MAP.get(file_name) or {}).items()
                    }
                    insertable_cols = _get_insertable_columns(cur, schema, table)
                    cols: list[str] = []
                    source_for_col: dict[str, str] = {}
                    for src_col in headers:
                        preferred = special_map.get(src_col, src_col)
                        candidate = preferred if preferred in insertable_cols else src_col
                        if candidate not in insertable_cols:
                            continue
                        if candidate in source_for_col:
                            continue
                        cols.append(candidate)
                        source_for_col[candidate] = src_col
                    if not cols:
                        logs.append(f"SKIP {file_name}: no matching DB columns in {schema}.{table}")
                        continue

                    sql = (
                        f"INSERT INTO {schema}.{table} ({', '.join(cols)}) VALUES %s"
                    )
                    batch: list[tuple[Any, ...]] = []
                    inserted = 0

                    for row in reader:
                        normalized = {_normalize_header(k): v for k, v in row.items()}
                        values = []
                        for db_col in cols:
                            csv_col = source_for_col.get(db_col, db_col)
                            values.append((normalized.get(csv_col) or "").strip() or None)
                        batch.append(tuple(values))
                        if len(batch) >= 2000:
                            psycopg2.extras.execute_values(cur, sql, batch, page_size=500)
                            inserted += len(batch)
                            batch = []
                    if batch:
                        psycopg2.extras.execute_values(cur, sql, batch, page_size=500)
                        inserted += len(batch)

                    logs.append(f"Loaded {inserted} rows from {file_name} -> {schema}.{table}")

        conn.commit()
        return CommandResult(True, 0, "\n".join(logs), "", "builtin_gtfs_import")
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return CommandResult(False, 1, "\n".join(logs), str(exc), "builtin_gtfs_import")
    finally:
        if conn is not None:
            conn.close()


def check_local_db_status(cfg: OpsConfig) -> dict[str, Any]:
    if not cfg.local_db_dsn:
        return {"ok": False, "error": "Missing LOCAL_GTFS_DB_DSN or LOCAL_DB_DSN"}
    try:
        return _connect_status(cfg.local_db_dsn)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def check_aws_db_status(cfg: OpsConfig) -> dict[str, Any]:
    if cfg.aws_db_dsn:
        try:
            out = _connect_status(cfg.aws_db_dsn)
            out["mode"] = "direct_dsn"
            return out
        except Exception as exc:
            return {"ok": False, "mode": "direct_dsn", "error": str(exc)}

    if cfg.aws_db_status_cmd:
        res = run_ssh(cfg, cfg.aws_db_status_cmd, timeout=180)
        return {
            "ok": res.ok,
            "mode": "ssh_command",
            "command": cfg.aws_db_status_cmd,
            "stdout": (res.stdout or "").strip(),
            "stderr": (res.stderr or "").strip(),
            "returncode": res.returncode,
        }

    app_dir, auto = _run_aws_docker_psql(
        cfg,
        "SELECT current_database()::text || ',' || current_user::text;",
        timeout=180,
        require_output=True,
    )
    if auto.ok and (auto.stdout or "").strip():
        raw = (auto.stdout or "").strip().splitlines()[-1].strip()
        db_name, db_user = (raw.split(",", 1) + [""])[:2]
        return {
            "ok": True,
            "mode": "ssh_auto_docker_psql",
            "app_dir": app_dir,
            "db_name": db_name.strip(),
            "db_user": db_user.strip(),
            "stdout": raw,
        }

    return {
        "ok": False,
        "error": "Missing AWS_DB_DSN or AWS_DB_STATUS_CMD; auto docker psql check failed",
        "stderr": (auto.stderr or "").strip(),
        "mode": "unconfigured",
    }


def run_local_db_import(cfg: OpsConfig, gtfs_zip_path: Path) -> CommandResult:
    if not cfg.local_db_import_cmd:
        extract_dir = cfg.local_gtfs_extract_dir
        if extract_dir is not None:
            return _run_builtin_local_gtfs_import(cfg, extract_dir)
        return CommandResult(
            ok=False,
            returncode=1,
            stdout="",
            stderr="LOCAL_GTFS_IMPORT_CMD is not configured and no LOCAL_GTFS_EXTRACT_DIR is available.",
            command="",
        )

    env = os.environ.copy()
    env["GTFS_ZIP_PATH"] = str(gtfs_zip_path)
    env["GTFS_EXTRACT_DIR"] = str(cfg.local_gtfs_extract_dir or "")
    env["GTFS_APP_ROOT"] = str(cfg.transportapp_root or "")

    cwd = cfg.transportapp_root or cfg.repo_root
    return run_local_shell(cfg.local_db_import_cmd, cwd=cwd, timeout=1800, env=env)


def run_local_db_refresh(cfg: OpsConfig) -> CommandResult:
    if not cfg.local_db_dsn:
        return CommandResult(False, 1, "", "Missing LOCAL_GTFS_DB_DSN or LOCAL_DB_DSN", "")

    sql_text = (cfg.local_db_refresh_sql or "").strip()
    if not sql_text:
        return CommandResult(True, 0, "No LOCAL_DB_REFRESH_SQL configured; skipped.", "", "")

    statements = _split_sql(sql_text)
    if not statements:
        return CommandResult(True, 0, "LOCAL_DB_REFRESH_SQL has no executable statements; skipped.", "", "")

    conn = None
    executed: list[str] = []
    try:
        conn = psycopg2.connect(cfg.local_db_dsn, connect_timeout=10)
        conn.autocommit = False
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
                executed.append(stmt)
        conn.commit()
    except Exception as exc:
        if conn is not None:
            conn.rollback()
        return CommandResult(False, 1, "\n".join(executed), str(exc), "LOCAL_DB_REFRESH_SQL")
    finally:
        if conn is not None:
            conn.close()

    return CommandResult(True, 0, "\n".join(executed), "", "LOCAL_DB_REFRESH_SQL")


def run_aws_db_import(
    cfg: OpsConfig,
    *,
    gtfs_extract_dir: Optional[str],
    gtfs_remote_zip: Optional[str] = None,
) -> CommandResult:
    if cfg.aws_db_import_cmd:
        env_parts: list[str] = []
        if gtfs_extract_dir:
            env_parts.append(f"AWS_GTFS_EXTRACT_DIR={shlex.quote(gtfs_extract_dir)}")
        if gtfs_remote_zip:
            env_parts.append(f"GTFS_REMOTE_ZIP={shlex.quote(gtfs_remote_zip)}")
        env_parts.append(cfg.aws_db_import_cmd)
        return run_ssh(cfg, " ".join(env_parts), timeout=5400)

    effective_extract_dir = (gtfs_extract_dir or cfg.aws_gtfs_extract_dir or "").strip()
    if not effective_extract_dir:
        return CommandResult(False, 1, "", "Missing AWS_GTFS_EXTRACT_DIR for AWS DB import", "")

    app_dir = _detect_aws_transportapp_dir(cfg) or (cfg.aws_transportapp_dir or "/opt/gtfs_app")
    script_path = "/tmp/datamind_gtfs_db_import.py"

    remote_cmd = (
        "set -euo pipefail\n"
        "if ! command -v python3 >/dev/null 2>&1; then echo 'python3 not found on EC2 host' >&2; exit 2; fi\n"
        f"if [ ! -d {shlex.quote(effective_extract_dir)} ]; then printf '%s\\n' {shlex.quote('GTFS extract dir not found: ' + effective_extract_dir)} >&2; exit 2; fi\n"
        f"cat > {shlex.quote(script_path)} <<'PY'\n"
        f"{AWS_GTFS_DB_IMPORT_SCRIPT}\n"
        "PY\n"
        f"APP_DIR={shlex.quote(app_dir)} GTFS_DIR={shlex.quote(effective_extract_dir)} DB_SERVICE=postgres python3 {shlex.quote(script_path)}\n"
        f"rm -f {shlex.quote(script_path)}"
    )
    return run_ssh(cfg, remote_cmd, timeout=5400)


def run_aws_db_refresh(cfg: OpsConfig, *, sql_fallback: Optional[str] = None) -> CommandResult:
    if cfg.aws_db_refresh_cmd:
        return run_ssh(cfg, cfg.aws_db_refresh_cmd, timeout=1800)

    if cfg.aws_db_dsn:
        sql_text = (sql_fallback or cfg.local_db_refresh_sql or "").strip()
        if not sql_text:
            return CommandResult(True, 0, "No SQL configured for AWS refresh; skipped.", "", "")

        statements = _split_sql(sql_text)
        if not statements:
            return CommandResult(True, 0, "No executable SQL statements for AWS refresh; skipped.", "", "")

        conn = None
        executed: list[str] = []
        try:
            conn = psycopg2.connect(cfg.aws_db_dsn, connect_timeout=10)
            conn.autocommit = False
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
                    executed.append(stmt)
            conn.commit()
        except Exception as exc:
            if conn is not None:
                conn.rollback()
            return CommandResult(False, 1, "\n".join(executed), str(exc), "AWS_DB_DSN SQL")
        finally:
            if conn is not None:
                conn.close()

        return CommandResult(True, 0, "\n".join(executed), "", "AWS_DB_DSN SQL")

    sql_text = (sql_fallback or cfg.local_db_refresh_sql or "").strip()
    if sql_text:
        _, auto = _run_aws_docker_psql(cfg, sql_text, timeout=1800)
        if auto.ok:
            return CommandResult(True, 0, auto.stdout, auto.stderr, auto.command)

    return CommandResult(False, 1, "", "Missing AWS_DB_REFRESH_CMD or AWS_DB_DSN", "")
