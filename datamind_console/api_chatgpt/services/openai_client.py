from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Dict, List, Protocol

DEFAULT_DATAMIND_CHATGPT_MODEL = "gpt-5.2"

MOCK_MODE_NAMES = {
    "mock",
    "mock_advisory",
    "fixture",
    "fixture_advisory",
    "fake",
    "fake_advisory",
}
REAL_MODE_NAMES = {
    "real_advisory",
}


@dataclass
class ModelCallResult:
    output: Dict[str, Any]
    raw_response: Dict[str, Any]
    model: str
    usage: Dict[str, Any]
    latency_ms: int
    schema_name: str
    sdk_version: str | None
    source: str


class ModelClientProtocol(Protocol):
    def generate(
        self,
        *,
        task: str,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> ModelCallResult:
        ...


class OpenAIResponsesClient:
    """
    Responses API wrapper with strict JSON schema output.

    Preferred request shape (OpenAI docs, March 2026):
    - client.responses.create(..., text={"format": {"type": "json_schema", "name": ..., "schema": ..., "strict": true}})
    """

    def __init__(self, *, model: str | None = None, temperature: float = 0.1) -> None:
        self.model = str(model or os.getenv("DATAMIND_CHATGPT_MODEL") or DEFAULT_DATAMIND_CHATGPT_MODEL)
        self.temperature = float(temperature)
        self.sdk_version = _detect_openai_sdk_version()

    def generate(
        self,
        *,
        task: str,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> ModelCallResult:
        del task, snapshot
        start = time.perf_counter()

        try:
            from openai import OpenAI
        except Exception as e:  # pragma: no cover
            raise RuntimeError(
                "OpenAI SDK is not installed. Install with `pip install openai`."
            ) from e

        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        messages = [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ]

        text_format = {
            "type": "json_schema",
            "name": schema_name.replace(".json", ""),
            "schema": schema,
            "strict": True,
        }

        response = None
        attempts = [
            {
                "model": self.model,
                "input": messages,
                "temperature": self.temperature,
                "tool_choice": "none",
                "text": {"format": text_format},
            },
            {
                "model": self.model,
                "input": messages,
                "temperature": self.temperature,
                "tool_choice": "none",
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name.replace(".json", ""),
                        "schema": schema,
                        "strict": True,
                    },
                },
            },
        ]

        last_err: Exception | None = None
        for payload in attempts:
            try:
                response = client.responses.create(**payload)
                last_err = None
                break
            except TypeError as e:
                last_err = e
            except Exception as e:
                last_err = e
                break

        if response is None:
            raise RuntimeError(f"OpenAI responses.create failed: {last_err}")

        parsed = _extract_parsed_json(response)
        if not isinstance(parsed, dict):
            raise RuntimeError("OpenAI structured output parsing failed; expected JSON object.")

        latency_ms = int((time.perf_counter() - start) * 1000)
        raw = _dump_response(response)

        return ModelCallResult(
            output=parsed,
            raw_response=raw,
            model=str(getattr(response, "model", self.model) or self.model),
            usage=_extract_usage(response),
            latency_ms=latency_ms,
            schema_name=schema_name,
            sdk_version=self.sdk_version,
            source="openai",
        )


class FixtureModelClient:
    """Deterministic local fallback for fixture/mock mode."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parents[1]
        self.fixture_path = (
            self.base_dir
            / "tests"
            / "fixtures"
            / "model_responses"
            / "analyze_latest_run_response.json"
        )

    def generate(
        self,
        *,
        task: str,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> ModelCallResult:
        del system_prompt, user_prompt, schema
        if task == "analyze_latest_run":
            with self.fixture_path.open("r", encoding="utf-8") as f:
                output = json.load(f)
        elif task == "review_ai_bot_quality":
            output = _mock_review_output(snapshot)
        elif task == "hades_patch_task_generator":
            output = _mock_patch_output(snapshot)
            output["task"] = "hades_patch_task_generator"
        elif task == "hades_retest_comparator":
            output = _mock_hades_retest_comparator_output(snapshot=snapshot)
        elif task == "generate_codex_patch_task":
            output = _mock_patch_output(snapshot)
        elif task == "hades_evidence_consistency_checker":
            output = _mock_hades_evidence_consistency_output(snapshot=snapshot)
        elif task == "hades_pipeline_interpreter":
            output = _mock_hades_pipeline_interpreter_output(snapshot=snapshot)
        elif task in {
            "interpret_pipeline_blocker",
            "prioritize_pipeline_resolution",
            "explain_cleanup_risk",
            "interpret_merge_evidence",
        }:
            output = _mock_pipeline_interpret_output(task=task, snapshot=snapshot)
        else:
            raise ValueError(f"Unsupported task for fixture model: {task}")

        return ModelCallResult(
            output=output,
            raw_response={"mock": True, "task": task},
            model="mock-fixture-model",
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            latency_ms=1,
            schema_name=schema_name,
            sdk_version=None,
            source="mock",
        )


def normalize_advisory_mode(mode: Any) -> str:
    return str(mode or "").strip().lower()


def is_mock_advisory_mode(mode: Any) -> bool:
    return normalize_advisory_mode(mode) in MOCK_MODE_NAMES


def is_real_advisory_mode(mode: Any) -> bool:
    return normalize_advisory_mode(mode) in REAL_MODE_NAMES


def is_mock_model_source(source: Any) -> bool:
    return str(source or "").strip().lower() in {"mock", "fixture", "fake", "mock_fallback"}


def build_default_model_client(
    *,
    mock_mode: bool | None = None,
    mode: str | None = None,
) -> ModelClientProtocol:
    normalized_mode = normalize_advisory_mode(mode)
    if is_real_advisory_mode(normalized_mode):
        use_mock = False
    elif is_mock_advisory_mode(normalized_mode):
        use_mock = True
    else:
        env_mock = str(os.getenv("DATAMIND_CHATGPT_MOCK_MODE", "true")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        use_mock = env_mock if mock_mode is None else bool(mock_mode)

    if use_mock:
        return FixtureModelClient()

    # Real mode requested; fail fast if SDK/key are missing.
    if not _detect_openai_sdk_version():
        raise RuntimeError("OpenAI SDK not installed; cannot use real model mode.")
    if not str(os.getenv("OPENAI_API_KEY") or "").strip():
        raise RuntimeError("OPENAI_API_KEY is required when DATAMIND_CHATGPT_MOCK_MODE=false")
    return OpenAIResponsesClient()


def _detect_openai_sdk_version() -> str | None:
    try:
        return metadata.version("openai")
    except metadata.PackageNotFoundError:
        return None


def _extract_parsed_json(response: Any) -> Dict[str, Any] | None:
    output_parsed = getattr(response, "output_parsed", None)
    if isinstance(output_parsed, dict):
        return output_parsed

    output = getattr(response, "output", None)
    if isinstance(output, list):
        for item in output:
            content = getattr(item, "content", None)
            if not isinstance(content, list):
                continue
            for part in content:
                parsed = getattr(part, "parsed", None)
                if isinstance(parsed, dict):
                    return parsed
                text = getattr(part, "text", None)
                if isinstance(text, str) and text.strip():
                    try:
                        obj = json.loads(text)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        return obj
    return None


def _dump_response(response: Any) -> Dict[str, Any]:
    if hasattr(response, "model_dump"):
        try:
            data = response.model_dump()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    if isinstance(response, dict):
        return response
    return {"repr": repr(response)}


def _extract_usage(response: Any) -> Dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        try:
            dumped = usage.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if isinstance(usage, dict):
        return usage
    return {}


def _confidence_from_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    insufficient = list(snapshot.get("insufficient_data_flags") or [])
    if insufficient:
        return {
            "band": "low",
            "score": 0.35,
            "reasons": [
                "Insufficient evidence flags present in snapshot",
                "Use advisory interpretation with caution",
            ],
        }
    return {
        "band": "medium",
        "score": 0.64,
        "reasons": ["Mock-mode estimate based on provided snapshot only"],
    }


def _mock_review_output(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    confidence = _confidence_from_snapshot(snapshot)
    evidence = [str(x.get("evidence_id")) for x in snapshot.get("evidence_refs") or [] if isinstance(x, dict)]
    insufficient = list(snapshot.get("insufficient_data_flags") or [])
    return {
        "mode": "advisory_only",
        "task": "review_ai_bot_quality",
        "summary": "AI Bot quality signals are present but limited by low Phase3 live evidence and absent operator labels.",
        "confidence": confidence,
        "risk_flags": [
            {
                "code": "quality_signal_bias_risk",
                "severity": "medium",
                "message": "Quality assessment may be biased due to sparse feedback labels.",
            }
        ],
        "insufficient_data_flags": insufficient,
        "operator_confirmation_required": True,
        "evidence_used": evidence,
        "quality_assessment": {
            "overall_rating": "fair",
            "signal_to_noise_ratio": 0.52,
            "coverage_assessment": "Phase1 telemetry is available, but Phase3 coverage remains shallow after scoped cleanup.",
            "readiness_bottleneck": "Operator-confirmed labels and fresh Phase3 runs",
        },
        "top_weaknesses": [
            {
                "weakness_id": "weak_low_label_volume",
                "title": "Low operator label volume",
                "severity": "high",
                "description": "There are too few operator labels to calibrate warning precision.",
                "evidence_refs": evidence[:1] or ["ev_quality_cards"],
            }
        ],
        "tuning_suggestions": [
            {
                "suggestion_id": "suggest_threshold_review",
                "description": "Review warning thresholds only after collecting fresh Phase3 runs and operator labels.",
                "expected_effect": "Reduces false confidence in threshold adjustments.",
                "evidence_refs": evidence[:1] or ["ev_quality_cards"],
            }
        ],
        "logging_gaps": [
            {
                "gap_id": "gap_operator_feedback_missing",
                "description": "Operator usefulness/correctness labels are missing from recent window.",
                "severity": "high",
                "evidence_refs": evidence[:1] or ["ev_feedback_rows_30d"],
            }
        ],
        "prioritized_improvements": [
            {
                "item_id": "improve_label_capture",
                "priority": "p0",
                "description": "Capture operator usefulness and correctness labels during Phase3 reviews.",
                "owner_hint": "ai_insights",
                "high_impact": False,
                "requires_operator_confirmation": True,
                "evidence_refs": evidence[:1] or ["ev_feedback_rows_30d"],
            }
        ],
    }


def _mock_patch_output(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    confidence = _confidence_from_snapshot(snapshot)
    evidence = [str(x.get("evidence_id")) for x in snapshot.get("evidence_refs") or [] if isinstance(x, dict)]
    insufficient = list(snapshot.get("insufficient_data_flags") or [])
    scope_files = (
        ((snapshot.get("recommended_patch_scope") or {}).get("files"))
        if isinstance(snapshot.get("recommended_patch_scope"), dict)
        else None
    )
    expected_files = [str(x) for x in (scope_files or []) if str(x).strip()]
    if not expected_files:
        expected_files = ["datamind_console/ai_insights/service.py"]

    prompt_text = (
        "You are a backend engineer working on DataMind AI Insights. "
        "Implement a small reversible patch to improve telemetry/logging clarity for low Phase3 coverage signals. "
        "Do not bypass phase gates, do not add destructive automation, and keep behavior advisory-only. "
        "Add tests for the new logging behavior and preserve existing route execution authority boundaries."
    )

    return {
        "mode": "advisory_only",
        "task": "generate_codex_patch_task",
        "summary": "Generated a scoped patch task focused on telemetry quality and advisory safety boundaries.",
        "confidence": confidence,
        "risk_flags": [
            {
                "code": "evidence_limited",
                "severity": "medium",
                "message": "Patch scope is conservative due to low evidence volume.",
            }
        ],
        "insufficient_data_flags": insufficient,
        "operator_confirmation_required": True,
        "evidence_used": evidence,
        "patch_task": {
            "title": "Improve AI Bot low-evidence telemetry signaling",
            "goal": "Increase diagnostic clarity for low Phase3 evidence without changing execution authority.",
            "scope": [
                "telemetry diagnostics",
                "insufficient-data flag clarity",
                "UI-readable advisory notes",
            ],
            "constraints": [
                "advisory_only",
                "no gate bypass",
                "no destructive actions",
                "no DB schema/migration changes",
            ],
            "prompt_text": prompt_text,
            "expected_files": expected_files,
            "acceptance_criteria": [
                "Insufficient data flags are explicit and test-covered",
                "No execution authority behavior is modified",
                "Advisory response remains schema-valid",
            ],
            "high_impact_notes": [
                "Any threshold or workflow-impacting change requires operator confirmation before rollout"
            ],
        },
    }


def _mock_hades_retest_comparator_output(*, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    baseline = dict(snapshot.get("baseline_run_snapshot") or snapshot.get("baseline") or {})
    retest = dict(snapshot.get("retest_run_snapshot") or snapshot.get("retest") or {})
    change_type = str(snapshot.get("change_type") or "tuning").strip() or "tuning"
    phase = str(snapshot.get("phase") or "").strip() or "phase3"
    step_focus = str(snapshot.get("step_focus") or "").strip() or "P3.2_SEQUENCE_STEP20"
    extra_context = dict(snapshot.get("extra_context") or {})

    def _to_float(v: Any) -> float | None:
        try:
            return float(v)
        except Exception:
            return None

    def _metric_value(run: Dict[str, Any], metric: str) -> float | None:
        metrics = dict(run.get("metrics") or {})
        if metric in metrics:
            return _to_float(metrics.get(metric))
        return _to_float(run.get(metric))

    higher_is_better = {"quality_score", "sequence_quality_score", "gate_pass_rate", "match_rate"}
    lower_is_better = {"warning_count", "unmatched_count", "ambiguous_count", "blocker_count", "retry_count"}
    metric_names = [
        "quality_score",
        "sequence_quality_score",
        "warning_count",
        "unmatched_count",
        "ambiguous_count",
    ]
    metric_deltas: List[Dict[str, Any]] = []
    improve_votes = 0
    regress_votes = 0

    for metric in metric_names:
        before = _metric_value(baseline, metric)
        after = _metric_value(retest, metric)
        if before is None or after is None:
            continue
        delta = round(float(after) - float(before), 6)
        interpretation = "no_change"
        if metric in higher_is_better:
            if delta > 0:
                interpretation = "improved"
                improve_votes += 1
            elif delta < 0:
                interpretation = "regressed"
                regress_votes += 1
        elif metric in lower_is_better:
            if delta < 0:
                interpretation = "improved"
                improve_votes += 1
            elif delta > 0:
                interpretation = "regressed"
                regress_votes += 1
        metric_deltas.append(
            {
                "metric": metric,
                "before": before,
                "after": after,
                "delta": delta,
                "interpretation": interpretation,
            }
        )

    comparability_notes: List[str] = []
    confounders: List[str] = []
    is_comparable = True

    baseline_route = str(baseline.get("route_id") or "").strip()
    retest_route = str(retest.get("route_id") or "").strip()
    if baseline_route and retest_route and baseline_route != retest_route:
        is_comparable = False
        comparability_notes.append("baseline and retest route_id differ")
        confounders.append("route_context_changed")

    baseline_phase = str(baseline.get("phase") or phase).strip()
    retest_phase = str(retest.get("phase") or phase).strip()
    if baseline_phase and retest_phase and baseline_phase != retest_phase:
        is_comparable = False
        comparability_notes.append("baseline and retest phase differ")
        confounders.append("phase_context_changed")

    baseline_step = str(baseline.get("step_id") or step_focus).strip()
    retest_step = str(retest.get("step_id") or step_focus).strip()
    if baseline_step and retest_step and baseline_step != retest_step:
        is_comparable = False
        comparability_notes.append("baseline and retest step focus differ")
        confounders.append("step_focus_changed")

    if bool(extra_context.get("threshold_config_changed")):
        confounders.append("threshold_config_changed")
    if bool(extra_context.get("policy_profile_changed")):
        confounders.append("policy_profile_changed")
    before_profile = str(extra_context.get("policy_profile_before") or "").strip()
    after_profile = str(extra_context.get("policy_profile_after") or "").strip()
    if before_profile and after_profile and before_profile != after_profile:
        confounders.append("policy_profile_changed")
    if bool(extra_context.get("manual_changes_applied")):
        confounders.append("manual_data_changes_applied")
    if bool(extra_context.get("phase2_partial_rerun")):
        confounders.append("phase2_partial_rerun_parallel_change")

    for item in list(snapshot.get("insufficient_data_flags") or []):
        txt = str(item or "").strip()
        if txt:
            confounders.append(f"insufficient_data:{txt}")

    if not metric_deltas:
        comparability_notes.append("insufficient shared numeric metrics between baseline and retest")
    if confounders:
        comparability_notes.append("confounders detected that may reduce attribution quality")

    likely_attributable = bool(is_comparable and metric_deltas and not confounders)

    if not metric_deltas:
        result = "inconclusive"
    elif improve_votes > regress_votes:
        result = "improved"
    elif regress_votes > improve_votes:
        result = "regressed"
    else:
        result = "inconclusive"

    if confounders:
        result = "inconclusive"

    recommendation = "observe_more"
    if result == "improved" and likely_attributable:
        recommendation = "accept"
    elif result == "regressed" and likely_attributable:
        recommendation = "rollback"
    elif not is_comparable:
        recommendation = "manual_review"

    confidence = 0.48
    if is_comparable:
        confidence += 0.12
    if likely_attributable:
        confidence += 0.18
    if len(metric_deltas) >= 3:
        confidence += 0.08
    if confounders:
        confidence -= min(0.25, 0.05 * float(len(confounders)))
    confidence = max(0.05, min(0.95, round(confidence, 4)))

    summary = (
        f"Retest comparison for {phase}/{step_focus} after {change_type}: "
        f"result={result}, recommendation={recommendation}, comparable={is_comparable}."
    )

    risk_notes = []
    if confounders:
        risk_notes.append(
            {
                "code": "CONFUNDED_COMPARISON",
                "severity": "medium",
                "message": "Confounders reduce confidence that metric shifts are attributable to the intended change.",
            }
        )
    if not metric_deltas:
        risk_notes.append(
            {
                "code": "METRIC_COMPARISON_WEAK",
                "severity": "high",
                "message": "Baseline and retest snapshots do not share enough numeric metrics for robust attribution.",
            }
        )
    if not likely_attributable:
        risk_notes.append(
            {
                "code": "ATTRIBUTION_UNCERTAIN",
                "severity": "medium",
                "message": "Observed changes may include non-target effects; avoid over-attributing improvement.",
            }
        )

    next_actions = []
    if recommendation == "accept":
        next_actions = [
            "Keep validator/gate thresholds unchanged and continue supervised rollout.",
            "Monitor the same corridor for 2-3 additional runs to confirm stability.",
        ]
    elif recommendation == "rollback":
        next_actions = [
            "Revert the recent tuning/patch change and rerun the affected phase/step.",
            "Open a patch follow-up focused on root-cause diagnostics before reapplying changes.",
        ]
    elif recommendation == "manual_review":
        next_actions = [
            "Run manual review because baseline and retest contexts are not directly comparable.",
            "Prepare a controlled A/B retest with matched corridor/step/policy context.",
        ]
    else:
        next_actions = [
            "Collect additional controlled retests with unchanged thresholds and policy profile.",
            "Record any parallel operational changes (manual node updates, reruns) in extra_context.",
        ]

    return {
        "mode": "advisory_only",
        "task": "hades_retest_comparator",
        "summary": summary,
        "result": result,
        "confidence": confidence,
        "comparability_assessment": {
            "is_comparable": bool(is_comparable),
            "notes": comparability_notes or ["No major comparability issues detected."],
        },
        "attribution_assessment": {
            "likely_attributable": bool(likely_attributable),
            "confounders": confounders,
        },
        "metric_deltas": metric_deltas,
        "recommendation": recommendation,
        "recommended_next_actions": next_actions,
        "risk_notes": risk_notes,
    }


def _mock_pipeline_interpret_output(*, task: str, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    confidence = _confidence_from_snapshot(snapshot)
    evidence = [str(x.get("evidence_id")) for x in snapshot.get("evidence_refs") or [] if isinstance(x, dict)]
    insufficient = list(snapshot.get("insufficient_data_flags") or [])
    base = {
        "mode": "advisory_only",
        "task": task,
        "summary": "Advisory interpretation generated for supervised pipeline decision support.",
        "confidence": confidence,
        "risk_flags": [
            {
                "code": "operator_confirmation_required",
                "severity": "medium",
                "message": "High-impact actions remain approval-gated.",
            }
        ],
        "insufficient_data_flags": insufficient,
        "operator_confirmation_required": True,
        "evidence_used": evidence,
        "recommended_actions": [
            {
                "action_id": "review_blocking_items",
                "priority": "p0",
                "description": "Review blockers with runtime evidence and resolve highest-impact issue first.",
                "high_impact": True,
                "requires_operator_confirmation": True,
                "evidence_refs": evidence[:4],
            },
            {
                "action_id": "resume_after_resolution",
                "priority": "p1",
                "description": "Resume pipeline automatically after required approvals and validations.",
                "high_impact": False,
                "requires_operator_confirmation": False,
                "evidence_refs": evidence[:4],
            },
        ],
    }
    if task == "interpret_pipeline_blocker":
        base["blocker_assessment"] = {
            "blocker_code": "STEP20_UNMATCHED_BLOCKING",
            "blocker_severity": "high",
            "recommended_next_action": "Resolve unmatched/ambiguous items in P1 New Nodes, confirm promote, then rerun Step20.",
        }
    elif task == "prioritize_pipeline_resolution":
        base["priority_order"] = ["resolve_unmatched", "resolve_ambiguous", "recheck_gate", "rerun_step20"]
    elif task == "explain_cleanup_risk":
        base["cleanup_risk"] = {
            "impact_level": "high",
            "affected_assets_estimate": 120,
            "rollback_complexity": "medium",
        }
    elif task == "interpret_merge_evidence":
        base["merge_assessment"] = {
            "confidence_label": "medium",
            "opposite_direction_likelihood": 0.62,
            "recommendation": "Keep merge/bind as approval-required and inspect top evidence pair manually.",
        }
    return base


def _mock_hades_pipeline_interpreter_output(*, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    validator = dict(snapshot.get("validator") or {})
    evidence = dict(validator.get("evidence") or {})
    warnings = list(validator.get("warnings") or [])
    status = str(validator.get("status") or "").strip().lower()
    block_reason_code = str(validator.get("block_reason_code") or "").strip()
    step_id = str(snapshot.get("step_id") or "").strip()
    phase = str(snapshot.get("phase") or "").strip()
    executor_summary = dict(snapshot.get("executor_summary") or {})
    ai_bot = dict(snapshot.get("ai_bot_snapshot") or {})
    contradictions = []
    consistency_precheck = dict(snapshot.get("consistency_precheck") or {})
    precheck_contradictions = list(consistency_precheck.get("contradictions") or [])

    def _to_int(v: Any) -> int:
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return 0

    unmatched = _to_int(evidence.get("unmatched_count"))
    ambiguous = _to_int(evidence.get("ambiguous_count"))

    resolved_total_exec = _to_int((dict(executor_summary.get("resolve") or {})).get("resolved_total"))
    resolved_count_payload = _to_int(evidence.get("resolved_count"))
    if resolved_total_exec > 0 and resolved_count_payload == 0:
        contradictions.append(
            {
                "code": "RESOLVE_COUNT_CONTRACT_MISMATCH",
                "details": (
                    f"Executor resolve total is {resolved_total_exec}, "
                    f"but validator payload resolved_count is {resolved_count_payload}."
                ),
            }
        )

    if block_reason_code == "RESOLVE_ZERO_RESULTS" and resolved_total_exec > 0:
        contradictions.append(
            {
                "code": "BLOCK_REASON_EVIDENCE_MISMATCH",
                "details": "Block reason says zero results while executor summary indicates resolved rows > 0.",
            }
        )

    dominant = "unknown"
    branch = "manual_review_required"
    confidence = 0.42
    secondary = []
    actions = []
    operator_action_required = False
    approval_type_if_needed = None
    patch = {
        "should_create_patch_task": False,
        "patch_type": None,
        "justification": None,
        "suggested_target": None,
    }
    risk_notes = []

    if contradictions:
        dominant = "diagnostics_visibility"
        branch = "patch_detector_scoring"
        confidence = 0.84
        operator_action_required = True
        patch = {
            "should_create_patch_task": True,
            "patch_type": "diagnostics",
            "justification": "Evidence contradiction indicates payload/diagnostic contract mismatch.",
            "suggested_target": "either",
        }
        actions = [
            "Freeze auto-advance for the affected step and preserve full artifacts.",
            "Create a diagnostics patch task to align executor and validator payload contracts.",
            "Re-run the same case after patch and compare block reason consistency.",
        ]
        risk_notes.append(
            {
                "code": "FALSE_BLOCK_RISK",
                "severity": "high",
                "message": "Contradictions can cause false blocking and misrouted remediation.",
            }
        )
    elif ambiguous > 0:
        dominant = "matching_ambiguity"
        branch = "phase1_new_nodes"
        confidence = 0.9
        operator_action_required = True
        approval_type_if_needed = "RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS"
        actions = [
            "Divert to P1 New Nodes ambiguous resolution queue.",
            "Operator confirms final ambiguous mapping and required promote action.",
            "Resume Step20 automatically after resolution is persisted.",
        ]
        risk_notes.append(
            {
                "code": "AMBIGUITY_GATE_ACTIVE",
                "severity": "high",
                "message": "Gate remains blocked until ambiguity resolution is operator-confirmed.",
            }
        )
    elif unmatched > 0:
        dominant = "node_db_gap"
        branch = "phase1_new_nodes"
        confidence = 0.88
        operator_action_required = True
        approval_type_if_needed = "RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS"
        actions = [
            "Route unmatched stops to P1 New Nodes with evidence prefill.",
            "Confirm promote for resolved nodes and persist resolution event.",
            "Trigger Step20 rerun (and optional P2 partial rerun if configured).",
        ]
        risk_notes.append(
            {
                "code": "NODE_GAP_BLOCKING",
                "severity": "high",
                "message": "Unmatched stops indicate data gap and block Step20 gate progression.",
            }
        )
    elif step_id == "P3.5_MERGE_OPPOSITE_DIRECTION":
        dominant = "merge_evidence"
        branch = "manual_review_required"
        confidence = 0.75
        operator_action_required = True
        approval_type_if_needed = "APPROVE_FINAL_ROUTE_OR_MERGE_BIND"
        actions = [
            "Keep merge/bind in approval queue with operator decision.",
            "Review top merge evidence pair and directional confidence before apply.",
        ]
    elif "timeout" in " ".join(str(x).lower() for x in warnings):
        dominant = "infra_timeout"
        branch = "tuning_retry"
        confidence = 0.7
        actions = [
            "Apply bounded retry with timeout-safe parameters.",
            "Collect latency telemetry and compare against baseline before escalation.",
        ]
    elif status in {"warning", "pass"}:
        dominant = "detector_thresholds"
        branch = "no_patch_continue"
        confidence = 0.58
        actions = [
            "Continue pipeline with warning traceability under current policy profile.",
            "Monitor warning subtype frequency for threshold noise patterns.",
        ]
        secondary.append(
            {
                "class": "diagnostics_visibility",
                "confidence": 0.44,
                "note": "If warning frequency keeps rising without failures, inspect detector messaging quality.",
            }
        )
    else:
        operator_action_required = True
        actions = [
            "Collect missing validator evidence and artifact references before changing branch.",
            "Keep gate authority intact and route to manual review if uncertainty persists.",
        ]

    if bool(consistency_precheck.get("contradictions_found")) and precheck_contradictions:
        dominant = "diagnostics_visibility"
        branch = "patch_detector_scoring"
        confidence = max(float(confidence), 0.8)
        operator_action_required = True
        patch = {
            "should_create_patch_task": True,
            "patch_type": "diagnostics",
            "justification": "Consistency checker pre-pass found contradictions in event payload assembly.",
            "suggested_target": "either",
        }
        actions = [
            "Prioritize contradiction fixes in validator/payload assembly before extractor tuning.",
            "Create diagnostics patch task and rerun the same corridor case.",
            "Compare contradiction set before/after patch to validate closure.",
        ]
        risk_notes.append(
            {
                "code": "PRECHECK_CONTRADICTIONS_FOUND",
                "severity": "high",
                "message": str(consistency_precheck.get("consistency_summary") or "Consistency checker detected contradictions."),
            }
        )

    if not actions:
        actions = ["Insufficient data: gather validator evidence and rerun diagnostics."]

    if not risk_notes:
        risk_notes = [
            {
                "code": "ADVISORY_ONLY",
                "severity": "medium",
                "message": "Interpretation is advisory; runtime validators and approvals remain authoritative.",
            }
        ]

    summary = (
        f"{phase or 'pipeline'} {step_id or 'step'} interpreted with dominant cause `{dominant}` "
        f"and branch `{branch}`."
    )
    ai_metrics = dict(ai_bot.get("metrics") or {})
    if ai_metrics:
        summary += f" AI metrics snapshot keys: {', '.join(sorted(ai_metrics.keys())[:4])}."

    return {
        "summary": summary,
        "dominant_cause_class": dominant,
        "confidence": float(max(0.0, min(1.0, confidence))),
        "secondary_causes": list(secondary),
        "evidence_consistency_checks": {
            "contradictions_found": bool(contradictions),
            "contradictions": contradictions,
        },
        "recommended_branch": branch,
        "recommended_next_actions": list(actions),
        "patch_task_recommendation": patch,
        "operator_action_required": bool(operator_action_required),
        "approval_type_if_needed": approval_type_if_needed,
        "risk_notes": risk_notes,
    }


def _mock_hades_evidence_consistency_output(*, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    validator = dict(snapshot.get("validator") or {})
    evidence = dict(validator.get("evidence") or {})
    executor_summary = dict(snapshot.get("executor_summary") or {})
    ai_bot = dict(snapshot.get("ai_bot_snapshot") or {})
    contradictions = []

    def _to_int(v: Any) -> int:
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return 0

    resolved_total_exec = _to_int((dict(executor_summary.get("resolve") or {})).get("resolved_total"))
    resolved_count_payload = _to_int(evidence.get("resolved_count"))
    block_reason_code = str(validator.get("block_reason_code") or "").strip()
    validator_status = str(validator.get("status") or "").strip()
    ai_warning_count = _to_int((dict(ai_bot.get("metrics") or {})).get("warning_count"))
    validator_warnings = list(validator.get("warnings") or [])

    if resolved_total_exec > 0 and resolved_count_payload == 0:
        contradictions.append(
            {
                "code": "RESOLVE_COUNT_MISMATCH",
                "severity": "high",
                "details": (
                    f"executor resolve.resolved_total={resolved_total_exec} "
                    f"but validator payload resolved_count={resolved_count_payload}"
                ),
                "likely_implication": "validator_mapping_bug",
            }
        )

    if block_reason_code == "RESOLVE_ZERO_RESULTS" and resolved_total_exec > 0:
        contradictions.append(
            {
                "code": "BLOCK_REASON_CONFLICT",
                "severity": "high",
                "details": "block_reason is RESOLVE_ZERO_RESULTS while executor has resolved rows > 0",
                "likely_implication": "stale_payload",
            }
        )

    if validator_status == "warning" and ai_warning_count == 0 and len(validator_warnings) > 0:
        contradictions.append(
            {
                "code": "AI_VALIDATOR_WARNING_DISAGREEMENT",
                "severity": "medium",
                "details": "validator indicates warnings but ai_bot metrics warning_count=0",
                "likely_implication": "scoring_inconsistency",
            }
        )

    if contradictions:
        summary = f"Detected {len(contradictions)} contradiction(s) in pipeline evidence."
    else:
        summary = "No explicit field-level contradictions detected across executor, validator, and AI Bot payloads."

    return {
        "contradictions_found": bool(contradictions),
        "contradictions": contradictions,
        "consistency_summary": summary,
    }
