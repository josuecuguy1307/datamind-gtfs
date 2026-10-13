#!/usr/bin/env python3
"""Three pinned consumers of the exact PR1 fixture; never connects to existing DBs."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FEED_SHA256 = '602b8192c4f91f5f064942e582db4bb6bbded89c83c1ddde49207ad1eeb6eea5'
TRANSITLAND_VERSION = 'v1.3.4'
TRANSITLAND_ASSETS = {
    ('Darwin', 'arm64'): ('transitland-macos-apple', 'c8da1716051b3a56bc475c75ee6fc0a5dbd385d7fe917ac278b2b7903d421c32'),
    ('Darwin', 'x86_64'): ('transitland-macos-intel', '0f1fa6acbe703ba87d4da4dcf6779de3405ef1add3abffd20be35c2b16ec161d'),
    ('Linux', 'x86_64'): ('transitland-linux', '1fe5aacc9e438404a43b31bfa36fdf8b22e6d747932fa6e43200627a444a528f'),
}
EXPECTED_ROWS = dict(agency=1, stops=3, routes=1, trips=4, stop_times=12, shapes=6, calendar=1, calendar_dates=1, feed_info=1)
EXPECTED_METRICS = dict(agency=1, stops=3, routes=1, trips=4, stop_times=12, shape_points=6,
                        shapes=2, calendar=1, calendar_dates=1, feed_info=1, service_days=364,
                        arrivals_departures=4368, connections=2912, orphan_stop_times=0,
                        orphan_negative_control=1, orphan_trips=0, orphan_routes=0,
                        exception_service_days=0, exception_departures=0,
                        next_service_departures=12, first_service_departures=12,
                        after_midnight_expanded=1, prior_service_on_exception_clock_date=5,
                        max_departure_seconds=88500, near_stops=1, far_stops=0,
                        correct_shape_starts=2, reversed_times=0)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_env() -> dict[str, str]:
    # Never inherit DSNs, libpq/psql configuration, Transitland registry config or secrets.
    return {k: v for k, v in os.environ.items() if not (
        k.startswith(('PG', 'TL_', 'DB_', 'GTFS_', 'SUPABASE_', 'DATABASE_', 'PSQL'))
        or any(word in k.upper() for word in ('PASSWORD', 'SECRET', 'TOKEN', 'DSN')))}


def check_metrics(metrics: dict) -> None:
    if not isinstance(metrics, dict):
        raise ValueError('missing metrics object')
    for key, expected in EXPECTED_METRICS.items():
        value = metrics.get(key)
        if isinstance(value, bool) or value is None or Decimal(str(value)) != expected:
            raise ValueError(f'{key}: expected {expected}, got {value!r}')
    if metrics.get('max_departure_interval') != '24:35:00':
        raise ValueError('GTFS time must retain 24:35:00, not wrap to 00:35')
    distance = Decimal(str(metrics.get('adjacent_distance_m')))
    if not 553 < distance < 554:
        raise ValueError(f'WGS84 adjacent distance should be ~553.486 m, got {distance}')


def read_feed(path: Path) -> dict[str, list[dict]]:
    with zipfile.ZipFile(path) as archive:
        if set(archive.namelist()) != {name + '.txt' for name in EXPECTED_ROWS}:
            raise ValueError('expected exactly nine GTFS tables')
        return {name: list(csv.DictReader(io.StringIO(archive.read(name + '.txt').decode('utf-8')))) for name in EXPECTED_ROWS}


def reconcile_roundtrip(original: dict, copied: dict) -> dict:
    numeric = {'stop_lat', 'stop_lon', 'shape_pt_lat', 'shape_pt_lon', 'shape_dist_traveled'}
    transforms = {}
    for table, expected in EXPECTED_ROWS.items():
        if len(copied.get(table, [])) != expected:
            raise ValueError(f'roundtrip dropped or added {table} rows')
        columns = list(original[table][0])
        if table == 'shapes':
            def ordered(rows):
                return sorted(rows, key=lambda r: (r['shape_id'], int(r['shape_pt_sequence'])))
            source_rows, copy_rows = ordered(original[table]), ordered(copied[table])
        else:
            def ordered(rows):
                return sorted(rows, key=lambda r: tuple(Decimal(r.get(c, '')) if c in numeric else r.get(c, '') for c in columns))
            source_rows, copy_rows = ordered(original[table]), ordered(copied[table])
        changed = set()
        for before, after in zip(source_rows, copy_rows):
            for column in columns:
                x, y = before[column], after.get(column, '')
                if x != y:
                    changed.add(column)
                if column == 'shape_pt_sequence':
                    continue  # permitted resequencing; point order checked above and below
                if column in numeric:
                    equal = Decimal(x) == Decimal(y)
                else:
                    equal = x == y
                if not equal:
                    raise ValueError(f'roundtrip changed semantic {table}.{column}: {x!r} -> {y!r}')
        if table == 'shapes':
            for shape in {row['shape_id'] for row in copy_rows}:
                sequences = [int(row['shape_pt_sequence']) for row in copy_rows if row['shape_id'] == shape]
                if sequences[0] < 0 or len(set(sequences)) != len(sequences):
                    raise ValueError('roundtrip shape sequence is invalid')
            transforms['shape_sequences'] = {
                shape: dict(source=[int(row['shape_pt_sequence']) for row in source_rows if row['shape_id']==shape],
                            copied=[int(row['shape_pt_sequence']) for row in copy_rows if row['shape_id']==shape])
                for shape in {row['shape_id'] for row in copy_rows}}
        if changed:
            transforms[table] = sorted(changed)
    return transforms


def transitland_report(report: dict) -> dict:
    if (not isinstance(report, dict) or report.get('success') is not True
            or report.get('validator_version') != TRANSITLAND_VERSION):
        raise ValueError('Transitland report missing, failed, or wrong version')
    if not isinstance(report.get('errors'), dict) or not isinstance(report.get('warnings'), dict):
        raise ValueError('Transitland report missing error/warning groups')
    if report['errors'] or report['warnings']:
        raise ValueError('Transitland notices require disposition; no silent pass')
    counts = {file['name'].removesuffix('.txt'): file['rows'] for file in report['details']['files']}
    if counts != EXPECTED_ROWS:
        raise ValueError(f'Transitland source row counts differ: {counts}')
    return dict(rows=counts, errors=report['errors'], warnings=report['warnings'], success=True)


class Run:
    def __init__(self, output: Path):
        self.output = output
        self.commands: list[dict] = []
        self.env = clean_env()

    def command(self, args, *, env=None, stdout_path=None, timeout=120, expected=0):
        args = [str(arg) for arg in args]
        index = len(self.commands) + 1
        record = dict(command=args, started_at_utc=datetime.now(timezone.utc).isoformat(), expected_returncode=expected)
        if env is not None:
            record['isolated_connection'] = {key: env[key] for key in ('PGHOST','PGPORT','PGUSER','PGDATABASE','PGCONNECT_TIMEOUT') if key in env}
        self.commands.append(record)
        try:
            proc = subprocess.run(args, env=env or self.env, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            record.update(status='error', failure=str(exc))
            raise
        out = stdout_path or self.output / f'command-{index:02d}.stdout.txt'
        err = self.output / f'command-{index:02d}.stderr.txt'
        out.write_text(proc.stdout, encoding='utf-8')
        err.write_text(proc.stderr, encoding='utf-8')
        record.update(returncode=proc.returncode, stdout=str(out), stderr=str(err))
        if proc.returncode != expected:
            raise RuntimeError(f'command {index} exit {proc.returncode}; expected {expected}: {proc.stderr[-1000:]}')
        return proc.stdout

    def postgres(self, files: list[Path], binary_dir: Path) -> dict:
        # initdb always initializes OUR new directory. No DSN or host option is accepted.
        data = self.output / 'pgdata'
        socket = Path(tempfile.mkdtemp(prefix='dm-gtfs-socket-', dir='/tmp'))
        socket.chmod(0o700)
        pg = {name: binary_dir.resolve() / name for name in ('initdb', 'pg_ctl', 'psql', 'postgres')}
        env = {**self.env, 'PGHOST': str(socket), 'PGPORT': '5432', 'PGUSER': 'proof', 'PGDATABASE': 'postgres', 'PGCONNECT_TIMEOUT': '5'}
        started = False
        try:
            version = self.command([pg['postgres'], '--version']).strip()
            if ' 17.' not in version:
                raise ValueError('recipe is pinned/tested for PostgreSQL 17; supply its bin directory')
            self.command([pg['initdb'], '-D', data, '-U', 'proof', '--auth=trust', '--no-locale', '-E', 'UTF8'], env=env)
            # TCP disabled. Private Unix socket directory accessible only to this OS user.
            self.command([pg['pg_ctl'], '-D', data, '-l', self.output/'postgres.log', '-o',
                          f"-k {socket} -c listen_addresses='' -c unix_socket_permissions=0700", '-w', 'start'], env=env)
            started = True
            sql = self.output/'import.sql'
            self.command([HERE/'node_modules/.bin/gtfs-to-sql', '--require-dependencies', '--', *files], stdout_path=sql)
            self.command([pg['psql'], '-X', '-v', 'ON_ERROR_STOP=1', '-f', sql], env=env)
            query = [pg['psql'], '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose']
            versions = self.command([*query, '-c', 'SELECT version(),postgis_full_version();'], env=env).strip()
            metrics = json.loads(self.command([*query, '-f', HERE/'postgres.sql'], env=env))
            check_metrics(metrics)
            # Real pinned-release defect: ST_AsText -> geometry discards SRID in upstream view.
            if metrics.get('shape_srid') != 0:
                raise ValueError('pinned importer SRID behavior changed; reassess workaround')
            repro = self.output/'shape-srid-repro.sql'
            repro.write_text("SELECT ST_Equals(ST_StartPoint(sh.shape),s.stop_loc::geometry)\nFROM shapes_aggregated sh JOIN stops s ON s.stop_id='S1' WHERE sh.shape_id='shape_0';\n")
            self.command([*query, '-f', repro], env=env, expected=3)
            srid_error = Path(self.commands[-1]['stderr']).read_text()
            if 'mixed SRID' not in srid_error:
                raise ValueError('unexpected shape repro failure')
            if metrics.get('stop_srid') != 4326:
                raise ValueError('imported stops lost source WGS84 SRID')
            # Candidate fix expression tested separately without changing the imported view.
            corrected = "SELECT json_build_object('source_srid',min(ST_SRID(shape_pt_loc::geometry)), 'fixed_start_matches', ST_Equals(ST_StartPoint(ST_MakeLine(shape_pt_loc::geometry ORDER BY shape_pt_sequence)), (SELECT stop_loc::geometry FROM stops WHERE stop_id='S1'))) FROM shapes WHERE shape_id='shape_0';"
            candidate = json.loads(self.command([*query, '-c', corrected], env=env))
            if candidate != dict(source_srid=4326, fixed_start_matches=True):
                raise ValueError('candidate shape SRID fix did not preserve source geometry')
            return dict(status='pass_with_known_upstream_defect', importer='gtfs-via-postgres', version='4.12.0',
                        engine=version, full_versions=versions, metrics=metrics, candidate_fix_control=candidate,
                        upstream_defect=dict(issue='https://github.com/public-transport/gtfs-via-postgres/issues/48',
                                             failing_sql=repro.read_text(), psql_returncode=3, error=srid_error),
                        workaround='Only shape joins restore WGS84 via ST_SetSRID; original view remains SRID 0.',
                        isolated=dict(tcp=False, socket=str(socket), inherited_dsn=False))
        finally:
            try:
                if started or (data/'postmaster.pid').exists():
                    self.command([pg['pg_ctl'], '-D', data, '-m', 'fast', '-w', 'stop'], env=env)
            finally:
                # Retain logs and imported SQL, remove only OUR disposable cluster after stopping.
                if not (data/'postmaster.pid').exists():
                    shutil.rmtree(data, ignore_errors=True)
                    shutil.rmtree(socket, ignore_errors=True)

    def duckdb(self, files: list[Path]) -> dict:
        db = self.output/'feed.duckdb'
        self.command([HERE/'node_modules/.bin/gtfs-to-duckdb', '--require-dependencies', '--', db, *files])
        result = json.loads(self.command(['node', HERE/'query_duckdb.cjs', db, HERE/'duckdb.sql']))
        check_metrics(result['metrics'])
        # The upstream campaign is checking a docs correction separately; record the released CLI behavior.
        bad = self.output/'unsupported-materialized.duckdb'
        self.command([HERE/'node_modules/.bin/gtfs-to-duckdb', '--stats-by-route-date', 'materialized-view',
                      '--require-dependencies', '--', bad, *files], expected=1)
        error = Path(self.commands[-1]['stderr']).read_text()
        if 'materialized' not in error:
            raise ValueError('unsupported materialized-view control failed for unexpected reason')
        return dict(status='pass', importer='gtfs-via-duckdb', version='5.0.1', **result,
                    unsupported_materialized_view=dict(returncode=1, stderr=error),
                    spatial_units='metres via spheroid after explicitly flipping stored lon/lat to lat/lon')

    def transitland(self, feed: Path, binary: Path) -> dict:
        asset, expected_hash = TRANSITLAND_ASSETS[(platform.system(), platform.machine())]
        if digest(binary) != expected_hash:
            raise ValueError('Transitland binary checksum mismatch')
        version = self.command([binary, 'version']).strip()
        if f'version: {TRANSITLAND_VERSION}' not in version:
            raise ValueError('Transitland version mismatch')
        report_path = self.output/'transitland-report.json'
        self.command([binary, 'validate', '--include-entities', '--out', report_path, feed])
        result = transitland_report(json.loads(report_path.read_text()))
        copy = self.output/'transitland-roundtrip.zip'
        self.command([binary, 'copy', feed, copy])
        transformations = reconcile_roundtrip(read_feed(feed), read_feed(copy))
        return dict(status='pass', version=TRANSITLAND_VERSION, full_version=version, binary_asset=asset,
                    binary_sha256=expected_hash, **result, roundtrip_original_fields='semantics preserved; numeric formatting and shape sequence identifiers normalized',
                    roundtrip_transformations=transformations,
                    roundtrip_sha256=digest(copy), roundtrip_scope='local derived artifact; not published')


def get_transitland(path: Path, download: bool) -> Path:
    asset, checksum = TRANSITLAND_ASSETS[(platform.system(), platform.machine())]
    if not path.exists():
        if not download:
            raise ValueError('Transitland missing; supply --transitland or use --download-transitland')
        path.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(f'https://github.com/interline-io/transitland-lib/releases/download/{TRANSITLAND_VERSION}/{asset}', path)
    if digest(path) != checksum:
        raise ValueError('Transitland binary checksum mismatch')
    path.chmod(0o755)
    return path.resolve()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--feed', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new directory, refuses stale evidence')
    parser.add_argument('--consumer', choices=('all', 'postgres', 'duckdb', 'transitland'), default='all')
    parser.add_argument('--postgres-bin', type=Path)
    parser.add_argument('--transitland', type=Path, default=ROOT/'.cache/consumers/transitland')
    parser.add_argument('--download-transitland', action='store_true')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        parser.error('output exists; choose a new directory')
    output.mkdir(parents=True)
    run = Run(output)
    evidence = dict(status='error', timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    invocation=[sys.executable, str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])],
                    feed_sha256=FEED_SHA256, claim='CC0 synthetic consumer fixture; not Phase 5 or real-city output',
                    commands=run.commands, consumers={})
    try:
        feed = args.feed.resolve()
        if digest(feed) != FEED_SHA256:
            raise ValueError('input must match exact PR1 synthetic ZIP SHA256')
        reference = ROOT/'docs/proof/synthetic-gtfs/evidence.json'
        validation = json.loads(reference.read_text())
        if (validation.get('feed_sha256') != FEED_SHA256 or validation.get('status') != 'pass'
                or validation.get('validator', {}).get('version') != '7.1.0'
                or validation['validator']['counts'] != dict(ERROR=0, WARNING=0, INFO=0)):
            raise ValueError('missing or inconsistent PR1 canonical evidence')
        evidence['canonical_reference'] = dict(path=str(reference), sha256=digest(reference), unchanged=True)
        evidence['node_version'] = run.command(['node','--version']).strip()
        lock = HERE/'package-lock.json'
        evidence['npm_lock_sha256'] = digest(lock)
        packages = json.loads(lock.read_text())['packages']
        evidence['npm_packages'] = {name: dict(version=packages['node_modules/'+name]['version'],
                                             integrity=packages['node_modules/'+name]['integrity'])
                                    for name in ('gtfs-via-postgres','gtfs-via-duckdb','@duckdb/node-api')}
        for name, expected in [('gtfs-via-postgres','4.12.0'), ('gtfs-via-duckdb','5.0.1'),('@duckdb/node-api','1.4.4-r.4')]:
            if json.loads((HERE/'node_modules'/name/'package.json').read_text())['version'] != expected:
                raise ValueError(f'installed {name} does not match pin; run npm ci --legacy-peer-deps')
        original = read_feed(feed)
        if {k:len(v) for k,v in original.items()} != EXPECTED_ROWS:
            raise ValueError('input row counts mismatch')
        files_dir = output/'gtfs'
        files_dir.mkdir()
        # Names checked against exact table set above; no untrusted ZIP paths are extracted.
        with zipfile.ZipFile(feed) as archive:
            for name in archive.namelist():
                (files_dir/name).write_bytes(archive.read(name))
        files = sorted(files_dir.glob('*.txt'))
        requested = ('postgres','duckdb','transitland') if args.consumer=='all' else (args.consumer,)
        for consumer in requested:
            try:
                if consumer=='postgres':
                    if not args.postgres_bin:
                        raise ValueError('--postgres-bin required; no existing DB connection is accepted')
                    result = run.postgres(files, args.postgres_bin)
                elif consumer=='duckdb':
                    result = run.duckdb(files)
                else:
                    result = run.transitland(feed,get_transitland(args.transitland,args.download_transitland))
                evidence['consumers'][consumer] = result
            except Exception as exc:
                evidence['consumers'][consumer] = dict(status='error', failure=f'{type(exc).__name__}: {exc}')
        if digest(feed) != FEED_SHA256:
            raise ValueError('input ZIP was modified by a consumer')
        if all(result['status'] in ('pass','pass_with_known_upstream_defect') for result in evidence['consumers'].values()):
            evidence['status']='pass_with_known_upstream_defect' if 'postgres' in requested else 'pass'
    except Exception as exc:
        evidence['failure'] = f'{type(exc).__name__}: {exc}'
    evidence['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    (output/'evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
    print(json.dumps(evidence,indent=2))
    return 0 if evidence['status'].startswith('pass') else 1


if __name__=='__main__':
    raise SystemExit(main())
