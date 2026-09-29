"""Phase 4.5 transactional commit — write direction_id + audit row atomically.

Single entry point for state transitions:

- ``apply`` mode: UPDATE ``route_prod.routes.direction_id`` + INSERT audit row
- ``dry_run`` mode: INSERT audit row only, with ``source='dry_run'``;
  ``direction_id`` is not touched

Both happen in one transaction. Commit only on success.
"""

from __future__ import annotations

import uuid
from typing import Optional

from .audit import AuditRow, record


def commit_state(
    conn,
    *,
    run_id: uuid.UUID,
    route_id: uuid.UUID,
    new_state: str,
    new_direction_id: Optional[int],
    source: str,
    prev_state: Optional[str] = None,
    prev_direction_id: Optional[int] = None,
    paired_route_id: Optional[uuid.UUID] = None,
    synthesized_node_ids: Optional[list[uuid.UUID]] = None,
    pair_score: Optional[float] = None,
    reason: Optional[str] = None,
    dry_run: bool = False,
) -> int:
    """Commit a Phase 4.5 state transition for one route.

    Returns the audit row ``id``. Caller is responsible for opening and
    closing the connection. The function manages its own transaction
    (single commit on success, rollback on error).
    """
    audit_source = "dry_run" if dry_run else source
    audit_row = AuditRow(
        run_id=run_id,
        route_id=route_id,
        prev_state=prev_state,
        new_state=new_state,
        prev_direction_id=prev_direction_id,
        new_direction_id=new_direction_id,
        paired_route_id=paired_route_id,
        synthesized_node_ids=tuple(synthesized_node_ids or ()),
        pair_score=pair_score,
        source=audit_source,
        reason=reason,
    )
    try:
        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE route_prod.routes
                       SET direction_id = %s
                     WHERE route_id = %s
                    """,
                    (new_direction_id, str(route_id)),
                )
        new_id = record(conn, audit_row)
        conn.commit()
        return new_id
    except Exception:
        conn.rollback()
        raise
