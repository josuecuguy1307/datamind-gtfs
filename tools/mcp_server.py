#!/usr/bin/env python3
"""
DataMind/DataMind MCP Server
──────────────────────────
Exposes the ML DATAMIND transit pipeline database to Claude Code
and Claude Desktop via the Model Context Protocol (MCP).

All tools are READ-ONLY. No mutations. Enforced in three layers:
  1. sessions open with default_transaction_read_only=on and a 30 s
     statement_timeout (SESSION_OPTIONS), and every transaction is
     SET TRANSACTION READ ONLY;
  2. query_db accepts exactly one SELECT/WITH/VALUES/TABLE/SHOW/EXPLAIN
     statement and rejects COPY, dblink and other side-effecting functions
     (tools/mcp_sql_guard.py);
  3. results are capped at 500 rows.
This is developer/operator tooling for coding agents. It is not part of the
Streamlit product and does not run pipeline engines.

Usage (stdio transport — default for Claude Code & Desktop):
    python tools/mcp_server.py
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence
from uuid import UUID

# ── ensure project root is on sys.path so we can reuse config ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── load .env (existing process variables win) before CFG is built ──
from datamind_console.common.env import load_project_env  # noqa: E402

load_project_env(PROJECT_ROOT / ".env")

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

# ── DB DSN resolution (mirrors datamind_console/common/config.py) ──
from datamind_console.common.config import resolve_active_db_dsn, CFG  # noqa: E402

try:  # sibling module when run as ``python tools/mcp_server.py``
    from mcp_sql_guard import MAX_ROWS, UnsafeSQLError, validate_read_query  # type: ignore
except ImportError:  # imported as tools.mcp_server (tests)
    from tools.mcp_sql_guard import MAX_ROWS, UnsafeSQLError, validate_read_query  # type: ignore

# Every MCP session is read-only at the *session* level (not just the first
# transaction) and bounded in time.
SESSION_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=30000"


# ============================================================
#  DB helpers (lightweight, same pattern as db.py)
# ============================================================

@contextmanager
def _db(*, readonly: bool = True) -> Iterator[psycopg2.extensions.connection]:
    """Short-lived connection: session read-only, transaction read-only, closed on exit."""
    dsn = resolve_active_db_dsn() or CFG.db_dsn
    if not dsn:
        raise RuntimeError("No DB_DSN configured. Set DB_DSN or LOCAL_DB_DSN in .env")
    conn = psycopg2.connect(dsn, connect_timeout=CFG.db_connect_timeout_s, options=SESSION_OPTIONS)
    try:
        conn.autocommit = False
        if readonly:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY;")
        yield conn
        conn.rollback()  # nothing to keep: end the read transaction explicitly
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _execute(cur: Any, sql: str, params: Optional[Sequence[Any]]) -> None:
    # Only interpolate when parameters are given. Passing an empty tuple
    # made psycopg2 parse every "%" (e.g. LIKE 'pg_temp%') and fail with
    # "tuple index out of range"; this broke list_schemas and any query_db
    # text containing "%".
    if params is None:
        cur.execute(sql)
    else:
        cur.execute(sql, params)


def _fetch_all(sql: str, params: Optional[Sequence[Any]] = None, *, max_rows: Optional[int] = None) -> List[Dict[str, Any]]:
    with _db(readonly=True) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _execute(cur, sql, params)
            if cur.description is None:
                return []
            rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
            return [dict(r) for r in rows]


def _fetch_one(sql: str, params: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    with _db(readonly=True) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _execute(cur, sql, params)
            row = cur.fetchone()
            return dict(row) if row else {}


# ============================================================
#  JSON serialiser (handles datetime, Decimal, UUID, etc.)
# ============================================================

class _Enc(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, memoryview):
            return o.tobytes().hex()
        if isinstance(o, bytes):
            return o.hex()
        return super().default(o)


def _json(obj: Any) -> str:
    return json.dumps(obj, cls=_Enc, indent=2, ensure_ascii=False)


# ============================================================
#  MCP Server
# ============================================================

mcp = FastMCP(
    "datamind",
    instructions=(
        "DataMind transit-data pipeline.  "
        "Use these tools to query the dashboard database (read-only).  "
        "The DB contains 5 phases: nodes (stops/POIs), semantics (places), "
        "routes, naming, and GTFS.  "
        "All geographic data belongs to the configured region (see workspace/config/supported_provinces.json)."
    ),
)


# ── 1. Dashboard KPIs ──────────────────────────────────────

@mcp.tool()
def dashboard_kpis() -> str:
    """Get high-level KPIs: total nodes, stops, POIs, places, routes, verified routes, and last-updated timestamps."""
    def _count(sql: str) -> int:
        row = _fetch_one(sql)
        return int(row.get("n") or 0)

    kpis = {
        "nodes_total": _count("SELECT COUNT(*) AS n FROM node_prod.nodes"),
        "nodes_stops": _count("SELECT COUNT(*) AS n FROM node_prod.nodes WHERE node_type='STOP'"),
        "nodes_poi": _count("SELECT COUNT(*) AS n FROM node_prod.nodes WHERE node_type='POI'"),
        "places_total": _count("SELECT COUNT(*) AS n FROM geo_prod.places"),
        "routes_total": _count("SELECT COUNT(*) AS n FROM route_prod.routes"),
        "routes_verified": _count("SELECT COUNT(*) AS n FROM route_prod.routes WHERE human_verified=true"),
        "node_place_mappings": _count("SELECT COUNT(*) AS n FROM geo_prod.node_place_map"),
    }

    for label, tbl in [("nodes", "node_prod.nodes"), ("places", "geo_prod.places"), ("routes", "route_prod.routes")]:
        row = _fetch_one(f"SELECT MAX(updated_at) AS ts FROM {tbl}")
        kpis[f"{label}_last_updated"] = row.get("ts")

    return _json(kpis)


# ── 2. Health Snapshot ──────────────────────────────────────

@mcp.tool()
def health_snapshot() -> str:
    """System health: null geometries, empty stop arrays, and data quality flags across all production schemas."""
    def _c(sql: str) -> int:
        row = _fetch_one(sql)
        return int(row.get("n") or 0)

    return _json({
        "node_prod_total": _c("SELECT COUNT(*) AS n FROM node_prod.nodes"),
        "node_prod_null_geom": _c("SELECT COUNT(*) AS n FROM node_prod.nodes WHERE geom IS NULL"),
        "geo_prod_places_total": _c("SELECT COUNT(*) AS n FROM geo_prod.places"),
        "route_prod_total": _c("SELECT COUNT(*) AS n FROM route_prod.routes"),
        "route_prod_null_geom": _c("SELECT COUNT(*) AS n FROM route_prod.routes WHERE geom IS NULL"),
        "route_prod_empty_stops": _c(
            "SELECT COUNT(*) AS n FROM route_prod.routes "
            "WHERE COALESCE(array_length(stop_node_ids, 1), 0) = 0"
        ),
    })


# ── 3. Phase Summary ───────────────────────────────────────

@mcp.tool()
def phase_summary(since_days: int = 30) -> str:
    """Approve/reject/edit counts by phase over the last N days (default 30)."""
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    rows = _fetch_all(
        """
        SELECT phase,
               COUNT(*) AS total,
               SUM(CASE WHEN decision='APPROVE' THEN 1 ELSE 0 END) AS approve,
               SUM(CASE WHEN decision='REJECT'  THEN 1 ELSE 0 END) AS reject,
               SUM(CASE WHEN decision='EDIT'    THEN 1 ELSE 0 END) AS edit,
               SUM(CASE WHEN decision='PUBLISH' THEN 1 ELSE 0 END) AS publish
        FROM console.phase_decisions
        WHERE created_at >= %s
        GROUP BY phase ORDER BY phase
        """,
        (since,),
    )
    return _json(rows)


# ── 4. Route Catalog ───────────────────────────────────────

@mcp.tool()
def route_catalog(limit: int = 50) -> str:
    """List routes from the Phase 3 global catalog view (route_review.phase3_global_catalog_v1): name, operator, sector, canonical/suppressed state, production and approval status, prior stop count."""
    rows = _fetch_all(
        """
        SELECT route_job_id,
               service_route_id,
               direction_id,
               COALESCE(prod_route_name, service_route_name, route_family_label) AS route_name,
               service_route_operator AS operator,
               sector_label,
               sector_key,
               is_canonical_route_job,
               is_suppressed_duplicate,
               prod_status,
               approval_status,
               geometry_status,
               step05_state,
               step20_state,
               prior_stop_count AS stop_count,
               inventory_status
        FROM route_review.phase3_global_catalog_v1
        ORDER BY route_name ASC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )
    return _json(rows)


# ── 5. Route Detail ────────────────────────────────────────

@mcp.tool()
def route_detail(route_id: str) -> str:
    """Get full detail for a single route (prod + raw metadata)."""
    row = _fetch_one(
        """
        SELECT r.*,
               ST_AsGeoJSON(r.geom)::jsonb AS geom_geojson,
               COALESCE(array_length(r.stop_node_ids, 1), 0) AS stop_count
        FROM route_prod.routes r
        WHERE r.route_id::text = %s
        """,
        (route_id,),
    )
    if not row:
        # try raw
        row = _fetch_one(
            "SELECT * FROM route_raw.active_route_jobs WHERE route_id::text = %s",
            (route_id,),
        )
    return _json(row) if row else '{"error": "route not found"}'


# ── 6. Route Leaderboards ──────────────────────────────────

@mcp.tool()
def routes_by_length(limit: int = 30) -> str:
    """Top routes by length (km), with stop count and naming confidence."""
    rows = _fetch_all(
        """
        SELECT route_id,
               COALESCE(route_name, route_id::text) AS label,
               ROUND((ST_Length(geom::geography)/1000.0)::numeric, 2) AS km,
               COALESCE(array_length(stop_node_ids, 1), 0) AS stop_count,
               naming_confidence,
               human_verified
        FROM route_prod.routes
        WHERE geom IS NOT NULL
        ORDER BY km DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )
    return _json(rows)


@mcp.tool()
def routes_by_stops(limit: int = 30) -> str:
    """Top routes by number of stops."""
    rows = _fetch_all(
        """
        SELECT route_id,
               COALESCE(route_name, route_id::text) AS label,
               COALESCE(array_length(stop_node_ids, 1), 0) AS stop_count,
               naming_confidence, human_verified
        FROM route_prod.routes
        ORDER BY stop_count DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )
    return _json(rows)


# ── 7. Node Inventory ──────────────────────────────────────

@mcp.tool()
def node_inventory(node_type: str = "", limit: int = 100) -> str:
    """List production nodes (stops/POIs) with coordinates. Filter by node_type (STOP, POI) or leave blank for all."""
    where = ""
    params: list = []
    if node_type.strip():
        where = "WHERE node_type = %s"
        params.append(node_type.upper().strip())
    params.append(limit)

    rows = _fetch_all(
        f"""
        SELECT node_id, node_type, name, ref, operator, tag_kind, confidence,
               ST_Y(geom) AS lat, ST_X(geom) AS lon
        FROM node_prod.nodes
        {where}
        ORDER BY confidence DESC NULLS LAST
        LIMIT %s
        """,
        tuple(params),
    )
    return _json(rows)


# ── 8. Places (Geo Prod) ───────────────────────────────────

@mcp.tool()
def place_inventory(place_type: str = "", limit: int = 100) -> str:
    """List production places with canonical names and coordinates. Filter by place_type (STOP, POI) or leave blank."""
    where = ""
    params: list = []
    if place_type.strip():
        where = "WHERE place_type = %s"
        params.append(place_type.upper().strip())
    params.append(limit)

    rows = _fetch_all(
        f"""
        SELECT place_id, canonical_name, place_type, node_id,
               confidence, mapping_source, node_type, lat, lon
        FROM geo_prod.v_place_points
        {where}
        ORDER BY confidence DESC NULLS LAST, canonical_name ASC
        LIMIT %s
        """,
        tuple(params),
    )
    return _json(rows)


# ── 9. Phase 1 Extraction Runs ─────────────────────────────

@mcp.tool()
def extraction_runs(limit: int = 30) -> str:
    """Recent Phase 1 Overpass extraction runs with element counts, runtime and status."""
    rows = _fetch_all(
        """
        SELECT run_id, query_id, area_id, bbox,
               element_count, status, runtime_ms, response_bytes,
               fetched_at
        FROM node_raw.overpass_runs
        ORDER BY fetched_at DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )
    return _json(rows)


# ── 10. Node Candidate Sets ────────────────────────────────

@mcp.tool()
def candidate_sets(area_id: str = "", limit: int = 30) -> str:
    """Phase 1 node candidate sets (node_work.node_candidate_sets). Filter by area_id substring (stored in params_used) or list recent."""
    where = ""
    params: list = []
    if area_id.strip():
        where = "WHERE params_used->>'area_id' ILIKE %s"
        params.append(f"%{area_id.strip()}%")
    params.append(limit)

    rows = _fetch_all(
        f"""
        SELECT node_set_id,
               params_used->>'area_id' AS area_id,
               params_used->>'group' AS area_group,
               source_run_ids,
               rank_score,
               rank_model_ver,
               created_at
        FROM node_work.node_candidate_sets
        {where}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        tuple(params),
    )
    return _json(rows)


# ── 11. Recent Decisions ───────────────────────────────────

@mcp.tool()
def recent_decisions(phase: int = 0, limit: int = 50) -> str:
    """Recent human decisions (approve/reject/edit/publish). Filter by phase (1-5) or 0 for all."""
    where = ""
    params: list = []
    if phase > 0:
        where = "WHERE phase = %s"
        params.append(phase)
    params.append(limit)

    rows = _fetch_all(
        f"""
        SELECT decision_id, phase, item_id, candidate_id,
               score_at_decision, decision, reason_code, notes,
               user_id, created_at
        FROM console.phase_decisions
        {where}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        tuple(params),
    )
    return _json(rows)


# ── 12. Arbitrary Read-Only SQL ─────────────────────────────

@mcp.tool()
def query_db(sql: str) -> str:
    """Execute ONE read-only SQL statement (SELECT/WITH/VALUES/TABLE/SHOW/EXPLAIN) against the datamind_ml database. Multiple statements, COPY, dblink and other side-effecting functions are rejected; the session is read-only with a 30 s timeout. Returns up to 500 rows as JSON."""
    try:
        clean, add_limit = validate_read_query(sql)
    except UnsafeSQLError as exc:
        return _json({"error": str(exc)})
    statement = f"{clean}\nLIMIT {MAX_ROWS}" if add_limit else clean
    try:
        rows = _fetch_all(statement, max_rows=MAX_ROWS)
    except psycopg2.Error as exc:
        return _json({"error": str(exc).strip()})
    return _json(rows)


# ── 13. Schema Explorer ────────────────────────────────────

@mcp.tool()
def list_schemas() -> str:
    """List all schemas in the database with their table counts."""
    rows = _fetch_all(
        """
        SELECT n.nspname AS schema_name,
               COUNT(c.relname) AS table_count
        FROM pg_catalog.pg_namespace n
        LEFT JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','v','m')
        WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
          AND n.nspname NOT LIKE 'pg_temp%'
        GROUP BY n.nspname
        ORDER BY n.nspname
        """
    )
    return _json(rows)


@mcp.tool()
def list_tables(schema: str = "node_prod") -> str:
    """List tables and views in a schema with row counts and column info."""
    rows = _fetch_all(
        """
        SELECT c.relname AS table_name,
               CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized_view' END AS type,
               pg_catalog.obj_description(c.oid) AS comment
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relkind IN ('r','v','m')
        ORDER BY c.relname
        """,
        (schema,),
    )
    return _json(rows)


@mcp.tool()
def describe_table(schema: str, table: str) -> str:
    """Show columns, types, and nullable status for a specific table."""
    rows = _fetch_all(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )
    return _json(rows)


# ── 14. AI Model Registry ──────────────────────────────────

@mcp.tool()
def ai_models() -> str:
    """List registered models: ai.ai_model_registry (AI bot) and geo_work.model_registry (Phase 2 name ranker / place type), with how each was trained. File-based artifacts are listed in docs/CURRENT_CAPABILITIES.md."""
    out: Dict[str, Any] = {}
    try:
        out["ai_model_registry"] = _fetch_all(
            """
            SELECT model_id, model_name, model_version, phase, artifact_path,
                   metrics, is_active, trained_on, created_at
            FROM ai.ai_model_registry
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
    except psycopg2.Error as exc:
        out["ai_model_registry"] = {"error": str(exc).strip()}
    try:
        out["geo_work_model_registry"] = _fetch_all(
            """
            SELECT model_name,
                   version,
                   artifact->>'method' AS method,
                   artifact->>'n_pos' AS n_pos,
                   artifact->>'n_neg' AS n_neg,
                   updated_at
            FROM geo_work.model_registry
            ORDER BY model_name
            """
        )
    except psycopg2.Error as exc:
        out["geo_work_model_registry"] = {"error": str(exc).strip()}
    out["note"] = (
        "File-based models are not registered in the database: Phase 1 STOP/POI LightGBM "
        "(stop_poi_lgbm_v3.txt), Phase 3 on-route LightGBM (models/on_route_lgbm.txt), "
        "Phase 4 LambdaRank (phase4_models/lgbm/v1/model.txt, empty). "
        "See docs/CURRENT_CAPABILITIES.md for what is actually active."
    )
    return _json(out)


# ── 15. Route Naming (Phase 4) ─────────────────────────────

@mcp.tool()
def naming_confidence_distribution() -> str:
    """Histogram of route naming confidence scores across all production routes."""
    rows = _fetch_all(
        """
        SELECT width_bucket(naming_confidence, 0, 1, 20) AS bucket,
               COUNT(*) AS n,
               MIN(naming_confidence) AS bucket_min,
               MAX(naming_confidence) AS bucket_max
        FROM route_prod.routes
        WHERE naming_confidence IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """
    )
    return _json(rows)


# ============================================================
#  Entrypoint
# ============================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
