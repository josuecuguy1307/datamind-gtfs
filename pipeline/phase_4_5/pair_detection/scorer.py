"""Thin wrapper around the migrated pair-scoring stack.

The CLI calls ``score_route_pair_evidence(...)`` from this module; we
re-export from the migrated location so consumers don't need to track
where the implementation lives.

Phase 4 migrates the underlying modules into ``pipeline.phase_4_5.pair_detection``;
until that migration lands, this wrapper imports from the legacy path
through the deprecation shim and emits a one-line warning.
"""

from __future__ import annotations

from typing import Any, Dict


def score_route_pair_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Score a route-pair evidence bundle.

    Returns the same dict shape as the migrated ``merge_scoring.score_route_pair_evidence``:
    ``{"opposite_direction_score": float, "same_route_family_score": float, ...}``.
    """
    # Lazy import — keeps Phase 4.5 importable even before Phase 4 migration lands.
    try:
        from pipeline.phase_4_5.pair_detection.merge_scoring import (
            score_route_pair_evidence as _scorer,
        )
    except ImportError:
        from datamind_console.ai_insights.merge_scoring import (
            score_route_pair_evidence as _scorer,
        )
    return _scorer(evidence)
