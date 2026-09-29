from __future__ import annotations

from typing import Any, Callable, Optional
import inspect
import importlib
import streamlit as st

from phases.phase1_nodes.client import _get_phase1_client


def _call(
    render_fn: Optional[Callable[..., Any]],
    *,
    ctx: Any,
    **deps: Any,
) -> Any:
    if render_fn is None:
        return None
    try:
        sig = inspect.signature(render_fn)
        params = sig.parameters
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return render_fn(ctx=ctx, **deps)
        safe_deps = {k: v for k, v in deps.items() if k in params}
        if "ctx" in params:
            return render_fn(ctx=ctx, **safe_deps)
        else:
            return render_fn(**safe_deps)
    except Exception as e:
        st.exception(e)
        return None


def _load(module_path: str, fn_name: str) -> Optional[Callable[..., Any]]:
    try:
        mod = importlib.import_module(module_path)
        fn = getattr(mod, fn_name, None)
        return fn if callable(fn) else None
    except Exception:
        return None


def _init_phase1_state() -> None:
    ss = st.session_state
    ss.setdefault("p1.node_set_id", None)


def render_phase1_view(*, ctx: Any, **_) -> None:
    _init_phase1_state()
    ss = st.session_state

    client = _get_phase1_client("v3.newnodes.fix_json_approve2")
    deps = {"client": client}

    st.subheader("Phase 1 — Nodes (Monitoring)")

    # Node set picker
    w_node_sets = _load("views.phases.phase1.widgets.node_sets_table", "render_node_sets_table")
    picked = _call(w_node_sets, ctx=ctx, **deps)

    if isinstance(picked, str) and picked.strip():
        ss["p1.node_set_id"] = picked.strip()

    node_set_id = ss.get("p1.node_set_id")
    if not node_set_id:
        st.info("Select a Node Set to see details.")
        return

    st.divider()

    # Summary + Quality
    w_summary = _load("views.phases.phase1.widgets.node_set_summary_card", "render_node_set_summary_card")
    w_quality = _load("views.phases.phase1.widgets.quality_plots", "render_quality_plots")
    w_kpis = _load("views.phases.phase1.widgets.resolved_points_kpis", "render_resolved_points_kpis")

    col1, col2 = st.columns([1.3, 1.0])
    with col1:
        _call(w_summary, ctx=ctx, node_set_id=node_set_id, **deps)
    with col2:
        _call(w_quality, ctx=ctx, node_set_id=node_set_id, **deps)

    st.divider()
    _call(w_kpis, ctx=ctx, node_set_id=node_set_id, **deps)
