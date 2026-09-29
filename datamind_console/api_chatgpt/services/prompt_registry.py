from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Tuple


TASK_TO_TEMPLATE = {
    "analyze_latest_run": "analyze_latest_run_user.txt",
    "review_ai_bot_quality": "review_ai_bot_quality_user.txt",
    "hades_patch_task_generator": "hades_patch_task_generator_user.txt",
    "hades_retest_comparator": "hades_retest_comparator_user.txt",
    "generate_codex_patch_task": "generate_codex_patch_task_user.txt",
    "hades_geography_interpreter": "hades_geography_interpreter_user.txt",
    "hades_evidence_consistency_checker": "hades_evidence_consistency_checker_user.txt",
    "hades_pipeline_interpreter": "hades_pipeline_interpreter_user.txt",
    "interpret_pipeline_blocker": "interpret_pipeline_blocker_user.txt",
    "prioritize_pipeline_resolution": "prioritize_pipeline_resolution_user.txt",
    "explain_cleanup_risk": "explain_cleanup_risk_user.txt",
    "interpret_merge_evidence": "interpret_merge_evidence_user.txt",
}

TASK_TO_SYSTEM_TEMPLATE = {
    "hades_evidence_consistency_checker": "hades_evidence_consistency_checker_system.txt",
    "hades_geography_interpreter": "hades_geography_interpreter_system.txt",
    "hades_patch_task_generator": "hades_patch_task_generator_system.txt",
    "hades_pipeline_interpreter": "hades_pipeline_interpreter_system.txt",
    "hades_retest_comparator": "hades_retest_comparator_system.txt",
}


class PromptRegistry:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path(__file__).resolve().parents[1]
        self.system_prompt_dir = self.base_dir / "prompts" / "system"
        self.system_prompt_path = self.system_prompt_dir / "datamind_ai_analyst_system.txt"
        self.task_prompt_dir = self.base_dir / "prompts" / "tasks"

    def render(self, *, task: str, snapshot: Dict[str, Any], operator_context: Dict[str, Any] | None) -> Tuple[str, str]:
        task_name = str(task or "").strip()
        template_name = TASK_TO_TEMPLATE.get(task_name)
        if not template_name:
            raise ValueError(f"Unsupported task: {task}")

        system_template_name = TASK_TO_SYSTEM_TEMPLATE.get(task_name)
        system_prompt_path = self.system_prompt_path
        if system_template_name:
            system_prompt_path = self.system_prompt_dir / system_template_name
        if not system_prompt_path.exists():
            raise ValueError(f"Missing system prompt template: {system_prompt_path}")
        task_prompt_path = self.task_prompt_dir / template_name
        if not task_prompt_path.exists():
            raise ValueError(f"Missing task prompt template: {task_prompt_path}")
        system_prompt = system_prompt_path.read_text(encoding="utf-8")
        user_template = task_prompt_path.read_text(encoding="utf-8")

        snapshot_json = json.dumps(snapshot, ensure_ascii=True, indent=2, sort_keys=True)
        operator_context_json = json.dumps(operator_context or {}, ensure_ascii=True, indent=2, sort_keys=True)
        recent_history_json = json.dumps(
            snapshot.get("recent_history_summary"),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        policy_profile_json = json.dumps(
            snapshot.get("policy_profile"),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )

        user_prompt = str(user_template)
        replacements = {
            "{{snapshot_json}}": snapshot_json,
            "{{operator_context_json}}": operator_context_json,
            "{{EVENT_SNAPSHOT_JSON}}": snapshot_json,
            "{{RECENT_HISTORY_SUMMARY_JSON_OR_NULL}}": recent_history_json,
            "{{POLICY_PROFILE_JSON_OR_NULL}}": policy_profile_json,
        }
        for token, value in replacements.items():
            user_prompt = user_prompt.replace(token, value)
        return system_prompt, user_prompt
