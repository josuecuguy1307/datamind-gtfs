"""End-to-end DR response handler — closes the loop.

Given a saved response file (either an existing
``workspace/dr_stop_coverage/responses/batch_*.md`` or an on-demand
``<unit_prefix>_NNN.md`` produced by
:mod:`hades.enforcers.dr_prompt_generator`):

1. Parses the response via
   :func:`hades.enforcers.dr_stop_coverage_importer.import_batch_file`
   → writes ``workspace/dr_stop_coverage/parsed/<request_id>.json``.
2. Validates landmarks (Overpass + Nominatim + coord sanity) via
   :func:`hades.enforcers.dr_stop_coverage_validator.validate_batch`
   → writes ``workspace/dr_stop_coverage/validated/<request_id>.json``.
3. Updates ``route_prod.dr_batch_dependencies`` rows for this
   ``dr_batch_id``: each accepted/uncertain landmark flips the matching
   ``(route_id, gap_idx)`` row to ``landmark_found``; rejected landmarks
   that exhaust a gap flip it to ``landmark_not_found``.
4. Calls
   :func:`hades.enforcers.reclassifier.reclassify_routes_affected_by_batch`
   to re-run the v2 classifier on every approval_queue row that depends
   on this batch and report which ones upgraded.

CLI:
    python -m hades.enforcers.dr_response_handler <response_path>

Optional flags:
    --skip-network   pass through to the validator (offline runs)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import psycopg2

from hades.enforcers.dr_stop_coverage_importer import (
    PARSED_DIR,
    RESPONSE_DIR,
    import_batch_file,
)
from hades.enforcers.dr_stop_coverage_validator import (
    VALIDATED_DIR,
    validate_batch,
)
from hades.enforcers.reclassifier import (
    reclassify_routes_affected_by_batch,
)
from datamind_core.dsn import need_dsn

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DSN = ""
PENDING_REQUESTS_DIR = ROOT / "workspace" / "dr_stop_coverage" / "pending_requests"


def _dsn() -> str:
    return os.environ.get("DB_DSN", DEFAULT_DSN)


# ---------------------------------------------------------------------------
# Per-gap dependency status refinement.
# ---------------------------------------------------------------------------

def _refine_dependency_status(
    request_id: str,
    validated_summary: dict[str, Any],
    *,
    dsn: Optional[str] = None,
) -> dict[str, int]:
    """Walk the validator output and bump dr_batch_dependencies status.

    Buckets in ``validated_summary``:
        accepted          → landmark_found
        accept_uncertain  → landmark_found
        rejected          → landmark_not_found (if no other accepted entry
                            exists for the same route+gap)

    The validator records ``gap_number`` as 1-indexed; deps store
    ``gap_idx`` 0-indexed, so we always subtract 1.
    """
    dsn = dsn or _dsn()
    found_keys: set[tuple[str, int]] = set()
    for bucket in ("accepted", "accept_uncertain"):
        for r in (validated_summary.get(bucket) or []):
            found_keys.add((r["route_code"], int(r["gap_number"]) - 1))
    rejected_keys: set[tuple[str, int]] = set()
    for r in (validated_summary.get("rejected") or []):
        key = (r["route_code"], int(r["gap_number"]) - 1)
        # Only mark not_found if no accepted/uncertain landed for this gap.
        if key not in found_keys:
            rejected_keys.add(key)

    n_found = 0
    n_not_found = 0
    with psycopg2.connect(need_dsn(dsn)) as conn:
        with conn.cursor() as cur:
            for route_code, gap_idx in found_keys:
                cur.execute(
                    """
                    UPDATE route_prod.dr_batch_dependencies
                       SET status      = 'landmark_found',
                           resolved_at = NOW()
                     WHERE dr_batch_id = %s
                       AND route_id    = %s::uuid
                       AND gap_idx     = %s
                    """,
                    (request_id, route_code, int(gap_idx)),
                )
                n_found += cur.rowcount
            for route_code, gap_idx in rejected_keys:
                cur.execute(
                    """
                    UPDATE route_prod.dr_batch_dependencies
                       SET status      = 'landmark_not_found',
                           resolved_at = NOW()
                     WHERE dr_batch_id = %s
                       AND route_id    = %s::uuid
                       AND gap_idx     = %s
                    """,
                    (request_id, route_code, int(gap_idx)),
                )
                n_not_found += cur.rowcount
    return {
        "deps_marked_found": n_found,
        "deps_marked_not_found": n_not_found,
    }


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def process_response_and_reclassify(
    response_path: Path,
    *,
    skip_network: bool = False,
    dsn: Optional[str] = None,
) -> dict[str, Any]:
    """Run the full pipeline for one saved response file."""
    response_path = Path(response_path)
    if not response_path.exists():
        raise FileNotFoundError(response_path)
    request_id = response_path.stem  # batch_NN_locality OR <prefix>_NNN

    # 1. Parse.
    parse_report = import_batch_file(response_path)
    parsed_path = PARSED_DIR / response_path.with_suffix(".json").name

    # 2. Validate (writes validated/<stem>.json).
    val_summary = validate_batch(parsed_path, skip_network=skip_network)

    # 3. Refine per-gap dr_batch_dependencies status.
    dep_refinement = _refine_dependency_status(
        request_id, val_summary, dsn=dsn,
    )

    # 4. Reclassify everything that depended on this batch.
    rec = reclassify_routes_affected_by_batch(request_id, dsn=dsn)

    # 5. If this was an on-demand request, archive its prompt + instructions
    #    so the pending_requests/ dir reflects only outstanding work.
    archived = _archive_pending_request_files(request_id)

    return {
        "request_id": request_id,
        "parsed": {
            "ok": parse_report.parsed_ok,
            "failed": parse_report.parsed_failed,
            "total_blocks": parse_report.total_blocks,
            "corrupted": bool(parse_report.corrupted),
        },
        "validated": dict(val_summary.get("counts") or {}),
        "dependencies": dep_refinement,
        "reclassification": {
            "rows_examined": rec["rows_examined"],
            "upgrades": rec["upgrades"],
            "no_change": rec["no_change"],
        },
        "archived_pending_request": archived,
    }


def _archive_pending_request_files(request_id: str) -> Optional[str]:
    """Move the prompt + INSTRUCTIONS into a sibling ``processed/`` folder.

    Per skill 17 (2026-04-22), prompts live under
    ``pending_requests/<unit>/<request_id>.md``. This function scans both
    the unit subfolders AND the legacy flat root (for prompts generated
    before the convention change) and archives next to wherever it found
    the file — so ``pending_requests/<unit>/<request_id>.md`` →
    ``pending_requests/<unit>/processed/<request_id>.md``, and the
    legacy flat case stays self-contained too.

    No-op for ``batch_*`` legacy IDs (those live in ``queries/`` and are
    historical, not workflow inbox).
    """
    candidates: list[Path] = []
    if PENDING_REQUESTS_DIR.exists():
        candidates.extend(PENDING_REQUESTS_DIR.glob(f"*/{request_id}.md"))
        flat = PENDING_REQUESTS_DIR / f"{request_id}.md"
        if flat.exists():
            candidates.append(flat)
    md = next((p for p in candidates if "processed" not in p.parts), None)
    if md is None:
        return None
    processed_dir = md.parent / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / md.name
    md.replace(dest)
    instr = md.with_name(f"{request_id}.INSTRUCTIONS.md")
    if instr.exists():
        instr.replace(processed_dir / instr.name)
    return str(dest.relative_to(ROOT))


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dr_response_handler",
        description="Process a saved DR response: parse + validate + reclassify.",
    )
    parser.add_argument(
        "response_path",
        type=Path,
        help="Path to the response markdown (batch_*.md or <prefix>_NNN.md).",
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="Pass to the validator — useful for offline / replay runs.",
    )
    args = parser.parse_args(argv)

    result = process_response_and_reclassify(
        args.response_path, skip_network=args.skip_network,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
