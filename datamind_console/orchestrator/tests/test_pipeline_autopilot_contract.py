from __future__ import annotations

import os
import unittest

from datamind_console.orchestrator.pipeline_autopilot import (
    STEP_P2_3_CLEANUP,
    STEP_P3_2_STEP20,
    ApprovalType,
    PolicyProfile,
    SupervisedPipelineAutopilot,
    build_default_pipeline_step_registry,
    build_phase_client_executor_bridge,
    build_phase_client_validator_bridge,
)


def _env_true(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


@unittest.skipUnless(_env_true("HADES_AUTOPILOT_CONTRACT_TESTS", False), "set HADES_AUTOPILOT_CONTRACT_TESTS=1")
class PipelineAutopilotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        p1 = None
        p2 = None
        p3 = None

        try:
            from phases.phase1_nodes.client import _get_phase1_client

            p1 = _get_phase1_client("v3.newnodes.fix_json_approve2")
        except Exception:
            p1 = None

        try:
            from datamind_console.phases.phase2_semantics.client import Phase2Client

            p2 = Phase2Client()
        except Exception:
            p2 = None

        try:
            from datamind_console.phases.phase3_routes.client import _get_phase3_client

            p3 = _get_phase3_client()
        except Exception:
            p3 = None

        registry = build_default_pipeline_step_registry()
        cls.engine = SupervisedPipelineAutopilot(
            step_registry=registry,
            executors=build_phase_client_executor_bridge(phase1_client=p1, phase2_client=p2, phase3_client=p3),
            validators=build_phase_client_validator_bridge(),
            enabled=True,
            persist_runs=False,
        )

    def test_step20_diversion_loop_contract(self) -> None:
        route_id = str(os.getenv("HADES_CONTRACT_ROUTE_ID") or "").strip()
        if not route_id:
            self.skipTest("set HADES_CONTRACT_ROUTE_ID for Step20 contract test")

        run = self.engine.start_run(
            pipeline_scope={"phases": ["phase3"], "phase3": {"route_id": route_id}},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P3_2_STEP20,
        )
        run = self.engine.advance_run(run.run_id, max_steps=1)

        self.assertTrue(any(e.event_type == "step_started" and e.step_id == STEP_P3_2_STEP20 for e in run.events))

        if run.status == "waiting_for_approval":
            pending = self.engine.list_pending_approvals(run.run_id)
            self.assertTrue(pending)
            self.assertEqual(
                pending[0].approval_type,
                ApprovalType.RESOLVE_ROUTE_BLOCKING_UNMATCHED_AMBIGUOUS,
            )
            run = self.engine.resolve_approval(
                run_id=run.run_id,
                approval_id=pending[0].approval_id,
                decision="approved",
                operator_id=str(os.getenv("HADES_CONTRACT_OPERATOR_ID") or "contract-operator"),
                operator_role=str(os.getenv("HADES_CONTRACT_OPERATOR_ROLE") or "admin"),
                operator_decision="contract resolution",
                resolution_payload={"promote_confirmed": True, "requires_p2_partial_rerun": False},
                max_steps_after_resume=2,
            )
            self.assertTrue(any(e.event_type == "resume_triggered" for e in run.events))
            self.assertTrue(any(e.event_type == "resume_completed" for e in run.events))
        else:
            self.assertIn(run.status, {"running", "paused", "completed"})

    def test_approval_apply_contract_cleanup(self) -> None:
        run = self.engine.start_run(
            pipeline_scope={"phases": ["phase2"]},
            policy_profile=PolicyProfile.BALANCED.value,
            start_step_id=STEP_P2_3_CLEANUP,
        )
        run = self.engine.advance_run(run.run_id, max_steps=1)

        self.assertEqual(run.status, "waiting_for_approval")
        pending = self.engine.list_pending_approvals(run.run_id)
        self.assertTrue(pending)
        self.assertEqual(pending[0].approval_type, ApprovalType.RUN_DESTRUCTIVE_CLEANUP)

        if _env_true("HADES_AUTOPILOT_CONTRACT_EXECUTE_APPROVAL_APPLY", False):
            run = self.engine.resolve_approval(
                run_id=run.run_id,
                approval_id=pending[0].approval_id,
                decision="approved",
                operator_id=str(os.getenv("HADES_CONTRACT_OPERATOR_ID") or "contract-operator"),
                operator_role=str(os.getenv("HADES_CONTRACT_OPERATOR_ROLE") or "admin"),
                operator_decision="contract cleanup approval",
                resolution_payload={"dedup_radius_m": 1.5, "delete_bad_named_nodes": False},
                max_steps_after_resume=1,
            )
            self.assertIn(run.status, {"running", "paused", "waiting_for_approval", "completed"})


if __name__ == "__main__":
    unittest.main()
