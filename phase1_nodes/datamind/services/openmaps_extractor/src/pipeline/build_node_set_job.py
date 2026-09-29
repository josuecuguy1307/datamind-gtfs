from __future__ import annotations

import logging
from typing import Any, Dict, List

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import db_conn
from phase1_nodes.datamind.services.openmaps_extractor.src.db.phase1_repo import (
    create_node_set,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.extract_job import (
    run_extract,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.pipeline.extraction_policy import (
    AREA_GROUP_ACTION_POLICY,
    DEFAULT_AREA_GROUP,
)

logger = logging.getLogger(__name__)


def _ordered_actions(
    candidate_actions: List[str],
    area_group: str | None = None,
) -> List[str]:
    """
    Return candidate_actions sorted by the deterministic policy for the
    given area_group.  Actions not in the policy list are appended at the end.

    Replaces the former Thompson-Sampling bandit which never received
    reward updates and was therefore effectively random.
    """
    group = area_group or DEFAULT_AREA_GROUP
    policy_order = AREA_GROUP_ACTION_POLICY.get(group, [])

    ordered: List[str] = []
    remainder: List[str] = []

    for action_id in policy_order:
        if action_id in candidate_actions:
            ordered.append(action_id)

    for action_id in candidate_actions:
        if action_id not in ordered:
            remainder.append(action_id)

    return ordered + remainder


def run_build_node_set(
    actions_path: str,
    candidate_actions: List[str],
    base_params: Dict[str, Any],
    n_runs: int = 1,
    area_group: str | None = None,
    # kept for backward-compat; ignored
    bandit_key: str = "phase1_nodes_default",
) -> dict:
    """
    Create ONE node_set by running extraction with the top-priority action
    for the given area_group.

    Action selection uses the deterministic policy from extraction_policy.py
    (AREA_GROUP_ACTION_POLICY) instead of the former Thompson-Sampling bandit.

    The bandit was removed because:
    - selection_log had 0 rows (reward signal never connected)
    - Thompson Sampling without reward updates is just random noise
    - extraction_policy already encodes domain knowledge about action ordering
    """
    ordered = _ordered_actions(candidate_actions, area_group)
    chosen_action = ordered[0] if ordered else candidate_actions[0]

    logger.info(
        "build_node_set.start action=%s area_group=%s n_runs=%s",
        chosen_action,
        area_group or DEFAULT_AREA_GROUP,
        n_runs,
    )

    # --------------------------------------------------------
    # Run extraction(s)
    # --------------------------------------------------------
    run_ids: List[str] = []
    action_ids: List[str] = []

    for _ in range(n_runs):
        out = run_extract(
            action_id=chosen_action,
            params=base_params,
            actions_path=actions_path,
        )
        run_ids.append(out["run_id"])
        action_ids.append(chosen_action)

    # --------------------------------------------------------
    # Persist node_set
    # --------------------------------------------------------
    with db_conn() as conn:
        node_set_id = create_node_set(
            conn,
            source_run_ids=run_ids,
            action_ids=action_ids,
            params_used=base_params,
        )

    return {
        "node_set_id": str(node_set_id),
        "run_ids": run_ids,
        "action_ids": action_ids,
        "params": base_params,
        "area_group": area_group or DEFAULT_AREA_GROUP,
    }
