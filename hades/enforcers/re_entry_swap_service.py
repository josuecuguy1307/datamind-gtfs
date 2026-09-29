"""HADES Re-Entry Swap Service.

Applies the operator's verdict on a v2 candidate in
``route_prod.approval_queue``:

- :func:`preview_swap_with_cleanup` — Read-only. Returns a
  :class:`SwapPreview` showing what cleanup *would* do to the row's
  ``proposed_stops`` if confirmed, plus any blockers. Used by the
  Streamlit Re-Entry Review tab to render BEFORE/AFTER maps before the
  operator commits.
- :func:`confirm_swap_with_cleanup` — Atomic. Runs cleanup (if the row
  is in a shippable class with no pending DR) AND executes the swap in
  a single transaction. ``operator_override=True`` bypasses the cleanup
  safety gate (>20% removal); requires a written ``override_reason``
  that is logged to ``route_prod.cleanup_override_audit``.
- :func:`approve_v2` — DEPRECATED wrapper around
  :func:`confirm_swap_with_cleanup`. Emits :class:`DeprecationWarning`
  on call. Kept so existing callers (integration tests, scripts) don't
  break at the boundary while we migrate the UI to the preview/confirm
  flow.
- :func:`reject_v2` — Rejects v2 as wrong; ``approval_queue`` →
  ``rejected``, ``re_entry_queue`` returns to ``pending`` with
  ``attempts`` reset so the worker can try again.
- :func:`quarantine_v2` — Flags the route as needing human work;
  ``approval_queue`` → ``rejected`` (with a ``[QUARANTINED]`` prefix in
  ``resolution_notes``), ``re_entry_queue`` → ``quarantined`` (never
  re-claimed by the worker).

All write operations execute inside a single transaction per route so a
crash rolls the whole thing back. The caller is responsible for
operator identity (``operator_id`` UUID + ``operator_username`` TEXT);
the service does not authenticate.

The v1 metrics captured in ``fix_reports.metrics_before`` come from
re-running the enforcers on the current (pre-swap) route state. That
keeps us independent of whether the worker persisted v1 reports in
``approval_queue.policy_flags`` and also captures current ground-truth
v1 shape, which may have drifted since the worker ran.

Trigger-refactor (2026-04-27): pre-ship cleanup is no longer
auto-applied by the reclassifier; it runs as part of the swap below
so the operator can review BEFORE/AFTER state and confirm before
either lands.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import warnings
from dataclasses import dataclass, field
from typing import Any, Optional

import psycopg2
import psycopg2.extras

from hades.enforcers.geometry_enforcer import GeometryEnforcer
from hades.enforcers.stop_refill import (
    HIGH_CONFIDENCE_THRESHOLD,
    ProductionRoute,
    RefillCandidate,
    find_refill_candidates,
)
from hades.enforcers.orphan_cleanup import (
    OrphanCleanupReport,
    cleanup_orphan_stops,
    validate_cleanup_not_destructive,
)
from hades.enforcers.stop_coverage_enforcer import StopCoverageEnforcer
from datamind_core.dsn import need_dsn


DEFAULT_DSN = os.environ.get("DB_DSN", "")


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _greek_swap_v2_enabled() -> bool:
    """Feature flag for the GREEK swap v2 atomic canonical-state flow.

    Default flipped ON in Fase 4 of the v2 rollout (design doc
    ``workspace/audits/2026-05-03_greek_swap_upgrade_v2_design.md``)
    after the 316-route Sample Region backfill landed cleanly. To roll back
    to the v1 swap, set ``HADES_GREEK_SWAP_V2=0`` in the worker / CLI
    environment; ``_confirm_greek_pipeline_v1`` is kept in the module
    for that purpose.
    """
    return _env_bool("HADES_GREEK_SWAP_V2", True)


# ---------------------------------------------------------------------------
# Result schema.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SwapResult:
    approval_queue_id: str
    route_id: str
    action: str  # "approved" | "rejected" | "quarantined"
    version_before: Optional[int] = None
    version_after: Optional[int] = None
    fix_report_id: Optional[str] = None
    created_synthetic_node_ids: list[str] = field(default_factory=list)
    resolution_notes: Optional[str] = None
    cleanup_applied: bool = False
    cleanup_report: Optional[dict] = None
    override_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_queue_id": self.approval_queue_id,
            "route_id": self.route_id,
            "action": self.action,
            "version_before": self.version_before,
            "version_after": self.version_after,
            "fix_report_id": self.fix_report_id,
            "created_synthetic_node_ids": list(self.created_synthetic_node_ids),
            "resolution_notes": self.resolution_notes,
            "cleanup_applied": self.cleanup_applied,
            "cleanup_report": self.cleanup_report,
            "override_used": self.override_used,
        }


@dataclass(slots=True)
class SwapPreview:
    """Read-only preview of what cleanup + swap *would* do.

    Returned by :func:`preview_swap_with_cleanup` so the UI can show
    BEFORE/AFTER maps before the operator commits.

    ``swap_blockers`` lists conditions that prevent the swap itself
    (status != pending, missing re_entry_queue_id, etc.). The UI must
    refuse to confirm when this list is non-empty.

    ``cleanup_eligible`` is True iff cleanup will run during confirm.
    When False, ``cleanup_skip_reason`` carries the reason (non-shippable
    class, pending DR batches, missing polyline). The swap can still
    proceed in this case — cleanup just won't touch ``proposed_stops``.

    ``safety_gate_status`` is one of ``pass`` / ``reject`` / ``n/a``.
    ``reject`` means the cleanup would remove >20% of stops and confirm
    will refuse unless ``operator_override=True`` with a reason.
    """
    queue_id: str
    route_code: Optional[str]
    quality_class: Optional[str]
    current_stops_count: int
    cleanup_eligible: bool
    cleanup_skip_reason: Optional[str] = None
    cleanup_report: Optional[dict] = None
    safety_gate_status: str = "n/a"  # 'pass' | 'reject' | 'n/a'
    safety_gate_reason: Optional[str] = None
    would_apply: bool = False
    swap_blockers: list[str] = field(default_factory=list)
    state_fingerprint: str = ""  # SHA256 of state-change-relevant fields at preview time; empty if row missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "route_code": self.route_code,
            "quality_class": self.quality_class,
            "current_stops_count": self.current_stops_count,
            "cleanup_eligible": self.cleanup_eligible,
            "cleanup_skip_reason": self.cleanup_skip_reason,
            "cleanup_report": self.cleanup_report,
            "safety_gate_status": self.safety_gate_status,
            "safety_gate_reason": self.safety_gate_reason,
            "would_apply": self.would_apply,
            "swap_blockers": list(self.swap_blockers),
            "state_fingerprint": self.state_fingerprint,
        }


class SwapError(RuntimeError):
    """Raised when the service cannot complete a swap for business reasons
    (missing rows, wrong statuses, malformed payloads). Distinct from
    psycopg2 errors so callers can render useful UI messages.
    """


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _coerce_for_hash(value: Any) -> Any:
    """Decode JSONB-as-string columns so the hash sees the same shape both
    inside and outside a transaction."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def compute_state_fingerprint(row: dict[str, Any]) -> str:
    """SHA256 hash of the approval_queue fields whose change invalidates a
    swap preview.

    Captures only the state that matters for cleanup + swap correctness:

      * ``proposed_stops`` — cleanup target list.
      * ``proposed_shape`` — canonical polyline.
      * ``quality_class`` — shippability gate.
      * ``pending_dr_batches`` — DR completion gate.

    Deliberately excluded (irrelevant changes that should NOT trigger
    re-preview): ``status`` (already filtered upstream), all timestamps,
    and the cleanup/refill satellite columns.

    Returns the SHA-256 hex digest of canonical JSON
    (``json.dumps(..., sort_keys=True)``) so two rows with semantically
    identical state hash to the same fingerprint regardless of dict-key
    insertion order or psycopg2's JSONB→str coercion.

    Accepts the ``dict`` shape produced by both ``_load_approval_row``
    (locked) and ``preview_swap_with_cleanup``'s SELECT (read-only) so
    the hash is computed identically on both sides.
    """
    canonical = json.dumps(
        {
            "proposed_stops": _coerce_for_hash(row.get("proposed_stops")),
            "proposed_shape": _coerce_for_hash(row.get("proposed_shape")),
            "quality_class": row.get("quality_class"),
            "pending_dr_batches": sorted(row.get("pending_dr_batches") or []),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_approval_row(cur, approval_queue_id: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT queue_id, route_code, version, status,
               decision_reasons, policy_flags,
               geometry_report, stop_coverage_report,
               proposed_stops, proposed_shape,
               quality_class, pre_ship_cleanup_applied,
               pending_dr_batches,
               crashed, crash_payload
          FROM route_prod.approval_queue
         WHERE queue_id = %s::uuid
         FOR UPDATE
        """,
        (approval_queue_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise SwapError(f"approval_queue queue_id={approval_queue_id} not found")
    return dict(row)


# Quality classes that are eligible for pre-ship orphan cleanup. Rows
# outside this set are still swap-able, cleanup just doesn't run for
# them. Mirrors the partial-index predicate in migration 036.
_SHIPPABLE_CLASSES_FOR_CLEANUP: frozenset[str] = frozenset(
    {"good", "acceptable", "ship_pending_dr"}
)


def _polyline_from_proposed_shape(
    proposed_shape: Any,
) -> list[tuple[float, float]]:
    """Decode ``proposed_shape`` JSON to ``[(lon, lat), …]`` for cleanup.

    Returns an empty list when the shape is missing or malformed —
    callers must check the length before passing to ``cleanup_orphan_stops``.
    """
    if proposed_shape is None:
        return []
    if isinstance(proposed_shape, str):
        try:
            proposed_shape = json.loads(proposed_shape)
        except Exception:
            return []
    if not isinstance(proposed_shape, dict):
        return []
    coords = proposed_shape.get("coordinates") or []
    out: list[tuple[float, float]] = []
    for c in coords:
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            out.append((float(c[0]), float(c[1])))
    return out


def _report_to_jsonable(
    report: OrphanCleanupReport,
    *,
    validate_ok: bool,
    validate_reason: str,
) -> dict[str, Any]:
    """Serialize an OrphanCleanupReport to the JSONB layout we persist."""
    return {
        "total_stops_before": report.total_stops_before,
        "total_stops_after": report.total_stops_after,
        "stops_aligned": report.stops_aligned,
        "stops_snapped": report.stops_snapped,
        "stops_removed": report.stops_removed,
        "snapped_stops": report.snapped_stops,
        "orphans_removed": report.orphans_removed,
        "thresholds_used": report.thresholds_used,
        "cleanup_version": report.cleanup_version,
        "validate_ok": validate_ok,
        "validate_reason": validate_reason,
    }


def _build_swap_preview(approval_row: dict[str, Any]) -> SwapPreview:
    """Construct a SwapPreview from a freshly-loaded approval_queue row.

    Used by both ``preview_swap_with_cleanup`` (read-only) and the
    re-validation step inside ``confirm_swap_with_cleanup`` (must run
    again under FOR UPDATE so the operator can't race a state change
    between preview and confirm).
    """
    queue_id = str(approval_row.get("queue_id"))
    route_code = approval_row.get("route_code")
    cls = approval_row.get("quality_class")
    proposed_stops = approval_row.get("proposed_stops") or []
    if isinstance(proposed_stops, str):
        try:
            proposed_stops = json.loads(proposed_stops)
        except Exception:
            proposed_stops = []
    pending_dr = approval_row.get("pending_dr_batches") or []

    fingerprint = compute_state_fingerprint(approval_row)

    blockers: list[str] = []
    if approval_row.get("status") != "pending":
        blockers.append(
            f"approval_queue.status={approval_row.get('status')} "
            "(expected 'pending')"
        )
    flags = approval_row.get("policy_flags") or {}
    if not flags.get("re_entry_queue_id"):
        blockers.append("policy_flags.re_entry_queue_id missing")

    if cls not in _SHIPPABLE_CLASSES_FOR_CLEANUP:
        return SwapPreview(
            queue_id=queue_id,
            route_code=route_code,
            quality_class=cls,
            current_stops_count=len(proposed_stops),
            cleanup_eligible=False,
            cleanup_skip_reason=f"quality_class={cls!r} is not shippable",
            swap_blockers=blockers,
            state_fingerprint=fingerprint,
        )
    if pending_dr:
        return SwapPreview(
            queue_id=queue_id,
            route_code=route_code,
            quality_class=cls,
            current_stops_count=len(proposed_stops),
            cleanup_eligible=False,
            cleanup_skip_reason=(
                f"{len(pending_dr)} pending DR batch(es): {list(pending_dr)}"
            ),
            swap_blockers=blockers,
            state_fingerprint=fingerprint,
        )

    polyline = _polyline_from_proposed_shape(approval_row.get("proposed_shape"))
    if len(polyline) < 2:
        return SwapPreview(
            queue_id=queue_id,
            route_code=route_code,
            quality_class=cls,
            current_stops_count=len(proposed_stops),
            cleanup_eligible=False,
            cleanup_skip_reason="proposed_shape has fewer than 2 coordinates",
            swap_blockers=blockers,
            state_fingerprint=fingerprint,
        )

    cleaned, report = cleanup_orphan_stops(proposed_stops, polyline)
    ok, reason = validate_cleanup_not_destructive(
        proposed_stops,
        cleaned,
        max_removal_pct=report.thresholds_used.get("max_removal_pct", 0.20),
    )
    report_dict = _report_to_jsonable(report, validate_ok=ok, validate_reason=reason)

    return SwapPreview(
        queue_id=queue_id,
        route_code=route_code,
        quality_class=cls,
        current_stops_count=len(proposed_stops),
        cleanup_eligible=True,
        cleanup_report=report_dict,
        safety_gate_status="pass" if ok else "reject",
        safety_gate_reason=reason,
        would_apply=ok,
        swap_blockers=blockers,
        state_fingerprint=fingerprint,
    )


def _load_re_entry_row(cur, re_entry_queue_id: str) -> dict[str, Any]:
    cur.execute(
        """
        SELECT queue_id, route_id, current_version, status, classification,
               priority, attempts
          FROM route_prod.re_entry_queue
         WHERE queue_id = %s::uuid
         FOR UPDATE
        """,
        (re_entry_queue_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise SwapError(
            f"re_entry_queue queue_id={re_entry_queue_id} not found"
        )
    return dict(row)


def _load_route_row(
    cur, route_id: str, version: int
) -> dict[str, Any]:
    cur.execute(
        """
        SELECT route_id, version, source, province, source_type,
               stop_node_ids::text[] AS stop_node_ids,
               ST_AsGeoJSON(geom) AS geom_json,
               legacy_grandfathered, grandfathered_until
          FROM route_prod.routes
         WHERE route_id = %s::uuid AND version = %s::smallint
         FOR UPDATE
        """,
        (route_id, int(version)),
    )
    row = cur.fetchone()
    if row is None:
        raise SwapError(
            f"routes (route_id={route_id}, version={version}) not found"
        )
    return dict(row)


def _extract_linestring_from_geojson(geom_json_str: str) -> list[tuple[float, float]]:
    geom = json.loads(geom_json_str) if isinstance(geom_json_str, str) else geom_json_str
    if geom["type"] == "LineString":
        return [(float(c[0]), float(c[1])) for c in geom["coordinates"]]
    if geom["type"] == "MultiLineString":
        flat: list[tuple[float, float]] = []
        for line in geom["coordinates"]:
            for c in line:
                flat.append((float(c[0]), float(c[1])))
        return flat
    raise SwapError(f"Unexpected geometry type in swap: {geom['type']}")


def _v1_metrics(cur, route_row: dict[str, Any]) -> dict[str, Any]:
    """Re-run enforcers on the pre-swap route state to capture metrics_before.

    Reads stops from ``node_prod.nodes`` using the stored ``stop_node_ids``
    array. Runs GeometryEnforcer + StopCoverageEnforcer with no resolvers
    (same contract as the populator + worker — this is a pure v1 audit).
    """
    coords_v1 = _extract_linestring_from_geojson(route_row["geom_json"])
    stop_ids_v1 = list(route_row["stop_node_ids"] or [])
    stops_v1: list[tuple[float, float]] = []
    if stop_ids_v1:
        cur.execute(
            """
            SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon
              FROM node_prod.nodes
             WHERE node_id = ANY(%s::uuid[])
             ORDER BY array_position(%s::uuid[], node_id)
            """,
            (stop_ids_v1, stop_ids_v1),
        )
        stops_v1 = [(float(r["lat"]), float(r["lon"])) for r in cur.fetchall()]

    geom_enf = GeometryEnforcer()
    geom_report = geom_enf.analyze(
        coords_v1,
        route_code=str(route_row["route_id"]),
        version=int(route_row["version"]),
    ).to_dict()

    sc_enf = StopCoverageEnforcer()
    sc_report = sc_enf.analyze(
        route_code=str(route_row["route_id"]),
        coords=coords_v1,
        stop_coords=stops_v1,
        stop_ids=stop_ids_v1 or None,
        version=int(route_row["version"]),
    ).to_dict()

    return {
        "n_coords": len(coords_v1),
        "n_stops": len(stops_v1),
        "geometry": {
            "classification": geom_report.get("classification"),
            "max_severity": geom_report.get("max_severity"),
            "n_anomalies": len(geom_report.get("anomalies", []) or []),
        },
        "stop_coverage": {
            "classification": sc_report.get("classification"),
            "n_gaps_total": (sc_report.get("summary", {}) or {}).get("n_gaps_total"),
            "n_gaps_unresolved": sc_report.get("n_gaps_unresolved"),
        },
    }


def _v2_metrics(
    approval_row: dict[str, Any], coords_v2: list[tuple[float, float]],
    proposed_stops: list[dict[str, Any]],
) -> dict[str, Any]:
    geom = approval_row.get("geometry_report") or {}
    sc = approval_row.get("stop_coverage_report") or {}
    return {
        "n_coords": len(coords_v2),
        "n_stops": len(proposed_stops),
        "geometry": {
            "classification": geom.get("classification"),
            "max_severity": geom.get("max_severity"),
            "n_anomalies": len(geom.get("anomalies", []) or []),
        },
        "stop_coverage": {
            "classification": sc.get("classification"),
            "n_gaps_total": (sc.get("summary", {}) or {}).get("n_gaps_total"),
            "n_gaps_unresolved": sc.get("n_gaps_unresolved"),
        },
    }


def _insert_synthetic_node(
    cur,
    *,
    lat: float,
    lon: float,
    province: str,
    tier: Optional[int],
    synthetic_confidence: str,
    operator_username: str,
    note: str,
) -> str:
    """Insert a synthetic bus_stop node for a Fixer-introduced stop.

    Routes through the universal stop-quality treater
    (``treat_stop(operation='synthetic_insert')``). The treater:
      - runs the contextual-naming cascade so the new stop gets a real
        name (was: NULL until a later cleanup)
      - creates the matching ``geo_prod.places`` row + ``node_place_map``
        link (was: never created — node lived without place mapping)
      - writes an audit row in ``stop_treatment_log``

    Returns the new node_id (UUID as str). Uses the connection from the
    cursor so it stays inside the caller's transaction.

    Note: ``cur`` is a psycopg2 cursor; we go through ``cur.connection``
    to satisfy the treater's connection-based API.
    """
    import psycopg2.extras as _ppx
    from datetime import datetime, timezone

    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )

    extras = {
        "source": "re_entry_swap",
        "source_type": "pure_synthesis",
        "synthetic_created_at": datetime.now(timezone.utc),
        "synthetic_created_by": operator_username,
        "synthetic_confidence": synthetic_confidence,
        "synthetic_review_state": "pending",
        "chosen_tags": _ppx.Json({"note": note, "tier": tier}),
    }
    result = treat_stop(
        StopTreatmentInput(
            operation="synthetic_insert",
            caller="re_entry_swap.synthetic_node",
            proposed_name="",          # cascade derives name
            proposed_lat=float(lat),
            proposed_lon=float(lon),
            province=province,
            confidence=0.5,
            extras=extras,
        ),
        cur.connection,
    )
    if not result.success:
        raise RuntimeError(f"re_entry_swap treatment failed: {result.error}")
    return str(result.node_id)


def _materialise_v2_stop_node_ids(
    cur,
    *,
    proposed_stops: list[dict[str, Any]],
    province: str,
    operator_username: str,
) -> tuple[list[str], list[str]]:
    """Walk proposed_stops, insert synthetic nodes for non-UUID ids, return
    ``(ordered_node_ids, created_synthetic_ids)``.
    """
    ordered: list[str] = []
    created: list[str] = []
    for stop in proposed_stops:
        sid = str(stop.get("stop_id", ""))
        if _is_uuid(sid):
            ordered.append(sid)
            continue
        # Synthetic id (``fix_gap{idx}_tier{n}`` or ``synthesized_{i}``).
        # Extract tier / confidence if encoded in the id for provenance.
        tier: Optional[int] = None
        if sid.startswith("fix_gap") and "_tier" in sid:
            try:
                tier = int(sid.rsplit("_tier", 1)[-1])
            except ValueError:
                tier = None
        new_id = _insert_synthetic_node(
            cur,
            lat=float(stop["lat"]),
            lon=float(stop["lon"]),
            province=province,
            tier=tier,
            synthetic_confidence="medium",
            operator_username=operator_username,
            note=f"swap fill source={sid}",
        )
        ordered.append(new_id)
        created.append(new_id)
    return ordered, created


def _build_fixes_applied(
    approval_row: dict[str, Any],
    coords_v2_count: int,
    stops_v2_count: int,
    created_synthetic_ids: list[str],
) -> dict[str, Any]:
    flags = approval_row.get("policy_flags") or {}
    return {
        "category": flags.get("fix_category"),
        "geom_fix_reason": flags.get("geom_fix_reason"),
        "cov_fix_reason": flags.get("cov_fix_reason"),
        "decision_reasons": approval_row.get("decision_reasons") or [],
        "re_entry_queue_id": flags.get("re_entry_queue_id"),
        "re_entry_classification": flags.get("re_entry_classification"),
        "v2_n_coords": coords_v2_count,
        "v2_n_stops": stops_v2_count,
        "synthetic_nodes_created": list(created_synthetic_ids),
    }


# ---------------------------------------------------------------------------
# PREVIEW (read-only).
# ---------------------------------------------------------------------------

def preview_swap_with_cleanup(
    approval_queue_id: str,
    *,
    dsn: str = DEFAULT_DSN,
) -> SwapPreview:
    """Compute a SwapPreview for one approval_queue row without touching it.

    Used by the Streamlit UI to render BEFORE/AFTER maps + change-summary
    metrics before the operator commits. Opens a short-lived
    connection, runs cleanup math in dry-run, and returns. No DB rows
    are modified.
    """
    conn = psycopg2.connect(need_dsn(dsn))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT queue_id, route_code, version, status,
                       decision_reasons, policy_flags,
                       geometry_report, stop_coverage_report,
                       proposed_stops, proposed_shape,
                       quality_class, pre_ship_cleanup_applied,
                       pending_dr_batches,
                       crashed, crash_payload
                  FROM route_prod.approval_queue
                 WHERE queue_id = %s::uuid
                """,
                (approval_queue_id,),
            )
            row = cur.fetchone()
            if row is None:
                return SwapPreview(
                    queue_id=approval_queue_id,
                    route_code=None,
                    quality_class=None,
                    current_stops_count=0,
                    cleanup_eligible=False,
                    cleanup_skip_reason="approval_queue row not found",
                    swap_blockers=[
                        f"approval_queue queue_id={approval_queue_id} not found"
                    ],
                )
            return _build_swap_preview(dict(row))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CONFIRM (atomic cleanup + swap).
# ---------------------------------------------------------------------------

def confirm_swap_with_cleanup(
    *,
    approval_queue_id: str,
    expected_fingerprint: str,
    operator_id: str,
    operator_username: str,
    dsn: str = DEFAULT_DSN,
    operator_override: bool = False,
    override_reason: Optional[str] = None,
) -> SwapResult:
    """Atomically apply pre-ship cleanup (if eligible) and swap v1 → v2.

    LEGACY (post-2026-04-27): for full GREEK pipeline including refill
    (γ) phase, prefer :func:`confirm_greek_pipeline`. This function
    remains for callers that don't need refill (``approve_v2`` shim,
    integration tests).

    Single transaction:

      1. ``FOR UPDATE`` lock the approval_queue row.
      2. Re-build the preview against the locked row (so an operator
         can't race a state change between preview and confirm).
      3. Validate that ``expected_fingerprint`` matches the locked row's
         current fingerprint. If not, abort STRICT — the operator must
         re-preview to see the fresh state. No auto-retry.
      4. Refuse if ``swap_blockers`` is non-empty.
      5. If ``cleanup_eligible``: refuse on safety-gate reject unless
         ``operator_override=True`` is set with a non-empty
         ``override_reason``. Apply cleanup (UPDATE proposed_stops +
         applied flag + report). Override events log to
         ``route_prod.cleanup_override_audit``.
      6. Run the swap (insert synthetic nodes, UPDATE routes, INSERT
         fix_reports, transition queues) using the cleaned stops.

    ``expected_fingerprint`` is required (raises ValueError when empty).
    Callers obtain it from :func:`preview_swap_with_cleanup`'s
    :class:`SwapPreview.state_fingerprint`. This is the GREEK pipeline
    phase β "last-step guarantee" that cleanup runs ONLY on the exact
    state the operator reviewed.

    Returns a :class:`SwapResult` carrying the cleanup outcome alongside
    the existing swap fields.
    """
    if not expected_fingerprint or not expected_fingerprint.strip():
        raise ValueError(
            "expected_fingerprint required — call preview_swap_with_cleanup "
            "first and pass its state_fingerprint here"
        )
    if operator_override and not (override_reason or "").strip():
        raise SwapError(
            "operator_override=True requires a non-empty override_reason "
            "(audited in route_prod.cleanup_override_audit)"
        )

    conn = psycopg2.connect(need_dsn(dsn))
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SET CONSTRAINTS route_prod.fk_re_entry_queue_route DEFERRED"
            )
            approval_row = _load_approval_row(cur, approval_queue_id)

            current_fingerprint = compute_state_fingerprint(approval_row)
            if current_fingerprint != expected_fingerprint:
                raise SwapError(
                    "State changed since preview "
                    f"(expected {expected_fingerprint[:8]}…, "
                    f"got {current_fingerprint[:8]}…). "
                    "Refresh preview and try again."
                )

            preview = _build_swap_preview(approval_row)
            if preview.swap_blockers:
                raise SwapError(
                    "swap blocked: " + "; ".join(preview.swap_blockers)
                )

            cleanup_applied_flag = False
            cleanup_report_persisted: Optional[dict] = None
            override_used = False

            if preview.cleanup_eligible:
                report = preview.cleanup_report or {}
                if not preview.would_apply and not operator_override:
                    raise SwapError(
                        f"cleanup safety gate: {preview.safety_gate_reason}. "
                        "Pass operator_override=True with a written "
                        "override_reason to bypass (audited)."
                    )

                # Re-run cleanup math on the locked row so we get the
                # exact ``cleaned`` list to write back. (Preview has
                # ``cleanup_report`` but not the cleaned stops list.)
                polyline = _polyline_from_proposed_shape(
                    approval_row.get("proposed_shape")
                )
                proposed_stops = approval_row.get("proposed_stops") or []
                if isinstance(proposed_stops, str):
                    proposed_stops = json.loads(proposed_stops)
                cleaned, fresh_report = cleanup_orphan_stops(
                    proposed_stops, polyline,
                )
                ok, reason = validate_cleanup_not_destructive(
                    proposed_stops,
                    cleaned,
                    max_removal_pct=fresh_report.thresholds_used.get(
                        "max_removal_pct", 0.20
                    ),
                )
                # Should match the preview within rounding; if it
                # diverged catastrophically (the row mutated between
                # preview and lock?), refuse rather than apply stale
                # math.
                if (
                    fresh_report.total_stops_before
                    != preview.current_stops_count
                ):
                    raise SwapError(
                        "approval_queue row mutated between preview and "
                        "confirm — re-run preview before retrying"
                    )

                cleanup_report_persisted = _report_to_jsonable(
                    fresh_report,
                    validate_ok=ok,
                    validate_reason=reason,
                )

                if not ok and not operator_override:
                    # Defensive — preview already gated this. Log the
                    # rejected report for audit and abort.
                    raise SwapError(
                        f"cleanup safety gate fired post-lock: {reason}"
                    )

                cur.execute(
                    """
                    UPDATE route_prod.approval_queue
                       SET proposed_stops             = %s::jsonb,
                           pre_ship_cleanup_applied   = TRUE,
                           pre_ship_cleanup_report    = %s::jsonb,
                           pre_ship_cleanup_at        = NOW()
                     WHERE queue_id = %s::uuid
                    """,
                    (
                        json.dumps(cleaned),
                        json.dumps(cleanup_report_persisted),
                        approval_queue_id,
                    ),
                )
                cleanup_applied_flag = True
                # Reload approval_row so the swap below uses the
                # cleaned proposed_stops.
                approval_row["proposed_stops"] = cleaned
                approval_row["pre_ship_cleanup_applied"] = True

                if operator_override and not ok:
                    override_used = True
                    cur.execute(
                        """
                        INSERT INTO route_prod.cleanup_override_audit (
                          queue_id, route_code, override_reason,
                          override_by, cleanup_report_at_override
                        ) VALUES (%s::uuid, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            approval_queue_id,
                            approval_row.get("route_code"),
                            override_reason.strip(),
                            operator_username,
                            json.dumps(cleanup_report_persisted),
                        ),
                    )

            # ---- Swap below — same as the legacy approve_v2 body. ----
            flags = approval_row.get("policy_flags") or {}
            re_entry_queue_id = flags.get("re_entry_queue_id")
            re_row = _load_re_entry_row(cur, re_entry_queue_id)
            if re_row["status"] != "v2_ready":
                raise SwapError(
                    f"re_entry_queue status={re_row['status']} "
                    "(expected 'v2_ready')"
                )

            route_id = str(re_row["route_id"])
            v1_version = int(re_row["current_version"])
            v2_version = int(approval_row["version"])
            if v2_version != v1_version + 1:
                raise SwapError(
                    f"version mismatch: re_entry.current_version={v1_version}, "
                    f"approval_queue.version={v2_version} "
                    f"(expected {v1_version + 1})"
                )

            route_row = _load_route_row(cur, route_id, v1_version)

            metrics_before = _v1_metrics(cur, route_row)

            proposed_shape = approval_row["proposed_shape"]
            coords_v2 = _extract_linestring_from_geojson(proposed_shape)
            proposed_stops_final = approval_row["proposed_stops"] or []

            metrics_after = _v2_metrics(
                approval_row, coords_v2, proposed_stops_final,
            )

            province = route_row.get("province") or "sample_region"
            v2_node_ids, created_synth = _materialise_v2_stop_node_ids(
                cur,
                proposed_stops=proposed_stops_final,
                province=province,
                operator_username=operator_username,
            )

            wkt = (
                "LINESTRING("
                + ", ".join(f"{lon} {lat}" for (lon, lat) in coords_v2)
                + ")"
            )

            cur.execute(
                """
                UPDATE route_prod.routes
                   SET geom = ST_SetSRID(ST_GeomFromText(%s), 4326),
                       stop_node_ids = %s::uuid[],
                       version = %s,
                       last_swap_at = NOW(),
                       last_swap_from_version = %s,
                       geometry_enforcer_report = %s::jsonb,
                       stop_coverage_report = %s::jsonb,
                       legacy_grandfathered = FALSE,
                       grandfathered_until = NULL,
                       pipeline_version = 're_entry_v1',
                       valhalla_request = COALESCE(valhalla_request, '{}'::jsonb),
                       quality_gate_passed_at = NOW(),
                       approved_by_user = %s::uuid,
                       approved_at = NOW(),
                       updated_at = NOW()
                 WHERE route_id = %s::uuid AND version = %s::smallint
                """,
                (
                    wkt,
                    v2_node_ids,
                    v2_version,
                    v1_version,
                    json.dumps(approval_row["geometry_report"] or {}),
                    json.dumps(approval_row["stop_coverage_report"] or {}),
                    operator_id,
                    route_id,
                    v1_version,
                ),
            )
            if cur.rowcount != 1:
                raise SwapError(
                    f"routes UPDATE affected {cur.rowcount} rows "
                    "(expected 1) — concurrent swap or stale lineage?"
                )

            fix_category = (flags.get("fix_category") or "structural")
            fixes_applied = _build_fixes_applied(
                approval_row,
                coords_v2_count=len(coords_v2),
                stops_v2_count=len(proposed_stops_final),
                created_synthetic_ids=created_synth,
            )
            cur.execute(
                """
                INSERT INTO route_prod.fix_reports (
                  route_id, version_before, version_after, fix_category,
                  fixes_applied, metrics_before, metrics_after,
                  applied_at, applied_by
                )
                VALUES (%s::uuid, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                        NOW(), %s)
                RETURNING report_id::text
                """,
                (
                    route_id,
                    v1_version,
                    v2_version,
                    fix_category,
                    json.dumps(fixes_applied),
                    json.dumps(metrics_before),
                    json.dumps(metrics_after),
                    operator_username,
                ),
            )
            fix_report_id = str(cur.fetchone()["report_id"])

            resolution_suffix = (
                "[APPROVED] swap applied"
                + (" + cleanup" if cleanup_applied_flag else "")
                + (" + override" if override_used else "")
            )
            cur.execute(
                """
                UPDATE route_prod.approval_queue
                   SET status = 'approved',
                       resolved_at = NOW(),
                       resolved_by = %s,
                       resolution_notes = COALESCE(resolution_notes, '') || %s
                 WHERE queue_id = %s::uuid
                """,
                (operator_username, resolution_suffix, approval_queue_id),
            )

            cur.execute(
                """
                UPDATE route_prod.re_entry_queue
                   SET status = 'swapped',
                       current_version = %s::smallint,
                       resolved_at = NOW()
                 WHERE queue_id = %s::uuid
                """,
                (v2_version, re_entry_queue_id),
            )

        conn.commit()
        return SwapResult(
            approval_queue_id=approval_queue_id,
            route_id=route_id,
            action="approved",
            version_before=v1_version,
            version_after=v2_version,
            fix_report_id=fix_report_id,
            created_synthetic_node_ids=created_synth,
            resolution_notes="swap applied",
            cleanup_applied=cleanup_applied_flag,
            cleanup_report=cleanup_report_persisted,
            override_used=override_used,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# APPROVE (deprecated wrapper).
# ---------------------------------------------------------------------------

def approve_v2(
    *,
    approval_queue_id: str,
    operator_id: str,
    operator_username: str,
    dsn: str = DEFAULT_DSN,
    operator_override: bool = False,
    override_reason: Optional[str] = None,
) -> SwapResult:
    """DEPRECATED — call :func:`confirm_swap_with_cleanup` directly.

    Thin wrapper retained so existing callers (integration tests,
    one-off scripts) don't break at the boundary while the UI migrates
    to the preview/confirm flow. Emits :class:`DeprecationWarning` on
    every call.

    The ``operator_override`` parameter now forwards to the
    cleanup-safety-gate bypass on
    :func:`confirm_swap_with_cleanup`. Under the previous design it
    bypassed the "must have run cleanup before this swap" precondition;
    that gate no longer exists because cleanup runs INSIDE the swap.
    Callers passing ``operator_override=True`` for the old reason can
    drop the flag — there is no longer a precondition to bypass — but
    if they pass it, they must also pass ``override_reason`` (or the
    new function will refuse).
    """
    warnings.warn(
        "approve_v2 is deprecated as of 2026-04-27; use "
        "preview_swap_with_cleanup + confirm_swap_with_cleanup. "
        "operator_override semantics changed: it now bypasses the "
        "cleanup safety gate instead of a pre-cleanup precondition.",
        DeprecationWarning,
        stacklevel=2,
    )
    preview = preview_swap_with_cleanup(approval_queue_id, dsn=dsn)
    if not preview.state_fingerprint:
        # Empty fingerprint means the row could not be loaded at preview
        # time. Surface the legacy "not found" error rather than a
        # spurious ValueError from confirm_swap_with_cleanup's input
        # validation.
        raise SwapError(
            f"approval_queue queue_id={approval_queue_id} not found"
        )
    return confirm_swap_with_cleanup(
        approval_queue_id=approval_queue_id,
        expected_fingerprint=preview.state_fingerprint,
        operator_id=operator_id,
        operator_username=operator_username,
        dsn=dsn,
        operator_override=operator_override,
        override_reason=override_reason,
    )


# ---------------------------------------------------------------------------
# REJECT (retry-eligible).
# ---------------------------------------------------------------------------

def reject_v2(
    *,
    approval_queue_id: str,
    operator_id: str,  # unused in DB write, kept for symmetry/telemetry
    operator_username: str,
    reason: str,
    dsn: str = DEFAULT_DSN,
) -> SwapResult:
    """Reject v2 as wrong; return the re_entry_queue row to ``pending``
    with ``attempts=0`` so the worker will retry. ``approval_queue`` →
    ``rejected``.
    """
    _ = operator_id  # reserved for future audit row
    conn = psycopg2.connect(need_dsn(dsn))
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            approval_row = _load_approval_row(cur, approval_queue_id)
            if approval_row["status"] != "pending":
                raise SwapError(
                    f"approval_queue status={approval_row['status']}"
                    " (expected 'pending')"
                )
            flags = approval_row.get("policy_flags") or {}
            re_entry_queue_id = flags.get("re_entry_queue_id")
            if not re_entry_queue_id:
                raise SwapError(
                    "approval_queue row is missing policy_flags.re_entry_queue_id"
                )
            re_row = _load_re_entry_row(cur, re_entry_queue_id)
            route_id = str(re_row["route_id"])

            cur.execute(
                """
                UPDATE route_prod.approval_queue
                   SET status = 'rejected',
                       resolved_at = NOW(),
                       resolved_by = %s,
                       resolution_notes = %s
                 WHERE queue_id = %s::uuid
                """,
                (operator_username, f"[REJECTED] {reason}", approval_queue_id),
            )

            cur.execute(
                """
                UPDATE route_prod.re_entry_queue
                   SET status = 'pending',
                       attempts = 0,
                       last_error = NULL,
                       resolved_at = NULL,
                       attempted_at = NULL
                 WHERE queue_id = %s::uuid
                """,
                (re_entry_queue_id,),
            )

        conn.commit()
        return SwapResult(
            approval_queue_id=approval_queue_id,
            route_id=route_id,
            action="rejected",
            resolution_notes=reason,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# QUARANTINE (no auto-retry).
# ---------------------------------------------------------------------------

def quarantine_v2(
    *,
    approval_queue_id: str,
    operator_id: str,
    operator_username: str,
    reason: str,
    dsn: str = DEFAULT_DSN,
) -> SwapResult:
    """Flag the route for human intervention. ``approval_queue`` →
    ``rejected`` (with a ``[QUARANTINED]`` prefix in
    ``resolution_notes``); ``re_entry_queue`` → ``quarantined`` so the
    worker's claim query never picks it up again.
    """
    _ = operator_id
    conn = psycopg2.connect(need_dsn(dsn))
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            approval_row = _load_approval_row(cur, approval_queue_id)
            if approval_row["status"] != "pending":
                raise SwapError(
                    f"approval_queue status={approval_row['status']}"
                    " (expected 'pending')"
                )
            flags = approval_row.get("policy_flags") or {}
            re_entry_queue_id = flags.get("re_entry_queue_id")
            if not re_entry_queue_id:
                raise SwapError(
                    "approval_queue row is missing policy_flags.re_entry_queue_id"
                )
            re_row = _load_re_entry_row(cur, re_entry_queue_id)
            route_id = str(re_row["route_id"])

            cur.execute(
                """
                UPDATE route_prod.approval_queue
                   SET status = 'rejected',
                       resolved_at = NOW(),
                       resolved_by = %s,
                       resolution_notes = %s
                 WHERE queue_id = %s::uuid
                """,
                (
                    operator_username,
                    f"[QUARANTINED] {reason}",
                    approval_queue_id,
                ),
            )

            cur.execute(
                """
                UPDATE route_prod.re_entry_queue
                   SET status = 'quarantined',
                       last_error = %s,
                       resolved_at = NOW()
                 WHERE queue_id = %s::uuid
                """,
                (reason, re_entry_queue_id),
            )

        conn.commit()
        return SwapResult(
            approval_queue_id=approval_queue_id,
            route_id=route_id,
            action="quarantined",
            resolution_notes=reason,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GREEK pipeline orchestrator (β → α → γ → δ).
#
# β  state fingerprint check       (compute_state_fingerprint)
# α  cleanup of orphan/snap stops  (cleanup_orphan_stops)
# γ  refill from production routes (find_refill_candidates)
# δ  atomic swap to route_prod     (existing swap body)
#
# Single transaction. Refill applies AFTER cleanup so candidate distance
# is computed against the polyline (which is unchanged) but the
# existing-stops baseline is the post-cleanup set. Refill state is
# deliberately NOT in the fingerprint — production routes are assumed
# stable during operator decision time, and any change to the four
# fingerprint fields invalidates the cached candidates anyway.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GreekPipelinePreview:
    """Combined preview for full GREEK pipeline (β → α → γ → δ).

    Returned by :func:`preview_greek_pipeline`. Carries the cleanup
    dry-run AND the refill candidates so the UI can render both in a
    single review step.
    """
    queue_id: str
    route_code: Optional[str]
    quality_class: Optional[str]
    state_fingerprint: str

    # α — cleanup
    cleanup_eligible: bool
    cleanup_skip_reason: Optional[str]
    cleanup_report: Optional[dict]
    cleanup_safety_gate: str  # 'pass' | 'reject' | 'blocked' | 'n/a'
    cleanup_safety_reason: Optional[str]

    # γ — refill
    refill_eligible: bool
    refill_candidates: list[dict]  # serialized RefillCandidate dicts
    refill_high_confidence_count: int
    refill_medium_confidence_count: int

    # Combined eligibility
    would_apply: bool
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "route_code": self.route_code,
            "quality_class": self.quality_class,
            "state_fingerprint": self.state_fingerprint,
            "cleanup_eligible": self.cleanup_eligible,
            "cleanup_skip_reason": self.cleanup_skip_reason,
            "cleanup_report": self.cleanup_report,
            "cleanup_safety_gate": self.cleanup_safety_gate,
            "cleanup_safety_reason": self.cleanup_safety_reason,
            "refill_eligible": self.refill_eligible,
            "refill_candidates": list(self.refill_candidates),
            "refill_high_confidence_count": self.refill_high_confidence_count,
            "refill_medium_confidence_count": self.refill_medium_confidence_count,
            "would_apply": self.would_apply,
            "blockers": list(self.blockers),
        }


@dataclass(slots=True)
class GreekPipelineResult:
    """Outcome of :func:`confirm_greek_pipeline`."""
    success: bool
    cleanup_applied: bool
    cleanup_report: Optional[dict]
    refill_applied: bool
    refill_decisions_count: int
    refill_accepted_count: int
    swap_id: Optional[str]
    override_used: bool
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "cleanup_applied": self.cleanup_applied,
            "cleanup_report": self.cleanup_report,
            "refill_applied": self.refill_applied,
            "refill_decisions_count": self.refill_decisions_count,
            "refill_accepted_count": self.refill_accepted_count,
            "swap_id": self.swap_id,
            "override_used": self.override_used,
            "error": self.error,
        }


def _confidence_band(score: float) -> str:
    if score >= HIGH_CONFIDENCE_THRESHOLD:
        return "high"
    return "medium"


def _refill_candidate_to_dict(c: RefillCandidate) -> dict[str, Any]:
    """Serialize a RefillCandidate for JSONB persistence + UI consumption.

    Adds derived fields the UI/audit layers expect (``confidence_band``,
    ``popularity`` alias) without changing the underlying dataclass.
    """
    return {
        "stop_id": c.stop_id,
        "lat": c.lat,
        "lon": c.lon,
        "distance_to_polyline_m": c.distance_to_polyline_m,
        "origin_distance_m": c.origin_distance_m,
        "routes_count": c.routes_count,
        "popularity": c.routes_count,  # spec alias
        "max_shared_segment_m": c.max_shared_segment_m,
        "score": c.score,
        "confidence_band": _confidence_band(c.score),
        "source_route_ids": list(c.source_route_ids),
    }


def _load_production_routes_pool(
    cur,
    *,
    exclude_route_id: str,
    proposed_shape: Any,
    province: str = "sample_region",
    max_routes: int = 25,
) -> list[ProductionRoute]:
    """Load the active production routes that geometrically overlap with
    the target route's polyline, formatted as :class:`ProductionRoute`.

    Filters: ``deploy_status='active'``, same province, target route
    excluded, geometry bounding-box intersects the target polyline.
    Joins to ``node_prod.nodes`` to materialise per-stop ``(lat, lon)``.
    Cap at ``max_routes`` ordered by geographic proximity so very large
    cohorts stay performant.
    """
    if proposed_shape is None:
        return []
    geom_json = (
        proposed_shape if isinstance(proposed_shape, str)
        else json.dumps(proposed_shape)
    )

    cur.execute(
        """
        SELECT r.route_id::text AS route_id,
               ST_AsGeoJSON(r.geom) AS geom_json,
               (
                 SELECT json_agg(json_build_object(
                          'stop_id', n.node_id::text,
                          'lat', ST_Y(n.geom),
                          'lon', ST_X(n.geom)
                        ))
                   FROM node_prod.nodes n
                  WHERE n.node_id::text = ANY(r.stop_node_ids::text[])
               ) AS stops
          FROM route_prod.routes r
         WHERE r.deploy_status = 'active'
           AND r.province = %s
           AND r.route_id::text <> %s
           AND r.geom && ST_GeomFromGeoJSON(%s)
        ORDER BY ST_Distance(
          r.geom::geography,
          ST_GeomFromGeoJSON(%s)::geography
        )
        LIMIT %s
        """,
        (province, exclude_route_id, geom_json, geom_json, max_routes),
    )

    pool: list[ProductionRoute] = []
    for row in cur.fetchall():
        try:
            geom = json.loads(row["geom_json"]) if row.get("geom_json") else None
        except Exception:
            geom = None
        if not geom or geom.get("type") != "LineString":
            continue
        coords_raw = geom.get("coordinates") or []
        polyline: list[tuple[float, float]] = []
        for c in coords_raw:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                polyline.append((float(c[0]), float(c[1])))
        if len(polyline) < 2:
            continue
        stops_raw = row.get("stops") or []
        if isinstance(stops_raw, str):
            try:
                stops_raw = json.loads(stops_raw)
            except Exception:
                stops_raw = []
        if not stops_raw:
            continue
        pool.append(
            ProductionRoute(
                route_id=str(row["route_id"]),
                polyline=polyline,
                stops=list(stops_raw),
            )
        )
    return pool


def _persist_refill_candidates(
    cur,
    approval_queue_id: str,
    candidates_dict: list[dict[str, Any]],
) -> None:
    """Cache refill candidates on the approval_queue row so confirm-time
    audit knows exactly which candidates the operator was shown."""
    cur.execute(
        """
        UPDATE route_prod.approval_queue
           SET proposed_refill_candidates = %s::jsonb
         WHERE queue_id = %s::uuid
        """,
        (json.dumps(candidates_dict), approval_queue_id),
    )


def preview_greek_pipeline(
    approval_queue_id: str,
    *,
    dsn: str = DEFAULT_DSN,
    production_routes: Optional[list[ProductionRoute]] = None,
) -> GreekPipelinePreview:
    """Compute a combined cleanup + refill preview for one approval_queue row.

    Reuses :func:`preview_swap_with_cleanup`'s cleanup logic, then runs
    :func:`find_refill_candidates` against the post-cleanup stops using
    the active production routes pool. The candidates are cached in
    ``approval_queue.proposed_refill_candidates`` so confirm-time audit
    can replay exactly what the operator was shown.

    ``production_routes`` may be supplied by the caller (tests, special
    workflows) to bypass the default DB loader. When ``None``, production
    routes are loaded from ``route_prod.routes`` via
    :func:`_load_production_routes_pool`.
    """
    conn = psycopg2.connect(need_dsn(dsn))
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT queue_id, route_code, version, status,
                       decision_reasons, policy_flags,
                       geometry_report, stop_coverage_report,
                       proposed_stops, proposed_shape,
                       quality_class, pre_ship_cleanup_applied,
                       pending_dr_batches, refill_applied,
                       crashed, crash_payload
                  FROM route_prod.approval_queue
                 WHERE queue_id = %s::uuid
                """,
                (approval_queue_id,),
            )
            row = cur.fetchone()
            if row is None:
                return GreekPipelinePreview(
                    queue_id=approval_queue_id,
                    route_code=None,
                    quality_class=None,
                    state_fingerprint="",
                    cleanup_eligible=False,
                    cleanup_skip_reason="approval_queue row not found",
                    cleanup_report=None,
                    cleanup_safety_gate="blocked",
                    cleanup_safety_reason=None,
                    refill_eligible=False,
                    refill_candidates=[],
                    refill_high_confidence_count=0,
                    refill_medium_confidence_count=0,
                    would_apply=False,
                    blockers=[
                        f"approval_queue queue_id={approval_queue_id} not found"
                    ],
                )

            approval_row = dict(row)
            preview_alpha = _build_swap_preview(approval_row)
            fingerprint = preview_alpha.state_fingerprint

            # Promote the cleanup-only preview's blockers up to GREEK.
            blockers = list(preview_alpha.swap_blockers)

            if not preview_alpha.cleanup_eligible:
                # Class/DR/polyline made cleanup ineligible. Surface the
                # reason but keep the fingerprint so the UI can still
                # operate.
                return GreekPipelinePreview(
                    queue_id=str(approval_row.get("queue_id")),
                    route_code=approval_row.get("route_code"),
                    quality_class=approval_row.get("quality_class"),
                    state_fingerprint=fingerprint,
                    cleanup_eligible=False,
                    cleanup_skip_reason=preview_alpha.cleanup_skip_reason,
                    cleanup_report=None,
                    cleanup_safety_gate="blocked",
                    cleanup_safety_reason=None,
                    refill_eligible=False,
                    refill_candidates=[],
                    refill_high_confidence_count=0,
                    refill_medium_confidence_count=0,
                    would_apply=False,
                    blockers=blockers,
                )

            cleanup_report = preview_alpha.cleanup_report
            cleanup_ok = preview_alpha.would_apply
            cleanup_safety = preview_alpha.safety_gate_status

            # Compute the post-cleanup stops for refill input. We re-run
            # cleanup_orphan_stops here (cheap) so we don't have to
            # change SwapPreview's surface to carry the cleaned list.
            polyline = _polyline_from_proposed_shape(
                approval_row.get("proposed_shape")
            )
            proposed_stops = approval_row.get("proposed_stops") or []
            if isinstance(proposed_stops, str):
                try:
                    proposed_stops = json.loads(proposed_stops)
                except Exception:
                    proposed_stops = []
            cleaned_stops, _ = cleanup_orphan_stops(proposed_stops, polyline)

            # γ — refill candidates (only when cleanup would apply).
            refill_candidates_dicts: list[dict[str, Any]] = []
            high_conf = 0
            medium_conf = 0
            if cleanup_ok:
                province = (
                    approval_row.get("policy_flags") or {}
                ).get("province") or "sample_region"
                if production_routes is None:
                    pool = _load_production_routes_pool(
                        cur,
                        exclude_route_id=str(
                            approval_row.get("route_code") or ""
                        ),
                        proposed_shape=approval_row.get("proposed_shape"),
                        province=province,
                    )
                else:
                    pool = list(production_routes)

                candidates = find_refill_candidates(
                    route_a_id=str(approval_row.get("route_code") or ""),
                    polyline_a=polyline,
                    existing_stops_a=cleaned_stops,
                    production_routes=pool,
                )
                refill_candidates_dicts = [
                    _refill_candidate_to_dict(c) for c in candidates
                ]
                high_conf = sum(
                    1 for c in refill_candidates_dicts
                    if c["confidence_band"] == "high"
                )
                medium_conf = sum(
                    1 for c in refill_candidates_dicts
                    if c["confidence_band"] == "medium"
                )

                # Cache the surfaced candidates on the row so confirm
                # can audit exactly what the operator was shown.
                _persist_refill_candidates(
                    cur, approval_queue_id, refill_candidates_dicts
                )

            conn.commit()

            return GreekPipelinePreview(
                queue_id=str(approval_row.get("queue_id")),
                route_code=approval_row.get("route_code"),
                quality_class=approval_row.get("quality_class"),
                state_fingerprint=fingerprint,
                cleanup_eligible=True,
                cleanup_skip_reason=None,
                cleanup_report=cleanup_report,
                cleanup_safety_gate=cleanup_safety,
                cleanup_safety_reason=preview_alpha.safety_gate_reason,
                refill_eligible=cleanup_ok,
                refill_candidates=refill_candidates_dicts,
                refill_high_confidence_count=high_conf,
                refill_medium_confidence_count=medium_conf,
                would_apply=cleanup_ok,
                blockers=blockers,
            )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_VALID_REFILL_DECISIONS: frozenset[str] = frozenset(
    {"accepted", "skipped", "reviewed_later"}
)


def _apply_v2_canonical_state(
    cur,
    *,
    route_id: str,
    route_code: str,
    queue_id: str,
    wkt: str,
    v2_node_ids: list[str],
    v2_version: int,
    operator_username: str,
) -> dict[str, str]:
    """Persist post-swap canonical state inside the swap transaction.

    Closes gaps #1–#3 and #6 from
    ``workspace/audits/2026-05-03_greek_swap_upgrade_v2_design.md``:
    inserts a fresh ``stop_sequence_candidate_set`` + candidate (rank 1)
    and a matching ``geometry_candidate_set`` + candidate (engine
    ``greek_post_swap_v2``); points
    ``route_prod.routes.chosen_*_candidate_id`` at them; flips
    ``canonical_sequence_ready=TRUE``; supersedes any active
    ``runtime_route_estimates`` for the route and clears
    ``route_runtime_estimate_bindings`` so Runtime Lab re-binds against
    the new stop sequence.

    All writes share the caller's connection / cursor and roll back
    together if anything downstream raises.

    Returns a dict of the four UUIDs created so the caller can stamp
    them into ``fix_reports.fixes_applied`` for lineage.
    """
    cur.execute(
        """
        INSERT INTO route_work.stop_sequence_candidate_sets
          (route_id, created_by, generator_version, notes)
        VALUES (%s::uuid, %s, %s, %s)
        RETURNING set_id::text AS set_id
        """,
        (
            route_id,
            operator_username,
            "greek_swap_v2",
            "post-swap canonical persistence",
        ),
    )
    seq_set_id = cur.fetchone()["set_id"]

    cur.execute(
        """
        INSERT INTO route_work.stop_sequence_candidates
          (set_id, rank, stop_node_ids, matched_stops, metrics)
        VALUES (%s::uuid, %s, %s::uuid[], %s, %s::jsonb)
        RETURNING candidate_id::text AS candidate_id
        """,
        (
            seq_set_id,
            1,
            v2_node_ids,
            len(v2_node_ids),
            json.dumps(
                {
                    "source": "greek_swap_v2",
                    "queue_id": queue_id,
                    "route_code": route_code,
                    "v2_version": v2_version,
                }
            ),
        ),
    )
    seq_cand_id = cur.fetchone()["candidate_id"]

    cur.execute(
        """
        INSERT INTO route_work.geometry_candidate_sets
          (route_id, stop_sequence_set_id, created_by,
           generator_version, notes)
        VALUES (%s::uuid, %s::uuid, %s, %s, %s)
        RETURNING set_id::text AS set_id
        """,
        (
            route_id,
            seq_set_id,
            operator_username,
            "greek_swap_v2",
            "post-swap canonical persistence",
        ),
    )
    geom_set_id = cur.fetchone()["set_id"]

    cur.execute(
        """
        INSERT INTO route_work.geometry_candidates
          (set_id, stop_sequence_candidate_id, engine, geom,
           length_m, score, metrics)
        VALUES (%s::uuid, %s::uuid, %s,
                ST_SetSRID(ST_GeomFromText(%s), 4326),
                ST_Length(ST_SetSRID(ST_GeomFromText(%s), 4326)::geography),
                %s, %s::jsonb)
        RETURNING geometry_candidate_id::text AS geometry_candidate_id
        """,
        (
            geom_set_id,
            seq_cand_id,
            "greek_post_swap_v2",
            wkt,
            wkt,
            0.0,
            json.dumps(
                {
                    "source": "greek_swap_v2",
                    "queue_id": queue_id,
                    "route_code": route_code,
                    "v2_version": v2_version,
                    "derived_from": "approval_queue.proposed_shape",
                }
            ),
        ),
    )
    geom_cand_id = cur.fetchone()["geometry_candidate_id"]

    cur.execute(
        """
        UPDATE route_prod.routes
           SET chosen_geometry_candidate_id      = %s::uuid,
               chosen_stop_sequence_candidate_id = %s::uuid,
               canonical_sequence_ready          = TRUE,
               pipeline_version                  = 're_entry_v2'
         WHERE route_id = %s::uuid AND version = %s::smallint
        """,
        (geom_cand_id, seq_cand_id, route_id, v2_version),
    )
    if cur.rowcount != 1:
        raise SwapError(
            "v2 canonical-state UPDATE on route_prod.routes affected "
            f"{cur.rowcount} rows (expected 1)"
        )

    cur.execute(
        """
        UPDATE gtfs_work.runtime_route_estimates
           SET status = 'superseded'
         WHERE route_id = %s::uuid AND status = 'active'
        """,
        (route_id,),
    )
    cur.execute(
        """
        DELETE FROM gtfs_work.route_runtime_estimate_bindings
         WHERE route_id = %s::uuid
        """,
        (route_id,),
    )

    return {
        "geom_set_id": geom_set_id,
        "geom_candidate_id": geom_cand_id,
        "seq_set_id": seq_set_id,
        "seq_candidate_id": seq_cand_id,
    }


def confirm_greek_pipeline(
    *,
    approval_queue_id: str,
    expected_fingerprint: str,
    refill_decisions: dict[str, str],
    operator_id: str,
    operator_username: str,
    dsn: str = DEFAULT_DSN,
    operator_override: bool = False,
    override_reason: Optional[str] = None,
) -> GreekPipelineResult:
    """Public dispatch — selects v1 or v2 path based on env feature flag.

    Default OFF (``HADES_GREEK_SWAP_V2`` unset / ``0``) → v1 path,
    bit-identical to the pre-upgrade behavior. ``=1`` → v2 atomic
    canonical-state flow per
    ``workspace/audits/2026-05-03_greek_swap_upgrade_v2_design.md``.
    """
    if _greek_swap_v2_enabled():
        return _confirm_greek_pipeline_v2(
            approval_queue_id=approval_queue_id,
            expected_fingerprint=expected_fingerprint,
            refill_decisions=refill_decisions,
            operator_id=operator_id,
            operator_username=operator_username,
            dsn=dsn,
            operator_override=operator_override,
            override_reason=override_reason,
        )
    return _confirm_greek_pipeline_v1(
        approval_queue_id=approval_queue_id,
        expected_fingerprint=expected_fingerprint,
        refill_decisions=refill_decisions,
        operator_id=operator_id,
        operator_username=operator_username,
        dsn=dsn,
        operator_override=operator_override,
        override_reason=override_reason,
    )


def _confirm_greek_pipeline_v1(
    *,
    approval_queue_id: str,
    expected_fingerprint: str,
    refill_decisions: dict[str, str],
    operator_id: str,
    operator_username: str,
    dsn: str = DEFAULT_DSN,
    operator_override: bool = False,
    override_reason: Optional[str] = None,
) -> GreekPipelineResult:
    """v1 swap (β/α/γ/δ) — identical to the original pre-2026-05-03 flow."""
    return _confirm_greek_pipeline_inner(
        approval_queue_id=approval_queue_id,
        expected_fingerprint=expected_fingerprint,
        refill_decisions=refill_decisions,
        operator_id=operator_id,
        operator_username=operator_username,
        dsn=dsn,
        operator_override=operator_override,
        override_reason=override_reason,
        apply_v2_canonical_state=False,
    )


def _confirm_greek_pipeline_v2(
    *,
    approval_queue_id: str,
    expected_fingerprint: str,
    refill_decisions: dict[str, str],
    operator_id: str,
    operator_username: str,
    dsn: str = DEFAULT_DSN,
    operator_override: bool = False,
    override_reason: Optional[str] = None,
) -> GreekPipelineResult:
    """v2 swap — v1 flow plus atomic canonical-state propagation.

    Closes the seven gaps catalogued in the design doc by writing fresh
    candidate rows, pointing ``routes.chosen_*_candidate_id`` at them,
    flipping ``canonical_sequence_ready=TRUE``, and invalidating stale
    runtime estimates + bindings — all in the same transaction.
    """
    return _confirm_greek_pipeline_inner(
        approval_queue_id=approval_queue_id,
        expected_fingerprint=expected_fingerprint,
        refill_decisions=refill_decisions,
        operator_id=operator_id,
        operator_username=operator_username,
        dsn=dsn,
        operator_override=operator_override,
        override_reason=override_reason,
        apply_v2_canonical_state=True,
    )


def _confirm_greek_pipeline_inner(
    *,
    approval_queue_id: str,
    expected_fingerprint: str,
    refill_decisions: dict[str, str],
    operator_id: str,
    operator_username: str,
    dsn: str = DEFAULT_DSN,
    operator_override: bool = False,
    override_reason: Optional[str] = None,
    apply_v2_canonical_state: bool = False,
) -> GreekPipelineResult:
    """Atomically execute β → α → γ → δ.

    Single transaction:

      β  Lock the approval_queue row, recompute fingerprint, abort
         strictly on mismatch (no auto-retry).
      α  Apply cleanup. Refuse on safety-gate reject unless
         ``operator_override=True`` is supplied with a non-empty reason.
      γ  Validate that ``refill_decisions`` covers EVERY candidate
         surfaced at preview time. Apply only ``accepted`` candidates.
         Log every decision (accepted/skipped/reviewed_later) to
         ``route_prod.refill_audit``.
      δ  Run the swap (insert synthetic nodes, UPDATE routes, INSERT
         fix_reports, transition queues) using cleaned-plus-refilled
         stops.

    On any failure the entire transaction rolls back. Returns a
    :class:`GreekPipelineResult` shaped per spec — ``success=True``
    indicates β/α/γ/δ all completed, ``success=False`` carries an
    ``error`` string with no DB writes left behind.
    """
    if not expected_fingerprint or not expected_fingerprint.strip():
        raise ValueError(
            "expected_fingerprint required — call preview_greek_pipeline "
            "first and pass its state_fingerprint here"
        )
    if operator_override:
        if not (override_reason or "").strip() or len(
            (override_reason or "").strip()
        ) < 20:
            raise ValueError(
                "override_reason required (min 20 chars) when "
                "operator_override=True"
            )
    for sid, decision in refill_decisions.items():
        if decision not in _VALID_REFILL_DECISIONS:
            raise ValueError(
                f"invalid refill decision {decision!r} for stop_id={sid}; "
                f"must be one of {sorted(_VALID_REFILL_DECISIONS)}"
            )

    conn = psycopg2.connect(need_dsn(dsn))
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SET CONSTRAINTS route_prod.fk_re_entry_queue_route DEFERRED"
            )

            # ---- β: load + fingerprint check ---------------------------
            cur.execute(
                """
                SELECT queue_id, route_code, version, status,
                       decision_reasons, policy_flags,
                       geometry_report, stop_coverage_report,
                       proposed_stops, proposed_shape,
                       quality_class, pre_ship_cleanup_applied,
                       pending_dr_batches,
                       proposed_refill_candidates,
                       crashed, crash_payload
                  FROM route_prod.approval_queue
                 WHERE queue_id = %s::uuid
                 FOR UPDATE
                """,
                (approval_queue_id,),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=None,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        f"approval_queue queue_id={approval_queue_id} "
                        "not found"
                    ),
                )
            approval_row = dict(row)

            current_fp = compute_state_fingerprint(approval_row)
            if current_fp != expected_fingerprint:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=None,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        "State changed since preview "
                        f"(expected {expected_fingerprint[:8]}…, "
                        f"got {current_fp[:8]}…). "
                        "Refresh preview and try again."
                    ),
                )

            preview_alpha = _build_swap_preview(approval_row)
            if preview_alpha.swap_blockers:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=None,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        "swap blocked: "
                        + "; ".join(preview_alpha.swap_blockers)
                    ),
                )
            if not preview_alpha.cleanup_eligible:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=None,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        "GREEK pipeline requires a shippable, DR-clean "
                        "row: "
                        + (preview_alpha.cleanup_skip_reason or "ineligible")
                    ),
                )

            # ---- α: cleanup -------------------------------------------
            polyline = _polyline_from_proposed_shape(
                approval_row.get("proposed_shape")
            )
            proposed_stops = approval_row.get("proposed_stops") or []
            if isinstance(proposed_stops, str):
                proposed_stops = json.loads(proposed_stops)
            cleaned, fresh_report = cleanup_orphan_stops(
                proposed_stops, polyline,
            )
            cleanup_ok, cleanup_reason = validate_cleanup_not_destructive(
                proposed_stops,
                cleaned,
                max_removal_pct=fresh_report.thresholds_used.get(
                    "max_removal_pct", 0.20
                ),
            )
            cleanup_report_persisted = _report_to_jsonable(
                fresh_report,
                validate_ok=cleanup_ok,
                validate_reason=cleanup_reason,
            )
            override_used_flag = False
            if not cleanup_ok and not operator_override:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=cleanup_report_persisted,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=f"Cleanup safety gate: {cleanup_reason}",
                )

            # ---- γ: refill --------------------------------------------
            cached_candidates = approval_row.get(
                "proposed_refill_candidates"
            ) or []
            if isinstance(cached_candidates, str):
                try:
                    cached_candidates = json.loads(cached_candidates)
                except Exception:
                    cached_candidates = []

            surfaced_ids = {
                str(c.get("stop_id"))
                for c in cached_candidates
                if c.get("stop_id")
            }
            decided_ids = {str(k) for k in refill_decisions.keys()}
            missing = surfaced_ids - decided_ids
            if missing:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=cleanup_report_persisted,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        f"Missing decisions for {len(missing)} refill "
                        "candidate(s). All surfaced candidates must be "
                        "reviewed before confirming."
                    ),
                )

            accepted_stops: list[dict[str, Any]] = []
            for cand in cached_candidates:
                sid = str(cand.get("stop_id") or "")
                if not sid:
                    continue
                decision = refill_decisions.get(sid, "skipped")
                if decision == "accepted":
                    accepted_stops.append(
                        {
                            "stop_id": sid,
                            "lat": float(cand.get("lat", 0.0)),
                            "lon": float(cand.get("lon", 0.0)),
                            "flag": "refill_added",
                        }
                    )

            final_stops = list(cleaned) + accepted_stops

            # Persist cleanup + refill state on approval_queue.
            cur.execute(
                """
                UPDATE route_prod.approval_queue
                   SET proposed_stops             = %s::jsonb,
                       pre_ship_cleanup_applied   = TRUE,
                       pre_ship_cleanup_report    = %s::jsonb,
                       pre_ship_cleanup_at        = NOW(),
                       refill_decisions           = %s::jsonb,
                       refill_applied             = TRUE,
                       refill_at                  = NOW()
                 WHERE queue_id = %s::uuid
                """,
                (
                    json.dumps(final_stops),
                    json.dumps(cleanup_report_persisted),
                    json.dumps(refill_decisions),
                    approval_queue_id,
                ),
            )
            approval_row["proposed_stops"] = final_stops
            approval_row["pre_ship_cleanup_applied"] = True

            # Audit every decision (accepted/skipped/reviewed_later).
            for cand in cached_candidates:
                sid = str(cand.get("stop_id") or "")
                if not sid:
                    continue
                decision = refill_decisions.get(sid, "skipped")
                cur.execute(
                    """
                    INSERT INTO route_prod.refill_audit (
                      queue_id, route_code, candidate_stop_id,
                      decision, decided_by, candidate_score,
                      candidate_metadata
                    ) VALUES (
                      %s::uuid, %s, %s::uuid, %s, %s, %s, %s::jsonb
                    )
                    """,
                    (
                        approval_queue_id,
                        approval_row.get("route_code"),
                        sid,
                        decision,
                        operator_username,
                        cand.get("score"),
                        json.dumps(cand),
                    ),
                )

            if operator_override and not cleanup_ok:
                override_used_flag = True
                cur.execute(
                    """
                    INSERT INTO route_prod.cleanup_override_audit (
                      queue_id, route_code, override_reason,
                      override_by, cleanup_report_at_override
                    ) VALUES (%s::uuid, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        approval_queue_id,
                        approval_row.get("route_code"),
                        (override_reason or "").strip(),
                        operator_username,
                        json.dumps(cleanup_report_persisted),
                    ),
                )

            # ---- δ: swap ----------------------------------------------
            flags = approval_row.get("policy_flags") or {}
            re_entry_queue_id = flags.get("re_entry_queue_id")
            re_row = _load_re_entry_row(cur, re_entry_queue_id)
            if re_row["status"] != "v2_ready":
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=cleanup_report_persisted,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        f"re_entry_queue status={re_row['status']} "
                        "(expected 'v2_ready')"
                    ),
                )

            route_id = str(re_row["route_id"])
            v1_version = int(re_row["current_version"])
            v2_version = int(approval_row["version"])
            if v2_version != v1_version + 1:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=cleanup_report_persisted,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        f"version mismatch: v1={v1_version}, "
                        f"v2={v2_version} (expected {v1_version + 1})"
                    ),
                )

            route_row = _load_route_row(cur, route_id, v1_version)
            metrics_before = _v1_metrics(cur, route_row)
            proposed_shape = approval_row["proposed_shape"]
            coords_v2 = _extract_linestring_from_geojson(proposed_shape)
            proposed_stops_final = approval_row["proposed_stops"] or []

            metrics_after = _v2_metrics(
                approval_row, coords_v2, proposed_stops_final,
            )

            province = route_row.get("province") or "sample_region"
            v2_node_ids, created_synth = _materialise_v2_stop_node_ids(
                cur,
                proposed_stops=proposed_stops_final,
                province=province,
                operator_username=operator_username,
            )

            wkt = (
                "LINESTRING("
                + ", ".join(f"{lon} {lat}" for (lon, lat) in coords_v2)
                + ")"
            )

            cur.execute(
                """
                UPDATE route_prod.routes
                   SET geom = ST_SetSRID(ST_GeomFromText(%s), 4326),
                       stop_node_ids = %s::uuid[],
                       version = %s,
                       last_swap_at = NOW(),
                       last_swap_from_version = %s,
                       geometry_enforcer_report = %s::jsonb,
                       stop_coverage_report = %s::jsonb,
                       legacy_grandfathered = FALSE,
                       grandfathered_until = NULL,
                       pipeline_version = 're_entry_v1',
                       valhalla_request = COALESCE(valhalla_request, '{}'::jsonb),
                       quality_gate_passed_at = NOW(),
                       approved_by_user = %s::uuid,
                       approved_at = NOW(),
                       updated_at = NOW()
                 WHERE route_id = %s::uuid AND version = %s::smallint
                """,
                (
                    wkt,
                    v2_node_ids,
                    v2_version,
                    v1_version,
                    json.dumps(approval_row["geometry_report"] or {}),
                    json.dumps(approval_row["stop_coverage_report"] or {}),
                    operator_id,
                    route_id,
                    v1_version,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return GreekPipelineResult(
                    success=False,
                    cleanup_applied=False,
                    cleanup_report=cleanup_report_persisted,
                    refill_applied=False,
                    refill_decisions_count=0,
                    refill_accepted_count=0,
                    swap_id=None,
                    override_used=False,
                    error=(
                        f"routes UPDATE affected {cur.rowcount} rows "
                        "(expected 1)"
                    ),
                )

            v2_canonical_ids: Optional[dict[str, str]] = None
            if apply_v2_canonical_state:
                v2_canonical_ids = _apply_v2_canonical_state(
                    cur,
                    route_id=route_id,
                    route_code=str(approval_row.get("route_code") or ""),
                    queue_id=approval_queue_id,
                    wkt=wkt,
                    v2_node_ids=v2_node_ids,
                    v2_version=v2_version,
                    operator_username=operator_username,
                )

            fix_category = flags.get("fix_category") or "structural"
            fixes_applied = _build_fixes_applied(
                approval_row,
                coords_v2_count=len(coords_v2),
                stops_v2_count=len(proposed_stops_final),
                created_synthetic_ids=created_synth,
            )
            if v2_canonical_ids is not None:
                fixes_applied["greek_swap_v2_canonical"] = v2_canonical_ids
            cur.execute(
                """
                INSERT INTO route_prod.fix_reports (
                  route_id, version_before, version_after, fix_category,
                  fixes_applied, metrics_before, metrics_after,
                  applied_at, applied_by
                )
                VALUES (%s::uuid, %s, %s, %s, %s::jsonb, %s::jsonb,
                        %s::jsonb, NOW(), %s)
                RETURNING report_id::text
                """,
                (
                    route_id,
                    v1_version,
                    v2_version,
                    fix_category,
                    json.dumps(fixes_applied),
                    json.dumps(metrics_before),
                    json.dumps(metrics_after),
                    operator_username,
                ),
            )
            fix_report_id = str(cur.fetchone()["report_id"])

            accepted_count = len(accepted_stops)
            resolution_suffix = (
                "[APPROVED] greek pipeline applied"
                + " + cleanup"
                + (
                    f" + refill({accepted_count}/{len(cached_candidates)})"
                    if cached_candidates else ""
                )
                + (" + override" if override_used_flag else "")
            )
            cur.execute(
                """
                UPDATE route_prod.approval_queue
                   SET status = 'approved',
                       resolved_at = NOW(),
                       resolved_by = %s,
                       resolution_notes = COALESCE(resolution_notes, '') || %s
                 WHERE queue_id = %s::uuid
                """,
                (
                    operator_username,
                    resolution_suffix,
                    approval_queue_id,
                ),
            )
            cur.execute(
                """
                UPDATE route_prod.re_entry_queue
                   SET status = 'swapped',
                       current_version = %s::smallint,
                       resolved_at = NOW()
                 WHERE queue_id = %s::uuid
                """,
                (v2_version, re_entry_queue_id),
            )

        conn.commit()
        return GreekPipelineResult(
            success=True,
            cleanup_applied=True,
            cleanup_report=cleanup_report_persisted,
            refill_applied=bool(cached_candidates),
            refill_decisions_count=len(cached_candidates),
            refill_accepted_count=len(accepted_stops),
            swap_id=fix_report_id,
            override_used=override_used_flag,
            error=None,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = [
    "SwapError",
    "SwapResult",
    "SwapPreview",
    "GreekPipelinePreview",
    "GreekPipelineResult",
    "preview_swap_with_cleanup",
    "confirm_swap_with_cleanup",
    "preview_greek_pipeline",
    "confirm_greek_pipeline",
    "compute_state_fingerprint",
    "approve_v2",  # deprecated wrapper
    "reject_v2",
    "quarantine_v2",
]
