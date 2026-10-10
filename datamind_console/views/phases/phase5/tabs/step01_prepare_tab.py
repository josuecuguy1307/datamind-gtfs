from __future__ import annotations

from typing import Any

import streamlit as st
from datamind_console.i18n import t

def render_step01_prepare_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown(t("ui.p5.step01_prepare.step_00_agencies_upload_dedup_only"))
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    gtfs_id = (st.session_state.get("phase5.gtfs_id") or "").strip()
    source_run_id = (st.session_state.get("phase5.source_export_run_id") or "").strip()
    source_run_err = ""

    st.caption(t("ui.p5.step01_prepare.context_gtfs_id_gtfs_id", gtfs_id=(gtfs_id or '-')))
    st.caption(t("ui.p5.step01_prepare.this_step_only_loads_agencies_from"))
    st.caption(t("ui.p5.step01_prepare.no_route_matching_no_direction_logic"))

    if not run_id:
        st.info(t("ui.p5.step01_prepare.create_select_gtfs_id_first_internal"))
        return
    try:
        if not bool(client.export_run_exists(run_id)):
            st.error(
                t("ui.p5.step01_prepare.internal_build_context_stale_deleted_re")
            )
            return
    except Exception:
        pass

    if not source_run_id:
        try:
            source_run_id = str(
                client.resolve_uploaded_gtfs_source_export_run_id(
                    gtfs_id=gtfs_id,
                    current_export_run_id=run_id,
                )
                or ""
            ).strip()
        except Exception as e:
            source_run_err = str(e)

    if source_run_err:
        st.error(t("ui.p5.step01_prepare.failed_resolve_uploaded_gtfs_source_export", source_run_err=(source_run_err)))
    else:
        st.caption(
            t("ui.p5.step01_prepare.agency_source_export_uploaded_document_source", source_run_id=(source_run_id or '-'))
        )

    st.markdown(t("ui.p5.step01_prepare.load_agencies_from_uploaded_gtfs"))
    l1, l2 = st.columns([1, 2])
    with l1:
        if st.button(t("ui.p5.step01_prepare.load_agencies_from_gtfs"), use_container_width=True, key="p5.step00.agency.load_gtfs"):
            if not source_run_id:
                st.error(t("ui.p5.step01_prepare.no_uploaded_gtfs_source_selected_pick"))
                rows = []
            else:
                try:
                    rows = client.preview_agencies_from_uploaded_gtfs(export_run_id=source_run_id, limit=3000)
                except Exception as e:
                    st.error(t("ui.p5.step01_prepare.load_gtfs_agencies_failed_e", e=(e)))
                    rows = []
            st.session_state["p5.step00.agency.gtfs_rows"] = rows

    gtfs_agency_rows = st.session_state.get("p5.step00.agency.gtfs_rows") or []
    if gtfs_agency_rows:
        n_exists_id = sum(1 for r in gtfs_agency_rows if str(r.get("status") or "") == "exists_id")
        n_exists_name = sum(1 for r in gtfs_agency_rows if str(r.get("status") or "") == "exists_name")
        n_missing = sum(1 for r in gtfs_agency_rows if str(r.get("status") or "") == "missing")
        n_dup = sum(1 for r in gtfs_agency_rows if bool(r.get("duplicate_in_uploaded_gtfs")))
        with l2:
            st.caption(
                t("ui.p5.step01_prepare.gtfs_agencies_loaded_total_n_gtfs", n_gtfs_agency_rows=(len(gtfs_agency_rows)), n_exists_id=(n_exists_id), n_exists_name=(n_exists_name), n_missing=(n_missing), n_dup=(n_dup))
            )

        st.dataframe(gtfs_agency_rows, use_container_width=True, hide_index=True, height=220)

        missing_rows = [r for r in gtfs_agency_rows if str(r.get("status") or "") in ("missing", "duplicate_in_uploaded_gtfs")]
        if missing_rows:
            st.caption(t("ui.p5.step01_prepare.only_missing_agencies_can_be_inserted"))
            c1, c2 = st.columns(2)
            with c1:
                if st.button(t("ui.p5.step01_prepare.import_all_missing_agencies"), use_container_width=True, key="p5.step00.agency.gtfs_import_all"):
                    if not source_run_id:
                        st.error(t("ui.p5.step01_prepare.no_uploaded_gtfs_source_selected"))
                    else:
                        try:
                            out_bulk = client.sync_agency_catalog_from_uploaded_gtfs(export_run_id=source_run_id, limit=3000)
                        except Exception as e:
                            st.error(t("ui.p5.step01_prepare.bulk_import_failed_e", e=(e)))
                        else:
                            st.success(
                                t("ui.p5.step01_prepare.bulk_import_finished_inserted_inserted_exists", inserted=(int(out_bulk.get('inserted') or 0)), already_exists_id=(int(out_bulk.get('already_exists_id') or 0)), already_exists_name=(int(out_bulk.get('already_exists_name') or 0)), failed=(int(out_bulk.get('failed') or 0)))
                            )
                            try:
                                st.session_state["p5.step00.agency.gtfs_rows"] = client.preview_agencies_from_uploaded_gtfs(
                                    export_run_id=source_run_id,
                                    limit=3000,
                                )
                            except Exception:
                                pass
                            st.rerun()

            opts = [f"{str(r.get('source_agency_id') or '')} | {str(r.get('source_agency_name') or '')}" for r in missing_rows]
            pick = st.selectbox(t("ui.p5.step01_prepare.missing_agency_from_gtfs"), opts, key="p5.step00.agency.gtfs_pick")
            picked = next(
                (
                    r
                    for r in missing_rows
                    if f"{str(r.get('source_agency_id') or '')} | {str(r.get('source_agency_name') or '')}" == pick
                ),
                {},
            )
            new_id = st.text_input(
                t("ui.p5.step01_prepare.agency_id_override_optional"),
                value=str(picked.get("source_agency_id") or ""),
                key="p5.step00.agency.gtfs_new_id",
            )
            with c2:
                if st.button(t("ui.p5.step01_prepare.insert_selected_agency"), use_container_width=True, key="p5.step00.agency.gtfs_insert_one"):
                    try:
                        out = client.insert_agency_from_uploaded_gtfs(
                            source_agency_name=str(picked.get("source_agency_name") or ""),
                            source_agency_id=(str(new_id or "").strip() or None),
                            source_agency_url=str(picked.get("source_agency_url") or "https://example.com"),
                            source_agency_timezone=str(picked.get("source_agency_timezone") or "America/Guayaquil"),
                            source_agency_lang=str(picked.get("source_agency_lang") or "es"),
                        )
                    except Exception as e:
                        st.error(t("ui.p5.step01_prepare.insert_failed_e", e=(e)))
                    else:
                        st.success(t("ui.p5.step01_prepare.agency_insert_result_out", out=(out)))
                        try:
                            st.session_state["p5.step00.agency.gtfs_rows"] = client.preview_agencies_from_uploaded_gtfs(
                                export_run_id=source_run_id or run_id,
                                limit=3000,
                            )
                        except Exception:
                            pass
                        st.rerun()
        else:
            st.info(t("ui.p5.step01_prepare.all_gtfs_agencies_already_exist_agency"))
    else:
        st.info(t("ui.p5.step01_prepare.load_agencies_from_gtfs_start"))

    st.divider()
    st.markdown(t("ui.p5.step01_prepare.preview_agencies_insert_into_selected_gtfs"))
    st.caption(t("ui.p5.step01_prepare.this_compares_uploaded_gtfs_agencies_vs"))
    target_prev_key = "p5.step00.agency.target_preview"
    p1, p2 = st.columns([1, 1])
    with p1:
        if st.button(t("ui.p5.step01_prepare.preview_step_00_insert_into_gtfs"), use_container_width=True, key="p5.step00.gtfs.preview_target"):
            if not source_run_id:
                st.error(t("ui.p5.step01_prepare.no_uploaded_gtfs_source_selected"))
                st.session_state[target_prev_key] = []
            else:
                try:
                    st.session_state[target_prev_key] = client.preview_gtfs_agency_insert_for_build(
                        source_export_run_id=source_run_id,
                        target_export_run_id=run_id,
                        limit=3000,
                    )
                except Exception as e:
                    st.error(t("ui.p5.step01_prepare.target_preview_failed_e", e=(e)))
                    st.session_state[target_prev_key] = []
    with p2:
        if st.button(t("ui.p5.step01_prepare.run_step_00_insert_agencies_into"), type="primary", use_container_width=True, key="p5.step00.gtfs.run"):
            if not source_run_id:
                st.error(t("ui.p5.step01_prepare.no_uploaded_gtfs_source_selected"))
            else:
                try:
                    out_step00 = client.run_step_00_load_gtfs_agencies(
                        source_export_run_id=source_run_id,
                        target_export_run_id=run_id,
                        limit=3000,
                    )
                except Exception as e:
                    st.error(str(e))
                else:
                    st.success(t("ui.p5.step01_prepare.step_00_completed"))
                    st.json(out_step00)
                    try:
                        st.session_state[target_prev_key] = client.preview_gtfs_agency_insert_for_build(
                            source_export_run_id=source_run_id,
                            target_export_run_id=run_id,
                            limit=3000,
                        )
                    except Exception:
                        pass

    target_preview_rows = [dict(r) for r in (st.session_state.get(target_prev_key) or [])]
    if target_preview_rows:
        n_ready = sum(1 for r in target_preview_rows if str(r.get("status") or "") == "ready")
        n_id = sum(1 for r in target_preview_rows if str(r.get("status") or "") == "already_in_gtfs_id_by_id")
        n_name = sum(1 for r in target_preview_rows if str(r.get("status") or "") == "already_in_gtfs_id_by_name")
        n_dup = sum(1 for r in target_preview_rows if str(r.get("status") or "") == "duplicate_in_source_upload")
        st.caption(
            t("ui.p5.step01_prepare.preview_summary_ready_n_ready_exists", n_ready=(n_ready), n_id=(n_id), n_name=(n_name), n_dup=(n_dup))
        )
        show_cols = ["agency_id", "agency_name", "status", "existing_agency_id", "existing_agency_name", "agency_timezone", "agency_lang"]
        st.dataframe(
            [{k: r.get(k) for k in show_cols if k in r} for r in target_preview_rows],
            use_container_width=True,
            hide_index=True,
            height=220,
        )
    else:
        st.info(t("ui.p5.step01_prepare.click_preview_see_what_step_00"))

    with st.expander(t("ui.p5.step01_prepare.agency_catalog"), expanded=False):
        try:
            catalog = client.list_agency_catalog(active_only=False, limit=2000)
            st.dataframe(catalog, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(t("ui.p5.step01_prepare.failed_load_catalog_e", e=(e)))
