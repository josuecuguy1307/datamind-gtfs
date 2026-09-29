# phase4_semantics/ingest/normalize_to_evidence.py
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus

import psycopg2
from psycopg2.extras import execute_values


# ============================================================
# Goal
# ============================================================
# Take ANY raw ingestion row (ArcGIS / Overpass / Docs / CSV)
# and normalize it into semantics.route_evidence_records
#
# IMPORTANT:
# - we store "best effort" normalized fields (name/ref/operator/from/to)
# - we store bbox (for candidate generation)
# - we store full raw_json for traceability
# - we compute stable keys to avoid duplicates
# ============================================================


# -----------------------------
# DB helpers
# -----------------------------

def _get_dsn() -> str:
    # prefer DB_DSN / DATABASE_URL
    dsn = os.getenv("DB_DSN") or os.getenv("DATABASE_URL") or os.getenv("PG_DSN")
    if not dsn and os.getenv("SUPABASE_DB_HOST"):
        host = os.getenv("SUPABASE_DB_HOST")
        port = os.getenv("SUPABASE_DB_PORT", "5432")
        name = os.getenv("SUPABASE_DB_NAME", "postgres")
        user = os.getenv("SUPABASE_DB_USER", "postgres")
        password = os.getenv("SUPABASE_DB_PASSWORD", "")
        dsn = (
            f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{name}"
            "?sslmode=require"
        )
    if not dsn:
        raise RuntimeError(
            "Missing DB_DSN/DATABASE_URL/PG_DSN or SUPABASE_DB_* in env."
        )
    return dsn


def get_conn():
    return psycopg2.connect(_get_dsn())


def _qall(cur, sql: str, args: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, args)
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        out.append({cols[i]: row[i] for i in range(len(cols))})
    return out


def _q1(cur, sql: str, args: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    rows = _qall(cur, sql, args)
    return rows[0] if rows else None


def _table_exists(cur, schema: str, table: str) -> bool:
    r = _q1(
        cur,
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema=%s AND table_name=%s
        """,
        (schema, table),
    )
    return bool(r)


def _columns(cur, schema: str, table: str) -> List[str]:
    rows = _qall(
        cur,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return [r["column_name"] for r in rows]


# -----------------------------
# Text helpers
# -----------------------------

def _clean_text(x: Any) -> Optional[str]:
    if x is None:
        return None
    s = str(x).strip()
    s = " ".join(s.split())
    return s if s else None


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _clip01(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return max(0.0, min(1.0, float(x)))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------
# Geometry bbox extractors
# -----------------------------

def _bbox_from_arcgis_geometry(geom: Any) -> Optional[Tuple[float, float, float, float]]:
    """
    Supports ArcGIS geometries (outSR=4326 recommended):
      Point: {"x": ..., "y": ...}
      Polyline: {"paths": [[[x,y],...], ...]}
      Polygon: {"rings": [[[x,y],...], ...]}
    Returns (minx, miny, maxx, maxy)
    """
    if not isinstance(geom, dict):
        return None

    pts: List[Tuple[float, float]] = []

    if "x" in geom and "y" in geom:
        x = _as_float(geom.get("x"))
        y = _as_float(geom.get("y"))
        if x is not None and y is not None:
            pts.append((x, y))

    if "paths" in geom and isinstance(geom["paths"], list):
        for path in geom["paths"]:
            if not isinstance(path, list):
                continue
            for xy in path:
                if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                    x = _as_float(xy[0])
                    y = _as_float(xy[1])
                    if x is not None and y is not None:
                        pts.append((x, y))

    if "rings" in geom and isinstance(geom["rings"], list):
        for ring in geom["rings"]:
            if not isinstance(ring, list):
                continue
            for xy in ring:
                if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                    x = _as_float(xy[0])
                    y = _as_float(xy[1])
                    if x is not None and y is not None:
                        pts.append((x, y))

    if not pts:
        return None

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_from_any(raw: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    # direct bbox
    bb = raw.get("bbox")
    if isinstance(bb, (list, tuple)) and len(bb) == 4:
        try:
            return (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]))
        except Exception:
            pass

    # arcgis raw_geometry
    if "raw_geometry" in raw:
        bb2 = _bbox_from_arcgis_geometry(raw.get("raw_geometry"))
        if bb2:
            return bb2

    # overpass sometimes might include bbox directly
    if "geometry_bbox" in raw:
        bb3 = raw.get("geometry_bbox")
        if isinstance(bb3, (list, tuple)) and len(bb3) == 4:
            try:
                return (float(bb3[0]), float(bb3[1]), float(bb3[2]), float(bb3[3]))
            except Exception:
                pass

    return None


# -----------------------------
# ID + hashing
# -----------------------------

def _sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _make_record_key(source_type: str, external_id: Optional[str]) -> str:
    """
    Stable key used for UPSERT.
    If external_id exists -> perfect.
    Else falls back to hashed content downstream.
    """
    st = (source_type or "unknown").strip().lower()
    ex = (external_id or "no_external_id").strip().lower()
    return f"{st}::{ex}"


def _make_evidence_hash(
    source_type: str,
    external_id: Optional[str],
    route_name: Optional[str],
    route_ref: Optional[str],
    operator_name: Optional[str],
    from_name: Optional[str],
    to_name: Optional[str],
    bbox: Optional[Tuple[float, float, float, float]],
) -> str:
    parts = [
        source_type or "",
        external_id or "",
        route_name or "",
        route_ref or "",
        operator_name or "",
        from_name or "",
        to_name or "",
        "" if bbox is None else ",".join([f"{x:.6f}" for x in bbox]),
    ]
    return _sha1_hex("|".join(parts))


# ============================================================
# Normalized Evidence Row (what we insert into DB)
# ============================================================

@dataclass
class EvidenceRow:
    source_type: str
    source_url: Optional[str]
    external_id: Optional[str]

    route_name: Optional[str]
    route_ref: Optional[str]
    operator_name: Optional[str]
    from_name: Optional[str]
    to_name: Optional[str]

    bbox: Optional[Tuple[float, float, float, float]]
    confidence_hint: Optional[float]

    record_key: str
    evidence_hash: str

    raw_json: Dict[str, Any]
    created_at: datetime


def normalize_raw_to_evidence(raw: Dict[str, Any]) -> EvidenceRow:
    """
    Raw record formats supported:

    ArcGIS fetch_arcgis.py output:
      {
        "source_type": "official_gis_arcgis",
        "source_url": ".../FeatureServer",
        "layer_id": "...",
        "raw_attributes": {...},
        "raw_geometry": {...},
        "hint_route_name": "...",
        "hint_route_ref": "...",
        "hint_operator": "...",
        "hint_from": "...",
        "hint_to": "..."
      }

    Overpass seed (recommended structure):
      {
        "source_type": "osm_overpass_seed",
        "relation_id": 1234,
        "tags": {"name": "...", "ref": "...", ...},
        "confidence_hint": 0.88,
        "bbox": [...],
        ...
      }

    Docs/Excel structured:
      {
        "source_type": "docs_pdf",
        "doc_id": "...",
        "row_index": 12,
        "fields": {"route_name": "...", "operator": "...", ...},
        "confidence_hint": 0.7,
        ...
      }
    """
    source_type = _clean_text(raw.get("source_type")) or "unknown"
    source_url = _clean_text(raw.get("source_url"))

    # -------- external_id (try best effort) --------
    external_id = (
        _clean_text(raw.get("external_id"))
        or _clean_text(raw.get("relation_id"))
        or _clean_text(raw.get("objectid"))
    )

    # ArcGIS: look inside raw_attributes for OBJECTID / id keys
    attrs = raw.get("raw_attributes") or {}
    if external_id is None and isinstance(attrs, dict):
        external_id = (
            _clean_text(attrs.get("OBJECTID"))
            or _clean_text(attrs.get("objectid"))
            or _clean_text(attrs.get("id"))
        )

    # Docs: doc row id fallback
    if external_id is None:
        doc_id = _clean_text(raw.get("doc_id"))
        row_idx = raw.get("row_index")
        if doc_id and row_idx is not None:
            external_id = f"{doc_id}::row::{row_idx}"

    # If STILL missing, use content hash so record_key becomes stable
    if external_id is None:
        external_id = _sha1_hex(json.dumps(raw, sort_keys=True, ensure_ascii=False))[:16]

    # -------- fields (name/ref/operator/from/to) --------
    # ArcGIS hints
    route_name = _clean_text(raw.get("hint_route_name"))
    route_ref = _clean_text(raw.get("hint_route_ref"))
    operator_name = _clean_text(raw.get("hint_operator"))
    from_name = _clean_text(raw.get("hint_from"))
    to_name = _clean_text(raw.get("hint_to"))

    # Overpass tags
    tags = raw.get("tags") or {}
    if isinstance(tags, dict):
        route_name = route_name or _clean_text(tags.get("name"))
        route_ref = route_ref or _clean_text(tags.get("ref"))
        operator_name = operator_name or _clean_text(tags.get("operator")) or _clean_text(tags.get("network"))
        from_name = from_name or _clean_text(tags.get("from"))
        to_name = to_name or _clean_text(tags.get("to"))

    # Docs structured fields
    fields = raw.get("fields") or {}
    if isinstance(fields, dict):
        route_name = route_name or _clean_text(fields.get("route_name")) or _clean_text(fields.get("name"))
        route_ref = route_ref or _clean_text(fields.get("route_ref")) or _clean_text(fields.get("ref"))
        operator_name = operator_name or _clean_text(fields.get("operator")) or _clean_text(fields.get("cooperativa"))
        from_name = from_name or _clean_text(fields.get("from")) or _clean_text(fields.get("origen"))
        to_name = to_name or _clean_text(fields.get("to")) or _clean_text(fields.get("destino"))

    # -------- confidence --------
    confidence_hint = _clip01(_as_float(raw.get("confidence_hint") or raw.get("confidence")))

    # -------- bbox --------
    bbox = _bbox_from_any(raw)

    # -------- stable keys --------
    record_key = _make_record_key(source_type, external_id)
    evidence_hash = _make_evidence_hash(
        source_type=source_type,
        external_id=external_id,
        route_name=route_name,
        route_ref=route_ref,
        operator_name=operator_name,
        from_name=from_name,
        to_name=to_name,
        bbox=bbox,
    )

    return EvidenceRow(
        source_type=source_type,
        source_url=source_url,
        external_id=external_id,
        route_name=route_name,
        route_ref=route_ref,
        operator_name=operator_name,
        from_name=from_name,
        to_name=to_name,
        bbox=bbox,
        confidence_hint=confidence_hint,
        record_key=record_key,
        evidence_hash=evidence_hash,
        raw_json=raw,
        created_at=_utc_now(),
    )


# ============================================================
# Insert into DB
# ============================================================

def _evidence_to_row_dict(ev: EvidenceRow) -> Dict[str, Any]:
    return {
        "source_type": ev.source_type,
        "source_url": ev.source_url,
        "external_id": ev.external_id,
        "record_key": ev.record_key,
        "evidence_hash": ev.evidence_hash,

        "route_name": ev.route_name,
        "route_ref": ev.route_ref,
        "operator_name": ev.operator_name,
        "from_name": ev.from_name,
        "to_name": ev.to_name,

        # store bbox as array if table expects it OR as json
        "bbox": list(ev.bbox) if ev.bbox is not None else None,
        "confidence_hint": ev.confidence_hint,

        "raw_json": json.dumps(ev.raw_json, ensure_ascii=False),
        "created_at": ev.created_at,
    }


def insert_evidence_rows(conn, evidence_rows: List[EvidenceRow]) -> int:
    """
    Bulk inserts into semantics.route_evidence_records.
    Auto-adapts to existing columns.
    Uses UPSERT if record_key OR evidence_hash column exists.
    """
    if not evidence_rows:
        return 0

    with conn.cursor() as cur:
        if not _table_exists(cur, "semantics", "route_evidence_records"):
            raise RuntimeError("Missing table semantics.route_evidence_records. Run V4__create_semantics first.")

        cols = _columns(cur, "semantics", "route_evidence_records")
        colset = set(cols)

        rows_dicts = [_evidence_to_row_dict(ev) for ev in evidence_rows]

        # Only keep keys that exist in DB
        insert_cols = [c for c in rows_dicts[0].keys() if c in colset]
        if not insert_cols:
            raise RuntimeError("No matching insert columns found in semantics.route_evidence_records.")

        values = [[r.get(c) for c in insert_cols] for r in rows_dicts]

        # Choose conflict target
        conflict_col = None
        if "record_key" in colset:
            conflict_col = "record_key"
        elif "evidence_hash" in colset:
            conflict_col = "evidence_hash"

        if conflict_col:
            # Upsert: update main semantic fields if re-ingested
            update_cols = [c for c in insert_cols if c not in (conflict_col,)]
            set_clause = ", ".join([f"{c}=EXCLUDED.{c}" for c in update_cols])
            sql = f"""
                INSERT INTO semantics.route_evidence_records ({",".join(insert_cols)})
                VALUES %s
                ON CONFLICT ({conflict_col})
                DO UPDATE SET {set_clause}
            """
        else:
            sql = f"""
                INSERT INTO semantics.route_evidence_records ({",".join(insert_cols)})
                VALUES %s
            """

        execute_values(cur, sql, values, page_size=500)
        return len(evidence_rows)


# ============================================================
# IO (JSONL)
# ============================================================

def load_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def dump_preview(evs: List[EvidenceRow], n: int = 5) -> None:
    print("\n--- Preview normalized evidence rows ---")
    for i, ev in enumerate(evs[:n]):
        print(f"\n[{i+1}] record_key={ev.record_key}")
        print(f"  source_type={ev.source_type}")
        print(f"  external_id={ev.external_id}")
        print(f"  route_name={ev.route_name}")
        print(f"  route_ref={ev.route_ref}")
        print(f"  operator={ev.operator_name}")
        print(f"  from={ev.from_name}  to={ev.to_name}")
        print(f"  bbox={ev.bbox}")
        print(f"  confidence_hint={ev.confidence_hint}")
        print(f"  evidence_hash={ev.evidence_hash}")


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="Normalize raw ingestion JSONL -> semantics.route_evidence_records")
    ap.add_argument("--input", required=True, help="Input JSONL file (raw ingestion)")
    ap.add_argument("--dry-run", action="store_true", help="Normalize + preview only (no DB write)")
    ap.add_argument("--limit", type=int, default=None, help="Limit rows processed")

    args = ap.parse_args()

    raw_rows = load_jsonl(args.input)
    if args.limit is not None:
        raw_rows = raw_rows[: args.limit]

    evs = [normalize_raw_to_evidence(r) for r in raw_rows]
    dump_preview(evs, n=5)

    if args.dry_run:
        print("\n✅ Dry run: no DB writes.\n")
        return

    conn = get_conn()
    try:
        inserted = insert_evidence_rows(conn, evs)
        conn.commit()
        print(f"\n✅ Inserted/Upserted evidence rows: {inserted}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
