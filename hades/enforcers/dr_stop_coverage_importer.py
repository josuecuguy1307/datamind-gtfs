"""Importer for DR Type 2 (Stop Coverage Gap Filling) responses.

Reads a human-pasted Claude.ai response from
``workspace/dr_stop_coverage/responses/batch_NN_<zone>.md`` and emits a
parser-strict JSON document at
``workspace/dr_stop_coverage/parsed/batch_NN_<zone>.json``.

The parser is **strict on structure, tolerant on drift**:

- LANDMARK_FOUND / LANDMARK_NOT_FOUND block markers must appear.
- Required fields inside LANDMARK_FOUND:
  ``QUERY_ID``, ``ROUTE_CODE``, ``GAP_NUMBER``, and at least one landmark
  with ``name`` + ``approx_lat`` + ``approx_lng``.
- Tolerant of: extra whitespace, blank lines, bold wrappers, triple-backtick
  fences, leading colons/dashes, comma decimals, degree-symbol noise,
  ``High`` / ``HIGH`` / ``Medium``, stray markdown headings between blocks.
- If more than ``CORRUPTION_THRESHOLD_PCT`` of queries fail to parse, the
  batch is flagged ``CORRUPTED`` and a diagnosis is written to
  ``workspace/dr_stop_coverage/parsed/_PARSE_ERRORS_batch_NN.md``.

No DB writes. No LLM calls. DR Type 1 parser lives elsewhere and must not
be re-used here.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[2]
RESPONSE_DIR = ROOT / "workspace" / "dr_stop_coverage" / "responses"
PARSED_DIR = ROOT / "workspace" / "dr_stop_coverage" / "parsed"

CORRUPTION_THRESHOLD_PCT = 10.0
MAX_LANDMARKS_PER_QUERY = 5

CONFIDENCE_MAP = {
    "high": "high",
    "medium": "medium",
    "low": "low",
    "med": "medium",
    "h": "high",
    "m": "medium",
    "l": "low",
}


@dataclass
class Landmark:
    name: str
    approx_lat: float
    approx_lng: float
    confidence: str = "medium"
    local_reference: Optional[str] = None
    source_description: Optional[str] = None


@dataclass
class ParsedQuery:
    query_id: str
    route_code: str
    gap_number: int
    found: bool
    landmarks: list[Landmark] = field(default_factory=list)
    reason: Optional[str] = None
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class ParseReport:
    batch_file: str
    total_blocks: int
    parsed_ok: int
    parsed_failed: int
    corrupted: bool
    queries: list[ParsedQuery] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


# ----------------------------------------------------------------------
# Normalisation helpers
# ----------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"-?\d+[\.,]?\d*")
_DEGREE_RE = re.compile(r"[°º]")


def _strip_decor(s: str) -> str:
    """Remove markdown/code-fence decoration around values."""
    s = s.strip()
    s = s.strip("`")
    s = s.strip()
    if s.startswith("**") and s.endswith("**") and len(s) >= 4:
        s = s[2:-2].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def _norm_confidence(raw: Optional[str]) -> str:
    if not raw:
        return "medium"
    key = _strip_decor(raw).lower()
    key = key.split()[0] if key else ""
    return CONFIDENCE_MAP.get(key, "medium")


def _parse_float(raw: str) -> Optional[float]:
    if raw is None:
        return None
    s = _DEGREE_RE.sub("", raw)
    s = _strip_decor(s)
    s = s.replace(" ", "")
    # Accept either "-0.123" or comma-decimal "-0,123" — but only if there
    # is exactly one comma used as a decimal separator.
    if s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    m = _NUM_RE.search(s)
    if m is None:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def _parse_int(raw: str) -> Optional[int]:
    f = _parse_float(raw)
    return int(f) if f is not None else None


# ----------------------------------------------------------------------
# Block-level parsing
# ----------------------------------------------------------------------

_BLOCK_START_RE = re.compile(
    r"^\s*#{0,6}\s*(?:LANDMARK_FOUND|LANDMARK_NOT_FOUND)\b",
    re.IGNORECASE,
)
_FOUND_MARKER_RE = re.compile(r"LANDMARK_FOUND", re.IGNORECASE)
_NOT_FOUND_MARKER_RE = re.compile(r"LANDMARK_NOT_FOUND", re.IGNORECASE)

_KV_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<key>[A-Za-z_][A-Za-z0-9_ ]*?)\s*[:=]\s*(?P<val>.+?)\s*$"
)
_LANDMARK_START_RE = re.compile(r"^\s*-\s+name\s*[:=]", re.IGNORECASE)


def _split_blocks(text: str) -> list[tuple[int, str]]:
    """Split text into (start_line_number, block_text) tuples, one per
    LANDMARK_FOUND / LANDMARK_NOT_FOUND block.
    """
    lines = text.splitlines()
    starts: list[int] = []
    for i, ln in enumerate(lines):
        if _BLOCK_START_RE.match(ln):
            starts.append(i)
    if not starts:
        return []
    starts.append(len(lines))
    blocks: list[tuple[int, str]] = []
    for a, b in zip(starts, starts[1:]):
        blocks.append((a + 1, "\n".join(lines[a:b])))
    return blocks


def _extract_top_level_fields(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_ln in block.splitlines():
        if _LANDMARK_START_RE.match(raw_ln):
            break  # reached landmarks list — stop scanning top-level fields
        m = _KV_RE.match(raw_ln)
        if not m:
            continue
        key = m.group("key").strip().lower().replace(" ", "_")
        val = _strip_decor(m.group("val"))
        if key in out:
            continue
        out[key] = val
    return out


def _extract_landmarks(block: str) -> tuple[list[dict[str, str]], list[str]]:
    """Return a list of per-landmark key/value dicts and any warnings."""
    warnings: list[str] = []
    lines = block.splitlines()
    landmarks: list[dict[str, str]] = []
    current: Optional[dict[str, str]] = None

    # Locate the LANDMARKS: header if present to anchor the list; if not,
    # still try to pick up "- name:" bullet starts anywhere below block head.
    for ln in lines:
        if _LANDMARK_START_RE.match(ln):
            if current is not None:
                landmarks.append(current)
            current = {}
            m = _KV_RE.match(ln.lstrip(" -\t*"))
            if m:
                key = m.group("key").strip().lower().replace(" ", "_")
                current[key] = _strip_decor(m.group("val"))
            continue
        if current is None:
            continue
        m = _KV_RE.match(ln)
        if not m:
            continue
        key = m.group("key").strip().lower().replace(" ", "_")
        val = _strip_decor(m.group("val"))
        if key in {
            "query_id",
            "route_code",
            "gap_number",
            "found",
            "reason",
            "fallback",
        }:
            # New outer section — landmarks list ended.
            break
        current[key] = val
    if current is not None:
        landmarks.append(current)

    if len(landmarks) > MAX_LANDMARKS_PER_QUERY:
        warnings.append(
            f"truncated to {MAX_LANDMARKS_PER_QUERY} landmarks "
            f"(block had {len(landmarks)})"
        )
        landmarks = landmarks[:MAX_LANDMARKS_PER_QUERY]
    return landmarks, warnings


def _parse_block(block_idx: int, block: str) -> tuple[Optional[ParsedQuery], Optional[dict[str, Any]]]:
    is_not_found = bool(_NOT_FOUND_MARKER_RE.search(block.splitlines()[0]))
    is_found = (not is_not_found) and bool(_FOUND_MARKER_RE.search(block.splitlines()[0]))
    if not (is_found or is_not_found):
        return None, {"block_idx": block_idx, "reason": "unknown_block_type"}

    top = _extract_top_level_fields(block)
    qid = top.get("query_id")
    route_code = top.get("route_code")
    gap_num_raw = top.get("gap_number")
    if not qid or not route_code or gap_num_raw is None:
        return None, {
            "block_idx": block_idx,
            "reason": "missing_required_header",
            "missing": [
                k for k, v in [("query_id", qid), ("route_code", route_code),
                               ("gap_number", gap_num_raw)] if not v
            ],
        }
    gap_num = _parse_int(gap_num_raw)
    if gap_num is None:
        return None, {"block_idx": block_idx, "reason": "gap_number_not_numeric", "raw": gap_num_raw}

    if is_not_found:
        reason = top.get("reason") or "unspecified"
        return (
            ParsedQuery(
                query_id=qid.upper(),
                route_code=route_code,
                gap_number=gap_num,
                found=False,
                reason=reason,
            ),
            None,
        )

    lm_records, lm_warnings = _extract_landmarks(block)
    landmarks: list[Landmark] = []
    warnings = list(lm_warnings)
    for lm_idx, lm in enumerate(lm_records):
        name = lm.get("name")
        lat = _parse_float(lm.get("approx_lat", "")) if lm.get("approx_lat") is not None else None
        # Accept both `approx_lng` and `approx_lon`.
        lng_raw = lm.get("approx_lng") or lm.get("approx_lon")
        lng = _parse_float(lng_raw) if lng_raw is not None else None
        if not name or lat is None or lng is None:
            warnings.append(f"landmark[{lm_idx}] missing required fields, skipped")
            continue
        landmarks.append(
            Landmark(
                name=name,
                approx_lat=lat,
                approx_lng=lng,
                confidence=_norm_confidence(lm.get("confidence")),
                local_reference=lm.get("local_reference"),
                source_description=lm.get("source_description"),
            )
        )
    if not landmarks:
        return None, {
            "block_idx": block_idx,
            "reason": "found_block_with_no_usable_landmarks",
            "raw_landmark_count": len(lm_records),
        }
    return (
        ParsedQuery(
            query_id=qid.upper(),
            route_code=route_code,
            gap_number=gap_num,
            found=True,
            landmarks=landmarks,
            parse_warnings=warnings,
        ),
        None,
    )


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def parse_batch_text(text: str, batch_file: str = "<stdin>") -> ParseReport:
    blocks = _split_blocks(text)
    report = ParseReport(
        batch_file=batch_file,
        total_blocks=len(blocks),
        parsed_ok=0,
        parsed_failed=0,
        corrupted=False,
    )
    for i, (_start_line, block) in enumerate(blocks):
        parsed, fail = _parse_block(i, block)
        if parsed is not None:
            report.queries.append(parsed)
            report.parsed_ok += 1
        else:
            report.parsed_failed += 1
            if fail is not None:
                report.failures.append(fail)
    if report.total_blocks > 0:
        fail_pct = 100.0 * report.parsed_failed / report.total_blocks
        report.corrupted = fail_pct > CORRUPTION_THRESHOLD_PCT
    return report


def _serialise_report(report: ParseReport) -> dict[str, Any]:
    return {
        "schema": "dr_stop_coverage_importer/v1",
        "batch_file": report.batch_file,
        "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_blocks": report.total_blocks,
        "parsed_ok": report.parsed_ok,
        "parsed_failed": report.parsed_failed,
        "corrupted": report.corrupted,
        "corruption_threshold_pct": CORRUPTION_THRESHOLD_PCT,
        "queries": [
            {
                "query_id": q.query_id,
                "route_code": q.route_code,
                "gap_number": q.gap_number,
                "found": q.found,
                "reason": q.reason,
                "parse_warnings": q.parse_warnings,
                "landmarks": [
                    {
                        "name": lm.name,
                        "approx_lat": lm.approx_lat,
                        "approx_lng": lm.approx_lng,
                        "confidence": lm.confidence,
                        "local_reference": lm.local_reference,
                        "source_description": lm.source_description,
                    }
                    for lm in q.landmarks
                ],
            }
            for q in report.queries
        ],
        "failures": report.failures,
    }


def _write_parse_errors(report: ParseReport, out_dir: Path, stem: str) -> None:
    if not report.failures:
        return
    errs_path = out_dir / f"_PARSE_ERRORS_{stem}.md"
    lines = [
        f"# Parse errors for {report.batch_file}",
        "",
        f"- Total blocks: {report.total_blocks}",
        f"- Parsed OK: {report.parsed_ok}",
        f"- Parsed failed: {report.parsed_failed}",
        f"- Corrupted: {report.corrupted}",
        "",
        "## Per-block failures",
        "",
    ]
    for f in report.failures:
        lines.append(f"- block #{f.get('block_idx')}: {f.get('reason')}")
        for k, v in f.items():
            if k in {"block_idx", "reason"}:
                continue
            lines.append(f"    - {k}: {v}")
    errs_path.write_text("\n".join(lines) + "\n")


def import_batch_file(response_path: Path) -> ParseReport:
    if not response_path.exists():
        raise FileNotFoundError(response_path)
    text = response_path.read_text()
    report = parse_batch_text(text, batch_file=response_path.name)
    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PARSED_DIR / response_path.with_suffix(".json").name
    out_path.write_text(json.dumps(_serialise_report(report), indent=2))
    stem = response_path.stem
    _write_parse_errors(report, PARSED_DIR, stem)
    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="DR Type 2 importer")
    ap.add_argument(
        "paths",
        nargs="*",
        help="Response markdown files. Defaults to all .md in responses/.",
    )
    args = ap.parse_args()

    if args.paths:
        targets = [Path(p) for p in args.paths]
    else:
        RESPONSE_DIR.mkdir(parents=True, exist_ok=True)
        targets = sorted(RESPONSE_DIR.glob("batch_*.md"))
    if not targets:
        print(f"[importer] no response files found in {RESPONSE_DIR}")
        return 0

    any_corrupted = False
    for path in targets:
        try:
            rep = import_batch_file(path)
        except FileNotFoundError:
            print(f"[importer] MISSING: {path}", file=sys.stderr)
            continue
        flag = "CORRUPTED" if rep.corrupted else "ok"
        print(
            f"[importer] {path.name}: {rep.parsed_ok}/{rep.total_blocks} parsed, "
            f"{rep.parsed_failed} failed — {flag}"
        )
        if rep.corrupted:
            any_corrupted = True
    return 2 if any_corrupted else 0


if __name__ == "__main__":
    sys.exit(main())
