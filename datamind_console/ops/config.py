from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote as urlquote, urlparse


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _bool_from_raw(raw: Optional[str], default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_mode(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    v = raw.strip().lower()
    if v in {"compose", "docker_compose", "docker-compose"}:
        return "compose"
    if v in {"docker", "container"}:
        return "container"
    if v in {"systemd", "service"}:
        return "systemd"
    return v


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return values

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value

    return values


def _build_pg_dsn(
    *,
    host: Optional[str],
    port: Optional[str],
    dbname: Optional[str],
    user: Optional[str],
    password: Optional[str],
) -> Optional[str]:
    if not host or not port or not dbname or not user:
        return None
    auth = urlquote(user, safe="")
    if password:
        auth += ":" + urlquote(password, safe="")
    return f"postgresql://{auth}@{host}:{port}/{urlquote(dbname, safe='')}"


def _dsn_db_name(dsn: Optional[str]) -> Optional[str]:
    if not dsn:
        return None
    try:
        parsed = urlparse(dsn)
        name = (parsed.path or "").lstrip("/")
        return name or None
    except Exception:
        return None


def _discover_transportapp_root(repo_root: Path) -> Optional[Path]:
    explicit = _env("GTFS_APP_ROOT")
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if p.exists() and p.is_dir():
            return p

    candidates = [
        repo_root.parent / "gtfs_app",
        repo_root.parent / "transportapp",
        repo_root.parent / "TRANSPORTAPP",
        repo_root / "gtfs_app",
        Path.cwd() / "gtfs_app",
    ]
    for candidate in candidates:
        try:
            p = candidate.expanduser().resolve()
        except Exception:
            continue
        if p.exists() and p.is_dir():
            return p
    return None


@dataclass(frozen=True)
class OpsConfig:
    repo_root: Path
    transportapp_root: Optional[Path]
    local_compose_file: Optional[Path]
    local_db_source: str

    local_db_dsn: Optional[str]
    aws_db_dsn: Optional[str]

    aws_ssh_host: Optional[str]
    aws_ssh_user: Optional[str]
    aws_ssh_key_path: Optional[str]
    aws_ssh_config_name: Optional[str]
    aws_ssh_port: int
    ssh_strict_host_key_checking: bool

    otp_local_mode: Optional[str]
    otp_aws_mode: Optional[str]
    otp_local_service_name: str
    otp_aws_service_name: str
    otp_local_systemd_unit: str
    otp_aws_systemd_unit: str

    otp_local_health_url: Optional[str]
    otp_aws_health_url: Optional[str]
    otp_local_build_cmd: Optional[str]
    otp_aws_build_cmd: Optional[str]
    otp_local_build_xmx: str
    otp_aws_build_xmx: str
    aws_allow_remote_otp_build: bool

    local_gtfs_incoming_dir: Path
    local_gtfs_extract_dir: Optional[Path]
    aws_gtfs_incoming_dir: str
    aws_gtfs_extract_dir: Optional[str]

    local_db_import_cmd: Optional[str]
    aws_db_import_cmd: Optional[str]
    aws_gtfs_apply_cmd: Optional[str]
    local_db_refresh_sql: Optional[str]
    aws_db_refresh_cmd: Optional[str]
    aws_db_status_cmd: Optional[str]

    aws_transportapp_dir: Optional[str]
    backend_push_local_dir: Optional[Path]
    aws_backend_remote_dir: Optional[str]
    aws_backend_deploy_cmd: Optional[str]
    aws_backend_service_name: str
    aws_backend_systemd_unit: str
    aws_backend_deploy_services: tuple[str, ...]


def load_ops_config() -> OpsConfig:
    repo_root = Path(__file__).resolve().parents[2]
    console_env = _read_dotenv(repo_root / ".env")

    def _ops_env(name: str, default: Optional[str] = None) -> Optional[str]:
        runtime = _env(name)
        if runtime is not None:
            return runtime
        file_value = (console_env.get(name) or "").strip()
        return file_value if file_value else default

    transport_root = _discover_transportapp_root(repo_root)
    transport_env = _read_dotenv(transport_root / ".env") if transport_root else {}

    local_compose_file: Optional[Path] = None
    if transport_root is not None:
        compose_yaml = transport_root / "docker-compose.yaml"
        compose_yml = transport_root / "docker-compose.yml"
        if compose_yaml.exists():
            local_compose_file = compose_yaml
        elif compose_yml.exists():
            local_compose_file = compose_yml

    local_incoming = Path(
        _ops_env("LOCAL_GTFS_INCOMING_DIR", str(repo_root / "data" / "gtfs" / "incoming"))
        or str(repo_root / "data" / "gtfs" / "incoming")
    ).expanduser()

    local_extract = _ops_env("LOCAL_GTFS_EXTRACT_DIR")
    if local_extract:
        local_extract_dir: Optional[Path] = Path(local_extract).expanduser()
    elif transport_root is not None:
        otp_router_dir = transport_root / "otp" / "otp-data" / "routers" / "default"
        gtfs_data_dir = transport_root / "gtfs_data"
        if otp_router_dir.exists() or not gtfs_data_dir.exists():
            local_extract_dir = otp_router_dir
        else:
            local_extract_dir = gtfs_data_dir
    else:
        local_extract_dir = None

    try:
        local_otp_port = int(_ops_env("OTP_LOCAL_PORT", "8080") or "8080")
    except Exception:
        local_otp_port = 8080

    local_db_dsn_env = (
        _ops_env("LOCAL_GTFS_DB_DSN")
        or _ops_env("LOCAL_TRANSIT_DB_DSN")
        or _ops_env("LOCAL_DB_DSN")
        or _ops_env("DB_DSN_LOCAL")
        or _ops_env("DB_DSN")
    )
    local_db_source = "env"

    transport_db_dsn = _build_pg_dsn(
        host="127.0.0.1",
        port=transport_env.get("PG_HOST_PORT", "5433"),
        dbname=transport_env.get("PGDATABASE"),
        user=transport_env.get("PGUSER"),
        password=transport_env.get("PGPASSWORD"),
    )
    selected_dsn = local_db_dsn_env
    selected_db_name = (_dsn_db_name(selected_dsn) or "").lower()
    if not selected_dsn and transport_db_dsn:
        selected_dsn = transport_db_dsn
        local_db_source = "gtfs_app:.env"
    elif selected_db_name and selected_db_name not in {"gtfs_app_db"} and transport_db_dsn:
        selected_dsn = transport_db_dsn
        local_db_source = "gtfs_app:.env"

    try:
        aws_ssh_port = int(_ops_env("AWS_SSH_PORT", "22") or "22")
    except Exception:
        aws_ssh_port = 22

    backend_local_raw = _ops_env("BACKEND_PUSH_LOCAL_DIR")
    backend_local_dir: Optional[Path]
    if backend_local_raw:
        backend_local_dir = Path(backend_local_raw).expanduser()
    elif transport_root is not None:
        backend_local_dir = transport_root
    else:
        backend_local_dir = None

    backend_services_raw = _ops_env("AWS_BACKEND_DEPLOY_SERVICES", "backend,otp") or "backend,otp"
    backend_services = tuple(x.strip() for x in backend_services_raw.split(",") if x.strip())
    if not backend_services:
        backend_services = ("backend",)

    return OpsConfig(
        repo_root=repo_root,
        transportapp_root=transport_root,
        local_compose_file=local_compose_file,
        local_db_source=local_db_source,
        local_db_dsn=selected_dsn,
        aws_db_dsn=_ops_env("AWS_DB_DSN"),
        aws_ssh_host=_ops_env("AWS_SSH_HOST"),
        aws_ssh_user=_ops_env("AWS_SSH_USER"),
        aws_ssh_key_path=_ops_env("AWS_SSH_KEY_PATH"),
        aws_ssh_config_name=_ops_env("AWS_SSH_CONFIG_NAME"),
        aws_ssh_port=aws_ssh_port,
        ssh_strict_host_key_checking=_bool_from_raw(_ops_env("SSH_STRICT_HOST_KEY_CHECKING"), False),
        otp_local_mode=_normalize_mode(_ops_env("OTP_LOCAL_MODE")),
        otp_aws_mode=_normalize_mode(_ops_env("OTP_AWS_MODE")),
        otp_local_service_name=_ops_env("OTP_LOCAL_SERVICE_NAME", "otp") or "otp",
        otp_aws_service_name=_ops_env("OTP_AWS_SERVICE_NAME", "otp") or "otp",
        otp_local_systemd_unit=_ops_env("OTP_LOCAL_SYSTEMD_UNIT", "otp") or "otp",
        otp_aws_systemd_unit=_ops_env("OTP_AWS_SYSTEMD_UNIT", "otp") or "otp",
        otp_local_health_url=(_ops_env("OTP_LOCAL_HEALTH_URL") or f"http://127.0.0.1:{local_otp_port}/otp/routers/default"),
        otp_aws_health_url=_ops_env("OTP_AWS_HEALTH_URL"),
        otp_local_build_cmd=_ops_env("OTP_LOCAL_BUILD_CMD"),
        otp_aws_build_cmd=_ops_env("OTP_AWS_BUILD_CMD"),
        otp_local_build_xmx=_ops_env("OTP_LOCAL_BUILD_XMX", "2G") or "2G",
        otp_aws_build_xmx=_ops_env("OTP_AWS_BUILD_XMX", "2G") or "2G",
        aws_allow_remote_otp_build=_bool_from_raw(_ops_env("AWS_ALLOW_REMOTE_OTP_BUILD"), False),
        local_gtfs_incoming_dir=local_incoming,
        local_gtfs_extract_dir=local_extract_dir,
        aws_gtfs_incoming_dir=_ops_env("AWS_GTFS_INCOMING_DIR", "/data/gtfs/incoming") or "/data/gtfs/incoming",
        aws_gtfs_extract_dir=_ops_env("AWS_GTFS_EXTRACT_DIR"),
        local_db_import_cmd=_ops_env("LOCAL_GTFS_IMPORT_CMD"),
        aws_db_import_cmd=_ops_env("AWS_DB_IMPORT_CMD"),
        aws_gtfs_apply_cmd=_ops_env("AWS_GTFS_APPLY_CMD"),
        local_db_refresh_sql=_ops_env("LOCAL_DB_REFRESH_SQL", ""),
        aws_db_refresh_cmd=_ops_env("AWS_DB_REFRESH_CMD"),
        aws_db_status_cmd=_ops_env("AWS_DB_STATUS_CMD"),
        aws_transportapp_dir=_ops_env("AWS_TRANSPORTAPP_DIR", "/opt/gtfs_app"),
        backend_push_local_dir=backend_local_dir,
        aws_backend_remote_dir=_ops_env("AWS_BACKEND_REMOTE_DIR"),
        aws_backend_deploy_cmd=_ops_env("AWS_BACKEND_DEPLOY_CMD"),
        aws_backend_service_name=_ops_env("AWS_BACKEND_SERVICE_NAME", "backend") or "backend",
        aws_backend_systemd_unit=_ops_env("AWS_BACKEND_SYSTEMD_UNIT", "backend") or "backend",
        aws_backend_deploy_services=backend_services,
    )


def _missing_ssh_fields(cfg: OpsConfig) -> list[str]:
    missing: list[str] = []
    if cfg.aws_ssh_config_name:
        return missing
    if not cfg.aws_ssh_host:
        missing.append("AWS_SSH_HOST")
    if not cfg.aws_ssh_user:
        missing.append("AWS_SSH_USER")
    if not cfg.aws_ssh_key_path:
        missing.append("AWS_SSH_KEY_PATH or AWS_SSH_CONFIG_NAME")
    return missing


def missing_for_local_publish(cfg: OpsConfig) -> list[str]:
    missing: list[str] = []
    if not cfg.local_db_dsn:
        missing.append("LOCAL_GTFS_DB_DSN or LOCAL_DB_DSN")
    if cfg.local_gtfs_extract_dir is None:
        missing.append("LOCAL_GTFS_EXTRACT_DIR")
    return missing


def missing_for_aws_publish(cfg: OpsConfig) -> list[str]:
    missing = _missing_ssh_fields(cfg)
    has_sql_fallback = bool((cfg.local_db_refresh_sql or "").strip())
    if not cfg.aws_db_refresh_cmd and not cfg.aws_db_dsn and not has_sql_fallback:
        missing.append("AWS_DB_REFRESH_CMD or AWS_DB_DSN")
    return missing


def missing_for_local_status(cfg: OpsConfig) -> list[str]:
    missing: list[str] = []
    if not cfg.local_db_dsn:
        missing.append("LOCAL_GTFS_DB_DSN or LOCAL_DB_DSN")
    return missing


def missing_for_aws_status(cfg: OpsConfig) -> list[str]:
    return _missing_ssh_fields(cfg)


def missing_for_aws_backend_push(cfg: OpsConfig) -> list[str]:
    missing = _missing_ssh_fields(cfg)
    if cfg.backend_push_local_dir is None:
        missing.append("BACKEND_PUSH_LOCAL_DIR or GTFS_APP_ROOT")
    elif not cfg.backend_push_local_dir.exists():
        missing.append(f"BACKEND_PUSH_LOCAL_DIR missing: {cfg.backend_push_local_dir}")
    return missing
