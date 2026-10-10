"""Re-entry review decisions are written to console.phase_decisions (best effort)."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

from datamind_console.views.phases.phase3.tabs import re_entry_review_tab as tab


def test_decision_row_shape_and_local_operator() -> None:
    calls: list[dict[str, Any]] = []
    with patch.object(tab.console_repo, "create_phase_decision", lambda **kw: calls.append(kw)):
        tab._log_decision(queue_id="q-1", decision="REJECT", operator_id=None,
                          reason_code="reentry_reject", notes="bad geometry")
    assert calls == [{
        "user_id": None, "phase": 3, "item_id": "q-1", "decision": "REJECT",
        "reason_code": "reentry_reject", "notes": "bad geometry",
    }]


def test_non_uuid_operator_is_not_sent_as_user_fk() -> None:
    calls: list[dict[str, Any]] = []
    with patch.object(tab.console_repo, "create_phase_decision", lambda **kw: calls.append(kw)):
        tab._log_decision(queue_id="q-2", decision="APPROVE", operator_id="operator")
    assert calls[0]["user_id"] is None


def test_logging_failure_never_raises() -> None:
    def boom(**_: Any) -> None:
        raise RuntimeError("db down")

    with patch.object(tab.console_repo, "create_phase_decision", boom), patch.object(tab.st, "caption") as caption:
        tab._log_decision(queue_id="q-3", decision="APPROVE", operator_id=None)
    caption.assert_called_once()


def test_reject_and_quarantine_always_carry_a_reason_code() -> None:
    source = tab.__file__ and open(tab.__file__, encoding="utf-8").read()
    assert 'decision="REJECT", operator_id=operator_id,\n                              reason_code="reentry_reject"' in source
    assert 'reason_code="reentry_quarantine"' in source
