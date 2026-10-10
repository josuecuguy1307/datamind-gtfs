"""Review · synthetic stops.

Stops the pipeline generated to fill coverage gaps are already in production
tables with ``synthetic_review_state = 'pending'``. This page lets a person
look at each one (where it is, which route directions use it, what real stops
are nearby) and record a decision: verified or rejected. Nothing is decided
automatically, and each decision is written with its audit row in one
transaction (``datamind_console.review.human_review``).
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from datamind_console.i18n import status_label, t
from datamind_console.review import human_review as hr
from datamind_console.ui.components.kpi import Kpi, kpi_row
from datamind_console.ui.components.page import page_header, section
from datamind_console.ui.components.review import decision_log, flash, reviewer_identity
from datamind_console.ui.components.states import empty_state, error_state, loading
from datamind_console.ui.components.status import Chip, chips_row
from datamind_console.ui.formatting import format_date, format_number
from datamind_console.ui.theme import palette

_FLASH = "review.synthetic.flash"
_PAGE = "review.synthetic.page"


@st.cache_data(ttl=60, show_spinner=False)
def _summary() -> dict[str, int]:
    return hr.synthetic_summary()


@st.cache_data(ttl=60, show_spinner=False)
def _page(filters: tuple, page: int) -> tuple[list[dict], int]:
    return hr.synthetic_page(dict(filters), page=page)


@st.cache_data(ttl=60, show_spinner=False)
def _detail(node_id: str) -> dict[str, Any]:
    return hr.synthetic_detail(node_id)


@st.cache_data(ttl=60, show_spinner=False)
def _log() -> list[dict]:
    return hr.recent_decisions("node")


def _clear_caches() -> None:
    for fn in (_summary, _page, _detail, _log):
        fn.clear()


def _filters() -> tuple:
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    with c1:
        confidence = st.selectbox(
            t("hreview.synthetic.filter.confidence"), ["", "high", "medium", "low"],
            format_func=lambda v: t("filter.all") if not v else status_label("synthetic_confidence", v),
            key="review.synthetic.f.confidence",
        )
    with c2:
        usage = st.selectbox(
            t("hreview.synthetic.filter.usage"), ["", "used", "unused"],
            format_func=lambda v: t(f"hreview.synthetic.usage.{v or 'all'}"),
            key="review.synthetic.f.usage",
        )
    with c3:
        superseded = st.selectbox(
            t("hreview.synthetic.filter.superseded"), ["", "no", "yes"],
            format_func=lambda v: t(f"hreview.synthetic.superseded.{v or 'all'}"),
            key="review.synthetic.f.superseded",
        )
    with c4:
        name = st.text_input(t("hreview.synthetic.filter.name"), key="review.synthetic.f.name")
    return tuple(sorted({"confidence": confidence, "usage": usage, "superseded": superseded, "name": name.strip()}.items()))


def _map(detail: dict[str, Any]) -> None:
    import pydeck as pdk

    pal = palette()
    node = detail["node"]
    if node.get("lat") is None:
        empty_state("hreview.synthetic.no_location")
        return
    focus = [{"lat": float(node["lat"]), "lon": float(node["lon"]), "name": node.get("name") or "—", "kind": t("hreview.synthetic.map.this_stop")}]
    others = [
        {"lat": float(r["lat"]), "lon": float(r["lon"]), "name": r.get("name") or "—",
         "kind": t("hreview.synthetic.map.synthetic") if r.get("synthetic") else t("hreview.synthetic.map.stop")}
        for r in detail.get("neighbours", []) if r.get("lat") is not None
    ]
    layers = [
        pdk.Layer("ScatterplotLayer", data=others, get_position="[lon, lat]", get_radius=7,
                  get_fill_color=pal.rgba(pal.info, 220), pickable=True, radius_min_pixels=4),
        pdk.Layer("ScatterplotLayer", data=focus, get_position="[lon, lat]", get_radius=10,
                  get_fill_color=pal.rgba(pal.accent, 255), get_line_color=pal.rgba(pal.background, 255),
                  stroked=True, line_width_min_pixels=2, pickable=True, radius_min_pixels=7),
    ]
    deck = pdk.Deck(
        initial_view_state=pdk.ViewState(latitude=focus[0]["lat"], longitude=focus[0]["lon"], zoom=16.5),
        layers=layers, tooltip={"text": "{kind}: {name}"}, map_style=pal.map_style,
    )
    st.pydeck_chart(deck, height=360)
    st.caption(t("hreview.synthetic.map.legend"))


def _facts(node: dict[str, Any]) -> None:
    chips = [Chip(status_label("synthetic_confidence", node.get("confidence")), tone="info")]
    if node.get("superseded_by"):
        chips.append(Chip(t("hreview.synthetic.superseded_chip"), tone="warning", help=t("hreview.synthetic.superseded_help")))
    if node.get("semantic_spatial_conflict"):
        chips.append(Chip(t("hreview.synthetic.conflict_chip"), tone="danger"))
    chips_row(chips)
    rows = [
        (t("hreview.synthetic.fact.name"), node.get("name") or "—"),
        (t("hreview.synthetic.fact.created"), format_date(node.get("created_at"), with_time=True)),
        (t("hreview.synthetic.fact.created_by"), node.get("created_by") or "—"),
        (t("hreview.synthetic.fact.method"), status_label("synthesis_source", node.get("source_type"))),
        (t("hreview.synthetic.fact.coordinates"), f"{float(node['lat']):.6f}, {float(node['lon']):.6f}" if node.get("lat") is not None else "—"),
        (t("hreview.synthetic.fact.path_distance"), _meters(node.get("path_projection_distance_m"))),
        (t("hreview.synthetic.fact.poi_distance"), _meters(node.get("poi_to_path_distance_m"))),
    ]
    st.table(pd.DataFrame(rows, columns=[t("col.field"), t("col.value")]).set_index(t("col.field")))


def _meters(value: Any) -> str:
    return "—" if value is None else f"{format_number(float(value), decimals=1)} m"


def _routes_table(routes: list[dict]) -> None:
    if not routes:
        st.info(t("hreview.synthetic.no_routes"), icon=":material/info:")
        return
    table = pd.DataFrame(
        {
            t("col.route"): [r.get("route_name") or "—" for r in routes],
            t("col.direction"): [r.get("direction_id") for r in routes],
            t("col.deploy_status"): [status_label("deploy", r.get("deploy_status")) for r in routes],
            t("col.position"): [t("hreview.synthetic.position", n=r.get("position") or "?", total=r.get("n_stops") or "?") for r in routes],
        }
    )
    st.dataframe(table, hide_index=True, width="stretch")


def _decision_form(node: dict[str, Any], reviewer: hr.Reviewer) -> None:
    node_id = node["node_id"]
    with st.form(f"review.synthetic.form.{node_id}", clear_on_submit=False, border=True):
        st.markdown(f"**{t('hreview.synthetic.form.title')}**")
        decision = st.radio(
            t("hreview.synthetic.form.decision"), ["verified", "rejected"], horizontal=True,
            format_func=lambda v: t(f"hreview.synthetic.form.{v}"), index=None,
        )
        reason = st.text_area(t("hreview.synthetic.form.reason"), help=t("hreview.synthetic.form.reason_help"), height=80)
        checked = st.checkbox(t("hreview.synthetic.form.checked"))
        submitted = st.form_submit_button(t("hreview.form.submit"), type="primary", icon=":material/gavel:")
    if not submitted:
        return
    if decision is None:
        st.warning(t("hreview.error.choose_decision"))
        return
    if not checked:
        st.warning(t("hreview.error.confirm_required"))
        return
    try:
        hr.decide_synthetic_stop(node_id, decision, reviewer=reviewer, reason=reason)
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
    st.session_state.pop("review.synthetic.selected", None)
    flash(_FLASH, t(f"hreview.synthetic.done.{decision}", name=node.get("name") or node_id[:8]))
    st.rerun()


def render_review_synthetic_stops_view(**_: Any) -> None:
    page_header(
        t("page.review_synthetic.title"), t("page.review_synthetic.subtitle"), eyebrow=t("page.review_synthetic.eyebrow"),
    )
    flash(_FLASH)
    st.info(t("hreview.principle"), icon=":material/how_to_reg:")

    try:
        with loading():
            summary = _summary()
    except Exception as exc:  # noqa: BLE001
        error_state(exc, context_key="hreview.error.load_failed")
        return
    kpi_row([
        Kpi(t("hreview.synthetic.kpi.pending"), summary.get("pending", 0)),
        Kpi(t("hreview.synthetic.kpi.verified"), summary.get("verified", 0)),
        Kpi(t("hreview.synthetic.kpi.rejected"), summary.get("rejected", 0)),
    ])
    reviewer = reviewer_identity()

    section(t("hreview.synthetic.queue.title"), t("hreview.synthetic.queue.caption"), divider=True)
    filters = _filters()
    if st.session_state.get("review.synthetic.last_filters") != filters:
        st.session_state["review.synthetic.last_filters"] = filters
        st.session_state[_PAGE] = 0
    page = int(st.session_state.get(_PAGE, 0))
    try:
        rows, total = _page(filters, page)
    except Exception as exc:  # noqa: BLE001
        error_state(exc, context_key="hreview.error.load_failed")
        return
    if not rows:
        empty_state("hreview.synthetic.empty")
        return

    pages = max(1, (total + hr.PAGE_SIZE - 1) // hr.PAGE_SIZE)
    nav1, nav2, nav3 = st.columns([1, 2, 1])
    with nav1:
        if st.button(t("action.previous"), disabled=page <= 0, icon=":material/chevron_left:", key="review.synthetic.prev"):
            st.session_state[_PAGE] = page - 1
            st.rerun()
    with nav2:
        st.caption(t("hreview.page_of", page=page + 1, pages=pages, total=format_number(total)))
    with nav3:
        if st.button(t("action.next"), disabled=page + 1 >= pages, icon=":material/chevron_right:", key="review.synthetic.next"):
            st.session_state[_PAGE] = page + 1
            st.rerun()

    table = pd.DataFrame(
        {
            t("col.stop"): [r.get("name") or "—" for r in rows],
            t("col.confidence"): [status_label("synthetic_confidence", r.get("confidence")) for r in rows],
            t("hreview.synthetic.col.deployed_routes"): [int(r["n_deployed"]) for r in rows],
            t("hreview.synthetic.col.all_routes"): [int(r["n_routes"]) for r in rows],
            t("hreview.synthetic.col.superseded"): [t("common.yes") if r["superseded"] else t("common.no") for r in rows],
            t("col.created"): [format_date(r.get("created_at")) for r in rows],
        }
    )
    event = st.dataframe(
        table, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-row",
        key=f"review.synthetic.table.{page}.{hash(filters)}",
    )
    selected_rows = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if not selected_rows:
        st.caption(t("hreview.synthetic.select_hint"))
    else:
        node_id = rows[selected_rows[0]]["node_id"]
        try:
            detail = _detail(node_id)
        except Exception as exc:  # noqa: BLE001
            error_state(exc, context_key="hreview.error.load_failed")
            return
        if not detail:
            empty_state("hreview.error.not_pending")
        else:
            section(detail["node"].get("name") or node_id[:8], t("hreview.synthetic.detail.caption"), divider=True)
            left, right = st.columns([3, 2])
            with left:
                _map(detail)
            with right:
                _facts(detail["node"])
            st.markdown(f"**{t('hreview.synthetic.routes.title', n=len(detail['routes']))}**")
            _routes_table(detail["routes"])
            if detail["node"].get("review_state") != "pending":
                st.info(t("hreview.error.not_pending"))
            else:
                _decision_form(detail["node"], reviewer)

    section(t("hreview.log.title"), t("hreview.log.caption"), divider=True)
    try:
        decision_log(_log(), empty_key="hreview.log.empty")
    except Exception as exc:  # noqa: BLE001
        error_state(exc, context_key="hreview.error.load_failed")
