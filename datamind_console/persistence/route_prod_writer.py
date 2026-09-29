"""Canonical wrapper for writes to route_prod.routes.

This is the ONLY code path that should write to route_prod.routes from
application code. Direct INSERT/UPDATE/DELETE against route_prod.* from
application code has been REVOKED at the DB level (symbolic while the app role
is superuser; effective once the app connects as ``route_prod_writer``).

Write routing (post-enforcement activation, 2026-04-21):

  * ``legacy_grandfathered=True``  → direct SQL UPSERT emitting
    ``legacy_grandfathered=TRUE`` + ``grandfathered_until``. Trigger lets it
    pass until the grandfathering deadline (``DEFAULT_GRANDFATHERED_UNTIL``,
    2026-07-20). After the deadline, grandfathered writes start failing.

  * ``legacy_grandfathered=False`` AND ``USE_STORED_PROCEDURE=True`` →
    ``SELECT route_prod.route_prod_writer_sp(...)``. The SP is
    ``SECURITY DEFINER`` — the only write path that still works once the
    app connects as ``route_prod_writer`` (which lacks direct write grants).

  * ``legacy_grandfathered=False`` AND ``USE_STORED_PROCEDURE=False`` →
    direct SQL UPSERT; requires all 5 audit payloads (pipeline_version,
    valhalla_request, geometry_enforcer_report, stop_coverage_report,
    quality_gate_passed_at) or the trigger rejects the write.

The SP does NOT accept Phase-4 naming columns (deploy_status,
direction_semantics, route_name, route_aliases, …). Callers that need
those must either stay on the direct-SQL path (legacy_grandfathered=True)
or follow the SP call with a ``patch_route_prod_fields(...)`` UPDATE.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

# Post-enforcement-activation: non-grandfathered writes go through the SP.
# Grandfathered writes still go through direct SQL because the SP hardcodes
# legacy_grandfathered=FALSE.
USE_STORED_PROCEDURE: bool = True

# Default grandfathering deadline applied when legacy_grandfathered=True is
# passed without an explicit grandfathered_until. Matches the activation log
# (90 days from 2026-04-21). After this date, new grandfathered writes start
# failing the trigger's expiry check — callers must migrate to full audit by
# then.
DEFAULT_GRANDFATHERED_UNTIL = datetime(2026, 7, 20, tzinfo=timezone.utc)

# DB-level enum enforced by the routes_source_type_valid CHECK constraint.
_VALID_DB_SOURCE_TYPES: frozenset[str] = frozenset({
    "constructor_canonical",
    "osm_relation_import",
    "manual_constructor",
    "discovery_pipeline",
    "deep_research_override",
    "synthetic_fill",
})


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class WriteResult:
    success: bool
    route_code: str
    version: int
    rows_affected: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class BulkWriteResult:
    """Return type for wrapper methods that affect multiple route_prod rows.

    `affected_route_codes` is the list of route_id strings that the bulk
    operation actually touched (as returned via SQL RETURNING), so callers
    can audit, log, or re-enqueue downstream work per-row.
    """
    success: bool
    operation: str
    rows_affected: dict[str, int] = field(default_factory=dict)
    affected_route_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_REQUIRED_AUDIT_WHEN_NOT_GRANDFATHERED = (
    "valhalla_request",
    "geometry_enforcer_report",
    "stop_coverage_report",
    "quality_gate_passed_at",
)

_ALLOWED_MODES = ("insert", "upsert", "update")


class WriterValidationError(ValueError):
    """Raised when mandatory arguments are missing or malformed."""


def _validate_inputs(
    *,
    route_code: str,
    route_data: dict,
    stops: list,
    shape: dict,
    source_type: str,
    pipeline_version: str,
    mode: str,
) -> None:
    if not route_code or not isinstance(route_code, str):
        raise WriterValidationError("route_code is required and must be a non-empty string")
    if not isinstance(route_data, dict) or not route_data:
        raise WriterValidationError("route_data must be a non-empty dict")
    if stops is None or not isinstance(stops, list):
        raise WriterValidationError("stops must be a list (may be empty)")
    if not isinstance(shape, dict) or not shape:
        raise WriterValidationError("shape must be a non-empty dict")
    if not source_type or not isinstance(source_type, str):
        raise WriterValidationError("source_type is required (non-negotiable audit field)")
    if not pipeline_version or not isinstance(pipeline_version, str):
        raise WriterValidationError("pipeline_version is required (non-negotiable audit field)")
    if mode not in _ALLOWED_MODES:
        raise WriterValidationError(f"mode must be one of {_ALLOWED_MODES}, got {mode!r}")
    if "route_id" not in route_data:
        raise WriterValidationError("route_data['route_id'] is required (UUID or str)")


# ---------------------------------------------------------------------------
# Shape / stops normalization
# ---------------------------------------------------------------------------

def phase3_end_sweep_for_stops(
    stop_node_ids: list, conn, *, caller: str = "phase3_end_sweep",
) -> dict:
    """End-of-Phase-3 safety net.

    Runs treat_stop(operation='phase3_end_audit') for every node_id passed
    in. Idempotent — clean names produce no UPDATE; only contaminated names
    are repaired. Every call writes an audit row in
    ``node_prod.stop_treatment_log`` so divergences from the treater
    contract are visible after the fact.

    Returns ``{'total': N, 'fixed': K, 'failed': M}``.

    Operates within the caller's transaction. Caller controls commit /
    rollback.
    """
    from phase3_routes.services.stop_quality import (
        StopTreatmentInput,
        treat_stop,
    )

    total = fixed = failed = 0
    with conn.cursor() as cur:
        for nid in stop_node_ids:
            cur.execute(
                "SELECT name, ST_Y(geom::geometry), ST_X(geom::geometry) "
                "FROM node_prod.nodes WHERE node_id = %s::uuid",
                (str(nid),),
            )
            row = cur.fetchone()
            if not row:
                continue
            name, lat, lon = row
            total += 1
            res = treat_stop(
                StopTreatmentInput(
                    operation="phase3_end_audit",
                    caller=caller,
                    node_id=uuid.UUID(str(nid)),
                    proposed_name=name,
                    proposed_lat=float(lat) if lat is not None else None,
                    proposed_lon=float(lon) if lon is not None else None,
                ),
                conn,
            )
            if not res.success:
                failed += 1
            elif res.final_name and res.final_name != name:
                fixed += 1
    return {"total": total, "fixed": fixed, "failed": failed}


def _normalize_stop_node_ids(stops: list) -> list[str]:
    """Accept list of UUID strings OR list of {'node_id': ..., 'order': ...} dicts.

    Preserves order. Returns list[str].
    """
    if not stops:
        return []
    first = stops[0]
    if isinstance(first, (str, uuid.UUID)):
        return [str(s) for s in stops]
    if isinstance(first, dict):
        key = "node_id" if "node_id" in first else ("stop_id" if "stop_id" in first else None)
        if key is None:
            raise WriterValidationError(
                "stops dicts must contain 'node_id' or 'stop_id' key"
            )
        ordered = stops
        if "order" in first or "ord" in first:
            ord_key = "order" if "order" in first else "ord"
            ordered = sorted(stops, key=lambda s: int(s.get(ord_key) or 0))
        return [str(s[key]) for s in ordered]
    raise WriterValidationError(
        f"stops must contain UUID/str or dict elements, got {type(first).__name__}"
    )


def _shape_to_sql_and_param(shape: dict) -> tuple[str, Any]:
    """Return (SQL placeholder expression, parameter value) for the geom column.

    Accepts:
        {'wkt': 'LINESTRING(...)'} → ST_GeomFromText placeholder + wkt
        {'geojson': <str-or-dict>}  → ST_SetSRID(ST_GeomFromGeoJSON, 4326)
        {'raw': <psycopg bytes>}    → plain %s (already a PostGIS binding)
    """
    if "raw" in shape:
        return "%s", shape["raw"]
    if "wkt" in shape:
        return "ST_GeomFromText(%s, 4326)", shape["wkt"]
    if "geojson" in shape:
        gj = shape["geojson"]
        if not isinstance(gj, str):
            gj = json.dumps(gj)
        return "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)", gj
    raise WriterValidationError(
        "shape must contain one of: 'raw', 'wkt', 'geojson'"
    )


# ---------------------------------------------------------------------------
# Column set — current schema (no audit columns yet — Prompt 2 adds them)
# ---------------------------------------------------------------------------

# Columns that callers may populate on route_prod.routes today.
# Additional columns with DB-side defaults are omitted unless passed explicitly.
# NB: `source` is optional too — DB default 'route_constructor' applies when
# omitted, matching legacy behavior of callers that never set it.
_OPTIONAL_COLUMNS: tuple[str, ...] = (
    "source",
    "chosen_geometry_candidate_id",
    "chosen_stop_sequence_candidate_id",
    "canonical_sequence_ready",
    "sequence_approved_at",
    "sequence_approved_by",
    "service_route_id",
    "direction_id",
    "route_name",
    "route_aliases",
    "landmark_tags",
    "direction_semantics",
    "naming_confidence",
    "human_verified",
    "deploy_status",
    "semantics_updated_at",
)


def _resolve_db_source_type(
    *,
    source_type: str,
    db_source_type: Optional[str],
    legacy_grandfathered: bool,
) -> str:
    """Resolve the DB ``source_type`` column value from wrapper inputs.

    The ``source_type`` kwarg has historically been used by callers as a
    free-text provenance string (e.g. ``'promote_chongon_routes'``) — not
    one of the 6 CHECK-enforced enum values. Callers keep using it as
    audit/log identifier; this resolver picks a valid enum for the DB:

      1. Explicit ``db_source_type`` override wins.
      2. If ``source_type`` already matches the enum, use it.
      3. Fallback: ``'manual_constructor'`` for grandfathered writes
         (safe bucket for legacy bulk imports), ``'constructor_canonical'``
         for canonical canonical non-grandfathered writes.
    """
    if db_source_type is not None:
        if db_source_type not in _VALID_DB_SOURCE_TYPES:
            raise WriterValidationError(
                f"db_source_type={db_source_type!r} not in DB enum "
                f"{sorted(_VALID_DB_SOURCE_TYPES)}"
            )
        return db_source_type
    if source_type in _VALID_DB_SOURCE_TYPES:
        return source_type
    return "manual_constructor" if legacy_grandfathered else "constructor_canonical"


def _build_upsert_sql(
    route_data: dict,
    shape_sql: str,
    *,
    version: int,
    db_source_type: str,
    pipeline_version: str,
    legacy_grandfathered: bool,
    grandfathered_until: Optional[datetime],
    valhalla_request: Optional[dict],
    geometry_enforcer_report: Optional[dict],
    stop_coverage_report: Optional[dict],
    quality_gate_passed_at: Optional[datetime],
    approved_by_user: Optional[str],
    approved_at: Optional[datetime],
    on_conflict_preserve: frozenset[str] = frozenset(),
) -> tuple[str, list[str], list[Any]]:
    """Construct the INSERT … ON CONFLICT DO UPDATE statement.

    Returns (sql, present_optionals_from_route_data, audit_param_values).

    The caller assembles the final params list by interleaving the
    route_data optionals with audit_param_values in the order this builder
    appended them to the column list.

    Columns classified as:
      * **Hard-required** — ``route_id, geom, stop_node_ids, province,
        version, source_type, pipeline_version``. Always emitted.
      * **Audit conditional** — only emitted when non-None. When
        ``legacy_grandfathered=False`` and the row-level CHECK constraint
        ``routes_audit_trail_required`` demands all 4 payloads, validation
        should have raised before reaching this builder.
      * **Grandfathered XOR audit** — per the
        ``routes_grandfathered_xor_audit`` CHECK: if
        ``legacy_grandfathered=True`` we emit
        ``legacy_grandfathered, grandfathered_until``; if False we leave
        them at the DB default (FALSE / NULL).
      * **route_data optionals** — caller-set Phase-4 / Phase-3 columns
        (``_OPTIONAL_COLUMNS``). Only emitted when present and non-None.

    ``on_conflict_preserve`` names columns that appear in VALUES (so new
    rows get the caller's value) but are excluded from ``DO UPDATE SET``
    so existing row values are preserved across re-runs.

    The composite PK ``(route_id, version)`` drives ON CONFLICT — legacy
    callers writing ``version=1`` keep upsert semantics identical to the
    old ``(route_id)``-only conflict target.
    """
    cols: list[str] = [
        "route_id", "geom", "stop_node_ids", "province",
        "version", "source_type", "pipeline_version",
    ]
    placeholders: list[str] = [
        "%s::uuid", shape_sql, "%s::uuid[]", "%s",
        "%s::smallint", "%s", "%s",
    ]

    audit_params: list[Any] = [version, db_source_type, pipeline_version]

    def _emit(col: str, placeholder: str, value: Any) -> None:
        cols.append(col)
        placeholders.append(placeholder)
        audit_params.append(value)

    if legacy_grandfathered:
        effective_deadline = grandfathered_until or DEFAULT_GRANDFATHERED_UNTIL
        _emit("legacy_grandfathered", "%s", True)
        _emit("grandfathered_until", "%s::timestamptz", effective_deadline)
    # Non-grandfathered writes: legacy_grandfathered + grandfathered_until stay
    # at their DB defaults (FALSE / NULL) so the routes_grandfathered_xor_audit
    # CHECK constraint holds.

    if valhalla_request is not None:
        _emit(
            "valhalla_request", "%s::jsonb",
            json.dumps(valhalla_request) if not isinstance(valhalla_request, str) else valhalla_request,
        )
    if geometry_enforcer_report is not None:
        _emit(
            "geometry_enforcer_report", "%s::jsonb",
            json.dumps(geometry_enforcer_report) if not isinstance(geometry_enforcer_report, str) else geometry_enforcer_report,
        )
    if stop_coverage_report is not None:
        _emit(
            "stop_coverage_report", "%s::jsonb",
            json.dumps(stop_coverage_report) if not isinstance(stop_coverage_report, str) else stop_coverage_report,
        )
    if quality_gate_passed_at is not None:
        _emit("quality_gate_passed_at", "%s::timestamptz", quality_gate_passed_at)
    if approved_by_user is not None:
        _emit("approved_by_user", "%s::uuid", str(approved_by_user))
    if approved_at is not None:
        _emit("approved_at", "%s::timestamptz", approved_at)

    present_optionals: list[str] = []
    for col in _OPTIONAL_COLUMNS:
        if col in route_data and route_data[col] is not None:
            present_optionals.append(col)
            cols.append(col)
            placeholders.append("%s")

    # ON CONFLICT (route_id, version) — composite PK post-enforcement migration.
    # Columns the caller omitted stay at their current row value; columns in
    # on_conflict_preserve stay at their current value too (but ARE written on
    # INSERT so new rows get the caller's value).
    update_cols = [
        c for c in cols
        if c not in ("route_id", "version") and c not in on_conflict_preserve
    ] + ["updated_at"]
    set_expr = ",\n      ".join(
        f"{c} = EXCLUDED.{c}" if c != "updated_at" else "updated_at = now()"
        for c in update_cols
    )

    sql = (
        f"INSERT INTO route_prod.routes ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (route_id, version) DO UPDATE SET\n      {set_expr}"
    )
    return sql, present_optionals, audit_params


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def write_to_route_prod(
    route_code: str,
    route_data: dict,
    stops: list,
    shape: dict,
    source_type: str,
    pipeline_version: str,
    *,
    conn=None,
    valhalla_request: Optional[dict] = None,
    geometry_enforcer_report: Optional[dict] = None,
    stop_coverage_report: Optional[dict] = None,
    quality_gate_passed_at: Optional[datetime] = None,
    approved_by_user: Optional[str] = None,
    approved_at: Optional[datetime] = None,
    version: int = 1,
    legacy_grandfathered: bool = False,
    grandfathered_until: Optional[datetime] = None,
    db_source_type: Optional[str] = None,
    mode: Literal["insert", "upsert", "update"] = "upsert",
    on_conflict_preserve: frozenset[str] = frozenset(),
    phase3_end_audit: bool = False,
) -> WriteResult:
    """Sole code path that writes to route_prod.routes.

    `source_type` / `pipeline_version` are the caller's free-text audit
    identifiers. They MUST be non-empty. The DB column ``source_type`` is
    constrained by an enum; the wrapper resolves it from (in order):
    ``db_source_type`` override → matching ``source_type`` → a
    grandfathered/canonical default (see ``_resolve_db_source_type``).

    `valhalla_request`, `geometry_enforcer_report`, `stop_coverage_report`,
    and `quality_gate_passed_at` are REQUIRED for non-grandfathered writes
    post-activation. Missing any of them emits a MISSING_AUDIT_COLUMNS
    warning AND the trigger/CHECK will reject the write at the DB level.

    `conn` is a psycopg2/psycopg3 connection. The wrapper does NOT manage
    the connection lifecycle; callers own commit/rollback at their own
    transaction boundary.
    """
    _validate_inputs(
        route_code=route_code,
        route_data=route_data,
        stops=stops,
        shape=shape,
        source_type=source_type,
        pipeline_version=pipeline_version,
        mode=mode,
    )

    if phase3_end_audit:
        if conn is None:
            raise WriterValidationError(
                "phase3_end_audit=True requires conn (treater operates in caller's tx)"
            )
        sweep_ids = _normalize_stop_node_ids(stops)
        phase3_end_sweep_for_stops(
            sweep_ids, conn, caller=f"route_prod_writer:{route_code}",
        )

    warnings: list[str] = []
    if not legacy_grandfathered:
        missing = []
        if valhalla_request is None:
            missing.append("valhalla_request")
        if geometry_enforcer_report is None:
            missing.append("geometry_enforcer_report")
        if stop_coverage_report is None:
            missing.append("stop_coverage_report")
        if quality_gate_passed_at is None:
            missing.append("quality_gate_passed_at")
        if missing:
            msg = f"MISSING_AUDIT_COLUMNS: {route_code} — {', '.join(missing)}"
            logger.warning(msg)
            warnings.append(msg)

    resolved_db_source_type = _resolve_db_source_type(
        source_type=source_type,
        db_source_type=db_source_type,
        legacy_grandfathered=legacy_grandfathered,
    )

    # Non-grandfathered + full audit payload + flag on → SP. Grandfathered
    # writes always stay on direct SQL (SP hardcodes legacy_grandfathered=FALSE).
    use_sp = (
        USE_STORED_PROCEDURE
        and not legacy_grandfathered
        and valhalla_request is not None
        and geometry_enforcer_report is not None
        and stop_coverage_report is not None
        and quality_gate_passed_at is not None
    )

    if use_sp:
        return _write_via_stored_procedure(
            conn=conn,
            route_code=route_code,
            route_data=route_data,
            stops=stops,
            shape=shape,
            db_source_type=resolved_db_source_type,
            pipeline_version=pipeline_version,
            valhalla_request=valhalla_request,
            geometry_enforcer_report=geometry_enforcer_report,
            stop_coverage_report=stop_coverage_report,
            quality_gate_passed_at=quality_gate_passed_at,
            approved_by_user=approved_by_user,
            approved_at=approved_at,
            version=version,
            warnings=warnings,
        )

    return _write_via_direct_sql(
        conn=conn,
        route_code=route_code,
        route_data=route_data,
        stops=stops,
        shape=shape,
        mode=mode,
        version=version,
        db_source_type=resolved_db_source_type,
        pipeline_version=pipeline_version,
        legacy_grandfathered=legacy_grandfathered,
        grandfathered_until=grandfathered_until,
        valhalla_request=valhalla_request,
        geometry_enforcer_report=geometry_enforcer_report,
        stop_coverage_report=stop_coverage_report,
        quality_gate_passed_at=quality_gate_passed_at,
        approved_by_user=approved_by_user,
        approved_at=approved_at,
        warnings=warnings,
        on_conflict_preserve=on_conflict_preserve,
    )


# ---------------------------------------------------------------------------
# Direct-SQL implementation (pre-Prompt-3)
# ---------------------------------------------------------------------------

def _write_via_direct_sql(
    *,
    conn,
    route_code: str,
    route_data: dict,
    stops: list,
    shape: dict,
    mode: str,
    version: int,
    db_source_type: str,
    pipeline_version: str,
    legacy_grandfathered: bool,
    grandfathered_until: Optional[datetime],
    valhalla_request: Optional[dict],
    geometry_enforcer_report: Optional[dict],
    stop_coverage_report: Optional[dict],
    quality_gate_passed_at: Optional[datetime],
    approved_by_user: Optional[str],
    approved_at: Optional[datetime],
    warnings: list[str],
    on_conflict_preserve: frozenset[str] = frozenset(),
) -> WriteResult:
    if conn is None:
        raise WriterValidationError(
            "conn= keyword argument is required for direct-SQL writes"
        )

    stop_ids = _normalize_stop_node_ids(stops)
    shape_sql, shape_param = _shape_to_sql_and_param(shape)
    sql, optionals, audit_params = _build_upsert_sql(
        route_data,
        shape_sql,
        version=version,
        db_source_type=db_source_type,
        pipeline_version=pipeline_version,
        legacy_grandfathered=legacy_grandfathered,
        grandfathered_until=grandfathered_until,
        valhalla_request=valhalla_request,
        geometry_enforcer_report=geometry_enforcer_report,
        stop_coverage_report=stop_coverage_report,
        quality_gate_passed_at=quality_gate_passed_at,
        approved_by_user=approved_by_user,
        approved_at=approved_at,
        on_conflict_preserve=on_conflict_preserve,
    )

    route_id = str(route_data["route_id"])
    province = route_data.get("province")
    if province is None or not str(province).strip():
        raise WriterValidationError(
            "route_data['province'] is required (Skill 11 §7: no default fallback)"
        )

    # Parameter order must match _build_upsert_sql:
    #   route_id, <shape_param>, stop_ids, province, *audit_params, *optionals
    params: list[Any] = [
        route_id, shape_param, stop_ids, str(province).strip().lower()
    ]
    params.extend(audit_params)
    for col in optionals:
        val = route_data[col]
        if col == "direction_semantics" and not isinstance(val, (str, bytes)):
            val = json.dumps(val)
        params.append(val)

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.rowcount
    except Exception as exc:  # pragma: no cover — caller owns transaction
        return WriteResult(
            success=False,
            route_code=route_code,
            version=version,
            rows_affected={},
            warnings=warnings,
            error=f"{type(exc).__name__}: {exc}",
        )

    return WriteResult(
        success=True,
        route_code=route_code,
        version=version,
        rows_affected={"route_prod.routes": rows},
        warnings=warnings,
        error=None,
    )


# ---------------------------------------------------------------------------
# Partial-attribute patch (targeted UPDATE, not a full row write)
# ---------------------------------------------------------------------------

_PATCHABLE_COLUMNS: frozenset[str] = frozenset({
    "service_route_id",
    "direction_id",
    "route_name",
    "route_aliases",
    "landmark_tags",
    "direction_semantics",
    "naming_confidence",
    "human_verified",
    "deploy_status",
    "semantics_updated_at",
    "canonical_sequence_ready",
    "sequence_approved_at",
    "sequence_approved_by",
    "chosen_geometry_candidate_id",
    "chosen_stop_sequence_candidate_id",
    # Cached path-inference result (Valhalla / OSM relation polyline)
    "inferred_path_polyline",
    "inferred_path_source",
    "inferred_path_computed_at",
})


def patch_route_prod_fields(
    *,
    conn,
    route_id: str,
    fields: dict[str, Any],
    source_type: str,
    pipeline_version: str,
) -> WriteResult:
    """Targeted UPDATE of allow-listed attribute columns on route_prod.routes.

    For partial writes (e.g. binding a route to a service_route/direction) that
    must not touch geom / stop_node_ids / province. Unknown or write-protected
    columns are rejected. Rows that do not exist are silently no-ops
    (rowcount=0) — same behavior as the direct UPDATE it replaces.
    """
    if not route_id:
        raise WriterValidationError("route_id is required")
    if not isinstance(fields, dict) or not fields:
        raise WriterValidationError("fields must be a non-empty dict")
    if not source_type or not pipeline_version:
        raise WriterValidationError("source_type and pipeline_version are required")

    unknown = [c for c in fields if c not in _PATCHABLE_COLUMNS]
    if unknown:
        raise WriterValidationError(
            f"columns not allowed via patch_route_prod_fields: {unknown}. "
            f"Allowed: {sorted(_PATCHABLE_COLUMNS)}"
        )

    if conn is None:
        raise WriterValidationError("conn= keyword argument is required")

    cols = list(fields.keys())
    set_clause = ", ".join(f"{c} = %s" for c in cols) + ", updated_at = now()"
    sql = f"UPDATE route_prod.routes SET {set_clause} WHERE route_id = %s::uuid"
    params: list[Any] = []
    for c in cols:
        val = fields[c]
        if c == "direction_semantics" and not isinstance(val, (str, bytes, type(None))):
            val = json.dumps(val)
        params.append(val)
    params.append(str(route_id))

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.rowcount
    except Exception as exc:  # pragma: no cover — caller owns transaction
        return WriteResult(
            success=False,
            route_code=str(route_id),
            version=1,
            rows_affected={},
            warnings=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    return WriteResult(
        success=True,
        route_code=str(route_id),
        version=1,
        rows_affected={"route_prod.routes": rows},
        warnings=[],
        error=None,
    )


# ---------------------------------------------------------------------------
# Targeted DELETE (sole delete path for route_prod.routes)
# ---------------------------------------------------------------------------

def delete_route_prod(
    *,
    conn,
    route_id: str,
    source_type: str,
    pipeline_version: str,
    reason: str,
) -> WriteResult:
    """Sole code path that deletes from route_prod.routes.

    Mirrors the write wrapper's audit contract: source_type + pipeline_version
    are mandatory, and `reason` is a free-text audit string logged alongside
    the delete. Absent rows are a silent no-op (rowcount=0, success=True) —
    same shape as the INSERT/UPDATE wrappers.
    """
    if not route_id:
        raise WriterValidationError("route_id is required")
    if not source_type or not isinstance(source_type, str):
        raise WriterValidationError("source_type is required (non-negotiable audit field)")
    if not pipeline_version or not isinstance(pipeline_version, str):
        raise WriterValidationError("pipeline_version is required (non-negotiable audit field)")
    if not reason or not isinstance(reason, str):
        raise WriterValidationError("reason is required (audit string)")
    if conn is None:
        raise WriterValidationError("conn= keyword argument is required")

    logger.info(
        "delete_route_prod: route_id=%s source_type=%s pipeline_version=%s reason=%s",
        route_id, source_type, pipeline_version, reason,
    )

    sql = "DELETE FROM route_prod.routes WHERE route_id = %s::uuid"
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [str(route_id)])
            rows = cur.rowcount
    except Exception as exc:  # pragma: no cover — caller owns transaction
        return WriteResult(
            success=False,
            route_code=str(route_id),
            version=1,
            rows_affected={},
            warnings=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    return WriteResult(
        success=True,
        route_code=str(route_id),
        version=1,
        rows_affected={"route_prod.routes": rows},
        warnings=[],
        error=None,
    )


# ---------------------------------------------------------------------------
# Bulk operations (cascade cleanups)
# ---------------------------------------------------------------------------

def _rows_to_codes(rows) -> list[str]:
    """Normalise psycopg fetchall output (tuple vs dict cursors) to list[str]."""
    out: list[str] = []
    for r in rows or ():
        if isinstance(r, (tuple, list)):
            out.append(str(r[0]))
        elif isinstance(r, dict):
            out.append(str(r.get("route_id")))
        else:
            out.append(str(r))
    return out


def prune_stop_node_id_from_routes(
    *,
    conn,
    stop_node_id,
    reason: str,
    pipeline_version: str,
    source_component: str,
) -> BulkWriteResult:
    """Remove a single stop_node_id from stop_node_ids of every route that
    currently references it (node-cascade cleanup).

    Single SQL statement → single transaction, all-or-nothing. Returns a
    BulkWriteResult whose `affected_route_codes` is the list of route_id
    strings that were actually mutated (via RETURNING).
    """
    if not stop_node_id:
        raise WriterValidationError("stop_node_id is required")
    if not reason or not isinstance(reason, str):
        raise WriterValidationError("reason is required (audit string)")
    if not pipeline_version or not isinstance(pipeline_version, str):
        raise WriterValidationError("pipeline_version is required (non-negotiable audit field)")
    if not source_component or not isinstance(source_component, str):
        raise WriterValidationError("source_component is required (non-negotiable audit field)")
    if conn is None:
        raise WriterValidationError("conn= keyword argument is required")

    sid = str(stop_node_id)
    logger.info(
        "prune_stop_node_id_from_routes: stop_node_id=%s source=%s version=%s reason=%s",
        sid, source_component, pipeline_version, reason,
    )

    sql = (
        "UPDATE route_prod.routes "
        "SET stop_node_ids = array_remove("
        "COALESCE(stop_node_ids, ARRAY[]::uuid[]), %s::uuid"
        "), updated_at = now() "
        "WHERE %s::uuid = ANY(COALESCE(stop_node_ids, ARRAY[]::uuid[])) "
        "RETURNING route_id::text"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [sid, sid])
            affected = _rows_to_codes(cur.fetchall())
    except Exception as exc:  # pragma: no cover — caller owns transaction
        return BulkWriteResult(
            success=False,
            operation="prune_stop_node_id_from_routes",
            error=f"{type(exc).__name__}: {exc}",
        )

    for code in affected:
        logger.info("  pruned stop %s from route %s", sid, code)

    return BulkWriteResult(
        success=True,
        operation="prune_stop_node_id_from_routes",
        rows_affected={"route_prod.routes": len(affected)},
        affected_route_codes=affected,
    )


def delete_routes_prod_bulk(
    *,
    conn,
    route_ids: list,
    reason: str,
    pipeline_version: str,
    source_component: str,
) -> BulkWriteResult:
    """Delete many routes from route_prod.routes in one transaction.

    Missing route_ids are reported via warnings but do not fail the call —
    the DELETE processes every id in the list and succeeds for the ones
    that exist (RETURNING tells us which). Empty `route_ids` is a
    no-op with a warning.
    """
    if not reason or not isinstance(reason, str):
        raise WriterValidationError("reason is required (audit string)")
    if not pipeline_version or not isinstance(pipeline_version, str):
        raise WriterValidationError("pipeline_version is required (non-negotiable audit field)")
    if not source_component or not isinstance(source_component, str):
        raise WriterValidationError("source_component is required (non-negotiable audit field)")
    if conn is None:
        raise WriterValidationError("conn= keyword argument is required")

    if not route_ids:
        msg = "delete_routes_prod_bulk: empty route_ids list — no-op"
        logger.warning(msg)
        return BulkWriteResult(
            success=True,
            operation="delete_routes_prod_bulk",
            rows_affected={"route_prod.routes": 0},
            warnings=[msg],
        )

    requested = [str(r) for r in route_ids]
    logger.info(
        "delete_routes_prod_bulk: count=%d source=%s version=%s reason=%s",
        len(requested), source_component, pipeline_version, reason,
    )

    sql = (
        "DELETE FROM route_prod.routes "
        "WHERE route_id = ANY(%s::uuid[]) "
        "RETURNING route_id::text"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql, [requested])
            affected = _rows_to_codes(cur.fetchall())
    except Exception as exc:  # pragma: no cover — caller owns transaction
        return BulkWriteResult(
            success=False,
            operation="delete_routes_prod_bulk",
            error=f"{type(exc).__name__}: {exc}",
        )

    missing = sorted(set(requested) - set(affected))
    warnings = [
        f"route_id not present in route_prod.routes: {m}" for m in missing
    ]
    for code in affected:
        logger.info("  deleted route %s", code)

    return BulkWriteResult(
        success=True,
        operation="delete_routes_prod_bulk",
        rows_affected={"route_prod.routes": len(affected)},
        affected_route_codes=affected,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Stored-procedure implementation
# ---------------------------------------------------------------------------

def _write_via_stored_procedure(
    *,
    conn,
    route_code: str,
    route_data: dict,
    stops: list,
    shape: dict,
    db_source_type: str,
    pipeline_version: str,
    valhalla_request: dict,
    geometry_enforcer_report: dict,
    stop_coverage_report: dict,
    quality_gate_passed_at: datetime,
    approved_by_user: Optional[str],
    approved_at: Optional[datetime],
    version: int,
    warnings: list[str],
) -> WriteResult:
    """Call ``SELECT route_prod.route_prod_writer_sp(...)``.

    The SP is ``SECURITY DEFINER`` so it runs with the table owner's rights
    even when the caller connects as ``route_prod_writer`` (which has no
    direct INSERT/UPDATE grants post-REVOKE). Only fields accepted by the
    SP's 15-parameter signature are passed; Phase-4 naming/metadata fields
    in ``route_data`` (``deploy_status``, ``direction_semantics``, etc.)
    are intentionally NOT passed here — callers that need them must follow
    up with ``patch_route_prod_fields(...)``.
    """
    if conn is None:
        raise WriterValidationError(
            "conn= keyword argument is required for SP writes"
        )

    stop_ids = _normalize_stop_node_ids(stops)
    shape_sql, shape_param = _shape_to_sql_and_param(shape)

    route_id = str(route_data["route_id"])
    province = route_data.get("province")
    if province is None or not str(province).strip():
        raise WriterValidationError(
            "route_data['province'] is required (Skill 11 §7: no default fallback)"
        )

    source_provenance = route_data.get("source") or "route_constructor"
    route_name = route_data.get("route_name")

    sql = (
        "SELECT route_prod.route_prod_writer_sp("
        "%s::uuid, " + shape_sql + ", %s::uuid[], "
        "%s, %s, %s, %s, "
        "%s::jsonb, %s::jsonb, %s::jsonb, "
        "%s::timestamptz, "
        "%s::uuid, %s::timestamptz, %s, %s::smallint"
        ") AS route_id"
    )
    params: list[Any] = [
        route_id,
        shape_param,
        stop_ids,
        source_provenance,
        db_source_type,
        str(province).strip().lower(),
        pipeline_version,
        json.dumps(valhalla_request) if not isinstance(valhalla_request, str) else valhalla_request,
        json.dumps(geometry_enforcer_report) if not isinstance(geometry_enforcer_report, str) else geometry_enforcer_report,
        json.dumps(stop_coverage_report) if not isinstance(stop_coverage_report, str) else stop_coverage_report,
        quality_gate_passed_at,
        str(approved_by_user) if approved_by_user else None,
        approved_at,
        route_name,
        version,
    ]

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            _ = cur.fetchone()  # SP returns route_id; we just drain it
            rows = 1
    except Exception as exc:  # pragma: no cover — caller owns transaction
        return WriteResult(
            success=False,
            route_code=route_code,
            version=version,
            rows_affected={},
            warnings=warnings,
            error=f"{type(exc).__name__}: {exc}",
        )

    return WriteResult(
        success=True,
        route_code=route_code,
        version=version,
        rows_affected={"route_prod.routes": rows},
        warnings=warnings,
        error=None,
    )
