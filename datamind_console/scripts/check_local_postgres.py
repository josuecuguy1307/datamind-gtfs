from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DATA_DIRS = [
    Path('/opt/homebrew/var/postgresql@17'),
    Path('/opt/homebrew/var/postgresql@16'),
    Path('/opt/homebrew/var/postgresql@15'),
]


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
        txt = (out.stdout or '') + (out.stderr or '')
        return out.returncode, txt.strip()
    except Exception as e:
        return 1, str(e)


def _brew_services() -> Dict[str, Any]:
    rc, txt = _run(['brew', 'services', 'list'])
    if rc != 0:
        return {'ok': False, 'error': txt}
    lines = [ln.rstrip('\n') for ln in txt.splitlines() if ln.strip()]
    out = []
    for ln in lines[1:]:
        parts = ln.split()
        if not parts:
            continue
        name = parts[0]
        if not name.lower().startswith('postgresql'):
            continue
        status = parts[1] if len(parts) > 1 else ''
        out.append({'name': name, 'status': status, 'raw': ln})
    return {'ok': True, 'services': out}


def _pid_file_diag(data_dir: Path) -> Dict[str, Any]:
    pid_file = data_dir / 'postmaster.pid'
    out: Dict[str, Any] = {
        'data_dir': str(data_dir),
        'pid_file_exists': pid_file.exists(),
        'stale_lock_suspected': False,
        'pid': None,
        'pid_command': None,
    }
    if not pid_file.exists():
        return out
    try:
        first = pid_file.read_text(encoding='utf-8').splitlines()[0].strip()
        pid = int(first)
        out['pid'] = pid
    except Exception as e:
        out['parse_error'] = str(e)
        return out

    rc, cmd_txt = _run(['ps', '-p', str(out['pid']), '-o', 'command='])
    if rc != 0 or not cmd_txt.strip():
        out['stale_lock_suspected'] = True
        out['pid_command'] = None
        return out
    out['pid_command'] = cmd_txt.strip()
    out['stale_lock_suspected'] = ('postgres' not in cmd_txt.lower())
    return out


def _db_probe() -> Dict[str, Any]:
    try:
        from datamind_console.db.db import db_conn

        with db_conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      current_database() AS db,
                      COALESCE(inet_server_addr()::text, 'local_socket') AS host,
                      inet_server_port() AS port
                    """
                )
                row = cur.fetchone() or ()
        # tuple cursor
        if isinstance(row, tuple):
            db, host, port = row[0], row[1], row[2]
        else:
            db, host, port = row.get('db'), row.get('host'), row.get('port')
        return {'ok': True, 'db': db, 'host': host, 'port': port}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def main() -> int:
    parser = argparse.ArgumentParser(description='Local Postgres health diagnostics for DataMind/AI Bot.')
    parser.add_argument('--json', action='store_true', help='Print JSON only.')
    args = parser.parse_args()

    brew = _brew_services()
    pid_diags = [
        _pid_file_diag(d)
        for d in DATA_DIRS
        if d.exists()
    ]
    db = _db_probe()

    stale = [d for d in pid_diags if d.get('stale_lock_suspected')]
    status = 'ok' if db.get('ok') and not stale else ('warn' if db.get('ok') else 'fail')

    out = {
        'status': status,
        'db_probe': db,
        'brew_services': brew,
        'pid_file_diagnostics': pid_diags,
        'recommendation': (
            'If stale lock suspected and service is stopped: stop service, remove postmaster.pid, start service.'
            if stale else 'No stale lock detected.'
        ),
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=True, indent=2))
    else:
        print('POSTGRES_HEALTH', json.dumps(out, ensure_ascii=True))
    return 0 if db.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
