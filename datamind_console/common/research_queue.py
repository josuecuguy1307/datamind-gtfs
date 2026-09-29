"""
Research prompt queue — filesystem-as-queue helper.

Implements the contract described in workspace/skills/research_prompt_queue.md.
Every research-emitting skill writes prompts here; operator moves them through
states via plain ``mv``.  This module is the only sanctioned write path.

Folder layout (under ``queue_root``):
    pending/    prompts waiting to be sent to Deep Research
    sent/       operator-moved here after pasting into Deep Research
    responses/  JSON responses, awaiting ingestion
    ingested/   paired prompts + responses after successful merge
    archive/    deprecated / superseded / orphaned artifacts
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

PENDING = "pending"
SENT = "sent"
RESPONSES = "responses"
INGESTED = "ingested"
ARCHIVE = "archive"

_VALID_PROMPT_TYPES = {
    "exhaustive_route_inventory",
    "exhaustive_route_inventory_rerun",
    "schedules_operations",
    "stop_grounding_detail",
    "enrichment_cycle",
    # Calibration-only: a single prompt scoped to one canary unit that
    # re-enriches its existing routes with stage-3b/3c/3d-friendly anchor
    # payloads. Not a coverage-closure prompt — see 07_INGESTION_CONTRACT.md
    # Variant F for the response schema.
    "canary_synthesis_exercise",
}

_VALID_BUDGETS = {"standard", "heavy"}

_EXTRA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_JSON_SCALAR_TYPES = (str, int, float, bool)


def _validate_extra_frontmatter(extra: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and return a shallow copy of ``extra_frontmatter``.

    Keys must be lowercase snake_case. Values must be JSON-serializable:
    str, int, float, bool, None, list (of same), or dict (of same).
    """
    if extra is None:
        return {}
    if not isinstance(extra, dict):
        raise TypeError(f"extra_frontmatter must be a dict, got {type(extra).__name__}")
    out: dict[str, Any] = {}
    for k, v in extra.items():
        if not isinstance(k, str) or not _EXTRA_KEY_RE.match(k):
            raise ValueError(
                f"extra_frontmatter key must be lowercase snake_case, got {k!r}"
            )
        _check_json_value(k, v)
        out[k] = v
    return out


def _check_json_value(path: str, v: Any) -> None:
    if v is None or isinstance(v, bool) or isinstance(v, (int, float, str)):
        return
    if isinstance(v, list):
        for i, item in enumerate(v):
            _check_json_value(f"{path}[{i}]", item)
        return
    if isinstance(v, dict):
        for k, item in v.items():
            if not isinstance(k, str):
                raise ValueError(
                    f"extra_frontmatter dict key at {path} must be str, got {type(k).__name__}"
                )
            _check_json_value(f"{path}.{k}", item)
        return
    raise ValueError(
        f"extra_frontmatter value at {path} is not JSON-serializable: {type(v).__name__}"
    )


# ---------------------------------------------------------------------------
# filename helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_slug(now: datetime | None = None) -> str:
    now = now or _utc_now()
    return now.strftime("%Y%m%d-%H%M")


def _iso_utc(now: datetime | None = None) -> str:
    now = now or _utc_now()
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


_SLOT_CLEAN_RE = re.compile(r"[^a-z0-9_\-]+")


def _slot_slug(s: str, *, max_len: int = 64) -> str:
    """Filename-slot slug. Preserves ``_`` and ``-`` so canonical unit codes
    (``dmq_quito_norte``) and route codes (``NE-03``) round-trip unchanged."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.strip().lower()
    s = _SLOT_CLEAN_RE.sub("-", s).strip("-_")
    if max_len and len(s) > max_len:
        s = s[:max_len].rstrip("-_")
    return s


def _compute_filename(
    *,
    priority: int,
    prompt_type: str,
    unit: str,
    route_code: str | None,
    timestamp: str,
) -> str:
    route_slot = "ALL" if route_code is None else _slot_slug(route_code, max_len=32)
    unit_slot = _slot_slug(unit, max_len=48)
    type_slot = prompt_type
    return f"{priority:02d}_{type_slot}_{unit_slot}_{route_slot}_{timestamp}.md"


# ---------------------------------------------------------------------------
# minimal frontmatter parser (we control the writer; no PyYAML dependency)
# ---------------------------------------------------------------------------


def _format_frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        inner = ", ".join(_yaml_scalar(v) for v in value)
        return f"[{inner}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = ", ".join(f"{k}: {_yaml_scalar(v)}" for k, v in value.items())
        return "{" + inner + "}"
    s = str(value)
    # quote if empty or contains YAML-sensitive characters
    if s == "" or any(c in s for c in ":#[]{}&*!|>'\"%@`,") or s.strip() != s:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise ValueError("file does not start with frontmatter fence")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("frontmatter not closed")
    body = text[4:end]
    out: dict[str, Any] = {}
    for raw in body.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            continue
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        out[key] = _parse_scalar(rest)
    return out


def _parse_scalar(s: str) -> Any:
    if s == "" or s.lower() in {"null", "~"}:
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(p.strip()) for p in _split_flow_list(inner)]
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        if "." not in s:
            return int(s)
    except ValueError:
        pass
    return s


def _split_flow_list(s: str) -> list[str]:
    """Split on commas not inside quotes."""
    out: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch in "[{":
            depth += 1
            buf.append(ch)
            continue
        if ch in "]}":
            depth -= 1
            buf.append(ch)
            continue
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return out


# ---------------------------------------------------------------------------
# atomic write
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


# ---------------------------------------------------------------------------
# dedup scan
# ---------------------------------------------------------------------------


def _dedup_scan(queue_root: Path, dedup_key: str) -> Path | None:
    for sub in (PENDING, SENT):
        d = queue_root / sub
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            try:
                text = p.read_text(encoding="utf-8")
                fm = _parse_frontmatter(text)
            except Exception:
                continue
            if fm.get("dedup_key") == dedup_key:
                return p
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def write_prompt(
    *,
    queue_root: Path,
    prompt_type: str,
    route_code: str | None,
    unit: str,
    province: str,
    trigger_condition: str,
    priority: int,
    estimated_research_budget: str,
    depends_on: list[str],
    dedup_key: str,
    prompt_markdown_content: str,
    generated_by_skill: str,
    supersede: bool = False,
    now: datetime | None = None,
    extra_frontmatter: dict[str, Any] | None = None,
) -> Path:
    """Write a prompt into ``queue_root/pending/`` atomically with dedup.

    Returns the path of the live prompt file. On a dedup hit without
    ``supersede``, the path of the *existing* file is returned and no write
    occurs. On a dedup hit with ``supersede=True``, the existing file is moved
    to ``archive/``, a line is appended to ``archive/superseded.log``, and the
    new prompt is written.

    ``extra_frontmatter`` lets callers attach structured side-channel fields
    (e.g. ``{"priority_bump": True}``) that render into the YAML frontmatter
    after the standard fields. Keys must be lowercase snake_case; values must
    be JSON-serializable scalars/lists/dicts.
    """
    queue_root = Path(queue_root)
    if prompt_type not in _VALID_PROMPT_TYPES:
        raise ValueError(f"invalid prompt_type: {prompt_type!r}")
    if not 0 <= priority <= 3:
        raise ValueError(f"priority must be in 0..3, got {priority}")
    if estimated_research_budget not in _VALID_BUDGETS:
        raise ValueError(
            f"invalid estimated_research_budget: {estimated_research_budget!r}"
        )
    extras = _validate_extra_frontmatter(extra_frontmatter)

    now = now or _utc_now()
    existing = _dedup_scan(queue_root, dedup_key)
    if existing is not None and not supersede:
        return existing

    filename = _compute_filename(
        priority=priority,
        prompt_type=prompt_type,
        unit=unit,
        route_code=route_code,
        timestamp=_timestamp_slug(now),
    )
    target = queue_root / PENDING / filename

    frontmatter = {
        "prompt_type": prompt_type,
        "route_code": route_code,
        "unit": unit,
        "province": province,
        "trigger_condition": trigger_condition,
        "priority": priority,
        "generated_at": _iso_utc(now),
        "generated_by_skill": generated_by_skill,
        "estimated_research_budget": estimated_research_budget,
        "depends_on": list(depends_on),
        "dedup_key": dedup_key,
    }
    for k, v in extras.items():
        if k in frontmatter:
            raise ValueError(
                f"extra_frontmatter key {k!r} collides with standard frontmatter"
            )
        frontmatter[k] = v

    if not prompt_markdown_content.endswith("\n"):
        prompt_markdown_content = prompt_markdown_content + "\n"
    text = _format_frontmatter(frontmatter) + "\n" + prompt_markdown_content

    # Guard against timestamp collision (same minute + same slot): append a
    # short UUID fragment so the new write doesn't overwrite a pre-existing
    # live prompt. Dedup would normally have caught same-dedup_key duplicates;
    # this branch only fires on different dedup_keys landing in the same slot.
    if target.exists():
        stem = target.stem
        target = target.with_name(f"{stem}-{uuid.uuid4().hex[:6]}.md")

    if existing is not None and supersede:
        archive_dir = queue_root / ARCHIVE
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived_path = archive_dir / existing.name
        shutil.move(str(existing), str(archived_path))
        log = archive_dir / "superseded.log"
        with log.open("a", encoding="utf-8") as f:
            f.write(
                f"{_iso_utc(now)}\t{archived_path.name}\tsuperseded_by={filename}\t"
                f"dedup_key={dedup_key}\n"
            )

    _atomic_write_text(target, text)
    return target


def list_pending(
    *,
    queue_root: Path,
    priority: int | None = None,
    prompt_type: str | None = None,
    unit: str | None = None,
) -> list[Path]:
    queue_root = Path(queue_root)
    pending = queue_root / PENDING
    if not pending.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(pending.glob("*.md")):
        try:
            fm = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if priority is not None and fm.get("priority") != priority:
            continue
        if prompt_type is not None and fm.get("prompt_type") != prompt_type:
            continue
        if unit is not None and fm.get("unit") != unit:
            continue
        out.append(p)
    return out


def move_to_sent(*, queue_root: Path, filename: str, now: datetime | None = None) -> Path:
    queue_root = Path(queue_root)
    src = queue_root / PENDING / filename
    if not src.exists():
        raise FileNotFoundError(f"pending prompt not found: {src}")

    text = src.read_text(encoding="utf-8")
    updated = _inject_frontmatter_field(text, "sent_at", _iso_utc(now))

    dst_dir = queue_root / SENT
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / filename

    _atomic_write_text(dst, updated)
    src.unlink()
    return dst


def _inject_frontmatter_field(text: str, key: str, value: Any) -> str:
    if not text.startswith("---\n"):
        raise ValueError("file does not start with frontmatter fence")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("frontmatter not closed")
    fm_body = text[4:end]
    lines = fm_body.splitlines()
    new_line = f"{key}: {_yaml_scalar(value)}"
    replaced = False
    out_lines = []
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith(f"{key}:"):
            out_lines.append(new_line)
            replaced = True
        else:
            out_lines.append(ln)
    if not replaced:
        out_lines.append(new_line)
    return "---\n" + "\n".join(out_lines) + "\n---" + text[end + 4 :]


def match_response_to_prompt(*, queue_root: Path, response_filename: str) -> Path:
    queue_root = Path(queue_root)
    if not response_filename.endswith(".json"):
        raise ValueError(f"response filename must end in .json: {response_filename}")
    base_stem = Path(response_filename).stem
    prompt_name = base_stem + ".md"
    for sub in (SENT, PENDING):
        candidate = queue_root / sub / prompt_name
        if candidate.exists():
            return candidate
    raise LookupError(
        f"no matching prompt for response {response_filename!r} in sent/ or pending/"
    )


def ingest_and_pair_move(
    *,
    queue_root: Path,
    response_filename: str,
    ingestion_result: dict[str, Any],
) -> tuple[Path, Path, Path]:
    """Atomically move paired prompt + response into ``ingested/``.

    Returns (moved_prompt, moved_response, sidecar). Raises before touching any
    file if the response is missing or unmatched.
    """
    queue_root = Path(queue_root)
    response_src = queue_root / RESPONSES / response_filename
    if not response_src.exists():
        raise FileNotFoundError(f"response not found: {response_src}")

    prompt_src = match_response_to_prompt(
        queue_root=queue_root, response_filename=response_filename
    )

    ingested_dir = queue_root / INGESTED
    ingested_dir.mkdir(parents=True, exist_ok=True)

    prompt_dst = ingested_dir / prompt_src.name
    response_dst = ingested_dir / response_src.name
    sidecar_dst = ingested_dir / (prompt_src.stem + ".ingest.json")

    # Stage the sidecar first (cheapest to undo); then move the two files in
    # order. If either move fails, unwind.
    sidecar_written = False
    moved_response = False
    moved_prompt = False
    try:
        _atomic_write_text(
            sidecar_dst, json.dumps(ingestion_result, ensure_ascii=False, indent=2)
        )
        sidecar_written = True

        shutil.move(str(response_src), str(response_dst))
        moved_response = True

        shutil.move(str(prompt_src), str(prompt_dst))
        moved_prompt = True
    except BaseException:
        if moved_response and not moved_prompt:
            try:
                shutil.move(str(response_dst), str(response_src))
            except Exception:
                pass
        if sidecar_written:
            try:
                sidecar_dst.unlink()
            except FileNotFoundError:
                pass
        raise

    return prompt_dst, response_dst, sidecar_dst


def archive_orphan_response(
    *,
    queue_root: Path,
    response_filename: str,
    reason: str,
    now: datetime | None = None,
) -> Path:
    queue_root = Path(queue_root)
    src = queue_root / RESPONSES / response_filename
    if not src.exists():
        raise FileNotFoundError(f"response not found: {src}")

    now = now or _utc_now()
    date_slug = now.strftime("%Y-%m-%d")
    dst_dir = queue_root / ARCHIVE / f"{date_slug}_orphans"
    dst_dir.mkdir(parents=True, exist_ok=True)

    dst = dst_dir / response_filename
    shutil.move(str(src), str(dst))

    sidecar = dst_dir / (Path(response_filename).stem + ".error.txt")
    sidecar.write_text(
        f"archived_at: {_iso_utc(now)}\nreason: {reason}\n",
        encoding="utf-8",
    )
    return dst
