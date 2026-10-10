"""Operator-reviewed access to local, interactive Claude Code and Codex sessions.

This page does not run an agent inside DataMind. It opens an external
coding CLI in a terminal with a reviewed context file, and only when the
local opt-in flag is set, in LOCAL mode, with a written scope. See
docs/ORCHESTRATION.md, section C.
"""
from __future__ import annotations

from typing import Any

import streamlit as st

from datamind_console.i18n import t
from datamind_console.services.cli_launch_service import (
    LOCAL_CLI_LAUNCH_ENV,
    inspect_cli,
    launch_interactive_cli,
)
from datamind_console.services.workspace_context_service import (
    context_summary,
    ensure_workspace_context,
    write_launch_context,
)
from datamind_console.ui.components.page import page_header


def _handoff_text(context: dict[str, str], scope: str) -> str:
    # The hand-off file is read by the CLI agent, so it stays in English
    # regardless of the UI language.
    return "\n".join(
        [
            "ML DATAMIND GTFS — reviewed operator context",
            f"Active work: {context['work']}",
            f"Input: {context['input']}",
            f"Results destination: {context['results_destination']}",
            f"Region / coverage: {context['region']}",
            f"Regional resource: {context['resource']}",
            f"Requested scope: {scope or 'not yet described'}",
            "Project policy remains in AGENTS.md and CLAUDE.md. This is context, not authorization.",
        ]
    )


def render_ai_access_view(**_: Any) -> None:
    ensure_workspace_context(st.session_state)
    page_header(t("page.ai.title"), t("page.ai.subtitle"), eyebrow=t("page.ai.eyebrow"))

    data_mode = str(st.session_state.get("data.mode") or "server").lower()
    local_mode = data_mode == "local"
    context = context_summary(st.session_state)

    with st.container(border=True):
        st.subheader(t("ai.scope.title"), anchor=False)
        scope = st.text_area(
            t("ai.scope.label"),
            placeholder=t("ai.scope.placeholder"),
            key="ai.launch.scope",
            height=100,
        ).strip()
        st.caption(t("ai.context.title"))
        st.code(_handoff_text(context, scope), language="text")
        st.caption(t("ai.context.note"))

    if not local_mode:
        st.warning(t("ai.server_mode"), icon=":material/cloud:")
    else:
        st.caption(t("ai.flag_required", flag=LOCAL_CLI_LAUNCH_ENV))

    with st.container(border=True):
        st.subheader(t("ai.sessions.title"), anchor=False)
        for provider in ("claude", "codex"):
            availability = inspect_cli(provider)
            status = [
                t("ai.cli_found") if availability.executable_path else t("ai.cli_missing"),
                t("ai.launch_enabled") if availability.launch_enabled else t("ai.launch_disabled"),
                t("ai.host_supported") if availability.platform_supported else t("ai.host_unsupported"),
            ]
            col_main, col_action = st.columns([3, 2])
            with col_main:
                st.markdown(f"**{availability.label}**")
                st.caption(" · ".join(status))
            with col_action:
                requested = st.button(
                    t("ai.open", label=availability.label),
                    key=f"ai.open.{provider}",
                    use_container_width=True,
                    disabled=not (local_mode and availability.ready and bool(scope)),
                    icon=":material/terminal:",
                )
            if requested:
                context_path = write_launch_context(st.session_state, scope)
                result = launch_interactive_cli(provider, context_path=context_path, scope_request=scope)
                if result.requested:
                    st.success(result.message)
                    st.caption(t("ai.context_file", path=context_path))
                else:
                    st.error(result.message)

    st.caption(t("ai.boundary"))
