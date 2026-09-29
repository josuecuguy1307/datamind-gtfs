"""Re-classify pending approval_queue rows under the v2 taxonomy.

Three operations live here:

* :func:`reclassify_all_pending` — re-runs
  :func:`hades.enforcers.stop_coverage_enforcer.classify_persisted` over
  every row in ``route_prod.approval_queue`` whose ``status='pending'``
  and writes the result into the new
  :col:`quality_class` / :col:`tier4_pending_count` /
  :col:`classified_at` / :col:`reclassified_count` columns.

* :func:`reclassify_routes_affected_by_batch` — same, but scoped to the
  rows whose ``pending_dr_batches`` array contains ``batch_id``. This is
  what the DR response handler calls after a batch lands. It also marks
  the matching rows in ``route_prod.dr_batch_dependencies`` as
  ``batch_processed``.

* :func:`populate_dr_dependencies` — for each pending row with at least
  one tier-4 unresolved gap, predict which existing batch file
  (``workspace/dr_stop_coverage/queries/batch_*.md``) covers each gap by
  point-in-bbox match. Smallest enclosing bbox wins; gaps that fall in
  no existing batch get reported back so the operator knows an
  on-demand prompt is needed (see ``hades.enforcers.dr_prompt_generator``).

NOTE (2026-04-27): pre-ship orphan cleanup is **no longer auto-triggered**
from this module. The trigger moved to the operator-confirmed pre-swap
flow in :mod:`hades.enforcers.re_entry_swap_service`
(``preview_swap_with_cleanup`` + ``confirm_swap_with_cleanup``). The
``--with-preship-cleanup`` / ``--no-preship-cleanup`` CLI flags and the
``with_preship_cleanup`` keyword argument remain for source compat but
emit :class:`DeprecationWarning` and are no-ops. The
:func:`_apply_preship_cleanup` helper is still exported for the UI's
on-demand button (which Commit 5 of the refactor replaces with the
preview/confirm flow).

Hard rules
~~~~~~~~~~
* Reads + writes only ``route_prod.approval_queue`` and
  ``route_prod.dr_batch_dependencies``. Never touches ``route_prod.routes``.
* Each invocation runs in a single transaction (``with conn:``); a
  partial failure rolls back cleanly.
* The classifier itself is the source of truth — this module only feeds
  it and persists the result.

CLI
~~~
::

    python -m hades.enforcers.reclassifier all
    python -m hades.enforcers.reclassifier batch <batch_id>
    python -m hades.enforcers.reclassifier populate-deps
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras

from hades.enforcers.orphan_cleanup import (
    OrphanCleanupReport,
    cleanup_orphan_stops,
    validate_cleanup_not_destructive,
)
from hades.enforcers.stop_coverage_enforcer import classify_persisted
from datamind_core.dsn import need_dsn

ROOT = Path(__file__).resolve().parents[2]

# Classes a route may carry when it is eligible for pre-ship cleanup.
# Mirrors the partial-index predicate in migration 036.
_SHIPPABLE_CLASSES: frozenset[str] = frozenset(
    {"good", "acceptable", "ship_pending_dr"}
)
DEFAULT_DSN = ""


def _dsn() -> str:
    return os.environ.get("DB_DSN", DEFAULT_DSN)


# ---------------------------------------------------------------------------
# Existing-batch bbox index.
# ---------------------------------------------------------------------------

_BBOX_RE = re.compile(
    r"Bounding box:\s*\[\s*([-0-9.]+)\s*,\s*([-0-9.]+)\s*,"
    r"\s*([-0-9.]+)\s*,\s*([-0-9.]+)\s*\]",
)


def _scan_batch_bboxes(
    batch_dir: Path,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Return ``[(batch_id, (lat_min, lon_min, lat_max, lon_max)), …]``.

    ``batch_id`` is the file stem (e.g. ``batch_12_sangolquí_rumiñahui``).
    Only files matching ``batch_*.md`` are scanned — on-demand
    ``pending_requests/<unit>_NNN.md`` files live in a different
    directory and use a different naming scheme by design.
    """
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    for p in sorted(batch_dir.glob("batch_*.md")):
        try:
            text = p.read_text(errors="ignore")[:2000]
        except OSError:
            continue
        m = _BBOX_RE.search(text)
        if not m:
            continue
        bbox = (
            float(m.group(1)), float(m.group(2)),
            float(m.group(3)), float(m.group(4)),
        )
        out.append((p.stem, bbox))
    return out


def _bbox_area(bbox: tuple[float, float, float, float]) -> float:
    return (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])


def _smallest_enclosing_batch(
    lat: float,
    lon: float,
    batches: Iterable[tuple[str, tuple[float, float, float, float]]],
) -> Optional[str]:
    """Smallest-bbox tiebreak per the operator decision."""
    matches = [
        (bid, bbox) for bid, bbox in batches
        if bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]
    ]
    if not matches:
        return None
    matches.sort(key=lambda b: _bbox_area(b[1]))
    return matches[0][0]


# ---------------------------------------------------------------------------
# Per-report dep prediction (used by re_entry_worker on INSERT).
# ---------------------------------------------------------------------------

def predict_dr_batches_for_report(
    sc_dict: dict[str, Any],
    *,
    batch_dir: Optional[Path] = None,
    prefer_legacy_batches: bool = False,
) -> dict[str, Any]:
    """For one stop_coverage_report dict, return the predicted DR batches.

    Walks unresolved tier-4 gaps and reports each gap's midpoint plus
    (optionally) the smallest existing ``batch_*.md`` bbox enclosing it.

    ``prefer_legacy_batches`` controls whether to bbox-match against the
    historical legacy corpus in ``workspace/dr_stop_coverage/queries/``:

    * ``False`` (default, NEW BEHAVIOUR per skill 17) — gaps are NEVER
      matched to legacy batches. All tier-4 gaps come back in
      ``unmatched``; the operator is expected to call
      ``hades.enforcers.dr_prompt_generator`` to produce on-demand
      ``<unit_prefix>_NNN.md`` prompts. This is the canonical path
      for new units, T2-T8, Sample Region B, future provinces.
    * ``True`` (legacy fallback) — gaps are bbox-matched against legacy
      batches; matches go into ``matched`` and populate
      ``pending_dr_batches`` with legacy batch ids. Use only when you
      explicitly want to reuse historical batch coverage (e.g. to
      backfill an old unit whose batches already exist).

    Returns:

      {
        "matched":   [(batch_id, gap_idx, lat, lon), …],
        "unmatched": [(gap_idx, lat, lon), …],
        "tier4_pending_count": <int>,
        "pending_dr_batches":  [<unique sorted batch_id>, …],
      }
    """
    batch_dir = batch_dir or (
        ROOT / "workspace" / "dr_stop_coverage" / "queries"
    )
    batches = _scan_batch_bboxes(batch_dir) if prefer_legacy_batches else []
    matched: list[tuple[str, int, float, float]] = []
    unmatched: list[tuple[int, float, float]] = []
    for g in (sc_dict.get("gaps") or []):
        res = g.get("resolution") or {}
        if not res:
            continue
        if int(res.get("tier") or 0) != 4 or res.get("resolved"):
            continue
        mc = g.get("midpoint_coord")
        if not mc or len(mc) != 2:
            continue
        lat, lon = float(mc[0]), float(mc[1])
        gap_idx = int(g.get("idx") or 0)
        bid = _smallest_enclosing_batch(lat, lon, batches) if batches else None
        if bid is None:
            unmatched.append((gap_idx, lat, lon))
        else:
            matched.append((bid, gap_idx, lat, lon))
    pending = sorted({bid for bid, *_ in matched})
    return {
        "matched": matched,
        "unmatched": unmatched,
        "tier4_pending_count": len(matched) + len(unmatched),
        "pending_dr_batches": pending,
    }


# ---------------------------------------------------------------------------
# Validated-landmark loader (for --with-live-landmarks mode).
# ---------------------------------------------------------------------------

LandmarkMap = dict[tuple[str, int], dict[str, Any]]


def _load_validated_landmarks(
    *,
    validated_dir: Optional[Path] = None,
    only_batch_ids: Optional[Iterable[str]] = None,
) -> LandmarkMap:
    """Build ``(route_code, gap_idx) → landmark dict`` from validated JSONs.

    Reads every ``validated/<batch_id>.json`` file written by
    :func:`hades.enforcers.dr_stop_coverage_validator.validate_batch`,
    walks the ``accepted`` + ``accept_uncertain`` buckets, and indexes
    landmarks by ``(route_code, gap_idx)``. ``gap_number`` in the
    validated file is 1-indexed; we store 0-indexed ``gap_idx`` to match
    ``StopCoverageReport.gaps[].idx``.

    If multiple landmarks exist for the same gap, the highest-confidence
    one wins.

    ``only_batch_ids`` filters which validated files to read — used by
    :func:`reclassify_routes_affected_by_batch` to scope the impact to
    one batch.
    """
    validated_dir = validated_dir or (
        ROOT / "workspace" / "dr_stop_coverage" / "validated"
    )
    out: LandmarkMap = {}
    if not validated_dir.exists():
        return out
    only = set(only_batch_ids) if only_batch_ids is not None else None
    for vf in sorted(validated_dir.glob("*.json")):
        if only is not None and vf.stem not in only:
            continue
        try:
            data = json.loads(vf.read_text())
        except Exception:
            continue
        for bucket in ("accepted", "accept_uncertain"):
            for r in (data.get(bucket) or []):
                rc = r.get("route_code")
                gn = r.get("gap_number")
                if not rc or gn is None:
                    continue
                key = (rc, int(gn) - 1)
                conf = float(r.get("final_confidence") or 0.0)
                if key not in out or conf > float(out[key].get("final_confidence") or 0.0):
                    out[key] = {
                        "name": r.get("name"),
                        "lat": r.get("approx_lat"),
                        "lon": r.get("approx_lng"),
                        "final_confidence": conf,
                        "decision": r.get("decision"),
                        "source_batch": vf.stem,
                    }
    return out


def _apply_landmarks_to_report(
    sc_dict: dict[str, Any],
    route_code: str,
    landmark_map: LandmarkMap,
) -> tuple[dict[str, Any], int]:
    """Return a deepcopy of ``sc_dict`` with tier-4 gaps flipped to resolved
    where a validated landmark exists.

    Returns ``(mutated_dict, n_gaps_flipped)``. ``n_gaps_flipped`` is how
    many tier-4-prepared gaps now carry ``resolution.resolved=True`` (and
    therefore stop counting toward ``tier4_pending`` /
    ``n_unresolved`` in the classifier).

    Defensive: deepcopies the input so the caller's dict (e.g. the row
    fetched from psycopg2) is never mutated in place.
    """
    out = deepcopy(sc_dict)
    n_flipped = 0
    for g in (out.get("gaps") or []):
        gap_idx = int(g.get("idx", -1))
        key = (route_code, gap_idx)
        if key not in landmark_map:
            continue
        res = g.get("resolution") or {}
        if int(res.get("tier") or 0) != 4 or res.get("resolved"):
            continue
        if "resolution" not in g or g["resolution"] is None:
            g["resolution"] = {}
        g["resolution"]["resolved"] = True
        g["resolution"]["tier_label"] = "dr_prepared_landmark_validated"
        # Stash the landmark coords so downstream readers can render them.
        lm = landmark_map[key]
        g["resolution"]["candidate_coord"] = [lm.get("lat"), lm.get("lon")]
        g["resolution"]["candidate_metadata"] = {
            "name": lm.get("name"),
            "final_confidence": lm.get("final_confidence"),
            "decision": lm.get("decision"),
            "source_batch": lm.get("source_batch"),
        }
        n_flipped += 1
    return out, n_flipped


# ---------------------------------------------------------------------------
# Per-row re-classification helper.
# ---------------------------------------------------------------------------

def _reclassify_row(
    sc_dict: Optional[dict[str, Any]],
    geom_dict: Optional[dict[str, Any]],
    *,
    route_code: Optional[str] = None,
    landmark_map: Optional[LandmarkMap] = None,
) -> tuple[str, dict[str, Any], int, int]:
    """Classify one row, optionally applying live landmarks first.

    Returns ``(class_name, reasoning, tier4_pending_count, n_landmarks_applied)``.
    ``n_landmarks_applied`` is 0 unless ``landmark_map`` was passed and
    at least one tier-4 gap was flipped to resolved.
    """
    sc = sc_dict or {}
    n_applied = 0
    if landmark_map and route_code:
        sc, n_applied = _apply_landmarks_to_report(sc, route_code, landmark_map)
    cls, rsn = classify_persisted(sc, geom_dict or {})
    return cls, rsn, int(rsn.get("tier4_pending", 0)), n_applied


# ---------------------------------------------------------------------------
# Pre-ship orphan cleanup helpers.
#
# As of 2026-04-27 cleanup is no longer auto-triggered from reclassify
# loops; the operator-confirmed pre-swap flow in re_entry_swap_service
# is the single trigger. The functions below remain exported so the UI
# (and any in-flight callers) can still invoke cleanup directly until
# Commit 5 retires the on-demand UI button.
# ---------------------------------------------------------------------------


_DEPRECATED_CLEANUP_FLAG_MSG = (
    "with_preship_cleanup / --with-preship-cleanup / "
    "--no-preship-cleanup are deprecated as of 2026-04-27. The "
    "reclassifier no longer auto-triggers pre-ship cleanup; use "
    "re_entry_swap_service.preview_swap_with_cleanup + "
    "confirm_swap_with_cleanup instead. The flag is now a no-op."
)


def _report_to_json(
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


def _apply_preship_cleanup(
    queue_id: str,
    proposed_stops: Optional[list[dict]],
    proposed_shape: Optional[dict],
    conn,
    *,
    thresholds: Optional[dict] = None,
) -> dict[str, Any]:
    """Run pre-ship cleanup for a single approval_queue row.

    Returns a structured outcome dict. One of:

      * ``"applied"`` — cleanup ran, validator accepted, columns updated
        (proposed_stops, pre_ship_cleanup_applied=TRUE, report, at).
      * ``"rejected"`` — cleanup ran but the safety gate (>max_removal_pct)
        rejected it. Report is persisted; ``applied`` stays FALSE so a
        future operator-initiated cleanup can override.
      * ``"skipped_no_polyline"`` — proposed_shape lacks a usable
        LineString (< 2 coordinates). No DB write.
    """
    coords_raw = (proposed_shape or {}).get("coordinates") or []
    polyline: list[tuple[float, float]] = [
        (float(c[0]), float(c[1])) for c in coords_raw if len(c) >= 2
    ]
    if len(polyline) < 2:
        return {
            "queue_id": queue_id,
            "outcome": "skipped_no_polyline",
            "reason": "proposed_shape has fewer than 2 coordinates",
        }

    cleaned, report = cleanup_orphan_stops(
        proposed_stops or [],
        polyline,
        thresholds=thresholds,
    )
    ok, validate_reason = validate_cleanup_not_destructive(
        proposed_stops or [],
        cleaned,
        max_removal_pct=report.thresholds_used.get("max_removal_pct", 0.20),
    )

    report_json = _report_to_json(
        report, validate_ok=ok, validate_reason=validate_reason
    )

    if not ok:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE route_prod.approval_queue
                   SET pre_ship_cleanup_report = %s::jsonb
                 WHERE queue_id = %s::uuid
                """,
                (json.dumps(report_json), queue_id),
            )
        return {
            "queue_id": queue_id,
            "outcome": "rejected",
            "reason": validate_reason,
            "stops_before": report.total_stops_before,
            "stops_after_proposed": report.total_stops_after,
        }

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE route_prod.approval_queue
               SET proposed_stops             = %s::jsonb,
                   pre_ship_cleanup_applied   = TRUE,
                   pre_ship_cleanup_report    = %s::jsonb,
                   pre_ship_cleanup_at        = NOW()
             WHERE queue_id = %s::uuid
            """,
            (json.dumps(cleaned), json.dumps(report_json), queue_id),
        )
    return {
        "queue_id": queue_id,
        "outcome": "applied",
        "stops_before": report.total_stops_before,
        "stops_after": report.total_stops_after,
        "stops_removed": report.stops_removed,
        "stops_snapped": report.stops_snapped,
    }


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def reclassify_all_pending(
    *,
    dsn: Optional[str] = None,
    with_live_landmarks: bool = False,
    with_preship_cleanup: bool = False,
    limit: Optional[int] = None,
    progress_every: int = 10,
) -> dict[str, Any]:
    """Re-classify every pending approval_queue row.

    ``with_live_landmarks=False`` (default, backward-compatible) — runs
    the classifier on the persisted ``stop_coverage_report`` as-is. Use
    when only the classifier thresholds changed.

    ``with_live_landmarks=True`` — loads validated landmarks from
    ``workspace/dr_stop_coverage/validated/*.json`` and, before
    classifying, mutates the in-memory report so any tier-4 gap that has
    a validated landmark flips to ``resolved=True``. Use after a DR
    response handler run to actually convert landed landmarks into
    class upgrades. The persisted JSON in ``stop_coverage_report`` is
    NOT modified — only ``quality_class`` / ``tier4_pending_count`` are
    written back.

    ``with_preship_cleanup`` is **deprecated** (2026-04-27). Cleanup is
    no longer auto-triggered from this loop; the operator-confirmed
    pre-swap flow in re_entry_swap_service is the single trigger.
    Passing ``True`` here emits a :class:`DeprecationWarning` and is a
    no-op. The parameter remains so callers don't break at the
    boundary.

    ``limit`` simply caps the row count, ordered by ``enqueued_at``
    ASC. (The previous "cleanup-eligible cohort" filter is gone with
    the auto-trigger removal.)

    Skips on per-row failure (logs to stderr, continues batch). Reports
    progress every ``progress_every`` rows.
    """
    if with_preship_cleanup:
        warnings.warn(
            _DEPRECATED_CLEANUP_FLAG_MSG, DeprecationWarning, stacklevel=2,
        )

    dsn = dsn or _dsn()
    transitions: Counter = Counter()
    new_class_counts: Counter = Counter()
    landmarks_applied_total = 0
    skipped: list[dict[str, str]] = []
    n_examined = 0

    landmark_map: LandmarkMap = (
        _load_validated_landmarks() if with_live_landmarks else {}
    )
    print(
        f"[reclassify-all] mode={'live-landmarks' if with_live_landmarks else 'stored-report'}"
        f"  landmark_map={len(landmark_map)} entries"
        f"  preship_cleanup=off (auto-trigger removed 2026-04-27)",
        file=sys.stderr,
    )

    with psycopg2.connect(need_dsn(dsn)) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            base_select = """
                SELECT queue_id::text       AS queue_id,
                       route_code,
                       stop_coverage_report,
                       geometry_report,
                       quality_class,
                       status,
                       pending_dr_batches
                  FROM route_prod.approval_queue
                 WHERE status = 'pending'
            """
            params: tuple = ()
            if limit is not None:
                base_select += " ORDER BY enqueued_at ASC LIMIT %s"
                params = (int(limit),)
            cur.execute(base_select, params)
            rows = cur.fetchall()
        for r in rows:
            n_examined += 1
            try:
                cls, _rsn, tier4, n_applied = _reclassify_row(
                    r["stop_coverage_report"], r["geometry_report"],
                    route_code=r["route_code"],
                    landmark_map=landmark_map if with_live_landmarks else None,
                )
            except Exception as exc:
                skipped.append({"route_code": r["route_code"], "error": str(exc)[:200]})
                continue
            old = r["quality_class"]
            transitions[(old or "unknown", cls)] += 1
            new_class_counts[cls] += 1
            landmarks_applied_total += n_applied
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE route_prod.approval_queue
                       SET quality_class      = %s,
                           tier4_pending_count = %s,
                           classified_at       = NOW(),
                           reclassified_count  = reclassified_count + 1
                     WHERE queue_id = %s::uuid
                    """,
                    (cls, tier4, r["queue_id"]),
                )
            if n_examined % progress_every == 0:
                print(
                    f"  [{n_examined}/{len(rows)}]  "
                    f"landmarks_applied={landmarks_applied_total}  "
                    f"skipped={len(skipped)}",
                    file=sys.stderr,
                )
    return {
        "n_examined": n_examined,
        "with_live_landmarks": with_live_landmarks,
        "with_preship_cleanup": False,  # always no-op now
        "landmarks_applied_total": landmarks_applied_total,
        "by_class": dict(new_class_counts),
        "transitions": {f"{a}→{b}": c for (a, b), c in transitions.items()},
        "skipped": skipped,
    }


def reclassify_routes_affected_by_batch(
    batch_id: str,
    *,
    dsn: Optional[str] = None,
    with_live_landmarks: bool = True,
    with_preship_cleanup: bool = False,
) -> dict[str, Any]:
    """Re-classify only rows whose ``pending_dr_batches`` contains ``batch_id``.

    ``with_live_landmarks=True`` (default) — loads validated landmarks
    from THIS batch's ``validated/<batch_id>.json`` and applies them
    in-memory before re-classifying. This is the path
    :func:`hades.enforcers.dr_response_handler.process_response_and_reclassify`
    invokes after a batch lands; the live-landmark step is what turns
    a ``landmark_found`` deps flip into an actual class upgrade.

    ``with_live_landmarks=False`` — re-classify on the persisted report
    only. Useful for diagnostics.

    Also marks every matching ``dr_batch_dependencies`` row from
    ``waiting`` to ``batch_processed`` (the response handler then
    refines to ``landmark_found`` / ``landmark_not_found`` per gap).

    ``with_preship_cleanup`` is **deprecated** (2026-04-27). Cleanup is
    no longer auto-triggered after a batch lands; it runs only at
    operator-confirmed approval time via the swap-service preview/
    confirm flow. Passing ``True`` here emits a
    :class:`DeprecationWarning` and is a no-op.
    """
    if with_preship_cleanup:
        warnings.warn(
            _DEPRECATED_CLEANUP_FLAG_MSG, DeprecationWarning, stacklevel=2,
        )

    dsn = dsn or _dsn()
    upgrades: list[dict[str, str]] = []
    no_change = 0
    landmarks_applied_total = 0
    skipped: list[dict[str, str]] = []

    # Load ALL validated landmarks (not just this batch's). A route may
    # have gaps resolved by landmarks from earlier batches; scoping the
    # landmark map to one batch would falsely downgrade those routes
    # because the earlier landmarks would not be applied. Bug surfaced
    # 2026-04-22 when round-2 QC batch processing produced spurious
    # ship_pending_dr → degraded transitions.
    landmark_map: LandmarkMap = (
        _load_validated_landmarks() if with_live_landmarks else {}
    )

    with psycopg2.connect(need_dsn(dsn)) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT queue_id::text       AS queue_id,
                       route_code,
                       stop_coverage_report,
                       geometry_report,
                       quality_class,
                       status,
                       pending_dr_batches
                  FROM route_prod.approval_queue
                 WHERE status = 'pending'
                   AND pending_dr_batches && ARRAY[%s]::text[]
                """,
                (batch_id,),
            )
            rows = cur.fetchall()
        for r in rows:
            try:
                cls, _rsn, tier4, n_applied = _reclassify_row(
                    r["stop_coverage_report"], r["geometry_report"],
                    route_code=r["route_code"],
                    landmark_map=landmark_map if with_live_landmarks else None,
                )
            except Exception as exc:
                skipped.append({"route_code": r["route_code"], "error": str(exc)[:200]})
                continue
            landmarks_applied_total += n_applied
            old = r["quality_class"]
            if cls != old:
                upgrades.append(
                    {"route_code": r["route_code"], "from": old or "unknown",
                     "to": cls, "n_landmarks_applied": n_applied}
                )
            else:
                no_change += 1
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE route_prod.approval_queue
                       SET quality_class      = %s,
                           tier4_pending_count = %s,
                           classified_at       = NOW(),
                           reclassified_count  = reclassified_count + 1
                     WHERE queue_id = %s::uuid
                    """,
                    (cls, tier4, r["queue_id"]),
                )
                cur.execute(
                    """
                    UPDATE route_prod.dr_batch_dependencies
                       SET status      = 'batch_processed',
                           resolved_at = NOW()
                     WHERE dr_batch_id = %s
                       AND route_id    = %s::uuid
                       AND status      = 'waiting'
                    """,
                    (batch_id, r["route_code"]),
                )
    return {
        "batch_id": batch_id,
        "with_live_landmarks": with_live_landmarks,
        "with_preship_cleanup": False,  # always no-op now
        "rows_examined": len(rows),
        "landmarks_applied_total": landmarks_applied_total,
        "upgrades": upgrades,
        "no_change": no_change,
        "skipped": skipped,
    }


def populate_dr_dependencies(
    *,
    dsn: Optional[str] = None,
    batch_dir: Optional[Path] = None,
    prefer_legacy_batches: bool = False,
) -> dict[str, Any]:
    """Link every tier-4 unresolved gap to a predicted DR batch (legacy mode).

    ``prefer_legacy_batches`` follows the same semantics as
    :func:`predict_dr_batches_for_report`:

    * ``False`` (default, NEW BEHAVIOUR per skill 17) — no legacy bbox
      matching is performed. EVERY tier-4 gap is reported as
      ``gaps_with_no_matching_batch`` and the operator is expected to
      generate on-demand prompts via
      ``hades.enforcers.dr_prompt_generator``. No
      ``dr_batch_dependencies`` rows are inserted in this mode (because
      no batch_id is predicted yet — the on-demand generator inserts its
      own deps when the prompt is created).
    * ``True`` (explicit opt-in) — original bbox-greedy behaviour. For
      each pending approval_queue row, walks unresolved tier-4 gaps,
      finds the smallest legacy batch bbox enclosing the gap midpoint,
      and ``ON CONFLICT DO NOTHING`` inserts a row into
      ``dr_batch_dependencies``. Also writes the union of predicted
      ``batch_id``s into ``approval_queue.pending_dr_batches``. Use only
      to backfill historical batch coverage.

    Existing ``dr_batch_dependencies`` rows are never deleted by this
    function — historical state is preserved either way.
    """
    dsn = dsn or _dsn()
    batch_dir = batch_dir or (
        ROOT / "workspace" / "dr_stop_coverage" / "queries"
    )
    batches = _scan_batch_bboxes(batch_dir) if prefer_legacy_batches else []

    inserted = 0
    no_match = 0
    routes_with_deps: set[str] = set()
    routes_with_unmatched: set[str] = set()
    pending_per_route: dict[str, set[str]] = defaultdict(set)

    with psycopg2.connect(need_dsn(dsn)) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT queue_id::text AS queue_id,
                       route_code,
                       stop_coverage_report
                  FROM route_prod.approval_queue
                 WHERE status = 'pending'
                """
            )
            rows = cur.fetchall()
        for r in rows:
            sc = r["stop_coverage_report"] or {}
            for g in (sc.get("gaps") or []):
                res = g.get("resolution") or {}
                if not res:
                    continue
                if int(res.get("tier") or 0) != 4 or res.get("resolved"):
                    continue
                mc = g.get("midpoint_coord")
                if not mc or len(mc) != 2:
                    continue
                lat, lon = float(mc[0]), float(mc[1])
                batch_id = _smallest_enclosing_batch(lat, lon, batches)
                if batch_id is None:
                    no_match += 1
                    routes_with_unmatched.add(r["route_code"])
                    continue
                routes_with_deps.add(r["route_code"])
                pending_per_route[r["route_code"]].add(batch_id)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO route_prod.dr_batch_dependencies
                            (dr_batch_id, route_id, gap_idx, gap_coords, status)
                        VALUES (%s, %s::uuid, %s, %s::jsonb, 'waiting')
                        ON CONFLICT (dr_batch_id, route_id, gap_idx)
                        DO NOTHING
                        """,
                        (batch_id, r["route_code"], int(g.get("idx") or 0),
                         json.dumps({"lat": lat, "lon": lon})),
                    )
                    inserted += 1
        for route_code, batch_set in pending_per_route.items():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE route_prod.approval_queue
                       SET pending_dr_batches = %s::text[]
                     WHERE route_code = %s
                       AND status     = 'pending'
                    """,
                    (sorted(batch_set), route_code),
                )
    return {
        "deps_inserted": inserted,
        "gaps_with_no_matching_batch": no_match,
        "routes_with_deps": len(routes_with_deps),
        "routes_needing_on_demand_prompts": sorted(routes_with_unmatched),
    }


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _print_summary(result: dict[str, Any]) -> None:
    print(json.dumps(result, indent=2, default=str))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reclassifier",
        description="Re-classify pending approval_queue rows under the v2 taxonomy.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_a = sub.add_parser("all", help="Reclassify every pending approval_queue row.")
    p_a.add_argument(
        "--with-live-landmarks",
        action="store_true",
        help=("Apply currently-validated DR landmarks to each row's stored "
              "stop_coverage_report before classifying. Use after a DR pass "
              "to convert landed landmarks into class upgrades."),
    )
    p_a.add_argument(
        "--with-preship-cleanup",
        action="store_true",
        default=None,
        help=("Run pre-ship orphan cleanup on every shippable row whose "
              "trigger conditions hold. Default: matches "
              "--with-live-landmarks (the two complement)."),
    )
    p_a.add_argument(
        "--no-preship-cleanup",
        action="store_true",
        help="Disable pre-ship orphan cleanup, even with --with-live-landmarks.",
    )
    p_a.add_argument(
        "--limit",
        type=int,
        default=None,
        help=("Cap the number of rows processed in this run. Used for "
              "staged backfill — together with --with-preship-cleanup "
              "the cohort is restricted to cleanup-eligible rows, "
              "ordered by enqueued_at ASC."),
    )

    p_b = sub.add_parser(
        "batch",
        help="Reclassify rows whose pending_dr_batches contains <batch_id>.",
    )
    p_b.add_argument("batch_id")
    p_b.add_argument(
        "--no-live-landmarks",
        action="store_true",
        help=("Skip the live-landmark apply step (default ON for batch mode "
              "since this is invoked by the response handler after a batch "
              "lands and we want the upgrades to fire)."),
    )
    p_b.add_argument(
        "--no-preship-cleanup",
        action="store_true",
        help=("Skip pre-ship orphan cleanup. Default ON for batch mode "
              "(matches the response-handler call from production)."),
    )

    sub.add_parser(
        "populate-deps",
        help="Populate dr_batch_dependencies for tier-4 pending gaps.",
    )
    args = parser.parse_args(argv)

    if args.cmd == "all":
        # --with-preship-cleanup / --no-preship-cleanup are deprecated
        # no-ops as of 2026-04-27. Warn if either is explicitly passed.
        if args.with_preship_cleanup is True or args.no_preship_cleanup:
            warnings.warn(
                _DEPRECATED_CLEANUP_FLAG_MSG, DeprecationWarning, stacklevel=2,
            )
        _print_summary(reclassify_all_pending(
            with_live_landmarks=args.with_live_landmarks,
            with_preship_cleanup=False,
            limit=args.limit,
        ))
    elif args.cmd == "batch":
        if args.no_preship_cleanup:
            warnings.warn(
                _DEPRECATED_CLEANUP_FLAG_MSG, DeprecationWarning, stacklevel=2,
            )
        _print_summary(reclassify_routes_affected_by_batch(
            args.batch_id,
            with_live_landmarks=not args.no_live_landmarks,
            with_preship_cleanup=False,
        ))
    elif args.cmd == "populate-deps":
        _print_summary(populate_dr_dependencies())
    else:  # pragma: no cover
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
