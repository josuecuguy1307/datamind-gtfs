from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import streamlit as st

from datamind_console.services.geo_api_service import GeoApiService
from datamind_console.i18n import t
from datamind_console.ui.components.page import page_header


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _configured_api_keys_count() -> int:
    raw = (
        os.getenv("GEO_API_API_KEYS")
        or os.getenv("GEO_API_KEYS")
        or os.getenv("GEO_API_ALLOWED_KEYS")
        or ""
    )
    return len([x.strip() for x in raw.split(",") if x.strip()])


def _response_ok(resp: Any) -> bool:
    return isinstance(resp, dict) and "error" not in resp


def _contract_check(endpoint: str, resp: Any) -> Dict[str, Any]:
    if not isinstance(resp, dict):
        return {"ok": False, "reason": t("geoapi.check.not_json_object")}
    if "error" in resp:
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        return {"ok": False, "reason": f"{err.get('code') or 'ERROR'}: {err.get('message') or t('geoapi.request_failed')}"}

    required_root = {"query", "endpoint", "timings_ms", "candidates"}
    missing = [k for k in required_root if k not in resp]
    if missing:
        return {"ok": False, "reason": t("geoapi.check.missing_root_keys", keys=', '.join(missing))}

    timings = resp.get("timings_ms")
    if not isinstance(timings, dict) or "total_ms" not in timings:
        return {"ok": False, "reason": t("geoapi.check.missing_field", field="timings_ms.total_ms")}

    candidates = resp.get("candidates")
    if not isinstance(candidates, list):
        return {"ok": False, "reason": t("geoapi.check.candidates_not_list")}

    if candidates:
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        cand_keys = {"id", "name", "entity_type", "lat", "lon", "score", "score_parts", "source"}
        missing_cand = [k for k in cand_keys if k not in first]
        if missing_cand:
            return {"ok": False, "reason": t("geoapi.check.missing_candidate_keys", keys=', '.join(missing_cand))}
        parts = first.get("score_parts")
        if not isinstance(parts, dict):
            return {"ok": False, "reason": t("geoapi.check.score_parts_not_object")}
        for k in ("semantic", "lexical", "prior"):
            if k not in parts:
                return {"ok": False, "reason": t("geoapi.check.missing_field", field=f"score_parts.{k}")}

    if str(resp.get("endpoint") or "") != endpoint:
        return {"ok": False, "reason": t("geoapi.check.endpoint_mismatch_got", expected=endpoint, got=resp.get('endpoint'))}

    return {"ok": True, "reason": t("geoapi.check.contract_ok")}


def _health_check(resp: Any) -> Dict[str, Any]:
    if not isinstance(resp, dict):
        return {"ok": False, "reason": t("geoapi.check.health_not_json")}
    if "error" in resp:
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        return {"ok": False, "reason": f"{err.get('code') or 'ERROR'}: {err.get('message') or t('geoapi.check.health_failed')}"}
    if "status" not in resp:
        return {"ok": False, "reason": t("geoapi.check.missing_health_status")}
    return {"ok": True, "reason": str(resp.get("status") or "unknown")}


def _render_status_card(title: str, check: Dict[str, Any]) -> None:
    if check.get("ok"):
        st.success(f"{title}: {check.get('reason')}")
    else:
        st.error(f"{title}: {check.get('reason')}")


def _detect_transport_auth_support() -> Dict[str, Any]:
    candidates = []
    env_root = (os.getenv("GTFS_APP_ROOT") or "").strip()
    if env_root:
        candidates.append(Path(env_root))

    here = Path(__file__).resolve()
    candidates.append(here.parents[2].parent / "gtfs_app")
    candidates.append(here.parents[2].parent / "transportapp")

    for root in candidates:
        otp_ctrl = root / "BACKEND" / "src" / "search" / "otpController.js"
        if not otp_ctrl.exists():
            continue
        txt = otp_ctrl.read_text(encoding="utf-8", errors="replace")
        has_auth = ("Authorization" in txt) or ("X-API-Key" in txt)
        has_legacy_fallback = ("LEGACY_GEOCODE_PATH" in txt) or ("/api/search/geocode" in txt)
        return {
            "found": True,
            "path": str(otp_ctrl),
            "supports_auth_header": has_auth,
            "uses_legacy_geocode_fallback": has_legacy_fallback,
        }
    return {"found": False}


def render_geo_api_production_view(*, analytics: Any = None, audit: Any = None, **_) -> None:
    service = GeoApiService()
    ss = st.session_state
    ss.setdefault("geo.prod.results", {})

    page_header(t("page.geo_api_production.title"), t("page.geo_api_production.subtitle"), eyebrow=t("page.integration.eyebrow"))

    require_auth = _env_bool("GEO_API_REQUIRE_AUTH", False)
    rate_limit = _env_int("GEO_API_RATE_LIMIT_PER_MIN", 0)
    public_health = _env_bool("GEO_API_PUBLIC_HEALTH", True)
    keys_count = _configured_api_keys_count()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(t("ui.misc.geo_api_production.http_base"), service.http_base_url)
    c2.metric(t("ui.misc.geo_api_production.auth_required"), "yes" if require_auth else "no")
    c3.metric(t("ui.misc.geo_api_production.api_keys_configured"), str(keys_count))
    c4.metric(t("ui.misc.geo_api_production.rate_limit_min"), str(rate_limit))

    entered_key = st.text_input(
        t("ui.misc.geo_api_production.api_key_validation_optional_if_auth"),
        type="password",
        key="geo.prod.api_key",
        help=t("ui.misc.geo_api_production.used_only_readiness_checks_from_this"),
    )

    if st.button(t("ui.misc.geo_api_production.run_production_readiness_check"), type="primary", use_container_width=True):
        api_key = (entered_key or "").strip() or None
        results: Dict[str, Any] = {}
        results["local_health"] = service.local_health()
        results["http_health"] = service.health_http(api_key=api_key)
        results["geocode_http"] = service.geocode_http(q="terminal quitumbe", top_k=3, api_key=api_key)
        results["autocomplete_http"] = service.autocomplete_http(q="term", top_k=5, api_key=api_key)
        results["reverse_http"] = service.reverse_http(lat=-0.28, lon=-78.52, top_k=3, radius_m=1200, api_key=api_key)
        ss["geo.prod.results"] = results

    results = ss.get("geo.prod.results") or {}
    if not results:
        st.info(t("ui.misc.geo_api_production.run_readiness_checks_validate_production_api"))
        return

    local_h = _health_check(results.get("local_health"))
    http_h = _health_check(results.get("http_health"))
    chk_geo = _contract_check("geocode", results.get("geocode_http"))
    chk_auto = _contract_check("autocomplete", results.get("autocomplete_http"))
    chk_rev = _contract_check("reverse", results.get("reverse_http"))

    st.markdown(t("ui.misc.geo_api_production.readiness_status"))
    r1, r2 = st.columns(2)
    with r1:
        _render_status_card(t("geoapi.card.local_health"), local_h)
        _render_status_card(t("geoapi.card.http_health"), http_h)
        _render_status_card(t("geoapi.card.geocode_contract"), chk_geo)
    with r2:
        _render_status_card(t("geoapi.card.autocomplete_contract"), chk_auto)
        _render_status_card(t("geoapi.card.reverse_contract"), chk_rev)

    auth_gate_ok = True
    if require_auth:
        if keys_count <= 0:
            auth_gate_ok = False
            st.error(t("ui.misc.geo_api_production.auth_required_but_no_api_keys"))
        elif not entered_key:
            auth_gate_ok = False
            st.warning(t("ui.misc.geo_api_production.auth_required_provide_api_key_this"))

    transport_check = _detect_transport_auth_support()
    st.markdown(t("ui.misc.geo_api_production.backend_compatibility"))
    if not transport_check.get("found"):
        st.warning(t("ui.misc.geo_api_production.could_not_locate_gtfs_app_backend"))
    else:
        st.caption(t("ui.misc.geo_api_production.detected_file_path", path=(transport_check.get('path'))))
        if require_auth and not transport_check.get("supports_auth_header"):
            st.error(
                t("ui.misc.geo_api_production.gtfs_app_geocoder_client_does_not")
            )
        else:
            st.success(t("ui.misc.geo_api_production.gtfs_app_compatibility_check_passed_current"))
        if transport_check.get("uses_legacy_geocode_fallback"):
            st.warning(
                t("ui.misc.geo_api_production.gtfs_app_still_references_legacy_geocoder")
            )
        else:
            st.success(t("ui.misc.geo_api_production.gtfs_app_uses_single_geocoder_api"))

    st.markdown(t("ui.misc.geo_api_production.backend_env_snippet"))
    st.code(
        "\n".join(
            [
                f"GEO_API_BASE_URL={service.public_base_url}",
                "GEO_API_GEOCODE_PATH=/api/geo/geocode",
                "# If auth enabled on Geo API, backend must send Authorization header.",
            ]
        ),
        language="bash",
    )

    header_part = '-H "Authorization: Bearer <API_KEY>"' if require_auth else ""
    st.markdown(t("ui.misc.geo_api_production.curl_smoke_tests"))
    st.code(
        "\n".join(
            [
                f'curl -sS -G "{service.public_base_url}/api/geo/health" {header_part}'.strip(),
                f'curl -sS -G "{service.public_base_url}/api/geo/geocode" --data-urlencode "q=terminal quitumbe" --data-urlencode "top_k=3" {header_part}'.strip(),
                f'curl -sS -G "{service.public_base_url}/api/geo/autocomplete" --data-urlencode "q=term" --data-urlencode "top_k=5" {header_part}'.strip(),
                f'curl -sS -G "{service.public_base_url}/api/geo/reverse" --data-urlencode "lat=-0.28" --data-urlencode "lon=-78.52" --data-urlencode "top_k=3" {header_part}'.strip(),
            ]
        ),
        language="bash",
    )

    st.markdown(t("ui.misc.geo_api_production.raw_check_payloads"))
    with st.expander(t("ui.misc.geo_api_production.show_raw_readiness_responses"), expanded=False):
        st.json(results)

    if all(x.get("ok") for x in (local_h, http_h, chk_geo, chk_auto, chk_rev)) and auth_gate_ok:
        st.success(t("ui.misc.geo_api_production.geo_api_production_readiness_checks_passed"))
    else:
        st.warning(t("ui.misc.geo_api_production.geo_api_production_readiness_checks_did"))
