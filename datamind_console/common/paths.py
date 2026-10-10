"""Path resolution for operational scripts.

Scripts used to hard-code one developer's home (``/Users/<name>/...``) for
repo files, inputs dropped in ``~/Downloads`` and Desktop mirrors. They now:

* address repo files relative to ``PROJECT_ROOT``;
* read operator-provided inputs from an environment variable, or from a
  declared candidate location (``~/Downloads/<file>``, home-relative so it
  works for any macOS user), and otherwise stop with a message naming the
  variable to set, never a silent fallback;
* write Desktop mirrors through ``PHASE3_CATALOG_MIRROR_DIR``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Union

from datamind_console.common.env import PROJECT_ROOT

PathLike = Union[str, Path]


class PathNotConfigured(FileNotFoundError):
    """A required path is neither configured nor discoverable.

    A ``FileNotFoundError`` so scripts that treat an optional input as
    best-effort (``except Exception``) keep doing so, and uncaught it stops
    the script with the message.
    """


def project_path(*parts: PathLike) -> Path:
    """A path inside the repository, independent of where it is checked out."""
    return PROJECT_ROOT.joinpath(*[str(p) for p in parts])


def downloads(name: str) -> Path:
    """``~/Downloads/<name>`` for the current user (macOS layout)."""
    return Path.home() / "Downloads" / name


def resolve_input(env_var: str, *candidates: PathLike, description: str = "input file") -> Path:
    """Resolve a file the operator provides.

    ``env_var`` wins and must point to an existing file. Otherwise the first
    existing candidate is used. Otherwise :class:`PathNotConfigured` explains
    what to set.
    """
    configured = os.environ.get(env_var, "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise PathNotConfigured(f"{env_var}={path} does not exist ({description}).")
        return path
    tried = [Path(c).expanduser() for c in candidates]
    for path in tried:
        if path.is_file():
            return path
    looked = ", ".join(str(p) for p in tried) or "no default location"
    raise PathNotConfigured(f"{description} not found. Set {env_var}=/path/to/file (looked in: {looked}).")


def resolve_output_dir(env_var: str, default: PathLike) -> Path:
    """Output directory from ``env_var`` or ``default`` (created if missing)."""
    configured = os.environ.get(env_var, "").strip()
    path = Path(configured).expanduser() if configured else Path(default).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def phase3_catalog_mirror_dir() -> Path:
    """Desktop mirror for Phase 3 catalog outputs: ``PHASE3_CATALOG_MIRROR_DIR`` or ~/Desktop."""
    configured = os.environ.get("PHASE3_CATALOG_MIRROR_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "phase3_route_catalog"


def first_existing(paths: Iterable[PathLike]) -> Path | None:
    for p in paths:
        path = Path(p).expanduser()
        if path.exists():
            return path
    return None
