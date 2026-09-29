from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Dict, Any
from datetime import datetime
from uuid import UUID

from psycopg2.extensions import connection as PGConnection
from psycopg2.extras import execute_batch, Json

from src.settings import (
    T_GEO_EXTRACT_RUNS,
    T_GEO_NAME_EVIDENCE,
)


# ============================================================
# Domain models (RAW = evidence, not truth)
# ============================================================

@dataclass(frozen=True)
class GeoExtractRun:
    extract_run_id: UUID
    context_key: str
    source: str
    started_at: datetime
    finished_at: Optional[datetime]


@dataclass(frozen=True)
class NameEvidence:
    extract_run_id: UUID
    node_id: str
    source: str
    raw_text: str
    lang: Optional[str]
    weight_hint: float
    tags_snapshot: dict


# ============================================================
# WRITE: extract runs
# ============================================================

def insert_extract_run(
    conn: PGConnection,
    *,
    extract_run_id: UUID,
    context_key: str,
    status: str = "ok",
    extracted_at: datetime | None = None,
) -> None:
    """
    Register a new semantic extraction run.
    Matches geo_raw.extract_runs schema exactly.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_GEO_EXTRACT_RUNS}
              (extract_run_id, context_key, status, extracted_at)
            VALUES (%s, %s, %s, %s)
            """,
            (
                extract_run_id,
                context_key,
                status,
                extracted_at or datetime.utcnow(),
            ),
        )

def finish_extract_run(
    conn: PGConnection,
    *,
    extract_run_id: UUID,
    finished_at: Optional[datetime] = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {T_GEO_EXTRACT_RUNS}
            SET finished_at = %s
            WHERE extract_run_id = %s
            """,
            (
                finished_at or datetime.utcnow(),
                extract_run_id,
            ),
        )


# ============================================================
# WRITE: name evidence (low-level)
# ============================================================

def insert_name_evidence(
    conn: PGConnection,
    evidence: NameEvidence,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {T_GEO_NAME_EVIDENCE}
              (extract_run_id,
               node_id,
               source,
               raw_text,
               lang,
               weight_hint,
               tags_snapshot)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                evidence.extract_run_id,
                evidence.node_id,
                evidence.source,
                evidence.raw_text,
                evidence.lang,
                evidence.weight_hint,
                evidence.tags_snapshot,
            ),
        )


def bulk_insert_name_evidence(
    conn: PGConnection,
    rows: Iterable[NameEvidence],
) -> int:
    rows = list(rows)
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            f"""
            INSERT INTO {T_GEO_NAME_EVIDENCE}
              (extract_run_id,
               node_id,
               source,
               raw_text,
               lang,
               weight_hint,
               tags_snapshot)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    r.extract_run_id,
                    r.node_id,
                    r.source,
                    r.raw_text,
                    r.lang,
                    r.weight_hint,
                    r.tags_snapshot,
                )
                for r in rows
            ],
        )

    return len(rows)


# ============================================================
# PIPELINE API (what Step 10 expects)
# ============================================================
def register_extract_run(
    conn: PGConnection,
    *,
    extract_run_id: str,
    context_key: str,
) -> None:
    """
    Pipeline-facing helper.
    """
    insert_extract_run(
        conn,
        extract_run_id=UUID(extract_run_id),
        context_key=context_key,
        status="ok",
    )

def insert_name_evidence_batch(
    conn: PGConnection,
    rows: List[Dict[str, Any]],
    batch_size: int = 500,
) -> None:
    """
    Pipeline-facing batch insert.
    """
    if not rows:
        return

    sql = f"""
        INSERT INTO {T_GEO_NAME_EVIDENCE}
          (extract_run_id,
           node_id,
           source,
           raw_text,
           lang,
           weight_hint,
           tags_snapshot)
        VALUES (
           %(extract_run_id)s,
           %(node_id)s,
           %(source)s,
           %(raw_text)s,
           %(lang)s,
           %(weight_hint)s,
           %(tags_snapshot)s
        )
    """
    adapted_rows = []
    for r in rows:
        r2 = dict(r)
        r2["tags_snapshot"] = Json(r2["tags_snapshot"])
        adapted_rows.append(r2)

    with conn.cursor() as cur:
        execute_batch(cur, sql, adapted_rows, page_size=batch_size)