from __future__ import annotations

from typing import Any
from difflib import SequenceMatcher

import streamlit as st

from ._gtfs_step_preview import render_gtfs_step_output_preview
from datamind_console.i18n import t


def _norm(v: Any) -> str:
    return " ".join(str(v or "").strip().lower().split())


def _sim(a: Any, b: Any) -> float:
    aa = _norm(a)
    bb = _norm(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    return float(SequenceMatcher(None, aa, bb).ratio())


def render_step03_routes_shapes_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown(t("ui.p5.step03_routes_shapes.step_02_insert_routes_from_service"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    route_id = (st.session_state.get("phase5.route_id") or "").strip() or None
    service_route_id = (st.session_state.get("phase5.service_route_id") or "").strip()
    selected_direction = int(st.session_state.get("phase5.direction_id") or 0)

    st.caption(t("ui.p5.step03_routes_shapes.context_service_route_id_service_route", service_route_id=(service_route_id or '-'), route_id=(route_id or '-')))
    if not run_id:
        st.info(t("ui.p5.step03_routes_shapes.create_select_gtfs_id_first_internal"))
        return

    st.caption(t("ui.p5.step03_routes_shapes.this_step_inserts_gtfs_routes_only"))
    st.caption(t("ui.p5.step03_routes_shapes.shapes_generated_next_step_direction_aware"))

    route_scope_ids = []
    if service_route_id:
        try:
            scope_rows = client.list_service_direction_inputs(verified_only=False, limit=2000)
        except Exception:
            scope_rows = []
        route_scope_ids = sorted(
            {
                str(r.get("route_id") or "").strip()
                for r in scope_rows
                if str(r.get("service_route_id") or "").strip() == service_route_id and str(r.get("route_id") or "").strip()
            }
        )
    if not route_scope_ids and route_id:
        route_scope_ids = [route_id]

    st.markdown(t("ui.p5.step03_routes_shapes.insert_mode"))
    mode = st.radio(
        t("ui.p5.step03_routes_shapes.route_insert_mode"),
        options=["one", "all"],
        format_func=lambda v: t(f"p5.route_insert_mode.{v}"),
        horizontal=True,
        key="p5.step02.routes.mode_choice",
    )
    verified_only_bulk = st.checkbox(t("ui.p5.step03_routes_shapes.bulk_verified_routes_only"), value=True, key="p5.step02.routes.verified_only")
    sim_threshold = float(
        st.slider(
            t("ui.p5.step03_routes_shapes.similarity_threshold_bulk_conflict_check_by"),
            min_value=0.70,
            max_value=1.00,
            value=0.92,
            step=0.01,
            key="p5.step02.routes.sim_threshold",
        )
    )

    try:
        all_inputs = client.list_service_direction_inputs(verified_only=bool(verified_only_bulk), limit=5000) or []
    except Exception as e:
        st.error(t("ui.p5.step03_routes_shapes.failed_load_route_inputs_e", e=(e)))
        all_inputs = []

    candidate_rows_by_route: dict[str, dict] = {}
    for r in all_inputs:
        rid = str(r.get("route_id") or "").strip()
        if not rid:
            continue
        if rid not in candidate_rows_by_route:
            candidate_rows_by_route[rid] = dict(r)

    candidate_rows_by_gtfs_route: dict[str, dict] = {}
    for r in all_inputs:
        src = dict(r)
        rid = str(src.get("route_id") or "").strip()
        gid = str(src.get("service_route_id") or rid).strip()
        if not gid or not rid:
            continue
        prev = candidate_rows_by_gtfs_route.get(gid)
        if prev is None:
            candidate_rows_by_gtfs_route[gid] = src
            continue
        # Canonical preview row:
        # 1) route_id == service_route_id, 2) direction 0, 3) stable lexical fallback
        def _rank(x: dict) -> tuple[int, int, str]:
            xr = str(x.get("route_id") or "").strip()
            xg = str(x.get("service_route_id") or xr).strip()
            xd = int(x.get("direction_id") or 0)
            return (0 if xr == xg else 1, 0 if xd == 0 else 1, xr)

        if _rank(src) < _rank(prev):
            candidate_rows_by_gtfs_route[gid] = src

    if mode == "one":
        selected_route_ids = [str(route_id).strip()] if route_id else []
        selected_gtfs_route_ids = [
            str((candidate_rows_by_route.get(rid) or {}).get("service_route_id") or rid).strip()
            for rid in selected_route_ids
            if str(rid).strip()
        ]
        if not selected_route_ids:
            st.info(t("ui.p5.step03_routes_shapes.select_route_route_scope_selector_first"))
    else:
        selected_route_ids = sorted(candidate_rows_by_route.keys())
        selected_gtfs_route_ids = sorted(candidate_rows_by_gtfs_route.keys())
        st.caption(
            t("ui.p5.step03_routes_shapes.bulk_route_candidates_from_our_routes", n_selected_route_ids=(len(selected_route_ids)), n_selected_gtfs_route_ids=(len(selected_gtfs_route_ids)))
        )

    if route_scope_ids and mode == "one":
        st.caption(t("p5.current_service_route_routes", routes=", ".join([f"`{r[:8]}`" for r in route_scope_ids])))

    try:
        existing_gtfs_routes = client.list_gtfs_rows("gtfs_routes", run_id, route_id=None, limit=5000) or []
    except Exception:
        existing_gtfs_routes = []
    by_existing_route_id = {str(r.get("route_id") or "").strip(): dict(r) for r in existing_gtfs_routes if str(r.get("route_id") or "").strip()}
    existing_refs = [(_norm(r.get("route_short_name")), dict(r)) for r in existing_gtfs_routes if _norm(r.get("route_short_name"))]
    existing_names = [(_norm(r.get("route_long_name")), dict(r)) for r in existing_gtfs_routes if _norm(r.get("route_long_name"))]

    preview_key = "p5.step02.routes.insert_preview"
    p1, p2 = st.columns([1, 1])
    with p1:
        if st.button(t("ui.p5.step03_routes_shapes.preview_routes_insert"), use_container_width=True, key="p5.step02.routes.preview"):
            rows = []
            preview_sources = (
                [candidate_rows_by_route.get(rid, {}) for rid in selected_route_ids]
                if mode == "one"
                else [candidate_rows_by_gtfs_route.get(gid, {}) for gid in selected_gtfs_route_ids]
            )
            for src in preview_sources:
                rid = str(src.get("route_id") or "").strip()
                if not rid:
                    continue
                gtfs_route_id = str(src.get("service_route_id") or rid).strip()
                src_name = str(src.get("route_name") or src.get("route_long_name") or "")
                src_ref = str(src.get("route_ref") or src.get("route_short_name") or "")
                status = "ready"
                reason = ""
                matched_route_id = ""
                matched_name = ""
                matched_ref = ""
                score = 0.0
                if gtfs_route_id in by_existing_route_id:
                    ex = by_existing_route_id[gtfs_route_id]
                    status = "exists_route_id"
                    reason = t("p5.reason.route_exists")
                    matched_route_id = str(ex.get("route_id") or "")
                    matched_name = str(ex.get("route_long_name") or "")
                    matched_ref = str(ex.get("route_short_name") or "")
                    score = 1.0
                else:
                    best = None
                    best_score = 0.0
                    src_ref_norm = _norm(src_ref)
                    src_name_norm = _norm(src_name)
                    for ex_ref_norm, ex in existing_refs:
                        if src_ref_norm and ex_ref_norm and src_ref_norm == ex_ref_norm:
                            best = ex
                            best_score = 1.0
                            break
                    if best is None:
                        for ex_name_norm, ex in existing_names:
                            s = _sim(src_name_norm, ex_name_norm)
                            if s > best_score:
                                best = ex
                                best_score = s
                    if best and best_score >= sim_threshold:
                        status = "similar_existing"
                        reason = t("p5.reason.similar_exists")
                        matched_route_id = str(best.get("route_id") or "")
                        matched_name = str(best.get("route_long_name") or "")
                        matched_ref = str(best.get("route_short_name") or "")
                        score = float(best_score)
                rows.append(
                    {
                        "source_route_id": rid,
                        "gtfs_route_id": gtfs_route_id,
                        "direction_id": int(src.get("direction_id") or 0),
                        "route_ref": src_ref,
                        "route_name": src_name,
                        "status": status,
                        "reason": reason,
                        "similarity_score": round(float(score), 4),
                        "matched_route_id": matched_route_id,
                        "matched_route_ref": matched_ref,
                        "matched_route_name": matched_name,
                    }
                )
            st.session_state[preview_key] = rows

    preview_rows = [dict(x) for x in (st.session_state.get(preview_key) or [])]
    ready_bulk_ids = []
    seen_ready_gtfs_ids: set[str] = set()
    for r in preview_rows:
        if str(r.get("status") or "") != "ready":
            continue
        gid = str(r.get("gtfs_route_id") or "").strip()
        rid = str(r.get("source_route_id") or r.get("route_id") or "").strip()
        if gid and gid in seen_ready_gtfs_ids:
            continue
        if gid:
            seen_ready_gtfs_ids.add(gid)
        if rid:
            ready_bulk_ids.append(rid)
    with p2:
        if mode == "one":
            if st.button(t("ui.p5.step03_routes_shapes.run_step_02_insert_selected_route"), type="primary", use_container_width=True, key="p5.step02.routes.run_one"):
                if not selected_route_ids:
                    st.error(t("ui.p5.step03_routes_shapes.no_route_selected"))
                else:
                    try:
                        out = client.run_step_03_routes_only(run_id, route_id=selected_route_ids[0])
                    except Exception as e:
                        st.error(str(e))
                    else:
                        st.success(t("ui.p5.step03_routes_shapes.step_02_completed"))
                        st.json(out)
        else:
            st.checkbox(
                t("ui.p5.step03_routes_shapes.confirm_insert_only_routes_status_ready"),
                value=False,
                key="p5.step02.routes.confirm_bulk",
            )
            if st.button(t("ui.p5.step03_routes_shapes.insert_without_already_existing_routes"), type="primary", use_container_width=True, key="p5.step02.routes.run_bulk"):
                if not preview_rows:
                    st.error(t("ui.p5.step03_routes_shapes.preview_first"))
                elif not st.session_state.get("p5.step02.routes.confirm_bulk"):
                    st.error(t("ui.p5.step03_routes_shapes.enable_confirmation_checkbox_first"))
                else:
                    runs = []
                    errors = []
                    for rid in ready_bulk_ids:
                        try:
                            out_i = client.run_step_03_routes_only(run_id, route_id=rid)
                            runs.append({"route_id": rid, "out": out_i})
                        except Exception as e:
                            errors.append({"route_id": rid, "error": str(e)})
                    st.success(t("ui.p5.step03_routes_shapes.bulk_insert_finished"))
                    summary = {
                        "ok": True,
                        "preview_total": len(preview_rows),
                        "ready_candidates": len(ready_bulk_ids),
                        "inserted_ready_routes": len(runs),
                        "skipped_existing_or_similar": len(preview_rows) - len(ready_bulk_ids),
                        "error_count": len(errors),
                    }
                    st.json(summary)
                    if errors:
                        st.error(t("ui.p5.step03_routes_shapes.some_routes_failed_during_bulk_insert"))
                        st.dataframe(errors[:100], use_container_width=True, hide_index=True, height=220)
                    if runs:
                        st.caption(t("ui.p5.step03_routes_shapes.inserted_routes_first_50"))
                        st.dataframe(
                            [{"route_id": x.get("route_id"), **dict(x.get("out") or {})} for x in runs[:50]],
                            use_container_width=True,
                            hide_index=True,
                            height=220,
                        )

    if preview_rows:
        st.markdown(t("ui.p5.step03_routes_shapes.preview_routes_insert_into_selected_gtfs"))
        n_ready = sum(1 for r in preview_rows if str(r.get("status")) == "ready")
        n_existing = sum(1 for r in preview_rows if str(r.get("status")) == "exists_route_id")
        n_similar = sum(1 for r in preview_rows if str(r.get("status")) == "similar_existing")
        st.caption(t("ui.p5.step03_routes_shapes.ready_n_ready_exists_gtfs_route", n_ready=(n_ready), n_existing=(n_existing), n_similar=(n_similar)))
        st.dataframe(preview_rows, use_container_width=True, hide_index=True, height=240)
    else:
        st.info(t("ui.p5.step03_routes_shapes.click_preview_visualize_what_step_02"))

    st.divider()
    active_preview_scope_ids = (
        [str(x).strip() for x in selected_route_ids if str(x).strip()]
        if mode == "all"
        else ([str(x).strip() for x in route_scope_ids if str(x).strip()] or ([str(route_id).strip()] if route_id else []))
    )
    if mode == "all" and preview_rows:
        active_preview_scope_ids = [str(r.get("source_route_id") or "").strip() for r in preview_rows if str(r.get("source_route_id") or "").strip()]
    active_preview_gtfs_route_ids = set()
    if active_preview_scope_ids:
        for rid in active_preview_scope_ids:
            src = candidate_rows_by_route.get(str(rid), {})
            active_preview_gtfs_route_ids.add(str(src.get("service_route_id") or rid).strip())
        active_preview_gtfs_route_ids = {x for x in active_preview_gtfs_route_ids if x}
    c1 = st.columns(1)[0]
    try:
        route_rows = client.list_gtfs_rows("gtfs_routes", run_id, route_id=None, limit=2000)
        if active_preview_gtfs_route_ids:
            route_rows = [r for r in route_rows if str(r.get("route_id") or "") in active_preview_gtfs_route_ids]
    except Exception:
        route_rows = []
    c1.metric(t("ui.p5.step03_routes_shapes.routes"), len(route_rows))
    st.caption(t("ui.p5.step03_routes_shapes.shapes_generated_next_step_02b_shapes"))

    with st.expander(t("ui.p5.step03_routes_shapes.deletion_log_phase_5_revisions"), expanded=False):
        try:
            logs = client.list_revision_events(export_run_id=run_id, event_type="step_rebuild_delete", limit=200)
        except Exception as e:
            st.error(t("ui.p5.step03_routes_shapes.failed_load_deletion_log_e", e=(e)))
            logs = []
        if logs:
            st.dataframe(logs, use_container_width=True, hide_index=True, height=220)
        else:
            st.info(t("ui.p5.step03_routes_shapes.no_deletion_log_entries_yet"))

    render_gtfs_step_output_preview(
        client=client,
        export_run_id=run_id,
        key="p5.step03.preview",
        route_id=route_id,
        route_ids=selected_route_ids if mode == "all" else route_scope_ids,
        title=t("p5.step02.output_preview_title"),
        default_tables=["gtfs_routes"],
        note=t("p5.step02.output_preview_note"),
    )
