from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from phase4_semantics.common.db import db_cursor, fetchall, jsonb
from phase4_semantics.naming.normalize import canonicalize_operator, cleanup_text, load_json_catalog
from phase4_semantics.naming.scoring import score_candidate


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


def _latest_seed(route_id: str) -> Optional[Dict[str, Any]]:
    if not _table_exists("semantics", "route_name_seed_runs"):
        return None
    rows = fetchall(
        """
        SELECT *
        FROM semantics.route_name_seed_runs
        WHERE route_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (route_id,),
    )
    return rows[0] if rows else None


def _template_candidates(seed: Dict[str, Any], template_catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    templates = template_catalog.get("templates") or []
    values = {
        "name": seed.get("route_name") or "",
        "ref": seed.get("route_ref") or "",
        "operator": seed.get("operator_name") or "",
        "from": seed.get("from_name") or "",
        "to": seed.get("to_name") or "",
    }

    out: List[Dict[str, Any]] = []
    for tpl in templates:
        try:
            name = cleanup_text(str(tpl).format(**values))
        except Exception:
            continue
        if not name:
            continue
        out.append(
            {
                "route_name": name,
                "route_ref": cleanup_text(values["ref"]),
                "operator_name": canonicalize_operator(values["operator"]),
                "from_name": cleanup_text(values["from"]),
                "to_name": cleanup_text(values["to"]),
                "source_type": "seed_osm",
            }
        )
    return out


def _dedupe_candidates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        key = (
            row.get("route_name") or "",
            row.get("route_ref") or "",
            row.get("operator_name") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _persist_candidates(route_id: str, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not _table_exists("semantics", "route_name_candidates"):
        return candidates

    run_id = uuid.uuid4()
    persisted: List[Dict[str, Any]] = []

    with db_cursor() as cur:
        for idx, cand in enumerate(candidates, start=1):
            candidate_id = uuid.uuid4()
            cur.execute(
                """
                INSERT INTO semantics.route_name_candidates (
                    candidate_id,
                    route_id,
                    run_id,
                    rank_pos,
                    route_name,
                    route_ref,
                    operator_name,
                    source_type,
                    feature_snapshot_version,
                    features,
                    heuristic_score,
                    model_score,
                    final_score,
                    metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(candidate_id),
                    route_id,
                    str(run_id),
                    idx,
                    cand.get("route_name"),
                    cand.get("route_ref") or None,
                    cand.get("operator_name") or None,
                    cand.get("source_type") or "heuristic",
                    "v1",
                    jsonb(cand.get("features") or {}),
                    float(cand.get("heuristic_score") or 0.0),
                    None,
                    float(cand.get("final_score") or 0.0),
                    jsonb(cand.get("metadata") or {}),
                ),
            )
            row = dict(cand)
            row["candidate_id"] = str(candidate_id)
            row["route_id"] = route_id
            row["run_id"] = str(run_id)
            row["rank_pos"] = idx
            persisted.append(row)

    return persisted


def build_top_name_candidates(
    route_id: str,
    *,
    catalog_inputs: Optional[Dict[str, Any]] = None,
    user_inputs: Optional[Dict[str, Any]] = None,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    catalog_inputs = catalog_inputs or {}
    user_inputs = user_inputs or {}

    seed = _latest_seed(route_id)
    if not seed:
        raise RuntimeError("No seed found. Run Step 10 first.")

    seed_payload = {
        "route_name": seed.get("seed_route_name") or "",
        "route_ref": seed.get("seed_route_ref") or "",
        "operator_name": seed.get("seed_operator_name") or "",
        "from_name": seed.get("seed_from_name") or "",
        "to_name": seed.get("seed_to_name") or "",
    }

    template_catalog = load_json_catalog("name_templates.json")
    generated = _template_candidates(seed_payload, template_catalog)

    # Catalog override for operator
    operator_override = cleanup_text(str(catalog_inputs.get("operator_name") or ""))
    if operator_override:
        for row in generated:
            row["operator_name"] = canonicalize_operator(operator_override)

    # User custom names
    for text in user_inputs.get("custom_names") or []:
        name = cleanup_text(str(text or ""))
        if not name:
            continue
        generated.append(
            {
                "route_name": name,
                "route_ref": cleanup_text(str(user_inputs.get("route_ref") or seed_payload["route_ref"])),
                "operator_name": canonicalize_operator(str(user_inputs.get("operator_name") or seed_payload["operator_name"])),
                "from_name": seed_payload["from_name"],
                "to_name": seed_payload["to_name"],
                "source_type": "user_input",
            }
        )

    deduped = _dedupe_candidates(generated)

    scored: List[Dict[str, Any]] = []
    for cand in deduped:
        final_score, parts = score_candidate(cand)
        row = dict(cand)
        row["features"] = parts
        row["heuristic_score"] = final_score
        row["final_score"] = final_score
        row["metadata"] = {
            "seed_source": seed.get("seed_source"),
            "seed_run_id": str(seed.get("seed_run_id")) if seed.get("seed_run_id") else None,
        }
        scored.append(row)

    scored = sorted(scored, key=lambda x: float(x.get("final_score") or 0.0), reverse=True)[: max(1, int(top_k))]
    return _persist_candidates(route_id, scored)
