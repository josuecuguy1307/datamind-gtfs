"""Phase 4.5 stage 4 — Deep Research escalation (DEFERRED).

For routes where synthesis fails, escalate to a templated Claude Deep
Research call (``workspace/skills/direction_dr_investigation.md``) to
determine whether the route operates one-directionally in real-world
service.

Implementation deferred to a follow-up session. See
``workspace/skills/direction_construction.md`` stage 4 for the full spec.
"""

from __future__ import annotations


def escalate_to_dr(*args, **kwargs):
    raise NotImplementedError(
        "see workspace/skills/direction_construction.md stage 4 — "
        "DR escalation is deferred to a follow-up session"
    )
