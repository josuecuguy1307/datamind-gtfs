from __future__ import annotations

from typing import Any, Dict, Optional
import streamlit as st

from datamind_console.phases.phase4_naming.client import _get_phase4_client
from datamind_console.i18n import t

def render_route_queue_table(*, limit: int = 50, offset: int = 0, key: str = "p4_queue") -> Optional[str]:
    """
    Shows Phase 4 pipeline queue status via client.list_pending().
    Returns selected route_id (or None).
    """
    c = _get_phase4_client()

    try:
        rows = c.list_pending(limit=limit, offset=offset)
    except Exception as e:
        st.error(t("ui.p124.route_queue_table.failed_list_pending_e", e=(e)))
        return None

    data: list[Dict[str, Any]] = []
    for p in rows:
        data.append(
            {
                "route_id": p.route_id,
                "service_route_id": (p.raw or {}).get("service_route_id"),
                "direction_id": (p.raw or {}).get("direction_id"),
                "label": p.label,
                "score": p.score,
                "status": p.status,
                "operator": (p.raw or {}).get("operator_name"),
                "ref": (p.raw or {}).get("route_ref"),
                "human_verified": (p.raw or {}).get("human_verified"),
            }
        )

    st.dataframe(data, use_container_width=True, hide_index=True)

    route_ids = [str(d.get("route_id")) for d in data if d.get("route_id")]
    if route_ids:
        pick = st.selectbox(t("ui.p124.route_queue_table.open_route_id"), route_ids, key=f"{key}_route_id")
        selected = pick.strip() if pick else None
        if selected:
            row = next((r for r in data if str(r.get("route_id")) == selected), {})
            st.session_state["phase4.route_id"] = selected
            st.session_state["phase4.service_route_id"] = str(row.get("service_route_id") or "").strip() or ""
            try:
                dval = row.get("direction_id")
                st.session_state["phase4.direction_id"] = int(dval) if dval is not None else 0
            except Exception:
                st.session_state["phase4.direction_id"] = 0

        with st.expander(t("ui.p124.route_queue_table.queue_cleanup"), expanded=False):
            st.caption(t("ui.p124.route_queue_table.cleanup_phase_4_naming_artifacts_delete"))
            action = st.radio(
                t("ui.p124.route_queue_table.action"),
                options=[
                    "Clear naming artifacts only",
                    "Delete routes from route_prod queue",
                ],
                format_func=lambda v: t("p4queue.action.clear") if v == "Clear naming artifacts only" else t("p4queue.action.delete"),
                horizontal=True,
                key=f"{key}_cleanup_action",
            )
            targets = st.multiselect(
                t("ui.p124.route_queue_table.route_ids"),
                options=route_ids,
                default=[selected] if selected else [],
                key=f"{key}_cleanup_targets",
            )

            c_opt1, c_opt2 = st.columns(2)
            with c_opt1:
                clear_naming_artifacts = st.checkbox(
                    t("ui.p124.route_queue_table.clear_naming_artifacts_first"),
                    value=True,
                    key=f"{key}_cleanup_clear",
                    disabled=(action == "Clear naming artifacts only"),
                )
            with c_opt2:
                reset_legacy_cols = st.checkbox(
                    t("ui.p124.route_queue_table.reset_legacy_naming_columns"),
                    value=False,
                    key=f"{key}_cleanup_reset",
                )

            c_btn1, c_btn2 = st.columns(2)
            with c_btn1:
                if st.button(
                    t("ui.p124.route_queue_table.preview_impact"),
                    key=f"{key}_cleanup_preview",
                    use_container_width=True,
                    disabled=(len(targets) == 0),
                ):
                    preview = []
                    for rid in targets:
                        try:
                            if action == "Delete routes from route_prod queue":
                                out = c.delete_route_from_prod(
                                    rid,
                                    clear_naming_artifacts=bool(clear_naming_artifacts),
                                    reset_legacy_route_columns=bool(reset_legacy_cols),
                                    dry_run=True,
                                )
                            else:
                                out = c.clear_route_naming_artifacts(
                                    rid,
                                    reset_legacy_route_columns=bool(reset_legacy_cols),
                                    dry_run=True,
                                )
                            preview.append(out)
                        except Exception as e:
                            preview.append({"route_id": rid, "error": str(e), "dry_run": True})
                    st.json(preview)

            confirm_cleanup = st.checkbox(
                t("ui.p124.route_queue_table.i_confirm_apply_cleanup_selected_routes"),
                value=False,
                key=f"{key}_cleanup_confirm",
            )
            with c_btn2:
                if st.button(
                    t("ui.p124.route_queue_table.apply_cleanup"),
                    key=f"{key}_cleanup_apply",
                    type="secondary",
                    use_container_width=True,
                    disabled=(len(targets) == 0),
                ):
                    if not confirm_cleanup:
                        st.error(t("ui.p124.route_queue_table.confirm_cleanup_first"))
                    else:
                        applied = 0
                        failed: list[Dict[str, Any]] = []
                        for rid in targets:
                            try:
                                if action == "Delete routes from route_prod queue":
                                    c.delete_route_from_prod(
                                        rid,
                                        clear_naming_artifacts=bool(clear_naming_artifacts),
                                        reset_legacy_route_columns=bool(reset_legacy_cols),
                                        dry_run=False,
                                    )
                                else:
                                    c.clear_route_naming_artifacts(
                                        rid,
                                        reset_legacy_route_columns=bool(reset_legacy_cols),
                                        dry_run=False,
                                    )
                                applied += 1
                            except Exception as e:
                                failed.append({"route_id": rid, "error": str(e)})

                        remaining = [rid for rid in route_ids if rid not in set(targets)]
                        if st.session_state.get("phase4.route_id") in set(targets):
                            st.session_state["phase4.route_id"] = remaining[0] if remaining else ""
                        st.cache_data.clear()

                        if failed:
                            st.warning(t("ui.p124.route_queue_table.applied_applied_route_s_failed_n", applied=(applied), n_failed=(len(failed))))
                            st.json(failed)
                        else:
                            st.success(t("ui.p124.route_queue_table.applied_applied_route_s", applied=(applied)))
                        st.rerun()

        return selected
    return None
