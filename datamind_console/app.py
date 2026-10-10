# datamind_console/app.py
from __future__ import annotations

import sys
import os
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st
from streamlit import config as streamlit_config

ROOT = Path(__file__).resolve().parents[1]

# Absolute import roots (EXACTLY ONCE, BEFORE ANY OTHER PROJECT IMPORT)
sys.path.insert(0, str(ROOT))                     # ML DATAMIND
sys.path.insert(0, str(ROOT / "phase4_naming"))
sys.path.insert(0, str(ROOT / "phase2_semantics"))
sys.path.insert(0, str(ROOT / "phase3_routes"))
sys.path.insert(0, str(ROOT / "phase5_gtfs"))

from datamind_console.common.env import load_project_env  # noqa: E402

load_project_env(ROOT / ".env")  # existing process variables win

from datamind_console.common.config import CFG, resolve_active_db_dsn  # noqa: E402
from datamind_console.db.console_repo import (  # noqa: E402
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
import datamind_console.db.console_repo as console_repo  # noqa: E402

from datamind_console.views.dashboard_view import render_dashboard_view  # noqa: E402
from datamind_console.views.review_view import render_review_view  # noqa: E402
from datamind_console.views.review_synthetic_stops_view import render_review_synthetic_stops_view  # noqa: E402
from datamind_console.views.review_proposals_view import render_review_proposals_view  # noqa: E402
from datamind_console.views.phases_view import render_phases_view  # noqa: E402
from datamind_console.views.settings_view import render_settings_view  # noqa: E402
from datamind_console.views.gtfs_ops_view import render_gtfs_ops_view  # noqa: E402
from datamind_console.views.revise_current_gtfs_view import render_revise_current_gtfs_view  # noqa: E402
from datamind_console.views.geo_api_view import render_geo_api_view  # noqa: E402
from datamind_console.views.geo_api_production_view import render_geo_api_production_view  # noqa: E402
from datamind_console.views.geo_api_fusion_view import render_geo_api_fusion_view  # noqa: E402
from datamind_console.views.sample_region_audit_view import render_sample_region_audit_view  # noqa: E402
from datamind_console.views.ai_access_view import render_ai_access_view  # noqa: E402

from datamind_console.services.analytics_service import AnalyticsService  # noqa: E402
from datamind_console.services.audit_service import AuditService  # noqa: E402
from datamind_console.services.auth_service import AuthService  # noqa: E402
from datamind_console.i18n import LANGUAGES, get_lang, init_language, language_label, option_label, set_lang, t  # noqa: E402
from datamind_console.ui import navigation  # noqa: E402
from datamind_console.ui.components.states import guarded  # noqa: E402
from datamind_console.ui.style import apply_global_style  # noqa: E402
from datamind_console.ui.theme import is_dark  # noqa: E402

# Ensure child modules/scripts see one active DB_DSN.
_active_boot_dsn = resolve_active_db_dsn(mode=os.getenv("DATA_MODE") or CFG.data_mode)
if _active_boot_dsn:
    os.environ["DB_DSN"] = _active_boot_dsn
    os.environ["DATABASE_URL"] = _active_boot_dsn
if CFG.local_only_mode:
    os.environ["DATA_MODE"] = "local"

LOGO_PATH = Path(__file__).resolve().parent / "ui" / "assets" / "datamind_logo_blue.svg"


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
    ss.setdefault("local.operator_label", t("sidebar.local_operator"))
    ss.setdefault("data.mode", ("local" if CFG.local_only_mode else (CFG.data_mode or "server")))
    # Compatibility for older views that still read "ui.style" to pick map
    # basemaps; the actual theme comes from .streamlit/config.toml.
    ss["ui.style"] = "Graphite" if is_dark() else "Light"


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


# ============================================================
# UI components
# ============================================================

def _login_ui() -> None:
    st.title(CFG.app_name, anchor=False)
    st.caption(t("auth.login_required"))

    with st.form("login_form", clear_on_submit=False):
        email = st.text_input(t("auth.email"), value="", placeholder=t("ui.misc.app.you_domain_com"))
        password = st.text_input(t("auth.password"), value="", type="password")
        submit = st.form_submit_button(t("auth.sign_in"), use_container_width=True)

    if not submit:
        return

    email = (email or "").strip().lower()
    if not email or not password:
        st.error(t("auth.enter_credentials"))
        return

    try:
        user = verify_password(email, password)
    except Exception as e:
        st.error(t("auth.db_error", error=e))
        return

    if not user:
        st.error(t("auth.invalid_credentials"))
        return

    try:
        sess = create_session(user_id=str(user["user_id"]), ttl_hours=24)
    except Exception as e:
        st.error(t("auth.session_error", error=e))
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


def _language_switch() -> None:
    current = get_lang()
    picked = st.segmented_control(
        t("sidebar.language"),
        options=list(LANGUAGES),
        default=current,
        format_func=language_label,
        key="ui.lang.pick",
    )
    if picked and picked != current:
        set_lang(picked)
        st.rerun()


def _data_source_block(local_ui: bool) -> str:
    """Compact data-source status; returns the active mode ('local'/'server')."""
    ss = st.session_state
    local_only_mode = bool(CFG.local_only_mode)
    current_mode = "local" if local_only_mode else str(ss.get("data.mode") or CFG.data_mode or "server").lower()
    if local_only_mode:
        ss["data.mode"] = "local"
        os.environ["DATA_MODE"] = "local"
        local_dsn = os.getenv("LOCAL_DB_DSN") or CFG.db_dsn_local or os.getenv("DB_DSN") or CFG.db_dsn or ""
        if local_dsn:
            os.environ["DB_DSN"] = local_dsn
            os.environ["DATABASE_URL"] = local_dsn

    st.html(f'<div class="dm-kicker">{t("sidebar.data_source")}</div>')
    if local_only_mode:
        mode_pick = "local"
    else:
        mode_pick = st.segmented_control(
            t("sidebar.data_source"),
            options=["local", "server"],
            default=current_mode,
            format_func=lambda m: t(f"status.data_mode.{m}"),
            key="ui.data_mode.pick",
            label_visibility="collapsed",
        ) or current_mode
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

    active_dsn = os.getenv("DB_DSN") or CFG.db_dsn or ""
    parsed = urlparse(active_dsn) if active_dsn else None
    host = (parsed.hostname if parsed else None) or "n/a"
    dbname = (parsed.path.lstrip("/") if parsed and parsed.path else "") or "—"
    with st.container(horizontal=True, gap="small"):
        st.badge(t(f"status.data_mode.{mode_pick}"), color="blue" if mode_pick == "local" else "violet")
        st.caption(f"{host} · {dbname}")
    if local_only_mode:
        st.caption(t("sidebar.local_only_note"))
    return mode_pick


def _server_sync_block(mode_pick: str, local_ui: bool, user: dict) -> None:
    """Server sync controls (unchanged behaviour); hidden in local-only mode."""
    ss = st.session_state
    if CFG.local_only_mode:
        return
    with st.expander(t("sidebar.sync.title"), expanded=False, icon=":material/sync:"):
        comment = st.text_input(t("sidebar.sync.comment"), value="", key="ui.sync.comment", placeholder=t("sidebar.sync.comment_placeholder"))
        schema_opts = list(DEFAULT_SYNC_SCHEMAS)
        selected_schemas = st.multiselect(
            t("sidebar.sync.schemas"),
            options=schema_opts,
            default=schema_opts,
            key="ui.sync.schemas",
        )
        sync_strategy = st.selectbox(
            t("sidebar.sync.strategy"),
            options=["replace", "upsert"],
            index=0 if DEFAULT_SYNC_STRATEGY == "replace" else 1,
            key="ui.sync.strategy",
            format_func=option_label("sync_strategy"),
            help=t("sidebar.sync.strategy_help"),
        )
        c_sync1, c_sync2 = st.columns(2)
        with c_sync1:
            do_dry = st.button(t("sidebar.sync.dry_run"), use_container_width=True, key="ui.sync.dryrun")
        with c_sync2:
            do_push = st.button(t("sidebar.sync.push"), type="primary", use_container_width=True, key="ui.sync.push")

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
                    t(
                        "sidebar.sync.result",
                        kind=t("sidebar.sync.dry_run") if do_dry else t("sidebar.sync.push"),
                        scanned=report.get("total_scanned"),
                        inserts=report.get("total_inserts"),
                        updates=report.get("total_updates"),
                        applied=report.get("total_applied"),
                    )
                )
                with st.expander(t("sidebar.sync.report"), expanded=False):
                    st.json(report)
                st.cache_data.clear()
                st.session_state["ui.sync.comment"] = ""
                st.rerun()
            except Exception as e:
                st.error(t("sidebar.sync.failed", error=e))

        if st.button(t("sidebar.sync.register"), use_container_width=True, key="ui.sync.register"):
            try:
                row = log_sync_event(
                    synced_by=(user.get("email") or user.get("display_name") or ss["local.operator_label"]),
                    source_mode=mode_pick,
                    target_mode="server",
                    comment=(comment or "").strip(),
                    status="logged",
                    payload={"phase": ss.get("phases.current_phase")},
                )
                st.success(t("sidebar.sync.logged", sync_id=row.get("sync_id")))
                st.cache_data.clear()
                st.session_state["ui.sync.comment"] = ""
                st.rerun()
            except Exception as e:
                st.error(t("sidebar.sync.log_failed", error=e))

        try:
            sync_rows = _cached_sync_updates(limit=8)
        except Exception as e:
            sync_rows = []
            st.caption(t("sidebar.sync.unavailable", error=e))
        if sync_rows:
            compact = [
                {
                    t("col.when"): r.get("synced_at"),
                    t("col.by"): r.get("synced_by"),
                    t("col.from"): r.get("source_mode"),
                    t("col.status"): r.get("status"),
                    t("col.comment"): r.get("comment"),
                }
                for r in sync_rows
            ]
            st.dataframe(compact, use_container_width=True, hide_index=True, height=180)
        else:
            st.caption(t("sidebar.sync.none"))


def _sidebar_footer(local_ui: bool) -> None:
    ss = st.session_state
    user = ss.get("auth.user") or {}
    roles = ss.get("auth.roles") or []
    with st.sidebar:
        st.divider()
        _language_switch()
        mode_pick = _data_source_block(local_ui)
        _server_sync_block(mode_pick, local_ui, user)
        if local_ui:
            st.caption(t("sidebar.local_workspace"))
        else:
            st.caption(
                t(
                    "sidebar.signed_in",
                    name=user.get("display_name", ""),
                    email=user.get("email", ""),
                    roles=", ".join(roles) or "—",
                )
            )
            if st.button(t("auth.logout"), use_container_width=True, icon=":material/logout:"):
                log_audit_event(user_id=(user.get("user_id") or ""), action="LOGOUT", payload={})
                _logout()
                st.rerun()


# ============================================================
# Pages
# ============================================================

def _page_specs(*, analytics: AnalyticsService, audit: AuditService, auth_service, local_ui: bool) -> list[navigation.PageSpec]:
    P = navigation.PageSpec
    return [
        P("overview", "overview", "nav.overview", ":material/space_dashboard:", "nav.section.operate",
          lambda: guarded(render_dashboard_view, analytics=analytics, audit=audit, context_key="error.page.overview"), default=True),
        P("review", "review", "nav.review", ":material/fact_check:", "nav.section.review",
          lambda: guarded(render_review_view, context_key="error.page.review")),
        P("review_synthetic", "review-synthetic-stops", "nav.review_synthetic", ":material/add_location_alt:", "nav.section.review",
          lambda: guarded(render_review_synthetic_stops_view, context_key="error.page.review")),
        P("review_proposals", "review-proposals", "nav.review_proposals", ":material/rule:", "nav.section.review",
          lambda: guarded(render_review_proposals_view, context_key="error.page.review")),
        P("phases", "phases", "nav.phases", ":material/account_tree:", "nav.section.operate",
          lambda: guarded(render_phases_view, analytics=analytics, audit=audit, context_key="error.page.phases")),
        P("gtfs", "gtfs", "nav.gtfs", ":material/directions_bus:", "nav.section.operate",
          lambda: guarded(render_gtfs_ops_view, analytics=analytics, audit=audit, context_key="error.page.gtfs")),
        P("revise_gtfs", "revise-gtfs", "nav.revise_gtfs", ":material/edit_road:", "nav.section.operate",
          lambda: guarded(render_revise_current_gtfs_view, analytics=analytics, audit=audit, context_key="error.page.revise_gtfs")),
        P("audit_sample_region", "audit-sample-region", "nav.audit_sample_region", ":material/query_stats:", "nav.section.audit",
          lambda: guarded(render_sample_region_audit_view, analytics=analytics, audit=audit, context_key="error.page.audit")),
        P("geo_api", "geo-api", "nav.geo_api", ":material/api:", "nav.section.integrate",
          lambda: guarded(render_geo_api_view, analytics=analytics, audit=audit, context_key="error.page.integration")),
        P("geo_api_production", "geo-api-production", "nav.geo_api_production", ":material/dns:", "nav.section.integrate",
          lambda: guarded(render_geo_api_production_view, analytics=analytics, audit=audit, context_key="error.page.integration")),
        P("geo_api_fusion", "geo-api-fusion", "nav.geo_api_fusion", ":material/merge:", "nav.section.integrate",
          lambda: guarded(render_geo_api_fusion_view, analytics=analytics, audit=audit, context_key="error.page.integration")),
        P("ai_assistance", "ai-assistance", "nav.ai_assistance", ":material/terminal:", "nav.section.system",
          lambda: guarded(render_ai_access_view, context_key="error.page.ai")),
        P("settings", "settings", "nav.settings", ":material/settings:", "nav.section.system",
          lambda: guarded(
              render_settings_view,
              auth=auth_service, audit=audit, console_repo=console_repo, local_mode=local_ui,
              context_key="error.page.settings",
          )),
    ]


# ============================================================
# Main
# ============================================================
def main() -> None:
    st.set_page_config(
        page_title=CFG.app_name,
        page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else None,
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_language()
    _ss_init()
    apply_global_style()
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

    if LOGO_PATH.exists():
        st.logo(str(LOGO_PATH), size="large", icon_image=str(LOGO_PATH))

    analytics = AnalyticsService()
    audit = AuditService()
    auth_service = None if local_ui else AuthService()

    current = navigation.build(_page_specs(analytics=analytics, audit=audit, auth_service=auth_service, local_ui=local_ui))
    _sidebar_footer(local_ui)

    # Persist workspace state (best-effort, remote accounts only)
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

    current.run()


if __name__ == "__main__":
    main()
