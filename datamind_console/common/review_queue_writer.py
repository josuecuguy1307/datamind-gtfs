"""
Writer for ``workspace/research_queue/synthetic_review/``.

Every synthesis event that produces a persistent node (stages 3a–3d, 4,
and osm_route_fill) emits a review markdown file into this queue. The
filename and frontmatter schema are fixed by
``workspace/skills/hades-path-aware-synthesis.md §9``.

**Filename convention**::

    {priority}_{stage}_{unit}_{route_code}_{anchor_slug}_{timestamp}.md

where the priority slot bucket-sorts the queue:
    00 → semantic_spatial_conflict (always highest, regardless of stage)
    01 → stage 4 (pure synthesis)
    02 → stage 3d (research_coords_path_snapped)
    03 → stages 3a / 3b / 3c / osm_route_fill (skim-review bucket)

**Dual-column source schema** (migration 013). The frontmatter records both:
    - ``source_type``  — the enum value from node_prod.nodes.source_type
    - ``source``       — the free-text audit string (caller-constructed)
The caller (Phase 2 synthesis core) is responsible for both values; this
module writes what it is given and validates ``source_type`` against the
8-value enum.
"""
from __future__ import annotations

import os
import re
import tempfile
import unicodedata
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from datamind_console.common.text_utils import slugify as _slugify

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

PENDING = "pending"
VERIFIED = "verified"
REJECTED = "rejected"
CONFLICTS = "semantic_spatial_conflicts"

VALID_STAGES: frozenset[str] = frozenset({
    "3a_poi_on_path",
    "3b_path_corridor",
    "3c_path_intersection",
    "3d_research_coords_snapped",
    "4_pure_synthesis",
    "osm_route_fill",
})

VALID_SOURCE_TYPES: frozenset[str] = frozenset({
    "osm",
    "backfill",
    "poi_anchored_path_projected",
    "path_corridor_projected",
    "path_intersection",
    "research_coords_path_snapped",
    "pure_synthesis",
    "gps_trace",
})

_STAGE_WEIGHTS: dict[str, float] = {
    "3a_poi_on_path": 0.0,
    "3b_path_corridor": 0.0,
    "3c_path_intersection": 0.0,
    "3d_research_coords_snapped": 0.5,
    "4_pure_synthesis": 1.0,
    "osm_route_fill": 0.0,
}

_STAGE_PRIORITY: dict[str, str] = {
    "3a_poi_on_path": "03",
    "3b_path_corridor": "03",
    "3c_path_intersection": "03",
    "osm_route_fill": "03",
    "3d_research_coords_snapped": "02",
    "4_pure_synthesis": "01",
}


# ---------------------------------------------------------------------------
# public helpers
# ---------------------------------------------------------------------------


def slugify(s: str, *, max_len: int = 64) -> str:
    """Thin re-export of text_utils.slugify so this module is self-contained
    for callers that only need the slug helper (e.g. in tests)."""
    return _slugify(s, max_len=max_len)


def compute_priority(*, stage: str, semantic_spatial_conflict: bool) -> str:
    if stage not in VALID_STAGES:
        raise ValueError(f"unknown stage: {stage!r}")
    if semantic_spatial_conflict:
        return "00"
    return _STAGE_PRIORITY[stage]


_SLOT_CLEAN_RE = re.compile(r"[^a-z0-9_\-]+")


def _slot_slug(s: str, *, max_len: int = 64) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.strip().lower()
    s = _SLOT_CLEAN_RE.sub("-", s).strip("-_")
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip("-_")
    return s


def compute_filename(
    *,
    priority: str,
    stage: str,
    unit: str,
    route_code: str,
    anchor_slug: str,
    timestamp: str,
) -> str:
    u = _slot_slug(unit, max_len=48)
    r = _slot_slug(route_code, max_len=32)
    a = _slot_slug(anchor_slug, max_len=48)
    return f"{priority}_{stage}_{u}_{r}_{a}_{timestamp}.md"


# ---------------------------------------------------------------------------
# validators
# ---------------------------------------------------------------------------


def _require_uuid(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UUID string, got {type(value).__name__}")
    try:
        _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError(f"{name} is not a valid UUID: {value!r}") from e
    return value


def _require_synthetic_osm_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"osm_id must be an integer, got {value!r}")
    if value > -1000000:
        raise ValueError(
            f"osm_id must be in the synthetic range (<= -1000000), got {value}"
        )
    return value


def _require_coords(name: str, value: Any) -> tuple[float, float]:
    if value is None or not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{name} must be a (lat, lon) pair, got {value!r}")
    return (float(value[0]), float(value[1]))


def _coords_or_none(name: str, value: Any) -> Optional[tuple[float, float]]:
    if value is None:
        return None
    return _require_coords(name, value)


def _validate_stage_weight(*, stage: str, cap_weight_consumed: float) -> float:
    expected = _STAGE_WEIGHTS[stage]
    if abs(cap_weight_consumed - expected) > 1e-9:
        raise ValueError(
            f"cap_weight_consumed={cap_weight_consumed} does not match "
            f"expected weight {expected} for stage {stage!r}"
        )
    return cap_weight_consumed


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _format_coords(v: Optional[tuple[float, float]]) -> str:
    if v is None:
        return "null"
    return f"[{v[0]}, {v[1]}]"


def _yaml_str(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or any(c in s for c in ':#[]{}&*!|>"\'%@`,'):
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return s


def _find_existing_by_node_id_and_stage(
    review_root: Path, node_id: str, stage: str
) -> Optional[Path]:
    for sub in (PENDING, CONFLICTS, VERIFIED, REJECTED):
        d = review_root / sub
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if f"node_id: {node_id}" in text and f"stage: {stage}" in text:
                return p
    return None


def write_synthetic_review(
    *,
    review_root: Path,
    node_id: str,
    osm_id: int,
    stage: str,
    source_type: str,
    source: str,
    unit: str,
    province: str,
    route_code: str,
    route_id: str,
    anchor_name: str,
    synthetic_confidence: str,
    final_coords: tuple[float, float],
    cap_weight_consumed: float,
    triggered_by_skill: str,
    research_output_file: Optional[str],
    evidence_markdown: str,
    why_synthesis_fired: str,
    anchor_slug: Optional[str] = None,
    research_coords: Optional[tuple[float, float]] = None,
    poi_osm_url: Optional[str] = None,
    matched_road: Optional[str] = None,
    poi_to_path_distance_m: Optional[float] = None,
    path_projection_distance_m: Optional[float] = None,
    research_to_projection_distance_m: Optional[float] = None,
    polyline_url: Optional[str] = None,
    polyline_source: Optional[str] = None,
    semantic_spatial_conflict: bool = False,
    osm_route_fill_context: Optional[str] = None,
    synthetic_review_state: str = "pending",
    now: Optional[datetime] = None,
) -> Path:
    """Write one synthetic-node review markdown file.

    Idempotent: if a file already exists for this (node_id, stage) pair
    across any of the four review subfolders, return that path without
    writing. This is the review-queue analogue of the research_queue dedup
    check.
    """
    review_root = Path(review_root)

    if stage not in VALID_STAGES:
        raise ValueError(f"unknown stage: {stage!r}")
    if source_type not in VALID_SOURCE_TYPES:
        raise ValueError(
            f"source_type {source_type!r} not in synthesis enum "
            f"{sorted(VALID_SOURCE_TYPES)}"
        )
    if synthetic_confidence not in {"low", "medium", "high"}:
        raise ValueError(f"synthetic_confidence must be low|medium|high")

    _require_uuid("node_id", node_id)
    _require_uuid("route_id", route_id)
    _require_synthetic_osm_id(osm_id)
    fc = _require_coords("final_coords", final_coords)
    rc = _coords_or_none("research_coords", research_coords)
    _validate_stage_weight(stage=stage, cap_weight_consumed=cap_weight_consumed)

    existing = _find_existing_by_node_id_and_stage(review_root, node_id, stage)
    if existing is not None:
        return existing

    now = now or datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d-%H%M")
    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if anchor_slug is None:
        anchor_slug = _slugify(anchor_name, max_len=48) or "anchor"

    priority = compute_priority(
        stage=stage, semantic_spatial_conflict=semantic_spatial_conflict
    )
    filename = compute_filename(
        priority=priority,
        stage=stage,
        unit=unit,
        route_code=route_code,
        anchor_slug=anchor_slug,
        timestamp=timestamp,
    )
    bucket = CONFLICTS if semantic_spatial_conflict else PENDING
    target = review_root / bucket / filename

    frontmatter_lines = [
        "---",
        f"node_id: {node_id}",
        f"osm_id: {osm_id}",
        f"stage: {stage}",
        f"source_type: {source_type}",
        f"source: {_yaml_str(source)}",
        f"unit: {_yaml_str(unit)}",
        f"province: {_yaml_str(province)}",
        f"route_code: {_yaml_str(route_code)}",
        f"route_id: {route_id}",
        f"anchor_name: {_yaml_str(anchor_name)}",
        f"synthetic_confidence: {synthetic_confidence}",
        f"synthetic_review_state: {synthetic_review_state}",
        f"final_coords: {_format_coords(fc)}",
        f"research_coords: {_format_coords(rc)}",
        f"poi_osm_url: {_yaml_str(poi_osm_url)}",
        f"matched_road: {_yaml_str(matched_road)}",
        f"poi_to_path_distance_m: {_yaml_str(poi_to_path_distance_m)}",
        f"path_projection_distance_m: {_yaml_str(path_projection_distance_m)}",
        (
            "research_to_projection_distance_m: "
            f"{_yaml_str(research_to_projection_distance_m)}"
        ),
        f"polyline_url: {_yaml_str(polyline_url)}",
        f"polyline_source: {_yaml_str(polyline_source)}",
        f"semantic_spatial_conflict: "
        f"{'true' if semantic_spatial_conflict else 'false'}",
        f"osm_route_fill_context: {_yaml_str(osm_route_fill_context)}",
        f"cap_weight_consumed: {cap_weight_consumed}",
        f"triggered_by_skill: {_yaml_str(triggered_by_skill)}",
        (
            "research_output_file: "
            f"{_yaml_str(os.path.basename(research_output_file) if research_output_file else None)}"
        ),
        f"created_at: {created_at}",
        "---",
    ]

    body = (
        "\n## Anchor\n"
        f"{anchor_name}\n"
        "\n## Why synthesis fired\n"
        f"{why_synthesis_fired}\n"
        "\n## Evidence\n"
        f"{evidence_markdown}\n"
        "\n## Operator decision\n"
        "- [ ] verified  → mv to verified/\n"
        "- [ ] rejected  → mv to rejected/ and write rejected_reason\n"
        "- [ ] escalate → semantic_spatial_conflicts/ "
        "(for stages where the automated check missed a conflict)\n"
    )

    text = "\n".join(frontmatter_lines) + "\n" + body
    _atomic_write_text(target, text)
    return target


# ---------------------------------------------------------------------------
# move helpers
# ---------------------------------------------------------------------------


def _inject_or_replace_field(text: str, key: str, value: str) -> str:
    if not text.startswith("---\n"):
        raise ValueError("file missing frontmatter fence")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("frontmatter not closed")
    fm = text[4:end].splitlines()
    new_line = f"{key}: {value}"
    out = []
    replaced = False
    for ln in fm:
        stripped = ln.lstrip()
        if stripped.startswith(f"{key}:"):
            out.append(new_line)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(new_line)
    return "---\n" + "\n".join(out) + "\n---" + text[end + 4 :]


def _mv_between_review_folders(
    *, review_root: Path, filename: str, dst_sub: str, extra_fields: dict
) -> Path:
    review_root = Path(review_root)
    # look up source in any folder except the target
    src: Optional[Path] = None
    for sub in (PENDING, CONFLICTS, VERIFIED, REJECTED):
        if sub == dst_sub:
            continue
        candidate = review_root / sub / filename
        if candidate.exists():
            src = candidate
            break
    if src is None:
        raise FileNotFoundError(
            f"review file {filename!r} not found in any source folder"
        )
    text = src.read_text(encoding="utf-8")
    for k, v in extra_fields.items():
        text = _inject_or_replace_field(text, k, v)
    dst = review_root / dst_sub / filename
    _atomic_write_text(dst, text)
    src.unlink()
    return dst


def mv_to_verified(*, review_root: Path, filename: str) -> Path:
    return _mv_between_review_folders(
        review_root=review_root,
        filename=filename,
        dst_sub=VERIFIED,
        extra_fields={"synthetic_review_state": "verified"},
    )


def mv_to_rejected(
    *, review_root: Path, filename: str, rejected_reason: str
) -> Path:
    return _mv_between_review_folders(
        review_root=review_root,
        filename=filename,
        dst_sub=REJECTED,
        extra_fields={
            "synthetic_review_state": "rejected",
            "rejected_reason": _yaml_str(rejected_reason),
        },
    )


def mv_to_conflicts(*, review_root: Path, filename: str) -> Path:
    return _mv_between_review_folders(
        review_root=review_root,
        filename=filename,
        dst_sub=CONFLICTS,
        extra_fields={"semantic_spatial_conflict": "true"},
    )
