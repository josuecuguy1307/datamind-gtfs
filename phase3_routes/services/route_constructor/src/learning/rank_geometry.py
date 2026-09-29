from __future__ import annotations

import json
import uuid
from src.db.conn import db_cursor
from src.geometry.stop_recovery import ensure_geometry_stop_recovery_schema

RECOVERY_REWARD = 2.5
AMBIGUITY_PENALTY = 1.25
REJECTION_PENALTY = 0.05
COMPLETENESS_REWARD = 1.5


def _uuid_str(x) -> str:
    if isinstance(x, uuid.UUID):
        return str(x)
    return str(uuid.UUID(str(x)))


def rank_geometry_set(conn, *, route_id: uuid.UUID, geometry_set_id: uuid.UUID) -> None:
    """
    V1 ranker:
      - ranks by existing 'score' DESC (NULL scores go last)
      - writes metrics.ml_rank and metrics.ml_score into route_work.geometry_candidates

    Works for BOTH:
      - sequence-based (stop_sequence_candidate_id NOT NULL)
      - raw-first (stop_sequence_candidate_id NULL)
    """
    del route_id
    ensure_geometry_stop_recovery_schema(conn)
    ranked_rows = []
    with db_cursor(conn) as cur:
        cur.execute(
            """
            SELECT
              gc.geometry_candidate_id,
              gc.score,
              gc.metrics,
              gc.stop_sequence_candidate_id,
              gsr.summary_metrics AS recovery_summary,
              gsr.recovered_stop_ids,
              gsr.ambiguous_nearby_stop_ids,
              gsr.rejected_nearby_stop_ids,
              gsr.enriched_stop_ids
            FROM route_work.geometry_candidates gc
            LEFT JOIN route_work.geometry_stop_recovery gsr
              ON gsr.geometry_candidate_id = gc.geometry_candidate_id
            WHERE gc.set_id=%s
            ORDER BY gc.score DESC NULLS LAST, gc.created_at ASC
            """,
            (str(geometry_set_id),),
        )
        rows = cur.fetchall()

        if not rows:
            raise ValueError(f"No geometry candidates for set_id={geometry_set_id}")

        for r in rows:
            recovery_summary = dict(r.get("recovery_summary") or {})
            base_score = float(r.get("score")) if r.get("score") is not None else -1e18
            recovered_count = int(recovery_summary.get("recovered_count") or len(r.get("recovered_stop_ids") or []))
            ambiguous_count = int(recovery_summary.get("ambiguous_count") or len(r.get("ambiguous_nearby_stop_ids") or []))
            rejected_count = int(recovery_summary.get("rejected_count") or len(r.get("rejected_nearby_stop_ids") or []))
            original_count = int(recovery_summary.get("original_stop_count") or 0)
            completion_gain = (float(recovered_count) / float(original_count)) if original_count > 0 else 0.0
            recovery_delta = (
                (recovered_count * RECOVERY_REWARD)
                + (completion_gain * COMPLETENESS_REWARD)
                - (ambiguous_count * AMBIGUITY_PENALTY)
                - (rejected_count * REJECTION_PENALTY)
            )
            ml_score = base_score + recovery_delta
            ranked_rows.append(
                {
                    "geometry_candidate_id": _uuid_str(r["geometry_candidate_id"]),
                    "metrics": dict(r.get("metrics") or {}),
                    "stop_sequence_candidate_id": r.get("stop_sequence_candidate_id"),
                    "recovery_summary": recovery_summary,
                    "recovered_stop_ids": list(r.get("recovered_stop_ids") or []),
                    "ambiguous_stop_ids": list(r.get("ambiguous_nearby_stop_ids") or []),
                    "rejected_stop_ids": list(r.get("rejected_nearby_stop_ids") or []),
                    "enriched_stop_ids": list(r.get("enriched_stop_ids") or []),
                    "ml_score": ml_score,
                    "recovery_delta": recovery_delta,
                }
            )

        ranked_rows.sort(
            key=lambda row: (
                -float(row.get("ml_score") or -1e18),
                str(row.get("geometry_candidate_id") or ""),
            )
        )

        for rank, row in enumerate(ranked_rows, start=1):
            metrics = dict(row.get("metrics") or {})
            seq_id = row.get("stop_sequence_candidate_id")
            metrics["ml_mode"] = "sequence_based" if seq_id is not None else "raw_first"
            metrics["ml_rank"] = rank
            metrics["ml_score"] = float(row.get("ml_score") or -1e18)
            metrics["stop_recovery_rank_delta"] = round(float(row.get("recovery_delta") or 0.0), 4)
            metrics["stop_recovery_summary"] = dict(row.get("recovery_summary") or {})
            metrics["stop_recovery_recovered_stop_ids"] = [str(x) for x in list(row.get("recovered_stop_ids") or [])]
            metrics["stop_recovery_ambiguous_stop_ids"] = [str(x) for x in list(row.get("ambiguous_stop_ids") or [])]
            metrics["stop_recovery_rejected_stop_ids"] = [str(x) for x in list(row.get("rejected_stop_ids") or [])]
            metrics["stop_recovery_enriched_stop_ids"] = [str(x) for x in list(row.get("enriched_stop_ids") or [])]

            cur.execute(
                """
                UPDATE route_work.geometry_candidates
                SET metrics=%s::jsonb
                WHERE geometry_candidate_id=%s
                """,
                (json.dumps(metrics, ensure_ascii=False), row["geometry_candidate_id"]),
            )
