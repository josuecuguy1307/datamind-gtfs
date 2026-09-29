from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Set


EXECUTION_WORD_RE = re.compile(
    r"\b(executed|applied|committed|merged|reordered|deleted|dropped|bypassed|auto-pass(?:ed)?)\b",
    re.IGNORECASE,
)

HIGH_IMPACT_HINT_RE = re.compile(
    r"\b(merge|bind|reorder|cleanup|delete|drop|destructive|auto-commit|auto pass|gate bypass)\b",
    re.IGNORECASE,
)


class PolicyGuard:
    EXPECTED_MODE = "advisory_only"
    EXPECTED_AUTHORITY = "runtime_validators_operator"
    NEW_PIPELINE_TASK = "hades_pipeline_interpreter"
    CONSISTENCY_CHECK_TASK = "hades_evidence_consistency_checker"

    def validate_safety_context(self, safety_context: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        mode = str((safety_context or {}).get("mode") or "")
        authority = str((safety_context or {}).get("execution_authority") or "")
        destructive = (safety_context or {}).get("destructive_actions_allowed")

        if mode != self.EXPECTED_MODE:
            errors.append("safety_context.mode must be advisory_only")
        if authority != self.EXPECTED_AUTHORITY:
            errors.append("safety_context.execution_authority must be runtime_validators_operator")
        if destructive is not False:
            errors.append("safety_context.destructive_actions_allowed must be false")
        return errors

    def enforce_post_response(
        self,
        *,
        task: str,
        response: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> List[str]:
        errors: List[str] = []
        task_name = str(task or "").strip()
        is_new_pipeline = task_name == self.NEW_PIPELINE_TASK
        is_consistency_check = task_name == self.CONSISTENCY_CHECK_TASK

        if not is_new_pipeline and not is_consistency_check:
            if str(response.get("mode") or "") != self.EXPECTED_MODE:
                errors.append("response.mode must be advisory_only")

            if str(response.get("task") or "") != str(task):
                errors.append("response.task must match endpoint task")

        text_blob = "\n".join(self._collect_strings(response))
        if EXECUTION_WORD_RE.search(text_blob):
            errors.append("response implies execution happened, which is forbidden")

        if not is_consistency_check:
            high_impact = self._contains_high_impact(response)
            if is_new_pipeline:
                if high_impact and bool(response.get("operator_action_required")) is not True:
                    errors.append("high-impact suggestions require operator_action_required=true")
            else:
                if high_impact and bool(response.get("operator_confirmation_required")) is not True:
                    errors.append("high-impact suggestions require operator_confirmation_required=true")

            item_errors = self._enforce_item_confirmation(response, is_new_pipeline=is_new_pipeline)
            errors.extend(item_errors)

        if not is_new_pipeline and not is_consistency_check:
            unknown_evidence = self._unknown_evidence_refs(response, snapshot)
            if unknown_evidence:
                errors.append(
                    "response references unknown evidence IDs: " + ", ".join(sorted(unknown_evidence))
                )

        return errors

    def _enforce_item_confirmation(self, response: Dict[str, Any], *, is_new_pipeline: bool = False) -> List[str]:
        errors: List[str] = []

        def _check_items(items: Iterable[Dict[str, Any]], field_name: str) -> None:
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if bool(item.get("high_impact")) and bool(item.get("requires_operator_confirmation")) is not True:
                    errors.append(
                        f"{field_name}[{idx}] high_impact item must set requires_operator_confirmation=true"
                    )

        _check_items(response.get("recommended_actions") or [], "recommended_actions")
        _check_items(response.get("prioritized_improvements") or [], "prioritized_improvements")
        if is_new_pipeline and response.get("recommended_branch") in {
            "patch_extractor",
            "patch_detector_scoring",
            "patch_diagnostics",
        }:
            patch = response.get("patch_task_recommendation")
            if not isinstance(patch, dict) or bool(patch.get("should_create_patch_task")) is not True:
                errors.append(
                    "recommended_branch patch_* requires patch_task_recommendation.should_create_patch_task=true"
                )
        return errors

    def _contains_high_impact(self, response: Dict[str, Any]) -> bool:
        for s in self._collect_strings(response):
            if HIGH_IMPACT_HINT_RE.search(s):
                return True
        for key in ("recommended_actions", "prioritized_improvements"):
            for item in response.get(key) or []:
                if isinstance(item, dict) and bool(item.get("high_impact")):
                    return True
        if str(response.get("recommended_branch") or "") in {
            "phase1_new_nodes",
            "patch_extractor",
            "patch_detector_scoring",
            "patch_diagnostics",
            "manual_review_required",
        }:
            return True
        return False

    def _unknown_evidence_refs(self, response: Dict[str, Any], snapshot: Dict[str, Any]) -> Set[str]:
        known: Set[str] = set()
        for row in snapshot.get("evidence_refs") or []:
            if isinstance(row, dict):
                ev = str(row.get("evidence_id") or "").strip()
                if ev:
                    known.add(ev)
            elif isinstance(row, str):
                s = row.strip()
                if s:
                    known.add(s)

        used: Set[str] = set()
        for ev in response.get("evidence_used") or []:
            s = str(ev or "").strip()
            if s:
                used.add(s)

        for ev in self._collect_evidence_refs(response):
            if ev:
                used.add(ev)

        return {ev for ev in used if ev not in known}

    def _collect_evidence_refs(self, obj: Any) -> Set[str]:
        out: Set[str] = set()
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "evidence_refs" and isinstance(v, list):
                    for item in v:
                        s = str(item or "").strip()
                        if s:
                            out.add(s)
                else:
                    out.update(self._collect_evidence_refs(v))
        elif isinstance(obj, list):
            for item in obj:
                out.update(self._collect_evidence_refs(item))
        return out

    def _collect_strings(self, obj: Any) -> List[str]:
        out: List[str] = []
        if isinstance(obj, dict):
            for v in obj.values():
                out.extend(self._collect_strings(v))
        elif isinstance(obj, list):
            for item in obj:
                out.extend(self._collect_strings(item))
        elif isinstance(obj, str):
            text = obj.strip()
            if text:
                out.append(text)
        return out
