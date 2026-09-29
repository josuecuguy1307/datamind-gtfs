from __future__ import annotations

from typing import Any, Dict

GENERIC_TOKENS = {
    "parada",
    "stop",
    "bus stop",
    "station",
    "estacion",
    "estación",
    "sin nombre",
    "unknown",
}


def compute_name_features(*, candidate_name: str, source_kind: str, freq: int = 1) -> Dict[str, float]:
    txt = (candidate_name or "").strip()
    low = txt.lower()
    token_count = len([t for t in low.split(" ") if t])
    has_digit = any(c.isdigit() for c in low)
    is_generic = 1.0 if low in GENERIC_TOKENS else 0.0
    source_weight = {
        "manual": 1.0,
        "custom": 0.95,
        "tag": 0.85,
        "model": 0.80,
        "generated": 0.75,
        "alias": 0.65,
    }.get(source_kind, 0.6)

    quality = 0.0
    if 3 <= len(txt) <= 64:
        quality += 0.45
    if 1 <= token_count <= 7:
        quality += 0.35
    if not has_digit:
        quality += 0.10
    if not is_generic:
        quality += 0.10

    return {
        "source_weight": float(source_weight),
        "quality": float(min(max(quality, 0.0), 1.0)),
        "freq": float(max(freq, 1)),
        "token_count": float(token_count),
        "has_digit": float(1.0 if has_digit else 0.0),
        "is_generic": float(is_generic),
        "length": float(len(txt)),
    }


def heuristic_score(features: Dict[str, Any]) -> float:
    source_weight = float(features.get("source_weight") or 0.0)
    quality = float(features.get("quality") or 0.0)
    freq = float(features.get("freq") or 1.0)
    generic_penalty = 0.35 * float(features.get("is_generic") or 0.0)
    digit_penalty = 0.10 * float(features.get("has_digit") or 0.0)
    freq_bonus = min(freq / 5.0, 0.25)
    score = (0.55 * source_weight) + (0.35 * quality) + freq_bonus - generic_penalty - digit_penalty
    return float(min(max(score, 0.0), 1.0))
