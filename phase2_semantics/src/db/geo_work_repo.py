"""
Phase 2 – Semantic Geocoder
Repository: geo_work (INTERMEDIATE / ML / CANDIDATE STAGE)

RESPONSIBILITIES:
- Candidate set lifecycle
- Alias candidates (search results)
- Place candidates (aggregated)
- Node ↔ place temporary mappings
- Metrics, selection logs, model registry

NON-RESPONSIBILITIES:
- ❌ Raw ingestion
- ❌ Extract runs
- ❌ Production truth

RULE:
This module NEVER touches geo_raw or geo_prod semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from datetime import datetime
from uuid import UUID

from psycopg2.extensions import connection as PGConnection

from src.settings import (
    T_PLACE_CANDIDATE_SETS,
    T_PLACE_CANDIDATES,
    T_ALIAS_CANDIDATES,
    T_NODE_PLACE_WORK,
    T_PLACE_SET_METRICS,
    T_SELECTION_LOG,
    T_MODEL_REGISTRY,
    T_PLACE_NAME_CANDIDATES,
    T_PLACE_NAME_FEEDBACK,
)

# ============================================================
# Domain models (WORK = volatile, can evolve quickly)
# ============================================================

@dataclass(frozen=True)
class PlaceCandidateSet:
    candidate_set_id: UUID
    context_key: str
    query_text: str
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class AliasCandidate:
    candidate_set_id: UUID
    place_id: str
    alias: str
    score: float
    source: str = "opensearch"
    meta: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class PlaceCandidate:
    candidate_set_id: UUID
    place_id: str
    score: float
    best_alias: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class NodePlaceWorkMap:
    candidate_set_id: UUID
    node_id: str
    place_id: str
    score: float
    source: str = "phase2_work"
    meta: Optional[Dict[str, Any]] = None


# ============================================================
# Candidate-set lifecycle (ENTRY POINT)
# ============================================================

def create_candidate_set(
    conn: PGConnection,
    *,
    candidate_set_id: UUID,
    context_key: str,
    query_text: str,
) -> None:
    """
    Create a new candidate set (ONE per query / run).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_PLACE_CANDIDATE_SETS}
              (candidate_set_id, context_key, query_text, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (candidate_set_id, context_key, query_text, datetime.utcnow()),
        )


def get_candidate_set(
    conn: PGConnection,
    candidate_set_id: UUID,
) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {T_PLACE_CANDIDATE_SETS}
            WHERE candidate_set_id = %s
            """,
            (candidate_set_id,),
        )
        return cur.fetchone()


# ============================================================
# Alias candidates (SEARCH-LEVEL)
# ============================================================

def insert_alias_candidates(
    conn: PGConnection,
    rows: Iterable[AliasCandidate],
) -> int:
    items = list(rows)
    if not items:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO {T_ALIAS_CANDIDATES}
              (candidate_set_id, place_id, alias, score, source, meta)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT DO NOTHING
            """,
            [
                (
                    r.candidate_set_id,
                    r.place_id,
                    r.alias,
                    float(r.score),
                    r.source,
                    r.meta or {},
                )
                for r in items
            ],
        )

    return len(items)


def get_alias_candidates(
    conn: PGConnection,
    candidate_set_id: UUID,
    *,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {T_ALIAS_CANDIDATES}
            WHERE candidate_set_id = %s
            ORDER BY score DESC
            LIMIT %s
            """,
            (candidate_set_id, limit),
        )
        return cur.fetchall() or []


# ============================================================
# Place candidates (AGGREGATED)
# ============================================================

def upsert_place_candidates(
    conn: PGConnection,
    rows: Iterable[PlaceCandidate],
) -> int:
    items = list(rows)
    if not items:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO {T_PLACE_CANDIDATES}
              (candidate_set_id, place_id, score, best_alias, meta)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (candidate_set_id, place_id)
            DO UPDATE SET
              score = EXCLUDED.score,
              best_alias = EXCLUDED.best_alias,
              meta = EXCLUDED.meta
            """,
            [
                (
                    r.candidate_set_id,
                    r.place_id,
                    float(r.score),
                    r.best_alias,
                    r.meta or {},
                )
                for r in items
            ],
        )

    return len(items)


def get_place_candidates(
    conn: PGConnection,
    candidate_set_id: UUID,
    *,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {T_PLACE_CANDIDATES}
            WHERE candidate_set_id = %s
            ORDER BY score DESC
            LIMIT %s
            """,
            (candidate_set_id, limit),
        )
        return cur.fetchall() or []


# ============================================================
# Node ↔ place temporary mapping (MVP)
# ============================================================

def upsert_node_place_work_map(
    conn: PGConnection,
    rows: Iterable[NodePlaceWorkMap],
) -> int:
    items = list(rows)
    if not items:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO {T_NODE_PLACE_WORK}
              (candidate_set_id, node_id, place_id, score, source, meta)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (candidate_set_id, node_id)
            DO UPDATE SET
              place_id = EXCLUDED.place_id,
              score = EXCLUDED.score,
              source = EXCLUDED.source,
              meta = EXCLUDED.meta
            """,
            [
                (
                    r.candidate_set_id,
                    r.node_id,
                    r.place_id,
                    float(r.score),
                    r.source,
                    r.meta or {},
                )
                for r in items
            ],
        )

    return len(items)


# ============================================================
# Metrics, logs, model registry
# ============================================================

def upsert_place_set_metrics(
    conn: PGConnection,
    *,
    candidate_set_id: UUID,
    metrics: Dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_PLACE_SET_METRICS}
              (candidate_set_id, metrics, updated_at)
            VALUES (%s, %s::jsonb, %s)
            ON CONFLICT (candidate_set_id)
            DO UPDATE SET
              metrics = EXCLUDED.metrics,
              updated_at = EXCLUDED.updated_at
            """,
            (candidate_set_id, metrics, datetime.utcnow()),
        )


def insert_selection_log(
    conn: PGConnection,
    *,
    candidate_set_id: UUID,
    approved_place_id: str,
    approved_score: float,
    reason: str,
    payload: Dict[str, Any],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_SELECTION_LOG}
              (candidate_set_id, approved_place_id, approved_score, reason, payload)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (candidate_set_id, approved_place_id, approved_score, reason, payload),
        )


def upsert_model_registry(
    conn: PGConnection,
    *,
    model_name: str,
    version: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_MODEL_REGISTRY}
              (model_name, version, meta, updated_at)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (model_name)
            DO UPDATE SET
              version = EXCLUDED.version,
              meta = EXCLUDED.meta,
              updated_at = EXCLUDED.updated_at
            """,
            (model_name, version, meta or {}, datetime.utcnow()),
        )


def list_name_candidates(
    conn: PGConnection,
    *,
    place_set_id: str,
    place_candidate_id: str,
) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {T_PLACE_NAME_CANDIDATES}
            WHERE place_set_id::text = %s
              AND place_candidate_id::text = %s
            ORDER BY model_rank ASC NULLS LAST, model_score DESC NULLS LAST, score DESC NULLS LAST, created_at ASC
            """,
            (place_set_id, place_candidate_id),
        )
        return cur.fetchall() or []


def insert_name_feedback(
    conn: PGConnection,
    *,
    place_set_id: str,
    place_candidate_id: str,
    chosen_name_candidate_id: Optional[str],
    chosen_name: str,
    chosen_name_norm: str,
    chosen_source: str,
    rejected_name_candidate_ids: List[str],
    reviewer: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_PLACE_NAME_FEEDBACK}
              (place_set_id, place_candidate_id, chosen_name_candidate_id, chosen_name, chosen_name_norm,
               chosen_source, rejected_name_candidate_ids, reviewer, context)
            VALUES (%s, %s, %s, %s, %s, %s, %s::uuid[], %s, %s::jsonb)
            """,
            (
                place_set_id,
                place_candidate_id,
                chosen_name_candidate_id,
                chosen_name,
                chosen_name_norm,
                chosen_source,
                "{" + ",".join(rejected_name_candidate_ids) + "}" if rejected_name_candidate_ids else "{}",
                reviewer,
                context or {},
            ),
        )
