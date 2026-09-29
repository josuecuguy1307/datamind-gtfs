from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, List, Optional, Sequence

from phase3_routes.services.route_constructor.src.db.conn import db_conn, db_cursor

from ..core.models import PersistedDirectionReadinessRow, PersistedDirectionStatusSnapshot


def _as_uuid_text(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except Exception:
        return raw


def _as_int(value: Any, *, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, "", 0, "0", "false", "False", "FALSE", "no", "No"):
        return False
    return bool(value)


def _as_text(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    return raw or None


def _as_text_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        txt = value.strip()
        return [txt] if txt else []
    return []


def _as_json_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return {}
        try:
            loaded = json.loads(txt)
            return dict(loaded) if isinstance(loaded, dict) else {}
        except Exception:
            return {}
    return {}


def _as_json_list(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return []
        try:
            loaded = json.loads(txt)
        except Exception:
            return [txt]
        if isinstance(loaded, list):
            return [str(item).strip() for item in loaded if str(item or "").strip()]
        return []
    return []


def _ts_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except Exception:
        txt = str(value).strip()
        return txt or None


def _hydrate_persisted_row(row: Any) -> PersistedDirectionReadinessRow:
    payload = dict(row or {}) if isinstance(row, dict) else {}
    return PersistedDirectionReadinessRow(
        service_route_id=_as_uuid_text(payload.get("service_route_id")) or "",
        route_short_name=_as_text(payload.get("route_short_name")),
        route_label=_as_text(payload.get("route_label")),
        route_name=_as_text(payload.get("route_name")),
        operator_name=_as_text(payload.get("operator_name")),
        direction_id=_as_int(payload.get("direction_id"), default=0),
        logical_route_id=_as_uuid_text(payload.get("logical_route_id")),
        bound_route_id=_as_uuid_text(payload.get("bound_route_id")),
        anchor_route_id=_as_uuid_text(payload.get("anchor_route_id")),
        top_candidate_route_id=_as_uuid_text(payload.get("top_candidate_route_id")),
        top_candidate_scores=_as_json_dict(payload.get("top_candidate_scores")),
        proposal_payload=_as_json_dict(payload.get("proposal_payload")),
        proposal_source=_as_text(payload.get("proposal_source")),
        proposal_evaluated_at=_ts_text(payload.get("proposal_evaluated_at")),
        phase3_progress_step=_as_int(payload.get("phase3_progress_step"), default=0),
        direction_approval_status=_as_text(payload.get("direction_approval_status")) or "pending",
        geom_source=_as_text(payload.get("geom_source")) or "unknown",
        inverse_status=_as_text(payload.get("inverse_status")) or "unknown",
        search_status=_as_text(payload.get("search_status")) or "not_started",
        search_request_payload=_as_json_dict(payload.get("search_request_payload")),
        search_result_payload=_as_json_dict(payload.get("search_result_payload")),
        dispatched_route_ids=_as_json_list(payload.get("dispatched_route_ids")),
        materialized_route_ids=_as_json_list(payload.get("materialized_route_ids")),
        search_started_at=_ts_text(payload.get("search_started_at")),
        search_finished_at=_ts_text(payload.get("search_finished_at")),
        search_error=_as_text(payload.get("search_error")),
        manual_required=_as_bool(payload.get("manual_required")),
        direction_ready=_as_bool(payload.get("direction_ready")),
        blocker_codes=_as_text_list(payload.get("blocker_codes")),
        blocker_messages=_as_text_list(payload.get("blocker_messages")),
        evidence_summary=_as_json_dict(payload.get("evidence_summary")),
        analysis_version=_as_text(payload.get("analysis_version")),
        last_evaluated_at=_ts_text(payload.get("last_evaluated_at")),
        created_at=_ts_text(payload.get("created_at")),
        updated_at=_ts_text(payload.get("updated_at")),
    )


def upsert_inverse_direction_status_rows(
    rows: Iterable[PersistedDirectionStatusSnapshot],
) -> int:
    row_list = list(rows or [])
    if not row_list:
        return 0

    with db_conn() as conn:
        with db_cursor(conn) as cur:
            for row in row_list:
                cur.execute(
                    """
                    INSERT INTO route_work.inverse_direction_status (
                      service_route_id,
                      direction_id,
                      anchor_route_id,
                      bound_route_id,
                      top_candidate_route_id,
                      top_candidate_scores,
                      proposal_payload,
                      proposal_source,
                      proposal_evaluated_at,
                      inverse_status,
                      search_status,
                      search_request_payload,
                      search_result_payload,
                      dispatched_route_ids,
                      materialized_route_ids,
                      search_started_at,
                      search_finished_at,
                      search_error,
                      manual_required,
                      direction_ready,
                      blocker_codes,
                      blocker_messages,
                      evidence_summary,
                      analysis_version,
                      last_evaluated_at
                    )
                    VALUES (
                      %s::uuid,
                      %s,
                      %s::uuid,
                      %s::uuid,
                      %s::uuid,
                      %s::jsonb,
                      %s::jsonb,
                      %s,
                      %s::timestamptz,
                      %s,
                      %s,
                      %s::jsonb,
                      %s::jsonb,
                      %s::jsonb,
                      %s::jsonb,
                      %s::timestamptz,
                      %s::timestamptz,
                      %s,
                      %s,
                      %s,
                      %s::text[],
                      %s::text[],
                      %s::jsonb,
                      %s,
                      %s::timestamptz
                    )
                    ON CONFLICT (service_route_id, direction_id) DO UPDATE SET
                      anchor_route_id = EXCLUDED.anchor_route_id,
                      bound_route_id = EXCLUDED.bound_route_id,
                      top_candidate_route_id = CASE
                        WHEN EXCLUDED.top_candidate_route_id IS NULL
                          AND EXCLUDED.top_candidate_scores = '{}'::jsonb
                          AND EXCLUDED.proposal_payload = '{}'::jsonb
                          AND EXCLUDED.proposal_source IS NULL
                          AND EXCLUDED.proposal_evaluated_at IS NULL
                        THEN route_work.inverse_direction_status.top_candidate_route_id
                        ELSE EXCLUDED.top_candidate_route_id
                      END,
                      top_candidate_scores = CASE
                        WHEN EXCLUDED.top_candidate_route_id IS NULL
                          AND EXCLUDED.top_candidate_scores = '{}'::jsonb
                          AND EXCLUDED.proposal_payload = '{}'::jsonb
                          AND EXCLUDED.proposal_source IS NULL
                          AND EXCLUDED.proposal_evaluated_at IS NULL
                        THEN route_work.inverse_direction_status.top_candidate_scores
                        ELSE EXCLUDED.top_candidate_scores
                      END,
                      proposal_payload = CASE
                        WHEN EXCLUDED.top_candidate_route_id IS NULL
                          AND EXCLUDED.top_candidate_scores = '{}'::jsonb
                          AND EXCLUDED.proposal_payload = '{}'::jsonb
                          AND EXCLUDED.proposal_source IS NULL
                          AND EXCLUDED.proposal_evaluated_at IS NULL
                        THEN route_work.inverse_direction_status.proposal_payload
                        ELSE EXCLUDED.proposal_payload
                      END,
                      proposal_source = CASE
                        WHEN EXCLUDED.top_candidate_route_id IS NULL
                          AND EXCLUDED.top_candidate_scores = '{}'::jsonb
                          AND EXCLUDED.proposal_payload = '{}'::jsonb
                          AND EXCLUDED.proposal_source IS NULL
                          AND EXCLUDED.proposal_evaluated_at IS NULL
                        THEN route_work.inverse_direction_status.proposal_source
                        ELSE EXCLUDED.proposal_source
                      END,
                      proposal_evaluated_at = CASE
                        WHEN EXCLUDED.top_candidate_route_id IS NULL
                          AND EXCLUDED.top_candidate_scores = '{}'::jsonb
                          AND EXCLUDED.proposal_payload = '{}'::jsonb
                          AND EXCLUDED.proposal_source IS NULL
                          AND EXCLUDED.proposal_evaluated_at IS NULL
                        THEN route_work.inverse_direction_status.proposal_evaluated_at
                        ELSE EXCLUDED.proposal_evaluated_at
                      END,
                      inverse_status = EXCLUDED.inverse_status,
                      search_status = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.search_status
                        ELSE EXCLUDED.search_status
                      END,
                      search_request_payload = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.search_request_payload
                        ELSE EXCLUDED.search_request_payload
                      END,
                      search_result_payload = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.search_result_payload
                        ELSE EXCLUDED.search_result_payload
                      END,
                      dispatched_route_ids = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.dispatched_route_ids
                        ELSE EXCLUDED.dispatched_route_ids
                      END,
                      materialized_route_ids = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.materialized_route_ids
                        ELSE EXCLUDED.materialized_route_ids
                      END,
                      search_started_at = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.search_started_at
                        ELSE EXCLUDED.search_started_at
                      END,
                      search_finished_at = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.search_finished_at
                        ELSE EXCLUDED.search_finished_at
                      END,
                      search_error = CASE
                        WHEN EXCLUDED.search_request_payload = '{}'::jsonb
                          AND EXCLUDED.search_result_payload = '{}'::jsonb
                          AND EXCLUDED.dispatched_route_ids = '[]'::jsonb
                          AND EXCLUDED.materialized_route_ids = '[]'::jsonb
                          AND EXCLUDED.search_started_at IS NULL
                          AND EXCLUDED.search_finished_at IS NULL
                          AND EXCLUDED.search_error IS NULL
                        THEN route_work.inverse_direction_status.search_error
                        ELSE EXCLUDED.search_error
                      END,
                      manual_required = EXCLUDED.manual_required,
                      direction_ready = EXCLUDED.direction_ready,
                      blocker_codes = EXCLUDED.blocker_codes,
                      blocker_messages = EXCLUDED.blocker_messages,
                      evidence_summary = EXCLUDED.evidence_summary,
                      analysis_version = EXCLUDED.analysis_version,
                      last_evaluated_at = EXCLUDED.last_evaluated_at,
                      updated_at = now()
                    """,
                    (
                        str(row.service_route_id),
                        int(row.direction_id),
                        _as_uuid_text(row.anchor_route_id),
                        _as_uuid_text(row.bound_route_id),
                        _as_uuid_text(row.top_candidate_route_id),
                        json.dumps(dict(row.top_candidate_scores or {}), ensure_ascii=False),
                        json.dumps(dict(row.proposal_payload or {}), ensure_ascii=False),
                        _as_text(row.proposal_source),
                        _as_text(row.proposal_evaluated_at),
                        str(row.inverse_status or "unknown").strip() or "unknown",
                        str(row.search_status or "not_started").strip() or "not_started",
                        json.dumps(dict(row.search_request_payload or {}), ensure_ascii=False),
                        json.dumps(dict(row.search_result_payload or {}), ensure_ascii=False),
                        json.dumps(list(row.dispatched_route_ids or []), ensure_ascii=False),
                        json.dumps(list(row.materialized_route_ids or []), ensure_ascii=False),
                        _as_text(row.search_started_at),
                        _as_text(row.search_finished_at),
                        _as_text(row.search_error),
                        bool(row.manual_required),
                        bool(row.direction_ready),
                        list(row.blocker_codes or []),
                        list(row.blocker_messages or []),
                        json.dumps(dict(row.evidence_summary or {}), ensure_ascii=False),
                        _as_text(row.analysis_version),
                        _as_text(row.last_evaluated_at),
                    ),
                )
    return len(row_list)


def list_persisted_direction_readiness_rows(
    *,
    service_route_id: Optional[str] = None,
    service_route_ids: Optional[Sequence[str]] = None,
    include_ready: bool = True,
    limit: Optional[int] = 100,
) -> List[PersistedDirectionReadinessRow]:
    params: List[Any] = []
    where = ["1=1"]

    sid = _as_uuid_text(service_route_id)
    if sid:
        where.append("service_route_id = %s")
        params.append(sid)

    sid_list = [_as_uuid_text(value) for value in list(service_route_ids or [])]
    sid_list = [value for value in sid_list if value]
    if sid_list:
        where.append("service_route_id = ANY(%s::text[])")
        params.append(sid_list)

    if not include_ready:
        where.append("COALESCE(direction_ready, FALSE) = FALSE")

    sql = """
        SELECT
          service_route_id,
          route_short_name,
          route_label,
          route_name,
          operator_name,
          direction_id,
          logical_route_id,
          bound_route_id,
          anchor_route_id,
          top_candidate_route_id,
          top_candidate_scores,
          proposal_payload,
          proposal_source,
          proposal_evaluated_at,
          phase3_progress_step,
          direction_approval_status,
          geom_source,
          inverse_status,
          search_status,
          search_request_payload,
          search_result_payload,
          dispatched_route_ids,
          materialized_route_ids,
          search_started_at,
          search_finished_at,
          search_error,
          manual_required,
          direction_ready,
          blocker_codes,
          blocker_messages,
          evidence_summary,
          analysis_version,
          last_evaluated_at,
          created_at,
          updated_at
        FROM route_work.v_direction_readiness
        WHERE
    """ + " AND ".join(where) + """
        ORDER BY
          COALESCE(last_evaluated_at, updated_at, created_at) DESC NULLS LAST,
          service_route_id,
          direction_id
    """
    if limit is not None and not sid and not sid_list:
        sql += " LIMIT %s"
        params.append(max(1, int(limit)))

    with db_conn() as conn:
        with db_cursor(conn) as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
    return [_hydrate_persisted_row(row) for row in rows]


def get_persisted_direction_readiness_row(
    *,
    service_route_id: str,
    direction_id: int,
) -> Optional[PersistedDirectionReadinessRow]:
    sid = _as_uuid_text(service_route_id)
    did = _as_int(direction_id, default=-1)
    if not sid or did not in (0, 1):
        return None

    with db_conn() as conn:
        with db_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                  service_route_id,
                  route_short_name,
                  route_label,
                  route_name,
                  operator_name,
                  direction_id,
                  logical_route_id,
                  bound_route_id,
                  anchor_route_id,
                  top_candidate_route_id,
                  top_candidate_scores,
                  proposal_payload,
                  proposal_source,
                  proposal_evaluated_at,
                  phase3_progress_step,
                  direction_approval_status,
                  geom_source,
                  inverse_status,
                  search_status,
                  search_request_payload,
                  search_result_payload,
                  dispatched_route_ids,
                  materialized_route_ids,
                  search_started_at,
                  search_finished_at,
                  search_error,
                  manual_required,
                  direction_ready,
                  blocker_codes,
                  blocker_messages,
                  evidence_summary,
                  analysis_version,
                  last_evaluated_at,
                  created_at,
                  updated_at
                FROM route_work.v_direction_readiness
                WHERE service_route_id = %s
                  AND direction_id = %s
                LIMIT 1
                """,
                (sid, did),
            )
            row = cur.fetchone() or {}
    if not row:
        return None
    return _hydrate_persisted_row(row)
