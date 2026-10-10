
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pydeck as pdk
import streamlit as st

from datamind_console.services.geo_api_service import GeoApiService, geo_api_error
from datamind_console.i18n import t
from datamind_console.ui.components.page import page_header


def _docs_spec() -> Dict[str, Dict[str, Any]]:
    return {
        "geocode": {
            "method": "GET",
            "path": "/api/geo/geocode",
            "params": [
                ("q", "required", t("geoapi.param.free_text_query")),
                ("top_k", "optional", t("geoapi.param.max_results")),
                ("bbox", "optional", t("ui.misc.geo_api.south_west_north_east")),
                ("area_key", "optional", t("geoapi.param.region_key")),
                ("types", "optional", t("geoapi.param.types_example")),
                ("language", "optional", t("geoapi.param.language_hint")),
            ],
            "curl": 'curl -G "{base}/api/geo/geocode" --data-urlencode "q=terminal quitumbe" --data-urlencode "top_k=5" -H "Authorization: Bearer <API_KEY>"',
            "response": {
                "query": "terminal quitumbe",
                "endpoint": "geocode",
                "timings_ms": {"total_ms": 0, "embed_ms": 0, "db_ms": 0, "rerank_ms": 0},
                "candidates": [
                    {
                        "id": "...",
                        "name": "Terminal Quitumbe",
                        "entity_type": "TERMINAL",
                        "lat": -0.30,
                        "lon": -78.55,
                        "score": 0.91,
                        "score_parts": {"semantic": 0.46, "lexical": 0.41, "prior": 0.04},
                        "source": "phase2_vector",
                    }
                ],
            },
            "notes": t("geoapi.notes.geocode"),
        },
        "autocomplete": {
            "method": "GET",
            "path": "/api/geo/autocomplete",
            "params": [
                ("q", "required", t("geoapi.param.prefix_query")),
                ("top_k", "optional", t("geoapi.param.max_results")),
                ("area_key", "optional", t("geoapi.param.region_key")),
                ("types", "optional", t("geoapi.param.comma_list")),
                ("bbox", "optional", t("ui.misc.geo_api.south_west_north_east")),
                ("language", "optional", t("geoapi.param.language_hint")),
            ],
            "curl": 'curl -G "{base}/api/geo/autocomplete" --data-urlencode "q=term" --data-urlencode "top_k=8" -H "Authorization: Bearer <API_KEY>"',
            "response": {
                "query": "term",
                "endpoint": "autocomplete",
                "timings_ms": {"total_ms": 0, "embed_ms": 0, "db_ms": 0, "rerank_ms": 0},
                "candidates": [
                    {
                        "id": "...",
                        "name": "Terminal Norte",
                        "entity_type": "TERMINAL",
                        "lat": -0.2,
                        "lon": -78.49,
                        "score": 0.88,
                        "score_parts": {"semantic": 0.25, "lexical": 0.58, "prior": 0.05},
                        "source": "phase2_vector",
                    }
                ],
            },
            "notes": t("geoapi.notes.autocomplete"),
        },
        "reverse": {
            "method": "GET",
            "path": "/api/geo/reverse",
            "params": [
                ("lat", "required", "Latitude"),
                ("lon", "required", "Longitude"),
                ("top_k", "optional", t("geoapi.param.max_results")),
                ("radius_m", "optional", t("geoapi.param.radius")),
                ("types", "optional", t("geoapi.param.comma_list")),
                ("area_key", "optional", t("geoapi.param.region_key")),
                ("q", "optional", t("geoapi.param.tie_break")),
            ],
            "curl": 'curl -G "{base}/api/geo/reverse" --data-urlencode "lat=-0.28" --data-urlencode "lon=-78.52" --data-urlencode "radius_m=800" -H "Authorization: Bearer <API_KEY>"',
            "response": {
                "query": "",
                "endpoint": "reverse",
                "timings_ms": {"total_ms": 0, "embed_ms": 0, "db_ms": 0, "rerank_ms": 0},
                "candidates": [
                    {
                        "id": "...",
                        "name": "La Magdalena",
                        "entity_type": "STOP",
                        "lat": -0.28,
                        "lon": -78.52,
                        "score": 0.82,
                        "score_parts": {"semantic": 0.00, "lexical": 0.00, "prior": 0.82},
                        "source": "phase2_vector",
                    }
                ],
            },
            "notes": t("geoapi.notes.reverse"),
        },
    }


def _pretty_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=True)


def _response_candidates(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    cands = resp.get("candidates") if isinstance(resp, dict) else []
    if isinstance(cands, list):
        return [c for c in cands if isinstance(c, dict)]
    return []


def _render_docs(spec: Dict[str, Any], base_url: str) -> None:
    st.markdown(t("ui.misc.geo_api.docs"))
    st.markdown(t("ui.misc.geo_api.method_method", method=(spec['method'])))
    st.markdown(t("ui.misc.geo_api.path_path", path=(spec['path'])))
    st.markdown(t("ui.misc.geo_api.query_params"))
    for name, req, desc in spec["params"]:
        st.markdown(f"- `{name}` ({t(f'geoapi.param.{req}')}) - {desc}")

    st.markdown(t("ui.misc.geo_api.example_request_curl"))
    st.code(spec["curl"].format(base=base_url), language="bash")

    st.markdown(t("ui.misc.geo_api.example_response_json"))
    st.code(_pretty_json(spec["response"]), language="json")

    st.markdown(t("ui.misc.geo_api.ranking_notes"))
    st.caption(spec["notes"])


def _map_candidates(candidates: List[Dict[str, Any]], selected_id: Optional[str]) -> None:
    rows: List[Dict[str, Any]] = []
    for i, c in enumerate(candidates):
        try:
            lat = float(c.get("lat"))
            lon = float(c.get("lon"))
        except Exception:
            continue
        cid = str(c.get("id") or f"cand-{i}")
        rows.append(
            {
                "id": cid,
                "name": str(c.get("name") or cid),
                "entity_type": str(c.get("entity_type") or ""),
                "score": float(c.get("score") or 0.0),
                "lat": lat,
                "lon": lon,
                "radius": 145 if cid == selected_id else 95,
                "color": [224, 65, 80, 235] if cid == selected_id else [46, 112, 214, 190],
            }
        )

    if not rows:
        st.info(t("ui.misc.geo_api.no_map_points_current_response"))
        return

    df = pd.DataFrame(rows)
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position="[lon, lat]",
        get_fill_color="color",
        get_radius="radius",
        pickable=True,
        auto_highlight=True,
        stroked=True,
        get_line_color=[15, 24, 40, 200],
        line_width_min_pixels=1,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(
            latitude=float(df["lat"].mean()),
            longitude=float(df["lon"].mean()),
            zoom=12.5,
            pitch=0,
        ),
        map_style="light",
        tooltip={"text": "{name} ({entity_type})\nscore: {score}"},
    )
    st.pydeck_chart(deck, use_container_width=True, height=310)


def _run_endpoint_call(service: GeoApiService, endpoint: str, mode: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    try:
        if endpoint == "geocode":
            return service.geocode_local(**payload) if mode == "local" else service.geocode_http(**payload)
        if endpoint == "autocomplete":
            return service.autocomplete_local(**payload) if mode == "local" else service.autocomplete_http(**payload)
        return service.reverse_local(**payload) if mode == "local" else service.reverse_http(**payload)
    except Exception as e:
        return geo_api_error(
            code="REQUEST_FAILED",
            message="Geo API request failed.",
            details={"exception": str(e), "endpoint": endpoint, "payload": payload},
        )


def _render_try_panel(service: GeoApiService, endpoint: str) -> Tuple[Dict[str, Any], Any, List[Dict[str, Any]]]:
    ss = st.session_state
    state_prefix = f"geoapi.{endpoint}"

    st.markdown(t("ui.misc.geo_api.try_it"))
    mode_label = st.radio(
        t("ui.misc.geo_api.execution_mode"),
        options=["http", "local"],
        format_func=lambda v: t(f"geoapi.mode.{v}"),
        key=f"{state_prefix}.mode_choice",
        horizontal=True,
    )
    mode = mode_label

    if endpoint in ("geocode", "autocomplete"):
        payload = {
            "q": st.text_input(t("ui.misc.geo_api.query"), value=ss.get(f"{state_prefix}.q", ""), key=f"{state_prefix}.q"),
            "top_k": int(st.number_input("top_k", min_value=1, max_value=100, value=int(ss.get(f"{state_prefix}.top_k", 8)), step=1, key=f"{state_prefix}.top_k")),
            "bbox": (st.text_input(t("ui.misc.geo_api.bbox"), value=ss.get(f"{state_prefix}.bbox", ""), key=f"{state_prefix}.bbox", placeholder=t("ui.misc.geo_api.south_west_north_east")) or None),
            "area_key": (st.text_input("area_key", value=ss.get(f"{state_prefix}.area_key", ""), key=f"{state_prefix}.area_key") or None),
            "types": (st.text_input("entity_types", value=ss.get(f"{state_prefix}.types", ""), key=f"{state_prefix}.types", placeholder=t("ui.misc.geo_api.stop_poi_address")) or None),
            "language": (st.text_input(t("ui.misc.geo_api.language"), value=ss.get(f"{state_prefix}.language", ""), key=f"{state_prefix}.language") or None),
        }
    else:
        c1, c2 = st.columns(2)
        with c1:
            lat = float(st.number_input(t("ui.misc.geo_api.lat"), value=float(ss.get(f"{state_prefix}.lat", -0.285)), format="%.8f", key=f"{state_prefix}.lat"))
        with c2:
            lon = float(st.number_input(t("ui.misc.geo_api.lon"), value=float(ss.get(f"{state_prefix}.lon", -78.52)), format="%.8f", key=f"{state_prefix}.lon"))
        payload = {
            "lat": lat,
            "lon": lon,
            "top_k": int(st.number_input("top_k", min_value=1, max_value=100, value=int(ss.get(f"{state_prefix}.top_k", 8)), step=1, key=f"{state_prefix}.top_k")),
            "radius_m": float(st.number_input("radius_m", min_value=50.0, max_value=20000.0, value=float(ss.get(f"{state_prefix}.radius_m", 1200.0)), step=50.0, key=f"{state_prefix}.radius_m")),
            "types": (st.text_input("entity_types", value=ss.get(f"{state_prefix}.types", ""), key=f"{state_prefix}.types", placeholder=t("ui.misc.geo_api.stop_poi_address")) or None),
            "area_key": (st.text_input("area_key", value=ss.get(f"{state_prefix}.area_key", ""), key=f"{state_prefix}.area_key") or None),
            "q": (st.text_input(t("ui.misc.geo_api.query_optional_tie_break"), value=ss.get(f"{state_prefix}.q", ""), key=f"{state_prefix}.q") or None),
        }

    if st.button(t("ui.misc.geo_api.send_request"), type="primary", use_container_width=True, key=f"{state_prefix}.send"):
        ss[f"{state_prefix}.payload"] = payload
        ss[f"{state_prefix}.mode"] = mode
        ss[f"{state_prefix}.response"] = _run_endpoint_call(service, endpoint, mode, payload)

    payload_saved = ss.get(f"{state_prefix}.payload") or payload
    response = ss.get(f"{state_prefix}.response")

    if response is None:
        st.info(t("ui.misc.geo_api.configure_parameters_click_send_request"))
        return payload_saved, response, []

    if isinstance(response, dict) and "error" in response:
        st.error((response.get("error") or {}).get("message") or t("geoapi.request_failed"))

    st.markdown(t("ui.misc.geo_api.response_json"))
    st.json(response)

    candidates = _response_candidates(response if isinstance(response, dict) else {})
    if candidates:
        rows = []
        for i, c in enumerate(candidates, start=1):
            rows.append(
                {
                    "rank": i,
                    "name": c.get("name"),
                    "entity_type": c.get("entity_type"),
                    "score": round(float(c.get("score") or 0.0), 6),
                    "lat": c.get("lat"),
                    "lon": c.get("lon"),
                }
            )
        st.markdown(t("ui.misc.geo_api.ranked_candidates"))
        st.dataframe(rows, use_container_width=True, hide_index=True)

        pick_options = [str(c.get("id") or f"cand-{idx}") for idx, c in enumerate(candidates)]
        selected_id = st.selectbox(t("ui.misc.geo_api.selected_candidate"), options=pick_options, index=0, key=f"{state_prefix}.selected_id")
        _map_candidates(candidates, selected_id)

    return payload_saved, response, candidates


def _render_debug_panel(payload: Dict[str, Any], response: Any) -> None:
    st.markdown(t("ui.misc.geo_api.debug"))
    tabs = st.tabs([t("ui.misc.geo_api.timing"), t("ui.misc.geo_api.payload"), t("ui.misc.geo_api.scoring"), t("ui.misc.geo_api.raw")])

    with tabs[0]:
        if isinstance(response, dict):
            timings = response.get("timings_ms") or {}
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("total_ms", f"{float(timings.get('total_ms') or 0.0):.3f}")
            c2.metric("embed_ms", f"{float(timings.get('embed_ms') or 0.0):.3f}")
            c3.metric("db_ms", f"{float(timings.get('db_ms') or 0.0):.3f}")
            c4.metric("rerank_ms", f"{float(timings.get('rerank_ms') or 0.0):.3f}")
        else:
            st.info(t("ui.misc.geo_api.no_timing_payload_yet"))

    with tabs[1]:
        st.json(payload)

    with tabs[2]:
        rows: List[Dict[str, Any]] = []
        if isinstance(response, dict):
            for c in _response_candidates(response):
                parts = c.get("score_parts") or {}
                rows.append(
                    {
                        "id": c.get("id"),
                        "name": c.get("name"),
                        "score": float(c.get("score") or 0.0),
                        "semantic": float(parts.get("semantic") or 0.0),
                        "lexical": float(parts.get("lexical") or 0.0),
                        "prior": float(parts.get("prior") or 0.0),
                    }
                )
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.info(t("ui.misc.geo_api.no_scoring_rows_yet"))

    with tabs[3]:
        st.json(response)


def _render_endpoint_tab(service: GeoApiService, endpoint: str, base_url: str) -> None:
    spec = _docs_spec()[endpoint]
    col_docs, col_try, col_debug = st.columns([1.0, 1.4, 1.0], gap="medium")

    with col_docs:
        _render_docs(spec, base_url)

    with col_try:
        payload, response, _ = _render_try_panel(service, endpoint)

    with col_debug:
        _render_debug_panel(payload, response)


def render_geo_api_view(*, analytics: Any = None, audit: Any = None, **_) -> None:
    service = GeoApiService()
    meta = service.product_meta()
    health = service.local_health()

    page_header(t("page.geo_api.title"), t("page.geo_api.subtitle"), eyebrow=t("page.integration.eyebrow"))

    c1, c2, c3 = st.columns([1.2, 1.1, 1.0], gap="medium")

    with c1:
        st.markdown(t("ui.misc.geo_api.value"))
        st.markdown(t("ui.misc.geo_api.semantic_geocoding"))
        st.markdown(t("ui.misc.geo_api.fast_autocomplete"))
        st.markdown(t("ui.misc.geo_api.reverse_geocoding"))
        st.markdown(t("ui.misc.geo_api.hybrid_ranking"))
        st.markdown(t("ui.misc.geo_api.local_transit_aware"))

    with c2:
        st.markdown(t("ui.misc.geo_api.base_url"))
        st.code(meta["base_url"], language="text")
        st.markdown(t("ui.misc.geo_api.auth"))
        st.code(meta["auth_header"], language="bash")
        st.markdown(t("ui.misc.geo_api.changelog"))
        st.caption(t("ui.misc.geo_api.geo_api_v1"))

    with c3:
        st.markdown(t("ui.misc.geo_api.status"))
        status = str(health.get("status") or "Degraded")
        if status == "Healthy":
            st.success(t("ui.misc.geo_api.healthy"))
        else:
            st.warning(t("ui.misc.geo_api.degraded"))
        st.caption(t("ui.misc.geo_api.queryable_index_bool", bool=(bool(health.get('queryable')))))
        st.markdown(t("ui.misc.geo_api.rate_limits"))
        st.info(t("ui.misc.geo_api.placeholder_only_enforcement_can_be_attached"))

    st.divider()

    tabs = st.tabs([t("ui.misc.geo_api.geocode"), t("ui.misc.geo_api.autocomplete"), t("ui.misc.geo_api.reverse_geocode")])
    with tabs[0]:
        _render_endpoint_tab(service, "geocode", meta["base_url"])
    with tabs[1]:
        _render_endpoint_tab(service, "autocomplete", meta["base_url"])
    with tabs[2]:
        _render_endpoint_tab(service, "reverse", meta["base_url"])
