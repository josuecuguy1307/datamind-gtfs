from __future__ import annotations

import os
import shlex
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .config import OpsConfig
from .otp_control import CommandResult, run_ssh, scp_upload


LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]

_EXCLUDE_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    "safe_backups",
    ".idea",
}
_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
_TRANSPORT_FILES = {
    "docker-compose.yaml",
    "docker-compose.yml",
    "docker-compose.prod.yaml",
    "docker-compose.prod.yml",
    ".env",
    ".env.production",
}
_TRANSPORT_DIRS = {
    "BACKEND",
    "config",
    "init",
    "scripts",
    "gtfs_data",
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


def _is_transport_root(path: Path) -> bool:
    return (path / "docker-compose.yaml").exists() or (path / "docker-compose.yml").exists()


def _allow_transport_rel(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    top = parts[0]

    if top in _TRANSPORT_FILES and len(parts) == 1:
        return True
    if top in _TRANSPORT_DIRS:
        return True
    if top == "otp":
        if len(parts) == 2 and parts[1] == "otp.jar":
            return True
        if len(parts) >= 5 and parts[1] == "otp-data" and parts[2] == "routers" and parts[3] == "default":
            return True
    return False


def _iter_backend_files(source_dir: Path, *, transport_mode: bool):
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_PARTS]
        root_path = Path(root)
        for name in files:
            if name in _EXCLUDE_PARTS:
                continue
            suffix = Path(name).suffix.lower()
            if suffix in _EXCLUDE_SUFFIXES:
                continue
            file_path = root_path / name
            rel = file_path.relative_to(source_dir)
            if any(part in _EXCLUDE_PARTS for part in rel.parts):
                continue
            if transport_mode and not _allow_transport_rel(rel):
                continue
            yield file_path, rel


def _create_backend_archive(
    source_dir: Path,
    *,
    transport_mode: bool,
    archive_root: str,
) -> tuple[Path, Path, dict[str, Any]]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="datamind_backend_push_"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = tmp_dir / f"backend_{stamp}.tar.gz"

    files = 0
    bytes_total = 0
    with tarfile.open(archive_path, "w:gz") as tar:
        for file_path, rel in _iter_backend_files(source_dir, transport_mode=transport_mode):
            arcname = Path(archive_root) / rel
            tar.add(str(file_path), arcname=str(arcname), recursive=False)
            files += 1
            try:
                bytes_total += int(file_path.stat().st_size)
            except Exception:
                pass

    return tmp_dir, archive_path, {"files": files, "source_bytes": bytes_total}


def _compose_candidates(cfg: OpsConfig, remote_root: str) -> list[str]:
    candidates = [
        f"{remote_root.rstrip('/')}/docker-compose.yaml",
        f"{remote_root.rstrip('/')}/docker-compose.yml",
    ]
    if cfg.aws_transportapp_dir:
        app_root = cfg.aws_transportapp_dir.rstrip("/")
        candidates.append(f"{app_root}/docker-compose.yaml")
        candidates.append(f"{app_root}/docker-compose.yml")
    candidates.extend(
        [
            "/opt/gtfs_app/docker-compose.yaml",
            "/opt/gtfs_app/docker-compose.yml",
            "/opt/gtfs_app/docker-compose.yaml",
            "/opt/gtfs_app/docker-compose.yml",
        ]
    )
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _detect_backend_runtime(cfg: OpsConfig, remote_root: str, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    service = cfg.aws_backend_service_name
    for compose_file in _compose_candidates(cfg, remote_root):
        cmd = (
            f"if [ -f {shlex.quote(compose_file)} ]; then "
            f"docker compose -f {shlex.quote(compose_file)} config --services; "
            "fi"
        )
        out = run_ssh(cfg, cmd, timeout=60)
        if not out.ok:
            continue
        services = {line.strip() for line in out.stdout.splitlines() if line.strip()}
        if service in services:
            _log(log, f"Detected backend docker compose service '{service}' ({compose_file}).")
            return {
                "mode": "compose",
                "compose_file": compose_file,
                "service": service,
            }

    out = run_ssh(cfg, "docker ps -a --format '{{.Names}}'", timeout=45)
    if out.ok:
        names = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        for name in names:
            if service.lower() in name.lower() or "backend" in name.lower():
                _log(log, f"Detected backend docker container '{name}'.")
                return {"mode": "container", "container": name}

    unit = cfg.aws_backend_systemd_unit
    out = run_ssh(cfg, f"systemctl is-active {shlex.quote(unit)}", timeout=45)
    if out.ok or out.returncode in {3}:
        _log(log, f"Detected backend systemd unit '{unit}'.")
        return {"mode": "systemd", "unit": unit}

    return {"mode": "unknown"}


def _deploy_backend(cfg: OpsConfig, mode_info: dict[str, Any], *, log: Optional[LogFn] = None) -> CommandResult:
    if cfg.aws_backend_deploy_cmd:
        _log(log, "Running custom backend deploy command from AWS_BACKEND_DEPLOY_CMD")
        return run_ssh(cfg, cfg.aws_backend_deploy_cmd, timeout=5400)

    mode = str(mode_info.get("mode") or "")
    if mode == "compose":
        compose_file = str(mode_info.get("compose_file") or "")
        services_raw = list(mode_info.get("services") or cfg.aws_backend_deploy_services or (cfg.aws_backend_service_name,))
        services = [str(s).strip() for s in services_raw if str(s).strip()]
        if not services:
            services = [cfg.aws_backend_service_name]
        services_arg = " ".join(shlex.quote(s) for s in services)
        compose_dir = str(Path(compose_file).parent)
        prod_yaml = f"{compose_dir.rstrip('/')}/docker-compose.prod.yaml"
        prod_yml = f"{compose_dir.rstrip('/')}/docker-compose.prod.yml"
        cmd = (
            f"if [ -f {shlex.quote(prod_yaml)} ]; then "
            f"docker compose -f {shlex.quote(compose_file)} -f {shlex.quote(prod_yaml)} up -d --build {services_arg}; "
            f"elif [ -f {shlex.quote(prod_yml)} ]; then "
            f"docker compose -f {shlex.quote(compose_file)} -f {shlex.quote(prod_yml)} up -d --build {services_arg}; "
            f"else docker compose -f {shlex.quote(compose_file)} up -d --build {services_arg}; fi"
        )
        _log(log, f"Deploying docker services: {', '.join(services)}")
        return run_ssh(cfg, cmd, timeout=5400)

    if mode == "container":
        container = str(mode_info.get("container") or cfg.aws_backend_service_name)
        cmd = f"docker restart {shlex.quote(container)}"
        _log(log, f"Restarting backend container '{container}'")
        return run_ssh(cfg, cmd, timeout=300)

    if mode == "systemd":
        unit = str(mode_info.get("unit") or cfg.aws_backend_systemd_unit)
        cmd = f"systemctl restart {shlex.quote(unit)}"
        _log(log, f"Restarting backend systemd unit '{unit}'")
        return run_ssh(cfg, cmd, timeout=300)

    return CommandResult(False, 1, "", "Could not detect backend runtime. Set AWS_BACKEND_DEPLOY_CMD.", "")


def _backend_status(cfg: OpsConfig, mode_info: dict[str, Any]) -> dict[str, Any]:
    mode = str(mode_info.get("mode") or "")
    if mode == "compose":
        compose_file = str(mode_info.get("compose_file") or "")
        services_raw = list(mode_info.get("services") or cfg.aws_backend_deploy_services or (cfg.aws_backend_service_name,))
        services = [str(s).strip() for s in services_raw if str(s).strip()]
        if not services:
            services = [cfg.aws_backend_service_name]

        if compose_file:
            check = run_ssh(
                cfg,
                f"docker compose -f {shlex.quote(compose_file)} ps --services --status running",
                timeout=60,
            )
            running_services = {line.strip() for line in (check.stdout or "").splitlines() if line.strip()}
            service_status = {
                svc: {"running": svc in running_services}
                for svc in services
            }
            all_running = check.ok and all(svc in running_services for svc in services)
            return {
                "mode": mode,
                "ok": all_running,
                "services": services,
                "compose_file": compose_file,
                "running_services": sorted(running_services),
                "detail": service_status,
                "stdout": (check.stdout or "").strip(),
                "stderr": (check.stderr or "").strip(),
                "returncode": check.returncode,
            }

        service_status: dict[str, dict[str, Any]] = {}
        all_running = True
        for svc in services:
            check = run_ssh(cfg, f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(svc)}", timeout=60)
            running = check.ok and (check.stdout or "").strip().lower() == "true"
            service_status[svc] = {"running": running}
            all_running = all_running and running
        return {
            "mode": mode,
            "ok": all_running,
            "services": services,
            "detail": service_status,
        }

    if mode == "container":
        container = str(mode_info.get("container") or cfg.aws_backend_service_name)
        check = run_ssh(cfg, f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(container)}", timeout=60)
        running = check.ok and (check.stdout or "").strip().lower() == "true"
        return {
            "mode": mode,
            "ok": running,
            "container": container,
            "running": running,
            "stdout": (check.stdout or "").strip(),
            "stderr": (check.stderr or "").strip(),
        }

    if mode == "systemd":
        unit = str(mode_info.get("unit") or cfg.aws_backend_systemd_unit)
        check = run_ssh(cfg, f"systemctl is-active {shlex.quote(unit)}", timeout=60)
        active = (check.stdout or "").strip() == "active"
        return {
            "mode": mode,
            "ok": active,
            "unit": unit,
            "active": active,
            "stdout": (check.stdout or "").strip(),
            "stderr": (check.stderr or "").strip(),
        }

    return {"mode": mode or "unknown", "ok": False}


def _backend_status_with_retry(
    cfg: OpsConfig,
    mode_info: dict[str, Any],
    *,
    log: Optional[LogFn] = None,
) -> dict[str, Any]:
    mode = str(mode_info.get("mode") or "")
    if mode != "compose":
        return _backend_status(cfg, mode_info)

    # OTP can take time to load graph/data after compose up.
    max_attempts_raw = os.getenv("AWS_BACKEND_STATUS_MAX_ATTEMPTS", "12")
    sleep_seconds_raw = os.getenv("AWS_BACKEND_STATUS_RETRY_SECONDS", "10")
    try:
        max_attempts = max(1, int(max_attempts_raw))
    except Exception:
        max_attempts = 12
    try:
        sleep_seconds = max(1, int(sleep_seconds_raw))
    except Exception:
        sleep_seconds = 10

    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        status = _backend_status(cfg, mode_info)
        attempts.append(
            {
                "attempt": attempt,
                "ok": bool(status.get("ok")),
                "running_services": status.get("running_services"),
                "detail": status.get("detail"),
            }
        )
        if status.get("ok"):
            status["attempt"] = attempt
            status["max_attempts"] = max_attempts
            status["attempts"] = attempts
            return status
        if attempt < max_attempts:
            _log(
                log,
                f"AWS backend status not ready yet (attempt {attempt}/{max_attempts}); waiting {sleep_seconds}s before retry.",
            )
            time.sleep(sleep_seconds)

    status["attempt"] = max_attempts
    status["max_attempts"] = max_attempts
    status["attempts"] = attempts
    return status


def _validate_transport_bundle(source_dir: Path) -> list[str]:
    missing: list[str] = []
    compose_yaml = source_dir / "docker-compose.yaml"
    compose_yml = source_dir / "docker-compose.yml"
    if not compose_yaml.exists() and not compose_yml.exists():
        missing.append("docker-compose.yaml or docker-compose.yml")

    otp_jar = source_dir / "otp" / "otp.jar"
    if not otp_jar.is_file():
        missing.append("otp/otp.jar")

    otp_start = source_dir / "scripts" / "otp-start.sh"
    if not otp_start.is_file():
        missing.append("scripts/otp-start.sh")

    router_default = source_dir / "otp" / "otp-data" / "routers" / "default"
    if not router_default.is_dir():
        missing.append("otp/otp-data/routers/default")
    else:
        graph_obj = router_default / "graph.obj"
        if not graph_obj.is_file():
            missing.append("otp/otp-data/routers/default/graph.obj")

    return missing


def run_aws_backend_push(
    cfg: OpsConfig,
    *,
    log: Optional[LogFn] = None,
    progress: Optional[ProgressFn] = None,
    source_dir_override: Optional[Path] = None,
    require_transport_bundle: bool = False,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    out: dict[str, Any] = {
        "ok": False,
        "started_at": started,
        "steps": {},
        "errors": [],
    }

    source_dir = source_dir_override or cfg.backend_push_local_dir
    if source_dir is not None:
        try:
            source_dir = source_dir.expanduser().resolve()
        except Exception:
            source_dir = source_dir.expanduser()
    if source_dir is None:
        out["errors"].append("Missing BACKEND_PUSH_LOCAL_DIR or GTFS_APP_ROOT")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out
    if not source_dir.exists() or not source_dir.is_dir():
        out["errors"].append(f"Local backend dir not found: {source_dir}")
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out

    remote_root = (cfg.aws_backend_remote_dir or cfg.aws_transportapp_dir or "/opt/gtfs_app").rstrip("/")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote_tmp_dir = "/var/tmp/datamind_backend_push"
    remote_tar = f"{remote_tmp_dir}/{stamp}_backend.tar.gz"
    transport_mode = _is_transport_root(source_dir)
    if require_transport_bundle and not transport_mode:
        out["errors"].append(
            f"AWS full deploy requires GTFS_APP_ROOT style source (docker-compose file missing in: {source_dir})."
        )
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        return out
    if require_transport_bundle and transport_mode:
        missing_bundle = _validate_transport_bundle(source_dir)
        if missing_bundle:
            out["errors"].append(
                "Local gtfs_app bundle is incomplete for AWS deploy: " + ", ".join(missing_bundle)
            )
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

    out["steps"]["source"] = {
        "source_dir": str(source_dir),
        "transport_mode": transport_mode,
        "require_transport_bundle": require_transport_bundle,
    }
    archive_root = "gtfs_app_payload" if transport_mode else "BACKEND"

    tmp_local_dir: Optional[Path] = None
    archive_path: Optional[Path] = None
    try:
        _progress(progress, 0.08, "Checking SSH connectivity")
        ping = run_ssh(cfg, "echo connected", timeout=30)
        out["steps"]["ssh_ping"] = _command_payload(ping)
        if not ping.ok:
            out["errors"].append("SSH connection failed")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        _progress(progress, 0.22, "Preparing backend bundle")
        tmp_local_dir, archive_path, archive_meta = _create_backend_archive(
            source_dir,
            transport_mode=transport_mode,
            archive_root=archive_root,
        )
        out["steps"]["package"] = {
            "ok": True,
            "source_dir": str(source_dir),
            "mode": "transport_context" if transport_mode else "backend_only",
            "archive_path": str(archive_path),
            **archive_meta,
        }
        _log(log, f"Packaged backend dir: files={archive_meta.get('files')} bytes={archive_meta.get('source_bytes')}")
        if int(archive_meta.get("files") or 0) <= 0:
            out["errors"].append("No files to upload from local backend directory")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        _progress(progress, 0.36, "Preparing remote temp directory")
        remote_prep = run_ssh(
            cfg,
            (
                f"mkdir -p {shlex.quote(remote_tmp_dir)} && "
                f"find {shlex.quote(remote_tmp_dir)} -maxdepth 1 -type f -name '*_backend.tar.gz' -delete || true"
            ),
            timeout=60,
        )
        out["steps"]["remote_prepare"] = _command_payload(remote_prep)
        if not remote_prep.ok:
            out["errors"].append("Failed to prepare remote temp directory")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        _progress(progress, 0.50, "Uploading backend bundle to AWS")
        _log(log, f"Uploading backend bundle to {remote_tar}")
        upload_attempts: list[dict[str, Any]] = []
        upload = CommandResult(False, 1, "", "SCP upload not started", "")
        for attempt in range(1, 4):
            upload = scp_upload(cfg, archive_path, remote_tar, timeout=2700)
            payload = _command_payload(upload)
            payload["attempt"] = attempt
            payload["remote_tar"] = remote_tar
            upload_attempts.append(payload)
            if upload.ok:
                if attempt > 1:
                    _log(log, f"SCP upload succeeded on retry {attempt}/3.")
                break
            retryable = any(
                token in (upload.stderr or "").lower()
                for token in ("broken pipe", "timed out", "lost connection", "connection reset")
            )
            if attempt >= 3 or not retryable:
                break
            _log(log, f"SCP upload failed (attempt {attempt}/3). Retrying.")
            run_ssh(cfg, f"rm -f {shlex.quote(remote_tar)}", timeout=60)

        out["steps"]["upload_attempts"] = upload_attempts
        out["steps"]["upload"] = upload_attempts[-1] if upload_attempts else _command_payload(upload)
        if not upload.ok:
            out["errors"].append("SCP upload failed")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        _progress(progress, 0.66, "Applying backend bundle on AWS")
        if transport_mode:
            apply_cmd = (
                "set -euo pipefail\n"
                f"REMOTE_ROOT={shlex.quote(remote_root)}\n"
                f"REMOTE_TAR={shlex.quote(remote_tar)}\n"
                f"STAMP={shlex.quote(stamp)}\n"
                "TMP_DIR=/var/tmp/datamind_backend_unpack_${STAMP}\n"
                "mkdir -p \"$TMP_DIR\" \"$REMOTE_ROOT\"\n"
                "tar -xzf \"$REMOTE_TAR\" -C \"$TMP_DIR\"\n"
                "SRC=\"$TMP_DIR/gtfs_app_payload\"\n"
                "if [ ! -d \"$SRC\" ]; then echo 'gtfs_app_payload missing in uploaded bundle' >&2; exit 2; fi\n"
                "for f in docker-compose.yaml docker-compose.yml docker-compose.prod.yaml docker-compose.prod.yml .env .env.production; do "
                "if [ -f \"$SRC/$f\" ]; then cp -f \"$SRC/$f\" \"$REMOTE_ROOT/$f\"; fi; done\n"
                "for d in BACKEND config init scripts gtfs_data; do "
                "if [ -d \"$SRC/$d\" ]; then "
                "if [ -e \"$REMOTE_ROOT/$d\" ]; then mv \"$REMOTE_ROOT/$d\" \"$REMOTE_ROOT/$d.prev.$STAMP\"; fi; "
                "mv \"$SRC/$d\" \"$REMOTE_ROOT/$d\"; "
                "fi; "
                "done\n"
                "if [ -f \"$SRC/otp/otp.jar\" ]; then "
                "mkdir -p \"$REMOTE_ROOT/otp\"; "
                "cp -f \"$SRC/otp/otp.jar\" \"$REMOTE_ROOT/otp/otp.jar\"; "
                "fi\n"
                "if [ -d \"$SRC/otp/otp-data/routers/default\" ]; then "
                "mkdir -p \"$REMOTE_ROOT/otp/otp-data/routers\"; "
                "if [ -e \"$REMOTE_ROOT/otp/otp-data/routers/default\" ]; then "
                "mv \"$REMOTE_ROOT/otp/otp-data/routers/default\" \"$REMOTE_ROOT/otp/otp-data/routers/default.prev.$STAMP\"; "
                "fi; "
                "cp -a \"$SRC/otp/otp-data/routers/default\" \"$REMOTE_ROOT/otp/otp-data/routers/default\"; "
                "if [ ! -f \"$REMOTE_ROOT/otp/otp-data/routers/default/graph.obj\" ]; then "
                "echo 'graph.obj missing after OTP router replace' >&2; "
                "ls -la \"$REMOTE_ROOT/otp/otp-data/routers/default\" || true; "
                "exit 3; "
                "fi; "
                "fi\n"
                "rm -rf \"$TMP_DIR\" \"$REMOTE_TAR\"\n"
                "ls -ld \"$REMOTE_ROOT/BACKEND\" || true\n"
                "ls -lh \"$REMOTE_ROOT/otp/otp-data/routers/default/graph.obj\" || true\n"
            )
        else:
            apply_cmd = (
                "set -euo pipefail\n"
                f"REMOTE_ROOT={shlex.quote(remote_root)}\n"
                f"REMOTE_TAR={shlex.quote(remote_tar)}\n"
                f"STAMP={shlex.quote(stamp)}\n"
                "TMP_DIR=/var/tmp/datamind_backend_unpack_${STAMP}\n"
                "mkdir -p \"$TMP_DIR\" \"$REMOTE_ROOT\"\n"
                "tar -xzf \"$REMOTE_TAR\" -C \"$TMP_DIR\"\n"
                "if [ ! -d \"$TMP_DIR/BACKEND\" ]; then echo 'BACKEND folder missing in uploaded bundle' >&2; exit 2; fi\n"
                "if [ -e \"$REMOTE_ROOT/BACKEND\" ]; then mv \"$REMOTE_ROOT/BACKEND\" \"$REMOTE_ROOT/BACKEND.prev.$STAMP\"; fi\n"
                "mv \"$TMP_DIR/BACKEND\" \"$REMOTE_ROOT/BACKEND\"\n"
                "rm -rf \"$TMP_DIR\" \"$REMOTE_TAR\"\n"
                "ls -ld \"$REMOTE_ROOT/BACKEND\"\n"
            )
        apply_res = run_ssh(cfg, apply_cmd, timeout=1800)
        out["steps"]["apply"] = _command_payload(apply_res)
        out["steps"]["apply"]["remote_root"] = remote_root
        if not apply_res.ok:
            out["errors"].append("Failed to apply backend bundle on AWS")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        _progress(progress, 0.78, "Detecting AWS backend runtime")
        mode_info = _detect_backend_runtime(cfg, remote_root, log=log)
        if str(mode_info.get("mode") or "") == "compose":
            mode_info["services"] = list(cfg.aws_backend_deploy_services or (cfg.aws_backend_service_name,))
        out["steps"]["runtime_detect"] = mode_info
        if str(mode_info.get("mode") or "unknown") == "unknown" and not cfg.aws_backend_deploy_cmd:
            out["errors"].append("Could not detect backend runtime mode")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        _progress(progress, 0.92, "Deploying / restarting backend")
        deploy_res = _deploy_backend(cfg, mode_info, log=log)
        out["steps"]["deploy"] = _command_payload(deploy_res)
        if not deploy_res.ok:
            out["errors"].append("Backend deploy command failed")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        _progress(progress, 0.98, "Checking backend status")
        status = _backend_status_with_retry(cfg, mode_info, log=log)
        out["steps"]["backend_status"] = status
        if not status.get("ok"):
            out["errors"].append("Backend status check failed after deploy")
            out["finished_at"] = datetime.now(timezone.utc).isoformat()
            return out

        out["ok"] = True
        out["finished_at"] = datetime.now(timezone.utc).isoformat()
        _progress(progress, 1.0, "AWS backend push completed")
        return out
    finally:
        if tmp_local_dir is not None:
            shutil.rmtree(tmp_local_dir, ignore_errors=True)
