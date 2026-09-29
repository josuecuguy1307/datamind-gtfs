#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor


DEFAULT_SOURCES = [
    "quito_sur_phase3_catalog.json",
    "catalogo_cumbaya_tumbaco_phase3.json",
    "catalogo_cumbaya_tumbaco_phase3_codex_isolated.json",
]


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in list(value or []):
        if isinstance(item, dict):
            out.append(dict(item))
    return out


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _clean_text(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def _source_name(value: Any) -> Optional[str]:
    text = _clean_text(value)
    return Path(text).name if text else None


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def _coerce_bbox_dict(raw: Any) -> Optional[Dict[str, float]]:
    if not isinstance(raw, dict):
        return None
    try:
        south = float(raw["south"])
        west = float(raw["west"])
        north = float(raw["north"])
        east = float(raw["east"])
    except Exception:
        return None
    if south >= north or west >= east:
        return None
    return {
        "south": south,
        "west": west,
        "north": north,
        "east": east,
    }


def _json_payload_mentions_source_document(payload: Any, source: str) -> bool:
    wanted = _source_name(source)
    if not wanted:
        return False
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if str(key) == "source_document":
                    if _source_name(value) == wanted:
                        return True
                elif isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(list(current))
    return False


def _iter_source_documents(payload: Any) -> Iterable[str]:
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if str(key) == "source_document":
                    name = _source_name(value)
                    if name:
                        yield name
                elif isinstance(value, (dict, list, tuple)):
                    stack.append(value)
        elif isinstance(current, (list, tuple)):
            stack.extend(list(current))


def _row_matches_source(row: Dict[str, Any], source: str) -> bool:
    wanted = _source_name(source)
    if not wanted:
        return False
    if _source_name(row.get("extractor_source")) == wanted:
        return True
    return _json_payload_mentions_source_document(row.get("extractor_review"), wanted)


def _bbox_center_km_point(raw: Any) -> Optional[tuple[float, float]]:
    bbox = _coerce_bbox_dict(raw)
    if not bbox:
        return None
    lat = (bbox["south"] + bbox["north"]) / 2.0
    lon = (bbox["west"] + bbox["east"]) / 2.0
    return lat, lon


def _point_distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    lat_mean = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * 111.32
    dx = (lon2 - lon1) * 111.32 * math.cos(lat_mean)
    return math.sqrt(dx * dx + dy * dy)


def _max_bbox_spread_km(attempts: Sequence[Dict[str, Any]]) -> Optional[float]:
    points = [_bbox_center_km_point(row.get("bbox_used")) for row in attempts]
    points = [point for point in points if point is not None]
    if len(points) < 2:
        return None
    max_km = 0.0
    for idx, left in enumerate(points):
        for right in points[idx + 1 :]:
            max_km = max(max_km, _point_distance_km(left, right))
    return round(max_km, 2)


def _route_hint_signature(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if not text:
        return None
    cleaned = re.sub(r"\s+", " ", text)
    parts = [
        re.sub(r"[^a-z0-9]+", " ", part.lower()).strip()
        for part in re.split(r"\s*(?:-|/|>| to | a )\s*", cleaned, flags=re.IGNORECASE)
    ]
    parts = [part for part in parts if part]
    if len(parts) >= 2:
        return f"{parts[0]} -> {parts[-1]}"
    return parts[0] if parts else None


def _discover_payload(review: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(review.get("discover"))


def _selection_payload(review: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(_discover_payload(review).get("selection_summary"))


def _fetch_payload(review: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(review.get("fetch"))


def _dedupe_payload(review: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(review.get("dedupe"))


def _target_payload(review: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(review.get("target"))


def _hints_payload(review: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(review.get("hints"))


def _geography_payload(review: Dict[str, Any]) -> Dict[str, Any]:
    return _as_dict(review.get("geography"))


def _current_selected_relation_id(row: Dict[str, Any]) -> Optional[int]:
    review = _as_dict(row.get("extractor_review"))
    selection = _selection_payload(review)
    discover = _discover_payload(review)
    return (
        _to_int(selection.get("selected_osm_relation_id"))
        or _to_int(discover.get("chosen_osm_relation_id"))
        or _to_int(row.get("chosen_osm_relation_id"))
    )


def _candidate_preview_relation_ids(row: Dict[str, Any]) -> List[int]:
    review = _as_dict(row.get("extractor_review"))
    discover = _discover_payload(review)
    preview = _as_dict_list(discover.get("candidate_preview"))
    rels = {
        int(rel_id)
        for rel_id in (
            _to_int(item.get("osm_relation_id"))
            for item in preview
        )
        if rel_id is not None
    }
    rels.update(
        int(rel_id)
        for rel_id in list(row.get("candidate_relation_ids") or [])
        if _to_int(rel_id) is not None
    )
    return sorted(rels)


def _reviewable(row: Dict[str, Any]) -> bool:
    review = _as_dict(row.get("extractor_review"))
    return bool(_discover_payload(review).get("relation_extraction_success"))


def _pseudo_attempt_from_review(row: Dict[str, Any]) -> Dict[str, Any]:
    review = _as_dict(row.get("extractor_review"))
    target = _target_payload(review)
    geography = _geography_payload(review)
    hints = _hints_payload(review)
    discover = _discover_payload(review)
    selection = _selection_payload(review)
    fetch = _fetch_payload(review)
    dedupe = _dedupe_payload(review)
    return {
        "source_document": review.get("source_document"),
        "place": target.get("place") or geography.get("place_input"),
        "group": target.get("group"),
        "priority": target.get("priority"),
        "place_bundle": target.get("place_bundle"),
        "seed_origin": target.get("seed_origin"),
        "attempt_type": target.get("attempt_type"),
        "bbox_used": geography.get("bbox_used"),
        "interpretation_source": geography.get("interpretation_source"),
        "route_hint_raw": hints.get("route_hint_raw"),
        "cooperative_hint": hints.get("cooperative_hint"),
        "chosen_osm_relation_id": (
            selection.get("selected_osm_relation_id")
            or discover.get("chosen_osm_relation_id")
            or row.get("chosen_osm_relation_id")
        ),
        "selection_confidence": selection.get("selection_confidence"),
        "fetch_relation_stored": fetch.get("fetch_relation_stored"),
        "fetch_status": fetch.get("fetch_status"),
        "novelty_status": (
            dedupe.get("novelty_status")
            or discover.get("novelty_status")
        ),
        "deleted_duplicate_route_id": None,
    }


def _all_attempts(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    review = _as_dict(row.get("extractor_review"))
    history = _as_dict_list(review.get("attempt_history"))
    if history:
        return history
    if _reviewable(row):
        return [_pseudo_attempt_from_review(row)]
    return []


def _source_attempts(row: Dict[str, Any], source: str) -> List[Dict[str, Any]]:
    review = _as_dict(row.get("extractor_review"))
    history = _as_dict_list(review.get("attempt_history"))
    matching = [
        item
        for item in history
        if _json_payload_mentions_source_document(item, source)
    ]
    if matching:
        return matching
    wanted = _source_name(source)
    if wanted and (
        _source_name(row.get("extractor_source")) == wanted
        or _source_name(review.get("source_document")) == wanted
    ):
        return [_pseudo_attempt_from_review(row)]
    return []


def _latest_source_attempt(row: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
    attempts = _source_attempts(row, source)
    return dict(attempts[-1]) if attempts else None


def _selected_relation_from_source_view(row: Dict[str, Any], source: str) -> Optional[int]:
    latest = _latest_source_attempt(row, source)
    return (
        _to_int(_as_dict(latest).get("chosen_osm_relation_id"))
        or _current_selected_relation_id(row)
    )


def _counter_list(counter: Counter[str], *, top_n: int = 8) -> List[Dict[str, Any]]:
    return [
        {"label": label, "count": int(count)}
        for label, count in counter.most_common(top_n)
    ]


def _sort_unique_ints(values: Iterable[Any]) -> List[int]:
    out = {
        int(value)
        for value in values
        if _to_int(value) is not None
    }
    return sorted(out)


def _sort_unique_text(values: Iterable[Any]) -> List[str]:
    out = {
        str(value).strip()
        for value in values
        if _clean_text(value)
    }
    return sorted(out)


@dataclass
class SuspiciousMerge:
    route_id: str
    host_relation_id: Optional[int]
    source_context_count: int
    total_host_context_count: int
    source_names_in_host: List[str]
    source_selected_relation_ids: List[int]
    current_candidate_relation_ids: List[int]
    current_preview_missing_relation_ids: List[int]
    distinct_places: List[str]
    distinct_groups: List[str]
    distinct_place_bundles: List[str]
    distinct_seed_origins: List[str]
    distinct_route_hints: List[str]
    distinct_route_hint_signatures: List[str]
    distinct_cooperative_hints: List[str]
    distinct_attempt_types: List[str]
    bbox_spread_km: Optional[float]
    source_selected_relation_mismatch: bool
    reason: str
    host_extractor_source: Optional[str]


def _build_suspicious_merge(row: Dict[str, Any], source: str) -> Optional[SuspiciousMerge]:
    attempts = _source_attempts(row, source)
    if len(attempts) < 2:
        return None

    selected_relation_ids = _sort_unique_ints(
        item.get("chosen_osm_relation_id")
        for item in attempts
    )
    places = _sort_unique_text(item.get("place") for item in attempts)
    groups = _sort_unique_text(item.get("group") for item in attempts)
    bundles = _sort_unique_text(item.get("place_bundle") for item in attempts)
    seed_origins = _sort_unique_text(item.get("seed_origin") for item in attempts)
    route_hints = _sort_unique_text(item.get("route_hint_raw") for item in attempts)
    route_hint_signatures = _sort_unique_text(
        _route_hint_signature(item.get("route_hint_raw"))
        for item in attempts
    )
    cooperative_hints = _sort_unique_text(item.get("cooperative_hint") for item in attempts)
    attempt_types = _sort_unique_text(item.get("attempt_type") for item in attempts)
    bbox_spread_km = _max_bbox_spread_km(attempts)

    host_relation_id = _to_int(row.get("chosen_osm_relation_id"))
    current_candidate_relation_ids = _candidate_preview_relation_ids(row)
    current_preview_missing = sorted(
        {
            relation_id
            for relation_id in selected_relation_ids
            if relation_id not in current_candidate_relation_ids
        }
    )

    reasons: List[str] = []
    if len(selected_relation_ids) > 1:
        reasons.append("multiple selected relation ids merged into one host row")
    if len(route_hints) > 1:
        reasons.append("distinct route hints were compacted together")
    if len(route_hint_signatures) > 1:
        reasons.append("route hint endpoint/corridor signatures diverge")
    if len(places) > 1 and (
        len(groups) > 1 or len(bundles) > 1 or (bbox_spread_km is not None and bbox_spread_km >= 4.0)
    ):
        reasons.append("distinct place/corridor bundles share one canonical row")
    if len(seed_origins) > 1 and len(places) > 1:
        reasons.append("different seed origins contributed to one canonical row")
    if host_relation_id is not None and any(rel_id != host_relation_id for rel_id in selected_relation_ids):
        reasons.append("host chosen relation differs from merged source relation ids")
    if current_preview_missing:
        reasons.append("current candidate preview no longer represents all merged relation ids")

    if not reasons:
        return None

    host_source_names = _sort_unique_text(
        _iter_source_documents(_all_attempts(row))
    )
    return SuspiciousMerge(
        route_id=str(row.get("route_id") or ""),
        host_relation_id=host_relation_id,
        source_context_count=len(attempts),
        total_host_context_count=len(_all_attempts(row)),
        source_names_in_host=host_source_names,
        source_selected_relation_ids=selected_relation_ids,
        current_candidate_relation_ids=current_candidate_relation_ids,
        current_preview_missing_relation_ids=current_preview_missing,
        distinct_places=places,
        distinct_groups=groups,
        distinct_place_bundles=bundles,
        distinct_seed_origins=seed_origins,
        distinct_route_hints=route_hints,
        distinct_route_hint_signatures=route_hint_signatures,
        distinct_cooperative_hints=cooperative_hints,
        distinct_attempt_types=attempt_types,
        bbox_spread_km=bbox_spread_km,
        source_selected_relation_mismatch=(
            host_relation_id is not None
            and any(rel_id != host_relation_id for rel_id in selected_relation_ids)
        ),
        reason="; ".join(reasons),
        host_extractor_source=_source_name(row.get("extractor_source")),
    )


def _severity_key(item: SuspiciousMerge) -> tuple[int, int, int, str]:
    reason_count = len([part for part in item.reason.split("; ") if part.strip()])
    return (
        -reason_count,
        -int(item.source_context_count),
        -int(item.total_host_context_count),
        item.route_id,
    )


def _classify_verdict(
    *,
    preserved_contexts: int,
    canonical_rows: int,
    compaction_ratio: Optional[float],
    merge_ambiguity_score: float,
    source_purity_score: Optional[float],
    suspicious_merge_count: int,
    relation_mismatch_rows: int,
) -> str:
    if preserved_contexts <= 0 or canonical_rows <= 0:
        return "healthy"
    if (
        merge_ambiguity_score >= 0.40
        or (
            compaction_ratio is not None
            and compaction_ratio >= 3.0
            and suspicious_merge_count >= 2
        )
    ):
        return "clearly collapsing useful diversity"
    if (
        merge_ambiguity_score >= 0.15
        or (
            compaction_ratio is not None
            and compaction_ratio >= 2.0
            and suspicious_merge_count >= 1
        )
        or (
            source_purity_score is not None
            and source_purity_score < 0.55
            and suspicious_merge_count >= 1
        )
    ):
        return "too aggressive"
    if (
        suspicious_merge_count >= 1
        or relation_mismatch_rows >= 1
        or (compaction_ratio is not None and compaction_ratio >= 1.2)
        or (source_purity_score is not None and source_purity_score < 0.80)
    ):
        return "slightly aggressive but acceptable"
    return "healthy"


def _fetch_rows(dsn: str) -> List[Dict[str, Any]]:
    sql = """
        SELECT
          rj.route_id::text AS route_id,
          rj.created_at,
          rj.status,
          rj.extractor_source,
          rj.chosen_osm_relation_id,
          rj.extractor_review,
          COALESCE(rc.candidate_count, 0) AS relation_candidate_count,
          COALESCE(rc.candidate_relation_ids, ARRAY[]::bigint[]) AS candidate_relation_ids,
          (orr.route_id IS NOT NULL) AS raw_relation_available
        FROM route_raw.route_jobs rj
        LEFT JOIN (
          SELECT
            route_id,
            COUNT(*)::int AS candidate_count,
            ARRAY_AGG(DISTINCT osm_relation_id ORDER BY osm_relation_id) AS candidate_relation_ids
          FROM route_raw.relation_candidates
          GROUP BY route_id
        ) rc
          ON rc.route_id = rj.route_id
        LEFT JOIN route_raw.osm_relations_raw orr
          ON orr.route_id = rj.route_id
        WHERE rj.extractor_review IS NOT NULL
        ORDER BY rj.created_at DESC
    """
    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall() or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["extractor_review"] = _as_dict(item.get("extractor_review"))
        item["candidate_relation_ids"] = [
            int(rel_id)
            for rel_id in list(item.get("candidate_relation_ids") or [])
            if _to_int(rel_id) is not None
        ]
        out.append(item)
    return out


def _available_sources(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in rows:
        direct = _source_name(row.get("extractor_source"))
        if direct:
            counts[direct] += 1
        review = _as_dict(row.get("extractor_review"))
        top = _source_name(review.get("source_document"))
        if top and top != direct:
            counts[top] += 1
        for source in _iter_source_documents(review.get("attempt_history")):
            counts[source] += 1
    return _counter_list(counts, top_n=max(12, len(counts)))


def _compute_source_metrics(rows: Sequence[Dict[str, Any]], source: str) -> Dict[str, Any]:
    wanted = _source_name(source)
    direct_rows = [
        row
        for row in rows
        if _source_name(row.get("extractor_source")) == wanted
    ]
    source_visible_rows = [
        row
        for row in rows
        if _row_matches_source(row, wanted)
    ]
    reviewable_rows = [
        row
        for row in source_visible_rows
        if _reviewable(row)
    ]

    source_contexts: List[Dict[str, Any]] = []
    novelty_counter: Counter[str] = Counter()
    place_counter: Counter[str] = Counter()
    cooperative_counter: Counter[str] = Counter()
    route_hint_counter: Counter[str] = Counter()
    source_visible_selected_relation_ids: List[int] = []
    host_relation_ids: List[int] = []
    current_candidate_relation_ids: set[int] = set()
    fetch_status_counter: Counter[str] = Counter()
    host_context_total = 0
    contaminated_rows = 0
    relation_mismatch_rows = 0
    canonical_rows_from_other_sources = 0
    suspicious_merges: List[SuspiciousMerge] = []
    direct_host_relation_counter: Counter[str] = Counter()
    direct_selected_relation_counter: Counter[str] = Counter()
    direct_relation_mismatch_count = 0

    for row in direct_rows:
        host_relation_id = _to_int(row.get("chosen_osm_relation_id"))
        source_selected_relation_id = _selected_relation_from_source_view(row, wanted)
        if host_relation_id is not None:
            direct_host_relation_counter[str(host_relation_id)] += 1
        if source_selected_relation_id is not None:
            direct_selected_relation_counter[str(source_selected_relation_id)] += 1
        if (
            host_relation_id is not None
            and source_selected_relation_id is not None
            and host_relation_id != source_selected_relation_id
        ):
            direct_relation_mismatch_count += 1

    for row in reviewable_rows:
        attempts = _source_attempts(row, wanted)
        if not attempts:
            attempts = [_pseudo_attempt_from_review(row)]
        source_contexts.extend(attempts)
        host_attempts = _all_attempts(row)
        host_context_total += len(host_attempts)

        host_sources = _sort_unique_text(_iter_source_documents(host_attempts))
        if len(host_sources) > 1:
            contaminated_rows += 1
        elif host_sources and host_sources[0] != wanted:
            contaminated_rows += 1

        latest = dict(attempts[-1])
        selected_relation_id = _to_int(
            latest.get("chosen_osm_relation_id")
        ) or _current_selected_relation_id(row)
        host_relation_id = _to_int(row.get("chosen_osm_relation_id"))
        if selected_relation_id is not None:
            source_visible_selected_relation_ids.append(selected_relation_id)
        if host_relation_id is not None:
            host_relation_ids.append(host_relation_id)
        if (
            selected_relation_id is not None
            and host_relation_id is not None
            and selected_relation_id != host_relation_id
        ):
            relation_mismatch_rows += 1

        if _source_name(row.get("extractor_source")) != wanted:
            canonical_rows_from_other_sources += 1

        for rel_id in _candidate_preview_relation_ids(row):
            current_candidate_relation_ids.add(rel_id)

        novelty = _clean_text(latest.get("novelty_status")) or "unknown"
        novelty_counter[novelty] += 1

        fetch_status = _clean_text(_fetch_payload(_as_dict(row.get("extractor_review"))).get("fetch_status"))
        if fetch_status:
            fetch_status_counter[fetch_status] += 1

        place = _clean_text(latest.get("place"))
        if place:
            place_counter[place] += 1
        cooperative = _clean_text(latest.get("cooperative_hint"))
        if cooperative:
            cooperative_counter[cooperative] += 1
        route_hint = _clean_text(latest.get("route_hint_raw"))
        if route_hint:
            route_hint_counter[route_hint] += 1

        suspicious = _build_suspicious_merge(row, wanted)
        if suspicious:
            suspicious_merges.append(suspicious)

    context_selected_relation_ids = _sort_unique_ints(
        item.get("chosen_osm_relation_id")
        for item in source_contexts
    )
    visible_selected_relation_ids = _sort_unique_ints(source_visible_selected_relation_ids)
    unique_candidate_relation_ids = sorted(current_candidate_relation_ids)
    preserved_contexts = len(source_contexts)
    canonical_rows = len(reviewable_rows)
    compaction_ratio = round(preserved_contexts / max(1, canonical_rows), 3) if canonical_rows else None
    relation_diversity_retention = (
        round(len(context_selected_relation_ids) / max(1, preserved_contexts), 3)
        if preserved_contexts
        else None
    )
    visible_relation_collapse_ratio = (
        round(len(context_selected_relation_ids) / max(1, len(visible_selected_relation_ids)), 3)
        if visible_selected_relation_ids
        else None
    )
    merge_ambiguity_score = (
        round(len(suspicious_merges) / max(1, canonical_rows), 3)
        if canonical_rows
        else 0.0
    )
    source_purity_score = (
        round(preserved_contexts / max(1, host_context_total), 3)
        if host_context_total
        else None
    )
    dominant_relation_share = None
    if source_contexts:
        relation_counter = Counter(
            _to_int(item.get("chosen_osm_relation_id"))
            for item in source_contexts
            if _to_int(item.get("chosen_osm_relation_id")) is not None
        )
        if relation_counter:
            dominant_relation_share = round(relation_counter.most_common(1)[0][1] / len(source_contexts), 3)

    verdict = _classify_verdict(
        preserved_contexts=preserved_contexts,
        canonical_rows=canonical_rows,
        compaction_ratio=compaction_ratio,
        merge_ambiguity_score=merge_ambiguity_score,
        source_purity_score=source_purity_score,
        suspicious_merge_count=len(suspicious_merges),
        relation_mismatch_rows=relation_mismatch_rows,
    )

    suspicious_merges.sort(key=_severity_key)

    return {
        "source": wanted,
        "present": bool(source_visible_rows),
        "direct_raw_rows": len(direct_rows),
        "source_visible_rows": len(source_visible_rows),
        "reviewable_rows": len(reviewable_rows),
        "canonical_rows_visible_after_compaction": canonical_rows,
        "preserved_reviewable_contexts": preserved_contexts,
        "host_context_total": host_context_total,
        "contexts_from_other_sources_in_same_host_rows": max(0, host_context_total - preserved_contexts),
        "canonical_rows_from_other_sources": canonical_rows_from_other_sources,
        "source_purity_score": source_purity_score,
        "contaminated_canonical_row_count": contaminated_rows,
        "compaction_ratio": compaction_ratio,
        "relation_diversity_retention": relation_diversity_retention,
        "visible_relation_collapse_ratio": visible_relation_collapse_ratio,
        "merge_ambiguity_score": merge_ambiguity_score,
        "suspicious_merge_count": len(suspicious_merges),
        "relation_mismatch_row_count": relation_mismatch_rows,
        "direct_relation_mismatch_count": direct_relation_mismatch_count,
        "dominant_relation_share": dominant_relation_share,
        "direct_host_relation_counts": _counter_list(direct_host_relation_counter),
        "direct_selected_relation_counts": _counter_list(direct_selected_relation_counter),
        "unique_selected_relation_ids_context": context_selected_relation_ids,
        "unique_selected_relation_ids_visible": visible_selected_relation_ids,
        "unique_host_relation_ids": _sort_unique_ints(host_relation_ids),
        "unique_candidate_relation_ids_visible": unique_candidate_relation_ids,
        "novelty_labels": dict(novelty_counter),
        "fetch_status_distribution": dict(fetch_status_counter),
        "top_places": _counter_list(place_counter),
        "top_cooperative_hints": _counter_list(cooperative_counter),
        "top_route_hints": _counter_list(route_hint_counter),
        "suspicious_merges": [
            {
                "route_id": item.route_id,
                "host_relation_id": item.host_relation_id,
                "source_context_count": item.source_context_count,
                "total_host_context_count": item.total_host_context_count,
                "host_extractor_source": item.host_extractor_source,
                "source_names_in_host": item.source_names_in_host,
                "source_selected_relation_ids": item.source_selected_relation_ids,
                "current_candidate_relation_ids": item.current_candidate_relation_ids,
                "current_preview_missing_relation_ids": item.current_preview_missing_relation_ids,
                "distinct_places": item.distinct_places,
                "distinct_groups": item.distinct_groups,
                "distinct_place_bundles": item.distinct_place_bundles,
                "distinct_seed_origins": item.distinct_seed_origins,
                "distinct_route_hints": item.distinct_route_hints,
                "distinct_route_hint_signatures": item.distinct_route_hint_signatures,
                "distinct_cooperative_hints": item.distinct_cooperative_hints,
                "distinct_attempt_types": item.distinct_attempt_types,
                "bbox_spread_km": item.bbox_spread_km,
                "source_selected_relation_mismatch": item.source_selected_relation_mismatch,
                "reason": item.reason,
            }
            for item in suspicious_merges
        ],
        "verdict": verdict,
    }


def _build_requested_sources(rows: Sequence[Dict[str, Any]], requested: Sequence[str]) -> List[Dict[str, Any]]:
    available_names = {
        entry["label"]
        for entry in _available_sources(rows)
    }
    out: List[Dict[str, Any]] = []
    for item in requested:
        name = _source_name(item) or str(item)
        out.append(
            {
                "requested": str(item),
                "resolved": name,
                "present": name in available_names or any(_row_matches_source(row, name) for row in rows),
            }
        )
    return out


def _print_source_report(metrics: Dict[str, Any], *, top_examples: int) -> None:
    print(f"\n== Source: {metrics['source']} ==")
    if not metrics["present"]:
        print("present_in_db: false")
        return

    print(f"verdict: {metrics['verdict']}")
    print(f"direct_raw_rows: {metrics['direct_raw_rows']}")
    print(f"source_visible_rows: {metrics['source_visible_rows']}")
    print(f"reviewable_rows: {metrics['reviewable_rows']}")
    print(f"canonical_rows_visible_after_compaction: {metrics['canonical_rows_visible_after_compaction']}")
    print(f"preserved_reviewable_contexts: {metrics['preserved_reviewable_contexts']}")
    print(f"compaction_ratio: {metrics['compaction_ratio']}")
    print(f"relation_diversity_retention: {metrics['relation_diversity_retention']}")
    print(f"visible_relation_collapse_ratio: {metrics['visible_relation_collapse_ratio']}")
    print(f"merge_ambiguity_score: {metrics['merge_ambiguity_score']}")
    print(f"source_purity_score: {metrics['source_purity_score']}")
    print(f"contexts_from_other_sources_in_same_host_rows: {metrics['contexts_from_other_sources_in_same_host_rows']}")
    print(f"canonical_rows_from_other_sources: {metrics['canonical_rows_from_other_sources']}")
    print(f"contaminated_canonical_row_count: {metrics['contaminated_canonical_row_count']}")
    print(f"relation_mismatch_row_count: {metrics['relation_mismatch_row_count']}")
    print(f"direct_relation_mismatch_count: {metrics['direct_relation_mismatch_count']}")
    print(f"dominant_relation_share: {metrics['dominant_relation_share']}")
    print(f"direct_host_relation_counts: {json.dumps(metrics['direct_host_relation_counts'], ensure_ascii=False)}")
    print(f"direct_selected_relation_counts: {json.dumps(metrics['direct_selected_relation_counts'], ensure_ascii=False)}")
    print(f"unique_selected_relation_ids_context: {metrics['unique_selected_relation_ids_context']}")
    print(f"unique_selected_relation_ids_visible: {metrics['unique_selected_relation_ids_visible']}")
    print(f"unique_host_relation_ids: {metrics['unique_host_relation_ids']}")
    print(f"unique_candidate_relation_ids_visible: {metrics['unique_candidate_relation_ids_visible']}")
    print(f"novelty_labels: {json.dumps(metrics['novelty_labels'], ensure_ascii=False, sort_keys=True)}")
    print(f"fetch_status_distribution: {json.dumps(metrics['fetch_status_distribution'], ensure_ascii=False, sort_keys=True)}")
    print(f"top_places: {json.dumps(metrics['top_places'], ensure_ascii=False)}")
    print(f"top_cooperative_hints: {json.dumps(metrics['top_cooperative_hints'], ensure_ascii=False)}")
    print(f"top_route_hints: {json.dumps(metrics['top_route_hints'], ensure_ascii=False)}")
    print(f"suspicious_merge_count: {metrics['suspicious_merge_count']}")
    for index, example in enumerate(metrics["suspicious_merges"][: max(0, int(top_examples))], start=1):
        print(
            f"suspicious_merge[{index}]: "
            f"{json.dumps(example, ensure_ascii=False, sort_keys=True)}"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit Phase 3 extractor dedupe/canonical reuse without mutating DB state."
    )
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        help="Repeat to audit explicit extractor sources. Defaults to the Phase 3 audit targets.",
    )
    parser.add_argument(
        "--top-examples",
        type=int,
        default=5,
        help="How many suspicious merge examples to print per source.",
    )
    parser.add_argument(
        "--json-output",
        type=str,
        default=None,
        help="Optional path to write the full audit payload as JSON.",
    )
    args = parser.parse_args(argv)

    dsn = _clean_text(os.environ.get("DB_DSN") or os.environ.get("DATABASE_URL"))
    if not dsn:
        print("Missing DB_DSN/DATABASE_URL in environment.", file=sys.stderr)
        return 2

    rows = _fetch_rows(dsn)
    requested_sources = list(args.sources or DEFAULT_SOURCES)
    resolved_sources = _build_requested_sources(rows, requested_sources)
    reports = [
        _compute_source_metrics(rows, item["resolved"])
        for item in resolved_sources
    ]

    payload = {
        "db_dsn_present": True,
        "row_count": len(rows),
        "requested_sources": resolved_sources,
        "available_sources": _available_sources(rows),
        "reports": reports,
    }

    print(f"route_job_rows_with_extractor_review: {len(rows)}")
    print(f"available_sources: {json.dumps(payload['available_sources'], ensure_ascii=False)}")
    print(f"requested_sources: {json.dumps(resolved_sources, ensure_ascii=False)}")
    for report in reports:
        _print_source_report(report, top_examples=args.top_examples)

    if args.json_output:
        output_path = Path(args.json_output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\njson_output: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
