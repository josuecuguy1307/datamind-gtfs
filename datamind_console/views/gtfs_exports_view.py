from __future__ import annotations

import os
import json
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

import streamlit as st

from datamind_console.services.gtfs_exports_service import (
    list_artifact_audit,
    list_artifact_notifications,
)


def _actor() -> str:
    user = st.session_state.get("auth.user") or {}
    who = str(user.get("email") or user.get("display_name") or "unknown")
    return f"dashboard:{who}"


def _api_base_url() -> str:
    return str(os.getenv("DATAMIND_BASE_URL", "") or "http://127.0.0.1:8006").rstrip("/")


def _api_key() -> str:
    return str(os.getenv("DATAMIND_API_KEY", "") or "").strip()


def _download_from_api(artifact_id: str) -> bytes:
    url = f"{_api_base_url()}/api/gtfs/exports/{artifact_id}/download"
    req = urlrequest.Request(url=url, method="GET")
    key = _api_key()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("X-API-Key", key)
    try:
        with urlrequest.urlopen(req, timeout=40) as resp:
            return resp.read()
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise RuntimeError(f"API download failed ({int(e.code)}): {body[:400]}") from e
    except Exception as e:
        raise RuntimeError(f"API download failed: {e}") from e


def _api_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_api_base_url()}{path}"
    raw = None
    if payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(url=url, data=raw, method=str(method or "GET").upper())
    req.add_header("Content-Type", "application/json")
    key = _api_key()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("X-API-Key", key)
    try:
        with urlrequest.urlopen(req, timeout=40) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        raise RuntimeError(f"API request failed ({int(e.code)}): {body[:400]}") from e
    except Exception as e:
        raise RuntimeError(f"API request failed: {e}") from e

    if not body:
        return {}
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"data": parsed}
    except Exception:
        return {"raw_text": body}


def render_gtfs_exports_section() -> None:
    st.markdown("### GTFS Exports")
    st.caption("Approval-gated GTFS artifacts. Download is only enabled after approval.")

    try:
        pending = (_api_json("GET", "/api/gtfs/exports?status=pending_approval&limit=200").get("items") or [])
        approved = (_api_json("GET", "/api/gtfs/exports?status=approved&limit=200").get("items") or [])
        downloaded = (_api_json("GET", "/api/gtfs/exports?status=downloaded&limit=200").get("items") or [])
        if not isinstance(pending, list):
            pending = []
        if not isinstance(approved, list):
            approved = []
        if not isinstance(downloaded, list):
            downloaded = []
    except Exception as e:
        st.error("GTFS exports API unavailable.")
        st.exception(e)
        return

    k1, k2, k3 = st.columns(3)
    k1.metric("Pending", len(pending))
    k2.metric("Approved", len(approved))
    k3.metric("Downloaded", len(downloaded))

    tabs = st.tabs(["Pending Approval", "Approved", "Audit + Notifications"])

    with tabs[0]:
        if not pending:
            st.info("No pending artifacts.")
        else:
            st.dataframe(
                [
                    {
                        "created_at": r.get("created_at"),
                        "artifact_id": r.get("artifact_id"),
                        "status": r.get("status"),
                        "zip_path": r.get("zip_path"),
                        "approval_token": r.get("approval_token"),
                        "summary_json": r.get("summary_json"),
                    }
                    for r in pending
                ],
                use_container_width=True,
                hide_index=True,
                height=220,
            )
            for r in pending:
                artifact_id = str(r.get("artifact_id") or "")
                title = f"{artifact_id} | {r.get('created_at')}"
                with st.expander(title, expanded=False):
                    st.json(r.get("summary_json") or {})
                    notes = st.text_input("notes", value="", key=f"gtfs_exports.pending.notes.{artifact_id}")
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("Approve", key=f"gtfs_exports.pending.approve.{artifact_id}", use_container_width=True):
                            try:
                                out = _api_json(
                                    "POST",
                                    f"/api/gtfs/exports/{artifact_id}/approve",
                                    payload={
                                        "approved_by": _actor(),
                                        "notes": (notes or "Approved from dashboard."),
                                    },
                                )
                                if out.get("ok"):
                                    st.success("Artifact approved.")
                                    st.rerun()
                                st.error("Artifact not found or no status change.")
                            except Exception as e:
                                st.error(f"Approve failed: {e}")
                    with c2:
                        if st.button("Reject", key=f"gtfs_exports.pending.reject.{artifact_id}", use_container_width=True):
                            try:
                                out = _api_json(
                                    "POST",
                                    f"/api/gtfs/exports/{artifact_id}/reject",
                                    payload={
                                        "rejected_by": _actor(),
                                        "notes": (notes or "Rejected from dashboard."),
                                    },
                                )
                                if out.get("ok"):
                                    st.success("Artifact rejected.")
                                    st.rerun()
                                st.error("Artifact not found or no status change.")
                            except Exception as e:
                                st.error(f"Reject failed: {e}")

    with tabs[1]:
        rows = approved + downloaded
        if not rows:
            st.info("No approved/downloaded artifacts.")
        else:
            st.dataframe(
                [
                    {
                        "created_at": r.get("created_at"),
                        "artifact_id": r.get("artifact_id"),
                        "status": r.get("status"),
                        "approved_at": r.get("approved_at"),
                        "approved_by": r.get("approved_by"),
                        "downloaded_at": r.get("downloaded_at"),
                    }
                    for r in rows
                ],
                use_container_width=True,
                hide_index=True,
                height=220,
            )
            for r in rows:
                artifact_id = str(r.get("artifact_id") or "")
                status = str(r.get("status") or "")
                with st.expander(f"{artifact_id} | status={status}", expanded=False):
                    st.caption(f"zip_path: `{r.get('zip_path')}`")
                    st.json(r.get("summary_json") or {})
                    if status in {"approved", "downloaded"}:
                        if st.button("Fetch download from API", key=f"gtfs_exports.fetch.{artifact_id}", use_container_width=True):
                            try:
                                data = _download_from_api(artifact_id)
                                st.session_state[f"gtfs_exports.bytes.{artifact_id}"] = data
                                st.success("Download payload ready.")
                            except Exception as e:
                                st.error(str(e))
                        data = st.session_state.get(f"gtfs_exports.bytes.{artifact_id}")
                        if isinstance(data, (bytes, bytearray)) and data:
                            st.download_button(
                                "Download GTFS ZIP",
                                data=bytes(data),
                                file_name=f"{artifact_id}.zip",
                                mime="application/zip",
                                use_container_width=True,
                                key=f"gtfs_exports.download.{artifact_id}",
                            )

    with tabs[2]:
        all_rows = pending + approved + downloaded
        if not all_rows:
            st.info("No artifacts to inspect.")
            return
        ids = [str(r.get("artifact_id") or "") for r in all_rows if r.get("artifact_id")]
        pick = st.selectbox("Artifact", options=ids, index=0, key="gtfs_exports.audit.pick")
        audit_rows = list_artifact_audit(artifact_id=pick, limit=200)
        notif_rows = list_artifact_notifications(artifact_id=pick, limit=200)

        st.markdown("#### Audit log")
        st.dataframe(audit_rows or [], use_container_width=True, hide_index=True, height=220)

        st.markdown("#### Notifications")
        st.dataframe(notif_rows or [], use_container_width=True, hide_index=True, height=220)
