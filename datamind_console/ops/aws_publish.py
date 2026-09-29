from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
import shlex
from urllib.parse import urlencode
from urllib import error as urlerror
from urllib import request as urlrequest

from .config import OpsConfig
from .db_refresh import check_aws_db_status, run_aws_db_import, run_aws_db_refresh
from .gtfs_validate import validate_gtfs_zip
from .otp_control import (
    CommandResult,
    aws_otp_status,
    build_aws_otp_graph,
    detect_aws_otp_mode,
    run_ssh,
    scp_upload,
    restart_aws_otp,
)
from .pre_deploy_enforcer import PreDeployEnforcer


LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]

_GTFS_CLEAN_FILES = [
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
]


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


def _remote_upload_path(local_file: Path, incoming_dir: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = f"{timestamp}_{local_file.name}"
    return f"{incoming_dir.rstrip('/')}/{safe_name}"


def _aws_incoming_dir_candidates(cfg: OpsConfig) -> list[str]:
    candidates: list[str] = [cfg.aws_gtfs_incoming_dir]
    if cfg.aws_transportapp_dir:
        candidates.append(f"{cfg.aws_transportapp_dir.rstrip('/')}/gtfs_incoming")
    candidates.extend(
        [
            "/opt/gtfs_app/gtfs_incoming",
            "/tmp/gtfs/incoming",
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _prepare_aws_incoming_dir(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> Optional[str]:
    for candidate in _aws_incoming_dir_candidates(cfg):
        check = run_ssh(
            cfg,
            (
                f"mkdir -p {shlex.quote(candidate)} && "
                f"[ -w {shlex.quote(candidate)} ] && "
                f"printf '%s' {shlex.quote(candidate)}"
            ),
            timeout=60,
        )
        if check.ok and check.stdout.strip():
            _log(log, f"Using AWS incoming dir: {candidate}")
            return candidate
    _log(log, "Could not create/write any AWS incoming dir candidate.")
    return None


def _aws_extract_dir_candidates(cfg: OpsConfig) -> list[str]:
    candidates: list[str] = []
    if cfg.aws_gtfs_extract_dir:
        candidates.append(cfg.aws_gtfs_extract_dir)
    if cfg.aws_transportapp_dir:
        root = cfg.aws_transportapp_dir.rstrip("/")
        candidates.append(f"{root}/otp/otp-data/routers/default")
        candidates.append(f"{root}/gtfs_data")
    candidates.extend(
        [
            "/opt/gtfs_app/otp/otp-data/routers/default",
            "/opt/gtfs_app/gtfs_data",
            "/opt/gtfs_app/otp/otp-data/routers/default",
            "/opt/gtfs_app/gtfs_data",
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _detect_aws_extract_dir(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> Optional[str]:
    for candidate in _aws_extract_dir_candidates(cfg):
        check = run_ssh(
            cfg,
            f"if [ -d {shlex.quote(candidate)} ]; then printf '%s' {shlex.quote(candidate)}; fi",
            timeout=30,
        )
        if check.ok and check.stdout.strip():
            _log(log, f"Detected AWS GTFS target dir: {candidate}")
            return candidate
    _log(log, "Could not auto-detect AWS GTFS target dir from standard candidates.")
    return None


def _local_graph_candidates(cfg: OpsConfig) -> list[Path]:
    candidates: list[Path] = []
    if cfg.local_gtfs_extract_dir:
        candidates.append(cfg.local_gtfs_extract_dir / "graph.obj")
    if cfg.transportapp_root:
        candidates.append(cfg.transportapp_root / "otp" / "otp-data" / "routers" / "default" / "graph.obj")

    out: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        if item.exists() and item.is_file():
            out.append(item)
    return out


def _is_fresh_graph(graph_path: Path, gtfs_zip_path: Path) -> bool:
    try:
        graph_mtime = graph_path.stat().st_mtime
        gtfs_mtime = gtfs_zip_path.stat().st_mtime
        return graph_mtime >= gtfs_mtime
    except Exception:
        return False


def run_aws_publish(
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

    # ── Step 0: Pre-deploy enforcer (MANDATORY — cannot be skipped) ──
    _progress(progress, 0.01, "Running pre-deploy enforcer")
    _log(log, "Pre-deploy enforcer: cleanup + resource checks before deployment")
    enforcer = PreDeployEnforcer()
    deploy_report = enforcer.enforce(cfg, gtfs_zip_path, log=log)
    out["steps"]["pre_deploy_enforcer"] = {
        "passed": deploy_report.passed,
        "checks": [
            {"name": c.name, "passed": c.passed, "details": c.details}
            for c in deploy_report.checks
        ],
        "remote_cleanup": deploy_report.remote_cleanup,
        "local_cleanup": deploy_report.local_cleanup,
        "expected_counts": deploy_report.post_deploy,
    }
    if not deploy_report.passed:
        for reason in deploy_report.blocking_reasons:
            out["errors"].append(f"Pre-deploy enforcer BLOCKED: {reason}")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out
    _log(log, "Pre-deploy enforcer: ALL CHECKS PASS")

    # Note: enforcer already validated GTFS zip and tested SSH, but
    # we re-run these lightweight checks to populate the expected step keys.
    _progress(progress, 0.05, "Validating GTFS zip")
    validation = validate_gtfs_zip(gtfs_zip_path)
    out["steps"]["validate"] = validation
    if not validation.get("ok"):
        out["errors"].append("GTFS validation failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.10, "Checking SSH connectivity")
    ping = run_ssh(cfg, "echo connected", timeout=30)
    out["steps"]["ssh_ping"] = _command_payload(ping)
    if not ping.ok:
        out["errors"].append("SSH connection failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.22, "Creating remote incoming directory")
    incoming_dir = _prepare_aws_incoming_dir(cfg, log=log)
    if not incoming_dir:
        out["steps"]["remote_mkdir"] = {
            "ok": False,
            "error": "Could not create AWS incoming dir",
            "candidates": _aws_incoming_dir_candidates(cfg),
        }
        out["errors"].append("Failed to prepare remote incoming directory")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out
    out["steps"]["remote_mkdir"] = {
        "ok": True,
        "incoming_dir": incoming_dir,
        "candidates": _aws_incoming_dir_candidates(cfg),
    }
    remote_zip = _remote_upload_path(gtfs_zip_path, incoming_dir)

    _progress(progress, 0.36, "Uploading GTFS zip to AWS")
    _log(log, f"Uploading {gtfs_zip_path.name} to {remote_zip}")
    upload = scp_upload(cfg, gtfs_zip_path, remote_zip, timeout=600)
    out["steps"]["upload"] = _command_payload(upload)
    out["steps"]["upload"]["remote_zip"] = remote_zip
    if not upload.ok:
        out["errors"].append("SCP upload failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.50, "Applying GTFS on EC2")
    effective_extract_dir = _detect_aws_extract_dir(cfg, log=log)
    out["steps"]["extract_dir"] = {
        "configured": cfg.aws_gtfs_extract_dir,
        "effective": effective_extract_dir,
    }
    if cfg.aws_gtfs_apply_cmd:
        cmd = (
            f"GTFS_REMOTE_ZIP={shlex.quote(remote_zip)} "
            f"AWS_GTFS_EXTRACT_DIR={shlex.quote(effective_extract_dir or '')} "
            f"{cfg.aws_gtfs_apply_cmd}"
        )
        apply_res = run_ssh(cfg, cmd, timeout=1800)
    elif effective_extract_dir:
        clean_txt_cmd = " ".join(
            f"rm -f {shlex.quote(effective_extract_dir.rstrip('/') + '/' + name)};"
            for name in _GTFS_CLEAN_FILES
        )
        cmd = (
            f"mkdir -p {shlex.quote(effective_extract_dir)} && "
            f"find {shlex.quote(effective_extract_dir)} -maxdepth 1 -type f -name '*.zip' ! -name 'latest_upload_gtfs.zip' -delete && "
            f"{clean_txt_cmd} "
            f"cp -f {shlex.quote(remote_zip)} {shlex.quote(effective_extract_dir.rstrip('/') + '/latest_upload_gtfs.zip')} && "
            f"unzip -o {shlex.quote(remote_zip)} -d {shlex.quote(effective_extract_dir)}"
        )
        apply_res = run_ssh(cfg, cmd, timeout=1800)
    else:
        apply_res = CommandResult(False, 1, "", "Missing AWS_GTFS_EXTRACT_DIR or AWS_GTFS_APPLY_CMD", "")
    out["steps"]["apply_gtfs"] = _command_payload(apply_res)
    if not apply_res.ok:
        out["errors"].append("Failed to apply GTFS on EC2")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.62, "Importing GTFS into AWS DB")
    _log(log, "Importing GTFS into AWS transit DB")
    db_import = run_aws_db_import(cfg, gtfs_extract_dir=effective_extract_dir, gtfs_remote_zip=remote_zip)
    out["steps"]["db_import"] = _command_payload(db_import)
    if not db_import.ok:
        out["errors"].append("AWS DB GTFS import failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.70, "Detecting AWS OTP runtime")
    mode_info = detect_aws_otp_mode(cfg, log=log)
    out["steps"]["otp_detect"] = mode_info
    if str(mode_info.get("mode") or "unknown") == "unknown":
        out["errors"].append("Could not detect AWS OTP runtime mode")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    # ── OTP Graph: ALWAYS build locally, upload to EC2 (never build on EC2) ──
    _progress(progress, 0.75, "Building OTP graph locally and uploading to EC2")
    graph_result = enforcer.build_and_upload_graph(cfg, gtfs_zip_path, log=log)
    out["steps"]["otp_graph_local_build"] = {
        "name": graph_result.name,
        "passed": graph_result.passed,
        "details": graph_result.details,
        "value": graph_result.value,
    }
    if not graph_result.passed:
        out["errors"].append(f"Local graph build/upload failed: {graph_result.details}")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.89, "Restarting AWS OTP")
    restart_res = restart_aws_otp(cfg, mode_info, log=log)
    out["steps"]["otp_restart"] = _command_payload(restart_res)
    if not restart_res.ok:
        out["errors"].append("AWS OTP restart failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    if effective_extract_dir:
        graph_stat = run_ssh(
            cfg,
            (
                f"if [ -f {shlex.quote(effective_extract_dir.rstrip('/') + '/graph.obj')} ]; then "
                f"stat -c '%Y %s' {shlex.quote(effective_extract_dir.rstrip('/') + '/graph.obj')}; "
                "fi"
            ),
            timeout=60,
        )
        out["steps"]["otp_graph_remote_stat"] = _command_payload(graph_stat)

    _progress(progress, 0.95, "Refreshing AWS DB / FTS")
    db_refresh = run_aws_db_refresh(cfg)
    out["steps"]["db_refresh"] = _command_payload(db_refresh)
    if not db_refresh.ok:
        out["errors"].append("AWS DB refresh failed")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    _progress(progress, 0.97, "Checking AWS status")
    out["steps"]["status"] = check_aws_status(cfg, log=log)

    # ── Post-deploy verification (enforcer step 4) ──
    _progress(progress, 0.99, "Running post-deploy verification")
    expected = out["steps"].get("pre_deploy_enforcer", {}).get("expected_counts", {})
    post_report = enforcer.post_deploy_verify(
        cfg,
        expected_routes=expected.get("expected_routes", 0),
        expected_stops=expected.get("expected_stops", 0),
        log=log,
    )
    out["steps"]["post_deploy_verify"] = {
        "passed": post_report.passed,
        "checks": [
            {"name": c.name, "passed": c.passed, "details": c.details, "value": c.value}
            for c in post_report.checks
        ],
    }
    if post_report.blocking_reasons:
        for reason in post_report.blocking_reasons:
            _log(log, f"POST-DEPLOY WARNING: {reason}")

    out["ok"] = True
    out["finished_at"] = datetime.now(timezone.utc).isoformat()
    _progress(progress, 1.0, "AWS publish completed")
    return out


def check_aws_status(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    ssh_ping = run_ssh(cfg, "echo connected", timeout=30)
    ssh_stderr = (ssh_ping.stderr or "").strip()
    ssh_stdout = (ssh_ping.stdout or "").strip()
    ssh_error = ssh_stderr or ssh_stdout or "SSH connectivity check failed"
    if not ssh_ping.ok:
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "ssh": {
                "ok": False,
                "stdout": ssh_stdout,
                "stderr": ssh_stderr,
                "returncode": ssh_ping.returncode,
            },
            "aws_otp": {
                "mode": "ssh_unreachable",
                "running": False,
                "health": {
                    "ok": False,
                    "reachable": False,
                    "error": f"Cannot probe AWS OTP because SSH failed: {ssh_error}",
                },
                "detail": {
                    "mode": "ssh_unreachable",
                    "running": False,
                },
            },
            "aws_db": {
                "ok": False,
                "mode": "ssh_unreachable",
                "error": f"Cannot probe AWS DB because SSH failed: {ssh_error}",
                "stderr": ssh_stderr,
            },
        }

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ssh": {
            "ok": True,
            "stdout": ssh_stdout,
            "stderr": ssh_stderr,
            "returncode": ssh_ping.returncode,
        },
        "aws_otp": aws_otp_status(cfg, log=log),
        "aws_db": check_aws_db_status(cfg),
    }


def _collect_coords(payload: Any) -> list[tuple[float, float]]:
    found: list[tuple[float, float]] = []

    def walk(node: Any, depth: int) -> None:
        if depth > 8 or len(found) >= 6:
            return
        if isinstance(node, dict):
            lat = node.get("lat")
            lon = node.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                found.append((float(lat), float(lon)))
            for value in node.values():
                walk(value, depth + 1)
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload, 0)
    return found


def _endpoint_url(base_url: str, path: str, params: Optional[dict[str, Any]] = None) -> str:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
    clean = f"{base_url.rstrip('/')}{path}"
    return clean if not query else f"{clean}?{query}"


def _payload_success(payload: Any) -> Optional[bool]:
    if isinstance(payload, dict) and "success" in payload:
        return bool(payload.get("success"))
    return None


def _clean_ssh_stderr(stderr: str) -> str:
    cleaned = [
        line.strip()
        for line in (stderr or "").splitlines()
        if line.strip() and not line.strip().startswith("Warning: Permanently added")
    ]
    return "\n".join(cleaned).strip()


def _aws_json_http_get(cfg: OpsConfig, url: str, *, timeout: int = 20) -> dict[str, Any]:
    marker = "__HTTP_CODE__"
    cmd = (
        "set -euo pipefail\n"
        "TMP=$(mktemp)\n"
        f"CODE=$(curl -sS -m {int(timeout)} -o \"$TMP\" -w '%{{http_code}}' {shlex.quote(url)} || true)\n"
        f"printf '{marker}%s\\n' \"$CODE\"\n"
        "cat \"$TMP\" || true\n"
        "rm -f \"$TMP\"\n"
    )
    res = run_ssh(cfg, cmd, timeout=max(35, timeout + 15))
    stdout = res.stdout or ""
    lines = stdout.splitlines()

    status_code: Optional[int] = None
    body = stdout
    if lines and lines[0].startswith(marker):
        raw_code = lines[0][len(marker) :].strip()
        body = "\n".join(lines[1:])
        try:
            code_int = int(raw_code)
            status_code = code_int if code_int > 0 else None
        except Exception:
            status_code = None

    payload: Any = None
    json_ok = False
    body_strip = body.strip()
    if body_strip:
        try:
            payload = json.loads(body_strip)
            json_ok = True
        except Exception:
            payload = None
            json_ok = False

    ok = bool(status_code and 200 <= status_code < 300)
    clean_stderr = _clean_ssh_stderr(res.stderr or "")
    error = ""
    body_preview = body_strip[:240]
    if not ok:
        if isinstance(payload, dict):
            perr = payload.get("error")
            if isinstance(perr, dict):
                error = str(perr.get("message") or perr.get("code") or "").strip()
            elif isinstance(perr, str):
                error = perr.strip()
        if not error:
            error = clean_stderr or f"HTTP {status_code or '?'}"
        if body_preview:
            error = f"{error} | body={body_preview}"
    elif not json_ok:
        error = "Response is not valid JSON"

    return {
        "ok": ok,
        "json_ok": json_ok,
        "status_code": status_code,
        "url": url,
        "payload": payload,
        "error": error,
        "body_preview": body_preview,
    }


def _run_aws_json_endpoint(
    cfg: OpsConfig,
    *,
    name: str,
    base_url: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    timeout: int = 18,
) -> dict[str, Any]:
    url = _endpoint_url(base_url, path, params)
    res = _aws_json_http_get(cfg, url, timeout=timeout)
    payload = res.get("payload")
    ok = bool(res.get("ok") and res.get("json_ok"))
    reason = "ok"

    success_flag = _payload_success(payload)
    if ok and success_flag is False:
        ok = False
        reason = "JSON payload contains success=false"

    if not ok and reason == "ok":
        reason = str(res.get("error") or f"HTTP {res.get('status_code') or '?'}")

    return {
        "ok": ok,
        "status_code": res.get("status_code"),
        "url": url,
        "reason": reason,
        "response": payload if isinstance(payload, dict) else None,
        "name": name,
    }


def _run_public_json_endpoint(
    *,
    name: str,
    base_url: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: int = 20,
) -> dict[str, Any]:
    res = _datamind_api_get(
        base_url=base_url,
        path=path,
        params=params,
        api_key=api_key,
        timeout=timeout,
    )
    payload = res.get("payload")
    ok = bool(res.get("ok") and isinstance(payload, dict))
    reason = "ok"
    success_flag = _payload_success(payload)
    if ok and success_flag is False:
        ok = False
        reason = "JSON payload contains success=false"
    if not ok and reason == "ok":
        reason = str(res.get("error") or f"HTTP {res.get('status_code') or '?'}")
    return {
        "ok": ok,
        "status_code": res.get("status_code"),
        "url": res.get("url"),
        "reason": reason,
        "response": payload if isinstance(payload, dict) else None,
        "name": name,
        "via": "public_http",
    }


def _run_public_post_json_endpoint(
    *,
    name: str,
    base_url: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: int = 20,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urlrequest.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header(
        "User-Agent",
        os.getenv("AWS_PUBLIC_API_USER_AGENT")
        or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status_code = int(getattr(resp, "status", 200) or 200)
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as e:
        status_code = int(getattr(e, "code", 500) or 500)
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(e)
    except Exception as e:
        return {
            "ok": False,
            "status_code": None,
            "url": url,
            "reason": f"HTTP request failed: {e}",
            "response": None,
            "name": name,
            "via": "public_http",
        }
    payload: Any = None
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = None
    ok = bool(status_code and 200 <= status_code < 300 and isinstance(payload, dict))
    reason = "ok"
    success_flag = _payload_success(payload)
    if ok and success_flag is False:
        ok = False
        reason = "JSON payload contains success=false"
    if not ok and reason == "ok":
        msg = ""
        if isinstance(payload, dict):
            perr = payload.get("error")
            if isinstance(perr, dict):
                msg = str(perr.get("message") or perr.get("code") or "").strip()
            elif isinstance(perr, str):
                msg = perr.strip()
        reason = msg or f"HTTP {status_code or '?'}"
    return {
        "ok": ok,
        "status_code": status_code,
        "url": url,
        "reason": reason,
        "response": payload if isinstance(payload, dict) else None,
        "name": name,
        "via": "public_http",
    }


def _skipped_check(name: str, reason: str) -> dict[str, Any]:
    return {
        "ok": True,
        "skipped": True,
        "reason": reason,
        "name": name,
    }


def _datamind_api_get(
    *,
    base_url: str,
    path: str,
    params: Optional[dict[str, Any]] = None,
    api_key: Optional[str] = None,
    timeout: int = 20,
) -> dict[str, Any]:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{base_url.rstrip('/')}{path}"
    full_url = url if not query else f"{url}?{query}"
    req = urlrequest.Request(full_url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header(
        "User-Agent",
        os.getenv("AWS_PUBLIC_API_USER_AGENT")
        or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            status_code = int(getattr(resp, "status", 200) or 200)
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as e:
        status_code = int(getattr(e, "code", 500) or 500)
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = str(e)
    except Exception as e:
        return {
            "ok": False,
            "status_code": None,
            "url": full_url,
            "payload": None,
            "error": f"HTTP request failed: {e}",
        }
    payload: Any = None
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = None
    ok = bool(status_code and 200 <= status_code < 300 and isinstance(payload, dict))
    error = ""
    if not ok:
        if isinstance(payload, dict):
            perr = payload.get("error")
            if isinstance(perr, dict):
                error = str(perr.get("message") or perr.get("code") or "").strip()
            elif isinstance(perr, str):
                error = perr.strip()
        if not error:
            error = f"HTTP {status_code or '?'}"
    return {
        "ok": ok,
        "status_code": status_code,
        "url": full_url,
        "payload": payload,
        "error": error,
    }


def _probe_geo_via_datamind_api(
    *,
    geo_api_base: str,
    api_key: Optional[str],
    fallback_lat: float,
    fallback_lon: float,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    health = _datamind_api_get(base_url=geo_api_base, path="/api/geo/health", params={}, api_key=api_key, timeout=16)
    health_payload = health.get("payload") if isinstance(health.get("payload"), dict) else {}
    health_err = (health_payload.get("error") or {}) if isinstance(health_payload, dict) else {}
    h_ok = bool(
        isinstance(health_payload, dict)
        and not health_err
        and str(health_payload.get("status") or "").strip().lower() == "healthy"
        and bool((health_payload.get("checks") or {}).get("db"))
        and bool((health_payload.get("checks") or {}).get("nominatim"))
    )
    out["backend_geo_health"] = {
        "ok": h_ok,
        "status_code": health.get("status_code"),
        "url": f"{geo_api_base.rstrip('/')}/api/geo/health",
        "reason": "ok" if h_ok else str(
            (health_err.get("message") if isinstance(health_err, dict) else "")
            or f"Geo health is {health_payload.get('status') if isinstance(health_payload, dict) else 'unknown'} "
            f"(db={bool((health_payload.get('checks') or {}).get('db')) if isinstance(health_payload, dict) else False}, "
            f"nominatim={bool((health_payload.get('checks') or {}).get('nominatim')) if isinstance(health_payload, dict) else False})"
        ),
        "status": (health_payload.get("status") if isinstance(health_payload, dict) else None),
        "checks": (health_payload.get("checks") if isinstance(health_payload, dict) else None),
        "via": "datamind_api_http",
    }

    geo_calls = [
        ("geo_geocode", "/api/geo/geocode", {"q": "terminal quitumbe", "top_k": 2}, "geocode"),
        ("geo_autocomplete", "/api/geo/autocomplete", {"q": "term", "top_k": 3}, "autocomplete"),
        ("geo_reverse", "/api/geo/reverse", {"lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}", "top_k": 2}, "reverse"),
    ]
    for name, path, params, suffix in geo_calls:
        result = _datamind_api_get(base_url=geo_api_base, path=path, params=params, api_key=api_key, timeout=20)
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        err = (payload.get("error") or {}) if isinstance(payload, dict) else {}
        ok = bool(result.get("ok") and isinstance(payload, dict) and not err)
        out[name] = {
            "ok": ok,
            "status_code": result.get("status_code"),
            "url": f"{geo_api_base.rstrip('/')}/api/geo/{suffix}",
            "reason": "ok" if ok else str(
                (err.get("message") if isinstance(err, dict) else "")
                or result.get("error")
                or "Geo API request failed"
            ),
            "response": payload if isinstance(payload, dict) and not err else None,
            "name": name,
            "via": "datamind_api_http",
        }
    return out


def run_all_aws_server_api_checks(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    checked_at = datetime.now(timezone.utc).isoformat()
    public_api_base = (
        os.getenv("AWS_PUBLIC_API_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or ""
    ).strip()
    # Frontend-aligned behavior: do not inject console/geo API keys into public app endpoints
    # unless the user explicitly sets a dedicated public checker key.
    public_api_key = (os.getenv("AWS_PUBLIC_API_KEY") or "").strip() or None

    if not public_api_base:
        return {
            "ok": False,
            "all_ok": False,
            "status": "STATUS_FALSE",
            "checked_at": checked_at,
            "mode": "public_http_frontend",
            "failed_checks": ["public_api_base_url"],
            "checks": {
                "public_api_base_url": {
                    "ok": False,
                    "reason": "Set AWS_PUBLIC_API_BASE_URL to run ALL AWS APIs via public frontend path (no SSH fallback).",
                    "name": "public_api_base_url",
                }
            },
        }

    _log(log, f"Running full AWS server API checks via public base ({public_api_base})")
    fallback_lat = -0.210000
    fallback_lon = -78.490000
    mode_label = "public_http_frontend"
    _log(log, "Frontend source of truth: reise_app/lib/services (ApiClient + service methods)")
    now = datetime.now()
    route_params = {
        "fromLat": f"{fallback_lat:.6f}",
        "fromLon": f"{fallback_lon:.6f}",
        "toLat": f"{(fallback_lat + 0.001):.6f}",
        "toLon": f"{(fallback_lon + 0.001):.6f}",
        "profile": "walk",
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "forceFresh": "true",
        "numItineraries": "1",
    }
    _log(log, "Checking public endpoint /health (ApiClient)")
    _log(log, "Checking public endpoint /api/routes (RouteService.planRoute)")
    checks: dict[str, Any] = {
        "backend_health": _run_public_json_endpoint(
            name="backend_health",
            base_url=public_api_base,
            path="/health",
            api_key=public_api_key,
            timeout=16,
        ),
        "backend_routes": _run_public_json_endpoint(
            name="backend_routes",
            base_url=public_api_base,
            path="/api/routes",
            params=route_params,
            api_key=public_api_key,
            timeout=36,
        ),
    }

    geo_api_base = (os.getenv("AWS_DATAMIND_API_BASE_URL") or public_api_base).strip()
    _log(log, "Checking public endpoint /api/geo/health")
    _log(log, "Checking public endpoint /api/geo/geocode")
    _log(log, "Checking public endpoint /api/geo/autocomplete")
    _log(log, "Checking public endpoint /api/geo/reverse")
    checks.update(
        _probe_geo_via_datamind_api(
            geo_api_base=geo_api_base,
            api_key=public_api_key,
            fallback_lat=fallback_lat,
            fallback_lon=fallback_lon,
        )
    )

    endpoint_checks = [
        ("search_global", "/api/search", {"q": "terminal"}),
        ("search_lines", "/api/search/lines", {"q": "ecovia"}),
        ("search_stations", "/api/search/stations", {"q": "terminal"}),
        ("search_geocode_alias", "/api/search/geocode", {"q": "terminal quitumbe", "top_k": 2}),
        ("search_reverse_alias", "/api/search/reversegeocode", {"lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}", "top_k": 2}),
        ("search_autocomplete", "/api/search/autocomplete", {"q": "term"}),
        ("lines_all", "/api/lines", {}),
        ("lines_mode_bus", "/api/lines/mode/bus", {}),
        ("stations_all", "/api/stations", {}),
        ("stations_nearby", "/api/stations/nearby", {"lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}", "radius": 1500}),
        ("realtime_nearby_vehicles", "/api/realtime/nearby-vehicles", {"lat": f"{fallback_lat:.6f}", "lon": f"{fallback_lon:.6f}", "radius": 1000}),
    ]
    for name, path, params in endpoint_checks:
        _log(log, f"Checking public endpoint {path}")
        probe = _run_public_json_endpoint(
            name=name,
            base_url=public_api_base,
            path=path,
            params=params,
            api_key=public_api_key,
            timeout=24,
        )
        checks[name] = probe

    _log(log, "Checking public endpoint /api/eta/nearby (RealtimeService.getNearbyRouteEtas)")
    checks["eta_nearby"] = _run_public_post_json_endpoint(
        name="eta_nearby",
        base_url=public_api_base,
        path="/api/eta/nearby",
        api_key=public_api_key,
        body={
            "userLocation": {"lat": fallback_lat, "lon": fallback_lon},
            "maxStops": 5,
            "maxVehicles": 50,
        },
        timeout=24,
    )

    checks["contact_submit"] = _skipped_check(
        "contact_submit",
        "Skipped by default (POST /api/contact is mutating). Set AWS_PUBLIC_API_CHECK_ALLOW_MUTATIONS=true to enable.",
    )
    checks["lines_detail"] = _skipped_check("lines_detail", "Skipped by default (GET /api/lines/:id requires a valid routeId fixture).")
    checks["stations_detail"] = _skipped_check("stations_detail", "Skipped by default (GET /api/stations/:id requires a valid stationId fixture).")
    checks["stations_by_line"] = _skipped_check("stations_by_line", "Skipped by default (GET /api/stations/line/:routeId requires a valid routeId fixture).")
    checks["realtime_vehicle_detail"] = _skipped_check("realtime_vehicle_detail", "Skipped by default (GET /api/realtime/vehicle/:vehicleId requires a live vehicle id).")
    checks["realtime_eta_legacy"] = _skipped_check("realtime_eta_legacy", "Skipped by default (GET /api/realtime/eta needs stop_id and route_id fixtures).")
    checks["auth_login"] = _skipped_check("auth_login", "Skipped by default (requires test credentials for /api/auth/login).")
    checks["auth_register"] = _skipped_check("auth_register", "Skipped by default (mutating user creation via /api/auth/register).")
    checks["driver_login"] = _skipped_check("driver_login", "Skipped by default (requires driver test credentials for /api/driver/login).")
    checks["driver_register"] = _skipped_check("driver_register", "Skipped by default (mutating /api/driver/admin/drivers).")
    checks["driver_telemetry"] = _skipped_check("driver_telemetry", "Skipped by default (mutating /api/driver/telemetry).")
    checks["trips_history"] = _skipped_check("trips_history", "Skipped by default (requires auth token + user/trip IDs for TripsService).")
    checks["trips_details"] = _skipped_check("trips_details", "Skipped by default (GET /api/trips/details/:tripId requires a valid tripId).")
    checks["trips_eta"] = _skipped_check("trips_eta", "Skipped by default (GET /api/trips/eta/:tripId requires tripId + stop_id).")
    checks["trips_save"] = _skipped_check("trips_save", "Skipped by default (POST /api/trips/save is mutating and requires auth + valid payload).")
    checks["driver_session_status"] = _skipped_check("driver_session_status", "Skipped by default (requires driver auth token).")
    checks["driver_service_start"] = _skipped_check("driver_service_start", "Skipped by default (POST /api/driver/service/start is mutating and requires driver auth).")
    checks["driver_service_stop"] = _skipped_check("driver_service_stop", "Skipped by default (POST /api/driver/service/stop is mutating and requires driver auth).")
    checks["driver_agencies"] = _skipped_check("driver_agencies", "Skipped by default in public checker (frontend call usually auth-backed context).")
    checks["driver_vehicles_available"] = _skipped_check("driver_vehicles_available", "Skipped by default (requires driver auth token).")
    checks["driver_routes"] = _skipped_check("driver_routes", "Skipped by default (requires driver auth token).")
    checks["ops_trip_slots"] = _skipped_check("ops_trip_slots", "Skipped by default (requires routeId fixture and driver auth context).")
    checks["ops_next_trip"] = _skipped_check("ops_next_trip", "Skipped by default (requires routeId + query fixtures and driver auth context).")
    checks["ops_trip_shape"] = _skipped_check("ops_trip_shape", "Skipped by default (requires tripId fixture and driver auth context).")
    checks["driver_navigation"] = _skipped_check("driver_navigation", "Skipped by default (requires driver auth token + tripId fixture).")

    all_ok = all(bool(item.get("ok")) for item in checks.values() if not bool(item.get("skipped")))
    failed = sorted(name for name, item in checks.items() if not bool(item.get("ok")))
    return {
        "ok": all_ok,
        "all_ok": all_ok,
        "status": "STATUS_AWESOME" if all_ok else "STATUS_FALSE",
        "checked_at": checked_at,
        "mode": mode_label,
        "backend_base_url": public_api_base,
        "checks": checks,
        "failed_checks": failed,
    }
