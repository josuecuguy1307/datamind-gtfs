from __future__ import annotations

import fnmatch
from typing import Dict, Iterable, Sequence

# ============================================================
# Step policies — role-based control tower model
# ============================================================

STEP_POLICIES: Dict[str, dict] = {
    "session_start": {
        "default_policy": "auto",
        "risk_level": "low",
        "description": "Start guided session and initialize operator context.",
        "allowed_files": [],
    },
    "define_work_mode": {
        "default_policy": "auto",
        "risk_level": "low",
        "description": "Persist template/mode badges and preferred dispatch target.",
        "allowed_files": [],
    },
    "load_ai_bot_snapshot": {
        "default_policy": "auto",
        "risk_level": "low",
        "description": "Load structured AI Bot snapshot (live/fixture).",
        "allowed_files": ["datamind_console/orchestrator_logs/*"],
    },
    "verify_ai_bot_health": {
        "default_policy": "gate",
        "risk_level": "medium",
        "description": "Operator checkpoint for AI Bot readiness and warning acceptance.",
        "allowed_files": ["datamind_console/orchestrator_logs/*"],
    },
    "analyze_latest_run": {
        "default_policy": "auto",
        "risk_level": "medium",
        "description": "Advisory interpretation of latest run over AI Bot snapshot.",
        "allowed_files": ["datamind_console/orchestrator_logs/*"],
    },
    "review_ai_bot_quality": {
        "default_policy": "auto",
        "risk_level": "medium",
        "description": "Assess AI Bot usefulness, gaps, and label coverage.",
        "allowed_files": ["datamind_console/orchestrator_logs/*"],
    },
    "generate_codex_patch_task": {
        "default_policy": "auto",
        "risk_level": "high",
        "description": "Generate structured patch task for Codex/Claude.",
        "allowed_files": ["datamind_console/orchestrator_logs/*", "local_runner/prompts/*"],
    },
    "operator_dispatch_checkpoint": {
        "default_policy": "gate",
        "risk_level": "critical",
        "description": "Mandatory operator decision before any assistant dispatch.",
        "allowed_files": [],
    },
    "prepare_prompt_artifact": {
        "default_policy": "auto",
        "risk_level": "high",
        "description": "Prepare final prompt artifact for local runner target.",
        "allowed_files": ["local_runner/prompts/*"],
    },
    "dispatch_to_assistant": {
        "default_policy": "auto",
        "risk_level": "critical",
        "description": "Dispatch prompt to Codex/Claude through local runner.",
        "allowed_files": ["local_runner/prompts/*", "local_runner/outputs/*"],
    },
    "review_assistant_output": {
        "default_policy": "gate",
        "risk_level": "critical",
        "description": "Operator review of assistant output (no auto-apply).",
        "allowed_files": ["local_runner/outputs/*"],
    },
    "apply_patch_and_retest": {
        "default_policy": "gate",
        "risk_level": "critical",
        "description": "Human-supervised patch application and validator/test checkpoint.",
        "allowed_files": [],
    },
    "operate_phase_workflows": {
        "default_policy": "gate",
        "risk_level": "critical",
        "description": "Phase1/2/3 operation with runtime and validators as authority.",
        "allowed_files": [],
    },
    "capture_operator_labels": {
        "default_policy": "gate",
        "risk_level": "high",
        "description": "Capture operator labels and close feedback loop.",
        "allowed_files": ["datamind_console/orchestrator_logs/*", "labels/*"],
    },
    "session_summary": {
        "default_policy": "auto",
        "risk_level": "low",
        "description": "Finalize session summary, artifacts, and governance status.",
        "allowed_files": ["datamind_console/orchestrator_logs/*"],
    },
}

# ============================================================
# Policy profiles
# ============================================================

POLICY_PROFILES: Dict[str, Dict[str, str]] = {
    "conservative": {
        "verify_ai_bot_health": "gate",
        "analyze_latest_run": "gate",
        "review_ai_bot_quality": "gate",
        "generate_codex_patch_task": "gate",
        "operator_dispatch_checkpoint": "gate",
        "prepare_prompt_artifact": "gate",
        "dispatch_to_assistant": "gate",
        "review_assistant_output": "gate",
        "apply_patch_and_retest": "gate",
        "operate_phase_workflows": "gate",
        "capture_operator_labels": "gate",
    },
    "balanced": {},
    "aggressive_supervised": {
        "verify_ai_bot_health": "auto",
        "analyze_latest_run": "auto",
        "review_ai_bot_quality": "auto",
        "generate_codex_patch_task": "auto",
        "operator_dispatch_checkpoint": "gate",
        "prepare_prompt_artifact": "auto",
        "dispatch_to_assistant": "auto",
        "review_assistant_output": "gate",
        "apply_patch_and_retest": "gate",
        "operate_phase_workflows": "gate",
        "capture_operator_labels": "gate",
    },
}

# ============================================================
# File scope guardrails
# ============================================================

FILE_SCOPE_GUARDRAILS = {
    "read_only": [
        "datamind_console/**/*.py",
        "datamind_core/**/*.py",
        "phase*/**/*.py",
        "docs/**/*",
    ],
    "writable": [
        "datamind_console/orchestrator_logs/*",
        "local_runner/prompts/*",
        "local_runner/outputs/*",
        "labels/*",
    ],
    "forbidden": [
        "**/.env",
        "**/.env.*",
        "**/credentials*",
        "**/secrets*",
        "**/*.pem",
        "**/*.key",
        "phase1_nodes/**",
        "phase2_semantics/**",
        "phase3_routes/**",
        "validators/core/**",
        "**/migrations/**",
    ],
}


# ============================================================
# Public API
# ============================================================

def get_effective_policy(step_key: str, profile: str = "balanced") -> str:
    normalized = _normalize_profile(profile)
    overrides = POLICY_PROFILES.get(normalized, {})
    if step_key in overrides:
        return overrides[step_key]
    step_cfg = STEP_POLICIES.get(step_key)
    if step_cfg:
        return step_cfg["default_policy"]
    return "gate"


def should_auto_advance(
    step_key: str,
    profile: str = "balanced",
    dry_run: bool = False,
    confidence: float | None = None,
    risk_flags: Sequence[str] | None = None,
    files: Iterable[str] | None = None,
) -> bool:
    if dry_run:
        return True
    if get_effective_policy(step_key, profile) != "auto":
        return False
    if confidence is not None:
        try:
            if float(confidence) < 0.55:
                return False
        except Exception:
            return False
    flags = {str(x or "").strip().lower() for x in (risk_flags or []) if str(x or "").strip()}
    if flags & {"high_impact", "destructive", "approval_required"}:
        return False
    for path in files or []:
        if not validate_file_scope(str(path or ""), operation="write"):
            return False
    return True


def get_risk_level(step_key: str) -> str:
    step_cfg = STEP_POLICIES.get(step_key)
    if step_cfg:
        return step_cfg["risk_level"]
    return "unknown"


def validate_file_scope(path: str, operation: str = "read") -> bool:
    normalized = path.replace("\\", "/")
    for pattern in FILE_SCOPE_GUARDRAILS["forbidden"]:
        if fnmatch.fnmatch(normalized, pattern):
            return False
    if operation == "write":
        return any(
            fnmatch.fnmatch(normalized, p)
            for p in FILE_SCOPE_GUARDRAILS["writable"]
        )
    return True


def _normalize_profile(profile: str) -> str:
    raw = str(profile or "").strip().lower()
    if raw in {"conservative", "cautious"}:
        return "conservative"
    if raw in {"aggressive_supervised", "aggressive"}:
        return "aggressive_supervised"
    return "balanced"
