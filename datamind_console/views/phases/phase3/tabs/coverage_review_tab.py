# ============================================================
# DEPRECATED — Constructor V2 replaces this module.
# See DEPRECATED_V1_MODULES.md for details.
# This file is preserved for historical reference only.
# ============================================================
from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st
from datamind_console.i18n import t, option_label, status_label


def _status_tokens(tokens: List[str]) -> str:
    items = [str(token).strip() for token in tokens if str(token).strip()]
    return " | ".join(f"`{token}`" for token in items)


def _catalog_row_tokens(row: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    if bool(row.get("is_suppressed_duplicate")):
        tokens.append(t("coverage.token.suppressed_duplicate"))
    elif bool(row.get("is_canonical_route_job")):
        tokens.append(t("coverage.token.canonical_route"))
    else:
        tokens.append(t("coverage.token.standalone_route"))
    if bool(row.get("manual_origin")):
        tokens.append(t("coverage.token.manual_route"))
    if str(row.get("latest_coverage_gap_id") or "").strip():
        tokens.append(t("coverage.token.linked_gap"))
    if str(row.get("prod_status") or "") == "in_prod":
        tokens.append(t("coverage.token.in_prod"))
    elif str(row.get("approval_status") or "") == "approved":
        tokens.append(t("coverage.token.approved"))
    else:
        tokens.append(status_label("approval", row.get("approval_status") or "pending"))
    return tokens


def _gap_tokens(row: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    resolution = str(row.get("resolution_status") or "open").strip()
    classification = str(row.get("effective_classification") or row.get("classification_status") or "needs_review").strip()
    tokens.append(status_label("gap_resolution", resolution))
    tokens.append(status_label("gap_classification", classification))
    if str(row.get("resolved_route_id") or "").strip():
        tokens.append(t("coverage.token.linked_route"))
    if str(row.get("resolved_prod_route_id") or "").strip():
        tokens.append(t("coverage.token.resolved_in_prod"))
    return tokens


def render_coverage_review_tab(ctx, client) -> None:
    del ctx
    ss = st.session_state

    st.subheader(t("ui.p3.coverage_review.phase_3_coverage_review"))
    st.caption(
        t("ui.p3.coverage_review.unified_catalog_sector_coverage_missing_route")
    )
    st.info(
        t("ui.p3.coverage_review.recommended_operator_sequence_1_finish_step05")
    )

    try:
        summary = client.get_phase3_catalog_summary()
    except Exception as exc:
        st.error(t("ui.p3.coverage_review.could_not_load_phase_3_coverage", exc=(exc)))
        return

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric(t("ui.p3.coverage_review.sectors"), int(summary.get("sector_count") or 0))
    with m2:
        st.metric(t("ui.p3.coverage_review.route_families"), int(summary.get("route_family_count") or 0))
    with m3:
        st.metric(t("ui.p3.coverage_review.open_gaps"), int(summary.get("open_gap_count") or 0))
    with m4:
        st.metric(t("ui.p3.coverage_review.prod"), int(summary.get("prod_family_count") or 0))

    a1, a2 = st.columns(2)
    with a1:
        if st.button(t("ui.p3.coverage_review.sync_coverage_gaps_from_phase_3"), type="primary", use_container_width=True, key="p3.coverage.sync"):
            try:
                out = client.sync_phase3_coverage_gaps()
                ss["p3.coverage.last_sync"] = out
                st.success(
                    t("ui.p3.coverage_review.catalogs_catalog_count_synced_gaps_synced", catalog_count=(int(out.get('catalog_count') or 0)), synced_gap_count=(int(out.get('synced_gap_count') or 0)), open_gap_count=(int(out.get('open_gap_count') or 0)))
                )
            except Exception as exc:
                st.error(t("ui.p3.coverage_review.coverage_sync_failed_exc", exc=(exc)))
    with a2:
        if st.button(t("ui.p3.coverage_review.export_missing_route_json_catalogs"), use_container_width=True, key="p3.coverage.export"):
            try:
                out = client.export_phase3_missing_route_catalogs()
                ss["p3.coverage.last_export"] = out
                st.success(
                    t("ui.p3.coverage_review.file_count_file_count_output_dir", file_count=(int(out.get('file_count') or 0)), output_dir=(out.get('output_dir') or '-'))
                )
            except Exception as exc:
                st.error(t("ui.p3.coverage_review.missing_route_export_failed_exc", exc=(exc)))

    last_sync = dict(ss.get("p3.coverage.last_sync") or {})
    if last_sync:
        st.caption(
            t("ui.p3.coverage_review.last_sync_catalogs_catalog_count_synced", catalog_count=(int(last_sync.get('catalog_count') or 0)), synced_gap_count=(int(last_sync.get('synced_gap_count') or 0)), resolved_gap_count=(int(last_sync.get('resolved_gap_count') or 0)), in_progress_gap_count=(int(last_sync.get('in_progress_gap_count') or 0)))
        )
    last_export = dict(ss.get("p3.coverage.last_export") or {})
    if list(last_export.get("files") or []):
        with st.expander(t("ui.p3.coverage_review.exported_missing_route_catalogs"), expanded=False):
            st.dataframe(list(last_export.get("files") or []), use_container_width=True, hide_index=True)

    with st.expander(t("ui.p3.coverage_review.gap_classification_guide"), expanded=False):
        st.markdown(
            t("ui.p3.coverage_review.still_extractable_keep_extractor_first_discipline")
        )

    st.divider()

    try:
        sector_rows = client.list_phase3_sector_coverage(limit=300)
    except Exception as exc:
        sector_rows = []
        st.warning(t("ui.p3.coverage_review.could_not_load_sector_coverage_exc", exc=(exc)))

    sector_options = ["all"]
    sector_options.extend([str(row.get("sector_key") or "") for row in sector_rows if str(row.get("sector_key") or "").strip()])
    sector_key = st.selectbox(
        t("ui.p3.coverage_review.sector_filter"),
        options=sector_options,
        index=(sector_options.index(str(ss.get("p3.coverage.sector_key") or "all")) if str(ss.get("p3.coverage.sector_key") or "all") in sector_options else 0),
        format_func=lambda key: t("coverage.all_sectors") if key == "all" else next((str(row.get("sector_label") or key) for row in sector_rows if str(row.get("sector_key")) == str(key)), key),
        key="p3.coverage.sector_key",
    )

    st.markdown(t("ui.p3.coverage_review.sector_coverage"))
    if sector_rows:
        sector_table = sector_rows
        if sector_key != "all":
            sector_table = [row for row in sector_rows if str(row.get("sector_key") or "") == str(sector_key)]
        st.dataframe(sector_table, use_container_width=True, hide_index=True)
    else:
        st.info(t("ui.p3.coverage_review.no_sector_coverage_rows_available_yet"))

    # ── Interpretation Layer ──
    with st.expander(t("ui.p3.coverage_review.route_interpretation_sector_suggestions"), expanded=False):
        st.caption(
            t("ui.p3.coverage_review.non_authoritative_interpretation_layer_shows_suggested")
        )

        interp_tabs = st.tabs([t("ui.p3.coverage_review.sector_suggestions"), t("ui.p3.coverage_review.label_interpretation"), t("ui.p3.coverage_review.gap_reclassification")])

        with interp_tabs[0]:
            try:
                sector_sugg = client.raw_query(
                    "SELECT suggested_sector, suggestion_reason, sector_confidence::float, COUNT(*) as cnt "
                    "FROM route_review.sector_suggestion_v1 "
                    "WHERE suggested_sector IS NOT NULL "
                    "GROUP BY 1, 2, 3 ORDER BY cnt DESC"
                )
                total_unassigned = client.raw_query(
                    "SELECT COUNT(*) as total FROM route_review.sector_suggestion_v1"
                )
                with_suggestion = client.raw_query(
                    "SELECT COUNT(*) as cnt FROM route_review.sector_suggestion_v1 WHERE suggested_sector IS NOT NULL"
                )
                u_cnt = int(total_unassigned[0]["total"]) if total_unassigned else 0
                s_cnt = int(with_suggestion[0]["cnt"]) if with_suggestion else 0
                sm1, sm2, sm3 = st.columns(3)
                with sm1:
                    st.metric(t("ui.p3.coverage_review.unassigned_routes"), u_cnt)
                with sm2:
                    st.metric(t("ui.p3.coverage_review.sector_suggestion"), s_cnt)
                with sm3:
                    st.metric(t("ui.p3.coverage_review.coverage"), f"{s_cnt * 100 // max(u_cnt, 1)}%")
                if sector_sugg:
                    st.dataframe(sector_sugg, use_container_width=True, hide_index=True)

                detail_rows = client.raw_query(
                    "SELECT route_job_id, route_family_label, interpreted_name, interpreted_operator, "
                    "current_sector, suggested_sector, suggestion_reason, sector_confidence::float "
                    "FROM route_review.sector_suggestion_v1 "
                    "WHERE suggested_sector IS NOT NULL "
                    "ORDER BY sector_confidence DESC LIMIT 50"
                )
                if detail_rows:
                    st.markdown(t("ui.p3.coverage_review.detailed_suggestions_top_50_by_confidence"))
                    st.dataframe(detail_rows, use_container_width=True, hide_index=True, height=280)
            except Exception as exc:
                st.warning(t("ui.p3.coverage_review.could_not_load_sector_suggestions_exc", exc=(exc)))

        with interp_tabs[1]:
            try:
                quality_counts = client.raw_query(
                    "SELECT label_quality, COUNT(*) as cnt "
                    "FROM route_review.route_interpretation_v1 "
                    "GROUP BY 1 ORDER BY cnt DESC"
                )
                if quality_counts:
                    st.dataframe(quality_counts, use_container_width=True, hide_index=True)

                unresolved = client.raw_query(
                    "SELECT route_job_id, route_family_label, interpreted_name, interpreted_ref, "
                    "interpreted_operator, label_quality, label_confidence::float, "
                    "label_evidence_source, unresolved_reason "
                    "FROM route_review.route_interpretation_v1 "
                    "WHERE label_quality != 'canonical' "
                    "ORDER BY label_confidence DESC LIMIT 50"
                )
                if unresolved:
                    st.markdown(t("ui.p3.coverage_review.non_canonical_label_interpretations_top_50"))
                    st.dataframe(unresolved, use_container_width=True, hide_index=True, height=280)
            except Exception as exc:
                st.warning(t("ui.p3.coverage_review.could_not_load_label_interpretations_exc", exc=(exc)))

        with interp_tabs[2]:
            try:
                gap_reclass = client.raw_query(
                    "SELECT suggested_gap_class, COUNT(*) as cnt, "
                    "SUM(supporting_route_count) as total_supporting "
                    "FROM route_review.gap_reclassification_v1 "
                    "GROUP BY 1 ORDER BY cnt DESC"
                )
                if gap_reclass:
                    st.dataframe(gap_reclass, use_container_width=True, hide_index=True)

                gap_details = client.raw_query(
                    "SELECT gap_id, sector_key, route_family_hint, current_gap_status, "
                    "suggested_gap_class, suggested_gap_reason, supporting_route_count, "
                    "operator_action_hint "
                    "FROM route_review.gap_reclassification_v1 "
                    "ORDER BY suggested_gap_class, route_family_hint LIMIT 60"
                )
                if gap_details:
                    st.markdown(t("ui.p3.coverage_review.gap_reclassification_details"))
                    st.dataframe(gap_details, use_container_width=True, hide_index=True, height=280)
            except Exception as exc:
                st.warning(t("ui.p3.coverage_review.could_not_load_gap_reclassification_exc", exc=(exc)))

    st.divider()

    c1, c2 = st.columns([1.1, 1.0])
    with c1:
        route_family_search = st.text_input(
            t("ui.p3.coverage_review.global_catalog_search"),
            value=str(ss.get("p3.coverage.route_family_search") or ""),
            key="p3.coverage.route_family_search",
            placeholder=t("ui.p3.coverage_review.route_name_hint_ref_place"),
        )
    with c2:
        include_suppressed = st.checkbox(
            t("ui.p3.coverage_review.include_suppressed_duplicates"),
            value=bool(ss.get("p3.coverage.include_suppressed", True)),
            key="p3.coverage.include_suppressed",
        )

    try:
        catalog_rows = client.list_phase3_global_catalog(
            limit=1200,
            sector_key=(None if sector_key == "all" else sector_key),
            include_suppressed=bool(include_suppressed),
            route_family_search=(route_family_search.strip() or None),
        )
    except Exception as exc:
        catalog_rows = []
        st.warning(t("ui.p3.coverage_review.could_not_load_global_catalog_exc", exc=(exc)))

    st.markdown(t("ui.p3.coverage_review.global_phase_3_catalog"))
    if catalog_rows:
        st.dataframe(catalog_rows, use_container_width=True, hide_index=True, height=320)
        catalog_route_ids = [str(row.get("route_job_id") or "") for row in catalog_rows if str(row.get("route_job_id") or "").strip()]
        if catalog_route_ids:
            default_catalog_route_id = str(ss.get("p3.coverage.catalog_route_id") or catalog_route_ids[0]).strip()
            if default_catalog_route_id not in catalog_route_ids:
                default_catalog_route_id = catalog_route_ids[0]
            selected_catalog_route_id = st.selectbox(
                t("ui.p3.coverage_review.inspect_catalog_route"),
                options=catalog_route_ids,
                index=(catalog_route_ids.index(default_catalog_route_id) if default_catalog_route_id in catalog_route_ids else 0),
                format_func=lambda rid: next(
                    (
                        f"{row.get('route_family_label') or row.get('service_route_name') or rid} | {row.get('sector_label') or row.get('sector_key') or 'Unassigned'}"
                        for row in catalog_rows
                        if str(row.get("route_job_id")) == str(rid)
                    ),
                    str(rid),
                ),
                key="p3.coverage.catalog_route_id",
            )
            selected_catalog_row = next(
                (row for row in catalog_rows if str(row.get("route_job_id")) == str(selected_catalog_route_id)),
                {},
            )
            st.caption(t("coverage.catalog_status", tokens=_status_tokens(_catalog_row_tokens(selected_catalog_row))))
            if bool(selected_catalog_row.get("is_suppressed_duplicate")):
                st.warning(
                    t("ui.p3.coverage_review.this_suppressed_duplicate_review_row_it")
                )
            elif str(selected_catalog_row.get("latest_coverage_gap_id") or "").strip() and str(selected_catalog_row.get("prod_status") or "") != "in_prod":
                st.info(t("ui.p3.coverage_review.this_route_linked_gap_but_not"))
            elif str(selected_catalog_row.get("latest_coverage_gap_id") or "").strip() and str(selected_catalog_row.get("prod_status") or "") == "in_prod":
                st.success(t("ui.p3.coverage_review.this_route_prod_still_carries_linked"))
    else:
        st.info(t("ui.p3.coverage_review.no_global_phase_3_catalog_rows"))

    st.divider()

    g1, g2 = st.columns([1.1, 1.0])
    with g1:
        resolution_filter = st.selectbox(
            t("ui.p3.coverage_review.gap_resolution_filter"),
            options=["all", "open", "in_progress", "resolved", "dismissed"],
            index=["all", "open", "in_progress", "resolved", "dismissed"].index(str(ss.get("p3.coverage.resolution_filter") or "all")),
            key="p3.coverage.resolution_filter",
            format_func=option_label("gap_resolution"),
        )
    with g2:
        classification_filter = st.selectbox(
            t("ui.p3.coverage_review.gap_classification_filter"),
            options=["all", "still_extractable", "non_reliably_extractable", "needs_review"],
            index=["all", "still_extractable", "non_reliably_extractable", "needs_review"].index(str(ss.get("p3.coverage.classification_filter") or "all")),
            key="p3.coverage.classification_filter",
            format_func=option_label("gap_classification"),
        )

    try:
        gap_rows = client.list_phase3_coverage_gaps(
            limit=1200,
            sector_key=(None if sector_key == "all" else sector_key),
            resolution_status=(None if resolution_filter == "all" else resolution_filter),
            effective_classification=(None if classification_filter == "all" else classification_filter),
        )
    except Exception as exc:
        gap_rows = []
        st.warning(t("ui.p3.coverage_review.could_not_load_coverage_gaps_exc", exc=(exc)))

    st.markdown(t("ui.p3.coverage_review.missing_route_work_items"))
    if gap_rows:
        st.dataframe(gap_rows, use_container_width=True, hide_index=True, height=320)
        gap_ids = [str(row.get("gap_id") or "") for row in gap_rows if str(row.get("gap_id") or "").strip()]
        default_gap_id = str(ss.get("p3.coverage.selected_gap_id") or (gap_ids[0] if gap_ids else "")).strip()
        if default_gap_id not in gap_ids and gap_ids:
            default_gap_id = gap_ids[0]
        selected_gap_id = st.selectbox(
            t("ui.p3.coverage_review.open_gap_work_item"),
            options=gap_ids,
            index=(gap_ids.index(default_gap_id) if default_gap_id in gap_ids else 0),
            format_func=lambda gid: next(
                (
                    f"{row.get('sector_label') or row.get('sector_key')} | {row.get('route_family_hint')} | {row.get('effective_classification') or row.get('classification_status')}"
                    for row in gap_rows
                    if str(row.get("gap_id")) == str(gid)
                ),
                str(gid),
            ),
            key="p3.coverage.selected_gap_id",
        )
        selected_gap = next((row for row in gap_rows if str(row.get("gap_id")) == str(selected_gap_id)), {})
        gap_context = client.get_phase3_gap_manual_context(selected_gap_id) if selected_gap_id else {}
        if selected_gap:
            st.caption(t("coverage.gap_status", tokens=_status_tokens(_gap_tokens(selected_gap))))

        d1, d2 = st.columns([1.1, 1.0], gap="large")
        with d1:
            st.json(
                {
                    "gap_id": selected_gap.get("gap_id"),
                    "sector": selected_gap.get("sector_label"),
                    "route_family_hint": selected_gap.get("route_family_hint"),
                    "known_aliases": list(selected_gap.get("known_aliases") or []),
                    "start_hint": selected_gap.get("start_hint"),
                    "end_hint": selected_gap.get("end_hint"),
                    "direction_hint": selected_gap.get("direction_hint"),
                    "classification_status": selected_gap.get("classification_status"),
                    "effective_classification": selected_gap.get("effective_classification"),
                    "recommended_next_action": selected_gap.get("recommended_next_action"),
                    "resolution_status": selected_gap.get("resolution_status"),
                    "related_route_ids": list(selected_gap.get("related_route_ids") or []),
                    "evidence_summary": dict(selected_gap.get("evidence_summary") or {}),
                    "heuristic_notes": dict(selected_gap.get("heuristic_notes") or {}),
                }
            )
        with d2:
            effective_classification = str(
                selected_gap.get("effective_classification")
                or selected_gap.get("classification_status")
                or "needs_review"
            ).strip()
            if effective_classification == "still_extractable":
                st.warning(
                    t("ui.p3.coverage_review.this_gap_currently_classified_as_still")
                )
            elif effective_classification == "non_reliably_extractable":
                st.info(
                    t("ui.p3.coverage_review.this_gap_currently_classified_as_non")
                )
            resolved_route_id = str(selected_gap.get("resolved_route_id") or "").strip()
            resolved_prod_route_id = str(selected_gap.get("resolved_prod_route_id") or "").strip()
            if resolved_route_id or resolved_prod_route_id:
                st.caption(
                    t("ui.p3.coverage_review.linked_route_id_resolved_route_id", resolved_route_id=(resolved_route_id or '-'), resolved_prod_route_id=(resolved_prod_route_id or '-'))
                )
            override_default = str(selected_gap.get("operator_override_classification") or "").strip()
            override_value = st.selectbox(
                t("ui.p3.coverage_review.operator_override_classification"),
                options=["", "still_extractable", "non_reliably_extractable"],
                index=["", "still_extractable", "non_reliably_extractable"].index(override_default if override_default in {"", "still_extractable", "non_reliably_extractable"} else ""),
                key=f"p3.coverage.override.{selected_gap_id}",
                format_func=option_label("gap_classification"),
            )
            priority_value = st.selectbox(
                t("ui.p3.coverage_review.manual_priority"),
                options=["low", "medium", "high", "critical"],
                index=["low", "medium", "high", "critical"].index(str(selected_gap.get("manual_priority") or "medium")),
                key=f"p3.coverage.priority.{selected_gap_id}",
                format_func=option_label("priority"),
            )
            resolution_value = st.selectbox(
                t("ui.p3.coverage_review.resolution_status"),
                options=["open", "in_progress", "resolved", "dismissed"],
                index=["open", "in_progress", "resolved", "dismissed"].index(str(selected_gap.get("resolution_status") or "open")),
                key=f"p3.coverage.status.{selected_gap_id}",
                format_func=option_label("gap_resolution"),
            )
            notes_value = st.text_area(
                t("ui.p3.coverage_review.gap_notes"),
                value=str(selected_gap.get("notes") or ""),
                height=110,
                key=f"p3.coverage.notes.{selected_gap_id}",
            )
            s1, s2 = st.columns(2)
            with s1:
                if st.button(t("ui.p3.coverage_review.save_gap_review"), use_container_width=True, key=f"p3.coverage.save.{selected_gap_id}"):
                    updated = client.update_phase3_coverage_gap(
                        gap_id=selected_gap_id,
                        operator_override_classification=(override_value or None),
                        manual_priority=priority_value,
                        resolution_status=resolution_value,
                        reviewed_by=str(ss.get("auth.username") or ss.get("auth.user") or "operator"),
                        notes=(notes_value.strip() or None),
                    )
                    st.success(
                        t("ui.p3.coverage_review.updated_gap_gap_id_classification_effective", gap_id=(updated.get('gap_id')), effective_classification=(updated.get('effective_classification') or updated.get('classification_status')))
                    )
            with s2:
                if st.button(t("ui.p3.coverage_review.open_manual_sequence_builder"), type="primary", use_container_width=True, key=f"p3.coverage.open_manual.{selected_gap_id}"):
                    ss["p3.manual.coverage_gap_id"] = str(selected_gap_id)
                    ss["p3.manual.gap_pick"] = str(selected_gap_id)
                    ss["p3.manual.name_hint"] = str((gap_context.get("name_hint") if isinstance(gap_context, dict) else "") or selected_gap.get("route_family_hint") or "")
                    ss["p3.manual.operator_hint"] = str((gap_context.get("operator_hint") if isinstance(gap_context, dict) else "") or "")
                    ss["p3.manual.variant_hint"] = str((gap_context.get("variant_hint") if isinstance(gap_context, dict) else "") or "")
                    if str(selected_gap.get("resolution_status") or "") == "open":
                        ss["p3.route_id"] = None
                        ss["phase3.route_id"] = None
                    ss["p3.screen"] = "Manual Sequence Builder"
                    st.rerun()

        related_catalog_rows: List[Dict[str, Any]] = list(gap_context.get("related_catalog_rows") or []) if isinstance(gap_context, dict) else []
        recommended_stops: List[Dict[str, Any]] = list(gap_context.get("recommended_stops") or []) if isinstance(gap_context, dict) else []
        if recommended_stops:
            with st.expander(t("ui.p3.coverage_review.recommended_prod_stops"), expanded=False):
                st.dataframe(recommended_stops, use_container_width=True, hide_index=True)
        if related_catalog_rows:
            with st.expander(t("ui.p3.coverage_review.related_catalog_rows"), expanded=False):
                st.dataframe(related_catalog_rows, use_container_width=True, hide_index=True)
    else:
        st.info(t("ui.p3.coverage_review.no_persisted_coverage_gaps_matched_current"))
