from __future__ import annotations

import html

from dataclasses import dataclass
from typing import Optional, Literal, Dict, Any, Sequence

import streamlit as st


AppTab = Literal["Dashboard", "Phases", "Geo API", "Revise Current GTFS", "Training", "Insights", "Settings"]
PhaseKey = Literal["Phase 1", "Phase 2", "Phase 3", "Phase 4"]


@dataclass
class WorkspaceBarState:
    phase: Optional[int] = None
    subtab: Optional[str] = None
    route_id: Optional[str] = None
    stop_id: Optional[str] = None
    candidate_id: Optional[str] = None
    status: Optional[str] = None


def init_page(
    title: str = "ML DATAMIND GTFS",
    layout: Literal["centered", "wide"] = "wide",
) -> None:
    st.set_page_config(page_title=title, layout=layout)


def set_app_state_defaults() -> None:
    if "active_tab" not in st.session_state:
        st.session_state.active_tab = "Dashboard"

    if "active_phase" not in st.session_state:
        st.session_state.active_phase = "Phase 1"

    if "active_phase_subtab" not in st.session_state:
        st.session_state.active_phase_subtab = "Explore"

    if "workspace" not in st.session_state:
        st.session_state.workspace = WorkspaceBarState()


def top_header(
    app_name: str = "ML DATAMIND GTFS",
    subtitle: str = "GTFS operations workspace",
    right_tag: Optional[str] = None,
) -> None:
    col1, col2 = st.columns([0.78, 0.22])
    with col1:
        st.markdown(f"## {app_name}")
        st.markdown(f"<div style='opacity:0.70; margin-top:-10px;'>{subtitle}</div>", unsafe_allow_html=True)
    with col2:
        if right_tag:
            st.markdown(
                f"""
                <div style="
                    width:100%;
                    text-align:right;
                    padding-top:18px;
                ">
                  <span style="
                    border:1px solid rgba(255,255,255,0.14);
                    padding:6px 10px;
                    border-radius:999px;
                    opacity:0.85;
                    font-size:12px;
                  ">{right_tag}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )
    st.divider()


def nav_tabs(
    tabs: Sequence[AppTab] = ("Dashboard", "Phases", "Geo API", "Revise Current GTFS", "Training", "Insights", "Settings"),
) -> AppTab:
    col = st.columns(len(tabs))
    for i, t in enumerate(tabs):
        with col[i]:
            is_active = st.session_state.active_tab == t
            if st.button(
                t,
                use_container_width=True,
                type="primary" if is_active else "secondary",
            ):
                st.session_state.active_tab = t
    return st.session_state.active_tab


def workspace_bar(
    state: Optional[WorkspaceBarState] = None,
) -> WorkspaceBarState:
    if state is None:
        state = st.session_state.workspace

    pill = []
    if state.phase:
        pill.append(f"Phase {state.phase}")
    if state.subtab:
        pill.append(str(state.subtab))
    if state.status:
        pill.append(str(state.status))

    left = " • ".join(pill) if pill else "No active context"

    route_txt = f"route: {state.route_id}" if state.route_id else None
    stop_txt = f"stop: {state.stop_id}" if state.stop_id else None
    cand_txt = f"candidate: {state.candidate_id}" if state.candidate_id else None
    right = " | ".join([x for x in [route_txt, stop_txt, cand_txt] if x]) or "—"

    st.markdown(
        f"""
        <div style="
          border: 1px solid rgba(255,255,255,0.12);
          border-radius: 16px;
          padding: 10px 12px;
          margin-top: 6px;
          margin-bottom: 14px;
          background: rgba(255,255,255,0.04);
        ">
          <div style="display:flex; justify-content:space-between; gap:14px;">
            <div style="font-size:12px; opacity:0.80; font-weight:650;">
              {left}
            </div>
            <div style="font-size:12px; opacity:0.70; text-align:right;">
              {right}
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return state


def phase_switcher() -> PhaseKey:
    st.markdown("### Phase Switcher")
    cols = st.columns(4)

    buttons = ["Phase 1", "Phase 2", "Phase 3", "Phase 4"]
    for i, label in enumerate(buttons):
        with cols[i]:
            active = st.session_state.active_phase == label
            if st.button(
                label,
                use_container_width=True,
                type="primary" if active else "secondary",
            ):
                st.session_state.active_phase = label

    return st.session_state.active_phase


def phase_subtabs(phase_key: PhaseKey) -> str:
    phase_map: Dict[str, Sequence[str]] = {
        "Phase 1": ("Explore", "Candidates", "Metrics", "Labeling", "Publish"),
        "Phase 2": ("Search", "Evidence", "Conflicts", "Editor", "Publish"),
        "Phase 3": ("Route Picker", "Stop Order", "Geometry", "Diagnostics", "Publish"),
        "Phase 4": ("Evidence", "Candidates", "Editor", "Consistency", "Publish"),
    }

    options = phase_map.get(phase_key, ("Explore",))
    cols = st.columns(len(options))

    for i, name in enumerate(options):
        with cols[i]:
            active = st.session_state.active_phase_subtab == name
            if st.button(
                name,
                use_container_width=True,
                type="primary" if active else "secondary",
            ):
                st.session_state.active_phase_subtab = name

    return st.session_state.active_phase_subtab


def right_panel_info(data: Dict[str, Any], title: str = "Inspector") -> None:
    st.markdown(f"### {title}")
    if not data:
        st.info("No details.")
        return

    for k, v in data.items():
        st.markdown(f"**{k}**")
        st.write(v)


def section(title: str, description: Optional[str] = None) -> None:
    st.markdown(f"### {title}")
    if description:
        st.markdown(f"<div style='opacity:0.70; margin-top:-8px;'>{description}</div>", unsafe_allow_html=True)


def footer(note: str = "ML DATAMIND GTFS • Internal") -> None:
    st.divider()
    st.markdown(f"<div style='opacity:0.60; font-size:12px;'>{note}</div>", unsafe_allow_html=True)


# ------------------------------------------------------------
# Backwards-compatible API (expected by older views)
# ------------------------------------------------------------

def _user_tag(user: Any) -> Optional[str]:
    """Best-effort tag for top right (supports dict or object user)."""
    if user is None:
        return None
    if isinstance(user, dict):
        return user.get("display_name") or user.get("username") or user.get("email")
    return (
        getattr(user, "display_name", None)
        or getattr(user, "username", None)
        or getattr(user, "email", None)
    )


def render_app_header(user: Any = None) -> None:
    """Compatibility wrapper for older imports."""
    top_header(
        app_name="ML DATAMIND GTFS",
        subtitle="GTFS operations workspace",
        right_tag=_user_tag(user),
    )


def render_sidebar(user: Any = None) -> AppTab:
    """
    Compatibility wrapper. Renders navigation in sidebar and returns active tab.
    """
    with st.sidebar:
        st.markdown("## Navigation")
        active = nav_tabs()
        st.divider()

        who = _user_tag(user)
        if who:
            who = html.escape(str(who))  # el nombre llega de la BD y va a HTML crudo
            st.markdown(
                f"<div style='opacity:0.70; font-size:12px;'>Signed in as <b>{who}</b></div>",
                unsafe_allow_html=True,
            )
    return active
