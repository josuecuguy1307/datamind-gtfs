"""HADES Re-Entry Classifier.

Assigns each grandfathered route (in ``route_prod.routes`` with
``grandfathered_until IS NOT NULL``) one of seven classifications used by
the re-entry worker to prioritise enhancement work before the 2026-07-20
deadline. The classifier is pure: it consumes enforcer reports plus the
route's ``source_type`` and returns a decision + priority. All DB work
lives in ``scripts/populate_re_entry_queue.py``.

Classification schema (matches the CHECK constraint on
``route_prod.re_entry_queue.classification`` from migration 032):

  osm_relation_severe            — OSM-relation origin + severe geometry.
                                    Geometry Fixer expected to help most.
  osm_relation_clean             — OSM-relation origin with no severe
                                    geometry anomalies. Usually just
                                    needs a cleaner stop list.
  discovery_legacy               — Legacy discovery-pipeline routes.
  constructor_canonical_legacy   — Canonical constructor outputs predating
                                    the v3 enforcer.
  manual_constructor_legacy      — Operator-hand-authored routes.
  structural_repair              — Coverage classified unroutable
                                    regardless of source. Overrides the
                                    source-type mapping because an
                                    unroutable route cannot ship at all.
  unclassified                   — Source type outside the Prompt 8 spec
                                    (e.g. deep_research_override,
                                    synthetic_fill). Deferred until a
                                    dedicated playbook lands.

Priority ladder (lower = processed sooner by the worker):

  10  osm_relation_severe              — geometry fixer most effective
  20  structural_repair                — unroutable must ship or die
  30  discovery_legacy
  40  constructor_canonical_legacy
  50  manual_constructor_legacy
  60  osm_relation_clean
  99  unclassified                     — defer; out-of-scope for Prompt 8

Within a classification bucket, callers may break ties on enqueue order
(older first) using the re_entry_queue.enqueued_at column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hades.enforcers.geometry_enforcer import GeometryReport
from hades.enforcers.stop_coverage_enforcer import StopCoverageReport


# ---------------------------------------------------------------------------
# Classification + priority constants. Matching the CHECK constraint order
# from migration 032 makes it easier to grep the SQL for the same labels.
# ---------------------------------------------------------------------------

OSM_RELATION_SEVERE = "osm_relation_severe"
OSM_RELATION_CLEAN = "osm_relation_clean"
DISCOVERY_LEGACY = "discovery_legacy"
CONSTRUCTOR_CANONICAL_LEGACY = "constructor_canonical_legacy"
MANUAL_CONSTRUCTOR_LEGACY = "manual_constructor_legacy"
STRUCTURAL_REPAIR = "structural_repair"
DEEP_RESEARCH_LEGACY = "deep_research_legacy"
UNCLASSIFIED = "unclassified"

CLASSIFICATION_VALUES = frozenset(
    {
        OSM_RELATION_SEVERE,
        OSM_RELATION_CLEAN,
        DISCOVERY_LEGACY,
        CONSTRUCTOR_CANONICAL_LEGACY,
        MANUAL_CONSTRUCTOR_LEGACY,
        STRUCTURAL_REPAIR,
        DEEP_RESEARCH_LEGACY,
        UNCLASSIFIED,
    }
)

CLASSIFICATION_PRIORITY = {
    OSM_RELATION_SEVERE: 10,
    STRUCTURAL_REPAIR: 20,
    DISCOVERY_LEGACY: 30,
    DEEP_RESEARCH_LEGACY: 35,
    CONSTRUCTOR_CANONICAL_LEGACY: 40,
    MANUAL_CONSTRUCTOR_LEGACY: 50,
    OSM_RELATION_CLEAN: 60,
    UNCLASSIFIED: 99,
}

# Source types the Prompt 8 spec explicitly maps. Anything else falls to
# UNCLASSIFIED regardless of enforcer state (structural_repair is the one
# exception — it fires even for unmapped source types because an
# unroutable route cannot ship by the deadline).
_SOURCE_TO_CLEAN_CLASSIFICATION = {
    "osm_relation_import": OSM_RELATION_CLEAN,
    "discovery_pipeline": DISCOVERY_LEGACY,
    "constructor_canonical": CONSTRUCTOR_CANONICAL_LEGACY,
    "manual_constructor": MANUAL_CONSTRUCTOR_LEGACY,
    "deep_research_override": DEEP_RESEARCH_LEGACY,
}


# ---------------------------------------------------------------------------
# Output schema.
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ClassifierOutput:
    classification: str
    priority: int
    priority_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "priority": int(self.priority),
            "priority_reason": self.priority_reason,
        }


# ---------------------------------------------------------------------------
# Pure classifier.
# ---------------------------------------------------------------------------

def classify(
    *,
    source_type: Optional[str],
    geometry_report: Optional[GeometryReport] = None,
    coverage_report: Optional[StopCoverageReport] = None,
) -> ClassifierOutput:
    """Assign a re-entry classification + priority to a route.

    Precedence (top wins):

      1. OSM relation + severe geometry → ``osm_relation_severe``
      2. Coverage unroutable → ``structural_repair``
      3. Source-type mapping → the corresponding *_legacy / osm_relation_clean
      4. Fallback → ``unclassified``

    All four arms return a priority + free-form reason string. Missing
    enforcer reports are treated as "no signal" — the classifier skips
    the arms that depend on them.
    """
    source = (source_type or "").strip()

    # Arm 1 — severe geometry on an OSM-relation origin.
    if (
        source == "osm_relation_import"
        and geometry_report is not None
        and geometry_report.classification == "severe"
    ):
        return ClassifierOutput(
            classification=OSM_RELATION_SEVERE,
            priority=CLASSIFICATION_PRIORITY[OSM_RELATION_SEVERE],
            priority_reason=(
                f"osm_relation_import+geometry={geometry_report.classification}"
                f" (anomalies={len(geometry_report.anomalies)},"
                f" max_severity={geometry_report.max_severity:.2f})"
            ),
        )

    # Arm 2 — coverage unroutable overrides the source mapping. An
    # unroutable route cannot ship, so structural repair takes priority
    # over the per-source enhancement track.
    if (
        coverage_report is not None
        and coverage_report.classification == "unroutable"
    ):
        return ClassifierOutput(
            classification=STRUCTURAL_REPAIR,
            priority=CLASSIFICATION_PRIORITY[STRUCTURAL_REPAIR],
            priority_reason=(
                f"coverage={coverage_report.classification}"
                f" (zone={coverage_report.zone},"
                f" n_stops={coverage_report.n_stops},"
                f" unresolved_gaps={coverage_report.n_gaps_unresolved})"
            ),
        )

    # Arm 3 — source-type mapping.
    mapped = _SOURCE_TO_CLEAN_CLASSIFICATION.get(source)
    if mapped is not None:
        reason_bits = [f"source_type={source}"]
        if geometry_report is not None:
            reason_bits.append(f"geometry={geometry_report.classification}")
        if coverage_report is not None:
            reason_bits.append(f"coverage={coverage_report.classification}")
        return ClassifierOutput(
            classification=mapped,
            priority=CLASSIFICATION_PRIORITY[mapped],
            priority_reason=", ".join(reason_bits),
        )

    # Arm 4 — unmapped source type.
    return ClassifierOutput(
        classification=UNCLASSIFIED,
        priority=CLASSIFICATION_PRIORITY[UNCLASSIFIED],
        priority_reason=f"unmapped source_type={source or '<null>'}",
    )


__all__ = [
    "OSM_RELATION_SEVERE",
    "OSM_RELATION_CLEAN",
    "DISCOVERY_LEGACY",
    "CONSTRUCTOR_CANONICAL_LEGACY",
    "MANUAL_CONSTRUCTOR_LEGACY",
    "STRUCTURAL_REPAIR",
    "DEEP_RESEARCH_LEGACY",
    "UNCLASSIFIED",
    "CLASSIFICATION_VALUES",
    "CLASSIFICATION_PRIORITY",
    "ClassifierOutput",
    "classify",
]
