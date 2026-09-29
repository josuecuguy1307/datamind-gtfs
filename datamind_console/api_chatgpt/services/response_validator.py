from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from jsonschema import Draft202012Validator


TASK_TO_RESPONSE_SCHEMA = {
    "analyze_latest_run": "analyze_latest_run_response.json",
    "review_ai_bot_quality": "review_ai_bot_quality_response.json",
    "hades_patch_task_generator": "hades_patch_task_generator_response.json",
    "hades_retest_comparator": "hades_retest_comparator_response.json",
    "generate_codex_patch_task": "generate_codex_patch_task_response.json",
    "hades_geography_interpreter": "hades_geography_interpreter_response.json",
    "hades_evidence_consistency_checker": "hades_evidence_consistency_checker_response.json",
    "hades_pipeline_interpreter": "hades_pipeline_interpreter_response.json",
    "interpret_pipeline_blocker": "interpret_pipeline_blocker_response.json",
    "prioritize_pipeline_resolution": "prioritize_pipeline_resolution_response.json",
    "explain_cleanup_risk": "explain_cleanup_risk_response.json",
    "interpret_merge_evidence": "interpret_merge_evidence_response.json",
}


class ResponseValidator:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parents[1]
        self.schemas_dir = self.base_dir / "schemas"
        self._schema_cache: Dict[str, Dict[str, Any]] = {}

    def _load_schema(self, rel_path: str) -> Dict[str, Any]:
        if rel_path not in self._schema_cache:
            path = self.schemas_dir / rel_path
            with path.open("r", encoding="utf-8") as f:
                self._schema_cache[rel_path] = json.load(f)
        return dict(self._schema_cache[rel_path])

    def request_schema(self) -> Dict[str, Any]:
        return self._load_schema("common/ai_bot_task_request_envelope.json")

    def response_schema_for_task(self, task: str) -> Dict[str, Any]:
        name = TASK_TO_RESPONSE_SCHEMA.get(str(task or "").strip())
        if not name:
            raise ValueError(f"Unsupported task: {task}")
        return self._load_schema(f"outputs/{name}")

    def response_schema_name_for_task(self, task: str) -> str:
        name = TASK_TO_RESPONSE_SCHEMA.get(str(task or "").strip())
        if not name:
            raise ValueError(f"Unsupported task: {task}")
        return name

    def validate_request_envelope(self, payload: Dict[str, Any]) -> List[str]:
        return self._validate(payload, self.request_schema())

    def validate_task_response(self, *, task: str, payload: Dict[str, Any]) -> List[str]:
        return self._validate(payload, self.response_schema_for_task(task))

    @staticmethod
    def _validate(payload: Dict[str, Any], schema: Dict[str, Any]) -> List[str]:
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
        out: List[str] = []
        for e in errors:
            if e.path:
                where = ".".join(str(x) for x in e.path)
                out.append(f"{where}: {e.message}")
            else:
                out.append(str(e.message))
        return out
