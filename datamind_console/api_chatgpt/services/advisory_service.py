from __future__ import annotations

import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List

from datamind_console.api_chatgpt.services.audit_logger import AuditLogger
from datamind_console.api_chatgpt.services.openai_client import (
    FixtureModelClient,
    ModelCallResult,
    ModelClientProtocol,
    build_default_model_client,
    is_mock_model_source,
    is_real_advisory_mode,
    normalize_advisory_mode,
)
from datamind_console.api_chatgpt.services.policy_guard import PolicyGuard
from datamind_console.api_chatgpt.services.prompt_registry import PromptRegistry
from datamind_console.api_chatgpt.services.response_validator import ResponseValidator
from datamind_console.api_chatgpt.services.snapshot_builder import SnapshotBuilder


@dataclass
class AdvisoryError(Exception):
    message: str
    errors: List[str]
    status_code: int = 400
    detail: Dict[str, Any] = field(default_factory=dict)


class AdvisoryService:
    def __init__(
        self,
        *,
        validator: ResponseValidator | None = None,
        policy_guard: PolicyGuard | None = None,
        prompt_registry: PromptRegistry | None = None,
        snapshot_builder: SnapshotBuilder | None = None,
        audit_logger: AuditLogger | None = None,
        model_client: ModelClientProtocol | None = None,
        requested_mode: str | None = None,
        allow_mock_fallback: bool | None = None,
    ) -> None:
        self.validator = validator or ResponseValidator()
        self.policy_guard = policy_guard or PolicyGuard()
        self.prompt_registry = prompt_registry or PromptRegistry()
        self.snapshot_builder = snapshot_builder or SnapshotBuilder()
        self.audit_logger = audit_logger or AuditLogger()
        self.requested_mode = normalize_advisory_mode(requested_mode)
        env_allow_fallback = str(os.getenv("DATAMIND_CHATGPT_ALLOW_MOCK_FALLBACK", "true")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if allow_mock_fallback is None:
            self.allow_mock_fallback = bool(env_allow_fallback and not is_real_advisory_mode(self.requested_mode))
        else:
            self.allow_mock_fallback = bool(allow_mock_fallback)
        self.model_client = model_client or build_default_model_client(mode=self.requested_mode or None)
        if self._real_mode_enabled():
            model_str = getattr(self.model_client, "model", None)
            if model_str and not str(model_str).startswith("gpt-5"):
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "ADVISORY_MODEL_VERSION_WARNING: expected gpt-5.x series, got %s", model_str,
                )

    def _real_mode_enabled(self) -> bool:
        return is_real_advisory_mode(self.requested_mode)

    def _failure_payload(
        self,
        *,
        task: str,
        schema_name: str,
        snapshot_id: Any,
        error_code: str,
        error_summary: str,
        model: Any = None,
    ) -> Dict[str, Any]:
        return {
            "task": str(task or ""),
            "status": "error",
            "source": "real_advisory_error" if self._real_mode_enabled() else "advisory_error",
            "requested_mode": (self.requested_mode or None),
            "model": (str(model) if model else None),
            "error_code": str(error_code or "ADVISORY_ERROR"),
            "error_summary": str(error_summary or "").strip() or None,
            "fallback_used": False,
            "token_usage": None,
            "snapshot_id": (str(snapshot_id) if snapshot_id else None),
            "schema_name": (str(schema_name) if schema_name else None),
        }

    def _log_failure(
        self,
        *,
        task: str,
        schema_name: str,
        snapshot_id: Any,
        error_code: str,
        error_summary: str,
        status_code: int,
        errors: List[str],
        model: Any = None,
        source: Any = None,
    ) -> None:
        self.audit_logger.log_call(
            {
                "task": task,
                "snapshot_id": snapshot_id,
                "model": (str(model) if model else None),
                "schema_name": schema_name,
                "latency_ms": 0,
                "token_usage": {},
                "confidence_score": None,
                "response_validity": False,
                "validation_failures": list(errors or []),
                "operator_outcome": None,
                "source": (str(source) if source else ("real_advisory_error" if self._real_mode_enabled() else "local")),
                "sdk_version": None,
                "requested_mode": (self.requested_mode or None),
                "fallback_used": False,
                "error_code": str(error_code or ""),
                "status_code": int(status_code),
                "error_summary": str(error_summary or ""),
            }
        )

    def _raise_failure(
        self,
        *,
        task: str,
        schema_name: str,
        snapshot_id: Any,
        message: str,
        error_code: str,
        error_summary: str,
        errors: List[str],
        status_code: int,
        model: Any = None,
        source: Any = None,
    ) -> None:
        payload = self._failure_payload(
            task=task,
            schema_name=schema_name,
            snapshot_id=snapshot_id,
            error_code=error_code,
            error_summary=error_summary,
            model=model,
        )
        self._log_failure(
            task=task,
            schema_name=schema_name,
            snapshot_id=snapshot_id,
            error_code=error_code,
            error_summary=error_summary,
            status_code=status_code,
            errors=errors,
            model=model,
            source=source,
        )
        raise AdvisoryError(message, errors=list(errors or []), status_code=status_code, detail=payload)

    @staticmethod
    def _classify_model_error(exc: Exception) -> str:
        lower = str(exc or "").strip().lower()
        if "openai_api_key" in lower or "api key" in lower:
            return "REAL_ADVISORY_CONFIG_MISSING"
        if "sdk" in lower or "not installed" in lower:
            return "REAL_ADVISORY_PROVIDER_UNAVAILABLE"
        return "REAL_ADVISORY_REQUEST_FAILED"

    def run_task(self, *, endpoint_task: str, envelope: Dict[str, Any]) -> Dict[str, Any]:
        detailed = self.run_task_detailed(endpoint_task=endpoint_task, envelope=envelope)
        return dict(detailed.get("response") or {})

    def run_task_detailed(self, *, endpoint_task: str, envelope: Dict[str, Any]) -> Dict[str, Any]:
        env = dict(envelope or {})
        errors = self.validator.validate_request_envelope(env)

        declared_task = str(env.get("task") or "")
        if declared_task != endpoint_task:
            errors.append(f"request.task must equal {endpoint_task}")

        safety_context = dict(env.get("safety_context") or {})
        errors.extend(self.policy_guard.validate_safety_context(safety_context))

        if errors:
            raise AdvisoryError("Invalid request envelope.", errors=errors, status_code=422)

        operator_context = dict(env.get("operator_context") or {})
        snapshot = self.snapshot_builder.build(
            task=endpoint_task,
            supplied_snapshot=(env.get("snapshot") if isinstance(env.get("snapshot"), dict) else {}),
            operator_context=operator_context,
        )

        schema_name = self.validator.response_schema_name_for_task(endpoint_task)
        schema = self.validator.response_schema_for_task(endpoint_task)

        try:
            system_prompt, user_prompt = self.prompt_registry.render(
                task=endpoint_task,
                snapshot=snapshot,
                operator_context=operator_context,
            )
        except Exception as exc:
            err = f"prompt_registry_error: {exc}"
            self._raise_failure(
                task=endpoint_task,
                schema_name=schema_name,
                snapshot_id=snapshot.get("snapshot_id"),
                message="Prompt rendering failed.",
                error_code=(
                    "REAL_ADVISORY_PROMPT_RENDER_FAILED"
                    if self._real_mode_enabled()
                    else "PROMPT_RENDER_FAILED"
                ),
                error_summary=str(exc),
                errors=[err],
                status_code=500,
                source="local",
            )

        try:
            model_result = self._call_model(
                task=endpoint_task,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_name=schema_name,
                schema=schema,
                snapshot=snapshot,
            )
        except AdvisoryError:
            raise
        except Exception as exc:
            err = f"model_call_error: {exc}"
            self._raise_failure(
                task=endpoint_task,
                schema_name=schema_name,
                snapshot_id=snapshot.get("snapshot_id"),
                message="Model call failed.",
                error_code=(
                    self._classify_model_error(exc)
                    if self._real_mode_enabled()
                    else "MODEL_CALL_FAILED"
                ),
                error_summary=str(exc),
                errors=[err],
                status_code=502,
                source="local",
            )

        response = dict(model_result.output or {})
        if self._real_mode_enabled() and (
            is_mock_model_source(model_result.source) or str(model_result.model or "").strip() == "mock-fixture-model"
        ):
            err = (
                f"ADVISORY_MODE_MISMATCH: requested real_advisory but model client returned "
                f"source={model_result.source!r} model={model_result.model!r}"
            )
            self._raise_failure(
                task=endpoint_task,
                schema_name=schema_name,
                snapshot_id=snapshot.get("snapshot_id"),
                message="Real advisory fallback was blocked.",
                error_code="ADVISORY_MODE_MISMATCH",
                error_summary=err,
                errors=[err, "REAL_ADVISORY_FALLBACK_BLOCKED"],
                status_code=502,
                model=model_result.model,
                source=model_result.source,
            )
        response_errors = self.validator.validate_task_response(task=endpoint_task, payload=response)
        guard_errors = self.policy_guard.enforce_post_response(
            task=endpoint_task,
            response=response,
            snapshot=snapshot,
        )
        all_response_errors = response_errors + guard_errors

        confidence_score = None
        if isinstance(response.get("confidence"), dict):
            score = response.get("confidence", {}).get("score")
            try:
                confidence_score = float(score)
            except Exception:
                confidence_score = None

        self.audit_logger.log_call(
            {
                "task": endpoint_task,
                "snapshot_id": snapshot.get("snapshot_id"),
                "model": model_result.model,
                "schema_name": schema_name,
                "latency_ms": int(model_result.latency_ms),
                "token_usage": model_result.usage,
                "confidence_score": confidence_score,
                "response_validity": len(all_response_errors) == 0,
                "validation_failures": all_response_errors,
                "operator_outcome": None,
                "source": model_result.source,
                "sdk_version": model_result.sdk_version,
                "requested_mode": (self.requested_mode or None),
                "fallback_used": False,
            }
        )

        if all_response_errors:
            self._raise_failure(
                task=endpoint_task,
                schema_name=schema_name,
                snapshot_id=snapshot.get("snapshot_id"),
                message="Model response failed schema/policy checks.",
                error_code=(
                    "REAL_ADVISORY_SCHEMA_INVALID"
                    if self._real_mode_enabled()
                    else "MODEL_RESPONSE_INVALID"
                ),
                error_summary="; ".join(str(x) for x in list(all_response_errors or [])[:8]),
                errors=list(all_response_errors or []),
                status_code=502,
                model=model_result.model,
                source=model_result.source,
            )

        return {
            "response": response,
            "meta": {
                "task": endpoint_task,
                "snapshot_id": snapshot.get("snapshot_id"),
                "model": model_result.model,
                "latency_ms": int(model_result.latency_ms),
                "token_usage": dict(model_result.usage or {}),
                "schema_name": schema_name,
                "source": model_result.source,
                "sdk_version": model_result.sdk_version,
                "requested_mode": (self.requested_mode or None),
                "fallback_used": False,
                "prompt_package": {
                    "task": endpoint_task,
                    "schema_name": schema_name,
                    "safety_context": safety_context,
                    "snapshot": snapshot,
                    "operator_context": operator_context,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                },
            },
        }

    def _call_model(
        self,
        *,
        task: str,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> ModelCallResult:
        try:
            return self.model_client.generate(
                task=task,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_name=schema_name,
                schema=schema,
                snapshot=snapshot,
            )
        except Exception as e:
            if self._real_mode_enabled():
                err = f"model_call_error: {e}"
                self._raise_failure(
                    task=task,
                    schema_name=schema_name,
                    snapshot_id=snapshot.get("snapshot_id"),
                    message="Model call failed.",
                    error_code=self._classify_model_error(e),
                    error_summary=str(e),
                    errors=[err],
                    status_code=502,
                    source="real_advisory_error",
                )
            if not self.allow_mock_fallback:
                raise

            import logging as _logging
            _logging.getLogger(__name__).warning(
                "ADVISORY_FALLBACK_TO_MOCK: real model call failed, falling back to fixture. "
                "requested_mode=%s error=%s",
                self.requested_mode, e,
            )
            fallback = FixtureModelClient()
            result = fallback.generate(
                task=task,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                schema_name=schema_name,
                schema=schema,
                snapshot=snapshot,
            )
            result.raw_response = {
                "fallback_reason": str(e),
                "fallback_triggered": True,
                "requested_mode": (self.requested_mode or None),
                "actual_mode": "mock_fallback",
                "trace": traceback.format_exc(limit=3),
            }
            result.source = "mock_fallback"
            return result
