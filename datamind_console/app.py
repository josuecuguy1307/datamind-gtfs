# datamind_console/app.py
from __future__ import annotations

import sys
import os
from pathlib import Path
import inspect
from urllib.parse import urlparse

import streamlit as st
from streamlit import config as streamlit_config
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]
if load_dotenv:
    load_dotenv(ROOT / ".env")

# Absolute import roots (EXACTLY ONCE, BEFORE ANY OTHER PROJECT IMPORT)
sys.path.insert(0, str(ROOT))                     # ML DATAMIND
sys.path.insert(0, str(ROOT / "phase4_naming"))
sys.path.insert(0, str(ROOT / "phase2_semantics"))
sys.path.insert(0, str(ROOT / "phase3_routes"))
sys.path.insert(0, str(ROOT / "phase5_gtfs"))

from datamind_console.common.config import CFG, resolve_active_db_dsn
from datamind_console.db.console_repo import (
    verify_password,
    create_session,
    get_session,
    revoke_session,
    list_user_roles,
    get_workspace_state,
    upsert_workspace_state,
    log_audit_event,
    list_sync_events,
    log_sync_event,
    sync_local_to_server,
    DEFAULT_SYNC_SCHEMAS,
    DEFAULT_SYNC_STRATEGY,
)
import datamind_console.db.console_repo as console_repo

from datamind_console.views.dashboard_view import render_dashboard_view
from datamind_console.views.phases_view import render_phases_view
from datamind_console.views.settings_view import render_settings_view
from datamind_console.views.gtfs_ops_view import render_gtfs_ops_view
from datamind_console.views.revise_current_gtfs_view import render_revise_current_gtfs_view
from datamind_console.views.geo_api_view import render_geo_api_view
from datamind_console.views.geo_api_production_view import render_geo_api_production_view
from datamind_console.views.geo_api_fusion_view import render_geo_api_fusion_view
from datamind_console.views.sample_region_audit_view import render_sample_region_audit_view
from datamind_console.views.ai_access_view import render_ai_access_view

from datamind_console.services.analytics_service import AnalyticsService
from datamind_console.services.audit_service import AuditService
from datamind_console.services.auth_service import AuthService
from datamind_console.ui.i18n import apply_streamlit_i18n, get_lang, set_lang, t

# Ensure child modules/scripts see one active DB_DSN.
_active_boot_dsn = resolve_active_db_dsn(mode=os.getenv("DATA_MODE") or CFG.data_mode)
if _active_boot_dsn:
    os.environ["DB_DSN"] = _active_boot_dsn
    os.environ["DATABASE_URL"] = _active_boot_dsn
if CFG.local_only_mode:
    os.environ["DATA_MODE"] = "local"

STYLE_OPTIONS = ["Light", "Graphite"]
LOCAL_OPERATOR_LABEL = "Local workspace"


def _inject_theme_css(style_name: str) -> None:
    style = str(style_name or "Graphite")
    if style == "Graphite":
        css = """
        <style>
          .stApp {
            background: linear-gradient(180deg, #17191b 0%, #111315 100%);
            color: #f1f3f4;
          }
          section[data-testid="stSidebar"] {
            background: #1c1f22;
            border-right: 1px solid rgba(255,255,255,0.10);
          }
          [data-testid="stMetric"] {
            background: #202428;
            border: 1px solid #3c4248;
            border-radius: 10px;
            padding: 0.45rem 0.65rem;
          }
          .stButton > button[kind="primary"] {
            background: #343a40 !important;
            border: 1px solid #58616a !important;
            color: #ffffff !important;
          }
          .stButton > button[kind="primary"]:hover,
          .stButton > button[kind="primary"]:focus-visible {
            background: #454d55 !important;
            border-color: #aeb8c2 !important;
          }
          .stButton > button[kind="secondary"] {
            background: #22272b !important;
            border: 1px solid #4b545d !important;
            color: #edf0f2 !important;
          }
          .stButton > button[kind="secondary"]:hover,
          .stButton > button[kind="secondary"]:focus-visible {
            background: #30363b !important;
            border-color: #b8c0c8 !important;
          }
          .stButton > button:disabled {
            background: #202326 !important;
            border-color: #34393e !important;
            color: #7d858d !important;
          }
        </style>
        """
    else:
        css = """
        <style>
          .stApp {
            background: linear-gradient(180deg, #f6f8fc 0%, #eef2f8 100%);
            color: #1f2a44;
          }
          section[data-testid="stSidebar"] {
            background: #f2f5fb;
            border-right: 1px solid rgba(20,30,50,0.12);
          }
          [data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #d6deea;
            border-radius: 10px;
            padding: 0.45rem 0.65rem;
          }
          .stButton > button[kind="primary"] {
            background: #23407f !important;
            border: 1px solid #21407f !important;
            color: #ffffff !important;
          }
        </style>
        """
    st.markdown(css, unsafe_allow_html=True)


@st.cache_data(ttl=20, show_spinner=False)
def _cached_sync_updates(limit: int) -> list[dict]:
    if CFG.local_only_mode:
        return []
    return list_sync_events(limit=int(limit))

# ============================================================
# Session helpers
# ============================================================

def _ss_init() -> None:
    ss = st.session_state
    ss.setdefault("local.operator_label", LOCAL_OPERATOR_LABEL)
    ss.setdefault("ui.page", "Dashboard")    # default landing
    ss.setdefault("ui.lang", "en")
    ss.setdefault("ui.style", "Graphite")
    ss.setdefault("ui.ready", True)
    ss.setdefault("data.mode", ("local" if CFG.local_only_mode else (CFG.data_mode or "server")))


def _local_loopback_ui() -> bool:
    """Allow account-free access only when Streamlit is bound to loopback."""
    if os.getenv("ML_DATAMIND_REMOTE_UI", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    try:
        address = str(streamlit_config.get_option("server.address") or "").strip().lower()
    except Exception:
        return False
    return address in {"127.0.0.1", "localhost", "::1"}


def _load_session_from_db(session_id: str) -> bool:
    row = get_session(session_id)
    if not row:
        return False
    if row.get("revoked_at") is not None:
        return False
    if row.get("is_active") is not True:
        return False

    st.session_state["auth.user"] = {
        "user_id": str(row["user_id"]),
        "email": row["email"],
        "display_name": row["display_name"],
    }
    st.session_state["auth.roles"] = list_user_roles(str(row["user_id"]))
    return True


def _logout() -> None:
    ss = st.session_state
    sid = ss.get("auth.session_id")
    if sid:
        try:
            revoke_session(sid)
        except Exception:
            pass

    ss["auth.session_id"] = None
    ss["auth.user"] = None
    ss["auth.roles"] = []
    ss["ui.page"] = "Dashboard"


# ============================================================
# UI components
# ============================================================

def _login_ui() -> None:
    st.title(CFG.app_name)
    st.caption(t("Login required", get_lang(st.session_state)))

    with st.form("login_form", clear_on_submit=False):
        email = st.text_input(t("Email", get_lang(st.session_state)), value="", placeholder="you@domain.com")
        password = st.text_input(t("Password", get_lang(st.session_state)), value="", type="password")
        submit = st.form_submit_button(t("Sign in", get_lang(st.session_state)), use_container_width=True)

    if not submit:
        return

    email = (email or "").strip().lower()
    if not email or not password:
        st.error(t("Enter email and password.", get_lang(st.session_state)))
        return

    try:
        user = verify_password(email, password)
    except Exception as e:
        st.error(f"DB error: {e}")
        return

    if not user:
        st.error(t("Invalid credentials.", get_lang(st.session_state)))
        return

    try:
        sess = create_session(user_id=str(user["user_id"]), ttl_hours=24)
    except Exception as e:
        st.error(f"Could not create session: {e}")
        return

    st.session_state["auth.session_id"] = str(sess["session_id"])
    st.session_state["auth.user"] = {
        "user_id": str(user["user_id"]),
        "email": user["email"],
        "display_name": user["display_name"],
    }
    st.session_state["auth.roles"] = list_user_roles(str(user["user_id"]))

    # Restore workspace (optional)
    ws = get_workspace_state(str(user["user_id"]))
    if ws:
        if ws.get("current_phase") is not None:
            st.session_state["phases.current_phase"] = int(ws["current_phase"])
        if ws.get("current_subtab"):
            st.session_state["phases.current_subtab"] = str(ws["current_subtab"])

    log_audit_event(
        user_id=str(user["user_id"]),
        action="LOGIN",
        payload={"email": user["email"]},
    )

    st.rerun()


def _sidebar_nav() -> str:
    ss = st.session_state
    local_ui = _local_loopback_ui()
    user = ss.get("auth.user") or {}
    roles = ss.get("auth.roles") or []
    workspace_screen = (
        ss.get("ui.page", "Operator Orchestrator") == "Phases"
        and int(ss.get("phases.current_phase") or 1) == 1
        and str(ss.get("p1.section") or "Steps") == "Workspace Nodes"
    )

    with st.sidebar:
        lang_opt = st.selectbox(
            t("Language", get_lang(ss)),
            options=["en", "es"],
            index=(1 if get_lang(ss) == "es" else 0),
            format_func=lambda x: t("Spanish", "es") if x == "es" else t("English", "es"),
            key="ui.lang.pick",
        )
        set_lang(ss, lang_opt)
        current_style = str(ss.get("ui.style") or "Graphite")
        if current_style not in STYLE_OPTIONS:
            current_style = "Graphite"
        style_pick = st.selectbox(
            "UI style",
            options=STYLE_OPTIONS,
            index=STYLE_OPTIONS.index(current_style),
            key="ui.style.pick",
        )
        if style_pick != current_style:
            ss["ui.style"] = style_pick
            st.rerun()
        _inject_theme_css(style_pick)
        st.markdown(f"### {CFG.app_name}")
        if local_ui:
            st.caption("Local workspace · no product account required")
        else:
            st.caption(
                f"User: **{user.get('display_name','')}**  \n"
                f"{user.get('email','')}  \n"
                f"Role: `{', '.join(roles) or '—'}`"
            )

        # ------------------------------------------------------------
        # Data mode (local/server)
        # ------------------------------------------------------------
        local_only_mode = bool(CFG.local_only_mode)
        current_mode = "local" if local_only_mode else str(ss.get("data.mode") or CFG.data_mode or "server").lower()
        if local_only_mode:
            ss["data.mode"] = "local"
            os.environ["DATA_MODE"] = "local"
            local_dsn = os.getenv("LOCAL_DB_DSN") or CFG.db_dsn_local or os.getenv("DB_DSN") or CFG.db_dsn or ""
            if local_dsn:
                os.environ["DB_DSN"] = local_dsn
                os.environ["DATABASE_URL"] = local_dsn
        active_dsn = os.getenv("DB_DSN") or CFG.db_dsn or ""
        parsed = urlparse(active_dsn) if active_dsn else None
        st.markdown("#### Data Mode")
        if local_only_mode:
            mode_pick = "local"
            st.caption("Local-only mode enabled. Server access is disabled.")
        else:
            mode_pick = st.radio(
                "Data source",
                options=["local", "server"],
                index=(0 if current_mode == "local" else 1),
                key="ui.data_mode.pick",
                horizontal=True,
                label_visibility="collapsed",
            )
            if mode_pick != current_mode:
                ss["data.mode"] = mode_pick
                os.environ["DATA_MODE"] = mode_pick
                if mode_pick == "local":
                    local_dsn = os.getenv("LOCAL_DB_DSN") or CFG.db_dsn_local or ""
                    if local_dsn:
                        os.environ["DB_DSN"] = local_dsn
                        os.environ["DATABASE_URL"] = local_dsn
                else:
                    server_dsn = os.getenv("SERVER_DB_DSN") or CFG.db_dsn_server or CFG.db_dsn or ""
                    if server_dsn:
                        os.environ["DB_DSN"] = server_dsn
                        os.environ["DATABASE_URL"] = server_dsn
                st.cache_data.clear()
                st.cache_resource.clear()
                st.rerun()
        badge = "SERVER" if mode_pick == "server" else "LOCAL"
        st.caption(f"Active: `{badge}` | Host: `{parsed.hostname if parsed else 'n/a'}`")

        # ------------------------------------------------------------
        # Sync updates (server log)
        # ------------------------------------------------------------
        st.markdown("#### Sync Updates")
        if local_only_mode:
            st.caption("Disabled in local-only mode.")
        else:
            comment = st.text_input("Sync comment", value="", key="ui.sync.comment", placeholder="What changed?")
            schema_opts = list(DEFAULT_SYNC_SCHEMAS)
            selected_schemas = st.multiselect(
                "Schemas",
                options=schema_opts,
                default=schema_opts,
                key="ui.sync.schemas",
            )
            sync_strategy = st.selectbox(
                "Sync strategy",
                options=["replace", "upsert"],
                index=0 if DEFAULT_SYNC_STRATEGY == "replace" else 1,
                key="ui.sync.strategy",
                help="replace: delete changed server rows by PK then insert local rows. upsert: update/insert only.",
            )
            c_sync1, c_sync2 = st.columns(2)
            with c_sync1:
                do_dry = st.button("Dry Run Local->Server", use_container_width=True, key="ui.sync.dryrun")
            with c_sync2:
                do_push = st.button("Push Local->Server", type="primary", use_container_width=True, key="ui.sync.push")

            if do_dry or do_push:
                try:
                    report = sync_local_to_server(
                        synced_by=(user.get("email") or user.get("display_name") or ss["local.operator_label"]),
                        comment=(comment or "").strip(),
                        schemas=tuple(selected_schemas),
                        dry_run=bool(do_dry),
                        sync_strategy=str(sync_strategy or DEFAULT_SYNC_STRATEGY),
                    )
                    if not local_ui:
                        log_audit_event(
                            user_id=(user.get("user_id") or ""),
                            action=("SYNC_DRY_RUN" if do_dry else "SYNC_PUSH"),
                            payload={
                                "sync_id": (report.get("sync_event") or {}).get("sync_id"),
                                "scanned": report.get("total_scanned"),
                                "inserts": report.get("total_inserts"),
                                "updates": report.get("total_updates"),
                                "applied": report.get("total_applied"),
                                "strategy": report.get("sync_strategy"),
                            },
                        )
                    st.success(
                        f"{'Dry run' if do_dry else 'Push'} ok | "
                        f"scanned={report.get('total_scanned')} "
                        f"inserts={report.get('total_inserts')} "
                        f"updates={report.get('total_updates')} "
                        f"applied={report.get('total_applied')}"
                    )
                    with st.expander("Sync report", expanded=False):
                        st.json(report)
                    st.cache_data.clear()
                    st.session_state["ui.sync.comment"] = ""
                    st.rerun()
                except Exception as e:
                    st.error(f"Sync failed: {e}")

            if st.button("Register Upload (log only)", use_container_width=True, key="ui.sync.register"):
                try:
                    row = log_sync_event(
                    synced_by=(user.get("email") or user.get("display_name") or ss["local.operator_label"]),
                        source_mode=mode_pick,
                        target_mode="server",
                        comment=(comment or "").strip(),
                        status="logged",
                        payload={"page": ss.get("ui.page"), "phase": ss.get("phases.current_phase")},
                    )
                    st.success(f"Logged: {row.get('sync_id')}")
                    st.cache_data.clear()
                    st.session_state["ui.sync.comment"] = ""
                    st.rerun()
                except Exception as e:
                    st.error(f"Sync log failed: {e}")

            try:
                sync_rows = _cached_sync_updates(limit=8)
            except Exception as e:
                sync_rows = []
                st.caption(f"Sync log unavailable: {e}")
            if sync_rows:
                compact = [
                    {
                        "when": r.get("synced_at"),
                        "by": r.get("synced_by"),
                        "from": r.get("source_mode"),
                        "status": r.get("status"),
                        "comment": r.get("comment"),
                    }
                    for r in sync_rows
                ]
                st.dataframe(compact, use_container_width=True, hide_index=True, height=180)
            else:
                st.caption("No sync updates yet.")

        if workspace_screen:
            st.info(t("Workspace fullscreen mode", get_lang(ss)))
            if st.button(t("Exit workspace fullscreen", get_lang(ss)), use_container_width=True):
                ss["p1.section"] = "Steps"
                st.rerun()
            if st.button(t("Go to Dashboard", get_lang(ss)), use_container_width=True):
                ss["ui.page"] = "Dashboard"
                st.rerun()
            page = "Phases"
            ss["ui.page"] = page
        else:
            nav_options = [
                "Dashboard",
                "Phases",
                "GTFS Ops",
                "Revise Current GTFS",
                "Geo API",
                "Geo API Production",
                "Geo API Fusion",
                "Sample Region Audit",
                "AI Assistance",
                "Settings",
            ]
            current_page = ss.get("ui.page", "Dashboard")
            if current_page not in nav_options:
                current_page = "Dashboard"
            page = st.radio(
                t("Navigation", get_lang(ss)),
                options=nav_options,
                index=nav_options.index(current_page),
                format_func=lambda x: t(x, get_lang(ss)),
            )
            ss["ui.page"] = page

        st.divider()

        if not local_ui and st.button(t("Logout", get_lang(ss)), use_container_width=True):
            log_audit_event(
                user_id=(user.get("user_id") or ""),
                action="LOGOUT",
                payload={},
            )
            _logout()
            st.rerun()

    return ss["ui.page"]


# ============================================================
# Safe dependency injection into views
# ============================================================

def _call_view(fn, **kwargs):
    """
    Call a view with only the kwargs it can accept.
    This prevents 'missing required keyword-only args' and
    also prevents 'got an unexpected keyword' errors.
    """
    sig = inspect.signature(fn)
    params = sig.parameters

    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    if has_varkw:
        return fn(**kwargs)

    filtered = {k: v for k, v in kwargs.items() if k in params}
    return fn(**filtered)


# ============================================================
# Main
# ============================================================
def main() -> None:
    st.set_page_config(
        page_title=CFG.app_name,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    _ss_init()
    _inject_theme_css(str(st.session_state.get("ui.style") or "Graphite"))
    apply_streamlit_i18n(st, st.session_state)
    ss = st.session_state

    local_ui = _local_loopback_ui()
    if not local_ui:
        # Remote mode retains the existing account/session controls.
        if ss.get("auth.user") is None and ss.get("auth.session_id"):
            ok = _load_session_from_db(ss["auth.session_id"])
            if not ok:
                _logout()
        if ss.get("auth.user") is None:
            _login_ui()
            return

    # deps (no ctx)
    analytics = AnalyticsService()
    audit = AuditService()
    auth_service = None if local_ui else AuthService()

    # sidebar nav
    page = _sidebar_nav()

    # Persist workspace state (best-effort)
    if not local_ui:
        try:
            upsert_workspace_state(
                user_id=ss["auth.user"]["user_id"],
                current_phase=ss.get("phases.current_phase"),
                current_subtab=ss.get("phases.current_subtab"),
                current_route_id=ss.get("phases.current_route_id"),
                current_stop_id=ss.get("phases.current_stop_id"),
                last_candidate_id=ss.get("phases.last_candidate_id"),
            )
        except Exception:
            pass

    # Route to views
    if page == "Dashboard":
        render_dashboard_view(analytics=analytics, audit=audit)
    elif page == "Phases":
        render_phases_view(analytics=analytics, audit=audit)
    elif page == "Geo API":
        render_geo_api_view(analytics=analytics, audit=audit)
    elif page == "Geo API Production":
        render_geo_api_production_view(analytics=analytics, audit=audit)
    elif page == "Geo API Fusion":
        render_geo_api_fusion_view(analytics=analytics, audit=audit)
    elif page == "GTFS Ops":
        render_gtfs_ops_view(analytics=analytics, audit=audit)
    elif page == "Revise Current GTFS":
        render_revise_current_gtfs_view(analytics=analytics, audit=audit)
    elif page == "Sample Region Audit":
        render_sample_region_audit_view(analytics=analytics, audit=audit)
    elif page == "AI Assistance":
        render_ai_access_view()
    elif page == "Settings":
        render_settings_view(auth=auth_service, audit=audit, console_repo=console_repo, local_mode=local_ui)
    else:
        render_dashboard_view(analytics=analytics, audit=audit)

if __name__ == "__main__":
    main()
