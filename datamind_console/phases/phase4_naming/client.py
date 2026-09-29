
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence
from collections.abc import Mapping
import importlib
import os
import re
import sys
import subprocess
from pathlib import Path

import streamlit as st


# =============================================================================
# Cache
# =============================================================================

def _get_phase4_client():
    return Phase4Client()


# =============================================================================
# Models
# =============================================================================

@dataclass(frozen=True)
class PendingRoute:
    route_id: str
    status: str
    score: Optional[float] = None
    label: Optional[str] = None
    raw: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class RouteDetail:
    route_id: str
    raw: Dict[str, Any]


# =============================================================================
# Errors
# =============================================================================

class Phase4ClientError(RuntimeError):
    pass


class MissingDependency(Phase4ClientError):
    pass


class MissingPipelineFunction(Phase4ClientError):
    pass


# =============================================================================
# DB helpers
# =============================================================================

try:
    from phase4_semantics.common.db import fetchall, fetchone, execute, db_cursor, get_conn  # type: ignore
except Exception as e:  # pragma: no cover
    raise MissingDependency(
        "phase4_semantics.common.db not available. "
        "Activate the correct venv and ensure PYTHONPATH is set."
    ) from e

from psycopg2.extras import RealDictCursor

from datamind_console.persistence import (
    patch_route_prod_fields,
    delete_route_prod as _delete_route_prod_row,
)

_SOURCE_TYPE_PHASE4 = "phase4_client"
_PIPELINE_VERSION_RESET = "phase4_client.clear_route_naming_artifacts"
_PIPELINE_VERSION_DELETE = "phase4_client.delete_route_from_prod"


# =============================================================================
# SQL CONTRACT — MATCHES YOUR MIGRATIONS
# =============================================================================

PENDING_VIEW = "semantics.v_routes_pending"
SEARCH_VIEW  = "semantics.v_routes_search"  # optional; we use route_prod.route_semantics FTS
_UUID_RX = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


# =============================================================================
# Small helpers
# =============================================================================

def _as_dict(row: Any) -> Dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return dict(row)
    return {"_row": row}


def _rows_as_dicts(rows: Any) -> List[Dict[str, Any]]:
    if not rows:
        return []
    if isinstance(rows[0], Mapping):
        return [dict(r) for r in rows]
    return [{"_row": r} for r in rows]


# =============================================================================
# Import utilities
# =============================================================================

def _import(module_path: str):
    try:
        return importlib.import_module(module_path)
    except Exception as e:
        raise MissingDependency(f"Failed import: {module_path}") from e


def _get_first_attr(module, names: Sequence[str]):
    for n in names:
        fn = getattr(module, n, None)
        if callable(fn):
            return fn
    raise MissingPipelineFunction(
        f"{module.__name__} missing callable. Tried: {', '.join(names)}"
    )


def _try_call(module_path: str, fn_names: Sequence[str], *args, **kwargs):
    mod = _import(module_path)
    fn = _get_first_attr(mod, fn_names)
    return fn(*args, **kwargs)


# =============================================================================
# Client
# =============================================================================

class Phase4Client:
    """
    Phase 4 client — schema-truthful and UI-safe.
    """

    # ---- pipeline hooks (no DB guessing) ----
    _RUN_MODULES = (
        ("phase4_semantics.scripts.run_phase4", ("process_route", "run_route", "run_one")),
        ("phase4_semantics.compiler.semantic_compiler", ("compile_route", "process_route")),
    )

    _BATCH_MODULES = (
        ("phase4_semantics.scripts.run_phase4", ("process_routes", "run_batch")),
        ("phase4_semantics.compiler.semantic_compiler", ("compile_routes",)),
    )

    _PUBLISH_MODULES = (
        ("phase4_semantics.compiler.publish", ("publish_route", "publish")),
    )

    _APPROVE_MODULES = (
        ("phase4_semantics.compiler.publish", ("approve_route", "approve")),
    )

    def _phase4_root(self) -> Path:
        return Path(__file__).resolve().parents[3] / "phase4_naming"

    def _run_script(self, script_name: str, *, args: Optional[List[str]] = None) -> Dict[str, Any]:
        root = self._phase4_root()
        script = root / "phase4_semantics" / "scripts" / script_name
        if not script.exists():
            raise Phase4ClientError(f"Missing script: {script}")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(root)

        result = subprocess.run(
            [sys.executable, str(script), *(args or [])],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
        )
        out = {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "script": script_name,
        }
        if result.returncode != 0:
            raise Phase4ClientError(
                f"{script_name} failed.\nSTDOUT:\n{out['stdout']}\n\nSTDERR:\n{out['stderr']}"
            )
        return out

    # ------------------------------------------------------------------
    # READS
    # ------------------------------------------------------------------

    def list_pending(self, *, limit: int = 50, offset: int = 0) -> List[PendingRoute]:
        """
        Queue view for Phase 4 naming pipeline:
          - pending
          - reviewed_not_promoted
          - promoted
        """
        sql = """
            SELECT
                r.route_id,
                r.service_route_id,
                r.direction_id,
                rs.route_name,
                rs.route_ref,
                rs.operator_name,
                rs.naming_confidence,
                rs.human_verified,
                CASE
                  WHEN rs.route_id IS NULL OR COALESCE(rs.human_verified, FALSE) = FALSE THEN 'pending'
                  WHEN COALESCE(r.human_verified, FALSE) = TRUE
                       AND COALESCE(r.route_name, '') <> ''
                       AND COALESCE(r.route_name, '') = COALESCE(rs.route_name, '') THEN 'promoted'
                  ELSE 'reviewed_not_promoted'
                END AS pipeline_status
            FROM route_prod.routes r
            LEFT JOIN route_prod.route_semantics rs
              ON rs.route_id = r.route_id
            ORDER BY
              CASE
                WHEN rs.route_id IS NULL OR COALESCE(rs.human_verified, FALSE) = FALSE THEN 0
                WHEN COALESCE(r.human_verified, FALSE) = TRUE
                     AND COALESCE(r.route_name, '') <> ''
                     AND COALESCE(r.route_name, '') = COALESCE(rs.route_name, '') THEN 2
                ELSE 1
              END ASC,
              COALESCE(rs.naming_confidence, 0) DESC,
              r.route_id
            LIMIT %s OFFSET %s
        """
        rows = fetchall(sql, (limit, offset)) or []
        out: List[PendingRoute] = []

        for r in rows:
            d = _as_dict(r)
            out.append(
                PendingRoute(
                    route_id=str(d.get("route_id") or (r[0] if isinstance(r, tuple) else "")),
                    status=str(d.get("pipeline_status") or "pending"),
                    score=d.get("naming_confidence"),
                    label=d.get("route_name") or d.get("route_ref"),
                    raw=d,
                )
            )

        return out

    def search(self, query: str, *, limit: int = 25, offset: int = 0) -> List[PendingRoute]:
        """
        Full-text search using stored tsvector on route_prod.route_semantics.
        """
        q = query.strip()

        sql = """
            SELECT
                rs.route_id,
                r.service_route_id,
                r.direction_id,
                rs.route_name,
                rs.route_ref,
                rs.operator_name,
                rs.naming_confidence,
                rs.human_verified,
                CASE
                  WHEN COALESCE(r.human_verified, FALSE) = TRUE
                       AND COALESCE(r.route_name, '') <> ''
                       AND COALESCE(r.route_name, '') = COALESCE(rs.route_name, '') THEN 'promoted'
                  WHEN COALESCE(rs.human_verified, FALSE) = TRUE THEN 'reviewed_not_promoted'
                  ELSE 'pending'
                END AS pipeline_status
            FROM route_prod.route_semantics rs
            JOIN route_prod.routes r ON r.route_id = rs.route_id
            WHERE rs.search_tsv @@ plainto_tsquery('simple'::regconfig, %s)
            ORDER BY rs.naming_confidence DESC, rs.semantics_updated_at DESC
            LIMIT %s OFFSET %s
        """
        rows = fetchall(sql, (q, limit, offset)) or []

        out: List[PendingRoute] = []
        for r in rows:
            d = _as_dict(r)
            out.append(
                PendingRoute(
                    route_id=str(d.get("route_id") or (r[0] if isinstance(r, tuple) else "")),
                    status=str(d.get("pipeline_status") or "pending"),
                    score=d.get("naming_confidence"),
                    label=d.get("route_name") or d.get("route_ref"),
                    raw=d,
                )
            )
        return out

    def get_route_direction_context(self, route_id: str) -> Dict[str, Any]:
        rid = str(route_id or "").strip()
        if not rid:
            return {}
        sql = """
            SELECT
              r.route_id::text AS route_id,
              r.service_route_id::text AS service_route_id,
              r.direction_id::int AS direction_id
            FROM route_prod.routes r
            WHERE r.route_id::text = %s
            LIMIT 1
        """
        row = fetchone(sql, (rid,))
        return _as_dict(row)

    def list_service_route_direction_bindings(self, *, limit: int = 400) -> List[Dict[str, Any]]:
        sql = """
            SELECT
              d.service_route_id::text AS service_route_id,
              d.direction_id::int AS direction_id,
              d.route_id::text AS route_id,
              COALESCE(d.direction_approval_status, 'pending') AS direction_status,
              COALESCE(sr.route_ref, '') AS service_route_ref,
              COALESCE(sr.route_name, '') AS service_route_name,
              COALESCE(rs.route_name, r.route_name, '') AS display_name,
              COALESCE(rs.human_verified, FALSE) AS semantics_verified
            FROM route_raw.service_route_directions d
            LEFT JOIN route_raw.service_routes sr
              ON sr.service_route_id = d.service_route_id
            LEFT JOIN route_prod.routes r
              ON r.route_id = d.route_id
            LEFT JOIN route_prod.route_semantics rs
              ON rs.route_id = d.route_id
            WHERE d.route_id IS NOT NULL
            ORDER BY sr.updated_at DESC NULLS LAST, d.service_route_id, d.direction_id
            LIMIT %s
        """
        rows = fetchall(sql, (int(limit),)) or []
        return _rows_as_dicts(rows)

    def stats_overview(self) -> Dict[str, Any]:
        sql = """
        SELECT
          (SELECT COUNT(*) FROM route_prod.routes) AS total_routes,
          (
            SELECT COUNT(*)
            FROM route_prod.routes r
            LEFT JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
            WHERE rs.route_id IS NULL OR COALESCE(rs.human_verified, FALSE) = FALSE
          ) AS pending_routes,
          (
            SELECT COUNT(*)
            FROM route_prod.routes r
            JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
            WHERE COALESCE(rs.human_verified, FALSE) = TRUE
              AND NOT (
                COALESCE(r.human_verified, FALSE) = TRUE
                AND COALESCE(r.route_name, '') <> ''
                AND COALESCE(r.route_name, '') = COALESCE(rs.route_name, '')
              )
          ) AS reviewed_not_promoted,
          (
            SELECT COUNT(*)
            FROM route_prod.routes r
            JOIN route_prod.route_semantics rs ON rs.route_id = r.route_id
            WHERE COALESCE(rs.human_verified, FALSE) = TRUE
              AND COALESCE(r.human_verified, FALSE) = TRUE
              AND COALESCE(r.route_name, '') <> ''
              AND COALESCE(r.route_name, '') = COALESCE(rs.route_name, '')
          ) AS promoted_routes,
          (SELECT COUNT(*) FROM route_prod.route_semantics) AS semantics_rows,
          (SELECT COUNT(*) FROM route_prod.route_semantics WHERE human_verified) AS verified_routes,
          (SELECT AVG(naming_confidence) FROM route_prod.route_semantics) AS avg_confidence,
          0::bigint AS drafts,
          0::bigint AS draft_approved,
          0::bigint AS draft_rejected
        ;
        """
        row = fetchone(sql, ())
        return _as_dict(row)

    def list_semantics(
        self,
        *,
        verified: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where = ""
        params: List[Any] = []
        if verified is not None:
            where = "WHERE human_verified = %s"
            params.append(verified)

        sql = f"""
        SELECT *
        FROM route_prod.route_semantics
        {where}
        ORDER BY naming_confidence DESC, semantics_updated_at DESC
        LIMIT %s OFFSET %s
        """
        params += [limit, offset]
        rows = fetchall(sql, tuple(params)) or []
        return _rows_as_dicts(rows)

    def get_latest_draft(self, route_id: str) -> Dict[str, Any]:
        sql = """
        SELECT *
        FROM semantics.route_semantics_drafts
        WHERE route_id = %s
        ORDER BY updated_at DESC
        LIMIT 1
        """
        row = fetchone(sql, (route_id,))
        return _as_dict(row)

    def list_drafts(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where = ""
        params: List[Any] = []
        if status:
            where = "WHERE status = %s"
            params.append(status)

        sql = f"""
        SELECT *
        FROM semantics.route_semantics_drafts
        {where}
        ORDER BY updated_at DESC
        LIMIT %s OFFSET %s
        """
        params += [limit, offset]
        rows = fetchall(sql, tuple(params)) or []
        return _rows_as_dicts(rows)

    def list_evidence_records(
        self,
        *,
        source_type: Optional[str] = None,
        route_id_hint: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        wh: List[str] = []
        params: List[Any] = []

        if source_type:
            wh.append("source_type = %s")
            params.append(source_type)
        if route_id_hint:
            wh.append("route_id_hint = %s")
            params.append(route_id_hint)

        where = ("WHERE " + " AND ".join(wh)) if wh else ""

        sql = f"""
        SELECT
          record_id, source_type, source_id, route_id_hint,
          route_ref, route_name, operator_name, from_name, to_name, via,
          confidence_hint, created_at
        FROM semantics.route_evidence_records
        {where}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
        """
        params += [limit, offset]
        rows = fetchall(sql, tuple(params)) or []
        return _rows_as_dicts(rows)

    def list_evidence_matches(
        self,
        *,
        route_id: Optional[str] = None,
        record_id: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        wh: List[str] = []
        params: List[Any] = []

        if route_id:
            wh.append("m.route_id = %s")
            params.append(route_id)
        if record_id:
            wh.append("m.record_id = %s")
            params.append(record_id)

        where = ("WHERE " + " AND ".join(wh)) if wh else ""

        sql = f"""
        SELECT
          m.match_id, m.record_id, m.route_id, m.score, m.is_best, m.created_at,
          r.source_type, r.source_id, r.route_name, r.route_ref, r.operator_name
        FROM semantics.route_evidence_matches m
        JOIN semantics.route_evidence_records r ON r.record_id = m.record_id
        {where}
        ORDER BY m.score DESC, m.created_at DESC
        LIMIT %s OFFSET %s
        """
        params += [limit, offset]
        rows = fetchall(sql, tuple(params)) or []
        return _rows_as_dicts(rows)

    def get_semantics(self, route_id: str) -> Dict[str, Any]:
        sql = """
        SELECT *
        FROM route_prod.route_semantics
        WHERE route_id = %s
        """
        row = fetchone(sql, (route_id,))
        return _as_dict(row)

    def get_route(self, route_id: str) -> RouteDetail:
        route_sql = "SELECT * FROM route_prod.routes WHERE route_id = %s"
        route_row = fetchone(route_sql, (route_id,))
        if not route_row:
            raise Phase4ClientError(f"Route not found: {route_id}")

        sem_row = self.get_semantics(route_id)

        raw = {
            "route": _as_dict(route_row),
            "semantics": sem_row,  # empty dict if none
        }
        return RouteDetail(route_id=route_id, raw=raw)

    # ------------------------------------------------------------------
    # WRITES (DB)
    # ------------------------------------------------------------------

    def _table_exists(self, table: str) -> bool:
        row = fetchone("SELECT to_regclass(%s) IS NOT NULL AS ok", (table,)) or {}
        return bool(row.get("ok"))

    def _table_columns(self, table: str) -> set[str]:
        if "." in table:
            schema, name = table.split(".", 1)
        else:
            schema, name = "public", table
        rows = fetchall(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            """,
            (schema, name),
        ) or []
        return {str(r.get("column_name")) for r in rows if r.get("column_name")}

    def clear_route_naming_artifacts(
        self,
        route_id: str,
        *,
        reset_legacy_route_columns: bool = True,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        rid = str(route_id or "").strip()
        if not rid:
            raise Phase4ClientError("route_id is required")

        out: Dict[str, Any] = {
            "route_id": rid,
            "route_name_feedback": 0,
            "route_name_candidates": 0,
            "route_name_seed_runs": 0,
            "route_semantics_drafts": 0,
            "route_semantics_rows": 0,
            "deleted_route_name_feedback": 0,
            "deleted_route_name_candidates": 0,
            "deleted_route_name_seed_runs": 0,
            "deleted_route_semantics_drafts": 0,
            "deleted_route_semantics_rows": 0,
            "updated_route_prod_rows": 0,
            "dry_run": bool(dry_run),
        }

        with get_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            def _count_if_exists(table: str, col: str = "route_id") -> int:
                if not self._table_exists(table):
                    return 0
                cur.execute(
                    f"SELECT COUNT(*)::int AS n FROM {table} WHERE {col}::text = %s",
                    (rid,),
                )
                return int((cur.fetchone() or {}).get("n") or 0)

            out["route_name_feedback"] = _count_if_exists("semantics.route_name_feedback")
            out["route_name_candidates"] = _count_if_exists("semantics.route_name_candidates")
            out["route_name_seed_runs"] = _count_if_exists("semantics.route_name_seed_runs")
            out["route_semantics_drafts"] = _count_if_exists("semantics.route_semantics_drafts")
            out["route_semantics_rows"] = _count_if_exists("route_prod.route_semantics")

            if dry_run:
                return out

            if self._table_exists("semantics.route_name_feedback"):
                cur.execute(
                    """
                    DELETE FROM semantics.route_name_feedback
                    WHERE route_id::text = %s
                    """,
                    (rid,),
                )
                out["deleted_route_name_feedback"] = int(cur.rowcount or 0)

            if self._table_exists("semantics.route_name_candidates"):
                cur.execute(
                    """
                    DELETE FROM semantics.route_name_candidates
                    WHERE route_id::text = %s
                    """,
                    (rid,),
                )
                out["deleted_route_name_candidates"] = int(cur.rowcount or 0)

            if self._table_exists("semantics.route_name_seed_runs"):
                cur.execute(
                    """
                    DELETE FROM semantics.route_name_seed_runs
                    WHERE route_id::text = %s
                    """,
                    (rid,),
                )
                out["deleted_route_name_seed_runs"] = int(cur.rowcount or 0)

            if self._table_exists("semantics.route_semantics_drafts"):
                cur.execute(
                    """
                    DELETE FROM semantics.route_semantics_drafts
                    WHERE route_id::text = %s
                    """,
                    (rid,),
                )
                out["deleted_route_semantics_drafts"] = int(cur.rowcount or 0)

            if self._table_exists("route_prod.route_semantics"):
                cur.execute(
                    """
                    DELETE FROM route_prod.route_semantics
                    WHERE route_id::text = %s
                    """,
                    (rid,),
                )
                out["deleted_route_semantics_rows"] = int(cur.rowcount or 0)

            if reset_legacy_route_columns and self._table_exists("route_prod.routes"):
                cols = self._table_columns("route_prod.routes")
                reset_fields: Dict[str, Any] = {}
                if "route_name" in cols:
                    reset_fields["route_name"] = None
                if "route_aliases" in cols:
                    reset_fields["route_aliases"] = []
                if "landmark_tags" in cols:
                    reset_fields["landmark_tags"] = []
                if "direction_semantics" in cols:
                    reset_fields["direction_semantics"] = {}
                if "naming_confidence" in cols:
                    reset_fields["naming_confidence"] = None
                if "human_verified" in cols:
                    reset_fields["human_verified"] = False
                if "semantics_updated_at" in cols:
                    reset_fields["semantics_updated_at"] = None

                if reset_fields:
                    _patch_result = patch_route_prod_fields(
                        conn=conn,
                        route_id=rid,
                        fields=reset_fields,
                        source_type=_SOURCE_TYPE_PHASE4,
                        pipeline_version=_PIPELINE_VERSION_RESET,
                    )
                    out["updated_route_prod_rows"] = int(
                        _patch_result.rows_affected.get("route_prod.routes", 0) or 0
                    )

        return out

    def delete_route_from_prod(
        self,
        route_id: str,
        *,
        clear_naming_artifacts: bool = True,
        reset_legacy_route_columns: bool = False,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        rid = str(route_id or "").strip()
        if not rid:
            raise Phase4ClientError("route_id is required")

        out: Dict[str, Any] = {
            "route_id": rid,
            "route_prod_rows": 0,
            "route_semantics_rows": 0,
            "deleted_route_prod_rows": 0,
            "dry_run": bool(dry_run),
        }

        if self._table_exists("route_prod.routes"):
            row = fetchone(
                """
                SELECT COUNT(*)::int AS n
                FROM route_prod.routes
                WHERE route_id::text = %s
                """,
                (rid,),
            ) or {}
            out["route_prod_rows"] = int(row.get("n") or 0)

        if self._table_exists("route_prod.route_semantics"):
            row = fetchone(
                """
                SELECT COUNT(*)::int AS n
                FROM route_prod.route_semantics
                WHERE route_id::text = %s
                """,
                (rid,),
            ) or {}
            out["route_semantics_rows"] = int(row.get("n") or 0)

        if clear_naming_artifacts:
            out["naming_cleanup"] = self.clear_route_naming_artifacts(
                rid,
                reset_legacy_route_columns=bool(reset_legacy_route_columns),
                dry_run=bool(dry_run),
            )

        if dry_run:
            return out

        if not self._table_exists("route_prod.routes"):
            raise Phase4ClientError("Missing table route_prod.routes")

        with get_conn() as conn:
            _del_result = _delete_route_prod_row(
                conn=conn,
                route_id=rid,
                source_type=_SOURCE_TYPE_PHASE4,
                pipeline_version=_PIPELINE_VERSION_DELETE,
                reason=f"phase4_client.delete_route_from_prod (clear_naming_artifacts={bool(clear_naming_artifacts)})",
            )
            out["deleted_route_prod_rows"] = int(
                _del_result.rows_affected.get("route_prod.routes", 0) or 0
            )

        if out["deleted_route_prod_rows"] == 0:
            raise Phase4ClientError(f"route_id not found in route_prod.routes: {rid}")
        return out

    def upsert_semantics(
        self,
        route_id: str,
        *,
        route_name: str,
        route_ref: Optional[str] = None,
        operator_name: Optional[str] = None,
        route_aliases: Optional[List[str]] = None,
        landmark_tags: Optional[List[str]] = None,
        direction_semantics: Optional[Dict[str, Any]] = None,
        naming_confidence: float = 0.0,
        human_verified: bool = False,
    ) -> Dict[str, Any]:
        sql = """
        INSERT INTO route_prod.route_semantics (
          route_id, route_name, route_ref, operator_name,
          route_aliases, landmark_tags, direction_semantics,
          naming_confidence, human_verified
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (route_id) DO UPDATE SET
          route_name = EXCLUDED.route_name,
          route_ref = EXCLUDED.route_ref,
          operator_name = EXCLUDED.operator_name,
          route_aliases = EXCLUDED.route_aliases,
          landmark_tags = EXCLUDED.landmark_tags,
          direction_semantics = EXCLUDED.direction_semantics,
          naming_confidence = EXCLUDED.naming_confidence,
          human_verified = EXCLUDED.human_verified
        RETURNING *;
        """
        params = (
            route_id,
            route_name,
            route_ref,
            operator_name,
            route_aliases or [],
            landmark_tags or [],
            (direction_semantics or {}),
            naming_confidence,
            human_verified,
        )
        row = fetchone(sql, params)
        return _as_dict(row)

    def set_verified(self, route_id: str, verified: bool = True) -> Dict[str, Any]:
        """
        IMPORTANT: also bumps semantics_updated_at so UI "throughput/recent updates" works.
        """
        sql = """
        UPDATE route_prod.route_semantics
        SET human_verified = %s,
            semantics_updated_at = now()
        WHERE route_id = %s
        RETURNING *;
        """
        row = fetchone(sql, (verified, route_id))
        return _as_dict(row)

    # ------------------------------------------------------------------
    # TRAINING / LABELS
    # ------------------------------------------------------------------

    def upsert_match_label(
        self,
        record_id: str,
        route_id: str,
        relevance: int,
        *,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        if relevance not in (0, 1, 2):
            raise Phase4ClientError(f"Invalid relevance={relevance}. Must be 0,1,2.")

        sql = """
        INSERT INTO semantics.match_labels (record_id, route_id, relevance, notes)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (record_id, route_id)
        DO UPDATE SET relevance = EXCLUDED.relevance, notes = EXCLUDED.notes
        RETURNING *;
        """
        row = fetchone(sql, (record_id, route_id, relevance, notes))
        return _as_dict(row)

    def label_balance(self) -> List[Dict[str, Any]]:
        sql = """
        SELECT relevance, COUNT(*) AS n
        FROM semantics.match_labels
        GROUP BY relevance
        ORDER BY relevance;
        """
        rows = fetchall(sql, ()) or []
        return _rows_as_dicts(rows)

    def list_ranker_models(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        sql = """
        SELECT *
        FROM semantics.ranker_models
        ORDER BY trained_at DESC
        LIMIT %s
        """
        rows = fetchall(sql, (limit,)) or []
        return _rows_as_dicts(rows)

    def list_ranker_predictions(
        self,
        *,
        record_id: Optional[str] = None,
        run_id: Optional[str] = None,
        model_id: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        wh: List[str] = []
        params: List[Any] = []

        if record_id:
            wh.append("record_id = %s")
            params.append(record_id)
        if run_id:
            wh.append("run_id = %s")
            params.append(run_id)
        if model_id is not None:
            wh.append("model_id = %s")
            params.append(model_id)

        where = ("WHERE " + " AND ".join(wh)) if wh else ""

        sql = f"""
        SELECT *
        FROM semantics.ranker_predictions
        {where}
        ORDER BY created_at DESC, rank_pos ASC
        LIMIT %s
        """
        params.append(limit)
        rows = fetchall(sql, tuple(params)) or []
        return _rows_as_dicts(rows)

    # ------------------------------------------------------------------
    # PIPELINE (imports)
    # ------------------------------------------------------------------

    def overpass_seed_for_route(self, route_id: str, **kwargs) -> Any:
        """
        Seed evidence for a route using Overpass / Phase 4 seed logic.
        PIPELINE action, not a DB read.
        """
        last_err = None
        seed_targets = (
            ("phase4_semantics.ingest.overpass.seed_candidates", ("seed_route", "run_seed", "seed")),
            ("phase4_semantics.scripts.run_phase4", ("seed_route", "seed_overpass")),
        )
        for module_path, fn_names in seed_targets:
            try:
                return _try_call(module_path, fn_names, route_id, **kwargs)
            except Exception as e:
                last_err = e

        raise MissingPipelineFunction(
            "No Overpass seed function found.\n"
            "Expected something like seed_route(route_id, ...).\n"
            f"Last error: {last_err}"
        )

    def run_route(self, route_id: str, *, force: bool = False, dry_run: bool = False, **kw):
        last = None
        for m, fns in self._RUN_MODULES:
            try:
                return _try_call(m, fns, route_id, force=force, dry_run=dry_run, **kw)
            except Exception as e:
                last = e
        raise MissingPipelineFunction(f"Run failed. Last error: {last}")

    def run_batch(self, route_ids: Iterable[str], *, force: bool = False, dry_run: bool = False, **kw):
        ids = list(route_ids)
        if not ids:
            return {"ok": True, "processed": 0}

        last = None
        for m, fns in self._BATCH_MODULES:
            try:
                return _try_call(m, fns, ids, force=force, dry_run=dry_run, **kw)
            except Exception as e:
                last = e

        return {"ok": True, "processed": len(ids), "fallback": True, "error": str(last)}

    def approve(self, route_id: str, *, approved: bool, **kw):
        last = None
        for m, fns in self._APPROVE_MODULES:
            try:
                return _try_call(m, fns, route_id, approved=approved, **kw)
            except Exception as e:
                last = e
        raise MissingPipelineFunction(f"No approve function found. Last error: {last}")

    def run_pending(self, *, limit: int = 50, force: bool = False, dry_run: bool = False, **kw):
        pending = self.list_pending(limit=limit)
        ids = [p.route_id for p in pending]
        return self.run_batch(ids, force=force, dry_run=dry_run, **kw)

    # ------------------------------------------------------------------
    # PHASE 4 NAMING-ONLY FLOW (10/20/30)
    # ------------------------------------------------------------------

    def get_seed_summary(self, route_id: str) -> Dict[str, Any]:
        sql = """
        SELECT
            seed_run_id,
            route_id,
            seed_source,
            chosen_osm_relation_id,
            seed_route_name,
            seed_route_ref,
            seed_operator_name,
            seed_from_name,
            seed_to_name,
            seed_payload,
            created_at
        FROM semantics.route_name_seed_runs
        WHERE route_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """
        row = fetchone(sql, (route_id,))
        return _as_dict(row)

    def list_name_candidates(self, route_id: str) -> List[Dict[str, Any]]:
        # Preferred read path: materialized latest-candidates view (V4_6 migration).
        sql_view = """
        SELECT
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
            metadata,
            generated_at
        FROM semantics.v_phase4_name_candidates_latest
        WHERE route_id = %s
        ORDER BY rank_pos ASC
        """
        try:
            rows = fetchall(sql_view, (route_id,)) or []
            return _rows_as_dicts(rows)
        except Exception:
            pass

        # Fallback read path when V4_6 view is not yet applied.
        sql_fallback = """
        WITH latest AS (
            SELECT run_id
            FROM semantics.route_name_candidates
            WHERE route_id = %s
            ORDER BY generated_at DESC
            LIMIT 1
        )
        SELECT
            c.candidate_id,
            c.route_id,
            c.run_id,
            c.rank_pos,
            c.route_name,
            c.route_ref,
            c.operator_name,
            c.source_type,
            c.feature_snapshot_version,
            c.features,
            c.heuristic_score,
            c.model_score,
            c.final_score,
            c.metadata,
            c.generated_at
        FROM semantics.route_name_candidates c
        JOIN latest l ON l.run_id = c.run_id
        WHERE c.route_id = %s
        ORDER BY c.rank_pos ASC
        """
        try:
            rows = fetchall(sql_fallback, (route_id, route_id)) or []
        except Exception:
            return []
        return _rows_as_dicts(rows)

    def list_gtfs_export_runs(self, *, limit: int = 30) -> List[Dict[str, Any]]:
        sql = """
        SELECT export_run_id::text AS export_run_id, status, created_at
        FROM gtfs_work.export_runs
        ORDER BY created_at DESC
        LIMIT %s
        """
        try:
            rows = fetchall(sql, (int(limit),)) or []
        except Exception:
            return []
        return _rows_as_dicts(rows)

    def list_gtfs_route_naming_pool(
        self,
        export_run_id: str,
        *,
        search: str = "",
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        run_id = str(export_run_id or "").strip()
        if not run_id:
            return []
        q = str(search or "").strip().lower()
        if q:
            sql = """
            SELECT
              route_id::text AS gtfs_route_id,
              route_short_name,
              route_long_name
            FROM gtfs_work.gtfs_routes
            WHERE export_run_id = %s
              AND (
                lower(COALESCE(route_id, '')) LIKE %s
                OR lower(COALESCE(route_short_name, '')) LIKE %s
                OR lower(COALESCE(route_long_name, '')) LIKE %s
              )
            ORDER BY
              lower(COALESCE(route_short_name, '')),
              lower(COALESCE(route_long_name, '')),
              route_id
            LIMIT %s
            """
            like = f"%{q}%"
            params: tuple[Any, ...] = (run_id, like, like, like, int(limit))
        else:
            sql = """
            SELECT
              route_id::text AS gtfs_route_id,
              route_short_name,
              route_long_name
            FROM gtfs_work.gtfs_routes
            WHERE export_run_id = %s
            ORDER BY
              lower(COALESCE(route_short_name, '')),
              lower(COALESCE(route_long_name, '')),
              route_id
            LIMIT %s
            """
            params = (run_id, int(limit))
        try:
            rows = fetchall(sql, params) or []
        except Exception:
            return []
        return _rows_as_dicts(rows)

    def get_gtfs_route_name_row(self, export_run_id: str, gtfs_route_id: str) -> Dict[str, Any]:
        run_id = str(export_run_id or "").strip()
        rid = str(gtfs_route_id or "").strip()
        if not run_id or not rid:
            return {}
        sql = """
        SELECT
          route_id::text AS gtfs_route_id,
          route_short_name,
          route_long_name
        FROM gtfs_work.gtfs_routes
        WHERE export_run_id = %s
          AND route_id = %s
        LIMIT 1
        """
        try:
            row = fetchone(sql, (run_id, rid)) or {}
        except Exception:
            return {}
        return _as_dict(row)

    def list_gtfs_stop_name_pool(
        self,
        export_run_id: str,
        gtfs_route_id: str,
        *,
        search: str = "",
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        run_id = str(export_run_id or "").strip()
        rid = str(gtfs_route_id or "").strip()
        if not run_id or not rid:
            return []
        q = str(search or "").strip().lower()
        wh = ""
        params: List[Any] = [run_id, run_id, rid]
        if q:
            wh = "AND lower(COALESCE(s.stop_name, '')) LIKE %s"
            params.append(f"%{q}%")
        params.append(int(limit))
        sql = f"""
        SELECT
          COALESCE(s.stop_name, '') AS stop_name,
          MIN(s.stop_id)::text AS sample_stop_id,
          COUNT(*)::int AS n_rows
        FROM gtfs_work.gtfs_stop_times st
        JOIN gtfs_work.gtfs_trips t
          ON t.export_run_id = st.export_run_id
         AND t.trip_id = st.trip_id
        JOIN gtfs_work.gtfs_stops s
          ON s.export_run_id = st.export_run_id
         AND s.stop_id = st.stop_id
        WHERE st.export_run_id = %s
          AND t.export_run_id = %s
          AND t.route_id = %s
          {wh}
        GROUP BY COALESCE(s.stop_name, '')
        ORDER BY lower(COALESCE(s.stop_name, '')), COALESCE(s.stop_name, '')
        LIMIT %s
        """
        try:
            rows = fetchall(sql, tuple(params)) or []
        except Exception:
            return []
        return _rows_as_dicts(rows)

    def _parse_gtfs_bridge_from_notes(self, notes: str) -> Dict[str, str]:
        text = str(notes or "")
        out: Dict[str, str] = {}
        m_run = re.search(rf"export_run_id=({_UUID_RX})", text)
        if m_run:
            out["export_run_id"] = str(m_run.group(1))
        m_route = re.search(r"route_id=([^|]+)", text)
        if m_route:
            rid = str(m_route.group(1) or "").strip()
            if rid and rid != "(all)":
                out["gtfs_route_id"] = rid
        return out

    def get_gtfs_naming_hint_for_phase4_route(self, route_id: str) -> Dict[str, Any]:
        rid = str(route_id or "").strip()
        if not rid:
            return {}
        row = fetchone(
            """
            SELECT notes, known_ref
            FROM route_raw.route_jobs
            WHERE route_id = %s
            LIMIT 1
            """,
            (rid,),
        ) or {}
        notes = str(row.get("notes") or "")
        known_ref = str(row.get("known_ref") or "").strip()
        bridge = self._parse_gtfs_bridge_from_notes(notes)
        export_run_id = str(bridge.get("export_run_id") or "")
        gtfs_route_id = str(bridge.get("gtfs_route_id") or "")

        out: Dict[str, Any] = {
            "route_id": rid,
            "export_run_id": export_run_id,
            "gtfs_route_id": gtfs_route_id,
            "known_ref": known_ref,
            "route_short_name": "",
            "route_long_name": "",
        }
        if not export_run_id:
            return out

        chosen = {}
        if gtfs_route_id:
            rows = self.list_gtfs_route_naming_pool(export_run_id, search=gtfs_route_id, limit=50)
            exact = [r for r in rows if str(r.get("gtfs_route_id") or "") == gtfs_route_id]
            if exact:
                chosen = exact[0]
        if (not chosen) and known_ref:
            rows = self.list_gtfs_route_naming_pool(export_run_id, search=known_ref, limit=50)
            exact_ref = [r for r in rows if str(r.get("route_short_name") or "").strip() == known_ref]
            if exact_ref:
                chosen = exact_ref[0]
        if chosen:
            out["gtfs_route_id"] = str(chosen.get("gtfs_route_id") or out["gtfs_route_id"])
            out["route_short_name"] = str(chosen.get("route_short_name") or "")
            out["route_long_name"] = str(chosen.get("route_long_name") or "")
        return out

    def build_step20_inputs_from_bundle(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        bundle = dict(payload or {})
        seed = dict(bundle.get("seed") or {})
        semantics = dict(bundle.get("semantics") or {})
        gtfs = dict(bundle.get("gtfs_route") or {})

        catalog_in = dict(bundle.get("catalog_inputs") or {})
        user_in = dict(bundle.get("user_inputs") or {})

        operator = (
            str(catalog_in.get("operator_name") or "").strip()
            or str(user_in.get("operator_name") or "").strip()
            or str(semantics.get("operator_name") or "").strip()
            or str(seed.get("seed_operator_name") or "").strip()
            or str(seed.get("operator_name") or "").strip()
        )

        route_ref = (
            str(user_in.get("route_ref") or "").strip()
            or str(gtfs.get("route_short_name") or "").strip()
            or str(semantics.get("route_ref") or "").strip()
            or str(seed.get("seed_route_ref") or "").strip()
            or str(seed.get("route_ref") or "").strip()
        )

        names_raw: List[str] = []
        for k in ("custom_names",):
            vals = user_in.get(k)
            if isinstance(vals, list):
                names_raw.extend([str(x).strip() for x in vals if str(x).strip()])
        cands = bundle.get("candidates") or []
        if isinstance(cands, list):
            for c in cands:
                if not isinstance(c, Mapping):
                    continue
                n = str(c.get("route_name") or "").strip()
                if n:
                    names_raw.append(n)
        n_sem = str(semantics.get("route_name") or "").strip()
        if n_sem:
            names_raw.append(n_sem)
        n_gtfs = str(gtfs.get("route_long_name") or "").strip()
        if n_gtfs:
            names_raw.append(n_gtfs)
        aliases = semantics.get("route_aliases") or []
        if isinstance(aliases, list):
            names_raw.extend([str(x).strip() for x in aliases if str(x).strip()])

        seen = set()
        custom_names: List[str] = []
        for n in names_raw:
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            custom_names.append(n)

        return {
            "catalog_inputs": {"operator_name": operator},
            "user_inputs": {
                "operator_name": operator,
                "route_ref": route_ref,
                "custom_names": custom_names,
            },
        }

    def export_naming_bundle(
        self,
        route_id: str,
        *,
        include_candidates: bool = True,
    ) -> Dict[str, Any]:
        seed = self.get_seed_summary(route_id) or {}
        semantics = self.get_semantics(route_id) or {}
        candidates = self.list_name_candidates(route_id) if include_candidates else []
        gtfs_route = self.get_gtfs_naming_hint_for_phase4_route(route_id) or {}

        inputs = self.build_step20_inputs_from_bundle(
            {
                "seed": seed,
                "semantics": semantics,
                "candidates": candidates,
                "gtfs_route": gtfs_route,
            }
        )

        return {
            "bundle_type": "phase4_naming",
            "bundle_version": "v1",
            "route_id": str(route_id),
            "seed": seed,
            "semantics": semantics,
            "candidates": candidates,
            "gtfs_route": gtfs_route,
            "catalog_inputs": inputs.get("catalog_inputs") or {},
            "user_inputs": inputs.get("user_inputs") or {},
        }

    def run_step_20_from_bundle(
        self,
        route_id: str,
        *,
        bundle_payload: Mapping[str, Any],
        top_k: int = 5,
    ) -> Dict[str, Any]:
        inputs = self.build_step20_inputs_from_bundle(bundle_payload)
        return self.run_step_20_candidates(
            route_id,
            catalog_inputs=dict(inputs.get("catalog_inputs") or {}),
            user_inputs=dict(inputs.get("user_inputs") or {}),
            top_k=max(1, int(top_k)),
        )

    def run_step_10_extract(self, route_id: str) -> Dict[str, Any]:
        out = _try_call(
            "phase4_semantics.seed.extract",
            ("extract_seed_for_route",),
            route_id,
            persist=True,
        )
        return _as_dict(out)

    def run_step_20_candidates(
        self,
        route_id: str,
        *,
        catalog_inputs: Optional[Dict[str, Any]] = None,
        user_inputs: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        rows = _try_call(
            "phase4_semantics.naming.candidate_builder",
            ("build_top_name_candidates",),
            route_id,
            catalog_inputs=catalog_inputs or {},
            user_inputs=user_inputs or {},
            top_k=max(1, int(top_k)),
        ) or []
        return {"route_id": route_id, "n_candidates": len(rows), "candidates": rows}

    def run_step_30_review(
        self,
        route_id: str,
        *,
        winner_candidate_id: str,
        candidate_scores: Dict[str, int],
        reviewer: str = "console",
    ) -> Dict[str, Any]:
        out = _try_call(
            "phase4_semantics.review.persist",
            ("persist_review",),
            route_id,
            winner_candidate_id,
            candidate_scores,
            reviewer=reviewer,
        )
        return _as_dict(out)

    def run_step_40_finalize(self, route_id: str) -> Dict[str, Any]:
        out = _try_call(
            "phase4_semantics.review.finalize",
            ("finalize_route_prod",),
            route_id,
        )
        return _as_dict(out)

    # ------------------------------------------------------------------
    # SOFT-DEPRECATED LEGACY STEP METHODS (03..07)
    # ------------------------------------------------------------------

    def _deprecated(self, name: str) -> Dict[str, Any]:
        return {
            "deprecated": True,
            "method": name,
            "message": (
                f"{name} is deprecated in naming-only Phase 4 flow. "
                "Use run_step_10_extract / run_step_20_candidates / run_step_30_review."
            ),
        }

    def run_step_01_sample_points(self, route_id: str, *, sample_version: str = "v1") -> Dict[str, Any]:
        out = self._deprecated("run_step_01_sample_points")
        out.update({"route_id": route_id, "sample_version": sample_version})
        return out

    def run_step_02_seed_overpass(self, route_id: str, *, sample_version: str = "v1") -> Dict[str, Any]:
        out = self._deprecated("run_step_02_seed_overpass")
        out.update({"route_id": route_id, "sample_version": sample_version})
        return out

    def run_step_03_intersections(self, route_id: str, *, sample_version: str = "v1") -> Dict[str, Any]:
        out = self._deprecated("run_step_03_intersections")
        out.update({"route_id": route_id, "sample_version": sample_version})
        return out

    def run_step_04_scoring(self, route_id: str, *, sample_version: str = "v1") -> Dict[str, Any]:
        out = self._deprecated("run_step_04_scoring")
        out.update({"route_id": route_id, "sample_version": sample_version})
        return out

    def run_step_05_persist_matches(self, route_id: str, *, sample_version: str = "v1") -> Dict[str, Any]:
        out = self._deprecated("run_step_05_persist_matches")
        out.update({"route_id": route_id, "sample_version": sample_version})
        return out

    def run_step_06_compile(self, route_id: str) -> Dict[str, Any]:
        out = self._deprecated("run_step_06_compile")
        out.update({"route_id": route_id})
        return out

    def run_step_07_auto_approve(self, route_id: str, *, min_confidence: float = 0.85) -> Dict[str, Any]:
        out = self._deprecated("run_step_07_auto_approve")
        out.update({"route_id": route_id, "min_confidence": float(min_confidence)})
        return out

    def run_phase4_script(
        self,
        *,
        limit: Optional[int] = None,
        auto_approve: bool = False,
        min_confidence: float = 0.85,
        continue_on_error: bool = False,
        start_step: int = 10,
        stop_after_step: Optional[int] = None,
        route_id: Optional[str] = None,
        auto_review: bool = False,
        reviewer: str = "console",
    ) -> Dict[str, Any]:
        args: List[str] = [
            "--start-step", str(int(start_step)),
        ]
        if stop_after_step is not None:
            args.extend(["--stop-after-step", str(int(stop_after_step))])
        if limit is not None:
            args.extend(["--limit", str(int(limit))])
        if route_id:
            args.extend(["--route-id", str(route_id)])
        if auto_review:
            args.append("--auto-review")
        if reviewer:
            args.extend(["--reviewer", str(reviewer)])
        return self._run_script("run_phase4.py", args=args)


# =============================================================================
# Smoke test
# =============================================================================

if __name__ == "__main__":
    c = Phase4Client()
    p = c.list_pending(limit=5)
    print("pending:", len(p))
    if p:
        print("first route:", p[0].route_id)
