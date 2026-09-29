from __future__ import annotations

from contextlib import contextmanager
import unittest
import uuid
from unittest.mock import patch

from datamind_console.phases.phase3_routes.client import Phase3Client


def _candidate_row(osm_relation_id: int, *, score: float, rank: int) -> dict:
    return {
        "osm_relation_id": int(osm_relation_id),
        "tags": {
            "_datamind_candidate_meta": {
                "score": float(score),
                "stop_prior_count": 5,
                "selection_rank": int(rank),
                "selection_confidence": 0.8,
                "matched_soft_signals": [],
                "selection_reason_codes": ["prefer_route_relation"],
            }
        },
        "ref": f"R{osm_relation_id}",
        "name": f"Relation {osm_relation_id}",
        "operator": "DATAMIND",
        "route_mode": "bus",
        "rel_type": "route",
        "found_at": "2026-03-01T00:00:00Z",
    }


class _RelationChoiceCursor:
    def __init__(self, conn: "_RelationChoiceConn") -> None:
        self.conn = conn
        self._rows: list[dict] = []

    def execute(self, sql, params=None) -> None:
        normalized = " ".join(str(sql).split())
        self.conn.executed.append((normalized, params))

        if "UPDATE route_raw.route_jobs" in normalized and "SET chosen_osm_relation_id = %s" in normalized:
            chosen_relation_id, route_id = params
            if str(route_id) != self.conn.route_id:
                raise AssertionError(f"unexpected route_id {route_id}")
            self.conn.working_chosen_relation_id = int(chosen_relation_id)
            self._rows = []
            return

        if "UPDATE route_raw.relation_candidates" in normalized and "SET is_chosen" in normalized:
            raise RuntimeError("column relation_candidates.is_chosen does not exist")

        if "FROM route_raw.relation_candidates rc" in normalized:
            route_id = str(params[0])
            if route_id != self.conn.route_id:
                raise AssertionError(f"unexpected route_id {route_id}")
            self._rows = [
                {
                    **dict(row),
                    "is_chosen": bool(int(row["osm_relation_id"]) == int(self.conn.working_chosen_relation_id)),
                }
                for row in self.conn.candidates
            ]
            return

        if "FROM route_raw.relation_candidates" in normalized:
            route_id = str(params[0])
            if route_id != self.conn.route_id:
                raise AssertionError(f"unexpected route_id {route_id}")
            self._rows = [dict(row) for row in self.conn.candidates]
            return

        raise AssertionError(f"unexpected SQL: {normalized}")

    def fetchall(self):
        return list(self._rows)

    def close(self) -> None:
        return None


class _RelationChoiceConn:
    def __init__(self, *, route_id: uuid.UUID, chosen_relation_id: int, candidates: list[dict]) -> None:
        self.route_id = str(route_id)
        self.persisted_chosen_relation_id = int(chosen_relation_id)
        self.working_chosen_relation_id = int(chosen_relation_id)
        self.candidates = [dict(row) for row in candidates]
        self.executed: list[tuple[str, object]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _RelationChoiceCursor:
        return _RelationChoiceCursor(self)

    def commit(self) -> None:
        self.commits += 1
        self.persisted_chosen_relation_id = int(self.working_chosen_relation_id)

    def rollback(self) -> None:
        self.rollbacks += 1
        self.working_chosen_relation_id = int(self.persisted_chosen_relation_id)


def _fake_db_context(conn: _RelationChoiceConn):
    @contextmanager
    def _db_conn():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def _db_cursor(active_conn):
        cur = active_conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    return _db_conn, _db_cursor


class RelationChoicePersistenceTests(unittest.TestCase):
    def _client(self) -> Phase3Client:
        with patch.object(Phase3Client, "_ensure_direction_schema", lambda *_: None), patch.object(
            Phase3Client, "_ensure_sequence_resolution_schema", lambda *_: None
        ):
            return Phase3Client()

    def test_set_chosen_relation_persists_route_job_without_relation_candidate_writes(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        conn = _RelationChoiceConn(
            route_id=route_id,
            chosen_relation_id=111,
            candidates=[
                _candidate_row(111, score=120.0, rank=2),
                _candidate_row(222, score=140.0, rank=1),
            ],
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(conn)

        with patch("datamind_console.phases.phase3_routes.client.db_conn", fake_db_conn), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor", fake_db_cursor
        ):
            client.set_chosen_relation(route_id, 222)

        self.assertEqual(conn.persisted_chosen_relation_id, 222)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)
        self.assertFalse(
            any("UPDATE route_raw.relation_candidates" in sql for sql, _params in conn.executed)
        )

    def test_list_relation_candidates_marks_only_latest_persisted_choice(self) -> None:
        client = self._client()
        route_id = uuid.uuid4()
        conn = _RelationChoiceConn(
            route_id=route_id,
            chosen_relation_id=111,
            candidates=[
                _candidate_row(111, score=120.0, rank=3),
                _candidate_row(222, score=135.0, rank=2),
                _candidate_row(333, score=150.0, rank=1),
            ],
        )
        fake_db_conn, fake_db_cursor = _fake_db_context(conn)

        with patch("datamind_console.phases.phase3_routes.client.db_conn", fake_db_conn), patch(
            "datamind_console.phases.phase3_routes.client.db_cursor", fake_db_cursor
        ):
            client.set_chosen_relation(route_id, 222)
            client.set_chosen_relation(route_id, 333)
            rows = client.list_relation_candidates(route_id)

        flags = {int(row["osm_relation_id"]): bool(row.get("is_chosen")) for row in rows}

        self.assertEqual(conn.persisted_chosen_relation_id, 333)
        self.assertEqual(flags, {111: False, 222: False, 333: True})
        self.assertEqual(int(rows[0]["osm_relation_id"]), 333)

