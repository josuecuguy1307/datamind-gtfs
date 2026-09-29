from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError, AdvisoryService
from datamind_console.api_chatgpt.services.openai_client import is_real_advisory_mode
from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.area_targeting import match_sector_alias


BBox = Dict[str, float]

# Overconstraint detection thresholds (resolver-level)
_OVERCONSTRAINT_SECTOR_SCORE_FLOOR = 0.80
_OVERCONSTRAINT_AI_CONFIDENCE_FLOOR = 0.60
_GEOCODER_URL = "https://nominatim.openstreetmap.org/search"
_GEOCODER_USER_AGENT = "DataMindPhase3Extractor/1.0"
_GEOCODER_COUNTRY_CODE = "ec"
_GEOCODER_THROTTLE_SECONDS = 1.05
_GEOCODER_MIN_SCORE = 0.35
_ANCHORS_PATH = Path(__file__).with_name("nominatim_group_bias.json")

# Central province registry (N-province ready). Never hardcode a
# province-specific string at this layer — always route through
# :mod:`datamind_core.province_config`.
from datamind_core.province_config import (  # noqa: E402
    DEFAULT_PROVINCE as _DEFAULT_PROVINCE,
    get_nominatim_bias as get_province_nominatim_bias,
    get_operational_bbox as _get_operational_bbox,
    get_province as _get_province_entry,
    load_province_namespaced_catalog as _load_province_catalog,
)


def _province_viewbox(province: Optional[str] = None) -> str:
    """Build the Nominatim viewbox string for a province.

    Priority:
      1. Explicit ``nominatim_viewbox`` string in the province config
         (used for strict retro-compat with historical runs).
      2. Derived from the province's ``operational_bbox``
         (``west,north,east,south`` to match Nominatim's format).
      3. Empty string — Nominatim falls back to its own global default.
    """
    entry = _get_province_entry(province or _DEFAULT_PROVINCE)
    explicit = entry.get("nominatim_viewbox")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    bbox = _get_operational_bbox(province or _DEFAULT_PROVINCE)
    if bbox:
        return f"{bbox['west']},{bbox['north']},{bbox['east']},{bbox['south']}"
    return ""


_GEOCODER_VIEWBOX = _province_viewbox(_DEFAULT_PROVINCE)


def _load_anchors(province: Optional[str] = None):
    data = _load_province_catalog(_ANCHORS_PATH, province or _DEFAULT_PROVINCE)
    if not data:
        # Graceful empty state for new provinces: no default group,
        # no bias, no anchors. Callers must handle empty dicts.
        return ("", {}, {})
    return (
        data.get("default_group_key", ""),
        data.get("group_query_bias", {}),
        {k: tuple(v) for k, v in (data.get("group_anchors", {}) or {}).items()},
    )

_DEFAULT_GROUP_KEY, _GROUP_QUERY_BIAS, _GROUP_ANCHORS = _load_anchors()
_VARIANT_PREFIX_TOKENS = {"terminal", "estacion", "parada", "paradero", "corredor", "ruta"}
_VARIANT_SUFFIX_TOKENS = {"centro", "central"}
_GEOCODER_LAST_REQUEST_TS = 0.0


@dataclass(frozen=True)
class BBoxCatalogRecord:
    bbox_id: str
    label: str
    description: str
    bbox: BBox
    aliases: Tuple[str, ...]


def normalize_geographic_text(text: Any) -> str:
    raw = str(text or "")
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _tokenize(text: Any) -> List[str]:
    return [tok for tok in normalize_geographic_text(text).split(" ") if tok]


def _parse_bbox_text(text: Any) -> Optional[BBox]:
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 4:
        return None
    try:
        south, west, north, east = [float(x) for x in parts]
    except Exception:
        return None
    return _validate_bbox_candidate({"south": south, "west": west, "north": north, "east": east})


def _looks_like_bbox_text(text: Any) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
    if len(nums) == 4:
        return True
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return len(parts) == 4


def _coerce_bbox(raw: Any) -> Optional[BBox]:
    if not isinstance(raw, dict):
        return None
    try:
        candidate = {
            "south": float(raw["south"]),
            "west": float(raw["west"]),
            "north": float(raw["north"]),
            "east": float(raw["east"]),
        }
    except Exception:
        return None
    return _validate_bbox_candidate(candidate)


def _coerce_geocoder_bbox(raw: Any) -> Optional[BBox]:
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        south = float(raw[0])
        north = float(raw[1])
        west = float(raw[2])
        east = float(raw[3])
    except Exception:
        return None
    return _validate_bbox_candidate({"south": south, "west": west, "north": north, "east": east})


def _validate_bbox_candidate(candidate: Optional[BBox]) -> Optional[BBox]:
    if not candidate:
        return None
    try:
        south = float(candidate["south"])
        west = float(candidate["west"])
        north = float(candidate["north"])
        east = float(candidate["east"])
    except Exception:
        return None
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        return None
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        return None
    if south >= north or west >= east:
        return None
    return {"south": south, "west": west, "north": north, "east": east}


def _bbox_center(candidate: Optional[BBox]) -> Optional[Tuple[float, float]]:
    bbox = _coerce_bbox(candidate)
    if not bbox:
        return None
    return (
        (float(bbox["south"]) + float(bbox["north"])) / 2.0,
        (float(bbox["west"]) + float(bbox["east"])) / 2.0,
    )


def _normalized_group_hint(hints: Optional[Dict[str, Any]] = None) -> str:
    payload = dict(hints or {})
    for key in ("phase3_target_group", "target_group", "group_hint", "group"):
        raw = str(payload.get(key) or "").strip()
        if raw:
            normalized = normalize_group_hint_key(raw)
            if normalized:
                return normalized
    return _DEFAULT_GROUP_KEY


def normalize_group_hint_key(raw: Any) -> str:
    norm = normalize_geographic_text(raw)
    if not norm:
        return _DEFAULT_GROUP_KEY
    if norm in _GROUP_QUERY_BIAS:
        return norm

    tokens = set(_tokenize(norm))
    if (
        "valle de los chillos" in norm
        or "ruminahui" in norm
        or tokens.intersection(
            {
                "chillos",
                "sangolqui",
                "rafael",
                "conocoto",
                "amaguana",
                "pintag",
                "cotogchoa",
                "rumipamba",
                "rumiloma",
                "triangulo",
                "fajardo",
                "loreto",
                "tambo",
                "tambillo",
                "vallecito",
                "tanipamba",
            }
        )
    ):
        return "valle de los chillos"
    if tokens.intersection(
        {
            "tumbaco",
            "cumbaya",
            "puembo",
            "yaruqui",
            "checa",
            "quinche",
            "interparish",
            "valley",
        }
    ):
        return "tumbaco cumbaya"
    if (
        "quito sur" in norm
        or tokens.intersection(
        {
            "quitumbe",
            "chillogallo",
            "guamani",
            "ecuatoriana",
            "turubamba",
            "argelia",
            "magdalena",
        }
        )
        or "23 de mayo" in norm
    ):
        return "quito sur"
    if tokens.intersection(
        {
            "quito",
            "marin",
            "quitumbe",
            "carcelen",
            "centro",
            "historico",
            "america",
            "trolebus",
            "ecovia",
            "metrobus",
        }
    ):
        return "quito urbano"
    return norm


def _group_query_bias(group_key: str) -> str:
    return _GROUP_QUERY_BIAS.get(group_key) or _GROUP_QUERY_BIAS[_DEFAULT_GROUP_KEY]


def _group_anchor(group_key: str) -> Tuple[float, float]:
    return _GROUP_ANCHORS.get(group_key) or _GROUP_ANCHORS[_DEFAULT_GROUP_KEY]


def build_geographic_text_variants(text: Any) -> Tuple[str, ...]:
    raw = str(text or "").strip()
    if not raw:
        return tuple()

    variants: List[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            candidate = " ".join(str(v or "").strip() for v in value if str(v or "").strip())
        else:
            candidate = str(value or "").strip()
        normalized = normalize_geographic_text(candidate)
        if normalized and normalized not in seen:
            seen.add(normalized)
            variants.append(candidate)

    tokens = _tokenize(raw)
    _add(raw)

    if len(tokens) >= 2 and tokens[0] in _VARIANT_PREFIX_TOKENS:
        _add(tokens[1:])
    if len(tokens) >= 3 and tokens[:2] == ["terminal", "terrestre"]:
        _add(tokens[2:])
    if len(tokens) >= 4 and tokens[:3] == ["terminal", "terrestre", "de"]:
        _add(tokens[3:])
    if len(tokens) >= 4 and tokens[:3] == ["parada", "terminal", "terrestre"]:
        _add(tokens[1:])
        _add(tokens[3:])
    if len(tokens) >= 5 and tokens[:4] == ["parada", "terminal", "terrestre", "de"]:
        _add(tokens[1:])
        _add(tokens[4:])
    if len(tokens) >= 2 and tokens[-1] in _VARIANT_SUFFIX_TOKENS:
        _add(tokens[:-1])
    if len(tokens) >= 4 and tokens[-2:] == ["de", "quito"]:
        _add(tokens[:-2])
    if len(tokens) >= 3 and tokens[:2] == ["valle", "de"]:
        _add(tokens[2:])
    if len(tokens) >= 2 and tokens[:2] == ["centro", "historico"]:
        _add(["quito", "centro"])
        _add(["centro", "historico"])

    for part in re.split(r"\s*(?:-|/|\(|\))\s*", raw):
        _add(part)

    return tuple(variants)


def _build_geocoder_queries(
    place_text: str,
    *,
    supporting_hints: Optional[Dict[str, Any]] = None,
    province: Optional[str] = None,
) -> List[str]:
    place = str(place_text or "").strip()
    if not place:
        return []
    group_key = _normalized_group_hint(supporting_hints)
    bias = _group_query_bias(group_key)
    province_bias = get_province_nominatim_bias(province)
    base_variants = list(build_geographic_text_variants(place) or [place])
    queries: List[str] = []
    for variant in base_variants:
        queries.extend(
            [
                f"{variant}, {bias}",
                f"{variant}, {province_bias}",
                f"{variant}, Ecuador",
                variant,
            ]
        )
    out: List[str] = []
    seen: set[str] = set()
    for item in queries:
        normalized = normalize_geographic_text(item)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(item)
    return out


@lru_cache(maxsize=256)
def _fetch_nominatim_rows(query: str) -> Tuple[Dict[str, Any], ...]:
    global _GEOCODER_LAST_REQUEST_TS

    payload = {
        "q": str(query or "").strip(),
        "format": "jsonv2",
        "limit": 5,
        "countrycodes": _GEOCODER_COUNTRY_CODE,
        "viewbox": _GEOCODER_VIEWBOX,
        "bounded": 0,
        "dedupe": 1,
    }
    if not payload["q"]:
        return tuple()

    now = time.monotonic()
    wait_for = _GEOCODER_THROTTLE_SECONDS - max(0.0, now - float(_GEOCODER_LAST_REQUEST_TS))
    if wait_for > 0.0:
        time.sleep(wait_for)

    url = f"{_GEOCODER_URL}?{urllib_parse.urlencode(payload)}"
    req = urllib_request.Request(url, headers={"User-Agent": _GEOCODER_USER_AGENT})
    _GEOCODER_LAST_REQUEST_TS = time.monotonic()

    try:
        with urllib_request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib_error.URLError, TimeoutError):
        return tuple()
    except Exception:
        return tuple()

    try:
        data = json.loads(raw)
    except Exception:
        return tuple()
    if not isinstance(data, list):
        return tuple()
    return tuple(dict(item or {}) for item in data if isinstance(item, dict))


def _resolve_place_with_geocoder(
    place_text: str,
    *,
    supporting_hints: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    place = str(place_text or "").strip()
    if not place:
        return None

    group_key = _normalized_group_hint(supporting_hints)
    anchor_lat, anchor_lon = _group_anchor(group_key)
    place_tokens = set(_tokenize(place))
    candidates: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()

    for query in _build_geocoder_queries(place, supporting_hints=supporting_hints):
        for row in _fetch_nominatim_rows(query):
            bbox = _coerce_geocoder_bbox(row.get("boundingbox"))
            if not bbox:
                continue
            key = (
                str(row.get("osm_type") or "").strip(),
                str(row.get("osm_id") or row.get("place_id") or row.get("display_name") or "").strip(),
            )
            if key in seen:
                continue
            seen.add(key)
            label = str(row.get("display_name") or row.get("name") or "").strip()
            label_tokens = set(_tokenize(label))
            token_overlap = (
                float(len(place_tokens.intersection(label_tokens))) / float(max(len(place_tokens), 1))
                if place_tokens
                else 0.0
            )
            center = _bbox_center(bbox)
            if center is None:
                continue
            dist_penalty = abs(float(center[0]) - anchor_lat) + abs(float(center[1]) - anchor_lon)
            importance = 0.0
            try:
                importance = float(row.get("importance") or 0.0)
            except Exception:
                importance = 0.0
            label_norm = normalize_geographic_text(label)
            place_norm = normalize_geographic_text(place)
            exact_bonus = 0.18 if place_norm and place_norm in label_norm else 0.0
            score = (token_overlap * 1.55) + exact_bonus + min(0.22, importance) - (dist_penalty * 1.7)
            confidence = max(
                0.50,
                min(
                    0.88,
                    0.56 + (token_overlap * 0.18) + min(0.08, importance * 0.20) - min(0.10, dist_penalty * 0.30),
                ),
            )
            candidates.append(
                {
                    "bbox": bbox,
                    "display_name": label,
                    "query": query,
                    "score": float(score),
                    "confidence": round(float(confidence), 4),
                    "importance": importance,
                }
            )

    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            float(item.get("confidence") or 0.0),
            float(item.get("importance") or 0.0),
        ),
        reverse=True,
    )
    best = dict(candidates[0] or {})
    if float(best.get("score") or 0.0) < _GEOCODER_MIN_SCORE:
        return None
    return best


def bbox_hash(candidate: Optional[Dict[str, Any]]) -> Optional[str]:
    bbox = _coerce_bbox(candidate)
    if not bbox:
        return None
    payload = {
        "south": round(float(bbox["south"]), 6),
        "west": round(float(bbox["west"]), 6),
        "north": round(float(bbox["north"]), 6),
        "east": round(float(bbox["east"]), 6),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def _default_bbox_catalog_path() -> Path:
    return Path(__file__).resolve().parents[2] / "phase1_nodes" / "catalogs" / "bbox_catalog.json"


@lru_cache(maxsize=2)
def load_bbox_catalog(path: Optional[str] = None) -> List[BBoxCatalogRecord]:
    p = Path(path).expanduser() if path else _default_bbox_catalog_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: List[BBoxCatalogRecord] = []
    for item in list(data.get("bbox_sets") or []):
        if not isinstance(item, dict):
            continue
        bbox = _coerce_bbox(item)
        if not bbox:
            continue
        bbox_id = str(item.get("bbox_id") or "").strip()
        label = str(item.get("label") or bbox_id).strip()
        description = str(item.get("description") or "").strip()
        aliases = {
            normalize_geographic_text(bbox_id),
            normalize_geographic_text(label),
            normalize_geographic_text(description),
        }
        for part in [bbox_id.replace("_", " "), label, description]:
            norm = normalize_geographic_text(part)
            if norm:
                aliases.add(norm)
        aliases = {a for a in aliases if a}
        out.append(
            BBoxCatalogRecord(
                bbox_id=bbox_id,
                label=label,
                description=description,
                bbox=bbox,
                aliases=tuple(sorted(aliases)),
            )
        )
    return out


def _bbox_catalog_matches(query: str) -> List[Dict[str, Any]]:
    norm = normalize_geographic_text(query)
    if not norm:
        return []
    q_tokens = set(_tokenize(norm))
    rows: List[Dict[str, Any]] = []
    for row in load_bbox_catalog():
        exact = norm in row.aliases
        alias_best = 0.0
        for alias in row.aliases:
            alias_tokens = set(alias.split(" "))
            if not alias_tokens:
                continue
            overlap = len(q_tokens.intersection(alias_tokens))
            if overlap <= 0:
                continue
            score = float(overlap) / float(max(len(alias_tokens), 1))
            if score > alias_best:
                alias_best = score
        score = 1.0 if exact else alias_best
        if score <= 0.0:
            continue
        rows.append(
            {
                "kind": "bbox_catalog",
                "score": float(score),
                "bbox_id": row.bbox_id,
                "label": row.label,
                "description": row.description,
                "bbox": dict(row.bbox),
                "area_group_hint": None,
                "sector_hint": None,
                "corridor_hint": None,
            }
        )
    rows.sort(key=lambda item: (-float(item.get("score") or 0.0), str(item.get("label") or "")))
    return rows[:8]


def _sector_catalog_match(query: str, *, route_tokens: Optional[Iterable[str]] = None) -> Optional[Dict[str, Any]]:
    rec = match_sector_alias(query, route_tokens=route_tokens)
    if rec is None:
        return None
    corridor_hint = rec.sector if any(tok in rec.area_group for tok in {"corridor", "axis", "gateway"}) else None
    return {
        "kind": "sector_catalog",
        "score": 0.86,
        "bbox_id": None,
        "label": rec.sector,
        "description": rec.area_group,
        "bbox": dict(rec.bbox_suggestion) if rec.bbox_suggestion else None,
        "area_group_hint": rec.area_group,
        "sector_hint": rec.sector,
        "corridor_hint": corridor_hint,
    }


class SharedGeographyResolver:
    def __init__(
        self,
        *,
        advisory_mode: Optional[str] = None,
        advisory_service: Optional[AdvisoryService] = None,
    ) -> None:
        self.advisory_mode = str(advisory_mode or "").strip() or None
        self.advisory_service = advisory_service
        self.init_error: Optional[str] = None
        if self.advisory_service is None and is_real_advisory_mode(self.advisory_mode):
            try:
                self.advisory_service = AdvisoryService(
                    requested_mode=self.advisory_mode,
                    allow_mock_fallback=False,
                )
            except Exception as exc:
                self.init_error = str(exc)

    def resolve(
        self,
        *,
        phase: str,
        place_input: Optional[str],
        explicit_bbox: Optional[Dict[str, Any]] = None,
        supporting_hints: Optional[Dict[str, Any]] = None,
        allow_ai_assist: bool = True,
    ) -> Dict[str, Any]:
        phase_name = str(phase or "").strip().lower() or "unknown"
        hints = dict(supporting_hints or {})
        place_text = str(place_input or "").strip()
        normalized = normalize_geographic_text(place_text)
        explicit = _coerce_bbox(explicit_bbox)
        parsed_from_text = None if explicit is not None else _parse_bbox_text(place_text)
        route_tokens = list(hints.get("route_tokens") or [])

        has_route_hints = bool(
            hints.get("refs")
            or hints.get("operator")
            or hints.get("name")
            or hints.get("phase3_operator_hint")
            or hints.get("phase3_route_hint")
            or hints.get("phase3_refs_hint")
        )

        out: Dict[str, Any] = {
            "original_geographic_input": place_text or None,
            "normalized_geographic_input": normalized or None,
            "interpreted_place_meaning": None,
            "interpretation_source": None,
            "interpretation_status": "not_requested" if not place_text and explicit is None else "unresolved",
            "interpretation_confidence": None,
            "bbox_candidate_confidence": None,
            "bbox_candidate": None,
            "bbox_validation_status": "missing",
            "fallback_used": False,
            "fallback_reason": None,
            "effective_bbox_fingerprint": None,
            "supporting_hints": dict(hints),
            "phase_applicability": [phase_name],
            "area_group_hint": None,
            "sector_hint": None,
            "corridor_hint": None,
            "advisory_trace": None,
            "geographic_input_type": None,
            "geographic_interpretation_source": None,
            "geographic_interpretation_status": None,
            "geography_priority_enforced": True,
            "route_hints_present": has_route_hints,
            "route_hints_influenced_bbox": False,
            "route_hints_used_as_secondary_signal": False,
            "route_hints_overconstrained_geography": False,
            "route_hint_effect_reason": None,
        }

        if explicit is not None:
            out.update(
                {
                    "interpreted_place_meaning": "explicit bbox override",
                    "interpretation_source": "explicit_bbox",
                    "interpretation_status": "ok",
                    "interpretation_confidence": 0.99,
                    "bbox_candidate_confidence": 0.99,
                    "bbox_candidate": dict(explicit),
                    "bbox_validation_status": "valid",
                    "geographic_input_type": "explicit_bbox",
                    "geographic_interpretation_source": "explicit_bbox",
                    "geographic_interpretation_status": "ok",
                }
            )
            out["effective_bbox_fingerprint"] = bbox_hash(out["bbox_candidate"])
            return out

        if parsed_from_text is not None:
            out.update(
                {
                    "interpreted_place_meaning": "explicit bbox string",
                    "interpretation_source": "explicit_bbox_text",
                    "interpretation_status": "ok",
                    "interpretation_confidence": 0.99,
                    "bbox_candidate_confidence": 0.99,
                    "bbox_candidate": dict(parsed_from_text),
                    "bbox_validation_status": "valid",
                    "geographic_input_type": "explicit_bbox_text",
                    "geographic_interpretation_source": "explicit_bbox_text",
                    "geographic_interpretation_status": "ok",
                }
            )
            out["effective_bbox_fingerprint"] = bbox_hash(out["bbox_candidate"])
            return out

        if place_text and _looks_like_bbox_text(place_text):
            out.update(
                {
                    "interpretation_source": "explicit_bbox_text",
                    "interpretation_status": "invalid_explicit_bbox",
                    "bbox_validation_status": "invalid",
                    "fallback_used": True,
                    "fallback_reason": "invalid_bbox_text",
                    "geographic_input_type": "explicit_bbox_text",
                    "geographic_interpretation_source": "explicit_bbox_text",
                    "geographic_interpretation_status": "invalid_explicit_bbox",
                }
            )
            return out

        place_variants = list(build_geographic_text_variants(place_text) or ([place_text] if place_text else []))
        if place_variants:
            out["supporting_hints"]["place_variants_considered"] = place_variants[:6]

        bbox_candidate_rows: List[Dict[str, Any]] = []
        for idx, variant in enumerate(place_variants or [place_text]):
            if not str(variant or "").strip():
                continue
            for row in _bbox_catalog_matches(variant):
                item = dict(row)
                item["query_variant"] = str(variant).strip()
                if idx > 0 and float(item.get("score") or 0.0) < 1.0:
                    item["score"] = max(0.0, float(item.get("score") or 0.0) - (0.03 * float(idx)))
                bbox_candidate_rows.append(item)
        bbox_matches: List[Dict[str, Any]] = []
        bbox_seen: Dict[str, Dict[str, Any]] = {}
        for row in bbox_candidate_rows:
            key = str(row.get("bbox_id") or row.get("label") or "")
            existing = bbox_seen.get(key)
            if existing is None or float(row.get("score") or 0.0) > float(existing.get("score") or 0.0):
                bbox_seen[key] = row
        bbox_matches = sorted(
            bbox_seen.values(),
            key=lambda item: (-float(item.get("score") or 0.0), str(item.get("label") or "")),
        )[:8]

        sector_candidates: List[Dict[str, Any]] = []
        for idx, variant in enumerate(place_variants or [place_text]):
            if not str(variant or "").strip():
                continue
            rec = _sector_catalog_match(variant, route_tokens=route_tokens)
            if rec is None:
                continue
            item = dict(rec)
            item["query_variant"] = str(variant).strip()
            if idx > 0:
                item["score"] = max(0.55, float(item.get("score") or 0.86) - (0.04 * float(idx)))
            sector_candidates.append(item)
        sector_candidates.sort(
            key=lambda item: (-float(item.get("score") or 0.0), str(item.get("label") or "")),
        )
        sector_match = dict(sector_candidates[0]) if sector_candidates else None

        if bbox_matches and float(bbox_matches[0].get("score") or 0.0) >= 1.0:
            chosen = dict(bbox_matches[0])
            out.update(
                {
                    "interpreted_place_meaning": chosen.get("label"),
                    "interpretation_source": "bbox_catalog",
                    "interpretation_status": "ok",
                    "interpretation_confidence": min(0.95, max(0.65, float(chosen.get("score") or 0.0))),
                    "bbox_candidate_confidence": min(0.95, max(0.65, float(chosen.get("score") or 0.0))),
                    "bbox_candidate": dict(chosen.get("bbox") or {}),
                    "bbox_validation_status": "valid",
                    "geographic_input_type": "place_input",
                    "geographic_interpretation_source": "bbox_catalog",
                    "geographic_interpretation_status": "ok",
                }
            )
            out["effective_bbox_fingerprint"] = bbox_hash(out["bbox_candidate"])
            return out

        if sector_match and sector_match.get("bbox") is not None:
            _route_influenced = bool(route_tokens)
            _sector_score = float(sector_match.get("score") or 0.0)
            _overconstrained = bool(
                _route_influenced
                and _sector_score < _OVERCONSTRAINT_SECTOR_SCORE_FLOOR
            )
            out["route_hints_influenced_bbox"] = _route_influenced
            out["route_hints_used_as_secondary_signal"] = _route_influenced
            out["route_hints_overconstrained_geography"] = _overconstrained
            out["route_hint_effect_reason"] = (
                "route_tokens_overconstrained_sector_match"
                if _overconstrained
                else ("route_tokens_influenced_sector_match" if _route_influenced else None)
            )
            out.update(
                {
                    "interpreted_place_meaning": sector_match.get("label"),
                    "interpretation_source": "sector_catalog",
                    "interpretation_status": "ok",
                    "interpretation_confidence": float(sector_match.get("score") or 0.86),
                    "bbox_candidate_confidence": float(sector_match.get("score") or 0.86),
                    "bbox_candidate": dict(sector_match.get("bbox") or {}),
                    "bbox_validation_status": "valid",
                    "area_group_hint": sector_match.get("area_group_hint"),
                    "sector_hint": sector_match.get("sector_hint"),
                    "corridor_hint": sector_match.get("corridor_hint"),
                    "geographic_input_type": "place_input",
                    "geographic_interpretation_source": "sector_catalog",
                    "geographic_interpretation_status": "ok",
                }
            )
            out["effective_bbox_fingerprint"] = bbox_hash(out["bbox_candidate"])
            return out

        deterministic_candidates: List[Dict[str, Any]] = []
        deterministic_candidates.extend(bbox_matches[:3])
        if sector_match is not None:
            deterministic_candidates.append(sector_match)
        out["supporting_hints"]["deterministic_candidates"] = deterministic_candidates[:4]

        if len(deterministic_candidates) > 1:
            top_score = float(deterministic_candidates[0].get("score") or 0.0)
            tied = [
                c for c in deterministic_candidates
                if abs(float(c.get("score") or 0.0) - top_score) < 1e-9
            ]
            if len(tied) > 1:
                out["interpretation_status"] = "ambiguous"
                out["fallback_reason"] = "ambiguous_place_input"

        geocoder_match = _resolve_place_with_geocoder(place_text, supporting_hints=hints) if place_text else None
        if geocoder_match is not None:
            bbox_candidate = _coerce_bbox(geocoder_match.get("bbox"))
            if bbox_candidate is not None:
                out["supporting_hints"]["geocoder_query_used"] = geocoder_match.get("query")
                out["supporting_hints"]["geocoder_display_name"] = geocoder_match.get("display_name")
                out["supporting_hints"]["geocoder_importance"] = geocoder_match.get("importance")
                out.update(
                    {
                        "interpreted_place_meaning": geocoder_match.get("display_name") or place_text,
                        "interpretation_source": "nominatim_geocoder",
                        "interpretation_status": "ok",
                        "interpretation_confidence": geocoder_match.get("confidence"),
                        "bbox_candidate_confidence": geocoder_match.get("confidence"),
                        "bbox_candidate": dict(bbox_candidate),
                        "bbox_validation_status": "valid",
                        "fallback_used": True,
                        "fallback_reason": "place_input_geocoded",
                        "geographic_input_type": "place_input",
                        "geographic_interpretation_source": "nominatim_geocoder",
                        "geographic_interpretation_status": "ok",
                    }
                )
                out["effective_bbox_fingerprint"] = bbox_hash(out["bbox_candidate"])
                return out

        if (
            allow_ai_assist
            and is_real_advisory_mode(self.advisory_mode)
            and out.get("interpretation_status") in {"unresolved", "ambiguous"}
        ):
            if self.advisory_service is None:
                out["interpretation_source"] = "hades_geography_interpreter"
                out["geographic_interpretation_source"] = "hades_geography_interpreter"
                out["geographic_interpretation_status"] = str(out.get("interpretation_status") or "unresolved")
                out["fallback_reason"] = out.get("fallback_reason") or "geography_interpreter_unavailable"
                out["supporting_hints"]["advisory_error"] = {
                    "error_code": "GEOGRAPHY_ADVISORY_UNAVAILABLE",
                    "error_summary": self.init_error,
                }
                out["advisory_trace"] = {
                    "task": "hades_geography_interpreter",
                    "requested_mode": self.advisory_mode,
                    "configured_model_name": None,
                    "configured_provider_name": None,
                    "schema_name": "hades_geography_interpreter_response.json",
                    "fallback_used": False,
                    "error_code": "GEOGRAPHY_ADVISORY_UNAVAILABLE",
                }
            else:
                ai_result = self._resolve_with_advisory(
                    phase=phase_name,
                    place_input=place_text,
                    normalized_input=normalized,
                    supporting_hints=hints,
                    deterministic_candidates=deterministic_candidates,
                )
                out["advisory_trace"] = dict(ai_result.get("trace") or {})
                payload = dict(ai_result.get("payload") or {})
                if payload:
                    bbox_candidate = _coerce_bbox(payload.get("bbox_candidate"))
                    confidence = payload.get("interpretation_confidence")
                    out.update(
                        {
                            "interpreted_place_meaning": payload.get("interpreted_place_meaning"),
                            "interpretation_source": payload.get("interpretation_source") or "hades_geography_interpreter",
                            "interpretation_status": payload.get("interpretation_status") or out.get("interpretation_status"),
                            "interpretation_confidence": confidence,
                            "bbox_candidate_confidence": confidence,
                            "bbox_candidate": dict(bbox_candidate) if bbox_candidate else None,
                            "bbox_validation_status": "valid" if bbox_candidate else "missing",
                            "area_group_hint": payload.get("area_group_hint"),
                            "sector_hint": payload.get("sector_hint"),
                            "corridor_hint": payload.get("corridor_hint"),
                            "geographic_input_type": "place_input",
                            "geographic_interpretation_source": payload.get("interpretation_source") or "hades_geography_interpreter",
                            "geographic_interpretation_status": payload.get("interpretation_status") or out.get("interpretation_status"),
                        }
                    )
                    if bbox_candidate:
                        out["effective_bbox_fingerprint"] = bbox_hash(out["bbox_candidate"])
                    # AI-assisted interpretation may use route hints as secondary evidence
                    if has_route_hints:
                        out["route_hints_used_as_secondary_signal"] = True
                        _ai_conf = float(confidence) if confidence is not None else None
                        _ai_overconstrained = bool(
                            _ai_conf is not None
                            and _ai_conf < _OVERCONSTRAINT_AI_CONFIDENCE_FLOOR
                        )
                        out["route_hints_overconstrained_geography"] = _ai_overconstrained
                        out["route_hint_effect_reason"] = (
                            "ai_advisory_route_hints_overconstrained_geography"
                            if _ai_overconstrained
                            else "ai_advisory_used_route_hints_as_context"
                        )
                    if out.get("interpretation_status") in {"ambiguous", "unresolved"} and payload.get("fallback_reason"):
                        out["fallback_reason"] = payload.get("fallback_reason")
                elif ai_result.get("error"):
                    out["interpretation_source"] = "hades_geography_interpreter"
                    out["geographic_interpretation_source"] = "hades_geography_interpreter"
                    out["geographic_interpretation_status"] = str(out.get("interpretation_status") or "unresolved")
                    out["fallback_reason"] = out.get("fallback_reason") or "geography_interpreter_unavailable"
                    out["supporting_hints"]["advisory_error"] = dict(ai_result.get("error") or {})

        if not out.get("bbox_candidate"):
            if out.get("interpretation_status") == "ambiguous":
                out["bbox_validation_status"] = "missing"
            elif out.get("interpretation_status") == "unresolved":
                out["fallback_reason"] = out.get("fallback_reason") or "place_input_unresolved"
            if out.get("interpretation_source") is None:
                out["interpretation_source"] = "deterministic_catalogs"
        if out.get("geographic_input_type") is None:
            out["geographic_input_type"] = "place_input" if place_text else None
        if out.get("geographic_interpretation_source") is None:
            out["geographic_interpretation_source"] = out.get("interpretation_source")
        if out.get("geographic_interpretation_status") is None:
            out["geographic_interpretation_status"] = out.get("interpretation_status")
        if out.get("bbox_candidate_confidence") is None:
            out["bbox_candidate_confidence"] = out.get("interpretation_confidence")
        return out

    def _resolve_with_advisory(
        self,
        *,
        phase: str,
        place_input: str,
        normalized_input: str,
        supporting_hints: Dict[str, Any],
        deterministic_candidates: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if self.advisory_service is None:
            return {"payload": None, "trace": {}, "error": {"error_code": "GEOGRAPHY_ADVISORY_UNAVAILABLE"}}
        envelope = {
            "task": "hades_geography_interpreter",
            "snapshot": {
                "phase": phase,
                "original_geographic_input": place_input,
                "normalized_geographic_input": normalized_input,
                "supporting_hints": dict(supporting_hints or {}),
                "deterministic_candidates": [dict(row) for row in list(deterministic_candidates or [])[:4]],
            },
            "operator_context": {},
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
        }
        try:
            detailed = self.advisory_service.run_task_detailed(
                endpoint_task="hades_geography_interpreter",
                envelope=envelope,
            )
        except AdvisoryError as exc:
            return {
                "payload": None,
                "trace": dict(exc.detail or {}),
                "error": dict(exc.detail or {}),
            }
        response = dict(detailed.get("response") or {})
        meta = dict(detailed.get("meta") or {})
        return {
            "payload": response,
            "trace": {
                "task": "hades_geography_interpreter",
                "requested_mode": meta.get("requested_mode"),
                "configured_model_name": meta.get("model"),
                "configured_provider_name": meta.get("source"),
                "schema_name": meta.get("schema_name"),
                "fallback_used": meta.get("fallback_used"),
            },
            "error": None,
        }
