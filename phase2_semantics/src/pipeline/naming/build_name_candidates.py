from __future__ import annotations

from collections import Counter
import re
from typing import Any, Dict, Optional

from psycopg2.extras import Json, execute_values

from src.pipeline.normalize.normalize_alias import normalize_alias
from src.pipeline.naming.name_features import compute_name_features, heuristic_score
from src.pipeline.naming.name_ranker import score as rank_score


def _table_exists(conn, fq_table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (fq_table,))
        row = cur.fetchone() or {}
    return bool((row.get("ok") if isinstance(row, dict) else row[0]))


def _pick_source(candidate_name: str, proposed_name: str, alias_set: set[str]) -> str:
    t = (candidate_name or "").strip()
    if t == (proposed_name or "").strip():
        return "generated"
    if t in alias_set:
        return "alias"
    return "tag"


_UNKNOWN_NORMS = {
    "sin nombre",
    "unknown",
    "unnamed",
    "no name",
    "s n",
    "na",
    "n a",
}
_LOWER_CONNECTORS = {"de", "del", "la", "las", "el", "los", "y", "a", "al", "en"}


def _is_unknownish(raw: str) -> bool:
    norm = normalize_alias(raw) or ""
    if not norm:
        return True
    compact = norm.replace(" ", "")
    if compact.isdigit():
        return True
    if norm in _UNKNOWN_NORMS:
        return True
    low = str(raw or "").strip().lower()
    if low.startswith("node_") or low.startswith("node-") or low.startswith("node "):
        return True
    return False


def _title_case_name(raw: str) -> str:
    txt = re.sub(r"\s+", " ", str(raw or "").strip())
    if not txt:
        return ""
    parts = txt.split(" ")
    out: list[str] = []
    for i, p in enumerate(parts):
        if not p:
            continue
        low = p.lower()
        if i > 0 and low in _LOWER_CONNECTORS:
            out.append(low)
            continue
        if p.isupper() and p.isalpha() and len(p) <= 4:
            # Keep short acronyms like AKI, UIO.
            out.append(p)
            continue
        out.append(p[:1].upper() + p[1:].lower())
    return " ".join(out).strip()


def _normalize_display_candidate(raw: str) -> str:
    txt = re.sub(r"\s+", " ", str(raw or "").strip())
    if not txt:
        return "(sin nombre)"
    if _is_unknownish(txt):
        return "(sin nombre)"
    return _title_case_name(txt) or "(sin nombre)"


def build_for_place_set(
    conn,
    *,
    place_set_id: str,
    ranker_artifact: Optional[Dict[str, Any]] = None,
) -> Dict[str, int]:
    if not _table_exists(conn, "geo_work.place_name_candidates"):
        return {"places": 0, "name_candidates": 0}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              pc.place_candidate_id::text AS place_candidate_id,
              pc.place_set_id::text AS place_set_id,
              pc.proposed_canonical_name,
              pc.provenance,
              COALESCE(array_agg(ac.alias) FILTER (WHERE ac.alias IS NOT NULL), ARRAY[]::text[]) AS aliases
            FROM geo_work.place_candidates pc
            LEFT JOIN geo_work.alias_candidates ac
              ON ac.place_candidate_id = pc.place_candidate_id
            WHERE pc.place_set_id::text = %s
            GROUP BY pc.place_candidate_id
            """,
            (place_set_id,),
        )
        rows = list(cur.fetchall() or [])

    if not rows:
        return {"places": 0, "name_candidates": 0}

    payload_rows = []
    total = 0
    for row in rows:
        pc_id = str(row["place_candidate_id"])
        proposed = _normalize_display_candidate(str(row.get("proposed_canonical_name") or ""))
        aliases = [_normalize_display_candidate(str(a)) for a in list(row.get("aliases") or []) if str(a).strip()]
        alias_set = set(aliases)
        bag = [proposed] + aliases

        prov = row.get("provenance") or {}
        node_tags = (prov.get("tags_snapshot") or {}) if isinstance(prov, dict) else {}
        for k in ("official_name", "name:es", "name", "short_name", "alt_name", "ref", "operator", "network"):
            v = node_tags.get(k)
            if isinstance(v, str) and v.strip():
                bag.append(_normalize_display_candidate(v.strip()))

        freq = Counter([x for x in bag if x])
        rank_items = []
        for cand, cnt in freq.items():
            norm = normalize_alias(cand)
            if not norm:
                continue
            source_kind = _pick_source(cand, proposed, alias_set)
            feats = compute_name_features(candidate_name=cand, source_kind=source_kind, freq=int(cnt))
            base = heuristic_score(feats)
            model = rank_score(feats, ranker_artifact) if isinstance(ranker_artifact, dict) else base
            rank_items.append((cand, norm, source_kind, feats, base, model, int(cnt)))

        # If there is at least one informative name, drop "(sin nombre)" placeholders from ranking.
        informative = [it for it in rank_items if not _is_unknownish(str(it[0] or ""))]
        if informative:
            rank_items = informative

        rank_items.sort(key=lambda x: (float(x[5]), float(x[4]), float(x[6])), reverse=True)
        # Different raw strings can collapse to the same normalized alias.
        # Keep only the best-scored row per normalized value.
        seen_norms: set[str] = set()
        deduped_rank_items = []
        for item in rank_items:
            norm = str(item[1])
            if norm in seen_norms:
                continue
            seen_norms.add(norm)
            deduped_rank_items.append(item)

        for i, (cand, norm, source_kind, feats, base, model, cnt) in enumerate(deduped_rank_items, start=1):
            payload_rows.append(
                (
                    place_set_id,
                    pc_id,
                    cand,
                    norm,
                    source_kind,
                    float(base),
                    float(model),
                    int(i),
                    bool(i == 1),
                    Json(feats),
                    Json({"freq": cnt}),
                )
            )
            total += 1

    with conn.cursor() as cur:
        cur.execute("DELETE FROM geo_work.place_name_candidates WHERE place_set_id::text = %s", (place_set_id,))
        if payload_rows:
            execute_values(
                cur,
                """
                INSERT INTO geo_work.place_name_candidates
                  (place_set_id, place_candidate_id, candidate_name, candidate_name_norm,
                   source_kind, score, model_score, model_rank, selected_by_model, features, provenance)
                VALUES %s
                """,
                payload_rows,
            )

    return {"places": len(rows), "name_candidates": total}
