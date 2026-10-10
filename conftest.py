"""Tests written against the original region's data skip, with the reason, when that data is absent.

This repository ships no regional catalogs, only a synthetic sample (README:
"What's included and what isn't"). The tests below exercise the pipeline
against the original region's files. Each group is skipped only while its
condition holds; with your own region's files in place they run as normal
tests. They are reported as skipped, never as passing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
PLACE_CATALOG = ROOT / "workspace" / "catalogs" / "phase3" / "place_geography_catalog.json"
PHASE3_CATALOGS = ROOT / "phase3_routes" / "catalogs"
BBOX_CATALOG = ROOT / "phase1_nodes" / "catalogs" / "bbox_catalog.json"


def _bbox_catalog_has_original_region() -> bool:
    try:
        return "quito" in BBOX_CATALOG.read_text(encoding="utf-8").lower()
    except OSError:
        return False


# These modules load the place catalog at import time, so they are not even collected without it.
collect_ignore = [] if PLACE_CATALOG.exists() else [
    "datamind_console/phases/phase3_routes/stop_grounding/tests/test_discovery_pipeline_a2_autofill.py",
    "datamind_console/phases/phase3_routes/stop_grounding/tests/test_geography_guardrails.py",
    "datamind_console/phases/phase3_routes/stop_grounding/tests/test_session_integration.py",
    "datamind_console/phases/phase3_routes/stop_grounding/tests/test_terminus_and_jurisdiction.py",
]

_EXTRACTOR = "datamind_console/phases/phase3_routes/tests/test_phase3_extractor_catalog.py::Phase3ExtractorCatalogTests::"
_GEO = "datamind_console/tests/"
REGION_TESTS = [
    (
        not PLACE_CATALOG.exists(),
        "needs workspace/catalogs/phase3/place_geography_catalog.json",
        ["tests/phase3/test_valhalla_meta_traceability.py::" + n for n in (
            "test_A1_corridor_builder_attaches_meta_to_corridor_result",
            "test_A2_geometry_candidate_inherits_meta_from_corridor",
            "test_A3_fallback_path_synthesises_replay_safe_meta",
            "test_B1_captured_meta_replays_to_matching_shape",
        )],
    ),
    (
        not ((PHASE3_CATALOGS / "valle_de_los_chillos_phase3_catalog.json").exists()
             and (PHASE3_CATALOGS / "quito_sur_phase3_catalog.json").exists()),
        "needs the original region's Phase 3 catalogs in phase3_routes/catalogs/",
        [_EXTRACTOR + n for n in (
            "test_catalog_expected_routes_produce_sector_scoped_gap_candidates",
            "test_harvest_uses_existing_reviewable_rows_to_reduce_remaining_goal",
            "test_quito_sur_catalog_builds_attempts_and_carries_bbox_hints",
            "test_valle_catalog_builds_broad_attempt_universe",
            "test_valle_catalog_includes_bundle_route_and_place_only_attempts",
            "test_valle_catalog_keeps_secondary_hints_geographically_aligned",
        )],
    ),
    (
        not _bbox_catalog_has_original_region(),
        "checks the original region's bbox catalog; phase1_nodes/catalogs/bbox_catalog.json is the synthetic sample",
        [
            _GEO + "test_geography_e2e_workflow.py::Phase1E2ETests::test_place_input_resolves_into_bbox_and_reaches_snapshot",
            _GEO + "test_geography_input_resolver.py::GeographyInputResolverTests::test_catalog_resolution_returns_trusted_bbox",
            _GEO + "test_geography_input_resolver.py::GeographyInputResolverTests::test_quito_sur_group_normalizes_and_biases_geocoder_queries",
            _GEO + "test_geography_input_resolver.py::GeographyInputResolverTests::test_valle_corridor_group_normalizes_and_biases_geocoder_queries",
        ],
    ),
]


def pytest_collection_modifyitems(config, items):
    root = str(ROOT) + "/"
    for applies, reason, nodeids in REGION_TESTS:
        if not applies:
            continue
        wanted = set(nodeids)
        for item in items:
            if item.nodeid in wanted or (str(item.path).replace(root, "") + "::" + "::".join(item.nodeid.split("::")[1:])) in wanted:
                item.add_marker(pytest.mark.skip(reason=f"regional data not shipped: {reason}"))
