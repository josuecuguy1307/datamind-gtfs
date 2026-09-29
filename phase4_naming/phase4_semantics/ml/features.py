"""
Phase 4 – Feature engineering for route naming (ML + scoring)

This module converts semantic evidence into numeric feature vectors.
NO ML. NO persistence. NO heuristics.
"""

from typing import Dict, Any


# -----------------------------
# Feature extraction
# -----------------------------

def extract_features(evidence: Dict[str, Any]) -> Dict[str, float]:
    """
    Convert semantic evidence into a numeric feature vector.

    Expected evidence keys (optional):
        - token_overlap_ratio: float in [0,1]
        - token_overlap_count: int
        - ref_match: bool
        - alias_hit: bool
        - name_length_ok: bool
        - pattern_match: bool
    """

    features = {}

    # -----------------------------
    # Numeric features
    # -----------------------------

    features["token_overlap_ratio"] = _clamp(
        evidence.get("token_overlap_ratio", 0.0)
    )

    features["token_overlap_count"] = float(
        min(evidence.get("token_overlap_count", 0), 10)
    )

    # -----------------------------
    # Boolean → binary
    # -----------------------------

    features["ref_match"] = _bool(evidence.get("ref_match"))
    features["alias_hit"] = _bool(evidence.get("alias_hit"))
    features["name_length_ok"] = _bool(evidence.get("name_length_ok"))
    features["pattern_match"] = _bool(evidence.get("pattern_match"))

    return features


# -----------------------------
# Helpers
# -----------------------------

def _bool(value: Any) -> float:
    """
    Convert truthy / falsy values to {0.0, 1.0}.
    """
    return 1.0 if bool(value) else 0.0


def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    """
    Clamp numeric values safely to [lo, hi].
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0

    return max(lo, min(hi, v))
