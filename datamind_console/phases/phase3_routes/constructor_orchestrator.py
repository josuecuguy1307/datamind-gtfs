"""
Route Constructor Orchestrator Engine
=====================================

Self-contained orchestration engine for the Phase 3 manual route builder system.
Handles preflight classification, draft lifecycle, export safety, idempotency,
metrics collection, and hint provenance.

Usage::

    from datamind_console.phases.phase3_routes.constructor_orchestrator import (
        ConstructorOrchestrator,
        ConstructorMetrics,
    )

    orch = ConstructorOrchestrator()
    classification = orch.classify_case(payload)
    result = orch.safe_export(payload)
    metrics = orch.collect_metrics()
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Sequence

from datamind_console.db.db import db_conn, fetch_all, fetch_one, exec_sql
from datamind_console.phases.phase3_routes.manual_sequence_contracts import (
    ManualSequenceExportRequest,
    ManualSequenceValidation,
    normalize_manual_sequence_export_request,
    validate_manual_sequence_rows,
)

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PREFLIGHT_CLASSES = (
    "true_missing_ready",
    "likely_already_represented",
    "duplicate_risk",
    "needs_cleanup_first",
    "missing_evidence",
    "operator_review_required",
)

CONSTRUCTOR_STATUSES = (
    "draft_saved",
    "draft_ready",
    "exported_sequence_ready",
    "awaiting_sequence_approval",
    "awaiting_geometry",
    "awaiting_review",
    "blocked_missing_evidence",
    "completed",
    "abandoned",
)

# Valid forward transitions: from_status -> set of allowed next statuses
_VALID_TRANSITIONS: Dict[str, set] = {
    "draft_saved":                  {"draft_ready", "abandoned"},
    "draft_ready":                  {"exported_sequence_ready", "draft_saved", "abandoned"},
    "exported_sequence_ready":      {"awaiting_sequence_approval", "draft_ready", "abandoned"},
    "awaiting_sequence_approval":   {"awaiting_geometry", "draft_ready", "blocked_missing_evidence", "abandoned"},
    "awaiting_geometry":            {"awaiting_review", "blocked_missing_evidence", "abandoned"},
    "awaiting_review":              {"completed", "draft_ready", "abandoned"},
    "blocked_missing_evidence":     {"draft_ready", "awaiting_geometry", "abandoned"},
    "completed":                    set(),
    "abandoned":                    {"draft_saved"},  # allow resurrection
}

# Hint provenance priority (higher = wins merge)
_HINT_PRIORITY = {
    "manual": 100,
    "constructor": 80,
    "gap_analysis": 60,
    "llm": 40,
    "default": 20,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError):
        return None


def _safe_json(obj: Any) -> str:
    """JSON-serialize with fallback for datetimes and UUIDs."""
    def _default(o: Any) -> Any:
        if isinstance(o, (datetime,)):
            return o.isoformat()
        if isinstance(o, uuid.UUID):
            return str(o)
        return str(o)
    return json.dumps(obj, default=_default, ensure_ascii=False)


def _row_to_dict(row: Any) -> dict:
    """Coerce a psycopg2 RealDictRow (or dict) into a plain dict."""
    if row is None:
        return {}
    return dict(row)


# ---------------------------------------------------------------------------
# ConstructorMetrics
# ---------------------------------------------------------------------------

class ConstructorMetrics:
    """Collects and reports constructor metrics.

    All state is in-memory; callers can persist via ``to_json()``.
    """

    def __init__(self) -> None:
        self.counters: Dict[str, int] = Counter()
        self.events: List[Dict[str, Any]] = []
        self._failure_events: List[Dict[str, Any]] = []

    # -- recording --

    def record_event(self, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
        entry = {
            "event_type": event_type,
            "ts": _now_iso(),
            "payload": payload or {},
        }
        self.events.append(entry)
        self.counters[event_type] = self.counters.get(event_type, 0) + 1
        if event_type.startswith("failure_") or event_type.startswith("error_"):
            self._failure_events.append(entry)

    def increment(self, counter_name: str, amount: int = 1) -> None:
        self.counters[counter_name] = self.counters.get(counter_name, 0) + amount

    # -- reporting --

    def summary(self) -> Dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "total_events": len(self.events),
            "total_failures": len(self._failure_events),
            "snapshot_ts": _now_iso(),
        }

    def failure_classes(self) -> List[Dict[str, Any]]:
        """Group failures by event_type with counts."""
        groups: Dict[str, int] = Counter()
        for ev in self._failure_events:
            groups[ev["event_type"]] += 1
        return [
            {"failure_class": k, "count": v}
            for k, v in sorted(groups.items(), key=lambda x: -x[1])
        ]

    def to_json(self) -> str:
        return _safe_json({
            "summary": self.summary(),
            "failure_classes": self.failure_classes(),
            "events": self.events,
        })


# ---------------------------------------------------------------------------
# ConstructorOrchestrator
# ---------------------------------------------------------------------------

class ConstructorOrchestrator:
    """Main orchestrator for the Phase 3 manual route constructor workflow.

    Parameters
    ----------
    client : optional
        A ``Phase3Client`` instance.  When provided, some operations
        delegate to the client for DB writes that already have battle-tested
        implementations.  When ``None``, the orchestrator uses its own
        ``db_conn`` / ``fetch_*`` helpers directly.
    metrics : optional
        An external ``ConstructorMetrics`` instance to share across callers.
        If ``None``, a fresh one is created.
    """

    def __init__(
        self,
        client: Any = None,
        metrics: Optional[ConstructorMetrics] = None,
    ) -> None:
        self._client = client
        self.metrics = metrics or ConstructorMetrics()

    # ======================================================================
    # 1. Preflight Classification
    # ======================================================================

    def classify_case(self, payload: dict) -> dict:
        """Classify an incoming missing-route case before any work.

        Parameters
        ----------
        payload : dict
            Expected keys (all optional except ``name_hint`` or ``ref_hint``):
            - name_hint, ref_hint, operator_hint, variant_hint
            - service_route_id, direction_id
            - ordered_stop_ids (list[str])  -- pre-selected stops, if any
            - coverage_gap_id

        Returns
        -------
        dict with keys:
            classification, reasons (list[str]), confidence (float 0-1),
            provenance (str), payload echo.
        """
        classification = "true_missing_ready"
        reasons: List[str] = []
        confidence = 1.0

        name_hint = (payload.get("name_hint") or "").strip()
        ref_hint = (payload.get("ref_hint") or "").strip()
        operator_hint = (payload.get("operator_hint") or "").strip()
        ordered_stop_ids = list(payload.get("ordered_stop_ids") or [])
        service_route_id = _uuid_str(payload.get("service_route_id"))
        coverage_gap_id = _uuid_str(payload.get("coverage_gap_id"))

        # --- Check catalog for duplicates ---
        catalog_matches = self._find_catalog_matches(
            name_hint=name_hint,
            ref_hint=ref_hint,
            operator_hint=operator_hint,
            service_route_id=service_route_id,
        )

        if catalog_matches.get("exact_match"):
            classification = "likely_already_represented"
            reasons.append(
                f"Exact match found in catalog: route_id={catalog_matches['exact_match']}"
            )
            confidence = 0.9
        elif catalog_matches.get("near_matches"):
            count = len(catalog_matches["near_matches"])
            classification = "duplicate_risk"
            reasons.append(
                f"{count} near-match(es) in catalog by ref/name/operator"
            )
            confidence = 0.7

        # --- Check stop evidence ---
        if not ordered_stop_ids and classification == "true_missing_ready":
            # No stops pre-selected: check if coverage gap has evidence
            if coverage_gap_id:
                gap_info = self._get_gap_evidence_summary(coverage_gap_id)
                if gap_info.get("approved_stop_count", 0) < 2:
                    classification = "missing_evidence"
                    reasons.append(
                        "Coverage gap has fewer than 2 approved stops; "
                        "cannot build sequence without manual stop selection."
                    )
                    confidence = 0.85
            else:
                classification = "missing_evidence"
                reasons.append("No ordered_stop_ids and no coverage_gap_id provided.")
                confidence = 0.95

        # --- Check for data quality issues ---
        if ordered_stop_ids:
            quality_issues = self._check_stop_quality(ordered_stop_ids)
            if quality_issues:
                if classification == "true_missing_ready":
                    classification = "needs_cleanup_first"
                reasons.extend(quality_issues)
                confidence = min(confidence, 0.6)

        # --- Check if operator is ambiguous ---
        if not operator_hint and not ref_hint and not name_hint:
            if classification == "true_missing_ready":
                classification = "operator_review_required"
                reasons.append(
                    "No name_hint, ref_hint, or operator_hint -- "
                    "cannot determine route identity without at least one."
                )
                confidence = 0.5

        self.metrics.record_event("preflight_classification", {
            "classification": classification,
            "confidence": confidence,
        })

        return {
            "classification": classification,
            "reasons": reasons,
            "confidence": round(confidence, 3),
            "provenance": "constructor_orchestrator.classify_case",
            "catalog_matches": catalog_matches,
            "payload_echo": {
                "name_hint": name_hint or None,
                "ref_hint": ref_hint or None,
                "operator_hint": operator_hint or None,
                "stop_count": len(ordered_stop_ids),
                "service_route_id": service_route_id,
                "coverage_gap_id": coverage_gap_id,
            },
        }

    # ======================================================================
    # 2. Draft Management
    # ======================================================================

    def list_drafts(
        self,
        *,
        limit: int = 50,
        status_filter: Optional[str] = None,
    ) -> List[dict]:
        """List manual sequence drafts, newest first.

        Parameters
        ----------
        limit : int
            Max rows to return.
        status_filter : str or None
            If provided, filter to drafts whose ``constructor_status``
            matches (stored in the draft's JSON metadata, or as a column
            if the schema supports it).
        """
        try:
            with db_conn(readonly=True) as conn:
                base_sql = """
                    SELECT
                        draft_id::text,
                        route_id::text,
                        service_route_id::text,
                        direction_id,
                        source,
                        ordered_stop_ids,
                        is_loop,
                        name_hint,
                        operator_hint,
                        variant_hint,
                        created_by,
                        created_at,
                        updated_at
                    FROM route_work.manual_sequence_drafts
                """
                params: list = []
                if status_filter:
                    # status is stored in-memory / session, not in this table,
                    # so we do post-filter below.  Still fetch all.
                    pass
                base_sql += " ORDER BY updated_at DESC LIMIT %s"
                params.append(int(limit))
                rows = fetch_all(conn, base_sql, params)
                # Coerce array fields
                result = []
                for r in rows:
                    d = _row_to_dict(r)
                    d["ordered_stop_count"] = len(d.get("ordered_stop_ids") or [])
                    # Attach constructor_status from status tracker
                    rid = d.get("route_id")
                    d["constructor_status"] = self._read_constructor_status(conn, rid)
                    result.append(d)
                if status_filter:
                    result = [
                        r for r in result
                        if r.get("constructor_status") == status_filter
                    ]
                return result
        except Exception as exc:
            _LOG.warning("list_drafts failed: %s", exc)
            self.metrics.record_event("error_list_drafts", {"error": str(exc)})
            return []

    def load_draft(self, draft_id: str) -> dict:
        """Load a single draft by ID."""
        uid = _uuid_str(draft_id)
        if not uid:
            return {"error": "Invalid draft_id"}
        try:
            with db_conn(readonly=True) as conn:
                row = fetch_one(
                    conn,
                    """
                    SELECT
                        draft_id::text,
                        route_id::text,
                        service_route_id::text,
                        direction_id,
                        source,
                        ordered_stop_ids,
                        ordered_node_ids,
                        ordered_coords,
                        is_loop,
                        name_hint,
                        operator_hint,
                        variant_hint,
                        created_by,
                        created_at,
                        updated_at
                    FROM route_work.manual_sequence_drafts
                    WHERE draft_id = %s::uuid
                    """,
                    (uid,),
                )
                if not row:
                    return {"error": f"Draft not found: {uid}"}
                d = _row_to_dict(row)
                d["constructor_status"] = self._read_constructor_status(
                    conn, d.get("route_id"),
                )
                return d
        except Exception as exc:
            _LOG.warning("load_draft failed: %s", exc)
            self.metrics.record_event("error_load_draft", {"error": str(exc)})
            return {"error": str(exc)}

    def update_draft(self, draft_id: str, payload: dict) -> dict:
        """Update an existing draft (partial update).

        Updatable fields: ordered_stop_ids, ordered_node_ids,
        ordered_coords, is_loop, name_hint, operator_hint, variant_hint.
        """
        uid = _uuid_str(draft_id)
        if not uid:
            return {"error": "Invalid draft_id"}

        # Build dynamic SET clause from allowed fields
        allowed = {
            "ordered_stop_ids", "ordered_node_ids", "ordered_coords",
            "is_loop", "name_hint", "operator_hint", "variant_hint",
        }
        sets: List[str] = []
        values: list = []
        for key in allowed:
            if key in payload:
                val = payload[key]
                if key == "ordered_coords":
                    sets.append(f"{key} = %s::jsonb")
                    values.append(json.dumps(val, ensure_ascii=False))
                elif key in ("ordered_stop_ids", "ordered_node_ids"):
                    sets.append(f"{key} = %s::uuid[]")
                    values.append([str(x) for x in (val or [])])
                elif key == "is_loop":
                    sets.append(f"{key} = %s")
                    values.append(bool(val))
                else:
                    sets.append(f"{key} = %s")
                    values.append(val)

        if not sets:
            return {"error": "No updatable fields provided"}

        sets.append("updated_at = now()")
        values.append(uid)

        try:
            with db_conn() as conn:
                sql = (
                    "UPDATE route_work.manual_sequence_drafts SET "
                    + ", ".join(sets)
                    + " WHERE draft_id = %s::uuid"
                    + " RETURNING draft_id::text, updated_at"
                )
                row = fetch_one(conn, sql, values)
                if not row:
                    return {"error": f"Draft not found or no change: {uid}"}
                self.metrics.record_event("draft_updated", {"draft_id": uid})
                return {"draft_id": uid, "updated_at": str(row.get("updated_at", ""))}
        except Exception as exc:
            _LOG.warning("update_draft failed: %s", exc)
            self.metrics.record_event("error_update_draft", {"error": str(exc)})
            return {"error": str(exc)}

    def resume_draft(self, draft_id: str) -> dict:
        """Load a draft and restore it to a session-ready state.

        Returns the full draft dict plus a ``resume_status`` indicator.
        """
        draft = self.load_draft(draft_id)
        if "error" in draft:
            return draft

        route_id = draft.get("route_id")
        current_status = draft.get("constructor_status") or "draft_saved"

        # If it was abandoned or blocked, move back to draft_saved
        if current_status in ("abandoned", "blocked_missing_evidence"):
            self.advance_status(route_id, "draft_saved")
            draft["constructor_status"] = "draft_saved"

        draft["resume_status"] = "ready"
        draft["resumed_at"] = _now_iso()
        self.metrics.record_event("draft_resumed", {
            "draft_id": draft_id,
            "from_status": current_status,
        })
        return draft

    def delete_draft(self, draft_id: str) -> dict:
        """Soft-delete a draft (sets source to 'deleted')."""
        uid = _uuid_str(draft_id)
        if not uid:
            return {"error": "Invalid draft_id"}
        try:
            with db_conn() as conn:
                affected = exec_sql(
                    conn,
                    """
                    UPDATE route_work.manual_sequence_drafts
                    SET source = 'deleted', updated_at = now()
                    WHERE draft_id = %s::uuid AND source != 'deleted'
                    """,
                    (uid,),
                )
                if affected == 0:
                    return {"error": f"Draft not found or already deleted: {uid}"}
                self.metrics.record_event("draft_deleted", {"draft_id": uid})
                return {"draft_id": uid, "deleted": True}
        except Exception as exc:
            _LOG.warning("delete_draft failed: %s", exc)
            self.metrics.record_event("error_delete_draft", {"error": str(exc)})
            return {"error": str(exc)}

    # ======================================================================
    # 3. Export with Safety
    # ======================================================================

    def safe_export(
        self,
        payload: dict,
        *,
        min_stops: int = 2,
        jump_warn_m: float = 3500.0,
    ) -> dict:
        """Atomic export with preflight + idempotency checks.

        Steps:
          1. Normalize the payload via contracts
          2. Run idempotency check -- refuse if duplicate export exists
          3. Validate stop linkage (min stops, jump distance)
          4. Classify the case (preflight)
          5. Refuse partial exports (all-or-nothing)
          6. Delegate to client.export_manual_sequence_to_phase3 or
             perform the export directly
          7. Record provenance and metrics

        Returns
        -------
        dict with export result or error information.
        """
        export_ts = _now_iso()
        self.metrics.increment("export_attempts")

        # -- 1. Normalize --
        try:
            req = normalize_manual_sequence_export_request(payload)
        except (ValueError, TypeError) as exc:
            self.metrics.record_event("failure_export_normalize", {"error": str(exc)})
            return {"ok": False, "error": f"Normalization failed: {exc}"}

        # -- 2. Idempotency --
        rerun_check = self.check_rerun_safety(payload)
        if rerun_check.get("existing_export_id"):
            self.metrics.record_event("export_skipped_idempotent", {
                "existing_export_id": rerun_check["existing_export_id"],
            })
            return {
                "ok": False,
                "error": "Duplicate export detected",
                "existing_export_id": rerun_check["existing_export_id"],
                "rerun_advice": rerun_check.get("advice", "skip"),
            }

        # -- 3. Validate stop linkage --
        resolved_rows = self._resolve_stop_rows(req.ordered_stop_ids)
        validation = validate_manual_sequence_rows(
            ordered_stop_ids=req.ordered_stop_ids,
            resolved_rows=resolved_rows,
            is_loop=req.is_loop,
            min_stops=min_stops,
            jump_warn_m=jump_warn_m,
        )
        if validation.errors:
            self.metrics.record_event("failure_export_validation", {
                "errors": validation.errors,
            })
            return {
                "ok": False,
                "error": "Validation failed",
                "errors": validation.errors,
                "warnings": validation.warnings,
            }

        # -- 4. Preflight classification --
        classification = self.classify_case(payload)
        cls_label = classification["classification"]
        if cls_label in ("likely_already_represented", "duplicate_risk"):
            _LOG.warning(
                "safe_export proceeding despite classification=%s for ref=%s name=%s",
                cls_label,
                req.ordered_stop_ids[:2] if req.ordered_stop_ids else "?",
                req.name_hint,
            )
            # We still allow export but record a warning
            self.metrics.record_event("export_warning_classification", {
                "classification": cls_label,
            })

        if cls_label == "needs_cleanup_first":
            self.metrics.record_event("failure_export_cleanup_required", {
                "reasons": classification["reasons"],
            })
            return {
                "ok": False,
                "error": "Data quality issues must be resolved first",
                "classification": classification,
            }

        # -- 5. Refuse partial (all stops must be resolved) --
        if len(validation.ordered_stops) != len(req.ordered_stop_ids):
            missing = len(req.ordered_stop_ids) - len(validation.ordered_stops)
            self.metrics.record_event("failure_export_partial", {
                "requested": len(req.ordered_stop_ids),
                "resolved": len(validation.ordered_stops),
            })
            return {
                "ok": False,
                "error": f"{missing} stop(s) could not be resolved -- refusing partial export",
                "warnings": validation.warnings,
            }

        # -- 6. Perform export --
        if self._client is not None:
            try:
                result = self._client.export_manual_sequence_to_phase3(
                    payload,
                    min_stops=min_stops,
                    jump_warn_m=jump_warn_m,
                )
                self.metrics.record_event("export_succeeded", {
                    "route_job_id": result.get("route_job_id"),
                    "export_id": result.get("export_id"),
                    "method": "client_delegate",
                })
                self.metrics.increment("exports_succeeded")
                # Advance status
                route_id = result.get("route_job_id")
                if route_id:
                    self.advance_status(route_id, "exported_sequence_ready")
                result["ok"] = True
                result["classification"] = classification
                result["provenance"] = {
                    "engine": "constructor_orchestrator",
                    "method": "client_delegate",
                    "export_ts": export_ts,
                }
                return result
            except Exception as exc:
                _LOG.error("safe_export client delegate failed: %s", exc)
                self.metrics.record_event("failure_export_client", {"error": str(exc)})
                return {"ok": False, "error": f"Export failed: {exc}"}
        else:
            # Direct export without Phase3Client
            return self._direct_export(
                req=req,
                validation=validation,
                classification=classification,
                export_ts=export_ts,
            )

    # ======================================================================
    # 4. Idempotency
    # ======================================================================

    def check_rerun_safety(self, payload: dict) -> dict:
        """Check if this export would create duplicates.

        Looks at ``manual_sequence_exports`` for matching route_id +
        ordered_stop_ids signature.

        Returns
        -------
        dict with:
            safe (bool), existing_export_id (str or None),
            advice ('skip' | 'update' | 'warn').
        """
        try:
            req = normalize_manual_sequence_export_request(payload)
        except (ValueError, TypeError):
            return {"safe": True, "existing_export_id": None, "advice": "proceed"}

        route_id = req.route_job_id
        if not route_id:
            return {"safe": True, "existing_export_id": None, "advice": "proceed"}

        stop_sig = ",".join(req.ordered_stop_ids)

        try:
            with db_conn(readonly=True) as conn:
                # Check for exact stop-sequence match on same route
                row = fetch_one(
                    conn,
                    """
                    SELECT export_id::text, created_at
                    FROM route_work.manual_sequence_exports
                    WHERE route_id = %s::uuid
                      AND array_to_string(ordered_stop_ids, ',') = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (route_id, stop_sig),
                )
                if row:
                    return {
                        "safe": False,
                        "existing_export_id": row["export_id"],
                        "existing_created_at": str(row.get("created_at", "")),
                        "advice": "skip",
                    }

                # Check for any export on same route (different stops = warn)
                row2 = fetch_one(
                    conn,
                    """
                    SELECT export_id::text, created_at,
                           array_length(ordered_stop_ids, 1) AS stop_count
                    FROM route_work.manual_sequence_exports
                    WHERE route_id = %s::uuid
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (route_id,),
                )
                if row2:
                    return {
                        "safe": True,
                        "existing_export_id": None,
                        "prior_export_id": row2["export_id"],
                        "prior_stop_count": row2.get("stop_count"),
                        "advice": "warn",
                        "message": (
                            "A different export already exists for this route_id. "
                            "This will create an additional export, not replace the existing one."
                        ),
                    }

        except Exception as exc:
            _LOG.warning("check_rerun_safety DB check failed: %s", exc)
            # Fail-open: allow the export but record the error
            self.metrics.record_event("error_rerun_check", {"error": str(exc)})

        return {"safe": True, "existing_export_id": None, "advice": "proceed"}

    # ======================================================================
    # 5. Status Tracking
    # ======================================================================

    def get_constructor_status(self, route_id: str) -> str:
        """Read the current constructor lifecycle status for a route."""
        uid = _uuid_str(route_id)
        if not uid:
            return "unknown"
        try:
            with db_conn(readonly=True) as conn:
                return self._read_constructor_status(conn, uid)
        except Exception as exc:
            _LOG.warning("get_constructor_status failed: %s", exc)
            return "unknown"

    def advance_status(self, route_id: str, new_status: str) -> dict:
        """Advance a route's constructor status.

        Validates the transition is legal.  Persists to
        ``route_work.constructor_status_log`` if the table exists,
        otherwise tracks in-memory only.
        """
        uid = _uuid_str(route_id)
        if not uid:
            return {"error": "Invalid route_id"}

        if new_status not in CONSTRUCTOR_STATUSES:
            return {"error": f"Invalid status: {new_status}"}

        try:
            with db_conn() as conn:
                current = self._read_constructor_status(conn, uid)

                # Validate transition
                allowed = _VALID_TRANSITIONS.get(current, set())
                if new_status != current and new_status not in allowed:
                    msg = (
                        f"Illegal transition: {current} -> {new_status}. "
                        f"Allowed: {sorted(allowed)}"
                    )
                    self.metrics.record_event("error_status_transition", {
                        "route_id": uid,
                        "from": current,
                        "to": new_status,
                    })
                    return {"error": msg}

                self._write_constructor_status(conn, uid, new_status, current)

                self.metrics.record_event("status_advanced", {
                    "route_id": uid,
                    "from": current,
                    "to": new_status,
                })
                return {
                    "route_id": uid,
                    "previous_status": current,
                    "new_status": new_status,
                }
        except Exception as exc:
            _LOG.warning("advance_status failed: %s", exc)
            self.metrics.record_event("error_advance_status", {"error": str(exc)})
            return {"error": str(exc)}

    # ======================================================================
    # 6. Metrics
    # ======================================================================

    def collect_metrics(self) -> dict:
        """Return current metrics summary."""
        summary = self.metrics.summary()

        # Enrich with DB-sourced counts if available
        try:
            with db_conn(readonly=True) as conn:
                db_counts = fetch_one(
                    conn,
                    """
                    SELECT
                        (SELECT count(*) FROM route_work.manual_sequence_drafts
                         WHERE source != 'deleted') AS active_drafts,
                        (SELECT count(*) FROM route_work.manual_sequence_exports) AS total_exports
                    """,
                )
                if db_counts:
                    summary["db_active_drafts"] = db_counts.get("active_drafts", 0)
                    summary["db_total_exports"] = db_counts.get("total_exports", 0)
        except Exception:
            pass

        return summary

    def get_failure_classes(self) -> List[dict]:
        """Return grouped failure classes from metrics."""
        return self.metrics.failure_classes()

    # ======================================================================
    # 7. Hint Preservation
    # ======================================================================

    def merge_hints(
        self,
        *hint_layers: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge hint dictionaries with priority based on provenance.

        Each layer dict should have keys like ``name_hint``, ``operator_hint``,
        ``variant_hint``, and a ``_provenance`` key indicating source
        ('manual', 'constructor', 'gap_analysis', 'llm', 'default').

        Higher-priority provenance wins per field.

        Returns merged dict with ``_provenance_map`` showing which source
        won each field.
        """
        hint_fields = ("name_hint", "operator_hint", "variant_hint", "ref_hint")
        merged: Dict[str, Any] = {}
        provenance_map: Dict[str, str] = {}
        best_priority: Dict[str, int] = {}

        for layer in hint_layers:
            source = str(layer.get("_provenance", "default"))
            priority = _HINT_PRIORITY.get(source, 0)
            for field in hint_fields:
                val = layer.get(field)
                if val and str(val).strip():
                    current_pri = best_priority.get(field, -1)
                    if priority >= current_pri:
                        merged[field] = str(val).strip()
                        provenance_map[field] = source
                        best_priority[field] = priority

        merged["_provenance_map"] = provenance_map
        return merged

    # ======================================================================
    # Internal helpers
    # ======================================================================

    def _find_catalog_matches(
        self,
        *,
        name_hint: str,
        ref_hint: str,
        operator_hint: str,
        service_route_id: Optional[str],
    ) -> dict:
        """Search the active catalog for possible duplicates."""
        result: Dict[str, Any] = {
            "exact_match": None,
            "near_matches": [],
        }

        # If service_route_id is given, check directly
        if service_route_id:
            try:
                with db_conn(readonly=True) as conn:
                    row = fetch_one(
                        conn,
                        """
                        SELECT route_id::text
                        FROM route_raw.active_route_jobs
                        WHERE service_route_id = %s::uuid
                        LIMIT 1
                        """,
                        (service_route_id,),
                    )
                    if row:
                        result["exact_match"] = row["route_id"]
                        return result
            except Exception as exc:
                _LOG.debug("catalog check by service_route_id failed: %s", exc)

        # Search by ref or name
        if not ref_hint and not name_hint:
            return result

        try:
            with db_conn(readonly=True) as conn:
                conditions: List[str] = []
                params: list = []

                if ref_hint:
                    conditions.append(
                        "(known_ref ILIKE %s OR notes ILIKE %s)"
                    )
                    like_ref = f"%{ref_hint}%"
                    params.extend([like_ref, like_ref])

                if name_hint:
                    conditions.append("notes ILIKE %s")
                    params.append(f"%{name_hint}%")

                if operator_hint:
                    conditions.append("notes ILIKE %s")
                    params.append(f"%{operator_hint}%")

                if not conditions:
                    return result

                where = " OR ".join(conditions)
                rows = fetch_all(
                    conn,
                    f"""
                    SELECT route_id::text, known_ref, status, notes
                    FROM route_raw.active_route_jobs
                    WHERE {where}
                    LIMIT 10
                    """,
                    params,
                )
                if rows:
                    result["near_matches"] = [
                        {
                            "route_id": r["route_id"],
                            "known_ref": r.get("known_ref"),
                            "status": r.get("status"),
                        }
                        for r in rows
                    ]
        except Exception as exc:
            _LOG.debug("catalog near-match search failed: %s", exc)

        return result

    def _get_gap_evidence_summary(self, gap_id: str) -> dict:
        """Check how many approved stops a coverage gap references."""
        try:
            with db_conn(readonly=True) as conn:
                row = fetch_one(
                    conn,
                    """
                    SELECT
                        gap_id::text,
                        COALESCE(array_length(related_route_ids, 1), 0) AS related_route_count
                    FROM route_review.coverage_gaps
                    WHERE gap_id = %s::uuid
                    """,
                    (gap_id,),
                )
                if not row:
                    return {"gap_id": gap_id, "approved_stop_count": 0}
                # Approximate: related routes count as proxy for evidence
                return {
                    "gap_id": gap_id,
                    "related_route_count": row.get("related_route_count", 0),
                    "approved_stop_count": row.get("related_route_count", 0),
                }
        except Exception:
            return {"gap_id": gap_id, "approved_stop_count": 0}

    def _check_stop_quality(self, ordered_stop_ids: List[str]) -> List[str]:
        """Run basic quality checks on stop IDs."""
        issues: List[str] = []

        # Check for invalid UUIDs
        invalid_count = 0
        for sid in ordered_stop_ids:
            if not _uuid_str(sid):
                invalid_count += 1
        if invalid_count:
            issues.append(f"{invalid_count} stop ID(s) are not valid UUIDs")

        # Check for duplicates
        seen = set()
        dup_count = 0
        for sid in ordered_stop_ids:
            if sid in seen:
                dup_count += 1
            seen.add(sid)
        if dup_count:
            issues.append(f"{dup_count} duplicate stop ID(s) in sequence")

        return issues

    def _resolve_stop_rows(self, ordered_stop_ids: List[str]) -> List[dict]:
        """Resolve stop IDs to rows with coordinates from node_prod."""
        if not ordered_stop_ids:
            return []

        valid_ids = [sid for sid in ordered_stop_ids if _uuid_str(sid)]
        if not valid_ids:
            return []

        try:
            with db_conn(readonly=True) as conn:
                rows = fetch_all(
                    conn,
                    """
                    SELECT
                        node_id::text AS stop_id,
                        node_id::text AS node_id,
                        lat,
                        lon,
                        name,
                        ref,
                        place_id::text
                    FROM node_prod.stop_nodes
                    WHERE node_id = ANY(%s::uuid[])
                    """,
                    (valid_ids,),
                )
                return rows
        except Exception as exc:
            _LOG.warning("_resolve_stop_rows failed: %s", exc)
            return []

    def _read_constructor_status(self, conn: Any, route_id: Optional[str]) -> str:
        """Read current constructor status from the log table, or infer it."""
        if not route_id:
            return "unknown"
        try:
            row = fetch_one(
                conn,
                """
                SELECT status
                FROM route_work.constructor_status_log
                WHERE route_id = %s::uuid
                ORDER BY changed_at DESC
                LIMIT 1
                """,
                (route_id,),
            )
            if row:
                return str(row["status"])
        except Exception:
            # Table may not exist yet; fall back to inference
            pass

        # Infer from existing data
        try:
            export_row = fetch_one(
                conn,
                """
                SELECT export_id::text
                FROM route_work.manual_sequence_exports
                WHERE route_id = %s::uuid
                LIMIT 1
                """,
                (route_id,),
            )
            if export_row:
                return "exported_sequence_ready"

            draft_row = fetch_one(
                conn,
                """
                SELECT draft_id::text, array_length(ordered_stop_ids, 1) AS stop_count
                FROM route_work.manual_sequence_drafts
                WHERE route_id = %s::uuid AND source != 'deleted'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (route_id,),
            )
            if draft_row:
                stop_count = draft_row.get("stop_count") or 0
                return "draft_ready" if stop_count >= 2 else "draft_saved"
        except Exception:
            pass

        return "draft_saved"

    def _write_constructor_status(
        self,
        conn: Any,
        route_id: str,
        new_status: str,
        previous_status: str,
    ) -> None:
        """Persist a status transition to the log table.

        Creates the table on first use if it does not exist (idempotent DDL).
        """
        try:
            exec_sql(
                conn,
                """
                CREATE TABLE IF NOT EXISTS route_work.constructor_status_log (
                    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    route_id UUID NOT NULL,
                    status TEXT NOT NULL,
                    previous_status TEXT,
                    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    changed_by TEXT
                )
                """,
            )
            exec_sql(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_constructor_status_log_route
                    ON route_work.constructor_status_log(route_id, changed_at DESC)
                """,
            )
        except Exception:
            # DDL might fail in read-only or if schema already exists differently
            pass

        try:
            exec_sql(
                conn,
                """
                INSERT INTO route_work.constructor_status_log
                    (route_id, status, previous_status, changed_by)
                VALUES (%s::uuid, %s, %s, %s)
                """,
                (
                    route_id,
                    new_status,
                    previous_status,
                    os.getenv("USER") or os.getenv("USERNAME") or "orchestrator",
                ),
            )
        except Exception as exc:
            _LOG.warning("_write_constructor_status insert failed: %s", exc)

    def _direct_export(
        self,
        *,
        req: ManualSequenceExportRequest,
        validation: ManualSequenceValidation,
        classification: dict,
        export_ts: str,
    ) -> dict:
        """Perform export directly (without Phase3Client).

        This is a fallback path used when no client is injected.
        It writes to manual_sequence_exports and creates the stop
        sequence set + candidate rows.
        """
        created_by = req.created_by or os.getenv("USER") or os.getenv("USERNAME") or "console"
        ordered_stop_ids = [str(s["stop_id"]) for s in validation.ordered_stops]
        ordered_node_ids = list(req.ordered_node_ids or ordered_stop_ids)
        ordered_coords = list(req.ordered_coords or [])
        if not ordered_coords:
            ordered_coords = [
                [float(s["lon"]), float(s["lat"])]
                for s in validation.ordered_stops
            ]

        try:
            with db_conn() as conn:
                # Create or resolve route_job_id
                route_id = req.route_job_id
                if not route_id:
                    row = fetch_one(
                        conn,
                        """
                        INSERT INTO route_raw.route_jobs (route_id, created_by, notes)
                        VALUES (gen_random_uuid(), %s, %s)
                        RETURNING route_id::text
                        """,
                        (
                            created_by,
                            f"Auto-created by constructor_orchestrator for {req.name_hint or 'unnamed'}",
                        ),
                    )
                    route_id = row["route_id"] if row else None
                    if not route_id:
                        return {"ok": False, "error": "Failed to create route_job"}

                # Create stop_sequence_set
                set_row = fetch_one(
                    conn,
                    """
                    INSERT INTO route_work.stop_sequence_candidate_sets
                        (set_id, route_id, notes)
                    VALUES (gen_random_uuid(), %s::uuid, %s)
                    RETURNING set_id::text
                    """,
                    (route_id, f"source=manual_builder is_loop={req.is_loop}"),
                )
                set_id = set_row["set_id"] if set_row else None

                # Create stop_sequence_candidate
                candidate_row = fetch_one(
                    conn,
                    """
                    INSERT INTO route_work.stop_sequence_candidates
                        (candidate_id, set_id, rank, stop_node_ids, stop_prior_seqs, metrics)
                    VALUES (
                        gen_random_uuid(), %s::uuid, 1,
                        %s::uuid[],
                        %s::int[],
                        %s::jsonb
                    )
                    RETURNING candidate_id::text
                    """,
                    (
                        set_id,
                        ordered_stop_ids,
                        list(range(1, len(ordered_stop_ids) + 1)),
                        json.dumps({
                            "mode": "manual_builder",
                            "source": "constructor_orchestrator",
                            "is_loop": bool(req.is_loop),
                            "ordered_stop_count": len(ordered_stop_ids),
                            "name_hint": req.name_hint,
                            "operator_hint": req.operator_hint,
                            "variant_hint": req.variant_hint,
                        }, ensure_ascii=False),
                    ),
                )
                candidate_id = candidate_row["candidate_id"] if candidate_row else None

                # Create the export record
                export_row = fetch_one(
                    conn,
                    """
                    INSERT INTO route_work.manual_sequence_exports
                        (route_id, service_route_id, direction_id, source,
                         ordered_stop_ids, ordered_node_ids, ordered_coords, is_loop,
                         name_hint, operator_hint, variant_hint, created_by,
                         stop_sequence_set_id, stop_sequence_candidate_id, coverage_gap_id)
                    VALUES
                        (%s::uuid, %s::uuid, %s, 'manual_builder',
                         %s::uuid[], %s::uuid[], %s::jsonb, %s,
                         %s, %s, %s, %s,
                         %s::uuid, %s::uuid, %s::uuid)
                    RETURNING export_id::text, created_at
                    """,
                    (
                        str(route_id),
                        req.service_route_id,
                        req.direction_id,
                        ordered_stop_ids,
                        ordered_node_ids,
                        json.dumps(ordered_coords, ensure_ascii=False),
                        bool(req.is_loop),
                        req.name_hint,
                        req.operator_hint,
                        req.variant_hint,
                        created_by,
                        set_id,
                        candidate_id,
                        req.coverage_gap_id,
                    ),
                )

                export_id = export_row["export_id"] if export_row else None

                # Advance status
                self._write_constructor_status(
                    conn, str(route_id), "exported_sequence_ready", "draft_ready",
                )

            self.metrics.record_event("export_succeeded", {
                "route_job_id": str(route_id),
                "export_id": export_id,
                "method": "direct",
            })
            self.metrics.increment("exports_succeeded")

            return {
                "ok": True,
                "export_id": export_id,
                "route_job_id": str(route_id),
                "service_route_id": req.service_route_id,
                "direction_id": req.direction_id,
                "coverage_gap_id": req.coverage_gap_id,
                "source": "manual_builder",
                "ordered_stop_ids": ordered_stop_ids,
                "ordered_stop_count": len(ordered_stop_ids),
                "is_loop": bool(req.is_loop),
                "stop_sequence_set_id": set_id,
                "stop_sequence_candidate_id": candidate_id,
                "classification": classification,
                "warnings": validation.warnings,
                "provenance": {
                    "engine": "constructor_orchestrator",
                    "method": "direct",
                    "export_ts": export_ts,
                },
            }
        except Exception as exc:
            _LOG.error("_direct_export failed: %s", exc)
            self.metrics.record_event("failure_export_direct", {"error": str(exc)})
            return {"ok": False, "error": f"Direct export failed: {exc}"}

    # ======================================================================
    # 7. Sequence Discovery Pipeline
    # ======================================================================

    def run_sequence_discovery(
        self,
        seed_payload: dict,
        *,
        artifact_dir: Optional[str] = None,
        refine_geometry: bool = True,
        run_llm_advisory: bool = False,
        llm_mode: str = "mock",
        valhalla_timeout_s: int = 60,
        buffer_passes: Optional[List[int]] = None,
        scoring_mode: str = "ensemble",
    ) -> dict:
        """
        Run the full sequence discovery pipeline for a route seed.

        Takes a seed dict with:
          - route_name, operator_name, anchor_a_hint, anchor_b_hint
          - intermediate_hints, locality_hints, operator_id, etc.

        Returns a ConstructorRunSummary dict with all intermediate results
        and persisted JSON artifacts.
        """
        from datamind_console.phases.phase3_routes.stop_grounding.contracts import RouteSeed
        from datamind_console.phases.phase3_routes.stop_grounding.discovery_pipeline import (
            run_discovery_pipeline,
        )

        self.metrics.record_event("discovery_pipeline_started", {
            "route_name": seed_payload.get("route_name"),
        })

        seed = RouteSeed(
            route_name=str(seed_payload.get("route_name") or ""),
            operator_name=seed_payload.get("operator_name"),
            cooperative_name=seed_payload.get("cooperative_name"),
            corridor_description=seed_payload.get("corridor_description"),
            anchor_a_hint=str(seed_payload.get("anchor_a_hint") or ""),
            anchor_b_hint=str(seed_payload.get("anchor_b_hint") or ""),
            intermediate_hints=list(seed_payload.get("intermediate_hints") or []),
            locality_hints=list(seed_payload.get("locality_hints") or []),
            sequence_seed_fragments=list(seed_payload.get("sequence_seed_fragments") or []),
            source_notes=seed_payload.get("source_notes"),
            operator_id=seed_payload.get("operator_id"),
            cooperative_id=seed_payload.get("cooperative_id"),
            route_job_id=seed_payload.get("route_job_id"),
            service_route_id=seed_payload.get("service_route_id"),
            direction_id=seed_payload.get("direction_id"),
            coverage_gap_id=seed_payload.get("coverage_gap_id"),
            sector_key=seed_payload.get("sector_key"),
        )

        # TODO(a2): A2 synthesis is intentionally NOT wired at this call site.
        # The A2 block inside run_discovery_pipeline is gated on `conn is not
        # None`; this orchestrator method is driven by the interactive/manual
        # Streamlit builder path and does not thread a DB connection through.
        # Wiring A2 here would require plumbing conn + A2RunInputs through
        # run_sequence_discovery's signature and every caller. Deferred as out
        # of scope — batch_discovery.py and canton_pipeline.py cover the
        # automated paths where A2 is expected to fire.
        summary = run_discovery_pipeline(
            seed,
            artifact_dir=artifact_dir,
            refine_geometry=refine_geometry,
            run_llm_advisory=run_llm_advisory,
            llm_mode=llm_mode,
            valhalla_timeout_s=valhalla_timeout_s,
            buffer_passes=buffer_passes,
            scoring_mode=scoring_mode,
        )

        self.metrics.record_event("discovery_pipeline_completed", {
            "route_name": seed.route_name,
            "status": summary.status,
            "total_stops": summary.metrics.get("total_discovered_stops", 0) if summary.metrics else 0,
        })

        return summary.to_dict()
