from __future__ import annotations

from contextlib import contextmanager
import unittest
import uuid
from unittest.mock import patch

from datamind_console.phases.phase3_routes.client import Phase3Client


def _bbox(south: float, west: float, north: float, east: float) -> dict:
    return {
        "south": float(south),
        "west": float(west),
        "north": float(north),
        "east": float(east),
    }


def _attempt(
    *,
    source_document: str,
    place: str,
    group: str,
    place_bundle: str,
    route_hint_raw: str,
    chosen_osm_relation_id: int,
    bbox_used: dict,
    selection_confidence: float = 0.8,
) -> dict:
    return {
        "source_document": source_document,
        "place": place,
        "group": group,
        "place_bundle": place_bundle,
        "route_hint_raw": route_hint_raw,
        "chosen_osm_relation_id": int(chosen_osm_relation_id),
        "bbox_used": dict(bbox_used),
        "selection_confidence": float(selection_confidence),
    }


def _review_row(
    *,
    route_id: str | None = None,
    extractor_source: str,
    relation_id: int,
    place: str,
    group: str,
    place_bundle: str,
    route_hint: str,
    bbox_used: dict,
    selection_confidence: float = 0.8,
    attempt_history: list[dict] | None = None,
    duplicate_attempt_count: int = 0,
    raw_relation_available: bool = True,
    fetch_relation_stored: bool = True,
    created_at: str = "2026-03-01T00:00:00Z",
) -> dict:
    return {
        "route_id": route_id or str(uuid.uuid4()),
        "extractor_source": extractor_source,
        "source_document": extractor_source,
        "selected_osm_relation_id": int(relation_id),
        "osm_relation_id": int(relation_id),
        "target_place": place,
        "target_group": group,
        "target_place_bundle": place_bundle,
        "route_hint": route_hint,
        "bbox_used": dict(bbox_used),
        "selection_confidence": float(selection_confidence),
        "attempt_history": list(attempt_history or []),
        "duplicate_attempt_count": int(duplicate_attempt_count),
        "raw_relation_available": bool(raw_relation_available),
        "fetch_relation_stored": bool(fetch_relation_stored),
        "created_at": created_at,
        "extractor_review": {
            "discover": {
                "relation_extraction_success": True,
                "chosen_osm_relation_id": int(relation_id),
            }
        },
    }


class _GroupCursor:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = list(rows)
        self.executed: list[tuple[str, object]] = []

    def execute(self, sql, params=None) -> None:
        self.executed.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return list(self.rows)

    def close(self) -> None:
        return None


def _fake_group_db(rows: list[dict]):
    cursor = _GroupCursor(rows)

    @contextmanager
    def _db_conn():
        yield object()

    @contextmanager
    def _db_cursor(_conn):
        yield cursor

    return _db_conn, _db_cursor


class ExtractorDedupeGuardrailTests(unittest.TestCase):
    def _client(self) -> Phase3Client:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            return Phase3Client()

    def test_same_source_same_relation_similar_context_still_allows_merge(self) -> None:
        client = self._client()
        host = _review_row(
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=6258190,
            place="La Magdalena",
            group="Quito Sur",
            place_bundle="quito_sur_gateway_connector_bundle",
            route_hint="Quitumbe - La Magdalena",
            bbox_used=_bbox(-0.27, -78.56, -0.21, -78.49),
            attempt_history=[
                _attempt(
                    source_document="quito_sur_phase3_catalog.json",
                    place="La Magdalena",
                    group="Quito Sur",
                    place_bundle="quito_sur_gateway_connector_bundle",
                    route_hint_raw="Quitumbe - La Magdalena",
                    chosen_osm_relation_id=6258190,
                    bbox_used=_bbox(-0.27, -78.56, -0.21, -78.49),
                )
            ],
        )
        incoming = _review_row(
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=6258190,
            place="La Magdalena",
            group="Quito Sur",
            place_bundle="quito_sur_gateway_connector_bundle",
            route_hint="Quitumbe - La Magdalena",
            bbox_used=_bbox(-0.271, -78.561, -0.211, -78.491),
        )

        decision = client._evaluate_extractor_canonical_reuse(host_row=host, incoming_row=incoming)

        self.assertTrue(bool(decision.get("allow_merge")))

    def test_different_relation_id_blocks_merge(self) -> None:
        client = self._client()
        host = _review_row(
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=6258190,
            place="La Magdalena",
            group="Quito Sur",
            place_bundle="quito_sur_gateway_connector_bundle",
            route_hint="Quitumbe - La Magdalena",
            bbox_used=_bbox(-0.27, -78.56, -0.21, -78.49),
        )
        incoming = _review_row(
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=2006760,
            place="La Magdalena",
            group="Quito Sur",
            place_bundle="quito_sur_gateway_connector_bundle",
            route_hint="Quitumbe - La Magdalena",
            bbox_used=_bbox(-0.27, -78.56, -0.21, -78.49),
        )

        decision = client._evaluate_extractor_canonical_reuse(host_row=host, incoming_row=incoming)

        self.assertFalse(bool(decision.get("allow_merge")))
        self.assertIn("relation_mismatch", list(decision.get("reason_codes") or []))

    def test_cross_source_weak_evidence_blocks_merge(self) -> None:
        client = self._client()
        host = _review_row(
            extractor_source="phase3_extract_targets.json",
            relation_id=6304544,
            place="Terminal Río Coca",
            group="Tumbaco-Cumbaya",
            place_bundle="tumbaco_gateway_anchors",
            route_hint="Río Coca - Pifo",
            bbox_used=_bbox(-0.24, -78.50, -0.14, -78.41),
        )
        incoming = _review_row(
            extractor_source="catalogo_cumbaya_tumbaco_phase3_codex_isolated.json",
            relation_id=6304544,
            place="Cumbayá",
            group="Tumbaco-Cumbaya",
            place_bundle="tumbaco_core_bundle",
            route_hint="Cumbayá - Arenal",
            bbox_used=_bbox(-0.24, -78.48, -0.16, -78.38),
        )

        decision = client._evaluate_extractor_canonical_reuse(host_row=host, incoming_row=incoming)

        self.assertFalse(bool(decision.get("allow_merge")))
        self.assertIn("cross_source_weak_evidence", list(decision.get("reason_codes") or []))

    def test_materially_different_route_hints_and_place_bundles_block_merge(self) -> None:
        client = self._client()
        host = _review_row(
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=6289406,
            place="Marin",
            group="Quito Sur",
            place_bundle="quito_sur_gateway_connector_bundle",
            route_hint="South neighborhoods - Marin",
            bbox_used=_bbox(-0.27, -78.56, -0.21, -78.49),
        )
        incoming = _review_row(
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=6289406,
            place="Guamaní",
            group="Quito Sur",
            place_bundle="quito_sur_outer_axis_bundle",
            route_hint="Quitumbe - Guamaní",
            bbox_used=_bbox(-0.33, -78.57, -0.25, -78.47),
        )

        decision = client._evaluate_extractor_canonical_reuse(host_row=host, incoming_row=incoming)

        self.assertFalse(bool(decision.get("allow_merge")))
        self.assertIn("route_hint_divergence", list(decision.get("reason_codes") or []))
        self.assertIn("place_bundle_divergence", list(decision.get("reason_codes") or []))

    def test_heterogeneous_host_does_not_keep_absorbing_more_contexts(self) -> None:
        client = self._client()
        host = _review_row(
            extractor_source="phase3_extract_targets.json",
            relation_id=6304544,
            place="Terminal Río Coca",
            group="Tumbaco-Cumbaya",
            place_bundle="tumbaco_gateway_anchors",
            route_hint="Río Coca - Pifo",
            bbox_used=_bbox(-0.24, -78.50, -0.14, -78.41),
            attempt_history=[
                _attempt(
                    source_document="phase3_extract_targets.json",
                    place="Terminal Río Coca",
                    group="Tumbaco-Cumbaya",
                    place_bundle="tumbaco_gateway_anchors",
                    route_hint_raw="Río Coca - Pifo",
                    chosen_osm_relation_id=6304544,
                    bbox_used=_bbox(-0.24, -78.50, -0.14, -78.41),
                ),
                _attempt(
                    source_document="catalogo_cumbaya_tumbaco_phase3_codex_isolated.json",
                    place="Cumbayá",
                    group="Tumbaco-Cumbaya",
                    place_bundle="tumbaco_core_bundle",
                    route_hint_raw="Cumbayá - Arenal",
                    chosen_osm_relation_id=6433717,
                    bbox_used=_bbox(-0.24, -78.48, -0.16, -78.38),
                ),
            ],
            duplicate_attempt_count=2,
        )
        incoming = _review_row(
            extractor_source="catalogo_cumbaya_tumbaco_phase3_codex_isolated.json",
            relation_id=6304544,
            place="Cumbayá",
            group="Tumbaco-Cumbaya",
            place_bundle="tumbaco_core_bundle",
            route_hint="Cumbayá - Floresta",
            bbox_used=_bbox(-0.23, -78.47, -0.15, -78.37),
        )

        decision = client._evaluate_extractor_canonical_reuse(host_row=host, incoming_row=incoming)

        self.assertFalse(bool(decision.get("allow_merge")))
        self.assertIn("heterogeneous_host", list(decision.get("reason_codes") or []))

    def test_resolve_extractor_candidate_novelty_preserves_separate_row_when_cross_source_guard_blocks(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        candidate_rows = [
            {
                "osm_relation_id": 6304544,
                "score": 1450.0,
                "stop_prior_count": 40,
                "selection_rank": 1,
                "selection_confidence": 0.82,
                "existing_relation_usage_count": 1,
                "existing_relation_route_ids": ["existing-route"],
                "matched_soft_signals": ["ref"],
                "is_chosen": True,
            }
        ]
        existing_host = _review_row(
            route_id="existing-route",
            extractor_source="phase3_extract_targets.json",
            relation_id=6304544,
            place="Terminal Río Coca",
            group="Tumbaco-Cumbaya",
            place_bundle="tumbaco_gateway_anchors",
            route_hint="Río Coca - Pifo",
            bbox_used=_bbox(-0.24, -78.50, -0.14, -78.41),
        )

        with patch.object(client, "_annotate_relation_candidate_novelty", return_value=candidate_rows), patch.object(
            client,
            "_list_existing_extractor_relation_routes",
            return_value={6304544: [existing_host]},
        ):
            out = client._resolve_extractor_candidate_novelty(
                route_id=route_id,
                candidate_rows=candidate_rows,
                chosen_relation_id=6304544,
                reuse_context={
                    "source_document": "catalogo_cumbaya_tumbaco_phase3_codex_isolated.json",
                    "place": "Cumbayá",
                    "group": "Tumbaco-Cumbaya",
                    "place_bundle": "tumbaco_core_bundle",
                    "route_hint_raw": "Cumbayá - Arenal",
                    "bbox_used": _bbox(-0.24, -78.48, -0.16, -78.38),
                    "selection_confidence": 0.82,
                },
            )

        self.assertEqual(str(out.get("novelty_status") or ""), "separate_review_required")
        self.assertIsNone(out.get("reused_existing_route_id"))

    def test_dedupe_extractor_review_jobs_merges_only_compatible_duplicates(self) -> None:
        client = self._client()
        relation_id = 6258190
        groups = [{"osm_relation_id": relation_id, "duplicate_count": 3}]
        host = _review_row(
            route_id="route-host",
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=relation_id,
            place="La Magdalena",
            group="Quito Sur",
            place_bundle="quito_sur_gateway_connector_bundle",
            route_hint="Quitumbe - La Magdalena",
            bbox_used=_bbox(-0.27, -78.56, -0.21, -78.49),
            created_at="2026-03-01T00:00:00Z",
        )
        same_duplicate = _review_row(
            route_id="route-same",
            extractor_source="quito_sur_phase3_catalog.json",
            relation_id=relation_id,
            place="La Magdalena",
            group="Quito Sur",
            place_bundle="quito_sur_gateway_connector_bundle",
            route_hint="Quitumbe - La Magdalena",
            bbox_used=_bbox(-0.271, -78.561, -0.211, -78.491),
            created_at="2026-03-02T00:00:00Z",
        )
        cross_source_divergent = _review_row(
            route_id="route-cross",
            extractor_source="phase3_extract_targets.json",
            relation_id=relation_id,
            place="Centro Histórico",
            group="Quito Urbano",
            place_bundle="quito_sur_urban_corridor_bundle",
            route_hint="Quito Sur - Centro Histórico",
            bbox_used=_bbox(-0.24, -78.54, -0.18, -78.46),
            created_at="2026-03-03T00:00:00Z",
        )
        fake_db_conn, fake_db_cursor = _fake_group_db(groups)

        with patch.object(client, "_ensure_extractor_review_schema", return_value=None), patch.object(
            client,
            "_ensure_route_review_schema",
            return_value=None,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_conn",
            fake_db_conn,
        ), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor",
            fake_db_cursor,
        ), patch.object(
            client,
            "_list_existing_extractor_relation_routes",
            return_value={relation_id: [host, same_duplicate, cross_source_divergent]},
        ), patch.object(
            client,
            "_merge_duplicate_extractor_attempt",
            return_value={},
        ) as merge:
            out = client.dedupe_extractor_review_jobs()

        self.assertEqual(merge.call_count, 1)
        self.assertEqual(str(merge.call_args.kwargs.get("duplicate_route_id") or ""), "route-same")
        self.assertEqual(int(out.get("merged_route_count") or 0), 1)


if __name__ == "__main__":
    unittest.main()
