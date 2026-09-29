"""
Tests for the route trash / papelera system.

These tests use a real database connection (integration tests).
Run with: python -m pytest phase3_routes/services/route_constructor/tests/test_trash_repo.py -v

Requires:
- DB_DSN / DATABASE_URL / SUPABASE_DB_* env vars
- Migration 030_route_trash.sql applied
- At least one route_job in the DB (or the test creates one)
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor


# ---------------------------------------------------------------------------
# Helpers -- lightweight DB access without importing full repo stack
# ---------------------------------------------------------------------------

def _get_dsn() -> str:
    dsn = os.environ.get("DB_DSN") or os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    host = os.environ.get("SUPABASE_DB_HOST")
    if host:
        return (
            f"host={host} "
            f"port={os.environ.get('SUPABASE_DB_PORT', '5432')} "
            f"dbname={os.environ.get('SUPABASE_DB_NAME', 'postgres')} "
            f"user={os.environ.get('SUPABASE_DB_USER', 'postgres')} "
            f"password={os.environ.get('SUPABASE_DB_PASSWORD', '')} "
            f"sslmode=require"
        )
    return ""


def _apply_sql_file(cur, path: Path) -> None:
    cur.execute(path.read_text(encoding="utf-8"))


def _create_test_route(cur, *, status="new", notes="trash_test_route") -> uuid.UUID:
    """Insert a minimal route_jobs row for testing."""
    route_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO route_raw.route_jobs (route_id, status, notes, is_trashed)
        VALUES (%s, %s, %s, FALSE)
        """,
        (str(route_id), status, notes),
    )
    return route_id


def _cleanup_test_route(cur, route_id: uuid.UUID):
    """Remove test artifacts."""
    rid = str(route_id)
    cur.execute("DELETE FROM route_trash.delete_events WHERE route_id = %s", (rid,))
    cur.execute("DELETE FROM route_trash.trash_items WHERE route_id = %s", (rid,))
    cur.execute("DELETE FROM route_raw.route_jobs WHERE route_id = %s", (rid,))


def _create_test_service_route_binding(cur, route_id: uuid.UUID, *, direction_id: int = 0) -> uuid.UUID:
    """Bind a route into a real service_route slot for client-level trash tests."""
    service_route_id = uuid.uuid4()
    sid = str(service_route_id)
    rid = str(route_id)
    did = 0 if int(direction_id) <= 0 else 1

    cur.execute(
        """
        INSERT INTO route_raw.service_routes (
            service_route_id, route_ref, route_name, notes, route_approval_status
        ) VALUES (%s, %s, %s, %s, 'approved')
        """,
        (sid, "TST", "Trash Test Route", "trash test binding"),
    )
    cur.execute(
        """
        INSERT INTO route_raw.service_route_directions (
            service_route_id, direction_id, route_id, phase3_progress_step,
            direction_approval_status, geom_source
        ) VALUES
            (%s, 0, %s, 3, 'ready', 'manual'),
            (%s, 1, NULL, 0, 'pending', 'unknown')
        ON CONFLICT (service_route_id, direction_id) DO NOTHING
        """,
        (sid, rid if did == 0 else None, sid),
    )
    if did == 1:
        cur.execute(
            """
            UPDATE route_raw.service_route_directions
            SET route_id = %s, phase3_progress_step = 3, direction_approval_status = 'ready', geom_source = 'manual'
            WHERE service_route_id = %s
              AND direction_id = 1
            """,
            (rid, sid),
        )
    cur.execute(
        """
        UPDATE route_raw.route_jobs
        SET service_route_id = %s,
            direction_id = %s
        WHERE route_id = %s
        """,
        (sid, did, rid),
    )
    cur.execute(
        """
        INSERT INTO route_work.service_route_approvals (service_route_id, notes)
        VALUES (%s, %s)
        ON CONFLICT (service_route_id) DO NOTHING
        """,
        (sid, "trash test approval"),
    )
    return service_route_id


def _cleanup_test_service_route(cur, service_route_id: uuid.UUID):
    sid = str(service_route_id)
    cur.execute("DELETE FROM route_work.service_route_approvals WHERE service_route_id = %s", (sid,))
    cur.execute("DELETE FROM route_raw.service_route_directions WHERE service_route_id = %s", (sid,))
    cur.execute("DELETE FROM route_raw.service_routes WHERE service_route_id = %s", (sid,))


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

@unittest.skipIf(not _get_dsn(), "No DB connection configured")
class TrashRepoIntegrationTests(unittest.TestCase):
    """Integration tests for route_trash operations."""

    @classmethod
    def setUpClass(cls):
        cls.dsn = _get_dsn()
        cls.conn = psycopg2.connect(cls.dsn)
        cls.conn.autocommit = False
        with cls.conn.cursor(cursor_factory=RealDictCursor) as cur:
            sql_path = Path(__file__).resolve().parents[1] / "sql" / "030_route_trash.sql"
            _apply_sql_file(cur, sql_path)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def setUp(self):
        """Create a fresh test route for each test."""
        self.conn.rollback()
        self.cur = self.conn.cursor(cursor_factory=RealDictCursor)
        self.route_id = _create_test_route(self.cur, status="new", notes="trash_test")
        self._extra_routes = []
        self._extra_service_routes = []

    def tearDown(self):
        """Roll back all changes -- leaves DB clean."""
        for sid in self._extra_service_routes:
            _cleanup_test_service_route(self.cur, sid)
        for rid in self._extra_routes:
            _cleanup_test_route(self.cur, rid)
        _cleanup_test_route(self.cur, self.route_id)
        self.cur.close()
        self.conn.rollback()

    # -- import lazily so test discovery works even without env --
    def _import_trash_repo(self):
        from phase3_routes.services.route_constructor.src.db.trash_repo import (
            trash_route,
            restore_route,
            trash_route_for_merge,
            get_trash_item,
            list_trash,
            list_delete_events,
            is_route_trashed,
        )
        return (
            trash_route, restore_route, trash_route_for_merge,
            get_trash_item, list_trash, list_delete_events, is_route_trashed,
        )

    # ---------------------------------------------------------------
    # Test 1: Trash creates snapshot + audit event
    # ---------------------------------------------------------------
    def test_trash_creates_snapshot_and_event(self):
        (trash_route, _, _, get_trash_item, _, list_delete_events, _) = self._import_trash_repo()

        trash_id = trash_route(
            self.conn,
            self.route_id,
            reason="test deletion",
            workflow="test_cleanup",
            actor="test_runner",
        )

        # Verify trash item
        item = get_trash_item(self.conn, trash_id)
        self.assertIsNotNone(item)
        self.assertEqual(str(item["route_id"]), str(self.route_id))
        self.assertEqual(item["deletion_reason"], "test deletion")
        self.assertEqual(item["deletion_source_workflow"], "test_cleanup")
        self.assertEqual(item["deleted_by"], "test_runner")
        self.assertEqual(item["restore_status"], "trashed")

        # Verify snapshot has route_jobs data
        snapshot = item["full_snapshot_jsonb"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        self.assertIn("route_jobs", snapshot)
        self.assertEqual(snapshot["route_jobs"]["route_id"], str(self.route_id))

        # Verify audit event
        events = list_delete_events(self.conn, route_id=self.route_id)
        self.assertGreaterEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["action_type"], "delete")
        self.assertEqual(evt["workflow_source"], "test_cleanup")

    # ---------------------------------------------------------------
    # Test 2: Trashed route excluded from active view
    # ---------------------------------------------------------------
    def test_trashed_route_excluded_from_active_view(self):
        (trash_route, _, _, _, _, _, _) = self._import_trash_repo()

        # Before trash -- visible in active view
        self.cur.execute(
            "SELECT 1 FROM route_raw.active_route_jobs WHERE route_id = %s",
            (str(self.route_id),),
        )
        self.assertIsNotNone(self.cur.fetchone())

        # Trash it
        trash_route(
            self.conn, self.route_id,
            reason="test", workflow="test", actor="test",
        )

        # After trash -- not visible in active view
        self.cur.execute(
            "SELECT 1 FROM route_raw.active_route_jobs WHERE route_id = %s",
            (str(self.route_id),),
        )
        self.assertIsNone(self.cur.fetchone())

        # But still in base table
        self.cur.execute(
            "SELECT is_trashed FROM route_raw.route_jobs WHERE route_id = %s",
            (str(self.route_id),),
        )
        row = self.cur.fetchone()
        self.assertTrue(row["is_trashed"])

    # ---------------------------------------------------------------
    # Test 3: Restore returns route to active state
    # ---------------------------------------------------------------
    def test_restore_returns_to_active(self):
        (trash_route, restore_route, _, get_trash_item, _, _, _) = self._import_trash_repo()

        trash_id = trash_route(
            self.conn, self.route_id,
            reason="test", workflow="test", actor="test",
        )

        # Restore
        restored_id = restore_route(
            self.conn, trash_id,
            actor="restorer",
            restore_status="new",
            notes="restoring for re-review",
        )
        self.assertEqual(str(restored_id), str(self.route_id))

        # Route is active again
        self.cur.execute(
            "SELECT is_trashed, status FROM route_raw.route_jobs WHERE route_id = %s",
            (str(self.route_id),),
        )
        row = self.cur.fetchone()
        self.assertFalse(row["is_trashed"])
        self.assertEqual(row["status"], "new")

        # Visible in active view
        self.cur.execute(
            "SELECT 1 FROM route_raw.active_route_jobs WHERE route_id = %s",
            (str(self.route_id),),
        )
        self.assertIsNotNone(self.cur.fetchone())

        # Trash item marked as restored
        item = get_trash_item(self.conn, trash_id)
        self.assertEqual(item["restore_status"], "restored")
        self.assertIsNotNone(item["restored_at"])
        self.assertEqual(item["restored_by"], "restorer")

    # ---------------------------------------------------------------
    # Test 4: Restore is idempotent
    # ---------------------------------------------------------------
    def test_restore_idempotent(self):
        (trash_route, restore_route, _, _, _, _, _) = self._import_trash_repo()

        trash_id = trash_route(
            self.conn, self.route_id,
            reason="test", workflow="test", actor="test",
        )

        # Restore twice
        r1 = restore_route(self.conn, trash_id, actor="test")
        r2 = restore_route(self.conn, trash_id, actor="test")
        self.assertEqual(str(r1), str(r2))

    # ---------------------------------------------------------------
    # Test 5: Trash is idempotent for already-trashed route
    # ---------------------------------------------------------------
    def test_trash_idempotent(self):
        (trash_route, _, _, _, _, _, _) = self._import_trash_repo()

        t1 = trash_route(
            self.conn, self.route_id,
            reason="first", workflow="test", actor="test",
        )
        t2 = trash_route(
            self.conn, self.route_id,
            reason="second", workflow="test", actor="test",
        )
        self.assertEqual(str(t1), str(t2))

    # ---------------------------------------------------------------
    # Test 6: Merge/replace records replaced_by metadata
    # ---------------------------------------------------------------
    def test_merge_records_replacement(self):
        (_, _, trash_route_for_merge, get_trash_item, _, list_delete_events, _) = (
            self._import_trash_repo()
        )

        canonical_id = _create_test_route(self.cur, status="approved", notes="canonical")
        self._extra_routes.append(canonical_id)

        trash_id = trash_route_for_merge(
            self.conn,
            self.route_id,
            canonical_route_id=canonical_id,
            reason="duplicate of canonical",
            actor="deduper",
        )

        item = get_trash_item(self.conn, trash_id)
        self.assertEqual(str(item["replaced_by_route_id"]), str(canonical_id))

        # Should have both a 'delete' and a 'merge' event
        events = list_delete_events(self.conn, route_id=self.route_id)
        action_types = {e["action_type"] for e in events}
        self.assertIn("delete", action_types)
        self.assertIn("merge", action_types)

        merge_evt = next(e for e in events if e["action_type"] == "merge")
        self.assertEqual(str(merge_evt["canonical_route_id"]), str(canonical_id))

    # ---------------------------------------------------------------
    # Test 7: is_route_trashed helper
    # ---------------------------------------------------------------
    def test_is_route_trashed(self):
        (trash_route, restore_route, _, _, _, _, is_route_trashed) = self._import_trash_repo()

        self.assertFalse(is_route_trashed(self.conn, self.route_id))

        trash_id = trash_route(
            self.conn, self.route_id,
            reason="test", workflow="test", actor="test",
        )
        self.assertTrue(is_route_trashed(self.conn, self.route_id))

        restore_route(self.conn, trash_id, actor="test")
        self.assertFalse(is_route_trashed(self.conn, self.route_id))

    # ---------------------------------------------------------------
    # Test 8: list_trash filtering
    # ---------------------------------------------------------------
    def test_list_trash_filtering(self):
        (trash_route, _, _, _, list_trash, _, _) = self._import_trash_repo()

        trash_route(
            self.conn, self.route_id,
            reason="test", workflow="special_workflow", actor="test",
        )

        # By route_id
        items = list_trash(self.conn, route_id=self.route_id)
        self.assertEqual(len(items), 1)

        # By workflow
        items = list_trash(self.conn, workflow="special_workflow")
        self.assertGreaterEqual(len(items), 1)
        self.assertTrue(any(str(i["route_id"]) == str(self.route_id) for i in items))

    # ---------------------------------------------------------------
    # Test 9: Non-deleting workflows unaffected
    # ---------------------------------------------------------------
    def test_non_deleting_route_unaffected(self):
        """A route that is NOT trashed remains fully visible and functional."""
        # Just verify it's visible
        self.cur.execute(
            "SELECT 1 FROM route_raw.active_route_jobs WHERE route_id = %s",
            (str(self.route_id),),
        )
        self.assertIsNotNone(self.cur.fetchone())

        self.cur.execute(
            "SELECT is_trashed FROM route_raw.route_jobs WHERE route_id = %s",
            (str(self.route_id),),
        )
        row = self.cur.fetchone()
        self.assertFalse(row["is_trashed"])

    # ---------------------------------------------------------------
    # Test 10: Client active surfaces hide trashed routes and restore rebinds
    # ---------------------------------------------------------------
    def test_client_active_surfaces_hide_trashed_route_and_restore_rebinds(self):
        from datamind_console.phases.phase3_routes.client import Phase3Client

        service_route_id = _create_test_service_route_binding(self.cur, self.route_id, direction_id=0)
        self._extra_service_routes.append(service_route_id)
        self.conn.commit()

        client = Phase3Client()

        active_jobs_before = {
            str(row.get("route_id"))
            for row in (client.list_route_jobs(limit=500) or [])
            if row.get("route_id")
        }
        self.assertIn(str(self.route_id), active_jobs_before)

        service_rows_before = {
            str(row.get("service_route_id")): dict(row)
            for row in (client.list_service_routes(limit=500) or [])
            if row.get("service_route_id")
        }
        self.assertEqual(
            str(service_rows_before[str(service_route_id)]["route_id_0"]),
            str(self.route_id),
        )

        trash_out = client.trash_route_job(
            self.route_id,
            reason="client trash test",
            workflow="test_queue_cleanup",
            actor="test_runner",
        )
        self.assertIn(trash_out["status"], {"trashed", "already_trashed"})
        self.assertTrue(trash_out.get("trash_id"))

        active_jobs_after = {
            str(row.get("route_id"))
            for row in (client.list_route_jobs(limit=500) or [])
            if row.get("route_id")
        }
        self.assertNotIn(str(self.route_id), active_jobs_after)

        service_rows_after = {
            str(row.get("service_route_id")): dict(row)
            for row in (client.list_service_routes(limit=500) or [])
            if row.get("service_route_id")
        }
        self.assertFalse(service_rows_after[str(service_route_id)].get("route_id_0"))
        self.assertEqual(service_rows_after[str(service_route_id)]["direction_status_0"], "pending")

        restore_out = client.restore_route(
            trash_out["trash_id"],
            actor="restorer",
            restore_status="new",
            notes="restore into review",
        )
        self.assertEqual(str(restore_out["route_id"]), str(self.route_id))

        active_jobs_restored = {
            str(row.get("route_id"))
            for row in (client.list_route_jobs(limit=500) or [])
            if row.get("route_id")
        }
        self.assertIn(str(self.route_id), active_jobs_restored)

        service_rows_restored = {
            str(row.get("service_route_id")): dict(row)
            for row in (client.list_service_routes(limit=500) or [])
            if row.get("service_route_id")
        }
        self.assertEqual(
            str(service_rows_restored[str(service_route_id)]["route_id_0"]),
            str(self.route_id),
        )
        self.assertEqual(service_rows_restored[str(service_route_id)]["direction_status_0"], "pending")

        events = client.list_delete_events(route_id=self.route_id, limit=20)
        action_types = {e["action_type"] for e in events}
        self.assertIn("delete", action_types)
        self.assertIn("deactivate", action_types)
        self.assertIn("restore", action_types)


@unittest.skipIf(not _get_dsn(), "No DB connection configured")
class DeleteServiceRouteIntegrationTests(unittest.TestCase):
    """
    Integration tests for the delete_service_route() trash workflow.

    Validated live 2026-03-08 against real Supabase DB.
    Covers the full path: trash (non-purge), visibility, restore, idempotence.
    """

    @classmethod
    def setUpClass(cls):
        cls.dsn = _get_dsn()
        cls.conn = psycopg2.connect(cls.dsn)
        cls.conn.autocommit = False
        with cls.conn.cursor(cursor_factory=RealDictCursor) as cur:
            sql_path = Path(__file__).resolve().parents[1] / "sql" / "030_route_trash.sql"
            _apply_sql_file(cur, sql_path)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        cls.conn.close()

    def setUp(self):
        self.conn.rollback()
        self.cur = self.conn.cursor(cursor_factory=RealDictCursor)
        # Create two route_jobs + a service_route binding both directions
        self.route_id_0 = _create_test_route(self.cur, status="fetched", notes="svc_route_trash_test_dir0")
        self.route_id_1 = _create_test_route(self.cur, status="new", notes="svc_route_trash_test_dir1")
        self.service_route_id = self._create_two_direction_service_route(
            self.cur, self.route_id_0, self.route_id_1
        )
        self.conn.commit()

    def tearDown(self):
        sid = str(self.service_route_id)
        rids = [str(self.route_id_0), str(self.route_id_1)]
        self.cur.execute("DELETE FROM route_trash.delete_events WHERE route_id = ANY(%s::uuid[])", (rids,))
        self.cur.execute("DELETE FROM route_trash.trash_items WHERE route_id = ANY(%s::uuid[])", (rids,))
        self.cur.execute("DELETE FROM route_work.service_route_approvals WHERE service_route_id = %s::uuid", (sid,))
        self.cur.execute("DELETE FROM route_raw.service_route_directions WHERE service_route_id = %s::uuid", (sid,))
        self.cur.execute("DELETE FROM route_raw.service_routes WHERE service_route_id = %s::uuid", (sid,))
        self.cur.execute("DELETE FROM route_raw.route_jobs WHERE route_id = ANY(%s::uuid[])", (rids,))
        self.cur.close()
        self.conn.rollback()

    @staticmethod
    def _create_two_direction_service_route(cur, route_id_0, route_id_1) -> uuid.UUID:
        sid = uuid.uuid4()
        rid0, rid1 = str(route_id_0), str(route_id_1)
        cur.execute(
            """
            INSERT INTO route_raw.service_routes
                (service_route_id, route_ref, route_name, notes, route_approval_status)
            VALUES (%s, 'TST-2D', 'Svc Route Trash Test 2dirs', 'svc_route_trash_test', 'pending')
            """,
            (str(sid),),
        )
        cur.execute(
            """
            INSERT INTO route_raw.service_route_directions
                (service_route_id, direction_id, route_id, phase3_progress_step,
                 direction_approval_status, geom_source)
            VALUES
                (%s::uuid, 0, %s::uuid, 2, 'in_progress', 'manual'),
                (%s::uuid, 1, %s::uuid, 1, 'pending', 'unknown')
            ON CONFLICT (service_route_id, direction_id) DO NOTHING
            """,
            (str(sid), rid0, str(sid), rid1),
        )
        cur.execute(
            "UPDATE route_raw.route_jobs SET service_route_id = %s::uuid, direction_id = 0 WHERE route_id = %s::uuid",
            (str(sid), rid0),
        )
        cur.execute(
            "UPDATE route_raw.route_jobs SET service_route_id = %s::uuid, direction_id = 1 WHERE route_id = %s::uuid",
            (str(sid), rid1),
        )
        cur.execute(
            """
            INSERT INTO route_work.service_route_approvals (service_route_id, notes)
            VALUES (%s::uuid, 'svc_route_trash_test_approval')
            ON CONFLICT (service_route_id) DO NOTHING
            """,
            (str(sid),),
        )
        return sid

    # ---------------------------------------------------------------
    # Test 11: delete_service_route() non-purge preserves service_routes shell
    # ---------------------------------------------------------------
    def test_delete_service_route_trash_preserves_shell(self):
        """
        delete_service_route(purge=False) must NOT hard-delete the service_routes row.
        It should demote status to 'pending' and trash the constituent route_jobs.

        Bug fixed 2026-03-08: previously the shell was being hard-deleted even in
        non-purge mode (labeled 'archived_service_route_shell' but was a destructive DELETE).
        """
        from datamind_console.phases.phase3_routes.client import Phase3Client

        client = Phase3Client()
        sid = str(self.service_route_id)

        out = client.delete_service_route(
            sid,
            delete_route_prod=True,
            delete_node_requests=True,
            delete_all_related=True,
            dry_run=False,
            purge=False,
        )

        self.assertFalse(out["purged"])
        self.assertEqual(int(out.get("trashed_route_jobs") or 0), 2)
        self.assertEqual(int(out.get("deleted_route_jobs") or 0), 0)

        # Shell MUST still exist after non-purge trash
        self.cur.execute(
            "SELECT route_approval_status FROM route_raw.service_routes WHERE service_route_id = %s::uuid",
            (sid,),
        )
        sr_row = self.cur.fetchone()
        self.assertIsNotNone(sr_row, "service_routes shell must be preserved after non-purge trash")
        self.assertEqual(
            sr_row["route_approval_status"], "pending",
            "service_routes shell must be demoted to 'pending' after trash",
        )

    # ---------------------------------------------------------------
    # Test 12: delete_service_route() trashes both route_jobs
    # ---------------------------------------------------------------
    def test_delete_service_route_trashes_all_constituent_routes(self):
        from datamind_console.phases.phase3_routes.client import Phase3Client

        client = Phase3Client()
        sid = str(self.service_route_id)
        rids = [str(self.route_id_0), str(self.route_id_1)]

        client.delete_service_route(sid, dry_run=False, purge=False)

        self.cur.execute(
            "SELECT route_id, is_trashed FROM route_raw.route_jobs WHERE route_id = ANY(%s::uuid[])",
            (rids,),
        )
        rows = {str(r["route_id"]): r["is_trashed"] for r in self.cur.fetchall()}
        self.assertTrue(rows[str(self.route_id_0)], "dir=0 route must be trashed")
        self.assertTrue(rows[str(self.route_id_1)], "dir=1 route must be trashed")

        # Both should be absent from active view
        self.cur.execute(
            "SELECT COUNT(*)::int AS n FROM route_raw.active_route_jobs WHERE route_id = ANY(%s::uuid[])",
            (rids,),
        )
        self.assertEqual(int((self.cur.fetchone() or {}).get("n") or 0), 0)

    # ---------------------------------------------------------------
    # Test 13: delete_service_route() creates snapshots + audit trail
    # ---------------------------------------------------------------
    def test_delete_service_route_creates_snapshots_and_audit(self):
        from datamind_console.phases.phase3_routes.client import Phase3Client

        client = Phase3Client()
        sid = str(self.service_route_id)
        rids = [str(self.route_id_0), str(self.route_id_1)]

        client.delete_service_route(sid, dry_run=False, purge=False)

        # Both routes must have trash_items
        self.cur.execute(
            "SELECT COUNT(*)::int AS n FROM route_trash.trash_items WHERE route_id = ANY(%s::uuid[]) AND restore_status = 'trashed'",
            (rids,),
        )
        n_items = int((self.cur.fetchone() or {}).get("n") or 0)
        self.assertEqual(n_items, 2, "Expected 2 trash_items (one per direction)")

        # Audit events must include delete + deactivate
        self.cur.execute(
            "SELECT DISTINCT action_type FROM route_trash.delete_events WHERE route_id = ANY(%s::uuid[])",
            (rids,),
        )
        action_types = {r["action_type"] for r in self.cur.fetchall()}
        self.assertIn("delete", action_types)
        self.assertIn("deactivate", action_types)

    # ---------------------------------------------------------------
    # Test 14: delete_service_route() unbinds direction slots cleanly
    # ---------------------------------------------------------------
    def test_delete_service_route_unbinds_direction_slots(self):
        from datamind_console.phases.phase3_routes.client import Phase3Client

        client = Phase3Client()
        sid = str(self.service_route_id)

        client.delete_service_route(sid, dry_run=False, purge=False)

        self.cur.execute(
            "SELECT direction_id, route_id, direction_approval_status FROM route_raw.service_route_directions WHERE service_route_id = %s::uuid ORDER BY direction_id",
            (sid,),
        )
        dirs = {r["direction_id"]: dict(r) for r in self.cur.fetchall()}

        # Both direction slots should have route_id=NULL and be in pending state
        for did in (0, 1):
            self.assertIn(did, dirs)
            self.assertIsNone(dirs[did]["route_id"], f"direction {did} route_id should be NULL after trash")
            self.assertEqual(
                dirs[did]["direction_approval_status"], "pending",
                f"direction {did} status should be 'pending' after trash",
            )

    # ---------------------------------------------------------------
    # Test 15: restore after delete_service_route() rebinds correctly
    # ---------------------------------------------------------------
    def test_delete_service_route_restore_rebinds(self):
        from datamind_console.phases.phase3_routes.client import Phase3Client
        from phase3_routes.services.route_constructor.src.db.trash_repo import restore_route

        client = Phase3Client()
        sid = str(self.service_route_id)

        client.delete_service_route(sid, dry_run=False, purge=False)

        # Fetch trash_id for route_id_0
        self.cur.execute(
            "SELECT trash_id FROM route_trash.trash_items WHERE route_id = %s::uuid AND restore_status = 'trashed' LIMIT 1",
            (str(self.route_id_0),),
        )
        trash_id_0 = self.cur.fetchone()["trash_id"]

        # Restore dir=0 route
        restored_id = restore_route(
            self.conn,
            trash_id_0,
            actor="test_restorer",
            restore_status="new",
            notes="restore from delete_service_route test",
        )
        self.assertEqual(str(restored_id), str(self.route_id_0))

        # Route must be active again
        self.cur.execute(
            "SELECT is_trashed, status FROM route_raw.route_jobs WHERE route_id = %s::uuid",
            (str(self.route_id_0),),
        )
        rj = self.cur.fetchone()
        self.assertFalse(rj["is_trashed"])
        self.assertEqual(rj["status"], "new")

        # Must appear in active_route_jobs
        self.cur.execute(
            "SELECT 1 FROM route_raw.active_route_jobs WHERE route_id = %s::uuid",
            (str(self.route_id_0),),
        )
        self.assertIsNotNone(self.cur.fetchone())

        # Direction slot must be rebound
        self.cur.execute(
            "SELECT route_id FROM route_raw.service_route_directions WHERE service_route_id = %s::uuid AND direction_id = 0",
            (sid,),
        )
        dir0_row = self.cur.fetchone()
        self.assertIsNotNone(dir0_row)
        self.assertEqual(str(dir0_row["route_id"]), str(self.route_id_0))

        # Trash item must be marked restored
        self.cur.execute(
            "SELECT restore_status FROM route_trash.trash_items WHERE trash_id = %s::uuid",
            (str(trash_id_0),),
        )
        ti = self.cur.fetchone()
        self.assertEqual(ti["restore_status"], "restored")

    # ---------------------------------------------------------------
    # Test 16: delete_service_route() dry_run makes no changes
    # ---------------------------------------------------------------
    def test_delete_service_route_dry_run_no_changes(self):
        from datamind_console.phases.phase3_routes.client import Phase3Client

        client = Phase3Client()
        sid = str(self.service_route_id)
        rids = [str(self.route_id_0), str(self.route_id_1)]

        out = client.delete_service_route(sid, dry_run=True, purge=False)
        self.assertTrue(out["dry_run"])
        self.assertTrue(out["service_route_exists"])
        self.assertEqual(len(out["bound_route_ids"]), 2)

        # No changes made
        self.cur.execute(
            "SELECT COUNT(*)::int AS n FROM route_raw.route_jobs WHERE route_id = ANY(%s::uuid[]) AND is_trashed = FALSE",
            (rids,),
        )
        n_active = int((self.cur.fetchone() or {}).get("n") or 0)
        self.assertEqual(n_active, 2, "Dry run must not trash any routes")

    # ---------------------------------------------------------------
    # Test 17: delete_service_route(purge=True) hard-deletes shell
    # ---------------------------------------------------------------
    def test_delete_service_route_purge_deletes_shell(self):
        from datamind_console.phases.phase3_routes.client import Phase3Client

        client = Phase3Client()
        sid = str(self.service_route_id)

        out = client.delete_service_route(sid, dry_run=False, purge=True)
        self.assertTrue(out["purged"])
        self.assertEqual(int(out.get("deleted_service_route") or 0), 1)

        self.cur.execute(
            "SELECT 1 FROM route_raw.service_routes WHERE service_route_id = %s::uuid",
            (sid,),
        )
        self.assertIsNone(self.cur.fetchone(), "Purge must hard-delete the service_routes row")

    # ---------------------------------------------------------------
    # Test 18: skip_trash=True guard creates snapshot before hard-delete
    # ---------------------------------------------------------------
    def test_delete_route_job_skip_trash_creates_snapshot(self):
        """
        delete_route_job(skip_trash=True) must snapshot the route before hard-deleting.
        Bug fixed 2026-03-08: the guard condition `and not skip_trash` was wrong, causing
        skip_trash=True to bypass snapshot creation entirely.
        """
        from datamind_console.phases.phase3_routes.client import Phase3Client

        client = Phase3Client()
        rid = str(self.route_id_0)

        # Pre-check: not trashed
        self.cur.execute(
            "SELECT is_trashed FROM route_raw.route_jobs WHERE route_id = %s::uuid", (rid,)
        )
        self.assertFalse(self.cur.fetchone()["is_trashed"])

        out = client.delete_route_job(
            rid,
            skip_trash=True,
            prune_empty_service_routes=False,
            dry_run=False,
        )
        self.assertTrue(out["purged"])
        self.assertEqual(int(out.get("deleted_route_job") or 0), 1)

        # A trash snapshot must exist from the pre-delete guard
        self.cur.execute(
            "SELECT COUNT(*)::int AS n FROM route_trash.trash_items WHERE route_id = %s::uuid",
            (rid,),
        )
        n_snaps = int((self.cur.fetchone() or {}).get("n") or 0)
        self.assertGreaterEqual(n_snaps, 1, "skip_trash=True must still create a snapshot before hard-delete")


if __name__ == "__main__":
    unittest.main()
