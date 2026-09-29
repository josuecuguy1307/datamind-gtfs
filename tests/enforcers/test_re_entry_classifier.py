"""Unit tests for hades.enforcers.re_entry_classifier.

The classifier is pure — tests pass synthetic GeometryReport /
StopCoverageReport dataclasses directly and assert on the returned
classification + priority. No DB access, no enforcer runs.
"""

from __future__ import annotations

from hades.enforcers.geometry_enforcer import Anomaly, GeometryReport
from hades.enforcers.stop_coverage_enforcer import StopCoverageReport
from hades.enforcers.re_entry_classifier import (
    CLASSIFICATION_PRIORITY,
    CONSTRUCTOR_CANONICAL_LEGACY,
    DISCOVERY_LEGACY,
    MANUAL_CONSTRUCTOR_LEGACY,
    OSM_RELATION_CLEAN,
    OSM_RELATION_SEVERE,
    STRUCTURAL_REPAIR,
    UNCLASSIFIED,
    classify,
)


def _geom_report(classification: str, n_anomalies: int = 0, max_sev: float = 0.0):
    anomalies = [
        Anomaly(
            type="IMPOSSIBLE_LOOP",
            severity=max_sev,
            location_idx=10,
            coords=(-0.18, -78.48),
            context="synthetic",
        )
        for _ in range(n_anomalies)
    ]
    return GeometryReport(
        route_code="T-R",
        version=3,
        anomalies=anomalies,
        classification=classification,
        max_severity=max_sev,
    )


def _cov_report(classification: str, n_unresolved: int = 0, zone: str = "urban_dense"):
    return StopCoverageReport(
        route_code="T-R",
        version=1,
        zone=zone,
        classification=classification,
        n_stops=10,
        route_length_m=5000.0,
        n_gaps_unresolved=n_unresolved,
    )


# ---------------------------------------------------------------------------
# Arm 1 — OSM relation + severe geometry.
# ---------------------------------------------------------------------------

def test_osm_relation_severe_when_geometry_severe():
    result = classify(
        source_type="osm_relation_import",
        geometry_report=_geom_report("severe", n_anomalies=3, max_sev=0.9),
        coverage_report=_cov_report("acceptable"),
    )
    assert result.classification == OSM_RELATION_SEVERE
    assert result.priority == CLASSIFICATION_PRIORITY[OSM_RELATION_SEVERE]
    assert result.priority == 10
    assert "osm_relation_import" in result.priority_reason
    assert "severe" in result.priority_reason


def test_osm_relation_clean_when_geometry_not_severe():
    result = classify(
        source_type="osm_relation_import",
        geometry_report=_geom_report("moderate", n_anomalies=1, max_sev=0.4),
        coverage_report=_cov_report("good"),
    )
    assert result.classification == OSM_RELATION_CLEAN
    assert result.priority == 60


# ---------------------------------------------------------------------------
# Arm 2 — coverage unroutable overrides any source mapping (except arm 1).
# ---------------------------------------------------------------------------

def test_coverage_unroutable_overrides_source_mapping():
    # Manual-constructor source would normally map to manual_constructor_legacy,
    # but unroutable coverage must short-circuit to structural_repair.
    result = classify(
        source_type="manual_constructor",
        geometry_report=_geom_report("clean"),
        coverage_report=_cov_report("unroutable", n_unresolved=2),
    )
    assert result.classification == STRUCTURAL_REPAIR
    assert result.priority == 20
    assert "unroutable" in result.priority_reason


def test_severe_osm_wins_over_unroutable_coverage():
    # Precedence: arm 1 (osm_relation_severe) runs before arm 2 (structural).
    result = classify(
        source_type="osm_relation_import",
        geometry_report=_geom_report("severe", n_anomalies=5, max_sev=0.95),
        coverage_report=_cov_report("unroutable", n_unresolved=3),
    )
    assert result.classification == OSM_RELATION_SEVERE
    assert result.priority == 10


# ---------------------------------------------------------------------------
# Arm 3 — source-type mapping for non-OSM, non-unroutable routes.
# ---------------------------------------------------------------------------

def test_discovery_pipeline_maps_to_discovery_legacy():
    result = classify(
        source_type="discovery_pipeline",
        geometry_report=_geom_report("minor"),
        coverage_report=_cov_report("acceptable"),
    )
    assert result.classification == DISCOVERY_LEGACY
    assert result.priority == 30


def test_manual_constructor_maps_to_manual_constructor_legacy():
    result = classify(
        source_type="manual_constructor",
        geometry_report=_geom_report("clean"),
        coverage_report=_cov_report("good"),
    )
    assert result.classification == MANUAL_CONSTRUCTOR_LEGACY
    assert result.priority == 50


def test_constructor_canonical_maps_to_constructor_canonical_legacy():
    result = classify(
        source_type="constructor_canonical",
        geometry_report=None,
        coverage_report=None,
    )
    assert result.classification == CONSTRUCTOR_CANONICAL_LEGACY
    assert result.priority == 40


# ---------------------------------------------------------------------------
# Arm 4 — unmapped source types fall through to unclassified.
# ---------------------------------------------------------------------------

def test_deep_research_override_is_unclassified():
    result = classify(
        source_type="deep_research_override",
        geometry_report=_geom_report("minor"),
        coverage_report=_cov_report("good"),
    )
    assert result.classification == UNCLASSIFIED
    assert result.priority == 99
    assert "deep_research_override" in result.priority_reason


def test_synthetic_fill_is_unclassified():
    result = classify(
        source_type="synthetic_fill",
        geometry_report=None,
        coverage_report=None,
    )
    assert result.classification == UNCLASSIFIED
    assert result.priority == 99


def test_null_source_type_is_unclassified_with_explicit_marker():
    result = classify(source_type=None)
    assert result.classification == UNCLASSIFIED
    assert "<null>" in result.priority_reason
