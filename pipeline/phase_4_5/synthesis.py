"""Phase 4.5 stage 3 — synthesis (DEFERRED).

Constructs the reverse direction of a route via lateral-vector logic:
reverse the polyline through Valhalla, project each stop laterally by
``LATERAL_OFFSET_M``, adopt nearby existing nodes or synthesize new ones.

Implementation deferred to a follow-up session. See
``workspace/skills/direction_construction.md`` stage 3 for the full spec.
"""

from __future__ import annotations


def synthesize_reverse(*args, **kwargs):
    raise NotImplementedError(
        "see workspace/skills/direction_construction.md stage 3 — "
        "synthesis is deferred to a follow-up session"
    )
