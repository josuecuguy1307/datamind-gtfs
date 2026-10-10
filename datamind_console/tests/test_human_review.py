"""Human review writes: one explicit decision, guarded, audited atomically."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from datamind_console.review import human_review as hr

NODE = "f58f328b-82ea-4985-8ed8-d447a43bf49f"
Q1 = "40b33063-4cbd-48ef-8851-ca5ca08657fb"
Q2 = "50b33063-4cbd-48ef-8851-ca5ca08657fb"


class _Cursor:
    def __init__(self, db):
        self.db = db
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        up = " ".join(sql.split()).upper()
        self.db.executed.append((up, params))
        if up.startswith("UPDATE NODE_PROD.NODES"):
            state, node_id = params
            pending = self.db.nodes.get(node_id) == "pending"
            if pending:
                self.db.nodes[node_id] = state
            self._row = {"node_id": node_id} if pending else None
        elif up.startswith("UPDATE ROUTE_PROD.APPROVAL_QUEUE"):
            resolved_by, notes, queue_id = params
            row = self.db.queue.get(queue_id)
            ok = bool(row) and row["status"] == "pending" and not row.get("reentry")
            if ok:
                row.update(status="rejected", resolved_by=resolved_by, notes=notes)
            self._row = {"queue_id": queue_id} if ok else None
        elif up.startswith("INSERT INTO CONSOLE.PHASE_DECISIONS"):
            if self.db.fail_audit:
                raise RuntimeError("audit insert failed")
            self.db.decisions.append(params)
            self._row = {"decision_id": f"d{len(self.db.decisions)}", "created_at": "now"}
        else:
            raise AssertionError(f"unexpected SQL: {up[:80]}")

    def fetchone(self):
        return self._row


class _FakeDb:
    def __init__(self):
        self.nodes = {NODE: "pending"}
        self.queue = {Q1: {"status": "pending"}, Q2: {"status": "pending"}}
        self.decisions = []
        self.executed = []
        self.fail_audit = False
        self.committed = 0
        self.rolled_back = 0

    def cursor(self, *a, **k):
        return _Cursor(self)

    @contextmanager
    def transaction(self):
        # Same contract as db.transaction(): commit on success, rollback on error.
        snapshot = ({**self.nodes}, {k: dict(v) for k, v in self.queue.items()}, list(self.decisions))
        try:
            yield self
            self.committed += 1
        except Exception:
            self.nodes, self.queue, self.decisions = snapshot
            self.rolled_back += 1
            raise


@pytest.fixture()
def db():
    return _FakeDb()


def test_verify_synthetic_stop_writes_state_and_audit_together(db) -> None:
    out = hr.decide_synthetic_stop(NODE, "verified", reviewer=hr.Reviewer("Ana"), transaction_factory=db.transaction)
    assert db.nodes[NODE] == "verified" and out["state"] == "verified"
    phase, item_id, decision, reason_code, notes, user_id = db.decisions[0]
    assert (phase, item_id, decision, reason_code) == (1, f"node:{NODE}", "APPROVE", "synthetic_stop_verified")
    assert notes.startswith("[reviewer: Ana]") and user_id is None


def test_reject_synthetic_stop_requires_a_real_reason(db) -> None:
    with pytest.raises(hr.ReviewInputError, match="reason_required"):
        hr.decide_synthetic_stop(NODE, "rejected", reviewer=hr.Reviewer("Ana"), reason="bad", transaction_factory=db.transaction)
    assert db.nodes[NODE] == "pending" and not db.executed
    hr.decide_synthetic_stop(NODE, "rejected", reviewer=hr.Reviewer("Ana"),
                             reason="Placed inside a park, 300 m from the road", transaction_factory=db.transaction)
    assert db.nodes[NODE] == "rejected" and db.decisions[0][2] == "REJECT"


def test_reviewer_name_is_mandatory_without_an_account(db) -> None:
    with pytest.raises(hr.ReviewInputError, match="reviewer_required"):
        hr.decide_synthetic_stop(NODE, "verified", reviewer=hr.Reviewer(" "), transaction_factory=db.transaction)
    assert not db.executed


def test_a_second_decision_is_refused_not_overwritten(db) -> None:
    hr.decide_synthetic_stop(NODE, "verified", reviewer=hr.Reviewer("Ana"), transaction_factory=db.transaction)
    with pytest.raises(hr.ReviewConflict):
        hr.decide_synthetic_stop(NODE, "rejected", reviewer=hr.Reviewer("Luis"),
                                 reason="I disagree with this stop", transaction_factory=db.transaction)
    assert db.nodes[NODE] == "verified" and len(db.decisions) == 1


def test_failed_audit_rolls_the_decision_back(db) -> None:
    db.fail_audit = True
    with pytest.raises(RuntimeError, match="audit insert failed"):
        hr.decide_synthetic_stop(NODE, "verified", reviewer=hr.Reviewer("Ana"), transaction_factory=db.transaction)
    assert db.nodes[NODE] == "pending" and db.rolled_back == 1 and db.committed == 0


def test_reject_proposals_is_all_or_nothing(db) -> None:
    db.queue[Q2]["status"] = "approved"
    with pytest.raises(hr.ReviewConflict):
        hr.reject_proposals([Q1, Q2], reviewer=hr.Reviewer("Ana"), reason="Older version, superseded",
                            transaction_factory=db.transaction)
    assert db.queue[Q1]["status"] == "pending" and not db.decisions


def test_reject_proposals_audits_each_row(db) -> None:
    out = hr.reject_proposals([Q1, Q2], reviewer=hr.Reviewer("Ana", user_id="not-a-uuid"),
                              reason="Unroutable shard, no corridor", transaction_factory=db.transaction)
    assert [r["queue_id"] for r in out] == [Q1, Q2]
    assert all(db.queue[q]["status"] == "rejected" and db.queue[q]["resolved_by"] == "console:Ana" for q in (Q1, Q2))
    assert [d[1] for d in db.decisions] == [f"approval:{Q1}", f"approval:{Q2}"]
    assert all(d[2] == "REJECT" and d[3] == "proposal_rejected" and d[5] is None for d in db.decisions)


def test_reentry_linked_proposals_cannot_be_rejected_here(db) -> None:
    db.queue[Q1]["reentry"] = True
    with pytest.raises(hr.ReviewConflict):
        hr.reject_proposals([Q1], reviewer=hr.Reviewer("Ana"), reason="Should go through re-entry",
                            transaction_factory=db.transaction)


def test_no_approve_path_exists_for_other_proposals() -> None:
    # AI proposes, human approves: but these proposals cannot be shipped by
    # the swap service, so the module must not offer an approve function.
    assert not [name for name in dir(hr) if "approve" in name.lower() and callable(getattr(hr, name))]
    assert "no_reentry_link" in hr.why_not_approvable({"quality_class": "good"})
    assert "not_shippable_class" in hr.why_not_approvable({"quality_class": "unroutable"})
