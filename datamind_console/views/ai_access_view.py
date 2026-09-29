"""Operator-reviewed access to local, interactive Claude Code and Codex sessions."""
from __future__ import annotations

from typing import Any

import streamlit as st

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


def _handoff_text(context: dict[str, str], scope: str) -> str:
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
    st.subheader("AI Assistance")
    st.caption("Review a current work context and request scope before opening an interactive local coding CLI.")

    data_mode = str(st.session_state.get("data.mode") or "server").lower()
    local_mode = data_mode == "local"
    context = context_summary(st.session_state)
    scope = st.text_area(
        "Requested scope for this session",
        placeholder="Example: review this GTFS validation report only; do not publish or modify data.",
        key="ai.launch.scope",
        height=100,
    ).strip()

    st.markdown("#### Context that will be delivered after you click Open")
    st.code(_handoff_text(context, scope), language="text")
    st.caption("No context, prompt, or provider session is created on page open or when a work is selected. A small local context file is created only after an enabled Open button is clicked.")

    if not local_mode:
        st.warning("Terminal launch is unavailable while data mode is SERVER. Switch to LOCAL on this machine to enable the local-only control.")
    else:
        st.info(f"Interactive launch also requires {LOCAL_CLI_LAUNCH_ENV}=true in this local process environment.")

    st.markdown("#### Interactive local sessions")
    for provider in ("claude", "codex"):
        availability = inspect_cli(provider)
        status = []
        status.append("CLI found" if availability.executable_path else "CLI not found on PATH")
        status.append("local launch enabled" if availability.launch_enabled else "local launch disabled")
        status.append("macOS host" if availability.platform_supported else "unsupported host")

        col_main, col_action = st.columns([3, 2])
        with col_main:
            st.markdown(f"**{availability.label}**")
            st.caption(" · ".join(status))
        with col_action:
            requested = st.button(
                f"Open {availability.label} with reviewed context",
                key=f"ai.open.{provider}",
                use_container_width=True,
                disabled=not (local_mode and availability.ready and bool(scope)),
            )
        if requested:
            context_path = write_launch_context(st.session_state, scope)
            result = launch_interactive_cli(provider, context_path=context_path, scope_request=scope)
            if result.requested:
                st.success(result.message)
                st.caption(f"Context file created for this launch: `{context_path}`")
            else:
                st.error(result.message)

    st.markdown("#### Safety boundary")
    st.caption(
        "Opening a terminal does not grant project, cloud, database, deployment, or Git permissions. "
        "The CLI retains its own authentication and approval prompts; this screen does not bypass them."
    )
