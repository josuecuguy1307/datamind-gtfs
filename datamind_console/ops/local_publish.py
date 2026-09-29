from __future__ import annotations

import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import OpsConfig
from .db_refresh import check_local_db_status, run_local_db_import, run_local_db_refresh
from .gtfs_validate import validate_gtfs_zip
from .otp_control import (
    CommandResult,
    build_local_otp_graph,
    detect_local_otp_mode,
    local_otp_status,
    restart_local_otp,
)


LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]

_GTFS_CLEAN_FILES = {
    "agency.txt",
    "stops.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "shapes.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "feed_info.txt",
    "frequencies.txt",
    "transfers.txt",
    "pathways.txt",
    "fare_attributes.txt",
    "fare_rules.txt",
    "levels.txt",
}


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)


def _progress(progress: Optional[ProgressFn], fraction: float, msg: str) -> None:
    if progress:
        progress(max(0.0, min(1.0, fraction)), msg)


def _command_payload(result: CommandResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "returncode": result.returncode,
        "command": result.command,
        "stdout": (result.stdout or "")[-4000:],
        "stderr": (result.stderr or "")[-4000:],
    }


def _extract_zip_to_dir(gtfs_zip_path: Path, target_dir: Path) -> dict[str, Any]:
    target_dir.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []

    for old_zip in target_dir.glob("*.zip"):
        try:
            old_zip.unlink()
            removed.append(old_zip.name)
        except Exception:
            pass

    for name in sorted(_GTFS_CLEAN_FILES):
        p = target_dir / name
        if p.exists() and p.is_file():
            try:
                p.unlink()
                removed.append(name)
            except Exception:
                pass

    otp_zip_copy = target_dir / "latest_upload_gtfs.zip"
    shutil.copy2(gtfs_zip_path, otp_zip_copy)

    extracted: list[str] = []
    with zipfile.ZipFile(gtfs_zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            file_name = Path(info.filename).name
            if not file_name:
                continue
            dest = target_dir / file_name
            with zf.open(info, "r") as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(file_name)

    return {
        "target_dir": str(target_dir),
        "otp_zip_copy": str(otp_zip_copy),
        "removed_old": sorted(set(removed)),
        "files": sorted(set(extracted)),
        "count": len(set(extracted)),
    }


def run_local_publish(
    cfg: OpsConfig,
    gtfs_zip_path: Path,
    *,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    out: dict[str, Any] = {
        "ok": False,
        "started_at": started,
        "zip_path": str(gtfs_zip_path),
        "steps": {},
        "errors": [],
    }

    _progress(progress, 0.02, "Validating GTFS zip")
    validation = validate_gtfs_zip(gtfs_zip_path)
    out["steps"]["validate"] = validation
    if not validation.get("ok"):
        _log(log, "GTFS validation failed")
        out["errors"].append("GTFS validation failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out
    _log(log, "GTFS validation passed")

    if cfg.local_gtfs_extract_dir is not None:
        _progress(progress, 0.15, "Extracting GTFS files to local OTP data directory")
        try:
            extraction = _extract_zip_to_dir(gtfs_zip_path, cfg.local_gtfs_extract_dir)
            out["steps"]["extract_local_gtfs"] = {"ok": True, **extraction}
            _log(log, f"Extracted {extraction['count']} GTFS files to {extraction['target_dir']}")
        except Exception as exc:
            out["steps"]["extract_local_gtfs"] = {"ok": False, "error": str(exc)}
            out["errors"].append(f"Failed to extract GTFS files: {exc}")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

    _progress(progress, 0.35, "Running local DB GTFS import")
    _log(log, "Running LOCAL_GTFS_IMPORT_CMD")
    import_result = run_local_db_import(cfg, gtfs_zip_path)
    out["steps"]["db_import"] = _command_payload(import_result)
    if not import_result.ok:
        out["errors"].append("Local DB import failed")
        _log(log, "Local DB import failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.55, "Refreshing local DB / FTS")
    _log(log, "Running local DB refresh SQL")
    refresh_result = run_local_db_refresh(cfg)
    out["steps"]["db_refresh"] = _command_payload(refresh_result)
    if not refresh_result.ok:
        out["errors"].append("Local DB refresh failed")
        _log(log, "Local DB refresh failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.70, "Detecting local OTP runtime")
    mode_info = detect_local_otp_mode(cfg, log=log)
    out["steps"]["otp_detect"] = mode_info
    if str(mode_info.get("mode") or "unknown") == "unknown":
        out["errors"].append("Could not detect local OTP runtime mode")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.82, "Building local OTP graph")
    build_result = build_local_otp_graph(cfg, mode_info, log=log)
    out["steps"]["otp_build"] = _command_payload(build_result)
    if not build_result.ok:
        _log(log, "Local OTP graph build failed. Attempting to bring OTP back online.")
        recover_result = restart_local_otp(cfg, mode_info, log=log)
        out["steps"]["otp_recover_restart"] = _command_payload(recover_result)
        out["errors"].append("Local OTP graph build failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.93, "Restarting local OTP")
    restart_result = restart_local_otp(cfg, mode_info, log=log)
    out["steps"]["otp_restart"] = _command_payload(restart_result)
    if not restart_result.ok:
        out["errors"].append("Local OTP restart failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.98, "Checking local status")
    status = {
        "local_db": check_local_db_status(cfg),
        "local_otp": local_otp_status(cfg, log=log),
    }
    out["steps"]["status"] = status

    out["ok"] = True
    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    _progress(progress, 1.0, "Local publish completed")
    return out


def check_local_status(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "local_db": check_local_db_status(cfg),
        "local_otp": local_otp_status(cfg, log=log),
    }
