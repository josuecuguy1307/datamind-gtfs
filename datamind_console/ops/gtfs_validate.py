from __future__ import annotations

import codecs
import csv
import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import zipfile

REQUIRED_FILES = {
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
}

SERVICE_FILES = {"calendar.txt", "calendar_dates.txt"}

_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "stops.txt": {"stop_id", "stop_name", "stop_lat", "stop_lon"},
    "routes.txt": {"route_id", "route_type"},
    "trips.txt": {"trip_id", "route_id", "service_id"},
    "stop_times.txt": {"trip_id", "stop_id", "stop_sequence"},
}

_MAX_SHAPE_ERRORS_PER_FILE = 8


def compute_sha256(file_path: Path) -> str:
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _zip_entries_by_basename(zf: zipfile.ZipFile) -> tuple[dict[str, zipfile.ZipInfo], dict[str, list[str]]]:
    by_name: dict[str, zipfile.ZipInfo] = {}
    dupes: dict[str, list[str]] = {}

    for info in zf.infolist():
        if info.is_dir():
            continue
        base = Path(info.filename).name.strip().lower()
        if not base:
            continue
        existing = by_name.get(base)
        if existing is None:
            by_name[base] = info
            continue
        dupes.setdefault(base, [existing.filename]).append(info.filename)

    return by_name, dupes


def _decode_utf8_strict(raw: bytes, *, file_name: str, errors: list[str], warnings: list[str]) -> str | None:
    data = raw
    if data.startswith(codecs.BOM_UTF8):
        warnings.append(f"{file_name} contains UTF-8 BOM; accepted")
        data = data[len(codecs.BOM_UTF8) :]

    if b"\x00" in data:
        errors.append(f"{file_name} contains NUL bytes and appears corrupted")
        return None

    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        errors.append(
            f"{file_name} is not valid UTF-8 (byte {exc.start}, reason={exc.reason})"
        )
        return None


def _validate_csv_text(
    *,
    file_name: str,
    csv_text: str,
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "header": [],
        "row_count": 0,
        "column_count": 0,
    }

    reader = csv.reader(io.StringIO(csv_text, newline=""))

    try:
        raw_header = next(reader)
    except StopIteration:
        errors.append(f"{file_name} is empty")
        return result

    header = [str(col).strip() for col in raw_header]
    if not header or all(not col for col in header):
        errors.append(f"{file_name} has an empty CSV header")
        return result

    result["header"] = header
    result["column_count"] = len(header)

    if any(not col for col in header):
        warnings.append(f"{file_name} header contains empty column name(s)")

    seen: set[str] = set()
    dup_cols: list[str] = []
    for col in header:
        if not col:
            continue
        if col in seen and col not in dup_cols:
            dup_cols.append(col)
        seen.add(col)
    if dup_cols:
        errors.append(f"{file_name} has duplicate columns: {', '.join(sorted(dup_cols))}")

    required_cols = _REQUIRED_COLUMNS.get(file_name, set())
    if required_cols:
        missing_cols = sorted(required_cols.difference(set(header)))
        if missing_cols:
            errors.append(f"{file_name} missing required columns: {', '.join(missing_cols)}")

    col_index = {name: idx for idx, name in enumerate(header) if name}
    stops_lat_idx = col_index.get("stop_lat") if file_name == "stops.txt" else None
    stops_lon_idx = col_index.get("stop_lon") if file_name == "stops.txt" else None

    shape_errors = 0
    for line_no, row in enumerate(reader, start=2):
        if not row or all(not str(cell).strip() for cell in row):
            continue
        result["row_count"] = int(result["row_count"]) + 1

        if len(row) != len(header):
            if shape_errors < _MAX_SHAPE_ERRORS_PER_FILE:
                errors.append(
                    f"{file_name}:{line_no} has {len(row)} columns; expected {len(header)}"
                )
            shape_errors += 1
            continue

        if file_name == "stops.txt" and stops_lat_idx is not None and stops_lon_idx is not None:
            lat_raw = str(row[stops_lat_idx]).strip()
            lon_raw = str(row[stops_lon_idx]).strip()
            try:
                lat = float(lat_raw)
                lon = float(lon_raw)
            except Exception:
                if shape_errors < _MAX_SHAPE_ERRORS_PER_FILE:
                    errors.append(
                        f"stops.txt:{line_no} has non-numeric stop_lat/stop_lon: "
                        f"'{lat_raw}', '{lon_raw}'"
                    )
                shape_errors += 1
                continue
            if not (-90.0 <= lat <= 90.0):
                if shape_errors < _MAX_SHAPE_ERRORS_PER_FILE:
                    errors.append(f"stops.txt:{line_no} stop_lat out of range: {lat}")
                shape_errors += 1
            if not (-180.0 <= lon <= 180.0):
                if shape_errors < _MAX_SHAPE_ERRORS_PER_FILE:
                    errors.append(f"stops.txt:{line_no} stop_lon out of range: {lon}")
                shape_errors += 1

    if file_name in REQUIRED_FILES and int(result["row_count"]) <= 0:
        errors.append(f"{file_name} has no data rows")

    if shape_errors > _MAX_SHAPE_ERRORS_PER_FILE:
        warnings.append(
            f"{file_name} has additional row-shape/value errors not listed "
            f"({shape_errors - _MAX_SHAPE_ERRORS_PER_FILE} more)"
        )

    return result


def validate_gtfs_zip(file_path: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    if not file_path.exists() or not file_path.is_file():
        return {
            "ok": False,
            "errors": [f"File not found: {file_path}"],
            "warnings": [],
            "checksum_sha256": "",
            "files": [],
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

    if file_path.suffix.lower() != ".zip":
        errors.append("GTFS input must be a .zip file")

    checksum = compute_sha256(file_path)

    files_lower: set[str] = set()
    csv_checks: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            bad = zf.testzip()
            if bad:
                errors.append(f"ZIP integrity check failed at entry: {bad}")

            entries, dupes = _zip_entries_by_basename(zf)
            files_lower = set(entries.keys())

            if not files_lower:
                errors.append("ZIP file has no files")

            if dupes:
                for name, paths in sorted(dupes.items()):
                    warnings.append(
                        f"Duplicate file basename in ZIP ({name}); using first entry: {paths[0]}"
                    )

            missing_required = sorted(REQUIRED_FILES.difference(files_lower))
            if missing_required:
                errors.append("Missing required GTFS files: " + ", ".join(missing_required))

            if not SERVICE_FILES.intersection(files_lower):
                errors.append("Missing service calendar files: include calendar.txt or calendar_dates.txt")

            inspect_targets = sorted(REQUIRED_FILES.union(SERVICE_FILES).intersection(files_lower))
            for candidate in inspect_targets:
                info = entries.get(candidate)
                if info is None:
                    continue
                try:
                    raw = zf.read(info)
                except Exception as exc:
                    errors.append(f"Could not read {candidate} from zip: {exc}")
                    continue

                text = _decode_utf8_strict(raw, file_name=candidate, errors=errors, warnings=warnings)
                if text is None:
                    continue

                csv_checks[candidate] = _validate_csv_text(
                    file_name=candidate,
                    csv_text=text,
                    errors=errors,
                    warnings=warnings,
                )

    except zipfile.BadZipFile:
        errors.append("Invalid ZIP file format")
    except Exception as exc:
        errors.append(f"Failed reading ZIP: {exc}")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "checksum_sha256": checksum,
        "files": sorted(files_lower),
        "csv_checks": csv_checks,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "size_bytes": file_path.stat().st_size,
    }
