"""Review: everything that waits for a person, in one read-only overview.

Each queue keeps its own semantics and its own authoritative screen. This
page only counts and explains them, then links to where decisions are made.
It never changes state. Where no console screen exists for a queue, the
page says so rather than implying a workflow.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from datamind_console.i18n import status_label, t
from datamind_console.metrics import pipeline as pipeline_metrics
from datamind_console.metrics import routes as route_metrics
from datamind_console.ui import navigation
from datamind_console.ui.components.kpi import Kpi, kpi_row
from datamind_console.ui.components.page import page_header, section
from datamind_console.ui.components.states import empty_state, error_state, loading
from datamind_console.ui.components.status import Chip, chips_row, tone_for
from datamind_console.ui.formatting import format_date, format_number


@st.cache_data(ttl=60, show_spinner=False)
def _review_data() -> dict[str, Any]:
    return {
        "attention": pipeline_metrics.attention(),
        "approval_status": pipeline_metrics.approval_status(),
        "approval_split": pipeline_metrics.approval_pending_split(),
        "pending_by_quality": pipeline_metrics.approval_pending_by_quality(),
        "reentry": pipeline_metrics.reentry_outcomes(),
        "gaps": pipeline_metrics.coverage_gaps(),
        "synthetic": pipeline_metrics.synthetic_stops(),
        "sample_region": route_metrics.route_counts("sample_region"),
        "breakdown": route_metrics.deploy_state_breakdown("sample_region"),
        "decisions": pipeline_metrics.recent_decisions(20),
    }


def _queue_header(title: str, owner: str, description: str) -> None:
    st.subheader(title, anchor=False)
    st.caption(f"{owner} · {description}")


def _route_review_queue(data: dict[str, Any]) -> None:
    with st.container(border=True):
        _queue_header(t("review.queue.approval.title"), t("review.owner.phase3_hades"), t("review.queue.approval.description"))
        status = data["approval_status"]
        chips_row([Chip(f"{status_label('queue', k)} · {format_number(v)}", tone=tone_for("queue", k)) for k, v in status.items()])
        split = data["approval_split"]
        c1, c2 = st.columns(2)
        with c1:
            st.metric(t("review.queue.approval.reentry_proposals"), format_number(split["reentry_proposals"]),
                      help=t("review.queue.approval.reentry_proposals_help"), border=True)
            navigation.link("phases", t("review.open_reentry_review"), icon=":material/arrow_forward:", phase=3, tab="re_entry")
        with c2:
            st.metric(t("review.queue.approval.other_proposals"), format_number(split["other_proposals"]),
                      help=t("review.queue.approval.other_proposals_help"), border=True)
            navigation.link("review_proposals", t("review.queue.open_proposals"), icon=":material/arrow_forward:")
        rows = [
            {
                t("col.quality_class"): status_label("quality", r["quality_class"] or None),
                t("col.pending"): int(r["n"]),
                t("col.meaning"): t(f"review.quality_meaning.{r['quality_class'] or '__none__'}"),
            }
            for r in data["pending_by_quality"]
        ]
        if rows:
            st.table(pd.DataFrame(rows).set_index(t("col.quality_class")))


def _reentry_queue(data: dict[str, Any]) -> None:
    r = data["reentry"]
    with st.container(border=True):
        _queue_header(t("review.queue.reentry.title"), t("review.owner.phase3_hades"), t("review.queue.reentry.description"))
        rows = [
            ("ready", r["ready"], "info"),
            ("needs_phase2", r["needs_phase2"], "warning"),
            ("no_playbook", r["no_playbook"], "warning"),
            ("invalid_current_shape", r["invalid_current_shape"], "danger"),
            ("other_error", r["other_error"], "danger"),
            ("no_improvement", r["no_improvement"], "neutral"),
            ("swapped", r["swapped"], "success"),
        ]
        table = [
            {
                t("col.outcome"): t(f"review.reentry_outcome.{key}"),
                t("col.route_directions"): int(n),
                t("col.next_step"): t(f"review.reentry_next.{key}"),
            }
            for key, n, _tone in rows
            if n or key in ("ready", "swapped")
        ]
        chips_row([Chip(f"{t(f'review.reentry_outcome.{k}')} · {format_number(n)}", tone=tone) for k, n, tone in rows[:4] if n])
        st.table(pd.DataFrame(table).set_index(t("col.outcome")))
        navigation.link("phases", t("review.open_reentry_review"), icon=":material/arrow_forward:", phase=3, tab="re_entry")


def _synthetic_stops(data: dict[str, Any]) -> None:
    with st.container(border=True):
        _queue_header(t("review.queue.synthetic.title"), t("review.owner.phase1_3"), t("review.queue.synthetic.description"))
        total = sum(int(x["n"]) for x in data["synthetic"] if x.get("review_state") == "pending")
        st.metric(t("review.queue.synthetic.pending"), format_number(total), border=True)
        if data["synthetic"]:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            t("col.source"): status_label("synthetic_source", x["source_type"]),
                            t("col.review_state"): status_label("review_state", x["review_state"]),
                            t("col.count"): int(x["n"]),
                            t("col.first_created"): format_date(x["first_created"]),
                            t("col.last_created"): format_date(x["last_created"]),
                        }
                        for x in data["synthetic"]
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
        navigation.link("review_synthetic", t("review.queue.open_synthetic"), icon=":material/arrow_forward:")


def _needs_work(data: dict[str, Any]) -> None:
    counts = data["sample_region"]
    with st.container(border=True):
        _queue_header(t("review.queue.cleanliness.title"), t("review.owner.phase3_4"), t("review.queue.cleanliness.description"))
        kpi_row(
            [
                Kpi(t("metric.route_directions_needs_work.label"), counts["route_directions_needs_work"], help=t("metric.route_directions_needs_work.help")),
                Kpi(t("metric.route_directions_not_deployed.label"), counts["route_directions_not_deployed"], help=t("metric.route_directions_not_deployed.help")),
            ]
        )
        reasons = [
            {
                t("col.reason"): status_label("dirty_reason", r["dirty_reason"]),
                t("col.route_directions"): int(r["route_directions"]),
                t("col.lines"): int(r["lines"]),
            }
            for r in data["breakdown"]
            if r["deploy_status"] == "active" and r["cleanliness_status"] == "dirty"
        ]
        if reasons:
            st.dataframe(pd.DataFrame(reasons), hide_index=True, width="stretch")
        navigation.link("audit_sample_region", t("review.open_audit"), icon=":material/arrow_forward:")


def _coverage_gaps(data: dict[str, Any]) -> None:
    gaps = data["gaps"]
    with st.container(border=True):
        _queue_header(t("review.queue.gaps.title"), t("review.owner.phase3"), t("review.queue.gaps.description"))
        chips_row([Chip(f"{status_label('gap_resolution', k)} · {format_number(v)}",
                        tone={"open": "warning", "in_progress": "info", "resolved": "success"}.get(k, "neutral"))
                   for k, v in gaps.items()])
        navigation.link("phases", t("review.open_coverage"), icon=":material/arrow_forward:", phase=3, tab="coverage")


def _decision_log(data: dict[str, Any]) -> None:
    section(t("review.decisions.title"), t("review.decisions.caption"), divider=True)
    decisions = data["decisions"]
    if not decisions:
        empty_state("review.decisions.empty", hint_key="review.decisions.empty_hint")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    t("col.when"): format_date(d["created_at"], with_time=True),
                    t("col.phase"): t(f"phase.{int(d['phase'])}.short"),
                    t("col.decision"): status_label("decision", d["decision"]),
                    t("col.reason"): d.get("reason_code") or "—",
                    t("col.item"): d.get("item_id"),
                }
                for d in decisions
            ]
        ),
        hide_index=True,
        width="stretch",
    )


def render_review_view(**_: Any) -> None:
    page_header(t("page.review.title"), t("page.review.subtitle"), eyebrow=t("page.review.eyebrow"))
    try:
        with loading("review.loading"):
            data = _review_data()
    except Exception as exc:  # noqa: BLE001 - rendered and logged
        error_state(exc, context_key="error.page.review", key="review.retry")
        return

    att = data["attention"]
    kpi_row(
        [
            Kpi(t("review.kpi.approval_pending"), att["approval_pending"], help=t("review.kpi.approval_pending_help")),
            Kpi(t("review.kpi.reentry_ready"), att["reentry_ready"], help=t("review.kpi.reentry_ready_help")),
            Kpi(t("review.kpi.reentry_blocked"), att["reentry_blocked"], help=t("review.kpi.reentry_blocked_help")),
            Kpi(t("review.kpi.synthetic_pending"), att["synthetic_stops_pending"], help=t("review.kpi.synthetic_pending_help")),
            Kpi(t("review.kpi.gaps_open"), data["gaps"].get("open", 0), help=t("review.kpi.gaps_open_help")),
        ]
    )
    st.caption(t("review.read_only_note"))

    left, right = st.columns(2)
    with left:
        _route_review_queue(data)
        _synthetic_stops(data)
        _coverage_gaps(data)
    with right:
        _reentry_queue(data)
        _needs_work(data)
    _decision_log(data)
