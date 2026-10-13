"""Failure-safe assertion boundaries for real CLI consumer recipes."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import json
import subprocess
import sys

import pytest

from scripts.gtfs_consumers import run as proof
from scripts.synthetic_gtfs_proof import generate_feed


def metrics():
    return {**proof.EXPECTED_METRICS, 'max_departure_interval': '24:35:00', 'adjacent_distance_m': 553.48644774}


def test_numeric_db_results_reconcile():
    proof.check_metrics(metrics())
    string_values = {k:str(v) for k,v in metrics().items()}
    proof.check_metrics(string_values)


@pytest.mark.parametrize('key,value', [
    ('orphan_stop_times',1), ('orphan_negative_control',0), ('orphan_trips',1),
    ('exception_service_days',1), ('prior_service_on_exception_clock_date',0),
    ('after_midnight_expanded',0), ('max_departure_seconds',2100),
    ('max_departure_interval','00:35:00'), ('near_stops',0), ('far_stops',1),
    ('correct_shape_starts',0), ('adjacent_distance_m',0.005),
    ('adjacent_distance_m',float('inf')), ('service_days',365), ('routes',True),
])
def test_real_query_assertions_reject_semantic_failures(key,value):
    result=metrics(); result[key]=value
    with pytest.raises(ValueError):
        proof.check_metrics(result)


def test_missing_metrics_never_pass():
    with pytest.raises(ValueError):
        proof.check_metrics({})


def report():
    return dict(success=True,validator_version='v1.3.4',errors={},warnings={},
                details=dict(files=[dict(name=name+'.txt',rows=count) for name,count in proof.EXPECTED_ROWS.items()]))


@pytest.mark.parametrize('mutation', [
    {'success':False}, {'success':'true'}, {'validator_version':'other'},
    {'errors':None}, {'warnings':None}, {'errors':{'reference_error':1}},
    {'warnings':{'warning':1}}, {'details':{'files':[]}},
])
def test_bad_transitland_report_never_passes(mutation):
    r=report();r.update(mutation)
    with pytest.raises((ValueError,KeyError,TypeError)):
        proof.transitland_report(r)


def test_transitland_report_matches_source_rows():
    assert proof.transitland_report(report())['rows']==proof.EXPECTED_ROWS


def tables(tmp_path):
    return proof.read_feed(generate_feed(tmp_path/'source',date(2026,10,13)))


def test_copy_normalization_keeps_geographic_and_time_semantics(tmp_path):
    source=tables(tmp_path);copy=deepcopy(source)
    for row in copy['stops']:
        row['stop_lat']=str(float(row['stop_lat']))
        row['stop_lon']=str(float(row['stop_lon']))
    for row in copy['shapes']:
        row['shape_pt_sequence']=str((int(row['shape_pt_sequence'])-1)*3)
    transformed=proof.reconcile_roundtrip(source,copy)
    assert transformed['shape_sequences']['shape_0']==dict(source=[1,2,3],copied=[0,3,6])


@pytest.mark.parametrize('table,column,value', [
    ('stop_times','departure_time','00:35:00'), ('stop_times','stop_id','MISSING'),
    ('shapes','shape_pt_lat','20.43'), ('stops','stop_lon','99.14'),
    ('calendar_dates','exception_type','1'), ('trips','shape_id','shape_9'),
])
def test_copy_semantic_changes_fail(tmp_path,table,column,value):
    source=tables(tmp_path);copy=deepcopy(source);copy[table][0][column]=value
    with pytest.raises(ValueError):
        proof.reconcile_roundtrip(source,copy)


def test_copy_dropped_row_fails(tmp_path):
    source=tables(tmp_path);copy=deepcopy(source);copy['trips'].pop()
    with pytest.raises(ValueError):
        proof.reconcile_roundtrip(source,copy)


def test_existing_connection_environment_is_not_inherited(monkeypatch):
    for key in ('PGHOST','PGPASSFILE','PGDATABASE','PSQLRC','TL_DATABASE_URL','DB_DSN','DATABASE_URL','SUPABASE_DB_PASSWORD','PRIVATE_TOKEN'):
        monkeypatch.setenv(key,'must-not-inherit')
    result=proof.clean_env()
    assert not any(value=='must-not-inherit' for value in result.values())


def test_wrong_input_fails_before_any_database_or_cli(tmp_path,monkeypatch):
    feed=tmp_path/'bad.zip';feed.write_bytes(b'wrong input')
    monkeypatch.setattr(proof.Run,'command',lambda *args,**kwargs: pytest.fail('must not execute'))
    out=tmp_path/'out'
    assert proof.main(['--feed',str(feed),'--output',str(out)])==1
    result=json.loads((out/'evidence.json').read_text())
    assert result['status']=='error' and result['commands']==[]


def test_existing_output_refused(tmp_path):
    sentinel=tmp_path/'sentinel';sentinel.write_text('keep')
    with pytest.raises(SystemExit):
        proof.main(['--feed','absent.zip','--output',str(tmp_path)])
    assert sentinel.read_text()=='keep'


def test_failed_command_recorded_and_raises(tmp_path):
    run=proof.Run(tmp_path)
    with pytest.raises(RuntimeError,match='exit 7'):
        run.command([sys.executable,'-c','raise SystemExit(7)'])
    assert run.commands[0]['returncode']==7


def test_timeout_never_passes(tmp_path,monkeypatch):
    def timed_out(*args,**kwargs):
        raise subprocess.TimeoutExpired('consumer',1)
    monkeypatch.setattr(proof.subprocess,'run',timed_out)
    run=proof.Run(tmp_path)
    with pytest.raises(subprocess.TimeoutExpired):
        run.command(['consumer'])
    assert run.commands[0]['status']=='error'
