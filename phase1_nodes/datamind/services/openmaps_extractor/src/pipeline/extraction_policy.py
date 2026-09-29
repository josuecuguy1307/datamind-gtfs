from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


AREA_GROUP_ACTION_POLICY: Dict[str, List[str]] = {
    "valle_core": [
        "stops_quality_bbox",
        "stops_broad_bbox",
        "platforms_bbox",
        "terminals_and_stations_bbox",
    ],
    "conocoto_corridor": [
        "stops_broad_bbox",
        "stops_quality_bbox",
        "stops_named_regex_bbox",
        "platforms_bbox",
    ],
    "amaguana_axis": [
        "stops_broad_bbox",
        "stops_quality_bbox",
        "stops_operator_regex_bbox",
        "terminals_and_stations_bbox",
    ],
    "quito_gateways": [
        "terminals_and_stations_bbox",
        "stops_quality_bbox",
        "platforms_bbox",
        "stops_broad_bbox",
    ],
}

DEFAULT_AREA_GROUP = "default"
DEFAULT_BBOX_EXPANSION_STEPS: Tuple[float, ...] = (0.0, 0.10, 0.25, 0.50)

PHASE1_AREA_SCORE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "default": {
        "candidate_yield": 0.18,
        "stop_balance": 0.14,
        "name_coverage": 0.14,
        "tag_coverage": 0.12,
        "cluster_quality": 0.12,
        "resolve_rate": 0.16,
        "approved_proxy": 0.08,
        "spatial_spread": 0.06,
    },
    "valle_core": {
        "candidate_yield": 0.16,
        "stop_balance": 0.16,
        "name_coverage": 0.16,
        "tag_coverage": 0.12,
        "cluster_quality": 0.12,
        "resolve_rate": 0.16,
        "approved_proxy": 0.08,
        "spatial_spread": 0.04,
    },
    "conocoto_corridor": {
        "candidate_yield": 0.18,
        "stop_balance": 0.12,
        "name_coverage": 0.12,
        "tag_coverage": 0.10,
        "cluster_quality": 0.10,
        "resolve_rate": 0.16,
        "approved_proxy": 0.08,
        "spatial_spread": 0.14,
    },
    "amaguana_axis": {
        "candidate_yield": 0.16,
        "stop_balance": 0.10,
        "name_coverage": 0.10,
        "tag_coverage": 0.10,
        "cluster_quality": 0.14,
        "resolve_rate": 0.16,
        "approved_proxy": 0.08,
        "spatial_spread": 0.16,
    },
    "quito_gateways": {
        "candidate_yield": 0.16,
        "stop_balance": 0.18,
        "name_coverage": 0.14,
        "tag_coverage": 0.14,
        "cluster_quality": 0.12,
        "resolve_rate": 0.14,
        "approved_proxy": 0.10,
        "spatial_spread": 0.02,
    },
}

PHASE1_AREA_SCORE_THRESHOLD: Dict[str, float] = {
    "default": 58.0,
    "valle_core": 62.0,
    "conocoto_corridor": 56.0,
    "amaguana_axis": 52.0,
    "quito_gateways": 60.0,
}

_STOP_RATIO_TARGET: Dict[str, float] = {
    "default": 0.70,
    "valle_core": 0.72,
    "conocoto_corridor": 0.67,
    "amaguana_axis": 0.60,
    "quito_gateways": 0.76,
}


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _clip01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _norm_key(v: Any) -> str:
    return str(v or "").strip().lower()


def _ratio(a: Any, b: Any) -> Optional[float]:
    aa = _f(a)
    bb = _f(b)
    if aa is None or bb is None or bb <= 0:
        return None
    return aa / bb


def _weighted_score(
    component_values: Dict[str, Optional[float]],
    weights: Dict[str, float],
) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    used = 0.0
    total = 0.0
    breakdown: List[Dict[str, Any]] = []
    for key, weight in weights.items():
        val = component_values.get(key)
        if val is None:
            breakdown.append(
                {
                    "component": key,
                    "weight": float(weight),
                    "value_0_1": None,
                    "contribution_0_100": None,
                }
            )
            continue
        vv = _clip01(float(val))
        used += float(weight)
        total += vv * float(weight)
        breakdown.append(
            {
                "component": key,
                "weight": float(weight),
                "value_0_1": round(vv, 4),
                "contribution_0_100": round(vv * float(weight) * 100.0, 2),
            }
        )
    if used <= 0:
        return None, breakdown
    return round((total / used) * 100.0, 2), breakdown


def _resolve_weights(area_group: Optional[str], weights_override: Optional[Dict[str, float]]) -> Dict[str, float]:
    out = dict(PHASE1_AREA_SCORE_WEIGHTS.get("default", {}))
    ag = str(area_group or "").strip().lower()
    if ag and ag in PHASE1_AREA_SCORE_WEIGHTS:
        out.update(PHASE1_AREA_SCORE_WEIGHTS[ag])
    if weights_override:
        out.update({str(k): float(v) for k, v in weights_override.items()})
    return out


def score_extraction_quality(
    metrics: Dict[str, Any],
    *,
    area_group: Optional[str] = None,
    weights_override: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    ag = _norm_key(area_group or metrics.get("area_group"))
    stage = _norm_key(metrics.get("stage") or metrics.get("quality_stage"))
    quality_scope = _norm_key(metrics.get("quality_scope"))
    target_stop_ratio = float(_STOP_RATIO_TARGET.get(ag, _STOP_RATIO_TARGET["default"]))
    weights = _resolve_weights(ag, weights_override)

    raw_count = _f(metrics.get("raw_count"))
    candidate_count = _f(metrics.get("candidate_count"))
    stop_count = _f(metrics.get("stop_count") if metrics.get("stop_count") is not None else metrics.get("stop_signal_count"))
    poi_count = _f(metrics.get("poi_count") if metrics.get("poi_count") is not None else metrics.get("poi_signal_count"))
    name_coverage = _f(metrics.get("name_coverage"))

    tag_cov_public_transport = _f(metrics.get("tag_coverage_public_transport"))
    tag_cov_bus_stop = _f(metrics.get("tag_coverage_highway_bus_stop"))
    tag_cov_bus_station = _f(metrics.get("tag_coverage_amenity_bus_station"))
    tag_cov_platform = _f(metrics.get("tag_coverage_platform"))

    n_clusters = _f(metrics.get("n_clusters") if metrics.get("n_clusters") is not None else metrics.get("cluster_count"))
    singletons = _f(metrics.get("singletons") if metrics.get("singletons") is not None else metrics.get("singleton_count"))
    noise_count = _f(metrics.get("noise_count"))
    resolved_count = _f(
        metrics.get("resolved_count")
        if metrics.get("resolved_count") is not None
        else metrics.get("resolved_total")
        if metrics.get("resolved_total") is not None
        else metrics.get("n_resolved")
    )
    approved_count = _f(metrics.get("approved_count"))
    spatial_spread = _f(metrics.get("spatial_spread_indicator"))

    candidate_yield = _ratio(candidate_count, raw_count)
    candidate_yield_s = None
    if candidate_yield is not None:
        # 0.32+ is already near saturation for this pipeline.
        candidate_yield_s = _clip01(candidate_yield / 0.32)

    stop_ratio = _ratio(stop_count, candidate_count)
    poi_ratio = _ratio(poi_count, candidate_count)
    stop_balance = None
    if stop_ratio is not None:
        stop_balance = _clip01(1.0 - (abs(stop_ratio - target_stop_ratio) / max(target_stop_ratio, 0.01)))

    # Tag coverage averaged over the required indicators.
    tag_values = [x for x in [tag_cov_public_transport, tag_cov_bus_stop, tag_cov_bus_station, tag_cov_platform] if x is not None]
    tag_coverage = (_clip01(sum(tag_values) / len(tag_values)) if tag_values else None)

    noise_ratio = _ratio(noise_count, candidate_count)
    singleton_ratio = _ratio(singletons, n_clusters)
    clustered_ratio = None
    if noise_ratio is not None:
        clustered_ratio = _clip01(1.0 - min(1.0, noise_ratio))

    singleton_inverse = None
    if singleton_ratio is not None:
        singleton_inverse = _clip01(1.0 - min(1.0, singleton_ratio))

    cluster_quality = None
    if clustered_ratio is not None or singleton_inverse is not None:
        cvals = [x for x in [clustered_ratio, singleton_inverse] if x is not None]
        cluster_quality = _clip01(sum(cvals) / len(cvals)) if cvals else None

    resolve_rate = _ratio(resolved_count, candidate_count)
    approved_proxy = _ratio(approved_count, resolved_count if resolved_count else candidate_count)

    component_values: Dict[str, Optional[float]] = {
        "candidate_yield": candidate_yield_s,
        "stop_balance": stop_balance,
        "name_coverage": (_clip01(name_coverage) if name_coverage is not None else None),
        "tag_coverage": tag_coverage,
        "cluster_quality": cluster_quality,
        "resolve_rate": (_clip01(resolve_rate) if resolve_rate is not None else None),
        "approved_proxy": (_clip01(approved_proxy) if approved_proxy is not None else None),
        "spatial_spread": (_clip01(spatial_spread) if spatial_spread is not None else None),
    }

    score, breakdown = _weighted_score(component_values, weights)

    warnings: List[str] = []
    if candidate_yield is not None and candidate_yield < 0.04:
        warnings.append("Low candidate yield from raw extraction.")
    if name_coverage is not None and name_coverage < 0.25:
        warnings.append("Low name coverage across candidates.")
    if tag_coverage is not None and tag_coverage < 0.30:
        warnings.append("Low transport-tag coverage in extracted candidates.")
    if resolve_rate is not None and resolve_rate < 0.35:
        warnings.append("Low resolved/candidate ratio.")
    if spatial_spread is not None and spatial_spread < 0.20:
        warnings.append("Candidate spread is narrow for the selected bbox.")

    contract_flags: List[str] = []
    extraction_only_stage = (stage == "step_build_node_set") or (quality_scope == "extraction_only")
    if extraction_only_stage and raw_count is not None and raw_count > 0:
        if candidate_count is None or candidate_count <= 0:
            contract_flags.append("candidate_materialization_pending")
        if resolved_count is None or resolved_count <= 0:
            contract_flags.append("resolve_materialization_pending")
    score_ready = not contract_flags
    score_provisional = score
    if not score_ready:
        score = None
        warnings.append(
            "Phase1 extraction-only metrics are provisional; candidate/resolve materialization is pending."
        )

    return {
        "score": score,
        "score_ready": score_ready,
        "score_provisional": score_provisional if not score_ready else None,
        "breakdown": breakdown,
        "warnings": warnings,
        "weights": weights,
        "diagnostics": {
            "stage": stage or None,
            "quality_scope": quality_scope or None,
            "contract_flags": contract_flags,
            "raw_count": raw_count,
            "candidate_count": candidate_count,
            "resolved_count": resolved_count,
        },
        "components_raw": {
            "candidate_yield": candidate_yield,
            "stop_ratio": stop_ratio,
            "poi_ratio": poi_ratio,
            "name_coverage": name_coverage,
            "tag_coverage_public_transport": tag_cov_public_transport,
            "tag_coverage_highway_bus_stop": tag_cov_bus_stop,
            "tag_coverage_amenity_bus_station": tag_cov_bus_station,
            "tag_coverage_platform": tag_cov_platform,
            "noise_ratio": noise_ratio,
            "singleton_ratio": singleton_ratio,
            "resolve_rate": resolve_rate,
            "approved_proxy": approved_proxy,
            "spatial_spread_indicator": spatial_spread,
        },
    }


def quality_threshold_for_area(area_group: Optional[str]) -> float:
    ag = str(area_group or "").strip().lower()
    return float(PHASE1_AREA_SCORE_THRESHOLD.get(ag, PHASE1_AREA_SCORE_THRESHOLD["default"]))


def is_low_quality(score: Optional[float], *, area_group: Optional[str] = None) -> bool:
    if score is None:
        return True
    return float(score) < quality_threshold_for_area(area_group)


def ordered_actions_for_area(
    area_group: Optional[str],
    *,
    available_actions: Optional[Iterable[str]] = None,
    requested_actions: Optional[Sequence[str]] = None,
) -> List[str]:
    available = [str(a) for a in (available_actions or []) if str(a).strip()]
    requested = [str(a) for a in (requested_actions or []) if str(a).strip()]

    base: List[str] = []
    ag = str(area_group or "").strip().lower()
    policy_actions = list(AREA_GROUP_ACTION_POLICY.get(ag, []))

    for action in policy_actions:
        if action not in base:
            base.append(action)

    for action in requested:
        if action not in base:
            base.append(action)

    if not base:
        base = requested or available

    if not available:
        return base

    filtered = [a for a in base if a in set(available)]
    if filtered:
        return filtered
    return available


def prioritize_actions_with_recommendation(
    action_order: Sequence[str],
    *,
    preferred_action: Optional[str],
) -> List[str]:
    ordered = [str(a) for a in action_order if str(a).strip()]
    pref = str(preferred_action or "").strip()
    if not pref:
        return ordered
    if pref not in ordered:
        return ordered
    return [pref] + [a for a in ordered if a != pref]


def expand_bbox_by_ratio(base_bbox: Dict[str, float], ratio: float) -> Dict[str, float]:
    south = float(base_bbox["south"])
    west = float(base_bbox["west"])
    north = float(base_bbox["north"])
    east = float(base_bbox["east"])
    lat_span = max(0.0, north - south)
    lon_span = max(0.0, east - west)
    rr = max(0.0, float(ratio))
    return {
        "south": south - (lat_span * rr),
        "west": west - (lon_span * rr),
        "north": north + (lat_span * rr),
        "east": east + (lon_span * rr),
    }


def bbox_retry_plan(
    base_bbox: Dict[str, float],
    *,
    max_retries: int = 4,
    expansion_steps: Optional[Sequence[float]] = None,
) -> List[Dict[str, Any]]:
    steps = list(expansion_steps or DEFAULT_BBOX_EXPANSION_STEPS)
    if not steps:
        steps = [0.0]
    out: List[Dict[str, Any]] = []
    for idx, ratio in enumerate(steps[: max(1, int(max_retries))], start=1):
        out.append(
            {
                "attempt_index": idx,
                "bbox_buffer_ratio": float(ratio),
                "bbox": expand_bbox_by_ratio(base_bbox, float(ratio)),
            }
        )
    return out


def _recommendations_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path).expanduser()
    return Path(__file__).resolve().parents[6] / "data" / "phase1_sector_recommendations.json"


def load_sector_recommendations(path: Optional[str] = None) -> Dict[str, Any]:
    p = _recommendations_path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "generated_at": None, "recommendations": {}}


def sector_recommendation_for_context(
    *,
    sector_key: Optional[str],
    area_group: Optional[str],
    path: Optional[str] = None,
) -> Dict[str, Any]:
    state = load_sector_recommendations(path)
    recs = dict(state.get("recommendations") or {})
    lookup_keys = [_norm_key(sector_key), _norm_key(area_group)]
    for key in lookup_keys:
        if not key:
            continue
        rec = dict(recs.get(key) or {})
        latest = dict(rec.get("latest") or {})
        if not latest:
            continue
        return {
            "recommendation_key": key,
            "sector": rec.get("sector"),
            "area_group": rec.get("area_group"),
            "best_action": latest.get("best_action"),
            "best_bbox_buffer_ratio": latest.get("best_bbox_buffer_ratio"),
            "best_score": latest.get("best_score"),
            "updated_at": rec.get("updated_at"),
            "latest": latest,
        }
    return {}


def save_sector_recommendation(
    *,
    sector_key: str,
    sector_name: str,
    area_group: str,
    recommendation: Dict[str, Any],
    path: Optional[str] = None,
    max_history: int = 20,
) -> Dict[str, Any]:
    p = _recommendations_path(path)
    state = load_sector_recommendations(str(p))
    recs = dict(state.get("recommendations") or {})

    key = str(sector_key or "").strip().lower()
    if not key:
        key = str(sector_name or "").strip().lower().replace(" ", "_")

    existing = dict(recs.get(key) or {})
    history = list(existing.get("history") or [])
    history.append(dict(recommendation))
    history = history[-max(1, int(max_history)) :]

    latest = dict(recommendation)
    recs[key] = {
        "sector": str(sector_name),
        "area_group": str(area_group),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "latest": latest,
        "history": history,
    }

    out = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recommendations": recs,
    }

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=True, indent=2), encoding="utf-8")
    return out
