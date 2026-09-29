"""Phase 4.5 stage 6 — rollback (DEFERRED).

Restore ``prev_state`` and ``prev_direction_id`` for every route touched
by a given ``run_id``. Mirrors the pattern in
``pipeline/snappers/precision_snap.py:--rollback``.

Implementation deferred to a follow-up session. See
``workspace/skills/direction_construction.md`` stage 6 for the full spec.
The audit table (``audit.py``) is in place and already records every
transition, so rollback can be added later without backfilling history.
"""

from __future__ import annotations


def rollback_run(*args, **kwargs):
    raise NotImplementedError(
        "see workspace/skills/direction_construction.md stage 6 — "
        "rollback CLI is deferred to a follow-up session"
    )
