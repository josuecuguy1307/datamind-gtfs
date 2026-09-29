# ============================================================
# DEPRECATED — Constructor V2 replaces this module.
# See DEPRECATED_V1_MODULES.md for details.
# This file is preserved for historical reference only.
# ============================================================
from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st


def _status_tokens(tokens: List[str]) -> str:
    items = [str(token).strip() for token in tokens if str(token).strip()]
    return " | ".join(f"`{token}`" for token in items)


def _catalog_row_tokens(row: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    if bool(row.get("is_suppressed_duplicate")):
        tokens.append("suppressed duplicate")
    elif bool(row.get("is_canonical_route_job")):
        tokens.append("canonical route")
    else:
        tokens.append("standalone route")
    if bool(row.get("manual_origin")):
        tokens.append("manual route")
    if str(row.get("latest_coverage_gap_id") or "").strip():
        tokens.append("linked gap")
    if str(row.get("prod_status") or "") == "in_prod":
        tokens.append("in prod")
    elif str(row.get("approval_status") or "") == "approved":
        tokens.append("approved")
    else:
        tokens.append(str(row.get("approval_status") or "pending"))
    return tokens


def _gap_tokens(row: Dict[str, Any]) -> List[str]:
    tokens: List[str] = []
    resolution = str(row.get("resolution_status") or "open").strip()
    classification = str(row.get("effective_classification") or row.get("classification_status") or "needs_review").strip()
    tokens.append(resolution)
    tokens.append(classification)
    if str(row.get("resolved_route_id") or "").strip():
        tokens.append("linked route")
    if str(row.get("resolved_prod_route_id") or "").strip():
        tokens.append("resolved in prod")
    return tokens


def render_coverage_review_tab(ctx, client) -> None:
    del ctx
    ss = st.session_state

    st.subheader("Phase 3 Coverage Review")
    st.caption(
        "Unified catalog, sector coverage, missing-route gaps, JSON export, and handoff into the Manual Sequence Builder."
    )
    st.info(
        "Recommended operator sequence: 1) finish Step05 extraction, 2) run safe canonicalization, "
        "3) review the global catalog, 4) inspect sector coverage, 5) sync and review gaps, 6) use Manual Sequence Builder only when the operator decides manual completion is justified."
    )

    try:
        summary = client.get_phase3_catalog_summary()
    except Exception as exc:
        st.error(f"Could not load Phase 3 coverage summary: {exc}")
        return

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Sectors", int(summary.get("sector_count") or 0))
    with m2:
        st.metric("Route families", int(summary.get("route_family_count") or 0))
    with m3:
        st.metric("Open gaps", int(summary.get("open_gap_count") or 0))
    with m4:
        st.metric("In prod", int(summary.get("prod_family_count") or 0))

    a1, a2 = st.columns(2)
    with a1:
        if st.button("Sync coverage gaps from Phase 3 catalogs", type="primary", use_container_width=True, key="p3.coverage.sync"):
            try:
                out = client.sync_phase3_coverage_gaps()
                ss["p3.coverage.last_sync"] = out
                st.success(
                    f"catalogs={int(out.get('catalog_count') or 0)} | "
                    f"synced_gaps={int(out.get('synced_gap_count') or 0)} | "
                    f"open_gaps={int(out.get('open_gap_count') or 0)}"
                )
            except Exception as exc:
                st.error(f"Coverage sync failed: {exc}")
    with a2:
        if st.button("Export missing-route JSON catalogs", use_container_width=True, key="p3.coverage.export"):
            try:
                out = client.export_phase3_missing_route_catalogs()
                ss["p3.coverage.last_export"] = out
                st.success(
                    f"file_count={int(out.get('file_count') or 0)} | output_dir={out.get('output_dir') or '-'}"
                )
            except Exception as exc:
                st.error(f"Missing-route export failed: {exc}")

    last_sync = dict(ss.get("p3.coverage.last_sync") or {})
    if last_sync:
        st.caption(
            f"Last sync: catalogs={int(last_sync.get('catalog_count') or 0)} | "
            f"synced_gaps={int(last_sync.get('synced_gap_count') or 0)} | "
            f"resolved={int(last_sync.get('resolved_gap_count') or 0)} | "
            f"in_progress={int(last_sync.get('in_progress_gap_count') or 0)}"
        )
    last_export = dict(ss.get("p3.coverage.last_export") or {})
    if list(last_export.get("files") or []):
        with st.expander("Exported missing-route catalogs", expanded=False):
            st.dataframe(list(last_export.get("files") or []), use_container_width=True, hide_index=True)

    with st.expander("Gap classification guide", expanded=False):
        st.markdown(
            "- `still_extractable`: keep extractor-first discipline; retry extraction or extractor patch review should still be considered before manual completion.\n"
            "- `non_reliably_extractable`: extractor evidence is weak enough that manual completion is usually the safer next action.\n"
            "- Operator overrides are allowed, but they should be written into gap notes."
        )

    st.divider()

    try:
        sector_rows = client.list_phase3_sector_coverage(limit=300)
    except Exception as exc:
        sector_rows = []
        st.warning(f"Could not load sector coverage: {exc}")

    sector_options = ["all"]
    sector_options.extend([str(row.get("sector_key") or "") for row in sector_rows if str(row.get("sector_key") or "").strip()])
    sector_key = st.selectbox(
        "Sector filter",
        options=sector_options,
        index=(sector_options.index(str(ss.get("p3.coverage.sector_key") or "all")) if str(ss.get("p3.coverage.sector_key") or "all") in sector_options else 0),
        format_func=lambda key: "All sectors" if key == "all" else next((str(row.get("sector_label") or key) for row in sector_rows if str(row.get("sector_key")) == str(key)), key),
        key="p3.coverage.sector_key",
    )

    st.markdown("#### Sector Coverage")
    if sector_rows:
        sector_table = sector_rows
        if sector_key != "all":
            sector_table = [row for row in sector_rows if str(row.get("sector_key") or "") == str(sector_key)]
        st.dataframe(sector_table, use_container_width=True, hide_index=True)
    else:
        st.info("No sector coverage rows available yet. Run coverage sync after extractor harvests.")

    # ── Interpretation Layer ──
    with st.expander("Route Interpretation & Sector Suggestions", expanded=False):
        st.caption(
            "Non-authoritative interpretation layer. Shows suggested labels, sector assignments, "
            "and gap reclassifications based on structured evidence. Suggestions do not overwrite canonical DB truth."
        )

        interp_tabs = st.tabs(["Sector Suggestions", "Label Interpretation", "Gap Reclassification"])

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
                    st.metric("Unassigned routes", u_cnt)
                with sm2:
                    st.metric("With sector suggestion", s_cnt)
                with sm3:
                    st.metric("Coverage", f"{s_cnt * 100 // max(u_cnt, 1)}%")
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
                    st.markdown("**Detailed suggestions** (top 50 by confidence)")
                    st.dataframe(detail_rows, use_container_width=True, hide_index=True, height=280)
            except Exception as exc:
                st.warning(f"Could not load sector suggestions: {exc}")

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
                    st.markdown("**Non-canonical label interpretations** (top 50)")
                    st.dataframe(unresolved, use_container_width=True, hide_index=True, height=280)
            except Exception as exc:
                st.warning(f"Could not load label interpretations: {exc}")

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
                    st.markdown("**Gap reclassification details**")
                    st.dataframe(gap_details, use_container_width=True, hide_index=True, height=280)
            except Exception as exc:
                st.warning(f"Could not load gap reclassification: {exc}")

    st.divider()

    c1, c2 = st.columns([1.1, 1.0])
    with c1:
        route_family_search = st.text_input(
            "Global catalog search",
            value=str(ss.get("p3.coverage.route_family_search") or ""),
            key="p3.coverage.route_family_search",
            placeholder="route name, hint, ref, place",
        )
    with c2:
        include_suppressed = st.checkbox(
            "Include suppressed duplicates",
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
        st.warning(f"Could not load global catalog: {exc}")

    st.markdown("#### Global Phase 3 Catalog")
    if catalog_rows:
        st.dataframe(catalog_rows, use_container_width=True, hide_index=True, height=320)
        catalog_route_ids = [str(row.get("route_job_id") or "") for row in catalog_rows if str(row.get("route_job_id") or "").strip()]
        if catalog_route_ids:
            default_catalog_route_id = str(ss.get("p3.coverage.catalog_route_id") or catalog_route_ids[0]).strip()
            if default_catalog_route_id not in catalog_route_ids:
                default_catalog_route_id = catalog_route_ids[0]
            selected_catalog_route_id = st.selectbox(
                "Inspect catalog route",
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
            st.caption("Catalog status: " + _status_tokens(_catalog_row_tokens(selected_catalog_row)))
            if bool(selected_catalog_row.get("is_suppressed_duplicate")):
                st.warning(
                    "This is a suppressed duplicate review row. It is not deleted. Review the canonical route row before taking extractor or coverage actions."
                )
            elif str(selected_catalog_row.get("latest_coverage_gap_id") or "").strip() and str(selected_catalog_row.get("prod_status") or "") != "in_prod":
                st.info("This route is linked to a gap but not resolved yet. Continue through Step30 and Step40 before treating the gap as closed.")
            elif str(selected_catalog_row.get("latest_coverage_gap_id") or "").strip() and str(selected_catalog_row.get("prod_status") or "") == "in_prod":
                st.success("This route is in prod and still carries a linked gap id. Confirm the gap row itself shows `resolved` before closing the work item.")
    else:
        st.info("No global Phase 3 catalog rows matched the current filter.")

    st.divider()

    g1, g2 = st.columns([1.1, 1.0])
    with g1:
        resolution_filter = st.selectbox(
            "Gap resolution filter",
            options=["all", "open", "in_progress", "resolved", "dismissed"],
            index=["all", "open", "in_progress", "resolved", "dismissed"].index(str(ss.get("p3.coverage.resolution_filter") or "all")),
            key="p3.coverage.resolution_filter",
        )
    with g2:
        classification_filter = st.selectbox(
            "Gap classification filter",
            options=["all", "still_extractable", "non_reliably_extractable", "needs_review"],
            index=["all", "still_extractable", "non_reliably_extractable", "needs_review"].index(str(ss.get("p3.coverage.classification_filter") or "all")),
            key="p3.coverage.classification_filter",
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
        st.warning(f"Could not load coverage gaps: {exc}")

    st.markdown("#### Missing-Route Work Items")
    if gap_rows:
        st.dataframe(gap_rows, use_container_width=True, hide_index=True, height=320)
        gap_ids = [str(row.get("gap_id") or "") for row in gap_rows if str(row.get("gap_id") or "").strip()]
        default_gap_id = str(ss.get("p3.coverage.selected_gap_id") or (gap_ids[0] if gap_ids else "")).strip()
        if default_gap_id not in gap_ids and gap_ids:
            default_gap_id = gap_ids[0]
        selected_gap_id = st.selectbox(
            "Open gap work item",
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
            st.caption("Gap status: " + _status_tokens(_gap_tokens(selected_gap)))

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
                    "This gap is currently classified as still extractable. Manual construction is allowed, "
                    "but extractor retry or matching patch review should be considered first."
                )
            elif effective_classification == "non_reliably_extractable":
                st.info(
                    "This gap is currently classified as non-reliably-extractable. "
                    "Manual completion is usually the preferred next action."
                )
            resolved_route_id = str(selected_gap.get("resolved_route_id") or "").strip()
            resolved_prod_route_id = str(selected_gap.get("resolved_prod_route_id") or "").strip()
            if resolved_route_id or resolved_prod_route_id:
                st.caption(
                    f"linked_route_id: `{resolved_route_id or '-'}` | resolved_prod_route_id: `{resolved_prod_route_id or '-'}`"
                )
            override_default = str(selected_gap.get("operator_override_classification") or "").strip()
            override_value = st.selectbox(
                "Operator override classification",
                options=["", "still_extractable", "non_reliably_extractable"],
                index=["", "still_extractable", "non_reliably_extractable"].index(override_default if override_default in {"", "still_extractable", "non_reliably_extractable"} else ""),
                key=f"p3.coverage.override.{selected_gap_id}",
            )
            priority_value = st.selectbox(
                "Manual priority",
                options=["low", "medium", "high", "critical"],
                index=["low", "medium", "high", "critical"].index(str(selected_gap.get("manual_priority") or "medium")),
                key=f"p3.coverage.priority.{selected_gap_id}",
            )
            resolution_value = st.selectbox(
                "Resolution status",
                options=["open", "in_progress", "resolved", "dismissed"],
                index=["open", "in_progress", "resolved", "dismissed"].index(str(selected_gap.get("resolution_status") or "open")),
                key=f"p3.coverage.status.{selected_gap_id}",
            )
            notes_value = st.text_area(
                "Gap notes",
                value=str(selected_gap.get("notes") or ""),
                height=110,
                key=f"p3.coverage.notes.{selected_gap_id}",
            )
            s1, s2 = st.columns(2)
            with s1:
                if st.button("Save gap review", use_container_width=True, key=f"p3.coverage.save.{selected_gap_id}"):
                    updated = client.update_phase3_coverage_gap(
                        gap_id=selected_gap_id,
                        operator_override_classification=(override_value or None),
                        manual_priority=priority_value,
                        resolution_status=resolution_value,
                        reviewed_by=str(ss.get("auth.username") or ss.get("auth.user") or "operator"),
                        notes=(notes_value.strip() or None),
                    )
                    st.success(
                        f"Updated gap {updated.get('gap_id')} | classification={updated.get('effective_classification') or updated.get('classification_status')}"
                    )
            with s2:
                if st.button("Open in Manual Sequence Builder", type="primary", use_container_width=True, key=f"p3.coverage.open_manual.{selected_gap_id}"):
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
            with st.expander("Recommended prod stops", expanded=False):
                st.dataframe(recommended_stops, use_container_width=True, hide_index=True)
        if related_catalog_rows:
            with st.expander("Related catalog rows", expanded=False):
                st.dataframe(related_catalog_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No persisted coverage gaps matched the current filter. Run coverage sync first.")
