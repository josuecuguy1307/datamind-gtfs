# views/phases/phase4_view.py
"""Phase 4 — Naming (Monitoring)"""
from __future__ import annotations

from typing import Any, Callable, Optional
import inspect
import importlib
import os
import streamlit as st

from datamind_console.phases.phase4_naming.client import _get_phase4_client
from datamind_console.db import console_repo


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


def _init_phase4_state() -> None:
    ss = st.session_state
    ss.setdefault("phase4.route_id", "")
    ss.setdefault("phase4.service_route_id", "")
    ss.setdefault("phase4.direction_id", 0)
    ss.setdefault("phase4.record_id", "")
    ss.setdefault("phase4.last_error", "")
    ss.setdefault("phase4.q", "")
    ss.setdefault("phase4.limit", 50)
    ss.setdefault("phase4.offset", 0)


def _sync_phase4_context_from_route_id(*, client: Any, ss: Any, route_id: str) -> None:
    rid = str(route_id or "").strip()
    if not rid:
        return
    try:
        ctx = client.get_route_direction_context(rid) or {}
    except Exception:
        ctx = {}
    sid = str(ctx.get("service_route_id") or "").strip()
    did = ctx.get("direction_id")
    if sid:
        ss["phase4.service_route_id"] = sid
    if did is not None:
        try:
            ss["phase4.direction_id"] = int(did)
        except Exception:
            pass


def render_phase4_view(*, ctx: Any, analytics: Any = None, audit: Any = None, **_) -> None:
    _init_phase4_state()
    ss = st.session_state

    local_only = os.getenv("DATAMIND_LOCAL_ONLY_MODE", "").strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    local_dsn = os.getenv("LOCAL_DB_DSN") or os.getenv("DATAMIND_LOCAL_DB_DSN") or os.getenv("DB_DSN_LOCAL")
    if local_only and not local_dsn:
        st.subheader("Phase 4 — Naming (Monitoring)")
        st.info(
            "Phase 4 data is unavailable: configure LOCAL_DB_DSN for the local-only session. "
            "Remote database fallbacks are disabled."
        )
        return

    client = _get_phase4_client()
    console = console_repo

    st.subheader("Phase 4 — Naming (Monitoring)")

    deps = {
        "analytics": analytics,
        "audit": audit,
        "client": client,
        "console": console,
    }

    # --- Route Context Selector ---
    st.markdown("### Logical Route Context")
    try:
        bindings = client.list_service_route_direction_bindings(limit=500) or []
    except Exception:
        bindings = []

    if bindings:
        by_sid: dict[str, dict[str, Any]] = {}
        for r in bindings:
            sid = str(r.get("service_route_id") or "").strip()
            if not sid:
                continue
            if sid not in by_sid:
                by_sid[sid] = {
                    "service_route_id": sid,
                    "service_route_ref": str(r.get("service_route_ref") or "").strip(),
                    "service_route_name": str(r.get("service_route_name") or "").strip(),
                    "dirs": {},
                }
            did = int(r.get("direction_id") or 0)
            by_sid[sid]["dirs"][did] = dict(r)

        sid_options = sorted(by_sid.keys())
        current_sid = str(ss.get("phase4.service_route_id") or "").strip()
        if current_sid not in sid_options:
            current_sid = sid_options[0]

        def _sid_label(sid: str) -> str:
            row = by_sid.get(sid) or {}
            ref = str(row.get("service_route_ref") or "").strip()
            name = str(row.get("service_route_name") or "").strip()
            return f"{sid} | ref:{ref or '-'} | name:{name or '-'}"

        chosen_sid = st.selectbox(
            "service_route_id",
            options=sid_options,
            index=sid_options.index(current_sid) if current_sid in sid_options else 0,
            format_func=_sid_label,
            key="phase4.logical_service_route_id",
        )
        ss["phase4.service_route_id"] = chosen_sid

        drows = (by_sid.get(chosen_sid) or {}).get("dirs") or {}
        d_opts = [d for d in (0, 1) if d in drows]
        if d_opts:
            current_d = int(ss.get("phase4.direction_id") or 0)
            if current_d not in d_opts:
                current_d = d_opts[0]

            def _d_label(d: int) -> str:
                row = drows.get(int(d)) or {}
                rid = str(row.get("route_id") or "").strip()
                status = str(row.get("direction_status") or "pending")
                return f"{d} | route:{rid[:8] if rid else '-'} | {status}"

            chosen_d = st.radio(
                "Direction",
                options=d_opts,
                index=d_opts.index(current_d) if current_d in d_opts else 0,
                format_func=_d_label,
                horizontal=True,
                key=f"phase4.logical_direction.{chosen_sid}",
            )
            ss["phase4.direction_id"] = int(chosen_d)
            chosen_route_id = str((drows.get(int(chosen_d)) or {}).get("route_id") or "").strip()
            if chosen_route_id:
                ss["phase4.route_id"] = chosen_route_id
                st.caption(f"Active route_id from direction: `{chosen_route_id}`")
        else:
            st.info("Selected service_route_id has no bound direction route_id.")

    st.divider()

    # --- Monitoring Widgets ---
    w_stats = _load("views.phases.phase4.widgets.semantics_stats_cards", "render_semantics_stats_cards")
    w_queue = _load("views.phases.phase4.widgets.route_queue_table", "render_route_queue_table")
    w_search = _load("views.phases.phase4.widgets.semantics_search_table", "render_semantics_search_table")
    w_detail = _load("views.phases.phase4.widgets.route_semantics_detail_card", "render_route_semantics_detail_card")
    w_insights = _load("views.phases.phase4.widgets.naming_candidate_insights", "render_naming_candidate_insights")
    w_conf_hist = _load("views.phases.phase4.widgets.confidence_distribution_histogram", "render_confidence_distribution_histogram")

    col1, col2 = st.columns([1.0, 1.2])
    with col1:
        _call(w_stats, ctx=ctx, key="p4_stats", **deps)
        st.divider()
        picked_from_queue = _call(w_queue, ctx=ctx, key="p4_queue", **deps)
        if isinstance(picked_from_queue, str) and picked_from_queue.strip():
            ss["phase4.route_id"] = picked_from_queue.strip()
            _sync_phase4_context_from_route_id(client=client, ss=ss, route_id=ss["phase4.route_id"])
    with col2:
        picked_from_search = _call(w_search, ctx=ctx, key="p4_search", **deps)
        if isinstance(picked_from_search, str) and picked_from_search.strip():
            ss["phase4.route_id"] = picked_from_search.strip()
            _sync_phase4_context_from_route_id(client=client, ss=ss, route_id=ss["phase4.route_id"])

        route_id = (ss.get("phase4.route_id") or "").strip()
        if route_id:
            st.divider()
            _call(w_detail, ctx=ctx, route_id=route_id, key="p4_detail", **deps)
            _call(w_insights, ctx=ctx, route_id=route_id, key="p4_insights", **deps)
        else:
            st.info("Pick a route_id (Queue/Search) to see details.")

    st.divider()

    # --- Confidence + Evidence + Matching ---
    w_evidence = _load("views.phases.phase4.tabs.evidence_tab", "render_evidence_tab")
    w_matching = _load("views.phases.phase4.tabs.matching_tab", "render_matching_tab")

    col3, col4 = st.columns([1.0, 1.0])
    with col3:
        _call(w_conf_hist, ctx=ctx, key="p4_conf_hist", **deps)
        st.divider()
        _call(w_evidence, ctx=ctx, **deps)
    with col4:
        _call(w_matching, ctx=ctx, **deps)

    st.divider()

    # --- Workspace Map ---
    w_workspace = _load("views.phases.phase4.widgets.phase4_workspace_map", "render_phase4_workspace_map")
    _call(w_workspace, ctx=ctx, **deps)
