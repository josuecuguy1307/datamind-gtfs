# phase3_routes/scripts/05_discover_relation.py
"""
Step 05 — Discover route relation (dynamic)

Find candidate OSM PT relations inside a bbox (optionally filtered by ref/operator/name),
score them by:
  - prefer type=route over type=route_master
  - more stop priors extracted (members that look like stops/platforms)

Then:
  - (best effort) stores candidates into route_raw.relation_candidates
  - (best effort) stores chosen relation id into route_raw.route_jobs.chosen_osm_relation_id
  - prints:
      route_id: <uuid>
      osm_relation_id: <int>

Usage examples:
  python scripts/05_discover_relation.py new --bbox "-0.35,-78.55,-0.10,-78.35" --refs E1,E2
  python scripts/05_discover_relation.py new --bbox "s,w,n,e" --operator "Cooperativa" --name "Ecovia"
  python scripts/05_discover_relation.py <route_uuid> --bbox "s,w,n,e" --refs E1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

from src.db.conn import db_conn, db_cursor
from src.db.route_raw_repo import create_route_job  # you already have this
from src.evidence.overpass import (
    fetch_relation_overpass_json,
    normalize_query_strategy,
    search_route_relations,
)
from src.evidence.parse_relation import extract_stop_prior
from src.discover.operator_catalog import expand_operator_keys_or_aliases


BBox = Tuple[float, float, float, float]  # (south, west, north, east)
CANDIDATE_META_KEY = "_datamind_candidate_meta"


def _parse_bbox(s: str) -> BBox:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be 'south,west,north,east'")
    south, west, north, east = map(float, parts)
    return (south, west, north, east)


def _parse_refs(s: Optional[str]) -> Optional[List[str]]:
    if not s:
        return None
    refs = [x.strip() for x in s.split(",") if x.strip()]
    return refs or None


def _soft_signal_matches(
    tags: Dict[str, Any],
    *,
    refs: Optional[List[str]],
    operator_contains: Optional[str],
    name_contains: Optional[str],
) -> List[str]:
    out: List[str] = []
    ref_val = str(tags.get("ref") or "").strip().lower()
    if refs:
        clean_refs = [str(x or "").strip().lower() for x in refs if str(x or "").strip()]
        if any(ref_val == ref_txt or ref_txt in ref_val for ref_txt in clean_refs):
            out.append("ref")
    operator_val = str(tags.get("operator") or "").strip().lower()
    if operator_contains and operator_contains.strip().lower() in operator_val:
        out.append("operator")
    name_val = str(tags.get("name") or "").strip().lower()
    if name_contains and name_contains.strip().lower() in name_val:
        out.append("name")
    return out


def _score_candidate(
    tags: Dict[str, Any],
    stop_prior_count: int,
    *,
    refs: Optional[List[str]],
    operator_contains: Optional[str],
    name_contains: Optional[str],
) -> Dict[str, Any]:
    rel_type = (tags.get("type") or "").lower()
    is_route = 1 if rel_type == "route" else 0
    matched_soft_signals = _soft_signal_matches(
        tags,
        refs=refs,
        operator_contains=operator_contains,
        name_contains=name_contains,
    )

    score = 0.0
    score += 1000.0 * is_route
    score += 10.0 * float(stop_prior_count)

    if tags.get("ref"):
        score += 5.0
    if tags.get("name"):
        score += 2.0

    route = (tags.get("route") or "").lower()
    if route in {"bus", "minibus", "trolleybus", "share_taxi", "tram", "subway"}:
        score += 20.0

    if "ref" in matched_soft_signals:
        score += 35.0
    if "operator" in matched_soft_signals:
        score += 12.0
    if "name" in matched_soft_signals:
        score += 12.0

    reason_codes: List[str] = []
    if is_route:
        reason_codes.append("prefer_route_relation")
    if stop_prior_count >= 6:
        reason_codes.append("stop_prior_signal_strong")
    elif stop_prior_count > 0:
        reason_codes.append("stop_prior_signal_present")
    else:
        reason_codes.append("stop_prior_signal_missing")
    for code in matched_soft_signals:
        reason_codes.append(f"soft_{code}_match")

    return {
        "score": score,
        "matched_soft_signals": matched_soft_signals,
        "selection_reason_codes": reason_codes,
    }


def _selection_confidence(
    *,
    score: float,
    stop_prior_count: int,
    matched_soft_signals: List[str],
    score_gap_top2: Optional[float],
    selection_rank: int,
) -> float:
    conf = 0.25
    conf += min(0.35, max(0, stop_prior_count) * 0.04)
    conf += min(0.18, max(0, len(matched_soft_signals)) * 0.06)
    if score_gap_top2 is not None:
        conf += min(0.12, max(0.0, float(score_gap_top2)) / 120.0)
    if selection_rank == 1:
        conf += 0.08
    elif selection_rank > 1:
        conf -= min(0.20, float(selection_rank - 1) * 0.05)
    if score <= 0:
        conf = min(conf, 0.30)
    return max(0.05, min(0.95, round(conf, 4)))


def _best_effort_update_route_job_discovery(
    conn,
    *,
    route_id: uuid.UUID,
    bbox: BBox,
    refs: Optional[List[str]],
    operator_contains: Optional[str],
    name_contains: Optional[str],
    chosen_osm_relation_id: int,
) -> None:
    """
    Best-effort: update route_raw.route_jobs if your schema has these columns.
    If not, it will just warn and continue.
    """
    try:
        with db_cursor(conn) as cur:
            cur.execute(
                """
                UPDATE route_raw.route_jobs
                SET
                  bbox = COALESCE(%s::jsonb, bbox),
                  known_ref = COALESCE(%s, known_ref),
                  notes = COALESCE(%s, notes),
                  chosen_osm_relation_id = %s
                WHERE route_id = %s
                """,
                (
                    json.dumps(
                        {
                            "south": float(bbox[0]),
                            "west": float(bbox[1]),
                            "north": float(bbox[2]),
                            "east": float(bbox[3]),
                        },
                        ensure_ascii=False,
                    ),
                    (refs[0] if refs else None),
                    (
                        f"step05_discover operator={operator_contains or ''} name={name_contains or ''}".strip()
                        or None
                    ),
                    int(chosen_osm_relation_id),
                    str(route_id),
                ),
            )
    except Exception as e:
        print(f"[WARN] Could not update route_raw.route_jobs.chosen_osm_relation_id (schema mismatch?): {e}")


def _best_effort_store_candidates(
    conn,
    *,
    route_id: uuid.UUID,
    candidates: List[Dict[str, Any]],
    chosen_id: int,
) -> None:
    """
    Best-effort: insert into route_raw.relation_candidates.
    Canonical storage contract:
      route_id, osm_relation_id, rel_type, route_mode, ref, name, operator, tags
    Candidate scoring/enrichment metadata is stored under tags[_datamind_candidate_meta].
    """
    try:
        with db_cursor(conn) as cur:
            for c in candidates:
                tags_payload = dict(c.get("tags") or {})
                tags_payload[CANDIDATE_META_KEY] = {
                    "stop_prior_count": int(c.get("stop_prior_count") or 0),
                    "score": round(float(c.get("score") or 0.0), 4),
                    "matched_soft_signals": list(c.get("matched_soft_signals") or []),
                    "selection_reason_codes": list(c.get("selection_reason_codes") or []),
                    "selection_rank": int(c.get("selection_rank") or 0),
                    "selection_confidence": c.get("selection_confidence"),
                    "query_strategy": c.get("query_strategy"),
                    "hard_filters_applied": list(c.get("hard_filters_applied") or []),
                    "soft_signals_used": list(c.get("soft_signals_used") or []),
                }
                cur.execute(
                    """
                    INSERT INTO route_raw.relation_candidates
                      (route_id, osm_relation_id, rel_type, route_mode, ref, name, operator, tags)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (route_id, osm_relation_id)
                    DO UPDATE SET
                      rel_type = EXCLUDED.rel_type,
                      route_mode = EXCLUDED.route_mode,
                      ref = EXCLUDED.ref,
                      name = EXCLUDED.name,
                      operator = EXCLUDED.operator,
                      tags = EXCLUDED.tags,
                      found_at = now()
                    """,
                    (
                        str(route_id),
                        int(c["osm_relation_id"]),
                        str(c.get("rel_type") or ""),
                        str(c.get("route_mode") or ""),
                        c.get("ref"),
                        c.get("name"),
                        c.get("operator"),
                        json.dumps(tags_payload, ensure_ascii=False),
                    ),
                )
    except Exception as e:
        print(f"[WARN] Could not insert into route_raw.relation_candidates (schema mismatch?): {e}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("route_id_or_new", type=str, help="'new' or an existing route UUID")
    p.add_argument("--bbox", type=_parse_bbox, required=True, help="south,west,north,east")
    p.add_argument("--refs", type=str, default=None, help="comma-separated refs (e.g. E1,E2)")
    p.add_argument("--operator", type=str, default=None, help="operator contains (regex-like, Overpass ~ ... ,i)")
    p.add_argument("--name", type=str, default=None, help="name contains (regex-like, Overpass ~ ... ,i)")
    p.add_argument("--max-candidates", type=int, default=20, help="how many relations to score (default 20)")
    p.add_argument("--timeout-s", type=int, default=60, help="Overpass timeout seconds")
    p.add_argument(
        "--query-strategy",
        type=str,
        default="bbox_first_broad",
        help="bbox_first_broad (default) or metadata_filtered",
    )
    p.add_argument("--store", action="store_true", help="store candidates + chosen relation to DB (best effort)")
    p.add_argument(
        "--province",
        type=str,
        default=os.getenv("ROUTE_JOB_PROVINCE"),
        help="Province name (required when route_id_or_new == 'new'; Skill 11 §7). "
             "Falls back to env var ROUTE_JOB_PROVINCE.",
    )
    args = p.parse_args()

    bbox: BBox = args.bbox
    refs = _parse_refs(args.refs)
    query_strategy = normalize_query_strategy(args.query_strategy)
    soft_signals_used = [name for name, raw in (("refs", refs), ("operator", args.operator), ("name", args.name)) if raw]
    hard_filters_applied = soft_signals_used if query_strategy == "metadata_filtered" else []

    created_by = os.getenv("USER") or os.getenv("USERNAME") or "unknown"

    with db_conn() as conn:
        # 1) route_id
        if args.route_id_or_new == "new":
            if not args.province or not args.province.strip():
                raise SystemExit(
                    "--province <name> is required when route_id_or_new == 'new' "
                    "(Skill 11 §7). Pass --province or set ROUTE_JOB_PROVINCE env var."
                )
            route_id = create_route_job(
                conn,
                created_by=created_by,
                notes="created via 05_discover_relation.py",
                province=args.province,
            )
        else:
            route_id = uuid.UUID(args.route_id_or_new)

        # 2) search candidate relations (minimal objects)
        
        operator_variants: List[Optional[str]] = [None]
        if args.operator and query_strategy == "metadata_filtered":
            operator_variants = expand_operator_keys_or_aliases(args.operator)

        raw_cands: List[Dict[str, Any]] = []

        for op in operator_variants:
            raw_cands.extend(
                search_route_relations(
                    bbox,
                    refs=refs if query_strategy == "metadata_filtered" else None,
                    operator_contains=op if query_strategy == "metadata_filtered" else None,
                    name_contains=args.name if query_strategy == "metadata_filtered" else None,
                    query_strategy=query_strategy,
                    timeout_s=int(args.timeout_s),
                    limit=max(1, int(args.max_candidates)),
                )
            )

        # de-duplicate
        seen = set()
        raw_cands = [
            c for c in raw_cands
            if not (c["id"] in seen or seen.add(c["id"]))
        ]





        if not raw_cands:
            raise SystemExit("No route relations found for that bbox/filters.")

        # 3) fetch+score each candidate (stop prior count is the key signal)
        scored: List[Dict[str, Any]] = []
        for c in raw_cands[: int(args.max_candidates)]:
            rid = int(c["id"])
            tags = c.get("tags") or {}

            try:
                rel_json = fetch_relation_overpass_json(rid)
                prior = extract_stop_prior(rel_json)
                prior_count = len(prior)
            except Exception as e:
                # If fetch/parse fails, keep candidate but with poor score
                prior_count = 0
                print(f"[WARN] candidate relation {rid} failed fetch/parse: {e}")

            score_meta = _score_candidate(
                tags,
                prior_count,
                refs=refs,
                operator_contains=args.operator,
                name_contains=args.name,
            )

            scored.append(
                {
                    "osm_relation_id": rid,
                    "tags": tags,
                    "rel_type": str(tags.get("type") or ""),
                    "route_mode": str(tags.get("route") or ""),
                    "ref": tags.get("ref"),
                    "name": tags.get("name"),
                    "operator": tags.get("operator"),
                    "stop_prior_count": prior_count,
                    "score": float(score_meta["score"]),
                    "matched_soft_signals": list(score_meta["matched_soft_signals"]),
                    "selection_reason_codes": list(score_meta["selection_reason_codes"]),
                    "query_strategy": query_strategy,
                    "hard_filters_applied": list(hard_filters_applied),
                    "soft_signals_used": list(soft_signals_used),
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        second_score = float(scored[1]["score"]) if len(scored) > 1 else None
        score_gap_top2 = (
            float(scored[0]["score"]) - float(second_score)
            if second_score is not None
            else None
        )
        for idx, row in enumerate(scored, start=1):
            row["selection_rank"] = idx
            row["selection_confidence"] = _selection_confidence(
                score=float(row.get("score") or 0.0),
                stop_prior_count=int(row.get("stop_prior_count") or 0),
                matched_soft_signals=list(row.get("matched_soft_signals") or []),
                score_gap_top2=score_gap_top2 if idx == 1 else None,
                selection_rank=idx,
            )
        best = scored[0]
        chosen_id = int(best["osm_relation_id"])
        candidate_preview = [
            {
                "osm_relation_id": int(row["osm_relation_id"]),
                "selection_rank": int(row.get("selection_rank") or 0),
                "selection_confidence": row.get("selection_confidence"),
                "score": round(float(row.get("score") or 0.0), 4),
                "stop_prior_count": int(row.get("stop_prior_count") or 0),
                "ref": row.get("ref"),
                "name": row.get("name"),
                "operator": row.get("operator"),
                "route_mode": row.get("route_mode"),
                "matched_soft_signals": list(row.get("matched_soft_signals") or []),
                "selection_reason_codes": list(row.get("selection_reason_codes") or []),
            }
            for row in scored[:10]
        ]
        candidate_universe = {
            "query_strategy": query_strategy,
            "candidate_universe_count": int(len(scored)),
            "candidate_scored_count": int(len(scored)),
            "candidate_fetch_evaluated_count": int(len(scored)),
            "max_candidates_requested": int(max(1, int(args.max_candidates))),
            "hard_filters_applied": list(hard_filters_applied),
            "soft_signals_used": list(soft_signals_used),
            "operator_variants_attempted": int(len(operator_variants)),
            "selected_osm_relation_id": chosen_id,
            "top_stop_prior_count": int(best.get("stop_prior_count") or 0),
            "top_score": round(float(best.get("score") or 0.0), 4),
            "score_gap_top2": (round(float(score_gap_top2), 4) if score_gap_top2 is not None else None),
        }
        selection_summary = {
            "selection_status": "provisional_selected",
            "selected_osm_relation_id": chosen_id,
            "selected_rank": int(best.get("selection_rank") or 1),
            "selected_score": round(float(best.get("score") or 0.0), 4),
            "selection_confidence": best.get("selection_confidence"),
            "score_gap_top2": (round(float(score_gap_top2), 4) if score_gap_top2 is not None else None),
            "selected_relation_stop_prior_count": int(best.get("stop_prior_count") or 0),
            "selection_reason_codes": list(best.get("selection_reason_codes") or []),
        }

        # 4) optional DB store
        if args.store:
            _best_effort_store_candidates(conn, route_id=route_id, candidates=scored, chosen_id=chosen_id)
            _best_effort_update_route_job_discovery(
                conn,
                route_id=route_id,
                bbox=bbox,
                refs=refs,
                operator_contains=args.operator,
                name_contains=args.name,
                chosen_osm_relation_id=chosen_id,
            )

    print("route_id:", route_id)
    print("osm_relation_id:", chosen_id)
    print("top_candidate:", json.dumps(best, ensure_ascii=False))
    print("candidate_universe:", json.dumps(candidate_universe, ensure_ascii=False))
    print("selection_summary:", json.dumps(selection_summary, ensure_ascii=False))
    print("candidate_preview:", json.dumps(candidate_preview, ensure_ascii=False))


if __name__ == "__main__":
    main()
