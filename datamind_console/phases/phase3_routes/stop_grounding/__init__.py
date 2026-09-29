"""
Phase 3 – Sequence Discovery & Geometry Constructor
====================================================

Transforms text-based route seeds (anchors + hints) into grounded stop
sequences and Valhalla-derived geometry candidates.

Pipeline stages:
  A. Route seed intake
  B. DB stop grounding (fuzzy text → real stop matches)
  C. Valhalla corridor construction
  D. Corridor ∩ stop DB intersection (PostGIS spatial query)
  E. Stop scoring + filtering (heuristic V0 / LightGBM)
  F. Sequence skeleton assembly
  G. Geometry candidate derivation
  H. LLM advisory refinement (grounded evidence only)
  I. Persistence + review artifacts
"""

from datamind_console.phases.phase3_routes.stop_grounding.contracts import (
    RouteSeed,
    StopMatch,
    StopGroundingResult,
    CorridorResult,
    CorridorStopCandidate,
    CorridorIntersectionResult,
    SequenceSkeleton,
    GeometryCandidate,
    ConstructorRunSummary,
)

__all__ = [
    "RouteSeed",
    "StopMatch",
    "StopGroundingResult",
    "CorridorResult",
    "CorridorStopCandidate",
    "CorridorIntersectionResult",
    "SequenceSkeleton",
    "GeometryCandidate",
    "ConstructorRunSummary",
]
