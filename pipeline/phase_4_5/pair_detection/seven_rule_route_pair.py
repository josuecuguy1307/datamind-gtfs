"""Route-pair variant of the Phase 1 seven-rule classifier (DEFERRED).

Phase 1's classifier (``workspace/provinces/.../scripts/40_reclassify.py``)
operates on raw OSM stop pairs and outputs MERGE / KEEP_BOTH decisions.
Phase 4.5 needs a parallel classifier that operates on whole route-pair
stop sets — same physical signals (bearing, name tokens, OSM relations,
centerline projection) but the unit of decision is a pair of routes.

Skeleton only in this PR. Full implementation pulls common rule logic
into a shared module (``hades/direction_rules/``) and adapts the input
schema for route-pair stop bags. Tracked in the skill at stage 1.
"""

from __future__ import annotations


def classify_route_pair(*args, **kwargs):
    raise NotImplementedError(
        "see workspace/skills/direction_construction.md stage 1 — "
        "seven-rule route-pair classifier is a follow-up session"
    )
