"""
Province-aware place geography catalog loader.

Loads ``workspace/catalogs/phase3/place_geography_catalog.json`` and exposes
per-province lookups with **safe-fallback** semantics: an unknown province
token returns an **empty** section — NEVER a silent fallback to Sample Region.

Rationale (Camino D, 2026-04-09)
--------------------------------
Before Camino D, ``geography_guardrails.py`` carried three module-level
Sample Region-only dicts (``PLACE_BBOXES``, ``PLACE_ALIASES``,
``PLACE_DISPLAY_NAMES``). When a non-Sample Region route hit any helper that
consulted those dicts, it silently borrowed Sample Region geometries and
produced catastrophic leakage — e.g. ``ALAUSI-01`` corridors starting in
Imbabura because the anchor-hint fuzzy matcher resolved a Sample Region B token
against a Sample Region bbox.

This loader is one half of the fix: the catalog is externalized and
province-namespaced, and helper lookups are threaded through a loader that
refuses to default-to-Sample Region for unknown provinces. The legacy
module-level constants in ``geography_guardrails.py`` are now thin compat
shims populated from ``sample_region`` at import time — byte-identical to the
pre-Camino-D hardcoded values.

Public API
----------
- ``SUPPORTED_PROVINCES`` — canonical 24-province tuple.
- ``DEFAULT_PROVINCE`` — ``"sample_region"`` (used by legacy call sites that
  have not yet been threaded with a province parameter).
- ``get_place_bboxes(province)`` / ``get_place_aliases(province)`` /
  ``get_place_display_names(province)`` — per-province lookups.
- ``is_supported_province(province)`` — validator.
- ``reload_catalog()`` — test hook; forces the JSON to be re-read on next
  access.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_LOG = logging.getLogger(__name__)

BBoxTuple = Tuple[float, float, float, float]

# Canonical 24-province list. Single source of truth for "is this a valid
# province token?". Must match the keyset of ``provinces`` in the JSON
# catalog (PIEZA 6 synthetic test asserts this).
SUPPORTED_PROVINCES: Tuple[str, ...] = (
    "sample_region",
    "sample_region_b",
    "manabi",
    "azuay",
    "los_rios",
    "el_oro",
    "santa_elena",
    "esmeraldas",
    "santo_domingo",
    "imbabura",
    "carchi",
    "cotopaxi",
    "tungurahua",
    "chimborazo",
    "bolivar",
    "canar",
    "loja",
    "zamora_chinchipe",
    "morona_santiago",
    "pastaza",
    "napo",
    "orellana",
    "sucumbios",
    "galapagos",
)

# Default used by legacy call sites that have not yet been updated to pass
# an explicit province. Sample Region is the only province with populated data
# as of 2026-04-09, so this preserves historical behavior byte-identically.
DEFAULT_PROVINCE: str = "sample_region"

# Path resolution: this file lives at
#   datamind_console/phases/phase3_routes/stop_grounding/place_geography_loader.py
# so parents[4] = ML DATAMIND repo root.
_CATALOG_PATH: Path = (
    Path(__file__).resolve().parents[4]
    / "workspace"
    / "catalogs"
    / "phase3"
    / "place_geography_catalog.json"
)

_lock = threading.Lock()
_catalog: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    """Load the catalog JSON (lazy, cached, thread-safe)."""
    global _catalog
    if _catalog is not None:
        return _catalog
    with _lock:
        if _catalog is not None:
            return _catalog
        if not _CATALOG_PATH.exists():
            raise FileNotFoundError(
                f"place_geography_catalog.json not found at {_CATALOG_PATH}. "
                "Camino D PIEZA 1 should have generated this file."
            )
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        schema = data.get("schema")
        if schema != "place_geography_catalog_v1":
            raise ValueError(
                f"Unexpected schema {schema!r} in {_CATALOG_PATH} "
                f"(expected 'place_geography_catalog_v1')"
            )
        provinces = data.get("provinces") or {}
        _LOG.info(
            "[PLACE-GEO] Loaded %s (version=%s, provinces=%d)",
            _CATALOG_PATH.name,
            data.get("version"),
            len(provinces),
        )
        _catalog = data
        return _catalog


def reload_catalog() -> None:
    """Test hook: drop the cache so the next access re-reads the JSON."""
    global _catalog
    with _lock:
        _catalog = None


def catalog_path() -> Path:
    """Return the absolute path to the backing JSON file (diagnostic)."""
    return _CATALOG_PATH


def _normalize_province(province: Optional[str]) -> str:
    if province is None:
        return DEFAULT_PROVINCE
    norm = str(province).strip().lower()
    return norm or DEFAULT_PROVINCE


def is_supported_province(province: Optional[str]) -> bool:
    """True if ``province`` (after normalization) is in ``SUPPORTED_PROVINCES``."""
    if province is None:
        return False
    return _normalize_province(province) in SUPPORTED_PROVINCES


def _province_section(province: Optional[str]) -> Dict[str, Any]:
    """
    Return the raw province section from the catalog.

    Safe-fallback: an unknown or missing province returns an **empty**
    section. This is intentional — it NEVER silently falls back to
    Sample Region, which was the pre-Camino-D footgun that produced geographic
    leakage across provinces.
    """
    cat = _load()
    norm = _normalize_province(province)
    provinces = cat.get("provinces") or {}
    section = provinces.get(norm)
    if section is None:
        _LOG.warning(
            "[PLACE-GEO] Unknown province %r (normalized=%r). Returning EMPTY "
            "section. SAFE-FALLBACK: not loading Sample Region defaults.",
            province,
            norm,
        )
        return {"bboxes": {}, "aliases": {}, "display_names": {}}
    return section


def get_place_bboxes(province: Optional[str] = DEFAULT_PROVINCE) -> Dict[str, BBoxTuple]:
    """
    Return the bbox dict for ``province``.

    Keys are normalized place names; values are ``(south, west, north,
    east)`` tuples. Returns an empty dict for unknown provinces.
    """
    section = _province_section(province)
    raw = section.get("bboxes") or {}
    return {str(k): tuple(v) for k, v in raw.items()}  # type: ignore[misc]


def get_place_aliases(province: Optional[str] = DEFAULT_PROVINCE) -> Dict[str, List[str]]:
    """
    Return the aliases dict for ``province``.

    Keys are normalized place names; values are lists of alias strings.
    """
    section = _province_section(province)
    raw = section.get("aliases") or {}
    return {str(k): list(v) for k, v in raw.items()}


def get_place_display_names(province: Optional[str] = DEFAULT_PROVINCE) -> Dict[str, str]:
    """Return the display-names dict for ``province``."""
    section = _province_section(province)
    raw = section.get("display_names") or {}
    return {str(k): str(v) for k, v in raw.items()}
