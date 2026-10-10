"""Phase 4 Evidence and Matching: read evidence, propose routes, record human judgements.

The tabs were written for an HTTP service that no longer exists (and whose
matching endpoints were never implemented). This module is the in-process
replacement, kept to what the review screens need.

Principle: the pipeline proposes, a person decides.

* Reads are read-only.
* ``generate_candidates`` is a *proposal*: a deterministic, explainable score
  of every production route against one evidence record. A reviewer asks for
  it; the rows record who asked and which matcher produced them. Nothing is
  accepted from a candidate.
* ``create_evidence`` and ``label_match`` are human decisions. Each is one
  transaction together with its audit row in ``console.phase_decisions``; if
  the audit insert fails, the write is rolled back.

There is no trained ranking model: ``phase4_naming/phase4_models/lgbm/v1/model.txt``
is empty and the old ``/ranker/train`` and ``/ranker/infer`` endpoints returned
"not implemented". Model prediction is therefore not offered; labels saved
here are the training data such a model would need.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Optional, Sequence

from datamind_console.db import schema_guard
from datamind_console.db.db import db_conn, fetch_one, query_all, query_one, transaction
from datamind_console.phases.phase4_naming.schema import EVIDENCE_MATCHING
from datamind_console.review.human_review import (
    ReviewInputError,
    Reviewer,
    _audit_notes,
    _insert_decision,
)

QueryAll = Callable[[str, Optional[Sequence]], list]
QueryOne = Callable[[str, Optional[Sequence]], Optional[dict]]

MATCHER = "deterministic_v1"
EVIDENCE_SOURCE_TYPES = ("manual_admin_note", "official_schedule", "operator_statement", "field_observation")
EVIDENCE_TEXT_FIELDS = ("route_ref", "route_name", "operator_name", "from_name", "to_name")
STATUS_FILTERS = ("pending", "linked", "partial", "rejected")
MAX_NOTE_CHARS = 4000
MIN_CANDIDATE_SCORE = 0.25

# Relevance (V4_2 scale) -> audit decision and reason code.
LABELS = {
    2: ("APPROVE", "evidence_match_confirmed"),
    1: ("EDIT", "evidence_match_partial"),
    0: ("REJECT", "evidence_match_rejected"),
}

# Signal weights. A signal the evidence cannot provide (no ref, no geometry...)
# is left out and the remaining weights are renormalised, so a record with
# only a name is not penalised for lacking a geometry.
WEIGHTS = {
    "route_hint": 0.30,
    "ref": 0.20,
    "name": 0.15,
    "endpoints": 0.15,
    "spatial": 0.15,
    "operator": 0.05,
}


def require_schema(connection_factory: Callable = db_conn) -> None:
    """Read-only check that V4_8 is applied; raises SchemaNotReadyError with the CLI command."""
    schema_guard.require(EVIDENCE_MATCHING, connection_factory=connection_factory)


# ---------------------------------------------------------------------------
# Evidence: read
# ---------------------------------------------------------------------------

EVIDENCE_LIST_SQL = """
WITH labelled AS (
  SELECT record_id, max(relevance) AS best_relevance, count(*) AS n_labels
  FROM semantics.match_labels
  GROUP BY record_id
)
SELECT e.record_id::text AS record_id, e.source_type, e.source_id,
       e.route_id_hint::text AS route_id_hint, e.route_ref, e.route_name, e.operator_name,
       e.from_name, e.to_name, e.confidence_hint, e.created_at,
       e.raw->>'title' AS title, e.raw->>'created_by' AS created_by,
       (e.geom IS NOT NULL) AS has_geometry, (e.bbox IS NOT NULL) AS has_bbox,
       CASE WHEN l.record_id IS NULL THEN 'pending'
            WHEN l.best_relevance = 2 THEN 'linked'
            WHEN l.best_relevance = 1 THEN 'partial'
            ELSE 'rejected' END AS status,
       COALESCE(l.n_labels, 0) AS n_labels,
       count(*) OVER () AS total
FROM semantics.route_evidence_records e
LEFT JOIN labelled l ON l.record_id = e.record_id
WHERE (%(source_type)s::text IS NULL OR e.source_type = %(source_type)s)
  AND (%(search)s::text IS NULL OR concat_ws(' ', e.route_ref, e.route_name, e.operator_name,
        e.from_name, e.to_name, e.source_id, e.raw->>'title') ILIKE '%%' || %(search)s || '%%')
  AND (%(status)s::text IS NULL OR
       CASE WHEN l.record_id IS NULL THEN 'pending'
            WHEN l.best_relevance = 2 THEN 'linked'
            WHEN l.best_relevance = 1 THEN 'partial'
            ELSE 'rejected' END = %(status)s)
ORDER BY e.created_at DESC, e.record_id
LIMIT %(limit)s OFFSET %(offset)s
"""


def list_evidence(*, search: str = "", source_type: str = "", status: str = "", limit: int = 50, offset: int = 0,
                  query: QueryAll = query_all) -> dict[str, Any]:
    if status and status not in STATUS_FILTERS:
        raise ReviewInputError("unknown_status")
    params = {
        "search": (search or "").strip() or None,
        "source_type": (source_type or "").strip() or None,
        "status": status or None,
        "limit": max(1, min(int(limit), 500)),
        "offset": max(0, int(offset)),
    }
    rows = [dict(r) for r in query(EVIDENCE_LIST_SQL, params) or []]
    total = int(rows[0]["total"]) if rows else 0
    for r in rows:
        r.pop("total", None)
    return {"items": rows, "total": total}


def list_source_types(query: QueryAll = query_all) -> list[str]:
    rows = query("SELECT DISTINCT source_type FROM semantics.route_evidence_records ORDER BY 1", None) or []
    return [str(r["source_type"]) for r in rows]


EVIDENCE_DETAIL_SQL = """
SELECT e.record_id::text AS record_id, e.source_type, e.source_id,
       e.route_id_hint::text AS route_id_hint, e.route_ref, e.route_name, e.operator_name,
       e.from_name, e.to_name, e.via, e.confidence_hint, e.raw, e.created_at,
       ST_AsGeoJSON(e.geom) AS geom_geojson, ST_AsGeoJSON(e.bbox) AS bbox_geojson
FROM semantics.route_evidence_records e
WHERE e.record_id = %s::uuid
"""

LABELS_FOR_RECORD_SQL = """
SELECT l.route_id::text AS route_id, l.relevance, l.notes, l.reviewed_by, l.label_source,
       l.created_at, l.updated_at,
       COALESCE(s.route_name, r.route_name) AS route_name
FROM semantics.match_labels l
LEFT JOIN route_prod.routes r ON r.route_id = l.route_id
LEFT JOIN route_prod.route_semantics s ON s.route_id = l.route_id
WHERE l.record_id = %s::uuid
ORDER BY l.relevance DESC, l.updated_at DESC
"""


def evidence_detail(record_id: str, *, query: QueryAll = query_all, query1: QueryOne = query_one) -> Optional[dict[str, Any]]:
    _require_uuid(record_id)
    rec = query1(EVIDENCE_DETAIL_SQL, (record_id,))
    if not rec:
        return None
    out = dict(rec)
    out["labels"] = [dict(r) for r in query(LABELS_FOR_RECORD_SQL, (record_id,)) or []]
    return out


# ---------------------------------------------------------------------------
# Evidence: manual creation (human decision)
# ---------------------------------------------------------------------------

def create_evidence(*, reviewer: Reviewer, source_type: str, fields: dict[str, Any], title: str = "", note: str = "",
                    confidence_hint: float = 0.5, transaction_factory: Callable = transaction) -> dict[str, Any]:
    """Add one evidence record typed by a person, with its audit row, in one transaction."""
    who = reviewer.validated()
    if source_type not in EVIDENCE_SOURCE_TYPES:
        raise ReviewInputError("unknown_source_type")
    clean = {k: (str(fields.get(k) or "").strip() or None) for k in EVIDENCE_TEXT_FIELDS}
    if not any(clean.values()):
        raise ReviewInputError("evidence_empty")
    note = (note or "").strip()
    if len(note) > MAX_NOTE_CHARS:
        raise ReviewInputError("note_too_long")
    try:
        confidence = float(confidence_hint)
    except (TypeError, ValueError):
        raise ReviewInputError("confidence_invalid") from None
    if not 0.0 <= confidence <= 1.0:
        raise ReviewInputError("confidence_invalid")
    raw = {"title": (title or "").strip() or None, "note": note or None, "created_by": who.name, "origin": "console_manual"}
    source_id = f"manual:{uuid.uuid4()}"
    with transaction_factory() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO semantics.route_evidence_records
              (source_type, source_id, route_ref, route_name, operator_name, from_name, to_name, confidence_hint, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING record_id::text AS record_id
            """,
            (source_type, source_id, clean["route_ref"], clean["route_name"], clean["operator_name"],
             clean["from_name"], clean["to_name"], confidence, json.dumps(raw)),
        )
        record_id = str(row["record_id"])
        audit = _insert_decision(
            conn, phase=4, item_id=f"evidence:{record_id}", decision="EDIT", reason_code="evidence_created",
            notes=_audit_notes(who, (title or "").strip()), user_id=who.user_id,
        )
    return {"record_id": record_id, "source_id": source_id, **audit}


# ---------------------------------------------------------------------------
# Matching: deterministic candidates (a proposal, requested by a person)
# ---------------------------------------------------------------------------

# Every production route with the raw similarity signals against one record.
# NULL means "the evidence has nothing to compare on this signal".
SIGNALS_SQL = """
WITH ev AS (
  SELECT record_id, route_id_hint,
         lower(unaccent(NULLIF(btrim(route_ref), ''))) AS ref,
         lower(unaccent(NULLIF(btrim(route_name), ''))) AS name,
         lower(unaccent(NULLIF(btrim(operator_name), ''))) AS operator,
         lower(unaccent(NULLIF(btrim(from_name), ''))) AS from_name,
         lower(unaccent(NULLIF(btrim(to_name), ''))) AS to_name,
         geom, bbox
  FROM semantics.route_evidence_records
  WHERE record_id = %s::uuid
),
routes AS (
  SELECT r.route_id,
         COALESCE(s.route_name, r.route_name) AS route_name,
         lower(unaccent(COALESCE(s.route_name, r.route_name, ''))) AS name_n,
         lower(unaccent(COALESCE(NULLIF(btrim(s.route_ref), ''), NULLIF(btrim(j.known_ref), ''), ''))) AS ref_n,
         lower(unaccent(COALESCE(s.operator_name, ''))) AS operator_n,
         lower(unaccent(COALESCE(nf.name, ''))) AS first_n,
         lower(unaccent(COALESCE(nl.name, ''))) AS last_n,
         r.geom
  FROM route_prod.routes r
  JOIN route_raw.route_jobs j ON j.route_id = r.route_id
  LEFT JOIN route_prod.route_semantics s ON s.route_id = r.route_id
  LEFT JOIN node_prod.nodes nf ON nf.node_id = r.stop_node_ids[1]
  LEFT JOIN node_prod.nodes nl ON nl.node_id = r.stop_node_ids[array_upper(r.stop_node_ids, 1)]
)
SELECT ro.route_id::text AS route_id, ro.route_name,
       CASE WHEN ev.route_id_hint IS NULL THEN NULL WHEN ev.route_id_hint = ro.route_id THEN 1.0 ELSE 0.0 END AS route_hint,
       CASE WHEN ev.ref IS NULL THEN NULL WHEN ev.ref = ro.ref_n AND ro.ref_n <> '' THEN 1.0 ELSE 0.0 END AS ref,
       CASE WHEN ev.name IS NULL THEN NULL ELSE similarity(ev.name, ro.name_n) END AS name,
       CASE WHEN ev.operator IS NULL THEN NULL ELSE similarity(ev.operator, ro.operator_n) END AS operator,
       CASE WHEN ev.from_name IS NULL THEN NULL ELSE similarity(ev.from_name, ro.first_n) END AS from_first,
       CASE WHEN ev.from_name IS NULL THEN NULL ELSE similarity(ev.from_name, ro.last_n) END AS from_last,
       CASE WHEN ev.to_name IS NULL THEN NULL ELSE similarity(ev.to_name, ro.last_n) END AS to_last,
       CASE WHEN ev.to_name IS NULL THEN NULL ELSE similarity(ev.to_name, ro.first_n) END AS to_first,
       CASE
         WHEN ro.geom IS NULL OR (ev.geom IS NULL AND ev.bbox IS NULL) THEN NULL
         WHEN ev.geom IS NOT NULL THEN LEAST(1.0, COALESCE(
           ST_Length(ST_Intersection(ev.geom, ST_Buffer(ro.geom::geography, 150)::geometry)::geography)
           / NULLIF(ST_Length(ev.geom::geography), 0), 0))
         WHEN ST_Intersects(ro.geom, ev.bbox) THEN 1.0 ELSE 0.0
       END AS spatial
FROM ev CROSS JOIN routes ro
"""


def _endpoints(sig: dict[str, Any]) -> Optional[float]:
    """Best of the two orientations: (from=first, to=last) or (from=last, to=first)."""
    forward = [sig.get("from_first"), sig.get("to_last")]
    backward = [sig.get("from_last"), sig.get("to_first")]
    forward = [float(x) for x in forward if x is not None]
    backward = [float(x) for x in backward if x is not None]
    if not forward and not backward:
        return None
    return max(sum(forward) / len(forward) if forward else 0.0, sum(backward) / len(backward) if backward else 0.0)


def score_route(sig: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Weighted mean of the signals the evidence provides; returns (score, explanation)."""
    values = {
        "route_hint": sig.get("route_hint"),
        "ref": sig.get("ref"),
        "name": sig.get("name"),
        "endpoints": _endpoints(sig),
        "spatial": sig.get("spatial"),
        "operator": sig.get("operator"),
    }
    present = {k: float(v) for k, v in values.items() if v is not None}
    if not present:
        return 0.0, {"signals": {}, "weights": {}, "matcher": MATCHER}
    total_w = sum(WEIGHTS[k] for k in present)
    score = sum(WEIGHTS[k] * v for k, v in present.items()) / total_w
    parts = {
        "signals": {k: round(v, 4) for k, v in present.items()},
        "weights": {k: round(WEIGHTS[k] / total_w, 4) for k in present},
        "matcher": MATCHER,
    }
    return round(min(1.0, max(0.0, score)), 6), parts


def rank_candidates(signal_rows: Sequence[dict[str, Any]], *, limit: int = 25,
                    min_score: float = MIN_CANDIDATE_SCORE) -> list[dict[str, Any]]:
    scored = []
    for row in signal_rows:
        score, parts = score_route(row)
        exact = (row.get("route_hint") == 1.0) or (row.get("ref") == 1.0)
        if score >= min_score or exact:
            scored.append({"route_id": str(row["route_id"]), "route_name": row.get("route_name"), "score": score, "score_parts": parts})
    scored.sort(key=lambda c: (-c["score"], c["route_id"]))
    out = scored[: max(1, int(limit))]
    for i, c in enumerate(out):
        c["is_best"] = i == 0
    return out


def generate_candidates(record_id: str, *, reviewer: Reviewer, limit: int = 25,
                        query: QueryAll = query_all, transaction_factory: Callable = transaction) -> dict[str, Any]:
    """Score every production route for one record and store the top proposals (idempotent)."""
    _require_uuid(record_id)
    who = reviewer.validated()
    signals = query(SIGNALS_SQL, (record_id,)) or []
    if not signals and not query("SELECT 1 FROM semantics.route_evidence_records WHERE record_id = %s::uuid", (record_id,)):
        raise ReviewInputError("evidence_not_found")
    ranked = rank_candidates([dict(r) for r in signals], limit=limit)
    with transaction_factory() as conn:
        kept = []
        for c in ranked:
            row = fetch_one(
                conn,
                """
                INSERT INTO semantics.route_evidence_matches
                  (record_id, route_id, matcher, score, score_parts, is_best, requested_by)
                VALUES (%s::uuid, %s::uuid, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (record_id, route_id, matcher) DO UPDATE
                  SET score = EXCLUDED.score, score_parts = EXCLUDED.score_parts, is_best = EXCLUDED.is_best,
                      requested_by = EXCLUDED.requested_by, created_at = now()
                RETURNING match_id::text AS match_id
                """,
                (record_id, c["route_id"], MATCHER, c["score"], json.dumps(c["score_parts"]), c["is_best"], who.name),
            )
            c["match_id"] = str(row["match_id"])
            kept.append(c["route_id"])
        # Proposals from an earlier run that no longer qualify are withdrawn; labels keep their decision.
        fetch_one(
            conn,
            """
            WITH gone AS (
              DELETE FROM semantics.route_evidence_matches
              WHERE record_id = %s::uuid AND matcher = %s AND NOT (route_id::text = ANY(%s::text[]))
              RETURNING 1
            ) SELECT count(*) AS n FROM gone
            """,
            (record_id, MATCHER, kept),
        )
    return {"record_id": record_id, "matcher": MATCHER, "items": ranked, "routes_scored": len(signals)}


CANDIDATES_SQL = """
SELECT m.match_id::text AS match_id, m.route_id::text AS route_id, m.score, m.score_parts, m.is_best,
       m.matcher, m.requested_by, m.created_at,
       COALESCE(s.route_name, r.route_name) AS route_name, s.route_ref, s.operator_name,
       l.relevance, l.reviewed_by, l.notes AS label_notes, l.updated_at AS labelled_at
FROM semantics.route_evidence_matches m
LEFT JOIN route_prod.routes r ON r.route_id = m.route_id
LEFT JOIN route_prod.route_semantics s ON s.route_id = m.route_id
LEFT JOIN semantics.match_labels l ON l.record_id = m.record_id AND l.route_id = m.route_id
WHERE m.record_id = %s::uuid
ORDER BY m.score DESC, m.route_id
"""


def list_candidates(record_id: str, *, query: QueryAll = query_all) -> list[dict[str, Any]]:
    _require_uuid(record_id)
    return [dict(r) for r in query(CANDIDATES_SQL, (record_id,)) or []]


# ---------------------------------------------------------------------------
# Matching: human label
# ---------------------------------------------------------------------------

def label_match(record_id: str, route_id: str, relevance: int, *, reviewer: Reviewer, notes: str = "",
                match_id: Optional[str] = None, transaction_factory: Callable = transaction) -> dict[str, Any]:
    """Record a reviewer's judgement on one (evidence, route) pair, with its audit row."""
    _require_uuid(record_id)
    _require_uuid(route_id)
    if match_id:
        _require_uuid(match_id)
    who = reviewer.validated()
    if relevance not in LABELS:
        raise ReviewInputError("unknown_relevance")
    notes = (notes or "").strip()
    if relevance == 0 and len(notes) < 3:
        raise ReviewInputError("reason_required")
    if len(notes) > MAX_NOTE_CHARS:
        raise ReviewInputError("note_too_long")
    decision, reason_code = LABELS[relevance]
    with transaction_factory() as conn:
        row = fetch_one(
            conn,
            """
            INSERT INTO semantics.match_labels (record_id, route_id, match_id, relevance, label_source, notes, reviewed_by)
            VALUES (%s::uuid, %s::uuid, %s::uuid, %s, 'admin_ui', %s, %s)
            ON CONFLICT (record_id, route_id) DO UPDATE
              SET relevance = EXCLUDED.relevance, notes = EXCLUDED.notes, reviewed_by = EXCLUDED.reviewed_by,
                  match_id = COALESCE(EXCLUDED.match_id, semantics.match_labels.match_id), updated_at = now()
            RETURNING label_id
            """,
            (record_id, route_id, match_id, relevance, notes or None, who.name),
        )
        audit = _insert_decision(
            conn, phase=4, item_id=f"evidence_match:{record_id}:{route_id}", decision=decision,
            reason_code=reason_code, notes=_audit_notes(who, notes), user_id=who.user_id,
        )
    return {"label_id": int(row["label_id"]), "relevance": relevance, **audit}


def _require_uuid(value: str) -> None:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise ReviewInputError("invalid_id") from None
