"""Phase 4.5 stage 2 — hard gate.

Classifies a route-pair score into one of three branches:

- ``"paired"`` — score >= RELIABLE_PAIR_SCORE; auto-accept, write both directions
- ``"operator_pending"`` — PLAUSIBLE_PAIR_SCORE <= score < RELIABLE_PAIR_SCORE;
  ambiguous, no direction_id written, queued for human review
- ``"synthesis_candidate"`` — score < PLAUSIBLE_PAIR_SCORE; proceed to stage 3

This module is intentionally tiny. The thresholds live in
``hades.geometry.canonical`` so every consumer reads from the same source.
"""

from __future__ import annotations

from typing import Optional

from hades.geometry.canonical import PLAUSIBLE_PAIR_SCORE, RELIABLE_PAIR_SCORE

PAIRED = "paired"
OPERATOR_PENDING = "operator_pending"
SYNTHESIS_CANDIDATE = "synthesis_candidate"
NO_SIGNAL = "no_signal"


def classify_by_score(score: Optional[float]) -> str:
    """Return the gate branch for a pair score.

    A ``None`` score means "no candidate pair was found at all" — distinct
    from a low score. Treated as ``"no_signal"`` so callers can decide
    whether to escalate to synthesis or DR.
    """
    if score is None:
        return NO_SIGNAL
    if score >= RELIABLE_PAIR_SCORE:
        return PAIRED
    if score >= PLAUSIBLE_PAIR_SCORE:
        return OPERATOR_PENDING
    return SYNTHESIS_CANDIDATE
