from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from datamind_console.db import db_conn, fetch_one, fetch_all, exec_sql

Json = Dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AuditService:
    """
    Audit + labeling (phase decisions) service.
    Aligned with datamind_console.db API (db_conn + fetch_one/fetch_all/exec_sql).
    """

    def __init__(
        self,
        *,
        t_users: str = "console.users",
        t_sessions: str = "console.sessions",          # <-- match your schema
        t_audit: str = "console.audit_events",
        t_decisions: str = "console.phase_decisions",
        t_workspace: str = "console.workspace_state",
    ):
        self.T_USERS = t_users
        self.T_SESSIONS = t_sessions
        self.T_AUDIT = t_audit
        self.T_DECISIONS = t_decisions
        self.T_WORKSPACE = t_workspace

    # -----------------------------
    # AUDIT EVENTS
    # -----------------------------
    def log_event(
        self,
        *,
        event_type: str,
        user_id: Optional[str] = None,
        phase: Optional[int] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        status: str = "ok",
        message: Optional[str] = None,
        meta: Optional[Json] = None,
        dedup_key: Optional[str] = None,
    ) -> str:
        meta = meta or {}

        sql = f"""
        INSERT INTO {self.T_AUDIT} (
          dedup_key, event_type, phase, entity_type, entity_id,
          status, message, meta, user_id
        )
        VALUES (
          %(dedup_key)s, %(event_type)s, %(phase)s, %(entity_type)s, %(entity_id)s,
          %(status)s, %(message)s, %(meta)s::jsonb, %(user_id)s::uuid
        )
        ON CONFLICT (dedup_key)
        DO UPDATE SET dedup_key = {self.T_AUDIT}.dedup_key
        RETURNING event_id
        """

        with db_conn() as conn:
            row = fetch_one(
                conn,
                sql,
                {
                    "dedup_key": dedup_key,
                    "event_type": event_type,
                    "phase": phase,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "status": status,
                    "message": message,
                    "meta": meta,
                    "user_id": user_id,
                },
            ) or {}

        return str(row.get("event_id"))

    def list_events(
        self,
        *,
        limit: int = 100,
        phase: Optional[int] = None,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        event_type: Optional[str] = None,
        since_days: Optional[int] = None,
    ) -> List[Json]:
        where: List[str] = []
        params: Dict[str, Any] = {"lim": int(limit)}

        if phase is not None:
            where.append("phase = %(phase)s")
            params["phase"] = int(phase)

        if user_id is not None:
            where.append("user_id = %(user_id)s::uuid")
            params["user_id"] = str(user_id)

        if status is not None:
            where.append("status = %(status)s")
            params["status"] = status

        if event_type is not None:
            where.append("event_type = %(event_type)s")
            params["event_type"] = event_type

        if since_days is not None:
            since = _utc_now() - timedelta(days=int(since_days))
            where.append("created_at >= %(since)s")
            params["since"] = since

        where_sql = "WHERE " + " AND ".join(where) if where else ""

        sql = f"""
        SELECT
          event_id, event_type, phase, entity_type, entity_id,
          status, message, meta, user_id, created_at
        FROM {self.T_AUDIT}
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %(lim)s
        """

        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, params)

    # -----------------------------
    # PHASE DECISIONS (labels)
    # -----------------------------
    def log_decision(
        self,
        *,
        phase: int,
        item_id: str,
        decision: str,
        user_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
        score_at_decision: Optional[float] = None,
        reason_code: Optional[str] = None,
        notes: Optional[str] = None,
        dedup_key: Optional[str] = None,
    ) -> str:
        d = decision.strip().upper()

        if d in ("REJECT", "EDIT") and not reason_code:
            raise ValueError("reason_code is required for REJECT/EDIT")

        sql = f"""
        INSERT INTO {self.T_DECISIONS} (
          dedup_key, phase, item_id, candidate_id, score_at_decision,
          decision, reason_code, notes, user_id
        )
        VALUES (
          %(dedup_key)s, %(phase)s, %(item_id)s, %(candidate_id)s, %(score_at_decision)s,
          %(decision)s, %(reason_code)s, %(notes)s, %(user_id)s::uuid
        )
        ON CONFLICT (dedup_key)
        DO UPDATE SET dedup_key = {self.T_DECISIONS}.dedup_key
        RETURNING decision_id
        """

        with db_conn() as conn:
            row = fetch_one(
                conn,
                sql,
                {
                    "dedup_key": dedup_key,
                    "phase": int(phase),
                    "item_id": item_id,
                    "candidate_id": candidate_id,
                    "score_at_decision": score_at_decision,
                    "decision": d,
                    "reason_code": reason_code,
                    "notes": notes,
                    "user_id": user_id,
                },
            ) or {}

        return str(row.get("decision_id"))

    def list_decisions(
        self,
        *,
        limit: int = 200,
        phase: Optional[int] = None,
        decision: Optional[str] = None,
        user_id: Optional[str] = None,
        reason_code: Optional[str] = None,
        since_days: Optional[int] = None,
    ) -> List[Json]:
        where: List[str] = []
        params: Dict[str, Any] = {"lim": int(limit)}

        if phase is not None:
            where.append("phase = %(phase)s")
            params["phase"] = int(phase)

        if decision is not None:
            where.append("decision = %(decision)s")
            params["decision"] = decision.strip().upper()

        if user_id is not None:
            where.append("user_id = %(user_id)s::uuid")
            params["user_id"] = str(user_id)

        if reason_code is not None:
            where.append("reason_code = %(reason_code)s")
            params["reason_code"] = reason_code

        if since_days is not None:
            since = _utc_now() - timedelta(days=int(since_days))
            where.append("created_at >= %(since)s")
            params["since"] = since

        where_sql = "WHERE " + " AND ".join(where) if where else ""

        sql = f"""
        SELECT
          decision_id, phase, item_id, candidate_id, score_at_decision,
          decision, reason_code, notes, user_id, created_at
        FROM {self.T_DECISIONS}
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %(lim)s
        """

        with db_conn(readonly=True) as conn:
            return fetch_all(conn, sql, params)

    # -----------------------------
    # WORKSPACE STATE (continue UX)
    # -----------------------------
    def upsert_workspace_state(
        self,
        *,
        user_id: str,
        current_phase: Optional[int] = None,
        current_subtab: Optional[str] = None,
        current_route_id: Optional[str] = None,
        current_stop_id: Optional[str] = None,
        last_candidate_id: Optional[str] = None,
    ) -> None:
        sql = f"""
        INSERT INTO {self.T_WORKSPACE}
          (user_id, current_phase, current_subtab, current_route_id, current_stop_id, last_candidate_id, updated_at)
        VALUES
          (%(user_id)s::uuid, %(current_phase)s, %(current_subtab)s, %(current_route_id)s, %(current_stop_id)s, %(last_candidate_id)s, NOW())
        ON CONFLICT (user_id)
        DO UPDATE SET
          current_phase = EXCLUDED.current_phase,
          current_subtab = EXCLUDED.current_subtab,
          current_route_id = EXCLUDED.current_route_id,
          current_stop_id = EXCLUDED.current_stop_id,
          last_candidate_id = EXCLUDED.last_candidate_id,
          updated_at = NOW()
        """

        with db_conn() as conn:
            exec_sql(
                conn,
                sql,
                {
                    "user_id": str(user_id),
                    "current_phase": current_phase,
                    "current_subtab": current_subtab,
                    "current_route_id": current_route_id,
                    "current_stop_id": current_stop_id,
                    "last_candidate_id": last_candidate_id,
                },
            )

    def get_workspace_state(self, user_id: str) -> Optional[Json]:
        sql = f"""
        SELECT
          user_id, current_phase, current_subtab, current_route_id,
          current_stop_id, last_candidate_id, updated_at
        FROM {self.T_WORKSPACE}
        WHERE user_id = %(user_id)s::uuid
        """

        with db_conn(readonly=True) as conn:
            return fetch_one(conn, sql, {"user_id": str(user_id)})
