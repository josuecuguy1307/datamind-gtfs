from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
import re
import zipfile

import streamlit as st

from datamind_console.ops.backend_push import run_aws_backend_push
from datamind_console.ops.aws_publish import check_aws_status, run_all_aws_server_api_checks, run_aws_publish
from datamind_console.ops.config import (
    OpsConfig,
    load_ops_config,
    missing_for_aws_backend_push,
    missing_for_aws_publish,
    missing_for_aws_status,
    missing_for_local_publish,
    missing_for_local_status,
)
from datamind_console.ops.gtfs_validate import validate_gtfs_zip
from datamind_console.ops.local_api_status import run_all_local_server_api_checks
from datamind_console.ops.local_publish import check_local_status, run_local_publish
from datamind_console.services.workspace_context_service import active_work


LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("ops.gtfs.last_file_path", "")
    ss.setdefault("ops.gtfs.last_checksum", "")
    ss.setdefault("ops.gtfs.last_validation", {})
    ss.setdefault("ops.local.last_run", "")
    ss.setdefault("ops.local.api.last_run", "")
    ss.setdefault("ops.aws.last_run", "")
    ss.setdefault("ops.aws.api.last_run", "")
    ss.setdefault("ops.backend.last_run", "")
    ss.setdefault("ops.logs.local", [])
    ss.setdefault("ops.logs.aws", [])
    ss.setdefault("ops.status", {})


def _work_state_key(base: str) -> str:
    """Namespace transient GTFS references by the explicitly selected work."""
    work = active_work(st.session_state)
    work_id = str((work or {}).get("id") or "")
    return f"{base}.{work_id}" if work_id else base


def _work_incoming_dir(cfg: OpsConfig) -> Path:
    """Avoid overwriting another selected work's uploaded package."""
    work = active_work(st.session_state)
    work_id = str((work or {}).get("id") or "")
    if not work_id:
        return cfg.local_gtfs_incoming_dir
    return cfg.local_gtfs_incoming_dir / "session-work" / work_id


def _append_log(scope: str, msg: str) -> None:
    key = "ops.logs.local" if scope == "local" else "ops.logs.aws"
    logs = list(st.session_state.get(key) or [])
    logs.append(f"[{_now_iso()}] {msg}")
    st.session_state[key] = logs[-800:]


def _sanitize_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "gtfs")
    return safe.strip("_") or "gtfs"


def _save_uploaded_gtfs(uploaded: Any, cfg: OpsConfig) -> Path:
    incoming_dir = _work_incoming_dir(cfg)
    incoming_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = _sanitize_name(str(uploaded.name or "gtfs.zip"))
    if not safe_name.lower().endswith(".zip"):
        safe_name = f"{safe_name}.zip"

    out_path = incoming_dir / f"{stamp}_{safe_name}"
    out_path.write_bytes(uploaded.getvalue())

    # A selected upload is only a candidate.  Keep the last known-good file
    # until validation succeeds, so an invalid ZIP cannot replace it.
    st.session_state[_work_state_key("ops.gtfs.candidate_file_path")] = str(out_path)
    _append_log("local", f"Saved uploaded GTFS zip to {out_path}")
    _append_log("aws", f"Saved uploaded GTFS zip to {out_path}")
    return out_path


def _resolve_gtfs_file(uploaded: Any, cfg: OpsConfig) -> Optional[Path]:
    if uploaded is not None:
        return _save_uploaded_gtfs(uploaded, cfg)

    raw = str(st.session_state.get(_work_state_key("ops.gtfs.last_file_path")) or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.exists() or not p.is_file():
        return None
    return p


_GTFS_BUNDLE_FILES = (
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
)


def _auto_pack_gtfs_from_dir(source_dir: Path, cfg: OpsConfig) -> Optional[Path]:
    if not source_dir.exists() or not source_dir.is_dir():
        return None

    required = {"stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
    found = {name for name in _GTFS_BUNDLE_FILES if (source_dir / name).is_file()}
    if not required.issubset(found):
        return None
    if "calendar.txt" not in found and "calendar_dates.txt" not in found:
        return None

    cfg.local_gtfs_incoming_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = cfg.local_gtfs_incoming_dir / f"{stamp}_auto_bundle_gtfs.zip"
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(found):
            zf.write(source_dir / name, arcname=name)
    return out_path


def _resolve_gtfs_for_aws_full_deploy(uploaded: Any, cfg: OpsConfig) -> tuple[Optional[Path], str]:
    direct = _resolve_gtfs_file(uploaded, cfg)
    if direct is not None:
        return direct, "uploaded_or_cached"

    zip_candidates: list[Path] = []
    dir_candidates: list[Path] = []

    if cfg.local_gtfs_extract_dir is not None:
        dir_candidates.append(cfg.local_gtfs_extract_dir)
        zip_candidates.append(cfg.local_gtfs_extract_dir / "latest_upload_gtfs.zip")

    if cfg.transportapp_root is not None:
        root = cfg.transportapp_root
        otp_router = root / "otp" / "otp-data" / "routers" / "default"
        gtfs_data = root / "gtfs_data"
        dir_candidates.extend([otp_router, gtfs_data])
        zip_candidates.extend(
            [
                otp_router / "latest_upload_gtfs.zip",
                gtfs_data / "latest_upload_gtfs.zip",
            ]
        )

    seen_zip: set[str] = set()
    for candidate in zip_candidates:
        key = str(candidate)
        if key in seen_zip:
            continue
        seen_zip.add(key)
        if candidate.exists() and candidate.is_file():
            st.session_state[_work_state_key("ops.gtfs.last_file_path")] = str(candidate)
            return candidate, "bundle_zip"

    seen_dir: set[str] = set()
    for candidate in dir_candidates:
        key = str(candidate)
        if key in seen_dir:
            continue
        seen_dir.add(key)
        packed = _auto_pack_gtfs_from_dir(candidate, cfg)
        if packed is not None:
            st.session_state[_work_state_key("ops.gtfs.last_file_path")] = str(packed)
            return packed, "packed_from_bundle_dir"

    return None, "missing"


def _ensure_validated_gtfs(path: Path) -> bool:
    validation = validate_gtfs_zip(path)
    st.session_state[_work_state_key("ops.gtfs.last_validation")] = validation

    if validation.get("ok"):
        st.session_state[_work_state_key("ops.gtfs.last_checksum")] = str(validation.get("checksum_sha256") or "")
        st.session_state[_work_state_key("ops.gtfs.last_file_path")] = str(path)
        _append_log("local", f"Validation OK. checksum={validation.get('checksum_sha256')}")
        _append_log("aws", f"Validation OK. checksum={validation.get('checksum_sha256')}")
        return True

    errors = validation.get("errors") or []
    _append_log("local", f"Validation failed: {errors}")
    _append_log("aws", f"Validation failed: {errors}")
    return False


def _run_action(
    *,
    scope: str,
    label: str,
    action: Callable[[LogFn, ProgressFn], dict[str, Any]],
) -> dict[str, Any]:
    status_box = st.empty()
    progress_bar = st.progress(0)
    live_log = st.empty()

    log_key = "ops.logs.local" if scope == "local" else "ops.logs.aws"

    def log(msg: str) -> None:
        _append_log(scope, msg)
        lines = list(st.session_state.get(log_key) or [])
        live_log.code("\n".join(lines[-40:]) or "", language="text")

    def progress(frac: float, msg: str) -> None:
        progress_bar.progress(int(max(0.0, min(1.0, frac)) * 100))
        status_box.info(f"{label}: {msg}")

    try:
        result = action(log, progress)
    except Exception as exc:
        log(f"{label} failed: {exc}")
        result = {"ok": False, "error": str(exc)}

    ok = bool(result.get("ok"))
    if ok:
        status_box.success(f"{label}: completed")
        progress_bar.progress(100)
    else:
        status_box.error(f"{label}: failed")

    return result


def _render_missing(title: str, missing: list[str]) -> None:
    if not missing:
        return
    st.warning(f"{title}: Not connected. Fix config: {', '.join(missing)}")


def _status_connected(kind: str, payload: Any) -> bool:
    data = payload if isinstance(payload, dict) else {}
    if kind in {"local_db", "aws_db"}:
        return bool(data.get("ok"))
    if kind in {"local_otp", "aws_otp"}:
        if bool(data.get("running")):
            return True
        health = data.get("health") if isinstance(data.get("health"), dict) else {}
        return bool(health.get("reachable"))
    return False


def _render_connectivity_tags(*, local_db: Any, local_otp: Any, aws_otp: Any, aws_db: Any) -> None:
    has_any_status = any(
        isinstance(item, dict) and bool(item)
        for item in (local_db, local_otp, aws_otp, aws_db)
    )
    if not has_any_status:
        st.info("No status snapshot yet. Run `Check LOCAL status` and/or `Check AWS status`.")
        return

    tags = [
        ("Local DB", _status_connected("local_db", local_db)),
        ("Local OTP", _status_connected("local_otp", local_otp)),
        ("AWS OTP", _status_connected("aws_otp", aws_otp)),
        ("AWS DB", _status_connected("aws_db", aws_db)),
    ]
    cols = st.columns(4)
    for idx, (name, ok) in enumerate(tags):
        with cols[idx]:
            if ok:
                st.success(f"{name}: Connected")
            else:
                st.warning(f"{name}: Not connected")

    if any(not ok for _, ok in tags):
        st.warning("Connectivity issue detected. Fix config/host/service and run status checks again.")


def _short_json(data: Any) -> Any:
    if isinstance(data, dict):
        return data
    if data is None:
        return {}
    return {"value": data}


def _local_api_guard_ok(status: Any) -> bool:
    payload = status if isinstance(status, dict) else {}
    local_api = payload.get("local_api_checks")
    return isinstance(local_api, dict) and bool(local_api.get("ok"))


def _merge_missing(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for item in group:
            key = str(item).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def _render_aws_deploy_checklist(cfg: OpsConfig) -> None:
    remote_root = (cfg.aws_backend_remote_dir or cfg.aws_transportapp_dir or "/opt/gtfs_app").rstrip("/")
    compose_file = f"{remote_root}/docker-compose.yaml"

    st.markdown("#### Safe AWS Deploy Checklist")
    with st.expander("Show checklist (stop/remove, replace, keep, health checks)", expanded=False):
        st.markdown(
            "\n".join(
                [
                    "1. Stop only app containers before deploy (`backend`, `otp`), keep data services running when possible.",
                    "2. Replace only app/runtime folders (`BACKEND`, `config`, `init`, `scripts`, `otp/otp.jar`, `otp/otp-data/routers/default`, optional `gtfs_data/osm_data`).",
                    "3. Do **not** delete Postgres/Redis containers, data directories, or named volumes (`pgdata`, `redisdata`).",
                    "4. Start app containers again and run post-deploy checks locally on EC2 and on public endpoints.",
                ]
            )
        )
        st.code(
            "\n".join(
                [
                    f"docker compose -f {compose_file} stop backend otp",
                    f"docker compose -f {compose_file} up -d backend otp",
                    "",
                    "# OTP local check on EC2",
                    "curl -fsS http://127.0.0.1:8080/otp/routers/default/index/stops | head -c 200",
                    "",
                    "# Backend local checks on EC2",
                    "curl -fsS http://127.0.0.1:3000/health",
                    "curl -fsS 'http://127.0.0.1:3000/api/routes?fromLat=-0.21&fromLon=-78.50&toLat=-0.20&toLon=-78.49&profile=walk&forceFresh=true'",
                ]
            ),
            language="bash",
        )


def render_gtfs_ops_widget() -> None:
    _init_state()
    ss = st.session_state
    cfg = load_ops_config()

    st.subheader("GTFS Ops (Local + AWS)")
    st.caption(
        "Upload one GTFS ZIP, publish locally, run full AWS deploy (runtime + GTFS + DB + OTP), "
        "and track status/logs from one panel. "
        "For AWS full deploy, GTFS upload is optional if your local bundle is already updated."
    )
    work = active_work(ss)
    if work:
        st.info(f"Active GTFS work: {work['name']}. Uploads are isolated from other selected works.")
    else:
        st.info("No regional profile is required to validate a GTFS ZIP. Create/select a work only when you need to retain an input or results destination.")

    with st.expander("Detected paths/config", expanded=False):
        st.write(
            {
                "transportapp_root": str(cfg.transportapp_root) if cfg.transportapp_root else None,
                "local_compose_file": str(cfg.local_compose_file) if cfg.local_compose_file else None,
                "local_gtfs_incoming_dir": str(cfg.local_gtfs_incoming_dir),
                "local_gtfs_extract_dir": str(cfg.local_gtfs_extract_dir) if cfg.local_gtfs_extract_dir else None,
                "local_db_dsn_set": bool(cfg.local_db_dsn),
                "local_db_source": cfg.local_db_source,
                "local_db_import_cmd_set": bool(cfg.local_db_import_cmd),
                "aws_gtfs_incoming_dir": cfg.aws_gtfs_incoming_dir,
                "aws_gtfs_extract_dir": cfg.aws_gtfs_extract_dir,
                "aws_ssh_host": cfg.aws_ssh_host,
                "aws_ssh_user": cfg.aws_ssh_user,
                "aws_ssh_key_path": cfg.aws_ssh_key_path,
                "aws_ssh_config_name": cfg.aws_ssh_config_name,
                "aws_db_dsn_set": bool(cfg.aws_db_dsn),
                "aws_db_refresh_cmd_set": bool(cfg.aws_db_refresh_cmd),
                "aws_db_import_cmd_set": bool(cfg.aws_db_import_cmd),
                "backend_push_local_dir": str(cfg.backend_push_local_dir) if cfg.backend_push_local_dir else None,
                "aws_backend_remote_dir": cfg.aws_backend_remote_dir,
                "aws_backend_deploy_cmd_set": bool(cfg.aws_backend_deploy_cmd),
                "aws_backend_service_name": cfg.aws_backend_service_name,
                "aws_backend_systemd_unit": cfg.aws_backend_systemd_unit,
                "aws_backend_deploy_services": list(cfg.aws_backend_deploy_services),
                "aws_gtfs_apply_cmd_set": bool(cfg.aws_gtfs_apply_cmd),
                "aws_allow_remote_otp_build": cfg.aws_allow_remote_otp_build,
                "otp_local_mode": cfg.otp_local_mode,
                "otp_aws_mode": cfg.otp_aws_mode,
            }
        )

    top_missing_local = missing_for_local_publish(cfg)
    top_missing_aws = missing_for_aws_publish(cfg)
    top_missing_backend = missing_for_aws_backend_push(cfg)
    top_missing_aws_full = _merge_missing(top_missing_aws, top_missing_backend)
    if top_missing_local or top_missing_aws_full:
        st.info(
            "Missing config detected. "
            f"Local: {', '.join(top_missing_local) if top_missing_local else 'OK'} | "
            f"AWS full deploy: {', '.join(top_missing_aws_full) if top_missing_aws_full else 'OK'}"
        )

    upload_key = _work_state_key("ops.gtfs.upload")
    uploaded = st.file_uploader("GTFS zip (.zip)", type=["zip"], key=upload_key)
    if uploaded is not None:
        st.caption(f"Selected upload: `{uploaded.name}` ({uploaded.size} bytes)")

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)

    with c1:
        do_validate = st.button("Validate GTFS", use_container_width=True, key="ops.gtfs.validate")
    with c2:
        do_local = st.button("Run LOCAL publish", use_container_width=True, key="ops.gtfs.local.publish")
    with c3:
        do_aws_full = st.button("Run AWS full deploy", use_container_width=True, key="ops.gtfs.aws.full")
    with c4:
        do_local_status = st.button("Check LOCAL status", use_container_width=True, key="ops.gtfs.local.status")
    with c5:
        do_aws_status = st.button("Check AWS status", use_container_width=True, key="ops.gtfs.aws.status")
    with c6:
        do_aws_api_checks = st.button("Run ALL AWS APIs", use_container_width=True, key="ops.gtfs.aws.apis")
    with c7:
        do_local_api_checks = st.button("Run ALL APIs", use_container_width=True, key="ops.gtfs.local.apis")

    confirm_aws_full = st.checkbox(
        "Are you sure? (required before AWS full deploy)",
        key="ops.gtfs.aws.full.confirm",
        value=False,
    )
    allow_without_local_api = st.checkbox(
        "Allow AWS actions without LOCAL API check pass (not recommended)",
        key="ops.gtfs.aws.allow_without_local_api",
        value=False,
    )

    _render_aws_deploy_checklist(cfg)

    if do_validate:
        path = _resolve_gtfs_file(uploaded, cfg)
        if path is None:
            st.warning("Upload a GTFS zip first or reuse the last uploaded file.")
        else:
            result = _run_action(
                scope="local",
                label="Validate GTFS",
                action=lambda log, progress: _validate_action(path, log, progress),
            )
            ss.setdefault("ops.status", {})["gtfs_validation"] = result

    if do_local:
        missing = missing_for_local_publish(cfg)
        if missing:
            _render_missing("LOCAL publish", missing)
        else:
            path = _resolve_gtfs_file(uploaded, cfg)
            if path is None:
                st.warning("Upload a GTFS zip first or reuse the last uploaded file.")
            elif not _ensure_validated_gtfs(path):
                st.error("Validation failed. Fix GTFS zip before local publish.")
            else:
                result = _run_action(
                    scope="local",
                    label="LOCAL publish",
                    action=lambda log, progress: run_local_publish(cfg, path, log=log, progress=progress),
                )
                if result.get("ok"):
                    ss["ops.local.last_run"] = _now_iso()
                ss.setdefault("ops.status", {})["local_publish"] = result

    if do_aws_full:
        if not confirm_aws_full:
            st.warning("Check the confirmation box before running AWS full deploy.")
        elif (not allow_without_local_api) and (not _local_api_guard_ok(ss.get("ops.status") or {})):
            st.warning("Run `ALL APIs` first and get `STATUS_AWESOME` before AWS full deploy.")
        else:
            missing = _merge_missing(missing_for_aws_publish(cfg), missing_for_aws_backend_push(cfg))
            if missing:
                _render_missing("AWS full deploy", missing)
            else:
                path, source_kind = _resolve_gtfs_for_aws_full_deploy(uploaded, cfg)
                if path is None:
                    st.error(
                        "AWS full deploy could not find GTFS in local bundle. "
                        "Run LOCAL publish first or upload one GTFS zip."
                    )
                elif not _ensure_validated_gtfs(path):
                    st.error("Validation failed. Fix GTFS zip before AWS full deploy.")
                else:
                    if source_kind != "uploaded_or_cached":
                        st.caption(f"AWS full deploy using auto-detected local GTFS bundle: `{path}`")
                    result = _run_action(
                        scope="aws",
                        label="AWS full deploy",
                        action=lambda log, progress: _aws_full_deploy_action(cfg, path, log, progress),
                    )
                    ss["ops.aws.last_run"] = _now_iso()
                    if result.get("ok"):
                        ss["ops.backend.last_run"] = _now_iso()
                    state = ss.setdefault("ops.status", {})
                    state["aws_full_deploy"] = result
                    if isinstance(result.get("aws_otp"), dict):
                        state["aws_otp"] = result.get("aws_otp")
                    if isinstance(result.get("aws_db"), dict):
                        state["aws_db"] = result.get("aws_db")
                    if isinstance(result.get("aws_backend"), dict):
                        state["aws_backend"] = result.get("aws_backend")

    if do_local_status:
        missing = missing_for_local_status(cfg)
        if missing:
            _render_missing("LOCAL status", missing)
        else:
            result = _run_action(
                scope="local",
                label="Check LOCAL status",
                action=lambda log, progress: _local_status_action(cfg, log, progress),
            )
            ss.setdefault("ops.status", {}).update(result)

    if do_aws_status:
        missing = missing_for_aws_status(cfg)
        if missing:
            _render_missing("AWS status", missing)
        else:
            result = _run_action(
                scope="aws",
                label="Check AWS status",
                action=lambda log, progress: _aws_status_action(cfg, log, progress),
            )
            ss.setdefault("ops.status", {}).update(result)

    if do_local_api_checks:
        result = _run_action(
            scope="local",
            label="Run ALL APIs",
            action=lambda log, progress: _local_api_checks_action(cfg, log, progress),
        )
        ss["ops.local.api.last_run"] = _now_iso()
        ss.setdefault("ops.status", {})["local_api_checks"] = result

    if do_aws_api_checks:
        missing = missing_for_aws_status(cfg)
        if missing:
            _render_missing("ALL AWS APIs", missing)
        else:
            result = _run_action(
                scope="aws",
                label="Run ALL AWS APIs",
                action=lambda log, progress: _aws_api_checks_action(cfg, log, progress),
            )
            ss["ops.aws.api.last_run"] = _now_iso()
            ss.setdefault("ops.status", {})["aws_api_checks"] = result

    st.markdown("#### Status")
    st.caption(
        "Last local publish: "
        f"`{ss.get('ops.local.last_run') or '-'}` | "
        f"Last local API check: `{ss.get('ops.local.api.last_run') or '-'}` | "
        f"Last AWS API check: `{ss.get('ops.aws.api.last_run') or '-'}` | "
        f"Last AWS full deploy: `{ss.get('ops.aws.last_run') or '-'}`"
    )

    status = ss.get("ops.status") or {}
    s1, s2 = st.columns(2)
    with s1:
        st.markdown("**Local DB status**")
        local_db = status.get("local_db") or status.get("local_publish", {}).get("steps", {}).get("status", {}).get("local_db")
        st.json(_short_json(local_db))

        st.markdown("**Local OTP status**")
        local_otp = status.get("local_otp") or status.get("local_publish", {}).get("steps", {}).get("status", {}).get("local_otp")
        st.json(_short_json(local_otp))

        st.markdown("**Local API preflight**")
        local_api = status.get("local_api_checks")
        if isinstance(local_api, dict) and local_api:
            if local_api.get("ok"):
                st.success("STATUS_AWESOME: all API checks passed. One failure would set run-all to false.")
            else:
                st.warning("Run ALL APIs is false. At least one endpoint failed; review `failed_checks` and `checks` before AWS actions.")
            st.json(_short_json(local_api))
        else:
            st.info("Run `ALL APIs` before AWS full deploy.")

    with s2:
        st.markdown("**AWS full deploy**")
        aws_full = status.get("aws_full_deploy")
        st.json(_short_json(aws_full))

        st.markdown("**AWS OTP status**")
        aws_otp = (
            status.get("aws_otp")
            or status.get("aws_status", {}).get("aws_otp")
            or status.get("aws_full_deploy", {}).get("aws_otp")
            or status.get("aws_full_deploy", {}).get("steps", {}).get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_otp")
            or status.get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_otp")
        )
        st.json(_short_json(aws_otp))

        st.markdown("**AWS DB status**")
        aws_db = (
            status.get("aws_db")
            or status.get("aws_status", {}).get("aws_db")
            or status.get("aws_full_deploy", {}).get("aws_db")
            or status.get("aws_full_deploy", {}).get("steps", {}).get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_db")
            or status.get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_db")
        )
        st.json(_short_json(aws_db))

        st.markdown("**AWS runtime deploy**")
        aws_backend = (
            status.get("aws_backend")
            or status.get("aws_full_deploy", {}).get("aws_backend")
            or status.get("aws_full_deploy", {}).get("steps", {}).get("aws_runtime", {}).get("steps", {}).get("backend_status")
            or status.get("aws_backend_push", {}).get("steps", {}).get("backend_status")
        )
        st.json(_short_json(aws_backend))

        st.markdown("**AWS API preflight**")
        aws_api = status.get("aws_api_checks")
        if isinstance(aws_api, dict) and aws_api:
            if aws_api.get("ok"):
                st.success("AWS STATUS_AWESOME: all AWS API checks passed.")
            else:
                st.warning("Run ALL AWS APIs is false. Review `failed_checks` and `checks`.")
            st.json(_short_json(aws_api))
        else:
            st.info("Run `ALL AWS APIs` to verify frontend-facing AWS endpoints.")

    _render_connectivity_tags(
        local_db=local_db,
        local_otp=local_otp,
        aws_otp=aws_otp,
        aws_db=aws_db,
    )

    st.markdown("#### Logs")
    l1, l2 = st.columns(2)
    with l1:
        ss["ops.logs.local.panel"] = "\n".join(ss.get("ops.logs.local") or [])
        st.text_area(
            "Local logs",
            height=240,
            key="ops.logs.local.panel",
            disabled=True,
        )
    with l2:
        ss["ops.logs.aws.panel"] = "\n".join(ss.get("ops.logs.aws") or [])
        st.text_area(
            "AWS logs",
            height=240,
            key="ops.logs.aws.panel",
            disabled=True,
        )


def _validate_action(path: Path, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.2, "Running GTFS zip validation")
    log(f"Validating GTFS zip: {path}")
    report = validate_gtfs_zip(path)

    if report.get("ok"):
        progress(1.0, "Validation succeeded")
        st.session_state[_work_state_key("ops.gtfs.last_file_path")] = str(path)
        st.session_state[_work_state_key("ops.gtfs.last_checksum")] = str(report.get("checksum_sha256") or "")
        log(f"Validation OK. checksum={report.get('checksum_sha256')}")
    else:
        progress(1.0, "Validation failed")
        log(f"Validation errors: {report.get('errors')}")

    st.session_state[_work_state_key("ops.gtfs.last_validation")] = report
    return report


def _local_status_action(cfg: OpsConfig, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.2, "Collecting local DB status")
    out = check_local_status(cfg, log=log)
    progress(1.0, "Local status collected")
    log("Local status check completed")
    payload = dict(out)
    local_db = payload.get("local_db") if isinstance(payload.get("local_db"), dict) else {}
    local_otp = payload.get("local_otp") if isinstance(payload.get("local_otp"), dict) else {}
    payload["ok"] = bool(local_db.get("ok")) and (
        bool(local_otp.get("running"))
        or bool((local_otp.get("health") or {}).get("reachable"))
    )
    return payload


def _aws_status_action(cfg: OpsConfig, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.2, "Collecting AWS status")
    out = check_aws_status(cfg, log=log)
    progress(1.0, "AWS status collected")
    log("AWS status check completed")
    ssh_ok = bool((out.get("ssh") or {}).get("ok"))
    aws_otp = out.get("aws_otp") if isinstance(out.get("aws_otp"), dict) else {}
    aws_db = out.get("aws_db") if isinstance(out.get("aws_db"), dict) else {}
    otp_ok = bool(aws_otp.get("running")) or bool((aws_otp.get("health") or {}).get("reachable"))
    db_ok = bool(aws_db.get("ok"))
    return {
        "ok": ssh_ok and otp_ok and db_ok,
        "aws_status": out,
        "aws_otp": out.get("aws_otp"),
        "aws_db": out.get("aws_db"),
    }


def _aws_full_deploy_action(
    cfg: OpsConfig,
    gtfs_zip: Path,
    log: LogFn,
    progress: ProgressFn,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "started_at": _now_iso(),
        "gtfs_zip_path": str(gtfs_zip),
        "steps": {},
        "errors": [],
    }

    progress(0.02, "Step 1/2: deploying gtfs_app runtime (backend + OTP)")

    def _runtime_progress(frac: float, msg: str) -> None:
        progress(0.02 + (max(0.0, min(1.0, frac)) * 0.48), f"Step 1/2 runtime deploy: {msg}")

    runtime_source = cfg.transportapp_root or cfg.backend_push_local_dir
    runtime_res = run_aws_backend_push(
        cfg,
        log=log,
        progress=_runtime_progress,
        source_dir_override=runtime_source,
        require_transport_bundle=True,
    )
    out["steps"]["aws_runtime"] = runtime_res
    if not runtime_res.get("ok"):
        out["errors"].append("AWS runtime deploy failed (backend/OTP update).")
        out["finished_at"] = _now_iso()
        return out

    progress(0.52, "Step 2/2: applying GTFS + DB + OTP graph on AWS")

    def _publish_progress(frac: float, msg: str) -> None:
        progress(0.52 + (max(0.0, min(1.0, frac)) * 0.48), f"Step 2/2 GTFS/DB/OTP: {msg}")

    publish_res = run_aws_publish(cfg, gtfs_zip, log=log, progress=_publish_progress)
    out["steps"]["aws_publish"] = publish_res
    if not publish_res.get("ok"):
        out["errors"].append("AWS GTFS/DB/OTP publish failed after runtime deploy.")
        out["finished_at"] = _now_iso()
        return out

    out["aws_backend"] = runtime_res.get("steps", {}).get("backend_status")
    out["aws_otp"] = publish_res.get("steps", {}).get("status", {}).get("aws_otp")
    out["aws_db"] = publish_res.get("steps", {}).get("status", {}).get("aws_db")
    out["ok"] = True
    out["finished_at"] = _now_iso()
    progress(1.0, "AWS full deploy completed")
    log("AWS full deploy completed (runtime + GTFS/DB/OTP).")
    return out


def _local_api_checks_action(cfg: OpsConfig, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.15, "Running ALL local server API checks")
    out = run_all_local_server_api_checks(cfg, log=log)
    if out.get("ok"):
        progress(1.0, "Run ALL APIs passed (STATUS_AWESOME)")
    else:
        progress(1.0, "Run ALL APIs completed: false")
    log(f"Run ALL APIs completed: {out.get('status')}")
    return out


def _aws_api_checks_action(cfg: OpsConfig, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.15, "Running ALL AWS server API checks")
    out = run_all_aws_server_api_checks(cfg, log=log)
    if out.get("ok"):
        progress(1.0, "Run ALL AWS APIs passed (STATUS_AWESOME)")
    else:
        progress(1.0, "Run ALL AWS APIs completed: false")
    log(f"Run ALL AWS APIs completed: {out.get('status')}")
    return out
