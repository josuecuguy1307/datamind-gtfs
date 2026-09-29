#!/usr/bin/env python3
"""Validate a GTFS feed directory for internal consistency.

Checks:
  1. Required files present
  2. shapes.txt: monotonic dist, no jumps > 1km
  3. stop_times.txt: monotonic sequence, referential integrity
  4. stops.txt: valid coords, no duplicate IDs
  5. Referential integrity across files
  6. Shape-stop proximity (every stop within 150m of shape)
"""
import csv
import math
import sys
from pathlib import Path
from collections import defaultdict

GTFS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parents[4]
    / "constructor_artifacts" / "valle_v2_CONSTRUCTED_39" / "gtfs_export"
)

REQUIRED_FILES = ["agency.txt", "routes.txt", "trips.txt", "stops.txt", "stop_times.txt", "calendar.txt"]
ECUADOR_LAT = (-1.5, 0.5)
ECUADOR_LON = (-79.5, -77.5)
SHAPE_JUMP_THRESHOLD_M = 1000
STOP_SHAPE_PROXIMITY_M = 150
STOP_SHAPE_WARNING_M = 100


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def read_csv(path):
    if not path.exists():
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def check_required_files(gtfs_dir):
    issues = []
    for fname in REQUIRED_FILES:
        if not (gtfs_dir / fname).exists():
            issues.append(f"MISSING required file: {fname}")
    return issues


def check_shapes(gtfs_dir):
    issues = []
    rows = read_csv(gtfs_dir / "shapes.txt")
    if not rows:
        return ["shapes.txt: empty or missing"]

    by_shape = defaultdict(list)
    for r in rows:
        by_shape[r["shape_id"]].append(r)

    for shape_id, pts in by_shape.items():
        pts.sort(key=lambda x: int(x["shape_pt_sequence"]))

        # Monotonic dist
        prev_dist = -1.0
        for p in pts:
            d = float(p.get("shape_dist_traveled", 0))
            if d < prev_dist:
                issues.append(f"shapes.txt: shape {shape_id} non-monotonic dist at seq {p['shape_pt_sequence']}")
                break
            prev_dist = d

        # No jumps
        for i in range(1, len(pts)):
            lat1, lon1 = float(pts[i-1]["shape_pt_lat"]), float(pts[i-1]["shape_pt_lon"])
            lat2, lon2 = float(pts[i]["shape_pt_lat"]), float(pts[i]["shape_pt_lon"])
            gap = haversine_m(lat1, lon1, lat2, lon2)
            if gap > SHAPE_JUMP_THRESHOLD_M:
                issues.append(f"shapes.txt: shape {shape_id} jump {gap:.0f}m at seq {pts[i]['shape_pt_sequence']}")

        # Duplicate consecutive
        for i in range(1, len(pts)):
            if (pts[i]["shape_pt_lat"] == pts[i-1]["shape_pt_lat"]
                    and pts[i]["shape_pt_lon"] == pts[i-1]["shape_pt_lon"]):
                issues.append(f"shapes.txt: shape {shape_id} duplicate point at seq {pts[i]['shape_pt_sequence']}")

    return issues


def check_stop_times(gtfs_dir):
    issues = []
    rows = read_csv(gtfs_dir / "stop_times.txt")
    if not rows:
        return ["stop_times.txt: empty or missing"]

    stops = {r["stop_id"] for r in read_csv(gtfs_dir / "stops.txt")}
    trips = {r["trip_id"] for r in read_csv(gtfs_dir / "trips.txt")}

    by_trip = defaultdict(list)
    for r in rows:
        by_trip[r["trip_id"]].append(r)

    for trip_id, st_rows in by_trip.items():
        st_rows.sort(key=lambda x: int(x["stop_sequence"]))

        if len(st_rows) < 2:
            issues.append(f"stop_times.txt: trip {trip_id} has only {len(st_rows)} stops")

        prev_seq = -1
        for r in st_rows:
            seq = int(r["stop_sequence"])
            if seq <= prev_seq:
                issues.append(f"stop_times.txt: trip {trip_id} non-monotonic seq at {seq}")
                break
            prev_seq = seq

            if r["stop_id"] not in stops:
                issues.append(f"stop_times.txt: trip {trip_id} references unknown stop_id {r['stop_id']}")

        if trip_id not in trips:
            issues.append(f"stop_times.txt: unknown trip_id {trip_id}")

    return issues


def check_stops(gtfs_dir):
    issues = []
    rows = read_csv(gtfs_dir / "stops.txt")
    if not rows:
        return ["stops.txt: empty or missing"]

    seen_ids = set()
    for r in rows:
        sid = r["stop_id"]
        if sid in seen_ids:
            issues.append(f"stops.txt: duplicate stop_id {sid}")
        seen_ids.add(sid)

        lat = float(r["stop_lat"])
        lon = float(r["stop_lon"])
        if not (ECUADOR_LAT[0] <= lat <= ECUADOR_LAT[1]):
            issues.append(f"stops.txt: stop {sid} lat {lat} outside Ecuador range")
        if not (ECUADOR_LON[0] <= lon <= ECUADOR_LON[1]):
            issues.append(f"stops.txt: stop {sid} lon {lon} outside Ecuador range")

    return issues


def check_referential_integrity(gtfs_dir):
    issues = []
    routes = {r["route_id"] for r in read_csv(gtfs_dir / "routes.txt")}
    calendar = {r["service_id"] for r in read_csv(gtfs_dir / "calendar.txt")}
    shapes_rows = read_csv(gtfs_dir / "shapes.txt")
    shape_ids = {r["shape_id"] for r in shapes_rows}
    trips = read_csv(gtfs_dir / "trips.txt")

    for t in trips:
        if t["route_id"] not in routes:
            issues.append(f"trips.txt: trip {t['trip_id']} references unknown route_id {t['route_id']}")
        if t["service_id"] not in calendar:
            issues.append(f"trips.txt: trip {t['trip_id']} references unknown service_id {t['service_id']}")
        if t.get("shape_id") and t["shape_id"] not in shape_ids:
            issues.append(f"trips.txt: trip {t['trip_id']} references unknown shape_id {t['shape_id']}")

    return issues


def check_stop_shape_proximity(gtfs_dir):
    issues = []
    warnings = []
    stops_data = {r["stop_id"]: (float(r["stop_lat"]), float(r["stop_lon"])) for r in read_csv(gtfs_dir / "stops.txt")}
    trips = read_csv(gtfs_dir / "trips.txt")
    stop_times = read_csv(gtfs_dir / "stop_times.txt")
    shapes_rows = read_csv(gtfs_dir / "shapes.txt")

    by_shape = defaultdict(list)
    for r in shapes_rows:
        by_shape[r["shape_id"]].append((float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))

    trip_shape = {t["trip_id"]: t.get("shape_id", "") for t in trips}
    by_trip = defaultdict(list)
    for r in stop_times:
        by_trip[r["trip_id"]].append(r["stop_id"])

    for trip_id, stop_ids in by_trip.items():
        shape_id = trip_shape.get(trip_id, "")
        shape_pts = by_shape.get(shape_id, [])
        if not shape_pts:
            continue

        for sid in stop_ids:
            if sid not in stops_data:
                continue
            slat, slon = stops_data[sid]
            min_dist = min(haversine_m(slat, slon, pt[0], pt[1]) for pt in shape_pts)
            if min_dist > STOP_SHAPE_PROXIMITY_M:
                issues.append(f"stop-shape: stop {sid} is {min_dist:.0f}m from shape {shape_id} (trip {trip_id})")
            elif min_dist > STOP_SHAPE_WARNING_M:
                warnings.append(f"stop-shape: stop {sid} is {min_dist:.0f}m from shape {shape_id} (trip {trip_id})")

    return issues, warnings


def main():
    gtfs_dir = GTFS_DIR
    print(f"Validating GTFS feed: {gtfs_dir}")
    print()

    all_issues = []
    all_warnings = []

    checks = [
        ("Required files", check_required_files),
        ("Shapes", check_shapes),
        ("Stop times", check_stop_times),
        ("Stops", check_stops),
        ("Referential integrity", check_referential_integrity),
    ]

    for name, fn in checks:
        issues = fn(gtfs_dir)
        status = "PASS" if not issues else f"FAIL ({len(issues)} issues)"
        print(f"  [{status:20s}] {name}")
        for i in issues[:5]:
            print(f"    - {i}")
        if len(issues) > 5:
            print(f"    ... and {len(issues) - 5} more")
        all_issues.extend(issues)

    # Shape-stop proximity (separate since it returns warnings too)
    prox_issues, prox_warnings = check_stop_shape_proximity(gtfs_dir)
    status = "PASS" if not prox_issues else f"FAIL ({len(prox_issues)} issues)"
    if prox_warnings and not prox_issues:
        status = f"WARN ({len(prox_warnings)} warnings)"
    print(f"  [{status:20s}] Stop-shape proximity")
    for i in prox_issues[:5]:
        print(f"    - {i}")
    for w in prox_warnings[:5]:
        print(f"    ~ {w}")
    all_issues.extend(prox_issues)
    all_warnings.extend(prox_warnings)

    print()
    if all_issues:
        print(f"RESULT: FAIL — {len(all_issues)} issues found")
    elif all_warnings:
        print(f"RESULT: PASS with {len(all_warnings)} warnings")
    else:
        print("RESULT: PASS — all checks passed")

    # Write report
    report_path = gtfs_dir / "validation_report.txt"
    with open(report_path, "w") as f:
        f.write(f"GTFS Validation Report\n{'='*40}\n\n")
        f.write(f"Feed: {gtfs_dir}\n\n")
        if all_issues:
            f.write(f"Issues ({len(all_issues)}):\n")
            for i in all_issues:
                f.write(f"  - {i}\n")
            f.write("\n")
        if all_warnings:
            f.write(f"Warnings ({len(all_warnings)}):\n")
            for w in all_warnings:
                f.write(f"  ~ {w}\n")
            f.write("\n")
        if not all_issues and not all_warnings:
            f.write("All checks passed.\n")
    print(f"Report: {report_path}")

    return 0 if not all_issues else 1


if __name__ == "__main__":
    sys.exit(main())
