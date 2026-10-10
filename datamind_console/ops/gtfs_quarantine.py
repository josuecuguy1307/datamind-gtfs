"""GTFS artifacts that must never be published.

Every Phase 5 export checked on 2026-10-09 fails the MobilityData validator
(the exporter reuses ``block_id`` across overlapping trips), and the newest
one is also the file sitting in the OTP router, which AWS full deploy picks up
automatically when nothing is uploaded. The structure check alone let it
through. This registry blocks those exact files, by SHA-256, in local publish
and AWS full deploy, with no acknowledgment override.

The registry is ``phase5_gtfs/quarantine.json``. A missing registry means no
quarantine; an unreadable one fails closed (every publish is refused) so a
corrupted file can never silently re-enable a known-bad feed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from datamind_console.common.env import PROJECT_ROOT

REGISTRY_PATH = PROJECT_ROOT / "phase5_gtfs" / "quarantine.json"


@dataclass(frozen=True)
class QuarantineEntry:
    sha256: str
    label: str
    reason: str
    quarantined_on: str = ""


class QuarantineRegistryError(RuntimeError):
    """The registry exists but cannot be read; publishing must stop."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_registry(path: Optional[Path] = None) -> dict[str, QuarantineEntry]:
    registry = Path(path or REGISTRY_PATH)
    if not registry.exists():
        return {}
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        entries = {}
        for raw in data["entries"]:
            entry = QuarantineEntry(
                sha256=str(raw["sha256"]).lower(),
                label=str(raw["label"]),
                reason=str(raw["reason"]),
                quarantined_on=str(raw.get("quarantined_on", "")),
            )
            entries[entry.sha256] = entry
        return entries
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise QuarantineRegistryError(f"GTFS quarantine registry unreadable: {registry}: {exc}") from exc


def quarantine_entry(zip_path: Path, *, registry_path: Optional[Path] = None) -> Optional[QuarantineEntry]:
    """Entry for ``zip_path`` if it is quarantined, else None. Raises on an unreadable registry."""
    entries = load_registry(registry_path)
    if not entries:
        return None
    return entries.get(sha256_of(zip_path))


def refusal(zip_path: Path, *, registry_path: Optional[Path] = None) -> Optional[str]:
    """Why ``zip_path`` must not be published, or None when it may proceed to the other gates."""
    try:
        entry = quarantine_entry(zip_path, registry_path=registry_path)
    except QuarantineRegistryError as exc:
        return str(exc)
    if entry is None:
        return None
    return f"Quarantined GTFS ({entry.label}): {entry.reason}"
