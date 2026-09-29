from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlparse

import streamlit as st

from datamind_console.common.config import CFG


def _password_policy(password: str) -> list[str]:
    """Keep the existing remote registration policy available outside local mode."""
    errors: list[str] = []
    if len(password) < 10:
        errors.append("Password must contain at least 10 characters.")
    if not re.search(r"[A-Z]", password):
        errors.append("Password must contain an uppercase letter.")
    if not re.search(r"[a-z]", password):
        errors.append("Password must contain a lowercase letter.")
    if not re.search(r"\d", password):
        errors.append("Password must contain a number.")
    return errors


def _inject_settings_css() -> None:
    st.markdown(
        """
        <style>
          .settings-card {
            border: 1px solid #c8d8f5;
            border-radius: 12px;
            padding: 14px;
            background: linear-gradient(180deg, #f8fbff 0%, #eff6ff 100%);
            margin-bottom: 0.8rem;
          }
          .settings-chip {
            display: inline-block;
            border-radius: 999px;
            background: #e1eeff;
            border: 1px solid #b8d4ff;
            color: #1f4f8a;
            padding: 0.2rem 0.55rem;
            margin-right: 0.35rem;
            font-size: 0.78rem;
            font-weight: 600;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_settings_view(
    *,
    ctx: Optional[Any] = None,
    auth: Any = None,
    audit: Any = None,
    console_repo: Any = None,
    local_mode: bool = False,
    **_,
) -> None:
    if ctx is None:
        ctx = SimpleNamespace(auth=auth, audit=audit, console_repo=console_repo)

    _inject_settings_css()

    st.subheader("Settings")
    ss = st.session_state

    if local_mode:
        st.markdown("### Local application")
        st.info("This local loopback installation does not use product accounts, registration, email confirmation, password recovery, or logout.")
        st.caption("Database, cloud, and assistant providers retain their own authentication and approval requirements.")
        rows = [
            {"key": "APP_ENV", "value": os.getenv("APP_ENV", "dev")},
            {"key": "DATA_MODE", "value": os.getenv("DATA_MODE", CFG.data_mode)},
            {"key": "DB_DSN", "value": "configured" if CFG.db_dsn else "not set"},
            {"key": "LOOPBACK_UI", "value": "enabled"},
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        return

    tabs = st.tabs(["Auth", "Environment", "Console DB", "Audit", "Workspace"])

    with tabs[0]:
        st.markdown("### Login / Session")

        auth_svc = getattr(ctx, "auth", None)
        repo = getattr(ctx, "console_repo", None)
        if auth_svc is None or repo is None:
            st.error("Auth service not available (ctx.auth/console_repo missing).")
            return

        user = ss.get("auth.user") or {}
        roles = ss.get("auth.roles") or []
        sid = ss.get("auth.session_id")
        st.markdown(
            f"""
            <div class="settings-card">
              <div><span class="settings-chip">{user.get('display_name','No user')}</span>
              <span class="settings-chip">{user.get('email','-')}</span>
              <span class="settings-chip">roles: {', '.join(roles) if roles else '-'}</span></div>
              <div style="margin-top:8px; color:#375980;">session_id: {sid or '-'}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.markdown("### Register New User")
        with st.form("settings.register_user", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                new_email = st.text_input("Email")
                new_name = st.text_input("Display name")
            with c2:
                new_role = st.selectbox("Role", ["viewer", "editor", "admin"], index=0)
                new_active = st.checkbox("Active", value=True)
            p1, p2 = st.columns(2)
            with p1:
                new_password = st.text_input("Password", type="password")
            with p2:
                new_password2 = st.text_input("Confirm password", type="password")
            submit_register = st.form_submit_button("Create user", use_container_width=True)

        if submit_register:
            new_email = (new_email or "").strip().lower()
            new_name = (new_name or "").strip()
            errs = []
            if not new_email or "@" not in new_email:
                errs.append("Valid email is required.")
            if not new_name:
                errs.append("Display name is required.")
            if new_password != new_password2:
                errs.append("Passwords do not match.")
            errs.extend(_password_policy(new_password or ""))
            if errs:
                for e in errs:
                    st.error(e)
            else:
                try:
                    created = repo.create_user(
                        email=new_email,
                        display_name=new_name,
                        password=new_password,
                        role=new_role,
                        is_active=bool(new_active),
                    )
                    repo.log_audit_event(
                        user_id=str(user.get("user_id") or "") or None,
                        action="REGISTER_USER",
                        payload={"email": new_email, "role": new_role},
                    )
                    st.success(f"User created: {created.get('email')}")
                except Exception as e:
                    st.error(f"Could not create user: {e}")

        st.markdown("### Email Confirmation (mock)")
        users = []
        try:
            users = repo.list_users(limit=200)
        except Exception as e:
            st.warning(f"Could not load users: {e}")

        if users:
            options = {
                f"{u.get('display_name') or 'user'} | {u.get('email')} | verified={bool(u.get('email_verified'))}": u
                for u in users
            }
            pick_label = st.selectbox("User", list(options.keys()), key="settings.auth.user_pick")
            picked = options[pick_label]

            csend, cttl = st.columns([2, 1])
            with csend:
                send_click = st.button("Send confirmation email", use_container_width=True, key="settings.auth.send_confirm")
            with cttl:
                ttl_h = st.number_input("TTL hours", min_value=1, max_value=168, value=24, step=1)

            if send_click:
                try:
                    row = repo.create_email_verification_token(
                        user_id=str(picked["user_id"]),
                        email=str(picked["email"]),
                        ttl_hours=int(ttl_h),
                        requested_by=str(user.get("user_id") or "") or None,
                    )
                    repo.log_audit_event(
                        user_id=str(user.get("user_id") or "") or None,
                        action="SEND_CONFIRM_EMAIL",
                        payload={"target_email": picked.get("email"), "token_id": str(row.get("token_id"))},
                    )
                    st.success(f"Mock email queued for {picked.get('email')}")
                    st.code(str(row.get("token") or ""), language="text")
                except Exception as e:
                    st.error(f"Could not send confirmation: {e}")

            st.markdown("#### Confirm token")
            tok = st.text_input("Confirmation token", key="settings.auth.confirm_token")
            if st.button("Confirm email", use_container_width=True, key="settings.auth.confirm_btn"):
                if not tok.strip():
                    st.warning("Enter token first.")
                else:
                    try:
                        res = repo.confirm_email_token(token=tok.strip())
                        if res.get("ok"):
                            repo.log_audit_event(
                                user_id=str(user.get("user_id") or "") or None,
                                action="CONFIRM_EMAIL",
                                payload={"token": tok[:8]},
                            )
                            st.success("Email confirmed.")
                        else:
                            st.error(res.get("error") or "Could not confirm token.")
                    except Exception as e:
                        st.error(f"Confirm failed: {e}")

            pending = repo.list_pending_verifications(limit=20)
            if pending:
                st.markdown("#### Pending tokens")
                st.dataframe(pending, use_container_width=True, hide_index=True)

    with tabs[1]:
        st.markdown("### Environment")
        parsed = urlparse(CFG.db_dsn) if CFG.db_dsn else None
        parsed_local = urlparse(CFG.db_dsn_local) if CFG.db_dsn_local else None
        parsed_server = urlparse(CFG.db_dsn_server) if CFG.db_dsn_server else None
        if os.getenv("DB_DSN"):
            dsn_source = "DB_DSN"
        elif os.getenv("DATABASE_URL"):
            dsn_source = "DATABASE_URL"
        elif os.getenv("DATAMIND_CONSOLE_DB_DSN"):
            dsn_source = "DATAMIND_CONSOLE_DB_DSN"
        elif os.getenv("DATAMIND_DB_DSN"):
            dsn_source = "DATAMIND_DB_DSN"
        elif os.getenv("SUPABASE_DB_HOST"):
            dsn_source = "SUPABASE_DB_*"
        else:
            dsn_source = "none"

        rows = [
            {"key": "APP_ENV", "value": os.getenv("APP_ENV", "dev")},
            {"key": "LOG_LEVEL", "value": os.getenv("LOG_LEVEL", "INFO")},
            {"key": "DATA_MODE", "value": os.getenv("DATA_MODE", CFG.data_mode)},
            {"key": "DB_DSN", "value": "configured" if CFG.db_dsn else "not set"},
            {"key": "DB_DSN_SOURCE", "value": dsn_source},
            {"key": "DB_HOST_ACTIVE", "value": (parsed.hostname if parsed else "n/a")},
            {"key": "DB_PORT_ACTIVE", "value": (parsed.port if parsed else "n/a")},
            {"key": "DB_NAME_ACTIVE", "value": ((parsed.path or "").lstrip("/") if parsed else "n/a")},
            {"key": "DB_HOST_LOCAL", "value": (parsed_local.hostname if parsed_local else "n/a")},
            {"key": "DB_HOST_SERVER", "value": (parsed_server.hostname if parsed_server else "n/a")},
            {"key": "SCHEMA_CONSOLE", "value": os.getenv("SCHEMA_CONSOLE", "console")},
            {"key": "CONSOLE_PASSWORD_MIN_LEN", "value": int(CFG.password_min_len)},
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    with tabs[2]:
        st.markdown("### Console DB")
        repo = getattr(ctx, "console_repo", None)
        if repo is None:
            st.info("Console repo not provided.")
        else:
            try:
                users = repo.list_users(limit=1000)
                pending = repo.list_pending_verifications(limit=1000)
                st.metric("Users", len(users))
                st.metric("Pending email verifications", len(pending))
            except Exception as e:
                st.error(f"DB check failed: {e}")

    with tabs[3]:
        st.markdown("### Audit")
        audit_svc = getattr(ctx, "audit", None)
        if audit_svc is None:
            st.info("Audit service not provided.")
        else:
            lim = st.number_input("Rows", min_value=10, max_value=500, value=50, step=10)
            try:
                events = audit_svc.list_events(limit=int(lim))
                if events:
                    st.dataframe(events, use_container_width=True, hide_index=True)
                else:
                    st.info("No audit events yet.")
            except Exception as e:
                st.error(f"Could not load audit events: {e}")

    with tabs[4]:
        st.markdown("### Workspace")
        repo = getattr(ctx, "console_repo", None)
        user = ss.get("auth.user") or {}
        if not repo or not user.get("user_id"):
            st.info("User/session unavailable.")
        else:
            try:
                ws = repo.get_workspace_state(str(user.get("user_id")))
                if ws:
                    st.json(ws)
                else:
                    st.info("No workspace state saved for current user.")
            except Exception as e:
                st.error(f"Could not load workspace state: {e}")
