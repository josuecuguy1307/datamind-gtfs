from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

import streamlit as st


def _short_id(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return "-"
    return s if len(s) <= 12 else f"{s[:8]}..{s[-4:]}"


def _service_route_label(r: dict) -> str:
    sid = str(r.get("service_route_id") or "")
    ref = str(r.get("route_ref") or "-")
    name = str(r.get("route_name") or "-")
    status = str(r.get("route_approval_status") or "pending")
    return f"{sid} | ref:{ref} | name:{name} | status:{status}"


def _build_route_pool(rows: list[dict], jobs: list[dict]) -> Dict[str, str]:
    labels: Dict[str, str] = {}

    for r in rows:
        sid = str(r.get("service_route_id") or "")
        ref = str(r.get("route_ref") or "-")
        name = str(r.get("route_name") or "-")

        rid0 = str(r.get("route_id_0") or "").strip()
        if rid0 and rid0 not in labels:
            labels[rid0] = (
                f"{rid0} | slot:0 | service:{_short_id(sid)} | "
                f"ref:{ref} | name:{name} | "
                f"progress:{int(r.get('progress_0') or 0)} | status:{str(r.get('direction_status_0') or 'pending')}"
            )

        rid1 = str(r.get("route_id_1") or "").strip()
        if rid1 and rid1 not in labels:
            labels[rid1] = (
                f"{rid1} | slot:1 | service:{_short_id(sid)} | "
                f"ref:{ref} | name:{name} | "
                f"progress:{int(r.get('progress_1') or 0)} | status:{str(r.get('direction_status_1') or 'pending')}"
            )

    for j in jobs:
        rid = str(j.get("route_id") or "").strip()
        if not rid or rid in labels:
            continue
        sid = str(j.get("service_route_id") or "").strip() or "-"
        did = j.get("direction_id")
        did_txt = str(did) if did in (0, 1) else "-"
        labels[rid] = (
            f"{rid} | job | service:{_short_id(sid)} | "
            f"dir:{did_txt} | status:{str(j.get('status') or '-')} | "
            f"osm_rel:{str(j.get('osm_relation_id') or '-')}"
        )
    return labels


def render_route_queue_table(*, ctx: Any, client: Any, key: str = "phase3_queue") -> Optional[str]:
    c = client
    ss = st.session_state

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        limit = st.number_input(
            "Limit",
            min_value=10,
            max_value=500,
            value=80,
            step=10,
            key=f"{key}_limit",
        )
    with col2:
        refresh = st.button("Refresh", key=f"{key}_refresh")
    with col3:
        st.caption("Select logical route first, then direction 0/1.")

    if refresh:
        st.cache_data.clear()

    rows = c.list_service_routes(limit=int(limit))
    if not rows:
        st.info("No logical routes found.")
        if st.button("Create new logical route (0/1 slots)", use_container_width=True, key=f"{key}_create_service_route"):
            sid = c.create_service_route(notes="Created from Phase 3 queue.")
            ss["p3.service_route_id"] = sid
            ss["p3.direction_id"] = 0
            ss["p3.route_id"] = None
            st.success(f"Created service_route_id = {sid}")
            st.rerun()
        return None

    st.dataframe(rows, use_container_width=True, height=260, hide_index=True)

    service_ids = [str(r.get("service_route_id")) for r in rows if r.get("service_route_id")]
    by_sid = {str(r.get("service_route_id")): r for r in rows if r.get("service_route_id")}

    # Apply queued UI focus changes before widgets are instantiated.
    pending_sid = ss.pop(f"{key}_pending_selected_service_route", None)
    if pending_sid in service_ids:
        ss[f"{key}_selected_service_route"] = str(pending_sid)
    pending_dir = ss.pop(f"{key}_pending_selected_direction", None)
    if pending_dir in (0, 1):
        ss[f"{key}_selected_direction"] = int(pending_dir)

    current_sid = str(ss.get("p3.service_route_id") or "")
    if current_sid not in service_ids:
        current_sid = service_ids[0]

    selected_sid = st.selectbox(
        "Selected service_route_id",
        options=service_ids,
        index=service_ids.index(current_sid) if current_sid in service_ids else 0,
        format_func=lambda x: _service_route_label(by_sid.get(str(x), {})),
        key=f"{key}_selected_service_route",
    )

    dir_default = int(ss.get("p3.direction_id") or 0)
    if dir_default not in (0, 1):
        dir_default = 0
    selected_dir = st.selectbox(
        "Direction",
        options=[0, 1],
        index=0 if dir_default == 0 else 1,
        key=f"{key}_selected_direction",
    )

    row = by_sid.get(str(selected_sid), {})
    prev_route_id = str(ss.get("p3.route_id") or "").strip() or None

    if int(selected_dir) == 0:
        route_id = str(row.get("route_id_0") or "").strip() or None
        progress = int(row.get("progress_0") or 0)
        d_status = str(row.get("direction_status_0") or "pending")
        geom_source = str(row.get("geom_source_0") or "unknown")
    else:
        route_id = str(row.get("route_id_1") or "").strip() or None
        progress = int(row.get("progress_1") or 0)
        d_status = str(row.get("direction_status_1") or "pending")
        geom_source = str(row.get("geom_source_1") or "unknown")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Direction", int(selected_dir))
    c2.metric("Progress step", int(progress))
    c3.metric("Direction status", d_status)
    c4.metric("Geom source", geom_source)
    try:
        inverse_summary = c.get_inverse_completion_summary(service_route_id=str(selected_sid), include_ready=True) or {}
    except Exception:
        inverse_summary = {}
    if inverse_summary:
        st.caption(
            "Inverse completion: "
            f"unresolved={int(inverse_summary.get('unresolved_rows') or 0)} | "
            f"reliable={int(inverse_summary.get('reliable_candidate_rows') or 0)} | "
            f"plausible={int(inverse_summary.get('plausible_candidate_rows') or 0)} | "
            f"searched={int(inverse_summary.get('search_discovered_rows') or 0) + int(inverse_summary.get('search_materialized_rows') or 0)}"
        )
    if route_id:
        st.caption(f"Active route_id for direction {selected_dir}: `{route_id}`")
    else:
        st.warning(f"Direction {selected_dir} has no route_id yet. Run Step 05 for this direction.")

    ss["p3.service_route_id"] = str(selected_sid)
    ss["p3.direction_id"] = int(selected_dir)
    ss["p3.route_id"] = route_id
    ss["phase3.route_id"] = route_id
    if (prev_route_id or None) != (route_id or None):
        ss["p3.active_sequence_set_id"] = None
        ss["p3.active_sequence_candidate_id"] = None
        ss["p3.active_geometry_set_id"] = None
        ss["p3.active_geometry_candidate_id"] = None
        ss["p3.geometry_candidate_id"] = None
        ss.pop("p3.step20.candidates", None)
        ss.pop("p3.step30.candidates", None)

    with st.expander("Logical route actions", expanded=False):
        st.caption("Cleanup is trash-first. Active routes are archived to papelera and can be restored.")
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Create new logical route (0/1 slots)", use_container_width=True, key=f"{key}_create_more"):
                sid = c.create_service_route(notes="Created from Phase 3 queue.")
                ss["p3.service_route_id"] = sid
                ss["p3.direction_id"] = 0
                ss["p3.route_id"] = None
                st.success(f"Created service_route_id = {sid}")
                st.rerun()
        with b2:
            delete_scope = st.selectbox(
                "Trash scope",
                options=[
                    "Direction only (selected 0/1)",
                    "Whole logical route (0+1)",
                ],
                index=0,
                key=f"{key}_delete_scope",
            )
            confirm_delete = st.checkbox(
                "Confirm move to trash",
                value=False,
                key=f"{key}_delete_confirm",
            )
            can_delete = (route_id is not None) or ("Whole logical route" in delete_scope)
            if st.button("Move to trash", use_container_width=True, key=f"{key}_delete_run", disabled=(not can_delete)):
                if not confirm_delete:
                    st.warning("Enable trash confirmation first.")
                    st.stop()
                if "Whole logical route" in delete_scope:
                    out = c.delete_service_route(
                        str(selected_sid),
                        delete_route_prod=True,
                        delete_node_requests=True,
                        delete_all_related=True,
                        dry_run=False,
                    )
                    if int(out.get("archived_service_route_shell") or 0) > 0 or int(out.get("deleted_service_route") or 0) > 0:
                        ss["p3.service_route_id"] = None
                        ss["p3.direction_id"] = 0
                        ss["p3.route_id"] = None
                    st.cache_data.clear()
                    st.success(
                        f"Trashed logical route {selected_sid} | trashed_route_jobs={int(out.get('trashed_route_jobs') or 0)} "
                        f"| archived_shell={int(out.get('archived_service_route_shell') or 0)}"
                    )
                    st.rerun()
                if not route_id:
                    st.warning("Selected direction has no route_id to trash.")
                    st.stop()
                out = c.delete_route_job(
                    route_id,
                    delete_route_prod=True,
                    delete_node_requests=True,
                    delete_all_related=True,
                    prune_empty_service_routes=True,
                    dry_run=False,
                )
                deleted_sids = [str(x) for x in (out.get("deleted_service_route_ids") or []) if str(x)]
                if str(selected_sid) in set(deleted_sids):
                    ss["p3.service_route_id"] = None
                    ss["p3.direction_id"] = 0
                ss["p3.route_id"] = None
                st.cache_data.clear()
                st.success(
                    f"Trashed route_id {route_id} | trash_id={str(out.get('trash_id') or '-')[:8]} "
                    f"| reset_direction_rows={int(out.get('reset_direction_rows') or 0)} "
                    f"| deleted_service_route_approvals={int(out.get('deleted_service_route_approvals') or 0)} "
                    f"| removed_from_prod={int(out.get('deleted_route_prod_rows') or 0)}"
                )
                st.rerun()

    with st.expander("Trash / restore", expanded=False):
        trash_items = c.list_trash(limit=50) or []
        if not trash_items:
            st.caption("No trashed routes in papelera.")
        else:
            st.dataframe(
                [
                    {
                        "trash_id": str(row.get("trash_id") or ""),
                        "route_id": str(row.get("route_id") or ""),
                        "route_name": row.get("route_name"),
                        "reason": row.get("deletion_reason"),
                        "workflow": row.get("deletion_source_workflow"),
                        "deleted_at": row.get("deleted_at"),
                        "replaced_by_route_id": row.get("replaced_by_route_id"),
                    }
                    for row in trash_items
                ],
                use_container_width=True,
                height=220,
                hide_index=True,
            )
            by_trash_id = {str(row.get("trash_id")): dict(row) for row in trash_items if row.get("trash_id")}
            trash_ids = list(by_trash_id.keys())
            selected_trash_id = st.selectbox(
                "Trash item to restore",
                options=trash_ids,
                format_func=lambda tid: (
                    f"{tid} | route:{str(by_trash_id.get(tid, {}).get('route_id') or '')[:8]} "
                    f"| {str(by_trash_id.get(tid, {}).get('route_name') or by_trash_id.get(tid, {}).get('deletion_reason') or '-')}"
                ),
                key=f"{key}_restore_trash_id",
            )
            restore_notes = st.text_input(
                "Restore notes",
                value="restored via phase3 queue",
                key=f"{key}_restore_notes",
            )
            restore_confirm = st.checkbox(
                "Confirm restore",
                value=False,
                key=f"{key}_restore_confirm",
            )
            if st.button("Restore selected route", use_container_width=True, key=f"{key}_restore_run"):
                if not restore_confirm:
                    st.warning("Enable restore confirmation first.")
                    st.stop()
                out = c.restore_route(
                    selected_trash_id,
                    restore_status="new",
                    notes=restore_notes or "restored via phase3 queue",
                )
                restored_route_id = str(out.get("route_id") or "").strip()
                try:
                    job = c.get_route_job(uuid.UUID(restored_route_id), include_trashed=False) if restored_route_id else {}
                except Exception:
                    job = {}
                ss["p3.service_route_id"] = job.get("service_route_id") or None
                ss["p3.direction_id"] = int(job.get("direction_id") or 0) if job.get("direction_id") in (0, 1) else 0
                ss["p3.route_id"] = restored_route_id or None
                st.cache_data.clear()
                st.success(
                    f"Restored route_id {restored_route_id} into review state | "
                    f"service_route_id={job.get('service_route_id') or '-'} | direction={job.get('direction_id')}"
                )
                st.rerun()

        recent_events = c.list_delete_events(limit=20) or []
        if recent_events:
            st.caption("Recent cleanup audit")
            st.dataframe(
                [
                    {
                        "event_at": row.get("event_at"),
                        "action_type": row.get("action_type"),
                        "route_id": str(row.get("route_id") or ""),
                        "workflow_source": row.get("workflow_source"),
                        "reason": row.get("reason"),
                        "actor": row.get("actor"),
                        "trash_id": str(row.get("trash_id") or ""),
                    }
                    for row in recent_events
                ],
                use_container_width=True,
                height=200,
                hide_index=True,
            )

    with st.expander("Merge routes into one service_route_id", expanded=False):
        st.caption(
            "Pick one preferred service_route_id, then assign one route_id to direction 0 "
            "and another route_id to direction 1."
        )
        target_sid = st.selectbox(
            "Preferred service_route_id (merge target)",
            options=service_ids,
            index=service_ids.index(str(selected_sid)) if str(selected_sid) in service_ids else 0,
            format_func=lambda x: _service_route_label(by_sid.get(str(x), {})),
            key=f"{key}_merge_target_sid",
        )

        target_row = by_sid.get(str(target_sid), {})
        target_rid0 = str(target_row.get("route_id_0") or "").strip()
        target_rid1 = str(target_row.get("route_id_1") or "").strip()
        st.caption(
            f"Current target slots: dir0=`{target_rid0 or '-'}` | dir1=`{target_rid1 or '-'}`"
        )

        jobs = c.list_route_jobs(limit=500) or []
        route_pool = _build_route_pool(rows, jobs)
        route_ids = sorted(route_pool.keys())
        if not route_ids:
            st.info("No route_ids available to merge yet.")
        else:
            st.info(
                "AI merge suggestions and API opinions are handled in `Operator Orchestrator` "
                "(outside Phases UI). This panel is manual apply-only."
            )

            d0_default = target_rid0 if target_rid0 in route_ids else route_ids[0]
            d1_default = target_rid1 if target_rid1 in route_ids else route_ids[min(1, len(route_ids) - 1)]

            merge_dir0_route = st.selectbox(
                "Route for direction 0",
                options=route_ids,
                index=route_ids.index(d0_default),
                format_func=lambda x: route_pool.get(x, x),
                key=f"{key}_merge_dir0_route",
            )
            merge_dir1_route = st.selectbox(
                "Route for direction 1",
                options=route_ids,
                index=route_ids.index(d1_default),
                format_func=lambda x: route_pool.get(x, x),
                key=f"{key}_merge_dir1_route",
            )

            if str(merge_dir0_route) == str(merge_dir1_route):
                st.error("Direction 0 and direction 1 must use different route_ids.")

            merge_confirm = st.checkbox(
                "Confirm merge assignment",
                value=False,
                key=f"{key}_merge_confirm",
            )
            if st.button(
                "Apply merge into target service_route_id",
                use_container_width=True,
                key=f"{key}_merge_apply",
                disabled=(str(merge_dir0_route) == str(merge_dir1_route)),
            ):
                if not merge_confirm:
                    st.warning("Enable merge confirmation first.")
                    st.stop()
                try:
                    out0 = c.bind_route_to_direction(
                        service_route_id=str(target_sid),
                        direction_id=0,
                        route_id=str(merge_dir0_route),
                        geom_source="manual",
                    )
                    out1 = c.bind_route_to_direction(
                        service_route_id=str(target_sid),
                        direction_id=1,
                        route_id=str(merge_dir1_route),
                        geom_source="manual",
                    )
                    ss["p3.service_route_id"] = str(target_sid)
                    ss["p3.direction_id"] = 0
                    ss["p3.route_id"] = str(merge_dir0_route)
                    ss[f"{key}_pending_selected_service_route"] = str(target_sid)
                    ss[f"{key}_pending_selected_direction"] = 0
                    st.success(
                        "Merged into target service_route_id: "
                        f"dir0={out0.get('route_id')} | dir1={out1.get('route_id')}"
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Merge failed: {e}")

    return route_id
