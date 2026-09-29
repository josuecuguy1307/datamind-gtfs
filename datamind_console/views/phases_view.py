# views/phases_view.py
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

import streamlit as st

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

        st.error(f"{view_fn.__name__} failed (TypeError).")
        st.exception(e)
        return

    except Exception as e:
        st.error(f"{view_fn.__name__} crashed while rendering.")
        st.exception(e)
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
def _init_phase_state() -> None:
    ss = st.session_state
    ss.setdefault("phases.current_phase", 1)


def _phase_selector() -> int:
    ss = st.session_state
    c1, c2, c3, c4, c5 = st.columns(5)

    def set_phase(p: int) -> None:
        ss["phases.current_phase"] = p

    with c1:
        st.button("Phase 1 · Nodes", use_container_width=True, on_click=set_phase, args=(1,), key="phases.btn.p1")
    with c2:
        st.button("Phase 2 · Semantics", use_container_width=True, on_click=set_phase, args=(2,), key="phases.btn.p2")
    with c3:
        st.button("Phase 3 · Routes", use_container_width=True, on_click=set_phase, args=(3,), key="phases.btn.p3")
    with c4:
        st.button("Phase 4 · Naming", use_container_width=True, on_click=set_phase, args=(4,), key="phases.btn.p4")
    with c5:
        st.button("Phase 5 · GTFS", use_container_width=True, on_click=set_phase, args=(5,), key="phases.btn.p5")

    return int(ss["phases.current_phase"])


def render_phases_view(*, analytics: Any, audit: Any) -> None:
    _init_phase_state()
    ss = st.session_state
    workspace_screen = (
        int(ss.get("phases.current_phase") or 1) == 1
        and str(ss.get("p1.section") or "Steps") == "Workspace Nodes"
    )

    if workspace_screen:
        ss["phases.current_phase"] = 1
        current_phase = 1
        st.markdown(
            """
            <style>
              .block-container { padding-top: 0.75rem; }
            </style>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.subheader("Manual Phase Workspaces")
        st.caption(
            "Use this screen for manual checkpoints and review tasks. "
            "For automated Phase 1/2/3 execution, blockers, approvals, and resume loops, run the Operator Orchestrator."
        )
        current_phase = _phase_selector()

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
