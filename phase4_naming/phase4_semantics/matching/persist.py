"""
Phase 4 – Persistence layer (aligned with run_phase4.py)

This module ONLY persists artifacts produced by previous steps:
- scored candidates
- chosen best candidate (assumes input is best-first)
- evidence + tags for audit

NO scoring. NO matching logic. NO ML.
"""

from __future__ import annotations

from typing import List, Dict, Any, Optional
import json

from phase4_semantics.common.db import fetchone, execute


# ============================================================
# Helpers
# ============================================================

def _table_exists(schema: str, table: str) -> bool:
    r = fetchone(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, table),
    )
    return bool(r)


def _ensure_phase4_tables() -> None:
    """
    Optional auto-create if you want the pipeline to be self-contained.
    If you already have tables, it does nothing.
    """
    if not _table_exists("semantics", "route_scored_candidates"):
        execute(
            """
            CREATE SCHEMA IF NOT EXISTS semantics;

            CREATE TABLE IF NOT EXISTS semantics.route_scored_candidates (
              route_id        uuid        NOT NULL,
              sample_version  text        NOT NULL,
              relation_id     bigint      NOT NULL,
              score           double precision NOT NULL,
              breakdown       jsonb       NOT NULL,
              evidence        jsonb       NOT NULL,
              relation_tags   jsonb,
              intersection    jsonb,
              created_at      timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (route_id, sample_version, relation_id)
            );
            """
        )

    if not _table_exists("semantics", "route_best_match"):
        execute(
            """
            CREATE TABLE IF NOT EXISTS semantics.route_best_match (
              route_id        uuid        NOT NULL,
              sample_version  text        NOT NULL,
              relation_id     bigint      NOT NULL,
              score           double precision NOT NULL,
              payload         jsonb       NOT NULL,
              created_at      timestamptz NOT NULL DEFAULT now(),
              PRIMARY KEY (route_id, sample_version)
            );
            """
        )


# ============================================================
# Main function expected by run_phase4.py
# ============================================================

def persist_matches(
    *,
    route_id: str,
    sample_version: str,
    scored_candidates: List[Dict[str, Any]],
    ensure_tables: bool = True,
) -> None:
    """
    ALIGNED CONTRACT with run_phase4.py:

      persist_matches(route_id=..., sample_version=..., scored_candidates=scored)

    Persists:
      1) All scored candidates into semantics.route_scored_candidates
      2) The best candidate (first item) into semantics.route_best_match
    """
    if ensure_tables:
        _ensure_phase4_tables()

    if not scored_candidates:
        # still keep things consistent: clear any previous rows for this route/version
        if _table_exists("semantics", "route_scored_candidates"):
            execute(
                """
                DELETE FROM semantics.route_scored_candidates
                WHERE route_id = %s AND sample_version = %s
                """,
                (route_id, sample_version),
            )
        if _table_exists("semantics", "route_best_match"):
            execute(
                """
                DELETE FROM semantics.route_best_match
                WHERE route_id = %s AND sample_version = %s
                """,
                (route_id, sample_version),
            )
        return

    # 0) Clear previous rows (idempotent overwrite)
    execute(
        """
        DELETE FROM semantics.route_scored_candidates
        WHERE route_id = %s AND sample_version = %s
        """,
        (route_id, sample_version),
    )

    # 1) Insert all scored candidates
    for row in scored_candidates:
        execute(
            """
            INSERT INTO semantics.route_scored_candidates (
              route_id, sample_version, relation_id,
              score, breakdown, evidence, relation_tags, intersection
            )
            VALUES (%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb)
            """,
            (
                route_id,
                sample_version,
                int(row["relation_id"]),
                float(row.get("score") or 0.0),
                json.dumps(row.get("breakdown") or {}),
                json.dumps(row.get("evidence") or {}),
                json.dumps(row.get("relation_tags") or {}),
                json.dumps(row.get("intersection") or {}),
            ),
        )

    # 2) Best candidate (assumes list is best-first)
    best = scored_candidates[0]
    execute(
        """
        INSERT INTO semantics.route_best_match (
          route_id, sample_version, relation_id, score, payload
        )
        VALUES (%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (route_id, sample_version)
        DO UPDATE SET
          relation_id = EXCLUDED.relation_id,
          score       = EXCLUDED.score,
          payload     = EXCLUDED.payload,
          created_at  = now()
        """,
        (
            route_id,
            sample_version,
            int(best["relation_id"]),
            float(best.get("score") or 0.0),
            json.dumps(best),
        ),
    )


# ============================================================
# (Optional) Your older "final decision" persistence
# Keep it — but it's a different step (after compile/publish).
# ============================================================

def persist_canonical_name(
    conn,
    route_id,
    canonical_name: str,
    confidence: float,
    source: str = "phase4",
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_prod.route_names (
                route_id,
                canonical_name,
                confidence,
                source
            )
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (route_id)
            DO UPDATE SET
                canonical_name = EXCLUDED.canonical_name,
                confidence     = EXCLUDED.confidence,
                source         = EXCLUDED.source,
                updated_at     = now()
            """,
            (route_id, canonical_name, confidence, source),
        )
    conn.commit()


def persist_aliases(
    conn,
    route_id,
    aliases: List[str],
    source: str = "phase4",
):
    if not aliases:
        return

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM route_prod.route_aliases
            WHERE route_id = %s AND source = %s
            """,
            (route_id, source),
        )

        cur.executemany(
            """
            INSERT INTO route_prod.route_aliases (
                route_id,
                alias,
                source
            )
            VALUES (%s, %s, %s)
            """,
            [(route_id, a, source) for a in aliases],
        )
    conn.commit()


def persist_semantic_evidence(
    conn,
    route_id,
    evidence: Dict[str, Any],
    source: str = "phase4",
):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_prod.route_semantic_evidence (
                route_id,
                source,
                evidence
            )
            VALUES (%s, %s, %s)
            ON CONFLICT (route_id, source)
            DO UPDATE SET
                evidence   = EXCLUDED.evidence,
                updated_at = now()
            """,
            (route_id, source, json.dumps(evidence)),
        )
    conn.commit()
