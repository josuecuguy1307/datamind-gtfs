# phase4_semantics/api/server.py
import os
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

import psycopg2
from psycopg2.extras import RealDictCursor

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ------------------------------------------------------------
# Config
# ------------------------------------------------------------

def _get_db_dsn() -> str:
    """
    Preferred:
      export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    Fallback:
      export PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD
    """
    dsn = os.getenv("DB_DSN") or os.getenv("DATABASE_URL")
    if dsn:
        return dsn

    supabase_host = os.getenv("SUPABASE_DB_HOST")
    if supabase_host:
        supabase_port = os.getenv("SUPABASE_DB_PORT", "5432")
        supabase_db = os.getenv("SUPABASE_DB_NAME", "postgres")
        supabase_user = os.getenv("SUPABASE_DB_USER", "postgres")
        supabase_pw = os.getenv("SUPABASE_DB_PASSWORD", "")
        return (
            f"postgresql://{quote_plus(supabase_user)}:{quote_plus(supabase_pw)}"
            f"@{supabase_host}:{supabase_port}/{supabase_db}?sslmode=require"
        )

    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    db = os.getenv("PGDATABASE", "postgres")
    user = os.getenv("PGUSER", "postgres")
    pw = os.getenv("PGPASSWORD", "")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def _db_conn():
    return psycopg2.connect(_get_db_dsn())


def _qall(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with _db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]


def _q1(sql: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
    rows = _qall(sql, params)
    return rows[0] if rows else None


def _exec(sql: str, params: tuple = ()) -> None:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


# ------------------------------------------------------------
# API models
# ------------------------------------------------------------

class EvidenceCreateIn(BaseModel):
    source_type: str = Field(..., examples=["osm_overpass_seed", "pdf_extract", "gis_layer", "manual"])
    source_id: str = Field(..., examples=["relation/123456", "pdf:2026_01_routes_table"])
    route_id_hint: Optional[str] = Field(None, description="UUID string for Phase3 route_id if known")
    route_ref: Optional[str] = None
    route_name: Optional[str] = None
    operator_name: Optional[str] = None
    from_name: Optional[str] = None
    to_name: Optional[str] = None
    via: Optional[List[str]] = None
    confidence_hint: float = 0.5
    raw: Dict[str, Any] = Field(default_factory=dict)


class EvidenceCreateOut(BaseModel):
    record_id: str
    created_at: str


class LabelCreateIn(BaseModel):
    record_id: str
    route_id: str
    relevance: int = Field(2, ge=0, le=2)
    notes: Optional[str] = None


class LabelCreateOut(BaseModel):
    ok: bool
    message: str


# ------------------------------------------------------------
# FastAPI app
# ------------------------------------------------------------

app = FastAPI(
    title="Phase 4 Semantics API",
    version="0.1.0",
)

if load_dotenv:
    load_dotenv()

# CORS: solo la consola local. Antes era allow_origins=["*"] con credenciales,
# lo que dejaba a cualquier página web abierta en el navegador llamar a esta API.
# Otros orígenes: PHASE4_CORS_ORIGINS="https://a.example,https://b.example".
_CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "PHASE4_CORS_ORIGINS", "http://127.0.0.1:8501,http://localhost:8501"
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """
    Los endpoints que ESCRIBEN (POST) exigen el header X-API-Key == PHASE4_API_KEY.
    Falla cerrado: sin PHASE4_API_KEY configurada responde 503, nunca deja pasar.
    """
    expected = os.getenv("PHASE4_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="PHASE4_API_KEY no configurada")
    if not x_api_key or not secrets.compare_digest(
        x_api_key.encode(), expected.encode()
    ):
        raise HTTPException(status_code=401, detail="API key inválida")


# ------------------------------------------------------------
# Health
# ------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True, "service": "phase4_semantics_api"}


# ------------------------------------------------------------
# Routes: pending + search
# ------------------------------------------------------------

@app.get("/routes/pending")
def get_routes_pending(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """
    Returns routes that are not human_verified yet.
    Uses semantics.v_routes_pending (created by V4_1 migration).
    """
    sql = """
    SELECT *
    FROM semantics.v_routes_pending
    ORDER BY updated_at DESC NULLS LAST
    LIMIT %s OFFSET %s;
    """
    rows = _qall(sql, (limit, offset))
    return {"count": len(rows), "rows": rows}


@app.get("/routes/search")
def search_routes(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    Fast search using stored tsvector column route_prod.routes.search_tsv.
    (This exists after the fixed V4_1 migration)
    """
    sql = """
    SELECT
      r.route_id,
      r.route_name,
      r.route_aliases,
      r.landmark_tags,
      r.naming_confidence,
      r.human_verified,
      r.semantics_updated_at
    FROM route_prod.routes r
    WHERE r.search_tsv @@ plainto_tsquery('simple'::regconfig, %s)
    ORDER BY
      ts_rank(r.search_tsv, plainto_tsquery('simple'::regconfig, %s)) DESC,
      r.updated_at DESC NULLS LAST
    LIMIT %s OFFSET %s;
    """
    rows = _qall(sql, (q, q, limit, offset))
    return {"q": q, "count": len(rows), "rows": rows}


# ------------------------------------------------------------
# Evidence: create + list
# ------------------------------------------------------------

@app.post("/evidence/create", response_model=EvidenceCreateOut, dependencies=[Depends(require_api_key)])
def create_evidence(payload: EvidenceCreateIn):
    """
    Inserts a normalized evidence record into semantics.route_evidence_records.

    This is used by:
    - Overpass seed normalization
    - PDF extraction normalization
    - GIS ingestion normalization
    - Manual entry from Admin UI
    """
    sql = """
    INSERT INTO semantics.route_evidence_records (
      source_type, source_id,
      route_id_hint,
      route_ref, route_name, operator_name, from_name, to_name, via,
      confidence_hint, raw
    )
    VALUES (
      %s, %s,
      %s,
      %s, %s, %s, %s, %s, %s,
      %s, %s::jsonb
    )
    ON CONFLICT (source_type, source_id)
    DO UPDATE SET
      route_id_hint = EXCLUDED.route_id_hint,
      route_ref = EXCLUDED.route_ref,
      route_name = EXCLUDED.route_name,
      operator_name = EXCLUDED.operator_name,
      from_name = EXCLUDED.from_name,
      to_name = EXCLUDED.to_name,
      via = EXCLUDED.via,
      confidence_hint = EXCLUDED.confidence_hint,
      raw = EXCLUDED.raw
    RETURNING record_id::text, created_at::text;
    """
    row = _q1(
        sql,
        (
            payload.source_type,
            payload.source_id,
            payload.route_id_hint,
            payload.route_ref,
            payload.route_name,
            payload.operator_name,
            payload.from_name,
            payload.to_name,
            payload.via,
            payload.confidence_hint,
            str(payload.raw).replace("'", '"'),  # safe-enough for now; we’ll switch to json.dumps next file
        ),
    )

    if not row:
        raise HTTPException(status_code=500, detail="Failed to insert evidence record")

    return EvidenceCreateOut(record_id=row["record_id"], created_at=row["created_at"])


@app.get("/evidence/by_route/{route_id}")
def list_evidence_for_route(route_id: str):
    """
    List evidence records that have route_id_hint = route_id.
    """
    sql = """
    SELECT
      record_id::text,
      source_type,
      source_id,
      route_ref,
      route_name,
      operator_name,
      from_name,
      to_name,
      via,
      confidence_hint,
      created_at
    FROM semantics.route_evidence_records
    WHERE route_id_hint = %s::uuid
    ORDER BY created_at DESC;
    """
    rows = _qall(sql, (route_id,))
    return {"route_id": route_id, "count": len(rows), "rows": rows}


# ------------------------------------------------------------
# Labels (for LightGBM training)
# ------------------------------------------------------------

@app.post("/ranker/label", response_model=LabelCreateOut, dependencies=[Depends(require_api_key)])
def create_label(payload: LabelCreateIn):
    """
    Save training label for LambdaRank.

    When admin confirms the correct match:
      relevance=2 for (record_id, route_id)
    """
    sql = """
    INSERT INTO semantics.match_labels (record_id, route_id, relevance, notes)
    VALUES (%s::uuid, %s::uuid, %s, %s)
    ON CONFLICT (record_id, route_id)
    DO UPDATE SET relevance = EXCLUDED.relevance, notes = EXCLUDED.notes, created_at = now();
    """
    _exec(sql, (payload.record_id, payload.route_id, payload.relevance, payload.notes))
    return LabelCreateOut(ok=True, message="Label saved.")


# ------------------------------------------------------------
# Hooks (we implement next)
# ------------------------------------------------------------

@app.post("/seed/overpass/{route_id}", dependencies=[Depends(require_api_key)])
def seed_overpass(route_id: str):
    """
    Hook:
      route_prod.routes -> sample_points -> overpass candidates -> intersections -> evidence record

    We will implement it in:
      phase4_semantics/ingest/overpass/seed_candidates.py

    For now returns stub.
    """
    return {
        "ok": False,
        "route_id": route_id,
        "message": "Not implemented yet. Next step: implement Overpass seed pipeline."
    }


@app.post("/ranker/train", dependencies=[Depends(require_api_key)])
def train_ranker():
    """
    Hook: Train LightGBM LambdaRank using semantics.match_labels.
    Will be implemented in:
      phase4_semantics/ml/train_ranker.py
    """
    return {"ok": False, "message": "Not implemented yet. Next step: implement LightGBM training pipeline."}


@app.post("/ranker/infer/{record_id}", dependencies=[Depends(require_api_key)])
def infer_ranker(record_id: str):
    """
    Hook: Infer best route for an evidence record and write predictions/matches.
    Will be implemented in:
      phase4_semantics/ml/infer_ranker.py
    """
    return {"ok": False, "record_id": record_id, "message": "Not implemented yet. Next step: implement ranker inference."}


# ------------------------------------------------------------
# Run locally
# ------------------------------------------------------------
# Usage:
#   export DATABASE_URL="postgresql://user:pass@host:5432/db"
#   uvicorn phase4_semantics.api.server:app --reload --port 8004
#
# Then:
#   http://localhost:8004/docs
