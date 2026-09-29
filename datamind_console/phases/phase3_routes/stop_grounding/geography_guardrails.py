from __future__ import annotations

import json
import logging
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

_LOG = logging.getLogger(__name__)

BBoxTuple = Tuple[float, float, float, float]
LonLat = Tuple[float, float]

BASE_VALLE_BBOX: BBoxTuple = (-0.42, -78.55, -0.19, -78.36)
BASE_VALLE_CORE_BBOX: BBoxTuple = (-0.40, -78.52, -0.26, -78.38)
BASE_CAYAMBE_BBOX: BBoxTuple = (-0.10, -78.25, 0.20, -78.00)
BASE_CAYAMBE_CORE_BBOX: BBoxTuple = (-0.05, -78.20, 0.18, -78.04)

# ---------------------------------------------------------------------------
# Place geography catalog — Camino D (2026-04-09)
#
# The three module-level dicts below (PLACE_BBOXES / PLACE_ALIASES /
# PLACE_DISPLAY_NAMES) used to be hardcoded Sample Region-only literals. They are
# now compat shims populated from workspace/catalogs/phase3/place_geography_catalog.json
# at import time. Values are byte-identical to the pre-Camino-D literals
# (verified in PIEZA 1.3 via canonical-JSON sort-keys diff, len=5802).
#
# New code should NOT read these module globals directly. Instead, call
# place_geography_loader.get_place_bboxes(province=...) / etc., and/or use
# the province= parameter on the helper functions below. The globals exist
# solely to avoid churning ~6 call sites in other phase3 modules that still
# expect module-level Sample Region constants.
# ---------------------------------------------------------------------------

from datamind_console.phases.phase3_routes.stop_grounding.place_geography_loader import (  # noqa: E402
    DEFAULT_PROVINCE,
    get_place_aliases,
    get_place_bboxes,
    get_place_display_names,
)

PLACE_BBOXES: Dict[str, BBoxTuple] = get_place_bboxes(DEFAULT_PROVINCE)
PLACE_ALIASES: Dict[str, List[str]] = get_place_aliases(DEFAULT_PROVINCE)
PLACE_DISPLAY_NAMES: Dict[str, str] = get_place_display_names(DEFAULT_PROVINCE)

# ---------------------------------------------------------------------------
# Territorial keys — loaded from JSON catalog with hardcoded fallback
# ---------------------------------------------------------------------------

_TERRITORIAL_KEYS_PATH = os.path.join(
    os.path.dirname(__file__), "catalogs", "territorial_keys.json",
)

_HARDCODED_VALLE_LOCALITY_KEYS = {
    "redondel del choclo", "sangolqui", "san rafael",
    "av abdon calderon", "av general enriquez", "el triangulo",
    "loreto", "rumiloma", "rumipamba", "tanipamba", "vallecito",
    "la moca", "el carmen", "la libertad", "amaguana", "conocoto",
    "pintag", "la merced", "san fernando", "san vicente",
    "inchalillo", "curipungo", "los tubos", "san antonio",
    "la armenia", "guangopolo", "autopista general ruminahui",
}

_HARDCODED_CAYAMBE_LOCALITY_KEYS = {
    "cayambe", "ayora", "olmedo", "pesillo", "cangahua",
    "cusubamba", "santa rosa de cuzubamba", "juan montalvo cayambe",
    "panamericana norte e35 cayambe", "via ayora olmedo",
}

_HARDCODED_GENERIC_GEOGRAPHY_HINTS = {
    "quito", "valle de los chillos", "sur de quito", "valle",
}


def _load_territorial_catalog() -> Dict[str, Any]:
    """Load territorial_keys.json if available."""
    if os.path.exists(_TERRITORIAL_KEYS_PATH):
        try:
            with open(_TERRITORIAL_KEYS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            _LOG.debug("Loaded territorial catalog from %s", _TERRITORIAL_KEYS_PATH)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            _LOG.warning("Failed to load territorial catalog: %s", exc)
    return {}


_TERRITORIAL_CATALOG = _load_territorial_catalog()

VALLE_LOCALITY_KEYS: set = (
    set(_TERRITORIAL_CATALOG["valle_locality_keys"])
    if "valle_locality_keys" in _TERRITORIAL_CATALOG
    else _HARDCODED_VALLE_LOCALITY_KEYS
)

CAYAMBE_LOCALITY_KEYS: set = (
    set(_TERRITORIAL_CATALOG["cayambe_locality_keys"])
    if "cayambe_locality_keys" in _TERRITORIAL_CATALOG
    else _HARDCODED_CAYAMBE_LOCALITY_KEYS
)

GENERIC_GEOGRAPHY_HINTS: set = (
    set(_TERRITORIAL_CATALOG["generic_geography_hints"])
    if "generic_geography_hints" in _TERRITORIAL_CATALOG
    else _HARDCODED_GENERIC_GEOGRAPHY_HINTS
)

# Jurisdiction lookup: territorial_key → jurisdiction code
_KEY_TO_JURISDICTION: Dict[str, str] = {}
_JURISDICTION_BBOXES: Dict[str, Tuple[float, float, float, float]] = {}
_JURISDICTION_META: Dict[str, Dict[str, Any]] = {}

def _normalize_bbox(raw: Any) -> Optional[Tuple[float, float, float, float]]:
    """Return (south, west, north, east) tuple from either list or dict form."""
    if isinstance(raw, dict):
        try:
            return (
                float(raw["south"]),
                float(raw["west"]),
                float(raw["north"]),
                float(raw["east"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        try:
            return (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))
        except (TypeError, ValueError):
            return None
    return None


for _jcode, _jdata in (_TERRITORIAL_CATALOG.get("jurisdictions") or {}).items():
    _JURISDICTION_META[_jcode] = {
        "label": _jdata.get("label", ""),
        "authority": _jdata.get("authority", ""),
    }
    _bbox_tuple = _normalize_bbox(_jdata.get("bbox"))
    if _bbox_tuple is not None:
        _JURISDICTION_BBOXES[_jcode] = _bbox_tuple
    for _tk in _jdata.get("territorial_keys", []):
        _KEY_TO_JURISDICTION[_tk] = _jcode


def resolve_jurisdiction(
    *,
    territorial_key: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
) -> Optional[str]:
    """Resolve a territorial key or coordinate to a jurisdiction code.

    Resolution order:
    1. Exact match on territorial_key → jurisdiction
    2. Point-in-bbox fallback (if lat/lon provided)
    3. Default from ``resolution_rules.default_jurisdiction`` in the
       catalog, or ``None`` (province-agnostic — downstream must
       handle unknown).
    """
    if territorial_key:
        jcode = _KEY_TO_JURISDICTION.get(territorial_key)
        if jcode:
            return jcode

    if lat is not None and lon is not None:
        # Check smallest bboxes first so specific jurisdictions win over broader ones
        sorted_j = sorted(
            _JURISDICTION_BBOXES.items(),
            key=lambda item: (item[1][2] - item[1][0]) * (item[1][3] - item[1][1]),
        )
        for jcode, bbox in sorted_j:
            south, west, north, east = bbox
            if south <= lat <= north and west <= lon <= east:
                return jcode

    default = (_TERRITORIAL_CATALOG.get("resolution_rules") or {}).get(
        "default_jurisdiction"
    )
    if isinstance(default, str) and default.strip():
        return default
    return None


def get_jurisdiction_meta(jurisdiction_code: str) -> Dict[str, str]:
    """Return label and authority for a jurisdiction code."""
    return _JURISDICTION_META.get(jurisdiction_code, {"label": "", "authority": ""})

HINT_TOKEN_STOPWORDS = {
    "a",
    "al",
    "autobus",
    "autopista",
    "av",
    "avenida",
    "bus",
    "corredor",
    "de",
    "del",
    "desde",
    "el",
    "estacion",
    "hacia",
    "la",
    "las",
    "los",
    "parada",
    "playon",
    "por",
    "puente",
    "ruta",
    "san",
    "sur",
    "terminal",
    "u",
    "via",
    "y",
}


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9\\s/]", " ", value)
    return re.sub(r"\\s+", " ", value).strip()


def _normalized_tokens(text: str) -> List[str]:
    return [
        token
        for token in normalize_text(text).replace("/", " ").split()
        if token and token not in HINT_TOKEN_STOPWORDS
    ]


def _place_sort_key(item: Tuple[str, List[str]]) -> Tuple[int, int]:
    key, aliases = item
    longest_alias = max((len(normalize_text(alias)) for alias in aliases), default=len(key))
    return (longest_alias, len(key))


def _alias_match(text: str, alias: str) -> bool:
    norm_text = f" {normalize_text(text)} "
    norm_alias = f" {normalize_text(alias)} "
    return norm_alias in norm_text


def _bbox_to_dict(bbox: BBoxTuple) -> Dict[str, float]:
    south, west, north, east = bbox
    return {
        "south": round(float(south), 6),
        "west": round(float(west), 6),
        "north": round(float(north), 6),
        "east": round(float(east), 6),
    }


def _dict_to_bbox(bbox: Dict[str, Any]) -> BBoxTuple:
    return (
        float(bbox["south"]),
        float(bbox["west"]),
        float(bbox["north"]),
        float(bbox["east"]),
    )


def bbox_center(bbox: BBoxTuple) -> LonLat:
    south, west, north, east = bbox
    return ((west + east) / 2.0, (south + north) / 2.0)


def merge_bboxes(*bboxes: Optional[BBoxTuple]) -> BBoxTuple:
    usable = [bbox for bbox in bboxes if bbox is not None]
    if not usable:
        return BASE_VALLE_BBOX
    south = min(b[0] for b in usable)
    west = min(b[1] for b in usable)
    north = max(b[2] for b in usable)
    east = max(b[3] for b in usable)
    return (south, west, north, east)


def expand_bbox(bbox: BBoxTuple, pct: float) -> BBoxTuple:
    south, west, north, east = bbox
    lat_span = north - south
    lon_span = east - west
    lat_pad = max(0.004, lat_span * pct / 2.0)
    lon_pad = max(0.004, lon_span * pct / 2.0)
    return (south - lat_pad, west - lon_pad, north + lat_pad, east + lon_pad)


def _resolve_place_dicts(
    province: Optional[str],
) -> Tuple[Dict[str, BBoxTuple], Dict[str, List[str]], Dict[str, str]]:
    """
    Return the (bboxes, aliases, display_names) triple for ``province``.

    When ``province`` is None or resolves to the default (Sample Region), returns
    the module-level compat shims so legacy behavior is byte-identical. Any
    other province is routed through the loader; unknown provinces get
    empty dicts (SAFE-FALLBACK — never silently borrow Sample Region data).
    """
    if province is None or str(province).strip().lower() in ("", DEFAULT_PROVINCE):
        return PLACE_BBOXES, PLACE_ALIASES, PLACE_DISPLAY_NAMES
    return (
        get_place_bboxes(province),
        get_place_aliases(province),
        get_place_display_names(province),
    )


def infer_locality_keys(
    texts: Iterable[str],
    *,
    province: Optional[str] = None,
) -> List[str]:
    _bboxes, aliases_dict, _display = _resolve_place_dicts(province)
    found: List[str] = []
    seen = set()
    normalized_texts = [normalize_text(text) for text in texts if str(text or "").strip()]
    place_items = sorted(aliases_dict.items(), key=_place_sort_key, reverse=True)
    for key, aliases in place_items:
        for text in normalized_texts:
            if any(_alias_match(text, alias) for alias in aliases):
                if key not in seen:
                    found.append(key)
                    seen.add(key)
                break
    return found


def place_center(
    key: str,
    *,
    province: Optional[str] = None,
) -> Optional[LonLat]:
    bboxes, _aliases, _display = _resolve_place_dicts(province)
    bbox = bboxes.get(key)
    if bbox is None:
        return None
    return bbox_center(bbox)


def derive_hint_geography_context(
    hint_text: str,
    *,
    envelope: Optional[Dict[str, Any]] = None,
    province: Optional[str] = None,
) -> Dict[str, Any]:
    bboxes, _aliases, display_dict = _resolve_place_dicts(province)
    hint_keys = infer_locality_keys([hint_text], province=province)
    expected_keys = set(envelope.get("expected_locality_keys") or []) if envelope else set()
    allowed_connectors = set(envelope.get("allowed_outward_connectors") or []) if envelope else set()
    ranked_keys = list(dict.fromkeys(hint_keys))
    ranked_keys.sort(
        key=lambda key: (
            1 if key in expected_keys else 0,
            1 if key in allowed_connectors else 0,
            len(normalize_text(display_dict.get(key, key))),
        ),
        reverse=True,
    )

    bbox_value: Optional[BBoxTuple] = None
    if ranked_keys:
        boxes = [bboxes[key] for key in ranked_keys if key in bboxes]
        if boxes:
            bbox_value = expand_bbox(merge_bboxes(*boxes), 0.04)

    proxy_key = ranked_keys[0] if ranked_keys else None
    proxy_center = place_center(proxy_key, province=province) if proxy_key else None
    return {
        "hint_locality_keys": ranked_keys,
        "hint_bbox": _bbox_to_dict(bbox_value) if bbox_value else None,
        "proxy_key": proxy_key,
        "proxy_name": display_dict.get(proxy_key or "", hint_text.strip()) if proxy_key else hint_text.strip(),
        "proxy_lon": round(float(proxy_center[0]), 6) if proxy_center else None,
        "proxy_lat": round(float(proxy_center[1]), 6) if proxy_center else None,
    }


def grounding_text_alignment(
    *,
    hint_text: str,
    stop_name: str,
    locality: str,
    ref: Optional[str] = None,
    province: Optional[str] = None,
) -> Dict[str, Any]:
    _bboxes, aliases_dict, _display = _resolve_place_dicts(province)
    hint_tokens = set(_normalized_tokens(hint_text))
    candidate_tokens = set(_normalized_tokens(f"{stop_name} {locality} {ref or ''}"))

    hint_keys = infer_locality_keys([hint_text], province=province)
    candidate_keys = infer_locality_keys([stop_name, locality, ref or ""], province=province)
    locality_key_overlap = len(set(hint_keys) & set(candidate_keys))

    token_overlap = 0.0
    if hint_tokens:
        token_overlap = len(hint_tokens & candidate_tokens) / max(len(hint_tokens), 1)

    alias_exact = False
    for key in hint_keys:
        aliases = aliases_dict.get(key, [key])
        if any(
            _alias_match(stop_name, alias) or _alias_match(locality, alias) or _alias_match(ref or "", alias)
            for alias in aliases
        ):
            alias_exact = True
            break

    score = 0.55 * token_overlap
    if locality_key_overlap:
        score += 0.35
    if alias_exact:
        score += 0.20

    hard_mismatch = bool(
        hint_tokens
        and token_overlap == 0.0
        and locality_key_overlap == 0
        and not alias_exact
        and any(len(token) >= 4 for token in hint_tokens)
    )

    return {
        "text_alignment_score": round(max(0.0, min(1.0, score)), 4),
        "hint_locality_keys": hint_keys,
        "candidate_locality_keys": candidate_keys,
        "hard_token_mismatch": hard_mismatch,
        "alias_exact": alias_exact,
        "token_overlap": round(token_overlap, 4),
    }


def is_generic_geography_hint(text: str) -> bool:
    norm = normalize_text(text)
    return norm in GENERIC_GEOGRAPHY_HINTS


def _is_specific_endpoint(text: str) -> bool:
    norm = normalize_text(text)
    if not norm:
        return False
    if is_generic_geography_hint(norm):
        return False
    return bool(infer_locality_keys([norm])) or "/" not in norm


def _first_specific(values: Sequence[str], *, from_end: bool = False) -> Optional[str]:
    iterable = list(reversed(values)) if from_end else list(values)
    for value in iterable:
        if _is_specific_endpoint(value):
            return str(value)
    return str(iterable[0]) if iterable else None


def normalize_seed_hints(
    *,
    route_name: str,
    anchor_a_hint: str,
    anchor_b_hint: str,
    intermediate_hints: Sequence[str],
    sequence_seed_fragments: Sequence[str],
) -> Dict[str, Any]:
    original_a = str(anchor_a_hint or "").strip()
    original_b = str(anchor_b_hint or "").strip()
    intermediates = [str(item).strip() for item in sequence_seed_fragments if str(item).strip()]
    notes: List[str] = []

    normalized_a = original_a
    normalized_b = original_b

    start_specific = _first_specific(intermediates, from_end=False)
    end_specific = _first_specific(intermediates, from_end=True)
    tail_specific = _first_specific(intermediate_hints, from_end=True)
    head_specific = _first_specific(intermediate_hints, from_end=False)

    if not normalized_a or is_generic_geography_hint(normalized_a):
        fallback = start_specific or head_specific
        if fallback and normalize_text(fallback) != normalize_text(normalized_a):
            normalized_a = fallback
            notes.append(f"anchor_a_normalized:{original_a or '-'}->{fallback}")

    if not normalized_b or is_generic_geography_hint(normalized_b):
        fallback = end_specific or tail_specific
        if fallback and normalize_text(fallback) != normalize_text(normalized_b):
            normalized_b = fallback
            notes.append(f"anchor_b_normalized:{original_b or '-'}->{fallback}")

    if normalize_text(route_name).startswith("quito ") and start_specific and normalize_text(normalized_a) != normalize_text(start_specific):
        normalized_a = start_specific
        notes.append(f"route_start_normalized:{original_a or '-'}->{start_specific}")

    return {
        "anchor_a_hint": normalized_a,
        "anchor_b_hint": normalized_b,
        "raw_anchor_a_hint": original_a,
        "raw_anchor_b_hint": original_b,
        "normalization_notes": notes,
    }


_ECUADOR_CONTINENTAL_BBOX: BBoxTuple = (-5.0, -81.5, 1.5, -75.0)  # (south, west, north, east) — continental only, NO Galápagos


def _build_generic_envelope(
    *,
    province: str,
    sector_key: Optional[str],
    reason: str,
) -> Dict[str, Any]:
    """
    Path 3 generic envelope: permissive continental-Ecuador bbox for provinces
    without a unit catalog. Disables strict bbox enforcement so interprovincial
    corridors are not blocked by a Sample Region-shaped guardrail. The per-route
    `must_NOT_enter` geography catalog remains the authoritative forbidden-zone
    check (separate code path, unchanged).
    """
    south, west, north, east = _ECUADOR_CONTINENTAL_BBOX
    bbox_dict = {"south": south, "west": west, "north": north, "east": east}
    return {
        "schema_version": "geography_envelope_v1",
        "family": "generic",
        "route_family_type": "interprovincial_generic",
        "bbox": bbox_dict,
        "core_bbox": dict(bbox_dict),
        "anchor_bbox": dict(bbox_dict),
        "anchor_locality_keys": [],
        "expected_localities": [],
        "expected_locality_keys": [],
        "allowed_outward_connectors": [],
        "strict_bbox_enforcement": False,
        "absolute_max_corridor_km": 600.0,
        "max_inflation_ratio": 5.0,
        "min_in_bounds_fraction": 0.0,
        "normalized_sector_key": sector_key or f"{province}_generic",
        "notes": [
            f"Generic envelope for province={province} ({reason}). "
            f"Strict bbox enforcement disabled; per-route must_NOT_enter areas from the "
            f"geography catalog remain authoritative.",
        ],
    }


def _try_path2_unit_catalog(
    *,
    unit_name: str,
    route_name: str,
) -> Optional[Dict[str, Any]]:
    """
    Path 2 unit-catalog lookup.
    Loads `constructor_artifacts/<unit_name>_route_geography_catalog.json` and
    returns the route's envelope if a matching entry exists. Returns None on any
    miss (missing file, missing route, parse error) — the caller falls through
    to Path 3 with a warning.
    """
    try:
        repo_root = Path(__file__).resolve().parents[4]
    except Exception:
        return None
    catalog_path = repo_root / "constructor_artifacts" / f"{unit_name}_route_geography_catalog.json"
    if not catalog_path.exists():
        return None
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            catalog = json.load(f)
    except Exception as exc:
        _LOG.warning("Path 2: failed to read %s: %s", catalog_path, exc)
        return None
    route_geos = catalog.get("route_geographies") or []
    entry = next(
        (r for r in route_geos if (r.get("route_name") or "") == route_name),
        None,
    )
    if entry is None:
        return None
    # Construct a permissive envelope anchored to the unit's required areas if any.
    # Today the unit catalog describes must_pass_through / must_NOT_enter via area
    # keys, not a single bbox, so we still disable strict bbox enforcement and
    # defer validation to the geographic_validator. This is intentional: Path 2
    # is a hook point, not a fully-baked second envelope source.
    province_hint = str(catalog.get("province") or "").strip().lower() or "unknown"
    south, west, north, east = _ECUADOR_CONTINENTAL_BBOX
    bbox_dict = {"south": south, "west": west, "north": north, "east": east}
    return {
        "schema_version": "geography_envelope_v1",
        "family": "unit_catalog",
        "route_family_type": "unit_catalog_route",
        "bbox": bbox_dict,
        "core_bbox": dict(bbox_dict),
        "anchor_bbox": dict(bbox_dict),
        "anchor_locality_keys": [],
        "expected_localities": list(entry.get("must_pass_through_areas_ordered") or []),
        "expected_locality_keys": list(entry.get("must_pass_through_areas_ordered") or []),
        "allowed_outward_connectors": list(entry.get("expected_arterials") or []),
        "strict_bbox_enforcement": False,
        "absolute_max_corridor_km": 600.0,
        "max_inflation_ratio": 5.0,
        "min_in_bounds_fraction": 0.0,
        "normalized_sector_key": f"{province_hint}_unit_{unit_name}",
        "notes": [
            f"Unit catalog envelope from {catalog_path.name} for province={province_hint}. "
            f"Strict bbox disabled; must_pass_through + must_NOT_enter enforced downstream "
            f"by geographic_validator.",
        ],
    }


def derive_expected_geographic_envelope(
    *,
    route_name: str,
    operator_name: Optional[str] = None,
    corridor_description: Optional[str] = None,
    anchor_a_hint: str,
    anchor_b_hint: str,
    intermediate_hints: Sequence[str],
    locality_hints: Sequence[str],
    sequence_seed_fragments: Sequence[str],
    source_notes: Any = None,
    sector_key: Optional[str] = None,
    province: Optional[str] = None,
    unit_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    3-Path dispatch:

    Path 1 (Sample Region legacy): `province is None or province.lower() == "sample_region"`.
        Executes the legacy body byte-identical to preserve retro-compat with
        all valle_de_los_chillos / Quito-centro / rural cantons guardrails.
        Sample Region never falls through to Path 2/3, even if unit_name is passed.

    Path 2 (Unit catalog lookup): any non-Sample Region province AND unit_name provided
        AND `constructor_artifacts/<unit_name>_route_geography_catalog.json` exists
        AND the route is present. Returns a permissive envelope that delegates
        strict validation to geographic_validator. Misses fall through to Path 3
        with a warning.

    Path 3 (Generic continental fallback): any other case. Returns a permissive
        continental-Ecuador envelope with strict bbox disabled. The per-route
        must_NOT_enter check remains authoritative via geographic_validator.
    """
    province_norm = (province or "").strip().lower() or "sample_region"

    if province_norm == "sample_region":
        return _derive_envelope_sample_region_legacy(
            route_name=route_name,
            operator_name=operator_name,
            corridor_description=corridor_description,
            anchor_a_hint=anchor_a_hint,
            anchor_b_hint=anchor_b_hint,
            intermediate_hints=intermediate_hints,
            locality_hints=locality_hints,
            sequence_seed_fragments=sequence_seed_fragments,
            source_notes=source_notes,
            sector_key=sector_key,
        )

    if unit_name:
        path2 = _try_path2_unit_catalog(unit_name=unit_name, route_name=route_name)
        if path2 is not None:
            return path2
        _LOG.warning(
            "Path 2 miss for province=%s unit=%s route=%r — falling back to Path 3 generic",
            province_norm, unit_name, route_name,
        )

    return _build_generic_envelope(
        province=province_norm,
        sector_key=sector_key,
        reason="no unit_name provided" if not unit_name else f"unit_name={unit_name} catalog miss",
    )


def _derive_envelope_sample_region_legacy(
    *,
    route_name: str,
    operator_name: Optional[str] = None,
    corridor_description: Optional[str] = None,
    anchor_a_hint: str,
    anchor_b_hint: str,
    intermediate_hints: Sequence[str],
    locality_hints: Sequence[str],
    sequence_seed_fragments: Sequence[str],
    source_notes: Any = None,
    sector_key: Optional[str] = None,
) -> Dict[str, Any]:
    anchor_locality_keys = infer_locality_keys(
        [
            anchor_a_hint,
            anchor_b_hint,
            *(list(sequence_seed_fragments[:1]) if sequence_seed_fragments else []),
            *(list(sequence_seed_fragments[-1:]) if sequence_seed_fragments else []),
        ]
    )
    source_texts: List[str] = [
        route_name,
        operator_name or "",
        corridor_description or "",
        anchor_a_hint,
        anchor_b_hint,
        *list(intermediate_hints or []),
        *list(locality_hints or []),
        *list(sequence_seed_fragments or []),
    ]
    if isinstance(source_notes, str):
        source_texts.append(source_notes)
    elif isinstance(source_notes, dict):
        for value in source_notes.values():
            if isinstance(value, str):
                source_texts.append(value)
            elif isinstance(value, list):
                source_texts.extend(str(item) for item in value)
    elif isinstance(source_notes, list):
        source_texts.extend(str(item) for item in source_notes)

    locality_keys = infer_locality_keys(source_texts)
    valle_keys = [key for key in locality_keys if key in VALLE_LOCALITY_KEYS]
    cayambe_keys = [key for key in locality_keys if key in CAYAMBE_LOCALITY_KEYS]

    norm_bundle = " ".join(normalize_text(text) for text in source_texts if str(text or "").strip())

    # Cayambe family branch — checked first so cayambe-region routes don't
    # fall through to the valle defaulting below. A route counts as cayambe
    # when its locality keys overlap CAYAMBE_LOCALITY_KEYS or its source
    # bundle names a cayambe-canton place.
    is_cayambe = bool(cayambe_keys) or any(
        term in norm_bundle for term in ("cayambe", "olmedo", "pesillo", "ayora", "cangahua")
    )

    if is_cayambe:
        family = "cayambe_local"
        connectors: List[str] = []
        # Connector for cayambe routes that bridge to neighbouring jurisdictions
        if any(term in norm_bundle for term in ("otavalo", "ibarra", "imbabura")):
            family = "cayambe_imbabura_connector"
            connectors.extend(["otavalo"])
        elif any(term in norm_bundle for term in ("tabacundo", "pedro moncayo")):
            family = "cayambe_pedro_moncayo_connector"
            connectors.extend(["tabacundo"])
        elif any(term in norm_bundle for term in ("quito", "la ofelia", "carcelen")):
            family = "cayambe_quito_connector"
            connectors.extend(["panamericana norte e35 cayambe"])

        if not cayambe_keys:
            cayambe_keys = ["cayambe"]

        anchor_boxes = [PLACE_BBOXES[key] for key in anchor_locality_keys if key in PLACE_BBOXES]
        core_boxes = [PLACE_BBOXES[key] for key in cayambe_keys if key in PLACE_BBOXES]
        if not core_boxes:
            core_boxes = [BASE_CAYAMBE_CORE_BBOX]
        core_bbox = merge_bboxes(*core_boxes)
        anchor_bbox = merge_bboxes(*anchor_boxes) if anchor_boxes else core_bbox

        connector_boxes = [PLACE_BBOXES[key] for key in connectors if key in PLACE_BBOXES]
        final_bbox = merge_bboxes(core_bbox, *connector_boxes)
        final_bbox = expand_bbox(final_bbox, 0.10)

        expected_localities = list(dict.fromkeys(
            cayambe_keys + [key for key in connectors if key in PLACE_BBOXES]
        ))
        absolute_max_km = 35.0
        inflation_limit = 3.0
        min_in_bounds_fraction = 0.82
        if family == "cayambe_imbabura_connector":
            absolute_max_km = 60.0
            inflation_limit = 3.4
            min_in_bounds_fraction = 0.74
        elif family == "cayambe_quito_connector":
            absolute_max_km = 80.0
            inflation_limit = 3.6
            min_in_bounds_fraction = 0.70
        elif family == "cayambe_pedro_moncayo_connector":
            absolute_max_km = 45.0
            inflation_limit = 3.2
            min_in_bounds_fraction = 0.78

        notes = ["Cayambe guardrail active: discovery should remain inside the Cayambe canton operating envelope unless the seed names an external connector."]
        if sector_key:
            notes.append(f"sector_key={sector_key}")

        return {
            "schema_version": "geography_envelope_v1",
            "family": "cayambe",
            "route_family_type": family,
            "bbox": _bbox_to_dict(final_bbox),
            "core_bbox": _bbox_to_dict(core_bbox),
            "anchor_bbox": _bbox_to_dict(anchor_bbox),
            "anchor_locality_keys": anchor_locality_keys,
            "expected_localities": expected_localities,
            "expected_locality_keys": expected_localities,
            "allowed_outward_connectors": list(dict.fromkeys(connectors)),
            "strict_bbox_enforcement": True,
            "absolute_max_corridor_km": absolute_max_km,
            "max_inflation_ratio": inflation_limit,
            "min_in_bounds_fraction": min_in_bounds_fraction,
            "normalized_sector_key": sector_key or "cayambe",
            "notes": notes,
        }

    family = "valle_local"
    connectors: List[str] = []

    if any(term in norm_bundle for term in ("quitumbe", "sur de quito")):
        family = "valle_quito_south_connector"
        connectors.extend(["autopista general ruminahui", "el trebol", "terminal quitumbe", "puengasi"])
    elif any(term in norm_bundle for term in ("san roque", "san francisco", "cumanda", "24 de mayo")):
        family = "valle_historic_connector"
        connectors.extend(["la marin", "cumanda", "san francisco", "san roque", "viaducto 24 de mayo"])
    elif any(term in norm_bundle for term in ("quito", "la marin", "marin", "parada de los valles", "el trebol")):
        family = "valle_quito_connector"
        connectors.extend(["autopista general ruminahui", "el trebol", "la marin"])

    if not valle_keys:
        valle_keys = ["conocoto"] if "conocoto" in norm_bundle else ["sangolqui"]

    anchor_boxes = [PLACE_BBOXES[key] for key in anchor_locality_keys if key in PLACE_BBOXES]
    core_boxes = [PLACE_BBOXES[key] for key in valle_keys if key in PLACE_BBOXES]
    if not core_boxes:
        core_boxes = [BASE_VALLE_CORE_BBOX]
    core_bbox = merge_bboxes(*core_boxes)
    anchor_bbox = merge_bboxes(*anchor_boxes) if anchor_boxes else core_bbox

    connector_boxes = [PLACE_BBOXES[key] for key in connectors if key in PLACE_BBOXES]
    final_bbox = merge_bboxes(core_bbox, *connector_boxes)
    final_bbox = expand_bbox(final_bbox, 0.10 if family != "valle_local" else 0.08)

    expected_localities = list(dict.fromkeys(valle_keys + [key for key in connectors if key in PLACE_BBOXES]))
    absolute_max_km = 30.0
    inflation_limit = 3.0
    min_in_bounds_fraction = 0.86
    if family == "valle_quito_connector":
        absolute_max_km = 38.0
        inflation_limit = 3.4
        min_in_bounds_fraction = 0.78
    elif family == "valle_quito_south_connector":
        absolute_max_km = 46.0
        inflation_limit = 3.8
        min_in_bounds_fraction = 0.74
    elif family == "valle_historic_connector":
        absolute_max_km = 40.0
        inflation_limit = 3.5
        min_in_bounds_fraction = 0.76

    notes = [
        "Valle guardrail active: discovery should remain inside the Valle/Ruminihui/Conocoto operating envelope unless the seed explicitly names a Quito connector.",
    ]
    if sector_key:
        notes.append(f"sector_key={sector_key}")

    return {
        "schema_version": "geography_envelope_v1",
        "family": "valle_de_los_chillos" if ("valle" in norm_bundle or valle_keys) else "generic",
        "route_family_type": family,
        "bbox": _bbox_to_dict(final_bbox),
        "core_bbox": _bbox_to_dict(core_bbox),
        "anchor_bbox": _bbox_to_dict(anchor_bbox),
        "anchor_locality_keys": anchor_locality_keys,
        "expected_localities": expected_localities,
        "expected_locality_keys": expected_localities,
        "allowed_outward_connectors": list(dict.fromkeys(connectors)),
        "strict_bbox_enforcement": True,
        "absolute_max_corridor_km": absolute_max_km,
        "max_inflation_ratio": inflation_limit,
        "min_in_bounds_fraction": min_in_bounds_fraction,
        "normalized_sector_key": sector_key or "valle_de_los_chillos",
        "notes": notes,
    }


def point_in_bbox(lon: float, lat: float, bbox: Dict[str, Any]) -> bool:
    box = _dict_to_bbox(bbox)
    return box[1] <= lon <= box[3] and box[0] <= lat <= box[2]


def _haversine_km(a: LonLat, b: LonLat) -> float:
    lon1, lat1 = a
    lon2, lat2 = b
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * radius_km * math.asin(math.sqrt(max(0.0, min(1.0, h))))


def distance_to_bbox_m(lon: float, lat: float, bbox: Dict[str, Any]) -> float:
    if point_in_bbox(lon, lat, bbox):
        return 0.0
    box = _dict_to_bbox(bbox)
    clamped_lon = min(max(lon, box[1]), box[3])
    clamped_lat = min(max(lat, box[0]), box[2])
    return _haversine_km((lon, lat), (clamped_lon, clamped_lat)) * 1000.0


def locality_consistency_score(
    *,
    locality: str,
    stop_name: str,
    envelope: Optional[Dict[str, Any]],
    province: Optional[str] = None,
) -> float:
    if not envelope:
        return 0.0
    _bboxes, aliases_dict, _display = _resolve_place_dicts(province)
    haystack = f"{normalize_text(locality)} {normalize_text(stop_name)}"
    expected = list(envelope.get("expected_locality_keys") or [])
    if not expected:
        return 0.0
    for key in expected:
        aliases = aliases_dict.get(key, [key])
        if any(_alias_match(haystack, alias) for alias in aliases):
            return 1.0
    return 0.0


def grounding_geography_score(
    *,
    lon: float,
    lat: float,
    locality: str,
    stop_name: str,
    hint_text: str,
    envelope: Optional[Dict[str, Any]],
    province: Optional[str] = None,
) -> Dict[str, Any]:
    if not envelope:
        return {
            "geography_score": 0.0,
            "in_expected_geography": True,
            "distance_to_expected_bbox_m": 0.0,
            "locality_consistency_score": 0.0,
        }
    _bboxes, aliases_dict, _display = _resolve_place_dicts(province)
    bbox = dict(envelope.get("bbox") or {})
    in_bbox = point_in_bbox(lon, lat, bbox) if bbox else True
    distance_m = distance_to_bbox_m(lon, lat, bbox) if bbox else 0.0
    locality_score = locality_consistency_score(
        locality=locality, stop_name=stop_name, envelope=envelope, province=province,
    )
    hint_locality_keys = infer_locality_keys([hint_text], province=province)
    hint_specificity_bonus = 0.0
    if hint_locality_keys:
        for key in hint_locality_keys:
            aliases = aliases_dict.get(key, [key])
            if any(_alias_match(stop_name, alias) or _alias_match(locality, alias) for alias in aliases):
                hint_specificity_bonus = 0.25
                break

    geography_score = 1.0 if in_bbox else max(0.0, 1.0 - (distance_m / 3500.0))
    geography_score = min(1.0, geography_score * 0.75 + locality_score * 0.15 + hint_specificity_bonus)
    return {
        "geography_score": round(geography_score, 4),
        "in_expected_geography": bool(in_bbox),
        "distance_to_expected_bbox_m": round(distance_m, 2),
        "locality_consistency_score": round(locality_score, 4),
    }


def _polyline_length_km(coords: Sequence[LonLat]) -> float:
    if len(coords) < 2:
        return 0.0
    return sum(_haversine_km(coords[i - 1], coords[i]) for i in range(1, len(coords)))


def evaluate_corridor_geography(
    *,
    corridor_geojson: Optional[Dict[str, Any]],
    waypoints_used: Sequence[Dict[str, Any]],
    expected_envelope: Optional[Dict[str, Any]],
    discovered_stop_count: int = 0,
) -> Dict[str, Any]:
    if not corridor_geojson or not expected_envelope:
        return {
            "route_length_km": 0.0,
            "straight_line_km": 0.0,
            "corridor_inflation_ratio": 0.0,
            "in_bounds_fraction": 0.0,
            "out_of_bounds_reason": "expected_envelope_unavailable",
            "geography_plausibility_score": 0.0,
            "rejected_for_geographic_implausibility": False,
            "route_locality_consistency_notes": ["No geography envelope available."],
        }

    coords = [
        (float(point[0]), float(point[1]))
        for point in list(corridor_geojson.get("coordinates") or [])
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    if len(coords) < 2:
        return {
            "route_length_km": 0.0,
            "straight_line_km": 0.0,
            "corridor_inflation_ratio": 0.0,
            "in_bounds_fraction": 0.0,
            "out_of_bounds_reason": "corridor_missing_coordinates",
            "geography_plausibility_score": 0.0,
            "rejected_for_geographic_implausibility": True,
            "route_locality_consistency_notes": ["Corridor had fewer than two points."],
        }

    bbox = dict(expected_envelope.get("bbox") or {})
    route_length_km = _polyline_length_km(coords)
    endpoint_chord_km = _haversine_km(coords[0], coords[-1])
    # Loop routes (origin ≈ destination) need a different denominator: the
    # endpoint chord is ~0, so inflation ratio against it is meaningless.
    # Use the longest chord between any two points on the corridor as the
    # effective "size" of the loop. For non-loops this equals the endpoint
    # chord, so the calculation is unchanged.
    is_loop = endpoint_chord_km < 1.5 or (
        route_length_km >= 5.0 and endpoint_chord_km / max(route_length_km, 1.0) < 0.15
    )
    if is_loop and len(coords) >= 2:
        max_chord_km = 0.0
        step = max(1, len(coords) // 80)
        sampled = coords[::step]
        for i in range(len(sampled)):
            for j in range(i + 1, len(sampled)):
                d = _haversine_km(sampled[i], sampled[j])
                if d > max_chord_km:
                    max_chord_km = d
        straight_line_km = max_chord_km
    else:
        straight_line_km = endpoint_chord_km
    denominator = max(straight_line_km, 1.0)
    inflation_ratio = route_length_km / denominator

    in_bounds_km = 0.0
    for idx in range(1, len(coords)):
        seg_len = _haversine_km(coords[idx - 1], coords[idx])
        midpoint = ((coords[idx - 1][0] + coords[idx][0]) / 2.0, (coords[idx - 1][1] + coords[idx][1]) / 2.0)
        if point_in_bbox(midpoint[0], midpoint[1], bbox):
            in_bounds_km += seg_len
    in_bounds_fraction = in_bounds_km / max(route_length_km, 0.001)

    absolute_max = float(expected_envelope.get("absolute_max_corridor_km") or 45.0)
    inflation_limit = float(expected_envelope.get("max_inflation_ratio") or 3.5)
    min_in_bounds = float(expected_envelope.get("min_in_bounds_fraction") or 0.75)
    density = float(discovered_stop_count) / max(route_length_km, 1.0)

    score = 1.0
    reasons: List[str] = []
    notes: List[str] = []

    if route_length_km > absolute_max:
        score -= min(0.55, (route_length_km - absolute_max) / max(absolute_max, 1.0))
        reasons.append(f"corridor_length_exceeds_family_limit:{route_length_km:.1f}>{absolute_max:.1f}km")
    if inflation_ratio > inflation_limit:
        score -= min(0.35, (inflation_ratio - inflation_limit) / max(inflation_limit, 1.0))
        reasons.append(f"inflation_ratio_too_high:{inflation_ratio:.2f}>{inflation_limit:.2f}")
    if in_bounds_fraction < min_in_bounds:
        score -= min(0.35, (min_in_bounds - in_bounds_fraction) * 1.2)
        reasons.append(f"corridor_outside_expected_geography:{in_bounds_fraction:.2f}<{min_in_bounds:.2f}")
    if straight_line_km <= 15.0 and route_length_km >= max(absolute_max * 1.1, 45.0):
        score -= 0.55
        reasons.append("local_route_corridor_implausibly_long")
    if density >= 18.0 and route_length_km >= max(absolute_max, 35.0):
        score -= 0.20
        reasons.append("stop_density_indicates_corridor_ballooning")

    score = max(0.0, min(1.0, score))
    rejected = bool(
        route_length_km > max(absolute_max * 1.2, 52.0)
        or inflation_ratio > max(inflation_limit * 1.5, 4.8)
        or in_bounds_fraction < max(0.45, min_in_bounds - 0.28)
        or "local_route_corridor_implausibly_long" in reasons
    )

    if rejected:
        notes.append("Geographically implausible corridor rejected for the route family.")
    else:
        notes.append("Corridor remains inside the expected Valle/connector geography.")

    return {
        "route_length_km": round(route_length_km, 3),
        "straight_line_km": round(straight_line_km, 3),
        "corridor_inflation_ratio": round(inflation_ratio, 4),
        "in_bounds_fraction": round(in_bounds_fraction, 4),
        "out_of_bounds_reason": "; ".join(reasons),
        "geography_plausibility_score": round(score, 4),
        "rejected_for_geographic_implausibility": rejected,
        "route_locality_consistency_notes": notes,
        "discovered_stop_density_per_km": round(density, 4),
    }
