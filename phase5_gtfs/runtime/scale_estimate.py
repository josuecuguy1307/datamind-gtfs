"""Proportional scaling of per-leg runtime estimates to match a known total."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import psycopg2.extras

from phase5_gtfs.common.config import db_conn


def scale_estimate_to_known_total(
    estimate_id: str,
    known_total_secs: float,
    time_period: str = "offpeak",
    source: str = "deep_research",
    *,
    conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Scale an existing per-leg estimate so leg times sum to *known_total_secs*.

    The physics model gives relative proportions (longer legs, steeper grades,
    more signals = proportionally more time).  The known total gives magnitude.

    Parameters
    ----------
    estimate_id : str
        UUID of an existing ``runtime_route_estimates`` row.
    known_total_secs : float
        Ground-truth total runtime in seconds (e.g. from Deep Research).
    time_period : str
        ``"offpeak"`` or ``"peak"`` — which timing column to scale.
    source : str
        Attribution tag stored alongside the scale factor.

    Returns
    -------
    dict  with ``estimate_id``, ``model_total_secs``, ``known_total_secs``,
    ``scale_factor``, ``legs_updated``, ``time_period``.
    """
    own_conn = conn is None
    if own_conn:
        conn = db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ---- load legs ----
            time_col = "peak_secs" if time_period == "peak" else "offpeak_secs"
            cur.execute(
                """
                SELECT leg_feature_id, leg_idx,
                       COALESCE(offpeak_secs, 0)::float8 AS offpeak_secs,
                       COALESCE(peak_secs, 0)::float8    AS peak_secs,
                       attrs
                FROM gtfs_work.runtime_route_leg_features
                WHERE estimate_id = %s::uuid
                ORDER BY leg_idx
                """,
                (estimate_id,),
            )
            legs: List[Dict[str, Any]] = [dict(r) for r in (cur.fetchall() or [])]
            if not legs:
                raise ValueError(f"No leg features for estimate {estimate_id}")

            model_total = sum(float(lg[time_col]) for lg in legs)
            if model_total <= 0:
                raise ValueError(f"Model total is {model_total} — cannot scale")

            scale_factor = known_total_secs / model_total

            # ---- scale each leg (travel portion only; dwell stays fixed) ----
            for lg in legs:
                attrs = dict(lg.get("attrs") or {})
                dwell_key = f"dwell_{time_period}_secs"
                dwell = max(0.0, float(attrs.get(dwell_key, 0.0)))
                old_total = float(lg[time_col])
                old_travel = max(0.0, old_total - dwell)

                new_travel = old_travel * scale_factor
                new_total = new_travel + dwell

                # Record calibration metadata
                attrs["scale_factor"] = round(scale_factor, 6)
                attrs["scale_source"] = source
                attrs[f"travel_{time_period}_secs"] = round(new_travel, 2)

                cur.execute(
                    f"""
                    UPDATE gtfs_work.runtime_route_leg_features
                    SET {time_col} = %s,
                        attrs = %s::jsonb
                    WHERE leg_feature_id = %s::uuid
                    """,
                    (round(new_total, 2), psycopg2.extras.Json(attrs), lg["leg_feature_id"]),
                )

            # ---- update route-level estimate ----
            metric_key = f"runtime_{time_period}_secs"
            new_route_total = sum(
                (float(lg[time_col]) - max(0.0, float((lg.get("attrs") or {}).get(f"dwell_{time_period}_secs", 0))))
                * scale_factor
                + max(0.0, float((lg.get("attrs") or {}).get(f"dwell_{time_period}_secs", 0)))
                for lg in legs
            )
            cur.execute(
                f"""
                UPDATE gtfs_work.runtime_route_estimates
                SET metrics = jsonb_set(
                    jsonb_set(
                        jsonb_set(
                            COALESCE(metrics, '{{}}'::jsonb),
                            '{{{metric_key}}}', %s::text::jsonb
                        ),
                        '{{calibration_factor}}', %s::text::jsonb
                    ),
                    '{{calibration_source}}', %s::jsonb
                ),
                estimated_at = NOW()
                WHERE estimate_id = %s::uuid
                """,
                (
                    round(new_route_total, 1),
                    round(scale_factor, 6),
                    psycopg2.extras.Json(source),
                    estimate_id,
                ),
            )

        if own_conn:
            conn.commit()

        return {
            "estimate_id": estimate_id,
            "model_total_secs": round(model_total, 1),
            "known_total_secs": round(known_total_secs, 1),
            "scale_factor": round(scale_factor, 4),
            "legs_updated": len(legs),
            "time_period": time_period,
        }
    finally:
        if own_conn:
            conn.close()
