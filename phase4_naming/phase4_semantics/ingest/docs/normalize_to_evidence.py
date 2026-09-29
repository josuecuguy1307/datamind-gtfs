# phase4_semantics/ingest/docs/normalize_to_evidence.py
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import quote_plus

import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values

from ...common.models import EvidenceRecord
from ...common.text import (
    ascii_fold,
    join_nonempty,
    lower_clean,
    normalize_ws,
    uniq_keep_order,
)

# ------------------------------------------------------------
# Source trust map (very important)
# ------------------------------------------------------------
SOURCE_TRUST: Dict[str, float] = {
    "arcgis_layer": 0.92,       # official-ish GIS layers
    "gtfs": 0.90,
    "pdf_table": 0.75,          # depends a lot on the PDF quality
    "excel_sheet": 0.80,
    "osm_overpass_seed": 0.78,  # good seed, but OSM can be incomplete/noisy
    "osm_manual": 0.85,
    "unknown": 0.60,
}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def source_weight(source_type: Optional[str], confidence_hint: Optional[float]) -> float:
    """
    Mathematical idea:
    - We treat each evidence source as having a prior trust level (SOURCE_TRUST).
    - The confidence_hint is like a likelihood proxy (extraction quality).
    - We fuse both by weighted averaging.

    weight = 0.6 * trust_prior + 0.4 * confidence_hint
    """
    st = (source_type or "unknown").strip()
    trust = SOURCE_TRUST.get(st, SOURCE_TRUST["unknown"])
    ch = 0.5 if confidence_hint is None else _clamp01(confidence_hint)
    return 0.6 * trust + 0.4 * ch


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------
# Normalization core
# ------------------------------------------------------------
def normalize_doc_row_to_evidence(
    row: Mapping[str, Any],
    *,
    doc_id: str,
    row_index: int,
    source_url: Optional[str] = None,
    source_title: Optional[str] = None,
    confidence_hint: float = 0.70,
) -> EvidenceRecord:
    """
    Input row can be messy (coming from OpenAI PDF extraction).
    We normalize it into (extracted + normalized).
    """

    # ---- pull best-effort fields
    route_name = normalize_ws(row.get("route_name") or row.get("name") or row.get("route"))
    route_ref = normalize_ws(row.get("route_ref") or row.get("ref") or row.get("code") or row.get("line"))
    operator = normalize_ws(row.get("operator") or row.get("company") or row.get("cooperative"))
    from_name = normalize_ws(row.get("from") or row.get("origin") or row.get("start"))
    to_name = normalize_ws(row.get("to") or row.get("destination") or row.get("end"))
    direction = normalize_ws(row.get("direction") or row.get("direction_text"))

    aliases = row.get("aliases") or row.get("route_aliases") or []
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(",") if a.strip()]
    aliases = [normalize_ws(a) for a in aliases if normalize_ws(a)]

    stops = row.get("stops") or row.get("stop_names") or []
    if isinstance(stops, str):
        # sometimes extracted as "A;B;C" or "A, B, C"
        sep = ";" if ";" in stops else ","
        stops = [s.strip() for s in stops.split(sep)]
    stops = [normalize_ws(s) for s in stops if normalize_ws(s)]

    notes = normalize_ws(row.get("notes") or row.get("comment") or row.get("remarks"))

    # ---- Build candidate names (VERY IMPORTANT for matching)
    name_candidates: List[str] = []
    if route_name:
        name_candidates.append(route_name)

    if route_ref and route_name:
        name_candidates.append(f"{route_ref} {route_name}")

    if from_name and to_name:
        name_candidates.append(f"{from_name} - {to_name}")
        name_candidates.append(f"{to_name} - {from_name}")  # reverse

    if route_ref and from_name and to_name:
        name_candidates.append(f"{route_ref} {from_name} - {to_name}")

    name_candidates = uniq_keep_order([normalize_ws(x) for x in name_candidates if normalize_ws(x)])

    # ---- extracted = clean but close to source
    extracted: Dict[str, Any] = {
        "route_name": route_name,
        "route_ref": route_ref,
        "operator": operator,
        "from": from_name,
        "to": to_name,
        "direction_text": direction,
        "aliases": aliases,
        "stops": stops,
        "notes": notes,
    }

    # ---- normalized = Phase4 matching keys (model-friendly)
    # add both original + ascii-folded variants
    folded_names = [ascii_fold(x) for x in name_candidates if ascii_fold(x)]
    folded_stops = [ascii_fold(x) for x in stops if ascii_fold(x)]

    # key strings used for trigram/fts candidate generation later
    match_keys = uniq_keep_order([
        *(name_candidates or []),
        *(folded_names or []),
        *(aliases or []),
        *(stops[:12] or []),        # cap a bit
        *(folded_stops[:12] or []),
        normalize_ws(route_ref) or "",
        ascii_fold(route_ref) or "",
        normalize_ws(operator) or "",
        ascii_fold(operator) or "",
    ])
    match_keys = [k for k in match_keys if k]

    search_text = join_nonempty([
        route_name,
        route_ref,
        operator,
        from_name,
        to_name,
        direction,
        " ".join(aliases[:10]),
        " ".join(stops[:15]),
    ])

    normalized: Dict[str, Any] = {
        "name_candidates": name_candidates,
        "ref_candidates": [route_ref] if route_ref else [],
        "operator_candidates": [operator] if operator else [],
        "from_to": {"from": from_name, "to": to_name},
        "direction_text": direction,
        "stop_names": stops,
        "match_keys": match_keys,   # used by matching module
        "search_text": search_text, # used by UI/search
    }

    # stable id per row
    source_type = "pdf_table"
    source_id = f"{doc_id}:{row_index}"

    # make title helpful in admin UI
    title = normalize_ws(source_title) or f"Doc {doc_id} row {row_index}"

    er = EvidenceRecord(
        source_type=source_type,
        source_id=source_id,
        source_url=source_url,
        title=title,
        confidence_hint=_clamp01(confidence_hint),
        raw_payload=dict(row),
        extracted=extracted,
        normalized=normalized,
    )
    return er


def normalize_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    doc_id: str,
    source_url: Optional[str] = None,
    source_title: Optional[str] = None,
    confidence_hint: float = 0.70,
) -> List[EvidenceRecord]:
    out: List[EvidenceRecord] = []
    for i, r in enumerate(rows):
        out.append(
            normalize_doc_row_to_evidence(
                r,
                doc_id=doc_id,
                row_index=i,
                source_url=source_url,
                source_title=source_title,
                confidence_hint=confidence_hint,
            )
        )
    return out


# ------------------------------------------------------------
# DB writing (upsert)
# ------------------------------------------------------------
def upsert_evidence_records(conn, records: List[EvidenceRecord]) -> List[EvidenceRecord]:
    """
    Inserts/updates into:
      semantics.route_evidence_records

    Required DB columns (expected from V4 migration):
      record_id uuid PK default gen_random_uuid()
      source_type text
      source_id text
      source_url text
      title text
      confidence_hint float8
      raw_payload jsonb
      extracted jsonb
      normalized jsonb
      created_at timestamptz
      updated_at timestamptz

    Required unique:
      UNIQUE(source_type, source_id)
    """

    if not records:
        return []

    sql = """
    INSERT INTO semantics.route_evidence_records
    (source_type, source_id, source_url, title, confidence_hint, raw_payload, extracted, normalized, created_at, updated_at)
    VALUES %s
    ON CONFLICT (source_type, source_id)
    DO UPDATE SET
      source_url       = EXCLUDED.source_url,
      title            = COALESCE(EXCLUDED.title, semantics.route_evidence_records.title),
      confidence_hint  = GREATEST(semantics.route_evidence_records.confidence_hint, EXCLUDED.confidence_hint),
      raw_payload      = EXCLUDED.raw_payload,
      extracted        = EXCLUDED.extracted,
      normalized       = EXCLUDED.normalized,
      updated_at       = now()
    RETURNING record_id, source_type, source_id;
    """

    rows = [
        (
            r.source_type,
            r.source_id,
            r.source_url,
            r.title,
            float(r.confidence_hint),
            Json(r.raw_payload),
            Json(r.extracted),
            Json(r.normalized),
            "now()",
            "now()",
        )
        for r in records
    ]

    # psycopg2 execute_values cannot inject now() if we pass it as string,
    # so we put timestamps directly in SQL.
    # We'll use a values template and set created_at/updated_at in SQL.
    sql = """
    INSERT INTO semantics.route_evidence_records
    (source_type, source_id, source_url, title, confidence_hint, raw_payload, extracted, normalized, created_at, updated_at)
    VALUES %s
    ON CONFLICT (source_type, source_id)
    DO UPDATE SET
      source_url       = EXCLUDED.source_url,
      title            = COALESCE(EXCLUDED.title, semantics.route_evidence_records.title),
      confidence_hint  = GREATEST(semantics.route_evidence_records.confidence_hint, EXCLUDED.confidence_hint),
      raw_payload      = EXCLUDED.raw_payload,
      extracted        = EXCLUDED.extracted,
      normalized       = EXCLUDED.normalized,
      updated_at       = now()
    RETURNING record_id, source_type, source_id;
    """

    values = [
        (
            r.source_type,
            r.source_id,
            r.source_url,
            r.title,
            float(r.confidence_hint),
            Json(r.raw_payload),
            Json(r.extracted),
            Json(r.normalized),
            # timestamps:
            # created_at + updated_at -> now()
            # but in VALUES we still need placeholders
            # easiest: pass now() by setting them in query below
            # (we will replace them using template)
        )
        for r in records
    ]

    # Template with now()
    template = "(%s,%s,%s,%s,%s,%s,%s,%s,now(),now())"

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        execute_values(cur, sql, values, template=template, page_size=200)
        returned = cur.fetchall()

    # Map returned record_id back
    idx = {(x["source_type"], x["source_id"]): x["record_id"] for x in returned}
    for r in records:
        r.record_id = idx.get((r.source_type, r.source_id))
    return records


# ------------------------------------------------------------
# Convenience runner (optional)
# ------------------------------------------------------------
def normalize_json_file_to_db(
    json_path: str,
    *,
    doc_id: str,
    dsn: Optional[str] = None,
    source_url: Optional[str] = None,
    source_title: Optional[str] = None,
    confidence_hint: float = 0.70,
) -> int:
    """
    If your extract_structured.py produced JSON rows, you can push them to DB.
    """
    with open(json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    if isinstance(payload, dict) and "rows" in payload:
        rows = payload["rows"]
    else:
        rows = payload

    recs = normalize_rows(
        rows,
        doc_id=doc_id,
        source_url=source_url,
        source_title=source_title,
        confidence_hint=confidence_hint,
    )

    dsn_eff = dsn or os.getenv("DB_DSN") or os.getenv("DATABASE_URL") or os.getenv("PG_DSN")
    if not dsn_eff and os.getenv("SUPABASE_DB_HOST"):
        host = os.getenv("SUPABASE_DB_HOST")
        port = os.getenv("SUPABASE_DB_PORT", "5432")
        name = os.getenv("SUPABASE_DB_NAME", "postgres")
        user = os.getenv("SUPABASE_DB_USER", "postgres")
        password = os.getenv("SUPABASE_DB_PASSWORD", "")
        dsn_eff = (
            f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
            "?sslmode=require"
        )
    if not dsn_eff:
        raise RuntimeError(
            "Missing DB DSN. Pass dsn=... or set DB_DSN/DATABASE_URL/PG_DSN or SUPABASE_DB_*."
        )

    conn = psycopg2.connect(dsn_eff)
    try:
        upsert_evidence_records(conn, recs)
        conn.commit()
    finally:
        conn.close()

    return len(recs)
