from __future__ import annotations

from typing import Any, Dict, List

from src.db.conn import db_conn
from src.pipeline.embeddings.embedder import embed


def _vector_literal(values: List[float]) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"


def semantic_search(query: str, *, k: int = 50) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []

    vec = embed(q)
    vec_lit = _vector_literal(vec)

    sem_limit = max(50, int(k) * 20)
    lex_limit = max(50, int(k) * 20)

    with db_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                WITH params AS (
                  SELECT
                    lower(trim(%s::text)) AS q_norm,
                    ('%%' || lower(trim(%s::text)) || '%%') AS q_like,
                    %s::vector AS q_vec
                ),
                sem AS (
                  SELECT
                    e.alias_id,
                    (1.0 - (e.embedding <=> p.q_vec))::float8 AS semantic_score
                  FROM geo_prod.place_alias_embeddings e
                  JOIN geo_prod.place_aliases a2 ON a2.alias_id = e.alias_id
                  JOIN geo_prod.places pl ON pl.place_id = a2.place_id AND pl.status = 'active'
                  CROSS JOIN params p
                  ORDER BY (e.embedding <=> p.q_vec) ASC
                  LIMIT %s
                ),
                lex AS (
                  SELECT
                    a.alias_id,
                    similarity(a.normalized_alias, p.q_norm)::float8 AS trgm_score,
                    ts_rank_cd(
                      to_tsvector('simple', a.normalized_alias),
                      plainto_tsquery('simple', p.q_norm)
                    )::float8 AS fts_score
                  FROM geo_prod.place_aliases a
                  JOIN geo_prod.places pl ON pl.place_id = a.place_id AND pl.status = 'active'
                  CROSS JOIN params p
                  WHERE
                    a.normalized_alias % p.q_norm
                    OR to_tsvector('simple', a.normalized_alias) @@ plainto_tsquery('simple', p.q_norm)
                    OR a.normalized_alias LIKE p.q_like
                  ORDER BY GREATEST(
                    similarity(a.normalized_alias, p.q_norm),
                    ts_rank_cd(to_tsvector('simple', a.normalized_alias), plainto_tsquery('simple', p.q_norm))
                  ) DESC
                  LIMIT %s
                ),
                candidates AS (
                  SELECT alias_id FROM sem
                  UNION
                  SELECT alias_id FROM lex
                )
                SELECT
                  a.alias_id::text AS id,
                  e.place_id::text AS place_id,
                  a.alias AS alias_text,
                  a.lang,
                  a.alias_kind AS kind,
                  COALESCE(l.trgm_score, 0.0)::float8 AS trgm_score,
                  COALESCE(l.fts_score, 0.0)::float8 AS fts_score,
                  COALESCE(s.semantic_score, (1.0 - (e.embedding <=> p.q_vec)))::float8 AS semantic_score,
                  CASE WHEN a.normalized_alias = p.q_norm THEN 1.0 ELSE 0.0 END AS exact_bonus,
                  (
                    (0.42 * COALESCE(s.semantic_score, (1.0 - (e.embedding <=> p.q_vec))))
                    + (0.33 * COALESCE(l.trgm_score, 0.0))
                    + (0.20 * COALESCE(l.fts_score, 0.0))
                    + (0.05 * CASE WHEN a.normalized_alias = p.q_norm THEN 1.0 ELSE 0.0 END)
                  )::float8 AS score
                FROM candidates c
                JOIN geo_prod.place_aliases a
                  ON a.alias_id = c.alias_id
                JOIN geo_prod.place_alias_embeddings e
                  ON a.alias_id = e.alias_id
                CROSS JOIN params p
                LEFT JOIN sem s
                  ON s.alias_id = c.alias_id
                LEFT JOIN lex l
                  ON l.alias_id = c.alias_id
                ORDER BY score DESC, semantic_score DESC
                LIMIT %s
                """,
                    (q, q, vec_lit, sem_limit, lex_limit, int(k)),
                )
            except Exception:
                cur.execute(
                    """
                    WITH q AS (SELECT %s::vector AS v)
                    SELECT
                      a.alias_id::text AS id,
                      e.place_id::text AS place_id,
                      a.alias AS alias_text,
                      a.lang,
                      a.alias_kind AS kind,
                      0.0::float8 AS trgm_score,
                      0.0::float8 AS fts_score,
                      (1.0 - (e.embedding <=> q.v))::float8 AS semantic_score,
                      0.0::float8 AS exact_bonus,
                      (1.0 - (e.embedding <=> q.v))::float8 AS score
                    FROM geo_prod.place_alias_embeddings e
                    JOIN geo_prod.place_aliases a
                      ON a.alias_id = e.alias_id
                    JOIN geo_prod.places pl
                      ON pl.place_id = a.place_id AND pl.status = 'active'
                    CROSS JOIN q
                    ORDER BY (e.embedding <=> q.v) ASC
                    LIMIT %s
                    """,
                    (vec_lit, int(k)),
                )
            rows = list(cur.fetchall() or [])

    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": str(r.get("id") or ""),
                "score": float(r.get("score") or 0.0),
                "payload": {
                    "place_id": str(r.get("place_id") or ""),
                    "alias_text": str(r.get("alias_text") or ""),
                    "lang": r.get("lang"),
                    "kind": r.get("kind"),
                    "trgm_score": float(r.get("trgm_score") or 0.0),
                    "fts_score": float(r.get("fts_score") or 0.0),
                    "semantic_score": float(r.get("semantic_score") or 0.0),
                    "exact_bonus": float(r.get("exact_bonus") or 0.0),
                },
            }
        )
    return out
