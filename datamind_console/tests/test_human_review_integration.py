"""Human review writes against real Postgres (constraints, guards, RETURNING).

Opt-in and disposable-only, like test_trash_repo: runs only when
DATAMIND_DISPOSABLE_TEST_DSN names a database whose name contains "test",
"scratch" or "disposable", and is not datamind_ml. The database needs the
schema of console.phase_decisions, node_prod.nodes, route_prod.approval_queue,
route_prod.routes and route_raw.route_jobs (``pg_dump -s -t ...``).
"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import parse_dsn  # noqa: E402

from datamind_console.review import human_review as hr  # noqa: E402

DSN = os.environ.get("DATAMIND_DISPOSABLE_TEST_DSN", "").strip()


def _disposable(dsn: str) -> bool:
    try:
        name = (parse_dsn(dsn).get("dbname") or "").lower()
    except Exception:
        return False
    return bool(name) and name != "datamind_ml" and any(m in name for m in ("test", "scratch", "disposable"))


pytestmark = pytest.mark.skipif(not _disposable(DSN), reason="set DATAMIND_DISPOSABLE_TEST_DSN to a disposable database")


@contextmanager
def _tx():
    conn = psycopg2.connect(DSN)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@pytest.fixture()
def seeded():
    node_id, q1, q2, q_re = (str(uuid.uuid4()) for _ in range(4))
    with _tx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO node_prod.nodes (node_id, geom, node_type, name, source, synthetic_review_state, synthetic_confidence) "
            "VALUES (%s, ST_SetSRID(ST_MakePoint(-78.5, -0.2), 4326), 'STOP', 'Test stop', 'synthetic', 'pending', 'medium')",
            (node_id,),
        )
        for qid, flags in ((q1, "{}"), (q2, "{}"), (q_re, '{"re_entry_queue_id": "x"}')):
            cur.execute(
                "INSERT INTO route_prod.approval_queue (queue_id, route_code, version, policy_profile, status, policy_flags, "
                " decision_reasons, geometry_report, stop_coverage_report, proposed_stops, proposed_shape) "
                "VALUES (%s, %s, 1, 'balanced', 'pending', %s::jsonb, '[]', '{}', '{}', '[]', '{}')",
                (qid, str(uuid.uuid4()), flags),
            )
    yield {"node": node_id, "q1": q1, "q2": q2, "q_re": q_re}
    with _tx() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM console.phase_decisions WHERE item_id = ANY(%s)",
                    ([f"node:{node_id}", f"approval:{q1}", f"approval:{q2}", f"approval:{q_re}"],))
        cur.execute("DELETE FROM route_prod.approval_queue WHERE queue_id = ANY(%s::uuid[])", ([q1, q2, q_re],))
        cur.execute("DELETE FROM node_prod.nodes WHERE node_id = %s", (node_id,))


def _one(sql, params):
    with _tx() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def test_verify_then_refuse_second_decision(seeded) -> None:
    hr.decide_synthetic_stop(seeded["node"], "verified", reviewer=hr.Reviewer("Integration"), transaction_factory=_tx)
    assert _one("SELECT synthetic_review_state FROM node_prod.nodes WHERE node_id=%s", (seeded["node"],))[0] == "verified"
    assert _one("SELECT count(*) FROM console.phase_decisions WHERE item_id=%s AND decision='APPROVE'", (f"node:{seeded['node']}",))[0] == 1
    with pytest.raises(hr.ReviewConflict):
        hr.decide_synthetic_stop(seeded["node"], "rejected", reviewer=hr.Reviewer("Integration"),
                                 reason="second opinion attempt", transaction_factory=_tx)


def test_reject_proposals_respects_constraints_and_reentry_guard(seeded) -> None:
    hr.reject_proposals([seeded["q1"], seeded["q2"]], reviewer=hr.Reviewer("Integration"),
                        reason="Superseded unroutable shard", transaction_factory=_tx)
    status, resolved_at, by = _one("SELECT status, resolved_at, resolved_by FROM route_prod.approval_queue WHERE queue_id=%s", (seeded["q1"],))
    assert status == "rejected" and resolved_at is not None and by == "console:Integration"
    with pytest.raises(hr.ReviewConflict):
        hr.reject_proposals([seeded["q_re"]], reviewer=hr.Reviewer("Integration"),
                            reason="must go through re-entry", transaction_factory=_tx)
    assert _one("SELECT status FROM route_prod.approval_queue WHERE queue_id=%s", (seeded["q_re"],))[0] == "pending"
