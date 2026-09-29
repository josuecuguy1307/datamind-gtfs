"""
Province configuration loader.

Single source of truth: ``workspace/config/supported_provinces.json``.

All consumers in HADES that need to know anything about a province
(display name, Nominatim bias, operator keywords, canton colors,
default jurisdictions, bbox, …) MUST read it through this module.

Design rules for N-province readiness:

* Never hardcode a province name anywhere else.
* Every lookup is parameterised by ``province: str`` and falls back
  to sensible, non-crashing defaults for unknown provinces.
* Adding a new province is a single JSON edit — no code change.
* The module caches the file in-process; call
  :func:`reload_supported_provinces` if you ever need to refresh.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

__all__ = [
    "DEFAULT_PROVINCE",
    "FALLBACK_NOMINATIM_BIAS",
    "DEFAULT_CANTON_COLOR",
    "load_supported_provinces",
    "reload_supported_provinces",
    "list_active_provinces",
    "list_all_provinces",
    "get_province",
    "get_display_name",
    "get_nominatim_bias",
    "get_operational_bbox",
    "get_operator_keywords",
    "get_default_jurisdiction_codes",
    "get_canton_colors",
    "get_canton_color",
    "get_country_info",
    "get_valhalla_port",
    "load_province_namespaced_catalog",
]


def load_province_namespaced_catalog(
    path: os.PathLike,
    province: Optional[str] = None,
) -> Dict[str, Any]:
    """Load a catalog that has been namespaced per-province.

    Supported on-disk formats:

    v2 (namespaced)::

        {
            "version": "2.0",
            "provinces": {
                "<province>": { ... },
                ...
            }
        }

    v1 (flat, legacy)::

        { ... }   # returned as-is for the default province only

    When reading a v2 file with a known province, the entry under
    ``provinces[province]`` is returned. Unknown provinces return an
    empty dict (graceful degradation — never crashes).

    When reading a v1 file, the full dict is returned for the default
    province (Sample Region) and an empty dict for any other province, so
    legacy catalogs continue to work unchanged during migration.
    """
    key = _normalize_key(province)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # v2 format detection.
    if "provinces" in data and isinstance(data["provinces"], dict):
        entry = data["provinces"].get(key)
        if isinstance(entry, dict):
            return entry
        return {}
    # v1 legacy: return full content only for the default province.
    if key == DEFAULT_PROVINCE:
        return data
    return {}


DEFAULT_PROVINCE = "sample_region"
FALLBACK_NOMINATIM_BIAS = "Quito, Sample Region, Ecuador"
DEFAULT_CANTON_COLOR: List[int] = [114, 224, 255, 200]

_CONFIG_ENV_VAR = "HADES_SUPPORTED_PROVINCES_PATH"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO_ROOT / "workspace" / "config" / "supported_provinces.json"

_cache_lock = threading.Lock()
_cache: Optional[Dict[str, Any]] = None


def _config_path() -> Path:
    override = os.environ.get(_CONFIG_ENV_VAR)
    if override:
        return Path(override)
    return _DEFAULT_PATH


def _normalize_key(province: Optional[str]) -> str:
    key = (province or DEFAULT_PROVINCE).strip().lower()
    return key or DEFAULT_PROVINCE


def _read_file() -> Dict[str, Any]:
    path = _config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def load_supported_provinces() -> Dict[str, Any]:
    """Return the parsed ``supported_provinces.json`` (cached)."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = _read_file()
        return _cache


def reload_supported_provinces() -> Dict[str, Any]:
    """Force re-read from disk and return the refreshed dict."""
    global _cache
    with _cache_lock:
        _cache = _read_file()
        return _cache


def list_all_provinces() -> List[str]:
    """Return all province keys defined in the config (any status)."""
    data = load_supported_provinces()
    provinces = (data or {}).get("provinces") or {}
    return sorted(str(k).lower() for k in provinces.keys())


def list_active_provinces() -> List[str]:
    """Return province keys with ``active: true``."""
    data = load_supported_provinces()
    provinces = (data or {}).get("provinces") or {}
    out: List[str] = []
    for key, entry in provinces.items():
        if isinstance(entry, dict) and bool(entry.get("active", False)):
            out.append(str(key).lower())
    return sorted(out)


def get_province(province: Optional[str]) -> Dict[str, Any]:
    """Return the province entry dict, or ``{}`` if unknown.

    Never raises. Unknown provinces return an empty dict so callers
    can degrade gracefully.
    """
    key = _normalize_key(province)
    data = load_supported_provinces()
    provinces = (data or {}).get("provinces") or {}
    entry = provinces.get(key)
    if isinstance(entry, dict):
        return entry
    return {}


def get_display_name(province: Optional[str]) -> str:
    entry = get_province(province)
    name = entry.get("display_name")
    if isinstance(name, str) and name.strip():
        return name
    return _normalize_key(province).title()


def get_nominatim_bias(province: Optional[str]) -> str:
    """Return the Nominatim bias string, falling back to Sample Region."""
    entry = get_province(province)
    bias = entry.get("nominatim_bias")
    if isinstance(bias, str) and bias.strip():
        return bias
    # Fallback to the Sample Region entry, then to the hardcoded literal.
    if _normalize_key(province) != DEFAULT_PROVINCE:
        default_entry = get_province(DEFAULT_PROVINCE)
        bias = default_entry.get("nominatim_bias")
        if isinstance(bias, str) and bias.strip():
            return bias
    return FALLBACK_NOMINATIM_BIAS


def get_operational_bbox(province: Optional[str]) -> Optional[Dict[str, float]]:
    entry = get_province(province)
    bbox = entry.get("operational_bbox")
    if isinstance(bbox, dict) and all(k in bbox for k in ("south", "west", "north", "east")):
        return {k: float(bbox[k]) for k in ("south", "west", "north", "east")}
    return None


def get_operator_keywords(province: Optional[str]) -> Set[str]:
    """Return the set of operator keywords for a province.

    Unknown / empty → empty set (never crashes). Keywords are
    lowercased to make matching case-insensitive at call-sites.
    """
    entry = get_province(province)
    raw = entry.get("operator_keywords") or []
    if not isinstance(raw, list):
        return set()
    return {str(kw).strip().lower() for kw in raw if str(kw).strip()}


def get_default_jurisdiction_codes(province: Optional[str]) -> List[str]:
    entry = get_province(province)
    raw = entry.get("default_jurisdiction_codes") or []
    if isinstance(raw, list):
        return [str(c) for c in raw if str(c).strip()]
    return []


def get_valhalla_port(province: Optional[str]) -> int:
    entry = get_province(province)
    port = entry.get("valhalla_port")
    try:
        return int(port)
    except (TypeError, ValueError):
        return 8003


def get_canton_colors(province: Optional[str]) -> Dict[str, List[int]]:
    """Return the explicitly declared canton colors for a province."""
    entry = get_province(province)
    raw = entry.get("canton_colors") or {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, List[int]] = {}
    for canton, color in raw.items():
        if isinstance(color, (list, tuple)) and len(color) in (3, 4):
            try:
                rgba = [int(c) for c in color]
            except (TypeError, ValueError):
                continue
            if len(rgba) == 3:
                rgba.append(220)
            out[str(canton).lower()] = rgba
    return out


def _deterministic_palette_color(seed: str) -> List[int]:
    """Deterministic RGB(A) color generated from a hash of ``seed``.

    Uses the HSV wheel to keep colors visually distinguishable even
    for neighbours on the same province. Returns RGBA with alpha=200.
    """
    digest = hashlib.sha1(seed.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    saturation = 0.55 + (digest[1] / 255.0) * 0.35  # 0.55–0.90
    value = 0.70 + (digest[2] / 255.0) * 0.25        # 0.70–0.95
    r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
    return [int(round(r * 255)), int(round(g * 255)), int(round(b * 255)), 210]


def get_canton_color(province: Optional[str], canton: Optional[str]) -> List[int]:
    """Return a canton color, auto-generated if not explicitly set.

    Never returns ``None`` and never crashes for unknown provinces or
    cantons — guarantees dashboards remain functional for any
    ``(province, canton)`` pair.
    """
    key_province = _normalize_key(province)
    key_canton = (canton or "").strip().lower()
    if not key_canton or key_canton == "_default":
        # Allow explicit default override in config.
        explicit = get_canton_colors(key_province).get("_default")
        if explicit:
            return explicit
        return list(DEFAULT_CANTON_COLOR)
    explicit_map = get_canton_colors(key_province)
    if key_canton in explicit_map:
        return explicit_map[key_canton]
    # Deterministic fallback keyed by province + canton so the same
    # pair always yields the same color across sessions.
    return _deterministic_palette_color(f"{key_province}/{key_canton}")


def get_country_info() -> Dict[str, Any]:
    data = load_supported_provinces()
    country = (data or {}).get("country") or {}
    if isinstance(country, dict):
        return country
    return {}
