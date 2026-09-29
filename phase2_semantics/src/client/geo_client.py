from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.db.conn import db_conn, fetchall, fetchone, exec_sql
from src import settings


@dataclass
class ExtractSummary:
    extract_run_id: str
    n_nodes_seen: int
    n_evidence: int


@dataclass
class ApproveSummary:
    place_set_id: str
    n_places: int
    n_aliases: int
    n_mappings: int


class GeoClient:
    """
    High-level client for Streamlit.
    Streamlit should call only this class, never raw repos/SQL directly.
    """

    # ---------------------------
    # RAW: extraction
    # ---------------------------

    def start_extract_run(self, context_key: str | None = None) -> str:
        ctx = context_key or settings.GEO_CONTEXT_KEY
        sql = f"""
        INSERT INTO {settings.T_GEO_EXTRACT_RUNS} (context_key, status)
        VALUES (%(ctx)s, 'ok')
        RETURNING extract_run_id
        """
        row = fetchone(sql, {"ctx": ctx})
        return str(row["extract_run_id"])

    def get_extract_run(self, extract_run_id: str) -> dict[str, Any]:
        sql = f"""
        SELECT *
        FROM {settings.T_GEO_EXTRACT_RUNS}
        WHERE extract_run_id = %(rid)s
        """
        return dict(fetchone(sql, {"rid": extract_run_id}))

    def list_extract_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        sql = f"""
        SELECT *
        FROM {settings.T_GEO_EXTRACT_RUNS}
        ORDER BY extracted_at DESC
        LIMIT %(lim)s
        """
        rows = fetchall(sql, {"lim": limit})
        return [dict(r) for r in rows]

    # ---------------------------
    # RAW: evidence
    # ---------------------------

    def count_nodes_in_prod(self) -> int:
        sql = f"SELECT COUNT(*) AS n FROM {settings.T_NODE_PROD_NODES}"
        return int(fetchone(sql)["n"])

    def count_evidence(self, extract_run_id: str) -> int:
        sql = f"""
        SELECT COUNT(*) AS n
        FROM {settings.T_GEO_NAME_EVIDENCE}
        WHERE extract_run_id = %(rid)s
        """
        return int(fetchone(sql, {"rid": extract_run_id})["n"])

    def evidence_preview(self, extract_run_id: str, limit: int = 50) -> list[dict[str, Any]]:
        sql = f"""
        SELECT node_id, source, raw_text, lang, weight_hint
        FROM {settings.T_GEO_NAME_EVIDENCE}
        WHERE extract_run_id = %(rid)s
        ORDER BY inserted_at DESC
        LIMIT %(lim)s
        """
        rows = fetchall(sql, {"rid": extract_run_id, "lim": limit})
        return [dict(r) for r in rows]

    # ---------------------------
    # WORK: candidate sets
    # ---------------------------

    def list_candidate_sets(self, context_key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        ctx = context_key or settings.GEO_CONTEXT_KEY
        sql = f"""
        SELECT place_set_id, source_extract_run_id, context_key, params_used, rank_score, created_at
        FROM {settings.T_PLACE_CANDIDATE_SETS}
        WHERE context_key = %(ctx)s
        ORDER BY created_at DESC
        LIMIT %(lim)s
        """
        rows = fetchall(sql, {"ctx": ctx, "lim": limit})
        return [dict(r) for r in rows]

    def get_place_set_metrics(self, place_set_id: str) -> dict[str, Any] | None:
        sql = f"""
        SELECT *
        FROM {settings.T_PLACE_SET_METRICS}
        WHERE place_set_id = %(sid)s
        """
        row = fetchone(sql, {"sid": place_set_id}, allow_none=True)
        return dict(row) if row else None

    # ---------------------------
    # PROD: approve
    # ---------------------------

    def prod_counts(self) -> dict[str, int]:
        sql = f"""
        SELECT
          (SELECT COUNT(*) FROM {settings.T_GEO_PLACES}) AS n_places,
          (SELECT COUNT(*) FROM {settings.T_GEO_PLACE_ALIASES}) AS n_aliases,
          (SELECT COUNT(*) FROM {settings.T_NODE_PLACE_MAP}) AS n_mappings
        """
        row = fetchone(sql)
        return {k: int(row[k]) for k in row.keys()}

    def approve_place_set(self, place_set_id: str) -> ApproveSummary:
        """
        This should call your pipeline approve module in real implementation.
        For Streamlit MVP: we just verify that set exists, then return counts after approve script ran.
        """
        sql = f"SELECT place_set_id FROM {settings.T_PLACE_CANDIDATE_SETS} WHERE place_set_id = %(sid)s"
        _ = fetchone(sql, {"sid": place_set_id})

        counts = self.prod_counts()
        return ApproveSummary(
            place_set_id=place_set_id,
            n_places=counts["n_places"],
            n_aliases=counts["n_aliases"],
            n_mappings=counts["n_mappings"],
        )
