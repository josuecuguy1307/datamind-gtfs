from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlparse

import streamlit as st

from datamind_console.common.config import CFG
from datamind_console.i18n import t, option_label
from datamind_console.orchestrator import signing_key
from datamind_console.ui.components.page import page_header


def _password_policy(password: str) -> list[str]:
    """Keep the existing remote registration policy available outside local mode."""
    errors: list[str] = []
    if len(password) < 10:
        errors.append(t("settings.pw.min_length"))
    if not re.search(r"[A-Z]", password):
        errors.append(t("settings.pw.uppercase"))
    if not re.search(r"[a-z]", password):
        errors.append(t("settings.pw.lowercase"))
    if not re.search(r"\d", password):
        errors.append(t("settings.pw.number"))
    return errors


def _render_signing_key_status() -> None:
    """Whether the autopilot signs decisions with this installation's own key (never shows it)."""
    with st.container(border=True):
        st.subheader(t("settings.signing.title"), anchor=False)
        if signing_key.is_development_key():
            st.warning(t("settings.signing.development", var=signing_key.PRIMARY_ENV_VAR), icon=":material/key_off:")
        else:
            st.success(t("settings.signing.configured", var=signing_key.source()), icon=":material/key:")
        st.caption(t("settings.signing.how"))
        st.code("python -m datamind_console.orchestrator.signing_key --write .env", language="bash")


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

    page_header(t("page.settings.title"), t("page.settings.subtitle"), eyebrow=t("page.settings.eyebrow"))
    ss = st.session_state

    with st.container(border=True):
        st.subheader(t("settings.appearance.title"), anchor=False)
        st.caption(t("settings.appearance.theme"))
        st.caption(t("settings.appearance.language"))

    _render_signing_key_status()

    if local_mode:
        with st.container(border=True):
            st.subheader(t("settings.local.title"), anchor=False)
            st.info(t("settings.local.no_accounts"), icon=":material/lock_open:")
            st.caption(t("settings.local.providers"))
            rows = [
                {t("col.setting"): "APP_ENV", t("col.value"): os.getenv("APP_ENV", "dev")},
                {t("col.setting"): "DATA_MODE", t("col.value"): os.getenv("DATA_MODE", CFG.data_mode)},
                {t("col.setting"): "DB_DSN", t("col.value"): t("settings.configured") if CFG.db_dsn else t("settings.not_set")},
                {t("col.setting"): "LOOPBACK_UI", t("col.value"): t("settings.enabled")},
            ]
            st.dataframe(rows, hide_index=True, width="stretch")
            st.caption(t("settings.local.schema_cli"))
            st.code("python -m datamind_console.db.schema_cli status", language="bash")
        return

    tabs = st.tabs([t("settings.tab.auth"), t("settings.tab.environment"), t("settings.tab.console_db"),
                    t("settings.tab.audit"), t("settings.tab.workspace")])

    with tabs[0]:
        st.markdown(t("ui.misc.settings.login_session"))

        auth_svc = getattr(ctx, "auth", None)
        repo = getattr(ctx, "console_repo", None)
        if auth_svc is None or repo is None:
            st.error(t("ui.misc.settings.auth_service_not_available_ctx_auth"))
            return

        user = ss.get("auth.user") or {}
        roles = ss.get("auth.roles") or []
        sid = ss.get("auth.session_id")
        # Values come from the database; never interpolate them into raw HTML
        # (they were rendered unescaped with unsafe_allow_html → stored XSS).
        with st.container(border=True):
            with st.container(horizontal=True, gap="small"):
                st.badge(str(user.get("display_name") or "—"), color="violet")
                st.badge(str(user.get("email") or "—"), color="gray")
                st.badge(t("settings.roles", roles=", ".join(roles) if roles else "—"), color="blue")
            st.caption(t("settings.session_id", sid=sid or "—"))

        st.markdown(t("ui.misc.settings.register_new_user"))
        with st.form("settings.register_user", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                new_email = st.text_input(t("ui.misc.settings.email"))
                new_name = st.text_input(t("ui.misc.settings.display_name"))
            with c2:
                new_role = st.selectbox(t("ui.misc.settings.role"), ["viewer", "editor", "admin"], index=0, format_func=option_label("role"))
                new_active = st.checkbox(t("ui.misc.settings.active"), value=True)
            p1, p2 = st.columns(2)
            with p1:
                new_password = st.text_input(t("ui.misc.settings.password"), type="password")
            with p2:
                new_password2 = st.text_input(t("ui.misc.settings.confirm_password"), type="password")
            submit_register = st.form_submit_button(t("ui.misc.settings.create_user"), use_container_width=True)

        if submit_register:
            new_email = (new_email or "").strip().lower()
            new_name = (new_name or "").strip()
            errs = []
            if not new_email or "@" not in new_email:
                errs.append(t("settings.register.email_required"))
            if not new_name:
                errs.append(t("settings.register.name_required"))
            if new_password != new_password2:
                errs.append(t("settings.register.password_mismatch"))
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
                    st.success(t("ui.misc.settings.user_created_email", email=(created.get('email'))))
                except Exception as e:
                    st.error(t("ui.misc.settings.could_not_create_user_e", e=(e)))

        st.markdown(t("ui.misc.settings.email_confirmation_mock"))
        users = []
        try:
            users = repo.list_users(limit=200)
        except Exception as e:
            st.warning(t("ui.misc.settings.could_not_load_users_e", e=(e)))

        if users:
            options = {
                f"{u.get('display_name') or 'user'} | {u.get('email')} | verified={bool(u.get('email_verified'))}": u
                for u in users
            }
            pick_label = st.selectbox(t("ui.misc.settings.user"), list(options.keys()), key="settings.auth.user_pick")
            picked = options[pick_label]

            csend, cttl = st.columns([2, 1])
            with csend:
                send_click = st.button(t("ui.misc.settings.send_confirmation_email"), use_container_width=True, key="settings.auth.send_confirm")
            with cttl:
                ttl_h = st.number_input(t("ui.misc.settings.ttl_hours"), min_value=1, max_value=168, value=24, step=1)

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
                    st.success(t("ui.misc.settings.mock_email_queued_email", email=(picked.get('email'))))
                    st.code(str(row.get("token") or ""), language="text")
                except Exception as e:
                    st.error(t("ui.misc.settings.could_not_send_confirmation_e", e=(e)))

            st.markdown(t("ui.misc.settings.confirm_token"))
            tok = st.text_input(t("ui.misc.settings.confirmation_token"), key="settings.auth.confirm_token")
            if st.button(t("ui.misc.settings.confirm_email"), use_container_width=True, key="settings.auth.confirm_btn"):
                if not tok.strip():
                    st.warning(t("ui.misc.settings.enter_token_first"))
                else:
                    try:
                        res = repo.confirm_email_token(token=tok.strip())
                        if res.get("ok"):
                            repo.log_audit_event(
                                user_id=str(user.get("user_id") or "") or None,
                                action="CONFIRM_EMAIL",
                                payload={"token": tok[:8]},
                            )
                            st.success(t("ui.misc.settings.email_confirmed"))
                        else:
                            st.error(res.get("error") or t("settings.token_not_confirmed"))
                    except Exception as e:
                        st.error(t("ui.misc.settings.confirm_failed_e", e=(e)))

            pending = repo.list_pending_verifications(limit=20)
            if pending:
                st.markdown(t("ui.misc.settings.pending_tokens"))
                st.dataframe(pending, use_container_width=True, hide_index=True)

    with tabs[1]:
        st.markdown(t("ui.misc.settings.environment"))
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
            {"key": "DB_DSN", "value": t("settings.configured") if CFG.db_dsn else t("settings.not_set")},
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
        st.markdown(t("ui.misc.settings.console_db"))
        repo = getattr(ctx, "console_repo", None)
        if repo is None:
            st.info(t("ui.misc.settings.console_repo_not_provided"))
        else:
            try:
                users = repo.list_users(limit=1000)
                pending = repo.list_pending_verifications(limit=1000)
                st.metric(t("ui.misc.settings.users"), len(users))
                st.metric(t("ui.misc.settings.pending_email_verifications"), len(pending))
            except Exception as e:
                st.error(t("ui.misc.settings.db_check_failed_e", e=(e)))

    with tabs[3]:
        st.markdown(t("ui.misc.settings.audit"))
        audit_svc = getattr(ctx, "audit", None)
        if audit_svc is None:
            st.info(t("ui.misc.settings.audit_service_not_provided"))
        else:
            lim = st.number_input(t("ui.misc.settings.rows"), min_value=10, max_value=500, value=50, step=10)
            try:
                events = audit_svc.list_events(limit=int(lim))
                if events:
                    st.dataframe(events, use_container_width=True, hide_index=True)
                else:
                    st.info(t("ui.misc.settings.no_audit_events_yet"))
            except Exception as e:
                st.error(t("ui.misc.settings.could_not_load_audit_events_e", e=(e)))

    with tabs[4]:
        st.markdown(t("ui.misc.settings.workspace"))
        repo = getattr(ctx, "console_repo", None)
        user = ss.get("auth.user") or {}
        if not repo or not user.get("user_id"):
            st.info(t("ui.misc.settings.user_session_unavailable"))
        else:
            try:
                ws = repo.get_workspace_state(str(user.get("user_id")))
                if ws:
                    st.json(ws)
                else:
                    st.info(t("ui.misc.settings.no_workspace_state_saved_current_user"))
            except Exception as e:
                st.error(t("ui.misc.settings.could_not_load_workspace_state_e", e=(e)))
