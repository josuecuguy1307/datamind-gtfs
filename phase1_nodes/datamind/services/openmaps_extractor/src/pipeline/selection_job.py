from __future__ import annotations

import logging
from typing import Optional

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    db_conn,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.db.phase1_repo import (
    log_selection,
)


logger = logging.getLogger(__name__)


def run_log_selection(
    decision_id: str,
    node_set_id: str,
    chosen: bool,
    reward: Optional[float] = None,
    notes: Optional[str] = None,
) -> dict:
    """
    Phase 1 – HUMAN SELECTION LOG

    Log a human decision for a node_set.
    This feeds the bandit / learning loop.
    """

    if reward is None:
        reward = 1.0 if chosen else 0.0

    logger.info(
        "selection.run.start",
        decision_id=decision_id,
        node_set_id=node_set_id,
        chosen=chosen,
        reward=reward,
    )

    with db_conn() as conn:
        log_selection(
            conn,
            decision_id=decision_id,
            node_set_id=node_set_id,
            chosen=chosen,
            reward=reward,
            notes=notes,
        )

    logger.info(
        "selection.run.logged",
        decision_id=decision_id,
        node_set_id=node_set_id,
        chosen=chosen,
        reward=reward,
    )

    return {
        "decision_id": decision_id,
        "node_set_id": node_set_id,
        "chosen": chosen,
        "reward": reward,
    }
