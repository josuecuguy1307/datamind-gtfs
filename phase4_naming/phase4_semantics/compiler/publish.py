# phase4_semantics/compiler/publish.py
"""
Publish (Write-back) Engine - Phase 4 V1P

This module is the ONLY place that writes final semantics back into route_prod.routes.

It supports 2 modes:

1) propose/update semantics fields:
   - route_name
   - route_aliases
   - landmark_tags
   - direction_semantics
   - naming_confidence
   - semantics_updated_at = now()
   - human_verified stays FALSE unless explicitly approved

2) approve/publish:
   - human_verified = TRUE
   - semantics_updated_at = now()

Optional:
- when admin approves a match between evidence_record and route_id,
  we can insert a label row into semantics.match_labels for training.

Design goal:
- robust to schema differences (column existence checks)
- safe for dev environments
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, List

import json
from datetime import datetime

import psycopg2
from psycopg2.extras import Json


# -----------------------------
# DB helpers
# -----------------------------

def _q1(cur, sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return None
    cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


def _qall(cur, sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, params)
    rows = cur.fetchall() or []
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _table_exists(cur, schema: str, table: str) -> bool:
    row = _q1(
        cur,
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return bool(row)


def _column_exists(cur, schema: str, table: str, col: str) -> bool:
    row = _q1(
        cur,
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s AND column_name = %s
        """,
        (schema, table, col),
    )
    return bool(row)


# -----------------------------
# Main publish functions
# -----------------------------

SEM_TABLE_SCHEMA = "route_prod"
SEM_TABLE_NAME = "routes"


def propose_route_semantics(
    cur,
    route_id: str,
    *,
    route_name: Optional[str] = None,
    route_aliases: Optional[list] = None,
    landmark_tags: Optional[list] = None,
    direction_semantics: Optional[dict] = None,
    naming_confidence: Optional[float] = None,
    debug_payload: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Updates route_prod.routes semantic fields WITHOUT marking human_verified.
    Safe: updates only columns that exist.
    """

    if not _table_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME):
        raise RuntimeError(f"Missing table {SEM_TABLE_SCHEMA}.{SEM_TABLE_NAME}")

    set_parts = []
    params: List[Any] = []

    def add_set(col: str, val: Any):
        set_parts.append(f"{col} = %s")
        params.append(val)

    # Only set if column exists AND value is provided
    if route_name is not None and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "route_name"):
        add_set("route_name", route_name)

    if route_aliases is not None and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "route_aliases"):
        add_set("route_aliases", route_aliases)

    if landmark_tags is not None and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "landmark_tags"):
        add_set("landmark_tags", landmark_tags)

    if direction_semantics is not None and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "direction_semantics"):
        add_set("direction_semantics", Json(direction_semantics))

    if naming_confidence is not None and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "naming_confidence"):
        add_set("naming_confidence", float(naming_confidence))

    # Always update semantics_updated_at if exists (important)
    if _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "semantics_updated_at"):
        add_set("semantics_updated_at", datetime.utcnow())

    # Optional debug field if you later add it
    if debug_payload is not None and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "semantics_debug"):
        add_set("semantics_debug", Json(debug_payload))

    # Nothing to update => just return current row
    if not set_parts:
        return {"ok": True, "route_id": route_id, "updated": False, "reason": "no_columns_or_values"}

    sql = f"""
    UPDATE {SEM_TABLE_SCHEMA}.{SEM_TABLE_NAME}
    SET {", ".join(set_parts)}
    WHERE route_id = %s
    RETURNING route_id
    """

    params.append(route_id)
    cur.execute(sql, tuple(params))

    return {"ok": True, "route_id": route_id, "updated": True, "mode": "propose"}


def approve_route_semantics(
    cur,
    route_id: str,
    *,
    actor: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Marks route as human_verified = TRUE.
    This is what your Admin UI triggers when user clicks "Approve / Publish".
    """

    if not _table_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME):
        raise RuntimeError(f"Missing table {SEM_TABLE_SCHEMA}.{SEM_TABLE_NAME}")

    set_parts = []
    params: List[Any] = []

    if _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "human_verified"):
        set_parts.append("human_verified = TRUE")

    if _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "semantics_updated_at"):
        set_parts.append("semantics_updated_at = %s")
        params.append(datetime.utcnow())

    # Optional audit columns if you add them later
    if actor and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "verified_by"):
        set_parts.append("verified_by = %s")
        params.append(actor)

    if notes and _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "verification_notes"):
        set_parts.append("verification_notes = %s")
        params.append(notes)

    if not set_parts:
        return {"ok": True, "route_id": route_id, "approved": False, "reason": "no_columns"}

    sql = f"""
    UPDATE {SEM_TABLE_SCHEMA}.{SEM_TABLE_NAME}
    SET {", ".join(set_parts)}
    WHERE route_id = %s
    RETURNING route_id
    """

    params.append(route_id)
    cur.execute(sql, tuple(params))

    return {"ok": True, "route_id": route_id, "approved": True, "mode": "approve"}


def insert_match_label(
    cur,
    *,
    record_id: str,
    route_id: str,
    relevance: int = 2,
    label_source: str = "admin_ui",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Optional but IMPORTANT for LightGBM training later.

    When an admin chooses:
      evidence_record_id -> correct route_id
    we store a ground truth label row.

    This function is safe: it does nothing if table doesn't exist.
    """

    if not _table_exists(cur, "semantics", "match_labels"):
        return {"ok": True, "inserted": False, "reason": "semantics.match_labels_missing"}

    cur.execute(
        """
        INSERT INTO semantics.match_labels (
          record_id, route_id, relevance, label_source, notes
        )
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (record_id, route_id) DO UPDATE
        SET relevance = EXCLUDED.relevance,
            label_source = EXCLUDED.label_source,
            notes = EXCLUDED.notes
        RETURNING label_id
        """,
        (record_id, route_id, int(relevance), label_source, notes),
    )

    row = cur.fetchone()
    label_id = row[0] if row else None
    return {"ok": True, "inserted": True, "label_id": str(label_id) if label_id else None}


def auto_approve_if_confident(
    cur,
    route_id: str,
    *,
    confidence: Optional[float],
    threshold: float = 0.90,
    actor: str = "auto_phase4",
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Auto-approves (human_verified=true) if naming_confidence >= threshold.

    ✅ schema-safe:
      - checks table existence
      - checks column existence
      - does nothing if already human_verified=true (if that column exists)

    Intended use:
      - after propose_route_semantics() has written naming_confidence
      - called by compiler when approve=False but you want auto approval
    """
    if confidence is None:
        return {"ok": True, "approved": False, "reason": "confidence_missing"}

    try:
        conf = float(confidence)
    except Exception:
        return {"ok": True, "approved": False, "reason": "confidence_not_float"}

    if conf < float(threshold):
        return {
            "ok": True,
            "approved": False,
            "reason": "below_threshold",
            "confidence": conf,
            "threshold": float(threshold),
        }

    if not _table_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME):
        raise RuntimeError(f"Missing table {SEM_TABLE_SCHEMA}.{SEM_TABLE_NAME}")

    # If already verified, skip
    if _column_exists(cur, SEM_TABLE_SCHEMA, SEM_TABLE_NAME, "human_verified"):
        row = _q1(
            cur,
            f"""
            SELECT human_verified
            FROM {SEM_TABLE_SCHEMA}.{SEM_TABLE_NAME}
            WHERE route_id = %s
            """,
            (route_id,),
        )
        if row and row.get("human_verified") is True:
            return {"ok": True, "approved": False, "reason": "already_verified"}

    # Otherwise approve
    auto_notes = notes or f"auto-approved by phase4 (confidence={conf:.4f} >= {float(threshold):.4f})"
    return approve_route_semantics(cur, route_id, actor=actor, notes=auto_notes)
