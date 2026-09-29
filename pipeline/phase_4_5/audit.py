"""Phase 4.5 audit-table helpers.

Every state transition in Phase 4.5 writes a row to
``node_prod.direction_construction_audit`` (created by
``migrations/0NN_direction_construction_audit.sql``).

This module is the only place that knows the table layout. Mirror of the
``precision_snap_audit`` pattern — keeps the SQL local so the writer ↔
schema relationship is visible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Sequence

import psycopg2
import psycopg2.extras

AUDIT_TABLE = "node_prod.direction_construction_audit"

VALID_SOURCES = frozenset(
    {"pair_detected", "synthesized", "dr_confirmed", "operator_set", "rollback", "dry_run"}
)


@dataclass
class AuditRow:
    run_id: uuid.UUID
    route_id: uuid.UUID
    new_state: str
    source: str
    prev_state: Optional[str] = None
    prev_direction_id: Optional[int] = None
    new_direction_id: Optional[int] = None
    paired_route_id: Optional[uuid.UUID] = None
    synthesized_node_ids: Sequence[uuid.UUID] = field(default_factory=tuple)
    pair_score: Optional[float] = None
    reason: Optional[str] = None


def record(conn, row: AuditRow) -> int:
    """Insert one audit row. Returns the new ``id``. Caller controls the txn."""
    if row.source not in VALID_SOURCES:
        raise ValueError(
            f"invalid audit source {row.source!r}; "
            f"must be one of {sorted(VALID_SOURCES)}"
        )
    sql = f"""
        INSERT INTO {AUDIT_TABLE}
            (run_id, route_id, prev_state, new_state,
             prev_direction_id, new_direction_id,
             paired_route_id, synthesized_node_ids,
             pair_score, source, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                str(row.run_id),
                str(row.route_id),
                row.prev_state,
                row.new_state,
                row.prev_direction_id,
                row.new_direction_id,
                str(row.paired_route_id) if row.paired_route_id else None,
                [str(x) for x in row.synthesized_node_ids] or None,
                row.pair_score,
                row.source,
                row.reason,
            ),
        )
        new_id = cur.fetchone()[0]
    return int(new_id)


def find_run(conn, run_id: uuid.UUID) -> list[dict]:
    """Return every audit row for a given run, oldest first."""
    sql = f"""
        SELECT id, run_id, route_id, prev_state, new_state,
               prev_direction_id, new_direction_id,
               paired_route_id, synthesized_node_ids,
               pair_score, source, reason, created_at
        FROM {AUDIT_TABLE}
        WHERE run_id = %s
        ORDER BY id ASC
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, (str(run_id),))
        return [dict(r) for r in cur.fetchall()]
