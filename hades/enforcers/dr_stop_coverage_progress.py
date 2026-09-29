"""Regenerate ``workspace/dr_stop_coverage/_PROGRESS.md`` and ``_INDEX.md``.

Walks the DR Type 2 directory, joins the export manifest with the state
of ``responses/``, ``parsed/``, ``validated/``, and rewrites two operator-
facing markdown files.

Read-only on the filesystem except for the two output files. No DB calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "workspace" / "dr_stop_coverage"
MANIFEST = BASE / "_export_manifest.json"
QUERIES = BASE / "queries"
RESPONSES = BASE / "responses"
PARSED = BASE / "parsed"
VALIDATED = BASE / "validated"
PROGRESS = BASE / "_PROGRESS.md"
INDEX = BASE / "_INDEX.md"


def _batch_stem(batch_number: int) -> str:
    return f"batch_{batch_number:02d}"


def _match_file(folder: Path, stem: str, suffix: str) -> Path | None:
    if not folder.exists():
        return None
    for p in folder.glob(f"{stem}_*{suffix}"):
        return p
    # Also match exact stem (pilot doesn't carry a zone suffix in its name).
    exact = folder / f"{stem}{suffix}"
    return exact if exact.exists() else None


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def _row_state(batch_stem: str) -> dict[str, Any]:
    response = _match_file(RESPONSES, batch_stem, ".md")
    parsed = _match_file(PARSED, batch_stem, ".json")
    validated = _match_file(VALIDATED, batch_stem, ".json")

    parse_info = _load_json(parsed) if parsed else None
    val_info = _load_json(validated) if validated else None

    state: str
    if not response:
        state = "PENDING"
    elif not parsed:
        state = "RESPONDED"
    elif parse_info and parse_info.get("corrupted"):
        state = "CORRUPTED"
    elif not validated:
        state = "PARSED"
    else:
        state = "VALIDATED"

    counts = (val_info or {}).get("counts", {})
    return {
        "state": state,
        "response_present": bool(response),
        "parsed_ok": (parse_info or {}).get("parsed_ok"),
        "parsed_total": (parse_info or {}).get("total_blocks"),
        "parsed_failed": (parse_info or {}).get("parsed_failed"),
        "parsed_corrupted": (parse_info or {}).get("corrupted"),
        "accepted": counts.get("accepted"),
        "accept_uncertain": counts.get("accept_uncertain"),
        "rejected": counts.get("rejected"),
    }


def _render_progress(manifest: dict[str, Any]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# DR Type 2 — Stop Coverage Gap Filling progress")
    lines.append("")
    lines.append(f"_Auto-regenerated: {ts}_  ")
    lines.append(
        f"Clustering method: **{manifest.get('plan_method')}**, "
        f"k={manifest.get('plan_k')}, "
        f"production queries={manifest.get('total_queries')}, "
        f"pilot queries={manifest.get('pilot_query_count')}"
    )
    lines.append("")
    lines.append("## Pipeline stages")
    lines.append("")
    lines.append(
        "`PENDING` = queries/ exists, no response yet · "
        "`RESPONDED` = response pasted, not yet parsed · "
        "`PARSED` = importer ran OK · "
        "`CORRUPTED` = importer flagged >10% parse failures · "
        "`VALIDATED` = validator ran and produced accepted/uncertain/rejected buckets"
    )
    lines.append("")

    # Pilot row first.
    pilot_state = _row_state("batch_00")
    # Pilot rows aggregate.
    lines.append("## Pilot")
    lines.append("")
    lines.append("| Batch | State | Parsed | Accept | Uncertain | Reject |")
    lines.append("|---|---|---|---:|---:|---:|")
    lines.append(_format_row("batch_00_pilot", pilot_state, manifest.get("pilot_query_count")))
    lines.append("")

    # Production.
    lines.append("## Production batches")
    lines.append("")
    lines.append("| Batch | State | Queries | Parsed | Accept | Uncertain | Reject |")
    lines.append("|---|---|---:|---|---:|---:|---:|")
    total_accept = 0
    total_uncertain = 0
    total_reject = 0
    for b in manifest.get("batches", []):
        stem = _batch_stem(int(b["batch_number"]))
        row = _row_state(stem)
        total_accept += row.get("accepted") or 0
        total_uncertain += row.get("accept_uncertain") or 0
        total_reject += row.get("rejected") or 0
        lines.append(_format_row_prod(b, row))
    lines.append("")
    lines.append(
        f"**Totals:** accepted={total_accept}, "
        f"uncertain={total_uncertain}, rejected={total_reject}"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def _format_row(name: str, state: dict[str, Any], query_count: Any) -> str:
    parsed = "—"
    if state.get("parsed_total") is not None:
        corr = "⚠ " if state.get("parsed_corrupted") else ""
        parsed = f"{corr}{state.get('parsed_ok')}/{state.get('parsed_total')}"
    return (
        f"| {name} | {state['state']} | {parsed} | "
        f"{state.get('accepted') or '—'} | "
        f"{state.get('accept_uncertain') or '—'} | "
        f"{state.get('rejected') or '—'} |"
    )


def _format_row_prod(manifest_entry: dict[str, Any], state: dict[str, Any]) -> str:
    stem = manifest_entry["file"].replace(".md", "")
    parsed = "—"
    if state.get("parsed_total") is not None:
        corr = "⚠ " if state.get("parsed_corrupted") else ""
        parsed = f"{corr}{state.get('parsed_ok')}/{state.get('parsed_total')}"
    return (
        f"| {stem} | {state['state']} | {manifest_entry.get('query_count')} | "
        f"{parsed} | "
        f"{state.get('accepted') or '—'} | "
        f"{state.get('accept_uncertain') or '—'} | "
        f"{state.get('rejected') or '—'} |"
    )


def _render_index(manifest: dict[str, Any]) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append("# DR Type 2 — batch index")
    lines.append("")
    lines.append(f"_Auto-regenerated: {ts}_")
    lines.append("")
    lines.append("## Files")
    lines.append("")
    lines.append(
        "- [`_README.md`](./_README.md) — pipeline overview · "
        "[`_PROGRESS.md`](./_PROGRESS.md) — operator-facing state"
    )
    lines.append(
        "- [`_clustering_plan.json`](./_clustering_plan.json) · "
        "[`_export_manifest.json`](./_export_manifest.json) · "
        "[`_VALIDATION_LOG.jsonl`](./_VALIDATION_LOG.jsonl)"
    )
    lines.append("")
    lines.append("## Production batches")
    lines.append("")
    for b in manifest.get("batches", []):
        fname = b["file"]
        lines.append(
            f"- **batch {b['batch_number']:02d}** — "
            f"[{fname}](./queries/{fname}) — "
            f"{b['query_count']} queries "
            f"(cluster_id={b['cluster_id']}, seed={b.get('matched_seed')})"
        )
    lines.append("")
    lines.append("## Pilot")
    lines.append("")
    lines.append(
        "- [batch_00_pilot.md](./queries/batch_00_pilot.md) — 15 curated queries"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    if not MANIFEST.exists():
        print(f"[progress] manifest not found at {MANIFEST}; run the exporter first.")
        return 1
    manifest = json.loads(MANIFEST.read_text())
    PROGRESS.write_text(_render_progress(manifest))
    INDEX.write_text(_render_index(manifest))
    print(f"[progress] wrote {PROGRESS.relative_to(ROOT)} and {INDEX.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
