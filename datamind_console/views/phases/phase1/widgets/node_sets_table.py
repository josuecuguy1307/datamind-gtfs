# views/phases/phase1/widgets/node_sets_table.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
import streamlit as st
import pandas as pd

from phases.phase1_nodes.client import _get_phase1_client
from datamind_console.i18n import t


# ---------------------------
# Main widget
# ---------------------------

def render_node_sets_table(
    client: Any | None = None,
    *,
    key: str = "phase1.node_sets_table",
    limit: int = 500,
    height: int = 360,
) -> Optional[str]:
    """
    Phase 1 — Node Sets Table

    - Wired directly to Phase1Client.list_node_sets
    - Shows latest node sets
    - Returns selected node_set_id (or None)
    """

    phase1 = client or _get_phase1_client()

    try:
        rows: List[Dict[str, Any]] = phase1.list_node_sets(limit=limit)
    except RuntimeError as e:
        # Missing local configuration is an expected offline condition, not a
        # widget failure.  Keep the actionable boundary message visible.
        st.info(str(e))
        return None
    except Exception as e:
        st.error(t("ui.p124.node_sets_table.failed_load_node_sets_e", e=(e)))
        return None

    if not rows:
        st.info(t("ui.p124.node_sets_table.no_node_sets_found"))
        return None

    # Normalize for DataFrame
    df = pd.DataFrame(rows)

    # Stable column order (only what matters)
    cols = [
        "node_set_id",
        "place_name",
        "target_group",
        "extraction_action",
        "status",
        "raw_count",
        "candidate_count",
        "stop_like_count",
        "poi_like_count",
        "created_at",
        "resolved_at",
        "n_resolved",
        "n_approved",
        "n_work",
        "n_rejected",
    ]
    df = df[[c for c in cols if c in df.columns]]

    # Selection column
    df.insert(0, "select", False)

    edited = st.data_editor(
        df,
        hide_index=True,
        height=height,
        key=key,
        column_config={
            "select": st.column_config.CheckboxColumn(
                t("ui.p124.node_sets_table.select"),
                help=t("ui.p124.node_sets_table.choose_node_set"),
                default=False,
            ),
            "node_set_id": st.column_config.TextColumn(
                "node_set_id",
                disabled=True,
            ),
            "place_name": st.column_config.TextColumn(
                t("ui.p124.node_sets_table.place"),
                disabled=True,
            ),
            "target_group": st.column_config.TextColumn(
                t("ui.p124.node_sets_table.group"),
                disabled=True,
            ),
            "extraction_action": st.column_config.TextColumn(
                t("ui.p124.node_sets_table.action"),
                disabled=True,
            ),
        },
        disabled=[c for c in df.columns if c != "select"],
    )

    

    selected_ids = [str(x) for x in edited.loc[edited["select"] == True, "node_set_id"].tolist() if x]
    st.caption(t("ui.p124.node_sets_table.selected_rows_n_selected_ids", n_selected_ids=(len(selected_ids))))

    with st.expander(t("ui.p124.node_sets_table.set_cleanup"), expanded=False):
        st.caption(t("ui.p124.node_sets_table.delete_selected_node_sets_from_node"))
        c1, c2 = st.columns([1, 1])
        with c1:
            delete_prod_nodes = st.checkbox(
                t("ui.p124.node_sets_table.also_delete_node_prod_nodes_from"),
                value=False,
                key=f"{key}.del_prod",
            )
        with c2:
            confirm_delete = st.checkbox(
                t("ui.p124.node_sets_table.i_confirm_delete_selected_node_sets"),
                value=False,
                key=f"{key}.del_confirm",
            )

        if st.button(
            t("ui.p124.node_sets_table.delete_selected_set_s"),
            type="secondary",
            use_container_width=True,
            key=f"{key}.del_btn",
            disabled=(len(selected_ids) == 0),
        ):
            if not confirm_delete:
                st.error(t("ui.p124.node_sets_table.confirm_deletion_first"))
            else:
                deleted = 0
                failed: List[Dict[str, Any]] = []
                for sid in selected_ids:
                    try:
                        phase1.delete_node_set(sid, delete_prod_nodes=bool(delete_prod_nodes))
                        deleted += 1
                    except Exception as e:
                        failed.append({"node_set_id": sid, "error": str(e)})

                if st.session_state.get("p1.node_set_id") in set(selected_ids):
                    st.session_state["p1.node_set_id"] = None
                st.session_state.pop(key, None)

                if failed:
                    st.warning(t("ui.p124.node_sets_table.deleted_deleted_set_s_failed_n", deleted=(deleted), n_failed=(len(failed))))
                    st.json(failed)
                else:
                    st.success(t("ui.p124.node_sets_table.deleted_deleted_set_s", deleted=(deleted)))
                st.rerun()

    if not selected_ids:
        return None

    if len(selected_ids) > 1:
        st.warning(t("ui.p124.node_sets_table.select_only_one_node_set_open"))
        return None

    return str(selected_ids[0])
