from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
import math
import uuid


# ---------------------------------------------------------------------------
# Constructor status lifecycle
# ---------------------------------------------------------------------------
CONSTRUCTOR_STATUSES = (
    "draft_saved",
    "draft_ready",
    "exported_sequence_ready",
    "awaiting_sequence_approval",
    "awaiting_geometry",
    "awaiting_review",
    "blocked_missing_evidence",
    "completed",
    "rejected",
)

PREFLIGHT_CLASSIFICATIONS = (
    "true_missing_ready",
    "likely_already_represented",
    "duplicate_risk",
    "needs_cleanup_first",
    "missing_evidence",
    "operator_review_required",
)

HINT_PROVENANCE_SOURCES = ("manual", "gap_default", "llm_suggestion", "catalog_inferred")


def _as_uuid_str(value: Any, *, field: str, allow_none: bool = True) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        if allow_none:
            return None
        raise ValueError(f"{field} is required")
    try:
        return str(uuid.UUID(raw))
    except Exception as exc:
        raise ValueError(f"{field} must be a valid UUID: {raw}") from exc


def _clean_text(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    return raw or None


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return float(2.0 * r * math.asin(math.sqrt(a)))


@dataclass(slots=True)
class ManualSequenceExportRequest:
    ordered_stop_ids: list[str]
    route_job_id: Optional[str] = None
    service_route_id: Optional[str] = None
    direction_id: Optional[int] = None
    coverage_gap_id: Optional[str] = None
    ordered_node_ids: Optional[list[str]] = None
    ordered_coords: Optional[list[list[float]]] = None
    is_loop: bool = False
    name_hint: Optional[str] = None
    operator_hint: Optional[str] = None
    variant_hint: Optional[str] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    source: str = "manual_builder"
    # --- Enhanced fields for constructor orchestrator ---
    constructor_status: str = "draft_saved"
    preflight_classification: Optional[str] = None
    hint_provenance: Optional[dict] = None
    draft_id: Optional[str] = None
    notes: Optional[str] = None
    sector_key: Optional[str] = None
    sector_label: Optional[str] = None
    # --- Sequence discovery fields ---
    anchor_stop_ids: Optional[list[str]] = None
    intermediate_stop_hints: Optional[list[dict]] = None
    corridor_hints: Optional[list[str]] = None
    locality_clues: Optional[list[str]] = None
    must_pass_through: Optional[list[str]] = None
    sequence_notes: Optional[str] = None
    sequence_confidence: Optional[str] = None
    llm_sequence_suggestion: Optional[dict] = None


@dataclass(slots=True)
class ManualSequenceValidation:
    errors: list[str]
    warnings: list[str]
    ordered_stops: list[dict[str, Any]]


def normalize_manual_sequence_export_request(payload: Any) -> ManualSequenceExportRequest:
    if isinstance(payload, ManualSequenceExportRequest):
        req = payload
    elif isinstance(payload, Mapping):
        req = ManualSequenceExportRequest(
            ordered_stop_ids=list(payload.get("ordered_stop_ids") or []),
            route_job_id=payload.get("route_job_id"),
            service_route_id=payload.get("service_route_id"),
            direction_id=payload.get("direction_id"),
            coverage_gap_id=payload.get("coverage_gap_id"),
            ordered_node_ids=list(payload.get("ordered_node_ids") or []),
            ordered_coords=list(payload.get("ordered_coords") or []),
            is_loop=bool(payload.get("is_loop")),
            name_hint=payload.get("name_hint"),
            operator_hint=payload.get("operator_hint"),
            variant_hint=payload.get("variant_hint"),
            created_by=payload.get("created_by"),
            created_at=payload.get("created_at"),
            source=str(payload.get("source") or "manual_builder"),
            constructor_status=str(payload.get("constructor_status") or "draft_saved"),
            preflight_classification=payload.get("preflight_classification"),
            hint_provenance=dict(payload.get("hint_provenance") or {}),
            draft_id=payload.get("draft_id"),
            notes=payload.get("notes"),
            sector_key=payload.get("sector_key"),
            sector_label=payload.get("sector_label"),
            anchor_stop_ids=list(payload.get("anchor_stop_ids") or []) or None,
            intermediate_stop_hints=list(payload.get("intermediate_stop_hints") or []) or None,
            corridor_hints=list(payload.get("corridor_hints") or []) or None,
            locality_clues=list(payload.get("locality_clues") or []) or None,
            must_pass_through=list(payload.get("must_pass_through") or []) or None,
            sequence_notes=payload.get("sequence_notes"),
            sequence_confidence=payload.get("sequence_confidence"),
            llm_sequence_suggestion=dict(payload.get("llm_sequence_suggestion") or {}) or None,
        )
    else:
        raise ValueError("payload must be a dict or ManualSequenceExportRequest")

    ordered_stop_ids: list[str] = []
    for i, raw in enumerate(req.ordered_stop_ids or []):
        ordered_stop_ids.append(_as_uuid_str(raw, field=f"ordered_stop_ids[{i}]", allow_none=False) or "")

    ordered_node_ids: list[str] = []
    raw_nodes = req.ordered_node_ids if req.ordered_node_ids else ordered_stop_ids
    for i, raw in enumerate(raw_nodes):
        ordered_node_ids.append(_as_uuid_str(raw, field=f"ordered_node_ids[{i}]", allow_none=False) or "")

    route_job_id = _as_uuid_str(req.route_job_id, field="route_job_id", allow_none=True)
    service_route_id = _as_uuid_str(req.service_route_id, field="service_route_id", allow_none=True)
    coverage_gap_id = _as_uuid_str(req.coverage_gap_id, field="coverage_gap_id", allow_none=True)

    direction_id: Optional[int]
    if req.direction_id is None or str(req.direction_id).strip() == "":
        direction_id = None
    else:
        direction_id = int(req.direction_id)
        if direction_id not in (0, 1):
            raise ValueError("direction_id must be 0 or 1 when provided")

    source = str(req.source or "manual_builder").strip().lower()
    if source != "manual_builder":
        raise ValueError("source must be 'manual_builder'")

    constructor_status = str(req.constructor_status or "draft_saved").strip()
    if constructor_status not in CONSTRUCTOR_STATUSES:
        constructor_status = "draft_saved"

    preflight_classification = _clean_text(req.preflight_classification)
    if preflight_classification and preflight_classification not in PREFLIGHT_CLASSIFICATIONS:
        preflight_classification = None

    return ManualSequenceExportRequest(
        ordered_stop_ids=ordered_stop_ids,
        route_job_id=route_job_id,
        service_route_id=service_route_id,
        direction_id=direction_id,
        coverage_gap_id=coverage_gap_id,
        ordered_node_ids=ordered_node_ids,
        ordered_coords=list(req.ordered_coords or []),
        is_loop=bool(req.is_loop),
        name_hint=_clean_text(req.name_hint),
        operator_hint=_clean_text(req.operator_hint),
        variant_hint=_clean_text(req.variant_hint),
        created_by=_clean_text(req.created_by),
        created_at=_clean_text(req.created_at),
        source="manual_builder",
        constructor_status=constructor_status,
        preflight_classification=preflight_classification,
        hint_provenance=dict(req.hint_provenance or {}),
        draft_id=_as_uuid_str(req.draft_id, field="draft_id", allow_none=True),
        notes=_clean_text(req.notes),
        sector_key=_clean_text(req.sector_key),
        sector_label=_clean_text(req.sector_label),
        anchor_stop_ids=req.anchor_stop_ids,
        intermediate_stop_hints=req.intermediate_stop_hints,
        corridor_hints=req.corridor_hints,
        locality_clues=req.locality_clues,
        must_pass_through=req.must_pass_through,
        sequence_notes=_clean_text(req.sequence_notes),
        sequence_confidence=_clean_text(req.sequence_confidence),
        llm_sequence_suggestion=req.llm_sequence_suggestion,
    )


def validate_manual_sequence_rows(
    *,
    ordered_stop_ids: Sequence[str],
    resolved_rows: Sequence[Mapping[str, Any]],
    is_loop: bool,
    min_stops: int = 2,
    jump_warn_m: float = 3500.0,
) -> ManualSequenceValidation:
    errors: list[str] = []
    warnings: list[str] = []

    by_id: dict[str, Mapping[str, Any]] = {}
    for row in resolved_rows or []:
        sid = str(row.get("stop_id") or row.get("node_id") or "").strip()
        if sid:
            by_id[sid] = row

    normalized: list[dict[str, Any]] = []
    for sid in ordered_stop_ids:
        row = by_id.get(str(sid))
        if not row:
            errors.append(f"Invalid or non-approved stop id: {sid}")
            continue
        lat = row.get("lat")
        lon = row.get("lon")
        if lat is None or lon is None:
            errors.append(f"Stop {sid} is missing coordinates")
            continue
        normalized.append(
            {
                "stop_id": str(sid),
                "node_id": str(sid),
                "lat": float(lat),
                "lon": float(lon),
                "name": row.get("name"),
                "ref": row.get("ref"),
                "place_id": row.get("place_id"),
            }
        )

    if len(ordered_stop_ids) < int(min_stops):
        errors.append(f"Sequence too short: minimum is {int(min_stops)} stops")

    for i in range(max(0, len(ordered_stop_ids) - 1)):
        a = str(ordered_stop_ids[i])
        b = str(ordered_stop_ids[i + 1])
        if a == b:
            errors.append(f"Duplicate consecutive stops at positions {i + 1} and {i + 2}: {a}")

    if not is_loop:
        seen: dict[str, int] = {}
        repeated: list[str] = []
        for sid in ordered_stop_ids:
            seen[sid] = int(seen.get(sid, 0)) + 1
            if seen[sid] == 2:
                repeated.append(sid)
        if repeated:
            warnings.append(
                "Repeated stops detected while is_loop=false: "
                + ", ".join(repeated[:8])
                + (" ..." if len(repeated) > 8 else "")
            )

    if len(normalized) >= 2:
        for i in range(len(normalized) - 1):
            a = normalized[i]
            b = normalized[i + 1]
            dist_m = _haversine_m(float(a["lon"]), float(a["lat"]), float(b["lon"]), float(b["lat"]))
            if dist_m > float(jump_warn_m):
                warnings.append(
                    f"Excessive jump {i + 1}->{i + 2}: {dist_m:.1f}m exceeds {float(jump_warn_m):.1f}m"
                )

    return ManualSequenceValidation(
        errors=errors,
        warnings=warnings,
        ordered_stops=normalized,
    )


def reversed_sequence(stop_ids: Sequence[str]) -> list[str]:
    return list(reversed([str(x) for x in stop_ids]))


# ---------------------------------------------------------------------------
# Hint provenance helpers
# ---------------------------------------------------------------------------

def build_hint_provenance(
    *,
    name_hint_source: str = "manual",
    operator_hint_source: str = "manual",
    variant_hint_source: str = "manual",
) -> dict:
    """Build a provenance dict tracking where each hint came from."""
    return {
        "name_hint": name_hint_source,
        "operator_hint": operator_hint_source,
        "variant_hint": variant_hint_source,
    }


def merge_hints_with_priority(
    *,
    manual: Optional[Mapping] = None,
    llm: Optional[Mapping] = None,
    gap_default: Optional[Mapping] = None,
) -> tuple[dict, dict]:
    """
    Merge hints from multiple sources with priority: manual > llm > gap_default.
    Returns (merged_hints, provenance) tuple.
    """
    sources = [
        ("manual", dict(manual or {})),
        ("llm_suggestion", dict(llm or {})),
        ("gap_default", dict(gap_default or {})),
    ]
    merged: dict = {}
    provenance: dict = {}
    for hint_key in ("name_hint", "operator_hint", "variant_hint"):
        for source_name, source_dict in sources:
            val = _clean_text(source_dict.get(hint_key))
            if val and hint_key not in merged:
                merged[hint_key] = val
                provenance[hint_key] = source_name
                break
        if hint_key not in merged:
            merged[hint_key] = None
            provenance[hint_key] = "none"
    return merged, provenance


def compute_sequence_bbox(ordered_stops: Sequence[Mapping[str, Any]]) -> Optional[dict]:
    """Compute bounding box from ordered stops list."""
    lats = [float(s["lat"]) for s in ordered_stops if s.get("lat") is not None]
    lons = [float(s["lon"]) for s in ordered_stops if s.get("lon") is not None]
    if not lats or not lons:
        return None
    return {
        "min_lat": min(lats),
        "max_lat": max(lats),
        "min_lon": min(lons),
        "max_lon": max(lons),
    }


def compute_total_distance_m(ordered_stops: Sequence[Mapping[str, Any]]) -> float:
    """Sum of haversine distances between consecutive stops."""
    total = 0.0
    for i in range(len(ordered_stops) - 1):
        a, b = ordered_stops[i], ordered_stops[i + 1]
        if a.get("lat") is not None and b.get("lat") is not None:
            total += _haversine_m(
                float(a["lon"]), float(a["lat"]),
                float(b["lon"]), float(b["lat"]),
            )
    return total


# ---------------------------------------------------------------------------
# Sequence discovery context
# ---------------------------------------------------------------------------

SEQUENCE_READINESS_LEVELS = (
    "strong_sequence_context",
    "enough_anchor_stops",
    "enough_hint_stops",
    "weak_sequence_context",
    "candidate_for_llm_sequence_interpretation",
    "insufficient_evidence",
)


@dataclass
class SequenceDiscoveryContext:
    """Aggregated context for sequence discovery on a route case."""
    anchor_stop_ids: list[str]
    intermediate_stop_hints: list[dict]
    corridor_hints: list[str]
    locality_clues: list[str]
    must_pass_through: list[str]
    name_hint: Optional[str] = None
    operator_hint: Optional[str] = None
    variant_hint: Optional[str] = None
    sector_key: Optional[str] = None
    coverage_gap_id: Optional[str] = None
    gap_start_hint: Optional[str] = None
    gap_end_hint: Optional[str] = None
    gap_direction_hint: Optional[str] = None
    total_ordered_stops: int = 0
    sequence_notes: Optional[str] = None

    def readiness_level(self) -> str:
        """Assess sequence readiness based on available evidence."""
        n_anchors = len(self.anchor_stop_ids)
        n_intermediates = len(self.intermediate_stop_hints)
        n_ordered = self.total_ordered_stops
        has_corridor = bool(self.corridor_hints)
        has_locality = bool(self.locality_clues)
        has_name = bool(self.name_hint)

        if n_ordered >= 5 and n_anchors >= 2:
            return "strong_sequence_context"
        if n_ordered >= 3 or n_anchors >= 2:
            return "enough_anchor_stops"
        if n_intermediates >= 2 or (n_anchors >= 1 and (has_corridor or has_locality)):
            return "enough_hint_stops"
        if has_name and (has_corridor or has_locality or n_anchors >= 1):
            return "candidate_for_llm_sequence_interpretation"
        if has_name or n_anchors >= 1:
            return "weak_sequence_context"
        return "insufficient_evidence"

    def to_evidence_dict(self) -> dict:
        """Convert to dict for LLM evidence payload."""
        return {
            "anchor_stop_ids": self.anchor_stop_ids,
            "anchor_count": len(self.anchor_stop_ids),
            "intermediate_stop_hints": self.intermediate_stop_hints,
            "intermediate_count": len(self.intermediate_stop_hints),
            "corridor_hints": self.corridor_hints,
            "locality_clues": self.locality_clues,
            "must_pass_through": self.must_pass_through,
            "name_hint": self.name_hint,
            "operator_hint": self.operator_hint,
            "variant_hint": self.variant_hint,
            "sector_key": self.sector_key,
            "coverage_gap_id": self.coverage_gap_id,
            "gap_start_hint": self.gap_start_hint,
            "gap_end_hint": self.gap_end_hint,
            "gap_direction_hint": self.gap_direction_hint,
            "total_ordered_stops": self.total_ordered_stops,
            "sequence_notes": self.sequence_notes,
            "readiness_level": self.readiness_level(),
        }


def build_route_seed_from_context(
    context: SequenceDiscoveryContext,
    *,
    route_job_id: Optional[str] = None,
    service_route_id: Optional[str] = None,
    direction_id: Optional[int] = None,
    coverage_gap_id: Optional[str] = None,
) -> dict:
    """
    Convert a SequenceDiscoveryContext into a route seed payload dict
    compatible with the sequence discovery pipeline.
    """
    intermediate_hints = []
    for hint in (context.intermediate_stop_hints or []):
        if isinstance(hint, dict):
            text = hint.get("name") or hint.get("hint") or hint.get("text") or ""
            if text:
                intermediate_hints.append(str(text))
        elif isinstance(hint, str) and hint.strip():
            intermediate_hints.append(hint.strip())

    anchor_a = ""
    anchor_b = ""
    if context.anchor_stop_ids and len(context.anchor_stop_ids) >= 2:
        anchor_a = str(context.anchor_stop_ids[0])
        anchor_b = str(context.anchor_stop_ids[-1])
    elif context.anchor_stop_ids and len(context.anchor_stop_ids) == 1:
        anchor_a = str(context.anchor_stop_ids[0])
    if context.gap_start_hint and not anchor_a:
        anchor_a = context.gap_start_hint
    if context.gap_end_hint and not anchor_b:
        anchor_b = context.gap_end_hint

    return {
        "route_name": context.name_hint or "",
        "operator_name": context.operator_hint,
        "anchor_a_hint": anchor_a,
        "anchor_b_hint": anchor_b,
        "intermediate_hints": intermediate_hints,
        "locality_hints": list(context.locality_clues or []),
        "corridor_description": ", ".join(context.corridor_hints) if context.corridor_hints else None,
        "sector_key": context.sector_key,
        "route_job_id": route_job_id,
        "service_route_id": service_route_id,
        "direction_id": direction_id,
        "coverage_gap_id": coverage_gap_id or context.coverage_gap_id,
    }


def build_sequence_context(payload: Mapping[str, Any], *, gap_context: Optional[Mapping] = None) -> SequenceDiscoveryContext:
    """Build a SequenceDiscoveryContext from a payload dict + optional gap context."""
    gap = dict(gap_context or {})
    gap_row = dict(gap.get("gap") or gap)
    return SequenceDiscoveryContext(
        anchor_stop_ids=list(payload.get("anchor_stop_ids") or []),
        intermediate_stop_hints=list(payload.get("intermediate_stop_hints") or []),
        corridor_hints=list(payload.get("corridor_hints") or []),
        locality_clues=list(payload.get("locality_clues") or []),
        must_pass_through=list(payload.get("must_pass_through") or []),
        name_hint=_clean_text(payload.get("name_hint")),
        operator_hint=_clean_text(payload.get("operator_hint")),
        variant_hint=_clean_text(payload.get("variant_hint")),
        sector_key=_clean_text(payload.get("sector_key")),
        coverage_gap_id=_clean_text(payload.get("coverage_gap_id")),
        gap_start_hint=_clean_text(gap_row.get("start_hint")),
        gap_end_hint=_clean_text(gap_row.get("end_hint")),
        gap_direction_hint=_clean_text(gap_row.get("direction_hint")),
        total_ordered_stops=len(payload.get("ordered_stop_ids") or []),
        sequence_notes=_clean_text(payload.get("sequence_notes")),
    )
