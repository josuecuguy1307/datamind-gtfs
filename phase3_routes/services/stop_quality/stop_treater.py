"""
Universal Stop Quality Treater — Phase 3 ownership.

Single canonical service for stop-name + stop-coord + place-mapping
treatment. REUSES the proven canonical implementations (does not duplicate
logic):

  * Forbidden-name predicate:
      datamind_console.common.naming_patterns.is_stop_name_forbidden
  * Normalizer:
      datamind_console.common.name_normalizer.normalize_name
  * Contextual cascade:
      phase2_semantics.src.pipeline.naming.contextual_name_generator
      .generate_contextual_name  (intersection→landmark→sector→ext_sector→orphan)

Public API:

    result = treat_stop(input, conn)

Operations:
    synthetic_insert  — A2 / backfill / OSM-fill (creates new node_prod.nodes row)
    name_repair       — pre-export enforcer (existing row, name only)
    snap_align        — GREEK α cleanup (existing row, geom + name)
    refill_adopt      — GREEK γ refill (existing row, validate + name)
    ground_validate   — Stage A grounding (insert OR repair)
    cover_validate    — gap fill (insert)
    phase3_end_audit  — end-of-pipeline safety net (existing row, idempotent)

Output guarantees on success:
    * final_name passes is_stop_name_forbidden() == False
    * final_name == normalize_name(final_name) (idempotent)
    * place_id is set; geo_prod.places + geo_prod.node_place_map consistent
    * audit row written into node_prod.stop_treatment_log

Transaction model:
    Operates inside the caller's transaction. Atomic with caller — if the
    caller rolls back, treater state rolls back.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID, uuid4

import psycopg2.extensions
import psycopg2.extras

# Register UUID adapter once at import time so callers don't have to.
psycopg2.extras.register_uuid()

# Force tuple-cursor reads even when caller's connection defaults to RealDictCursor.
# Code below uses row[0] / unpacking, which fails with KeyError(0) on dict cursors.
_TUPLE_CURSOR = psycopg2.extensions.cursor

from datamind_console.common.naming_patterns import is_stop_name_forbidden
from datamind_console.common.name_normalizer import normalize_name

# The phase2_semantics package __init__ pulls in unrelated modules with
# broken sibling imports; load the generator directly from its file path
# so we don't pay that cost.
import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _Path

_GEN_PATH = (
    _Path(__file__).resolve().parents[3]
    / "phase2_semantics/src/pipeline/naming/contextual_name_generator.py"
)
_spec = _ilu.spec_from_file_location("_cng_for_treater", _GEN_PATH)
_cng = _ilu.module_from_spec(_spec)
_sys.modules["_cng_for_treater"] = _cng
_spec.loader.exec_module(_cng)
generate_contextual_name = _cng.generate_contextual_name
ContextualName = _cng.ContextualName


log = logging.getLogger(__name__)

# ─── Public dataclasses ──────────────────────────────────────────────────────


@dataclass
class StopTreatmentInput:
    operation: str
    caller: str
    node_id: Optional[UUID] = None
    proposed_name: Optional[str] = None
    proposed_lat: Optional[float] = None
    proposed_lon: Optional[float] = None
    proposed_tags: Optional[dict] = None
    province: Optional[str] = None
    confidence: float = 0.7
    transaction_id: Optional[int] = None
    # node_type override for synthetic_insert; "STOP" if None.
    # Used by backfill executor to flag bus_station hits as STATION.
    node_type: Optional[str] = None
    # Optional pass-through of synthesis-specific node_prod.nodes columns.
    # Only keys in SYNTHETIC_EXTRAS_WHITELIST are honored; anything else is
    # silently dropped to keep the treater contract narrow.
    extras: Optional[dict] = None


# Columns the treater is willing to set on synthetic_insert beyond its
# core (node_id, geom, node_type, name, confidence, province). Whitelisted
# explicitly to avoid arbitrary column writes through caller-controlled dicts.
SYNTHETIC_EXTRAS_WHITELIST = frozenset({
    "osm_id",
    "source",
    "source_type",
    "synthetic_confidence",
    "synthetic_created_at",
    "synthetic_created_by",
    "synthetic_review_state",
    "chosen_tags",
    "poi_anchor_osm_id",
    "poi_anchor_class",
    "poi_anchor_access_point",
    "poi_to_path_distance_m",
    "path_projection_distance_m",
    "research_to_projection_distance_m",
    "semantic_spatial_conflict",
    "osm_route_fill_context",
    # Operator-approval fields used by approve_promote (phase1 admin paths).
    "ref",
    "operator",
    "tag_kind",
    "source_node_set_id",
    "chosen_candidate_id",
    "approved_at",
})


@dataclass
class TreatmentResult:
    success: bool
    treatment_id: str
    node_id: Optional[UUID]
    final_name: Optional[str]
    final_lat: Optional[float]
    final_lon: Optional[float]
    place_id: Optional[UUID]
    name_was_forbidden_input: bool
    name_normalized: bool
    context_name_applied: bool
    context_name_method: Optional[str]
    place_mapping_created: bool
    place_mapping_validated: bool
    error: Optional[str] = None


# ─── Public API ──────────────────────────────────────────────────────────────


SUPPORTED_OPERATIONS = frozenset({
    "synthetic_insert",
    "name_repair",
    "snap_align",
    "refill_adopt",
    "ground_validate",
    "cover_validate",
    "phase3_end_audit",
    "approve_promote",
})


def treat_stop(input: StopTreatmentInput, conn) -> TreatmentResult:
    """
    Universal stop quality treatment.

    Cascade:
      1. validate input
      2. normalize the proposed name
      3. forbidden check
      4. context-naming cascade (only if forbidden/empty)
      5. operation dispatch
      6. ensure place mapping (geo_prod.places + node_place_map)
      7. consistency check
      8. audit log
    """
    treatment_id = str(uuid4())

    err = _validate_input(input)
    if err:
        return _audit_failure(conn, treatment_id, input, f"invalid input: {err}")

    raw_name = (input.proposed_name or "").strip()
    normalized = (normalize_name(raw_name) or "").strip() if raw_name else ""
    name_normalized_flag = bool(normalized) and normalized != raw_name

    name_forbidden = (not normalized) or is_stop_name_forbidden(normalized)

    final_name = normalized
    context_name_applied = False
    context_name_method: Optional[str] = None

    if name_forbidden:
        from datamind_console.common.extended_stop_naming import (
            compute_extended_stop_name,
            ExtendedNamingError,
        )
        try:
            ext = compute_extended_stop_name(
                conn,
                lat=input.proposed_lat,
                lon=input.proposed_lon,
                original_name=raw_name or "",
                node_id=input.node_id,
            )
            final_name = ext.new_name
            context_name_applied = True
            context_name_method = ext.tier
        except ExtendedNamingError as exc:
            return _audit_failure(
                conn, treatment_id, input,
                f"extended naming exhausted: {exc}",
                name_was_forbidden=name_forbidden,
                name_normalized=name_normalized_flag,
                context_name_applied=False,
                context_name_method=None,
            )

    if is_stop_name_forbidden(final_name):
        return _audit_failure(
            conn, treatment_id, input,
            f"final_name still forbidden after cascade: {final_name!r}",
            name_was_forbidden=name_forbidden,
            name_normalized=name_normalized_flag,
            context_name_applied=context_name_applied,
            context_name_method=context_name_method,
        )

    try:
        node_id, lat, lon = _dispatch(input, final_name, conn)
    except Exception as exc:
        return _audit_failure(
            conn, treatment_id, input, f"operation failed: {exc!r}",
            name_was_forbidden=name_forbidden,
            name_normalized=name_normalized_flag,
            context_name_applied=context_name_applied,
            context_name_method=context_name_method,
        )

    try:
        place_id, place_created, place_validated = _ensure_place_mapping(
            node_id, final_name, lat, lon, conn,
        )
    except Exception as exc:
        return _audit_failure(
            conn, treatment_id, input, f"place mapping failed: {exc!r}",
            node_id=node_id,
            final_name=final_name, final_lat=lat, final_lon=lon,
            name_was_forbidden=name_forbidden,
            name_normalized=name_normalized_flag,
            context_name_applied=context_name_applied,
            context_name_method=context_name_method,
        )

    consistency_err = _validate_consistency(node_id, place_id, conn)
    if consistency_err:
        return _audit_failure(
            conn, treatment_id, input, f"consistency check failed: {consistency_err}",
            node_id=node_id,
            final_name=final_name, final_lat=lat, final_lon=lon,
            name_was_forbidden=name_forbidden,
            name_normalized=name_normalized_flag,
            context_name_applied=context_name_applied,
            context_name_method=context_name_method,
            place_id=place_id,
            place_mapping_created=place_created,
            place_mapping_validated=place_validated,
        )

    _write_audit(
        conn=conn, treatment_id=treatment_id, input=input,
        node_id=node_id, final_name=final_name, final_lat=lat, final_lon=lon,
        name_was_forbidden=name_forbidden,
        name_normalized=name_normalized_flag,
        context_name_applied=context_name_applied,
        context_name_method=context_name_method,
        place_id=place_id, place_mapping_created=place_created,
        place_mapping_validated=place_validated,
        success=True, error=None,
    )

    return TreatmentResult(
        success=True,
        treatment_id=treatment_id,
        node_id=node_id,
        final_name=final_name, final_lat=lat, final_lon=lon,
        place_id=place_id,
        name_was_forbidden_input=name_forbidden,
        name_normalized=name_normalized_flag,
        context_name_applied=context_name_applied,
        context_name_method=context_name_method,
        place_mapping_created=place_created,
        place_mapping_validated=place_validated,
        error=None,
    )


# ─── Validation ──────────────────────────────────────────────────────────────


def _validate_input(inp: StopTreatmentInput) -> Optional[str]:
    if inp.operation not in SUPPORTED_OPERATIONS:
        return f"unsupported operation: {inp.operation!r}"
    if not inp.caller:
        return "caller is required"

    if inp.operation == "synthetic_insert":
        if inp.node_id is not None:
            return "synthetic_insert must NOT carry an existing node_id"
        if inp.proposed_lat is None or inp.proposed_lon is None:
            return "synthetic_insert requires lat + lon"
    elif inp.operation in ("name_repair", "refill_adopt", "phase3_end_audit"):
        if inp.node_id is None:
            return f"{inp.operation} requires node_id"
    elif inp.operation == "snap_align":
        if inp.node_id is None:
            return "snap_align requires node_id"
        if inp.proposed_lat is None or inp.proposed_lon is None:
            return "snap_align requires lat + lon (it updates geom)"
    elif inp.operation in ("ground_validate", "cover_validate"):
        if inp.proposed_lat is None or inp.proposed_lon is None:
            return f"{inp.operation} requires lat + lon"
    elif inp.operation == "approve_promote":
        # Operator-approved insert-or-update. node_id is OPTIONAL — when
        # absent a fresh uuid is generated; when present, UPSERT semantics.
        if inp.proposed_lat is None or inp.proposed_lon is None:
            return "approve_promote requires lat + lon"
    return None


# ─── Context naming wrapper ──────────────────────────────────────────────────


def _try_context_naming(*, conn, place_id_str: str, original_name: str,
                        lat: Optional[float], lon: Optional[float]) -> Optional[Any]:
    if lat is None or lon is None:
        return None
    try:
        return generate_contextual_name(
            conn=conn,
            place_id=place_id_str,
            original_name=original_name,
            lat=float(lat),
            lon=float(lon),
            category="GARBAGE",
        )
    except Exception as exc:
        log.warning("contextual cascade failed: %r", exc)
        return None


# ─── Operation dispatch ──────────────────────────────────────────────────────


def _dispatch(inp: StopTreatmentInput, final_name: str, conn):
    op = inp.operation
    if op == "synthetic_insert":
        return _op_synthetic_insert(inp, final_name, conn)
    if op == "name_repair":
        return _op_name_repair(inp, final_name, conn)
    if op == "snap_align":
        return _op_snap_align(inp, final_name, conn)
    if op == "refill_adopt":
        return _op_refill_adopt(inp, final_name, conn)
    if op == "ground_validate":
        return _op_ground_validate(inp, final_name, conn)
    if op == "cover_validate":
        # Covering = a stop is missing on a route; always insert.
        return _op_synthetic_insert(inp, final_name, conn)
    if op == "phase3_end_audit":
        return _op_phase3_end_audit(inp, final_name, conn)
    if op == "approve_promote":
        return _op_approve_promote(inp, final_name, conn)
    raise ValueError(f"unhandled operation: {op}")


def _op_synthetic_insert(inp: StopTreatmentInput, final_name: str, conn):
    """INSERT a new node_prod.nodes row.

    Uses the schema's NOT NULL DEFAULTs for tag_kind, source, chosen_tags,
    confidence, approved_at, updated_at. `province` MUST be passed when
    not Sample Region (the column has a Sample Region default).

    `inp.extras` (whitelist-filtered against SYNTHETIC_EXTRAS_WHITELIST) is
    written into matching columns. Unknown keys are silently dropped.
    """
    new_id = uuid4()
    cols = ["node_id", "geom", "node_type", "name", "confidence"]
    placeholders = [
        "%s",
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326)",
        "%s", "%s", "%s",
    ]
    vals: list[Any] = [
        new_id, inp.proposed_lon, inp.proposed_lat,
        inp.node_type or "STOP", final_name, inp.confidence,
    ]
    if inp.province:
        cols.append("province")
        placeholders.append("%s")
        vals.append(inp.province)

    if inp.extras:
        for key, value in inp.extras.items():
            if key not in SYNTHETIC_EXTRAS_WHITELIST:
                log.debug("dropping non-whitelisted extras key: %r", key)
                continue
            cols.append(key)
            placeholders.append("%s")
            vals.append(value)

    sql = f"INSERT INTO node_prod.nodes ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
    with conn.cursor() as cur:
        cur.execute(sql, vals)
    return new_id, inp.proposed_lat, inp.proposed_lon


def _op_approve_promote(inp: StopTreatmentInput, final_name: str, conn):
    """Operator-approved insert-or-update (UPSERT by node_id).

    Used by the phase1 admin paths (approve_resolved_node,
    approve_node_review_request, create_prod_node_manual). When
    `inp.node_id` is None a fresh uuid is generated; when present,
    `INSERT ... ON CONFLICT (node_id) DO UPDATE` semantics apply.

    Operator-supplied fields (ref, operator, tag_kind,
    source_node_set_id, chosen_candidate_id, approved_at, source) are
    accepted via `inp.extras` (whitelist-filtered).
    """
    node_id = inp.node_id if inp.node_id is not None else uuid4()
    cols = ["node_id", "geom", "node_type", "name", "confidence"]
    placeholders = [
        "%s",
        "ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326)",
        "%s", "%s", "%s",
    ]
    vals: list[Any] = [
        node_id, inp.proposed_lon, inp.proposed_lat,
        inp.node_type or "STOP", final_name, inp.confidence,
    ]
    if inp.province:
        cols.append("province")
        placeholders.append("%s")
        vals.append(inp.province)

    if inp.extras:
        for key, value in inp.extras.items():
            if key not in SYNTHETIC_EXTRAS_WHITELIST:
                log.debug("dropping non-whitelisted extras key: %r", key)
                continue
            cols.append(key)
            placeholders.append("%s")
            vals.append(value)

    update_cols = [c for c in cols if c != "node_id"]
    update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
    update_clause += ", updated_at = NOW()"

    sql = (
        f"INSERT INTO node_prod.nodes ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (node_id) DO UPDATE SET {update_clause}"
    )
    with conn.cursor() as cur:
        cur.execute(sql, vals)
    return node_id, inp.proposed_lat, inp.proposed_lon


def _op_name_repair(inp: StopTreatmentInput, final_name: str, conn):
    with conn.cursor(cursor_factory=_TUPLE_CURSOR) as cur:
        cur.execute(
            "UPDATE node_prod.nodes SET name = %s, updated_at = NOW() WHERE node_id = %s",
            (final_name, inp.node_id),
        )
        cur.execute(
            "SELECT ST_Y(geom::geometry), ST_X(geom::geometry) "
            "FROM node_prod.nodes WHERE node_id = %s",
            (inp.node_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"node not found: {inp.node_id}")
        return inp.node_id, float(row[0]), float(row[1])


def _op_snap_align(inp: StopTreatmentInput, final_name: str, conn):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE node_prod.nodes "
            "   SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), "
            "       name = %s, updated_at = NOW() "
            " WHERE node_id = %s",
            (inp.proposed_lon, inp.proposed_lat, final_name, inp.node_id),
        )
    return inp.node_id, inp.proposed_lat, inp.proposed_lon


def _op_refill_adopt(inp: StopTreatmentInput, final_name: str, conn):
    with conn.cursor(cursor_factory=_TUPLE_CURSOR) as cur:
        cur.execute(
            "SELECT name, ST_Y(geom::geometry), ST_X(geom::geometry) "
            "FROM node_prod.nodes WHERE node_id = %s",
            (inp.node_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"refill target not found: {inp.node_id}")
        current_name, lat, lon = row
        if current_name != final_name:
            cur.execute(
                "UPDATE node_prod.nodes SET name = %s, updated_at = NOW() WHERE node_id = %s",
                (final_name, inp.node_id),
            )
    return inp.node_id, float(lat), float(lon)


def _op_ground_validate(inp: StopTreatmentInput, final_name: str, conn):
    if inp.node_id is not None:
        return _op_name_repair(inp, final_name, conn)
    return _op_synthetic_insert(inp, final_name, conn)


def _op_phase3_end_audit(inp: StopTreatmentInput, final_name: str, conn):
    with conn.cursor(cursor_factory=_TUPLE_CURSOR) as cur:
        cur.execute(
            "SELECT name, ST_Y(geom::geometry), ST_X(geom::geometry) "
            "FROM node_prod.nodes WHERE node_id = %s",
            (inp.node_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"audit target not found: {inp.node_id}")
        current_name, lat, lon = row
        if current_name != final_name:
            cur.execute(
                "UPDATE node_prod.nodes SET name = %s, updated_at = NOW() WHERE node_id = %s",
                (final_name, inp.node_id),
            )
    return inp.node_id, float(lat), float(lon)


# ─── Place mapping ───────────────────────────────────────────────────────────


def _ensure_place_mapping(node_id, name, lat, lon, conn):
    """
    Ensure (node_id → place_id) mapping exists in geo_prod.node_place_map and
    references an active geo_prod.places row.

    Returns (place_id, was_created, was_validated).
    """
    with conn.cursor(cursor_factory=_TUPLE_CURSOR) as cur:
        cur.execute(
            "SELECT npm.place_id, p.canonical_name, p.status "
            "FROM geo_prod.node_place_map npm "
            "LEFT JOIN geo_prod.places p ON p.place_id = npm.place_id "
            "WHERE npm.node_id = %s",
            (node_id,),
        )
        row = cur.fetchone()

        if row and row[2] == "active":
            return row[0], False, True

        # Look for an existing active place at this name within 50 m.
        cur.execute(
            "SELECT place_id FROM geo_prod.v_active_places "
            "WHERE canonical_name = %s "
            "  AND ST_DWithin( "
            "        geom::geography, "
            "        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, "
            "        50) "
            "ORDER BY ST_Distance( "
            "        geom::geography, "
            "        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) "
            "LIMIT 1",
            (name, lon, lat, lon, lat),
        )
        match = cur.fetchone()

        if match:
            place_id = match[0]
            created = False
        else:
            place_id = uuid4()
            cur.execute(
                "INSERT INTO geo_prod.places (place_id, canonical_name, place_type, geom, status) "
                "VALUES (%s, %s, 'STOP', "
                "  ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geometry(Point,4326), 'active')",
                (place_id, name, lon, lat),
            )
            created = True

        cur.execute(
            "INSERT INTO geo_prod.node_place_map (node_id, place_id, mapping_source) "
            "VALUES (%s, %s, 'phase2_auto') "
            "ON CONFLICT (node_id) DO UPDATE "
            "  SET place_id = EXCLUDED.place_id, "
            "      mapping_source = EXCLUDED.mapping_source, "
            "      updated_at = NOW()",
            (node_id, place_id),
        )
    return place_id, created, True


def _validate_consistency(node_id, place_id, conn) -> Optional[str]:
    with conn.cursor(cursor_factory=_TUPLE_CURSOR) as cur:
        cur.execute(
            "SELECT "
            "  (SELECT COUNT(*) FROM node_prod.nodes WHERE node_id = %s), "
            "  (SELECT COUNT(*) FROM geo_prod.places WHERE place_id = %s AND status = 'active'), "
            "  (SELECT COUNT(*) FROM geo_prod.node_place_map WHERE node_id = %s AND place_id = %s)",
            (node_id, place_id, node_id, place_id),
        )
        n_nodes, n_active, n_map = cur.fetchone()
    if n_nodes != 1:
        return f"node_prod.nodes count = {n_nodes} (expected 1)"
    if n_active != 1:
        return f"geo_prod.places (active) count = {n_active} (expected 1)"
    if n_map != 1:
        return f"node_place_map count = {n_map} (expected 1)"
    return None


# ─── Audit log ───────────────────────────────────────────────────────────────


def _write_audit(*, conn, treatment_id, input, node_id,
                 final_name, final_lat, final_lon,
                 name_was_forbidden, name_normalized,
                 context_name_applied, context_name_method,
                 place_id, place_mapping_created, place_mapping_validated,
                 success, error):
    coord_changed = (
        input.proposed_lat is not None and input.proposed_lon is not None
        and (input.proposed_lat != final_lat or input.proposed_lon != final_lon)
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.stop_treatment_log ("
            "  treatment_id, node_id, operation, caller, "
            "  name_before, name_after, name_was_forbidden, name_normalized, "
            "  context_name_applied, context_name_method, "
            "  coord_before_lat, coord_before_lon, coord_after_lat, coord_after_lon, "
            "  coord_changed, place_id, place_mapping_created, place_mapping_validated, "
            "  success, error_reason, transaction_id"
            ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                treatment_id, node_id, input.operation, input.caller,
                input.proposed_name, final_name,
                name_was_forbidden, name_normalized,
                context_name_applied, context_name_method,
                input.proposed_lat, input.proposed_lon, final_lat, final_lon,
                coord_changed, place_id, place_mapping_created, place_mapping_validated,
                success, error, input.transaction_id,
            ),
        )


def _audit_failure(conn, treatment_id, input, error, **kw) -> TreatmentResult:
    log.error("treatment %s failed: %s", treatment_id, error)
    try:
        _write_audit(
            conn=conn, treatment_id=treatment_id, input=input,
            node_id=kw.get("node_id"),
            final_name=kw.get("final_name"),
            final_lat=kw.get("final_lat"),
            final_lon=kw.get("final_lon"),
            name_was_forbidden=kw.get("name_was_forbidden", False),
            name_normalized=kw.get("name_normalized", False),
            context_name_applied=kw.get("context_name_applied", False),
            context_name_method=kw.get("context_name_method"),
            place_id=kw.get("place_id"),
            place_mapping_created=kw.get("place_mapping_created", False),
            place_mapping_validated=kw.get("place_mapping_validated", False),
            success=False, error=error,
        )
    except Exception as exc:
        log.exception("audit-of-failure write failed: %r", exc)
    return TreatmentResult(
        success=False,
        treatment_id=treatment_id,
        node_id=kw.get("node_id") or input.node_id,
        final_name=kw.get("final_name"),
        final_lat=kw.get("final_lat"),
        final_lon=kw.get("final_lon"),
        place_id=kw.get("place_id"),
        name_was_forbidden_input=kw.get("name_was_forbidden", False),
        name_normalized=kw.get("name_normalized", False),
        context_name_applied=kw.get("context_name_applied", False),
        context_name_method=kw.get("context_name_method"),
        place_mapping_created=kw.get("place_mapping_created", False),
        place_mapping_validated=kw.get("place_mapping_validated", False),
        error=error,
    )
