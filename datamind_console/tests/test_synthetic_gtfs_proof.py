"""Consumer fixture integrity, byte reproduction, and proof failure states."""
import csv
from datetime import date
import io
import json
import subprocess
import zipfile

import pytest

from scripts import synthetic_gtfs_proof as proof
from phase5_gtfs.common.time_utils import hhmmss_to_seconds

START = date(2026, 10, 13)


def test_fixture_references_dates_order_and_midnight(tmp_path):
    feed = proof.generate_feed(tmp_path / "a", START)
    second = proof.generate_feed(tmp_path / "b", START)
    assert feed.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(feed) as archive:
        tables = {name: list(csv.DictReader(io.StringIO(archive.read(name).decode()))) for name in archive.namelist()}
    stops = {row["stop_id"]: row for row in tables["stops.txt"]}
    routes = {row["route_id"] for row in tables["routes.txt"]}
    services = {row["service_id"] for row in tables["calendar.txt"]}
    trips = {row["trip_id"]: row for row in tables["trips.txt"]}
    shapes = {row["shape_id"] for row in tables["shapes.txt"]}
    assert len(trips) == 4 and len(stops) == 3
    calendar = tables["calendar.txt"][0]
    assert calendar["start_date"] == "20261013" and calendar["end_date"] == "20271012"
    info = tables["feed_info.txt"][0]
    assert info["feed_start_date"] == calendar["start_date"] and info["feed_end_date"] == calendar["end_date"]
    for exception in tables["calendar_dates.txt"]:
        assert exception["service_id"] in services
        assert calendar["start_date"] <= exception["date"] <= calendar["end_date"]
        assert exception["exception_type"] == "2"
    assert {r["trip_id"] for r in tables["stop_times.txt"]} == set(trips)
    for trip_id, trip in trips.items():
        assert trip["route_id"] in routes and trip["service_id"] in services and trip["shape_id"] in shapes
        times = [r for r in tables["stop_times.txt"] if r["trip_id"] == trip_id]
        shape = [r for r in tables["shapes.txt"] if r["shape_id"] == trip["shape_id"]]
        assert [int(r["stop_sequence"]) for r in times] == [1, 2, 3]
        assert [int(r["shape_pt_sequence"]) for r in shape] == [1, 2, 3]
        assert [r["stop_id"] for r in times] == (["S1", "S2", "S3"] if trip["direction_id"] == "0" else ["S3", "S2", "S1"])
        departures = [hhmmss_to_seconds(r["departure_time"]) for r in times]
        assert departures == sorted(departures) and all(b - a == 300 for a, b in zip(departures, departures[1:]))
        for row, point in zip(times, shape):
            stop = stops[row["stop_id"]]
            assert stop["stop_lat"] == point["shape_pt_lat"] and stop["stop_lon"] == point["shape_pt_lon"]
            assert row["shape_dist_traveled"] == point["shape_dist_traveled"]
            assert hhmmss_to_seconds(row["arrival_time"]) <= hhmmss_to_seconds(row["departure_time"])
        distances = [float(r["shape_dist_traveled"]) for r in shape]
        assert distances[0] == 0 and all(b > a for a, b in zip(distances, distances[1:]))
    assert max(hhmmss_to_seconds(r["departure_time"]) for r in tables["stop_times.txt"]) == 88500
    assert "CC0" in (feed.parent / "LICENSE.txt").read_text()


def test_missing_validator_records_failure(tmp_path):
    output = tmp_path / "proof"
    assert proof.main(["--output", str(output), "--cache", str(tmp_path / "missing")]) == 1
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["status"] == "error" and "missing" in evidence["failure"]
    assert evidence["feed_sha256"]


def test_wrong_jar_checksum_fails(tmp_path):
    (tmp_path / f"gtfs-validator-{proof.VERSION}-cli.jar").write_bytes(b"wrong")
    with pytest.raises(RuntimeError, match="checksum"):
        proof.validator_jar(tmp_path, False)


def test_existing_output_refused(tmp_path):
    sentinel = tmp_path / "evidence.json"
    sentinel.write_text("previous proof")
    with pytest.raises(SystemExit):
        proof.main(["--output", str(tmp_path)])
    assert sentinel.read_text() == "previous proof"


def test_requested_parser_failure_fails_proof(tmp_path, monkeypatch):
    from datamind_console.ops.gtfs_canonical_validator import ValidatorResult
    monkeypatch.setattr(proof.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stderr="test Java"))
    monkeypatch.setattr(proof, "validator_jar", lambda *args: tmp_path / "jar")
    monkeypatch.setattr(proof.canonical, "resolve_java", lambda: "java")
    monkeypatch.setattr(proof.canonical, "run_validator", lambda *args, **kwargs: ValidatorResult(ok=True, status="pass", validator_version=proof.VERSION))
    monkeypatch.setattr(proof, "parser_smoke", lambda *args: (_ for _ in ()).throw(RuntimeError("parser missing")))
    output = tmp_path / "out"
    assert proof.main(["--output", str(output), "--parser-smoke"]) == 1
    assert "parser missing" in json.loads((output / "evidence.json").read_text())["failure"]
