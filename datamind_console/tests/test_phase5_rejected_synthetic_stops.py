"""Rejected synthetic stops never reach an exported feed (owner decision, 2026-10-09).

Every export SQL that writes stops, stop_times or trip headsigns from
node_prod.nodes must carry EXPORTABLE_STOP. The statements are checked
statically here; they were also EXPLAINed against the local database.
"""
from __future__ import annotations

import ast
from pathlib import Path

from phase5_gtfs.common.exportable_stops import EXPORTABLE_STOP

ROOT = Path(__file__).resolve().parents[2]
# (file, function) pairs whose SQL builds export rows from node_prod.nodes.
EXPORT_SITES = {
    ("datamind_console/phases/phase5_gtfs/client.py", "run_step_015_load_stops_from_phase2_final"): 5,  # 3 inserts + 2 scoped counts
    ("datamind_console/phases/phase5_gtfs/client.py", "preview_phase2_final_gtfs_stops"): 3,
    ("phase5_gtfs/compiler/build_stop_times.py", "build_stop_times"): 1,
    ("phase5_gtfs/compiler/build_trips.py", "build_trips_and_frequencies"): 1,
    ("phase5_gtfs/compiler/build_routes.py", "build_routes_and_stops"): 1,
}


def _function(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_condition_excludes_only_rejected() -> None:
    assert EXPORTABLE_STOP == "n.synthetic_review_state IS DISTINCT FROM 'rejected'"  # NULL, pending, verified pass


def test_every_export_sql_filters_rejected_synthetic_stops() -> None:
    for (path, func), expected in EXPORT_SITES.items():
        fn = _function(path, func)
        sql_reading_nodes = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.JoinedStr)
            and "node_prod.nodes" in "".join(v.value for v in n.values if isinstance(v, ast.Constant))
            and "DELETE" not in "".join(v.value for v in n.values if isinstance(v, ast.Constant))
        ]
        filtered = [n for n in sql_reading_nodes if any(isinstance(x, ast.Name) and x.id == "EXPORTABLE_STOP" for x in ast.walk(n))]
        assert len(filtered) == expected == len(sql_reading_nodes), (path, func, len(filtered), len(sql_reading_nodes))
