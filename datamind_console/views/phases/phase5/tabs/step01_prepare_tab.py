from __future__ import annotations

from typing import Any

import streamlit as st

def render_step01_prepare_tab(*, ctx: Any, client: Any, **_) -> None:
    st.markdown("### Step 00 Agencies (upload + dedup only)")
    run_id = (st.session_state.get("phase5.export_run_id") or "").strip()
    gtfs_id = (st.session_state.get("phase5.gtfs_id") or "").strip()
    source_run_id = (st.session_state.get("phase5.source_export_run_id") or "").strip()
    source_run_err = ""

    st.caption(f"Context: gtfs_id=`{gtfs_id or '-'}`")
    st.caption("This step only loads agencies from uploaded GTFS and inserts missing ones into agency catalog.")
    st.caption("No route matching and no direction logic in this step.")

    if not run_id:
        st.info("Create/select gtfs_id first (internal export context is created automatically).")
        return
    try:
        if not bool(client.export_run_exists(run_id)):
            st.error(
                "Internal build context is stale/deleted. Re-select the gtfs_id so Phase 5 recreates its internal export context."
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
        st.error(f"Failed to resolve uploaded GTFS source export context: {source_run_err}")
    else:
        st.caption(
            f"Agency source export (uploaded document): `{source_run_id or '-'}`. "
            "This is only used for loading/preview into the selected `gtfs_id`."
        )

    st.markdown("#### Load agencies from uploaded GTFS")
    l1, l2 = st.columns([1, 2])
    with l1:
        if st.button("Load agencies from GTFS", use_container_width=True, key="p5.step00.agency.load_gtfs"):
            if not source_run_id:
                st.error("No uploaded GTFS source selected. Pick an uploaded export in the GTFS Build Context widget above.")
                rows = []
            else:
                try:
                    rows = client.preview_agencies_from_uploaded_gtfs(export_run_id=source_run_id, limit=3000)
                except Exception as e:
                    st.error(f"Load GTFS agencies failed: {e}")
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
                f"GTFS agencies loaded: total={len(gtfs_agency_rows)} | "
                f"exists_id={n_exists_id} | exists_name={n_exists_name} | "
                f"missing={n_missing} | duplicate_in_gtfs={n_dup}"
            )

        st.dataframe(gtfs_agency_rows, use_container_width=True, hide_index=True, height=220)

        missing_rows = [r for r in gtfs_agency_rows if str(r.get("status") or "") in ("missing", "duplicate_in_uploaded_gtfs")]
        if missing_rows:
            st.caption("Only missing agencies can be inserted. Duplicate protection checks both agency_id and agency_name.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("Import all missing agencies", use_container_width=True, key="p5.step00.agency.gtfs_import_all"):
                    if not source_run_id:
                        st.error("No uploaded GTFS source selected.")
                    else:
                        try:
                            out_bulk = client.sync_agency_catalog_from_uploaded_gtfs(export_run_id=source_run_id, limit=3000)
                        except Exception as e:
                            st.error(f"Bulk import failed: {e}")
                        else:
                            st.success(
                                "Bulk import finished: "
                                f"inserted={int(out_bulk.get('inserted') or 0)} | "
                                f"exists_id={int(out_bulk.get('already_exists_id') or 0)} | "
                                f"exists_name={int(out_bulk.get('already_exists_name') or 0)} | "
                                f"failed={int(out_bulk.get('failed') or 0)}"
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
            pick = st.selectbox("missing agency from GTFS", opts, key="p5.step00.agency.gtfs_pick")
            picked = next(
                (
                    r
                    for r in missing_rows
                    if f"{str(r.get('source_agency_id') or '')} | {str(r.get('source_agency_name') or '')}" == pick
                ),
                {},
            )
            new_id = st.text_input(
                "agency_id override (optional)",
                value=str(picked.get("source_agency_id") or ""),
                key="p5.step00.agency.gtfs_new_id",
            )
            with c2:
                if st.button("Insert selected agency", use_container_width=True, key="p5.step00.agency.gtfs_insert_one"):
                    try:
                        out = client.insert_agency_from_uploaded_gtfs(
                            source_agency_name=str(picked.get("source_agency_name") or ""),
                            source_agency_id=(str(new_id or "").strip() or None),
                            source_agency_url=str(picked.get("source_agency_url") or "https://example.com"),
                            source_agency_timezone=str(picked.get("source_agency_timezone") or "America/Guayaquil"),
                            source_agency_lang=str(picked.get("source_agency_lang") or "es"),
                        )
                    except Exception as e:
                        st.error(f"Insert failed: {e}")
                    else:
                        st.success(f"Agency insert result: {out}")
                        try:
                            st.session_state["p5.step00.agency.gtfs_rows"] = client.preview_agencies_from_uploaded_gtfs(
                                export_run_id=source_run_id or run_id,
                                limit=3000,
                            )
                        except Exception:
                            pass
                        st.rerun()
        else:
            st.info("All GTFS agencies already exist in agency catalog.")
    else:
        st.info("Load agencies from GTFS to start.")

    st.divider()
    st.markdown("#### Preview agencies to insert into selected gtfs_id")
    st.caption("This compares uploaded GTFS agencies vs current GTFS build agencies for the selected `gtfs_id`.")
    target_prev_key = "p5.step00.agency.target_preview"
    p1, p2 = st.columns([1, 1])
    with p1:
        if st.button("Preview Step 00 insert into gtfs_id", use_container_width=True, key="p5.step00.gtfs.preview_target"):
            if not source_run_id:
                st.error("No uploaded GTFS source selected.")
                st.session_state[target_prev_key] = []
            else:
                try:
                    st.session_state[target_prev_key] = client.preview_gtfs_agency_insert_for_build(
                        source_export_run_id=source_run_id,
                        target_export_run_id=run_id,
                        limit=3000,
                    )
                except Exception as e:
                    st.error(f"Target preview failed: {e}")
                    st.session_state[target_prev_key] = []
    with p2:
        if st.button("Run Step 00 (insert agencies into gtfs_id)", type="primary", use_container_width=True, key="p5.step00.gtfs.run"):
            if not source_run_id:
                st.error("No uploaded GTFS source selected.")
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
                    st.success("Step 00 completed.")
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
            f"Preview summary: ready={n_ready} | exists_by_id={n_id} | exists_by_name={n_name} | duplicate_in_source={n_dup}"
        )
        show_cols = ["agency_id", "agency_name", "status", "existing_agency_id", "existing_agency_name", "agency_timezone", "agency_lang"]
        st.dataframe(
            [{k: r.get(k) for k in show_cols if k in r} for r in target_preview_rows],
            use_container_width=True,
            hide_index=True,
            height=220,
        )
    else:
        st.info("Click preview to see what Step 00 will insert into this gtfs_id.")

    with st.expander("Agency catalog", expanded=False):
        try:
            catalog = client.list_agency_catalog(active_only=False, limit=2000)
            st.dataframe(catalog, use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"Failed to load catalog: {e}")
