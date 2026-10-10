"""Review · other proposals.

Pending rows of ``route_prod.approval_queue`` that are not linked to a
re-entry item (re-entry proposals have their own screen in Phase 3). On
2026-10-09 they were all route candidates from ``route_raw`` that never
reached production, mostly classified ``unroutable``.

They cannot be approved from the console: the swap service requires a
re-entry link and refuses non-shippable quality classes. The page says so
plainly, shows the evidence, and lets a person reject proposals with a
reason (logged per row) or leave them pending. No automatic decisions.
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd
import streamlit as st

from datamind_console.i18n import humanize, status_label, t
from datamind_console.review import human_review as hr
from datamind_console.ui.components.kpi import Kpi, kpi_row
from datamind_console.ui.components.page import page_header, section
from datamind_console.ui.components.review import decision_log, flash, reviewer_identity
from datamind_console.ui.components.states import empty_state, error_state, loading
from datamind_console.ui.formatting import format_date, format_number
from datamind_console.ui.theme import palette

_FLASH = "review.proposals.flash"


@st.cache_data(ttl=60, show_spinner=False)
def _rows() -> list[dict]:
    return hr.proposals()


@st.cache_data(ttl=60, show_spinner=False)
def _detail(queue_id: str) -> dict[str, Any]:
    return hr.proposal_detail(queue_id)


@st.cache_data(ttl=60, show_spinner=False)
def _log() -> list[dict]:
    return hr.recent_decisions("approval")


def _clear_caches() -> None:
    for fn in (_rows, _detail, _log):
        fn.clear()


def _as_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _map(detail: dict[str, Any]) -> None:
    import pydeck as pdk

    pal = palette()
    layers, centre = [], None
    for key, colour, label in (("current_geojson", pal.success, "current"), ("proposed_shape_geojson", pal.accent, "proposed")):
        geo = _as_json(detail.get(key))
        if not geo or geo.get("type") != "LineString" or not geo.get("coordinates"):
            continue
        coords = geo["coordinates"]
        centre = centre or coords[len(coords) // 2]
        layers.append(pdk.Layer(
            "PathLayer", data=[{"path": coords, "kind": t(f"hreview.proposals.map.{label}")}],
            get_path="path", get_color=pal.rgba(colour, 230), width_scale=4, width_min_pixels=3, pickable=True,
        ))
    if not layers:
        empty_state("hreview.proposals.no_shape")
        return
    deck = pdk.Deck(
        initial_view_state=pdk.ViewState(latitude=centre[1], longitude=centre[0], zoom=11.5),
        layers=layers, tooltip={"text": "{kind}"}, map_style=pal.map_style,
    )
    st.pydeck_chart(deck, height=380)
    st.caption(t("hreview.proposals.map.legend"))


def _evidence(detail: dict[str, Any]) -> None:
    reasons = _as_json(detail.get("decision_reasons")) or []
    if reasons:
        st.markdown(f"**{t('hreview.proposals.reasons')}**")
        for code in reasons:
            st.markdown(f"- {_reason_text(str(code))}  `{code}`")
    coverage = _as_json(detail.get("stop_coverage_report")) or {}
    geometry = _as_json(detail.get("geometry_report")) or {}
    gaps = coverage.get("gaps") if isinstance(coverage, dict) else None
    severity = (geometry.get("summary") or {}).get("max_severity") if isinstance(geometry, dict) else None
    facts = [
        (t("hreview.proposals.fact.route"), detail.get("route_label") or "—"),
        (t("hreview.proposals.fact.job_status"), status_label("route_job", detail.get("job_status"))),
        (t("hreview.proposals.fact.quality"), status_label("quality", detail.get("quality_class"))),
        (t("hreview.proposals.fact.version"), detail.get("version")),
        (t("hreview.proposals.fact.enqueued"), format_date(detail.get("enqueued_at"), with_time=True)),
        (t("hreview.proposals.fact.gaps"), format_number(len(gaps)) if isinstance(gaps, list) else "—"),
        (t("hreview.proposals.fact.severity"), "—" if severity is None else format_number(float(severity), decimals=2)),
    ]
    st.table(pd.DataFrame(facts, columns=[t("col.field"), t("col.value")]).astype(str).set_index(t("col.field")))


def _reason_text(code: str) -> str:
    """Translate a pipeline reason code; codes like ``dr_deferred_3`` carry a count."""
    from datamind_console.i18n import has_key

    base, _, tail = code.rpartition("_")
    if tail.isdigit() and has_key(f"hreview.proposals.reason.{base}"):
        return t(f"hreview.proposals.reason.{base}", n=int(tail))
    if has_key(f"hreview.proposals.reason.{code}"):
        return t(f"hreview.proposals.reason.{code}")
    return humanize(code)


def _reject_form(selected: list[dict], reviewer: hr.Reviewer) -> None:
    with st.form("review.proposals.reject", border=True):
        st.markdown(f"**{t('hreview.proposals.form.title', n=len(selected))}**")
        reason = st.text_area(t("hreview.proposals.form.reason"), help=t("hreview.proposals.form.reason_help"), height=80)
        confirmed = st.checkbox(t("hreview.proposals.form.confirm", n=len(selected)))
        submitted = st.form_submit_button(
            t("hreview.proposals.form.submit", n=len(selected)), type="primary", icon=":material/block:",
        )
    if not submitted:
        return
    if not confirmed:
        st.warning(t("hreview.error.confirm_required"))
        return
    try:
        hr.reject_proposals([r["queue_id"] for r in selected], reviewer=reviewer, reason=reason)
    except hr.ReviewInputError as exc:
        st.warning(t(f"hreview.error.{exc}"))
        return
    except hr.ReviewConflict:
        st.warning(t("hreview.error.not_pending"))
        _clear_caches()
        return
    except Exception as exc:  # noqa: BLE001
        error_state(exc, context_key="hreview.error.write_failed", retry=False)
        return
    _clear_caches()
    flash(_FLASH, t("hreview.proposals.done", n=len(selected)))
    st.rerun()


def render_review_proposals_view(**_: Any) -> None:
    page_header(t("page.review_proposals.title"), t("page.review_proposals.subtitle"), eyebrow=t("page.review_proposals.eyebrow"))
    flash(_FLASH)
    st.info(t("hreview.principle"), icon=":material/how_to_reg:")
    try:
        with loading():
            rows = _rows()
    except Exception as exc:  # noqa: BLE001
        error_state(exc, context_key="hreview.error.load_failed")
        return
    if not rows:
        empty_state("hreview.proposals.empty")
        section(t("hreview.log.title"), t("hreview.log.caption"), divider=True)
        decision_log(_log(), empty_key="hreview.log.empty")
        return

    by_class: dict[str, int] = {}
    for r in rows:
        by_class[r.get("quality_class") or "__none__"] = by_class.get(r.get("quality_class") or "__none__", 0) + 1
    kpi_row([
        Kpi(t("hreview.proposals.kpi.pending"), len(rows)),
        Kpi(t("hreview.proposals.kpi.routes"), len({r["route_code"] for r in rows})),
        Kpi(t("hreview.proposals.kpi.unroutable"), by_class.get("unroutable", 0)),
        Kpi(t("hreview.proposals.kpi.unclassified"), by_class.get("__none__", 0)),
        Kpi(t("hreview.proposals.kpi.in_production"), sum(1 for r in rows if r.get("in_production"))),
    ])
    st.warning(t("hreview.proposals.cannot_approve"), icon=":material/do_not_disturb_on:")
    reviewer = reviewer_identity()

    section(t("hreview.proposals.queue.title"), t("hreview.proposals.queue.caption"), divider=True)
    only_older = st.toggle(t("hreview.proposals.only_older"), help=t("hreview.proposals.only_older_help"), key="review.proposals.only_older")
    view_rows = [r for r in rows if not only_older or int(r["copy_rank"]) > 1]
    if not view_rows:
        empty_state("hreview.proposals.empty_filtered")
        return
    table = pd.DataFrame(
        {
            t("col.route"): [r.get("route_label") or "—" for r in view_rows],
            t("hreview.proposals.col.copy"): [
                t("hreview.proposals.copy.newest") if int(r["copy_rank"]) == 1
                else t("hreview.proposals.copy.older", k=int(r["copy_rank"]), n=int(r["copies_pending"]))
                for r in view_rows
            ],
            t("hreview.proposals.col.version"): [r["version"] for r in view_rows],
            t("col.quality"): [status_label("quality", r.get("quality_class")) for r in view_rows],
            t("hreview.proposals.col.stops"): [int(r.get("n_proposed_stops") or 0) for r in view_rows],
            t("hreview.proposals.col.job_status"): [status_label("route_job", r.get("job_status")) for r in view_rows],
            t("col.created"): [format_date(r.get("enqueued_at")) for r in view_rows],
        }
    )
    event = st.dataframe(
        table, hide_index=True, width="stretch", height=420, on_select="rerun", selection_mode="multi-row",
        key=f"review.proposals.table.{int(only_older)}",
    )
    picked = [view_rows[i] for i in (getattr(getattr(event, "selection", None), "rows", []) or [])]
    if not picked:
        st.caption(t("hreview.proposals.select_hint"))
    else:
        first = picked[0]
        try:
            detail = _detail(first["queue_id"])
        except Exception as exc:  # noqa: BLE001
            error_state(exc, context_key="hreview.error.load_failed")
            return
        section(t("hreview.proposals.detail.title", route=first.get("route_label") or "—", v=first["version"]),
                t("hreview.proposals.detail.caption", n=len(picked)) if len(picked) > 1 else None, divider=True)
        left, right = st.columns([3, 2])
        with left:
            _map(detail)
        with right:
            _evidence(detail)
        _reject_form(picked, reviewer)

    section(t("hreview.log.title"), t("hreview.log.caption"), divider=True)
    try:
        decision_log(_log(), empty_key="hreview.log.empty")
    except Exception as exc:  # noqa: BLE001
        error_state(exc, context_key="hreview.error.load_failed")
