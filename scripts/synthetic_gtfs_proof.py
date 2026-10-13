#!/usr/bin/env python3
"""DB-free synthetic consumer fixture + pinned MobilityData validation (no publication)."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phase5_gtfs.compiler.build_feed_info import build_feed_info
from phase5_gtfs.common.time_utils import seconds_to_hhmmss
from datamind_console.ops.gtfs_validate import validate_gtfs_zip
from datamind_console.ops import gtfs_canonical_validator as canonical

VERSION = "7.1.0"
JAR_SHA256 = "52c2785089aaf04e7ba1bb11b2db215692e2622eb0e196b823c194d156d9b58c"
JAR_URL = f"https://github.com/MobilityData/gtfs-validator/releases/download/v{VERSION}/gtfs-validator-{VERSION}-cli.jar"
PARSER_VERSION = "1.1.2"
LICENSE = """Synthetic DataMind GTFS consumer fixture — no real transit service.
All coordinates, names, routes and timetables are invented for software tests.
The generated fixture data is dedicated to the public domain under CC0 1.0:
https://creativecommons.org/publicdomain/zero/1.0/legalcode
This dedication applies only to this generated fixture, not repository code,
validator binaries, third-party libraries, OSM data or regional datasets.
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_tables(start: date) -> dict[str, list[dict]]:
    end = start + timedelta(days=364)
    tables = {
        "agency.txt": [dict(agency_id="synthetic", agency_name="Synthetic DataMind Test Transit",
                            agency_url="https://example.org", agency_timezone="America/Mexico_City", agency_lang="en")],
        "stops.txt": [dict(stop_id=f"S{i+1}", stop_name=f"Synthetic Stop {i+1}",
                           stop_lat=f"{19.430 + i * .005:.6f}", stop_lon="-99.140000", location_type=0)
                      for i in range(3)],
        "routes.txt": [dict(route_id="R1", agency_id="synthetic", route_short_name="S1",
                            route_long_name="Synthetic Test Corridor", route_type=3,
                            route_color="004488", route_text_color="FFFFFF")],
        "trips.txt": [], "stop_times.txt": [], "shapes.txt": [],
        "calendar.txt": [dict(service_id="daily", **{d: 1 for d in
                          ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")},
                          start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))],
        "calendar_dates.txt": [dict(service_id="daily", date=(start + timedelta(days=10)).strftime("%Y%m%d"), exception_type=2)],
    }
    for direction in (0, 1):
        stops = tables["stops.txt"][::1 if direction == 0 else -1]
        for seq, stop in enumerate(stops, 1):
            tables["shapes.txt"].append(dict(shape_id=f"shape_{direction}", shape_pt_lat=stop["stop_lat"],
                                           shape_pt_lon=stop["stop_lon"], shape_pt_sequence=seq,
                                           shape_dist_traveled=f"{(seq-1)*555.975:.3f}"))
        for label, departure in (("day", 8 * 3600 + direction * 1800), ("night", 23 * 3600 + 55 * 60 + direction * 1800)):
            trip = f"{label}_{direction}"
            tables["trips.txt"].append(dict(route_id="R1", service_id="daily", trip_id=trip,
                                          trip_headsign=stops[-1]["stop_name"], direction_id=direction,
                                          shape_id=f"shape_{direction}"))
            for seq, stop in enumerate(stops, 1):
                time = seconds_to_hhmmss(departure + (seq - 1) * 300)
                tables["stop_times.txt"].append(dict(trip_id=trip, arrival_time=time, departure_time=time,
                                                   stop_id=stop["stop_id"], stop_sequence=seq, timepoint=1,
                                                   shape_dist_traveled=f"{(seq-1)*555.975:.3f}"))
    return tables


def generate_feed(output: Path, start: date) -> Path:
    """Fixed ZIP metadata + stored bytes make the same date byte-reproducible."""
    output.mkdir(parents=True, exist_ok=True)
    tables = fixture_tables(start)
    with tempfile.TemporaryDirectory(prefix="synthetic-gtfs-") as tmp:
        directory = Path(tmp)
        for name, rows in tables.items():
            with (directory / name).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        build_feed_info(str(directory), dict(publisher_name="Synthetic DataMind Test Fixture (CC0)",
                                             publisher_url="https://example.org", feed_lang="en", default_lang="en",
                                             feed_start_date=start.strftime("%Y%m%d"),
                                             feed_end_date=(start + timedelta(days=364)).strftime("%Y%m%d"),
                                             feed_version=f"synthetic-v1-{start:%Y%m%d}",
                                             contact_email="", contact_url="https://example.org"))
        feed = output / "synthetic-gtfs.zip"
        with zipfile.ZipFile(feed, "w", compression=zipfile.ZIP_STORED) as archive:
            for path in sorted(directory.glob("*.txt")):
                entry = zipfile.ZipInfo(path.name, date_time=(2000, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, path.read_bytes())
    (output / "LICENSE.txt").write_text(LICENSE, encoding="utf-8")
    return feed


def validator_jar(cache: Path, download: bool) -> Path:
    jar = cache / f"gtfs-validator-{VERSION}-cli.jar"
    if not jar.is_file():
        if not download:
            raise RuntimeError("pinned validator missing; use --download-validator or populate --cache")
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            urllib.request.urlretrieve(JAR_URL, temporary)
            if sha256(temporary) != JAR_SHA256:
                raise RuntimeError("downloaded validator checksum mismatch")
            temporary.replace(jar)
        finally:
            temporary.unlink(missing_ok=True)
    if sha256(jar) != JAR_SHA256:
        raise RuntimeError("validator checksum mismatch")
    return jar.resolve()


def parser_smoke(feed: Path, start: date) -> dict:
    if importlib.metadata.version("partridge") != PARSER_VERSION:
        raise RuntimeError(f"Partridge must be {PARSER_VERSION}")
    import partridge
    parsed = partridge.load_feed(str(feed))
    expected = {"agency": 1, "stops": 3, "routes": 1, "trips": 4, "stop_times": 12, "shapes": 6, "calendar": 1, "calendar_dates": 1, "feed_info": 1}
    actual = {name: len(getattr(parsed, name)) for name in expected}
    if actual != expected:
        raise RuntimeError(f"parser row counts differ: {actual}")
    maximum = float(parsed.stop_times.departure_time.max())
    if maximum != 24 * 3600 + 35 * 60:
        raise RuntimeError("parser did not preserve after-midnight times")
    services = partridge.read_service_ids_by_date(str(feed))
    if "daily" not in services.get(start, set()) or services.get(start + timedelta(days=10), set()):
        raise RuntimeError("parser service calendar/exception mismatch")
    return dict(status="pass", name="partridge", version=PARSER_VERSION, rows=actual, max_departure_seconds=maximum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new directory (existing directories refused)")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 10, 13))
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache" / "gtfs-proof")
    parser.add_argument("--download-validator", action="store_true", help="explicit opt-in to pinned public release download")
    parser.add_argument("--parser-smoke", action="store_true", help="require pinned Partridge; failure fails the command")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        parser.error("output already exists; choose a new directory to avoid stale evidence")
    output.mkdir(parents=True)
    evidence = dict(status="error", synthetic=True, fixture_kind="DB-free consumer fixture; not a Phase 5 export",
                    license="CC0-1.0", timestamp_utc=datetime.now(timezone.utc).isoformat(),
                    invocation=[sys.executable, str(Path(__file__).resolve()), *(argv if argv is not None else sys.argv[1:])],
                    validator_release=VERSION, validator_jar_url=JAR_URL, validator_jar_sha256=JAR_SHA256,
                    service_start=args.start_date.isoformat())
    try:
        feed = generate_feed(output, args.start_date)
        evidence.update(feed_sha256=sha256(feed), structure=validate_gtfs_zip(feed))
        jar = validator_jar(args.cache, args.download_validator)
        java = canonical.resolve_java()
        if not java:
            raise RuntimeError("Java missing (requires Java 17+)")
        evidence["java_version"] = subprocess.run([java, "-version"], capture_output=True, text=True, check=True).stderr.strip()
        # Avoid implicit region/configured-jar selection. No .env or DB module is loaded.
        os.environ["GTFS_VALIDATOR_JAR"] = str(jar)
        os.environ.pop("GTFS_VALIDATOR_COUNTRY_CODE", None)
        report_dir = output / "validator"
        evidence["validator_command"] = canonical.build_command(java, jar, feed, report_dir)
        result = canonical.run_validator(feed, output_dir=report_dir)
        evidence["validator"] = dict(status=result.status, counts=result.counts, notices=result.notices,
                                     version=result.validator_version, returncode=result.returncode, stderr_tail=result.stderr_tail)
        if not result.ok or not evidence["structure"]["ok"] or result.validator_version != VERSION:
            raise RuntimeError("validation failed or validator version missing/mismatched; inspect raw reports")
        if args.parser_smoke:
            evidence["parser"] = parser_smoke(feed, args.start_date)
        else:
            evidence["parser"] = dict(status="not run")
        evidence["status"] = "pass"
    except Exception as exc:
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
    (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))
    return 0 if evidence["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
