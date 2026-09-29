from __future__ import annotations

from typing import Any, Dict, Mapping

from phase4_semantics.common.db import db_cursor, fetchall, execute_returning


def _table_exists(schema: str, table: str) -> bool:
    rows = fetchall(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        LIMIT 1
        """,
        (schema, table),
    )
    return bool(rows)


def persist_review(
    route_id: str,
    winner_candidate_id: str,
    candidate_scores: Mapping[str, int],
    *,
    reviewer: str = "console",
) -> Dict[str, Any]:
    if not _table_exists("semantics", "route_name_candidates"):
        raise RuntimeError("Table semantics.route_name_candidates not found. Apply migrations V4_3+ first.")

    rows = fetchall(
        """
        SELECT candidate_id, route_name, route_ref, operator_name, final_score
        FROM semantics.route_name_candidates
        WHERE route_id = %s
        """,
        (route_id,),
    )
    if not rows:
        raise RuntimeError("No candidates found for route. Run Step 20 first.")

    by_id = {str(r["candidate_id"]): r for r in rows}
    for cid, score in candidate_scores.items():
        if int(score) < 1 or int(score) > 5:
            raise RuntimeError(f"Invalid score for {cid}. Must be 1..5.")

    # Winner is driven by the highest user score.
    # Tie-break: lowest rank_pos from latest candidate ordering.
    rank_pos = {str(r["candidate_id"]): int(r.get("rank_pos") or 999999) for r in rows}
    scored_ids = [cid for cid in by_id.keys() if cid in candidate_scores]
    if not scored_ids:
        raise RuntimeError("No candidate scores provided.")

    computed_winner = min(
        scored_ids,
        key=lambda cid: (-int(candidate_scores.get(cid, 0)), rank_pos.get(cid, 999999)),
    )
    winner_candidate_id = computed_winner

    winner = by_id[winner_candidate_id]
    aliases = [r["route_name"] for cid, r in by_id.items() if cid != winner_candidate_id and r.get("route_name")]

    if _table_exists("semantics", "route_name_feedback"):
        with db_cursor() as cur:
            # Clear previous winner flag to avoid partial-unique-index conflict
            cur.execute(
                "UPDATE semantics.route_name_feedback SET is_winner = false WHERE route_id = %s AND reviewer = %s AND is_winner = true",
                (route_id, reviewer),
            )
            for cid, row in by_id.items():
                score = int(candidate_scores.get(cid, 3))
                cur.execute(
                    """
                    INSERT INTO semantics.route_name_feedback (
                        route_id,
                        candidate_id,
                        reviewer,
                        user_score,
                        is_winner,
                        feature_snapshot_version
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (route_id, candidate_id, reviewer)
                    DO UPDATE SET
                        user_score = EXCLUDED.user_score,
                        is_winner = EXCLUDED.is_winner,
                        created_at = now()
                    """,
                    (
                        route_id,
                        cid,
                        reviewer,
                        score,
                        cid == winner_candidate_id,
                        "v1",
                    ),
                )

    row = execute_returning(
        """
        INSERT INTO route_prod.route_semantics (
            route_id,
            route_name,
            route_ref,
            operator_name,
            route_aliases,
            landmark_tags,
            direction_semantics,
            naming_confidence,
            human_verified,
            semantics_updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, now())
        ON CONFLICT (route_id)
        DO UPDATE SET
            route_name = EXCLUDED.route_name,
            route_ref = EXCLUDED.route_ref,
            operator_name = EXCLUDED.operator_name,
            route_aliases = EXCLUDED.route_aliases,
            landmark_tags = EXCLUDED.landmark_tags,
            direction_semantics = EXCLUDED.direction_semantics,
            naming_confidence = EXCLUDED.naming_confidence,
            human_verified = EXCLUDED.human_verified,
            semantics_updated_at = now()
        RETURNING route_id, route_name, route_ref, operator_name, naming_confidence, human_verified
        """,
        (
            route_id,
            winner.get("route_name"),
            winner.get("route_ref"),
            winner.get("operator_name"),
            aliases,
            [],
            "{}",
            float(winner.get("final_score") or 0.0),
            True,
        ),
    )

    return {
        "route_id": route_id,
        "winner_candidate_id": winner_candidate_id,
        "reviewer": reviewer,
        "saved_feedback": len(by_id) if _table_exists("semantics", "route_name_feedback") else 0,
        "semantics": row,
    }
