from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


# ------------------------------------------------------------
# Data model
# ------------------------------------------------------------

@dataclass
class PlaceSetScore:
    """
    A scored candidate place set.

    - place_id: canonical identifier
    - score: aggregated confidence score
    """
    place_id: str
    score: float


# ------------------------------------------------------------
# Approval logic (decision layer)
# ------------------------------------------------------------

def approve_place_set(
    candidates: List[PlaceSetScore],
    *,
    min_score: float = 0.0,
    manual_choice: Optional[str] = None,
) -> PlaceSetScore:
    """
    Decide which place set becomes canonical.

    Decision order:
      1) Manual override (if provided)
      2) Highest-score candidate (max)
      3) Must pass min_score

    This function:
      - does NOT write to disk
      - does NOT log
      - only decides truth
    """

    if not candidates:
        raise ValueError("No place set candidates provided")

    # --------------------------------------------------------
    # 1) Manual override (human-in-the-loop)
    # --------------------------------------------------------
    if manual_choice is not None:
        for c in candidates:
            if c.place_id == manual_choice:
                return c
        raise ValueError(
            f"Manual choice '{manual_choice}' not found in candidates"
        )

    # --------------------------------------------------------
    # 2) Automatic decision (argmax)
    # --------------------------------------------------------
    best = max(candidates, key=lambda c: c.score)

    # --------------------------------------------------------
    # 3) Minimum confidence gate
    # --------------------------------------------------------
    if best.score < min_score:
        raise ValueError(
            f"Best score {best.score:.4f} below minimum {min_score:.4f}"
        )

    return best
