from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Sequence


from hades.geometry.canonical import (  # noqa: E402,F401
    EARTH_RADIUS_M,
    angular_delta_deg,
    bearing_deg,
    haversine_m,
)


def path_length_m(coords: Sequence[tuple[float, float]]) -> float:
    total = 0.0
    for idx in range(1, len(coords)):
        lon1, lat1 = coords[idx - 1]
        lon2, lat2 = coords[idx]
        total += haversine_m(lat1, lon1, lat2, lon2)
    return total


def normalize_name(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"\[[^\]]+\]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def stable_hash(parts: Iterable[Any]) -> str:
    payload = json.dumps(list(parts), sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def order_agreement_ratio(reference_ids: Sequence[str], candidate_ids: Sequence[str]) -> float:
    reference_rank = {stop_id: idx for idx, stop_id in enumerate(reference_ids)}
    common = [stop_id for stop_id in candidate_ids if stop_id in reference_rank]
    if len(common) <= 1:
        return 1.0
    agreeing = 0
    total = 0
    for idx, left in enumerate(common):
        for right in common[idx + 1 :]:
            total += 1
            if reference_rank[left] < reference_rank[right]:
                agreeing += 1
    if total == 0:
        return 1.0
    return agreeing / total
