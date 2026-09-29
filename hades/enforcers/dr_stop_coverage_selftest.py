"""Self-test for the DR Type 2 pipeline.

Generates a synthetic Claude.ai response covering the batch_00 pilot,
then runs the full round-trip:

    importer  → validator(--skip-network)  → progress regen

Artefacts land in a sandboxed ``_selftest/`` subtree so the real
``responses/`` / ``parsed/`` / ``validated/`` trees stay clean. No
network calls. No DB access. No writes to ``route_prod``.

Run with:
    python -m hades.enforcers.dr_stop_coverage_selftest
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hades.enforcers import (
    dr_stop_coverage_importer as importer_mod,
    dr_stop_coverage_validator as validator_mod,
)


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "workspace" / "dr_stop_coverage"
PILOT_FILE = BASE / "queries" / "batch_00_pilot.md"
SELFTEST_DIR = BASE / "_selftest"
LOG_PATH = ROOT / "workspace" / "dr_infrastructure_log.md"


_Q_HEADER = re.compile(r"^### (Q\d{3})\s*$")
_ROUTE_CODE = re.compile(r"-\s*Route code:\s*(\S+)")
_GAP_NUM = re.compile(r"-\s*Gap number in this route:\s*(\d+)")
_MID = re.compile(r"-\s*Gap midpoint:\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)")


def _parse_pilot_queries(text: str) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    lines = text.splitlines()
    in_queries_section = False
    for ln in lines:
        if ln.strip() == "## Queries":
            in_queries_section = True
            continue
        if not in_queries_section:
            continue
        m = _Q_HEADER.match(ln)
        if m:
            if current:
                queries.append(current)
            current = {"qid": m.group(1)}
            continue
        if not current:
            continue
        m = _ROUTE_CODE.search(ln)
        if m:
            current["route_code"] = m.group(1)
            continue
        m = _GAP_NUM.search(ln)
        if m:
            current["gap_number"] = int(m.group(1))
            continue
        m = _MID.search(ln)
        if m:
            current["lat"] = float(m.group(1))
            current["lon"] = float(m.group(2))
    if current:
        queries.append(current)
    return queries


def _synth_response(queries: list[dict[str, Any]]) -> str:
    """Emit a synthetic, parser-strict response exercising both
    LANDMARK_FOUND and LANDMARK_NOT_FOUND paths, plus mild drift cases.
    """
    parts: list[str] = []
    for i, q in enumerate(queries):
        if i % 5 == 2:
            # Every 3rd-of-5 is a LANDMARK_NOT_FOUND exercise.
            parts.append(
                "### LANDMARK_NOT_FOUND\n"
                f"    QUERY_ID: {q['qid']}\n"
                f"    ROUTE_CODE: {q['route_code']}\n"
                f"    GAP_NUMBER: {q['gap_number']}\n"
                "    FOUND: false\n"
                "    reason: \"no data for synthetic self-test\"\n"
                "    fallback: use_synthetic\n"
            )
            continue
        # Nudge the coords slightly off the midpoint so haversine > 0 but
        # well under the sanity threshold (< 500 m).
        lat = q["lat"] + 0.0002 * (1 if i % 2 == 0 else -1)
        lon = q["lon"] + 0.0002
        conf = "high" if i % 3 == 0 else ("Medium" if i % 3 == 1 else "low")
        # Include a drift case: comma decimals on the second landmark.
        second_lat = q["lat"] - 0.0001
        second_lon = q["lon"] - 0.0001
        second_lat_str = f"{second_lat:.4f}".replace(".", ",")
        second_lon_str = f"{second_lon:.4f}".replace(".", ",")
        parts.append(
            "### LANDMARK_FOUND\n"
            f"    QUERY_ID: {q['qid']}\n"
            f"    ROUTE_CODE: {q['route_code']}\n"
            f"    GAP_NUMBER: {q['gap_number']}\n"
            "    LANDMARKS:\n"
            f"      - name: \"Synthetic Parada {q['qid']}\"\n"
            f"        local_reference: \"selftest_ref_{i}\"\n"
            f"        approx_lat: {lat:.6f}\n"
            f"        approx_lng: {lon:.6f}\n"
            f"        confidence: {conf}\n"
            f"        source_description: \"self-test synthetic\"\n"
            f"      - name: \"Secondary Landmark {q['qid']}\"\n"
            f"        approx_lat: {second_lat_str}\n"
            f"        approx_lng: {second_lon_str}\n"
            f"        confidence: HIGH\n"
        )
    return "\n".join(parts)


def _write_selftest_response(text: str) -> Path:
    SELFTEST_DIR.mkdir(parents=True, exist_ok=True)
    response_path = SELFTEST_DIR / "batch_00_pilot.md"
    response_path.write_text(text)
    return response_path


def _write_log(payload: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = LOG_PATH.read_text() if LOG_PATH.exists() else ""
    block = [
        "",
        f"## DR Type 2 self-test — {ts}",
        "",
        f"- Pilot queries processed: {payload['pilot_query_count']}",
        f"- Synthetic response blocks emitted: {payload['blocks_emitted']}",
        f"- Importer parsed OK: {payload['parsed_ok']}/{payload['total_blocks']}",
        f"- Importer flagged corrupted: {payload['corrupted']}",
        f"- Validator accepted: {payload['accepted']}",
        f"- Validator accept_uncertain: {payload['accept_uncertain']}",
        f"- Validator rejected: {payload['rejected']}",
        f"- Skip network: {payload['skip_network']}",
        f"- Artefacts: `{payload['artefacts_root']}`",
        "",
    ]
    LOG_PATH.write_text(existing + "\n".join(block))


def main() -> int:
    if not PILOT_FILE.exists():
        print(f"[selftest] pilot file missing at {PILOT_FILE}", file=sys.stderr)
        return 1
    queries = _parse_pilot_queries(PILOT_FILE.read_text())
    if not queries:
        print("[selftest] no queries parsed from pilot", file=sys.stderr)
        return 1

    synth = _synth_response(queries)
    response_path = _write_selftest_response(synth)
    blocks_emitted = synth.count("### LANDMARK_")

    # 1. Import — route artefacts into the sandbox, not PARSED_DIR.
    SELFTEST_DIR.mkdir(parents=True, exist_ok=True)
    importer_mod.PARSED_DIR = SELFTEST_DIR  # type: ignore[attr-defined]
    report = importer_mod.import_batch_file(response_path)

    # 2. Validate — sandbox the validator too, and skip network to keep
    # the self-test hermetic.
    validator_mod.VALIDATED_DIR = SELFTEST_DIR / "validated"  # type: ignore[attr-defined]
    validator_mod.VALIDATION_LOG = SELFTEST_DIR / "_VALIDATION_LOG.jsonl"  # type: ignore[attr-defined]
    parsed_json = SELFTEST_DIR / "batch_00_pilot.json"
    summary = validator_mod.validate_batch(parsed_json, skip_network=True)

    payload = {
        "pilot_query_count": len(queries),
        "blocks_emitted": blocks_emitted,
        "parsed_ok": report.parsed_ok,
        "total_blocks": report.total_blocks,
        "corrupted": report.corrupted,
        "accepted": summary["counts"]["accepted"],
        "accept_uncertain": summary["counts"]["accept_uncertain"],
        "rejected": summary["counts"]["rejected"],
        "skip_network": True,
        "artefacts_root": str(SELFTEST_DIR.relative_to(ROOT)),
    }
    _write_log(payload)
    print(json.dumps(payload, indent=2))
    fail = bool(report.corrupted) or report.parsed_ok < report.total_blocks
    return 2 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
