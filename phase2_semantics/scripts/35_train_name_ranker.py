from __future__ import annotations

from psycopg2.extras import Json, execute_values

from src.db.conn import db_conn
from src.pipeline.naming.name_ranker import train_weights, score
from src.pipeline.naming.poi_stop_model import train_type_model, predict_type
from src.utils.jsonlog import get_logger

logger = get_logger("phase2.train_models")

NAME_MODEL = "phase2_name_ranker_v1"
TYPE_MODEL = "phase2_place_type_model_v1"
MODEL_VERSION = "v1"

STOP_HINTS = ("terminal", "estacion", "estación", "parada", "stop", "station", "bus")
POI_HINTS = ("parque", "hospital", "universidad", "colegio", "mall", "museo", "mercado")


def _save_model(conn, model_name: str, artifact):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO geo_work.model_registry (model_name, version, artifact, updated_at)
            VALUES (%s, %s, %s::jsonb, now())
            ON CONFLICT (model_name)
            DO UPDATE SET
              version = EXCLUDED.version,
              artifact = EXCLUDED.artifact,
              updated_at = now()
            """,
            (model_name, MODEL_VERSION, Json(artifact)),
        )


def _load_name_training_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              c.features,
              CASE
                WHEN f.chosen_name_candidate_id = c.name_candidate_id THEN 1
                WHEN c.name_candidate_id = ANY(f.rejected_name_candidate_ids) THEN 0
                ELSE NULL
              END AS label
            FROM geo_work.place_name_feedback f
            JOIN geo_work.place_name_candidates c
              ON c.place_candidate_id = f.place_candidate_id
             AND c.place_set_id = f.place_set_id
            WHERE f.chosen_name_candidate_id IS NOT NULL
            """
        )
        rows = list(cur.fetchall() or [])

    out = []
    for r in rows:
        y = r.get("label")
        if y is None:
            continue
        out.append({"features": r.get("features") or {}, "label": int(y)})
    return out


def _rescore_name_candidates(conn, artifact):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name_candidate_id::text, place_set_id::text, place_candidate_id::text, features
            FROM geo_work.place_name_candidates
            """
        )
        rows = list(cur.fetchall() or [])

    by_pc = {}
    for r in rows:
        key = (str(r["place_set_id"]), str(r["place_candidate_id"]))
        by_pc.setdefault(key, []).append(r)

    updates = []
    for _, items in by_pc.items():
        scored = []
        for it in items:
            s = score(it.get("features") or {}, artifact)
            scored.append((it, float(s)))
        scored.sort(key=lambda x: x[1], reverse=True)
        for i, (it, s) in enumerate(scored, start=1):
            updates.append((s, i, i == 1, str(it["name_candidate_id"])))

    if not updates:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            UPDATE geo_work.place_name_candidates AS c
            SET model_score = v.model_score,
                model_rank = v.model_rank,
                selected_by_model = v.selected_by_model
            FROM (VALUES %s) AS v(model_score, model_rank, selected_by_model, name_candidate_id)
            WHERE c.name_candidate_id::text = v.name_candidate_id
            """,
            updates,
            template="(%s::double precision, %s::int, %s::boolean, %s::text)",
            page_size=1000,
        )
    return len(updates)


def _type_features(row):
    name = str(row.get("proposed_canonical_name") or "").lower()
    return {
        "tag_stop_weight": float(row.get("tag_stop_weight") or 0.0),
        "tag_poi_weight": float(row.get("tag_poi_weight") or 0.0),
        "transit_density_300m": float(row.get("transit_density_300m") or 0.0),
        "poi_density_300m": float(row.get("poi_density_300m") or 0.0),
        "name_stop_kw": float(1.0 if any(k in name for k in STOP_HINTS) else 0.0),
        "name_poi_kw": float(1.0 if any(k in name for k in POI_HINTS) else 0.0),
    }


def _load_type_training_rows(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              f.chosen_place_type,
              pc.proposed_canonical_name,
              COALESCE(gc.tag_stop_weight, 0) AS tag_stop_weight,
              COALESCE(gc.tag_poi_weight, 0) AS tag_poi_weight,
              COALESCE(gc.transit_density_300m, 0) AS transit_density_300m,
              COALESCE(gc.poi_density_300m, 0) AS poi_density_300m
            FROM geo_work.poi_stop_feedback f
            JOIN geo_work.place_candidates pc
              ON pc.place_candidate_id = f.place_candidate_id
            LEFT JOIN geo_work.node_geo_context gc
              ON gc.node_id::text = (pc.provenance ->> 'node_id')
            """
        )
        rows = list(cur.fetchall() or [])

    out = []
    for r in rows:
        out.append({"features": _type_features(r), "label": str(r.get("chosen_place_type") or "OTHER").upper()})
    return out


def _rescore_place_types(conn, artifact):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              pc.place_candidate_id::text,
              pc.proposed_canonical_name,
              COALESCE(gc.tag_stop_weight, 0) AS tag_stop_weight,
              COALESCE(gc.tag_poi_weight, 0) AS tag_poi_weight,
              COALESCE(gc.transit_density_300m, 0) AS transit_density_300m,
              COALESCE(gc.poi_density_300m, 0) AS poi_density_300m
            FROM geo_work.place_candidates pc
            LEFT JOIN geo_work.node_geo_context gc
              ON gc.node_id::text = (pc.provenance ->> 'node_id')
            """
        )
        rows = list(cur.fetchall() or [])

    updates = []
    for r in rows:
        pred = predict_type(_type_features(r), artifact)
        updates.append((pred["label"], float(pred["score"]), str(r["place_candidate_id"])))

    if not updates:
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            UPDATE geo_work.place_candidates AS pc
            SET model_place_type = v.model_place_type,
                model_place_type_score = v.model_place_type_score
            FROM (VALUES %s) AS v(model_place_type, model_place_type_score, place_candidate_id)
            WHERE pc.place_candidate_id::text = v.place_candidate_id
            """,
            updates,
            template="(%s::text, %s::double precision, %s::text)",
            page_size=1000,
        )
    return len(updates)


def main() -> None:
    with db_conn() as conn:
        name_rows = _load_name_training_rows(conn)
        name_artifact = train_weights(name_rows)
        _save_model(conn, NAME_MODEL, name_artifact)
        rescored_names = _rescore_name_candidates(conn, name_artifact)

        type_rows = _load_type_training_rows(conn)
        type_artifact = train_type_model(type_rows)
        _save_model(conn, TYPE_MODEL, type_artifact)
        rescored_types = _rescore_place_types(conn, type_artifact)

        conn.commit()

    logger.info(
        "✓ Step 35 completed",
        extra={
            "name_train_rows": len(name_rows),
            "type_train_rows": len(type_rows),
            "rescored_name_candidates": rescored_names,
            "rescored_place_types": rescored_types,
            "name_method": name_artifact.get("method"),
            "type_method": type_artifact.get("method"),
        },
    )


if __name__ == "__main__":
    main()
