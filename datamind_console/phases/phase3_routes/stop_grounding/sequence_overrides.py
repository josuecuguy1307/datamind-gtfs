"""
Sequence Override Engine

Post-processing layer that applies manual corrections on top of
auto-generated sequences. Overrides survive pipeline re-runs.

Three action types:
  - replace_sequence: full replacement with keep/remove flags
  - reorder: same stops, new order (by stop_id list)
  - remove_stops: remove specific stop_ids, keep rest in order
"""
from __future__ import annotations

import glob
import json
import logging
import os
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger(__name__)

OVERRIDES_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "..", "..",
    "phase3_route_catalog", "sequence_overrides",
)


def find_latest_override_file(overrides_dir: Optional[str] = None) -> Optional[str]:
    """Find the latest override JSON in the overrides directory."""
    search_dir = overrides_dir or OVERRIDES_DIR
    search_dir = os.path.normpath(search_dir)
    if not os.path.isdir(search_dir):
        return None

    files = sorted(glob.glob(os.path.join(search_dir, "override_*.json")))
    if not files:
        return None

    return files[-1]


def load_overrides(path: str) -> Dict[str, Any]:
    """Load an override JSON file."""
    with open(path) as f:
        data = json.load(f)

    version = data.get("version", "?")
    overrides = data.get("overrides", [])
    _LOG.info("Loaded %d overrides from %s (version=%s)", len(overrides), path, version)
    return data


def apply_sequence_overrides(
    sequences: Dict[str, Any],
    overrides: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Apply manual overrides on top of auto-generated sequences.

    Args:
        sequences: the full route sequences dict (with "routes" key)
        overrides: the override specification dict (with "overrides" key)

    Returns:
        Updated sequences with overrides applied. Unmatched routes are untouched.
    """
    override_list = overrides.get("overrides", [])
    if not override_list:
        return sequences

    overrides_map = {o["route"]: o for o in override_list}
    override_version = overrides.get("version", "?")

    applied = 0
    skipped = 0

    routes = sequences.get("routes", [])
    for route in routes:
        route_name = route.get("route", "")
        if route_name not in overrides_map:
            continue

        override = overrides_map[route_name]
        action = override.get("action", "")
        reason = override.get("reason", "")

        if action == "replace_sequence":
            _apply_replace(route, override)
            applied += 1
        elif action == "reorder":
            _apply_reorder(route, override)
            applied += 1
        elif action == "remove_stops":
            _apply_remove(route, override)
            applied += 1
        else:
            _LOG.warning("Unknown override action '%s' for route '%s'", action, route_name)
            skipped += 1
            continue

        route["override_applied"] = reason
        route["override_version"] = override_version
        route["override_action"] = action

    # Check for overrides that didn't match any route
    route_names = {r.get("route", "") for r in routes}
    for override_name in overrides_map:
        if override_name not in route_names:
            _LOG.warning("Override for '%s' did not match any route", override_name)

    _LOG.info(
        "Applied %d overrides, skipped %d (version=%s)",
        applied, skipped, override_version,
    )

    # Update metadata
    sequences["overrides_applied"] = applied
    sequences["override_version"] = override_version

    return sequences


def _apply_replace(route: Dict[str, Any], override: Dict[str, Any]) -> None:
    """Replace sequence: keep only stops with keep=true, in given order."""
    ordered = override.get("ordered_stops", [])
    kept = [s for s in ordered if s.get("keep", True)]

    for i, s in enumerate(kept, 1):
        s["seq"] = i

    removed = [s for s in ordered if not s.get("keep", True)]
    n_before = len(route.get("ordered_stops", []))

    route["ordered_stops"] = kept
    route["stop_count"] = len(kept)

    _LOG.info(
        "replace_sequence '%s': %d -> %d stops (%d removed)",
        route.get("route", "?"), n_before, len(kept), len(removed),
    )


def _apply_reorder(route: Dict[str, Any], override: Dict[str, Any]) -> None:
    """Reorder: same stops, new order from stop_order list."""
    stop_order = override.get("stop_order", [])
    stop_map = {s["stop_id"]: dict(s) for s in route.get("ordered_stops", [])}

    reordered = []
    for sid in stop_order:
        if sid in stop_map:
            reordered.append(stop_map[sid])
        else:
            _LOG.warning("reorder: stop_id '%s' not found in route '%s'", sid, route.get("route", "?"))

    for i, s in enumerate(reordered, 1):
        s["seq"] = i

    n_before = len(route.get("ordered_stops", []))
    route["ordered_stops"] = reordered
    route["stop_count"] = len(reordered)

    _LOG.info(
        "reorder '%s': %d -> %d stops",
        route.get("route", "?"), n_before, len(reordered),
    )


def _apply_remove(route: Dict[str, Any], override: Dict[str, Any]) -> None:
    """Remove specific stops by ID, keep everything else in order."""
    remove_ids = set(override.get("remove_stop_ids", []))
    existing = route.get("ordered_stops", [])

    filtered = [s for s in existing if s.get("stop_id") not in remove_ids]

    for i, s in enumerate(filtered, 1):
        s["seq"] = i

    n_removed = len(existing) - len(filtered)
    route["ordered_stops"] = filtered
    route["stop_count"] = len(filtered)

    _LOG.info(
        "remove_stops '%s': removed %d stops (%d remaining)",
        route.get("route", "?"), n_removed, len(filtered),
    )


def override_summary(overrides: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return a summary list of overrides for display."""
    rows = []
    for o in overrides.get("overrides", []):
        action = o.get("action", "?")
        n_stops = 0
        if action == "replace_sequence":
            kept = sum(1 for s in o.get("ordered_stops", []) if s.get("keep", True))
            removed = sum(1 for s in o.get("ordered_stops", []) if not s.get("keep", True))
            n_stops = kept
            detail = f"{kept} kept, {removed} removed"
        elif action == "reorder":
            n_stops = len(o.get("stop_order", []))
            detail = f"{n_stops} stops reordered"
        elif action == "remove_stops":
            n_remove = len(o.get("remove_stop_ids", []))
            detail = f"{n_remove} stops to remove"
        else:
            detail = "unknown action"

        rows.append({
            "route": o.get("route", "?"),
            "action": action,
            "detail": detail,
            "reason": o.get("reason", ""),
        })
    return rows
