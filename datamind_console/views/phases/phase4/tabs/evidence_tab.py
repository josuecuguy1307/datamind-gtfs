"""Phase 4 · Evidence: browse evidence records and add one by hand.

Reads are read-only. Adding evidence is an explicit human action: the
reviewer is named, the input is validated, and the record and its audit row
are written in one transaction (see ``evidence_matching.create_evidence``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd
import streamlit as st

from datamind_console.db.schema_guard import SchemaNotReadyError
from datamind_console.i18n import has_key, option_label, status_label, t
from datamind_console.phases.phase4_naming import evidence_matching as em
from datamind_console.review.human_review import ReviewInputError, recent_decisions
from datamind_console.ui.formatting import format_date
from datamind_console.ui.components.review import decision_log, flash, reviewer_identity
from datamind_console.ui.components.states import error_state

FLASH_KEY = "p4ev.flash"
PAGE_SIZES = (25, 50, 100)


@dataclass
class Phase4Context:
    user: Optional[dict] = None


def error_text(code: str) -> str:
    """Message for a ReviewInputError code raised by evidence_matching."""
    for key in (f"p4ev.error.{code}", f"hreview.error.{code}"):
        if has_key(key):
            return t(key)
    return t("p4ev.error.generic", code=code)


def schema_ready() -> bool:
    try:
        em.require_schema()
        return True
    except SchemaNotReadyError as exc:
        error_state(exc, retry=False)
        return False


def render_evidence_tab(ctx: Phase4Context) -> None:
    st.subheader(t("p4ev.title"), anchor=False)
    st.caption(t("p4ev.intro"))
    if not schema_ready():
        return
    flash(FLASH_KEY)
    ss = st.session_state
    # Drawn once here; the Matching section reads it with current_reviewer().
    reviewer = reviewer_identity()

    search = st.text_input(t("p4ev.filter.search"), key="p4ev.search", placeholder=t("p4ev.filter.search_hint"))
    f2, f3, f4 = st.columns([3, 3, 2])
    with f2:
        source_type = st.selectbox(t("p4ev.filter.source_type"), ["all", *em.list_source_types()],
                                   key="p4ev.source_type", format_func=option_label("evidence_source"))
    with f3:
        status = st.selectbox(t("p4ev.filter.status"), ["all", *em.STATUS_FILTERS], key="p4ev.status",
                              format_func=option_label("evidence"))
    with f4:
        page_size = st.selectbox(t("p4ev.filter.page_size"), PAGE_SIZES, key="p4ev.page_size")

    page = em.list_evidence(
        search=search, source_type="" if source_type == "all" else source_type,
        status="" if status == "all" else status, limit=int(page_size),
    )
    items = page["items"]
    st.caption(t("p4ev.count", shown=len(items), total=page["total"]))
    if not items:
        st.info(t("p4ev.empty"), icon=":material/inbox:")
    else:
        selected = _evidence_table(items)
        if selected:
            ss["phase4.record_id"] = selected

    record_id = (ss.get("phase4.record_id") or "").strip()
    if record_id:
        _evidence_detail(record_id)

    with st.expander(t("p4ev.create.title"), expanded=False, icon=":material/note_add:"):
        _create_form(reviewer)

    st.markdown(f"#### {t('p4ev.log.title')}")
    decision_log(recent_decisions("evidence"), empty_key="p4ev.log.empty")


def _evidence_table(items: list[dict[str, Any]]) -> Optional[str]:
    df = pd.DataFrame(
        {
            t("p4ev.col.title"): [_title(r) for r in items],
            t("p4ev.col.endpoints"): [" → ".join(x for x in (r.get("from_name"), r.get("to_name")) if x) for r in items],
            t("p4ev.col.status"): [status_label("evidence", r.get("status")) for r in items],
        }
    )
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
                         key="p4ev.table")
    rows = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    return str(items[rows[0]]["record_id"]) if rows else None


def _title(rec: dict[str, Any]) -> str:
    """What a person would call the record: its title or ref/name, else its source and source id."""
    label = rec.get("title") or " · ".join(x for x in (rec.get("route_ref"), rec.get("route_name")) if x)
    return label or f"{status_label('evidence_source', rec.get('source_type'))} {rec.get('source_id') or ''}".strip()


def _evidence_detail(record_id: str) -> None:
    rec = em.evidence_detail(record_id)
    if not rec:
        st.session_state["phase4.record_id"] = ""
        return
    raw = rec.get("raw") or {}
    with st.container(border=True):
        st.markdown(f"#### {t('p4ev.detail.title')}")
        fields = [
            (t("p4ev.col.ref"), rec.get("route_ref")), (t("p4ev.field.route_name"), rec.get("route_name")),
            (t("p4ev.col.operator"), rec.get("operator_name")), (t("p4ev.field.from"), rec.get("from_name")),
            (t("p4ev.field.to"), rec.get("to_name")), (t("p4ev.field.confidence"), f"{float(rec.get('confidence_hint') or 0):.2f}"),
            (t("p4ev.field.geometry"), t("common.yes") if rec.get("geom_geojson") else t("common.no")),
        ]
        st.dataframe(pd.DataFrame({t("col.setting"): [k for k, _ in fields], t("col.value"): [v or "—" for _, v in fields]}),
                     hide_index=True, width="stretch")
        st.caption(t("p4ev.detail.provenance", source=status_label("evidence_source", rec.get("source_type")),
                     source_id=rec.get("source_id"), created=format_date(rec.get("created_at"), with_time=True),
                     by=raw.get("created_by") or t("p4ev.detail.pipeline")))
        if raw.get("note"):
            st.caption(t("p4ev.detail.note", note=raw["note"]))
        labels = rec.get("labels") or []
        if labels:
            st.markdown(t("p4ev.detail.labels"))
            for lab in labels:
                st.caption(t("p4ev.detail.label_row", route=lab.get("route_name") or lab["route_id"][:8],
                             relevance=status_label("match_relevance", lab["relevance"]), who=lab.get("reviewed_by")))
        else:
            st.caption(t("p4ev.detail.no_labels"))
        st.caption(t("p4ev.detail.go_matching"))
        with st.expander(t("p4ev.detail.raw")):
            st.json(raw)


def _create_form(reviewer) -> None:
    st.caption(t("p4ev.create.help"))
    with st.form("p4ev.create", clear_on_submit=False):
        source_type = st.selectbox(t("p4ev.filter.source_type"), em.EVIDENCE_SOURCE_TYPES,
                                   format_func=option_label("evidence_source"))
        title = st.text_input(t("p4ev.create.title_field"), placeholder=t("p4ev.create.title_hint"))
        c1, c2 = st.columns(2)
        with c1:
            route_ref = st.text_input(t("p4ev.col.ref"))
            route_name = st.text_input(t("p4ev.field.route_name"))
            operator_name = st.text_input(t("p4ev.col.operator"))
        with c2:
            from_name = st.text_input(t("p4ev.field.from"))
            to_name = st.text_input(t("p4ev.field.to"))
            confidence = st.slider(t("p4ev.field.confidence"), 0.0, 1.0, 0.65, 0.05)
        note = st.text_area(t("p4ev.create.note"), height=100)
        confirmed = st.checkbox(t("p4ev.create.confirm"))
        submitted = st.form_submit_button(t("p4ev.create.submit"), type="primary", disabled=False)
    if not submitted:
        return
    if not confirmed:
        st.warning(t("p4ev.create.confirm_needed"))
        return
    try:
        out = em.create_evidence(
            reviewer=reviewer, source_type=source_type, title=title, note=note, confidence_hint=confidence,
            fields={"route_ref": route_ref, "route_name": route_name, "operator_name": operator_name,
                    "from_name": from_name, "to_name": to_name},
        )
    except ReviewInputError as exc:
        st.error(error_text(str(exc)))
        return
    except Exception as exc:  # noqa: BLE001 - rendered and logged
        error_state(exc, context_key="p4ev.error.write_failed", retry=False)
        return
    st.session_state["phase4.record_id"] = out["record_id"]
    flash(FLASH_KEY, t("p4ev.create.done"))
    st.rerun()
