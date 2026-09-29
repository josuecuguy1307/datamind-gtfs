# views/phases/phase2_view.py
"""Phase 2 — Semantic Geocoder (Monitoring)"""
from __future__ import annotations

from typing import Any, Callable, Optional
import inspect
import importlib

import streamlit as st

from datamind_console.phases.phase2_semantics.client import Phase2Client


def _load(module_path: str, fn_name: str) -> Optional[Callable[..., Any]]:
    try:
        mod = importlib.import_module(module_path)
        fn = getattr(mod, fn_name, None)
        return fn if callable(fn) else None
    except Exception:
        return None


def _call(render_fn: Optional[Callable[..., Any]], *, ctx: Any, **deps: Any) -> Any:
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
        return render_fn(**safe_deps)
    except Exception as e:
        st.exception(e)
        return None


def render_phase2_view(*, ctx: Any, analytics: Any = None, audit: Any = None, **_) -> None:
    if not hasattr(ctx, "phase2") or getattr(ctx, "phase2") is None:
        ctx.phase2 = Phase2Client()

    phase2 = ctx.phase2
    st.subheader("Phase 2 — Semantic Geocoder (Monitoring)")

    # Prod place summary
    w_prod = _load("views.phases.phase2.tabs.prod_place_summary_table", "render_prod_place_summary_table")
    _call(w_prod, ctx=ctx, client=phase2, phase2=phase2)

    st.divider()

    # Place points map
    w_map = _load("views.phases.phase2.widgets.place_points_map", "render_place_points_map")
    _call(w_map, ctx=ctx, client=phase2, phase2=phase2, mode="prod")

    st.divider()

    # Evidence coverage KPIs
    w_kpis = _load("views.phases.phase2.widgets.evidence_coverage_kpis", "render_evidence_coverage_kpis")
    _call(w_kpis, ctx=ctx, client=phase2, phase2=phase2)
