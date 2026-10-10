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
from datamind_console.i18n import t
from datamind_console.ops import gtfs_canonical_validator as canonical_validator
from datamind_console.ops import gtfs_quarantine
from datamind_console.ui.formatting import format_number


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


def _upload_token(uploaded: Any) -> str:
    return f"{getattr(uploaded, 'file_id', None) or uploaded.name}:{getattr(uploaded, 'size', '')}"


def _resolve_gtfs_file_once(uploaded: Any, cfg: OpsConfig) -> Optional[Path]:
    """Like ``_resolve_gtfs_file`` but saves each upload only once.

    ``_save_uploaded_gtfs`` writes a new timestamped copy on every call, so
    each action click (validate, validator, publish) used to store another
    copy of the same upload.
    """
    if uploaded is None:
        return _resolve_gtfs_file(None, cfg)
    key = _work_state_key("ops.gtfs.saved_upload")
    token = _upload_token(uploaded)
    cached = st.session_state.get(key) or {}
    if cached.get("token") == token and Path(str(cached.get("path") or "")).is_file():
        return Path(cached["path"])
    path = _save_uploaded_gtfs(uploaded, cfg)
    st.session_state[key] = {"token": token, "path": str(path)}
    return path


def _known_current_path(uploaded: Any) -> Optional[str]:
    """Path of the file the operator is looking at, without saving anything."""
    if uploaded is not None:
        cached = st.session_state.get(_work_state_key("ops.gtfs.saved_upload")) or {}
        return cached.get("path") if cached.get("token") == _upload_token(uploaded) else None
    raw = str(st.session_state.get(_work_state_key("ops.gtfs.last_file_path")) or "").strip()
    return raw or None


def _record_structure_result(path: Path, report: dict[str, Any]) -> None:
    results = st.session_state.setdefault(_work_state_key("ops.gtfs.structure_results"), {})
    results[str(path)] = {"ok": bool(report.get("ok")), "errors": list(report.get("errors") or [])[:20]}


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
    direct = _resolve_gtfs_file_once(uploaded, cfg)
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
    _record_structure_result(path, validation)

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
        status_box.success(t("ui.misc.gtfs_ops.label_completed", label=(label)))
        progress_bar.progress(100)
    else:
        status_box.error(t("ui.misc.gtfs_ops.label_failed", label=(label)))

    return result


def _render_missing(title: str, missing: list[str]) -> None:
    if not missing:
        return
    st.warning(t("ui.misc.gtfs_ops.title_not_connected_fix_config_join", title=(title), join=(', '.join(missing))))


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
        st.info(t("ui.misc.gtfs_ops.no_status_snapshot_yet_run_check"))
        return

    tags = [
        (t("ops.conn.local_db"), _status_connected("local_db", local_db)),
        (t("ops.conn.local_otp"), _status_connected("local_otp", local_otp)),
        (t("ops.conn.aws_otp"), _status_connected("aws_otp", aws_otp)),
        (t("ops.conn.aws_db"), _status_connected("aws_db", aws_db)),
    ]
    cols = st.columns(4)
    for idx, (name, ok) in enumerate(tags):
        with cols[idx]:
            if ok:
                st.success(t("ui.misc.gtfs_ops.name_connected", name=(name)))
            else:
                st.warning(t("ui.misc.gtfs_ops.name_not_connected", name=(name)))

    if any(not ok for _, ok in tags):
        st.warning(t("ui.misc.gtfs_ops.connectivity_issue_detected_fix_config_host"))


def _short_json(data: Any) -> Any:
    if isinstance(data, dict):
        return data
    if data is None:
        return {}
    return {"value": data}


def _status_block(title: str, payload: Any) -> None:
    """One status entry: 'not checked' when empty, raw payload behind an expander."""
    st.markdown(f"**{title}**")
    if not payload:
        st.caption(t("gtfs.status.not_checked"))
        return
    with st.expander(t("gtfs.status.details"), expanded=False):
        st.json(_short_json(payload))


def _api_preflight_block(title: str, result: Any, *, hint_key: str) -> None:
    st.markdown(f"**{title}**")
    if not (isinstance(result, dict) and result):
        st.caption(t(hint_key))
        return
    if result.get("ok"):
        st.success(t("gtfs.status.api_ok"), icon=":material/check_circle:")
    else:
        st.warning(t("gtfs.status.api_failed"), icon=":material/warning:")
    with st.expander(t("gtfs.status.details"), expanded=not result.get("ok")):
        st.json(_short_json(result))


def _canonical_passed_for(path: Path) -> bool:
    """True when the MobilityData validator passed on this exact resolved file in this session."""
    state = st.session_state.get(_work_state_key("ops.gtfs.canonical_result")) or {}
    try:
        return state.get("status") == "pass" and Path(str(state.get("path"))).resolve() == Path(path).resolve()
    except (OSError, TypeError):
        return False


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

    st.markdown(t("ui.misc.gtfs_ops.safe_aws_deploy_checklist"))
    with st.expander(t("ui.misc.gtfs_ops.show_checklist_stop_remove_replace_keep"), expanded=False):
        st.markdown(
            "\n".join(
                [
                    t("ops.checklist.1"),
                    t("ops.checklist.2"),
                    t("ops.checklist.3"),
                    t("ops.checklist.4"),
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


def _render_validation_layers(current_path: Optional[Path]) -> None:
    """What has (and has not) been checked for the current file."""
    ss = st.session_state
    structure_results = ss.get(_work_state_key("ops.gtfs.structure_results")) or {}
    canonical = ss.get(_work_state_key("ops.gtfs.canonical_result")) or {}
    same_file = lambda p: bool(current_path is not None and p and str(p) == str(current_path))  # noqa: E731

    structure = structure_results.get(str(current_path)) if current_path is not None else None
    structure_state = ("pass" if structure.get("ok") else "fail") if structure else "not_run"
    canonical_state = canonical.get("status") if same_file(canonical.get("path")) else "not_run"
    if canonical_state == "error":
        canonical_state = "fail"

    rows = [
        ("structure", structure_state),
        ("quality", "not_applicable"),
        ("canonical", canonical_state),
    ]
    with st.container(horizontal=True, gap="small"):
        for layer, state in rows:
            tone = {"pass": "green", "fail": "red", "not_run": "gray", "not_applicable": "gray"}.get(state, "gray")
            st.badge(f"{t(f'gtfs.layer.{layer}')} · {t(f'gtfs.layer_state.{state}')}", color=tone)
    st.caption(t("gtfs.layer.quality_note"))
    blocked = gtfs_quarantine.refusal(current_path) if current_path is not None and current_path.exists() else None
    if blocked:
        st.error(t("gtfs.quarantine.blocked", reason=blocked), icon=":material/block:")
        return
    ready = structure_state == "pass" and canonical_state == "pass"
    if ready:
        st.success(t("gtfs.publish_ready.yes"), icon=":material/task_alt:")
    else:
        st.info(t("gtfs.publish_ready.no"), icon=":material/info:")

    if canonical and same_file(canonical.get("path")):
        counts = canonical.get("counts") or {}
        with st.expander(t("gtfs.canonical.details", errors=format_number(counts.get("ERROR", 0)), warnings=format_number(counts.get("WARNING", 0))), expanded=canonical.get("status") != "pass"):
            if canonical.get("status") == "error":
                st.error(t("gtfs.canonical.error"))
                st.code(canonical.get("stderr_tail") or "", language="text")
            if canonical.get("notices"):
                st.dataframe(
                    [{t("col.notice"): n.get("code"), t("col.severity"): n.get("severity"), t("col.count"): n.get("total")} for n in canonical["notices"]],
                    hide_index=True, width="stretch",
                )
            st.caption(t("gtfs.canonical.version", version=canonical.get("version") or "—", at=canonical.get("at") or "—"))
            html_path = canonical.get("html_report")
            if html_path and Path(html_path).is_file():
                st.download_button(
                    t("gtfs.canonical.download_report"),
                    data=Path(html_path).read_bytes(),
                    file_name=f"gtfs-validator-report-{Path(str(current_path)).stem}.html",
                    mime="text/html",
                    icon=":material/download:",
                    key="ops.gtfs.canonical.download",
                )


def render_gtfs_ops_widget() -> None:
    _init_state()
    ss = st.session_state
    cfg = load_ops_config()

    work = active_work(ss)
    if work:
        st.caption(t("gtfs.work_active", name=work["name"]))
    else:
        st.caption(t("gtfs.work_none"))

    with st.expander(t("gtfs.config_paths"), expanded=False, icon=":material/settings_ethernet:"):
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
            t(
                "gtfs.config_missing",
                local=", ".join(top_missing_local) if top_missing_local else "OK",
                aws=", ".join(top_missing_aws_full) if top_missing_aws_full else "OK",
            ),
            icon=":material/settings_alert:",
        )

    # ---- 1 · Validate the feed -------------------------------------------
    with st.container(border=True):
        st.subheader(t("gtfs.validate.title"), anchor=False)
        st.caption(t("gtfs.validate.caption"))
        upload_key = _work_state_key("ops.gtfs.upload")
        uploaded = st.file_uploader(t("gtfs.upload"), type=["zip"], key=upload_key)
        if uploaded is not None:
            st.caption(t("gtfs.upload_selected", name=uploaded.name, size=format_number(int(uploaded.size))))
        v1, v2 = st.columns(2)
        with v1:
            do_validate = st.button(t("gtfs.structure.run"), use_container_width=True, key="ops.gtfs.validate", icon=":material/rule:")
        with v2:
            avail = canonical_validator.availability()
            do_canonical = st.button(
                t("gtfs.canonical.run"), use_container_width=True, key="ops.gtfs.canonical",
                icon=":material/verified:", disabled=not avail.ready,
                help=None if avail.ready else t("gtfs.canonical.unavailable", missing=", ".join(avail.missing)),
            )
        if do_canonical:
            current_path = _resolve_gtfs_file_once(uploaded, cfg)
            if current_path is None:
                st.warning(t("gtfs.need_upload"))
            else:
                with st.spinner(t("gtfs.canonical.running"), show_time=True):
                    vres = canonical_validator.run_validator(Path(current_path))
                ss[_work_state_key("ops.gtfs.canonical_result")] = {
                    "path": str(current_path),
                    "status": vres.status,
                    "counts": vres.counts,
                    "notices": vres.notices[:25],
                    "version": vres.validator_version,
                    "html_report": str(vres.html_report) if vres.html_report else None,
                    "stderr_tail": vres.stderr_tail,
                    "at": _now_iso(),
                }
                _append_log("local", f"MobilityData validator: {vres.status} counts={vres.counts}")
        if do_validate:
            path = _resolve_gtfs_file_once(uploaded, cfg)
            if path is None:
                st.warning(t("gtfs.need_upload"))
            else:
                result = _run_action(
                    scope="local",
                    label=t("ops.action.validate"),
                    action=lambda log, progress: _validate_action(path, log, progress),
                )
                ss.setdefault("ops.status", {})["gtfs_validation"] = result
        current_known = _known_current_path(uploaded)
        _render_validation_layers(Path(current_known) if current_known else None)

    # ---- 2 · Publish ------------------------------------------------------
    with st.container(border=True):
        st.subheader(t("gtfs.publish.title"), anchor=False)
        st.caption(t("gtfs.publish.caption"))
        canonical_state = ss.get(_work_state_key("ops.gtfs.canonical_result")) or {}
        current_known = _known_current_path(uploaded)
        canonical_passed = bool(
            current_known is not None
            and canonical_state.get("path") == current_known
            and canonical_state.get("status") == "pass"
        )
        ack_unvalidated = True
        if not canonical_passed:
            ack_unvalidated = st.checkbox(t("gtfs.publish.ack_unvalidated"), key="ops.gtfs.local.ack_unvalidated", value=False)
        p1, p2 = st.columns(2)
        with p1:
            do_local = st.button(
                t("gtfs.publish.local"), type="primary", use_container_width=True, key="ops.gtfs.local.publish",
                icon=":material/publish:", disabled=not ack_unvalidated,
            )
        with p2:
            do_aws_full = st.button(t("gtfs.publish.aws"), use_container_width=True, key="ops.gtfs.aws.full", icon=":material/cloud_upload:")
        confirm_aws_full = st.checkbox(t("gtfs.publish.aws_confirm"), key="ops.gtfs.aws.full.confirm", value=False)
        allow_without_local_api = st.checkbox(t("gtfs.publish.aws_without_api"), key="ops.gtfs.aws.allow_without_local_api", value=False)
        _render_aws_deploy_checklist(cfg)

    # ---- 3 · Environment status ------------------------------------------
    with st.container(border=True):
        st.subheader(t("gtfs.env.title"), anchor=False)
        st.caption(t("gtfs.env.caption"))
        e1, e2, e3, e4 = st.columns(4)
        with e1:
            do_local_status = st.button(t("gtfs.env.local_status"), use_container_width=True, key="ops.gtfs.local.status")
        with e2:
            do_local_api_checks = st.button(t("gtfs.env.local_apis"), use_container_width=True, key="ops.gtfs.local.apis")
        with e3:
            do_aws_status = st.button(t("gtfs.env.aws_status"), use_container_width=True, key="ops.gtfs.aws.status")
        with e4:
            do_aws_api_checks = st.button(t("gtfs.env.aws_apis"), use_container_width=True, key="ops.gtfs.aws.apis")

    if do_local:
        missing = missing_for_local_publish(cfg)
        if missing:
            _render_missing(t("ops.action.local_publish"), missing)
        elif not ack_unvalidated:
            st.warning(t("gtfs.publish.ack_required"))
        else:
            path = _resolve_gtfs_file_once(uploaded, cfg)
            blocked = gtfs_quarantine.refusal(path) if path is not None else None
            if path is None:
                st.warning(t("gtfs.need_upload"))
            elif blocked:
                st.error(t("gtfs.quarantine.blocked", reason=blocked), icon=":material/block:")
            elif not _ensure_validated_gtfs(path):
                st.error(t("gtfs.publish.structure_failed"))
            else:
                result = _run_action(
                    scope="local",
                    label=t("ops.action.local_publish"),
                    action=lambda log, progress: run_local_publish(cfg, path, log=log, progress=progress),
                )
                if result.get("ok"):
                    ss["ops.local.last_run"] = _now_iso()
                ss.setdefault("ops.status", {})["local_publish"] = result

    if do_aws_full:
        if not confirm_aws_full:
            st.warning(t("gtfs.publish.aws_confirm_required"))
        elif (not allow_without_local_api) and (not _local_api_guard_ok(ss.get("ops.status") or {})):
            st.warning(t("gtfs.publish.aws_api_required"))
        else:
            missing = _merge_missing(missing_for_aws_publish(cfg), missing_for_aws_backend_push(cfg))
            if missing:
                _render_missing(t("ops.action.aws_full_deploy"), missing)
            else:
                path, source_kind = _resolve_gtfs_for_aws_full_deploy(uploaded, cfg)
                blocked = gtfs_quarantine.refusal(path) if path is not None else None
                if path is None:
                    st.error(
                        t("ui.misc.gtfs_ops.aws_full_deploy_could_not_find")
                    )
                elif blocked:
                    st.error(t("gtfs.quarantine.blocked", reason=blocked), icon=":material/block:")
                elif not (_canonical_passed_for(path) or ack_unvalidated):
                    # The auto-detected bundle is not the file the checkbox above was shown for.
                    st.warning(t("gtfs.publish.ack_required"))
                elif not _ensure_validated_gtfs(path):
                    st.error(t("gtfs.publish.structure_failed"))
                else:
                    if source_kind != "uploaded_or_cached":
                        st.caption(t("ui.misc.gtfs_ops.aws_full_deploy_using_auto_detected", path=(path)))
                    result = _run_action(
                        scope="aws",
                        label=t("ops.action.aws_full_deploy"),
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
            _render_missing(t("ops.action.local_status"), missing)
        else:
            result = _run_action(
                scope="local",
                label=t("ops.action.check_local_status"),
                action=lambda log, progress: _local_status_action(cfg, log, progress),
            )
            ss.setdefault("ops.status", {}).update(result)

    if do_aws_status:
        missing = missing_for_aws_status(cfg)
        if missing:
            _render_missing(t("ops.action.aws_status"), missing)
        else:
            result = _run_action(
                scope="aws",
                label=t("ops.action.check_aws_status"),
                action=lambda log, progress: _aws_status_action(cfg, log, progress),
            )
            ss.setdefault("ops.status", {}).update(result)

    if do_local_api_checks:
        result = _run_action(
            scope="local",
            label=t("ops.action.run_local_apis"),
            action=lambda log, progress: _local_api_checks_action(cfg, log, progress),
        )
        ss["ops.local.api.last_run"] = _now_iso()
        ss.setdefault("ops.status", {})["local_api_checks"] = result

    if do_aws_api_checks:
        missing = missing_for_aws_status(cfg)
        if missing:
            _render_missing(t("ops.action.aws_apis"), missing)
        else:
            result = _run_action(
                scope="aws",
                label=t("ops.action.run_aws_apis"),
                action=lambda log, progress: _aws_api_checks_action(cfg, log, progress),
            )
            ss["ops.aws.api.last_run"] = _now_iso()
            ss.setdefault("ops.status", {})["aws_api_checks"] = result

    st.markdown(f"#### {t('gtfs.status.title')}")
    st.caption(
        t(
            "gtfs.status.last_runs",
            local=ss.get("ops.local.last_run") or "—",
            local_api=ss.get("ops.local.api.last_run") or "—",
            aws_api=ss.get("ops.aws.api.last_run") or "—",
            aws=ss.get("ops.aws.last_run") or "—",
        )
    )

    status = ss.get("ops.status") or {}
    s1, s2 = st.columns(2)
    with s1:
        local_db = status.get("local_db") or status.get("local_publish", {}).get("steps", {}).get("status", {}).get("local_db")
        _status_block(t("gtfs.status.local_db"), local_db)

        local_otp = status.get("local_otp") or status.get("local_publish", {}).get("steps", {}).get("status", {}).get("local_otp")
        _status_block(t("gtfs.status.local_otp"), local_otp)

        local_api = status.get("local_api_checks")
        _api_preflight_block(t("gtfs.status.local_api"), local_api, hint_key="gtfs.status.local_api_hint")

    with s2:
        aws_full = status.get("aws_full_deploy")
        _status_block(t("gtfs.status.aws_full"), aws_full)

        aws_otp = (
            status.get("aws_otp")
            or status.get("aws_status", {}).get("aws_otp")
            or status.get("aws_full_deploy", {}).get("aws_otp")
            or status.get("aws_full_deploy", {}).get("steps", {}).get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_otp")
            or status.get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_otp")
        )
        _status_block(t("gtfs.status.aws_otp"), aws_otp)

        aws_db = (
            status.get("aws_db")
            or status.get("aws_status", {}).get("aws_db")
            or status.get("aws_full_deploy", {}).get("aws_db")
            or status.get("aws_full_deploy", {}).get("steps", {}).get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_db")
            or status.get("aws_publish", {}).get("steps", {}).get("status", {}).get("aws_db")
        )
        _status_block(t("gtfs.status.aws_db"), aws_db)

        aws_backend = (
            status.get("aws_backend")
            or status.get("aws_full_deploy", {}).get("aws_backend")
            or status.get("aws_full_deploy", {}).get("steps", {}).get("aws_runtime", {}).get("steps", {}).get("backend_status")
            or status.get("aws_backend_push", {}).get("steps", {}).get("backend_status")
        )
        _status_block(t("gtfs.status.aws_runtime"), aws_backend)

        aws_api = status.get("aws_api_checks")
        _api_preflight_block(t("gtfs.status.aws_api"), aws_api, hint_key="gtfs.status.aws_api_hint")

    _render_connectivity_tags(
        local_db=local_db,
        local_otp=local_otp,
        aws_otp=aws_otp,
        aws_db=aws_db,
    )

    st.markdown(t("ui.misc.gtfs_ops.logs"))
    l1, l2 = st.columns(2)
    with l1:
        ss["ops.logs.local.panel"] = "\n".join(ss.get("ops.logs.local") or [])
        st.text_area(
            t("ui.misc.gtfs_ops.local_logs"),
            height=240,
            key="ops.logs.local.panel",
            disabled=True,
        )
    with l2:
        ss["ops.logs.aws.panel"] = "\n".join(ss.get("ops.logs.aws") or [])
        st.text_area(
            t("ui.misc.gtfs_ops.aws_logs"),
            height=240,
            key="ops.logs.aws.panel",
            disabled=True,
        )


def _validate_action(path: Path, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.2, t("ops.progress.validating"))
    log(f"Validating GTFS zip: {path}")
    report = validate_gtfs_zip(path)

    if report.get("ok"):
        progress(1.0, t("ops.progress.validation_ok"))
        st.session_state[_work_state_key("ops.gtfs.last_file_path")] = str(path)
        st.session_state[_work_state_key("ops.gtfs.last_checksum")] = str(report.get("checksum_sha256") or "")
        log(f"Validation OK. checksum={report.get('checksum_sha256')}")
    else:
        progress(1.0, t("ops.progress.validation_failed"))
        log(f"Validation errors: {report.get('errors')}")

    st.session_state[_work_state_key("ops.gtfs.last_validation")] = report
    _record_structure_result(path, report)
    return report


def _local_status_action(cfg: OpsConfig, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.2, t("ops.progress.local_status"))
    out = check_local_status(cfg, log=log)
    progress(1.0, t("ops.progress.local_status_done"))
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
    progress(0.2, t("ops.progress.aws_status"))
    out = check_aws_status(cfg, log=log)
    progress(1.0, t("ops.progress.aws_status_done"))
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

    blocked = gtfs_quarantine.refusal(gtfs_zip)
    if blocked:
        log(blocked)
        out["errors"].append(blocked)
        out["steps"]["quarantine"] = {"ok": False, "reason": blocked}
        out["finished_at"] = _now_iso()
        return out

    progress(0.02, t("ops.progress.deploy_step1"))

    def _runtime_progress(frac: float, msg: str) -> None:
        progress(0.02 + (max(0.0, min(1.0, frac)) * 0.48), t("ops.progress.deploy_step1_detail", msg=msg))

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

    progress(0.52, t("ops.progress.deploy_step2"))

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
    progress(1.0, t("ops.progress.deploy_done"))
    log("AWS full deploy completed (runtime + GTFS/DB/OTP).")
    return out


def _local_api_checks_action(cfg: OpsConfig, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.15, t("ops.progress.local_apis"))
    out = run_all_local_server_api_checks(cfg, log=log)
    if out.get("ok"):
        progress(1.0, t("ops.progress.local_apis_ok"))
    else:
        progress(1.0, t("ops.progress.local_apis_failed"))
    log(f"Run ALL APIs completed: {out.get('status')}")
    return out


def _aws_api_checks_action(cfg: OpsConfig, log: LogFn, progress: ProgressFn) -> dict[str, Any]:
    progress(0.15, t("ops.progress.aws_apis"))
    out = run_all_aws_server_api_checks(cfg, log=log)
    if out.get("ok"):
        progress(1.0, t("ops.progress.aws_apis_ok"))
    else:
        progress(1.0, t("ops.progress.aws_apis_failed"))
    log(f"Run ALL AWS APIs completed: {out.get('status')}")
    return out
