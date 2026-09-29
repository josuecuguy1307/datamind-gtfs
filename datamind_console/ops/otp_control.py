from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

from .config import OpsConfig


LogFn = Callable[[str], None]


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    command: str


def _log(log: Optional[LogFn], message: str) -> None:
    if log:
        log(message)


def _format_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(x) for x in cmd)


def run_local(
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = 300,
    env: Optional[dict[str, str]] = None,
) -> CommandResult:
    command = _format_cmd(cmd)
    try:
        completed = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env=env,
        )
    except Exception as exc:
        return CommandResult(False, 1, "", str(exc), command)

    return CommandResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        command=command,
    )


def run_local_shell(
    command: str,
    *,
    cwd: Optional[Path] = None,
    timeout: int = 300,
    env: Optional[dict[str, str]] = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env=env,
        )
    except Exception as exc:
        return CommandResult(False, 1, "", str(exc), command)

    return CommandResult(
        ok=completed.returncode == 0,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        command=command,
    )


def _ssh_target(cfg: OpsConfig) -> str:
    if cfg.aws_ssh_config_name:
        return cfg.aws_ssh_config_name
    host = cfg.aws_ssh_host or ""
    user = cfg.aws_ssh_user or ""
    return f"{user}@{host}" if user else host


def _ssh_base(cfg: OpsConfig) -> list[str]:
    cmd = ["ssh"]
    if cfg.aws_ssh_key_path:
        cmd += ["-i", cfg.aws_ssh_key_path]
    if cfg.aws_ssh_port and cfg.aws_ssh_port != 22:
        cmd += ["-p", str(cfg.aws_ssh_port)]
    cmd += [
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=20",
        "-o",
        "ServerAliveCountMax=20",
    ]
    if not cfg.ssh_strict_host_key_checking:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    return cmd


def run_ssh(cfg: OpsConfig, remote_cmd: str, *, timeout: int = 300) -> CommandResult:
    target = _ssh_target(cfg)
    cmd = _ssh_base(cfg) + [target, f"bash -lc {shlex.quote(remote_cmd)}"]
    return run_local(cmd, timeout=timeout)


def scp_upload(cfg: OpsConfig, local_file: Path, remote_path: str, *, timeout: int = 600) -> CommandResult:
    target = _ssh_target(cfg)
    cmd = ["scp"]
    if cfg.aws_ssh_key_path:
        cmd += ["-i", cfg.aws_ssh_key_path]
    if cfg.aws_ssh_port and cfg.aws_ssh_port != 22:
        cmd += ["-P", str(cfg.aws_ssh_port)]
    cmd += [
        "-o",
        "ConnectTimeout=20",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ServerAliveInterval=20",
        "-o",
        "ServerAliveCountMax=20",
    ]
    if not cfg.ssh_strict_host_key_checking:
        cmd += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    cmd += [str(local_file), f"{target}:{remote_path}"]
    return run_local(cmd, timeout=timeout)


def _detect_compose_local(cfg: OpsConfig, service_name: str) -> Optional[dict[str, Any]]:
    if cfg.local_compose_file is None:
        return None
    compose_file = cfg.local_compose_file

    for tool in (("docker", "compose"), ("docker-compose",)):
        cmd = list(tool) + ["-f", str(compose_file), "config", "--services"]
        check = run_local(cmd, timeout=30)
        if not check.ok:
            continue
        services = {line.strip() for line in check.stdout.splitlines() if line.strip()}
        if service_name not in services:
            continue

        ps_cmd = list(tool) + ["-f", str(compose_file), "ps", "--services"]
        ps = run_local(ps_cmd, timeout=30)
        running_services = {line.strip() for line in ps.stdout.splitlines() if line.strip()}
        return {
            "mode": "compose",
            "tool": " ".join(tool),
            "compose_file": str(compose_file),
            "service": service_name,
            "running": service_name in running_services,
        }
    return None


def _detect_container_local(service_name: str) -> Optional[dict[str, Any]]:
    cmd = ["docker", "ps", "-a", "--format", "{{.Names}}"]
    out = run_local(cmd, timeout=30)
    if not out.ok:
        return None

    names = [line.strip() for line in out.stdout.splitlines() if line.strip()]
    for name in names:
        if service_name.lower() in name.lower() or "otp" in name.lower():
            running = run_local(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=30)
            is_running = running.ok and running.stdout.strip().lower() == "true"
            return {"mode": "container", "container": name, "running": is_running}
    return None


def _detect_systemd_local(unit_name: str) -> Optional[dict[str, Any]]:
    active = run_local(["systemctl", "is-active", unit_name], timeout=30)
    enabled = run_local(["systemctl", "is-enabled", unit_name], timeout=30)
    if active.returncode == 4 and enabled.returncode == 1:
        return None
    if active.returncode != 0 and enabled.returncode != 0:
        return None
    return {
        "mode": "systemd",
        "unit": unit_name,
        "running": active.ok,
        "enabled": enabled.ok,
    }


def detect_local_otp_mode(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    forced = cfg.otp_local_mode
    if forced:
        _log(log, f"Using forced local OTP mode: {forced}")

    service_name = cfg.otp_local_service_name
    unit_name = cfg.otp_local_systemd_unit

    if forced in {None, "compose"}:
        compose = _detect_compose_local(cfg, service_name)
        if compose:
            _log(log, f"Detected local OTP in docker compose service '{service_name}'.")
            return compose
    if forced in {None, "container"}:
        container = _detect_container_local(service_name)
        if container:
            _log(log, f"Detected local OTP docker container '{container['container']}'.")
            return container
    if forced in {None, "systemd"}:
        service = _detect_systemd_local(unit_name)
        if service:
            _log(log, f"Detected local OTP systemd unit '{unit_name}'.")
            return service

    return {"mode": "unknown", "running": False}


def _aws_compose_candidates(cfg: OpsConfig) -> list[str]:
    candidates: list[str] = []
    if cfg.aws_transportapp_dir:
        root = cfg.aws_transportapp_dir.rstrip("/")
        candidates.append(f"{root}/docker-compose.yaml")
        candidates.append(f"{root}/docker-compose.yml")

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
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def detect_aws_otp_mode(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    forced = cfg.otp_aws_mode
    if forced:
        _log(log, f"Using forced AWS OTP mode: {forced}")

    service_name = cfg.otp_aws_service_name
    unit_name = cfg.otp_aws_systemd_unit
    if forced in {None, "compose"}:
        for compose_file in _aws_compose_candidates(cfg):
            cmd = (
                f"if [ -f {shlex.quote(compose_file)} ]; then "
                f"docker compose -f {shlex.quote(compose_file)} config --services; "
                "fi"
            )
            out = run_ssh(cfg, cmd, timeout=45)
            if not out.ok:
                continue
            services = {line.strip() for line in out.stdout.splitlines() if line.strip()}
            if service_name not in services:
                continue
            ps = run_ssh(
                cfg,
                f"docker compose -f {shlex.quote(compose_file)} ps --services",
                timeout=45,
            )
            running = service_name in {line.strip() for line in ps.stdout.splitlines() if line.strip()}
            _log(log, f"Detected AWS OTP docker compose service '{service_name}' ({compose_file}).")
            return {
                "mode": "compose",
                "service": service_name,
                "compose_file": compose_file,
                "running": running,
            }

    if forced in {None, "container"}:
        out = run_ssh(cfg, "docker ps -a --format '{{.Names}}'", timeout=45)
        if out.ok:
            names = [line.strip() for line in out.stdout.splitlines() if line.strip()]
            for name in names:
                if service_name.lower() in name.lower() or "otp" in name.lower():
                    run = run_ssh(cfg, f"docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(name)}", timeout=45)
                    running = run.ok and run.stdout.strip().lower() == "true"
                    _log(log, f"Detected AWS OTP docker container '{name}'.")
                    return {"mode": "container", "container": name, "running": running}

    if forced in {None, "systemd"}:
        out = run_ssh(cfg, f"systemctl is-active {shlex.quote(unit_name)}", timeout=45)
        if out.ok or out.returncode in {3}:
            _log(log, f"Detected AWS OTP systemd unit '{unit_name}'.")
            return {"mode": "systemd", "unit": unit_name, "running": out.ok}

    return {"mode": "unknown", "running": False}


def build_local_otp_graph(cfg: OpsConfig, mode_info: dict[str, Any], *, log: Optional[LogFn] = None) -> CommandResult:
    if cfg.otp_local_build_cmd:
        _log(log, "Running local OTP build command from OTP_LOCAL_BUILD_CMD")
        return run_local_shell(cfg.otp_local_build_cmd, timeout=3600)

    mode = str(mode_info.get("mode") or "")
    xmx = str(cfg.otp_local_build_xmx or "2G")
    if mode == "compose" and cfg.local_compose_file is not None:
        service = str(mode_info.get("service") or cfg.otp_local_service_name)
        tool = str(mode_info.get("tool") or "docker compose")
        parts = tool.split()
        stop_cmd = parts + ["-f", str(cfg.local_compose_file), "stop", service]
        run_local(stop_cmd, timeout=180)
        build_cmd = (
            f"mkdir -p /otp/otp-data/routers/default && "
            f"java -Xmx{xmx} -jar /otp/otp.jar --build --save /otp/otp-data/routers/default"
        )
        cmd = parts + [
            "-f",
            str(cfg.local_compose_file),
            "run",
            "--rm",
            "--entrypoint",
            "bash",
            service,
            "-lc",
            build_cmd,
        ]
        _log(log, "Building local OTP graph via docker compose")
        return run_local(cmd, timeout=3600)

    if mode == "container":
        container = str(mode_info.get("container") or cfg.otp_local_service_name)
        cmd = [
            "docker",
            "exec",
            container,
            "bash",
            "-lc",
            (
                f"mkdir -p /otp/otp-data/routers/default && "
                f"java -Xmx{xmx} -jar /otp/otp.jar --build --save /otp/otp-data/routers/default"
            ),
        ]
        _log(log, "Building local OTP graph via docker exec")
        return run_local(cmd, timeout=3600)

    return CommandResult(
        ok=False,
        returncode=1,
        stdout="",
        stderr="OTP graph build command is not configured for this runtime mode. Set OTP_LOCAL_BUILD_CMD.",
        command="",
    )


def restart_local_otp(cfg: OpsConfig, mode_info: dict[str, Any], *, log: Optional[LogFn] = None) -> CommandResult:
    mode = str(mode_info.get("mode") or "")

    if mode == "compose" and cfg.local_compose_file is not None:
        service = str(mode_info.get("service") or cfg.otp_local_service_name)
        tool = str(mode_info.get("tool") or "docker compose")
        parts = tool.split()
        cmd = parts + ["-f", str(cfg.local_compose_file), "up", "-d", service]
        _log(log, "Restarting local OTP docker compose service")
        return run_local(cmd, timeout=300)

    if mode == "container":
        container = str(mode_info.get("container") or cfg.otp_local_service_name)
        _log(log, f"Restarting local OTP container '{container}'")
        return run_local(["docker", "restart", container], timeout=180)

    if mode == "systemd":
        unit = str(mode_info.get("unit") or cfg.otp_local_systemd_unit)
        _log(log, f"Restarting local OTP systemd unit '{unit}'")
        return run_local(["systemctl", "restart", unit], timeout=180)

    return CommandResult(False, 1, "", "Could not determine local OTP runtime mode.", "")


def build_aws_otp_graph(cfg: OpsConfig, mode_info: dict[str, Any], *, log: Optional[LogFn] = None) -> CommandResult:
    if cfg.otp_aws_build_cmd:
        _log(log, "Running AWS OTP build command from OTP_AWS_BUILD_CMD")
        return run_ssh(cfg, cfg.otp_aws_build_cmd, timeout=3600)

    mode = str(mode_info.get("mode") or "")
    xmx = str(cfg.otp_aws_build_xmx or "2G")
    if mode == "compose":
        service = str(mode_info.get("service") or cfg.otp_aws_service_name)
        compose_file = str(mode_info.get("compose_file") or f"{(cfg.aws_transportapp_dir or '/opt/gtfs_app').rstrip('/')}/docker-compose.yaml")
        remote = (
            f"docker compose -f {shlex.quote(compose_file)} stop {shlex.quote(service)} || true; "
            f"docker compose -f {shlex.quote(compose_file)} run --rm --entrypoint bash {shlex.quote(service)} "
            f"-lc {shlex.quote(f'mkdir -p /otp/otp-data/routers/default && java -Xmx{xmx} -jar /otp/otp.jar --build --save /otp/otp-data/routers/default')}"
        )
        _log(log, "Building AWS OTP graph via docker compose")
        return run_ssh(cfg, remote, timeout=3600)

    if mode == "container":
        container = str(mode_info.get("container") or cfg.otp_aws_service_name)
        remote = (
            f"docker exec {shlex.quote(container)} bash -lc "
            f"{shlex.quote(f'mkdir -p /otp/otp-data/routers/default && java -Xmx{xmx} -jar /otp/otp.jar --build --save /otp/otp-data/routers/default')}"
        )
        _log(log, f"Building AWS OTP graph via docker exec in '{container}'")
        return run_ssh(cfg, remote, timeout=3600)

    return CommandResult(
        ok=False,
        returncode=1,
        stdout="",
        stderr="OTP graph build command is not configured for AWS runtime mode. Set OTP_AWS_BUILD_CMD.",
        command="",
    )


def restart_aws_otp(cfg: OpsConfig, mode_info: dict[str, Any], *, log: Optional[LogFn] = None) -> CommandResult:
    mode = str(mode_info.get("mode") or "")

    if mode == "compose":
        service = str(mode_info.get("service") or cfg.otp_aws_service_name)
        compose_file = str(mode_info.get("compose_file") or f"{(cfg.aws_transportapp_dir or '/opt/gtfs_app').rstrip('/')}/docker-compose.yaml")
        cmd = f"docker compose -f {shlex.quote(compose_file)} up -d {shlex.quote(service)}"
        _log(log, f"Restarting AWS OTP docker compose service '{service}'")
        return run_ssh(cfg, cmd, timeout=300)

    if mode == "container":
        container = str(mode_info.get("container") or cfg.otp_aws_service_name)
        _log(log, f"Restarting AWS OTP container '{container}'")
        return run_ssh(cfg, f"docker restart {shlex.quote(container)}", timeout=300)

    if mode == "systemd":
        unit = str(mode_info.get("unit") or cfg.otp_aws_systemd_unit)
        _log(log, f"Restarting AWS OTP systemd unit '{unit}'")
        return run_ssh(cfg, f"systemctl restart {shlex.quote(unit)}", timeout=300)

    return CommandResult(False, 1, "", "Could not determine AWS OTP runtime mode.", "")


def check_http_health(url: str, *, timeout: int = 6) -> dict[str, Any]:
    req = urlrequest.Request(url, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            code = int(resp.status)
            body = resp.read(256).decode("utf-8", errors="replace")
        return {
            "reachable": True,
            "status_code": code,
            "ok": 200 <= code < 500,
            "body_preview": body,
        }
    except urlerror.HTTPError as exc:
        return {
            "reachable": True,
            "status_code": int(exc.code),
            "ok": 200 <= int(exc.code) < 500,
            "body_preview": str(exc)[:256],
        }
    except Exception as exc:
        return {
            "reachable": False,
            "status_code": None,
            "ok": False,
            "error": str(exc),
        }


def _check_aws_http_health_via_ssh(cfg: OpsConfig, url: str, *, timeout: int = 10) -> dict[str, Any]:
    cmd = (
        "if command -v curl >/dev/null 2>&1; then "
        f"code=$(curl -sS -o /tmp/datamind_otp_health.$$ -w '%{{http_code}}' --max-time {int(timeout)} {shlex.quote(url)} || true); "
        "body=$(head -c 256 /tmp/datamind_otp_health.$$ 2>/dev/null || true); "
        "rm -f /tmp/datamind_otp_health.$$ >/dev/null 2>&1 || true; "
        "printf '%s\\n' \"$code\"; printf '%s' \"$body\"; "
        "else printf '000\\n'; fi"
    )
    res = run_ssh(cfg, cmd, timeout=max(20, timeout + 15))
    if not res.ok:
        return {
            "reachable": False,
            "status_code": None,
            "ok": False,
            "error": (res.stderr or res.stdout or "SSH health check failed").strip(),
        }

    lines = (res.stdout or "").splitlines()
    code_raw = (lines[0].strip() if lines else "") or "000"
    body_preview = "\n".join(lines[1:])[:256] if len(lines) > 1 else ""
    try:
        code = int(code_raw)
    except Exception:
        code = 0

    if code <= 0:
        return {
            "reachable": False,
            "status_code": None,
            "ok": False,
            "error": "No HTTP response code returned by remote probe",
            "body_preview": body_preview,
        }

    return {
        "reachable": True,
        "status_code": code,
        "ok": 200 <= code < 500,
        "body_preview": body_preview,
    }


def local_otp_status(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    mode_info = detect_local_otp_mode(cfg, log=log)
    mode = str(mode_info.get("mode") or "unknown")
    health: dict[str, Any] = {"ok": False, "reachable": False}

    if cfg.otp_local_health_url:
        health = check_http_health(cfg.otp_local_health_url)

    return {
        "mode": mode,
        "running": bool(mode_info.get("running")),
        "health": health,
        "detail": mode_info,
    }


def aws_otp_status(cfg: OpsConfig, *, log: Optional[LogFn] = None) -> dict[str, Any]:
    mode_info = detect_aws_otp_mode(cfg, log=log)
    health: dict[str, Any] = {"ok": False, "reachable": False}

    running = bool(mode_info.get("running"))
    mode = str(mode_info.get("mode") or "unknown")

    if not running:
        if mode == "compose":
            health = {
                "reachable": False,
                "status_code": None,
                "ok": False,
                "error": "OTP docker compose service is not running on AWS host.",
            }
        else:
            health = {
                "reachable": False,
                "status_code": None,
                "ok": False,
                "error": f"OTP service is not running (mode={mode or 'unknown'}).",
            }
    elif cfg.otp_aws_health_url:
        health = check_http_health(cfg.otp_aws_health_url)
    else:
        health = _check_aws_http_health_via_ssh(cfg, "http://127.0.0.1:8080/otp/routers/default")

    return {
        "mode": mode,
        "running": running,
        "health": health,
        "detail": mode_info,
    }
