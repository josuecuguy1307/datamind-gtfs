"""Overview: the operational home of ML DATAMIND GTFS.

Answers, from real data only: what is in production, what needs a person,
what failed, what is ready, and what changed recently. Every number comes
from ``datamind_console.metrics`` (definitions in ``docs/METRICS.md``). The
network map stays on demand so the page never runs heavy geometry queries
by default.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import pydeck as pdk
import streamlit as st

from datamind_console.i18n import status_label, t
from datamind_console.metrics import pipeline as pipeline_metrics
from datamind_console.metrics import routes as route_metrics
from datamind_console.services.workspace_context_service import (
    SAMPLE_REGION_RESOURCE,
    active_work,
    create_work,
    ensure_workspace_context,
    select_work,
    start_new_session,
    transient_form_is_dirty,
)
from datamind_console.ui import navigation
from datamind_console.ui.components.kpi import Kpi, kpi_row
from datamind_console.ui.components.page import page_header, section
from datamind_console.ui.components.states import empty_state, error_state, loading
from datamind_console.ui.components.status import Chip, chips_row, tone_for
from datamind_console.ui.formatting import days_since, format_age, format_date, format_number
from datamind_console.ui.theme import palette
from datamind_core.province_config import (
    DEFAULT_PROVINCE,
    get_canton_color,
    get_canton_colors,
    list_active_provinces,
)

DORMANT_AFTER_DAYS = 30


# ---------------------------------------------------------------------------
# Data (cached results, never cached connections)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def _overview_data() -> dict[str, Any]:
    return {
        "routes_all": route_metrics.route_counts(None),
        "provinces": route_metrics.provinces(),
        "freshness": pipeline_metrics.freshness(),
        "attention": pipeline_metrics.attention(),
        "pending_by_quality": pipeline_metrics.approval_pending_by_quality(),
        "feed": pipeline_metrics.current_feed(),
        "activity": pipeline_metrics.recent_activity(14),
    }


@st.cache_data(ttl=60, show_spinner=False)
def _province_counts(province: str) -> dict[str, int]:
    return route_metrics.route_counts(province)


@st.cache_data(ttl=120, show_spinner=False)
def _stop_count(_analytics: Any) -> int:
    return int(_analytics.count_stops())


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _last_activity(freshness: dict[str, Any]) -> Any:
    stamps = [v for v in freshness.values() if v is not None]
    return max(stamps) if stamps else None


def _header(data: dict[str, Any]) -> None:
    last = _last_activity(data["freshness"])
    age_days = days_since(last)
    chips: list[Chip] = []
    if last is not None:
        state = "dormant" if (age_days or 0) > DORMANT_AFTER_DAYS else "fresh"
        chips.append(
            Chip(
                t(f"overview.freshness.{state}", date=format_date(last), age=format_age(last)),
                tone=tone_for("freshness", state),
                icon=":material/schedule:",
                help=t("overview.freshness.help"),
            )
        )
    feed = data["feed"]
    if feed.get("published_at"):
        chips.append(
            Chip(
                t("overview.feed_chip", date=format_date(feed["published_at"])),
                tone="info",
                icon=":material/directions_bus:",
            )
        )
    page_header(
        t("page.overview.title"),
        t("page.overview.subtitle"),
        eyebrow=t("app.name"),
        chips=chips,
    )


def _network_section(data: dict[str, Any], analytics: Any) -> None:
    section(t("overview.network.title"), t("overview.network.caption"))
    counts = data["routes_all"]
    try:
        stops = _stop_count(analytics)
    except Exception:
        stops = None
    kpi_row(
        [
            Kpi(t("metric.route_directions_deployed.label"), counts["route_directions_deployed"], help=t("metric.route_directions_deployed.help")),
            Kpi(t("metric.lines_deployed.label"), counts["lines_deployed"], help=t("metric.lines_deployed.help")),
            Kpi(t("metric.route_directions_clean.label"), counts["route_directions_clean"], help=t("metric.route_directions_clean.help")),
            Kpi(t("metric.lines_clean.label"), counts["lines_clean"], help=t("metric.lines_clean.help")),
            Kpi(t("metric.stops_in_production.label"), stops, help=t("metric.stops_in_production.help")),
        ]
    )

    rows = []
    for prov in data["provinces"]:
        name = prov["province"]
        c = _province_counts(name)
        rows.append(
            {
                t("col.province"): name.capitalize(),
                t("metric.route_directions_total.label"): c["route_directions_total"],
                t("metric.route_directions_deployed.label"): c["route_directions_deployed"],
                t("metric.route_directions_clean.label"): c["route_directions_clean"],
                t("metric.route_directions_needs_work.label"): c["route_directions_needs_work"],
                t("metric.route_directions_unclassified.label"): c["route_directions_unclassified"],
                t("metric.route_directions_not_deployed.label"): c["route_directions_not_deployed"],
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(t("overview.network.table_note"))


def _attention_card(title: str, value: Any, caption: str, *, link_page: str, link_label: str, chips: list[Chip] | None = None, **query: Any) -> None:
    with st.container(border=True):
        st.html(f'<div class="dm-kicker">{title}</div>')
        st.metric(title, format_number(value), label_visibility="collapsed")
        st.caption(caption)
        if chips:
            chips_row(chips)
        navigation.link(link_page, link_label, icon=":material/arrow_forward:", **query)


def _attention_section(data: dict[str, Any]) -> None:
    section(t("overview.attention.title"), t("overview.attention.caption"))
    att = data["attention"]
    counts = data["routes_all"]
    quality_chips = [
        Chip(f"{status_label('quality', r['quality_class'] or None)} · {format_number(r['n'])}",
             tone=tone_for("quality", r["quality_class"] or None))
        for r in data["pending_by_quality"][:4]
    ]
    feed = data["feed"]

    c1, c2, c3 = st.columns(3)
    with c1:
        _attention_card(
            t("overview.card.review_queue.title"), att["approval_pending"], t("overview.card.review_queue.caption"),
            link_page="review", link_label=t("overview.card.open_review"), chips=quality_chips,
        )
    with c2:
        _attention_card(
            t("overview.card.reentry.title"), att["reentry_blocked"],
            t("overview.card.reentry.caption", no_improvement=format_number(att["reentry_no_improvement"])),
            link_page="phases", link_label=t("overview.card.open_reentry"), phase=3, tab="re_entry",
            chips=[Chip(t("overview.card.reentry.ready_chip", n=format_number(att["reentry_ready"])), tone="info")],
        )
    with c3:
        _attention_card(
            t("overview.card.synthetic.title"), att["synthetic_stops_pending"], t("overview.card.synthetic.caption"),
            link_page="review_synthetic", link_label=t("review.queue.open_synthetic"),
        )

    c4, c5, c6 = st.columns(3)
    with c4:
        _attention_card(
            t("overview.card.needs_work.title"), counts["route_directions_needs_work"],
            t("overview.card.needs_work.caption", unclassified=format_number(counts["route_directions_unclassified"])),
            link_page="audit_sample_region", link_label=t("overview.card.open_audit"),
        )
    with c5:
        _attention_card(
            t("overview.card.naming.title"), att["deployed_without_naming_confidence"],
            t("overview.card.naming.caption", low=format_number(att["deployed_low_naming_confidence"])),
            link_page="phases", link_label=t("overview.card.open_phase4"), phase=4,
        )
    with c6:
        published = feed.get("published_at")
        _attention_card(
            t("overview.card.feed.title"),
            format_date(published) if published else "—",
            t("overview.card.feed.caption", age=format_age(published)) if published else t("overview.card.feed.none"),
            link_page="gtfs", link_label=t("overview.card.open_gtfs"),
            chips=[Chip(t("overview.card.feed.file_missing"), tone="danger")] if feed.get("zip_exists") is False else None,
        )


def _activity_section(data: dict[str, Any]) -> None:
    section(t("overview.activity.title"), t("overview.activity.caption"))
    activity = data["activity"]
    if not activity:
        empty_state("overview.activity.empty")
        return
    rows = []
    for item in activity:
        examples = [str(e) for e in (item.get("examples") or []) if e]
        rows.append(
            {
                t("col.day"): format_date(item["day"]),
                t("col.phase"): t(f"phase.{int(item['phase'])}.short"),
                t("col.event"): t(f"activity.{item['kind']}") if item["kind"] else "—",
                t("col.count"): int(item["n"]),
                t("col.examples"): " · ".join(e[:48] for e in examples),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def _freshness_section(data: dict[str, Any]) -> None:
    with st.expander(t("overview.freshness.title"), icon=":material/history:"):
        fr = data["freshness"]
        rows = [
            {t("col.signal"): t(f"overview.freshness.{key}"), t("col.when"): format_date(fr.get(key), with_time=True), t("col.age"): format_age(fr.get(key))}
            for key in ("last_extraction", "last_node_update", "last_place_update", "last_review_activity", "last_reentry_activity", "last_feed_published")
        ]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption(t("overview.freshness.note"))


# ---------------------------------------------------------------------------
# Work context (session-scoped; unchanged behaviour, translated)
# ---------------------------------------------------------------------------

def _work_context_section() -> None:
    work = active_work(st.session_state)
    label = t("overview.work.active", name=work["name"]) if work else t("overview.work.title")
    with st.expander(label, icon=":material/work:", expanded=bool(st.session_state.get("workspace.show_work_setup"))):
        st.caption(t("overview.work.caption"))
        works = st.session_state.get("workspace.works") or {}
        if works:
            labels = {wid: str(item.get("name") or wid) for wid, item in works.items()}
            chosen = st.selectbox(
                t("overview.work.existing"),
                options=[""] + list(labels),
                format_func=lambda wid: t("overview.work.choose") if not wid else labels[wid],
                key="workspace.work_picker",
            )
            if chosen and chosen != st.session_state.get("workspace.active_work_id"):
                select_work(st.session_state, chosen)
                st.rerun()
        else:
            st.caption(t("overview.work.none"))

        with st.form("workspace.create_work", clear_on_submit=True):
            name = st.text_input(t("overview.work.name"), placeholder=t("overview.work.name_placeholder"))
            input_description = st.text_input(t("overview.work.input"), placeholder=t("overview.work.input_placeholder"))
            results_destination = st.text_input(t("overview.work.results"), placeholder=t("overview.work.results_placeholder"))
            region = st.text_input(t("overview.work.region"), placeholder=t("overview.work.region_placeholder"))
            resource = st.selectbox(
                t("overview.work.resource"),
                options=["", SAMPLE_REGION_RESOURCE["id"]],
                format_func=lambda value: t("overview.work.no_resource") if not value else SAMPLE_REGION_RESOURCE["name"],
            )
            submitted = st.form_submit_button(t("overview.work.create"), type="primary")
        if submitted:
            create_work(st.session_state, name=name, input_description=input_description, results_destination=results_destination, region=region, resource_id=resource)
            st.session_state["workspace.show_work_setup"] = False
            st.rerun()

        if work:
            st.caption(" · ".join([
                t("overview.work.input_value", value=work.get("input") or t("overview.work.not_selected")),
                t("overview.work.results_value", value=work.get("results_destination") or t("overview.work.not_selected")),
                t("overview.work.region_value", value=work.get("region") or t("overview.work.not_selected")),
            ]))
            if work.get("resource_id") == SAMPLE_REGION_RESOURCE["id"]:
                st.caption(f"{SAMPLE_REGION_RESOURCE['name']} · `{SAMPLE_REGION_RESOURCE['path']}`")

        if transient_form_is_dirty(st.session_state):
            st.warning(t("overview.work.dirty_warning"))
        confirm = st.checkbox(t("overview.work.new_session_confirm"), key="workspace.new_session.confirm")
        st.button(
            t("overview.work.new_session"),
            key="workspace.new_session",
            disabled=not confirm,
            on_click=lambda: start_new_session(st.session_state),
        )


# ---------------------------------------------------------------------------
# Network map (on demand)
# ---------------------------------------------------------------------------

_DEFAULT_CANTON_COLOR = [114, 224, 255, 200]


def _known_canton_keys(province: str | None = None) -> list[str]:
    """Canton keys for ``province`` (or all active provinces), longest first."""
    if province:
        keys = list(get_canton_colors(province).keys())
    else:
        keys = []
        for p in list_active_provinces() or [DEFAULT_PROVINCE]:
            keys.extend(get_canton_colors(p).keys())
    unique = list(dict.fromkeys(keys))
    unique.sort(key=len, reverse=True)
    return unique


def _canton_color(canton: str, province: str | None = None) -> list[int]:
    if not canton or canton == "other":
        return list(_DEFAULT_CANTON_COLOR)
    return get_canton_color(province or DEFAULT_PROVINCE, canton)


def _canton_from_source(source: str, province: str | None = None) -> str:
    s = (source or "").lower()
    for canton in _known_canton_keys(province):
        if canton in s:
            return canton
    return "other"


def _coords_to_path(coords, gtype: str):
    if not coords:
        return []
    if gtype and "Multi" in gtype:
        merged = []
        for seg in coords:
            if isinstance(seg, list):
                merged.extend(seg)
        return merged
    return coords


@st.cache_data(ttl=180, show_spinner=False)
def _cached_route_geometries(_analytics: Any) -> list:
    return _analytics.get_all_route_geometries(source_filter=None)


def _network_map(analytics: Any) -> None:
    with st.expander(t("overview.map.title"), icon=":material/map:"):
        st.caption(t("overview.map.caption"))
        if not st.toggle(t("overview.map.load"), key="overview.map.load"):
            return
        with loading("overview.map.loading"):
            rows = _cached_route_geometries(analytics)
        paths = []
        for r in rows or []:
            path = _coords_to_path(r.get("coords"), r.get("gtype") or "")
            if len(path) < 2:
                continue
            canton = _canton_from_source(r.get("source", ""))
            paths.append({
                "path": path,
                "name": r.get("route_name", ""),
                "canton": canton,
                "stops": int(r.get("stop_count") or 0),
                "km": round(float(r.get("km") or 0), 1),
                "color": _canton_color(canton),
            })
        if not paths:
            empty_state("overview.map.empty")
            return
        p = palette()
        lats = [c[1] for item in paths for c in item["path"] if isinstance(c, (list, tuple)) and len(c) >= 2]
        lons = [c[0] for item in paths for c in item["path"] if isinstance(c, (list, tuple)) and len(c) >= 2]
        view = pdk.ViewState(latitude=(min(lats) + max(lats)) / 2, longitude=(min(lons) + max(lons)) / 2, zoom=9.6)
        deck = pdk.Deck(
            layers=[pdk.Layer("PathLayer", data=pd.DataFrame(paths), get_path="path", get_color="color",
                              get_width=4, width_min_pixels=2, pickable=True, auto_highlight=True)],
            initial_view_state=view,
            tooltip={"text": "{name}\n{canton} · {stops} · {km} km"},
            map_style=p.map_style,
        )
        st.pydeck_chart(deck, height=560)
        st.caption(t("overview.map.count", n=format_number(len(paths))))


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def render_dashboard_view(*, analytics: Any, audit: Any) -> None:  # noqa: ARG001 - audit kept for the page contract
    ensure_workspace_context(st.session_state)
    try:
        with loading("overview.loading"):
            data = _overview_data()
    except Exception as exc:  # noqa: BLE001 - rendered and logged
        page_header(t("page.overview.title"), t("page.overview.subtitle"), eyebrow=t("app.name"))
        error_state(exc, context_key="error.page.overview", key="overview.retry")
        _work_context_section()
        return

    _header(data)
    _network_section(data, analytics)
    _attention_section(data)
    _activity_section(data)
    _freshness_section(data)
    _work_context_section()
    _network_map(analytics)
