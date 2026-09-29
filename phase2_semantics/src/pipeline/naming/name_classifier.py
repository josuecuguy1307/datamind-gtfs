"""
Block 3.1 — Name classification engine.

Classifies duplicate place names into:
  GARBAGE         — zero semantic value, must be fully replaced
  OVER_APPLIED    — real name applied to too many geographically spread stops
  DIRECTIONAL_PAIR — same name for stops on opposite road sides
  UNIQUE          — legitimately common name, no action needed
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class PlaceRecord:
    place_id: str
    canonical_name: str
    place_type: str
    lat: float
    lon: float


# ─── Geo helpers ──────────────────────────────────────────────────

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def compute_spread_m(places: List[PlaceRecord]) -> float:
    """Max distance between any two places in the group."""
    if len(places) < 2:
        return 0.0
    max_d = 0.0
    for i, a in enumerate(places):
        for b in places[i + 1 :]:
            d = _haversine_m(a.lat, a.lon, b.lat, b.lon)
            if d > max_d:
                max_d = d
    return max_d


def is_tight_cluster(places: List[PlaceRecord], max_radius_m: float) -> bool:
    """Check if all places fit within max_radius_m of the centroid."""
    if len(places) < 2:
        return True
    clat = sum(p.lat for p in places) / len(places)
    clon = sum(p.lon for p in places) / len(places)
    return all(_haversine_m(p.lat, p.lon, clat, clon) <= max_radius_m for p in places)


# ─── Catalog loader ──────────────────────────────────────────────

def load_classification_rules(path: Optional[str] = None) -> Dict[str, Any]:
    if path is None:
        path = str(Path(__file__).resolve().parents[3] / "catalogs" / "name_classification_rules.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── Classifier ───────────────────────────────────────────────────

def strip_route_code(name: str, patterns: List[str]) -> str:
    """Remove route code prefix leaks (e.g. 'Q10 Cotocollao' → 'Cotocollao')."""
    for pat in patterns:
        m = re.match(pat, name, re.IGNORECASE)
        if m:
            stripped = name[m.end() :].strip()
            if stripped:
                return stripped
    return name


def classify_duplicate_name(
    name: str,
    places: List[PlaceRecord],
    catalog: Dict[str, Any],
) -> str:
    """
    Classify a name that appears on multiple places.

    Returns: 'GARBAGE', 'OVER_APPLIED', 'DIRECTIONAL_PAIR', or 'UNIQUE'
    """
    gc = catalog.get("categories", {}).get("GARBAGE", {})
    oa = catalog.get("categories", {}).get("OVER_APPLIED", {})
    dp = catalog.get("categories", {}).get("DIRECTIONAL_PAIR", {})

    # 1. GARBAGE — exact match
    if name in gc.get("exact_matches", []):
        return "GARBAGE"

    # 2. GARBAGE — pattern match
    for pat in gc.get("patterns", []):
        if re.match(pat, name):
            return "GARBAGE"

    # 3. Route code leak → strip and re-classify
    leak_patterns = catalog.get("route_code_leak_patterns", [])
    stripped = strip_route_code(name, leak_patterns)
    if stripped != name:
        # Re-classify the cleaned name
        return classify_duplicate_name(stripped, places, catalog)

    # 4. DIRECTIONAL_PAIR — tight cluster
    max_cluster = dp.get("max_cluster_radius_m", 50)
    max_pair = dp.get("max_pair_size", 4)
    if is_tight_cluster(places, max_cluster):
        if len(places) <= max_pair:
            return "DIRECTIONAL_PAIR"

    # 5. OVER_APPLIED — known names
    if name in oa.get("known_over_applied_names", []):
        return "OVER_APPLIED"

    # 6. OVER_APPLIED — geographic spread heuristic
    min_spread = oa.get("min_geographic_spread_m", 200)
    min_occ = oa.get("min_occurrences", 20)
    if len(places) >= min_occ:
        spread = compute_spread_m(places[:50])  # Sample for perf
        if spread > min_spread:
            return "OVER_APPLIED"

    # 7. Default
    return "UNIQUE"


def classify_all_duplicates(
    name_groups: Dict[str, List[PlaceRecord]],
    catalog: Dict[str, Any],
    threshold: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Classify all name groups above the duplicate threshold.

    Returns: {name: {category, count, sample_place_ids}}
    """
    thresh = threshold or catalog.get("duplicate_threshold", 50)
    results: Dict[str, Dict[str, Any]] = {}

    for name, places in name_groups.items():
        if len(places) < thresh:
            continue
        cat = classify_duplicate_name(name, places, catalog)
        results[name] = {
            "category": cat,
            "count": len(places),
            "sample_place_ids": [p.place_id for p in places[:5]],
        }

    return results
