from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_AREA_GROUPS = {
    "valle_core",
    "conocoto_corridor",
    "amaguana_axis",
    "quito_gateways",
}

_SECTOR_TYPES = {
    "urban_center",
    "corridor_node",
    "terminal_zone",
    "rural_axis",
}


@dataclass(frozen=True)
class SectorRecord:
    area_group: str
    sector: str
    aliases: Tuple[str, ...]
    priority: int
    sector_type: str
    bbox_suggestion: Optional[Dict[str, float]]


def default_sector_catalog_path() -> Path:
    override = str(os.getenv("DATAMIND_P1_SECTOR_CATALOG_JSON") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[6] / "phase1_sector_catalog.json"


def normalize_area_text(text: Any) -> str:
    raw = str(text or "")
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower()
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def tokenize_area_text(text: Any) -> List[str]:
    return [tok for tok in normalize_area_text(text).split(" ") if tok]


def _coerce_bbox(raw: Any) -> Optional[Dict[str, float]]:
    if not isinstance(raw, dict):
        return None
    keys = ("south", "west", "north", "east")
    out: Dict[str, float] = {}
    try:
        for k in keys:
            if raw.get(k) is None:
                return None
            out[k] = float(raw[k])
    except Exception:
        return None
    if out["south"] >= out["north"] or out["west"] >= out["east"]:
        return None
    return out


def _parse_sector(raw: Dict[str, Any]) -> Optional[SectorRecord]:
    area_group = str(raw.get("area_group") or "").strip().lower()
    if area_group not in _AREA_GROUPS:
        return None

    sector = str(raw.get("sector") or raw.get("canonical_name") or "").strip()
    if not sector:
        return None

    priority = int(raw.get("priority") or 99)
    sector_type = str(raw.get("type") or raw.get("sector_type") or "").strip().lower()
    if sector_type not in _SECTOR_TYPES:
        sector_type = "corridor_node"

    aliases: List[str] = []
    for alias in list(raw.get("aliases") or []):
        a = str(alias or "").strip()
        if a:
            aliases.append(a)
    aliases.append(sector)

    dedup: Dict[str, str] = {}
    for a in aliases:
        norm = normalize_area_text(a)
        if norm:
            dedup[norm] = a
    if not dedup:
        return None

    bbox = _coerce_bbox(raw.get("bbox_suggestion"))

    return SectorRecord(
        area_group=area_group,
        sector=sector,
        aliases=tuple(sorted(dedup.keys())),
        priority=priority,
        sector_type=sector_type,
        bbox_suggestion=bbox,
    )


@lru_cache(maxsize=4)
def load_sector_catalog(path: Optional[str] = None) -> List[SectorRecord]:
    p = Path(path).expanduser() if path else default_sector_catalog_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []

    sectors_raw = list(data.get("sectors") or []) if isinstance(data, dict) else []
    out: List[SectorRecord] = []
    for item in sectors_raw:
        if not isinstance(item, dict):
            continue
        parsed = _parse_sector(item)
        if parsed is not None:
            out.append(parsed)

    out.sort(key=lambda x: (x.priority, x.area_group, normalize_area_text(x.sector)))
    return out


def _match_alias_score(query_tokens: set[str], alias_tokens: Sequence[str]) -> float:
    if not query_tokens or not alias_tokens:
        return 0.0
    a_tokens = set(alias_tokens)
    overlap = len(query_tokens.intersection(a_tokens))
    if overlap == 0:
        return 0.0
    return float(overlap) / float(max(len(a_tokens), 1))


def match_sector_alias(
    text: Any,
    *,
    route_tokens: Optional[Iterable[str]] = None,
    catalog_path: Optional[str] = None,
) -> Optional[SectorRecord]:
    sectors = load_sector_catalog(catalog_path)
    if not sectors:
        return None

    text_norm = normalize_area_text(text)
    route_norm_tokens = [normalize_area_text(t) for t in (route_tokens or []) if normalize_area_text(t)]

    full_text = " ".join([x for x in [text_norm, *route_norm_tokens] if x]).strip()
    if not full_text:
        return None

    # 1) Exact alias hit.
    for s in sectors:
        if full_text in s.aliases:
            return s
        if text_norm and text_norm in s.aliases:
            return s

    # 2) Token overlap match.
    query_tokens = set(tokenize_area_text(full_text))
    if not query_tokens:
        return None

    best: Optional[SectorRecord] = None
    best_score = 0.0
    best_tie = 0

    for s in sectors:
        sector_best = 0.0
        for alias in s.aliases:
            score = _match_alias_score(query_tokens, alias.split(" "))
            if score > sector_best:
                sector_best = score
        if sector_best <= 0.0:
            continue
        tie = -int(s.priority)
        if (sector_best > best_score) or (abs(sector_best - best_score) < 1e-9 and tie > best_tie):
            best = s
            best_score = sector_best
            best_tie = tie

    if best_score < 0.5:
        return None
    return best


def sector_catalog_as_rows(catalog_path: Optional[str] = None) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for s in load_sector_catalog(catalog_path):
        rows.append(
            {
                "area_group": s.area_group,
                "sector": s.sector,
                "aliases": list(s.aliases),
                "priority": int(s.priority),
                "type": s.sector_type,
                "bbox_suggestion": dict(s.bbox_suggestion) if s.bbox_suggestion else None,
            }
        )
    return rows
