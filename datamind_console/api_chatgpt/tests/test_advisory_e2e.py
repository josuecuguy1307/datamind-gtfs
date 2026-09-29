from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from datamind_console.api_chatgpt.services.advisory_service import AdvisoryError, AdvisoryService
from datamind_console.api_chatgpt.services.audit_logger import AuditLogger
from datamind_console.api_chatgpt.services.openai_client import (
    DEFAULT_DATAMIND_CHATGPT_MODEL,
    ModelCallResult,
    ModelClientProtocol,
    OpenAIResponsesClient,
)
from datamind_console.api_chatgpt.services.prompt_registry import PromptRegistry
from datamind_console.api_chatgpt.services.snapshot_builder import SnapshotBuilder


class StubModelClient(ModelClientProtocol):
    def __init__(self) -> None:
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "model_responses"
            / "analyze_latest_run_response.json"
        )
        with fixture_path.open("r", encoding="utf-8") as f:
            self.payload = json.load(f)

    def generate(
        self,
        *,
        task: str,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: dict,
        snapshot: dict,
    ) -> ModelCallResult:
        del task, system_prompt, user_prompt, schema_name, schema, snapshot
        return ModelCallResult(
            output=dict(self.payload),
            raw_response={"stub": True},
            model="stub-model",
            usage={"total_tokens": 42},
            latency_ms=3,
            schema_name="analyze_latest_run_response.json",
            sdk_version=None,
            source="stub",
        )


class _FixturePayloadMixin:
    def _fixture_payload(self) -> dict:
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "model_responses"
            / "analyze_latest_run_response.json"
        )
        with fixture_path.open("r", encoding="utf-8") as f:
            return json.load(f)


class AdvisoryE2ETests(unittest.TestCase):
    def test_openai_responses_client_defaults_to_gpt_5_2(self) -> None:
        with mock.patch.dict("os.environ", {"DATAMIND_CHATGPT_MODEL": ""}, clear=False):
            model = OpenAIResponsesClient().model
        self.assertEqual(model, DEFAULT_DATAMIND_CHATGPT_MODEL)
        self.assertEqual(model, "gpt-5.2")

    def test_fixture_snapshot_to_validated_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = AdvisoryService(
                model_client=StubModelClient(),
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(Path(td) / "audit.jsonl")),
            )

            envelope = {
                "task": "analyze_latest_run",
                "snapshot": {},
                "operator_context": {"origin": "test"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            out = service.run_task(endpoint_task="analyze_latest_run", envelope=envelope)
            self.assertEqual(out.get("task"), "analyze_latest_run")
            self.assertEqual(out.get("mode"), "advisory_only")
            self.assertIn("top_issues", out)
            self.assertIn("recommended_actions", out)

    def test_run_task_detailed_returns_meta_with_prompt_package(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = AdvisoryService(
                model_client=StubModelClient(),
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(Path(td) / "audit.jsonl")),
            )

            envelope = {
                "task": "analyze_latest_run",
                "snapshot": {},
                "operator_context": {"origin": "test"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            out = service.run_task_detailed(endpoint_task="analyze_latest_run", envelope=envelope)
            response = dict(out.get("response") or {})
            meta = dict(out.get("meta") or {})
            self.assertEqual(response.get("task"), "analyze_latest_run")
            self.assertEqual(meta.get("task"), "analyze_latest_run")
            self.assertIsInstance(meta.get("latency_ms"), int)
            self.assertIsInstance(meta.get("token_usage"), dict)
            self.assertEqual(meta.get("schema_name"), "analyze_latest_run_response.json")
            self.assertIsInstance(meta.get("prompt_package"), dict)
            self.assertIn("system_prompt", dict(meta.get("prompt_package") or {}))
            self.assertIn("user_prompt", dict(meta.get("prompt_package") or {}))

    def test_missing_prompt_template_raises_clear_advisory_error_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            audit_path = Path(td) / "audit.jsonl"
            reg_base = Path(td) / "missing_prompts_root"
            reg_base.mkdir(parents=True, exist_ok=True)

            service = AdvisoryService(
                model_client=StubModelClient(),
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                prompt_registry=PromptRegistry(base_dir=reg_base),
                audit_logger=AuditLogger(file_path=str(audit_path)),
            )

            envelope = {
                "task": "analyze_latest_run",
                "snapshot": {},
                "operator_context": {"origin": "test_missing_prompt"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            with self.assertRaises(AdvisoryError) as ctx:
                service.run_task_detailed(endpoint_task="analyze_latest_run", envelope=envelope)
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertTrue(any("prompt_registry_error" in str(err) for err in (ctx.exception.errors or [])))

            lines = [x for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertTrue(lines)
            row = json.loads(lines[-1])
            self.assertEqual(row.get("response_validity"), False)
            self.assertTrue(any("prompt_registry_error" in str(e) for e in list(row.get("validation_failures") or [])))

    def test_schema_validation_failure_returns_advisory_error_and_logs(self) -> None:
        class _InvalidInterpreterModelClient(ModelClientProtocol):
            def generate(
                self,
                *,
                task: str,
                system_prompt: str,
                user_prompt: str,
                schema_name: str,
                schema: dict,
                snapshot: dict,
            ) -> ModelCallResult:
                del task, system_prompt, user_prompt, schema_name, schema, snapshot
                return ModelCallResult(
                    output={"summary": "invalid_missing_required_fields"},
                    raw_response={"stub": True},
                    model="stub-invalid",
                    usage={"total_tokens": 5},
                    latency_ms=2,
                    schema_name="hades_pipeline_interpreter_response.json",
                    sdk_version=None,
                    source="stub",
                )

        with tempfile.TemporaryDirectory() as td:
            audit_path = Path(td) / "audit.jsonl"
            service = AdvisoryService(
                model_client=_InvalidInterpreterModelClient(),
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(audit_path)),
            )

            envelope = {
                "task": "hades_pipeline_interpreter",
                "snapshot": {
                    "phase": "phase3",
                    "step_id": "P3.2_SEQUENCE_STEP20",
                    "validator": {"status": "blocked"},
                },
                "operator_context": {"origin": "test_invalid_schema"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            with self.assertRaises(AdvisoryError) as ctx:
                service.run_task_detailed(endpoint_task="hades_pipeline_interpreter", envelope=envelope)
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("schema/policy checks", str(ctx.exception.message or ""))

            lines = [x for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertTrue(lines)
            row = json.loads(lines[-1])
            self.assertEqual(row.get("response_validity"), False)
            self.assertTrue(list(row.get("validation_failures") or []))

    def test_retest_comparator_schema_validation_failure_returns_advisory_error_and_logs(self) -> None:
        class _InvalidRetestComparatorModelClient(ModelClientProtocol):
            def generate(
                self,
                *,
                task: str,
                system_prompt: str,
                user_prompt: str,
                schema_name: str,
                schema: dict,
                snapshot: dict,
            ) -> ModelCallResult:
                del task, system_prompt, user_prompt, schema_name, schema, snapshot
                return ModelCallResult(
                    output={
                        "mode": "advisory_only",
                        "task": "hades_retest_comparator",
                        "summary": "missing required comparator fields",
                    },
                    raw_response={"stub": True},
                    model="stub-invalid-retest",
                    usage={"total_tokens": 7},
                    latency_ms=2,
                    schema_name="hades_retest_comparator_response.json",
                    sdk_version=None,
                    source="stub",
                )

        with tempfile.TemporaryDirectory() as td:
            audit_path = Path(td) / "audit.jsonl"
            service = AdvisoryService(
                model_client=_InvalidRetestComparatorModelClient(),
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(audit_path)),
            )

            envelope = {
                "task": "hades_retest_comparator",
                "snapshot": {
                    "change_type": "patch_detector_scoring",
                    "phase": "phase3",
                    "step_focus": "P3.2_SEQUENCE_STEP20",
                    "baseline_run_snapshot": {"run_id": "run-base"},
                    "retest_run_snapshot": {"run_id": "run-retest"},
                },
                "operator_context": {"origin": "test_invalid_retest_schema"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            with self.assertRaises(AdvisoryError) as ctx:
                service.run_task_detailed(endpoint_task="hades_retest_comparator", envelope=envelope)
            self.assertEqual(ctx.exception.status_code, 502)
            self.assertIn("schema/policy checks", str(ctx.exception.message or ""))

            lines = [x for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertTrue(lines)
            row = json.loads(lines[-1])
            self.assertEqual(row.get("response_validity"), False)
            self.assertTrue(list(row.get("validation_failures") or []))

    def test_real_advisory_does_not_fallback_to_mock_on_provider_failure(self) -> None:
        class _FailingRealModelClient(ModelClientProtocol):
            def generate(
                self,
                *,
                task: str,
                system_prompt: str,
                user_prompt: str,
                schema_name: str,
                schema: dict,
                snapshot: dict,
            ) -> ModelCallResult:
                del task, system_prompt, user_prompt, schema_name, schema, snapshot
                raise RuntimeError("OPENAI_API_KEY is required when DATAMIND_CHATGPT_MOCK_MODE=false")

        with tempfile.TemporaryDirectory() as td:
            audit_path = Path(td) / "audit.jsonl"
            service = AdvisoryService(
                model_client=_FailingRealModelClient(),
                requested_mode="real_advisory",
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(audit_path)),
            )

            envelope = {
                "task": "analyze_latest_run",
                "snapshot": {},
                "operator_context": {"origin": "test_real_fail_closed"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            with self.assertRaises(AdvisoryError) as ctx:
                service.run_task_detailed(endpoint_task="analyze_latest_run", envelope=envelope)

            detail = dict(ctx.exception.detail or {})
            self.assertEqual(detail.get("status"), "error")
            self.assertEqual(detail.get("source"), "real_advisory_error")
            self.assertEqual(detail.get("requested_mode"), "real_advisory")
            self.assertEqual(detail.get("error_code"), "REAL_ADVISORY_CONFIG_MISSING")
            self.assertEqual(detail.get("fallback_used"), False)
            self.assertIsNone(detail.get("model"))
            self.assertNotEqual(detail.get("source"), "mock")

            lines = [x for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertTrue(lines)
            row = json.loads(lines[-1])
            self.assertEqual(row.get("source"), "real_advisory_error")
            self.assertEqual(row.get("fallback_used"), False)

    def test_explicit_mock_mode_still_allows_mock_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = AdvisoryService(
                requested_mode="mock_advisory",
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(Path(td) / "audit.jsonl")),
            )

            envelope = {
                "task": "hades_pipeline_interpreter",
                "snapshot": {
                    "phase": "phase3",
                    "step_id": "P3.2_SEQUENCE_STEP20",
                    "validator": {"status": "blocked"},
                },
                "operator_context": {"origin": "test_mock_mode"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            out = service.run_task_detailed(endpoint_task="hades_pipeline_interpreter", envelope=envelope)
            meta = dict(out.get("meta") or {})
            self.assertEqual(meta.get("requested_mode"), "mock_advisory")
            self.assertEqual(meta.get("source"), "mock")
            self.assertEqual(meta.get("model"), "mock-fixture-model")
            self.assertEqual(meta.get("fallback_used"), False)

    def test_real_advisory_mode_mismatch_is_blocked_and_logged(self) -> None:
        class _MockLeakModelClient(_FixturePayloadMixin, ModelClientProtocol):
            def generate(
                self,
                *,
                task: str,
                system_prompt: str,
                user_prompt: str,
                schema_name: str,
                schema: dict,
                snapshot: dict,
            ) -> ModelCallResult:
                del task, system_prompt, user_prompt, schema_name, schema, snapshot
                return ModelCallResult(
                    output=self._fixture_payload(),
                    raw_response={"mock": True},
                    model="mock-fixture-model",
                    usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    latency_ms=1,
                    schema_name="analyze_latest_run_response.json",
                    sdk_version=None,
                    source="mock",
                )

        with tempfile.TemporaryDirectory() as td:
            audit_path = Path(td) / "audit.jsonl"
            service = AdvisoryService(
                model_client=_MockLeakModelClient(),
                requested_mode="real_advisory",
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(audit_path)),
            )

            envelope = {
                "task": "analyze_latest_run",
                "snapshot": {},
                "operator_context": {"origin": "test_real_mode_mismatch"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            with self.assertRaises(AdvisoryError) as ctx:
                service.run_task_detailed(endpoint_task="analyze_latest_run", envelope=envelope)

            detail = dict(ctx.exception.detail or {})
            self.assertEqual(detail.get("error_code"), "ADVISORY_MODE_MISMATCH")
            self.assertEqual(detail.get("source"), "real_advisory_error")
            self.assertIn("REAL_ADVISORY_FALLBACK_BLOCKED", list(ctx.exception.errors or []))

    def test_geography_interpreter_task_validates_against_registered_schema(self) -> None:
        case = self

        class _GeographyModelClient(ModelClientProtocol):
            def generate(
                self,
                *,
                task: str,
                system_prompt: str,
                user_prompt: str,
                schema_name: str,
                schema: dict,
                snapshot: dict,
            ) -> ModelCallResult:
                del system_prompt, user_prompt, schema, snapshot
                case.assertEqual(task, "hades_geography_interpreter")
                case.assertEqual(schema_name, "hades_geography_interpreter_response.json")
                return ModelCallResult(
                    output={
                        "mode": "advisory_only",
                        "task": "hades_geography_interpreter",
                        "interpreted_place_meaning": "Conocoto terminal corridor",
                        "interpretation_source": "hades_geography_interpreter",
                        "interpretation_status": "ok",
                        "interpretation_confidence": 0.81,
                        "bbox_candidate": {
                            "south": -0.33,
                            "west": -78.48,
                            "north": -0.22,
                            "east": -78.38,
                        },
                        "area_group_hint": "conocoto_corridor",
                        "sector_hint": "Conocoto",
                        "corridor_hint": "Conocoto",
                        "fallback_reason": None,
                    },
                    raw_response={"stub": True},
                    model="stub-geography",
                    usage={"total_tokens": 19},
                    latency_ms=2,
                    schema_name="hades_geography_interpreter_response.json",
                    sdk_version=None,
                    source="stub",
                )

        with tempfile.TemporaryDirectory() as td:
            audit_path = Path(td) / "audit.jsonl"
            service = AdvisoryService(
                model_client=_GeographyModelClient(),
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(audit_path)),
            )
            envelope = {
                "task": "hades_geography_interpreter",
                "snapshot": {
                    "phase": "phase3",
                    "original_geographic_input": "Conocoto terminal corridor",
                },
                "operator_context": {"origin": "test_geography_task"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }
            out = service.run_task_detailed(endpoint_task="hades_geography_interpreter", envelope=envelope)
            response = dict(out.get("response") or {})
            meta = dict(out.get("meta") or {})
            self.assertEqual(str(response.get("interpretation_status") or ""), "ok")
            self.assertEqual(str(response.get("interpretation_source") or ""), "hades_geography_interpreter")
            self.assertEqual(str(meta.get("schema_name") or ""), "hades_geography_interpreter_response.json")

            lines = [x for x in audit_path.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertTrue(lines)
            row = json.loads(lines[-1])
            self.assertEqual(row.get("task"), "hades_geography_interpreter")
            self.assertEqual(row.get("response_validity"), True)
            self.assertEqual(row.get("source"), "stub")
            self.assertIsNone(row.get("error_code"))

    def test_successful_real_advisory_preserves_real_metadata(self) -> None:
        class _RealSuccessModelClient(_FixturePayloadMixin, ModelClientProtocol):
            def generate(
                self,
                *,
                task: str,
                system_prompt: str,
                user_prompt: str,
                schema_name: str,
                schema: dict,
                snapshot: dict,
            ) -> ModelCallResult:
                del task, system_prompt, user_prompt, schema_name, schema, snapshot
                return ModelCallResult(
                    output=self._fixture_payload(),
                    raw_response={"openai": True},
                    model="gpt-4.1-mini",
                    usage={"input_tokens": 120, "output_tokens": 32, "total_tokens": 152},
                    latency_ms=42,
                    schema_name="analyze_latest_run_response.json",
                    sdk_version="1.99.0",
                    source="openai",
                )

        with tempfile.TemporaryDirectory() as td:
            service = AdvisoryService(
                model_client=_RealSuccessModelClient(),
                requested_mode="real_advisory",
                snapshot_builder=SnapshotBuilder(snapshot_mode="fixture"),
                audit_logger=AuditLogger(file_path=str(Path(td) / "audit.jsonl")),
            )

            envelope = {
                "task": "analyze_latest_run",
                "snapshot": {},
                "operator_context": {"origin": "test_real_success"},
                "safety_context": {
                    "mode": "advisory_only",
                    "execution_authority": "runtime_validators_operator",
                    "destructive_actions_allowed": False,
                },
            }

            out = service.run_task_detailed(endpoint_task="analyze_latest_run", envelope=envelope)
            meta = dict(out.get("meta") or {})
            self.assertEqual(meta.get("requested_mode"), "real_advisory")
            self.assertEqual(meta.get("source"), "openai")
            self.assertEqual(meta.get("model"), "gpt-4.1-mini")
            self.assertEqual(meta.get("fallback_used"), False)
            self.assertNotEqual(meta.get("model"), "mock-fixture-model")


class RealAdvisoryFallbackTests(unittest.TestCase):
    """Tests for Part 2 — Real advisory mode enforcement."""

    def test_mock_fallback_source_detected(self):
        from datamind_console.api_chatgpt.services.openai_client import is_mock_model_source
        self.assertTrue(is_mock_model_source("mock"))
        self.assertTrue(is_mock_model_source("mock_fallback"))
        self.assertTrue(is_mock_model_source("fixture"))
        self.assertFalse(is_mock_model_source("openai"))

    def test_real_advisory_blocks_mock_result(self):
        """When real_advisory is requested, a mock model result must be rejected."""

        class MockClient:
            model = "mock-fixture-model"

            def generate(self, **kwargs):
                return ModelCallResult(
                    output={"mode": "advisory_only", "task": "analyze_latest_run",
                            "summary": "test", "confidence": {"band": "low", "score": 0.3, "reasons": ["test"]},
                            "risk_flags": [], "insufficient_data_flags": [], "operator_confirmation_required": True,
                            "evidence_used": [], "quality_assessment": {}, "top_weaknesses": [],
                            "tuning_suggestions": [], "logging_gaps": [], "prioritized_improvements": []},
                    raw_response={"mock": True},
                    model="mock-fixture-model",
                    usage={"total_tokens": 0},
                    latency_ms=1,
                    schema_name="test",
                    sdk_version=None,
                    source="mock",
                )

        svc = AdvisoryService(
            model_client=MockClient(),
            requested_mode="real_advisory",
            allow_mock_fallback=False,
        )
        envelope = {
            "task": "analyze_latest_run",
            "safety_context": {
                "mode": "advisory_only",
                "execution_authority": "runtime_validators_operator",
                "destructive_actions_allowed": False,
            },
            "snapshot": {},
            "operator_context": {},
        }
        with self.assertRaises(AdvisoryError) as ctx:
            svc.run_task(endpoint_task="analyze_latest_run", envelope=envelope)
        self.assertIn("ADVISORY_MODE_MISMATCH", str(ctx.exception.detail.get("error_code", "")))

    def test_fallback_result_marked_as_mock_fallback(self):
        """When fallback is allowed, the result source must be 'mock_fallback'."""
        import traceback

        class FailingClient:
            model = "gpt-5.2"

            def generate(self, **kwargs):
                raise RuntimeError("simulated openai failure")

        svc = AdvisoryService(
            model_client=FailingClient(),
            requested_mode="mock_advisory",
            allow_mock_fallback=True,
        )
        # _call_model should fallback and mark source
        result = svc._call_model(
            task="analyze_latest_run",
            system_prompt="test",
            user_prompt="test",
            schema_name="test",
            schema={},
            snapshot={},
        )
        self.assertEqual(result.source, "mock_fallback")
        self.assertTrue(result.raw_response.get("fallback_triggered"))
        self.assertEqual(result.raw_response.get("actual_mode"), "mock_fallback")


if __name__ == "__main__":
    unittest.main()
