# views/phases_view.py
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

import streamlit as st

from datamind_console.i18n import t
from datamind_console.ui.components.page import page_header
from datamind_console.ui.components.states import error_state
from datamind_console.ui.formatting import format_date
from datamind_console.views.phases.phase1_view import render_phase1_view
from datamind_console.views.phases.phase2_view import render_phase2_view
from datamind_console.views.phases.phase3_view import render_phase3_view
from datamind_console.views.phases.phase4_view import render_phase4_view
from datamind_console.views.phases.phase5_view import render_phase5_view


def _safe_call(view_fn: Callable[..., Any], *, ctx: Any, **deps: Any) -> None:
    """
    Call a view function safely:
    - Pass only supported deps
    - If the call fails due to unexpected kwargs, retry once with ctx-only
    - If the view itself raises, DO NOT call it again (avoids duplicate widget keys)
    """
    try:
        sig = inspect.signature(view_fn)
        params = sig.parameters

        # has **kwargs => pass everything
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            view_fn(ctx=ctx, **deps)
            return

        # else pass only supported kwargs
        safe = {k: v for k, v in deps.items() if k in params}
        view_fn(ctx=ctx, **safe)
        return

    except TypeError as e:
        msg = str(e)

        # Retry ONLY if the unexpected kw is one of the deps we passed to the view.
        # Avoid retrying when TypeError happens *inside* the view.
        if ("unexpected keyword argument" in msg or "got an unexpected keyword argument" in msg) and any(
            f"'{k}'" in msg for k in deps.keys()
        ):
            view_fn(ctx=ctx)
            return

        error_state(e, context_key="error.page.phase_render", phase=getattr(ctx, "current_phase", ""))
        return

    except Exception as e:  # noqa: BLE001 - rendered and logged
        error_state(e, context_key="error.page.phase_render", phase=getattr(ctx, "current_phase", ""))
        return


# -------------------------
# Context (minimal)
# -------------------------
@dataclass
class PhaseContext:
    user: dict[str, Any]
    current_phase: int


# -------------------------
# State (only phase selector)
# -------------------------
PHASES = (1, 2, 3, 4, 5)


def _init_phase_state() -> None:
    """Phase comes from ``?phase=`` (deep link) first, then the session."""
    ss = st.session_state
    ss.setdefault("phases.current_phase", 1)
    requested = st.query_params.get("phase")
    if requested and str(requested).isdigit() and int(requested) in PHASES:
        ss["phases.current_phase"] = int(requested)


def _phase_selector() -> int:
    ss = st.session_state
    current = int(ss.get("phases.current_phase") or 1)
    picked = st.segmented_control(
        t("phases.selector"),
        options=list(PHASES),
        default=current,
        format_func=lambda p: t(f"phase.{p}.label"),
        key="phases.selector",
        label_visibility="collapsed",
        width="stretch",
    )
    if picked and int(picked) != current:
        ss["phases.current_phase"] = int(picked)
        st.query_params["phase"] = str(int(picked))
        st.query_params.pop("tab", None)
        st.rerun()
    st.caption(t(f"phase.{current}.description"))
    return current


@st.cache_data(ttl=120, show_spinner=False)
def _automation_history() -> dict[str, Any]:
    from datamind_console.db.db import query_one

    sessions = query_one(
        "SELECT count(*) AS n, max(created_at) AS last_at FROM console.orchestrator_sessions"
    ) or {}
    autopilot_tables = query_one(
        "SELECT to_regclass('console.pipeline_autopilot_runs') IS NOT NULL AS present"
    ) or {}
    return {
        "sessions": int(sessions.get("n") or 0),
        "last_session": sessions.get("last_at"),
        "autopilot_tables": bool(autopilot_tables.get("present")),
    }


def _automation_panel() -> None:
    """How automated runs actually happen (terminal), with recorded history.

    The console has no orchestrator screen (it was never wired, commit
    1fb6a471). This panel replaces the old pointer to a non-existent
    "Operator Orchestrator" page with facts.
    """
    with st.expander(t("phases.automation.title"), icon=":material/terminal:"):
        st.caption(t("phases.automation.body"))
        st.code("python -m datamind_console.orchestrator.autopilot_worker", language="bash")
        try:
            hist = _automation_history()
        except Exception as exc:  # noqa: BLE001 - rendered and logged
            from datamind_console.ui.components.states import error_state

            error_state(exc, context_key="error.page.phases", retry=False)
            return
        st.caption(
            t(
                "phases.automation.history",
                n=hist["sessions"],
                date=format_date(hist["last_session"]),
            )
        )
        if not hist["autopilot_tables"]:
            st.warning(t("phases.automation.tables_missing"), icon=":material/database:")
        st.caption(t("phases.automation.docs"))


def render_phases_view(*, analytics: Any, audit: Any) -> None:
    _init_phase_state()
    ss = st.session_state

    page_header(t("page.phases.title"), t("page.phases.subtitle"), eyebrow=t("page.phases.eyebrow"))
    current_phase = _phase_selector()
    _automation_panel()

    ctx = PhaseContext(
        user=ss.get("auth.user") or {},
        current_phase=current_phase,
    )

    deps = {"analytics": analytics, "audit": audit}

    # IMPORTANT:
    # Each phase owns its own workspace (catalogs + selections + client wiring).
    if current_phase == 1:
        _safe_call(render_phase1_view, ctx=ctx, **deps)
    elif current_phase == 2:
        _safe_call(render_phase2_view, ctx=ctx, **deps)
    elif current_phase == 3:
        _safe_call(render_phase3_view, ctx=ctx, **deps)
    elif current_phase == 4:
        _safe_call(render_phase4_view, ctx=ctx, **deps)
    else:
        _safe_call(render_phase5_view, ctx=ctx, **deps)
